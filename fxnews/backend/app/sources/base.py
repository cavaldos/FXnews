"""Shared types for news sources. Every source returns list[RawNews]."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol


@dataclass
class RawNews:
    kind: Literal["calendar", "headline"]
    source: str  # short id: "forexfactory", "fxstreet", ...
    title: str
    event_time: datetime | None  # timezone-aware UTC
    url: str | None = None
    summary: str | None = None
    currency: str | None = None  # calendar: FF "country" field (USD, EUR, ...)
    impact: str | None = None  # High / Medium / Low / Holiday (calendar only)
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Stable identity. Calendar items keep identity when forecast/actual change."""
        if self.kind == "calendar":
            ident = f"cal|{self.source}|{self.currency}|{self.title}|{self.event_time.isoformat() if self.event_time else ''}"
        else:
            ident = f"hl|{self.source}|{self.url or self.title}"
        return hashlib.sha1(ident.encode("utf-8")).hexdigest()


class NewsSource(Protocol):
    name: str

    async def fetch(self) -> list[RawNews]: ...
