"""Symbol catalog (ported from src/prices/mt5ToYahoo.js)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    kind: Literal["fx", "metal", "energy", "index", "crypto"]
    digits: int
    yahoo: str
    tv: str  # TradingView ticker (primary data provider; must match frontend/src/lib/tradingview.ts)


# TradingView tickers for non-forex symbols (forex is FX:<BASE><QUOTE>). Keep in sync with
# frontend/src/lib/tradingview.ts (the embed fallback) -- the frontend prefers the `tv` value from /api/symbols.
_TV_OVERRIDES: dict[str, str] = {
    "XAUUSD": "OANDA:XAUUSD",
    "XAGUSD": "OANDA:XAGUSD",
    "USOIL": "TVC:USOIL",
    "US500": "OANDA:SPX500USD",
    "NAS100": "OANDA:NAS100USD",
    "US30": "OANDA:US30USD",
    "DE40": "OANDA:DE30EUR",
    "BTCUSD": "BITSTAMP:BTCUSD",
    "ETHUSD": "BITSTAMP:ETHUSD",
}

_RAW: list[tuple[str, str, str, str, int, str]] = [
    # Majors
    ("EURUSD", "EUR", "USD", "fx", 5, "EURUSD=X"),
    ("GBPUSD", "GBP", "USD", "fx", 5, "GBPUSD=X"),
    ("USDJPY", "USD", "JPY", "fx", 3, "USDJPY=X"),
    ("AUDUSD", "AUD", "USD", "fx", 5, "AUDUSD=X"),
    ("NZDUSD", "NZD", "USD", "fx", 5, "NZDUSD=X"),
    ("USDCAD", "USD", "CAD", "fx", 5, "USDCAD=X"),
    ("USDCHF", "USD", "CHF", "fx", 5, "USDCHF=X"),
    # Crosses
    ("EURGBP", "EUR", "GBP", "fx", 5, "EURGBP=X"),
    ("EURJPY", "EUR", "JPY", "fx", 3, "EURJPY=X"),
    ("GBPJPY", "GBP", "JPY", "fx", 3, "GBPJPY=X"),
    ("AUDJPY", "AUD", "JPY", "fx", 3, "AUDJPY=X"),
    ("EURAUD", "EUR", "AUD", "fx", 5, "EURAUD=X"),
    # Metals / energy (Yahoo spot XAU is unreliable -> futures)
    ("XAUUSD", "XAU", "USD", "metal", 2, "GC=F"),
    ("XAGUSD", "XAG", "USD", "metal", 3, "SI=F"),
    ("USOIL", "OIL", "USD", "energy", 2, "CL=F"),
    # Indices
    ("US500", "SPX", "USD", "index", 1, "^GSPC"),
    ("NAS100", "NDX", "USD", "index", 1, "^NDX"),
    ("US30", "DJI", "USD", "index", 1, "^DJI"),
    ("DE40", "DAX", "EUR", "index", 1, "^GDAXI"),
    # Crypto
    ("BTCUSD", "BTC", "USD", "crypto", 1, "BTC-USD"),
    ("ETHUSD", "ETH", "USD", "crypto", 2, "ETH-USD"),
]

SYMBOLS: dict[str, SymbolInfo] = {
    row[0]: SymbolInfo(*row, tv=_TV_OVERRIDES.get(row[0], f"FX:{row[1]}{row[2]}")) for row in _RAW
}

FIAT_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY")


def get_symbol(symbol: str) -> SymbolInfo | None:
    return SYMBOLS.get(str(symbol or "").strip().upper())


def all_symbols() -> list[SymbolInfo]:
    return list(SYMBOLS.values())
