"""Local append-only audit log (JSONL). No cloud, no PII beyond filenames."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("pdfsuite")


class AuditLog:
    """Thread-safe local audit trail stored as JSON lines."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 2000,
    ) -> None:
        self.path = path
        self.max_entries = max(100, max_entries)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(
        self,
        action: str,
        *,
        detail: str = "",
        status: str = "ok",
        filename: str = "",
        job_id: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "ts": time.time(),
            "action": action,
            "status": status,
            "detail": (detail or "")[:500],
            "filename": (filename or "")[:260],
            "job_id": job_id or "",
            "meta": meta or {},
        }
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                self._trim_unlocked()
            except OSError as exc:
                logger.warning("audit write failed: %s", exc)
        return event

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        events: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        events.reverse()  # newest first
        return events

    def _trim_unlocked(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= self.max_entries:
                return
            keep = lines[-self.max_entries :]
            self.path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("audit trim failed: %s", exc)
