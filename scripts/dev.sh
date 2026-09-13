#!/usr/bin/env bash
# 一条命令把前后端都拉起来。Ctrl-C 一起退出。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${AGENTLAB_PORT:-8000}"
WEB_PORT="${AGENTLAB_WEB_PORT:-5273}"

command -v uv   >/dev/null || { echo "缺少 uv：brew install uv"; exit 1; }
command -v pnpm >/dev/null || { echo "缺少 pnpm：npm i -g pnpm"; exit 1; }

echo "==> 同步依赖"
(cd "$ROOT/backend" && uv sync --quiet)
[ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && pnpm install)

cleanup() {
  echo
  echo "==> 停止"
  # 杀掉整个进程组，避免 uvicorn / vite 变成孤儿进程
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null || true
  [ -n "${WEB_PID:-}" ] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> 后端 http://127.0.0.1:$API_PORT"
(cd "$ROOT/backend" && uv run uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --reload) &
API_PID=$!

# 等后端起来再起前端，省得前端第一次请求就打空
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done

echo "==> 前端 http://localhost:$WEB_PORT"
(cd "$ROOT/frontend" && pnpm dev --port "$WEB_PORT") &
WEB_PID=$!

echo
echo "  界面   http://localhost:$WEB_PORT"
echo "  接口   http://127.0.0.1:$API_PORT/docs"
echo
wait
