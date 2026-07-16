"""On-disk store for asynchronous clink dispatch jobs.

A job records a task dispatched to a remote/background service (clink's dispatch
runner) so its handle can be polled and collected in later MCP calls. The remote
service owns execution — the store only maps a job id to the handle plus enough
metadata to re-resolve the CLI client, so records survive server restarts and are
readable from other sessions. Client configuration (and any secrets in it) is
deliberately not stored; it is re-resolved from the registry on every access.

Records live as one JSON file each under ``~/.pal/jobs`` (atomic writes) and are
swept once they are older than ``JOB_TTL_SECONDS``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 48 * 3600
OUTPUT_TAIL_CHARS = 2_000

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_COLLECTED = "collected"


def jobs_dir() -> Path:
    """Directory holding job records (function so tests can redirect HOME)."""
    return Path.home() / ".pal" / "jobs"


@dataclass
class JobRecord:
    """One dispatched task. ``kind`` is reserved for future in-process job types."""

    job_id: str
    cli_name: str
    role: str
    handle: str
    state: str = STATE_RUNNING
    kind: str = "remote"
    created_at: float = 0.0
    updated_at: float = 0.0
    continuation_id: str | None = None
    sanitized_command: list[str] = field(default_factory=list)
    poll_attempts: int = 0
    output_tail: str = ""
    error: str | None = None
    result: dict[str, Any] | None = None


def create_job(
    *,
    cli_name: str,
    role: str,
    handle: str,
    continuation_id: str | None = None,
    sanitized_command: list[str] | None = None,
) -> JobRecord:
    """Create, persist, and return a new running job for a dispatched task."""
    sweep_stale()
    now = time.time()
    record = JobRecord(
        job_id=uuid.uuid4().hex[:12],
        cli_name=cli_name,
        role=role,
        handle=handle,
        created_at=now,
        updated_at=now,
        continuation_id=continuation_id,
        sanitized_command=list(sanitized_command or []),
    )
    save_job(record)
    return record


def save_job(record: JobRecord) -> None:
    """Atomically write a job record to disk (safe against concurrent writers)."""
    record.updated_at = time.time()
    directory = jobs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.job_id}.json"
    # A unique temp name per writer keeps two sessions saving the same job_id
    # (e.g. concurrent `jobs status`) from racing on one temp file before rename.
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f"{record.job_id}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(record), handle, indent=2)
    os.replace(tmp_name, path)


def get_job(job_id: str) -> JobRecord | None:
    """Return a live job by id, or None if unknown, expired (deleted), or unreadable."""
    # Job ids are generated hex; reject anything else before touching the filesystem.
    if not job_id.isalnum():
        return None
    path = jobs_dir() / f"{job_id}.json"
    record = _load(path)
    if record is None:
        return None
    if _expired(record):
        path.unlink(missing_ok=True)
        return None
    return record


def list_jobs() -> list[JobRecord]:
    """Return all live (non-expired, readable) job records, oldest first."""
    sweep_stale()
    records = (_load(path) for path in sorted(jobs_dir().glob("*.json")))
    return sorted((r for r in records if r), key=lambda r: r.created_at)


def sweep_stale() -> None:
    """Delete expired or unreadable job records (and any orphaned temp files)."""
    directory = jobs_dir()
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        record = _load(path)
        if record is None or _expired(record):
            path.unlink(missing_ok=True)
    # Reap temp files orphaned by a crash between write and atomic rename.
    for tmp in directory.glob("*.tmp"):
        if time.time() - tmp.stat().st_mtime > JOB_TTL_SECONDS:
            tmp.unlink(missing_ok=True)


def _expired(record: JobRecord) -> bool:
    return time.time() - record.created_at > JOB_TTL_SECONDS


def _load(path: Path) -> JobRecord | None:
    try:
        return JobRecord(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        logger.warning("Ignoring unreadable job record %s", path, exc_info=True)
        return None
