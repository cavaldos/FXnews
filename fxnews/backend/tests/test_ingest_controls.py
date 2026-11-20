"""Ingest cost controls and resilience: pre-event retry, headline cooldown, hourly budget, FF rate-limit
guard on manual runs, last_ingest_at honesty, title-only headline impact.

`advisor.analyze_news`, `map_news` and `rules.headline_impact` are stubbed so these tests do not depend on the
LLM prompt or on the keyword tables (which change independently)."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from app.analysis import advisor
from app.config import get_settings
from app.db import session_scope
from app.models import NewsItem
from app.schemas import AnalysisOut
from app.services import ingest
from app.sources.base import RawNews
from tests.support import wipe_db

FEED = "https://feeds.test/rss"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeAnalyses:
    """Stands in for advisor.analyze_news."""

    def __init__(self):
        self.calls: list[tuple[int, str | None]] = []
        self.error: str | None = None
        self.raises: Exception | None = None
        self.before_return = None

    async def __call__(self, news_id, *, kind=None, **_kw):
        self.calls.append((news_id, kind))
        if self.raises:
            raise self.raises
        if self.before_return:
            self.before_return(news_id)
        return AnalysisOut(id=len(self.calls), kind=kind or "headline", model="m", summary="s", error=self.error,
                           created_at=datetime.now(UTC))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    wipe_db()
    ingest.reset_cost_controls()
    ingest.state["last_ingest_at"] = None
    clock = Clock()
    monkeypatch.setattr(ingest, "_now_ts", clock)
    monkeypatch.setattr(ingest, "_analysis_slots", asyncio.Semaphore(2))  # bound to this test's loop
    settings = get_settings()
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "rss_feeds", {"t": FEED})
    monkeypatch.setattr(settings, "headline_cooldown_minutes", 10.0)
    monkeypatch.setattr(settings, "max_auto_llm_calls_per_hour", 20)
    monkeypatch.setattr(settings, "max_headline_analyses_per_cycle", 5)
    fake = FakeAnalyses()
    monkeypatch.setattr(advisor, "analyze_news", fake)

    # "[USD] ..." -> currency USD; titles containing HIGH are High impact.
    def fake_map(item, limit=6):
        cur = item.title[1:4] if item.title.startswith("[") else None
        return ([cur], [f"{cur}JPY"]) if cur else ([], [])

    monkeypatch.setattr(ingest, "map_news", fake_map)
    monkeypatch.setattr(ingest.rules, "headline_impact", lambda text: "High" if "HIGH" in text else "Low")
    yield type("Env", (), {"clock": clock, "analyses": fake, "settings": settings})
    wipe_db()
    ingest.reset_cost_controls()
    ingest._ff_marker().unlink(missing_ok=True)


def rss_xml(items):
    """items: (title, url, minutes_ago)"""
    now = datetime.now(UTC)
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{format_datetime(now - timedelta(minutes=m))}</pubDate></item>"
        for t, u, m in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'


def feed(items):
    return httpx.Response(200, content=rss_xml(items).encode(), headers={"content-type": "application/rss+xml"})


def calendar_event(title, currency="USD", minutes_ahead=20, when=None):
    return RawNews(kind="calendar", source="forexfactory", title=title, currency=currency, impact="High",
                   event_time=when or datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=minutes_ahead))


def news_row(news_id):
    with session_scope() as s:
        row = s.get(NewsItem, news_id)
        return row.pre_event_alerted, row.event_time


# ── #3 pre-event retry ───────────────────────────────────────────────


async def cycle():
    n = await ingest.run_pre_event_cycle()
    await ingest.drain_background(5)
    return n


async def test_failed_pre_event_analysis_is_retried_at_most_three_times(env):
    (nid,), _ = ingest.store_items([calendar_event("CPI m/m")])
    env.analyses.error = "OpenRouter HTTP 503"

    assert await cycle() == 1
    assert news_row(nid)[0] is False  # re-armed for the next 60s cycle
    assert await cycle() == 1
    assert news_row(nid)[0] is False
    assert await cycle() == 1  # third and last attempt
    assert news_row(nid)[0] is True  # gave up: stays marked
    assert await cycle() == 0
    assert [c[0] for c in env.analyses.calls] == [nid, nid, nid]
    assert all(kind == "pre_event" for _, kind in env.analyses.calls)


async def test_successful_pre_event_analysis_is_not_retried_and_forgets_attempts(env):
    (nid,), _ = ingest.store_items([calendar_event("CPI m/m")])
    assert await cycle() == 1
    assert news_row(nid)[0] is True
    assert await cycle() == 0
    assert len(env.analyses.calls) == 1 and nid not in ingest._pre_event_attempts


async def test_exception_in_analysis_counts_as_failure(env):
    (nid,), _ = ingest.store_items([calendar_event("CPI m/m")])
    env.analyses.raises = RuntimeError("boom")
    assert await cycle() == 1
    assert news_row(nid)[0] is False


async def test_recovery_on_a_retry_stops_further_attempts(env):
    (nid,), _ = ingest.store_items([calendar_event("CPI m/m")])
    env.analyses.error = "OpenRouter HTTP 503"
    await cycle()
    env.analyses.error = None  # OpenRouter is back
    assert await cycle() == 1
    assert await cycle() == 0
    assert len(env.analyses.calls) == 2 and news_row(nid)[0] is True


async def test_no_retry_once_the_event_has_started(env):
    (nid,), _ = ingest.store_items([calendar_event("CPI m/m")])
    env.analyses.error = "OpenRouter HTTP 503"

    def release_now(news_id):  # the release time passes while the (slow) analysis is running
        with session_scope() as s:
            s.get(NewsItem, news_id).event_time = datetime.now(UTC) - timedelta(seconds=5)

    env.analyses.before_return = release_now
    assert await cycle() == 1
    assert news_row(nid)[0] is True  # not re-armed: it is no longer a *pre*-event analysis


async def test_no_retry_without_an_api_key(env, monkeypatch):
    monkeypatch.setattr(env.settings, "openrouter_api_key", "")
    (nid,), _ = ingest.store_items([calendar_event("CPI m/m")])
    env.analyses.error = "OPENROUTER_API_KEY is not configured"
    assert await cycle() == 1
    assert news_row(nid)[0] is True and await cycle() == 0
    assert not ingest._auto_llm_calls  # no LLM call was made, so nothing is counted against the budget


async def test_only_the_group_leader_is_rearmed(env):
    when = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=20)
    ids, _ = ingest.store_items([calendar_event("CPI m/m", when=when), calendar_event("Core CPI m/m", when=when)])
    env.analyses.error = "OpenRouter HTTP 503"
    assert await cycle() == 1
    armed = {i: news_row(i)[0] for i in ids}
    assert sorted(armed.values()) == [False, True]  # exactly one row (the leader) can be picked up again
    leader = next(i for i, flag in armed.items() if flag is False)
    assert await cycle() == 1 and env.analyses.calls[-1][0] == leader


# ── #8 headline cooldown + hourly budget ─────────────────────────────


async def rss_cycle(items):
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(return_value=feed(items))
        result = await ingest.run_rss_cycle()
    await ingest.drain_background(5)
    return result


async def test_headline_cooldown_keeps_only_the_newest_headline_per_currency(env):
    result = await rss_cycle([
        ("[USD] HIGH Fed hikes", "https://x/1", 1),
        ("[USD] HIGH Fed speaker", "https://x/2", 2),
        ("[EUR] HIGH ECB cuts", "https://x/3", 3),
        ("[USD] HIGH Powell talks", "https://x/4", 4),
        ("[GBP] low chatter", "https://x/5", 5),  # not High
    ])
    assert result.inserted == 5 and result.analyzed == 2
    with session_scope() as s:
        titles = {s.get(NewsItem, i).title for i, kind in env.analyses.calls if kind == "headline"}
    assert titles == {"[USD] HIGH Fed hikes", "[EUR] HIGH ECB cuts"}  # newest USD + the only EUR

    env.clock.advance(5 * 60)  # inside the 10 minute cooldown
    assert (await rss_cycle([("[USD] HIGH new USD story", "https://x/6", 1)])).analyzed == 0
    assert (await rss_cycle([("[JPY] HIGH BoJ", "https://x/7", 1)])).analyzed == 1  # other currency is free

    env.clock.advance(6 * 60)  # 11 minutes after the first USD analysis
    assert (await rss_cycle([("[USD] HIGH later USD story", "https://x/8", 1)])).analyzed == 1
    assert len(env.analyses.calls) == 4


async def test_headline_cooldown_is_configurable(env, monkeypatch):
    monkeypatch.setattr(env.settings, "headline_cooldown_minutes", 0)
    result = await rss_cycle([(f"[USD] HIGH story {i}", f"https://x/{i}", i + 1) for i in range(3)])
    assert result.analyzed == 3


async def test_hourly_budget_stops_headline_analyses_and_recovers(env, monkeypatch):
    monkeypatch.setattr(env.settings, "max_auto_llm_calls_per_hour", 3)
    cur = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"]
    r1 = await rss_cycle([(f"[{c}] HIGH news {c}", f"https://x/a{c}", i + 1) for i, c in enumerate(cur[:6])])
    assert r1.analyzed == 3  # newest three; the budget is spent
    env.clock.advance(1800)
    assert (await rss_cycle([("[CAD] HIGH more", "https://x/b1", 1)])).analyzed == 0
    env.clock.advance(1801)  # > 1h since the first three
    assert (await rss_cycle([("[CHF] HIGH again", "https://x/b2", 1)])).analyzed == 1
    assert len(env.analyses.calls) == 4


async def test_zero_budget_disables_auto_headline_analysis(env, monkeypatch):
    monkeypatch.setattr(env.settings, "max_auto_llm_calls_per_hour", 0)
    assert (await rss_cycle([("[USD] HIGH Fed", "https://x/z", 1)])).analyzed == 0
    assert env.analyses.calls == []


async def test_pre_event_counts_against_the_budget_but_is_never_blocked(env, monkeypatch):
    monkeypatch.setattr(env.settings, "max_auto_llm_calls_per_hour", 1)
    ingest.store_items([calendar_event("CPI m/m", "USD"), calendar_event("Rate decision", "EUR")])
    assert await cycle() == 2  # both run although the budget is 1
    assert len(ingest._auto_llm_calls) == 2
    assert (await rss_cycle([("[JPY] HIGH BoJ", "https://x/q", 1)])).analyzed == 0  # ...and they used it up


async def test_per_cycle_cap_still_applies(env, monkeypatch):
    monkeypatch.setattr(env.settings, "max_headline_analyses_per_cycle", 2)
    cur = ["USD", "EUR", "GBP", "JPY"]
    assert (await rss_cycle([(f"[{c}] HIGH {c}", f"https://x/{c}", i + 1) for i, c in enumerate(cur)])).analyzed == 2


# ── title-only impact ────────────────────────────────────────────────


def test_headline_impact_is_judged_on_the_title_only(monkeypatch):
    seen = []
    monkeypatch.setattr(ingest.rules, "headline_impact", lambda text: seen.append(text) or "Low")
    ingest.store_items([RawNews(kind="headline", source="s", title="Gold consolidates", url="https://x/t",
                                summary="Fed rate hike inflation war tariffs cpi", event_time=datetime.now(UTC))])
    assert seen == ["Gold consolidates"]


# ── #6 manual run vs the FF rate limit ───────────────────────────────


@pytest.fixture
def ff(monkeypatch):
    fetched = {"n": 0}

    async def fetch():
        fetched["n"] += 1
        return []

    async def no_rss():
        return ingest.IngestResult(inserted=0, analyzed=0)

    async def no_pre():
        return 0

    monkeypatch.setattr(ingest._ff_source, "fetch", fetch)
    monkeypatch.setattr(ingest, "run_rss_cycle", no_rss)
    monkeypatch.setattr(ingest, "run_pre_event_cycle", no_pre)
    ingest._ff_marker().unlink(missing_ok=True)
    return fetched


async def test_manual_run_skips_ff_while_it_is_not_due(ff):
    first = await ingest.run_all()  # no marker yet: due
    assert ff["n"] == 1 and first.errors == []
    again = await ingest.run_all()  # marker was just touched
    assert ff["n"] == 1
    assert again.errors == [] and len(again.notes) == 1
    assert not any(e.startswith("forexfactory") for e in again.errors)
    assert again.notes and "rate limited" in again.notes[0]
    assert "refresh in" in again.notes[0]  # includes the seconds left


async def test_force_overrides_the_guard(ff):
    await ingest.run_all()
    forced = await ingest.run_all(force=True)
    assert ff["n"] == 2 and forced.errors == []


async def test_ff_is_fetched_again_once_the_interval_has_passed(ff):
    await ingest.run_all()
    old = datetime.now().timestamp() - (get_settings().ff_poll_minutes * 60 + 5)
    os.utime(ingest._ff_marker(), (old, old))
    assert (await ingest.run_all()).errors == [] and ff["n"] == 2


def test_api_passes_force_through(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    seen = []

    async def run_all(force=False):
        seen.append(force)
        return ingest.IngestResult(inserted=0, analyzed=0)

    monkeypatch.setattr(ingest, "run_all", run_all)
    c = TestClient(app)
    assert c.post("/api/ingest/run").status_code == 200
    assert c.post("/api/ingest/run?force=true").status_code == 200
    assert c.post("/api/ingest/run?force=false").status_code == 200
    assert seen == [False, True, False]


# ── #11 last_ingest_at ───────────────────────────────────────────────


async def test_last_ingest_at_stays_empty_when_every_feed_fails(env):
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(return_value=httpx.Response(500))
        result = await ingest.run_rss_cycle()
    assert len(result.errors) == 1 and ingest.state["last_ingest_at"] is None


async def test_last_ingest_at_is_set_when_at_least_one_feed_works(env, monkeypatch):
    monkeypatch.setattr(env.settings, "rss_feeds", {"ok": FEED, "bad": "https://feeds.test/broken"})
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).mock(return_value=feed([("[USD] low", "https://x/ok", 1)]))
        router.get("https://feeds.test/broken").mock(return_value=httpx.Response(500))
        result = await ingest.run_rss_cycle()
    assert result.inserted == 1 and len(result.errors) == 1
    assert isinstance(ingest.state["last_ingest_at"], datetime)


async def test_last_ingest_at_stays_empty_with_no_feeds_configured(env, monkeypatch):
    monkeypatch.setattr(env.settings, "rss_feeds", {})
    await ingest.run_rss_cycle()
    assert ingest.state["last_ingest_at"] is None
