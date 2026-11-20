from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.analysis import advisor
from app.db import get_db, utcnow
from app.models import NewsItem
from app.schemas import AnalysisOut, AnalyzeRequest, NewsKind, NewsOut
from app.services.serializers import news_out

router = APIRouter(prefix="/api", tags=["news"])


def _csv_list(value: str | None) -> list[str]:
    return [v.strip().upper() for v in (value or "").split(",") if v.strip()]


def _utc(value: datetime) -> datetime:
    """The API's time contract: naive values (incl. bare dates, which pydantic reads as midnight) are UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _event_time_bounds(from_: datetime | None, to: datetime | None) -> list:
    """`from` is inclusive, `to` is exclusive, both compared against event_time."""
    if from_ is not None and to is not None and _utc(from_) > _utc(to):
        raise HTTPException(422, "from must not be after to")
    clauses = []
    if from_ is not None:
        clauses.append(NewsItem.event_time >= _utc(from_))
    if to is not None:
        clauses.append(NewsItem.event_time < _utc(to))
    return clauses


@router.get("/news", response_model=list[NewsOut])
def list_news(
    kind: NewsKind | None = None,
    currency: str | None = Query(None, description="CSV, e.g. USD,EUR"),
    impact: str | None = Query(None, description="CSV, e.g. High,Medium"),
    symbol: str | None = None,
    from_: datetime | None = Query(None, alias="from", description="event_time >= from (inclusive). ISO datetime "
                                   "or date; naive values are UTC."),
    to: datetime | None = Query(None, description="event_time < to (exclusive). ISO datetime or date; naive values "
                                "are UTC. Pair with `from` for a date range; page with before/before_id while "
                                "pages come back full."),
    before: datetime | None = Query(None, description="cursor: event_time of the last row you have"),
    before_id: int | None = Query(None, ge=1, description="cursor tie-break: id of the last row you have. Send "
                                  "`before` + `before_id` to page without skipping rows that share a timestamp; "
                                  "rows without event_time come last (send only `before_id` to page inside them). "
                                  "`before` alone keeps the old strict `event_time < before` behaviour."),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(NewsItem)
    if kind:
        stmt = stmt.where(NewsItem.kind == kind)
    else:  # mixed feed: headlines + calendar items that already happened
        stmt = stmt.where(or_(NewsItem.kind == "headline", NewsItem.event_time <= utcnow()))
    if currencies := _csv_list(currency):
        stmt = stmt.where(or_(*[NewsItem.currencies_csv.like(f"%,{c},%") for c in currencies]))
    if impacts := [i.capitalize() for i in _csv_list(impact)]:
        stmt = stmt.where(NewsItem.impact.in_(impacts))
    if symbol:
        stmt = stmt.where(NewsItem.symbols_csv.like(f"%,{symbol.upper()},%"))
    for clause in _event_time_bounds(from_, to):
        stmt = stmt.where(clause)
    # Keyset pagination over the sort key (event_time DESC NULLS LAST, id DESC): a row comes after the cursor
    # (T, I) iff its time is earlier, or equal with a smaller id, or it has no time at all.
    if before is not None and before_id is not None:
        stmt = stmt.where(or_(NewsItem.event_time < before,
                              and_(NewsItem.event_time == before, NewsItem.id < before_id),
                              NewsItem.event_time.is_(None)))
    elif before is not None:
        stmt = stmt.where(NewsItem.event_time < before)
    elif before_id is not None:  # cursor sits inside the no-event_time tail
        stmt = stmt.where(NewsItem.event_time.is_(None), NewsItem.id < before_id)
    stmt = stmt.order_by(NewsItem.event_time.desc().nulls_last(), NewsItem.id.desc()).limit(limit)
    return [news_out(n) for n in db.scalars(stmt)]


@router.get("/calendar", response_model=list[NewsOut])
def calendar(
    days: int = Query(7, ge=1, le=14, description="Default window: now + N days. Ignored when from/to is given."),
    past_days: int = Query(1, ge=0, le=14, description="Default window: now - N days. Ignored when from/to is given."),
    from_: datetime | None = Query(None, alias="from", description="event_time >= from (inclusive). Replaces the "
                                   "days/past_days window. ISO datetime or date; naive values are UTC."),
    to: datetime | None = Query(None, description="event_time < to (exclusive). Replaces the days/past_days window. "
                                "ISO datetime or date; naive values are UTC."),
    currency: str | None = None,
    impact: str | None = None,
    db: Session = Depends(get_db),
):
    if from_ is None and to is None:
        now = utcnow()
        window = [NewsItem.event_time >= now - timedelta(days=past_days),
                  NewsItem.event_time <= now + timedelta(days=days)]
    else:  # an explicit range replaces the default window
        window = _event_time_bounds(from_, to)
    stmt = select(NewsItem).where(NewsItem.kind == "calendar", *window)
    if currencies := _csv_list(currency):
        stmt = stmt.where(NewsItem.currency.in_(currencies))
    if impacts := [i.capitalize() for i in _csv_list(impact)]:
        stmt = stmt.where(NewsItem.impact.in_(impacts))
    return [news_out(n) for n in db.scalars(stmt.order_by(NewsItem.event_time, NewsItem.id))]


@router.get("/news/{news_id}", response_model=NewsOut)
def get_news(news_id: int, db: Session = Depends(get_db)):
    item = db.get(NewsItem, news_id)
    if item is None:
        raise HTTPException(404, "news not found")
    return news_out(item)


@router.post("/news/{news_id}/analyze", response_model=AnalysisOut)
async def analyze(news_id: int, body: AnalyzeRequest | None = None):
    try:
        return await advisor.analyze_news(news_id, model=body.model if body else None)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
