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
- **运行期依赖**（contract.Context）：运行当天（服务端时区）、现查表的日期范围（W1-a 的 make_prober，没有这个模块时不给）、
  数据目录（按运行的目录视图读，和 db_schema 给模型看的同一份）、剖析事实。
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

    queries：编号 Qn → 输入快照（contract.InputSnapshot，masked 留空，用到时由 prepare 补）。results：编号 An → 整份
    结论；artifacts：An → 结论的工件 id。snapshot：发起时的分析设置快照（没有为 None，现读设置）。mask_cache：快照工件 →
    遮罩列；source_cache：数据源和表 → 目录、剖析事实（一次节点执行里各读一次，只在工具执行时用，不影响编号）。"""

    node_id: str
    snapshot: dict[str, Any] | None = None
    queries: dict[str, InputSnapshot] = field(default_factory=dict)
    results: dict[str, AnalysisResult] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    mask_cache: dict[str, list[str]] = field(default_factory=dict)
    source_cache: dict[str, Any] = field(default_factory=dict)

    def add_query(self, alias: str, snapshot: InputSnapshot) -> None:
        self.queries[alias] = snapshot

    def add_result(self, result: AnalysisResult, artifact: str) -> str:
        """记一份结论，返回它在本节点的编号（A1、A2……，按记下的先后）。"""
        alias = f"{contract.CATALOG_PREFIX}{len(self.results) + 1}"
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


def input_snapshot(content: Mapping[str, Any], *, ref: str, artifact: str, node_id: str) -> InputSnapshot:
    """一份查询快照（数据源查询工具交回的、工件库里的）→ 算子的一份输入（masked 留空，prepare 时补）。列的类型对齐成和
    columns 同序的列表（快照里记的是 {列: 类型}，没记的为 None）；行一律写成和 columns 同序的列表。"""
    columns = [str(c) for c in content.get("columns") or []]
    types = content.get("column_types")
    rows: list[list[Any]] = []
    for row in content.get("rows") or []:
        if isinstance(row, (list, tuple)):
            rows.append(list(row))
        elif isinstance(row, Mapping):
            rows.append([row.get(c) for c in columns])
        else:
            rows.append([row])
    return {
        "ref": ref, "artifact": artifact, "node_id": node_id, "source": str(content.get("source") or ""),
        "sql": str(content.get("sql") or ""), "columns": columns,
        "column_types": [types.get(c) if isinstance(types, Mapping) else None for c in columns],
        "rows": rows, "truncated": bool(content.get("truncated")), "masked": [],
    }


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
    """补上容器里每份输入快照的遮罩列（每份工件只认一次）。快照取不回来、和哈希对不上的照实报错：查询明明交回过，
    分析读不到它的快照，不是 agent 写错了参数。"""
    import asyncio

    from app.core import artifact_store

    for alias, snapshot in scope.queries.items():
        artifact = snapshot.get("artifact") or ""
        if artifact not in scope.mask_cache:
            try:
                content = await asyncio.to_thread(artifact_store.load, artifact)
            except ValueError as e:
                raise RuntimeError(f"{alias} 的查询快照与哈希不一致，疑似被修改，不能分析") from e
            if content is None:
                raise RuntimeError(f"{alias} 的查询快照已不存在，不能分析")
            scope.mask_cache[artifact] = await masked_of(content, artifact)
        snapshot["masked"] = list(scope.mask_cache[artifact])


# --------------------------------------------------------------------------
# 运行期依赖：运行当天、目录、剖析事实、现查表范围
# --------------------------------------------------------------------------


def _tables_of(snapshots: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """数据源名 → 这几份快照的 SQL 里用到的表（evidence.sql_tables，去掉 schema 前缀前的写法）。合并查询不是数据源。"""
    from app.engine.evidence import sql_tables
    from app.engine.merge_query import MERGE_SOURCE

    out: dict[str, set[str]] = {}
    for snap in snapshots:
        name = str(snap.get("source") or "")
        if not name or name == MERGE_SOURCE:
            continue
        try:
            tables = sql_tables(str(snap.get("sql") or ""))
        except Exception:  # noqa: BLE001 - 认不出表就只是少给一点目录
            tables = []
        out.setdefault(name, set()).update(str(t) for t in tables)
    return out


async def context_of(snapshots: Iterable[Mapping[str, Any]], *, tool_ctx: Any,
                     cache: dict[str, Any] | None = None) -> contract.Context:
    """算子要的运行期依赖（contract.Context）。

    - today：运行当天，服务端时区（算子据此判本期完不完整）
    - catalogs：数据源名 → {表名: 这张表的目录（app/data/catalog 的 notes）}，按运行的目录视图读（tool_ctx.catalog_view，
      和 db_schema 给模型看的、冻结进表结构快照的同一份），只给快照的 SQL 里用到的表
    - facts：数据源名 → {表名: 剖析事实（catalog_profile_jobs.last_facts）}；上传表格的剖析不是这次查的那一版的不给
    - probe：现查一列的实际范围（W1-a 的 make_prober，按这次运行固定的数据版本解析数据源；没有这个模块时为 None）

    tool_ctx 是查询用的那个 ToolContext（固定的数据版本、目录视图都从它取）。cache 是调用方的缓存（同一个节点里分析
    好几次，目录、剖析事实读一次）。读不到的源当没有：目录、剖析是附加说明，不能挡住分析。"""
    cache = cache if cache is not None else {}
    catalogs: dict[str, Any] = {}
    facts: dict[str, Any] = {}
    found: dict[str, Any] = {}
    for name, tables in _tables_of(snapshots).items():
        key = f"{name}\n" + "\n".join(sorted(tables))
        if key not in cache:
            cache[key] = await _source_context(name, tables, tool_ctx)
        entry = cache[key]
        if entry is None:
            continue
        found[name] = entry["source"]
        catalogs[name], facts[name] = entry["catalog"], entry["facts"]
    return {"today": date.today(), "probe": _prober(found, facts), "catalogs": catalogs, "facts": facts}


async def _source_context(name: str, tables: set[str], tool_ctx: Any) -> dict[str, Any] | None:
    """一个数据源这几张表的目录、剖析事实，和查询用的那个源（探查用）。读不到为 None。"""
    from app.data import catalog as data_catalog
    from app.data import catalog_view
    from app.data.catalog_profile_jobs import last_facts
    from app.db.base import SessionLocal
    from app.tools.datasource import resolved_source

    try:
        resolved = await resolved_source(name, tool_ctx)
        if resolved is None:
            return None
        row, source = resolved
        entries = await catalog_view.read(catalog_view.CatalogView.parse(getattr(tool_ctx, "catalog_view", None)), row)
        names = {data_catalog.resolve_table_name(getattr(source, "schema_cache", None), t) or t for t in tables}
        async with SessionLocal() as session:
            profiled = await last_facts(session, str(row.id), sorted(names)) if names else {}
    except Exception:  # noqa: BLE001 - 目录、剖析读不到当没有（目录视图指定的快照不在了另由查询工具照实报）
        logger.warning("分析读取数据源 %s 的目录、剖析事实失败，这次不附", name, exc_info=True)
        return None
    snapshot = getattr(source, "snapshot_id", None)
    return {
        "source": source,
        "catalog": {t: dict(entries[t].notes or {}) for t in sorted(names) if t in entries},
        # 上传表格：剖析时是另一版数据，范围对不上这次读的这一版（同 tools/datasource 的 _coverage）
        "facts": {t: f for t, f in profiled.items() if not (snapshot and f.get("snapshot_id") != snapshot)},
    }


def _probe_factory() -> Any:
    """W1-a 的 app.analysis.probe.make_prober；还没有这个模块时为 None（算子只用剖析事实，核对不了的写「核对不了」）。"""
    try:
        from app.analysis.probe import make_prober  # type: ignore[import-not-found]
    except ModuleNotFoundError as e:
        if e.name not in ("app.analysis.probe", "app.analysis"):
            raise
        return None
    return make_prober


def _prober(sources: Mapping[str, Any], facts: Mapping[str, Any]) -> contract.Prober | None:
    """按数据源名分派的探查（contract.Prober）。每个源第一次被问到时才建它自己的探查：

        make_prober(查询用的源, masked=遮罩列, cache=coverage.Cache(), facts=按表取剖析事实的协程函数)

    签名以 W1-a 的交付为准（合并时由编排者对齐这一处）。遮罩列上的不探（data/coverage 的规矩），探查失败由它自己退回
    剖析事实。"""
    make = _probe_factory()
    if make is None or not sources:
        return None
    from app.data import coverage
    from app.data.engine import masked_columns

    built: dict[str, Any] = {}

    async def probe(source: str, table: str, column: str) -> contract.Probe | None:
        if source not in built:
            obj = sources.get(source)
            known = (facts.get(source) or {}) if isinstance(facts, Mapping) else {}

            async def by_table(name: str, known: Mapping[str, Any] = known) -> Mapping[str, Any] | None:
                return known.get(name)

            built[source] = None if obj is None else make(
                obj, masked=masked_columns(getattr(obj, "options", None)), cache=coverage.Cache(), facts=by_table)
        found = built[source]
        return await found(source, table, column) if found is not None else None

    return probe


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


def hypothesis_of(value: Any) -> str | None:
    """agent（或模板作者）写的「这次在验证什么」：去掉首尾空白、截到 HYPOTHESIS_MAX 字；空的为 None。"""
    text = str(value or "").strip()
    return text[:HYPOTHESIS_MAX] if text else None


__all__ = [
    "ANALYSIS_TOOLS", "CALL_ERROR_HEAD", "REFUSED_CODE", "REPLY_HEAD", "SCOPE_KEY", "AnalysisScope", "call_error_text", "context_of",
    "hypothesis_of", "input_snapshot", "is_analysis_tool", "masked_of", "payload_of", "prepare", "reply_text",
    "run_snapshot", "scope_of", "settings_of", "store", "tool_context",
]
