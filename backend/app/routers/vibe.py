"""/api/vibe/* - thin proxy to the Vibe-Trading sidecar (swarm research, FX backtests, FRED macro series)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from app.config import get_settings
from app.vibe import VibeError, VibeNotFound, VibeUnavailable, get_client, jobs
from app.vibe.schemas import BacktestIn, BacktestJob, MacroSeries, SwarmPreset, SwarmRun, SwarmStartIn, VibeStatus

router = APIRouter(prefix="/api/vibe", tags=["vibe"])

RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
SeriesId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{1,40}$")]


async def _guard(coro):
    try:
        return await coro
    except VibeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except VibeNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except VibeError as exc:
        raise HTTPException(502, f"Vibe-Trading error: {exc}") from exc


@router.get("/status", response_model=VibeStatus)
async def status():
    """Always 200: `enabled` / `reachable` / `error` describe the sidecar state."""
    return await get_client().health()


@router.get("/presets", response_model=list[SwarmPreset])
async def presets():
    return await _guard(get_client().list_swarm_presets())


@router.post("/swarm", response_model=SwarmRun)
async def swarm_start(body: SwarmStartIn):
    return await _guard(get_client().run_swarm(body.preset_name, body.variables))


@router.get("/swarm/{run_id}", response_model=SwarmRun)
async def swarm_get(run_id: RunId):
    return await _guard(get_client().get_swarm_run(run_id))


@router.post("/backtest", response_model=BacktestJob, status_code=202)
async def backtest_start(body: BacktestIn):
    if not get_client().enabled:
        raise HTTPException(503, "Vibe-Trading integration is disabled (set VIBE_ENABLED=true and start sidecar/vibe-trading/run.sh)")
    if body.strategy.kind == "custom" and not get_settings().vibe_allow_custom_code:
        raise HTTPException(400, "Custom strategy code is disabled (set VIBE_ALLOW_CUSTOM_CODE=true to allow it). Use sma_cross, rsi_reversion or donchian_breakout.")
    return jobs.start(body)


@router.get("/backtest/{job_id}", response_model=BacktestJob)
def backtest_get(job_id: RunId):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"backtest job {job_id} not found")
    return job


@router.get("/macro/{series_id}", response_model=MacroSeries)
async def macro(series_id: SeriesId, start: date | None = Query(None), end: date | None = Query(None), limit: int = Query(2000, ge=1, le=5000)):
    return await _guard(get_client().get_macro_series(series_id, start, end, limit))
