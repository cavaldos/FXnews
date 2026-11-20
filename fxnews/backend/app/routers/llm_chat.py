"""LLM test chat: list the models that can be tried, and chat with one model.

GET  /api/llm/models  providers (with their default `model` and `thinking` level from llm.yaml, and
                      `thinkingChoices`) + models (settings.backtest_models, each provider's default model and its
                      suggested_models, deduplicated; available=false when the provider has a setup problem)
GET  /api/llm/presets prompts to start from (the backtest predictor's system prompt)
POST /api/llm/chat    one chat turn with a "provider:model" spec; the last message must be from the user,
                      the messages before it are the history. Optional `thinking` (effort level) overrides the
                      provider's llm.yaml level; messages may carry `images` (data URLs).

Errors: BadInput -> 400, no usable key / provider not set up -> 503, any other LLMError -> 502.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.backtest.predict import SYSTEM_PROMPT as BACKTEST_SYSTEM_PROMPT
from app.config import get_settings
from app.llm import get_client, parse_model_spec
from app.llm.config import ConfigError
from app.llm.keys import NoKeyAvailable
from app.llm.providers import available
from app.llm.schema import BadInput, ChatRequest, LLMError, Message

log = logging.getLogger("app.llm")

router = APIRouter(prefix="/api/llm", tags=["llm"])


MAX_IMAGES = 6
MAX_IMAGE_CHARS = 7_000_000  # one data URL (~5 MB of image)
_IMAGE_URL = re.compile(r"^data:image/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=]+$")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field("", max_length=100_000)
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)  # data:image/...;base64,... URLs

    @field_validator("images")
    @classmethod
    def _check_images(cls, urls: list[str]) -> list[str]:
        for url in urls:
            if len(url) > MAX_IMAGE_CHARS:
                raise ValueError(f"image too large (max {MAX_IMAGE_CHARS // 1_000_000} MB of base64)")
            if not _IMAGE_URL.match(url):
                raise ValueError("images must be base64 data URLs of png, jpeg, gif or webp")
        return urls


class ChatBody(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    system: str | None = Field(None, max_length=20_000)
    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1, le=100_000)
    thinking: str | None = Field(None, max_length=32)  # effort level; empty/None = the provider's llm.yaml level


@router.get("/models")
def models():
    """Providers (with their setup problem, if any) and the models offered for the chat."""
    try:
        client = get_client()
    except ConfigError as exc:  # a bad llm.yaml: say so on every provider instead of failing the page
        log.warning("llm config: %s", exc)
        return {"providers": [{"name": n, "label": n, "problem": str(exc), "model": "", "thinking": "",
                               "thinkingChoices": []} for n in available()], "models": []}
    infos = {i["name"]: i for i in client.info()}
    providers = [{"name": i["name"], "label": i.get("label") or i["name"], "problem": i.get("problem"),
                  "model": i.get("model") or "", "thinking": i.get("thinking") or "",
                  "thinkingChoices": _thinking_choices(client, i["name"])}
                 for i in infos.values()]

    specs: list[str] = list(get_settings().backtest_models)
    for name, info in infos.items():
        if info.get("model"):
            specs.append(f"{name}:{info['model']}")
        try:
            suggested = client.provider(name).suggested_models()
        except Exception as exc:  # noqa: BLE001 - one provider failing (no key, offline) must not hide the others
            log.warning("suggested_models of %s failed: %s", name, exc)
            continue
        specs.extend(f"{name}:{m['id']}" for m in suggested if m.get("id"))

    out: list[dict] = []
    seen: set[str] = set()
    for spec in specs:
        provider, model = parse_model_spec(spec, client.settings.default_provider)
        key = f"{provider}:{model}"
        if not model or key in seen:
            continue
        seen.add(key)
        problem = (infos.get(provider) or {}).get("problem")
        out.append({"spec": key, "provider": provider, "model": model, "available": problem is None})
    return {"providers": providers, "models": out}


def _thinking_choices(client, name: str) -> list[str] | None:
    """Levels the provider accepts for `thinking` (None = any, [] = none); a broken provider offers none."""
    try:
        return client.provider(name).thinking_choices()
    except Exception:  # noqa: BLE001
        return []


@router.get("/presets")
def presets():
    """Prompts the test chat can load: the system prompt the backtest predictor sends with every event."""
    return {"backtest_system_prompt": BACKTEST_SYSTEM_PROMPT}


def _history_to_request(body: ChatBody, model: str | None, extra: dict | None = None) -> ChatRequest:
    *history, last = body.messages
    if last.role != "user":
        raise BadInput("the last message must be from the user")
    if not last.text.strip() and not last.images:
        raise BadInput("send a message")
    messages: list[Message] = []
    if body.system and body.system.strip():
        messages.append(Message("system", body.system))
    messages.extend(Message(m.role, m.text, list(m.images)) for m in history)
    messages.append(Message("user", last.text, list(last.images)))
    return ChatRequest(messages, model=model, temperature=body.temperature, max_tokens=body.max_tokens,
                       extra=extra or {})


@router.post("/chat")
async def chat(body: ChatBody):
    try:
        client = get_client()
    except ConfigError as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        provider_name, model = parse_model_spec(body.model, client.settings.default_provider)
        if provider_name not in set(available()) or not model:
            raise BadInput(f"unknown model {body.model!r}: use <provider>:<model>, providers: {available()}")
        provider = client.provider(provider_name)
        problem = provider.problem()
        if problem:
            raise NoKeyAvailable(problem)
        req = _history_to_request(body, model, provider.thinking_extra(body.thinking))
        started = time.monotonic()
        result = await provider.achat(req)
    except BadInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except NoKeyAvailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc

    return {
        "text": result.text,
        "model": result.model,
        "provider": result.provider,
        "finish_reason": result.finish_reason,
        "usage": result.usage,
        "reasoning": result.reasoning,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
