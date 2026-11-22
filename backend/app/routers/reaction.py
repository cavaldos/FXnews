"""Market reaction endpoints: /api/reaction and /api/reaction/batch."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.market import reaction

router = APIRouter(prefix="/api/reaction", tags=["market"])

MAX_BATCH = 200


class ReactionItem(BaseModel):
    id: str | int
    symbol: str = Field(min_length=1, max_length=16)
    time: datetime


class ReactionBatch(BaseModel):
    items: list[ReactionItem] | None = None
    horizons: list[str] | None = None


def _horizons(value: str | list[str] | None) -> list[str]:
    try:
        return reaction.parse_horizons(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def get_one(
    symbol: str = Query(..., min_length=1, max_length=16, description="e.g. XAUUSD"),
    time: datetime = Query(..., description="event time, ISO 8601 (UTC when no offset)"),
    horizons: str | None = Query(None, description="comma separated subset of 5m,15m,1h,4h,1d (default all)"),
    db: Session = Depends(get_db),
):
    res = await reaction.get_reaction(symbol, time, _horizons(horizons), db=db)
    if res["status"] == "unsupported_symbol":
        raise HTTPException(404, res["message"])
    return res


@router.post("/batch")
async def post_batch(body: ReactionBatch | list[ReactionItem], horizons: str | None = Query(None),
                     db: Session = Depends(get_db)):
    """Body: ``[{id, symbol, time}]`` (or ``{items: [...], horizons: [...]}``), up to 200 items."""
    items = body if isinstance(body, list) else (body.items or [])
    hz = _horizons(horizons if horizons is not None else (None if isinstance(body, list) else body.horizons))
    if len(items) > MAX_BATCH:
        raise HTTPException(422, f"Too many items (max {MAX_BATCH})")
    results = await reaction.get_reactions([(i.symbol, i.time) for i in items], hz, db=db)
    return {"results": [{"id": i.id, **r} for i, r in zip(items, results, strict=True)]}
