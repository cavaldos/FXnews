from datetime import UTC, datetime

import pytest

from app.analysis import rules
from app.analysis.mapping import detect_currencies, explicit_symbols, map_news, symbol_sign
from app.sources.base import RawNews
from app.sources.forexfactory import parse_event


def headline(title: str, summary: str = "") -> RawNews:
    return RawNews(kind="headline", source="test", title=title, summary=summary, event_time=datetime.now(UTC))


@pytest.mark.parametrize(
    ("symbol", "currency", "sign"),
    [("EURUSD", "USD", -1), ("EURUSD", "EUR", 1), ("USDJPY", "USD", 1), ("XAUUSD", "USD", -1),
     ("USDCAD", "OIL", -1), ("AUDUSD", "CNY", 1), ("GBPJPY", "USD", 0)],
)
def test_symbol_sign(symbol, currency, sign):
    assert symbol_sign(symbol, currency) == sign


def test_explicit_pairs_and_currency_detection():
    assert explicit_symbols("EUR/USD Price Forecast: rebounds; XAU/USD flat") == ["EURUSD", "XAUUSD"]
    assert explicit_symbols("THE USD and ECB") == []
    cur = detect_currencies("Fed's Waller hawkish; ECB Lagarde cautious, gold slips")
    assert {"USD", "EUR", "XAU"} <= set(cur)


def test_map_calendar_event():
    item = parse_event({"title": "CPI m/m", "country": "USD", "date": "2026-11-10T08:30:00-05:00",
                        "impact": "High", "forecast": "0.3%", "previous": "0.6%"})
    assert item.event_time == datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    currencies, symbols = map_news(item)
    assert currencies == ["USD"]
    assert symbols[:3] == ["EURUSD", "USDJPY", "XAUUSD"]


def test_map_headline_round_robin():
    currencies, symbols = map_news(headline("British Pound turns lower as BoE holds; weak Yen"))
    assert currencies[:2] == ["GBP", "JPY"]
    assert symbols[:2] == ["GBPUSD", "USDJPY"]


def test_parse_economic_value():
    assert rules.parse_economic_value("256K") == 256_000
    assert rules.parse_economic_value("-0.2%") == -0.2
    assert rules.parse_economic_value("1,234.5B") == 1_234_500_000_000
    assert rules.parse_economic_value("") is None
    assert rules.parse_economic_value("n/a") is None


@pytest.mark.parametrize(
    ("title", "actual", "forecast", "expected"),
    [("CPI m/m", "0.5%", "0.3%", "stronger"), ("Non-Farm Employment Change", "100K", "180K", "weaker"),
     ("Unemployment Rate", "4.4%", "4.2%", "weaker"), ("Initial Jobless Claims", "200K", "230K", "stronger"),
     ("Some Speech", "1", "2", None), ("CPI m/m", "0.3%", "0.3%", None)],
)
def test_surprise_direction(title, actual, forecast, expected):
    assert rules.surprise_direction(title, actual, forecast) == expected


def test_calendar_rule_bias_respects_quote_currency():
    # Hot US CPI -> USD stronger -> EURUSD down
    assert rules.rule_bias(kind="calendar", title="CPI m/m", summary=None, currencies=["USD"],
                           symbols=["EURUSD"], actual="0.6%", forecast="0.3%") == "bearish"
    assert rules.rule_bias(kind="calendar", title="CPI m/m", summary=None, currencies=["USD"],
                           symbols=["USDJPY"], actual="0.6%", forecast="0.3%") == "bullish"
    # FF feed has no actual -> neutral
    assert rules.rule_bias(kind="calendar", title="CPI m/m", summary=None, currencies=["USD"],
                           symbols=["EURUSD"], actual=None, forecast="0.3%") == "neutral"


def test_headline_rule_bias():
    assert rules.rule_bias(kind="headline", title="EUR/USD rises toward 1.1200", summary=None,
                           currencies=["EUR", "USD"], symbols=["EURUSD"], explicit_pair=True) == "bullish"
    assert rules.rule_bias(kind="headline", title="Fed's Waller sounds hawkish, sees more hikes", summary=None,
                           currencies=["USD"], symbols=["EURUSD"]) == "bearish"
    assert rules.headline_impact("FOMC minutes show split") == "High"
    assert rules.headline_impact("Gold consolidates") == "Low"


def _bias(title: str) -> str:
    _currencies, symbols = map_news(headline(title))
    from app.analysis.mapping import detect_currencies as dc
    return rules.rule_bias(kind="headline", title=title, summary=None, currencies=dc(title), symbols=symbols)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # review #2 real/realistic cases
        ("Fed holds rates steady but ECB signals rate cut in December", "bearish"),  # EUR weaker -> EURUSD down
        ("Canadian Dollar declines despite higher oil prices", "bullish"),  # CAD weaker -> USDCAD up
        ("Dow Jones futures surge as easing geopolitical risks, AI earnings revive market sentiment", "bullish"),
        ("Gold rallies as easing yields boost bullion", "bullish"),
        ("British Pound turns lower as bulls shrug hawkish BoE; downside seems limited vs weak Yen", "bearish"),
        ("British Pound hangs near one-week low, eyes 1.3300 as Fed hike bets lift USD", "bearish"),
        ("EUR/USD rises toward 1.1200", "bullish"),
        ("Silver Price Forecast: XAG/USD surges to near $53 despite rising US bond Yields", "bullish"),
        ("Federal Reserve: December rate hike expectations reinforced – Commerzbank", "bearish"),
        ("Euro: Knot signals ECB readiness for new tools – Rabobank", "neutral"),
        # conflicting votes -> neutral instead of a guess
        ("USD rallies while EUR/USD rises", "neutral"),
    ],
)
def test_headline_bias_cases(title, expected):
    assert _bias(title) == expected


def test_indices_not_signed_by_quote_currency():
    assert symbol_sign("US500", "USD") == 0
    assert symbol_sign("US500", "SPX") == 1
    assert symbol_sign("DE40", "EUR") == 0


def test_fallback_trades_propagate_sign():
    ideas = rules.fallback_trades(["EURUSD", "USDJPY", "XAUUSD"], "USD", "bearish", "High")
    assert [(i.symbol, i.bias) for i in ideas] == [("EURUSD", "bearish"), ("USDJPY", "bullish"), ("XAUUSD", "bearish")]
    assert rules.fallback_trades(["EURUSD"], "USD", "neutral", "High") == []
