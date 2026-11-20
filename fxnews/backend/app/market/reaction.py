"""Market reaction to an event: what the price did after ``time`` for one symbol.

Wraps the pure maths of ``app.backtest.outcomes`` (same price conventions and per-asset-class flat thresholds as
the backtest) with candle selection, stored-outcome lookup (``bt_outcomes``) and a result cache.

Labels are ``up`` / ``down`` / ``sideways`` (the backtest's ``flat`` is renamed here).
Per-horizon status: ``ok`` | ``pending`` (not elapsed yet) | ``unavailable`` (no candle that fine / market closed).
Overall status: ``ok`` | ``pending`` (event in the future) | ``no_data`` | ``unsupported_symbol`` | ``error``.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.models import HORIZON_MINUTES, HORIZONS, BtEvent, BtOutcome
from app.backtest.outcomes import INTERVAL_SECONDS, MAX_EXCURSION_STEP, OutcomeData, compute_outcome
from app.market import candles as candle_service
from app.market.symbols import get_symbol

log = logging.getLogger("fxnews.reaction")

INTERVAL_CHAIN = ("5m", "15m", "1h", "1d")  # finest first
_RANGE_BUCKETS_DAYS = (5, 30, 60, 365, 730)  # few distinct ranges -> candle cache is shared between events
FINAL_TTL_S = 24 * 3600.0
LIVE_TTL_S = 60.0
NO_DATA_TTL_S = 300.0
CACHE_MAX = 4096
MAX_CONCURRENCY = 8

_cache: OrderedDict[tuple[str, int], tuple[float, dict[str, Any]]] = OrderedDict()


def clear_cache() -> None:
    _cache.clear()


def _label(direction: str | None) -> str | None:
    return "sideways" if direction == "flat" else direction


def parse_horizons(value: str | list[str] | None) -> list[str]:
    if value is None or value == "" or value == []:
        return list(HORIZONS)
    items = value.split(",") if isinstance(value, str) else value
    out = [h.strip().lower() for h in items if h and h.strip()]
    bad = [h for h in out if h not in HORIZON_MINUTES]
    if bad:
        raise ValueError(f"unknown horizon {bad}; use {', '.join(HORIZONS)}")
    return [h for h in HORIZONS if h in out]


def to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _range_for_age(age_days: float) -> str:
    need = age_days + 3
    for d in _RANGE_BUCKETS_DAYS:
        if need <= d:
            return f"{d}d"
    return "max"


def _horizon_rows(out: OutcomeData | None, event_ts: int, now_ts: int, horizons: list[str]) -> list[dict[str, Any]]:
    rows = []
    for h in horizons:
        ret = out.returns.get(h) if out else None
        if ret is not None:
            status = "ok"
        elif event_ts + HORIZON_MINUTES[h] * 60 > now_ts:
            status = "pending"
        else:
            status = "unavailable"
        rows.append({
            "horizon": h,
            "price": out.prices.get(h) if out else None,
            "return_pct": ret,
            "label": _label(out.directions.get(h)) if out and ret is not None else None,
            "status": status,
        })
    return rows


def _from_outcome_data(out: OutcomeData, event_ts: int, now_ts: int, source: str) -> dict[str, Any]:
    hour_over = event_ts + 3600 <= now_ts
    return {
        "status": "ok",
        "source": source,
        "interval": out.interval,
        "price_at_release": out.price_at_release,
        "horizons": _horizon_rows(out, event_ts, now_ts, list(HORIZONS)),
        "max_up_1h": out.max_up_1h,
        "max_down_1h": out.max_down_1h,
        "max_status": "ok" if out.max_up_1h is not None else ("pending" if not hour_over else "unavailable"),
        "complete": out.complete,
    }


# ── stored outcomes (bt_outcomes) ───────────────────────────────────────────


def _stored_to_outcome(row: BtOutcome) -> OutcomeData:
    out = OutcomeData(symbol=row.symbol, interval=row.interval, price_at_release=row.price_at_release)
    for h in HORIZONS:
        out.prices[h] = getattr(row, f"price_{h}")
        out.returns[h] = getattr(row, f"ret_{h}")
        out.directions[h] = getattr(row, f"dir_{h}")
    out.max_up_1h, out.max_down_1h = row.max_up_1h, row.max_down_1h
    out.complete = bool(row.complete)
    return out


def _stored_is_usable(row: BtOutcome, event_ts: int, now_ts: int) -> bool:
    """Stored values are final unless something that has elapsed since is still missing and could now exist."""
    if row.complete:
        return True
    step = INTERVAL_SECONDS.get(row.interval, 10**9)
    for h in HORIZONS:
        secs = HORIZON_MINUTES[h] * 60
        if event_ts + secs <= now_ts and secs >= step and getattr(row, f"ret_{h}") is None:
            return False
    return not (event_ts + 3600 <= now_ts and step <= MAX_EXCURSION_STEP and row.max_up_1h is None)


def lookup_stored(db: Session, symbol: str, when: datetime, now_ts: int) -> OutcomeData | None:
    """Stored outcome of an event at ``when`` (+-30 s) for ``symbol``; the finest-interval usable row wins."""
    lo, hi = when - timedelta(seconds=30), when + timedelta(seconds=30)
    rows = db.scalars(
        select(BtOutcome)
        .join(BtEvent, BtEvent.id == BtOutcome.event_id)
        .where(BtOutcome.symbol == symbol, BtEvent.time >= lo, BtEvent.time <= hi)
    ).all()
    usable = [r for r in rows if _stored_is_usable(r, int(when.timestamp()), now_ts)]
    if not usable:
        return None
    best = min(usable, key=lambda r: INTERVAL_SECONDS.get(r.interval, 10**9))
    return _stored_to_outcome(best)


# ── live computation ────────────────────────────────────────────────────────


async def _live_outcome(symbol: str, kind: str, event_ts: int, now_ts: int) -> OutcomeData | None:
    rng = _range_for_age((now_ts - event_ts) / 86400.0)
    last_error: Exception | None = None
    for interval in INTERVAL_CHAIN:
        try:
            res = await candle_service.get_candles(symbol, interval, rng)
        except candle_service.MarketDataError as exc:
            last_error = exc
            continue
        if not res.candles:
            continue
        out = compute_outcome(res.candles, event_ts, symbol=symbol, kind=kind, interval=interval, now_ts=now_ts)
        if out is not None:
            return out
    if last_error is not None:
        raise last_error
    return None


def _ttl(event_ts: int, now_ts: int, status: str) -> float:
    if status != "ok":
        return NO_DATA_TTL_S
    return FINAL_TTL_S if event_ts + 86400 + 3600 <= now_ts else LIVE_TTL_S


def _remember(key: tuple[str, int], value: dict[str, Any], ttl: float) -> None:
    _cache[key] = (_time.monotonic() + ttl, value)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


def _filter(result: dict[str, Any], horizons: list[str]) -> dict[str, Any]:
    out = dict(result)
    out["horizons"] = [h for h in result.get("horizons", []) if h["horizon"] in horizons]
    return out


async def get_reaction(
    symbol: str,
    when: datetime,
    horizons: list[str] | None = None,
    *,
    db: Session | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reaction of ``symbol`` to an event at ``when``. Never raises for data problems: see ``status``."""
    horizons = horizons or list(HORIZONS)
    sym = (symbol or "").strip().upper()
    when = to_utc(when)
    event_ts = int(when.timestamp())
    now_ts = int((now or datetime.now(UTC)).timestamp())
    base: dict[str, Any] = {"symbol": sym, "time": when.isoformat().replace("+00:00", "Z")}

    info = get_symbol(sym)
    if info is None:
        return {**base, "status": "unsupported_symbol", "horizons": [], "message": f"Unsupported symbol {symbol!r}"}
    if event_ts > now_ts:
        return {**base, "status": "pending", "source": None, "interval": None, "price_at_release": None,
                "horizons": _horizon_rows(None, event_ts, now_ts, horizons),
                "max_up_1h": None, "max_down_1h": None, "max_status": "pending", "complete": False}

    key = (sym, event_ts)
    hit = _cache.get(key)
    if hit and hit[0] > _time.monotonic() and now is None:
        return {**base, **_filter(hit[1], horizons)}

    result: dict[str, Any] | None = None
    if db is not None:
        try:
            stored = lookup_stored(db, sym, when, now_ts)
        except Exception:  # a broken DB must not hide live data
            log.exception("reaction: stored outcome lookup failed")
            stored = None
        if stored is not None:
            result = _from_outcome_data(stored, event_ts, now_ts, "backtest")
    if result is None:
        try:
            live = await _live_outcome(sym, info.kind, event_ts, now_ts)
        except (candle_service.MarketDataError, candle_service.InvalidRequestError, ValueError) as exc:
            log.warning("reaction %s @ %s failed: %s", sym, when, exc)
            return {**base, "status": "error", "horizons": [], "message": f"Market data unavailable: {exc}"[:240]}
        if live is None:
            result = {"status": "no_data", "source": None, "interval": None, "price_at_release": None,
                      "horizons": _horizon_rows(None, event_ts, now_ts, list(HORIZONS)),
                      "max_up_1h": None, "max_down_1h": None, "max_status": "unavailable", "complete": False,
                      "message": "No price at that time (market closed or older than the available candles)"}
        else:
            result = _from_outcome_data(live, event_ts, now_ts, "live")
    if now is None:
        _remember(key, result, _ttl(event_ts, now_ts, result["status"]))
    return {**base, **_filter(result, horizons)}


async def get_reactions(
    items: list[tuple[str, datetime]],
    horizons: list[str] | None = None,
    *,
    db: Session | None = None,
) -> list[dict[str, Any]]:
    """Several (symbol, time) pairs; computed concurrently (candles are cached/single-flight per symbol+interval)."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    unique: dict[tuple[str, int], asyncio.Task] = {}

    async def one(sym: str, when: datetime) -> dict[str, Any]:
        async with sem:
            return await get_reaction(sym, when, horizons, db=db)

    for sym, when in items:
        k = ((sym or "").strip().upper(), int(to_utc(when).timestamp()))
        if k not in unique:
            unique[k] = asyncio.ensure_future(one(sym, when))
    done = {k: await t for k, t in unique.items()}
    return [done[((s or "").strip().upper(), int(to_utc(w).timestamp()))] for s, w in items]


__all__ = ["get_reaction", "get_reactions", "parse_horizons", "clear_cache"]
