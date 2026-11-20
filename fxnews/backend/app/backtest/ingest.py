"""Backfill: collect events for a date range, store them, then compute price outcomes from candles."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.backtest import sources
from app.backtest.models import HORIZONS, BtEvent, BtOutcome
from app.backtest.outcomes import INTERVAL_SECONDS, compute_outcome
from app.config import get_settings
from app.db import session_scope, utcnow
from app.market import candles as candle_service
from app.market.symbols import get_symbol
from app.models import NewsItem
from app.sources.base import RawNews

log = logging.getLogger(__name__)

# (symbol, interval) -> as much candle history as the providers hold; may return a ``CandlesOut`` (carries
# ``source``) or a plain list
CandleFetcher = Callable[[str, str], Awaitable[object]]
Progress = Callable[[str], None]


@dataclass
class BackfillResult:
    start: str = ""
    end: str = ""
    collected: dict[str, int] = field(default_factory=dict)  # source -> events found in range
    events_inserted: int = 0
    events_updated: int = 0
    outcomes_created: int = 0
    outcomes_updated: int = 0
    outcomes_skipped_future: int = 0  # event not yet released
    outcomes_skipped_no_data: int = 0  # candles exist but none at the release time (market closed), or fetch failed
    outcomes_skipped_not_covered: int = 0  # event older than the history of every candle interval
    outcomes_skipped_complete: int = 0  # already final (or already computed at a finer interval)
    outcomes_by_interval: dict[str, int] = field(default_factory=dict)  # candle interval used -> outcomes written
    symbols_without_candles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def default_candle_fetcher(symbol: str, interval: str):
    """Deepest history available (TradingView ~5500 bars, Yahoo fallback at its own limits)."""
    return await candle_service.get_candles(symbol, interval, "max")


# ── 1. collect ────────────────────────────────────────────────────────────


def _stored_news(start: datetime, end: datetime, kinds: tuple[str, ...]) -> list[RawNews]:
    """Items the live pipeline already stored in ``news_items`` (it has been polling the feeds)."""
    with session_scope() as session:
        rows = session.scalars(
            select(NewsItem).where(NewsItem.event_time >= start, NewsItem.event_time < end, NewsItem.kind.in_(kinds))
        ).all()
        return [
            RawNews(kind=r.kind, source=r.source, title=r.title, event_time=r.event_time, url=r.url, summary=r.summary,
                    currency=r.currency, impact=r.impact, actual=r.actual, forecast=r.forecast, previous=r.previous,
                    raw=r.raw or {})
            for r in rows
        ]


def _load_headline_history():
    """``app.backtest.headline_history.fetch_headline_history`` when that optional module exists and imports, else
    None. A module that exists but fails to import is reported by the caller, not silently ignored."""
    try:
        module = importlib.import_module("app.backtest.headline_history")
    except ModuleNotFoundError as exc:
        if exc.name == "app.backtest.headline_history":
            return None
        raise
    return getattr(module, "fetch_headline_history", None)


def _same_story(rec: sources.EventRecord) -> tuple[str, str, str]:
    return rec.source, re.sub(r"\W+", " ", rec.title.lower()).strip(), rec.time.strftime("%Y-%m-%d")


async def collect_events(start: datetime, end: datetime, result: BackfillResult,
                         tv_client=None, live: bool = True, history: bool | None = None) -> list[sources.EventRecord]:
    """``history`` (default: same as ``live``) enables the optional ``headline_history`` module."""
    settings = get_settings()
    cap = settings.backtest_max_symbols
    records: dict[str, sources.EventRecord] = {}
    stories: set[tuple[str, str, str]] = set()

    def add(recs: list[sources.EventRecord], label: str) -> None:
        recs = [r for r in recs if start <= r.time < end]
        result.collected[label] = result.collected.get(label, 0) + len(recs)
        for r in recs:
            if r.dedupe_key in records:
                continue
            if r.kind == "headline":
                story = _same_story(r)
                if story in stories:
                    continue
                stories.add(story)
            records[r.dedupe_key] = r

    tv_events, tv_errors = await sources.fetch_tv_calendar(start, end, cap, client=tv_client)
    result.errors.extend(tv_errors)
    add(tv_events, "tradingview")

    # ForexFactory is only a fallback for the calendar: TradingView has history and actuals, FF does not.
    if not tv_events:
        stored = [i for i in _stored_news(start, end, ("calendar",))]
        if live:
            try:
                from app.sources.forexfactory import ForexFactorySource

                stored += await ForexFactorySource().fetch()
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"forexfactory: {exc}")
        add([r for r in (sources.from_raw_news(i, cap) for i in stored) if r], "forexfactory")

    headlines = _stored_news(start, end, ("headline",))
    if live:
        from app.sources.rss import build_rss_sources

        feeds = build_rss_sources(settings)
        fetched = await asyncio.gather(*(f.fetch() for f in feeds), return_exceptions=True)
        for feed, res in zip(feeds, fetched):
            if isinstance(res, BaseException):
                result.errors.append(f"rss {feed.name}: {res}")
            else:
                headlines.extend(res)
    by_source: dict[str, list[sources.EventRecord]] = defaultdict(list)
    for item in headlines:
        rec = sources.from_raw_news(item, cap)
        if rec:
            by_source[rec.source].append(rec)
    for name, recs in by_source.items():
        unique = {r.dedupe_key: r for r in recs}
        add(list(unique.values()), name)

    # Optional: older headlines from app/backtest/headline_history.py (written separately). Added last so what the
    # live feeds / pipeline stored wins on duplicates.
    if live if history is None else history:
        try:
            fetch_history = _load_headline_history()
            if fetch_history is not None:
                extras: dict = {}
                hist_errors: list[str] = []
                if "errors" in inspect.signature(fetch_history).parameters:  # optional per-source error reporting
                    extras["errors"] = hist_errors
                items = await fetch_history(start, end, **extras)
                result.errors.extend(f"headline_history: {e}"[:240] for e in hist_errors)
                by_source = defaultdict(list)
                for item in items or []:
                    rec = sources.from_dict(item, cap)
                    if rec:
                        by_source[f"{rec.source} (history)"].append(rec)
                for name, recs in by_source.items():
                    add(recs, name)
        except Exception as exc:  # noqa: BLE001 - the hook is optional, never fatal
            log.warning("backtest: headline history failed: %s", exc)
            result.errors.append(f"headline_history: {exc}"[:240])
    return list(records.values())


# ── 2. store ──────────────────────────────────────────────────────────────


def store_events(records: list[sources.EventRecord]) -> tuple[int, int]:
    """Upsert by ``dedupe_key``. Returns (inserted, updated). Updates refresh actual/forecast/previous/impact."""
    if not records:
        return 0, 0
    inserted = updated = 0
    with session_scope() as session:
        keys = [r.dedupe_key for r in records]
        existing: dict[str, BtEvent] = {}
        for i in range(0, len(keys), 500):  # SQLite variable limit
            existing.update({e.dedupe_key: e for e in session.scalars(select(BtEvent).where(BtEvent.dedupe_key.in_(keys[i:i + 500])))})
        for rec in records:
            row = existing.get(rec.dedupe_key)
            if row is None:
                row = BtEvent(dedupe_key=rec.dedupe_key, time=rec.time, kind=rec.kind, source=rec.source,
                              currency=rec.currency, country=rec.country, title=rec.title, summary=rec.summary,
                              url=rec.url, impact=rec.impact, category=rec.category, actual=rec.actual,
                              forecast=rec.forecast, previous=rec.previous, raw=rec.raw)
                row.symbols = rec.symbols
                session.add(row)
                inserted += 1
                continue
            changes = {"actual": rec.actual, "forecast": rec.forecast, "previous": rec.previous, "impact": rec.impact,
                       "time": rec.time}
            if any(getattr(row, k) != v for k, v in changes.items()):
                for k, v in changes.items():
                    setattr(row, k, v)
                updated += 1
    return inserted, updated


# ── 3. outcomes ───────────────────────────────────────────────────────────

_PAUSE_RE = re.compile(r"paused for (?:another )?(\d+)s")
FETCH_ATTEMPTS = 3


_sleep = asyncio.sleep  # tests replace it


async def _fetch_with_retry(fetcher: CandleFetcher, symbol: str, interval: str, sleep=None):
    """TradingView/Yahoo answer 429 when many symbols are fetched back to back; the candle service then pauses the
    provider for a while ("paused for another 48s"). Wait that long and try again instead of giving up."""
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return await fetcher(symbol, interval)
        except Exception as exc:  # noqa: BLE001
            if attempt == FETCH_ATTEMPTS - 1:
                raise
            waits = [int(m) for m in _PAUSE_RE.findall(str(exc))]
            delay = min(max(waits) + 2, 200) if waits else 15
            log.info("backtest: candles %s failed (%s); retrying in %ss", symbol, str(exc)[:120], delay)
            await (sleep or _sleep)(delay)


def _apply(row: BtOutcome, data, source: str | None) -> None:
    row.interval = data.interval
    row.candle_source = source
    row.price_at_release = data.price_at_release
    for h in HORIZONS:
        setattr(row, f"price_{h}", data.prices.get(h))
        setattr(row, f"ret_{h}", data.returns.get(h))
        setattr(row, f"dir_{h}", data.directions.get(h))
    row.max_up_1h = data.max_up_1h
    row.max_down_1h = data.max_down_1h
    row.direction = data.direction
    row.direction_horizon = data.direction_horizon
    row.complete = data.complete
    row.computed_at = utcnow()


def _candle_chain(settings) -> list[str]:
    chain = [i for i in settings.backtest_candle_intervals if i in INTERVAL_SECONDS]
    return sorted(dict.fromkeys(chain), key=INTERVAL_SECONDS.__getitem__) or ["5m"]


def _history_start(candles: list, step: int) -> int:
    """Unix time from which the series has a finished candle (the earliest event it can price)."""
    return min(c.time for c in candles) + step


async def compute_outcomes(start: datetime, end: datetime, result: BackfillResult,
                           fetcher: CandleFetcher | None = None, now: datetime | None = None,
                           symbols: list[str] | None = None, event_ids: list[int] | None = None,
                           force: bool = False) -> None:
    """Outcomes for the events in ``[start, end)``. Per symbol, candles are downloaded once per interval (deepest
    history), finest interval first; an event is priced from the finest interval whose history reaches it, and the
    interval used is stored in ``bt_outcomes.interval``. Coarser intervals are only downloaded when some events are
    older than the finer one's history. A gap at the release time (market closed) is not escalated to a coarser
    interval: that would only price it from stale data.

    ``event_ids`` restricts the work to those events (the time range may hold others). ``force`` recomputes
    outcomes that are already final, at the finest interval that covers the event."""
    settings = get_settings()
    chain = _candle_chain(settings)
    fetcher = fetcher or default_candle_fetcher
    now = utc(now or utcnow())
    now_ts = int(now.timestamp())

    # symbol -> [(event id, ts)] still needing work
    todo: dict[str, list[tuple[int, int]]] = defaultdict(list)
    existing_step: dict[tuple[int, str], int] = {}  # outcome already stored -> its candle step
    with session_scope() as session:
        stmt = select(BtEvent).where(BtEvent.time >= start, BtEvent.time < end)
        if event_ids is not None:
            stmt = stmt.where(BtEvent.id.in_(event_ids))
        events = session.scalars(stmt).all()
        outcomes = {(o.event_id, o.symbol): o for o in session.scalars(
            select(BtOutcome).where(BtOutcome.event_id.in_([e.id for e in events] or [0])))} if events else {}
        for ev in events:
            ts = int(ev.time.timestamp())
            for sym in ev.symbols[: settings.backtest_max_symbols]:
                if symbols and sym not in symbols:
                    continue
                old = outcomes.get((ev.id, sym))
                if ts > now_ts:
                    result.outcomes_skipped_future += 1
                elif old is not None and old.complete and not force:
                    result.outcomes_skipped_complete += 1
                else:
                    todo[sym].append((ev.id, ts))
                    if old is not None and not force:
                        existing_step[(ev.id, sym)] = INTERVAL_SECONDS.get(old.interval, 10**9)

    kinds = {s: (get_symbol(s).kind if get_symbol(s) else None) for s in todo}
    fetched_any = False
    for sym, items in todo.items():
        pending = list(items)
        any_candles = False
        for interval in chain:
            if not pending:
                break
            step = INTERVAL_SECONDS[interval]
            if fetched_any and settings.backtest_candle_delay_seconds:
                await _sleep(settings.backtest_candle_delay_seconds)  # be gentle with the candle providers
            fetched_any = True
            try:
                candles = await _fetch_with_retry(fetcher, sym, interval)
            except Exception as exc:  # noqa: BLE001 - one symbol failing must not stop the others
                log.warning("backtest: %s candles for %s failed: %s", interval, sym, exc)
                result.errors.append(f"candles {sym} {interval}: {exc}"[:240])
                result.symbols_without_candles.append(sym)
                result.outcomes_skipped_no_data += len(pending)
                pending = []
                break
            source = getattr(candles, "source", None)
            candles = list(getattr(candles, "candles", candles))
            if not candles:
                continue  # nothing at this interval: a coarser one may still have data
            any_candles = True
            covered_from = _history_start(candles, step)
            here = [(eid, ts) for eid, ts in pending if ts >= covered_from]
            pending = [(eid, ts) for eid, ts in pending if ts < covered_from]
            with session_scope() as session:
                for event_id, ts in here:
                    if existing_step.get((event_id, sym), 10**9) < step:
                        result.outcomes_skipped_complete += 1  # keep the finer outcome already stored
                        continue
                    data = compute_outcome(candles, ts, symbol=sym, kind=kinds[sym], interval=interval, now_ts=now_ts,
                                           primary_horizon=settings.backtest_primary_horizon)
                    if data is None:
                        result.outcomes_skipped_no_data += 1
                        continue
                    row = session.scalar(select(BtOutcome).where(BtOutcome.event_id == event_id, BtOutcome.symbol == sym))
                    if row is None:
                        row = BtOutcome(event_id=event_id, symbol=sym, interval=interval, price_at_release=data.price_at_release)
                        session.add(row)
                        result.outcomes_created += 1
                    else:
                        result.outcomes_updated += 1
                    result.outcomes_by_interval[interval] = result.outcomes_by_interval.get(interval, 0) + 1
                    _apply(row, data, source)
        if pending and any_candles:  # older than every interval's history
            result.outcomes_skipped_not_covered += len(pending)
        elif pending:  # the providers returned nothing at any interval
            result.symbols_without_candles.append(sym)
            result.outcomes_skipped_no_data += len(pending)


async def backfill(start: datetime, end: datetime | None = None, *, fetcher: CandleFetcher | None = None,
                   now: datetime | None = None, tv_client=None, live: bool = True,
                   progress: Progress | None = None) -> BackfillResult:
    """Collect events in ``[start, end)`` (default end = now), store them and compute their outcomes."""
    say = progress or (lambda _msg: None)
    now = utc(now or utcnow())
    start, end = utc(start), utc(end or now)
    if end <= start:
        raise ValueError("end must be after start")
    result = BackfillResult(start=start.isoformat(), end=end.isoformat())

    say("collecting events")
    records = await collect_events(start, end, result, tv_client=tv_client, live=live)
    say(f"storing {len(records)} events")
    result.events_inserted, result.events_updated = store_events(records)
    say("computing outcomes from candles")
    await compute_outcomes(start, end, result, fetcher=fetcher, now=now)
    say("done")
    return result


async def verify_event(event_id: int, *, force: bool = False, fetcher: CandleFetcher | None = None,
                       now: datetime | None = None) -> BackfillResult:
    """Price ONE stored event from historical candles (touches no other event). Raises ``LookupError`` when the
    event does not exist and ``ValueError`` when it has not been released yet."""
    now = utc(now or utcnow())
    with session_scope() as session:
        event = session.get(BtEvent, event_id)
        if event is None:
            raise LookupError("event not found")
        when = utc(event.time)
    if when > now:
        raise ValueError("event has not been released yet")
    result = BackfillResult(start=when.isoformat(), end=when.isoformat())
    await compute_outcomes(when, when + timedelta(seconds=1), result, fetcher=fetcher, now=now,
                           event_ids=[event_id], force=force)
    return result
