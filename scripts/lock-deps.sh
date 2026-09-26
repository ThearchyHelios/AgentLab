#!/usr/bin/env bash
# 重新生成 backend/requirements.lock：后端全部依赖（含间接依赖、全部 extras）的精确版本。
#
#   ./scripts/lock-deps.sh                                 加了依赖：只补新的，已锁的版本不动
#   ./scripts/lock-deps.sh --upgrade-package langgraph     升一个包（连带它必须跟着动的）
#   ./scripts/lock-deps.sh --upgrade                       全部升到 pyproject 允许的最新
#
# 升级之后跑一遍 pytest 和前端检查再提交——锁文件的意义就是"这一套测过"。
# 锁是跨平台的（--universal），Linux / Windows 上装到的也是同一套版本。
# 需要 uv（brew install uv）；装包慢可以在命令前加 UV_INDEX_URL=<镜像地址>。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v uv >/dev/null || { echo "缺少 uv：https://docs.astral.sh/uv/"; exit 1; }

cd "$ROOT/backend"
uv pip compile pyproject.toml \
  --all-extras \
  --universal \
  --python-version 3.12 \
  --custom-compile-command "./scripts/lock-deps.sh" \
  -o requirements.lock \
  "$@"
