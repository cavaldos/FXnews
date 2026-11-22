"""Fetch sources -> normalise/map -> upsert SQLite -> SSE -> schedule LLM analyses."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.analysis import advisor, rules
from app.analysis.mapping import explicit_symbols, map_news
from app.config import get_settings
from app.db import session_scope, utcnow
from app.events import broadcaster
from app.models import NewsItem
from app.schemas import IngestResult
from app.services.serializers import news_out
from app.sources.base import RawNews
from app.sources.forexfactory import ForexFactorySource

log = logging.getLogger(__name__)

IMPACT_RANK = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0}
HEADLINE_FRESH_MINUTES = 30  # only auto-analyse headlines newer than this (avoids backlog on first run)
MAX_PRE_EVENT_ATTEMPTS = 3  # analysis attempts per calendar event (1 + up to 2 retries after a failure)
BUDGET_WINDOW_S = 3600.0

_ff_source = ForexFactorySource()
_background: set[asyncio.Task] = set()
_analysis_slots = asyncio.Semaphore(2)
state: dict[str, datetime | None] = {"last_ingest_at": None}

# In-memory cost controls for AUTOMATIC analyses (reset on restart, which is fine for a single-user app).
_pre_event_attempts: dict[int, int] = {}  # news id -> analysis attempts started
_headline_cooldown: dict[str, float] = {}  # primary currency -> time of the last auto headline analysis
_auto_llm_calls: deque[float] = deque()  # start times of auto analyses inside the budget window


def _now_ts() -> float:
    return time.time()


def _budget_left(max_per_hour: int) -> int:
    cutoff = _now_ts() - BUDGET_WINDOW_S
    while _auto_llm_calls and _auto_llm_calls[0] <= cutoff:
        _auto_llm_calls.popleft()
    return max(0, max_per_hour - len(_auto_llm_calls))


def _cooldown_left(currency: str, minutes: float) -> float:
    last = _headline_cooldown.get(currency)
    return 0.0 if last is None else max(0.0, last + minutes * 60 - _now_ts())


def reset_cost_controls() -> None:
    """Forget attempts / cooldowns / budget usage (tests)."""
    _pre_event_attempts.clear()
    _headline_cooldown.clear()
    _auto_llm_calls.clear()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _allow_pre_event_retry(news_id: int) -> bool:
    """After a failed pre-event analysis: re-arm the event so the next cycle (60s) tries again, as long as it
    is still in the future and fewer than MAX_PRE_EVENT_ATTEMPTS attempts were made."""
    attempts = _pre_event_attempts.get(news_id, 0)
    if attempts >= MAX_PRE_EVENT_ATTEMPTS:
        log.warning("pre-event analysis of news %s failed %d times; giving up", news_id, attempts)
        return False
    with session_scope() as session:
        row = session.get(NewsItem, news_id)
        if row is None or row.event_time is None or row.event_time <= utcnow():
            return False
        row.pre_event_alerted = False
    log.warning("pre-event analysis of news %s failed (attempt %d/%d); will retry on the next cycle",
                news_id, attempts, MAX_PRE_EVENT_ATTEMPTS)
    return True


async def _analyze_bg(news_id: int, kind: str) -> None:
    failed = False
    async with _analysis_slots:
        try:
            out = await advisor.analyze_news(news_id, kind=kind)
            failed = bool(getattr(out, "error", None))
        except Exception:  # noqa: BLE001
            log.exception("auto analysis of news %s failed", news_id)
            failed = True
    if kind != "pre_event":
        return
    # Without an API key the "failure" is permanent and rule-based: retrying would only repeat it.
    if failed and get_settings().llm_enabled:
        _allow_pre_event_retry(news_id)
    else:
        _pre_event_attempts.pop(news_id, None)


def store_items(items: list[RawNews]) -> tuple[list[int], int]:
    """Upsert items. Returns (new ids, updated count). Publishes `news` SSE events for new rows."""
    if not items:
        return [], 0
    unique = {item.dedupe_key: item for item in items}
    new_rows: list[NewsItem] = []
    updated = 0
    with session_scope() as session:
        existing = {row.dedupe_key: row for row in session.scalars(
            select(NewsItem).where(NewsItem.dedupe_key.in_(list(unique))))}
        for key, item in unique.items():
            currencies, symbols = map_news(item)
            # Headline impact is judged on the title only: summaries mention "Fed", "inflation", ... in almost
            # every article, which made ~half of all headlines "High" and each one an LLM call.
            impact = item.impact if item.kind == "calendar" else rules.headline_impact(item.title)
            bias = rules.rule_bias(kind=item.kind, title=item.title, summary=item.summary, currencies=currencies,
                                   symbols=symbols, actual=item.actual, forecast=item.forecast,
                                   explicit_pair=bool(explicit_symbols(item.title)))
            row = existing.get(key)
            if row is not None:
                if item.kind == "calendar" and (row.forecast, row.previous, row.actual, row.impact) != (
                        item.forecast, item.previous, item.actual, impact):
                    row.forecast, row.previous, row.actual, row.impact = item.forecast, item.previous, item.actual, impact
                    row.rule_bias = bias
                    updated += 1
                continue
            row = NewsItem(dedupe_key=key, kind=item.kind, source=item.source, title=item.title[:500],
                           summary=item.summary, url=item.url, currency=currencies[0] if currencies else item.currency,
                           impact=impact, event_time=item.event_time, actual=item.actual, forecast=item.forecast,
                           previous=item.previous, rule_bias=bias, raw=item.raw)
            row.currencies = currencies
            row.symbols = symbols
            session.add(row)
            new_rows.append(row)
        session.flush()
        new_ids = [r.id for r in new_rows]
        payloads = [news_out(r) for r in sorted(new_rows, key=lambda r: r.event_time or utcnow())]
    for payload in payloads:
        broadcaster.publish("news", payload)
    return new_ids, updated


def _ff_marker() -> Path:
    url = get_settings().database_url
    base = Path(url.removeprefix("sqlite:///")).parent if url.startswith("sqlite:///") else Path(".")
    return base / ".ff_last_fetch"


def seconds_until_calendar_due() -> float:
    """The FF feed is rate limited; survive restarts (uvicorn --reload) without refetching."""
    try:
        age = time.time() - _ff_marker().stat().st_mtime
    except OSError:
        return 0.0
    return max(0.0, get_settings().ff_poll_minutes * 60 - age)


async def run_calendar_cycle() -> IngestResult:
    try:  # mark every attempt (not just successes) so restarts after a 429 also back off
        _ff_marker().parent.mkdir(parents=True, exist_ok=True)
        _ff_marker().touch()
    except OSError:
        pass
    try:
        items = await _ff_source.fetch()
    except Exception as exc:  # noqa: BLE001
        log.warning("calendar fetch failed: %s", exc)
        return IngestResult(inserted=0, analyzed=0, errors=[f"forexfactory: {exc}"])
    new_ids, updated = store_items(items)
    state["last_ingest_at"] = utcnow()
    log.info("calendar: %d items, %d new, %d updated", len(items), len(new_ids), updated)
    return IngestResult(inserted=len(new_ids), analyzed=0)


async def run_rss_cycle() -> IngestResult:
    settings = get_settings()
    try:
        from app.sources.rss import build_rss_sources
    except ImportError:
        return IngestResult(inserted=0, analyzed=0, errors=["rss module not installed"])

    sources = build_rss_sources(settings)
    results = await asyncio.gather(*(s.fetch() for s in sources), return_exceptions=True)
    items: list[RawNews] = []
    errors: list[str] = []
    for src, res in zip(sources, results):
        if isinstance(res, BaseException):
            errors.append(f"{src.name}: {res}")
            log.warning("rss %s failed: %s", src.name, res)
        else:
            items.extend(res)
    new_ids, _ = store_items(items)
    if any(not isinstance(res, BaseException) for res in results):
        state["last_ingest_at"] = utcnow()  # health must not look fresh while every feed is failing

    analyzed = skipped_cooldown = 0
    if settings.auto_analyze_headlines and settings.llm_enabled and new_ids:
        fresh_after = utcnow() - timedelta(minutes=HEADLINE_FRESH_MINUTES)
        watch = set(settings.watch_currencies)
        with session_scope() as session:
            rows = session.scalars(select(NewsItem).where(NewsItem.id.in_(new_ids))).all()
            candidates = [(r.id, r.currency or r.currencies[0])  # newest first, with the primary currency
                          for r in sorted(rows, key=lambda r: r.event_time or utcnow(), reverse=True)
                          if r.impact == "High" and r.event_time and r.event_time >= fresh_after
                          and watch.intersection(r.currencies)]
        for news_id, currency in candidates:
            if analyzed >= settings.max_headline_analyses_per_cycle:
                break
            if _budget_left(settings.max_auto_llm_calls_per_hour) <= 0:
                log.warning("auto LLM budget (%d/hour) spent; skipping the remaining headline analyses",
                            settings.max_auto_llm_calls_per_hour)
                break
            if _cooldown_left(currency, settings.headline_cooldown_minutes) > 0:
                skipped_cooldown += 1  # an analysis of this currency is recent; the newest headline won it
                continue
            _headline_cooldown[currency] = _now_ts()
            _auto_llm_calls.append(_now_ts())
            _spawn(_analyze_bg(news_id, "headline"))
            analyzed += 1
    log.info("rss: %d items, %d new, %d queued for analysis (%d skipped by cooldown)",
             len(items), len(new_ids), analyzed, skipped_cooldown)
    return IngestResult(inserted=len(new_ids), analyzed=analyzed, errors=errors)


async def run_pre_event_cycle() -> int:
    """Analyse upcoming high-impact calendar events once, shortly before release."""
    settings = get_settings()
    min_rank = IMPACT_RANK.get(settings.analyze_min_impact, 3)
    impacts = [k for k, v in IMPACT_RANK.items() if v >= min_rank and k != "Holiday"]
    now = utcnow()
    with session_scope() as session:
        rows = session.scalars(select(NewsItem).where(
            NewsItem.kind == "calendar", NewsItem.impact.in_(impacts), NewsItem.pre_event_alerted.is_(False),
            NewsItem.event_time > now,
            NewsItem.event_time <= now + timedelta(minutes=settings.pre_event_alert_minutes),
        ).order_by(NewsItem.event_time)).all()
        groups: dict[tuple, list[NewsItem]] = {}
        for row in rows:
            if row.currency in settings.watch_currencies:
                groups.setdefault((row.currency, row.event_time), []).append(row)
            row.pre_event_alerted = True  # mark first so a slow LLM call can't trigger duplicates
        leaders = [g[0].id for g in groups.values()]
    for news_id in leaders:
        _pre_event_attempts[news_id] = _pre_event_attempts.get(news_id, 0) + 1
        if settings.llm_enabled:  # counted against the hourly budget, but never blocked by it
            _auto_llm_calls.append(_now_ts())
        _spawn(_analyze_bg(news_id, "pre_event"))
    if leaders:
        log.info("pre-event analyses queued: %s", leaders)
    return len(leaders)


async def run_all(force: bool = False) -> IngestResult:
    """One manual round. The ForexFactory feed is rate limited, so it is skipped (with the reason in
    ``notes``, not ``errors``: it is expected, not a failure) while its poll interval has not elapsed, unless ``force`` is set."""
    wait = seconds_until_calendar_due()
    if wait > 0 and not force:
        cal = IngestResult(inserted=0, analyzed=0, notes=[
            f"ForexFactory calendar fetched recently (rate limited); next refresh in {wait:.0f}s"])
    else:
        cal = await run_calendar_cycle()
    rss = await run_rss_cycle()
    pre = await run_pre_event_cycle()
    return IngestResult(inserted=cal.inserted + rss.inserted, analyzed=rss.analyzed + pre,
                        errors=cal.errors + rss.errors, notes=cal.notes + rss.notes)


async def drain_background(timeout: float = 5.0) -> None:
    if _background:
        await asyncio.wait(list(_background), timeout=timeout)
