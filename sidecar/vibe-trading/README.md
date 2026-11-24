# Vibe-Trading sidecar

[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) (MIT, `vibe-trading-ai==0.1.19`) runs as a separate
process with its own virtualenv. The NewsTrade backend talks to it through `backend/app/vibe/client.py`
and exposes it under `/api/vibe/*`. Off by default (`VIBE_ENABLED=false`).

## Run

```bash
cp sidecar/vibe-trading/.env.example sidecar/vibe-trading/.env   # add OPENROUTER_API_KEY (swarm) and FRED_API_KEY (macro)
sidecar/vibe-trading/run.sh                                       # creates .venv, installs the pinned version, serves :8899
# or together with the app:
VIBE_SIDECAR=1 ./dev.sh
```

Then set in `backend/.env`: `VIBE_ENABLED=true` (and `VIBE_BASE_URL` / `VIBE_RUNS_DIR` if you changed port or folder).
Python >= 3.11 is required. Docker alternative: see `docker-compose.vibe.yml` at the repo root.

## Transport: MCP over Streamable HTTP (`vibe-trading-mcp --transport http`, endpoint `/mcp`)

Vibe-Trading also ships a REST server (`api_server.py`), but it is built around the agent chat UI. Its REST surface has
swarm routes, yet no direct backtest endpoint (a backtest there is something the LLM agent does) and no macro/market-data
endpoint. The MCP server exposes exactly the tools we need as plain, schema'd calls:

| Need | MCP tool |
| --- | --- |
| backtest | `backtest(run_dir)` (reads `config.json` + `code/signal_engine.py`) |
| OHLCV | `get_market_data(codes, start_date, end_date, source, interval, ...)` |
| FRED macro | `get_macro_series(series_id, start_date, end_date, limit)` (needs `FRED_API_KEY`) |
| swarm | `list_swarm_presets`, `run_swarm(preset_name, variables, start_only=true)`, `get_swarm_status`, `get_run_result` |

The backend uses a tiny JSON-RPC client over `httpx` (initialize, `tools/call`), so no `mcp` SDK dependency is added.
Backtests need a shared filesystem: the backend writes `config.json` and `code/signal_engine.py` into
`sidecar/vibe-trading/runs/<id>/` (`VIBE_RUNS_DIR`), which `run.sh` registers as an allowed run root.

## Security posture

- Binds to `127.0.0.1` only. The MCP endpoint has no authentication, so never publish the port. The server adds a
  Host/Origin allow-list (loopback by default) against DNS rebinding.
- The MCP surface never exposes order-placing or order-cancelling tools. Live trading routes exist only in the REST
  `api_server`, which we do not start.
- Shell tools (`bash`, `background_run`, `cancel_background`) are OFF (`VIBE_TRADING_ENABLE_SHELL_TOOLS=0`; `run.sh`
  forces the default to 0). Do not pass `--enable-shell-tools`.
- `write_file`/`read_file` MCP tools stay sandboxed by Vibe-Trading's allowed-roots policy; the backend never calls them.
  Generated strategy code is AST-checked by Vibe-Trading before execution and run in a subprocess.
- Runtime state lives in `sidecar/vibe-trading/data` and `runs` (git-ignored), not in `~/.vibe-trading`.
- Output is research, not financial advice; the backend never forwards it to order execution.
