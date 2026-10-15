"""数据源的只读表结构视图：库概览、表目录、表详情（字段、数据目录、连接条件、两跳的连接路径）。

助手查看（app/look/tables.py）、运行时 agent 的 db_schema__（tools/datasource.py）、/api/datasources/{id}/schema?table=
三处共用这一份（look 文档 D6：同一张表只有一种说法，同一套遮罩规矩）。以前三处各拼各的：助手的小库摘要每张表只列
前 6 列，db_schema 和助手又都把遮罩列的目录原样交给模型。

**遮罩规矩。** 遮罩列（data/engine.py 的 masked_columns，按列名设在整个数据源上）只给列名和类型：它的注释、目录里
这一列的各项（含码值、带具体取值的说明）都不给。和表助手（data/catalog_assistant.py）是同一条规矩。代价是运行时
agent 看到的目录少了遮罩列那部分（README 写明）。

只读：只看表结构缓存（schema_cache）和调用方读好的数据目录，不碰业务库。这里的文字都是写给模型看的，不上界面。
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from app.data import catalog, introspect
from app.data.catalog import CARDINALITY_LABEL, INFERRED_MARK, JoinEdge
from app.data.engine import masked_columns

#: 一个源最多列多少条连接条件、多少条连接路径
JOIN_LINES_LIMIT = 40
PATH_LINES_LIMIT = 12

# ==========================================================================
# 写给模型的文字
# ==========================================================================

OVERVIEW_LINE_PROMPT = "- {name}（{kind}，{access}）：{description}｜{state}"
OVERVIEW_TABLES_PROMPT = "共 {total} 张表"
OVERVIEW_PARTIAL_PROMPT = "共 {total} 张表，只探查了 {explored} 张"
OVERVIEW_CATALOG_ALL_PROMPT = "都有数据目录"
OVERVIEW_CATALOG_SOME_PROMPT = "{n} 张有数据目录"
OVERVIEW_CATALOG_NONE_PROMPT = "还没有数据目录"
OVERVIEW_MASKED_PROMPT = "设了遮罩"
OVERVIEW_SCHEMAS_PROMPT = "该账号名下没有对象，数据可能在这些 schema：{names}"
MASKED_COLUMN_PROMPT = "已遮罩，只给列名和类型"
BRIEF_PROMPT = "{name}[{kind}]：{columns}（全部 {n} 列，字数超了只给字段名，要看字段说明和目录时点名再 look）"
JOIN_HEAD_PROMPT = "表之间的连接条件（写 JOIN … ON … 时照抄）："
JOIN_MORE_PROMPT = "…另有 {n} 条，点名查看相关的表看各表的关联关系"
PATH_HEAD_PROMPT = "连接路径："
ROUTE_PROMPT = "{a} 与 {b} 没有直接关联，经 {mid} 连接：FROM {a} JOIN {mid} ON {first} JOIN {b} ON {second}"


# ==========================================================================
# 遮罩
# ==========================================================================


def masked_set(source: Any) -> set[str]:
    """这个源的遮罩列（小写）。"""
    return {c.lower() for c in masked_columns(getattr(source, "options", None))}


def _masked_meta(meta: Mapping[str, Any], masked: set[str]) -> dict[str, Any]:
    """表结构里遮罩列的注释抹掉：注释里常有取值说明（「1=男 2=女」），遮罩列只给列名和类型。"""
    out = dict(meta)
    out["columns"] = [{**col, "comment": None} if str(col.get("name") or "").lower() in masked else col
                      for col in meta.get("columns") or [] if isinstance(col, Mapping)]
    return out


def masked_notes(notes: Mapping[str, Any] | None, masked: set[str]) -> dict[str, Any]:
    """目录里遮罩列的各项整列去掉（含码值和说明）。表级的项、关联关系只是列名，照给。"""
    out = copy.deepcopy(dict(notes or {}))
    columns = out.get("columns")
    if masked and isinstance(columns, dict):
        out["columns"] = {name: items for name, items in columns.items() if str(name).lower() not in masked}
    return out


# ==========================================================================
# 库概览、表目录
# ==========================================================================


def overview_line(source: Any, *, catalog_tables: int) -> str:
    """system 里一个库一行：名称、类型、是否只读、说明、表数（没探查全时写明）、有数据目录的表数、有没有设遮罩；
    没探查或探查失败的写原因（introspect.why_empty）。"""
    cache = getattr(source, "schema_cache", None) or {}
    tables = cache.get("tables") or {}
    if not tables:
        state = introspect.why_empty(cache)
        if candidates := cache.get("available_schemas"):
            state += "，" + OVERVIEW_SCHEMAS_PROMPT.format(names="、".join(candidates[:8]))
    else:
        total = cache.get("total")
        total = max(total if isinstance(total, int) and not isinstance(total, bool) else 0, len(tables))
        parts = [OVERVIEW_PARTIAL_PROMPT.format(total=total, explored=len(tables)) if cache.get("truncated")
                 and total > len(tables) else OVERVIEW_TABLES_PROMPT.format(total=total)]
        if catalog_tables >= len(tables):
            parts.append(OVERVIEW_CATALOG_ALL_PROMPT)
        elif catalog_tables:
            parts.append(OVERVIEW_CATALOG_SOME_PROMPT.format(n=catalog_tables))
        else:
            parts.append(OVERVIEW_CATALOG_NONE_PROMPT)
        if masked_set(source):
            parts.append(OVERVIEW_MASKED_PROMPT)
        state = "，".join(parts)
    return OVERVIEW_LINE_PROMPT.format(
        name=source.name, kind=source.kind, access="只读" if source.readonly else "可写",
        description=source.description or "未填说明", state=state)


def table_index(source: Any, notes_by_table: Mapping[str, Mapping[str, Any]], *,
                tables: Iterable[str] | None = None) -> str:
    """紧凑的表目录，每张表一行：表名｜中文名｜粒度（catalog.render_table_index）。挑选失败时交回给模型点名。"""
    return catalog.render_table_index(getattr(source, "schema_cache", None), notes_by_table, tables=tables)


# ==========================================================================
# 表详情
# ==========================================================================


def table_detail(source: Any, table: str, *, catalog_notes: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """单表的完整字段说明，有可给模型看的目录时附在后面；遮罩列只给列名和类型。

    catalog_notes 是这个源的数据目录 {表名: notes}（原样，含驳回项：render_table_notes 自己去掉）。找不到这张表时
    交回的几种说法（结构没探查、只探查了一部分、确实没有）和 introspect.describe_table 一样。
    """
    meta = introspect.find_table(source, table)
    if not meta:
        return introspect.describe_table(source, table)
    masked = masked_set(source)
    shown = _masked_meta(meta, masked)
    head = f"{'视图' if meta.get('is_view') else '表'} {meta.get('qualified', table)}"
    if meta.get("comment"):
        head += f"（{meta['comment']}）"
    lines = [head]
    pk = set(meta.get("primary_key") or [])
    for col in shown["columns"]:
        marks = []
        if str(col.get("name") or "").lower() in masked:
            marks.append(MASKED_COLUMN_PROMPT)
        else:
            if col["name"] in pk:
                marks.append("主键")
            if not col.get("nullable", True):
                marks.append("非空")
            if col.get("comment"):
                marks.append(col["comment"])
        suffix = f"  {'、'.join(marks)}" if marks else ""
        lines.append(f"  {col['name']}  {col.get('type') or ''}{suffix}")
    if catalog_notes:
        key = catalog.resolve_table_name(getattr(source, "schema_cache", None), table)
        block = catalog.render_table_notes(masked_notes(catalog_notes.get(key) if key else None, masked),
                                           meta=shown, system_notes=catalog.system_notes_source(source))
        if block:
            lines.append(block)
    return "\n".join(lines)


def table_brief(source: Any, table: str) -> str:
    """字数超了时的一行：表名和全部字段名（遮罩列的名字照给，名字本身不是值）。"""
    meta = introspect.find_table(source, table) or {}
    columns = [str(c.get("name")) for c in meta.get("columns") or [] if isinstance(c, Mapping) and c.get("name")]
    return BRIEF_PROMPT.format(name=meta.get("qualified", table), kind="视图" if meta.get("is_view") else "表",
                               columns="、".join(columns), n=len(columns))


# ==========================================================================
# 连接条件与连接路径
# ==========================================================================


def relation_graph(source: Any, raw_notes: Mapping[str, Mapping[str, Any]]) -> dict[str, list[JoinEdge]]:
    """这个源的关系图：目录里的关系（驳回的不进，驳回项还要压住同一条关系的现推结果，所以给原样的目录）加上按表结构
    现推的（外键约束、命名推断）。"""
    return catalog.relation_graph(raw_notes, schema_cache=getattr(source, "schema_cache", None))


def complete(picked: list[str], graph: Mapping[str, list[JoinEdge]],
             room: int) -> tuple[list[str], list[tuple[str, str, tuple[JoinEdge, ...]]]]:
    """补连接用的中间表：(给详情的表, [(a, b, 经中间表的两跳路径)])。room 是还能补几张。

    挑中的表两两之间已经直接相连的不补；有一条路径的中间表已经在里面的也不补。
    """
    tables = list(picked)
    routes: list[tuple[str, str, tuple[JoinEdge, ...]]] = []
    for i, a in enumerate(picked):
        for b in picked[i + 1:]:
            if any(e.to_table == b for e in graph.get(a, ())):
                continue
            paths = [p for p in catalog.join_paths(graph, a, b, max_hops=2) if len(p) == 2]
            via = next((p for p in paths if p[0].to_table in tables), None)
            if via is None and paths and len(tables) - len(picked) < room:
                via = paths[0]
                tables.append(via[0].to_table)
            if via is not None:
                routes.append((a, b, via))
    return tables, routes


def qualifier(source: Any):
    """表名（schema_cache 的键）→ 全名：模型照着全名写 SQL，漏了 schema 前缀在 Oracle 上直接报错。"""
    tables = (getattr(source, "schema_cache", None) or {}).get("tables") or {}
    return lambda key: str((tables.get(key) or {}).get("qualified") or key)


def _condition(edge: JoinEdge, qualified) -> str:
    """连接条件，表名用全名：channel_visits.visit_id = visits.id（多列用 AND 连接）。"""
    left, right = qualified(edge.from_table), qualified(edge.to_table)
    return " AND ".join(f"{left}.{a} = {right}.{b}" for a, b in zip(edge.from_columns, edge.to_columns))


def _edge_line(edge: JoinEdge, qualified) -> str:
    card = f"（{CARDINALITY_LABEL[edge.cardinality]}）" if edge.cardinality in CARDINALITY_LABEL else ""
    return _condition(edge, qualified) + card + (INFERRED_MARK if edge.status == "proposed" else "")


def _edges_within(graph: Mapping[str, list[JoinEdge]], tables: list[str]) -> list[JoinEdge]:
    """两端都在 tables 里的关系，每条一次（按 relation_id）；一对多的翻过来，从「多」的一端写起（外键所在的表在左）。"""
    wanted = set(tables)
    seen: set[str] = set()
    out = []
    for table in tables:
        for edge in graph.get(table, ()):
            if edge.to_table not in wanted or edge.relation_id in seen:
                continue
            seen.add(edge.relation_id)
            out.append(edge.reversed() if edge.cardinality == "one_to_many" else edge)
    return out


def join_lines(source: Any, graph: Mapping[str, list[JoinEdge]], tables: list[str], *, indent: str = "  ") -> list[str]:
    """给出的几张表之间的连接条件，可以照抄进 SQL。没有就是空列表。"""
    edges = _edges_within(graph, tables)
    if not edges:
        return []
    qualified = qualifier(source)
    lines = [indent + JOIN_HEAD_PROMPT]
    lines += [f"{indent}  {_edge_line(e, qualified)}" for e in edges[:JOIN_LINES_LIMIT]]
    if len(edges) > JOIN_LINES_LIMIT:
        lines.append(indent + "  " + JOIN_MORE_PROMPT.format(n=len(edges) - JOIN_LINES_LIMIT))
    return lines


def route_lines(source: Any, routes: list[tuple[str, str, tuple[JoinEdge, ...]]], *, indent: str = "  ") -> list[str]:
    """两张表没有直接关联、要经一张中间表连接的写法。"""
    if not routes:
        return []
    qualified = qualifier(source)
    lines = [indent + PATH_HEAD_PROMPT]
    for a, b, (first, second) in routes[:PATH_LINES_LIMIT]:
        line = ROUTE_PROMPT.format(a=qualified(a), b=qualified(b), mid=qualified(first.to_table),
                                   first=_condition(first, qualified), second=_condition(second, qualified))
        inferred = first.status == "proposed" or second.status == "proposed"
        lines.append(f"{indent}  {line}" + (INFERRED_MARK if inferred else ""))
    return lines


__all__ = [
    "JOIN_LINES_LIMIT", "PATH_LINES_LIMIT", "complete", "join_lines", "masked_notes", "masked_set", "overview_line",
    "qualifier", "relation_graph", "route_lines", "table_brief", "table_detail", "table_index",
]
