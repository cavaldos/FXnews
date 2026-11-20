from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow


def _split(csv: str | None) -> list[str]:
    return [part for part in (csv or "").split(",") if part]


def _join(values: list[str] | None) -> str:
    # Leading/trailing commas let us filter with LIKE '%,EURUSD,%'.
    clean = [v.strip().upper() for v in values or [] if v and v.strip()]
    return f",{','.join(dict.fromkeys(clean))}," if clean else ""


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # calendar | headline
    source: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    currency: Mapped[str | None] = mapped_column(String(8), index=True)
    currencies_csv: Mapped[str] = mapped_column(String(200), default="")
    symbols_csv: Mapped[str] = mapped_column(String(300), default="")
    impact: Mapped[str | None] = mapped_column(String(16), index=True)
    event_time: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    actual: Mapped[str | None] = mapped_column(String(32))
    forecast: Mapped[str | None] = mapped_column(String(32))
    previous: Mapped[str | None] = mapped_column(String(32))
    rule_bias: Mapped[str | None] = mapped_column(String(16))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis_id: Mapped[int | None] = mapped_column(Integer)
    pre_event_alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def currencies(self) -> list[str]:
        return _split(self.currencies_csv)

    @currencies.setter
    def currencies(self, values: list[str]) -> None:
        self.currencies_csv = _join(values)

    @property
    def symbols(self) -> list[str]:
        return _split(self.symbols_csv)

    @symbols.setter
    def symbols(self, values: list[str]) -> None:
        self.symbols_csv = _join(values)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # pre_event | headline | ad_hoc
    news_id: Mapped[int | None] = mapped_column(ForeignKey("news_items.id", ondelete="SET NULL"), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text, default="")
    currency_impact: Mapped[list] = mapped_column(JSON, default=list)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    scenarios: Mapped[list] = mapped_column(JSON, default=list)
    risk_notes: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_response: Mapped[str | None] = mapped_column(Text)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    news_id: Mapped[int | None] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    bias: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(8))  # rules | llm
    model: Mapped[str | None] = mapped_column(String(120))
    horizon: Mapped[str | None] = mapped_column(String(32))
    entry_idea: Mapped[str | None] = mapped_column(Text)
    invalidation: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
