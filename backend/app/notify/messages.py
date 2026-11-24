"""Message bodies for the notification channels (Discord embeds and Telegram text)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.analysis.rules import recommended_action
from app.schemas import AnalysisOut, NewsOut

IMPACT_ICON = {"High": "🔥", "Medium": "⚡", "Low": "💧"}
BIAS_ICON = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
COLOR = {"High": 0xFF4A4A, "Medium": 0xFFCC44, "Low": 0x6A8A9A}


def _esc(text: str) -> str:
    """Telegram parses a small HTML subset: escape everything that is not ours."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stamp(event_time: datetime | None) -> str:
    if event_time is None:
        return ""
    return event_time.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _facts(news: NewsOut) -> str:
    return " · ".join(f"{k}: {v}" for k, v in (("Forecast", news.forecast), ("Previous", news.previous),
                                              ("Actual", news.actual)) if v)


def _headline(news: NewsOut, extra_titles: list[str] | None, reminder_minutes: int | None) -> str:
    icon = IMPACT_ICON.get(news.impact or "", "📰")
    if reminder_minutes:
        return f"⏰ In {reminder_minutes} min: [{news.currency or '-'}] {news.title}"
    kind = "Calendar release" if news.kind == "calendar" else "New headline"
    return f"{icon} {kind}: [{news.currency or '-'}] {news.title}"


def news_embed(news: NewsOut, extra_titles: list[str] | None = None, reminder_minutes: int | None = None) -> dict:
    """Discord embed for a news alert (no LLM involved)."""
    desc = [_facts(news)]
    if news.event_time is not None:
        desc.append(_stamp(news.event_time))
    if extra_titles:
        desc.append("Same time: " + ", ".join(extra_titles))
    embed = {
        "title": _cut(_headline(news, extra_titles, reminder_minutes), 256),
        "description": _cut("\n".join(d for d in desc if d), 4000),
        "color": COLOR.get(news.impact or "", 0x6A8A9A),
        "footer": {"text": "FXNews"},
    }
    if news.url and news.url.startswith("http"):
        embed["url"] = news.url
    return embed


def news_text(news: NewsOut, extra_titles: list[str] | None = None, reminder_minutes: int | None = None) -> str:
    """Telegram message for a news alert."""
    lines = [f"<b>{_esc(_headline(news, extra_titles, reminder_minutes))}</b>"]
    facts = _facts(news)
    if facts:
        lines.append(_esc(facts))
    if news.event_time is not None:
        lines.append(_esc(f"🕒 {_stamp(news.event_time)}"))
    if extra_titles:
        lines.append(_esc("Same time: " + ", ".join(extra_titles)))
    if news.url and news.url.startswith("http"):
        lines.append(_esc(news.url))
    return "\n".join(lines)


def analysis_text(analysis: AnalysisOut, news: NewsOut | None = None,
                  extra_titles: list[str] | None = None) -> str:
    """Telegram message for an LLM analysis (Discord uses a rich embed instead)."""
    lines = []
    if news is not None:
        lines.append(f"<b>{_esc(_headline(news, extra_titles, None))}</b>")
    else:
        lines.append(f"<b>🤖 Advice {_esc(analysis.symbol or '')}</b>")
    if analysis.summary:
        lines.append(_esc(analysis.summary))
    if news is not None:
        facts = _facts(news)
        if facts:
            lines.append(_esc(facts))
        if news.event_time is not None:
            lines.append(_esc(f"🕒 {_stamp(news.event_time)}"))
    for t in analysis.trades[:6]:
        head = f"{BIAS_ICON.get(t.bias, '⚪')} {_esc(t.symbol)} — {_esc(recommended_action(t.bias, t.confidence))} ({t.confidence}%)"
        detail = " / ".join(x for x in (t.entry_idea, t.invalidation, t.targets, t.horizon) if x)
        lines.append(_esc(f"{head}\n{detail}" if detail else head))
    for s in analysis.scenarios[:4]:
        lines.append(_esc(f"• If {s.condition} → {s.outcome}"))
    if analysis.risk_notes:
        lines.append(_esc(f"⚠️ Risks: {analysis.risk_notes}"))
    lines.append(_esc(f"FXNews · {analysis.model}"))
    return "\n".join(lines)
