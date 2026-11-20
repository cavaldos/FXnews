"""OHLC candles + quotes: TradingView's chart websocket (primary, so candles match the TradingView chart
exactly), with Yahoo Finance (ported from src/prices/yahooChart.js) and Twelve Data fallbacks and a
stale-cache safety net.

Design notes
- One lazily created module-level ``httpx.AsyncClient``; ``await aclose()`` on shutdown.
- Results are cached in memory (TTL by interval) and concurrent identical requests share a single
  upstream call (single-flight), so many browser tabs polling the same chart cost one upstream hit.
- Provider order on a cache miss: TradingView (``settings.tradingview_enabled``) -> Yahoo -> Twelve Data
  (only if ``settings.twelvedata_api_key``) -> last-good cache entry (kept ``STALE_MAX_AGE_S`` past its TTL,
  returned with ``source="<src>-stale"``) -> ``MarketDataError``. ``CandlesOut.source`` is "tradingview" |
  "yahoo" | "twelvedata", each optionally suffixed "-stale".
- Rate-limit friendly: after Yahoo exhausts its retries on HTTP 429/5xx it is not called again until a
  cooldown passes (a rate-limited Twelve Data and repeated TradingView connection failures likewise), so a
  blocked IP is not hammered. A TradingView handshake refused with HTTP 429 starts its own ``tradingview-429``
  backoff (30s doubling to 5min, reset by the next success). Unknown TradingView tickers, empty series and
  single-series timeouts are per-symbol errors: they start no cooldown.
- The client/in-flight state is bound to the running event loop and transparently rebuilt if the loop
  changes (pytest, Starlette ``TestClient`` without ``with``, uvicorn ``--reload``).
- Errors: ``UnsupportedSymbolError``/``InvalidRequestError`` (both ``ValueError``) for bad input,
  ``MarketDataError`` for anything wrong on the provider side (network, HTTP, bad payload).
- The Twelve Data key travels as a query parameter (their documented form): never log request URLs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar
from urllib.parse import quote as url_quote

import httpx

from app.config import get_settings
from app.market import tradingview
from app.market.symbols import SymbolInfo, get_symbol
from app.schemas import Candle, CandlesOut, QuoteOut

logger = logging.getLogger(__name__)


class _RedactApiKey(logging.Filter):
    """httpx logs every request as 'HTTP Request: GET <full url>' at INFO. Twelve Data wants the key in
    the query string, so scrub ``apikey=...`` from those records before any handler sees them."""

    _RE = re.compile(r"(apikey=)[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # malformed record: not our business
            return True
        if "apikey=" in msg.lower():
            record.msg, record.args = self._RE.sub(r"\1***", msg), ()
        return True


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactApiKey())

YAHOO_CHART_ENDPOINT = "https://query2.finance.yahoo.com/v8/finance/chart"
TWELVEDATA_ENDPOINT = "https://api.twelvedata.com"
SOURCE = "yahoo"
TRADINGVIEW_SOURCE = "tradingview"
TWELVEDATA_SOURCE = "twelvedata"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_S = 20.0
MAX_RETRIES = 3  # retries after the first attempt (429 / 5xx only)
_BACKOFF_BASE_S = 0.8  # delay before retry n = base * 2**n (tests set this to 0)
_BACKOFF_MAX_S = 10.0  # also caps Retry-After so an API request never hangs for long
MAX_CONCURRENT_REQUESTS = 6
_CACHE_MAX_ENTRIES = 256
# The header polls ~8 quotes; each is one Yahoo chart call, so keep this >= 60s to stay under Yahoo's limit.
QUOTE_TTL_S = 60.0
STALE_MAX_AGE_S = 6 * 3600.0  # how long past its TTL a last-good entry may still be served on errors
# After Yahoo exhausted its retries on 429 / 5xx, leave it alone for this long.
YAHOO_COOLDOWN_429_S = 180.0
YAHOO_COOLDOWN_5XX_S = 60.0
TWELVEDATA_COOLDOWN_S = 60.0  # after a rate-limit / quota error from Twelve Data
TRADINGVIEW_COOLDOWN_S = 60.0  # after repeated connection-level failures (not per-symbol errors)
TRADINGVIEW_FAILURES_BEFORE_COOLDOWN = 2  # consecutive connection-level failures before the global pause
_tv_connection_failures = 0  # current streak; any answer from TradingView (even a per-symbol error) resets it
# HTTP 429 on the websocket handshake: its own backoff, doubling 30s -> 5min, reset by the next success.
TV_429_PROVIDER = "tradingview-429"
TV_429_BASE_S = 30.0
TV_429_MAX_S = 300.0
_tv_429_streak = 0  # consecutive 429 handshakes since the last successful TradingView session
# Twelve Data's free plan is ~8 credits/min and 800/day: cache its answers a little longer.
TWELVEDATA_MIN_CANDLE_TTL_S = 120.0
TWELVEDATA_QUOTE_TTL_S = 60.0

# interval -> (yahoo interval, default range, max lookback in days or None, cache TTL seconds)
_INTERVAL_CFG: dict[str, tuple[str, str, int | None, float]] = {
    "1m": ("1m", "1d", 7, 20.0),
    "5m": ("5m", "5d", 60, 20.0),
    "15m": ("15m", "5d", 60, 60.0),
    "30m": ("30m", "1mo", 60, 60.0),
    "1h": ("60m", "1mo", 730, 300.0),
    "4h": ("60m", "3mo", 730, 300.0),  # Yahoo has no 4h: fetch 60m and resample
    "1d": ("1d", "1y", None, 300.0),
}
SUPPORTED_INTERVALS = tuple(_INTERVAL_CFG)
_RESAMPLE_SECONDS = {"4h": 4 * 3600}

_RANGE_RE = re.compile(r"^(\d{1,4})(d|mo|y)$")
_RANGE_UNIT_DAYS = {"d": 1, "mo": 30, "y": 365}
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

T = TypeVar("T")


class UnsupportedSymbolError(ValueError):
    """Symbol is not in the catalog (app/market/symbols.py)."""


class InvalidRequestError(ValueError):
    """Unsupported interval or malformed range."""


class MarketDataError(RuntimeError):
    """The market data provider failed (network error, HTTP error, empty/invalid payload)."""


# ── per-event-loop state ─────────────────────────────────────────────


@dataclass
class _LoopState:
    loop: asyncio.AbstractEventLoop
    client: httpx.AsyncClient | None = None
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_REQUESTS))
    inflight: dict[Any, asyncio.Task] = field(default_factory=dict)


@dataclass
class _Entry:
    value: Any
    fresh_until: float  # served as a normal cache hit until then
    stale_until: float  # last-good: served (labelled stale) when every provider fails, until then
    fetched_at: float = 0.0  # wall clock (unix seconds) when the value was stored, i.e. fetched


_state: _LoopState | None = None
_cache: dict[Any, _Entry] = {}  # loop independent
_cooldowns: dict[str, tuple[float, str]] = {}  # provider -> (blocked_until, reason)


def _now() -> float:
    return time.monotonic()


def _get_state() -> _LoopState:
    global _state
    loop = asyncio.get_running_loop()
    if _state is None or _state.loop is not loop:
        # First use or a different loop: the old client/tasks belong to a loop we cannot use.
        _state = _LoopState(loop=loop)
    return _state


def _get_client() -> httpx.AsyncClient:
    st = _get_state()
    if st.client is None or st.client.is_closed:
        st.client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
    return st.client


async def aclose() -> None:
    """Close the shared HTTP client (call from the FastAPI shutdown/lifespan hook)."""
    global _state
    st, _state = _state, None
    if st is not None and st.client is not None:
        try:
            await st.client.aclose()
        except RuntimeError:  # client belonged to an event loop that is already gone
            pass


def clear_cache() -> None:
    """Drop all cached candles/quotes (incl. stale last-good copies) and provider cooldowns (for tests)."""
    global _tv_connection_failures, _tv_429_streak
    _cache.clear()
    _cooldowns.clear()
    _tv_connection_failures = 0
    _tv_429_streak = 0
    tradingview.reset_session_gate()


def _cache_get(key: Any) -> Any | None:
    """A fresh (within TTL) value, else None."""
    entry = _cache.get(key)
    if entry is None or entry.fresh_until <= _now():
        return None
    return entry.value


def _stale_entry(key: Any) -> _Entry | None:
    """The last-good entry if it is still inside its stale window (even when past its TTL)."""
    entry = _cache.get(key)
    if entry is None:
        return None
    if entry.stale_until <= _now():
        del _cache[key]
        return None
    return entry


def _cache_put(key: Any, value: Any, ttl: float) -> None:
    now = _now()
    if len(_cache) >= _CACHE_MAX_ENTRIES and key not in _cache:
        for k in [k for k, e in _cache.items() if e.stale_until <= now]:
            del _cache[k]
        while len(_cache) >= _CACHE_MAX_ENTRIES:  # still full: evict the entry closest to expiry
            del _cache[min(_cache, key=lambda k: _cache[k].stale_until)]
    _cache[key] = _Entry(
        value, fresh_until=now + ttl, stale_until=now + ttl + STALE_MAX_AGE_S, fetched_at=time.time()
    )


def _cooldown_remaining(provider: str) -> tuple[float, str]:
    until, reason = _cooldowns.get(provider, (0.0, ""))
    return max(0.0, until - _now()), reason


def _start_cooldown(provider: str, seconds: float, reason: str) -> None:
    _cooldowns[provider] = (_now() + seconds, reason)
    logger.warning("%s paused for %.0fs (%s)", provider, seconds, reason)


async def _single_flight(key: Any, factory: Callable[[], Awaitable[T]]) -> T:
    """Run ``factory`` once per ``key`` at a time; concurrent callers await the same task."""
    st = _get_state()
    task = st.inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(factory())
        st.inflight[key] = task

        def _done(t: asyncio.Task, _key: Any = key, _st: _LoopState = st) -> None:
            if _st.inflight.get(_key) is t:
                del _st.inflight[_key]
            if not t.cancelled():
                t.exception()  # mark as retrieved; every awaiting caller still receives it

        task.add_done_callback(_done)
    # shield: one caller disconnecting must not cancel the fetch the others are waiting on
    return await asyncio.shield(task)


# ── Yahoo HTTP ───────────────────────────────────────────────────────


def _yahoo_error_description(resp: httpx.Response) -> str:
    try:
        err = (resp.json().get("chart") or {}).get("error") or {}
        if err.get("description"):
            return str(err["description"])
    except (ValueError, AttributeError):
        pass
    return (resp.text or resp.reason_phrase or "")[:200].strip()


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _BACKOFF_MAX_S)
        except ValueError:
            pass
    return min(_BACKOFF_BASE_S * (2**attempt), _BACKOFF_MAX_S)


async def _fetch_chart_json(ticker: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"{YAHOO_CHART_ENDPOINT}/{url_quote(ticker, safe='')}"
    attempt = 0
    while True:
        # Checked before every attempt: a concurrent request may have just exhausted its retries.
        remaining, why = _cooldown_remaining("yahoo")
        if remaining > 0:
            raise MarketDataError(f"{ticker}: Yahoo requests paused for another {remaining:.0f}s ({why})")
        st = _get_state()
        client = _get_client()
        try:
            async with st.sem:
                resp = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise MarketDataError(f"{ticker}: Yahoo chart request timed out") from exc
        except httpx.HTTPError as exc:
            raise MarketDataError(f"{ticker}: Yahoo chart request failed: {exc!r}") from exc

        if resp.status_code in _RETRYABLE and attempt < MAX_RETRIES:
            attempt += 1
            delay = _retry_delay(resp, attempt)
            logger.warning(
                "yahoo %s: HTTP %s, retry %d/%d in %.1fs", ticker, resp.status_code, attempt, MAX_RETRIES, delay
            )
            await asyncio.sleep(delay)
            continue

        if resp.status_code >= 400:
            if resp.status_code in _RETRYABLE:  # still failing after all retries: stop calling for a while
                seconds = YAHOO_COOLDOWN_429_S if resp.status_code == 429 else YAHOO_COOLDOWN_5XX_S
                _start_cooldown("yahoo", seconds, f"HTTP {resp.status_code}")
            raise MarketDataError(
                f"{ticker}: Yahoo chart request failed (HTTP {resp.status_code}): {_yahoo_error_description(resp)}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise MarketDataError(f"{ticker}: Yahoo returned a non-JSON response") from exc
        if not isinstance(data, dict):
            raise MarketDataError(f"{ticker}: unexpected Yahoo response shape")
        return data


def _chart_result(data: dict[str, Any], ticker: str) -> dict[str, Any]:
    chart = data.get("chart") or {}
    result = chart.get("result")
    if not result:
        desc = (chart.get("error") or {}).get("description") or "missing chart result"
        raise MarketDataError(f"{ticker}: Yahoo chart error: {desc}")
    return result[0]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def parse_candles(result: dict[str, Any]) -> list[Candle]:
    """Yahoo ``chart.result[0]`` -> candles: bars with a missing OHLC value are dropped,
    sorted ascending by time, one bar per ``time`` (the later one wins)."""
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    opens, highs, lows, closes = (quote.get(k) or [] for k in ("open", "high", "low", "close"))
    volumes = quote.get("volume") or []

    def at(arr: list, i: int) -> Any:
        return arr[i] if i < len(arr) else None

    by_time: dict[int, Candle] = {}
    for i, ts in enumerate(timestamps):
        t = _num(ts)
        o, h, l, c = (_num(at(a, i)) for a in (opens, highs, lows, closes))
        if t is None or o is None or h is None or l is None or c is None:
            continue
        by_time[int(t)] = Candle(time=int(t), open=o, high=h, low=l, close=c, volume=_num(at(volumes, i)))
    return [by_time[t] for t in sorted(by_time)]


def resample_candles(candles: list[Candle], seconds: int) -> list[Candle]:
    """Aggregate ascending candles into ``seconds``-wide buckets aligned to the UTC epoch
    (4h -> 00/04/08/12/16/20 UTC). Bucket ``time`` is the bucket start."""
    out: list[Candle] = []
    vol_seen = False
    for c in candles:
        bucket = c.time - c.time % seconds
        if out and out[-1].time == bucket:
            cur = out[-1]
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            if c.volume is not None:
                cur.volume = (cur.volume or 0.0) + c.volume
        else:
            out.append(Candle(time=bucket, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume))
        vol_seen = vol_seen or c.volume is not None
    if not vol_seen:
        for c in out:
            c.volume = None
    return out


# ── candles ──────────────────────────────────────────────────────────


def _range_days(rng: str) -> float:
    if rng == "max":
        return math.inf
    if rng == "ytd":
        return 366.0  # worst case, only used for clamping
    n, unit = _RANGE_RE.match(rng).groups()  # type: ignore[union-attr]
    return int(n) * _RANGE_UNIT_DAYS[unit]


def resolve_range(interval: str, range_: str | None) -> str:
    """Default the range for ``interval`` and clamp it to what Yahoo serves for that interval."""
    if interval not in _INTERVAL_CFG:
        raise InvalidRequestError(f"Unsupported interval {interval!r}; use one of {', '.join(SUPPORTED_INTERVALS)}")
    _, default_range, max_days, _ = _INTERVAL_CFG[interval]
    rng = (range_ or "").strip().lower() or default_range
    m = _RANGE_RE.match(rng)
    if rng not in ("ytd", "max") and (m is None or int(m.group(1)) < 1):
        raise InvalidRequestError(f"Invalid range {range_!r}; use e.g. 1d, 5d, 1mo, 3mo, 1y, ytd, max")
    if max_days is not None and _range_days(rng) > max_days:
        clamped = f"{max_days}d"
        logger.debug("range %s clamped to %s for interval %s", rng, clamped, interval)
        return clamped
    return rng


async def _yahoo_candles(info: SymbolInfo, interval: str, rng: str) -> CandlesOut:
    yahoo_interval = _INTERVAL_CFG[interval][0]
    data = await _fetch_chart_json(info.yahoo, {"interval": yahoo_interval, "range": rng})
    candles = parse_candles(_chart_result(data, info.yahoo))
    if interval in _RESAMPLE_SECONDS:
        candles = resample_candles(candles, _RESAMPLE_SECONDS[interval])
    return CandlesOut(symbol=info.symbol, interval=interval, source=SOURCE, candles=candles)  # type: ignore[arg-type]


# ── TradingView (primary provider) ───────────────────────────────────

_TV_BARS_PER_DAY = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}


def _tradingview_available(info: SymbolInfo) -> bool:
    return bool(get_settings().tradingview_enabled) and bool(info.tv)


def _tv_bar_count(info: SymbolInfo, interval: str, rng: str) -> int:
    """How many bars cover ``rng`` (generous: weekends/sessions only make the real span a bit longer)."""
    days = min(_range_days(rng), 5000.0)
    market_days = 1.0 if info.kind == "crypto" else 5 / 7
    return max(30, min(tradingview.MAX_BARS, math.ceil(days * _TV_BARS_PER_DAY[interval] * market_days * 1.1)))


async def _tradingview_bars(info: SymbolInfo, interval: str, bars: int, *, deep: bool = False) -> list[Candle]:
    global _tv_connection_failures, _tv_429_streak
    remaining, why = _cooldown_remaining(TV_429_PROVIDER)
    if remaining > 0:  # rate limited: no session at all, callers fall through to Yahoo / stale cache
        raise MarketDataError(f"{info.symbol}: TradingView backing off for another {remaining:.0f}s ({why})")
    remaining, why = _cooldown_remaining("tradingview")
    if remaining > 0:
        raise MarketDataError(f"{info.symbol}: TradingView paused for another {remaining:.0f}s ({why})")
    try:
        # Session concurrency and connect spacing are gated inside tradingview.fetch_bars.
        if deep:
            candles = await tradingview.fetch_bars(info.tv, interval, bars, limit=tradingview.DEEP_BARS)
        else:
            candles = await tradingview.fetch_bars(info.tv, interval, bars)
    except tradingview.TradingViewRateLimitError as exc:
        # Not counted as a connection failure: its own exponential backoff (30s, 60s, ... capped at 5min).
        _tv_429_streak += 1
        delay = min(TV_429_BASE_S * 2 ** min(_tv_429_streak - 1, 10), TV_429_MAX_S)
        _start_cooldown(TV_429_PROVIDER, delay, f"HTTP 429, {_tv_429_streak} in a row")
        raise MarketDataError(str(exc)) from exc
    except tradingview.TradingViewConnectionError as exc:
        # The feed itself is failing: pause everyone only after a few failures in a row, so one blip
        # does not push every symbol to Yahoo.
        _tv_connection_failures += 1
        if _tv_connection_failures >= TRADINGVIEW_FAILURES_BEFORE_COOLDOWN:
            _tv_connection_failures = 0
            _start_cooldown("tradingview", TRADINGVIEW_COOLDOWN_S, "connection/protocol error")
        raise MarketDataError(str(exc)) from exc
    except tradingview.TradingViewError as exc:
        # Per-symbol problem (unknown ticker, empty series, one series timing out): the connection worked,
        # so this neither counts toward nor pauses the feed.
        _tv_connection_failures = 0
        raise MarketDataError(str(exc)) from exc
    _tv_connection_failures = 0
    _tv_429_streak = 0  # a successful session ends the 429 backoff schedule
    _cooldowns.pop(TV_429_PROVIDER, None)
    return candles


async def _tradingview_candles(info: SymbolInfo, interval: str, rng: str, *, deep: bool = False) -> CandlesOut:
    if deep:  # ask for everything TradingView will give (its server-side cap decides)
        candles = await _tradingview_bars(info, interval, tradingview.DEEP_BARS, deep=True)
    else:
        candles = await _tradingview_bars(info, interval, _tv_bar_count(info, interval, rng))
    return CandlesOut(symbol=info.symbol, interval=interval, source=TRADINGVIEW_SOURCE, candles=candles)  # type: ignore[arg-type]


_TV_PREV_CLOSE_TTL_S = 300.0  # the previous daily close only changes at the daily rollover


async def _tradingview_prev_daily_close(info: SymbolInfo) -> float | None:
    """Close of the last completed daily bar (the one before the forming bar), cached: it is stable
    between rollovers, and fetching it on every quote would double the TradingView sessions per poll."""
    key = ("tv_prev_close", info.symbol)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    daily = await _tradingview_bars(info, "1d", 3)
    if len(daily) < 2:
        return None
    prev = daily[-2].close
    _cache_put(key, prev, _TV_PREV_CLOSE_TTL_S)
    return prev


async def _tradingview_quote(info: SymbolInfo) -> QuoteOut:
    """Last price = latest 5m close of the same TradingView ticker the candle chart draws (never the daily
    bar, which can lag the intraday series and disagree with the chart); change vs the previous daily close."""
    intraday = await _tradingview_bars(info, "5m", 2)
    price = intraday[-1].close
    prev = await _tradingview_prev_daily_close(info)
    change = round(price - prev, 8) if prev else 0.0
    change_pct = round(change / prev * 100, 4) if prev else 0.0
    return QuoteOut(symbol=info.symbol, price=price, change=change, change_pct=change_pct, time=int(time.time()))


# ── Twelve Data (fallback provider) ──────────────────────────────────

# 4h is built from 1h bars (same as the Yahoo path) so buckets are always aligned to 00/04/08.. UTC.
_TD_INTERVAL = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "1h", "1d": "1day"}
_TD_BARS_PER_DAY = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 24, "1d": 1}
_TD_MAX_OUTPUTSIZE = 5000


def _twelvedata_key() -> str:
    return (get_settings().twelvedata_api_key or "").strip()


def _twelvedata_symbol(info: SymbolInfo) -> str | None:
    """EURUSD -> 'EUR/USD', XAUUSD -> 'XAU/USD', BTCUSD -> 'BTC/USD'. Indices/energy: not offered."""
    return f"{info.base}/{info.quote}" if info.kind in ("fx", "metal", "crypto") else None


def _twelvedata_available(info: SymbolInfo) -> bool:
    return bool(_twelvedata_key()) and _twelvedata_symbol(info) is not None


def _td_float(value: Any) -> float | None:
    """Twelve Data sends numbers as strings."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _td_time(value: Any) -> int | None:
    """'2026-11-11 08:15:00' or '2026-11-11' (requested with timezone=UTC) -> unix seconds."""
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return int((dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp())


def _td_outputsize(info: SymbolInfo, interval: str, rng: str) -> int:
    days = min(_range_days(rng), _TD_MAX_OUTPUTSIZE)
    market_days = 1.0 if info.kind == "crypto" else 5 / 7  # FX/metals do not trade on weekends
    return max(30, min(_TD_MAX_OUTPUTSIZE, math.ceil(days * _TD_BARS_PER_DAY[interval] * market_days)))


async def _twelvedata_get(path: str, params: dict[str, str], label: str) -> dict[str, Any]:
    remaining, why = _cooldown_remaining("twelvedata")
    if remaining > 0:
        raise MarketDataError(f"{label}: Twelve Data paused for another {remaining:.0f}s ({why})")
    key = _twelvedata_key()
    try:
        # NB: the key is in the query string -> never log/raise the request URL.
        resp = await _get_client().get(f"{TWELVEDATA_ENDPOINT}/{path}", params={**params, "apikey": key})
    except httpx.TimeoutException as exc:
        raise MarketDataError(f"{label}: Twelve Data request timed out") from exc
    except httpx.HTTPError as exc:
        raise MarketDataError(f"{label}: Twelve Data request failed: {exc!r}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = None
    body_error = isinstance(data, dict) and data.get("status") == "error"
    if resp.status_code >= 400 or body_error:
        # Twelve Data reports most errors as HTTP 200 + {"code": 429, "message": ..., "status": "error"}
        body = data if isinstance(data, dict) else {}
        code = body.get("code") or resp.status_code
        message = str(body.get("message") or resp.reason_phrase or "")[:200]
        if key:
            message = message.replace(key, "***")
        if str(code) == "429":
            _start_cooldown("twelvedata", TWELVEDATA_COOLDOWN_S, "rate limit / credits exhausted")
        raise MarketDataError(f"{label}: Twelve Data error {code}: {message}")
    if not isinstance(data, dict):
        raise MarketDataError(f"{label}: Twelve Data returned an unexpected response")
    return data


def parse_twelvedata_candles(data: dict[str, Any]) -> list[Candle]:
    """``time_series`` payload (newest first, string numbers) -> ascending, de-duplicated candles."""
    by_time: dict[int, Candle] = {}
    for row in data.get("values") or []:
        if not isinstance(row, dict):
            continue
        t = _td_time(row.get("datetime"))
        o, h, l, c = (_td_float(row.get(k)) for k in ("open", "high", "low", "close"))
        if t is None or o is None or h is None or l is None or c is None:
            continue
        by_time[t] = Candle(time=t, open=o, high=h, low=l, close=c, volume=_td_float(row.get("volume")))
    return [by_time[t] for t in sorted(by_time)]


async def _twelvedata_candles(info: SymbolInfo, interval: str, rng: str) -> CandlesOut:
    params = {
        "symbol": _twelvedata_symbol(info) or "",
        "interval": _TD_INTERVAL[interval],
        "outputsize": str(_td_outputsize(info, interval, rng)),
        "timezone": "UTC",
    }
    data = await _twelvedata_get("time_series", params, info.symbol)
    candles = parse_twelvedata_candles(data)
    if not candles:
        raise MarketDataError(f"{info.symbol}: Twelve Data returned no candles")
    if interval in _RESAMPLE_SECONDS:
        candles = resample_candles(candles, _RESAMPLE_SECONDS[interval])
    return CandlesOut(symbol=info.symbol, interval=interval, source=TWELVEDATA_SOURCE, candles=candles)  # type: ignore[arg-type]


# ── provider chain ───────────────────────────────────────────────────


async def _through_providers(
    *,
    key: Any,
    label: str,
    providers: list[tuple[str, Callable[[], Awaitable[T]] | None, float]],
    as_stale: Callable[[T, float], T],  # (last-good value, unix time it was fetched) -> served value
) -> T:
    """Try ``providers`` in order (``(name, factory, min_ttl)``; a ``None`` factory is skipped) -> stale
    last-good -> MarketDataError. A fresh result is cached for ``min_ttl`` (at least)."""
    errors: list[str] = []
    value: T | None = None
    ttl = 0.0
    for name, factory, min_ttl in providers:
        if factory is None:
            continue
        try:
            value = await factory()
        except MarketDataError as exc:
            errors.append(str(exc))
            continue
        ttl = min_ttl
        if errors:
            logger.warning("%s: %s served after: %s", label, name, "; ".join(errors))
        break
    if value is None:
        stale = _stale_entry(key)
        if stale is not None:
            logger.warning("%s: all providers failed, serving stale data (%s)", label, "; ".join(errors))
            return as_stale(stale.value, stale.fetched_at)
        raise MarketDataError("; ".join(errors) or "no market data provider available")
    _cache_put(key, value, ttl)
    return value


def _stale_candles(out: CandlesOut, fetched_at: float) -> CandlesOut:
    source = out.source if out.source.endswith("-stale") else f"{out.source}-stale"
    return out.model_copy(update={"source": source, "stale": True, "as_of": int(fetched_at)})


async def get_candles(symbol: str, interval: str = "15m", range_: str | None = None) -> CandlesOut:
    """Candles for ``symbol`` (MT5-style name, e.g. EURUSD). Cached; see module docstring for the
    provider order, the ``source`` values and the errors.

    The returned object may be shared with the cache: treat it as read-only."""
    info = get_symbol(symbol)
    if info is None:
        raise UnsupportedSymbolError(f"Unsupported symbol {symbol!r}")
    rng = resolve_range(interval, range_)
    # ``range_="max"`` = as much history as the providers hold. TradingView is asked for its full bar budget
    # (not the 60d clamp Yahoo needs for intraday), so it gets its own cache slot.
    deep = (range_ or "").strip().lower() == "max"
    key = ("candles", info.symbol, interval, "max-deep" if deep else rng)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    ttl = _INTERVAL_CFG[interval][3]
    return await _single_flight(
        key,
        lambda: _through_providers(
            key=key,
            label=f"candles {info.symbol} {interval}",
            providers=[
                (
                    "tradingview",
                    (lambda: _tradingview_candles(info, interval, rng, deep=deep)) if _tradingview_available(info) else None,
                    ttl,
                ),
                ("yahoo", lambda: _yahoo_candles(info, interval, rng), ttl),
                (
                    "twelvedata",
                    (lambda: _twelvedata_candles(info, interval, rng)) if _twelvedata_available(info) else None,
                    max(ttl, TWELVEDATA_MIN_CANDLE_TTL_S),
                ),
            ],
            as_stale=_stale_candles,
        ),
    )


# ── quotes ───────────────────────────────────────────────────────────


def _first_positive(*values: Any) -> float | None:
    for v in values:
        n = _num(v)
        if n:  # not None and not 0
            return n
    return None


async def _yahoo_quote(info: SymbolInfo) -> QuoteOut:
    # range=1d makes chartPreviousClose the previous session's close; 1d interval keeps the payload tiny.
    data = await _fetch_chart_json(info.yahoo, {"interval": "1d", "range": "1d"})
    result = _chart_result(data, info.yahoo)
    meta = result.get("meta") or {}

    price = _num(meta.get("regularMarketPrice"))
    if price is None:
        candles = parse_candles(result)
        price = candles[-1].close if candles else None
    if price is None:
        raise MarketDataError(f"{info.yahoo}: no price in Yahoo response")

    prev = _first_positive(meta.get("previousClose"), meta.get("chartPreviousClose"))
    change = round(price - prev, 8) if prev else 0.0
    change_pct = round(change / prev * 100, 4) if prev else 0.0
    ts = _num(meta.get("regularMarketTime"))
    return QuoteOut(
        symbol=info.symbol,
        price=price,
        change=change,
        change_pct=change_pct,
        time=int(ts) if ts is not None else int(time.time()),
    )


async def _twelvedata_quote(info: SymbolInfo) -> QuoteOut:
    data = await _twelvedata_get("quote", {"symbol": _twelvedata_symbol(info) or ""}, info.symbol)
    price = _td_float(data.get("close"))  # /quote: "close" is the latest price
    if price is None:
        raise MarketDataError(f"{info.symbol}: no price in Twelve Data response")
    prev = _td_float(data.get("previous_close"))
    if prev:
        change = round(price - prev, 8)
        change_pct = round(change / prev * 100, 4)
    else:
        change = _td_float(data.get("change")) or 0.0
        change_pct = _td_float(data.get("percent_change")) or 0.0
    ts = _td_float(data.get("timestamp"))
    return QuoteOut(
        symbol=info.symbol,
        price=price,
        change=change,
        change_pct=change_pct,
        time=int(ts) if ts else int(time.time()),
    )


async def _get_quote(symbol: str) -> QuoteOut:
    info = get_symbol(symbol)
    if info is None:
        raise UnsupportedSymbolError(f"Unsupported symbol {symbol!r}")
    key = ("quote", info.symbol)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    # A stale quote keeps its original `time`, so the client can tell how old it is.
    return await _single_flight(
        key,
        lambda: _through_providers(
            key=key,
            label=f"quote {info.symbol}",
            providers=[
                ("tradingview", (lambda: _tradingview_quote(info)) if _tradingview_available(info) else None, QUOTE_TTL_S),
                ("yahoo", lambda: _yahoo_quote(info), QUOTE_TTL_S),
                (
                    "twelvedata",
                    (lambda: _twelvedata_quote(info)) if _twelvedata_available(info) else None,
                    max(QUOTE_TTL_S, TWELVEDATA_QUOTE_TTL_S),
                ),
            ],
            as_stale=lambda q, _fetched_at: q,
        ),
    )


async def get_quotes(symbols: list[str]) -> list[QuoteOut]:
    """Last price + change vs previous close. Fetched concurrently; a symbol that is unknown or
    fails is skipped (warning logged), so the result may be shorter than ``symbols``."""
    unique = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
    results = await asyncio.gather(*(_get_quote(s) for s in unique), return_exceptions=True)
    quotes: list[QuoteOut] = []
    for sym, res in zip(unique, results):
        if isinstance(res, QuoteOut):
            quotes.append(res)
        elif isinstance(res, Exception):
            logger.warning("quote %s skipped: %s", sym, res)
        else:  # CancelledError etc.
            raise res
    return quotes
