"""In-process background jobs with progress and cancel support."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from app.tempfiles import safe_rmtree, safe_unlink

logger = logging.getLogger("pdfsuite")


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = JobStatus.queued
    progress: float = 0.0
    phase: str = "queued"
    message: str = ""
    current: int = 0
    total: int = 0
    result_path: str | None = None
    result_name: str | None = None
    media_type: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Extra cleanup paths (inputs, temps) when job is discarded
    cleanup_paths: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status.value,
            "progress": round(self.progress, 1),
            "phase": self.phase,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "result_name": self.result_name,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "downloadable": self.status == JobStatus.completed and bool(self.result_path),
            "cancellable": self.status in {JobStatus.queued, JobStatus.running},
            "meta": self.meta,
        }

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def request_cancel(self) -> None:
        self.cancel_event.set()
        if self.status in {JobStatus.queued, JobStatus.running}:
            self.status = JobStatus.cancelled
            self.phase = "cancelled"
            self.message = "Cancelled by user"
            self.updated_at = time.time()


class JobStore:
    """Thread-safe in-memory job registry (single process)."""

    def __init__(self, *, ttl_seconds: int = 3600, max_jobs: int = 100) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.ttl_seconds = ttl_seconds
        self.max_jobs = max_jobs

    def create(self, kind: str, **meta: Any) -> Job:
        self.purge_expired()
        job = Job(id=uuid.uuid4().hex, kind=kind, meta=dict(meta))
        with self._lock:
            # Evict oldest completed/failed if over cap
            if len(self._jobs) >= self.max_jobs:
                self._evict_oldest_unlocked()
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: float | None = None,
        phase: str | None = None,
        message: str | None = None,
        current: int | None = None,
        total: int | None = None,
        error: str | None = None,
        result_path: str | None = None,
        result_name: str | None = None,
        media_type: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if job.status == JobStatus.cancelled and status not in {
                JobStatus.cancelled,
                JobStatus.failed,
            }:
                # Don't resurrect cancelled jobs
                return
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress = max(0.0, min(100.0, progress))
            if phase is not None:
                job.phase = phase
            if message is not None:
                job.message = message
            if current is not None:
                job.current = current
            if total is not None:
                job.total = total
            if error is not None:
                job.error = error
            if result_path is not None:
                job.result_path = result_path
            if result_name is not None:
                job.result_name = result_name
            if media_type is not None:
                job.media_type = media_type
            job.updated_at = time.time()

    def progress_cb(self, job_id: str) -> Callable[..., None]:
        """Return a callback(current, total, phase=..., message=...)."""

        def _cb(
            current: int,
            total: int,
            *,
            phase: str | None = None,
            message: str | None = None,
        ) -> None:
            pct = (current / total * 100.0) if total > 0 else 0.0
            # Reserve 0-5 for upload/queue, 5-95 for work, 95-100 for finalize
            mapped = 5.0 + (pct * 0.90)
            self.update(
                job_id,
                progress=mapped,
                current=current,
                total=total,
                phase=phase or "processing",
                message=message or (f"Page {current} of {total}" if total else ""),
            )

        return _cb

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            dead = [
                jid
                for jid, j in self._jobs.items()
                if now - j.updated_at > self.ttl_seconds
                and j.status
                in {
                    JobStatus.completed,
                    JobStatus.failed,
                    JobStatus.cancelled,
                }
            ]
            for jid in dead:
                self._discard_unlocked(jid)

    def discard(self, job_id: str) -> None:
        with self._lock:
            self._discard_unlocked(job_id)

    def _discard_unlocked(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if not job:
            return
        paths = list(job.cleanup_paths)
        if job.result_path:
            paths.append(job.result_path)
        for p in paths:
            safe_unlink(p)
            safe_rmtree(p)

    def _evict_oldest_unlocked(self) -> None:
        finished = [
            j
            for j in self._jobs.values()
            if j.status
            in {JobStatus.completed, JobStatus.failed, JobStatus.cancelled}
        ]
        if not finished:
            return
        finished.sort(key=lambda j: j.updated_at)
        self._discard_unlocked(finished[0].id)


class CancelledError(Exception):
    """Raised when a job is cancelled mid-flight."""
