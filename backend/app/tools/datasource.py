"""把数据源变成 agent 能调的工具。

每个启用的数据源生成一对工具，而不是"一个通用工具带 datasource 参数"：

- 工具名自带语义（`db_query__sales`），模型不会把查销售库的 SQL 发给财务库
- 工具 description 里直接塞该库的表清单，模型一眼看到有什么可查，
  省掉"先问有哪些表"那一轮
- 权限边界跟着工具走：只读源生成的工具就是只读的，dangerous 标记也各自独立

代价是工具数量随数据源线性增长。真到了几十个数据源的规模，再考虑按需装载。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data import introspect
from app.data.engine import run_query
from app.data.guard import QueryLimits, SqlRejected, is_write
from app.db.models import DataSource
from app.tools.registry import ToolContext

QUERY_PREFIX = "db_query__"
SCHEMA_PREFIX = "db_schema__"


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


def _no_schema_note(source: DataSource) -> str:
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


def _query_description(source: DataSource) -> str:
    tables = introspect.table_names(source)
    head = f"在数据源「{source.name}」上执行 SQL 查询。"
    if source.description:
        head += f"{source.description}。"
    head += "只读。" if source.readonly else "可写（写操作需要人工审批）。"
    if tables:
        listed = "、".join(tables[:25])
        more = f" 等 {len(tables)} 张表" if len(tables) > 25 else ""
        head += f" 可用表：{listed}{more}。"
        head += " 字段不确定时先查结构，不要猜字段名。"
    else:
        head += _no_schema_note(source)
    return head


async def build_datasource_tools(
    names: list[str], ctx: ToolContext, session: AsyncSession
) -> list[StructuredTool]:
    """按工具名反查数据源并构造工具。build_tools 的第四个来源。"""
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
        for prefix in wanted.get(row.name, []):
            if prefix == QUERY_PREFIX:
                tools.append(_make_query_tool(row, ctx))
            else:
                tools.append(_make_schema_tool(row))
    return tools


def _make_query_tool(source: DataSource, ctx: ToolContext) -> StructuredTool:
    async def _run(sql: str, limit: int | None = None) -> str:
        limits = QueryLimits(max_rows=int(limit)) if limit else QueryLimits()
        try:
            result = await run_query(source, sql, limits=limits)
        except SqlRejected as e:
            # 被守卫拒掉要把原因原样交回模型——它据此改写 SQL 重试，
            # 给一句笼统的"失败了"只会让它瞎猜
            return f"SQL 被拒绝：{e}"
        except Exception as e:  # noqa: BLE001
            return f"查询失败：{type(e).__name__}: {e}"

        payload = result.to_payload()
        payload["source"] = source.name
        # 查询快照进工件库：出具体系的数字回指要能下钻到"这个数是哪条 SQL 查出来的"
        try:
            from app.core.artifact_store import put_json

            payload["artifact"] = await put_json(
                payload, kind="query_snapshot",
                run_id=ctx.run_id or "", node_id=ctx.node_id or "",
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
    )


def _make_schema_tool(source: DataSource) -> StructuredTool:
    async def _run(table: str | list[str] | None = None) -> str:
        wanted = [table] if isinstance(table, str) else list(table or [])
        # 逗号分隔也认："user, roles" 和 ["user","roles"] 是同一个意思，
        # 而模型两种都会写。为此拒绝一次调用，纯属浪费一步
        wanted = [t.strip() for one in wanted for t in str(one).split(",") if t.strip()]
        if wanted:
            return "\n\n".join(introspect.describe_table(source, t) for t in wanted)
        tables = introspect.table_names(source)
        if not tables:
            # 把实情交出去，而不是让它去点一个它点不到的按钮
            return f"数据源「{source.name}」的结构信息不可用。" + _no_schema_note(source)
        return f"数据源「{source.name}」共 {len(tables)} 张表：\n" + "\n".join(
            f"  {n}" for n in tables
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


def is_dangerous_call(source: DataSource, sql: str) -> bool:
    """这次调用要不要走人工审批：非只读源上的写操作要。"""
    return not source.readonly and is_write(sql)
