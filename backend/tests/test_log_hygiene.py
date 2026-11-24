"""Secrets that live in URLs (Discord webhook token, Twelve Data ?apikey=) must never reach the logs."""

import logging

import httpx
import pytest
import respx

import app.main  # noqa: F401  (importing the app applies the production logging setup)
from app.config import get_settings
from app.notify import discord

WEBHOOK = "https://discord.com/api/webhooks/123456789/SECRET-WEBHOOK-TOKEN-abc"


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setattr(get_settings(), "discord_webhook_url", WEBHOOK)
    with respx.mock(assert_all_called=False) as router:
        router.post(WEBHOOK).mock(return_value=httpx.Response(204))
        yield


def test_httpx_and_httpcore_are_muted_by_the_app():
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


async def test_discord_webhook_token_is_not_logged(webhook, caplog):
    with caplog.at_level(logging.DEBUG):  # root as verbose as it gets; only the app's own logger config applies
        assert await discord.send_text("hello", url=WEBHOOK) is True
    assert "SECRET-WEBHOOK-TOKEN-abc" not in caplog.text
    assert not [r for r in caplog.records if r.name in ("httpx", "httpcore") and r.levelno < logging.WARNING]


async def test_control_unmuted_httpx_would_have_leaked_the_token(webhook, caplog):
    # Proves the test above can fail: with httpx at INFO the full URL (token included) is logged.
    with caplog.at_level(logging.INFO, logger="httpx"):
        await discord.send_text("hello", url=WEBHOOK)
    assert "SECRET-WEBHOOK-TOKEN-abc" in caplog.text
