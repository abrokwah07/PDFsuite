"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Enterprise-oriented defaults for Local PDF Suite."""

    app_name: str = "Local PDF Suite"
    app_version: str = "1.1.0"
    environment: str = "production"

    # Network
    host: str = "0.0.0.0"
    port: int = 8000

    # Security / capacity
    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB per file
    max_files_per_request: int = 50
    max_preview_pages: int = 30
    max_total_preview_pages: int = 500
    subprocess_timeout_seconds: int = 180
    allow_origins: tuple[str, ...] = ("*",)

    # Paths
    base_dir: Path = Path(__file__).resolve().parent.parent
    index_html: Path = base_dir / "index.html"
    temp_dir: Path | None = None

    # Feature flags
    enable_ocr: bool = True
    enable_libreoffice: bool = True
    enable_ghostscript: bool = True
    enable_camelot: bool = True

    log_level: str = "INFO"
    request_id_header: str = "X-Request-ID"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    origins = os.getenv("ALLOW_ORIGINS", "*")
    origin_list = tuple(o.strip() for o in origins.split(",") if o.strip()) or ("*",)

    temp_raw = os.getenv("TEMP_DIR", "").strip()
    temp_dir = Path(temp_raw) if temp_raw else None

    return Settings(
        app_name=os.getenv("APP_NAME", "Local PDF Suite"),
        app_version=os.getenv("APP_VERSION", "1.1.0"),
        environment=os.getenv("APP_ENV", "production"),
        host=os.getenv("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000),
        max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
        max_files_per_request=_env_int("MAX_FILES_PER_REQUEST", 50),
        max_preview_pages=_env_int("MAX_PREVIEW_PAGES", 30),
        max_total_preview_pages=_env_int("MAX_TOTAL_PREVIEW_PAGES", 500),
        subprocess_timeout_seconds=_env_int("SUBPROCESS_TIMEOUT_SECONDS", 180),
        allow_origins=origin_list,
        temp_dir=temp_dir,
        enable_ocr=_env_bool("ENABLE_OCR", True),
        enable_libreoffice=_env_bool("ENABLE_LIBREOFFICE", True),
        enable_ghostscript=_env_bool("ENABLE_GHOSTSCRIPT", True),
        enable_camelot=_env_bool("ENABLE_CAMELOT", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        request_id_header=os.getenv("REQUEST_ID_HEADER", "X-Request-ID"),
    )
