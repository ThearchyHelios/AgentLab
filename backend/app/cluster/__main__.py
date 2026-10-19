"""python -m app.cluster workers：照后端自己的配置（环境变量和 .env 一起读）说起几个工作进程，scripts/prod.sh 和 Docker 的入口按它给
uvicorn --workers（复核 R-b4：写在 .env 里的 AGENTLAB_DATABASE_URL、AGENTLAB_WORKERS，shell 脚本自己读不到）。应用库是 SQLite 而进程数
大于 1 时照实报错、退出码 2（SQLite 只支持单进程）。"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if argv[:1] != ["workers"]:
        print("用法：python -m app.cluster workers", file=sys.stderr)
        return 64
    from app.cluster.guard import SINGLE_PROCESS
    from app.core.config import settings

    if settings.db_dialect == "sqlite" and settings.workers > 1:
        print(SINGLE_PROCESS.format(dir=settings.data_dir, hint=f"AGENTLAB_WORKERS={settings.workers}"), file=sys.stderr)
        return 2
    print(settings.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
