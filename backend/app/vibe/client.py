"""Async client for the Vibe-Trading sidecar.

Transport: MCP over Streamable HTTP (`vibe-trading-mcp --transport http`, endpoint `<base>/mcp`), spoken with a minimal
JSON-RPC layer on httpx (initialize -> notifications/initialized -> tools/call) so the backend needs no `mcp` SDK.
Every Vibe-Trading tool returns a JSON string in the first text content block; `_call` parses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.vibe import strategies
from app.vibe.schemas import (
    BacktestIn,
    BacktestResult,
    MacroObservation,
    MacroSeries,
    MarketData,
    SwarmPreset,
    SwarmRun,
    VibeStatus,
)

log = logging.getLogger("fxnews.vibe")

PROTOCOL_VERSION = "2025-03-26"
TERMINAL = {"completed", "failed", "cancelled"}


class VibeError(Exception):
    """The sidecar answered, but the tool call failed (bad input, missing key, engine error)."""


class VibeNotFound(VibeError):
    """The requested run / series does not exist on the sidecar."""


class VibeUnavailable(Exception):
    """Integration disabled (VIBE_ENABLED=false) or the sidecar cannot be reached."""


def to_sidecar_code(code: str) -> str:
    """'EUR/USD' / 'EURUSD' / 'EURUSD.FX' -> Yahoo form 'EURUSD=X' (the only FX form the free loaders resolve)."""
    c = code.strip().upper()
    if c.endswith("=X"):
        return c
    base = c.removesuffix(".FX").replace("/", "")
    if len(base) == 6 and base.isalpha():
        return f"{base}=X"
    return c


def prune_runs(root: Path, keep: int) -> None:
    """Delete all but the newest `keep` nt-* run dirs under `root`."""
    try:
        dirs = sorted((d for d in root.glob("nt-*") if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        return
    for d in dirs[max(keep, 1):]:
        shutil.rmtree(d, ignore_errors=True)


def _first_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


class VibeClient:
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._server_info: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._next_id = 0

    # ── plumbing ──

    @property
    def enabled(self) -> bool:
        return get_settings().vibe_enabled

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise VibeUnavailable("Vibe-Trading integration is disabled (set VIBE_ENABLED=true and start sidecar/vibe-trading/run.sh)")

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            s = get_settings()
            self._http = httpx.AsyncClient(base_url=s.vibe_base_url.rstrip("/"), timeout=httpx.Timeout(s.vibe_timeout_seconds, connect=5.0))
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._session_id = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        token = get_settings().vibe_token
        if token:
            h["Authorization"] = f"Bearer {token}"
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    @staticmethod
    def _decode(resp: httpx.Response, rpc_id: int | None) -> dict[str, Any]:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            found: dict[str, Any] | None = None
            for line in resp.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and (rpc_id is None or msg.get("id") == rpc_id) and ("result" in msg or "error" in msg):
                    found = msg
            if found is None:
                raise VibeError("empty MCP event stream")
            return found
        try:
            msg = resp.json()
        except ValueError as exc:
            raise VibeError(f"non-JSON answer from the sidecar (HTTP {resp.status_code})") from exc
        if not isinstance(msg, dict):
            raise VibeError("unexpected MCP answer")
        return msg

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return await self._client().post("/mcp", json=body, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise VibeUnavailable(f"Vibe-Trading sidecar timed out ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise VibeUnavailable(f"Vibe-Trading sidecar unreachable at {get_settings().vibe_base_url}: {type(exc).__name__}") from exc

    async def _initialize(self) -> None:
        self._session_id = None
        self._next_id += 1
        rid = self._next_id
        resp = await self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "newstrade", "version": get_settings().app_version}}})
        if resp.status_code >= 400:
            raise VibeUnavailable(f"Vibe-Trading sidecar refused the MCP handshake (HTTP {resp.status_code})")
        msg = self._decode(resp, rid)
        if "error" in msg:
            raise VibeUnavailable(f"MCP initialize failed: {msg['error'].get('message', msg['error'])}")
        self._session_id = resp.headers.get("mcp-session-id")
        self._server_info = (msg.get("result") or {}).get("serverInfo") or {}
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_enabled()
        async with self._lock:
            if self._session_id is None and not self._server_info:
                await self._initialize()
        for attempt in (0, 1):
            self._next_id += 1
            rid = self._next_id
            resp = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            if resp.status_code in (400, 404) and attempt == 0:  # stale / unknown session: handshake again once
                async with self._lock:
                    await self._initialize()
                continue
            if resp.status_code >= 500:
                raise VibeError(f"Vibe-Trading sidecar error (HTTP {resp.status_code})")
            if resp.status_code >= 400:
                raise VibeUnavailable(f"Vibe-Trading sidecar rejected the request (HTTP {resp.status_code})")
            msg = self._decode(resp, rid)
            if "error" in msg:
                raise VibeError(str(msg["error"].get("message", msg["error"])))
            return msg.get("result") or {}
        raise VibeError("MCP session could not be established")

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        result = await self._rpc("tools/call", {"name": tool, "arguments": arguments})
        blocks = result.get("content") or []
        text = next((b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"), "")
        payload = _first_json(text) if text else result.get("structuredContent", {})
        if result.get("isError"):
            raise VibeError(text or f"tool {tool} failed")
        if isinstance(payload, dict):
            err = payload.get("error") if payload.get("status") == "error" or payload.get("ok") is False else None
            if payload.get("status") == "error" and not err:  # backtest: engine failure carries only exit_code/stderr
                tail = str(payload.get("stderr") or payload.get("stdout") or "").strip().splitlines()
                err = f"backtest failed (exit {payload.get('exit_code')}): " + " | ".join(tail[-3:])[:600]
            if err:
                cls = VibeNotFound if "not found" in str(err).lower() else VibeError
                raise cls(str(err))
        return payload

    # ── public API ──

    async def health(self) -> VibeStatus:
        s = get_settings()
        if not s.vibe_enabled:
            return VibeStatus(enabled=False, base_url=s.vibe_base_url, error="disabled (VIBE_ENABLED=false)")
        try:
            async with self._lock:
                await self._initialize()
            tools = await self._rpc("tools/list")
            return VibeStatus(enabled=True, reachable=True, base_url=s.vibe_base_url, server_name=self._server_info.get("name"),
                              server_version=self._server_info.get("version"), tool_count=len(tools.get("tools") or []))
        except (VibeUnavailable, VibeError) as exc:
            return VibeStatus(enabled=True, reachable=False, base_url=s.vibe_base_url, error=str(exc))

    async def list_swarm_presets(self) -> list[SwarmPreset]:
        data = await self._call("list_swarm_presets", {})
        rows = data if isinstance(data, list) else []
        return [SwarmPreset.model_validate(r) for r in rows if isinstance(r, dict)]

    async def run_swarm(self, preset: str, variables: dict[str, str] | None = None) -> SwarmRun:
        """Start a swarm run and return immediately (poll with `get_swarm_run` / `watch_swarm`)."""
        data = await self._call("run_swarm", {"preset_name": preset, "variables": variables or {}, "start_only": True})
        return self._run(data)

    async def get_swarm_run(self, run_id: str) -> SwarmRun:
        return self._run(await self._call("get_run_result", {"run_id": run_id}))

    async def watch_swarm(self, run_id: str, *, interval: float = 5.0, max_wait: float = 3600.0) -> AsyncIterator[SwarmRun]:
        """Yield the run each poll until it is terminal (or `max_wait` elapses)."""
        waited = 0.0
        while True:
            run = await self.get_swarm_run(run_id)
            yield run
            if run.ready or waited >= max_wait:
                return
            await asyncio.sleep(interval)
            waited += interval

    @staticmethod
    def _run(data: Any) -> SwarmRun:
        if not isinstance(data, dict) or "run_id" not in data:
            raise VibeError("unexpected swarm payload from the sidecar")
        data = dict(data)
        data.setdefault("ready", data.get("status") in TERMINAL)
        return SwarmRun.model_validate(data)

    async def get_macro_series(self, series_id: str, start: date | str | None = None, end: date | str | None = None, limit: int = 2000) -> MacroSeries:
        args: dict[str, Any] = {"series_id": series_id, "limit": limit}
        if start:
            args["start_date"] = str(start)
        if end:
            args["end_date"] = str(end)
        data = await self._call("get_macro_series", args)
        body = data.get("data", data) if isinstance(data, dict) else {}
        obs = [MacroObservation(date=str(o.get("date")), value=o.get("value")) for o in body.get("observations", []) if isinstance(o, dict)]
        return MacroSeries(series_id=str(body.get("series_id", series_id)), source=str(data.get("source", "fred")) if isinstance(data, dict) else "fred",
                           count=int(body.get("count", len(obs))), observations_available=body.get("observations_available"),
                           truncated=bool(body.get("truncated", False)), observations=obs)

    async def get_market_data(self, symbol: str, start: date | str, end: date | str, interval: str = "1D", source: str = "auto", max_rows: int = 250) -> MarketData:
        data = await self._call("get_market_data", {"codes": [to_sidecar_code(symbol)], "start_date": str(start), "end_date": str(end), "source": source,
                                                    "interval": interval, "max_rows": max_rows})
        return MarketData(symbol=symbol, interval=interval, data=data)

    async def run_backtest(self, config: BacktestIn) -> BacktestResult:
        """Write config.json + code/signal_engine.py into a fresh run dir under VIBE_RUNS_DIR and run the sidecar's backtest.

        Blocks until the sidecar finishes (use `app.vibe.jobs` for the async job API).
        """
        self._require_enabled()
        try:
            code = strategies.render(config.strategy)
        except ValueError as exc:
            raise VibeError(str(exc)) from exc
        cfg: dict[str, Any] = {
            "source": config.source, "codes": [to_sidecar_code(c) for c in config.codes], "start_date": str(config.start_date), "end_date": str(config.end_date),
            "interval": config.interval, "engine": "daily", "initial_cash": config.initial_cash, "leverage": config.leverage,
            "slippage_pips": config.slippage_pips, "swap_enabled": config.swap_enabled,
        }
        if config.spread_pips is not None:
            cfg["spread_pips_override"] = config.spread_pips
        run_dir = Path(get_settings().vibe_runs_dir).expanduser().resolve() / f"nt-{uuid.uuid4().hex[:12]}"
        try:
            (run_dir / "code").mkdir(parents=True)
            (run_dir / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            (run_dir / "code" / "signal_engine.py").write_text(code, encoding="utf-8")
        except OSError as exc:
            raise VibeError(f"cannot write the backtest run dir {run_dir}: {exc}") from exc
        try:
            data = await self._call("backtest", {"run_dir": str(run_dir)})
        except VibeUnavailable:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        finally:
            prune_runs(run_dir.parent, get_settings().vibe_runs_keep)
        data = data if isinstance(data, dict) else {}
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        metrics = summary.get("metrics") or data.get("metrics") or {k: v for k, v in summary.items() if isinstance(v, (int, float))}
        return BacktestResult(metrics=metrics if isinstance(metrics, dict) else {}, equity_preview=list(summary.get("equity_preview") or []),
                              artifact_paths=summary.get("artifact_paths") or {}, run_dir=str(run_dir), raw=data)


_client: VibeClient | None = None


def get_client() -> VibeClient:
    global _client
    if _client is None:
        _client = VibeClient()
    return _client


def reset_client() -> None:
    """Drop the cached client (tests / settings changes). The old httpx client is garbage-collected."""
    global _client
    _client = None
