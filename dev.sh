#!/usr/bin/env bash
# Chạy đồng thời backend (FastAPI :8000) và frontend (Vite :5173). Ctrl+C để dừng cả hai.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

# Backend: tạo venv + cài deps nếu chưa có
cd "$ROOT/backend"
if [ ! -d .venv ]; then
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

echo "Backend:  http://localhost:8000"
echo "Frontend: http://localhost:5173"
wait
