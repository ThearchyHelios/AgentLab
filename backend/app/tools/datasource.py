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
from app.data import introspect, schema_view, table_versions
from app.data.sqlcheck import SqlChecker
from app.data.engine import SnapshotTampered, masked_columns, query_timeout, run_query
from app.data.guard import QueryLimits, SqlRejected, check, is_write
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
#: ToolContext.extra 里放「认出的遮罩值」的键（engine/masking 的 SeenValues：调用方交进来的，加上查询时遮掉的）：核查
#: agent（engine/verify_agent.py）放进去，打开了遮罩开关的查询工具按它们按值遮，再把每次遮掉的值并进去。只在内存里给
#: 调用方按值遮用，不进快照、不交给模型
MASKED_VALUES_KEY = "masked_values"

#: 每次查询之后基于数据目录的 SQL 检查最多等这么久。正常一条 SQL 几毫秒；等不到就放弃这一次检查，
#: 查询结果照常交回——检查是附加的，不能拖慢查询
SQL_CHECK_TIMEOUT_S = 2.0
#: force_readonly 的工具拒掉写语句时交回的原因（接在「SQL 被拒绝：」后面）。不说「数据源是只读的」：源本身
#: 可能是可写的，是这次调用只许读；也不提「另建一个可写的数据源」——核查用不着写
FORCED_READONLY = ("这次查询只允许读取数据，这条语句会修改数据，不被允许。"
                   "只能执行不改数据的 SELECT / WITH / SHOW / DESCRIBE / EXPLAIN")


class _QueryArgs(BaseModel):
    sql: str = Field(description="要执行的 SQL，一次一条")
    limit: int | None = Field(default=None, description="最多返回多少行，默认 1000")


class _SchemaArgs(BaseModel):
    # 一次能问好几张。串行跑工具时一次调用就是一步，逐张问 6 张表就是 6 步——
    # 实测一次 8 步的运行里 7 步花在这上面，最后只剩一步来真查数据（run dd9927e6）
    table: str | list[str] | None = Field(
        default=None,
        description='表名。可以一次给多张："user" 或 ["user","roles"]；留空则列出表（表多时只列前 50 张）',
    )
    # 几千张表的库（next-round 03 A2）：不知道表名时按需求的关键词查，和助手 look 用同一个检索
    keyword: str | None = Field(
        default=None,
        description="不知道表名时写要查的内容的关键词（如「退货 门店」），按表名、注释、数据目录检索相关的表",
    )


def tool_names(source: DataSource) -> list[str]:
    return [f"{QUERY_PREFIX}{source.name}", f"{SCHEMA_PREFIX}{source.name}"]


def source_of_tool(name: str) -> str | None:
    """db_query__<源名> / db_schema__<源名> → 源名；不是数据源工具返回 None。"""
    for prefix in (QUERY_PREFIX, SCHEMA_PREFIX):
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


#: Run.options 里的键：这次运行的查询结果先遮再交给模型（api/runs.py：开登录后 user 角色发起的问数据运行）。发起时定下、
#: 随 run.started 封存，续跑、审批恢复沿用
MASK_RESULTS_OPTION = "mask_results"


#: Run.options 里的键：这次运行的数据源查询一律只读，可写源上的写语句也由守卫拒掉（_make_query_tool 的 force_readonly）。
#: 和 MASK_RESULTS_OPTION 一起由 api/runs.py 给开登录后 user 角色的问数据运行打上：只读不能交给审批——Agent 的
#: approval 写成 never、或者管理员给这个工具设过「始终允许」，写语句就不问人直接执行了。另记一个键而不是拿遮罩开关
#: 顺带：遮不遮和能不能写是两件事，各自说清
READ_ONLY_OPTION = "read_only_data"


def _run_option(run_ctx: Any, key: str) -> bool:
    options = (getattr(run_ctx, "extra", None) or {}).get("run_options") or {}
    return bool(options.get(key))


def run_masks_results(run_ctx: Any) -> bool:
    """这次运行的查询结果要不要在工具边界就遮（见 MASK_RESULTS_OPTION）。构造 ToolContext 时传进去。"""
    return _run_option(run_ctx, MASK_RESULTS_OPTION)


def run_reads_only(run_ctx: Any) -> bool:
    """这次运行的数据源查询是不是一律只读（见 READ_ONLY_OPTION）。构造 ToolContext 时传进去。"""
    return _run_option(run_ctx, READ_ONLY_OPTION)


def run_scope_fixed(run_ctx: Any) -> bool:
    """这次运行的记忆范围、知识库是不是由运行定、节点配置和工具参数都改不了（ToolContext.fixed_scope）：查询结果要遮、
    或者一律只读的运行（使用者的问数据、个人应用的刷新、个人蓝图的试运行，auth/chat_policy.py）。记忆、知识库的内容不经
    遮罩，对这类运行来说换个范围就绕过了遮罩。"""
    return run_masks_results(run_ctx) or run_reads_only(run_ctx)


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


def _query_description(source: Any, *, readonly: bool | None = None) -> str:
    """readonly 不给就按源本身；force_readonly 的工具传 True（源可写，这次调用也只许读）。"""
    tables = introspect.table_names(source)
    head = f"在数据源「{source.name}」上执行 SQL 查询。"
    if source.description:
        head += f"{source.description}。"
    head += "只读。" if (source.readonly if readonly is None else readonly) else "可写（写操作需要人工审批）。"
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
    names: list[str], ctx: ToolContext, session: AsyncSession, *, force_readonly: bool = False,
    mask_results: bool = False,
) -> list[StructuredTool]:
    """按工具名反查数据源并构造工具。build_tools 的第四个来源。

    上传源先按 ctx.data_versions 解析成绑定快照的视图（见 _resolve），工具描述、表结构、
    查询都按那个版本来。固定的版本找不到时建一个同名的替身（_unavailable_tool），
    每次调用都说明原因，不拿当前版本顶上。

    force_readonly：查询工具一律按只读守卫检查，可写源上的写语句也拒绝（见 _make_query_tool）。
    核查（engine/verify_agent.py）建工具时传它：那一路没有审批关卡，绝不能写。

    mask_results：查询结果交给模型、落进快照之前先遮（见 _make_query_tool）。核查建工具时也传它：模型看不到遮罩列的
    值，就不会把它们写进回答、理由。缺省关着：Agent 节点、调用工具节点的查询照旧原样。
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
        entries = await _catalog_of(row, ctx.catalog_view)
        facts, outside = await _extras_of(source)
        if outside is not None:
            # 有对象索引的源：按需加入以后两个工具都要看到新的表结构（查询时的 SQL 检查、冻结的表结构快照）
            source = _LiveSource(source)
        for prefix in prefixes:
            if prefix == QUERY_PREFIX:
                tools.append(_make_query_tool(source, ctx, fixed=fixed, catalog=entries, force_readonly=force_readonly,
                                              mask_results=mask_results, outside=outside))
            else:
                tools.append(_make_schema_tool(source, fixed=fixed, catalog=entries, facts=facts, ctx=ctx,
                                               outside=outside))
    return tools


class _LiveSource:
    """建工具时的数据源，schema_cache 可以换（运行时按需加入探查范围以后）：查询工具、表结构工具拿的是同一个对象，加进来的表
    两边都看得到。别的属性照原来的。不是 ORM 对象，换它不会写回库（库里由 source_objects.rebuild_schema_cache 写）。"""

    def __init__(self, base: Any) -> None:
        self._base = base
        self.schema_cache = getattr(base, "schema_cache", None) or {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


async def _extras_of(source: Any) -> tuple[dict[str, dict[str, Any]], frozenset[str] | None]:
    """建工具时读一次（和目录同一时机）：这个源的剖析事实（db_schema 的表详情附一节，next-round 遗留 e）、对象索引里探查
    范围外的对象（name_key；SQL 检查照实列「未按目录检查的表」，点名时按需加入）。没有对象索引的源（上传表格、升级前探查的）
    范围外是 None。读不到就当没有：少一节说明，不挡工具。"""
    from app.data import catalog_profile_jobs, source_objects
    from app.db.base import SessionLocal

    facts: dict[str, dict[str, Any]] = {}
    outside: frozenset[str] | None = None
    source_id = getattr(source, "id", None)
    if not source_id:
        return facts, outside
    try:
        async with SessionLocal() as session:
            if (getattr(source, "schema_cache", None) or {}).get("tables"):
                facts = await catalog_profile_jobs.last_facts(session, source_id)
            if isinstance((getattr(source, "schema_cache", None) or {}).get("scope"), dict) \
                    and (getattr(source, "origin", None) or "manual") == "manual":
                outside = await source_objects.outside_names(session, source)
    except Exception:  # noqa: BLE001
        logger.exception("读数据源 %s 的剖析事实、对象索引失败，这次不给", getattr(source, "name", "?"))
    return facts, outside


async def _catalog_of(row: Any, view: Any = None) -> dict[str, data_catalog.CatalogEntry]:
    """这个源的数据目录，建工具时读一次：db_schema 给模型看的和查询时冻结进表结构快照的是同一版。

    读哪一份由目录视图定（view，data/catalog_view.py：当前目录、目录快照、关；只用已核实的项），不给就是当前目录。
    读不到就当没有目录，工具照常可用——目录是附加说明，不能挡住查询；视图指定的目录快照不在了照实报错（不悄悄换成当前
    目录）。用单独的会话读：出错要回滚，而回滚会让调用方会话里已经加载的对象（row）全部过期，之后再读属性就是异步环境里
    的懒加载。
    """
    from app.data import catalog_view

    return await catalog_view.read(catalog_view.CatalogView.parse(view), row)


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


async def resolved_source(name: str, ctx: ToolContext) -> tuple[DataSource, Any] | None:
    """按名字找启用着的数据源，按这次运行固定的版本解析成查询用的那个源（_resolve，同 build_datasource_tools）：(数据源的行,
    查询用的源)。找不到、固定的版本没了返回 None。

    分析（app/tools/analysis.py）读目录、剖析事实、现查表的日期范围时用：查的要和这次运行的查询是同一版数据。用自己的短
    会话，不提交（行上已经读出来的属性会话关了照样能读）。"""
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(
            DataSource.name == name, DataSource.enabled.is_(True)))).scalar_one_or_none()
        if row is None:
            return None
        try:
            source, _ = await _resolve(session, row, ctx)
        except table_versions.SnapshotMissing:
            return None
        return row, source


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
    if (getattr(row, "origin", None) or "manual") == "records":
        return await _resolve_records(session, row, ctx), True
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


async def _resolve_records(session: AsyncSession, row: DataSource, ctx: ToolContext) -> Any:
    """记录表（docs/design/normal-user-apps.md N8）：按这次运行 data_versions 里记下的高水位和行级过滤，换成连接上建
    同名临时视图的 RecordsView（records/views.py）。和上传表格同一套规矩：

    - 不在运行里（工具库试用，Versions.CURRENT）、升级前发起的运行（None）：现在的数据、看全部（只有搭建者进得来）；
    - 在运行里却没拿到固定的版本（Versions.UNSET）：报错，不退回现在的数据；
    - 运行里还没固定（发起时没算到）：第一次用到时固定当时的高水位和行级过滤，记进运行（_pin_late_records）。"""
    from app.records import views as records_views

    pins = ctx.data_versions
    if pins is None or pins is Versions.CURRENT:
        return await records_views.resolve(session, row)
    if not isinstance(pins, dict):
        logger.error("记录表 %s 的工具没拿到用哪一版（运行 %s 节点 %s 的 ToolContext.data_versions=%r）",
                     row.name, ctx.run_id, ctx.node_id, pins)
        raise table_versions.SnapshotMissing(_versions_lost(row.name))
    entry = pins.get(row.id)
    if not isinstance(entry, dict) or not isinstance(entry.get("seq"), int):
        entry = await _pin_late_records(row, ctx)
    return await records_views.resolve(session, row, entry)


async def _pin_late_records(row: DataSource, ctx: ToolContext) -> dict[str, Any]:
    """发起时没固定到的记录表：第一次用到时固定当时的高水位和这次运行看哪些行，记进 runs.data_versions。
    做法同 _pin_late：锁内认运行记录里已有的，没有才现算；补固定这件事发一条日志事件。"""
    from app.db.base import SessionLocal
    from app.db.models import Run
    from app.records.views import pin_entry

    pins = ctx.data_versions
    assert isinstance(pins, dict)
    created = False
    async with _late_lock():
        entry = pins.get(row.id)
        if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
            return entry
        async with SessionLocal() as session:
            run = await session.get(Run, ctx.run_id) if ctx.run_id else None
            stored = dict((run.data_versions if run is not None else None) or {})
            entry = stored.get(row.id)
            if not (isinstance(entry, dict) and isinstance(entry.get("seq"), int)):
                entry = await pin_entry(session, row, run_class=run.run_class if run is not None else "exploratory",
                                        started_by=run.started_by if run is not None else None)
                if entry is None:
                    raise table_versions.SnapshotMissing(f"记录表「{row.name}」的登记或数据文件不在了，请联系管理员")
                entry["late"] = True
                created = True
                if run is not None:
                    stored[row.id] = entry
                    run.data_versions = stored
                    await session.commit()
        pins[row.id] = entry
    if created:
        await _announce(ctx, EventType.LOG, level="info", code=LATE_PIN_CODE,
                        message=f"记录表「{row.name}」发起时没有固定，本次运行第一次用到它时固定为当时已有的记录，"
                                "之后继续运行、审批恢复都沿用这一份",
                        source_id=row.id, source=row.name, seq=entry.get("seq"), late=True)
    return entry


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

    async def _schema(table: str | list[str] | None = None, keyword: str | None = None) -> str:
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
                     catalog: Mapping[str, data_catalog.CatalogEntry] | None = None,
                     force_readonly: bool = False, mask_results: bool = False,
                     outside: frozenset[str] | None = None) -> StructuredTool:
    """source 是手工源的 DataSource，或上传源绑定快照的 SourceView（字段同名）。

    fixed：上传源的版本是这次运行固定的（找不到时的说法不同）。
    catalog：建工具时读到的数据目录，随表结构快照冻结（_store_schema）。
    force_readonly：不管源可不可写，一律按只读守卫检查，写语句交回 FORCED_READONLY、不执行。只靠守卫这一道：
    连接池按源缓存、不分只读与否（engine.cache_key），可写源借不到连接层只读的连接——别为此另起一个只读池，
    缓存键相同，会把这个源平时用的可写连接也换成只读的。
    mask_results：数据源设了遮罩时，取自遮罩列的格子（按 SQL 认，改了名字的也算）在交给模型、落进快照之前就换成
    「已遮罩」，见 engine/masking.py 的 mask_query_result；这一次遮掉的值并进 ctx.extra[MASKED_VALUES_KEY]（有的话）。
    那里已经有的值（这个 agent 前面的查询遮掉的、调用方交进来的）这一次也按值换，数据源没设遮罩也一样：先查号码、
    再查写着号码的备注或别的库，后一份同样遮好。按值换只换文字格，数值格照原样（存下的是证据）。查询失败时报错原话里
    的值也换（masking.mask_error_text）。
    outside：对象索引里探查范围外的对象（name_key）。查询用到范围外、表结构里没有的表时，结果里照实列出
    unchecked_tables（「未按目录检查的表」）：SQL 检查只按范围内的表结构和目录查，不新增检查规则。
    """
    # 数据源在 options.query_timeout_s 里配了就用它，否则取缺省。数据库按它停下语句，
    # 引擎按它告诉界面上限是多少（metadata["timeout_s"]）
    seconds = query_timeout(source)
    # 基于数据目录的 SQL 检查器：和冻结进表结构快照的是同一版目录。第一次查询时才建（关系图也是用到才建）
    checker: list[tuple[int, SqlChecker]] = []

    def _checker() -> SqlChecker:
        # 表结构换过（按需加入了表）就重建：检查按的是此刻的表结构
        if not checker or checker[0][0] != id(source.schema_cache):
            checker[:] = [(id(source.schema_cache), SqlChecker(
                kind=source.kind, schema_cache=source.schema_cache,
                notes={name: entry.notes for name, entry in (catalog or {}).items()}, source_name=source.name))]
        return checker[0][1]

    async def _run(sql: str, limit: int | None = None) -> str:
        if gone := _vanished(source, fixed):
            return f"{DATA_UNAVAILABLE}{gone}"
        if force_readonly and not source.readonly:
            # 只读源由 run_query 自己按只读检查（原因照旧说「数据源是只读的」）；可写源在这里先按只读判一遍。
            # 先过一遍和 run_query 相同的检查：多条语句、结构变更这类问题照原来的说法交回
            try:
                statement = check(sql, readonly=False, source_name=source.name, dialect=(source.kind or "").lower())
            except SqlRejected as e:
                return f"SQL 被拒绝：{e}"
            if is_write(statement, dialect=(source.kind or "").lower()):
                return f"SQL 被拒绝：{FORCED_READONLY}"
        limits = QueryLimits(timeout_seconds=seconds, **({"max_rows": int(limit)} if limit else {}))
        mask = masked_columns(source.options)
        # 此前认出的遮罩值合起来的另一份（agent 前面的查询遮掉的在前，调用方交进来的在后，见 masking.SeenValues）：线程里
        # 读的时候这边不会再往里并。上万个值合一遍要几十毫秒，放进线程
        sink = ctx.extra.get(MASKED_VALUES_KEY) if mask_results else None
        known = await asyncio.to_thread(sink.pooled) if sink is not None else None
        known = known if known is not None and known.strong else None
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
            said = first_line(e) or explain(e)[0]
            if mask_results and (mask or known is not None):
                from app.engine.masking import mask_error_text

                # 数据库会把转换失败的值写进报错（invalid input syntax for type bigint: "139-0000-0001"）
                said = mask_error_text(said, sql, known)
            return f"查询失败：{said}"

        payload = result.to_payload()
        payload["source"] = source.name
        # 查询时就整列遮掉的结果列；没开遮罩开关、数据源没设遮罩又没有可按值换的时为 None（快照和原来一字不差）。
        # 别的列里按值换过的格子另记（masked_cells），没有就不记
        masked_now: list[str] | None = None
        masked_cells: list[list[int]] = []
        masked_whole = False
        if mask_results and (mask or known is not None):
            from app.engine.masking import mask_query_result

            # 按 SQL 认列、逐格比对是纯计算，放进线程（同 _sql_checks）
            done = await asyncio.to_thread(mask_query_result, payload, mask, source.kind, known,
                                           schema=source.schema_cache)
            if mask or done.payload is not payload:
                payload, masked_now, masked_cells = done.payload, done.columns, done.cells
                masked_whole = done.whole
            if sink is not None:
                # 并进 agent 查到的那一份（不和调用方交进来的混在一起，上限收满了也不丢），也放进线程
                await asyncio.to_thread(sink.add, done.secrets)
        # 对照数据目录检查这条 SQL（一对多关联后重复计算、存量跨期求和……）。结果交给模型（Agent 据此改写），
        # 也随查询快照存下：证据面板看得到，口径卡据此给指标标「存疑」、出具降档。没查出问题不加这个键，
        # 快照和以前一字不差
        if checks := await _sql_checks(_checker(), sql):
            payload["checks"] = checks
        # 结果里有整列是 0 或空的数值列时，现查查询区间里这张表有没有数据（data/coverage.py）：没有就照实写进结果和
        # 快照（no_data，给人和模型看的 message），运行事件里记一条 query_no_data。没查出问题不加这个键，快照和以前一字不差
        if gaps := await _coverage(source, str(payload.get("sql") or sql), payload, mask):
            payload["no_data"] = gaps
            await _announce(ctx, EventType.LOG, level="warn", code="query_no_data", no_data=gaps,
                            message="；".join(g["message"] for g in gaps))
        if outside and (unchecked := _unchecked_tables(sql, source, outside)):
            payload["unchecked_tables"] = unchecked
        # 表结构快照：这次查询时数据源的结构冻结下来，报告里写的表名、字段名按它核对。事后数据源
        # 重新探查、改了结构，已经跑完的运行核对的还是当时那一份。上传源冻结的是绑定快照里那份
        if schema := await _store_schema(source, ctx, catalog=catalog):
            payload["schema_artifact"] = schema
        # 查询快照进工件库：出具体系的数字回指要能下钻到"这个数是哪条 SQL 查出来的"
        try:
            from app.core.artifact_store import put_json

            # 只进快照、不进交给模型的结果的几样（模型看到多余的字段只会误会：以为数据被遮了，
            # 或者把一串十六进制抄进报告）：
            # - 查询当时数据源设的遮罩列：数据源事后改名、删掉，证据面板仍按当时的遮。只记数据源设的列名——证据面板把
            #   同源各份快照记的这一项并起来，从表的字段清单里去掉，混进结果列的别名会把同名的真实字段也去掉；
            # - masked_at_query：开了遮罩开关时，查询时就整列遮掉的结果列（这几列的格子是「已遮罩」，不是数据源里的值，
            #   改了名字的也在这里）。有这一项的快照，别的格子整格是「已遮罩」的是查询时按值换掉的；
            # - masked_cells：别的列里查询时按值换过的文字格 [行, 列号]（整格换的、换掉一截的），格子引用指到它们时照实
            #   说是遮罩。数值格不按值换，存下的数和数据库里的一样；
            # - masked_whole：这条 SQL 的写法认不全，整份结果查询时都遮了（masked_at_query 是全部的列），格子引用指到它时
            #   照实说写法认不全，不说成取自遮罩列；
            # - data_version：上传表格查的是哪个快照，事后据此认得出这个数出自哪一版数据。
            # 手工源、没设遮罩的，快照和原来一字不差
            extra: dict[str, Any] = {}
            if mask:
                extra["mask_columns"] = mask
            if masked_now is not None:
                extra["masked_at_query"] = masked_now
            if masked_cells:
                extra["masked_cells"] = masked_cells
            if masked_whole:
                extra["masked_whole"] = True
            if version := getattr(source, "snapshot_id", None):
                extra["data_version"] = version
            # 记录表（records/views.py）：查的是第几条以内、看的是谁的行。不写进 data_version：那一项是上传表格的快照
            # id，出处链按它找快照文件
            if getattr(source, "origin", None) == "records" and getattr(source, "prelude", None):
                extra["records_view"] = {"high_water": getattr(source, "high_water", None),
                                         "rows": getattr(source, "rows", None),
                                         # 跨版本汇总并进来的旧版本和各自的高水位（records/views.py）
                                         **({"older": list(source.older)} if getattr(source, "older", None) else {})}
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
        description=_query_description(source, readonly=True if force_readonly else None),
        args_schema=_QueryArgs,
        coroutine=_run,
        func=None,
        # 同一个工具，危不危险看这一次的 SQL。审批关卡经 registry.call_is_dangerous 问它。
        # force_readonly 的工具写不了，没有要人批的调用
        metadata={"dangerous_if": (lambda args: False) if force_readonly
                  else (lambda args: is_dangerous_call(source, str(args.get("sql") or ""))),
                  "timeout_s": seconds},
    )


#: 查询结果里「未按目录检查的表」那一项交给模型时的说明（键名 unchecked_tables 本身就是给它看的）
def _unchecked_tables(sql: str, source: Any, outside: frozenset[str]) -> list[str]:
    """SQL 里用到的、在对象索引里探查范围外、表结构里没有的表（照 SQL 里的写法）。认不出表就当没有。"""
    from app.data.names import name_key
    from app.engine.evidence import sql_tables

    try:
        names = sql_tables(sql)
    except Exception:  # noqa: BLE001
        return []
    cache = getattr(source, "schema_cache", None)
    return [t for t in dict.fromkeys(names)
            if name_key(str(t).rsplit(".", 1)[-1]) in outside and data_catalog.resolve_table_name(cache, str(t)) is None]


async def _coverage(source: Any, sql: str, payload: Mapping[str, Any], mask: list[str]) -> list[dict[str, Any]]:
    """查询区间里这张表没有数据的那几列（data/coverage.result_gaps）。只在结果里有整列是 0 或空的数值列时才查库；遮罩列
    不探查；现查表的范围不成时退回剖析、取值观察记下的范围（上传表格的快照对不上就不用）。从不抛异常。"""
    from app.data import coverage

    if not coverage.zero_columns(payload):
        return []

    async def facts(table: str) -> Mapping[str, Any] | None:
        from app.data.catalog_profile_jobs import last_facts
        from app.db.base import SessionLocal

        source_id = getattr(source, "id", None)
        if not source_id:
            return None
        name = data_catalog.resolve_table_name(getattr(source, "schema_cache", None), table) or table
        async with SessionLocal() as session:
            found = (await last_facts(session, str(source_id), [name])).get(name)
        snapshot = getattr(source, "snapshot_id", None)
        if found and snapshot and found.get("snapshot_id") != snapshot:
            # 上传表格：剖析时是另一版数据，范围对不上这次读的这一版
            return None
        return found

    return await coverage.result_gaps(source, sql, payload, facts=facts, masked=mask)


async def _sql_checks(checker: SqlChecker, sql: str) -> list[dict[str, Any]]:
    """一条查询的 SQL 检查结果（as_dict 的形状）。在线程里跑、限时 SQL_CHECK_TIMEOUT_S：解析是纯计算，放在事件循环里
    会挡住同一进程里别的运行。出错、超时都返回空列表——检查不能让一次成功的查询变成失败。"""
    if not checker.enabled:
        return []
    try:
        return await asyncio.wait_for(asyncio.to_thread(checker.check_dicts, sql), timeout=SQL_CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("SQL 检查超过 %.1f 秒，本次跳过（数据源 %s）", SQL_CHECK_TIMEOUT_S, checker.source_name)
    except Exception:  # noqa: BLE001
        logger.exception("SQL 检查出错，本次跳过（数据源 %s）", checker.source_name)
    return []


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
    # 这次运行读的不是缺省的当前目录（评测的对照：关、目录快照、只用已核实的项）：记在引用的 meta 里，证据面板写得出
    # 「本次运行目录：关」。不进内容哈希，缺省视图不记，快照和以前一字不差
    from app.data.catalog_view import CatalogView, describe

    if (view := describe(CatalogView.parse(getattr(ctx, "catalog_view", None)))) is not None:
        meta["catalog_view"] = view
    try:
        return await put_json(content, kind=SCHEMA_SNAPSHOT, run_id=ctx.run_id or "", node_id=ctx.node_id or "",
                              meta=meta)
    except Exception:  # noqa: BLE001 - 存不下不影响查询本身，只是这次报告不核对表名
        return None


def _schema_tables(table: str | list[str] | None) -> list[str]:
    """db_schema 的 table 参数 → 要看的表名。"user, roles"、["user","roles"]、整个写成一个字符串的 JSON 数组
    '["user", "roles"]' 是同一个意思，模型三种都会写，为此拒绝一次调用纯属浪费一步。最后那种只按逗号拆就成了
    「没有 ["user"」「没有 "roles"]」，两张表明明都在（2026-10 验收：盲算、反方审稿的核查 agent 因此白白多调了几轮
    模型）。只认元素都是字符串的 JSON 数组，别的写法照旧按逗号拆。"""
    wanted = [table] if isinstance(table, str) else list(table or [])
    names: list[str] = []
    for one in wanted:
        text = str(one).strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list) and all(isinstance(t, str) for t in parsed):
                names += parsed
                continue
        names.append(str(one))
    return [t.strip() for one in names for t in one.split(",") if t.strip()]


#: db_schema 不传 table 时最多列几张（几千张表的库整份列出来没法看，也塞不进上下文）、按 keyword 检索给几个
SCHEMA_LIST_LIMIT = 50
SCHEMA_KEYWORD_LIMIT = 30
SCHEMA_MORE_PROMPT = "…另有 {n} 张没有列出{outside}：用 keyword 按要查的内容检索，或者 table 直接点名"
SCHEMA_MORE_OUTSIDE = "（其中 {n} 张在探查范围外，点名时按需加入）"
SCHEMA_KEYWORD_HEAD = "数据源「{source}」里和「{keyword}」相关的表（按相关程度排）："
SCHEMA_KEYWORD_NONE = "数据源「{source}」里没有检索到和「{keyword}」相关的表：换个说法，或者 table 直接点名"
SCHEMA_OUTSIDE_MARK = "（探查范围外：table 点名它会按需加入）"
SCHEMA_ADDED_PROMPT = "（{table} 在探查范围外，这次按需加入了探查范围、当场读了结构）"
SCHEMA_RUN_CAPPED = "这次运行最多按需加入 {limit} 张探查范围外的表，已经用完"


def _make_schema_tool(source: Any, *, fixed: bool = False,
                      catalog: Mapping[str, data_catalog.CatalogEntry] | None = None,
                      facts: Mapping[str, Mapping[str, Any]] | None = None, ctx: ToolContext | None = None,
                      outside: frozenset[str] | None = None) -> StructuredTool:
    """source 同 _make_query_tool：上传源的表结构取绑定快照里冻结的那份。

    catalog：建工具时读到的数据目录，查单表时附在字段清单后面；facts：剖析事实，单表详情里附一节。单表的说法和助手查看、
    /schema 接口是同一份（data/schema_view.table_detail）：遮罩列只给列名和类型，它的注释和目录项都不给（look 文档 D6）。

    几千张表的库（next-round 03 A2）：不传 table 时只列范围内前 SCHEMA_LIST_LIMIT 张、提示用 keyword 或 table；keyword 和
    助手 look 用同一个检索（schema_view.search_tables）。点名探查范围外的表（outside 里有）当场只读反射、加进范围（每次运行
    最多 ON_DEMAND_PER_RUN 张，留痕记这次运行和节点）；表已不在、读不到结构、范围外没加进来的分开说。
    """
    notes = {name: entry.notes for name, entry in (catalog or {}).items()}

    async def _run(table: str | list[str] | None = None, keyword: str | None = None) -> str:
        if gone := _vanished(source, fixed):
            return f"{DATA_UNAVAILABLE}{gone}"
        wanted = _schema_tables(table)
        if wanted:
            added_lines: list[str] = []
            missing = [t for t in wanted if introspect.find_table(source, t) is None]
            reasons: dict[str, str] = {}
            if missing and outside is not None:
                reasons, added_lines = await _on_demand(source, missing, ctx)
            parts = []
            for t in wanted:
                if introspect.find_table(source, t) is not None or t not in reasons:
                    parts.append(schema_view.table_detail(source, t, catalog_notes=notes, profile=facts))
                else:
                    parts.append(reasons[t])
            return "\n\n".join(parts + added_lines)
        if keyword and keyword.strip():
            return await _keyword_list(source, notes, keyword.strip(), outside)
        tables = introspect.table_names(source)
        if not tables:
            # 把实情交出去，而不是让它去点一个它点不到的按钮
            return f"数据源「{source.name}」的结构信息不可用。" + _no_schema_note(source)
        raw = set(unshaped_tables(source))
        totals = set(reported_total_tables(source))
        shown = tables[:SCHEMA_LIST_LIMIT]
        total = max(len(tables), int((getattr(source, "schema_cache", None) or {}).get("total") or 0))
        head = f"数据源「{source.name}」共 {total} 张表" + (f"，探查范围内 {len(tables)} 张" if total > len(tables) else "")
        lines = [f"  {n}" + ("（按原样导入、未经规整，不能直接对列求和）" if n in raw else "")
                 + (REPORTED_TOTAL_MARK if n in totals else "") for n in shown]
        if total > len(shown):
            beyond = total - len(tables)
            lines.append("  " + SCHEMA_MORE_PROMPT.format(
                n=total - len(shown), outside=SCHEMA_MORE_OUTSIDE.format(n=beyond) if beyond > 0 else ""))
        return head + "：\n" + "\n".join(lines)

    return StructuredTool(
        name=f"{SCHEMA_PREFIX}{source.name}",
        description=(
            f"查看数据源「{source.name}」的表结构。"
            "写 SQL 前先用它确认字段名。不传 table 则列出表（表多时只列前 50 张）；"
            "**要看多张表就一次全传进来**（table 可以是数组），不要一张一张问；"
            "不知道表名时用 keyword 按要查的内容检索。"
        ),
        args_schema=_SchemaArgs,
        coroutine=_run,
        func=None,
    )


async def _keyword_list(source: Any, notes: Mapping[str, Mapping[str, Any]], keyword: str,
                        outside: frozenset[str] | None) -> str:
    """按 keyword 检索（范围内的表、对象索引里范围外的对象），每个命中一行：表名｜中文名（范围外的是注释、标出来）。"""
    objects: list[Any] = []
    if outside:
        from app.data import source_objects
        from app.db.base import SessionLocal

        try:
            async with SessionLocal() as session:
                objects = await source_objects.outside_objects(session, source)
        except Exception:  # noqa: BLE001 - 读不到范围外的清单就只检索范围内的
            objects = []
    hits = schema_view.search_tables(source, notes, keyword, outside=objects, limit=SCHEMA_KEYWORD_LIMIT)
    if not hits:
        return SCHEMA_KEYWORD_NONE.format(source=source.name, keyword=keyword)
    by_name = {o.name: o for o in objects}
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    schema = (getattr(source, "schema_cache", None) or {}).get("schema")
    lines = [SCHEMA_KEYWORD_HEAD.format(source=source.name, keyword=keyword)]
    for hit in hits:
        if hit.in_scope:
            lines.append("  " + data_catalog.render_table_index(getattr(source, "schema_cache", None),
                                                                 {k: data_catalog.visible_notes(v)
                                                                  for k, v in notes.items()}, tables=[hit.name])
                         if hit.name in tables else f"  {hit.name}")
        else:
            obj = by_name.get(hit.name)
            comment = f"｜{obj.comment}" if obj is not None and obj.comment else ""
            lines.append(f"  {f'{schema}.{hit.name}' if schema else hit.name}{comment}{SCHEMA_OUTSIDE_MARK}")
    return "\n".join(lines)


async def _on_demand(source: Any, missing: list[str], ctx: ToolContext | None) -> tuple[dict[str, str], list[str]]:
    """db_schema 点名了表结构里没有的表：探查范围外、对象索引里有的当场加进范围（每次运行最多 ON_DEMAND_PER_RUN 张），
    别的说清为什么不在。返回 ({点名: 交回的那句话}，加进来的提示行)；加进来的表不在前一个字典里（调用方照常给详情）。"""
    from app.data import source_objects as so
    from app.db.base import SessionLocal
    from app.look.base import ON_DEMAND_PER_LOOK, ON_DEMAND_PER_RUN

    from app.db.models import Run

    out: dict[str, str] = {}
    lines: list[str] = []
    starter: str | None = None
    try:
        async with SessionLocal() as session:
            reasons = await so.absent_reasons(session, source, missing)
            used = await so.count_added_in_run(session, ctx.run_id) if ctx is not None and ctx.run_id else 0
            # 留痕记上运行的发起人（同 look 那条路记对话里的发起人）：不然只有 run_id，要回头去翻运行才知道是谁
            if ctx is not None and ctx.run_id:
                starter = await session.scalar(select(Run.started_by).where(Run.id == ctx.run_id))
    except Exception:  # noqa: BLE001 - 查不了原因就照旧说
        logger.exception("db_schema 查表不在的原因失败（数据源 %s）", getattr(source, "name", "?"))
        return out, lines
    limit = ON_DEMAND_PER_RUN if ctx is not None and ctx.run_id else ON_DEMAND_PER_LOOK
    room = max(limit - used, 0)
    candidates = [t for t in missing if reasons.get(t) == so.ABSENT_OUT_OF_SCOPE]
    take = candidates[:room]
    for t in missing:
        if t in take:
            continue
        why = SCHEMA_RUN_CAPPED.format(limit=limit) if t in candidates else ""
        text = schema_view.absent_detail(source, t, reasons.get(t), why=why,
                                         error=((getattr(source, "schema_cache", None) or {}).get("skipped") or {})
                                         .get(t))
        if text is not None:
            out[t] = text
    if take:
        got = await so.add_to_scope(getattr(source, "id", ""), take, trail=so.Trail(
            via=so.VIA_SCHEMA_TOOL, actor=starter, run_id=(ctx.run_id or None) if ctx else None,
            ref=(ctx.node_id or None) if ctx else None,
            need=f"运行时 {SCHEMA_PREFIX}{getattr(source, 'name', '')} 点名：{'、'.join(take)}"))
        if got.cache is not None and got.added:
            source.schema_cache = got.cache
        for t in take:
            key = data_catalog.resolve_table_name(getattr(source, "schema_cache", None), t)
            if key is not None:
                lines.append(SCHEMA_ADDED_PROMPT.format(table=key))
                continue
            why = got.failed.get(t) or got.refused.get(t) or next(
                (v for k, v in {**got.failed, **got.refused}.items() if k.lower() == t.lower()), "")
            out[t] = schema_view.absent_detail(source, t, so.ABSENT_OUT_OF_SCOPE, why=why) or why
    return out, lines


def is_dangerous_call(source: Any, sql: str) -> bool:
    """这次调用要不要走人工审批：非只读源上的写操作要。

    只读源上的写不需要审批——守卫和连接层都会拒，批了也写不进去。
    """
    return not source.readonly and is_write(sql, dialect=source.kind)
