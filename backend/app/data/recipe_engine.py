"""配方执行器（期 2，WP-2）：按已确认的配方把 Excel 工作表确定性地抽成表，写进一个临时 SQLite 库。

**为什么要有执行器而不是「按原样导入」。** 交叉表、多块报表按原样进库，同一列里混着不同口径的行，模型只能
猜（期 1 的「未规整」）。配方把「哪些格是数据、落进哪张表哪一列、合计怎么核对」写成封闭的规则，执行器逐格
执行、逐格记账，任何对不上的地方都报出来，绝不猜（H1–H3）。

**每个非空格恰好一个去向（格子账）。** 去向见契约 Role：值、合计、表头、标签、分段标题、统计期、区域外文字、
忽略的列、排除的隐藏行……没有去向的格是问题：数据行上的叫「没有去处」（cell_unclaimed），区域外的数字叫
「区域外数字」（outside_number），没有分段认领的数据行叫 row_unclaimed。读取层和扫描层各数一遍非空格，
逐行对账（pass_mismatch），两个解析器看到的必须是同一张表。

**问题收集策略。** 定位失败的分段（或块）跳过抽取，其余照常执行，一次报全（9.4 要求 label_duplicate 和
pk_duplicate 同时出现）。块级的定位失败（找不到日期表头、找不到表头）时，这张工作表的区域外数字、没有去处的格
不再逐个报：区域都不知道在哪，报出来的只是噪声，根因已经报了。

**给模型看的问题文字（model_message）只有位置和配方里的名字。** 坐标、行号、个数、分段 id、表名列名、配方
写的期望标签和表头；格子里的任何内容（数值、文字、日期）都不放——AI 修订时回灌的就是它（P2-SPEC 6.2），
模型要看格子写了什么，去压缩表示里按坐标找。message 是给人看的，可以带格子原文。

**执行顺序**（P2-SPEC 4.2）：认工作表 → 选读法（网格 / 流式）→ 定位各块（交叉表定位不需要年份；列表直接抽完）
→ 统计期（只看区域外的文字格）→ 交叉表补年份、断言、抽取 → 区域外文字分类 → 隐藏行列 → 格子账收尾 → 写库。

**写库。** 一个事务写完全部表（默认的回滚日志模式，不开 WAL：WAL 下 settle 会改写文件头，库哈希跟着变）；
有 structure / input / recipe 类问题时不留下库文件；任何异常都先删掉库文件再抛。table_hashes 在关掉写连接之后
用 SQL 排序流式算，不在 Python 里整表排序。库文件哈希不由这里算（调用方 settle_journal 之后再算）。

建库连接用 engine.open_checked_sqlite（关 DQS，和核对模块同一个入口）。建表语句不经 SQL 守卫
（同 tabular.py 开头的理由：导入是我们自己发起的可信操作）。

**期 3（P3-SPEC 9.2、3.1、3.2、第 8 节遗留项 4）在这里加了四件事，没用新字段的配方写出的库与期 2 逐字节相同：**
- 三种忽略规则（ignore_rows / ignore_columns / ignore_outside）：都按文件里的文字认，锚点在某一期没出现就什么也
  不忽略、也不报错；被忽略的格去向是 ignored（按表头忽略的列沿用 ignored_column），不再报「没有去处」「区域外
  数字」。
- 修复按钮的参数 Problem.fix_args：构造补丁要的标签、表头、分段标题、锚点原文和坐标，**不含数据格的数值**
  （修复按钮直接把它写进配方，混进数值就等于把本期的数写死在配方里）。fix 和 fix_args 同时有或同时没有：拿不出
  参数的问题（合并成「另有 N 处」的那条、列表找不到标题、标签不是文字的 row_unclaimed）不给修复按钮，界面也就
  不会放一个点了没用的按钮。
- 区域标记带块 id，按（去向、块、列段）合矩形：框选的重放比对按块过滤，网格按块描边，相邻两块的格不再合进同一
  个矩形。
- 回执里的「排除的行」rows_excluded（H2：排除的行要写进回执）：隐藏行、列表跳过的空行、按规则忽略的行、
  停止之后没导入的文字行、只核对不另存的合计行。
"""
from __future__ import annotations

import bisect
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from app.data import engine as _data_engine
from app.data import tabular, xlsx_cells
from app.data.names import canon
from app.data.recipe_parsers import (
    AxisDate, HourRange, HourTotal, Period, classify_outside, cn_date_range, cn_year_month, filename_period,
    hour_range, hour_range_total, is_pure_period_cell, looks_numeric_text, match_key, month_day_or_date,
    period_residue, text_label, thousands_number,
)
from app.data.recipe_types import (
    AxisOut, CanonEntry, CheckResult, ColumnOut, ContextSpec, CrosstabBlock, DerivedItem, DimensionSegment,
    ExcludedRows, Extraction, Grid, GridCell, LedgerSheet, ListBlock, MeasuresSegment, OutsideText,
    PeriodInput, PeriodOut, Problem, Recipe, RegionMark, SegmentLabels, SheetRecipe, TableOut, TableSpec,
    derive_tables,
)
from app.data.xlsx_scan import MAX_MERGED, SheetScan, WorkbookScan, cell_ref, col_letter

__all__ = ["ANCHOR_WINDOW", "PROBLEM_CODES", "PROBLEM_FIX", "execute"]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 执行器能产出的全部问题 code → 类别。test_recipe_engine_messages 维护一份同样的表，逐个 code 造夹具断言
#: model_message 非空、且不含触发格的值：这里新增 code 不补用例，那个测试就失败
PROBLEM_CODES: dict[str, str] = {
    # 配方本身（derive_tables 报的形状问题：调用方必须拦下，不能拿它产出的列去建表）
    "table_shape_conflict": "recipe", "measures_keys_mismatch": "recipe",
    # 工作表与读取
    "sheet_missing": "structure", "sheet_unexpected": "structure", "sheet_too_large_for_layout": "structure",
    "merged_too_many": "structure", "sparse": "structure", "pass_mismatch": "structure",
    # 格子账、区域外
    "cell_unclaimed": "structure", "outside_number": "structure", "outside_digits": "confirm",
    "hidden_rows": "structure", "hidden_cols": "structure",
    # 统计期
    "period_missing": "input",
    # 交叉表定位
    "axis_not_found": "structure", "axis_ambiguous": "structure", "axis_gap": "structure",
    "axis_extra_cells": "structure", "axis_year_missing": "structure", "axis_year_ambiguous": "structure",
    "axis_duplicate": "structure", "axis_not_contiguous": "structure", "axis_coverage": "structure",
    "row_without_label": "structure", "row_unclaimed": "structure", "title_not_found": "structure",
    "title_ambiguous": "structure", "segment_not_found": "structure", "segment_overlap": "structure",
    "label_missing": "structure", "label_unexpected": "structure", "label_duplicate": "structure",
    "label_unparsed": "structure", "const_missing": "structure", "merged_in_values": "structure",
    # 列表定位
    "header_not_found": "structure", "header_ambiguous": "structure", "column_missing": "structure",
    "column_extra": "structure", "rows_after_stop": "confirm", "total_row_missing": "structure",
    "total_row_blank": "structure", "list_empty": "confirm",
    # 值
    "value_not_integer": "structure", "value_text_number": "structure", "value_blank": "structure",
    "value_error": "structure", "value_not_number": "structure", "value_not_date": "structure",
    "value_formula": "structure", "formula_uncached": "structure", "formula_full_calc": "structure",
    "pk_duplicate": "structure",
}

#: 修复按钮的种类（契约 Problem.fix，取值见 FIX_KINDS）。参数 fix_args 的形状见 P3-SPEC 3.2，由报问题的地方随问题
#: 一起填（合并类问题在 _Engine._fix_args 里补）。期 3 相对期 2 的改动（3.1）：
#: - row_unclaimed 改为 ignore_cells（按行标签忽略）：分段是连续的数据行，隔了空行的那一行把标签加进分段也认领
#:   不到，加标签只会再多报一条 label_missing；
#: - cell_unclaimed 不给：数据行里单独一格没有能泛化的锚点（同一行的标签会把整行忽略掉），只能改文件或框选；
#: - rows_after_stop 不给：它是需确认类，勾选即可启用，列表的按行忽略不在期 3 范围内；
#: - column_extra（列表）给 ignore_cells（按表头忽略）、hidden_rows / hidden_cols 给 declare_hidden、
#:   sheet_missing 给 rename_sheet。
#: edit_members 不来自执行器（来自静态校验的 fact_claim_mismatch），rename_sheet 另有不是问题的触发
#: （Extraction.sheets.renamed），都不在这里。
PROBLEM_FIX: dict[str, str] = {
    "label_missing": "remove_label", "label_unexpected": "add_label", "title_not_found": "rename_title",
    "label_unparsed": "declare_total", "value_not_number": "declare_placeholder",
    "axis_extra_cells": "ignore_cells", "column_extra": "ignore_cells", "row_unclaimed": "ignore_cells",
    "outside_number": "ignore_cells",
    "hidden_rows": "declare_hidden", "hidden_cols": "declare_hidden", "sheet_missing": "rename_sheet",
}

#: 列表找表头的窗口：从 after_title 的下一行（或已用区域首行）起往下这么多行
ANCHOR_WINDOW = 200
#: 写库时每批 executemany 的行数
WRITE_BATCH = 5000
#: 问题的格子坐标最多几个（契约 Problem.cells）
CELLS_MAX = 20
#: 同一工作表、同一 code 最多逐条列出的问题数，其余合成一条「另有 N 处」
PER_CODE_MAX = 50
#: 规范写法对照最多几条
CANON_MAX = 500
#: declare_placeholder 的 fix_args.texts 最多几种（P3-SPEC 3.2 ⑦）；_Issue 多收一种，才知道是不是超了
PLACEHOLDER_TEXTS_MAX = 8
#: 能当占位符提议的文字最长几个字（canon 之后）。更长的多半是备注、说明，不是「无数据」的记号
PLACEHOLDER_TEXT_LEN = 8
#: 排除的行在回执里的顺序（同一工作表内按原因、块、锚点、首行排）
_EXCLUDED_ORDER = ("hidden_excluded", "blank_skipped", "ignored_rows", "ignored_outside", "after_stop",
                   "total_not_kept")
#: 公式区域展开的上限：更大的区域不可能全落在基表里，直接判「引用了基表之外的格」
REF_CELLS_MAX = 200_000

#: 块认领的去向（区域外文字、统计期不算）：隐藏行列只看这些格所在的行列
_BLOCK_ROLES = frozenset({"value", "derived_value", "derived_label", "col_header", "row_label", "section_title",
                          "ignored_column", "hidden_excluded", "total_label", "total_value"})
#: 块级定位失败的工作表上不再报的「区域外 / 没有去处」类问题（根因已经报了）
_NOISE_CODES = frozenset({"outside_number", "outside_digits", "cell_unclaimed", "row_unclaimed",
                          "row_without_label", "rows_after_stop"})
#: 像表下说明的开头（按 canon 之后比）。跳过空行的列表里，空行之后只有首列一格文字的行一律当表的下边界（与起草器
#: 一致，_ListRun._note_after_blank）；以这些开头的只记进回执（rows_excluded、区域外文字），其余的另出 rows_after_stop
#: 要人确认：它也可能是表尾一条只填了首列的数据行，静默移出去就少了一条记录（WP-8 评审意见 4）
_NOTE_PREFIXES = tuple(canon(x) for x in ("注", "说明", "备注", "制表", "填表", "填报", "来源", "数据来源", "单位",
                                          "审核"))
#: after_title 最长 40 字，算上空白和全角写法，比这更长的格不可能是它
_TITLE_SCAN_MAX = 200
_PARSER_NAME = {
    "hour_range": "时段写法（如 7-8）", "hour_range_total": "时段合计写法（如 18-22时合计）",
    "text": "文字", "measures": "标签",
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _coord(sheet: str, r: int, c: int) -> str:
    return f"{sheet}!{cell_ref(r, c)}"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _text(value: Any) -> str:
    """格子的文字：文本原样，其余按简单导入的写法（tabular.as_text）。"""
    if isinstance(value, str):
        return value
    return tabular.as_text(value)


def _shown(value: Any, limit: int = 20) -> str:
    """给人看的一格内容（截断）。"""
    text = _text(value).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _join(items: list[str], limit: int = 10) -> str:
    shown = "、".join(items[:limit])
    return shown + (f" 等 {len(items)} 处" if len(items) > limit else "")


def _quoted(items: list[str], limit: int = 10) -> str:
    return _join([f"「{x}」" for x in items], limit)


def _rows_text(rows: list[int], limit: int = 10) -> str:
    """行号列表 → 「10–20、23」：相邻的合成一段。"""
    spans: list[list[int]] = []
    for r in sorted(set(rows)):
        if spans and r == spans[-1][1] + 1:
            spans[-1][1] = r
        else:
            spans.append([r, r])
    return _join([str(a) if a == b else f"{a}–{b}" for a, b in spans], limit)


def _anchor_text(value: Any) -> str | None:
    """能写进 fix_args 当锚点的文字（表头、行标签、区域外文字、候选标题）：只认文字格，原样返回；空白、不是文字、
    像一个数（「1,234」「12.5%」）的一律 None。

    为什么要拦「像数的」：fix_args 不许带数据格的数值（契约 Problem.fix_args），而表头位置上的「987654」、标签列上
    的「2,345」其实就是数，写进配方的忽略规则还等于把本期的数写死。拦下之后界面给不出这条提议，提示改文件。"""
    if not isinstance(value, str) or not value.strip() or looks_numeric_text(value.strip()):
        return None
    return value


def _spans_of(rows: list[list[int]]) -> list[list[int]]:
    """[[起, 止], …]（可能乱序、重叠、相接）→ 升序、不重叠、相接的合成一段。"""
    out: list[list[int]] = []
    for a, b in sorted(rows):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _in_spans(spans: list[tuple[int, int]], starts: list[int], x: int) -> bool:
    i = bisect.bisect_right(starts, x) - 1
    return i >= 0 and spans[i][0] <= x <= spans[i][1]


def _open_sqlite(path: str, *, readonly: bool) -> sqlite3.Connection:
    """关 DQS 的 sqlite3 连接，和核对模块共用 engine.open_checked_sqlite：关不掉就拒绝连接。"""
    return _data_engine.open_checked_sqlite(path, readonly=readonly)


def _remove_db(path: str) -> None:
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------
# 公式形状（合计格的引用核对，4.6）
# --------------------------------------------------------------------------

_REF = r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})"
_AREA = rf"{_REF}(?:\s*:\s*{_REF})?"
_AREAS = rf"{_AREA}(?:\s*,\s*{_AREA})*"
_F_SUM = re.compile(rf"=\s*SUM\s*\(\s*({_AREAS})\s*\)\s*", re.IGNORECASE | re.ASCII)
_F_SUBTOTAL = re.compile(rf"=\s*SUBTOTAL\s*\(\s*(?:9|109)\s*,\s*({_AREAS})\s*\)\s*", re.IGNORECASE | re.ASCII)
_F_AGGREGATE = re.compile(rf"=\s*AGGREGATE\s*\(\s*9\s*,\s*[0-9]{{1,2}}\s*,\s*({_AREAS})\s*\)\s*",
                          re.IGNORECASE | re.ASCII)
_F_PLUS = re.compile(rf"=\s*{_REF}(?:\s*\+\s*{_REF})*\s*", re.ASCII)
_ONE_AREA = re.compile(_AREA, re.ASCII)
_ONE_REF = re.compile(_REF, re.ASCII)


def _col_no(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def formula_refs(formula: str) -> tuple[str, list[tuple[int, int]]] | None:
    """合计公式 → (函数, 引用的格 [(行, 列)…])。认四种形状：SUM(区域…)、单格相加、SUBTOTAL(9/109, 区域)、
    AGGREGATE(9, 选项, 区域)，允许 $ 和空白；别的工作表的引用、别的函数一律认不出（None）。区域太大时
    给出的格子列表是空的、函数照给（调用方按「引用了基表之外的格」处理）。"""
    text = formula.strip()
    for func, rx in (("SUM", _F_SUM), ("SUBTOTAL", _F_SUBTOTAL), ("AGGREGATE", _F_AGGREGATE)):
        m = rx.fullmatch(text)
        if m is None:
            continue
        cells: list[tuple[int, int]] = []
        for a in _ONE_AREA.finditer(m.group(1)):
            c1, r1 = _col_no(a.group(1)), int(a.group(2))
            c2, r2 = (_col_no(a.group(3)), int(a.group(4))) if a.group(3) else (c1, r1)
            r1, r2 = min(r1, r2), max(r1, r2)
            c1, c2 = min(c1, c2), max(c1, c2)
            if (r2 - r1 + 1) * (c2 - c1 + 1) + len(cells) > REF_CELLS_MAX:
                return func, []
            cells.extend((r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1))
        return func, cells
    if _F_PLUS.fullmatch(text):
        return "plus", [(int(m.group(2)), _col_no(m.group(1))) for m in _ONE_REF.finditer(text)]
    return None


# --------------------------------------------------------------------------
# 溯源、区域标记、两遍对账、合并区
# --------------------------------------------------------------------------


class _Runs:
    """一列的溯源：连续 rowid 且格子沿同一行向右（或同一列向下）的合成一段（recipe-ui 原型的 _lineage_runs）。"""

    __slots__ = ("runs",)

    def __init__(self) -> None:
        #: [起始 rowid, 工作表, 起始行, 起始列, 个数, 方向]
        self.runs: list[list[Any]] = []

    def add(self, rowid: int, sheet: str, r: int, c: int) -> None:
        runs = self.runs
        if runs:
            last = runs[-1]
            start, lsheet, lr, lc, n, way = last
            if lsheet == sheet and start + n == rowid:
                if way == "right" and r == lr and c == lc + n:
                    last[4] = n + 1
                    return
                if way == "down" and c == lc and r == lr + n:
                    last[4] = n + 1
                    return
                if n == 1:
                    if r == lr and c == lc + 1:
                        last[4], last[5] = 2, "right"
                        return
                    if c == lc and r == lr + 1:
                        last[4], last[5] = 2, "down"
                        return
        runs.append([rowid, sheet, r, c, 1, "right"])

    def out(self) -> list[list[Any]]:
        return [[start, sheet, cell_ref(r, c), n, way] for start, sheet, r, c, n, way in self.runs]


#: 一行里的一个区域段：(起列, 止列, 去向, 块 id)。区域外文字、统计期、按区域外锚点忽略的格块 id 为 None
Span = tuple[int, int, str, "str | None"]


def _row_spans(cells: list[tuple[int, GridCell]], claims: dict[int, str], layout: list[Span] | None,
               blocks: dict[int, str] | str | None) -> list[Span]:
    """一行的区域段 [(起列, 止列, 去向, 块)]。blocks：已认领格所在的块（按列给，或者整行同一个块）。

    块登记了这一行的固定列段（layout：列表数据行是本块的列范围，交叉表数据行是标签列和日期列）时，按固定列段
    出段，不看这一行哪几格碰巧是空的：末列常空的「备注」、隔行为空的金额，否则每行的段都不一样，20 万行的表会
    出 20 万个矩形（4.9「区域标记按矩形」）。固定列段里有去向或块对不上的格（块之间认领重叠）时退回按格出段。
    其余的格：同一去向、同一块的相邻已认领格合成一段（中间的空格一并覆盖，着色无妨），没有认领的格把段隔开
    （未认领的格不在区域标记里，由问题的 cells 指出）。不同块的格不合段（期 3：框选的重放比对按块过滤区域，
    并排的两张列表的值区合成一个矩形就分不出各自的范围了）。"""
    block_of: Callable[[int], str | None] = (blocks.get if isinstance(blocks, dict)  # type: ignore[assignment]
                                             else (lambda c: blocks))
    items: list[tuple[int, int, str | None, str | None]] = []
    if layout:
        fixed = sorted(layout, key=lambda x: (x[0], x[1]))
        for c, _ in cells:
            for a, b, role, blk in fixed:
                if a <= c <= b:
                    if claims.get(c) != role or block_of(c) != blk:
                        layout = None
                    break
            else:
                items.append((c, c, claims.get(c), block_of(c)))
            if layout is None:
                items = []
                break
        if layout:
            items.extend(fixed)
            items.sort(key=lambda x: x[0])
    if not layout:
        items = [(c, c, claims.get(c), block_of(c)) for c, _ in cells]
    out: list[list[Any]] = []
    broken = True
    for a, b, role, blk in items:
        if role is None:
            broken = True
            continue
        if out and not broken and out[-1][2] == role and out[-1][3] == blk:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b, role, blk])
        broken = False
    return [(a, b, role, blk) for a, b, role, blk in out]


class _Regions:
    """按行喂入区域段，同一去向、同一块、同一列范围、行相接的合成矩形（给网格着色，契约 RegionMark）。

    行内怎么分段见 _row_spans。喂入不必全局按行有序：乱序的只是多出几个小矩形，覆盖不受影响（区域外文字要等
    统计期定下来才分类，喂得晚）。"""

    def __init__(self, sheet: str) -> None:
        self.sheet = sheet
        self.open: dict[tuple[str, str | None, int, int], list[int]] = {}
        self.closed: list[tuple[int, int, int, int, str, str | None]] = []

    def feed_row(self, r: int, cells: list[tuple[int, str]]) -> None:
        """单独几格（区域外文字、统计期、按区域外锚点忽略的数字）：不属于任何块，同一去向的相邻格合成一段。"""
        spans: list[Span] = []
        for c, role in cells:
            if spans and spans[-1][2] == role:
                spans[-1] = (spans[-1][0], c, role, None)
            else:
                spans.append((c, c, role, None))
        self.feed_spans(r, spans)

    def feed_spans(self, r: int, spans: list[Span]) -> None:
        for c1, c2, role, block in spans:
            key = (role, block, c1, c2)
            rect = self.open.get(key)
            if rect is not None and rect[1] == r - 1:
                rect[1] = r
                continue
            if rect is not None:
                self.closed.append((rect[0], c1, rect[1], c2, role, block))
            self.open[key] = [r, r]

    def marks(self) -> list[RegionMark]:
        rects = list(self.closed) + [(r1, c1, r2, c2, role, block)
                                     for (role, block, c1, c2), (r1, r2) in self.open.items()]
        rects.sort(key=lambda x: (x[0], x[1], x[4], x[5] or ""))
        out = []
        for r1, c1, r2, c2, role, block in rects:
            ref = cell_ref(r1, c1) if (r1, c1) == (r2, c2) else f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}"
            out.append(RegionMark(self.sheet, role, ref, block))  # type: ignore[arg-type]
        return out


class _Reconciler:
    """两遍对账：读取层逐行数到的非空格数，和扫描层的 row_runs 逐行比（行号升序喂入）。"""

    def __init__(self, sheet: SheetScan) -> None:
        self.scan = sheet
        self.total = 0
        self.bad: list[tuple[int, int, int]] = []
        self.by_rows = bool(sheet.row_runs) or sheet.nonempty == 0
        self._it = self._expected()
        self._next = next(self._it, None)

    def _expected(self) -> Iterator[tuple[int, int]]:
        for lo, hi, n in self.scan.row_runs:
            for r in range(lo, hi + 1):
                yield r, n

    def feed(self, r: int, n: int) -> None:
        self.total += n
        if not self.by_rows:
            return
        while self._next is not None and self._next[0] < r:
            self.bad.append((self._next[0], self._next[1], 0))
            self._next = next(self._it, None)
        expect = 0
        if self._next is not None and self._next[0] == r:
            expect = self._next[1]
            self._next = next(self._it, None)
        if expect != n:
            self.bad.append((r, expect, n))

    def finish(self) -> None:
        while self.by_rows and self._next is not None:
            self.bad.append((self._next[0], self._next[1], 0))
            self._next = next(self._it, None)


class _MergeSweep:
    """按行号递增查询「这一行有哪些合并区」：合并区按首行排序，活动集合随行推进（列表块逐行处理时用）。"""

    def __init__(self, merges: list[tuple[int, int, int, int]], c1: int, c2: int) -> None:
        self.todo = sorted((m for m in merges if m[3] >= c1 and m[1] <= c2), key=lambda m: m[0])
        self.i = 0
        self.active: list[tuple[int, int, int, int]] = []

    def at(self, r: int) -> list[tuple[int, int, int, int]]:
        while self.i < len(self.todo) and self.todo[self.i][0] <= r:
            self.active.append(self.todo[self.i])
            self.i += 1
        self.active = [m for m in self.active if m[2] >= r]
        return self.active


# --------------------------------------------------------------------------
# 问题
# --------------------------------------------------------------------------


@dataclass
class _Issue:
    """按（code、位置）合并的一组同类格子问题：值的类型、区域外数字、没有去处的格……"""

    code: str
    sheet: str | None
    where: str
    reason: str
    tail: str
    count: int = 0
    cells: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    #: 期 3 修复参数要的位置：所在块（value_not_number）、所在行（outside_number 按行合并，一行一条）
    block: str | None = None
    row: int | None = None
    #: value_not_number：能当占位符提议的文字（canon 后）→ 格数，最多收 PLACEHOLDER_TEXTS_MAX + 1 种
    texts: Counter[str] = field(default_factory=Counter)


#: 合并类问题的说明：code → (原因（{n} 是个数）, 下一步)。下一步写配方面板上的「字段」和「选项」原文
#: （frontend/src/lib/terms.ts 的 RECIPE_TEXT、RECIPE_CHOICE_LABEL），界面上改了标签这里要跟着改
_ISSUE_TEXT: dict[str, tuple[str, str]] = {
    "value_not_integer": ("{n} 个单元格不是整数", "。该列按整数导入：请检查原表，或在配方面板中把「类型」改为「小数」"),
    "value_text_number": ("{n} 个单元格是文本写的数字",
                          "。如需按数字导入，请在配方面板的「文本写的数字」中选择「千分位写法按数字保存」"),
    "value_blank": ("{n} 个单元格为空", "。如原表确实为空，请在配方面板的「空格」中选择「存为空值」"),
    "value_error": ("{n} 个单元格是错误值", "。请在 Excel 中修正后重新上传"),
    "value_not_number": ("{n} 个单元格不是数字", "。如果它表示无数据，请在配方面板中添加占位符"),
    "value_not_date": ("{n} 个单元格不是日期", "。请检查原表，或在配方面板中把这一列的「类型」改为「文字」"),
    "value_formula": ("{n} 个数据格是公式",
                      "。数据区的公式通常是没有识别出来的合计；如确为数据，请在配方面板的「公式」中选择「按保存值导入」"),
    "formula_uncached": ("{n} 个公式格没有保存计算结果", "，文件未经计算保存。请在 Excel 中打开并保存后重新上传"),
    "formula_full_calc": ("工作簿设置了打开时重新计算，{n} 个公式格的保存值可能只是占位",
                          "。请在 Excel 中打开并保存后重新上传"),
    "pk_duplicate": ("主键有 {n} 处重复", "。同一主键只能出现一次，请检查原表（例如同一项写成了两种写法）"),
    "cell_unclaimed": ("{n} 个单元格没有去处", "。请调整配方，或删除这些单元格"),
    "outside_number": ("导入区域之外有 {n} 个数字", "。请删除这些单元格，或调整配方让它们进入导入区域"),
    "merged_in_values": ("数据区有 {n} 个合并单元格", "。请在 Excel 中取消合并后重新上传"),
}
_PK_BLANK = ("主键列有 {n} 个单元格为空", "。主键不能为空，请检查原表")
_PK_PLACEHOLDER = ("主键列有 {n} 个单元格是占位符", "。占位符按空值存，主键不能为空，请检查原表")
#: 交叉表、合计另存表的主键列是空值（占位符、blank=null 的空格、没有保存值的合计格、跳过的隐藏行）
_PK_NULL = ("有 {n} 处为空值", "。主键不能为空：请检查原表，或在配方面板的「主键」中去掉这一列")


class _Problems:
    def __init__(self) -> None:
        self.items: list[tuple[str | None, Problem]] = []
        self.issues: dict[tuple[str, str | None, str], _Issue] = {}
        self._per: Counter[tuple[str, str | None]] = Counter()
        self._over: Counter[tuple[str, str | None]] = Counter()

    def add(self, code: str, message: str, model_message: str, cells: list[str] | tuple[str, ...] = (), *,
            sheet: str | None = None, fix_args: dict[str, Any] | None = None) -> Problem | None:
        """记一条问题，返回它（超过 PER_CODE_MAX 合进「另有 N 处」时返回 None）。

        fix 只在 fix_args 也给了时才填：修复补丁全靠 fix_args 构造，没有参数的修复按钮点了也没用（P3-SPEC 3.1）。
        title_not_found 的候选标题要等所有块认领完才知道，调用方拿返回的 Problem 之后再补（_Crosstab.post_claims）。"""
        key = (code, sheet)
        self._per[key] += 1
        if self._per[key] > PER_CODE_MAX:
            self._over[key] += 1
            return None
        fix = PROBLEM_FIX.get(code) if fix_args is not None else None
        p = Problem(code=code, category=PROBLEM_CODES[code], message=message,  # type: ignore[arg-type]
                    cells=list(cells)[:CELLS_MAX], model_message=model_message, fix=fix,
                    fix_args=fix_args if fix is not None else None)
        self.items.append((sheet, p))
        return p

    def issue(self, code: str, sheet: str | None, where: str, cell: str | list[str], example: str | None = None, *,
              text: tuple[str, str] | None = None, group: str = "", block: str | None = None,
              row: int | None = None, sample: str | None = None) -> None:
        """记一处同类问题；cell 可以是几个坐标（主键重复要列出两处）。block、row、sample 是修复参数要的：
        sample 是 value_not_number 那一格的文字（canon 后），能当占位符提议的才收进 texts。"""
        key = (code, sheet, where + "\x00" + group)
        it = self.issues.get(key)
        if it is None:
            reason, tail = text or _ISSUE_TEXT[code]
            it = self.issues[key] = _Issue(code, sheet, where, reason, tail, block=block, row=row)
        it.count += 1
        for one in ([cell] if isinstance(cell, str) else cell):
            if len(it.cells) < CELLS_MAX and one not in it.cells:
                it.cells.append(one)
        if example is not None and len(it.examples) < 3:
            it.examples.append(example)
        if sample is not None and (sample in it.texts or len(it.texts) <= PLACEHOLDER_TEXTS_MAX):
            it.texts[sample] += 1

    def finish(self, failed_sheets: set[str],
               args_for: Callable[[_Issue], dict[str, Any] | None] | None = None) -> list[Problem]:
        """合并类问题出成 Problem；args_for 给有修复按钮的合并类问题（outside_number、value_not_number）算 fix_args。
        「另有 N 处」那一条没有坐标和参数，不给修复按钮。"""
        for it in self.issues.values():
            reason = it.reason.format(n=it.count)
            shown = [c.split("!")[-1] for c in it.cells]
            ex = f"（如 {'；'.join(it.examples)}）" if it.examples else f"（{_join(shown, 5)}）"
            model_ex = f"（{_join(shown, 5)}）"
            args = args_for(it) if args_for is not None and it.code in PROBLEM_FIX else None
            self.add(it.code, f"{it.where}：{reason}{ex}{it.tail}", f"{it.where}：{reason}{model_ex}", it.cells,
                     sheet=it.sheet, fix_args=args)
        for (code, sheet), n in self._over.items():
            where = f"工作表「{sheet}」" if sheet else "本文件"
            text = f"{where}另有 {n} 处同类问题未逐一列出"
            self.items.append((sheet, Problem(code=code, category=PROBLEM_CODES[code], message=text,  # type: ignore[arg-type]
                                              model_message=text)))
        return [p for sheet, p in self.items if not (sheet in failed_sheets and p.code in _NOISE_CODES)]

    def blocking(self) -> bool:
        return any(p.category in ("structure", "input", "recipe") for _, p in self.items) or any(
            PROBLEM_CODES[i.code] in ("structure", "input", "recipe") for i in self.issues.values())


# --------------------------------------------------------------------------
# 表的写入口
# --------------------------------------------------------------------------


class _Sink:
    """一张表的行：主键去重、计数、溯源、批量写库（有库时）。rowid 就是插入序号（新库、1 起）。"""

    def __init__(self, eng: _Engine, name: str, cols: list[ColumnOut], spec: TableSpec | None) -> None:
        self.eng = eng
        self.name = name
        self.cols = cols
        names = [c.name for c in cols]
        self.grain = [g for g in (spec.grain if spec else []) if g in names]
        self.grain_idx = [names.index(g) for g in self.grain]
        self.check_pk = bool(self.grain_idx) and not eng.partial
        self.seen: dict[tuple[Any, ...], tuple[int, int]] = {}
        self.rows = 0
        self.lineage: dict[str, _Runs] = {}
        self.buffer: list[tuple[Any, ...]] = []
        self.sheet: str | None = None
        self.sources: list[str] = []
        self.insert_sql = f"INSERT INTO {_q(name)} VALUES ({', '.join('?' * len(cols))})"

    def add(self, values: list[Any], *, sheet: str, at: tuple[int, int],
            cells: list[tuple[str, int, int]] | None = None, fails: bool = False) -> int | None:
        """加一行；主键重复或主键为空时不加（问题已记），返回 None。at：代表这一行的格（报主键重复用）；
        cells：[(列名, 行, 列)] 用于溯源。

        fails=True 时 values 里可以有 _FAIL：转换时已经报过问题的格（交叉表的值、合计格），按空值存。
        主键是 _FAIL 的行不再另报；主键是空值（占位符、blank=null 的空格、没有保存值的合计格、排除的隐藏行）
        一律报 value_blank：不加这一行又不出声，库里就静默少一行，N 核对（COUNT(*) = expected_rows）照样通过（H2）。
        列表的失败行不进这里（转换失败就不加），主键列的空格、占位符在转换时已经拒收。"""
        if self.grain_idx:
            key = tuple(values[i] for i in self.grain_idx)
            if any(k is None or k is _FAIL for k in key):
                if not any(k is _FAIL for k in key):
                    self._pk_null(values, sheet, at, cells)
                return None
        if fails:
            values = [None if v is _FAIL else v for v in values]
        if self.grain_idx:
            if self.check_pk:
                first = self.seen.get(key)
                if first is not None:
                    shown = "、".join(str(k) for k in key)
                    self.eng.problems.issue(
                        "pk_duplicate", sheet, f"表「{self.name}」（主键：{'、'.join(self.grain)}）",
                        [_coord(sheet, *first), _coord(sheet, *at)],
                        f"「{shown}」出现在 {cell_ref(*first)} 和 {cell_ref(*at)}")
                    return None
                self.seen[key] = at
        if self.sheet is None:
            self.sheet = sheet
        self.rows += 1
        rowid = self.rows
        if cells:
            for col, r, c in cells:
                runs = self.lineage.get(col)
                if runs is None:
                    runs = self.lineage[col] = _Runs()
                runs.add(rowid, sheet, r, c)
        if self.eng.conn is not None:
            self.buffer.append(tuple(values))
            if len(self.buffer) >= WRITE_BATCH:
                self.flush()
        return rowid

    def _pk_null(self, values: list[Any], sheet: str, at: tuple[int, int],
                 cells: list[tuple[str, int, int]] | None) -> None:
        """主键列是空值：按主键列分组记 value_blank，坐标取溯源里那一列的格（没有就用代表格）。"""
        names = [self.grain[j] for j, i in enumerate(self.grain_idx) if values[i] is None]
        where_of = {col: (r, c) for col, r, c in (cells or [])}
        coords = [_coord(sheet, *where_of.get(n, at)) for n in names]
        self.eng.problems.issue("value_blank", sheet, f"工作表「{sheet}」的表「{self.name}」的主键列{_quoted(names)}",
                                coords, text=_PK_NULL, group="pk_null")

    def flush(self) -> None:
        if self.buffer and self.eng.conn is not None:
            self.eng.conn.executemany(self.insert_sql, self.buffer)
        self.buffer.clear()


# --------------------------------------------------------------------------
# 值的转换（4.5）
# --------------------------------------------------------------------------

_FAIL = object()
#: 列表数据区里不能填充的合并单元格覆盖的空格（已经报了 merged_in_values）
_MERGED = object()


# --------------------------------------------------------------------------
# 工作表上下文
# --------------------------------------------------------------------------


@dataclass
class _Stopped:
    """被空行停下的列表块：往下看紧接着的行有没有像数据的文字行（rows_after_stop，4.4）。"""

    block: str
    stop_row: int
    c1: int
    c2: int
    #: 本块列范围内几格非空就算「像数据的行」：规格写的是 2 格（挡住单独一格的落款、页码），只有 1 列的块
    #: 按 1 格算——只有一列的名单在空行之后被截掉，正是这条检查要让人看见的情形
    need: int = 2
    closed: bool = False
    rows: list[int] = field(default_factory=list)
    cells: list[str] = field(default_factory=list)
    #: 这些行在本块列范围内的非空格数（rows_excluded 的 after_stop 用；cells 只留前 CELLS_MAX 个坐标）
    n: int = 0
    #: 跳过空行的列表按「表下说明」收尾时（_ListRun._note_after_blank，WP-8 修补 3）：说明那一行的行号、坐标、文字
    #: （去了首尾空白），以及它像不像说明（_note_like）。stop 模式碰到空行收尾时都是 None
    note_row: int | None = None
    note_cell: str | None = None
    note_text: str | None = None
    note_like: bool = True
    #: 按表下说明收尾时，说明之下本块列范围内有字、但不到 need 格的行（第二行说明、单独一格的落款）和它们的格数：
    #: 修补 3 之前这些行都会读成数据，现在交给了区域外，同样记进 rows_excluded（after_stop），不另出问题
    tail: list[int] = field(default_factory=list)
    tail_n: int = 0


class _Sheet:
    def __init__(self, eng: _Engine, si: int, sr: SheetRecipe, scan: SheetScan) -> None:
        self.eng = eng
        self.si = si
        self.sr = sr
        self.scan = scan
        self.name = scan.name
        self.context: ContextSpec | None = sr.context[0] if sr.context else None
        self.grid: Grid | None = None
        self.limit = scan.bounds.max_row if scan.bounds else 0
        self.truncated = False
        self.claims: dict[tuple[int, int], str] = {}
        self.owners: dict[tuple[int, int], str] = {}
        #: 格 → 认领它的块 id（区域标记按块合矩形）。owners 里交叉表的格记的是分段 id（重叠报错要写分段名），
        #: 这里统一换算成块 id
        self.cell_block: dict[tuple[int, int], str] = {}
        self.overlaps: set[tuple[str, str]] = set()
        self.data_rows: set[int] = set()
        self.suppressed: set[tuple[int, int]] = set()
        self.failed = False
        self.deferred: list[tuple[int, int, str]] = []
        self.roles: Counter[str] = Counter()
        self.read_count = 0
        self.regions = _Regions(self.name)
        self.recon: _Reconciler | None = None
        self.stopped: list[_Stopped] = []
        #: 行 → 块登记的固定列段（_row_spans）：格子账收尾那一行时取走，流式路径里同时只有一两行
        self.layouts: dict[int, list[Span]] = {}
        #: 排除的行：(原因, 块, 锚点) → [[[起, 止], …], 格数]，_phase4 收尾时出成 ExcludedRows
        self.excluded: dict[tuple[str, str | None, str | None], list[Any]] = {}
        #: ignore_outside 的锚点：[(match_key, 配方里的原文)]，按配方顺序
        self.outside_rules: list[tuple[str, str]] = [(k, x.anchor) for x in sr.ignore_outside
                                                     if (k := match_key(x.anchor))]
        #: 有 ignore_outside 时，区域外的数字格先压着，等同一行的区域外文字都分类完再定忽略还是报问题
        self.held: list[tuple[int, int, Any]] = []
        #: 行 → 这一行区域外的文字格 [(列, 原文)]（不含统计期来源）：ignore_outside 的锚点、outside_number 的修复参数
        self.outside_texts: dict[int, list[tuple[int, str]]] = {}
        self.order: list[tuple[int, str]] = []
        self.hidden_claimed_rows: set[int] = set()
        self.hidden_claimed_cols: set[int] = set()
        self._hr, self._hr_starts = scan.hidden_rows, [a for a, _ in scan.hidden_rows]
        self._hc, self._hc_starts = scan.hidden_cols, [a for a, _ in scan.hidden_cols]
        self.merges: list[tuple[int, int, int, int]] = []
        for ref in scan.merged:
            span = xlsx_cells.parse_range(ref)
            if span is not None:
                self.merges.append(span)

    def hidden_row(self, r: int) -> bool:
        return bool(self._hr) and _in_spans(self._hr, self._hr_starts, r)

    def hidden_col(self, c: int) -> bool:
        return bool(self._hc) and _in_spans(self._hc, self._hc_starts, c)

    def coord(self, r: int, c: int) -> str:
        return _coord(self.name, r, c)

    def layout(self, r: int, spans: list[Span]) -> None:
        """登记这一行的固定列段（区域标记按它出矩形，见 _row_spans）。"""
        got = self.layouts.get(r)
        if got is None:
            self.layouts[r] = list(spans)
        else:
            got.extend(spans)

    def claim(self, r: int, c: int, role: str, owner: str, block: str | None = None) -> bool:
        """认领一格（只认领网格里的非空格）。被别的块认领过就记重叠、不改。block 缺省时就是 owner（列表块、
        交叉表的表头行用块 id 认领；交叉表分段里的格 owner 是分段 id，另传块 id）。"""
        prev = self.owners.get((r, c))
        if prev is not None and prev != owner:
            self.overlaps.add((min(prev, owner), max(prev, owner)))
            return False
        self.claims[(r, c)] = role
        self.owners[(r, c)] = owner
        self.cell_block[(r, c)] = block or owner
        return True

    def exclude(self, reason: str, first: int, last: int, cells: int, *, block: str | None = None,
                anchor: str | None = None) -> None:
        """记一段排除的行（rows_excluded）。同一原因、同一块、同一锚点的合成一项。"""
        got = self.excluded.setdefault((reason, block, anchor), [[], 0])
        got[0].append([first, last])
        got[1] += cells

    def excluded_rows(self) -> list[ExcludedRows]:
        out = [ExcludedRows(self.name, reason, _spans_of(rows), n, anchor=anchor, block=block)  # type: ignore[arg-type]
               for (reason, block, anchor), (rows, n) in self.excluded.items()]
        out.sort(key=lambda x: (_EXCLUDED_ORDER.index(x.reason), x.block or "", x.anchor or "", x.rows[0][0]))
        return out


# --------------------------------------------------------------------------
# 交叉表（4.3、4.5、4.6）
# --------------------------------------------------------------------------


@dataclass
class _SegSpec:
    seg: Any
    role: str
    #: 规范写法 → 配方里写的期望标签
    expect: dict[str, str]
    parser: str

    def parsed(self, value: Any) -> Any:
        if self.role == "measures":
            key = match_key(_text(value))
            return key or None
        if self.role == "derived":
            return hour_range_total(value)
        if self.parser == "hour_range":
            return hour_range(value)
        return text_label(_text(value))

    def canonical(self, value: Any) -> str | None:
        p = self.parsed(value)
        if p is None:
            return None
        return p if isinstance(p, str) else p.canonical


def _seg_spec(seg: Any) -> _SegSpec:
    if isinstance(seg, MeasuresSegment):
        spec = _SegSpec(seg, "measures", {}, "measures")
    elif isinstance(seg, DimensionSegment):
        spec = _SegSpec(seg, "dimension", {}, seg.dim.parser)
    else:
        spec = _SegSpec(seg, "derived", {}, "hour_range_total")
    for label in seg.labels.expect:
        key = spec.canonical(label)
        if key is not None:
            spec.expect.setdefault(key, label)
    return spec


@dataclass
class _SegLoc:
    spec: _SegSpec
    rows: list[int]
    title_row: int | None = None
    #: 行 → 解析结果（HourRange / HourTotal / 规范写法）；解析不了的行不在这里
    parsed: dict[int, Any] = field(default_factory=dict)
    #: 规范写法 → 第一次出现的行
    first: dict[str, int] = field(default_factory=dict)
    #: 不进表的行（标签不在期望中的指标行、解析不了的行）
    skip: set[int] = field(default_factory=set)


class _Crosstab:
    def __init__(self, eng: _Engine, sh: _Sheet, block: CrosstabBlock, bi: int) -> None:
        self.eng, self.sh, self.block, self.bi = eng, sh, block, bi
        self.grid: Grid = sh.grid  # type: ignore[assignment]
        self.where = f"工作表「{sh.name}」的交叉表「{block.id}」"
        self.located = False
        self.axis_row = 0
        self.label_c = 0
        self.first_c = self.last_c = 0
        self.axis_cells: list[tuple[int, AxisDate]] = []
        self.kinds: dict[int, str] = {}
        self.specs = [_seg_spec(s) for s in block.segments]
        self.locs: dict[str, _SegLoc] = {}
        self.failed: set[str] = set()
        self.row_owner: dict[int, str] = {}
        self.extra_cols: set[int] = set()
        self.dates: dict[int, _dt.date] | None = None
        self.excluded: set[tuple[int, int]] = set()
        self.cell_index: dict[tuple[int, int], tuple[str, int]] = {}
        self.exclude_hidden = sh.sr.hidden.rows == "exclude"
        #: 期 3 忽略规则：match_key → 配方里的原文（同一键取第一条；同键重复是静态校验的 ignore_duplicate）
        self.ignore_rows: dict[str, str] = {}
        for rule in block.ignore_rows:
            if (k := match_key(rule.label)):
                self.ignore_rows.setdefault(k, rule.label)
        self.ignore_cols = {k for rule in block.ignore_columns if (k := match_key(rule.header))}
        #: 按表头忽略的列（轴行右侧）
        self.ignored_cols: list[int] = []
        #: 本块报出的 title_not_found（候选标题要等所有块认领完，在 post_claims 里补进 fix_args）
        self.title_missing: list[Problem] = []

    # ---------------- 定位 ----------------

    def _seg_where(self, seg_id: str) -> str:
        return f"工作表「{self.sh.name}」的分段「{seg_id}」"

    def _axis_cols(self) -> range:
        return range(self.first_c, self.last_c + 1)

    def locate(self) -> None:
        grid, sh, block = self.grid, self.sh, self.block
        need = block.axis.find.min
        cands: list[tuple[int, list[tuple[int, AxisDate]], list[tuple[int, GridCell]]]] = []
        for r in grid.row_numbers():
            row = grid.row(r)
            dates = [(c, d) for c, cell in row if (d := month_day_or_date(cell.value)) is not None]
            if len(dates) < need or dates[0][0] < 2:
                continue
            label_c = dates[0][0] + block.label_offset
            right = sum(1 for c, _ in row if c >= label_c)
            if len(dates) * 2 > right:
                cands.append((r, dates, row))
        if not cands:
            msg = f"{self.where}：未找到日期表头行：没有一行有至少 {need} 格日期"
            self.eng.problems.add("axis_not_found", msg, msg, sheet=sh.name)
            sh.failed = True
            return
        if len(cands) > 1:
            rows = [r for r, _, _ in cands]
            msg = f"{self.where}：有多行像日期表头（第 {_rows_text(rows)} 行），无法确定用哪一行"
            self.eng.problems.add("axis_ambiguous", msg, msg, [sh.coord(r, d[0][0]) for r, d, _ in cands],
                                  sheet=sh.name)
            sh.failed = True
            return
        r, dates, row = cands[0]
        self.axis_row = r
        self.first_c, self.last_c = dates[0][0], dates[-1][0]
        self.label_c = self.first_c + block.label_offset
        by_col = dict(dates)
        gaps = [c for c in self._axis_cols() if c not in by_col]
        if gaps:
            cells = [sh.coord(r, c) for c in gaps]
            shown = [f"{cell_ref(r, c)}「{_shown(grid.get(r, c).value)}」" if grid.get(r, c) else
                     f"{cell_ref(r, c)}（空）" for c in gaps]
            self.eng.problems.add(
                "axis_gap", f"{self.where}：日期表头（第 {r} 行）中间有不是日期的格：{_join(shown)}",
                f"{self.where}：日期表头（第 {r} 行）中间有不是日期的格：{_join([cell_ref(r, c) for c in gaps])}",
                cells, sheet=sh.name)
            sh.failed = True
            return
        self.axis_cells = dates
        extra = [c for c, _ in row if c > self.last_c]
        # 期 3：表头命中 ignore_columns 的列按表头忽略，不报 axis_extra_cells；其余照期 2
        self.ignored_cols = [c for c in extra if self.ignore_cols
                             and match_key(_text(grid.get(r, c).value)) in self.ignore_cols]  # type: ignore[union-attr]
        extra = [c for c in extra if c not in self.ignored_cols]
        if extra:
            self.extra_cols = set(extra)
            shown = [f"{cell_ref(r, c)}「{_shown(grid.get(r, c).value)}」" for c in extra]
            self.eng.problems.add(
                "axis_extra_cells",
                f"{self.where}：日期表头（第 {r} 行）最后一个日期右边还有内容：{_join(shown)}。请删除这些列，或调整配方",
                f"{self.where}：日期表头（第 {r} 行）最后一个日期右边还有内容：{_join([cell_ref(r, c) for c in extra])}",
                [sh.coord(r, c) for c in extra], sheet=sh.name,
                fix_args={"block": block.id, "how": "columns",
                          "headers": [{"raw": _anchor_text(grid.get(r, c).value), "cell": sh.coord(r, c)}  # type: ignore[union-attr]
                                      for c in extra]})
            for (rr, cc) in grid.cells:
                if rr >= r and cc in self.extra_cols:
                    sh.suppressed.add((rr, cc))
        self.located = True
        if grid.get(r, self.label_c) is not None:
            sh.claim(r, self.label_c, "col_header", block.id)
        for c, _ in dates:
            sh.claim(r, c, "col_header", block.id)
        self._classify_rows()
        self._locate_segments()

    def _claim_ignored_cols(self, bottom: int) -> None:
        """按表头忽略的列：从轴行到本块数据区底部（bottom，本块最后一个数据行，post_claims 算）的格，去向
        ignored_column；表头记进 Extraction.ignored_columns[块]。数据区之下的格不归这条规则管（照常算区域外），
        免得表下的备注、落款被一并吞掉。

        为什么放在 post_claims、底部只按本块自己的行算：kinds 覆盖轴行以下整张工作表，表下另有一张列表时，列表的
        行同样「标签列有字、日期列有值」。按整张表的 data 行取底部、又在列表认领之前先认领，被忽略的这一列会一直
        认领到列表的最后一行，列表再认领同一批格就成了 segment_overlap（结构类、没有修复按钮）：用户按了
        「按表头忽略」，换来一条看不懂的结构错误。已归别的块的格照样跳过，与期 2 多出的列（axis_extra_cells 只
        压掉、不认领）对别的块的效果一致。"""
        if not self.ignored_cols:
            return
        grid, sh, r = self.grid, self.sh, self.axis_row
        for rr in range(r, bottom + 1):
            for cc in self.ignored_cols:
                if grid.get(rr, cc) is not None and (rr, cc) not in sh.owners:
                    sh.claim(rr, cc, "ignored_column", self.block.id)
        self.eng.ex.ignored_columns[self.block.id] = [_text(grid.get(r, c).value).strip()  # type: ignore[union-attr]
                                                      for c in self.ignored_cols]

    def _label(self, r: int) -> GridCell | None:
        return self.grid.get(r, self.label_c)

    def _classify_rows(self) -> None:
        grid, lo, hi = self.grid, self.first_c, self.last_c
        for r in grid.row_numbers():
            if r <= self.axis_row:
                continue
            label = self._label(r)
            axis = any(lo <= c <= hi for c, _ in grid.row(r))
            if label is None and not axis:
                continue
            if label is None:
                self.kinds[r] = "orphan"
            elif axis:
                self.kinds[r] = "data"
            elif any((k := s.canonical(label.value)) is not None and k in s.expect for s in self.specs):
                # 整期为空格的时段行：标签认得出、又在期望里，按数据行处理（否则会被当成分段标题）
                self.kinds[r] = "data"
            else:
                self.kinds[r] = "title"

    def _run(self, start: int, stop_total: bool) -> list[int]:
        rows = []
        r = start
        while self.kinds.get(r) == "data":
            label = self._label(r)
            if stop_total and label is not None and hour_range_total(label.value) is not None:
                break
            rows.append(r)
            r += 1
        return rows

    def _locate_segments(self) -> None:
        pending = list(self.specs)
        progress = True
        while pending and progress:
            progress = False
            for spec in list(pending):
                seg = spec.seg
                if seg.locate.by == "after" and seg.locate.segment not in self.locs:
                    if seg.locate.segment in self.failed or not any(s.seg.id == seg.locate.segment for s in self.specs):
                        pending.remove(spec)
                        self.failed.add(seg.id)
                        progress = True
                    continue
                pending.remove(spec)
                progress = True
                self._locate_one(spec)
        for spec in pending:
            self.failed.add(spec.seg.id)

    def _missing_ok(self) -> bool:
        """部分干跑且网格读不全时，「没找到」类的问题可能只是还没读到，不报。"""
        return self.eng.partial and self.sh.truncated

    def _locate_one(self, spec: _SegSpec) -> None:
        seg, sh, eng = spec.seg, self.sh, self.eng
        where = self._seg_where(seg.id)
        stop_total = isinstance(seg, DimensionSegment) and seg.stop_parser == "hour_range_total"
        title_row = None
        if seg.locate.by == "section_title":
            key = match_key(seg.locate.title or "")
            hits = [r for r, k in sorted(self.kinds.items()) if k == "title"
                    and match_key(_text(self._label(r).value)) == key]  # type: ignore[union-attr]
            if not hits:
                self.failed.add(seg.id)
                if not self._missing_ok():
                    msg = f"{where}：没有找到分段标题「{seg.locate.title}」"
                    p = eng.problems.add("title_not_found", msg, msg, sheet=sh.name,
                                         fix_args={"segment": seg.id, "title": seg.locate.title, "candidates": []})
                    if p is not None:
                        self.title_missing.append(p)
                return
            if len(hits) > 1:
                self.failed.add(seg.id)
                cells = [sh.coord(r, self.label_c) for r in hits]
                msg = f"{where}：分段标题「{seg.locate.title}」出现了不止一次（{_join([c.split('!')[-1] for c in cells])}）"
                eng.problems.add("title_ambiguous", msg, msg, cells, sheet=sh.name)
                return
            title_row = hits[0]
            rows = self._run(title_row + 1, stop_total)
        elif seg.locate.by == "labels":
            first = next((r for r, k in sorted(self.kinds.items()) if k == "data"
                          and (key := spec.canonical(self._label(r).value)) is not None  # type: ignore[union-attr]
                          and key in spec.expect), None)
            if first is None:
                self.failed.add(seg.id)
                if not self._missing_ok():
                    msg = f"{where}：没有找到期望标签所在的行（期望：{_quoted(list(seg.labels.expect))}）"
                    eng.problems.add("segment_not_found", msg, msg, sheet=sh.name)
                return
            start = first
            while self.kinds.get(start - 1) == "data":
                start -= 1
            rows = self._run(start, stop_total)
            if first not in rows:           # 第一处命中本身就是停止行（不会发生在正常配方里）
                rows = self._run(first, False)
        else:
            prev = self.locs[seg.locate.segment]
            rows = self._run(prev.rows[-1] + 1, False) if prev.rows else []
        loc = _SegLoc(spec, rows, title_row)
        # 每行只能属于一个分段
        clash = [r for r in rows if r in self.row_owner]
        if clash:
            self.failed.add(seg.id)
            other = self.row_owner[clash[0]]
            msg = f"{self.where}：分段「{other}」和「{seg.id}」认领了同一批行（第 {_rows_text(clash)} 行）"
            eng.problems.add("segment_overlap", msg, msg, [sh.coord(r, self.label_c) for r in clash], sheet=sh.name)
            return
        for r in rows:
            self.row_owner[r] = seg.id
        self.locs[seg.id] = loc
        self._check_labels(loc)
        self._claim(loc)
        if isinstance(seg, DimensionSegment) and seg.const and title_row is not None:
            title_raw = _text(self._label(title_row).value)  # type: ignore[union-attr]
            for name in sorted(seg.const):
                pick = seg.const[name].pick
                if canon(pick) not in canon(title_raw):
                    ref = cell_ref(title_row, self.label_c)
                    eng.problems.add(
                        "const_missing",
                        f"{where}：常量「{name}」的取值「{pick}」不在分段标题「{_shown(title_raw, 40)}」中",
                        f"{where}：常量「{name}」的取值「{pick}」不在 {ref} 的分段标题中",
                        [sh.coord(title_row, self.label_c)], sheet=sh.name)
        first_row = title_row if title_row is not None else (rows[0] if rows else None)
        if first_row is not None:
            sh.order.append((first_row, seg.id))

    def _check_labels(self, loc: _SegLoc) -> None:
        spec, sh, eng = loc.spec, self.sh, self.eng
        seg = spec.seg
        where = self._seg_where(seg.id)
        raw, canonical = [], []
        unparsed, dup, unexpected = [], [], []
        for r in loc.rows:
            value = self._label(r).value  # type: ignore[union-attr]
            raw.append(_text(value))
            p = spec.parsed(value)
            if p is None:
                canonical.append("")
                unparsed.append(r)
                loc.skip.add(r)
                continue
            key = p if isinstance(p, str) else p.canonical
            canonical.append(key)
            loc.parsed[r] = p
            if key in loc.first:
                dup.append(r)
                if spec.role == "measures":
                    loc.skip.add(r)
                continue
            loc.first[key] = r
            if key not in spec.expect:
                unexpected.append(r)
                if spec.role == "measures":
                    loc.skip.add(r)
        eng.ex.labels.append(SegmentLabels(seg.id, raw, canonical, list(loc.rows)))
        lc = self.label_c
        if unparsed:
            cells = [sh.coord(r, lc) for r in unparsed]
            shown = [f"{cell_ref(r, lc)}「{_shown(self._label(r).value)}」" for r in unparsed]  # type: ignore[union-attr]
            name = _PARSER_NAME[spec.parser]
            # declare_total 的前提之一：解析不了的行都在分段末尾、连成一段（P3-SPEC 3.2 ⑤）
            at_end = unparsed == loc.rows[len(loc.rows) - len(unparsed):]
            eng.problems.add(
                "label_unparsed", f"{where}：{_join(shown)}的标签无法按{name}解析",
                f"{where}：{_join([cell_ref(r, lc) for r in unparsed])} 的标签无法按{name}解析", cells, sheet=sh.name,
                fix_args={"segment": seg.id, "role": spec.role, "at_end": at_end,
                          "labels": [{"raw": _text(self._label(r).value), "cell": sh.coord(r, lc),  # type: ignore[union-attr]
                                      "total": hour_range_total(self._label(r).value) is not None}  # type: ignore[union-attr]
                                     for r in unparsed]})
        if unexpected:
            cells = [sh.coord(r, lc) for r in unexpected]
            shown = [f"{cell_ref(r, lc)}「{_shown(self._label(r).value)}」" for r in unexpected]  # type: ignore[union-attr]
            eng.problems.add(
                "label_unexpected", f"{where}：{_join(shown)}的标签不在期望的标签中",
                f"{where}：{_join([cell_ref(r, lc) for r in unexpected])} 的标签不在期望的标签中", cells, sheet=sh.name,
                fix_args={"segment": seg.id, "role": spec.role,
                          "labels": [{"raw": _text(self._label(r).value), "cell": sh.coord(r, lc)}  # type: ignore[union-attr]
                                     for r in unexpected]})
        if dup:
            cells = [sh.coord(r, lc) for r in dup]
            pairs = [f"{cell_ref(loc.first[canonical[loc.rows.index(r)]], lc)} 与 {cell_ref(r, lc)}" for r in dup]
            shown = [f"「{canonical[loc.rows.index(r)]}」（{p}）" for r, p in zip(dup, pairs)]
            eng.problems.add("label_duplicate", f"{where}：标签重复：{_join(shown)}",
                             f"{where}：标签重复：{_join(pairs)}", cells, sheet=sh.name)
        missing = [label for key, label in spec.expect.items() if key not in loc.first]
        edge = self._missing_ok() and (not loc.rows or loc.rows[-1] >= self.sh.limit - 1)
        if missing and not edge:
            cells = [sh.coord(loc.rows[0], lc)] if loc.rows else (
                [sh.coord(loc.title_row, lc)] if loc.title_row else [])
            msg = f"{where}：期望的标签{_quoted(missing)}没有找到"
            eng.problems.add("label_missing", msg, msg, cells, sheet=sh.name,
                             fix_args={"segment": seg.id, "labels": list(missing)})

    def _claim(self, loc: _SegLoc) -> None:
        sh, grid, seg, bid = self.sh, self.grid, loc.spec.seg, self.block.id
        derived = loc.spec.role == "derived"
        # 只核对、不另存的合计行（keep_as 为空）：参与核对，但不进任何表，记进「排除的行」（遗留项 4 的 total_not_kept）
        not_kept = derived and seg.keep_as is None
        if loc.title_row is not None:
            sh.claim(loc.title_row, self.label_c, "section_title", seg.id, bid)
        for r in loc.rows:
            sh.data_rows.add(r)
            excluded = self.exclude_hidden and sh.hidden_row(r)
            label_role = "hidden_excluded" if excluded else ("derived_label" if derived else "row_label")
            value_role = "hidden_excluded" if excluded else ("derived_value" if derived else "value")
            # 区域按标签列和整段日期列出（某天是空格的行不另起矩形）
            sh.layout(r, [(self.label_c, self.label_c, label_role, bid), (self.first_c, self.last_c, value_role, bid)])
            n = int(sh.claim(r, self.label_c, label_role, seg.id, bid))
            for c, _ in grid.row(r):
                if self.first_c <= c <= self.last_c:
                    n += sh.claim(r, c, value_role, seg.id, bid)
                    if excluded:
                        self.excluded.add((r, c))
            if not_kept and not excluded and n:
                sh.exclude("total_not_kept", r, r, n, block=bid)

    def post_claims(self) -> None:
        """所有块都认领完之后：没有分段认领的数据行、没有标签的行、数据区的合并单元格。"""
        if not self.located:
            return
        sh, grid, eng = self.sh, self.grid, self.eng
        lc, bid = self.label_c, self.block.id
        # 本块的数据行（按表头忽略的列认领到哪一行为止）：分段认领的行，加上下面没有分段认领、又不归别的块的
        # 数据行（按 ignore_rows 忽略的、报 row_unclaimed 的、分段没定位到而压掉的），都是本块要负责的行
        own = set(self.row_owner)
        for r, kind in sorted(self.kinds.items()):
            if kind == "title" or r in self.row_owner:
                continue
            row = grid.row(r)
            if any((r, c) in sh.owners and sh.owners[(r, c)] != bid for c, _ in row):
                continue                    # 别的块（列表）认领了这一行
            if kind == "data":
                own.add(r)
            if kind == "data" and self.ignore_rows and self._ignore_row(r, row):
                continue
            for c, _ in row:
                sh.suppressed.add((r, c))
            if kind == "data":
                if self.failed:
                    continue                # 有分段没定位到：这些行多半就是它的，不再逐行报
                coord = sh.coord(r, lc)
                label = self._label(r).value  # type: ignore[union-attr]
                # 修复按钮只有「按行标签忽略」（3.1：把标签加进分段认领不到隔了空行的这一行）。标签不是文字（数、
                # 像数的文字）时没有能写进 ignore_rows 的锚点，不给按钮（fix_args 为空，fix 随之为空），提示也不提
                # 按标签忽略；框选成分段同样要按标签文字认，也不提
                text = _anchor_text(label)
                if text is None:
                    hint = "它的标签不是文字（例如是一个数），无法按标签忽略。请删除这一行，或把标签改成文字后重新上传"
                    args = None
                else:
                    hint = "请删除这一行，或按行标签忽略这一行；也可以在网格上把它框选为新的分段"
                    args = {"block": bid, "how": "rows", "labels": [{"raw": text, "cell": coord}]}
                eng.problems.add(
                    "row_unclaimed",
                    f"工作表「{sh.name}」第 {r} 行（{cell_ref(r, lc)}「{_shown(label)}」）有数据，"
                    f"但没有分段认领这一行。{hint}",
                    f"工作表「{sh.name}」第 {r} 行有数据，但没有分段认领这一行（标签在 {cell_ref(r, lc)}）",
                    [coord] + [sh.coord(r, c) for c, _ in row if c != lc], sheet=sh.name, fix_args=args)
            else:
                msg = f"工作表「{sh.name}」第 {r} 行有数据，但标签列 {cell_ref(r, lc)} 是空的"
                eng.problems.add("row_without_label", msg, msg, [sh.coord(r, c) for c, _ in row], sheet=sh.name)
        self._claim_ignored_cols(max(own, default=self.axis_row))
        data = {r for r, k in self.kinds.items() if k == "data"}
        for r1, c1, r2, c2 in grid.merges:
            if c2 < self.first_c or c1 > self.last_c:
                continue
            if any(r in data for r in range(max(r1, self.axis_row + 1), r2 + 1)):
                ref = cell_ref(r1, c1) if (r1, c1) == (r2, c2) else f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}"
                eng.problems.issue("merged_in_values", sh.name, self.where, sh.coord(r1, c1), ref)
        if self.title_missing:
            # rename_title 的候选：本块里像分段标题、又没被任何块认领的行（按行号升序）。要等所有块都认领完：
            # 后面的分段、列表块可能还会认领标题行（P3-SPEC 3.2 ④）
            cands = []
            for r, kind in sorted(self.kinds.items()):
                if kind != "title" or (r, lc) in sh.owners:
                    continue
                text = _anchor_text(self._label(r).value)  # type: ignore[union-attr]
                if text is not None and len(cands) < CELLS_MAX:
                    cands.append({"cell": sh.coord(r, lc), "text": text})
            for p in self.title_missing:
                p.fix_args["candidates"] = list(cands)  # type: ignore[index]

    def _ignore_row(self, r: int, row: list[tuple[int, GridCell]]) -> bool:
        """没有分段认领的数据行，标签命中 ignore_rows 时按配方忽略：标签格和日期列上的格去向 ignored，不报
        row_unclaimed，记进「排除的行」。这一行别处的格（标签左边、轴行右侧没有表头的列）不归这条规则管，照常
        算区域外：规则只说了忽略这一行的数据。ignored 不算块认领的格（不在 _BLOCK_ROLES）：这一行即使隐藏了
        也不报 hidden_rows，反正不导入。"""
        sh, bid = self.sh, self.block.id
        rule = self.ignore_rows.get(match_key(_text(self._label(r).value)))  # type: ignore[union-attr]
        if rule is None:
            return False
        n = 0
        for c, _ in row:
            if c == self.label_c or self.first_c <= c <= self.last_c:
                n += sh.claim(r, c, "ignored", bid)
        sh.layout(r, [(self.label_c, self.label_c, "ignored", bid), (self.first_c, self.last_c, "ignored", bid)])
        if n:
            sh.exclude("ignored_rows", r, r, n, block=bid, anchor=rule)
        return True

    # ---------------- 年份与断言 ----------------

    def complete_axis(self, period: Period | None, period_reported: bool) -> None:
        if not self.located:
            return
        sh, eng, axis = self.sh, self.eng, self.block.axis
        r = self.axis_row
        resolved: dict[int, _dt.date] = {}
        missing: list[tuple[int, str]] = []
        ambiguous: list[int] = []
        blocked = False
        for c, d in self.axis_cells:
            if d.year is not None:
                try:
                    resolved[c] = _dt.date(d.year, d.month, d.day)
                except ValueError:
                    missing.append((c, "（日期不合法）"))
                continue
            if axis.year_from is None:
                missing.append((c, "（表头没有写年份，配方也没有指定年份来源）"))
                continue
            if period is None:
                if period_reported:
                    blocked = True          # 统计期待录入：已经报了 period_missing，这里不重复
                else:
                    missing.append((c, "（没有统计期，无法补全年份）"))
                continue
            hits = []
            for y in range(period.start.year, period.end.year + 1):
                try:
                    day = _dt.date(y, d.month, d.day)
                except ValueError:
                    continue
                if period.start <= day <= period.end:
                    hits.append(day)
            if len(hits) == 1:
                resolved[c] = hits[0]
            elif hits:
                ambiguous.append(c)
            else:
                missing.append((c, "（不在统计期内）"))
        grid = self.grid
        if missing:
            cells = [sh.coord(r, c) for c, _ in missing]
            shown = [f"{cell_ref(r, c)}「{_shown(grid.get(r, c).value)}」{why}" for c, why in missing]  # type: ignore[union-attr]
            eng.problems.add("axis_year_missing", f"{self.where}：日期表头无法确定年份：{_join(shown)}",
                             f"{self.where}：日期表头无法确定年份：{_join([cell_ref(r, c) + why for c, why in missing])}",
                             cells, sheet=sh.name)
        if ambiguous:
            cells = [sh.coord(r, c) for c in ambiguous]
            msg = f"{self.where}：统计期跨了不止一年，日期表头的年份有多种可能：{_join([cell_ref(r, c) for c in ambiguous])}"
            eng.problems.add("axis_year_ambiguous", msg, msg, cells, sheet=sh.name)
        form = {d.form for _, d in self.axis_cells}
        first_txt = _shown(grid.get(r, self.first_c).value)  # type: ignore[union-attr]
        last_txt = _shown(grid.get(r, self.last_c).value)  # type: ignore[union-attr]
        if missing or ambiguous or blocked:
            eng.ex.axes.append(AxisOut(self.block.id, r, first_txt, last_txt, len(self.axis_cells),
                                       form.pop() if len(form) == 1 else "mixed"))
            return
        ordered = [(c, resolved[c]) for c, _ in self.axis_cells]
        seen: dict[_dt.date, int] = {}
        dups: list[tuple[int, int]] = []
        for c, day in ordered:
            if day in seen:
                dups.append((seen[day], c))
            else:
                seen[day] = c
        if dups:
            cells = [sh.coord(r, c) for pair in dups for c in pair]
            eng.problems.add(
                "axis_duplicate",
                f"{self.where}：日期表头有重复的日期："
                + _join([f"{resolved[a].isoformat()} 出现在 {cell_ref(r, a)} 和 {cell_ref(r, b)}" for a, b in dups]),
                f"{self.where}：日期表头有重复的日期：" + _join([f"{cell_ref(r, a)} 和 {cell_ref(r, b)}" for a, b in dups]),
                cells, sheet=sh.name)
        # 有重复日期时「不连续」「没有恰好覆盖」都只是它的后果，不再另报
        if "contiguous" in axis.checks and not dups:
            for (ca, da), (cb, db) in zip(ordered, ordered[1:]):
                if db - da != _dt.timedelta(days=1):
                    eng.problems.add(
                        "axis_not_contiguous",
                        f"{self.where}：日期表头不是逐日连续的：{cell_ref(r, ca)}（{da.isoformat()}）之后是 "
                        f"{cell_ref(r, cb)}（{db.isoformat()}）",
                        f"{self.where}：日期表头不是逐日连续的：{cell_ref(r, ca)} 之后是 {cell_ref(r, cb)}",
                        [sh.coord(r, ca), sh.coord(r, cb)], sheet=sh.name)
                    break
        if "covers_context" in axis.checks and period is not None and not eng.partial and not dups:
            days = {period.start + _dt.timedelta(days=i) for i in range(period.days)}
            if set(resolved.values()) != days or len(ordered) != len(days):
                eng.problems.add(
                    "axis_coverage",
                    f"{self.where}：第 {r} 行的日期是 {first_txt} 至 {last_txt} 共 {len(ordered)} 格，"
                    f"统计期 {period.start.isoformat()} 至 {period.end.isoformat()} 共 {period.days} 天",
                    f"{self.where}：第 {r} 行的日期表头共 {len(ordered)} 格，没有恰好覆盖统计期（共 {period.days} 天）",
                    [sh.coord(r, self.first_c), sh.coord(r, self.last_c)], sheet=sh.name)
        eng.ex.axes.append(AxisOut(self.block.id, r, ordered[0][1].isoformat(), ordered[-1][1].isoformat(),
                                   len(ordered), form.pop() if len(form) == 1 else "mixed"))
        if not dups:
            self.dates = resolved

    # ---------------- 抽取 ----------------

    def extract(self) -> None:
        if not self.located:
            return
        eng, sh = self.eng, self.sh
        order = sorted(self.locs.values(), key=lambda loc: (loc.title_row or (loc.rows[0] if loc.rows else 0)))
        for loc in order:
            if loc.spec.seg.id in self.failed:
                continue
            if loc.spec.role == "measures":
                self._measures(loc)
            elif loc.spec.role == "dimension":
                self._dimension(loc)
        for loc in order:
            if loc.spec.role == "derived" and loc.spec.seg.id not in self.failed:
                self._derived(loc)

    def _value(self, r: int, c: int, where: str) -> Any:
        return self.eng.convert_number(self.grid.get(r, c), self.block.values.type, self.block.values,
                                       self.sh.name, where, r, c, data_area=True)

    def _measures(self, loc: _SegLoc) -> None:
        eng, sh, seg = self.eng, self.sh, loc.spec.seg
        where = self._seg_where(seg.id)
        sink = eng.sinks[seg.table]
        sink.sources.append(f"{sh.sr.id}/{self.block.id}/{seg.id}")
        rows_by_label: dict[str, int] = {}
        for key, label in loc.spec.expect.items():
            r = loc.first.get(key)
            if r is not None and r not in loc.skip:
                rows_by_label[label] = r
                eng.headers[(seg.table, seg.measures.get(label, ""))] = _text(self._label(r).value)  # type: ignore[union-attr]
        # 值保留 _FAIL（已报过问题），交给 _Sink 按空值存；没找到的标签（已报 label_missing）也按 _FAIL
        values: dict[tuple[str, int], Any] = {}
        for label, r in rows_by_label.items():
            excluded = self.exclude_hidden and sh.hidden_row(r)
            for c in self._axis_cols():
                values[(label, c)] = None if excluded else self._value(r, c, where)
        if self.dates is None:
            return
        for c in self._axis_cols():
            row: list[Any] = [self.dates[c].isoformat()]
            cells: list[tuple[str, int, int]] = []
            for label in seg.labels.expect:
                row.append(values.get((label, c), _FAIL))
                r = rows_by_label.get(label)
                if r is not None:
                    cells.append((seg.measures.get(label, ""), r, c))
            sink.add(row, sheet=sh.name, at=(self.axis_row, c), cells=cells, fails=True)

    def _derive_values(self, derive: dict[str, str], hr: Any) -> list[Any]:
        ordered = sorted(derive.items(), key=lambda kv: (kv[1] != "start", kv[0]))
        if not isinstance(hr, (HourRange, HourTotal)):
            return [None for _ in ordered]
        return [hr.start if role == "start" else hr.end for _, role in ordered]

    def _dimension(self, loc: _SegLoc) -> None:
        eng, sh, seg = self.eng, self.sh, loc.spec.seg
        where = self._seg_where(seg.id)
        sink = eng.sinks[seg.table]
        sink.sources.append(f"{sh.sr.id}/{self.block.id}/{seg.id}")
        consts = [seg.const[name].pick for name in sorted(seg.const)]
        for r in loc.rows:
            if r in loc.skip or r not in loc.parsed:
                continue
            excluded = self.exclude_hidden and sh.hidden_row(r)
            if excluded:
                continue
            p = loc.parsed[r]
            dim = p if isinstance(p, str) else p.canonical
            derives = self._derive_values(seg.dim.derive, p)
            for c in self._axis_cols():
                v = self._value(r, c, where)
                if self.dates is None:
                    continue
                row = [self.dates[c].isoformat(), dim, *consts, *derives, v]
                rowid = sink.add(row, sheet=sh.name, at=(r, c), cells=[(seg.value, r, c)], fails=True)
                if rowid is not None:
                    self.cell_index[(r, c)] = (seg.table, rowid)

    def _derived(self, loc: _SegLoc) -> None:
        eng, sh, seg, grid = self.eng, self.sh, loc.spec.seg, self.grid
        where = self._seg_where(seg.id)
        prev = self.locs.get(seg.locate.segment or "")
        prev_seg = prev.spec.seg if prev is not None else None
        base = seg.verify.against_table
        start_col = end_col = None
        writers = [s.seg for s in self.specs if isinstance(s.seg, DimensionSegment) and s.seg.table == base]
        if isinstance(prev_seg, DimensionSegment) and prev_seg.table == base:
            writers.insert(0, prev_seg)
        for w in writers:
            for name, role in w.dim.derive.items():
                if role == "start" and start_col is None:
                    start_col = name
                if role == "end" and end_col is None:
                    end_col = name
        consts = {name: c.pick for name, c in sorted(prev_seg.const.items())} if isinstance(
            prev_seg, DimensionSegment) else {}
        keep = seg.keep_as
        sink = eng.sinks.get(keep.table) if keep is not None else None
        if sink is not None:
            sink.sources.append(f"{sh.sr.id}/{self.block.id}/{seg.id}")
        forms: set[str] = set()
        for r in loc.rows:
            for c in self._axis_cols():
                cell = grid.get(r, c)
                if cell is not None:
                    forms.add("formula" if cell.formula is not None else "literal")
        eng.ex.derived_form[seg.id] = forms.pop() if len(forms) == 1 else ("mixed" if forms else "literal")
        if self.dates is None:
            for r in loc.rows:          # 类型问题照样报
                for c in self._axis_cols():
                    eng.convert_number(grid.get(r, c), self.block.values.type, self.block.values, sh.name, where,
                                       r, c, data_area=False)
            return
        for r in loc.rows:
            total = loc.parsed.get(r)
            if not isinstance(total, HourTotal) or r in loc.skip:
                continue
            if self.exclude_hidden and sh.hidden_row(r):
                continue                    # 排除的隐藏行：合计行本身也不核对、不另存
            label_raw = _text(self._label(r).value)  # type: ignore[union-attr]
            hidden_in_range = prev is not None and any(
                sh.hidden_row(pr) for pr, hr in prev.parsed.items()
                if isinstance(hr, HourRange) and hr.start >= total.start and hr.end <= total.end)
            derives = self._derive_values(keep.derive, total) if keep is not None else []
            for c in self._axis_cols():
                cell = grid.get(r, c)
                coord = sh.coord(r, c)
                got = eng.convert_number(cell, self.block.values.type, self.block.values, sh.name, where, r, c,
                                         data_area=False)
                value = None if got is _FAIL else got
                item = DerivedItem(
                    kind="label_range_sum", segment=seg.id, sheet=sh.name, cell=coord, label_raw=label_raw,
                    label=total.canonical, value=value, is_formula=cell is not None and cell.formula is not None,
                    formula=cell.formula if cell is not None else None, base_table=base,
                    base_value=seg.verify.value, start=total.start, end=total.end,
                    key={self.block.axis.name: self.dates[c].isoformat(), **consts},
                    start_col=start_col, end_col=end_col, hidden_in_range=hidden_in_range,
                    keep_table=keep.table if keep is not None else None)
                if item.is_formula:
                    eng.resolve_refs(item, item.formula or "", lambda rc: self._ref_lookup(rc, base))
                eng.ex.derived.append(item)
                if sink is not None and keep is not None:
                    sink.add([self.dates[c].isoformat(), total.canonical, *derives, got], sheet=sh.name,
                             at=(r, c), cells=[(keep.value, r, c)], fails=True)

    def _ref_lookup(self, rc: tuple[int, int], base: str) -> str | int:
        hit = self.cell_index.get(rc)
        if hit is not None and hit[0] == base:
            return hit[1]
        if rc in self.excluded:
            return "hidden"
        return "outside"


# --------------------------------------------------------------------------
# 列表（4.4、4.5）
# --------------------------------------------------------------------------

Emit = Callable[[int, list[tuple[int, GridCell]], dict[int, str], bool], None]


class _ListRun:
    """一个列表块在一张工作表上的一遍执行。逐行喂入（行号递增、只喂有非空格的行），网格和流式共用。

    每一行最后都经 emit 交出去（行号、格、本块的认领、是不是本块的数据行）：网格路径把认领记进工作表，
    流式路径当场做格子账。找表头要先看完窗口里的行，所以窗口内的行先缓存，定下表头再按顺序交出。"""

    def __init__(self, eng: _Engine, sh: _Sheet, block: ListBlock, bi: int, emit: Emit) -> None:
        self.eng, self.sh, self.block, self.bi, self.emit = eng, sh, block, bi, emit
        self.where = f"工作表「{sh.name}」的列表「{block.id}」"
        self.n = block.header_rows
        self.cols = block.columns
        self.title_key = match_key(block.after_title) if block.after_title else None
        #: 同一工作表上别的列表块的 after_title：窗口里已经有候选表头之后再碰到它，窗口就到它为止（上下两张
        #: 表头相同的时候，窗口不越过下一张表的标题，「恰好一处」才判得出来）。还没有候选表头时碰到的别块标题
        #: 不截断：没写 after_title 的块从已用区域首行找起，首行往往正是上面那张表的标题
        self.stop_keys = {match_key(b.after_title) for b in sh.sr.blocks
                          if isinstance(b, ListBlock) and b.after_title and b.id != block.id} - {self.title_key}
        self.state = "title" if self.title_key else "header"
        self.title_hits: list[str] = []
        self.window_start: int | None = None
        self.buffer: list[tuple[int, list[tuple[int, GridCell]]]] = []
        self.located = False
        self.header_top = 0
        self.c1 = self.c2 = 0
        self.colmap: dict[int, int] = {}
        self.ignored: set[int] = set()
        self.headers_read: dict[int, str] = {}
        self.last_row = 0
        self.done = False
        self.stop: tuple[str, int | None] | None = None
        #: skip 模式下，上一条数据行之后已经按空行跳过的几段（起、止）：随后判定表已结束（表下的说明文字）时要撤回
        #: 计数和排除的行，那几行不是「数据中间的空行」，而是表的下边界
        self.blank_run: list[tuple[int, int]] = []
        self.row_rowid: dict[int, int] = {}
        self.rowid_first: int | None = None
        self.rowid_last: int | None = None
        #: 表头之下读到的数据行数（不管写没写进库）：一行都没有时报 list_empty（AU-1）
        self.data_rows = 0
        self.hidden_seen = False
        self.excluded_rows: set[int] = set()
        self.merges: _MergeSweep | None = None
        self.merge_judged: dict[tuple[int, int, int, int], bool] = {}
        self.merge_bad: list[tuple[int, int, int, int]] = []
        self.topleft: dict[tuple[int, int, int, int], GridCell | None] = {}
        spec = eng.specs.get(block.table)
        self.grain = set(spec.grain) if spec else set()
        self.sink = eng.sinks[block.table]
        #: 期 3：按表头忽略的列（ignore_columns）的 match_key。只管命中的列，其余多出的列照 extra_columns
        self.ignore_keys = {k for rule in block.ignore_columns if (k := match_key(rule.header))}
        total = block.rows.total_row
        self.total_idx = next((i for i, c in enumerate(self.cols) if total and c.name == total.label_column), None)

    # ---------------- 喂入 ----------------

    def feed(self, r: int, cells: list[tuple[int, GridCell]]) -> None:
        if self.title_key is not None:
            # 标题是文字格：只对不太长的文本算 match_key（大列表逐格规范化要好几秒）
            hit = next((c for c, cell in cells if type(cell.value) is str and len(cell.value) <= _TITLE_SCAN_MAX
                        and match_key(cell.value) == self.title_key), None)
            if hit is not None:
                self.title_hits.append(self.sh.coord(r, hit))
                if self.state == "title":
                    self.state = "header"
                    self.window_start = r + 1
                    self.emit(r, cells, {}, False)
                    return
        if self.state == "title":
            self.emit(r, cells, {}, False)
            return
        if self.state == "header":
            if self.window_start is None:
                self.window_start = r
            in_window = r <= self.window_start + ANCHOR_WINDOW - 1 + self.n - 1
            cands = None
            if in_window and self.stop_keys and self.buffer and any(
                    type(cell.value) is str and len(cell.value) <= _TITLE_SCAN_MAX
                    and match_key(cell.value) in self.stop_keys for _, cell in cells):
                cands = self._candidates(self._window_top(r)) or None
            if in_window and cands is None:
                self.buffer.append((r, cells))
                return
            self._decide(before=r if cands is not None else None, cands=cands)
        if self.state == "data":
            self._data_row(r, cells)
        else:
            self.emit(r, cells, {}, False)

    def finish(self) -> None:
        if self.state == "title":
            if not (self.eng.partial and self.sh.truncated):
                msg = f"工作表「{self.sh.name}」中没有找到标题「{self.block.after_title}」（列表「{self.block.id}」只在这行之后找表头）"
                self.eng.problems.add("title_not_found", msg, msg, sheet=self.sh.name)
            self.sh.failed = True
        elif self.state == "header":
            self._decide()
        if self.state == "data" and not self.done:
            self.done = True
            self.stop = ("end", None)
        if self.located and self.data_rows == 0 and not (self.eng.partial and self.sh.truncated):
            # 只有表头、没有数据行（导出出错，或本月确实没有记录）：表会被整个替换成 0 行。不拒收（没有记录也是
            # 合理的），但要人确认（AU-1）；没有数据行时关系核对判无法核对，也不能写「已核对」
            head = self.sh.coord(self.header_top, self.c1)
            msg = (f"{self.where}的表头之下没有数据行：本期这张表将导入 0 行。"
                   "如果原表本期确实没有记录，确认后可以导入；否则请检查导出的文件")
            model = f"{self.where}的表头（{head.split('!')[-1]}）之下没有数据行"
            self.eng.problems.add("list_empty", msg, model, [head], sheet=self.sh.name)
        self._check_total_found()
        if self.title_key is not None and len(self.title_hits) > 1:
            msg = (f"工作表「{self.sh.name}」中标题「{self.block.after_title}」出现了不止一次"
                   f"（{_join([c.split('!')[-1] for c in self.title_hits])}），列表「{self.block.id}」无法确定从哪里找表头")
            self.eng.problems.add("title_ambiguous", msg, msg, self.title_hits, sheet=self.sh.name)
        for m in self.merge_bad:
            r1, c1, r2, c2 = m
            ref = cell_ref(r1, c1) if (r1, c1) == (r2, c2) else f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}"
            self.eng.problems.issue("merged_in_values", self.sh.name, self.where, self.sh.coord(r1, c1), ref)
        if self.located:
            for i, text in self.headers_read.items():
                self.eng.headers[(self.block.table, self.cols[i].name)] = text

    # ---------------- 表头 ----------------

    def _compose(self, rows: dict[int, dict[int, GridCell]], t: int,
                 merges: list[tuple[int, int, int, int]]) -> tuple[dict[int, str], dict[int, str]]:
        """以 t 为首行的表头：(列 → 表头文字, 列 → 表头原文)。表头文字去了各段首尾空白，比对、报错用；
        原文保留每段的原样（「地区　」「销量\\n」），填进 TableOut.columns[].header，差异卡的 header_writing
        才看得到这类写法变化（4.4、9.3）。判空、判重（与上一层相同）都按去了空白的文字。"""
        n = self.n
        cols: set[int] = set()
        for hr in range(t, t + n):
            cols.update(rows.get(hr, {}))
        local: list[tuple[int, int, int, int]] = []
        if n > 1:
            local = [m for m in merges if t <= m[0] <= t + n - 1]
            for m in local:
                if (m[0], m[1]) in ((hr, c) for hr in range(t, t + n) for c in rows.get(hr, {})):
                    cols.update(range(m[1], m[3] + 1))
        out: dict[int, str] = {}
        raw: dict[int, str] = {}
        for c in sorted(cols):
            parts: list[str] = []
            raws: list[str] = []
            for hr in range(t, t + n):
                cell = rows.get(hr, {}).get(c)
                original = _text(cell.value) if cell is not None else ""
                text = original.strip()
                if not text and local:
                    m = next((m for m in local if m[0] <= hr <= m[2] and m[1] <= c <= m[3]), None)
                    if m is not None:
                        tl = rows.get(m[0], {}).get(m[1])
                        original = _text(tl.value) if tl is not None else ""
                        text = original.strip()
                if text and (not parts or canon(parts[-1]) != canon(text)):
                    parts.append(text)
                    raws.append(original)
            if parts:
                out[c] = "_".join(parts)
                raw[c] = "_".join(raws)
        return out, raw

    def _keys(self) -> dict[str, int]:
        """配方表头的 match_key → 列序号（同名的取第一个）。"""
        keys = self.__dict__.get("_keys_cache")
        if keys is None:
            keys = {}
            for i, col in enumerate(self.cols):
                keys.setdefault(match_key(col.header), i)
            self.__dict__["_keys_cache"] = keys
        return keys

    def _window_top(self, before: int | None) -> int:
        """表头首行最晚可以在哪一行：窗口末尾；碰到别块标题时，表头必须整个在那一行之上。"""
        last_top = (self.window_start or 0) + ANCHOR_WINDOW - 1
        if before is not None:
            last_top = min(last_top, before - self.n)
        return last_top

    def _candidates(self, last_top: int) -> list[tuple[int, int, list[int], dict[int, str], dict[int, str]]]:
        """缓存的窗口里首行不晚于 last_top 的候选表头：(命中列数, 首行, 列段, 表头文字, 表头原文)。"""
        rows = {r: dict(cells) for r, cells in self.buffer}
        start = self.window_start or 0
        keys = self._keys()
        need = len(self.cols)
        merges = [m for m in self.sh.merges if m[0] <= last_top + self.n and m[2] >= start] if self.n > 1 else []
        cands: list[tuple[int, int, list[int], dict[int, str], dict[int, str]]] = []
        for t in sorted(rows):
            if t > last_top:
                break
            composed, raw = self._compose(rows, t, merges)
            run: list[int] = []
            for c in sorted(composed) + [None]:  # type: ignore[list-item]
                if c is not None and (not run or c == run[-1] + 1):
                    run.append(c)
                    continue
                if run:
                    hit = {keys[k] for cc in run if (k := match_key(composed[cc])) in keys}
                    if len(hit) * 2 > need:
                        cands.append((len(hit), t, run, composed, raw))
                run = [c] if c is not None else []
        return cands

    def _decide(self, before: int | None = None,
                cands: list[tuple[int, int, list[int], dict[int, str], dict[int, str]]] | None = None) -> None:
        sh, eng = self.sh, self.eng
        start = self.window_start or 0
        keys = self._keys()
        need = len(self.cols)
        last_top = self._window_top(before)
        if cands is None:
            cands = self._candidates(last_top)
        if not cands:
            if not (eng.partial and sh.truncated and not self.buffer):
                # 窗口按实际读到的范围写：已用区域只有 10 行时不写「第 1–200 行」；窗口是空的（标题就在最后
                # 一行、工作表没有内容）时不拼出「第 5–4 行」
                hi = min(last_top, sh.limit) if sh.limit else last_top
                span = (f"在第 {start} 行中" if hi == start else f"在第 {start}–{hi} 行中") if (
                    self.window_start is not None and hi >= start) else ""
                msg = f"{self.where}：{span}没有找到表头（配方的表头：{_quoted([c.header for c in self.cols])}）"
                eng.problems.add("header_not_found", msg, msg, sheet=sh.name)
            self._fail()
            return
        best = max(x[0] for x in cands)
        tops = [x for x in cands if x[0] == best]
        if len(tops) > 1:
            msg = f"{self.where}：有多处都像表头（{_join([cell_ref(t, run[0]) for _, t, run, _, _ in tops])}），无法确定用哪一处"
            eng.problems.add("header_ambiguous", msg, msg, [sh.coord(t, run[0]) for _, t, run, _, _ in tops],
                             sheet=sh.name)
            self._fail()
            return
        _, t, run, composed, raw = tops[0]
        self.header_top, self.c1, self.c2 = t, run[0], run[-1]
        extra: list[int] = []
        dup: dict[int, list[int]] = {}
        for c in run:
            k = match_key(composed[c])
            i = keys.get(k)
            if i is None:
                extra.append(c)
            elif i in self.colmap:
                dup.setdefault(i, [self.colmap[i]]).append(c)
            else:
                self.colmap[i] = c
                self.headers_read[i] = raw[c]
        hb = t + self.n - 1
        bad = False
        missing = [self.cols[i].header for i in range(need) if i not in self.colmap]
        if missing:
            msg = f"{self.where}：表头（第 {t} 行）中没有找到列{_quoted(missing)}"
            eng.problems.add("column_missing", msg, msg, [sh.coord(t, self.c1)], sheet=sh.name)
            bad = True
        if dup:
            for i, cs in dup.items():
                msg = f"{self.where}：表头「{self.cols[i].header}」出现了不止一次（{_join([cell_ref(hb, c) for c in cs])}）"
                eng.problems.add("header_ambiguous", msg, msg, [sh.coord(hb, c) for c in cs], sheet=sh.name)
            bad = True
        if extra:
            # 期 3：表头命中 ignore_columns 的列按期 2 extra_columns=ignore 的方式忽略；其余多出的列照 extra_columns
            ignored = [c for c in extra if self.ignore_keys and match_key(composed[c]) in self.ignore_keys]
            rest = [c for c in extra if c not in ignored]
            if rest and self.block.extra_columns == "ignore":
                ignored, rest = extra, []
            if ignored:
                self.ignored = set(ignored)
                eng.ex.ignored_columns[self.block.id] = [composed[c] for c in ignored]
            if rest:
                shown = [f"「{composed[c]}」（{cell_ref(hb, c)}）" for c in rest]
                eng.problems.add("column_extra", f"{self.where}：表头多出了列{_join(shown)}",
                                 f"{self.where}：表头多出了 {_join([cell_ref(hb, c) for c in rest])} 这几列",
                                 [sh.coord(hb, c) for c in rest], sheet=sh.name,
                                 fix_args={"block": self.block.id, "how": "columns",
                                           "headers": [{"raw": _anchor_text(composed[c]), "cell": sh.coord(hb, c)}
                                                       for c in rest]})
                bad = True
        if bad:
            self._fail()
            return
        self.located = True
        self.state = "data"
        self.last_row = hb
        # 数据行的固定列段：映射列是 value、忽略的列是 ignored_column，按列相邻合段（区域标记用，_row_spans）
        bid = self.block.id
        layout: list[Span] = []
        for c in range(self.c1, self.c2 + 1):
            role = "ignored_column" if c in self.ignored else "value"
            if layout and layout[-1][2] == role:
                layout[-1] = (layout[-1][0], c, role, bid)
            else:
                layout.append((c, c, role, bid))
        self.layout_data = layout
        self.layout_hidden: list[Span] = [(self.c1, self.c2, "hidden_excluded", bid)]
        self.sink.sources.append(f"{sh.sr.id}/{self.block.id}")
        self.merges = _MergeSweep(sh.merges, self.c1, self.c2)
        sh.order.append((t, self.block.id))
        buffered, self.buffer = self.buffer, []
        for r, cells in buffered:
            if r < t:
                self.emit(r, cells, {}, False)
            elif r <= hb:
                claims = {c: ("ignored_column" if c in self.ignored else "col_header")
                          for c, _ in cells if self.c1 <= c <= self.c2}
                self.emit(r, cells, claims, False)
            else:
                self._data_row(r, cells)

    def _fail(self) -> None:
        self.state = "failed"
        self.sh.failed = True
        buffered, self.buffer = self.buffer, []
        for r, cells in buffered:
            self.emit(r, cells, {}, False)

    def _check_total_found(self) -> None:
        """配方里有合计行、本期列表里却没碰到：明确报出来（结构类）。不报的话这一期照常导入，核对里没有 T、
        说明照旧写「原表的合计行未导入本表」，另存的合计表是空的、说明生成又找不到核对结果——都是误导。
        部分干跑（只读了前几百行）时合计行可能还在后面，不报。"""
        total = self.block.rows.total_row
        if total is None or self.state != "data" or (self.stop and self.stop[0] == "total"):
            return
        if self.eng.partial and self.sh.truncated:
            return
        c = self.colmap.get(self.total_idx) if self.total_idx is not None else None
        cells = [self.sh.coord(self.last_row, c)] if c is not None and self.last_row else []
        where = f"{self.where}：配方里有合计行（「{total.label_column}」列以「{total.pick}」开头的行）"
        self.eng.problems.add(
            "total_row_missing",
            f"{where}，本期文件中没有找到。请检查文件是否缺了合计行；本期确实没有合计行时，"
            "在配方面板的「合计行」中取消「有合计行」",
            f"{where}，本期没有找到", cells, sheet=self.sh.name)

    # ---------------- 数据行 ----------------

    def _stop_blank(self, row: int, note: tuple[int, GridCell] | None = None) -> _Stopped:
        """列表到 row 之前为止（row 是收尾的那一段空行的第一行）。note：按表下说明收尾时说明那一行（行号, 那一格），
        记进 _Stopped，_phase4 据此写排除的行、判要不要人确认。"""
        self.done = True
        self.stop = ("blank", row)
        st = _Stopped(self.block.id, row, self.c1, self.c2, min(2, self.c2 - self.c1 + 1))
        if note is not None:
            r, cell = note
            st.note_row, st.note_cell = r, self.sh.coord(r, self.c1)
            st.note_text = _text(cell.value).strip()
            st.note_like = canon(st.note_text).startswith(_NOTE_PREFIXES)
        self.sh.stopped.append(st)
        return st

    def _skip_blank(self, first: int, last: int) -> None:
        """skip 模式下按空行跳过 first..last：计数、记排除的行（blank_skipped，整行没有要导入的格，格数为 0），
        并记进 blank_run，随后若判定表已到下边界（_note_after_blank）就撤回。"""
        self.eng.ex.blank_rows_skipped += last - first + 1
        self.sh.exclude("blank_skipped", first, last, 0, block=self.block.id)
        self.blank_run.append((first, last))

    def _unskip_blanks(self) -> int:
        """撤回 blank_run 里记的空行（它们其实是表的下边界，不是数据中间的空行），返回第一行。"""
        first = self.blank_run[0][0]
        got = self.sh.excluded.get(("blank_skipped", self.block.id, None))
        for a, b in self.blank_run:
            self.eng.ex.blank_rows_skipped -= b - a + 1
            if got is not None and [a, b] in got[0]:
                got[0].remove([a, b])
        if got is not None and not got[0]:
            del self.sh.excluded[("blank_skipped", self.block.id, None)]
        self.blank_run = []
        return first

    def _note_after_blank(self, inb: dict[int, GridCell]) -> bool:
        """skip 模式下，空行之后这一行在本块列范围内只有首列一格文字、其余列全空：是表下隔着空行的说明文字
        （「注：……」「制表人：……」），不是数据行（WP-8 修补，P3-SPEC 1.5 留给 WP-8 的那一条）。

        起草器本来就这么认（recipe_suggest._compatible：空行之后要有两格以上、类型对得上才算同一张表），执行器却把
        空行之后的任何一行都当数据读：说明文字静默成了一条首列是「注：……」、其余全为空值的记录，回执里只多一行，
        区域外文字里也看不到它（H2）。这里按同一条规则把它判为表的下边界：这一行和下面的行交给区域外，文字照常进
        回执，数字照常报 outside_number。首列是数、日期，或者正是合计行的标签时不算（那是缺数的数据行或合计行）；
        只有一列的块分不出来，照旧当数据。

        判成下边界不等于静默（WP-8 评审意见 4）：这一行和其下交给区域外的行都记进排除的行（after_stop，锚点是这格
        文字）；文字不像说明（_NOTE_PREFIXES）时另出 rows_after_stop 要人确认，见 _Engine._phase4。"""
        if self.c2 <= self.c1 or len(inb) != 1 or self.c1 not in inb:
            return False
        cell = inb[self.c1]
        if cell.formula is not None or not isinstance(cell.value, str):
            return False
        text = cell.value.strip()
        if not text or looks_numeric_text(text) or month_day_or_date(text) is not None:
            return False
        total = self.block.rows.total_row
        if (total is not None and self.total_idx is not None and self.colmap.get(self.total_idx) == self.c1
                and match_key(text).startswith(match_key(total.pick))):
            return False
        return True

    def _data_row(self, r: int, cells: list[tuple[int, GridCell]]) -> None:
        if self.done:
            self.emit(r, cells, {}, False)
            return
        block, eng = self.block, self.eng
        gap = r - (self.last_row + 1)
        inb = {c: cell for c, cell in cells if self.c1 <= c <= self.c2}
        if inb and self.ignored and all(c in self.ignored for c in inb):
            # 只有忽略的列有字（表下紧跟的「注：以上为初步统计」写在备注列）：这一行没有一格要导入，按空行处理，
            # 格子交给区域外（文字进回执、数字照常报）。当成数据行会凭空写进一条全是空值的记录
            inb = {}
        if (block.rows.blank_rows == "skip" and inb and self.data_rows and (gap > 0 or self.blank_run)
                and self._note_after_blank(inb)):
            # 表下隔着空行的说明文字：列表到上一条数据行为止，和 stop 模式碰到空行一样收尾（之后的行交给区域外）
            self._stop_blank(self._unskip_blanks() if self.blank_run else self.last_row + 1,
                             note=(r, inb[self.c1]))
            self.emit(r, cells, {}, False)
            return
        if gap > 0:
            if block.rows.blank_rows == "stop":
                self._stop_blank(self.last_row + 1)
                self.emit(r, cells, {}, False)
                return
            self._skip_blank(self.last_row + 1, r - 1)
        self.last_row = r
        if not inb:
            if block.rows.blank_rows == "stop":
                self._stop_blank(r)
            else:
                # 这一行的格都交给区域外照常记账，不算被排除
                self._skip_blank(r, r)
            self.emit(r, cells, {}, False)
            return
        self.blank_run = []
        fills = self._merges_at(r, inb)
        if self.sh.hidden_row(r):
            self.hidden_seen = True
            if self.sh.sr.hidden.rows == "exclude":
                self.excluded_rows.add(r)
                self.sh.layout(r, self.layout_hidden)
                self.emit(r, cells, {c: "hidden_excluded" for c in inb}, True)
                return
        if self.total_idx is not None:
            label = inb.get(self.colmap.get(self.total_idx, -1))
            pick = match_key(block.rows.total_row.pick)  # type: ignore[union-attr]
            if label is not None and match_key(_text(label.value)).startswith(pick):
                self._total_row(r, inb, cells, label)
                return
        self._record(r, inb, cells, fills)

    def _plan(self) -> list[tuple[Any, int, bool, str]]:
        """每列的（列定义、所在列号、是不是主键、位置说明）：定下表头之后算一次，逐行复用。"""
        plan = self.__dict__.get("_plan_cache")
        if plan is None:
            plan = [(col, self.colmap[i], col.name in self.grain, f"{self.where}的列「{col.name}」")
                    for i, col in enumerate(self.cols)]
            self.__dict__["_plan_cache"] = plan
            self._grain_c = next((c for col, c, pk, _ in plan if pk), self.c1)
        return plan

    def _merges_at(self, r: int, inb: dict[int, GridCell]) -> dict[int, GridCell | None]:
        """这一行碰到的合并区：不能填充的记问题（合并区里的空格不再另报为空）；fill 时给 TEXT 列返回要填的值
        （列号 → 左上格）。返回值里值为 _MERGED 的列是「已按合并单元格报过、按空值处理」。"""
        if self.merges is None:
            return {}
        fills: dict[int, Any] = {}
        first_data = self.header_top + self.n
        for m in self.merges.at(r):
            if m[0] == r:
                self.topleft[m] = inb.get(m[1])
            ok = self.merge_judged.get(m)
            if ok is None:
                ok = self.block.merged_data == "fill" and m[0] >= first_data and all(
                    self.cols[i].type == "TEXT" for i, c in self.colmap.items() if m[1] <= c <= m[3])
                self.merge_judged[m] = ok
                if not ok:
                    self.merge_bad.append(m)
            if not ok:
                for c in range(max(m[1], self.c1), min(m[3], self.c2) + 1):
                    if c not in inb:
                        fills[c] = _MERGED
                continue
            for c in range(max(m[1], self.c1), min(m[3], self.c2) + 1):
                if (r, c) != (m[0], m[1]) and c not in inb:
                    fills[c] = self.topleft.get(m)
        return fills

    def _record(self, r: int, inb: dict[int, GridCell], cells: list[tuple[int, GridCell]],
                fills: dict[int, GridCell | None]) -> None:
        eng, sh, block = self.eng, self.sh, self.block
        convert, pol, name, table = eng.convert_list, block.values, sh.name, block.table
        values: list[Any] = []
        lineage: list[tuple[str, int, int]] = []
        failed = False
        for col, c, is_pk, where in self._plan():
            cell = inb.get(c)
            if cell is None and c in fills:
                if fills[c] is _MERGED:
                    values.append(None)         # 合并单元格已经报过，这一格不再按空格另报
                    lineage.append((col.name, r, c))
                    failed = True
                    continue
                cell = fills[c]
            v = convert(cell, col, is_pk, pol, name, where, r, c, table)
            if v is _FAIL:
                failed = True
                v = None
            values.append(v)
            lineage.append((col.name, r, c))
        ignored = self.ignored
        claims = {c: ("ignored_column" if c in ignored else "value") for c in inb}
        self.sh.layout(r, self.layout_data)
        self.data_rows += 1
        if not failed:
            rowid = self.sink.add(values, sheet=name, at=(r, self._grain_c), cells=lineage)
            if rowid is not None:
                self.row_rowid[r] = rowid
                if self.rowid_first is None:
                    self.rowid_first = rowid
                self.rowid_last = rowid
        self.emit(r, cells, claims, True)

    def _total_row(self, r: int, inb: dict[int, GridCell], cells: list[tuple[int, GridCell]],
                   label: GridCell) -> None:
        eng, sh, block = self.eng, self.sh, self.block
        total = block.rows.total_row
        assert total is not None
        claims = {c: ("ignored_column" if c in self.ignored else "total_label") for c in inb}
        label_raw = _text(label.value)
        numeric = [self.colmap[i] for i, col in enumerate(self.cols) if col.type in ("INTEGER", "REAL")]
        if numeric and not any(c in inb for c in numeric):
            # 合计行在，数字格却全空：没有一列能核对，另存的合计表也只会多一行空值。明确拒收，不让核对和说明
            # 当成「有合计行」去写（说明会找不到核对结果，或者写出原表并没有的合计）
            coord = sh.coord(r, self.colmap[self.total_idx])  # type: ignore[index]
            where = f"{self.where}：合计行（{coord.split('!')[-1]}）的数字格全部为空"
            eng.problems.add(
                "total_row_blank",
                f"{where}，无法按各列明细核对。请在文件中补上合计后重新上传；原表不再提供合计时，删掉这一行，"
                "并在配方面板的「合计行」中取消「有合计行」",
                f"{where}，无法核对", [coord], sheet=sh.name)
        keep_values: list[Any] = []
        for i, col in enumerate(self.cols):
            if col.type not in ("INTEGER", "REAL"):
                continue
            c = self.colmap[i]
            cell = inb.get(c)
            if cell is None:
                keep_values.append(None)
                continue
            claims[c] = "total_value"
            coord = sh.coord(r, c)
            got = eng.convert_number(cell, col.type, block.values, sh.name, f"{self.where}的合计行", r, c,
                                     data_area=False)
            value = None if got is _FAIL else got
            keep_values.append(got)
            item = DerivedItem(
                kind="column_sum", segment=block.id, sheet=sh.name, cell=coord, label_raw=label_raw,
                label=canon(label_raw), value=value, is_formula=cell.formula is not None, formula=cell.formula,
                base_table=block.table, base_value=col.name, rowid_first=self.rowid_first,
                rowid_last=self.rowid_last, hidden_in_range=self.hidden_seen, keep_table=total.keep_as)
            if item.is_formula:
                eng.resolve_refs(item, cell.formula or "", self._ref_lookup)
            eng.ex.derived.append(item)
        if total.keep_as and total.keep_as in eng.sinks:
            ks = eng.sinks[total.keep_as]
            ks.sources.append(f"{sh.sr.id}/{block.id}")
            lin = [(col.name, r, self.colmap[i]) for i, col in enumerate(self.cols) if col.type in ("INTEGER", "REAL")]
            ks.add([canon(label_raw), *keep_values], sheet=sh.name, at=(r, self.colmap[self.total_idx]),  # type: ignore[index]
                   cells=lin, fails=True)
        elif not total.keep_as:
            # 只核对、不另存的合计行：参与核对，但不进任何表，记进「排除的行」（遗留项 4 的 total_not_kept）
            n = sum(1 for role in claims.values() if role in ("total_label", "total_value"))
            if n:
                sh.exclude("total_not_kept", r, r, n, block=block.id)
        self.done = True
        self.stop = ("total", r)
        self.emit(r, cells, claims, True)

    def _ref_lookup(self, rc: tuple[int, int]) -> str | int:
        r, c = rc
        if self.c1 <= c <= self.c2:
            rowid = self.row_rowid.get(r)
            if rowid is not None:
                return rowid
            if r in self.excluded_rows:
                return "hidden"
        return "outside"


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def _placeholder_sample(key: str) -> str | None:
    """value_not_number 的这一格能不能当占位符提议（P3-SPEC 3.2 ⑦）：canon 之后不超过 PLACEHOLDER_TEXT_LEN 个字、
    不像一个数，而且**不含数字**。规格只要求前两条；多拦「含数字」是因为 fix_args 不许带数据格的数值，「约987654」
    「9876.5万」这种写法不像一个数，却正是本期的数（含数字的占位符本来也没有意义：「—」「无」「/」才是）。"""
    if not key or len(key) > PLACEHOLDER_TEXT_LEN or looks_numeric_text(key) or any(ch.isdigit() for ch in key):
        return None
    return key


class _Engine:
    def __init__(self, recipe: Recipe, raw: bytes, filename: str, scan: WorkbookScan, db_path: str | None,
                 context_inputs: dict[str, PeriodInput] | None, max_rows: int | None) -> None:
        self.recipe = recipe
        self.raw = raw
        self.filename = filename
        self.scan = scan
        self.db_path = db_path
        self.context_inputs = context_inputs or {}
        self.max_rows = max_rows
        self.partial = max_rows is not None
        self.ex = Extraction(ok=False, partial=self.partial, full_calc_on_load=scan.full_calc_on_load)
        self.problems = _Problems()
        self.conn: sqlite3.Connection | None = None
        self.specs: dict[str, TableSpec] = {t.name: t for t in recipe.tables}
        self.cols: dict[str, list[ColumnOut]] = {}
        self.sinks: dict[str, _Sink] = {}
        self.headers: dict[tuple[str, str], str] = {}
        self.canon_log: dict[tuple[str, str, str, str], CanonEntry] = {}
        #: 每个块自己的占位符（canon 后 → 配方里的原文）：别的块声明的占位符不能在这里放过
        self._ph: dict[int, dict[str, str]] = {}
        #: 块的取值规则对象 → 块 id：值的转换只拿到取值规则，declare_placeholder 的 fix_args 要写块 id
        self._pol_block: dict[int, str] = {id(b.values): b.id for s in recipe.sheets for b in s.blocks}
        self.books = xlsx_cells.Workbooks(raw)
        self.sheets: list[_Sheet] = []

    def _placeholders(self, pol: Any) -> dict[str, str]:
        hit = self._ph.get(id(pol))
        if hit is None:
            hit = self._ph[id(pol)] = {canon(p.text): p.text for p in pol.placeholders}
        return hit

    # ---------------- 值的转换 ----------------

    def convert_number(self, cell: GridCell | None, vtype: str, pol: Any, sheet: str, where: str, r: int, c: int, *,
                       data_area: bool, pk: bool = False) -> Any:
        """数值格 → 入库值；有问题记问题、返回 _FAIL。data_area=False 是合计格：空格、无缓存的公式都只是 None，
        公式不受「数据区不许公式」约束（合计本来就该是公式，由核对模块按标签区间重算）。
        坐标只在出问题时才拼（20 万行的列表每格都拼一次字符串，光这一项就要好几秒）。"""
        if cell is None:
            if not data_area:
                return None
            if pk:
                self.problems.issue("value_blank", sheet, where, _coord(sheet, r, c), text=_PK_BLANK, group="pk")
                return _FAIL
            if pol.blank == "null":
                return None
            self.problems.issue("value_blank", sheet, where, _coord(sheet, r, c))
            return _FAIL
        v = cell.value
        if cell.formula is not None:
            if data_area:
                v = self._accept_formula(cell, pol, sheet, where, r, c)
                if v is _FAIL:
                    return _FAIL
            elif v is None:
                return None
        kind = type(v)
        if kind is int:
            return v if vtype == "INTEGER" else float(v)
        if kind is float and (vtype == "REAL" or v.is_integer()):
            return v if vtype == "REAL" else int(v)
        return self._number(v, vtype, pol, sheet, where, r, c, pk)

    def _accept_formula(self, cell: GridCell, pol: Any, sheet: str, where: str, r: int, c: int) -> Any:
        """数据区的公式格：拒收、无缓存、打开时重算都是问题；否则按保存值导入并计数。"""
        p = self.problems
        if pol.formula == "reject":
            p.issue("value_formula", sheet, where, _coord(sheet, r, c), f"{cell_ref(r, c)} 的 {cell.formula[:40]}")  # type: ignore[index]
            return _FAIL
        if cell.value is None:
            p.issue("formula_uncached", sheet, where, _coord(sheet, r, c))
            return _FAIL
        if self.scan.full_calc_on_load:
            p.issue("formula_full_calc", sheet, where, _coord(sheet, r, c))
            return _FAIL
        self.ex.formula_cells_accepted += 1
        return cell.value

    def _number(self, v: Any, vtype: str, pol: Any, sheet: str, where: str, r: int, c: int, pk: bool = False) -> Any:
        """非原生数字的值（文本、bool、日期……）。pk：主键列，占位符和空白文本都会变成空值，一律拒收。"""
        p = self.problems

        def bad(code: str, sample: str | None = None) -> Any:
            block = self._pol_block.get(id(pol)) if code == "value_not_number" else None
            p.issue(code, sheet, where, _coord(sheet, r, c), f"{cell_ref(r, c)}「{_shown(v)}」", block=block,
                    sample=sample)
            return _FAIL

        if isinstance(v, bool):
            return bad("value_not_number")
        if isinstance(v, int):
            return v if vtype == "INTEGER" else float(v)
        if isinstance(v, float):
            if vtype == "REAL":
                return v
            return int(v) if v.is_integer() else bad("value_not_integer")
        if isinstance(v, str):
            t = v.strip()
            key = canon(t)
            ph = self._placeholders(pol)
            if key in ph:
                if pk:
                    p.issue("value_blank", sheet, where, _coord(sheet, r, c), f"{cell_ref(r, c)}「{_shown(v)}」",
                            text=_PK_PLACEHOLDER, group="pk_placeholder")
                    return _FAIL
                name = ph[key]
                self.ex.placeholders[name] = self.ex.placeholders.get(name, 0) + 1
                return None
            if not t:
                if pk:
                    p.issue("value_blank", sheet, where, _coord(sheet, r, c), text=_PK_BLANK, group="pk")
                    return _FAIL
                if pol.blank == "null":
                    return None
                p.issue("value_blank", sheet, where, _coord(sheet, r, c))
                return _FAIL
            if t in tabular.EXCEL_ERRORS:
                return bad("value_error")
            num = thousands_number(t)
            if num is not None:
                if pol.text_number != "parse_thousands":
                    return bad("value_text_number")
                return self._number(num, vtype, pol, sheet, where, r, c, pk)
            return bad("value_not_number", _placeholder_sample(key))
        return bad("value_not_number")

    def convert_list(self, cell: GridCell | None, col: Any, is_pk: bool, pol: Any, sheet: str, where: str,
                     r: int, c: int, table: str) -> Any:
        ctype = col.type
        if cell is not None and cell.formula is not None:
            v = self._accept_formula(cell, pol, sheet, where, r, c)
            if v is _FAIL:
                return _FAIL
            cell = GridCell(v)
        if ctype == "INTEGER" or ctype == "REAL":
            return self.convert_number(cell, ctype, pol, sheet, where, r, c, data_area=True, pk=is_pk)
        v = cell.value if cell is not None else None
        if v is None or (type(v) is str and not v.strip()):
            if is_pk:
                self.problems.issue("value_blank", sheet, where, _coord(sheet, r, c), text=_PK_BLANK, group="pk")
                return _FAIL
            if ctype == "DATE" and pol.blank == "reject":
                self.problems.issue("value_blank", sheet, where, _coord(sheet, r, c))
                return _FAIL
            return None
        if ctype == "DATE":
            if isinstance(v, _dt.datetime):
                if (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0):
                    return v.date().isoformat()
            elif isinstance(v, _dt.date):
                return v.isoformat()
            elif isinstance(v, str):
                d = month_day_or_date(v)
                if d is not None and d.year is not None:
                    try:
                        return _dt.date(d.year, d.month, d.day).isoformat()
                    except ValueError:
                        pass
            self.problems.issue("value_not_date", sheet, where, _coord(sheet, r, c), f"{cell_ref(r, c)}「{_shown(v)}」")
            return _FAIL
        # 文本的写法与简单导入一致（tabular.as_text）；文本格就是去首尾空白，不必再走一遍数字识别
        text = v.strip() if type(v) is str else tabular.as_text(v)
        store = col.store or ("canonical" if is_pk else "raw")
        if store == "raw":
            return text
        stored = canon(text)
        if stored != text:
            key = (table, col.name, text, stored)
            hit = self.canon_log.get(key)
            if hit is not None:
                hit.count += 1
            elif len(self.canon_log) < CANON_MAX:
                self.canon_log[key] = CanonEntry(table, col.name, text, stored, 1, _coord(sheet, r, c))
        return stored

    def resolve_refs(self, item: DerivedItem, formula: str, lookup: Callable[[tuple[int, int]], str | int]) -> None:
        """合计公式的引用经溯源：全在基表 → ref_rowids；有隐藏行或分类汇总函数 → hidden；
        有不在基表的 → outside；形状认不出 → unrecognized。执行器不做加法（H1）。"""
        shape = formula_refs(formula)
        if shape is None:
            item.ref_problem = "unrecognized"
            return
        func, cells = shape
        item.func = func
        rowids: list[int] = []
        hidden = func in ("SUBTOTAL", "AGGREGATE")
        outside = not cells
        for rc in cells:
            hit = lookup(rc)
            if isinstance(hit, int):
                rowids.append(hit)
            elif hit == "hidden":
                hidden = True
                outside = True
            else:
                outside = True
        item.ref_rowids = sorted(rowids) if not outside else None
        item.ref_problem = "hidden" if hidden else ("outside" if outside else None)

    # ---------------- 格子账 ----------------

    def finish_row(self, sh: _Sheet, r: int, cells: list[tuple[int, GridCell]], claims: dict[int, str],
                   data_row: bool, blocks: dict[int, str] | str | None = None) -> None:
        """格子账的一行。blocks：已认领格所在的块（网格路径按格给，流式路径整行是同一个列表块）。"""
        sh.read_count += len(cells)
        if sh.recon is not None:
            sh.recon.feed(r, len(cells))
        layout = sh.layouts.pop(r, None) if sh.layouts else None
        unclaimed: list[int] = []
        hidden_excluded = 0
        for c, cell in cells:
            role = claims.get(c)
            if role is not None:
                sh.roles[role] += 1
                if role in _BLOCK_ROLES:
                    if sh.hidden_row(r):
                        sh.hidden_claimed_rows.add(r)
                    if sh.hidden_col(c):
                        sh.hidden_claimed_cols.add(c)
                    if role == "hidden_excluded":
                        hidden_excluded += 1
                continue
            if (r, c) in sh.suppressed:
                continue
            if data_row:
                unclaimed.append(c)
                continue
            if cell.formula is None and isinstance(cell.value, str):
                sh.deferred.append((r, c, cell.value))   # 统计期定下来之后再分类
                continue
            kind = "number" if cell.formula is not None else classify_outside(cell.value)
            if kind == "number":
                self._outside_number(sh, r, c, cell.value)
            else:
                self._outside_text(sh, r, c, _text(cell.value), kind)
        if hidden_excluded:
            # 排除的隐藏行按工作表记（契约 ExcludedRows.block：hidden_excluded 为 None），各块的合成一项
            sh.exclude("hidden_excluded", r, r, hidden_excluded)
        if claims or layout:
            sh.regions.feed_spans(r, _row_spans(cells, claims, layout, blocks))
        for c in unclaimed:
            self.problems.issue("cell_unclaimed", sh.name, f"工作表「{sh.name}」第 {r} 行", sh.coord(r, c))
        for st in sh.stopped:
            # 按表下说明收尾的：说明那一行由 _phase4 单独记，它和上面那段空行都不在这里看。空行里可能有只写在忽略列
            # 的字，网格路径收尾时 _Stopped 已经在了、流式路径还没有，看了两条路径的结果就不一样
            if st.closed or r <= st.stop_row or (st.note_row is not None and r <= st.note_row):
                continue
            if claims:
                st.closed = True           # 到了别的块的认领区域
                continue
            inside = [c for c, _ in cells if st.c1 <= c <= st.c2]
            if len(inside) >= st.need:
                st.rows.append(r)
                st.n += len(inside)
                st.cells.extend(sh.coord(r, c) for c in inside[:CELLS_MAX - len(st.cells)])
            elif inside and st.note_row is not None:
                st.tail.append(r)
                st.tail_n += len(inside)

    def _outside_number(self, sh: _Sheet, r: int, c: int, value: Any) -> None:
        """区域外的数字格。配了 ignore_outside 的工作表先压着：同一行有没有锚点文字，要等区域外的文字都分类完
        （_phase4）才知道。没配的照期 2 当场记问题，问题的先后顺序不变。"""
        if sh.outside_rules:
            sh.held.append((r, c, value))
            return
        self._report_outside_number(sh, r, c, value)

    def _report_outside_number(self, sh: _Sheet, r: int, c: int, value: Any) -> None:
        self.problems.issue("outside_number", sh.name, f"工作表「{sh.name}」第 {r} 行", sh.coord(r, c),
                            f"{cell_ref(r, c)}「{_shown(value)}」", row=r)

    def _ignore_outside(self, sh: _Sheet) -> None:
        """ignore_outside：区域外的数字格所在行，如果有区域外文字格的 match_key 等于某条锚点，这些数字格去向
        ignored，记进「排除的行」；锚点格本身照常是区域外文字（已记录、照常比对）。一行命中几条锚点时取配方里
        排在前面的那条。没命中的照常报 outside_number。"""
        keys = {r: {match_key(t) for _, t in items} for r, items in sh.outside_texts.items()}
        for r, c, value in sorted(sh.held, key=lambda x: (x[0], x[1])):
            row_keys = keys.get(r)
            hit = next((raw for key, raw in sh.outside_rules if key in row_keys), None) if row_keys else None
            if hit is None:
                self._report_outside_number(sh, r, c, value)
                continue
            sh.roles["ignored"] += 1
            sh.regions.feed_row(r, [(c, "ignored")])
            sh.exclude("ignored_outside", r, r, 1, anchor=hit)
        sh.held = []

    def _fix_args(self, it: _Issue) -> dict[str, Any] | None:
        """合并类问题的修复参数（P3-SPEC 3.2 ⑥ outside、⑦），在所有工作表收尾之后算：区域外文字要等统计期定下来
        才分类，锚点那时才齐。"""
        if it.code == "value_not_number":
            if it.block is None:
                return None
            texts = list(it.texts)[:PLACEHOLDER_TEXTS_MAX]
            return {"block": it.block, "texts": texts, "other": it.count - sum(it.texts[t] for t in texts)}
        if it.code == "outside_number" and it.row is not None:
            sh = next((x for x in self.sheets if x.name == it.sheet), None)
            if sh is None:
                return None
            # 锚点：这一行区域外的文字格，优先取不含数字的（含数字的锚点下一期多半对不上，修复按钮不会采用）
            items = [(c, t) for c, t in sorted(sh.outside_texts.get(it.row, [])) if _anchor_text(t) is not None]
            pick = next(((c, t) for c, t in items if not any(ch.isdigit() for ch in t)), items[0] if items else None)
            return {"sheet": sh.name, "sheet_id": sh.sr.id, "how": "outside",
                    "rows": [{"row": it.row, "anchor": pick[1] if pick else None,
                              "anchor_cell": sh.coord(it.row, pick[0]) if pick else None, "cells": list(it.cells)}]}
        return None

    def _outside_text(self, sh: _Sheet, r: int, c: int, text: str, kind: str, *, period_source: bool = False,
                      role: str = "outside_text") -> None:
        coord = sh.coord(r, c)
        sh.roles[role] += 1
        sh.regions.feed_row(r, [(c, role)])
        self.ex.outside_text.append(OutsideText(sh.name, coord, text, kind, period_source))  # type: ignore[arg-type]
        if kind == "text_digits" and not period_source:
            self.problems.add(
                "outside_digits",
                f"工作表「{sh.name}」{cell_ref(r, c)} 在导入区域之外有含数字的文字「{_shown(text, 40)}」，不会导入，请确认",
                f"工作表「{sh.name}」{cell_ref(r, c)} 在导入区域之外有含数字的文字，不会导入，需要确认",
                [coord], sheet=sh.name)

    # ---------------- 统计期（4.2 第 4 步） ----------------

    def _period(self) -> tuple[Period | None, bool, set[tuple[str, int, int]]]:
        """返回 (统计期, 是否报了 period_missing, 作为统计期来源的格)。"""
        ctx_sheets = [sh for sh in self.sheets if sh.context is not None]
        if not ctx_sheets:
            return None, False, set()
        found: list[tuple[_Sheet, int, int, str, Period]] = []
        months: list[tuple[_Sheet, int, int, str, Any]] = []
        for sh in ctx_sheets:
            for r, c, text in sorted(sh.deferred):
                p = cn_date_range(text)
                if p is not None:
                    found.append((sh, r, c, text, p))
                    continue
                m = cn_year_month(text)
                if m is not None:
                    months.append((sh, r, c, text, m))
        spec0 = ctx_sheets[0].context
        assert spec0 is not None
        sources: set[tuple[str, int, int]] = set()
        if not found:
            entered = self.context_inputs.get("统计期")
            if entered is not None:
                self.ex.period = PeriodOut(entered.start.isoformat(), entered.end.isoformat(), "human",
                                           signed_by=entered.signed_by)
                period = Period(entered.start, entered.end)
                self._c2(period, ctx_sheets)
                return period, False, sources
            hint = ""
            fp = filename_period(self.filename)
            if fp is not None:
                hint = f"（文件名中的日期为 {fp.start.isoformat()} 至 {fp.end.isoformat()}，可作参考）"
            self.problems.add("period_missing", f"未能从表格中解析出统计期，请为本期录入统计期{hint}",
                              "未能从表格中解析出统计期，需要人工录入")
            return None, True, sources
        distinct = {p for *_, p in found}
        chosen = found[0]
        if len(distinct) > 1:
            for item in found:
                prefer = item[0].context.prefer_prefix if item[0].context else None
                if prefer and match_key(item[3]).startswith(match_key(prefer)):
                    chosen = item
                    break
        period = chosen[4]
        out = PeriodOut(period.start.isoformat(), period.end.isoformat(), "cells")
        details: list[str] = []
        failed = 0
        for sh, r, c, text, p in found:
            coord = sh.coord(r, c)
            if p != period:
                failed += 1
            details.append(f"{coord}：{p.start.isoformat()} 至 {p.end.isoformat()}")
        for sh, r, c, text, m in months:
            coord = sh.coord(r, c)
            if not m.contains(period):
                failed += 1
            details.append(f"{coord}：{m.year}年{m.month}月")
        for sh, r, c, text, _ in [*found, *months]:
            coord = sh.coord(r, c)
            out.cells.append(coord)
            out.texts[coord] = text
            sources.add((sh.name, r, c))
            prefer = sh.context.prefer_prefix if sh.context else None
            sh.roles["context"] += 1
            sh.regions.feed_row(r, [(c, "context")])
            if not is_pure_period_cell(text, prefer):
                out.annotated[coord] = period_residue(text) or ""
                self.ex.outside_text.append(OutsideText(sh.name, coord, text, "text_digits", True))
        self.ex.period = out
        cells = list(out.cells)
        if failed:
            self.ex.context_checks.append(CheckResult(
                id="C1", kind="context_agree", title="统计期多处一致", status="mismatch", category="data_quality",
                checked=len(cells), failed=failed,
                details=[f"统计期取自 {chosen[0].coord(chosen[1], chosen[2])}"] + details, cells=cells[:CELLS_MAX],
                acceptable=True))
        else:
            self.ex.context_checks.append(CheckResult(
                id="C1", kind="context_agree", title="统计期多处一致", status="passed", category="data_quality",
                checked=len(cells), details=details, cells=cells[:CELLS_MAX]))
        self._c2(period, ctx_sheets)
        return period, False, sources

    def _c2(self, period: Period, ctx_sheets: list[_Sheet]) -> None:
        if not any(sh.context is not None and sh.context.cross_check == "filename" for sh in ctx_sheets):
            return
        fp = filename_period(self.filename)
        title = "文件名中的日期与统计期一致"
        if fp is None:
            self.ex.context_checks.append(CheckResult(
                id="C2", kind="filename_period", title=title, status="info", category="info",
                details=["文件名中没有日期区间"]))
        elif fp == period:
            self.ex.context_checks.append(CheckResult(
                id="C2", kind="filename_period", title=title, status="passed", category="data_quality", checked=1))
        else:
            self.ex.context_checks.append(CheckResult(
                id="C2", kind="filename_period", title=title, status="mismatch", category="data_quality", checked=1,
                failed=1, acceptable=True,
                details=[f"文件名中的日期为 {fp.start.isoformat()} 至 {fp.end.isoformat()}，"
                         f"统计期为 {period.start.isoformat()} 至 {period.end.isoformat()}"]))

    # ---------------- 主流程 ----------------

    def run(self) -> Extraction:
        t0 = time.perf_counter()
        cols, shape = derive_tables(self.recipe)
        if shape:
            for rp in shape:
                self.problems.add(rp.code, rp.message, rp.message)
            self.ex.problems = self.problems.finish(set())
            return self.ex
        self.cols = cols
        for name, cs in cols.items():
            self.sinks[name] = _Sink(self, name, cs, self.specs.get(name))
        if self.db_path is not None:
            _remove_db(self.db_path)
            self.conn = _open_sqlite(self.db_path, readonly=False)
            self.conn.isolation_level = None
            self.conn.execute("BEGIN")
            self._create_tables()
        self._match_sheets()
        t1 = time.perf_counter()
        crosstabs: list[_Crosstab] = []
        for sh in self.sheets:
            crosstabs.extend(self._phase1(sh))
        t2 = time.perf_counter()
        period, period_reported, _ = self._period()
        for ct in crosstabs:
            ct.complete_axis(period, period_reported)
            ct.extract()
        for sh in self.sheets:
            self._phase4(sh)
        self.ex.sheets.skipped_hidden = [{"sheet": s.name, "state": s.state} for s in self.scan.sheets
                                         if s.state != "visible"]
        t3 = time.perf_counter()
        self._outputs()
        failed_sheets = {sh.name for sh in self.sheets if sh.failed}
        if failed_sheets:
            # 块级定位失败的工作表：区域都不知道在哪，「区域外文字」只是噪声（统计期来源照留）
            self.ex.outside_text = [o for o in self.ex.outside_text
                                    if o.period_source or o.sheet not in failed_sheets]
        if failed_sheets:
            # 同理：区域外按锚点忽略的格、列表停止之后的文字行，在区域定不下来的工作表上也只是噪声
            self.ex.rows_excluded = [x for x in self.ex.rows_excluded if not (
                x.sheet in failed_sheets and x.reason in ("ignored_outside", "after_stop"))]
        blocking = self.problems.blocking()
        self.ex.problems = self.problems.finish(failed_sheets, self._fix_args)
        t4 = time.perf_counter()
        if self.conn is not None:
            if blocking:
                self.conn.close()
                self.conn = None
                _remove_db(self.db_path)  # type: ignore[arg-type]
            else:
                for sink in self.sinks.values():
                    sink.flush()
                self.conn.execute("COMMIT")
                self.conn.close()
                self.conn = None
                self.ex.table_hashes = self._hashes()
        self.ex.ok = not blocking and (self.db_path is None or bool(self.ex.table_hashes) or not self.cols)
        t5 = time.perf_counter()
        self.ex.timings = {"match": round(t1 - t0, 4), "read_locate": round(t2 - t1, 4),
                           "extract": round(t3 - t2, 4), "receipt": round(t4 - t3, 4), "write": round(t5 - t4, 4),
                           "total": round(t5 - t0, 4)}
        return self.ex

    def _create_tables(self) -> None:
        assert self.conn is not None
        for name, cs in self.cols.items():
            sink = self.sinks[name]
            grain = sink.grain
            defs = []
            alias = len(grain) == 1 and next(c.type for c in cs if c.name == grain[0]) == "INTEGER"
            for c in cs:
                d = f"{_q(c.name)} {c.type}"
                if alias and c.name == grain[0]:
                    # 单列 INTEGER 主键写成表约束会变成 rowid 的别名（rowid 就不再是插入序号，溯源和列表合计的
                    # rowid 区间都会错）；列约束的 PRIMARY KEY DESC 不是别名（SQLite 文档写明的历史行为）
                    d += " PRIMARY KEY DESC NOT NULL"
                elif c.name in grain:
                    d += " NOT NULL"
                defs.append(d)
            if grain and not alias:
                defs.append(f"PRIMARY KEY ({', '.join(_q(g) for g in grain)})")
            self.conn.execute(f"CREATE TABLE {_q(name)} ({', '.join(defs)}) STRICT")

    def _hashes(self) -> dict[str, str]:
        out: dict[str, str] = {}
        conn = _open_sqlite(self.db_path, readonly=True)  # type: ignore[arg-type]
        try:
            for name, cs in self.cols.items():
                grain = self.sinks[name].grain
                order = grain or [c.name for c in cs]
                sql = (f"SELECT {', '.join(_q(c.name) for c in cs)} FROM {_q(name)} "
                       f"ORDER BY {', '.join(_q(x) for x in order)}")
                h = hashlib.sha256()
                for row in conn.execute(sql):
                    h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    h.update(b"\n")
                out[name] = h.hexdigest()
        finally:
            conn.close()
        return out

    def _match_sheets(self) -> None:
        visible = [s for s in self.scan.sheets if s.state == "visible" and s.nonempty > 0]
        used: set[str] = set()
        #: 找不到的工作表先记下，等全部认完再报：rename_sheet 的候选是「配方里其他工作表没认走的」可见工作表
        #: （P3-SPEC 6.1），认完之前算不出来。这一段只报 sheet_missing，挪到循环之后问题的先后顺序不变
        missing: list[tuple[SheetRecipe, str]] = []
        for si, sr in enumerate(self.recipe.sheets):
            key = match_key(sr.match.name)
            hit = next((s for s in self.scan.sheets if match_key(s.name) == key), None)
            renamed = False
            if hit is not None and hit.state != "visible":
                missing.append((sr, f"工作表「{hit.name}」是隐藏的工作表，不读取（配方中的工作表「{sr.match.name}」）"))
                continue
            if hit is None and sr.match.fallback == "only_visible_sheet" and len(visible) == 1:
                hit, renamed = visible[0], True
            if hit is None or hit.name in used:
                names = "、".join(f"「{s.name}」" for s in visible[:8]) or "无"
                missing.append((sr, f"没有找到工作表「{sr.match.name}」（工作簿中有内容的可见工作表：{names}）"))
                continue
            used.add(hit.name)
            self.ex.sheets.matched[sr.id] = hit.name
            if renamed:
                self.ex.sheets.renamed[sr.match.name] = hit.name
            self.sheets.append(_Sheet(self, si, sr, hit))
        free = [s.name for s in visible if s.name not in used]
        for sr, msg in missing:
            self.problems.add("sheet_missing", msg, msg,
                              fix_args={"sheet_id": sr.id, "name": sr.match.name, "candidates": list(free)})
        for s in visible:
            if s.name in used:
                continue
            self.ex.sheets.other_visible.append(s.name)
            if self.recipe.other_visible_sheets == "reject":
                msg = (f"工作簿中另有可见工作表「{s.name}」，配方没有列出它，按配方设置不能导入。"
                       "请删除该工作表，或在配方面板的「配方没有列出的其他工作表」中选择「记入回执，需确认」")
                self.problems.add("sheet_unexpected", msg, msg)

    def _phase1(self, sh: _Sheet) -> list[_Crosstab]:
        """读取、定位各块、列表直接抽完、格子账的第一遍（区域外的文字先存着，等统计期）。"""
        s = sh.scan
        p = self.problems
        if s.merged_total > len(s.merged) or s.merged_total > MAX_MERGED:
            msg = (f"工作表「{sh.name}」的合并单元格超过 {MAX_MERGED:,} 个，无法逐一核对。"
                   "请在 Excel 中取消不需要的合并后重新上传")
            p.add("merged_too_many", msg, msg, sheet=sh.name)
            sh.failed = True
            return []
        b = s.bounds
        if b is not None and not self.partial and b.area > tabular.SPARSE_FACTOR * max(s.nonempty, tabular.SPARSE_FLOOR):
            far = "、".join(list(dict.fromkeys(s.far_cells))[:3])
            msg = (f"工作表「{sh.name}」的已用区域 {b.a1()} 共 {b.area:,} 个单元格，但只有 {s.nonempty:,} 个单元格有内容；"
                   f"远处的单元格（如 {far}）可能是误输入或残留格式。请在 Excel 中删除这些单元格后重新上传")
            p.add("sparse", msg, msg, [f"{sh.name}!{x}" for x in s.far_cells[:CELLS_MAX]], sheet=sh.name)
            sh.failed = True
            return []
        lists = [blk for blk in sh.sr.blocks if isinstance(blk, ListBlock)]
        tabs = [blk for blk in sh.sr.blocks if isinstance(blk, CrosstabBlock)]
        big = s.nonempty > xlsx_cells.GRID_MAX_CELLS
        if big and (tabs or len(lists) != 1):
            msg = (f"工作表「{sh.name}」有 {s.nonempty:,} 个非空单元格，超过逐格核对的上限"
                   f"（{xlsx_cells.GRID_MAX_CELLS:,} 个）；这么大的工作表只支持一个列表，不支持交叉表或多个表格")
            p.add("sheet_too_large_for_layout", msg, msg, sheet=sh.name)
            sh.failed = True
            return []
        if big and not self.partial and b is not None:
            sh.recon = _Reconciler(s)
            bid = lists[0].id
            run = _ListRun(self, sh, lists[0], 0, lambda r, cells, claims, data: self.finish_row(
                sh, r, cells, claims, data, bid))
            for r, cells in xlsx_cells.sheet_rows(self.books, s, min_row=b.min_row, max_row=b.max_row,
                                                  min_col=b.min_col, max_col=b.max_col):
                run.feed(r, cells)
            run.finish()
            self._recon_problem(sh)
            return []
        grid = xlsx_cells.grid_from(self.books, s, max_rows=self.max_rows)
        sh.grid = grid
        sh.truncated = grid.truncated
        if self.partial and self.max_rows is not None and b is not None:
            sh.limit = min(b.max_row, b.min_row + self.max_rows - 1)
        if not grid.truncated:
            sh.recon = _Reconciler(s)
        cts = [_Crosstab(self, sh, blk, bi) for bi, blk in enumerate(sh.sr.blocks) if isinstance(blk, CrosstabBlock)]
        for ct in cts:
            ct.locate()

        def store(r: int, cells: list[tuple[int, GridCell]], claims: dict[int, str], data: bool,
                  owner: str = "") -> None:
            for c, role in claims.items():
                sh.claim(r, c, role, owner)
            if data:
                sh.data_rows.add(r)

        for bi, blk in enumerate(sh.sr.blocks):
            if not isinstance(blk, ListBlock):
                continue
            run = _ListRun(self, sh, blk, bi, lambda r, cells, claims, data, o=blk.id: store(r, cells, claims, data, o))
            for r in grid.row_numbers():
                run.feed(r, grid.row(r))
            run.finish()
        for ct in cts:
            ct.post_claims()
        for a, bb in sorted(sh.overlaps):
            msg = f"工作表「{sh.name}」：「{a}」和「{bb}」认领了同一批单元格"
            p.add("segment_overlap", msg, msg, sheet=sh.name)
        for r in grid.row_numbers():
            cells = grid.row(r)
            claims: dict[int, str] = {}
            blocks: dict[int, str] = {}
            for c, _ in cells:
                role = sh.claims.get((r, c))
                if role is not None:
                    claims[c] = role
                    blocks[c] = sh.cell_block[(r, c)]
            self.finish_row(sh, r, cells, claims, r in sh.data_rows, blocks)
        self._recon_problem(sh)
        return cts

    def _recon_problem(self, sh: _Sheet) -> None:
        rc = sh.recon
        if rc is None or self.partial:
            return
        rc.finish()
        if not rc.bad and rc.total == sh.scan.nonempty:
            return
        parts = [f"第 {r} 行：扫描到 {a} 个非空格，读取到 {b} 个" for r, a, b in rc.bad[:CELLS_MAX]]
        if len(rc.bad) > CELLS_MAX:
            parts.append(f"另有 {len(rc.bad) - CELLS_MAX} 行不一致")
        parts.append(f"合计：扫描到 {sh.scan.nonempty} 个，读取到 {rc.total} 个")
        msg = (f"工作表「{sh.name}」两遍读取的非空单元格数不一致：{'；'.join(parts)}。"
               "文件可能由非常规程序生成，请在 Excel 中另存为 .xlsx 后重新上传")
        self.problems.add("pass_mismatch", msg, msg, sheet=sh.name)

    def _phase4(self, sh: _Sheet) -> None:
        """区域外文字分类（统计期之后）、停止之后的行、隐藏行列、格子账收尾。"""
        period_cells = set(self.ex.period.cells) if self.ex.period and self.ex.period.source == "cells" else set()
        for r, c, text in sh.deferred:
            if sh.coord(r, c) in period_cells:
                continue
            kind = classify_outside(text)
            if kind == "number":
                self._outside_number(sh, r, c, text)
            else:
                sh.outside_texts.setdefault(r, []).append((c, text))
                self._outside_text(sh, r, c, text, kind)
        if sh.held:
            self._ignore_outside(sh)
        for st in sh.stopped:
            where = f"工作表「{sh.name}」的列表「{st.block}」"
            if st.note_row is not None and not st.note_like:
                # 跳过空行的列表按「表下说明」收尾，可那格文字不像说明：也可能是表尾一条只填了首列的数据行。照样不导入
                # （与起草器一致），但要人确认，首次导入和每期重放都要勾（confirm 类，确认项 rows_after_stop:<块>）
                ref = (st.note_cell or "").split("!")[-1]
                msg = (f"{where}：第 {st.note_row} 行只有首列文字（{ref}「{_shown(st.note_text)}」），"
                       "已当作表下说明，没有导入。如果它是一条数据，请在原表中补上这一行其他列的值后重新上传")
                model = f"{where}：第 {st.note_row} 行只有首列文字（{ref}），已当作表下说明，没有导入"
                self.problems.add("rows_after_stop", msg, model, [st.note_cell or ""], sheet=sh.name)
            if st.rows:
                shown = _join([c.split('!')[-1] for c in st.cells], 5)
                if st.note_row is None:
                    msg = (f"{where}：第 {st.rows[0]} 行起有 {len(st.rows)} 行文字在空行之后，没有导入（{shown}）。"
                           "如果它们也是数据，请在配方面板的「数据中间的空行」中选择「跳过继续」")
                    model = f"{where}：第 {st.rows[0]} 行起有 {len(st.rows)} 行文字在空行之后，没有导入（{shown}）"
                else:
                    # 已经是「跳过继续」：叫人去选它没有用。列表停在表下说明那一行，说明下面的行要导入只能挪走说明
                    msg = (f"{where}：第 {st.rows[0]} 行起有 {len(st.rows)} 行文字在表下说明（第 {st.note_row} 行）"
                           f"之后，没有导入（{shown}）。如果它们也是数据，请把原表第 {st.note_row} 行的说明移到表的"
                           "最下方后重新上传")
                    model = (f"{where}：第 {st.rows[0]} 行起有 {len(st.rows)} 行文字在表下说明（第 {st.note_row} 行）"
                             f"之后，没有导入（{shown}）")
                self.problems.add("rows_after_stop", msg, model, st.cells, sheet=sh.name)
                # 排除的行（after_stop）：格数是这些行在本块列范围内的非空格（它们在格子账里照常算区域外）
                for i, r in enumerate(st.rows):
                    sh.exclude("after_stop", r, r, st.n if i == 0 else 0, block=st.block, anchor=st.note_text)
            if st.note_row is not None:
                # 按表下说明收尾：说明那一行（本块列范围内只有首列一格）和其下交给区域外的行都记进排除的行，锚点是说明
                # 那格的文字，回执里看得出列表停在哪一行、因为什么。像说明的只记在这里和区域外文字里，不出问题
                sh.exclude("after_stop", st.note_row, st.note_row, 1, block=st.block, anchor=st.note_text)
                for i, r in enumerate(st.tail):
                    sh.exclude("after_stop", r, r, st.tail_n if i == 0 else 0, block=st.block, anchor=st.note_text)
        rows, cols = sorted(sh.hidden_claimed_rows), sorted(sh.hidden_claimed_cols)
        pol = sh.sr.hidden
        if rows or cols:
            self.ex.hidden[sh.name] = {"rows": rows, "cols": [col_letter(c) for c in cols],
                                       "policy_rows": pol.rows, "policy_cols": pol.cols}
        if rows and pol.rows == "reject_if_any":
            hint = "（工作表设置了筛选，可能是筛选隐藏的行）" if sh.scan.autofilter else ""
            msg = (f"工作表「{sh.name}」导入区域内有隐藏的行：第 {_rows_text(rows)} 行{hint}。"
                   "请在 Excel 中取消隐藏，或在配方面板的「隐藏行」中选择「照常导入」或「跳过」")
            self.problems.add("hidden_rows", msg, msg, sheet=sh.name,
                              fix_args={"sheet": sh.name, "sheet_id": sh.sr.id, "axis": "rows", "rows": rows,
                                        "autofilter": bool(sh.scan.autofilter)})
        if cols and pol.cols == "reject_if_any":
            letters = [col_letter(c) for c in cols]
            msg = (f"工作表「{sh.name}」导入区域内有隐藏的列：{_join(letters)} 列。"
                   "请在 Excel 中取消隐藏，或在配方面板的「隐藏列」中选择「照常导入」")
            self.problems.add("hidden_cols", msg, msg, sheet=sh.name,
                              fix_args={"sheet": sh.name, "sheet_id": sh.sr.id, "axis": "cols", "cols": letters,
                                        "autofilter": bool(sh.scan.autofilter)})
        roles = dict(sh.roles)
        self.ex.ledger.append(LedgerSheet(sh.name, sh.scan.nonempty, sh.read_count, roles,
                                          max(sh.read_count - sum(roles.values()), 0)))
        self.ex.regions.extend(sh.regions.marks())
        self.ex.block_order[sh.name] = [key for _, key in sorted(sh.order)]
        self.ex.rows_excluded.extend(sh.excluded_rows())

    def _outputs(self) -> None:
        ex = self.ex
        ex.canonicalized = list(self.canon_log.values())
        for name, cs in self.cols.items():
            sink = self.sinks[name]
            spec = self.specs.get(name)
            columns = []
            for c in cs:
                header = self.headers.get((name, c.name), c.header)
                columns.append(ColumnOut(c.name, c.type, header, c.unit, c.role))
            ex.tables.append(TableOut(
                name=name, sheet=sink.sheet or (self.sheets[0].name if self.sheets else ""),
                kind=spec.kind if spec else "data", columns=columns, grain=list(sink.grain), rows=sink.rows,
                sources=list(dict.fromkeys(sink.sources))))
            ex.expected_rows[name] = sink.rows
            if sink.lineage:
                ex.lineage[name] = {col: runs.out() for col, runs in sink.lineage.items()}

    def close(self) -> None:
        self.books.close()
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


def execute(recipe: Recipe, raw: bytes, filename: str, scan: WorkbookScan, db_path: str | None, *,
            context_inputs: dict[str, PeriodInput] | None = None,
            max_rows: int | None = None) -> Extraction:
    """按配方执行一份原件（同步，调用方放进线程池）。

    db_path=None 是干跑：完整定位、认领、转换、溯源，不建库（起草器判完整性、AI 修订回灌用），ok 表示没有
    structure / input / recipe 类问题。有库时一个事务写完全部表（默认回滚日志模式，不开 WAL），关掉连接后算
    table_hashes；有 structure / input / recipe 类问题时不留库文件、ok=False；任何异常都先删掉库文件再抛。
    db_path 上已有的文件会先删掉（调用方给的应当是新路径，试运行库先写 .tmp- 再改名是调用方的事）。

    坐标一律写成「工作表!A1」（Problem.cells、DerivedItem.cell、OutsideText.cell、PeriodOut 的键、
    CanonEntry.first_cell）；只有溯源的起始格和 RegionMark.ref 不带工作表（它们旁边另有工作表字段）。

    max_rows（只许和 db_path=None 一起用）：每张工作表只读已用区域的前 max_rows 行做部分干跑，partial=True，
    跳过依赖整张表的检查（两遍对账、窗口之外的未认领、轴覆盖、稀疏、主键全表去重）。
    """
    if max_rows is not None and db_path is not None:
        raise ValueError("只读前若干行的预检（max_rows）不能写库")
    eng = _Engine(recipe, raw, filename, scan, db_path, context_inputs, max_rows)
    try:
        return eng.run()
    except BaseException:
        eng.close()
        if db_path is not None:
            _remove_db(db_path)
        raise
    finally:
        eng.close()
