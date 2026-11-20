"""LLMClient: one place to get a provider by name and run a chat. Trimmed port of Bot_Trade/llm_service/src/client.py
(no YAML hot-reload, no image helpers: settings come from the backend's env configuration)."""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable

from .config import Settings
from .providers import LLMProvider, available, create_provider
from .schema import BadInput, ChatRequest, ChatResult, LLMError, Message

log = logging.getLogger("app.llm")


def parse_model_spec(spec: str, default_provider: str = "openrouter") -> tuple[str, str]:
    """``"openrouter:google/gemini-2.5-flash"`` -> (``"openrouter"``, ``"google/gemini-2.5-flash"``).

    Splits on the first colon only when the left side is a registered provider, so an OpenRouter id such as
    ``meta/llama:free`` is not mangled. A bare model name goes to ``default_provider``.
    """
    spec = (spec or "").strip()
    head, sep, tail = spec.partition(":")
    if sep and head in available():
        return head, tail.strip()
    return default_provider, spec


def model_spec(provider: str, model: str) -> str:
    return f"{provider}:{model}"


class LLMClient:
    """Holds settings and the open provider connections. Thread-safe; reuse one instance."""

    def __init__(self, settings: Settings | None = None, providers: dict[str, LLMProvider] | None = None):
        self.settings = settings or Settings.from_app()
        self._providers: dict[str, LLMProvider] = dict(providers or {})
        self._lock = threading.Lock()

    def provider(self, name: str | None = None) -> LLMProvider:
        name = name or self.settings.default_provider
        with self._lock:
            if name not in self._providers:
                self._providers[name] = create_provider(name, self.settings)
            return self._providers[name]

    @staticmethod
    def providers() -> list[str]:
        return available()

    def info(self) -> list[dict[str, Any]]:
        """Provider state for display (never a secret)."""
        out = []
        for name in available():
            try:
                out.append(self.provider(name).info())
            except Exception as exc:  # noqa: BLE001 - a broken provider still shows up
                out.append({"name": name, "label": name, "model": "", "problem": str(exc)})
        return out

    def build_request(self, prompt: str = "", *, system: str | None = None, history: Iterable[Message] = (),
                      model: str | None = None, temperature: float | None = None,
                      max_tokens: int | None = None, extra: dict[str, Any] | None = None) -> ChatRequest:
        if not (prompt or "").strip():
            raise BadInput("send a prompt")
        messages: list[Message] = []
        if system:
            messages.append(Message("system", system))
        messages.extend(history)
        messages.append(Message("user", prompt))
        return ChatRequest(messages, model=model, temperature=temperature, max_tokens=max_tokens, extra=extra or {})

    def chat(self, prompt: str = "", *, provider: str | None = None, **kw) -> ChatResult:
        return self.provider(provider).chat(self.build_request(prompt, **kw))

    async def achat(self, prompt: str = "", *, provider: str | None = None, **kw) -> ChatResult:
        return await self.provider(provider).achat(self.build_request(prompt, **kw))

    async def achat_spec(self, spec: str, prompt: str, **kw) -> ChatResult:
        """Chat with a ``provider:model`` spec."""
        provider, model = parse_model_spec(spec, self.settings.default_provider)
        return await self.achat(prompt, provider=provider, model=model or None, **kw)

    def close(self) -> None:
        with self._lock:
            for p in self._providers.values():
                p.close()
            self._providers.clear()

    async def aclose(self) -> None:
        for p in list(self._providers.values()):
            await p.aclose()
        self._providers.clear()


_default: LLMClient | None = None
_default_lock = threading.Lock()


def get_client() -> LLMClient:
    global _default
    with _default_lock:
        if _default is None:
            _default = LLMClient()
        return _default


def set_client(client: LLMClient | None) -> None:
    """Replace the shared client (tests)."""
    global _default
    with _default_lock:
        _default = client


async def aclose_default() -> None:
    global _default
    with _default_lock:
        client, _default = _default, None
    if client is not None:
        await client.aclose()


__all__ = ["LLMClient", "LLMError", "ChatResult", "Settings", "get_client", "set_client", "parse_model_spec",
           "model_spec", "aclose_default"]
