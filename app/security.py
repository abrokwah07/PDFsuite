"""Input validation and path-safety helpers."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import Settings

# Dangerous path / control characters
_UNSAFE_NAME = re.compile(r"[^\w.\- ()[\]]+", re.UNICODE)

PDF_EXTENSIONS = {".pdf"}
OFFICE_TO_PDF_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
OFFICE_COMPRESS_EXTENSIONS = {".docx", ".pptx"}
ALLOWED_UPLOAD_EXTENSIONS = PDF_EXTENSIONS | OFFICE_TO_PDF_EXTENSIONS


def sanitize_filename(name: str | None, default: str = "document") -> str:
    """Return a basename-only, path-traversal-safe filename."""
    if not name or not str(name).strip():
        return default

    # Drop any directory components (path traversal)
    base = Path(str(name)).name.strip()
    if not base or base in {".", ".."}:
        return default

    # Collapse whitespace and strip risky characters
    base = base.replace("\x00", "")
    cleaned = _UNSAFE_NAME.sub("_", base).strip(" ._")
    if not cleaned:
        return default

    # Cap length while preserving extension when possible
    if len(cleaned) > 180:
        stem = Path(cleaned).stem[:150]
        suffix = Path(cleaned).suffix[:20]
        cleaned = f"{stem}{suffix}" if suffix else stem[:180]

    return cleaned


def require_extension(filename: str, allowed: set[str], label: str = "file") -> str:
    ext = Path(filename).suffix.lower()
    if ext not in allowed:
        allowed_fmt = ", ".join(sorted(allowed))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported {label} type '{ext or '(none)'}'. Allowed: {allowed_fmt}",
        )
    return ext


async def read_upload(
    upload: UploadFile,
    settings: Settings,
    *,
    allowed_extensions: set[str] | None = None,
    label: str = "file",
) -> tuple[str, bytes]:
    """Validate and read an uploaded file into memory with size limits."""
    safe_name = sanitize_filename(upload.filename)
    if allowed_extensions is not None:
        require_extension(safe_name, allowed_extensions, label=label)

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_upload_bytes:
            max_mb = settings.max_upload_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"File '{safe_name}' exceeds the maximum size of {max_mb} MB.",
            )
        chunks.append(chunk)

    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail=f"Uploaded {label} is empty.")

    # Light magic-byte checks for PDF
    if allowed_extensions and allowed_extensions <= PDF_EXTENSIONS:
        if not data.lstrip().startswith(b"%PDF"):
            raise HTTPException(
                status_code=400,
                detail=f"File '{safe_name}' does not appear to be a valid PDF.",
            )

    return safe_name, data


def validate_file_count(count: int, settings: Settings) -> None:
    if count < 1:
        raise HTTPException(status_code=400, detail="At least one file is required.")
    if count > settings.max_files_per_request:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum is {settings.max_files_per_request}.",
        )
