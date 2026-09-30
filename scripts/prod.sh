#!/usr/bin/env bash
# 不用 Docker 的生产运行：构建前端，由后端一并托管页面，单进程、不热重载。Ctrl-C 退出。
set -euo pipefail

usage() {
  cat <<'USAGE'
用法：./scripts/prod.sh [--host] [--port <端口>] [--build | --no-build]

  --host         监听 0.0.0.0（同一网络里的设备都能打开）。默认只听 127.0.0.1，只有本机能打开。
                 注意：AgentLab 没有登录，同一网络里打得开页面的人都能用它——跑工作流、花你配的
                 模型额度、读数据源、执行沙箱代码。只在信得过的网络里开，并配好防火墙。
  --port <端口>  监听端口。也可以用环境变量 AGENTLAB_PORT，默认 8000。
  --build        不管前端产物新旧，强制重新构建。
  --no-build     跳过构建，直接用现有的 frontend/dist。
  -h, --help     显示这段说明。

默认只在 frontend/dist 不存在、或者比前端源码旧的时候才构建。
数据目录照后端配置：默认是仓库下的 data/，用环境变量 AGENTLAB_DATA_DIR 改。
只能单进程运行：运行管理器在进程内、数据库是 SQLite，多个 worker 会互相踩。
USAGE
}

# 参数先认完再干活，认不出的直接报错，免得写错的参数被悄悄忽略
HOST="127.0.0.1"
PORT="${AGENTLAB_PORT:-8000}"
BUILD="auto"
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="0.0.0.0" ;;
    --port)
      [ $# -ge 2 ] || { echo "--port 后面要跟端口号"; echo; usage; exit 2; }
      PORT="$2"; shift ;;
    --port=*) PORT="${1#--port=}" ;;
    --build) BUILD="always" ;;
    --no-build) BUILD="never" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "认不出的参数：$1"; echo; usage; exit 2 ;;
  esac
  shift
done
case "$PORT" in
  ''|*[!0-9]*) echo "端口必须是数字：$PORT"; exit 2 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "端口超出范围：$PORT"; exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/frontend/dist"
CONDA_ENV="${AGENTLAB_CONDA_ENV:-agentlab}"

# ---- 后端解释器：和 dev.sh 一样找 conda 环境，没有就按 environment.yml 建 ----
command -v conda >/dev/null || { echo "缺少 conda：https://conda-forge.org/miniforge/"; exit 1; }
if ! conda env list | awk '{print $1}' | grep -qx "$CONDA_ENV"; then
  echo "==> 创建 conda 环境 $CONDA_ENV"
  conda env create -f "$ROOT/environment.yml"
fi
PY_BIN="$(conda run -n "$CONDA_ENV" python -c 'import sys; print(sys.executable)' 2>/dev/null)"
[ -x "$PY_BIN" ] || { echo "conda 环境 $CONDA_ENV 不可用，试试：conda env create -f environment.yml"; exit 1; }
echo "==> 后端解释器 $PY_BIN"

# ---- 前端构建 ----
# dist 比源码旧：源码、入口页、依赖清单、构建配置里有任何一个比 dist/index.html 新
dist_stale() {
  [ -f "$DIST/index.html" ] || return 0
  local newer
  newer="$(find "$ROOT/frontend/src" "$ROOT/frontend/public" \
    "$ROOT/frontend/index.html" "$ROOT/frontend/package.json" "$ROOT/frontend/pnpm-lock.yaml" \
    "$ROOT/frontend/vite.config.ts" "$ROOT/frontend/tsconfig.json" \
    -newer "$DIST/index.html" -print 2>/dev/null | head -n 1)"
  [ -n "$newer" ]
}

need_build=""
case "$BUILD" in
  always) need_build=1 ;;
  never)
    [ -f "$DIST/index.html" ] || { echo "用了 --no-build，但 $DIST 下没有 index.html。先去掉 --no-build 构建一次"; exit 1; }
    ;;
  auto)
    if [ ! -f "$DIST/index.html" ]; then
      echo "==> 前端还没构建过"; need_build=1
    elif dist_stale; then
      echo "==> 前端源码比构建产物新"; need_build=1
    else
      echo "==> 前端构建产物是最新的，跳过构建（--build 可强制重建）"
    fi
    ;;
esac

if [ -n "$need_build" ]; then
  command -v pnpm >/dev/null || { echo "缺少 pnpm：npm i -g pnpm"; exit 1; }
  [ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && pnpm install --frozen-lockfile)
  echo "==> 构建前端（pnpm build）"
  (cd "$ROOT/frontend" && pnpm build)
  [ -f "$DIST/index.html" ] || { echo "构建完了却没有 $DIST/index.html，看上面的构建输出"; exit 1; }
fi

# ---- 端口 ----
# 生产服务要固定地址，端口被占就报错，不像 dev.sh 那样自动往后挪
port_taken() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    # 没有 lsof（精简的 Linux）就试着绑一下：绑得上说明没人占
    if "$PY_BIN" -c 'import socket,sys; s=socket.socket(); s.bind((sys.argv[1], int(sys.argv[2])))' "$HOST" "$1" 2>/dev/null; then
      return 1
    fi
    return 0
  fi
}
if port_taken "$PORT"; then
  echo "端口 $PORT 已被占用，换一个（--port 或 AGENTLAB_PORT）或停掉占用方"
  command -v lsof >/dev/null 2>&1 && { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -n +2 | head -3; }
  exit 1
fi

export AGENTLAB_WEB_DIST="$DIST"
DATA_SHOW="${AGENTLAB_DATA_DIR:-$ROOT/data}"

echo "==> 数据目录 $DATA_SHOW"
if [ "$HOST" = "0.0.0.0" ]; then
  # 本机在局域网里的地址：macOS 取 en0 / en1，Linux 取 hostname -I 的第一个
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
  URLS="http://127.0.0.1:$PORT  ·  http://${LAN_IP:-<本机 IP>}:$PORT"
  echo "==> 监听所有网卡。注意：AgentLab 没有登录，同一网络里打得开页面的人都能用它——"
  echo "    跑工作流、花你配的模型额度、读数据源、执行沙箱代码。只在信得过的网络里开。"
else
  URLS="http://127.0.0.1:$PORT"
fi

# 就绪后再报一次地址：启动日志一长，前面打的那行容易被冲掉。
# 确认答话的是 agentlab 自己（/api/health 里带 "agentlab"），不是碰巧占着端口的别的服务
if command -v curl >/dev/null 2>&1; then
  (
    # $$ 在子 shell 里仍是主脚本的进程号，exec 之后就是 uvicorn：它退了就别再等
    for _ in $(seq 1 120); do
      kill -0 $$ 2>/dev/null || exit 0
      if curl -sf "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q '"agentlab"'; then
        echo
        echo "  AgentLab 已就绪：$URLS"
        echo "  按 Ctrl-C 停止"
        echo
        exit 0
      fi
      sleep 0.5
    done
  ) &
fi

echo "==> 启动 $URLS"
cd "$ROOT/backend"
# exec：让 uvicorn 直接收 Ctrl-C / SIGTERM，一个信号就走完整的优雅退出（收尾运行、关沙箱）。
# 要是包一层 shell 再转发，终端的 Ctrl-C 会让 uvicorn 收到两次信号，第二次直接强退、跳过收尾
exec "$PY_BIN" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
