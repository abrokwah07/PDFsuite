"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env from project root if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Enterprise-oriented defaults for Local PDF Suite."""

    app_name: str = "Local PDF Suite"
    app_version: str = "1.3.1"
    environment: str = "production"

    # Network
    host: str = "127.0.0.1"
    port: int = 8000
    forwarded_allow_ips: str = "127.0.0.1"

    # Security / capacity
    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB per file
    max_files_per_request: int = 50
    max_preview_pages: int = 30
    max_total_preview_pages: int = 500
    max_ocr_pages: int = 200
    # Full-document auto conversion: process in chunks, merge to one file
    word_chunk_pages: int = 40  # layout chunks
    excel_chunk_pages: int = 50
    max_word_total_pages: int = 8000  # hard safety ceiling for one job
    max_excel_total_pages: int = 3000
    max_pptx_total_pages: int = 500  # image slides are heavy on RAM
    # Back-compat aliases used by older code paths / tests
    max_word_pages: int = 8000
    max_excel_pages: int = 3000
    # Layout (pdf2docx) is slow; auto uses fast text above this many pages
    word_layout_page_threshold: int = 40
    max_password_length: int = 128
    max_zip_entries: int = 10_000
    max_zip_uncompressed_bytes: int = 500 * 1024 * 1024  # 500 MB
    max_request_bytes: int = 550 * 1024 * 1024  # hard ceiling on Content-Length
    max_concurrent_jobs: int = 2
    rate_limit_per_minute: int = 120
    subprocess_timeout_seconds: int = 180
    allow_origins: tuple[str, ...] = ("*",)
    trusted_hosts: tuple[str, ...] = ()
    api_key: str | None = None  # if set, required for /api/*

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
    _load_dotenv()

    origins = os.getenv("ALLOW_ORIGINS", "*")
    origin_list = tuple(o.strip() for o in origins.split(",") if o.strip()) or ("*",)

    hosts_raw = os.getenv("TRUSTED_HOSTS", "").strip()
    trusted_hosts = tuple(h.strip() for h in hosts_raw.split(",") if h.strip())

    temp_raw = os.getenv("TEMP_DIR", "").strip()
    temp_dir = Path(temp_raw) if temp_raw else None

    api_key = os.getenv("API_KEY", "").strip() or None

    # Safer default: localhost. Explicit HOST=0.0.0.0 for LAN/Docker.
    default_host = "0.0.0.0" if os.getenv("RUNNING_IN_DOCKER") else "127.0.0.1"

    return Settings(
        app_name=os.getenv("APP_NAME", "Local PDF Suite"),
        app_version=os.getenv("APP_VERSION", "1.3.1"),
        environment=os.getenv("APP_ENV", "production"),
        host=os.getenv("HOST", default_host),
        port=_env_int("PORT", 8000),
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
        max_files_per_request=_env_int("MAX_FILES_PER_REQUEST", 50),
        max_preview_pages=_env_int("MAX_PREVIEW_PAGES", 30),
        max_total_preview_pages=_env_int("MAX_TOTAL_PREVIEW_PAGES", 500),
        max_ocr_pages=_env_int("MAX_OCR_PAGES", 200),
        word_chunk_pages=_env_int("WORD_CHUNK_PAGES", 40),
        excel_chunk_pages=_env_int("EXCEL_CHUNK_PAGES", 50),
        max_word_total_pages=_env_int("MAX_WORD_TOTAL_PAGES", 8000),
        max_excel_total_pages=_env_int("MAX_EXCEL_TOTAL_PAGES", 3000),
        max_pptx_total_pages=_env_int("MAX_PPTX_TOTAL_PAGES", 500),
        # Prefer new total caps; fall back to legacy MAX_WORD_PAGES if only that is set
        max_word_pages=_env_int(
            "MAX_WORD_TOTAL_PAGES",
            _env_int("MAX_WORD_PAGES", 8000),
        ),
        max_excel_pages=_env_int(
            "MAX_EXCEL_TOTAL_PAGES",
            _env_int("MAX_EXCEL_PAGES", 3000),
        ),
        word_layout_page_threshold=_env_int("WORD_LAYOUT_PAGE_THRESHOLD", 40),
        max_password_length=_env_int("MAX_PASSWORD_LENGTH", 128),
        max_zip_entries=_env_int("MAX_ZIP_ENTRIES", 10_000),
        max_zip_uncompressed_bytes=_env_int(
            "MAX_ZIP_UNCOMPRESSED_BYTES", 500 * 1024 * 1024
        ),
        max_request_bytes=_env_int("MAX_REQUEST_BYTES", 550 * 1024 * 1024),
        max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", 2),
        rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 120),
        subprocess_timeout_seconds=_env_int("SUBPROCESS_TIMEOUT_SECONDS", 180),
        allow_origins=origin_list,
        trusted_hosts=trusted_hosts,
        api_key=api_key,
        temp_dir=temp_dir,
        enable_ocr=_env_bool("ENABLE_OCR", True),
        enable_libreoffice=_env_bool("ENABLE_LIBREOFFICE", True),
        enable_ghostscript=_env_bool("ENABLE_GHOSTSCRIPT", True),
        enable_camelot=_env_bool("ENABLE_CAMELOT", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        request_id_header=os.getenv("REQUEST_ID_HEADER", "X-Request-ID"),
    )


def clear_settings_cache() -> None:
    """Test helper — drop cached settings after env changes."""
    get_settings.cache_clear()
