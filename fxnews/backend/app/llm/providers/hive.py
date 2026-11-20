"""Hive (thehive.ai) LLMs through its OpenAI-compatible chat completions API.

    POST https://api.thehive.ai/api/v3/chat/completions
    Authorization: Bearer <secret key>   (Hive dashboard -> Service API Keys)

Settings (app/llm/config.py, section "hive"):
    model      default model (a request's `model` wins), e.g. deepseek-ai/deepseek-v4.1-flash-1017
    rotation   failover | round_robin (see keys.py)
    api_keys   one or more secret keys; a key Hive refuses is skipped until it works again
    base_url   https://api.thehive.ai/api/v3 (also api-cdn.thehive.ai, or api-va1.thehive.ai for
               Virginia customers)

When a key is skipped (see key_problem):
    401 / 403   invalid or revoked key                   -> rests 1 h
    402 / 405   organization out of balance (paused)     -> rests 1 h
    429         over the rate limit (5 requests/s each)   -> another key takes the request; the
                                                            key rests a few seconds. With one key
                                                            the request is retried as usual.
Keys of different Hive organizations have separate balances and limits, so rotation helps for both.

Keys, retries and HTTP live in openai_compat.py (shared with OpenRouter).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from . import register
from .openai_compat import KEY_REST, OpenAICompatProvider, json_body, response_error

DEFAULT_MODEL = "deepseek-ai/deepseek-v4.1-flash-1017"
DEFAULT_BASE_URL = "https://api.thehive.ai/api/v3"
RATE_REST = 5.0  # seconds a key rests after a 429 when another key can take over

# Hive has no documented model list endpoint: the models its docs name.
MODELS = (
    {"id": "deepseek-ai/deepseek-v4.1-flash-1017", "name": "DeepSeek V4.1 Flash (text + image, 1M context)", "vision": True, "free": False},
    {"id": "zai-org/glm-5.3-flash", "name": "GLM 5.3 Flash (text + image + video, 1M context)", "vision": True, "free": False},
    {"id": "hive/vision-language-model", "name": "Hive VLM (image understanding)", "vision": True, "free": False},
)


def key_problem(status: int, message: str, reset_at: Optional[float] = None,
                now: Optional[float] = None) -> Optional[tuple[str, float, bool]]:
    """(reason, rest seconds, hard) when a Hive answer means "this key cannot be used", else None."""
    low = (message or "").lower()
    now = time.time() if now is None else now
    short = " ".join((message or "").split())[:160]
    if status in (401, 403):
        return f"refused ({status}): {short}", KEY_REST, True
    if status == 402 or (status == 405 and any(w in low for w in ("balance", "paused", "credit"))):
        return f"out of balance ({status}): {short}", KEY_REST, True
    if status == 429:
        until = reset_at if reset_at and reset_at > now else now + RATE_REST
        return f"rate limit (429): {short}", until - now, False
    return None


@register("hive")
class HiveProvider(OpenAICompatProvider):
    name = "hive"
    label = "Hive"
    thinking_levels = ()
    default_model = DEFAULT_MODEL
    default_base_url = DEFAULT_BASE_URL
    min_rest = 1.0

    def key_problem(self, status, message, reset_at=None, now=None):
        return key_problem(status, message, reset_at, now)

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": m["id"], "name": m["name"], "architecture": {"input_modalities": ["text", "image"]}} for m in MODELS]

    def suggested_models(self) -> list[dict[str, Any]]:
        out = [dict(m) for m in MODELS]
        if self.model and all(m["id"] != self.model for m in out):
            out.insert(0, {"id": self.model, "name": self.model, "vision": True, "free": False})
        return out

    def check_keys(self) -> list[dict[str, Any]]:
        """Send a 1-token request with each key (Hive has no key info endpoint; costs almost nothing).

        A key refused here rests like one refused by a chat request.
        """
        out = []
        payload = {"model": self.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        client = self._get_sync()
        for key in list(self.keys.keys):
            item: dict[str, Any] = {"name": key.name, "masked": key.masked, "ok": False}
            try:
                resp = client.post(self.chat_path, json=payload, headers=self._auth(key), timeout=min(30.0, self.settings.timeout))
            except httpx.HTTPError as e:
                item["error"] = f"unreachable: {e}"
                out.append(item)
                continue
            if resp.status_code >= 400:
                message = response_error(resp, json_body(resp))
                item.update(status=resp.status_code, error=f"HTTP {resp.status_code}: {message}")
                problem = key_problem(resp.status_code, message, now=self.keys.now())
                if problem and problem[2]:
                    self.keys.rest(key, problem[0], problem[1], resp.status_code, floor=self.min_rest)
                out.append(item)
                continue
            self.keys.succeeded(key)
            item.update(ok=True, note=f"answered with {self.model}")
            out.append(item)
        return out


__all__ = ["HiveProvider", "key_problem"]
