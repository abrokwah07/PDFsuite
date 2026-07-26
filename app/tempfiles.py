"""Safe temporary file helpers with guaranteed cleanup."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterable

from fastapi import BackgroundTasks
from fastapi.responses import FileResponse

from app.config import Settings

logger = logging.getLogger(__name__)


def work_dir(settings: Settings) -> str | None:
    if settings.temp_dir is None:
        return None
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    return str(settings.temp_dir)


def write_bytes(data: bytes, suffix: str, settings: Settings) -> str:
    """Write bytes to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=suffix, dir=work_dir(settings))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        safe_unlink(path)
        raise
    return path


def make_temp_path(suffix: str, settings: Settings) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, dir=work_dir(settings))
    os.close(fd)
    return path


def make_temp_dir(settings: Settings) -> str:
    return tempfile.mkdtemp(dir=work_dir(settings))


def safe_unlink(path: str | Path | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to remove temp file %s: %s", path, exc)


def safe_rmtree(path: str | Path | None) -> None:
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        logger.warning("Failed to remove temp dir %s: %s", path, exc)


def schedule_cleanup(background: BackgroundTasks, *paths: str | Path | None) -> None:
    for path in paths:
        if path is None:
            continue
        p = Path(path)
        if p.is_dir():
            background.add_task(safe_rmtree, p)
        else:
            background.add_task(safe_unlink, p)


def file_response(
    path: str,
    *,
    filename: str,
    media_type: str,
    background: BackgroundTasks,
    extra_cleanup: Iterable[str | Path | None] = (),
) -> FileResponse:
    """Return a FileResponse that deletes the file (and extras) after send."""
    schedule_cleanup(background, path, *extra_cleanup)
    return FileResponse(
        path=path,
        filename=filename,
        media_type=media_type,
        background=background,
    )


@contextmanager
def managed_paths(*paths: str | Path | None) -> Generator[None, None, None]:
    """Ensure paths are deleted when leaving the context (on success or error)."""
    try:
        yield
    finally:
        for path in paths:
            if path is None:
                continue
            p = Path(path)
            if p.is_dir():
                safe_rmtree(p)
            else:
                safe_unlink(p)
