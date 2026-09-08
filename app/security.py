"""Input validation and path-safety helpers."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.config import Settings

# Dangerous path / control characters
_UNSAFE_NAME = re.compile(r"[^\w.\- ()[\]]+", re.UNICODE)

PDF_EXTENSIONS = {".pdf"}
OFFICE_TO_PDF_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".odp", ".ods"}
OFFICE_CROSS_CONVERT_EXTENSIONS = OFFICE_TO_PDF_EXTENSIONS
OFFICE_COMPRESS_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
ALLOWED_UPLOAD_EXTENSIONS = PDF_EXTENSIONS | OFFICE_TO_PDF_EXTENSIONS

# OOXML packages are ZIP files
_ZIP_MAGIC = b"PK"
_PDF_MAGIC = b"%PDF"


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


def validate_password(
    password: str | None,
    settings: Settings,
    *,
    required: bool = False,
    field: str = "password",
) -> str:
    """Normalize and bound password length (mitigates DoS / memory abuse)."""
    value = password if password is not None else ""
    if isinstance(value, str):
        value = value  # keep as-is; do not strip internal spaces
    else:
        value = str(value)

    if not value:
        if required:
            raise HTTPException(status_code=400, detail=f"{field} is required.")
        return ""

    if len(value) > settings.max_password_length:
        raise HTTPException(
            status_code=400,
            detail=f"{field} is too long (max {settings.max_password_length} characters).",
        )
    return value


def parse_page_spec(pages: str) -> set[int]:
    """
    Parse page specs like '1,3,5-7' into 0-based page indices.
    Raises ValueError on bad input.
    """
    page_indices: set[int] = set()
    for part in pages.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start > end or start < 1:
                raise ValueError("Invalid page range")
            if end - start > 50_000:
                raise ValueError("Page range too large")
            for p in range(start, end + 1):
                page_indices.add(p - 1)
        else:
            page = int(part)
            if page < 1:
                raise ValueError("Invalid page number")
            page_indices.add(page - 1)
    if not page_indices:
        raise ValueError("No pages specified")
    if len(page_indices) > 10_000:
        raise ValueError("Too many pages selected")
    return page_indices


def is_safe_zip_member(name: str) -> bool:
    """Reject absolute paths and path traversal inside ZIP members."""
    member = Path(name).as_posix()
    if not member or member.endswith("/"):
        return True  # directory entries ok to skip later
    if member.startswith("/") or member.startswith("\\"):
        return False
    if ".." in member.split("/"):
        return False
    # Windows drive letters
    if len(member) >= 2 and member[1] == ":":
        return False
    return True


def assert_zip_safe(data: bytes, settings: Settings, *, label: str = "archive") -> None:
    """
    Lightweight zip-bomb / path-traversal guard using central directory metadata
    (does not fully expand).
    """
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            infos = zf.infolist()
            if len(infos) > settings.max_zip_entries:
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} has too many entries (max {settings.max_zip_entries}).",
                )
            total = 0
            for info in infos:
                if not is_safe_zip_member(info.filename):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{label} contains an unsafe path entry.",
                    )
                if info.is_dir():
                    continue
                total += max(0, int(info.file_size))
                if total > settings.max_zip_uncompressed_bytes:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{label} uncompressed size exceeds safety limit.",
                    )
    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid {label} (not a valid ZIP/OOXML)."
        ) from exc


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

    # Light magic-byte checks
    if allowed_extensions is not None:
        if allowed_extensions <= PDF_EXTENSIONS:
            if not data.lstrip().startswith(_PDF_MAGIC):
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{safe_name}' does not appear to be a valid PDF.",
                )
        elif allowed_extensions <= OFFICE_COMPRESS_EXTENSIONS or (
            allowed_extensions <= OFFICE_TO_PDF_EXTENSIONS
            and Path(safe_name).suffix.lower() in {".docx", ".xlsx", ".pptx"}
        ):
            if not data.startswith(_ZIP_MAGIC):
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{safe_name}' does not appear to be a valid Office document.",
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
