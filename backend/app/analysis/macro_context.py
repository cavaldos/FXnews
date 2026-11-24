"""Optional macro snapshot (FRED series via the Vibe-Trading sidecar) for the advisor prompt. Never raises."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

from app.config import get_settings

log = logging.getLogger("fxnews.macro")

FAIL_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class Series:
    id: str
    label: str
    transform: str = "level"  # "level" | "yoy" (YoY % of an index)


SERIES: dict[str, list[Series]] = {
    "USD": [Series("DFF", "US fed funds rate (%)"), Series("DGS2", "US 2y Treasury yield (%)"),
            Series("DGS10", "US 10y Treasury yield (%)"), Series("CPIAUCSL", "US CPI YoY (%)", "yoy"),
            Series("UNRATE", "US unemployment rate (%)")],
    "EUR": [Series("ECBDFR", "ECB deposit facility rate (%)"), Series("IRLTLT01DEM156N", "Germany 10y yield (%)")],
    "GBP": [Series("IRLTLT01GBM156N", "UK 10y gilt yield (%)")],
    "JPY": [Series("IRLTLT01JPM156N", "Japan 10y JGB yield (%)")],
    "AUD": [Series("IRLTLT01AUM156N", "Australia 10y yield (%)")],
    "CAD": [Series("IRLTLT01CAM156N", "Canada 10y yield (%)")],
    "CHF": [Series("IRLTLT01CHM156N", "Switzerland 10y yield (%)")],
    "NZD": [Series("IRLTLT01NZM156N", "New Zealand 10y yield (%)")],
}

# series id -> (expires_at monotonic, summary line or None for a remembered failure)
_cache: dict[str, tuple[float, str | None]] = {}


def clear_cache() -> None:
    _cache.clear()


def _at_or_before(obs: list[tuple[date, float]], target: date) -> float | None:
    for d, v in reversed(obs):
        if d <= target:
            return v
    return None


def summarise(s: Series, raw: list) -> str | None:
    obs: list[tuple[date, float]] = []
    for o in raw:
        try:
            if o.value is None:
                continue
            obs.append((date.fromisoformat(str(o.date)[:10]), float(o.value)))
        except (ValueError, TypeError):
            continue
    obs.sort()
    if s.transform == "yoy":
        yoy = []
        for d, v in obs:
            try:
                prev = _at_or_before(obs, d.replace(year=d.year - 1))
            except ValueError:
                prev = _at_or_before(obs, d - timedelta(days=365))
            if prev:
                yoy.append((d, (v / prev - 1) * 100))
        obs = yoy
    if not obs:
        return None
    last_d, last_v = obs[-1]
    changes = []
    for name, days in (("1m", 30), ("3m", 91), ("1y", 365)):
        old = _at_or_before(obs, last_d - timedelta(days=days))
        if old is not None and obs[0][0] <= last_d - timedelta(days=days):
            changes.append(f"{name} {last_v - old:+.2f}")
    tail = f", change vs {' / '.join(changes)}" if changes else ""
    return f"{s.label}: {last_v:.2f} ({last_d.isoformat()}){tail}"


async def _fetch(s: Series, timeout: float) -> str | None:
    now = time.monotonic()
    hit = _cache.get(s.id)
    if hit and hit[0] > now:
        return hit[1]
    settings = get_settings()
    try:
        from app.vibe.client import get_client
        start = date.today() - timedelta(days=800)
        res = await asyncio.wait_for(get_client().get_macro_series(s.id, start=start, limit=1500), timeout)
        line = summarise(s, res.observations)
        _cache[s.id] = (now + settings.advisor_macro_ttl_minutes * 60, line)
        return line
    except Exception as exc:  # noqa: BLE001 - never break an analysis
        log.debug("macro series %s unavailable: %s", s.id, exc)
        _cache[s.id] = (now + FAIL_TTL_SECONDS, None)
        return None


async def macro_block(currencies: list[str]) -> str:
    try:
        wanted: list[Series] = []
        for c in dict.fromkeys(x.upper() for x in currencies if x):
            wanted += SERIES.get(c, [])
        if not wanted:
            return ""
        timeout = get_settings().advisor_macro_timeout_seconds
        lines = await asyncio.wait_for(asyncio.gather(*(_fetch(s, timeout) for s in wanted)), timeout + 1)
        lines = [ln for ln in lines if ln]
        if not lines:
            log.warning("macro snapshot unavailable (is FRED_API_KEY set on the sidecar?)") if not _warned() else None
            return ""
        return "# Macro snapshot (FRED via Vibe-Trading)\n" + "\n".join(f"- {ln}" for ln in lines)
    except Exception as exc:  # noqa: BLE001
        log.debug("macro_block failed: %s", exc)
        return ""


_warned_once = False


def _warned() -> bool:
    global _warned_once
    was, _warned_once = _warned_once, True
    return was


async def macro_suffix(currencies: list[str]) -> str:
    """Prompt suffix ('' when disabled / unavailable)."""
    s = get_settings()
    if not (s.advisor_macro_enabled and s.vibe_enabled):
        return ""
    block = await macro_block(currencies)
    return f"\n\n{block}" if block else ""
