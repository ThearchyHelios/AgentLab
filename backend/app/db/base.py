from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import JSON, BigInteger, Column, DateTime, Integer, MetaData, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

logger = logging.getLogger(__name__)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class UTCDateTime(TypeDecorator):
    """存 UTC、读出来也是带时区的 UTC。

    SQLite 没有时区类型：DateTime(timezone=True) 写进去的只是一串不带偏移的
    字面时间，读回来是 naive 的。API 于是吐出 '2026-09-26T01:11:19'，浏览器把
    不带偏移的 ISO 串当本地时间解析，运行列表整体慢了 8 小时。在这一层补上 UTC，
    pydantic 序列化自然带 +00:00，所有接口一次修好。

    写入前先换算成 UTC 再去掉时区：SQLite 方言格式化时直接丢弃 tzinfo，
    一个 +08:00 的时间原样写进去，读回来就差出 8 小时。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


#: 应用库的 JSON 列：两种库上都是存原文的 JSON（PostgreSQL 的 json，不是 JSONB）。JSONB 会把对象里的键重新排序：
#: 应用里不少地方按存进去的键的先后出东西（快照的列、表的先后、复核结论各段的先后），换成 JSONB 读回来就变了样，
#: 测试当场抓到好几处。json 的代价是没有相等运算符：== 比较、DISTINCT、GROUP BY、ORDER BY 不能直接作用在整列上
#: （应用里没有这种写法，test_db_portable 守着）；按里面某个键筛选走 db/portable 的 json_eq 等，只把取出来的那一小段
#: 转成 JSONB 比较
PortableJSON = JSON()


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # 图定义、节点配置、事件负载都是任意形状的 JSON，统一映射到 JSON 列（见 PortableJSON）
    type_annotation_map = {dict[str, Any]: PortableJSON, list[Any]: PortableJSON, datetime: UTCDateTime}


@event.listens_for(Column, "after_parent_attach")
def _portable_integer(column: Column, table: Any) -> None:
    """应用库的 Integer 列在 PostgreSQL 上换成 BIGINT（SQLite 照旧是 INTEGER，主键照旧是 rowid）。

    SQLite 的 INTEGER 是 64 位，PostgreSQL 的只有 32 位：行数估计、字节数、累计的计数都可能过 21 亿。在列挂到表上
    的那一刻换（定义模型时，任何查询之前）：建表是 BIGINT（自增主键是 BIGSERIAL），和这一列比较、写进这一列的绑定
    参数也按 BIGINT 转换。只管挂在应用 metadata 上、类型正好是 Integer 的列；业务数据源那边的表不受影响。"""
    if getattr(table, "metadata", None) is Base.metadata and type(column.type) is Integer \
            and not column.type._variant_mapping:
        column.type = Integer().with_variant(BigInteger(), "postgresql")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


def _stop_aiosqlite_once() -> None:
    """aiosqlite 连接的工作线程只停一次；之后再要求停的，拿到的是同一个结果。

    aiosqlite 0.22 的 stop() 每调一次都往工作线程队列里放一个"关闭并退出线程"
    的请求，而线程处理完第一个就退出了——之后放进去的请求没人处理，等它的一方
    永远挂着。close() 的 finally 里会调 stop()，SQLAlchemy 的强制 terminate 也会
    直接调；两个 close 并发、或者 close 撞上强制 terminate，就会卡死。实测同一
    连接并发 close 两次，10 次卡 10 次。

    在这里触发它的路径：并行的两条支路一条失败，LangGraph 取消另一条，而那一条
    正在写工件。取消打进进行中的数据库操作，SQLAlchemy 在两条路径上各 terminate
    一次同一条连接——节点任务取消不完，整个运行卡在 running，占着的并发名额也不
    还。大约五次里有一次。连接池和 checkpointer 都走 aiosqlite，补在这一层都管得到。
    """
    import weakref

    import aiosqlite

    original = aiosqlite.Connection.stop
    if getattr(original, "_agentlab_once", False):
        return
    stopping: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def stop(self):  # type: ignore[no-untyped-def]
        if self not in stopping:
            stopping[self] = original(self)
        return stopping[self]

    stop._agentlab_once = True  # type: ignore[attr-defined]
    aiosqlite.Connection.stop = stop  # type: ignore[method-assign]


# 应用库是 PostgreSQL 时也照样打：上传表格、手工登记的 SQLite 业务库（data/engine）还是走 aiosqlite 的连接池
_stop_aiosqlite_once()


def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - 驱动回调
    """WAL + 外键约束。WAL 让读写不互相阻塞，事件写入和前端轮询能并行。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _no_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    return value


def pg_json_dumps(value: Any) -> str:
    """PostgreSQL 上写 JSON 列。它不收 NaN / Infinity（SQLite 照存）；字符串里的 \\u0000 json 列收，但一转成 JSONB
    （按键筛选时，db/portable）、一取成文本就报错，那一整条查询跟着失败。

    非有限的浮点写成 null（同浏览器的 JSON.stringify），字符串里的 NUL 字符去掉；平常的值一次 dumps 就过，
    只有碰上了才多走一遍。"""
    try:
        text = json.dumps(value, allow_nan=False)
    except ValueError:
        text = json.dumps(_finite(value), allow_nan=False)
    if "\\u0000" in text:
        text = json.dumps(_no_nul(json.loads(text)), allow_nan=False)
    return text


#: 应用连 PostgreSQL 时报的 application_name。copy-to-postgres 按它认目标库上还有没有服务连着（它自己报的是 agentlab-copy）
APPLICATION_NAME = "agentlab"


def make_engine(url: str | None = None, *, application_name: str = APPLICATION_NAME) -> AsyncEngine:
    """应用库的引擎。SQLite：WAL、外键、忙等，单个文件；PostgreSQL：连接池、方言调整（db/postgres.py）。

    copy-to-postgres 建目标库的引擎也走这里，两边的列类型、排序规则是同一套。
    """
    url = url or settings.db_url
    if url.startswith("sqlite"):
        # pool_pre_ping：取连接前先探一下活。取消异常打进 SQLAlchemy 的清理流程时
        # （_close_connection 记完日志会把 CancelledError 原样抛出，跳过了把连接记录
        # 清空的那一行），已经关掉的连接会留在池里，下一个取到它的人拿到的是
        # "no active connection"。探活失败会被当成断开，换一条新的——SQLite 本地一次
        # SELECT 1 的代价，换池子不被污染。
        made = create_async_engine(url, echo=False, future=True, pool_pre_ping=True)
        event.listen(made.sync_engine, "connect", _set_sqlite_pragma)
        return made
    from app.db import postgres

    pool: dict[str, Any] = (
        {"poolclass": NullPool} if settings.db_pool_size <= 0 else {
            "pool_size": settings.db_pool_size, "max_overflow": max(settings.db_max_overflow, 0),
            "pool_timeout": settings.db_pool_timeout, "pool_recycle": settings.db_pool_recycle})
    made = create_async_engine(
        url, echo=False, future=True, pool_pre_ping=True, json_serializer=pg_json_dumps,
        # 会话时区定成 UTC：时间列是不带时区的 UTC（UTCDateTime），库里 now() 之类的也按 UTC 算
        connect_args={"server_settings": {"application_name": application_name, "timezone": "UTC"}},
        **pool)
    postgres.install(made.sync_engine.dialect)
    return made


def describe_database() -> str:
    """启动日志里的一句：用的是哪种库、在哪（不写密码）。"""
    from app.db.urls import describe

    if settings.checkpoint_url:
        points = f"断点在 PostgreSQL {describe(settings.checkpoint_url)}"
    elif settings.db_dialect == "sqlite":
        points = f"断点 {settings.checkpoint_path}"
    else:
        points = "断点在同一个库"
    if settings.db_dialect == "sqlite":
        return f"SQLite（{settings.db_path}，{points}）"
    return f"PostgreSQL（{describe(settings.db_url)}，{points}）"


engine = make_engine()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用的会话。"""
    async with SessionLocal() as session:
        yield session


# 轻量迁移：create_all 只建新表，不给已有表加列。ADD COLUMN 便宜且带默认值回填，够用；等真需要改列类型再考虑 alembic。
# 第三项照 SQLite 写（类型和缺省值的字面量）。PostgreSQL 上类型取模型里这一列的（按应用库的方言调整编译：String(n)
# 是 VARCHAR、Integer 是 BIGINT、JSON 是 json、时间是 TIMESTAMP），缺省值取这里写的，布尔的 0 / 1 换成 false / true
# （_column_ddl）。所以这里加的每一列模型里都得有：test_db_portable 守着
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("runs", "run_class", "TEXT DEFAULT 'exploratory'"),
    ("runs", "version", "INTEGER"),
    ("runs", "version_hash", "VARCHAR(64)"),
    ("runs", "manifest_hash", "VARCHAR(64)"),
    ("runs", "manifest_seq", "INTEGER"),
    ("runs", "started_by", "VARCHAR(100)"),
    # 续跑、恢复要沿用第一次发起时的记忆域、知识库和审批默认值；以前只在第一段
    # _drive 里传过一次，恢复后回落成 "default"，同一次运行前后两段查的不是同一个库
    ("runs", "memory_scope", "VARCHAR(100)"),
    ("runs", "collection", "VARCHAR(100)"),
    ("runs", "approval_default", "VARCHAR(20)"),
    ("runs", "tool_trust", "JSON"),
    ("runs", "agent_limits", "JSON"),
    ("runs", "error_node_id", "VARCHAR(64)"),
    ("workflows", "status", "TEXT DEFAULT 'draft'"),
    ("workflows", "published_version", "INTEGER"),
    ("workflows", "published_by", "VARCHAR(100)"),
    ("workflow_versions", "graph_hash", "VARCHAR(64)"),
    # 存量版本回填成 NULL：不知道当时按哪一档发布的，正式运行照旧看工作流的 status
    ("workflow_versions", "level", "VARCHAR(20)"),
    # 发布时的数据目录版本（阶段 4A）。存量版本回填成 NULL：不知道当时按哪一版目录发布的，正式运行不提醒
    ("workflow_versions", "catalog_versions", "JSON"),
    ("approvals", "resolved_by", "VARCHAR(100)"),
    # 存量向量没有这两列，回填成空串 / 0 —— 正好表示"不知道是谁建的"，
    # 检索侧会把它们当成对不上当前 embedder 处理
    ("chunks", "embed_model", "VARCHAR(100) DEFAULT ''"),
    ("chunks", "embed_dim", "INTEGER DEFAULT 0"),
    ("memories", "embed_model", "VARCHAR(100) DEFAULT ''"),
    ("memories", "embed_dim", "INTEGER DEFAULT 0"),
    ("chunks", "token_len", "INTEGER DEFAULT 0"),
    ("documents", "status", "TEXT DEFAULT 'ready'"),
    ("documents", "error", "TEXT DEFAULT ''"),
    # 存量轮次没有复核结论。NULL 正好表示"这一轮没复核过"，和"复核过、没问题"
    # （存 verdict=ok）是两回事，前端要分得开
    ("conversation_turns", "review", "JSON"),
    ("conversation_turns", "meta", "JSON"),
    ("data_sources", "last_check", "JSON"),
    ("mcp_servers", "last_check", "JSON"),
    # 存量的源一律先记成 manual；老的上传源由 table_versions.startup 按路径认出来，
    # 补上 v0 版本后再改成 upload
    ("data_sources", "origin", "TEXT DEFAULT 'manual'"),
    ("data_sources", "current_snapshot_id", "VARCHAR(64)"),
    # 存量运行没有钉过版本，NULL 正好表示「升级前发起的」
    ("runs", "data_versions", "JSON"),
    # 按配方导入（期 2）。新表 table_recipes、import_stagings 由 create_all 建；这些是已有表的新列，
    # 存量行一律 NULL：存量的源都是简单导入或手工源，存量的导入记录都不是按配方导入的
    ("data_sources", "current_recipe_id", "VARCHAR(32)"),
    ("table_imports", "recipe_id", "VARCHAR(32)"),
    ("table_imports", "period_start", "VARCHAR(10)"),
    ("table_imports", "period_end", "VARCHAR(10)"),
    ("table_imports", "context", "JSON"),
    ("table_imports", "checks", "JSON"),
    ("table_imports", "overrides", "JSON"),
    ("table_imports", "waivers", "JSON"),
    ("table_imports", "confirmations", "JSON"),
    ("table_imports", "manifest_artifact", "VARCHAR(64)"),
    ("table_imports", "signed_by", "VARCHAR(100)"),
    ("table_imports", "staging_id", "VARCHAR(32)"),
    # 期 3：按期累积、版本页（P3-SPEC 9.6）。新表 snapshot_activations 由 create_all 建；存量行一律 NULL，
    # 含义是「期 3 之前的」：快照的 mode 按 replace、recipe_id 取最后一期导入的、activated_at 按 created_at
    ("source_snapshots", "mode", "VARCHAR(20)"),
    ("source_snapshots", "recipe_id", "VARCHAR(32)"),
    ("source_snapshots", "activated_at", "DATETIME"),
    ("source_snapshots", "manifest_artifact", "VARCHAR(64)"),
    ("table_imports", "revoked", "JSON"),
    ("import_stagings", "edits", "JSON"),
    ("import_stagings", "answers_dropped", "JSON"),
    ("import_stagings", "redraft_sha256", "VARCHAR(64)"),
    # 运行选项（发起时的快照，整次运行沿用）和发起它的定时任务。存量运行回填 {} 和 NULL：
    # 没有选项，也不是定时任务发起的
    ("runs", "options", "JSON DEFAULT '{}'"),
    ("runs", "schedule_id", "VARCHAR(32)"),
    # 数据目录起草任务（验收后修正）：超时、没写出来的调用次数，当前的列数上限，提醒；逐表的模型留空的列。
    # 存量任务回填 0 和 NULL：升级前没记这些
    ("catalog_draft_jobs", "timeouts", "INTEGER DEFAULT 0"),
    ("catalog_draft_jobs", "failed_calls", "INTEGER DEFAULT 0"),
    ("catalog_draft_jobs", "part_columns", "INTEGER"),
    ("catalog_draft_jobs", "notice", "TEXT"),
    ("catalog_draft_tasks", "blank_columns", "INTEGER DEFAULT 0"),
    ("catalog_draft_tasks", "blank_sample", "JSON"),
    # 数据目录闭环第一步：宽表逐段落盘的段数、块进度。存量任务回填 0 和 NULL：升级前没记这些
    ("catalog_draft_tasks", "parts_written", "INTEGER DEFAULT 0"),
    ("catalog_draft_tasks", "chunks_total", "INTEGER"),
    ("catalog_draft_tasks", "chunks_done", "INTEGER"),
    # 复核后：逐表的日志（模型提议的有效记录条件没有收下的原因）。存量任务回填 NULL，接口给空列表
    ("catalog_draft_tasks", "log", "JSON"),
    # 应用平台（docs/design/app-platform-and-look.md §3.1.2）。新表由 create_all 建；这些是已有表的新列，存量一律 NULL：
    # 运行的来源未记录、版本的发布人和退役没记过、MCP 没有认证头、对话不属于任何应用
    ("runs", "origin_kind", "VARCHAR(20)"),
    ("runs", "origin", "JSON"),
    ("runs", "app_id", "VARCHAR(32)"),
    ("runs", "app_version", "INTEGER"),
    ("workflow_versions", "published_at", "DATETIME"),
    ("workflow_versions", "published_by", "VARCHAR(100)"),
    ("workflow_versions", "retired_at", "DATETIME"),
    ("workflow_versions", "retired_by", "VARCHAR(100)"),
    ("workflow_versions", "retire_note", "TEXT"),
    ("workflow_versions", "capability_versions", "JSON"),
    ("workflows", "origin", "JSON"),
    ("mcp_servers", "headers", "JSON DEFAULT '{}'"),
    ("mcp_servers", "endpoint_id", "VARCHAR(32)"),
    ("conversations", "app_id", "VARCHAR(32)"),
    ("conversations", "created_by", "VARCHAR(100)"),
    # 普通用户建应用（docs/design/normal-user-apps.md §3.1）：个人应用（scope=personal、owner）和出自哪个蓝图
    ("apps", "scope", "VARCHAR(20) DEFAULT 'shared'"),
    ("apps", "owner", "VARCHAR(100)"),
    ("apps", "blueprint_id", "VARCHAR(32)"),
    # 下一轮（docs/design/next-round/00-总方案.md X4、X9）：运行为什么结束在 cancelled；定时的目标种类和 id；
    # 定时触发建的那次评测。存量行：end_reason 为空，target_kind 是 workflow（target_id 由 scheduler 按 workflow_id 读）
    ("runs", "end_reason", "VARCHAR(20)"),
    ("schedules", "target_kind", "VARCHAR(20) DEFAULT 'workflow'"),
    ("schedules", "target_id", "VARCHAR(32)"),
    ("schedule_fires", "eval_run_id", "VARCHAR(32)"),
    # 编排完善（docs/design/orchestration-round/00-总方案.md §4）：定时任务的触发方式。存量行都是 cron
    ("schedules", "trigger_kind", "VARCHAR(20) DEFAULT 'cron'"),
    ("schedules", "trigger", "JSON"),
    # 第 2 波：服务账号（users.kind）；剖析上传表格源时的快照
    ("users", "kind", "VARCHAR(20) DEFAULT 'person'"),
    ("catalog_profile_tasks", "snapshot_id", "VARCHAR(32)"),
    # 第 3 波：发布时这一版引用的表的结构版本
    ("workflow_versions", "schema_versions", "JSON"),
]

#: 迁移补上的列要建的索引：(索引名, 表, 列或多列)。create_all 不碰已有的表，表上新列的索引也就不会建。
#: 索引名照 NAMING_CONVENTION 取（多列的照模型 __table_args__ 里写的名字），和新库 create_all 建出来的同名，
#: IF NOT EXISTS 才不会重复建
_INDEX_MIGRATIONS: list[tuple[str, str, str | tuple[str, ...]]] = [
    # 定时任务的触发历史按它找这个任务发起的运行
    ("ix_runs_schedule_id", "runs", "schedule_id"),
    # 应用平台：按来源、按应用筛运行；按工作流、版本、状态找钉住版本最近一次封存的运行
    ("ix_runs_origin_kind", "runs", "origin_kind"),
    ("ix_runs_app_id", "runs", "app_id"),
    ("ix_runs_wf_ver_status", "runs", ("workflow_id", "version", "status")),
    ("ix_conversations_app_id", "conversations", "app_id"),
    ("ix_conversations_created_by", "conversations", "created_by"),
    # 口径账本按种类反查工件（kind=caliber_match，next-round 02 做法 2）
    ("ix_artifacts_kind", "artifacts", "kind"),
]


#: 迁移补建的唯一索引：(索引名, 表, 列, 部分索引的条件, 建之前的去重语句)。只在索引还不存在时先去重再建——存量库里可能
#: 已经有并发留下的重复行，直接建会失败。去重的规矩写在各自的语句里：多出的钩子行删掉（只留最早的那一行，它的密钥照旧
#: 能用），多出的助手对话归档（不删，历史还在）。条件和语句里的 {false} / {true} 按方言换成布尔的字面量（SQLite 是 0 / 1）
_UNIQUE_INDEX_MIGRATIONS: list[tuple[str, str, tuple[str, ...], str | None, str]] = [
    ("ux_app_hooks_app_name", "app_hooks", ("app_id", "name"), None,
     "DELETE FROM app_hooks WHERE id NOT IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
     "(PARTITION BY app_id, name ORDER BY created_at, id) AS rn FROM app_hooks) AS ranked WHERE rn = 1)"),
    ("ux_conversations_app_owner", "conversations", ("app_id", "created_by"), "kind = 'app' AND archived = {false}",
     "UPDATE conversations SET archived = {true} WHERE kind = 'app' AND archived = {false} AND created_by IS NOT NULL "
     "AND id NOT IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY app_id, created_by "
     "ORDER BY last_active_at DESC, id) AS rn FROM conversations WHERE kind = 'app' AND archived = {false} "
     "AND created_by IS NOT NULL) AS ranked WHERE rn = 1)"),
]

#: 布尔字面量：SQLite 的布尔列存 0 / 1
_BOOL_LITERALS = {"sqlite": {"false": "0", "true": "1"}, "postgresql": {"false": "false", "true": "true"}}

_DEFAULT = re.compile(r"\s+DEFAULT\s+(.+)$", re.I | re.S)


def _column_ddl(dialect: Any, table: str, column: str, ddl: str) -> str:
    """ADD COLUMN 里列名后面那一段。SQLite 原样用迁移表里写的；PostgreSQL 类型取模型里这一列的，缺省值取迁移表里的。"""
    if dialect.name == "sqlite":
        return ddl
    from sqlalchemy import Boolean

    from app.db.postgres import AppTypeCompiler

    col = Base.metadata.tables[table].c[column]
    out = AppTypeCompiler(dialect).process(col.type, type_expression=col)
    if match := _DEFAULT.search(ddl):
        default = match.group(1).strip()
        if isinstance(col.type, Boolean):
            default = {"0": "false", "1": "true"}.get(default, default)
        out += f" DEFAULT {default}"
    return out


def _migrate(conn) -> None:  # pragma: no cover - 同步回调
    """给老库补列、补索引。已有的列和索引按 inspector 认（两种库同一套），再跑一遍什么都不做。"""
    from sqlalchemy import inspect, text

    dialect = conn.dialect
    literals = _BOOL_LITERALS.get(dialect.name, _BOOL_LITERALS["postgresql"])
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    columns = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
    # IF NOT EXISTS：PostgreSQL 上多个进程同时启动，各自的检查和补列之间别人可能已经补上了（init_db 另外拿着锁）
    add = "ADD COLUMN IF NOT EXISTS" if dialect.name == "postgresql" else "ADD COLUMN"
    for table, column, ddl in _COLUMN_MIGRATIONS:
        cols = columns.get(table)
        if cols and column not in cols:
            conn.execute(text(f"ALTER TABLE {table} {add} {column} {_column_ddl(dialect, table, column, ddl)}"))
            cols.add(column)
    for name, table, wanted in _INDEX_MIGRATIONS:
        wanted = (wanted,) if isinstance(wanted, str) else wanted
        cols = columns.get(table) or set()
        if all(c in cols for c in wanted):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(wanted)})"))
    for name, table, wanted, where, dedupe in _UNIQUE_INDEX_MIGRATIONS:
        cols = columns.get(table) or set()
        if not all(c in cols for c in wanted):
            continue
        if any(ix["name"] == name for ix in inspect(conn).get_indexes(table)):
            continue
        conn.execute(text(dedupe.format(**literals)))
        conn.execute(text(f"CREATE UNIQUE INDEX {name} ON {table} ({', '.join(wanted)})"
                          + (f" WHERE {where.format(**literals)}" if where else "")))


#: init_db 在 PostgreSQL 上拿的会话级咨询锁的号：几个进程同时启动时，建表和迁移一个一个来（同一个号别处别用）
_INIT_LOCK = 0x4167656E744C6162  # "AgentLab"


async def create_schema(target: AsyncEngine) -> None:
    """建表、补列补索引（create_all 只建新表，老库的新列靠 _migrate）。两种库同一套；copy-to-postgres 建目标库也用它。"""
    from app.db import models  # noqa: F401  确保模型已注册到 metadata

    async with target.begin() as conn:
        if conn.dialect.name == "postgresql":
            # 事务结束自动放锁。CREATE TABLE IF NOT EXISTS 在 PostgreSQL 上并发执行也会撞（系统表的唯一约束）
            from sqlalchemy import text

            await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _INIT_LOCK})
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


async def init_db() -> None:
    logger.info("应用库：%s", describe_database())
    await create_schema(engine)
