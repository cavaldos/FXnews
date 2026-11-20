"""OpenRouter client (OpenAI-compatible chat completions) with JSON-mode output."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.schemas import ModelOut

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict
    text: str
    model: str
    latency_ms: int
    usage: dict


_client: httpx.AsyncClient | None = None
_models_cache: tuple[float, list[dict]] | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.AsyncClient(
            base_url=settings.openrouter_base_url,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
            headers={"HTTP-Referer": "http://localhost:5173", "X-Title": "FXNews"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model reply (handles ```json fences and preambles)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    raise LLMError(f"Model reply is not valid JSON: {text[:200]!r}")


RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_TRANSIENT_RETRIES = 2
_RETRY_BASE_S = 2.0  # tests set this to 0


def _retry_after(res: httpx.Response | None, attempt: int) -> float:
    if res is not None:
        try:
            return min(10.0, float(res.headers.get("retry-after", "")))
        except ValueError:
            pass
    return min(10.0, _RETRY_BASE_S * (2**attempt))


async def chat_json(messages: list[dict], model: str, *, temperature: float | None = None,
                    max_tokens: int | None = None) -> LLMResult:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not configured")

    body = {
        "model": model,
        "messages": messages,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        # Reasoning models (gemini-2.5, ...) spend part of this budget on thinking tokens.
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    started = time.perf_counter()

    transient = 0
    while True:
        res: httpx.Response | None = None
        try:
            res = await _http().post("/chat/completions", json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if transient >= MAX_TRANSIENT_RETRIES:
                raise LLMError(f"OpenRouter request failed: {exc!r}") from exc
        else:
            if res.status_code == 400 and "response_format" in body:
                # Some providers reject JSON mode; the prompt already demands JSON, so retry without it.
                log.info("Model %s rejected response_format, retrying without it", model)
                body.pop("response_format")
                continue
            if res.status_code < 400:
                break
            if res.status_code not in RETRY_STATUSES or transient >= MAX_TRANSIENT_RETRIES:
                raise LLMError(f"OpenRouter HTTP {res.status_code}: {res.text[:300]}")
        delay = _retry_after(res, transient)
        transient += 1
        log.info("OpenRouter transient failure (%s), retry %d in %.1fs",
                 res.status_code if res is not None else "network", transient, delay)
        await asyncio.sleep(delay)

    try:
        payload = res.json()
    except ValueError as exc:
        raise LLMError(f"OpenRouter returned non-JSON body: {res.text[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise LLMError(f"Unexpected OpenRouter payload: {str(payload)[:300]}")
    if payload.get("error"):
        raise LLMError(f"OpenRouter error: {payload['error']}")
    try:
        text = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected OpenRouter payload: {str(payload)[:300]}") from exc
    finish = (payload.get("choices") or [{}])[0].get("finish_reason")
    if finish == "length" and not text.rstrip().endswith("}"):
        raise LLMError("Model output truncated (max_tokens); raise LLM_MAX_TOKENS")

    return LLMResult(
        data=extract_json(text),
        text=text,
        model=payload.get("model") or model,
        latency_ms=int((time.perf_counter() - started) * 1000),
        usage=payload.get("usage") or {},
    )


async def list_models() -> list[ModelOut]:
    """Suggested models enriched with OpenRouter metadata (cached 1h). Works without an API key."""
    global _models_cache
    settings = get_settings()
    wanted = list(dict.fromkeys([settings.openrouter_model, *settings.openrouter_suggested_models,
                                 *settings.openrouter_symbol_models.values()]))

    if _models_cache is None or time.time() - _models_cache[0] > 3600:
        try:
            res = await _http().get("/models")
            res.raise_for_status()
            _models_cache = (time.time(), res.json().get("data", []))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Could not load OpenRouter model list: %s", exc)
            _models_cache = (time.time() - 3000, [])  # retry in ~10 minutes

    meta = {m.get("id"): m for m in _models_cache[1]}

    def price(m: dict, key: str) -> float | None:
        try:
            return round(float(m["pricing"][key]) * 1_000_000, 4)
        except (KeyError, TypeError, ValueError):
            return None

    out = []
    for model_id in wanted:
        m = meta.get(model_id, {})
        out.append(ModelOut(
            id=model_id,
            name=m.get("name") or model_id,
            is_default=model_id == settings.openrouter_model,
            context_length=m.get("context_length"),
            prompt_price=price(m, "prompt"),
            completion_price=price(m, "completion"),
        ))
    return out
