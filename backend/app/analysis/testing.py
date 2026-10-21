"""测试用的分析结论（只给测试用）：第 1 波各包并行开发时，证据接入、入口、说法核对的测试不等算子写完，按契约的形状
拿一份假的结论。算子落地后，端到端的测试改用 analysis.run 真算。"""
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


def fake_result(op: str = "decompose", mode: str | None = "additive", verdict: str | None = "driver", *,
                subjects: list[list[str]] | None = None, direction: str | None = "down", status: str = "ok",
                reason_code: str | None = None, inputs: list[dict[str, Any]] | None = None,
                **overrides: Any) -> AnalysisResult:
    """一份形状完整的假结论。缺省是「销售额下降主要来自华东」的可加拆解。"""
    if op not in ("decompose", "differ"):
        mode = None
    table = {
        "columns": ["region", "base", "target", "delta", "pct", "share", "flag"],
        "labels": {"region": "地区", "base": "基期", "target": "本期", "delta": "变化", "pct": "变化率",
                   "share": "占变化的比重", "flag": "说明"},
        "rows": [["华东", "820", "700", "-120", "-0.1463", "0.6186", None],
                 ["华北", "600", "560", "-40", "-0.0667", "0.2062", None],
                 ["华南", "700", "666", "-34", "-0.0486", "0.1753", None],
                 ["西南", "100", "100", "0", "0", "0", None]],
    }
    result: AnalysisResult = {
        "op": op,
        "mode": mode,
        "method": METHODS.get((op, mode), f"{op}@1"),
        "params": {"query": "Q2", "by": ["region"], "period": "week", "base": "W41", "target": "W42",
                   "measure": "gmv", "label": "销售额"},
        "settings": effective_settings(None),
        "inputs": inputs if inputs is not None else [
            {"ref": "Q2", "artifact": "0" * 64, "role": "data", "columns": ["region", "week", "gmv"], "rows": "all"}],
        "probes": [],
        "status": status,  # type: ignore[typeddict-item]
        "reason_code": reason_code,
        "reason": None if status == "ok" else "历史只有 5 期，至少要 8 期",
        "table": table if status == "ok" else None,
        "values": {"base_total": "2220", "target_total": "2026", "delta_total": "-194", "pct_total": "-0.0874",
                   "top_key": "华东", "top_share": "0.6186", "segments": "4"} if status == "ok" else {},
        "verdict": ({"code": verdict, "subjects": subjects if subjects is not None else
                     ([["华东"]] if verdict == "driver" else []), "direction": direction}  # type: ignore[typeddict-item]
                    if status == "ok" and verdict else None),
        "criterion": "一段占总变化一半以上算主要来源",
        "phrase": _PHRASES.get(verdict or "", "") if status == "ok" else "变化拆解没有做成：历史只有 5 期，至少要 8 期",
        "caveats": [],
        "hypothesis": "地区拖累",
    }
    result.update(overrides)  # type: ignore[typeddict-item]
    return result


__all__ = ["fake_result"]
