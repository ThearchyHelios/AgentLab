"""助手建图时给模型看的数据源上下文：小库给详细摘要，大库先按需求挑表，再把挑中的表讲透（数据目录阶段 1B）。

**为什么要挑表。** 接进来的业务库常有上百张表。以前超过详细摘要的阈值（一个库超过 40 张表，或者几个库的摘要
加起来超过 6000 字）就整体退成「只列表名」，工具清单里也只列前 12 个表名：助手一个字段都看不到，只能照着表名
编字段、猜怎么连表，跑到查库那一步才「no such column」。现在大库先用一次轻量的模型调用，按这一轮的需求从
紧凑的表目录（每张表一行：表名｜中文名｜粒度）里挑出相关的表，再沿关系图补上连接要经过的中间表；挑中的表给
全部字段、数据目录和可以照抄进 SQL 的连接条件，其余表只列名字，让模型知道还有别的表可查。

**按数据源分三种（selected_by）。**

- all：放得下详细摘要的库，照旧每张表一行带字段，额外带上数据目录和表之间的连接条件；
- model：大库，挑表成功：挑中的表讲透，其余只列名字；
- fallback：大库，挑表失败（模型不可用、报错、超时、回复读不出、返回为空或返回的表都不存在），退回以前的
  「只列表名」。挑表只是让模型少猜，**绝不能挡住生成**：没有它，模型照旧可以先用 db_schema 查字段。

现有工作流（画布上改图时的原图、问数据追问时上一轮的图）的 SQL 里查过哪些表是确定的事实：大库里这些表一律
给全字段，挑表失败时也一样。

**一轮只挑一次。** 流式生成的自查修正、升级的修正轮都沿用同一份上下文（同一段 system 提示词），不重新挑表：
两次挑出来的表不一样，模型前后看到的结构就对不上。

**用法。** plan_context 读目录、按大小定每个源怎么给（不调模型，接口函数在开流之前调，因为请求的会话只在
那时可用）；pick_model 拿挑表用的模型（同一个助手模型，关掉思考、额度取小）；ContextPlan.resolve 挑表并组装
（流式生成在流里调，期间照常发心跳）；DatasourceContext 给出提示词片段（section）、工具清单里列出的可查对象
（listed）和告诉前端这一轮参考了哪些表的 context 操作（op）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from app.data import catalog
from app.data import introspect
from app.data.catalog import CARDINALITY_LABEL, INFERRED_MARK, JoinEdge
from app.providers.factory import ModelSpec, get_chat_model

logger = logging.getLogger(__name__)

# ==========================================================================
# 参数
# ==========================================================================

#: 详细摘要的字符预算。超了的库改为按需求挑表，字段让挑中的表带、其余的让模型用 db_schema 工具按需查。
#: 实测一个 53 对象的库带字段约 4000 字符，接三五个库就会把真正的需求淹掉——system prompt 里塞满 schema
#: 而挤掉用户想要什么，是本末倒置
DATASOURCE_BUDGET_CHARS = 6000
#: 详细摘要每个库最多列这么多对象（与 introspect.summary 的默认值一致）。超过就说明会截断：多出来的表模型
#: 根本看不见，那还不如挑表
DETAIL_TABLE_LIMIT = 40
#: 挑表最多挑几张（所有数据源合计，含现有工作流用到的表）
PICK_LIMIT = 12
#: 补上连接用的中间表之后，给全字段的表最多几张（所有数据源合计）
CONTEXT_TABLE_LIMIT = 16
#: 挑表调用的时限（秒）。挑表在生成之前、用户在等，宁可放弃挑表也不能让生成干等
PICK_TIMEOUT_S = 20.0
#: 挑表调用的输出额度：十几个表名的 JSON 用不了几百 token
PICK_MAX_TOKENS = 1024
#: 详细模式下附带的连接条件和数据目录的字符预算（所有数据源合计）。超了的不附，模型用 db_schema 查时照样看得到
CATALOG_BUDGET_CHARS = 6000
#: 一个源最多列多少条连接条件、多少条连接路径
JOIN_LINES_LIMIT = 40
PATH_LINES_LIMIT = 12

SelectedBy = Literal["all", "model", "fallback"]

# ==========================================================================
# 写给模型的文字（不上界面）
# ==========================================================================

SECTION_HEAD_PROMPT = "\n\n已接入的数据源（涉及取数时优先用它们，而不是让用户自己写 SQL 的代码节点）：\n"
#: 有源只列了名字时补的一句（以前紧凑模式的原话）
NAMES_ONLY_PROMPT = "  （对象较多，上面只列了名字。写 SQL 前用 db_schema 工具确认字段，别猜）"
INFERRED_PROMPT = ("  标「推断，未确认」的说明和连接条件是按列名、数据库注释或模型推断出来的，没有人确认过："
                   "照抄前对照字段核实，拿不准就先用 db_schema 工具查")
SQL_RULES_PROMPT = (
    "\n\n写 SQL 的硬性要求：\n"
    # 这三条都是真实踩过的：Oracle 上漏 schema 前缀直接 ORA-00942，
    # 写惯 MySQL 的模型会顺手写 LIMIT，而一条没有行数限制的查询能拖垮生产库
    "- 表名照抄上面的全名（含 schema 前缀），漏掉前缀在 Oracle 上会直接报 ORA-00942\n"
    "- 认准方言：Oracle 用 FETCH FIRST n ROWS ONLY，不是 LIMIT\n"
    "- 一次只写一条语句；分号拼接会被拒\n"
    "- 字段拿不准就在图里先放一个 db_schema 工具节点，别猜字段名\n"
    # 真实踩过：列名是照着业务说法猜的，跑到查库那一步才「no such column」
    "- 列名只照上面结构里列出来的写；结构里只列了表名时先用 db_schema 查字段。搭完自查会拿 SQL 里的列名"
    "去对结构，对不上会打回来让你改\n"
    "- 比率、增幅、占比这类派生计算进口径卡（metrics 节点）；查询结果里原样的数，报告撰写节点（report）"
    "可以直接用 [[v:Q1.r0.列名]] 引用。别让 llm 节点直接对数字做算术\n"
)

PICK_SYSTEM = (
    "你在帮工作流助手挑选数据表。用户这一轮提了一个需求，助手接下来要据此搭工作流、写 SQL；数据库的表太多，"
    "没法把每张表的字段都给它看，它只会看到你挑出的表的全部字段。请从表目录里挑出完成这个需求要用到的表。\n\n"
    "挑选规则：\n"
    f"- 最多挑 {PICK_LIMIT} 张，按和需求的相关程度从高到低排\n"
    "- 事实表和维度表都要：要按渠道、门店、日期之类分组或筛选时，把对应的维度表也挑上\n"
    "- 两张表要靠一张关联表才能连起来时，关联表也挑上\n"
    "- 「当前工作流已用到」的表，需求还用得着就挑上\n"
    "- 拿不准要不要的，宁可挑上\n"
    "- table 照抄目录里的表名（含 schema 前缀），不要改写、不要编造；source 写表所在的数据源名\n"
    "- 需求和数据库无关（闲聊、只改提示词或流程结构）时返回空列表\n\n"
    '只输出 JSON：{"tables": [{"source": "数据源名", "table": "表名"}]}'
)
PICK_PROMPT = "需求：{need}\n\n{used}表目录（每行一张表：表名｜中文名｜粒度；行尾标「推断，未确认」的是推断出来的）：\n\n{tables}"
PICK_USED_PROMPT = "当前工作流已用到：{tables}\n\n"
PICK_PROMPT_SCHEMA = {
    "title": "picked_tables",
    "description": "完成需求要用到的表，按相关程度从高到低排",
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "表所在的数据源名"},
                    "table": {"type": "string", "description": "照抄表目录里的表名"},
                },
                "required": ["table"],
            },
        },
    },
    "required": ["tables"],
}


# ==========================================================================
# 拿模型
# ==========================================================================


async def pick_model(session: Any, spec: ModelSpec) -> tuple[Any | None, str | None]:
    """挑表用的模型：(模型, None)，拿不到时 (None, 原因)。

    spec 是助手的模型规格（copilot_model_spec），这里关掉思考、额度取小、温度 0、请求时限对齐 PICK_TIMEOUT_S：
    挑表是一次分类，不需要思考，思考只会拖慢生成开始的时间。没配接入时 get_chat_model 会静默退回演示模型，
    演示模型挑不了表，直接按挑表失败处理。

    get_chat_model 用本模块的名字：建图模型和挑表模型分得开（测试里各换各的假模型，互不串）。
    """
    from app.core.errors import explain
    from app.providers.mock_model import MockChatModel

    spec = spec.model_copy(update={"thinking": "off", "max_tokens": PICK_MAX_TOKENS, "temperature": 0,
                                   "timeout": math.ceil(PICK_TIMEOUT_S)})
    try:
        model, _ = await get_chat_model(session, spec)
    except Exception as e:  # noqa: BLE001 - 拿不到模型只是不挑表，生成照常
        return None, f"助手使用的模型不可用：{explain(e)[0]}"
    if isinstance(model, MockChatModel):
        return None, "演示模型不能按需求挑选数据表"
    return model, None


# ==========================================================================
# 规划：读目录、定模式
# ==========================================================================


@dataclass
class SourcePlan:
    """一个数据源怎么给：all 给详细摘要，pick 先挑表。"""

    source: Any
    mode: Literal["all", "pick"]
    #: 目录原样（含驳回项）：关系图要靠驳回项压住同一条关系的现推结果
    raw_notes: dict[str, dict[str, Any]]
    #: 现有工作流 SQL 里查过的表（schema_cache 的键）
    seeds: list[str]

    @property
    def tables(self) -> dict[str, Any]:
        return (getattr(self.source, "schema_cache", None) or {}).get("tables") or {}

    @property
    def notes(self) -> dict[str, dict[str, Any]]:
        """给模型看的目录：去掉驳回项。"""
        return {t: catalog.visible_notes(n) for t, n in self.raw_notes.items()}


def _modes(sources: list[Any]) -> list[Literal["all", "pick"]]:
    """每个源给详细摘要还是先挑表。

    沿用以前的判断：表超过 DETAIL_TABLE_LIMIT 的库一定挑表；其余的按详细摘要从短到长放进 DATASOURCE_BUDGET_CHARS，
    放得下的给详细摘要，放不下的挑表。全都放得下时和以前一样全是详细模式；以前超预算是所有库一起退成只列表名，
    现在是放不下的那几个库去挑表。没探查过结构的库没有表可挑，照旧给一行说明。
    """
    modes: list[Literal["all", "pick"]] = []
    sized: list[tuple[int, int]] = []
    for i, src in enumerate(sources):
        names = introspect.table_names(src)
        if names and len(names) > DETAIL_TABLE_LIMIT:
            modes.append("pick")
            continue
        modes.append("all")
        if names:
            sized.append((len(introspect.summary(src, detail=True)), i))
    used = 0
    for size, i in sorted(sized):
        if used + size <= DATASOURCE_BUDGET_CHARS:
            used += size
        else:
            modes[i] = "pick"
    return modes


def graph_tables(graph: Mapping[str, Any] | None, sources: Iterable[Any]) -> dict[str, list[str]]:
    """现有工作流里查库节点的 SQL 用到的表：{源名: [schema_cache 的键]}，按出现顺序。

    只认调用工具节点上写死的 SQL（db_query__<源>）；Agent 运行时自己写的 SQL 图里看不到。节点认两种写法：
    画布的 {data: {config}} 和 GraphSpec 的 {config}。SQL 解析不了的跳过。
    """
    from app.engine.evidence import sql_tables
    from app.tools.datasource import QUERY_PREFIX

    by_name = {getattr(s, "name", None): s for s in sources}
    out: dict[str, list[str]] = {}
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.get("config")
        if not isinstance(config, dict):
            config = (node.get("data") or {}).get("config") if isinstance(node.get("data"), dict) else None
        if not isinstance(config, dict):
            continue
        tool = str(config.get("tool") or "")
        source = by_name.get(tool[len(QUERY_PREFIX):]) if tool.startswith(QUERY_PREFIX) else None
        args = config.get("args")
        sql = args.get("sql") if isinstance(args, dict) else None
        if source is None or not isinstance(sql, str):
            continue
        try:
            names = sql_tables(sql)
        except Exception:  # noqa: BLE001 - 认不出表就当没用到，只少一个提示
            continue
        found = out.setdefault(source.name, [])
        for name in names:
            key = catalog.resolve_table_name(source.schema_cache, name)
            if key and key not in found:
                found.append(key)
    return {k: v for k, v in out.items() if v}


async def plan_context(sources: list[Any], *, graph: Mapping[str, Any] | None = None) -> ContextPlan:
    """读各源的数据目录、定每个源怎么给。不调模型。

    graph 是现有工作流（改图时的原图、追问时上一轮的图），它的 SQL 里查过的表在大库里一律给全字段。目录读不到
    就当没有（tools.datasource._catalog_of：用单独的会话读，出错不连累请求的会话）。
    """
    from app.tools.datasource import _catalog_of

    seeds = graph_tables(graph, sources)
    plans = []
    for src, mode in zip(sources, _modes(sources)):
        entries = await _catalog_of(src) if introspect.table_names(src) else {}
        plans.append(SourcePlan(source=src, mode=mode, raw_notes={t: e.notes for t, e in entries.items()},
                                seeds=seeds.get(src.name, [])))
    return ContextPlan(plans)


# ==========================================================================
# 挑表
# ==========================================================================


class _Unreadable(ValueError):
    """模型的回复读不出表名。"""


def _picked(raw: Any) -> list[tuple[str | None, str]] | None:
    """模型的回复 → [(源名或 None, 表名)]。结构化输出的对象、正文里的 JSON、字符串列表都认；对不上格式返回 None。"""
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if isinstance(raw, str):
        text = raw.strip()
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            return None
        end = text.rfind("}" if text[start] == "{" else "]")
        try:
            raw = json.loads(text[start:end + 1])
        except ValueError:
            return None
    if isinstance(raw, dict):
        raw = raw.get("tables")
    if not isinstance(raw, list):
        return None
    out: list[tuple[str | None, str]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append((None, item.strip()))
        elif isinstance(item, dict) and isinstance(item.get("table"), str) and item["table"].strip():
            src = item.get("source")
            out.append((src.strip() if isinstance(src, str) and src.strip() else None, item["table"].strip()))
    return out


async def _ask(model: Any, messages: list[tuple[str, str]]) -> list[tuple[str | None, str]]:
    """问一次。先走结构化输出；不支持、报错、或给回来的不是约定的结构，再退回纯文本、从正文里取 JSON。

    结构化输出报错也退回（同 catalog._ask_model）：有的网关不支持工具调用，一调就报错，纯文本却能用。两次加起来
    仍受 PICK_TIMEOUT_S 约束。
    """
    from app.engine.state import message_text

    try:
        parsed = _picked(await model.with_structured_output(PICK_PROMPT_SCHEMA).ainvoke(messages))
    except Exception:  # noqa: BLE001 - 退回纯文本
        parsed = None
    if parsed is not None:
        return parsed
    parsed = _picked(message_text(await model.ainvoke(messages)))
    if parsed is None:
        raise _Unreadable("模型的回复不是约定的格式，读不出要用的表")
    return parsed


@dataclass
class ContextPlan:
    """plan_context 的结果：各源怎么给，等着挑表、组装。"""

    plans: list[SourcePlan]

    @property
    def needs_pick(self) -> bool:
        return any(p.mode == "pick" for p in self.plans)

    def _pick_messages(self, need: str) -> list[tuple[str, str]]:
        blocks, used = [], []
        for p in self.plans:
            if p.mode != "pick":
                continue
            qualified = _qualifier(p.tables)
            index = catalog.render_table_index(p.source.schema_cache, p.notes)
            blocks.append(f"数据源「{p.source.name}」（共 {len(p.tables)} 张表）：\n{index}")
            if p.seeds:
                used.append(f"数据源「{p.source.name}」的 " + "、".join(qualified(t) for t in p.seeds))
        human = PICK_PROMPT.format(need=need.strip(), used=PICK_USED_PROMPT.format(tables="；".join(used)) if used
                                   else "", tables="\n\n".join(blocks))
        return [("system", PICK_SYSTEM), ("human", human)]

    def _resolve(self, picks: list[tuple[str | None, str]]) -> list[tuple[int, str]]:
        """模型挑的表对到表结构上：[(第几个源, schema_cache 的键)]。对不上的丢掉。

        只有一个源要挑时不看模型写的源名（写错了也不要紧）；有几个时按源名认，表名前面带着源名（「源名.表名」）
        也认，写了一个不存在的源名的丢掉；没写源名的按顺序在各源里找。
        """
        pick = [i for i, p in enumerate(self.plans) if p.mode == "pick"]
        by_name = {self.plans[i].source.name: i for i in pick}
        out: list[tuple[int, str]] = []
        for source, table in picks:
            name = table
            if len(pick) == 1:
                candidates = pick
            elif source in by_name:
                candidates = [by_name[source]]
            else:
                prefixed = next(((by_name[n], table[len(n) + 1:]) for n in by_name
                                 if table.startswith(n) and table[len(n):len(n) + 1] in (".", ":", "：")), None)
                if prefixed is not None:
                    candidates, name = [prefixed[0]], prefixed[1]
                elif source is not None:
                    continue
                else:
                    candidates = pick
            for i in candidates:
                key = catalog.resolve_table_name(self.plans[i].source.schema_cache, name)
                if key:
                    out.append((i, key))
                    break
        return out

    async def resolve(self, model: Any | None, *, need: str, unavailable: str | None = None) -> DatasourceContext:
        """挑表并组装。不抛异常：挑表的任何失败都退回只列表名（fallback），原因写进 context 操作和日志。

        model 是 pick_model 拿到的模型；拿不到时为 None，unavailable 是原因。没有要挑表的源时不调模型。
        """
        if not self.needs_pick:
            return self._assemble([], reason=None, elapsed_ms=None)
        started = time.monotonic()
        reason: str | None = None
        chosen: list[tuple[int, str]] = []
        if model is None:
            reason = unavailable or "没有可用的模型"
        else:
            try:
                picks = await asyncio.wait_for(_ask(model, self._pick_messages(need)), timeout=PICK_TIMEOUT_S)
                chosen = self._resolve(picks)
                if not picks:
                    reason = "模型没有挑出任何表"
                elif not chosen:
                    reason = "模型挑出的表在表结构中都不存在"
            except asyncio.TimeoutError:
                reason = f"挑选数据表超过 {PICK_TIMEOUT_S:g} 秒未完成"
            except _Unreadable as e:
                reason = str(e)
            except Exception as e:  # noqa: BLE001 - 挑表失败不挡生成
                from app.core.errors import explain

                reason = f"挑选数据表的模型调用失败：{explain(e)[0]}"
        elapsed = int((time.monotonic() - started) * 1000)
        ctx = self._assemble(chosen if reason is None else [], reason=reason, elapsed_ms=elapsed)
        if reason is None:
            logger.info("助手挑表用时 %d ms：%s", elapsed, "；".join(
                f"{s.source.name} {len(s.tables)} 张" for s in ctx.sources if s.selected_by == "model"))
        else:
            logger.warning("助手挑表未成功（%s），用时 %d ms，退回只列表名", reason, elapsed)
        return ctx

    def fallback(self, reason: str) -> DatasourceContext:
        """不挑表、直接按挑表失败组装：resolve 本身出了意外时用。"""
        return self._assemble([], reason=reason, elapsed_ms=None)

    def _assemble(self, chosen: list[tuple[int, str]], *, reason: str | None,
                  elapsed_ms: int | None) -> DatasourceContext:
        """定下每个源给全字段的表。

        先放现有工作流用到的表，再按模型给的顺序放挑中的表，合计不超过 PICK_LIMIT；然后每个源在挑中的表两两之间
        找不超过 2 跳的连接路径，把中间表补进来，所有源合计不超过 CONTEXT_TABLE_LIMIT。已经直接相连的一对不补；
        有一条路径的中间表已经在里面的也不补。
        """
        picked: dict[int, list[str]] = {}
        room = PICK_LIMIT
        for i, key in [(i, t) for i, p in enumerate(self.plans) if p.mode == "pick" for t in p.seeds] + chosen:
            have = picked.setdefault(i, [])
            if room > 0 and key not in have:
                have.append(key)
                room -= 1
        extra = CONTEXT_TABLE_LIMIT - sum(len(v) for v in picked.values())
        out: list[SourceContext] = []
        for i, p in enumerate(self.plans):
            if p.mode == "all":
                out.append(SourceContext(p, "all", list(p.tables)))
                continue
            first = picked.get(i, [])
            graph: dict[str, list[JoinEdge]] = {}
            tables, routes = list(first), []
            if len(first) > 1:
                graph = _relation_graph(p)
                tables, routes = _complete(first, graph, extra)
                extra -= len(tables) - len(first)
            out.append(SourceContext(p, "fallback" if reason is not None else "model", tables, picked=first,
                                     routes=routes, graph=graph))
        return DatasourceContext(out, reason=reason, elapsed_ms=elapsed_ms)


def _relation_graph(plan: SourcePlan) -> dict[str, list[JoinEdge]]:
    """这个源的关系图：目录里的关系（驳回的不进）加上按结构现推的（外键约束、命名推断）。推不出来就当没有。"""
    try:
        return catalog.relation_graph(plan.raw_notes, schema_cache=plan.source.schema_cache)
    except Exception:  # noqa: BLE001 - 少了连接条件只是少一个提示
        logger.exception("数据源 %s 的关系图推不出来，本轮不给连接条件", plan.source.name)
        return {}


def _complete(picked: list[str], graph: Mapping[str, list[JoinEdge]],
              room: int) -> tuple[list[str], list[tuple[str, str, tuple[JoinEdge, ...]]]]:
    """补连接用的中间表：(给全字段的表, [(a, b, 经中间表的两跳路径)])。room 是还能补几张。"""
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


# ==========================================================================
# 组装好的上下文
# ==========================================================================


def _qualifier(tables: Mapping[str, Any]):
    """表名（schema_cache 的键）→ 全名：模型照着全名写 SQL，漏了 schema 前缀在 Oracle 上直接报错。"""
    return lambda key: str((tables.get(key) or {}).get("qualified") or key)


@dataclass
class SourceContext:
    """一个源这一轮怎么给的：selected_by、给全字段的表（all 时是全部表）。"""

    plan: SourcePlan
    selected_by: SelectedBy
    #: 给全字段的表（schema_cache 的键）：挑中的表在前，补进来的中间表在后
    tables: list[str]
    #: 挑中的表（含现有工作流用到的），不含补进来的中间表
    picked: list[str] = field(default_factory=list)
    routes: list[tuple[str, str, tuple[JoinEdge, ...]]] = field(default_factory=list)
    graph: dict[str, list[JoinEdge]] = field(default_factory=dict)

    @property
    def source(self) -> Any:
        return self.plan.source

    @property
    def total(self) -> int:
        return len(self.plan.tables)

    def qualified(self, key: str) -> str:
        return _qualifier(self.plan.tables)(key)


@dataclass
class DatasourceContext:
    sources: list[SourceContext]
    #: 挑表失败的原因（没有失败时为 None）
    reason: str | None = None
    #: 挑表用了多久（毫秒）；没有挑表时为 None
    elapsed_ms: int | None = None

    def listed(self, source: Any) -> list[str]:
        """工具清单里「可查」后面列的对象：挑中的表在前，其余按表结构的顺序。"""
        names = introspect.table_names(source)
        ctx = next((s for s in self.sources if s.source is source), None)
        if ctx is None or ctx.selected_by == "all" or not ctx.tables:
            return names
        first = [ctx.qualified(t) for t in ctx.tables]
        return first + [n for n in names if n not in first]

    def op(self) -> dict[str, Any] | None:
        """告诉前端这一轮参考了哪些表：{"op": "context", "sources": [...], "elapsed_ms"?}。没有可查的表时为 None。

        每个源一项：source 源名，tables 给了全字段的表（全名），selected_by，total 这个源一共几张表；挑表失败的
        带上 reason。只列了名字的表不进 tables：界面上说的「参考了 N 张表」是模型看得到字段的那些。
        """
        rows = []
        for s in self.sources:
            if not s.total:
                continue
            row: dict[str, Any] = {"source": s.source.name, "tables": [s.qualified(t) for t in s.tables],
                                   "selected_by": s.selected_by, "total": s.total}
            if s.selected_by == "fallback" and self.reason:
                row["reason"] = self.reason
            rows.append(row)
        if not rows:
            return None
        out: dict[str, Any] = {"op": "context", "sources": rows}
        if self.elapsed_ms is not None:
            out["elapsed_ms"] = self.elapsed_ms
        return out

    def section(self) -> str:
        """system 提示词里的数据源一节。没有数据源时返回空串，不占提示词。"""
        if not self.sources:
            return ""
        budget = [CATALOG_BUDGET_CHARS]
        blocks: list[str] = []
        names_only = False
        for s in self.sources:
            try:
                if s.selected_by == "all":
                    blocks.append(_detail_block(s, budget))
                elif s.tables:
                    blocks.append(_selected_block(s))
                else:
                    blocks.append(introspect.summary(s.source, detail=False))
                    names_only = True
            except Exception:  # noqa: BLE001 - 结构缓存里有意外的写法：这个源退回只列表名，不挡生成
                logger.exception("数据源 %s 的上下文组装失败，本轮只列表名", s.source.name)
                blocks.append(introspect.summary(s.source, detail=False))
                names_only = True
        text = "\n".join(blocks)
        tail = []
        if names_only:
            tail.append(NAMES_ONLY_PROMPT)
        if INFERRED_MARK in text:
            tail.append(INFERRED_PROMPT)
        return SECTION_HEAD_PROMPT + "\n".join([text, *tail]) + SQL_RULES_PROMPT


# ==========================================================================
# 渲染
# ==========================================================================


def _notes_body(s: SourceContext, key: str) -> list[str]:
    """一张表的目录正文（render_table_notes 去掉标题行：「推断，未确认」的说明在整节末尾说一次）。"""
    block = catalog.render_table_notes(s.plan.notes.get(key), meta=s.plan.tables.get(key),
                                       system_notes=catalog.system_notes_source(s.source))
    return block.split("\n")[1:] if block else []


def _condition(edge: JoinEdge, qualified) -> str:
    """连接条件，表名用全名：channel_visits.visit_id = visits.id（多列用 AND 连接）。"""
    left, right = qualified(edge.from_table), qualified(edge.to_table)
    return " AND ".join(f"{left}.{a} = {right}.{b}" for a, b in zip(edge.from_columns, edge.to_columns))


def _edge_line(edge: JoinEdge, qualified) -> str:
    card = f"（{CARDINALITY_LABEL[edge.cardinality]}）" if edge.cardinality in CARDINALITY_LABEL else ""
    return _condition(edge, qualified) + card + (INFERRED_MARK if edge.status == "proposed" else "")


def _edges_within(graph: Mapping[str, list[JoinEdge]], tables: list[str]) -> list[JoinEdge]:
    """两端都在 tables 里的关系，每条一次；一对多的翻过来，从「多」的一端写起（外键所在的表在左）。"""
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


def _join_lines(s: SourceContext, graph: Mapping[str, list[JoinEdge]], tables: list[str]) -> list[str]:
    edges = _edges_within(graph, tables)
    if not edges:
        return []
    lines = ["    表之间的连接条件（写 JOIN … ON … 时照抄）："]
    lines += [f"      {_edge_line(e, s.qualified)}" for e in edges[:JOIN_LINES_LIMIT]]
    if len(edges) > JOIN_LINES_LIMIT:
        lines.append(f"      …另有 {len(edges) - JOIN_LINES_LIMIT} 条，用 db_schema 工具查看各表的关联关系")
    return lines


def _detail_block(s: SourceContext, budget: list[int]) -> str:
    """放得下详细摘要的源：照旧每张表一行带字段，后面附表之间的连接条件和数据目录。

    连接条件和目录共用一份预算（budget，所有详细模式的源合计 CATALOG_BUDGET_CHARS）：详细摘要本身已经按
    DATASOURCE_BUDGET_CHARS 放进来了，附带的东西不能再把提示词撑大一倍。连接条件先放（最短、最能防止猜着连表），
    目录按表放，放不下的表让模型用 db_schema 查（db_schema 的输出里带目录）。
    """
    lines = [introspect.summary(s.source, detail=True)]
    if not s.total:
        return lines[0]
    joins = _join_lines(s, _relation_graph(s.plan), list(s.plan.tables))
    size = sum(len(line) + 1 for line in joins)
    if joins and size <= budget[0]:
        budget[0] -= size
        lines += joins
    elif joins:
        lines.append("    （表之间的连接条件用 db_schema 工具查看各表的关联关系）")
    notes: list[str] = []
    skipped = False
    for key in s.plan.tables:
        body = _notes_body(s, key)
        if not body:
            continue
        piece = [f"      · {s.qualified(key)}：", *(f"      {line}" for line in body)]
        size = sum(len(line) + 1 for line in piece)
        if size > budget[0]:
            skipped = True
            continue
        budget[0] -= size
        notes += piece
    if notes:
        lines.append("    数据目录：")
        lines += notes
    if skipped:
        lines.append("    （其余表的数据目录用 db_schema 工具查看）")
    return "\n".join(lines)


def _source_head(source: Any) -> str:
    """一个源的标题行，和 introspect 的详细摘要同一个写法。"""
    schema = (getattr(source, "schema_cache", None) or {}).get("schema")
    return (f"- {source.name}（{source.kind}，{'只读' if source.readonly else '可写'}）："
            f"{source.description or '未填说明'}" + (f"｜schema：{schema}" if schema else ""))


def _route_line(a: str, b: str, path: tuple[JoinEdge, ...], qualified) -> str:
    first, second = path
    qa, mid, qb = qualified(a), qualified(first.to_table), qualified(b)
    return (f"      {qa} 与 {qb} 没有直接关联，经 {mid} 连接：FROM {qa} JOIN {mid} ON {_condition(first, qualified)} "
            f"JOIN {qb} ON {_condition(second, qualified)}" + (INFERRED_MARK if any(e.status == "proposed" for e in path)
                                                               else ""))


def _selected_block(s: SourceContext) -> str:
    """挑了表的源：挑中的表给全部字段和目录，然后是连接条件、连接路径，其余表只列名字。"""
    lead = "按这一轮的需求挑出" if s.selected_by == "model" else "现有工作流用到"
    blocks, noted = [], False
    for key in s.tables:
        blocks += [f"    {line}" for line in introspect.describe_table(s.source, key).split("\n")]
        body = _notes_body(s, key)
        if body:
            noted = True
            blocks.append("      数据目录：")
            blocks += [f"      {line}" for line in body]
    lines = [_source_head(s.source),
             f"    {lead}的 {len(s.tables)} 张表（全部字段{'，有数据目录的附在表后' if noted else ''}）：", *blocks]
    lines += _join_lines(s, s.graph, s.tables)
    if s.routes:
        lines.append("    连接路径：")
        lines += [_route_line(a, b, path, s.qualified) for a, b, path in s.routes[:PATH_LINES_LIMIT]]
    shown = set(s.tables)
    rest = [s.qualified(t) for t in s.plan.tables if t not in shown]
    if rest:
        lines.append(f"    其余 {len(rest)} 个对象只列名字（要用时先用 db_schema__{s.source.name} 查字段，别猜）："
                     + "、".join(rest))
    return "\n".join(lines)


__all__ = [
    "CATALOG_BUDGET_CHARS", "CONTEXT_TABLE_LIMIT", "ContextPlan", "DATASOURCE_BUDGET_CHARS", "DETAIL_TABLE_LIMIT",
    "DatasourceContext", "PICK_LIMIT", "PICK_MAX_TOKENS", "PICK_TIMEOUT_S", "SourceContext", "SourcePlan",
    "graph_tables", "pick_model", "plan_context",
]
