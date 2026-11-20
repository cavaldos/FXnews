from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.events import broadcaster
from app.notify import discord
from app.schemas import HealthOut, IngestResult
from app.services import ingest

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthOut)
def health():
    s = get_settings()
    return HealthOut(status="ok", version=s.app_version, llm_enabled=s.llm_enabled, discord_enabled=s.discord_enabled,
                     default_model=s.openrouter_model, last_ingest_at=ingest.state["last_ingest_at"])


@router.post("/ingest/run", response_model=IngestResult)
async def ingest_now(force: bool = Query(False, description="fetch the rate-limited ForexFactory feed even if it was fetched recently")):
    return await ingest.run_all(force=force)


@router.post("/notify/test")
async def notify_test():
    if not get_settings().discord_enabled:
        raise HTTPException(400, "DISCORD_WEBHOOK_URL is not configured")
    ok = await discord.send_text("✅ FXNews: Discord webhook connected successfully.")
    if not ok:
        raise HTTPException(502, "Discord webhook returned an error, see backend logs")
    return {"sent": True}


@router.get("/stream")
async def stream():
    return StreamingResponse(
        broadcaster.subscribe(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
