"""Recall past market reactions for the advisor prompt, from the SQLite copy of the memory (no Neo4j, no LLM).

Facts only: the release, the measured price move afterwards. ``as_of`` hides anything whose 1d horizon had not yet
elapsed, so a backtest prompt never sees the future.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.models import HORIZON_MINUTES
from app.memory.facts import EDGE_REACTION, group_id_for, is_clean
from app.memory.models import MemoryEdge, MemoryNode

KNOWLEDGE_DELAY = timedelta(minutes=HORIZON_MINUTES["1d"])
CANDIDATES = 800  # newest reactions per symbol that are scored
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("the of and in on at to for a an m q y mom yoy qoq us".split())


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1}


def recall(db: Session, query: str, symbol: str, *, as_of: datetime | None = None, limit: int = 5) -> list[MemoryEdge]:
    """Reaction edges of ``symbol`` most similar to ``query`` (token overlap), newest first among equals."""
    q = _tokens(query)
    if not q or limit <= 0:
        return []
    stmt = select(MemoryEdge).where(MemoryEdge.group_id == group_id_for(symbol), MemoryEdge.name == EDGE_REACTION)
    if as_of is not None:
        moment = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        stmt = stmt.where(MemoryEdge.valid_at <= moment - KNOWLEDGE_DELAY)
    rows = db.execute(stmt.order_by(MemoryEdge.valid_at.desc()).limit(CANDIDATES)).scalars()
    rows = list(rows)
    titles = {n.uuid: (n.attributes or {}).get("title", "") for n in db.execute(
        select(MemoryNode).where(MemoryNode.uuid.in_({e.source_uuid for e in rows}))).scalars()} if rows else {}
    scored = []
    for edge in rows:
        if not is_clean(edge.fact, titles.get(edge.source_uuid, "")):
            continue  # re-checked at read time: imported / hand-edited rows never reach a prompt with an opinion in them
        overlap = len(q & _tokens(edge.fact))
        if overlap:
            scored.append((overlap / len(q), edge))
    scored.sort(key=lambda s: s[0], reverse=True)  # stable: newest stays first among equal scores
    return [e for _, e in scored[:limit]]


def memory_block(db: Session, query: str, symbols: list[str], *, per_symbol: int = 4,
                 as_of: datetime | None = None) -> list[str]:
    """Prompt lines (facts only) for ``PAST_REACTIONS``; empty when memory has nothing relevant."""
    lines: list[str] = []
    for symbol in symbols:
        lines += [f"{e.valid_at:%Y-%m-%d}: {e.fact}" for e in recall(db, query, symbol, as_of=as_of, limit=per_symbol)]
    return lines
