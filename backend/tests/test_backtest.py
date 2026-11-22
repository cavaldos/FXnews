"""Backtest dataset: outcome maths, storage, backfill, prediction runner, API, and the ported LLM layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.backtest import ingest, predict, sources, stats
from app.backtest.models import BtEvent, BtOutcome, BtPrediction
from app.backtest.outcomes import compute_outcome, flat_threshold_pct, label_direction
from app.db import init_db, session_scope
from app.llm import LLMClient, Settings, parse_model_spec
from app.llm.providers.groq import GroqProvider
from app.main import app
from app.schemas import Candle

T0 = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)  # a Friday NFP-style release
TS0 = int(T0.timestamp())
STEP = 300


def bars(start_ts: int, closes: list[float], *, spread: float = 0.0) -> list[Candle]:
    """5m candles: open = previous close, high/low padded by ``spread``."""
    out, prev = [], closes[0]
    for i, close in enumerate(closes):
        out.append(Candle(time=start_ts + i * STEP, open=prev, high=max(prev, close) + spread,
                          low=min(prev, close) - spread, close=close))
        prev = close
    return out


def ramp(start_ts: int, n: int, start: float = 1.1000, step: float = 0.0001) -> list[Candle]:
    return bars(start_ts, [start + i * step for i in range(n)])


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(ingest, "_sleep", no_sleep)
    monkeypatch.setattr(sources, "_sleep", no_sleep)
    init_db()
    with session_scope() as s:
        for table in (BtPrediction, BtOutcome, BtEvent):
            s.execute(delete(table))
    yield


# ── outcome maths ─────────────────────────────────────────────────────────


def test_thresholds_scale_with_horizon_and_kind():
    assert flat_threshold_pct("fx", 60) == pytest.approx(0.04)
    assert flat_threshold_pct("fx", 240) == pytest.approx(0.08)  # sqrt(4) = 2x
    assert flat_threshold_pct("crypto", 60) > flat_threshold_pct("metal", 60) > flat_threshold_pct("fx", 60)
    assert label_direction(0.05, "fx", 60) == "up"
    assert label_direction(-0.05, "fx", 60) == "down"
    assert label_direction(0.01, "fx", 60) == "flat"
    assert label_direction(None, "fx", 60) is None


def test_outcome_returns_prices_and_excursions():
    # candles start 1h before the release and run 2 days; price rises 0.0001 per candle
    candles = ramp(TS0 - 3600, 12 + 12 * 24 * 2 + 20)
    out = compute_outcome(candles, TS0, symbol="EURUSD", kind="fx", now_ts=TS0 + 3 * 86400)
    assert out is not None
    # last finished candle at the release is the one starting 5 minutes before it
    i0 = 12 - 1
    p0 = 1.1000 + i0 * 0.0001
    assert out.price_at_release == pytest.approx(p0)
    # +5m = close of the candle that ends at T0+5m (one step later)
    assert out.prices["5m"] == pytest.approx(p0 + 1 * 0.0001)
    assert out.prices["1h"] == pytest.approx(p0 + 12 * 0.0001)
    assert out.returns["1h"] == pytest.approx(12 * 0.0001 / p0 * 100)
    assert out.returns["1d"] == pytest.approx(288 * 0.0001 / p0 * 100)
    assert out.directions["5m"] == "flat" and out.directions["1h"] == "up" and out.directions["1d"] == "up"
    assert out.direction == "up" and out.direction_horizon == "1h"
    # the 1h window = 12 candles; its highest high is the last close, no dip below the entry price
    assert out.max_up_1h == pytest.approx(12 * 0.0001 / p0 * 100)
    assert out.max_down_1h == 0.0
    assert out.complete


def test_outcome_down_move_excursions():
    closes = [1.1000] * 12 + [1.0990, 1.0950, 1.0960] + [1.0960] * 300
    out = compute_outcome(bars(TS0 - 3600, closes, spread=0.0005), TS0, symbol="EURUSD", kind="fx", now_ts=TS0 + 86400 * 3)
    assert out.price_at_release == pytest.approx(1.1000)
    assert out.returns["15m"] == pytest.approx((1.0960 / 1.1000 - 1) * 100)
    assert out.directions["15m"] == "down"
    assert out.max_down_1h < out.returns["15m"]  # the low (1.0945) is deeper than the close
    assert out.max_up_1h == pytest.approx(0.0005 / 1.1000 * 100)  # only the padding above 1.1000


def test_outcome_horizons_not_elapsed_stay_null():
    candles = ramp(TS0 - 3600, 12 + 6)  # 30 minutes of data after the release
    now = TS0 + 30 * 60
    out = compute_outcome(candles, TS0, symbol="EURUSD", kind="fx", now_ts=now)
    assert out.prices["5m"] is not None and out.prices["15m"] is not None
    assert out.prices["1h"] is None and out.returns["4h"] is None and out.directions["1d"] is None
    assert out.max_up_1h is None and out.max_down_1h is None
    assert out.direction is None and not out.complete


def test_outcome_none_when_market_closed_at_release():
    # data ends 3 hours before the release (weekend gap): no usable price
    assert compute_outcome(ramp(TS0 - 4 * 3600, 12), TS0, symbol="EURUSD", kind="fx", now_ts=TS0 + 86400) is None
    assert compute_outcome([], TS0, symbol="EURUSD") is None
    # release before the first candle
    assert compute_outcome(ramp(TS0 + 3600, 50), TS0, symbol="EURUSD") is None


def test_outcome_gap_inside_horizon_leaves_that_horizon_null():
    candles = ramp(TS0 - 3600, 12 + 12) + ramp(TS0 + 8 * 3600, 400)  # 2h of data, then nothing until +8h
    out = compute_outcome(candles, TS0, symbol="EURUSD", kind="fx", now_ts=TS0 + 3 * 86400)
    assert out.prices["1h"] is not None
    assert out.prices["4h"] is None  # no candle within 10 minutes of T0+4h
    assert out.prices["1d"] is not None


def hourly(start_ts: int, n: int, start: float = 1.1000, step: float = 0.0010) -> list[Candle]:
    return [Candle(time=start_ts + i * 3600, open=start + i * step, high=start + i * step + 0.0005,
                   low=start + i * step - 0.0005, close=start + (i + 1) * step) for i in range(n)]


def test_outcome_on_coarse_candles_leaves_finer_horizons_null():
    candles = hourly(TS0 - 6 * 3600, 6 + 24 * 3)
    out = compute_outcome(candles, TS0, symbol="EURUSD", kind="fx", interval="1h", now_ts=TS0 + 3 * 86400)
    assert out is not None and out.interval == "1h"
    assert out.prices["5m"] is None and out.returns["15m"] is None and out.directions["5m"] is None
    assert out.returns["1h"] is not None and out.returns["4h"] is not None and out.returns["1d"] is not None
    assert out.direction == out.directions["1h"] == "up"
    assert out.max_up_1h is None and out.max_down_1h is None  # 1h candles would mix pre-release action into the first hour
    assert out.complete


def test_outcome_on_daily_candles_only_has_the_one_day_horizon():
    day = 86400
    candles = [Candle(time=TS0 - 10 * day + i * day, open=1.0, high=1.1, low=0.9, close=1.0 + 0.01 * i) for i in range(20)]
    out = compute_outcome(candles, TS0, symbol="EURUSD", kind="fx", interval="1d", now_ts=TS0 + 5 * day)
    assert out is not None
    assert [h for h in out.prices if out.prices[h] is not None] == ["1d"]
    assert out.direction is None  # primary horizon (1h) cannot be answered with daily candles


# ── sources ───────────────────────────────────────────────────────────────

TV_ITEM = {"id": "100", "title": "Non Farm Payrolls", "country": "US", "currency": "USD", "category": "lbr",
           "actual": 165, "forecast": 65, "previous": 12, "unit": None, "scale": "K", "importance": 1,
           "date": "2026-11-06T13:30:00.000Z", "comment": "x" * 900}


def test_from_dict_normalises_history_headlines():
    rec_ = sources.from_dict({"time": "2026-02-03T10:15:00Z", "title": "  Gold jumps as Fed signals cuts  ", "source": "fxstreet",
                              "url": "https://x/1"}, 3)
    assert rec_.time == datetime(2026, 2, 3, 10, 15, tzinfo=UTC) and rec_.kind == "headline" and rec_.source == "fxstreet"
    assert rec_.title == "Gold jumps as Fed signals cuts" and rec_.impact in ("high", "medium", "low")
    assert rec_.symbols and rec_.dedupe_key == sources.from_dict({"time": "2026-02-03T10:15:00Z", "title": "other", "source": "fxstreet",
                                                                  "url": "https://x/1"}, 3).dedupe_key
    explicit = sources.from_dict({"time": T0, "title": "t", "impact": "HIGH", "symbols": ["eurusd", "NOPE"], "currency": "usd",
                                  "dedupe_key": "abc"}, 3)
    assert (explicit.impact, explicit.symbols, explicit.currency, explicit.dedupe_key) == ("high", ["EURUSD"], "USD", "abc")
    assert sources.from_dict({"time": "nope", "title": "t"}, 3) is None
    assert sources.from_dict({"time": T0, "title": " "}, 3) is None


def test_tv_item_mapping():
    rec = sources.from_tv_item(TV_ITEM, 3)
    assert rec.time == T0 and rec.impact == "high" and rec.currency == "USD" and rec.category == "lbr"
    assert (rec.actual, rec.forecast, rec.previous) == ("165K", "65K", "12K")
    assert rec.symbols == ["EURUSD", "USDJPY", "XAUUSD"]
    assert len(rec.summary) == 600 and "comment" not in rec.raw
    assert sources.from_tv_item({**TV_ITEM, "importance": 0}, 3).impact == "medium"
    assert sources.from_tv_item({**TV_ITEM, "importance": -1}, 3).impact == "low"
    assert sources.from_tv_item({**TV_ITEM, "date": "garbage"}, 3) is None
    assert sources.from_tv_item({**TV_ITEM, "id": None}, 3) is None
    pct = sources.from_tv_item({**TV_ITEM, "actual": 4.85, "unit": "%", "scale": None}, 3)
    assert pct.actual == "4.85%"


async def test_fetch_tv_calendar_windows_and_dedupes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        assert request.headers["origin"] == "https://www.tradingview.com"
        return httpx.Response(200, json={"status": "ok", "result": [TV_ITEM, {**TV_ITEM, "id": "101", "date": "2026-11-30T00:00:00Z"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        start = datetime(2026, 11, 1, tzinfo=UTC)
        events, errors = await sources.fetch_tv_calendar(start, datetime(2026, 11, 20, tzinfo=UTC), 3, client=client)
    assert len(calls) == 3  # 19 days -> 7 + 7 + 5
    assert [e.dedupe_key for e in events] == [sources.from_tv_item(TV_ITEM, 3).dedupe_key]  # same id in every window; out-of-range id dropped
    assert errors == []


async def test_fetch_tv_calendar_reports_window_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, errors = await sources.fetch_tv_calendar(T0, T0 + timedelta(days=2), 3, client=client)
    assert events == [] and len(errors) == 1 and "tradingview calendar" in errors[0]
    assert len(calls) == sources.TV_MAX_ATTEMPTS  # retried before giving up


async def test_fetch_tv_calendar_pages_whole_range_without_gaps_and_paces(monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(sources, "_sleep", fake_sleep)
    windows = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        windows.append((p["from"], p["to"]))
        return httpx.Response(200, json={"status": "ok", "result": []})

    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 1, 12, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, errors = await sources.fetch_tv_calendar(start, end, 3, client=client, request_delay=0.5)
    assert errors == [] and events == []
    assert windows[0][0].startswith("2026-01-01T00:00:00") and windows[-1][1].startswith("2026-03-01T12:00:00")
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:]))  # each window starts where the previous ended
    assert len(windows) == 9 and slept == [0.5] * 8  # 59.5 days -> 9 weekly windows, paused between them


async def test_fetch_tv_calendar_retries_429_honouring_retry_after(monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(sources, "_sleep", fake_sleep)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if len(attempts) == 2:
            return httpx.Response(429)  # no header: exponential backoff
        return httpx.Response(200, json={"status": "ok", "result": [TV_ITEM]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, errors = await sources.fetch_tv_calendar(T0 - timedelta(days=1), T0 + timedelta(days=1), 3, client=client)
    assert errors == [] and len(events) == 1 and len(attempts) == 3
    assert slept == [7.0, sources.TV_BACKOFF_BASE_S * 2]


# ── storage ───────────────────────────────────────────────────────────────


def rec(**kw) -> sources.EventRecord:
    base = dict(time=T0, kind="calendar", source="tradingview", title="Non Farm Payrolls", impact="high",
                dedupe_key="k1", currency="USD", actual=None, forecast="65K", symbols=["EURUSD", "XAUUSD"], raw={"a": 1})
    return sources.EventRecord(**{**base, **kw})


def test_store_events_is_idempotent_and_refreshes_actuals():
    assert ingest.store_events([rec(), rec(dedupe_key="k2", title="CPI")]) == (2, 0)
    assert ingest.store_events([rec(), rec(dedupe_key="k2", title="CPI")]) == (0, 0)
    assert ingest.store_events([rec(actual="165K", impact="medium")]) == (0, 1)
    with session_scope() as s:
        row = s.scalar(select(BtEvent).where(BtEvent.dedupe_key == "k1"))
        assert (row.actual, row.impact, row.symbols, row.raw) == ("165K", "medium", ["EURUSD", "XAUUSD"], {"a": 1})
        assert row.symbols_csv == ",EURUSD,XAUUSD,"
        assert row.time == T0  # timezone-aware UTC round trip


def fake_fetcher(calls: list):
    async def fetch(symbol: str, interval: str):
        calls.append((symbol, interval))
        if symbol == "XAUUSD":
            raise RuntimeError("no data")
        return ramp(TS0 - 7 * 86400, 7 * 288 + 12 * 24 * 3 + 200)

    return fetch


async def test_compute_outcomes_stores_rows_skips_future_and_failures():
    future = T0 + timedelta(days=30)
    ingest.store_events([rec(), rec(dedupe_key="fut", time=future, title="Later")])
    now = T0 + timedelta(hours=5)  # +1d not elapsed yet
    calls: list = []
    result = ingest.BackfillResult()
    await ingest.compute_outcomes(T0 - timedelta(days=1), future + timedelta(days=1), result,
                                  fetcher=fake_fetcher(calls), now=now)
    assert result.outcomes_created == 1 and result.outcomes_skipped_future == 2
    assert result.symbols_without_candles == ["XAUUSD"] and result.outcomes_skipped_no_data == 1
    assert {c[0] for c in calls} == {"EURUSD", "XAUUSD"} and calls.count(("XAUUSD", "5m")) == ingest.FETCH_ATTEMPTS
    with session_scope() as s:
        o = s.scalar(select(BtOutcome))
        assert o.symbol == "EURUSD" and o.interval == "5m" and o.complete is False and o.ret_1d is None  # 1d horizon not elapsed
        assert o.ret_1h is not None and o.dir_1h == "up" and o.direction == "up"

    # a later run (all horizons elapsed) fills the rest and marks the row final; completed rows are then left alone
    later = T0 + timedelta(days=3)
    r2 = ingest.BackfillResult()
    await ingest.compute_outcomes(T0 - timedelta(days=1), T0 + timedelta(days=1), r2, fetcher=fake_fetcher([]), now=later)
    assert r2.outcomes_updated == 1 and r2.outcomes_created == 0
    r3 = ingest.BackfillResult()
    await ingest.compute_outcomes(T0 - timedelta(days=1), T0 + timedelta(days=1), r3, fetcher=fake_fetcher([]), now=later)
    assert r3.outcomes_skipped_complete == 1 and r3.outcomes_updated == 0
    with session_scope() as s:
        assert s.scalar(select(BtOutcome)).complete is True and s.scalar(select(BtOutcome)).ret_1d is not None


async def test_fetch_with_retry_waits_for_provider_pause():
    attempts, slept = [], []

    async def fetcher(symbol, interval):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("EURUSD: TradingView paused for another 48s (connection/protocol error)")
        return ["ok"]

    async def fake_sleep(delay):
        slept.append(delay)

    assert await ingest._fetch_with_retry(fetcher, "EURUSD", "5m", sleep=fake_sleep) == ["ok"]
    assert slept == [50, 50]


async def test_compute_outcomes_picks_finest_interval_covering_each_event():
    """5m history covers 2 days, 15m 6 days, 1h 30 days, 1d 400 days; candles are fetched once per interval."""
    now = T0 + timedelta(days=40)
    day = 86400
    history_days = {"5m": 2, "15m": 6, "1h": 30, "1d": 400}
    step = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
    ages = {"e_5m": 1, "e_15m": 5, "e_1h": 20, "e_1d": 200, "e_old": 600}  # days before ``now``
    ingest.store_events([rec(dedupe_key=k, title=k, time=now - timedelta(days=d, hours=-1), symbols=["EURUSD"]) for k, d in ages.items()])
    calls: list = []

    async def fetch(symbol, interval):
        calls.append((symbol, interval))
        n = history_days[interval] * day // step[interval]
        first = int(now.timestamp()) - n * step[interval]
        return [Candle(time=first + i * step[interval], open=1.1, high=1.1, low=1.1, close=1.1 + i * 1e-7) for i in range(n)]

    result = ingest.BackfillResult()
    await ingest.compute_outcomes(now - timedelta(days=700), now, result, fetcher=fetch, now=now)
    assert calls == [("EURUSD", "5m"), ("EURUSD", "15m"), ("EURUSD", "1h"), ("EURUSD", "1d")]  # once per interval
    assert result.outcomes_by_interval == {"5m": 1, "15m": 1, "1h": 1, "1d": 1}
    assert result.outcomes_skipped_not_covered == 1  # older than even the daily history
    with session_scope() as s:
        rows = {e.dedupe_key: o for e, o in s.execute(select(BtEvent, BtOutcome).join(BtOutcome, BtOutcome.event_id == BtEvent.id))}
    assert {k: o.interval for k, o in rows.items()} == {"e_5m": "5m", "e_15m": "15m", "e_1h": "1h", "e_1d": "1d"}
    assert rows["e_5m"].ret_5m is not None or rows["e_5m"].price_5m is not None
    assert rows["e_15m"].ret_5m is None and rows["e_15m"].ret_15m is not None
    assert rows["e_1h"].ret_5m is None and rows["e_1h"].ret_15m is None and rows["e_1h"].ret_1h is not None
    assert rows["e_1d"].ret_1h is None and rows["e_1d"].ret_4h is None and rows["e_1d"].ret_1d is not None


async def test_compute_outcomes_does_not_fetch_coarser_intervals_when_not_needed():
    now = T0 + timedelta(hours=3)
    ingest.store_events([rec(symbols=["EURUSD"])])
    calls: list = []

    async def fetch(symbol, interval):
        calls.append(interval)
        return ramp(TS0 - 86400, 288 * 2)

    result = ingest.BackfillResult()
    await ingest.compute_outcomes(T0 - timedelta(days=1), T0 + timedelta(days=1), result, fetcher=fetch, now=now)
    assert calls == ["5m"] and result.outcomes_by_interval == {"5m": 1}


async def test_market_closed_at_release_is_not_escalated_to_coarser_candles():
    ingest.store_events([rec(symbols=["EURUSD"])])
    now = T0 + timedelta(days=3)
    calls: list = []

    async def fetch(symbol, interval):
        calls.append(interval)
        return ramp(TS0 - 86400, 100)  # history covers the event but ends 20h before it: nothing priced at release

    result = ingest.BackfillResult()
    await ingest.compute_outcomes(T0 - timedelta(days=1), T0 + timedelta(days=1), result, fetcher=fetch, now=now)
    assert calls == ["5m"] and result.outcomes_skipped_no_data == 1 and result.outcomes_created == 0


async def test_recompute_keeps_a_finer_outcome_already_stored():
    ingest.store_events([rec(symbols=["EURUSD"])])
    now = T0 + timedelta(hours=3)  # incomplete: +1d not elapsed
    first = ingest.BackfillResult()
    await ingest.compute_outcomes(T0 - timedelta(days=1), T0 + timedelta(days=1), first,
                                  fetcher=fake_fetcher([]), now=now)
    assert first.outcomes_by_interval == {"5m": 1}
    later = T0 + timedelta(days=2)

    async def only_coarse(symbol, interval):
        if interval == "5m":
            return ramp(TS0 + 3600, 10)  # 5m history no longer reaches the event
        return hourly(TS0 - 6 * 3600, 80)

    r2 = ingest.BackfillResult()
    await ingest.compute_outcomes(T0 - timedelta(days=1), T0 + timedelta(days=1), r2, fetcher=only_coarse, now=later)
    assert r2.outcomes_created == r2.outcomes_updated == 0 and r2.outcomes_skipped_complete == 1
    with session_scope() as s:
        assert s.scalar(select(BtOutcome)).interval == "5m"


async def test_collect_events_merges_headline_history_hook(monkeypatch):
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    items = [
        {"time": "2026-01-10T09:00:00Z", "title": "Dollar slides after jobs data", "source": "fxstreet", "url": "https://x/1"},
        {"time": "2026-01-10T09:00:00Z", "title": "Dollar slides after jobs data", "source": "fxstreet", "url": "https://x/1"},  # dup
        {"time": "2026-01-10T11:00:00Z", "title": "Dollar slides  after jobs data!", "source": "fxstreet", "url": "https://x/2"},  # same story
        {"time": "2026-01-12T09:00:00Z", "title": "Gold hits record", "source": "reuters"},
        {"time": "2026-03-01T09:00:00Z", "title": "Out of range", "source": "reuters"},
        {"time": "garbage", "title": "bad"},
    ]

    async def fetch_headline_history(s, e):
        assert (s, e) == (start, end)
        return items

    monkeypatch.setattr(ingest, "_load_headline_history", lambda: fetch_headline_history)

    async def no_tv(*a, **k):
        return [], []

    monkeypatch.setattr(sources, "fetch_tv_calendar", no_tv)
    result = ingest.BackfillResult()
    records = await ingest.collect_events(start, end, result, live=False, history=True)
    assert sorted(r.title for r in records) == ["Dollar slides after jobs data", "Gold hits record"]
    assert result.errors == [] and "fxstreet (history)" in result.collected

    # a broken hook is reported, never fatal
    async def boom(s, e):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(ingest, "_load_headline_history", lambda: boom)
    result2 = ingest.BackfillResult()
    assert await ingest.collect_events(start, end, result2, live=False, history=True) == []
    assert result2.errors and "headline_history" in result2.errors[0]

    # absent module -> silently skipped; ``live=False`` leaves the hook off by default
    monkeypatch.setattr(ingest, "_load_headline_history", lambda: None)
    assert await ingest.collect_events(start, end, ingest.BackfillResult(), live=False, history=True) == []


def test_load_headline_history_absent_or_present(monkeypatch):
    import sys
    import types

    monkeypatch.delitem(sys.modules, "app.backtest.headline_history", raising=False)
    monkeypatch.setitem(sys.modules, "app.backtest.headline_history", None)  # import raises ModuleNotFoundError
    assert ingest._load_headline_history() is None
    fake = types.ModuleType("app.backtest.headline_history")

    async def fetch_headline_history(start, end):
        return []

    fake.fetch_headline_history = fetch_headline_history
    monkeypatch.setitem(sys.modules, "app.backtest.headline_history", fake)
    assert ingest._load_headline_history() is fetch_headline_history


# ── predictions ───────────────────────────────────────────────────────────

REPLY = json.dumps({"predictions": [
    {"symbol": "EURUSD", "direction": "Bullish", "confidence": "72%", "horizon": "1h", "reasoning": "weak USD"},
    {"symbol": "xauusd", "direction": "down", "confidence": 150, "reasoning": ""},
    {"symbol": "GBPUSD", "direction": "up", "confidence": 50},  # not asked for
]})


def test_parse_predictions_normalises_and_filters():
    rows = predict.parse_predictions("```json\n" + REPLY + "\n```", ["EURUSD", "XAUUSD"], "1h")
    assert [(r["symbol"], r["direction"], r["confidence"]) for r in rows] == [("EURUSD", "up", 72), ("XAUUSD", "down", 100)]
    assert rows[0]["reasoning"] == "weak USD" and rows[1]["reasoning"] is None and rows[1]["horizon"] == "1h"
    with pytest.raises(ValueError):
        predict.parse_predictions('{"predictions": [{"symbol": "EURUSD", "direction": "maybe"}]}', ["EURUSD"], "1h")
    with pytest.raises(Exception):
        predict.parse_predictions("no json here", ["EURUSD"], "1h")


def test_prompt_contains_release_facts_but_no_prices():
    ingest.store_events([rec(actual="165K")])
    with session_scope() as s:
        ev = s.scalar(select(BtEvent))
    prompt = predict.build_prompt(ev, ["EURUSD", "XAUUSD"], "1h")
    assert "Non Farm Payrolls" in prompt and "Actual: 165K | Forecast: 65K" in prompt
    assert "EURUSD (up = EUR gains vs USD)" in prompt and "2026-11-06 13:30" in prompt


class FakeClient:
    """Stands in for LLMClient: records calls, answers per model."""

    def __init__(self, problems=None, fail=()):
        self.settings = Settings()
        self.calls: list[str] = []
        self.problems = problems or {}
        self.fail = set(fail)

    def provider(self, name):
        problem = self.problems.get(name)
        return type("P", (), {"problem": lambda self_: problem})()

    async def achat_spec(self, spec, prompt, **kw):
        from app.llm import ChatResult, LLMError

        self.calls.append(spec)
        if spec in self.fail:
            raise LLMError("boom")
        syms = [s for s in ("EURUSD", "XAUUSD") if s in prompt]
        direction = "up" if spec.endswith("bull") else "down"
        body = {"predictions": [{"symbol": s, "direction": direction, "confidence": 60, "reasoning": "r"} for s in syms]}
        return ChatResult(text=json.dumps(body), model=spec, provider="x")


async def test_run_predictions_many_models_skip_existing_and_limits(monkeypatch):
    monkeypatch.setattr(predict.get_settings(), "backtest_llm_min_interval_seconds", 0.0)
    ingest.store_events([rec(), rec(dedupe_key="k2", time=T0 + timedelta(hours=1), title="CPI"),
                         rec(dedupe_key="low", time=T0 + timedelta(hours=2), impact="low", title="Minor"),
                         rec(dedupe_key="nosym", time=T0 + timedelta(hours=3), symbols=[], title="No symbol")])
    client = FakeClient(problems={"claude_code": "CLI missing"}, fail={"openrouter:bad"})
    models = ["openrouter:bull", "groq:bear", "claude_code:haiku", "openrouter:bad"]
    now = T0 + timedelta(days=1)
    start, end = T0 - timedelta(days=1), T0 + timedelta(days=1)

    res = await predict.run_predictions(start, end, models, client=client, now=now)
    assert res.events_considered == 2  # medium+ with symbols; the low-impact and symbol-less ones are out
    assert res.models_unavailable == {"claude_code:haiku": "CLI missing"}
    assert res.per_model == {"openrouter:bull": 4, "groq:bear": 4}  # 2 events x 2 symbols
    assert any("stopped after" in e or "boom" in e for e in res.errors)
    assert client.calls.count("openrouter:bad") == 2
    with session_scope() as s:
        assert s.query(BtPrediction).count() == 8
        first = s.scalar(select(BtPrediction).where(BtPrediction.model == "openrouter:bull"))
        assert (first.direction, first.confidence, first.horizon, first.reasoning) == ("up", 60, "1h", "r")

    # second run: nothing new for the finished models, only the failing one is retried
    client.calls.clear()
    again = await predict.run_predictions(start, end, models, client=client, now=now)
    assert again.predictions_created == 0 and again.skipped_existing == 4
    assert "openrouter:bull" not in client.calls and "groq:bear" not in client.calls

    # include low impact + limit
    client.calls.clear()
    third = await predict.run_predictions(start, end, ["openrouter:bull"], client=client, now=now, min_impact="low", limit=1)
    assert third.predictions_created == 2 and client.calls == ["openrouter:bull"]

    # future events are never predicted
    nothing = await predict.run_predictions(start, end, ["groq:new"], client=client, now=T0 - timedelta(hours=1))
    assert nothing.events_considered == 0


# ── per-event verify / predict / prompt context ───────────────────────────


def _event_id(key: str) -> int:
    with session_scope() as s:
        return s.scalar(select(BtEvent.id).where(BtEvent.dedupe_key == key))


def _ctx_prompt(key: str, **kw) -> str:
    with session_scope() as s:
        ev = s.scalar(select(BtEvent).where(BtEvent.dedupe_key == key))
        s.expunge(ev)
    return predict.build_prompt(ev, ev.symbols, "1h", predict.load_context(ev), **kw)


def test_prompt_context_is_strictly_earlier_related_and_priceless():
    ingest.store_events([
        rec(dedupe_key="main", actual="165K", title="Non Farm Payrolls"),
        rec(dedupe_key="before", time=T0 - timedelta(hours=2), title="ADP Employment", actual="150K", forecast="140K", previous="120K"),
        rec(dedupe_key="same", time=T0, title="SAMETIME-EVENT"),  # same timestamp: not "earlier"
        rec(dedupe_key="after", time=T0 + timedelta(minutes=1), title="LATER-EVENT"),
        rec(dedupe_key="old", time=T0 - timedelta(hours=30), title="TOO-OLD"),
        rec(dedupe_key="other", time=T0 - timedelta(hours=1), title="UNRELATED", currency="JPY", symbols=["USDJPY"]),
        rec(dedupe_key="head", time=T0 - timedelta(minutes=10), kind="headline", title="Fed speaker hawkish", currency="USD",
            symbols=["XAUUSD"], actual=None, forecast=None),
    ])
    prompt = _ctx_prompt("main", now=T0 + timedelta(days=1))
    assert "Earlier context" in prompt
    assert prompt.index("Fed speaker hawkish") < prompt.index("ADP Employment")  # newest first
    assert "ADP Employment | Actual: 150K | Forecast: 140K | Previous: 120K" in prompt
    for banned in ("SAMETIME-EVENT", "LATER-EVENT", "TOO-OLD", "UNRELATED"):
        assert banned not in prompt
    assert "outcome" not in prompt.lower() and "return" not in prompt.lower() and "price" not in prompt.lower().replace("no price", "")
    assert "published before the release" in predict.SYSTEM_PROMPT

    # a context item handed in by a careless caller is still dropped when it is not strictly earlier
    with session_scope() as s:
        ev = s.scalar(select(BtEvent).where(BtEvent.dedupe_key == "main"))
        leaked = predict.ContextItem(time=T0, kind="headline", impact="high", title="LEAK-AT-RELEASE")
        assert "LEAK-AT-RELEASE" not in predict.build_prompt(ev, ["EURUSD"], "1h", [leaked])


def test_prompt_context_limits_future_actuals_and_can_be_disabled(monkeypatch):
    now = T0 - timedelta(hours=1)
    ingest.store_events([
        rec(dedupe_key="main", time=T0, actual="999K"),  # future calendar event: its actual is unknown
        rec(dedupe_key="past", time=T0 - timedelta(hours=3), title="Past", actual="1.0%"),
        rec(dedupe_key="fut", time=T0 - timedelta(minutes=30), title="FutureButEarlier", actual="2.0%"),
    ])
    prompt = _ctx_prompt("main", now=now)
    assert "Actual: n/a | Forecast: 65K" in prompt and "999K" not in prompt
    assert "Past | Actual: 1.0%" in prompt and "FutureButEarlier | Actual: n/a" in prompt

    monkeypatch.setattr(predict.get_settings(), "backtest_prompt_context_items", 1)
    assert "Past" not in _ctx_prompt("main", now=now) and "FutureButEarlier" in _ctx_prompt("main", now=now)
    monkeypatch.setattr(predict.get_settings(), "backtest_prompt_context_items", 0)
    assert "Earlier context" not in _ctx_prompt("main", now=now)


async def test_predict_one_stores_skips_existing_and_force_replaces(monkeypatch):
    monkeypatch.setattr(predict.get_settings(), "backtest_llm_min_interval_seconds", 0.0)
    ingest.store_events([rec(), rec(dedupe_key="k2", time=T0 + timedelta(hours=1), title="CPI")])
    eid, other = _event_id("k1"), _event_id("k2")
    client = FakeClient(fail={"openrouter:bad"}, problems={"claude_code": "CLI missing"})
    now = T0 + timedelta(days=1)

    res = await predict.predict_one(eid, ["openrouter:bull", "groq:bear", "openrouter:bad", "claude_code:x"], client=client, now=now)
    by = {r["model"]: r for r in res}
    assert by["openrouter:bull"]["status"] == "ok" and by["openrouter:bull"]["predictions_created"] == 2
    assert by["openrouter:bull"]["latency_ms"] is not None and by["openrouter:bull"]["error"] is None
    assert by["openrouter:bad"]["status"] == "error" and "boom" in by["openrouter:bad"]["error"]
    assert by["claude_code:x"]["status"] == "error" and by["claude_code:x"]["error"] == "CLI missing"
    assert sorted(client.calls) == ["groq:bear", "openrouter:bad", "openrouter:bull"]  # one call per model, none for the problem
    with session_scope() as s:
        assert s.query(BtPrediction).filter_by(event_id=eid).count() == 4
        assert s.query(BtPrediction).filter_by(event_id=other).count() == 0  # the other event is untouched

    # existing predictions: no new call, status skipped
    client.calls.clear()
    again = {r["model"]: r for r in await predict.predict_one(eid, ["openrouter:bull"], client=client, now=now)}
    assert again["openrouter:bull"] == {"model": "openrouter:bull", "status": "skipped", "error": None,
                                         "predictions_created": 0, "latency_ms": None}
    assert client.calls == []

    # force replaces the model's old rows (the fake answers "up" for names ending in bull)
    with session_scope() as s:
        for p in s.scalars(select(BtPrediction).where(BtPrediction.model == "openrouter:bull")):
            p.direction = "flat"
    forced = await predict.predict_one(eid, ["openrouter:bull"], force=True, client=client, now=now)
    assert forced[0]["status"] == "ok" and forced[0]["predictions_created"] == 2 and client.calls == ["openrouter:bull"]
    with session_scope() as s:
        rows = s.scalars(select(BtPrediction).where(BtPrediction.model == "openrouter:bull")).all()
        assert len(rows) == 2 and {r.direction for r in rows} == {"up"}

    # a failing forced call keeps the old answer
    with session_scope() as s:
        s.add(BtPrediction(event_id=eid, symbol="EURUSD", model="openrouter:bad", direction="up"))
    failed = await predict.predict_one(eid, ["openrouter:bad"], force=True, client=client, now=now)
    assert failed[0]["status"] == "error"
    with session_scope() as s:
        assert s.query(BtPrediction).filter_by(event_id=eid, model="openrouter:bad").count() == 1

    with pytest.raises(LookupError):
        await predict.predict_one(999999, ["groq:bear"], client=client)
    ingest.store_events([rec(dedupe_key="nosym", time=T0 + timedelta(hours=3), symbols=[], title="None")])
    with pytest.raises(ValueError):
        await predict.predict_one(_event_id("nosym"), ["groq:bear"], client=client)


async def test_predict_prompt_uses_context_before_release_only(monkeypatch):
    monkeypatch.setattr(predict.get_settings(), "backtest_llm_min_interval_seconds", 0.0)
    ingest.store_events([rec(), rec(dedupe_key="before", time=T0 - timedelta(hours=1), title="BEFORE-ITEM"),
                         rec(dedupe_key="after", time=T0 + timedelta(hours=1), title="AFTER-ITEM")])
    prompts: list[str] = []

    class Spy(FakeClient):
        async def achat_spec(self, spec, prompt, **kw):
            prompts.append(prompt)
            return await super().achat_spec(spec, prompt, **kw)

    await predict.predict_one(_event_id("k1"), ["groq:bear"], client=Spy(), now=T0 + timedelta(days=1))
    assert "BEFORE-ITEM" in prompts[0] and "AFTER-ITEM" not in prompts[0]
    # the bulk runner uses the same context
    prompts.clear()
    await predict.run_predictions(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1), ["groq:other"], client=Spy(),
                                  now=T0 + timedelta(days=1))
    assert len(prompts) == 1 and "BEFORE-ITEM" in prompts[0] and "AFTER-ITEM" not in prompts[0]


async def test_verify_event_only_touches_that_event():
    ingest.store_events([rec(symbols=["EURUSD"]), rec(dedupe_key="twin", title="Twin at same time", symbols=["EURUSD"]),
                         rec(dedupe_key="later", time=T0 + timedelta(hours=1), title="Later", symbols=["EURUSD"])])
    now = T0 + timedelta(days=3)
    calls: list = []
    res = await ingest.verify_event(_event_id("k1"), fetcher=fake_fetcher(calls), now=now)
    assert res.outcomes_created == 1 and res.outcomes_by_interval == {"5m": 1}
    with session_scope() as s:
        assert [o.event_id for o in s.scalars(select(BtOutcome))] == [_event_id("k1")]
        assert s.scalar(select(BtOutcome)).complete is True

    again = await ingest.verify_event(_event_id("k1"), fetcher=fake_fetcher([]), now=now)
    assert again.outcomes_skipped_complete == 1 and again.outcomes_created == again.outcomes_updated == 0
    forced = await ingest.verify_event(_event_id("k1"), force=True, fetcher=fake_fetcher([]), now=now)
    assert forced.outcomes_updated == 1 and forced.outcomes_skipped_complete == 0

    with pytest.raises(LookupError):
        await ingest.verify_event(999999, now=now)
    with pytest.raises(ValueError):
        await ingest.verify_event(_event_id("later"), fetcher=fake_fetcher([]), now=T0)


def test_api_single_event_endpoints(monkeypatch):
    monkeypatch.setattr(predict.get_settings(), "backtest_llm_min_interval_seconds", 0.0)
    ingest.store_events([rec(symbols=["EURUSD", "XAUUSD"], actual="165K"),
                         rec(dedupe_key="before", time=T0 - timedelta(hours=1), title="BEFORE-ITEM"),
                         rec(dedupe_key="fut", time=utc_now_plus(days=30), title="Future release")])
    eid, fut = _event_id("k1"), _event_id("fut")
    client_stub = FakeClient()
    monkeypatch.setattr(predict, "get_client", lambda: client_stub)

    async def fetcher(symbol, interval):
        if symbol == "XAUUSD":
            raise RuntimeError("no data")
        return ramp(TS0 - 7 * 86400, 7 * 288 + 12 * 24 * 3 + 200)

    monkeypatch.setattr(ingest, "default_candle_fetcher", fetcher)
    with TestClient(app) as client:
        got = client.get(f"/api/backtest/events/{eid}")
        assert got.status_code == 200 and got.json()["title"] == "Non Farm Payrolls" and got.json()["outcomes"] == []
        assert client.get("/api/backtest/events/999999").status_code == 404

        v = client.post(f"/api/backtest/events/{eid}/verify")
        assert v.status_code == 200, v.text
        body = v.json()
        assert set(body["verify"]) == set(("outcomes_created outcomes_updated outcomes_skipped_future outcomes_skipped_no_data "
                                           "outcomes_skipped_not_covered outcomes_skipped_complete outcomes_by_interval "
                                           "symbols_without_candles errors").split())
        assert body["verify"]["outcomes_created"] == 1 and body["verify"]["symbols_without_candles"] == ["XAUUSD"]
        assert [o["symbol"] for o in body["event"]["outcomes"]] == ["EURUSD"]
        assert client.post(f"/api/backtest/events/{eid}/verify", json={"force": True}).status_code == 200
        assert client.post(f"/api/backtest/events/{fut}/verify").status_code == 422
        assert client.post("/api/backtest/events/999999/verify").status_code == 404

        p = client.post(f"/api/backtest/events/{eid}/predict", json={"models": ["openrouter:bull", "groq:bear"]})
        assert p.status_code == 200, p.text
        out = p.json()
        assert [(r["model"], r["status"], r["predictions_created"]) for r in out["results"]] == [
            ("openrouter:bull", "ok", 2), ("groq:bear", "ok", 2)]
        assert set(out["event"]["predictions"]) == {"openrouter:bull", "groq:bear"}
        again = client.post(f"/api/backtest/events/{eid}/predict", json={"models": ["openrouter:bull"]}).json()
        assert again["results"][0]["status"] == "skipped"
        assert client.post(f"/api/backtest/events/{eid}/predict", json={"models": ["openrouter:"]}).status_code == 422
        assert client.post(f"/api/backtest/events/{eid}/predict", json={"models": []}).status_code == 422
        assert client.post("/api/backtest/events/999999/predict", json={"models": ["groq:x"]}).status_code == 404
        nosym = rec(dedupe_key="nosym", time=T0 + timedelta(hours=3), symbols=[], title="None")
        ingest.store_events([nosym])
        assert client.post(f"/api/backtest/events/{_event_id('nosym')}/predict", json={"models": ["groq:x"]}).status_code == 422
        # a future event is a forecast: allowed
        assert client.post(f"/api/backtest/events/{fut}/predict", json={"models": ["groq:x"]}).json()["results"][0]["status"] == "ok"

        pr = client.get(f"/api/backtest/events/{eid}/prompt", params={"horizon": "4h"}).json()
        assert pr["system"] == predict.SYSTEM_PROMPT and "Horizon: 4h" in pr["prompt"] and "BEFORE-ITEM" in pr["prompt"]
        assert client.get(f"/api/backtest/events/{eid}/prompt", params={"horizon": "9h"}).status_code == 422
        assert client.get("/api/backtest/events/999999/prompt").status_code == 404


def utc_now_plus(**kw) -> datetime:
    return datetime.now(UTC) + timedelta(**kw)


# ── summary + API ─────────────────────────────────────────────────────────


def seed_scored_dataset():
    """3 events with known outcomes (EURUSD) and two models with known hits."""
    ingest.store_events([
        rec(dedupe_key="a", time=T0, impact="high", symbols=["EURUSD"]),
        rec(dedupe_key="b", time=T0 + timedelta(hours=1), impact="high", symbols=["EURUSD"], title="B"),
        rec(dedupe_key="c", time=T0 + timedelta(hours=2), impact="low", symbols=["EURUSD"], title="C"),
    ])
    labels = {"a": "up", "b": "down", "c": "flat"}
    with session_scope() as s:
        for e in s.scalars(select(BtEvent)).all():
            lab = labels[e.dedupe_key]
            s.add(BtOutcome(event_id=e.id, symbol="EURUSD", interval="5m", price_at_release=1.1, ret_1h=0.1, dir_1h=lab,
                            dir_15m="up", direction=lab, direction_horizon="1h", complete=True, max_up_1h=0.2, max_down_1h=-0.1))
            for model, guesses in (("m:bull", {"a": "up", "b": "up", "c": "up"}), ("m:half", {"a": "up", "b": "down", "c": "up"})):
                s.add(BtPrediction(event_id=e.id, symbol="EURUSD", model=model, direction=guesses[e.dedupe_key], confidence=80, horizon="1h"))


def test_summary_hit_rates_by_impact_and_horizon():
    seed_scored_dataset()
    with session_scope() as s:
        out = stats.summary(s, None, None)
    assert out["events"] == 3 and out["outcomes"] == 3 and out["primary_horizon"] == "1h"
    models = {m["model"]: m for m in out["models"]}
    bull, half = models["m:bull"], models["m:half"]
    assert (bull["overall"]["n"], bull["overall"]["hits"]) == (3, 1)
    assert (half["overall"]["hits"], half["overall"]["hit_rate"]) == (2, 0.6667)
    assert half["by_impact"]["high"]["hit_rate"] == 1.0 and half["by_impact"]["low"]["hit_rate"] == 0.0
    assert half["by_horizon"]["1h"]["directional_n"] == 2  # the flat outcome is excluded from the directional rate
    assert half["by_horizon"]["1h"]["directional_hit_rate"] == 1.0
    assert bull["by_horizon"]["15m"]["hits"] == 3  # every 15m label is "up"
    assert half["by_impact_horizon"]["high"]["1h"]["n"] == 2
    assert half["avg_confidence"] == 80.0
    assert out["baseline_by_horizon"]["1h"]["n"] == 3
    assert sum(out["outcomes_by_interval"].values()) == out["outcomes"] == 3

    with session_scope() as s:
        only_high = stats.summary(s, None, None, impact=["high"])
        assert only_high["events"] == 2
        other_symbol = stats.summary(s, None, None, symbol="USDJPY")
        assert other_symbol["events"] == 0 and other_symbol["models"] == []


def test_api_events_summary_and_validation():
    seed_scored_dataset()
    with TestClient(app) as client:
        res = client.get("/api/backtest/events", params={"from": "2026-11-06", "to": "2026-11-06"})
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 3 and body["models"] == ["m:bull", "m:half"]
        first = body["events"][-1]  # newest first -> the 13:30 event is last
        assert first["title"] == "Non Farm Payrolls" and first["impact"] == "high"
        assert first["outcomes"][0]["directions"]["1h"] == "up" and first["outcomes"][0]["max_down_1h"] == -0.1
        assert set(first["predictions"]) == {"m:bull", "m:half"}
        assert first["predictions"]["m:half"][0]["confidence"] == 80

        assert client.get("/api/backtest/events", params={"impact": "high"}).json()["total"] == 2
        assert client.get("/api/backtest/events", params={"symbol": "USDJPY"}).json()["total"] == 0
        assert client.get("/api/backtest/events", params={"from": "2026-11-07"}).json()["total"] == 0
        assert client.get("/api/backtest/events", params={"impact": "huge"}).status_code == 422
        assert client.get("/api/backtest/events", params={"from": "nope"}).status_code == 422

        summ = client.get("/api/backtest/summary", params={"horizon": "15m"}).json()
        assert summ["primary_horizon"] == "15m"
        assert {m["model"]: m["overall"]["hit_rate"] for m in summ["models"]} == {"m:bull": 1.0, "m:half": 0.6667}

        models = client.get("/api/backtest/models").json()
        assert {p["name"] for p in models["providers"]} >= {"openrouter", "groq", "hive", "claude_code"}
        assert all("id" in m for m in models["models"])


def test_api_predict_validates_models_and_jobs():
    with TestClient(app) as client:
        bad = client.post("/api/backtest/predict", json={"models": ["openrouter:"]})
        assert bad.status_code == 422
        assert client.post("/api/backtest/predict", json={"models": ["x"], "min_impact": "huge"}).status_code == 422
        assert client.get("/api/backtest/jobs/unknown").status_code == 404
        assert client.post("/api/backtest/backfill", json={"from": "2026-11-05", "to": "2026-11-01"}).status_code == 422


# ── ported LLM layer ──────────────────────────────────────────────────────


def test_parse_model_spec():
    assert parse_model_spec("openrouter:google/gemini-2.5-flash") == ("openrouter", "google/gemini-2.5-flash")
    assert parse_model_spec("meta-llama/llama-3:free") == ("openrouter", "meta-llama/llama-3:free")  # not a provider prefix
    assert parse_model_spec("claude_code:claude-haiku-5-5") == ("claude_code", "claude-haiku-5-5")
    assert parse_model_spec("llama-3.3", "groq") == ("groq", "llama-3.3")


def test_settings_from_env_never_invents_keys(monkeypatch):
    from app.config import get_settings

    cfg = get_settings()
    monkeypatch.setattr(cfg, "groq_api_key", "gsk_one")
    monkeypatch.setattr(cfg, "groq_api_keys", "gsk_two, gsk_one ,gsk_three")
    monkeypatch.setattr(cfg, "openrouter_api_key", "")
    s = Settings.from_app(cfg)
    assert [k["key"] for k in s.section("groq")["api_keys"]] == ["gsk_one", "gsk_two", "gsk_three"]
    assert s.section("openrouter")["api_keys"] == []
    assert s.timeout == cfg.llm_timeout_seconds


def test_provider_key_rotation_and_chat_over_mock_transport():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if request.headers["authorization"].endswith("1111"):
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]})

    keys = [{"name": "a", "key": "gsk_aaaaaaaa1111"}, {"name": "b", "key": "gsk_bbbbbbbb2222"}]
    settings = Settings(max_retries=0, providers={"groq": {"api_keys": keys}})
    provider = GroqProvider(settings, transport=httpx.MockTransport(handler))
    client = LLMClient(settings, providers={"groq": provider})
    assert client.chat("ping", provider="groq").text == "pong"
    assert seen == ["Bearer gsk_aaaaaaaa1111", "Bearer gsk_bbbbbbbb2222"]  # first key refused -> next answers, same request
    assert client.chat("ping", provider="groq").text == "pong"
    assert seen[2:] == ["Bearer gsk_bbbbbbbb2222"]  # the refused key rests


def test_job_cancel_stops_running_job():
    import asyncio

    from app.backtest import jobs

    async def scenario():
        async def runner(progress):
            progress("working")
            await asyncio.sleep(30)

        job = jobs.start("predict", runner)
        await asyncio.sleep(0)
        assert jobs.cancel(job.id).message == "stopping"
        for _ in range(50):
            if job.status != "running":
                break
            await asyncio.sleep(0.01)
        assert job.status == "cancelled"
        assert job.finished_at is not None
        assert jobs.running("predict") is None
        assert jobs.cancel("missing") is None

    asyncio.run(scenario())
