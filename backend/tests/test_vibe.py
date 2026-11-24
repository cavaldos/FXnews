"""Vibe-Trading sidecar integration: the MCP Streamable HTTP sidecar is mocked with respx."""

import asyncio
import json

import httpx
import pytest
import respx

from app.config import get_settings
from app.main import app
from app.vibe import VibeUnavailable, get_client, jobs, reset_client

BASE = "http://vibe.test:8899"
MCP = f"{BASE}/mcp"


@pytest.fixture(autouse=True)
def _vibe(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "vibe_enabled", True)
    monkeypatch.setattr(s, "vibe_base_url", BASE)
    monkeypatch.setattr(s, "vibe_runs_dir", str(tmp_path / "runs"))
    reset_client()
    jobs.clear()
    yield
    reset_client()


@pytest.fixture
async def api():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
        yield c


def mcp_mock(tools: dict, *, sse: bool = False):
    """respx side_effect emulating initialize / notifications / tools/list / tools/call."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "Vibe-Trading", "version": "0.1.19"}, "capabilities": {}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}, headers={"mcp-session-id": "sess-1"})
        if method == "tools/list":
            result = {"tools": [{"name": n} for n in tools]}
        elif method == "tools/call":
            name, args = body["params"]["name"], body["params"]["arguments"]
            calls.append((name, args))
            out = tools[name](args) if callable(tools[name]) else tools[name]
            result = {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "unknown"}})
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(msg)}\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=msg)

    return handler, calls


RUN = {"run_id": "run-1", "status": "running", "preset": "macro_rates_fx_desk", "tasks": [{"id": "t1", "status": "running"}], "final_report": None}


async def test_disabled_returns_503(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "vibe_enabled", False)
    assert (await api.get("/api/vibe/presets")).status_code == 503
    assert (await api.post("/api/vibe/swarm", json={"preset_name": "x"})).status_code == 503
    assert (await api.get("/api/vibe/macro/DGS10")).status_code == 503
    r = await api.post("/api/vibe/backtest", json={"codes": ["EUR/USD"], "start_date": "2024-01-01", "end_date": "2024-06-01"})
    assert r.status_code == 503 and "disabled" in r.json()["detail"]
    st = (await api.get("/api/vibe/status")).json()
    assert st["enabled"] is False and st["reachable"] is False


@respx.mock
async def test_status_ok(api):
    handler, _ = mcp_mock({})
    respx.post(MCP).mock(side_effect=handler)
    st = (await api.get("/api/vibe/status")).json()
    assert st["enabled"] and st["reachable"] and st["server_version"] == "0.1.19" and st["tool_count"] == 0


@respx.mock
async def test_status_unreachable(api):
    respx.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    r = await api.get("/api/vibe/status")
    assert r.status_code == 200 and r.json()["reachable"] is False and "unreachable" in r.json()["error"]


@respx.mock
async def test_unreachable_is_503(api):
    respx.post(MCP).mock(side_effect=httpx.ConnectError("refused"))
    r = await api.get("/api/vibe/presets")
    assert r.status_code == 503 and "unreachable" in r.json()["detail"]


@respx.mock
async def test_presets_and_swarm_flow(api):
    presets = [{"name": "macro_rates_fx_desk", "title": "Macro", "description": "d", "agent_count": 4,
                "variables": [{"name": "goal", "description": "g", "required": True}], "source": "bundled"}]
    done = {**RUN, "status": "completed", "final_report": "EUR/USD: neutral"}
    handler, calls = mcp_mock({"list_swarm_presets": presets, "run_swarm": RUN, "get_run_result": done}, sse=True)
    respx.post(MCP).mock(side_effect=handler)
    assert (await api.get("/api/vibe/presets")).json()[0]["variables"][0]["required"] is True
    started = await api.post("/api/vibe/swarm", json={"preset_name": "macro_rates_fx_desk", "variables": {"goal": "EUR/USD", "timeframe": "1m"}})
    assert started.status_code == 200 and started.json()["run_id"] == "run-1" and started.json()["ready"] is False
    assert ("run_swarm", {"preset_name": "macro_rates_fx_desk", "variables": {"goal": "EUR/USD", "timeframe": "1m"}, "start_only": True}) in calls
    final = (await api.get("/api/vibe/swarm/run-1")).json()
    assert final["ready"] and final["final_report"] == "EUR/USD: neutral"


@respx.mock
async def test_swarm_unknown_run_is_404(api):
    handler, _ = mcp_mock({"get_run_result": {"status": "error", "error": "Run nope not found"}})
    respx.post(MCP).mock(side_effect=handler)
    assert (await api.get("/api/vibe/swarm/nope")).status_code == 404


@respx.mock
async def test_tool_error_is_502(api):
    handler, _ = mcp_mock({"get_macro_series": {"ok": False, "error": "FRED_API_KEY is not set"}})
    respx.post(MCP).mock(side_effect=handler)
    r = await api.get("/api/vibe/macro/DGS10")
    assert r.status_code == 502 and "FRED_API_KEY" in r.json()["detail"]


@respx.mock
async def test_macro_series(api):
    payload = {"ok": True, "source": "fred", "data": {"series_id": "DGS10", "count": 2, "observations_available": 2, "truncated": False,
                                                       "observations": [{"date": "2024-01-02", "value": 3.95}, {"date": "2024-01-03", "value": 4.0}]}}
    handler, calls = mcp_mock({"get_macro_series": payload})
    respx.post(MCP).mock(side_effect=handler)
    r = (await api.get("/api/vibe/macro/DGS10?start=2024-01-01&end=2024-01-31&limit=10")).json()
    assert r["count"] == 2 and r["observations"][1]["value"] == 4.0
    assert calls[0] == ("get_macro_series", {"series_id": "DGS10", "limit": 10, "start_date": "2024-01-01", "end_date": "2024-01-31"})


async def _wait_job(api, jid):
    for _ in range(100):
        got = (await api.get(f"/api/vibe/backtest/{jid}")).json()
        if got["status"] != "running":
            return got
        await asyncio.sleep(0.02)
    raise AssertionError("job never finished")


@respx.mock
async def test_backtest_job(api):
    envelope = {"status": "ok", "summary": {"metrics": {"sharpe": 1.2, "max_drawdown": -0.05}, "equity_preview": [100000, 101000], "artifact_paths": {"metrics": "artifacts/metrics.csv"}}}
    handler, calls = mcp_mock({"backtest": envelope})
    respx.post(MCP).mock(side_effect=handler)
    body = {"codes": ["eur/usd"], "start_date": "2024-01-01", "end_date": "2024-06-01", "leverage": 50, "spread_pips": 1.5,
            "strategy": {"kind": "sma_cross", "params": {"fast": 10, "slow": 30}}}
    job = await api.post("/api/vibe/backtest", json=body)
    assert job.status_code == 202
    got = await _wait_job(api, job.json()["id"])
    assert got["status"] == "completed" and got["result"]["metrics"]["sharpe"] == 1.2
    run_dir = calls[0][1]["run_dir"]
    cfg = json.loads(open(f"{run_dir}/config.json").read())
    assert cfg["codes"] == ["EURUSD=X"] and cfg["leverage"] == 50 and cfg["spread_pips_override"] == 1.5 and cfg["source"] == "auto"
    code = open(f"{run_dir}/code/signal_engine.py").read()
    assert "class SignalEngine" in code and "rolling(10)" in code and "rolling(30)" in code
    compile(code, "signal_engine.py", "exec")


@respx.mock
async def test_backtest_job_failure_recorded(api):
    handler, _ = mcp_mock({"backtest": {"status": "error", "error": "no data for EUR/USD"}})
    respx.post(MCP).mock(side_effect=handler)
    jid = (await api.post("/api/vibe/backtest", json={"codes": ["EUR/USD"], "start_date": "2024-01-01", "end_date": "2024-02-01"})).json()["id"]
    got = await _wait_job(api, jid)
    assert got["status"] == "failed" and "no data" in got["error"]


async def test_backtest_custom_code_gated(api, monkeypatch):
    body = {"codes": ["EUR/USD"], "start_date": "2024-01-01", "end_date": "2024-02-01",
            "strategy": {"kind": "custom", "code": "class SignalEngine:\n    def generate(self, d):\n        return {}\n"}}
    r = await api.post("/api/vibe/backtest", json=body)
    assert r.status_code == 400 and "VIBE_ALLOW_CUSTOM_CODE" in r.json()["detail"]
    monkeypatch.setattr(get_settings(), "vibe_allow_custom_code", True)
    assert (await api.post("/api/vibe/backtest", json=body)).status_code == 202


def test_prune_runs(tmp_path):
    from app.vibe.client import prune_runs
    import os
    for i in range(5):
        d = tmp_path / f"nt-{i}"
        d.mkdir()
        os.utime(d, (1000 + i, 1000 + i))
    (tmp_path / "other").mkdir()
    prune_runs(tmp_path, 2)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nt-3", "nt-4", "other"]


async def test_backtest_validation(api):
    bad = {"codes": ["EUR/USD"], "start_date": "2024-06-01", "end_date": "2024-01-01"}
    assert (await api.post("/api/vibe/backtest", json=bad)).status_code == 422
    custom = {"codes": ["EUR/USD"], "start_date": "2024-01-01", "end_date": "2024-02-01", "strategy": {"kind": "custom"}}
    assert (await api.post("/api/vibe/backtest", json=custom)).status_code == 422
    assert (await api.get("/api/vibe/backtest/unknown")).status_code == 404


@respx.mock
async def test_client_retries_on_stale_session():
    calls = {"n": 0}
    handler, _ = mcp_mock({"list_swarm_presets": []})

    def flaky(request):
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(404)
        return handler(request)

    respx.post(MCP).mock(side_effect=flaky)
    assert await get_client().list_swarm_presets() == []
    assert calls["n"] == 2


async def test_client_disabled_raises(monkeypatch):
    monkeypatch.setattr(get_settings(), "vibe_enabled", False)
    with pytest.raises(VibeUnavailable):
        await get_client().list_swarm_presets()
