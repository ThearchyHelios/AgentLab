"""部署脚本（deploy/deploy.sh、scripts/dev.sh）在容器里调的几件小事：`python -m app.ops <命令>`。

放在镜像里用 Python 做，不在宿主机上用 shell 做：数据目录归容器里的用户（uid 10001），宿主机上跑 deploy.sh 的人不一定写得
进去；生成密码、改权限、拷 SQLite、删旧备份在容器里以同一个用户做，属主和权限都对。只用标准库，不连应用库、不读设置。

    postgres-secrets   内置 PostgreSQL 的密码：<数据目录>/.postgres/password（给 PostgreSQL 容器建库时读）和 pgpass（给应用
                       连库时读，PGPASSFILE 指过来）。已有就不动；密码不打印、不进任何参数和日志
    state              数据目录用的是哪种库：没切过是 sqlite；--set builtin 记成「已切到内置 PostgreSQL」（deploy.sh 只在导入、
                       核对都通过之后才记，记下之前出错就还是 sqlite）；migrating 是「开始往内置库里导了、还没切成」——
                       用的仍是 SQLite，只是让下一次 migrate-to-postgres 认得出数据卷里那半份是自己上次留下的
    backup-sqlite      拷一份 agentlab.db、checkpoints.db 到 <数据目录>/backups/sqlite/<时间>/（SQLite 的备份接口，一致的一份），
                       只留最近 --keep 份
    prune              目录里按名字排序只留最近 --keep 个（pg_dump 的备份用）
"""

from __future__ import annotations

import argparse
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.core import atomic

#: 内置 PostgreSQL 的东西放在数据目录的这个子目录里
POSTGRES_DIR = ".postgres"
STATE_FILE = "state"
MODES = ("sqlite", "builtin", "migrating")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def postgres_secrets(data: Path, *, folder: str, host: str, port: int, database: str, user: str) -> bool:
    """建密码文件和 pgpass（都是 0600）。已有密码就沿用（库是按它初始化的），pgpass 按现在的主机名重写。交回是不是新建的密码。"""
    root = data / folder
    root.mkdir(parents=True, exist_ok=True)
    # 0711：别人列不出目录、读不到密码（两个文件 0600），但宿主机上跑 deploy.sh 的人能按名字读到 state（0644）
    root.chmod(0o711)
    password_file = root / "password"
    made = False
    if not password_file.exists():
        made = atomic.create_text(password_file, secrets.token_urlsafe(32) + "\n", mode=0o600)
    password = password_file.read_text(encoding="utf-8").strip()
    if not password:
        raise SystemExit(f"{password_file} 是空的：删掉它再来会生成新的（库已经按旧密码初始化过的话，要连库一起重建）")
    # pgpass 的格式：主机:端口:库:用户:密码，冒号和反斜杠要转义
    escaped = password.replace("\\", "\\\\").replace(":", "\\:")
    atomic.write_text(root / "pgpass", f"{host}:{port}:{database}:{user}:{escaped}\n", mode=0o600)
    return made


def read_state(data: Path) -> str:
    """数据目录用的是哪种库：sqlite（没切过）、builtin（已切到内置 PostgreSQL）或 migrating（切到一半，用的仍是 SQLite）。"""
    path = data / POSTGRES_DIR / STATE_FILE
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("mode="):
                mode = line.split("=", 1)[1].strip()
                return mode if mode in MODES else "sqlite"
    except FileNotFoundError:
        pass
    return "sqlite"


def write_state(data: Path, mode: str) -> None:
    if mode not in MODES:
        raise SystemExit(f"认不出的模式：{mode}（{' / '.join(MODES)}）")
    root = data / POSTGRES_DIR
    if not root.exists():
        root.mkdir(parents=True)
        root.chmod(0o711)
    # 不是密钥：宿主机上的 deploy.sh 直接读它，0644
    atomic.write_text(root / STATE_FILE, f"mode={mode}\nsince={datetime.now(timezone.utc).isoformat()}\n", mode=0o644)


def _copy_sqlite(src: Path, dst: Path) -> None:
    # 读写打开、只许读：WAL 模式的库用 mode=ro 打开，关掉时会留下它自己建的 -wal（同 db/to_postgres.snapshot）
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA query_only = 1")
    try:
        out = sqlite3.connect(dst)
        try:
            conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()


def prune(folder: Path, keep: int) -> list[Path]:
    """folder 里按名字（名字里带时间，排序就是先后）只留最近 keep 个，交回删掉的。"""
    if keep < 1 or not folder.is_dir():
        return []
    items = sorted((p for p in folder.iterdir() if not p.name.startswith(".")), key=lambda p: p.name)
    gone = items[:-keep] if len(items) > keep else []
    for path in gone:
        if path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
            path.rmdir()
        else:
            path.unlink()
    return gone


def backup_sqlite(data: Path, *, keep: int) -> Path | None:
    """agentlab.db、checkpoints.db 拷到 backups/sqlite/<时间>/。两个都没有（库在 PostgreSQL）交回 None。"""
    sources = [p for p in (data / "agentlab.db", data / "checkpoints.db") if p.is_file()]
    if not sources:
        return None
    root = data / "backups" / "sqlite"
    target = root / _stamp()
    target.mkdir(parents=True, exist_ok=True)
    for src in sources:
        _copy_sqlite(src, target / src.name)
    prune(root, keep)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ops", description="部署脚本在容器里调的几件小事")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("postgres-secrets")
    p.add_argument("--data", default="/data")
    p.add_argument("--dir", default=POSTGRES_DIR)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=5432)
    p.add_argument("--db", default="agentlab")
    p.add_argument("--user", default="agentlab")
    s = sub.add_parser("state")
    s.add_argument("--data", default="/data")
    s.add_argument("--set", choices=MODES)
    b = sub.add_parser("backup-sqlite")
    b.add_argument("--data", default="/data")
    b.add_argument("--keep", type=int, default=5)
    r = sub.add_parser("prune")
    r.add_argument("--dir", required=True)
    r.add_argument("--keep", type=int, default=5)
    args = parser.parse_args(argv)

    if args.command == "postgres-secrets":
        made = postgres_secrets(Path(args.data), folder=args.dir, host=args.host, port=args.port, database=args.db,
                                user=args.user)
        print("生成了新密码" if made else "沿用已有的密码")
    elif args.command == "state":
        if args.set:
            write_state(Path(args.data), args.set)
        print(read_state(Path(args.data)))
    elif args.command == "backup-sqlite":
        target = backup_sqlite(Path(args.data), keep=args.keep)
        print(target if target is not None else "没有 SQLite 库，不用备份")
    elif args.command == "prune":
        for gone in prune(Path(args.dir), args.keep):
            print(f"删掉旧备份 {gone.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
