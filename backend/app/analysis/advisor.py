"""News/price context -> OpenRouter LLM -> stored Analysis + Signals -> SSE + Discord."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.analysis import llm, rules
from app.analysis.mapping import symbol_sign
from app.config import get_settings
from app.db import session_scope, utcnow
from app.events import broadcaster
from app.market.symbols import SYMBOLS, get_symbol
from app.models import Analysis, NewsItem, Signal
from app.schemas import AnalysisOut, CurrencyImpact, Scenario, TradeIdea

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a news-trading Forex expert working for an individual trader.
Task: read the news / economic calendar + price context and give practical, concise trade assessments.

Return ONLY a single JSON object (no markdown, no text outside the JSON) following this schema:
{
  "summary": "2-4 sentences in English: what the news says, how it compares to expectations, the main impact",
  "currency_impact": [{"currency": "USD", "direction": "stronger|weaker|neutral", "strength": 0.0}],
  "trades": [{"symbol": "EURUSD", "bias": "bullish|bearish|neutral", "confidence": 0,
              "horizon": "scalp|intraday|swing", "entry_idea": "...", "invalidation": "...", "targets": "..."}],
  "scenarios": [{"condition": "...", "outcome": "..."}],
  "risk_notes": "..."
}

Rules:
- Only use symbols from the ALLOWED_SYMBOLS list.
- "bias" is the direction of the symbol's PRICE (bullish = symbol price rises). A currency that strengthens in the quote position pushes the pair price down
  (USD strong => EURUSD down, USDJPY up, XAUUSD down). Use the provided SYMBOL_SIGNS table.
- "confidence" (0-100) reflects true certainty. Ambiguous, already priced-in or conflicting news => neutral or < 50.
- Unreleased events (pre_event): do NOT invent actual figures. trades show the bias before the release (usually low);
  the focus is scenarios: actual > forecast, actual < forecast, in line with forecast - for each scenario state which pairs and which direction.
- entry_idea / invalidation / targets are based on PRICE_CONTEXT (nearby high/low, ATR); state concrete price levels when possible.
- Always mention spread widening / slippage risk around the release time in risk_notes when relevant.
- Write all content in English, keeping standard trading terminology."""


# ── context helpers ──────────────────────────────────────────────────


def _currency_filter(currencies: list[str]):
    return or_(*[NewsItem.currencies_csv.like(f"%,{c},%") for c in currencies]) if currencies else None


def _news_line(n: NewsItem) -> str:
    when = n.event_time.strftime("%Y-%m-%d %H:%M UTC") if n.event_time else "?"
    if n.kind == "calendar":
        parts = [f"[{when}] {n.currency} {n.title} (impact {n.impact})"]
        for label, val in (("actual", n.actual), ("forecast", n.forecast), ("previous", n.previous)):
            if val:
                parts.append(f"{label}={val}")
        return " ".join(parts)
    summary = f" — {n.summary[:220]}" if n.summary else ""
    return f"[{when}] ({n.source}) {n.title}{summary}"


def related_headlines(session: Session, currencies: list[str], hours: int = 12, limit: int = 8,
                      exclude_id: int | None = None) -> list[str]:
    cond = _currency_filter(currencies)
    if cond is None:
        return []
    stmt = (select(NewsItem).where(NewsItem.kind == "headline", cond,
                                   NewsItem.event_time >= utcnow() - timedelta(hours=hours))
            .order_by(NewsItem.event_time.desc()).limit(limit + 1))
    return [_news_line(n) for n in session.scalars(stmt) if n.id != exclude_id][:limit]


def calendar_window(session: Session, currencies: list[str], *, hours_back: int = 0, hours_ahead: int = 24,
                    min_impact: tuple[str, ...] = ("High", "Medium"), limit: int = 10) -> list[str]:
    """Nearest events on both sides of now: a busy past day must not crowd out upcoming releases."""
    cond = _currency_filter(currencies)
    if cond is None:
        return []
    now = utcnow()
    base = select(NewsItem).where(NewsItem.kind == "calendar", cond, NewsItem.impact.in_(min_impact))
    upcoming = session.scalars(base.where(NewsItem.event_time > now,
                                          NewsItem.event_time <= now + timedelta(hours=hours_ahead))
                               .order_by(NewsItem.event_time).limit(limit)).all()
    past = session.scalars(base.where(NewsItem.event_time <= now,
                                      NewsItem.event_time >= now - timedelta(hours=hours_back))
                           .order_by(NewsItem.event_time.desc()).limit(limit)).all() if hours_back else []
    n_up = min(len(upcoming), max(limit - len(past), (limit + 1) // 2))
    chosen = upcoming[:n_up] + past[: limit - n_up]
    return [_news_line(n) for n in sorted(chosen, key=lambda n: n.event_time)]


async def price_context(symbol: str, interval: str = "15m", bars: int = 96) -> dict | None:
    try:
        from app.market.candles import get_candles
    except ImportError:  # market module not available yet
        return None
    try:
        data = await get_candles(symbol, interval)
    except Exception as exc:  # noqa: BLE001 - price context is best-effort
        log.warning("price context for %s failed: %s", symbol, exc)
        return None
    candles = data.candles[-bars:]
    if len(candles) < 5:
        return None
    digits = SYMBOLS[symbol].digits if symbol in SYMBOLS else 5
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(candles, candles[1:])]
    atr = sum(trs[-14:]) / len(trs[-14:])
    last, first = candles[-1].close, candles[0].close
    return {
        "symbol": symbol,
        "interval": interval,
        "source": data.source,
        "stale": data.source.endswith("-stale"),  # cached copy served while providers are down
        "bars": len(candles),
        "last_bar_utc": datetime.fromtimestamp(candles[-1].time, UTC).strftime("%Y-%m-%d %H:%M"),
        "last": round(last, digits),
        "window_high": round(max(c.high for c in candles), digits),
        "window_low": round(min(c.low for c in candles), digits),
        "high_last_24_bars": round(max(c.high for c in candles[-24:]), digits),
        "low_last_24_bars": round(min(c.low for c in candles[-24:]), digits),
        "atr14": round(atr, digits),
        "window_change_pct": round((last - first) / first * 100, 3) if first else None,
        "last_12_closes": [round(c.close, digits) for c in candles[-12:]],
    }


def _sign_table(symbols: list[str], currencies: list[str]) -> list[str]:
    rows = []
    for sym in symbols:
        for cur in currencies:
            sign = symbol_sign(sym, cur)
            if sign:
                rows.append(f"{cur} strengthens => {sym} {'UP' if sign > 0 else 'DOWN'}")
    return rows


# ── LLM output coercion ──────────────────────────────────────────────


def _norm_bias(value) -> str:
    v = str(value or "").strip().lower()
    if v in {"bullish", "buy", "long", "up"}:
        return "bullish"
    if v in {"bearish", "sell", "short", "down"}:
        return "bearish"
    return "neutral"


def _text(value, limit: int = 1000) -> str | None:
    """Models sometimes return lists/dicts for free-text fields; flatten instead of failing."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value if v not in (None, ""))
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).strip()[:limit] or None


def _confidence(value) -> int:
    """Accept 72, "72", "72%", 0.72 (fraction) -> 0..100."""
    try:
        c = float(str(value).strip().rstrip("%")) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0
    if c != c or c in (float("inf"), float("-inf")):  # NaN / inf
        return 0
    if 0 < c <= 1 and not (isinstance(value, str) and value.strip().endswith("%")):
        c *= 100
    return max(0, min(100, int(round(c))))


def coerce_llm_output(data: dict, allowed_symbols: list[str]) -> dict:
    allowed = {s.upper() for s in allowed_symbols}
    trades: list[TradeIdea] = []
    for t in data.get("trades") or []:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("symbol") or "").upper().replace("/", "").replace(" ", "")
        if sym not in allowed or any(x.symbol == sym for x in trades):
            continue
        trades.append(TradeIdea(
            symbol=sym, bias=_norm_bias(t.get("bias")), confidence=_confidence(t.get("confidence")),
            horizon=_text(t.get("horizon"), 32), entry_idea=_text(t.get("entry_idea")),
            invalidation=_text(t.get("invalidation")), targets=_text(t.get("targets")),
        ))

    impacts: list[CurrencyImpact] = []
    for c in data.get("currency_impact") or []:
        if not isinstance(c, dict):
            continue
        direction = str(c.get("direction") or "neutral").lower()
        try:
            impacts.append(CurrencyImpact(
                currency=str(c.get("currency") or "").upper()[:4],
                direction=direction if direction in {"stronger", "weaker"} else "neutral",
                strength=max(0.0, min(1.0, float(c.get("strength") or 0))),
            ))
        except (ValidationError, TypeError, ValueError):
            continue

    scenarios: list[Scenario] = []
    for s in data.get("scenarios") or []:
        if isinstance(s, dict):
            cond, out = _text(s.get("condition") or s.get("if")), _text(s.get("outcome") or s.get("then"))
            if cond and out:
                scenarios.append(Scenario(condition=cond, outcome=out))

    return {
        "summary": _text(data.get("summary"), 3000) or "",
        "currency_impact": [c.model_dump() for c in impacts],
        "trades": [t.model_dump() for t in trades],
        "scenarios": [s.model_dump() for s in scenarios],
        "risk_notes": _text(data.get("risk_notes"), 2000),
    }


# ── persistence / fan-out ────────────────────────────────────────────


def _save(session: Session, *, kind: str, news: list[NewsItem], symbol: str | None, model: str, parsed: dict,
          question: str | None, error: str | None, latency_ms: int | None, raw_response: str | None,
          source: str) -> Analysis:
    analysis = Analysis(kind=kind, news_id=news[0].id if news else None, symbol=symbol, model=model,
                        question=question, error=error, latency_ms=latency_ms, raw_response=raw_response, **parsed)
    session.add(analysis)
    session.flush()
    for t in parsed["trades"]:
        session.add(Signal(analysis_id=analysis.id, news_id=analysis.news_id, symbol=t["symbol"], bias=t["bias"],
                           confidence=t["confidence"], source=source, model=model, horizon=t.get("horizon"),
                           entry_idea=t.get("entry_idea"), invalidation=t.get("invalidation"),
                           title=news[0].title if news else (question or f"Advice {symbol}")))
    for n in news:
        # A failed (rules-fallback) run must not replace a good LLM analysis already linked to the news.
        if error is None or n.analysis_id is None:
            n.analysis_id = analysis.id
    session.flush()
    return analysis


def _should_notify(kind: str, parsed: dict, error: str | None) -> bool:
    settings = get_settings()
    if error:  # don't push "LLM unavailable" noise to Discord; the UI still shows it
        return False
    if kind == "pre_event":
        return settings.notify_pre_event
    if kind == "headline":
        return any(t["bias"] != "neutral" and t["confidence"] >= settings.notify_min_confidence for t in parsed["trades"])
    return False


_notify_tasks: set[asyncio.Task] = set()


async def _notify_discord(out: AnalysisOut, news_out, extra_titles: list[str]) -> None:
    from app.notify import discord  # local import keeps notify optional during tests

    try:
        sent = await discord.send_analysis(out, news_out, extra_titles=extra_titles)
    except Exception as exc:  # noqa: BLE001 - never fail an analysis because Discord is down
        log.warning("Discord notify failed: %s", exc)
        return
    if sent:
        with session_scope() as session:
            session.get(Analysis, out.id).notified = True
        broadcaster.publish("analysis", out.model_copy(update={"notified": True}))


async def _finish(analysis_id: int, kind: str, parsed: dict, news_titles: list[str], notify: bool | None) -> AnalysisOut:
    from app.services.serializers import news_out as to_news_out

    with session_scope() as session:
        row = session.get(Analysis, analysis_id)
        out = AnalysisOut.model_validate(row)
        news = session.get(NewsItem, row.news_id) if row.news_id else None
        news_out = to_news_out(news) if news is not None else None

    # UI first; Discord (slow, may back off on 429) runs in the background.
    broadcaster.publish("analysis", out)
    want = _should_notify(kind, parsed, out.error) if notify is None else notify
    if want and get_settings().discord_enabled:
        task = asyncio.create_task(_notify_discord(out, news_out, news_titles[1:]))
        _notify_tasks.add(task)
        task.add_done_callback(_notify_tasks.discard)
    return out


async def drain_notifications(timeout: float = 5.0) -> None:
    if _notify_tasks:
        await asyncio.wait(list(_notify_tasks), timeout=timeout)


# ── public API ───────────────────────────────────────────────────────


_news_locks: dict[int, asyncio.Lock] = {}
REUSE_RECENT_SECONDS = 120


async def analyze_news(news_id: int, *, model: str | None = None, kind: str | None = None,
                       notify: bool | None = None) -> AnalysisOut:
    """Serialised per news item: a double-click or auto+manual overlap reuses the fresh result
    instead of paying for a second LLM call (pass `model` to force a new run with another model)."""
    if len(_news_locks) > 500:  # prune idle locks (an unlocked lock has no waiters)
        for key in [k for k, lk in _news_locks.items() if not lk.locked()]:
            del _news_locks[key]
    lock = _news_locks.setdefault(news_id, asyncio.Lock())
    async with lock:
        if model is None:
            with session_scope() as session:
                news = session.get(NewsItem, news_id)
                recent = session.get(Analysis, news.analysis_id) if news is not None and news.analysis_id else None
                if (recent is not None and recent.error is None
                        and (kind is None or recent.kind == kind)
                        and recent.created_at >= utcnow() - timedelta(seconds=REUSE_RECENT_SECONDS)):
                    return AnalysisOut.model_validate(recent)
        return await _analyze_news(news_id, model=model, kind=kind, notify=notify)


async def _analyze_news(news_id: int, *, model: str | None, kind: str | None, notify: bool | None) -> AnalysisOut:
    settings = get_settings()
    with session_scope() as session:
        news = session.get(NewsItem, news_id)
        if news is None:
            raise LookupError(f"news {news_id} not found")
        now = utcnow()
        if kind is None:
            kind = "pre_event" if news.kind == "calendar" and news.event_time and news.event_time > now else "headline"

        group = [news]
        if news.kind == "calendar" and news.event_time:
            # Events released together (CPI m/m + Core CPI m/m at 08:30) are analysed as one.
            group += list(session.scalars(select(NewsItem).where(
                NewsItem.kind == "calendar", NewsItem.currency == news.currency,
                NewsItem.event_time == news.event_time, NewsItem.id != news.id)))

        currencies, symbols = news.currencies, news.symbols
        context = {
            "analysis_kind": kind,
            "now_utc": now.strftime("%Y-%m-%d %H:%M"),
            "news": [_news_line(n) for n in group],
            "news_url": news.url,
            "rule_prior_primary_symbol": {"symbol": symbols[0] if symbols else None, "bias": news.rule_bias},
            "related_headlines_12h": related_headlines(session, currencies, exclude_id=news.id),
            "calendar_last24h_next24h": calendar_window(session, currencies, hours_back=24, hours_ahead=24),
        }
        titles = [n.title for n in group]
        impact, primary_currency = news.impact, (currencies[0] if currencies else None)

    # Unmapped headline (manual "analyze"): let the model pick among the most liquid markets.
    allowed = symbols or ["EURUSD", "USDJPY", "XAUUSD", "US500"]
    context["ALLOWED_SYMBOLS"] = allowed
    context["SYMBOL_SIGNS"] = _sign_table(allowed, currencies)
    context["PRICE_CONTEXT"] = [pc for sym in allowed[:3] if (pc := await price_context(sym))]

    model_id = settings.model_for(allowed[0], model)
    user_msg = "Analyze the news below and return JSON following the schema.\n" + json.dumps(context, ensure_ascii=False, indent=1)
    parsed, error, latency, raw, source = await _run_llm(model_id, user_msg, allowed)
    if parsed is None:
        prior = context["rule_prior_primary_symbol"]["bias"] or "neutral"
        parsed = {
            "summary": f"LLM unavailable ({error}). Rule-based result for: {titles[0]}",
            "currency_impact": [], "scenarios": [], "risk_notes": None,
            "trades": [t.model_dump() for t in rules.fallback_trades(allowed, primary_currency, prior, impact)],
        }
        model_id = "rules"

    with session_scope() as session:
        group_rows = [session.get(NewsItem, n_id) for n_id in [news_id] + [g.id for g in group[1:]]]
        analysis = _save(session, kind=kind, news=group_rows, symbol=None, model=model_id, parsed=parsed,
                         question=None, error=error, latency_ms=latency, raw_response=raw, source=source)
        analysis_id = analysis.id
    return await _finish(analysis_id, kind, parsed, titles, notify)


async def advise(symbol: str, *, question: str | None = None, model: str | None = None,
                 interval: str = "15m") -> AnalysisOut:
    settings = get_settings()
    info = get_symbol(symbol)
    if info is None:
        raise ValueError(f"Unsupported symbol {symbol}")
    currencies = [c for c in (info.base, info.quote) if c]
    question = (question or "").strip() or (
        f"Outlook and trade plan for {info.symbol} for the next session, based on news, the economic calendar and price.")

    with session_scope() as session:
        recent = session.scalars(select(Analysis).where(
            or_(Analysis.symbol == info.symbol, Analysis.id.in_(
                select(Signal.analysis_id).where(Signal.symbol == info.symbol))))
            .order_by(Analysis.created_at.desc()).limit(4)).all()
        context = {
            "analysis_kind": "ad_hoc",
            "now_utc": utcnow().strftime("%Y-%m-%d %H:%M"),
            "QUESTION": question,
            "ALLOWED_SYMBOLS": [info.symbol],
            "SYMBOL_SIGNS": _sign_table([info.symbol], currencies),
            "headlines_24h": related_headlines(session, currencies, hours=24, limit=12),
            "calendar_last24h_next48h": calendar_window(session, currencies, hours_back=24, hours_ahead=48, limit=14),
            "previous_analyses": [{"at": a.created_at.strftime("%Y-%m-%d %H:%M"), "summary": a.summary[:300],
                                   "trades": [t for t in a.trades if t.get("symbol") == info.symbol]} for a in recent],
        }

    context["PRICE_CONTEXT"] = [pc for pc in (await price_context(info.symbol, interval, 120),
                                              await price_context(info.symbol, "1h", 72)) if pc]
    model_id = settings.model_for(info.symbol, model)
    user_msg = ("Answer the trader's question and return JSON following the schema (the main answer goes in summary).\n"
                + json.dumps(context, ensure_ascii=False, indent=1))
    parsed, error, latency, raw, source = await _run_llm(model_id, user_msg, [info.symbol])
    if parsed is None:
        parsed = {"summary": f"LLM unavailable: {error}", "currency_impact": [], "trades": [],
                  "scenarios": [], "risk_notes": None}
        model_id = "rules"

    with session_scope() as session:
        analysis = _save(session, kind="ad_hoc", news=[], symbol=info.symbol, model=model_id, parsed=parsed,
                         question=question, error=error, latency_ms=latency, raw_response=raw, source=source)
        analysis_id = analysis.id
    return await _finish(analysis_id, "ad_hoc", parsed, [], notify=False)


async def _run_llm(model_id: str, user_msg: str, allowed: list[str]):
    """Returns (parsed|None, error|None, latency_ms|None, raw_text|None, signal_source)."""
    if not get_settings().llm_enabled:
        return None, "OPENROUTER_API_KEY is not configured", None, None, "rules"
    try:
        result = await llm.chat_json([{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": user_msg}], model_id)
    except llm.LLMError as exc:
        log.warning("LLM analysis failed (%s): %s", model_id, exc)
        return None, f"[{model_id}] {exc}"[:500], None, None, "rules"
    try:
        parsed = coerce_llm_output(result.data, allowed)
    except Exception as exc:  # noqa: BLE001 - schema drift must fall back, not lose a paid response
        log.warning("LLM output from %s did not fit the schema: %s", model_id, exc)
        return None, f"[{model_id}] output does not match the schema: {exc}"[:500], result.latency_ms, result.text, "rules"
    return parsed, None, result.latency_ms, result.text, "llm"
