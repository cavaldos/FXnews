"""Historical forex / macro headlines for the backtest.

The live RSS feeds (FXStreet, CNBC) only expose their latest items, so past headlines are rebuilt from public
archives. Every source is optional (``HEADLINE_HISTORY_*`` flags in ``app/config.py``), is paced, retried on
429/5xx and isolated: one broken source adds an entry to ``errors`` and the others still contribute.

Sources (all respect robots.txt: only sitemaps listed there, article pages that are not disallowed, public APIs)
    investinglive  weekly article sitemaps (``/sitemaps/news/articles/2026-Wnn.xml``): URL + ``lastmod`` (== publish
                   time, to the second); the title is the URL slug (or ``og:title`` when enabled). ForexLive successor.
    fxstreet       FXStreet article URLs end in ``-YYYYMMDDHHMM`` (publish minute, UTC); the URLs are listed by the
                   Internet Archive CDX index (FXStreet's own sitemap only holds the last ~2 days).
    cnbc           ``sitemapAll`` lists article URLs (``/2026/01/08/<slug>.html``) with only a date, so finance-relevant
                   titles get one page-head request for ``article:published_time``.
    gdelt          GDELT DOC 2.0 ``artlist`` per day chunk (title, url, first-seen time, domain).
    marketaux      optional, needs ``HEADLINE_HISTORY_MARKETAUX_API_KEY`` (date range supported).
    finnhub        optional, needs ``HEADLINE_HISTORY_FINNHUB_API_KEY`` (forex/general feed, recent items only).

``fetch_headline_history`` returns dicts shaped like ``app.backtest.sources.EventRecord`` (kind="headline", time UTC,
impact / symbols / currency / dedupe_key from the same ``from_raw_news`` code as the live pipeline), so the caller can
do ``EventRecord(**d)``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import httpx

from app.backtest import sources
from app.config import Settings, get_settings
from app.market.symbols import SYMBOLS
from app.sources.base import RawNews

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; FXNewsBacktest/0.1; research, low-rate)"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)
RETRIES = 2
BACKOFF_SECONDS = 4.0  # first retry wait; doubles. Tests set this to 0.
MAX_BACKOFF_SECONDS = 60.0
ABORT_AFTER_FAILURES = 4  # consecutive failed chunks -> give up on that source (it is blocked / down)
PAGE_HEAD_BYTES = 1_200_000  # CNBC's <head> is ~700 KB (inline JSON); ~120 KB on the wire (gzip)

INVESTINGLIVE = "https://investinglive.com"
CNBC_INDEX = "https://www.cnbc.com/sitemapAll.xml"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY = ('(forex OR "central bank" OR "interest rates" OR inflation OR "Federal Reserve" OR ECB OR '
               '"Bank of Japan" OR gold OR "crude oil" OR tariffs OR "currency") sourcelang:english')
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
FINNHUB_URL = "https://finnhub.io/api/v1/news"

_GENERAL_SOURCES = {"gdelt", "marketaux", "finnhub"}
_IMPACT_RANK = {"high": 2, "medium": 1, "low": 0}


class SourceBlocked(RuntimeError):
    """The host keeps answering 429/5xx or refuses us: stop hitting it."""


# ───────────────────────────── helpers ─────────────────────────────

class _Pacer:
    """Minimum gap between two requests to the same host."""

    def __init__(self, default_delay: float, per_host: dict[str, float] | None = None) -> None:
        self.default = default_delay
        self.per_host = per_host or {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        gap = self.per_host.get(host, self.default)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = self._last.get(host, 0.0) + gap - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


async def _get(client: httpx.AsyncClient, pacer: _Pacer, url: str, *, params: dict | list | None = None,
               headers: dict | None = None) -> httpx.Response:
    """GET with pacing and retry on 429 / 5xx / network errors. Raises SourceBlocked or httpx.HTTPError."""
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        await pacer.wait(url)
        retry_after: float | None = None
        try:
            res = await client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last = exc
        else:
            if res.status_code != 429 and res.status_code < 500:
                res.raise_for_status()
                return res
            last = SourceBlocked(f"HTTP {res.status_code} from {urlsplit(url).netloc}")
            header = res.headers.get("retry-after", "")
            retry_after = float(header) if header.isdigit() else None
        if attempt < RETRIES and BACKOFF_SECONDS:
            await asyncio.sleep(min(retry_after or BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS))
    if isinstance(last, SourceBlocked):
        raise last
    raise SourceBlocked(f"{urlsplit(url).netloc}: {last}")


_ISO_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = _ISO_FRAC_RE.sub(r"\1", value.strip().replace("Z", "+00:00"))
    text = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text)  # +0000 -> +00:00
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


_CODES = {c for info in SYMBOLS.values() for c in (info.base, info.quote)}
_ACRONYMS = _CODES | {"fed", "fomc", "ecb", "boj", "boe", "rba", "rbnz", "boc", "snb", "pboc", "opec", "cpi", "pmi", "gdp",
                      "nfp", "ppi", "pce", "imf", "ism", "adp", "wti", "us", "uk", "eu", "ai", "dxy", "eia", "oecd"}
_ACRONYMS = {a.lower() for a in _ACRONYMS}


def title_from_slug(slug: str) -> str:
    """``eur-usd-slips-after-cpi-20260111`` -> ``EUR USD slips after CPI``. Pair names (``eurusd``) become ``EUR/USD``;
    currency / central-bank acronyms are upper-cased so the case-sensitive pair regex of the mapper still works."""
    slug = re.sub(r"-?(?:20\d{10}|20\d{6})$", "", slug.strip("/ "))
    words = []
    for w in slug.split("-"):
        lw = w.lower()
        if lw in _ACRONYMS:
            words.append(lw.upper())
        elif len(lw) == 6 and lw.upper() in SYMBOLS:
            words.append(f"{lw[:3].upper()}/{lw[3:].upper()}")
        else:
            words.append(w)
    text = " ".join(x for x in words if x)
    return text[:1].upper() + text[1:]


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _day_chunks(start: datetime, end: datetime, hours: int = 24):
    step = timedelta(hours=max(1, hours))
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def _slug(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1].removesuffix(".html")


def _news(source: str, title: str, when: datetime, url: str | None, raw: dict | None = None,
          summary: str | None = None) -> RawNews | None:
    title = re.sub(r"\s+", " ", html.unescape(title or "")).strip()
    if not title or when is None:
        return None
    return RawNews(kind="headline", source=source, title=title[:500], event_time=when.astimezone(UTC), url=url,
                   summary=summary, raw=raw or {})


async def _page_head(client: httpx.AsyncClient, pacer: _Pacer, url: str) -> tuple[str | None, datetime | None]:
    """(og:title, published time) read from the first bytes of an article page."""
    await pacer.wait(url)
    chunks: list[bytes] = []
    size = 0
    async with client.stream("GET", url) as res:
        res.raise_for_status()
        async for chunk in res.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= PAGE_HEAD_BYTES or b"</head>" in chunk:
                break
    text = b"".join(chunks).decode("utf-8", "replace")
    title = _meta(text, r'og:title') or _meta(text, r'twitter:title')
    published = _parse_iso(_meta(text, r'article:published_time')) or _parse_iso(
        (re.search(r'"datePublished"\s*:\s*"([^"]+)"', text) or [None, None])[1])
    return title, published


def _meta(text: str, prop: str) -> str | None:
    m = (re.search(rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]*)"', text)
         or re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{prop}"', text))
    return html.unescape(m.group(1)).strip() if m else None


# ───────────────────────────── investinglive ─────────────────────────────

_WEEK_RE = re.compile(r"(\d{4})-W(\d{2})")


def _week_range(name: str) -> tuple[datetime, datetime] | None:
    m = _WEEK_RE.search(name)
    if not m:
        return None
    try:
        monday = datetime.fromisocalendar(int(m.group(1)), int(m.group(2)), 1).replace(tzinfo=UTC)
    except ValueError:
        return None
    # the site's week starts on Sunday evening (Asia open): pad one day each side
    return monday - timedelta(days=1), monday + timedelta(days=8)


def _xml_entries(text: str) -> list[tuple[str, str | None]]:
    """(loc, lastmod) of every ``<url>`` / ``<sitemap>`` element of a sitemap document."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        return []
    out = []
    for el in root:
        loc = lastmod = None
        for child in el:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "loc":
                loc = (child.text or "").strip()
            elif tag == "lastmod":
                lastmod = (child.text or "").strip()
        if loc:
            out.append((loc, lastmod))
    return out


async def _investinglive(client, pacer, cfg: Settings, start: datetime, end: datetime, errors: list[str]) -> list[RawNews]:
    res = await _get(client, pacer, f"{INVESTINGLIVE}/articles-sitemap-index.xml")
    weeks = []
    for loc, _ in _xml_entries(res.text):
        rng = _week_range(loc)
        if rng and rng[0] < end and rng[1] > start:
            weeks.append(loc)
    items: list[RawNews] = []
    failures = 0
    for loc in sorted(weeks):
        try:
            week = await _get(client, pacer, loc)
        except (httpx.HTTPError, SourceBlocked) as exc:
            errors.append(f"investinglive {loc.rsplit('/', 1)[-1]}: {exc}")
            failures += 1
            if failures >= ABORT_AFTER_FAILURES:
                errors.append("investinglive: aborted after repeated failures")
                break
            continue
        failures = 0
        for url, lastmod in _xml_entries(week.text):
            when = _parse_iso(lastmod)
            if when is None or not (start <= when < end) or "/es/" in url:
                continue
            slug = _slug(url)
            if urlsplit(url).path.strip("/").count("/") < 1 or slug.count("-") < 2:  # section / tag pages, not articles
                continue
            rec = _news("investinglive", title_from_slug(slug), when, url, {"lastmod": lastmod})
            if rec:
                items.append(rec)
    if cfg.headline_history_investinglive_fetch_titles:
        await _enrich_titles(client, pacer, items, errors, limit=cfg.headline_history_max_total)
    return items


async def _enrich_titles(client, pacer, items: list[RawNews], errors: list[str], limit: int) -> None:
    failures = 0
    for item in items[:limit]:
        try:
            if not item.url:
                continue
            title, _ = await _page_head(client, pacer, item.url)
        except (httpx.HTTPError, SourceBlocked) as exc:
            failures += 1
            if failures >= ABORT_AFTER_FAILURES:
                errors.append(f"{item.source} title enrichment stopped: {exc}")
                return
            continue
        failures = 0
        if title:
            item.title = title[:500]


# ───────────────────────────── FXStreet via Wayback CDX ─────────────────────────────

_FX_TS_RE = re.compile(r"/news/(?P<slug>[^/?#]+)-(?P<ts>20\d{10})$")


async def _fxstreet_wayback(client, pacer, cfg: Settings, start: datetime, end: datetime, errors: list[str]) -> list[RawNews]:
    items: dict[str, RawNews] = {}
    failures = 0
    for lo, hi in _day_chunks(start, end, 24 * 7):
        days = []
        d = lo.replace(hour=0, minute=0, second=0, microsecond=0)
        while d < hi:
            days.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        params = [("url", "fxstreet.com/news/"), ("matchType", "prefix"), ("output", "json"), ("fl", "original"),
                  ("filter", "statuscode:200"), ("filter", "original:https?://www\\.fxstreet\\.com/news/.*-(?:" + "|".join(days) + ")\\d{4}$"),
                  ("collapse", "urlkey"), ("limit", "20000")]
        try:
            res = await _get(client, pacer, WAYBACK_CDX, params=params)
            rows = res.json() if res.text.strip() else []
        except (httpx.HTTPError, SourceBlocked, ValueError) as exc:
            errors.append(f"fxstreet(wayback) {lo:%Y-%m-%d}: {exc}")
            failures += 1
            if failures >= ABORT_AFTER_FAILURES:
                errors.append("fxstreet(wayback): aborted after repeated failures")
                break
            continue
        failures = 0
        for row in rows[1:] if rows and rows[0] == ["original"] else rows:
            url = row[0] if isinstance(row, list) else str(row)
            m = _FX_TS_RE.search(urlsplit(url).path)
            if not m:
                continue
            try:
                when = datetime.strptime(m["ts"], "%Y%m%d%H%M").replace(tzinfo=UTC)
            except ValueError:
                continue
            if not (start <= when < end):
                continue
            url = url.replace("http://", "https://", 1)
            rec = _news("fxstreet", title_from_slug(m["slug"]), when, url, {"via": "wayback-cdx"})
            if rec:
                items.setdefault(url, rec)
    return list(items.values())


# ───────────────────────────── CNBC ─────────────────────────────

_CNBC_ARTICLE_RE = re.compile(r"^https://www\.cnbc\.com/(\d{4})/(\d\d)/(\d\d)/([^/]+)\.html$")
_CNBC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>\s*<lastmod>\s*([^<]+?)\s*</lastmod>")


async def _cnbc(client, pacer, cfg: Settings, start: datetime, end: datetime, errors: list[str]) -> list[RawNews]:
    index = await _get(client, pacer, CNBC_INDEX)
    files = [loc for loc, _ in _xml_entries(index.text)]

    def num(url: str) -> int:
        m = re.search(r"(\d+)\.xml$", url)
        return int(m.group(1)) if m else -1

    candidates: dict[str, tuple[datetime, str]] = {}
    failures = 0
    for loc in sorted(files, key=num, reverse=True):  # newest file first; each file is sorted by lastmod desc
        try:
            res = await _get(client, pacer, loc)
        except (httpx.HTTPError, SourceBlocked) as exc:
            errors.append(f"cnbc {loc.rsplit('/', 1)[-1]}: {exc}")
            failures += 1
            if failures >= ABORT_AFTER_FAILURES:
                break
            continue
        failures = 0
        oldest_lastmod = None
        for url, lastmod in _CNBC_RE.findall(res.text):
            lm = _parse_iso(lastmod)
            if lm and (oldest_lastmod is None or lm < oldest_lastmod):
                oldest_lastmod = lm
            m = _CNBC_ARTICLE_RE.match(url)
            if not m or "/video/" in url:
                continue
            day = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=UTC)
            if start - timedelta(days=1) <= day < end + timedelta(days=1):
                candidates[url] = (day, m[4])
        # lastmod >= publish date: once a whole file is older than `start`, older files cannot hold newer articles
        if oldest_lastmod is not None and oldest_lastmod < start - timedelta(days=1):
            break

    # Rank by finance relevance (title from slug), then spend the page-fetch budget on the best ones.
    scored = []
    seen_in_day: Counter = Counter()
    for url, (day, slug) in sorted(candidates.items(), key=lambda kv: kv[1][0]):
        rec = sources.from_raw_news(RawNews(kind="headline", source="cnbc", title=title_from_slug(slug), event_time=day, url=url),
                                    cfg.backtest_max_symbols)
        if rec is None:
            continue
        rank = _IMPACT_RANK[rec.impact] * 2 + (1 if rec.symbols else 0)
        if rank == 0:
            continue
        # best tier first; inside a tier take the n-th article of every day before the (n+1)-th (even coverage)
        seen_in_day[(rank, day)] += 1
        scored.append((-rank, seen_in_day[(rank, day)], day, url, slug))
    scored.sort()
    items: list[RawNews] = []
    fetched = failures = 0
    for _, _, day, url, slug in scored:
        if fetched >= cfg.headline_history_cnbc_max_page_fetches:
            errors.append(f"cnbc: page-fetch cap {cfg.headline_history_cnbc_max_page_fetches} reached; "
                          f"{len(scored) - fetched} relevant articles left without a timestamp")
            break
        fetched += 1
        try:
            title, published = await _page_head(client, pacer, url)
        except (httpx.HTTPError, SourceBlocked) as exc:
            failures += 1
            if failures >= ABORT_AFTER_FAILURES:
                errors.append(f"cnbc page fetch stopped: {exc}")
                break
            continue
        failures = 0
        if published is None or not (start <= published < end):
            continue
        rec = _news("cnbc", title or title_from_slug(slug), published, url, {"slug_date": day.date().isoformat()})
        if rec:
            items.append(rec)
    return items


# ───────────────────────────── GDELT ─────────────────────────────

async def _gdelt(client, pacer, cfg: Settings, start: datetime, end: datetime, errors: list[str]) -> list[RawNews]:
    items: list[RawNews] = []
    failures = 0
    # ArtList only really serves the last ~3 months (older windows return a handful of leftovers): skip them.
    lookback = cfg.headline_history_gdelt_lookback_days
    floor = datetime.now(UTC) - timedelta(days=lookback) if lookback else None
    if floor is not None and floor > start:
        errors.append(f"gdelt: skipped {start:%Y-%m-%d}..{min(floor, end):%Y-%m-%d} (older than {lookback} days)")
        start = floor
    for lo, hi in _day_chunks(start, end, cfg.headline_history_gdelt_chunk_hours):
        params = {"query": GDELT_QUERY, "mode": "artlist", "format": "json", "maxrecords": "250", "sort": "datedesc",
                  "startdatetime": lo.strftime("%Y%m%d%H%M%S"), "enddatetime": hi.strftime("%Y%m%d%H%M%S")}
        try:
            res = await _get(client, pacer, GDELT_URL, params=params)
            body = res.text.strip()
            try:
                payload = json.loads(body) if body else {}  # an empty window can come back as an empty body
            except ValueError:
                # GDELT answers plain text for bad queries / throttling / outages
                raise ValueError(body[:120]) from None
        except (httpx.HTTPError, SourceBlocked, ValueError) as exc:
            errors.append(f"gdelt {lo:%Y-%m-%d %H}h: {exc}")
            failures += 1
            if failures >= ABORT_AFTER_FAILURES:
                errors.append("gdelt: aborted after repeated failures")
                break
            continue
        failures = 0
        for art in payload.get("articles") or []:
            when = _gdelt_time(art.get("seendate"))
            if when is None or not (start <= when < end):
                continue
            rec = _news("gdelt", art.get("title") or "", when, art.get("url"),
                        {"domain": art.get("domain"), "country": art.get("sourcecountry")})
            if rec:
                items.append(rec)
    return items


def _gdelt_time(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


# ───────────────────────────── Marketaux / Finnhub (optional keys) ─────────────────────────────

async def _marketaux(client, pacer, cfg: Settings, start: datetime, end: datetime, errors: list[str]) -> list[RawNews]:
    items: list[RawNews] = []
    failures = 0
    for lo, hi in _day_chunks(start, end, 24):
        for page in range(1, max(1, cfg.headline_history_marketaux_max_pages) + 1):
            params = {"api_token": cfg.headline_history_marketaux_api_key, "language": "en",
                      "published_after": lo.strftime("%Y-%m-%dT%H:%M:%S"), "published_before": hi.strftime("%Y-%m-%dT%H:%M:%S"),
                      "search": 'forex | "central bank" | gold | "interest rates" | inflation | oil', "page": str(page)}
            try:
                res = await _get(client, pacer, MARKETAUX_URL, params=params)
                payload = res.json()
            except httpx.HTTPStatusError as exc:
                # 401 bad key / 402 plan limit / 429 usage limit: nothing more to get today
                errors.append(f"marketaux: HTTP {exc.response.status_code}; stopped")
                return items
            except (httpx.HTTPError, SourceBlocked, ValueError) as exc:
                errors.append(f"marketaux {lo:%Y-%m-%d}: {str(exc).replace(cfg.headline_history_marketaux_api_key, '***')}")
                failures += 1
                if failures >= ABORT_AFTER_FAILURES:
                    return items
                break
            failures = 0
            data = payload.get("data") or []
            for art in data:
                when = _parse_iso(art.get("published_at"))
                if when is None or not (start <= when < end):
                    continue
                rec = _news("marketaux", art.get("title") or "", when, art.get("url"), {"domain": art.get("source")},
                            art.get("description"))
                if rec:
                    items.append(rec)
            if len(data) < int((payload.get("meta") or {}).get("limit") or 1):
                break
    return items


async def _finnhub(client, pacer, cfg: Settings, start: datetime, end: datetime, errors: list[str]) -> list[RawNews]:
    items: list[RawNews] = []
    for category in ("forex", "general"):
        try:
            res = await _get(client, pacer, FINNHUB_URL, params={"category": category, "token": cfg.headline_history_finnhub_api_key})
            data = res.json()
        except (httpx.HTTPError, SourceBlocked, ValueError) as exc:
            errors.append(f"finnhub {category}: {str(exc).replace(cfg.headline_history_finnhub_api_key, '***')}")
            continue
        for art in data if isinstance(data, list) else []:
            try:
                when = datetime.fromtimestamp(int(art["datetime"]), UTC)
            except (KeyError, TypeError, ValueError, OSError):
                continue
            if not (start <= when < end):
                continue
            rec = _news("finnhub", art.get("headline") or "", when, art.get("url"), {"publisher": art.get("source")},
                        art.get("summary"))
            if rec:
                items.append(rec)
    return items


# ───────────────────────────── orchestration ─────────────────────────────

def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _enabled_sources(cfg: Settings) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if cfg.headline_history_investinglive:
        out.append(("investinglive", _investinglive))
    if cfg.headline_history_fxstreet_wayback:
        out.append(("fxstreet", _fxstreet_wayback))
    if cfg.headline_history_cnbc:
        out.append(("cnbc", _cnbc))
    if cfg.headline_history_gdelt:
        out.append(("gdelt", _gdelt))
    if cfg.headline_history_marketaux_api_key:
        out.append(("marketaux", _marketaux))
    if cfg.headline_history_finnhub_api_key:
        out.append(("finnhub", _finnhub))
    return out


async def fetch_headline_records(start: datetime, end: datetime, *, client: httpx.AsyncClient | None = None,
                                 settings: Settings | None = None, errors: list[str] | None = None,
                                 stats: dict[str, int] | None = None) -> list[sources.EventRecord]:
    """Headlines in ``[start, end)`` as ``EventRecord`` (kind="headline"), oldest first.

    ``errors`` / ``stats`` (optional, filled in place) receive per-source problems and kept-record counts per source.
    """
    cfg = settings or get_settings()
    errors = errors if errors is not None else []
    stats = stats if stats is not None else {}
    start, end = _aware(start), _aware(end)
    end = min(end, datetime.now(UTC))
    if not cfg.headline_history_enabled or end <= start:
        return []
    active = _enabled_sources(cfg)
    if not active:
        return []

    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    delay = cfg.headline_history_request_delay_seconds
    pacer = _Pacer(delay, {"api.gdeltproject.org": max(delay, cfg.headline_history_gdelt_min_interval_seconds),
                           "web.archive.org": delay,
                           "www.cnbc.com": min(delay, 0.5)})

    async def run(name, fn) -> tuple[str, list[RawNews]]:
        try:
            return name, await fn(client, pacer, cfg, start, end, errors)
        except Exception as exc:  # one source must never fail the whole run
            log.warning("headline history source %s failed: %s", name, exc)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return name, []

    try:
        results = await asyncio.gather(*(run(n, f) for n, f in active))
    finally:
        if own:
            await client.aclose()

    records: dict[str, sources.EventRecord] = {}
    seen_titles: set[tuple[str, str]] = set()
    for name, raws in results:
        kept = 0
        for raw in raws:
            if raw.event_time is None or not (start <= raw.event_time < end):
                continue
            rec = sources.from_raw_news(raw, cfg.backtest_max_symbols)
            if rec is None or rec.dedupe_key in records:
                continue
            if cfg.headline_history_relevant_only:
                # general-news aggregators are noisy (a bushfire "maps" to AUD): they need a market-moving keyword
                general = name in _GENERAL_SOURCES
                if rec.impact == "low" and (general or not rec.symbols):
                    continue
            title_key = (_norm_title(rec.title), rec.time.strftime("%Y%m%d"))  # same wire headline from several outlets
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            records[rec.dedupe_key] = rec
            kept += 1
        stats[name] = kept

    out = list(records.values())
    cap = cfg.headline_history_max_total
    if cap and len(out) > cap:
        # keep the most market-moving ones, then restore chronological order
        out.sort(key=lambda r: (-_IMPACT_RANK.get(r.impact, 0), -bool(r.symbols), r.time))
        errors.append(f"headline cap {cap} reached: dropped {len(out) - cap} lowest-impact headlines")
        out = out[:cap]
    out.sort(key=lambda r: r.time)
    return out


async def fetch_headline_history(start: datetime, end: datetime, *, client: httpx.AsyncClient | None = None,
                                 settings: Settings | None = None, errors: list[str] | None = None,
                                 stats: dict[str, int] | None = None) -> list[dict]:
    """Headlines in ``[start, end)`` as plain dicts with the ``EventRecord`` fields (``EventRecord(**d)`` works):
    time (UTC datetime), kind="headline", source, title, impact (high|medium|low), dedupe_key, currency, country,
    summary, url, category, actual/forecast/previous (None), symbols, raw. Oldest first. Never raises for a failing
    source; pass an ``errors`` list to read what went wrong and ``stats`` for per-source counts."""
    records = await fetch_headline_records(start, end, client=client, settings=settings, errors=errors, stats=stats)
    return [asdict(r) for r in records]
