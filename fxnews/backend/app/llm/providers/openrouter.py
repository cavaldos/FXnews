"""OpenRouter (OpenAI-compatible chat completions). Default model: openrouter/free.

Settings (app/llm/config.py, section "openrouter"):
    model      default model (a request's `model` wins)
    thinking   "off" | "low" | "medium" | "high" | "" (model default); a request's
               extra={"reasoning": ...} wins
    rotation   failover | round_robin (see keys.py)
    api_keys   one or more keys; a key OpenRouter refuses is skipped until it works again
    base_url, app_name, site_url

Keys, retries and HTTP live in openai_compat.py (shared with Hive).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from ..config import Settings, text
from ..keys import mask
from ..schema import BadInput, LLMError
from . import register
from .openai_compat import KEY_REST, OpenAICompatProvider, _epoch, error_message, json_body

DEFAULT_MODEL = "openrouter/free"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
THINKING = ("off", "low", "medium", "high")
NO_KEY = ("no OpenRouter API key: add one to providers.openrouter.api_keys in backend/llm.yaml "
          "(or set OPENROUTER_API_KEY / OPENROUTER_API_KEYS in backend/.env)")


def key_problem(status: int, message: str, reset_at: Optional[float] = None, now: Optional[float] = None) -> Optional[tuple[str, float, bool]]:
    """(reason, rest seconds, hard) when an answer means "this key cannot be used", else None.

    hard: the key rests even when it is the only one. Soft limits (per minute) only make the
    request move to another key; with no other key they are retried like any 429.
    Upstream rate limits of a free model are not the key's fault: None.
    """
    low = (message or "").lower()
    now = time.time() if now is None else now
    short = " ".join((message or "").split())[:160]
    if status == 401:
        return f"refused (401): {short}", KEY_REST, True
    if status == 402:
        return f"out of credits (402): {short}", KEY_REST, True
    if status == 403 and "key" in low and any(w in low for w in ("limit", "expired", "disabled", "revoked")):
        return f"key limit (403): {short}", KEY_REST, True
    if status == 429 and "upstream" not in low:
        if "per-day" in low or "per day" in low or "daily" in low:
            until = reset_at if reset_at and reset_at > now else _next_utc_midnight(now)
            return f"daily limit (429): {short}", until - now, True
        if "per-min" in low or "per min" in low or "rate limit exceeded" in low:
            until = reset_at if reset_at and reset_at > now else now + 60
            return f"rate limit (429): {short}", until - now, False
    return None


def _next_utc_midnight(now: float) -> float:
    day = datetime.fromtimestamp(now, tz=timezone.utc).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()


@register("openrouter")
class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"
    label = "OpenRouter"
    thinking_levels = THINKING
    default_model = DEFAULT_MODEL
    default_base_url = DEFAULT_BASE_URL

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        super().__init__(settings, transport)
        self.app_name = text(self.cfg.get("app_name")) if "app_name" in self.cfg else "fxnews"
        self.site_url = text(self.cfg.get("site_url"))

    # ---- hooks ------------------------------------------------------------

    no_key_message = NO_KEY

    def key_problem(self, status, message, reset_at=None, now=None):
        return key_problem(status, message, reset_at, now)

    def extra_headers(self) -> dict[str, str]:
        headers = {}
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-Title"] = self.app_name
        return headers

    def payload_options(self) -> dict[str, Any]:
        reasoning = self.reasoning(self.thinking)
        return {"reasoning": reasoning} if reasoning is not None else {}

    def reset_at(self, resp: httpx.Response, data: dict[str, Any], now: float) -> Optional[float]:
        """From the headers or the error's metadata (OpenRouter sends milliseconds)."""
        err = data.get("error")
        headers = (err.get("metadata") or {}).get("headers") if isinstance(err, dict) else None
        headers = headers or {}
        return _epoch(resp.headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset"))

    def retry_without(self, exc: LLMError, payload: dict[str, Any]) -> Optional[str]:
        """Some models (and openrouter/free when it routes to them) cannot turn reasoning off."""
        reasoning = payload.get("reasoning")
        if "reasoning is mandatory" in str(exc).lower() and isinstance(reasoning, dict) and reasoning.get("enabled") is False:
            return "reasoning"
        return None

    # ---- OpenRouter extras -----------------------------------------------

    def list_models(self) -> list[dict[str, Any]]:
        return self._request_sync("GET", "/models").get("data") or []

    def suggested_models(self) -> list[dict[str, Any]]:
        """Free models first, then the rest; vision = reads images (the bot sends a chart)."""
        free_router = {"id": "openrouter/free", "name": "OpenRouter free router (picks a free model)", "vision": True, "free": True}
        seen = {free_router["id"]}
        out = []
        for m in self.list_models():
            model_id = m.get("id")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            arch = m.get("architecture") or {}
            pricing = m.get("pricing") or {}
            try:
                free = float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
            except (TypeError, ValueError):
                free = False
            out.append({
                "id": model_id,
                "name": m.get("name") or model_id,
                "vision": "image" in (arch.get("input_modalities") or []),
                "free": free,
            })
        out.sort(key=lambda m: (not m["free"], not m["vision"], str(m["id"])))
        return [free_router] + out

    def problem(self) -> str | None:
        if len(self.keys) and self.thinking and self.thinking not in THINKING:
            return f"openrouter thinking {self.thinking!r} is not one of {', '.join(THINKING)}"
        return super().problem()

    def check_keys(self) -> list[dict[str, Any]]:
        """Ask OpenRouter about each key (GET /key): credit left, free requests today, expiry.

        A key refused here (401) rests like one refused by a chat request. Costs nothing.
        """
        out = []
        client = self._get_sync()
        for key in list(self.keys.keys):
            item: dict[str, Any] = {"name": key.name, "masked": key.masked, "ok": False}
            try:
                resp = client.get("/key", headers=self._auth(key), timeout=min(20.0, self.settings.timeout))
                data = json_body(resp)
            except httpx.HTTPError as e:
                item["error"] = f"unreachable: {e}"
                out.append(item)
                continue
            if resp.status_code >= 400:
                message = error_message(data.get("error")) if data.get("error") else resp.text[:200]
                item.update(status=resp.status_code, error=f"HTTP {resp.status_code}: {message}")
                problem = key_problem(resp.status_code, message, now=self.keys.now())
                if problem and problem[2]:
                    self.keys.rest(key, problem[0], problem[1], resp.status_code)
                out.append(item)
                continue
            info = data.get("data") or {}
            daily = info.get("free_model_daily_requests") or {}
            item.update(
                ok=True,
                label=info.get("label"),
                usage=info.get("usage"),
                limit=info.get("limit"),
                limitRemaining=info.get("limit_remaining"),
                freeTier=info.get("is_free_tier"),
                expiresAt=info.get("expires_at"),
                freeRequestsToday={"used": daily.get("used"), "limit": daily.get("limit"), "remaining": daily.get("remaining")} if daily else None,
            )
            out.append(item)
        return out

    def thinking_extra(self, level: str | None) -> dict[str, Any]:
        """A request's `reasoning` option wins over providers.openrouter.thinking (extra updates the payload)."""
        reasoning = self.reasoning(level or "")
        return {"reasoning": reasoning} if reasoning is not None else {}

    @staticmethod
    def reasoning(level: str) -> Optional[dict[str, Any]]:
        """OpenRouter `reasoning` option of a thinking level (None = model default)."""
        level = (level or "").strip().lower()
        if not level:
            return None
        if level in ("off", "false", "none", "no"):
            return {"enabled": False}
        if level in THINKING:
            return {"effort": level}
        raise BadInput(f"unknown thinking level '{level}', use one of {', '.join(THINKING)}")


__all__ = ["OpenRouterProvider", "key_problem", "mask"]
