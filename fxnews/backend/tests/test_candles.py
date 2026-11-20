import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.market import candles as market
from app.routers import market as market_router

YAHOO_URL = re.compile(r"^https://query2\.finance\.yahoo\.com/v8/finance/chart/.+")
TD_URL = re.compile(r"^https://api\.twelvedata\.com/(time_series|quote)")
TD_KEY = "test-key-123"
H = 3600


@pytest.fixture(autouse=True)
async def _clean_state(monkeypatch):
    monkeypatch.setattr(market, "_BACKOFF_BASE_S", 0.0)
    market.clear_cache()
    yield
    await market.aclose()
    market.clear_cache()


def chart_payload(timestamps, o, h, l, c, v=None, **meta):
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {"symbol": "EURUSD=X", **meta},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v or [0] * len(timestamps)}]
                    },
                }
            ],
        }
    }


def simple_payload(n=3, start=1_700_000_000, step=900):
    ts = [start + i * step for i in range(n)]
    px = [1.10 + i * 0.001 for i in range(n)]
    return chart_payload(ts, px, [p + 0.0005 for p in px], [p - 0.0005 for p in px], [p + 0.0002 for p in px])


def quote_payload(price, prev=None, chart_prev=None, t=1_794_388_889):
    meta = {"regularMarketPrice": price, "regularMarketTime": t}
    if prev is not None:
        meta["previousClose"] = prev
    if chart_prev is not None:
        meta["chartPreviousClose"] = chart_prev
    return chart_payload([t], [price], [price], [price], [price], **meta)


# ── candles parsing ──────────────────────────────────────────────────


@respx.mock
async def test_drops_none_sorts_and_dedupes():
    ts = [1000, 400, 700, 700, 1300, 1600]
    o = [1.0, 1.0, 1.0, 9.0, 1.0, 1.0]
    h = [2.0, 2.0, 2.0, 9.5, 2.0, 2.0]
    l = [0.5, 0.5, 0.5, 8.5, 0.5, None]  # last bar has a None low -> dropped
    c = [1.5, 1.5, None, 9.2, 1.5, 1.5]  # first 700 bar has a None close -> dropped; duplicate 700 survives
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=chart_payload(ts, o, h, l, c)))

    out = await market.get_candles("eurusd", "15m")

    assert out.symbol == "EURUSD" and out.interval == "15m" and out.source == "yahoo"
    assert [x.time for x in out.candles] == [400, 700, 1000, 1300]
    assert all(isinstance(x.time, int) for x in out.candles)
    dup = next(x for x in out.candles if x.time == 700)
    assert (dup.open, dup.close) == (9.0, 9.2)  # the valid duplicate wins
    assert out.candles[0].volume == 0.0


@respx.mock
async def test_nan_and_non_numeric_values_are_dropped():
    payload = chart_payload([100, 200, 300], [1, "x", 1], [2, 2, 2], [0.5, 0.5, 0.5], [1.5, 1.5, 1.5], [None, 5, 7])
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=payload))
    out = await market.get_candles("EURUSD", "1d")
    assert [x.time for x in out.candles] == [100, 300]
    assert out.candles[0].volume is None and out.candles[1].volume == 7.0


@respx.mock
async def test_request_params_and_ticker_encoding():
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=simple_payload()))
    await market.get_candles("US500", "15m")  # ^GSPC
    req = route.calls.last.request
    assert str(req.url).startswith("https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC?")
    assert dict(req.url.params) == {"interval": "15m", "range": "5d"}
    assert "mozilla" in req.headers["user-agent"].lower()


@respx.mock
@pytest.mark.parametrize(
    "interval,range_,expected_interval,expected_range",
    [
        ("1m", None, "1m", "1d"),
        ("5m", None, "5m", "5d"),
        ("15m", None, "15m", "5d"),
        ("30m", None, "30m", "1mo"),
        ("1h", None, "60m", "1mo"),  # Yahoo calls 1h "60m"
        ("4h", None, "60m", "3mo"),  # 4h is built from 60m
        ("1d", None, "1d", "1y"),
        ("1m", "1mo", "1m", "7d"),  # clamped to Yahoo limits
        ("1m", "5d", "1m", "5d"),
        ("5m", "1y", "5m", "60d"),
        ("15m", "3mo", "15m", "60d"),
        ("15m", "1mo", "15m", "1mo"),
        ("1h", "5y", "60m", "730d"),
        ("4h", "max", "60m", "730d"),
        ("1d", "5y", "1d", "5y"),
        ("1d", "MAX", "1d", "max"),
        ("1h", "2y", "60m", "2y"),
    ],
)
async def test_interval_mapping_default_and_clamped_range(interval, range_, expected_interval, expected_range):
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=simple_payload()))
    await market.get_candles("EURUSD", interval, range_)
    params = route.calls.last.request.url.params
    assert (params["interval"], params["range"]) == (expected_interval, expected_range)


async def test_invalid_inputs_raise_value_error_without_http():
    with respx.mock(assert_all_called=False) as router:
        route = router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=simple_payload()))
        with pytest.raises(market.UnsupportedSymbolError):
            await market.get_candles("NOPEUSD")
        with pytest.raises(ValueError):  # subclass check: spec says ValueError
            await market.get_candles("")
        with pytest.raises(market.InvalidRequestError):
            await market.get_candles("EURUSD", "2m")
        for bad in ("abc", "0d", "5 days", "-1d"):
            with pytest.raises(market.InvalidRequestError):
                await market.get_candles("EURUSD", "15m", bad)
        assert route.call_count == 0


# ── 4h resample ──────────────────────────────────────────────────────


@respx.mock
async def test_resample_4h_utc_buckets():
    day = 1_699_920_000  # 2023-11-14 00:00:00 UTC
    assert day % (4 * H) == 0
    hours = list(range(1, 10))  # 01:00 .. 09:00 -> buckets 00:00 (01-03), 04:00 (04-07), 08:00 (08-09)
    ts = [day + h * H for h in hours]
    o = [10 + h for h in hours]
    c = [20 + h for h in hours]
    hi = [100 + h for h in hours]
    lo = [50 - h for h in hours]
    vol = [1.0] * len(hours)
    # a bar with a missing value must not poison its bucket
    c[3] = None
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=chart_payload(ts, o, hi, lo, c, vol)))

    out = await market.get_candles("EURUSD", "4h")

    assert out.interval == "4h"
    b0, b1, b2 = out.candles
    assert [b.time for b in out.candles] == [day, day + 4 * H, day + 8 * H]
    # bucket 00:00 holds 01,02,03
    assert (b0.open, b0.close, b0.high, b0.low, b0.volume) == (11, 23, 103, 47, 3.0)
    # bucket 04:00 holds 04(dropped, None close),05,06,07
    assert (b1.open, b1.close, b1.high, b1.low, b1.volume) == (15, 27, 107, 43, 3.0)
    # bucket 08:00 holds 08,09
    assert (b2.open, b2.close, b2.high, b2.low, b2.volume) == (18, 29, 109, 41, 2.0)


@respx.mock
async def test_resample_volume_stays_none_when_provider_has_none():
    day = 1_699_920_000
    ts = [day + h * H for h in range(4)]
    payload = chart_payload(ts, [1] * 4, [2] * 4, [0.5] * 4, [1.5] * 4, [None] * 4)
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=payload))
    out = await market.get_candles("EURUSD", "4h")
    assert len(out.candles) == 1 and out.candles[0].volume is None


# ── cache / single-flight ────────────────────────────────────────────


@respx.mock
async def test_cache_hit_expiry_and_distinct_keys(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(market, "_now", lambda: clock["t"])
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=simple_payload()))

    a = await market.get_candles("EURUSD", "15m")  # ttl 60s
    b = await market.get_candles("EURUSD", "15m")
    assert route.call_count == 1 and b is a

    await market.get_candles("EURUSD", "15m", "1d")  # other range -> other key
    await market.get_candles("GBPUSD", "15m")  # other symbol
    await market.get_candles("EURUSD", "5m")  # other interval
    assert route.call_count == 4

    clock["t"] += 59
    await market.get_candles("EURUSD", "15m")
    assert route.call_count == 4
    clock["t"] += 2  # > 60s
    await market.get_candles("EURUSD", "15m")
    assert route.call_count == 5

    # 1h has a longer TTL (300s)
    await market.get_candles("EURUSD", "1h")
    n = route.call_count
    clock["t"] += 200
    await market.get_candles("EURUSD", "1h")
    assert route.call_count == n
    clock["t"] += 101
    await market.get_candles("EURUSD", "1h")
    assert route.call_count == n + 1


@respx.mock
async def test_concurrent_identical_requests_share_one_upstream_call():
    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=simple_payload())

    route = respx.get(YAHOO_URL).mock(side_effect=slow)
    results = await asyncio.gather(*(market.get_candles("EURUSD", "15m") for _ in range(8)))
    assert route.call_count == 1
    assert all(r is results[0] for r in results)


@respx.mock
async def test_concurrent_requests_share_failure_and_failure_is_not_cached():
    calls = {"n": 0}

    async def flaky(request):
        calls["n"] += 1
        await asyncio.sleep(0.02)
        if calls["n"] == 1:
            return httpx.Response(404, json={"chart": {"result": None, "error": {"description": "No data found"}}})
        return httpx.Response(200, json=simple_payload())

    respx.get(YAHOO_URL).mock(side_effect=flaky)
    results = await asyncio.gather(*(market.get_candles("EURUSD", "15m") for _ in range(3)), return_exceptions=True)
    assert all(isinstance(r, market.MarketDataError) for r in results)
    assert calls["n"] == 1
    out = await market.get_candles("EURUSD", "15m")  # retried fresh, not served from a cached error
    assert len(out.candles) == 3


@respx.mock
async def test_cancelled_caller_does_not_cancel_shared_fetch():
    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=simple_payload())

    route = respx.get(YAHOO_URL).mock(side_effect=slow)
    first = asyncio.create_task(market.get_candles("EURUSD", "15m"))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(market.get_candles("EURUSD", "15m"))
    await asyncio.sleep(0.01)
    first.cancel()
    out = await second
    assert len(out.candles) == 3 and route.call_count == 1


# ── retries / errors ─────────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize("bad_status", [429, 500, 503])
async def test_retries_then_succeeds(bad_status):
    route = respx.get(YAHOO_URL).mock(
        side_effect=[
            httpx.Response(bad_status),
            httpx.Response(bad_status, headers={"retry-after": "0"}),
            httpx.Response(200, json=simple_payload()),
        ]
    )
    out = await market.get_candles("EURUSD", "15m")
    assert route.call_count == 3 and len(out.candles) == 3


@respx.mock
async def test_gives_up_after_three_retries():
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(503, text="busy"))
    with pytest.raises(market.MarketDataError, match="503"):
        await market.get_candles("EURUSD", "15m")
    assert route.call_count == 1 + market.MAX_RETRIES == 4


@respx.mock
async def test_non_retryable_status_fails_immediately_with_yahoo_message():
    route = respx.get(YAHOO_URL).mock(
        return_value=httpx.Response(
            404, json={"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}
        )
    )
    with pytest.raises(market.MarketDataError, match="symbol may be delisted"):
        await market.get_candles("EURUSD", "15m")
    assert route.call_count == 1


@respx.mock
async def test_200_with_error_body_and_garbage_raise_market_data_error():
    respx.get(YAHOO_URL).mock(
        side_effect=[
            httpx.Response(200, json={"chart": {"result": None, "error": {"description": "boom"}}}),
            httpx.Response(200, text="<html>not json</html>"),
            httpx.Response(200, json=["unexpected"]),
        ]
    )
    for _ in range(3):
        with pytest.raises(market.MarketDataError):
            await market.get_candles("EURUSD", "15m")


@respx.mock
async def test_network_errors_become_market_data_error():
    respx.get(YAHOO_URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
    with pytest.raises(market.MarketDataError, match="timed out"):
        await market.get_candles("EURUSD", "15m")
    with pytest.raises(market.MarketDataError, match="failed"):
        await market.get_candles("EURUSD", "15m")


# ── quotes ───────────────────────────────────────────────────────────


@respx.mock
async def test_quotes_change_vs_previous_close():
    def handler(request):
        if "EURUSD" in str(request.url):
            return httpx.Response(200, json=quote_payload(1.1143, prev=1.1167, chart_prev=1.1192))
        return httpx.Response(200, json=quote_payload(4063.3, chart_prev=4030.2, t=1_794_388_319))  # GC=F: no previousClose

    route = respx.get(YAHOO_URL).mock(side_effect=handler)
    quotes = await market.get_quotes(["eurusd", "XAUUSD", "EURUSD", " "])

    assert route.call_count == 2  # de-duplicated
    assert [q.symbol for q in quotes] == ["EURUSD", "XAUUSD"]
    eur, xau = quotes
    assert eur.price == 1.1143
    assert eur.change == pytest.approx(-0.0024, abs=1e-9)  # prefers previousClose over chartPreviousClose
    assert eur.change_pct == pytest.approx(-0.2149, abs=1e-4)
    assert eur.time == 1_794_388_889
    assert xau.change == pytest.approx(33.1, abs=1e-6)  # falls back to chartPreviousClose
    assert xau.change_pct == pytest.approx(0.8213, abs=1e-4)
    assert xau.time == 1_794_388_319
    assert dict(route.calls[0].request.url.params) == {"interval": "1d", "range": "1d"}


@respx.mock
async def test_quotes_skip_failed_and_unknown_symbols(caplog):
    def handler(request):
        if "GBPUSD" in str(request.url):
            return httpx.Response(404, json={"chart": {"result": None, "error": {"description": "gone"}}})
        return httpx.Response(200, json=quote_payload(1.25, prev=1.24))

    respx.get(YAHOO_URL).mock(side_effect=handler)
    with caplog.at_level("WARNING", logger=market.logger.name):
        quotes = await market.get_quotes(["EURUSD", "GBPUSD", "NOPE"])
    assert [q.symbol for q in quotes] == ["EURUSD"]
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "GBPUSD" in warned and "NOPE" in warned


@respx.mock
async def test_quote_without_previous_close_and_cache():
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=quote_payload(1.2)))
    q = (await market.get_quotes(["EURUSD"]))[0]
    assert (q.price, q.change, q.change_pct) == (1.2, 0.0, 0.0)
    await market.get_quotes(["EURUSD"])
    assert route.call_count == 1  # 15s cache


@respx.mock
async def test_quote_price_falls_back_to_last_close():
    payload = chart_payload([100, 200], [1, 1], [2, 2], [0.5, 0.5], [1.5, 1.7], previousClose=1.0)
    respx.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=payload))
    q = (await market.get_quotes(["EURUSD"]))[0]
    assert q.price == 1.7 and q.change == pytest.approx(0.7)


# ── stale cache / Twelve Data fallback ───────────────────────────────


@pytest.fixture
def td_key(monkeypatch):
    """tests/conftest.py blanks TWELVEDATA_API_KEY; enable the fallback provider explicitly."""
    monkeypatch.setattr(get_settings(), "twelvedata_api_key", TD_KEY)


class Clock:
    def __init__(self, monkeypatch):
        self.t = 1000.0
        monkeypatch.setattr(market, "_now", lambda: self.t)

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


YAHOO_429 = httpx.Response(429, text="Too Many Requests")
YAHOO_404 = httpx.Response(
    404, json={"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}
)


def utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def td_row(dt, o="1.10000", h="1.20000", l="1.00000", c="1.15000", v=None):
    row = {"datetime": dt, "open": o, "high": h, "low": l, "close": c}
    if v is not None:
        row["volume"] = v
    return row


def td_series(rows):
    return {"meta": {"symbol": "EUR/USD", "currency_base": "Euro"}, "values": rows, "status": "ok"}


# newest first, like the real API
TD_ROWS_15M = [
    td_row("2026-11-11 08:30:00", c="1.13"),
    td_row("2026-11-11 08:15:00", c="1.12"),
    td_row("2026-11-11 08:00:00", c="1.11"),
]


@respx.mock
async def test_stale_data_served_when_yahoo_fails_after_ttl(clock):
    state = {"up": True}

    def handler(request):
        return httpx.Response(200, json=simple_payload()) if state["up"] else YAHOO_429

    route = respx.get(YAHOO_URL).mock(side_effect=handler)

    fresh = await market.get_candles("EURUSD", "15m")
    assert fresh.source == "yahoo" and route.call_count == 1

    state["up"] = False
    clock.advance(61)  # past the 60s TTL, well inside the stale window
    stale = await market.get_candles("EURUSD", "15m")
    assert stale.source == "yahoo-stale"
    assert stale.candles == fresh.candles and stale.symbol == "EURUSD" and stale.interval == "15m"
    assert fresh.source == "yahoo"  # the cached object itself is not relabelled
    assert route.call_count == 1 + 1 + market.MAX_RETRIES

    # Yahoo is now cooling down: still stale data, zero extra Yahoo traffic
    again = await market.get_candles("EURUSD", "15m")
    assert again.source == "yahoo-stale" and route.call_count == 5

    # cooldown over and Yahoo healthy again -> fresh data replaces the stale answer
    state["up"] = True
    clock.advance(market.YAHOO_COOLDOWN_429_S + 1)
    back = await market.get_candles("EURUSD", "15m")
    assert back.source == "yahoo" and route.call_count == 6


@respx.mock
async def test_stale_window_expires(clock):
    state = {"up": True}
    respx.get(YAHOO_URL).mock(
        side_effect=lambda request: httpx.Response(200, json=simple_payload()) if state["up"] else YAHOO_404
    )
    await market.get_candles("EURUSD", "15m")
    state["up"] = False

    clock.advance(60 + market.STALE_MAX_AGE_S - 1)
    assert (await market.get_candles("EURUSD", "15m")).source == "yahoo-stale"
    clock.advance(2)  # now beyond TTL + STALE_MAX_AGE_S
    with pytest.raises(market.MarketDataError):
        await market.get_candles("EURUSD", "15m")


@respx.mock
@pytest.mark.parametrize("status,cooldown", [(429, market.YAHOO_COOLDOWN_429_S), (503, market.YAHOO_COOLDOWN_5XX_S)])
async def test_yahoo_cooldown_after_exhausted_retries(clock, status, cooldown):
    route = respx.get(YAHOO_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(market.MarketDataError, match=str(status)):
        await market.get_candles("EURUSD", "15m")
    assert route.call_count == 1 + market.MAX_RETRIES

    # any other request (even another symbol / quotes) does not touch Yahoo while it is paused
    with pytest.raises(market.MarketDataError, match="paused"):
        await market.get_candles("GBPUSD", "5m")
    assert await market.get_quotes(["EURUSD", "XAUUSD"]) == []
    assert route.call_count == 1 + market.MAX_RETRIES

    clock.advance(cooldown - 1)
    with pytest.raises(market.MarketDataError, match="paused"):
        await market.get_candles("GBPUSD", "5m")
    assert route.call_count == 1 + market.MAX_RETRIES
    clock.advance(2)
    with pytest.raises(market.MarketDataError, match=str(status)):
        await market.get_candles("GBPUSD", "5m")
    assert route.call_count == 2 * (1 + market.MAX_RETRIES)


@respx.mock
async def test_404_and_garbage_do_not_start_a_cooldown():
    route = respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    for sym in ("EURUSD", "GBPUSD", "USDJPY"):
        with pytest.raises(market.MarketDataError, match="No data found"):
            await market.get_candles(sym, "15m")
    assert route.call_count == 3


@respx.mock
async def test_twelvedata_fallback_for_candles(td_key, clock):
    yahoo = respx.get(YAHOO_URL).mock(return_value=YAHOO_429)
    td = respx.get(TD_URL).mock(return_value=httpx.Response(200, json=td_series(TD_ROWS_15M)))

    out = await market.get_candles("EURUSD", "15m")

    assert out.source == "twelvedata" and out.symbol == "EURUSD" and out.interval == "15m"
    assert [c.time for c in out.candles] == [utc(2026, 11, 11, 8, 0), utc(2026, 11, 11, 8, 15), utc(2026, 11, 11, 8, 30)]
    assert [c.close for c in out.candles] == [1.11, 1.12, 1.13]  # string numbers -> floats, oldest first
    assert (out.candles[0].open, out.candles[0].high, out.candles[0].low) == (1.1, 1.2, 1.0)
    assert out.candles[0].volume is None  # forex rows have no volume
    assert yahoo.call_count == 1 + market.MAX_RETRIES and td.call_count == 1
    req = td.calls.last.request
    assert req.url.path == "/time_series"
    assert dict(req.url.params) == {
        "symbol": "EUR/USD",
        "interval": "15min",
        "outputsize": "343",  # 5d * 96 bars/day * 5/7 (no weekend)
        "timezone": "UTC",
        "apikey": TD_KEY,
    }

    # cached (Twelve Data answers are kept at least 120s to spare its credits) and Yahoo stays paused
    clock.advance(100)
    assert (await market.get_candles("EURUSD", "15m")).source == "twelvedata"
    assert td.call_count == 1
    clock.advance(21)
    assert (await market.get_candles("EURUSD", "15m")).source == "twelvedata"
    assert td.call_count == 2 and yahoo.call_count == 1 + market.MAX_RETRIES


@respx.mock
@pytest.mark.parametrize(
    "symbol,interval,range_,td_symbol,td_interval,outputsize",
    [
        ("EURUSD", "1m", None, "EUR/USD", "1min", "1029"),
        ("GBPJPY", "30m", None, "GBP/JPY", "30min", "1029"),  # 1mo=30d * 48 * 5/7
        ("XAUUSD", "1h", None, "XAU/USD", "1h", "515"),
        ("XAGUSD", "5m", None, "XAG/USD", "5min", "1029"),  # 5d * 288 * 5/7
        ("BTCUSD", "15m", None, "BTC/USD", "15min", "480"),  # crypto trades 24/7
        ("ETHUSD", "1d", None, "ETH/USD", "1day", "365"),
        ("EURUSD", "1d", "max", "EUR/USD", "1day", "3572"),  # 5000 calendar days * 5/7
        ("EURUSD", "1h", "max", "EUR/USD", "1h", "5000"),  # 730d (Yahoo clamp) * 24 * 5/7 -> capped at 5000
        ("EURUSD", "4h", None, "EUR/USD", "1h", "1543"),  # 4h is resampled from 1h bars
    ],
)
async def test_twelvedata_symbol_interval_and_outputsize_mapping(
    td_key, symbol, interval, range_, td_symbol, td_interval, outputsize
):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    td = respx.get(TD_URL).mock(return_value=httpx.Response(200, json=td_series(TD_ROWS_15M)))
    out = await market.get_candles(symbol, interval, range_)
    assert out.source == "twelvedata"
    params = td.calls.last.request.url.params
    assert (params["symbol"], params["interval"], params["outputsize"]) == (td_symbol, td_interval, outputsize)


@respx.mock
async def test_twelvedata_4h_is_resampled_from_1h_bars(td_key):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    rows = [td_row(f"2026-11-11 {h:02d}:00:00", o=str(h), h=str(100 + h), l=str(50 - h), c=str(20 + h), v="2") for h in range(7, -1, -1)]
    respx.get(TD_URL).mock(return_value=httpx.Response(200, json=td_series(rows)))

    out = await market.get_candles("EURUSD", "4h")

    assert out.interval == "4h" and out.source == "twelvedata"
    first, second = out.candles
    assert (first.time, second.time) == (utc(2026, 11, 11, 0), utc(2026, 11, 11, 4))
    assert (first.open, first.close, first.high, first.low, first.volume) == (0, 23, 103, 47, 8.0)
    assert (second.open, second.close, second.high, second.low, second.volume) == (4, 27, 107, 43, 8.0)


@respx.mock
async def test_twelvedata_daily_bars_with_date_only_datetime(td_key):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    rows = [td_row("2026-11-10"), td_row("2026-11-09")]
    respx.get(TD_URL).mock(return_value=httpx.Response(200, json=td_series(rows)))
    out = await market.get_candles("EURUSD", "1d")
    assert [c.time for c in out.candles] == [utc(2026, 11, 9), utc(2026, 11, 10)]


def test_parse_twelvedata_candles_skips_bad_rows_and_dedupes():
    data = {
        "values": [
            td_row("2026-11-11 08:15:00", c="1.2", v="10"),
            "garbage",
            td_row("2026-11-11 08:00:00", o=None),  # missing value
            td_row("not a date"),
            td_row("2026-11-11 08:00:00", c="1.1"),
            td_row("2026-11-11 08:15:00", c="1.3", v="12"),  # duplicate time: later row wins
            td_row("2026-11-11 08:30:00", c="nan"),
        ]
    }
    candles = market.parse_twelvedata_candles(data)
    assert [(c.time, c.close, c.volume) for c in candles] == [
        (utc(2026, 11, 11, 8, 0), 1.1, None),
        (utc(2026, 11, 11, 8, 15), 1.3, 12.0),
    ]
    assert market.parse_twelvedata_candles({}) == []


@respx.mock
async def test_twelvedata_not_attempted_without_key_or_for_unmapped_symbols(td_key, monkeypatch):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    td = respx.get(TD_URL).mock(return_value=httpx.Response(200, json=td_series(TD_ROWS_15M)))

    for sym in ("US500", "NAS100", "DE40", "USOIL"):  # no Twelve Data mapping
        with pytest.raises(market.MarketDataError):
            await market.get_candles(sym, "15m")
    assert await market.get_quotes(["US500"]) == []
    assert td.call_count == 0

    monkeypatch.setattr(get_settings(), "twelvedata_api_key", "")  # no key configured
    with pytest.raises(market.MarketDataError):
        await market.get_candles("EURUSD", "15m")
    assert td.call_count == 0


@respx.mock
async def test_both_providers_failing_raises_with_both_reasons(td_key):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    respx.get(TD_URL).mock(
        return_value=httpx.Response(200, json={"code": 400, "message": f"symbol not found ({TD_KEY})", "status": "error"})
    )
    with pytest.raises(market.MarketDataError) as ei:
        await market.get_candles("EURUSD", "15m")
    msg = str(ei.value)
    assert "Yahoo chart request failed" in msg and "Twelve Data error 400" in msg
    assert TD_KEY not in msg  # the key must never leak, even if the API echoes it


@respx.mock
async def test_twelvedata_rate_limit_starts_cooldown(td_key, clock):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    td = respx.get(TD_URL).mock(
        return_value=httpx.Response(200, json={"code": 429, "message": "run out of API credits", "status": "error"})
    )
    with pytest.raises(market.MarketDataError, match="Twelve Data error 429"):
        await market.get_candles("EURUSD", "15m")
    with pytest.raises(market.MarketDataError, match="Twelve Data paused"):
        await market.get_candles("GBPUSD", "15m")
    assert td.call_count == 1
    clock.advance(market.TWELVEDATA_COOLDOWN_S + 1)
    with pytest.raises(market.MarketDataError, match="Twelve Data error 429"):
        await market.get_candles("GBPUSD", "15m")
    assert td.call_count == 2


@respx.mock
async def test_twelvedata_http_and_network_errors_are_market_data_errors(td_key):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    respx.get(TD_URL).mock(
        side_effect=[httpx.Response(500, text="oops"), httpx.ConnectTimeout("t"), httpx.Response(200, text="<html>")]
    )
    for pattern in ("Twelve Data error 500", "timed out", "unexpected response"):
        with pytest.raises(market.MarketDataError, match=pattern):
            await market.get_candles("EURUSD", "15m")


@respx.mock
async def test_stale_data_when_both_providers_fail_keeps_origin_in_label(td_key, clock):
    td_state = {"up": True}
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    respx.get(TD_URL).mock(
        side_effect=lambda request: httpx.Response(200, json=td_series(TD_ROWS_15M))
        if td_state["up"]
        else httpx.Response(200, json={"code": 400, "message": "down", "status": "error"})
    )
    fresh = await market.get_candles("EURUSD", "15m")
    assert fresh.source == "twelvedata"

    td_state["up"] = False
    clock.advance(130)  # past the 120s Twelve Data TTL
    stale = await market.get_candles("EURUSD", "15m")
    assert stale.source == "twelvedata-stale" and stale.candles == fresh.candles


@respx.mock
async def test_quotes_fall_back_to_twelvedata(td_key, clock):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    td = respx.get(TD_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "symbol": "EUR/USD",
                "close": "1.11430",
                "previous_close": "1.11670",
                "change": "-0.0024",
                "percent_change": "-0.21",
                "timestamp": 1_794_388_889,
                "is_market_open": True,
            },
        )
    )
    (q,) = await market.get_quotes(["EURUSD"])
    assert q.symbol == "EURUSD" and q.price == 1.1143 and q.time == 1_794_388_889
    assert q.change == pytest.approx(-0.0024, abs=1e-9) and q.change_pct == pytest.approx(-0.2149, abs=1e-4)
    req = td.calls.last.request
    assert req.url.path == "/quote" and dict(req.url.params) == {"symbol": "EUR/USD", "apikey": TD_KEY}

    clock.advance(45)  # Twelve Data quotes are cached for 60s (Yahoo ones for 15s)
    await market.get_quotes(["EURUSD"])
    assert td.call_count == 1
    clock.advance(16)
    await market.get_quotes(["EURUSD"])
    assert td.call_count == 2


@respx.mock
async def test_twelvedata_quote_without_previous_close_uses_reported_change(td_key):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_404)
    respx.get(TD_URL).mock(
        return_value=httpx.Response(200, json={"close": "2000.5", "change": "10.5", "percent_change": "0.53", "timestamp": "1794388889"})
    )
    (q,) = await market.get_quotes(["XAUUSD"])
    assert (q.price, q.change, q.change_pct, q.time) == (2000.5, 10.5, 0.53, 1_794_388_889)


@respx.mock
async def test_stale_quote_served_when_all_providers_fail(clock):
    state = {"up": True}
    respx.get(YAHOO_URL).mock(
        side_effect=lambda request: httpx.Response(200, json=quote_payload(1.2, prev=1.0)) if state["up"] else YAHOO_404
    )
    (fresh,) = await market.get_quotes(["EURUSD"])
    state["up"] = False
    clock.advance(60)
    (stale,) = await market.get_quotes(["EURUSD"])
    assert stale == fresh  # same price and `time`, so the client can see how old it is
    clock.advance(market.STALE_MAX_AGE_S)
    assert await market.get_quotes(["EURUSD"]) == []


@respx.mock
async def test_api_key_is_never_logged(td_key, caplog):
    respx.get(YAHOO_URL).mock(return_value=YAHOO_429)
    respx.get(TD_URL).mock(side_effect=[
        httpx.Response(200, json=td_series(TD_ROWS_15M)),
        httpx.Response(200, json={"code": 429, "message": f"key {TD_KEY} is out of credits", "status": "error"}),
    ])
    # app.main (imported by other test modules) mutes the httpx logger; force it back to INFO so httpx emits
    # its "HTTP Request: GET <url with ?apikey=...>" lines and the redaction filter is genuinely exercised.
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.INFO, logger="httpx"):
        await market.get_candles("EURUSD", "15m")
        with pytest.raises(market.MarketDataError):
            await market.get_candles("GBPUSD", "15m")
        await market.get_quotes(["XAUUSD"])
    assert any(r.name == "httpx" and "api.twelvedata.com" in r.getMessage() for r in caplog.records)  # lines exist...
    assert TD_KEY not in caplog.text  # ...and the key is scrubbed from them
    assert any("Twelve Data" in r.getMessage() for r in caplog.records)  # we did log the fallback


# ── event loop handling ──────────────────────────────────────────────


def test_http_client_and_inflight_state_are_rebuilt_when_the_event_loop_changes():
    async def grab():
        st = market._get_state()
        return st, market._get_client()

    st1, c1 = asyncio.run(grab())
    st2, c2 = asyncio.run(grab())  # a new loop (e.g. TestClient without `with`, uvicorn --reload)
    assert st2 is not st1 and c2 is not c1


async def test_client_is_reused_within_one_loop_and_recreated_after_aclose():
    c1 = market._get_client()
    assert market._get_client() is c1
    await market.aclose()
    assert c1.is_closed
    assert market._get_client() is not c1


# ── router (a fresh TestClient per request => a new event loop each time) ─────


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(market_router.router)
    return TestClient(app)


def test_router_symbols(client):
    r = client.get("/api/symbols")
    assert r.status_code == 200
    body = r.json()
    eur = next(s for s in body if s["symbol"] == "EURUSD")
    assert eur == {"symbol": "EURUSD", "base": "EUR", "quote": "USD", "kind": "fx", "digits": 5, "yahoo": "EURUSD=X", "tv": "FX:EURUSD"}
    assert any(s["symbol"] == "XAUUSD" and s["yahoo"] == "GC=F" and s["tv"] == "OANDA:XAUUSD" for s in body)


def test_router_candles_ok_across_event_loops(client):
    with respx.mock(assert_all_called=False) as router:
        route = router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=simple_payload()))
        r = client.get("/api/candles", params={"symbol": "EURUSD", "interval": "15m", "range": "5d"})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "EURUSD" and body["interval"] == "15m" and body["source"] == "yahoo"
        assert [c["time"] for c in body["candles"]] == [1_700_000_000, 1_700_000_900, 1_700_001_800]
        assert set(body["candles"][0]) == {"time", "open", "high", "low", "close", "volume"}
        # second request runs on a different loop; must reuse the cache and not blow up
        r2 = client.get("/api/candles", params={"symbol": "EURUSD", "interval": "15m", "range": "5d"})
        assert r2.status_code == 200 and route.call_count == 1
        # different key forces a real call on the new loop
        r3 = client.get("/api/candles", params={"symbol": "GBPUSD"})
        assert r3.status_code == 200 and route.call_count == 2


def test_router_candles_errors(client):
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(503, text="down"))
        assert client.get("/api/candles", params={"symbol": "NOPE"}).status_code == 404
        assert client.get("/api/candles", params={"symbol": "EURUSD", "range": "forever"}).status_code == 400
        assert client.get("/api/candles", params={"symbol": "EURUSD", "interval": "2m"}).status_code == 422
        assert client.get("/api/candles").status_code == 422
        r = client.get("/api/candles", params={"symbol": "EURUSD"})
        assert r.status_code == 502
        assert "Market data provider error" in r.json()["detail"] and "503" in r.json()["detail"]


def test_router_quotes(client):
    with respx.mock(assert_all_called=False) as router:
        route = router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=quote_payload(1.2, prev=1.0)))
        r = client.get("/api/quotes", params={"symbols": "eurusd,XAUUSD,NOPE"})
        assert r.status_code == 200
        assert [q["symbol"] for q in r.json()] == ["EURUSD", "XAUUSD"]
        assert set(r.json()[0]) == {"symbol", "price", "change", "change_pct", "time"}
        assert route.call_count == 2

        r = client.get("/api/quotes")  # default list: majors + XAUUSD
        assert r.status_code == 200
        assert [q["symbol"] for q in r.json()] == market_router.DEFAULT_QUOTE_SYMBOLS

        assert client.get("/api/quotes", params={"symbols": "NOPE,ALSO"}).status_code == 404


def test_router_quotes_502_when_provider_down(client):
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(404, json={"chart": {"result": None, "error": {}}}))
        assert client.get("/api/quotes", params={"symbols": "EURUSD"}).status_code == 502


def test_router_reports_the_actual_source(client, td_key):
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=YAHOO_429)
        router.get(TD_URL).mock(return_value=httpx.Response(200, json=td_series(TD_ROWS_15M)))
        r = client.get("/api/candles", params={"symbol": "EURUSD", "interval": "15m"})
        assert r.status_code == 200
        assert r.json()["source"] == "twelvedata" and len(r.json()["candles"]) == 3


def test_router_502_when_every_provider_and_the_cache_fail(client, td_key):
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=YAHOO_429)
        router.get(TD_URL).mock(
            return_value=httpx.Response(200, json={"code": 429, "message": "credits", "status": "error"})
        )
        r = client.get("/api/candles", params={"symbol": "EURUSD"})
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert "Market data provider error" in detail and "429" in detail and "Twelve Data" in detail
        assert TD_KEY not in detail


def test_router_serves_stale_instead_of_502(client, clock):
    state = {"up": True}
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(
            side_effect=lambda request: httpx.Response(200, json=simple_payload()) if state["up"] else YAHOO_429
        )
        assert client.get("/api/candles", params={"symbol": "EURUSD"}).json()["source"] == "yahoo"
        state["up"] = False
        clock.advance(400)
        r = client.get("/api/candles", params={"symbol": "EURUSD"})
        assert r.status_code == 200 and r.json()["source"] == "yahoo-stale" and len(r.json()["candles"]) == 3
