"""OpenCode provider: command building, JSON event parsing, model list cache, setup problems (no network, no real process)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from app.llm import parse_model_spec
from app.llm.config import Settings
from app.llm.providers import available, create_provider
from app.llm.providers import opencode as oc
from app.llm.providers.opencode import OpenCodeProvider, parse_models
from app.llm.schema import BadInput, ChatRequest, LLMError, Message

BIN = "/fake/bin/opencode"
MODELS_OUT = """9router/deepseek
9router/kc/minimax/minimax-m3:free
github-copilot/gpt-5.5
github-copilot/claude-sonnet-5.5
opencode/beam-free
not a model line
github-copilot/gpt-5.5
"""


def events(*items: dict) -> str:
    return "\n".join(json.dumps(i) for i in items) + "\n"


def ok_events(text: str = "Hi!", **extra) -> str:
    return events(
        {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}},
        {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": text}},
        {"type": "step_finish", "sessionID": "ses_1",
         "part": {"type": "step-finish", "reason": "stop", "cost": 0.5,
                  "tokens": {"input": 3, "output": 23, "reasoning": 5, "cache": {"read": 10, "write": 100}}}},
        *extra.get("more", []),
    )


def make(**cfg) -> OpenCodeProvider:
    return OpenCodeProvider(Settings(max_retries=cfg.pop("max_retries", 0), providers={"opencode": cfg}))


@pytest.fixture(autouse=True)
def fake_binary(monkeypatch):
    oc.clear_models_cache()
    monkeypatch.setattr(oc.shutil, "which", lambda name: BIN if name == "opencode" else None)
    yield
    oc.clear_models_cache()


def req(*msgs: Message, model: str | None = "github-copilot/auto") -> ChatRequest:
    return ChatRequest(list(msgs), model=model)


# ---- registry / spec -----------------------------------------------------------------------------------------------

def test_registered_with_label_and_defaults():
    assert "opencode" in available()
    p = create_provider("opencode", Settings())
    assert p.label == "OpenCode"
    assert p.model == "opencode/beam-free"
    assert p.timeout == 180.0


def test_settings_from_app_have_an_opencode_section():
    cfg = Settings.from_app().section("opencode")
    assert cfg["model"] == "opencode/beam-free"
    assert cfg["timeout"] == 180.0


def test_spec_splits_only_on_the_first_colon():
    assert parse_model_spec("opencode:github-copilot/claude-sonnet-5.5") == ("opencode", "github-copilot/claude-sonnet-5.5")
    assert parse_model_spec("opencode:9router/kc/minimax/minimax-m3:free") == ("opencode", "9router/kc/minimax/minimax-m3:free")


# ---- command -------------------------------------------------------------------------------------------------------

def test_build_command_single_message():
    args, _ = make().build_command(req(Message("user", "say hi")))
    assert args == [BIN, "run", "--standalone", "--format", "json", "-m", "github-copilot/auto", "--", "say hi"]
    assert "--auto" not in args


def test_build_command_uses_default_model_when_request_has_none():
    args, _ = make(model="9router/glm").build_command(req(Message("user", "x"), model=None))
    assert args[args.index("-m") + 1] == "9router/glm"


def test_build_message_folds_system_and_history():
    msg = OpenCodeProvider.build_message(req(
        Message("system", "Be brief."), Message("user", "hi"), Message("assistant", "hello"), Message("user", "again?")))
    assert msg == ("[system instructions]\nBe brief.\n\n[earlier user message]\nhi\n\n"
                   "[earlier assistant message]\nhello\n\n[user message]\nagain?")


def test_build_message_without_system_or_history_is_the_plain_text():
    assert OpenCodeProvider.build_message(req(Message("user", "ping"))) == "ping"


def test_prompt_starting_with_a_dash_stays_a_positional_argument():
    args, _ = make().build_command(req(Message("user", "--help")))
    assert args[-2:] == ["--", "--help"]


def test_images_are_attached_as_files(tmp_path):
    args, _ = make().build_command(req(Message("user", "look", images=["data:image/png;base64,iVBORw0K"])),
                                   image_dir=str(tmp_path))
    i = args.index("-f")
    assert args[i + 1].endswith("image-1.png") and (tmp_path / "image-1.png").read_bytes()
    assert args.index("--") > i
    with pytest.raises(BadInput):
        make().build_command(req(Message("user", "look", images=["not a data url"])), image_dir=str(tmp_path))


def test_empty_prompts_are_bad_input():
    with pytest.raises(BadInput):
        make().build_command(req(Message("system", "only system")))


# ---- parsing -------------------------------------------------------------------------------------------------------

def test_parse_output_text_usage_and_session():
    r = make().parse_output(ok_events("Hi!"), model="github-copilot/auto")
    assert r.text == "Hi!"
    assert r.model == "github-copilot/auto"
    assert r.provider == "opencode"
    assert r.finish_reason == "stop"
    assert r.id == "ses_1"
    assert r.reasoning is None
    assert r.usage["input_tokens"] == 3 and r.usage["output_tokens"] == 23 and r.usage["reasoning_tokens"] == 5
    assert r.usage["prompt_tokens"] == 113 and r.usage["completion_tokens"] == 23 and r.usage["total_tokens"] == 136
    assert r.usage["cost_usd"] == 0.5


def test_parse_output_joins_text_parts_and_collects_reasoning():
    out = events(
        {"type": "reasoning", "part": {"type": "reasoning", "text": "think 1"}},
        {"type": "text", "part": {"type": "text", "text": "A"}},
        {"type": "reasoning", "part": {"type": "reasoning", "text": "think 2"}},
        {"type": "text", "part": {"type": "text", "text": "B"}},
    )
    r = make().parse_output("not json\n" + out)
    assert r.text == "AB"
    assert r.reasoning == "think 1\nthink 2"
    assert r.usage == {}


def test_parse_output_error_event_maps_to_llm_error():
    out = events({"type": "error", "error": {"type": "provider.no-route", "message": "Model unavailable: x/y"}})
    with pytest.raises(LLMError) as e:
        make().parse_output(out)
    assert "Model unavailable: x/y" in str(e.value)
    assert e.value.provider == "opencode"


def test_parse_output_quota_error_carries_its_status():
    out = events({"type": "error", "error": {"type": "provider.quota", "message": "Insufficient balance", "status": 402}})
    with pytest.raises(LLMError) as e:
        make().parse_output(out)
    assert e.value.status_code == 402
    assert "402" in str(e.value)


def test_parse_output_without_answer_uses_stderr():
    with pytest.raises(LLMError, match="no answer: boom"):
        make().parse_output("", "boom\n", 1)
    with pytest.raises(LLMError, match="exit code 3"):
        make().parse_output("", "", 3)


# ---- running (subprocess mocked) -----------------------------------------------------------------------------------

def test_chat_runs_the_cli_in_a_temp_cwd_and_returns_the_result(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(stdout=ok_events("OK"), stderr="", returncode=0)

    monkeypatch.setattr(oc.subprocess, "run", fake_run)
    r = make(timeout=42).chat(req(Message("user", "Reply with OK"), model="github-copilot/auto"))

    assert r.text == "OK" and r.model == "github-copilot/auto"
    args, kw = calls[0]
    assert args[:7] == [BIN, "run", "--standalone", "--format", "json", "-m", "github-copilot/auto"]
    assert kw["timeout"] == 42
    assert kw["cwd"].endswith("fxnews-opencode")
    assert kw["stdin"] == subprocess.DEVNULL


def test_chat_timeout_and_missing_binary_are_llm_errors(monkeypatch):
    def timeout(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(oc.subprocess, "run", timeout)
    with pytest.raises(LLMError, match="timed out"):
        make().chat(req(Message("user", "hi")))

    def missing(args, **kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(oc.subprocess, "run", missing)
    with pytest.raises(LLMError, match="cannot run OpenCode"):
        make().chat(req(Message("user", "hi")))


def test_chat_retries_rate_limits_only(monkeypatch):
    monkeypatch.setattr(oc.time, "sleep", lambda s: None)
    outputs = [
        events({"type": "error", "error": {"type": "provider.rate-limit", "message": "slow down"}}),
        ok_events("second try"),
    ]
    runs = []

    def fake_run(args, **kw):
        runs.append(1)
        return SimpleNamespace(stdout=outputs[len(runs) - 1], stderr="", returncode=0)

    monkeypatch.setattr(oc.subprocess, "run", fake_run)
    assert make(max_retries=2).chat(req(Message("user", "hi"))).text == "second try"

    runs.clear()
    outputs[:] = [events({"type": "error", "error": {"type": "provider.no-route", "message": "Model unavailable"}})] * 3
    with pytest.raises(LLMError):
        make(max_retries=2).chat(req(Message("user", "hi")))
    assert len(runs) == 1  # a 404 is not retried


def test_achat_uses_an_async_subprocess(monkeypatch):
    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return ok_events("async ok").encode(), b""

    async def fake_exec(*args, **kw):
        seen["args"], seen["kw"] = args, kw
        return FakeProc()

    monkeypatch.setattr(oc.asyncio, "create_subprocess_exec", fake_exec)
    r = asyncio.run(make().achat(req(Message("user", "hi"))))

    assert r.text == "async ok"
    assert seen["args"][:2] == (BIN, "run")
    assert seen["kw"]["cwd"].endswith("fxnews-opencode")


def test_achat_timeout_kills_the_process(monkeypatch):
    state = {"killed": False}

    class FakeProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            state["killed"] = True

        async def wait(self):
            return 0

    async def fake_exec(*args, **kw):
        return FakeProc()

    monkeypatch.setattr(oc.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(LLMError, match="timed out"):
        asyncio.run(make(timeout=0.05).achat(req(Message("user", "hi"))))
    assert state["killed"]


# ---- problem / binary lookup ---------------------------------------------------------------------------------------

def test_problem_when_binary_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(oc.shutil, "which", lambda name: None)
    monkeypatch.setattr(oc.Path, "home", classmethod(lambda cls: tmp_path))
    p = make()
    assert "not found" in p.problem()
    assert p.info()["problem"] == p.problem()
    assert p.suggested_models()[0]["id"] == "opencode/beam-free"  # only the default, no process run


def test_binary_falls_back_to_bun_bin(monkeypatch, tmp_path):
    bun = tmp_path / ".bun" / "bin"
    bun.mkdir(parents=True)
    (bun / "opencode").write_text("#!/bin/sh\n")
    monkeypatch.setattr(oc.shutil, "which", lambda name: None)
    monkeypatch.setattr(oc.Path, "home", classmethod(lambda cls: tmp_path))
    p = make()
    assert p.problem() is None
    assert p._binary() == str(bun / "opencode")


def test_configured_bin_is_looked_up_not_the_fallback(monkeypatch):
    monkeypatch.setattr(oc.shutil, "which", lambda name: "/custom/oc" if name == "/custom/oc" else None)
    assert make(bin="/custom/oc")._binary() == "/custom/oc"
    assert make(bin="/missing/oc").problem() is not None


# ---- models --------------------------------------------------------------------------------------------------------

def test_parse_models_keeps_only_provider_slash_model_lines():
    assert parse_models(MODELS_OUT) == [
        "9router/deepseek", "9router/kc/minimax/minimax-m3:free", "github-copilot/gpt-5.5",
        "github-copilot/claude-sonnet-5.5", "opencode/beam-free"]


def test_suggested_models_lists_cli_models_with_default_first_and_caches(monkeypatch):
    runs = []

    def fake_run(args, **kw):
        runs.append(args)
        return SimpleNamespace(stdout=MODELS_OUT, stderr="", returncode=0)

    monkeypatch.setattr(oc.subprocess, "run", fake_run)
    p = make(model="github-copilot/auto")
    models = p.suggested_models()

    assert runs == [[BIN, "models", "--standalone"]]
    ids = [m["id"] for m in models]
    assert ids[0] == "github-copilot/auto"  # the configured default is not in the CLI list, so it leads
    assert "github-copilot/claude-sonnet-5.5" in ids and "9router/kc/minimax/minimax-m3:free" in ids
    free = {m["id"]: m["free"] for m in models}
    assert free["9router/kc/minimax/minimax-m3:free"] and free["opencode/beam-free"] and not free["9router/deepseek"]

    p.suggested_models()
    assert len(runs) == 1  # cached

    monkeypatch.setattr(oc.time, "monotonic", lambda: 10_000_000.0)  # past the ~10 minute TTL
    p.suggested_models()
    assert len(runs) == 2


def test_models_cli_failure_gives_only_the_default(monkeypatch):
    monkeypatch.setattr(oc.subprocess, "run", lambda args, **kw: SimpleNamespace(stdout="", stderr="bad", returncode=1))
    assert [m["id"] for m in make().suggested_models()] == ["opencode/beam-free"]

    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(oc.subprocess, "run", boom)
    assert [m["id"] for m in make().suggested_models()] == ["opencode/beam-free"]


# ---- thinking variant + isolated mode ------------------------------------------------------------------------------

def test_thinking_becomes_the_model_variant():
    args, _ = make(thinking="high").build_command(req(Message("user", "x"), model="opencode/beam-free"))
    assert args[args.index("-m") + 1] == "opencode/beam-free#high"
    for off in ("", "off"):
        args, _ = make(thinking=off).build_command(req(Message("user", "x"), model="opencode/beam-free"))
        assert args[args.index("-m") + 1] == "opencode/beam-free"
    args, _ = make(thinking="high").build_command(req(Message("user", "x"), model="opencode/beam-free#low"))
    assert args[args.index("-m") + 1] == "opencode/beam-free#low"  # the model's own variant wins
    r = ChatRequest([Message("user", "x")], model="a/b", extra={"reasoning": {"effort": "low"}})
    assert make(thinking="high").build_command(r)[0][-3] == "a/b#low"  # a request's effort wins


def test_isolated_runs_standalone_with_an_empty_opencode_config():
    args, env = make().build_command(req(Message("user", "x")))
    assert "--standalone" in args
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1" and env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    config = env["XDG_CONFIG_HOME"]
    assert config.endswith(".config") and not [f for f in os.listdir(config) if f.startswith("opencode.json")]  # no global config


def test_isolated_false_uses_the_machines_opencode(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/me/.config")
    args, env = make(isolated=False).build_command(req(Message("user", "x")))
    assert "--standalone" not in args
    assert env["XDG_CONFIG_HOME"] == "/home/me/.config" and "OPENCODE_DISABLE_PROJECT_CONFIG" not in env
