"""Ingest -> LLM (mocked OpenRouter) -> stored analysis/signals -> API."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.analysis import advisor, llm
from app.config import get_settings
from app.db import init_db
from app.main import app
from app.notify import discord
from app.schemas import AnalysisOut
from app.services import ingest
from app.sources.base import RawNews

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    init_db()

    async def no_prices(*_a, **_k):
        return None

    monkeypatch.setattr(advisor, "price_context", no_prices)
    monkeypatch.setattr(llm, "_RETRY_BASE_S", 0.0)
    yield


def llm_reply(payload: dict | str, model: str = "google/gemini-2.5-flash") -> httpx.Response:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": content}}],
                                     "usage": {"total_tokens": 10}})


def cpi_event(minutes_ahead: int = 20) -> RawNews:
    return RawNews(kind="calendar", source="forexfactory", title="CPI m/m", currency="USD", impact="High",
                   event_time=datetime.now(UTC) + timedelta(minutes=minutes_ahead), forecast="0.3%", previous="0.6%",
                   url="https://www.forexfactory.com/calendar")


def test_extract_json_handles_fences_and_preamble():
    assert llm.extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('note {"b": 2} end') == {"b": 2}
    with pytest.raises(llm.LLMError):
        llm.extract_json("no json here")


def test_coerce_filters_unknown_symbols_and_normalizes():
    out = advisor.coerce_llm_output({
        "summary": "x",
        "trades": [{"symbol": "EUR/USD", "bias": "SELL", "confidence": "72.6"},
                   {"symbol": "DOGEUSD", "bias": "buy", "confidence": 90},
                   {"symbol": "EURUSD", "bias": "buy", "confidence": 10},  # duplicate ignored
                   {"symbol": "USDJPY", "bias": "long", "confidence": 140}],
        "currency_impact": [{"currency": "usd", "direction": "stronger", "strength": 3}],
        "scenarios": [{"if": "actual > 0.3%", "then": "sell EURUSD"}],
    }, ["EURUSD", "USDJPY"])
    assert [(t["symbol"], t["bias"], t["confidence"]) for t in out["trades"]] == [
        ("EURUSD", "bearish", 73), ("USDJPY", "bullish", 100)]
    assert out["currency_impact"] == [{"currency": "USD", "direction": "stronger", "strength": 1.0}]
    assert out["scenarios"] == [{"condition": "actual > 0.3%", "outcome": "sell EURUSD"}]


@respx.mock
async def test_pre_event_cycle_analyzes_grouped_event_once():
    core = RawNews(**{**cpi_event().__dict__, "title": "Core CPI m/m", "forecast": "0.2%"})
    core.event_time = cpi_event().event_time.replace(second=0, microsecond=0)
    cpi = cpi_event()
    cpi.event_time = core.event_time
    new_ids, _ = ingest.store_items([cpi, core])
    assert len(new_ids) == 2

    route = respx.post(OPENROUTER).mock(return_value=llm_reply({
        "summary": "US CPI is coming up.",
        "trades": [{"symbol": "EURUSD", "bias": "neutral", "confidence": 30}],
        "scenarios": [{"condition": "Actual > 0.3%", "outcome": "USD strong: sell EURUSD"}],
        "risk_notes": "Spreads widen.",
    }))
    assert await ingest.run_pre_event_cycle() == 1
    await ingest.drain_background(timeout=10)
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert "Core CPI m/m" in sent["messages"][1]["content"] and "CPI m/m" in sent["messages"][1]["content"]
    assert sent["response_format"] == {"type": "json_object"}
    # second cycle must not re-alert
    assert await ingest.run_pre_event_cycle() == 0

    with TestClient(app) as client:
        cal = client.get("/api/calendar").json()
        ids = {e["analysis_id"] for e in cal if e["id"] in new_ids}
        assert len(ids) == 1 and None not in ids
        analysis = client.get(f"/api/analyses/{ids.pop()}").json()
        assert analysis["kind"] == "pre_event"
        assert analysis["scenarios"][0]["outcome"].startswith("USD strong")
        assert client.get("/api/signals", params={"symbol": "EURUSD"}).json()[0]["bias"] == "neutral"


@respx.mock
async def test_llm_failure_falls_back_to_rules():
    new_ids, _ = ingest.store_items([RawNews(kind="headline", source="t", title="Fed's Waller hawkish, sees more hikes",
                                             event_time=datetime.now(UTC), url="https://example.com/a")])
    respx.post(OPENROUTER).mock(return_value=httpx.Response(500, text="upstream down"))
    out = await advisor.analyze_news(new_ids[0])
    assert out.model == "rules"
    assert "500" in (out.error or "")
    assert out.trades and out.trades[0].symbol == "EURUSD" and out.trades[0].bias == "bearish"


def test_coerce_survives_schema_drift():
    out = advisor.coerce_llm_output({
        "summary": ["a", "b"],
        "trades": [
            {"symbol": "EURUSD", "bias": "sell", "confidence": 0.85, "entry_idea": ["sell 1.1150", "add 1.1170"],
             "invalidation": {"stop": 1.12}},
            {"symbol": "USDJPY", "bias": "buy", "confidence": "70%"},
            {"symbol": "XAUUSD", "bias": "buy", "confidence": float("inf")},
        ],
        "risk_notes": {"spread": "wide"},
    }, ["EURUSD", "USDJPY", "XAUUSD"])
    eur, jpy, xau = out["trades"]
    assert eur["confidence"] == 85 and eur["entry_idea"] == "sell 1.1150; add 1.1170" and "1.12" in eur["invalidation"]
    assert jpy["confidence"] == 70
    assert xau["confidence"] == 0
    assert out["summary"] == "a; b" and "spread" in out["risk_notes"]


@respx.mock
async def test_transient_openrouter_errors_are_retried():
    route = respx.post(OPENROUTER).mock(side_effect=[
        httpx.Response(503, text="overloaded"),
        httpx.ConnectTimeout("slow"),
        llm_reply({"summary": "ok", "trades": [{"symbol": "EURUSD", "bias": "bearish", "confidence": 70}]}),
    ])
    out = await advisor.advise("EURUSD")
    assert route.call_count == 3 and out.error is None and out.trades[0].bias == "bearish"


@respx.mock
async def test_failed_rerun_keeps_good_analysis_and_skips_discord(monkeypatch):
    monkeypatch.setattr(get_settings(), "discord_webhook_url", "https://discord.test/webhook")
    hook = respx.post("https://discord.test/webhook").mock(return_value=httpx.Response(204))
    new_ids, _ = ingest.store_items([RawNews(kind="headline", source="t", title="ECB signals rate cut in December",
                                             event_time=datetime.now(UTC), url="https://example.com/ecb")])
    respx.post(OPENROUTER).mock(side_effect=[
        llm_reply({"summary": "EUR weak", "trades": [{"symbol": "EURUSD", "bias": "bearish", "confidence": 75}]}),
        httpx.Response(500, text="down"), httpx.Response(500, text="down"), httpx.Response(500, text="down"),
    ])
    good = await advisor.analyze_news(new_ids[0])
    await advisor.drain_notifications()
    assert hook.call_count == 1  # confident headline idea -> Discord
    # same news again within the reuse window -> no new LLM call
    assert (await advisor.analyze_news(new_ids[0])).id == good.id
    bad = await advisor.analyze_news(new_ids[0], model="openai/gpt-4.1-mini")  # forced rerun, LLM down
    await advisor.drain_notifications()
    assert bad.error and bad.model == "rules"
    assert hook.call_count == 1  # fallback result is not pushed to Discord
    with TestClient(app) as client:
        assert client.get(f"/api/news/{new_ids[0]}").json()["analysis_id"] == good.id


def test_calendar_window_keeps_upcoming_events():
    now = datetime.now(UTC)
    past = [RawNews(kind="calendar", source="forexfactory", title=f"Past {i}", currency="USD", impact="High",
                    event_time=now - timedelta(hours=1 + i)) for i in range(12)]
    future = [RawNews(kind="calendar", source="forexfactory", title=f"Future {i}", currency="USD", impact="High",
                      event_time=now + timedelta(hours=1 + i)) for i in range(3)]
    ingest.store_items(past + future)
    from app.db import session_scope
    with session_scope() as session:
        lines = advisor.calendar_window(session, ["USD"], hours_back=24, hours_ahead=24, limit=10)
    assert len(lines) == 10
    assert sum("Future" in line for line in lines) == 3


@respx.mock
async def test_response_format_rejected_retries_without_it():
    route = respx.post(OPENROUTER).mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "response_format not supported"}}),
        llm_reply('{"summary": "ok", "trades": []}'),
    ])
    out = await advisor.advise("XAUUSD", question="Gold this week?")
    assert out.summary == "ok" and out.kind == "ad_hoc" and out.symbol == "XAUUSD"
    assert "response_format" not in json.loads(route.calls[1].request.content)


def test_model_selection_per_symbol(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "openrouter_symbol_models", {"XAUUSD": "anthropic/claude-sonnet-4.5"})
    assert s.model_for("XAUUSD") == "anthropic/claude-sonnet-4.5"
    assert s.model_for("EURUSD") == s.openrouter_model
    assert s.model_for("XAUUSD", "openai/gpt-4.1-mini") == "openai/gpt-4.1-mini"


@respx.mock
async def test_discord_embed_and_send(monkeypatch):
    monkeypatch.setattr(get_settings(), "discord_webhook_url", "https://discord.test/webhook")
    hook = respx.post("https://discord.test/webhook").mock(side_effect=[
        httpx.Response(429, json={"retry_after": 0.01}), httpx.Response(204)])
    analysis = AnalysisOut(id=1, kind="headline", model="m", summary="USD strengthens",
                           trades=[{"symbol": "EURUSD", "bias": "bearish", "confidence": 80, "entry_idea": "sell 1.1150"}],
                           created_at=datetime.now(UTC))
    assert await discord.send_analysis(analysis) is True
    embed = json.loads(hook.calls[1].request.content)["embeds"][0]
    assert embed["fields"][0]["name"] == "🔴 EURUSD — STRONG SELL (80%)"
    assert embed["color"] == discord.COLORS["bearish"]
