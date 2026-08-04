"""
Local PDF Suite — FastAPI backend.

All document processing runs locally. Uploads are size-checked, filenames are
sanitized, subprocesses are timed out, jobs are concurrency-limited, and
temporary files are cleaned up.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import pdfplumber
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions
from reportlab.pdfgen import canvas

from app.config import Settings, get_settings
from app.conversion import (
    convert_pdf_to_excel,
    convert_pdf_to_word,
    count_pages,
    pages_label,
    resolve_pages,
    run_ocr,
)
from app.job_limit import JobLimiter, RateLimiter
from app.jobs import JobStatus, JobStore
from app.middleware import RequestContextMiddleware
from app.office_compress import compress_ooxml
from app.pdf_editor import apply_edits, inspect_page
from app.pptx_convert import (
    OFFICE_FORMAT_MAP,
    detect_source,
    libreoffice_convert,
    media_type_for_ext,
    pdf_to_pptx,
)
from app.security import (
    OFFICE_COMPRESS_EXTENSIONS,
    OFFICE_CROSS_CONVERT_EXTENSIONS,
    OFFICE_TO_PDF_EXTENSIONS,
    PDF_EXTENSIONS,
    assert_zip_safe,
    is_safe_zip_member,
    parse_page_spec,
    read_upload,
    validate_file_count,
    validate_password,
)
from app.subprocess_util import run_subprocess
from app.tempfiles import (
    file_response,
    make_temp_dir,
    make_temp_path,
    safe_rmtree,
    safe_unlink,
    schedule_cleanup,
    write_bytes,
)

# Optional heavy dependencies — degraded gracefully if missing
try:
    import ocrmypdf
except ImportError:  # pragma: no cover
    ocrmypdf = None  # type: ignore

try:
    from pdf2docx import Converter
except ImportError:  # pragma: no cover
    Converter = None  # type: ignore

try:
    import camelot
except ImportError:  # pragma: no cover
    camelot = None  # type: ignore

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore

logger = logging.getLogger("pdfsuite")


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _limiter(request: Request) -> JobLimiter:
    return request.app.state.job_limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings)
    app.state.settings = settings
    app.state.started_at = time.time()
    app.state.gs_path = _which("gs")
    app.state.libreoffice_path = _which("libreoffice") or _which("soffice")
    app.state.tesseract_path = _which("tesseract")
    app.state.job_limiter = JobLimiter(settings.max_concurrent_jobs)
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.job_store = JobStore(ttl_seconds=3600, max_jobs=80)

    logger.info(
        "Starting %s v%s env=%s gs=%s libreoffice=%s tesseract=%s ocrmypdf=%s camelot=%s api_key=%s",
        settings.app_name,
        settings.app_version,
        settings.environment,
        bool(app.state.gs_path),
        bool(app.state.libreoffice_path),
        bool(app.state.tesseract_path),
        ocrmypdf is not None,
        camelot is not None,
        bool(settings.api_key),
    )
    yield
    logger.info("Shutting down %s", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url="/redoc" if settings.environment != "production" else None,
        openapi_url="/openapi.json" if settings.environment != "production" else None,
        lifespan=lifespan,
    )

    # Trusted hosts (optional hardening when set)
    if settings.trusted_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allow_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[
            "X-Request-ID",
            "Content-Disposition",
            "X-Pages-Processed",
            "X-Pages-Total",
            "X-Convert-Engine",
            "X-Convert-Detail",
            "X-Chunks",
            "X-Pages-Label",
        ],
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        request_id = getattr(request.state, "request_id", None)
        headers = {"X-Request-ID": request_id or ""}
        if exc.headers:
            headers.update(exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": request_id},
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled exception request_id=%s", request_id)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "An unexpected server error occurred.",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id or ""},
        )

    # ------------------------------------------------------------------
    # System endpoints
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def read_index(request: Request):
        settings = _settings(request)
        if not settings.index_html.is_file():
            raise HTTPException(status_code=500, detail="Frontend index.html is missing.")
        html = settings.index_html.read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/favicon.ico")
    async def favicon():
        # Tiny inline SVG favicon (avoids 404 noise in browser consoles)
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
            "<rect width='32' height='32' rx='6' fill='#2563eb'/>"
            "<path d='M9 8h9l5 5v11a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2z' fill='white'/>"
            "</svg>"
        )
        from fastapi.responses import Response

        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/health")
    async def health(request: Request):
        settings = _settings(request)
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
            "uptime_seconds": round(time.time() - request.app.state.started_at, 1),
        }

    @app.get("/api/convert/detect")
    async def convert_detect(filename: str = ""):
        """
        Given a filename, return detected source type and valid conversion targets.
        Used by the Smart Convert UI so users only pick the *output* format.
        """
        info = detect_source(filename or "")
        status = 200 if info.get("ok") else 400
        return JSONResponse(status_code=status, content=info)

    @app.get("/ready")
    async def ready(request: Request):
        """Readiness: core Python PDF stack must work; system tools reported."""
        settings = _settings(request)
        checks = {
            "index_html": settings.index_html.is_file(),
            "ghostscript": bool(request.app.state.gs_path) if settings.enable_ghostscript else True,
            "libreoffice": bool(request.app.state.libreoffice_path)
            if settings.enable_libreoffice
            else True,
            "ocrmypdf": (ocrmypdf is not None and bool(request.app.state.tesseract_path))
            if settings.enable_ocr
            else True,
            "pymupdf": True,  # soft — edit endpoints self-check
        }
        try:
            import fitz  # noqa: F401

            checks["pymupdf"] = True
        except ImportError:
            checks["pymupdf"] = False

        core_ok = checks["index_html"]
        status = "ready" if core_ok else "degraded"
        return JSONResponse(
            status_code=200 if core_ok else 503,
            content={
                "status": status,
                "checks": checks,
                "features": {
                    "ocr": settings.enable_ocr and checks["ocrmypdf"],
                    "ghostscript": settings.enable_ghostscript and checks["ghostscript"],
                    "libreoffice": settings.enable_libreoffice and checks["libreoffice"],
                    "camelot": settings.enable_camelot and camelot is not None,
                    "edit": checks["pymupdf"],
                    "api_key_required": bool(settings.api_key),
                },
                "limits": {
                    "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
                    "max_files_per_request": settings.max_files_per_request,
                    "max_ocr_pages": settings.max_ocr_pages,
                    "max_auto_ocr_pages": settings.max_auto_ocr_pages,
                    "max_word_pages": settings.max_word_total_pages,
                    "max_excel_pages": settings.max_excel_total_pages,
                    "word_chunk_pages": settings.word_chunk_pages,
                    "excel_chunk_pages": settings.excel_chunk_pages,
                    "max_concurrent_jobs": settings.max_concurrent_jobs,
                    "auto_chunk_merge": True,
                    "background_jobs": True,
                    "smart_scan_convert": True,
                    "max_pptx_pages": settings.max_pptx_total_pages,
                },
            },
        )

    # ------------------------------------------------------------------
    # Background jobs (convert with live progress + cancel)
    # ------------------------------------------------------------------

    def _job_store(request: Request) -> JobStore:
        return request.app.state.job_store

    def _run_word_job(
        job_id: str,
        store: JobStore,
        settings: Settings,
        *,
        input_path: str,
        name: str,
        pages: str,
        mode: str,
        gs_libre: object = None,
    ) -> None:
        """Synchronous worker for PDF → Word (runs in a thread)."""
        output_docx = make_temp_path(".docx", settings)
        job = store.get(job_id)
        if job:
            job.cleanup_paths.extend([input_path, output_docx])

        def cancelled() -> bool:
            j = store.get(job_id)
            return bool(j and j.cancelled())

        def on_prog(cur: int, tot: int) -> None:
            store.update(
                job_id,
                progress=5 + (cur / tot * 90 if tot else 0),
                current=cur,
                total=tot,
                phase="converting",
                message=f"Page {cur} of {tot}",
            )

        try:
            store.update(
                job_id,
                status=JobStatus.running,
                progress=3,
                phase="preparing",
                message="Counting pages…",
            )
            total = count_pages(input_path)
            indices = resolve_pages(
                total_pages=total,
                pages=pages,
                max_total=settings.max_word_total_pages,
                label="PDF → Word",
            )
            store.update(
                job_id,
                total=len(indices),
                progress=5,
                phase="converting",
                message=f"Converting {len(indices)} pages…",
            )
            if cancelled():
                raise RuntimeError("cancelled")

            meta = convert_pdf_to_word(
                input_path,
                output_docx,
                indices,
                mode=mode,
                layout_threshold=settings.word_layout_page_threshold,
                chunk_size=settings.word_chunk_pages,
                converter_cls=Converter,
                enable_ocr=settings.enable_ocr and ocrmypdf is not None,
                ocrmypdf_module=ocrmypdf,
                make_temp=lambda s: make_temp_path(s, settings),
                max_ocr_pages=settings.max_auto_ocr_pages,
                on_progress=on_prog,
                should_cancel=cancelled,
            )
            chunks_used = int(meta.get("chunks") or 1)
            engine = str(meta.get("engine") or "fast")
            label = pages_label(indices, total, chunks=chunks_used)

            if cancelled():
                raise RuntimeError("cancelled")

            base_name = Path(name).stem
            if total > 0 and len(indices) < total:
                fname = f"{base_name}_p{indices[0] + 1}-{indices[-1] + 1}.docx"
            else:
                fname = f"{base_name}.docx"

            store.update(
                job_id,
                status=JobStatus.completed,
                progress=100,
                phase="done",
                message=f"Done — {label} ({engine})",
                current=len(indices),
                total=len(indices),
                result_path=output_docx,
                result_name=fname,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            j = store.get(job_id)
            if j:
                j.meta["engine"] = engine
                j.meta["pages_label"] = label
        except RuntimeError as exc:
            if str(exc) == "cancelled":
                store.update(
                    job_id,
                    status=JobStatus.cancelled,
                    phase="cancelled",
                    message="Cancelled",
                    progress=0,
                )
                safe_unlink(output_docx)
            else:
                store.update(
                    job_id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc),
                    error=str(exc),
                )
                safe_unlink(output_docx)
        except Exception as exc:
            logger.exception("word job %s failed", job_id)
            detail = getattr(exc, "detail", None) or str(exc) or "Conversion failed"
            if not isinstance(detail, str):
                detail = str(detail)
            store.update(
                job_id,
                status=JobStatus.failed,
                phase="error",
                message=detail,
                error=detail,
            )
            safe_unlink(output_docx)

    def _run_excel_job(
        job_id: str,
        store: JobStore,
        settings: Settings,
        *,
        items: list[tuple[str, str]],
        pages: str,
    ) -> None:
        """Synchronous worker for PDF → Excel."""
        temps: list[str] = [p for _, p in items]
        job = store.get(job_id)
        if job:
            job.cleanup_paths.extend(temps)

        def cancelled() -> bool:
            j = store.get(job_id)
            return bool(j and j.cancelled())

        try:
            if pd is None:
                raise RuntimeError("pandas is not installed")
            store.update(
                job_id,
                status=JobStatus.running,
                progress=3,
                phase="preparing",
                message="Extracting tables…",
            )
            excel_files: list[tuple[str, str]] = []
            last_label = ""
            last_engine = ""

            for name, input_path in items:
                if cancelled():
                    raise RuntimeError("cancelled")
                total = count_pages(input_path)
                indices = resolve_pages(
                    total_pages=total,
                    pages=pages,
                    max_total=settings.max_excel_total_pages,
                    label="PDF → Excel",
                )
                n_chunks = max(
                    1,
                    (len(indices) + settings.excel_chunk_pages - 1)
                    // settings.excel_chunk_pages,
                )
                last_label = pages_label(indices, total, chunks=n_chunks)

                def on_prog(cur: int, tot: int) -> None:
                    store.update(
                        job_id,
                        progress=5 + (cur / tot * 90 if tot else 0),
                        current=cur,
                        total=tot,
                        phase="extracting",
                        message=f"Scanning page {cur} of {tot}",
                    )

                out_path = make_temp_path(".xlsx", settings)
                temps.append(out_path)
                meta = convert_pdf_to_excel(
                    input_path,
                    out_path,
                    indices,
                    pd_module=pd,
                    chunk_size=settings.excel_chunk_pages,
                    camelot_module=camelot,
                    enable_camelot=settings.enable_camelot and camelot is not None,
                    enable_ocr=settings.enable_ocr and ocrmypdf is not None,
                    ocrmypdf_module=ocrmypdf,
                    make_temp=lambda s: make_temp_path(s, settings),
                    max_ocr_pages=settings.max_auto_ocr_pages,
                    on_progress=on_prog,
                    should_cancel=cancelled,
                )
                last_engine = str(meta.get("engine") or "")
                stem = Path(name).stem
                if total > 0 and len(indices) < total:
                    out_name = f"{stem}_p{indices[0] + 1}-{indices[-1] + 1}.xlsx"
                else:
                    out_name = f"{stem}.xlsx"
                excel_files.append((out_name, out_path))

            if cancelled():
                raise RuntimeError("cancelled")
            if not excel_files:
                raise RuntimeError(
                    "No readable tables found. Try a page range that contains tables, "
                    "or run OCR first for scanned PDFs."
                )

            if len(excel_files) == 1:
                result_path = excel_files[0][1]
                result_name = excel_files[0][0]
                media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            else:
                result_path = make_temp_path(".zip", settings)
                temps.append(result_path)
                with zipfile.ZipFile(result_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for arcname, file_path in excel_files:
                        zipf.write(file_path, arcname=arcname)
                result_name = "Batch_Excel_Extraction.zip"
                media = "application/zip"

            done_msg = f"Done — {last_label}" if last_label else "Done"
            if last_engine:
                done_msg += f" ({last_engine})"
            store.update(
                job_id,
                status=JobStatus.completed,
                progress=100,
                phase="done",
                message=done_msg,
                result_path=result_path,
                result_name=result_name,
                media_type=media,
            )
            j = store.get(job_id)
            if j:
                j.meta["pages_label"] = last_label
                j.meta["engine"] = last_engine
                # Keep result; clean other temps on discard except result
                j.cleanup_paths = [p for p in temps if p != result_path]
        except RuntimeError as exc:
            if str(exc) == "cancelled":
                store.update(
                    job_id,
                    status=JobStatus.cancelled,
                    phase="cancelled",
                    message="Cancelled",
                )
            else:
                store.update(
                    job_id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc),
                    error=str(exc),
                )
            for p in temps:
                safe_unlink(p)
        except Exception as exc:
            logger.exception("excel job %s failed", job_id)
            store.update(
                job_id,
                status=JobStatus.failed,
                phase="error",
                message=str(exc),
                error=str(exc),
            )
            for p in temps:
                safe_unlink(p)

    def _run_pptx_job(
        job_id: str,
        store: JobStore,
        settings: Settings,
        *,
        input_path: str,
        name: str,
        pages: str,
    ) -> None:
        output_pptx = make_temp_path(".pptx", settings)
        job = store.get(job_id)
        if job:
            job.cleanup_paths.extend([input_path, output_pptx])

        def cancelled() -> bool:
            j = store.get(job_id)
            return bool(j and j.cancelled())

        def on_prog(cur: int, tot: int) -> None:
            store.update(
                job_id,
                progress=5 + (cur / tot * 90 if tot else 0),
                current=cur,
                total=tot,
                phase="converting",
                message=f"Slide {cur} of {tot}",
            )

        try:
            store.update(
                job_id,
                status=JobStatus.running,
                progress=3,
                phase="preparing",
                message="Counting pages…",
            )
            total = count_pages(input_path)
            indices = resolve_pages(
                total_pages=total,
                pages=pages,
                max_total=settings.max_pptx_total_pages,
                label="PDF → PowerPoint",
            )
            store.update(
                job_id,
                total=len(indices),
                progress=5,
                phase="converting",
                message=f"Building {len(indices)} slides…",
            )
            if cancelled():
                raise RuntimeError("cancelled")

            pdf_to_pptx(
                input_path,
                output_pptx,
                indices,
                on_progress=on_prog,
                should_cancel=cancelled,
            )
            if cancelled():
                raise RuntimeError("cancelled")

            base_name = Path(name).stem
            if total > 0 and len(indices) < total:
                fname = f"{base_name}_p{indices[0] + 1}-{indices[-1] + 1}.pptx"
            else:
                fname = f"{base_name}.pptx"
            label = pages_label(indices, total, chunks=1)
            store.update(
                job_id,
                status=JobStatus.completed,
                progress=100,
                phase="done",
                message=f"Done — {label}",
                current=len(indices),
                total=len(indices),
                result_path=output_pptx,
                result_name=fname,
                media_type=media_type_for_ext("pptx"),
            )
            j = store.get(job_id)
            if j:
                j.meta["pages_label"] = label
                j.meta["engine"] = "pdf-image-slides"
        except RuntimeError as exc:
            if str(exc) == "cancelled":
                store.update(
                    job_id,
                    status=JobStatus.cancelled,
                    phase="cancelled",
                    message="Cancelled",
                )
                safe_unlink(output_pptx)
            else:
                store.update(
                    job_id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc),
                    error=str(exc),
                )
                safe_unlink(output_pptx)
        except Exception as exc:
            logger.exception("pptx job %s failed", job_id)
            detail = getattr(exc, "detail", None) or str(exc) or "Conversion failed"
            if not isinstance(detail, str):
                detail = str(detail)
            store.update(
                job_id,
                status=JobStatus.failed,
                phase="error",
                message=detail,
                error=detail,
            )
            safe_unlink(output_pptx)

    def _run_office_format_job(
        job_id: str,
        store: JobStore,
        settings: Settings,
        *,
        libreoffice_path: str,
        input_path: str,
        name: str,
        target: str,
    ) -> None:
        temp_dir = make_temp_dir(settings)
        job = store.get(job_id)
        if job:
            job.cleanup_paths.extend([input_path, temp_dir])

        def cancelled() -> bool:
            j = store.get(job_id)
            return bool(j and j.cancelled())

        try:
            store.update(
                job_id,
                status=JobStatus.running,
                progress=10,
                phase="converting",
                message=f"LibreOffice → {target}…",
            )
            if cancelled():
                raise RuntimeError("cancelled")

            # Copy input into temp dir with safe name (LO is picky)
            safe_in = os.path.join(temp_dir, name)
            shutil.copy2(input_path, safe_in)

            store.update(job_id, progress=40, message="Running LibreOffice…")
            out_path = libreoffice_convert(
                libreoffice_path=libreoffice_path,
                input_path=safe_in,
                output_dir=temp_dir,
                target_ext=target,
                run_subprocess=run_subprocess,
                settings=settings,
            )
            if cancelled():
                raise RuntimeError("cancelled")

            # Copy result out so we can wipe temp dir later
            final_path = make_temp_path(f".{target}", settings)
            shutil.copy2(out_path, final_path)
            if job:
                job.cleanup_paths.append(final_path)

            base = Path(name).stem
            fname = f"{base}.{target}"
            store.update(
                job_id,
                status=JobStatus.completed,
                progress=100,
                phase="done",
                message=f"Converted to {target.upper()}",
                result_path=final_path,
                result_name=fname,
                media_type=media_type_for_ext(target),
            )
            safe_rmtree(temp_dir)
        except RuntimeError as exc:
            if str(exc) == "cancelled":
                store.update(
                    job_id,
                    status=JobStatus.cancelled,
                    phase="cancelled",
                    message="Cancelled",
                )
            else:
                store.update(
                    job_id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc),
                    error=str(exc),
                )
            safe_rmtree(temp_dir)
        except Exception as exc:
            logger.exception("office format job %s failed", job_id)
            detail = getattr(exc, "detail", None) or str(exc) or "Conversion failed"
            if not isinstance(detail, str):
                detail = str(detail)
            store.update(
                job_id,
                status=JobStatus.failed,
                phase="error",
                message=detail,
                error=detail,
            )
            safe_rmtree(temp_dir)

    @app.post("/api/jobs/convert/to-pptx")
    async def job_convert_to_pptx(
        request: Request,
        file: UploadFile = File(...),
        pages: str = Form(""),
    ):
        """PDF → PowerPoint (one image slide per page). Background job with progress."""
        settings = _settings(request)
        store = _job_store(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        job = store.create(kind="convert_to_pptx", filename=name)
        job.cleanup_paths.append(input_path)
        store.update(job.id, phase="queued", message="Queued…", progress=1)

        async def _runner() -> None:
            try:
                async with _limiter(request).slot(label="conversion job"):
                    await asyncio.to_thread(
                        _run_pptx_job,
                        job.id,
                        store,
                        settings,
                        input_path=input_path,
                        name=name,
                        pages=pages,
                    )
            except HTTPException as exc:
                store.update(
                    job.id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc.detail),
                    error=str(exc.detail),
                )

        asyncio.create_task(_runner())
        return JSONResponse({"job_id": job.id, **job.to_dict()})

    @app.post("/api/jobs/convert/office")
    async def job_convert_office(
        request: Request,
        file: UploadFile = File(...),
        target: str = Form(...),
    ):
        """
        Cross-format Office conversion via LibreOffice, e.g.:
        PPTX → PDF/DOCX, DOCX → PPTX/PDF, XLSX → PDF/PPTX, etc.
        """
        settings = _settings(request)

        target_norm = (target or "").strip().lower().lstrip(".")
        if target_norm not in {"pdf", "docx", "pptx", "xlsx", "odt", "odp", "ods"}:
            raise HTTPException(
                status_code=400,
                detail="Unsupported target. Use pdf, docx, pptx, xlsx, odt, odp, or ods.",
            )

        name, data = await read_upload(
            file,
            settings,
            allowed_extensions=OFFICE_CROSS_CONVERT_EXTENSIONS,
            label="Office document",
        )
        src_ext = Path(name).suffix.lower()
        if src_ext.lstrip(".") == target_norm:
            raise HTTPException(status_code=400, detail="Source and target formats are the same.")
        allowed_targets = OFFICE_FORMAT_MAP.get(src_ext)
        if not allowed_targets or target_norm not in allowed_targets:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot convert {src_ext or 'this file'} → .{target_norm}. "
                    f"Allowed targets: {', '.join(sorted(allowed_targets or [])) or 'none'}."
                ),
            )

        if not settings.enable_libreoffice or not request.app.state.libreoffice_path:
            raise HTTPException(status_code=503, detail="LibreOffice is not available.")

        if src_ext in {".docx", ".xlsx", ".pptx"}:
            assert_zip_safe(data, settings, label=name)

        input_path = write_bytes(data, src_ext or ".bin", settings)
        store = _job_store(request)
        job = store.create(kind="convert_office", filename=name, target=target_norm)
        job.cleanup_paths.append(input_path)
        store.update(job.id, phase="queued", message="Queued…", progress=1)
        lo_path = request.app.state.libreoffice_path

        async def _runner() -> None:
            try:
                async with _limiter(request).slot(label="conversion job"):
                    await asyncio.to_thread(
                        _run_office_format_job,
                        job.id,
                        store,
                        settings,
                        libreoffice_path=lo_path,
                        input_path=input_path,
                        name=name,
                        target=target_norm,
                    )
            except HTTPException as exc:
                store.update(
                    job.id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc.detail),
                    error=str(exc.detail),
                )

        asyncio.create_task(_runner())
        return JSONResponse({"job_id": job.id, **job.to_dict()})

    @app.post("/api/jobs/convert/to-word")
    async def job_convert_to_word(
        request: Request,
        file: UploadFile = File(...),
        pages: str = Form(""),
        mode: str = Form("auto"),
    ):
        settings = _settings(request)
        store = _job_store(request)
        mode_norm = (mode or "auto").strip().lower()
        if mode_norm not in {"auto", "layout", "fast"}:
            raise HTTPException(status_code=400, detail="Invalid mode. Use auto, layout, or fast.")

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        job = store.create(kind="convert_to_word", filename=name)
        job.cleanup_paths.append(input_path)
        store.update(job.id, phase="queued", message="Queued…", progress=1)

        async def _runner() -> None:
            try:
                async with _limiter(request).slot(label="conversion job"):
                    await asyncio.to_thread(
                        _run_word_job,
                        job.id,
                        store,
                        settings,
                        input_path=input_path,
                        name=name,
                        pages=pages,
                        mode=mode_norm,
                    )
            except HTTPException as exc:
                store.update(
                    job.id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc.detail),
                    error=str(exc.detail),
                )

        asyncio.create_task(_runner())
        return JSONResponse({"job_id": job.id, **job.to_dict()})

    @app.post("/api/jobs/convert/to-excel")
    async def job_convert_to_excel(
        request: Request,
        files: List[UploadFile] = File(...),
        pages: str = Form(""),
    ):
        settings = _settings(request)
        if pd is None:
            raise HTTPException(status_code=503, detail="pandas is not installed.")
        store = _job_store(request)
        validate_file_count(len(files), settings)

        items: list[tuple[str, str]] = []
        for upload in files:
            name, data = await read_upload(
                upload, settings, allowed_extensions=PDF_EXTENSIONS
            )
            path = write_bytes(data, ".pdf", settings)
            items.append((name, path))

        job = store.create(kind="convert_to_excel", files=len(items))
        job.cleanup_paths.extend(p for _, p in items)
        store.update(job.id, phase="queued", message="Queued…", progress=1)

        async def _runner() -> None:
            try:
                async with _limiter(request).slot(label="extraction job"):
                    await asyncio.to_thread(
                        _run_excel_job,
                        job.id,
                        store,
                        settings,
                        items=items,
                        pages=pages,
                    )
            except HTTPException as exc:
                store.update(
                    job.id,
                    status=JobStatus.failed,
                    phase="error",
                    message=str(exc.detail),
                    error=str(exc.detail),
                )

        asyncio.create_task(_runner())
        return JSONResponse({"job_id": job.id, **job.to_dict()})

    @app.get("/api/jobs/{job_id}")
    async def job_status(request: Request, job_id: str):
        job = _job_store(request).get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    async def job_cancel(request: Request, job_id: str):
        job = _job_store(request).get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.status not in {JobStatus.queued, JobStatus.running}:
            return {**job.to_dict(), "message": "Job is not running."}
        job.request_cancel()
        return job.to_dict()

    @app.get("/api/jobs/{job_id}/download")
    async def job_download(
        request: Request,
        background: BackgroundTasks,
        job_id: str,
    ):
        store = _job_store(request)
        job = store.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.status != JobStatus.completed or not job.result_path:
            raise HTTPException(
                status_code=409,
                detail=f"Job is not ready (status={job.status.value}).",
            )
        path = job.result_path
        name = job.result_name or "download.bin"
        media = job.media_type or "application/octet-stream"
        # After download, schedule cleanup of this job
        def _cleanup_job() -> None:
            store.discard(job_id)

        background.add_task(_cleanup_job)
        return file_response(
            path,
            filename=name,
            media_type=media,
            background=background,
        )

    # ------------------------------------------------------------------
    # PDF operations
    # ------------------------------------------------------------------

    @app.post("/api/merge")
    async def merge_pdfs(
        request: Request,
        background: BackgroundTasks,
        files: List[UploadFile] = File(...),
    ):
        settings = _settings(request)
        validate_file_count(len(files), settings)

        merger = PdfWriter()
        temps: list[str] = []
        try:
            for upload in files:
                _name, data = await read_upload(
                    upload, settings, allowed_extensions=PDF_EXTENSIONS
                )
                path = write_bytes(data, ".pdf", settings)
                temps.append(path)
                merger.append(path)

            output_path = make_temp_path(".pdf", settings)
            with open(output_path, "wb") as out:
                merger.write(out)
            merger.close()

            schedule_cleanup(background, *temps)
            return file_response(
                output_path,
                filename="merged_document.pdf",
                media_type="application/pdf",
                background=background,
            )
        except HTTPException:
            merger.close()
            for path in temps:
                safe_unlink(path)
            raise
        except Exception as exc:
            merger.close()
            for path in temps:
                safe_unlink(path)
            logger.exception("merge failed")
            raise HTTPException(status_code=500, detail="Failed to merge PDFs.") from exc

    @app.post("/api/compress")
    async def compress_pdf(
        request: Request,
        background: BackgroundTasks,
        files: List[UploadFile] = File(...),
        level: str = Form("medium"),
    ):
        settings = _settings(request)
        validate_file_count(len(files), settings)

        if not settings.enable_ghostscript or not request.app.state.gs_path:
            raise HTTPException(status_code=503, detail="Ghostscript is not available.")

        gs_settings = {
            "high": "/screen",
            "medium": "/ebook",
            "low": "/printer",
        }
        pdf_setting = gs_settings.get(level.lower(), "/ebook")
        processed: list[tuple[str, str]] = []
        inputs: list[str] = []

        try:
            async with _limiter(request).slot(label="compression job"):
                for upload in files:
                    name, data = await read_upload(
                        upload, settings, allowed_extensions=PDF_EXTENSIONS
                    )
                    input_path = write_bytes(data, ".pdf", settings)
                    inputs.append(input_path)
                    output_path = make_temp_path(".pdf", settings)

                    run_subprocess(
                        [
                            request.app.state.gs_path,
                            "-sDEVICE=pdfwrite",
                            "-dCompatibilityLevel=1.4",
                            f"-dPDFSETTINGS={pdf_setting}",
                            "-dNOPAUSE",
                            "-dQUIET",
                            "-dBATCH",
                            f"-sOutputFile={output_path}",
                            input_path,
                        ],
                        settings,
                        label="Ghostscript compression",
                    )
                    processed.append((name, output_path))

            if len(processed) == 1:
                orig_name, out_path = processed[0]
                schedule_cleanup(background, *inputs)
                return file_response(
                    out_path,
                    filename=f"compressed_{orig_name}",
                    media_type="application/pdf",
                    background=background,
                )

            zip_path = make_temp_path(".zip", settings)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for orig_name, out_path in processed:
                    zipf.write(out_path, arcname=f"compressed_{orig_name}")
            schedule_cleanup(background, *inputs, *[p for _, p in processed])
            return file_response(
                zip_path,
                filename="compressed_batch.zip",
                media_type="application/zip",
                background=background,
            )
        except HTTPException:
            for path in inputs:
                safe_unlink(path)
            for _, path in processed:
                safe_unlink(path)
            raise
        except Exception as exc:
            for path in inputs:
                safe_unlink(path)
            for _, path in processed:
                safe_unlink(path)
            logger.exception("compress failed")
            raise HTTPException(status_code=500, detail="Failed to compress files.") from exc

    @app.post("/api/split")
    async def split_pdf(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        pages: str = Form(...),
    ):
        settings = _settings(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)

        try:
            page_indices = parse_page_spec(pages)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid page specification. Use formats like 1,3,5-7.",
            ) from exc

        input_path = write_bytes(data, ".pdf", settings)
        try:
            reader = PdfReader(input_path)
            writer = PdfWriter()
            total_pages = len(reader.pages)
            for idx in sorted(page_indices):
                if 0 <= idx < total_pages:
                    writer.add_page(reader.pages[idx])

            if len(writer.pages) == 0:
                raise HTTPException(status_code=400, detail="No valid pages selected.")

            output_path = make_temp_path(".pdf", settings)
            with open(output_path, "wb") as out:
                writer.write(out)

            return file_response(
                output_path,
                filename=f"extracted_{name}",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
        except HTTPException:
            safe_unlink(input_path)
            raise
        except Exception as exc:
            safe_unlink(input_path)
            logger.exception("split failed")
            raise HTTPException(status_code=500, detail="Failed to extract pages.") from exc

    @app.post("/api/ocr")
    async def ocr_pdf(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
    ):
        settings = _settings(request)
        if not settings.enable_ocr or ocrmypdf is None:
            raise HTTPException(status_code=503, detail="OCR is not available.")

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        output_path = make_temp_path(".pdf", settings)
        try:
            # Cap pages to avoid multi-hour / multi-GB OCR jobs
            try:
                page_count = len(PdfReader(input_path).pages)
            except Exception:
                page_count = 0
            if page_count > settings.max_ocr_pages:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"PDF has {page_count} pages; OCR is limited to "
                        f"{settings.max_ocr_pages} pages. Split the document first."
                    ),
                )

            async with _limiter(request).slot(label="OCR job"):

                def _ocr_job() -> None:
                    run_ocr(
                        input_path,
                        output_path,
                        ocrmypdf_module=ocrmypdf,
                        force=True,
                        deskew=True,
                    )

                await asyncio.to_thread(_ocr_job)

            return file_response(
                output_path,
                filename=f"searchable_{name}",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
        except HTTPException:
            safe_unlink(input_path)
            safe_unlink(output_path)
            raise
        except Exception as exc:
            safe_unlink(input_path)
            safe_unlink(output_path)
            logger.exception("ocr failed")
            raise HTTPException(status_code=500, detail="OCR processing failed.") from exc

    @app.post("/api/protect")
    async def protect_pdf(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        open_password: str = Form(""),
        owner_password: str = Form(""),
        disable_print: bool = Form(False),
        disable_copy: bool = Form(False),
        disable_modify: bool = Form(False),
    ):
        settings = _settings(request)
        open_password = validate_password(
            open_password, settings, field="open_password"
        )
        owner_password = validate_password(
            owner_password, settings, field="owner_password"
        )
        if not open_password and not owner_password:
            raise HTTPException(
                status_code=400,
                detail="Provide at least an open password or owner password.",
            )

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        try:
            reader = PdfReader(input_path)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            # Start with full access, then revoke selected rights
            permissions = -1
            if disable_print:
                permissions &= ~(
                    int(UserAccessPermissions.PRINT)
                    | int(UserAccessPermissions.PRINT_TO_REPRESENTATION)
                )
            if disable_copy:
                permissions &= ~(
                    int(UserAccessPermissions.EXTRACT)
                    | int(UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS)
                )
            if disable_modify:
                permissions &= ~(
                    int(UserAccessPermissions.MODIFY)
                    | int(UserAccessPermissions.ADD_OR_MODIFY)
                    | int(UserAccessPermissions.FILL_FORM_FIELDS)
                    | int(UserAccessPermissions.ASSEMBLE_DOC)
                )

            final_owner = owner_password if owner_password else open_password
            # AES-256 when supported by pypdf
            writer.encrypt(
                user_password=open_password,
                owner_password=final_owner,
                permissions_flag=permissions,
                algorithm="AES-256",
            )

            output_path = make_temp_path(".pdf", settings)
            with open(output_path, "wb") as out:
                writer.write(out)

            return file_response(
                output_path,
                filename=f"protected_{name}",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
        except HTTPException:
            safe_unlink(input_path)
            raise
        except Exception as exc:
            safe_unlink(input_path)
            logger.exception("protect failed")
            raise HTTPException(status_code=500, detail="Failed to protect the PDF.") from exc

    @app.post("/api/convert/to-pdf")
    async def convert_to_pdf(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
    ):
        settings = _settings(request)
        if not settings.enable_libreoffice or not request.app.state.libreoffice_path:
            raise HTTPException(status_code=503, detail="LibreOffice is not available.")

        name, data = await read_upload(
            file, settings, allowed_extensions=OFFICE_TO_PDF_EXTENSIONS, label="Office document"
        )
        # OOXML path safety for modern Office files
        if Path(name).suffix.lower() in {".docx", ".xlsx", ".pptx"}:
            assert_zip_safe(data, settings, label="Office document")

        temp_dir = make_temp_dir(settings)
        try:
            async with _limiter(request).slot(label="conversion job"):
                input_path = os.path.join(temp_dir, name)
                with open(input_path, "wb") as handle:
                    handle.write(data)

                run_subprocess(
                    [
                        request.app.state.libreoffice_path,
                        "--headless",
                        "--nologo",
                        "--nofirststartwizard",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        temp_dir,
                        input_path,
                    ],
                    settings,
                    label="LibreOffice conversion",
                )

                base_name = Path(name).stem
                output_pdf = os.path.join(temp_dir, f"{base_name}.pdf")
                if not os.path.isfile(output_pdf):
                    pdfs = list(Path(temp_dir).glob("*.pdf"))
                    if not pdfs:
                        raise HTTPException(status_code=500, detail="Conversion produced no PDF.")
                    output_pdf = str(pdfs[0])

                final_path = make_temp_path(".pdf", settings)
                shutil.copy2(output_pdf, final_path)

            schedule_cleanup(background, temp_dir)
            return file_response(
                final_path,
                filename=f"{base_name}.pdf",
                media_type="application/pdf",
                background=background,
            )
        except HTTPException:
            safe_rmtree(temp_dir)
            raise
        except Exception as exc:
            safe_rmtree(temp_dir)
            logger.exception("convert to pdf failed")
            raise HTTPException(status_code=500, detail="Failed to convert to PDF.") from exc

    @app.post("/api/convert/to-word")
    async def convert_to_word(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        pages: str = Form(""),
        mode: str = Form("auto"),
    ):
        """
        Convert PDF → Word as one file.

        Empty ``pages`` = entire document. Large files are processed in automatic
        chunks and merged back into a single .docx.

        - mode=auto: layout for small docs, fast text for large ones
        - mode=layout: preserve layout (chunked + merge; slower)
        - mode=fast: text extraction (best for long PDFs)
        """
        settings = _settings(request)
        mode_norm = (mode or "auto").strip().lower()
        if mode_norm not in {"auto", "layout", "fast"}:
            raise HTTPException(
                status_code=400,
                detail="Invalid mode. Use auto, layout, or fast.",
            )

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        output_docx = make_temp_path(".docx", settings)
        cleanup: list[str | None] = [input_path]
        try:
            total = await asyncio.to_thread(count_pages, input_path)
            indices = resolve_pages(
                total_pages=total,
                pages=pages,
                max_total=settings.max_word_total_pages,
                label="PDF → Word",
            )

            logger.info(
                "to-word file=%s total=%s pages=%s mode=%s",
                name,
                total,
                len(indices),
                mode_norm,
            )

            async with _limiter(request).slot(label="conversion job"):

                def _word_job() -> dict:
                    return convert_pdf_to_word(
                        input_path,
                        output_docx,
                        indices,
                        mode=mode_norm,
                        layout_threshold=settings.word_layout_page_threshold,
                        chunk_size=settings.word_chunk_pages,
                        converter_cls=Converter,
                        enable_ocr=settings.enable_ocr and ocrmypdf is not None,
                        ocrmypdf_module=ocrmypdf,
                        make_temp=lambda s: make_temp_path(s, settings),
                        max_ocr_pages=settings.max_auto_ocr_pages,
                    )

                meta = await asyncio.to_thread(_word_job)

            chunks_used = int(meta.get("chunks") or 1)
            engine = str(meta.get("engine") or "fast")
            # Header stays simple for tests: layout | fast
            engine_header = "layout" if engine.startswith("layout") else "fast"
            label = pages_label(indices, total, chunks=chunks_used)

            base_name = Path(name).stem
            if total > 0 and len(indices) < total:
                fname = f"{base_name}_p{indices[0] + 1}-{indices[-1] + 1}.docx"
            else:
                fname = f"{base_name}.docx"

            resp = file_response(
                output_docx,
                filename=fname,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                background=background,
                extra_cleanup=cleanup,
            )
            resp.headers["X-Pages-Processed"] = str(len(indices))
            resp.headers["X-Pages-Total"] = str(total)
            resp.headers["X-Convert-Engine"] = engine_header
            resp.headers["X-Convert-Detail"] = engine
            resp.headers["X-Chunks"] = str(chunks_used)
            resp.headers["X-Pages-Label"] = label
            return resp
        except HTTPException:
            safe_unlink(input_path)
            safe_unlink(output_docx)
            raise
        except Exception as exc:
            safe_unlink(input_path)
            safe_unlink(output_docx)
            logger.exception("convert to word failed")
            raise HTTPException(status_code=500, detail="Failed to convert PDF to Word.") from exc

    @app.post("/api/convert/to-excel")
    async def convert_to_excel(
        request: Request,
        background: BackgroundTasks,
        files: List[UploadFile] = File(...),
        pages: str = Form(""),
    ):
        """
        Extract tables from PDFs into one Excel file.

        Empty ``pages`` = entire document, scanned in automatic page chunks and
        merged into a single workbook. Optional ``pages`` limits the scope.
        """
        settings = _settings(request)
        if pd is None:
            raise HTTPException(status_code=503, detail="pandas is not installed.")

        validate_file_count(len(files), settings)
        excel_files: list[tuple[str, str]] = []
        temps: list[str] = []
        last_label = ""
        last_engine = ""

        try:
            async with _limiter(request).slot(label="extraction job"):
                for upload in files:
                    name, data = await read_upload(
                        upload, settings, allowed_extensions=PDF_EXTENSIONS
                    )
                    input_path = write_bytes(data, ".pdf", settings)
                    temps.append(input_path)

                    total = await asyncio.to_thread(count_pages, input_path)
                    indices = resolve_pages(
                        total_pages=total,
                        pages=pages,
                        max_total=settings.max_excel_total_pages,
                        label="PDF → Excel",
                    )
                    n_chunks = max(
                        1,
                        (len(indices) + settings.excel_chunk_pages - 1)
                        // settings.excel_chunk_pages,
                    )
                    last_label = pages_label(indices, total, chunks=n_chunks)
                    logger.info(
                        "to-excel file=%s total=%s pages=%s chunks=%s",
                        name,
                        total,
                        len(indices),
                        n_chunks,
                    )

                    out_path = make_temp_path(".xlsx", settings)
                    temps.append(out_path)

                    def _excel_job(
                        src: str = input_path,
                        dest: str = out_path,
                        idxs: list[int] = indices,
                    ) -> dict:
                        return convert_pdf_to_excel(
                            src,
                            dest,
                            idxs,
                            pd_module=pd,
                            chunk_size=settings.excel_chunk_pages,
                            camelot_module=camelot,
                            enable_camelot=settings.enable_camelot and camelot is not None,
                            enable_ocr=settings.enable_ocr and ocrmypdf is not None,
                            ocrmypdf_module=ocrmypdf,
                            make_temp=lambda s: make_temp_path(s, settings),
                            max_ocr_pages=settings.max_auto_ocr_pages,
                        )

                    try:
                        meta = await asyncio.to_thread(_excel_job)
                        last_engine = str(meta.get("engine") or "")
                        stem = Path(name).stem
                        if total > 0 and len(indices) < total:
                            out_name = f"{stem}_p{indices[0] + 1}-{indices[-1] + 1}.xlsx"
                        else:
                            out_name = f"{stem}.xlsx"
                        excel_files.append((out_name, out_path))
                    except HTTPException:
                        raise
                    except Exception as exc:
                        logger.info("excel conversion failed for %s: %s", name, exc)

            if not excel_files:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "No readable tables or text found. "
                        "Try running OCR first for scanned PDFs, or pick pages with tables."
                    ),
                )

            if len(excel_files) == 1:
                schedule_cleanup(background, *temps)
                resp = file_response(
                    excel_files[0][1],
                    filename=excel_files[0][0],
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    background=background,
                )
                if last_label:
                    resp.headers["X-Pages-Label"] = last_label
                if last_engine:
                    resp.headers["X-Convert-Engine"] = last_engine
                return resp

            zip_path = make_temp_path(".zip", settings)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for arcname, file_path in excel_files:
                    zipf.write(file_path, arcname=arcname)
            schedule_cleanup(
                background, *temps, *[path for _, path in excel_files]
            )
            return file_response(
                zip_path,
                filename="Batch_Excel_Extraction.zip",
                media_type="application/zip",
                background=background,
            )
        except HTTPException:
            for path in temps:
                safe_unlink(path)
            for _, path in excel_files:
                safe_unlink(path)
            raise
        except Exception as exc:
            for path in temps:
                safe_unlink(path)
            for _, path in excel_files:
                safe_unlink(path)
            logger.exception("convert to excel failed")
            raise HTTPException(status_code=500, detail="Failed to batch convert.") from exc

    @app.post("/api/watermark")
    async def watermark_pdf(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        text: str = Form(...),
    ):
        settings = _settings(request)
        watermark_text = (text or "").strip()
        if not watermark_text:
            raise HTTPException(status_code=400, detail="Watermark text is required.")
        if len(watermark_text) > 120:
            raise HTTPException(status_code=400, detail="Watermark text is too long (max 120).")

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        try:
            reader = PdfReader(input_path)
            writer = PdfWriter()

            packet = io.BytesIO()
            can = canvas.Canvas(packet)
            can.setFont("Helvetica-Bold", 72)
            can.setFillColorRGB(0.5, 0.5, 0.5, alpha=0.3)
            can.translate(300, 400)
            can.rotate(45)
            can.drawCentredString(0, 0, watermark_text)
            can.save()
            packet.seek(0)
            watermark = PdfReader(packet)

            for page in reader.pages:
                page.merge_page(watermark.pages[0])
                writer.add_page(page)

            output_path = make_temp_path(".pdf", settings)
            with open(output_path, "wb") as out:
                writer.write(out)

            base_name = Path(name).stem
            return file_response(
                output_path,
                filename=f"watermarked_{base_name}.pdf",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
        except Exception as exc:
            safe_unlink(input_path)
            logger.exception("watermark failed")
            raise HTTPException(status_code=500, detail="Failed to add watermark.") from exc

    @app.post("/api/scrub")
    async def scrub_metadata(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
    ):
        settings = _settings(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        try:
            reader = PdfReader(input_path)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            writer.add_metadata({})

            output_path = make_temp_path(".pdf", settings)
            with open(output_path, "wb") as out:
                writer.write(out)

            base_name = Path(name).stem
            return file_response(
                output_path,
                filename=f"scrubbed_{base_name}.pdf",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
        except Exception as exc:
            safe_unlink(input_path)
            logger.exception("scrub failed")
            raise HTTPException(status_code=500, detail="Failed to scrub metadata.") from exc

    @app.post("/api/preview")
    async def preview_pdf(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        page_start: int = Form(0),
    ):
        settings = _settings(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        try:
            is_locked = False
            try:
                reader = PdfReader(input_path)
                if reader.is_encrypted:
                    try:
                        result = reader.decrypt("")
                        is_locked = result == 0
                    except Exception:
                        is_locked = True
            except Exception:
                is_locked = True

            if is_locked:
                safe_unlink(input_path)
                return JSONResponse(
                    content={
                        "filename": name,
                        "total_pages": 0,
                        "preview_images": [],
                        "current_start": 0,
                        "current_end": 0,
                        "has_more": False,
                        "is_encrypted": True,
                    }
                )

            page_start = max(0, int(page_start))
            preview_images: list[str] = []
            # Prefer PyMuPDF for page count + raster (much faster on huge PDFs)
            total_pages = 0
            start = 0
            end = 0
            try:
                import fitz

                doc = fitz.open(input_path)
                try:
                    total_pages = doc.page_count
                    # Cap how far into a huge doc the user can page the preview UI
                    hard_cap = max(settings.max_total_preview_pages, settings.max_preview_pages)
                    if page_start >= hard_cap:
                        page_start = max(0, hard_cap - settings.max_preview_pages)
                    start = min(page_start, total_pages)
                    end = min(start + settings.max_preview_pages, total_pages, hard_cap)
                    # Never refuse large PDFs — only preview a window of pages
                    for i in range(start, end):
                        page = doc[i]
                        # Lower DPI for speed on big files
                        zoom = 1.2 if total_pages > 200 else 1.5
                        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                        encoded = base64.b64encode(pix.tobytes("jpeg")).decode("ascii")
                        preview_images.append(f"data:image/jpeg;base64,{encoded}")
                finally:
                    doc.close()
            except Exception as fitz_exc:
                logger.info("PyMuPDF preview fallback to pdfplumber: %s", fitz_exc)
                with pdfplumber.open(input_path) as pdf:
                    total_pages = len(pdf.pages)
                    hard_cap = max(settings.max_total_preview_pages, settings.max_preview_pages)
                    if page_start >= hard_cap:
                        page_start = max(0, hard_cap - settings.max_preview_pages)
                    start = min(page_start, total_pages)
                    end = min(start + settings.max_preview_pages, total_pages, hard_cap)
                    for i in range(start, end):
                        page = pdf.pages[i]
                        img = page.to_image(resolution=100)
                        buffer = io.BytesIO()
                        img.original.save(buffer, format="JPEG", quality=75, optimize=True)
                        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                        preview_images.append(f"data:image/jpeg;base64,{encoded}")

            safe_unlink(input_path)
            return JSONResponse(
                content={
                    "filename": name,
                    "total_pages": total_pages,
                    "preview_images": preview_images,
                    "current_start": start,
                    "current_end": end,
                    "has_more": end < min(total_pages, max(settings.max_total_preview_pages, settings.max_preview_pages)),
                    "is_encrypted": False,
                    "preview_capped": total_pages
                    > max(settings.max_total_preview_pages, settings.max_preview_pages),
                }
            )
        except HTTPException:
            safe_unlink(input_path)
            raise
        except Exception as exc:
            safe_unlink(input_path)
            logger.exception("preview failed")
            raise HTTPException(status_code=500, detail="Failed to generate preview.") from exc

    @app.post("/api/modify-pages")
    async def modify_pages(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        modifications: str = Form(...),
    ):
        settings = _settings(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)

        try:
            mods = json.loads(modifications)
            if not isinstance(mods, dict):
                raise ValueError("modifications must be an object")
            rotations = mods.get("rotations", {}) or {}
            deletions = set(mods.get("deletions", []) or [])
            if len(deletions) > 10_000 or len(rotations) > 10_000:
                raise ValueError("too many modifications")
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid modifications payload.") from exc

        input_path = write_bytes(data, ".pdf", settings)
        try:
            reader = PdfReader(input_path)
            writer = PdfWriter()
            for i, page in enumerate(reader.pages):
                if i in deletions:
                    continue
                if str(i) in rotations:
                    angle = int(rotations[str(i)]) % 360
                    if angle != 0:
                        page.rotate(angle)
                writer.add_page(page)

            if len(writer.pages) == 0:
                raise HTTPException(status_code=400, detail="Cannot delete all pages.")

            output_path = make_temp_path(".pdf", settings)
            with open(output_path, "wb") as out:
                writer.write(out)

            base_name = Path(name).stem
            return file_response(
                output_path,
                filename=f"modified_{base_name}.pdf",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
        except HTTPException:
            safe_unlink(input_path)
            raise
        except Exception as exc:
            safe_unlink(input_path)
            logger.exception("modify pages failed")
            raise HTTPException(status_code=500, detail="Failed to modify pages.") from exc

    @app.post("/api/preview-merge")
    async def preview_merge(
        request: Request,
        files: List[UploadFile] = File(...),
    ):
        settings = _settings(request)
        validate_file_count(len(files), settings)

        preview_images = []
        temps: list[str] = []
        try:
            for upload in files[:20]:
                name, data = await read_upload(
                    upload, settings, allowed_extensions=PDF_EXTENSIONS
                )
                input_path = write_bytes(data, ".pdf", settings)
                temps.append(input_path)
                with pdfplumber.open(input_path) as pdf:
                    if len(pdf.pages) > 0:
                        img = pdf.pages[0].to_image(resolution=48)
                        buffer = io.BytesIO()
                        img.original.save(buffer, format="JPEG", quality=70, optimize=True)
                        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                        preview_images.append(
                            {
                                "filename": name,
                                "image": f"data:image/jpeg;base64,{encoded}",
                            }
                        )
            return JSONResponse(content={"previews": preview_images})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("merge preview failed")
            raise HTTPException(status_code=500, detail="Failed to preview merge files.") from exc
        finally:
            for path in temps:
                safe_unlink(path)

    @app.post("/api/unlock")
    async def unlock_pdf(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        password: str = Form(...),
    ):
        settings = _settings(request)
        password = validate_password(password, settings, required=True, field="password")

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        try:
            reader = PdfReader(input_path)
            if not reader.is_encrypted:
                raise HTTPException(status_code=400, detail="This PDF is not encrypted.")

            decrypted = reader.decrypt(password)
            if decrypted == 0:
                raise HTTPException(status_code=401, detail="Incorrect password.")

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            output_path = make_temp_path(".pdf", settings)
            with open(output_path, "wb") as out:
                writer.write(out)

            base_name = Path(name).stem
            return file_response(
                output_path,
                filename=f"unlocked_{base_name}.pdf",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
        except HTTPException:
            safe_unlink(input_path)
            raise
        except Exception as exc:
            safe_unlink(input_path)
            logger.exception("unlock failed")
            raise HTTPException(status_code=500, detail="Failed to unlock PDF.") from exc

    @app.post("/api/compress-office")
    async def compress_office(
        request: Request,
        background: BackgroundTasks,
        files: List[UploadFile] = File(...),
        level: str = Form("medium"),
    ):
        """
        Aggressively shrink .docx/.pptx by re-encoding embedded images and
        repacking the OOXML zip at max DEFLATE.
        """
        settings = _settings(request)
        validate_file_count(len(files), settings)
        level_norm = (level or "medium").strip().lower()
        if level_norm not in {"low", "medium", "high"}:
            level_norm = "medium"

        processed: list[tuple[str, str, dict]] = []

        try:
            for upload in files:
                name, data = await read_upload(
                    upload,
                    settings,
                    allowed_extensions=OFFICE_COMPRESS_EXTENSIONS,
                    label="Office document",
                )
                assert_zip_safe(data, settings, label=name)
                ext = Path(name).suffix.lower()

                compressed, stats = await asyncio.to_thread(
                    compress_ooxml, data, level=level_norm
                )
                out_path = write_bytes(compressed, ext, settings)
                processed.append((name, out_path, stats))
                logger.info(
                    "office compress %s: %s → %s bytes (%.1f%% saved, %s images)",
                    name,
                    stats["original_bytes"],
                    stats["compressed_bytes"],
                    stats["saved_percent"],
                    stats["images_touched"],
                )

            if len(processed) == 1:
                name, out_path, stats = processed[0]
                schedule_cleanup(background, out_path)
                resp = file_response(
                    out_path,
                    filename=f"compressed_{name}",
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    if name.lower().endswith(".docx")
                    else "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    background=background,
                )
                resp.headers["X-Original-Bytes"] = str(stats["original_bytes"])
                resp.headers["X-Compressed-Bytes"] = str(stats["compressed_bytes"])
                resp.headers["X-Saved-Percent"] = str(stats["saved_percent"])
                resp.headers["X-Images-Touched"] = str(stats["images_touched"])
                return resp

            zip_path = make_temp_path(".zip", settings)
            with zipfile.ZipFile(
                zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as zipf:
                for orig_name, out_path, _stats in processed:
                    zipf.write(out_path, arcname=f"compressed_{orig_name}")

            total_before = sum(s["original_bytes"] for _, _, s in processed)
            total_after = sum(s["compressed_bytes"] for _, _, s in processed)
            schedule_cleanup(background, *[p for _, p, _ in processed])
            resp = file_response(
                zip_path,
                filename="Compressed_Office_Batch.zip",
                media_type="application/zip",
                background=background,
            )
            resp.headers["X-Original-Bytes"] = str(total_before)
            resp.headers["X-Compressed-Bytes"] = str(total_after)
            if total_before:
                resp.headers["X-Saved-Percent"] = str(
                    round((1 - total_after / total_before) * 100, 1)
                )
            return resp
        except HTTPException:
            for _, path, _ in processed:
                safe_unlink(path)
            raise
        except Exception as exc:
            for _, path, _ in processed:
                safe_unlink(path)
            logger.exception("office compress failed")
            raise HTTPException(
                status_code=500,
                detail="Compression failed. Ensure the file is a valid .docx or .pptx.",
            ) from exc

    @app.post("/api/edit/inspect")
    async def edit_inspect(
        request: Request,
        file: UploadFile = File(...),
        page_index: int = Form(0),
    ):
        """Return page preview + selectable text spans (Adobe-like edit surface)."""
        settings = _settings(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        try:
            page_i = int(page_index)
            if page_i < 0:
                raise HTTPException(status_code=400, detail="page_index must be >= 0")
            info = inspect_page(data, page_index=page_i)
            info["filename"] = name
            return JSONResponse(content=info)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("edit inspect failed")
            raise HTTPException(
                status_code=500, detail="Failed to inspect PDF for editing."
            ) from exc

    @app.post("/api/edit/apply")
    async def edit_apply(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        operations: str = Form(...),
    ):
        """
        Apply real content edits via PyMuPDF:
        replace / redact / add_text / whiteout
        """
        settings = _settings(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)

        try:
            ops = json.loads(operations)
            if not isinstance(ops, list):
                raise ValueError("operations must be a list")
            if len(ops) > 500:
                raise HTTPException(status_code=400, detail="Too many operations (max 500).")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid operations payload.") from exc

        if not ops:
            raise HTTPException(status_code=400, detail="No edit operations provided.")

        try:
            result = apply_edits(data, ops)
            output_path = write_bytes(result, ".pdf", settings)
            base_name = Path(name).stem
            return file_response(
                output_path,
                filename=f"edited_{base_name}.pdf",
                media_type="application/pdf",
                background=background,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("edit apply failed")
            raise HTTPException(status_code=500, detail="Failed to apply PDF edits.") from exc

    @app.post("/api/edit-page")
    async def edit_pdf_page_legacy(
        request: Request,
        background: BackgroundTasks,
        file: UploadFile = File(...),
        page_index: int = Form(0),
        actions: str = Form(...),
    ):
        """Backward-compatible stamper API → mapped onto real content editor."""
        settings = _settings(request)
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)

        try:
            edits = json.loads(actions)
            if not isinstance(edits, list):
                raise ValueError("actions must be a list")
            if len(edits) > 500:
                raise ValueError("too many actions")
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid actions payload.") from exc

        page_i = int(page_index)
        ops: list[dict] = []
        for act in edits:
            if not isinstance(act, dict):
                continue
            typ = act.get("type")
            if typ == "whiteout":
                x0 = float(act.get("pctX", 0))
                y0 = float(act.get("pctY", 0))
                w = float(act.get("pctW", 0))
                h = float(act.get("pctH", 0))
                ops.append(
                    {
                        "type": "redact",
                        "page": page_i,
                        "bbox": {"x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h},
                    }
                )
            elif typ == "text":
                ops.append(
                    {
                        "type": "add_text",
                        "page": page_i,
                        "x": float(act.get("pctX", 0)),
                        "y": float(act.get("pctY", 0)),
                        "text": str(act.get("text", ""))[:2000],
                        "size": float(act.get("size", 12)),
                    }
                )

        if not ops:
            raise HTTPException(status_code=400, detail="No valid actions.")

        try:
            result = apply_edits(data, ops)
            output_path = write_bytes(result, ".pdf", settings)
            base_name = Path(name).stem
            return file_response(
                output_path,
                filename=f"edited_{base_name}.pdf",
                media_type="application/pdf",
                background=background,
            )
        except Exception as exc:
            logger.exception("legacy edit-page failed")
            raise HTTPException(status_code=500, detail="Failed to bake edits into PDF.") from exc

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "development",
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )
