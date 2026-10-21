"""算子共用的：一次计算的环境（输入、目录事实、现查簿、阈值）、结论的组装、几样参数的规范化。

每个算子是 (raw params, Env) → AnalysisResult 的纯函数：只经 Env 取输入快照（Inputs）、目录事实（Facts）、现查结果
（Book），阈值只来自 Env.settings。run 和 recompute 只是装 Env 的方式不同（见 app/analysis/__init__.py）。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any

from app.analysis import phrases as ph
from app.analysis.contract import METHODS, AnalysisResult, CallError
from app.analysis.facts import Facts
from app.analysis.inputs import Inputs
from app.analysis.numbers import to_fraction
from app.analysis.probe import Book

#: 结论的 params 里由系统填、不收调用方的几项：目录事实、运行当天、下钻的层数
DERIVED_PARAMS = frozenset({"catalog", "today", "depth"})


@dataclass
class Env:
    op: str
    mode: str | None
    settings: Mapping[str, Any]
    inputs: Inputs
    facts: Facts
    book: Book
    today: date | None
    parents: Callable[[str], Mapping[str, Any] | None] | None
    hypothesis: str | None

    def threshold(self, key: str) -> Fraction:
        value = to_fraction(self.settings[key])
        assert value is not None, key
        return value


def label_for(env: Env, raw: Any, snap: Any, column: str | None, *, default: str | None = None) -> str:
    """判定语里的度量叫法：agent 给的 label → 目录里的中文名 → default → 列名，依次取第一个消毒后不为空的（agent 给了能用的
    就不去查目录）。"""
    if ph.clean_label(raw):
        return ph.clean_label(raw)
    catalog = env.facts.label(snap, column) if column else None
    return ph.pick_label(catalog, default, column)


def column_label(env: Env, snap: Any, column: str) -> str:
    """维度、因子这类列的叫法：目录里的中文名，没有就是列名（都消毒）。"""
    return ph.pick_label(env.facts.label(snap, column), column)


def today_param(env: Env) -> str:
    """要判本期完不完整的算子把运行当天记进 params（复算用记下的那一天）。"""
    day = env.today or date.today()
    env.today = day
    return day.isoformat()


def build(env: Env, params: dict[str, Any], used: Sequence[str], *, status: str = "ok",
          reason_code: str | None = None, reason: str | None = None, table: Mapping[str, Any] | None = None,
          values: Mapping[str, Any] | None = None, verdict: Mapping[str, Any] | None = None, criterion: str,
          phrase: str, caveats: Sequence[Mapping[str, str]] = ()) -> AnalysisResult:
    """组装一份结论：形状严格按 contract.AnalysisResult，params 补上查过的目录事实。"""
    record = env.facts.record()
    out_params = {k: v for k, v in params.items() if v is not None}
    if record:
        out_params["catalog"] = record
    return {
        "op": env.op,
        "mode": env.mode,
        "method": METHODS[(env.op, env.mode)],
        "params": out_params,
        "settings": {k: env.settings[k] for k in used},
        "inputs": list(env.inputs.records),
        "probes": env.book.used,
        "status": status,  # type: ignore[typeddict-item]
        "reason_code": reason_code,
        "reason": reason,
        "table": dict(table) if table is not None else None,  # type: ignore[typeddict-item]
        "values": dict(values or {}),
        "verdict": dict(verdict) if verdict is not None else None,  # type: ignore[typeddict-item]
        "criterion": criterion,
        "phrase": phrase,
        "caveats": [dict(c) for c in caveats],  # type: ignore[misc]
        "hypothesis": env.hypothesis,
    }


def refuse(env: Env, params: dict[str, Any], used: Sequence[str], code: str, reason: str, *,
           criterion: str, caveats: Sequence[Mapping[str, str]] = ()) -> AnalysisResult:
    """算不了（status refused）：判定语是「{算子的叫法}没有做成：{原因}」。"""
    return build(env, params, used, status="refused", reason_code=code, reason=reason, criterion=criterion,
                 phrase=ph.refused(env.op, reason), caveats=caveats)


def need(raw: Mapping[str, Any], key: str, what: str) -> Any:
    """必填的参数；没写是调用错误。"""
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()) or value == []:
        raise CallError("missing_param", f"没写{what}")
    return value


def written_places(raw: Any) -> int:
    """快照里一格写到第几位小数（headline 是取整过的，按它的书写精度容差）：数字文本按字面，浮点按最短写法，整数为 0。"""
    if isinstance(raw, bool) or raw is None:
        return 0
    text = repr(raw) if isinstance(raw, float) else str(raw).strip()
    if "e" in text.lower():
        return 0
    return len(text.split(".", 1)[1]) if "." in text else 0


def closes(total: Fraction, cell: Fraction, raw: Any) -> bool:
    """各段之和（或合起来的比率）和总数那一格对得上：十进制逐位相等；总数取整过的，差在它书写精度的半个末位以内也算；浮点
    算出来的差在最后几位的也算（相对误差 1e-9 以内，同 engine/compare._equal）。"""
    if total == cell:
        return True
    gap = abs(total - cell)
    if gap <= Fraction(1, 2) / Fraction(10) ** written_places(raw):
        return True
    scale = max(abs(total), abs(cell))
    return scale != 0 and gap <= scale * Fraction(1, 10 ** 9)


def sign_word(value: Fraction | float | int) -> str | None:
    return "up" if value > 0 else "down" if value < 0 else None


__all__ = ["DERIVED_PARAMS", "Env", "build", "closes", "column_label", "label_for", "need", "refuse", "sign_word",
           "today_param", "written_places"]
