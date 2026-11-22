"""Market reaction: candle selection, labels, pending handling, stored outcomes, cache and the API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.backtest.models import BtEvent, BtOutcome
from app.db import init_db, session_scope
from app.main import app
from app.market import candles as candle_service
from app.market import reaction
from app.schemas import Candle, CandlesOut

T0 = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
TS0 = int(T0.timestamp())


def make_candles(step: int, start_ts: int, n: int, p0: float = 2000.0, per_bar: float = 0.0) -> list[Candle]:
    out, prev = [], p0
    for i in range(n):
        close = p0 + per_bar * (i + 1)
        out.append(Candle(time=start_ts + i * step, open=prev, high=max(prev, close) + 0.1, low=min(prev, close) - 0.1,
                          close=close))
        prev = close
    return out


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    init_db()
    with session_scope() as s:
        for table in (BtOutcome, BtEvent):
            s.execute(delete(table))
    reaction.clear_cache()
    yield
    reaction.clear_cache()


def fake_candles(monkeypatch, per_interval: dict[str, list[Candle]]):
    calls: list[tuple[str, str, str | None]] = []

    async def fake(symbol, interval="15m", range_=None):
        calls.append((symbol, interval, range_))
        return CandlesOut(symbol=symbol, interval=interval, source="test", candles=per_interval.get(interval, []))

    monkeypatch.setattr(candle_service, "get_candles", fake)
    return calls


def by_h(res):
    return {h["horizon"]: h for h in res["horizons"]}


async def test_up_label_and_excursions_from_5m(monkeypatch):
    # gold rises 1.0 per 5m bar from 1h before the release -> clearly "up" at every horizon
    calls = fake_candles(monkeypatch, {"5m": make_candles(300, TS0 - 3600, 12 + 12 * 26, per_bar=1.0)})
    now = T0 + timedelta(days=3)
    res = await reaction.get_reaction("xauusd", T0, db=None, now=now)
    assert res["status"] == "ok" and res["interval"] == "5m" and res["source"] == "live"
    assert calls[0][1] == "5m"
    h = by_h(res)
    assert [x["horizon"] for x in res["horizons"]] == ["5m", "15m", "1h", "4h", "1d"]
    assert all(x["label"] == "up" and x["status"] == "ok" for x in h.values())
    assert h["1h"]["return_pct"] > h["15m"]["return_pct"] > 0
    assert res["max_up_1h"] > 0 and res["max_down_1h"] <= 0 and res["max_status"] == "ok"


async def test_sideways_and_down_use_asset_class_thresholds(monkeypatch):
    flat = make_candles(300, TS0 - 3600, 12 + 12 * 26, per_bar=0.0)
    fake_candles(monkeypatch, {"5m": flat})
    res = await reaction.get_reaction("XAUUSD", T0, now=T0 + timedelta(days=3))
    assert {x["label"] for x in res["horizons"]} == {"sideways"}

    fake_candles(monkeypatch, {"5m": make_candles(300, TS0 - 3600, 12 + 12 * 26, per_bar=-1.0)})
    reaction.clear_cache()
    res = await reaction.get_reaction("XAUUSD", T0, now=T0 + timedelta(days=3))
    assert by_h(res)["1h"]["label"] == "down"


async def test_future_event_is_pending(monkeypatch):
    calls = fake_candles(monkeypatch, {})
    now = T0 - timedelta(hours=2)
    res = await reaction.get_reaction("EURUSD", T0, now=now)
    assert res["status"] == "pending" and not calls
    assert {x["status"] for x in res["horizons"]} == {"pending"}
    assert all(x["label"] is None and x["return_pct"] is None for x in res["horizons"])


async def test_unelapsed_horizons_are_pending(monkeypatch):
    fake_candles(monkeypatch, {"5m": make_candles(300, TS0 - 3600, 12 + 6, per_bar=1.0)})
    res = await reaction.get_reaction("XAUUSD", T0, now=T0 + timedelta(minutes=20))
    h = by_h(res)
    assert res["status"] == "ok"
    assert h["5m"]["status"] == "ok" and h["15m"]["status"] == "ok"
    assert h["1h"]["status"] == h["4h"]["status"] == h["1d"]["status"] == "pending"
    assert h["1h"]["return_pct"] is None and res["max_status"] == "pending"


async def test_falls_back_to_coarser_interval(monkeypatch):
    # 5m / 15m have no history that old; 1h does: 5m + 15m horizons are unavailable (finer than the candles)
    calls = fake_candles(monkeypatch, {"1h": make_candles(3600, TS0 - 5 * 3600, 5 + 24 * 3, per_bar=3.0)})
    res = await reaction.get_reaction("XAUUSD", T0, now=T0 + timedelta(days=3))
    assert [c[1] for c in calls] == ["5m", "15m", "1h"]
    assert res["interval"] == "1h"
    h = by_h(res)
    assert h["5m"]["status"] == "unavailable" and h["5m"]["label"] is None
    assert h["1h"]["label"] == "up"
    assert res["max_status"] == "unavailable"  # 1h candles cannot give first-hour excursions


async def test_no_data_and_unsupported(monkeypatch):
    fake_candles(monkeypatch, {})
    res = await reaction.get_reaction("XAUUSD", T0, now=T0 + timedelta(days=3))
    assert res["status"] == "no_data"
    assert by_h(res)["1h"]["status"] == "unavailable"
    res = await reaction.get_reaction("NOPE", T0, now=T0 + timedelta(days=3))
    assert res["status"] == "unsupported_symbol"


async def test_provider_error_is_reported_not_raised(monkeypatch):
    async def boom(*_a, **_k):
        raise candle_service.MarketDataError("down")

    monkeypatch.setattr(candle_service, "get_candles", boom)
    res = await reaction.get_reaction("XAUUSD", T0, now=T0 + timedelta(days=3))
    assert res["status"] == "error" and "down" in res["message"]


def store_outcome(**kw):
    with session_scope() as s:
        ev = BtEvent(dedupe_key="k1", time=T0, kind="calendar", source="t", title="NFP", impact="high")
        s.add(ev)
        s.flush()
        row = dict(event_id=ev.id, symbol="XAUUSD", interval="5m", price_at_release=2000.0, ret_5m=0.5, ret_15m=0.6,
                   ret_1h=-0.4, ret_4h=0.0, ret_1d=0.2, price_1h=1992.0, dir_5m="up", dir_15m="up", dir_1h="down",
                   dir_4h="flat", dir_1d="up", max_up_1h=0.7, max_down_1h=-0.5, complete=True)
        row.update(kw)
        s.add(BtOutcome(**row))


async def test_stored_backtest_outcome_wins_and_flat_is_sideways(monkeypatch):
    calls = fake_candles(monkeypatch, {})
    store_outcome()
    with session_scope() as s:
        res = await reaction.get_reaction("XAUUSD", T0, db=s, now=T0 + timedelta(days=3))
    assert not calls and res["source"] == "backtest" and res["status"] == "ok"
    h = by_h(res)
    assert h["1h"]["label"] == "down" and h["1h"]["return_pct"] == -0.4 and h["1h"]["price"] == 1992.0
    assert h["4h"]["label"] == "sideways"
    assert res["max_up_1h"] == 0.7 and res["max_down_1h"] == -0.5


async def test_incomplete_stored_outcome_is_recomputed_when_horizon_elapsed(monkeypatch):
    calls = fake_candles(monkeypatch, {"5m": make_candles(300, TS0 - 3600, 12 + 12 * 26, per_bar=1.0)})
    store_outcome(complete=False, ret_1d=None, dir_1d=None)
    with session_scope() as s:
        res = await reaction.get_reaction("XAUUSD", T0, db=s, now=T0 + timedelta(days=3))
    assert calls and res["source"] == "live"


async def test_results_are_cached(monkeypatch):
    calls = fake_candles(monkeypatch, {"5m": make_candles(300, TS0 - 3600, 12 + 12 * 26, per_bar=1.0)})
    # real clock: the event is long past, so the result is final and cached
    old = datetime.now(UTC) - timedelta(days=5)
    cands = make_candles(300, int(old.timestamp()) - 3600, 12 + 12 * 24 * 2, per_bar=1.0)
    calls = fake_candles(monkeypatch, {"5m": cands})
    a = await reaction.get_reaction("XAUUSD", old)
    n = len(calls)
    b = await reaction.get_reaction("XAUUSD", old, ["1h"])
    assert a["status"] == "ok" and len(calls) == n
    assert [x["horizon"] for x in b["horizons"]] == ["1h"]


def test_parse_horizons():
    assert reaction.parse_horizons("1d,5m") == ["5m", "1d"]
    assert reaction.parse_horizons(None) == ["5m", "15m", "1h", "4h", "1d"]
    with pytest.raises(ValueError):
        reaction.parse_horizons("2h")


def test_api_get_and_batch(monkeypatch):
    old = datetime.now(UTC) - timedelta(days=5)
    cands = make_candles(300, int(old.timestamp()) - 3600, 12 + 12 * 24 * 2, per_bar=1.0)
    fake_candles(monkeypatch, {"5m": cands})
    with TestClient(app) as client:
        iso = old.isoformat()
        r = client.get("/api/reaction", params={"symbol": "XAUUSD", "time": iso, "horizons": "5m,1h"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok" and [h["horizon"] for h in body["horizons"]] == ["5m", "1h"]
        assert client.get("/api/reaction", params={"symbol": "XAUUSD", "time": iso, "horizons": "9x"}).status_code == 422
        assert client.get("/api/reaction", params={"symbol": "NOPE", "time": iso}).status_code == 404

        future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        r = client.post("/api/reaction/batch", json=[
            {"id": "a", "symbol": "XAUUSD", "time": iso},
            {"id": "b", "symbol": "XAUUSD", "time": future},
            {"id": 3, "symbol": "NOPE", "time": iso},
        ])
        assert r.status_code == 200, r.text
        res = {x["id"]: x for x in r.json()["results"]}
        assert res["a"]["status"] == "ok" and res["b"]["status"] == "pending" and res[3]["status"] == "unsupported_symbol"

        big = [{"id": i, "symbol": "XAUUSD", "time": iso} for i in range(201)]
        assert client.post("/api/reaction/batch", json=big).status_code == 422
