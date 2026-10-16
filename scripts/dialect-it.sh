#!/usr/bin/env bash
# 方言的容器集成测试（下一轮 03 细案 C2）：本机 docker 起 PostgreSQL 16、MySQL 8.4、MariaDB 11、Oracle Free 23 四个容器，
# 建虚构的夹具（保留字列、Oracle 小写带引号的表、CHAR 码值、MySQL ENUM、零日期 / 无穷大 / 占位日期、做旧的统计信息、
# 超过整表统计上限的大表、带索引的日期列），用 AgentLab 自己的探查和剖析代码跑全部剖析查询和取值观察的新方法，
# 断言零报错、结论和手算的一致。
#
# 为什么不进 CI：要拉几 GB 的镜像、Oracle 起一次要一两分钟。要用户同意才在本机跑。跑完删容器、留镜像。
#
# 用法：scripts/dialect-it.sh                 四种库都跑
#       DIALECT_IT_ONLY=pg,mysql scripts/dialect-it.sh   只跑其中几种（pg、mysql、mariadb、oracle）
#       PYTHON=/path/to/python scripts/dialect-it.sh   指定装着后端依赖的 Python（缺省 python3）
#
# 夹具全是虚构的通用名字（stores、orders、events、visits），不连任何业务库。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
ONLY="${DIALECT_IT_ONLY:-pg,mysql,mariadb,oracle}"
PREFIX="agentlab-dialect-it"
PASS="itpass"

started=()
cleanup() {
  for name in "${started[@]}"; do
    docker rm -f "$name" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

want() { [[ ",$ONLY," == *",$1,"* ]]; }

run() {
  local name="$PREFIX-$1"; shift
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" "$@" >/dev/null
  started+=("$name")
}

host_port() { docker port "$PREFIX-$1" "$2" | head -1 | sed 's/.*://'; }

if want pg; then
  run pg -e POSTGRES_PASSWORD="$PASS" -e POSTGRES_DB=it -p 127.0.0.1::5432 postgres:16
fi
if want mysql; then
  run mysql -e MYSQL_ROOT_PASSWORD="$PASS" -e MYSQL_DATABASE=it -p 127.0.0.1::3306 mysql:8.4
fi
if want mariadb; then
  run mariadb -e MARIADB_ROOT_PASSWORD="$PASS" -e MARIADB_DATABASE=it -p 127.0.0.1::3306 mariadb:11
fi
if want oracle; then
  run oracle -e ORACLE_PASSWORD="$PASS" -e APP_USER=it -e APP_USER_PASSWORD="$PASS" -p 127.0.0.1::1521 \
    gvenzl/oracle-free:23-slim
fi

export DIALECT_IT_PASSWORD="$PASS" DIALECT_IT_ONLY="$ONLY"
want pg && export DIALECT_IT_PG_PORT="$(host_port pg 5432/tcp)"
want mysql && export DIALECT_IT_MYSQL_PORT="$(host_port mysql 3306/tcp)"
want mariadb && export DIALECT_IT_MARIADB_PORT="$(host_port mariadb 3306/tcp)"
want oracle && export DIALECT_IT_ORACLE_PORT="$(host_port oracle 1521/tcp)"

# 等库起来由 Python 那边按连接重试（Oracle 最慢，最多等 6 分钟）
PYTHONPATH="$ROOT/backend" "$PY" "$ROOT/scripts/dialect_it.py"
