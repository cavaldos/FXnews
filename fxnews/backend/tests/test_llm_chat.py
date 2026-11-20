"""POST /api/llm/chat and GET /api/llm/models, against a fake provider (no network, no keys)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.llm import LLMClient, Settings, set_client
from app.llm.providers.claude_code import ClaudeCodeProvider
from app.llm.providers.base import LLMProvider
from app.llm.schema import ChatRequest, ChatResult, LLMError, Message
from app.main import app


class FakeProvider(LLMProvider):
    def __init__(self, name: str, *, model: str = "fake-model", problem: str | None = None,
                 error: Exception | None = None, suggested: tuple[str, ...] = ()):
        self.suggested = suggested
        self.name = name
        self.label = name.title()
        self.model = model
        self._problem = problem
        self._error = error
        self.requests: list[ChatRequest] = []

    def problem(self) -> str | None:
        return self._problem

    def suggested_models(self) -> list[dict]:
        return [{"id": i} for i in self.suggested]

    def _answer(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        if self._error is not None:
            raise self._error
        last = req.messages[-1].text
        return ChatResult(text=f"echo: {last}", model=req.model or self.model, provider=self.name,
                          finish_reason="stop", usage={"prompt_tokens": len(req.messages)}, reasoning="thinking")

    def chat(self, req: ChatRequest) -> ChatResult:
        return self._answer(req)

    async def achat(self, req: ChatRequest) -> ChatResult:
        return self._answer(req)


@pytest.fixture
def fake_openrouter() -> FakeProvider:
    provider = FakeProvider("openrouter")
    set_client(LLMClient(providers={"openrouter": provider, "groq": FakeProvider("groq", problem="no GROQ key")}))
    yield provider
    set_client(None)


@pytest.fixture
def client(fake_openrouter):
    return TestClient(app)


def test_models_lists_backtest_models_and_defaults_without_duplicates(client):
    body = client.get("/api/llm/models").json()

    specs = [m["spec"] for m in body["models"]]
    assert len(specs) == len(set(specs)), "models must be deduplicated"
    assert "openrouter:fake-model" in specs  # the provider's default model is listed too
    assert "claude_code:claude-haiku-5-5" in specs  # listed once even if both BACKTEST_MODELS and the default have it
    for m in body["models"]:
        assert m["spec"] == f"{m['provider']}:{m['model']}"
        assert isinstance(m["available"], bool)


def test_models_marks_models_of_providers_with_a_problem_unavailable(client):
    body = client.get("/api/llm/models").json()

    providers = {p["name"]: p for p in body["providers"]}
    assert providers["openrouter"]["problem"] is None
    assert providers["groq"]["problem"] == "no GROQ key"
    by_spec = {m["spec"]: m for m in body["models"]}
    assert by_spec["openrouter:fake-model"]["available"] is True
    for m in body["models"]:
        if m["provider"] == "groq":
            assert m["available"] is False


def test_chat_sends_system_history_and_last_user_message(client, fake_openrouter):
    payload = {
        "model": "openrouter:fake-model",
        "system": "Be brief.",
        "messages": [
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "hello"},
            {"role": "user", "text": "how are you?"},
        ],
        "temperature": 0.2,
        "max_tokens": 50,
    }
    resp = client.post("/api/llm/chat", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "echo: how are you?"
    assert body["model"] == "fake-model"
    assert body["provider"] == "openrouter"
    assert body["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 4}
    assert body["reasoning"] == "thinking"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0

    sent = fake_openrouter.requests[-1]
    assert [(m.role, m.text) for m in sent.messages] == [
        ("system", "Be brief."), ("user", "hi"), ("assistant", "hello"), ("user", "how are you?")]
    assert sent.model == "fake-model"
    assert sent.temperature == 0.2
    assert sent.max_tokens == 50


def test_chat_without_system_or_history(client, fake_openrouter):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model",
                                              "messages": [{"role": "user", "text": "ping"}], "system": None})

    assert resp.status_code == 200
    assert [(m.role, m.text) for m in fake_openrouter.requests[-1].messages] == [("user", "ping")]


def test_last_message_must_be_from_user(client):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model",
                                              "messages": [{"role": "user", "text": "hi"},
                                                           {"role": "assistant", "text": "hello"}]})

    assert resp.status_code == 400
    assert "user" in resp.json()["detail"]


def test_empty_last_message_is_bad_input(client):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model",
                                              "messages": [{"role": "user", "text": "   "}]})

    assert resp.status_code == 400
    assert resp.json()["detail"]


def test_spec_without_model_name_is_bad_input(client):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:",
                                              "messages": [{"role": "user", "text": "hi"}]})

    assert resp.status_code == 400
    assert "openrouter:" in resp.json()["detail"]


def test_provider_without_key_returns_503(client):
    resp = client.post("/api/llm/chat", json={"model": "groq:llama",
                                              "messages": [{"role": "user", "text": "hi"}]})

    assert resp.status_code == 503
    assert resp.json()["detail"] == "no GROQ key"


def test_provider_failure_returns_502(client, fake_openrouter):
    fake_openrouter._error = LLMError("upstream exploded", status_code=500, provider="openrouter")

    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model",
                                              "messages": [{"role": "user", "text": "hi"}]})

    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream exploded"


def test_temperature_out_of_range_is_rejected_by_validation(client):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model", "temperature": 9,
                                              "messages": [{"role": "user", "text": "hi"}]})

    assert resp.status_code == 422


def test_models_includes_every_providers_suggested_models(fake_openrouter):
    fake_openrouter.suggested = ("fake-model", "other/model:free", "x-1")  # fake-model repeats the default
    groq = FakeProvider("groq", problem="no GROQ key", suggested=("llama",))
    broken = FakeProvider("hive")
    broken.suggested_models = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    oc = FakeProvider("opencode", model="github-copilot/auto",
                      suggested=("github-copilot/auto", "9router/kc/minimax/minimax-m3:free"))
    set_client(LLMClient(providers={"openrouter": fake_openrouter, "groq": groq, "hive": broken, "opencode": oc}))

    body = TestClient(app).get("/api/llm/models").json()

    specs = [m["spec"] for m in body["models"]]
    assert len(specs) == len(set(specs))
    for spec in ("openrouter:other/model:free", "openrouter:x-1", "groq:llama", "opencode:github-copilot/auto",
                 "opencode:9router/kc/minimax/minimax-m3:free"):
        assert spec in specs
    by_spec = {m["spec"]: m for m in body["models"]}
    assert by_spec["opencode:9router/kc/minimax/minimax-m3:free"]["model"] == "9router/kc/minimax/minimax-m3:free"
    assert by_spec["openrouter:x-1"]["available"] is True
    assert by_spec["groq:llama"]["available"] is False  # provider has a problem
    assert specs.count("openrouter:fake-model") == 1  # default + suggested + (backtest) deduplicated


def test_models_lists_claude_code_full_lineup():
    set_client(LLMClient(providers={"claude_code": ClaudeCodeProvider(Settings())}))
    try:
        specs = {m["spec"] for m in TestClient(app).get("/api/llm/models").json()["models"]}
    finally:
        set_client(None)
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2", "opus", "sonnet", "haiku"):
        assert f"claude_code:{model}" in specs


# ---- thinking, images, presets, provider defaults -------------------------------------------------------------

PNG = "data:image/png;base64,iVBORw0KGgo="


def test_models_exposes_each_providers_default_model_and_thinking(fake_openrouter):
    fake_openrouter.thinking = "medium"
    fake_openrouter.thinking_levels = ("low", "medium")
    body = TestClient(app).get("/api/llm/models").json()

    providers = {p["name"]: p for p in body["providers"]}
    assert providers["openrouter"]["model"] == "fake-model"
    assert providers["openrouter"]["thinking"] == "medium"
    assert providers["openrouter"]["thinkingChoices"] == ["low", "medium"]
    assert providers["groq"]["model"] == "fake-model" and providers["groq"]["thinking"] == ""


def test_models_of_real_providers_report_defaults_and_choices():
    set_client(LLMClient(providers={"claude_code": ClaudeCodeProvider(Settings())}))
    try:
        providers = {p["name"]: p for p in TestClient(app).get("/api/llm/models").json()["providers"]}
    finally:
        set_client(None)
    assert providers["claude_code"]["model"]
    assert providers["claude_code"]["thinkingChoices"] == ["off", "low", "medium", "high", "xhigh", "max"]


def test_chat_thinking_is_passed_to_the_provider_as_extra(client, fake_openrouter):
    fake_openrouter.thinking_levels = ("off", "low", "high")
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model", "thinking": "High",
                                              "messages": [{"role": "user", "text": "hi"}]})

    assert resp.status_code == 200
    assert fake_openrouter.requests[-1].extra == {"effort": "high"}


def test_chat_without_thinking_sends_no_extra(client, fake_openrouter):
    for value in (None, "", "  "):
        resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model", "thinking": value,
                                                  "messages": [{"role": "user", "text": "hi"}]})
        assert resp.status_code == 200
        assert fake_openrouter.requests[-1].extra == {}


def test_chat_unknown_thinking_level_is_bad_input(client, fake_openrouter):
    fake_openrouter.thinking_levels = ("low", "high")
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model", "thinking": "max",
                                              "messages": [{"role": "user", "text": "hi"}]})

    assert resp.status_code == 400
    assert "max" in resp.json()["detail"]


def test_chat_thinking_on_provider_without_thinking_is_bad_input(client):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model", "thinking": "low",
                                              "messages": [{"role": "user", "text": "hi"}]})

    assert resp.status_code == 400  # FakeProvider has no thinking levels


def test_thinking_extra_per_provider():
    from app.llm.providers.groq import GroqProvider
    from app.llm.providers.openrouter import OpenRouterProvider
    from app.llm.providers.opencode import OpenCodeProvider
    from app.llm.schema import BadInput

    cc = ClaudeCodeProvider(Settings())
    assert cc.thinking_extra("max") == {"effort": "max"}
    assert cc.effort(cc.thinking_extra("off")) == "low"  # "off" cannot be turned off through the CLI
    assert cc.thinking_extra("") == {}
    with pytest.raises(BadInput):
        cc.thinking_extra("ultra")

    assert OpenRouterProvider(Settings()).thinking_extra("high") == {"reasoning": {"effort": "high"}}
    assert OpenRouterProvider(Settings()).thinking_extra("off") == {"reasoning": {"enabled": False}}
    assert OpenRouterProvider(Settings()).thinking_extra(None) == {}
    with pytest.raises(BadInput):
        OpenRouterProvider(Settings()).thinking_extra("xhigh")

    assert GroqProvider(Settings()).thinking_extra("low") == {"reasoning_effort": "low"}
    with pytest.raises(BadInput):
        GroqProvider(Settings()).thinking_extra("off")

    oc = OpenCodeProvider(Settings())
    assert oc.thinking_extra("high") == {"effort": "high"}
    with pytest.raises(BadInput):
        oc.thinking_extra("hi gh; rm")


def test_thinking_reaches_each_real_providers_request():
    from app.llm.providers.groq import GroqProvider
    from app.llm.providers.openrouter import OpenRouterProvider
    from app.llm.providers.opencode import OpenCodeProvider

    def req(provider, level):
        return ChatRequest([Message("user", "hi")], model="m", extra=provider.thinking_extra(level))

    cc = ClaudeCodeProvider(Settings())
    args, _, _ = cc.build_command(req(cc, "xhigh"))
    assert args[args.index("--effort") + 1] == "xhigh"

    orp = OpenRouterProvider(Settings())
    assert orp.build_payload(req(orp, "low"))["reasoning"] == {"effort": "low"}

    groq = GroqProvider(Settings())
    assert groq.build_payload(req(groq, "high"))["reasoning_effort"] == "high"

    oc = OpenCodeProvider(Settings())
    assert oc.model_arg(req(oc, "high")) == "m#high"


def test_chat_passes_images_and_allows_image_only_message(client, fake_openrouter):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model", "messages": [
        {"role": "user", "text": "first", "images": [PNG]},
        {"role": "assistant", "text": "ok"},
        {"role": "user", "text": "", "images": [PNG, PNG]}]})

    assert resp.status_code == 200
    sent = fake_openrouter.requests[-1].messages
    assert sent[0].images == [PNG] and sent[1].images == [] and sent[2].images == [PNG, PNG]


@pytest.mark.parametrize("images", [["http://example.com/a.png"], ["data:text/html;base64,AAAA"],
                                    ["data:image/png;base64,not base64!"], [PNG] * 7])
def test_chat_rejects_bad_images(client, images):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model",
                                              "messages": [{"role": "user", "text": "hi", "images": images}]})

    assert resp.status_code == 422


def test_chat_rejects_too_large_image(client):
    huge = "data:image/png;base64," + "A" * 7_000_000
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model",
                                              "messages": [{"role": "user", "text": "hi", "images": [huge]}]})

    assert resp.status_code == 422


def test_chat_empty_text_without_images_is_bad_input(client):
    resp = client.post("/api/llm/chat", json={"model": "openrouter:fake-model",
                                              "messages": [{"role": "user", "text": ""}]})

    assert resp.status_code == 400


def test_presets_has_the_backtest_system_prompt(client):
    from app.backtest.predict import SYSTEM_PROMPT

    assert client.get("/api/llm/presets").json() == {"backtest_system_prompt": SYSTEM_PROMPT}
