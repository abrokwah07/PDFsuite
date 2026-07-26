"""Timed, logged subprocess runner for Ghostscript / LibreOffice."""

from __future__ import annotations

import logging
import subprocess

from fastapi import HTTPException

from app.config import Settings

logger = logging.getLogger("pdfsuite")


def run_subprocess(
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
