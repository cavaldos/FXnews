"""Tiny in-memory registry of long-running backtest jobs (backfill / predictions), polled by the UI."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)
MAX_JOBS = 30


@dataclass
class Job:
    id: str
    kind: str  # backfill | predict
    status: str = "running"  # running | done | error | cancelled
    message: str = "starting"
    result: Any = None
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


_jobs: dict[str, Job] = {}
_tasks: set[asyncio.Task] = set()
_task_of: dict[str, asyncio.Task] = {}


def running(kind: str) -> Job | None:
    return next((j for j in _jobs.values() if j.kind == kind and j.status == "running"), None)


def start(kind: str, runner: Callable[[Callable[[str], None]], Awaitable[Any]]) -> Job:
    """Run ``runner(progress)`` in the background. One running job per kind (the caller checks ``running`` first)."""
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    _jobs[job.id] = job
    while len(_jobs) > MAX_JOBS:
        _jobs.pop(next(iter(_jobs)))

    def progress(message: str) -> None:
        job.message = message

    async def run() -> None:
        try:
            out = await runner(progress)
            job.result = out.to_dict() if hasattr(out, "to_dict") else out
            job.status = "done"
            job.message = "done"
        except asyncio.CancelledError:
            job.status, job.message = "cancelled", "stopped by user"
        except Exception as exc:  # noqa: BLE001
            log.exception("backtest job %s failed", job.id)
            job.status, job.error, job.message = "error", str(exc), "failed"
        finally:
            job.finished_at = datetime.now(UTC).isoformat()

    task = asyncio.create_task(run())
    _tasks.add(task)
    _task_of[job.id] = task
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(lambda _t: _task_of.pop(job.id, None))
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def recent() -> list[Job]:
    return list(reversed(_jobs.values()))


def cancel(job_id: str) -> Job | None:
    """Stop a running job. Work already stored (outcomes, predictions) is kept."""
    job = _jobs.get(job_id)
    if job is None:
        return None
    task = _task_of.get(job_id)
    if job.status == "running" and task is not None and not task.done():
        job.message = "stopping"
        task.cancel()
    return job
