"""xlsx 版式元数据的流式扫描：openpyxl read_only 拿不到的，这里要拿全、拿准。

导入的第二遍按这里算出的边界读值（tabular.load_into），所以边界错了就是数据错了：
`<dimension>` 陈旧时少读一截、离群格把区域撑成几千万格。夹具都用 openpyxl 现造，
需要 openpyxl 写不出来的形态（缓存值、无坐标的单元格、DTD）时直接改压缩包里的 XML。
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import asdict

import pytest
from openpyxl import Workbook

from app.data import xlsx_scan
from app.data.xlsx_scan import UnsupportedTable, scan


def save(book: Workbook) -> bytes:
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def patch(raw: bytes, edits: dict[str, Callable[[str], str]]) -> bytes:
    """改压缩包里的部件：{路径: 文本 → 文本}。"""
    src = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename in edits:
                data = edits[info.filename](data.decode()).encode()
            dst.writestr(info, data)
    return out.getvalue()


def cache(values: dict[str, str]) -> Callable[[str], str]:
    """给 openpyxl 写出的公式补上缓存值（模拟 Excel 保存过）。"""
    def edit(text: str) -> str:
        def fill(m: re.Match[str]) -> str:
            ref = m.group(1)
            return f'<c r="{ref}"><f>{m.group(2)}</f><v>{values[ref]}</v></c>' if ref in values else m.group(0)
        return re.sub(r'<c r="([A-Z]+\d+)"><f>([^<]*)</f><v></v></c>', fill, text)
    return edit


SHEET1 = "xl/worksheets/sheet1.xml"


# --------------------------------------------------------------------------
# 边界
# --------------------------------------------------------------------------


def test_bounds_come_from_cells_not_from_dimension():
    """<dimension> 是写文件的程序自己填的，可能是陈旧的：真实边界只能从非空格算。"""
    book = Workbook()
    ws = book.active
    ws["C5"] = "地区"
    ws["D5"] = "金额"
    for r in range(6, 26):
        ws.cell(r, 3, f"分区{r}")
        ws.cell(r, 4, r)
    raw = patch(save(book), {SHEET1: lambda t: re.sub(r'<dimension ref="[^"]*"/>', '<dimension ref="A1:B2"/>', t)})
    sheet = scan(raw).sheets[0]
    assert sheet.dimension == "A1:B2"            # 记下来，但不信
    assert sheet.bounds is not None
    assert sheet.bounds.a1() == "C5:D25"
    assert sheet.nonempty == 42


def test_missing_dimension_is_fine():
    """openpyxl 的 write_only 根本不写 <dimension>。"""
    book = Workbook(write_only=True)
    ws = book.create_sheet("明细")
    ws.append(["a", "b"])
    for i in range(30):
        ws.append([i, i * 2])
    sheet = scan(save(book)).sheets[0]
    assert sheet.dimension is None
    assert sheet.bounds.a1() == "A1:B31"


def test_blank_strings_do_not_count_but_formulas_do():
    """只有空白的文本算空（导入时也按空值存）；有公式的格不算空，哪怕没有缓存值。"""
    book = Workbook()
    ws = book.active
    ws["A1"] = "x"
    ws["B1"] = "   "              # 共享字符串里的空白
    ws["E9"] = "=A1"              # 没有缓存值的公式
    sheet = scan(save(book)).sheets[0]
    assert sheet.nonempty == 2
    assert sheet.bounds.a1() == "A1:E9"


def test_inline_strings_and_cells_without_coordinates():
    """有的程序写内联字符串、省略行号和单元格坐标：位置按顺序推出来。"""
    book = Workbook()
    book.active["A1"] = "占位"
    raw = save(book)
    data = ('<sheetData><row><c t="inlineStr"><is><t>甲</t></is></c><c><v>1</v></c></row>'
            '<row><c t="inlineStr"><is><t> </t></is></c><c t="inlineStr"><is><r><t>乙</t></r></is></c>'
            '<c><v>2</v></c></row></sheetData>')
    raw = patch(raw, {SHEET1: lambda t: re.sub(r"<sheetData>.*</sheetData>", data, t, flags=re.S)})
    sheet = scan(raw).sheets[0]
    assert sheet.bounds.a1() == "A1:C2"
    assert sheet.nonempty == 4                    # 「 」是空白，不算


def test_far_cells_name_the_outliers():
    """稀疏判定拒收时要说出是哪几个远处的格子：A 列一百行数据、XFD 列冒出来一个。"""
    book = Workbook()
    ws = book.active
    for r in range(1, 101):
        ws.cell(r, 1, r)
    ws["XFD5"] = "误输入"
    ws["A90000"] = 1
    sheet = scan(save(book)).sheets[0]
    assert sheet.bounds.a1() == "A1:XFD90000"
    assert sheet.far_cells == ["XFD5", "A90000"]


def test_a_far_cell_jumping_both_ways_is_named_once():
    """右下角一个格子（XFD1048576）同时撑出了右边界和下边界：示例里只出现一次。"""
    book = Workbook()
    ws = book.active
    for r in range(1, 201):
        ws.cell(r, 1, r)
        ws.cell(r, 2, r)
    ws["XFD1048576"] = "x"
    assert scan(save(book)).sheets[0].far_cells == ["XFD1048576"]


# --------------------------------------------------------------------------
# 版式
# --------------------------------------------------------------------------


def test_merged_hidden_and_autofilter():
    book = Workbook()
    ws = book.active
    for r in range(1, 12):
        ws.append([f"r{r}", r, r * 10, r * 100])
    ws.merge_cells("A1:B1")
    ws.merge_cells("C3:D4")
    for r in (3, 4, 5, 9):
        ws.row_dimensions[r].hidden = True
    ws.column_dimensions["C"].hidden = True
    ws.auto_filter.ref = "A1:D11"
    ws.auto_filter.add_filter_column(0, ["r2"])
    sheet = scan(save(book)).sheets[0]
    assert sorted(sheet.merged) == ["A1:B1", "C3:D4"]
    assert sheet.merged_total == 2
    assert sheet.hidden_rows == [(3, 5), (9, 9)]     # 连续的合成区间
    assert sheet.hidden_cols == [(3, 3)]
    assert sheet.autofilter == "A1:D11"
    # <filterColumn> 不是公式：字节级查找 b"<f" 会误中，按元素名判断不会
    assert sheet.formulas == 0


def test_sheet_states():
    book = Workbook()
    book.active.title = "可见"
    book.active["A1"] = 1
    hidden = book.create_sheet("隐藏")
    hidden.sheet_state = "hidden"
    very = book.create_sheet("配置")
    very.sheet_state = "veryHidden"
    very["A1"] = "口令"
    sc = scan(save(book))
    assert [(s.name, s.state) for s in sc.sheets] == [("可见", "visible"), ("隐藏", "hidden"), ("配置", "veryHidden")]
    assert sc.sheet("隐藏").bounds is None
    assert sc.sheet("配置").nonempty == 1


# --------------------------------------------------------------------------
# 公式与错误值
# --------------------------------------------------------------------------


def test_uncached_formulas_are_listed():
    """openpyxl 写的公式没有缓存值，data_only 读出来是空：要能逐格指出来。"""
    book = Workbook()
    ws = book.active
    ws.append(["a", "b", "合计"])
    for r in range(2, 6):
        ws.append([r, r, f"=A{r}+B{r}"])
    sheet = scan(save(book)).sheets[0]
    assert sheet.formulas == 4
    assert sheet.formulas_uncached.cells == ["C2", "C3", "C4", "C5"]
    assert sheet.formulas_uncached.total == 4
    assert sheet.formulas_uncached.max_row == 5


def test_cached_formulas_and_empty_string_results_are_not_uncached():
    book = Workbook()
    ws = book.active
    ws["A1"] = 1
    ws["A2"] = "=A1*2"
    ws["A3"] = '=IF(A1>5,"x","")'
    raw = save(book)
    raw = patch(raw, {SHEET1: lambda t: cache({"A2": "2"})(t).replace('<c r="A3">', '<c r="A3" t="str">')})
    sheet = scan(raw).sheets[0]
    assert sheet.formulas == 2
    # 公式返回空字符串时 Excel 写 t="str" 加空的 <v>：那是有缓存的空值
    assert sheet.formulas_uncached.total == 0


def test_uncached_list_is_capped_but_counted():
    book = Workbook()
    ws = book.active
    for r in range(1, 81):
        ws.cell(r, 1, r)
        ws.cell(r, 2, f"=A{r}")
    cells = scan(save(book)).sheets[0].formulas_uncached
    assert cells.total == 80
    assert len(cells.cells) == xlsx_scan.MAX_CELLS
    assert cells.max_row == 80           # 记不全坐标，也能精确判断某行以下有没有


def test_error_cells():
    book = Workbook()
    book.active["A1"] = 1
    raw = patch(save(book), {SHEET1: lambda t: t.replace(
        '<c r="A1" t="n"><v>1</v></c>',
        '<c r="A1" t="n"><v>1</v></c><c r="B1" t="e"><f>1/0</f><v>#DIV/0!</v></c><c r="C1" t="e"><v>#N/A</v></c>')})
    sheet = scan(raw).sheets[0]
    assert sheet.errors.cells == ["B1", "C1"]
    assert sheet.formulas == 1 and sheet.formulas_uncached.total == 0


@pytest.mark.parametrize(("formula", "cell", "expected"), [
    ("SUM(C22:C25)", "C26", True),          # 表内合计
    ("SUM($C$22:$C$25)", "C26", True),
    ("SUBTOTAL(9,D2:D9)", "D10", True),
    ("B3*C3", "D3", False),                 # 逐行计算
    ("SUM(C$2:C3)", "D3", False),           # 累计：区域含本行
    ("C5/SUM(C$5:C$20)", "D5", False),      # 占比：区域不在上方
    ("SUM(C24:C25)", "D26", False),         # 别的列
    ("SUM(Sheet2!C2:C9)", "C26", False),    # 别的工作表
    ("C25", "C26", False),                  # 单格引用
    ("SUM(C25:C25)", "C26", False),         # 只有一行
    # 逐格相加的合计：单格引用至少两个、全在本格正上方的同一列
    ("B2+B3+B4", "B5", True),
    ("SUM(C2,C3,C4)", "C5", True),
    ("SUBTOTAL(109,$C$2,$C$3)", "C5", True),
    ("b2+b3", "B5", True),                  # 小写也认
    ("C2+B3", "C3", False),                 # 滚动余额：上一行余额加本行发生额
    ("B4+C4", "B5", False),                 # 引用了别的列
    ("B2+B5", "B5", False),                 # 含本格这一行
    ("B6+B7", "B5", False),                 # 在下方
    ("Sheet2!B2+Sheet2!B3", "B5", False),   # 别的工作表
    ("'B2 表'!B2+'B2 表'!B3", "B5", False),  # 带引号的工作表名
    ('"B2"&"B3"', "B5", False),             # 字符串里的不是引用
    ("SUM(Sheet2!B2:B4)+B3", "B5", False),  # 别的工作表的区域，抹掉后只剩一个单格
    ("LOG10(B2)+B3", "B5", True),           # 函数名里的「G10」不是引用
    ("LOG10(C2)", "G11", False),
])
def test_formulas_aggregating_rows_above(formula, cell, expected):
    row, col = xlsx_scan.parse_ref(cell)
    assert xlsx_scan._aggregates_above(formula, row, col) is expected


def _sales_table(totals: int = 1) -> xlsx_scan.TableScan:
    """A1:C6：表头一行、明细四行（2 到 5 行）、汇总一行（第 6 行）。"""
    return xlsx_scan.TableScan(name="销售表", ref="A1:C6" if totals else "A1:C5", sheet="s",
                               totals_row_count=totals, columns=["地区", "金额", "数量"])


@pytest.mark.parametrize(("formula", "cell", "expected"), [
    ("SUBTOTAL(109,销售表[金额])", "B6", True),            # 表格对象的汇总行
    ("SUBTOTAL(109,销售表[[#Data],[数量]])", "C6", True),
    ("SUM(销售表[金额])", "C6", False),                     # 别的列
    ("SUM(销售表[[金额]:[数量]])", "C6", True),             # 列区间覆盖本列
    ("SUM(销售表[])", "A6", True),                          # 不写列名：整张表的明细
    ("SUM(销售表[#All])", "B9", True),                     # 表格下方的合计
    ("SUM(销售表[#All])", "B6", False),                    # #All 含汇总行本身
    ("销售表[[#This Row],[金额]]*2", "B4", False),          # 本行
    ("[@金额]*2", "B4", False),
    ("销售表[[#Totals],[金额]]", "B8", False),              # 只引用汇总行：占比之类，不是合计
    ("销售表[[#Headers],[金额]]", "B8", False),
    ("B3/SUM(销售表[金额])", "B3", False),                  # 占比：明细含本行
    ("SUM(其他表[金额])", "B6", False),                     # 不是本工作表的表格对象
    ("SUM(销售表[金额)", "B6", False),                      # 括号不成对
])
def test_structured_references_aggregating_rows_above(formula, cell, expected):
    row, col = xlsx_scan.parse_ref(cell)
    tables = {"销售表": _sales_table()}
    assert xlsx_scan._aggregates_above(formula, row, col, tables) is expected


def test_structured_reference_below_a_table_without_totals_row():
    """没勾汇总行、在表格下面自己写了一行合计：同样是合计行。"""
    tables = {"销售表": _sales_table(totals=0)}
    row, col = xlsx_scan.parse_ref("B6")
    assert xlsx_scan._aggregates_above("SUM(销售表[金额])", row, col, tables) is True
    assert xlsx_scan._aggregates_above("SUM(销售表[金额])", row, col) is False      # 不知道表格位置就不猜


def test_table_totals_row_formula_is_recorded():
    from openpyxl.worksheet.table import Table

    book = Workbook()
    ws = book.active
    ws.append(["地区", "金额"])
    for r in [["甲", 100], ["乙", 200], ["丙", 300]]:
        ws.append(r)
    ws.append(["汇总", "=SUBTOTAL(109,销售表[金额])"])
    table = Table(displayName="销售表", ref="A1:B5")
    table.totalsRowCount = 1
    ws.add_table(table)
    sc = scan(save(book))
    assert sc.sheets[0].formulas_above.cells == ["B5"]
    assert sc.tables[0].totals_rows() == (5, 5)


def test_formulas_above_are_recorded_with_text():
    book = Workbook()
    ws = book.active
    for r in range(1, 5):
        ws.append([f"x{r}", r])
    ws["B5"] = "=SUM(B1:B4)"
    above = scan(save(book)).sheets[0].formulas_above
    assert above.cells == ["B5"]
    assert above.texts == ["SUM(B1:B4)"]


# --------------------------------------------------------------------------
# 工作簿级
# --------------------------------------------------------------------------


def test_workbook_level_metadata():
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.table import Table

    book = Workbook()
    ws = book.active
    ws.title = "月报"
    ws.append(["地区", "金额"])
    ws.append(["分区甲", 100])
    ws.append(["分区乙", 200])
    ws.append(["合计", 300])
    table = Table(displayName="销售表", ref="A1:B4")
    table.totalsRowCount = 1
    ws.add_table(table)
    book.defined_names["数据区"] = DefinedName("数据区", attr_text="月报!$A$1:$B$3")
    ws.defined_names["本表区域"] = DefinedName("本表区域", attr_text="月报!$A$2:$B$3")
    sc = scan(save(book))
    # openpyxl 写文件时总带 fullCalcOnLoad；XlsxWriter 同样，还会把公式缓存写成 0
    assert sc.full_calc_on_load is True
    assert [asdict(t) for t in sc.tables] == [
        {"name": "销售表", "ref": "A1:B4", "sheet": "月报", "totals_row_count": 1, "header_row_count": 1,
         "columns": ["地区", "金额"]},
    ]
    assert sc.tables[0].span() == (1, 1, 4, 2)
    assert sc.tables[0].totals_rows() == (4, 4)
    assert sc.defined_names["数据区"] == "月报!$A$1:$B$3"
    assert sc.defined_names["月报!本表区域"] == "月报!$A$2:$B$3"    # 工作表级的名称带上表名


def test_full_calc_flag_absent():
    book = Workbook()
    book.active["A1"] = 1
    raw = patch(save(book), {"xl/workbook.xml": lambda t: t.replace(' fullCalcOnLoad="1"', "")})
    assert scan(raw).full_calc_on_load is False


def test_strict_ooxml_namespaces():
    """Strict OOXML 换了命名空间：元素和关系都按本地名认。"""
    book = Workbook()
    book.active.append(["a", "b"])
    book.active.append([1, 2])
    strict_main = "http://purl.oclc.org/ooxml/spreadsheetml/main"
    strict_rel = "http://purl.oclc.org/ooxml/officeDocument/relationships"
    tran_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    tran_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    def strict(text: str) -> str:
        return text.replace(tran_main, strict_main).replace(tran_rel, strict_rel)

    raw = patch(save(book), {"xl/workbook.xml": strict, SHEET1: strict, "xl/_rels/workbook.xml.rels": strict})
    sheet = scan(raw).sheets[0]
    assert sheet.bounds.a1() == "A1:B2"


def test_result_is_json_ready():
    import json

    book = Workbook()
    book.active["A1"] = 1
    json.dumps(asdict(scan(save(book))))


# --------------------------------------------------------------------------
# 拒收：坏文件、炸弹、DTD
# --------------------------------------------------------------------------


def test_not_a_zip():
    with pytest.raises(UnsupportedTable) as info:
        scan(b"not really an xlsx")
    text = str(info.value)
    assert text.startswith("无法打开该 Excel 文件")
    assert "BadZipFile" not in text


def test_zip_without_workbook():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(UnsupportedTable, match="无法打开该 Excel 文件"):
        scan(buf.getvalue())


def test_truncated_xml():
    book = Workbook()
    book.active["A1"] = 1
    raw = patch(save(book), {SHEET1: lambda t: t[: len(t) // 2]})
    with pytest.raises(UnsupportedTable, match="无法打开该 Excel 文件"):
        scan(raw)


def test_total_uncompressed_size_is_limited(monkeypatch):
    book = Workbook()
    ws = book.active
    for r in range(1, 200):
        ws.append(["一段比较长的文字" * 4] * 5)
    raw = save(book)
    monkeypatch.setattr(xlsx_scan, "MAX_UNCOMPRESSED", 20_000)
    with pytest.raises(UnsupportedTable, match="解压后超过"):
        scan(raw)


def test_single_part_size_is_limited(monkeypatch):
    book = Workbook()
    ws = book.active
    for r in range(1, 400):
        ws.append([r, r, r, r])
    raw = save(book)
    monkeypatch.setattr(xlsx_scan, "MAX_XML_BYTES", 10_000)
    with pytest.raises(UnsupportedTable, match="解压后超过"):
        scan(raw)


def test_reading_counts_actual_bytes(monkeypatch):
    """声明的大小先查一遍，读的时候再实数一遍：压缩包头里的大小可以造假，读出来的字节数造不了假。"""
    book = Workbook()
    ws = book.active
    for r in range(1, 400):
        ws.append([r, r, r, r])
    raw = save(book)
    budget_seen: list[int] = []
    real_take = xlsx_scan._Budget.take

    def take(self, n):
        budget_seen.append(n)
        real_take(self, n)

    monkeypatch.setattr(xlsx_scan._Budget, "take", take)
    scan(raw)
    assert sum(budget_seen) > 10_000


def test_doctype_is_rejected():
    """OOXML 不需要 DTD；有 DTD 只可能是实体膨胀之类的攻击。"""
    book = Workbook()
    book.active["A1"] = 1
    bomb = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
    raw = patch(save(book), {SHEET1: lambda t: bomb + re.sub(r"^<\?xml[^>]*\?>", "", t)})
    with pytest.raises(UnsupportedTable, match="DTD"):
        scan(raw)


def _rewrite(raw: bytes, part: str, data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, data if info.filename == part else src.read(info.filename))
    return out.getvalue()


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_doctype_in_utf16_is_rejected(encoding):
    """换成 UTF-16 写，字节串「<!DOCTYPE」就对不上了；拦在解析器上，和编码无关。"""
    book = Workbook()
    book.active["A1"] = 1
    raw = save(book)
    body = re.sub(r"^<\?xml[^>]*\?>", "", zipfile.ZipFile(io.BytesIO(raw)).read(SHEET1).decode())
    text = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "aaa">]>' + body
    with pytest.raises(UnsupportedTable, match="DTD"):
        scan(_rewrite(raw, SHEET1, text.encode(encoding)))


def test_utf16_parts_without_doctype_are_fine():
    book = Workbook()
    book.active["A1"] = 1
    raw = save(book)
    body = re.sub(r"^<\?xml[^>]*\?>", "", zipfile.ZipFile(io.BytesIO(raw)).read(SHEET1).decode())
    text = '<?xml version="1.0" encoding="UTF-16"?>' + body
    assert scan(_rewrite(raw, SHEET1, text.encode("utf-16"))).sheets[0].bounds.a1() == "A1:A1"


@pytest.mark.parametrize("part", ["xl/styles.xml", "docProps/core.xml", "[Content_Types].xml"])
def test_doctype_in_parts_the_scan_does_not_read_is_rejected(part):
    """样式、文档属性这些部件扫描不读、openpyxl 读：DTD 的拒收一样要覆盖到。"""
    book = Workbook()
    book.active["A1"] = 1
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaa">]>'
    raw = patch(save(book), {part: lambda t: bomb + re.sub(r"^<\?xml[^>]*\?>", "", t)})
    with pytest.raises(UnsupportedTable, match="DTD"):
        scan(raw)


def test_unreadable_prolog_is_rejected():
    """序言都读不过去（编码不认识）：别的解析器读出来是什么样说不准，拒收。"""
    book = Workbook()
    book.active["A1"] = 1
    raw = patch(save(book), {"xl/styles.xml": lambda t: '<?xml version="1.0" encoding="x-no-such-encoding"?>'
                             + re.sub(r"^<\?xml[^>]*\?>", "", t)})
    with pytest.raises(UnsupportedTable) as info:
        scan(raw)
    assert str(info.value).startswith("无法打开该 Excel 文件")


# --------------------------------------------------------------------------
# 拒收：超出 Excel 自身上限的结构（压缩比极高的小文件解出来撑爆内存）
# --------------------------------------------------------------------------


def _one_sheet(data: str) -> bytes:
    """一张工作表，sheetData 换成给定的 XML。"""
    book = Workbook()
    book.active["A1"] = 1
    return patch(save(book), {SHEET1: lambda t: re.sub(r"<sheetData>.*</sheetData>|<sheetData/>",
                                                        lambda _m: data, t, flags=re.S)})


def test_row_number_beyond_excel_is_rejected():
    raw = _one_sheet('<sheetData><row r="1048577"><c r="A1048577"><v>1</v></c></row></sheetData>')
    with pytest.raises(UnsupportedTable, match="行数上限"):
        scan(raw)


def test_row_elements_beyond_excel_are_rejected(monkeypatch):
    """不写行号的空行一个个往下排：个数到了 Excel 的上限就拒收（几十 KB 的压缩包能解出几百万个）。"""
    monkeypatch.setattr(xlsx_scan, "EXCEL_MAX_ROWS", 200)
    with pytest.raises(UnsupportedTable, match="行数上限"):
        scan(_one_sheet("<sheetData>" + "<row/>" * 201 + "</sheetData>"))
    assert scan(_one_sheet("<sheetData>" + "<row/>" * 199 + '<row><c><v>1</v></c></row></sheetData>')
                ).sheets[0].bounds.a1() == "A200:A200"


def test_column_beyond_excel_is_rejected():
    with pytest.raises(UnsupportedTable, match="列数上限"):
        scan(_one_sheet('<sheetData><row r="1"><c r="XFE1"><v>1</v></c></row></sheetData>'))
    assert scan(_one_sheet('<sheetData><row r="1"><c r="XFD1"><v>1</v></c></row></sheetData>')
                ).sheets[0].bounds.a1() == "XFD1:XFD1"


def test_elements_left_hanging_are_capped_but_cleared_rows_are_not_counted(monkeypatch):
    """行清空后它下面的单元格就释放了，不算；行以外一直挂着的元素（未知元素、合并区）算，超了拒收。"""
    monkeypatch.setattr(xlsx_scan, "MAX_ALIVE", 500)
    rich = '<c t="inlineStr"><is><r><rPr><b/><sz val="9"/></rPr><t>甲</t></r><r><t>乙</t></r></is></c>'
    rows = "".join(f'<row r="{r}">{rich}<c><v>{r}</v></c><c><f>B{r}*2</f><v>{r * 2}</v></c></row>'
                   for r in range(1, 301))
    # 300 个行壳加几个零碎：没超。单元格加起来有好几千个元素，要是没从计数里减掉就会超
    assert scan(_one_sheet(f"<sheetData>{rows}</sheetData>")).sheets[0].nonempty == 900
    with pytest.raises(UnsupportedTable, match="元素过多"):
        scan(_one_sheet("<sheetData><row><c><v>1</v></c></row>" + "<x/>" * 600 + "</sheetData>"))
    merges = "".join(f'<mergeCell ref="A{r}:B{r}"/>' for r in range(1, 601))
    with pytest.raises(UnsupportedTable, match="元素过多"):
        scan(_one_sheet(f'<sheetData><row><c><v>1</v></c></row></sheetData><mergeCells>{merges}</mergeCells>'))


def test_cell_text_and_formula_length_follow_excel():
    def inline(n: int) -> bytes:
        return _one_sheet(f'<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{"字" * n}</t></is></c></row></sheetData>')

    assert scan(inline(32_767)).sheets[0].nonempty == 1
    with pytest.raises(UnsupportedTable, match="A1 中的文字超过"):
        scan(inline(32_768))
    with pytest.raises(UnsupportedTable, match="文字超过"):
        scan(_one_sheet(f'<sheetData><row r="1"><c r="A1" t="str"><v>{"x" * 32_768}</v></c></row></sheetData>'))
    with pytest.raises(UnsupportedTable, match="公式超过"):
        scan(_one_sheet(f'<sheetData><row r="1"><c r="A1"><f>{"1+" * 4097}1</f><v>1</v></c></row></sheetData>'))


SST_REL = ('<Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
           'Target="sharedStrings.xml" Id="rId99"/>')
SST_CT = ('<Override PartName="/xl/sharedStrings.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>')


def _with_sst(items: str, *, ct_part: str | None = "/xl/sharedStrings.xml") -> bytes:
    """给工作簿加一张共享字符串表（openpyxl 写的是内联字符串，没有这张表）。"""
    raw = _one_sheet('<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>')
    sst = ('<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/'
           f'spreadsheetml/2006/main">{items}</sst>')
    out = io.BytesIO()
    src = zipfile.ZipFile(io.BytesIO(raw))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename).decode()
            if info.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace("</Relationships>", SST_REL + "</Relationships>")
            elif info.filename == "[Content_Types].xml" and ct_part:
                data = data.replace("</Types>", SST_CT.replace("/xl/sharedStrings.xml", ct_part) + "</Types>")
            dst.writestr(info, data)
        dst.writestr("xl/sharedStrings.xml", sst)
    return out.getvalue()


def test_shared_strings_follow_excel_limits(monkeypatch):
    assert scan(_with_sst("<si><t>甲</t></si>")).sheets[0].nonempty == 1
    with pytest.raises(UnsupportedTable, match="一项文字超过"):
        scan(_with_sst(f"<si><t>{'x' * 32_768}</t></si>"))
    monkeypatch.setattr(xlsx_scan, "MAX_SHARED_STRINGS", 10)
    with pytest.raises(UnsupportedTable, match="条目超过"):
        scan(_with_sst("<si><t>甲</t></si>" * 11))
    monkeypatch.setattr(xlsx_scan, "MAX_SI_ELEMENTS", 20)
    with pytest.raises(UnsupportedTable, match="元素过多"):
        scan(_with_sst("<si>" + "<r><t>甲</t></r>" * 30 + "</si>"))


def test_shared_strings_must_be_registered_consistently():
    """内容类型清单（openpyxl 按它找）和工作簿关系文件（扫描按它找）指的共享字符串表不同：拒收。"""
    with pytest.raises(UnsupportedTable, match="共享字符串表不一致"):
        scan(_with_sst("<si><t>甲</t></si>", ct_part="/xl/other.xml"))


STYLES = "xl/styles.xml"


def test_small_parts_have_their_own_limits(monkeypatch):
    """样式、主题这些部件 openpyxl 整份建树：字节数、元素个数、格式条目数都有上限。图片这类二进制不受影响。"""
    book = Workbook()
    book.active["A1"] = 1
    raw = save(book)
    padded = patch(raw, {STYLES: lambda t: t.replace("</styleSheet>", "<extLst/>" * 3000 + "</styleSheet>")})
    monkeypatch.setattr(xlsx_scan, "MAX_PART_BYTES", 20_000)
    with pytest.raises(UnsupportedTable, match="解压后超过"):
        scan(padded)
    # 100 KB 的图片：不是 XML，不按小部件的上限管
    assert scan(_with_part(raw, "xl/media/image1.png", bytes(range(256)) * 400)).sheets[0].nonempty == 1
    monkeypatch.setattr(xlsx_scan, "MAX_PART_BYTES", 16 * 1024 ** 2)
    monkeypatch.setattr(xlsx_scan, "MAX_PART_ELEMENTS", 2000)
    with pytest.raises(UnsupportedTable, match="元素超过"):
        scan(padded)
    # 计算链、批注这类没人解析的部件可以很大（和公式数成正比），不按小部件管
    chain = '<calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + '<c r="A1"/>' * 3000 + "</calcChain>"
    assert scan(_with_part(raw, "xl/calcChain.xml", chain)).sheets[0].nonempty == 1
    monkeypatch.setattr(xlsx_scan, "MAX_XF", 3)
    many_xf = patch(raw, {STYLES: lambda t: re.sub(r"<cellXfs[^>]*>", lambda m: m.group(0) + '<xf numFmtId="0"/>' * 5, t)})
    with pytest.raises(UnsupportedTable, match="单元格格式超过"):
        scan(many_xf)


def test_entry_count_is_limited(monkeypatch):
    book = Workbook()
    book.active["A1"] = 1
    monkeypatch.setattr(xlsx_scan, "MAX_ENTRIES", 5)
    with pytest.raises(UnsupportedTable, match="部件超过"):
        scan(save(book))


# --------------------------------------------------------------------------
# 拒收：两处登记对不上的工作簿、同名工作表
# --------------------------------------------------------------------------

WORKBOOK = "xl/workbook.xml"


def _two_sheets() -> bytes:
    book = Workbook()
    book.active.title = "明细"
    book.active["A1"] = "甲"
    book.create_sheet("备注")["A1"] = "乙"
    return save(book)


@pytest.mark.parametrize("first, second", [("明细", "明细"), ("mingxi", "MINGXI")])
def test_duplicate_sheet_names_are_rejected(first, second):
    """两个同名工作表（前一个隐藏）：按名字取表会拿到隐藏的那个。名字不区分大小写。"""
    def edit(t: str) -> str:
        t = t.replace('name="明细" sheetId="1" state="visible"', f'name="{first}" sheetId="1" state="veryHidden"')
        return t.replace('name="备注"', f'name="{second}"')

    with pytest.raises(UnsupportedTable, match="两个工作表同名"):
        scan(patch(_two_sheets(), {WORKBOOK: edit}))


def _with_part(raw: bytes, path: str, data: str) -> bytes:
    out = io.BytesIO()
    src = zipfile.ZipFile(io.BytesIO(raw))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr(path, data)
    return out.getvalue()


def test_workbook_part_must_be_registered_consistently():
    """包关系文件（扫描按它找）和内容类型清单（openpyxl 按它找）指向不同的工作簿主部件：拒收。"""
    raw = _two_sheets()
    decoy = zipfile.ZipFile(io.BytesIO(raw)).read(WORKBOOK).decode()
    raw = _with_part(raw, "xl/decoy.xml", decoy)
    raw = _with_part(raw, "xl/_rels/decoy.xml.rels",
                     zipfile.ZipFile(io.BytesIO(raw)).read("xl/_rels/workbook.xml.rels").decode())
    pointed = patch(raw, {"_rels/.rels": lambda t: t.replace('Target="xl/workbook.xml"', 'Target="xl/decoy.xml"')})
    with pytest.raises(UnsupportedTable, match="工作簿主部件不一致"):
        scan(pointed)
    main = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    twice = patch(raw, {"[Content_Types].xml": lambda t: t.replace(
        "</Types>", f'<Override PartName="/xl/decoy.xml" ContentType="{main}"/></Types>')})
    with pytest.raises(UnsupportedTable, match="不止一个工作簿主部件"):
        scan(twice)
    assert [s.name for s in scan(raw).sheets] == ["明细", "备注"]       # 多一个没人指的部件不要紧


# --------------------------------------------------------------------------
# 拒收：改了名的部件里的 DTD
# --------------------------------------------------------------------------


def _rename(raw: bytes, old: str, new: str) -> bytes:
    """把一个部件改名，关系文件和内容类型清单里的引用跟着改。"""
    out = io.BytesIO()
    src = zipfile.ZipFile(io.BytesIO(raw))
    tail_old, tail_new = old.rsplit("/", 1)[-1], new.rsplit("/", 1)[-1]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            name = new if info.filename == old else info.filename
            if info.filename.endswith(".rels") or info.filename == "[Content_Types].xml":
                data = data.decode().replace(tail_old, tail_new).encode()
            dst.writestr(name, data)
    return out.getvalue()


ENTITY = '<?xml version="1.0"?><!DOCTYPE worksheet [<!ENTITY e "展开">]>'


@pytest.mark.parametrize("new", ["xl/worksheets/sheet1.bin", "xl/worksheets/sheet1", "xl/worksheets/sheet1.svg"])
def test_doctype_in_a_renamed_part_is_rejected(new):
    """工作表改名成不以 .xml 结尾：关系文件照样指着它，扫描和 openpyxl 照样当 XML 解析。DTD 一样拒收。"""
    book = Workbook()
    book.active["A1"] = 1
    raw = _rename(save(book), SHEET1, new)
    assert scan(raw).sheets[0].bounds.a1() == "A1:A1"         # 光改名不影响导入
    bombed = _rewrite(raw, new, (ENTITY + re.sub(r"^<\?xml[^>]*\?>", "", zipfile.ZipFile(io.BytesIO(raw))
                                                 .read(new).decode())).encode())
    with pytest.raises(UnsupportedTable, match="DTD"):
        scan(bombed)


def test_binary_parts_and_plain_svg_doctype_are_fine():
    """图片、打印设置、宏这些二进制部件解析不了是正常的；SVG 带不含内部子集的 DOCTYPE 也是正常的。"""
    book = Workbook()
    book.active["A1"] = 1
    raw = save(book)
    raw = _with_part(raw, "xl/media/image1.png", "\x89PNG\r\n\x1a\n" + "\x00" * 64)
    raw = _with_part(raw, "xl/vbaProject.bin", "\xd0\xcf\x11\xe0" + "\x01" * 64)
    svg = ('<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
           '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg"/>')
    assert scan(_with_part(raw, "xl/media/image2.svg", svg)).sheets[0].nonempty == 1
    subset = '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY e "x">]><svg xmlns="http://www.w3.org/2000/svg"/>'
    with pytest.raises(UnsupportedTable, match="DTD"):
        scan(_with_part(raw, "xl/media/image3.svg", subset))


def test_chartsheets_and_their_charts_count_as_small_parts(monkeypatch):
    """图表工作表 openpyxl 会整份解析，连同它引用的绘图和图表：这些部件同样按小部件设限。"""
    from openpyxl.chart import BarChart, Reference

    book = Workbook()
    ws = book.active
    for r in range(1, 6):
        ws.append([r])
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=1, min_row=1, max_row=5))
    book.create_chartsheet("图").add_chart(chart)
    raw = save(book)
    charts = [n for n in zipfile.ZipFile(io.BytesIO(raw)).namelist() if n.startswith("xl/charts/")]
    assert charts and scan(raw).sheets[0].nonempty == 5
    padded = patch(raw, {charts[0]: lambda t: t.replace("</chartSpace>", "<extLst/>" * 3000 + "</chartSpace>")})
    monkeypatch.setattr(xlsx_scan, "MAX_PART_ELEMENTS", 2000)
    with pytest.raises(UnsupportedTable, match="元素超过"):
        scan(padded)
