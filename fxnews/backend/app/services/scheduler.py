"""Background polling loops started from the FastAPI lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import get_settings
from app.services import ingest

log = logging.getLogger(__name__)


async def _loop(name: str, interval_seconds: float, job: Callable[[], Awaitable[object]], initial_delay: float = 0) -> None:
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("scheduled job %s failed", name)
        await asyncio.sleep(interval_seconds)


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        s = get_settings()
        self._tasks = [
            asyncio.create_task(_loop("calendar", max(300.0, s.ff_poll_minutes * 60), ingest.run_calendar_cycle,
                                      ingest.seconds_until_calendar_due())),
            asyncio.create_task(_loop("rss", max(60.0, s.rss_poll_minutes * 60), ingest.run_rss_cycle, 5)),
            asyncio.create_task(_loop("pre_event", 60.0, ingest.run_pre_event_cycle, 20)),
        ]
        log.info("scheduler started (%d jobs)", len(self._tasks))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await ingest.drain_background()


scheduler = Scheduler()
