"""Discord webhook notifications (no-op when DISCORD_WEBHOOK_URL is empty)."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.analysis.rules import recommended_action
from app.config import get_settings
from app.schemas import AnalysisOut, NewsOut

log = logging.getLogger(__name__)

COLORS = {"bullish": 0x2AFF8A, "bearish": 0xFF4A4A, "neutral": 0x6A8A9A, "pre_event": 0xFFCC44}
BIAS_ICON = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
IMPACT_ICON = {"High": "🔥", "Medium": "⚡", "Low": "💧"}


EMBED_MAX_CHARS = 6000  # Discord: title + description + footer + every field name/value, per message
_MIN_PART = 80  # fit_embed never shrinks a description / field value below this while it can drop fields


def _cut(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _units(text: str) -> int:
    """Length as Discord may count it (UTF-16 code units): the safe reading for emoji, which are 2 units."""
    return len(text.encode("utf-16-le")) // 2


def _cut_units(text: str, limit: int) -> str:
    if _units(text) <= limit:
        return text
    if limit <= 0:
        return ""
    out = text[: max(limit - 1, 0)]
    while out and _units(out) > limit - 1:
        out = out[:-1]
    return out.rstrip() + "…"


def embed_size(embed: dict) -> int:
    total = _units(embed.get("title") or "") + _units(embed.get("description") or "")
    total += _units((embed.get("footer") or {}).get("text") or "")
    total += _units((embed.get("author") or {}).get("name") or "")
    for field in embed.get("fields") or []:
        total += _units(field.get("name") or "") + _units(field.get("value") or "")
    return total


def fit_embed(embed: dict, limit: int = EMBED_MAX_CHARS) -> dict:
    """Shrink ``embed`` in place until its total size is <= ``limit`` (Discord answers 400 above 6000, and the
    alert would be lost). Order: trim the longest description/field value, then drop trailing fields, then
    trim the description. The title (<=256) and footer (<=2048) are never needed to reach the limit."""
    over = embed_size(embed) - limit
    fields = embed.get("fields") or []
    while over > 0:  # 1. cut the single longest free-text part by what is missing (never below _MIN_PART)
        parts = [(_units(embed.get("description") or ""), None)] + [(_units(f.get("value") or ""), f) for f in fields]
        size, holder = max(parts, key=lambda p: p[0])
        if size <= _MIN_PART:
            break
        target = max(_MIN_PART, size - over)
        if holder is None:
            embed["description"] = _cut_units(embed["description"], target)
        else:
            holder["value"] = _cut_units(holder["value"], target)
        over = embed_size(embed) - limit
    while over > 0 and fields:  # 2. still too big: drop the last field (risk notes, then scenarios, then trades)
        fields.pop()
        over = embed_size(embed) - limit
    if over > 0:  # 3. last resort: the description (title + footer alone are < 2.4k)
        embed["description"] = _cut_units(embed.get("description") or "", _units(embed.get("description") or "") - over)
    return embed


def build_embed(analysis: AnalysisOut, news: NewsOut | None = None, extra_titles: list[str] | None = None) -> dict:
    trades = analysis.trades
    lead = next((t for t in trades if t.bias != "neutral"), trades[0] if trades else None)
    color = COLORS["pre_event"] if analysis.kind == "pre_event" else COLORS[lead.bias if lead else "neutral"]

    if news is not None:
        icon = IMPACT_ICON.get(news.impact or "", "📰")
        prefix = {"pre_event": "Upcoming release", "headline": "New headline"}.get(analysis.kind, "News")
        title = f"{icon} {prefix}: [{news.currency or '-'}] {news.title}"
    else:
        title = f"🤖 Advice {analysis.symbol}"

    desc = [analysis.summary]
    if extra_titles:
        desc.append("Same time: " + ", ".join(extra_titles))
    if news is not None and news.kind == "calendar":
        desc.append(" · ".join(f"{k}: {v}" for k, v in (("Forecast", news.forecast), ("Previous", news.previous),
                                                         ("Actual", news.actual)) if v))
    if news is not None and news.event_time is not None:
        desc.append(f"<t:{int(news.event_time.timestamp())}:F> (<t:{int(news.event_time.timestamp())}:R>)")

    fields = []
    for t in trades[:6]:
        lines = [x for x in (
            f"Entry: {t.entry_idea}" if t.entry_idea else None,
            f"SL/Invalid: {t.invalidation}" if t.invalidation else None,
            f"Target: {t.targets}" if t.targets else None,
            f"Horizon: {t.horizon}" if t.horizon else None,
        ) if x]
        fields.append({
            "name": _cut(f"{BIAS_ICON[t.bias]} {t.symbol} — {recommended_action(t.bias, t.confidence)} ({t.confidence}%)", 256),
            "value": _cut("\n".join(lines) or "—", 1024),
            "inline": False,
        })
    for s in analysis.scenarios[:4]:
        fields.append({"name": _cut(f"If {s.condition}", 256), "value": _cut(s.outcome, 1024), "inline": False})
    if analysis.risk_notes:
        fields.append({"name": "⚠️ Risks", "value": _cut(analysis.risk_notes, 1024), "inline": False})

    embed = {
        "title": _cut(title, 256),
        "description": _cut("\n\n".join(d for d in desc if d), 4000),
        "color": color,
        "fields": fields[:25],
        "footer": {"text": _cut(f"FXNews · {analysis.model}" + (f" · {analysis.error}" if analysis.error else ""), 2048)},
        "timestamp": analysis.created_at.isoformat(),
    }
    if news is not None and news.url and news.url.startswith("http"):
        embed["url"] = news.url
    return fit_embed(embed)


async def _post(payload: dict) -> bool:
    url = get_settings().discord_webhook_url
    if not url:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(3):
            res = await client.post(url, json=payload)
            if res.status_code == 429:
                try:
                    retry = float(res.json().get("retry_after", 1))
                except ValueError:
                    retry = 1.0
                await asyncio.sleep(min(retry, 10))
                continue
            if res.status_code >= 400:
                log.warning("Discord webhook HTTP %s: %s", res.status_code, res.text[:200])
                return False
            return True
    return False


async def send_analysis(analysis: AnalysisOut, news: NewsOut | None = None, extra_titles: list[str] | None = None) -> bool:
    return await _post({"username": "FXNews", "embeds": [build_embed(analysis, news, extra_titles)]})


async def send_text(content: str) -> bool:
    return await _post({"username": "FXNews", "content": _cut(content, 2000)})
