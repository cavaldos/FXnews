#!/usr/bin/env bash
# Start the Vibe-Trading MCP server (Streamable HTTP) as a NewsTrade sidecar.
# Own venv: sidecar/vibe-trading/.venv (Python >= 3.11). Endpoint: http://$VIBE_HOST:$VIBE_PORT/mcp
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VERSION="0.1.19"

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi
VIBE_HOST="${VIBE_HOST:-127.0.0.1}"
VIBE_PORT="${VIBE_PORT:-8899}"

if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then PY="$c"; break; fi
  done
  [ -n "$PY" ] || { echo "Python >= 3.11 is required for the Vibe-Trading sidecar" >&2; exit 1; }
  "$PY" -m venv .venv
fi
if ! .venv/bin/python -c "import importlib.metadata as m,sys; sys.exit(0 if m.version('vibe-trading-ai')=='$VERSION' else 1)" 2>/dev/null; then
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install "vibe-trading-ai==$VERSION"
fi

# Keep all runtime state (runs, sessions, swarm runs, memory) inside the sidecar folder.
mkdir -p data runs
export VIBE_TRADING_HOME="${VIBE_TRADING_HOME:-$DIR/data}"
# Backtest run_dirs written by the NewsTrade backend must sit inside an allowed run root.
export VIBE_TRADING_ALLOWED_RUN_ROOTS="${VIBE_TRADING_ALLOWED_RUN_ROOTS:-$DIR/runs}"
# Never expose process control (bash). The MCP server also never surfaces order-placing tools.
export VIBE_TRADING_ENABLE_SHELL_TOOLS="${VIBE_TRADING_ENABLE_SHELL_TOOLS:-0}"

exec .venv/bin/vibe-trading-mcp --transport http --host "$VIBE_HOST" --port "$VIBE_PORT"
