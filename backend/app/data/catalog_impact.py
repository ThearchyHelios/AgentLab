"""数据目录的影响面与发布时的目录版本（阶段 4A：维护闭环）。

目录改了一项（「在园人数是存量」），用到这张表的模板算出来的数可能就该变了。这里回答两件事：

- **影响面**：哪些已发布、受管的模板用到了这张表（table_impact）。按每个模板当前的已发布版本算，不看画布上的
  草稿——正式运行跑的是已发布的那一版。调用工具节点写死的 SQL 用到了这张表算「直接引用」；Agent 绑定了这个源
  的查询工具算「可能涉及」（SQL 是运行时写的，静态看不出）；合并查询按它的输入一路追溯。
- **发布时的目录版本**：发布时记下模板用到的表的目录版本（catalog_versions_for），和影响面同一个说法分两组：
  {"direct": {源名: {表名: 版本}}, "possible": {源名: {表名: 版本}}}。direct 是写死的 SQL 用到的表；possible 是
  Agent、协作成员绑定了查询工具的源里所有有目录的表。从这一版发起正式运行时和当前的比（catalog_drift），有变化
  只提醒、不拦运行——目录是给人和助手看的说明，不是运行的输入；拦下来等于让一次文字修订卡住正式出具。

表名的取法和证据台账同一个口径：evidence.sql_tables 取 FROM / JOIN 后面的表（去掉注释、字符串、CTE），
去掉 schema 前缀后按 name_key 对到表结构上（和 catalog.table_usage 一致）。哪些节点算「写死的查询」以
sqlcheck.tool_query 为准（tool_queries），发布记录、影响面、SQL 检查和助手的上下文认的是同一种节点。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.names import name_key

logger = logging.getLogger(__name__)

#: 影响程度：direct 直接引用（SQL 里写着这张表）；possible 可能涉及（Agent 运行时自己写 SQL）
IMPACTS = ("direct", "possible")


def _lookup(tables: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in tables:
        out.setdefault(name_key(name), name)
    return out


def sql_table_keys(sql: str, tables: Iterable[str] | None) -> set[str]:
    """SQL 用到的表，对到表结构的写法上。tables 不给（不知道表结构）时用 SQL 里去掉 schema 前缀的原名。"""
    from app.engine.evidence import sql_tables

    try:
        names = sql_tables(sql or "")
    except Exception:  # noqa: BLE001 - 解析不了就当没用到表：影响面和版本记录都是提示，不能因此报错
        return set()
    shorts = [str(n).rsplit(".", 1)[-1] for n in names]
    if tables is None:
        return set(shorts)
    lookup = _lookup(tables)
    return {hit for s in shorts if (hit := lookup.get(name_key(s))) is not None}


def _as_node(raw: Mapping[str, Any]) -> Any:
    """字典写法的节点 → tool_query 认的样子（有 type、config）。画布 {data: {config}} 和 GraphSpec {config} 都认。"""
    config = raw.get("config")
    if not isinstance(config, Mapping):
        data = raw.get("data")
        config = data.get("config") if isinstance(data, Mapping) else None
    return SimpleNamespace(id=raw.get("id"), type=raw.get("type"), config=config if isinstance(config, Mapping) else {})


def tool_queries(nodes: Iterable[Any]) -> Iterator[tuple[Any, str, str]]:
    """一张图里调用工具节点写死的查询：(节点, 源名, SQL)，按图里的顺序。

    判定一律交给 sqlcheck.tool_query（是不是调用工具节点、是不是数据源查询工具、SQL 是不是非空字符串）：发布时
    记目录版本、影响面、SQL 检查、助手给全字段的表都走这一份，不各写一遍。节点认 GraphSpec 的节点对象，也认还没
    校验过的字典（画布写法、GraphSpec 写法）——助手改图时手上的是字典，图不一定完整到能过 GraphSpec。
    """
    from app.data.sqlcheck import tool_query

    for node in nodes or []:
        if isinstance(node, Mapping):
            found = tool_query(_as_node(node))
        elif node is not None:
            found = tool_query(node)
        else:
            found = None
        if found:
            yield node, found[0], found[1]


def graph_sql_tables(spec: Any, tables_by_source: Mapping[str, Iterable[str]] | None = None) -> dict[str, set[str]]:
    """一张图里调用工具节点写死的 SQL 用到的表：{源名: {表名}}。Agent 的 SQL 运行时才有，不在这里（见 agent_sources）。

    tables_by_source 是各源表结构里的表名（schema_cache 的 tables 的键）：给了的源按它规范表名、对不上的不算；
    没给的源用 SQL 里的原名。
    """
    out: dict[str, set[str]] = {}
    for _, source, sql in tool_queries(getattr(spec, "nodes", []) or []):
        known = (tables_by_source or {}).get(source)
        hits = sql_table_keys(sql, list(known) if known is not None else None)
        if hits:
            out.setdefault(source, set()).update(hits)
    return out


def _kind(node: Any) -> str:
    return str(getattr(node.type, "value", node.type))


def _setting(spec: Any, node: Any, key: str) -> Any:
    """节点配置的实际取值：节点上没写就取图级 defaults（和运行时 NodeContext.cfg 同一个规则）。"""
    value = (node.config or {}).get(key)
    return (getattr(spec, "defaults", None) or {}).get(key) if value in (None, "") else value


def _query_sources(tools: Any) -> set[str]:
    """工具清单里绑定的数据源查询工具（db_query__<源>）→ 源名。"""
    from app.tools.datasource import QUERY_PREFIX

    names = [t.strip() for t in tools if isinstance(t, str)] if isinstance(tools, (list, tuple)) else []
    return {t[len(QUERY_PREFIX):] for t in names if t.startswith(QUERY_PREFIX) and len(t) > len(QUERY_PREFIX)}


def agent_bindings(spec: Any) -> list[tuple[Any, str | None, set[str]]]:
    """Agent 节点和协作成员各自绑定了哪些源的查询工具：[(节点, 成员名或 None, {源名})]，按图里的顺序。

    它们的 SQL 运行时才由模型写出来，静态看不出用哪张表：影响面算「可能涉及」，发布时记下整个源有目录的表。
    """
    out: list[tuple[Any, str | None, set[str]]] = []
    for node in getattr(spec, "nodes", []) or []:
        kind = _kind(node)
        if kind == "agent":
            out.append((node, None, _query_sources(_setting(spec, node, "tools"))))
        elif kind == "supervisor":
            for member in _setting(spec, node, "agents") or []:
                if isinstance(member, Mapping):
                    out.append((node, str(member.get("name") or ""), _query_sources(member.get("tools"))))
    return out


def agent_sources(spec: Any) -> set[str]:
    """Agent 节点、协作成员绑定了查询工具的数据源名。"""
    return {name for _, _, bound in agent_bindings(spec) for name in bound}


def template_refs(spec: Any, source: str, table: str, tables: Iterable[str] | None) -> list[dict[str, Any]]:
    """一张图里哪些节点用到了某个源的某张表，按图里的顺序。每项 {node_id, label, type, impact, via?, member?}。

    - 调用工具节点：调用 db_query__<源>、SQL 里有这张表 → direct。
    - Agent 节点、协作成员：绑定了 db_query__<源> → possible（每个成员各一项，带 member）。
    - 合并查询：输入（别名 → 节点）里有用到这张表的 → 取输入里最强的那一级，via 写经由哪些输入。合并的合并
      一路追到底；成环（校验会报错的图）时停下。
    """
    wanted = name_key(table)
    known = list(tables) if tables is not None else None
    nodes = list(getattr(spec, "nodes", []) or [])
    by_id = {n.id: n for n in nodes}
    own: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []

    def base(node: Any, impact: str, **extra: Any) -> dict[str, Any]:
        return {"node_id": node.id, "label": node.title, "type": _kind(node), "impact": impact, **extra}

    for node, name, sql in tool_queries(nodes):
        if name == source and wanted in {name_key(t) for t in sql_table_keys(sql, known)}:
            own[node.id] = base(node, "direct")
    for node, member, bound in agent_bindings(spec):
        if source not in bound:
            continue
        if member is None:
            own[node.id] = base(node, "possible")
        else:
            out.append(base(node, "possible", member=member))

    resolved: dict[str, dict[str, Any] | None] = {}

    def through(node_id: str, trail: frozenset[str]) -> dict[str, Any] | None:
        """合并查询节点经由输入用到这张表的样子；用不到返回 None。"""
        if node_id in resolved:
            return resolved[node_id]
        node = by_id.get(node_id)
        if node is None or _kind(node) != "merge" or node_id in trail:
            return own.get(node_id)
        via: list[dict[str, Any]] = []
        best: str | None = None
        inputs = (node.config or {}).get("inputs")
        for alias, target in (inputs.items() if isinstance(inputs, Mapping) else []):
            if not isinstance(target, str):
                continue
            hit = own.get(target) or through(target, trail | {node_id})
            if hit is None:
                continue
            via.append({"node_id": target, "label": by_id[target].title if target in by_id else target, "alias": alias})
            best = "direct" if hit["impact"] == "direct" or best == "direct" else "possible"
        resolved[node_id] = base(node, best, via=via) if best else None
        return resolved[node_id]

    for node in nodes:
        if node.id in own:
            out.append(own[node.id])
        elif _kind(node) == "merge" and (hit := through(node.id, frozenset())):
            out.append(hit)
    order = {n.id: i for i, n in enumerate(nodes)}
    out.sort(key=lambda r: order.get(r["node_id"], 0))
    return out


async def _published(session: AsyncSession) -> list[tuple[Any, Any]]:
    """每个发布过的模板和它当前的已发布版本：[(Workflow, WorkflowVersion)]。"""
    from app.db.models import Workflow, WorkflowVersion

    rows = (await session.execute(
        select(Workflow, WorkflowVersion)
        .join(WorkflowVersion, (WorkflowVersion.workflow_id == Workflow.id)
              & (WorkflowVersion.version == Workflow.published_version))
        .where(Workflow.published_version.is_not(None))
    )).all()
    return [(w, v) for w, v in rows]


async def table_impact(session: AsyncSession, source: str, table: str,
                       tables: Iterable[str] | None) -> list[dict[str, Any]]:
    """引用某个源某张表的已发布、受管模板，直接引用的在前，同级按名字。

    每项 {workflow_id, name, version, level, impact, nodes}：version 是当前的已发布版本，level 是它发布时的级别
    （published / governed；记级别之前发布的老版本按工作流现在的状态）。图解析不了的版本跳过。
    """
    from app.engine.schema import GraphSpec

    known = list(tables) if tables is not None else None
    out: list[dict[str, Any]] = []
    for workflow, version in await _published(session):
        try:
            spec = GraphSpec.model_validate(version.graph or {})
        except Exception:  # noqa: BLE001 - 解析不了的版本发起不了正式运行，也就谈不上影响
            continue
        refs = template_refs(spec, source, table, known)
        if not refs:
            continue
        level = version.level or (workflow.status if workflow.status in ("published", "governed") else "published")
        out.append({
            "workflow_id": workflow.id, "name": workflow.name, "version": version.version, "level": level,
            "impact": "direct" if any(r["impact"] == "direct" for r in refs) else "possible", "nodes": refs,
        })
    out.sort(key=lambda t: (t["impact"] != "direct", t["name"]))
    return out


# ==========================================================================
# 发布时的目录版本
# ==========================================================================


async def _sources_by_name(session: AsyncSession, names: Iterable[str]) -> dict[str, Any]:
    """启用着的数据源：{源名: (DataSource 行, 用来读表结构的源)}。上传源解析成当前快照的视图，解析不了用原行。"""
    from app.data import table_versions
    from app.db.models import DataSource

    wanted = sorted({n for n in names if n})
    if not wanted:
        return {}
    rows = (await session.execute(
        select(DataSource).where(DataSource.name.in_(wanted), DataSource.enabled.is_(True))
    )).scalars()
    out: dict[str, Any] = {}
    for row in rows:
        try:
            view = await table_versions.resolve_source(session, row)
        except table_versions.SnapshotMissing:
            view = row
        out[row.name] = (row, view)
    return out


def _schema_tables(view: Any) -> list[str] | None:
    tables = (getattr(view, "schema_cache", None) or {}).get("tables")
    return list(tables) if isinstance(tables, Mapping) and tables else None


#: 发布记录的两组，和影响面的两级同名：direct 写死的 SQL 用到的表，possible Agent 可能查询的表
CatalogVersions = dict[str, dict[str, dict[str, int]]]


def _pinned_group(value: Any) -> dict[str, dict[str, Any]] | None:
    """{源名: {表名: 版本}} 的一组；形状不对返回 None。"""
    if not isinstance(value, Mapping) or not all(isinstance(t, Mapping) for t in value.values()):
        return None
    return {str(k): dict(v) for k, v in value.items()}


def recorded_versions(recorded: Any) -> CatalogVersions | None:
    """发布时记下的目录版本，统一成 {"direct": {...}, "possible": {...}}。没记过返回 None。

    认两种写法：
    - 现在的写法：顶层只有 direct、possible 两个键，每组 {源名: {表名: 版本}}。
    - 可能涉及上线之前的写法：{源名: {表名: 版本}}，只记了写死的 SQL 用到的表——整个算 direct，possible 为空：
      那时没记 Agent 可能查询的表，不知道当时是哪一版，不能说它们变了。
    两种写法靠第二层分得开：旧写法第二层是版本号（整数），新写法第二层是 {表名: 版本}。源名可以叫 direct，
    所以不能只看顶层的键。
    """
    if not isinstance(recorded, Mapping):
        return None
    if recorded and set(recorded) <= set(IMPACTS):
        groups = {k: _pinned_group(recorded.get(k, {})) for k in IMPACTS}
        if all(g is not None for g in groups.values()):
            return {k: g or {} for k, g in groups.items()}
    legacy = _pinned_group(recorded)
    return {"direct": legacy, "possible": {}} if legacy is not None else None


async def catalog_versions_for(session: AsyncSession, spec: Any) -> CatalogVersions | None:
    """发布时要记下的目录版本：{"direct": {源名: {表名: 版本}}, "possible": {源名: {表名: 版本}}}。

    - direct：调用工具节点写死的 SQL 用到的表；还没有目录的表记 0。记 0 是有意的：发布之后有人给这张表补了
      目录，同样算「自发布以来有变化」。
    - possible：Agent 节点、协作成员绑定了某个源的查询工具（影响面里的「可能涉及」）。SQL 运行时才写，看不出用哪
      张表，于是记下这个源里**所有有目录的表**；direct 里已经有的不重复记。发布时还没有目录的表不逐张记 0（一个
      库几百张表），比对时「之后新建了目录」照样算变化（catalog_drift）。源里一张有目录的表都没有时记一个空的
      {}，表示这个源在看着。

    找不到的源（停用、删了）不记：没有目录可比，正式运行时工具自己会报源不存在。读库出错时返回 None（和没记过
    一样，不提醒）——记不下版本不该挡住发布。
    """
    from app.data import catalog

    try:
        possible_names = agent_sources(spec)
        sources = await _sources_by_name(session, [*graph_sql_tables(spec).keys(), *possible_names])
        tables_by_source = {name: _schema_tables(view) for name, (_, view) in sources.items()}
        used = graph_sql_tables(spec, {k: v for k, v in tables_by_source.items() if v is not None})
        entries_of: dict[str, dict[str, Any]] = {}

        async def entries(name: str) -> dict[str, Any]:
            if name not in entries_of:
                entries_of[name] = await catalog.read_catalog(session, sources[name][0].id)
            return entries_of[name]

        direct: dict[str, dict[str, int]] = {}
        for name, tables in sorted(used.items()):
            if name in sources:
                found = await entries(name)
                direct[name] = {t: (found[t].version if t in found else 0) for t in sorted(tables)}
        possible: dict[str, dict[str, int]] = {}
        for name in sorted(possible_names):
            if name in sources:
                pinned = direct.get(name, {})
                possible[name] = {t: e.version for t, e in sorted((await entries(name)).items()) if t not in pinned}
        return {"direct": direct, "possible": possible}
    except Exception:  # noqa: BLE001
        logger.exception("记录发布时的目录版本失败，本次不记")
        return None


def _label(entry: Any) -> str | None:
    """表现在的中文名；没有或被驳回时为 None。"""
    label = (entry.notes.get("label") if entry else None) or {}
    return label.get("value") if isinstance(label, dict) and label.get("status") != "rejected" else None


async def catalog_drift(session: AsyncSession, recorded: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """发布时记下的目录版本和现在比：变了的表 [{source, source_id, table, label, published, current, impact}]。

    impact 是这张表属于哪一组（direct 直接引用 / possible Agent 可能查询的），direct 在前，同组按源名、表名。
    possible 的源里发布时没记、现在有了目录的表也算（published 为 0），direct 里已有的表不重复报。

    没记过（这个功能之前发布的版本）返回空列表——不知道当时是哪一版，不能说它变了；只记了直接引用的旧记录
    同理不报可能涉及（recorded_versions）。现在找不到的源跳过（理由同 catalog_versions_for）。label 是表现在的
    中文名（没有或被驳回时为 None）。读库出错返回空列表：提醒是附加的。
    """
    from app.data import catalog

    groups = recorded_versions(recorded)
    if not groups or not any(groups.values()):
        return []
    try:
        sources = await _sources_by_name(session, [*groups["direct"], *groups["possible"]])
        entries_of: dict[str, dict[str, Any]] = {}

        async def entries(name: str) -> dict[str, Any]:
            if name not in entries_of:
                entries_of[name] = await catalog.read_catalog(session, sources[name][0].id)
            return entries_of[name]

        out: list[dict[str, Any]] = []
        for impact in IMPACTS:
            for name in sorted(groups[impact]):
                if name not in sources:
                    continue
                pinned = dict(groups[impact][name])
                found = await entries(name)
                if impact == "possible":
                    # 发布时没有目录、之后新建了的表：记录里没有它，按发布时「尚无目录」算
                    skip = groups["direct"].get(name, {})
                    pinned.update({t: 0 for t in found if t not in pinned and t not in skip})
                for table in sorted(pinned):
                    then = pinned[table]
                    entry = found.get(table)
                    now = entry.version if entry else 0
                    if isinstance(then, int) and then == now:
                        continue
                    out.append({"source": name, "source_id": sources[name][0].id, "table": table,
                                "label": _label(entry), "published": then if isinstance(then, int) else None,
                                "current": now, "impact": impact})
        return out
    except Exception:  # noqa: BLE001
        logger.exception("比对发布时的目录版本失败，本次不提醒")
        return []


__all__ = ["IMPACTS", "CatalogVersions", "agent_bindings", "agent_sources", "catalog_drift", "catalog_versions_for",
           "graph_sql_tables", "recorded_versions", "sql_table_keys", "table_impact", "template_refs", "tool_queries"]
