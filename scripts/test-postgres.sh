#!/usr/bin/env bash
# 后端测试跑在 PostgreSQL 上（应用库和断点库都是 PostgreSQL）：本机 docker 起一个临时的 postgres:16 容器，只绑
# 127.0.0.1 的随机端口、数据放内存里，跑完（出错、Ctrl+C 也一样）删掉容器，留镜像。
#
#   scripts/test-postgres.sh                          全量，-n auto
#   scripts/test-postgres.sh tests/test_db_portable.py -x   参数原样交给 pytest（给了参数就不再加 -n auto）
#   PYTHON=/path/to/python scripts/test-postgres.sh   指定装着后端依赖（含 [postgres]、[dev]）的 Python，缺省 python
#   AGENTLAB_PG_IMAGE=postgres:16 …                   换镜像（要本机已经有，这里不拉）
#
# 测试怎么用这个库见 backend/tests/conftest.py：AGENTLAB_TEST_DATABASE_URL 给的是管理用的库，每个 xdist 进程在上面
# 建自己的库（库名加 _gw0 之类的后缀），测试结束删掉。只对 SQLite 成立的测试标了 sqlite_only，这里跳过。
# 不碰 scripts/dialect-it.sh 的容器（那边叫 agentlab-dialect-it-*）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python}"
IMAGE="${AGENTLAB_PG_IMAGE:-postgres:16}"
NAME="agentlab-pgtest-$$"
USER_NAME="agentlab"
PASS="pgtest-only"
DB="agentlab_test"

command -v docker >/dev/null || { echo "缺少 docker"; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "本机没有镜像 ${IMAGE}（这个脚本不拉镜像）：先 docker pull ${IMAGE}"; exit 1; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

# fsync、同步提交都关掉：测试库用完就扔，不需要落盘的保证。连接数放宽：每个 xdist 进程按事件循环各有一个池子
# （tests/pgpool.py），断点库还各有一个小池子。认证用 trust（只绑 127.0.0.1、用完即删的库）：每个测试的事件循环都要
# 新建连接，SCRAM 握手一次十几毫秒，trust 三毫秒
docker run -d --name "$NAME" -e POSTGRES_USER="$USER_NAME" -e POSTGRES_PASSWORD="$PASS" -e POSTGRES_DB="$DB" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data:rw "$IMAGE" \
  -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=1000 -c shared_buffers=256MB \
  >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U "$USER_NAME" -d "$DB" -h 127.0.0.1 >/dev/null 2>&1; then break; fi
  sleep 0.5
done
docker exec "$NAME" pg_isready -U "$USER_NAME" -d "$DB" -h 127.0.0.1 >/dev/null || { echo "PostgreSQL 没起来"; docker logs "$NAME" | tail -20; exit 1; }
PORT="$(docker port "$NAME" 5432/tcp | head -1 | sed 's/.*://')"

export AGENTLAB_TEST_DATABASE_URL="postgresql+asyncpg://$USER_NAME:$PASS@127.0.0.1:$PORT/$DB"
echo "测试库：127.0.0.1:${PORT}/${DB}（容器 ${NAME}，结束后删除）"

cd "$ROOT/backend"
args=("$@")
[ ${#args[@]} -eq 0 ] && args=(-n auto)
# 同 README「开发与验证」：环境里的密钥不带进测试
env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
  "$PY" -m pytest "${args[@]}"
