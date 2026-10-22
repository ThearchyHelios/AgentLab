"""变化拆解（细案 01 §2）：变化从哪来。三种模式，agent 按指标的性质选，算子核对选得对不对。

**参数**（AnalysisRequest.params；列名不分大小写，按快照认）：

- 一份快照：query（编号）、period（期次列）、base / target（期次列里的两个值），本期齐不齐同波动基线那一套（comparable.
  period_complete：不到整期、没过完、数据没到 → partial_target；核对不了的附说明，只在证据面板）；或两份快照：base_query、
  target_query（各自只有维度列和度量列，先做可比性检查：口径结构不同的不拆，算不了 incomparable_sides；其余不可比的照样算，
  判定后附上可比性结论）。
- additive：by（维度列，1–2 列）、measure（度量列）；可选 headline {base, target}（基期、本期总数各一格，如 Q1.r1.gmv，用来
  核对闭合）、parent {of: A3, key: [华东]}（下钻：这一层各段之和要等于 A3 里华东那一段）。
- ratio：by、numerator（分子列）、denominator（分母列）；可选 headline {base, target}（整体比率的两格）、parent（上一层的比率
  拆解）。
- factor：product（乘积列）、factors（因子列，2–4 个）；一份快照按 period / base / target 取两行，两份快照各一行。
- 都可以写 label（度量的叫法，消毒后进判定语）。

**计算**全程 Fraction（快照里的数按字面换成精确的有理数）：各段之和逐位等于总变化；比率拆解的结构效应、自身效应按中点
对称拆，Σmix + Σrate 逐位等于比率的变化；乘积拆解是 LMDI-I（对数），浮点，各因子贡献之和在浮点误差内等于总变化。判定、
阈值、边界照细案 §2.1–§2.4。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app.analysis import comparable as cmp
from app.analysis import phrases as ph
from app.analysis.common import (Env, build, closes, column_label, column_types, label_for, need, refuse,
                                 reserved_check, sign_word, today_param)
from app.analysis.contract import CONV_PCT, CONV_POINTS, CallError
from app.analysis.inputs import Snap, as_list, hashable, same_value
from app.analysis.numbers import canon, to_fraction

#: 辛普森：分母够大的段里，按平均权重算有这么多的段比率和整体反着变
SIMPSON_SHARE = Fraction(4, 5)
#: 下钻、总数的写法（调用错误里教 agent 怎么写）
PARENT_SHAPE = "下钻要写成 {of: A3, key: [华东]} 这样：of 是上一层变化拆解的编号，key 是那一段的维度值"
HEADLINE_SHAPE = "总数要写成 {base: Q1.r1.gmv, target: Q1.r0.gmv} 这样：base 是基期总数那一格，target 是本期总数那一格"
SAME_QUERY = "基期和本期是同一份查询：两期各给一份查询，或者给一份按期次分组的查询和期次列"
#: 乘积闭合的最小相对误差
_FACTOR_REL = Fraction(1, 10 ** 6)
_NOT_ADDITIVE = {"ratio": "比率", "identifier": "标识", "status": "状态", "attribute": "属性"}
#: 各模式的结论里记的阈值（下钻时另加 max_drill_depth）
USED: dict[str, tuple[str, ...]] = {
    "additive": ("main_share", "concentration", "offset_ratio", "flat_pct", "table_top_k"),
    "ratio": ("main_share", "flat_pct", "min_denominator", "table_top_k"),
    "factor": ("main_share", "flat_pct"),
}


def refusal_basis(mode: str, raw: Mapping[str, Any], settings: Mapping[str, Any]) -> tuple[list[str], str]:
    """还没算就算不了时（分析卡把随数据变化的调用错误记成算不了的结论）结论里记的阈值和判定标准：同这个模式自己算不了时
    的那一份。"""
    used = list(USED[mode]) + (["max_drill_depth"] if mode != "factor" and raw.get("parent") is not None else [])
    if mode == "additive":
        summed = raw.get("headline") is None and raw.get("parent") is None
        return used, ph.criterion_additive(settings, top_k_max=None, summed=summed)
    return used, ph.criterion_ratio(settings) if mode == "ratio" else ph.criterion_factor(settings)


@dataclass
class Side:
    """一期的各段：键 → 值（ratio 模式是 (分子, 分母)）；nulls 是这一期度量格为空的段；rows 是用到的行。"""

    snap: Snap
    values: dict[tuple[Any, ...], Any] = field(default_factory=dict)
    nulls: list[tuple[Any, ...]] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)


@dataclass
class Segment:
    key: tuple[Any, ...]
    base: Any
    target: Any
    flags: list[str] = field(default_factory=list)


def _key_strings(key: Sequence[Any]) -> list[str]:
    return [ph.key_text([k]) for k in key]


def _sides(raw: Mapping[str, Any], env: Env, roles: Sequence[tuple[str, str]], *, by_needed: bool = True,
           reserved: Sequence[str] = ()) -> tuple[dict[str, Any], Snap, Snap, dict[str, str], list[str]]:
    """一份或两份快照：(规范化的参数, 基期快照, 本期快照, {角色: 列名}, 维度列)。roles 是 [(参数名, 角色的叫法)]；reserved 是
    结果表固定的列名，维度列不能叫这些。"""
    params: dict[str, Any] = {}
    two = raw.get("base_query") is not None or raw.get("target_query") is not None
    if two:
        base = env.inputs.snap(need(raw, "base_query", "基期的查询编号"), "基期的查询编号", role="base")
        target = env.inputs.snap(need(raw, "target_query", "本期的查询编号"), "本期的查询编号",
                                 role="target")
        if base is target:
            raise CallError("same_query", SAME_QUERY)
        params.update(base_query=base.ref, target_query=target.ref)
    else:
        base = target = env.inputs.snap(need(raw, "query", "查询编号"), "查询编号")
        params["query"] = base.ref
    by: list[str] = []
    if by_needed:
        wanted = as_list(raw.get("by"))
        if not 1 <= len(wanted) <= 2:
            raise CallError("bad_by", "维度列要写 1 到 2 列")
        by = [target.column(c, "维度列") for c in wanted]
        if two:
            for c in by:
                base.column(c, "维度列")
        for c in by:
            reserved_check(c, reserved, "维度列")
        params["by"] = by
    cols: dict[str, str] = {}
    for key, what in roles:
        if key == "factors":
            continue
        col = target.column(need(raw, key, what), what)
        if two:
            base.column(col, what)
        cols[key] = col
        params[key] = col
    if not two:
        params["period"] = target.column(need(raw, "period", "期次列"), "期次列")
        params["base"] = need(raw, "base", "基期（期次列里的值）")
        params["target"] = need(raw, "target", "本期（期次列里的值）")
        if same_value(params["base"], params["target"]):
            raise CallError("same_period", "基期和本期是同一期：写期次列里两个不同的值")
    used = [*cols.values(), *([params["period"]] if "period" in params else [])]
    if len(set(by)) != len(by) or set(by) & set(used) or len(set(used)) != len(used):
        raise CallError("bad_by", "维度列、期次列、度量列要是不同的列")
    return params, base, target, cols, by


def _period_rows(snap: Snap, period: str, wanted: Any, what: str) -> list[int]:
    i = snap.index(period)
    rows = [r for r, row in enumerate(snap.rows) if i < len(row) and same_value(wanted, row[i])]
    if not rows:
        values = list(dict.fromkeys(ph.key_text([row[i]]) for row in snap.rows if i < len(row)))
        raise CallError("period_not_found", f"期次列「{period}」里没有「{wanted}」：{what}要写 {snap.ref} 里有的期次值，"
                                            f"比如 {'、'.join(values[:6])}",
                        reason=f"期次列「{period}」里没有{what}「{ph.key_text([wanted])}」")
    return rows


def _rows_of(raw: Mapping[str, Any], params: dict[str, Any], base: Snap, target: Snap) -> tuple[list[int], list[int]]:
    if "period" in params:
        return (_period_rows(base, params["period"], params["base"], "基期"),
                _period_rows(target, params["period"], params["target"], "本期"))
    return list(range(len(base.rows))), list(range(len(target.rows)))


def _collect(snap: Snap, rows: Sequence[int], by: Sequence[str], read: Any, labels: Sequence[str],
             period_label: str | None) -> Side:
    """一期的各段：按维度键，同一键只许一行（多行是 SQL 没按这些维度汇总）。read(row) 读出这一行的值（None 是空）。"""
    side = Side(snap)
    seen: set[tuple[Any, ...]] = set()
    idx = [snap.index(c) for c in by]
    for r in rows:
        row = snap.rows[r]
        key = tuple(hashable(row[i] if i < len(row) else None) for i in idx)
        if key in seen:
            keys = "、".join([*labels, *([period_label] if period_label else [])])
            raise CallError("duplicate_rows", f"{snap.ref} 里同一个{'、'.join(labels)}在同一期有不止一行：在 SQL 里按 "
                                              f"{keys} 汇总，每段每期一行")
        seen.add(key)
        value = read(r)
        side.rows.append(r)
        if value is None:
            side.nulls.append(key)
        side.values[key] = value
    return side


def _segments(base: Side, target: Side, zero: Any) -> list[Segment]:
    """两期的各段，按先出现的顺序：缺的一边记 0，标「新出现 / 消失」；度量格为空的记 0，标「为空」。"""
    out: list[Segment] = []
    for key in dict.fromkeys([*base.values, *target.values]):
        flags = []
        if key not in base.values:
            flags.append("新出现")
        if key not in target.values:
            flags.append("消失")
        if key in base.nulls or key in target.nulls:
            flags.append("为空")
        b, t = base.values.get(key), target.values.get(key)
        out.append(Segment(key, zero if b is None else b, zero if t is None else t, flags))
    return out


def _parent(raw: Mapping[str, Any], env: Env, mode: str, dims: int) -> tuple[dict[str, Any], list[Any], int]:
    """下钻：上一层的结论和那一段。返回 (params 里的 parent, 那一段在上一层结果表里的那一行, 这一层的层数)。"""
    from app.core.artifact_store import canonical_json, content_hash

    spec = raw.get("parent")
    name = spec.get("of") if isinstance(spec, Mapping) else None
    key = as_list(spec.get("key")) if isinstance(spec, Mapping) else []
    if not isinstance(name, str) or not name.strip() or not key:
        raise CallError("bad_parent", PARENT_SHAPE)
    name = name.strip()
    found = env.parents(name) if env.parents is not None else None
    if not isinstance(found, Mapping):
        raise CallError("parent_missing", f"没有叫「{name}」的分析结论：下钻写这次运行里变化拆解的编号（如 A3）")
    if found.get("op") != "decompose" or found.get("mode") != mode:
        raise CallError("parent_not_decompose", f"{name} 不是{'按维度' if mode == 'additive' else '比率'}的变化拆解，"
                                                "下钻要接在同一种拆解后面")
    # 上一层先记进输入：它没做成、这期没有那一段时（分析卡记成算不了的结论），复算也要按内容哈希取回它、走到同一处
    env.inputs.parent(name, content_hash(canonical_json(found)))
    if found.get("status") != "ok" or not isinstance(found.get("table"), Mapping):
        why = f"（{found['reason']}）" if isinstance(found.get("reason"), str) and found["reason"] else ""
        raise CallError("parent_refused", f"{name} 没有做成，不能在它下面下钻",
                        reason=f"上一层「{name}」没有做成{why}，不能在它下面下钻")
    depth = int((found.get("params") or {}).get("depth") or 0) + 1
    limit = int(env.settings["max_drill_depth"])
    if depth > limit:
        raise CallError("too_deep", f"已经下钻了 {depth - 1} 层，最多 {limit} 层：换一个维度从头拆")
    table = found["table"]
    fixed = _FIXED_ADDITIVE if mode == "additive" else _FIXED_RATIO
    columns = list(table.get("columns") or [])
    dim_count = len([c for c in columns if c not in fixed])
    if len(key) != dim_count:
        raise CallError("bad_parent", f"{name} 按 {dim_count} 列拆，下钻的那一段要写 {dim_count} 个维度值")
    hit = next((row for row in table.get("rows") or []
                if all(same_value(k, v) for k, v in zip(key, row[:dim_count]))), None)
    if hit is None:
        raise CallError("parent_key_missing", f"{name} 里没有「{ph.key_text(key)}」这一段：写 {name} 结果表里的段",
                        reason=f"上一层「{name}」里没有「{ph.key_text(key)}」这一段")
    return {"of": name, "key": key}, dict(zip(columns, hit)), depth


#: 结果表里以比率存的列（占比、变化率、权重、比率本身）：引用、展开时按百分数显示（ResultTable.formats 记 pct）
_RATE_COLUMNS = ("pct", "share")


def _rate_formats(columns: Sequence[str], rate: Sequence[str] = _RATE_COLUMNS,
                  points: Sequence[str] = ()) -> dict[str, str]:
    """结果表各列记的换算：rate 里的列（占比、变化率、权重）记 pct，points 里的列（比率的变化）记 pp。
    比率本身（如客单价）不是占比，不进 rate——套上百分数会显示成 16,062.5%。"""
    return {**{c: CONV_PCT for c in rate if c in columns},
            **{c: CONV_POINTS for c in points if c in columns}}


_FIXED_ADDITIVE = ("base", "target", "delta", "pct", "share", "flag")
_FIXED_RATIO = ("n_base", "n_target", "d_base", "d_target", "w_base", "w_target", "r_base", "r_target", "mix", "rate",
                "total", "flag")


def _headline(raw: Mapping[str, Any], env: Env) -> tuple[dict[str, str] | None, dict[str, tuple[Fraction, Any]]]:
    """headline：基期、本期总数各一格 → ({base, target} 的写法, {base / target: (值, 原样)})。"""
    spec = raw.get("headline")
    if spec is None:
        return None, {}
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        spec = {"base": spec[0], "target": spec[1]}
    if not isinstance(spec, Mapping) or not spec.get("base") or not spec.get("target"):
        raise CallError("bad_headline", HEADLINE_SHAPE)
    out: dict[str, tuple[Fraction, Any]] = {}
    refs: dict[str, str] = {}
    for side, what in (("base", "基期总数"), ("target", "本期总数")):
        snap, row, col, value = env.inputs.cell(spec[side], f"headline_{side}", what)
        if value is None:
            raise CallError("headline_empty", f"{what}那一格（{snap.ref}.r{row}.{col}）是空的：换一格有值的",
                            reason=f"{what}那一格（{snap.ref}.r{row}.{col}）是空的")
        out[side] = (value, snap.cell(row, col))
        refs[side] = f"{snap.ref}.r{row}.{col}"
    return refs, out


def _measure_guard(env: Env, snap: Snap, column: str, caveats: list[dict[str, str]], tail: str) -> None:
    """additive 的度量列：目录标成（确认过的）比率、标识、状态、属性是调用错误；没标的、只是推断的照算，写附带说明。"""
    kind, confirmed = env.facts.measure(snap, column)
    if kind in _NOT_ADDITIVE and confirmed:
        if kind == "ratio":
            raise CallError("ratio_as_additive", f"「{column}」是比率（或平均值），不能按段相加：改用比率拆解，分子、分母各给一列")
        raise CallError("not_a_measure", f"「{column}」在目录里是{_NOT_ADDITIVE[kind]}，不是能相加的量：度量列要选金额、数量这类列")
    if kind is None:
        caveats.append(ph.caveat("measure_unlabeled", ph.unlabeled_text(column, tail)))
    elif kind in _NOT_ADDITIVE:
        caveats.append(ph.caveat("measure_unlabeled", ph.unconfirmed_text(column, kind, tail)))


def _comparability(env: Env, params: dict[str, Any], base: Snap, target: Snap, measure: str | None
                   ) -> tuple[cmp.Outcome | None, str | None]:
    """两份快照时先做可比性：(结论, 口径结构不同的原因)。"""
    if "base_query" not in params:
        return None, None
    params["today"] = today_param(env)
    outcome = cmp.compare_queries(base, target, measure, env.facts, env.book, env.today)  # type: ignore[arg-type]
    return outcome, outcome.structure


def _period_complete(env: Env, params: dict[str, Any], snap: Snap, measure: str | None
                     ) -> tuple[str | None, dict[str, str] | None]:
    """一份快照按期次分组时本期齐不齐（同波动基线那一套，comparable.period_complete；期次按 periods.parse_compared 认）：
    (不齐时 partial_target 的原因, 核对不了时的附带说明)。两份快照的在可比性检查里核过整期，不在这里核。"""
    from app.analysis import periods as pd

    if "base_query" in params:
        return None, None
    params["today"] = today_param(env)
    period = params["period"]
    i = snap.index(period)
    values = [row[i] if i < len(row) else None for row in snap.rows]
    parsed = pd.parse_compared(values, snap.sql, period, snap.dialect)
    if parsed is None:
        return None, ph.caveat("comparability", ph.NO_DATES)
    items, grain = parsed
    target = next(item for item, v in zip(items, values) if same_value(params["target"], v))
    status, why, note = cmp.period_complete(snap, period, target, grain, measure, env.facts, env.book,
                                            env.today)  # type: ignore[arg-type]
    if status == "short":
        return f"{why}，和完整的{pd.PER_WORD[grain]}比没有意义", None
    return None, ph.caveat("comparability", note) if status == "unknown" and note else None


def _share_rank(segments: Sequence[Any], share: Any) -> list[Any]:
    """按 share 从大到小（一样大的按先出现的）。"""
    return sorted(segments, key=lambda s: -share(s))


# --------------------------------------------------------------------------
# additive
# --------------------------------------------------------------------------


def additive(raw: Mapping[str, Any], env: Env) -> Any:
    used = list(USED["additive"])
    params, bs, ts, cols, by = _sides(raw, env, [("measure", "度量列")], reserved=_FIXED_ADDITIVE)
    measure = cols["measure"]
    label = label_for(env, raw.get("label"), ts, measure)
    if raw.get("label") is not None:
        params["label"] = ph.clean_label(raw.get("label")) or None
    dims = [column_label(env, ts, c) for c in by]
    caveats: list[dict[str, str]] = []
    _measure_guard(env, ts, measure, caveats, "按可以按段相加处理")
    refs, heads = _headline(raw, env)
    if refs:
        params["headline"] = refs
    parent_row = None
    if raw.get("parent") is not None:
        params["parent"], parent_row, params["depth"] = _parent(raw, env, "additive", len(by))
        used.append("max_drill_depth")
    summed = not heads and parent_row is None
    criterion_args = {"summed": summed}
    for snap in dict.fromkeys([bs, ts]):
        if snap.truncated:
            env.inputs.use(snap, "data" if bs is ts else ("base" if snap is bs else "target"), [*by, measure])
            return refuse(env, params, used, "truncated",
                          f"{snap.ref} 只取回了前 {len(snap.rows)} 行，各段之和不是完整的和",
                          criterion=ph.criterion_additive(env.settings, top_k_max=None, **criterion_args))
    rows_b, rows_t = _rows_of(raw, params, bs, ts)
    period_label = column_label(env, ts, params["period"]) if "period" in params else None
    side_b = _collect(bs, rows_b, by, lambda r: bs.number(r, measure, "度量列"), dims, period_label)
    side_t = _collect(ts, rows_t, by, lambda r: ts.number(r, measure, "度量列"), dims, period_label)
    if bs is ts:
        env.inputs.use(ts, "data", [*by, params["period"], measure], side_b.rows + side_t.rows)
    else:
        env.inputs.use(bs, "base", [*by, measure], side_b.rows)
        env.inputs.use(ts, "target", [*by, measure], side_t.rows)
    outcome, structure = _comparability(env, params, bs, ts, measure)
    if structure is not None:
        return refuse(env, params, used, "incomparable_sides", f"{structure}，不是同一个量的两期，不拆",
                      criterion=ph.criterion_additive(env.settings, top_k_max=None, **criterion_args), caveats=caveats)
    partial, note = _period_complete(env, params, ts, measure)
    if partial is not None:
        return refuse(env, params, used, "partial_target", partial,
                      criterion=ph.criterion_additive(env.settings, top_k_max=None, **criterion_args), caveats=caveats)
    if note is not None:
        caveats.append(note)
    segs = _segments(side_b, side_t, Fraction(0))
    if len(segs) < 2:
        return refuse(env, params, used, "too_few_segments",
                      ph.say("按", ph.dims_text(dims), f"只有 {len(segs)} 段，至少要 2 段才拆得开"),
                      criterion=ph.criterion_additive(env.settings, top_k_max=None, **criterion_args), caveats=caveats)
    return _additive_result(env, params, used, segs, by, dims, label, heads, parent_row, outcome, caveats, summed,
                            side_b, side_t)


def _additive_result(env: Env, params: dict[str, Any], used: list[str], segs: list[Segment], by: list[str],
                     dims: list[str], label: str, heads: Mapping[str, tuple[Fraction, Any]],
                     parent_row: Mapping[str, Any] | None, outcome: cmp.Outcome | None,
                     caveats: list[dict[str, str]], summed: bool, side_b: Side, side_t: Side) -> Any:
    s = env.settings
    main_share, concentration = env.threshold("main_share"), env.threshold("concentration")
    offset_ratio, flat_pct = env.threshold("offset_ratio"), env.threshold("flat_pct")
    B = sum((x.base for x in segs), Fraction(0))
    T = sum((x.target for x in segs), Fraction(0))
    delta = T - B
    gaps: dict[str, Fraction] = {}
    for side, total in (("base", B), ("target", T)):
        if side in heads and not closes(total, heads[side][0], heads[side][1]):
            gaps[side] = heads[side][0] - total
        if parent_row is not None:
            expect = to_fraction(parent_row.get(side))
            if expect is None or not closes(total, expect, None, exact=True):
                gaps.setdefault(side, (expect or Fraction(0)) - total)
    closed = not gaps
    deltas = {x.key: x.target - x.base for x in segs}
    shares = {k: d / delta for k, d in deltas.items()} if closed and delta != 0 else {}
    gross_up = sum((d for d in deltas.values() if d > 0), Fraction(0))
    gross_down = sum((d for d in deltas.values() if d < 0), Fraction(0))
    opposite = abs(gross_down) if delta > 0 else gross_up if delta < 0 else gross_up
    n = len(segs)
    k_max = max(2, math.ceil(n / 5))
    flat = delta == 0 or (B != 0 and abs(delta) / abs(B) < flat_pct)
    ranked = _share_rank(segs, lambda x: shares[x.key]) if shares else []
    top_k = top_k_share = None
    if ranked:
        running = Fraction(0)
        for k, seg in enumerate(ranked, start=1):
            running += shares[seg.key]
            if running >= concentration:
                top_k, top_k_share = k, running
                break
    if not closed:
        code, subjects = "unclosed", []
    elif flat:
        code, subjects = "flat", []
    elif shares[ranked[0].key] >= main_share:
        code, subjects = "driver", [ranked[0]]
    elif top_k is not None and 2 <= top_k <= k_max:
        code, subjects = "concentrated", ranked[:top_k]
    else:
        code, subjects = "dispersed", []
    offset = opposite > 0 and opposite >= offset_ratio * abs(delta)
    if offset:
        caveats.append(ph.caveat("offset", ph.offset_text(delta, gross_up, gross_down)))
    nulls = [x for x in segs if "为空" in x.flags]
    if nulls:
        caveats.append(ph.caveat("null_values", ph.null_segments_text([ph.key_text(x.key) for x in nulls], label)))
    if summed:
        caveats.append(ph.caveat("headline_basis", ph.HEADLINE_BASIS))
    if outcome is not None and outcome.verdict != "comparable":
        caveats.append(ph.caveat("comparability", outcome.phrase))
    top = ranked[0] if ranked else max(segs, key=lambda x: abs(deltas[x.key]))
    total_text = ph.change(delta, B)
    top_n = int(s["table_top_k"])
    if code == "driver":
        main = ph.decompose_driver(label, total_text, ph.key_text(top.key), ph.amount(deltas[top.key], signed=True),
                                   shares[top.key])
    elif code == "concentrated":
        main = ph.decompose_concentrated(label, total_text, ph.names([ph.key_text(x.key) for x in subjects], top_n),
                                         top_k_share)
    elif code == "dispersed":
        main = ph.decompose_dispersed(label, total_text, ph.dims_text(dims), main_share)
    elif code == "flat":
        ups = [x for x in segs if deltas[x.key] > 0]
        downs = [x for x in segs if deltas[x.key] < 0]
        pair = None
        if offset and ups and downs:
            up = max(ups, key=lambda x: deltas[x.key])
            down = min(downs, key=lambda x: deltas[x.key])
            pair = (ph.key_text(up.key), ph.amount(deltas[up.key], signed=True), ph.key_text(down.key),
                    ph.amount(deltas[down.key], signed=True))
        main = ph.decompose_flat(label, delta / abs(B) if B != 0 else None, pair)
    else:
        gap = max(gaps.values(), key=abs)
        main = ph.decompose_unclosed(ph.dims_text(dims), gap, ratio=False)
    order = sorted(segs, key=lambda x: -abs(deltas[x.key]))
    rows = []
    for x in order:
        d = deltas[x.key]
        rows.append([*x.key, canon(x.base), canon(x.target), canon(d), canon(d / x.base) if x.base != 0 else None,
                     canon(shares[x.key]) if shares else None, "、".join(x.flags) or None])
    columns = [*by, *_FIXED_ADDITIVE]
    table = {"columns": columns,
             "labels": {**dict(zip(by, dims)), "base": "基期", "target": "本期", "delta": "变化", "pct": "变化率",
                        "share": "占变化的比重", "flag": "说明"},
             "rows": rows,
             "column_types": column_types(columns, dims={c: side_t.snap.kind(c) for c in by}, texts=("flag",))}
    values: dict[str, Any] = {
        "base_total": canon(B), "target_total": canon(T), "delta_total": canon(delta),
        "pct_total": canon(delta / abs(B)) if B != 0 else None, "top_key": ph.key_text(top.key),
        "top_share": canon(shares[top.key]) if shares else None, "top_k": str(top_k) if top_k is not None else None,
        "top_k_share": canon(top_k_share) if top_k_share is not None else None, "gross_up": canon(gross_up),
        "gross_down": canon(gross_down), "segments": str(n),
    }
    if gaps:
        values["gap"] = canon(max(gaps.values(), key=abs))
    verdict = {"code": code, "subjects": [_key_strings(x.key) for x in subjects],
               "direction": sign_word(delta) if code in ("driver", "concentrated", "dispersed") else None}
    # 一份快照时 comparability 只会是本期核对不了的那句（同波动基线，只在证据面板）；两份快照的可比性结论照旧进判定语
    skip = (("offset",) if code == "flat" else ()) + (("comparability",) if outcome is None else ())
    phrase = ph.with_caveats(main, caveats, skip=skip)
    return build(env, params, used, table=table, values=values, verdict=verdict,
                 criterion=ph.criterion_additive(s, top_k_max=k_max, summed=summed), phrase=phrase, caveats=caveats,
                 formats=_rate_formats(columns),
                 named={k: CONV_PCT for k in ("pct_total", "top_share", "top_k_share")})


# --------------------------------------------------------------------------
# ratio
# --------------------------------------------------------------------------


def ratio(raw: Mapping[str, Any], env: Env) -> Any:
    used = list(USED["ratio"])
    params, bs, ts, cols, by = _sides(raw, env, [("numerator", "分子列"), ("denominator", "分母列")],
                                      reserved=_FIXED_RATIO)
    num, den = cols["numerator"], cols["denominator"]
    num_label, den_label = column_label(env, ts, num), column_label(env, ts, den)
    label = ph.pick_label(raw.get("label"), ph.say(num_label, "与", den_label, "之比"))
    if raw.get("label") is not None:
        params["label"] = ph.clean_label(raw.get("label")) or None
    dims = [column_label(env, ts, c) for c in by]
    refs, heads = _headline(raw, env)
    if refs:
        params["headline"] = refs
    parent_row = None
    if raw.get("parent") is not None:
        params["parent"], parent_row, params["depth"] = _parent(raw, env, "ratio", len(by))
        used.append("max_drill_depth")
    criterion = ph.criterion_ratio(env.settings)
    for snap in dict.fromkeys([bs, ts]):
        if snap.truncated:
            env.inputs.use(snap, "data" if bs is ts else ("base" if snap is bs else "target"), [*by, num, den])
            return refuse(env, params, used, "truncated", f"{snap.ref} 只取回了前 {len(snap.rows)} 行，各段合起来不是完整的数",
                          criterion=criterion)
    rows_b, rows_t = _rows_of(raw, params, bs, ts)
    period_label = column_label(env, ts, params["period"]) if "period" in params else None

    def reader(snap: Snap) -> Any:
        return lambda r: (snap.number(r, num, "分子列"), snap.number(r, den, "分母列"))

    side_b = _collect(bs, rows_b, by, reader(bs), dims, period_label)
    side_t = _collect(ts, rows_t, by, reader(ts), dims, period_label)
    for side in (side_b, side_t):
        side.nulls = [k for k, v in side.values.items() if v[0] is None or v[1] is None]
        side.values = {k: (v[0] or Fraction(0), v[1] or Fraction(0)) for k, v in side.values.items()}
    if bs is ts:
        env.inputs.use(ts, "data", [*by, params["period"], num, den], side_b.rows + side_t.rows)
    else:
        env.inputs.use(bs, "base", [*by, num, den], side_b.rows)
        env.inputs.use(ts, "target", [*by, num, den], side_t.rows)
    outcome, structure = _comparability(env, params, bs, ts, den)
    if structure is not None:
        return refuse(env, params, used, "incomparable_sides", f"{structure}，不是同一个量的两期，不拆", criterion=criterion)
    partial, note = _period_complete(env, params, ts, den)
    if partial is not None:
        return refuse(env, params, used, "partial_target", partial, criterion=criterion)
    segs = _segments(side_b, side_t, (Fraction(0), Fraction(0)))
    live = [x for x in segs if x.base[1] != 0 or x.target[1] != 0]
    if len(live) < 2:
        return refuse(env, params, used, "too_few_segments",
                      ph.say("按", ph.dims_text(dims), f"只有 {len(live)} 段，至少要 2 段才拆得开"),
                      criterion=criterion)
    for when, i in (("基期", 0), ("本期", 1)):
        if sum(((x.base, x.target)[i][1] for x in live), Fraction(0)) == 0:
            return refuse(env, params, used, "zero_base", ph.say(f"{when}的", den_label, "合计为 0，算不了比率"),
                          criterion=criterion)
    for x in live:
        for (n_, d_), when in ((x.base, "基期"), (x.target, "本期")):
            if d_ == 0 and n_ != 0:
                return refuse(env, params, used, "zero_base",
                              ph.say(ph.key_text(x.key), f"{when}的", den_label, "为 0 而", num_label,
                                     "不为 0，比率拆不了"), criterion=criterion)
    return _ratio_result(env, params, used, segs, by, dims, label, num_label, den_label, heads, parent_row, outcome,
                         criterion, target_snap=ts, note=note)


def ratio_effects(segs: Sequence[tuple[Fraction, Fraction, Fraction, Fraction]]
                  ) -> tuple[Fraction, Fraction, list[tuple[Fraction, ...]]]:
    """比率拆解的计算（细案 §2.2，中点对称拆法）。segs 是各段 (分子_基期, 分母_基期, 分子_本期, 分母_本期)，两期分母都为 0
    的段先剔掉。交回 (R, R', 各段 (w, w', r, r', mix, rate))：Σmix + Σrate = R' − R 逐位成立（Σ(w' − w) = 0）。一边分母为 0
    的段：那一边权重 0、比率取另一边的（自身效应为 0，变化全记在结构上）。"""
    db = sum((s[1] for s in segs), Fraction(0))
    dt = sum((s[3] for s in segs), Fraction(0))
    R = sum((s[0] for s in segs), Fraction(0)) / db
    R2 = sum((s[2] for s in segs), Fraction(0)) / dt
    mid = (R + R2) / 2
    out = []
    for nb, d_b, nt, d_t in segs:
        w, w2 = d_b / db, d_t / dt
        r = nb / d_b if d_b != 0 else None
        r2 = nt / d_t if d_t != 0 else None
        r = r2 if r is None else r
        r2 = r if r2 is None else r2
        mix = (w2 - w) * ((r + r2) / 2 - mid)                   # type: ignore[operator]
        rate = (r2 - r) * (w + w2) / 2                          # type: ignore[operator]
        out.append((w, w2, r, r2, mix, rate))
    return R, R2, out


def _ratio_result(env: Env, params: dict[str, Any], used: list[str], segs: list[Segment], by: list[str],
                  dims: list[str], label: str, num_label: str, den_label: str,
                  heads: Mapping[str, tuple[Fraction, Any]], parent_row: Mapping[str, Any] | None,
                  outcome: cmp.Outcome | None, criterion: str, *, target_snap: Snap,
                  note: Mapping[str, str] | None = None) -> Any:
    main_share, flat_pct = env.threshold("main_share"), env.threshold("flat_pct")
    min_den = env.threshold("min_denominator")
    live = [x for x in segs if x.base[1] != 0 or x.target[1] != 0]
    dropped = [x for x in segs if x not in live]
    R, R2, effects = ratio_effects([(x.base[0], x.base[1], x.target[0], x.target[1]) for x in live])
    delta = R2 - R
    by_key = {x.key: e for x, e in zip(live, effects)}
    gaps: dict[str, Fraction] = {}
    for side, total in (("base", R), ("target", R2)):
        if side in heads and not closes(total, heads[side][0], heads[side][1]):
            gaps[side] = heads[side][0] - total
    if parent_row is not None:
        sums = {"n_base": sum((x.base[0] for x in live), Fraction(0)), "d_base": sum((x.base[1] for x in live), Fraction(0)),
                "n_target": sum((x.target[0] for x in live), Fraction(0)),
                "d_target": sum((x.target[1] for x in live), Fraction(0))}
        for col, total in sums.items():
            expect = to_fraction(parent_row.get(col))
            if expect is None or not closes(total, expect, None, exact=True):
                gaps.setdefault("base" if col.endswith("base") else "target", (expect or Fraction(0)) - total)
    closed = not gaps
    mix_total = sum((e[4] for e in effects), Fraction(0))
    rate_total = sum((e[5] for e in effects), Fraction(0))
    flat = delta == 0 or (R != 0 and abs(delta) / abs(R) < flat_pct)
    mix_share = mix_total / delta if delta != 0 else None
    rate_share = rate_total / delta if delta != 0 else None
    sign = 1 if delta >= 0 else -1
    if not closed:
        code, subjects = "unclosed", []
    elif flat:
        code, subjects = "flat", []
    elif mix_share >= main_share and rate_share < main_share:  # type: ignore[operator]
        code = "mix_driven"
        subjects = [max(live, key=lambda x: sign * by_key[x.key][4])]
    elif rate_share >= main_share and mix_share < main_share:  # type: ignore[operator]
        code = "rate_driven"
        subjects = [max(live, key=lambda x: sign * by_key[x.key][5])]
    else:
        code, subjects = "both", []
    proportion = all(0 <= v[0] <= v[1] for x in live for v in (x.base, x.target))
    small = [x for x in live if x.base[1] < min_den or x.target[1] < min_den]
    caveats: list[dict[str, str]] = []
    eligible = [x for x in live if x not in small]
    if delta != 0 and eligible:
        weight = sum(((by_key[x.key][0] + by_key[x.key][1]) / 2 for x in eligible), Fraction(0))
        against = sum(((by_key[x.key][0] + by_key[x.key][1]) / 2 for x in eligible
                       if (by_key[x.key][3] - by_key[x.key][2]) * sign < 0), Fraction(0))
        if weight > 0 and against / weight >= SIMPSON_SHARE:
            # 判在结构变化上（mix_driven）时判定语正文已经说了结构变化、点了这一段，附带说明只说各段和整体反着变
            star = max(live, key=lambda x: sign * by_key[x.key][4])
            caveats.append(ph.caveat("simpson", ph.simpson_text(label, delta > 0, ph.key_text(star.key),
                                                                by_key[star.key][0], by_key[star.key][1],
                                                                shift=code != "mix_driven")))
    if small:
        caveats.append(ph.caveat("small_denominator", ph.small_denominator_text(
            [ph.key_text(x.key) for x in small], den_label, int(env.settings["min_denominator"]))))
    nulls = [x for x in segs if "为空" in x.flags]
    if nulls:
        caveats.append(ph.caveat("null_values", ph.null_segments_text([ph.key_text(x.key) for x in nulls],
                                                                      ph.say(num_label, "或", den_label))))
    if outcome is not None and outcome.verdict != "comparable":
        caveats.append(ph.caveat("comparability", outcome.phrase))
    if note is not None:
        caveats.append(dict(note))
    total_text = ph.ratio_change(R, R2, proportion=proportion)
    if code == "mix_driven":
        star = subjects[0]
        main = ph.ratio_mix(label, total_text, ph.key_text(star.key), by_key[star.key][0], by_key[star.key][1])
    elif code == "rate_driven":
        main = ph.ratio_rate(label, total_text, ph.key_text(subjects[0].key))
    elif code == "both":
        main = ph.ratio_both(label, total_text, mix_share, rate_share)
    elif code == "flat":
        main = ph.decompose_flat(label, delta / abs(R) if R != 0 else None, None)
    else:
        main = ph.decompose_unclosed(ph.dims_text(dims), max(gaps.values(), key=abs), ratio=True)
    rows = []
    for x in sorted(live, key=lambda x: -abs(by_key[x.key][4] + by_key[x.key][5])):
        w, w2, r, r2, mix, rate = by_key[x.key]
        flags = list(x.flags)
        if x.base[1] == 0:
            flags.append("基期分母为 0")
        if x.target[1] == 0:
            flags.append("本期分母为 0")
        if x in small:
            flags.append("分母小")
        rows.append([*x.key, canon(x.base[0]), canon(x.target[0]), canon(x.base[1]), canon(x.target[1]), canon(w),
                     canon(w2), canon(r), canon(r2), canon(mix), canon(rate), canon(mix + rate),
                     "、".join(dict.fromkeys(flags)) or None])
    for x in dropped:
        rows.append([*x.key, canon(x.base[0]), canon(x.target[0]), "0", "0", None, None, None, None, None, None, None,
                     "两期分母都为 0，不计入"])
    columns = [*by, *_FIXED_RATIO]
    table = {"columns": columns,
             "labels": {**dict(zip(by, dims)), "n_base": ph.say("基期", num_label), "n_target": ph.say("本期", num_label),
                        "d_base": ph.say("基期", den_label), "d_target": ph.say("本期", den_label),
                        "w_base": "基期权重", "w_target": "本期权重", "r_base": "基期比率", "r_target": "本期比率",
                        "mix": "结构效应", "rate": "自身效应", "total": "合计影响", "flag": "说明"},
             "rows": rows,
             "column_types": column_types(columns, dims={c: target_snap.kind(c) for c in by}, texts=("flag",))}
    values: dict[str, Any] = {
        "r_base_total": canon(R), "r_target_total": canon(R2), "delta": canon(delta), "mix_total": canon(mix_total),
        "rate_total": canon(rate_total), "mix_share": canon(mix_share), "rate_share": canon(rate_share),
        "segments": str(len(live)),
    }
    if gaps:
        values["gap"] = canon(max(gaps.values(), key=abs))
    verdict = {"code": code, "subjects": [_key_strings(x.key) for x in subjects],
               "direction": sign_word(delta) if code in ("mix_driven", "rate_driven", "both") else None}
    # 显示格式按「这一段（这个数）是占比还是带量纲的比率」分：
    # - 各段分子都不超过分母时，整体和各段的比率是个占比（退款率）：比率按百分数，结构 / 自身 / 合计影响是比率的变化，
    #   按百分点
    # - 否则比率本身带量纲（客单价，元 / 单，可以大于 1）：比率照原值；影响也是同一个量纲的变化（元 / 单），照原值，
    #   不能乘一百说成「个百分点」——否则判定语写「客单价下降 9.89」、表里同一件事写「-988.7 个百分点」
    ratio_rate = ("r_base", "r_target") if proportion else ()
    points = ("mix", "rate", "total") if proportion else ()
    named_points = ("delta", "mix_total", "rate_total") if proportion else ()
    return build(env, params, used, table=table, values=values, verdict=verdict, criterion=criterion,
                 phrase=ph.with_caveats(main, caveats, skip=("comparability",) if outcome is None else ()),
                 caveats=caveats,
                 formats=_rate_formats(columns, rate=("w_base", "w_target") + ratio_rate, points=points),
                 named={k: CONV_POINTS for k in named_points}
                 | ({k: CONV_PCT for k in ("r_base_total", "r_target_total")} if proportion else {})
                 | {"mix_share": CONV_PCT, "rate_share": CONV_PCT})


# --------------------------------------------------------------------------
# factor
# --------------------------------------------------------------------------


def _half_ulp_ratio(raw: Any, value: Fraction) -> Fraction:
    from app.analysis.common import written_places

    return Fraction(1, 2) / Fraction(10) ** written_places(raw) / abs(value) if value != 0 else Fraction(0)


def lmdi(base: Sequence[float], target: Sequence[float], m_base: float, m_target: float) -> list[float]:
    """LMDI-I：contrib_k = L(M', M) · ln(F_k' / F_k)，L(a, b) = (a − b) / (ln a − ln b)，L(a, a) = a。因子按书写精度取过整、
    乘积和乘积列差一点时，各因子按同一比例缩放到乘积恰好等于乘积列（每个因子的对数比各补上 (1/K)·差的对数），Σcontrib 在
    浮点误差内等于 M' − M。"""
    k = len(base)
    fix = (math.log(m_target / math.prod(target)) - math.log(m_base / math.prod(base))) / k
    weight = m_base if m_base == m_target else (m_target - m_base) / (math.log(m_target) - math.log(m_base))
    return [weight * (math.log(t / b) + fix) for b, t in zip(base, target)]


def factor(raw: Mapping[str, Any], env: Env) -> Any:
    used = list(USED["factor"])
    params, bs, ts, cols, _ = _sides(raw, env, [("product", "乘积列")], by_needed=False)
    product = cols["product"]
    wanted = as_list(raw.get("factors"))
    if not 2 <= len(wanted) <= 4:
        raise CallError("bad_factors", "因子列要写 2 到 4 列")
    factors = [ts.column(c, "因子列") for c in wanted]
    if bs is not ts:
        for c in factors:
            bs.column(c, "因子列")
    if len(set(factors)) != len(factors) or product in factors:
        raise CallError("bad_factors", "因子列不能重复，也不能是乘积列本身")
    params["factors"] = factors
    label = label_for(env, raw.get("label"), ts, product)
    if raw.get("label") is not None:
        params["label"] = ph.clean_label(raw.get("label")) or None
    names = [column_label(env, ts, c) for c in factors]
    criterion = ph.criterion_factor(env.settings)
    for snap in dict.fromkeys([bs, ts]):
        if snap.truncated:
            env.inputs.use(snap, "data" if bs is ts else ("base" if snap is bs else "target"), [product, *factors])
            return refuse(env, params, used, "truncated", f"{snap.ref} 只取回了前 {len(snap.rows)} 行，结果不完整",
                          criterion=criterion)
    if "period" in params:
        rows_b = _period_rows(bs, params["period"], params["base"], "基期")
        rows_t = _period_rows(ts, params["period"], params["target"], "本期")
    else:
        rows_b, rows_t = list(range(len(bs.rows))), list(range(len(ts.rows)))
    for rows, snap, when in ((rows_b, bs, "基期"), (rows_t, ts, "本期")):
        if len(rows) != 1:
            raise CallError("duplicate_rows", f"{snap.ref} 里{when}有 {len(rows)} 行：乘积拆解每期要正好一行（在 SQL 里汇总成一行）")
    cells: dict[str, list[tuple[Fraction, Any]]] = {}
    for when, snap, r in (("base", bs, rows_b[0]), ("target", ts, rows_t[0])):
        row = []
        for col, what in ((product, "乘积列"), *((c, "因子列") for c in factors)):
            value = snap.number(r, col, what)
            if value is None:
                raise CallError("empty_cell", f"{snap.ref} 第 {r} 行的「{col}」是空的：乘积、因子都要有值",
                                reason=f"{'基期' if when == 'base' else '本期'}的「{col}」那一格是空的（{snap.ref} 第 {r} 行）")
            row.append((value, snap.cell(r, col)))
        cells[when] = row
    if bs is ts:
        env.inputs.use(ts, "data", [params["period"], product, *factors], rows_b + rows_t)
    else:
        env.inputs.use(bs, "base", [product, *factors], rows_b)
        env.inputs.use(ts, "target", [product, *factors], rows_t)
    outcome, structure = _comparability(env, params, bs, ts, product)
    if structure is not None:
        return refuse(env, params, used, "incomparable_sides", f"{structure}，不是同一个量的两期，不拆", criterion=criterion)
    partial, note = _period_complete(env, params, ts, product)
    if partial is not None:
        return refuse(env, params, used, "partial_target", partial, criterion=criterion)
    if any(v <= 0 for row in cells.values() for v, _ in row):
        return refuse(env, params, used, "non_positive", "乘积或因子里有不大于 0 的值，对数拆解要求全为正", criterion=criterion)
    for when, row in cells.items():
        m = row[0][0]
        prod = math.prod((v for v, _ in row[1:]), start=Fraction(1))
        rel = abs(m - prod) / abs(m)
        allowed = max(_FACTOR_REL, sum((_half_ulp_ratio(raw_v, v) for v, raw_v in row[1:]), Fraction(0)))
        if rel > allowed:
            raise CallError("not_product", ph.say("这几列相乘不等于", column_label(env, ts, product),
                                                  f"（{'基期' if when == 'base' else '本期'}差 {ph.pct(rel, signed=False)}），"
                                                  "不是乘积关系：换成乘起来正好等于它的几列"))
    M, M2 = cells["base"][0][0], cells["target"][0][0]
    fb = [float(v) for v, _ in cells["base"][1:]]
    ft = [float(v) for v, _ in cells["target"][1:]]
    contrib = lmdi(fb, ft, float(M), float(M2))
    delta = M2 - M
    flat = delta == 0 or abs(delta) / abs(M) < env.threshold("flat_pct")
    shares = [c / float(delta) for c in contrib] if delta != 0 else [None] * len(contrib)
    main_share = float(env.threshold("main_share"))
    order = sorted(range(len(factors)), key=lambda i: -abs(contrib[i]))
    caveats: list[dict[str, str]] = []
    if outcome is not None and outcome.verdict != "comparable":
        caveats.append(ph.caveat("comparability", outcome.phrase))
    if note is not None:
        caveats.append(note)
    total_text = ph.change(delta, M)
    top = max(range(len(factors)), key=lambda i: shares[i] if shares[i] is not None else -math.inf)
    if flat:
        code, subjects = "flat", []
        main = ph.decompose_flat(label, delta / abs(M), None)
    elif shares[top] >= main_share:  # type: ignore[operator]
        code, subjects = "driver", [[names[top]]]
        main = ph.factor_driver(label, total_text, names[top], contrib[top], shares[top])
    else:
        code, subjects = "both", []
        main = ph.factor_both(label, total_text, [(names[i], shares[i]) for i in order])
    rows = []
    for i in order:
        b, t = cells["base"][i + 1][0], cells["target"][i + 1][0]
        rows.append([names[i], canon(b), canon(t), canon((t - b) / b), canon(contrib[i]),
                     canon(shares[i]) if shares[i] is not None else None])
    columns = ["factor", "base", "target", "pct", "contrib", "share"]
    table = {"columns": columns,
             "labels": {"factor": "因子", "base": "基期", "target": "本期", "pct": "变化率", "contrib": "贡献",
                        "share": "占变化的比重"},
             "rows": rows,
             "column_types": column_types(columns, texts=("factor",))}
    values = {"base_total": canon(M), "target_total": canon(M2), "delta_total": canon(delta),
              "pct_total": canon(delta / abs(M)), "top_key": names[top],
              "top_share": canon(shares[top]) if shares[top] is not None else None}
    verdict = {"code": code, "subjects": subjects, "direction": sign_word(delta) if code != "flat" else None}
    return build(env, params, used, table=table, values=values, verdict=verdict, criterion=criterion,
                 phrase=ph.with_caveats(main, caveats, skip=("comparability",) if outcome is None else ()),
                 caveats=caveats, formats=_rate_formats(columns),
                 named={k: CONV_PCT for k in ("pct_total", "top_share")})


__all__ = ["HEADLINE_SHAPE", "PARENT_SHAPE", "SAME_QUERY", "SIMPSON_SHARE", "USED", "additive", "factor", "lmdi",
           "ratio", "ratio_effects", "refusal_basis"]
