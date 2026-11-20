"""Where backtest events come from.

* TradingView economic calendar (``economic-calendar.tradingview.com``, public, no key; it needs the tradingview.com
  ``Origin`` header). It serves HISTORY with actual / forecast / previous and a 3-level importance, so it is the
  only source here that can reach back before the current week.
* ForexFactory faireconomy feed (existing ``ForexFactorySource``): this week + next week only and no ``actual``.
  Used only as a fallback when the TradingView calendar cannot be read, and for rows the live app already stored.
* RSS headlines (existing ``RssSource`` feeds) and headlines already stored by the live pipeline (``news_items``):
  feeds only expose their most recent ~20-60 entries, so headline history is as old as what was collected.
* ``app/backtest/headline_history.py`` (optional module, written separately): older headlines. The backfill calls
  ``await fetch_headline_history(start, end)`` when that module imports and merges the result (see below).

Event shape
-----------
Every source ends up as an :class:`EventRecord` (stored in ``bt_events``). ``fetch_headline_history`` returns a list
of plain dicts with the same field names; :func:`from_dict` turns each into an ``EventRecord``::

    {
        "time":       datetime (UTC) or ISO-8601 string          # REQUIRED: publication time
        "title":      str                                        # REQUIRED (stored truncated to 500 chars)
        "source":     str      # short feed / site name, e.g. "fxstreet", "reuters"      (default "headline_history")
        "kind":       "headline"                                 # (default; "calendar" is also accepted)
        "impact":     "high" | "medium" | "low"   # default: rules.headline_impact(title)
        "url":        str | None                  # also the dedupe identity when ``dedupe_key`` is absent
        "summary":    str | None
        "currency":   str | None  # primary currency (USD, EUR...); default: inferred by app.analysis.mapping
        "country":    str | None
        "category":   str | None
        "actual" / "forecast" / "previous": str | None           # calendar-style rows only
        "symbols":    list[str]  # MT5-style names (EURUSD...), at most BACKTEST_MAX_SYMBOLS are kept;
                                 # default: inferred by app.analysis.mapping.map_news
        "dedupe_key": str | None  # stable id; default sha1(source|url or title|time)
        "raw":        dict                                       # anything extra worth keeping
    }

Records outside the requested ``[start, end)`` range are dropped; duplicates (same ``dedupe_key``, or same source +
title + day) are merged, keeping the first one seen.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app.analysis import rules
from app.analysis.mapping import map_news
from app.market.symbols import get_symbol
from app.sources.base import RawNews

log = logging.getLogger(__name__)

TV_CALENDAR_URL = "https://economic-calendar.tradingview.com/events"
TV_COUNTRIES = ("US", "EU", "GB", "JP", "AU", "NZ", "CA", "CH", "CN", "DE", "FR")
TV_WINDOW_DAYS = 7
TV_REQUEST_DELAY_S = 0.4  # pause between window requests (the endpoint answers 429 when hammered)
TV_MAX_ATTEMPTS = 5  # per window: first try + retries on 429 / 5xx / network errors
TV_BACKOFF_BASE_S = 2.0  # retry n waits base * 2**n (or Retry-After when the server gives one)
TV_BACKOFF_MAX_S = 60.0
_TV_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_sleep = asyncio.sleep  # tests replace it
_TV_IMPACT = {1: "high", 0: "medium", -1: "low"}
_FF_IMPACT = {"High": "high", "Medium": "medium", "Low": "low", "Holiday": "low"}


@dataclass
class EventRecord:
    """Normalised event, ready to be stored in ``bt_events``."""

    time: datetime  # UTC
    kind: str  # calendar | headline
    source: str
    title: str
    impact: str  # high | medium | low
    dedupe_key: str
    currency: str | None = None
    country: str | None = None
    summary: str | None = None
    url: str | None = None
    category: str | None = None
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    symbols: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _key(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _fmt_value(value: object, unit: str | None, scale: str | None) -> str | None:
    if value is None or value == "":
        return None
    suffix = f"{scale or ''}{'%' if unit == '%' else ''}"
    return f"{value}{suffix}"[:32]


def _symbols(currencies: list[str], symbols: list[str], limit: int) -> list[str]:
    return [s for s in symbols if get_symbol(s)][:limit]


def from_tv_item(item: dict, max_symbols: int) -> EventRecord | None:
    """One ``result`` entry of the TradingView calendar."""
    try:
        when = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00")).astimezone(UTC)
    except (KeyError, ValueError, TypeError):
        return None
    title = str(item.get("title") or "").strip()
    if not title or item.get("id") is None:
        return None
    currency = (item.get("currency") or "").upper() or None
    unit, scale = item.get("unit"), item.get("scale")
    raw_news = RawNews(kind="calendar", source="tradingview", title=title, event_time=when, currency=currency)
    currencies, symbols = map_news(raw_news, max_symbols)
    comment = item.get("comment")
    return EventRecord(
        time=when,
        kind="calendar",
        source="tradingview",
        title=title[:500],
        impact=_TV_IMPACT.get(item.get("importance"), "low"),
        dedupe_key=_key("tvcal", item["id"]),
        currency=currency,
        country=(item.get("country") or None),
        summary=(str(comment)[:600] if comment else None),
        url="https://www.tradingview.com/economic-calendar/",
        category=item.get("category"),
        actual=_fmt_value(item.get("actual"), unit, scale),
        forecast=_fmt_value(item.get("forecast"), unit, scale),
        previous=_fmt_value(item.get("previous"), unit, scale),
        symbols=_symbols(currencies, symbols, max_symbols),
        raw={k: v for k, v in item.items() if k != "comment"},
    )


def _retry_delay(res: httpx.Response | None, attempt: int) -> float:
    retry_after = res.headers.get("retry-after") if res is not None else None
    try:
        if retry_after:
            return min(max(float(retry_after), 1.0), TV_BACKOFF_MAX_S)
    except ValueError:
        pass
    return min(TV_BACKOFF_BASE_S * 2**attempt, TV_BACKOFF_MAX_S)


async def _get_tv_window(client: httpx.AsyncClient, params: dict, headers: dict) -> dict:
    """One window request; retries 429 / 5xx / network errors with backoff (honouring ``Retry-After``)."""
    for attempt in range(TV_MAX_ATTEMPTS):
        res: httpx.Response | None = None
        try:
            res = await client.get(TV_CALENDAR_URL, params=params, headers=headers)
            if res.status_code not in _TV_RETRYABLE:
                res.raise_for_status()
                payload = res.json()
                if payload.get("status") != "ok":
                    raise ValueError(f"status={payload.get('status')!r}")
                return payload
            failure: Exception = httpx.HTTPStatusError(f"HTTP {res.status_code}", request=res.request, response=res)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            failure = exc
        if attempt == TV_MAX_ATTEMPTS - 1:
            raise failure
        delay = _retry_delay(res, attempt)
        log.info("tradingview calendar %s: %s; retry %d/%d in %.0fs", params["from"][:10], failure,
                 attempt + 1, TV_MAX_ATTEMPTS - 1, delay)
        await _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def fetch_tv_calendar(start: datetime, end: datetime, max_symbols: int = 3,
                            client: httpx.AsyncClient | None = None,
                            request_delay: float | None = None) -> tuple[list[EventRecord], list[str]]:
    """Economic releases in ``[start, end)`` (UTC), paged in consecutive 7-day windows that cover the whole range
    (no gaps, no overlap), paced by ``request_delay`` seconds and retried on 429/5xx. Returns (events, errors);
    a window that still fails after its retries is reported in ``errors`` and the next one is tried."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    delay = TV_REQUEST_DELAY_S if request_delay is None else request_delay
    headers = {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/",
               "User-Agent": "Mozilla/5.0 (FXNews backtest)", "Accept": "application/json"}
    events: dict[str, EventRecord] = {}
    errors: list[str] = []
    try:
        cursor, first = start, True
        while cursor < end:
            window_end = min(cursor + timedelta(days=TV_WINDOW_DAYS), end)
            if not first and delay:
                await _sleep(delay)
            first = False
            params = {"from": cursor.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "to": window_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                      "countries": ",".join(TV_COUNTRIES)}
            try:
                payload = await _get_tv_window(client, params, headers)
                for item in payload.get("result") or []:
                    rec = from_tv_item(item, max_symbols)
                    # The window edges are inclusive on both sides: keep the half-open [start, end).
                    if rec and start <= rec.time < end:
                        events[rec.dedupe_key] = rec
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"tradingview calendar {params['from'][:10]}..{params['to'][:10]}: {exc}")
            cursor = window_end
    finally:
        if own:
            await client.aclose()
    return list(events.values()), errors


def from_raw_news(item: RawNews, max_symbols: int) -> EventRecord | None:
    """Existing source items (ForexFactory calendar rows, RSS headlines) -> EventRecord."""
    if item.event_time is None:
        return None
    currencies, symbols = map_news(item, max(max_symbols, 6))
    if item.kind == "calendar":
        impact = _FF_IMPACT.get(item.impact or "Low", "low")
    else:
        impact = rules.headline_impact(item.title).lower()
    return EventRecord(
        time=item.event_time.astimezone(UTC),
        kind=item.kind,
        source=item.source,
        title=item.title[:500],
        impact=impact,
        dedupe_key=_key("raw", item.dedupe_key),
        currency=(currencies[0] if currencies else item.currency),
        country=None,
        summary=item.summary,
        url=item.url,
        category=None if item.kind == "headline" else "calendar",
        actual=item.actual,
        forecast=item.forecast,
        previous=item.previous,
        symbols=_symbols(currencies, symbols, max_symbols),
        raw=item.raw or {},
    )


def _as_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def from_dict(item: dict, max_symbols: int) -> EventRecord | None:
    """A plain dict in the shape documented at the top of this module (e.g. from ``fetch_headline_history``) ->
    EventRecord; missing impact / currency / symbols / dedupe_key are derived. None when time or title is unusable."""
    when = _as_utc(item.get("time"))
    title = str(item.get("title") or "").strip()
    if when is None or not title:
        return None
    kind = item.get("kind") if item.get("kind") in ("headline", "calendar") else "headline"
    source = str(item.get("source") or "headline_history")[:32]
    impact = str(item.get("impact") or "").lower()
    if impact not in ("high", "medium", "low"):
        impact = rules.headline_impact(title).lower() if kind == "headline" else "low"
    currency = (item.get("currency") or None)
    symbols = [str(s).upper() for s in (item.get("symbols") or [])]
    if not symbols or not currency:
        currencies, mapped = map_news(RawNews(kind=kind, source=source, title=title, event_time=when,
                                              currency=currency, summary=item.get("summary")), max(max_symbols, 6))
        symbols = symbols or mapped
        currency = currency or (currencies[0] if currencies else None)
    return EventRecord(
        time=when,
        kind=kind,
        source=source,
        title=title[:500],
        impact=impact,
        dedupe_key=str(item.get("dedupe_key") or _key("hist", source, item.get("url") or title, when.isoformat())),
        currency=currency.upper() if currency else None,
        country=item.get("country"),
        summary=item.get("summary"),
        url=(str(item["url"])[:1000] if item.get("url") else None),
        category=item.get("category"),
        actual=item.get("actual"),
        forecast=item.get("forecast"),
        previous=item.get("previous"),
        symbols=_symbols([], symbols, max_symbols),
        raw=dict(item.get("raw") or {}),
    )
