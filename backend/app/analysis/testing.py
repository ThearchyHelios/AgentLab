"""测试用的分析结论（只给测试用）：第 1 波各包并行开发时，证据接入、入口、说法核对的测试不等算子写完，按契约的形状
拿一份假的结论。算子落地后，端到端的测试改用 analysis.run 真算。

每个算子的假结论带它自己形状的结果表、具名标量、参数和输入（列 id、叫法、类型照各算子的实现：变化拆解按维度、波动基线和
趋势按期次、差异检验按对、可比性检查按检查项），数是编的、彼此对得上判定语。"""
from __future__ import annotations

from typing import Any

from app.analysis.contract import METHODS, AnalysisResult, effective_settings

_PHRASES = {
    "driver": "销售额下降 194 万（-8.7%），主要来自华东（-120 万，占 62%）",
    "concentrated": "销售额下降 194 万（-8.7%），集中在华东、华北（合计占 85%）",
    "dispersed": "销售额下降 194 万（-8.7%），分散在各地区，没有哪一段占到一半",
    "within_band": "销售额本期变化 -3.1%，在过去 12 周的常规波动范围内（-6.1% 至 +5.3%）",
    "outside_band": "销售额本期变化 -8.7%，超出过去 12 周的常规波动范围（-6.1% 至 +5.3%）",
    "rising": "过去 10 周线上销售额呈上升趋势（每周约 +3.0%）",
    "no_evidence": "西南和华东的退款率差异可能只是样本波动（样本量 40、230）",
    "comparable": "两期可比",
    "incomparable": "两期不可比：本期只有 3 天数据",
}

_WEEKS = ["2026-07-20", "2026-07-27", "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31",
          "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12"]


def _decompose() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    table = {
        "columns": ["region", "base", "target", "delta", "pct", "share", "flag"],
        "labels": {"region": "地区", "base": "基期", "target": "本期", "delta": "变化", "pct": "变化率",
                   "share": "占变化的比重", "flag": "说明"},
        "rows": [["华东", "820", "700", "-120", "-0.1463", "0.6186", None],
                 ["华北", "600", "560", "-40", "-0.0667", "0.2062", None],
                 ["华南", "700", "666", "-34", "-0.0486", "0.1753", None],
                 ["西南", "100", "100", "0", "0", "0", None]],
    }
    values = {"base_total": "2220", "target_total": "2026", "delta_total": "-194", "pct_total": "-0.0874",
              "top_key": "华东", "top_share": "0.6186", "segments": "4"}
    params = {"query": "Q2", "by": ["region"], "period": "week", "base": "W41", "target": "W42", "measure": "gmv",
              "label": "销售额"}
    return table, values, params, ["region", "week", "gmv"]


def _baseline() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    changes = ["0.012", "-0.021", "0.034", "-0.008", "0.019", "-0.045", "0.027", "-0.015", "0.006", "0.041", "-0.032",
               "-0.004"]
    levels = ["2210", "2164", "2238", "2220", "2262", "2160", "2218", "2185", "2198", "2288", "2215", "2206"]
    columns = ["week", "value", "change", "role"]
    rows = [[w, v, c, "历史"] for w, v, c in zip(_WEEKS[:-1], levels, changes)] + [[_WEEKS[-1], "2138", "-0.031", "本期"]]
    table = {"columns": columns, "labels": {"week": "周", "value": "销售额", "change": "变化率", "role": "说明"},
             "rows": rows, "column_types": {"week": "text", "value": "number", "change": "number", "role": "text"}}
    values = {"x": "-0.031", "value": "2138", "median": "0.001", "band_lo": "-0.061", "band_hi": "0.053", "z": "-1.12",
              "rank": "11", "n_history": "12", "hist_min": "-0.045", "hist_max": "0.041"}
    params = {"query": "Q1", "period": "week", "measure": "gmv", "label": "销售额", "grain": "week", "basis": "change",
              "lag": 1, "target": _WEEKS[-1], "today": "2026-10-21"}
    return table, values, params, ["week", "gmv"]


def _trend() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    levels = [100, 103, 106, 109, 113, 116, 119, 123, 127, 130]
    rows = [[w, str(v), str(v - levels[i - 1]) if i else None] for i, (w, v) in enumerate(zip(_WEEKS[-10:], levels))]
    table = {"columns": ["week", "value", "change"], "labels": {"week": "周", "value": "线上销售额", "change": "比上一期"},
             "rows": rows, "column_types": {"week": "text", "value": "number", "change": "number"}}
    values = {"slope": "3.333333333333", "rel_slope": "0.03", "p": "0.000002755732", "s": "45", "n": "10",
              "run_length": "9", "run_direction": "up"}
    params = {"query": "Q1", "period": "week", "measure": "gmv", "label": "线上销售额", "grain": "week",
              "today": "2026-10-21"}
    return table, values, params, ["week", "gmv"]


def _differ() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    columns = ["a", "b", "a_value", "b_value", "diff", "ci_lo", "ci_hi", "a_n", "b_n", "test", "p", "p_adj", "flag"]
    table = {"columns": columns,
             "labels": {"a": "组", "b": "对照组", "a_value": "组的比率", "b_value": "对照组的比率", "diff": "差",
                        "ci_lo": "差的区间下限", "ci_hi": "差的区间上限", "a_n": "组的样本量", "b_n": "对照组的样本量",
                        "test": "检验", "p": "p 值", "p_adj": "校正后的 p 值", "flag": "说明"},
             "rows": [["西南", "华东", "0.05", "0.021739130435", "0.028260869565", "-0.026", "0.137", "40", "230",
                       "Fisher 精确检验", "0.27", "0.27", "样本少，用了精确检验"]],
             "column_types": {"a": "text", "b": "text", **{c: "number" for c in columns[2:9]}, "test": "text",
                              "p": "number", "p_adj": "number", "flag": "text"}}
    values = {"a": "0.05", "b": "0.021739130435", "diff": "0.028260869565", "ci_lo": "-0.026", "ci_hi": "0.137",
              "p": "0.27", "p_adj": "0.27", "n_a": "40", "n_b": "230", "test": "fisher", "pairs": "1", "differs": "0"}
    params = {"query": "Q3", "by": ["region"], "numerator": "refunds", "denominator": "orders", "label": "退款率",
              "pairs": [["西南", "华东"]]}
    return table, values, params, ["region", "refunds", "orders"]


def _comparable() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    columns = ["check", "status", "base", "target", "note"]
    table = {"columns": columns,
             "labels": {"check": "检查项", "status": "结果", "base": "基期", "target": "本期", "note": "说明"},
             "rows": [["同口径", "通过", None, None, None],
                      ["窗口长度", "通过", "2026-10-05 到 2026-10-11（7 天）", "2026-10-12 到 2026-10-18（7 天）", None],
                      ["整期", "通过", "通过", "通过", None], ["星期构成", "通过", None, None, None],
                      ["数据问题", "通过", None, None, None]],
             "column_types": dict.fromkeys(columns, "text")}
    values = {"same_query": "pass", "window": "pass", "complete": "pass", "weekdays": "pass", "data": "pass",
              "base_start": "2026-10-05", "base_end": "2026-10-11", "base_days": "7", "base_weekend": "2",
              "target_start": "2026-10-12", "target_end": "2026-10-18", "target_days": "7", "target_weekend": "2"}
    params = {"base_query": "Q4", "target_query": "Q5", "measure": "gmv", "today": "2026-10-21"}
    return table, values, params, ["gmv"]


_SHAPES = {"decompose": _decompose, "baseline": _baseline, "trend": _trend, "differ": _differ,
           "comparable": _comparable}
_CRITERIA = {"decompose": "一段占总变化一半以上算主要来源",
             "baseline": "本期和过去 12 周的中位数比，差不到 2 倍稳健标准差算常规波动",
             "trend": "Mann–Kendall 检验的双侧 p 值小于 0.05 时按 Theil–Sen 斜率的正负判上升或下降",
             "differ": "双侧 p 值小于 0.05 算差异站得住",
             "comparable": "只差日期、窗口一样长、是整期、星期构成相同才算可比"}


def fake_result(op: str = "decompose", mode: str | None = "additive", verdict: str | None = "driver", *,
                subjects: list[list[str]] | None = None, direction: str | None = "down", status: str = "ok",
                reason_code: str | None = None, inputs: list[dict[str, Any]] | None = None,
                **overrides: Any) -> AnalysisResult:
    """一份形状完整的假结论。缺省是「销售额下降主要来自华东」的可加拆解；别的算子给各自形状的结果表、具名标量和参数。"""
    if op not in ("decompose", "differ"):
        mode = None
    elif op == "differ" and mode not in ("rate", "mean", "rows"):
        mode = "rate"
    table, values, params, columns = _SHAPES.get(op, _decompose)()
    if op == "comparable":
        default_inputs = [{"ref": ref, "artifact": "0" * 64, "role": role, "columns": columns, "rows": "all"}
                          for ref, role in (("Q4", "base"), ("Q5", "target"))]
    else:
        default_inputs = [{"ref": str(params.get("query") or "Q2"), "artifact": "0" * 64, "role": "data",
                           "columns": columns, "rows": "all"}]
    result: AnalysisResult = {
        "op": op,
        "mode": mode,
        "method": METHODS.get((op, mode), f"{op}@1"),
        "params": params,
        "settings": effective_settings(None),
        "inputs": inputs if inputs is not None else default_inputs,
        "probes": [],
        "status": status,  # type: ignore[typeddict-item]
        "reason_code": reason_code,
        "reason": None if status == "ok" else "历史只有 5 期，至少要 8 期",
        "table": table if status == "ok" else None,  # type: ignore[typeddict-item]
        "values": values if status == "ok" else {},
        "verdict": ({"code": verdict, "subjects": subjects if subjects is not None else
                     ([["华东"]] if verdict == "driver" else []), "direction": direction}  # type: ignore[typeddict-item]
                    if status == "ok" and verdict else None),
        "criterion": _CRITERIA.get(op, _CRITERIA["decompose"]),
        "phrase": _PHRASES.get(verdict or "", "") if status == "ok" else "变化拆解没有做成：历史只有 5 期，至少要 8 期",
        "caveats": [],
        "hypothesis": "地区拖累",
    }
    result.update(overrides)  # type: ignore[typeddict-item]
    return result


__all__ = ["fake_result"]
