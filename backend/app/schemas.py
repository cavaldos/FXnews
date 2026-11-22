"""API contract. Frontend types in frontend/src/api/types.ts mirror this file."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NewsKind = Literal["calendar", "headline"]
Impact = Literal["High", "Medium", "Low", "Holiday"]
Bias = Literal["bullish", "bearish", "neutral"]
AnalysisKind = Literal["pre_event", "headline", "ad_hoc"]
SignalSource = Literal["rules", "llm"]
Interval = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── symbols / market ─────────────────────────────────────────────────


class SymbolOut(BaseModel):
    symbol: str  # MT5-style name, e.g. EURUSD, XAUUSD
    base: str  # EUR, XAU, ...
    quote: str  # USD, JPY, ...
    kind: Literal["fx", "metal", "energy", "index", "crypto"]
    digits: int  # price precision for display
    yahoo: str  # provider ticker, e.g. EURUSD=X
    tv: str  # TradingView ticker, e.g. FX:EURUSD, OANDA:XAUUSD (primary candle source)


class Candle(BaseModel):
    time: int  # unix seconds (UTC), bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class CandlesOut(BaseModel):
    symbol: str
    interval: Interval
    source: str  # e.g. "yahoo"; "<provider>-stale" when served from the last-good cache
    candles: list[Candle]
    stale: bool = False  # True when served from the last-good cache because every provider failed
    as_of: int | None = None  # unix seconds the stale data was fetched (set only when stale)


class QuoteOut(BaseModel):
    symbol: str
    price: float
    change: float  # absolute change vs previous close
    change_pct: float
    time: int  # unix seconds of the last price


# ── news ─────────────────────────────────────────────────────────────


class NewsOut(ORMModel):
    id: int
    kind: NewsKind
    source: str  # "forexfactory", "fxstreet", ...
    title: str
    summary: str | None = None
    url: str | None = None
    currency: str | None = None  # primary currency (calendar country / detected)
    currencies: list[str] = []  # all detected currencies
    symbols: list[str] = []  # related symbols from mapping
    impact: Impact | None = None
    event_time: datetime | None = None  # calendar release time / headline publish time (UTC)
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    rule_bias: Bias | None = None  # deterministic prior for the primary symbol
    fetched_at: datetime
    analysis_id: int | None = None  # latest LLM analysis for this item, if any


# ── analysis / signals ───────────────────────────────────────────────


class TradeIdea(BaseModel):
    symbol: str
    bias: Bias
    confidence: int = Field(ge=0, le=100)
    horizon: str | None = None  # "scalp", "intraday", "swing"
    entry_idea: str | None = None
    invalidation: str | None = None
    targets: str | None = None


class Scenario(BaseModel):
    condition: str  # e.g. "Actual > 0.3% forecast"
    outcome: str  # e.g. "USD stronger: sell EURUSD, XAUUSD"


class CurrencyImpact(BaseModel):
    currency: str
    direction: Literal["stronger", "weaker", "neutral"]
    strength: float = Field(ge=0, le=1)


class AnalysisOut(ORMModel):
    id: int
    kind: AnalysisKind
    news_id: int | None = None
    symbol: str | None = None  # focus symbol for ad_hoc
    model: str  # OpenRouter model id or "rules"
    summary: str
    currency_impact: list[CurrencyImpact] = []
    trades: list[TradeIdea] = []
    scenarios: list[Scenario] = []
    risk_notes: str | None = None
    question: str | None = None  # ad_hoc question
    error: str | None = None  # set when LLM failed and we fell back to rules
    latency_ms: int | None = None
    notified: bool = False
    created_at: datetime


class SignalOut(ORMModel):
    id: int
    analysis_id: int | None = None
    news_id: int | None = None
    symbol: str
    bias: Bias
    confidence: int
    source: SignalSource
    model: str | None = None
    horizon: str | None = None
    entry_idea: str | None = None
    invalidation: str | None = None
    title: str | None = None  # news title for display
    created_at: datetime


class AnalyzeRequest(BaseModel):
    model: str | None = None  # override OpenRouter model


class AdviceRequest(BaseModel):
    symbol: str
    question: str | None = None
    model: str | None = None
    interval: Interval = "15m"


class ModelOut(BaseModel):
    id: str
    name: str
    is_default: bool = False
    context_length: int | None = None
    prompt_price: float | None = None  # USD per 1M tokens
    completion_price: float | None = None


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    llm_enabled: bool
    discord_enabled: bool
    default_model: str
    last_ingest_at: datetime | None = None


class IngestResult(BaseModel):
    inserted: int
    analyzed: int
    errors: list[str] = []
    notes: list[str] = []  # informational (e.g. a rate-limited source skipped on purpose), not failures
