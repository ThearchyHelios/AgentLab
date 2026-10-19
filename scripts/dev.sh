#!/usr/bin/env bash
# 一条命令把前后端都拉起来。Ctrl-C 一起退出。
set -euo pipefail

usage() {
  cat <<'USAGE'
用法：./scripts/dev.sh [--host] [--postgres]

  --host       前端也在局域网上监听（同一网络里的手机、别的电脑能打开）。后端仍只听 127.0.0.1：
               页面里的 /api 由前端开发服务器在本机转发，局域网里的设备不需要、也不该直连后端。
               注意：AgentLab 没有登录，同一网络里打得开页面的人都能用它——跑工作流、花你配的
               模型额度、读数据源、执行沙箱代码。只在信得过的网络里开。
  --postgres   应用库用 PostgreSQL：起（已经有就复用）一个本地开发用的 PostgreSQL 容器，端口只开在 127.0.0.1，
               后端照它设 AGENTLAB_DATABASE_URL。密码第一次自动生成，在 data/.postgres-dev/ 里；库的数据在 docker
               数据卷里，Ctrl-C 只停前后端，库的容器留着下次复用（不要了：docker rm -f 容器名 && docker volume rm 卷名）。
               不加就和原来一样：SQLite，不用 docker。下面几项可以改缺省（环境变量，或者写在 .env / backend/.env 里，
               环境变量优先）：
                 AGENTLAB_DEV_PG_CONTAINER  容器名，缺省 agentlab-dev-postgres
                 AGENTLAB_DEV_PG_PORT       本机端口，缺省 55432
                 AGENTLAB_DEV_PG_VOLUME     数据卷，缺省 agentlab-dev-pgdata
                 AGENTLAB_DEV_PG_IMAGE      镜像，缺省 postgres:16
USAGE
}

# 参数先认完再干活：以前不读参数，--host 这类写了也被悄悄忽略
WEB_ARGS=()
WITH_PG=""
for arg in "$@"; do
  case "$arg" in
    --host) WEB_ARGS+=(--host) ;;
    --postgres) WITH_PG=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "认不出的参数：$arg"; echo; usage; exit 2 ;;
  esac
done

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

# ---- --postgres：本地开发用的 PostgreSQL ----
# 这几项（AGENTLAB_DEV_PG_*）只有这个脚本用。和后端的配置一样认 .env：环境变量 > backend/.env > .env > 缺省（后端的 settings
# 读这两个文件，后一个优先）。不 source：.env 是给后端按 KEY=VALUE 读的，不是 shell（值里的 $、空格、反引号不该被执行）
dotenv_value() {
  local key="$1" file line val="" found=""
  for file in "$ROOT/.env" "$ROOT/backend/.env"; do
    [ -f "$file" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%$'\r'}"
      line="${line#"${line%%[![:space:]]*}"}"
      line="${line#export }"
      case "$line" in
        "$key="*) val="${line#*=}"; found=1 ;;
      esac
    done < "$file"
  done
  [ -n "$found" ] || return 1
  val="${val%"${val##*[![:space:]]}"}"
  case "$val" in
    \"*\"|\'*\') val="${val:1:${#val}-2}" ;;
    *) val="${val%%[[:space:]]#*}"; val="${val%"${val##*[![:space:]]}"}" ;;
  esac
  printf '%s' "$val"
}
dev_pg_setting() {
  local value
  value="$(printenv "$1" 2>/dev/null || true)"
  [ -n "$value" ] || value="$(dotenv_value "$1" || true)"
  printf '%s' "${value:-$2}"
}
if [ -n "$WITH_PG" ]; then
  PG_CONTAINER="$(dev_pg_setting AGENTLAB_DEV_PG_CONTAINER agentlab-dev-postgres)"
  PG_PORT="$(dev_pg_setting AGENTLAB_DEV_PG_PORT 55432)"
  PG_VOLUME="$(dev_pg_setting AGENTLAB_DEV_PG_VOLUME agentlab-dev-pgdata)"
  PG_IMAGE="$(dev_pg_setting AGENTLAB_DEV_PG_IMAGE postgres:16)"
  DEV_DATA="${AGENTLAB_DATA_DIR:-$ROOT/data}"
  case "$PG_PORT" in ''|*[!0-9]*) echo "AGENTLAB_DEV_PG_PORT 要是端口号：$PG_PORT"; exit 2 ;; esac
  command -v docker >/dev/null || { echo "--postgres 要用 docker，没找到它"; exit 1; }
  docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || { echo "连不上 docker 服务，先把 Docker 启动起来"; exit 1; }
  # 密码：第一次生成（0600），之后沿用；pgpass 给后端连库用（PGPASSFILE），密码不进命令行和环境变量
  (cd "$ROOT/backend" && "$PY_BIN" -m app.ops postgres-secrets --data "$DEV_DATA" --dir .postgres-dev \
    --host 127.0.0.1 --port "$PG_PORT") >/dev/null
  if docker container inspect "$PG_CONTAINER" >/dev/null 2>&1; then
    role="$(docker inspect -f '{{index .Config.Labels "agentlab.role"}}' "$PG_CONTAINER")"
    [ "$role" = dev-postgres ] || { echo "已经有一个叫 $PG_CONTAINER 的容器，不是这个脚本起的。换个名字：AGENTLAB_DEV_PG_CONTAINER=…"; exit 1; }
    published="$(docker inspect -f '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostPort}}{{end}}{{end}}' "$PG_CONTAINER")"
    [ "$published" = "$PG_PORT" ] || { echo "容器 $PG_CONTAINER 开的是本机端口 ${published}，不是 ${PG_PORT}：AGENTLAB_DEV_PG_PORT=$published 再来，或者删掉容器重建"; exit 1; }
    if [ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER")" != true ]; then
      echo "==> 启动开发用的 PostgreSQL（${PG_CONTAINER}）"
      docker start "$PG_CONTAINER" >/dev/null
    else
      echo "==> 复用开发用的 PostgreSQL（${PG_CONTAINER}）"
    fi
  else
    port_taken "$PG_PORT" && { echo "端口 $PG_PORT 已被占用：换一个 AGENTLAB_DEV_PG_PORT=…"; exit 1; }
    echo "==> 起开发用的 PostgreSQL（${PG_CONTAINER}，${PG_IMAGE}，127.0.0.1:${PG_PORT}，数据卷 ${PG_VOLUME}）"
    docker run -d --name "$PG_CONTAINER" --label agentlab.role=dev-postgres \
      -e POSTGRES_USER=agentlab -e POSTGRES_DB=agentlab -e POSTGRES_PASSWORD_FILE=/run/agentlab/password \
      -v "$PG_VOLUME:/var/lib/postgresql/data" -v "$DEV_DATA/.postgres-dev/password:/run/agentlab/password:ro" \
      -p "127.0.0.1:$PG_PORT:5432" "$PG_IMAGE" >/dev/null
  fi
  # 走 TCP 问：第一次初始化时临时起的那个只听本地套接字，问得通才是正式起好了
  ready=""
  for _ in $(seq 1 60); do
    if docker exec "$PG_CONTAINER" pg_isready -q -h 127.0.0.1 -U agentlab -d agentlab 2>/dev/null; then ready=1; break; fi
    [ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null)" = true ] || break
    sleep 1
  done
  [ -n "$ready" ] || { docker logs --tail 30 "$PG_CONTAINER" 2>&1 || true; echo "开发用的 PostgreSQL 没起来，见上面的日志"; exit 1; }
  export AGENTLAB_DATABASE_URL="postgresql+asyncpg://agentlab@127.0.0.1:$PG_PORT/agentlab"
  export PGPASSFILE="$DEV_DATA/.postgres-dev/pgpass"
  echo "==> 应用库 PostgreSQL 127.0.0.1:$PG_PORT/agentlab（容器 ${PG_CONTAINER}）"
fi

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
# 开发时一个进程、改了代码自动重启（--reload 先停旧的再起新的，SQLite 的单进程守卫不会挡住它）。要试多进程用
# scripts/prod.sh --workers N（应用库要是 PostgreSQL），或者 scripts/test-multiprocess.sh 跑多进程端到端
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
(cd "$ROOT/frontend" && pnpm dev --port "$WEB_PORT" ${WEB_ARGS[@]+"${WEB_ARGS[@]}"}) &
WEB_PID=$!

echo
echo "  界面   http://localhost:$WEB_PORT"
if [ ${#WEB_ARGS[@]} -gt 0 ]; then
  # 本机在局域网里的地址：macOS 取 en0 / en1，Linux 取 hostname -I 的第一个
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
  echo "  局域网 http://${LAN_IP:-<本机 IP>}:$WEB_PORT   （没有登录：同一网络里打得开页面的人都能用它）"
fi
echo "  接口   http://127.0.0.1:$API_PORT/docs"
echo
wait
