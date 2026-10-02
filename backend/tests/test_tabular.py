"""Excel / CSV → 可以用 SQL 查的表。

这块存在的理由不是"多支持一种格式"。这个项目有一条不可回退的决策：叙述层
无算术权限，所有算术下沉到 SQL 或口径卡（engine/issuance.py）。表格传进知识库
只能被切块检索，数字就成了模型从片段里"读"出来的——复核层拦得住凭空多出的
数字，拦不住"从一堆片段里读错了一格"。

所以这里最要紧的一条不是"能不能导进去"，是**导进去的数字能不能真的被 SQL 算**：
一个整数列如果落成 TEXT，ORDER BY 就变成字典序（'10' < '9'），而没有任何
地方会报错。同样要紧的是**不许静默出错**：前导零、长卡号、千分位、隐藏工作表、
空行、交叉表——每一种取舍要么让用户拍板（NeedsDecision），要么写进回执，要么整份拒收。

夹具都在这里用 openpyxl 现造，标签是假名，数字是手写或随机的假数。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import random
import re
import sqlite3
import time
import zipfile
from collections.abc import Callable
from dataclasses import asdict

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import tabular
from app.data.names import name_problem
from app.data.tabular import NeedsDecision, UnsupportedTable, infer_type, load_into
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def save(book) -> bytes:
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def patch(raw: bytes, edits: dict[str, Callable[[str], str]]) -> bytes:
    """改压缩包里的部件：{路径: 文本 → 文本}。openpyxl 写不出来的形态（缓存值、陈旧 dimension）这么造。"""
    src = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename in edits:
                data = edits[info.filename](data.decode()).encode()
            dst.writestr(info, data)
    return out.getvalue()


def excel_saved(raw: bytes, values: dict[str, object]) -> bytes:
    """模拟「在 Excel 里打开并保存过」：给公式补上缓存值，去掉打开时重算的标志。"""
    def fill(text: str) -> str:
        def one(m: re.Match[str]) -> str:
            ref = m.group(1)
            return f'<c r="{ref}"><f>{m.group(2)}</f><v>{values[ref]}</v></c>' if ref in values else m.group(0)
        return re.sub(r'<c r="([A-Z]+\d+)"><f>([^<]*)</f><v></v></c>', one, text)
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    edits: dict[str, Callable[[str], str]] = {n: fill for n in names if n.startswith("xl/worksheets/sheet")}
    edits["xl/workbook.xml"] = lambda t: t.replace(' fullCalcOnLoad="1"', "")
    return patch(raw, edits)


def query(db, sql):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def col_types(db, table):
    return {r[1]: r[2] for r in query(db, f'PRAGMA table_info("{table}")')}


def tables(db):
    return [r[0] for r in query(db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid")]


# --------------------------------------------------------------------------
# 类型推断：这块的全部意义
# --------------------------------------------------------------------------


def test_infer_type_basics():
    assert infer_type([1, 2, 3]) == "INTEGER"
    assert infer_type(["1", "2", "3"]) == "INTEGER"       # CSV 读出来全是字符串
    assert infer_type([1, 2.5]) == "REAL"
    assert infer_type(["1.5", "2"]) == "REAL"
    assert infer_type([1, "两个"]) == "TEXT"
    assert infer_type([]) == "TEXT"                        # 整列空的，别猜
    assert infer_type([None, "  ", None]) == "TEXT"
    # 空单元格不该把一个数字列拖成 TEXT——那正是 SUM() 静默出错的起点
    assert infer_type([1, None, 3]) == "INTEGER"
    assert infer_type([1, "", 3]) == "INTEGER"
    # 真/假不是数量，不该被当成 INTEGER 去求和
    assert infer_type([True, False]) == "TEXT"
    # 千分位文本是数字；前导零、超长数字、科学计数法写法、带加号的是编码
    assert infer_type(["1,234", "2,000.5"]) == "REAL"
    assert infer_type(["00123", "00456"]) == "TEXT"
    assert infer_type([1, 2, "00123"]) == "TEXT"
    assert infer_type(["6222021234567890123"]) == "TEXT"
    assert infer_type(["1E5", "2E3"]) == "TEXT"
    assert infer_type(["+86", "+1"]) == "TEXT"
    assert infer_type(["１２３", "４５６"]) == "TEXT"         # 全角数字不悄悄转成数字
    # 首组是 0 的不是千分位（「0,250」多半是逗号当小数点），不能放大一千倍
    assert infer_type(["0,123", "0,250"]) == "TEXT"
    # 数字列混入少量占位符：按数字类型存（非数字要么存空值，要么不导入，见 load_into 的 mixed）
    assert infer_type([1, 2, 3, 4, "·"]) == "INTEGER"


def test_numbers_sort_as_numbers_not_as_text(tmp_path):
    """整块设计的落点：ORDER BY 一个数字列要是 9 < 10，不是 '10' < '9'。

    列落成 TEXT 的话这条会反过来，而且不报任何错——只是从此以后每一次
    排序、每一次 MAX()、每一次区间筛选都悄悄错着。
    """
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"数据": [["名称", "销量"], ["甲", 9], ["乙", 10], ["丙", 100]]}), "a.xlsx")

    table = tables(db)[0]
    assert col_types(db, table)["销量"] == "INTEGER"
    assert [r[0] for r in query(db, f'SELECT "名称" FROM "{table}" ORDER BY "销量"')] == \
        ["甲", "乙", "丙"]
    assert query(db, f'SELECT SUM("销量") FROM "{table}"')[0][0] == 119


def test_empty_cells_become_null_not_empty_string(tmp_path):
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s": [["a", "b", "c"], [1, None, "x"], [2, "  ", "y"]]}), "a.xlsx")
    table = tables(db)[0]
    assert query(db, f'SELECT COUNT("b") FROM "{table}"')[0][0] == 0   # COUNT 不数 NULL
    assert query(db, f'SELECT SUM("a") FROM "{table}"')[0][0] == 3


# --------------------------------------------------------------------------
# 表 / 列
# --------------------------------------------------------------------------


def test_multiple_sheets_become_multiple_tables(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({
        "销售明细": [["月份", "金额"], ["1月", 100]],
        "Summary": [["k", "v"], ["总计", 100]],
    }), "a.xlsx")
    assert len(report.tables) == 2
    # 中文 sheet 名原样留着。清洗成 ASCII 的话「明细」「汇总」都会变成
    # sheet、sheet_2——模型和用户都认不出哪张是哪张
    assert {t.name for t in report.tables} == {"销售明细", "Summary"}
    assert query(db, 'SELECT "金额" FROM 销售明细')[0][0] == 100   # 不加引号也查得了


def test_columns_get_sql_names_and_keep_headers(tmp_path):
    """列名换成能直接写进 SQL 和实体标记的名字，原表头另记在回执里。

    以前列名保持原样（「本月销量 (万元)」），结果空格、括号、换行、方括号让 `[[c:表.列]]`
    和 `[[v:]]` 都写不进去，不加引号的 SQL 也断成几截。
    """
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [["本月销量 (万元)", "x"], [12, "a"]]}), "a.xlsx")
    table = report.tables[0]
    assert table.columns[0] == {"name": "本月销量_万元", "type": "INTEGER", "header": "本月销量 (万元)"}
    assert query(db, f'SELECT 本月销量_万元 FROM "{table.name}"')[0][0] == 12


def test_broken_headers_get_usable_names(tmp_path):
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s": [["名称", None, "名称"], ["a", "b", "c"]]}), "a.xlsx")
    table = tables(db)[0]
    cols = list(col_types(db, table))
    assert cols == ["名称", "col_2", "名称_2"], cols


def test_column_name_collisions_do_not_break_the_table(tmp_path):
    """去重要躲开已有的名字，还要不分大小写：SQLite 认为 Name 和 name 是同一列。"""
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({
        "s": [["a", "a", "a_2", "Name", "name"], [1, 2, 3, 4, 5]],
    }), "a.xlsx")
    assert [c["name"] for c in report.tables[0].columns] == ["a", "a_2", "a_2_2", "Name", "name_2"]
    assert [c["header"] for c in report.tables[0].columns] == ["a", "a", "a_2", "Name", "name"]


def test_table_names_that_differ_only_in_case_both_survive(tmp_path):
    """「Q1 Sales」和「q1-sales」以前清洗成 Q1_Sales、q1_sales，后一张 DROP 掉前一张。"""
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({
        "Q1 Sales": [["v"], [1]],
        "q1-sales": [["v"], [2]],
    }), "a.xlsx")
    assert [t.name for t in report.tables] == ["Q1_Sales", "q1_sales_2"]
    assert [t.sheet for t in report.tables] == ["Q1 Sales", "q1-sales"]
    assert query(db, "SELECT v FROM Q1_Sales")[0][0] == 1
    assert query(db, "SELECT v FROM q1_sales_2")[0][0] == 2


def test_odd_names_become_valid_names(tmp_path):
    """全角、带圈数字、看不见的填充字符、换行、方括号、竖线、关键字：都换成能用的名字。"""
    db = tmp_path / "t.db"
    headers = ["金额\n(元)", "单价[元]", "a|b", "order", "日客流ㅤ", "ＡＢＣ", "①期"]
    report = load_into(str(db), xlsx({
        "ＡＢＣ": [headers, [1, 2, 3, 4, 5, 6, 7]],
        "sqlite_表": [["x"], [1]],
    }), "a.xlsx")
    names = [t.name for t in report.tables] + [c["name"] for t in report.tables for c in t.columns]
    assert all(name_problem(n) is None for n in names), names
    assert [t.name for t in report.tables] == ["ABC", "t_sqlite_表"]
    assert [c["name"] for c in report.tables[0].columns][:4] == ["金额_元", "单价_元", "a_b", "order_"]
    assert [c["header"] for c in report.tables[0].columns] == headers


def test_header_row_can_be_moved(tmp_path):
    """不猜表头行。猜错的表现是列名变成一行数据，而且没有任何征兆。"""
    db = tmp_path / "t.db"
    sheets = {"s": [["2026 年销售报表", None], ["月份", "金额"], ["1月", 100]]}

    load_into(str(db), xlsx(sheets), "a.xlsx", header_row=2)
    table = tables(db)[0]
    assert list(col_types(db, table)) == ["月份", "金额"]

    # 取默认第 1 行就会把标题当表头——这正是要让用户当场看见的那种错
    report = load_into(str(db), xlsx(sheets), "a.xlsx")
    assert "c_2026_年销售报表" in col_types(db, table)
    assert report.tables[0].columns[0]["header"] == "2026 年销售报表"


def test_ragged_rows_are_padded(tmp_path):
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s": [["a", "b", "c"], [1], [1, 2, 3, 4]]}), "a.xlsx")
    table = tables(db)[0]
    assert query(db, f'SELECT COUNT(*) FROM "{table}"')[0][0] == 2
    # 比表头长的那一格不能丢：多出一列 col_4
    assert list(col_types(db, table)) == ["a", "b", "c", "col_4"]


# --------------------------------------------------------------------------
# 工作表、边界、空行空列
# --------------------------------------------------------------------------


def test_hidden_sheets_are_not_imported(tmp_path):
    """实测有人把口令放在 veryHidden 的工作表里：隐藏的工作表一律不导入，回执里写明。"""
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "明细"
    book.active.append(["地区", "金额"])
    book.active.append(["分区甲", 100])
    hidden = book.create_sheet("草稿")
    hidden.sheet_state = "hidden"
    hidden.append(["x"])
    hidden.append([1])
    very = book.create_sheet("配置")
    very.sheet_state = "veryHidden"
    very.append(["项", "值"])
    very.append(["口令", "secret-123"])
    db = tmp_path / "t.db"
    report = load_into(str(db), save(book), "a.xlsx")
    assert tables(db) == ["明细"]
    assert report.skipped_sheets == [
        {"sheet": "草稿", "state": "hidden", "reason": "hidden"},
        {"sheet": "配置", "state": "veryHidden", "reason": "hidden"},
    ]


def test_only_hidden_content_is_rejected(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "空白"
    very = book.create_sheet("配置")
    very.sheet_state = "veryHidden"
    very["A1"] = "口令"
    with pytest.raises(UnsupportedTable, match="可见工作表.*隐藏"):
        load_into(str(tmp_path / "t.db"), save(book), "a.xlsx")


def _same_named_sheets() -> bytes:
    """两个同名工作表，前一个深度隐藏（内容是占位的「隐藏内容」），后一个可见。Excel 不会这样存，只能构造。"""
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "明细"
    book.active.append(["项"])
    book.active.append(["隐藏内容"])
    book.active.sheet_state = "veryHidden"
    shown = book.create_sheet("备注")
    shown.append(["地区", "金额"])
    shown.append(["分区甲", 100])
    return patch(save(book), {"xl/workbook.xml": lambda t: t.replace('name="备注"', 'name="明细"')})


def test_same_named_sheets_are_rejected_and_nothing_is_imported(tmp_path):
    """同名工作表：按名字取表会拿到隐藏的那个，内容按可见表的边界写进库，回执却说隐藏表已跳过。整份拒收。"""
    db = tmp_path / "t.db"
    with pytest.raises(UnsupportedTable, match="同名"):
        load_into(str(db), _same_named_sheets(), "a.xlsx")
    assert not db.exists() or tables(db) == []


def _write_only_xlsx(rows: int) -> bytes:
    """openpyxl write_only 写出的工作簿：不带 <dimension>（不少导出工具都这样写）。"""
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("明细")
    ws.append(["地区", "产品", "销量", "金额", "备注"])
    for i in range(rows):
        ws.append([f"区{i % 7}", f"品{i % 50}", i % 997, i * 3, "无"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_open_book_does_not_parse_a_whole_sheet_to_find_a_missing_dimension():
    """openpyxl 只读打开时要找 <dimension>，原实现一直读到 </sheetData>：没有这个元素的文件，每打开一次就把
    整张表解析一遍（20 万行约 3 秒，交互路径每次回答都要开两遍）。open_book 装的等价实现读到 <sheetData>
    开头就停：有 <dimension> 的照样取到，没有的照样留空（读取层本来就按扫描的边界读）。"""
    raw = _write_only_xlsx(20_000)
    part = next(n for n in zipfile.ZipFile(io.BytesIO(raw)).namelist() if n.startswith("xl/worksheets/sheet"))
    size = zipfile.ZipFile(io.BytesIO(raw)).getinfo(part).file_size
    assert size > 1_000_000 and b"<dimension" not in zipfile.ZipFile(io.BytesIO(raw)).read(part)
    book = tabular.open_book(raw)
    try:
        ws = book.worksheets[0]
        assert ws.max_row is None and ws.max_column is None
        # 再探一次尺寸，数一数读了多少字节：只读到 <sheetData> 开头附近
        counted: list[int] = []
        original = ws._get_source

        class Counting:
            def __init__(self, f):
                self.f, self.n = f, 0

            def read(self, n=-1):
                b = self.f.read(n)
                self.n += len(b)
                return b

            def close(self):
                counted.append(self.n)
                self.f.close()

        ws._get_source = lambda: Counting(original())
        ws._get_size()
        del ws._get_source
        assert counted and counted[0] < size // 10, (counted, size)
        assert ws.max_row is None
        # 值照常读得出来
        assert list(ws.iter_rows(min_row=2, max_row=2, min_col=1, max_col=5, values_only=True)) == \
            [("区0", "品0", 0, 0, "无")]
    finally:
        book.close()
    # 有 <dimension> 的（Excel 存盘的文件）照样取到；公式一份（data_only=False）同样
    with_dim = xlsx({"甲": [["a", "b"], [1, 2], [3, 4]]})
    for data_only in (True, False):
        book = tabular.open_book(with_dim, data_only=data_only)
        try:
            ws = book.worksheets[0]
            assert (ws.min_row, ws.min_column, ws.max_row, ws.max_column) == (1, 1, 3, 2)
        finally:
            book.close()


def test_worksheets_are_matched_by_part_path(tmp_path):
    """扫描结果和 openpyxl 的工作表按部件路径配对。钉住 openpyxl 的私有属性 _worksheet_path：
    它改名或换了格式，这里先失败，而不是导入时静默配错。"""
    from openpyxl import load_workbook

    from app.data import xlsx_scan

    raw = xlsx({"甲": [["a"], [1]], "乙": [["b"], [2]]})
    sheets = xlsx_scan.scan(raw).sheets
    book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        assert [ws._worksheet_path for ws in book.worksheets] == [s.path for s in sheets]
        assert [tabular._worksheet_for(book, s).title for s in sheets] == ["甲", "乙"]
        # 路径对不上、名字对不上、openpyxl 那边不是可见的：都拒收，不猜
        for broken in (xlsx_scan.SheetScan(name="甲", state="visible", path="xl/worksheets/none.xml"),
                       xlsx_scan.SheetScan(name="丙", state="visible", path=sheets[0].path)):
            with pytest.raises(UnsupportedTable, match="无法确认工作表"):
                tabular._worksheet_for(book, broken)
        book.worksheets[1].sheet_state = "hidden"
        with pytest.raises(UnsupportedTable, match="无法确认工作表"):
            tabular._worksheet_for(book, sheets[1])
    finally:
        book.close()


def test_stale_dimension_still_reads_everything(tmp_path):
    """`<dimension>` 陈旧（写着 A1:B2）时 read_only 会少读：按扫描算出的边界读。"""
    rows = [["地区", "金额"]] + [[f"分区{i}", i] for i in range(1, 41)]
    raw = patch(xlsx({"s": rows}), {
        "xl/worksheets/sheet1.xml": lambda t: re.sub(r'<dimension ref="[^"]*"/>', '<dimension ref="A1:B2"/>', t),
    })
    db = tmp_path / "t.db"
    report = load_into(str(db), raw, "a.xlsx")
    assert report.tables[0].rows == 40
    assert query(db, "SELECT SUM(金额) FROM s")[0][0] == sum(range(1, 41))


def test_sparse_sheet_is_rejected_with_coordinates(tmp_path):
    """A 列、XFD 列各有内容：区域撑成几百万格，读下去就是几百万个空值。要快速拒收并说出坐标。"""
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    for r in range(1, 201):
        ws.cell(r, 1, r)
        ws.cell(r, 16384, r)
    raw = save(book)
    started = time.perf_counter()
    with pytest.raises(UnsupportedTable) as info:
        load_into(str(tmp_path / "t.db"), raw, "a.xlsx")
    assert time.perf_counter() - started < 2
    assert "XFD1" in str(info.value)
    assert not isinstance(info.value, NeedsDecision)


def test_sparse_message_names_each_far_cell_once_and_groups_digits(tmp_path):
    """右下角一个误输入的格子：坐标只说一次，几百亿格的面积按千分位写，读得出量级。"""
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    for r in range(1, 201):
        ws.cell(r, 1, r)
        ws.cell(r, 2, r)
    ws["XFD1048576"] = "x"
    with pytest.raises(UnsupportedTable) as info:
        load_into(str(tmp_path / "t.db"), save(book), "a.xlsx")
    message = str(info.value)
    assert message.count("XFD1048576") == 2            # 一次在区域 A1:XFD1048576 里，一次是示例
    assert "（如 XFD1048576）" in message
    assert "共 17,179,869,184 个单元格" in message and "只有 401 个单元格有内容" in message


def test_blank_rows_and_empty_edge_columns_are_dropped(tmp_path):
    """表头在 C5：前两列不再变成全 NULL 的 col_1/col_2，空行不再灌进 COUNT(*)。"""
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "月报"
    ws["A1"] = "2026 年月报"
    ws["C5"] = "地区"
    ws["D5"] = "金额"
    ws["C6"], ws["D6"] = "分区甲", 100
    ws["C8"], ws["D8"] = "分区乙", 200       # 第 7 行空着
    ws["C9"], ws["D9"] = "分区丙", 300
    db = tmp_path / "t.db"
    report = load_into(str(db), save(book), "a.xlsx", header_row=5)
    table = report.tables[0]
    assert [c["name"] for c in table.columns] == ["地区", "金额"]
    assert table.columns_trimmed == ["A", "B"]
    assert table.blank_rows_skipped == 1
    assert table.rows == 3
    assert table.region == "C5:D9"
    assert query(db, "SELECT COUNT(*), SUM(金额) FROM 月报")[0] == (3, 600)


def test_header_row_beyond_content_skips_sheet(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({
        "长": [["t"], ["x"], ["k", "v"], ["a", 1]],
        "短": [["k"], ["a"]],
    }), "a.xlsx", header_row=3)
    assert [t.name for t in report.tables] == ["长"]
    assert {"sheet": "短", "state": "visible", "reason": "empty"} in report.skipped_sheets
    assert any("「短」" in w for w in report.warnings)


def test_blank_header_row_is_warned(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [[None, None], ["k", "v"], ["a", 1]]}), "a.xlsx")
    assert any("表头行（第 1 行）没有内容" in w for w in report.warnings)


# --------------------------------------------------------------------------
# 值
# --------------------------------------------------------------------------


def test_thousands_text_becomes_numbers(tmp_path):
    """「1,234」以前存成 TEXT，SUM 只取逗号前的 1：3534 算成了 3。"""
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [["金额", "单价"], ["1,234", "1,234.5"], ["2,000", "10"], [300, "2.5"]]}),
                       "a.xlsx")
    assert col_types(db, "s") == {"金额": "INTEGER", "单价": "REAL"}
    assert query(db, "SELECT SUM(金额), MAX(金额), SUM(单价) FROM s")[0] == (3534, 2000, 1247.0)
    assert {(c["column"], c["kind"], c["count"]) for c in report.conversions} == {
        ("金额", "thousands_separator", 2), ("单价", "thousands_separator", 1),
    }


@pytest.mark.parametrize("text", ["0,123", "00,123", "-0,500", "1,5", "1.234,56", "01,234"])
def test_comma_decimals_are_not_thousands(text):
    """合法的千分位首组是 1 到 3 位、不以 0 开头。其余带逗号的数字写法按文本，并且记下来。"""
    assert tabular._classify(text) == (tabular._TEXT, text, "comma")


@pytest.mark.parametrize(("text", "value"), [("1,234", 1234), ("-12,345", -12345), ("999,000,000", 999000000),
                                             ("1,234.5", 1234.5)])
def test_real_thousands_still_convert(text, value):
    assert tabular._classify(text)[1:] == (value, "thousands")


def test_comma_decimals_in_a_number_column_need_a_decision(tmp_path):
    """数字列里混进「0,250」：以前被当成 250，现在按非数字的值交给用户选。"""
    rows = [["名称", "单价"]] + [[f"品{i}", i * 100] for i in range(1, 10)] + [["品x", "0,250"]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), xlsx({"s": rows}), "a.xlsx")
    assert info.value.details["columns"][0]["values"] == [{"value": "0,250", "count": 1}]


def test_semicolon_csv_keeps_commas_as_text(tmp_path):
    """分号分隔的 CSV 来自逗号当小数点的区域设置：「1,500」可能是 1.5，不按千分位转，回执里写明。"""
    db = tmp_path / "t.db"
    report = load_into(str(db), "名称;单价\n甲;1,500\n乙;0,250\n丙;2,000\n丁;1,234,567\n".encode(), "x.csv")
    assert col_types(db, "x")["单价"] == "TEXT"
    assert query(db, "SELECT 单价 FROM x") == [("1,500",), ("0,250",), ("2,000",), ("1,234,567",)]
    assert report.conversions == []
    assert any("列「单价」有 4 个带逗号的数字写法" in w and "小数点还是千分位" in w for w in report.warnings)


def test_comma_csv_still_converts_thousands(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), 'k,v\na,"1,500"\nb,"2,000"\n'.encode(), "x.csv")
    assert query(db, "SELECT SUM(v) FROM x")[0][0] == 3500
    assert [c["kind"] for c in report.conversions] == ["thousands_separator"]


def test_leading_zeros_and_long_numbers_stay_text(tmp_path):
    """编号「00123」以前变成 123；19 位卡号以前变成同一个浮点数。整列按文本原样存。"""
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [
        ["编号", "卡号", "金额"],
        ["00123", "6222021234567890123", 1],
        ["00456", "6222021234567890124", 2],
        [789, "6222021234567890125", 3],
    ]}), "a.xlsx")
    assert col_types(db, "s") == {"编号": "TEXT", "卡号": "TEXT", "金额": "INTEGER"}
    assert [r[0] for r in query(db, "SELECT 编号 FROM s")] == ["00123", "00456", "789"]
    assert query(db, "SELECT COUNT(DISTINCT 卡号) FROM s")[0][0] == 3
    kinds = {(c["column"], c["kind"]) for c in report.conversions}
    assert kinds == {("编号", "kept_as_text"), ("卡号", "kept_as_text")}


def test_codes_are_not_scientific_notation_or_phone_prefixes(tmp_path):
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s": [["代码", "区号"], ["1E5", "+86"], ["2E3", "+1"]]}), "a.xlsx")
    assert query(db, "SELECT 代码, 区号 FROM s") == [("1E5", "+86"), ("2E3", "+1")]


def test_dates_are_plain_iso(tmp_path):
    """纯日期存 YYYY-MM-DD：以前带 T00:00:00，`= '2026-01-05'` 查不到，BETWEEN 漏掉月末那天。"""
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s": [
        ["日期", "时刻", "金额"],
        [dt.datetime(2026, 1, 5), dt.datetime(2026, 1, 5, 8, 30), 1],
        [dt.datetime(2026, 1, 31), dt.datetime(2026, 1, 31, 23, 0, 5), 2],
    ]}), "a.xlsx")
    assert query(db, "SELECT 日期, 时刻 FROM s") == [
        ("2026-01-05", "2026-01-05 08:30:00"), ("2026-01-31", "2026-01-31 23:00:05"),
    ]
    assert query(db, "SELECT SUM(金额) FROM s WHERE 日期 BETWEEN '2026-01-01' AND '2026-01-31'")[0][0] == 3


def test_mixed_column_needs_a_decision(tmp_path):
    """数字列混进「N/A」「·」：以前整列静默变成 TEXT、MAX 按字典序取。现在要用户选。"""
    rows = [["地区", "销量"]] + [[f"分区{i}", i * 10] for i in range(1, 10)] + [["分区x", "N/A"], ["分区y", "·"]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), xlsx({"明细": rows}), "a.xlsx")
    e = info.value
    assert isinstance(e, UnsupportedTable)         # 不认识 NeedsDecision 的老调用方当作拒收
    assert e.kind == "mixed"
    assert e.details == {"columns": [{
        "sheet": "明细", "table": "明细", "column": "销量", "header": "销量", "numeric": 9, "nonnumeric": 2,
        "values": [{"value": "N/A", "count": 1}, {"value": "·", "count": 1}],
    }]}
    json.dumps(e.details)
    assert "存为空值" in str(e)
    assert not (tmp_path / "t.db").exists() or tables(tmp_path / "t.db") == []


def test_mixed_column_with_null_stores_numbers(tmp_path):
    rows = [["地区", "销量"]] + [[f"分区{i}", i * 10] for i in range(1, 10)] + [["分区x", "N/A"], ["分区y", "·"]]
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"明细": rows}), "a.xlsx", mixed="null")
    assert col_types(db, "明细")["销量"] == "INTEGER"
    assert query(db, "SELECT MAX(销量), typeof(MAX(销量)), COUNT(销量), COUNT(*) FROM 明细")[0] == \
        (90, "integer", 9, 11)
    assert report.conversions == [{
        "table": "明细", "column": "销量", "kind": "nonnumeric_to_null", "count": 2, "examples": ["N/A", "·"],
    }]


def test_error_values_count_as_placeholders(tmp_path):
    """#DIV/0! 进数字列：按「非数字取值」走混合列规则。"""
    rows = [["k", "v"]] + [[f"r{i}", i] for i in range(1, 10)] + [["rx", "#DIV/0!"]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "a.db"), xlsx({"s": rows}), "a.xlsx")
    assert info.value.details["columns"][0]["values"] == [{"value": "#DIV/0!", "count": 1}]
    db = tmp_path / "b.db"
    load_into(str(db), xlsx({"s": rows}), "a.xlsx", mixed="null")
    assert query(db, "SELECT MAX(v), COUNT(v) FROM s")[0] == (9, 9)


def test_error_values_in_text_columns_are_warned(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [["名称", "备注"], ["甲", "正常"], ["乙", "#REF!"], ["丙", "缺货"]]}),
                       "a.xlsx")
    assert col_types(db, "s")["备注"] == "TEXT"
    assert any("列「备注」中有 1 个错误值" in w and "#REF!" in w for w in report.warnings)


def test_messy_numeric_column_stays_text_with_a_warning(tmp_path):
    """非数字的写法太多（超过五种）就不是「占位符」了：整列按文本，回执里明说不能求和。"""
    rows = [["k", "v"]] + [[f"r{i}", i] for i in range(1, 12)] + [[f"x{i}", w] for i, w in
                                                                 enumerate(["无", "暂缺", "待定", "-", "?", "不详"])]
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": rows}), "a.xlsx")
    assert col_types(db, "s")["v"] == "TEXT"
    assert any("列「v」有 11 个数字" in w and "不能直接对这一列求和" in w for w in report.warnings)


def test_placeholder_heavy_column_is_warned(tmp_path):
    """数字不到八成（「·」占了一大半）够不上混合列，照文本存——但不能静默：回执里写明数字和占位符各几个。"""
    raw = ("日期,夜间客流\n" + "".join(f"8月{i}日,{i * 10 if i % 5 < 2 else '·'}\n" for i in range(1, 11))).encode()
    db = tmp_path / "t.db"
    report = load_into(str(db), raw, "x.csv")
    assert col_types(db, "x")["夜间客流"] == "TEXT"
    assert report.conversions == []
    hits = [w for w in report.warnings if "列「夜间客流」" in w]
    assert len(hits) == 1
    assert "有 4 个数字、6 个非数字的值" in hits[0] and "「·」6 个" in hits[0] and "不能直接对这一列求和" in hits[0]


def test_text_columns_with_a_few_codes_are_warned_too(tmp_path):
    """等级 A/B/C 里混了两个数字：取值是占位符一类（不超过五种），数字照样提一句。"""
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [["名称", "等级"], ["甲", "A"], ["乙", 2], ["丙", "B"], ["丁", 3],
                                            ["戊", "A"]]}), "a.xlsx")
    assert any("列「等级」有 2 个数字、3 个非数字的值" in w for w in report.warnings)


def test_real_text_columns_with_stray_numbers_are_not_warned(tmp_path):
    """备注这种真正的文本列（写法很多），偶尔一个数字不提，免得回执里全是噪音。"""
    notes = ["正常", "缺货", "已补", "待查", "退回", "改期", "加急"]
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [["名称", "备注"]] + [[f"甲{i}", n] for i, n in enumerate(notes)]
                                      + [["乙", 12]]}), "a.xlsx")
    assert not any("列「备注」" in w for w in report.warnings)


def test_serial_numbers_in_a_date_column_are_warned(tmp_path):
    """日期列里混进没设日期格式的序列号（45000）：整列按文本存，回执里点名。"""
    rows = [["日期", "金额"]] + [[dt.datetime(2026, 1, d), d] for d in range(1, 11)] + [[45000, 11]]
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": rows}), "a.xlsx")
    assert col_types(db, "s")["日期"] == "TEXT"
    assert any("列「日期」有 10 个日期、1 个数字" in w and "日期格式" in w for w in report.warnings)


def test_tables_are_strict_and_values_are_typed(tmp_path):
    """STRICT 表，插入前已经按列类型转好：不靠 SQLite 的亲和性去悄悄转换。"""
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s": [
        ["名称", "数量", "单价", "是否", "日期"],
        ["甲", 1, 1.5, True, dt.datetime(2026, 1, 1)],
        ["乙", "2", "2", False, None],
    ]}), "a.xlsx")
    assert query(db, "SELECT sql FROM sqlite_master WHERE name='s'")[0][0].rstrip().endswith("STRICT")
    assert col_types(db, "s") == {"名称": "TEXT", "数量": "INTEGER", "单价": "REAL", "是否": "TEXT", "日期": "TEXT"}
    assert query(db, "SELECT typeof(名称), typeof(数量), typeof(单价), 是否 FROM s") == [
        ("text", "integer", "real", "TRUE"), ("text", "integer", "real", "FALSE"),
    ]


# --------------------------------------------------------------------------
# 样本之外：前 _TYPE_SAMPLE 行看不到的值
# --------------------------------------------------------------------------


def test_values_after_the_sample_rewrite_the_column(tmp_path, monkeypatch):
    """类型先按样本定，写入时统计整列；整列的结论不一样就按整列重写，不留「样本外静默出错」的口子。"""
    monkeypatch.setattr(tabular, "_TYPE_SAMPLE", 3)
    rows = [["编号", "数量", "名称"]] + [[100 + i, i, f"甲{i}"] for i in range(1, 6)] + [["00789", 2.5, "乙"]]
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": rows}), "a.xlsx")
    assert col_types(db, "s") == {"编号": "TEXT", "数量": "REAL", "名称": "TEXT"}
    assert query(db, "SELECT 编号, 数量 FROM s ORDER BY rowid DESC LIMIT 1")[0] == ("00789", 2.5)
    assert query(db, "SELECT COUNT(*), SUM(数量) FROM s")[0] == (6, 17.5)
    assert [c["type"] for c in report.tables[0].columns] == ["TEXT", "REAL", "TEXT"]


def test_placeholder_after_the_sample_still_needs_a_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(tabular, "_TYPE_SAMPLE", 3)
    rows = [["k", "v"]] + [[f"r{i}", i] for i in range(1, 10)] + [["rx", "·"]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), xlsx({"s": rows}), "a.xlsx")
    assert info.value.kind == "mixed"


def test_wider_csv_rows_after_the_sample_keep_their_values(tmp_path, monkeypatch):
    monkeypatch.setattr(tabular, "_TYPE_SAMPLE", 2)
    raw = "a,b\n1,2\n3,4\n5,6\n7,8,9\n".encode()
    db = tmp_path / "t.db"
    report = load_into(str(db), raw, "x.csv")
    assert [c["name"] for c in report.tables[0].columns] == ["a", "b", "col_3"]
    assert query(db, "SELECT SUM(col_3) FROM x")[0][0] == 9


# --------------------------------------------------------------------------
# 公式
# --------------------------------------------------------------------------


def _with_total(cached: bool) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "明细"
    ws.append(["地区", "金额", "税额"])
    for i, amount in enumerate([100, 200, 300], start=2):
        ws.append([f"分区{i}", amount, f"=B{i}*0.1"])
    raw = save(book)
    return excel_saved(raw, {"C2": 10, "C3": 20, "C4": 30}) if cached else raw


def test_uncached_formulas_in_the_table_are_rejected(tmp_path):
    """程序生成、没用 Excel 保存过的文件：公式没有缓存值，读出来全是空。"""
    with pytest.raises(UnsupportedTable) as info:
        load_into(str(tmp_path / "t.db"), _with_total(cached=False), "a.xlsx")
    text = str(info.value)
    assert "C2" in text and "在 Excel 中打开并保存" in text
    assert not isinstance(info.value, NeedsDecision)


def test_cached_formulas_are_read(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), _with_total(cached=True), "a.xlsx")
    assert query(db, "SELECT SUM(税额) FROM 明细")[0][0] == 60
    assert not any("重新计算" in w for w in report.warnings)


def test_uncached_formulas_above_the_header_are_ignored(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws["A1"] = "=TODAY()"                 # 标题区的公式，不在导入区域里
    ws.append([])
    ws["A3"], ws["B3"] = "k", "v"
    ws["A4"], ws["B4"] = "a", 1
    db = tmp_path / "t.db"
    report = load_into(str(db), save(book), "a.xlsx", header_row=3)
    assert report.tables[0].rows == 1


def test_full_calc_on_load_is_warned(tmp_path):
    """XlsxWriter 默认把公式缓存写成 0 并要求打开时重算：缓存值可能只是占位。"""
    raw = patch(_with_total(cached=True), {"xl/workbook.xml": lambda t: t.replace(
        "<calcPr ", '<calcPr fullCalcOnLoad="1" ')})
    db = tmp_path / "t.db"
    report = load_into(str(db), raw, "a.xlsx")
    assert any("打开时重新计算" in w and "占位" in w for w in report.warnings)


# --------------------------------------------------------------------------
# 结构：交叉表、多块
# --------------------------------------------------------------------------


def _date_header_table() -> bytes:
    header = ["指标"] + [f"8月{d}日" for d in range(1, 11)]
    return xlsx({"日报": [header, ["客流"] + list(range(10, 20)), ["收入"] + list(range(20, 30))]})


def test_date_header_needs_a_decision(tmp_path):
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), _date_header_table(), "a.xlsx")
    e = info.value
    assert e.kind == "shape"
    assert [(r["kind"], r["cells"]) for r in e.details["reasons"]] == [("date_header", ["B1:K1"])]
    assert "按原样导入（未规整）" in str(e)
    json.dumps(e.details)


def test_raw_mode_imports_and_marks_unshaped(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), _date_header_table(), "a.xlsx", raw_mode=True)
    assert report.tables[0].unshaped is True
    assert report.tables[0].rows == 2
    assert any("按原样导入、未经规整" in w for w in report.warnings)


def test_section_title_needs_a_decision(tmp_path):
    rows = [["地区", "一月", "二月", "三月"],
            ["分区甲", 1, 2, 3],
            ["东部小计口径", None, None, None],
            ["分区乙", 4, 5, 6]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), xlsx({"s": rows}), "a.xlsx")
    reason = info.value.details["reasons"][0]
    assert reason["kind"] == "section_title"
    assert reason["cells"] == ["A3"]


def test_stacked_tables_need_a_decision(tmp_path):
    """一张工作表里上下叠了两张表：第二张的标题和表头不能被当成第一张的数据行。"""
    rows = [["地区", "销量", "金额"], ["分区甲", 10, 100], ["分区乙", 20, 200], [],
            ["二、费用"], ["项目", "金额"], ["差旅", 5], ["办公", 7]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), xlsx({"s": rows}), "a.xlsx")
    assert [(r["kind"], r["cells"]) for r in info.value.details["reasons"]] == [("section_title", ["A5"])]


def test_formula_totals_need_a_decision(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.append(["地区", "金额"])
    for i, v in enumerate([10, 20, 30], start=2):
        ws.append([f"分区{i}", v])
    ws.append(["合计", "=SUM(B2:B4)"])
    raw = excel_saved(save(book), {"B5": 60})
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), raw, "a.xlsx")
    reason = info.value.details["reasons"][0]
    assert reason["kind"] == "formula_above"
    assert reason["cells"] == ["B5"]
    assert "SUM(B2:B4)" in reason["message"]


def _cached_totals_book() -> bytes:
    """合计行不用区域写法：B5 = B2+B3+B4，C5 = SUM(C2,C3,C4)。"""
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "s"
    ws.append(["地区", "金额", "数量"])
    for row in [["分区甲", 100, 1], ["分区乙", 200, 2], ["分区丙", 300, 3]]:
        ws.append(row)
    ws.append(["合计", "=B2+B3+B4", "=SUM(C2,C3,C4)"])
    return excel_saved(save(book), {"B5": 600, "C5": 6})


def test_cell_by_cell_totals_need_a_decision(tmp_path):
    """手工表格里常见的逐格相加的合计行，以前漏检、照常进库，SUM 翻倍。"""
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), _cached_totals_book(), "a.xlsx")
    reason = info.value.details["reasons"][0]
    assert reason["kind"] == "formula_above"
    assert reason["cells"] == ["B5", "C5"]
    assert "B2+B3+B4" in reason["message"]


def test_running_balance_is_not_a_total(tmp_path):
    """滚动余额（C3 = C2+B3）引用了上一行，但不是合计：照常导入。"""
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "s"
    ws.append(["日期", "发生额", "余额"])
    ws.append(["1日", 100, 100])
    values = {}
    balance = 100
    for r, amount in enumerate([50, -30, 20], start=3):
        balance += amount
        ws.append([f"{r - 1}日", amount, f"=C{r - 1}+B{r}"])
        values[f"C{r}"] = balance
    db = tmp_path / "t.db"
    report = load_into(str(db), excel_saved(save(book), values), "a.xlsx")
    assert report.tables[0].rows == 4
    assert query(db, "SELECT 余额 FROM s ORDER BY rowid DESC LIMIT 1")[0][0] == 140


def _table_with_totals_row() -> bytes:
    """Excel 的表格对象（插入 → 表格）勾了汇总行：B5 = SUBTOTAL(109,销售表[金额])。"""
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table

    book = Workbook()
    ws = book.active
    ws.title = "s"
    ws.append(["地区", "金额"])
    for row in [["分区甲", 100], ["分区乙", 200], ["分区丙", 300]]:
        ws.append(row)
    ws.append(["汇总", "=SUBTOTAL(109,销售表[金额])"])
    table = Table(displayName="销售表", ref="A1:B5")
    table.totalsRowCount = 1
    ws.add_table(table)
    return excel_saved(save(book), {"B5": 600})


def test_table_totals_row_needs_a_decision(tmp_path):
    """以前汇总行当成普通数据行导入，SUM 翻倍、COUNT 多一行，回执里一个字都没有。"""
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), _table_with_totals_row(), "a.xlsx")
    e = info.value
    assert e.kind == "shape"
    assert [(r["kind"], r["cells"]) for r in e.details["reasons"]] == [("table_totals", ["A5:B5"])]
    assert "销售表" in e.details["reasons"][0]["message"]
    assert "表格对象的汇总行" in str(e)


def test_table_totals_row_without_formulas_is_still_detected(tmp_path):
    """汇总行里只有文字、没有公式（或者公式被改成了数值）：表格对象的定义照样说明那是汇总行。"""
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table

    book = Workbook()
    ws = book.active
    ws.title = "s"
    for row in [["地区", "金额"], ["分区甲", 100], ["分区乙", 200], ["汇总", 300]]:
        ws.append(row)
    table = Table(displayName="明细表", ref="A1:B4")
    table.totalsRowCount = 1
    ws.add_table(table)
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), save(book), "a.xlsx")
    assert [(r["kind"], r["cells"]) for r in info.value.details["reasons"]] == [("table_totals", ["A4:B4"])]


def test_table_totals_row_raw_mode(tmp_path):
    db = tmp_path / "t.db"
    report = load_into(str(db), _table_with_totals_row(), "a.xlsx", raw_mode=True)
    assert report.tables[0].unshaped is True
    assert any("表格对象的汇总行" in w for w in report.warnings)


def test_tables_without_totals_rows_import_normally(tmp_path):
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table

    book = Workbook()
    ws = book.active
    ws.title = "s"
    for row in [["地区", "金额"], ["分区甲", 100], ["分区乙", 200]]:
        ws.append(row)
    ws.add_table(Table(displayName="明细表", ref="A1:B3"))
    db = tmp_path / "t.db"
    report = load_into(str(db), save(book), "a.xlsx")
    assert query(db, "SELECT SUM(金额), COUNT(*) FROM s")[0] == (300, 2)
    assert report.warnings == [w for w in report.warnings if "重新计算" in w]


def test_optional_text_columns_are_not_section_titles(tmp_path):
    """全是文字列的表，一行只填了名字很正常，不拒收；但照常导入的这种行要逐行写进回执，不静默放过。"""
    rows = [["姓名", "部门", "备注"], ["张甲", "一部", "x"], ["李乙", None, None], ["王丙", "二部", None]]
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": rows}), "a.xlsx")
    assert report.tables[0].rows == 3 and not report.tables[0].unshaped
    hits = [w for w in report.warnings if "只有一个单元格有文字" in w]
    assert len(hits) == 1
    assert "有 1 行" in hits[0] and "A3「李乙」" in hits[0] and "会计入行数" in hits[0]


def test_two_column_tables_have_section_titles_too(tmp_path):
    """两列的表（地区、金额）里的分段标题同样会灌进 COUNT：不因为列少就放过。"""
    rows = [["地区", "金额"], ["分区甲", 100], ["华东", None], ["分区乙", 200], ["分区丙", 300],
            ["华北", None], ["分区丁", 400]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), xlsx({"s": rows}), "a.xlsx")
    reason = info.value.details["reasons"][0]
    assert reason["kind"] == "section_title" and reason["cells"] == ["A3", "A6"]
    assert "疑似分段标题" in reason["message"]


def test_section_titles_in_mostly_text_tables(tmp_path):
    """文字列占多数（地区、门店、负责人）、只有销量一列是数字：分段标题照样要检出。"""
    rows = [["地区", "门店", "负责人", "销量"], ["分区甲", "店一", "张甲", 10], ["华东区", None, None, None],
            ["分区乙", "店二", "李乙", 20], ["华北区", None, None, None], ["分区丙", "店三", "王丙", 30]]
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), xlsx({"s": rows}), "a.xlsx")
    assert [(r["kind"], r["cells"]) for r in info.value.details["reasons"]] == [("section_title", ["A3", "A5"])]


def test_single_column_lists_are_not_section_titles(tmp_path):
    """只有一列的名单，每一行都「只有一个格子有字」：没有其余列，这条判据不适用。"""
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": [["姓名"], ["张甲"], ["李乙"], ["王丙"]]}), "a.xlsx")
    assert report.tables[0].rows == 3
    assert report.warnings == []


def test_many_date_columns_are_not_a_crosstab(tmp_path):
    """一行一个项目、好几个日期列：日期在值里，不是横排的表头。"""
    header = ["项目"] + [f"节点{i}" for i in range(1, 9)]
    rows = [header] + [[f"项目{p}"] + [dt.datetime(2026, 1, p + i) for i in range(8)] for p in range(1, 4)]
    rows.append(["项目9"] + [dt.datetime(2026, 2, 1)] * 3 + [None] * 5)
    db = tmp_path / "t.db"
    report = load_into(str(db), xlsx({"s": rows}), "a.xlsx")
    assert report.tables[0].rows == 4


def crosstab_book(days: int = 31, seed: int = 7):
    """结构仿照客流报表的合成交叉表：日期横排、三块叠放、分段标题、占位符「·」、公式合计。标签是假名。

    返回 (工作簿, 公式的缓存值)：openpyxl 存不了缓存值，存盘后由 excel_saved 补上。
    """
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    rnd = random.Random(seed)
    book = Workbook()
    ws = book.active
    ws.title = "客流汇总"
    ws["B2"] = "统计时间范围：2026年8月1日至2026年8月31日"
    ws["B3"] = "单位：人次"
    ws["B4"] = "指标"
    for d in range(days):
        ws.cell(4, 3 + d, f"8月{d + 1}日")
    for d in range(days):                              # 第一块：全日 = 甲 + 乙
        a, b = rnd.randint(100, 900), rnd.randint(100, 900)
        ws.cell(5, 3 + d, a + b)
        ws.cell(6, 3 + d, a)
        ws.cell(7, 3 + d, b)
    ws["B5"], ws["B6"], ws["B7"] = "全日客流（人次）", "分区甲（人次）", "分区乙（人次）"
    ws["B9"] = "日间时段客流（人次）"                   # 第二块
    ws.merge_cells(start_row=9, start_column=2, end_row=9, end_column=2 + days)
    for i, h in enumerate(range(7, 18)):
        ws.cell(10 + i, 2, f"{h}-{h + 1}")
        for d in range(days):
            ws.cell(10 + i, 3 + d, "·" if (h == 7 and d % 5 == 0) else rnd.randint(0, 200))
    ws["B21"] = "夜间时段客流（人次）"                  # 第三块
    night: dict[tuple[int, int], int] = {}
    for i, h in enumerate(range(18, 24)):
        ws.cell(22 + i, 2, f"{h}-{h + 1}")
        for d in range(days):
            night[(22 + i, d)] = v = rnd.randint(0, 100)
            ws.cell(22 + i, 3 + d, v)
    cached: dict[str, object] = {}
    for i, (label, r1, r2) in enumerate([("18-22 时合计", 22, 25), ("22-24 时合计", 26, 27), ("18-24 时合计", 22, 27)]):
        ws.cell(28 + i, 2, label)
        for d in range(days):
            col = get_column_letter(3 + d)
            ws.cell(28 + i, 3 + d, f"=SUM({col}{r1}:{col}{r2})")
            cached[f"{col}{28 + i}"] = sum(night[(r, d)] for r in range(r1, r2 + 1))
    return book, cached


def crosstab() -> bytes:
    book, cached = crosstab_book()
    return excel_saved(save(book), cached)


@pytest.mark.parametrize("header_row", [1, 4])
def test_synthetic_crosstab_is_rejected_with_reasons(tmp_path, header_row):
    """表头行填对（第 4 行）或用默认值（第 1 行）都要拒收，并逐条说出原因和坐标。"""
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), crosstab(), "a.xlsx", header_row=header_row)
    e = info.value
    assert e.kind == "shape"
    by_kind = {r["kind"]: r for r in e.details["reasons"]}
    assert by_kind.keys() == ({"date_header", "section_title", "formula_above"} if header_row == 4
                              else {"date_row", "section_title", "formula_above"})
    assert by_kind.get("date_header", by_kind.get("date_row"))["cells"] == ["C4:AG4"]
    assert {"B9", "B21"} <= set(by_kind["section_title"]["cells"])
    assert by_kind["formula_above"]["cells"][0] == "C28"
    assert all(r["sheet"] == "客流汇总" and r["message"] for r in e.details["reasons"])
    assert not (tmp_path / "t.db").exists() or tables(tmp_path / "t.db") == []
    if header_row == 4:
        # 第一次拒收就列全四组：合计（formula_above）、分段标题、日期列里的占位符「·」、日期没有写年份。
        # 「·」以前要等选了「按原样导入」、第二次退回时才看得到
        assert by_kind["date_header"]["no_year"] is True and "没有写年份" in by_kind["date_header"]["message"]
        assert e.details["mixed_complete"] is True
        preview = e.details["mixed"]
        assert len(preview) == 7 and {v["value"] for c in preview for v in c["values"]} == {"·"}
        assert "另有 7 列以数字为主、混有非数字的值（如 「·」）" in str(e)
        # 预告和第二轮真正要选的是同一批列
        with pytest.raises(NeedsDecision) as second:
            load_into(str(tmp_path / "t.db"), crosstab(), "a.xlsx", header_row=4, raw_mode=True)
        assert second.value.kind == "mixed" and second.value.details["columns"] == preview


def test_shape_decision_without_mixed_columns_has_no_preview(tmp_path):
    """没有混合列的多块表：不带预告，提示里也不提。"""
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    for row in [["项目", "金额"], ["分区甲", 10], ["分区乙", 20], ["二、费用", None], ["分区丙", 5]]:
        ws.append(row)
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), save(book), "a.xlsx")
    assert info.value.kind == "shape" and "mixed" not in info.value.details
    assert "混有非数字" not in str(info.value)


def test_synthetic_crosstab_raw_mode(tmp_path):
    """选「按原样导入（未规整）」：照常导入并标记；「·」要么存空值要么拒收，选存空值后 MAX 是数字。"""
    db = tmp_path / "t.db"
    with pytest.raises(NeedsDecision) as info:
        load_into(str(db), crosstab(), "a.xlsx", header_row=4, raw_mode=True)
    assert info.value.kind == "mixed"
    assert {v["value"] for c in info.value.details["columns"] for v in c["values"]} == {"·"}

    report = load_into(str(db), crosstab(), "a.xlsx", header_row=4, raw_mode=True, mixed="null")
    table = report.tables[0]
    assert table.unshaped is True
    assert table.columns[0] == {"name": "指标", "type": "TEXT", "header": "指标"}
    assert table.columns[1] == {"name": "c_8月1日", "type": "INTEGER", "header": "8月1日"}
    assert table.region == "B4:AG30"
    assert table.blank_rows_skipped == 1
    assert query(db, 'SELECT typeof(MAX("c_8月1日")) FROM 客流汇总')[0][0] == "integer"
    assert {c["kind"] for c in report.conversions} == {"nonnumeric_to_null"}
    assert any("未经规整" in w for w in report.warnings)
    assert any("合并单元格" in w and "B9:AG9" in w for w in report.warnings)


def test_raw_mode_leaves_tidy_tables_shaped(tmp_path):
    book, cached = crosstab_book()
    tidy = book.create_sheet("明细")
    tidy.append(["地区", "金额"])
    tidy.append(["分区甲", 1])
    db = tmp_path / "t.db"
    raw = excel_saved(save(book), cached)
    report = load_into(str(db), raw, "a.xlsx", header_row=1, raw_mode=True, mixed="null")
    assert {t.name: t.unshaped for t in report.tables} == {"客流汇总": True, "明细": False}


# --------------------------------------------------------------------------
# 回执
# --------------------------------------------------------------------------


def test_merged_cells_and_hidden_rows_are_reported(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "s"
    ws.append(["地区", "产品", "销量"])
    ws.append(["分区甲", "A", 1])
    ws.append([None, "B", 2])
    ws.append(["分区乙", "A", 3])
    ws.merge_cells("A2:A3")
    ws.row_dimensions[4].hidden = True
    ws.column_dimensions["B"].hidden = True
    report = load_into(str(tmp_path / "t.db"), save(book), "a.xlsx")
    text = "\n".join(report.warnings)
    assert "1 处合并单元格（如 A2:A3）" in text
    assert "1 行在 Excel 中处于隐藏或筛选状态（如第 4 行）" in text
    assert "1 列在 Excel 中被隐藏（如 B 列）" in text


def test_report_is_json_ready(tmp_path):
    report = load_into(str(tmp_path / "t.db"), xlsx({"s": [["a", "b"], ["1,000", "00123"]]}), "a.xlsx")
    data = asdict(report)
    json.dumps(data, ensure_ascii=False)
    assert data["tables"][0] == {
        "name": "s", "sheet": "s",
        "columns": [{"name": "a", "type": "INTEGER", "header": "a"}, {"name": "b", "type": "TEXT", "header": "b"}],
        "rows": 1, "region": "A1:B2", "blank_rows_skipped": 0, "columns_trimmed": [], "unshaped": False,
    }
    assert report.total_rows == 1


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def test_csv_with_gbk_encoding(tmp_path):
    """中文环境里 GBK 的 CSV 极常见（Excel 另存为 CSV 的默认行为）。

    一律按 UTF-8 读会得到乱码列名，而乱码是不报错的——只是从此搜不到、看不懂。
    """
    db = tmp_path / "t.db"
    raw = "月份,金额\n1月,100\n".encode("gb18030")
    load_into(str(db), raw, "销售.csv")
    table = tables(db)[0]
    assert "月份" in col_types(db, table)
    assert query(db, f'SELECT "金额" FROM "{table}"')[0][0] == 100


def test_utf16_csv(tmp_path):
    db = tmp_path / "t.db"
    load_into(str(db), "月份\t金额\n1月\t100\n".encode("utf-16"), "x.tsv")
    assert query(db, "SELECT 金额 FROM x")[0][0] == 100


def test_tsv_and_semicolon_csv(tmp_path):
    db = tmp_path / "t.db"
    load_into(str(db), b"a\tb\n1\t2\n", "x.tsv")
    table = tables(db)[0]
    assert list(col_types(db, table)) == ["a", "b"]


def test_csv_follows_the_same_rules(tmp_path):
    """CSV 走同样的值、类型、空行、名字规则。"""
    raw = ("编号,金额,备注,\n"
           "00123,\"1,234\",甲,\n"
           ",,,\n"
           "\n"
           "00456,\"2,000\",乙,\n").encode()
    db = tmp_path / "t.db"
    report = load_into(str(db), raw, "账目 2026.csv")
    table = report.tables[0]
    assert table.name == "账目_2026"
    assert [c["name"] for c in table.columns] == ["编号", "金额", "备注"]
    assert table.columns_trimmed == ["D"]
    assert table.blank_rows_skipped == 2
    assert query(db, "SELECT 编号, 金额 FROM 账目_2026") == [("00123", 1234), ("00456", 2000)]


def test_csv_mixed_needs_a_decision(tmp_path):
    raw = ("k,v\n" + "".join(f"r{i},{i}\n" for i in range(1, 10)) + "rx,-\n").encode()
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "a.db"), raw, "x.csv")
    assert info.value.details["columns"][0]["values"] == [{"value": "-", "count": 1}]
    db = tmp_path / "b.db"
    load_into(str(db), raw, "x.csv", mixed="null")
    assert query(db, "SELECT MAX(v), COUNT(v) FROM x")[0] == (9, 9)


def test_unsupported_formats_say_what_to_do(tmp_path):
    db = tmp_path / "t.db"
    with pytest.raises(UnsupportedTable, match="另存为"):
        load_into(str(db), b"x", "old.xls")
    with pytest.raises(UnsupportedTable, match="仅支持"):
        load_into(str(db), b"x", "a.pdf")


def test_bad_arguments(tmp_path):
    with pytest.raises(UnsupportedTable, match="从 1 开始"):
        load_into(str(tmp_path / "t.db"), b"a\n1\n", "x.csv", header_row=0)
    with pytest.raises(UnsupportedTable) as info:
        load_into(str(tmp_path / "t.db"), b"a\n1\n", "x.csv", mixed="maybe")
    text = str(info.value)
    assert text.startswith("导入选项无效")
    # 这句话可能原样进 400：不露参数名和取值
    assert not re.search(r"mixed|reject|null|maybe", text)


# --------------------------------------------------------------------------
# 重传
# --------------------------------------------------------------------------


def test_reupload_replaces_instead_of_appending(tmp_path):
    """重传的语义是"这份数据更新了"。追加会让行数悄悄翻倍，没人会发现。"""
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s": [["a"], [1], [2]]}), "a.xlsx")
    load_into(str(db), xlsx({"s": [["a"], [9]]}), "a.xlsx")
    table = tables(db)[0]
    assert [r[0] for r in query(db, f'SELECT "a" FROM "{table}"')] == [9]


def test_failed_reload_leaves_previous_tables_intact(tmp_path):
    """所有表在一个事务里写：第二张表要拍板时，第一张表不能已经被清空（以前就是这样）。"""
    db = tmp_path / "t.db"
    load_into(str(db), xlsx({"s1": [["a"], [1], [2]], "s2": [["b"], [10]]}), "a.xlsx")
    bad = xlsx({"s1": [["a"], [7]], "s2": [["b"]] + [[i] for i in range(1, 10)] + [["N/A"]]})
    with pytest.raises(NeedsDecision):
        load_into(str(db), bad, "a.xlsx")
    assert [r[0] for r in query(db, "SELECT a FROM s1")] == [1, 2]
    assert [r[0] for r in query(db, "SELECT b FROM s2")] == [10]


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


async def _upload(client, name, content, filename="a.xlsx", **form):
    return await client.post(
        "/api/datasources/upload",
        files={"file": (filename, content,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"name": name, **form},
    )


@pytest.mark.asyncio
async def test_upload_creates_a_queryable_source(client):
    resp = await _upload(
        client, "tab_sales",
        xlsx({"明细": [["月份", "金额"], ["1月", 100], ["2月", 250]]}),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"]["kind"] == "sqlite"
    assert body["replaced"] is False
    # 列名和类型要原样回显：表头行取错了，用户当场就该看见
    cols = {c["name"]: c["type"] for c in body["tables"][0]["columns"]}
    assert cols == {"月份": "TEXT", "金额": "INTEGER"}
    assert body["tables"][0]["rows"] == 2
    # 建完立刻探查过，否则 db_query 工具的描述里没有表清单
    assert body["source"]["table_count"] >= 1

    # 真能查——而且走的是和别的数据源完全相同的那条路
    from app.data.engine import run_query
    from app.db.base import SessionLocal
    from app.db.models import DataSource
    from sqlalchemy import select as _select

    async with SessionLocal() as session:
        row = (await session.execute(
            _select(DataSource).where(DataSource.name == "tab_sales")
        )).scalar_one()
    result = await run_query(row, 'SELECT SUM("金额") AS s FROM "明细"')
    assert result.rows[0][0] == 350


@pytest.mark.asyncio
async def test_reupload_keeps_the_same_source_and_name(client):
    """重传不能换 id 或名字——名字是工具名（db_query__x），写进了保存过的图。"""
    first = await _upload(client, "tab_keep", xlsx({"s": [["a"], [1]]}))
    assert first.status_code == 201
    src_id = first.json()["source"]["id"]

    again = await _upload(client, "tab_keep", xlsx({"s": [["a"], [7], [8]]}))
    assert again.status_code == 201, again.text
    assert again.json()["replaced"] is True
    assert again.json()["source"]["id"] == src_id
    assert again.json()["source"]["name"] == "tab_keep"
    assert again.json()["tables"][0]["rows"] == 2


@pytest.mark.asyncio
async def test_upload_rejects_a_name_that_cannot_be_a_tool(client):
    resp = await _upload(client, "带中文的名字", xlsx({"s": [["a"], [1]]}))
    assert resp.status_code == 400
    assert "工具名" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_will_not_hijack_a_real_database(client):
    """同名的真数据库不能被一个 Excel 顶掉。"""
    from app.db.base import SessionLocal
    from app.db.models import DataSource

    async with SessionLocal() as session:
        session.add(DataSource(name="tab_real", kind="mysql", host="h", database="d"))
        await session.commit()

    resp = await _upload(client, "tab_real", xlsx({"s": [["a"], [1]]}))
    assert resp.status_code == 409
    assert "请换一个名称" in resp.json()["detail"]




def test_broken_excel_message_has_no_exception_class_name(tmp_path):
    """坏掉的 .xlsx：报错说人话，不带 Python 异常类名（BadZipFile、InvalidFileException…）。

    这句话原样进上传的 400 detail，界面上是红字首行。
    """
    with pytest.raises(UnsupportedTable) as info:
        load_into(str(tmp_path / "t.db"), b"not really an xlsx", "orders.xlsx")
    text = str(info.value)
    assert text.startswith("无法打开该 Excel 文件")
    assert "BadZipFile" not in text and "Error" not in text and "Exception" not in text


# --------------------------------------------------------------------------
# 表头行号超出内容、CSV 的说法
# --------------------------------------------------------------------------


@pytest.mark.parametrize("header_row", [42, 500])
def test_header_row_beyond_the_content_says_so(tmp_path, header_row):
    """表里有内容、只是表头行号填大了：报的是行号超出了内容范围、最后一行是第几行，
    不是「没有包含内容的可见工作表」（那会让人以为文件坏了）。"""
    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "明细"
    ws.append(["日期", "金额"])
    for i in range(40):
        ws.append([f"2026-01-{i % 28 + 1:02d}", i])
    with pytest.raises(UnsupportedTable) as info:
        load_into(str(tmp_path / "t.db"), save(book), "t.xlsx", header_row=header_row)
    message = str(info.value)
    assert f"表头行号第 {header_row} 行超出了工作表的内容范围" in message, message
    assert "「明细」到第 41 行" in message and "没有包含内容" not in message
    # 刚好在最后一行还能导入（只有表头、没有数据时另有说法）
    assert load_into(str(tmp_path / "ok.db"), save(book), "t.xlsx", header_row=41).tables


def test_csv_header_row_beyond_the_content_says_so(tmp_path):
    raw = "区域,数量\n甲,1\n乙,2\n".encode()
    with pytest.raises(UnsupportedTable, match="表头行号第 9 行超出了文件的内容范围"):
        load_into(str(tmp_path / "t.db"), raw, "t.csv", header_row=9)
    # 表头那一行是空行、下面有数据的，不是超出范围：照常导入，列名按位置生成
    report = load_into(str(tmp_path / "ok.db"), "标题\n\n甲,1\n乙,2\n".encode(), "t.csv", header_row=2)
    assert report.tables[0].rows == 2


def test_csv_crosstab_is_called_a_file_not_a_sheet(tmp_path):
    """CSV 没有工作表：交叉表拒收、表头为空这些提示说「文件「…」」。"""
    head = "路段," + ",".join(f"2026-01-0{d}" for d in range(1, 9))
    body = ["甲区" + "," * 8] + [f"路段{i}," + ",".join(str(i * d) for d in range(1, 9)) for i in range(1, 4)] \
        + ["乙区" + "," * 8] + [f"路段{i}," + ",".join(str(i + d) for d in range(1, 9)) for i in range(4, 7)]
    raw = "\n".join([head, *body]).encode()
    with pytest.raises(NeedsDecision) as info:
        load_into(str(tmp_path / "t.db"), raw, "cross_traffic.csv")
    message = str(info.value)
    assert message.startswith("文件「cross_traffic」不是一行一条记录的规整表格"), message
    assert "工作表" not in message
    assert {r["sheet"] for r in info.value.details["reasons"]} == {"cross_traffic"}

    report = load_into(str(tmp_path / "blank.db"), ",\n1,2\n3,4\n".encode(), "无表头.csv")
    assert any(w.startswith("文件「无表头」的表头行（第 1 行）没有内容") for w in report.warnings), report.warnings


# --------------------------------------------------------------------------
# 带百分号、单位、货币符号的数字
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("values", "examples"), [
    (["15%", "25%", "60%"], "「15%」、「25%」、「60%」"),
    (["1,234元", "2,000元", "500元"], "「1,234元」、「2,000元」、「500元」"),
    (["¥1234", "¥2000", "¥500"], "「¥1234」、「¥2000」、「¥500」"),
])
def test_numbers_with_units_kept_as_text_are_reported(tmp_path, values, examples):
    """「15%」「1,234元」「¥1234」按文本存（口径要人来定），但 SQLite 对它们 SUM 只取开头的数字、MAX 按字典序：
    以前回执里一个字都没有（import-surface #17 的另一半）。现在逐列写进警告。"""
    db = tmp_path / "t.db"
    raw = ("分区,金额\n" + "\n".join(f'分区{i},"{v}"' for i, v in enumerate(values))).encode()
    report = load_into(str(db), raw, "a.csv")
    assert report.tables[0].columns[1]["type"] == "TEXT"
    [warning] = [w for w in report.warnings if "金额" in w]
    assert f"有 3 个带百分号、单位或货币符号的数字（如 {examples}）" in warning, warning
    assert "已整列按文本保存" in warning and "不会按数值计算" in warning
    assert report.conversions == []


def test_a_few_unit_values_in_a_text_column_are_not_reported(tmp_path):
    """备注列里偶尔出现一个「50%」不提：这类值加上纯数字不过半的，是真正的文本列。"""
    raw = "分区,备注\n甲,正常\n乙,下降约50%\n丙,50%\n丁,待核实\n戊,无\n".encode()
    report = load_into(str(tmp_path / "t.db"), raw, "a.csv")
    assert not [w for w in report.warnings if "备注" in w]


def test_dates_and_slots_are_not_numbers_with_units(tmp_path):
    """「8月1日」「2026年」「3号」「8-9」「12A」不是带单位的数量：日期横排的表头照样认得出，编号列不写警告。"""
    for text in ("8月1日", "2026年", "8月", "3号", "18时", "8-9", "12A", "1E5", "+86"):
        assert tabular._classify_text(text)[2] != "unit", text
    for text in ("15%", "60％", "1,234元", "¥1234", "$1,234.50", "-5%", "12kg", "50万", "1.5亿元"):
        assert tabular._classify_text(text)[2] == "unit", text
    raw = "编号,数量\n12A,1\n13B,2\n14C,3\n".encode()
    assert not [w for w in load_into(str(tmp_path / "t.db"), raw, "a.csv").warnings if "编号" in w]
