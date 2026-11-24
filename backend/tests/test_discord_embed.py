"""Discord rejects a message whose embed totals more than 6000 characters (HTTP 400): the alert would be lost."""

import copy
import json
import random
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.config import get_settings
from app.notify import discord
from app.schemas import AnalysisOut, NewsOut, Scenario, TradeIdea

LIMIT = discord.EMBED_MAX_CHARS


def utf16(embed):
    """Independent re-implementation of Discord's counting (title + description + footer + fields)."""
    parts = [embed.get("title", ""), embed.get("description", ""), embed.get("footer", {}).get("text", "")]
    for f in embed.get("fields", []):
        parts += [f["name"], f["value"]]
    return sum(len(p.encode("utf-16-le")) // 2 for p in parts)


def huge_analysis(unit="x"):
    trades = [TradeIdea(symbol=s, bias=b, confidence=70 + i, horizon="intraday", entry_idea=unit * 900,
                        invalidation=unit * 900, targets=unit * 900)
              for i, (s, b) in enumerate([("EURUSD", "bearish"), ("USDJPY", "bullish"), ("XAUUSD", "bearish"),
                                          ("GBPUSD", "bearish"), ("AUDUSD", "bearish"), ("USDCAD", "bullish")])]
    scenarios = [Scenario(condition=unit * 200, outcome=unit * 1000) for _ in range(4)]
    return AnalysisOut(id=1, kind="pre_event", model="google/gemini-2.5-flash", summary=unit * 3000, trades=trades,
                       scenarios=scenarios, risk_notes=unit * 1500, created_at=datetime.now(UTC))


def news():
    return NewsOut(id=1, kind="calendar", source="forexfactory", title="Core CPI m/m " + "y" * 600, currency="USD",
                   impact="High", url="https://www.forexfactory.com/calendar", forecast="0.3%", previous="0.2%",
                   event_time=datetime.now(UTC), fetched_at=datetime.now(UTC))


@pytest.mark.parametrize("unit", ["x", "é", "😀", "漢"])
def test_oversized_analysis_is_cut_to_the_limit(unit):
    embed = discord.build_embed(huge_analysis(unit), news(), extra_titles=["Another release " + unit * 300])
    assert utf16(embed) <= LIMIT
    assert discord.embed_size(embed) == utf16(embed)
    assert len(json.dumps(embed)) < 100_000  # still serialisable


def test_the_important_parts_survive_the_cut():
    embed = discord.build_embed(huge_analysis(), news())
    assert embed["title"].startswith("🔥 Upcoming release: [USD] Core CPI m/m")
    assert embed["description"].startswith("xxxx") and len(embed["description"]) >= 80
    assert embed["fields"][0]["name"].startswith("🔴 EURUSD — ") and "(70%)" in embed["fields"][0]["name"]
    assert embed["footer"]["text"].startswith("FXNews · google/gemini-2.5-flash")
    assert embed["color"] == discord.COLORS["pre_event"] and embed["url"] == "https://www.forexfactory.com/calendar"
    # cut text is marked, not silently truncated
    assert any(f["value"].endswith("…") for f in embed["fields"]) or embed["description"].endswith("…")


def test_small_embeds_are_left_untouched():
    analysis = AnalysisOut(id=1, kind="headline", model="m", summary="USD strengthens", created_at=datetime.now(UTC),
                           trades=[TradeIdea(symbol="EURUSD", bias="bearish", confidence=80, entry_idea="sell 1.1150")])
    embed = discord.build_embed(analysis)
    assert discord.embed_size(embed) < LIMIT
    assert discord.fit_embed(copy.deepcopy(embed)) == embed
    assert embed["fields"][0]["name"] == "🔴 EURUSD — STRONG SELL (80%)"
    assert embed["fields"][0]["value"] == "Entry: sell 1.1150"


def test_fit_embed_respects_the_limit_for_random_embeds():
    rng = random.Random(7)
    alphabet = "abc éü😀漢 \n"
    for _ in range(400):
        def text(n):
            return "".join(rng.choice(alphabet) for _ in range(n))

        embed = {
            "title": text(rng.randint(0, 256)),
            "description": text(rng.randint(0, 4000)),
            "footer": {"text": text(rng.randint(0, 2048))},
            "fields": [{"name": text(rng.randint(1, 256)), "value": text(rng.randint(1, 1024))}
                       for _ in range(rng.randint(0, 25))],
        }
        before = copy.deepcopy(embed)
        out = discord.fit_embed(embed)
        assert utf16(out) <= LIMIT
        assert out["title"] == before["title"] and out["footer"] == before["footer"]
        if utf16(before) <= LIMIT:
            assert out == before


def test_fit_embed_worst_case_everything_at_its_maximum():
    embed = {
        "title": "t" * 256, "description": "d" * 4096, "footer": {"text": "f" * 2048},
        "fields": [{"name": "n" * 256, "value": "v" * 1024} for _ in range(25)],
    }
    assert utf16(embed) > 3 * LIMIT
    assert utf16(discord.fit_embed(embed)) <= LIMIT


@respx.mock
async def test_the_payload_actually_sent_is_within_the_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "discord_webhook_url", "https://discord.test/hook")
    hook = respx.post("https://discord.test/hook").mock(return_value=httpx.Response(204))
    assert await discord.send_analysis(huge_analysis("😀"), news(), url="https://discord.test/hook") is True
    sent = json.loads(hook.calls[0].request.content)["embeds"][0]
    assert utf16(sent) <= LIMIT
