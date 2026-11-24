"""Telegram Bot API notifications (one account = one bot + one chat)."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_CHARS = 4096  # Telegram's sendMessage limit


async def send_message(text: str, *, bot_token: str, chat_id: str) -> bool:
    if not bot_token or not chat_id:
        return False
    payload = {
        "chat_id": chat_id,
        "text": text[:MAX_CHARS],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(f"{API}/bot{bot_token}/sendMessage", json=payload)
    except httpx.HTTPError as exc:
        # The URL carries the bot token: log the exception type only, never its message.
        log.warning("Telegram request failed: %s", type(exc).__name__)
        return False
    if res.status_code >= 400:
        log.warning("Telegram HTTP %s: %s", res.status_code, res.text[:200])
        return False
    return True
