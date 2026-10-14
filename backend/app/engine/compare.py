"""运行对比：同一个工作流的两次运行（本周和上周的周报），把每个指标的变化拆清楚——是数据变了、口径变了，
还是模板 / SQL 变了；数据变化再按维度归因。每一项都追得到两边的查询快照。

只读：这里只算，不读库、不写工件、不追加事件。接口（api/compare.py）把两次运行封存范围内的事件、工件的读法和
遮罩准备好交进来（Side），结果原样交回给发请求的人。

怎么对：

- 查询：两次运行各自封存范围内交回过的查询快照，按「节点 + 该节点里第几次查询 + 工具」配对，配不上的再按规范化
  SQL（data/sql_semantics.normalize_sql）配；同一节点、同一工具里规范化 SQL 相同的先配上，字面量也相同的又在前
  （Agent 每次查询的先后可能不同，见 _pair）。结构：规范文本和字面量都相同是 same；规范文本相同、只有字面量不同是
  params_only（定时运行每次的日期不同，属于参数变化不是模板变化）；否则 changed（数据源换了也算）。核查类的查询
  （双盲复算、反方审稿发的 role: verify）不进报告，不参与对比。
- 口径卡：同一个口径卡节点按指标 id 配对；循环里执行了几次的取最后一次。表达式、口径版本、小数位、单位、比率与否
  有一样不同就是口径变了：小数位改变取整，单位、比率与否改变数值的含义，都不只是换个写法。千分位和不分组互换只影响
  显示，写进说明、不算口径变化。升级前的卡没记小数位、显示格式的，按那次运行图里的指标定义核对，定义也没有就不比。
- 每个指标一个分类，按这个次序判：一边有一边没有（added / removed）；口径变了（caliber_and_data，可能连同数据；
  喂它的取数也有变化的照样写进说明）；喂它的查询结构变了、上游节点改了配置（sql_changed，标出、不拆）；输入对不上、
  说不清变化从哪来（incomparable）；只有参数变了（params_only，含运行输入）；都没变时数值相等是 unchanged，
  不等是 data_only。
- 「喂它的查询」按输入的出处认：
  - cell() 那一格、Agent 按出处交来的字段落在哪份快照上就是哪条查询，两边各自的快照直接比 SQL；快照是合并查询的
    结果时，再按别名一路追到喂它的每一份上游快照。只差字面量时还要看图里的 SQL：图里的 SQL 改过（改了写死的筛选值、
    系数）是模板变化，图里一字未改的才是运行时代入的参数
  - Agent 按出处交来的字段还要引用同一列、同一段行（两边都引用整份结果的不算换了；行段相同、一边是整份结果一边
    不是的算换了；行号相同、都不是整份结果的，按维度值数两边各引用了它几行、引用的行外面还有没有它的行：引用过的
    每个维度值两边要被切在同一处——都整个被引用（一边的快照里根本没有它的，那一边算整个被引用），或者都只引用了
    它同样的那几行（排在引用的行前面的行数、引用的行数都一样）——否则也算换了：取前几名时名次变了、按区域排序时
    多了一个区域、区域 × 日期的结果里一个区域有一天被挤出引用的行、一个区域多了一天却只引用了前几天。维度按两边
    整份结果认一组、按归因用的行（引用的那段行）再认一组，两组都要切在同一处——归因按哪组维度说，就按哪组核对；
    单格引用的那一行两次要是同一个维度值，还要是它同样的那一行（排在这一行前面的、它的行数一样）；快照读不出来、
    有的行和列对不上，核对不了是不是同一批维度值的，说不清）：取哪一列、哪几行是 Agent 每次运行时定的，换了就不是
    同一个数
  - 不落在快照上的：模型、人交出的值（Agent 没按出处交、模型调用、人工审批……）没有出处，值变了说不清是数据还是它
    本身的差异；代码、数据整形这类节点看它自己查过的库，不查库的看它的配置和画布上的全部上游；记不出写入者的看
    口径卡之前可能执行过的节点（口径卡自己、只在它下游的不算）。上游的查询结果两次一模一样、值却变了的同样说不清；
    上游有模型、人交出值的节点、两次交出的不同的也说不清
  - 图级的变化落到节点上：连线有增删的，终点节点算改了；默认设置改了的，运行时会读这几项（_CFG_KEYS）、又没有自行
    设置的节点算改了
- 严格拆分（只对口径变了的）：旧表达式的每个叶子路径都能在这次的口径卡输入里取得值时，
  口径效应 = f_new(x_new) − f_old(x_new)，数据效应 = f_old(x_new) − f_old(x_old)。f_old(x_new) 用推演重算
  （engine/recompute.py）的还原和求值算，取整按旧口径卡的小数位；三个数按十进制精确相减，两效应之和逐位等于
  总变化（转成浮点后也逐位相加，见 _split_exact）。取数的 SQL 也变了、用到了遮罩列、单位不同的不拆，说明为什么。
- 维度归因：指标的输入落在查询快照的某一格（或 Agent 数组字段的一段行）、两边快照都没截断时，取快照里不是数值的
  列作维度（遮罩的、日期类型的不算；两边都只有一个值的、两边没有一个值相同的——日期、期次这类每期都不同的列——
  也不拿来配对），按维度键配对两边的行，算每个键的 base / target / delta，按 |delta| 取前 10。只有度量列之和
  等于这个输入的值（两边都等）时才给占比；取的是合计行那一格时，其余各行之和等于它也算，合计行不列进去。
- 遮罩列的值绝不出现在结果里：取自遮罩列的输入不给值，遮罩列不当维度、不当度量，用到遮罩列的指标不拆分（拆出来的
  f_old(x_new) 配上公开的数就能反推出遮住的那一格）。
"""
from __future__ import annotations

import ast
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.events import EventType
from app.data.sql_semantics import NormalizedSql, diff_facets, facets, normalize_sql
from app.engine import recompute
from app.engine.evidence import DEFAULT_FORMAT, RenderError, is_number, render_number
from app.engine.expressions import ExpressionError, cell_value, leaf_refs, parse_expression
from app.engine.labels import TYPE_LABEL, field_label
from app.engine.merge_query import MERGE_SOURCE
from app.engine.nodes.metrics import _REF_HEAD, _display_of
from app.engine.toolcalls import QUERY_PREFIX

UNCHANGED = "unchanged"
DATA_ONLY = "data_only"
CALIBER_AND_DATA = "caliber_and_data"
SQL_CHANGED = "sql_changed"
PARAMS_ONLY = "params_only"
ADDED = "added"
REMOVED = "removed"
INCOMPARABLE = "incomparable"
#: summary 里的各类，次序同接口约定
CLASSES = (UNCHANGED, DATA_ONLY, CALIBER_AND_DATA, SQL_CHANGED, PARAMS_ONLY, ADDED, REMOVED, INCOMPARABLE)
#: 这几类才做维度归因：数据（连同参数、口径）变了，两边的行说的是同一回事
_ATTRIBUTED = frozenset({DATA_ONLY, PARAMS_ONLY, CALIBER_AND_DATA})

#: 维度归因最多列几个维度值（按变化的绝对值从大到小）
TOP_KEYS = 10
#: 一条查询的结构差异最多写几条（diff_facets 已按对结果的影响从大到小排）
MAX_DIFFERENCES = 5
#: 表达式、取值写进说明时最多几个字
_SHOWN_MAX = 60
#: 说明里最多列几个行号：一个维度值挤出去几十行时，前几个就够人去快照里找
_ROWS_SHOWN = 3
#: 比较 SQL 时两边的叫法（diff_facets 的 labels）
_LABELS = ("基准", "这次")
#: 合并查询的 SQL 在 SQLite 里执行（engine/merge_query.py）
_MERGE_DIALECT = "sqlite"
#: 日期类的列不当维度：每期都不同，按它配对两边的行一个都配不上
_TEMPORAL = frozenset({"date", "datetime", "time"})
_FORMAT_LABEL = {"thousands": "千分位", "plain": "不分组", "percent_of_ratio": "比率按百分数显示"}
_RATIO = "percent_of_ratio"
#: 图里写 SQL 的配置项：调用工具节点的参数、合并查询的合并 SQL
_SQL_KEY = {"tool": "args.sql", "merge": "sql"}
#: 交出的值由模型或人给出的节点（类型、口径卡记的来历）：同样的输入、同样的配置，交出的值也可能不同
_GENERATED = frozenset({"agent", "llm", "supervisor", "human", "memory", "retrieve", "subgraph", "report"})
_GENERATED_VIA = frozenset({"agent", "agent_field", "llm", "team", "human", "memory", "retrieval", "subgraph",
                            "report"})
#: 调模型时读的配置项（nodes/llm._model_spec）
_MODEL_KEYS = frozenset({"provider", "model", "temperature", "max_tokens", "thinking", "effort"})
#: 各类节点运行时按 NodeContext.cfg 读的配置项：节点上没写这一项就回退到图级默认设置（GraphSpec.defaults），默认
#: 设置改了这一项，没自行设置它的这类节点交出的值可能跟着变；不读它的不受影响（结论句裁判只有报告节点读）。读了也
#: 不影响交出的值的见 _QUIET_DEFAULTS。合并查询、双盲复算、反方审稿不经 cfg 读配置。执行器新读了一项要在这里登记，
#: tests/test_compare_engine.py 按各类节点的执行器（连同它用到的函数）的源码逐类核对
_CFG_KEYS: dict[str, frozenset[str]] = {
    "input": frozenset({"fields"}),
    # 出具契约按报告节点同一套参数重建证据目录：后三项读的是报告节点上的，报告节点没写同样回退到默认设置。成果节点
    # 自己从不写这三项，默认设置改了就算它跟着变（报告节点自己写了的也算，宁可多算）
    "output": frozenset({"fields", "contract", "evidence_from", "metrics_from", "entities"}),
    "transform": frozenset({"mode", "expression", "template", "assign_to"}),
    "llm": _MODEL_KEYS | {"system", "skills", "use_history", "prompt", "output_schema", "emit_message", "assign_to"},
    "agent": _MODEL_KEYS | {"system", "skills", "use_history", "prompt", "output_schema", "cite_fields", "tools",
                            "memory_scope", "collection", "parallel_tools", "max_steps", "budget_tokens",
                            "budget_usd", "approval", "emit_message", "assign_to"},
    "supervisor": frozenset({"agents", "max_rounds", "on_exhausted", "goal", "provider", "model", "max_tokens",
                             "max_parallel", "approval", "emit_message", "assign_to"}),
    "subgraph": frozenset({"workflow_id", "workflow_version", "input", "assign_to"}),
    "tool": frozenset({"tool", "args", "fail_fast", "memory_scope", "collection", "approval", "assign_to", "assignee"}),
    "code": frozenset({"code", "evidence_role", "language", "timeout", "memory_mb", "network", "isolation", "files",
                       "fail_fast", "approval", "assign_to"}),
    "branch": frozenset({"cases", "mode"}),
    "loop": frozenset({"mode", "max_iterations", "item_var", "items", "condition"}),
    "human": frozenset({"mode", "title", "message", "schema", "context", "draft", "stop_on_reject", "assign_to",
                        "assignee", "policy", "due", "on_timeout", "form", "form_from"}),
    "validate": frozenset({"schema", "source", "max_retries", "repair_with_llm", "fail_fast", "assign_to"}),
    "memory": frozenset({"action", "scope", "query", "limit", "kind", "content", "importance", "assign_to"}),
    "retrieve": frozenset({"query", "collection", "limit", "alpha", "rerank", "min_score", "assign_to"}),
    "metrics": frozenset({"metrics", "caliber", "caliber_version", "on_missing", "assign_to"}),
    "report": _MODEL_KEYS | {"numbers", "claims", "judge", "entities", "max_repairs", "system", "instructions",
                             "on_violation", "evidence_from", "metrics_from", "emit_message", "assign_to"},
}
#: 只在调模型时才读的（_calls_model）：条件分支让模型分类、结构校验让模型修复（默认开着）、知识检索用模型重排
_CFG_KEYS_IF: dict[str, frozenset[str]] = {
    "branch": frozenset({"input", "instruction", "provider", "model"}),
    "validate": frozenset({"provider", "model"}),
    "retrieve": _MODEL_KEYS,
}
#: 改了也不影响交出的值的默认设置：审批策略只管调用工具前问不问人
_QUIET_DEFAULTS = frozenset({"approval"})
#: 配置键的叫法，engine/labels 里没有的这两个：节点类型；模型服务叫「模型接入」（lib/terms.ts），不写 provider
_KEY_LABEL = {"type": "节点类型", "provider": "模型接入"}
#: 浮点能原样写回的十进制有效数字位数（DBL_DIG）
_FLOAT_DIGITS = 15

NO_CARDS = "两次运行都没有口径卡，没有可以对比的指标"
EDGES_CHANGED = "节点之间的连线有变化"
DEFAULTS_CHANGED = "工作流默认设置有变化"
MASKED_NOTE = "部分列在数据源中设置了遮罩，对比中不显示这些列的值，也不用它们做拆分和归因"
NO_LINEAGE = "口径卡没有记录输入（生成于系统升级前），无法判断变化来源"
DATA_FROM_QUERIES = "取数的 SQL 没有变化，查询结果有变化"
DATA_FROM_UPSTREAM = "模板和取数的 SQL 都没有变化，变化来自上游数据"
#: 图有别的改动、都不在它的上游：不能说「模板没有变化」
DATA_FROM_UPSTREAM_SCOPED = "它上游的节点和取数的 SQL 都没有变化，变化来自上游数据"
SQL_NOT_SPLIT = "取数的 SQL 或上游配置有变化，变化无法在口径与数据之间拆分"
NOT_NUMBERS = "有一边的值不是数值，无法拆分"
SPLIT_MASKED = "用到了设置了遮罩的列，不做拆分"
NO_DIFFERENCE = "查询的写法有变化（选取的列、计算方式等）"
TRUNCATED = "查询结果被截断（{side}只取回了前 {rows} 行），不做维度归因"
MASKED_MEASURE = "度量列「{column}」设置了遮罩，不做维度归因"
UNREADABLE_SNAPSHOT = "查询快照无法读取，不做维度归因"
NOT_A_MEASURE = "度量列「{column}」有不是数值的格，不做维度归因"
NO_SHARE = "各维度值的度量之和与输入的值不相等，变化仅供参考，不给占比"
ZERO_TOTAL = "总变化为 0，不计算占比"
TOTAL_ROW = "输入取自合计行（第 {row} 行），按其余各行归因"
MORE_KEYS = "共 {count} 个维度值，列出变化最大的 {top} 个"
NO_OVERLAP = "列「{columns}」两边没有相同的值（可能是日期、期次这类每期都不同的列），不按它配对"


@dataclass
class Side:
    """一次运行在对比里要用到的东西，接口从这次运行的封存范围里准备好。

    - run：运行的概要（id、工作流名、版本、类别、起止时间、定时任务），原样放进结果
    - graph：运行时的图快照；events：封存范围内的事件 (seq, type, node_id, data)
    - cards：封存范围内复验过哈希的口径卡 (节点, 工件, 指标集)（api/evidence._sealed_cards）。事件里记着、这里
      没有的，就是读不出来或者被改过
    - load：工件 id → 内容，只认封存范围内的；文件不在返回 None，内容和哈希对不上抛 ValueError
    - masked：查询快照 → 要遮的列（小写）：查询当时记下的加数据源现在设的
    - sources：口径卡那次执行开始的事件序号 → 推演重算还原输入要用的 Sources（节点产出、跑完的时刻都按那一刻）。
      不给就按 load、masked 拼一个最小的：nodes.* 这类容器输入还原不出来
    - dialects：数据源名 → 类型（sqlite / postgres …），规范化 SQL 时按它的方言解析
    - inputs：运行输入（Run.input）
    """

    run: dict[str, Any]
    graph: dict[str, Any]
    events: list[tuple[int, str, str | None, Any]]
    cards: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    load: Callable[[str], Any] = lambda _: None
    masked: Mapping[str, frozenset[str]] = field(default_factory=dict)
    sources: Callable[[int], recompute.Sources] | None = None
    dialects: Mapping[str, str | None] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)


def compare(base: Side, target: Side) -> dict[str, Any]:
    """对比两次运行（base 是基准，target 是这次），只读，不碰两次运行的任何东西。返回：

    - target / base：两次运行的概要（Side.run 原样）
    - summary：各类指标的个数，键是 CLASSES 里的八类
    - template：{base_version, target_version, same_graph, nodes: [{node_id, title, change: added | removed | config,
      keys: [配置键名，值是字典的往下一层写 args.sql], labels: [同序的中文叫法]}]}。只列键名，不展示配置的值
    - metrics：每个指标一项 {card_id, card_title, metric_id, name, unit, decimals, base, target（有限的数，否则
      None）, base_rendered, target_rendered（卡上记的写法）, delta, delta_rendered（为 0、不能相减时为 None）,
      delta_pct（相对基准的百分比，两位小数；基准为 0 时为 None）, classification, reasons: [说明], decomposition:
      {caliber_effect, data_effect, caliber_rendered, data_rendered} 或 None, decomposition_note（为什么没拆）,
      inputs: [{path, base, target, masked?, base_artifact?, target_artifact?}], attribution}
    - classification 按这个次序取第一个对得上的：added / removed → caliber_and_data → sql_changed → incomparable
      → params_only → unchanged / data_only。口径变了的指标喂它的取数也变了时，分类仍是 caliber_and_data，取数的
      变化接在口径的说明后面写进 reasons
    - attribution：{path, base_artifact, target_artifact（归因用的是哪个输入、两边各自哪份快照）, columns, measure,
      rows: [{key: [维度值…], base, target, delta, share}], total_delta, note} 或 None。归不了因的（快照截断、
      度量列遮罩、快照读不出来、维度列每期都不同）rows 为空、total_delta 为 None，原因写在 note；一边没有的维度值
      那一边写 None
    - queries：每对查询一项 {node_id, node_title, ordinal, structure, base_sql, target_sql, literals: [{base,
      target}]（只列不同的字面量）, base_rows, target_rows, base_truncated, target_truncated, base_artifact,
      target_artifact, differences: [结构上差在哪], problem?（快照读不出来、被改过）}
    - notes：整体的说明：连线、默认设置、运行输入的变化，口径卡执行了几次、读不出来，遮罩
    """
    return _Compare(base, target).result()


# --------------------------------------------------------------------------
# 查询：收集、配对、比结构
# --------------------------------------------------------------------------


@dataclass
class _Query:
    """一次运行里交回过的一份查询快照（核查类的不算）。"""

    artifact: str
    node_id: str
    tool: str | None
    #: 这个节点里第几次查询，从 1 数（循环里执行几次的接着数）
    ordinal: int
    call_id: str | None = None
    #: 快照里的 SQL；快照读不出来时取 tool.start 的参数（合并查询取 merge.end 的）
    sql: str | None = None
    snap: dict[str, Any] | None = None
    #: 快照读不出来的原因：missing / tampered
    problem: str | None = None
    source: str | None = None
    #: 合并查询的各个输入 [{alias, node_id, artifact, …}]：快照里记的，快照读不出来时取 merge.end 的
    inputs: list[dict[str, Any]] | None = None


def _snapshot(load: Callable[[str], Any], artifact: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        content = load(artifact)
    except ValueError:
        return None, "tampered"
    if isinstance(content, dict) and isinstance(content.get("rows"), list):
        return content, None
    return None, "missing"


def _queries(side: Side) -> list[_Query]:
    """封存范围内交回过的查询快照，按交回的先后。

    调用工具节点、Agent 的查询在 tool.end 里（query_artifact）；合并查询只记在 node.finished 的台账里。同一件快照
    两处都有的按 tool.end 算一次。核查类的查询（role: verify）不进写作目录，这里也不收。"""
    out: list[_Query] = []
    seen: set[tuple[str, str]] = set()
    asked: dict[tuple[str, Any], str] = {}      # (节点, call_id) → tool.start 里的 SQL
    merged: dict[str, str] = {}                  # 合并节点 → merge.end 里的 SQL
    merged_inputs: dict[str, Any] = {}           # 合并节点 → merge.end 里的输入
    count: dict[str, int] = {}

    def add(node_id: str, artifact: str, tool: Any, call_id: Any, sql: Any, source: Any) -> None:
        count[node_id] = count.get(node_id, 0) + 1
        snap, problem = _snapshot(side.load, artifact)
        own = snap.get("sql") if snap is not None and isinstance(snap.get("sql"), str) else None
        source = (snap or {}).get("source") or source
        inputs = (snap or {}).get("inputs") if snap is not None else merged_inputs.get(node_id)
        out.append(_Query(artifact=artifact, node_id=node_id, tool=str(tool) if tool else None,
                          ordinal=count[node_id], call_id=str(call_id) if call_id else None,
                          sql=own or (sql if isinstance(sql, str) and sql.strip() else None), snap=snap,
                          problem=problem, source=source,
                          inputs=inputs if source == MERGE_SOURCE and isinstance(inputs, list) else None))

    for _, etype, node_id, data in side.events:
        if not node_id or not isinstance(data, dict) or data.get("role") == "verify":
            continue
        if etype == EventType.TOOL_START:
            args = data.get("args")
            if isinstance(args, dict) and isinstance(args.get("sql"), str):
                asked[(node_id, data.get("call_id"))] = args["sql"]
        elif etype == EventType.TOOL_END and data.get("query_artifact"):
            artifact = str(data["query_artifact"])
            seen.add((node_id, artifact))
            add(node_id, artifact, data.get("tool"), data.get("call_id"), asked.get((node_id, data.get("call_id"))),
                None)
        elif etype == EventType.MERGE_END:
            if isinstance(data.get("sql"), str):
                merged[node_id] = data["sql"]
            merged_inputs[node_id] = data.get("inputs")
        elif etype == EventType.NODE_FINISHED:
            for entry in data.get("evidence") or []:
                if not (isinstance(entry, dict) and entry.get("kind") == "query" and entry.get("artifact")):
                    continue
                artifact = str(entry["artifact"])
                if (node_id, artifact) in seen:
                    # tool.end 已经收过：台账里多记着数据源，读不出快照时用得上
                    for q in out:
                        if q.node_id == node_id and q.artifact == artifact and not q.source:
                            q.source = entry.get("source")
                    continue
                seen.add((node_id, artifact))
                add(node_id, artifact, entry.get("tool"), entry.get("call_id"), merged.get(node_id),
                    entry.get("source"))
    return out


def _outputs(side: Side) -> dict[str, tuple[Any, ...]]:
    """每个节点在封存范围内交出的产出工件（node.finished 的 artifact），按执行的先后。工件按内容寻址：两次运行的同一个
    节点交出的一模一样，工件 id 就一样。产出没落成工件的记 None，核对不了。"""
    out: dict[str, list[Any]] = {}
    for _, etype, node_id, data in side.events:
        if etype == EventType.NODE_FINISHED and node_id and isinstance(data, dict):
            out.setdefault(node_id, []).append(data.get("artifact"))
    return {node_id: tuple(found) for node_id, found in out.items()}


def _pair(base: list[_Query], target: list[_Query],
          norm: Callable[[_Query], NormalizedSql | None]) -> list[tuple[_Query | None, _Query | None]]:
    """配对，一轮比一轮宽：
    1. 同一节点、同一工具、规范化 SQL 和字面量都相同：Agent 每次查询的先后可能不同，按「第几次」硬配会把换了先后的
       两条都配成「结构变了」；先配一字不差的，换了先后的东区、南区两条才各自配上自己，不会配成两条「参数不同」
    2. 同一节点、同一工具、规范化 SQL 相同（只有字面量不同）：循环里同一个模板查几次的，按先后一一配上
    3. 剩下的按「节点 + 第几次 + 工具」配：同一个位置上的查询换了写法，就是这条查询的结构变了
    4. 再剩下的跨节点按规范化 SQL 配（同样先配字面量也相同的）：节点换了 ID，查的还是同一个模板
    结果按这次运行的先后排，基准运行多出来的接在后面。"""
    matched: dict[int, int] = {}
    taken: set[int] = set()

    def link(same: Callable[[_Query, _Query], bool]) -> None:
        for j, q in enumerate(target):
            if j in matched:
                continue
            i = next((i for i, b in enumerate(base) if i not in taken and same(b, q)), None)
            if i is not None:
                matched[j] = i
                taken.add(i)

    def same_sql(b: _Query, q: _Query, *, literals: bool = False) -> bool:
        nq = norm(q)
        if nq is None:
            return False
        nb = norm(b)
        return nb is not None and nb.text == nq.text and (not literals or nb.literals == nq.literals)

    def here(b: _Query, q: _Query) -> bool:
        return b.node_id == q.node_id and b.tool == q.tool

    link(lambda b, q: here(b, q) and same_sql(b, q, literals=True))
    link(lambda b, q: here(b, q) and same_sql(b, q))
    link(lambda b, q: (b.node_id, b.ordinal, b.tool) == (q.node_id, q.ordinal, q.tool))
    link(lambda b, q: same_sql(b, q, literals=True))
    link(same_sql)
    pairs: list[tuple[_Query | None, _Query | None]] = [
        (base[matched[j]] if j in matched else None, q) for j, q in enumerate(target)]
    return pairs + [(b, None) for i, b in enumerate(base) if i not in taken]


@dataclass
class _Shape:
    """一对查询的结构：same / params_only / changed / added / removed，不同的字面量，结构上差在哪。"""

    structure: str
    literals: list[dict[str, str]] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    #: 两边的 SQL 都拿得到（拿不到的只能说不知道，不能说变了）
    known: bool = True


# --------------------------------------------------------------------------
# 口径卡
# --------------------------------------------------------------------------


@dataclass
class _Card:
    """一个口径卡节点在一次运行里最后一次执行的指标集。"""

    node_id: str
    artifact: str
    content: dict[str, Any] | None
    #: 指标集读不出来、被改过的说法
    problem: str | None
    #: 那次执行的 node.started 序号：输入按那一刻的节点产出还原
    started: int
    #: 执行了几次
    runs: int
    plans: dict[str, Any] = field(default_factory=dict)
    #: 这张卡上全部指标的输入，按路径（同一次执行里同一条路径的值是同一个）
    inputs: dict[str, Any] = field(default_factory=dict)
    sources: recompute.Sources | None = None

    def metrics(self) -> list[dict[str, Any]]:
        return [m for m in (self.content or {}).get("metrics") or [] if isinstance(m, dict) and m.get("id") is not None]


def _cards(side: Side) -> dict[str, _Card]:
    """每个口径卡节点最后一次执行的指标集，按第一次执行的先后。哪些 node.finished 算口径卡的执行，和
    api/evidence._sealed_cards 一样认：台账里的 metric_set 条目；一期以前没有台账的，画布上是口径卡的节点的产出。"""
    verified = {(node_id, artifact): card for node_id, artifact, card in side.cards}
    calibers = {str(n.get("id")) for n in side.graph.get("nodes") or []
                if isinstance(n, dict) and n.get("type") == "metrics"}
    runs: dict[str, list[tuple[int, str]]] = {}
    started: dict[str, list[int]] = {}
    for seq, etype, node_id, data in side.events:
        if not node_id or not isinstance(data, dict):
            continue
        if etype == EventType.NODE_STARTED:
            started.setdefault(node_id, []).append(seq)
        if etype != EventType.NODE_FINISHED:
            continue
        arts = [str(e["artifact"]) for e in data.get("evidence") or []
                if isinstance(e, dict) and e.get("kind") == "metric_set" and e.get("artifact")]
        if not arts and node_id in calibers and data.get("artifact"):
            arts = [str(data["artifact"])]
        runs.setdefault(node_id, []).extend((seq, art) for art in arts)
    out: dict[str, _Card] = {}
    for node_id, done in runs.items():
        if not done:
            continue
        seq, artifact = done[-1]
        content = verified.get((node_id, artifact))
        out[node_id] = _Card(node_id=node_id, artifact=artifact, content=content,
                             problem=None if content is not None else _card_problem(side.load, artifact),
                             started=max((s for s in started.get(node_id, []) if s < seq), default=seq),
                             runs=len(done))
    return out


def _card_problem(load: Callable[[str], Any], artifact: str) -> str:
    """指标集为什么用不了，接在「口径卡「X」」后面。"""
    try:
        content = load(artifact)
    except ValueError:
        return "的指标集与哈希不一致，疑似被修改"
    return "的指标集无法读取" if content is None else "的指标集内容不完整"


def _plan_card(side: Side, card: _Card, queries: list[_Query]) -> None:
    """按推演重算的规则还原卡上每个指标的输入：落在哪份快照上、是不是遮罩列、容器的完整值。"""
    if card.content is None:
        return
    sources = side.sources(card.started) if side.sources is not None else recompute.Sources(
        load=side.load, queries=tuple(dict.fromkeys(q.artifact for q in queries)), masked=side.masked)
    store = recompute._Store(sources)
    card.sources = sources
    for metric in card.metrics():
        plan = recompute._plan(metric, store)
        card.plans[str(metric["id"])] = plan
        for path, inp in plan.inputs.items():
            card.inputs.setdefault(path, inp)


# --------------------------------------------------------------------------
# 数：精确相减、渲染
# --------------------------------------------------------------------------


def _exact(value: Any) -> Decimal | None:
    """有限的数换成十进制：浮点按最短的写法（repr），相减不带二进制的误差。"""
    if not recompute._number(value):
        return None
    try:
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _plain(d: Decimal, *, whole: bool) -> int | float | None:
    """十进制换回 JSON 里的数：参与的都是整数时给整数，否则给浮点。浮点放不下的（超过 1e308）给 None：接口按 JSON
    交回，写不出无穷大。"""
    if whole and d == d.to_integral_value():
        return int(d)
    value = float(d)
    return value if math.isfinite(value) else None


def _percent(base: Decimal, target: Decimal) -> float | None:
    """相对基准的百分比，两位小数；基准为 0 没有百分比。基准极小（浮点误差那么小的数）、这次很大时，百分比的整数
    部分比十进制的默认精度（28 位）还长，量化到两位小数会抛 InvalidOperation：退回浮点算，浮点也放不下的给 None。"""
    if base == 0:
        return None
    ratio = (target - base) / abs(base) * 100
    try:
        return _plain(ratio.quantize(Decimal("0.01")), whole=False)
    except ArithmeticError:
        value = float(ratio)
        return round(value, 2) if math.isfinite(value) else None


def _round_trips(d: Decimal) -> bool:
    """这个十进制数换成浮点、再按最短写法写回来还是它自己（JSON 里交出去的就是浮点）。"""
    value = float(d)
    return math.isfinite(value) and Decimal(repr(value)) == d


def _split_exact(delta: Decimal, caliber: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(总变化, 口径效应, 数据效应)，三个数转成浮点交出去以后仍逐位相加。

    十进制里数据效应 = 总变化 − 口径效应是精确的，可转成浮点时三个数各舍入各的：没设小数位的指标（0.1 + 0.2 =
    0.30000000000000004 这种）交出去的两效应之和会和总变化差在最后一位。这时三个数按同一个精度量化（最多 15 位
    有效数字，浮点能原样写回的位数）：总变化和口径效应各自量化，数据效应取两者之差。设了小数位的值本来就写得回，
    原样交回。"""
    data = delta - caliber
    if all(_round_trips(x) for x in (delta, caliber, data)):
        return delta, caliber, data
    top = max(abs(delta), abs(caliber), abs(data))
    for digits in range(_FLOAT_DIGITS, _FLOAT_DIGITS - 3, -1):
        exp = Decimal(1).scaleb(top.adjusted() + 1 - digits)
        d, c = delta.quantize(exp), caliber.quantize(exp)
        if all(_round_trips(x) for x in (d, c, d - c)):
            return d, c, d - c
    return delta, caliber, data


def _places(*metrics: dict[str, Any]) -> int | None:
    """几个指标里最多的小数位；有一个没设的就不定小数位（按值本来的样子写）。"""
    found = [m.get("decimals") for m in metrics]
    if any(not isinstance(d, int) or isinstance(d, bool) for d in found):
        return None
    return max(found)


def _scale(metric: dict[str, Any]) -> tuple[str, bool]:
    """数值的含义：单位，以及是不是比率（percent_of_ratio 的 0.087 和单位为 % 的 8.7 不能直接相减）。"""
    return str(metric.get("unit") or ""), (metric.get("format") or DEFAULT_FORMAT) == "percent_of_ratio"


def _signed(meta: dict[str, Any], value: Any) -> str | None:
    """变化量的写法：+300.0元、-2单、+10.3 个百分点（比率、百分数的变化写百分点，和推演同一个写法）。为 0 写不带
    符号的 0。渲染不出来的给 None。"""
    if not recompute._number(value):
        return None
    if value != 0:
        return recompute._delta(meta, 0, value)[1]
    fmt, unit, places = meta.get("format") or DEFAULT_FORMAT, str(meta.get("unit") or ""), meta.get("decimals")
    try:
        if fmt == "percent_of_ratio":
            return render_number(0, decimals=places, fmt=fmt).rstrip("%") + " 个百分点"
        if unit in ("%", "％"):
            return render_number(0, decimals=places, fmt=fmt) + " 个百分点"
        return render_number(0, unit=unit, decimals=places, fmt=fmt)
    except RenderError:
        return None


def _shown(value: Any) -> str:
    """一个值写进说明：文本、数照写（太长的截断），空值写「空值」，容器写大概。"""
    if value is None:
        return "空值"
    if isinstance(value, (list, dict)):
        return f"[{len(value)} 项]" if isinstance(value, list) else f"{{{len(value)} 个字段}}"
    text = str(value)
    return text if len(text) <= _SHOWN_MAX else text[:_SHOWN_MAX] + "…"


def _expression(metric: dict[str, Any]) -> str:
    """表达式的规范写法：空白、括号写法不同的算同一个。解析不了的照原文。"""
    raw = str(metric.get("expression") or "").strip()
    try:
        return ast.unparse(parse_expression(raw)[0])
    except (ExpressionError, ValueError):
        return raw


# --------------------------------------------------------------------------
# 画布
# --------------------------------------------------------------------------


def _nodes(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n.get("id")): n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}


def _data(node: dict[str, Any]) -> dict[str, Any]:
    return node.get("data") if isinstance(node.get("data"), dict) else {}


def _config(node: dict[str, Any]) -> dict[str, Any]:
    config = _data(node).get("config")
    return config if isinstance(config, dict) else {}


def _edges(graph: dict[str, Any]) -> set[tuple[str, str, str]]:
    """连线：(起点, 终点, 出口)。默认出口的几种写法算同一个（和 autofix 认连线的规则一致）。"""
    out = set()
    for edge in graph.get("edges") or []:
        if isinstance(edge, dict):
            handle = edge.get("sourceHandle")
            out.add((str(edge.get("source")), str(edge.get("target")),
                     "" if handle in (None, "out", "source") else str(handle)))
    return out


def _config_keys(was: dict[str, Any], now: dict[str, Any]) -> tuple[list[str], bool]:
    """一个节点改了哪些配置项：(键名列表, 改的是不是只有名称)。值是字典的往下看一层（args.sql），不展示值本身。"""
    keys: list[str] = []
    if was.get("type") != now.get("type"):
        keys.append("type")
    renamed = _data(was).get("label") != _data(now).get("label")
    a, b = _config(was), _config(now)
    for key in dict.fromkeys([*a, *b]):
        x, y = a.get(key), b.get(key)
        if x == y:
            continue
        if isinstance(x, dict) and isinstance(y, dict):
            keys += [f"{key}.{sub}" for sub in dict.fromkeys([*x, *y]) if x.get(sub) != y.get(sub)]
        else:
            keys.append(key)
    return (keys + ["label"] if renamed else keys), renamed and not keys


def _key_label(key: str, node_type: Any) -> str:
    return _KEY_LABEL.get(key) or field_label(key, node_type)


def _unset(value: Any) -> bool:
    """节点上没写这一项：运行时回退到图级默认设置（NodeContext.cfg 同一个规则）。"""
    return value in (None, "")


def _reads(node: dict[str, Any], defaults: dict[str, Any]) -> frozenset[str]:
    """这个节点运行时按 NodeContext.cfg 读的配置项（_CFG_KEYS）。条件分支、结构校验、知识检索调不调模型看它那次的
    设置，调模型的才读模型那几项。"""
    kind = str(node.get("type"))
    keys = _CFG_KEYS.get(kind, frozenset())
    if kind in _CFG_KEYS_IF and _calls_model(kind, node, defaults):
        keys = keys | _CFG_KEYS_IF[kind]
    return keys


def _calls_model(kind: str, node: dict[str, Any], defaults: dict[str, Any]) -> bool:
    """条件分支是让模型分类、结构校验让模型修复（默认开着）、知识检索用模型重排：和执行器同一个判断、同一个缺省。"""
    def cfg(key: str, default: Any) -> Any:
        value = _config(node).get(key)
        return defaults.get(key, default) if _unset(value) else value

    if kind == "branch":
        return cfg("mode", "expression") == "llm"
    if kind == "validate":
        return bool(cfg("repair_with_llm", True))
    return (cfg("rerank", "off") or "off") == "model"


def _graph_sql(node: dict[str, Any] | None) -> tuple[str, str | None] | None:
    """节点在图里写的 SQL 和它的数据源：调用工具节点是参数里的 SQL（数据源从工具名来），合并查询是合并 SQL。
    别的节点（Agent 自己写 SQL）返回 None。"""
    if node is None:
        return None
    config = _config(node)
    if node.get("type") == "tool":
        args = config.get("args")
        sql = args.get("sql") if isinstance(args, dict) else None
        tool = str(config.get("tool") or "")
        source = tool[len(QUERY_PREFIX):] if tool.startswith(QUERY_PREFIX) else None
        return (sql, source) if isinstance(sql, str) and sql.strip() else None
    if node.get("type") == "merge":
        sql = config.get("sql")
        return (sql, MERGE_SOURCE) if isinstance(sql, str) and sql.strip() else None
    return None


def _request(snap: dict[str, Any]) -> dict[str, Any] | None:
    """端点读取的快照（capabilities/endpoints.py 的 endpoint_snapshot）记的是请求，不是 SQL。"""
    request = snap.get("request")
    return request if isinstance(request, dict) and not isinstance(snap.get("sql"), str) else None


def _compare_request(b: "_Query", t: "_Query") -> "_Shape":
    """两次端点读取的取数方式变没变：比端点、方法、路径；只有查询参数、请求体里的值不同的算参数变化。"""
    was, now = _request(b.snap or {}), _request(t.snap or {})
    if was is None or now is None:
        return _Shape("changed", differences=["一次是数据库查询、一次是端点读取，取数方式不同"])
    differences = []
    if b.source != t.source:
        differences.append(f"端点由「{b.source}」改为「{t.source}」")
    if (was.get("method"), was.get("path")) != (now.get("method"), now.get("path")):
        differences.append(f"请求由 {was.get('method')} {was.get('path')} 改为 {now.get('method')} {now.get('path')}")
    if differences:
        return _Shape("changed", differences=differences[:MAX_DIFFERENCES])
    literals = [{"base": json.dumps(was.get(k), ensure_ascii=False, sort_keys=True, default=str),
                 "target": json.dumps(now.get(k), ensure_ascii=False, sort_keys=True, default=str)}
                for k in ("query", "body") if was.get(k) != now.get(k)]
    return _Shape("params_only", literals) if literals else _Shape("same")


def _definition(graph: dict[str, Any], card_id: str, metric_id: str) -> dict[str, Any] | None:
    """图里口径卡节点上写的指标定义；指标定义来自别的工作流（口径卡来源）的不算，那里写的才是当时用的。"""
    card = _nodes(graph).get(card_id)
    if card is None or card.get("type") != "metrics" or _config(card).get("caliber_from") not in (None, "", {}):
        return None
    return next((d for d in _config(card).get("metrics") or []
                 if isinstance(d, dict) and str(d.get("id")) == metric_id), None)


# --------------------------------------------------------------------------
# 输入的出处有没有变
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Change:
    """一个输入的出处在两次运行之间变没变：same / params / sql / template / unknown，加一句说明。"""

    kind: str
    reason: str | None = None


#: 合并几处出处变化时的轻重：模板、SQL 变了 > 说不清 > 只有参数变了 > 没变
_WEIGHT = {"sql": 3, "template": 3, "unknown": 2, "params": 1, "same": 0}


def _worst(changes: Iterable[_Change]) -> _Change:
    found = list(changes)
    return max(found, key=lambda c: _WEIGHT[c.kind]) if found else _Change("same")


def _reach(node_id: str, links: Mapping[str, set[str]], memo: dict[str, set[str]]) -> set[str]:
    """顺着连线（links：节点 → 上一层或下一层的节点）能走到的全部节点，不含它自己（成环的除外）。"""
    if node_id not in memo:
        seen: set[str] = set()
        todo = [node_id]
        while todo:
            for nxt in links.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        memo[node_id] = seen
    return memo[node_id]


def _from_input(path: str, item: dict[str, Any]) -> bool:
    head = _REF_HEAD.match(path)
    return item.get("via") == "input" or (head is not None and head.group(1) == "input")


# --------------------------------------------------------------------------
# 对比
# --------------------------------------------------------------------------


class _Compare:
    def __init__(self, base: Side, target: Side) -> None:
        self.base, self.target = base, target
        self.notes: list[str] = []
        self.masked_seen = False
        self._norm: dict[tuple[str, str | None], NormalizedSql] = {}
        self._shapes: dict[tuple[int, int], _Shape] = {}
        self._ancestors: dict[str, set[str]] = {}
        self._descendants: dict[str, set[str]] = {}
        self.base_nodes, self.target_nodes = _nodes(base.graph), _nodes(target.graph)
        self.base_queries, self.target_queries = _queries(base), _queries(target)
        self.pairs = _pair(self.base_queries, self.target_queries, self._norm_of)
        self.template, self.changed, self.inherited = self._template()
        self.titles = {**{k: self._title_of(n) for k, n in self.base_nodes.items()},
                       **{k: self._title_of(n) for k, n in self.target_nodes.items()}}
        #: 画布上的先后（这次运行的图在前）：说明里点名上游节点时按它挑第一个
        self.order = {n: i for i, n in enumerate(dict.fromkeys([*self.target_nodes, *self.base_nodes]))}
        self.base_outputs, self.target_outputs = _outputs(base), _outputs(target)
        self.parents: dict[str, set[str]] = {}
        self.children: dict[str, set[str]] = {}
        for source, target_id, _ in _edges(base.graph) | _edges(target.graph):
            self.parents.setdefault(target_id, set()).add(source)
            self.children.setdefault(source, set()).add(target_id)

    # ---------------------------------------------------------------- 结果

    def result(self) -> dict[str, Any]:
        metrics = self._metrics()
        queries = [self._query_view(b, t) for b, t in self.pairs]
        self._run_notes()
        if self.masked_seen:
            self.notes.append(MASKED_NOTE)
        summary = {name: 0 for name in CLASSES}
        for m in metrics:
            summary[m["classification"]] += 1
        return {"target": self.target.run, "base": self.base.run, "summary": summary, "template": self.template,
                "metrics": metrics, "queries": queries, "notes": list(dict.fromkeys(self.notes))}

    def _title_of(self, node: dict[str, Any]) -> str:
        return str(_data(node).get("label") or node.get("id"))

    def title(self, node_id: Any) -> str:
        return self.titles.get(str(node_id), str(node_id))

    def _run_notes(self) -> None:
        """运行输入的变化：键名，值短的连值一起写。值可能是一大段文字，太长的只说有变化。"""
        before, after = self.base.inputs or {}, self.target.inputs or {}
        for key in dict.fromkeys([*before, *after]):
            a, b = before.get(key), after.get(key)
            if a == b:
                continue
            short = all(v is None or isinstance(v, (int, float, str, bool)) and len(str(v)) <= 40 for v in (a, b))
            self.notes.append(f"运行输入「{key}」由「{_shown(a)}」改为「{_shown(b)}」" if short
                              else f"运行输入「{key}」有变化")

    # ---------------------------------------------------------------- 查询

    def _dialect(self, source: Any) -> str | None:
        if source == MERGE_SOURCE:
            return _MERGE_DIALECT
        return self.target.dialects.get(str(source)) or self.base.dialects.get(str(source)) if source else None

    def _normalize(self, sql: str, dialect: str | None) -> NormalizedSql:
        key = (sql, dialect)
        if key not in self._norm:
            self._norm[key] = normalize_sql(sql, dialect)
        return self._norm[key]

    def _norm_of(self, q: _Query) -> NormalizedSql | None:
        return self._normalize(q.sql, self._dialect(q.source)) if q.sql else None

    def _same_graph_sql(self, a: tuple[str, str | None] | None, b: tuple[str, str | None] | None) -> bool:
        """图里的两段 SQL 规范化以后一模一样（只差空白、大小写、注释）。"""
        if a is None or b is None:
            return False
        na, nb = self._normalize(a[0], self._dialect(a[1])), self._normalize(b[0], self._dialect(b[1]))
        return (na.text, na.literals) == (nb.text, nb.literals)

    def shape(self, b: _Query | None, t: _Query | None) -> _Shape:
        if b is None:
            return _Shape("added")
        if t is None:
            return _Shape("removed")
        key = (id(b), id(t))
        if key not in self._shapes:
            self._shapes[key] = self._compare_sql(b, t)
        return self._shapes[key]

    def _compare_sql(self, b: _Query, t: _Query) -> _Shape:
        if b.snap is not None and t.snap is not None and (_request(b.snap) or _request(t.snap)):
            return _compare_request(b, t)
        if b.sql is None or t.sql is None:
            which = "基准运行" if b.sql is None else "这次运行"
            return _Shape("changed", known=False, differences=[f"{which}的查询快照无法读取，无法比较 SQL"])
        differences = []
        if b.source and t.source and b.source != t.source:
            differences.append(f"数据源由「{b.source}」改为「{t.source}」")
        dialect = self._dialect(t.source or b.source)
        nb, nt = self._normalize(b.sql, dialect), self._normalize(t.sql, dialect)
        if nb.text != nt.text:
            # 时间范围的不同排到最后：定时运行每期的日期本来就不同，结构上真正改了的（多了筛选、换了时间列……）
            # 才是要先看的
            found = sorted(diff_facets(facets(b.sql, dialect), facets(t.sql, dialect), _LABELS),
                           key=lambda d: d.aspect == "time_range")
            texts = [d.text for d in found] or [NO_DIFFERENCE]
            return _Shape("changed", differences=(differences + texts)[:MAX_DIFFERENCES])
        if differences:
            return _Shape("changed", differences=differences)
        literals = [{"base": x, "target": y} for x, y in zip(nb.literals, nt.literals) if x != y]
        return _Shape("params_only", literals) if literals else _Shape("same")

    def _query_view(self, b: _Query | None, t: _Query | None) -> dict[str, Any]:
        shape = self.shape(b, t)
        q = t or b
        assert q is not None
        view = {
            "node_id": q.node_id, "node_title": self.title(q.node_id), "ordinal": q.ordinal,
            "structure": shape.structure,
            "base_sql": b.sql if b else None, "target_sql": t.sql if t else None, "literals": shape.literals,
            "base_rows": len(b.snap["rows"]) if b and b.snap else None,
            "target_rows": len(t.snap["rows"]) if t and t.snap else None,
            "base_truncated": bool(b.snap.get("truncated")) if b and b.snap else None,
            "target_truncated": bool(t.snap.get("truncated")) if t and t.snap else None,
            "base_artifact": b.artifact if b else None, "target_artifact": t.artifact if t else None,
            "differences": shape.differences,
        }
        problems = [f"{side}的查询快照{'与哈希不一致，疑似被修改' if one.problem == 'tampered' else '无法读取'}"
                    for side, one in (("基准运行", b), ("这次运行", t)) if one is not None and one.problem]
        if problems:
            view["problem"] = "；".join(problems)
        return view

    def _side_query(self, queries: list[_Query], inp: Any) -> _Query | None:
        """一个输入落在的那份快照，是这次运行里哪一次查询交回的（同一件快照出现几次时先找写它的节点）。"""
        found = [q for q in queries if q.artifact == inp.snapshot]
        node_id = inp.item.get("node_id")
        return next((q for q in found if q.node_id == node_id), found[0] if found else None)

    def _query_change(self, b: _Query | None, t: _Query | None) -> _Change:
        q = t or b
        assert q is not None
        name = f"查询「{self.title(q.node_id)}」"
        if b is None:
            return _Change("sql", f"节点「{self.title(q.node_id)}」这次多了一次查询")
        if t is None:
            return _Change("sql", f"节点「{self.title(q.node_id)}」这次少了一次查询")
        shape = self.shape(b, t)
        if not shape.known:
            return _Change("unknown", f"{name}的快照无法读取，无法判断 SQL 是否有变化")
        # 端点读取比的是请求（_compare_request），说法跟着换
        what = "请求" if (b.snap and _request(b.snap)) or (t.snap and _request(t.snap)) else " SQL "
        if shape.structure == "changed":
            return _Change("sql", f"{name}的{what}结构有变化" + (f"：{shape.differences[0]}" if shape.differences else ""))
        if shape.structure == "params_only":
            shown = "，".join(f"{x['base']} → {x['target']}" for x in shape.literals[:2])
            more = f" 等 {len(shape.literals)} 处" if len(shape.literals) > 2 else ""
            return _Change("params", f"{name}的{what}只有参数不同（{shown}{more}）")
        return _Change("same")

    def _snapshot_change(self, b: _Query | None, t: _Query | None, seen: frozenset[str] = frozenset()) -> _Change:
        """一对查询的取数变没变：SQL 和数据源（_query_change）；只差字面量时再看图里的 SQL 改没改过；合并查询再
        一路追到喂它的每一份上游快照。"""
        change = self._query_change(b, t)
        if b is None or t is None:
            return change
        found = [change]
        if change.kind == "params" and (edited := self._edited_sql(b, t)) is not None:
            found.append(edited)
        if b.source == MERGE_SOURCE and t.source == MERGE_SOURCE:
            found += self._merge_change(b, t, seen | {b.artifact, t.artifact})
        return _worst(found)

    def _edited_sql(self, b: _Query, t: _Query) -> _Change | None:
        """执行的 SQL 只差字面量时，图里写的 SQL 改没改过。定时运行代进去的日期不同，图里一字未改：是参数。图里写死的
        筛选值、系数改了（'paid' 改成 'refunded'、0.9 改成 0.8）：执行的 SQL 同样只差字面量，却是改了模板。规范化
        以后一模一样的（只改了空白、大小写）不算改过。Agent 自己写的 SQL 图里没有，返回 None。"""
        was, now = _graph_sql(self.base_nodes.get(b.node_id)), _graph_sql(self.target_nodes.get(t.node_id))
        if was is None or now is None or self._same_graph_sql(was, now):
            return None
        nb, nt = self._normalize(was[0], self._dialect(was[1])), self._normalize(now[0], self._dialect(now[1]))
        shown = [f"{x} → {y}" for x, y in zip(nb.literals, nt.literals) if x != y] if nb.text == nt.text else []
        detail = "（" + "，".join(shown[:2]) + (f" 等 {len(shown)} 处" if len(shown) > 2 else "") + "）" if shown else ""
        return _Change("sql", f"工作流里「{self.title(t.node_id)}」的 SQL 改过{detail}，不是运行时代入的参数")

    def _merge_change(self, b: _Query, t: _Query, seen: frozenset[str]) -> list[_Change]:
        """合并查询的每个输入（按别名配对两边）各自的取数变没变。合并 SQL 一字未改，喂它的取数 SQL 加了筛选，合并
        结果说的也已经不是同一回事。输入是另一个合并查询的结果时接着往上追。"""
        name = f"合并查询「{self.title(t.node_id)}」"
        before, after = _merge_inputs(b), _merge_inputs(t)
        if before is None or after is None:
            return [_Change("unknown", f"{name}没有记录它的输入，无法比较上游的取数")]
        out = []
        for alias in dict.fromkeys([*after, *before]):
            was, now = before.get(alias), after.get(alias)
            if was is None or now is None:
                out.append(_Change("sql", f"{name}的输入有变化：这次{'多了' if was is None else '少了'}「{alias}」"))
                continue
            if was in seen or now in seen:
                continue        # 合并查询的输入只能是上游的快照，成不了环；防一手
            qb = next((q for q in self.base_queries if q.artifact == was), None)
            qt = next((q for q in self.target_queries if q.artifact == now), None)
            if qb is None or qt is None:
                out.append(_Change("unknown", f"{name}的输入「{alias}」落在的查询快照不在封存范围内，无法比较上游的取数"))
                continue
            change = self._snapshot_change(qb, qt, seen)
            out.append(_Change(change.kind, f"{name}的输入「{alias}」：{change.reason}") if change.reason else change)
        return out

    def _node_change(self, node_id: str) -> _Change | None:
        """一个节点自己查过的库在两次运行之间变没变；它两边都没查过库返回 None。"""
        own = [(b, t) for b, t in self.pairs if (b and b.node_id == node_id) or (t and t.node_id == node_id)]
        return _worst(self._snapshot_change(b, t) for b, t in own) if own else None

    def _data_moved(self, scope: set[str]) -> bool:
        """这几个节点查到的数据两次有没有不同：有一对查询的行（或列）不同就算有；快照读不出来、只在一边有的核对不了，
        也算有（不能说它们一样）。一条查询都没有的算没有。"""
        for b, t in self.pairs:
            if (t or b).node_id not in scope:
                continue
            if b is None or t is None or b.snap is None or t.snap is None:
                return True
            if b.snap.get("rows") != t.snap.get("rows") or b.snap.get("columns") != t.snap.get("columns"):
                return True
        return False

    def _upstream(self, node_id: str) -> set[str]:
        """画布上 node_id 的全部上游（两次运行的图合起来，宁可多算）。"""
        return _reach(node_id, self.parents, self._ancestors)

    def _downstream(self, node_id: str) -> set[str]:
        """画布上 node_id 的全部下游（两次运行的图合起来）。"""
        return _reach(node_id, self.children, self._descendants)

    def _before(self, card: str) -> set[str]:
        """口径卡执行之前可能执行过的节点：画布上它的上游，加上和它不在一条线上的（并行的支路可能先写了变量）。它自己、
        只在它下游的节点在它之后才执行，写不了它读到的值（循环回边连回来的算上游）。"""
        after = self._downstream(card) - self._upstream(card)
        return {n for n in self.titles if n != card and n not in after}

    def _type_of(self, node_id: str) -> str | None:
        node = self.target_nodes.get(node_id) or self.base_nodes.get(node_id)
        return str(node.get("type")) if node is not None else None

    def _moved(self, node_id: str, *, upstream: bool = True) -> str:
        """一个影响取值的节点变化（self.changed）写成一句话。upstream 是它在不在画布上的上游（并行支路上的节点不在，
        不说成「上游」）。"""
        name = f"{'上游节点' if upstream else '节点'}「{self.title(node_id)}」"
        change = self.changed.get(node_id, "")
        if change == "defaults":
            keys = self.inherited.get(node_id, [])
            shown = "、".join(_key_label(k, self._type_of(node_id)) for k in keys[:3])
            more = f"等 {len(keys)} 项" if len(keys) > 3 else ""
            return f"{name}跟随的工作流默认设置（{shown}{more}）有变化"
        return {"added": f"{name}是这次新增的", "removed": f"{name}这次被删除了",
                "edges": f"{name}的连线有变化"}.get(change, f"{name}的配置有变化")

    def _ordered(self, nodes: Iterable[str]) -> list[str]:
        """按画布上的先后排（这次运行的图在前）。"""
        return sorted(nodes, key=lambda n: self.order.get(n, len(self.order)))

    def _regenerated(self, node_id: str) -> bool:
        """模型、人交出值的节点（_GENERATED）两次交出的产出不同，或者核对不了。一模一样的没带进它本身的差异。"""
        if self._type_of(node_id) not in _GENERATED:
            return False
        before, after = self.base_outputs.get(node_id, ()), self.target_outputs.get(node_id, ())
        return before != after or None in before or None in after

    def provenance(self, path: str, b: Any, t: Any, *, card: str, moved: bool = True) -> _Change:
        """一个输入的出处在两次运行之间变没变（b、t 是推演重算还原出来的输入，card 是读它的口径卡节点）。moved 是这个
        指标的值变没变：输入是容器、完整值又还原不出来，核对不了它变没变时，按它推断。"""
        if b is None or t is None:
            return _Change("unknown", f"输入「{path}」只在一边的口径卡里有记录")
        if b.snapshot or t.snapshot:
            return self._linked(path, b, t)
        nb, nt = b.item.get("node_id"), t.item.get("node_id")
        if nb != nt:
            if not nb or not nt:
                side = "基准运行" if not nb else "这次运行"
                return _Change("unknown", f"{side}没有记录输入「{path}」由哪个节点写入，无法比较来源")
            return _Change("template", f"输入「{path}」的来源节点由「{self.title(nb)}」换成了「{self.title(nt)}」")
        same = _same_input(b, t)
        if _from_input(path, b.item):
            return self._run_input(path, b, t, same, moved=moved)
        if same is None:
            same = not moved
        writer = str(nb) if nb else None
        if writer and (self._type_of(writer) in _GENERATED
                       or {b.item.get("via"), t.item.get("via")} & _GENERATED_VIA):
            return self._generated(path, writer, same)
        own = self._node_change(writer) if writer else None
        if writer and own is not None:
            # 写它的节点自己查过库（调用工具节点的结果整份用、合并查询……）：它查的那几条就是喂它的查询
            if writer in self.changed and own.kind in ("same", "params"):
                return _Change("template", self._moved(writer))
            if own.kind == "same" and not same and not self._data_moved({writer}):
                return _Change("unknown", f"输入「{path}」来自「{self.title(writer)}」，它查到的数据两次相同，"
                                          "交出的值却变了，无法判断变化来源")
            return own
        if writer and writer in self.changed:
            return _Change("template", self._moved(writer))
        # 写它的节点不查库（代码、数据整形……）：看画布上它的全部上游。写它的节点不知道时，看口径卡之前可能执行过的
        # 节点（_before）：口径卡自己、只在它下游的节点（写周报……）写不了它读到的值，点名它们就把下游的变化说成了
        # 它的来源。above 是点名时算「上游」的节点
        if writer:
            scope = above = self._upstream(writer)
        else:
            scope, above = self._before(card), self._upstream(card)
        moved_nodes = self._ordered(n for n in scope if n in self.changed)
        worst = _worst(self._query_change(b2, t2) for b2, t2 in self.pairs if (t2 or b2).node_id in scope)
        where = f"来自「{self.title(writer)}」" if writer else "不知道由哪个节点写入"
        if moved_nodes:
            first = moved_nodes[0]
            return _Change("unknown", f"输入「{path}」{where}，{self._moved(first, upstream=first in above)}，"
                                      "无法判断变化来源")
        if worst.kind in ("sql", "template", "unknown"):
            return _Change("unknown", f"输入「{path}」{where}，对不上具体的查询；它的上游有变化，无法判断变化来源")
        if not same and (spoken := self._ordered(n for n in scope if self._regenerated(n))):
            # 上游有模型、人交出的值，两次不同：它交的数变了，可能是数据变了，也可能是模型本身的差异（它可能用的
            # 正是 Agent 没按出处交的数）。上游的查询结果变没变都一样说不清
            first = spoken[0]
            who = ("它的上游节点" if writer else "上游节点") if first in above else "节点"
            kind = TYPE_LABEL.get(self._type_of(first) or "", "节点")
            return _Change("unknown", f"输入「{path}」{where}，{who}「{self.title(first)}」（{kind}）两次交出的结果不同，"
                                      "无法判断变化是否来自数据")
        if worst.kind == "same" and not same and not self._data_moved(scope):
            # 上游查到的数据两次一模一样，它交的值却变了：不是数据变的。上游只有运行输入变了的，算参数
            if (self.base.inputs or {}) != (self.target.inputs or {}) \
                    and any(self._type_of(n) == "input" for n in scope):
                return _Change("params", f"输入「{path}」{where}，它的上游只有运行输入有变化")
            return _Change("unknown", f"输入「{path}」{where}，它上游的查询结果两次相同，交出的值却变了，"
                                      "无法判断变化来源")
        return worst

    def _linked(self, path: str, b: Any, t: Any) -> _Change:
        """落在查询快照上的输入：两边各自的那份快照直接比取数，不管全局配对把它们配给了谁。Agent 按出处交来的字段还要
        引用同一列、同一段行。"""
        if not (b.snapshot and t.snapshot):
            return _Change("unknown", f"输入「{path}」只有一边对得上查询快照，无法比较取数")
        qb, qt = self._side_query(self.base_queries, b), self._side_query(self.target_queries, t)
        if qb is None or qt is None:
            return _Change("unknown", f"输入「{path}」落在的查询快照不在封存范围内的查询里，无法比较取数")
        change = self._snapshot_change(qb, qt)
        if "agent_field" not in (b.item.get("via"), t.item.get("via")):
            # cell() 取哪一格由口径卡的表达式定（行列用到的变量是另外的输入，各比各的）
            return change
        agent = t.item.get("node_id") or b.item.get("node_id")
        if (cited := self._citation(agent, b, t, qb, qt)) is not None:
            change = _worst([change, cited])
        if agent and str(agent) in self.changed and change.kind in ("same", "params"):
            # SQL 没变、引用的也是同一处，Agent 自己的配置（提示词、工具、模型……）变了：字段的意思可能已经不同
            return _Change("template", f"{self._moved(str(agent))}，它按出处交来的字段可能换了取法")
        return change

    def _citation(self, agent: Any, b: Any, t: Any, qb: _Query, qt: _Query) -> _Change | None:
        """Agent 按出处交来的字段两次引用的是不是同一处：同一列（对象数组是同一组列）、同一行或同一段行。两边引用的都是
        整份结果的算同一处（这次多了几行也是）；行段相同、一边是整份结果一边不是的不算——没引用的那几行还在这次的快照
        里，按维度配对会把它们算成「数据没了」。行号相同、都不是整份结果的，还要引用同一批维度值（_left_out）：行段
        要把引用过的每个维度值切在同一处（两边都整个被引用，或者都只引用了它同样的那几行），单格要引用同一个维度值的
        同一行。行段的起止行有一个不同就不是同一处（只差最后一行也是：多引用的那一行改变了合计）。不是同一处、核对
        不了是不是同一处的，返回说明。"""
        name = f"Agent「{self.title(agent)}」" if agent else "Agent"
        lb, lt = _locator(b), _locator(t)
        cb, ct = lb.get("column") or lb.get("columns"), lt.get("column") or lt.get("columns")
        if cb != ct:
            return _Change("unknown", f"{name}引用的列由{_columns_text(cb)}改为{_columns_text(ct)}，取的不是同一个数")
        what = "同一个数" if b.row is not None and t.row is not None else "同一组数"
        changed = _Change("unknown", f"{name}引用的行由{_rows_text(b)}改为{_rows_text(t)}，取的不是{what}")
        if b.row is not None or t.row is not None:
            # 单格（数组字段里的一个元素、按出处交的一格）：取哪一行由口径卡的下标和 Agent 交的行段一起定，同一行才是
            # 同一处
            return self._left_out(name, what, b, t, qb, qt, cb) if (b.row, b.rows) == (t.row, t.rows) else changed
        wb, wt = _whole(b, qb), _whole(t, qt)
        same_span = b.rows == t.rows
        if wb and wt:
            return None
        if same_span and wb is False and wt is False:
            return self._left_out(name, what, b, t, qb, qt, cb)
        if (wb is None or wt is None) and (same_span or (wb is not False and wt is not False)):
            # 快照读不出来，核对不了是不是整份结果：行段相同的可能一边整份一边不是，行段不同的也可能两边都是整份
            side = "基准运行" if wb is None else "这次运行"
            return _Change("unknown", f"{side}的查询快照无法读取，无法核对 {name}引用的是不是整份结果")
        if not same_span:
            return changed
        if wb:
            return _Change("unknown", f"{name}在基准运行中引用了整份结果，这次只引用了{_rows_text(t)}"
                                      f"（结果共 {len(qt.snap['rows'])} 行），取的不是同一组数")
        return _Change("unknown", f"{name}在基准运行中只引用了{_rows_text(b)}（结果共 {len(qb.snap['rows'])} 行），"
                                  "这次引用了整份结果，取的不是同一组数")

    def _left_out(self, name: str, what: str, b: Any, t: Any, qb: _Query, qt: _Query, column: Any) -> _Change | None:
        """行号相同、都不是整份结果的引用，引用的是不是同一批维度值（区域……）。按维度键分别数两边引用了它几行、引用
        的行外面还有没有它的行（_cut 按一组维度核对）：

        - 行段（口径卡按引用的几行一起算）：引用过的每个维度值，两边要被切在同一处——都整个被引用（引用的行外面没有
          它的行；那一边的快照里根本没有它的，也算整个被引用，没什么可漏的），或者都只引用了它同样的那几行（按查询
          结果的先后数：它排在引用的行前面的行数一样、引用的行数也一样，两次都是南区的第一天）。切法不同就不是同一组
          数：按区域排序的结果多了北区，西区整个挪出了引用的行段；区域 × 日期的结果东区多了一天，南区的第二天被挤到
          引用的行外面；南区这次多了一天、引用的行只盖住前两天（引用的行数没变少，南区却从整个被引用变成只引用了一
          部分）；北区这次新出现，三天里只引用了两天；引用的行不从第 1 行开始，东区两次都只引用了一行，基准运行是它
          金额第二高的那天、这次是最高的那天。按维度配对会把还在快照里、只是没被引用的行算成数据变化，和真的少了、
          真的多了分不出来，正负号都可能反。只比引用的行数查不出后三种，只看「引用的行里有没有这个键」连挤出一部分都
          查不出。引用的行后面还剩它几行不进切法：两次都只引用了南区的第一天，后面剩一天还是两天，引用的都是同一天。
        - 单格（数组字段里的一个元素）：引用的那一行两次要是同一个维度值，还要是它同样的那一行——按查询结果的先后，
          排在这一行前面的、它的行数两边一样（两次都是南区的第一天）。区域 × 日期的结果东区少了一天，第 3 行从南区的
          第一天变成了第二天；按金额排，第 2 行从东区第二高的那天变成了最高的那天：区域没变，取的却不是同一个数（同一处
          引用换成只有一行的行段也说不清），按行号认会把南区第一天的 50 和第二天的 45 当成同一个数，同一天的南区其实
          是 50 → 60，正负号都反了。只比排在前面的行数，不比「它在这一行外面还有没有行」：单格只有一行，口径卡不按它
          算这个区域的合计，东区两天还是一天，第 1 行都是东区的第一天。不是同一个维度值的，一边引用的那个维度值在另一
          边的快照里、只是不在这一行，取的不是同一个数。
        - 一边引用了、另一边快照里没有的，是真的没有了（或者这次才有），照常比。快照被截断的按取回的行比，和引用整份
          结果时一样（系统的行数上限当作查询的一部分，像 SQL 里的 LIMIT）：挤到截断点后面的行看不到，和真的没有了
          分不出来——截断的快照不做归因，_attribute 写明截断。

        说明里说的是引用的行外面有它的那一边（两边都有的，是引用得少的那一边；行数一样、只是引用的不是它同样那几行
        的，两边排在前面的行数都写出来）；几个维度值切法都不同的，说基准运行引用的行里先出现的那个。

        按哪组维度核对（_dimensions，遮罩列不当维度）：两边整份结果认出的那组，加上归因用的那组（_attributed_rows：数组
        字段只在它引用的那段行上认），两组不同时两组都核对，都是同一处才算同一处。归因只在引用的行上认维度：渠道在引用
        的行里两边都只有「线上」，不当维度，归因按区域说南区的变化；只按整份结果的（区域, 渠道）核对，南区 / 线上两边
        都整个被引用、南区 / 线下两边都没被引用，看着都切在同一处，按区域看南区这次却有一行（线下）在引用的行外面，
        归因说的南区就不是同一组数——归因按哪组维度说，就要按哪组维度核对。只按归因用的那组也不够：只引用了一行的，
        引用的行里每列都只有一个值，认不出维度；整份结果里分得清的维度（区域, 渠道）也要切在同一处。

        先只用没遮的列认，说明里写的是没遮的列的值和行号；有列设了遮罩的，再连遮住的列一起核对一遍（区域设了遮罩时它
        往往正是唯一认得出行的那一列，只用没遮的列等于对它不核对），这一遍的说明只写行号、不写是哪个值。两遍都分不出
        维度（没有不是数值的列）的，只能按行号认，返回 None。快照读不出来的说不清：只有单格会走到这里（行段在 _citation
        里先核对整份结果，读不出来的已经说不清），那一行两次是不是同一个维度值核对不了，和行段一样不按行号当成同一处。
        快照里有的行和列对不上（格数不对、不是一行）的，认不出各行是哪个维度值，同样说不清。"""
        if qb.snap is None or qt.snap is None:
            side = "基准运行" if qb.snap is None else "这次运行"
            return _Change("unknown", f"{side}的查询快照无法读取，无法核对 {name}两次引用的{_rows_text(t)}"
                                      "是不是同一个维度值")
        rb, rt = _records(qb.snap, None), _records(qt.snap, None)
        if rb is None or rt is None:
            side = "基准运行" if rb is None else "这次运行"
            return _Change("unknown", f"{side}的查询快照里有的行和列对不上，无法核对 {name}两次引用的{_rows_text(t)}"
                                      f"是不是同一{'个' if b.row is not None else '批'}维度值")
        measure, hidden = column if isinstance(column, str) else "", self._hidden(b, t)
        whole, _ = _dimensions(qb.snap, qt.snap, rb, rt, measure, hidden)
        # 整份结果的行没问题，引用的那段行（它的一部分）也读得出来；单格的归因用整份结果，和 whole 是同一组
        used, _ = _dimensions(qb.snap, qt.snap, _attributed_rows(qb.snap, b) or [], _attributed_rows(qt.snap, t) or [],
                              measure, hidden)
        for dims in dict.fromkeys(tuple(d) for d in (whole, used) if d):
            if (found := self._cut(name, what, b, t, rb, rt, list(dims))) is not None:
                return found
        if hidden and any(str(c).lower() in hidden for snap in (qb.snap, qt.snap) for c in snap.get("columns") or []):
            # 拿遮住的列核对过（哪怕最后没核对出不同）：整体说明照样提一句有列设了遮罩
            self.masked_seen = True
        if hidden:
            # 遮罩列不当维度，是为了值不进说明；可它往往正是认得出行的那一列（区域设了遮罩），只拿没遮的列核对，等于对它
            # 不核对，挤出引用的行就和真的没有了分不出来。连遮住的列一起再核对一遍，说明里只写行号、不写值
            whole, _ = _dimensions(qb.snap, qt.snap, rb, rt, measure, frozenset())
            used, _ = _dimensions(qb.snap, qt.snap, _attributed_rows(qb.snap, b) or [],
                                  _attributed_rows(qt.snap, t) or [], measure, frozenset())
            for dims in dict.fromkeys(tuple(d) for d in (whole, used) if d):
                if (found := self._cut(name, what, b, t, rb, rt, list(dims), redact=True)) is not None:
                    return found
        return None

    def _cut(self, name: str, what: str, b: Any, t: Any, rb: list[tuple[int, dict[str, Any]]],
             rt: list[tuple[int, dict[str, Any]]], dims: list[str], *, redact: bool = False) -> _Change | None:
        """按 dims 这组维度核对两边引用的是不是同一批维度值（规则见 _left_out）：是返回 None，不是返回说明。rb、rt 是
        两边整份结果的行。redact：dims 里有设了遮罩的列，说明里不写是哪个维度值，只写行号。"""

        def named(key: tuple[Any, ...]) -> str:
            return "其中一个维度值" if redact else _key_text(key)

        masked_note = "（维度列设了遮罩，不写出是哪个值）" if redact else ""

        def split(records: list[tuple[int, dict[str, Any]]], inp: Any) -> tuple[dict[Any, int], dict[Any, list[int]]]:
            """{维度键: 引用的行里有几行}，{维度键: [引用的行外面的行号]}。一个键可能占几行（去掉日期这类每期都不同的
            列以后），要数清几行，只记第一次出现的看不出「有一部分挪了出去」。"""
            first, last = _cited(inp, len(records))
            inside: dict[Any, int] = {}
            outside: dict[Any, list[int]] = {}
            for i, row in records:
                key = tuple(_hashable(row.get(d)) for d in dims)
                if first <= i <= last:
                    inside[key] = inside.get(key, 0) + 1
                else:
                    outside.setdefault(key, []).append(i)
            return inside, outside

        (in_b, out_b), (in_t, out_t) = split(rb, b), split(rt, t)
        single = b.row is not None             # 和 t.row 一样（_citation 只在两边行号相同时走到这里）
        cited, first = _rows_text(t), _cited(t, len(rt))[0]

        def ahead(outside: list[int]) -> int:
            """它排在引用的行前面的行数：按查询结果的先后，引用的是它的第几行起。两边行号相同，first 是同一个。"""
            return sum(1 for i in outside if i < first)

        def cut(inside: int, outside: list[int]) -> tuple[int, int] | None:
            """行段切在这个维度值的哪儿：引用的行外面没有它的行记 None（整个被引用；快照里根本没有它的也是 None）；
            有的记（它排在引用的行前面的行数, 引用了它几行）——按查询结果的先后，引用的是它的第几行到第几行。"""
            return (ahead(outside), inside) if outside else None

        for key in dict.fromkeys([*in_b, *in_t]):          # 只看引用过的：两边都没引用的不进这个数
            nb, nt = in_b.get(key, 0), in_t.get(key, 0)
            ob, ot = out_b.get(key, []), out_t.get(key, [])
            if single:
                if nb == nt:
                    # 那一行两次都是它（单格只有一行，nb、nt 都是 1）：还要是它同样的那一行，排在前面的行数一样
                    if ahead(ob) == ahead(ot):
                        continue
                    return _Change("unknown", f"{name}两次都引用{cited}，两次都是{named(key)}，可不是它同样的那一行"
                                              f"（它排在这一行前面的，基准运行有 {ahead(ob)} 行，这次有 {ahead(ot)} 行），"
                                              f"取的不是{what}{masked_note}")
                # 不是同一个维度值：看只在一边被引用的它在另一边的快照里还有没有——有（只是不在这一行）是换了一个数，
                # 没有是真的没有了、这次才有
                if not (ot if nb > nt else ob):
                    continue
            else:
                cb, ct = cut(nb, ob), cut(nt, ot)
                if cb == ct:
                    continue
                if cb is not None and ct is not None and nb == nt:
                    # 都只引用了它的一部分、行数也一样，可排在引用的行前面的行数不同：引用的不是它同样的那几行
                    return _Change("unknown", f"{name}两次都引用{cited}，两次都引用了{named(key)}的 {nb} 行，可不是"
                                              f"它同样的那几行（它排在引用的行前面的，基准运行有 {cb[0]} 行，这次有 "
                                              f"{ct[0]} 行），取的不是{what}{masked_note}")
            # 说引用的行外面有它的那一边；两边都有的，说引用得少的那一边
            if ot and (not ob or nt < nb):
                where = (f"这次有 {len(ot)} 行在查询结果{_row_numbers(ot)}" if nt
                         else f"这次仍在查询结果{_row_numbers(ot)}")
            else:
                where = (f"在基准运行中有 {len(ob)} 行位于查询结果{_row_numbers(ob)}" if nb
                         else f"在基准运行中位于查询结果{_row_numbers(ob)}")
            return _Change("unknown", f"{name}两次都引用{cited}，{named(key)}{where}，不在引用的行里，"
                                      f"取的不是{what}{masked_note}")
        return None

    def _hidden(self, b: Any, t: Any) -> frozenset[str]:
        """两边输入落在的快照上要遮的列（小写）。"""
        return (self.base.masked.get(b.snapshot) or frozenset()) | (self.target.masked.get(t.snapshot) or frozenset())

    def _generated(self, path: str, writer: str, same: bool) -> _Change:
        """模型、人交出的值（Agent 没按出处交、模型调用、人工审批……）：没有出处，值变了说不清是数据变了还是它本身的
        差异——同样的数据、同样的配置，模型两次交出的数也可能不同。它自己查的库结构变了、它的配置变了照常标出。"""
        own = self._node_change(writer)
        if own is not None and own.kind == "sql":
            return own
        if writer in self.changed:
            return _Change("template", self._moved(writer))
        if own is not None and own.kind == "unknown":
            return own
        if same:
            return own or _Change("same")
        kind = TYPE_LABEL.get(self._type_of(writer) or "", "节点")
        return _Change("unknown", f"输入「{path}」由节点「{self.title(writer)}」（{kind}）直接给出，没有对应的查询快照，"
                                  "无法判断变化是否来自数据")

    def _run_input(self, path: str, b: Any, t: Any, same: bool | None, *, moved: bool) -> _Change:
        """运行输入（input.*、入口节点写进 vars 的）：变了就是参数变化。列表、对象按还原出来的完整值比，卡上只记了
        「[2 项]」这样的大概，长度不变、内容变了的不能当没变。"""
        if same is None:
            if moved:
                return _Change("unknown", f"运行输入「{path}」是列表或对象，当时没有保存完整值，无法核对是否有变化")
            return _Change("same")
        if same:
            return _Change("same")
        if b.shape == "container" or t.shape == "container":
            return _Change("params", f"运行输入「{path}」的内容有变化")
        return _Change("params", f"运行输入「{path}」由「{_shown(b.item.get('value'))}」"
                                 f"改为「{_shown(t.item.get('value'))}」")

    # ---------------------------------------------------------------- 模板

    def _template(self) -> tuple[dict[str, Any], dict[str, str], dict[str, list[str]]]:
        """两次运行的图快照比较：新增、删除的节点，配置有变化的节点和配置项名（不展示值）。

        changed 是影响取值的变化，判断输入的出处时用：节点 → added / removed / config / edges / defaults。只改名称的
        不算；图里的 SQL 只改了空白、大小写的不算（规范化以后一模一样）。图级的变化也落到节点上：连线有增删的，终点
        节点交出的值可能变了（接进来的东西、执行的条件变了）；默认设置改了的，运行时会读这几项（_reads）、又没有自行
        设置的节点跟着变，inherited 记着各自跟着变的是哪几项。"""
        before, after = self.base_nodes, self.target_nodes
        nodes: list[dict[str, Any]] = []
        changed: dict[str, str] = {}
        inherited: dict[str, list[str]] = {}

        def entry(node_id: str, node: dict[str, Any], change: str, keys: list[str]) -> dict[str, Any]:
            return {"node_id": node_id, "title": self._title_of(node), "change": change, "keys": keys,
                    "labels": [_key_label(k, node.get("type")) for k in keys]}

        for node_id, node in after.items():
            was = before.get(node_id)
            if was is None:
                nodes.append(entry(node_id, node, "added", []))
                changed[node_id] = "added"
                continue
            keys, _ = _config_keys(was, node)
            if keys:
                nodes.append(entry(node_id, node, "config", keys))
                sql_key = _SQL_KEY.get(str(node.get("type"))) if was.get("type") == node.get("type") else None
                if any(k != "label" and not (k == sql_key and self._same_graph_sql(_graph_sql(was), _graph_sql(node)))
                       for k in keys):
                    changed[node_id] = "config"
        for node_id, node in before.items():
            if node_id not in after:
                nodes.append(entry(node_id, node, "removed", []))
                changed[node_id] = "removed"
        edges_before, edges_after = _edges(self.base.graph), _edges(self.target.graph)
        for _, target_id, _ in edges_before ^ edges_after:
            if target_id in before and target_id in after:
                changed.setdefault(target_id, "edges")
        defaults_before = self.base.graph.get("defaults") or {}
        defaults_after = self.target.graph.get("defaults") or {}
        differing = [k for k in dict.fromkeys([*defaults_before, *defaults_after])
                     if defaults_before.get(k) != defaults_after.get(k) and k not in _QUIET_DEFAULTS]
        for node_id, node in after.items():
            was = before.get(node_id)
            if not differing or was is None:
                continue
            reads = _reads(was, defaults_before) | _reads(node, defaults_after)
            hit = [k for k in differing
                   if k in reads and _unset(_config(node).get(k)) and _unset(_config(was).get(k))]
            if hit:
                changed.setdefault(node_id, "defaults")
                inherited[node_id] = hit
        same_edges = edges_before == edges_after
        same_defaults = defaults_before == defaults_after
        if not same_edges:
            self.notes.append(EDGES_CHANGED)
        if not same_defaults:
            self.notes.append(DEFAULTS_CHANGED)
        return {"base_version": self.base.run.get("version"), "target_version": self.target.run.get("version"),
                "same_graph": not nodes and same_edges and same_defaults, "nodes": nodes}, changed, inherited

    # ---------------------------------------------------------------- 指标

    def _metrics(self) -> list[dict[str, Any]]:
        before, after = _cards(self.base), _cards(self.target)
        if not before and not after:
            self.notes.append(NO_CARDS)
            return []
        out: list[dict[str, Any]] = []
        for node_id in [*after, *(n for n in before if n not in after)]:
            b, t = before.get(node_id), after.get(node_id)
            for side, card in (("基准运行", b), ("这次运行", t)):
                if card is not None and card.runs > 1:
                    self.notes.append(f"口径卡「{self.title(node_id)}」在{side}中执行了 {card.runs} 次，按最后一次对比")
            if b is not None:
                _plan_card(self.base, b, self.base_queries)
            if t is not None:
                _plan_card(self.target, t, self.target_queries)
            for side, card in (("基准运行", b), ("这次运行", t)):
                if card is not None and card.problem:
                    self.notes.append(f"{side}的口径卡「{self.title(node_id)}」{card.problem}，其中的指标无法对比")
            self._card_notes(node_id, b, t)
            ids = [str(m["id"]) for m in (t.metrics() if t else [])]
            ids += [mid for mid in (str(m["id"]) for m in (b.metrics() if b else [])) if mid not in ids]
            for mid in ids:
                bm = next((m for m in (b.metrics() if b else []) if str(m["id"]) == mid), None)
                tm = next((m for m in (t.metrics() if t else []) if str(m["id"]) == mid), None)
                out.append(self._metric(node_id, b, t, bm, tm))
        return out

    def _card_notes(self, node_id: str, b: _Card | None, t: _Card | None) -> None:
        """口径卡本身的变化（口径名称、固定的来源版本）：不改变指标的分类，写进说明。"""
        if b is None or t is None or b.content is None or t.content is None:
            return
        before, after = b.content.get("caliber") or "", t.content.get("caliber") or ""
        if before != after:
            self.notes.append(f"口径卡「{self.title(node_id)}」的口径名称由「{before}」改为「{after}」")
        src_b, src_t = b.content.get("source"), t.content.get("source")
        if src_b != src_t:
            def version(src: Any) -> str:
                return f"v{src.get('workflow_version')}" if isinstance(src, dict) else "本地定义"
            self.notes.append(f"口径卡「{self.title(node_id)}」固定的口径来源由 {version(src_b)} 改为 {version(src_t)}")

    def _metric(self, card_id: str, b: _Card | None, t: _Card | None, bm: dict[str, Any] | None,
                tm: dict[str, Any] | None) -> dict[str, Any]:
        meta = tm or bm or {}
        mid = str(meta.get("id"))
        caliber = next((c.content.get("caliber") for c in (t, b) if c is not None and c.content), None)
        out: dict[str, Any] = {
            # 画布上的节点名；图里找不到这个节点（老运行的图读不出来）就写口径名
            "card_id": card_id, "card_title": self.title(card_id) if card_id in self.titles else caliber or card_id,
            "metric_id": mid, "name": meta.get("name") or mid, "unit": meta.get("unit") or "",
            "decimals": meta.get("decimals"),
            "base": _value(bm), "target": _value(tm),
            "base_rendered": _rendered(bm), "target_rendered": _rendered(tm),
            "delta": None, "delta_rendered": None, "delta_pct": None,
            "classification": INCOMPARABLE, "reasons": [], "decomposition": None, "decomposition_note": None,
            "inputs": [], "attribution": None,
        }
        bp = b.plans.get(mid) if b is not None and bm is not None else None
        tp = t.plans.get(mid) if t is not None and tm is not None else None
        out["inputs"] = self._inputs(bp, tp)
        # 一边有一边没有：整张卡都没有、卡里没有这个指标；卡读不出来的说不清有没有
        for card, metric, cls, side in ((b, bm, ADDED, "基准运行"), (t, tm, REMOVED, "这次运行")):
            if metric is not None:
                continue
            if card is None:
                out.update(classification=cls, reasons=[f"{side}没有口径卡「{self.title(card_id)}」"])
            elif card.content is None:
                out["reasons"] = [f"{side}的口径卡「{self.title(card_id)}」{card.problem}，无法对比"]
            else:
                out.update(classification=cls, reasons=[f"{side}的口径卡「{self.title(card_id)}」里没有这个指标"])
            return out
        assert bm is not None and tm is not None and b is not None and t is not None
        defs = (_definition(self.base.graph, card_id, mid), _definition(self.target.graph, card_id, mid))
        caliber = _caliber_reasons(bm, tm, b.content or {}, t.content or {}, defs)
        if shown := _display_change(bm, tm, defs):
            self.notes.append(f"口径卡「{self.title(card_id)}」的指标「{out['name']}」{shown}，只影响显示，不算口径变化")
        self._delta(out, bm, tm)
        lineage = isinstance(bm.get("inputs"), list) and isinstance(tm.get("inputs"), list)
        moved = not recompute._same(bm.get("value"), tm.get("value"))
        changes: dict[str, _Change] = {}
        if lineage and bp is not None and tp is not None:
            changes = {p: self.provenance(p, bp.inputs.get(p), tp.inputs.get(p), card=card_id, moved=moved)
                       for p in dict.fromkeys([*tp.inputs, *bp.inputs])}
        if caliber:
            # 口径优先；喂它的取数也变了、说不清的，接在口径的说明后面照样写出来，不因为口径变了就不提。只在一边才有
            # 的输入是改表达式带来的，口径的说明已经说了
            both = set(bp.inputs) & set(tp.inputs) if bp is not None and tp is not None else set()
            also = [c.reason for p, c in changes.items() if p in both and c.kind in ("sql", "template", "unknown")
                    and c.reason]
            out.update(classification=CALIBER_AND_DATA, reasons=list(dict.fromkeys([*caliber, *also])))
            self._decompose(out, bm, tm, b, t, bp, tp)
        elif not lineage:
            quiet = self.template["same_graph"] and all(self.shape(x, y).structure == "same" for x, y in self.pairs)
            out.update(classification=UNCHANGED if quiet and not moved else INCOMPARABLE,
                       reasons=[] if quiet and not moved else [NO_LINEAGE])
        else:
            worst = _worst(changes.values())
            reasons = list(dict.fromkeys(c.reason for c in changes.values() if c.kind == worst.kind and c.reason))
            if worst.kind in ("sql", "template"):
                out.update(classification=SQL_CHANGED, reasons=reasons, decomposition_note=SQL_NOT_SPLIT)
            elif worst.kind == "unknown":
                out.update(classification=INCOMPARABLE, reasons=reasons)
            elif worst.kind == "params":
                out.update(classification=PARAMS_ONLY, reasons=reasons)
            elif not moved:
                out.update(classification=UNCHANGED)
            elif all(inp.snapshot for inp in tp.inputs.values()) and tp.inputs:
                out.update(classification=DATA_ONLY, reasons=[DATA_FROM_QUERIES])
            else:
                # 「模板没有变化」只在整张图都没变时说；图有别的改动、都不在它的上游的，只说它的上游没变
                out.update(classification=DATA_ONLY, reasons=[
                    DATA_FROM_UPSTREAM if self.template["same_graph"] else DATA_FROM_UPSTREAM_SCOPED])
        for side, metric in (("基准运行", bm), ("这次运行", tm)):
            if metric.get("status") == "missing_input":
                out["reasons"].append(f"{side}这个指标缺少输入，没有值")
        if out["classification"] in _ATTRIBUTED and bp is not None and tp is not None:
            out["attribution"] = self._attribution(bp, tp, card_id)
        if any(inp.masked for plan in (bp, tp) if plan is not None for inp in plan.inputs.values()):
            self.masked_seen = True
        return out

    def _delta(self, out: dict[str, Any], bm: dict[str, Any], tm: dict[str, Any]) -> None:
        """总变化：两边都是数、数值的含义相同（单位一样、比率与否一样）才相减，按十进制精确算。"""
        b, t = _exact(bm.get("value")), _exact(tm.get("value"))
        if b is None or t is None or _scale(bm) != _scale(tm):
            return
        whole = all(isinstance(m.get("value"), int) for m in (bm, tm))
        delta = _plain(t - b, whole=whole)
        meta = {**tm, "decimals": _places(bm, tm)}
        out.update(delta=delta, delta_rendered=_signed(meta, delta) if delta != 0 else None, delta_pct=_percent(b, t))

    def _decompose(self, out: dict[str, Any], bm: dict[str, Any], tm: dict[str, Any], b: _Card, t: _Card, bp: Any,
                   tp: Any) -> None:
        """严格拆分，写进 out 的 decomposition（拆不了的原因写进 decomposition_note）。为了两效应之和在 JSON 里也逐位
        等于总变化，总变化可能按拆分的精度重写一遍（_split_exact）。"""
        split, note = self._split(bm, tm, b, t, bp, tp)
        out["decomposition"], out["decomposition_note"] = split, note
        if split is not None and "delta" in split:
            delta = split.pop("delta")
            meta = {**tm, "decimals": _places(bm, tm)}
            out.update(delta=delta, delta_rendered=_signed(meta, delta) if delta != 0 else None)

    def _split(self, bm: dict[str, Any], tm: dict[str, Any], b: _Card, t: _Card, bp: Any,
               tp: Any) -> tuple[dict[str, Any] | None, str | None]:
        """严格拆分：(拆分, None) 或 (None, 为什么没法拆)。"""
        old, new = _exact(bm.get("value")), _exact(tm.get("value"))
        if old is None or new is None:
            return None, NOT_NUMBERS
        if _scale(bm) != _scale(tm):
            return None, "单位或比率的写法不同，两边的数值不能直接相减，不做拆分"
        try:
            leaves = leaf_refs(parse_expression(str(bm.get("expression") or ""))[0])
        except ExpressionError as e:
            return None, f"旧口径的表达式无法解析（{e}），无法严格拆分"
        if bp is None or not isinstance(bm.get("inputs"), list):
            return None, "基准运行的口径卡没有记录输入（生成于系统升级前），无法确认取数是否有变化，无法严格拆分"
        missing = [p for p in leaves if p not in t.inputs]
        if missing:
            more = f"等 {len(missing)} 个输入" if len(missing) > 1 else ""
            return None, f"旧口径用到的「{missing[0]}」{more}在这次运行的口径卡里没有记录，无法严格拆分"
        used = [t.inputs[p] for p in leaves]
        plans = [p for p in (bp, tp) if p is not None]
        if any(inp.masked for inp in used) or any(inp.masked for plan in plans for inp in plan.inputs.values()):
            return None, SPLIT_MASKED
        for path in leaves:
            change = self.provenance(path, bp.inputs.get(path), t.inputs[path], card=t.node_id)
            if change.kind in ("sql", "template"):
                return None, f"旧口径用到的输入取数也有变化，无法严格拆分：{change.reason}"
            if change.kind == "unknown":
                return None, f"无法确认旧口径用到的输入只是数据有变化，无法严格拆分：{change.reason}"
        # f_old(x_new)：旧表达式配这次的输入，按推演重算的规则还原、求值，取整按旧口径卡的小数位
        synthetic = {"id": bm.get("id"), "expression": bm.get("expression"), "decimals": bm.get("decimals"),
                     "inputs": [inp.item for inp in used], "substituted": None, "value": None}
        assert t.sources is not None
        plan = recompute._plan(synthetic, recompute._Store(t.sources))
        if plan.problem:
            return None, f"无法严格拆分：{plan.problem}"
        for path in plan.tops:
            if problem := plan.inputs[path].problem:
                return None, f"无法严格拆分：这次运行的输入「{path}」无法还原（{problem}）"
        value, reason = recompute._evaluate(plan, {p: inp.value for p, inp in plan.inputs.items()})
        if reason is not None:
            return None, f"按旧口径在这次的数据上无法计算（{reason}），无法严格拆分"
        middle = _exact(value)
        if middle is None:
            return None, "按旧口径在这次的数据上算出的不是有限的数值，无法严格拆分"
        whole = all(isinstance(v, int) and not isinstance(v, bool) for v in (bm.get("value"), tm.get("value"), value))
        total, caliber_d, data_d = _split_exact(new - old, new - middle)
        caliber, data = _plain(caliber_d, whole=whole), _plain(data_d, whole=whole)
        meta = {**tm, "decimals": _places(bm, tm)}
        split = {"caliber_effect": caliber, "data_effect": data, "caliber_rendered": _signed(meta, caliber),
                 "data_rendered": _signed(meta, data)}
        if total != new - old:
            split["delta"] = _plain(total, whole=whole)
        return split, None

    def _inputs(self, bp: Any, tp: Any) -> list[dict[str, Any]]:
        """两边的输入按路径对齐：值（遮罩列、核对不了遮没遮的不给），各自落在的查询快照。"""
        b_in = bp.inputs if bp is not None else {}
        t_in = tp.inputs if tp is not None else {}
        out = []
        for path in dict.fromkeys([*t_in, *b_in]):
            b, t = b_in.get(path), t_in.get(path)
            entry: dict[str, Any] = {"path": path, "base": _input_value(b), "target": _input_value(t)}
            if any(inp is not None and inp.masked for inp in (b, t)):
                entry["masked"] = True
            if b is not None and b.snapshot:
                entry["base_artifact"] = b.snapshot
            if t is not None and t.snapshot:
                entry["target_artifact"] = t.snapshot
            out.append(entry)
        return out

    # ---------------------------------------------------------------- 维度归因

    def _attribution(self, bp: Any, tp: Any, card: str) -> dict[str, Any] | None:
        """挑一个落在查询快照上、两边都变了的输入做维度归因：先找给得出占比的，没有就取第一个列得出行的，再没有
        就取第一个说明（截断、度量列遮罩……）。一个都挑不出来返回 None。"""
        found: list[dict[str, Any]] = []
        for path, t in tp.inputs.items():
            b = bp.inputs.get(path)
            if b is None or not (b.snapshot and t.snapshot) or recompute._same(b.value, t.value):
                continue
            if self.provenance(path, b, t, card=card).kind not in ("same", "params"):
                continue
            result = self._attribute(b, t)
            if result is None:
                continue
            if result["rows"] and all(r["share"] is not None for r in result["rows"]):
                return result
            found.append(result)
        return next((r for r in found if r["rows"]), found[0] if found else None)

    def _attribute(self, b: Any, t: Any) -> dict[str, Any] | None:
        """一个输入的维度归因：{path, base_artifact, target_artifact, columns, measure, rows, total_delta, note}；
        归不了因的照实写在 note 里（rows 为空）。快照里没有可以当维度的列（只有一行汇总这种）返回 None：没什么可归的。"""
        measure = _locator(b).get("column")
        if not isinstance(measure, str) or _locator(t).get("column") != measure:
            return None
        sb, st = self._snap(self.base_queries, b), self._snap(self.target_queries, t)
        # 归因用的是哪个输入、两边各自哪份快照：一个指标有几个落在快照上的输入时分得清
        trace = {"path": t.path, "base_artifact": b.snapshot, "target_artifact": t.snapshot}
        empty = {**trace, "columns": [], "measure": measure, "rows": [], "total_delta": None}
        if sb is None or st is None:
            return {**empty, "note": UNREADABLE_SNAPSHOT}
        for side, snap in (("基准运行", sb), ("这次运行", st)):
            if snap.get("truncated"):
                # 截断的结果只是前几行：各键的和不是完整的和，按它归因会把没取回的行的变化算到别的键头上
                return {**empty, "note": TRUNCATED.format(side=side, rows=len(snap["rows"]))}
        hidden = self._hidden(b, t)
        if any(str(c).lower() in hidden for c in [*(sb.get("columns") or []), *(st.get("columns") or [])]):
            self.masked_seen = True
        if measure.lower() in hidden:
            return {**empty, "note": MASKED_MEASURE.format(column=measure)}
        rb, rt = _attributed_rows(sb, b), _attributed_rows(st, t)
        if rb is None or rt is None:
            return None
        values = (_input_total(b), _input_total(t))
        notes: list[str] = []
        if b.row is not None and t.row is not None and not _adds_up(rb, rt, measure, values):
            # 取的是合计行那一格：其余各行之和等于它（两边都等）时按其余各行归因，合计行不列进去
            kb, kt = [x for x in rb if x[0] != b.row], [x for x in rt if x[0] != t.row]
            if kb and kt and len(kb) < len(rb) and len(kt) < len(rt) and _adds_up(kb, kt, measure, values):
                rb, rt = kb, kt
                notes.append(TOTAL_ROW.format(row=t.row + 1))
        dims, apart = _dimensions(sb, st, rb, rt, measure, hidden)
        if not dims:
            return {**empty, "note": "；".join(apart)} if apart else None
        notes += apart
        groups_b, groups_t = _group(rb, dims, measure), _group(rt, dims, measure)
        if groups_b is None or groups_t is None:
            return {**empty, "note": NOT_A_MEASURE.format(column=measure)}
        total_b, total_t = sum(groups_b.values(), Decimal(0)), sum(groups_t.values(), Decimal(0))
        total = total_t - total_b
        # 占比只在这份结果正好是这个输入的拆分时才有意义：各键之和等于输入的值，两边都等
        share = _equal(values[0], total_b) and _equal(values[1], total_t)
        if not share:
            notes.append(NO_SHARE)
        elif total == 0:
            notes.append(ZERO_TOTAL)
        rows = []
        for key in dict.fromkeys([*groups_b, *groups_t]):
            vb, vt = groups_b.get(key), groups_t.get(key)
            delta = (vt or Decimal(0)) - (vb or Decimal(0))
            rows.append((abs(delta), {"key": list(key), "base": None if vb is None else _plain(vb, whole=False),
                                      "target": None if vt is None else _plain(vt, whole=False),
                                      "delta": _plain(delta, whole=False),
                                      "share": _plain(delta / total, whole=False) if share and total != 0 else None}))
        rows.sort(key=lambda r: -r[0])         # 稳定排序：变化一样大的按先出现的
        if len(rows) > TOP_KEYS:
            notes.append(MORE_KEYS.format(count=len(rows), top=TOP_KEYS))
        return {**trace, "columns": dims, "measure": measure, "rows": [r for _, r in rows[:TOP_KEYS]],
                "total_delta": _plain(total, whole=False), "note": "；".join(notes) or None}

    def _snap(self, queries: list[_Query], inp: Any) -> dict[str, Any] | None:
        q = self._side_query(queries, inp)
        return q.snap if q is not None else None


# --------------------------------------------------------------------------
# 指标、输入的几样小事
# --------------------------------------------------------------------------


def _value(metric: dict[str, Any] | None) -> Any:
    """指标的值：有限的数才给，别的（空值、文本、溢出成无穷大的）给 None，写法看 rendered。"""
    value = (metric or {}).get("value")
    return value if recompute._number(value) else None


def _rendered(metric: dict[str, Any] | None) -> str | None:
    """卡上记的写法（报告里就是这几个字）；没记的按卡上的格式渲染。"""
    if metric is None:
        return None
    rendered = metric.get("rendered")
    return rendered if isinstance(rendered, str) else recompute._render(metric, metric.get("value"))


def _setting(metric: dict[str, Any], definition: dict[str, Any] | None, key: str) -> tuple[bool, Any]:
    """指标的小数位（decimals）或显示格式（format）：(知不知道, 值)。卡上记了的按卡上的；升级前的卡没记这两项（当时
    按定义里的小数位取过整），按那次运行图里的指标定义（和当时一样规整）；定义也找不到的不知道，不拿来比。"""
    if key in metric:
        value = metric[key]
        return True, (value or DEFAULT_FORMAT) if key == "format" else value
    if definition is None:
        return False, None
    display, problem = _display_of(definition)
    return (False, None) if problem else (True, display[key])


def _caliber_reasons(bm: dict[str, Any], tm: dict[str, Any], bc: dict[str, Any], tc: dict[str, Any],
                     defs: tuple[dict[str, Any] | None, dict[str, Any] | None] = (None, None)) -> list[str]:
    """口径变了的地方，一处一句；没变返回空列表。defs 是两次运行图里这个指标的定义（老卡没记小数位、格式时用）。"""
    out = []
    if _expression(bm) != _expression(tm):
        out.append(f"表达式由「{_shown(bm.get('expression'))}」改为「{_shown(tm.get('expression'))}」")
    before, after = str(bc.get("caliber_version") or ""), str(tc.get("caliber_version") or "")
    if before != after:
        out.append(f"口径版本由 {before or '未设置'} 改为 {after or '未设置'}")
    (kb, db), (kt, dt) = _setting(bm, defs[0], "decimals"), _setting(tm, defs[1], "decimals")
    if kb and kt and db != dt:
        def places(d: Any) -> str:
            return "未设置" if d is None else str(d)
        out.append(f"小数位由 {places(db)} 改为 {places(dt)}")
    if (bm.get("unit") or "") != (tm.get("unit") or ""):
        out.append(f"单位由「{bm.get('unit') or '无'}」改为「{tm.get('unit') or '无'}」")
    (kb, fb), (kt, ft) = _setting(bm, defs[0], "format"), _setting(tm, defs[1], "format")
    if kb and kt and (fb == _RATIO) != (ft == _RATIO):
        # 比率（0.087 显示成 8.7%）和普通数值的含义不同；千分位、不分组只是写法，见 _display_change
        out.append(f"显示格式由「{_FORMAT_LABEL.get(fb, fb)}」改为「{_FORMAT_LABEL.get(ft, ft)}」")
    return out


def _display_change(bm: dict[str, Any], tm: dict[str, Any],
                    defs: tuple[dict[str, Any] | None, dict[str, Any] | None]) -> str | None:
    """只影响显示的格式变化（千分位和不分组互换）：一句说明；没有返回 None。"""
    (kb, fb), (kt, ft) = _setting(bm, defs[0], "format"), _setting(tm, defs[1], "format")
    if not (kb and kt) or fb == ft or _RATIO in (fb, ft):
        return None
    return f"显示格式由「{_FORMAT_LABEL.get(fb, fb)}」改为「{_FORMAT_LABEL.get(ft, ft)}」"


def _input_value(inp: Any) -> Any:
    """输入在对比里给人看的值：遮罩列、落在读不出来的快照上的不给；容器写卡上记的大概。"""
    if inp is None or inp.masked or (inp.unreadable and inp.shape == "scalar"):
        return None
    if inp.shape == "container":
        return inp.item.get("value")
    return recompute._finite(inp.item.get("value"))


def _locator(inp: Any) -> dict[str, Any]:
    locator = inp.item.get("locator")
    return locator if isinstance(locator, dict) else {}


def _same_input(b: Any, t: Any) -> bool | None:
    """一个输入两边的值相不相同。容器按还原出来的完整值比（卡上只记了「[2 项]」这样的大概，长度相同、内容不同的
    不能当一样）；完整值还原不出来时，大概不同的就是不同，大概相同的说不清，返回 None。"""
    if b.shape == "container" or t.shape == "container":
        if b.shape == t.shape == "container" and b.problem is None and t.problem is None:
            return recompute._same(b.value, t.value)
        return None if recompute._same(b.item.get("value"), t.item.get("value")) else False
    return recompute._same(b.item.get("value"), t.item.get("value"))


def _cited(inp: Any, count: int) -> tuple[int, int]:
    """输入引用了快照的哪几行（第一行, 最后一行），count 是快照共几行：单格是那一行，数组字段是它那一段行，容器本身
    是一份查询结果的是全部的行。"""
    if inp.row is not None:
        return inp.row, inp.row
    if inp.rows is not None:
        return inp.rows
    return 0, count - 1


def _whole(inp: Any, q: _Query) -> bool | None:
    """这个输入引用的是不是整份查询结果：引用的行从第一行到最后一行（容器本身就是整份结果）。快照读不出来、不知道
    结果共几行的返回 None。"""
    if q.snap is None:
        return None
    count = len(q.snap["rows"])
    return _cited(inp, count) == (0, count - 1)


def _key_text(key: tuple[Any, ...]) -> str:
    """维度值写进说明：几列的用「 / 」连起来，和对比页上归因行的写法一样。"""
    return "「" + " / ".join(_shown(v) for v in key) + "」"


def _rows_text(inp: Any) -> str:
    if inp.row is not None:
        return f"第 {inp.row + 1} 行"
    if inp.rows is not None:
        return f"第 {inp.rows[0] + 1} 至 {inp.rows[1] + 1} 行"
    return "整份结果"


def _row_numbers(rows: list[int]) -> str:
    """快照里的几行（行号从 0 数）写进说明：「第 5 行」「第 5、6 行」，多的写「第 5、6、7 行等」。"""
    shown = "、".join(str(i + 1) for i in rows[:_ROWS_SHOWN])
    return f"第 {shown} 行" + ("等" if len(rows) > _ROWS_SHOWN else "")


def _columns_text(columns: Any) -> str:
    if isinstance(columns, dict):
        return "「" + "、".join(str(c) for c in columns.values()) + "」"
    return f"「{columns}」" if columns else "（未记录）"


def _merge_inputs(q: _Query) -> dict[str, str] | None:
    """合并查询的输入：别名 → 上游快照。没记录的返回 None。"""
    if q.inputs is None:
        return None
    return {str(i["alias"]): str(i["artifact"]) for i in q.inputs
            if isinstance(i, dict) and i.get("alias") and i.get("artifact")}


def _records(snap: dict[str, Any], span: tuple[int, int] | None) -> list[tuple[int, dict[str, Any]]] | None:
    """快照里的行：(行号, {列: 值})。span 给了只取那一段行（归因时数组字段只用它那一段），None 是整份结果（单格按
    整份结果归因）。行的形状不对返回 None。"""
    columns = [str(c) for c in snap.get("columns") or []]
    rows = snap.get("rows") or []
    first, last = span if span is not None else (0, len(rows) - 1)
    out = []
    for i in range(first, min(last, len(rows) - 1) + 1):
        row = rows[i]
        if isinstance(row, dict):
            out.append((i, {str(k): v for k, v in row.items()}))
        elif isinstance(row, (list, tuple)) and len(row) == len(columns):
            out.append((i, dict(zip(columns, row))))
        else:
            return None
    return out


def _attributed_rows(snap: dict[str, Any], inp: Any) -> list[tuple[int, dict[str, Any]]] | None:
    """归因用的行（_attribute 在这些行上认维度、按维度汇总）：数组字段是它引用的那一段行，单格是整份结果（单格按
    整份结果归因，取的是合计行那一格时再去掉合计行）。_left_out 在同样这些行上再认一遍维度、按它核对引用的是不是同一
    批维度值：归因按哪组维度说，就按哪组维度核对，两处取行的规则只写在这里。单格核对的是那一行本身，归因不把它和别的
    行合在一起算，去不去合计行不影响核对。"""
    return _records(snap, inp.rows)


def _kind(snap: dict[str, Any], column: str) -> str | None:
    types = snap.get("column_types")
    kind = types.get(column) if isinstance(types, dict) else None
    return kind if isinstance(kind, str) else None


def _numeric(values: Iterable[Any], kind: str | None) -> bool:
    """这一列是数值列：查询时记的类型是 number，或者没记类型、值（数字文本也算）全是数。"""
    if kind is not None:
        return kind == "number"
    present = [v for v in values if v is not None]
    return bool(present) and all(is_number(cell_value(v)) for v in present)


def _dimensions(sb: dict[str, Any], st: dict[str, Any], rb: list[tuple[int, dict[str, Any]]],
                rt: list[tuple[int, dict[str, Any]]], measure: str,
                hidden: frozenset[str] | set[str]) -> tuple[list[str] | None, list[str]]:
    """维度列：(列名列表, 说明)。两边都有、不是数值、不是日期类型、没遮罩的列；两边各自都只有一个值的不算（分不出
    维度）；两边没有一个值相同的不拿来配对（日期、期次这类每期都不同的列），写进说明。一个候选都没有返回 None。"""
    common = [c for c in (str(c) for c in sb.get("columns") or []) if c in {str(x) for x in st.get("columns") or []}]
    candidates = []
    for column in common:
        if column == measure or column.lower() in hidden:
            continue
        kinds = (_kind(sb, column), _kind(st, column))
        if any(k in _TEMPORAL for k in kinds):
            continue
        vb, vt = [r.get(column) for _, r in rb], [r.get(column) for _, r in rt]
        if _numeric(vb, kinds[0]) or _numeric(vt, kinds[1]):
            continue
        candidates.append((column, vb, vt))
    if not candidates:
        return None, []
    dims, apart = [], []
    for column, vb, vt in candidates:
        seen_b, seen_t = {_hashable(v) for v in vb}, {_hashable(v) for v in vt}
        if len(seen_b) <= 1 and len(seen_t) <= 1:
            continue
        if seen_b and seen_t and not seen_b & seen_t:
            apart.append(column)
            continue
        dims.append(column)
    return dims, ([NO_OVERLAP.format(columns="、".join(apart))] if apart else [])


def _hashable(value: Any) -> Any:
    """维度值当键：文本、数、布尔、空值原样；别的（列表、对象）和非数、无穷大写成文字——键要能比较，也要写得进 JSON。"""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value if isinstance(value, (str, int, float, bool)) or value is None else repr(value)


def _measure(raw: Any) -> Decimal | None:
    """度量列的一格，十进制：空值按 0（SQL 的 SUM 也不计空值），数字文本按数，别的不是数返回 None。"""
    return Decimal(0) if raw is None else _exact(cell_value(raw))


def _group(rows: list[tuple[int, dict[str, Any]]], dims: list[str],
           measure: str) -> dict[tuple[Any, ...], Decimal] | None:
    """按维度键汇总度量：{键: 和}（去掉了期次列之后同一个键可能有几行）。有不是数值的格返回 None。"""
    out: dict[tuple[Any, ...], Decimal] = {}
    for _, row in rows:
        number = _measure(row.get(measure))
        if number is None:
            return None
        key = tuple(_hashable(row.get(d)) for d in dims)
        out[key] = out.get(key, Decimal(0)) + number
    return out


def _input_total(inp: Any) -> Decimal | None:
    """输入的值（数组字段是各项之和），十进制。不是数的给 None：这时不给占比。"""
    value = inp.value
    if isinstance(value, list):
        parts = [_exact(cell_value(v)) for v in value if v is not None]
        return sum(parts, Decimal(0)) if all(p is not None for p in parts) else None  # type: ignore[arg-type]
    return _exact(cell_value(value))


def _adds_up(rb: list[tuple[int, dict[str, Any]]], rt: list[tuple[int, dict[str, Any]]], measure: str,
             values: tuple[Decimal | None, Decimal | None]) -> bool:
    """度量列之和等于输入的值（两边都等）。"""
    for rows, value in ((rb, values[0]), (rt, values[1])):
        parts = [_measure(row.get(measure)) for _, row in rows]
        if any(p is None for p in parts) or not _equal(value, sum(parts, Decimal(0))):  # type: ignore[arg-type]
            return False
    return True


def _equal(value: Decimal | None, total: Decimal) -> bool:
    """输入的值和度量之和相等：十进制逐位比；浮点算出来的值差在最后几位的也算（相对误差 1e-9 以内）。"""
    if value is None:
        return False
    if value == total:
        return True
    scale = max(abs(value), abs(total))
    return scale != 0 and abs(value - total) <= scale * Decimal("1e-9")
