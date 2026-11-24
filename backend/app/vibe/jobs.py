"""In-memory registry of backtest jobs (a backtest can take minutes; POST returns an id, GET polls it).

Jobs live in this process only (lost on restart); the sidecar run dir with artifacts stays on disk.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from app.vibe.client import VibeError, VibeUnavailable, get_client
from app.vibe.schemas import BacktestIn, BacktestJob

MAX_JOBS = 50
_jobs: OrderedDict[str, BacktestJob] = OrderedDict()
_tasks: set[asyncio.Task] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _run(job_id: str, req: BacktestIn) -> None:
    job = _jobs[job_id]
    try:
        job.result = await get_client().run_backtest(req)
        job.status = "completed"
    except (VibeError, VibeUnavailable) as exc:
        job.status, job.error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - never leave a job stuck in "running"
        job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
    job.finished_at = _now()


def start(req: BacktestIn) -> BacktestJob:
    job = BacktestJob(id=uuid.uuid4().hex[:12], status="running", created_at=_now(), request=req)
    _jobs[job.id] = job
    while len(_jobs) > MAX_JOBS:
        _jobs.popitem(last=False)
    task = asyncio.get_running_loop().create_task(_run(job.id, req))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


def get(job_id: str) -> BacktestJob | None:
    return _jobs.get(job_id)


def clear() -> None:
    _jobs.clear()
