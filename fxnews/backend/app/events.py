"""Tiny in-process pub/sub used by the SSE endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self, queue_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._queue_size = queue_size

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: BaseModel | dict[str, Any]) -> None:
        payload = data.model_dump_json() if isinstance(data, BaseModel) else json.dumps(data, default=str)
        message = f"event: {event}\ndata: {payload}\n\n"
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:  # slow client: drop rather than block ingestion
                log.warning("SSE subscriber queue full, dropping %s event", event)

    async def subscribe(self, heartbeat_seconds: float = 15.0) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subscribers.discard(queue)


broadcaster = Broadcaster()
