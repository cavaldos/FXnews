"""Notifications: the YAML config (backend/notify.yaml), the two alert rules (high-impact news, pre-event
reminder) and the Discord / Telegram accounts."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from fastapi.testclient import TestClient

from app.notify import config as notify_config
from app.services import ingest
from app.sources.base import RawNews

DISCORD = "https://discord.test/webhook/123/TOKEN"
TG_VALUE = "abc123"
TG_OTHER = "xyz789"
TELEGRAM = f"https://api.telegram.org/bot{TG_VALUE}/sendMessage"


@pytest.fixture(autouse=True)
def _clean():
    """The config file and the news live in shared state: wipe them so no test leaks into the next."""
    from tests.support import wipe_db

    wipe_db()
    path = notify_config.config_path()
    if path.exists():
        path.unlink()
    yield
    if path.exists():
        path.unlink()


def write_config(**kw) -> None:
    """Write backend/notify.yaml directly (like a hand edit would)."""
    data = {
        "min_impact": "High",
        "pre_event_lead_minutes": 10,
        "notify_analyses": True,
        "notify_pre_event": True,
        "min_confidence": 60,
        "channels": {
            "discord": {"accounts": [{"name": "main", "enabled": True, "webhook_url": DISCORD}]},
            "telegram": {"accounts": [{"name": "main", "enabled": True, "bot_token": TG_VALUE, "chat_id": "42"}]},
        },
    }
    data.update(kw)
    notify_config.config_path().write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def mock_channels(discord_status: int = 204):
    """Both channels, mocked. Returns (discord_route, telegram_route)."""
    return (
        respx.post(DISCORD).mock(return_value=httpx.Response(discord_status)),
        respx.post(TELEGRAM).mock(return_value=httpx.Response(200, json={"ok": True})),
    )


def headline(title: str = "Fed raises rates") -> RawNews:
    return RawNews(kind="headline", source="fxstreet", title=title, url="https://x.test/1",
                   event_time=datetime.now(UTC))


def calendar(title: str = "CPI m/m", minutes: int = 5) -> RawNews:
    return RawNews(kind="calendar", source="forexfactory", title=title, currency="USD", impact="High",
                   event_time=datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=minutes))


# ── the YAML file ─────────────────────────────────────────────────────


def test_the_file_is_read_and_the_env_accounts_are_added(monkeypatch):
    write_config()
    cfg = notify_config.load()
    assert cfg.min_impact == "High" and cfg.pre_event_lead_minutes == 10
    assert [a.name for a in cfg.accounts("discord")] == ["main"]
    assert cfg.accounts("telegram")[0].chat_id == "42"

    monkeypatch.setattr(notify_config.get_settings(), "discord_webhook_url", "https://discord.test/env")
    cfg = notify_config.load()
    assert [a.name for a in cfg.accounts("discord")] == ["main"]  # the file is untouched
    assert [a.name for a in cfg.env_channels["discord"]] == ["env"]
    assert [a.name for a in cfg.all_accounts("discord")] == ["main", "env"]


def test_the_env_account_is_not_written_to_the_file(monkeypatch):
    monkeypatch.setattr(notify_config.get_settings(), "discord_webhook_url", "https://discord.test/env")
    write_config()
    client = TestClient(app_client())
    r = client.post("/api/notify/settings", json={"accounts": [
        {"channel": "discord", "name": "main", "enabled": True},
        {"channel": "discord", "name": "env", "enabled": True, "source": "env"},
    ]})
    assert r.status_code == 200
    written = yaml.safe_load(notify_config.config_path().read_text(encoding="utf-8"))
    assert [a["name"] for a in written["channels"]["discord"]["accounts"]] == ["main"]
    assert written["channels"]["discord"]["accounts"][0]["webhook_url"] == DISCORD


def test_renaming_an_account_keeps_its_secret():
    write_config()
    client = TestClient(app_client())
    r = client.post("/api/notify/settings", json={"accounts": [
        {"id": "discord:main", "channel": "discord", "name": "renamed", "enabled": True},
    ]})
    assert r.status_code == 200
    written = yaml.safe_load(notify_config.config_path().read_text(encoding="utf-8"))
    account = written["channels"]["discord"]["accounts"][0]
    assert account["name"] == "renamed" and account["webhook_url"] == DISCORD


def test_a_broken_file_is_reported_not_ignored():
    notify_config.config_path().write_text("min_impact: [unclosed\n", encoding="utf-8")
    with pytest.raises(notify_config.ConfigError):
        notify_config.load()
    assert notify_config.load_safe() is None


def test_the_api_writes_the_file_and_never_returns_the_secrets():
    client = TestClient(app_client())
    r = client.post("/api/notify/settings", json={
        "rules": {"pre_event_lead_minutes": 15, "min_impact": "Medium"},
        "accounts": [
            {"channel": "discord", "name": "main", "enabled": True, "webhook_url": "https://discord.test/new"},
            {"channel": "telegram", "name": "main", "enabled": True, "bot_token": TG_OTHER, "chat_id": "42"},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["rules"]["pre_event_lead_minutes"] == 15 and body["rules"]["min_impact"] == "Medium"
    assert TG_OTHER not in r.text and "discord.test/new" not in r.text
    assert body["accounts"][0]["secret_set"] is True

    written = yaml.safe_load(notify_config.config_path().read_text(encoding="utf-8"))
    assert written["pre_event_lead_minutes"] == 15 and written["min_impact"] == "Medium"
    assert written["channels"]["discord"]["accounts"][0]["webhook_url"] == "https://discord.test/new"
    assert written["channels"]["telegram"]["accounts"][0]["bot_token"] == TG_OTHER


def test_a_secret_that_is_not_sent_keeps_its_value():
    write_config()
    client = TestClient(app_client())
    r = client.post("/api/notify/settings", json={
        "accounts": [
            {"channel": "discord", "name": "main", "enabled": True},
            {"channel": "telegram", "name": "main", "enabled": True, "bot_token": TG_VALUE, "chat_id": "42"},
        ],
    })
    assert r.status_code == 200
    written = yaml.safe_load(notify_config.config_path().read_text(encoding="utf-8"))
    assert written["channels"]["discord"]["accounts"][0]["webhook_url"] == DISCORD


def app_client():
    from app.main import app

    return app


# ── rule 1: a high-impact headline is announced on arrival ────────────


@respx.mock
async def test_high_impact_headline_reaches_every_enabled_account():
    write_config()
    discord, telegram = mock_channels()

    (nid,), _ = ingest.store_items([headline()])
    assert await ingest.notify_new_items([nid]) == 1
    assert discord.called and telegram.called
    assert "Fed raises rates" in discord.calls[0].request.content.decode()


@respx.mock
async def test_a_disabled_account_is_skipped():
    write_config(channels={"discord": {"accounts": [
        {"name": "main", "enabled": True, "webhook_url": DISCORD},
        {"name": "off", "enabled": False, "webhook_url": "https://discord.test/off"},
    ]}})
    discord, telegram = mock_channels()

    (nid,), _ = ingest.store_items([headline()])
    assert await ingest.notify_new_items([nid]) == 1
    assert discord.call_count == 1
    assert not telegram.called


@respx.mock
async def test_low_impact_headline_is_not_announced():
    write_config()
    discord, telegram = mock_channels()

    (nid,), _ = ingest.store_items([headline("Gold consolidates in a quiet session")])
    assert await ingest.notify_new_items([nid]) == 0
    assert not discord.called and not telegram.called


@respx.mock
async def test_threshold_decides_what_is_announced():
    write_config(min_impact="Low")
    discord, _ = mock_channels()

    (nid,), _ = ingest.store_items([headline("Gold consolidates in a quiet session")])
    assert await ingest.notify_new_items([nid]) == 1
    assert discord.called


@respx.mock
async def test_nothing_is_sent_when_no_account_is_enabled():
    write_config(channels={"discord": {"accounts": [{"name": "main", "enabled": False, "webhook_url": DISCORD}]}})
    discord, telegram = mock_channels()

    (nid,), _ = ingest.store_items([headline()])
    assert await ingest.notify_new_items([nid]) == 0
    assert not discord.called and not telegram.called


# ── rule 2: a reminder before a high-impact release ────────────────────


@respx.mock
async def test_release_is_announced_once_inside_the_lead_window():
    write_config(pre_event_lead_minutes=10)
    discord, _ = mock_channels()

    ingest.store_items([calendar(minutes=5)])
    assert await ingest.run_pre_event_notify_cycle() == 1
    assert discord.call_count == 1
    assert "In 10 min" in discord.calls[0].request.content.decode()

    assert await ingest.run_pre_event_notify_cycle() == 0  # already announced
    assert discord.call_count == 1


@respx.mock
async def test_release_outside_the_lead_window_is_not_announced():
    write_config(pre_event_lead_minutes=10)
    discord, _ = mock_channels()

    ingest.store_items([calendar(minutes=60)])
    assert await ingest.run_pre_event_notify_cycle() == 0
    assert not discord.called


@respx.mock
async def test_lead_zero_turns_the_reminder_off():
    write_config(pre_event_lead_minutes=0)
    discord, _ = mock_channels()

    ingest.store_items([calendar(minutes=5)])
    assert await ingest.run_pre_event_notify_cycle() == 0
    assert not discord.called


@respx.mock
async def test_low_impact_release_is_not_announced():
    write_config(pre_event_lead_minutes=10)
    discord, _ = mock_channels()

    ingest.store_items([RawNews(kind="calendar", source="forexfactory", title="Some speech", currency="USD",
                                impact="Low", event_time=datetime.now(UTC) + timedelta(minutes=5))])
    assert await ingest.run_pre_event_notify_cycle() == 0
    assert not discord.called


# ── channels ──────────────────────────────────────────────────────────


@respx.mock
async def test_telegram_message_is_escaped():
    write_config(channels={"telegram": {"accounts": [
        {"name": "main", "enabled": True, "bot_token": TG_VALUE, "chat_id": "42"}]}})
    _, telegram = mock_channels()

    (nid,), _ = ingest.store_items([headline("Fed <b>raises</b> rates & cuts")])
    assert await ingest.notify_new_items([nid]) == 1
    body = telegram.calls[0].request.content.decode()
    assert "&lt;b&gt;" in body and "&amp;" in body


@respx.mock
async def test_a_failing_account_does_not_break_the_other_one():
    write_config()
    discord, telegram = mock_channels(discord_status=500)

    (nid,), _ = ingest.store_items([headline()])
    assert await ingest.notify_new_items([nid]) == 1
    assert telegram.called


@respx.mock
async def test_the_test_endpoint_reports_every_account():
    write_config()
    discord, telegram = mock_channels()
    client = TestClient(app_client())

    r = client.post("/api/notify/test")
    assert r.status_code == 200
    assert r.json()["accounts"] == {"discord:main": True, "telegram:main": True}
    assert discord.called and telegram.called


@respx.mock
async def test_the_test_endpoint_can_test_one_account():
    write_config()
    discord, telegram = mock_channels()
    client = TestClient(app_client())

    r = client.post("/api/notify/test", json={"channel": "telegram", "name": "main"})
    assert r.status_code == 200
    assert r.json()["accounts"] == {"telegram:main": True}
    assert telegram.called and not discord.called


def test_the_test_endpoint_needs_an_account():
    client = TestClient(app_client())
    assert client.post("/api/notify/test").status_code == 400


def test_the_config_path_is_the_yaml_file():
    assert Path(notify_config.config_path()).name == "notify.yaml"
