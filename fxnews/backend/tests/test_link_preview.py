import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app
from app.services import link_preview as lp

PAGE = """<!doctype html><html><head>
<title>Fallback title</title>
<meta property="og:title" content="ECB signals readiness">
<meta property="og:site_name" content="Example Wire">
<meta property="og:image" content="/img/hero.png">
<meta property="og:description" content="Short description.">
<meta property="article:published_time" content="2026-11-11T08:32:31Z">
<meta name="author" content="Jane Doe">
<link rel="icon" href="/fav.ico">
</head><body>
<nav><p>Home | Markets | Currencies and a very long menu item that should be ignored entirely ok</p></nav>
<article>
<p>The euro rose on Wednesday after a senior ECB policymaker said the bank is ready to deploy new tools if needed.</p>
<p>Analysts said the move reflects a broader repricing of rate expectations across the euro area this quarter.</p>
</article>
<footer><p>Copyright footer text that is long enough to be counted as a paragraph if not removed.</p></footer>
<script>var x = 1;</script>
</body></html>"""


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    async def fake(host):
        return {"127.0.0.1": ["127.0.0.1"], "internal.test": ["10.0.0.7"], "rebind.test": ["93.184.216.34", "10.0.0.1"]}.get(host, ["93.184.216.34"])

    monkeypatch.setattr(lp, "resolve_host", fake)
    lp.clear_cache()
    yield
    lp.clear_cache()


@pytest.fixture
def client():
    return TestClient(app)


def _html(body=PAGE, **headers):
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


@respx.mock
def test_preview_extracts_fields(client):
    route = respx.get("https://news.example.com/a").mock(return_value=_html())
    r = client.get("/api/link-preview", params={"url": "https://news.example.com/a"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["title"] == "ECB signals readiness"
    assert d["site_name"] == "Example Wire"
    assert d["image"] == "https://news.example.com/img/hero.png"
    assert d["favicon"] == "https://news.example.com/fav.ico"
    assert d["published"] == "2026-11-11T08:32:31Z"
    assert d["author"] == "Jane Doe"
    assert d["excerpt"].startswith("The euro rose on Wednesday")
    assert "Copyright" not in d["excerpt"] and "Markets" not in d["excerpt"]
    assert d["frameable"] is True
    # second call is served from the cache
    client.get("/api/link-preview", params={"url": "https://news.example.com/a"})
    assert route.call_count == 1


@respx.mock
def test_frameable_false_with_xfo_or_csp(client):
    respx.get("https://a.example.com/").mock(return_value=_html(**{"X-Frame-Options": "DENY"}))
    respx.get("https://b.example.com/").mock(return_value=_html(**{"Content-Security-Policy": "default-src 'self'; frame-ancestors 'self'"}))
    assert client.get("/api/link-preview", params={"url": "https://a.example.com/"}).json()["frameable"] is False
    assert client.get("/api/link-preview", params={"url": "https://b.example.com/"}).json()["frameable"] is False


@respx.mock
def test_excerpt_capped_and_fallback_to_description(client):
    long_p = "<p>" + "word " * 600 + "</p>"
    respx.get("https://long.example.com/").mock(return_value=_html(f"<html><body><main>{long_p}</main></body></html>"))
    d = client.get("/api/link-preview", params={"url": "https://long.example.com/"}).json()
    assert d["ok"] and len(d["excerpt"]) <= lp.EXCERPT_CHARS + 1 and d["excerpt"].endswith("…")
    respx.get("https://nop.example.com/").mock(return_value=_html('<html><head><title>T</title><meta name="description" content="Only a description"></head><body></body></html>'))
    d = client.get("/api/link-preview", params={"url": "https://nop.example.com/"}).json()
    assert d["excerpt"] == "Only a description"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8000/x", "http://[::1]/", "http://10.1.2.3/", "http://192.168.0.1/",
    "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/", "http://internal.test/", "http://rebind.test/",
    "ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "http://user:pw@example.com/",
    "http://[::ffff:127.0.0.1]/",
])
@respx.mock
def test_ssrf_blocked(client, url):
    route = respx.route().mock(return_value=_html())
    r = client.get("/api/link-preview", params={"url": url})
    assert r.status_code == 400
    assert route.call_count == 0


@respx.mock
def test_redirect_to_private_is_blocked_and_never_fetched(client):
    respx.get("https://redir.example.com/").mock(return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    meta = respx.get("http://169.254.169.254/").mock(return_value=_html())
    r = client.get("/api/link-preview", params={"url": "https://redir.example.com/"})
    assert r.status_code == 400
    assert meta.call_count == 0


@respx.mock
def test_redirect_followed(client):
    respx.get("https://short.example.com/x").mock(return_value=httpx.Response(301, headers={"location": "/final"}))
    respx.get("https://short.example.com/final").mock(return_value=_html())
    d = client.get("/api/link-preview", params={"url": "https://short.example.com/x"}).json()
    assert d["ok"] and d["final_url"] == "https://short.example.com/final"


@respx.mock
def test_failures_are_negative_cached(client):
    route = respx.get("https://down.example.com/").mock(return_value=httpx.Response(500))
    d = client.get("/api/link-preview", params={"url": "https://down.example.com/"}).json()
    assert d["ok"] is False and "500" in d["error"]
    client.get("/api/link-preview", params={"url": "https://down.example.com/"})
    assert route.call_count == 1


@respx.mock
def test_timeout_and_non_html(client):
    respx.get("https://slow.example.com/").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("https://img.example.com/a.png").mock(return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    assert client.get("/api/link-preview", params={"url": "https://slow.example.com/"}).json()["error"] == "timed out"
    assert "not an HTML page" in client.get("/api/link-preview", params={"url": "https://img.example.com/a.png"}).json()["error"]


@respx.mock
def test_size_cap(client):
    big = "<html><head><title>Big</title></head><body><p>" + "x" * (3 * 1024 * 1024) + "</p></body></html>"
    respx.get("https://big.example.com/").mock(return_value=_html(big))
    d = client.get("/api/link-preview", params={"url": "https://big.example.com/"}).json()
    assert d["ok"] and d["title"] == "Big"
