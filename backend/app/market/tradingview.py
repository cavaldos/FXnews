"""TradingView historical bars over its public chart websocket (the one tradingview.com's own charts use).

Why: Yahoo/Twelve Data quote different instruments than the TradingView chart (e.g. gold futures vs OANDA
spot), so candles never matched. This module pulls the exact same series TradingView draws, keyed by the
same ``EXCHANGE:SYMBOL`` ticker (see ``SymbolInfo.tv``).

Protocol (unofficial, reverse engineered; see e.g. tvDatafeed / tradingview-ws):
- ``wss://data.tradingview.com/socket.io/websocket`` with ``Origin: https://www.tradingview.com``.
- Every message is framed ``~m~<len>~m~<payload>`` (several frames may share one websocket message).
  Payloads are JSON ``{"m": method, "p": params}`` or a heartbeat ``~h~<n>`` that must be echoed back
  (framed) or the server drops the connection.
- Client: ``set_auth_token ["unauthorized_user_token"]`` -> ``chart_create_session [cs, ""]`` ->
  ``switch_timezone [cs, "Etc/UTC"]`` -> ``resolve_symbol [cs, "sds_sym_1", "={json}"]`` ->
  ``create_series [cs, "sds_1", "s1", "sds_sym_1", <tv interval>, <bars>, ""]``.
- Server: ``timescale_update`` / ``du`` carry ``{"sds_1": {"s": [{"i": n, "v": [t, o, h, l, c, vol]}]}}``,
  then ``series_completed``. ``symbol_error`` / ``series_error`` / ``critical_error`` mean failure.

Bar ``t`` is the bar open time in unix seconds. The last bar is the still-forming one, like on the chart.
The anonymous token is limited to roughly 5000 bars per series.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import string
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from app.config import get_settings
from app.schemas import Candle

logger = logging.getLogger(__name__)

TV_WS_URL = "wss://data.tradingview.com/socket.io/websocket?from=chart%2F&type=chart"
TV_ORIGIN = "https://www.tradingview.com"
TV_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
AUTH_TOKEN = "unauthorized_user_token"
MAX_BARS = 5000  # anonymous sessions are capped around here
DEEP_BARS = 10000  # what a "give me all history" request asks for; the server then answers with its own cap (~5500)
DEFAULT_TIMEOUT_S = 15.0

# our Interval -> TradingView resolution
TV_INTERVALS: dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "1D",
}

_SERIES_ID = "sds_1"
_SYMBOL_ID = "sds_sym_1"
_FAIL_METHODS = frozenset({"symbol_error", "series_error", "critical_error"})


class TradingViewError(RuntimeError):
    """The TradingView feed failed. Catch a subclass to tell a per-symbol problem from a feed outage."""


class TradingViewSymbolError(TradingViewError):
    """A per-symbol problem: unknown ticker/series, no bars, or this one series timed out. The connection
    itself worked, so it says nothing about the feed being down."""


class TradingViewConnectionError(TradingViewError):
    """A connection-level failure: connect/handshake timeout, socket error, dropped websocket, or a
    server ``critical_error``. Repeated failures of this kind mean the feed is unreachable."""


class TradingViewRateLimitError(TradingViewError):
    """The websocket handshake was refused with HTTP 429. Not a generic connection failure: callers back off
    on their own schedule (see candles.py, ``tradingview-429``)."""


# ── framing ──────────────────────────────────────────────────────────


def encode_frame(payload: str | dict[str, Any]) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"


def encode_message(method: str, params: list[Any]) -> str:
    return encode_frame({"m": method, "p": params})


def parse_frames(raw: str) -> list[str]:
    """Split a websocket text message into frame payloads (JSON strings, ``~h~N`` heartbeats, or the
    initial handshake JSON). Stops at the first malformed frame."""
    out: list[str] = []
    pos, n = 0, len(raw)
    while pos < n:
        if not raw.startswith("~m~", pos):
            break
        end = raw.find("~m~", pos + 3)
        if end < 0:
            break
        try:
            size = int(raw[pos + 3 : end])
        except ValueError:
            break
        start = end + 3
        out.append(raw[start : start + size])
        pos = start + size
    return out


def _rand_session(prefix: str) -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase, k=12))


# ── parsing ──────────────────────────────────────────────────────────


def _finite(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def parse_series_bars(params: list[Any], series_id: str = _SERIES_ID) -> list[Candle]:
    """``p`` of a ``timescale_update`` / ``du`` message -> candles (ascending is not guaranteed here)."""
    if len(params) < 2 or not isinstance(params[1], dict):
        return []
    series = params[1].get(series_id)
    rows = series.get("s") if isinstance(series, dict) else None
    out: list[Candle] = []
    for row in rows or []:
        v = row.get("v") if isinstance(row, dict) else None
        if not isinstance(v, list) or len(v) < 5:
            continue
        t, o, h, l, c = (_finite(x) for x in v[:5])
        if t is None or o is None or h is None or l is None or c is None:
            continue
        vol = _finite(v[5]) if len(v) > 5 else None
        out.append(Candle(time=int(t), open=o, high=h, low=l, close=c, volume=vol))
    return out


def merge_bars(bars: dict[int, Candle], new: list[Candle]) -> None:
    for c in new:
        bars[c.time] = c  # a later update of the same bar (forming bar) wins


def tv_resolution(interval: str) -> str:
    try:
        return TV_INTERVALS[interval]
    except KeyError:
        raise ValueError(f"Unsupported interval {interval!r} for TradingView") from None


# ── session gate ─────────────────────────────────────────────────────
# Every fetch opens its own websocket. Without a gate, get_quotes (one session per symbol) plus a backfill
# job trip TradingView's anonymous rate limit (HTTP 429). The gate caps concurrent sessions and spaces out
# new connections. It is process-wide within one event loop and rebuilt when the loop or the limit changes.


@dataclass
class _SessionGate:
    loop: asyncio.AbstractEventLoop
    limit: int
    sem: asyncio.Semaphore
    pace_lock: asyncio.Lock
    last_connect: float = -math.inf  # time.monotonic() of the last connection attempt started


_gate: _SessionGate | None = None


def _get_gate() -> _SessionGate:
    global _gate
    loop = asyncio.get_running_loop()
    limit = max(1, int(get_settings().tradingview_max_sessions))
    if _gate is None or _gate.loop is not loop or _gate.limit != limit:
        _gate = _SessionGate(loop=loop, limit=limit, sem=asyncio.Semaphore(limit), pace_lock=asyncio.Lock())
    return _gate


def reset_session_gate() -> None:
    """Forget the gate (tests; a new event loop rebuilds it anyway)."""
    global _gate
    _gate = None


@asynccontextmanager
async def _session_slot() -> AsyncIterator[None]:
    """One TradingView session: wait for a free slot, then wait out the connect spacing. Time spent queued
    here is not part of the connect/collect timeouts."""
    gate = _get_gate()
    async with gate.sem:
        spacing = max(0, int(get_settings().tradingview_connect_spacing_ms)) / 1000.0
        async with gate.pace_lock:
            wait = gate.last_connect + spacing - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            gate.last_connect = time.monotonic()
        yield


def _handshake_status(exc: BaseException) -> int | None:
    """HTTP status of a rejected websocket handshake (websockets' InvalidStatus carries the response)."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


# ── client ───────────────────────────────────────────────────────────


def _handshake_messages(tv_symbol: str, resolution: str, bars: int) -> list[str]:
    session = _rand_session("cs_")
    symbol_spec = "=" + json.dumps({"symbol": tv_symbol, "adjustment": "splits", "session": "regular"}, separators=(",", ":"))
    return [
        encode_message("set_auth_token", [AUTH_TOKEN]),
        encode_message("chart_create_session", [session, ""]),
        encode_message("switch_timezone", [session, "Etc/UTC"]),
        encode_message("resolve_symbol", [session, _SYMBOL_ID, symbol_spec]),
        encode_message("create_series", [session, _SERIES_ID, "s1", _SYMBOL_ID, resolution, int(bars), ""]),
    ]


async def _connect(url: str):
    from websockets.asyncio.client import connect  # imported lazily: only this module needs it

    return await connect(
        url,
        origin=cast(Any, TV_ORIGIN),
        additional_headers={"User-Agent": TV_USER_AGENT},
        open_timeout=DEFAULT_TIMEOUT_S,
        close_timeout=2,
        max_size=16 * 1024 * 1024,
        ping_interval=None,  # TradingView has its own ~h~ heartbeat
    )


async def _collect(ws: Any, tv_symbol: str, resolution: str, bars: int) -> list[Candle]:
    for msg in _handshake_messages(tv_symbol, resolution, bars):
        await ws.send(msg)
    collected: dict[int, Candle] = {}
    while True:
        raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        for payload in parse_frames(raw):
            if payload.startswith("~h~"):
                await ws.send(encode_frame(payload))  # echo the heartbeat
                continue
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue  # e.g. the {"session_id": ...} handshake is a dict without "m"
            method, params = msg.get("m"), msg.get("p") or []
            if method in _FAIL_METHODS:
                detail = " ".join(str(x) for x in params[1:] if isinstance(x, (str, int))) or str(method)
                if method == "critical_error":
                    raise TradingViewConnectionError(f"{tv_symbol}: TradingView critical_error: {detail}")
                raise TradingViewSymbolError(f"{tv_symbol}: TradingView {method}: {detail}")
            if method in ("timescale_update", "du"):
                merge_bars(collected, parse_series_bars(params))
            elif method == "series_completed":
                return [collected[t] for t in sorted(collected)]


async def fetch_bars(
    tv_symbol: str,
    interval: str,
    bars: int,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    url: str = TV_WS_URL,
    limit: int = MAX_BARS,
) -> list[Candle]:
    """The last ``bars`` bars of ``tv_symbol`` (e.g. ``OANDA:XAUUSD``) at ``interval`` (our Interval),
    ascending by time.

    Raises TradingViewRateLimitError when the handshake is refused with HTTP 429, TradingViewConnectionError
    when the feed itself is unreachable (connect timeout, socket or websocket failure, critical_error),
    TradingViewSymbolError for everything specific to this symbol (unknown ticker, empty series, the series
    timing out after a successful connect). Concurrency and connect spacing are gated process-wide."""
    resolution = tv_resolution(interval)
    bars = max(1, min(int(bars), limit))
    phase = "connect"
    async with _session_slot():
        try:
            async with asyncio.timeout(timeout):
                ws = await _connect(url)
                try:
                    phase = "collect"
                    result = await _collect(ws, tv_symbol, resolution, bars)
                finally:
                    await ws.close()
        except TradingViewError:
            raise
        except TimeoutError as exc:
            if phase == "connect":
                raise TradingViewConnectionError(f"{tv_symbol}: TradingView connect timed out after {timeout:.0f}s") from exc
            raise TradingViewSymbolError(f"{tv_symbol}: TradingView timed out after {timeout:.0f}s") from exc
        except Exception as exc:  # websockets.ConnectionClosed / InvalidStatus / OSError ...
            if phase == "connect" and _handshake_status(exc) == 429:
                raise TradingViewRateLimitError(f"{tv_symbol}: TradingView rate limited the connection (HTTP 429)") from exc
            raise TradingViewConnectionError(f"{tv_symbol}: TradingView websocket failed: {exc!r}") from exc
    if not result:
        raise TradingViewSymbolError(f"{tv_symbol}: TradingView returned no bars")
    return result
