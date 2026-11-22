"""SQLite copy of the memory graph -- the source of truth.

``memory_nodes`` / ``memory_edges`` hold exactly what ``facts.build_record`` produced, so the graph can be viewed
(Memory page), exported / imported (Data page) and re-pushed to Graphiti (``python -m app.memory push-graph``) without
Neo4j being up. ``memory_sync`` records which outcome rows were turned into facts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow


class MemoryNode(Base):
    __tablename__ = "memory_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    group_id: Mapped[str] = mapped_column(String(40), index=True)  # fx_<SYMBOL>
    label: Mapped[str] = mapped_column(String(24), index=True)  # Symbol | MarketEvent | Indicator
    name: Mapped[str] = mapped_column(String(600))
    summary: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MemoryEdge(Base):
    __tablename__ = "memory_edges"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    group_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(32), index=True)  # MARKET_REACTION | INSTANCE_OF
    source_uuid: Mapped[str] = mapped_column(String(36), index=True)
    target_uuid: Mapped[str] = mapped_column(String(36), index=True)
    fact: Mapped[str] = mapped_column(Text)
    valid_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)  # event time
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MemorySync(Base):
    __tablename__ = "memory_sync"
    __table_args__ = (UniqueConstraint("event_id", "symbol", name="uq_memory_sync_event_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("bt_events.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    outcome_computed_at: Mapped[datetime] = mapped_column(UTCDateTime)  # re-sync when the outcome is recomputed
    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
