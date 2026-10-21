"""算子的输入：按名字取快照、拆单元格的写法、按列名认列、遮罩列、度量格换成精确的数、同一键同一期多行（细案 01 §1、§8.1）。

- 名字：agent 写本节点查过的编号（Q3，和交回模型的「【证据 Q3】」同一个），分析卡写来源节点 id；resolve 取不到就是调用错误。
- 单元格写成「名字.r行.列」（Q1.r0.gmv），行从 0 数；名字里可以有点（取最后一个「.r数字.」切开）。
- 列名比较不分大小写，按快照的 columns 定位：一字不差的优先；只差大小写的有好几列时是调用错误。
- 遮罩列（快照的 masked，不分大小写）不能当维度、度量、期次：调用错误。结论里只出现非遮罩列的格。
- 度量格：数（含 DECIMAL 落成的数字文本）换成 Fraction；空值为 None（各算子按细案 §1 处理）；文本、布尔是调用错误。
  查询时记了类型的列按类型认：text 列里的 "2026" 不当数。

调用错误（contract.CallError）的说法一种，写给 agent：说清哪里不对、怎么改。
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app.analysis.contract import AnalysisInput, CallError, InputSnapshot
from app.analysis.numbers import canon, to_fraction

#: 单元格的写法：名字.r行.列
_CELL = re.compile(r"^(?P<name>.+)\.r(?P<row>\d+)\.(?P<column>.+)$")
#: 报错里列出的列最多几个
_LIST_MAX = 12


def _listed(columns: Sequence[str]) -> str:
    shown = "、".join(columns[:_LIST_MAX])
    return shown + (f"等 {len(columns)} 列" if len(columns) > _LIST_MAX else "")


def hashable(value: Any) -> Any:
    """维度值当键：文本、数、布尔、空值原样；别的（列表、对象）和非数、无穷写成文字（同 engine/compare._hashable）。"""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value if isinstance(value, (str, int, float, bool)) or value is None else repr(value)


def same_value(a: Any, b: Any) -> bool:
    """调用里写的期次、段和快照里的值是不是同一个：相等，或者写出来一样（"2026" 和 2026、" 华东" 和 "华东"）。"""
    if a == b and type(a) is type(b):
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    fa, fb = to_fraction(a), to_fraction(b)
    if fa is not None and fb is not None and not isinstance(a, str) and not isinstance(b, str):
        return fa == fb
    return str(a).strip() == str(b).strip()


@dataclass(eq=False)
class Snap:
    """一份输入快照（InputSnapshot 的薄包装）。"""

    ref: str
    data: Mapping[str, Any]
    dialect: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    named: list[str] = field(default_factory=list)        # 调用里认过的列（按先后，Inputs.settle 记用法用）

    def __post_init__(self) -> None:
        self.columns = [str(c) for c in self.data.get("columns") or []]
        self.rows = [list(r) if isinstance(r, (list, tuple)) else [] for r in self.data.get("rows") or []]

    @property
    def artifact(self) -> str | None:
        value = self.data.get("artifact")
        return value if isinstance(value, str) and value else None

    @property
    def source(self) -> str:
        return str(self.data.get("source") or "")

    @property
    def sql(self) -> str:
        return str(self.data.get("sql") or "")

    @property
    def truncated(self) -> bool:
        return bool(self.data.get("truncated"))

    @property
    def masked(self) -> set[str]:
        """不能当维度、度量的列（小写）：入口交来的 masked；没交（复算时取回的是快照工件本身）就按快照记下的遮罩列
        （数据源设的 mask_columns、查询时整列遮掉的 masked_at_query）。"""
        found = self.data.get("masked")
        if found is None:
            found = [*(self.data.get("mask_columns") or []), *(self.data.get("masked_at_query") or [])]
        return {str(m).strip().lower() for m in found or [] if str(m).strip()}

    def kind(self, column: str) -> str | None:
        """查询时记下的这一列的类型（column_types 是 {列: 类型} 或按列序的列表）。"""
        types = self.data.get("column_types")
        if isinstance(types, Mapping):
            kind = types.get(column)
        elif isinstance(types, list) and column in self.columns:
            i = self.columns.index(column)
            kind = types[i] if i < len(types) else None
        else:
            kind = None
        return kind if isinstance(kind, str) else None

    def column(self, wanted: Any, role: str) -> str:
        """按名字认一列（不分大小写），认出的是快照里的写法。不存在、只差大小写的好几列、遮罩列都是调用错误。"""
        if not isinstance(wanted, str) or not wanted.strip():
            raise CallError("missing_column", f"没写{role}：写 {self.ref} 里的列名")
        name = wanted.strip()
        hits = [c for c in self.columns if c == name] or [c for c in self.columns if c.lower() == name.lower()]
        if not hits:
            raise CallError("unknown_column", f"{self.ref} 里没有「{name}」这一列，{role}要写它的列名：{self.ref} 的列有 "
                                              f"{_listed(self.columns)}")
        if len(hits) > 1:
            raise CallError("ambiguous_column", f"{self.ref} 里有好几列都叫「{name}」（只差大小写）：{role}写出确切的列名")
        column = hits[0]
        if column.lower() in self.masked:
            raise CallError("masked_column", f"「{column}」是遮罩列，不能当{role}：换一列，或者在 SQL 里不选它")
        if column not in self.named:
            self.named.append(column)
        return column

    def index(self, column: str) -> int:
        return self.columns.index(column)

    def cell(self, row: int, column: str) -> Any:
        r = self.rows[row]
        i = self.index(column)
        return r[i] if i < len(r) else None

    def number(self, row: int, column: str, role: str) -> Fraction | None:
        """一格换成精确的数；空值为 None。不是数是调用错误。"""
        raw = self.cell(row, column)
        if raw is None:
            return None
        if isinstance(raw, bool) or (isinstance(raw, str) and self.kind(column) == "text"):
            raise self._not_numeric(row, column, raw, role)
        if isinstance(raw, str):
            from app.engine.expressions import numeric_text

            if numeric_text(raw.strip()) is None:
                raise self._not_numeric(row, column, raw, role)
        value = to_fraction(raw)
        if value is None:
            raise self._not_numeric(row, column, raw, role)
        return value

    def _not_numeric(self, row: int, column: str, raw: Any, role: str) -> CallError:
        shown = str(raw).replace("\n", " ")[:20]
        return CallError("not_numeric", f"{self.ref} 的「{column}」不是数值列（第 {row} 行是「{shown}」）：{role}要选数值列")


class Inputs:
    """按名字取输入，记下结论的 inputs（同一份快照、同一格只记一次，按用到的先后）。"""

    def __init__(self, resolve: Callable[[str], InputSnapshot | None],
                 dialect: Callable[[str, Mapping[str, Any]], str | None] | None = None) -> None:
        self._resolve = resolve
        self._dialect = dialect
        self._snaps: dict[str, Snap] = {}
        self._roles: dict[str, str] = {}
        self.records: list[AnalysisInput] = []

    def snap(self, name: Any, what: str, *, role: str = "data") -> Snap:
        """按名字取一份快照。what 是调用错误里的叫法；role 是这份快照在结论里的角色（第一次取时的为准，settle 用）。"""
        if not isinstance(name, str) or not name.strip():
            raise CallError("missing_query", f"没写{what}：写本节点查过的查询编号（如 Q3）")
        key = name.strip()
        if key not in self._snaps:
            found = self._resolve(key)
            if not isinstance(found, Mapping):
                raise CallError("unknown_query", f"没有叫「{key}」的查询结果：{what}写本节点查过的查询编号（如 Q3）")
            self._snaps[key] = Snap(key, found, self._dialect(key, found) if self._dialect else found.get("dialect"))
            self._roles[key] = role
        return self._snaps[key]

    def cell(self, ref: Any, role: str, label: str) -> tuple[Snap, int, str, Fraction | None]:
        """「名字.r行.列」→ (快照, 行, 列, 值)。"""
        found = _CELL.match(ref.strip()) if isinstance(ref, str) else None
        if found is None:
            raise CallError("bad_cell", f"{label}要写成单元格「编号.r行.列」（如 Q1.r0.gmv），现在是「{ref}」")
        snap = self.snap(found.group("name"), label, role=role)
        row = int(found.group("row"))
        if row >= len(snap.rows):
            raise CallError("row_out_of_range", f"{snap.ref} 没有第 {row} 行（共 {len(snap.rows)} 行，从 r0 数起）：改{label}",
                            reason=f"{label}那一格（{ref.strip()}）不在：{snap.ref} 这次只有 {len(snap.rows)} 行")
        column = snap.column(found.group("column"), label)
        value = snap.number(row, column, label)
        self.record({"ref": f"{snap.ref}.r{row}.{column}", "role": role, "row": row, "column": column,
                     "value": canon(value)}, snap)
        return snap, row, column, value

    def use(self, snap: Snap, role: str, columns: Sequence[str], rows: Sequence[int] | None = None) -> None:
        """记下一份快照的用法：用了哪几列、哪几行（全用了写 all）。"""
        used = sorted(set(rows)) if rows is not None else None
        shown: Any = "all" if used is None or used == list(range(len(snap.rows))) else used
        self.record({"ref": snap.ref, "role": role, "columns": list(dict.fromkeys(columns)), "rows": shown}, snap)

    def record(self, entry: dict[str, Any], snap: Snap) -> None:
        out: dict[str, Any] = {"ref": entry["ref"]}
        if snap.artifact:
            out["artifact"] = snap.artifact
        if snap.dialect:
            out["dialect"] = snap.dialect
        if snap.masked:
            out["masked"] = sorted(snap.masked)       # 只记列名（复算按它认遮罩列，快照工件里不一定都有）
        out.update({k: v for k, v in entry.items() if k != "ref"})
        if out not in self.records:
            self.records.append(out)  # type: ignore[arg-type]

    def parent(self, name: str, artifact: str) -> None:
        entry: dict[str, Any] = {"ref": name, "artifact": artifact, "role": "parent"}
        if entry not in self.records:
            self.records.append(entry)  # type: ignore[arg-type]

    def settle(self) -> None:
        """调用在算出结果之前就算不了时（随数据变化的调用错误记成算不了的结论）：取过、还没记下用法的快照照样记一条（角色照
        第一次取时的，列是调用里认过的那几列，行记 all），复算才按名字取得回它们、走到同一处。"""
        recorded = set()
        for item in self.records:
            cell = _CELL.match(str(item.get("ref") or "")) if "row" in item else None
            recorded.add(cell.group("name") if cell else item.get("ref"))
        for key, snap in self._snaps.items():
            if key not in recorded:
                self.record({"ref": key, "role": self._roles.get(key, "data"), "columns": list(snap.named),
                             "rows": "all"}, snap)


def as_list(value: Any) -> list[Any]:
    """参数里一个值或一组值：都规范成列表（None 是空列表）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


__all__ = ["Inputs", "Snap", "as_list", "hashable", "same_value"]
