"""应用库的维护命令：python -m app.db <命令>。

    copy-to-postgres   把 SQLite 数据目录里的应用库和断点导入 PostgreSQL（db/to_postgres.py；先停服务）
"""

from __future__ import annotations

import sys

COMMANDS = {"copy-to-postgres": "把 SQLite 数据目录里的应用库和断点导入 PostgreSQL（先停服务）"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("用法：python -m app.db <命令> [参数]\n")
        for name, what in COMMANDS.items():
            print(f"  {name:<18}{what}")
        print("\n各命令加 --help 看参数")
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "copy-to-postgres":
        from app.db.to_postgres import main as copy_main

        return copy_main(rest)
    print(f"认不出的命令：{command}（可用的：{'、'.join(COMMANDS)}）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
