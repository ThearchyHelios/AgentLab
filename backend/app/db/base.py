from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import JSON, DateTime, MetaData, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import settings

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


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # 图定义、节点配置、事件负载都是任意形状的 JSON，统一映射到 SQLite 的 JSON 列
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


engine = create_async_engine(settings.db_url, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - 驱动回调
    """WAL + 外键约束。WAL 让读写不互相阻塞，事件写入和前端轮询能并行。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用的会话。"""
    async with SessionLocal() as session:
        yield session


# 轻量迁移：create_all 只建新表，不给已有表加列。SQLite 的 ADD COLUMN
# 便宜且带默认值回填，够用；等真需要改列类型再考虑 alembic。
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("runs", "run_class", "TEXT DEFAULT 'exploratory'"),
    ("runs", "version", "INTEGER"),
    ("runs", "version_hash", "VARCHAR(64)"),
    ("runs", "manifest_hash", "VARCHAR(64)"),
    ("runs", "started_by", "VARCHAR(100)"),
    ("workflows", "status", "TEXT DEFAULT 'draft'"),
    ("workflows", "published_version", "INTEGER"),
    ("workflows", "published_by", "VARCHAR(100)"),
    ("workflow_versions", "graph_hash", "VARCHAR(64)"),
    ("approvals", "resolved_by", "VARCHAR(100)"),
    # 存量向量没有这两列，回填成空串 / 0 —— 正好表示"不知道是谁建的"，
    # 检索侧会把它们当成对不上当前 embedder 处理
    ("chunks", "embed_model", "VARCHAR(100) DEFAULT ''"),
    ("chunks", "embed_dim", "INTEGER DEFAULT 0"),
    ("memories", "embed_model", "VARCHAR(100) DEFAULT ''"),
    ("memories", "embed_dim", "INTEGER DEFAULT 0"),
    ("chunks", "token_len", "INTEGER DEFAULT 0"),
]


def _migrate(conn) -> None:  # pragma: no cover - 同步回调
    from sqlalchemy import text

    for table, column, ddl in _COLUMN_MIGRATIONS:
        cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        if cols and column not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def init_db() -> None:
    from app.db import models  # noqa: F401  确保模型已注册到 metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)
