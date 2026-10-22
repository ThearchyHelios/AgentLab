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
          phrase: str, caveats: Sequence[Mapping[str, str]] = (),
          formats: Mapping[str, str] | None = None, named: Mapping[str, str] | None = None) -> AnalysisResult:
    """组装一份结论：形状严格按 contract.AnalysisResult，params 补上查过的目录事实。formats 是结果表各列的换算（占比、
    变化率的列记 pct，比率的变化记 pp），named 是具名的数的换算（value_formats）：都只留记了的，为空时整项不写。"""
    record = env.facts.record()
    out_params = {k: v for k, v in params.items() if v is not None}
    if record:
        out_params["catalog"] = record
    out_table = dict(table) if table is not None else None
    if out_table is not None and (table_formats := column_formats(formats or {})):
        out_table["formats"] = table_formats
    out: AnalysisResult = {
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
        "table": out_table,  # type: ignore[typeddict-item]
        "values": dict(values or {}),
        "verdict": dict(verdict) if verdict is not None else None,  # type: ignore[typeddict-item]
        "criterion": criterion,
        "phrase": phrase,
        "caveats": [dict(c) for c in caveats],  # type: ignore[misc]
        "hypothesis": env.hypothesis,
    }
    if named_formats := value_formats(named or {}):
        out["value_formats"] = named_formats
    return out


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


def reserved_check(column: str, reserved: Sequence[str], what: str) -> None:
    """结果表的列 id 是输入的维度列（期次列）加上固定的几列：重名了列 id 就重复，引用结果表的格、下钻按的维度数都会认错。
    遇到这些名字（不分大小写）是调用错误，让 agent 在 SQL 里换个别名。"""
    if column.lower() in {r.lower() for r in reserved}:
        raise CallError("reserved_column", f"{what}「{column}」和结果表的列重名（结果表要用 {'、'.join(reserved)} 这几个名字）："
                                           f"在 SQL 里给它换个别名再分析，比如 AS {column}_name")


def written_places(raw: Any) -> int:
    """快照里一格写到第几位小数（headline 是取整过的，按它的书写精度容差）：数字文本按字面，浮点按最短写法，整数为 0。是整数
    的浮点（SQLite 的 ROUND() 交回 2120.0）是取到个位的，也为 0——最短写法里的「.0」不是书写精度。"""
    if isinstance(raw, bool) or raw is None:
        return 0
    if isinstance(raw, float) and raw.is_integer():
        return 0
    text = repr(raw) if isinstance(raw, float) else str(raw).strip()
    if "e" in text.lower():
        return 0
    return len(text.split(".", 1)[1]) if "." in text else 0


def closes(total: Fraction, cell: Fraction, raw: Any, *, exact: bool = False) -> bool:
    """各段之和（或合起来的比率）和总数那一格对得上：十进制逐位相等；总数取整过的，差在它书写精度的半个末位以内也算；浮点
    算出来的差在最后几位的也算（相对误差 1e-9 以内，同 engine/compare._equal）。exact：对的是上一层那一段算出来的精确值
    （下钻），没有取整，只容浮点的最后几位。"""
    if total == cell:
        return True
    gap = abs(total - cell)
    if not exact and gap <= Fraction(1, 2) / Fraction(10) ** written_places(raw):
        return True
    scale = max(abs(total), abs(cell))
    return scale != 0 and gap <= scale * Fraction(1, 10 ** 9)


def column_types(columns: Sequence[str], *, dims: Mapping[str, str | None] | None = None,
                 texts: Sequence[str] = ()) -> dict[str, str]:
    """结果表各列的类型（ResultTable.column_types，按列的先后）：dims 里的列照给的类型（维度列照输入快照里那一列查询时记下的
    类型，没记的为 None、不写，同快照「拿不准的列不在里面」）；texts 里的是文字列（说明、叫法）写 text；其余都是数值列写
    number（格子是十进制字符串）。"""
    out: dict[str, str] = {}
    for column in columns:
        if dims is not None and column in dims:
            if dims[column]:
                out[column] = dims[column]  # type: ignore[assignment]
        else:
            out[column] = "text" if column in texts else "number"
    return out


def column_formats(formats: Mapping[str, str]) -> dict[str, str]:
    """结果表的 formats（ResultTable.formats）：列 id → 换算。只留记了换算的列，都为空时整项不写（结论和以前一字不差）。"""
    return {str(k): v for k, v in formats.items() if v}


def value_formats(values: Mapping[str, Any]) -> dict[str, str]:
    """结论的 value_formats（AnalysisResult.value_formats）：键 → 换算。只留记了换算的数，都为空时整项不写。"""
    return {str(k): v for k, v in values.items() if v}


def sign_word(value: Fraction | float | int) -> str | None:
    return "up" if value > 0 else "down" if value < 0 else None


__all__ = ["DERIVED_PARAMS", "Env", "build", "closes", "column_formats", "column_label", "column_types", "label_for",
           "need", "refuse", "reserved_check", "sign_word", "today_param", "value_formats", "written_places"]
