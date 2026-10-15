"""记录表的查询视图（docs/design/normal-user-apps.md §3.7「查询连接」）。

origin=records 的数据源查询时换成一个 RecordsView（字段和 DataSource 同名，同 table_versions.SourceView 的做法）：
带着这次查询的高水位、看哪些行，以及连接上要先执行的两条语句（建同名临时视图）。data/engine.run_query 认这两样：
- prelude：借到连接后、执行查询前依次执行（DROP / CREATE TEMP VIEW records）；
- forbid_schemas：SQL 里直接写 main.<表> 的拒掉——不然绕过临时视图就看到了底表（全部行、被更正的行、幂等键）。

高水位来自运行的 data_versions（发起时记下，engine/runner._data_versions），不在运行里（工具库试用、数据剖析）的查询
用现在的数据、看全部（这些入口只有搭建者以上进得来）。

**跨版本汇总。** 表单不兼容地改过之后，看板查的是新一版的数据源，旧记录在比它老的版本文件里。查询时把这张表比这一版老的
各版本只读挂到连接上（attach：ATTACH 'file:…?mode=ro' AS records_v<版本>，data/engine.run_query 执行），临时视图按
UNION ALL 接上，列照这一版的来、旧版本没有的（或类型改得对不上的）补 NULL（records/store.view_prelude）。各版本各有高水位：
发起时一并记进 data_versions 那一项的 older（停用之前发起的运行还能往停用的版本写完，records/actions.py，所以停用的版本
同样要按高水位固定）。挂上去的库名同样进 forbid_schemas：records_v1.records 和 main.records 一样绕过视图，一律拒。
用户 SQL 里写 ATTACH 照旧被只读守卫拒（data/guard.py），挂库只有平台自己做。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.records import RECORDS_ORIGIN, store

logger = logging.getLogger(__name__)

#: data_versions 里记录表那一项的形状：{"name", "origin": "records", "table", "version", "seq", "older", "rows",
#: "viewer"?}。older 是比这一版老的各版本的高水位 [{"version", "seq"}]（跨版本汇总）；数据文件不在了的那一版记成
#: {"version", "seq": None, "missing": True}——汇总里没有它，印章（apps/resolve.stamp_of）、查询快照照实说
MISSING = "missing"
PIN_KEYS = ("name", "origin", "table", "version", "seq", "older", "rows", "viewer")
#: 旧版本挂在查询连接上的库名：records_v<版本>
ATTACH_PREFIX = "records_v"


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
    #: 先只读挂到连接上的旧版本：((库名, 文件路径), …)，data/engine.run_query 在 prelude 之前挂好
    attach: tuple[tuple[str, str], ...] = ()
    #: SQL 里不许直接写的库名（main 和挂上去的各旧版本）
    forbid_schemas: tuple[str, ...] = ("main",)
    #: 并进来的旧版本和各自的高水位 [{"version", "seq"}]，数据文件不在了、没并进来的记 {"version", "seq": None,
    #: "missing": True}（查询快照里记下，照实说这次汇总含哪几版、缺哪几版）
    older: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Older:
    """并进汇总的一个旧版本：版本号、文件、业务列、这次查询的高水位（None 为不限）。"""

    version: int
    path: str
    columns: list[store.Column]
    high_water: int | None

    @property
    def schema(self) -> str:
        return f"{ATTACH_PREFIX}{int(self.version)}"


def bind_view(source: Any, columns: list[store.Column], *, high_water: int | None, rows: str = "all",
              viewer: str | None = None, older: list[Older] | tuple[Older, ...] = (),
              missing: list[int] | tuple[int, ...] = ()) -> RecordsView:
    """数据源行 + 高水位 + 行级过滤（+ 并进来的旧版本；missing 是数据文件不在了、并不进来的旧版本号）→ RecordsView。"""
    rows = rows if rows in ("all", "mine", "none") else "none"
    older = tuple(older)
    return RecordsView(
        id=source.id, name=source.name, database=source.database, options=dict(source.options or {}),
        description=source.description or "", schema_cache=copy.deepcopy(source.schema_cache or {}),
        schema_synced_at=getattr(source, "schema_synced_at", None), enabled=bool(source.enabled),
        last_check=getattr(source, "last_check", None), high_water=high_water, rows=rows, viewer=viewer,
        prelude=store.view_prelude(columns, high_water=high_water, scope=rows, viewer=viewer,
                                   older=[store.OlderVersion(o.schema, o.columns, o.high_water) for o in older]),
        attach=tuple((o.schema, o.path) for o in older),
        forbid_schemas=("main", *(o.schema for o in older)),
        older=(*({"version": o.version, "seq": o.high_water} for o in older),
               *({"version": int(v), "seq": None, MISSING: True} for v in missing)))


def columns_of_source(source: Any) -> list[store.Column]:
    """数据源上登记的表结构里的业务列（不含系统列）。records_tables 的列定义是权威；这里只在拿不到登记时兜底。"""
    from app.records import SYSTEM_COLUMNS

    meta = ((getattr(source, "schema_cache", None) or {}).get("tables") or {}).get(store.TABLE) or {}
    return [store.Column(name=str(c.get("name")), label=str(c.get("comment") or c.get("name")),
                         type=_type_of(str(c.get("type") or "")))
            for c in meta.get("columns") or [] if isinstance(c, dict) and c.get("name") not in SYSTEM_COLUMNS]


def _type_of(sql_type: str) -> str:
    return {"REAL": "number", "INTEGER": "integer"}.get(sql_type.upper(), "string")


async def _older_versions(session: AsyncSession, table: Any) -> list[Any]:
    """这张表比这一版老的各版本（新的在前）：停用了的旧版本，以及起草出的新一版还没发布时线上在写的那一版。"""
    from app.records.tables import versions_of

    if table is None:
        return []
    return [row for row in await versions_of(session, table.table_key) if row.version < table.version]


async def resolve(session: AsyncSession, source: Any, entry: Any = None) -> RecordsView:
    """查询时实际该用的视图。entry 是这次运行 data_versions 里这张表的那一项；None 是不在运行里（现在的数据、看全部）。

    比这一版老的各版本一起并进来（跨版本汇总）：在运行里的，只并发起时固定了高水位的那几版（entry 的 older），各按自己的
    高水位；不在运行里的，各版本现在的数据都算。"""
    from app.records.tables import columns_of, path_of, table_of_source

    table = await table_of_source(session, source.id)
    columns = columns_of(table) if table is not None else columns_of_source(source)
    older_rows = await _older_versions(session, table)
    if isinstance(entry, dict):
        seq = entry.get("seq")
        marked = [o for o in entry.get("older") or [] if isinstance(o, dict) and isinstance(o.get("version"), int)]
        # 发起时文件就不在的那几版：没有高水位，不挂；照原样带着，快照里照实说汇总缺了它们
        missing = [int(o["version"]) for o in marked if o.get(MISSING)]
        pinned = {int(o["version"]): o.get("seq") for o in marked if not o.get(MISSING)}
        older = [Older(row.version, str(path_of(row)), columns_of(row),
                       int(pinned[row.version]) if isinstance(pinned[row.version], int) else 0)
                 for row in older_rows if row.version in pinned]
        return bind_view(source, columns, high_water=int(seq) if isinstance(seq, int) else 0,
                         rows=str(entry.get("rows") or "none"), viewer=entry.get("viewer"), older=older,
                         missing=missing)
    older = [Older(row.version, str(path_of(row)), columns_of(row), None) for row in older_rows
             if path_of(row).is_file()]
    missing = [row.version for row in older_rows if not path_of(row).is_file()]
    if missing:
        logger.warning("记录表 %s 第 %s 版的数据文件不在了，跨版本汇总里不含这几版", table.table_key,
                       "、".join(str(v) for v in missing))
    return bind_view(source, columns, high_water=None, rows="all", older=older, missing=missing)


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
    older: list[dict[str, Any]] = []
    for row in await _older_versions(session, table):
        try:
            older.append({"version": row.version, "seq": await asyncio.to_thread(store.high_water, path_of(row))})
        except store.RecordsRefused:
            # 旧版本的文件不在了（管理员手工删了文件才会这样）：汇总里没有它。记进这一项（印章、查询快照照实说缺了哪一版，
            # 看的人知道数不全），另记一条日志
            older.append({"version": row.version, "seq": None, MISSING: True})
            logger.warning("记录表 %s 第 %s 版的数据文件不在了，跨版本汇总里不含这一版", table.table_key, row.version)
    entry: dict[str, Any] = {"name": source.name, "origin": RECORDS_ORIGIN, "table": table.table_key,
                             "version": table.version, "seq": seq, "older": older}
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


def cache_token(entry: Any) -> Any:
    """按需视图缓存键里记录表那一项（apps/resolve.cache_key）：这一版的高水位，并进来的旧版本有的话连同它们的高水位——
    停用之前发起的运行还能往旧版本写（records/actions.py），旧版本多了一行，汇总就变了。"""
    if not isinstance(entry, dict):
        return None
    older = [[o.get("version"), o.get("seq")] for o in entry.get("older") or [] if isinstance(o, dict)]
    return [entry.get("seq"), older] if older else entry.get("seq")


def is_records(source: Any) -> bool:
    return (getattr(source, "origin", None) or "manual") == RECORDS_ORIGIN
