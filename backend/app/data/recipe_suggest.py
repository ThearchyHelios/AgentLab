"""确定性起草（期 2，WP-4）：从扫描事实和网格推出一份配方草稿、系统发现的关系、建议卡片和待确认问题。

**所有产物只是草稿**（H3）。起草器只读格子、不做任何判定性的导入：它写出的配方要经静态校验（注入的
validate）和执行器干跑（注入的 dry_run），再由人逐条确认之后才能启用。所以这里宁可认不出就说认不出
（complete=False，failures 写人话原因），也不猜。

**为什么 detect_facts 只给关系不给数值。** 「第 5 行 = 第 6 行 + 第 7 行」这种事实要交给用户确认、交给 AI
起草参考；数值本身不需要离开表格，系统也不该替用户判断某个数「对不对」——对不对由每期的 SQL 核对说了算。

**为什么 questions_for 吃任意配方。** 规则草稿、AI 草稿、用户在面板里改过的配方都要给同一套卡片和问题，
接口层每次回答、PUT、AI 草稿之后都重算一遍。给了最近一次干跑（extraction）时，区域类卡片（统计期取自哪格、
哪些是区域外文字、哪些格没有去处）直接用执行器的结论：同一件事只有执行器一套判法，这里自己定位只是没有
干跑时的近似。

**问题的 effects 怎么写。** 每个选项是一组 JSON Patch，路径按传进来的配方写；接口层从 answers_base 起、按
问题顺序重放全部回答（P2-SPEC 7.2）。因此：
- 关系的两个选项都用整条 replace（「登记」写回完整的 sum_eq / not_comparable，「不登记」写 dismissed），
  与当前状态无关，先选 A 再改选 B 等于直接选 B；
- 占位符的「不是」用 remove，同一块里有多个占位符时问题按下标从大到小排列，按问题顺序重放不会错位；
- 改某个字段用 add（对象成员上 add 等于覆盖），父对象缺失时（传进来的是去掉默认值的紧凑形式）才连父对象
  一起建。起草器自己产出的配方一律是补全默认值的完整形式，父对象都在。
"""
from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import inspect
import itertools
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from app.data import recipe_parsers as P
from app.data.names import canon, collide_key, to_sql_name
from app.data.recipe_types import (
    RECIPE_FORMAT,
    REASON_SLOT,
    Card,
    CrosstabBlock,
    DerivedSegment,
    DimensionSegment,
    Draft,
    DraftFacts,
    Extraction,
    Fact,
    Grid,
    GridCell,
    ListBlock,
    MeasuresSegment,
    Problem,
    Question,
    NotComparable,
    Recipe,
    RecipeProblem,
    SumEq,
    accumulate_blockers,
    derive_tables,
)
from app.data.tabular import DATE_HEADER_MIN, EXCEL_ERRORS
from app.data.xlsx_scan import WorkbookScan, cell_ref, col_letter, parse_ref

logger = logging.getLogger(__name__)

ValidateFn = Callable[[dict[str, Any], DraftFacts | None, str], tuple[Recipe | None, list[RecipeProblem]]]
#: 第三个参数是 origin（"rules" / "ai"）：draft 传 "rules"，ai_draft 传 "ai"——ai_note_forbidden 靠它触发
DryRunFn = Callable[[Recipe], Extraction]
#: AI 起草（recipe_ai.ai_draft）用的干跑可以是协程函数：WP-5c 注入的实现占一个解析名额、在线程池里跑执行器。
#: 一次干跑可能要几秒，绝不能同步跑在事件循环上（会堵住同进程的所有请求）；见 run_dry_async
AsyncDryRunFn = Callable[[Recipe], Awaitable[Extraction]]
#: WP-5c 注入：网格路径的工作表完整干跑，超过 GRID_MAX_CELLS 的工作表 execute(max_rows=500) 部分干跑（4.9）

#: 起草时每张工作表最多读多少行（接口层 read_grid 的 max_rows）
DRAFT_MAX_ROWS = 500
#: 列表的第一个表头只在前这么多行里找（P2-SPEC 6.1 第 11 条）
LIST_HEADER_SCAN_ROWS = 50
#: 卡片最多指向多少个格子（界面着色用，再多也看不过来）
CARD_CELLS_MAX = 40
#: sum_eq 事实：合计行等于另外几行之和，最多几行
FACT_PARTS_MIN, FACT_PARTS_MAX = 2, 4
#: 组合数会爆炸：指标段超过这么多行时只找「两行之和」，超过 FACT_SEARCH_MAX_ROWS 行时不找 sum_eq
#: （指标段一般只有几行；几十行的指标段多半是认错了版式，起草本来也不会完整）
FACT_DEEP_MAX_ROWS = 20
FACT_SEARCH_MAX_ROWS = 60
#: 回灌给模型的问题最多几条（P2-SPEC 6.2）
MODEL_LINES_MAX = 20

#: 给模型看的问题行（起草失败原因、AI 修订回灌）：静态校验 = 路径 + 消息；干跑 = code + model_message + 坐标
MODEL_LINE_PROMPT = {"static": "{path}：{message}", "dry": "{code}", "dry_msg": "{code}：{message}",
                     "cells": "{line}（{cells}）"}

_HOUR_DIM, _TEXT_DIM = "时段", "项目"
_DERIVE = {"起始小时": "start", "结束小时": "end"}
_TOTAL_HINT = ("合计", "小计", "总计")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_ENUM_PREFIX = re.compile(r"^[（(]?[一二三四五六七八九十]+[)）]?[、.．\s]*")


# ==========================================================================
# 小工具
# ==========================================================================


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int_like(v: Any) -> bool:
    return isinstance(v, int) or (isinstance(v, float) and v.is_integer())


def _text(v: Any) -> str | None:
    """格子里的文字（去首尾空白）；不是文字或只有空白时 None。"""
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def _shown(v: Any) -> str:
    """界面上显示一个格子的值（只给人看）。"""
    if isinstance(v, _dt.datetime):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat(sep=" ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    return str(v).strip()


def _placeholder_like(text: str) -> bool:
    """占位符候选：canon 后不超过 2 个字、没有字母数字、不像数（「·」「-」「/」「—」）。"""
    s = canon(text)
    return 0 < len(s) <= 2 and not any(ch.isalnum() for ch in s) and not P.looks_numeric_text(s)


def _is_error(v: Any) -> bool:
    return isinstance(v, str) and v.strip().upper() in EXCEL_ERRORS


def _total_word(text: Any) -> str | None:
    """列表合计行的标签：match_key 后以 TOTAL_WORDS 之一开头时返回那个词。"""
    if not isinstance(text, str):
        return None
    key = P.match_key(text)
    return next((w for w in P.TOTAL_WORDS if key.startswith(w)), None)


def _a1(r: int, c: int) -> str:
    return cell_ref(r, c)


def _coord(sheet: str, r: int, c: int) -> str:
    return f"{sheet}!{cell_ref(r, c)}"


def _coords(sheet: str, cells: list[tuple[int, int]]) -> list[str]:
    return [_coord(sheet, r, c) for r, c in sorted(set(cells))[:CARD_CELLS_MAX]]


def _rows_text(rows: list[int]) -> str:
    rows = sorted(set(rows))
    if not rows:
        return ""
    if len(rows) == 1:
        return f"第 {rows[0]} 行"
    if rows[-1] - rows[0] + 1 == len(rows):
        return f"第 {rows[0]}–{rows[-1]} 行"
    return "第 " + "、".join(str(r) for r in rows[:8]) + (" 等" if len(rows) > 8 else "") + " 行"


def _a1_list(cells: list[tuple[int, int]], limit: int = 5) -> str:
    cells = sorted(set(cells))
    head = "、".join(_a1(r, c) for r, c in cells[:limit])
    return head + (f" 等 {len(cells)} 格" if len(cells) > limit else "")


def _ptr(*parts: Any) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _bottom(grid: Grid) -> int:
    rows = grid.row_numbers()
    return rows[-1] if rows else 0


def _cell_kind(cell: GridCell | None) -> str:
    """数值区里的一格：blank / formula / num / ph（占位符候选）/ error / text / other。"""
    if cell is None:
        return "blank"
    if cell.formula is not None:
        return "formula"
    v = cell.value
    if _num(v):
        return "num"
    if isinstance(v, str):
        if _is_error(v):
            return "error"
        return "ph" if _placeholder_like(v) else "text"
    return "other"


def _value_num(cell: GridCell | None) -> int | float | None:
    """格子的数（公式取保存值）；不是数时 None。"""
    if cell is None:
        return None
    return cell.value if _num(cell.value) else None


def _equal(a: float, b: float) -> bool:
    if _int_like(a) and _int_like(b):
        return a == b
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _unique(name: str, used: set[str], *, limit: int = 48) -> str:
    """名字撞了（collide_key）就加 _2、_3……；超长截断。"""
    base = name[:limit]
    out, n = base, 2
    while collide_key(out) in used:
        tail = f"_{n}"
        out = base[: limit - len(tail)] + tail
        n += 1
    used.add(collide_key(out))
    return out


# ==========================================================================
# 版式分析（起草和压缩表示共用）
# ==========================================================================


def _axis_row(grid: Grid) -> tuple[int, list[int]] | None:
    """日期表头行：至少 DATE_HEADER_MIN 格能被 month_day_or_date 解析，取个数最多的那行（并列取最上面）。"""
    best: tuple[int, list[int]] | None = None
    for r in grid.row_numbers():
        cols = [c for c, cell in grid.row(r) if P.month_day_or_date(cell.value) is not None]
        if len(cols) >= DATE_HEADER_MIN and (best is None or len(cols) > len(best[1])):
            best = (r, cols)
    return best


def _header_texts(grid: Grid, rows: list[int], cols: list[int]) -> dict[int, str]:
    """多行表头每列的文字：合并区内取左上格（左上格要在这几行里），去掉空的和与上一层相同的，用「_」拼接。"""
    tops: dict[tuple[int, int], tuple[int, int]] = {}
    lo, hi = rows[0], rows[-1]
    for (r1, c1, r2, c2) in grid.merges:
        if lo <= r1 <= hi:
            for rr in range(r1, min(r2, hi) + 1):
                for cc in range(c1, c2 + 1):
                    tops[(rr, cc)] = (r1, c1)
    out: dict[int, str] = {}
    for c in cols:
        parts: list[str] = []
        for r in rows:
            src = tops.get((r, c), (r, c))
            cell = grid.get(*src)
            t = _text(cell.value) if cell is not None else None
            if t and (not parts or parts[-1] != t):
                parts.append(t)
        if parts:
            out[c] = "_".join(parts)
    return out


def _header_like(grid: Grid, r: int) -> bool:
    """表头行：≥2 个非空格、全是文本（不像数、不以冒号结尾）且互不相同，下面 3 行里至少有一行有数字。

    冒号结尾的是表单的标签（「项目名称：」），不是表头：键值表单不能被认成列表。
    """
    cells = grid.row(r)
    if len(cells) < 2:
        return False
    keys: set[str] = set()
    for _, cell in cells:
        if cell.formula is not None or not isinstance(cell.value, str):
            return False
        s = canon(cell.value)
        if not s or P.looks_numeric_text(s) or s.endswith(":"):
            return False
        k = P.match_key(s)
        if k in keys:
            return False
        keys.add(k)
    lo, hi = cells[0][0], cells[-1][0]
    return any(any(_num(cell.value) for c, cell in grid.row(rr) if lo <= c <= hi) for rr in range(r + 1, r + 4))


def _all_text_row(grid: Grid, r: int) -> bool:
    cells = grid.row(r)
    return len(cells) >= 2 and all(isinstance(cell.value, str) and cell.formula is None
                                   and not P.looks_numeric_text(cell.value) for _, cell in cells)


def _header_rows_at(grid: Grid, r: int) -> list[int]:
    """r 是表头行时，表头占哪几行：上一行（或本行）有横向合并区覆盖表头的 ≥2 列时是两行。"""
    cols = {c for c, _ in grid.row(r)}
    for (r1, c1, r2, c2) in grid.merges:
        if r1 == r2 == r - 1 and len(cols & set(range(c1, c2 + 1))) >= 2:
            return [r - 1, r]
    # 本行就是上一层（「销售」横跨「数量」「金额」），下一行是全文字的下一层
    if any(r1 == r2 == r and c2 > c1 for (r1, c1, r2, c2) in grid.merges) and _all_text_row(grid, r + 1):
        return [r, r + 1]
    return [r]


@dataclass
class LayoutHints:
    """压缩表示要知道的版式事实：哪一行是日期轴、标签列在哪、哪些行是列表的表头。"""

    axis_row: int | None = None
    label_col: int | None = None
    header_rows: set[int] = field(default_factory=set)
    top_header_row: int | None = None


def layout_hints(grid: Grid) -> LayoutHints:
    """压缩表示据此决定哪些文字格按结构文字照发。

    列表的表头只认两种：最上面那个表头（和与它同一行的并排表头），以及上方隔着空行（或「空行 + 小标题」）的
    后续表头。紧挨着上一张表的「表头」不认：数据行的数字格空着时（「王五 | 电话 | (空) | 备注」）它也像表头，
    认了就会把姓名、电话当结构文字发给模型（P2-SPEC 6.2「列表数据列的文字只发列画像」）。起草那边已经不会在
    这种行上切开（_scan_block），这里再守一道：判错的代价是外发数据，宁可让模型少看一个表头。
    """
    axis = _axis_row(grid)
    if axis is not None:
        return LayoutHints(axis_row=axis[0], label_col=min(axis[1]) - 1)
    hints = LayoutHints()
    blocks = _analyze_lists(grid).blocks
    if not blocks:
        return hints
    top = min(b.header_rows[0] for b in blocks)
    for b in blocks:
        first = b.header_rows[0]
        if first == top or _separated_above(grid, first, b.cols):
            hints.header_rows.update(range(first, b.header_rows[-1] + 1))
    hints.top_header_row = top
    return hints


# --------------------------------------------------------------------------
# 交叉表
# --------------------------------------------------------------------------


@dataclass
class _Seg:
    role: str                                   # measures / dimension / derived
    rows: list[int]
    labels: list[str]                           # 原文（去首尾空白）
    title: str | None = None
    title_row: int | None = None
    parser: str | None = None                   # dimension：hour_range / text
    # 以下由命名阶段填
    id: str = ""
    table: str = ""
    columns: dict[str, str] = field(default_factory=dict)   # measures：标签原文 → 列名
    unit: str | None = None                     # dimension：值列单位
    unit_raw: str | None = None                 # 分段标题括号里的原文（可能不在词表）
    value: str = ""
    dim: str = ""
    const: dict[str, str] = field(default_factory=dict)
    base: "_Seg | None" = None                  # derived：紧跟的分段
    stop: bool = False
    keep_table: str = ""


@dataclass
class _Failure:
    human: str
    model: str


@dataclass
class _Cross:
    sheet: str
    axis_row: int
    label_col: int
    c1: int
    c2: int
    date_cols: list[int]
    year_less: bool
    segments: list[_Seg] = field(default_factory=list)
    value_type: str = "INTEGER"
    #: 占位符：canon → [首个原文, 个数, 格子]
    placeholders: dict[str, list[Any]] = field(default_factory=dict)
    nonnumeric: dict[str, int] = field(default_factory=dict)
    blanks: list[tuple[int, int]] = field(default_factory=list)
    formulas: list[tuple[int, int]] = field(default_factory=list)
    region: set[tuple[int, int]] = field(default_factory=set)
    failures: list[_Failure] = field(default_factory=list)
    #: 按行号：data 行的数值（公式取保存值，不是数为 None），只给 detect_facts 用
    values: dict[int, list[int | float | None]] = field(default_factory=dict)


def _label_of(cell: GridCell | None) -> str | None:
    if cell is None or cell.value is None:
        return None
    if isinstance(cell.value, str):
        return _text(cell.value)
    return _shown(cell.value) or None


def _sums_above(cell: GridCell | None, r: int) -> bool:
    """这格是不是「汇总上方连续几行」的公式（=SUM(C22:C25) 写在 C26）：表内合计行的信号。"""
    if cell is None or not cell.formula:
        return False
    m = re.fullmatch(r"=\s*SUM\(\s*\$?([A-Z]{1,3})\$?(\d+)\s*:\s*\$?([A-Z]{1,3})\$?(\d+)\s*\)\s*", cell.formula.upper())
    return bool(m) and m.group(1) == m.group(3) and int(m.group(4)) == r - 1


def _analyze_crosstab(grid: Grid, axis_row: int, date_cols: list[int]) -> _Cross:
    sheet = grid.sheet
    c1, c2 = min(date_cols), max(date_cols)
    label_col = c1 - 1
    x = _Cross(sheet=sheet, axis_row=axis_row, label_col=label_col, c1=c1, c2=c2, date_cols=date_cols,
               year_less=any((cell := grid.get(axis_row, c)) is not None
                             and (d := P.month_day_or_date(cell.value)) is not None and d.year is None
                             for c in date_cols))
    fail = x.failures.append
    if label_col < 1:
        fail(_Failure(f"工作表「{sheet}」：第 {axis_row} 行的日期从 A 列开始，左边没有行标签列",
                      f"工作表「{sheet}」第 {axis_row} 行的日期从 A 列开始，左边没有行标签列"))
        return x
    gaps = [c for c in range(c1, c2 + 1) if c not in set(date_cols)]
    if gaps:
        where = "、".join(f"{col_letter(c)} 列" for c in gaps[:5])
        msg = f"工作表「{sheet}」：第 {axis_row} 行的日期中间夹着不是日期的格（{where}）"
        fail(_Failure(msg, msg))
    extra = [c for c, _ in grid.row(axis_row) if c > c2]
    if extra:
        msg = f"工作表「{sheet}」：第 {axis_row} 行最后一个日期右边还有格子（{_a1_list([(axis_row, c) for c in extra])}）"
        fail(_Failure(msg, msg))

    # ---- 行分类（P2-SPEC 4.3）：只看标签列和轴列
    bottom = _bottom(grid)
    kinds: list[tuple[int, str, str | None]] = []
    for r in range(axis_row + 1, bottom + 1):
        lab_cell = grid.get(r, label_col)
        label = _label_of(lab_cell)
        has = any(grid.get(r, c) is not None for c in range(c1, c2 + 1))
        if lab_cell is not None and lab_cell.value is not None and not isinstance(lab_cell.value, str):
            # 数字、日期当行标签：配方的标签只收文字，也不能把格子里的数写进配方（配方会发给模型看）
            kinds.append((r, "badlabel", None))
        elif label is None:
            kinds.append((r, "orphan" if has else "blank", None))
        elif has:
            kinds.append((r, "data", label))
        elif P.hour_range(label) is not None or P.hour_range_total(label) is not None:
            # 整期是空格的时段行：照常按 data 行处理，不能误判成分段标题（D11）
            kinds.append((r, "data", label))
        else:
            kinds.append((r, "title", label))

    # ---- 分组：空行结束一组；分段标题开始一组
    groups: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for r, kind, label in kinds:
        if kind == "blank":
            cur = None
        elif kind == "title":
            cur = {"title": label, "title_row": r, "rows": [], "labels": []}
            groups.append(cur)
        elif kind == "badlabel":
            cur = None
            msg = f"工作表「{sheet}」：第 {r} 行的行标签（{_a1(r, label_col)}）不是文字"
            fail(_Failure(msg, msg))
            x.region.update((r, c) for c in range(label_col, c2 + 1) if grid.get(r, c) is not None)
        elif kind == "orphan":
            cur = None
            cells = [(r, c) for c in range(c1, c2 + 1) if grid.get(r, c) is not None]
            msg = f"工作表「{sheet}」：第 {r} 行有数值但标签列（{col_letter(label_col)} 列）是空的"
            fail(_Failure(msg, msg))
            x.region.update(cells)    # 已经报了失败，不再按区域外重复报
        else:
            if cur is None:
                cur = {"title": None, "title_row": None, "rows": [], "labels": []}
                groups.append(cur)
            cur["rows"].append(r)
            cur["labels"].append(label)

    seen_title = False
    for g in groups:
        if g["title"] is None:
            if seen_title or any(s.role == "measures" for s in x.segments):
                msg = (f"工作表「{sheet}」：{_rows_text(g['rows'])}的数据行上方没有分段标题，"
                       "和前面的行之间又隔着空行，无法确定归属")
                fail(_Failure(msg, msg))
                continue
            x.segments.append(_Seg("measures", g["rows"], g["labels"]))
            continue
        seen_title = True
        if not g["rows"]:
            continue    # 下面没有数据行的标题：区域外文字
        rows, labels = g["rows"], g["labels"]
        cut = next((i for i, lab in enumerate(labels) if P.hour_range_total(lab) is not None), len(rows))
        drows, dlabels, trows, tlabels = rows[:cut], labels[:cut], rows[cut:], labels[cut:]
        bad_total = False
        for r, lab in zip(drows, dlabels):
            cells = [grid.get(r, c) for c in range(c1, c2 + 1)]
            if any(w in lab for w in _TOTAL_HINT) or (any(c is not None for c in cells)
                                                       and all(_sums_above(c, r) for c in cells if c is not None)):
                msg = f"工作表「{sheet}」：第 {r} 行像合计行，但标签无法确定区间"
                fail(_Failure(f"{msg}（「{lab}」）", f"{msg}（「{lab}」）"))
                bad_total = True
        for r, lab in zip(trows, tlabels):
            if P.hour_range_total(lab) is None:
                msg = f"工作表「{sheet}」：第 {r} 行在合计行之后，但不是能确定区间的合计行（「{lab}」）"
                fail(_Failure(msg, msg))
                bad_total = True
        if bad_total:
            continue
        if not drows:
            msg = f"工作表「{sheet}」：分段标题「{g['title']}」下面只有合计行，没有明细"
            fail(_Failure(msg, msg))
            continue
        parser = "hour_range" if all(P.hour_range(lab) is not None for lab in dlabels) else "text"
        if parser == "text":
            bad = [(r, lab) for r, lab in zip(drows, dlabels) if P.text_label(lab) is None]
            for r, lab in bad:
                msg = f"工作表「{sheet}」：第 {r} 行的标签超过 {P.LABEL_MAX} 字或不是文字"
                fail(_Failure(msg, msg))
            if bad:
                continue
        seg = _Seg("dimension", drows, dlabels, title=g["title"], title_row=g["title_row"], parser=parser)
        x.segments.append(seg)
        if trows:
            if parser != "hour_range":
                msg = (f"工作表「{sheet}」：{_rows_text(trows)}是按时段区间的合计，"
                       f"但上方「{g['title']}」的标签不全是时段")
                fail(_Failure(msg, msg))
                continue
            seg.stop = True
            x.segments.append(_Seg("derived", trows, tlabels, base=seg))

    # ---- 数值区：类型、占位符、空格、公式（P2-SPEC 6.1 第 9 条）
    ints = True
    for seg in x.segments:
        for r in seg.rows:
            row_vals: list[int | float | None] = []
            for c in range(c1, c2 + 1):
                cell = grid.get(r, c)
                kind = _cell_kind(cell)
                num = _value_num(cell)
                row_vals.append(num)
                if num is not None and not _int_like(num):
                    ints = False
                if seg.role == "derived":
                    continue
                if kind == "blank":
                    x.blanks.append((r, c))
                elif kind == "formula":
                    x.formulas.append((r, c))
                elif kind == "ph":
                    key = canon(cell.value)
                    entry = x.placeholders.setdefault(key, [cell.value.strip(), 0, []])
                    entry[1] += 1
                    entry[2].append((r, c))
                    x.nonnumeric[cell.value.strip()] = x.nonnumeric.get(cell.value.strip(), 0) + 1
                elif kind in ("text", "error", "other"):
                    shown = _shown(cell.value)
                    if isinstance(cell.value, str):
                        x.nonnumeric[shown] = x.nonnumeric.get(shown, 0) + 1
                    why = "是错误值" if kind == "error" else "不是数字，也不像表示无数据的符号"
                    fail(_Failure(f"工作表「{sheet}」：{_a1(r, c)} 的「{shown}」{why}",
                                  f"工作表「{sheet}」：{_a1(r, c)} {why}"))
            x.values[r] = row_vals
    x.value_type = "INTEGER" if ints else "REAL"

    # ---- 认领区域、合并区
    claimed_rows = [r for s in x.segments for r in s.rows]
    x.region.update((axis_row, c) for c in range(label_col, c2 + 1))
    for r in claimed_rows:
        x.region.update((r, c) for c in range(label_col, c2 + 1))
    for s in x.segments:
        if s.title_row is not None:
            x.region.add((s.title_row, label_col))
    data_rows = {r for s in x.segments if s.role != "derived" for r in s.rows}
    for (r1, mc1, r2, mc2) in grid.merges:
        if any(r1 <= r <= r2 for r in data_rows) and mc2 >= c1 and mc1 <= c2:
            msg = f"工作表「{sheet}」：合并单元格 {_a1(r1, mc1)}:{_a1(r2, mc2)} 落在数值区"
            fail(_Failure(msg, msg))
    return x


# --------------------------------------------------------------------------
# 列表
# --------------------------------------------------------------------------


@dataclass
class _ListPlan:
    header_rows: list[int]
    cols: list[int]
    headers: dict[int, str]
    rows: list[int] = field(default_factory=list)
    types: dict[int, str] = field(default_factory=dict)
    total_row: int | None = None
    total_word: str | None = None
    skip_blank: bool = False
    skipped: list[int] = field(default_factory=list)
    placeholders: dict[str, list[Any]] = field(default_factory=dict)
    nonnumeric: dict[str, int] = field(default_factory=dict)
    grain_col: int | None = None
    merged_fill: list[tuple[int, int, int, int]] = field(default_factory=list)
    title: str | None = None
    region: set[tuple[int, int]] = field(default_factory=set)
    #: 紧挨着数据、像另一张表表头的行：没有空行或小标题隔开，不在这里切开（切错了会把数据行当表头），
    #: 按数据行收进来并记起草失败
    adjacent: list[tuple[int, list[int]]] = field(default_factory=list)
    #: 不在末尾第一列的合计字样：(行, 列)。第一列在中间或最上面、或者落在别的列上，都无法按合计行核对，
    #: 记起草失败；不记就会把合计当明细导入，求和翻倍（H2）
    stray_totals: list[tuple[int, int]] = field(default_factory=list)
    # 命名阶段填
    id: str = ""
    table: str = ""
    names: dict[int, str] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    keep_table: str = ""


@dataclass
class _Lists:
    sheet: str
    blocks: list[_ListPlan] = field(default_factory=list)
    failures: list[_Failure] = field(default_factory=list)


def _date_kind(v: Any) -> bool:
    if isinstance(v, _dt.datetime):
        return (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0)
    if isinstance(v, _dt.date):
        return True
    if isinstance(v, str):
        d = P.month_day_or_date(v)
        return d is not None and d.year is not None
    return False


def _list_kind(cell: GridCell | None) -> str | None:
    """列表数据格：num / date / ph / text；空格 None。公式按保存值算。"""
    if cell is None:
        return None
    v = cell.value
    if v is None:
        return "text" if cell.formula is None else "num"
    if _num(v):
        return "num"
    if _date_kind(v):
        return "date"
    if isinstance(v, str) and _placeholder_like(v):
        return "ph"
    return "text"


def _col_type(kinds: set[str], all_int: bool) -> str:
    if kinds and kinds <= {"num", "ph"} and "num" in kinds:
        return "INTEGER" if all_int else "REAL"
    if kinds and kinds <= {"date", "ph"} and "date" in kinds:
        return "DATE"
    return "TEXT"


def _types_of(grid: Grid, rows: list[int], cols: list[int]) -> dict[int, str]:
    acc = _ColTypes(grid, cols)
    for r in rows:
        acc.add(r)
    return acc.types()


class _ColTypes:
    """各列到目前为止的类型（_col_type 的增量版）。收数据行时每一行都要问一次「和本块对不对得上」，
    每次从头推一遍是行数的平方（文字格还要试着按日期解析），几百行的名单就要一两秒。"""

    def __init__(self, grid: Grid, cols: list[int]) -> None:
        self.grid, self.cols = grid, cols
        self.kinds: dict[int, set[str]] = {c: set() for c in cols}
        self.ints: dict[int, bool] = {c: True for c in cols}
        self._types: dict[int, str] | None = None

    def add(self, r: int) -> None:
        for c in self.cols:
            cell = self.grid.get(r, c)
            k = _list_kind(cell)
            if k is None:
                continue
            self.kinds[c].add(k)
            if _num(cell.value) and not _int_like(cell.value):     # type: ignore[union-attr]
                self.ints[c] = False
        self._types = None

    def types(self) -> dict[int, str]:
        if self._types is None:
            self._types = {c: _col_type(self.kinds[c], self.ints[c]) for c in self.cols}
        return self._types


def _compatible(grid: Grid, r: int, cols: list[int], types: dict[int, str]) -> bool:
    """空行之后的这一行和上方是不是同一张表：≥2 个非空格，每格都合各列的类型。"""
    cells = [(c, grid.get(r, c)) for c in cols if grid.get(r, c) is not None]
    if len(cells) < 2:
        return False
    for c, cell in cells:
        k = _list_kind(cell)
        t = types.get(c, "TEXT")
        if t in ("INTEGER", "REAL") and k not in ("num", "ph"):
            return False
        if t == "DATE" and k not in ("date", "ph"):
            return False
    return True


def _next_row(grid: Grid, r: int, cols: list[int], bottom: int) -> int | None:
    lo, hi = cols[0], cols[-1]
    for rr in grid.row_numbers():
        if rr > r and rr <= bottom and any(lo <= c <= hi for c, _ in grid.row(rr)):
            return rr
    return None


_TYPE_CLASS = {"INTEGER": "num", "REAL": "num", "DATE": "date"}


def _foreign_cols(grid: Grid, r: int, cols: list[int], types: dict[int, str]) -> list[int]:
    """r 行里落在数字或日期列上的文字格所在的列。"""
    return [c for c in cols if types.get(c) in _TYPE_CLASS and _list_kind(grid.get(r, c)) == "text"]


def _title_row(grid: Grid, r: int, cols: list[int]) -> bool:
    """块的列范围里只有一格文字（「二、费用」）：下一张表的小标题。"""
    cells = [cell for c, cell in grid.row(r) if cols[0] <= c <= cols[-1]]
    if len(cells) != 1 or cells[0].formula is not None:
        return False
    t = _text(cells[0].value)
    return t is not None and not P.looks_numeric_text(t) and _total_word(t) is None


def _separated_above(grid: Grid, r: int, cols: list[int]) -> bool:
    """r 行（表头）上方在这几列里隔着空行，或者是「空行 + 小标题」，或者已经到了工作表顶上。"""
    rr = r - 1
    if rr >= 1 and _title_row(grid, rr, cols):
        rr -= 1
    return rr < 1 or not any(cols[0] <= c <= cols[-1] for c, _ in grid.row(rr))


def _new_header(grid: Grid, r: int, plan: _ListPlan, bottom: int, types: dict[int, str]) -> bool:
    """r 行是不是下一张表的表头：像表头，并且和本块对不上——有文字落在本块的数字或日期列上，或者它下面
    几行的列类型和本块不同。

    只看「像表头」不够：数据行的数字格空着时（「王五 | 电话 | (空) | 备注」）这一行全是文字、互不相同、下面
    有数，也像表头；可它的文字都在本块的文字列上，下面的行也和本块同类型。把它当表头，姓名、电话就成了列名，
    还会当结构文字发给模型。
    """
    if not plan.rows or not _header_like(grid, r):
        return False
    if _foreign_cols(grid, r, plan.cols, types):
        return True
    below = [rr for rr in range(r + 1, min(r + 3, bottom) + 1)
             if any(grid.get(rr, c) is not None for c in plan.cols)]
    if not below:
        return False
    under = _types_of(grid, below, plan.cols)
    return any(_TYPE_CLASS.get(under[c]) and _TYPE_CLASS.get(under[c]) != _TYPE_CLASS.get(types.get(c, ""))
               for c in plan.cols)


def _more_rows(grid: Grid, r: int, plan: _ListPlan, bottom: int, types: dict[int, str]) -> bool:
    """合计字样那一行下面紧接着还有同一张表的行（明细，或另一行合计）：它不在末尾，是夹在中间的小计。"""
    nr = r + 1
    if nr > bottom or all(grid.get(nr, c) is None for c in plan.cols):
        return False
    if _title_row(grid, nr, plan.cols) or _new_header(grid, nr, plan, bottom, types):
        return False
    return _compatible(grid, nr, plan.cols, types)


def _scan_block(grid: Grid, plan: _ListPlan, bottom: int) -> None:
    """从表头下一行往下收数据行，到空行（后面不是同一张表）、小标题 + 新表头、或末尾的合计行为止。

    新表头只在空行或小标题之后才认（P2-SPEC 6.1 第 11 条的「第二个表头」）：块里上下行都是数据时，一行
    「像表头」多半只是数字格空着，不能在那里切开。真像另一张表、却紧挨着数据的行记进 adjacent（起草失败）。
    """
    cols = plan.cols
    acc = _ColTypes(grid, cols)
    r = plan.header_rows[-1] + 1
    while r <= bottom:
        cells = [grid.get(r, c) for c in cols]
        types = acc.types()
        if all(c is None for c in cells):
            nxt = _next_row(grid, r, cols, bottom)
            if nxt is None or not plan.rows or _new_header(grid, nxt, plan, bottom, types):
                break
            if _compatible(grid, nxt, cols, types) and _total_word(
                    _label_of(grid.get(nxt, cols[0]))) is None:
                plan.skip_blank = True
                plan.skipped.extend(range(r, nxt))
                r = nxt
                continue
            break
        if plan.rows and _title_row(grid, r, cols) and _new_header(grid, r + 1, plan, bottom, types):
            break
        if _new_header(grid, r, plan, bottom, types):
            plan.adjacent.append((r, _foreign_cols(grid, r, cols, types)))
        word = _total_word(_label_of(grid.get(r, cols[0])))
        if word is not None and plan.rows and not _more_rows(grid, r, plan, bottom, types):
            plan.total_row, plan.total_word = r, word
            break
        if word is not None:
            plan.stray_totals.append((r, cols[0]))
        plan.stray_totals.extend((r, c) for c, cell in zip(cols[1:], cells[1:])
                                 if cell is not None and _total_word(cell.value) is not None)
        plan.rows.append(r)
        acc.add(r)
        r += 1


def _list_failures(sheet: str, grid: Grid, plan: _ListPlan) -> list[_Failure]:
    """块里收进来、但起草无法确定的行：紧挨着数据的新表头、不在末尾第一列的合计字样（照交叉表第 7 条的做法）。

    模型版只写行号、列号和命中的合计字样（「合计」「小计」本身是结构文字），不带格子原文。
    """
    out: list[_Failure] = []
    for r, foreign in plan.adjacent:
        where = f"，{'、'.join(col_letter(c) for c in foreign)} 列应是数字或日期" if foreign else ""
        msg = (f"工作表「{sheet}」：第 {r} 行像另一张表的表头{where}，但它紧挨着上方的数据，"
               "中间没有空行或标题，无法确定两张表的分界")
        out.append(_Failure(msg, msg))
    seen: set[int] = set()
    for r, c in plan.stray_totals:
        if r in seen:
            continue
        seen.add(r)
        cell = grid.get(r, c)
        text = _text(cell.value) if cell is not None else None
        word = _total_word(text) or ""
        shown = (text or "")[:20]
        if c == plan.cols[0]:
            why = "但不在列表末尾，无法确定它汇总的是哪几行"
        else:
            why = "但合计字样不在第一列，无法按合计行核对"
        out.append(_Failure(f"工作表「{sheet}」：第 {r} 行像合计行（{col_letter(c)} 列「{shown}」），{why}",
                            f"工作表「{sheet}」：第 {r} 行像合计行（{col_letter(c)} 列以「{word}」开头），{why}"))
    return out


def _analyze_lists(grid: Grid) -> _Lists:
    out = _Lists(sheet=grid.sheet)
    rows = grid.row_numbers()
    if not rows:
        return out
    bottom = rows[-1]
    first_limit = rows[0] + LIST_HEADER_SCAN_ROWS - 1
    r_idx = 0
    seen_keys: list[frozenset[str]] = []
    while r_idx < len(rows):
        r = rows[r_idx]
        if not out.blocks and r > first_limit:
            break
        if not _header_like(grid, r):
            r_idx += 1
            continue
        hrows = _header_rows_at(grid, r)
        if hrows[0] < r and out.blocks and hrows[0] <= max(b.rows[-1] if b.rows else b.header_rows[-1]
                                                            for b in out.blocks):
            hrows = [r]
        all_cols = sorted({c for hr in hrows for c, _ in grid.row(hr)}
                          | {c for (r1, c1, r2, c2) in grid.merges if hrows[0] <= r1 <= hrows[-1]
                             for c in range(c1, c2 + 1)})
        texts = _header_texts(grid, hrows, all_cols)
        runs: list[list[int]] = []
        for c in sorted(texts):
            if runs and c == runs[-1][-1] + 1:
                runs[-1].append(c)
            else:
                runs.append([c])
        made: list[_ListPlan] = []
        for run in runs:
            if len(run) < 2:
                continue
            plan = _ListPlan(header_rows=hrows, cols=run, headers={c: texts[c] for c in run})
            _scan_block(grid, plan, bottom)
            if not plan.rows:
                continue
            made.append(plan)
        if not made:
            r_idx += 1
            continue
        for plan in made:
            keys = frozenset(P.match_key(h) for h in plan.headers.values())
            if keys in seen_keys:
                msg = (f"工作表「{grid.sheet}」：{_rows_text(plan.header_rows)}的表头和上方另一张表完全相同，"
                       "无法区分两张表")
                out.failures.append(_Failure(msg, msg))
            seen_keys.append(keys)
            _finish_block(grid, plan)
            out.failures.extend(_list_failures(grid.sheet, grid, plan))
            out.blocks.append(plan)
        end = max((p.total_row or (p.rows[-1] if p.rows else p.header_rows[-1])) for p in made)
        while r_idx < len(rows) and rows[r_idx] <= end:
            r_idx += 1
    return out


def _finish_block(grid: Grid, plan: _ListPlan) -> None:
    """类型、占位符、主键、合并区、标题、认领区域。"""
    cols, rows = plan.cols, plan.rows
    plan.types = _types_of(grid, rows, cols)
    for c in cols:
        if plan.types[c] not in ("INTEGER", "REAL", "DATE"):
            continue
        for r in rows:
            cell = grid.get(r, c)
            if _list_kind(cell) == "ph":
                key = canon(cell.value)
                entry = plan.placeholders.setdefault(key, [cell.value.strip(), 0, []])
                entry[1] += 1
                entry[2].append((r, c))
                plan.nonnumeric[cell.value.strip()] = plan.nonnumeric.get(cell.value.strip(), 0) + 1
    for c in cols:
        if plan.types[c] not in ("TEXT", "DATE"):
            continue
        keys: list[str] = []
        for r in rows:
            cell = grid.get(r, c)
            if cell is None or cell.value is None:
                break
            keys.append(canon(_shown(cell.value)))
        if len(keys) == len(rows) and len(set(keys)) == len(keys):
            plan.grain_col = c
            break
    for m in grid.merges:
        r1, c1, r2, c2 = m
        if not any(r1 <= r <= r2 for r in rows) or c2 < cols[0] or c1 > cols[-1]:
            continue
        plan.merged_fill.append(m)
    # 表头正上方的单格文字当标题（表名用它，不含数字时）
    above = plan.header_rows[0] - 1
    for rr in (above, above - 1):
        cells = [(c, cell) for c, cell in grid.row(rr) if cols[0] <= c <= cols[-1]]
        if len(cells) == 1 and _text(cells[0][1].value):
            plan.title = _text(cells[0][1].value)
            break
        if cells:
            break
    last = plan.total_row or rows[-1]
    for r in range(plan.header_rows[0], last + 1):
        for c in cols:
            if grid.get(r, c) is not None or r in plan.header_rows:
                plan.region.add((r, c))
    for (r1, c1, r2, c2) in plan.merged_fill:
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                plan.region.add((r, c))


# --------------------------------------------------------------------------
# 一张工作表
# --------------------------------------------------------------------------


@dataclass
class _Sheet:
    sheet: str
    sid: str
    cross: _Cross | None = None
    lists: _Lists | None = None
    region: set[tuple[int, int]] = field(default_factory=set)
    period: list[tuple[int, int, str]] = field(default_factory=list)
    outside: list[tuple[int, int]] = field(default_factory=list)
    failures: list[_Failure] = field(default_factory=list)
    hidden_rows: list[int] = field(default_factory=list)
    hidden_cols: list[int] = field(default_factory=list)


def _analyze_sheet(grid: Grid, sid: str) -> _Sheet:
    sh = _Sheet(sheet=grid.sheet, sid=sid)
    axis = _axis_row(grid)
    if axis is not None:
        sh.cross = _analyze_crosstab(grid, *axis)
        sh.region = set(sh.cross.region)
        sh.failures.extend(sh.cross.failures)
    else:
        sh.lists = _analyze_lists(grid)
        sh.failures.extend(sh.lists.failures)
        for b in sh.lists.blocks:
            sh.region |= b.region
            for (r1, c1, r2, c2) in b.merged_fill:
                bad = [c for c in range(c1, c2 + 1) if b.types.get(c) != "TEXT"]
                if bad or c2 > c1:
                    msg = (f"工作表「{grid.sheet}」：合并单元格 {_a1(r1, c1)}:{_a1(r2, c2)} 落在数据区，"
                           "只有文字列的纵向合并能按左上格填充")
                    sh.failures.append(_Failure(msg, msg))
            if not any(t != "TEXT" for t in b.types.values()):
                msg = (f"工作表「{grid.sheet}」：{_rows_text(b.header_rows)}下面没有一列是统一的数字或日期，"
                       "看起来不是列表（可能是表单）")
                sh.failures.append(_Failure(msg, msg))
        if not sh.lists.blocks:
            msg = (f"工作表「{grid.sheet}」：认不出版式。没有一行有 {DATE_HEADER_MIN} 格以上的日期（交叉表），"
                   "也没有一行像列表的表头（几个互不相同的文字格、下方有数字）")
            sh.failures.append(_Failure(msg, msg))
    rows_in = {r for r, _ in sh.region}
    cols_in = {c for _, c in sh.region}
    sh.hidden_rows = sorted(r for r in grid.hidden_rows if r in rows_in)
    sh.hidden_cols = sorted(c for c in grid.hidden_cols if c in cols_in)
    # 区域外：统计期、区域外文字、区域外数字
    for (r, c), cell in sorted(grid.cells.items()):
        if (r, c) in sh.region:
            continue
        v = cell.value
        if cell.formula is not None:
            msg = f"工作表「{grid.sheet}」：{_a1(r, c)} 是数据区外的公式"
            sh.failures.append(_Failure(msg, msg))
            continue
        if isinstance(v, str) and P.cn_date_range(v) is not None:
            sh.period.append((r, c, v.strip()))
            sh.outside.append((r, c))
            continue
        kind = P.classify_outside(v)
        if kind == "number":
            sh.failures.append(_Failure(
                f"工作表「{grid.sheet}」：数据区外的 {_a1(r, c)} 是数字（「{_shown(v)}」），不知道它属于哪张表",
                f"工作表「{grid.sheet}」：数据区外的 {_a1(r, c)} 是数字，不知道它属于哪张表"))
            continue
        sh.outside.append((r, c))
    return sh


# ==========================================================================
# 整个工作簿：命名、配方、事实
# ==========================================================================


@dataclass
class _Analysis:
    sheets: list[_Sheet]
    hidden_sheets: list[str]
    facts: DraftFacts
    recipe: dict[str, Any] | None
    failures: list[_Failure]


def _visible_grids(scan: WorkbookScan, grids: dict[str, Grid]) -> list[Grid]:
    order = [s.name for s in scan.sheets if s.state == "visible"] if scan.sheets else list(grids)
    out = [grids[n] for n in order if n in grids and grids[n].cells]
    out += [g for n, g in grids.items() if n not in order and g.cells]
    return out


def _common_suffix(titles: list[str]) -> str:
    """表名 = 标题去掉单位后缀后的最长公共后缀，收缩到每个前缀至少 2 字；只有一段时就是去单位后的标题。"""
    bases = [P.split_unit_suffix(t)[0] for t in titles]
    if len(bases) == 1:
        return bases[0]
    suf = 0
    while all(len(b) > suf for b in bases) and len({b[len(b) - 1 - suf] for b in bases}) == 1:
        suf += 1
    while suf and any(len(b) - suf < 2 for b in bases):
        suf -= 1
    name = bases[0][len(bases[0]) - suf:].strip(" _-") if suf else ""
    return name if len(name) >= 2 else bases[0]


def _group_dimensions(segs: list[_Seg]) -> list[list[_Seg]]:
    """合表：dim.parser 相同、标签规范写法集合两两不相交、单位相同的分段写进同一张表（P2-SPEC 6.1 第 5 条）。"""
    groups: list[list[_Seg]] = []
    for s in segs:
        keys = {_dim_key(s.parser, lab) for lab in s.labels}
        unit = P.split_unit_suffix(s.title or "")[1]
        for g in groups:
            if g[0].parser != s.parser or P.split_unit_suffix(g[0].title or "")[1] != unit:
                continue
            if any(keys & {_dim_key(o.parser, lab) for lab in o.labels} for o in g):
                continue
            g.append(s)
            break
        else:
            groups.append([s])
    # 合表要能给每段选出互不相同的常量；选不出来就各成一张表
    out: list[list[_Seg]] = []
    for g in groups:
        if len(g) > 1:
            titles = [s.title or "" for s in g]
            picks = [(P.candidate_words(t, [o for o in titles if o != t]) or [None])[0] for t in titles]
            if None in picks or len(set(picks)) != len(picks):
                out.extend([s] for s in g)
                continue
        out.append(g)
    return out


def _dim_key(parser: str | None, label: str) -> str:
    if parser == "hour_range":
        hr = P.hour_range(label)
        return hr.canonical if hr else canon(label)
    return canon(label)


def _measure_column(label: str, pos: int) -> tuple[str, str | None, str | None]:
    """标签 → (列名, 词表内单位, 括号原文)。单位在词表里：列名去单位；不在词表：列名保留原文（P2-SPEC 6.1 第 8 条）。"""
    name, unit = P.split_unit_suffix(label)
    if unit is not None and P.unit_known(unit):
        return to_sql_name(name, fallback=f"列{pos}"), unit, unit
    if unit is not None:
        return to_sql_name(label, fallback=f"列{pos}"), None, unit
    return to_sql_name(name, fallback=f"列{pos}"), None, None


def _analyze(scan: WorkbookScan, grids: dict[str, Grid]) -> _Analysis:
    visible = _visible_grids(scan, grids)
    hidden = [s.name for s in scan.sheets if s.state != "visible"]
    sheets = [_analyze_sheet(g, f"s{i + 1}") for i, g in enumerate(visible)]
    failures = [f for sh in sheets for f in sh.failures]

    tables_used: set[str] = set()
    seg_ids: set[str] = set()
    block_ids: set[str] = set()
    tables: list[dict[str, Any]] = []
    children: dict[str, list[dict[str, Any]]] = {}

    def table(name: str, grain: list[str], units: dict[str, str], kind: str = "data",
              parent: str | None = None) -> None:
        spec = {"name": name, "grain": grain, "kind": kind, "units": units, "note": ""}
        if parent is None:
            tables.append(spec)
        else:
            children.setdefault(parent, []).append(spec)

    def seg_id(want: str, sid: str) -> str:
        want = want[:32]
        out = want if want not in seg_ids else f"{want[:32 - len(sid) - 1]}_{sid}"
        n = 2
        while out in seg_ids:
            out = f"{want[:28]}_{n}"
            n += 1
        seg_ids.add(out)
        return out

    sheet_dicts: list[dict[str, Any]] = []
    candidates: dict[str, list[str]] = {}
    crosstabs = 0
    lists_n = 0
    for sh in sheets:
        blocks: list[dict[str, Any]] = []
        x = sh.cross
        if x is not None and x.segments:
            crosstabs += 1
            bid = "交叉表" if crosstabs == 1 else f"交叉表_{sh.sid}"
            block_ids.add(bid)
            segs_out: list[dict[str, Any]] = []
            for s in x.segments:
                if s.role != "measures":
                    continue
                base = to_sql_name(sh.sheet, fallback=f"表{len(tables_used) + 1}")[:45]
                s.table = _unique(f"{base}_按日", tables_used)
                s.id = seg_id(s.table, sh.sid)
                used_cols = {collide_key("日期")}
                units: dict[str, str] = {}
                for i, lab in enumerate(s.labels):
                    col, unit, _raw = _measure_column(lab, i + 2)
                    col = _unique(col, used_cols)
                    s.columns[lab] = col
                    if unit:
                        units[col] = unit
                table(s.table, ["日期"], units)
            dims = [s for s in x.segments if s.role == "dimension"]
            for g in _group_dimensions(dims):
                titles = [s.title or "" for s in g]
                n_fallback = len(tables_used) + 1
                want = _common_suffix(titles)
                plain = (to_sql_name(want, fallback=f"表{n_fallback}") if want else f"表{n_fallback}")[:48]
                tname = _unique(plain, tables_used)
                dim = _HOUR_DIM if g[0].parser == "hour_range" else _TEXT_DIM
                const_col = f"{dim}类别" if len(g) > 1 else ""
                # 值列名取去重之前的表名：撞名后的「时段客流_2」最后两个字是「_2」，会得到无意义的「c_2」
                value = to_sql_name(plain[-2:], fallback="数值") if len(plain) >= 2 else "数值"
                reserved = {collide_key(n) for n in ("日期", dim, const_col, *(_DERIVE if g[0].parser == "hour_range"
                                                                                  else ())) if n}
                if collide_key(value) in reserved or len(value) < 2:
                    value = "数值"
                unit_raw = P.split_unit_suffix(titles[0])[1]
                unit = unit_raw if unit_raw and P.unit_known(unit_raw) else None
                for s in g:
                    s.table, s.dim, s.value, s.unit, s.unit_raw = tname, dim, value, unit, unit_raw
                    siblings = [t for t in titles if t != s.title]
                    cands = P.candidate_words(s.title or "", siblings)
                    candidates[s.title or ""] = cands
                    if const_col:
                        s.const = {const_col: cands[0]}
                        s.id = seg_id(cands[0], sh.sid)
                    else:
                        s.id = seg_id(tname, sh.sid)
                table(tname, ["日期", dim], {value: unit} if unit else {})
            for s in x.segments:
                if s.role == "derived" and s.base is not None:
                    s.id = seg_id(f"{s.base.id}合计", sh.sid)
                    keep = f"{s.base.table}_表内合计"
                    if collide_key(keep) not in tables_used:
                        tables_used.add(collide_key(keep))
                        table(keep, ["日期", "合计项"], {s.base.value: s.base.unit} if s.base.unit else {},
                              kind="reported_total", parent=s.base.table)
                    s.keep_table = keep
            for s in x.segments:
                segs_out.append(_segment_dict(s))
            placeholders = [{"text": e[0], "meaning": "无数据"} for e in x.placeholders.values()]
            blocks.append({
                "id": bid, "layout": "crosstab",
                "axis": {"find": {"parser": "month_day_or_date", "min": 2}, "name": "日期", "type": "DATE",
                         "year_from": "统计期", "checks": ["contiguous", "covers_context"]},
                "label_offset": -1,
                "values": {"type": x.value_type, "placeholders": placeholders, "blank": "reject",
                           "text_number": "reject", "formula": "reject"},
                "segments": segs_out,
            })
        if sh.lists is not None:
            for b in sh.lists.blocks:
                lists_n += 1
                block_ids.add(f"列表{lists_n}")
                block, main, total_spec = list_block(sh.sheet, b, block_id=f"列表{lists_n}", tables_used=tables_used)
                table(main["name"], main["grain"], main["units"])
                if total_spec is not None:
                    table(total_spec["name"], total_spec["grain"], total_spec["units"], kind="reported_total",
                          parent=main["name"])
                blocks.append(block)
        if not blocks:
            continue
        context: list[dict[str, Any]] = []
        if sh.period or x is not None:
            context.append({"id": "统计期", "kind": "period", "parser": "cn_date_range",
                            "prefer_prefix": _prefer_prefix(sh.period), "cross_check": "filename"})
        sheet_dicts.append({"id": sh.sid, "match": {"name": sh.sheet[:31], "fallback": "only_visible_sheet"},
                            "hidden": {"rows": "reject_if_any", "cols": "reject_if_any"},
                            "context": context, "blocks": blocks})

    facts = _facts_of(sheets)
    facts.candidates = candidates
    facts.nonnumeric = {}
    for sh in sheets:
        for src in ([sh.cross.nonnumeric] if sh.cross else []) + [b.nonnumeric for b in
                                                                   (sh.lists.blocks if sh.lists else [])]:
            for k, v in src.items():
                facts.nonnumeric[k] = facts.nonnumeric.get(k, 0) + v

    recipe: dict[str, Any] | None = None
    if sheet_dicts:
        ordered: list[dict[str, Any]] = []
        for t in tables:
            ordered.append(t)
            ordered.extend(children.get(t["name"], []))
        recipe = {"recipe_format": RECIPE_FORMAT, "mode": "replace", "other_visible_sheets": "confirm",
                  "sheets": sheet_dicts, "tables": ordered, "relations": _relations_for(facts, sheets)}
    return _Analysis(sheets, hidden, facts, recipe, failures)


def list_block(sheet: str, b: _ListPlan, *, block_id: str, tables_used: set[str],
               table: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """列表计划 → (块, 主表的表定义, 合计表的表定义或 None)。起草和框选（recipe_select 的 as=list）共用这一个函数
    （P3-SPEC 4.2、评审二-m12）：列名、单位、类型、grain、合计行、blank_rows、占位符、合并单元格填充都由这里按
    计划推出，框选只决定表头行和列范围。各写一套的话，走了框选就会丢掉占位符、合并填充、跳过空行这些设置，
    框选得到的规则也不再与起草器相同（4.6 第 1 条要求同一份文件框选后配方哈希不变）。

    tables_used 是已用表名的 collide_key 集合，原地更新（与起草器的去重同一口径）。table 给了时用它作表名
    （框选沿用被替换的块的表名、或客户端给的、已经过名字校验的表名），不再从标题推。
    合计表名撞了已用的名字时照起草器的老行为：不另建表定义，keep_as 仍写这个名字（交给静态校验报）。"""
    b.id = block_id
    if table is None:
        title = b.title if b.title and not any(ch.isdigit() for ch in b.title) else None
        want = _ENUM_PREFIX.sub("", P.split_unit_suffix(title)[0]) if title else sheet
        n_fallback = len(tables_used) + 1
        b.table = _unique(to_sql_name(want, fallback=f"表{n_fallback}") if want else f"表{n_fallback}", tables_used)
    else:
        b.table = table
        tables_used.add(collide_key(table))
    used_cols: set[str] = set()
    cols_out: list[dict[str, Any]] = []
    for i, c in enumerate(b.cols):
        col, unit, _raw = _measure_column(b.headers[c], i + 1)
        col = _unique(col, used_cols)
        b.names[c] = col
        if unit:
            b.units[col] = unit
        cols_out.append({"header": b.headers[c][:80], "name": col, "type": b.types[c]})
    total = None
    total_spec: dict[str, Any] | None = None
    if b.total_row is not None:
        keep = f"{b.table}_表内合计"
        if collide_key(keep) not in tables_used:
            tables_used.add(collide_key(keep))
            num_units = {n: u for c, n in b.names.items() if (u := b.units.get(n))
                         and b.types[c] in ("INTEGER", "REAL")}
            total_spec = {"name": keep, "grain": ["合计项"], "kind": "reported_total", "units": num_units, "note": ""}
        b.keep_table = keep
        total = {"label_column": b.names[b.cols[0]], "pick": b.total_word, "keep_as": keep}
    grain = [b.names[b.grain_col]] if b.grain_col is not None else []
    main = {"name": b.table, "grain": grain, "kind": "data", "units": dict(b.units), "note": ""}
    fill = bool(b.merged_fill) and all(r2 > r1 and c1 == c2 and b.types.get(c1) == "TEXT"
                                        for (r1, c1, r2, c2) in b.merged_fill)
    block = {
        "id": b.id, "layout": "list", "table": b.table, "header_rows": len(b.header_rows),
        "after_title": None, "columns": cols_out, "extra_columns": "reject",
        "rows": {"blank_rows": "skip" if b.skip_blank else "stop", "total_row": total},
        "values": {"placeholders": [{"text": e[0], "meaning": "无数据"} for e in b.placeholders.values()],
                   "blank": "null", "text_number": "reject", "formula": "accept_cached"},
        "merged_data": "fill" if fill else "reject",
    }
    return block, main, total_spec


def _prefer_prefix(period: list[tuple[int, int, str]]) -> str | None:
    """统计期格冒号前的文字（≤12 字、不含数字），作回执里优先展示的格。取第一处。"""
    if not period:
        return None
    s = canon(period[0][2])
    if ":" not in s:
        return None
    head = s.split(":", 1)[0].strip()
    return head if 0 < len(head) <= 12 and not any(ch.isdigit() for ch in head) else None


def _segment_dict(s: _Seg) -> dict[str, Any]:
    if s.role == "measures":
        return {"id": s.id, "role": "measures", "table": s.table, "locate": {"by": "labels"},
                "labels": {"expect": list(s.labels)}, "measures": dict(s.columns)}
    if s.role == "dimension":
        out: dict[str, Any] = {
            "id": s.id, "role": "dimension", "table": s.table,
            "locate": {"by": "section_title", "title": s.title},
            "labels": {"expect": list(s.labels)},
            "dim": {"name": s.dim, "parser": s.parser, "derive": dict(_DERIVE) if s.parser == "hour_range" else {}},
            "value": s.value, "const": {k: {"pick": v} for k, v in s.const.items()},
        }
        if s.stop:
            out["stop_parser"] = "hour_range_total"
        return out
    base = s.base
    assert base is not None
    return {"id": s.id, "role": "derived", "locate": {"by": "after", "segment": base.id},
            "labels_parser": "hour_range_total", "labels": {"expect": list(s.labels)},
            "verify": {"kind": "label_range_sum", "against_table": base.table, "value": base.value},
            "keep_as": {"table": s.keep_table, "dim": "合计项", "derive": dict(_DERIVE), "value": base.value}}


# --------------------------------------------------------------------------
# 系统发现
# --------------------------------------------------------------------------


def _facts_of(sheets: list[_Sheet]) -> DraftFacts:
    """sum_eq 与 not_equal_sum（P2-SPEC 6.1 第 10 条）。编号按工作表、分段、合计行的先后。"""
    facts: list[Fact] = []
    for sh in sheets:
        x = sh.cross
        if x is None:
            continue
        ncols = x.c2 - x.c1 + 1
        sum_totals: dict[str, int] = {}
        for seg in x.segments:
            if seg.role != "measures" or not FACT_PARTS_MIN + 1 <= len(seg.rows) <= FACT_SEARCH_MAX_ROWS:
                continue
            deepest = FACT_PARTS_MAX if len(seg.rows) <= FACT_DEEP_MAX_ROWS else FACT_PARTS_MIN
            for ti, t in enumerate(seg.rows):
                others = [i for i in range(len(seg.rows)) if i != ti]
                hit = None
                for size in range(FACT_PARTS_MIN, min(deepest, len(others)) + 1):
                    for combo in itertools.combinations(others, size):
                        ok = _sum_holds(x, t, [seg.rows[i] for i in combo])
                        if ok is not None:
                            hit = (combo, ok)
                            break
                    if hit:
                        break
                if hit is None:
                    continue
                combo, checked = hit
                parts = [seg.rows[i] for i in combo]
                fid = f"F{len(facts) + 1}"
                text = (f"第 {t} 行 = " + " + ".join(f"第 {p} 行" for p in parts)
                        + f"（{ncols} 列中 {checked} 列成立）")
                facts.append(Fact(fid, "sum_eq", sh.sheet, text, {
                    "segment": seg.id, "total": seg.labels[ti], "parts": [seg.labels[i] for i in combo],
                    "rows": [t, *parts]}))
                sum_totals.setdefault(seg.id, ti)
        measures = next((s for s in x.segments if s.role == "measures"), None)
        if measures is None:
            continue
        bi = sum_totals.get(measures.id, 0)
        b_row = measures.rows[bi]
        by_table: dict[str, list[_Seg]] = {}
        for s in x.segments:
            if s.role == "dimension":
                by_table.setdefault(s.table, []).append(s)
        for tname, segs in by_table.items():
            equal = checked = 0
            for ci in range(ncols):
                b = x.values.get(b_row, [None] * ncols)[ci]
                parts = [x.values.get(r, [None] * ncols)[ci] for s in segs for r in s.rows]
                nums = [p for p in parts if p is not None]
                if b is None or not nums:
                    continue
                checked += 1
                equal += _equal(float(sum(nums)), float(b))
            if checked and equal < checked:
                fid = f"F{len(facts) + 1}"
                names = "、".join(s.id for s in segs)
                facts.append(Fact(fid, "not_equal_sum", sh.sheet,
                                  f"{names}各行之和 与 第 {b_row} 行：{checked} 列中 {equal} 列相等",
                                  {"a": [s.id for s in segs], "b_segment": measures.id,
                                   "b": measures.labels[bi], "equal": equal, "checked": checked}))
    return DraftFacts(facts=facts)


def _sum_holds(x: _Cross, total: int, parts: list[int]) -> int | None:
    """total 行在两边都是数的列上都等于 parts 之和（≥2 列、全部成立）时返回核对的列数。全零的组成行不算。"""
    tv = x.values.get(total)
    pv = [x.values.get(p) for p in parts]
    if tv is None or any(v is None for v in pv):
        return None
    checked = 0
    for ci, t in enumerate(tv):
        col = [v[ci] for v in pv]  # type: ignore[index]
        if t is None or any(c is None for c in col):
            continue
        if not _equal(float(sum(col)), float(t)):
            return None
        checked += 1
    if checked < 2:
        return None
    for v in pv:
        if all((c or 0) == 0 for c in v):  # type: ignore[union-attr]
            return None
    return checked


def _relations_for(facts: DraftFacts, sheets: list[_Sheet]) -> list[dict[str, Any]]:
    """每条事实一条认领：sum_eq 先编 R1…，not_comparable 接着编（P2-SPEC 6.1 第 8b 条）。"""
    segs = {s.id: s for sh in sheets if sh.cross for s in sh.cross.segments}
    out: list[dict[str, Any]] = []
    for f in facts.facts:
        if f.kind != "sum_eq":
            continue
        seg = segs.get(f.detail["segment"])
        if seg is None:
            continue
        out.append({"id": f"R{len(out) + 1}", "kind": "sum_eq", "table": seg.table,
                    "total": seg.columns[f.detail["total"]],
                    "parts": [seg.columns[p] for p in f.detail["parts"]], "claims": f.id})
    for f in facts.facts:
        if f.kind != "not_equal_sum":
            continue
        a = segs.get(f.detail["a"][0])
        b = segs.get(f.detail["b_segment"])
        if a is None or b is None:
            continue
        out.append({"id": f"R{len(out) + 1}", "kind": "not_comparable",
                    "a": {"table": a.table, "value": a.value},
                    "b": {"table": b.table, "value": b.columns[f.detail["b"]]}, "by": "日期", "claims": f.id})
    return out


def detect_facts(grids: dict[str, Grid], scan: WorkbookScan, *,
                 recipe: dict[str, Any] | None = None) -> DraftFacts:
    """系统发现（只给关系不给数值）。分段 id 按规则起草的取法（同一份原件得到同一组 id）。

    给了 recipe（改配方、上传新一期时的现行或工作配方）时，再按标签把事实里的分段 id 和标签原文换成
    这份配方里对应的写法（remap_facts）：用户把规则起草的「客流汇总_按日」改名成「日客流」之后，静态校验的
    认领检查才对得上；事实本身（哪几行之和等于哪一行）不变，所以「甲 + 乙」配「甲 + 乙 + 丙」照样报出来。
    """
    an = _analyze(scan, grids)
    if recipe is None:
        return an.facts
    titles = {s.id: s.title for sh in an.sheets if sh.cross for s in sh.cross.segments
              if s.role == "dimension" and s.title}
    return remap_facts(an.facts, recipe, visible=[sh.sheet for sh in an.sheets], titles=titles)


def remap_facts(facts: DraftFacts, recipe: dict[str, Any], *, visible: list[str] | None = None,
                titles: dict[str, str] | None = None) -> DraftFacts:
    """把事实的 detail 里的分段 id、标签原文换成配方里按 match_key 对得上的分段和 expect 写法。对不上的原样留着。

    **只在事实所在的那张工作表里找**：配方里 match.name 与 Fact.sheet 按 match_key 相同的工作表；没有同名的，
    且文件里恰好只有一张有内容的可见工作表（visible）、配方里恰好一张 fallback=only_visible_sheet 时，用那一张
    （与执行器认工作表的办法一致）。不限定工作表的话，多张同结构工作表时后面那张的事实会被改指到第一张的分段，
    原样的规则草稿也会报 fact_claim_mismatch。visible 不给时不做回退。

    not_equal_sum 的明细分段 a 改过 id 时，按分段标题认（titles：规则起草的分段 id → 标题，detect_facts 给）；
    认不全时，只有这张工作表上恰好一张长表才取它的全部分段——两张长表时取全部会把别的表拼进来。
    """
    try:
        parsed = Recipe.model_validate(recipe)
    except ValidationError:
        return facts
    titles = titles or {}

    def scope(fact_sheet: str) -> list[Any]:
        key = P.match_key(fact_sheet)
        named = [s for s in parsed.sheets if P.match_key(s.match.name) == key]
        if named:
            return named
        if visible is not None and len(visible) == 1 and P.match_key(visible[0]) == key:
            fb = [s for s in parsed.sheets if s.match.fallback == "only_visible_sheet"]
            if len(fb) == 1:
                return fb
        return []

    def segments(sheets: list[Any]) -> tuple[list[MeasuresSegment], list[DimensionSegment]]:
        ms: list[MeasuresSegment] = []
        ds: list[DimensionSegment] = []
        for sheet in sheets:
            for block in sheet.blocks:
                if isinstance(block, CrosstabBlock):
                    ms += [s for s in block.segments if isinstance(s, MeasuresSegment)]
                    ds += [s for s in block.segments if isinstance(s, DimensionSegment)]
        return ms, ds

    def find_measures(ms: list[MeasuresSegment], seg_id: str,
                      labels: list[str]) -> tuple[MeasuresSegment, dict[str, str]] | None:
        want = [P.match_key(x) for x in labels]
        # 同 id 的分段优先（没改名时不动 id，只换标签写法）
        for seg in sorted(ms, key=lambda s: s.id != seg_id):
            keys = {P.match_key(e): e for e in seg.labels.expect}
            if all(k in keys for k in want):
                return seg, keys
        return None

    out: list[Fact] = []
    for f in facts.facts:
        d = dict(f.detail)
        ms, ds = segments(scope(f.sheet))
        if f.kind == "sum_eq":
            hit = find_measures(ms, d.get("segment", ""), [d["total"], *d["parts"]])
            if hit is not None:
                seg, keys = hit
                d["segment"] = seg.id
                d["total"] = keys[P.match_key(d["total"])]
                d["parts"] = [keys[P.match_key(p)] for p in d["parts"]]
        else:
            hit = find_measures(ms, d.get("b_segment", ""), [d["b"]])
            if hit is not None:
                seg, keys = hit
                d["b_segment"] = seg.id
                d["b"] = keys[P.match_key(d["b"])]
            ids = {s.id for s in ds}
            if ds and not all(a in ids for a in d["a"]):
                by_title = {P.match_key(s.locate.title): s.id for s in ds
                            if s.locate.by == "section_title" and s.locate.title}
                mapped: list[str | None] = []
                for a in d["a"]:
                    title = titles.get(a)
                    mapped.append(a if a in ids else by_title.get(P.match_key(title)) if title else None)
                if all(mapped):
                    d["a"] = mapped
                elif len({s.table for s in ds}) == 1:
                    d["a"] = [s.id for s in ds]
        out.append(Fact(f.id, f.kind, f.sheet, f.text, d))
    return DraftFacts(facts=out, candidates=dict(facts.candidates), nonnumeric=dict(facts.nonnumeric))


# ==========================================================================
# 起草
# ==========================================================================


def _eligible(recipe: dict[str, Any]) -> bool:
    """这份配方符合按期累积的资格（契约 accumulate_blockers 为空）。pydantic 不收时不符合。"""
    try:
        return not accumulate_blockers(Recipe.model_validate(recipe))
    except ValidationError:
        return False


def _full_form(recipe: dict[str, Any]) -> dict[str, Any]:
    """补全默认值的完整形式：问题的 effects 按它写路径，父对象都在。pydantic 不收时原样返回（交给静态校验报）。"""
    try:
        return Recipe.model_validate(recipe).model_dump(mode="json")
    except ValidationError:
        return recipe


def draft(scan: WorkbookScan, grids: dict[str, Grid], filename: str, *,
          validate: ValidateFn, dry_run: DryRunFn | None = None) -> Draft:
    """规则起草。不抛异常；认不出时 recipe=None、complete=False、failures 写原因。

    filename 只为与接口层的调用一致：文件名旁证一律写 cross_check="filename"，由执行器在每期导入时核对，
    起草时不看文件名（文件名每期都变，不能进配方）。
    """
    try:
        an = _analyze(scan, grids)
    except Exception as e:  # noqa: BLE001 - 起草器出任何错都只能变成「起草不完整」
        _log_failure("rule draft", e)
        return Draft(None, False, "rules", failures=[_FAIL_TEXT["draft"]],
                     failures_for_model=[_FAIL_TEXT["draft_model"]])
    failures = [f.human for f in an.failures]
    for_model = [f.model for f in an.failures]
    recipe = _full_form(an.recipe) if an.recipe is not None else None
    if recipe is not None and _eligible(recipe):
        # D14 = A：按统计期出的报表默认按期累积，其他默认替换（P3-SPEC 2.2）；首次导入时由人经 q_mode 和确认项确认
        recipe["mode"] = "accumulate"
    extraction: Extraction | None = None
    if recipe is None and not failures:
        failures.append("没有找到有内容的可见工作表")
        for_model.append("没有找到有内容的可见工作表")
    if recipe is not None:
        h, m, parsed = check_recipe(recipe, an.facts, "rules", validate)
        failures += h
        for_model += m
        if parsed is not None and not h and dry_run is not None:
            extraction, h, m = run_dry(parsed, dry_run, grids)
            failures += h
            for_model += m
    cards: list[Card] = []
    questions: list[Question] = []
    if recipe is not None:
        try:
            cards, questions = questions_for(recipe, an.facts, grids, extraction=extraction)
        except Exception:  # noqa: BLE001 - 卡片是辅助信息，出错不能拖垮起草
            cards, questions = [], []
    # 给了干跑时 questions_for 已经按执行器的 skipped_hidden 出过这些卡片：同 id 的不再补，免得界面上 key 重复
    have = {c.id for c in cards}
    extra = [_hidden_sheet_card(name) for name in an.hidden_sheets if f"hidden_sheet:{name}" not in have]
    at = next((i for i, c in enumerate(cards) if c.id == "mode"), len(cards))
    cards[at:at] = extra
    return Draft(recipe=recipe, complete=recipe is not None and not failures, origin="rules", cards=cards,
                 questions=questions, facts=an.facts, failures=_dedupe(failures),
                 failures_for_model=_dedupe(for_model))


#: 起草器、注入的校验和干跑自己出错时的失败原因。异常类名只进日志：界面上不露内部实现，报错写「发生了什么 +
#: 下一步」（terms.ts 的规范，后端返回给界面的文案同样适用）
_FAIL_TEXT = {
    "draft": "规则起草未能完成：分析表格版式时出错。请在配方面板中粘贴一份完整的配方",   # 没有半成品：面板里没有表单可填
    "draft_model": "规则起草未能完成，没有半成品配方",
    "validate": "配方检查未能完成：检查时出错。请在配方面板中修改配方后重试",
    "validate_model": "配方检查未能完成",
    "dry_run": "按草稿检查表格未能完成：检查时出错。请在配方面板中修改配方后重试",
    "dry_run_model": "按草稿检查表格未能完成",
}


def _log_failure(what: str, e: BaseException) -> None:
    """异常只进日志：记类名和出错的位置，不记异常消息（消息里可能带着格子的值）。"""
    tb = traceback.extract_tb(e.__traceback__)
    where = f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else "?"
    logger.warning("%s failed: %s at %s", what, type(e).__name__, where)


def _hidden_sheet_card(name: str) -> Card:
    return Card(f"hidden_sheet:{name}", f"工作表「{name}」是隐藏的，默认不导入",
                "隐藏的工作表不读取内容；需要导入时请在 Excel 里取消隐藏后重新上传")


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for x in items:
        if x not in seen:
            seen.append(x)
    return seen


def check_recipe(recipe: dict[str, Any], facts: DraftFacts | None, origin: str,
                 validate: ValidateFn) -> tuple[list[str], list[str], Recipe | None]:
    """静态校验：(给人看的问题, 给模型看的问题, 通过时的 Recipe)。校验器出错也算问题，不抛。"""
    try:
        parsed, problems = validate(recipe, facts, origin)
    except Exception as e:  # noqa: BLE001
        _log_failure("recipe validation", e)
        return [_FAIL_TEXT["validate"]], [_FAIL_TEXT["validate_model"]], None
    if problems:
        return ([p.message for p in problems],
                [MODEL_LINE_PROMPT["static"].format(path=p.path or "/", message=p.message)
                 for p in problems[:MODEL_LINES_MAX]], None)
    if parsed is None:
        return ["静态校验没有给出配方"], ["静态校验没有给出配方"], None
    return [], [], parsed


def _dry_outcome(ext: Extraction, grids: dict[str, Grid]) -> tuple[Extraction, list[str], list[str]]:
    structure = [p for p in ext.problems if p.category == "structure"]
    return ext, [p.message for p in structure], problem_lines_for_model(structure, grids)


def run_dry(recipe: Recipe, dry_run: DryRunFn,
            grids: dict[str, Grid]) -> tuple[Extraction | None, list[str], list[str]]:
    """干跑：(Extraction, 结构类问题的人话, 同一组问题的模型版)。只有 structure 类算起草不完整。"""
    try:
        ext = dry_run(recipe)
    except Exception as e:  # noqa: BLE001
        _log_failure("dry run", e)
        return None, [_FAIL_TEXT["dry_run"]], [_FAIL_TEXT["dry_run_model"]]
    return _dry_outcome(ext, grids)


def _is_async(fn: Any) -> bool:
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(getattr(fn, "__call__", None))


async def run_dry_async(recipe: Recipe, dry_run: DryRunFn | AsyncDryRunFn,
                        grids: dict[str, Grid]) -> tuple[Extraction | None, list[str], list[str]]:
    """run_dry 的协程版（AI 起草在事件循环上用）：协程函数直接 await（WP-5c 注入的那份自己占解析名额、
    进线程池）；普通函数放进线程池跑，不在事件循环上执行。出错同 run_dry，变成一条起草失败原因。"""
    try:
        if _is_async(dry_run):
            ext = await dry_run(recipe)  # type: ignore[misc]
        else:
            ext = await asyncio.to_thread(dry_run, recipe)
            if inspect.isawaitable(ext):
                ext = await ext
    except Exception as e:  # noqa: BLE001
        _log_failure("dry run", e)
        return None, [_FAIL_TEXT["dry_run"]], [_FAIL_TEXT["dry_run_model"]]
    return _dry_outcome(ext, grids)  # type: ignore[arg-type]


# ==========================================================================
# 给模型看的问题文字（起草失败原因、AI 修订回灌共用）
# ==========================================================================


def _cell_tokens(value: Any) -> list[str]:
    if isinstance(value, bool) or value is None:
        return []
    if _num(value):
        out = {str(value)}
        if isinstance(value, float) and value.is_integer():
            out.add(str(int(value)))
        try:
            out.add(f"{value:,}")
            if isinstance(value, float) and value.is_integer():
                out.add(f"{int(value):,}")
        except (TypeError, ValueError):
            pass
        return sorted(out, key=len, reverse=True)
    if isinstance(value, str) and P.looks_numeric_text(value):
        return sorted({value.strip(), canon(value)}, key=len, reverse=True)
    return []


def scrub_cells(text: str, cells: list[str], grids: dict[str, Grid]) -> str:
    """把这条问题自己的格子里的数（数字格的值、像数的文字）在 text 里出现的地方换成 <值>。

    只按数的边界换（前面不是字母数字、后面不是数字），坐标「C5」里的 5 不会被误换。
    """
    for coord in cells:
        sheet, _, ref = coord.rpartition("!")
        grid = grids.get(sheet)
        if grid is None or not ref:
            continue
        try:
            r, c = parse_ref(ref.replace("$", "").split(":")[0])
        except (ValueError, IndexError):
            continue
        cell = grid.get(r, c)
        if cell is None:
            continue
        for tok in _cell_tokens(cell.value):
            if tok:
                text = re.sub(r"(?<![0-9A-Za-z])" + re.escape(tok) + r"(?![0-9])", "<值>", text)
    return text


def problem_lines_for_model(problems: list[Problem], grids: dict[str, Grid]) -> list[str]:
    """干跑问题的模型版：code + model_message + 坐标；model_message 为空时只有 code 和坐标，绝不退回 message。"""
    out: list[str] = []
    for p in problems[:MODEL_LINES_MAX]:
        cells = "、".join(p.cells[:20])
        line = (MODEL_LINE_PROMPT["dry_msg"].format(code=p.code, message=p.model_message) if p.model_message
                else MODEL_LINE_PROMPT["dry"].format(code=p.code))
        if cells:
            line = MODEL_LINE_PROMPT["cells"].format(line=line, cells=cells)
        out.append(scrub_cells(line, p.cells, grids))
    return out


# ==========================================================================
# 卡片与问题（任意配方）
# ==========================================================================


@dataclass
class _CrossLoc:
    axis_row: int
    label_col: int
    c1: int
    c2: int
    dates: list[int]
    seg_rows: dict[str, list[int]] = field(default_factory=dict)
    title_rows: dict[str, int] = field(default_factory=dict)
    data_rows: list[int] = field(default_factory=list)
    region: set[tuple[int, int]] = field(default_factory=set)


@dataclass
class _ListLoc:
    header_rows: list[int]
    cols: list[int]
    rows: list[int]
    total_row: int | None
    region: set[tuple[int, int]] = field(default_factory=set)


def _find_grid(name: str, grids: dict[str, Grid]) -> Grid | None:
    for g in grids.values():
        if P.match_key(g.sheet) == P.match_key(name):
            return g
    live = [g for g in grids.values() if g.cells]
    return live[0] if len(live) == 1 else None


def _locate_crosstab(block: CrosstabBlock, grid: Grid) -> _CrossLoc | None:
    best: tuple[int, list[int]] | None = None
    for r in grid.row_numbers():
        cols = [c for c, cell in grid.row(r) if P.month_day_or_date(cell.value) is not None]
        if len(cols) < block.axis.find.min:
            continue
        right = [c for c, _ in grid.row(r) if c >= min(cols) - 1]
        if len(cols) * 2 <= len(right):
            continue
        if best is None or len(cols) > len(best[1]):
            best = (r, cols)
    if best is None:
        return None
    axis_row, dates = best
    c1, c2 = min(dates), max(dates)
    loc = _CrossLoc(axis_row, c1 - 1, c1, c2, dates)
    if loc.label_col < 1:
        return None

    def parse_key(seg: Any, label: str) -> str | None:
        if isinstance(seg, MeasuresSegment):
            return P.match_key(label)
        if isinstance(seg, DimensionSegment):
            if seg.dim.parser == "hour_range":
                hr = P.hour_range(label)
                return hr.canonical if hr else None
            return P.text_label(label)
        hr = P.hour_range_total(label)
        return hr.canonical if hr else None

    expects: list[tuple[Any, set[str]]] = []
    for seg in block.segments:
        keys = {k for e in seg.labels.expect if (k := parse_key(seg, e)) is not None}
        expects.append((seg, keys))
    kinds: dict[int, str] = {}
    labels: dict[int, str] = {}
    for r in range(axis_row + 1, _bottom(grid) + 1):
        label = _label_of(grid.get(r, loc.label_col))
        has = any(grid.get(r, c) is not None for c in range(c1, c2 + 1))
        if label is None:
            kinds[r] = "orphan" if has else "blank"
            continue
        labels[r] = label
        if has or any(parse_key(seg, label) in keys for seg, keys in expects):
            kinds[r] = "data"
        else:
            kinds[r] = "title"

    def run_from(start: int, accept: Callable[[str], bool] | None = None,
                 stop: Callable[[str], bool] | None = None) -> list[int]:
        out: list[int] = []
        r = start
        while kinds.get(r) == "data":
            lab = labels[r]
            if stop is not None and stop(lab):
                break
            if accept is not None and not accept(lab):
                break
            out.append(r)
            r += 1
        return out

    for seg, keys in expects:
        rows: list[int] = []
        if isinstance(seg, DerivedSegment):
            prev = loc.seg_rows.get(seg.locate.segment or "")
            if prev:
                rows = run_from(prev[-1] + 1, accept=lambda lab: P.hour_range_total(lab) is not None)
        elif seg.locate.by == "section_title":
            want = P.match_key(seg.locate.title or "")
            tr = next((r for r, k in kinds.items() if k == "title" and P.match_key(labels[r]) == want), None)
            if tr is not None:
                loc.title_rows[seg.id] = tr
                stop = ((lambda lab: P.hour_range_total(lab) is not None)
                        if isinstance(seg, DimensionSegment) and seg.stop_parser else None)
                rows = run_from(tr + 1, stop=stop)
        else:
            first = next((r for r, k in kinds.items() if k == "data" and parse_key(seg, labels[r]) in keys), None)
            if first is not None:
                start = first
                while kinds.get(start - 1) == "data":
                    start -= 1
                rows = run_from(start)
        loc.seg_rows[seg.id] = rows
        if not isinstance(seg, DerivedSegment):
            loc.data_rows.extend(rows)
    loc.region.update((axis_row, c) for c in range(loc.label_col, c2 + 1))
    for rows in loc.seg_rows.values():
        for r in rows:
            loc.region.update((r, c) for c in range(loc.label_col, c2 + 1))
    for tr in loc.title_rows.values():
        loc.region.add((tr, loc.label_col))
    return loc


def _locate_list(block: ListBlock, grid: Grid, taken: set[int]) -> _ListLoc | None:
    n = block.header_rows
    want = [P.match_key(c.header) for c in block.columns]
    start = grid.row_numbers()[0] if grid.row_numbers() else 1
    if block.after_title:
        key = P.match_key(block.after_title)
        hit = next((r for r in grid.row_numbers() if any(P.match_key(_label_of(cell) or "") == key
                                                            for _, cell in grid.row(r))), None)
        if hit is not None:
            start = hit + 1
    best: tuple[int, list[int], dict[int, str]] | None = None
    best_hits = 0
    want_set = set(want)
    for r in grid.row_numbers():
        if r < start + n - 1 or r in taken:
            continue
        if n == 1 and not any(P.match_key(_label_of(cell) or "") in want_set for _, cell in grid.row(r)):
            continue
        hrows = list(range(r - n + 1, r + 1))
        cols = sorted({c for hr in hrows for c, _ in grid.row(hr)})
        texts = _header_texts(grid, hrows, cols)
        keys = {P.match_key(t): c for c, t in texts.items()}
        hits = sum(1 for k in want if k in keys)
        if hits * 2 > len(want) and hits > best_hits:
            best, best_hits = (r, hrows, texts), hits
    if best is None:
        return None
    r, hrows, texts = best
    keys = {P.match_key(t): c for c, t in texts.items()}
    anchor = [keys[k] for k in want if k in keys]
    lo, hi = min(anchor), max(anchor)
    while lo - 1 in texts:
        lo -= 1
    while hi + 1 in texts:
        hi += 1
    cols = list(range(lo, hi + 1))
    rows: list[int] = []
    total = None
    bottom = _bottom(grid)
    rr = r + 1
    pick = P.match_key(block.rows.total_row.pick) if block.rows.total_row else None
    while rr <= bottom:
        if all(grid.get(rr, c) is None for c in cols):
            if block.rows.blank_rows == "skip":
                rr += 1
                continue
            break
        if pick and P.match_key(_label_of(grid.get(rr, cols[0])) or "").startswith(pick):
            total = rr
            break
        rows.append(rr)
        rr += 1
    loc = _ListLoc(hrows, cols, rows, total)
    for row in [*hrows, *rows, *([total] if total else [])]:
        loc.region.update((row, c) for c in cols)
    return loc


def _registered_relation(fact: Fact, recipe: Recipe, rid: str) -> dict[str, Any] | None:
    """按事实和配方拼出「登记」时的整条关系（sum_eq / not_comparable）；拼不出来时 None。"""
    measures: list[tuple[MeasuresSegment, CrosstabBlock]] = []
    dims: list[DimensionSegment] = []
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, CrosstabBlock):
                for seg in block.segments:
                    if isinstance(seg, MeasuresSegment):
                        measures.append((seg, block))
                    elif isinstance(seg, DimensionSegment):
                        dims.append(seg)

    def measure_col(seg: MeasuresSegment, label: str) -> str | None:
        keys = {P.match_key(e): e for e in seg.labels.expect}
        e = keys.get(P.match_key(label))
        return seg.measures.get(e) if e is not None else None

    def find(seg_id: str, labels: list[str]) -> tuple[MeasuresSegment, CrosstabBlock] | None:
        for seg, block in measures:
            if seg.id == seg_id:
                return seg, block
        for seg, block in measures:
            if all(measure_col(seg, lab) for lab in labels):
                return seg, block
        return None

    d = fact.detail
    if fact.kind == "sum_eq":
        hit = find(d.get("segment", ""), [d.get("total", ""), *d.get("parts", [])])
        if hit is None:
            return None
        seg, _ = hit
        total = measure_col(seg, d.get("total", ""))
        parts = [measure_col(seg, p) for p in d.get("parts", [])]
        if total is None or None in parts:
            return None
        return {"id": rid, "kind": "sum_eq", "table": seg.table, "total": total, "parts": parts, "claims": fact.id}
    hit = find(d.get("b_segment", ""), [d.get("b", "")])
    a = next((s for s in dims if s.id in d.get("a", [])), None)
    if hit is None or a is None:
        return None
    seg, block = hit
    b_col = measure_col(seg, d.get("b", ""))
    if b_col is None:
        return None
    return {"id": rid, "kind": "not_comparable", "a": {"table": a.table, "value": a.value},
            "b": {"table": seg.table, "value": b_col}, "by": block.axis.name, "claims": fact.id}


def _add_ops(recipe: dict[str, Any], parts: list[Any], value: Any) -> list[dict[str, Any]]:
    """把 parts 指向的成员设成 value：父对象都在时一条 add；缺了哪一层就在那一层 add 一个嵌套对象。"""
    node: Any = recipe
    for i, key in enumerate(parts[:-1]):
        nxt = node[key] if isinstance(node, list) and isinstance(key, int) and key < len(node) else (
            node.get(key) if isinstance(node, dict) else None)
        if nxt is None:
            nested: Any = value
            for k in reversed(parts[i + 1:]):
                nested = {k: nested}
            return [{"op": "add", "path": _ptr(*parts[: i + 1]), "value": nested}]
        node = nxt
    return [{"op": "add", "path": _ptr(*parts), "value": value}]


def questions_for(recipe: dict[str, Any], facts: DraftFacts, grids: dict[str, Grid], *,
                  extraction: Extraction | None = None) -> tuple[list[Card], list[Question]]:
    """给任意配方生成建议卡片和待确认问题。配方不合法（pydantic 不收）时只给导入模式卡片。"""
    cards: list[Card] = []
    questions: list[Question] = []
    try:
        parsed = Recipe.model_validate(recipe)
    except ValidationError:
        return [_mode_card(None)], []
    relations = recipe.get("relations") if isinstance(recipe.get("relations"), list) else []
    claims = {r.claims: i for i, r in enumerate(parsed.relations) if r.claims}
    next_rid = [max([int(r.id[1:]) for r in parsed.relations] + [0])]
    facts_left = list(facts.facts)
    ext = extraction
    used: set[str] = set()

    def relation_question(f: Fact) -> Question:
        idx = claims.get(f.id)
        rid = parsed.relations[idx].id if idx is not None else None
        if rid is None:
            next_rid[0] += 1
            rid = f"R{next_rid[0]}"
        current = parsed.relations[idx] if idx is not None else None
        reg = (relations[idx] if current is not None and current.kind in ("sum_eq", "not_comparable")
               and idx < len(relations) else _registered_relation(f, parsed, rid))
        dismissed = {"id": rid, "kind": "dismissed", "claims": f.id, "reason": REASON_SLOT}

        def put(value: dict[str, Any]) -> list[dict[str, Any]]:
            if idx is not None:
                return [{"op": "replace", "path": _ptr("relations", idx), "value": value}]
            if "relations" not in recipe:
                return [{"op": "add", "path": "/relations", "value": [value]}]
            return [{"op": "add", "path": "/relations/-", "value": value}]

        verb = "每期核对" if f.kind == "sum_eq" else "口径不同"
        options = []
        effects: dict[str, list[dict[str, Any]]] = {}
        if reg is not None:
            options.append({"value": "register", "label": "登记", "needs_reason": False})
            effects["register"] = put(reg)
        options.append({"value": "dismiss", "label": "不登记", "needs_reason": True})
        effects["dismiss"] = put(dismissed)
        return Question(f"q_relation:{f.id}", f"{f.text}：要登记为{verb}吗", options, None, effects)

    for si, sheet in enumerate(parsed.sheets):
        grid = _find_grid(sheet.match.name, grids)
        name = grid.sheet if grid is not None else sheet.match.name
        region: set[tuple[int, int]] = set()
        cross_locs: dict[int, _CrossLoc] = {}
        list_locs: dict[int, _ListLoc] = {}
        taken: set[int] = set()
        if grid is not None:
            for bi, block in enumerate(sheet.blocks):
                if isinstance(block, CrosstabBlock):
                    cl = _locate_crosstab(block, grid)
                    if cl is not None:
                        cross_locs[bi] = cl
                        region |= cl.region
                else:
                    ll = _locate_list(block, grid, taken)
                    if ll is not None:
                        list_locs[bi] = ll
                        region |= ll.region
                        taken.update(ll.header_rows)
        sheet_facts = [f for f in facts_left if P.match_key(f.sheet) == P.match_key(name)]
        facts_left = [f for f in facts_left if f not in sheet_facts]

        for bi, block in enumerate(sheet.blocks):
            if isinstance(block, CrosstabBlock):
                cards_q = _crosstab_cards(si, bi, sheet, block, cross_locs.get(bi), grid, name, recipe, parsed,
                                          sheet_facts, relation_question, ext, region, used)
            else:
                cards_q = _list_cards(si, bi, sheet, block, list_locs.get(bi), grid, name, recipe, ext, used)
            cards.extend(cards_q[0])
            questions.extend(cards_q[1])
        if not any(isinstance(b, CrosstabBlock) for b in sheet.blocks):
            facts_left.extend(sheet_facts)     # 没有交叉表来认领：放到最后统一出问题
            # 列表工作表也可能有统计期
            c, q = _period_cards(si, sheet, grid, name, region, ext)
            cards.extend(c)
            questions.extend(q)
            c2 = _outside_card(sheet, grid, name, region, ext)
            cards.extend(c2)
        # 隐藏行列
        hq = _hidden_question(si, sheet, grid, name, region, ext)
        if hq is not None:
            cards.append(hq[0])
            questions.append(hq[1])
        # 干跑报的「没有去处」的格
        if ext is not None:
            bad = [p for p in ext.problems if p.code in ("cell_unclaimed", "row_unclaimed")
                   and any(c.startswith(f"{name}!") for c in p.cells)]
            if bad:
                cells = [c for p in bad for c in p.cells][:CARD_CELLS_MAX]
                cards.append(Card(f"unclaimed:{sheet.id}", "有单元格没有去处",
                                  "；".join(p.message for p in bad[:5]), cells))
    # 没落到任何工作表的事实（工作表改名等）也要能认领
    for f in facts_left:
        q = relation_question(f)
        questions.append(q)
        cards.append(Card(f"fact:{f.id}", f.text, "系统发现的关系需要逐条认领：登记，或写明理由不登记",
                          [], q.id))
    if ext is not None and ext.sheets.skipped_hidden:
        for nm in _dedupe([item.get("sheet", "") for item in ext.sheets.skipped_hidden]):
            cards.append(_hidden_sheet_card(nm))
    blockers = accumulate_blockers(parsed)
    cards.append(_mode_card(blockers))
    if not blockers:
        # q_mode 固定排在问题列表最后：期 2 的测试和性能用例取 questions[0]，不能被它顶掉（评审二-B2）
        questions.append(_mode_question())
    return cards, questions


def _qid(base: str, block_id: str, used: set[str]) -> str:
    """问题 id 在整份配方内唯一：同一个占位符出现在两个块里时，后面的带上块 id。"""
    qid = base if base not in used else f"{base}@{block_id}"
    used.add(qid)
    return qid


#: 导入模式两种取值的后果（P3-SPEC 2.2，评审三-m4）：卡片的理由里写，确认项 mode 的 detail（WP-3）和界面
#: q_mode 选项下方的说明（WP-6）也是这两句
MODE_CONSEQUENCE = {
    "accumulate": "每期以统计期为键加入当前版本；统计期与已有各期部分重叠的文件会被拒收，同一统计期再传要确认替换",
    "replace": "每次上传新一期，当前版本只含新的一期，此前各期留在历史版本中",
}


def _mode_question() -> Question:
    """q_mode：符合按期累积资格时才问。effects 作用在 answers_base（完整形式，/mode 总在）上；用 add 是为了
    answers_base 不合 schema、原样保存时也能应用（add 到已有键等于替换，RFC 6902）。没有默认选中：没回答时，
    草稿里的取值生效（起草器在符合资格时已写 accumulate）。"""
    return Question("q_mode", "这份报表每期怎么更新",
                    [{"value": "accumulate", "label": "按期累积（建议）", "needs_reason": False},
                     {"value": "replace", "label": "每期替换", "needs_reason": False}],
                    None,
                    {"accumulate": [{"op": "add", "path": "/mode", "value": "accumulate"}],
                     "replace": [{"op": "add", "path": "/mode", "value": "replace"}]})


def _mode_card(blockers: list[RecipeProblem] | None) -> Card:
    """导入模式卡片。blockers 为空（符合资格）时建议按期累积并指向 q_mode；不符合时写第一条原因；
    None 表示配方还没过 schema，判断不了。"""
    if blockers is not None and not blockers:
        return Card("mode", "导入模式：按期累积（建议）",
                    "这是按统计期出的报表：每期以统计期为键累积，跨期可以比较；也可以选每期替换。"
                    f"按期累积：{MODE_CONSEQUENCE['accumulate']}。每期替换：{MODE_CONSEQUENCE['replace']}",
                    question="q_mode")
    if blockers is None:
        reason = "配方还不完整，暂时无法判断能否按期累积"
    else:
        reason = blockers[0].message
    return Card("mode", "导入模式：每期替换", f"{reason}。每期替换：{MODE_CONSEQUENCE['replace']}")


def _period_cells(grid: Grid | None, name: str, region: set[tuple[int, int]],
                  ext: Extraction | None) -> list[str]:
    """统计期取自哪些格：有干跑就用执行器的结论；否则在区域外找能解析出区间的格。"""
    if ext is not None:
        if ext.period is None or ext.period.source != "cells":
            return []
        return [c if "!" in c else f"{name}!{c}" for c in ext.period.cells
                if "!" not in c or c.startswith(f"{name}!")]
    if grid is None:
        return []
    out = []
    for (r, c), cell in sorted(grid.cells.items()):
        if (r, c) not in region and isinstance(cell.value, str) and P.cn_date_range(cell.value) is not None:
            out.append(_coord(name, r, c))
    return out


def _cell_text(grid: Grid | None, coord: str) -> str:
    if grid is None:
        return ""
    try:
        r, c = parse_ref(coord.rpartition("!")[2])
    except ValueError:
        return ""
    cell = grid.get(r, c)
    return _shown(cell.value) if cell is not None and cell.value is not None else ""


def _period_cards(si: int, sheet: Any, grid: Grid | None, name: str, region: set[tuple[int, int]],
                  ext: Extraction | None) -> tuple[list[Card], list[Question]]:
    if not sheet.context:
        return [], []
    ctx = sheet.context[0]
    cells = _period_cells(grid, name, region, ext)
    if cells:
        shown = "、".join(c.rpartition("!")[2] for c in cells[:5])
        texts = "；".join(f"{c.rpartition('!')[2]}「{_cell_text(grid, c)}」" for c in cells[:5])
        reason = f"能解析出日期区间的格：{texts}"
        if ctx.cross_check == "filename":
            reason += "。另与文件名里的日期区间核对"
        return [Card(f"period:{sheet.id}", f"统计期取自 {shown}", reason, cells[:CARD_CELLS_MAX])], []
    q = Question(f"q_period:{sheet.id}" if si else "q_period", "表格里找不到统计期，每次导入时怎么确定",
                 [{"value": "human", "label": "每期人工录入", "needs_reason": False}], "human", {"human": []})
    return [Card(f"period:{sheet.id}", "未找到统计期：每期人工录入",
                 "表格里没有能解析出日期区间的文字，每次导入时需要人工录入本期的统计期", [], q.id)], [q]


def _outside_card(sheet: Any, grid: Grid | None, name: str, region: set[tuple[int, int]],
                  ext: Extraction | None) -> list[Card]:
    items: list[tuple[str, str, bool]] = []      # (坐标, 全文, 含数字)
    if ext is not None:
        for o in ext.outside_text:
            if o.sheet == name:
                items.append((f"{o.sheet}!{o.cell}" if "!" not in o.cell else o.cell, o.text,
                              o.kind == "text_digits"))
    elif grid is not None:
        for (r, c), cell in sorted(grid.cells.items()):
            if (r, c) in region or cell.formula is not None:
                continue
            v = cell.value
            if isinstance(v, str) and P.cn_date_range(v) is not None and P.is_pure_period_cell(
                    v, sheet.context[0].prefer_prefix if sheet.context else None):
                continue
            kind = P.classify_outside(v)
            if kind == "number":
                continue
            items.append((_coord(name, r, c), _shown(v), kind == "text_digits"))
    if not items:
        return []
    refs = [i[0].rpartition("!")[2] for i in items]
    head = "、".join(refs[:5]) + (f" 等 {len(refs)} 格" if len(refs) > 5 else "")
    reason = "；".join(f"{i[0].rpartition('!')[2]}「{i[1][:40]}」" for i in items[:5])
    reason += "：不在任何数据区内，记录在回执里，不导入"
    if any(i[2] for i in items):
        reason += "。其中含数字的文字需要在提交时逐条确认"
    return [Card(f"outside:{sheet.id}", f"{head} 是区域外文字", reason, [i[0] for i in items][:CARD_CELLS_MAX])]


def _hidden_question(si: int, sheet: Any, grid: Grid | None, name: str, region: set[tuple[int, int]],
                     ext: Extraction | None) -> tuple[Card, Question] | None:
    rows: list[int] = []
    cols: list[int] = []
    if ext is not None and name in ext.hidden:
        rows = list(ext.hidden[name].get("rows") or [])
        cols = list(ext.hidden[name].get("cols") or [])
    elif grid is not None:
        rows_in = {r for r, _ in region}
        cols_in = {c for _, c in region}
        rows = sorted(r for r in grid.hidden_rows if r in rows_in)
        cols = sorted(c for c in grid.hidden_cols if c in cols_in)
    if not rows and not cols:
        return None
    parts = []
    if rows:
        parts.append(f"隐藏行：{_rows_text([int(r) for r in rows])}")
    if cols:
        parts.append("隐藏列：" + "、".join(col_letter(c) if isinstance(c, int) else str(c) for c in cols[:10]))
    options = [{"value": "reject_if_any", "label": "拒收（文件里不应有隐藏的行列）", "needs_reason": False},
               {"value": "include", "label": "照常导入隐藏的行列", "needs_reason": False}]
    policy = {"reject_if_any": {"rows": "reject_if_any", "cols": "reject_if_any"},
              "include": {"rows": "include", "cols": "include"}}
    if rows:
        options.append({"value": "exclude", "label": "跳过隐藏的行", "needs_reason": False})
        policy["exclude"] = {"rows": "exclude", "cols": "include" if cols else "reject_if_any"}
    effects = {k: [{"op": "add", "path": _ptr("sheets", si, "hidden"), "value": v}] for k, v in policy.items()}
    q = Question(f"q_hidden:{name}", f"工作表「{name}」的数据区里有隐藏的行或列，怎么处理", options, None, effects)
    card = Card(f"hidden:{name}", "数据区里有隐藏的行或列", "；".join(parts) + "。请选择怎么处理", [], q.id)
    return card, q


def _unit_cards(labels: list[tuple[str, str]], sheet_id: str) -> list[Card]:
    out = []
    for label, where in labels:
        _, unit = P.split_unit_suffix(label)
        if unit is not None and not P.unit_known(unit):
            out.append(Card(f"unit_unknown:{sheet_id}:{label}", f"「{label}」的单位「{unit}」不在单位词表中",
                            f"{where}：列名保留原文，单位不写进单位字段"))
    return out


def _crosstab_cards(si: int, bi: int, sheet: Any, block: CrosstabBlock, loc: _CrossLoc | None, grid: Grid | None,
                    name: str, recipe: dict[str, Any], parsed: Recipe, sheet_facts: list[Fact],
                    relation_question: Callable[[Fact], Question], ext: Extraction | None,
                    region: set[tuple[int, int]], used: set[str]) -> tuple[list[Card], list[Question]]:
    cards: list[Card] = []
    questions: list[Question] = []
    lc = loc.label_col if loc is not None else None

    def label_cells(rows: list[int]) -> list[str]:
        return _coords(name, [(r, lc) for r in rows]) if lc is not None else []

    # 1 日期表头
    axis_first = axis_last = ""
    count = 0
    axis_cells: list[str] = []
    if ext is not None and (ax := next((a for a in ext.axes if a.block == block.id), None)) is not None:
        axis_first, axis_last, count = ax.first, ax.last, ax.count
        row = ax.row
        axis_cells = [_coord(name, row, c) for c in (loc.dates if loc else [])][:CARD_CELLS_MAX]
    elif loc is not None and grid is not None:
        row = loc.axis_row
        count = len(loc.dates)
        axis_first = _shown(grid.get(row, loc.dates[0]).value)        # type: ignore[union-attr]
        axis_last = _shown(grid.get(row, loc.dates[-1]).value)        # type: ignore[union-attr]
        axis_cells = _coords(name, [(row, c) for c in loc.dates])
    else:
        row = 0
    measures = [s for s in block.segments if isinstance(s, MeasuresSegment)]
    has_unit = any(P.unit_known(P.split_unit_suffix(e)[1]) for s in measures for e in s.labels.expect)
    if row:
        reason = f"第 {row} 行有 {count} 格日期（{axis_first} 至 {axis_last}）"
        if lc is not None:
            reason += f"，行标签在 {col_letter(lc)} 列"
        if block.axis.year_from and grid is not None and loc is not None and any(
                (cell := grid.get(loc.axis_row, c)) is not None
                and (d := P.month_day_or_date(cell.value)) is not None and d.year is None for c in loc.dates):
            reason += "；日期只写了月日，年份取自统计期"
        if has_unit:
            reason += "；行标签末尾括号里的单位拆进单位字段，列名去掉单位后缀"
        cards.append(Card(f"axis:{block.id}", f"第 {row} 行是日期表头，按交叉表导入", reason, axis_cells))
    else:
        cards.append(Card(f"axis:{block.id}", "按交叉表导入", "表格里没有找到符合配方的日期表头行，试运行时会报出具体原因"))
    cards.extend(_unit_cards([(e, f"分段「{s.id}」") for s in measures for e in s.labels.expect], sheet.id))

    # 2 统计期
    c, q = _period_cards(si, sheet, grid, name, region, ext)
    cards.extend(c)
    questions.extend(q)

    # 3 分段标题
    dims = [s for s in block.segments if isinstance(s, DimensionSegment)]
    titled = [s for s in dims if s.locate.by == "section_title"]
    if titled:
        rows = [loc.title_rows[s.id] for s in titled if loc is not None and s.id in loc.title_rows]
        refs = "、".join(_a1(r, lc) for r in rows) if lc is not None and rows else ""
        title = f"分段标题 {refs}" if refs else "分段标题"
        reason = "".join(f"「{s.locate.title}」" for s in titled) + "下面的行各成一段，按标题定位"
        cards.append(Card(f"titles:{block.id}", title, reason, label_cells(rows)))
    cards.extend(_unit_cards([(s.locate.title or "", f"分段「{s.id}」") for s in titled], sheet.id))

    # 4 sum_eq
    for f in sheet_facts:
        if f.kind != "sum_eq":
            continue
        q = relation_question(f)
        questions.append(q)
        cards.append(Card(f"fact:{f.id}", f.text,
                          "建议登记为每期核对：以后每期导入都会逐行核对这个等式；如果只是巧合，可以选择不登记并写明理由",
                          label_cells(list(f.detail.get("rows", []))), q.id))

    # 5 合表
    by_table: dict[str, list[DimensionSegment]] = {}
    for s in dims:
        by_table.setdefault(s.table, []).append(s)
    for tname, segs in by_table.items():
        if len(segs) < 2:
            continue
        picks = ["、".join(v.pick for v in s.const.values()) or s.id for s in segs]
        consts = sorted({k for s in segs for k in s.const})
        reason = f"这几段的{segs[0].dim.name}互不重叠，写进同一张表"
        if consts:
            reason += "，用列" + "、".join(f"「{k}」" for k in consts) + "区分，取值从分段标题的候选词里选：" + "、".join(picks)
        rows = [loc.title_rows[s.id] for s in segs if loc is not None and s.id in loc.title_rows]
        cards.append(Card(f"merge:{tname}", f"{'、'.join(picks)}合进一张表「{tname}」", reason, label_cells(rows)))

    # 6 占位符（按下标从大到小出问题：remove 重放不错位）
    phs = list(enumerate(block.values.placeholders))
    ph_cards: list[Card] = []
    ph_qs: list[Question] = []
    for pi, ph in phs:
        key = canon(ph.text)
        cells: list[tuple[int, int]] = []
        if loc is not None and grid is not None:
            for r in loc.data_rows:
                for cc in range(loc.c1, loc.c2 + 1):
                    cell = grid.get(r, cc)
                    if cell is not None and isinstance(cell.value, str) and canon(cell.value) == key:
                        cells.append((r, cc))
        n = ext.placeholders.get(ph.text, len(cells)) if ext is not None else len(cells)
        qid = _qid(f"q_placeholder:{ph.text}", block.id, used)
        ph_cards.append(Card(f"placeholder:{block.id}:{ph.text}", f"「{ph.text}」表示无数据吗",
                             f"数值区有 {n} 格「{ph.text}」：确认后这些格存为空值；"
                             "如果它不是「无数据」的意思，这份文件无法按当前配方导入",
                             _coords(name, cells), qid))
        path = ["sheets", si, "blocks", bi, "values", "placeholders", pi]
        ph_qs.append(Question(qid, f"「{ph.text}」表示无数据吗",
                              [{"value": "null", "label": "是，存为空值", "needs_reason": False},
                               {"value": "reject", "label": "不是，不导入", "needs_reason": False}],
                              None, {"null": [], "reject": [{"op": "remove", "path": _ptr(*path)}]}))
    cards.extend(ph_cards)
    questions.extend(reversed(ph_qs))

    # 7 合计段
    for s in block.segments:
        if not isinstance(s, DerivedSegment):
            continue
        rows = []
        if ext is not None:
            lab = next((x for x in ext.labels if x.segment == s.id), None)
            rows = list(lab.rows) if lab is not None else []
        if not rows and loc is not None:
            rows = loc.seg_rows.get(s.id, [])
        where = _rows_text(rows) or f"分段「{s.id}」"
        first = s.labels.expect[0] if s.labels.expect else ""
        reason = f"「{first}」等 {len(s.labels.expect)} 行按时段区间与上方明细逐日核对"
        reason += f"，原值另存表「{s.keep_as.table}」" if s.keep_as else "，只核对不另存"
        cards.append(Card(f"derived:{s.id}", f"{where}是合计，改作核对并另存" if s.keep_as else f"{where}是合计，改作核对",
                          reason, label_cells(rows)))

    # 8 not_equal_sum
    for f in sheet_facts:
        if f.kind != "not_equal_sum":
            continue
        q = relation_question(f)
        questions.append(q)
        a = next((s for s in dims if s.id in f.detail.get("a", [])), None)
        bseg = next((s for s in measures if s.id == f.detail.get("b_segment")), None)
        bcol = None
        if bseg is not None:
            keys = {P.match_key(e): e for e in bseg.labels.expect}
            e = keys.get(P.match_key(f.detail.get("b", "")))
            bcol = bseg.measures.get(e) if e else None
        unit = "天" if block.axis.type == "DATE" else "组"
        if a is not None and bcol:
            title = (f"各{a.dim.name}之和与{bcol} {f.detail.get('checked', 0)} {unit}中 "
                     f"{f.detail.get('equal', 0)} {unit}相等")
        else:
            title = f.text
        rows = [loc.title_rows[s.id] for s in dims if loc is not None and s.id in loc.title_rows
                and s.id in f.detail.get("a", [])]
        cards.append(Card(f"fact:{f.id}", title,
                          "两者的口径不同（例如统计范围不同），建议登记为口径不同：以后每期只记录相等的天数，不作核对；"
                          "如果判断为巧合，可以选择不登记并写明理由", label_cells(rows), q.id))

    # 9 区域外文字
    cards.extend(_outside_card(sheet, grid, name, region, ext))

    # 数值区的空格、公式（规格没有给默认处理：保守地按拒收起草，出问题让人选）
    if loc is not None and grid is not None:
        blanks = [(r, cc) for r in loc.data_rows for cc in range(loc.c1, loc.c2 + 1) if grid.get(r, cc) is None]
        formulas = [(r, cc) for r in loc.data_rows for cc in range(loc.c1, loc.c2 + 1)
                    if (cell := grid.get(r, cc)) is not None and cell.formula is not None]
        vpath = ["sheets", si, "blocks", bi, "values"]
        if blanks:
            q = Question(f"q_blank:{block.id}", f"数值区有 {len(blanks)} 个空格，怎么处理",
                         [{"value": "null", "label": "存为空值", "needs_reason": False},
                          {"value": "reject", "label": "拒收（文件里不应有空格）", "needs_reason": False}], None,
                         {"null": _add_ops(recipe, [*vpath, "blank"], "null"),
                          "reject": _add_ops(recipe, [*vpath, "blank"], "reject")})
            questions.append(q)
            cards.append(Card(f"blank:{block.id}", f"数值区有 {len(blanks)} 个空格",
                              f"{_a1_list(blanks)} 是空的：存为空值，还是拒收", _coords(name, blanks), q.id))
        if formulas:
            q = Question(f"q_formula:{block.id}", f"数值区有 {len(formulas)} 个公式格，怎么处理",
                         [{"value": "accept_cached", "label": "按保存值导入", "needs_reason": False},
                          {"value": "reject", "label": "拒收（可能是没认出来的合计）", "needs_reason": False}], None,
                         {"accept_cached": _add_ops(recipe, [*vpath, "formula"], "accept_cached"),
                          "reject": _add_ops(recipe, [*vpath, "formula"], "reject")})
            questions.append(q)
            cards.append(Card(f"formula:{block.id}", f"数值区有 {len(formulas)} 个公式格",
                              f"{_a1_list(formulas)} 是公式：按保存值导入，还是拒收", _coords(name, formulas), q.id))
    return cards, questions


def _list_cards(si: int, bi: int, sheet: Any, block: ListBlock, loc: _ListLoc | None, grid: Grid | None, name: str,
                recipe: dict[str, Any], ext: Extraction | None, used: set[str]) -> tuple[list[Card], list[Question]]:
    cards: list[Card] = []
    questions: list[Question] = []
    headers = "、".join(f"「{c.header}」" for c in block.columns[:12])
    if loc is not None:
        where = _rows_text(loc.header_rows)
        rows = f"；{_rows_text(loc.rows)}是数据" if loc.rows else ""
        cards.append(Card(f"list:{block.id}", f"{where}是表头，按列表导入（表「{block.table}」）",
                          f"列：{headers}{rows}", _coords(name, [(r, c) for r in loc.header_rows for c in loc.cols])))
    else:
        cards.append(Card(f"list:{block.id}", f"按列表导入（表「{block.table}」）",
                          f"列：{headers}。表格里没有找到这组表头，试运行时会报出具体原因"))
    cards.extend(_unit_cards([(c.header, f"列「{c.name}」") for c in block.columns], sheet.id))
    years = [c.header for c in block.columns if _YEAR.search(c.header)]
    if years:
        cards.append(Card(f"header_year:{block.id}", "表头里含年份：下一期年份变化时将无法匹配",
                          "、".join(f"「{h}」" for h in years[:6]) + "。下一期表头里的年份变了，需要修改配方"))
    # 占位符
    ph_qs: list[Question] = []
    for pi, ph in enumerate(block.values.placeholders):
        cells: list[tuple[int, int]] = []
        if loc is not None and grid is not None:
            for r in loc.rows:
                for c in loc.cols:
                    cell = grid.get(r, c)
                    if cell is not None and isinstance(cell.value, str) and canon(cell.value) == canon(ph.text):
                        cells.append((r, c))
        n = ext.placeholders.get(ph.text, len(cells)) if ext is not None else len(cells)
        qid = _qid(f"q_placeholder:{ph.text}", block.id, used)
        cards.append(Card(f"placeholder:{block.id}:{ph.text}", f"「{ph.text}」表示无数据吗",
                          f"数字列里有 {n} 格「{ph.text}」：确认后这些格存为空值", _coords(name, cells), qid))
        path = ["sheets", si, "blocks", bi, "values", "placeholders", pi]
        ph_qs.append(Question(qid, f"「{ph.text}」表示无数据吗",
                              [{"value": "null", "label": "是，存为空值", "needs_reason": False},
                               {"value": "reject", "label": "不是，不导入", "needs_reason": False}],
                              None, {"null": [], "reject": [{"op": "remove", "path": _ptr(*path)}]}))
    questions.extend(reversed(ph_qs))
    if block.merged_data == "fill":
        q = Question(f"q_merged:{block.id}", "数据区的文字列有纵向合并的单元格，怎么处理",
                     [{"value": "fill", "label": "用合并区左上格的值填充", "needs_reason": False},
                      {"value": "reject", "label": "拒收", "needs_reason": False}], None,
                     {"fill": _add_ops(recipe, ["sheets", si, "blocks", bi, "merged_data"], "fill"),
                      "reject": _add_ops(recipe, ["sheets", si, "blocks", bi, "merged_data"], "reject")})
        questions.append(q)
        cards.append(Card(f"merged:{block.id}", "文字列有纵向合并的单元格",
                          "合并区里每一行都按左上格的值填充（例如按地区分组时，地区只写在第一行）", [], q.id))
    if block.rows.blank_rows == "skip":
        n = ext.blank_rows_skipped if ext is not None else None
        extra = f"（本期 {n} 行）" if n else ""
        cards.append(Card(f"blank_skip:{block.id}", "数据中间有空行，跳过继续",
                          f"空行下面的行与上方同类型，按同一张表继续读取{extra}；提交时需要确认"))
    total = block.rows.total_row
    if total is not None:
        where = _rows_text([loc.total_row]) if loc is not None and loc.total_row else f"「{total.pick}」开头的行"
        reason = f"「{total.pick}」行按列求和与上方明细核对"
        reason += f"，原值另存表「{total.keep_as}」" if total.keep_as else "，只核对不另存"
        cells = _coords(name, [(loc.total_row, loc.cols[0])]) if loc is not None and loc.total_row else []
        cards.append(Card(f"total_row:{block.id}", f"{where}是合计行，改作核对", reason, cells))
    return cards, questions


# ==========================================================================
# 期 3：按规则重新起草，并把名字对齐到现行配方（P3-SPEC 6.3）
# ==========================================================================

#: 表配对的门槛：来源签名的 Jaccard 相似度至少这么多
ALIGN_MIN_JACCARD = 0.5


def _table_signatures(recipe: Recipe) -> dict[str, set[tuple[str, str]]]:
    """表 → 来源签名：写入它的 measures 标签、dimension 标题与标签、合计分段的标签、列表表头的 match_key 集合。

    带上来源的种类（指标、维度标题、维度标签、合计、列表、列表合计）：列表的合计表和主表表头相同，不分种类的话
    两张表的签名一样，会并列最大、谁也配不上。"""
    out: dict[str, set[tuple[str, str]]] = {}
    for sheet in recipe.sheets:
        for block in sheet.blocks:
            if isinstance(block, ListBlock):
                out.setdefault(block.table, set()).update(("list", P.match_key(c.header)) for c in block.columns)
                total = block.rows.total_row
                if total is not None and total.keep_as:
                    out.setdefault(total.keep_as, set()).update(
                        ("list_total", P.match_key(c.header)) for c in block.columns if c.type in ("INTEGER", "REAL"))
                continue
            for seg in block.segments:
                if isinstance(seg, MeasuresSegment):
                    out.setdefault(seg.table, set()).update(("measure", P.match_key(x)) for x in seg.labels.expect)
                elif isinstance(seg, DimensionSegment):
                    sig = out.setdefault(seg.table, set())
                    if seg.locate.title:
                        sig.add(("dim_title", P.match_key(seg.locate.title)))
                    sig.update(("dim_label", _dim_key(seg.dim.parser, x)) for x in seg.labels.expect)
                elif isinstance(seg, DerivedSegment) and seg.keep_as is not None:
                    out.setdefault(seg.keep_as.table, set()).update(
                        ("derived", (h.canonical if (h := P.hour_range_total(x)) else canon(x))) for x in seg.labels.expect)
    return out


def _pair_tables(cand: Recipe, cur: Recipe, report: list[str]) -> dict[str, str]:
    """起草的表 → 现行的表，一对一（评审一-m13）：Jaccard ≥ 0.5，按相似度从高到低贪心配对，一张现行表只配一次；
    同一张起草的表有两个并列最大的候选，或者两张起草的表对同一张现行表并列最大，都不配对，记进 report。"""
    cs, ks = _table_signatures(cand), _table_signatures(cur)
    scored: list[tuple[float, str, str]] = []
    for c, a in cs.items():
        for k, b in ks.items():
            if a or b:
                j = len(a & b) / len(a | b)
                if j >= ALIGN_MIN_JACCARD:
                    scored.append((j, c, k))
    out: dict[str, str] = {}
    used_c: set[str] = set()
    used_k: set[str] = set()
    for score in sorted({s for s, _c, _k in scored}, reverse=True):
        level = [(c, k) for s, c, k in scored if s == score and c not in used_c and k not in used_k]
        n_c: dict[str, int] = {}
        n_k: dict[str, int] = {}
        for c, k in level:
            n_c[c] = n_c.get(c, 0) + 1
            n_k[k] = n_k.get(k, 0) + 1
        tied_c = {c for c, n in n_c.items() if n > 1}
        tied_k = {k for k, n in n_k.items() if n > 1}
        for c, k in level:
            if c in tied_c or k in tied_k:
                continue
            out[c] = k
        for c in sorted(tied_c):
            report.append(f"表「{c}」与现行配方里的几张表同样相似，无法确定对应哪一张，保留起草器给的名字")
        for k in sorted(tied_k - {out.get(c) for c in tied_c}):
            report.append(f"起草的几张表都同样像现行的表「{k}」，无法确定哪一张对应它，保留起草器给的名字")
        used_c |= set(out) | tied_c
        used_k |= set(out.values()) | tied_k
    return out


def align_to_contract(candidate: dict[str, Any], current: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把按规则重新起草的配方的名字对齐到现行配方（P3-SPEC 6.3）。规则封闭，只从现行配方里抄名字：

    | 对象 | 怎么配对 | 对上以后 |
    | 表 | 来源签名的 Jaccard ≥ 0.5，一对一贪心，并列不配 | 改成现行的表名，并改掉一切引用 |
    | measures 列、列表列 | 按标签、表头的 match_key | 改成现行的列名 |
    | measures 段 | 同一张表上标签重合最多的段 | 抄 id |
    | dimension 段 | 按标题，或按标签集合 | 抄 id、dim.name、value、常量列名（pick 仍要能沿用，3.3）、派生列名 |
    | derived 段 | 跟着前一段 | 同上（另存表的合计项列、派生列、值列） |
    | 关系 | 按 kind 和改名后的成员 | 抄 id 和 claims |
    另外沿用现行配方的导入模式、日期列名、工作表 id 和块 id：重新起草只是换一套定位规则，不该悄悄改掉这些。

    对不上的保留起草器给的名字，记进 report（人话）。返回 (对齐后的配方（完整形式）, report)。不改入参。
    任何一份不合 schema 时原样返回候选和一句说明。理由：重新起草时大多数「破坏性变更」只是名字不同（起草器把宽表
    叫「客流汇总_按日」，现行叫「日客流」），按来源对齐能消掉这类假警报，剩下的才是真变化（final.md R11、3.3）。"""
    try:
        cand_m = Recipe.model_validate(candidate)
        cur_m = Recipe.model_validate(current)
    except ValidationError:
        return copy.deepcopy(candidate), ["配方的格式有问题，名字没有对齐"]
    report: list[str] = []
    cand = cand_m.model_dump(mode="json")
    tmap = _pair_tables(cand_m, cur_m, report)
    for t in derive_tables(cand_m)[0]:
        if t not in tmap:
            report.append(f"表「{t}」没有在现行配方里找到对应的表")
    cmap: dict[str, dict[str, str]] = {}           # 起草的表名 → {起草的列名: 现行的列名}
    smap: dict[str, str] = {}                      # 起草的分段 id → 现行的分段 id
    bmap: dict[tuple[int, int], str] = {}          # 起草的块位置 → 现行的块 id
    picks: dict[str, dict[str, str]] = {}          # 起草的分段 id → {现行的常量列名: pick}

    def col(t: str, old: str, new: str) -> None:
        if old and new and old != new:
            cmap.setdefault(t, {})[old] = new

    cur_segs = [(sheet, block, seg) for sheet in cur_m.sheets for block in sheet.blocks
                if isinstance(block, CrosstabBlock) for seg in block.segments]
    cur_lists = [(sheet, block) for sheet in cur_m.sheets for block in sheet.blocks if isinstance(block, ListBlock)]
    cur_cross = [(sheet, block) for sheet in cur_m.sheets for block in sheet.blocks if isinstance(block, CrosstabBlock)]
    sheet_ids: dict[int, str] = {}
    for si, sheet in enumerate(cand_m.sheets):
        same = [s for s in cur_m.sheets if P.match_key(s.match.name) == P.match_key(sheet.match.name)]
        if len(same) == 1:
            sheet_ids[si] = same[0].id
        elif len(cand_m.sheets) == 1 and len(cur_m.sheets) == 1:
            sheet_ids[si] = cur_m.sheets[0].id
        for bi, block in enumerate(sheet.blocks):
            if isinstance(block, ListBlock):
                target = tmap.get(block.table)
                hit = next((b for _s, b in cur_lists if b.table == target), None)
                if hit is None:
                    continue
                bmap[(si, bi)] = hit.id
                by_key = {P.match_key(c.header): c.name for c in hit.columns}
                for c in block.columns:
                    new = by_key.get(P.match_key(c.header))
                    if new:
                        col(block.table, c.name, new)
                        if block.rows.total_row is not None and block.rows.total_row.keep_as:
                            col(block.rows.total_row.keep_as, c.name, new)
                continue
            cross = [b for s, b in cur_cross if P.match_key(s.match.name) == P.match_key(sheet.match.name)]
            if not cross and len(cur_cross) == 1:
                cross = [cur_cross[0][1]]
            if len(cross) == 1:
                bmap[(si, bi)] = cross[0].id
                if cross[0].axis.name != block.axis.name:
                    for seg in block.segments:
                        for t in (getattr(seg, "table", None), seg.keep_as.table if isinstance(seg, DerivedSegment)
                                  and seg.keep_as else None):
                            if t:
                                col(t, block.axis.name, cross[0].axis.name)
            for seg in block.segments:
                if isinstance(seg, MeasuresSegment):
                    pool = [s for _sh, _b, s in cur_segs if isinstance(s, MeasuresSegment) and s.table == tmap.get(seg.table)]
                    keys = {P.match_key(x): x for x in seg.labels.expect}
                    best = max(pool, key=lambda s: len(keys.keys() & {P.match_key(x) for x in s.labels.expect}),
                               default=None)
                    if best is None or not keys.keys() & {P.match_key(x) for x in best.labels.expect}:
                        continue
                    smap[seg.id] = best.id
                    theirs = {P.match_key(x): best.measures.get(x) for x in best.labels.expect}
                    for k, raw in keys.items():
                        if theirs.get(k):
                            col(seg.table, seg.measures[raw], theirs[k])  # type: ignore[arg-type]
                elif isinstance(seg, DimensionSegment):
                    pool = [s for _sh, _b, s in cur_segs if isinstance(s, DimensionSegment)]
                    title = P.match_key(seg.locate.title) if seg.locate.title else None
                    hit = next((s for s in pool if title and s.locate.title and P.match_key(s.locate.title) == title), None)
                    if hit is None:
                        mine = {_dim_key(seg.dim.parser, x) for x in seg.labels.expect}
                        same_set = [s for s in pool if {_dim_key(s.dim.parser, x) for x in s.labels.expect} == mine]
                        hit = same_set[0] if len(same_set) == 1 else None
                    if hit is None:
                        report.append(f"分段「{seg.id}」没有在现行配方里找到对应的分段")
                        continue
                    smap[seg.id] = hit.id
                    col(seg.table, seg.dim.name, hit.dim.name)
                    col(seg.table, seg.value, hit.value)
                    roles = {v: k for k, v in hit.dim.derive.items()}
                    for name, role in seg.dim.derive.items():
                        col(seg.table, name, roles.get(role, name))
                    if len(seg.const) == 1 and len(hit.const) == 1:
                        (mine_c, mine_v), (their_c, their_v) = next(iter(seg.const.items())), next(iter(hit.const.items()))
                        col(seg.table, mine_c, their_c)
                        # pick 仍要满足 3.3：现行的 pick 仍是新标题的子串才沿用，否则用起草器从新标题里选的词
                        keep = their_v.pick if seg.locate.title and canon(their_v.pick) in canon(seg.locate.title) \
                            else mine_v.pick
                        picks[seg.id] = {their_c: keep}
            for seg in block.segments:
                if not isinstance(seg, DerivedSegment):
                    continue
                base = smap.get(seg.locate.segment or "")
                hit = next((s for _sh, _b, s in cur_segs if isinstance(s, DerivedSegment)
                            and s.locate.segment == base), None) if base else None
                if hit is None:
                    report.append(f"合计分段「{seg.id}」没有在现行配方里找到对应的分段")
                    continue
                smap[seg.id] = hit.id
                if seg.keep_as is not None and hit.keep_as is not None:
                    t = seg.keep_as.table
                    col(t, seg.keep_as.dim, hit.keep_as.dim)
                    col(t, seg.keep_as.value, hit.keep_as.value)
                    roles = {v: k for k, v in hit.keep_as.derive.items()}
                    for name, role in seg.keep_as.derive.items():
                        col(t, name, roles.get(role, name))

    def T(name: Any) -> Any:
        return tmap.get(name, name)

    def C(table: Any, name: Any) -> Any:
        return cmap.get(table, {}).get(name, name)

    cand["mode"] = cur_m.mode
    for si, sheet in enumerate(cand["sheets"]):
        if si in sheet_ids:
            sheet["id"] = sheet_ids[si]
        for bi, block in enumerate(sheet["blocks"]):
            if (si, bi) in bmap:
                block["id"] = bmap[(si, bi)]
            if block["layout"] == "list":
                old = block["table"]
                block["table"] = T(old)
                for c in block["columns"]:
                    c["name"] = C(old, c["name"])
                total = block["rows"].get("total_row")
                if total:
                    total["label_column"] = C(old, total["label_column"])
                    total["keep_as"] = T(total["keep_as"]) if total.get("keep_as") else total.get("keep_as")
                continue
            for seg in block["segments"]:
                old_id = seg["id"]
                seg["id"] = smap.get(old_id, old_id)
                if seg["locate"].get("segment"):
                    seg["locate"]["segment"] = smap.get(seg["locate"]["segment"], seg["locate"]["segment"])
                if seg["role"] == "measures":
                    t = seg["table"]
                    seg["measures"] = {k: C(t, v) for k, v in seg["measures"].items()}
                    seg["table"] = T(t)
                elif seg["role"] == "dimension":
                    t = seg["table"]
                    seg["dim"]["name"] = C(t, seg["dim"]["name"])
                    seg["dim"]["derive"] = {C(t, k): v for k, v in seg["dim"]["derive"].items()}
                    seg["value"] = C(t, seg["value"])
                    seg["const"] = {C(t, k): v for k, v in seg["const"].items()}
                    for k, pick in picks.get(old_id, {}).items():
                        if k in seg["const"]:
                            seg["const"][k] = {"pick": pick}
                    seg["table"] = T(t)
                else:
                    v = seg["verify"]
                    v["value"] = C(v["against_table"], v["value"])
                    v["against_table"] = T(v["against_table"])
                    k = seg.get("keep_as")
                    if k:
                        t = k["table"]
                        k.update(dim=C(t, k["dim"]), value=C(t, k["value"]),
                                 derive={C(t, n): r for n, r in k["derive"].items()}, table=T(t))
            cur_block = next((b for _s, b in cur_cross if b.id == block["id"]), None) if (si, bi) in bmap else None
            if cur_block is not None:
                block["axis"]["name"] = cur_block.axis.name
    for t in cand["tables"]:
        old = t["name"]
        t["name"] = T(old)
        t["grain"] = [C(old, g) for g in t["grain"]]
        t["units"] = {C(old, k): v for k, v in t["units"].items()}
    # 关系：先改引用，再按 kind 和成员与现行的关系配对，抄 id 和 claims；没配上的顺延编号，免得与抄来的 id 重号
    cur_rel: dict[Any, Any] = {}
    for r in cur_m.relations:
        if isinstance(r, SumEq):
            cur_rel[("sum_eq", r.table, r.total, frozenset(r.parts))] = r
        elif isinstance(r, NotComparable):
            cur_rel[("not_comparable", r.a.table, r.a.value, r.b.table, r.b.value, r.by)] = r
    taken: set[str] = set()
    loose: list[dict[str, Any]] = []
    for r in cand["relations"]:
        if r["kind"] == "sum_eq":
            old = r["table"]
            r.update(table=T(old), total=C(old, r["total"]), parts=[C(old, p) for p in r["parts"]])
            key: Any = ("sum_eq", r["table"], r["total"], frozenset(r["parts"]))
        elif r["kind"] == "not_comparable":
            a, b = r["a"], r["b"]
            r["by"] = C(a["table"], r["by"])
            a.update(value=C(a["table"], a["value"]), table=T(a["table"]))
            b.update(value=C(b["table"], b["value"]), table=T(b["table"]))
            key = ("not_comparable", a["table"], a["value"], b["table"], b["value"], r["by"])
        else:
            key = None
        hit = cur_rel.get(key) if key is not None else None
        if hit is not None and hit.id not in taken:
            r["id"] = hit.id
            r["claims"] = hit.claims
            taken.add(hit.id)
        else:
            loose.append(r)
            if r["kind"] != "dismissed":
                report.append(f"关系「{r['id']}」没有在现行配方里找到对应的关系")
    n = max([int(i[1:]) for i in taken] + [0])
    for r in loose:
        if r["id"] in taken:
            n += 1
            while f"R{n}" in taken:
                n += 1
            r["id"] = f"R{n}"
        taken.add(r["id"])
    _dedupe_names(cand, set(tmap.values()), set(smap.values()) | set(bmap.values()))
    return cand, _dedupe(report)


def _dedupe_names(recipe: dict[str, Any], kept_tables: set[str], kept_ids: set[str]) -> None:
    """对齐之后，没配上的起草名字可能和抄来的现行名字撞上：只改没配上的那一方（加 _2）。"""
    used = {collide_key(t) for t in kept_tables}
    rename: dict[str, str] = {}
    for t in recipe["tables"]:
        if t["name"] in kept_tables:
            continue
        if collide_key(t["name"]) in used:
            rename[t["name"]] = _unique(t["name"], used)
        else:
            used.add(collide_key(t["name"]))
    if rename:
        R = lambda n: rename.get(n, n)  # noqa: E731
        for t in recipe["tables"]:
            t["name"] = R(t["name"])
        for sheet in recipe["sheets"]:
            for block in sheet["blocks"]:
                if block["layout"] == "list":
                    block["table"] = R(block["table"])
                    total = block["rows"].get("total_row")
                    if total and total.get("keep_as"):
                        total["keep_as"] = R(total["keep_as"])
                for seg in block.get("segments") or []:
                    if seg.get("table"):
                        seg["table"] = R(seg["table"])
                    if seg.get("verify"):
                        seg["verify"]["against_table"] = R(seg["verify"]["against_table"])
                    if seg.get("keep_as"):
                        seg["keep_as"]["table"] = R(seg["keep_as"]["table"])
        for r in recipe["relations"]:
            if r.get("table"):
                r["table"] = R(r["table"])
            for side in ("a", "b"):
                if isinstance(r.get(side), dict):
                    r[side]["table"] = R(r[side]["table"])
    ids = set(kept_ids)
    for sheet in recipe["sheets"]:
        for block in sheet["blocks"]:
            for item in [block, *(block.get("segments") or [])]:
                if item["id"] in kept_ids:
                    continue
                if item["id"] in ids:
                    old = item["id"]
                    n = 2
                    while f"{old[:28]}_{n}" in ids:
                        n += 1
                    item["id"] = f"{old[:28]}_{n}"
                    for seg in block.get("segments") or []:
                        if (seg.get("locate") or {}).get("segment") == old:
                            seg["locate"]["segment"] = item["id"]
                ids.add(item["id"])
