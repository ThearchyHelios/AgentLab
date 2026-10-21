"""分析结论的契约（docs/design/analysis-round/00-总方案.md §3.1，细案 01 §1）。

一次分析（agent 调一次分析工具，或分析卡里的一项）产出一份**分析结论**：内容寻址存成工件（ARTIFACT_KIND），证据台账里记一条
LEDGER_KIND，写作目录编号 A1、A2……。这里只定形状、名字、缺省值和几个纯函数；算子在 app/analysis 的各模块里，入口（agent
工具、分析卡）、证据接入、说法核对都按这里写，不各自另起一套。

- **纯函数**：结论只由输入快照里的格、probes（现查的表范围）、params、settings、method 决定；证据面板、导出、可验证报告用
  recompute 按同一个函数复算，逐字段相同（same）才算核对通过。
- **模型只选算什么**：AnalysisRequest 里没有阈值；阈值只来自设置快照（run.options["analysis"]）和分析卡上写的覆盖。
- **算不了是一条结论**（status refused + reason_code），调用写错是 CallError（不记结论，把 message 交回 agent 改）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any, Literal, NotRequired, TypedDict

# --------------------------------------------------------------------------
# 名字（一种说法：别处一律引用这里，不另写字面量）
# --------------------------------------------------------------------------

#: 分析结论的工件种类
ARTIFACT_KIND = "analysis_result"
#: 证据台账里的条目种类（engine/evidence.EvidenceEntry.kind）
LEDGER_KIND = "analysis"
#: 写作目录编号的前缀：A1、A2……（和 Q、K 同一套按台账先后编号的规则）
CATALOG_PREFIX = "A"
#: 引用标记的种类：[[a:A3]]、[[a:A3.top_share|pct]]
MARKER_KIND = "a"
#: 分析卡的节点类型
NODE_TYPE = "analysis"
#: 设置分组的键：settings 里的 analysis 一组、run.options["analysis"]
SETTINGS_KEY = "analysis"

Op = Literal["decompose", "baseline", "trend", "differ", "comparable"]
OPS: tuple[str, ...] = ("decompose", "baseline", "trend", "differ", "comparable")
#: 有模式的算子和它们的模式；其余算子 mode 为 None
MODES: dict[str, tuple[str, ...]] = {
    "decompose": ("additive", "ratio", "factor"),
    "differ": ("rate", "mean", "rows"),
}
#: 每个算子（和模式）的方法名与版本：改算法就升版本号，结论里记下实际用的那一版
METHODS: dict[tuple[str, str | None], str] = {
    ("decompose", "additive"): "decompose.additive@1",
    ("decompose", "ratio"): "decompose.ratio@1",
    ("decompose", "factor"): "decompose.factor@1",
    ("baseline", None): "baseline.robust@1",
    ("trend", None): "trend.theil_sen_mk@1",
    ("differ", "rate"): "differ.rate@1",
    ("differ", "mean"): "differ.welch@1",
    ("differ", "rows"): "differ.welch_rows@1",
    ("comparable", None): "comparable@1",
}
#: agent 分析工具的名字（tools/builtin 里注册），一个算子一个工具：参数各自聚焦，模型少写错
TOOL_NAMES: dict[str, str] = {op: f"analysis_{op}" for op in OPS}
#: 算子、模式给人看的叫法，只有这一份：证据面板、写作目录、审计、导出、可验证报告、裁判摘录、分析卡的校验与表单
#: （engine/labels 的 ANALYSIS_OP_LABEL / ANALYSIS_MODE_LABEL 引用这里）、「{叫法}没有做成」的判定语都用它（method_label 拼成一句）
OP_LABELS: dict[str, str] = {
    "decompose": "变化拆解", "baseline": "波动基线", "trend": "趋势判断", "differ": "差异检验", "comparable": "可比性检查",
}
MODE_LABELS: dict[str, str] = {
    "additive": "可加", "ratio": "比率", "factor": "乘积", "rate": "比率", "mean": "均值", "rows": "逐行明细",
}


def method_label(op: Any, mode: Any = None) -> str:
    """「变化拆解（可加）」「波动基线」。认不出的算子、模式原样写出来，不编叫法。"""
    name = OP_LABELS.get(str(op or ""), str(op or ""))
    return f"{name}（{MODE_LABELS.get(str(mode), str(mode))}）" if mode else name


def is_result(content: Any) -> bool:
    """一件工件的内容是不是分析结论：按形状认（op 是五个算子之一、method 是文字、inputs 是列表）。证据接入取回 ARTIFACT_KIND
    的工件、SQL 问题顺着输入往下找、可验证报告认嵌进来的工件，都按这一个函数认。"""
    return isinstance(content, Mapping) and content.get("op") in OPS and isinstance(content.get("method"), str) \
        and isinstance(content.get("inputs"), list)

#: 每个算子可能给出的判定（细案 01 §2–§6）
VERDICTS: dict[str, tuple[str, ...]] = {
    "decompose": ("flat", "driver", "concentrated", "dispersed", "unclosed", "mix_driven", "rate_driven", "both"),
    "baseline": ("within_band", "outside_band", "far_outside_band"),
    "trend": ("rising", "falling", "no_trend"),
    "differ": ("differs", "no_evidence"),
    "comparable": ("comparable", "conditional", "incomparable"),
}
#: 算不了的原因（细案 01 §8.2）。算不了也记成结论：写作者可以照实写「历史只有 5 周，判断不了是否异常」。最后两种是分析卡
#: 里随数据变化的调用错误记成的（analysis.run 的 refuse_data_errors，见 app/analysis 的 DATA_ERRORS）：parent_refused 上一层
#: 没做成；input_unavailable 这次的数据里没有要用的那一项（下钻的段、期次、总数那一格、要比的组……，reason 写是哪一项、哪个值）。
#: untestable：差异检验一对都拿不到 p 值（两组各 1 个样本、样本量为 0……）
REASONS: tuple[str, ...] = (
    "truncated", "too_few_segments", "history_short", "partial_target", "target_gap", "series_gap",
    "period_unreadable", "zero_base", "non_positive", "incomparable_sides", "parent_refused", "input_unavailable",
    "untestable",
)
#: 附带说明的种类（caveats[].code）
CAVEATS: tuple[str, ...] = (
    "offset", "simpson", "small_denominator", "record_high", "record_low", "weekday_effect",
    "multiple_comparisons", "outlier_sensitive", "small_sample", "measure_unlabeled", "null_values",
    "headline_basis", "comparability", "partial_periods", "untested",
)

# --------------------------------------------------------------------------
# 设置（细案 01 §10）：键、缺省、取值范围。api/settings.py 的 analysis 一组、发起时的快照、分析卡上的覆盖都按这张表
# --------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "main_share": 0.5,
    "concentration": 0.8,
    "offset_ratio": 0.25,
    "flat_pct": 0.005,
    "k_warn": 2.0,
    "k_alert": 3.0,
    "min_history": 8,
    "max_history": 52,
    "trend_min_points": 6,
    "trend_alpha": 0.05,
    "differ_alpha": 0.05,
    "min_n": 30,
    "min_expected": 5,
    "min_denominator": 30,
    "max_calls": 12,
    "max_drill_depth": 3,
    "table_top_k": 10,
}
#: 每个键的类型和闭区间。整数键只收整数（布尔不算）
RANGES: dict[str, tuple[type, float, float]] = {
    "main_share": (float, 0.3, 0.9),
    "concentration": (float, 0.5, 0.99),
    "offset_ratio": (float, 0.05, 1.0),
    "flat_pct": (float, 0.0, 0.1),
    "k_warn": (float, 1.0, 5.0),
    "k_alert": (float, 1.5, 8.0),
    "min_history": (int, 4, 104),
    "max_history": (int, 8, 260),
    "trend_min_points": (int, 4, 104),
    "trend_alpha": (float, 0.001, 0.2),
    "differ_alpha": (float, 0.001, 0.2),
    "min_n": (int, 2, 10_000),
    "min_expected": (int, 1, 50),
    "min_denominator": (int, 1, 100_000),
    "max_calls": (int, 1, 50),
    "max_drill_depth": (int, 1, 6),
    "table_top_k": (int, 3, 30),
}

#: 每个键给人看的叫法（设置页、报错都用它，句子里不裸写键名）
LABELS: dict[str, str] = {
    "main_share": "「主要来源」的占比",
    "concentration": "「集中」的合计占比",
    "offset_ratio": "记为「抵消」的比例",
    "flat_pct": "「基本持平」的幅度",
    "k_warn": "「超出常规波动」的倍数",
    "k_alert": "「明显超出」的倍数",
    "min_history": "至少几期历史",
    "max_history": "最多用几期历史",
    "trend_min_points": "趋势至少几期",
    "trend_alpha": "趋势的显著性水平",
    "differ_alpha": "差异检验的显著性水平",
    "min_n": "均值比较的最小样本数",
    "min_expected": "比率比较的最小期望频数",
    "min_denominator": "比率拆解的最小分母",
    "max_calls": "Agent 每次执行最多分析几次",
    "max_drill_depth": "最多下钻几层",
    "table_top_k": "列出前几段",
}


def settings_problem(values: Mapping[str, Any]) -> str | None:
    """一组分析设置（或分析卡上的覆盖）哪里不对：不认识的键、类型不对、超出范围、k_alert 不大于 k_warn、
    max_history 小于 min_history。都对返回 None。说法给人看。"""
    for key, value in values.items():
        if key not in RANGES:
            return f"不认识的分析设置：{key}"
        kind, low, high = RANGES[key]
        label = LABELS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{label}要填数字"
        if kind is int and not isinstance(value, int):
            return f"{label}要填整数"
        if not low <= value <= high:
            return f"{label}要在 {low:g} 到 {high:g} 之间"
    merged = {**DEFAULTS, **values}
    if merged["k_alert"] <= merged["k_warn"]:
        return f"{LABELS['k_alert']}要大于{LABELS['k_warn']}"
    if merged["max_history"] < merged["min_history"]:
        return f"{LABELS['max_history']}不能小于{LABELS['min_history']}"
    return None


def effective_settings(snapshot: Mapping[str, Any] | None, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """这次分析实际用的阈值：缺省 ← 发起时的快照 ← 分析卡上的覆盖（agent 工具没有覆盖）。"""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (snapshot or {}).items() if k in DEFAULTS})
    out.update({k: v for k, v in (overrides or {}).items() if k in DEFAULTS})
    return out


# --------------------------------------------------------------------------
# 形状。都是普通 dict，TypedDict 只用来写清形状（同 engine/evidence.py 的写法）
# --------------------------------------------------------------------------


class InputSnapshot(TypedDict, total=False):
    """算子的一份输入：本次运行里交回过的一份查询快照（tools/datasource 的 to_payload 加上出处）。"""

    ref: str                  # 调用里写的名字：agent 用本节点的编号（Q3），分析卡用来源节点 id
    artifact: str             # 查询快照工件的 sha256（台账里 kind query 那条的 artifact）
    node_id: str              # 交回这份快照的节点
    source: str               # 数据源名
    sql: str
    columns: list[str]
    # 查询时记下的列类型（number / text / date / datetime / time / boolean，拿不准的列不在里面）。tools 的 to_payload 给的是
    # {列: 类型}（data/engine.QueryResult.column_types）；按列序排的列表也认（analysis/inputs.Snap.kind 两种都读）
    column_types: dict[str, str] | list[str | None]
    rows: list[list[Any]]
    truncated: bool
    masked: list[str]         # 不能当维度、度量的列（遮罩列、按 SQL 认出改了名字的遮罩列），比较时不分大小写
    # 以下几项照查询快照里的原样带上（第 1 波 W1-a 加，都可选）：
    dialect: str              # SQL 的 sqlglot 方言（sqlite / mysql / postgres / oracle）；不给时按探查器的 dialect(数据源名)
    checks: list[dict[str, Any]]    # 快照里的 SQL 检查结果（level 为 error 的算可比性的数据问题）
    no_data: list[dict[str, Any]]   # 快照里的「查询区间里这张表没有数据」
    mask_columns: list[str]   # 没给 masked 时（复算取回的是快照工件本身）按这两项认遮罩列
    masked_at_query: list[str]


class AnalysisInput(TypedDict, total=False):
    """结论里记下的一份输入：用了哪份快照的哪些列、哪些行，或者哪一格。复算按 artifact 取回快照。"""

    ref: str                  # 调用里写的名字（Q3、Q1.r0.gmv、A3）
    artifact: str
    role: str                 # data（主输入）、base / target（两份快照）、headline_base / headline_target、parent
    columns: list[str]
    rows: Literal["all"] | list[int]
    row: int                  # 一格时：第几行、哪一列、值（十进制字符串）
    column: str
    value: str | None
    dialect: str              # 读这份快照的 SQL 用的方言（第 1 波 W1-a 加，可选）：复算按它读，界的写法才一样
    masked: list[str]         # 这份快照当时不能当维度、度量的列（小写，只记列名；第 1 波 W1-a 加，可选）：复算按它认


class Probe(TypedDict, total=False):
    """可比性检查现查的一张表的一列的实际范围（data/coverage 的探查；失败退回剖析、取值观察）。复算用记下的值。"""

    source: str
    table: str
    column: str
    min: str | None           # ISO 日期
    max: str | None
    basis: Literal["live", "profile", "observed"]
    at: str                   # 现查的时刻（ISO，服务端时区）
    # 按运行当天现算的界（CURRENT_DATE、date('now', …)）在库里算出的值（第 1 波 W1-a 加，可选）：有 expr 的这一条记的是界，
    # 没有 table / column / min / max
    expr: str                 # 界的 SQL 写法（按这份快照的方言）
    value: str | None         # 库里算出的日期（带时刻的写成 YYYY-MM-DD HH:MM:SS）


class Verdict(TypedDict, total=False):
    code: str                 # VERDICTS[op] 之一
    subjects: list[list[str]]  # 点名的段（维度键，一段一个列表，多维时几个值）或因子名；没有点名时为空
    direction: Literal["up", "down"] | None


class Caveat(TypedDict):
    code: str                 # CAVEATS 之一
    text: str                 # 渲染好的一句（一种说法）


class ResultTable(TypedDict):
    """结果表：形状同查询快照（columns / rows），[[v:A3.r0.share]]、[[table:A3 …]] 按它取格。"""

    columns: list[str]        # 稳定的列 id（维度列照输入的列名，其余是 base、target、delta、share……）
    labels: dict[str, str]    # 列 id → 给人看的列名（基期、本期、变化、占变化的比重……）
    rows: list[list[Any]]     # 数一律十进制字符串或 None，维度值照原样
    # 列 id → 类型（写法同查询快照的 column_types；第 1 波 W1-a 加，可选）：维度列照输入快照里那一列查询时记下的类型
    # （文本型的码值 "1203" 才不会被渲染成「1,203」；输入没记类型的不写），数值列 number，说明、叫法这类文字列 text
    column_types: NotRequired[dict[str, str]]


class AnalysisRequest(TypedDict, total=False):
    """一次分析的请求：只有「算什么」。agent 工具的参数、分析卡的一项都规范成这个形状。"""

    op: str
    mode: str | None
    params: dict[str, Any]    # 各算子的参数（细案 01 §2–§6）：query / by / period / base / target / measure / numerator /
                              # denominator / factors / product / headline / parent / target / basis / grain / lag / label……
    hypothesis: str           # agent 写的这次在验证什么，只在证据面板显示，不进任何系统渲染的正文


class AnalysisResult(TypedDict, total=False):
    op: str
    mode: str | None
    method: str               # METHODS 里的一项
    params: dict[str, Any]    # 规范化后的参数
    settings: dict[str, Any]  # 实际用的阈值（effective_settings）
    inputs: list[AnalysisInput]
    probes: list[Probe]
    status: Literal["ok", "refused"]
    reason_code: str | None   # REASONS 之一（refused 时）
    reason: str | None
    table: ResultTable | None
    values: dict[str, Any]    # 具名标量：[[a:A3.top_share]]；数一律十进制字符串
    verdict: Verdict | None
    criterion: str            # 判定标准，一句，照实际阈值写
    phrase: str               # 判定语（app/analysis/phrases 渲染），[[a:A3]] 渲染成它
    caveats: list[Caveat]
    hypothesis: str | None


class CallError(Exception):
    """调用错误（细案 01 §8.1）：列不存在、列被遮罩、角色和目录度量类型冲突、同一键同一期多行、期次不在列里、乘积
    不闭合、parent 不对、下钻超深、这个 Agent 这次执行的分析次数用完（max_calls 按每个 Agent 每次执行计）。不记结论，message
    交回 agent（一种说法，告诉它怎么改）。

    随数据变化的那几种（app/analysis 的 DATA_ERRORS：这期没有要用的期次、段、组，上一层没做成……）另带 reason：照实说是哪一项、
    哪个值，不带改法。分析卡（模板定死了调用）按它记成算不了的结论，见 analysis.run 的 refuse_data_errors。"""

    def __init__(self, code: str, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


#: 现查一列的实际范围：(数据源名, 表, 列) → Probe；查不了返回 None（算子写「核对不了」，不判不可比）
Prober = Callable[[str, str, str], Awaitable[Probe | None]]


class Context(TypedDict, total=False):
    """入口交给算子的运行期依赖。复算不用它（probes 已记在结论里）。"""

    today: date               # 运行当天（服务端时区）：判本期完不完整
    probe: Prober | None      # None 时只用 facts
    catalogs: Mapping[str, Any]   # 数据源名 → 数据目录（app/data/catalog 的 notes）：认度量类型、业务日期列、列的叫法
    facts: Mapping[str, Any]      # 数据源名 → 剖析事实（探查失败时兜底）：{表名: last_facts 的那一项}，或取它的函数


# --------------------------------------------------------------------------
# 台账条目
# --------------------------------------------------------------------------


def ledger_entry(result: AnalysisResult, *, node_id: str, exec_no: int, artifact: str,
                 call_id: str | None = None, item: str | None = None) -> dict[str, Any]:
    """证据台账里的一条分析结论（engine/evidence.EvidenceEntry 的 kind analysis）。agent 工具带 call_id，分析卡带 item
    （那一项的 id）；inputs 是输入快照的 sha256（去重、保序），证据图据此画「派生自」的边，写作目录据此写「来自 Q3」。"""
    entry: dict[str, Any] = {
        "kind": LEDGER_KIND,
        "node_id": node_id,
        "exec": exec_no,
        "artifact": artifact,
        "op": result.get("op"),
        "mode": result.get("mode"),
        "status": result.get("status"),
        "verdict": (result.get("verdict") or {}).get("code"),
        "inputs": list(dict.fromkeys(i["artifact"] for i in result.get("inputs") or [] if i.get("artifact"))),
    }
    if call_id is not None:
        entry["call_id"] = call_id
    if item is not None:
        entry["item"] = item
    return entry


# --------------------------------------------------------------------------
# 跨包的约定：写作目录里的分析条目、口径卡指标上的可比性、报告文档里系统写的「分析范围」
# --------------------------------------------------------------------------


def catalog_result(entry: Mapping[str, Any] | None) -> AnalysisResult | None:
    """写作目录（engine/evidence.build_catalog）里一条分析条目的结论。约定：编号 A{n} 的条目 kind 为 LEDGER_KIND，
    result 是从工件取回的整份结论（证据接入负责装上，说法核对、分析范围只经这个函数读）。"""
    if not entry or entry.get("kind") != LEDGER_KIND:
        return None
    result = entry.get("result")
    return result if isinstance(result, dict) else None


class MetricComparability(TypedDict, total=False):
    """口径卡指标上的可比性（00-总方案 §3.4 第 3 条）：指标的表达式用到两份同形、只差字面量的查询快照时，口径卡执行时
    自动做一次可比性检查，记在指标条目的 comparability 字段。写作目录照实提醒，带契约的出口按 verdict 记缺口或标注。"""

    artifact: str             # 那份可比性结论的工件 sha256
    verdict: str              # comparable / conditional / incomparable
    phrase: str
    reasons: list[str]        # fail、warn 的原因，一句一个


class ScopeItem(TypedDict, total=False):
    alias: str                # 写作目录编号 A{n}
    op: str
    mode: str | None
    dims: list[str]           # 拆解的维度（给人看的叫法）
    verdict: str | None
    phrase: str
    parent: str | None        # 下钻：上一层的编号
    reason: str | None        # 算不了的原因


class ScopeBlock(TypedDict, total=False):
    """报告文档的 analysis_scope 字段：系统写的「分析范围」（00-总方案 §3.6）。结构化数据随文档封存，不进正文、不过
    裸数字和说法核对（不是模型写的）；网页、导出、可验证报告各自按它渲染，编号点得开。"""

    phenomena: list[ScopeItem]        # 可比性、基线、趋势
    decomposed: list[ScopeItem]       # 拆过的（按下钻链的先后）
    excluded: list[ScopeItem]         # 判定为分散、差异不足以下结论的
    refused: list[ScopeItem]          # 算不了的
    not_decomposed: list[str] | None  # 候选维度里没拆过的叫法；None = 候选维度核对不了
    note: str | None                  # 核对不了时的一句说明
    tests: int                        # 这次做了几次差异检验
    corrected: bool | None            # 检验校正过没有：只做了一次为 True（一次里比了多对的已按次数校正），不止一次为
                                      # False（几次之间没有校正），没做为 None


#: 复算核对时不比的字段：hypothesis 是 agent 写的话，不是算出来的
NOT_RECOMPUTED = frozenset({"hypothesis"})


def same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """两份结论（封存的、复算的）是否逐字段相同（hypothesis 除外）。"""
    keys = (set(a) | set(b)) - NOT_RECOMPUTED
    return all(a.get(k) == b.get(k) for k in keys)


__all__ = [
    "ARTIFACT_KIND", "CATALOG_PREFIX", "CAVEATS", "DEFAULTS", "LABELS", "LEDGER_KIND", "MARKER_KIND", "METHODS", "MODES",
    "MODE_LABELS", "NODE_TYPE", "NOT_RECOMPUTED", "OPS", "OP_LABELS", "RANGES", "REASONS", "SETTINGS_KEY", "TOOL_NAMES",
    "VERDICTS", "AnalysisInput", "AnalysisRequest", "AnalysisResult", "CallError", "Caveat", "Context", "InputSnapshot",
    "MetricComparability", "Probe", "Prober", "ResultTable", "ScopeBlock", "ScopeItem", "Verdict", "catalog_result",
    "effective_settings", "is_result", "ledger_entry", "method_label", "same", "settings_problem",
]
