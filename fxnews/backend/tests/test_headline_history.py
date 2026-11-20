"""app/backtest/headline_history.py: historical headline sources, all HTTP mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.backtest import headline_history as hh
from app.backtest.sources import EventRecord
from app.config import Settings

START = datetime(2026, 1, 12, tzinfo=UTC)
END = datetime(2026, 1, 19, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(hh, "BACKOFF_SECONDS", 0.0)


def cfg(**over) -> Settings:
    base = dict(headline_history_investinglive=False, headline_history_fxstreet_wayback=False, headline_history_cnbc=False,
                headline_history_gdelt=False, headline_history_marketaux_api_key="", headline_history_finnhub_api_key="",
                headline_history_gdelt_lookback_days=0, headline_history_request_delay_seconds=0.0, headline_history_gdelt_min_interval_seconds=0.0)
    base.update(over)
    return Settings(**base)


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def sitemap(*rows: tuple[str, str]) -> str:
    body = "".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in rows)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def sitemap_index(*locs: str) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'


def test_title_from_slug():
    assert hh.title_from_slug("eur-usd-slips-after-us-cpi-20260111") == "EUR USD slips after US CPI"
    assert hh.title_from_slug("eurusd-price-forecast-bounces-202611200633") == "EUR/USD price forecast bounces"
    assert hh.title_from_slug("fed-holds-rates") == "FED holds rates"


# ── investinglive ──

IL_INDEX = sitemap_index("https://investinglive.com/sitemaps/news/articles/2026-W03.xml",
                         "https://investinglive.com/sitemaps/news/articles/2026-W20.xml")
IL_WEEK = sitemap(
    ("https://investinglive.com/central-banks/ecb-lagarde-says-rate-cut-not-needed-20260113/", "2026-01-13T09:30:15+00:00"),
    ("https://investinglive.com/forex/gold-rallies-as-usd-slides-20260114/", "2026-01-14T14:00:00+00:00"),
    ("https://investinglive.com/news/weekend-recipes-and-pasta-notes-20260115/", "2026-01-15T10:00:00+00:00"),
    ("https://investinglive.com/news/too-early-20260110/", "2026-01-11T10:00:00+00:00"),  # before START
    ("https://investinglive.com/es/noticias/algo-en-espanol-20260113/", "2026-01-13T10:00:00+00:00"),
    ("https://investinglive.com/forex/", "2026-01-13T10:00:00+00:00"),  # section page
)


async def test_investinglive_weekly_sitemap():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path.endswith("articles-sitemap-index.xml"):
            return httpx.Response(200, text=IL_INDEX)
        if req.url.path.endswith("2026-W03.xml"):
            return httpx.Response(200, text=IL_WEEK)
        return httpx.Response(404)

    errors, stats = [], {}
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, END, client=c, settings=cfg(headline_history_investinglive=True),
                                              errors=errors, stats=stats)
    assert not any("W20" in u for u in calls), "weeks outside the range must not be downloaded"
    assert [d["title"] for d in out] == ["ECB lagarde says rate cut not needed", "Gold rallies as USD slides"]
    first = out[0]
    assert first["time"] == datetime(2026, 1, 13, 9, 30, 15, tzinfo=UTC)
    assert first["kind"] == "headline" and first["source"] == "investinglive"
    assert first["impact"] == "high" and first["symbols"] and first["currency"] == "EUR"
    assert first["url"].startswith("https://investinglive.com/central-banks/")
    assert stats == {"investinglive": 2} and errors == []
    EventRecord(**first)  # same shape as the other backtest events


async def test_relevant_only_toggle():
    def handler(req):
        if req.url.path.endswith("articles-sitemap-index.xml"):
            return httpx.Response(200, text=IL_INDEX)
        return httpx.Response(200, text=IL_WEEK)

    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(
            START, END, client=c, settings=cfg(headline_history_investinglive=True, headline_history_relevant_only=False))
    assert "Weekend recipes and pasta notes" in [d["title"] for d in out]


async def test_investinglive_articles_without_date_suffix_are_kept():
    # investinglive stopped appending -YYYYMMDD to article slugs in July 2026: those articles must not be dropped
    week = sitemap(
        ("https://investinglive.com/news/gold-rallies-as-usd-slides-20260114/", "2026-01-14T14:00:00+00:00"),
        ("https://investinglive.com/commodities/analysts-split-on-whether-hormuz-flare-up-threatens-the-truce/",
         "2026-01-14T15:00:00+00:00"),
        ("https://investinglive.com/stocks/microsoft-quits-data-centre-deal-ahead-of-planned-ipo/",
         "2026-01-14T16:00:00+00:00"),
        ("https://investinglive.com/forex/", "2026-01-14T17:00:00+00:00"),  # section page: still not an article
        ("https://investinglive.com/tag/gold/", "2026-01-14T18:00:00+00:00"),  # tag page: still not an article
    )

    def handler(req):
        if req.url.path.endswith("articles-sitemap-index.xml"):
            return httpx.Response(200, text=IL_INDEX)
        return httpx.Response(200, text=week)

    errors = []
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, END, client=c, errors=errors,
                                              settings=cfg(headline_history_investinglive=True, headline_history_relevant_only=False))
    assert sorted(d["url"] for d in out) == [
        "https://investinglive.com/commodities/analysts-split-on-whether-hormuz-flare-up-threatens-the-truce/",
        "https://investinglive.com/news/gold-rallies-as-usd-slides-20260114/",
        "https://investinglive.com/stocks/microsoft-quits-data-centre-deal-ahead-of-planned-ipo/",
    ]
    assert errors == []


# ── FXStreet via Wayback CDX ──

async def test_fxstreet_wayback_uses_publish_time_from_url():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["params"] = str(req.url)
        rows = [["original"],
                ["https://www.fxstreet.com/news/eur-usd-gains-as-ecb-signals-pause-202601140830"],
                ["http://www.fxstreet.com/news/gold-hits-record-on-fed-bets-202601151215"],
                ["https://www.fxstreet.com/news/old-article-202601050830"],
                ["https://www.fxstreet.com/news/no-timestamp-here"]]
        return httpx.Response(200, text=json.dumps(rows))

    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, END, client=c, settings=cfg(headline_history_fxstreet_wayback=True))
    assert "web.archive.org" in seen["params"] and "collapse=urlkey" in seen["params"]
    assert [(d["time"], d["title"]) for d in out] == [
        (datetime(2026, 1, 14, 8, 30, tzinfo=UTC), "EUR USD gains as ECB signals pause"),
        (datetime(2026, 1, 15, 12, 15, tzinfo=UTC), "Gold hits record on FED bets")]
    assert out[1]["url"].startswith("https://")
    assert all(d["source"] == "fxstreet" for d in out)


# ── CNBC ──

def cnbc_page(published: str, title: str) -> str:
    return (f'<html><head><meta property="og:title" content="{title}"/>'
            f'<meta property="article:published_time" content="{published}"/></head><body>x</body></html>')


async def test_cnbc_sitemap_and_page_head_timestamp():
    pages = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/sitemapAll.xml":
            return httpx.Response(200, text=sitemap_index("https://www.cnbc.com/CNBCsitemapAll12.xml",
                                                          "https://www.cnbc.com/CNBCsitemapAll13.xml"))
        if path == "/CNBCsitemapAll13.xml":
            return httpx.Response(200, text=sitemap(
                ("https://www.cnbc.com/2026/01/14/dollar-falls-as-fed-rate-cut-bets-grow.html", "2026-01-20T10:00:00Z"),
                ("https://www.cnbc.com/2026/01/14/best-pasta-recipes.html", "2026-01-20T10:00:00Z"),
                ("https://www.cnbc.com/2026/03/02/outside-the-range-gold.html", "2026-03-02T10:00:00Z"),
                ("https://www.cnbc.com/video/2026/01/14/some-video.html", "2026-01-15T10:00:00Z")))
        if path == "/CNBCsitemapAll12.xml":
            return httpx.Response(200, text=sitemap(("https://www.cnbc.com/2020/01/14/ancient-fed-story.html", "2020-01-15T10:00:00Z")))
        if path.endswith("dollar-falls-as-fed-rate-cut-bets-grow.html"):
            pages.append(path)
            return httpx.Response(200, text=cnbc_page("2026-01-14T17:26:56+0000", "Dollar falls as Fed rate cut bets grow"))
        return httpx.Response(404)

    errors = []
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, END, client=c, settings=cfg(headline_history_cnbc=True), errors=errors)
    assert len(out) == 1 and pages == ["/2026/01/14/dollar-falls-as-fed-rate-cut-bets-grow.html"]
    assert out[0]["time"] == datetime(2026, 1, 14, 17, 26, 56, tzinfo=UTC)
    assert out[0]["title"] == "Dollar falls as Fed rate cut bets grow" and out[0]["source"] == "cnbc"
    assert out[0]["impact"] == "high" and "EURUSD" in out[0]["symbols"]
    assert errors == []


async def test_cnbc_page_fetch_cap_is_reported():
    def handler(req):
        if req.url.path == "/sitemapAll.xml":
            return httpx.Response(200, text=sitemap_index("https://www.cnbc.com/CNBCsitemapAll13.xml"))
        if req.url.path == "/CNBCsitemapAll13.xml":
            return httpx.Response(200, text=sitemap(*[(f"https://www.cnbc.com/2026/01/1{d}/fed-rate-story-{d}.html", "2026-01-20T10:00:00Z")
                                                      for d in (3, 4, 5)]))
        return httpx.Response(200, text=cnbc_page("2026-01-13T10:00:00+0000", "Fed story"))

    errors = []
    async with client_for(handler) as c:
        await hh.fetch_headline_history(START, END, client=c, errors=errors,
                                        settings=cfg(headline_history_cnbc=True, headline_history_cnbc_max_page_fetches=2))
    assert any("page-fetch cap 2" in e for e in errors)


# ── GDELT ──

GDELT_JSON = {"articles": [
    {"url": "https://a.example/1", "title": "Gold climbs as dollar slides before Fed minutes", "seendate": "20260114T081500Z",
     "domain": "a.example", "sourcecountry": "US"},
    {"url": "https://b.example/2", "title": "Gold climbs as dollar slides before FED minutes!", "seendate": "20260114T091500Z",
     "domain": "b.example", "sourcecountry": "UK"},  # same headline from a second outlet
    {"url": "https://c.example/3", "title": "BOJ holds rates", "seendate": "20260115T031500Z", "domain": "c.example"},
    {"url": "https://c.example/4", "title": "Out of range", "seendate": "20251201T031500Z", "domain": "c.example"},
]}


async def test_gdelt_artlist_chunks_dedupe_and_bad_body():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(dict(req.url.params))
        if req.url.params["startdatetime"].startswith("20260112"):
            return httpx.Response(200, text="Timespan is too short.")  # GDELT answers plain text on bad requests
        return httpx.Response(200, json=GDELT_JSON)

    errors = []
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, datetime(2026, 1, 15, tzinfo=UTC), client=c, errors=errors,
                                              settings=cfg(headline_history_gdelt=True))
    assert [p["startdatetime"] for p in calls] == ["20260112000000", "20260113000000", "20260114000000"]
    assert calls[0]["mode"] == "artlist" and calls[0]["maxrecords"] == "250"
    titles = [d["title"] for d in out]
    assert titles.count("Gold climbs as dollar slides before Fed minutes") == 1  # syndicated duplicate dropped
    assert "Out of range" not in titles
    assert any("gdelt 2026-01-12" in e for e in errors)  # the bad chunk is reported, the rest still processed
    assert out[0]["raw"]["domain"] == "a.example"


async def test_blocked_source_does_not_fail_others_and_aborts_early():
    gdelt_calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.gdeltproject.org":
            gdelt_calls.append(1)
            return httpx.Response(429, text="Please limit requests to one every 5 seconds")
        if req.url.path.endswith("articles-sitemap-index.xml"):
            return httpx.Response(200, text=IL_INDEX)
        return httpx.Response(200, text=IL_WEEK)

    errors, stats = [], {}
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, END, client=c, errors=errors, stats=stats,
                                              settings=cfg(headline_history_gdelt=True, headline_history_investinglive=True))
    assert stats["investinglive"] == 2 and stats["gdelt"] == 0 and len(out) == 2
    assert any("gdelt: aborted" in e for e in errors)
    assert len(gdelt_calls) == hh.ABORT_AFTER_FAILURES * (hh.RETRIES + 1)  # stops hammering a blocked host


async def test_source_exception_is_contained():
    def handler(req):
        if req.url.host == "investinglive.com":
            raise RuntimeError("boom")
        return httpx.Response(200, json=GDELT_JSON)

    errors = []
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, datetime(2026, 1, 16, tzinfo=UTC), client=c, errors=errors,
                                              settings=cfg(headline_history_gdelt=True, headline_history_investinglive=True))
    assert any(e.startswith("investinglive:") for e in errors) and out


# ── optional keyed sources ──

async def test_keyed_sources_skipped_without_key():
    def handler(req):
        raise AssertionError(f"unexpected request {req.url}")

    async with client_for(handler) as c:
        assert await hh.fetch_headline_history(START, END, client=c, settings=cfg()) == []


async def test_marketaux_and_finnhub_with_keys_and_no_key_in_errors():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.marketaux.com":
            if req.url.params["published_after"].startswith("2026-01-13"):
                return httpx.Response(500, text="oops")
            return httpx.Response(200, json={"meta": {"found": 1, "returned": 1, "limit": 3, "page": 1}, "data": [
                {"title": "ECB signals rate cut in March", "description": "d", "url": "https://m.example/1",
                 "published_at": "2026-01-12T10:00:00.000000Z", "source": "m.example"}]})
        if req.url.host == "finnhub.io":
            return httpx.Response(200, json=[{"headline": "USD/JPY jumps as BOJ hints at hike", "datetime": 1768300000,
                                              "url": "https://f.example/1", "source": "F", "summary": "s"}])
        return httpx.Response(404)

    errors, stats = [], {}
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(
            START, datetime(2026, 1, 14, tzinfo=UTC), client=c, errors=errors, stats=stats,
            settings=cfg(headline_history_marketaux_api_key="SECRETKEY", headline_history_finnhub_api_key="SECRETKEY"))
    assert stats == {"marketaux": 1, "finnhub": 1}
    assert {d["source"] for d in out} == {"marketaux", "finnhub"}
    assert any(e.startswith("marketaux 2026-01-13") for e in errors)
    assert not any("SECRETKEY" in e for e in errors)


# ── caps / args ──

async def test_total_cap_keeps_highest_impact_and_chronological_order():
    rows = [(f"https://investinglive.com/news/story-{i}-fed-rate-cut-20260113/", f"2026-01-13T0{i}:00:00+00:00") for i in range(5)]
    rows.append(("https://investinglive.com/news/story-pmi-eur-flash-20260113/", "2026-01-13T09:00:00+00:00"))  # medium

    def handler(req):
        if req.url.path.endswith("articles-sitemap-index.xml"):
            return httpx.Response(200, text=IL_INDEX)
        return httpx.Response(200, text=sitemap(*rows))

    errors = []
    async with client_for(handler) as c:
        out = await hh.fetch_headline_history(START, END, client=c, errors=errors,
                                              settings=cfg(headline_history_investinglive=True, headline_history_max_total=3))
    assert len(out) == 3 and [d["time"] for d in out] == sorted(d["time"] for d in out)
    assert all(d["impact"] == "high" for d in out) and any("cap 3" in e for e in errors)


async def test_disabled_and_empty_range():
    def handler(req):
        raise AssertionError("no request expected")

    async with client_for(handler) as c:
        assert await hh.fetch_headline_history(START, END, client=c, settings=cfg(headline_history_enabled=False,
                                                                                   headline_history_gdelt=True)) == []
        assert await hh.fetch_headline_history(END, START, client=c, settings=cfg(headline_history_gdelt=True)) == []


async def test_gdelt_skips_windows_older_than_lookback():
    calls = []

    def handler(req):
        calls.append(dict(req.url.params))
        return httpx.Response(200, json={"articles": []})

    errors = []
    async with client_for(handler) as c:
        await hh.fetch_headline_history(START, END, client=c, errors=errors,
                                        settings=cfg(headline_history_gdelt=True, headline_history_gdelt_lookback_days=30))
    assert calls == [] and any("gdelt: skipped" in e for e in errors)
