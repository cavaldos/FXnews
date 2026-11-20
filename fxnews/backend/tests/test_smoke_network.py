"""Live smoke test (real internet). Skipped by default; run with: .venv/bin/pytest -m network"""

import pytest

from app.market import candles as market
from app.sources.rss import RssSource

pytestmark = pytest.mark.network


async def test_live_fxstreet_and_yahoo():
    try:
        items = await RssSource("fxstreet", "https://www.fxstreet.com/rss/news").fetch()
        assert items, "FXStreet returned no items"
        assert all(i.kind == "headline" and i.title for i in items)
        assert any(i.event_time and i.event_time.tzinfo for i in items)

        eur = await market.get_candles("EURUSD", "15m")
        assert len(eur.candles) > 10
        times = [c.time for c in eur.candles]
        assert times == sorted(set(times)), "times must be strictly ascending"

        gold = await market.get_candles("XAUUSD", "4h")
        assert len(gold.candles) > 10
        assert all(c.time % (4 * 3600) == 0 for c in gold.candles)

        quotes = await market.get_quotes(["EURUSD", "XAUUSD"])
        assert {q.symbol for q in quotes} == {"EURUSD", "XAUUSD"}
        assert all(q.price > 0 for q in quotes)
    finally:
        await market.aclose()
