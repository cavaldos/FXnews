from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..schema import BadInput, ChatRequest, ChatResult


class LLMProvider(ABC):
    """One backend (OpenRouter, Claude Code, ...). Images in requests are already data/http URLs.

    Its settings are the section providers.<name> of the Settings built from app.config (settings.section(name)).
    """

    name: str = ""
    label: str = ""                         # shown on the web
    thinking_levels: tuple[str, ...] = ()   # values of `thinking` in the settings ("" = the model's default)
    model: str = ""                         # default model (providers.<name>.model)
    thinking: str = ""                      # default thinking level (providers.<name>.thinking)

    @abstractmethod
    def chat(self, req: ChatRequest) -> ChatResult: ...

    @abstractmethod
    async def achat(self, req: ChatRequest) -> ChatResult: ...

    def list_models(self) -> list[dict[str, Any]]:
        return []

    def suggested_models(self) -> list[dict[str, Any]]:
        """Models to offer in a picker: [{id, name, vision, free}]."""
        return []

    def thinking_choices(self) -> list[str] | None:
        """Thinking levels a request may ask for; None = any (the provider checks), [] = no thinking option."""
        return list(self.thinking_levels)

    def thinking_extra(self, level: str | None) -> dict[str, Any]:
        """`ChatRequest.extra` that makes this provider use the thinking `level` ("" / None = keep the settings' one).

        The default speaks the `effort` key that claude_code and opencode read; OpenAI-compatible providers
        override it with their own payload option.
        """
        level = (level or "").strip().lower()
        if not level:
            return {}
        choices = self.thinking_choices()
        if choices is not None and level not in choices:
            hint = f"use one of {', '.join(choices)}" if choices else "it has no thinking option"
            raise BadInput(f"{self.label or self.name} does not accept thinking '{level}': {hint}")
        return {"effort": level}

    def problem(self) -> str | None:
        """A setup problem that makes every request fail (missing key, missing CLI), or None."""
        return None

    def info(self) -> dict[str, Any]:
        """Settings and state for display (never a secret)."""
        try:
            problem = self.problem()
        except Exception as exc:  # a broken provider still shows up
            problem = str(exc)
        return {
            "name": self.name,
            "label": self.label or self.name,
            "model": self.model,
            "thinking": self.thinking,
            "thinkingLevels": list(self.thinking_levels),
            "problem": problem,
        }

    def close(self) -> None:
        pass

    async def aclose(self) -> None:
        self.close()
