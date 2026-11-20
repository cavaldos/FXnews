from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.analysis import advisor, llm
from app.db import get_db
from app.market.symbols import get_symbol
from app.models import Analysis, Signal
from app.schemas import AdviceRequest, AnalysisOut, ModelOut, SignalOut

router = APIRouter(prefix="/api", tags=["signals"])


@router.get("/signals", response_model=list[SignalOut])
def list_signals(symbol: str | None = None, limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    stmt = select(Signal)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol.upper())
    return db.scalars(stmt.order_by(Signal.created_at.desc()).limit(limit)).all()


@router.get("/analyses", response_model=list[AnalysisOut])
def list_analyses(symbol: str | None = None, limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    stmt = select(Analysis)
    if symbol:
        sym = symbol.upper()
        stmt = stmt.where(or_(Analysis.symbol == sym,
                              Analysis.id.in_(select(Signal.analysis_id).where(Signal.symbol == sym))))
    return db.scalars(stmt.order_by(Analysis.created_at.desc()).limit(limit)).all()


@router.get("/analyses/{analysis_id}", response_model=AnalysisOut)
def get_analysis(analysis_id: int, db: Session = Depends(get_db)):
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(404, "analysis not found")
    return row


@router.post("/advice", response_model=AnalysisOut)
async def advice(body: AdviceRequest):
    if get_symbol(body.symbol) is None:
        raise HTTPException(404, f"Unsupported symbol {body.symbol}")
    return await advisor.advise(body.symbol, question=body.question, model=body.model, interval=body.interval)


@router.get("/models", response_model=list[ModelOut])
async def models():
    return await llm.list_models()
