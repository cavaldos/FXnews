#!/usr/bin/env bash
# Chạy đồng thời backend (FastAPI :8000) và frontend (Vite :5173). Ctrl+C để dừng cả hai.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

# Backend: tạo lại venv nếu interpreter cũ không còn tồn tại
cd "$ROOT/backend"
if [ ! -x .venv/bin/python ]; then
  rm -rf .venv
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && echo "Đã tạo backend/.env từ .env.example"; }

# Frontend: cài deps nếu chưa có
cd "$ROOT/frontend"
[ -d node_modules ] || npm install

pids=()
cleanup() { trap - INT TERM EXIT; kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null; }
trap cleanup INT TERM EXIT

(cd "$ROOT/backend" && exec .venv/bin/uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 3) &
pids+=($!)
(cd "$ROOT/frontend" && exec npm run dev) &
pids+=($!)

if [ "${VIBE_SIDECAR:-0}" = "1" ]; then
  (cd "$ROOT/sidecar/vibe-trading" && exec ./run.sh) &
  pids+=($!)
  echo "Vibe-Trading sidecar: http://127.0.0.1:${VIBE_PORT:-8899}/mcp (set VIBE_ENABLED=true in backend/.env)"
fi

echo "Backend:  http://localhost:8000"
echo "Frontend: http://localhost:5173"
wait
