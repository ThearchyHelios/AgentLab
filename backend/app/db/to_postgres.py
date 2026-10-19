"""把 SQLite 数据目录里的应用库和断点导入 PostgreSQL：`python -m app.db copy-to-postgres`。

    python -m app.db copy-to-postgres <数据目录或 agentlab.db> [目标地址] [--checkpoint-url 地址] [--dry-run]

目标地址缺省取 AGENTLAB_DATABASE_URL；断点缺省进同一个库（AGENTLAB_CHECKPOINT_URL 或 --checkpoint-url 另给）。

**先停服务。** 来源库旁边有 agentlab.db-wal（WAL 模式下还有连接开着，或者上次是强杀），或者目标库上有 application_name
是 agentlab 的连接（服务已经连着 PostgreSQL 跑起来了），就拒绝导入。确认是强杀留下的 -wal 可以加 --assume-stopped；
这只是能查到的两样，查不到的（比如服务连着别的数据目录的拷贝、另一台机器上的服务）要自己保证。

**读的是一份快照。** 先用 SQLite 的备份接口把 agentlab.db、checkpoints.db 拷到临时目录（拷贝过程中别人在写也拿到
一致的一份；放在 --work-dir，缺省系统临时目录，要留出和两个库一样大的空间），在拷贝上跑一遍和启动时一样的建表补列
（老版本的库升级到当前的表结构），从拷贝读，来源的内容不改。

**幂等。** 目标上建表（同启动时的 create_schema），按外键先后逐表、按主键分批插入，`ON CONFLICT DO NOTHING`：
目标上已经有同一主键（或撞了别的唯一约束）的行一律不动，不覆盖、不合并——重跑只补上还没有的行，不重复。所以
导入之后又在 SQLite 上改过的行，重跑不会带过去；要整份重来就清空目标库再导。断点同理：目标上已有的断点不重写，
pending writes 按（断点、任务、序号）插入，已有的不动。

**核对。** 每张表按主键核对：来源的每一行，目标上有没有同一主键的行（导入前就有的、这次补上的分开数），断点和
pending writes 按条数核对，逐项打印；有来源的行目标上没有（撞了约束没插进去的会列出主键和原因），退出码非零。
目标上另有来源里没有的行不算错（同一个目标导过别的、或者服务已经在上面跑过）。

**值的转换。** 读写都按模型的列类型走：时间是不带时区的 UTC、布尔是真布尔、JSON 原文照搬（SQL 的 NULL 和 JSON 的
null 分开，不混）。SQLite 不查类型、PostgreSQL 查：整数列里的整数值的浮点、数字字符串换成整数，文本列里的非文本换成
字符串；文本里的 NUL 字符（PostgreSQL 不收）去掉，JSON 里的 NaN / Infinity 写成 null；模型里 NOT NULL、有缺省值的列，
老库补列时没带缺省值留下的 NULL 填成缺省值（启动时的迁移在 SQLite 上也这么填）；各自计数打印。

**孤儿行。** 外键指向已经不在的行（SQLite 当时没强制外键留下的）：外键是 SET NULL 的置空、计入转换；别的从快照里拿掉、
不导入，按表列出条数，不算核对失败（settle_orphans）。

**自增主键。** 导入后序列接在已用过的最大号后面：目标表的 MAX、SQLite 的 AUTOINCREMENT 计数器（删过的号不复用）取大的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, Float, Integer, LargeBinary, String, Text, bindparam, cast, func, select, text
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.db.urls import describe, libpq_conninfo, normalize_checkpoint_url, normalize_database_url

COPY_APPLICATION_NAME = "agentlab-copy"
DEFAULT_BATCH = 1000


@dataclass
class TableResult:
    name: str
    #: 来源的行数（不含孤儿行）
    source: int = 0
    #: 外键指向已不存在的行、又不能置空的（外键是 CASCADE 或不可空）：SQLite 当时没强制外键留下的，没导入
    orphans: int = 0
    #: 来源的这些主键，目标上导入前已经有的（--dry-run 时就是「不用补的」）、导入后有的
    present_before: int = 0
    present_after: int = 0
    #: 目标整张表的行数（导入前、导入后）：比来源多出来的是目标上原有的
    target_before: int = 0
    target_after: int = 0
    refused: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        """来源有、目标上还没有的行（按主键数）。"""
        return self.source - self.present_after


@dataclass
class Report:
    tables: list[TableResult] = field(default_factory=list)
    checkpoints: tuple[int, int] = (0, 0)              # 来源（断点、pending writes）
    checkpoints_target: tuple[int, int] = (0, 0)       # 目标上同一批线程的
    checkpoints_written: int = 0
    threads: int = 0
    fixed: dict[str, int] = field(default_factory=dict)
    #: 没导入的孤儿行，按表（见 settle_orphans）
    orphans: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        if self.dry_run:
            return True
        tables_ok = all(t.present_after == t.source and not t.refused for t in self.tables)
        return tables_ok and self.checkpoints_target[0] >= self.checkpoints[0] \
            and self.checkpoints_target[1] >= self.checkpoints[1]


class CopyRefused(RuntimeError):
    """导入前的检查没过（服务还开着、来源不对）：照实说明，什么都没写。"""


# --------------------------------------------------------------------------
# 来源
# --------------------------------------------------------------------------


def resolve_source(raw: str | Path) -> tuple[Path, Path | None]:
    """数据目录或 agentlab.db → (应用库文件, 断点库文件或 None)。"""
    path = Path(raw).expanduser()
    db = path / "agentlab.db" if path.is_dir() else path
    if not db.is_file():
        raise CopyRefused(f"找不到应用库：{db}（给数据目录，或者直接给 agentlab.db 的路径）")
    with open(db, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise CopyRefused(f"{db} 不是 SQLite 库文件")
    ckpt = db.parent / "checkpoints.db"
    return db, (ckpt if ckpt.is_file() else None)


def source_in_use(db: Path, ckpt: Path | None = None) -> Path | None:
    """来源库是不是还有连接开着：WAL 模式下最后一个连接正常关掉时 -wal 会被删掉，还在就是有人开着（或者上次是强杀）。
    交回还在的那个 -wal（应用库、断点库各看一个），都没有是 None。"""
    for path in (db, ckpt):
        if path is not None and (wal := Path(f"{path}-wal")).exists():
            return wal
    return None


def snapshot(db: Path, into: Path) -> Path:
    """用 SQLite 的备份接口拷一份一致的快照（别人正在写也是某一刻的完整一份）。

    来源按读写打开、只许读（query_only）：WAL 模式的库用 mode=ro 打开，关掉时删不了它自己建出来的 -wal、-shm，
    下一次导入就会当成服务还开着；读写打开的连接是最后一个关的，关掉时 SQLite 把日志并回主库、删掉这两个文件
    （内容不变）。"""
    out = into / db.name
    src = sqlite3.connect(db)
    src.execute("PRAGMA query_only = 1")
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return out


def settle_orphans(path: Path, *, fixed: dict[str, int]) -> dict[str, int]:
    """快照上按模型的外键找孤儿行：外键列有值、指向的行却不在（SQLite 当时没强制外键：老版本、或者那时外键还没加上）。
    PostgreSQL 一律拒收，重跑也补不上，所以导入前在快照上（来源不动）先处理：

    - 外键是 ON DELETE SET NULL、列可空的：置空，计入「转换」——父行被删时本来就该这样；
    - 别的（CASCADE、不可空）：从快照里拿掉、不导入，按表计数交回——父行被删时本来就该跟着删。拿掉一行可能让下一层又成了
      孤儿，反复扫到没有为止。

    这些不算核对失败：报告里照实列出哪张表、几行。"""
    from sqlalchemy import and_, delete, exists, update

    from app.db import models  # noqa: F401
    from app.db.base import Base

    orphans: dict[str, int] = {}
    engine = create_sync_engine(f"sqlite:///{path}")
    try:
        while True:
            removed = 0
            with engine.begin() as conn:
                for table in Base.metadata.sorted_tables:
                    for fk in table.foreign_key_constraints:
                        parent = fk.referred_table.alias("parent")
                        pairs = [(el.parent, parent.c[el.column.name]) for el in fk.elements]
                        lost = and_(*(child.is_not(None) for child, _ in pairs),
                                    ~exists(select(1).select_from(parent).where(
                                        and_(*(p == child for child, p in pairs)))))
                        n = int(conn.execute(select(func.count()).select_from(table).where(lost)).scalar() or 0)
                        if not n:
                            continue
                        if (fk.ondelete or "").upper() == "SET NULL" and all(c.nullable for c, _ in pairs):
                            conn.execute(update(table).where(lost).values({c.name: None for c, _ in pairs}))
                            fixed["外键指向已不存在的行，置空"] = fixed.get("外键指向已不存在的行，置空", 0) + n
                        else:
                            conn.execute(delete(table).where(lost))
                            orphans[table.name] = orphans.get(table.name, 0) + n
                            removed += n
            if not removed:
                return orphans
    finally:
        engine.dispose()


def upgrade_snapshot(path: Path) -> None:
    """在快照上跑一遍启动时的建表补列：老版本的库按当前的表结构读。"""
    from app.db import models  # noqa: F401
    from app.db.base import Base, _migrate

    engine = create_sync_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
            _migrate(conn)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# 值
# --------------------------------------------------------------------------


def _is_json(column: Any) -> bool:
    return isinstance(column.type, JSON)


class _Fixer:
    """SQLite 里存得进、PostgreSQL 不收的值，换掉并计数。"""

    def __init__(self, fixed: dict[str, int]) -> None:
        self.fixed = fixed

    def _count(self, what: str) -> None:
        self.fixed[what] = self.fixed.get(what, 0) + 1

    def value(self, column: Any, value: Any) -> Any:
        if value is None:
            return self.missing(column)
        if _is_json(column):
            return self.json(value)
        kind = column.type
        if isinstance(kind, Boolean):
            return bool(value)
        if isinstance(kind, Integer):
            if isinstance(value, bool) or isinstance(value, int):
                return int(value)
            if isinstance(value, float) and value.is_integer():
                self._count("整数列里的浮点写法")
                return int(value)
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                self._count("整数列里的数字字符串")
                return int(value.strip())
            return value
        if isinstance(kind, Float):
            return float(value) if isinstance(value, (int, float)) else value
        if isinstance(kind, LargeBinary):
            return bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else str(value).encode()
        if isinstance(kind, (String, Text)) and not isinstance(value, str):
            self._count("文本列里的非文本值")
            value = str(value)
        if isinstance(value, str) and "\x00" in value:
            self._count("文本里的 NUL 字符")
            value = value.replace("\x00", "")
        return value

    def missing(self, column: Any) -> Any:
        """SQL 的 NULL。模型列 NOT NULL、又有缺省值的（老版本补列时没带缺省值，存量行留着 NULL；SQLite 的 ADD COLUMN
        不带 NOT NULL，照存）填成缺省值：PostgreSQL 的表照模型建，带 NOT NULL，不填就整行插不进。"""
        from app.db.base import model_default, pg_json_dumps

        if column.nullable or column.primary_key:
            return None
        known, default = model_default(column)
        if not known:
            return None
        self._count("NOT NULL 列的空值填成缺省值")
        return pg_json_dumps(default) if _is_json(column) else default

    def json(self, raw: Any) -> str:
        """JSON 原文 → 写进 PostgreSQL 的原文。平常的原样照搬（键的先后不变）；有 NaN / NUL 的才重写一遍。"""
        from app.db.base import pg_json_dumps

        textual = raw if isinstance(raw, str) else json.dumps(raw)
        if "NaN" in textual or "Infinity" in textual or "\\u0000" in textual:
            redone = pg_json_dumps(json.loads(textual))
            if redone != textual:
                self._count("JSON 里的 NaN / Infinity / NUL")
            return redone
        return textual


# --------------------------------------------------------------------------
# 应用库
# --------------------------------------------------------------------------


def _pk(table: Any) -> list[Any]:
    return list(table.primary_key.columns) or list(table.columns)


def _insert(table: Any, columns: list[Any]) -> Any:
    """目标上的插入：JSON 列按原文绑定再转 json（SQL 的 NULL 还是 NULL），撞了任何唯一约束都不动目标上的行。"""
    values = {c.name: (cast(bindparam(c.name, type_=Text()), c.type) if _is_json(c) else bindparam(c.name, type_=c.type))
              for c in columns}
    return postgresql.insert(table).values(values).on_conflict_do_nothing()


async def _count(conn: Any, table: Any) -> int:
    return int((await conn.execute(select(func.count()).select_from(table))).scalar() or 0)


async def _write_batch(target: Any, statement: Any, rows: list[dict[str, Any]], table: Any,
                       refused: list[str]) -> None:
    """一批一个事务。整批撞了约束（外键对不上之类）就逐行重试，插不进的记下主键和原因、跳过。"""
    try:
        async with target.begin() as conn:
            await conn.execute(statement, rows)
        return
    except IntegrityError:
        pass
    keys = [c.name for c in _pk(table)]
    for row in rows:
        try:
            async with target.begin() as conn:
                await conn.execute(statement, [row])
        except IntegrityError as e:
            from app.core.errors import first_line

            ident = ", ".join(f"{k}={row.get(k)!r}" for k in keys)
            refused.append(f"{ident}：{first_line(e)}")


def _present(table: Any, keys: list[tuple[Any, ...]]) -> Any:
    """目标上这些主键有几行。"""
    from sqlalchemy import tuple_

    pk = _pk(table)
    where = pk[0].in_([k[0] for k in keys]) if len(pk) == 1 else tuple_(*pk).in_(keys)
    return select(func.count()).select_from(table).where(where)


async def copy_tables(snapshot_db: Path, target: Any, *, batch: int, dry_run: bool, report: Report,
                      progress: Callable[[str], None]) -> None:
    """按外键先后逐表、按主键分批：插入（目标上已有的不动），每批插完按主键数一遍目标上有了几行。"""
    from sqlalchemy import type_coerce

    from app.db import models  # noqa: F401
    from app.db.base import Base

    fixer = _Fixer(report.fixed)
    source = create_sync_engine(f"sqlite:///{snapshot_db}")
    try:
        with source.connect() as src:
            # AUTOINCREMENT 表的计数器（删过最后几行时比 MAX 大）：PostgreSQL 的序列要接在它后面，不复用删掉的号——
            # 召回按 source_changes.seq 当游标往后拉，复用了就跳过新变化
            counters = dict(src.execute(text("SELECT name, seq FROM sqlite_sequence")).all()) if src.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")).first() else {}
            for table in Base.metadata.sorted_tables:
                result = TableResult(table.name, orphans=report.orphans.get(table.name, 0))
                report.tables.append(result)
                result.source = int(src.execute(select(func.count()).select_from(table)).scalar() or 0)
                async with target.connect() as conn:
                    exists = await conn.run_sync(lambda sync, name=table.name: _has_table(sync, name))
                    result.target_before = await _count(conn, table) if exists else 0
                columns = list(table.columns)
                pk_at = [columns.index(c) for c in _pk(table)]
                # JSON 列按原文取（type_coerce 成 Text：SQL 的 NULL 是 None，JSON 的 null 是 'null'），别的按列类型取
                reads = [type_coerce(c, Text()).label(c.name) if _is_json(c) else c for c in columns]
                statement = _insert(table, columns)
                rows = src.execution_options(yield_per=batch).execute(select(*reads).order_by(*_pk(table)))
                done = 0
                for n, part in enumerate(rows.partitions(batch), 1):
                    keys = [tuple(row[i] for i in pk_at) for row in part]
                    if exists:
                        async with target.connect() as conn:
                            result.present_before += int((await conn.execute(_present(table, keys))).scalar() or 0)
                    if not dry_run:
                        payload = [{c.name: fixer.value(c, row[i]) for i, c in enumerate(columns)} for row in part]
                        await _write_batch(target, statement, payload, table, result.refused)
                        async with target.connect() as conn:
                            result.present_after += int((await conn.execute(_present(table, keys))).scalar() or 0)
                    done += len(part)
                    if n % 20 == 0:
                        progress(f"  {table.name}：{done}/{result.source}")
                if dry_run:
                    result.present_after, result.target_after = result.present_before, result.target_before
                    continue
                await _reset_sequences(target, table, int(counters.get(table.name) or 0))
                async with target.connect() as conn:
                    result.target_after = await _count(conn, table)
    finally:
        source.dispose()


def _has_table(sync: Any, name: str) -> bool:
    from sqlalchemy import inspect

    return inspect(sync).has_table(name)


async def _reset_sequences(target: Any, table: Any, floor: int = 0) -> None:
    """自增主键的序列接到已用过的最大号后面：导入时写的是来源的主键值，序列自己没动过，不改的话下一条撞主键。
    已用过的取三者最大：目标表里的 MAX、SQLite 的 AUTOINCREMENT 计数器（floor）、序列现在的值（重跑时不往回拨）。"""
    for column in table.primary_key.columns:
        if column is not table.autoincrement_column:
            continue
        async with target.begin() as conn:
            name = (await conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
                                       {"t": table.name, "c": column.name})).scalar()
            if not name:
                continue
            top = int((await conn.execute(select(func.max(column)))).scalar() or 0)
            last, called = (await conn.execute(text(f"SELECT last_value, is_called FROM {name}"))).one()
            used = max(top, floor, int(last) if called else int(last) - 1)
            await conn.execute(text("SELECT setval(:n, :v, :called)"),
                               {"n": name, "v": max(used, 1), "called": used >= 1})


async def services_on(target: Any) -> int:
    """目标库上服务的连接数（application_name 是 agentlab 的，不算本命令自己的）。"""
    from app.db.base import APPLICATION_NAME

    async with target.connect() as conn:
        return int((await conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = :n "
            "AND pid <> pg_backend_pid()"), {"n": APPLICATION_NAME})).scalar() or 0)


# --------------------------------------------------------------------------
# 断点
# --------------------------------------------------------------------------


def _split_writes(writes: list[tuple[str, str, Any]]) -> dict[str, tuple[list[tuple[str, Any]], list[tuple[str, Any]]]]:
    """pending writes 按任务分组，各分成「特殊通道」（错误、中断、恢复……，序号是固定的负数）和普通的两拨：普通的分开
    单独写，序号才和来源里的一样从 0 排起（来源里一个任务在一个断点上的普通写入是一次写下的）。"""
    from langgraph.checkpoint.base import WRITES_IDX_MAP

    out: dict[str, tuple[list[tuple[str, Any]], list[tuple[str, Any]]]] = {}
    for task_id, channel, value in writes:
        special, regular = out.setdefault(task_id, ([], []))
        (special if channel in WRITES_IDX_MAP else regular).append((channel, value))
    return out


async def copy_checkpoints(snapshot_ckpt: Path, conninfo: str, *, dry_run: bool, report: Report,
                           progress: Callable[[str], None]) -> None:
    """逐条读出来源的断点（saver 的公开接口，含 pending writes），写进 PostgreSQL 的断点库，再按线程核对条数。"""
    from app.engine.checkpoints import count, open_postgres_saver, open_sqlite_saver

    threads: set[str] = set()
    async with open_sqlite_saver(snapshot_ckpt) as src:
        checkpoints = writes = 0
        async for tup in src.alist(None):
            checkpoints += 1
            writes += len(tup.pending_writes or ())
            threads.add(str(tup.config["configurable"]["thread_id"]))
        report.checkpoints, report.threads = (checkpoints, writes), len(threads)
        if dry_run:
            return
        async with open_postgres_saver(conninfo, max_size=4, application_name=COPY_APPLICATION_NAME) as dst:
            done = 0
            async for tup in src.alist(None):
                conf = tup.config["configurable"]
                if await dst.aget_tuple(tup.config) is None:
                    parent = (tup.parent_config or {}).get("configurable", {}).get("checkpoint_id")
                    put_config = {"configurable": {"thread_id": conf["thread_id"],
                                                   "checkpoint_ns": conf.get("checkpoint_ns", ""),
                                                   **({"checkpoint_id": parent} if parent else {})}}
                    await dst.aput(put_config, tup.checkpoint, tup.metadata,
                                   dict(tup.checkpoint.get("channel_versions") or {}))
                    report.checkpoints_written += 1
                for task_id, (special, regular) in _split_writes(list(tup.pending_writes or ())).items():
                    for group in (special, regular):
                        if group:
                            await dst.aput_writes(tup.config, group, task_id)
                done += 1
                if done % 500 == 0:
                    progress(f"  断点：{done}/{checkpoints}")
            target = [0, 0]
            for thread in sorted(threads):
                c, w = await count(dst, thread)
                target[0] += c
                target[1] += w
            report.checkpoints_target = (target[0], target[1])


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def checkpoint_conninfo(target_url: str, *, checkpoint_url: str | None = None, direct_url: str | None = None) -> str:
    """断点搬到哪儿（psycopg 的连接串）：给了断点库地址用它；否则进目标库——给了直连地址就走直连（复核 R-b12：目标地址是
    PgBouncer 的事务模式时，psycopg 的预备语句、建表时取的会话级锁都会出问题）。"""
    if checkpoint_url:
        return normalize_checkpoint_url(checkpoint_url)
    return libpq_conninfo(normalize_database_url(direct_url) if direct_url else normalize_database_url(target_url))


async def copy_to_postgres(source: str | Path, target_url: str, *, checkpoint_url: str | None = None,
                           direct_url: str | None = None, dry_run: bool = False, assume_stopped: bool = False,
                           batch: int = DEFAULT_BATCH, work_dir: str | Path | None = None,
                           progress: Callable[[str], None] = print) -> Report:
    """direct_url：目标库的直连地址（目标地址是 PgBouncer 时给；断点走它）。"""
    from app.db.base import create_schema, make_engine

    target_url = normalize_database_url(target_url)
    conninfo = checkpoint_conninfo(target_url, checkpoint_url=checkpoint_url, direct_url=direct_url)
    db, ckpt = resolve_source(source)
    if (wal := source_in_use(db, ckpt)) and not assume_stopped:
        raise CopyRefused(f"{wal} 还在：服务看起来还开着这个库。先停服务再导入；确认服务已停（上次是强杀，留下了 "
                          "-wal）就加 --assume-stopped")
    report = Report(dry_run=dry_run)
    target = make_engine(target_url, application_name=COPY_APPLICATION_NAME)
    try:
        if not assume_stopped and (on := await services_on(target)):
            raise CopyRefused(f"目标库上已经有 {on} 条服务的连接（application_name=agentlab）：先停掉连着它的服务再导入")
        progress(f"来源：{db}{f'，断点 {ckpt}' if ckpt else '（没有断点库）'}")
        progress(f"目标：PostgreSQL {describe(target_url)}{'（只核对，不写）' if dry_run else ''}")
        with tempfile.TemporaryDirectory(prefix="agentlab-copy-", dir=work_dir) as tmp:
            snap = snapshot(db, Path(tmp))
            upgrade_snapshot(snap)
            report.orphans = settle_orphans(snap, fixed=report.fixed)
            if not dry_run:
                await create_schema(target)
            await copy_tables(snap, target, batch=batch, dry_run=dry_run, report=report, progress=progress)
            if ckpt is not None:
                ckpt_snap = snapshot(ckpt, Path(tmp))
                await copy_checkpoints(ckpt_snap, conninfo, dry_run=dry_run, report=report, progress=progress)
    finally:
        await target.dispose()
    return report


def render(report: Report) -> list[str]:
    lines = ["", f"{'表':<28}{'来源':>10}{'导入前已有':>10}{'导入后已有':>10}{'目标总行数':>10}  结论"]
    for t in report.tables:
        lacking = t.source - t.present_after
        if not t.source:
            verdict = "空表"
        elif report.dry_run:
            verdict = f"要补 {lacking} 行" if lacking else "都已经有了"
        elif t.refused:
            verdict = f"少 {lacking} 行：撞了约束，没插进去（见下）"
        elif lacking:
            verdict = f"少 {lacking} 行"
        else:
            verdict = "一致" + (f"（这次补了 {t.present_after - t.present_before} 行）"
                               if t.present_after > t.present_before else "（都已经有了）")
        if t.orphans:
            verdict += f"；另有 {t.orphans} 行孤儿行没导入"
        lines.append(f"{t.name:<28}{t.source:>10}{t.present_before:>10}{t.present_after:>10}{t.target_after:>10}"
                     f"  {verdict}")
    if report.orphans:
        total = sum(report.orphans.values())
        lines.append(f"孤儿行（SQLite 当时没强制外键，外键指向的行已经不在）：{total} 行没导入，不算核对失败——"
                     + "、".join(f"{name} {n} 行" for name, n in report.orphans.items()))
    for t in report.tables:
        for item in t.refused[:20]:
            lines.append(f"  {t.name} 没插进去：{item}")
        if len(t.refused) > 20:
            lines.append(f"  {t.name} 另有 {len(t.refused) - 20} 行没插进去")
    c, w = report.checkpoints
    if report.dry_run:
        lines.append(f"断点：来源 {report.threads} 次运行、{c} 个断点、{w} 条 pending writes")
    else:
        tc, tw = report.checkpoints_target
        lines.append(f"断点：来源 {report.threads} 次运行、{c} 个断点、{w} 条 pending writes；目标上这些运行有 {tc} 个断点、"
                     f"{tw} 条 pending writes（这次新写 {report.checkpoints_written} 个断点）"
                     + ("" if tc >= c and tw >= w else "——对不上"))
    for what, n in report.fixed.items():
        lines.append(f"转换：{what} {n} 处")
    if report.dry_run:
        lines.append("只核对，什么都没写")
    else:
        lines.append("核对通过" if report.ok else "核对没过：见上面标出的表和断点")
    return lines


def main(argv: list[str] | None = None) -> int:
    from app.core.config import settings

    parser = argparse.ArgumentParser(prog="python -m app.db copy-to-postgres",
                                     description="把 SQLite 数据目录里的应用库和断点导入 PostgreSQL（先停服务）。")
    parser.add_argument("source", help="数据目录，或者 agentlab.db 的路径（断点库 checkpoints.db 在同一个目录里找）")
    parser.add_argument("target", nargs="?", help="目标地址 postgresql+asyncpg://…；不给就用 AGENTLAB_DATABASE_URL")
    parser.add_argument("--checkpoint-url", help="断点放到另一个 PostgreSQL 库；不给就用 AGENTLAB_CHECKPOINT_URL，再没有就进目标库")
    parser.add_argument("--direct-url", help="目标库的直连地址（目标地址是 PgBouncer 时给，断点走它）；不给、目标又是 "
                                             "AGENTLAB_DATABASE_URL 时用 AGENTLAB_DATABASE_DIRECT_URL")
    parser.add_argument("--dry-run", action="store_true", help="只核对：数来源和目标各有多少行，不建表、不写")
    parser.add_argument("--assume-stopped", action="store_true",
                        help="不查服务停没停（来源旁边留着强杀后的 -wal 时用）")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help=f"每批插入的行数，缺省 {DEFAULT_BATCH}")
    parser.add_argument("--work-dir", help="放快照的目录（要留出和两个库一样大的空间），缺省系统临时目录")
    args = parser.parse_args(argv)
    target = args.target or settings.database_url
    if not target:
        parser.error("没有目标地址：给第二个参数，或者设 AGENTLAB_DATABASE_URL")
    direct = args.direct_url or None
    if direct is None and settings.database_direct_url:
        try:
            same = normalize_database_url(target) == settings.database_url
        except ValueError:
            same = False
        direct = settings.database_direct_url if same else None
    try:
        report = asyncio.run(copy_to_postgres(
            args.source, target, checkpoint_url=args.checkpoint_url or settings.checkpoint_url or None, direct_url=direct,
            dry_run=args.dry_run, assume_stopped=args.assume_stopped, batch=max(args.batch, 1),
            work_dir=args.work_dir))
    except (CopyRefused, ValueError) as e:
        print(f"没有导入：{e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - 连不上、库报错：照实说一句，不甩一屏调用栈
        from app.core.errors import first_line

        what = "核对" if args.dry_run else "导入"
        tail = "什么都没写" if args.dry_run else "已经写进目标库的行留着，处理好之后重新运行只补缺的"
        print(f"没有{what}完：{type(e).__name__}：{first_line(e) or '（没有说明）'}。{tail}", file=sys.stderr)
        return 1
    for line in render(report):
        print(line)
    return 0 if report.ok else 1

