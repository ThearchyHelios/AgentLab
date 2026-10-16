"""查看的对象之一：数据表（look 文档 §2.4 D2、D3、D6）。

- overview：每个库一行（data/schema_view.overview_line），外加「现有工作流用到的表」只列表名；
- resolve：点名直取，认全名、只写表名、大小写不同、「库名.表名」（catalog.resolve_table_name）；只有一个候选库时可以不写库名；
- pick：候选库的表加起来不超过 PICK_LIMIT 张就全给，否则用挑选模型按需求从表目录（表名｜中文名｜粒度）里挑；候选超过
  PREFILTER_LIMIT 张（几千张表的库）时先按需求检索一遍（schema_view.search_tables，BM25），挑选模型只看前 PREFILTER_LIMIT
  个命中、工作流已用到的表、命中表的一跳关联表（next-round 03 A2）；
- 按需加入：点名或挑中探查范围外的表（对象索引里有、schema_cache 里没有），当场只读反射这一张、落盘、留痕
  （source_objects.add_to_scope：谁、哪一轮、哪一次查看、需求原文），每次查看最多 ON_DEMAND_PER_LOOK 张，超了照实说；
  look 记录里记 added_to_scope。读到的结构这一轮接着用（换成带新缓存的源），下一轮、运行时、SQL 检查、目录都看得到；
- detail：沿关系图补上两跳以内的中间表（合计不超过 CONTEXT_TABLE_LIMIT），每张表给全部字段和数据目录（遮罩列只给
  列名和类型）、剖析事实（行数、日期范围，附「只供写查询参考」），再给表之间的连接条件和连接路径；超过字数上限的表只给
  字段名。

表结构、目录、剖析事实、范围外的对象清单在开流之前读好（load）：用单独的会话读，读不到就当没有，不挡生成。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.data import catalog, introspect, schema_view
from app.data.catalog import INFERRED_MARK, JoinEdge
from app.data.names import name_key
from app.look.base import (
    CONTEXT_TABLE_LIMIT, NEIGHBOR_LIMIT, ON_DEMAND_PER_LOOK, PICK_LIMIT, PREFILTER_LIMIT, LookTarget,
)
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
#: 有对象索引的源（探查范围以外的表有名字、可以按需加入）
OUT_OF_SCOPE_PROMPT = ("（{source} 共 {total} 张表，探查范围内 {explored} 张；用户说的表在这里找不到时，点名 look 它，或者用 "
                       "db_schema__{source} 按 keyword 查：范围外的表会按需加入，别猜）")
ADDED_PROMPT = "按需加入探查范围：{tables}（在探查范围外，这次当场读了结构）"
ADD_CAPPED_PROMPT = "一次查看最多按需加入 {limit} 张探查范围外的表，{tables} 这次没有加入：下一次 look 点名再看"
ADD_FAILED_PROMPT = "{table} 在探查范围外，按需加入时没能读到结构：{why}"
PREFILTERED_PROMPT = "数据源「{source}」（共 {total} 张表，按需求预筛出 {n} 张，探查范围外的标出来了）："
OUTSIDE_MARK_PROMPT = "（探查范围外，选中后按需加入）"
INDEX_MORE_PROMPT = "…另有 {n} 张没有列出：点名查看，或者换个说法写 need 再 look"
INFERRED_PROMPT = ("标「推断，未确认」的说明和连接条件是按列名、数据库注释或模型推断出来的，没有人确认过：照抄前对照字段"
                   "核实")
INDEX_HEAD_PROMPT = "数据源「{source}」（共 {total} 张表）："
INDEX_PARTIAL_PROMPT = "数据源「{source}」（共 {total} 张表，只探查了 {explored} 张，下面只列探查到的）："
INDEX_SCOPED_PROMPT = ("数据源「{source}」（共 {total} 张表，探查范围内 {explored} 张，下面只列范围内的；范围外的点名查看时"
                       "按需加入）：")
PICK_WHAT_PROMPT = "数据表"
PICK_LINE_PROMPT = "每行一张表：表名｜中文名｜粒度"


@dataclass
class SourcePlan:
    """一个候选库：表结构、原样的目录（含驳回项：关系图要靠驳回项压住同一条关系的现推结果）、现有工作流用到的表。"""

    source: Any
    entries: dict[str, catalog.CatalogEntry] = field(default_factory=dict)
    #: 现有工作流 SQL 里查过的表（schema_cache 的键）
    seeds: list[str] = field(default_factory=list)
    #: 剖析事实 {表名: facts}（catalog_profile_jobs.last_facts）：表详情里附一节
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 对象索引里探查范围外的对象（source_objects.outside_objects）：检索、点名时用，点到了按需加入
    outside: list[Any] = field(default_factory=list)
    _graph: dict[str, list[JoinEdge]] | None = None
    _search: Any = None
    _outside_keys: dict[str, Any] | None = None

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
    def scoped(self) -> bool:
        """有对象索引（按探查范围物化的 schema_cache）：范围外的表有名字、可以按需加入。"""
        return isinstance((getattr(self.source, "schema_cache", None) or {}).get("scope"), dict)

    def outside_of(self, name: str) -> Any | None:
        """对象索引里范围外的这个对象（按 name_key 比，写了 schema 前缀的只看表名）。"""
        if self._outside_keys is None:
            self._outside_keys = {name_key(o.name): o for o in self.outside}
        return self._outside_keys.get(name_key(name.rsplit(".", 1)[-1]))

    def search(self) -> Any:
        """这个源的检索（第一次要预筛时才建）。"""
        if self._search is None:
            self._search = schema_view.TableSearch(self.source, self.notes, self.outside)
        return self._search

    def replace_cache(self, cache: dict[str, Any], added: list[str]) -> None:
        """按需加入以后：换成带新 schema_cache 的源（不改调用方会话里的数据源对象），关系图、检索重建，加进来的不再算范围外。"""
        base = self.source._base if isinstance(self.source, _WithCache) else self.source
        self.source = _WithCache(base, cache)
        keys = {name_key(n) for n in added}
        self.outside = [o for o in self.outside if name_key(o.name) not in keys]
        self._graph = self._search = self._outside_keys = None

    def outside_qualified(self, name: str) -> str:
        schema = (getattr(self.source, "schema_cache", None) or {}).get("schema")
        return f"{schema}.{name}" if schema else name

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


class _WithCache:
    """数据源换上一份新的 schema_cache（按需加入以后）：别的属性照原来的。不是 ORM 对象，改它不会被调用方的会话写回库。"""

    def __init__(self, base: Any, cache: dict[str, Any]) -> None:
        self._base = base
        self.schema_cache = cache

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


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

    def __init__(self, plans: list[SourcePlan], *, picker: Picker | None = None,
                 trail: Mapping[str, Any] | None = None) -> None:
        self.plans = plans
        self.picker = picker
        #: 按需加入的留痕：{actor, conversation_id}（开流前定下）；这一次查看的需求原文、编号由 begin 记
        self.trail = dict(trail or {})
        self._need = ""
        self._ref = ""
        #: 这一次查看里已经按需加入了几张、加了哪些（{第几个库: [表名]}）、没加成的（{(库, 点名): 说法}）
        self._added_now = 0
        self._added: dict[int, list[str]] = {}
        self._notes: list[str] = []
        self._prefiltered: dict[int, int] = {}
        self._last_index: dict[tuple[int, ...], str] = {}

    @classmethod
    async def load(cls, sources: list[Any], *, graph: Mapping[str, Any] | None = None,
                   picker: Picker | None = None, catalog_view: Any = None,
                   trail: Mapping[str, Any] | None = None) -> TableKind:
        """读各源的数据目录、剖析事实、探查范围外的对象清单。graph 是现有工作流（改图时的原图、追问时上一轮的图），概览里
        列出它查过的表。trail 是按需加入的留痕（谁、哪一轮对话）：不给时谁取这次请求的身份。

        读哪一份目录由 catalog_view 定（data/catalog_view.py：当前目录、目录快照、关；只用已核实的项），不给是当前目录。
        查看结果里的目录和运行时 db_schema 给模型看的、自查用的是同一份——评测的「目录对照」量的才是整份贡献。"""
        from app.data.catalog_view import CatalogView, read

        view = CatalogView.parse(catalog_view)
        seeds = graph_tables(graph, sources)
        plans = [SourcePlan(source=s, entries=await read(view, s) if introspect.table_names(s) else {},
                            seeds=seeds.get(s.name, [])) for s in sources]
        await _load_extras(plans)
        trail = dict(trail or {})
        if "actor" not in trail:
            from app.api.deps import current_principal

            trail["actor"] = current_principal().username
        return cls(plans, picker=picker, trail=trail)

    def begin(self, *, need: str, ref: str) -> None:
        """一次查看开始（LookService 调）：按需加入的上限按这一次算，留痕记这次的需求和编号。"""
        self._need, self._ref = need, ref
        self._added_now = 0
        self._added = {}
        self._notes = []
        self._prefiltered = {}
        # 交回给模型点名的那份预筛目录只对这一次的需求有效
        self._last_index = {}

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
        """交回给模型点名的表目录。这一次查看预筛过的，交回预筛的那一份（几千张表的整份目录塞不进来）；没预筛过、表又太多
        的只列前 PREFILTER_LIMIT 张，写明另有几张。"""
        candidates = self._candidates(sources)[0]
        if (cached := self._last_index.get(tuple(candidates))) is not None:
            return cached
        blocks = []
        for i in candidates:
            p = self.plans[i]
            head = (INDEX_HEAD_PROMPT if not p.unexplored else INDEX_SCOPED_PROMPT if p.scoped
                    else INDEX_PARTIAL_PROMPT).format(source=p.name, total=p.total, explored=p.explored)
            names = list(p.tables)
            body = schema_view.table_index(p.source, p.notes, tables=names[:PREFILTER_LIMIT])
            if len(names) > PREFILTER_LIMIT:
                body += "\n" + INDEX_MORE_PROMPT.format(n=len(names) - PREFILTER_LIMIT)
            blocks.append(f"{head}\n{body}")
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

    def _find_outside(self, source: str | None, name: str) -> tuple[int, Any] | None:
        """点名的表在哪个候选库的探查范围外（对象索引里有）：(第几个库, 对象)。规则同 _find。"""
        if source is not None:
            candidates = [i for i, p in enumerate(self.plans) if p.name == source]
        elif (hit := self._prefixed(name)) is not None:
            candidates, name = [hit[0]], hit[1]
        else:
            candidates = [i for i, p in enumerate(self.plans) if p.tables or p.outside]
        for i in candidates:
            obj = self.plans[i].outside_of(name)
            if obj is not None:
                return i, obj
        return None

    async def _add(self, wanted: dict[int, list[str]]) -> dict[tuple[int, str], TableRef]:
        """按需加入（每次查看最多 ON_DEMAND_PER_LOOK 张，超了的照实说）：{(第几个库, 对象名): 加进来以后的 TableRef}。读不到
        结构、加不进去的记进这一次的说明。"""
        from app.data import source_objects as so

        out: dict[tuple[int, str], TableRef] = {}
        capped: list[str] = []
        for i, names in wanted.items():
            p = self.plans[i]
            room = max(ON_DEMAND_PER_LOOK - self._added_now, 0)
            take, rest = names[:room], names[room:]
            capped += [f"{p.name} 的 {n}" for n in rest]
            if not take:
                continue
            source_id = getattr(p.source, "id", None)
            if not source_id:
                continue
            try:
                got = await so.add_to_scope(source_id, take, trail=so.Trail(
                    via=so.VIA_LOOK, actor=self.trail.get("actor"), conversation_id=self.trail.get("conversation_id"),
                    ref=self._ref, need=self._need))
            except Exception as e:  # noqa: BLE001 - 按需加入是辅助步骤，失败只是少一张表
                from app.core.errors import explain

                logger.exception("按需加入失败（数据源 %s）", p.name)
                self._notes += [ADD_FAILED_PROMPT.format(table=n, why=explain(e)[0]) for n in take]
                continue
            self._added_now += len(got.added)
            for name, why in {**got.failed, **got.refused}.items():
                self._notes.append(ADD_FAILED_PROMPT.format(table=name, why=why))
            if got.cache is not None and got.added:
                p.replace_cache(got.cache, got.added)
                self._added.setdefault(i, []).extend(got.added)
            for name in got.added + got.already:
                key = catalog.resolve_table_name(p.source.schema_cache, name)
                if key:
                    out[(i, name_key(name))] = TableRef(i, key)
        if capped:
            self._notes.append(ADD_CAPPED_PROMPT.format(limit=ON_DEMAND_PER_LOOK, tables="、".join(capped)))
        return out

    async def resolve(self, targets: list[LookTarget]) -> tuple[list[TableRef], list[LookTarget]]:
        found: list[TableRef] = []
        missing: list[LookTarget] = []
        single = len([p for p in self.plans if p.tables or p.outside]) == 1
        outside: list[tuple[LookTarget, int, Any]] = []
        for t in targets:
            source = t.get("source") or None
            if source is not None and source not in {p.name for p in self.plans}:
                missing.append(t)
                continue
            where = None if single and source is None else source
            ref = self._find(where, str(t.get("name") or ""))
            if ref is None:
                hit = self._find_outside(where, str(t.get("name") or ""))
                if hit is not None:
                    outside.append((t, hit[0], hit[1]))
                else:
                    missing.append(t)
            elif ref not in found:
                found.append(ref)
        if outside:
            wanted: dict[int, list[str]] = {}
            for _t, i, obj in outside:
                if obj.name not in wanted.setdefault(i, []):
                    wanted[i].append(obj.name)
            added = await self._add(wanted)
            for t, i, obj in outside:
                ref = added.get((i, name_key(obj.name)))
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
        outside = sum(len(self.plans[i].outside) for i in candidates)
        if len(pool) + outside <= PICK_LIMIT and not outside:
            return pool, None
        if self.picker is None:
            return [], "没有可用的挑选模型"
        if len(pool) + outside > PREFILTER_LIMIT:
            index = self._prefiltered_index(need, candidates)
        else:
            index = "\n\n".join(self._full_index(self.plans[i]) for i in candidates)
        self._last_index[tuple(candidates)] = index
        used = "；".join(f"数据源「{p.name}」的 " + "、".join(p.qualified(t) for t in p.seeds)
                        for p in (self.plans[i] for i in candidates) if p.seeds)
        try:
            picks = await self.picker.pick(need=need, what=PICK_WHAT_PROMPT, line=PICK_LINE_PROMPT, index=index,
                                           used=used)
        except PickFailed as e:
            return [], str(e)
        names = {self.plans[i].name for i in candidates}
        out: list[TableRef] = []
        wanted: dict[int, list[str]] = {}
        order: list[tuple[str, Any]] = []
        for source, name in picks:
            if len(candidates) == 1:
                # 只有一个库要挑时不看模型写的库名（写错了也不要紧）
                where: str | None = self.plans[candidates[0]].name
            elif source in names or source is None:
                where = source
            elif self._prefixed(name):
                where = None
            else:
                # 写了一个不在候选里的库名：表名前带着库名的照样认，否则丢掉
                continue
            ref = self._find(where, name)
            if ref is not None and ref.plan in candidates:
                if not any(r.plan == ref.plan and r.key == ref.key for r in out):
                    out.append(TableRef(ref.plan, ref.key, "picked"))
                    order.append(("in", out[-1]))
                continue
            hit = self._find_outside(where, name)
            if hit is not None and hit[0] in candidates and hit[1].name not in wanted.setdefault(hit[0], []):
                wanted[hit[0]].append(hit[1].name)
                order.append(("out", (hit[0], name_key(hit[1].name))))
        if any(wanted.values()):
            added = await self._add({i: n for i, n in wanted.items() if n})
            ranked: list[TableRef] = []
            for kind, item in order:
                ref = item if kind == "in" else added.get(item)
                if ref is not None and not any(r.plan == ref.plan and r.key == ref.key for r in ranked):
                    ranked.append(TableRef(ref.plan, ref.key, "picked"))
            out = ranked
        if picks and not out:
            return [], "挑出的表在表结构中都不存在"
        return out[:PICK_LIMIT], None

    def _full_index(self, p: SourcePlan) -> str:
        """整份表目录（候选不多时交给挑选模型的那一份）：范围内的每张一行，范围外的另起一行、标出来。"""
        head = (INDEX_PARTIAL_PROMPT if p.unexplored and not p.outside else INDEX_HEAD_PROMPT).format(
            source=p.name, total=p.total, explored=p.explored)
        lines = [schema_view.table_index(p.source, p.notes)]
        lines += [self._outside_line(p, o) for o in p.outside]
        return f"{head}\n" + "\n".join(x for x in lines if x)

    def _outside_line(self, p: SourcePlan, obj: Any) -> str:
        comment = f"｜{obj.comment}" if getattr(obj, "comment", None) else ""
        return f"{p.outside_qualified(obj.name)}{comment}{OUTSIDE_MARK_PROMPT}"

    def _prefiltered_index(self, need: str, candidates: list[int]) -> str:
        """几千张表的库：按需求检索一遍，挑选模型只看前 PREFILTER_LIMIT 个命中（各库合起来按得分排）、工作流已用到的表、
        命中表的一跳关联表（最多 NEIGHBOR_LIMIT 张）。检索什么都没命中时退回各库表结构里的前几张。"""
        hits: list[tuple[float, int, Any]] = []
        for i in candidates:
            for hit in self.plans[i].search().search(need, limit=PREFILTER_LIMIT):
                hits.append((hit.score, i, hit))
        hits.sort(key=lambda h: (-h[0], h[1], h[2].name))
        top = hits[:PREFILTER_LIMIT]
        if not top:
            per = max(PREFILTER_LIMIT // max(len(candidates), 1), 1)
            top = [(0.0, i, schema_view.SearchHit(n, 0.0, True)) for i in candidates
                   for n in list(self.plans[i].tables)[:per]]
        blocks = []
        for i in candidates:
            p = self.plans[i]
            mine = [h for _s, j, h in top if j == i]
            inside = [h.name for h in mine if h.in_scope]
            picked = list(dict.fromkeys([*p.seeds, *inside]))
            neighbors: list[str] = []
            graph = p.graph() if inside else {}
            for name in inside:
                for edge in graph.get(name, ()):
                    if edge.to_table in p.tables and edge.to_table not in picked and edge.to_table not in neighbors:
                        neighbors.append(edge.to_table)
                if len(neighbors) >= NEIGHBOR_LIMIT:
                    break
            shown = picked + neighbors[:NEIGHBOR_LIMIT]
            outside = [h.name for h in mine if not h.in_scope]
            if not shown and not outside:
                continue
            self._prefiltered[i] = len(shown) + len(outside)
            lines = [schema_view.table_index(p.source, p.notes, tables=shown)] if shown else []
            for name in outside:
                obj = p.outside_of(name)
                if obj is not None:
                    lines.append(self._outside_line(p, obj))
            head = PREFILTERED_PROMPT.format(source=p.name, total=p.total, n=len(shown) + len(outside))
            blocks.append(head + "\n" + "\n".join(x for x in lines if x))
        return "\n\n".join(blocks)

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
                full = schema_view.table_detail(p.source, key, catalog_notes=notes, profile=p.facts)
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
            if self._added.get(i):
                lines.append("  " + ADDED_PROMPT.format(tables="、".join(p.qualified(t) for t in self._added[i])))
            if p.unexplored:
                prompt = OUT_OF_SCOPE_PROMPT if p.scoped else UNEXPLORED_PROMPT
                lines.append("  " + prompt.format(source=p.name, total=p.total, explored=p.explored))
            blocks.append(self._record(p, named=named, picked=picked, joined=joined,
                                       missing=[str(t.get("name") or "") for t in missing
                                                if self._missing_in(t) == i]))
        if briefed:
            lines.append(BRIEF_NOTE_PROMPT)
        if capped:
            lines.append(CAPPED_PROMPT.format(limit=CONTEXT_TABLE_LIMIT))
        lines += self._notes
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
        index = next(i for i, q in enumerate(self.plans) if q is p)
        if self._added.get(index):
            out["added_to_scope"] = [p.qualified(t) for t in self._added[index]]
        if index in self._prefiltered:
            out["prefiltered"] = self._prefiltered[index]
        return out


async def _load_extras(plans: list[SourcePlan]) -> None:
    """各源的剖析事实、探查范围外的对象清单：一个短会话读完。读不到就当没有（少一节剖析事实、少几个可按需加入的名字），
    不挡生成。上传表格、记录表没有对象索引；探查范围以内就是全部的源（没有范围外的对象）不读清单。"""
    from app.data import catalog_profile_jobs, source_objects
    from app.db.base import SessionLocal

    try:
        async with SessionLocal() as session:
            for p in plans:
                source_id = getattr(p.source, "id", None)
                if not source_id:
                    continue
                if p.tables:
                    p.facts = await catalog_profile_jobs.last_facts(session, source_id)
                scope = (getattr(p.source, "schema_cache", None) or {}).get("scope")
                if isinstance(scope, dict) and scope.get("out_of_scope"):
                    p.outside = await source_objects.outside_objects(session, p.source)
    except Exception:  # noqa: BLE001
        logger.exception("读剖析事实、探查范围外的对象清单失败，这一轮查看不给这两样")
