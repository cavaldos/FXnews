"""ForexFactory economic calendar via the public faireconomy JSON feed.

Ported from src/news/forexfactoryCalendar.js. The feed only carries forecast/previous
(no `actual`), and is rate limited, so we poll it sparingly and keep the last good
payload per URL when a request is refused.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.sources.base import RawNews

log = logging.getLogger(__name__)

# ForexFactory publishes its calendar in US Eastern time. The feed normally carries an explicit offset
# ("2026-11-11T08:30:00-05:00"); a date without one is read as New York wall-clock time (DST aware), never in
# the server's own zone. (On systems without tz data, `pip install tzdata`; such dates are skipped.)
try:
    FF_NAIVE_TZ: ZoneInfo | None = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # pragma: no cover - depends on the OS
    FF_NAIVE_TZ = None
    log.warning("tz database not found: ForexFactory dates without a UTC offset will be skipped (pip install tzdata)")

THIS_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEXT_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
CALENDAR_PAGE_URL = "https://www.forexfactory.com/calendar"

_IMPACTS = {"high": "High", "medium": "Medium", "low": "Low", "holiday": "Holiday"}


def normalize_impact(value: str | None) -> str:
    return _IMPACTS.get(str(value or "").strip().lower(), "Low")


def _clean(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_event(raw: dict) -> RawNews | None:
    if not isinstance(raw, dict):
        return None
    title = _clean(raw.get("title"))
    date_raw = _clean(raw.get("date"))
    if not title or not date_raw:
        return None
    try:
        parsed = datetime.fromisoformat(date_raw)
    except ValueError:
        log.debug("Skipping FF event with bad date %r", date_raw)
        return None
    if parsed.tzinfo is None:
        if FF_NAIVE_TZ is None:
            log.debug("Skipping FF event with offset-less date %r (no tz database)", date_raw)
            return None
        parsed = parsed.replace(tzinfo=FF_NAIVE_TZ)
    event_time = parsed.astimezone(UTC)

    currency = (_clean(raw.get("country")) or "ALL").upper()
    return RawNews(
        kind="calendar",
        source="forexfactory",
        title=title,
        event_time=event_time,
        url=CALENDAR_PAGE_URL,
        currency=currency,
        impact=normalize_impact(raw.get("impact")),
        actual=_clean(raw.get("actual")),
        forecast=_clean(raw.get("forecast")),
        previous=_clean(raw.get("previous")),
        raw=raw,
    )


class ForexFactorySource:
    name = "forexfactory"

    def __init__(self, urls: tuple[str, ...] = (THIS_WEEK_URL, NEXT_WEEK_URL), timeout: float = 20.0) -> None:
        self.urls = urls
        self.timeout = timeout
        self._last_good: dict[str, list[dict]] = {}

    async def _fetch_url(self, client: httpx.AsyncClient, url: str) -> list[dict]:
        try:
            res = await client.get(url)
            if res.status_code == 404 and url == NEXT_WEEK_URL:
                return []  # next week's file is published late in the week
            res.raise_for_status()
            data = res.json()  # rate-limit responses come back as HTML -> ValueError
            if not isinstance(data, list):
                raise ValueError("unexpected payload shape")
            self._last_good[url] = data
            return data
        except (httpx.HTTPError, ValueError) as exc:
            cached = self._last_good.get(url)
            if cached is not None:
                log.warning("ForexFactory %s failed (%s); using cached payload", url, exc)
                return cached
            raise RuntimeError(f"ForexFactory feed {url} failed: {exc}") from exc

    async def fetch(self) -> list[RawNews]:
        headers = {"User-Agent": "Mozilla/5.0 (FXNews calendar)", "Accept": "application/json"}
        events: list[RawNews] = []
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers, follow_redirects=True) as client:
            for url in self.urls:
                try:
                    payload = await self._fetch_url(client, url)
                except RuntimeError as exc:
                    errors.append(str(exc))
                    continue
                events.extend(item for item in map(parse_event, payload) if item)
        if errors and not events:
            raise RuntimeError("; ".join(errors))
        for err in errors:
            log.warning(err)
        return events
