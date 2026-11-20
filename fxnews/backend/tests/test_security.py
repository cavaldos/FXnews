"""CSRF guard (cross-site POSTs) + Host allow-list (DNS rebinding)."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.analysis import advisor
from app.config import Settings, get_settings
from app.main import app
from app.notify import discord
from app.schemas import AnalysisOut, IngestResult
from app.security import install_security
from app.services import ingest

ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
EVIL = {"Origin": "http://evil.example"}
MUTATING = ["/api/ingest/run", "/api/news/1/analyze", "/api/notify/test"]


@pytest.fixture
def calls(monkeypatch):
    """Replace the real work behind every mutating endpoint with a counter."""
    count = {"ingest": 0, "analyze": 0, "notify": 0}

    async def run_all(force=False):
        count["ingest"] += 1
        return IngestResult(inserted=0, analyzed=0)

    async def analyze_news(news_id, **_kw):
        count["analyze"] += 1
        return AnalysisOut(id=1, kind="headline", model="m", summary="ok", created_at=datetime.now(UTC))

    async def send_text(_content):
        count["notify"] += 1
        return True

    monkeypatch.setattr(ingest, "run_all", run_all)
    monkeypatch.setattr(advisor, "analyze_news", analyze_news)
    monkeypatch.setattr(discord, "send_text", send_text)
    monkeypatch.setattr(get_settings(), "discord_webhook_url", "https://discord.test/hook")
    return count


@pytest.fixture
def client():
    return TestClient(app)


# ── CSRF guard on the real app ───────────────────────────────────────


@pytest.mark.parametrize("path", MUTATING)
def test_cross_site_post_is_rejected_without_side_effects(client, calls, path):
    r = client.post(path, headers=EVIL)
    assert r.status_code == 403
    assert "Cross-site request blocked" in r.json()["detail"] and "evil.example" in r.json()["detail"]
    assert calls == {"ingest": 0, "analyze": 0, "notify": 0}


@pytest.mark.parametrize(
    "origin",
    [
        "null",  # sandboxed iframe / redirect chains
        "http://localhost:5174",  # another local app on a different port
        "http://localhost",  # same host, other port (80)
        "https://localhost:5173",  # other scheme
        "http://localhost:5173.evil.example",  # prefix trick
        "http://evil.example/http://localhost:5173",
    ],
)
def test_look_alike_origins_are_rejected(client, calls, origin):
    assert client.post("/api/ingest/run", headers={"Origin": origin}).status_code == 403
    assert calls["ingest"] == 0


def test_vite_proxy_and_curl_still_work(client, calls):
    # The browser talks to :5173; Vite forwards to :8000 keeping Host/Origin of the dev server.
    for host in ("localhost:5173", "127.0.0.1:5173"):
        r = client.post("/api/ingest/run", headers={"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200, host
    # Direct browser call from the dev page to the API port: same-site, allowed Origin.
    r = client.post("/api/ingest/run", headers={"Host": "localhost:8000", "Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 200
    # curl / scripts: no Origin, no Sec-Fetch-Site.
    assert client.post("/api/ingest/run", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.post("/api/news/1/analyze", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.post("/api/notify/test", headers={"Host": "localhost:8000"}).status_code == 200
    assert calls == {"ingest": 4, "analyze": 1, "notify": 1}


def test_origin_match_is_case_insensitive(client, calls):
    assert client.post("/api/ingest/run", headers={"Origin": "HTTP://LOCALHOST:5173"}).status_code == 200


@pytest.mark.parametrize("site,ok", [("cross-site", False), ("same-site", True), ("same-origin", True), ("none", True)])
def test_sec_fetch_site_is_checked_when_origin_is_absent(client, calls, site, ok):
    r = client.post("/api/ingest/run", headers={"Sec-Fetch-Site": site})
    assert (r.status_code == 200) is ok


def test_allowed_origin_wins_over_sec_fetch_site(client, calls):
    # The operator listed this origin on purpose (e.g. UI on another domain), so it is not second-guessed.
    r = client.post("/api/ingest/run", headers={"Origin": "http://localhost:5173", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_reads_are_never_blocked(client):
    r = client.get("/api/health", headers=EVIL)
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers  # CORS (not the guard) keeps the page from reading it


def test_cors_preflight_unchanged(client):
    ok = client.options("/api/ingest/run", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    bad = client.options("/api/ingest/run", headers={**EVIL, "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 400


# ── Host allow-list ──────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["rebind.attacker.example", "rebind.attacker.example:8000", "localhost.evil.example", "10.0.0.5:8000"])
def test_unknown_host_is_rejected_for_reads_and_writes(client, calls, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 400
    assert client.post("/api/ingest/run", headers={"Host": host}).status_code == 400
    assert calls["ingest"] == 0


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "localhost:5173", "127.0.0.1:8000", "127.0.0.1:5173"])
def test_local_hosts_are_accepted(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_settings_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    assert Settings(_env_file=None).allowed_hosts == ["localhost", "127.0.0.1"]
    monkeypatch.setenv("ALLOWED_HOSTS", '["fxnews.lan"]')
    assert Settings(_env_file=None).allowed_hosts == ["fxnews.lan"]


# ── middleware in isolation (default production settings) ────────────


def make_app(origins=ORIGINS, hosts=("localhost", "127.0.0.1")) -> TestClient:
    mini = FastAPI()
    install_security(mini, allowed_origins=list(origins), allowed_hosts=list(hosts))

    @mini.post("/write")
    def write():
        return {"ok": True}

    @mini.get("/read")
    def read():
        return {"ok": True}

    return TestClient(mini)


def test_same_origin_ui_served_by_the_app_is_allowed():
    c = make_app(hosts=["api.local"])
    assert c.post("/write", headers={"Host": "api.local:8000", "Origin": "http://api.local:8000"}).status_code == 200
    assert c.post("/write", headers={"Host": "api.local:8000", "Origin": "http://evil.example"}).status_code == 403


def test_same_origin_rule_cannot_be_abused_through_dns_rebinding():
    # Attacker page on a rebinding name: Origin == Host, but the Host allow-list rejects it first.
    c = make_app()
    r = c.post("/write", headers={"Host": "rebind.attacker.example:8000", "Origin": "http://rebind.attacker.example:8000"})
    assert r.status_code == 400


def test_wildcard_origin_allows_everything():
    c = make_app(origins=["*"])
    assert c.post("/write", headers={"Host": "localhost", **EVIL}).status_code == 200


def test_default_production_config_blocks_evil_and_allows_local():
    c = make_app()
    assert c.post("/write", headers={"Host": "localhost:8000", **EVIL}).status_code == 403
    assert c.post("/write", headers={"Host": "localhost:8000", "Origin": ORIGINS[0]}).status_code == 200
    assert c.post("/write", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert c.get("/read", headers={"Host": "localhost:8000", **EVIL}).status_code == 200
