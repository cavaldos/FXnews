"""TradingView websocket provider: framing, parsing, fetch flow (fake websocket) and candles.py integration."""

import asyncio
import json
import re
import time

import httpx
import pytest
import respx
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response as HttpResponse

from app.config import get_settings
from app.market import candles as market
from app.market import tradingview as tv
from app.market.symbols import all_symbols, get_symbol

YAHOO_URL = re.compile(r"^https://query2\.finance\.yahoo\.com/v8/finance/chart/.+")
H = 3600


def frame(obj) -> str:
    return tv.encode_frame(obj)


def timescale(rows, sid="sds_1"):
    return frame(
        {
            "m": "timescale_update",
            "p": ["cs_x", {sid: {"s": [{"i": i, "v": v} for i, v in enumerate(rows)], "ns": {"d": "", "indexes": []}, "t": "s1"}}],
        }
    )


XAU_ROWS = [
    [1795136400.0, 3596.89, 3609.74, 3557.03, 3559.98, 122209.0],
    [1795140000.0, 3559.89, 3561.41, 3525.995, 3547.465, 107842.0],
    [1795143600.0, 3547.485, 3556.54, 3517.39, 3554.075, 78108.0],
]
HANDSHAKE = frame('{"session_id":"<0.1_2>","timestamp":1,"protocol":"json"}')
COMPLETED = frame({"m": "series_completed", "p": ["cs_x", "sds_1", "s1", "streaming"]})


class FakeWS:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if not self.incoming:
            raise tv.TradingViewError("no more data")  # would hang otherwise
        return self.incoming.pop(0)

    async def close(self):
        self.closed = True


def patch_ws(monkeypatch, ws):
    async def fake_connect(url):
        return ws

    monkeypatch.setattr(tv, "_connect", fake_connect)


# ── framing / parsing ────────────────────────────────────────────────


def test_encode_and_parse_frames_roundtrip():
    raw = frame({"m": "x", "p": [1, "a"]}) + "~m~4~m~~h~7"
    assert tv.parse_frames(raw) == ['{"m":"x","p":[1,"a"]}', "~h~7"]
    assert tv.parse_frames(HANDSHAKE) == ['{"session_id":"<0.1_2>","timestamp":1,"protocol":"json"}']
    assert tv.parse_frames("garbage") == []
    assert tv.parse_frames("~m~99~m~{}") == ["{}"]  # truncated frame: keep what is there


def test_parse_series_bars_captured_style_frame():
    raw = timescale(XAU_ROWS) + COMPLETED
    first = tv.parse_frames(raw)[0]
    msg = json.loads(first)
    bars = tv.parse_series_bars(msg["p"])
    assert [b.time for b in bars] == [1795136400, 1795140000, 1795143600]
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close, bars[0].volume) == (3596.89, 3609.74, 3557.03, 3559.98, 122209.0)


def test_parse_series_bars_skips_bad_rows_and_missing_volume():
    p = ["cs", {"sds_1": {"s": [{"i": 0, "v": [1, 2, 3, 1, 2]}, {"i": 1, "v": [2, None, 3, 1, 2, 5]}, {"i": 2, "v": [3, 1]}, "x"]}}]
    bars = tv.parse_series_bars(p)
    assert len(bars) == 1 and bars[0].volume is None
    assert tv.parse_series_bars(["cs"]) == []
    assert tv.parse_series_bars(["cs", {"other": {}}]) == []


def test_interval_mapping_covers_every_interval():
    assert tv.TV_INTERVALS == {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "1D"}
    assert set(tv.TV_INTERVALS) == set(market.SUPPORTED_INTERVALS)
    with pytest.raises(ValueError):
        tv.tv_resolution("2h")


# ── fetch flow ───────────────────────────────────────────────────────


async def test_fetch_bars_handshake_heartbeat_and_merge(monkeypatch):
    forming = timescale([[1795143600.0, 3547.485, 3560.0, 3517.39, 3558.0, 80000.0]])  # same bar, updated
    ws = FakeWS([HANDSHAKE, "~m~4~m~~h~1", timescale(XAU_ROWS) + forming + COMPLETED])
    patch_ws(monkeypatch, ws)
    bars = await tv.fetch_bars("OANDA:XAUUSD", "1h", 3)

    assert [b.time for b in bars] == [1795136400, 1795140000, 1795143600]
    assert bars[-1].close == 3558.0  # later update of the same bar wins
    assert ws.closed
    assert "~m~4~m~~h~1" in ws.sent  # heartbeat echoed

    msgs = [json.loads(tv.parse_frames(m)[0]) for m in ws.sent if not m.startswith("~m~4~m~~h~")]
    assert [m["m"] for m in msgs] == [
        "set_auth_token",
        "chart_create_session",
        "switch_timezone",
        "resolve_symbol",
        "create_series",
    ]
    assert msgs[0]["p"] == ["unauthorized_user_token"]
    assert msgs[2]["p"][1] == "Etc/UTC"
    assert json.loads(msgs[3]["p"][2][1:])["symbol"] == "OANDA:XAUUSD"
    cs = msgs[1]["p"][0]
    assert msgs[4]["p"] == [cs, "sds_1", "s1", "sds_sym_1", "60", 3, ""]


@pytest.mark.parametrize("method", ["symbol_error", "series_error"])
async def test_fetch_bars_symbol_error(monkeypatch, method):
    ws = FakeWS([HANDSHAKE, frame({"m": method, "p": ["cs_x", "sds_sym_1", "invalid symbol"]})])
    patch_ws(monkeypatch, ws)
    with pytest.raises(tv.TradingViewSymbolError):
        await tv.fetch_bars("FX:NOPE", "1h", 10)
    assert ws.closed


async def test_fetch_bars_critical_error_is_connection_level(monkeypatch):
    patch_ws(monkeypatch, FakeWS([frame({"m": "critical_error", "p": ["cs_x", "boom", "details"]})]))
    with pytest.raises(tv.TradingViewConnectionError):
        await tv.fetch_bars("FX:EURUSD", "1h", 10)


async def test_fetch_bars_socket_failure_is_connection_level(monkeypatch):
    async def boom(url):
        raise OSError("network down")

    monkeypatch.setattr(tv, "_connect", boom)
    with pytest.raises(tv.TradingViewConnectionError, match="websocket failed"):
        await tv.fetch_bars("FX:EURUSD", "1h", 10)


async def test_fetch_bars_connect_timeout_is_connection_level(monkeypatch):
    async def hang(url):
        await asyncio.sleep(10)

    monkeypatch.setattr(tv, "_connect", hang)
    with pytest.raises(tv.TradingViewConnectionError, match="connect timed out"):
        await tv.fetch_bars("FX:EURUSD", "1h", 10, timeout=0.05)


async def test_fetch_bars_series_timeout_is_symbol_level(monkeypatch):
    class Hang(FakeWS):
        async def recv(self):
            await asyncio.sleep(10)

    patch_ws(monkeypatch, Hang([HANDSHAKE]))
    with pytest.raises(tv.TradingViewSymbolError, match="timed out") as ei:
        await tv.fetch_bars("FX:EURUSD", "1h", 10, timeout=0.05)
    assert not isinstance(ei.value, tv.TradingViewConnectionError)


async def test_fetch_bars_empty_is_symbol_level(monkeypatch):
    patch_ws(monkeypatch, FakeWS([HANDSHAKE, COMPLETED]))
    with pytest.raises(tv.TradingViewSymbolError, match="no bars"):
        await tv.fetch_bars("FX:EURUSD", "1h", 10)


# ── symbol catalog ───────────────────────────────────────────────────


def test_symbol_tv_tickers_match_frontend_mapping():
    expected = {
        "XAUUSD": "OANDA:XAUUSD",
        "XAGUSD": "OANDA:XAGUSD",
        "USOIL": "TVC:USOIL",
        "US500": "OANDA:SPX500USD",
        "NAS100": "OANDA:NAS100USD",
        "US30": "OANDA:US30USD",
        "DE40": "OANDA:DE30EUR",
        "BTCUSD": "BITSTAMP:BTCUSD",
        "ETHUSD": "BITSTAMP:ETHUSD",
    }
    for s in all_symbols():
        assert s.tv == expected.get(s.symbol, f"FX:{s.base}{s.quote}"), s.symbol
    assert get_symbol("EURUSD").tv == "FX:EURUSD"


# ── candles.py integration ───────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _state(monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(market, "_BACKOFF_BASE_S", 0.0)
    market.clear_cache()
    yield
    await market.aclose()
    market.clear_cache()
    monkeypatch.undo()
    get_settings.cache_clear()


def fake_fetch(monkeypatch, result):
    calls = []

    async def fetch(tv_symbol, interval, bars, **kw):
        calls.append((tv_symbol, interval, bars))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tv, "fetch_bars", fetch)
    return calls


def tv_bars(n=3, start=1_700_000_000, step=H):
    from app.schemas import Candle

    return [Candle(time=start + i * step, open=1.0 + i, high=2.0 + i, low=0.5 + i, close=1.5 + i, volume=10.0) for i in range(n)]


async def test_candles_prefers_tradingview_and_caches(monkeypatch):
    calls = fake_fetch(monkeypatch, tv_bars())
    with respx.mock(assert_all_called=False) as router:
        yahoo = router.get(YAHOO_URL).mock(return_value=httpx.Response(500))
        out = await market.get_candles("XAUUSD", "1h")
        again = await market.get_candles("XAUUSD", "1h")
    assert out.source == "tradingview" and len(out.candles) == 3
    assert again is out and len(calls) == 1 and not yahoo.called
    assert calls[0][0] == "OANDA:XAUUSD" and calls[0][1] == "1h" and 30 <= calls[0][2] <= tv.MAX_BARS


async def test_candles_range_max_asks_tradingview_for_its_full_budget(monkeypatch):
    seen = []

    async def fetch(tv_symbol, interval, bars, **kw):
        seen.append((interval, bars, kw))
        return tv_bars()

    monkeypatch.setattr(tv, "fetch_bars", fetch)
    deep = await market.get_candles("EURUSD", "15m", "max")  # Yahoo would clamp 15m to 60d (~4500 bars)
    normal = await market.get_candles("EURUSD", "15m", "60d")
    assert seen[0] == ("15m", tv.DEEP_BARS, {"limit": tv.DEEP_BARS})
    assert seen[1][1] < tv.DEEP_BARS and seen[1][2] == {}
    assert deep is not normal  # separate cache slots


YAHOO_OK = {
    "chart": {"error": None, "result": [{"meta": {}, "timestamp": [1, 2], "indicators": {"quote": [
        {"open": [1, 1], "high": [2, 2], "low": [1, 1], "close": [2, 2], "volume": [0, 0]}]}}]}
}


def scripted_fetch(monkeypatch, outcomes):
    """Each TradingView call consumes the next outcome: bars to return, or an exception to raise."""
    calls, queue = [], list(outcomes)

    async def fetch(tv_symbol, interval, bars, **kw):
        calls.append(tv_symbol)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(tv, "fetch_bars", fetch)
    return calls


async def test_candles_falls_back_to_yahoo_on_tradingview_failure_with_cooldown(monkeypatch):
    calls = fake_fetch(monkeypatch, tv.TradingViewConnectionError("down"))
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=YAHOO_OK))
        out = await market.get_candles("EURUSD", "15m")
        assert out.source == "yahoo"
        out2 = await market.get_candles("EURUSD", "5m")  # second failure in a row -> cooldown starts
        assert out2.source == "yahoo"
        out3 = await market.get_candles("EURUSD", "1h")  # different key -> cache miss, TradingView is paused
        assert out3.source == "yahoo"
    assert len(calls) == 2  # third request skipped TradingView because of the cooldown


async def test_single_connection_failure_does_not_pause_tradingview(monkeypatch):
    calls = scripted_fetch(monkeypatch, [tv.TradingViewConnectionError("blip"), tv_bars()])
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=YAHOO_OK))
        first = await market.get_candles("EURUSD", "15m")
        second = await market.get_candles("GBPUSD", "15m")
    assert first.source == "yahoo"
    assert second.source == "tradingview"
    assert len(calls) == 2


async def test_connection_failures_must_be_consecutive(monkeypatch):
    # fail, per-symbol error, fail, success: never two connection failures in a row -> never paused
    calls = scripted_fetch(monkeypatch, [
        tv.TradingViewConnectionError("blip"),
        tv.TradingViewSymbolError("timed out"),
        tv.TradingViewConnectionError("blip"),
        tv_bars(),
    ])
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=YAHOO_OK))
        sources = [(await market.get_candles("EURUSD", iv)).source for iv in ("15m", "5m", "1h", "30m")]
    assert sources == ["yahoo", "yahoo", "yahoo", "tradingview"]
    assert len(calls) == 4


async def test_unknown_ticker_does_not_pause_tradingview(monkeypatch):
    calls = fake_fetch(monkeypatch, tv.TradingViewSymbolError("nope"))
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(500))
        for sym in ("EURUSD", "GBPUSD"):
            with pytest.raises(market.MarketDataError):
                await market.get_candles(sym, "15m")
    assert len(calls) == 2


async def test_stale_tradingview_data_served_when_everything_fails(monkeypatch):
    fake_fetch(monkeypatch, tv_bars())
    out = await market.get_candles("EURUSD", "15m")
    assert out.source == "tradingview"
    market._cache[("candles", "EURUSD", "15m", "5d")].fresh_until = 0  # expire TTL, keep last-good
    fake_fetch(monkeypatch, tv.TradingViewConnectionError("down"))
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(404, json={}))
        stale = await market.get_candles("EURUSD", "15m")
    assert stale.source == "tradingview-stale"


async def test_disabled_flag_skips_tradingview(monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_ENABLED", "false")
    get_settings.cache_clear()
    calls = fake_fetch(monkeypatch, tv_bars())
    payload = {"chart": {"error": None, "result": [{"meta": {}, "timestamp": [1], "indicators": {"quote": [
        {"open": [1], "high": [2], "low": [1], "close": [2], "volume": [0]}]}}]}}
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=payload))
        out = await market.get_candles("EURUSD", "1h")
    assert out.source == "yahoo" and not calls


def fake_fetch_by_interval(monkeypatch, series):
    """TradingView stub: ``series`` maps our interval -> closes (ascending); records (ticker, interval)."""
    calls = []

    async def fetch(tv_symbol, interval, bars, **kw):
        calls.append((tv_symbol, interval))
        return [tv_closes_bar(i, c) for i, c in enumerate(series[interval])]

    monkeypatch.setattr(tv, "fetch_bars", fetch)
    return calls


def tv_closes_bar(i, close):
    from app.schemas import Candle

    return Candle(time=1_700_000_000 + i * H, open=close, high=close, low=close, close=close, volume=None)


async def test_quote_price_is_latest_intraday_close_of_the_chart_ticker(monkeypatch):
    # Regression (US500 quote 7139.8 vs chart ~7329): the quote used to take the forming DAILY bar's close.
    # Here the daily bar says 110 while the chart's latest 5m close is 111.5: the quote must follow the chart.
    calls = fake_fetch_by_interval(monkeypatch, {
        "5m": [111.0, 111.5],
        "1d": [100.0, 110.0],  # previous completed day = 100, forming day = 110
    })
    (quote,) = await market.get_quotes(["US500"])
    candles = await market.get_candles("US500", "5m")
    assert quote.price == candles.candles[-1].close == 111.5
    assert quote.change == 11.5 and quote.change_pct == 11.5  # vs previous completed daily close (100)
    assert {ticker for ticker, _ in calls} == {"OANDA:SPX500USD"}  # same ticker as the chart, never ^GSPC
    assert ("OANDA:SPX500USD", "5m") in calls and ("OANDA:SPX500USD", "1d") in calls


async def test_quote_previous_daily_close_is_cached_across_quote_refreshes(monkeypatch):
    calls = fake_fetch_by_interval(monkeypatch, {"5m": [110.0, 112.0], "1d": [100.0, 110.0]})
    (first,) = await market.get_quotes(["XAUUSD"])
    market._cache[("quote", "XAUUSD")].fresh_until = 0  # quote TTL expires, previous close still cached
    (second,) = await market.get_quotes(["XAUUSD"])
    assert first.price == second.price == 112.0
    assert first.change == 12.0 and first.change_pct == 12.0
    assert [interval for _, interval in calls] == ["5m", "1d", "5m"]  # one daily fetch in total


async def test_quote_from_tradingview_daily_bars_is_only_the_previous_close(monkeypatch):
    # The forming daily close is not used as the price, only as the source of the previous close.
    calls = fake_fetch_by_interval(monkeypatch, {"5m": [111.5], "1d": [100.0, 110.0]})
    quotes = await market.get_quotes(["XAUUSD"])
    assert ("OANDA:XAUUSD", "5m") in calls and ("OANDA:XAUUSD", "1d") in calls
    assert quotes[0].price == 111.5 and quotes[0].change == 11.5 and quotes[0].change_pct == 11.5


@pytest.mark.network
async def test_live_tradingview_smoke():
    bars = await tv.fetch_bars("OANDA:XAUUSD", "1h", 10)
    assert len(bars) >= 5 and all(b.high >= b.low for b in bars)


# ── session gate: concurrency cap and connect spacing ────────────────


@pytest.fixture
def tv_settings(monkeypatch):
    def set_(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    yield set_
    monkeypatch.undo()
    get_settings.cache_clear()


class CountingWS(FakeWS):
    def __init__(self, incoming, counter):
        super().__init__(incoming)
        self.counter = counter

    async def close(self):
        await super().close()
        self.counter["active"] -= 1


async def test_session_gate_caps_concurrent_sessions(monkeypatch, tv_settings):
    tv_settings(TRADINGVIEW_MAX_SESSIONS=2, TRADINGVIEW_CONNECT_SPACING_MS=0)
    counter = {"active": 0, "peak": 0, "connects": 0}

    async def connect(url):
        counter["active"] += 1
        counter["connects"] += 1
        counter["peak"] = max(counter["peak"], counter["active"])
        await asyncio.sleep(0.02)  # keep sessions overlapping
        return CountingWS([HANDSHAKE, timescale(XAU_ROWS) + COMPLETED], counter)

    monkeypatch.setattr(tv, "_connect", connect)
    results = await asyncio.gather(*(tv.fetch_bars("FX:EURUSD", "1h", 3) for _ in range(6)))
    assert all(len(r) == 3 for r in results)
    assert counter["connects"] == 6 and counter["peak"] == 2
    assert counter["active"] == 0


async def test_connect_spacing_separates_new_connections(monkeypatch, tv_settings):
    tv_settings(TRADINGVIEW_MAX_SESSIONS=10, TRADINGVIEW_CONNECT_SPACING_MS=100)
    starts = []

    async def connect(url):
        starts.append(time.monotonic())
        return FakeWS([HANDSHAKE, timescale(XAU_ROWS) + COMPLETED])

    monkeypatch.setattr(tv, "_connect", connect)
    await asyncio.gather(*(tv.fetch_bars("FX:EURUSD", "1h", 3) for _ in range(3)))
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(gaps) == 2 and all(g >= 0.09 for g in gaps)


async def test_queue_wait_for_a_session_slot_is_not_a_connect_timeout(monkeypatch, tv_settings):
    tv_settings(TRADINGVIEW_MAX_SESSIONS=1, TRADINGVIEW_CONNECT_SPACING_MS=0)

    async def connect(url):
        return FakeWS([HANDSHAKE, timescale(XAU_ROWS) + COMPLETED])

    async def slow_session(*args, **kwargs):
        await asyncio.sleep(0.2)  # each session takes 0.2s, under the 0.3s timeout
        return await real_collect(*args, **kwargs)

    real_collect = tv._collect
    monkeypatch.setattr(tv, "_connect", connect)
    monkeypatch.setattr(tv, "_collect", slow_session)
    # The third caller queues ~0.4s for the single slot. Counted against its timeout it would fail.
    results = await asyncio.gather(
        *(tv.fetch_bars("FX:EURUSD", "1h", 3, timeout=0.3) for _ in range(3)), return_exceptions=True
    )
    assert all(isinstance(r, list) and len(r) == 3 for r in results), results


# ── handshake HTTP 429 ───────────────────────────────────────────────


def handshake_refused(status: int) -> InvalidStatus:
    return InvalidStatus(HttpResponse(status, "refused", Headers(), b""))


async def test_handshake_429_is_a_rate_limit_error_not_a_connection_error(monkeypatch):
    async def refuse(url):
        raise handshake_refused(429)

    monkeypatch.setattr(tv, "_connect", refuse)
    with pytest.raises(tv.TradingViewRateLimitError, match="429") as ei:
        await tv.fetch_bars("FX:EURUSD", "1h", 10)
    assert not isinstance(ei.value, tv.TradingViewConnectionError)


async def test_other_handshake_refusals_stay_connection_errors(monkeypatch):
    async def refuse(url):
        raise handshake_refused(503)

    monkeypatch.setattr(tv, "_connect", refuse)
    with pytest.raises(tv.TradingViewConnectionError, match="websocket failed") as ei:
        await tv.fetch_bars("FX:EURUSD", "1h", 10)
    assert not isinstance(ei.value, tv.TradingViewRateLimitError)


@pytest.fixture
def fake_clock(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(market, "_now", lambda: clock["t"])
    return clock


SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "EURGBP", "EURJPY"]
RATE_LIMITED = tv.TradingViewRateLimitError("OANDA:EURUSD: TradingView rate limited the connection (HTTP 429)")


async def test_429_backoff_doubles_from_30s_to_5min(monkeypatch, fake_clock):
    calls = scripted_fetch(monkeypatch, [RATE_LIMITED] * 6)
    waits = []
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=YAHOO_OK))
        for sym in SYMBOLS[:6]:
            out = await market.get_candles(sym, "15m")
            assert out.source == "yahoo"
            until, reason = market._cooldowns["tradingview-429"]
            waits.append(until - fake_clock["t"])
            assert "HTTP 429" in reason
            fake_clock["t"] = until + 1  # backoff over: the next request tries TradingView again
    assert waits == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    assert len(calls) == 6
    assert market._tv_connection_failures == 0 and "tradingview" not in market._cooldowns


async def test_429_backoff_skips_tradingview_entirely(monkeypatch, fake_clock):
    calls = scripted_fetch(monkeypatch, [RATE_LIMITED])
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=YAHOO_OK))
        first = await market.get_candles("EURUSD", "15m")
        fake_clock["t"] += 29  # still inside the 30s backoff
        second = await market.get_candles("GBPUSD", "15m")
    assert first.source == second.source == "yahoo"
    assert len(calls) == 1


async def test_429_backoff_serves_stale_cache_and_labels_it(monkeypatch, fake_clock):
    calls = scripted_fetch(monkeypatch, [tv_bars(), RATE_LIMITED, RATE_LIMITED])
    before = int(time.time())
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(404, json={}))
        fresh = await market.get_candles("EURUSD", "15m")
        assert fresh.source == "tradingview" and fresh.stale is False and fresh.as_of is None
        fake_clock["t"] += 61  # past the 60s TTL, inside the stale window
        stale = await market.get_candles("EURUSD", "15m")  # TradingView 429s, Yahoo 404s -> last-good copy
        fake_clock["t"] += 5  # still backing off: TradingView is not called again
        stale_again = await market.get_candles("EURUSD", "15m")
    after = int(time.time())
    assert stale.source == stale_again.source == "tradingview-stale"
    assert stale.stale is True and stale.candles == fresh.candles
    assert before <= stale.as_of <= after
    assert len(calls) == 2


async def test_429_streak_resets_after_a_successful_session(monkeypatch, fake_clock):
    calls = scripted_fetch(monkeypatch, [RATE_LIMITED, RATE_LIMITED, tv_bars(), RATE_LIMITED])
    with respx.mock(assert_all_called=False) as router:
        router.get(YAHOO_URL).mock(return_value=httpx.Response(200, json=YAHOO_OK))
        await market.get_candles("EURUSD", "15m")  # 30s
        fake_clock["t"] += 31
        await market.get_candles("GBPUSD", "15m")  # 60s
        fake_clock["t"] += 61
        ok = await market.get_candles("USDJPY", "15m")  # TradingView answers: streak reset
        assert ok.source == "tradingview"
        await market.get_candles("AUDUSD", "15m")  # new episode starts again at 30s
    until, _ = market._cooldowns["tradingview-429"]
    assert until - fake_clock["t"] == 30.0
    assert len(calls) == 4
