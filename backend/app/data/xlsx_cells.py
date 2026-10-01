"""配方导入的读取层：一张可见工作表读成网格（小表）或逐行流（大列表），外加给界面的网格预览。

**两遍读取，口径一致。** 第一遍是 xlsx_scan（标准库流式扫描：边界、合并区、隐藏行列、逐行非空格数），
这里是第二遍（openpyxl read_only）。两遍各数一次非空格，配方执行器逐行对账：对不上说明两个解析器看到的
不是同一张表（XML 行号乱序、重复的行 openpyxl 会静默跳过），整份拒收，不猜。所以「非空」的口径必须和
xlsx_scan 一字不差：值不是 None、也不是只含空白的文本，或者是公式格（哪怕没有缓存值）；0 和 False 不算空。

**值和公式各读一份。** 值来自 data_only 的工作簿（用户要的是数），公式原文只有 data_only=False 才拿得到。
工作表有公式（SheetScan.formulas > 0）时再开一份 data_only=False 的只读工作簿，两份按行同步迭代：两个生成器
解析的是同一段 XML，行与行一一对应。openpyxl 只读模式会把共享公式展开成各格自己的写法（B5 的
`<f t="shared" si="0"/>` 读成 `=SUM(B1:B4)`），合计公式的引用核对靠的就是这一点。

**边界用扫描算出来的。** `<dimension>` 是写文件的程序自己填的，可能陈旧或者干脆没有：先 reset_dimensions，
再按 SheetScan.bounds 传 min/max 行列（与期 1 的 tabular.load_into 相同）。

隐藏工作表一律不读（H10）：worksheet_for 只认可见的工作表，按部件路径配对，名字、路径、状态对不上就拒收。
"""
from __future__ import annotations

import datetime as _dt
import logging
import zipfile
from collections.abc import Iterator
from typing import Any
from xml.etree import ElementTree as ET

from app.data import tabular
from app.data.recipe_types import Grid, GridCell
from app.data.xlsx_scan import SheetScan, UnsupportedTable, WorkbookScan, _broken, cell_ref, parse_ref

logger = logging.getLogger(__name__)
#: 读格子时 openpyxl、zipfile、XML 解析会抛的异常（数字格的 <v> 写坏了是 ValueError）：一律转成 UnsupportedTable，
#: 固定中文文案。不转的话会经全局 ValueError 处理器把英文原文连同格子内容回成不带 code 的 400（SE-7）
_READ_ERRORS = (ValueError, KeyError, ET.ParseError, zipfile.BadZipFile, EOFError)

__all__ = [
    "GRID_MAX_CELLS", "PREVIEW_MAX_COLS", "PREVIEW_MAX_ROWS", "GridTooLarge", "Workbooks", "display", "grid_from",
    "iter_rows", "parse_range", "preview", "read_grid", "sheet_rows", "visible_sheet",
]

#: 物化网格的非空格上限。更大的工作表走流式（iter_rows），配方执行器在那条路上只支持一个列表块
GRID_MAX_CELLS = 200_000
#: 网格预览最多给多少行、多少列（界面只读展示，再多也看不过来）
PREVIEW_MAX_ROWS, PREVIEW_MAX_COLS = 300, 60
#: 预览里一格文字最多多少字（长备注截断，原文在证据面板）
PREVIEW_TEXT_MAX = 200


class GridTooLarge(UnsupportedTable):
    """非空格超过 GRID_MAX_CELLS、又没有给 max_rows：不物化，调用方改走 iter_rows。"""


# --------------------------------------------------------------------------
# 工作簿
# --------------------------------------------------------------------------


def _open_formula_book(raw: bytes) -> Any:
    return tabular.open_book(raw, data_only=False)


class Workbooks:
    """一次执行里复用的两份只读工作簿：值一份（data_only），公式一份（第一次要读有公式的工作表时才开）。

    打开一次工作簿要解析样式表和共享字符串表，多张工作表、多次读取各开一遍很浪费。用完 close()：
    只读工作簿一直开着压缩包。
    """

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self._values: Any = None
        self._formulas: Any = None

    def values(self) -> Any:
        if self._values is None:
            self._values = tabular.open_book(self.raw)
        return self._values

    def formulas(self) -> Any:
        if self._formulas is None:
            self._formulas = _open_formula_book(self.raw)
        return self._formulas

    def close(self) -> None:
        for book in (self._values, self._formulas):
            if book is not None:
                try:
                    book.close()
                except Exception:  # noqa: BLE001  关闭失败不影响已经读出来的结果
                    pass
        self._values = self._formulas = None

    def __enter__(self) -> Workbooks:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def visible_sheet(scan: WorkbookScan, sheet: str) -> SheetScan:
    """扫描结果里的一张可见工作表；没有这张表、或者它是隐藏的，抛 UnsupportedTable。"""
    found = scan.sheet(sheet)
    if found is None:
        raise UnsupportedTable(f"工作簿中没有名为「{sheet}」的工作表")
    if found.state != "visible":
        raise UnsupportedTable(f"工作表「{sheet}」是隐藏的工作表，不读取")
    return found


# --------------------------------------------------------------------------
# 逐行读取
# --------------------------------------------------------------------------


def _formula_text(value: Any) -> str:
    """openpyxl 给的公式：普通公式是「=…」字符串，数组公式是 ArrayFormula（.text 是原文）。"""
    text = value if isinstance(value, str) else getattr(value, "text", None) or str(value)
    return text if text.startswith("=") else f"={text}"


def sheet_rows(books: Workbooks, sheet: SheetScan, *, min_row: int, max_row: int, min_col: int,
               max_col: int) -> Iterator[tuple[int, list[tuple[int, GridCell]]]]:
    """按行流式读一个矩形：每行给 (行号, [(列号, 格)…])，只给非空格、按列升序；整行为空的不给。

    值和公式两份工作簿按行同步迭代（zip）：openpyxl 只读模式按行号补齐缺的行，两边解析同一段 XML，
    行数和每行的宽度都一样。文件内容写坏了（见 _READ_ERRORS）抛 UnsupportedTable，日志只记异常类型。
    """
    try:
        yield from _sheet_rows(books, sheet, min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col)
    except UnsupportedTable:
        raise
    except _READ_ERRORS as e:
        logger.info("reading sheet failed: %s", type(e).__name__)
        raise UnsupportedTable(_broken()) from e


def _sheet_rows(books: Workbooks, sheet: SheetScan, *, min_row: int, max_row: int, min_col: int,
                max_col: int) -> Iterator[tuple[int, list[tuple[int, GridCell]]]]:
    if min_row > max_row or min_col > max_col:
        return
    ws = tabular.worksheet_for(books.values(), sheet)
    ws.reset_dimensions()          # 不信 <dimension>：边界用扫描算出来的
    values = ws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col, values_only=True)
    formulas: Iterator[Any] | None = None
    if sheet.formulas > 0:
        fws = tabular.worksheet_for(books.formulas(), sheet)
        fws.reset_dimensions()
        formulas = iter(fws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col,
                                      values_only=False))
    row = min_row
    for vrow in values:
        frow = next(formulas, None) if formulas is not None else None
        out: list[tuple[int, GridCell]] = []
        col = min_col
        for i, v in enumerate(vrow):
            f = None
            if frow is not None and i < len(frow):
                fc = frow[i]
                if getattr(fc, "data_type", None) == "f" and fc.value is not None:
                    f = _formula_text(fc.value)
            if f is None and (v is None or (type(v) is str and not v.strip())):
                col += 1
                continue
            out.append((col, GridCell(v, f)))
            col += 1
        if out:
            yield row, out
        row += 1


def iter_rows(raw: bytes, scan: WorkbookScan, sheet: str, *, min_row: int, max_row: int,
              min_col: int, max_col: int) -> Iterator[tuple[int, list[tuple[int, GridCell]]]]:
    """流式读一张可见工作表的一个矩形（大列表用）：每行只给非空格（按列排序），整行为空的行不给。"""
    found = visible_sheet(scan, sheet)
    books = Workbooks(raw)
    try:
        yield from sheet_rows(books, found, min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col)
    finally:
        books.close()


# --------------------------------------------------------------------------
# 网格
# --------------------------------------------------------------------------


def parse_range(ref: str) -> tuple[int, int, int, int] | None:
    """「B2:AG2」→ (2, 2, 2, 33)；单格「B2」→ (2, 2, 2, 2)；写坏了 None。"""
    a, _, b = ref.replace("$", "").partition(":")
    try:
        r1, c1 = parse_ref(a)
        r2, c2 = parse_ref(b or a)
    except ValueError:
        return None
    if r1 <= 0 or c1 <= 0 or r2 <= 0 or c2 <= 0:
        return None
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def _expand(spans: list[tuple[int, int]], lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for a, b in spans:
        a, b = max(a, lo), min(b, hi)
        if a <= b:
            out.update(range(a, b + 1))
    return out


def grid_from(books: Workbooks, sheet: SheetScan, *, max_rows: int | None = None) -> Grid:
    """read_grid 的内部版本：用调用方开好的工作簿（配方执行器一次执行里读多张表时复用）。"""
    b = sheet.bounds
    if b is None:
        return Grid(sheet=sheet.name, bounds=None)
    if max_rows is None and sheet.nonempty > GRID_MAX_CELLS:
        raise GridTooLarge(
            f"工作表「{sheet.name}」有 {sheet.nonempty:,} 个非空单元格，超过网格读取的上限（{GRID_MAX_CELLS:,} 个）")
    last = b.max_row if max_rows is None else min(b.max_row, b.min_row + max(max_rows, 1) - 1)
    truncated = last < b.max_row
    cells: dict[tuple[int, int], GridCell] = {}
    for r, row in sheet_rows(books, sheet, min_row=b.min_row, max_row=last, min_col=b.min_col, max_col=b.max_col):
        if max_rows is not None and len(cells) + len(row) > GRID_MAX_CELLS:
            # 前 max_rows 行就已经超过上限（极宽的表）：读到上一行为止
            truncated, last = True, r - 1
            break
        for c, cell in row:
            cells[(r, c)] = cell
    merges = []
    for ref in sheet.merged:
        span = parse_range(ref)
        if span is not None and span[0] <= last and span[2] >= b.min_row:
            merges.append(span)
    return Grid(
        sheet=sheet.name, bounds=(b.min_row, b.min_col, b.max_row, b.max_col), cells=cells, merges=merges,
        hidden_rows=_expand(sheet.hidden_rows, b.min_row, last),
        hidden_cols=_expand(sheet.hidden_cols, b.min_col, b.max_col),
        truncated=truncated,
    )


def read_grid(raw: bytes, scan: WorkbookScan, sheet: str, *, max_rows: int | None = None) -> Grid:
    """物化一张可见工作表。

    隐藏工作表抛 UnsupportedTable。非空格超过 GRID_MAX_CELLS 且没给 max_rows 抛 GridTooLarge；给了 max_rows
    就只读已用区域的前 max_rows 行（起草、预览用），读不完时 truncated=True。Grid.bounds 始终是整张表的真实边界
    （界面要知道表有多大），读到哪一行看 truncated 和 cells。隐藏行列、合并区只保留落在读取范围内的。
    """
    found = visible_sheet(scan, sheet)
    with Workbooks(raw) as books:
        return grid_from(books, found, max_rows=max_rows)


# --------------------------------------------------------------------------
# 预览
# --------------------------------------------------------------------------


def display(cell: GridCell) -> tuple[str, str]:
    """一格在预览里的 (写法, 种类)。种类：text / number / date / bool / error / formula / formula_uncached。"""
    v = cell.value
    if cell.formula is not None:
        if v is None:
            return "", "formula_uncached"
        return _clip(tabular.as_text(v)), "formula"
    if isinstance(v, bool):
        return ("TRUE" if v else "FALSE"), "bool"
    if isinstance(v, (int, float)):
        return tabular.as_text(v), "number"
    if isinstance(v, str):
        text = v.strip()
        return _clip(text), ("error" if text in tabular.EXCEL_ERRORS else "text")
    text = tabular.as_text(v)
    return _clip(text), ("date" if isinstance(v, (_dt.datetime, _dt.date, _dt.time)) else "text")


def _clip(text: str) -> str:
    return text if len(text) <= PREVIEW_TEXT_MAX else text[:PREVIEW_TEXT_MAX] + "…"


def _range_a1(span: tuple[int, int, int, int]) -> str:
    r1, c1, r2, c2 = span
    return cell_ref(r1, c1) if (r1, c1) == (r2, c2) else f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}"


def preview(grid: Grid, *, max_rows: int = PREVIEW_MAX_ROWS, max_cols: int = PREVIEW_MAX_COLS) -> dict[str, Any]:
    """给界面的网格预览（P2-SPEC 7.4 的 GridPreview）：左上角起最多 max_rows 行、max_cols 列。

    cells 是 [行, 列, 写法, 种类]；formulas 是 {坐标: 公式原文}（悬停时显示）。这是给用户看的，数字照给；
    发给模型的压缩表示另有一套（recipe_ai.compress），不从这里取。
    """
    if grid.bounds is None:
        return {"sheet": grid.sheet, "bounds": None, "total_rows": 0, "total_cols": 0, "truncated": grid.truncated,
                "rows": [], "cols": [], "cells": [], "formulas": {}, "merges": [], "hidden_rows": [],
                "hidden_cols": []}
    r1, c1, r2, c2 = grid.bounds
    total_rows, total_cols = r2 - r1 + 1, c2 - c1 + 1
    last_r = min(r2, r1 + max(max_rows, 1) - 1)
    last_c = min(c2, c1 + max(max_cols, 1) - 1)
    cells: list[list[Any]] = []
    formulas: dict[str, str] = {}
    for r in grid.row_numbers():
        if r > last_r:
            break
        for c, cell in grid.row(r):
            if c > last_c:
                break
            text, kind = display(cell)
            cells.append([r, c, text, kind])
            if cell.formula is not None:
                formulas[cell_ref(r, c)] = cell.formula
    merges = [_range_a1(m) for m in grid.merges
              if m[0] <= last_r and m[2] >= r1 and m[1] <= last_c and m[3] >= c1]
    return {
        "sheet": grid.sheet,
        "bounds": f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}",
        "total_rows": total_rows,
        "total_cols": total_cols,
        "truncated": bool(grid.truncated or last_r < r2 or last_c < c2),
        "rows": list(range(r1, last_r + 1)),
        "cols": list(range(c1, last_c + 1)),
        "cells": cells,
        "formulas": formulas,
        "merges": merges,
        "hidden_rows": sorted(r for r in grid.hidden_rows if r1 <= r <= last_r),
        "hidden_cols": sorted(c for c in grid.hidden_cols if c1 <= c <= last_c),
    }
