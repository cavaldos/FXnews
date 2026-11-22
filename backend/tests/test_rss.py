from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.sources.rss import RssFetchError, RssSource, build_rss_sources

FEED_URL = "https://feeds.example.com/rss/news"

LONG_TEXT = "word " * 400  # 2000 chars

SAMPLE_RSS = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Example FX News</title>
    <link>https://feeds.example.com/news</link>
    <description>demo</description>
    <item>
      <title><![CDATA[Euro: Knot signals ECB readiness for new tools – Rabobank]]></title>
      <link>https://feeds.example.com/news/euro-knot-1</link>
      <guid isPermaLink="false">guid-1</guid>
      <pubDate>Wed, 11 Nov 2026 08:32:31 GMT</pubDate>
      <description><![CDATA[<p>EUR/USD <b>rises</b> &amp; holds   above 1.11.</p><br/><img src="https://x/y.png" alt="chart"/>
      <a href="https://example.com">Read more</a>]]></description>
      <enclosure url="https://cdn.example.com/a.png" length="0" type="image/png"/>
    </item>
    <item>
      <title>Gold &amp; Silver slip   as USD firms</title>
      <link>https://feeds.example.com/news/gold-2</link>
      <guid isPermaLink="false">guid-2</guid>
      <pubDate>Wed, 11 Nov 2026 15:00:00 +0700</pubDate>
      <description>&lt;p&gt;XAU/USD &amp;lt; 4,000 after CPI&lt;/p&gt;</description>
    </item>
    <item>
      <title>   </title>
      <link>https://feeds.example.com/news/no-title-3</link>
      <pubDate>Wed, 11 Nov 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <link>https://feeds.example.com/news/no-title-4</link>
      <description>orphan description</description>
    </item>
    <item>
      <title>No pubDate and no description</title>
      <link>https://feeds.example.com/news/nodate-5</link>
    </item>
    <item>
      <title>Long summary</title>
      <link>https://feeds.example.com/news/long-6</link>
      <pubDate>Wed, 11 Nov 2026 07:00:00 GMT</pubDate>
      <description><![CDATA[<div>{LONG_TEXT}</div>]]></description>
    </item>
  </channel>
</rss>
"""


def _rss_response(body: str = SAMPLE_RSS, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body.encode("utf-8"), headers={"content-type": "application/rss+xml"})


@respx.mock
async def test_parse_sample_feed():
    route = respx.get(FEED_URL).mock(return_value=_rss_response())
    items = await RssSource("example", FEED_URL).fetch()

    # untitled items dropped
    assert [i.title for i in items] == [
        "Euro: Knot signals ECB readiness for new tools – Rabobank",
        "Gold & Silver slip as USD firms",
        "No pubDate and no description",
        "Long summary",
    ]
    assert all(i.kind == "headline" and i.source == "example" for i in items)
    assert route.calls.last.request.headers["user-agent"].startswith("Mozilla/5.0")

    euro, gold, nodate, long_ = items
    assert euro.url == "https://feeds.example.com/news/euro-knot-1"
    assert euro.event_time == datetime(2026, 11, 11, 8, 32, 31, tzinfo=timezone.utc)
    assert euro.event_time.utcoffset().total_seconds() == 0
    # HTML stripped (CDATA), entities decoded, whitespace collapsed, no tag residue
    assert euro.summary == "EUR/USD rises & holds above 1.11. Read more"
    assert euro.raw["guid"] == "guid-1"
    assert euro.raw["image"] == "https://cdn.example.com/a.png"

    # +0700 offset converted to UTC; escaped-HTML description handled
    assert gold.event_time == datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)
    assert gold.summary == "XAU/USD < 4,000 after CPI"

    # missing pubDate -> None, missing description -> None
    assert nodate.event_time is None
    assert nodate.summary is None

    assert len(long_.summary) <= 600
    assert long_.summary.endswith("…")
    assert "<" not in long_.summary

    # dedupe key must be stable & per-url
    assert len({i.dedupe_key for i in items}) == len(items)


@respx.mock
async def test_empty_but_valid_feed_returns_empty_list():
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
    respx.get(FEED_URL).mock(return_value=_rss_response(empty))
    assert await RssSource("example", FEED_URL).fetch() == []


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_http_error_raises(status):
    respx.get(FEED_URL).mock(return_value=httpx.Response(status, text="nope"))
    with pytest.raises(RssFetchError, match=f"HTTP {status}"):
        await RssSource("example", FEED_URL).fetch()


@respx.mock
async def test_timeout_raises():
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(RssFetchError, match="timed out"):
        await RssSource("example", FEED_URL).fetch()


@respx.mock
async def test_connection_error_raises():
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RssFetchError, match="failed"):
        await RssSource("example", FEED_URL).fetch()


@respx.mock
async def test_html_page_instead_of_feed_raises():
    # e.g. a Cloudflare challenge served with HTTP 200
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, text="<html><body><h1>Just a moment...</h1></body></html>")
    )
    with pytest.raises(RssFetchError, match="valid RSS"):
        await RssSource("example", FEED_URL).fetch()


@respx.mock
async def test_follows_redirects():
    respx.get("https://old.example.com/rss").mock(
        return_value=httpx.Response(301, headers={"location": FEED_URL})
    )
    respx.get(FEED_URL).mock(return_value=_rss_response())
    items = await RssSource("example", "https://old.example.com/rss").fetch()
    assert len(items) == 4


def test_build_rss_sources():
    settings = SimpleNamespace(
        rss_feeds={"fxstreet": "https://www.fxstreet.com/rss/news", "other": " https://x.example/rss ", "": "u", "bad": ""}
    )
    sources = build_rss_sources(settings)
    assert [(s.name, s.url) for s in sources] == [
        ("fxstreet", "https://www.fxstreet.com/rss/news"),
        ("other", "https://x.example/rss"),
    ]
    assert build_rss_sources(SimpleNamespace(rss_feeds={})) == []
