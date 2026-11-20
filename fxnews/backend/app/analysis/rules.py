"""Deterministic priors used before/without the LLM.

Ported from calendarSignalGenerator.js (actual-vs-forecast surprise) and
newsSignalGenerator.js (impact keywords, directional language).
"""

from __future__ import annotations

import re
from typing import Literal

from app.analysis.mapping import PAIR_RE, PROXY_SIGN, currency_positions, symbol_sign
from app.market.symbols import SYMBOLS
from app.schemas import TradeIdea

Strength = Literal["stronger", "weaker"]

# Higher print => currency stronger (hawkish / growth). CPI/PPI included: hotter inflation
# raises rate expectations, which is how FX usually reacts on release.
_HIGHER_IS_BETTER = re.compile(
    r"gdp|employment change|non-?farm|payroll|retail sales|pmi|business confidence|job openings|"
    r"average (hourly )?earnings|wage|consumer confidence|sentiment|trade balance|industrial production|"
    r"manufacturing production|cpi|ppi|inflation|pce|housing starts|building permits|durable goods|"
    r"ism|zew|ifo|current account",
    re.IGNORECASE,
)
_LOWER_IS_BETTER = re.compile(r"unemployment rate|jobless claims|initial claims|continuing claims|claimant count", re.IGNORECASE)

_HIGH_IMPACT_HEADLINE = re.compile(
    r"\b(fed|fomc|ecb|boe|boj|rba|rbnz|boc|snb|rate (?:hike|cut|decision)|interest rate|cpi|inflation|nfp|non-?farm|"
    r"payrolls?|gdp|pce|intervention|tariffs?|sanctions?|war|ceasefire|opec)\b",
    re.IGNORECASE,
)
_MEDIUM_IMPACT_HEADLINE = re.compile(
    r"\b(pmi|retail sales|jobless claims|yields?|treasury|auction|minutes|speech|speaks|testimony|"
    r"trade balance|sentiment|ism)\b",
    re.IGNORECASE,
)

# Currency-strength language (monetary policy / data surprise), scored per clause.
_STRONGER = re.compile(
    r"\b(hawkish|rate hikes?|hikes?|tighten\w*|beats?|stronger[- ]than[- ]expected|hotter|accelerat\w*|"
    r"upside surprise|firmer|robust)\b",
    re.IGNORECASE,
)
# "easing" alone is ambiguous ("easing geopolitical risks"); only count it with policy/rates.
_WEAKER = re.compile(
    r"\b(dovish|rate cuts?|cuts? rates|cutting rates|(?:policy|monetary) easing|eas(?:e|es|ing) (?:policy|rates)|"
    r"miss(?:es|ed)?|weaker[- ]than[- ]expected|cool(?:s|ed|ing)|slow(?:s|ed|ing|down)|downside surprise|"
    r"contraction|recession)\b",
    re.IGNORECASE,
)
# Price-action language describing the subject of a clause ("EUR/USD rises", "Gold rallies", "Loonie declines").
_UP = re.compile(r"\b(rises?|climbs?|gains?|jumps?|surges?|rall(?:y|ies)|advances?|rebounds?|soars?|higher|bid|"
                 r"firms?|strengthens?|recovers?)\b", re.IGNORECASE)
_DOWN = re.compile(r"\b(falls?|drops?|declines?|slides?|slumps?|plunges?|sinks?|tumbles?|retreats?|lower|offered|"
                   r"weakens?|slips?|eases|dips?)\b", re.IGNORECASE)
# Clause boundaries inside a headline. ":" is kept on purpose ("Fed: hike expectations reinforced").
_CLAUSE_SPLIT = re.compile(
    r"\s*(?:[;|,]|\s[–—-]\s|\b(?:as|while|but|despite|after|amid|though|although|whereas|ahead of|even as)\b)\s*",
    re.IGNORECASE,
)


def parse_economic_value(raw: str | None) -> float | None:
    text = str(raw or "").strip().replace(",", "").replace(" ", "")
    match = re.fullmatch(r"([<>]?)(-?\d+(?:\.\d+)?)([KMBT%]?)", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(2))
    return value * {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(match.group(3).upper(), 1)


def surprise_direction(title: str, actual: str | None, forecast: str | None) -> Strength | None:
    a, f = parse_economic_value(actual), parse_economic_value(forecast)
    if a is None or f is None or a == f:
        return None
    if _LOWER_IS_BETTER.search(title or ""):
        return "weaker" if a > f else "stronger"
    if _HIGHER_IS_BETTER.search(title or ""):
        return "stronger" if a > f else "weaker"
    return None


def headline_impact(text: str) -> str:
    if _HIGH_IMPACT_HEADLINE.search(text or ""):
        return "High"
    if _MEDIUM_IMPACT_HEADLINE.search(text or ""):
        return "Medium"
    return "Low"


def headline_strength(text: str) -> Strength | None:
    up, down = len(_STRONGER.findall(text or "")), len(_WEAKER.findall(text or ""))
    if up > down:
        return "stronger"
    if down > up:
        return "weaker"
    return None


def clause_at(text: str, position: int) -> str:
    """The clause of `text` that contains character `position`."""
    start = 0
    for m in _CLAUSE_SPLIT.finditer(text or ""):
        if m.start() > position:
            return text[start:m.start()]
        start = m.end()
    return (text or "")[start:]


def clause_strength(clause: str) -> Strength | None:
    """Price action of the clause subject wins over policy language ("GBP lower despite hawkish BoE")."""
    pa = price_action_bias(clause)
    if pa != "neutral":
        return "stronger" if pa == "bullish" else "weaker"
    return headline_strength(clause)


def bias_from_strength(symbol: str, currency: str, strength: Strength | None) -> str:
    if not strength:
        return "neutral"
    sign = symbol_sign(symbol, currency)
    if sign == 0:
        return "neutral"
    up = (strength == "stronger") == (sign > 0)
    return "bullish" if up else "bearish"


def price_action_bias(title: str) -> str:
    up, down = bool(_UP.search(title or "")), bool(_DOWN.search(title or ""))
    if up and not down:
        return "bullish"
    if down and not up:
        return "bearish"
    return "neutral"


def rule_bias(*, kind: str, title: str, summary: str | None, currencies: list[str], symbols: list[str],
              actual: str | None = None, forecast: str | None = None, explicit_pair: bool = False) -> str:
    """Bias for the primary (first) symbol.

    Headlines are scored on the TITLE only, clause by clause: each currency mentioned in the title
    votes through the clause it appears in, and conflicting votes give neutral rather than a guess.
    `summary`/`explicit_pair` are accepted for call-site compatibility.
    """
    if not symbols:
        return "neutral"
    primary = symbols[0]
    if kind == "calendar":
        return bias_from_strength(primary, currencies[0], surprise_direction(title, actual, forecast)) if currencies else "neutral"

    title = title or ""
    votes: set[str] = set()
    pair_spans: list[tuple[int, int]] = []
    for m in PAIR_RE.finditer(title):
        sym = f"{m.group(1)}{m.group(2)}"
        if sym not in SYMBOLS:
            continue
        pair_spans.append(m.span())
        if sym == primary:  # "EUR/USD rises" speaks about the pair price directly
            pa = price_action_bias(clause_at(title, m.start()))
            if pa != "neutral":
                votes.add(pa)
    proxy_votes: set[str] = set()
    for currency, pos in currency_positions(title).items():
        if any(start <= pos < end for start, end in pair_spans):
            continue  # already counted as pair price action
        bias = bias_from_strength(primary, currency, clause_strength(clause_at(title, pos)))
        if bias != "neutral":
            # Correlation proxies (oil -> CAD, China -> AUD) only count when nothing direct was said.
            (proxy_votes if (currency, primary) in PROXY_SIGN else votes).add(bias)
    votes = votes or proxy_votes
    return votes.pop() if len(votes) == 1 else "neutral"


def fallback_trades(symbols: list[str], currency: str | None, bias_primary: str, impact: str | None) -> list[TradeIdea]:
    """Low-confidence ideas when the LLM is unavailable: propagate the primary bias via currency signs."""
    if bias_primary == "neutral" or not symbols:
        return []
    base_conf = 50 if impact == "High" else 40
    ideas = [TradeIdea(symbol=symbols[0], bias=bias_primary, confidence=base_conf, horizon="intraday",
                       entry_idea="Rule-based: wait for the post-release candle to confirm; do not enter on the news candle itself.")]
    if currency:
        primary_sign = symbol_sign(symbols[0], currency)
        for sym in symbols[1:4]:
            sign = symbol_sign(sym, currency)
            if sign == 0 or primary_sign == 0:
                continue
            same = sign == primary_sign
            bias = bias_primary if same else ("bearish" if bias_primary == "bullish" else "bullish")
            ideas.append(TradeIdea(symbol=sym, bias=bias, confidence=base_conf - 10, horizon="intraday"))
    return ideas


def recommended_action(bias: str, confidence: int) -> str:
    if bias == "bullish":
        return "STRONG BUY" if confidence >= 75 else "BUY"
    if bias == "bearish":
        return "STRONG SELL" if confidence >= 75 else "SELL"
    return "WAIT"
