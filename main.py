"""
Local PDF Suite — FastAPI backend.

All document processing runs locally. Uploads are size-checked, filenames are
sanitized, subprocesses are timed out, and temporary files are cleaned up.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions
from reportlab.pdfgen import canvas
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings, get_settings
from app.security import (
    OFFICE_COMPRESS_EXTENSIONS,
    OFFICE_TO_PDF_EXTENSIONS,
    PDF_EXTENSIONS,
    read_upload,
    sanitize_filename,
    validate_file_count,
)
from app.pdf_editor import apply_edits, inspect_page
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


def _run_subprocess(
    command: list[str],
    settings: Settings,
    *,
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.subprocess_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("%s timed out after %ss", label, settings.subprocess_timeout_seconds)
        raise HTTPException(
            status_code=504,
            detail=f"{label} timed out. Try a smaller file or raise SUBPROCESS_TIMEOUT_SECONDS.",
        ) from exc
    except FileNotFoundError as exc:
        logger.error("%s binary missing: %s", label, command[0])
        raise HTTPException(
            status_code=503,
            detail=f"{label} is not installed on this server.",
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "")[:500]
        logger.error("%s failed: %s", label, stderr)
        raise HTTPException(status_code=500, detail=f"{label} failed.") from exc


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach request IDs, security headers, and structured access logs."""

    async def dispatch(self, request: Request, call_next):
        settings: Settings = request.app.state.settings
        request_id = request.headers.get(settings.request_id_header) or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled error request_id=%s path=%s", request_id, request.url.path)
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        # Local tool: do not allow embedding of third-party scripts beyond our own page.
        # Tailwind CDN is used by index.html; keep a deliberate CSP for that.
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings)
    app.state.settings = settings
    app.state.started_at = time.time()
    app.state.gs_path = _which("gs")
    app.state.libreoffice_path = _which("libreoffice") or _which("soffice")
    app.state.tesseract_path = _which("tesseract")

    logger.info(
        "Starting %s v%s env=%s gs=%s libreoffice=%s tesseract=%s ocrmypdf=%s camelot=%s",
        settings.app_name,
        settings.app_version,
        settings.environment,
        bool(app.state.gs_path),
        bool(app.state.libreoffice_path),
        bool(app.state.tesseract_path),
        ocrmypdf is not None,
        camelot is not None,
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allow_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Content-Disposition"],
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": request_id},
            headers={"X-Request-ID": request_id or ""},
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
        settings: Settings = request.app.state.settings
        if not settings.index_html.is_file():
            raise HTTPException(status_code=500, detail="Frontend index.html is missing.")
        html = settings.index_html.read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    @app.get("/health")
    async def health(request: Request):
        settings: Settings = request.app.state.settings
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
            "uptime_seconds": round(time.time() - request.app.state.started_at, 1),
        }

    @app.get("/ready")
    async def ready(request: Request):
        """Readiness: core Python PDF stack must work; system tools reported."""
        settings: Settings = request.app.state.settings
        checks = {
            "index_html": settings.index_html.is_file(),
            "ghostscript": bool(request.app.state.gs_path) if settings.enable_ghostscript else True,
            "libreoffice": bool(request.app.state.libreoffice_path)
            if settings.enable_libreoffice
            else True,
            "ocrmypdf": (ocrmypdf is not None and bool(request.app.state.tesseract_path))
            if settings.enable_ocr
            else True,
        }
        # Core always required
        core_ok = checks["index_html"]
        status = "ready" if core_ok else "degraded"
        return JSONResponse(
            status_code=200 if core_ok else 503,
            content={"status": status, "checks": checks},
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
        settings: Settings = request.app.state.settings
        validate_file_count(len(files), settings)

        merger = PdfWriter()
        temps: list[str] = []
        try:
            for upload in files:
                name, data = await read_upload(
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
            for path in temps:
                safe_unlink(path)
            raise
        except Exception as exc:
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
        settings: Settings = request.app.state.settings
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
            for upload in files:
                name, data = await read_upload(
                    upload, settings, allowed_extensions=PDF_EXTENSIONS
                )
                input_path = write_bytes(data, ".pdf", settings)
                inputs.append(input_path)
                output_path = make_temp_path(".pdf", settings)

                _run_subprocess(
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
        settings: Settings = request.app.state.settings
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)

        try:
            page_indices: set[int] = set()
            for part in pages.replace(" ", "").split(","):
                if not part:
                    continue
                if "-" in part:
                    start_s, end_s = part.split("-", 1)
                    start, end = int(start_s), int(end_s)
                    if start > end or start < 1:
                        raise ValueError("Invalid page range")
                    for p in range(start, end + 1):
                        page_indices.add(p - 1)
                else:
                    page = int(part)
                    if page < 1:
                        raise ValueError("Invalid page number")
                    page_indices.add(page - 1)
            if not page_indices:
                raise HTTPException(status_code=400, detail="No pages specified.")
        except HTTPException:
            raise
        except Exception as exc:
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
        settings: Settings = request.app.state.settings
        if not settings.enable_ocr or ocrmypdf is None:
            raise HTTPException(status_code=503, detail="OCR is not available.")

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        output_path = make_temp_path(".pdf", settings)
        try:
            ocrmypdf.ocr(input_path, output_path, force_ocr=True, progress_bar=False)
            return file_response(
                output_path,
                filename=f"searchable_{name}",
                media_type="application/pdf",
                background=background,
                extra_cleanup=[input_path],
            )
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
        settings: Settings = request.app.state.settings
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
            writer.encrypt(
                user_password=open_password,
                owner_password=final_owner,
                permissions_flag=permissions,
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
        settings: Settings = request.app.state.settings
        if not settings.enable_libreoffice or not request.app.state.libreoffice_path:
            raise HTTPException(status_code=503, detail="LibreOffice is not available.")

        name, data = await read_upload(
            file, settings, allowed_extensions=OFFICE_TO_PDF_EXTENSIONS, label="Office document"
        )
        temp_dir = make_temp_dir(settings)
        try:
            # Sanitized basename only — prevents path traversal
            input_path = os.path.join(temp_dir, name)
            with open(input_path, "wb") as handle:
                handle.write(data)

            _run_subprocess(
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
                # LibreOffice sometimes normalizes names
                pdfs = list(Path(temp_dir).glob("*.pdf"))
                if not pdfs:
                    raise HTTPException(status_code=500, detail="Conversion produced no PDF.")
                output_pdf = str(pdfs[0])

            # Copy out of the temp dir so we can delete the whole dir after send
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
    ):
        settings: Settings = request.app.state.settings
        if Converter is None:
            raise HTTPException(status_code=503, detail="pdf2docx is not installed.")

        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        output_docx = make_temp_path(".docx", settings)
        try:
            cv = Converter(input_path)
            try:
                cv.convert(output_docx)
            finally:
                cv.close()

            base_name = Path(name).stem
            return file_response(
                output_docx,
                filename=f"{base_name}.docx",
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                background=background,
                extra_cleanup=[input_path],
            )
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
    ):
        settings: Settings = request.app.state.settings
        if pd is None:
            raise HTTPException(status_code=503, detail="pandas is not installed.")

        validate_file_count(len(files), settings)
        excel_files: list[tuple[str, str]] = []
        temps: list[str] = []

        try:
            for upload in files:
                name, data = await read_upload(
                    upload, settings, allowed_extensions=PDF_EXTENSIONS
                )
                input_path = write_bytes(data, ".pdf", settings)
                temps.append(input_path)
                all_tables: list = []

                if settings.enable_camelot and camelot is not None:
                    try:
                        tables = camelot.read_pdf(input_path, pages="all", flavor="stream")
                        for table in tables:
                            if not table.df.empty:
                                all_tables.append(table.df)
                    except Exception as exc:
                        logger.info("Camelot extraction fallback: %s", exc)

                if not all_tables:
                    try:
                        with pdfplumber.open(input_path) as pdf:
                            for page in pdf.pages:
                                tables = page.extract_tables(
                                    {
                                        "vertical_strategy": "text",
                                        "horizontal_strategy": "text",
                                    }
                                )
                                for table in tables or []:
                                    all_tables.append(pd.DataFrame(table))
                    except Exception as exc:
                        logger.info("pdfplumber extraction fallback: %s", exc)

                if not all_tables and settings.enable_ocr and ocrmypdf is not None:
                    ocr_path = make_temp_path(".pdf", settings)
                    temps.append(ocr_path)
                    try:
                        ocrmypdf.ocr(
                            input_path,
                            ocr_path,
                            force_ocr=True,
                            output_type="pdf",
                            deskew=True,
                            progress_bar=False,
                        )
                        with pdfplumber.open(ocr_path) as pdf:
                            settings_tbl = {
                                "vertical_strategy": "text",
                                "horizontal_strategy": "text",
                                "snap_tolerance": 5,
                                "join_tolerance": 5,
                            }
                            for page in pdf.pages:
                                tables = page.extract_tables(settings_tbl)
                                for table in tables or []:
                                    all_tables.append(pd.DataFrame(table))
                    except Exception as exc:
                        logger.info("OCR table rescue failed: %s", exc)

                if all_tables:
                    for i in range(len(all_tables)):
                        all_tables[i].columns = range(all_tables[i].shape[1])
                    master_df = pd.concat(all_tables, ignore_index=True)
                    master_df.dropna(how="all", inplace=True)
                    out_path = make_temp_path(".xlsx", settings)
                    master_df.to_excel(
                        out_path, index=False, header=False, sheet_name="Master_Data"
                    )
                    excel_files.append((f"{Path(name).stem}.xlsx", out_path))

            if not excel_files:
                raise HTTPException(
                    status_code=400, detail="No readable tables found in any files."
                )

            if len(excel_files) == 1:
                schedule_cleanup(background, *temps)
                return file_response(
                    excel_files[0][1],
                    filename=excel_files[0][0],
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    background=background,
                )

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
        settings: Settings = request.app.state.settings
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
        settings: Settings = request.app.state.settings
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
        settings: Settings = request.app.state.settings
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        input_path = write_bytes(data, ".pdf", settings)
        try:
            is_locked = False
            try:
                reader = PdfReader(input_path)
                if reader.is_encrypted:
                    # Try empty password (owner-only encryption)
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
            with pdfplumber.open(input_path) as pdf:
                total_pages = len(pdf.pages)
                if total_pages > settings.max_total_preview_pages:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"PDF has {total_pages} pages; max previewable is "
                            f"{settings.max_total_preview_pages}."
                        ),
                    )

                start = min(page_start, total_pages)
                end = min(start + settings.max_preview_pages, total_pages)

                for i in range(start, end):
                    page = pdf.pages[i]
                    img = page.to_image(resolution=110)
                    buffer = io.BytesIO()
                    img.original.save(buffer, format="JPEG", quality=80, optimize=True)
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
                    "has_more": end < total_pages,
                    "is_encrypted": False,
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
        settings: Settings = request.app.state.settings
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)

        try:
            mods = json.loads(modifications)
            if not isinstance(mods, dict):
                raise ValueError("modifications must be an object")
            rotations = mods.get("rotations", {}) or {}
            deletions = set(mods.get("deletions", []) or [])
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
        settings: Settings = request.app.state.settings
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
        settings: Settings = request.app.state.settings
        if not password:
            raise HTTPException(status_code=400, detail="Password is required.")

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
        settings: Settings = request.app.state.settings
        validate_file_count(len(files), settings)

        compression_settings = {
            "low": {"quality": 85, "max_dim": (1600, 1600)},
            "medium": {"quality": 50, "max_dim": (1024, 1024)},
            "high": {"quality": 15, "max_dim": (600, 600)},
        }
        cfg = compression_settings.get(level.lower(), compression_settings["medium"])
        quality = cfg["quality"]
        max_dim = cfg["max_dim"]
        processed: list[tuple[str, str]] = []

        try:
            for upload in files:
                name, data = await read_upload(
                    upload,
                    settings,
                    allowed_extensions=OFFICE_COMPRESS_EXTENSIONS,
                    label="Office document",
                )
                ext = Path(name).suffix.lower()
                out_zip = make_temp_path(ext, settings)

                with zipfile.ZipFile(io.BytesIO(data), "r") as zin:
                    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zout:
                        for item in zin.infolist():
                            buffer = zin.read(item.filename)
                            # Never allow path traversal inside package members
                            member_name = Path(item.filename).as_posix()
                            if member_name.startswith("/") or ".." in member_name.split("/"):
                                continue

                            filename_lower = member_name.lower()
                            if filename_lower.endswith(
                                (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp")
                            ):
                                try:
                                    img = Image.open(io.BytesIO(buffer))
                                    if img.mode in ("RGBA", "LA") or (
                                        img.mode == "P" and "transparency" in img.info
                                    ):
                                        background_img = Image.new("RGB", img.size, (255, 255, 255))
                                        background_img.paste(
                                            img.convert("RGBA"),
                                            mask=img.convert("RGBA").split()[3],
                                        )
                                        img = background_img
                                    elif img.mode != "RGB":
                                        img = img.convert("RGB")

                                    if hasattr(Image, "Resampling"):
                                        img.thumbnail(max_dim, Image.Resampling.LANCZOS)
                                    else:
                                        img.thumbnail(max_dim, Image.ANTIALIAS)

                                    img_io = io.BytesIO()
                                    img.save(img_io, format="JPEG", quality=quality, optimize=True)
                                    zout.writestr(member_name, img_io.getvalue())
                                except Exception as img_err:
                                    logger.debug(
                                        "Skipping image %s: %s", member_name, img_err
                                    )
                                    zout.writestr(member_name, buffer)
                            else:
                                zout.writestr(member_name, buffer)

                processed.append((name, out_zip))

            zip_path = make_temp_path(".zip", settings)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for orig_name, out_path in processed:
                    zipf.write(out_path, arcname=f"compressed_{orig_name}")

            schedule_cleanup(background, *[p for _, p in processed])
            return file_response(
                zip_path,
                filename="Compressed_Office_Batch.zip",
                media_type="application/zip",
                background=background,
            )
        except HTTPException:
            for _, path in processed:
                safe_unlink(path)
            raise
        except Exception as exc:
            for _, path in processed:
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
        settings: Settings = request.app.state.settings
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)
        try:
            info = inspect_page(data, page_index=int(page_index))
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
        settings: Settings = request.app.state.settings
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
        settings: Settings = request.app.state.settings
        name, data = await read_upload(file, settings, allowed_extensions=PDF_EXTENSIONS)

        try:
            edits = json.loads(actions)
            if not isinstance(edits, list):
                raise ValueError("actions must be a list")
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
                        "text": str(act.get("text", "")),
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
        forwarded_allow_ips="*",
    )
