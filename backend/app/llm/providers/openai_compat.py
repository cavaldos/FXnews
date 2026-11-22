"""Base of providers that speak the OpenAI chat-completions format with one or more API keys.

It holds what OpenRouter and Hive share: the key pool (failover / round_robin, see keys.py),
sync + async HTTP clients, retries of 429 / 5xx / network errors, request building and answer
parsing. A subclass sets its name, label, defaults and `key_problem` (which answers mean
"this key cannot be used now"); everything else is optional.

Settings (app/llm/config.py: backend/llm.yaml, then env; section "<name>"):
    model      default model (a request's `model` wins)
    rotation   failover | round_robin (see keys.py)
    api_keys   one or more keys; a key the provider refuses is skipped until it works again
    base_url   API root (the subclass has a default)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

import httpx

from ..config import Settings, text
from ..keys import ApiKey, KeyPool, NoKeyAvailable, describe_wait, parse_keys, shared_pool
from ..schema import ChatRequest, ChatResult, LLMError
from .base import LLMProvider

log = logging.getLogger("app.llm")

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
KEY_REST = 3600.0  # seconds a refused key (invalid, expired, no credits) waits before it is tried again


class OpenAICompatProvider(LLMProvider):
    """OpenAI-compatible /chat/completions behind a pool of API keys."""

    default_model = ""
    default_base_url = ""
    chat_path = "/chat/completions"
    min_rest = 30.0  # shortest rest of a refused key (Hive's per-second limit needs less)

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        cfg = settings.section(self.name)
        self.cfg = cfg
        self.model = text(cfg.get("model")) or self.default_model
        self.thinking = text(cfg.get("thinking")).lower()
        self.base_url = (text(cfg.get("base_url")) or self.default_base_url).rstrip("/")
        self.rotation = text(cfg.get("rotation")).lower() or "failover"
        self.keys: KeyPool = shared_pool(self.name, parse_keys(cfg.get("api_keys", cfg.get("api_key"))), self.rotation)
        self._transport = transport  # tests inject httpx.MockTransport (works for sync and async)
        self._lock = threading.Lock()
        self._sync: httpx.Client | None = None
        self._async: httpx.AsyncClient | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None

    # ---- hooks for subclasses ---------------------------------------------

    @property
    def no_key_message(self) -> str:
        return (f"no {self.label or self.name} API key: add one to providers.{self.name}.api_keys in backend/llm.yaml "
                f"(or set {self.name.upper()}_API_KEY / {self.name.upper()}_API_KEYS in backend/.env)")

    def key_problem(self, status: int, message: str, reset_at: Optional[float] = None,
                    now: Optional[float] = None) -> Optional[tuple[str, float, bool]]:
        """(reason, rest seconds, hard) when an answer means "this key cannot be used", else None.

        hard: the key rests even when it is the only one. Soft problems (short rate limits) only
        move the request to another key; with no other key they are retried like any 429.
        """
        short = " ".join((message or "").split())[:160]
        if status == 401:
            return f"refused (401): {short}", KEY_REST, True
        if status == 402:
            return f"out of credits (402): {short}", KEY_REST, True
        return None

    def extra_headers(self) -> dict[str, str]:
        return {}

    def payload_options(self) -> dict[str, Any]:
        """Provider options added to every payload (a request's `extra` wins)."""
        return {}

    def reset_at(self, resp: httpx.Response, data: dict[str, Any], now: float) -> Optional[float]:
        """When a rate limit resets (epoch seconds), if the answer says so."""
        return _epoch(resp.headers.get("x-ratelimit-reset"))

    def retry_without(self, exc: LLMError, payload: dict[str, Any]) -> Optional[str]:
        """A payload key to drop and send again after this error (None = give up)."""
        return None

    # ---- public API -------------------------------------------------------

    def chat(self, req: ChatRequest) -> ChatResult:
        payload = self.build_payload(req)
        try:
            data = self._request_sync("POST", self.chat_path, json=payload)
        except LLMError as exc:
            drop = self.retry_without(exc, payload)
            if not drop:
                raise
            payload = {k: v for k, v in payload.items() if k != drop}
            data = self._request_sync("POST", self.chat_path, json=payload)
        return self._parse(data, payload)

    async def achat(self, req: ChatRequest) -> ChatResult:
        payload = self.build_payload(req)
        try:
            data = await self._request_async("POST", self.chat_path, json=payload)
        except LLMError as exc:
            drop = self.retry_without(exc, payload)
            if not drop:
                raise
            payload = {k: v for k, v in payload.items() if k != drop}
            data = await self._request_async("POST", self.chat_path, json=payload)
        return self._parse(data, payload)

    def problem(self) -> str | None:
        if not len(self.keys):
            return self.no_key_message
        problem = self.keys.problem()
        return f"{self.label}: {problem}" if problem else None

    def info(self) -> dict[str, Any]:
        out = super().info()
        out["rotation"] = self.keys.rotation
        out["keys"] = self.keys.status()
        return out

    def close(self) -> None:
        if self._sync:
            self._sync.close()
            self._sync = None

    async def aclose(self) -> None:
        self.close()
        if self._async:
            await self._async.aclose()
            self._async = None

    # ---- request building / parsing --------------------------------------

    def build_payload(self, req: ChatRequest) -> dict[str, Any]:
        messages = []
        for m in req.messages:
            if m.images:
                content: Any = [{"type": "text", "text": m.text}] if m.text else []
                content += [{"type": "image_url", "image_url": {"url": u}} for u in m.images]
            else:
                content = m.text
            messages.append({"role": m.role, "content": content})

        payload: dict[str, Any] = {"model": req.model or self.model, "messages": messages}
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        payload.update(self.payload_options())
        payload.update(req.extra)  # a request's own options win
        return payload

    def _parse(self, data: dict[str, Any], payload: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.label} returned no choices", provider=self.name)
        choice = choices[0]
        if choice.get("error"):
            raise LLMError(f"{self.label}: {error_message(choice['error'])}", provider=self.name)

        msg = choice.get("message") or {}
        return ChatResult(
            text=content_text(msg.get("content")),
            model=data.get("model") or payload["model"],
            provider=self.name,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
            id=data.get("id"),
            reasoning=msg.get("reasoning") or msg.get("reasoning_content"),
            raw=data,
        )

    # ---- HTTP -------------------------------------------------------------

    def _client_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"base_url": self.base_url, "headers": self.extra_headers(), "timeout": self.settings.timeout}
        if self._transport is not None:
            kw["transport"] = self._transport
        return kw

    @staticmethod
    def _auth(key: ApiKey) -> dict[str, str]:
        return {"Authorization": f"Bearer {key.secret}"}

    def _get_sync(self) -> httpx.Client:
        with self._lock:
            if self._sync is None:
                self._sync = httpx.Client(**self._client_kwargs())
            return self._sync

    def _get_async(self) -> httpx.AsyncClient:
        # An AsyncClient is tied to the event loop it first ran on; make a new one if the loop changed
        # (e.g. the caller uses asyncio.run() more than once).
        loop = asyncio.get_running_loop()
        if self._async is None or self._async_loop is not loop:
            self._async = httpx.AsyncClient(**self._client_kwargs())
            self._async_loop = loop
        return self._async

    def _pick(self) -> ApiKey:
        if not len(self.keys):
            raise LLMError(self.no_key_message, provider=self.name)
        try:
            return self.keys.pick()
        except NoKeyAvailable as e:
            raise LLMError(f"{self.label}: {e}", status_code=e.status_code, provider=self.name) from None

    def _request_sync(self, method: str, path: str, **kw) -> dict[str, Any]:
        client = self._get_sync()
        retries = 0
        while True:
            key = self._pick()
            try:
                resp = client.request(method, path, headers=self._auth(key), **kw)
            except httpx.TransportError as e:
                if retries >= self.settings.max_retries:
                    raise LLMError(f"{self.label} unreachable: {e}", provider=self.name) from e
                time.sleep(self._retry_wait(retries, None, e))
                retries += 1
                continue
            action, wait = self._next_step(key, resp, retries)
            if action == "switch":
                continue
            if action == "retry":
                time.sleep(wait)
                retries += 1
                continue
            return self._check(resp, key)

    async def _request_async(self, method: str, path: str, **kw) -> dict[str, Any]:
        client = self._get_async()
        retries = 0
        while True:
            key = self._pick()
            try:
                resp = await client.request(method, path, headers=self._auth(key), **kw)
            except httpx.TransportError as e:
                if retries >= self.settings.max_retries:
                    raise LLMError(f"{self.label} unreachable: {e}", provider=self.name) from e
                await asyncio.sleep(self._retry_wait(retries, None, e))
                retries += 1
                continue
            action, wait = self._next_step(key, resp, retries)
            if action == "switch":
                continue
            if action == "retry":
                await asyncio.sleep(wait)
                retries += 1
                continue
            return self._check(resp, key)

    def _next_step(self, key: ApiKey, resp: httpx.Response, retries: int) -> tuple[str, float]:
        """("switch", 0): try another key now (key switches do not use up retries),
        ("retry", wait): same request again later, ("done", 0): answer or final error."""
        if resp.status_code < 400:
            return "done", 0.0
        data = json_body(resp)
        message = response_error(resp, data)
        problem = self.key_problem(resp.status_code, message, self.reset_at(resp, data, self.keys.now()), now=self.keys.now())
        if problem is not None:
            reason, seconds, hard = problem
            if hard or self.keys.has_other(key):
                self.keys.rest(key, reason, seconds, resp.status_code, floor=self.min_rest)
                log.warning("%s key %s (%s) %s; resting %s", self.label, key.name, key.masked, reason, describe_wait(seconds))
                return "switch", 0.0
        if resp.status_code in RETRY_STATUS and retries < self.settings.max_retries:
            self.keys.note_error(key, f"HTTP {resp.status_code}: {' '.join(message.split())[:160]}")
            return "retry", self._retry_wait(retries, resp)
        return "done", 0.0

    def _retry_wait(self, attempt: int, resp: httpx.Response | None, exc: Exception | None = None) -> float:
        wait = min(2.0**attempt, 10.0)
        if resp is not None:
            try:
                wait = min(float(resp.headers.get("retry-after", "")), 30.0)
            except ValueError:
                pass
        reason = f"HTTP {resp.status_code}" if resp is not None else type(exc).__name__
        log.warning("%s %s, retry %d in %.1fs", self.label, reason, attempt + 1, wait)
        return wait

    def _check(self, resp: httpx.Response, key: ApiKey) -> dict[str, Any]:
        data = json_body(resp)
        if resp.status_code >= 400 or data.get("error"):
            msg = response_error(resp, data)
            self.keys.note_error(key, f"HTTP {resp.status_code}: {' '.join(msg.split())[:160]}")
            raise LLMError(f"{self.label} {resp.status_code}: {msg}", status_code=resp.status_code, provider=self.name)
        self.keys.succeeded(key)
        return data


# ---- helpers ------------------------------------------------------------------

def json_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def response_error(resp: httpx.Response, data: dict[str, Any]) -> str:
    """The error text of an answer: {"error": ...}, {"message": ...} (Hive) or the raw body."""
    if data.get("error"):
        return error_message(data["error"])
    if isinstance(data.get("message"), str) and data["message"]:
        return data["message"]
    return resp.text[:300]


def error_message(err: Any) -> str:
    if isinstance(err, dict):
        msg = err.get("message") or str(err)
        raw = (err.get("metadata") or {}).get("raw")
        return f"{msg} ({raw})" if isinstance(raw, str) and raw else msg
    return str(err)


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):  # some models return content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


def _epoch(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value / 1000.0 if value > 1e11 else value  # milliseconds -> seconds
