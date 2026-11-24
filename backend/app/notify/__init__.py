"""Notification fan-out: every enabled account of every channel gets the same message.

The channels and the rules live in backend/notify.yaml (see `app/notify/config.py`).
"""

from __future__ import annotations

import logging

from app.notify import config as notify_config
from app.notify.config import CHANNEL_TYPES, Account, Config
from app.schemas import AnalysisOut, NewsOut

log = logging.getLogger(__name__)


def _targets(cfg: Config, only: str | None = None) -> list[tuple[str, Account]]:
    """The (channel type, account) pairs that get a message (the file's accounts plus the .env ones)."""
    out: list[tuple[str, Account]] = []
    for ctype in CHANNEL_TYPES:
        for account in cfg.all_accounts(ctype):
            if not account.enabled:
                continue
            if only and ctype != only:
                continue
            out.append((ctype, account))
    return out


async def _send_one(ctype: str, account: Account, *, news: NewsOut | None = None, extra_titles: list[str] | None = None,
                    reminder_minutes: int | None = None, analysis: AnalysisOut | None = None,
                    text: str | None = None) -> bool:
    if ctype == "discord":
        from app.notify import discord

        if news is not None:
            return await discord.send_news(news, extra_titles, reminder_minutes, url=account.webhook_url)
        if analysis is not None:
            return await discord.send_analysis(analysis, news, extra_titles, url=account.webhook_url)
        return await discord.send_text(text or "", url=account.webhook_url)
    from app.notify import messages, telegram

    if news is not None:
        body = messages.news_text(news, extra_titles, reminder_minutes)
    elif analysis is not None:
        body = messages.analysis_text(analysis, news, extra_titles)
    else:
        body = text or ""
    return await telegram.send_message(body, bot_token=account.bot_token, chat_id=account.chat_id)


async def send_analysis(analysis: AnalysisOut, news: NewsOut | None = None,
                        extra_titles: list[str] | None = None) -> bool:
    """Push an LLM analysis (trade ideas) to every enabled account."""
    cfg = notify_config.load_safe()
    if cfg is None:
        return False
    sent = False
    for ctype, account in _targets(cfg):
        sent = await _send_one(ctype, account, news=news, extra_titles=extra_titles, analysis=analysis) or sent
    return sent


async def send_news(news: NewsOut, extra_titles: list[str] | None = None,
                    reminder_minutes: int | None = None) -> bool:
    """Push a news alert (no LLM involved) to every enabled account."""
    cfg = notify_config.load_safe()
    if cfg is None:
        return False
    sent = False
    for ctype, account in _targets(cfg):
        sent = await _send_one(ctype, account, news=news, extra_titles=extra_titles,
                               reminder_minutes=reminder_minutes) or sent
    return sent


async def send_text(content: str, only: str | None = None, name: str | None = None) -> dict[str, bool]:
    """Push a plain message. ``only`` = one channel type, ``name`` = one account ("" = every enabled account).

    Returns {account id: sent}, so the UI can show which account answered.
    """
    cfg = notify_config.load_safe()
    if cfg is None:
        return {}
    results: dict[str, bool] = {}
    for ctype, account in _targets(cfg, only):
        if name and account.name != name:
            continue
        ok = await _send_one(ctype, account, text=content)
        results[f"{ctype}:{account.name}"] = ok
    return results
