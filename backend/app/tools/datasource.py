"""把数据源变成 agent 能调的工具。

每个启用的数据源生成一对工具，而不是"一个通用工具带 datasource 参数"：

- 工具名自带语义（`db_query__sales`），模型不会把查销售库的 SQL 发给财务库
- 工具 description 里直接塞该库的表清单，模型一眼看到有什么可查，
  省掉"先问有哪些表"那一轮
- 权限边界跟着工具走：只读源生成的工具就是只读的，dangerous 标记也各自独立

代价是工具数量随数据源线性增长。真到了几十个数据源的规模，再考虑按需装载。

**上传表格按版本查。** 上传源每次重传是一个新快照（data/table_versions.py）。运行发起时把
用到的上传源的当前快照固定下来（runs.data_versions），这里按它把源解析成绑定那个快照的视图：
工具描述、表结构、冻结的表结构快照、查询，全部出自同一个版本——运行中途有人重传，这次运行
看到的还是发起时那一版。固定的版本没了就明确报错，绝不改用当前版本。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventType
from app.data import catalog as data_catalog
from app.data import introspect, table_versions
from app.data.engine import SnapshotTampered, masked_columns, query_timeout, run_query
from app.data.guard import QueryLimits, SqlRejected, is_write
from app.data.tabular import UNSHAPED_NOTE
from app.db.models import DataSource
from app.tools.registry import ToolContext, Versions

logger = logging.getLogger(__name__)

QUERY_PREFIX = "db_query__"
SCHEMA_PREFIX = "db_schema__"
#: RunContext.extra 里放这次运行固定的数据版本的键：engine/runner.py 放进去，构造 ToolContext 的节点取出来
RUN_VERSIONS_KEY = "data_versions"
#: 数据源工具因为数据本身用不了（这次运行固定的版本没了、表格文件被删）而没执行时交回的话的开头。
#: 和「查询失败：」分开：那种改了 SQL 能好，这种改 SQL 没用。调用工具节点据此判失败、原因照交，
#: 不再让人去改 SQL（engine/nodes/tools.py）。仍以「查询失败」起头：Agent 里这句话是作为工具结果
#: 交回的（tool.end），运行面板（frontend/src/run/decode.ts）按这个开头把那一步标成失败
DATA_UNAVAILABLE = "查询失败（数据不可用）："
#: 运行中途补固定一个源时发的那条日志事件的 code（_pin_late）
LATE_PIN_CODE = "data_version_pinned"


class _QueryArgs(BaseModel):
    sql: str = Field(description="要执行的 SQL，一次一条")
    limit: int | None = Field(default=None, description="最多返回多少行，默认 1000")


class _SchemaArgs(BaseModel):
    # 一次能问好几张。串行跑工具时一次调用就是一步，逐张问 6 张表就是 6 步——
    # 实测一次 8 步的运行里 7 步花在这上面，最后只剩一步来真查数据（run dd9927e6）
    table: str | list[str] | None = Field(
        default=None,
        description='表名。可以一次给多张："user" 或 ["user","roles"]；留空则列出所有表',
    )


def tool_names(source: DataSource) -> list[str]:
    return [f"{QUERY_PREFIX}{source.name}", f"{SCHEMA_PREFIX}{source.name}"]


def source_of_tool(name: str) -> str | None:
    """db_query__<源名> / db_schema__<源名> → 源名；不是数据源工具返回 None。"""
    for prefix in (QUERY_PREFIX, SCHEMA_PREFIX):
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def run_versions(run_ctx: Any) -> dict[str, Any] | Versions | None:
    """一次运行固定的数据版本（runner 放在 RunContext.extra 里）。构造 ToolContext 时传进去。

    runner 每段执行都放这个键，升级前发起的运行放 None。键不在，说明这个 RunContext 不是 runner
    建的，或者往下传的时候丢了（例如子工作流复制 extra 时漏掉）：交 Versions.UNSET，上传源据此
    报错——不能当成 None 改用当前版本，那样漏传一处就会静默查到别的版本。
    """
    extra = getattr(run_ctx, "extra", None) or {}
    return extra[RUN_VERSIONS_KEY] if RUN_VERSIONS_KEY in extra else Versions.UNSET


def pinned_snapshot(entry: Any) -> str | None:
    """runs.data_versions 里一个源的记录 → 快照 id。形状是 {"snapshot": id, "name": 源名}，也认直接写 id 的。"""
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        sid = entry.get("snapshot") or entry.get("snapshot_id")
        return sid if isinstance(sid, str) and sid else None
    return None


def _no_schema_note(source: Any) -> str:
    """结构拿不到时，把实情和下一步一起交给 agent。

    以前这里只说"结构尚未探查，请在设置页里执行一次"。两个问题：它可能是错的
    （探查执行过了，是超时失败），而且它给的下一步 agent 根本做不到——agent
    进不了设置页。于是它只能自己摸：run 554a0f92 里先猜了 7 个表名、又发了
    一条 SELECT 1 试连通性、再用三条 information_schema 把 schema 重建一遍，
    9 次工具调用花掉 6 次、29 秒之后才碰到真正要查的数据。

    那条自救路线是对的，只是它自己摸索了三轮才找到。直接告诉它。
    """
    cache = source.schema_cache or {}
    note = f" {introspect.why_empty(cache)}。"
    candidates = cache.get("available_schemas") or []
    if candidates:
        # 这几个候选一直在缓存里躺着，只是以前没人交给 agent
        note += (
            f"当前连的库是「{source.database or '未指定'}」，"
            f"这台服务器上还有这些库可选：{'、'.join(candidates[:10])}。"
        )
    note += (
        "**不要猜表名**——直接查数据字典自己确认，"
        "例如 information_schema.tables / information_schema.columns"
        "（SQLite 则是 sqlite_master），一条带 WHERE 的查询就能拿到表和字段。"
    )
    return note


def unshaped_tables(source: Any) -> list[str]:
    """按原样导入、未经规整的表（表结构里的 comment 是 tabular.UNSHAPED_NOTE），按表结构里的顺序。"""
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    return [meta.get("qualified", name) for name, meta in tables.items()
            if isinstance(meta, dict) and meta.get("comment") == UNSHAPED_NOTE]


def reported_total_tables(source: Any) -> list[str]:
    """按配方导入时另存的「原表写明的合计」表（表结构里 kind=reported_total，recipe_notes.apply_notes 记的）。"""
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    return [meta.get("qualified", name) for name, meta in tables.items()
            if isinstance(meta, dict) and meta.get("kind") == "reported_total"]


#: 原表写明的合计表在工具描述、db_schema 表清单里的标记。不含数字（H11）
REPORTED_TOTAL_MARK = "（原表写明的合计，不要彼此相加，也不要与明细相加）"

#: 按配方导入的源在工具描述「可用表」之后多加的两句（P2-SPEC 5.4，不含数字）
RECIPE_QUERY_HINT = " 中文表名和列名请加双引号。查单个值时，把主键列一起选出来，证据面板才能追到原表格子。"

#: 冻结 schema_snapshot 时从 schema_cache 复制的顶层键。import_mode、import_manifests 只有按配方导入的源才有：
#: 冻结进来以后，schema_snapshot 的内容哈希就承诺了导入清单（证据链按哈希从封存事件走到清单，中间不经过
#: 可改的数据库列，H5）；手工源和期 1 的上传源没有这两个键，冻结内容和原来一字不差。
#: snapshot_manifest（期 3，P3-SPEC 2.9）：按期累积物化的快照才有，是快照清单的内容哈希。冻结进来以后证据链是
#: 封存事件 → schema_snapshot → 快照清单 → 各期导入清单 → 原件或清除记录，每一跳都按内容哈希
_FROZEN_SCHEMA_KEYS = ("schema", "synced_at", "truncated", "total", "import_mode", "import_manifests",
                       "snapshot_manifest")
#: 表结构快照里放数据目录的顶层键（_store_schema）。不来自 schema_cache：目录存在 catalog_notes 表里
CATALOG_SNAPSHOT_KEY = "catalog"


def _query_description(source: Any) -> str:
    tables = introspect.table_names(source)
    head = f"在数据源「{source.name}」上执行 SQL 查询。"
    if source.description:
        head += f"{source.description}。"
    head += "只读。" if source.readonly else "可写（写操作需要人工审批）。"
    if tables:
        listed = "、".join(tables[:25])
        more = f" 等 {len(tables)} 张表" if len(tables) > 25 else ""
        head += f" 可用表：{listed}{more}。"
        # 按配方导入的源：表名列名是中文（不加引号在多数方言里报错），主键是证据面板追溯原表格子的钥匙
        if (getattr(source, "schema_cache", None) or {}).get("import_mode") == "recipe":
            head += RECIPE_QUERY_HINT
        # 未规整的说明以前只在 db_schema 查单表时看得到：只绑了查询工具的 Agent、调用工具节点接报告的
        # 链路都看不到，照样对列求和。工具描述里点名（不含数字，H11）
        if raw := unshaped_tables(source):
            shown = "、".join(raw[:10]) + (f" 等 {len(raw)} 张" if len(raw) > 10 else "")
            head += f" 其中 {shown} 按原样导入、未经规整：同一列里混有不同口径的行，不能直接对列求和。"
        # 原表写明的合计表同理：「不要相加」的说明只在 db_schema 查单表时看得到（AU-5）
        if totals := reported_total_tables(source):
            shown = "、".join(totals[:10]) + (f" 等 {len(totals)} 张" if len(totals) > 10 else "")
            head += f" 其中 {shown} 是原表写明的合计：各合计项可能互相重叠，不要彼此相加，也不要与明细表相加。"
        head += " 字段不确定时先查结构，不要猜字段名。"
    else:
        head += _no_schema_note(source)
    return head


async def build_datasource_tools(
    names: list[str], ctx: ToolContext, session: AsyncSession
) -> list[StructuredTool]:
    """按工具名反查数据源并构造工具。build_tools 的第四个来源。

    上传源先按 ctx.data_versions 解析成绑定快照的视图（见 _resolve），工具描述、表结构、
    查询都按那个版本来。固定的版本找不到时建一个同名的替身（_unavailable_tool），
    每次调用都说明原因，不拿当前版本顶上。
    """
    wanted: dict[str, list[str]] = {}
    for name in names:
        for prefix in (QUERY_PREFIX, SCHEMA_PREFIX):
            if name.startswith(prefix):
                wanted.setdefault(name[len(prefix):], []).append(prefix)

    if not wanted:
        return []

    rows = list((
        await session.execute(
            select(DataSource).where(
                DataSource.name.in_(list(wanted)), DataSource.enabled.is_(True)
            )
        )
    ).scalars())

    tools: list[StructuredTool] = []
    for row in rows:
        prefixes = wanted.get(row.name, [])
        try:
            source, fixed = await _resolve(session, row, ctx)
        except table_versions.SnapshotMissing as e:
            tools.extend(_unavailable_tool(row, prefix, str(e)) for prefix in prefixes)
            continue
        entries = await _catalog_of(row)
        for prefix in prefixes:
            if prefix == QUERY_PREFIX:
                tools.append(_make_query_tool(source, ctx, fixed=fixed, catalog=entries))
            else:
                tools.append(_make_schema_tool(source, fixed=fixed, catalog=entries))
    return tools


async def _catalog_of(row: DataSource) -> dict[str, data_catalog.CatalogEntry]:
    """这个源的数据目录，建工具时读一次：db_schema 给模型看的和查询时冻结进表结构快照的是同一版。

    读不到就当没有目录，工具照常可用——目录是附加说明，不能挡住查询。用单独的会话读：出错要回滚，
    而回滚会让调用方会话里已经加载的对象（row）全部过期，之后再读属性就是异步环境里的懒加载。
    """
    from app.db.base import SessionLocal

    try:
        async with SessionLocal() as own:
            return await data_catalog.read_catalog(own, row.id)
    except Exception:  # noqa: BLE001
        logger.exception("读取数据源 %s 的数据目录失败，本次不附目录", row.name)
        return {}


# --------------------------------------------------------------------------
# 上传表格的版本
# --------------------------------------------------------------------------


def _fixed_gone(name: str) -> str:
    """这次运行固定的版本找不到时交回的原因。不提当前版本有什么：说了就等于改用了当前版本。"""
    return (f"本次运行固定的数据版本已不存在（数据源「{name}」），不会改用当前版本。"
            "如需使用当前数据，请重新发起运行")


def _versions_lost(name: str) -> str:
    """在运行里却没拿到固定的版本（构造 ToolContext 的地方漏传了）时交回的原因。"""
    return (f"没有拿到本次运行固定的数据版本（数据源「{name}」），为免前后查到两版数据，不会改用当前版本。"
            "这是系统内部的问题，请联系管理员")


def _rebuilt(name: str) -> str:
    """这次运行固定的源被删掉、又用同一个名字新建了一个时交回的原因。"""
    return (f"本次运行固定的数据源「{name}」已被删除或重建，不会改用同名的新数据源。"
            "如需使用现在的数据，请重新发起运行")


def _pinned_under_name(pins: dict[str, Any], name: str) -> bool:
    """data_versions 里有没有以这个名字固定的源（记录里带 name 的才比得出来；老的直接写 id 的不算）。"""
    return any(isinstance(entry, dict) and entry.get("name") == name for entry in pins.values())


async def _resolve(session: AsyncSession, row: DataSource, ctx: ToolContext) -> tuple[Any, bool]:
    """查询该用的源，以及它的版本是不是这次运行固定的。

    - 手工源原样返回（不分版本，也不进 data_versions）。
    - 不在运行里（工具库试用，Versions.CURRENT）、升级前发起的运行（None）：当前版本。
    - 谁都没说用哪一版（Versions.UNSET，在运行里漏传了）：抛 SnapshotMissing，不退回当前版本。
    - 运行里固定了：只认那个快照。不属于这个源、已回收、文件不在，都抛 SnapshotMissing，
      绝不退回当前版本——运行前后两段查的必须是同一版数据，报告里的数字才说得清来历。
    - 运行里还没固定：第一次用到时固定当前版本并记进运行（_pin_late）。
    - 运行里固定过一个同名、id 不同的源（固定的那个被删了，又用同一个名字新建了一个）：抛
      SnapshotMissing，不改用同名的新源，也不当成「运行中途新建的源」去补固定。工具是按名字反查
      源的，不比对的话续跑会悄悄查到另一份数据。这一步在「手工源原样返回」之前：同名重建成手工源
      的也要拦住。
    """
    pins = ctx.data_versions
    if isinstance(pins, dict) and row.id not in pins and _pinned_under_name(pins, row.name):
        raise table_versions.SnapshotMissing(_rebuilt(row.name))
    if (getattr(row, "origin", None) or "manual") != "upload":
        return row, False
    if pins is None or pins is Versions.CURRENT:
        return await table_versions.resolve_source(session, row), False
    if not isinstance(pins, dict):
        logger.error("数据源 %s 的工具没拿到用哪一版（运行 %s 节点 %s 的 ToolContext.data_versions=%r）："
                     "构造 ToolContext 的地方漏传了 run_versions", row.name, ctx.run_id, ctx.node_id, pins)
        raise table_versions.SnapshotMissing(_versions_lost(row.name))
    sid = pinned_snapshot(pins.get(row.id))
    if sid is None:
        sid = await _pin_late(row, ctx)
    try:
        return await table_versions.resolve_source(session, row, sid), True
    except table_versions.SnapshotMissing as e:
        raise table_versions.SnapshotMissing(_fixed_gone(row.name)) from e


_late_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _late_lock() -> asyncio.Lock:
    """运行中途固定版本时用的锁，按事件循环各一把（asyncio.Lock 不能跨循环用）。

    并行的两个分支可能同时第一次用到同一个源：不串行的话，中间恰好有人重传，两边会各自
    固定一个版本。
    """
    loop = asyncio.get_running_loop()
    lock = _late_locks.get(loop)
    if lock is None:
        lock = _late_locks[loop] = asyncio.Lock()
    return lock


async def _pin_late(row: DataSource, ctx: ToolContext) -> str:
    """发起时没固定到的上传源：第一次用到时固定当前版本，记进 runs.data_versions，返回快照 id。

    发起时按图里绑定的工具算（engine/runner.py），有几种情形算不到：继续运行时改了节点配置、
    改绑了别的数据源；子工作流没固定版本、运行中途被改过；运行中途才新建或启用的源。
    它们不能每次都取当前版本——中途有人重传，同一次运行前后就查了两版数据。所以第一次用到时
    固定下来：同一段执行里后面的节点看同一个 dict（ctx.data_versions，就地写入），续跑、恢复
    从运行记录里读回来；回收也因为运行记录里有它而不删这个快照。记录带 late 标记，说明它不在
    发起时的 run.started 里。

    补固定这件事本身发一条日志事件（code 为 LATE_PIN_CODE，带源 id、快照 id）：当次这一段的
    run.started 里没有这个源，不发的话事件流里就完全没有它，封存清单（对事件做哈希）也记不到。
    之后续跑、恢复开出的新一段 run.started 会从运行记录里带上它。
    """
    from app.db.base import SessionLocal
    from app.db.models import Run

    pins = ctx.data_versions
    assert isinstance(pins, dict)   # _resolve 只在拿到运行固定的 dict 时才走到这里
    created = False
    async with _late_lock():
        if sid := pinned_snapshot(pins.get(row.id)):
            return sid      # 同一段执行里别的节点刚固定过
        # 读当前指针到写进运行记录，持着版本存储的锁（table_versions.pin_lock）：中间有人重传的话，
        # 发布后的回收看不到这次补固定，会把刚读到的快照删掉。指针在锁里重新读：row 是建工具前读的，
        # 那之后可能已经换过版本、旧的已经回收
        async with table_versions.pin_lock(), SessionLocal() as session:
            run = await session.get(Run, ctx.run_id) if ctx.run_id else None
            stored = dict((run.data_versions if run is not None else None) or {})
            entry = stored.get(row.id)
            # 运行记录里已经有（上一段执行补固定的，这一段的内存里还没有）：认运行记录的
            sid = pinned_snapshot(entry)
            if sid is None:
                sid = (await session.execute(
                    select(DataSource.current_snapshot_id).where(DataSource.id == row.id)
                )).scalar_one_or_none()
                if not sid:
                    raise table_versions.SnapshotMissing(f"上传的表格「{row.name}」没有可用的版本，请重新上传")
                entry = {"snapshot": sid, "name": row.name, "late": True}
                created = True
                if run is not None:
                    stored[row.id] = entry
                    run.data_versions = stored
                    await session.commit()
        pins[row.id] = entry
    if created:
        await _announce(ctx, EventType.LOG, level="info", code=LATE_PIN_CODE,
                        message=f"数据源「{row.name}」发起时没有固定版本，本次运行第一次用到它时固定为当时的版本，"
                                "之后继续运行、审批恢复都沿用这一版",
                        source_id=row.id, source=row.name, snapshot=sid, late=True)
    # 一定是非空的快照 id：交给 resolve_source 的空值会被当成「没固定」，退回当前指针
    return sid


async def _announce(ctx: ToolContext, event_type: EventType, **data: Any) -> None:
    """经 ToolContext.emit 往运行的事件流里发一条事件；没给 emit（不在运行里）就不发。发不出去不影响查询。"""
    if ctx.emit is None:
        return
    try:
        out = ctx.emit(event_type, **data)
        if inspect.isawaitable(out):
            await out
    except Exception:  # noqa: BLE001 - 事件只是留痕，固定本身已经记进运行记录
        logger.warning("补固定数据版本的事件没发出去（运行 %s）", ctx.run_id, exc_info=True)


def _vanished(source: Any, fixed: bool) -> str | None:
    """绑定的快照文件在建工具之后没了（被手工删掉）：返回原因，否则 None。

    建工具时 resolve_source 查过一次，但工具建好到调用之间隔着模型思考、审批等待。缓存的连接
    还开着已删除的文件，照样能查出旧数据——那份数据已经无从核对，不如明说。
    """
    if not getattr(source, "snapshot_id", None) or Path(source.database or "").is_file():
        return None
    return _fixed_gone(source.name) if fixed else f"上传的表格「{source.name}」当前版本的数据文件已不存在，请重新上传"


def _unavailable_tool(row: DataSource, prefix: str, reason: str) -> StructuredTool:
    """数据版本找不到时的替身：名字、参数和正常工具一样，描述和每次调用都说明原因。

    不能干脆不建：节点会报「找不到工具」，看不出是数据版本的问题。也不能拿当前版本顶上。
    描述里不列表名：表名是当前版本的，交给模型就等于改用了当前版本。
    """
    if prefix == QUERY_PREFIX:
        async def _query(sql: str, limit: int | None = None) -> str:
            # DATA_UNAVAILABLE 开头：调用工具节点据此判失败、原因照交，不让人去改 SQL（engine/nodes/tools.py）
            return f"{DATA_UNAVAILABLE}{reason}"

        return StructuredTool(
            name=f"{QUERY_PREFIX}{row.name}",
            description=f"在数据源「{row.name}」上执行 SQL 查询。目前无法使用：{reason}。",
            args_schema=_QueryArgs, coroutine=_query, func=None,
            metadata={"dangerous_if": lambda args: False, "timeout_s": query_timeout(row)},
        )

    async def _schema(table: str | list[str] | None = None) -> str:
        return f"{DATA_UNAVAILABLE}{reason}"

    return StructuredTool(
        name=f"{SCHEMA_PREFIX}{row.name}",
        description=f"查看数据源「{row.name}」的表结构。目前无法使用：{reason}。",
        args_schema=_SchemaArgs, coroutine=_schema, func=None,
    )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _make_query_tool(source: Any, ctx: ToolContext, *, fixed: bool = False,
                     catalog: Mapping[str, data_catalog.CatalogEntry] | None = None) -> StructuredTool:
    """source 是手工源的 DataSource，或上传源绑定快照的 SourceView（字段同名）。

    fixed：上传源的版本是这次运行固定的（找不到时的说法不同）。
    catalog：建工具时读到的数据目录，随表结构快照冻结（_store_schema）。
    """
    # 数据源在 options.query_timeout_s 里配了就用它，否则取缺省。数据库按它停下语句，
    # 引擎按它告诉界面上限是多少（metadata["timeout_s"]）
    seconds = query_timeout(source)

    async def _run(sql: str, limit: int | None = None) -> str:
        if gone := _vanished(source, fixed):
            return f"{DATA_UNAVAILABLE}{gone}"
        limits = QueryLimits(timeout_seconds=seconds, **({"max_rows": int(limit)} if limit else {}))
        try:
            result = await run_query(source, sql, limits=limits)
        except SqlRejected as e:
            # 被守卫拒掉要把原因原样交回模型——它据此改写 SQL 重试，
            # 给一句笼统的"失败了"只会让它瞎猜
            return f"SQL 被拒绝：{e}"
        except SnapshotTampered as e:
            # 快照文件和登记的哈希对不上、读不出来：数据本身用不了，改 SQL 没用。按 DATA_UNAVAILABLE
            # 交回，调用工具节点据此判失败、原因照交，不让人去改 SQL；Agent 也不会当成 SQL 写错去重写。
            # 下一步只说找管理员：同一个文件重传是同一个构建，会撞上同一个被改过的文件
            return f"{DATA_UNAVAILABLE}数据源「{source.name}」的{e}。请联系管理员检查数据目录"
        except Exception as e:  # noqa: BLE001
            from app.core.errors import explain, first_line

            # 驱动的原话留着（"no such table: x" 正是模型改写 SQL 要的线索），
            # 类名和 SQLAlchemy 的包装前缀去掉：这一行也会原样出现在运行面板上
            return f"查询失败：{first_line(e) or explain(e)[0]}"

        payload = result.to_payload()
        payload["source"] = source.name
        # 表结构快照：这次查询时数据源的结构冻结下来，报告里写的表名、字段名按它核对。事后数据源
        # 重新探查、改了结构，已经跑完的运行核对的还是当时那一份。上传源冻结的是绑定快照里那份
        if schema := await _store_schema(source, ctx, catalog=catalog):
            payload["schema_artifact"] = schema
        # 查询快照进工件库：出具体系的数字回指要能下钻到"这个数是哪条 SQL 查出来的"
        try:
            from app.core.artifact_store import put_json

            # 只进快照、不进交给模型的结果的两样（模型看到多余的字段只会误会：以为数据被遮了，
            # 或者把一串十六进制抄进报告）：
            # - 查询当时数据源设的遮罩列：数据源事后改名、删掉，证据面板仍按当时的遮；
            # - data_version：上传表格查的是哪个快照，事后据此认得出这个数出自哪一版数据。
            # 手工源、没设遮罩的，快照和原来一字不差
            extra: dict[str, Any] = {}
            if mask := masked_columns(source.options):
                extra["mask_columns"] = mask
            if version := getattr(source, "snapshot_id", None):
                extra["data_version"] = version
            # 工件引用的 meta 记下源和 SQL 里用到的表：数据目录按它数每张表被运行查询过几次
            # （catalog.table_usage），不用再逐个读工件内容。meta 不进内容哈希，快照本身一字不变
            payload["artifact"] = await put_json(
                {**payload, **extra} if extra else payload, kind=data_catalog.QUERY_SNAPSHOT_KIND,
                run_id=ctx.run_id or "", node_id=ctx.node_id or "",
                meta=data_catalog.query_snapshot_meta(source, str(payload.get("sql") or sql)),
            )
        except Exception:  # noqa: BLE001 - 存不下不影响查询本身
            pass
        return json.dumps(payload, ensure_ascii=False, default=str)

    return StructuredTool(
        name=f"{QUERY_PREFIX}{source.name}",
        description=_query_description(source),
        args_schema=_QueryArgs,
        coroutine=_run,
        func=None,
        # 同一个工具，危不危险看这一次的 SQL。审批关卡经 registry.call_is_dangerous 问它
        metadata={"dangerous_if": lambda args: is_dangerous_call(source, str(args.get("sql") or "")),
                  "timeout_s": seconds},
    )


async def _store_schema(source: Any, ctx: ToolContext, *,
                        catalog: Mapping[str, data_catalog.CatalogEntry] | None = None) -> str | None:
    """把数据源此刻的 schema_cache 存成 schema_snapshot 工件，返回工件 id。

    上传源传进来的是绑定快照的视图，schema_cache 就是快照里冻结的那份：查的是哪一版，冻结的
    就是哪一版的结构，不会是重传之后数据源上的新结构。

    内容寻址：结构没变的话，同一次运行里查多少次都是同一件，文件只有一份。没探查过结构
    （或者探查失败、一张表都没有）返回 None：没有东西可以冻结，报告也就不核对表名。

    数据目录一起冻结，放在顶层键 catalog 下：{表名: {version, notes（去掉驳回项）}}，只收这份结构里有的、
    有目录的表（catalog.frozen_catalog）。这次运行的模型看到的是哪一版目录，事后按快照就能回溯。已有字段
    一个不动；没有目录的源不加这个键，快照和以前一字不差；目录不变（版本和内容都不变）则快照不变。
    """
    cache = source.schema_cache or {}
    tables = cache.get("tables")
    if not isinstance(tables, dict) or not tables:
        return None
    from app.core.artifact_store import put_json
    from app.engine.evidence import SCHEMA_SNAPSHOT

    content = {"source": source.name, "tables": tables,
               **{k: cache[k] for k in _FROZEN_SCHEMA_KEYS if k in cache}}
    meta: dict[str, Any] = {"source": source.name, "tables": len(tables)}
    if frozen := data_catalog.frozen_catalog(catalog or {}, tables):
        content[CATALOG_SNAPSHOT_KEY] = frozen
        meta["catalog_tables"] = len(frozen)
    try:
        return await put_json(content, kind=SCHEMA_SNAPSHOT, run_id=ctx.run_id or "", node_id=ctx.node_id or "",
                              meta=meta)
    except Exception:  # noqa: BLE001 - 存不下不影响查询本身，只是这次报告不核对表名
        return None


def _make_schema_tool(source: Any, *, fixed: bool = False,
                      catalog: Mapping[str, data_catalog.CatalogEntry] | None = None) -> StructuredTool:
    """source 同 _make_query_tool：上传源的表结构取绑定快照里冻结的那份。

    catalog：建工具时读到的数据目录，查单表时附在字段清单后面（introspect.describe_table）。
    """
    notes = {name: entry.notes for name, entry in (catalog or {}).items()}

    async def _run(table: str | list[str] | None = None) -> str:
        if gone := _vanished(source, fixed):
            return f"{DATA_UNAVAILABLE}{gone}"
        wanted = [table] if isinstance(table, str) else list(table or [])
        # 逗号分隔也认："user, roles" 和 ["user","roles"] 是同一个意思，
        # 而模型两种都会写。为此拒绝一次调用，纯属浪费一步
        wanted = [t.strip() for one in wanted for t in str(one).split(",") if t.strip()]
        if wanted:
            return "\n\n".join(introspect.describe_table(source, t, catalog=notes) for t in wanted)
        tables = introspect.table_names(source)
        if not tables:
            # 把实情交出去，而不是让它去点一个它点不到的按钮
            return f"数据源「{source.name}」的结构信息不可用。" + _no_schema_note(source)
        raw = set(unshaped_tables(source))
        totals = set(reported_total_tables(source))
        return f"数据源「{source.name}」共 {len(tables)} 张表：\n" + "\n".join(
            f"  {n}" + ("（按原样导入、未经规整，不能直接对列求和）" if n in raw else "")
            + (REPORTED_TOTAL_MARK if n in totals else "") for n in tables
        )

    return StructuredTool(
        name=f"{SCHEMA_PREFIX}{source.name}",
        description=(
            f"查看数据源「{source.name}」的表结构。"
            "写 SQL 前先用它确认字段名。不传 table 则列出所有表；"
            "**要看多张表就一次全传进来**（table 可以是数组），不要一张一张问。"
        ),
        args_schema=_SchemaArgs,
        coroutine=_run,
        func=None,
    )


def is_dangerous_call(source: Any, sql: str) -> bool:
    """这次调用要不要走人工审批：非只读源上的写操作要。

    只读源上的写不需要审批——守卫和连接层都会拒，批了也写不进去。
    """
    return not source.readonly and is_write(sql, dialect=source.kind)
