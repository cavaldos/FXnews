from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.events import broadcaster
from app.notify import config as notify_config
from app.schemas import (
    HealthOut,
    IngestResult,
    NotifySettingsIn,
    NotifySettingsOut,
    NotifyTestIn,
)
from app.services import ingest

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthOut)
def health():
    s = get_settings()
    cfg = notify_config.load_safe()
    return HealthOut(status="ok", version=s.app_version, llm_enabled=s.llm_enabled,
                     discord_enabled=bool(cfg and cfg.all_accounts("discord")),
                     telegram_enabled=bool(cfg and cfg.all_accounts("telegram")), default_model=s.openrouter_model,
                     last_ingest_at=ingest.state["last_ingest_at"])


@router.post("/ingest/run", response_model=IngestResult)
async def ingest_now(force: bool = Query(False, description="fetch the rate-limited ForexFactory feed even if it was fetched recently")):
    return await ingest.run_all(force=force)


@router.get("/notify/settings", response_model=NotifySettingsOut)
def notify_settings_get():
    try:
        cfg = notify_config.load()
    except notify_config.ConfigError as exc:
        raise HTTPException(500, str(exc)) from None
    return NotifySettingsOut(**notify_config.public(cfg))


@router.post("/notify/settings", response_model=NotifySettingsOut)
def notify_settings_save(body: NotifySettingsIn):
    """Rewrite backend/notify.yaml with the new rules and accounts."""
    try:
        cfg = notify_config.load()
        updated = notify_config.apply_update(cfg, body.rules.model_dump() if body.rules else None,
                                             [a.model_dump() for a in body.accounts]
                                             if body.accounts is not None else None)
    except notify_config.ConfigError as exc:
        raise HTTPException(422, str(exc)) from None
    notify_config.write(updated)
    updated.file = str(notify_config.config_path())
    return NotifySettingsOut(**notify_config.public(updated))


@router.post("/notify/test")
async def notify_test(body: NotifyTestIn | None = None):
    """Send a test message to every enabled account (or to one account)."""
    from app.notify import send_text

    results = await send_text("✅ FXNews: notification channel connected successfully.",
                              only=body.channel if body else None,
                              name=body.name if body else None)
    if not results:
        raise HTTPException(400, "No notification account is enabled — add one in the Notifications page")
    if not any(results.values()):
        failed = ", ".join(k for k, ok in results.items() if not ok)
        raise HTTPException(502, f"No account accepted the test message ({failed}); see the backend logs")
    return {"sent": any(results.values()), "accounts": results}


@router.get("/stream")
async def stream():
    return StreamingResponse(
        broadcaster.subscribe(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
