"""Typed request/response models for the Vibe-Trading sidecar (also the /api/vibe/* contract)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ── status ──


class VibeStatus(BaseModel):
    enabled: bool
    reachable: bool = False
    base_url: str = ""
    server_name: str | None = None
    server_version: str | None = None
    tool_count: int | None = None
    error: str | None = None


# ── swarm ──


class SwarmVariable(BaseModel):
    name: str
    description: str = ""
    required: bool = False


class SwarmPreset(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    agent_count: int = 0
    variables: list[SwarmVariable] = []
    source: str = "bundled"


class SwarmStartIn(BaseModel):
    preset_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    variables: dict[str, str] = Field(default_factory=dict, description="Preset variables, e.g. {'goal': 'EUR/USD tactical view', 'timeframe': 'tactical 1-3 months'}")


class SwarmTask(BaseModel):
    model_config = {"extra": "allow"}
    id: str | None = None
    agent_id: str | None = None
    status: str | None = None
    summary: str | None = None
    error: str | None = None


class SwarmRun(BaseModel):
    run_id: str
    status: str  # pending | running | completed | failed | cancelled
    preset: str | None = None
    created_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    tasks: list[SwarmTask] = []
    final_report: str | None = None
    report_is_partial: bool = False
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    is_stale: bool = False
    ready: bool = False  # True once status is completed / failed / cancelled


# ── backtest ──

StrategyKind = Literal["sma_cross", "rsi_reversion", "donchian_breakout", "custom"]


class StrategySpec(BaseModel):
    kind: StrategyKind = "sma_cross"
    # sma_cross: fast, slow | rsi_reversion: period, low, high | donchian_breakout: lookback
    params: dict[str, float] = Field(default_factory=dict)
    code: str | None = Field(default=None, max_length=20000, description="kind=custom: Python source defining class SignalEngine with generate(data_map) -> {code: Series in [-1, 1]}")

    @model_validator(mode="after")
    def _custom_needs_code(self):
        if self.kind == "custom" and not (self.code and "class SignalEngine" in self.code):
            raise ValueError("kind='custom' requires `code` defining class SignalEngine")
        return self


class BacktestIn(BaseModel):
    codes: list[str] = Field(min_length=1, max_length=10, description="FX pairs such as 'EUR/USD', 'USD/JPY', 'XAU/USD'")
    start_date: date
    end_date: date
    interval: Literal["1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"] = "1D"
    source: str = Field(default="auto", pattern=r"^[A-Za-z0-9_-]{1,30}$")
    strategy: StrategySpec = Field(default_factory=StrategySpec)
    initial_cash: float = Field(default=100_000.0, gt=0)
    leverage: float = Field(default=1.0, ge=1, le=500, description="Notional = signal x equity x leverage; 1 = unlevered (a +/-1 signal risks 100% of equity per unit)")
    spread_pips: float | None = Field(default=None, ge=0, le=100, description="Override the per-pair default spread")
    slippage_pips: float = Field(default=0.3, ge=0, le=50)
    swap_enabled: bool = True

    @field_validator("codes")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        out = [c.strip().upper() for c in v]
        if any(not c or len(c) > 20 or not all(ch.isalnum() or ch in "/.-_" for ch in c) for c in out):
            raise ValueError("invalid instrument code")
        return out

    @model_validator(mode="after")
    def _range(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class BacktestResult(BaseModel):
    metrics: dict[str, Any] = {}
    equity_preview: list[Any] = []
    artifact_paths: dict[str, Any] = {}
    run_dir: str | None = None
    raw: dict[str, Any] = {}  # full tool envelope from the sidecar


class BacktestJob(BaseModel):
    id: str
    status: Literal["running", "completed", "failed"]
    created_at: str
    finished_at: str | None = None
    error: str | None = None
    request: BacktestIn | None = None
    result: BacktestResult | None = None


# ── data ──


class MacroObservation(BaseModel):
    date: str
    value: float | str | None = None


class MacroSeries(BaseModel):
    series_id: str
    source: str = "fred"
    count: int = 0
    observations_available: int | None = None
    truncated: bool = False
    observations: list[MacroObservation] = []


class MarketData(BaseModel):
    symbol: str
    interval: str
    data: Any = None  # sidecar payload as is (per-symbol rows + provenance)
