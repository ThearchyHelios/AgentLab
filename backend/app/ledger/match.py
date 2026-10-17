"""认口径：运行封存之后，拿这次执行的 SQL 和已发布口径的取数比对（docs/design/next-round/02-口径账本.md 做法 2）。

**什么时候比。** 探索运行（问数据 chat、画布试运行 canvas、蓝图试运行 trial、个人应用刷新 personal_app）成功结束、封存完、
封存范围内有查询快照时，在 runner 封存之后放到后台比（和 notify_run 同一个位置 spawn）：比对是 CPU 活，不该拖着运行收尾；
放在封存之前，外部的口径表就混进了运行自身的封存事实。正式运行不比 SQL——它本身就是某一版的承诺，账本按 Run.workflow_id /
version 记。系统派生的运行（评测、对账、召回）不比：它们是服务端为了量、为了比替人跑的（engine/origin.SYSTEM_ORIGINS）。

**和谁比。** 每个工作流最新的已发布、未退役版本（apps/versions.latest_published，和 look 的 caliber、蓝图复用口径同一种说法，
拍板 L4：受管、已发布都算，标签写明级别）里查库的调用工具节点。先按同一个数据源、SQL 里的表有交集预筛，每条查询最多比
MAX_CANDIDATES 份（按工作流、版本、节点排好的先后）。

**怎么比。** data/sql_semantics.match_template：先绑定参数（只是启发），再按绑定渲染口径的取数 SQL 交 explain_sql_diff 证明。

**按引用的列投影**（_projected，algo 3）。助手写的 SQL 总会多选几列（订单数、日期常量、MIN / MAX……），口径的取数也只选
它的指标要的那几列：整条比永远不等价。整条比不等价时，答案一侧按报告引用了这条查询的哪几列（labels.referenced_columns：
单元格的列、指标输入落在的列）投影，口径一侧按口径卡上每个指标读了这个取数节点的哪几列（labels.card_reads）投影
（sql_semantics.project_select / project_template：去掉没被引用、去掉也不改变被引用那几列取值的选择项，认不准的一律
不投影），投影后的两条照样交 match_template 绑定、判等价——不另开一条给信任的路，判等价那一步一点不放宽。

被引用的列两边怎么对上：答案一侧按列名在查询快照记的结果列（columns）里找位置，结果列按位置对上选择列表的各项（列数
和选择列表的项数要一样）；口径一侧按口径卡 cell(nodes.取数, 行, '列') 里写的列名，在取数 SQL 选择列表的输出名（别名，
没有别名的是列名，不分大小写，同名的不止一项时不投影）里找位置。投影后两边留下的几列按位置配对（和整条比时的列映射一样：
外层别名不计、列的先后计），两边列数一样才比；答案一侧留下两三列时另试调换先后。每份口径取数最多试 MAX_PROJECTIONS 种。
证明了的候选记 projection {ours, dropped, caliber, caliber_dropped}；没证明的给第一种投影的比较（差在哪是投影之后的说法，
答案多选了列的在最前面写一句「这次多查了 N 列，不影响这个数」）。没有投影可试的（报告没引用这条查询、两边都不投影、
投影不了）照旧是整条比的结果，逐对一字不差。

**助手选用的（composed）。** 问数据的助手 use_caliber 选用口径时，取数节点是服务端照那一版拷进来的（ledger/compose.py），
口径卡的 caliber_from 带着改名映射 {原 id: 新 id}。这几个节点交回的查询照样要证明：证明了的那一份记 how=composed，查询
状态记 composed——数确实来自口径，但「这份口径答的就是这个问题」是助手凭问法判断的，不能说成「等价已证明」（拍板 L1：
自动正式出具只给独立等价的）。没能证明的（拷进来之后在画布上被改过）照常是相关口径，另写一句为什么。映射从这次运行
封存的图里读（composed_nodes），比对时把那一份口径排在最前面，不会因为候选太多被挤掉。
schema 用这条查询当时冻结的表结构（表结构快照），列的中文名取同一份快照里冻结的数据目录。只读封存范围内的快照（api/evidence
的 _sealed 读法），读不出、被改过的快照不比，记 skipped。

**资源。** 每次运行限时（设置 ledger.match_timeout_s，缺省 TIMEOUT_S = 8 秒），在线程里跑，同时最多 CONCURRENCY（2）个（照 scheduler/auto_compare 的做法：
信号量按事件循环分开，线程池两个线程）。时限按绑定逐组看：过了时限，还没比的口径记 skipped（比对超时）；整次比对卡在线程里
过了时限再加 GRACE_S 还没回来，整次记 skipped。解析失败同样照实记 skipped 和原因。都只记日志和 skipped，不影响运行本身。

**结果。** ledger/store：内容寻址工件 kind=caliber_match + 封存之后追加的 ledger.matched。

**启动时补比。** 最近 BACKFILL_DAYS 天封存了、却一次都没比过的探索运行，启动后在后台按从新到旧逐个补比（最多 BACKFILL_LIMIT 个），
trigger=backfill。补比只读工件和版本表（加上数据源的类型，取方言用），不查业务库、不调模型。和调度循环同一个开关
（AGENTLAB_SCHEDULER）：拷来的数据目录（检查脚本）、测试不在启动时替原库补做后台的事。打开账本时没比过的运行按需比
（trigger=on_demand，api/ledger 的按运行读接口），同样是事后按那时的口径回看。

评测（第 3 波）要逐题比 SQL 时直接调 compare_queries：同一个比对函数。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import DataSource, Run, Workflow, WorkflowVersion
from app.ledger import labels as ledger_labels
from app.ledger import store

logger = logging.getLogger(__name__)

#: 比对哪几种来源的运行（见模块说明）
EXPLORE_ORIGINS = ("chat", "canvas", "trial", "personal_app")
#: 每次运行的时限（秒）、同时最多几个
TIMEOUT_S = 8.0
CONCURRENCY = 2
#: 线程过了时限还没回来，再等这么久就整次记 skipped
GRACE_S = 2.0
#: 每条查询最多比几份口径
MAX_CANDIDATES = 8
#: 按引用的列投影之后，每份口径取数最多再试几种比法（答案一侧留下的列的先后 × 口径一侧的列组，见 _projected）
MAX_PROJECTIONS = 12
#: 启动时补比最近几天、最多几个
BACKFILL_DAYS = 7
BACKFILL_LIMIT = 200

#: skipped 的原因
TIMEOUT = "比对超过时限，没有做完"
SEAL_BROKEN = "封存核对不一致，不比对"
SNAPSHOT_TAMPERED = "查询快照与哈希不一致，疑似被修改，不比对"
SNAPSHOT_MISSING = "查询快照读不出来，不比对"
NO_SQL = "查询快照里没有 SQL，不比对"
PARSE_FAILED = "这次的 SQL 无法解析，没有比对"
MERGED = "合并查询的格不和口径比对（它的数来自各个输入的查询）"
COMPOSED_UNPROVEN = "这条查询出自助手选用的口径，但和口径的取数没能证明一致（可能被改过）"
MORE_CANDIDATES = "另有 {n} 份相关口径没有比对（每条查询最多比 {max} 份）"
#: 按引用的列投影之后没证明等价时，差在哪的第一句：答案只是多选了几列（去掉它们不影响被引用的数，见 sql_semantics 的投影）
EXTRA_COLUMNS = "这次多查了 {n} 列，不影响这个数"

#: 不比对的原因（GET 账本时照实说）
NOT_FOUND = "运行记录不存在"
FORMAL = "正式运行本身就是某一版口径的出具，不比对 SQL"
NOT_EXPLORE = "只比对问数据、画布试运行、蓝图试运行和个人应用刷新这几种探索运行"
NOT_SUCCEEDED = "运行没有成功结束，不比对"
NOT_SEALED = "运行还没有封存，不比对"
NO_QUERIES = "这次运行没有查询，没有可比对的"


def ineligible(run: Any) -> str | None:
    """这次运行比不比对：不比的回一句为什么，比的是 None（还要封存范围内有查询快照，见 match_run）。"""
    if run is None:
        return NOT_FOUND
    if run.run_class == "formal":
        return FORMAL
    if (run.origin_kind or "") not in EXPLORE_ORIGINS:
        return NOT_EXPLORE
    if run.status != "succeeded":
        return NOT_SUCCEEDED
    if not run.manifest_hash:
        return NOT_SEALED
    return None


def wanted(run_class: str | None, origin_kind: str | None) -> bool:
    """runner 收尾时用：成功结束的这次运行要不要放到后台比（状态、封存由 match_run 再核对一次）。"""
    return run_class != "formal" and (origin_kind or "") in EXPLORE_ORIGINS


# --------------------------------------------------------------------------
# 口径与查询
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fetch:
    """口径版本里一个查库的调用工具节点。"""

    node_id: str
    source: str
    sql: str
    tables: frozenset[str]


@dataclass(frozen=True)
class Caliber:
    """一个工作流最新的已发布、未退役版本（只收里面有查库节点的）。fields 是入口节点的字段原样（取缺省值用，不进工件），
    cards 是源头口径卡的节点 id，expressions 是这些卡上每个指标换成记号后的式子（labels.card_expressions），reads 是每个
    指标读了哪几个取数节点的哪几列（labels.card_reads：口径一侧按它投影，不进工件）。"""

    workflow_id: str
    version: int
    level: str
    graph_hash: str | None
    fetches: tuple[Fetch, ...]
    fields: tuple[dict[str, Any], ...] = ()
    cards: tuple[str, ...] = ()
    expressions: tuple[tuple[str, str, str], ...] = ()
    reads: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = ()

    @property
    def key(self) -> str:
        return ledger_labels.caliber_key(self.workflow_id, self.version)

    def summary(self) -> dict[str, Any]:
        """工件里记的「考虑过的口径版本」：不放工作流名（会改）、不放缺省值。"""
        return {"caliber": self.key, "workflow_id": self.workflow_id, "version": self.version, "level": self.level,
                "graph_hash": self.graph_hash, "fetches": [f.node_id for f in self.fetches], "cards": list(self.cards),
                "fields": [{"name": f.get("name"), "required": bool(f.get("required")),
                            "has_default": f.get("default") not in (None, "")} for f in self.fields]}


@dataclass
class Query:
    """封存范围内的一条查询：快照工件、哪个节点查的、数据源、SQL、结果的列名、SQL 里的表、方言、当时冻结的表结构和列的中文名。
    problem 不为空时这条不比（快照读不出、被改过、没有 SQL）。"""

    artifact: str
    node_id: str | None
    source: str
    sql: str = ""
    columns: list[str] | None = None
    tables: frozenset[str] = frozenset()
    dialect: str | None = None
    schema: dict[str, Any] | None = None
    names: dict[str, str] = field(default_factory=dict)
    problem: str | None = None


def _table_keys(sql: str) -> frozenset[str]:
    """SQL 里 FROM / JOIN 的表名（去掉库名前缀、按 name_key），预筛用。"""
    from app.data.names import name_key
    from app.engine.evidence import sql_tables

    try:
        return frozenset(name_key(t.rsplit(".", 1)[-1]) for t in sql_tables(sql or ""))
    except Exception:  # noqa: BLE001 - 认不出表：这一条预筛不上，不比
        return frozenset()


def caliber_of(workflow_id: str, row: Any) -> Caliber | None:
    """一个已发布版本 → Caliber；里面没有查库的调用工具节点（SQL 写在 args.sql 里）的不算口径的取数，返回 None。"""
    from app.apps.tree import node_map
    from app.tools.datasource import QUERY_PREFIX

    graph = getattr(row, "graph", None) or {}
    nodes = node_map(graph)
    fetches: list[Fetch] = []
    fields: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for node_id, node in sorted(nodes.items()):
        cfg = node["config"] if isinstance(node["config"], dict) else {}
        if node["type"] == "tool":
            tool = str(cfg.get("tool") or "")
            sql = (cfg.get("args") or {}).get("sql") if isinstance(cfg.get("args"), dict) else None
            if tool.startswith(QUERY_PREFIX) and "{{" not in tool and isinstance(sql, str) and sql.strip():
                fetches.append(Fetch(node_id, tool[len(QUERY_PREFIX):], sql, _table_keys(sql)))
        elif node["type"] == "input":
            fields += [dict(f) for f in cfg.get("fields") or [] if isinstance(f, dict) and f.get("name")]
        elif node["type"] == "metrics" and cfg.get("caliber_from") in (None, "", {}):
            cards.append({"node_id": node_id, "metrics": [m for m in cfg.get("metrics") or [] if isinstance(m, dict)]})
    if not fetches:
        return None
    return Caliber(workflow_id=workflow_id, version=int(row.version), level=str(getattr(row, "level", None) or ""),
                   graph_hash=getattr(row, "graph_hash", None), fetches=tuple(fetches), fields=tuple(fields),
                   cards=tuple(c["node_id"] for c in cards),
                   expressions=tuple(ledger_labels.card_expressions(cards)),
                   reads=tuple(ledger_labels.card_reads(cards)))


async def load_calibers(session: Any) -> list[Caliber]:
    """每个工作流最新的已发布、未退役版本里的口径取数（apps/versions.is_published，同 latest_published 的说法）。先只读版本号
    和级别挑出每个工作流的那一版，再读这几版的图。"""
    from app.apps.versions import is_published

    rows = (await session.execute(
        select(WorkflowVersion.id, WorkflowVersion.workflow_id, WorkflowVersion.version, WorkflowVersion.level,
               WorkflowVersion.retired_at, Workflow.published_version)
        .join(Workflow, Workflow.id == WorkflowVersion.workflow_id))).all()
    latest: dict[str, tuple[int, str]] = {}
    for vid, wid, version, level, retired, current in rows:
        probe = _VersionProbe(version, level, retired)
        if is_published(probe, current=current) and version > latest.get(wid, (0, ""))[0]:
            latest[wid] = (version, vid)
    if not latest:
        return []
    ids = [vid for _, vid in latest.values()]
    versions = (await session.execute(select(WorkflowVersion).where(WorkflowVersion.id.in_(ids)))).scalars().all()
    out = [c for v in versions if (c := caliber_of(v.workflow_id, v)) is not None]
    return sorted(out, key=lambda c: (c.workflow_id, c.version))


@dataclass(frozen=True)
class _VersionProbe:
    """is_published 只看这三样：不必为了判断把整张图读出来。"""

    version: int
    level: str | None
    retired_at: Any


def _frozen(schema_artifact: Any) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """查询当时冻结的表结构（explain_sql_diff 的 schema）和列的中文名（同一份快照里冻结的数据目录）。读不出时都没有。"""
    from app.core.artifact_store import load
    from app.data.sql_semantics import catalog_labels

    if not isinstance(schema_artifact, str) or not schema_artifact:
        return None, {}
    try:
        content = load(schema_artifact)
    except ValueError:
        return None, {}
    if not isinstance(content, dict) or not isinstance(content.get("tables"), dict):
        return None, {}
    frozen = content.get("catalog") if isinstance(content.get("catalog"), dict) else {}
    notes = {table: entry.get("notes") for table, entry in frozen.items()
             if isinstance(entry, dict) and isinstance(entry.get("notes"), dict)}
    return {"tables": content["tables"]}, catalog_labels(notes)


def read_queries(sealed: Any, kinds: dict[str, str | None]) -> list[Query]:
    """封存范围内的查询快照 → Query（读盘、复验哈希，放在线程里调）。合并查询不比：它的数都来自它的输入。"""
    from app.core.artifact_store import load
    from app.engine.merge_query import MERGE_SOURCE

    out: list[Query] = []
    for artifact, info in sealed.queries.items():
        node_id = (info or {}).get("node_id")
        try:
            snap = load(artifact)
        except ValueError:
            out.append(Query(artifact, node_id, str((info or {}).get("source") or ""), problem=SNAPSHOT_TAMPERED))
            continue
        if not isinstance(snap, dict):
            out.append(Query(artifact, node_id, str((info or {}).get("source") or ""), problem=SNAPSHOT_MISSING))
            continue
        source = str(snap.get("source") or (info or {}).get("source") or "")
        if source == MERGE_SOURCE:
            continue
        sql = snap.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            out.append(Query(artifact, node_id, source, problem=NO_SQL))
            continue
        schema, names = _frozen(snap.get("schema_artifact"))
        columns = [str(c) for c in snap.get("columns") or []] if isinstance(snap.get("columns"), list) else None
        out.append(Query(artifact, node_id, source, sql, columns, _table_keys(sql), kinds.get(source), schema, names))
    return out


# --------------------------------------------------------------------------
# 比对（纯计算，在线程里跑）
# --------------------------------------------------------------------------


def _candidate(caliber: Caliber, fetch: Fetch, m: Any) -> dict[str, Any]:
    """一份口径取数的比对结果，进工件。"""
    out: dict[str, Any] = {
        "caliber": caliber.key, "workflow_id": caliber.workflow_id, "version": caliber.version,
        "level": caliber.level, "node_id": fetch.node_id,
        "status": "proven" if m.equivalent else "related", "comparable": m.comparable,
        "via": m.level, "binding": dict(m.binding), "unbound": list(m.unbound),
    }
    if m.equivalent:
        out["columns"] = [dict(c) for c in m.columns] if m.columns is not None else None
    elif m.diff is not None:
        out["diff"] = {"items": [d.as_dict() for d in m.diff.items], "fallback": m.diff.fallback,
                       "text_diff": list(m.diff.text_diff)}
    if m.reason:
        out["reason"] = m.reason
    return out


def _skipped(caliber: Caliber, fetch: Fetch, reason: str) -> dict[str, Any]:
    return {"caliber": caliber.key, "workflow_id": caliber.workflow_id, "version": caliber.version,
            "level": caliber.level, "node_id": fetch.node_id, "status": "skipped", "reason": reason}


def composed_nodes(graph: Any) -> dict[str, tuple[str, str]]:
    """这次运行的图里助手选用口径拷进来的取数节点：{这张图里的节点 id: (「工作流@版本」, 那一版里的原 id)}。从口径卡的
    caliber_from.nodes 读（ledger/compose.py 实例化时写的）；没有改名映射的口径卡不算（蓝图复用口径时取数是模型写的）。"""
    out: dict[str, tuple[str, str]] = {}
    for node in (graph or {}).get("nodes") or [] if isinstance(graph, dict) else []:
        if not isinstance(node, dict) or node.get("type") != "metrics":
            continue
        cfg = (node.get("data") or {}).get("config") if isinstance(node.get("data"), dict) else None
        ref = cfg.get("caliber_from") if isinstance(cfg, dict) else None
        rename = ref.get("nodes") if isinstance(ref, dict) else None
        version = ref.get("workflow_version") if isinstance(ref, dict) else None
        if not isinstance(rename, dict) or not ref.get("workflow_id") or not str(version or "").isdigit():
            continue
        key = ledger_labels.caliber_key(str(ref["workflow_id"]), int(version))
        for old, new in rename.items():
            if isinstance(old, str) and isinstance(new, str):
                out.setdefault(new, (key, old))
    return out


def _wanted(query: Query, referenced: Mapping[str, Collection[str]] | None) -> tuple[int, ...] | None:
    """报告引用了这条查询的哪几列：按列名在查询快照记的结果列里找位置（按位置先后，同名的几列都算）。没引用、快照没记
    结果列的为 None（不投影）。"""
    names = (referenced or {}).get(query.artifact)
    if not names or query.columns is None:
        return None
    return tuple(i for i, c in enumerate(query.columns) if c in names) or None


def _caliber_sets(caliber: Caliber, fetch: Fetch, names: list[str]) -> list[tuple[int, ...]]:
    """口径一侧按哪几组列投影：口径卡上每个指标读了这个取数节点的哪几列（labels.card_reads），按列名（不分大小写）在取数
    SQL 的输出名里找位置、按位置排好；最后加上所有指标读到的列合在一起的那一组。找不到、同名的不止一项的那一组不要。"""
    lowered = [n.lower() for n in names]
    out: list[tuple[int, ...]] = []
    union: set[int] = set()
    for _, _, reads in caliber.reads:
        columns = [column for node, column in reads if node == fetch.node_id]
        if not columns:
            continue
        found = [[i for i, n in enumerate(lowered) if n == c.lower()] for c in columns]
        if any(len(f) != 1 for f in found):
            continue
        group = tuple(sorted({f[0] for f in found}))
        union |= set(group)
        if group not in out:
            out.append(group)
    whole = tuple(sorted(union))
    if whole and whole not in out:
        out.append(whole)
    return out


def _projection_view(query: Query, ours: Any, theirs: Any) -> dict[str, Any]:
    """候选上记的「按引用的列比的」：答案留下的列（快照里的列名）、去掉了几列，口径留下的列、去掉了几列。"""
    return {"ours": [str((query.columns or [])[i]) for i in ours.kept], "dropped": ours.dropped,
            "caliber": list(theirs.names), "caliber_dropped": theirs.dropped}


def _projected(caliber: Caliber, fetch: Fetch, query: Query, wanted: tuple[int, ...],
               deadline: float | None) -> dict[str, Any] | None:
    """整条比不等价时按引用的列投影再比（见模块说明）：证明了的候选（带 projection）；都没证明时第一种投影的那次比较；
    没有投影可试的为 None（照旧用整条比的结果）。"""
    from itertools import permutations

    from app.data.sql_semantics import FacetDiff, match_template, output_names, project_select, project_template

    width = len(query.columns or [])
    whole = project_select(query.sql, tuple(range(width)), query.dialect, width=width)
    if whole.sql is None:
        return None
    ours = []
    if len(wanted) < width:
        cut = project_select(query.sql, wanted, query.dialect, width=width)
        if cut.sql is not None:
            ours.append(cut)
    ours = ours or [whole]
    for base in list(ours):
        if 2 <= len(base.kept) <= 3:
            for order in permutations(base.kept):
                if order != base.kept and (p := project_select(query.sql, order, query.dialect, width=width)).sql:
                    ours.append(p)
    names = output_names(fetch.sql, query.dialect, template=True)
    if names is None:
        return None
    theirs = [p for group in _caliber_sets(caliber, fetch, names) if len(group) < len(names)
              for p in [project_template(fetch.sql, group, query.dialect)] if p.sql is not None]
    theirs.append(project_template(fetch.sql, tuple(range(len(names))), query.dialect))
    first: tuple[Any, Any, Any] | None = None
    tries = 0
    for a in ours:
        for c in theirs:
            if c.sql is None or len(a.kept) != len(c.kept) or (not a.dropped and not c.dropped
                                                               and a.kept == whole.kept):
                continue        # 两边都没投影：就是整条比过的那一次
            if tries >= MAX_PROJECTIONS or (deadline is not None and time.monotonic() > deadline):
                break
            tries += 1
            m = match_template(c.sql, list(caliber.fields), a.sql, query.dialect, schema=query.schema,
                               names=query.names, columns=[str((query.columns or [])[i]) for i in a.kept],
                               deadline=deadline)
            if m.equivalent:
                return {**_candidate(caliber, fetch, m), "projection": _projection_view(query, a, c)}
            if first is None:
                first = (a, c, m)
    if first is None:
        return None
    a, c, m = first
    cand = {**_candidate(caliber, fetch, m), "projection": _projection_view(query, a, c)}
    if a.dropped and isinstance(cand.get("diff"), dict):
        note = FacetDiff("projection", None, None, EXTRA_COLUMNS.format(n=a.dropped)).as_dict()
        cand["diff"] = {**cand["diff"], "items": [note, *cand["diff"]["items"]]}
    return cand


def compare_queries(queries: list[Query], calibers: list[Caliber], *, deadline: float | None = None,
                    composed: dict[str, tuple[str, str]] | None = None,
                    referenced: Mapping[str, Collection[str]] | None = None) -> list[dict[str, Any]]:
    """每条查询和预筛出来的口径取数逐个比（match_template），给出 [{artifact, node_id, source, tables, status, reason?,
    candidates}]。status：proven（和某一份等价）/ composed（助手选用的那一份，证明了）/ related（有同源同表的口径，都不
    等价）/ none（没有相关的口径）/ skipped（没比完：超时、解析失败、快照读不出）。composed 是 composed_nodes 的结果，
    不给就没有助手选用的。referenced 是报告引用了哪条查询的哪几列（labels.referenced_columns：{查询快照: {列名}}）：
    给了的，整条比不等价时按引用的列投影再比（_projected）；不给就只整条比。纯计算，从不抛异常。评测逐题比 SQL 也调它。"""
    from app.data.sql_semantics import match_template, normalize_sql

    out: list[dict[str, Any]] = []
    for query in queries:
        row: dict[str, Any] = {"artifact": query.artifact, "node_id": query.node_id, "source": query.source,
                               "tables": sorted(query.tables)}
        if query.problem:
            out.append({**row, "status": "skipped", "reason": query.problem, "candidates": []})
            continue
        chosen = (composed or {}).get(str(query.node_id or ""))
        wanted = _wanted(query, referenced)
        pool = [(c, f) for c in calibers for f in c.fetches if f.source == query.source and f.tables & query.tables]
        if chosen is not None:
            # 助手选用的那一份排最前面：候选多的时候也一定比到它
            pool.sort(key=lambda cf: (cf[0].key, cf[1].node_id) != chosen)
        if not pool:
            out.append({**row, "status": "none", "candidates": []})
            continue
        if normalize_sql(query.sql, query.dialect).parse_error:
            out.append({**row, "status": "skipped", "reason": PARSE_FAILED, "candidates": []})
            continue
        candidates: list[dict[str, Any]] = []
        for caliber, fetch in pool[:MAX_CANDIDATES]:
            if deadline is not None and time.monotonic() > deadline:
                candidates.append(_skipped(caliber, fetch, TIMEOUT))
                continue
            try:
                m = match_template(fetch.sql, list(caliber.fields), query.sql, query.dialect, schema=query.schema,
                                   names=query.names, columns=query.columns, deadline=deadline)
            except Exception:  # noqa: BLE001 - match_template 自己不抛；万一抛了，这一份照实记没比
                logger.exception("口径比对出错（运行查询 %s，口径 %s）", query.artifact[:12], caliber.key)
                candidates.append(_skipped(caliber, fetch, "比对出错"))
                continue
            cand = _candidate(caliber, fetch, m)
            if not m.equivalent and m.comparable and wanted is not None:
                try:
                    cand = _projected(caliber, fetch, query, wanted, deadline) or cand
                except Exception:  # noqa: BLE001 - 投影再比是附加的一步：出错照旧用整条比的结果
                    logger.exception("按引用的列投影再比出错（运行查询 %s，口径 %s）", query.artifact[:12], caliber.key)
            if chosen is not None and (caliber.key, fetch.node_id) == chosen:
                if cand["status"] == "proven":
                    cand["how"] = "composed"
                else:
                    cand.setdefault("reason", COMPOSED_UNPROVEN)
            candidates.append(cand)
        statuses = {c["status"] for c in candidates}
        status = "proven" if "proven" in statuses else "skipped" if "skipped" in statuses else "related"
        if status == "proven" and any(c.get("how") == "composed" for c in candidates):
            # 证明了的是助手选用的那一份：数来自口径，但不是这次独立写出来的（见模块说明）。同时和别的口径等价的也按选用的算
            status = "composed"
        if len(pool) > MAX_CANDIDATES:
            row["note"] = MORE_CANDIDATES.format(n=len(pool) - MAX_CANDIDATES, max=MAX_CANDIDATES)
        if status == "skipped":
            row["reason"] = next(c.get("reason") for c in candidates if c["status"] == "skipped")
        out.append({**row, "status": status, "candidates": candidates})
    return out


def compute(sealed: Any, calibers: list[Caliber], kinds: dict[str, str | None], *, timeout: float) -> dict[str, Any]:
    """一次运行的比对工件内容（见 ledger/store 的模块说明）。在线程里调：读快照、比 SQL、标数都是同步的活。"""
    from app.api.evidence import _choose, _reports
    from app.core.artifact_store import load

    deadline = time.monotonic() + timeout
    queries = read_queries(sealed, kinds)
    sealed_cards = {artifact for _, artifact in sealed.ledger}

    def load_card(artifact: str) -> dict[str, Any] | None:
        if artifact not in sealed_cards:
            return None
        try:
            card = load(artifact)
        except ValueError:
            return None
        return card if isinstance(card, dict) else None

    found = _reports(sealed)
    readable = [report for report in found if report.doc is not None and report.hash_ok is True]
    if not sealed.trusted:
        results = [{"artifact": q.artifact, "node_id": q.node_id, "source": q.source, "tables": sorted(q.tables),
                    "status": "skipped", "reason": SEAL_BROKEN, "candidates": []} for q in queries]
    else:
        referenced = ledger_labels.referenced_columns([report.doc for report in readable], load_card)
        results = compare_queries(queries, calibers, deadline=deadline, composed=composed_nodes(sealed.graph),
                                  referenced=referenced)
    used = {c["caliber"] for q in results for c in q["candidates"]}
    considered = [c for c in calibers if c.key in used]
    by_key = {c.key: {"level": c.level, "fields": c.summary()["fields"], "expressions": list(c.expressions)}
              for c in considered}
    by_artifact = {q["artifact"]: q for q in results}
    # 合并查询的快照不比（read_queries 跳过了它），引用了它的格照实写为什么没比对；不进工件的 queries，统计不数它
    for artifact in sealed.queries:
        by_artifact.setdefault(artifact, {"artifact": artifact, "status": "skipped", "reason": MERGED,
                                          "candidates": []})
    reports: dict[str, Any] = {}
    primary: str | None = None
    for report in readable:
        reports[report.node_id] = {"labels": ledger_labels.label_doc(report.doc, by_artifact, by_key, load_card)}
    if found:
        primary = _choose(found, None, sealed).node_id
    whole = ledger_labels.whole_of((reports.get(primary) or {}).get("labels") or {}, by_key) if primary in reports \
        else {"equivalent": False, "reason": "这次运行没有可读的报告", "counts": {k: 0 for k in ledger_labels.KINDS}}
    return {"format": store.FORMAT, "algo": store.ALGO, "run_id": sealed.run_id,
            "manifest_hash": sealed.manifest_hash, "manifest_seq": sealed.run_manifest_seq,
            "calibers": [c.summary() for c in considered], "queries": results, "reports": reports,
            "primary": primary, "whole": whole}


def _timed_out(sealed: Any) -> dict[str, Any]:
    """线程过了时限还没回来：整次记 skipped（每条查询都写比对超时），标数都是没比对。"""
    rows = [{"artifact": a, "node_id": (i or {}).get("node_id"), "source": str((i or {}).get("source") or ""),
             "tables": [], "status": "skipped", "reason": TIMEOUT, "candidates": []} for a, i in sealed.queries.items()]
    return {"format": store.FORMAT, "algo": store.ALGO, "run_id": sealed.run_id, "manifest_hash": sealed.manifest_hash,
            "manifest_seq": sealed.run_manifest_seq, "calibers": [], "queries": rows, "reports": {}, "primary": None,
            "whole": {"equivalent": False, "reason": TIMEOUT, "counts": {k: 0 for k in ledger_labels.KINDS}}}


# --------------------------------------------------------------------------
# 调度：后台、限时、限并发
# --------------------------------------------------------------------------

_POOL: ThreadPoolExecutor | None = None
#: 比对的空位，按事件循环分开（同 scheduler/auto_compare 的 _SLOTS）：测试每个用例一个新循环，跨循环用同一个信号量会报错
_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_background: set[asyncio.Task[Any]] = set()
#: 正在比的运行：同一次运行不重复排队（封存后、补比、按需几处可能同时要）
_pending: set[str] = set()


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="ledger-match")
    return _POOL


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _SLOTS:
        _SLOTS[loop] = asyncio.Semaphore(CONCURRENCY)
    return _SLOTS[loop]


async def _kinds(session: Any) -> dict[str, str | None]:
    """数据源名 → 类型（取方言）。只读应用库，不连业务库。"""
    return {name: kind for name, kind in (await session.execute(select(DataSource.name, DataSource.kind))).all()}


async def match_run(run_id: str, *, trigger: str = "sealed", timeout: float | None = None) -> str | None:
    """比对一次运行，存工件、追加 ledger.matched；返回工件 id。不比对（不是探索运行、没成功、没封存、没有查询）、比对期间
    运行换了封存时返回 None。比对本身出的问题都记成 skipped，不抛。timeout 不给按设置（ledger.match_timeout_s）。

    运行一封存就比的（trigger=sealed）比完接着看能不能按口径自动正式出具（ledger/issue.auto_issue，拍板 L1）：事后补比、
    按需比的不自动出具——那是按现在的口径回看老答案，不该替人补发正式运行。"""
    from app.api.evidence import _sealed
    from app.api.settings import ledger_settings

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if ineligible(run) is not None:
            return None
        if timeout is None:
            timeout = float((await ledger_settings(session))["match_timeout_s"])
    sealed = await _sealed(run_id)
    if not sealed.queries:
        return None
    async with _slot():
        async with SessionLocal() as session:
            calibers = await load_calibers(session)
            kinds = await _kinds(session)
        loop = asyncio.get_running_loop()
        work = loop.run_in_executor(_pool(), functools.partial(compute, sealed, calibers, kinds, timeout=timeout))
        try:
            content = await asyncio.wait_for(work, timeout=timeout + GRACE_S)
        except TimeoutError:
            logger.warning("运行 %s 的口径比对超过 %g 秒仍未完成，记为没比完", run_id, timeout + GRACE_S)
            content = _timed_out(sealed)
    artifact = await store.save(run_id, content, seal=(sealed.run_manifest_seq, sealed.manifest_hash), trigger=trigger)
    if artifact is not None and trigger == "sealed" and (content.get("whole") or {}).get("equivalent"):
        from app.ledger import issue

        await issue.auto_issue(run_id)
    return artifact


def schedule(run_id: str, *, trigger: str = "sealed") -> bool:
    """放到后台比对（runner 封存之后、按需）。已经在排队的同一次运行不再排；返回排没排上。"""
    if run_id in _pending:
        return False
    _pending.add(run_id)
    task = asyncio.create_task(_guarded(run_id, trigger))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True


def pending(run_id: str) -> bool:
    return run_id in _pending


async def _guarded(run_id: str, trigger: str) -> None:
    try:
        await match_run(run_id, trigger=trigger)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 比对是辅助步骤：出错只记日志，不影响运行和答案
        logger.exception("运行 %s 的口径比对出错", run_id)
    finally:
        _pending.discard(run_id)


async def backfill(*, days: int = BACKFILL_DAYS, limit: int = BACKFILL_LIMIT) -> int:
    """最近 days 天封存了、一次都没比过的探索运行，从新到旧逐个补比（trigger=backfill），返回补了几个。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with SessionLocal() as session:
        ids = [rid for (rid,) in (await session.execute(
            select(Run.id).where(Run.status == "succeeded", Run.run_class != "formal",
                                 Run.origin_kind.in_(EXPLORE_ORIGINS), Run.manifest_hash.is_not(None),
                                 Run.finished_at >= since)
            .order_by(Run.finished_at.desc()).limit(limit))).all()]
        done = await store.matched_run_ids(session, ids)
    count = 0
    for run_id in ids:
        if run_id in done or run_id in _pending:
            continue
        _pending.add(run_id)
        try:
            if await match_run(run_id, trigger="backfill"):
                count += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 一次补比出错不挡后面的
            logger.exception("运行 %s 的口径补比出错", run_id)
        finally:
            _pending.discard(run_id)
    if count:
        logger.info("启动时补比了 %d 次运行的口径", count)
    return count


def start_backfill() -> bool:
    """启动时在后台补比（engine/runner 的 setup 调）。和调度循环同一个开关（见模块说明）；返回起没起。"""
    from app.core.config import settings

    if not settings.scheduler:
        return False
    task = asyncio.create_task(_backfill_guarded())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True


async def _backfill_guarded() -> None:
    try:
        await backfill()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("启动时的口径补比出错，服务照常运行")


def _mine() -> list[asyncio.Task[Any]]:
    loop = asyncio.get_running_loop()
    return [t for t in _background if t.get_loop() is loop]


async def drain() -> None:
    """等这个事件循环里的后台比对都做完（测试用）。只等还没做完的：已经做完、还留在登记里的（完成回调还没来得及跑）顺手
    去掉——不然对着做完的任务反复 gather，事件循环原地空转（并行跑全量测试时偶发过）。"""
    while True:
        tasks = _mine()
        for task in tasks:
            if task.done():
                _background.discard(task)
        live = [task for task in tasks if not task.done()]
        if not live:
            return
        await asyncio.gather(*live, return_exceptions=True)


async def shutdown() -> None:
    """关停：这个事件循环里还没做完的比对取消掉（没做完的不记，下次启动补比会再比）。"""
    tasks = _mine()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["BACKFILL_DAYS", "CONCURRENCY", "EXPLORE_ORIGINS", "EXTRA_COLUMNS", "MAX_PROJECTIONS", "TIMEOUT_S", "Caliber",
           "Fetch", "Query", "backfill", "caliber_of", "compare_queries", "composed_nodes", "compute", "drain",
           "ineligible", "load_calibers", "match_run", "pending", "read_queries", "schedule", "shutdown",
           "start_backfill", "wanted"]
