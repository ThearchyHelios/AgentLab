"""数据源的增删改查、连接测试、结构探查。

密码的处理规则和 Provider 完全一致（app/api/settings.py）：入库加密、
出站只给掩码、PATCH 时不传表示不动、传空串表示清空。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt, mask
from app.data import introspect as introspect_mod
from app.data.engine import SUPPORTED_KINDS, engines, test_connection
from app.db.base import get_session
from app.db.models import DataSource
from app.tools.datasource import tool_names

router = APIRouter(prefix="/api/datasources", tags=["datasources"])

# 名字会成为工具名的一部分（db_query__<name>），而模型的 function name 只认
# ASCII。所以这里收紧成 slug，中文说明放 description 字段。
_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,40}$"


class DataSourceIn(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN,
                      description="英文标识，会成为工具名的一部分，如 sales")
    kind: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    readonly: bool = True
    description: str = ""
    enabled: bool = True


class DataSourcePatch(BaseModel):
    kind: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None  # 空串=清空；不传=不动
    options: dict[str, Any] | None = None
    readonly: bool | None = None
    description: str | None = None
    enabled: bool | None = None


class DataSourceOut(BaseModel):
    id: str
    name: str
    kind: str
    host: str | None
    port: int | None
    database: str | None
    username: str | None
    options: dict[str, Any]
    readonly: bool
    description: str
    enabled: bool
    password_masked: str = ""
    has_password: bool = False
    table_count: int = 0
    schema_synced_at: str | None = None
    tools: list[str] = Field(default_factory=list)


def _to_out(row: DataSource) -> DataSourceOut:
    tables = (row.schema_cache or {}).get("tables") or {}
    return DataSourceOut(
        id=row.id, name=row.name, kind=row.kind, host=row.host, port=row.port,
        database=row.database, username=row.username, options=row.options or {},
        readonly=row.readonly, description=row.description, enabled=row.enabled,
        password_masked=mask(row.password),
        has_password=bool(row.password),
        table_count=len(tables),
        schema_synced_at=row.schema_synced_at.isoformat() if row.schema_synced_at else None,
        tools=tool_names(row),
    )


async def _get_or_404(session: AsyncSession, source_id: str) -> DataSource:
    row = await session.get(DataSource, source_id)
    if not row:
        raise HTTPException(404, "数据源不存在")
    return row


@router.get("", response_model=list[DataSourceOut])
async def list_sources(session: AsyncSession = Depends(get_session)) -> list[DataSourceOut]:
    rows = (await session.execute(select(DataSource).order_by(DataSource.name))).scalars()
    return [_to_out(r) for r in rows]


@router.get("/kinds")
async def list_kinds() -> dict[str, Any]:
    """支持哪些数据库，以及各自需要填什么——前端表单据此渲染。"""
    return {
        "kinds": [
            {"value": "mysql", "label": "MySQL / MariaDB", "default_port": 3306,
             "needs": ["host", "database", "username", "password"],
             "hint": "options 可填 charset（默认 utf8mb4）"},
            {"value": "postgres", "label": "PostgreSQL", "default_port": 5432,
             "needs": ["host", "database", "username", "password"], "hint": ""},
            {"value": "oracle", "label": "Oracle", "default_port": 1521,
             "needs": ["host", "username", "password"],
             "hint": "库名填 service_name，或在 options 里给 service_name / sid。"
                     "驱动走 thin 模式，不需要装 Instant Client。"
                     "只读账号名下通常没有对象——数据在别的 schema 里，"
                     "用 options.schema 指定（如 ANALYTICS），否则探查结果是空的"},
            {"value": "sqlite", "label": "SQLite（文件）", "default_port": None,
             "needs": ["database"], "hint": "库名填数据库文件的绝对路径"},
        ],
        "supported": list(SUPPORTED_KINDS),
    }


@router.post("", response_model=DataSourceOut, status_code=201)
async def create_source(
    payload: DataSourceIn, session: AsyncSession = Depends(get_session)
) -> DataSourceOut:
    if payload.kind not in SUPPORTED_KINDS:
        raise HTTPException(400, f"不支持的类型 {payload.kind}，支持：{', '.join(SUPPORTED_KINDS)}")
    exists = (await session.execute(
        select(DataSource).where(DataSource.name == payload.name)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"已经有叫 {payload.name} 的数据源了")

    row = DataSource(
        **payload.model_dump(exclude={"password"}),
        password=encrypt(payload.password),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.patch("/{source_id}", response_model=DataSourceOut)
async def update_source(
    source_id: str, payload: DataSourcePatch, session: AsyncSession = Depends(get_session)
) -> DataSourceOut:
    row = await _get_or_404(session, source_id)
    data = payload.model_dump(exclude_unset=True)
    if "password" in data:
        # 空串表示清空，非空表示换新的
        data["password"] = encrypt(data["password"]) if data["password"] else None
    for key, value in data.items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    # 连接参数可能变了，旧连接池不能再用——否则改完密码还在用旧连接，很难排查
    await engines.invalidate(source_id)
    return _to_out(row)


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await _get_or_404(session, source_id)
    await session.delete(row)
    await session.commit()
    await engines.invalidate(source_id)


@router.post("/{source_id}/test")
async def test_source(source_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """测连接。失败时把驱动的原始报错带出来——这类问题九成靠错误信息定位。"""
    row = await _get_or_404(session, source_id)
    return await test_connection(row)


@router.post("/{source_id}/introspect", response_model=DataSourceOut)
async def introspect_source(
    source_id: str, schema: str | None = None, session: AsyncSession = Depends(get_session)
) -> DataSourceOut:
    """探查结构并缓存。显式动作，不做后台轮询——生产库不该被实验工具定时扫。"""
    row = await _get_or_404(session, source_id)
    try:
        row.schema_cache = await introspect_mod.introspect(row, schema=schema)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"探查失败：{type(e).__name__}: {e}") from e
    row.schema_synced_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.get("/{source_id}/schema")
async def get_schema(source_id: str, table: str | None = None,
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await _get_or_404(session, source_id)
    if table:
        return {"table": table, "detail": introspect_mod.describe_table(row, table)}
    return {
        "tables": introspect_mod.table_names(row),
        "summary": introspect_mod.summary(row),
        "synced_at": row.schema_synced_at.isoformat() if row.schema_synced_at else None,
    }
