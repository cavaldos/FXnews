"""Memory API (the "Memory" page): browse the fact graph, sync it from finished backtest outcomes.

The facts live in SQLite (``memory_*`` tables, exported / imported by the Data page); Graphiti / Neo4j is optional.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.db import session_scope
from app.memory import view
from app.routers.backtest import parse_bound

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("/overview")
def overview():
    with session_scope() as db:
        return view.overview(db)


@router.get("/graph")
def graph(
    symbol: str = Query(..., min_length=2, max_length=16),
    limit: int = Query(150, ge=1, le=view.MAX_GRAPH_EVENTS),
    q: str | None = Query(None, max_length=200, description="text filter on the fact"),
    frm: str | None = Query(None, alias="from"),
    to: str | None = None,
    kind: Literal["calendar", "headline"] | None = None,
    direction: Literal["up", "down", "flat"] | None = None,
):
    with session_scope() as db:
        return view.graph(db, symbol, limit=limit, q=q, frm=parse_bound(frm), to=parse_bound(to, end=True), kind=kind,
                          direction=direction)


class SyncRequest(BaseModel):
    push_graph: bool = False  # also write to Graphiti (needs Neo4j / FalkorDB running)


_SYNC_LOCK = threading.Lock()


def _run_sync(push_graph: bool) -> dict:
    """Runs in a worker thread with its own event loop: the SQLite work is blocking and can take a while."""
    from app.memory.sync import sync as run_sync

    async def go() -> dict:
        store, graph_error = None, None
        if push_graph:
            try:
                from app.memory.store import MemoryStore

                store = MemoryStore()
            except Exception as exc:  # noqa: BLE001 - driver / config problem
                graph_error = f"{type(exc).__name__}: {exc}"
        try:
            out = (await run_sync(store)).to_dict()
        finally:
            if store is not None:
                await store.close()
        out["graph_error"] = out["graph_error"] or graph_error
        return out

    return asyncio.run(go())


@router.post("/sync")
async def sync(body: SyncRequest | None = None):
    if not _SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(409, "a memory sync is already running")
    try:
        return await run_in_threadpool(_run_sync, bool(body and body.push_graph))
    finally:
        _SYNC_LOCK.release()
