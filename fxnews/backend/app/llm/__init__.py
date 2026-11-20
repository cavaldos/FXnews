"""Provider-agnostic LLM layer (OpenRouter, Groq, Hive, Claude Code CLI), ported from Bot_Trade/llm_service.

The older ``app.analysis.llm`` (OpenRouter JSON helper used by the live advisor) is untouched; this package is what
the backtest model runner uses so several providers/models can be compared on the same events.
"""

from .client import LLMClient, aclose_default, get_client, model_spec, parse_model_spec, set_client
from .config import Settings
from .schema import BadInput, ChatRequest, ChatResult, LLMError, Message

__all__ = ["LLMClient", "Settings", "LLMError", "BadInput", "ChatRequest", "ChatResult", "Message", "get_client",
           "set_client", "parse_model_spec", "model_spec", "aclose_default"]
