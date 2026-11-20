"""Market data endpoints: /api/symbols, /api/candles, /api/quotes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query

from app.market import candles as market
from app.market.symbols import all_symbols, get_symbol
from app.schemas import CandlesOut, Interval, QuoteOut, SymbolOut

router = APIRouter(prefix="/api", tags=["market"])

DEFAULT_QUOTE_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "XAUUSD"]
MAX_QUOTE_SYMBOLS = 30


@router.get("/symbols", response_model=list[SymbolOut])
async def list_symbols() -> list[SymbolOut]:
    return [SymbolOut(**asdict(s)) for s in all_symbols()]


@router.get("/candles", response_model=CandlesOut)
async def get_candles(
    symbol: str = Query(..., min_length=1, description="e.g. EURUSD, XAUUSD"),
    interval: Interval = Query("15m"),
    range_: str | None = Query(None, alias="range", description="e.g. 1d, 5d, 1mo, 3mo, 1y; default depends on interval"),
) -> CandlesOut:
    try:
        return await market.get_candles(symbol, interval, range_)
    except market.UnsupportedSymbolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:  # bad range
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except market.MarketDataError as exc:
        raise HTTPException(status_code=502, detail=f"Market data provider error: {exc}") from exc


@router.get("/quotes", response_model=list[QuoteOut])
async def get_quotes(
    symbols: str | None = Query(None, description="comma separated, e.g. EURUSD,XAUUSD; default: majors + XAUUSD"),
) -> list[QuoteOut]:
    requested = (
        list(dict.fromkeys(s.strip().upper() for s in symbols.split(",") if s.strip()))
        if symbols
        else list(DEFAULT_QUOTE_SYMBOLS)
    )
    if len(requested) > MAX_QUOTE_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"Too many symbols (max {MAX_QUOTE_SYMBOLS})")
    known = [s for s in requested if get_symbol(s)]
    if requested and not known:
        raise HTTPException(status_code=404, detail=f"Unknown symbols: {', '.join(requested)}")
    quotes = await market.get_quotes(known)
    if known and not quotes:  # every known symbol failed upstream (each failure is logged)
        raise HTTPException(status_code=502, detail="Market data provider error: no quotes available")
    return quotes
