"""Generic RSS/Atom headline source (FXStreet, ...). Returns ``RawNews(kind="headline")``."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

from app.sources.base import RawNews

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 15.0
SUMMARY_MAX_CHARS = 600
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
}

_BLOCK_TAG_RE = re.compile(r"</?(?:p|br|div|li|ul|ol|tr|td|th|table|h[1-6]|blockquote|hr)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"</?[a-zA-Z!][^>]*>")  # requires a letter/!, so "EUR/USD < 1.10" survives
_WS_RE = re.compile(r"\s+")


class RssFetchError(RuntimeError):
    """The feed could not be downloaded or is not a valid feed (network, HTTP status, bad body)."""


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _html_to_text(raw: str) -> str:
    text = _BLOCK_TAG_RE.sub(" ", raw)
    text = _TAG_RE.sub("", text)
    return _collapse_ws(html.unescape(text))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _struct_to_utc(parsed: Any) -> datetime | None:
    """feedparser normalises *_parsed to a UTC time.struct_time."""
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):  # e.g. leap second "60"
        return None


def _event_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        dt = _struct_to_utc(entry.get(key))
        if dt is not None:
            return dt
    # feedparser could not parse the date string; try RFC 2822 ourselves.
    for key in ("published", "updated"):
        value = entry.get(key)
        if not value:
            continue
        try:
            dt = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            continue
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


def _entry_url(entry: Any) -> str | None:
    link = (entry.get("link") or "").strip()
    if link:
        return link
    guid = (entry.get("id") or "").strip()
    return guid if guid.startswith(("http://", "https://")) else None


def _entry_raw(entry: Any, feed_url: str) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "guid": entry.get("id") or entry.get("guid"),
        "published": entry.get("published") or entry.get("updated"),
        "feed_url": feed_url,
    }
    if entry.get("author"):
        raw["author"] = str(entry["author"])
    tags = [str(t.get("term")) for t in entry.get("tags") or [] if t.get("term")]
    if tags:
        raw["tags"] = tags
    for enc in entry.get("enclosures") or []:
        href = enc.get("href")
        if href and str(enc.get("type", "")).startswith("image"):
            raw["image"] = href
            break
    return raw


class RssSource:
    """One RSS feed. ``fetch()`` raises ``RssFetchError`` on any download/parse failure."""

    def __init__(self, name: str, url: str, *, timeout: float = REQUEST_TIMEOUT_S) -> None:
        self.name = name
        self.url = url
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"RssSource(name={self.name!r}, url={self.url!r})"

    async def fetch(self) -> list[RawNews]:
        content = await self._download()
        parsed = await asyncio.to_thread(feedparser.parse, content)
        # feedparser never raises: a Cloudflare/HTML page or truncated XML comes back as an empty,
        # "bozo" feed. Surface that instead of silently reporting "no news".
        if not parsed.entries and (parsed.bozo or not parsed.get("version")):
            reason = repr(parsed.get("bozo_exception")) if parsed.bozo else "unrecognised feed format"
            raise RssFetchError(f"{self.name}: {self.url} did not return a valid RSS/Atom feed ({reason})")

        items: list[RawNews] = []
        for entry in parsed.entries:
            title = _collapse_ws(str(entry.get("title") or ""))
            if not title:
                continue
            summary = _truncate(_html_to_text(str(entry.get("summary") or "")), SUMMARY_MAX_CHARS)
            items.append(
                RawNews(
                    kind="headline",
                    source=self.name,
                    title=title,
                    url=_entry_url(entry),
                    summary=summary or None,
                    event_time=_event_time(entry),
                    raw=_entry_raw(entry, self.url),
                )
            )
        logger.debug("rss %s: %d items (%d entries)", self.name, len(items), len(parsed.entries))
        return items

    async def _download(self) -> bytes:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout), headers=_HEADERS, follow_redirects=True
            ) as client:
                resp = await client.get(self.url)
                resp.raise_for_status()
                return resp.content
        except httpx.HTTPStatusError as exc:
            raise RssFetchError(f"{self.name}: HTTP {exc.response.status_code} from {self.url}") from exc
        except httpx.TimeoutException as exc:
            raise RssFetchError(f"{self.name}: timed out after {self.timeout:g}s fetching {self.url}") from exc
        except httpx.HTTPError as exc:
            raise RssFetchError(f"{self.name}: request to {self.url} failed: {exc!r}") from exc


def build_rss_sources(settings: Any) -> list[RssSource]:
    """Build one ``RssSource`` per entry of ``settings.rss_feeds`` (name -> url)."""
    sources: list[RssSource] = []
    for name, url in (getattr(settings, "rss_feeds", None) or {}).items():
        name, url = str(name).strip(), str(url).strip()
        if name and url:
            sources.append(RssSource(name, url))
    return sources
