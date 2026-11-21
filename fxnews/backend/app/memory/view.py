"""Read side of the SQLite memory copy for the Memory page."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.memory.facts import EDGE_INSTANCE, EDGE_REACTION, group_id_for
from app.memory.models import MemoryEdge, MemoryNode
from app.memory.sync import pending_count

MAX_GRAPH_EVENTS = 600


def overview(db: Session) -> dict[str, Any]:
    direction = func.json_extract(MemoryEdge.attributes, "$.direction")
    q = (
        select(
            MemoryEdge.group_id,
            func.count(),
            func.sum(case((direction == "up", 1), else_=0)),
            func.sum(case((direction == "down", 1), else_=0)),
            func.sum(case((direction == "flat", 1), else_=0)),
            func.min(MemoryEdge.valid_at),
            func.max(MemoryEdge.valid_at),
        )
        .where(MemoryEdge.name == EDGE_REACTION)
        .group_by(MemoryEdge.group_id)
        .order_by(func.count().desc())
    )
    symbols = []
    for gid, n, up, down, flat, first, last in db.execute(q).all():
        sym = db.execute(select(MemoryNode.name).where(MemoryNode.group_id == gid, MemoryNode.label == "Symbol")
                         .limit(1)).scalar_one_or_none() or gid.removeprefix("fx_")
        symbols.append({"symbol": sym, "group_id": gid, "reactions": n, "up": up or 0, "down": down or 0,
                        "flat": flat or 0, "first": first, "last": last})
    return {
        "symbols": symbols,
        "nodes": db.execute(select(func.count()).select_from(MemoryNode)).scalar_one(),
        "edges": db.execute(select(func.count()).select_from(MemoryEdge)).scalar_one(),
        "pending": pending_count(db),
    }


def _node(n: MemoryNode) -> dict[str, Any]:
    return {"id": n.uuid, "label": n.label, "name": n.name, "summary": n.summary, "attributes": n.attributes or {}}


def _edge(e: MemoryEdge) -> dict[str, Any]:
    return {"id": e.uuid, "type": e.name, "source": e.source_uuid, "target": e.target_uuid, "fact": e.fact,
            "valid_at": e.valid_at, "attributes": e.attributes or {}}


def graph(db: Session, symbol: str, *, limit: int = 150, q: str | None = None, frm: datetime | None = None,
          to: datetime | None = None, kind: str | None = None, direction: str | None = None) -> dict[str, Any]:
    """The newest ``limit`` reactions of ``symbol`` (optionally filtered) with their events and indicators."""
    gid = group_id_for(symbol)
    limit = max(1, min(limit, MAX_GRAPH_EVENTS))
    rq = select(MemoryEdge).where(MemoryEdge.group_id == gid, MemoryEdge.name == EDGE_REACTION)
    if q:
        like = f"%{q.strip()}%"
        rq = rq.where(MemoryEdge.fact.ilike(like))
    if frm is not None:
        rq = rq.where(MemoryEdge.valid_at >= frm)
    if to is not None:
        rq = rq.where(MemoryEdge.valid_at < to)
    if direction in ("up", "down", "flat"):
        rq = rq.where(func.json_extract(MemoryEdge.attributes, "$.direction") == direction)
    if kind in ("calendar", "headline"):  # event kind lives on the event node
        rq = rq.join(MemoryNode, MemoryNode.uuid == MemoryEdge.source_uuid).where(
            func.json_extract(MemoryNode.attributes, "$.kind") == kind)
    total = db.execute(select(func.count()).select_from(rq.subquery())).scalar_one()
    reactions = list(db.execute(rq.order_by(MemoryEdge.valid_at.desc()).limit(limit)).scalars())

    event_ids = {e.source_uuid for e in reactions}
    instances = list(db.execute(select(MemoryEdge).where(
        MemoryEdge.group_id == gid, MemoryEdge.name == EDGE_INSTANCE, MemoryEdge.source_uuid.in_(event_ids))).scalars()) \
        if event_ids else []
    node_ids = event_ids | {e.target_uuid for e in reactions} | {e.target_uuid for e in instances}
    nodes = list(db.execute(select(MemoryNode).where(MemoryNode.uuid.in_(node_ids))).scalars()) if node_ids else []
    return {
        "symbol": symbol.upper(),
        "total": total,
        "truncated": total > len(reactions),
        "nodes": [_node(n) for n in nodes],
        "edges": [_edge(e) for e in reactions + instances],
    }
