"""记录表的查询视图（docs/design/normal-user-apps.md §3.7「查询连接」）。

origin=records 的数据源查询时换成一个 RecordsView（字段和 DataSource 同名，同 table_versions.SourceView 的做法）：
带着这次查询的高水位、看哪些行，以及连接上要先执行的两条语句（建同名临时视图）。data/engine.run_query 认这两样：
- prelude：借到连接后、执行查询前依次执行（DROP / CREATE TEMP VIEW records）；
- forbid_schemas：SQL 里直接写 main.<表> 的拒掉——不然绕过临时视图就看到了底表（全部行、被更正的行、幂等键）。

高水位来自运行的 data_versions（发起时记下，engine/runner._data_versions），不在运行里（工具库试用、数据剖析）的查询
用现在的数据、看全部（这些入口只有搭建者以上进得来）。
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.records import RECORDS_ORIGIN, store

#: data_versions 里记录表那一项的形状：{"name", "origin": "records", "table", "version", "seq", "rows", "viewer"?}
PIN_KEYS = ("name", "origin", "table", "version", "seq", "rows", "viewer")


@dataclass
class RecordsView:
    """绑定了高水位和行级过滤的记录表数据源。engine / introspect / guard / 工具只读这些字段，和 DataSource 同名。"""

    id: str
    name: str
    kind: str = "sqlite"
    database: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    readonly: bool = True
    description: str = ""
    schema_cache: dict[str, Any] = field(default_factory=dict)
    schema_synced_at: datetime | None = None
    enabled: bool = True
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    last_check: dict[str, Any] | None = None
    origin: str = RECORDS_ORIGIN
    current_snapshot_id: str | None = None
    #: 记录表不分快照：引擎缓存按源 id 一个，临时视图每次查询现建
    snapshot_id: str | None = None
    #: 文件还在追加，不能以 immutable 打开
    immutable: bool = False
    expected_sha256: str | None = None
    #: 这次查询的高水位（None：现在有的都算）
    high_water: int | None = None
    #: all / mine / none（records/access.py）
    rows: str = "all"
    viewer: str | None = None
    #: 连接上先执行的语句（建同名临时视图）
    prelude: tuple[str, ...] = ()
    #: SQL 里不许直接写的库名
    forbid_schemas: tuple[str, ...] = ("main",)


def bind_view(source: Any, columns: list[store.Column], *, high_water: int | None, rows: str = "all",
              viewer: str | None = None) -> RecordsView:
    """数据源行 + 高水位 + 行级过滤 → RecordsView。"""
    rows = rows if rows in ("all", "mine", "none") else "none"
    return RecordsView(
        id=source.id, name=source.name, database=source.database, options=dict(source.options or {}),
        description=source.description or "", schema_cache=copy.deepcopy(source.schema_cache or {}),
        schema_synced_at=getattr(source, "schema_synced_at", None), enabled=bool(source.enabled),
        last_check=getattr(source, "last_check", None), high_water=high_water, rows=rows, viewer=viewer,
        prelude=store.view_prelude(columns, high_water=high_water, scope=rows, viewer=viewer))


def columns_of_source(source: Any) -> list[store.Column]:
    """数据源上登记的表结构里的业务列（不含系统列）。records_tables 的列定义是权威；这里只在拿不到登记时兜底。"""
    from app.records import SYSTEM_COLUMNS

    meta = ((getattr(source, "schema_cache", None) or {}).get("tables") or {}).get(store.TABLE) or {}
    return [store.Column(name=str(c.get("name")), label=str(c.get("comment") or c.get("name")),
                         type=_type_of(str(c.get("type") or "")))
            for c in meta.get("columns") or [] if isinstance(c, dict) and c.get("name") not in SYSTEM_COLUMNS]


def _type_of(sql_type: str) -> str:
    return {"REAL": "number", "INTEGER": "integer"}.get(sql_type.upper(), "string")


async def resolve(session: AsyncSession, source: Any, entry: Any = None) -> RecordsView:
    """查询时实际该用的视图。entry 是这次运行 data_versions 里这张表的那一项；None 是不在运行里（现在的数据、看全部）。"""
    from app.records.tables import columns_of, table_of_source

    table = await table_of_source(session, source.id)
    columns = columns_of(table) if table is not None else columns_of_source(source)
    if isinstance(entry, dict):
        seq = entry.get("seq")
        return bind_view(source, columns, high_water=int(seq) if isinstance(seq, int) else 0,
                         rows=str(entry.get("rows") or "none"), viewer=entry.get("viewer"))
    return bind_view(source, columns, high_water=None, rows="all")


def default_view(source: Any) -> RecordsView:
    """没经解析就拿来查的记录表数据源（数据剖析这类只有搭建者进得来的入口）：现在的数据、看全部，照样走临时视图——
    底表里的幂等键、被更正的行不露出来。列取登记在数据源上的表结构。"""
    return bind_view(source, columns_of_source(source), high_water=None, rows="all")


async def pin_entry(session: AsyncSession, source: Any, *, run_class: str | None = None,
                    started_by: str | None = None) -> dict[str, Any] | None:
    """发起运行时记进 data_versions 的那一项：高水位，以及（给了运行类别时）这次运行看哪些行。登记不在、文件不在的
    返回 None（这个源不固定，查询时照实报错）。"""
    from app.records.access import scope_of_run
    from app.records.tables import path_of, table_of_source

    table = await table_of_source(session, source.id)
    if table is None:
        return None
    try:
        seq = await asyncio.to_thread(store.high_water, path_of(table))
    except store.RecordsRefused:
        return None
    entry: dict[str, Any] = {"name": source.name, "origin": RECORDS_ORIGIN, "table": table.table_key,
                             "version": table.version, "seq": seq}
    if run_class is not None:
        entry.update(await scope_of_run(session, table.app_id, run_class=run_class, started_by=started_by))
    return entry


async def scope_entries(session: AsyncSession, versions: dict[str, Any] | None, *, run_class: str | None,
                        started_by: str | None) -> None:
    """给 data_versions 里的记录表补上这次运行看哪些行（就地改）：正式运行看全部，别的按发起人在应用上的授权。"""
    from app.records.access import scope_of_run
    from app.records.tables import table_of_source

    for source_id, entry in (versions or {}).items():
        if not isinstance(entry, dict) or entry.get("origin") != RECORDS_ORIGIN:
            continue
        table = await table_of_source(session, source_id)
        entry.update(await scope_of_run(session, table.app_id if table is not None else None, run_class=run_class,
                                        started_by=started_by))


def is_records(source: Any) -> bool:
    return (getattr(source, "origin", None) or "manual") == RECORDS_ORIGIN
