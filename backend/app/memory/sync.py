"""Turn finished outcomes (``bt_outcomes.complete``) into memory facts. Idempotent; only new / recomputed rows are done.

Facts always land in SQLite (``memory_nodes`` / ``memory_edges``); when a Graphiti store is given they are also pushed
to the graph DB. Reads ``bt_events`` + ``bt_outcomes`` only -- prediction / analysis / signal tables are never queried.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.backtest.models import BtEvent, BtOutcome
from app.config import get_settings
from app.db import session_scope, utcnow
from app.memory.facts import BiasLeakError, EdgeSpec, MemoryRecord, NodeSpec, build_record, is_clean
from app.memory.models import MemoryEdge, MemoryNode, MemorySync

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    facts: int = 0  # (event, symbol) outcomes turned into facts
    edges: int = 0
    graph_edges: int = 0  # also pushed to Graphiti
    skipped_bias: int = 0
    graph_error: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"facts": self.facts, "edges": self.edges, "graph_edges": self.graph_edges,
                "skipped_bias": self.skipped_bias, "graph_error": self.graph_error, "errors": self.errors}


def _pending_query():
    return (
        select(BtEvent, BtOutcome)
        .join(BtOutcome, BtOutcome.event_id == BtEvent.id)
        .outerjoin(MemorySync, and_(MemorySync.event_id == BtOutcome.event_id, MemorySync.symbol == BtOutcome.symbol))
        .where(BtOutcome.complete.is_(True))
        .where(or_(MemorySync.id.is_(None), MemorySync.outcome_computed_at < BtOutcome.computed_at))
    )


def pending(db: Session, *, start: datetime | None = None, end: datetime | None = None,
            symbols: list[str] | None = None, limit: int = 200) -> list[tuple[BtEvent, BtOutcome]]:
    """Complete outcomes not yet in memory, or recomputed since they were written. Oldest event first."""
    q = _pending_query().order_by(BtEvent.time, BtOutcome.id).limit(limit)
    if start is not None:
        q = q.where(BtEvent.time >= start)
    if end is not None:
        q = q.where(BtEvent.time <= end)
    if symbols:
        q = q.where(BtOutcome.symbol.in_([s.upper() for s in symbols]))
    return [(ev, oc) for ev, oc in db.execute(q).all()]


def pending_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(_pending_query().subquery())).scalar_one()


CHUNK = 400  # rows per INSERT (SQLite bound-parameter limit)


def _upsert(db: Session, table, rows: list[dict], keys: list[str], update: list[str]) -> None:
    for i in range(0, len(rows), CHUNK):
        stmt = sqlite_insert(table).values(rows[i:i + CHUNK])
        db.execute(stmt.on_conflict_do_update(index_elements=keys, set_={c: stmt.excluded[c] for c in update}))


def save_records(db: Session, records: list[MemoryRecord]) -> int:
    """Upsert into SQLite on the deterministic uuids. Returns the number of edges written."""
    now = utcnow()
    nodes = {n.uuid: (rec.group_id, n) for rec in records for n in rec.nodes}
    edges = {e.uuid: (rec.group_id, e) for rec in records for e in rec.edges}
    if nodes:
        rows = [dict(uuid=u, group_id=g, label=n.label, name=n.name, summary=n.summary, attributes=n.attributes,
                     updated_at=now) for u, (g, n) in nodes.items()]
        _upsert(db, MemoryNode.__table__, rows, ["uuid"],
                ["group_id", "label", "name", "summary", "attributes", "updated_at"])
    if edges:
        rows = [dict(uuid=u, group_id=g, name=e.name, source_uuid=e.source_uuid, target_uuid=e.target_uuid,
                     fact=e.fact, valid_at=e.valid_at, attributes=e.attributes, updated_at=now)
                for u, (g, e) in edges.items()]
        _upsert(db, MemoryEdge.__table__, rows, ["uuid"],
                ["group_id", "name", "source_uuid", "target_uuid", "fact", "valid_at", "attributes", "updated_at"])
    return len(edges)


def _mark(db: Session, pairs: list[tuple[BtEvent, BtOutcome]]) -> None:
    if not pairs:
        return
    rows = [dict(event_id=ev.id, symbol=oc.symbol, outcome_computed_at=oc.computed_at, synced_at=utcnow())
            for ev, oc in pairs]
    _upsert(db, MemorySync.__table__, rows, ["event_id", "symbol"], ["outcome_computed_at", "synced_at"])


async def sync(store=None, *, start: datetime | None = None, end: datetime | None = None,
               symbols: list[str] | None = None, progress: Callable[[str], None] | None = None) -> SyncResult:
    """Outcomes -> facts in SQLite; ``store`` (a ``MemoryStore``) also receives them. A graph error stops only the
    graph push (reported in ``graph_error``); SQLite keeps going, and ``push-graph`` can catch the graph up later."""
    settings = get_settings()
    result = SyncResult()
    while True:
        with session_scope() as db:
            batch = pending(db, start=start, end=end, symbols=symbols, limit=settings.memory_sync_batch)
            if not batch:
                break
            records: list[MemoryRecord] = []
            for ev, oc in batch:
                try:
                    records.append(build_record(ev, oc, include_summary=settings.memory_include_summary))
                except BiasLeakError as exc:
                    log.warning("memory: skipped event %s / %s: %s", ev.id, oc.symbol, exc)
                    result.skipped_bias += 1  # still marked below, so it is not retried forever
            result.edges += save_records(db, records)
            result.facts += len(records)
            _mark(db, batch)
        if store is not None and result.graph_error is None and records:
            try:
                result.graph_edges += await store.write(records)
            except Exception as exc:  # noqa: BLE001 - graph DB down / misconfigured
                result.graph_error = f"{type(exc).__name__}: {exc}"
                log.warning("memory: graph push failed: %s", result.graph_error)
        if progress:
            progress(f"{result.facts} facts")
        if len(batch) < settings.memory_sync_batch:
            break
    return result


def records_from_db(db: Session, *, group_ids: list[str] | None = None) -> list[MemoryRecord]:
    """Rebuild records from SQLite (after an import, or to fill an empty graph DB)."""
    nq, eq = select(MemoryNode), select(MemoryEdge)
    if group_ids:
        nq, eq = nq.where(MemoryNode.group_id.in_(group_ids)), eq.where(MemoryEdge.group_id.in_(group_ids))
    nodes: dict[str, list[NodeSpec]] = defaultdict(list)
    edges: dict[str, list[EdgeSpec]] = defaultdict(list)
    for n in db.execute(nq).scalars():
        nodes[n.group_id].append(NodeSpec(n.uuid, n.name, n.label, n.summary or "", dict(n.attributes or {})))
    titles = {n.uuid: (n.attributes or {}).get("title", "") for g in nodes.values() for n in g}
    for e in db.execute(eq).scalars():
        if not is_clean(e.fact, titles.get(e.source_uuid, "")):
            log.warning("memory: dropped edge %s with opinion vocabulary (imported / edited row)", e.uuid)
            continue
        edges[e.group_id].append(EdgeSpec(e.uuid, e.name, e.source_uuid, e.target_uuid, e.fact, e.valid_at,
                                          dict(e.attributes or {})))
    return [MemoryRecord(g, nodes[g], edges.get(g, [])) for g in sorted(nodes)]


async def push_graph(store, *, group_ids: list[str] | None = None) -> int:
    with session_scope() as db:
        records = records_from_db(db, group_ids=group_ids)
    written = 0
    for rec in records:  # one transaction per symbol keeps each write bounded
        written += await store.write([rec])
    return written
