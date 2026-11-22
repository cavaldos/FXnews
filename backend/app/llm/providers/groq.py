"""Groq (groq.com) LLMs through its OpenAI-compatible chat completions API.

    POST https://api.groq.com/openai/v1/chat/completions
    Authorization: Bearer <API key>   (console.groq.com -> API Keys)

Settings (app/llm/config.py, section "groq"):
    model      default model (a request's `model` wins), e.g. llama-3.3-70b-versatile
    thinking   "" (model default) | "low" | "medium" | "high": sent as `reasoning_effort`
               (gpt-oss models); dropped and retried when the model rejects it
    rotation   failover | round_robin (see keys.py)
    api_keys   one or more keys; a key Groq refuses is skipped until it works again
    base_url   https://api.groq.com/openai/v1

When a key is skipped (see key_problem):
    401 / 403   invalid, revoked or blocked key         -> rests 1 h
    429 per day (requests per day, RPD)                  -> rests for Groq's `retry-after`
                                                            (even the only key: nothing else works)
    429 per minute (requests / tokens per minute)        -> the next key answers; this one rests
                                                            for `retry-after`. With one key the
                                                            request is retried as usual.
Limits are per organization: keys of different organizations have separate limits.

Keys, retries and HTTP live in openai_compat.py (shared with OpenRouter and Hive).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from ..schema import BadInput, LLMError
from . import register
from .openai_compat import KEY_REST, OpenAICompatProvider, json_body, response_error

DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
THINKING = ("low", "medium", "high")
MINUTE_REST = 60.0  # per-minute limit without a retry-after header
DAY_REST = 3600.0   # per-day limit without a retry-after header

# Models of Groq's docs, shown while /models cannot be read (no key yet).
MODELS = (
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
)
# /models says nothing about images: guess from the id.
VISION_HINTS = ("vision", "scout", "maverick", "-vl", "muse-spark")


def key_problem(status: int, message: str, reset_at: Optional[float] = None,
                now: Optional[float] = None) -> Optional[tuple[str, float, bool]]:
    """(reason, rest seconds, hard) when a Groq answer means "this key cannot be used", else None."""
    low = (message or "").lower()
    now = time.time() if now is None else now
    short = " ".join((message or "").split())[:160]
    if status in (401, 403):
        return f"refused ({status}): {short}", KEY_REST, True
    if status == 429:
        wait = reset_at - now if reset_at and reset_at > now else None
        if "per day" in low or "(rpd)" in low or "tokens per day" in low or "(tpd)" in low:
            return f"daily limit (429): {short}", wait or DAY_REST, True
        return f"rate limit (429): {short}", wait or MINUTE_REST, False
    return None


@register("groq")
class GroqProvider(OpenAICompatProvider):
    name = "groq"
    label = "Groq"
    thinking_levels = THINKING
    default_model = DEFAULT_MODEL
    default_base_url = DEFAULT_BASE_URL

    def key_problem(self, status, message, reset_at=None, now=None):
        return key_problem(status, message, reset_at, now)

    def payload_options(self) -> dict[str, Any]:
        return {"reasoning_effort": self.thinking} if self.thinking in THINKING else {}

    def thinking_extra(self, level: str | None) -> dict[str, Any]:
        """A request's `reasoning_effort` wins over providers.groq.thinking (extra updates the payload)."""
        level = (level or "").strip().lower()
        if not level:
            return {}
        if level not in THINKING:
            raise BadInput(f"Groq does not accept thinking '{level}': use one of {', '.join(THINKING)}")
        return {"reasoning_effort": level}

    def reset_at(self, resp: httpx.Response, data: dict[str, Any], now: float) -> Optional[float]:
        """`retry-after` is a number of seconds (only on a 429)."""
        try:
            return now + float(resp.headers.get("retry-after", ""))
        except ValueError:
            return None

    def retry_without(self, exc: LLMError, payload: dict[str, Any]) -> Optional[str]:
        """Models without reasoning reject `reasoning_effort`: send again without it."""
        if "reasoning_effort" in payload and exc.status_code == 400 and "reasoning" in str(exc).lower():
            return "reasoning_effort"
        return None

    def problem(self) -> str | None:
        if len(self.keys) and self.thinking and self.thinking not in THINKING:
            return f"groq thinking {self.thinking!r} is not one of {', '.join(THINKING)}"
        return super().problem()

    def list_models(self) -> list[dict[str, Any]]:
        return self._request_sync("GET", "/models").get("data") or []

    def suggested_models(self) -> list[dict[str, Any]]:
        try:
            ids = sorted(str(m["id"]) for m in self.list_models() if m.get("id") and m.get("active", True))
        except LLMError:
            ids = []
        ids = [i for i in ids if not any(w in i for w in ("whisper", "tts", "guard", "orpheus"))] or list(MODELS)
        if self.model and self.model not in ids:
            ids.insert(0, self.model)
        return [{"id": i, "name": i, "vision": any(w in i.lower() for w in VISION_HINTS), "free": False} for i in ids]

    def check_keys(self) -> list[dict[str, Any]]:
        """GET /models with each key: tells a valid key from a refused one, uses no tokens."""
        out = []
        client = self._get_sync()
        for key in list(self.keys.keys):
            item: dict[str, Any] = {"name": key.name, "masked": key.masked, "ok": False}
            try:
                resp = client.get("/models", headers=self._auth(key), timeout=min(20.0, self.settings.timeout))
            except httpx.HTTPError as e:
                item["error"] = f"unreachable: {e}"
                out.append(item)
                continue
            if resp.status_code >= 400:
                message = response_error(resp, json_body(resp))
                item.update(status=resp.status_code, error=f"HTTP {resp.status_code}: {message}")
                problem = key_problem(resp.status_code, message, self.reset_at(resp, {}, self.keys.now()), now=self.keys.now())
                if problem and problem[2]:
                    self.keys.rest(key, problem[0], problem[1], resp.status_code, floor=self.min_rest)
                out.append(item)
                continue
            item.update(ok=True, note=f"{len(json_body(resp).get('data') or [])} models")
            out.append(item)
        return out


__all__ = ["GroqProvider", "key_problem"]
