#!/usr/bin/env bash
# 一条命令把前后端都拉起来。Ctrl-C 一起退出。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${AGENTLAB_PORT:-8000}"
WEB_PORT="${AGENTLAB_WEB_PORT:-5273}"

CONDA_ENV="${AGENTLAB_CONDA_ENV:-agentlab}"

command -v conda >/dev/null || { echo "缺少 conda：https://conda-forge.org/miniforge/"; exit 1; }
command -v pnpm  >/dev/null || { echo "缺少 pnpm：npm i -g pnpm"; exit 1; }

# 环境不存在就按 environment.yml 建一个
if ! conda env list | awk '{print $1}' | grep -qx "$CONDA_ENV"; then
  echo "==> 创建 conda 环境 $CONDA_ENV"
  conda env create -f "$ROOT/environment.yml"
fi
PY_BIN="$(conda run -n "$CONDA_ENV" python -c 'import sys; print(sys.executable)' 2>/dev/null)"
[ -x "$PY_BIN" ] || { echo "conda 环境 $CONDA_ENV 不可用，试试：conda env create -f environment.yml"; exit 1; }

echo "==> 后端解释器 $PY_BIN"
[ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && pnpm install)

# 端口被别人占着就往后挪。踩过一次坑：8000 被另一个项目占住时，uvicorn
# 起不来直接退出，而下面的健康检查去探 8000 又被那个项目答应了，脚本以为
# 后端就绪、继续起前端——于是前端连着别人的服务，界面显示的全是陈年旧数据。
port_taken() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
if [ -z "${AGENTLAB_PORT:-}" ]; then
  while port_taken "$API_PORT"; do
    echo "==> 端口 $API_PORT 已被占用，换 $((API_PORT + 1))"
    API_PORT=$((API_PORT + 1))
  done
elif port_taken "$API_PORT"; then
  echo "端口 $API_PORT 已被占用（AGENTLAB_PORT 指定的），换一个或停掉占用方"
  lsof -nP -iTCP:"$API_PORT" -sTCP:LISTEN | tail -n +2 | head -3
  exit 1
fi
# 前端代理要跟着后端端口走，否则请求会打到占端口的那个服务上
export AGENTLAB_PORT="$API_PORT"

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
(cd "$ROOT/backend" && "$PY_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" --reload) &
API_PID=$!

# 等后端起来再起前端，省得前端第一次请求就打空。
# 光看"有没有响应"不够——得确认答应的是 agentlab 自己，不是碰巧占着
# 这个端口的别的服务；顺便看住后端进程别悄悄死了。
ready=""
for _ in $(seq 1 60); do
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "后端启动失败，上面应该有它的报错"
    exit 1
  fi
  if curl -sf "http://127.0.0.1:$API_PORT/api/health" 2>/dev/null | grep -q '"agentlab"'; then
    ready=1; break
  fi
  sleep 0.5
done
[ -n "$ready" ] || { echo "后端 30s 内没就绪（或 $API_PORT 上答话的不是 agentlab）"; exit 1; }

echo "==> 前端 http://localhost:$WEB_PORT"
(cd "$ROOT/frontend" && pnpm dev --port "$WEB_PORT") &
WEB_PID=$!

echo
echo "  界面   http://localhost:$WEB_PORT"
echo "  接口   http://127.0.0.1:$API_PORT/docs"
echo
wait
