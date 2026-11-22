"""backend/llm.yaml -> app.llm Settings: loading, several keys / accounts per provider, env merge, validation.

No network, no real llm.yaml (tests/conftest.py points LLM_CONFIG at a missing file; these tests pass paths)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings as AppSettings
from app.llm import LLMClient, Settings, set_client
from app.llm.config import CONFIG_FILE, EXAMPLE_FILE, ConfigError, config_path
from app.llm.keys import reset_shared_pools
from app.llm.providers import available
from app.llm.providers.claude_code import ClaudeCodeProvider
from app.llm.providers.groq import GroqProvider
from app.llm.providers.opencode import OpenCodeProvider
from app.llm.providers.openrouter import OpenRouterProvider
from app.llm.schema import ChatRequest, LLMError, Message
from app.main import app


@pytest.fixture(autouse=True)
def _fresh_pools():
    reset_shared_pools()
    yield
    reset_shared_pools()
    set_client(None)


def app_settings(**kw) -> AppSettings:
    """Backend settings without backend/.env and without the keys the test environment sets."""
    kw.setdefault("openrouter_api_key", "")
    return AppSettings(_env_file=None, **kw)


def write(tmp_path: Path, body: str) -> Path:
    file = tmp_path / "llm.yaml"
    file.write_text(body, encoding="utf-8")
    return file


FULL = """
default_provider: groq
timeout: 45
max_retries: 1
max_image_mb: 4
providers:
  openrouter:
    model: "x/model"
    thinking: off          # unquoted off is a YAML boolean: it still means "off"
    rotation: round_robin
    base_url: https://example.test/api/v1
    api_keys:
      - {name: main, key: sk-or-aaaa1111}
      - {name: backup, key: sk-or-bbbb2222}
      - sk-or-v1-...        # placeholder: ignored
  groq:
    model: llama-test
    api_keys: gsk_single
  claude_code:
    model: claude-sonnet-5-5
    thinking: high
    accounts:
      - {name: main}
      - {name: work, config_dir: "~/.claude-work"}
  opencode:
    model: github-copilot/x
    timeout: 99
    accounts:
      - {name: a, data_home: /data/a}
"""


# ---- the committed files ---------------------------------------------------------------------------------------

def test_example_file_is_valid_and_has_only_placeholder_keys():
    s = Settings.from_app(app_settings(), path=EXAMPLE_FILE)
    assert s.file == str(EXAMPLE_FILE)
    assert s.default_provider == "openrouter"
    for name in available():
        assert name in s.providers
        assert s.section(name).get("api_keys", []) == []  # every key in the example is a placeholder
    assert s.timeout == 120.0 and s.max_retries == 2 and s.max_image_mb == 10.0
    assert (s.section("openrouter")["model"], s.section("openrouter")["thinking"]) == ("openrouter/free", "off")
    assert s.section("openrouter")["rotation"] == "failover"
    assert s.section("groq")["model"] == "llama-3.3-70b-versatile" and s.section("groq")["thinking"] == ""
    assert s.section("hive")["model"] == "deepseek-ai/deepseek-v4.1-flash-1017"
    assert (s.section("claude_code")["model"], s.section("claude_code")["thinking"]) == ("claude-haiku-5-5", "low")
    assert (s.section("opencode")["model"], s.section("opencode")["thinking"]) == ("opencode/beam-free", "high")
    assert "isolated" not in s.section("opencode")  # commented out in the file: the provider default (true)


def test_llm_yaml_and_example_have_the_same_settings():
    if not CONFIG_FILE.is_file():
        pytest.skip("no local llm.yaml")
    a = Settings.from_app(app_settings(), path=CONFIG_FILE)
    b = Settings.from_app(app_settings(), path=EXAMPLE_FILE)
    assert a.providers == b.providers and a.timeout == b.timeout
    assert "sk-" not in CONFIG_FILE.read_text().replace("sk-or-v1-...", "")  # placeholders only


def test_default_path_is_backend_llm_yaml_and_llm_config_overrides_it():
    assert CONFIG_FILE.name == "llm.yaml" and CONFIG_FILE.parent == EXAMPLE_FILE.parent
    assert config_path("/tmp/x.yaml") == Path("/tmp/x.yaml")
    assert config_path(None, app_settings(llm_config="mine.yaml")) == CONFIG_FILE.parent / "mine.yaml"
    assert config_path(None, app_settings(llm_config="/abs/mine.yaml")) == Path("/abs/mine.yaml")


def test_tests_do_not_read_the_real_llm_yaml():
    assert config_path(None, None) != CONFIG_FILE  # conftest sets LLM_CONFIG to a missing temp file
    s = Settings.from_app()
    assert s.file == ""


# ---- loading ---------------------------------------------------------------------------------------------------

def test_loads_top_level_and_provider_sections(tmp_path):
    s = Settings.from_app(app_settings(), path=write(tmp_path, FULL))
    assert (s.default_provider, s.timeout, s.max_retries, s.max_image_mb) == ("groq", 45.0, 1, 4.0)
    assert s.max_image_bytes == 4 * 1024 * 1024
    o = s.section("openrouter")
    assert (o["model"], o["thinking"], o["rotation"], o["base_url"]) == ("x/model", "off", "round_robin", "https://example.test/api/v1")
    assert o["api_keys"] == [{"name": "main", "key": "sk-or-aaaa1111"}, {"name": "backup", "key": "sk-or-bbbb2222"}]
    assert s.section("groq")["api_keys"] == [{"name": "key 1", "key": "gsk_single"}]
    assert s.section("claude_code")["accounts"][1]["config_dir"].endswith(".claude-work")
    assert "~" not in s.section("claude_code")["accounts"][1]["config_dir"]
    assert s.section("opencode")["timeout"] == 99.0
    assert s.section("hive")["model"]  # not in the file: the app/config.py default


def test_missing_file_falls_back_to_env_settings(tmp_path):
    s = Settings.from_app(app_settings(groq_api_key="gsk_env"), path=tmp_path / "nope.yaml")
    assert s.file == ""
    assert s.section("groq")["api_keys"] == [{"name": "key 1", "key": "gsk_env"}]
    assert s.default_provider == "openrouter" and s.timeout == 60.0


def test_empty_file_is_fine(tmp_path):
    assert Settings.from_app(app_settings(), path=write(tmp_path, "# nothing\n")).default_provider == "openrouter"


def test_unknown_provider_and_field_are_ignored_with_a_warning(tmp_path, caplog):
    body = "providers:\n  mistral: {model: x}\n  groq: {model: g, colour: red}\nsurprise: 1\n"
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        s = Settings.from_app(app_settings(), path=write(tmp_path, body))
    assert "mistral" not in s.providers
    assert s.section("groq")["model"] == "g" and "colour" not in s.section("groq")
    text = caplog.text
    assert "providers.mistral" in text and "colour" in text and "surprise" in text


# ---- keys: rotation order ----------------------------------------------------------------------------------------

def _groq(settings: Settings, seen: list[str], refuse: tuple[str, ...] = ()) -> GroqProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"].removeprefix("Bearer ")
        seen.append(auth)
        if auth in refuse:
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "pong"}}]})

    return GroqProvider(settings, transport=httpx.MockTransport(handler))


KEYS = """
max_retries: 0
providers:
  groq:
    rotation: {rotation}
    api_keys:
      - {{name: one, key: gsk_1111}}
      - {{name: two, key: gsk_2222}}
      - {{name: three, key: gsk_3333}}
"""


def test_failover_uses_the_first_key_until_it_is_refused(tmp_path):
    s = Settings.from_app(app_settings(), path=write(tmp_path, KEYS.format(rotation="failover")))
    seen: list[str] = []
    p = _groq(s, seen, refuse=("gsk_1111",))
    assert [k.name for k in p.keys.keys] == ["one", "two", "three"]
    assert p.keys.rotation == "failover"
    for _ in range(3):
        assert LLMClient(s, providers={"groq": p}).chat("hi", provider="groq").text == "pong"
    assert seen == ["gsk_1111", "gsk_2222", "gsk_2222", "gsk_2222"]  # 1st refused -> 2nd in the same request, 1st rests
    assert [k["state"] for k in p.info()["keys"]] == ["resting", "active", "standby"]


def test_round_robin_takes_the_keys_in_turn(tmp_path):
    s = Settings.from_app(app_settings(), path=write(tmp_path, KEYS.format(rotation="round_robin")))
    seen: list[str] = []
    p = _groq(s, seen)
    client = LLMClient(s, providers={"groq": p})
    for _ in range(4):
        client.chat("hi", provider="groq")
    assert seen == ["gsk_1111", "gsk_2222", "gsk_3333", "gsk_1111"]


def test_all_keys_refused_gives_a_clear_error_and_problem(tmp_path):
    s = Settings.from_app(app_settings(), path=write(tmp_path, KEYS.format(rotation="failover")))
    p = _groq(s, [], refuse=("gsk_1111", "gsk_2222", "gsk_3333"))
    with pytest.raises(LLMError):
        p.chat(ChatRequest([Message("user", "hi")]))
    assert "all 3 keys unavailable" in p.problem()


# ---- env fallback and merge --------------------------------------------------------------------------------------

def test_env_keys_are_appended_after_the_yaml_keys_without_repeats(tmp_path):
    cfg = app_settings(openrouter_api_key="sk-or-env0000", openrouter_api_keys="sk-or-bbbb2222, sk-or-env1111")
    s = Settings.from_app(cfg, path=write(tmp_path, FULL))
    keys = [k["key"] for k in s.section("openrouter")["api_keys"]]
    assert keys == ["sk-or-aaaa1111", "sk-or-bbbb2222", "sk-or-env0000", "sk-or-env1111"]
    assert [k["name"] for k in s.section("openrouter")["api_keys"]][:2] == ["main", "backup"]


def test_env_keys_work_without_any_yaml(tmp_path):
    cfg = app_settings(groq_api_key="gsk_one", groq_api_keys="gsk_two, gsk_one ,gsk_three", hive_api_key="hive_k")
    s = Settings.from_app(cfg, path=tmp_path / "missing.yaml")
    assert [k["key"] for k in s.section("groq")["api_keys"]] == ["gsk_one", "gsk_two", "gsk_three"]
    assert [k["key"] for k in s.section("hive")["api_keys"]] == ["hive_k"]
    assert s.section("openrouter")["api_keys"] == []


def test_explicit_env_scalars_override_yaml_but_defaults_and_empty_do_not(tmp_path):
    file = write(tmp_path, FULL)
    s = Settings.from_app(app_settings(), path=file)
    assert s.section("openrouter")["model"] == "x/model"  # the app default does not hide the file
    assert s.timeout == 45.0
    s = Settings.from_app(app_settings(openrouter_model="env/model", llm_timeout_seconds=7, groq_model="",
                                       claude_code_thinking="max", llm_key_rotation="failover"), path=file)
    assert s.section("openrouter")["model"] == "env/model"
    assert s.timeout == 7.0
    assert s.section("groq")["model"] == "llama-test"  # empty env value: ignored
    assert s.section("claude_code")["thinking"] == "max"
    assert s.section("openrouter")["rotation"] == "failover"  # LLM_KEY_ROTATION wins over the section's round_robin
    assert s.section("groq")["rotation"] == "failover"


def test_overrides_argument_still_wins(tmp_path):
    s = Settings.from_app(app_settings(), path=write(tmp_path, FULL), timeout=5, providers={"groq": {"model": "o"}})
    assert s.timeout == 5 and s.section("groq")["model"] == "o" and s.section("groq")["api_keys"]


# ---- invalid files ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body, field", [
    ("providers: [", "line"),
    ("- just\n- a list\n", "must be a mapping"),
    ("default_provider: nope\n", "default_provider"),
    ("timeout: fast\n", "timeout"),
    ("timeout: -3\n", "timeout"),
    ("max_retries: 1.5\n", "max_retries"),
    ("max_retries: true\n", "max_retries"),
    ("providers: [a, b]\n", "providers must be a mapping"),
    ("providers:\n  groq: [x]\n", "providers.groq must be a mapping"),
    ("providers:\n  groq: {rotation: sideways}\n", "providers.groq.rotation"),
    ("providers:\n  groq: {thinking: extreme}\n", "providers.groq.thinking"),
    ("providers:\n  openrouter: {base_url: ftp://x}\n", "providers.openrouter.base_url"),
    ("providers:\n  groq: {model: [a]}\n", "providers.groq.model"),
    ("providers:\n  groq: {api_keys: [{name: a, kee: x}]}\n", "providers.groq.api_keys[0]"),
    ("providers:\n  groq: {api_keys: [[x]]}\n", "providers.groq.api_keys[0]"),
    ("providers:\n  groq: {api_keys: [{key: [x]}]}\n", "providers.groq.api_keys[0].key"),
    ("providers:\n  claude_code: {accounts: nope}\n", "providers.claude_code.accounts"),
    ("providers:\n  claude_code: {accounts: [{name: a, data_home: x}]}\n", "providers.claude_code.accounts[0]"),
    ("providers:\n  opencode: {accounts: [{config_dir: [x]}]}\n", "unknown field"),
])
def test_invalid_file_names_the_file_and_the_field(tmp_path, body, field):
    file = write(tmp_path, body)
    with pytest.raises(ConfigError) as err:
        Settings.from_app(app_settings(), path=file)
    assert str(file) in str(err.value)
    assert field in str(err.value)


def test_bad_yaml_is_reported_by_the_models_endpoint(tmp_path, monkeypatch):
    from app.config import get_settings

    file = write(tmp_path, "providers:\n  groq: {rotation: sideways}\n")
    monkeypatch.setattr(get_settings(), "llm_config", str(file))
    set_client(None)
    r = TestClient(app).get("/api/llm/models")
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == []
    assert {p["name"] for p in body["providers"]} == set(available())
    assert all(str(file) in p["problem"] and "providers.groq.rotation" in p["problem"] for p in body["providers"])
    chat = TestClient(app).post("/api/llm/chat", json={"model": "groq:x", "messages": [{"role": "user", "text": "hi"}]})
    assert chat.status_code == 503 and str(file) in chat.json()["detail"]


# ---- /api/llm/models problem field -------------------------------------------------------------------------------

def test_models_reports_no_key_for_placeholder_keys(tmp_path):
    s = Settings.from_app(app_settings(), path=write(tmp_path, FULL.replace("sk-or-aaaa1111", "sk-or-v1-...")
                                                     .replace("sk-or-bbbb2222", "sk-or-v1-...")))
    set_client(LLMClient(s, providers={
        "openrouter": OpenRouterProvider(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
        "groq": GroqProvider(s, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))),
    }))
    body = TestClient(app).get("/api/llm/models").json()
    by_name = {p["name"]: p for p in body["providers"]}
    assert by_name["openrouter"]["problem"].startswith("no OpenRouter API key")
    assert "llm.yaml" in by_name["openrouter"]["problem"]
    assert by_name["groq"]["problem"] is None  # gsk_single
    by_spec = {m["spec"]: m for m in body["models"]}
    assert by_spec["openrouter:x/model"]["available"] is False
    assert by_spec["groq:llama-test"]["available"] is True


# ---- CLI accounts ----------------------------------------------------------------------------------------------

def _ok_claude(text: str = "pong") -> subprocess.CompletedProcess:
    out = json.dumps({"type": "result", "result": text, "is_error": False})
    return subprocess.CompletedProcess([], 0, out + "\n", "")


def _err_claude(message: str, status: int | None = None) -> subprocess.CompletedProcess:
    out = json.dumps({"type": "result", "result": message, "is_error": True, "api_error_status": status})
    return subprocess.CompletedProcess([], 1, out + "\n", "")


CLAUDE = """
max_retries: 0
providers:
  claude_code:
    bin: sh
    rotation: {rotation}
    accounts:
      - {{name: main}}
      - {{name: work, config_dir: /acct/work}}
      - {{name: spare, config_dir: /acct/spare}}
"""


def _claude(tmp_path, monkeypatch, replies: list, rotation: str = "failover"):
    s = Settings.from_app(app_settings(), path=write(tmp_path, CLAUDE.format(rotation=rotation)))
    calls: list[str | None] = []

    def fake_run(args, **kw):
        calls.append(kw["env"].get("CLAUDE_CONFIG_DIR"))
        reply = replies.pop(0) if replies else _ok_claude()
        return reply

    monkeypatch.setattr("app.llm.providers.claude_code.subprocess.run", fake_run)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return ClaudeCodeProvider(s), calls


def test_claude_code_without_accounts_is_unchanged(monkeypatch):
    s = Settings(max_retries=0, providers={"claude_code": {"bin": "claude-test-bin"}})
    p = ClaudeCodeProvider(s)
    assert p.accounts.pool is None and "accounts" not in p.info()
    seen = []
    monkeypatch.setattr("app.llm.providers.claude_code.subprocess.run", lambda a, **kw: (seen.append(kw["env"]), _ok_claude())[1])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/inherited")
    assert p.chat(ChatRequest([Message("user", "hi")])).text == "pong"
    assert seen[0]["CLAUDE_CONFIG_DIR"] == "/inherited"


def test_claude_code_accounts_fail_over_on_usage_limit_and_auth_errors(tmp_path, monkeypatch):
    p, calls = _claude(tmp_path, monkeypatch, [
        _err_claude("Claude AI usage limit reached|4102444800"),   # main: limit -> rests
        _err_claude("Invalid API key / Please run /login"),         # work: login refused -> rests
    ])
    req = ChatRequest([Message("user", "hi")])
    assert p.chat(req).text == "pong"
    assert calls == [None, "/acct/work", "/acct/spare"]  # main uses the default login (no env), then each account's own dir
    assert p.chat(req).text == "pong" and calls[3:] == ["/acct/spare"]  # the two failed accounts rest
    states = {a["name"]: a for a in p.info()["accounts"]}
    assert states["main"]["state"] == "resting" and "usage limit" in states["main"]["reason"]
    assert states["work"]["state"] == "resting" and states["spare"]["state"] == "active"
    assert states["work"]["masked"] == "CLAUDE_CONFIG_DIR=/acct/work"
    assert p.problem() is None


def test_claude_code_accounts_round_robin(tmp_path, monkeypatch):
    p, calls = _claude(tmp_path, monkeypatch, [], rotation="round_robin")
    req = ChatRequest([Message("user", "hi")])
    for _ in range(4):
        p.chat(req)
    assert calls == [None, "/acct/work", "/acct/spare", None]


def test_claude_code_timeouts_and_other_errors_do_not_rotate(tmp_path, monkeypatch):
    p, calls = _claude(tmp_path, monkeypatch, [_err_claude("something odd happened")])
    with pytest.raises(LLMError, match="something odd"):
        p.chat(ChatRequest([Message("user", "hi")]))
    assert calls == [None]
    assert p.info()["accounts"][0]["state"] != "resting"  # not the account's fault


def test_claude_code_all_accounts_resting_is_a_problem(tmp_path, monkeypatch):
    p, calls = _claude(tmp_path, monkeypatch, [_err_claude("usage limit reached")] * 3)
    with pytest.raises(LLMError, match="usage limit"):
        p.chat(ChatRequest([Message("user", "hi")]))
    assert len(calls) == 3
    assert "all 3 accounts unavailable" in p.problem()
    with pytest.raises(LLMError, match="all 3 accounts unavailable"):
        p.chat(ChatRequest([Message("user", "hi")]))


async def test_claude_code_accounts_work_in_async_too(tmp_path, monkeypatch):
    s = Settings.from_app(app_settings(), path=write(tmp_path, CLAUDE.format(rotation="failover")))
    p = ClaudeCodeProvider(s)
    calls: list = []
    replies = [_err_claude("usage limit reached"), _ok_claude("async pong")]

    class FakeProc:
        returncode = 0

        def __init__(self, reply):
            self.reply = reply

        async def communicate(self, _input=None):
            return self.reply.stdout.encode(), b""

    async def fake_exec(*args, **kw):
        calls.append(kw["env"].get("CLAUDE_CONFIG_DIR"))
        reply = replies.pop(0)
        proc = FakeProc(reply)
        proc.returncode = reply.returncode
        return proc

    monkeypatch.setattr("app.llm.providers.claude_code.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert (await p.achat(ChatRequest([Message("user", "hi")]))).text == "async pong"
    assert calls == [None, "/acct/work"]


def test_opencode_accounts_get_their_own_xdg_env_and_fail_over(tmp_path, monkeypatch):
    body = """
max_retries: 0
providers:
  opencode:
    bin: sh
    isolated: false
    accounts:
      - {name: a}
      - {name: b, data_home: /acct/b-data, config_home: /acct/b-config}
"""
    p = OpenCodeProvider(Settings.from_app(app_settings(), path=write(tmp_path, body)))
    calls: list[tuple] = []
    outputs = [
        json.dumps({"type": "error", "error": {"type": "provider.quota", "message": "quota exhausted"}}),
        json.dumps({"type": "text", "part": {"type": "text", "text": "hello"}}),
    ]

    def fake_run(args, **kw):
        calls.append((kw["env"].get("XDG_DATA_HOME"), kw["env"].get("XDG_CONFIG_HOME")))
        return subprocess.CompletedProcess(args, 0, outputs.pop(0) if outputs else outputs_ok, "")

    outputs_ok = outputs[1]
    monkeypatch.setattr("app.llm.providers.opencode.subprocess.run", fake_run)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    req = ChatRequest([Message("user", "hi")])
    assert p.chat(req).text == "hello"
    assert calls == [(None, None), ("/acct/b-data", "/acct/b-config")]
    assert p.chat(req).text == "hello" and calls[2] == ("/acct/b-data", "/acct/b-config")  # account a rests (quota)
    assert [a["state"] for a in p.info()["accounts"]] == ["resting", "active"]
