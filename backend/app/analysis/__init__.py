"""确定性分析算子（docs/design/analysis-round/）：变化拆解、波动基线、趋势、差异检验、可比性检查。

入口只有两个函数，agent 分析工具（tools/builtin）和分析卡（engine/nodes/analysis.py）都只经它们调用：

- run：按请求算一份分析结论。resolve 按调用里写的名字（Q3、来源节点 id）取回输入快照；单元格写成「名字.r行.列」
  （Q1.r0.gmv），由这里拆开再经 resolve 取快照。parents 按名字（A3、分析卡里的项 id）取回上一层拆解，下钻用。
  调用写错抛 contract.CallError；算不了返回 status refused 的结论。唯一的异步来自 context["probe"]（现查表的日期范围）。
  refuse_data_errors 为真时（分析卡：调用是模板定死的，这期的数据里没有要用的那一项不是写错了），随数据变化的调用错误
  （DATA_ERRORS）也记成算不了的结论：上一层没做成 parent_refused，其余 input_unavailable，原因照实写是哪一项、哪个值；
  结构性的（列不存在、遮罩列、比率当可加、重复行、参数写错）照旧抛。agent 工具不传，调用错误交回模型改调用。
- recompute：按结论里记下的 params、settings、probes 和输入快照（按 sha256 取回）重算一遍，不现查；和封存的用
  contract.same 比。一律按 refuse_data_errors 为真复现：分析卡记下的这类结论复算也成立。

契约（名字、形状、缺省阈值）在 contract.py。各算子在 decompose.py（变化拆解）、series.py（波动基线、趋势）、differ.py
（差异检验）、comparable.py（可比性检查）；统计在 stats.py，判定语和数的写法在 phrases.py，现查在 probe.py。

**一份结论只由 inputs（快照里的格）、probes、params、settings、method 决定**：run 时从 context 读到的东西（运行当天、目录
里的度量类型和叫法、现查的范围）都记进结论——运行当天、目录事实在 params（today、catalog），现查结果在 probes——复算只读
记下的。同样的输入两次 run 逐字节相同（现查结果相同时）；recompute 和 run 逐字段相同。

**参数**（AnalysisRequest.params；列名不分大小写，按快照认）见各算子文件头；params 里的 catalog、today、depth 由系统填，
调用方写了也不收。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from app.analysis.contract import (MODES, OP_LABELS, OPS, AnalysisRequest, AnalysisResult, CallError, Context,
                                   InputSnapshot, effective_settings)

#: 按调用里写的名字取回输入快照；没有这个名字返回 None
Resolver = Callable[[str], InputSnapshot | None]
#: 按名字取回上一层的分析结论（下钻）；没有返回 None
ParentResolver = Callable[[str], AnalysisResult | None]

#: 随数据变化的调用错误（CallError.code）：调用本身没写错，是这期的数据里没有要用的那一项。分析卡里（refuse_data_errors）
#: 记成算不了的结论，不让卡失败：上一层没做成（parent_refused）；下钻的段这期没有（parent_key_missing）；期次这期没有
#: （period_not_found：变化拆解、波动基线的本期、差异检验按期比、可比性检查）；按位置取的总数那一格越界或为空
#: （row_out_of_range、headline_empty）；要比的组这期没有（unknown_group、too_few_groups）；要用的那一格这期为空
#: （empty_cell：乘积拆解的乘积、因子，差异检验的计数）
DATA_ERRORS = frozenset({"parent_refused", "parent_key_missing", "period_not_found", "row_out_of_range",
                         "headline_empty", "unknown_group", "too_few_groups", "empty_cell"})


def _op_mode(request: Mapping[str, Any]) -> tuple[str, str | None]:
    op = request.get("op")
    if op not in OPS:
        raise CallError("unknown_op", f"不认识的分析：{op}。能做的有{'、'.join(OP_LABELS[o] for o in OPS)}")
    modes = MODES.get(op)
    mode = request.get("mode")
    if modes is None:
        return op, None
    if mode not in modes:
        raise CallError("unknown_mode", f"{OP_LABELS[op]}要写模式：{' / '.join(modes)}")
    return op, mode


def _compute(op: str, mode: str | None, raw: Mapping[str, Any], env: Any) -> AnalysisResult:
    from app.analysis import comparable, decompose, differ, series

    if op == "decompose":
        modes = {"additive": decompose.additive, "ratio": decompose.ratio, "factor": decompose.factor}
        return modes[mode](raw, env)  # type: ignore[index]
    if op == "baseline":
        return series.baseline(raw, env)
    if op == "trend":
        return series.trend(raw, env)
    if op == "differ":
        return differ.differ(raw, env)
    return comparable.comparable(raw, env)


def _env(op: str, mode: str | None, settings: Mapping[str, Any], inputs: Any, facts: Any, book: Any,
         today: date | None, parents: ParentResolver | None, hypothesis: Any) -> Any:
    from app.analysis.common import Env

    return Env(op=op, mode=mode, settings=settings, inputs=inputs, facts=facts, book=book, today=today,
               parents=parents, hypothesis=hypothesis if isinstance(hypothesis, str) else None)


def _data_refusal(op: str, mode: str | None, raw: Mapping[str, Any], env: Any, error: CallError) -> AnalysisResult:
    """随数据变化的调用错误记成的算不了的结论。params 记调用原样（label 照样消毒；调用还没走完、规范化不完整，复算拿它重走一遍
    走到同一处），取过的快照都记进输入（Inputs.settle），阈值和判定标准同这个算子自己算不了时那一份。"""
    from app.analysis import decompose, differ, series
    from app.analysis import phrases as ph
    from app.analysis.common import build

    env.inputs.settle()
    params = dict(raw)
    if params.get("label") is not None:
        params["label"] = ph.clean_label(params["label"]) or None
    if op == "decompose":
        used, criterion = decompose.refusal_basis(mode, raw, env.settings)  # type: ignore[arg-type]
    elif op in ("baseline", "trend"):
        used, criterion = series.refusal_basis(op, env.settings)
    elif op == "differ":
        used, criterion = differ.refusal_basis(mode, env.settings)
    else:
        used, criterion = [], ph.CRITERION_COMPARABLE
    code = "parent_refused" if error.code == "parent_refused" else "input_unavailable"
    reason = error.reason or error.message
    return build(env, params, used, status="refused", reason_code=code, reason=reason, criterion=criterion,
                 phrase=ph.refused(op, reason))


def _compute_or_refuse(op: str, mode: str | None, raw: Mapping[str, Any], env: Any, refuse_data_errors: bool
                       ) -> AnalysisResult:
    try:
        return _compute(op, mode, raw, env)
    except CallError as e:
        if refuse_data_errors and e.code in DATA_ERRORS:
            return _data_refusal(op, mode, raw, env, e)
        raise


async def run(request: AnalysisRequest, resolve: Resolver, *, settings: Mapping[str, Any], context: Context,
              parents: ParentResolver | None = None, refuse_data_errors: bool = False) -> AnalysisResult:
    """算一份分析结论。settings 是 contract.effective_settings 算好的阈值。refuse_data_errors 见文件头（分析卡传真）。"""
    from app.analysis.common import DERIVED_PARAMS
    from app.analysis.facts import Facts
    from app.analysis.inputs import Inputs
    from app.analysis.probe import MAX_ROUNDS, Book, fetch

    op, mode = _op_mode(request)
    raw = {k: v for k, v in (request.get("params") or {}).items() if k not in DERIVED_PARAMS}
    full = effective_settings(settings)
    prober = context.get("probe")
    today = context.get("today") or date.today()
    if isinstance(today, datetime):
        today = today.date()

    def dialect(name: str, snap: Mapping[str, Any]) -> str | None:
        given = snap.get("dialect")
        if isinstance(given, str) and given:
            return given
        lookup = getattr(prober, "dialect", None)
        return lookup(str(snap.get("source") or "")) if lookup is not None else None

    known: dict[tuple[str, ...], Any] = {}
    for rounds in range(MAX_ROUNDS + 1):
        book = Book(known)
        env = _env(op, mode, full, Inputs(resolve, dialect), Facts(catalogs=context.get("catalogs")), book, today,
                   parents, request.get("hypothesis"))
        result = _compute_or_refuse(op, mode, raw, env, refuse_data_errors)
        missing = book.missing
        if not missing or rounds == MAX_ROUNDS:
            return result
        known.update(await fetch(book, context))
    return result  # pragma: no cover - 循环里一定返回


def recompute(result: AnalysisResult, load: Callable[[str], InputSnapshot | None], *,
              load_result: Callable[[str], AnalysisResult | None] | None = None) -> AnalysisResult:
    """复算：load 按 sha256 取回输入快照，load_result 按 sha256 取回 parent 那份结论。用结论里记下的 params（含运行当天、
    目录事实）、settings、probes，不现查、不读目录。取不回输入时抛 CallError（code input_missing）；方法升版后调用不再成立
    时同样抛 CallError（照 run 的说法）。随数据变化的调用错误一律记成算不了（同 run 的 refuse_data_errors 为真），分析卡记下
    的这类结论复算也成立。和封存的那份用 contract.same 比。"""
    from app.analysis.common import DERIVED_PARAMS
    from app.analysis.facts import Facts
    from app.analysis.inputs import _CELL, Inputs
    from app.analysis.probe import Book

    op, mode = _op_mode(result)
    params = dict(result.get("params") or {})
    settings = effective_settings(result.get("settings"))
    names: dict[str, tuple[str | None, str | None, list[str]]] = {}
    parents_at: dict[str, str] = {}
    for item in result.get("inputs") or []:
        ref = str(item.get("ref") or "")
        if item.get("role") == "parent":
            parents_at[ref] = str(item.get("artifact") or "")
            continue
        cell = _CELL.match(ref) if "row" in item else None
        name = cell.group("name") if cell else ref
        names.setdefault(name, (item.get("artifact"), item.get("dialect"), list(item.get("masked") or [])))

    def resolve(name: str) -> InputSnapshot | None:
        artifact, _, masked = names.get(name, (None, None, []))
        found = load(artifact) if artifact else None
        if not isinstance(found, Mapping):
            raise CallError("input_missing", f"复算取不回 {name} 的查询快照")
        return {**found, "artifact": artifact, "masked": masked}  # type: ignore[typeddict-item]

    def dialect(name: str, snap: Mapping[str, Any]) -> str | None:
        return names.get(name, (None, None, []))[1]

    def parents(name: str) -> AnalysisResult | None:
        artifact = parents_at.get(name)
        found = load_result(artifact) if artifact and load_result is not None else None
        if not isinstance(found, Mapping):
            raise CallError("input_missing", f"复算取不回上一层的分析结论 {name}")
        return found  # type: ignore[return-value]

    today = date.fromisoformat(params["today"]) if isinstance(params.get("today"), str) else None
    env = _env(op, mode, settings, Inputs(resolve, dialect), Facts(recorded=params.get("catalog") or {}),
               Book(result.get("probes") or []), today, parents, result.get("hypothesis"))
    raw = {k: v for k, v in params.items() if k not in DERIVED_PARAMS}
    return _compute_or_refuse(op, mode, raw, env, True)


def exhausted(settings: Mapping[str, Any]) -> CallError:
    """分析次数用完（max_calls）的调用错误：上限按每个 Agent 每次执行计，入口（本节点的分析容器）数着次数，到了上限交回它，
    不再算。"""
    limit = int(effective_settings(settings)["max_calls"])
    return CallError("max_calls", f"这个 Agent 这次执行最多做 {limit} 次分析，已经用完：用已有的分析结论写，不再新做分析")


def make_prober(*args: Any, **kwargs: Any) -> Any:
    """见 app/analysis/probe.make_prober。"""
    from app.analysis.probe import make_prober as make

    return make(*args, **kwargs)


def comparable_reasons(result: Mapping[str, Any]) -> list[str]:
    """可比性结论里 fail、warn 的原因，一句一个（口径卡指标的 comparability.reasons）。"""
    from app.analysis.comparable import reasons

    return reasons(result)


__all__ = ["DATA_ERRORS", "ParentResolver", "Resolver", "comparable_reasons", "exhausted", "make_prober", "recompute",
           "run"]
