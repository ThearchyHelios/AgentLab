"""部署脚本（deploy/deploy.sh、scripts/dev.sh）在容器里调的几件小事：`python -m app.ops <命令>`。

放在镜像里用 Python 做，不在宿主机上用 shell 做：数据目录归容器里的用户（uid 10001），宿主机上跑 deploy.sh 的人不一定写得
进去；生成密码、改权限、拷 SQLite、删旧备份在容器里以同一个用户做，属主和权限都对。只用标准库，不连应用库、不读设置。

    postgres-secrets   内置 PostgreSQL 的密码：<数据目录>/.postgres/password（给 PostgreSQL 容器建库时读）和 pgpass（给应用
                       连库时读，PGPASSFILE 指过来）。已有就不动；密码不打印、不进任何参数和日志
    state              数据目录用的是哪种库：没切过是 sqlite；--set builtin 记成「已切到内置 PostgreSQL」（deploy.sh 只在导入、
                       核对都通过之后才记，记下之前出错就还是 sqlite）；migrating 是「开始往内置库里导了、还没切成」——
                       用的仍是 SQLite，只是让下一次 migrate-to-postgres 认得出数据卷里那半份是自己上次留下的。
                       --container / --volume / --network 记下这个数据目录的内置库用的名字；第一次记时生成部署编号 id
                       （deploy.sh 把它打在库的容器、数据卷、网络的标签上，按它认哪些是这个部署的）。--get 键 打印一项
    backup-sqlite      拷一份 agentlab.db、checkpoints.db 和填报的记录表 records/**/*.db 到 <数据目录>/backups/sqlite/<时间>/
                       （SQLite 的备份接口，各自一致的一份，记录表保持 records/ 下的相对路径），只留最近 --keep 份。上传原件、
                       上传表格的版本库、工件、附件、沙箱工作区不在里面：那些要整个数据目录一起备份
    backup-dir         建（或收紧）备份目录：从 backups/ 起每一层 0700（pg_dump 写进去之前用）
    prune              目录里按名字排序只留最近 --keep 个（pg_dump 的备份用）

备份里是整个库（含加密存的密钥、业务数据），目录一律 0700、文件 0600，属主是容器里的用户（同数据目录）；宿主机上要读用 sudo。
"""

from __future__ import annotations

import argparse
import contextlib
import os
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
BACKUPS_DIR = "backups"
#: 备份的目录、文件的权限：只有属主（容器里的用户）能读
BACKUP_DIR_MODE = 0o700
BACKUP_FILE_MODE = 0o600


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


#: 状态文件里的几项（顺序就是写出来的顺序）。名字三项：这个数据目录的内置库用的容器、数据卷、网络
STATE_KEYS = ("mode", "since", "id", "container", "volume", "network")
NAME_KEYS = ("container", "volume", "network")


def read_state_values(data: Path) -> dict[str, str]:
    path = data / POSTGRES_DIR / STATE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in STATE_KEYS:
            values[key.strip()] = value.strip()
    return values


def read_state(data: Path) -> str:
    """数据目录用的是哪种库：sqlite（没切过）、builtin（已切到内置 PostgreSQL）或 migrating（切到一半，用的仍是 SQLite）。"""
    mode = read_state_values(data).get("mode", "sqlite")
    return mode if mode in MODES else "sqlite"


def write_state(data: Path, mode: str | None = None, **names: str | None) -> dict[str, str]:
    """改模式（mode 给了才改；改了才换 since）、记名字（给了的那几项）；别的照旧。第一次写时生成部署编号 id，之后不变。"""
    if mode is not None and mode not in MODES:
        raise SystemExit(f"认不出的模式：{mode}（{' / '.join(MODES)}）")
    unknown = set(names) - set(NAME_KEYS)
    if unknown:
        raise SystemExit(f"认不出的名字：{'、'.join(sorted(unknown))}")
    values = read_state_values(data)
    if mode is not None and values.get("mode") != mode:
        values["mode"] = mode
        values["since"] = datetime.now(timezone.utc).isoformat()
    values.setdefault("mode", "sqlite")
    values.setdefault("since", datetime.now(timezone.utc).isoformat())
    values.setdefault("id", secrets.token_hex(6))
    for key, value in names.items():
        if value:
            values[key] = value
    root = data / POSTGRES_DIR
    if not root.exists():
        root.mkdir(parents=True)
        root.chmod(0o711)
    # 不是密钥：宿主机上的 deploy.sh 直接读它，0644
    text = "".join(f"{key}={values[key]}\n" for key in STATE_KEYS if values.get(key))
    atomic.write_text(root / STATE_FILE, text, mode=0o644)
    return values


@contextlib.contextmanager
def _private_umask():
    """这一段里新建的文件、目录别人读不到（SQLite 备份时建的 -journal 也一样），出来恢复原来的 umask。"""
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def backup_dir(data: Path, *parts: str) -> Path:
    """<数据目录>/backups/<parts…>：从 backups/ 起每一层建出来（已有的收紧）成 0700，交回最里面那层。"""
    folder = data / BACKUPS_DIR
    levels = [folder]
    for part in parts:
        folder = folder / part
        levels.append(folder)
    for level in levels:
        level.mkdir(exist_ok=True)
        level.chmod(BACKUP_DIR_MODE)
    return folder


def _copy_sqlite(src: Path, dst: Path) -> None:
    # 读写打开、只许读：WAL 模式的库用 mode=ro 打开，关掉时会留下它自己建的 -wal（同 db/to_postgres.snapshot）
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA query_only = 1")
    try:
        # 先以 0600 建出空文件再交给 SQLite 写：从有这个文件起就只有属主读得到
        os.close(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, BACKUP_FILE_MODE))
        out = sqlite3.connect(dst)
        try:
            conn.backup(out)
        finally:
            out.close()
        dst.chmod(BACKUP_FILE_MODE)
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


def _sqlite_sources(data: Path) -> list[Path]:
    """要备份的 SQLite 文件（相对数据目录）：应用库、断点库，和填报的记录表（records/ 下，原地追加写，开着服务直接拷可能拷到
    写了一半的，所以也走备份接口）。"""
    found = [Path(name) for name in ("agentlab.db", "checkpoints.db") if (data / name).is_file()]
    records = data / "records"
    if records.is_dir():
        found += sorted(p.relative_to(data) for p in records.rglob("*.db") if p.is_file())
    return found


def backup_sqlite(data: Path, *, keep: int) -> Path | None:
    """agentlab.db、checkpoints.db、records/**/*.db 拷到 backups/sqlite/<时间>/（相对路径照旧）。应用库和断点库都没有（库在
    PostgreSQL）交回 None——记录表那时由整目录备份管。"""
    sources = _sqlite_sources(data)
    if not any(p.parent == Path(".") for p in sources):
        return None
    with _private_umask():
        root = backup_dir(data, "sqlite")
        target = root / _stamp()
        target.mkdir(exist_ok=True)
        target.chmod(BACKUP_DIR_MODE)
        for rel in sources:
            dst = target / rel
            for folder in reversed(dst.relative_to(target).parents[:-1]):
                (target / folder).mkdir(exist_ok=True)
                (target / folder).chmod(BACKUP_DIR_MODE)
            _copy_sqlite(data / rel, dst)
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
    for key in NAME_KEYS:
        s.add_argument(f"--{key}")
    s.add_argument("--get", choices=STATE_KEYS)
    b = sub.add_parser("backup-sqlite")
    b.add_argument("--data", default="/data")
    b.add_argument("--keep", type=int, default=5)
    d = sub.add_parser("backup-dir")
    d.add_argument("--data", default="/data")
    d.add_argument("parts", nargs="*", help="backups/ 下面的几层，比如 postgres")
    r = sub.add_parser("prune")
    r.add_argument("--dir", required=True)
    r.add_argument("--keep", type=int, default=5)
    args = parser.parse_args(argv)

    if args.command == "postgres-secrets":
        made = postgres_secrets(Path(args.data), folder=args.dir, host=args.host, port=args.port, database=args.db,
                                user=args.user)
        print("生成了新密码" if made else "沿用已有的密码")
    elif args.command == "state":
        names = {key: getattr(args, key) for key in NAME_KEYS if getattr(args, key)}
        if args.set or names:
            write_state(Path(args.data), args.set, **names)
        if args.get:
            print(read_state_values(Path(args.data)).get(args.get, ""))
        else:
            print(read_state(Path(args.data)))
    elif args.command == "backup-sqlite":
        target = backup_sqlite(Path(args.data), keep=args.keep)
        print(target if target is not None else "没有 SQLite 库，不用备份")
    elif args.command == "backup-dir":
        print(backup_dir(Path(args.data), *args.parts))
    elif args.command == "prune":
        for gone in prune(Path(args.dir), args.keep):
            print(f"删掉旧备份 {gone.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
