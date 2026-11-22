"""Map a news item to the currencies and tradable symbols it is likely to move.

Ported/extended from COUNTRY_TO_SYMBOLS (calendarSignalGenerator.js) and
MT5_SYMBOL_RULES (newsSignalGenerator.js) in the old Node project.
"""

from __future__ import annotations

import re

from app.market.symbols import SYMBOLS, get_symbol
from app.sources.base import RawNews

# Symbols most sensitive to each currency/asset, in priority order.
CURRENCY_SYMBOLS: dict[str, list[str]] = {
    "USD": ["EURUSD", "USDJPY", "XAUUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "US500"],
    "EUR": ["EURUSD", "EURGBP", "EURJPY", "EURAUD", "DE40"],
    "GBP": ["GBPUSD", "EURGBP", "GBPJPY"],
    "JPY": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"],
    "AUD": ["AUDUSD", "AUDJPY", "EURAUD"],
    "NZD": ["NZDUSD"],
    "CAD": ["USDCAD"],
    "CHF": ["USDCHF"],
    # China has no direct pair in the catalog; AUD/NZD are the usual proxies.
    "CNY": ["AUDUSD", "NZDUSD"],
    "XAU": ["XAUUSD"],
    "XAG": ["XAGUSD"],
    "OIL": ["USOIL", "USDCAD"],
    "BTC": ["BTCUSD"],
    "ETH": ["ETHUSD"],
    "SPX": ["US500", "NAS100"],
    "NDX": ["NAS100", "US500"],
}

# Proxy direction: CNY strength tends to lift AUD/NZD (+1).
PROXY_SIGN: dict[tuple[str, str], int] = {
    ("CNY", "AUDUSD"): 1,
    ("CNY", "NZDUSD"): 1,
    ("OIL", "USDCAD"): -1,  # higher oil -> stronger CAD -> USDCAD lower
}

_CURRENCY_PATTERNS: dict[str, re.Pattern[str]] = {
    code: re.compile(pattern, re.IGNORECASE)
    for code, pattern in {
        "USD": r"\b(fed|fomc|powell|waller|federal reserve|u\.?s\.? (?:cpi|gdp|pce|ppi|jobs|economy|inflation|treasur\w*|yields?)|"
        r"non-?farm|nfp|payrolls?|jobless claims|ism|us dollar|greenback|dxy|usd|wall street|treasur(?:y|ies))\b",
        "EUR": r"\b(ecb|lagarde|euro ?zone|euro area|euro|eur|german[y]?|bund|france|french|italy|italian|hicp)\b",
        "GBP": r"\b(boe|bank of england|bailey|uk|britain|british|sterling|pound|gbp|gilts?)\b",
        "JPY": r"\b(boj|bank of japan|ueda|japan|japanese|yen|jpy|jgb)\b",
        "AUD": r"\b(rba|reserve bank of australia|bullock|australia|australian|aussie|aud)\b",
        "NZD": r"\b(rbnz|new zealand|kiwi|nzd)\b",
        "CAD": r"\b(boc|bank of canada|macklem|canada|canadian|loonie|cad)\b",
        "CHF": r"\b(snb|swiss|switzerland|franc|chf)\b",
        "CNY": r"\b(pboc|china|chinese|yuan|renminbi|cny)\b",
        "XAU": r"\b(gold|xau|bullion)\b",
        "XAG": r"\b(silver|xag)\b",
        "OIL": r"\b(oil|wti|brent|crude|opec\+?|eia inventor\w*)\b",
        "BTC": r"\b(bitcoin|btc)\b",
        "ETH": r"\b(ethereum|eth)\b",
        "SPX": r"\b(s&p ?500|us500|spx|dow jones|stocks?|equities)\b",
        "NDX": r"\b(nasdaq|nas100|tech stocks)\b",
    }.items()
}

# "EUR/USD", "EURUSD", "XAU/USD", "GBP-JPY"
PAIR_RE = re.compile(r"\b([A-Z]{3})\s?[/\-]?\s?([A-Z]{3})\b")


def symbol_sign(symbol: str, currency: str) -> int:
    """+1 if `currency` strengthening pushes `symbol` up, -1 if down, 0 if unrelated."""
    info = get_symbol(symbol)
    if not info:
        return 0
    currency = currency.upper()
    if (currency, info.symbol) in PROXY_SIGN:
        return PROXY_SIGN[(currency, info.symbol)]
    if info.base == currency:
        return 1
    # For indices/energy the quote currency is only the denomination: a stronger USD does not
    # mechanically push US500 or WTI down the way it pushes EURUSD down.
    if info.quote == currency and info.kind in ("fx", "metal", "crypto"):
        return -1
    return 0


def explicit_symbols(text: str) -> list[str]:
    found: list[str] = []
    for base, quote in PAIR_RE.findall(text or ""):
        sym = f"{base}{quote}"
        if sym in SYMBOLS and sym not in found:
            found.append(sym)
    return found


def currency_positions(text: str) -> dict[str, int]:
    """Currency code -> index of its first mention in `text`."""
    text = text or ""
    found: dict[str, int] = {}
    for code, pattern in _CURRENCY_PATTERNS.items():
        if m := pattern.search(text):
            found[code] = m.start()
    for m in PAIR_RE.finditer(text):
        sym = f"{m.group(1)}{m.group(2)}"
        if sym in SYMBOLS:
            for code, pos in ((SYMBOLS[sym].base, m.start(1)), (SYMBOLS[sym].quote, m.start(2))):
                found[code] = min(found.get(code, pos), pos)
    return found


def detect_currencies(text: str) -> list[str]:
    """Currencies mentioned in `text`, ordered by first mention (the subject usually comes first)."""
    positions = currency_positions(text)
    return sorted(positions, key=positions.__getitem__)


def symbols_for_currencies(currencies: list[str], limit: int = 6) -> list[str]:
    """Round-robin across currencies so every detected currency gets its top symbol."""
    pools = [list(CURRENCY_SYMBOLS.get(c, [])) for c in currencies]
    out: list[str] = []
    while len(out) < limit and any(pools):
        for pool in pools:
            while pool:
                sym = pool.pop(0)
                if sym not in out:
                    out.append(sym)
                    break
            if len(out) >= limit:
                break
    return out


def map_news(item: RawNews, limit: int = 6) -> tuple[list[str], list[str]]:
    """Return (currencies, symbols) for a news item, most relevant first."""
    if item.kind == "calendar":
        currencies = [item.currency] if item.currency and item.currency != "ALL" else []
        return currencies, symbols_for_currencies(currencies, limit)

    # Title decides the subject; the summary is only a fallback when the title names nothing.
    text = f"{item.title}. {item.summary or ''}"
    pairs = explicit_symbols(item.title) or explicit_symbols(text)
    currencies = detect_currencies(item.title) or detect_currencies(text)
    symbols = list(pairs)
    for sym in symbols_for_currencies(currencies, limit):
        if sym not in symbols:
            symbols.append(sym)
    return currencies, symbols[:limit]
