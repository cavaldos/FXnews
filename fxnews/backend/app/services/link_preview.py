"""Link preview: fetch a public web page server-side and boil it down to a small card.

Security: the URL comes from the browser, so this is an SSRF surface. Only http/https;
every hostname is resolved and *each* resolved address (and every redirect hop) must be a global
unicast IP - loopback, private, link-local (cloud metadata), multicast, reserved and unspecified are refused.
After connecting we re-check the peer address, which closes most of the DNS-rebinding window.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

log = logging.getLogger("fxnews.link_preview")

TIMEOUT_S = 6.0
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
EXCERPT_CHARS = 1500
TTL_OK_S = 6 * 3600
TTL_FAIL_S = 10 * 60
CACHE_MAX = 500
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 FXNewsLinkPreview/1.0"
)
_HTML_TYPES = ("text/html", "application/xhtml+xml")


class BlockedURL(ValueError):
    """The URL is not a public http(s) URL (bad scheme, private/loopback address, ...)."""


class FetchFailed(Exception):
    """Upstream problem (timeout, HTTP error, not HTML, too many redirects)."""


@dataclass
class Preview:
    ok: bool
    url: str
    final_url: str = ""
    title: str = ""
    site_name: str = ""
    favicon: str = ""
    image: str = ""
    description: str = ""
    published: str = ""
    author: str = ""
    excerpt: str = ""
    frameable: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── SSRF guard ─────────────────────────────────────────────────────────────────────────────────────────────────
def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def resolve_host(host: str) -> list[str]:
    """All addresses of `host` (separate function so tests can stub DNS)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


async def check_url(url: str) -> str:
    """Return the normalised URL, or raise BlockedURL."""
    url = url.strip()
    if len(url) > 2048:
        raise BlockedURL("URL too long")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("only http/https URLs are allowed")
    host = parts.hostname
    if not host:
        raise BlockedURL("URL has no host")
    if parts.username or parts.password:
        raise BlockedURL("credentials in URL are not allowed")
    try:
        _ = parts.port
    except ValueError as exc:
        raise BlockedURL("invalid port") from exc
    if host.lower().rstrip(".") in ("localhost",) or host.lower().endswith((".localhost", ".local", ".internal")):
        raise BlockedURL("host is not public")
    try:
        ipaddress.ip_address(host)
        addrs = [host]  # literal IP (also covers [::1])
    except ValueError:
        try:
            addrs = await resolve_host(host)
        except OSError as exc:
            raise FetchFailed("host not found") from exc
    if not addrs or not all(_is_public(a) for a in addrs):
        raise BlockedURL("host resolves to a non-public address")
    return url


# ── fetch ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _frameable(headers: httpx.Headers) -> bool:
    """False when the page forbids being embedded (X-Frame-Options / CSP frame-ancestors other than `*`)."""
    if headers.get("x-frame-options"):
        return False
    for csp in headers.get_list("content-security-policy"):
        for directive in csp.split(";"):
            tokens = directive.strip().split()
            if tokens and tokens[0].lower() == "frame-ancestors":
                return tokens[1:] == ["*"]
    return True


async def _fetch_html(url: str) -> tuple[str, bytes, httpx.Headers]:
    """GET with manual redirects (every hop is re-validated), size cap and total deadline."""
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(TIMEOUT_S),
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                 "Accept-Language": "en,vi;q=0.8"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            url = await check_url(url)
            try:
                async with client.stream("GET", url) as resp:
                    stream = resp.extensions.get("network_stream")
                    peer = stream.get_extra_info("server_addr") if stream is not None else None
                    if peer and not _is_public(str(peer[0])):
                        raise BlockedURL("host connected to a non-public address")
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code >= 400:
                        raise FetchFailed(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "text/html").split(";")[0].strip().lower()
                    if ctype not in _HTML_TYPES:
                        raise FetchFailed(f"not an HTML page ({ctype or 'unknown'})")
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) >= MAX_BYTES:
                            break
                    return url, bytes(buf[:MAX_BYTES]), resp.headers
            except httpx.TimeoutException as exc:
                raise FetchFailed("timed out") from exc
            except httpx.HTTPError as exc:
                raise FetchFailed(f"request failed ({type(exc).__name__})") from exc
    raise FetchFailed("too many redirects")


# ── parsing ────────────────────────────────────────────────────────────────────────────────────────────────────
_WS = re.compile(r"\s+")
_NOISE = "script, style, noscript, nav, header, footer, aside, form, iframe, svg, template, button, select, figcaption"
_JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


def _meta(tree: HTMLParser, *names: str) -> str:
    for name in names:
        for attr in ("property", "name"):
            node = tree.css_first(f'meta[{attr}="{name}"]')
            if node is not None and _clean(node.attributes.get("content")):
                return _clean(node.attributes.get("content"))
    return ""


def _abs(base: str, ref: str) -> str:
    ref = (ref or "").strip()
    if not ref or ref.lower().startswith(("data:", "javascript:")):
        return ""
    out = urljoin(base, ref)
    return out if urlsplit(out).scheme in ("http", "https") else ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def _text_len(node: Node) -> int:
    return len(_clean(node.text()))


def extract_excerpt(tree: HTMLParser) -> str:
    """Readable article text: strip page chrome, pick <article>/<main>, then the parent holding the most <p> text."""
    body = tree.body
    if body is None:
        return ""
    for node in body.css(_NOISE):
        node.decompose()
    scope: Node = body
    articles = body.css("article")
    if articles:
        scope = max(articles, key=_text_len)
    else:
        mains = body.css("main, [role=main]")
        if mains:
            scope = max(mains, key=_text_len)
    paras = [p for p in scope.css("p") if _text_len(p) >= 40]
    if not paras and scope is not body:
        paras = [p for p in body.css("p") if _text_len(p) >= 40]
    if not paras:
        return ""
    clusters: dict[int, tuple[int, list[Node]]] = {}
    for p in paras:
        parent = p.parent
        key = parent.mem_id if parent is not None else 0
        total, items = clusters.get(key, (0, []))
        items.append(p)
        clusters[key] = (total + _text_len(p), items)
    # Earliest substantial cluster wins (sectioned pages like Wikipedia would otherwise start mid-article).
    biggest = max(total for total, _ in clusters.values())
    best = next(items for total, items in clusters.values() if total >= min(biggest, max(200, biggest * 0.35)))
    return _truncate("\n\n".join(_clean(p.text()) for p in best), EXCERPT_CHARS)


def parse_page(html: bytes, final_url: str, headers: httpx.Headers) -> Preview:
    tree = HTMLParser(html)
    host = (urlsplit(final_url).hostname or "").removeprefix("www.")

    title = _meta(tree, "og:title", "twitter:title")
    if not title:
        t = tree.css_first("title")
        title = _clean(t.text()) if t is not None else ""
    description = _meta(tree, "og:description", "twitter:description", "description")
    site_name = _meta(tree, "og:site_name", "application-name") or host
    image = _abs(final_url, _meta(tree, "og:image", "og:image:url", "twitter:image", "twitter:image:src"))

    favicon = ""
    for sel in ('link[rel~="icon"]', 'link[rel="shortcut icon"]', 'link[rel="apple-touch-icon"]'):
        node = tree.css_first(sel)
        if node is not None:
            favicon = _abs(final_url, node.attributes.get("href") or "")
            if favicon:
                break
    if not favicon:
        parts = urlsplit(final_url)
        favicon = f"{parts.scheme}://{parts.netloc}/favicon.ico"

    published = _meta(tree, "article:published_time", "og:article:published_time", "datePublished",
                      "pubdate", "publish-date", "date", "parsely-pub-date", "sailthru.date")
    if not published:
        t = tree.css_first("article time[datetime], time[datetime]")
        if t is not None:
            published = _clean(t.attributes.get("datetime"))
    if not published:
        m = _JSONLD_DATE.search(html[:MAX_BYTES].decode("utf-8", "ignore"))
        published = m.group(1) if m else ""

    author = _meta(tree, "author", "article:author", "parsely-author")
    if author.lower().startswith(("http://", "https://")):
        author = ""

    excerpt = extract_excerpt(tree)
    if not excerpt:
        excerpt = _truncate(description, EXCERPT_CHARS)

    if not (title or excerpt or image):
        raise FetchFailed("page has no readable content")
    return Preview(
        ok=True, url=final_url, final_url=final_url, title=_truncate(title, 300), site_name=site_name[:120],
        favicon=favicon, image=image, description=_truncate(description, 500), published=published[:64],
        author=_truncate(author, 120), excerpt=excerpt, frameable=_frameable(headers),
    )


# ── cache + public entry point ─────────────────────────────────────────────────────────────────────────────────
@dataclass
class _Entry:
    expires: float
    value: Preview


@dataclass
class _Cache:
    items: dict[str, _Entry] = field(default_factory=dict)
    inflight: dict[str, asyncio.Future] = field(default_factory=dict)


_cache = _Cache()


def clear_cache() -> None:
    _cache.items.clear()
    _cache.inflight.clear()


def _store(url: str, value: Preview, ttl: float) -> None:
    if len(_cache.items) >= CACHE_MAX:
        now = time.monotonic()
        for k in [k for k, e in _cache.items.items() if e.expires <= now]:
            del _cache.items[k]
        while len(_cache.items) >= CACHE_MAX:
            del _cache.items[next(iter(_cache.items))]  # oldest insertion
    _cache.items[url] = _Entry(time.monotonic() + ttl, value)


async def _build(url: str) -> Preview:
    try:
        final_url, body, headers = await _fetch_html(url)
        preview = parse_page(body, final_url, headers)
        preview.url = url
        _store(url, preview, TTL_OK_S)
        return preview
    except FetchFailed as exc:
        failed = Preview(ok=False, url=url, error=str(exc))
        _store(url, failed, TTL_FAIL_S)
        return failed
    except BlockedURL:
        raise
    except Exception as exc:  # parser bug etc. must not 500 a hover
        log.warning("link preview failed for %s: %r", url, exc)
        failed = Preview(ok=False, url=url, error="could not read the page")
        _store(url, failed, TTL_FAIL_S)
        return failed


async def get_preview(url: str) -> Preview:
    """Cached preview for `url`. Raises BlockedURL for non-public / non-http(s) URLs (never cached, never fetched)."""
    url = await check_url(url)
    hit = _cache.items.get(url)
    if hit is not None and hit.expires > time.monotonic():
        return hit.value
    pending = _cache.inflight.get(url)
    if pending is not None:
        return await asyncio.shield(pending)
    task = asyncio.ensure_future(_build(url))
    _cache.inflight[url] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done():
            _cache.inflight.pop(url, None)


def preview_json(p: Preview) -> str:  # pragma: no cover - debugging helper
    return json.dumps(p.to_dict(), ensure_ascii=False, indent=2)
