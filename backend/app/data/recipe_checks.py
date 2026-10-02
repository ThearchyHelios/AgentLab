"""配方导入的核对（P2-SPEC 5.2）：在执行器写好的临时库上用 SQL 重算，得出「通过 / 不一致 / 无法核对」。

为什么算术放在这里、而且只用 SQL：执行器只搬数、记来历，不做加法（H1）。核对用库里真正存下的值重算，
SQL 原文和参数原样进 CheckResult、进导入清单，复核的人拿同一条 SQL 在同一个库上能得到同一个数。

三种状态要分清（final.md 5 节、critic C6）：
- 「通过」只在真的比上了、而且相等时给；
- 明细没铺满、合计没缓存、合计格是空的、明细里有空值、有隐藏行或分类汇总函数——这些情况下合计
  「等于什么」本身说不准，判「无法核对」（可写理由接受），绝不退回「通过」，也不硬判「不一致」；
- 「不一致」只在比得上、确实不等、又没有上面那些口径疑点时给。

各项核对的编号（K / G / T 按出现顺序编号，R 沿用配方）由 plan_checks 一处决定：说明生成
（recipe_notes）要按编号找回某个分段的核对结果，两边必须是同一套编号。
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.data.engine import open_checked_sqlite
from app.data.recipe_types import (
    CheckResult,
    ColumnOut,
    CrosstabBlock,
    DerivedItem,
    DerivedSegment,
    Extraction,
    ListBlock,
    NotComparable,
    Recipe,
    SumEq,
    derive_tables,
)

#: 无法核对的原因（契约 CheckResult.reasons 的键）的固定顺序：细节、说明都按这个顺序列
REASON_ORDER = ("tiling", "uncached", "blank", "null_detail", "hidden", "unrecognized")

#: 无法核对的原因 → 给人看的说法（交叉表合计、公式引用）
REASON_TEXT = {
    "tiling": "明细时段没有恰好铺满合计的范围",
    "uncached": "文件未经计算保存",
    "blank": "合计格为空",
    "null_detail": "明细含无数据占位符",
    "hidden": "存在隐藏行或使用了分类汇总函数，合计口径无法确定",
    "unrecognized": "公式写法无法识别",
}
#: 列表合计行的说法：明细空值来自空格或占位符，不只是占位符；「没铺满」在列表上是合计行之上一行明细都没有
_LIST_REASON_TEXT = {**REASON_TEXT, "null_detail": "明细含空值", "tiling": "合计行之上没有明细行"}

#: 合计公式是这两种函数时，不等只能判「无法核对」：SUBTOTAL(109) 本来就不算隐藏行，SUM 却算
_SUBTOTAL_FUNCS = frozenset({"SUBTOTAL", "AGGREGATE"})
#: 细节最多逐条列出这么多格（其余写一句「另有 N 格」）；cells 同样封顶
_DETAIL_MAX = 20

_FULL_CALC_NOTE = "工作簿设置了打开时重算，保存值与重算一致"
_A1 = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)$")


# ---------------------------------------------------------------- 编号


@dataclass
class CheckPlan:
    """K / G / T 的编号与各自要核对的合计格。run_checks 和 recipe_notes 共用。"""

    #: derived 分段键 → K 编号（每个有合计格的 derived 段一条）
    k: dict[str, str] = field(default_factory=dict)
    #: derived 分段键 → G 编号（只给有公式格的 derived 段）
    g: dict[str, str] = field(default_factory=dict)
    #: 列表块键 → T 编号（每个有合计行的列表块一条）
    t: dict[str, str] = field(default_factory=dict)
    items: dict[str, list[DerivedItem]] = field(default_factory=dict)


def _recipe_keys(recipe: Recipe) -> tuple[list[str], list[str]]:
    """配方里 derived 分段键、带合计行的列表块键，按配方里的先后。"""
    derived: list[str] = []
    totals: list[str] = []
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, CrosstabBlock):
                derived += [s.id for s in block.segments if isinstance(s, DerivedSegment)]
            elif block.rows.total_row is not None:
                totals.append(block.id)
    return derived, totals


def _ordered(recipe_keys: list[str], items: list[DerivedItem]) -> list[str]:
    """先按配方里的先后；配方里找不到的分段（不该出现）按执行结果里第一次出现的先后排在后面，不丢。"""
    present: list[str] = []
    for it in items:
        if it.segment not in present:
            present.append(it.segment)
    known = set(recipe_keys)
    return [k for k in recipe_keys if k in present] + [k for k in present if k not in known]


def plan_checks(recipe: Recipe, extraction: Extraction) -> CheckPlan:
    """按配方顺序给 K / G / T 编号。同一份配方和执行结果永远得到同一套编号。"""
    derived_keys, total_keys = _recipe_keys(recipe)
    sums = [d for d in extraction.derived if d.kind == "label_range_sum"]
    cols = [d for d in extraction.derived if d.kind == "column_sum"]
    plan = CheckPlan()
    for key in _ordered(derived_keys, sums):
        plan.items[key] = [d for d in sums if d.segment == key]
        plan.k[key] = f"K{len(plan.k) + 1}"
        if any(d.is_formula for d in plan.items[key]):
            plan.g[key] = f"G{len(plan.g) + 1}"
    for key in _ordered(total_keys, cols):
        plan.items[key] = [d for d in cols if d.segment == key]
        plan.t[key] = f"T{len(plan.t) + 1}"
    return plan


# ---------------------------------------------------------------- 小工具


def _q(name: str) -> str:
    """SQL 标识符：一律双引号、内部双引号加倍（库是 DQS 关闭的连接，写错的名字会直接报错）。"""
    return '"' + str(name).replace('"', '""') + '"'


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letters(index: int) -> str:
    out = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def split_coord(coord: str) -> tuple[str | None, int, int] | None:
    """「工作表!C5」或「C5」→ (工作表或 None, 列号, 行号)；认不出返回 None。"""
    sheet, _, ref = str(coord).rpartition("!")
    m = _A1.match(ref.strip())
    if not m:
        return None
    return (sheet or None), col_index(m.group(1)), int(m.group(2))


def lineage_cell(lineage: dict[str, dict[str, list[list[Any]]]], table: str, column: str,
                 rowid: int) -> str | None:
    """按溯源段把 (表, 列, rowid) 反查成「工作表!A1」。溯源段：[起始 rowid, 工作表, 起始格, 个数, 方向]。"""
    for run in (lineage.get(table) or {}).get(column) or []:
        try:
            start, sheet, cell, count, direction = run[0], run[1], run[2], run[3], run[4]
        except (IndexError, TypeError):
            continue
        if not (start <= rowid < start + count):
            continue
        parsed = split_coord(cell)
        if parsed is None:
            return None
        _, c, r = parsed
        offset = rowid - start
        if direction == "down":
            r += offset
        else:
            c += offset
        return f"{sheet}!{col_letters(c)}{r}"
    return None


def _short(item: DerivedItem) -> str:
    """细节里写的格子：不带工作表名（「G28」），工作表在 cells 里。"""
    return str(item.cell).rpartition("!")[2]


def _coord(item: DerivedItem) -> str:
    return item.cell if "!" in str(item.cell) else f"{item.sheet}!{item.cell}"


def _fmt(x: Any) -> str:
    if x is None:
        return "空"
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, float):
        if math.isfinite(x) and x.is_integer() and abs(x) < 1e15:
            return str(int(x))
        return format(x, ".10g")
    return str(x)


def _is_int_value(x: Any) -> bool:
    return isinstance(x, int) or (isinstance(x, float) and math.isfinite(x) and x.is_integer())


def same_number(a: Any, b: Any) -> bool:
    """两边都是整数值时精确相等，否则相对容差 1e-9（P2-SPEC 5.2 的比较规则）。"""
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float)) \
            or not isinstance(b, (int, float)):
        return False
    if _is_int_value(a) and _is_int_value(b):
        return int(a) == int(b)
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _range_text(item: DerivedItem) -> str:
    return f"{item.start}-{item.end} 时"


class _Broken(Exception):
    """合计格缺了核对必需的信息（执行器的错）：这一条核对判结构类不一致，不硬算。"""


class _Sql:
    """执行 SQL 并记下最后一条：出错时 CheckResult 里也要有那条 SQL 原文。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.last: tuple[str | None, list[Any]] = (None, [])

    def one(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> tuple[Any, ...]:
        self.last = (sql, list(params))
        return self.conn.execute(sql, list(params)).fetchone()

    def all(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        self.last = (sql, list(params))
        return self.conn.execute(sql, list(params)).fetchall()


@dataclass
class _Tally:
    """逐格判定的汇总：K、T、G 共用。"""

    checked: int = 0
    mismatches: list[tuple[str, str]] = field(default_factory=list)   # (格, 细节)
    reasons: Counter = field(default_factory=Counter)
    unverifiable_cells: list[str] = field(default_factory=list)
    extra: dict[str, list[str]] = field(default_factory=dict)          # 原因 → 附加说明（如没铺满的区间）
    sql: str | None = None
    params: list[Any] = field(default_factory=list)
    _sql_rank: int = 3

    def keep_sql(self, sql: str, params: list[Any], rank: int) -> None:
        # 代表性的 SQL：优先取第一处不一致，其次第一处无法核对，再次第一格
        if rank < self._sql_rank:
            self.sql, self.params, self._sql_rank = sql, list(params), rank

    def ok(self) -> None:
        self.checked += 1

    def bad(self, cell: str, detail: str) -> None:
        self.checked += 1
        self.mismatches.append((cell, detail))

    def unverifiable(self, cell: str, reason: str, note: str | None = None) -> None:
        self.reasons[reason] += 1
        self.unverifiable_cells.append(cell)
        if note and note not in self.extra.setdefault(reason, []):
            self.extra[reason].append(note)


def _finish(cid: str, kind: Any, title: str, tally: _Tally, *, reason_text: dict[str, str],
            full_calc: bool = False) -> CheckResult:
    unverifiable = sum(tally.reasons.values())
    details: list[str] = [d for _, d in tally.mismatches[:_DETAIL_MAX]]
    if len(tally.mismatches) > _DETAIL_MAX:
        details.append(f"另有 {len(tally.mismatches) - _DETAIL_MAX} 格不一致")
    for reason in REASON_ORDER + tuple(r for r in tally.reasons if r not in REASON_ORDER):
        n = tally.reasons.get(reason, 0)
        if not n:
            continue
        text = reason_text.get(reason, reason)
        if reason == "tiling" and tally.extra.get("tiling"):
            text = "明细时段没有恰好铺满 " + "、".join(tally.extra["tiling"])
        details.append(f"{n} 格无法核对：{text}")
    if tally.mismatches:
        status = "mismatch"
    elif unverifiable:
        status = "unverifiable"
    else:
        status = "passed"
        details.append(f"{tally.checked} 格全部一致")
        if full_calc:
            details.append(_FULL_CALC_NOTE)
    cells = [c for c, _ in tally.mismatches] + tally.unverifiable_cells
    # category 一律 structure：不一致是结构类（拒收），无法核对时 acceptable=True、可写理由接受（进 waivers）。
    # 所以判「拒收」要看 status != passed 且 category == structure 且 acceptable 为假，不能只看 category
    return CheckResult(
        id=cid, kind=kind, title=title, status=status, category="structure",
        checked=tally.checked, failed=len(tally.mismatches), unverifiable=unverifiable,
        sql=tally.sql, params=tally.params, details=details, cells=cells[:_DETAIL_MAX],
        acceptable=status == "unverifiable",
        reasons={r: n for r, n in tally.reasons.items() if n},
    )


# ---------------------------------------------------------------- K：合计按标签区间重算


def _require_range(item: DerivedItem) -> None:
    if item.start is None or item.end is None or not item.start_col or not item.end_col \
            or not item.base_table or not item.base_value:
        raise _Broken(f"{_short(item)}：合计格缺少核对需要的区间或基表信息")


def _range_where(item: DerivedItem, filters: Iterable[tuple[str, Any]]) -> tuple[str, list[Any]]:
    """WHERE 各分组列 = ? AND 起始列 >= ? AND 结束列 <= ?，以及对应的参数。"""
    filters = list(filters)
    where = [f"{_q(k)} = ?" for k, _ in filters] + [f"{_q(item.start_col)} >= ?", f"{_q(item.end_col)} <= ?"]
    return " AND ".join(where), [v for _, v in filters] + [item.start, item.end]


def _tiled(sql: _Sql, item: DerivedItem, filters: tuple[tuple[str, Any], ...]) -> bool:
    """区间 [start, end] 里的明细时段是否恰好铺满：首尾对上、相邻首尾相接、不重叠。"""
    where, params = _range_where(item, filters)
    rows = sql.all(f"SELECT DISTINCT {_q(item.start_col)}, {_q(item.end_col)} FROM {_q(item.base_table)} "
                   f"WHERE {where} ORDER BY {_q(item.start_col)}, {_q(item.end_col)}", params)
    if not rows or rows[0][0] != item.start or rows[-1][1] != item.end:
        return False
    prev_end = None
    for s, e in rows:
        if s is None or e is None or s >= e or (prev_end is not None and s != prev_end):
            return False
        prev_end = e
    return True


def _sum_sql(item: DerivedItem) -> tuple[str, list[Any]]:
    # WHERE 带上 key 的全部列（轴列 + 前一分段的常量列）：同表别的类别的行不会被加进来
    where, params = _range_where(item, item.key.items())
    return (f"SELECT TOTAL({_q(item.base_value)}), COUNT({_q(item.base_value)}), COUNT(*) "
            f"FROM {_q(item.base_table)} WHERE {where}"), params


def _judge_value(tally: _Tally, item: DerivedItem, total: float, nonnull: int, count: int,
                 sql: str, params: list[Any], *, mismatch_detail: Callable[[], str]) -> None:
    """判定顺序第 2–6 条（第 1 条铺满由调用方先判）。K 与 T 共用。"""
    cell = _coord(item)
    if item.value is None:
        tally.unverifiable(cell, "uncached" if item.is_formula else "blank")
        tally.keep_sql(sql, params, 1)
    elif nonnull < count:
        # 明细缺数时合计等于什么都说不准：不能判一致，也不能判不一致
        tally.unverifiable(cell, "null_detail")
        tally.keep_sql(sql, params, 1)
    elif same_number(item.value, total):
        tally.ok()
        tally.keep_sql(sql, params, 2)
    elif item.hidden_in_range or (item.func or "").upper() in _SUBTOTAL_FUNCS:
        tally.unverifiable(cell, "hidden")
        tally.keep_sql(sql, params, 1)
    else:
        tally.bad(cell, mismatch_detail())
        tally.keep_sql(sql, params, 0)


def _check_k(sql: _Sql, cid: str, title: str, items: list[DerivedItem], axis: str | None,
             full_calc: bool) -> CheckResult:
    tally = _Tally()
    tiling: dict[tuple[Any, ...], bool] = {}
    for item in items:
        _require_range(item)
        # 铺满只看区间和常量列（分段类别），不看日期：长表里每个日期的时段行是同一组
        filters = tuple((k, v) for k, v in item.key.items() if k != axis)
        tkey = (item.base_table, item.start_col, item.end_col, item.start, item.end, filters)
        if tkey not in tiling:
            tiling[tkey] = _tiled(sql, item, filters)
        q, params = _sum_sql(item)
        if not tiling[tkey]:
            tally.unverifiable(_coord(item), "tiling", _range_text(item))
            tally.keep_sql(q, params, 1)
            continue
        total, nonnull, count = sql.one(q, params)
        if count == 0:
            # 这个日期一行明细都没有：同样是没铺满
            tally.unverifiable(_coord(item), "tiling", _range_text(item))
            tally.keep_sql(q, params, 1)
            continue
        _judge_value(tally, item, total, nonnull, count, q, params, mismatch_detail=lambda: (
            f"{_short(item)}：表内为 {_fmt(item.value)}，按 {_range_text(item)}的明细重算为 {_fmt(total)}"))
    return _finish(cid, "derived_sum", title, tally, reason_text=REASON_TEXT, full_calc=full_calc)


# ---------------------------------------------------------------- G：公式引用与标签一致


def _check_g(sql: _Sql, cid: str, title: str, items: list[DerivedItem]) -> CheckResult:
    tally = _Tally()
    for item in items:
        if not item.is_formula:
            continue
        cell = _coord(item)
        problem = item.ref_problem
        if problem == "outside":
            tally.bad(cell, f"{_short(item)}：公式引用了不在「{item.base_table}」明细里的格子")
            continue
        if problem == "hidden" or (problem is None and (item.func or "").upper() in _SUBTOTAL_FUNCS):
            tally.unverifiable(cell, "hidden")
            continue
        if problem is not None or item.ref_rowids is None:
            tally.unverifiable(cell, "unrecognized")
            continue
        _require_range(item)
        where, params = _range_where(item, item.key.items())
        q = f"SELECT rowid FROM {_q(item.base_table)} WHERE {where}"
        got = {r[0] for r in sql.all(q, params)}
        if got == set(item.ref_rowids):
            tally.ok()
            tally.keep_sql(q, params, 2)
        else:
            tally.bad(cell, f"{_short(item)}：标签「{item.label}」与公式引用的时段不一致")
            tally.keep_sql(q, params, 0)
    return _finish(cid, "formula_refs", title, tally, reason_text=REASON_TEXT)


# ---------------------------------------------------------------- T：列表合计行按列求和


def _check_t(sql: _Sql, cid: str, title: str, items: list[DerivedItem], full_calc: bool) -> CheckResult:
    tally = _Tally()
    for item in items:
        if not item.base_table or not item.base_value:
            raise _Broken(f"{_short(item)}：合计格缺少基表信息")
        first, last = item.rowid_first, item.rowid_last
        if (first is None) != (last is None):
            raise _Broken(f"{_short(item)}：合计行对应的明细行区间只有一端")
        q = (f"SELECT TOTAL({_q(item.base_value)}), COUNT({_q(item.base_value)}), COUNT(*) "
             f"FROM {_q(item.base_table)} WHERE rowid BETWEEN ? AND ?")
        params = [first, last]
        total, nonnull, count = sql.one(q, params)
        if count == 0:
            # 区间里一行明细都没有：表头下面紧接着就是合计行（执行器用两端都是 None 表示空区间，
            # BETWEEN NULL AND NULL 一行都选不到），或者区间对不上库。TOTAL 得 0，合计写 0 时会被判「一致」；
            # 没有可比的明细，判无法核对
            tally.unverifiable(_coord(item), "tiling")
            tally.keep_sql(q, params, 1)
            continue
        _judge_value(tally, item, total, nonnull, count, q, params, mismatch_detail=lambda: (
            f"{_short(item)}：表内为 {_fmt(item.value)}，按明细求和为 {_fmt(total)}"))
    return _finish(cid, "column_sum", title, tally, reason_text=_LIST_REASON_TEXT, full_calc=full_calc)


# ---------------------------------------------------------------- R：关系


def _check_sum_eq(sql: _Sql, rel: SumEq, title: str, columns: list[ColumnOut], grain: list[str],
                  lineage: dict[str, dict[str, list[list[Any]]]]) -> CheckResult:
    table, total, parts = rel.table, rel.total, list(rel.parts)
    names = [total, *parts]
    nonnull = " AND ".join(f"{_q(c)} IS NOT NULL" for c in names)
    anynull = " OR ".join(f"{_q(c)} IS NULL" for c in names)
    checked = sql.one(f"SELECT COUNT(*) FROM {_q(table)} WHERE {nonnull}")[0]
    nulls = sql.one(f"SELECT COUNT(*) FROM {_q(table)} WHERE {anynull}")[0]
    types = {c.name: c.type for c in columns}
    added = " + ".join(_q(p) for p in parts)
    if any(types.get(c) == "REAL" for c in names):
        cond = f"ABS({_q(total)} - ({added})) > 1e-9 * MAX(1, ABS({_q(total)}))"
    else:
        cond = f"{_q(total)} <> {added}"
    where = f"{nonnull} AND {cond}"
    failed = sql.one(f"SELECT COUNT(*) FROM {_q(table)} WHERE {where}")[0]
    picked = ", ".join(["rowid", *(_q(g) for g in grain), _q(total), f"({added})"])
    rows_sql = f"SELECT {picked} FROM {_q(table)} WHERE {where} ORDER BY rowid"
    rows = sql.all(rows_sql + f" LIMIT {_DETAIL_MAX}") if failed else []
    details: list[str] = []
    cells: list[str] = []
    for row in rows:
        rowid, keys, value, summed = row[0], row[1:1 + len(grain)], row[-2], row[-1]
        where_text = "、".join(f"{g} {v}" for g, v in zip(grain, keys)) or f"第 {rowid} 条记录"
        cell = lineage_cell(lineage, table, total, rowid)
        if cell:
            cells.append(cell)
        at = f"（{cell.rpartition('!')[2]}）" if cell else ""
        details.append(f"{where_text}{at}：{total} 为 {_fmt(value)}，{'、'.join(parts)} 之和为 {_fmt(summed)}")
    if failed > len(rows):
        details.append(f"另有 {failed - len(rows)} 行不成立")
    if nulls:
        details.append(f"{nulls} 行含空值，未能核对")
    if failed:
        status = "mismatch"
    elif nulls:
        status = "unverifiable"
    elif checked == 0:
        # 一行都没有核对过（表头下面没有数据行）：不能当「通过」，与 T 的空区间一样判无法核对（AU-1）。
        # 重传时文件导出出错只剩表头，上一期的数据会被整表替换成 0 行，要让人写理由接受才能提交
        status = "unverifiable"
        details.append("本期这张表没有数据行，关系未能核对")
    else:
        status = "passed"
        details.append(f"{checked} 行全部成立")
    return CheckResult(
        id=rel.id, kind="relation_sum_eq", title=title, status=status, category="data_quality",
        checked=checked, failed=failed, unverifiable=nulls, sql=rows_sql, params=[],
        details=details, cells=cells, acceptable=status != "passed",
    )


def _check_not_comparable(sql: _Sql, rel: NotComparable, title: str, tables: dict[str, list[ColumnOut]],
                          grains: dict[str, list[str]], date_cols: dict[str, set[str]]) -> CheckResult:
    a, b, by = rel.a, rel.b, rel.by
    inner = (f"SELECT {_q(by)}, TOTAL({_q(a.value)}) AS __sum FROM {_q(a.table)} GROUP BY {_q(by)}")
    joined = f"SELECT COUNT(*) FROM ({inner}) a JOIN {_q(b.table)} b ON a.{_q(by)} = b.{_q(by)}"
    groups = sql.one(joined)[0]
    b_type = next((c.type for c in tables.get(b.table, []) if c.name == b.value), "INTEGER")
    if b_type == "REAL":
        cond = f"ABS(a.__sum - b.{_q(b.value)}) <= 1e-9 * MAX(1, ABS(b.{_q(b.value)}))"
    else:
        cond = f"a.__sum = b.{_q(b.value)}"
    equal_sql = f"{joined} WHERE {cond}"
    equal = sql.one(equal_sql)[0]
    dims = [g for g in grains.get(a.table, []) if g != by]
    unit = "天" if by in date_cols.get(a.table, set()) else "组"
    head = f"各{'、'.join(dims)}之和与{b.value}" if dims else f"{a.value}按{by}汇总与{b.value}"
    if groups and equal == groups:
        # 声明了口径不同、本期却处处相等：说明换不含「不等于」的写法，确认项要人看一眼（7.5）
        detail = f"{head}：{groups} {unit}全部相等"
    else:
        detail = f"{head}：{groups} {unit}中 {equal} {unit}相等"
    return CheckResult(
        id=rel.id, kind="relation_not_comparable", title=title,
        status="info", category="info", checked=groups, failed=groups - equal,
        sql=equal_sql, params=[], details=[detail],
    )


# ---------------------------------------------------------------- N、P：落库


def _check_rows(sql: _Sql, cid: str, title: str, table: str, expected: int | None) -> CheckResult:
    q = f"SELECT COUNT(*) FROM {_q(table)}"
    n = sql.one(q)[0]
    if expected is None:
        status, detail = "mismatch", f"缺少按格子账推出的期望行数，库里 {n} 行"
    elif n != expected:
        status, detail = "mismatch", f"库里 {n} 行，按格子账应有 {expected} 行"
    else:
        status, detail = "passed", f"{n} 行，与格子账一致"
    return CheckResult(id=cid, kind="row_count", title=title, status=status, category="structure",
                       checked=1, failed=int(status != "passed"), sql=q, params=[], details=[detail])


def _check_pk(sql: _Sql, cid: str, title: str, table: str, grain: list[str]) -> CheckResult:
    q = (f"SELECT COUNT(*) FROM (SELECT 1 FROM {_q(table)} GROUP BY {', '.join(_q(g) for g in grain)} "
         f"HAVING COUNT(*) > 1)")
    dup = sql.one(q)[0]
    detail = f"{dup} 组主键重复" if dup else "主键没有重复"
    return CheckResult(id=cid, kind="pk_unique", title=title, status="passed" if not dup else "mismatch",
                       category="structure", checked=1, failed=int(bool(dup)), sql=q, params=[], details=[detail])


# ---------------------------------------------------------------- 入口


def _axis_names(recipe: Recipe) -> dict[str, str]:
    """derived 分段键 → 所在交叉表块的轴列名（K 的铺满判定不按日期分）。"""
    out: dict[str, str] = {}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, CrosstabBlock):
                for seg in block.segments:
                    if isinstance(seg, DerivedSegment):
                        out[seg.id] = block.axis.name
    return out


def date_columns(recipe: Recipe) -> dict[str, set[str]]:
    """表 → 日期列（交叉表的轴列、列表里 type=DATE 的列）。「逐日」「天」的措辞按它定。"""
    tables, _ = derive_tables(recipe)
    out: dict[str, set[str]] = {t: {c.name for c in cols if c.role == "axis"} for t, cols in tables.items()}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, ListBlock):
                out.setdefault(block.table, set()).update(c.name for c in block.columns if c.type == "DATE")
    return out


def _guarded(sql: _Sql, cid: str, kind: Any, title: str,
             run: Callable[[str], CheckResult]) -> CheckResult:
    """核对 SQL 出错（表或列不存在）或合计格缺信息，都是执行器或配方的错：判结构类不一致，不让整次核对崩掉。"""
    sql.last = (None, [])
    try:
        return run(title)
    except (sqlite3.Error, _Broken) as e:
        text, params = sql.last
        reason = str(e) if isinstance(e, _Broken) else f"核对 SQL 执行失败：{e}"
        return CheckResult(id=cid, kind=kind, title=title, status="mismatch", category="structure",
                           sql=text, params=params, details=[reason])


def run_checks(db_path: str, extraction: Extraction, recipe: Recipe) -> list[CheckResult]:
    """在临时库上跑全部核对。同步（调用方放进线程池）。

    结果 = extraction.context_checks（C1、C2 原样排在最前）+ K… + G… + T… + R…（配方顺序）+ N… + P…，
    顺序只取决于配方和执行结果。库以只读、DQS 关闭的连接打开（engine.open_checked_sqlite）。
    """
    plan = plan_checks(recipe, extraction)
    tables, _ = derive_tables(recipe)
    grains = {t.name: list(t.grain) for t in recipe.tables}
    axis = _axis_names(recipe)
    dates = date_columns(recipe)
    full_calc = extraction.full_calc_on_load
    conn = open_checked_sqlite(db_path, readonly=True)
    sql = _Sql(conn)
    # (编号, 种类, 标题, 核对函数)：标题在这里定一次，出错时的那条结果也用它
    jobs: list[tuple[str, Any, str, Callable[[str], CheckResult]]] = []
    for seg, cid in plan.k.items():
        jobs.append((cid, "derived_sum", f"合计按明细重算（分段「{seg}」）",
                     lambda t, seg=seg, cid=cid: _check_k(sql, cid, t, plan.items[seg], axis.get(seg), full_calc)))
    for seg, cid in plan.g.items():
        jobs.append((cid, "formula_refs", f"合计公式引用（分段「{seg}」）",
                     lambda t, seg=seg, cid=cid: _check_g(sql, cid, t, plan.items[seg])))
    for block, cid in plan.t.items():
        jobs.append((cid, "column_sum", f"合计行按列求和（块「{block}」）",
                     lambda t, block=block, cid=cid: _check_t(sql, cid, t, plan.items[block], full_calc)))
    for rel in recipe.relations:
        if isinstance(rel, SumEq):
            jobs.append((rel.id, "relation_sum_eq", f"「{rel.table}」：{rel.total} = {' + '.join(rel.parts)}",
                         lambda t, rel=rel: _check_sum_eq(sql, rel, t, tables.get(rel.table, []),
                                                          grains.get(rel.table, []), extraction.lineage)))
        elif isinstance(rel, NotComparable):
            jobs.append((rel.id, "relation_not_comparable", f"口径不同：「{rel.a.table}」与「{rel.b.table}」",
                         lambda t, rel=rel: _check_not_comparable(sql, rel, t, tables, grains, dates)))
    for i, table in enumerate(tables, 1):
        jobs.append((f"N{i}", "row_count", f"行数：「{table}」",
                     lambda t, table=table, i=i: _check_rows(sql, f"N{i}", t, table,
                                                             extraction.expected_rows.get(table))))
    for i, table in enumerate([t for t in tables if grains.get(t)], 1):
        jobs.append((f"P{i}", "pk_unique", f"主键唯一：「{table}」",
                     lambda t, table=table, i=i: _check_pk(sql, f"P{i}", t, table, grains[table])))
    try:
        return list(extraction.context_checks) + [_guarded(sql, *job) for job in jobs]
    finally:
        conn.close()


def column_null_counts(db_path: str, recipe: Recipe) -> dict[str, dict[str, int]]:
    """表 → 列 → 空值行数。给 recipe_notes.build_notes 的 null_counts：只有本期真有空值的值列才写空值说明。

    说明生成拿不到库（契约签名里没有库路径），Extraction 里也没有按列的空值来源，所以单独查一次。
    """
    tables, _ = derive_tables(recipe)
    out: dict[str, dict[str, int]] = {}
    conn = open_checked_sqlite(db_path, readonly=True)
    try:
        for table, cols in tables.items():
            names = [c.name for c in cols]
            row = conn.execute(
                f"SELECT COUNT(*), {', '.join(f'COUNT({_q(n)})' for n in names)} FROM {_q(table)}").fetchone()
            out[table] = {n: row[0] - row[i] for i, n in enumerate(names, 1)}
    finally:
        conn.close()
    return out
