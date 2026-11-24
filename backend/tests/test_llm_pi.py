"""pi provider: command building (the toggled flags), JSON event parsing, thinking, setup problems
(no network, no real process)."""

from __future__ import annotations

import json

import pytest

from app.llm import parse_model_spec
from app.llm.config import Settings
from app.llm.providers import available, create_provider
from app.llm.providers.pi import PiProvider
from app.llm.schema import BadInput, ChatRequest, LLMError, Message

BIN = "/fake/bin/pi"


def events(*items: dict) -> str:
    return "\n".join(json.dumps(i) for i in items) + "\n"


def msg(**kw) -> dict:
    return {"role": "assistant", "model": "google/gemini-3-pro", "stopReason": "stop",
            "content": [{"type": "text", "text": "Hi!"}],
            "usage": {"input": 3, "output": 23, "cacheRead": 10, "cacheWrite": 100,
                      "totalTokens": 136, "cost": {"total": 0.5}}, **kw}


def make(**cfg) -> PiProvider:
    return PiProvider(Settings(max_retries=cfg.pop("max_retries", 0), providers={"pi": cfg}))


@pytest.fixture
def fake_binary(monkeypatch):
    import app.llm.providers.pi as mod
    monkeypatch.setattr(mod.shutil, "which", lambda name: BIN if name == "pi" else None)
    yield


def req(*msgs: Message, model: str | None = None, extra: dict | None = None) -> ChatRequest:
    return ChatRequest(list(msgs), model=model, extra=extra or {})


# ---- registry / spec -----------------------------------------------------------------------------------------------

def test_registered_with_defaults():
    assert "pi" in available()
    p = create_provider("pi", Settings())
    assert isinstance(p, PiProvider)
    assert p.label == "pi agent"
    assert p.flags == {name: True for name, _ in [
        ("no_extensions", 0), ("no_skills", 0), ("no_prompt_templates", 0),
        ("no_context_files", 0), ("no_mcp", 0), ("no_tools", 0), ("no_themes", 0)]}
    assert p.timeout == 180.0


def test_spec_splits_on_the_first_colon():
    assert parse_model_spec("pi:google/gemini-3-pro") == ("pi", "google/gemini-3-pro")


# ---- command -------------------------------------------------------------------------------------------------------

def test_build_command_has_all_the_example_flags_by_default(fake_binary):
    args, _ = make().build_command(req(Message("user", "say hi")))
    assert args == [BIN, "--print", "--mode", "json", "--no-session",
                    "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files",
                    "--no-mcp", "--no-tools", "--no-themes",
                    "--system-prompt", "You are a helpful assistant.", "--", "say hi"]


def test_flags_can_be_turned_off(fake_binary):
    args, _ = make(flags={"no_mcp": False, "no_tools": False}).build_command(req(Message("user", "x")))
    assert "--no-mcp" not in args and "--no-tools" not in args
    assert "--no-extensions" in args and "--no-themes" in args


def test_model_and_thinking_are_passed_when_set(fake_binary):
    args, _ = make(model="google/gemini-3-pro").build_command(
        req(Message("user", "x")), )
    assert args[args.index("--model") + 1] == "google/gemini-3-pro"
    args, _ = make(thinking="high").build_command(req(Message("user", "x")))
    assert args[args.index("--thinking") + 1] == "high"
    args, _ = make().build_command(req(Message("user", "x"), extra={"effort": "low"}))
    assert args[args.index("--thinking") + 1] == "low"


def test_request_system_message_wins_over_the_configured_one(fake_binary):
    args, _ = make(system_prompt="fallback").build_command(req(Message("system", "Be brief."), Message("user", "x")))
    assert args[args.index("--system-prompt") + 1] == "Be brief."
    args, _ = make(system_prompt="fallback").build_command(req(Message("user", "x")))
    assert args[args.index("--system-prompt") + 1] == "fallback"


def test_history_is_folded_into_one_prompt(fake_binary):
    args, _ = make().build_command(req(
        Message("user", "hi"), Message("assistant", "hello"), Message("user", "again?")))
    assert args[-1] == "[earlier user message]\nhi\n\n[earlier assistant message]\nhello\n\n[user message]\nagain?"


def test_bad_thinking_is_rejected(fake_binary):
    with pytest.raises(BadInput):
        make(thinking="turbo").build_command(req(Message("user", "x")))


# ---- output parsing ------------------------------------------------------------------------------------------------

def test_parse_output_text_usage_and_reasoning():
    out = events(
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "q"}]}},
        {"type": "message_end", "message": msg(content=[
            {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Hi"}])},
    )
    r = make(model="google/gemini-3-pro").parse_output(out)
    assert r.text == "Hi"
    assert r.model == "google/gemini-3-pro" and r.provider == "pi"
    assert r.finish_reason == "stop"
    assert r.reasoning == "hmm"
    assert r.usage["prompt_tokens"] == 113 and r.usage["completion_tokens"] == 23
    assert r.usage["total_tokens"] == 136 and r.usage["cost_usd"] == 0.5


def test_parse_output_error_raises_with_status():
    with pytest.raises(LLMError) as e:
        make().parse_output(events({"type": "message_end", "message": msg(
            stopReason="error", content=[], errorMessage="HTTP 429: rate limited")}))
    assert e.value.status_code == 429


def test_parse_output_no_answer():
    with pytest.raises(LLMError) as e:
        make().parse_output("", "boom", 1)
    assert "boom" in e.value.message


# ---- setup ---------------------------------------------------------------------------------------------------------

def test_problem_names_the_missing_binary(monkeypatch):
    import app.llm.providers.pi as mod
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    p = make()
    assert "not found" in (p.problem() or "")
