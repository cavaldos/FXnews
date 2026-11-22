from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.analysis import llm
from app.config import get_settings
from app.db import init_db
from app.routers import backtest, data, link_preview, llm_chat, memory, news, reaction, signals, system
from app.security import install_security
from app.services.scheduler import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx/httpcore log every request URL at INFO, and some URLs are secrets (the Discord webhook token is the
# last path segment, Twelve Data takes ?apikey=...). Keep them at WARNING so credentials never reach the logs.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("fxnews")

try:
    from app.routers import market
except ImportError:  # pragma: no cover - market module is built separately
    market = None
    log.warning("app.routers.market not available; /api/candles, /api/quotes, /api/symbols disabled")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    init_db()
    if settings.scheduler_enabled:
        scheduler.start()
    log.info("FXNews backend ready (LLM=%s, Discord=%s, model=%s)",
             settings.llm_enabled, settings.discord_enabled, settings.openrouter_model)
    yield
    await scheduler.stop()
    from app.analysis import advisor
    await advisor.drain_notifications()
    await llm.aclose()
    from app.llm import aclose_default
    await aclose_default()
    try:
        from app.market import candles
        await candles.aclose()
    except (ImportError, AttributeError):
        pass


app = FastAPI(title="FXNews API", version=get_settings().app_version, lifespan=lifespan)
# Host allow-list (DNS rebinding) + CORS + cross-site POST guard (CSRF). See app/security.py.
install_security(app, allowed_origins=get_settings().cors_origins, allowed_hosts=get_settings().allowed_hosts)

for r in (system.router, data.router, news.router, signals.router, backtest.router, reaction.router, link_preview.router, llm_chat.router, memory.router, *( [market.router] if market else [] )):
    app.include_router(r)
