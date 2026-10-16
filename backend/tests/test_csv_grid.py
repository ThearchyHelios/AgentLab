"""CSV 走配方（Excel 期 5 第 4 项）：CSV 适配成网格，后面的执行器、核对、溯源全部复用。

格子坐标沿用「工作表!A1」（见 csv_grid 的模块说明）：CSV 没有工作表，名字固定是「CSV」，每月的文件名变了也认得出。
"""
from __future__ import annotations

import pytest

from app.data import csv_grid, recipe_engine, xlsx_cells
from app.data.recipe_types import Recipe
from app.data.xlsx_scan import SheetScan, UnsupportedTable, scan_from_json, scan_to_json

CSV = ("订单号,下单日期,门店,金额,备注\n"
       "A001,2026-08-01,一号店,120,\n"
       "A002,2026-08-02,二号店,80.5,\"带,逗号\"\n"
       "\n"
       "A003,2026-08-03,一号店,1,234,多出的一格\n").encode("utf-8")


def test_text_is_sniffed_as_csv_and_binary_is_not():
    assert csv_grid.looks_like_csv(CSV)
    assert csv_grid.looks_like_csv("a;b\n1;2\n".encode("gb18030"))
    assert csv_grid.looks_like_csv(b"\xff\xfe" + "a\tb\n".encode("utf-16-le"))
    assert not csv_grid.looks_like_csv(b"PK\x03\x04rest-of-zip")
    assert not csv_grid.looks_like_csv(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)   # 旧版 .xls
    assert not csv_grid.looks_like_csv(b"\x89PNG\r\n\x1a\n\x00\x00")
    assert not csv_grid.looks_like_csv(b"")


def test_scan_gives_one_visible_sheet_with_bounds_and_row_counts():
    scan = csv_grid.scan(CSV)
    assert scan.format == "csv" and len(scan.sheets) == 1
    sh = scan.sheets[0]
    assert sh.name == csv_grid.CSV_SHEET and sh.state == "visible" and sh.path == csv_grid.CSV_PATH + ","
    assert (sh.bounds.min_row, sh.bounds.min_col, sh.bounds.max_row, sh.bounds.max_col) == (1, 1, 5, 6)
    # 空字段不算非空格；第 4 行是空行
    assert sh.row_runs == [(1, 1, 5), (2, 2, 4), (3, 3, 5), (5, 5, 6)]
    assert sh.nonempty == 20
    again = scan_from_json(scan_to_json(scan))
    assert again.format == "csv" and again.sheets[0].path == csv_grid.CSV_PATH + ","


def test_cell_values_are_typed_only_when_unambiguous():
    """整数、小数写成数；千分位、前导零、正号、科学计数法、日期都留作文字，交给配方的规则（text_number、类型）判。"""
    v = csv_grid.cell_value
    assert v("120") == 120 and v(" 80.5 ") == 80.5 and v("-3") == -3 and v("0") == 0
    for text in ("1,234", "007", "+5", "1e5", "2026-08-01", "12%", "TRUE", "=SUM(A1:A2)", "1234567890123456"):
        assert v(text) == text, text
    assert v("") is None and v("   ") is None


def test_grid_and_rows_read_the_same_cells():
    scan = csv_grid.scan(CSV)
    grid = xlsx_cells.read_grid(CSV, scan, csv_grid.CSV_SHEET)
    assert grid.get(2, 4).value == 120 and grid.get(3, 4).value == 80.5 and grid.get(3, 5).value == "带,逗号"
    assert grid.get(2, 5) is None and grid.get(1, 1).value == "订单号"
    assert grid.get(5, 4).value == 1 and grid.get(5, 5).value == 234     # 没加引号的千分位被逗号拆开了：照实读
    rows = list(xlsx_cells.iter_rows(CSV, scan, csv_grid.CSV_SHEET, min_row=1, max_row=5, min_col=1, max_col=6))
    assert [r for r, _ in rows] == [1, 2, 3, 5]
    assert sum(len(cells) for _, cells in rows) == len(grid.cells) == scan.sheets[0].nonempty
    small = xlsx_cells.read_grid(CSV, scan, csv_grid.CSV_SHEET, max_rows=2)
    assert small.truncated and set(r for r, _ in small.cells) == {1, 2}


def test_semicolon_and_tab_delimiters_and_gbk():
    scan = csv_grid.scan("名称;数量\n甲;3\n".encode("gb18030"))
    grid = xlsx_cells.read_grid("名称;数量\n甲;3\n".encode("gb18030"), scan, csv_grid.CSV_SHEET)
    assert grid.get(2, 1).value == "甲" and grid.get(2, 2).value == 3
    raw = "a\tb\n1\t2\n".encode("utf-8")
    assert xlsx_cells.read_grid(raw, csv_grid.scan(raw), csv_grid.CSV_SHEET).get(2, 2).value == 2


def test_scan_any_dispatches_and_keeps_the_xlsx_refusal():
    assert csv_grid.scan_any(CSV, "orders.csv").format == "csv"
    assert csv_grid.scan_any(CSV, "ORDERS.CSV").format == "csv"
    with pytest.raises(UnsupportedTable):
        csv_grid.scan_any(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "old.xls")


def test_only_csv_and_tsv_names_are_read_as_csv():
    """复核反例：以前只看内容，不是 zip / OLE、前 64KB 没有 NUL 就当 CSV 读。系统导出的「HTML 表格冒充 .xls」、
    误传的文本文件，都被拆成一张叫「CSV」的工作表去起草。现在和简单导入（tabular）一样按扩展名定：只有 .csv / .tsv
    按 CSV 读；别的照旧交给 xlsx_scan 拒收。"""
    with pytest.raises(UnsupportedTable) as e:
        csv_grid.scan_any(CSV, "orders.txt")
    assert "不是有效的 .xlsx" in str(e.value)
    html = "<html><body><table><tr><td>订单号</td><td>金额</td></tr></table></body></html>".encode("utf-8")
    for raw in (html, b"\xef\xbb\xbf  \r\n" + html, b"\xff\xfe" + "<table><tr><td>a</td></tr></table>".encode("utf-16-le"),
                b'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"/>'):
        with pytest.raises(UnsupportedTable) as e:
            csv_grid.scan_any(raw, "report.xls")
        assert "网页或 XML" in str(e.value) and "另存为 .xlsx 或 .csv" in str(e.value)
    # 扩展名是 .csv、内容却不是文字（图片之类改了扩展名）：按 CSV 读不了，照实说
    with pytest.raises(UnsupportedTable) as e:
        csv_grid.scan_any(b"\x89PNG\r\n\x1a\n\x00\x00", "orders.csv")
    assert "不是文字" in str(e.value)


def test_tsv_is_always_tab_separated_and_the_delimiter_is_kept_for_reading():
    """.tsv 一律按制表符分隔（同 tabular）：每行逗号个数一样的 TSV，嗅探会挑逗号（csv.Sniffer 优先逗号）。
    扫描时定下的分隔符记在工作表的 path 里，之后读格子（重放、试运行）用同一个，不再各嗅各的。"""
    raw = "名称,规格\t金额\n甲,1kg\t10\n乙,2kg\t20\n".encode("utf-8")      # 每行一个逗号、一个制表符
    scan = csv_grid.scan_any(raw, "goods.tsv")
    assert scan.sheets[0].path == csv_grid.CSV_PATH + "\t"
    grid = xlsx_cells.read_grid(raw, scan_from_json(scan_to_json(scan)), csv_grid.CSV_SHEET)
    assert grid.get(2, 1).value == "甲,1kg" and grid.get(2, 2).value == 10
    # 这一版之前的暂存区：path 只有「csv:」，读的时候再嗅
    assert csv_grid.is_csv_sheet(SheetScan(name="CSV", state="visible", path=csv_grid.CSV_PATH))
    assert csv_grid.sheet_delimiter(SheetScan(name="CSV", state="visible", path=csv_grid.CSV_PATH)) is None
    assert not csv_grid.is_csv_sheet(SheetScan(name="明细", state="visible", path="xl/worksheets/sheet1.xml"))
    sniffed = csv_grid.scan_any(raw, "goods.csv")
    assert sniffed.sheets[0].path == csv_grid.CSV_PATH + ","        # 同样的内容叫 .csv：照嗅探的来（逗号）
    grid = xlsx_cells.read_grid(raw, sniffed, csv_grid.CSV_SHEET)
    assert grid.get(2, 1).value == "甲" and grid.get(2, 2).value == "1kg\t10"


def test_too_many_columns_is_refused():
    raw = (",".join(["x"] * (csv_grid.MAX_COLS + 1)) + "\n").encode()
    with pytest.raises(UnsupportedTable) as e:
        csv_grid.scan(raw)
    assert "列" in str(e.value)


LIST_RECIPE = {
    "recipe_format": "agentlab-recipe/2",
    "sheets": [{"id": "s1", "match": {"name": "CSV"},
                "blocks": [{"id": "列表1", "layout": "list", "table": "订单",
                            "columns": [{"header": "订单号", "name": "订单号", "type": "TEXT"},
                                        {"header": "下单日期", "name": "下单日期", "type": "DATE"},
                                        {"header": "门店", "name": "门店", "type": "TEXT"},
                                        {"header": "金额", "name": "金额", "type": "REAL"}],
                            "rows": {"blank_rows": "stop"}}]}],
    "tables": [{"name": "订单", "grain": ["订单号"]}],
}


def test_engine_runs_a_list_recipe_on_csv(tmp_path):
    raw = "订单号,下单日期,门店,金额\nA001,2026-08-01,一号店,120\nA002,2026-08-02,二号店,80.5\n".encode()
    scan = csv_grid.scan(raw)
    db = tmp_path / "t.sqlite"
    ex = recipe_engine.execute(Recipe.model_validate(LIST_RECIPE), raw, "orders_2026-08.csv", scan, str(db))
    assert ex.ok, [p.message for p in ex.problems]
    assert ex.tables[0].rows == 2 and ex.tables[0].sheet == "CSV"
    # 溯源照常：订单号这一列从 CSV!A2 起往下 2 格
    run = ex.lineage["订单"]["订单号"][0]
    assert run[1:] == ["CSV", "A2", 2, "down"]
    import sqlite3
    rows = sqlite3.connect(db).execute('SELECT "订单号", "下单日期", "金额" FROM "订单" ORDER BY rowid').fetchall()
    assert rows == [("A001", "2026-08-01", 120.0), ("A002", "2026-08-02", 80.5)]
