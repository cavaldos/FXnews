"""Backtest dataset API: events + outcomes + per-model predictions, accuracy summary, backfill / model-run jobs."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.backtest import ingest, jobs, predict, stats
from app.backtest.models import HORIZONS, BtEvent  # noqa: F401  (table registration)
from app.config import get_settings
from app.db import get_db, session_scope, utcnow
from app.llm import get_client, parse_model_spec
from app.llm.providers import available

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


def parse_bound(value: str | None, *, end: bool = False) -> datetime | None:
    """``2026-11-01`` or an ISO datetime (UTC when no offset). A date-only END includes that whole day."""
    if not value:
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime.combine(d + timedelta(days=1) if end else d, time.min, tzinfo=UTC)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, f"invalid date {value!r}: use YYYY-MM-DD or an ISO datetime") from exc
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _impacts(impact: str | None) -> list[str] | None:
    if not impact:
        return None
    out = [i.strip().lower() for i in impact.split(",") if i.strip()]
    bad = [i for i in out if i not in stats.IMPACTS]
    if bad:
        raise HTTPException(422, f"invalid impact {bad}: use high, medium, low")
    return out


@router.get("/events")
def events(from_: str | None = Query(None, alias="from"), to: str | None = None, impact: str | None = None,
           symbol: str | None = None, kind: str | None = None, limit: int = Query(1000, ge=1, le=5000),
           offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    return stats.list_events(db, parse_bound(from_), parse_bound(to, end=True), impact=_impacts(impact),
                             symbol=symbol, kind=kind, limit=limit, offset=offset)


@router.get("/summary")
def summary(from_: str | None = Query(None, alias="from"), to: str | None = None, impact: str | None = None,
            symbol: str | None = None, kind: str | None = None, horizon: str | None = None,
            db: Session = Depends(get_db)):
    return stats.summary(db, parse_bound(from_), parse_bound(to, end=True), impact=_impacts(impact),
                         symbol=symbol, kind=kind, horizon=horizon)


@router.get("/models")
def models():
    """Models offered for runs (BACKTEST_MODELS) and the setup state of every provider."""
    client = get_client()
    infos = {i["name"]: i for i in client.info()}
    out = []
    for spec in get_settings().backtest_models:
        provider, model = parse_model_spec(spec, client.settings.default_provider)
        out.append({"id": spec, "provider": provider, "model": model, "problem": (infos.get(provider) or {}).get("problem")})
    return {"models": out, "providers": [{"name": i["name"], "label": i.get("label"), "problem": i.get("problem")}
                                         for i in infos.values()]}


VERIFY_KEYS = ("outcomes_created", "outcomes_updated", "outcomes_skipped_future", "outcomes_skipped_no_data",
               "outcomes_skipped_not_covered", "outcomes_skipped_complete", "outcomes_by_interval",
               "symbols_without_candles", "errors")


def _event_or_404(event_id: int) -> dict:
    with session_scope() as db:
        event = stats.get_event(db, event_id)
    if event is None:
        raise HTTPException(404, "event not found")
    return event


def _validate_specs(specs: list[str]) -> None:
    known = set(available())
    for spec in specs:
        provider, model = parse_model_spec(spec, get_client().settings.default_provider)
        if provider not in known or not model:
            raise HTTPException(422, f"unknown model spec {spec!r}: use <provider>:<model>, providers: {sorted(known)}")


class VerifyBody(BaseModel):
    force: bool = False


class PredictOneBody(BaseModel):
    models: list[str] = Field(min_length=1, max_length=8)
    force: bool = False


@router.get("/events/{event_id}")
def get_event(event_id: int):
    return _event_or_404(event_id)


@router.post("/events/{event_id}/verify")
async def verify_event(event_id: int, body: VerifyBody | None = None):
    """Price this one event from historical candles (synchronous; may take a few seconds)."""
    _event_or_404(event_id)
    try:
        result = await ingest.verify_event(event_id, force=bool(body and body.force))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    data = result.to_dict()
    return {"event": _event_or_404(event_id), "verify": {k: data[k] for k in VERIFY_KEYS}}


@router.post("/events/{event_id}/predict")
async def predict_event(event_id: int, body: PredictOneBody):
    """Ask the chosen models about this one event (only information up to its release time is in the prompt)."""
    _event_or_404(event_id)
    _validate_specs(body.models)
    try:
        results = await predict.predict_one(event_id, body.models, force=body.force)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"event": _event_or_404(event_id), "results": results}


@router.get("/events/{event_id}/prompt")
def event_prompt(event_id: int, horizon: str | None = None):
    """The exact system + user prompt a model would get for this event."""
    if horizon is not None and horizon not in HORIZONS:
        raise HTTPException(422, f"invalid horizon {horizon!r}: use one of {', '.join(HORIZONS)}")
    with session_scope() as db:
        event = db.get(BtEvent, event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        db.expunge(event)
    context = predict.load_context(event)
    prompt = predict.build_prompt(event, event.symbols, horizon or get_settings().backtest_primary_horizon, context)
    return {"system": predict.SYSTEM_PROMPT, "prompt": prompt}


class RangeBody(BaseModel):
    from_: str | None = Field(None, alias="from")
    to: str | None = None

    model_config = {"populate_by_name": True}

    def bounds(self) -> tuple[datetime, datetime]:
        now = utcnow()
        start = parse_bound(self.from_) or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = parse_bound(self.to, end=True) or now
        end = min(end, now) if self.to and len(self.to) == 10 else end
        if end <= start:
            raise HTTPException(422, "'to' must be after 'from'")
        return start, end


class PredictBody(RangeBody):
    models: list[str] = Field(min_length=1, max_length=8)
    min_impact: str = "medium"
    limit: int | None = Field(None, ge=1, le=5000)  # new calls per model
    symbols: list[str] | None = None


@router.post("/backfill", status_code=202)
async def backfill(body: RangeBody):
    if (job := jobs.running("backfill")) is not None:
        raise HTTPException(409, f"a backfill is already running (job {job.id})")
    start, end = body.bounds()
    job = jobs.start("backfill", lambda progress: ingest.backfill(start, end, progress=progress))
    return job.to_dict()


@router.post("/predict", status_code=202)
async def run_models(body: PredictBody):
    if (job := jobs.running("predict")) is not None:
        raise HTTPException(409, f"a model run is already running (job {job.id})")
    if body.min_impact.lower() not in predict.IMPACT_RANK:
        raise HTTPException(422, "min_impact must be high, medium or low")
    _validate_specs(body.models)
    start, end = body.bounds()
    job = jobs.start("predict", lambda progress: predict.run_predictions(
        start, end, body.models, min_impact=body.min_impact, limit=body.limit, symbols=body.symbols, progress=progress))
    return job.to_dict()


@router.get("/jobs")
def list_jobs():
    return [j.to_dict() for j in jobs.recent()]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.to_dict()


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.to_dict()
