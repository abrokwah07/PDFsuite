"""Concurrency and simple per-IP rate limiting for heavy PDF jobs."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Deque

from fastapi import HTTPException, Request

from app.config import Settings


class JobLimiter:
    """
    Process-wide concurrency gate so OCR / Ghostscript / LibreOffice cannot
    pile up and OOM the host. Fails fast with 503 when saturated.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self._max = max(1, max_concurrent)
        self._sem = asyncio.Semaphore(self._max)
        self._active = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self, *, label: str = "job") -> AsyncIterator[None]:
        async with self._lock:
            if self._active >= self._max:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Server is busy processing other {label}s. "
                        "Try again in a moment, or raise MAX_CONCURRENT_JOBS."
                    ),
                )
            self._active += 1

        await self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()
            async with self._lock:
                self._active = max(0, self._active - 1)


class RateLimiter:
    """In-memory sliding-window rate limiter (single-process)."""

    def __init__(self, max_per_minute: int = 120) -> None:
        self._max = max(1, max_per_minute)
        self._hits: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        window = 60.0
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= self._max:
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Slow down and try again shortly.",
                    headers={"Retry-After": "30"},
                )
            q.append(now)


def client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def enforce_api_rate_limit(request: Request, settings: Settings) -> None:
    if settings.rate_limit_per_minute <= 0:
        return
    if not request.url.path.startswith("/api/"):
        return
    limiter: RateLimiter = request.app.state.rate_limiter
    limiter.check(client_ip(request))
