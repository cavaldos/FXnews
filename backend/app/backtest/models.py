"""Tables of the backtest dataset (kept apart from the live news/analysis tables).

events       one row per news item / economic release (deduplicated by ``dedupe_key``)
outcomes     what the market did after an event, per related symbol (computed from candles)
predictions  what each LLM said about an event, per symbol -- MANY models per event so they can be compared
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow

HORIZONS: tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")
HORIZON_MINUTES: dict[str, int] = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


class BtEvent(Base):
    __tablename__ = "bt_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    time: Mapped[datetime] = mapped_column(UTCDateTime, index=True)  # release / publication time, UTC
    kind: Mapped[str] = mapped_column(String(16), index=True)  # calendar | headline
    source: Mapped[str] = mapped_column(String(32), index=True)  # tradingview | forexfactory | fxstreet | ...
    currency: Mapped[str | None] = mapped_column(String(8), index=True)  # primary currency (USD, EUR, ...)
    country: Mapped[str | None] = mapped_column(String(8))  # ISO-ish country of an economic release (US, EU, ...)
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    impact: Mapped[str] = mapped_column(String(8), index=True)  # high | medium | low
    category: Mapped[str | None] = mapped_column(String(32))
    actual: Mapped[str | None] = mapped_column(String(32))
    forecast: Mapped[str | None] = mapped_column(String(32))
    previous: Mapped[str | None] = mapped_column(String(32))
    symbols_csv: Mapped[str] = mapped_column(String(300), default="")  # ",EURUSD,XAUUSD," (LIKE-filterable)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def symbols(self) -> list[str]:
        return [s for s in (self.symbols_csv or "").split(",") if s]

    @symbols.setter
    def symbols(self, values: list[str]) -> None:
        clean = [v.strip().upper() for v in values or [] if v and v.strip()]
        self.symbols_csv = f",{','.join(dict.fromkeys(clean))}," if clean else ""


class BtOutcome(Base):
    __tablename__ = "bt_outcomes"
    __table_args__ = (UniqueConstraint("event_id", "symbol", name="uq_bt_outcome_event_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bt_events.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    interval: Mapped[str] = mapped_column(String(8))  # candle interval used
    candle_source: Mapped[str | None] = mapped_column(String(32))
    price_at_release: Mapped[float] = mapped_column(Float)
    # Returns in percent vs price_at_release; NULL = horizon not reached yet / no candle there.
    ret_5m: Mapped[float | None] = mapped_column(Float)
    ret_15m: Mapped[float | None] = mapped_column(Float)
    ret_1h: Mapped[float | None] = mapped_column(Float)
    ret_4h: Mapped[float | None] = mapped_column(Float)
    ret_1d: Mapped[float | None] = mapped_column(Float)
    price_5m: Mapped[float | None] = mapped_column(Float)
    price_15m: Mapped[float | None] = mapped_column(Float)
    price_1h: Mapped[float | None] = mapped_column(Float)
    price_4h: Mapped[float | None] = mapped_column(Float)
    price_1d: Mapped[float | None] = mapped_column(Float)
    dir_5m: Mapped[str | None] = mapped_column(String(8))  # up | down | flat
    dir_15m: Mapped[str | None] = mapped_column(String(8))
    dir_1h: Mapped[str | None] = mapped_column(String(8))
    dir_4h: Mapped[str | None] = mapped_column(String(8))
    dir_1d: Mapped[str | None] = mapped_column(String(8))
    max_up_1h: Mapped[float | None] = mapped_column(Float)  # best excursion within 1h, percent (>= 0)
    max_down_1h: Mapped[float | None] = mapped_column(Float)  # worst excursion within 1h, percent (<= 0)
    direction: Mapped[str | None] = mapped_column(String(8), index=True)  # label at the primary horizon
    direction_horizon: Mapped[str | None] = mapped_column(String(4))
    complete: Mapped[bool] = mapped_column(Boolean, default=False)  # all horizons elapsed -> final
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class BtPrediction(Base):
    __tablename__ = "bt_predictions"
    __table_args__ = (UniqueConstraint("event_id", "symbol", "model", name="uq_bt_prediction_event_symbol_model"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bt_events.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(160), index=True)  # "<provider>:<model>"
    direction: Mapped[str] = mapped_column(String(8))  # up | down | flat
    confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100
    horizon: Mapped[str | None] = mapped_column(String(8))
    reasoning: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
