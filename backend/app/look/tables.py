"""查看的对象之一：数据表（look 文档 §2.4 D2、D3、D6）。

- overview：每个库一行（data/schema_view.overview_line），外加「现有工作流用到的表」只列表名；
- resolve：点名直取，认全名、只写表名、大小写不同、「库名.表名」（catalog.resolve_table_name）；只有一个候选库时可以不写库名；
- pick：候选库的表加起来不超过 PICK_LIMIT 张就全给，否则用挑选模型按需求从表目录（表名｜中文名｜粒度）里挑；
- detail：沿关系图补上两跳以内的中间表（合计不超过 CONTEXT_TABLE_LIMIT），每张表给全部字段和数据目录（遮罩列只给
  列名和类型），再给表之间的连接条件和连接路径；超过字数上限的表只给字段名。

表结构和目录在开流之前读好（load）：目录用单独的会话读，读不到就当没有，不挡生成。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.data import catalog, introspect, schema_view
from app.data.catalog import INFERRED_MARK, JoinEdge
from app.look.base import CONTEXT_TABLE_LIMIT, PICK_LIMIT, LookTarget
from app.look.pick import Picker, PickFailed

logger = logging.getLogger(__name__)

# ==========================================================================
# 写给模型的文字
# ==========================================================================

OVERVIEW_HEAD_PROMPT = "已接入的数据源（要看表结构时用 look，kind 写 table）："
OVERVIEW_SEEDS_PROMPT = "现有工作流用到的表：{tables}（来自现有工作流，字段要用时 look）"
SOURCE_HEAD_PROMPT = "{source}（{kind}）：{lead}"
LEAD_NAMED_PROMPT = "点名查看 {n} 张"
LEAD_PICKED_PROMPT = "按需求从 {total} 张表中挑出 {n} 张"
LEAD_ALL_PROMPT = "候选只有 {n} 张表，全部给出"
LEAD_JOINED_PROMPT = "另为连接补上 {n} 张"
JOINED_MARK_PROMPT = "（为连接补上）"
CAPPED_PROMPT = "一次最多查看 {limit} 张表，点名的只取了前 {limit} 张"
BRIEF_NOTE_PROMPT = "（这一次查看的字数超过上限，后面的表只给了字段名）"
MISSING_PROMPT = "没找到：{items}（表结构里没有，可能没探查到；别拿名字相近的表顶替）"
MISSING_SOURCE_PROMPT = "没找到数据源「{source}」：不在这一轮可用的数据源里"
UNEXPLORED_PROMPT = ("（{source} 共 {total} 张表，只探查了 {explored} 张；用户说的表在这里找不到时，先用 db_schema__{source} "
                     "查，也查不到就查数据字典（information_schema.columns，SQLite 用 sqlite_master），别猜）")
INFERRED_PROMPT = ("标「推断，未确认」的说明和连接条件是按列名、数据库注释或模型推断出来的，没有人确认过：照抄前对照字段"
                   "核实")
INDEX_HEAD_PROMPT = "数据源「{source}」（共 {total} 张表）："
INDEX_PARTIAL_PROMPT = "数据源「{source}」（共 {total} 张表，只探查了 {explored} 张，下面只列探查到的）："
PICK_WHAT_PROMPT = "数据表"
PICK_LINE_PROMPT = "每行一张表：表名｜中文名｜粒度"


@dataclass
class SourcePlan:
    """一个候选库：表结构、原样的目录（含驳回项：关系图要靠驳回项压住同一条关系的现推结果）、现有工作流用到的表。"""

    source: Any
    entries: dict[str, catalog.CatalogEntry] = field(default_factory=dict)
    #: 现有工作流 SQL 里查过的表（schema_cache 的键）
    seeds: list[str] = field(default_factory=list)
    _graph: dict[str, list[JoinEdge]] | None = None

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def tables(self) -> dict[str, Any]:
        return (getattr(self.source, "schema_cache", None) or {}).get("tables") or {}

    @property
    def explored(self) -> int:
        return len(self.tables)

    @property
    def total(self) -> int:
        """这个源一共几张表（含视图）：探查时数据库报的总数。老缓存没记总数时按探查到的算。"""
        raw = (getattr(self.source, "schema_cache", None) or {}).get("total")
        return max(raw if isinstance(raw, int) and not isinstance(raw, bool) else 0, self.explored)

    @property
    def unexplored(self) -> int:
        """探查截断了的话还有几张没探查到；没截断为 0（反射失败跳过的表是探查了、读不到，不算）。"""
        truncated = (getattr(self.source, "schema_cache", None) or {}).get("truncated")
        return max(self.total - self.explored, 0) if truncated else 0

    @property
    def raw_notes(self) -> dict[str, dict[str, Any]]:
        return {t: e.notes for t, e in self.entries.items()}

    @property
    def notes(self) -> dict[str, dict[str, Any]]:
        """给模型看的目录：去掉驳回项。"""
        return {t: catalog.visible_notes(e.notes) for t, e in self.entries.items()}

    @property
    def catalog_tables(self) -> int:
        """有数据目录的表数：去掉驳回项之后还有内容的。"""
        return sum(1 for e in self.entries.values() if catalog.visible_notes(e.notes))

    def graph(self) -> dict[str, list[JoinEdge]]:
        """关系图，第一次要用时才推。推不出来就当没有：少了连接条件只是少一个提示。"""
        if self._graph is None:
            try:
                self._graph = schema_view.relation_graph(self.source, self.raw_notes)
            except Exception:  # noqa: BLE001
                logger.exception("数据源 %s 的关系图推不出来，这次查看不给连接条件", self.name)
                self._graph = {}
        return self._graph

    def qualified(self, key: str) -> str:
        return schema_view.qualifier(self.source)(key)


@dataclass(frozen=True)
class TableRef:
    """查看结果里的一张表：第几个候选库、schema_cache 的键、怎么来的（named 点名 / picked 按需求挑出）。"""

    plan: int
    key: str
    how: str = "named"


def graph_tables(graph: Mapping[str, Any] | None, sources: list[Any]) -> dict[str, list[str]]:
    """现有工作流里查库节点的 SQL 用到的表：{源名: [schema_cache 的键]}，按出现顺序。

    只认调用工具节点上写死的 SQL（db_query__<源>）；Agent 运行时自己写的 SQL 图里看不到。哪些节点算写死的查询
    以 sqlcheck.tool_query 为准（catalog_impact.tool_queries，和发布记录、SQL 检查同一份）：换成别的类型的节点
    配置里残留的 tool、args 不算。节点认两种写法：画布的 {data: {config}} 和 GraphSpec 的 {config}。SQL 解析
    不了的跳过。发布前修复、升级证据由系统代为查看的就是这些表（look 文档 D5）。
    """
    from app.data.catalog_impact import tool_queries
    from app.engine.evidence import sql_tables

    by_name = {getattr(s, "name", None): s for s in sources}
    out: dict[str, list[str]] = {}
    nodes = (graph or {}).get("nodes") or []
    for _, source_name, sql in tool_queries(n for n in nodes if isinstance(n, dict)):
        source = by_name.get(source_name)
        if source is None:
            continue
        try:
            names = sql_tables(sql)
        except Exception:  # noqa: BLE001 - 认不出表就当没用到
            continue
        found = out.setdefault(source.name, [])
        for table in names:
            key = catalog.resolve_table_name(source.schema_cache, table)
            if key and key not in found:
                found.append(key)
    return {k: v for k, v in out.items() if v}


class TableKind:
    kind = "table"
    label = "表"

    def __init__(self, plans: list[SourcePlan], *, picker: Picker | None = None) -> None:
        self.plans = plans
        self.picker = picker

    @classmethod
    async def load(cls, sources: list[Any], *, graph: Mapping[str, Any] | None = None,
                   picker: Picker | None = None) -> TableKind:
        """读各源的数据目录。graph 是现有工作流（改图时的原图、追问时上一轮的图），概览里列出它查过的表。"""
        from app.tools.datasource import _catalog_of

        seeds = graph_tables(graph, sources)
        plans = [SourcePlan(source=s, entries=await _catalog_of(s) if introspect.table_names(s) else {},
                            seeds=seeds.get(s.name, [])) for s in sources]
        return cls(plans, picker=picker)

    # ---------------------------------------------------------------- 概览、目录

    async def overview(self) -> list[str]:
        if not self.plans:
            return []
        lines = [OVERVIEW_HEAD_PROMPT]
        lines += [schema_view.overview_line(p.source, catalog_tables=p.catalog_tables) for p in self.plans]
        seeds = [f"{p.name} 的 " + "、".join(p.qualified(t) for t in p.seeds) for p in self.plans if p.seeds]
        if seeds:
            lines.append(OVERVIEW_SEEDS_PROMPT.format(tables="；".join(seeds)))
        return lines

    def _candidates(self, sources: list[str] | None) -> tuple[list[int], list[str]]:
        """候选库（在这一轮可用的范围里）和认不出的库名。sources 不写时为全部。"""
        if not sources:
            return [i for i, p in enumerate(self.plans) if p.tables], []
        by_name = {p.name: i for i, p in enumerate(self.plans)}
        found = [by_name[s] for s in sources if s in by_name]
        return list(dict.fromkeys(found)), [s for s in sources if s not in by_name]

    async def index(self, sources: list[str] | None) -> str:
        blocks = []
        for i in self._candidates(sources)[0]:
            p = self.plans[i]
            head = (INDEX_PARTIAL_PROMPT if p.unexplored else INDEX_HEAD_PROMPT).format(
                source=p.name, total=p.total, explored=p.explored)
            blocks.append(f"{head}\n{schema_view.table_index(p.source, p.notes)}")
        return "\n\n".join(blocks)

    # ---------------------------------------------------------------- 点名、挑选

    def _prefixed(self, name: str) -> tuple[int, str] | None:
        """「库名.表名」这种写法：(第几个库, 表名)。"""
        return next(((i, name[len(p.name) + 1:]) for i, p in enumerate(self.plans)
                     if name.startswith(p.name) and name[len(p.name):len(p.name) + 1] in (".", ":", "：")), None)

    def _find(self, source: str | None, name: str) -> TableRef | None:
        """一个点名 → 表。写了库名的只在那个库里找；表名前面带着库名的也认；都没写的按顺序在各库里找。"""
        if source is not None:
            candidates = [i for i, p in enumerate(self.plans) if p.name == source]
        elif (hit := self._prefixed(name)) is not None:
            candidates, name = [hit[0]], hit[1]
        else:
            candidates = [i for i, p in enumerate(self.plans) if p.tables]
        for i in candidates:
            key = catalog.resolve_table_name(self.plans[i].source.schema_cache, name)
            if key:
                return TableRef(i, key)
        return None

    async def resolve(self, targets: list[LookTarget]) -> tuple[list[TableRef], list[LookTarget]]:
        found: list[TableRef] = []
        missing: list[LookTarget] = []
        single = len([p for p in self.plans if p.tables]) == 1
        for t in targets:
            source = t.get("source") or None
            if source is not None and source not in {p.name for p in self.plans}:
                missing.append(t)
                continue
            ref = self._find(None if single and source is None else source, str(t.get("name") or ""))
            if ref is None:
                missing.append(t)
            elif ref not in found:
                found.append(ref)
        return found, missing

    async def pick(self, need: str, sources: list[str] | None) -> tuple[list[TableRef], str | None]:
        candidates, _ = self._candidates(sources)
        if not any(self.plans[i].tables for i in candidates):
            # 候选库一张表都没探查到：不是「按需求没挑出来」，照实说为什么是空的（和概览行同一句）
            bare = [self.plans[i] for i in candidates] if sources else self.plans
            if bare:
                return [], "；".join(f"数据源「{p.name}」{introspect.why_empty(p.source.schema_cache or {})}"
                                    for p in bare)
        pool = [TableRef(i, key, "picked") for i in candidates for key in self.plans[i].tables]
        if len(pool) <= PICK_LIMIT:
            return pool, None
        if self.picker is None:
            return [], "没有可用的挑选模型"
        index = "\n\n".join(
            f"{(INDEX_PARTIAL_PROMPT if p.unexplored else INDEX_HEAD_PROMPT).format(source=p.name, total=p.total, explored=p.explored)}\n"
            f"{schema_view.table_index(p.source, p.notes)}" for p in (self.plans[i] for i in candidates))
        used = "；".join(f"数据源「{p.name}」的 " + "、".join(p.qualified(t) for t in p.seeds)
                        for p in (self.plans[i] for i in candidates) if p.seeds)
        try:
            picks = await self.picker.pick(need=need, what=PICK_WHAT_PROMPT, line=PICK_LINE_PROMPT, index=index,
                                           used=used)
        except PickFailed as e:
            return [], str(e)
        names = {self.plans[i].name for i in candidates}
        out: list[TableRef] = []
        for source, name in picks:
            if len(candidates) == 1:
                # 只有一个库要挑时不看模型写的库名（写错了也不要紧）
                ref = self._find(self.plans[candidates[0]].name, name)
            elif source in names or source is None:
                ref = self._find(source, name)
            else:
                # 写了一个不在候选里的库名：表名前带着库名的照样认，否则丢掉
                ref = self._find(None, name) if self._prefixed(name) else None
            if ref is not None and ref.plan in candidates and not any(r.plan == ref.plan and r.key == ref.key
                                                                      for r in out):
                out.append(TableRef(ref.plan, ref.key, "picked"))
        if picks and not out:
            return [], "挑出的表在表结构中都不存在"
        return out[:PICK_LIMIT], None

    def key(self, obj: TableRef) -> str:
        return f"table:{self.plans[obj.plan].name}:{obj.key}"

    # ---------------------------------------------------------------- 详情

    async def detail(self, objects: list[TableRef], budget: int, *,
                     missing: list[LookTarget] = ()) -> tuple[str, list[dict[str, Any]]]:
        capped = len(objects) > CONTEXT_TABLE_LIMIT
        objects = objects[:CONTEXT_TABLE_LIMIT]
        by_plan: dict[int, list[TableRef]] = {}
        for ref in objects:
            by_plan.setdefault(ref.plan, []).append(ref)
        room = CONTEXT_TABLE_LIMIT - len(objects)
        lines: list[str] = []
        blocks: list[dict[str, Any]] = []
        used = 0
        briefed = False
        for i, refs in by_plan.items():
            p = self.plans[i]
            first = [r.key for r in refs]
            tables, routes = list(first), []
            if len(first) > 1 and room > 0:
                tables, routes = schema_view.complete(first, p.graph(), room)
                room -= len(tables) - len(first)
            joined = tables[len(first):]
            named = [r.key for r in refs if r.how == "named"]
            picked = [r.key for r in refs if r.how == "picked"]
            lead = []
            if named:
                lead.append(LEAD_NAMED_PROMPT.format(n=len(named)))
            if picked:
                # 候选不超过 PICK_LIMIT 张时没有挑，全部给出：照实说
                every = len(picked) == len(p.tables) and len(p.tables) <= PICK_LIMIT and not p.unexplored
                lead.append(LEAD_ALL_PROMPT.format(n=len(picked)) if every
                            else LEAD_PICKED_PROMPT.format(total=p.total, n=len(picked)))
            if joined:
                lead.append(LEAD_JOINED_PROMPT.format(n=len(joined)))
            lines.append(SOURCE_HEAD_PROMPT.format(source=p.name, kind=p.source.kind, lead="，".join(lead)))
            notes = p.raw_notes
            for key in tables:
                full = schema_view.table_detail(p.source, key, catalog_notes=notes)
                block = full.split("\n")
                if used + len(full) > budget:
                    block = [schema_view.table_brief(p.source, key)]
                    briefed = True
                used += sum(len(x) + 1 for x in block)
                mark = JOINED_MARK_PROMPT if key in joined else ""
                lines.append(f"  · {block[0]}{mark}")
                lines += [f"    {x}" for x in block[1:]]
            lines += schema_view.join_lines(p.source, p.graph(), tables, indent="  ")
            lines += schema_view.route_lines(p.source, routes, indent="  ")
            if p.unexplored:
                lines.append("  " + UNEXPLORED_PROMPT.format(source=p.name, total=p.total, explored=p.explored))
            blocks.append(self._record(p, named=named, picked=picked, joined=joined,
                                       missing=[str(t.get("name") or "") for t in missing
                                                if self._missing_in(t) == i]))
        if briefed:
            lines.append(BRIEF_NOTE_PROMPT)
        if capped:
            lines.append(CAPPED_PROMPT.format(limit=CONTEXT_TABLE_LIMIT))
        lines += self._missing_lines(missing, blocks, set(by_plan))
        text = "\n".join(lines)
        if INFERRED_MARK in text:
            text += "\n" + INFERRED_PROMPT
        return text, blocks

    def _missing_in(self, target: LookTarget) -> int | None:
        """没找到的点名落在哪个候选库（记录按库分块）：写了库名就是那个库，只有一个库时就是它。"""
        source = target.get("source")
        names = [i for i, p in enumerate(self.plans) if p.name == source] if source else \
            [i for i, p in enumerate(self.plans) if p.tables]
        return names[0] if len(names) == 1 else None

    def _missing_lines(self, missing: list[LookTarget], blocks: list[dict[str, Any]], shown: set[int]) -> list[str]:
        """没找到的点名：库认不出的说库，表认不出的说表；落在没给详情的库里的，另补一块记录。"""
        lines: list[str] = []
        tables: list[str] = []
        known = {p.name for p in self.plans}
        for t in missing:
            source = t.get("source")
            if source and source not in known:
                lines.append(MISSING_SOURCE_PROMPT.format(source=source))
                blocks.append({"kind": "table", "source": source, "source_id": None, "named": [], "picked": [],
                               "joined": [], "missing": [str(t["name"])] if t.get("name") else []})
                continue
            where = self._missing_in(t)
            label = f"{self.plans[where].name} 的 {t.get('name')}" if where is not None else str(t.get("name"))
            tables.append(label)
            if where is not None and where not in shown:
                shown.add(where)
                blocks.append(self._record(self.plans[where], named=[], picked=[], joined=[],
                                           missing=[str(m.get("name") or "") for m in missing
                                                    if self._missing_in(m) == where]))
        if tables:
            lines.append(MISSING_PROMPT.format(items="、".join(tables)))
        return lines

    def _record(self, p: SourcePlan, *, named: list[str], picked: list[str], joined: list[str],
                missing: list[str]) -> dict[str, Any]:
        """look 事件里一个库的那一块（look 文档 §2.5.7）。表名写全名。"""
        given = [*named, *picked, *joined]
        synced = getattr(p.source, "schema_synced_at", None)
        out: dict[str, Any] = {
            "kind": "table", "source": p.name, "source_id": getattr(p.source, "id", None),
            "named": [p.qualified(t) for t in named], "picked": [p.qualified(t) for t in picked],
            "joined": [p.qualified(t) for t in joined], "missing": missing, "total": p.total,
            "catalog_tables": p.catalog_tables,
            "schema_synced_at": synced.isoformat() if synced is not None else None,
            "catalog_versions": {p.qualified(t): p.entries[t].version for t in given if t in p.entries},
        }
        if p.unexplored:
            out["explored"] = p.explored
        return out
