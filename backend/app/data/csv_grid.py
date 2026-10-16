"""CSV 走配方（Excel 期 5 第 4 项）：把 CSV 适配成配方读取层认的样子——一张可见工作表的扫描结果（WorkbookScan）
和网格（Grid / 逐行流），之后的执行器、核对、说明、溯源、按期累积全部复用，不另写一套 CSV 的解析（否则会和配方
语言分叉，违背「一种说法」）。

**怎么认出是 CSV。** 看内容不看扩展名（配方的几个入口只拿得到原件字节）：不是 zip（xlsx）、不是旧版 .xls 的 OLE
文件头、前 64 KB 里没有 NUL 字节（图片、PDF 这类二进制文件都有），就按 CSV 读。编码识别、分隔符嗅探复用 tabular
读 CSV 的那一段（UTF-8 / GB18030 / 带 BOM 的 UTF-16，逗号、分号、制表符、竖线）。

**坐标沿用「工作表!A1」。** 细案原写「格子坐标记成 R{行}C{列}」，这里没这么做（见交付说明的偏离项）：坐标在执行器、
核对、修复按钮、框选、溯源、裁判摘录、前端网格里一共有十来处解析和拼写，引入第二种写法就要处处认两种，漏一处就是
着色错位、下钻找不到格子；期 1 读 CSV 时（tabular._shape_reasons）用的也是 A1。CSV 没有工作表，名字固定为
CSV_SHEET：每月文件名不同（orders_2026-08.csv、orders_2026-09.csv），配方按工作表名认，名字必须稳定。行号是第几条
记录（引号里的换行不另起一行），列号是第几个字段，和用 Excel 打开这个 CSV 看到的 A1 坐标一致。

**格子的值。** CSV 里只有文字。只有写法毫无歧义的才转成数（-12、80.5，不超过 15 位有效数字），其余一律原样留作文字：
「1,234」（千分位，交给配方的 text_number 决定）、「007」（前导零是编码的一部分）、「+5」「1e5」「12%」、日期写法
（执行器按 DATE 列解析文字日期，起草按文字认日期）、「=SUM(…)」（CSV 里不是公式，Excel 打开时才会当公式算）。
空字段、只有空白的字段是空格（和 xlsx_scan 的口径一致）。没有公式、合并单元格、隐藏行列。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Iterator

from app.data.recipe_types import GridCell
from app.data.xlsx_scan import Bounds, SheetScan, UnsupportedTable, WorkbookScan

__all__ = ["CSV_PATH", "CSV_SHEET", "MAX_COLS", "MAX_ROWS", "CsvTable", "cell_value", "looks_like_csv", "parse",
           "rows", "scan", "scan_any"]

#: CSV 的「工作表名」：配方按它认（SheetMatch.name），每月的文件名不同也认得出
CSV_SHEET = "CSV"
#: SheetScan.path 的取值：读取层据此知道这张「工作表」要从 CSV 里读，不去压缩包里找 XML
CSV_PATH = "csv:"
#: 和 Excel 的上限一致：超过的 CSV 用 Excel 打开也装不下，交给模型的格子坐标也写不出来
MAX_ROWS = 1_048_576
MAX_COLS = 16_384
#: 嗅探看多少字节
_SNIFF = 64 * 1024
#: 写法毫无歧义的整数、小数（不带正号、不带前导零、不用科学计数法）
_INT = re.compile(r"-?(?:0|[1-9][0-9]*)")
_DEC = re.compile(r"-?(?:0|[1-9][0-9]*)\.[0-9]+")
#: 超过这么多位有效数字的不转成数：转成浮点会丢位（19 位卡号撞值），留作文字
_DIGITS_MAX = 15


def looks_like_csv(raw: bytes) -> bool:
    """这份原件按 CSV 读：不是 zip、不是 OLE（旧版 .xls）、开头没有 NUL 字节、不是空文件。带 BOM 的 UTF-16 例外
    （每个字符都带 NUL，Excel「Unicode 文本」另存出来的就是它）。"""
    if not raw or raw[:4] == b"PK\x03\x04" or raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return False
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True
    return b"\x00" not in raw[:_SNIFF]


def cell_value(text: str) -> Any:
    """CSV 的一个字段 → 格子的值：空 / 只有空白是 None（空格），写法毫无歧义的整数、小数转成数，其余原样（文字）。"""
    t = text.strip()
    if not t:
        return None
    digits = len(t.lstrip("-").replace(".", "").lstrip("0")) or 1
    if digits <= _DIGITS_MAX:
        if _INT.fullmatch(t):
            return int(t)
        if _DEC.fullmatch(t):
            return float(t)
    return text


@dataclass
class CsvTable:
    """解析好的 CSV：records[i] 是第 i + 1 条记录的字段（原文）。"""

    records: list[list[str]]
    delimiter: str


def parse(raw: bytes) -> CsvTable:
    """原件 → 记录。编码、分隔符照 tabular 读 CSV 的做法；字段太长、引号不配对读不了时抛 UnsupportedTable。"""
    from app.data import tabular

    text = tabular._decode_csv(raw)
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","       # 嗅不出来就按最常见的来
    records: list[list[str]] = []
    try:
        for record in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
            if len(records) >= MAX_ROWS:
                raise UnsupportedTable(f"CSV 文件超过 {MAX_ROWS:,} 行，超出可以导入的上限")
            if len(record) > MAX_COLS:
                raise UnsupportedTable(f"CSV 文件第 {len(records) + 1} 行有 {len(record):,} 列，超过 {MAX_COLS:,} 列的上限")
            records.append(record)
    except csv.Error as e:
        raise UnsupportedTable(f"无法按 CSV 读取这个文件：第 {len(records) + 1} 行的写法不正确（{e}）。"
                               "请在 Excel 中重新另存为 CSV 后上传") from e
    return CsvTable(records=records, delimiter=delimiter)


def _cells(table: CsvTable) -> Iterator[tuple[int, list[tuple[int, GridCell]]]]:
    for r, record in enumerate(table.records, 1):
        out = [(c, GridCell(v)) for c, field in enumerate(record, 1) if (v := cell_value(field)) is not None]
        if out:
            yield r, out


def scan(raw: bytes) -> WorkbookScan:
    """CSV 原件 → 扫描结果：一张可见工作表（名字 CSV_SHEET、路径 CSV_PATH），边界、非空格数、逐行非空格数的游程。
    没有合并区、隐藏行列、公式、错误值、离群格（CSV 里每一格都是写出来的，没有残留格式）。"""
    table = parse(raw)
    sheet = SheetScan(name=CSV_SHEET, state="visible", path=CSV_PATH)
    lo_r = lo_c = hi_r = hi_c = 0
    runs: list[tuple[int, int, int]] = []
    for r, cells in _cells(table):
        n = len(cells)
        sheet.nonempty += n
        first, last = cells[0][0], cells[-1][0]
        if not lo_r:
            lo_r, lo_c, hi_c = r, first, last
        lo_c, hi_c, hi_r = min(lo_c, first), max(hi_c, last), r
        if runs and runs[-1][1] == r - 1 and runs[-1][2] == n:
            runs[-1] = (runs[-1][0], r, n)
        else:
            runs.append((r, r, n))
    if lo_r:
        sheet.bounds = Bounds(lo_r, lo_c, hi_r, hi_c)
        sheet.dimension = sheet.bounds.a1()
    sheet.row_runs = runs
    return WorkbookScan(sheets=[sheet], format="csv")


def scan_any(raw: bytes) -> WorkbookScan:
    """配方导入的扫描入口：CSV 走这里的适配，其余照旧交给 xlsx_scan（读不了照旧拒收，文案不变）。"""
    from app.data import xlsx_scan

    if looks_like_csv(raw):
        return scan(raw)
    return xlsx_scan.scan(raw)


def rows(table: CsvTable, *, min_row: int, max_row: int, min_col: int,
         max_col: int) -> Iterator[tuple[int, list[tuple[int, GridCell]]]]:
    """按行流式给一个矩形里的非空格：(行号, [(列号, 格)…])，口径同 xlsx_cells.sheet_rows（整行为空的不给）。"""
    for r in range(max(min_row, 1), min(max_row, len(table.records)) + 1):
        record = table.records[r - 1]
        out = [(c, GridCell(v)) for c in range(max(min_col, 1), min(max_col, len(record)) + 1)
               if (v := cell_value(record[c - 1])) is not None]
        if out:
            yield r, out
