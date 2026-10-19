#!/usr/bin/env bash
# 多进程端到端验收（docs/design/orchestration-round §5 W2-b）：本机 docker 起一个临时的 postgres:16 容器（只绑 127.0.0.1 的随机
# 端口、数据放内存里，跑完删掉，同 test-postgres.sh），在上面起两个工作进程（uvicorn，指着同一个库、同一个数据目录），用演示模型
# 跑 backend/tests/test_multiprocess_e2e.py：运行由任一进程认领、另一个进程上的订阅收得到实时事件、同一个审批决定只执行一次、
# 跨进程停止、定时只触发一次、评测和问数据并发时优先级生效、每日花费上限跨进程不越过、强杀一个进程后租约过期被接手或照实标中断。
#
#   scripts/test-multiprocess.sh                      跑一遍（工作进程的日志在失败时打出来）
#   scripts/test-multiprocess.sh -k kill              参数原样交给 pytest
#   PYTHON=/path/to/python scripts/test-multiprocess.sh   指定装着后端依赖（含 [postgres]、[dev]）的 Python，缺省 python
#   AGENTLAB_PG_IMAGE=postgres:16 …                   换镜像（要本机已经有，这里不拉）
#
# 作为 pytest 的一组：tests/test_multiprocess_e2e.py 缺省跳过，要 AGENTLAB_MULTIPROCESS_E2E=1 和 AGENTLAB_TEST_DATABASE_URL
# （这个脚本设好两样）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python}"
IMAGE="${AGENTLAB_PG_IMAGE:-postgres:16}"
NAME="agentlab-mptest-$$"
USER_NAME="agentlab"
PASS="mptest-only"
DB="agentlab_test"

command -v docker >/dev/null || { echo "缺少 docker"; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "本机没有镜像 ${IMAGE}（这个脚本不拉镜像）：先 docker pull ${IMAGE}"; exit 1; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

docker run -d --name "$NAME" -e POSTGRES_USER="$USER_NAME" -e POSTGRES_PASSWORD="$PASS" -e POSTGRES_DB="$DB" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data:rw "$IMAGE" \
  -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=300 \
  >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U "$USER_NAME" -d "$DB" -h 127.0.0.1 >/dev/null 2>&1; then break; fi
  sleep 0.5
done
docker exec "$NAME" pg_isready -U "$USER_NAME" -d "$DB" -h 127.0.0.1 >/dev/null || { echo "PostgreSQL 没起来"; docker logs "$NAME" | tail -20; exit 1; }
PORT="$(docker port "$NAME" 5432/tcp | head -1 | sed 's/.*://')"

export AGENTLAB_TEST_DATABASE_URL="postgresql+asyncpg://$USER_NAME:$PASS@127.0.0.1:$PORT/$DB"
export AGENTLAB_MULTIPROCESS_E2E=1
echo "测试库：127.0.0.1:${PORT}/${DB}（容器 ${NAME}，结束后删除）"

cd "$ROOT/backend"
args=("$@")
# 不并行（-p no:xdist）：工作进程、端口、库都是这一组共用的；-s 让工作进程的日志在失败时看得到
env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u OPENAI_API_KEY \
  "$PY" -m pytest tests/test_multiprocess_e2e.py -p no:xdist -p no:randomly -rA "${args[@]}"
