"""HTTP middleware: request IDs, security headers, body limits, rate limits."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings
from app.job_limit import enforce_api_rate_limit

logger = logging.getLogger("pdfsuite")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach request IDs, security headers, rate limits, and access logs."""

    async def dispatch(self, request: Request, call_next):
        settings: Settings = request.app.state.settings
        request_id = request.headers.get(settings.request_id_header) or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()

        # Reject oversized requests early when Content-Length is present
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > settings.max_request_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": "Request body is too large.",
                            "request_id": request_id,
                        },
                        headers={"X-Request-ID": request_id},
                    )
            except ValueError:
                pass

        # Optional API key for network-exposed deployments
        if settings.api_key and request.url.path.startswith("/api/"):
            provided = request.headers.get("X-API-Key") or ""
            auth = request.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip() or provided
            if not provided or provided != settings.api_key:
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Missing or invalid API key.",
                        "request_id": request_id,
                    },
                    headers={"X-Request-ID": request_id, "WWW-Authenticate": "Bearer"},
                )

        try:
            enforce_api_rate_limit(request, settings)
        except Exception as exc:
            # HTTPException from rate limiter
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                headers = {"X-Request-ID": request_id}
                if exc.headers:
                    headers.update(exc.headers)
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail, "request_id": request_id},
                    headers=headers,
                )
            raise

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "Unhandled error request_id=%s path=%s", request_id, request.url.path
            )
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        # Local tool: Tailwind CDN is used by index.html; keep a deliberate CSP.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'self'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )

        logger.info(
            "request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
