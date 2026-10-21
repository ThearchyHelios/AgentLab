"""分析的入口共用的运行期部分（docs/design/analysis-round/00-总方案.md §3.2、§3.4、§3.10）。

两个入口——agent 分析工具（tools/builtin/analysis.py）、分析卡（engine/nodes/analysis.py）——和口径卡的可比性检查
（engine/nodes/metrics.py）都经这里取阈值、拼算子要的运行期依赖、认遮罩列、把结论落成工件，算子只经 app.analysis.run
调用，不各自另起一套：

- **本节点的查询容器**（AnalysisScope）：agent 建工具早于查询（engine/nodes/llm.py），工具经 ToolContext.extra[SCOPE_KEY]
  拿到这个可变容器。节点每记一次查询往里放一份输入快照（编号和交回模型的「【证据 Qn】」同一个），每记一份分析结论往里
  放一份（编号 An，下钻的 parent 按它找上一层）。只在记台账的地方往里放：工具调用在 @task 里，重放时不执行，记台账的
  那一步按 checkpoint 里的结果照样放一遍，编号还是同一套。
- **阈值**：发起时的快照（run.options["analysis"]，engine/launch.py）← 分析卡上写的覆盖；没有快照的现读设置。agent 改不了：
  分析工具的参数里没有阈值（契约 D4）。
- **运行期依赖**（contract.Context）：运行当天（服务端时区）、现查表的日期范围（analysis.make_prober，按这次运行固定的数据
  版本解析数据源）、数据目录（按运行的目录视图读，和 db_schema 给模型看的同一份）、剖析事实（analysis.probe.profile_facts，
  和查询工具的覆盖检查同一个取法）。
- **遮罩列**：数据源设的遮罩列、查询当时记下的、按 SQL 认出改了名字的（和证据面板、运行对比同一个函数，api/evidence 的
  _mask_sets 的 hidden，宁可多遮），加上遮罩运行里查询时就整列遮掉的。算子不拿它们当维度、度量。
- **结论**：整份落成内容寻址的工件（contract.ARTIFACT_KIND），台账记 contract.ledger_entry；交回 agent 的是一段短的
  「【分析 An】」加判定语、关键数、附带说明、算不了的原因。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.analysis import contract
from app.analysis.contract import AnalysisResult, CallError, InputSnapshot

logger = logging.getLogger(__name__)

#: ToolContext.extra 里放本节点分析容器（AnalysisScope）的键
SCOPE_KEY = "analysis_scope"
#: 五个分析工具的名字（contract.TOOL_NAMES）
ANALYSIS_TOOLS = frozenset(contract.TOOL_NAMES.values())
#: 交回 agent 的那一段开头的编号写法，和查询的「【证据 Qn】」一个样子
REPLY_HEAD = "【分析 {alias}】"
#: 调用写错时交回 agent 的那句的开头：说清没有记成结论，下一步照着改
CALL_ERROR_HEAD = "分析调用有误，没有记成结论："
#: agent 写的「这次在验证什么」最多留多少字（只在证据面板显示，不进正文）
HYPOTHESIS_MAX = 300
#: 一份分析算不了（status refused）时那条提醒的 code（运行日志）：两个入口同一个。复核信号按它认（engine/review.py 的
#: WARN_CODES，第 2 波登记）
REFUSED_CODE = "analysis_refused"


def is_analysis_tool(name: Any) -> bool:
    return isinstance(name, str) and name.strip() in ANALYSIS_TOOLS


def call_error_text(error: CallError) -> str:
    """调用写错（contract.CallError）交回模型的那句：不记结论、不进台账，模型照着 message 改了再调。"""
    return f"{CALL_ERROR_HEAD}{error.message}"


# --------------------------------------------------------------------------
# 本节点的查询容器
# --------------------------------------------------------------------------


@dataclass
class AnalysisScope:
    """一次节点执行里分析工具读的容器（agent 节点、协作成员的一次执行各一个）。

    queries：编号 Qn → 输入快照（contract.InputSnapshot；记台账时先放交回模型的那份 payload，分析前 prepare 换成工件库里
    那份完整快照，补上遮罩列）。results：编号 An → 整份结论；artifacts：An → 结论的工件 id。snapshot：发起时的分析设置快照
    （没有为 None，现读设置）。mask_cache：快照工件 → 遮罩列；source_cache：数据源和表 → 目录、查询用的源；tables_cache：
    SQL → 它用到的表（每次分析调用都要按本节点全部查询认表，同一条 SQL 只解析一次）；probe_cache：数据源名 →
    coverage.Cache（一次节点执行里各读、各查一次，只在工具执行时用，不影响编号）。"""

    node_id: str
    snapshot: dict[str, Any] | None = None
    queries: dict[str, InputSnapshot] = field(default_factory=dict)
    results: dict[str, AnalysisResult] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    mask_cache: dict[str, list[str]] = field(default_factory=dict)
    source_cache: dict[str, Any] = field(default_factory=dict)
    tables_cache: dict[str, tuple[str, ...]] = field(default_factory=dict)
    probe_cache: dict[str, Any] = field(default_factory=dict)
    #: 已经换成工件库里那份完整快照的编号（prepare 每份只取回一次）
    prepared: set[str] = field(default_factory=set)

    def add_query(self, alias: str, snapshot: InputSnapshot) -> None:
        self.queries[alias] = snapshot

    def next_alias(self) -> str:
        """下一份结论在本节点的编号：工具执行完、记台账之前，tool.end 先按它写上这份结论的编号（event_view）。"""
        return f"{contract.CATALOG_PREFIX}{len(self.results) + 1}"

    def add_result(self, result: AnalysisResult, artifact: str) -> str:
        """记一份结论，返回它在本节点的编号（A1、A2……，按记下的先后）。"""
        alias = self.next_alias()
        self.results[alias] = result
        self.artifacts[alias] = artifact
        return alias

    def resolve(self, name: str) -> InputSnapshot | None:
        return self.queries.get(str(name).strip())

    def parent(self, name: str) -> AnalysisResult | None:
        return self.results.get(str(name).strip())


def scope_of(extra: Mapping[str, Any] | None) -> AnalysisScope | None:
    scope = (extra or {}).get(SCOPE_KEY)
    return scope if isinstance(scope, AnalysisScope) else None


# --------------------------------------------------------------------------
# 阈值
# --------------------------------------------------------------------------


def run_snapshot(run_ctx: Any) -> dict[str, Any] | None:
    """这次运行发起时快照的分析设置（run.options["analysis"]，engine/launch.py 写，runner 放进 RunContext.extra 的
    run_options）。没有快照（图里本来没有分析、不经 launch_run 发起的）为 None。"""
    from app.engine.runner import RUN_OPTIONS_KEY

    options = (getattr(run_ctx, "extra", None) or {}).get(RUN_OPTIONS_KEY)
    snapshot = options.get(contract.SETTINGS_KEY) if isinstance(options, dict) else None
    return snapshot if isinstance(snapshot, dict) else None


async def settings_of(snapshot: Mapping[str, Any] | None,
                      overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """这次分析实际用的阈值：缺省 ← 发起时的快照（没有就现读设置）← 分析卡上的覆盖（contract.effective_settings）。"""
    if snapshot is None:
        from app.api.settings import analysis_settings   # 在这里导入：api.settings 导入了 engine 里的模块
        from app.db.base import SessionLocal

        async with SessionLocal() as session:
            snapshot = await analysis_settings(session)
    return contract.effective_settings(snapshot, overrides)


# --------------------------------------------------------------------------
# 输入快照与遮罩列
# --------------------------------------------------------------------------


def input_snapshot(content: Mapping[str, Any], *, ref: str, artifact: str, node_id: str,
                   masked: Iterable[str] = ()) -> InputSnapshot:
    """一份查询快照 → 算子的一份输入：查询工具的 payload（工件库里那份）原样——列、行、类型、SQL、数据源，连同 SQL 检查结果
    （checks）、「查询区间里这张表没有数据」（no_data）、查询当时记下的遮罩（mask_columns、masked_at_query），可比性检查按
    它们判数据问题——再加上出处（ref、artifact、node_id）和不能当维度、度量的列（masked，见 masked_of）。合并查询在 SQLite
    里执行，方言写明；数据源的方言算子按探查器认（prober.dialect）。"""
    from app.engine.merge_query import MERGE_SOURCE

    out: dict[str, Any] = {**content, "ref": ref, "artifact": artifact, "node_id": node_id, "masked": list(masked)}
    if content.get("source") == MERGE_SOURCE and not content.get("dialect"):
        out["dialect"] = "sqlite"
    return out  # type: ignore[return-value]


async def masked_of(content: Mapping[str, Any] | None, artifact: str | None) -> list[str]:
    """一份查询快照里不能当维度、度量的列（小写）：结果里取自遮罩列的列按快照的列序在前，其余遮罩列名在后。

    认法和证据面板查询步骤、运行对比同一个函数（api/evidence 的 _mask_sets 的 hidden：查询当时记下的加数据源现在设的，
    SQL 里给遮罩列起了别名、套了函数的结果列也算，SQL 认不准时整份的列都算，合并查询按它的来历认）。遮罩运行
    （mask_results）里查询时就遮掉的列（masked_at_query、整份遮的 masked_whole）格子里已经是「已遮罩」，一并算上。"""
    from app.api.evidence import _Masks   # 在这里导入：api.evidence 导入了 engine 里的模块

    if not isinstance(content, Mapping):
        return []
    hidden = set((await _Masks().sets(artifact, content.get("source"), dict(content))).hidden)
    columns = [str(c) for c in content.get("columns") or []]
    hidden |= {str(c).lower() for c in content.get("masked_at_query") or [] if isinstance(c, str)}
    if content.get("masked_whole"):
        hidden |= {c.lower() for c in columns}
    shown = [c.lower() for c in columns if c.lower() in hidden]
    return list(dict.fromkeys([*shown, *sorted(hidden - set(shown))]))


async def prepare(scope: AnalysisScope) -> None:
    """分析前把容器里的每份输入换成工件库里那份完整的查询快照（取回时复验哈希），补上遮罩列（每份工件只认一次）。快照
    取不回来、和哈希对不上的照实报错：查询明明交回过，分析读不到它的快照，不是 agent 写错了参数。"""
    import asyncio

    from app.core import artifact_store

    for alias, snapshot in list(scope.queries.items()):
        if alias in scope.prepared:
            continue
        artifact = str(snapshot.get("artifact") or "")
        try:
            content = await asyncio.to_thread(artifact_store.load, artifact)
        except ValueError as e:
            raise RuntimeError(f"{alias} 的查询快照与哈希不一致，疑似被修改，不能分析") from e
        if not isinstance(content, Mapping):
            raise RuntimeError(f"{alias} 的查询快照已不存在，不能分析")
        if artifact not in scope.mask_cache:
            scope.mask_cache[artifact] = await masked_of(content, artifact)
        scope.queries[alias] = input_snapshot(content, ref=alias, artifact=artifact,
                                              node_id=str(snapshot.get("node_id") or scope.node_id),
                                              masked=scope.mask_cache[artifact])
        scope.prepared.add(alias)


# --------------------------------------------------------------------------
# 运行期依赖：运行当天、目录、剖析事实、现查表范围
# --------------------------------------------------------------------------


def _tables_of(snapshots: Iterable[Mapping[str, Any]],
               memo: dict[str, tuple[str, ...]] | None = None) -> dict[str, set[str]]:
    """数据源名 → 这几份快照的 SQL 里用到的表（evidence.sql_tables，去掉 schema 前缀前的写法）。合并查询不是数据源。
    memo 是 SQL → 表的缓存（调用方的，同一个节点里分析好几次，每条 SQL 只解析一次）。"""
    from app.engine.evidence import sql_tables
    from app.engine.merge_query import MERGE_SOURCE

    memo = memo if memo is not None else {}
    out: dict[str, set[str]] = {}
    for snap in snapshots:
        name = str(snap.get("source") or "")
        if not name or name == MERGE_SOURCE:
            continue
        sql = str(snap.get("sql") or "")
        if sql not in memo:
            try:
                memo[sql] = tuple(str(t) for t in sql_tables(sql))
            except Exception:  # noqa: BLE001 - 认不出表就只是少给一点目录
                memo[sql] = ()
        out.setdefault(name, set()).update(memo[sql])
    return out


async def context_of(snapshots: Iterable[Mapping[str, Any]], *, tool_ctx: Any,
                     cache: dict[str, Any] | None = None,
                     probe_cache: dict[str, Any] | None = None,
                     tables_cache: dict[str, tuple[str, ...]] | None = None) -> contract.Context:
    """算子要的运行期依赖（contract.Context）。每次分析调用一份（探查器有总时限，一次调用建一个）。

    - today：运行当天，服务端时区（算子据此判本期完不完整）
    - catalogs：数据源名 → {表名: 这张表的目录（app/data/catalog 的 notes）}，按运行的目录视图读（tool_ctx.catalog_view，
      和 db_schema 给模型看的、冻结进表结构快照的同一份），只给快照的 SQL 里用到的表
    - facts：数据源名 → 取剖析事实的函数（analysis.probe.profile_facts：上传表格的剖析不是这次查的那一版的不给）
    - probe：analysis.make_prober(数据源名 → 查询用的源)：按这次运行固定的数据版本解析数据源（上传表格查的是固定的那一版），
      遮罩列按数据源设的不探，探不成由算子退回剖析事实；一个源都解析不出来时为 None

    tool_ctx 是查询用的那个 ToolContext（固定的数据版本、目录视图都从它取）。cache 是调用方的缓存（同一个节点里分析好几次，
    目录、数据源读一次）；tables_cache 是 SQL → 用到的表（同一条 SQL 一个节点里只解析一次：agent 每次分析都按本节点全部
    查询认表）；probe_cache 是数据源名 → coverage.Cache（同一张表的范围一个节点里只查一次）。读不到的源当没有：目录、剖析、
    探查都是附加的，不能挡住分析。"""
    from app.analysis.probe import profile_facts

    cache = cache if cache is not None else {}
    catalogs: dict[str, Any] = {}
    facts: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    for name, tables in _tables_of(snapshots, tables_cache).items():
        key = f"{name}\n" + "\n".join(sorted(tables))
        if key not in cache:
            cache[key] = await _source_context(name, tables, tool_ctx)
        entry = cache[key]
        if entry is None:
            continue
        sources[name] = entry["source"]
        catalogs[name] = entry["catalog"]
        facts[name] = profile_facts(entry["source"])
    return {"today": date.today(), "probe": _prober(sources, probe_cache), "catalogs": catalogs, "facts": facts}


async def _source_context(name: str, tables: set[str], tool_ctx: Any) -> dict[str, Any] | None:
    """一个数据源这几张表的目录，和查询用的那个源（探查、剖析事实都按它）。读不到为 None。"""
    from app.data import catalog as data_catalog
    from app.data import catalog_view
    from app.tools.datasource import resolved_source

    try:
        resolved = await resolved_source(name, tool_ctx)
        if resolved is None:
            return None
        row, source = resolved
        entries = await catalog_view.read(catalog_view.CatalogView.parse(getattr(tool_ctx, "catalog_view", None)), row)
    except Exception:  # noqa: BLE001 - 目录读不到当没有（目录视图指定的快照不在了另由查询工具照实报）
        logger.warning("分析读取数据源 %s 的目录失败，这次不附", name, exc_info=True)
        return None
    names = {data_catalog.resolve_table_name(getattr(source, "schema_cache", None), t) or t for t in tables}
    return {"source": source, "catalog": {t: dict(entries[t].notes or {}) for t in sorted(names) if t in entries}}


def _prober(sources: Mapping[str, Any], probe_cache: dict[str, Any] | None) -> contract.Prober | None:
    """这次分析调用的探查器（analysis.make_prober）。遮罩列、剖析事实按缺省（数据源设的遮罩列；profile_facts 现读）；
    coverage.Cache 按数据源名放在调用方给的 probe_cache 里，同一个节点里的几次分析共用。"""
    if not sources:
        return None
    from app import analysis
    from app.data import coverage

    shared = probe_cache if probe_cache is not None else {}
    return analysis.make_prober(dict(sources), cache={name: shared.setdefault(name, coverage.Cache())
                                                      for name in sources})


def tool_context(run_ctx: Any, node_id: str, emit: Any = None) -> Any:
    """分析卡、口径卡没有自己的 ToolContext：拼一个只用来解析数据源的（固定的数据版本、目录视图、遮罩、只读同运行）。
    记忆范围、知识库用不着，不读节点配置。"""
    from app.data import catalog_view
    from app.tools.datasource import run_masks_results, run_reads_only, run_scope_fixed, run_versions
    from app.tools.registry import ToolContext

    return ToolContext(run_id=run_ctx.run_id, node_id=node_id, sandbox_session=run_ctx.thread_id,
                       data_versions=run_versions(run_ctx), mask_results=run_masks_results(run_ctx),
                       force_readonly=run_reads_only(run_ctx), fixed_scope=run_scope_fixed(run_ctx),
                       catalog_view=catalog_view.of_run(run_ctx), emit=emit)


# --------------------------------------------------------------------------
# 结论：落成工件、交回模型
# --------------------------------------------------------------------------


async def store(result: AnalysisResult, *, run_id: str, node_id: str) -> str:
    """整份结论落成内容寻址的工件（contract.ARTIFACT_KIND）。复算、证据面板、导出都按这个 id 取回。"""
    from app.core.artifact_store import put_json

    return await put_json(result, kind=contract.ARTIFACT_KIND, run_id=run_id, node_id=node_id,
                          meta={"op": result.get("op"), "status": result.get("status")})


def payload_of(content: Any) -> dict[str, Any] | None:
    """分析工具交回的 JSON（文本或对象）→ {artifact, result}；不是一份分析结论的返回 None。"""
    data = content
    if isinstance(data, str):
        if not data.lstrip().startswith("{"):
            return None
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    artifact, result = data.get("analysis_artifact"), data.get("result")
    if not (isinstance(artifact, str) and artifact and isinstance(result, dict) and result.get("op") in contract.OPS):
        return None
    return {"artifact": artifact, "result": result}


def reply_text(alias: str, result: AnalysisResult) -> str:
    """交回模型的那一段：「【分析 An】」加一个紧凑的 JSON——判定、判定语、判定标准、关键数、附带说明、算不了的原因、
    结果表的前几段（列几段照结论里记的 table_top_k）。整份结论在工件里，模型要的是能接着往下做的那几样。"""
    verdict = result.get("verdict") or {}
    body: dict[str, Any] = {"status": result.get("status"), "op": result.get("op")}
    if result.get("mode"):
        body["mode"] = result.get("mode")
    if result.get("status") == "ok":
        body.update(verdict=verdict.get("code"), subjects=verdict.get("subjects") or [],
                    direction=verdict.get("direction"))
    else:
        body.update(reason_code=result.get("reason_code"), reason=result.get("reason"))
    body["phrase"] = result.get("phrase")
    if result.get("criterion"):
        body["criterion"] = result.get("criterion")
    if result.get("values"):
        body["values"] = result.get("values")
    if caveats := [c.get("text") for c in result.get("caveats") or [] if isinstance(c, dict) and c.get("text")]:
        body["caveats"] = caveats
    table = result.get("table")
    if isinstance(table, dict) and table.get("rows"):
        top = int((result.get("settings") or {}).get("table_top_k") or contract.DEFAULTS["table_top_k"])
        rows = list(table.get("rows") or [])
        body["table"] = {"columns": table.get("columns"), "rows": rows[:top],
                         **({"more_rows": len(rows) - top} if len(rows) > top else {})}
    return REPLY_HEAD.format(alias=alias) + "\n" + json.dumps(body, ensure_ascii=False, default=str)


def event_view(result: AnalysisResult, alias: str | None = None) -> dict[str, Any]:
    """分析工具的 tool.end 上带的那一小段（tool.end 的 analysis）：编号、算子、模式、状态、判定码、方向、判定语、算不了的原因码。
    preview 只是结论 JSON 的前一截（截到 2000 个字符），判定语多半在截掉的那段里；时间线照这一段显示，不从 preview 里拼。
    编号是这份结论在本节点的编号（「【分析 An】」那个，AnalysisScope.next_alias），不记台账的执行里没有编号，为 None。
    判定语是系统渲染的（遮罩列不当维度、度量，里面没有遮罩值）；Agent 写的假设不带。"""
    verdict = result.get("verdict") or {}
    return {"alias": alias, "op": result.get("op"), "mode": result.get("mode"), "status": result.get("status"),
            "verdict": verdict.get("code"), "direction": verdict.get("direction"), "phrase": result.get("phrase") or "",
            "reason_code": result.get("reason_code")}


def hypothesis_of(value: Any) -> str | None:
    """agent（或模板作者）写的「这次在验证什么」：去掉首尾空白、截到 HYPOTHESIS_MAX 字；空的为 None。"""
    text = str(value or "").strip()
    return text[:HYPOTHESIS_MAX] if text else None


__all__ = [
    "ANALYSIS_TOOLS", "CALL_ERROR_HEAD", "REFUSED_CODE", "REPLY_HEAD", "SCOPE_KEY", "AnalysisScope", "call_error_text", "context_of",
    "event_view", "hypothesis_of", "input_snapshot", "is_analysis_tool", "masked_of", "payload_of", "prepare", "reply_text",
    "run_snapshot", "scope_of", "settings_of", "store", "tool_context",
]
