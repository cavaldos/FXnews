"""Prediction runner: ask one or more LLMs, per event, which way each related symbol will move.

Optional and never started automatically. Each (event, model) is ONE call that answers for every related symbol of
the event; the answer is stored as one ``bt_predictions`` row per symbol. (event, model) pairs that already have
predictions are skipped, so the runner is resumable and cheap to re-run. Rate limits: each model works through its
events sequentially with ``backtest_llm_min_interval_seconds`` between calls (the provider layer adds retries and
key rotation), and at most ``backtest_llm_concurrency`` calls are in flight.

The prompt contains only what was known at release time (title, summary, actual/forecast/previous) plus a short
"Earlier context" list of related items published STRICTLY before the release: no prices, outcomes or other models'
answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select

from app.analysis.llm import extract_json
from app.backtest.ingest import utc
from app.backtest.models import HORIZON_MINUTES, BtEvent, BtPrediction
from app.config import get_settings
from app.db import session_scope, utcnow
from app.llm import LLMClient, LLMError, get_client, parse_model_spec
from app.market.symbols import get_symbol

log = logging.getLogger(__name__)

IMPACT_RANK = {"low": 1, "medium": 2, "high": 3}
MAX_CONSECUTIVE_ERRORS = 4  # a model failing this many times in a row (bad key, no quota...) is abandoned for the run

SYSTEM_PROMPT = (
    "You are a markets analyst. You are shown ONE news item or economic release exactly as it was known the moment it "
    "came out (no price data, no hindsight). For every listed symbol predict the direction of its price over the given "
    "horizon, measured from the moment of release. The 'Earlier context' section, when present, only contains "
    "information published before the release. Answer with a single JSON object and nothing else."
)


@dataclass(frozen=True)
class ContextItem:
    """A related item published before the release being predicted (plain data: safe to use outside a session)."""

    time: datetime
    kind: str
    impact: str
    title: str
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None


def load_context(event: BtEvent, *, now: datetime | None = None) -> list[ContextItem]:
    """Up to ``backtest_prompt_context_items`` other events from the ``backtest_prompt_context_hours`` before the
    release that share its currency or symbols, newest first. Strictly earlier than the release: nothing published
    at or after it can leak into the prompt. For items still in the future (forecast mode) ``actual`` is dropped."""
    settings = get_settings()
    limit = settings.backtest_prompt_context_items
    if limit <= 0:
        return []
    now = utc(now or utcnow())
    release = utc(event.time)
    floor = release - timedelta(hours=settings.backtest_prompt_context_hours)
    related = [BtEvent.symbols_csv.like(f"%,{s},%") for s in event.symbols]
    if event.currency:
        related.append(BtEvent.currency == event.currency)
    if not related:
        return []
    with session_scope() as session:
        rows = session.scalars(select(BtEvent).where(
            BtEvent.time < release, BtEvent.time >= floor, BtEvent.id != event.id, or_(*related)
        ).order_by(BtEvent.time.desc(), BtEvent.id.desc()).limit(limit)).all()
        return [ContextItem(time=utc(r.time), kind=r.kind, impact=r.impact, title=r.title,
                            actual=None if utc(r.time) > now else r.actual, forecast=r.forecast, previous=r.previous)
                for r in rows]


def build_prompt(event: BtEvent, symbols: list[str], horizon: str, context: list[ContextItem] | None = None,
                 *, now: datetime | None = None) -> str:
    """The user prompt. ``context`` items at/after the release time are dropped as a last line of defence."""
    now = utc(now or utcnow())
    release = utc(event.time)
    lines = [
        f"Release time (UTC): {event.time:%Y-%m-%d %H:%M}",
        f"Type: {'economic calendar release' if event.kind == 'calendar' else 'news headline'}",
        f"Source: {event.source}",
        f"Currency/country: {event.currency or '-'}{f' ({event.country})' if event.country else ''}",
        f"Importance: {event.impact}",
        f"Title: {event.title}",
    ]
    if event.summary and event.kind == "headline":
        lines.append(f"Summary: {event.summary[:500]}")
    if event.kind == "calendar":
        actual = event.actual if release <= now else None  # not released yet: the actual cannot be known
        lines.append(f"Actual: {actual or 'n/a'} | Forecast: {event.forecast or 'n/a'} | Previous: {event.previous or 'n/a'}")
    earlier = [c for c in (context or []) if utc(c.time) < release]
    if earlier:
        lines += ["", "Earlier context (published before this release, newest first):"]
        for c in earlier:
            line = f"- {utc(c.time):%Y-%m-%d %H:%M} UTC | {c.kind} | {c.impact} | {c.title[:200]}"
            if c.kind == "calendar":
                actual = c.actual if utc(c.time) <= now else None
                line += f" | Actual: {actual or 'n/a'} | Forecast: {c.forecast or 'n/a'} | Previous: {c.previous or 'n/a'}"
            lines.append(line)
    pairs = []
    for sym in symbols:
        info = get_symbol(sym)
        pairs.append(f"{sym} (up = {info.base} gains vs {info.quote})" if info else sym)
    lines += [
        "",
        f"Symbols: {', '.join(pairs)}",
        f"Horizon: {horizon} after the release.",
        "",
        'Reply as JSON: {"predictions": [{"symbol": "<SYMBOL>", "direction": "up|down|flat", "confidence": <0-100>, '
        f'"horizon": "{horizon}", "reasoning": "<one or two sentences>"}}, ...]}}',
        'Use "flat" when you expect no meaningful move. Confidence is your probability (0-100) that the direction is right.',
    ]
    return "\n".join(lines)


_DIRS = {"up": "up", "long": "up", "buy": "up", "bullish": "up", "higher": "up", "rise": "up",
         "down": "down", "short": "down", "sell": "down", "bearish": "down", "lower": "down", "fall": "down",
         "flat": "flat", "neutral": "flat", "none": "flat", "sideways": "flat", "range": "flat", "hold": "flat"}


def parse_predictions(text: str, symbols: list[str], horizon: str) -> list[dict]:
    """Rows ``{symbol, direction, confidence, horizon, reasoning}`` from a model reply; unknown symbols are dropped."""
    data = extract_json(text)
    items = data.get("predictions")
    if items is None and "direction" in data:  # a single bare prediction
        items = [data]
    if not isinstance(items, list):
        raise ValueError("no 'predictions' list in the reply")
    wanted = {s.upper() for s in symbols}
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or (symbols[0] if len(symbols) == 1 else "")).upper().replace("/", "")
        direction = _DIRS.get(str(item.get("direction") or "").strip().lower())
        if symbol not in wanted or direction is None or symbol in out:
            continue
        try:
            confidence = int(round(float(str(item.get("confidence")).rstrip("%"))))
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            confidence = max(0, min(100, confidence))
        h = str(item.get("horizon") or horizon).lower()
        out[symbol] = {"symbol": symbol, "direction": direction, "confidence": confidence,
                       "horizon": h if h in HORIZON_MINUTES else horizon,
                       "reasoning": (str(item.get("reasoning") or "").strip() or None)}
    if not out:
        raise ValueError("reply has no usable prediction for the listed symbols")
    return list(out.values())


async def predict_event(event: BtEvent, symbols: list[str], spec: str, client: LLMClient, horizon: str,
                        context: list[ContextItem] | None = None, *, sem: asyncio.Semaphore | None = None,
                        now: datetime | None = None) -> tuple[list[dict], int]:
    """ONE model call for one event covering all ``symbols``. Returns (parsed rows, latency in ms); raises
    ``LLMError`` / ``ValueError`` when the call or the reply is unusable. Nothing is stored here."""
    prompt = build_prompt(event, symbols, horizon, context, now=now)
    started = time.perf_counter()
    if sem is not None:
        async with sem:
            reply = await client.achat_spec(spec, prompt, system=SYSTEM_PROMPT, temperature=0.0,
                                            max_tokens=get_settings().llm_max_tokens)
    else:
        reply = await client.achat_spec(spec, prompt, system=SYSTEM_PROMPT, temperature=0.0,
                                        max_tokens=get_settings().llm_max_tokens)
    rows = parse_predictions(reply.text, symbols, horizon)
    return rows, int((time.perf_counter() - started) * 1000)


def _provider_problem(client: LLMClient, spec: str) -> str | None:
    provider_name, _model = parse_model_spec(spec, client.settings.default_provider)
    try:
        return client.provider(provider_name).problem()
    except LLMError as exc:
        return str(exc)


@dataclass
class PredictResult:
    models: list[str] = field(default_factory=list)
    events_considered: int = 0
    calls: int = 0
    predictions_created: int = 0
    skipped_existing: int = 0  # (event, model) pairs already predicted
    errors: list[str] = field(default_factory=list)
    models_unavailable: dict[str, str] = field(default_factory=dict)
    per_model: dict[str, int] = field(default_factory=dict)  # model -> predictions stored

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _select_events(start: datetime, end: datetime, min_impact: str, symbols: list[str] | None, now: datetime):
    floor = IMPACT_RANK.get(min_impact.lower(), 2)
    with session_scope() as session:
        rows = session.scalars(select(BtEvent).where(BtEvent.time >= start, BtEvent.time < end, BtEvent.time <= now,
                                                     BtEvent.symbols_csv != "").order_by(BtEvent.time)).all()
        events = [e for e in rows if IMPACT_RANK.get(e.impact, 1) >= floor]
        out = []
        for e in events:
            syms = [s for s in e.symbols if not symbols or s in symbols]
            if syms:
                session.expunge(e)
                out.append((e, syms))
        return out


def _existing(model: str, event_ids: list[int]) -> set[int]:
    done: set[int] = set()
    with session_scope() as session:
        for i in range(0, len(event_ids), 500):
            done.update(session.scalars(select(BtPrediction.event_id).where(
                BtPrediction.model == model, BtPrediction.event_id.in_(event_ids[i:i + 500]))))
    return done


def _store(event_id: int, model: str, rows: list[dict], latency_ms: int, *, replace: bool = False) -> int:
    with session_scope() as session:
        if replace:
            session.execute(delete(BtPrediction).where(BtPrediction.event_id == event_id, BtPrediction.model == model))
        present = {s for s in session.scalars(select(BtPrediction.symbol).where(
            BtPrediction.event_id == event_id, BtPrediction.model == model))}
        n = 0
        for r in rows:
            if r["symbol"] in present:
                continue
            session.add(BtPrediction(event_id=event_id, model=model, latency_ms=latency_ms, created_at=utcnow(), **r))
            n += 1
        return n


async def run_predictions(start: datetime, end: datetime, models: list[str], *, min_impact: str = "medium",
                          limit: int | None = None, symbols: list[str] | None = None,
                          client: LLMClient | None = None, now: datetime | None = None,
                          progress: Callable[[str], None] | None = None) -> PredictResult:
    """Run ``models`` (``provider:model`` specs) over stored events in ``[start, end)``.

    ``limit`` caps the number of NEW (event, model) calls per model, newest-first is not assumed: oldest events first.
    """
    settings = get_settings()
    client = client or get_client()
    say = progress or (lambda _m: None)
    now = utc(now or utcnow())
    start, end = utc(start), utc(end)
    horizon = settings.backtest_primary_horizon
    result = PredictResult(models=list(dict.fromkeys(models)))
    wanted_symbols = [s.upper() for s in symbols] if symbols else None

    pairs = _select_events(start, end, min_impact, wanted_symbols, now)
    result.events_considered = len(pairs)
    sem = asyncio.Semaphore(max(1, settings.backtest_llm_concurrency))

    async def work(spec: str) -> None:
        problem = _provider_problem(client, spec)
        if problem:
            result.models_unavailable[spec] = problem
            return
        done = _existing(spec, [e.id for e, _ in pairs])
        todo = [(e, s) for e, s in pairs if e.id not in done]
        result.skipped_existing += len(pairs) - len(todo)
        if limit is not None:
            todo = todo[:limit]
        failures = 0
        for n, (event, syms) in enumerate(todo, 1):
            try:
                rows, latency_ms = await predict_event(event, syms, spec, client, horizon, load_context(event, now=now),
                                                       sem=sem, now=now)
            except (LLMError, ValueError) as exc:
                failures += 1
                result.errors.append(f"{spec} event {event.id}: {exc}"[:300])
                log.warning("backtest predict %s event %s failed: %s", spec, event.id, exc)
                if failures >= MAX_CONSECUTIVE_ERRORS:
                    result.errors.append(f"{spec}: stopped after {failures} consecutive errors")
                    break
            else:
                failures = 0
                stored = _store(event.id, spec, rows, latency_ms)
                result.predictions_created += stored
                result.per_model[spec] = result.per_model.get(spec, 0) + stored
            finally:
                result.calls += 1
            say(f"{spec}: {n}/{len(todo)}")
            if n < len(todo):
                await asyncio.sleep(max(0.0, settings.backtest_llm_min_interval_seconds))

    await asyncio.gather(*(work(m) for m in result.models))
    return result


async def predict_one(event_id: int, models: list[str], *, force: bool = False, client: LLMClient | None = None,
                      now: datetime | None = None) -> list[dict]:
    """Ask each of ``models`` about ONE stored event (concurrently, within ``backtest_llm_concurrency``).

    Per model: ``{"model", "status": "ok"|"skipped"|"error", "error", "predictions_created", "latency_ms"}``.
    A model that already has predictions for the event is skipped unless ``force``; with ``force`` its old rows are
    replaced once the new answer parsed (a failed call keeps what was there). Raises ``LookupError`` when the event
    does not exist and ``ValueError`` when it has no symbols.
    """
    settings = get_settings()
    client = client or get_client()
    with session_scope() as session:
        event = session.get(BtEvent, event_id)
        if event is None:
            raise LookupError("event not found")
        session.expunge(event)
    symbols = event.symbols
    if not symbols:
        raise ValueError("event has no related symbols")
    horizon = settings.backtest_primary_horizon
    now = utc(now or utcnow())
    context = load_context(event, now=now)  # same for every model; computed once
    sem = asyncio.Semaphore(max(1, settings.backtest_llm_concurrency))
    specs = list(dict.fromkeys(models))

    async def one(spec: str) -> dict:
        out = {"model": spec, "status": "ok", "error": None, "predictions_created": 0, "latency_ms": None}
        if not force and event_id in _existing(spec, [event_id]):
            out["status"] = "skipped"
            return out
        problem = _provider_problem(client, spec)
        if problem:
            return {**out, "status": "error", "error": problem[:300]}
        try:
            rows, latency_ms = await predict_event(event, symbols, spec, client, horizon, context, sem=sem, now=now)
        except (LLMError, ValueError) as exc:
            log.warning("backtest predict %s event %s failed: %s", spec, event_id, exc)
            return {**out, "status": "error", "error": str(exc)[:300]}
        out["predictions_created"] = _store(event_id, spec, rows, latency_ms, replace=force)
        out["latency_ms"] = latency_ms
        return out

    return list(await asyncio.gather(*(one(m) for m in specs)))
