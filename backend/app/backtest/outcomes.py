"""Outcome computation: what the price did after an event. Pure functions (no I/O) so they are easy to test.

Conventions (all returns are in percent, ``(price / price_at_release - 1) * 100``):
* ``price_at_release`` = close of the last *finished* candle at the event time (for a release on a candle boundary
  that is the last price before the release). It must be at most ~2 candles old, otherwise the market was closed
  (weekend / session break) and there is no outcome.
* price at +h = close of the last finished candle at ``time + h``, again at most ~2 candles old, and the horizon
  must already have elapsed (``time + h <= now``) -- otherwise the field stays NULL and is filled in by a later run.
* ``max_up_1h`` / ``max_down_1h`` = highest high / lowest low of the candles covering the first hour after the
  event, relative to ``price_at_release`` (>= 0 / <= 0). Only set once the hour has elapsed.
* direction label per horizon = ``up`` / ``down`` when ``|return|`` reaches the flat threshold, else ``flat``.
  The threshold depends on the instrument and grows with sqrt(horizon) (random-walk scaling).
* resolution: the candle interval decides which numbers exist. A horizon shorter than one candle (e.g. +5m / +15m on
  1h candles) stays NULL -- there is no price that fine -- and the 1h excursions need candles of 15m or finer.
  ``OutcomeData.interval`` records the interval used; the backfill picks the finest one that covers the event.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.backtest.models import HORIZON_MINUTES, HORIZONS
from app.schemas import Candle

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
MAX_EXCURSION_STEP = 900  # coarser candles would mix pre-release price action into the first-hour high/low

# "flat" band at the 1h horizon, in percent, by instrument kind. Other horizons scale with sqrt(h / 1h).
FLAT_PCT_1H: dict[str, float] = {"fx": 0.04, "metal": 0.10, "energy": 0.20, "index": 0.12, "crypto": 0.30}
DEFAULT_FLAT_PCT_1H = 0.05
MIN_FLAT_PCT = 0.005


def flat_threshold_pct(kind: str | None, horizon_minutes: int) -> float:
    base = FLAT_PCT_1H.get(kind or "", DEFAULT_FLAT_PCT_1H)
    return max(MIN_FLAT_PCT, base * math.sqrt(horizon_minutes / 60.0))


def label_direction(ret_pct: float | None, kind: str | None, horizon_minutes: int) -> str | None:
    if ret_pct is None:
        return None
    threshold = flat_threshold_pct(kind, horizon_minutes)
    if ret_pct >= threshold:
        return "up"
    if ret_pct <= -threshold:
        return "down"
    return "flat"


@dataclass
class OutcomeData:
    symbol: str
    interval: str
    price_at_release: float
    prices: dict[str, float | None] = field(default_factory=dict)  # horizon -> price
    returns: dict[str, float | None] = field(default_factory=dict)  # horizon -> percent
    directions: dict[str, str | None] = field(default_factory=dict)  # horizon -> up/down/flat
    max_up_1h: float | None = None
    max_down_1h: float | None = None
    direction: str | None = None  # label at the primary horizon
    direction_horizon: str | None = None
    complete: bool = False  # all horizons have elapsed: nothing left to fill in


def _last_finished_close(candles: list[Candle], ends: list[int], at: int, tolerance: int) -> float | None:
    """Close of the last candle whose end <= ``at`` and that ends within ``tolerance`` seconds of it."""
    lo, hi = 0, len(ends)
    while lo < hi:  # bisect_right on ends
        mid = (lo + hi) // 2
        if ends[mid] <= at:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    if at - ends[lo - 1] > tolerance:
        return None
    return candles[lo - 1].close


def compute_outcome(
    candles: list[Candle],
    event_ts: int,
    *,
    symbol: str,
    kind: str | None = None,
    interval: str = "5m",
    now_ts: int | None = None,
    primary_horizon: str = "1h",
) -> OutcomeData | None:
    """Outcome of an event at unix time ``event_ts`` from ascending ``candles``; None when there is no price then."""
    step = INTERVAL_SECONDS[interval]
    if not candles:
        return None
    candles = sorted(candles, key=lambda c: c.time)
    ends = [c.time + step for c in candles]
    tolerance = 2 * step

    p0 = _last_finished_close(candles, ends, event_ts, tolerance)
    if p0 is None or p0 <= 0:
        return None

    out = OutcomeData(symbol=symbol, interval=interval, price_at_release=p0)
    for h in HORIZONS:
        target = event_ts + HORIZON_MINUTES[h] * 60
        price = None
        if HORIZON_MINUTES[h] * 60 < step:
            pass  # finer than the candles: no honest price (stays NULL, as does its label)
        elif now_ts is None or target <= now_ts:
            price = _last_finished_close(candles, ends, target, tolerance)
        ret = (price / p0 - 1.0) * 100.0 if price else None
        out.prices[h] = price
        out.returns[h] = ret
        out.directions[h] = label_direction(ret, kind, HORIZON_MINUTES[h])

    hour_end = event_ts + 3600
    if step <= MAX_EXCURSION_STEP and (now_ts is None or hour_end <= now_ts):
        window = [c for c, end in zip(candles, ends) if end > event_ts and c.time < hour_end]
        if window:
            out.max_up_1h = max(0.0, (max(c.high for c in window) / p0 - 1.0) * 100.0)
            out.max_down_1h = min(0.0, (min(c.low for c in window) / p0 - 1.0) * 100.0)

    out.direction_horizon = primary_horizon if primary_horizon in HORIZONS else "1h"
    out.direction = out.directions.get(out.direction_horizon)
    # Final once every horizon has elapsed (a horizon inside a weekend gap stays NULL for good).
    out.complete = now_ts is None or event_ts + HORIZON_MINUTES["1d"] * 60 <= now_ts
    return out
