"""一遍流式扫描 .xlsx 的版式元数据：openpyxl read_only 拿不到的，这里补上。

**为什么要自己扫。** 导入走 openpyxl 的 read_only（流式、整数不失真、内存低），但 read_only
没有合并区、隐藏行列、自动筛选、表格对象，`<dimension>` 也可能是陈旧的或者干脆没有；
普通模式能拿到这些，代价是整本工作簿进内存（20 万行约 950 MB）。所以元数据另走一遍：
标准库 zipfile 加 ElementTree.iterparse，按行流式读，读完一行丢一行。

**为什么不信 `<dimension>`。** 它是写文件的程序自己填的：openpyxl 的 write_only 根本不写，
有的程序删了数据也不更新。真实边界只能从非空单元格算——这一遍顺手就算出来了，
导入的第二遍按这个边界读（tabular.load_into）。

**安全。** 上传的文件不可信：
- 解压后的总大小、单个部件的大小、部件个数都设上限（防 zip 炸弹）。只限字节不够：每个字节在
  内存里能放大几十倍，所以工作表按 Excel 自己的上限数行、列、挂在树上的元素和单元格文字长度，
  共享字符串表数条目，openpyxl 整份建树的小部件（样式、文档属性、图表工作表……）数元素和格式条目。
- 压缩包里**每一个**条目（不看扩展名：工作表改名成 .bin，关系文件照样指着它）都先用 expat 解析到
  根元素为止，序言里有 DTD 就拒收。OOXML 从不需要 DTD，有 DTD 只可能是实体膨胀之类的攻击。拦在
  解析器上而不是比字节，是因为字节比较认不出 UTF-16 写的「<!DOCTYPE」。
- 这里和 openpyxl 找部件的办法不同（这里按关系文件，openpyxl 按内容类型清单），两边找到的
  工作簿主部件、共享字符串表必须是同一个；工作表名重复（不区分大小写）拒收。否则扫描看的是
  一份，导入读的是另一份，「隐藏的工作表已跳过」这类回执就不可信了。

这里只读不判：哪些情况拒收、怎么提示，是 tabular 的事。
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from xml.parsers import expat
from contextlib import closing
from dataclasses import asdict, dataclass, field, fields
from typing import IO, Any

#: 解压后的总大小上限。上传本身限 50 MB，正常的 xlsx 压缩比在 5 到 20 倍之间；
#: 超过这个数的基本是 zip 炸弹。可调
MAX_UNCOMPRESSED = 2 * 1024 ** 3
#: 单个 XML 部件（一张工作表、共享字符串表）解压后的上限。可调
MAX_XML_BYTES = 1024 ** 3
#: 工作表、共享字符串表以外的 XML 部件（样式、主题、文档属性、工作簿、关系文件、表格定义……）
#: 解压后的上限。这些部件 openpyxl 一次整份建树（样式每个字节在内存里放大几十倍），正常文件
#: 一般不过 1 MB，样式堆积得很厉害的也在 10 MB 上下
MAX_PART_BYTES = 16 * 1024 ** 2
#: 上述小部件里元素个数的上限：字节上限挡不住「几百万个空元素」这种写法
MAX_PART_ELEMENTS = 400_000
#: 样式表里格式条目（xf）的上限：Excel 自己最多 65,490 种单元格格式，两组（cellStyleXfs、cellXfs）各算一份
MAX_XF = 2 * 65_490
#: 压缩包里条目个数的上限。一千张工作表的工作簿也只有几千个部件
MAX_ENTRIES = 10_000
#: Excel 的行数、列数上限
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_COLS = 16_384
#: Excel 单元格文字、公式长度的上限（字符）
EXCEL_MAX_TEXT = 32_767
EXCEL_MAX_FORMULA = 8_192
#: 扫描一张工作表时，树上同时挂着的元素个数上限。处理完的行清空后行本身的空壳还挂在
#: sheetData 下（只要 end 事件就拿不到父元素、摘不下来），行以外的元素（合并区、筛选、扩展、
#: 写文件的程序塞进来的未知元素）也一直挂着：每个几十到上百字节，几百万个就是几百 MB。
#: 正常的表最多 1,048,576 个行壳加上几万个其他元素，这里再留一百万的余量
MAX_ALIVE = EXCEL_MAX_ROWS + 1_000_000
#: 共享字符串表的条目上限。openpyxl 会把整张表读进一个列表，每条约一百字节
MAX_SHARED_STRINGS = 4_000_000
#: 共享字符串表里一个条目（富文本的若干段）的元素个数上限
MAX_SI_ELEMENTS = 100_000
#: 坐标清单（无缓存值的公式、错误值……）最多记多少个。总数另计，不受这个限制
MAX_CELLS = 50
#: 合并区最多记多少个。合并区上万的文件也见过（整列逐格合并），全记下来只是占内存
MAX_MERGED = 10_000
#: 离群格示例最多几个
MAX_FAR = 10
#: 判「离群」的跳跃幅度：一个非空格比此前见过的最大行号多出这么多行、或比最大列号多出这么多列。
#: 正常表格的行是连着的，隔几千行才冒出来一个格子，多半是误输入或残留格式
FAR_ROW_JUMP = 1000
FAR_COL_JUMP = 50

_CHUNK = 64 * 1024

_MAIN_NS = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "http://purl.oclc.org/ooxml/spreadsheetml/main",          # Strict OOXML
)
#: 工作表里要看的元素：完整标签 → 本地名。两种命名空间都认，一次字典查找就分派
_SHEET_TAGS = {
    f"{{{ns}}}{local}": local
    for ns in _MAIN_NS
    for local in ("row", "c", "v", "f", "is", "mergeCell", "autoFilter", "col", "dimension")
}
_DIGITS = "0123456789"


class UnsupportedTable(ValueError):
    """认不出或读不了。message 直接给用户看。

    定义在这里而不是 tabular：扫描是导入的第一步，同样要能拒收，而 tabular 依赖这个模块。
    对外仍然从 app.data.tabular 导入（那边原样转出）。
    """


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------


@dataclass
class Bounds:
    """真实非空单元格的外接矩形，1 基行列号。"""

    min_row: int
    min_col: int
    max_row: int
    max_col: int

    @property
    def area(self) -> int:
        return (self.max_row - self.min_row + 1) * (self.max_col - self.min_col + 1)

    def a1(self) -> str:
        return f"{cell_ref(self.min_row, self.min_col)}:{cell_ref(self.max_row, self.max_col)}"


@dataclass
class CellList:
    """一类单元格的坐标：前 MAX_CELLS 个的坐标，加总数和最大行号。

    最大行号让调用方不必拿到全部坐标也能精确判断「某行以下有没有」：
    导入区域从表头行一直到边界底部，有没有落在区域里只看 max_row。
    """

    cells: list[str] = field(default_factory=list)
    total: int = 0
    max_row: int = 0
    #: 和 cells 一一对应的原文（目前只有公式类用：公式文本，截断到 80 字）
    texts: list[str] = field(default_factory=list)

    def add(self, row: int, col: int, text: str | None = None) -> None:
        self.total += 1
        if row > self.max_row:
            self.max_row = row
        if len(self.cells) < MAX_CELLS:
            self.cells.append(cell_ref(row, col))
            if text is not None:
                self.texts.append(text[:80])


@dataclass
class SheetScan:
    name: str
    #: visible / hidden / veryHidden
    state: str
    #: 工作表 XML 在压缩包里的路径
    path: str
    #: 真实非空格的边界；整张表没有非空格时是 None
    bounds: Bounds | None = None
    #: 非空格数。有公式的格不算空（即使没有缓存值）；只有空白字符的文本算空
    nonempty: int = 0
    merged: list[str] = field(default_factory=list)
    merged_total: int = 0
    #: 隐藏行、隐藏列，合并成闭区间 (起, 止)。筛选隐藏和手动隐藏在 XML 里分不出来
    hidden_rows: list[tuple[int, int]] = field(default_factory=list)
    hidden_cols: list[tuple[int, int]] = field(default_factory=list)
    autofilter: str | None = None
    formulas: int = 0
    #: 有公式、却没有缓存值的格（程序生成、没用 Excel 保存过的文件）。data_only 读出来是空
    formulas_uncached: CellList = field(default_factory=CellList)
    #: 错误值格（#DIV/0! 之类）
    errors: CellList = field(default_factory=CellList)
    #: 汇总了本格上方连续几行的公式（如 C26 的 SUM(C22:C25)）：表内合计行的信号
    formulas_above: CellList = field(default_factory=CellList)
    #: 离群格示例：稀疏判定拒收时告诉用户是哪几个远处的格子把区域撑大了
    far_cells: list[str] = field(default_factory=list)
    #: 文件自己声明的 <dimension>，只作记录，不用来定边界
    dimension: str | None = None
    #: 每行非空格数的游程：(起始行, 结束行, 每行个数)，只记个数 > 0 的行，按行号升序。
    #: 配方执行器拿它和第二遍（openpyxl）逐行读到的个数对账：两边数的口径一致（见 nonempty），
    #: 对不上就说明两个解析器看到的不是同一张表（XML 行号乱序、重复的行，openpyxl 会静默跳过）
    row_runs: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class TableScan:
    """Excel 的表格对象（插入 → 表格）。ref 连表头带汇总行一起算。"""

    name: str
    ref: str
    sheet: str
    totals_row_count: int = 0
    header_row_count: int = 1
    #: 各列的列名，从左到右
    columns: list[str] = field(default_factory=list)

    def span(self) -> tuple[int, int, int, int] | None:
        """ref 的 (首行, 首列, 末行, 末列)；ref 写坏了是 None。"""
        a, _, b = self.ref.replace("$", "").partition(":")
        try:
            r1, c1 = parse_ref(a)
            r2, c2 = parse_ref(b or a)
        except ValueError:
            return None
        return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)

    def totals_rows(self) -> tuple[int, int] | None:
        """汇总行的行号区间（闭区间）；没有汇总行是 None。"""
        span = self.span()
        if span is None or self.totals_row_count <= 0:
            return None
        return max(span[0], span[2] - self.totals_row_count + 1), span[2]


@dataclass
class WorkbookScan:
    sheets: list[SheetScan] = field(default_factory=list)
    #: calcPr 的 fullCalcOnLoad：打开时全部重算。XlsxWriter 默认这么写，同时把公式缓存值写成 0
    full_calc_on_load: bool = False
    tables: list[TableScan] = field(default_factory=list)
    #: 定义名称 → 原文。工作表级的名称记作「工作表名!名称」，免得同名互相覆盖
    defined_names: dict[str, str] = field(default_factory=dict)

    def sheet(self, name: str) -> SheetScan | None:
        return next((s for s in self.sheets if s.name == name), None)


# --------------------------------------------------------------------------
# 坐标
# --------------------------------------------------------------------------

_COL_CACHE: dict[str, int] = {}


def col_letter(col: int) -> str:
    out = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return out


def cell_ref(row: int, col: int) -> str:
    return f"{col_letter(col)}{row}"


def _col_number(letters: str) -> int:
    hit = _COL_CACHE.get(letters)
    if hit is None:
        hit = 0
        for ch in letters.upper():
            hit = hit * 26 + ord(ch) - 64
        if len(_COL_CACHE) < 20_000:
            _COL_CACHE[letters] = hit
    return hit


def parse_ref(ref: str) -> tuple[int, int]:
    """「AB12」→ (12, 28)。不认 $，调用方给的是单元格的 r 属性，不带 $。"""
    letters = ref.rstrip(_DIGITS)
    return int(ref[len(letters):]), _col_number(letters)


# --------------------------------------------------------------------------
# 读压缩包
# --------------------------------------------------------------------------


class _Budget:
    """整个压缩包解压出来的总字节数。声明的大小先查一遍，读的时候再实数一遍。"""

    def __init__(self) -> None:
        self.used = 0

    def take(self, n: int) -> None:
        self.used += n
        if self.used > MAX_UNCOMPRESSED:
            raise UnsupportedTable(_too_big())


def _too_big() -> str:
    return (f"文件解压后超过 {MAX_UNCOMPRESSED // 1024 ** 2} MB，无法导入。"
            "请删除不需要的工作表，或另存为 .csv 后重新上传")


class _Guarded:
    """包住压缩包里的一个 XML 部件：数着字节读，超过大小上限就停。

    iterparse 只要求 source 有 read(n)。DTD 不在这里查：见 _check_prolog。
    """

    def __init__(self, fp: IO[bytes], part: str, budget: _Budget, limit: int | None = None) -> None:
        self._fp = fp
        self._part = part
        self._budget = budget
        self._seen = 0
        #: 这个部件最多读多少字节。None 表示按 MAX_XML_BYTES（现取，测试里可以改）
        self._limit = limit

    def read(self, n: int = -1) -> bytes:
        data = self._fp.read(n if n and n > 0 else _CHUNK)
        if not data:
            return data
        self._seen += len(data)
        self._budget.take(len(data))
        limit = self._limit if self._limit is not None else MAX_XML_BYTES
        if self._seen > limit:
            raise UnsupportedTable(_part_too_big(self._part, limit))
        return data

    def close(self) -> None:
        self._fp.close()


def _part_too_big(part: str, limit: int) -> str:
    return (f"文件中的「{part}」解压后超过 {limit // 1024 ** 2} MB，无法导入。"
            "请拆分工作表，或另存为 .csv 后重新上传")


def _abnormal(what: str) -> str:
    """文件结构超出 Excel 自己的上限、或者前后矛盾：Excel 不会生成这样的文件。"""
    return f"{what}，不是 Excel 正常保存的文件，已拒绝导入。请在 Excel 中另存为 .xlsx 或 .csv 后重新上传"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve(base_dir: str, target: str) -> str:
    """关系文件里的 Target → 压缩包内路径。openpyxl 写绝对路径（/xl/...），Excel 写相对路径。"""
    if target.startswith("/"):
        return target.lstrip("/")
    out: list[str] = []
    for part in f"{base_dir}/{target}".split("/"):
        if part == "..":
            if out:
                out.pop()
        elif part and part != ".":
            out.append(part)
    return "/".join(out)


class _Archive:
    def __init__(self, raw: bytes) -> None:
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise UnsupportedTable(_not_xlsx()) from e
        infos = self.zip.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UnsupportedTable(_abnormal(f"压缩包中的部件超过 {MAX_ENTRIES:,} 个"))
        self.names = set(self.zip.namelist())
        self.budget = _Budget()
        declared = sum(info.file_size for info in infos)
        if declared > MAX_UNCOMPRESSED:
            raise UnsupportedTable(_too_big())
        #: 已经数过元素个数的小部件（_check_small 只做一次）
        self.checked: set[str] = set()

    def open(self, path: str, limit: int | None = None) -> _Guarded:
        """打开一个部件，读出的字节受 limit 限制（None 表示按 MAX_XML_BYTES）。声明的大小先查一遍。"""
        info = self.zip.getinfo(path)
        cap = limit if limit is not None else MAX_XML_BYTES
        if info.file_size > cap:
            raise UnsupportedTable(_part_too_big(path, cap))
        return _Guarded(self.zip.open(info), path, self.budget, cap)

    def tree(self, path: str) -> ET.Element:
        """小部件（workbook.xml、关系文件、表格定义）整个解析。先按小部件的上限数一遍元素，再建树。"""
        _check_small(self, path)
        parser = ET.XMLParser()
        with closing(self.open(path, MAX_PART_BYTES)) as source:
            while chunk := source.read(_CHUNK):
                parser.feed(chunk)
        return parser.close()

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """部件的关系表：Id → (类型, 解析后的路径)。没有关系文件时是空的。"""
        folder, _, name = part.rpartition("/")
        rels_path = f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"
        if rels_path not in self.names:
            return {}
        out: dict[str, tuple[str, str]] = {}
        for rel in self.tree(rels_path):
            target = rel.get("Target") or ""
            if rel.get("TargetMode") == "External":
                continue
            out[rel.get("Id") or ""] = (rel.get("Type") or "", _resolve(folder, target))
        return out


class _RootReached(Exception):
    """序言读完了（见到了根元素）：_check_prolog 用它提前结束解析。"""


def _no_dtd(*_args: object) -> None:
    raise UnsupportedTable(_DTD_MESSAGE)


def _root_reached(*_args: object) -> None:
    raise _RootReached


_DTD_MESSAGE = "文件中含有 Excel 文件不应包含的内容（DTD 声明），已拒绝导入"
_PROLOG_CHUNK = 4096


def _check_prolog(archive: _Archive, path: str, *, strict: bool = True) -> bool:
    """一个部件的序言里有没有 DTD：有就拒收。返回它是不是 XML（读到了根元素）。

    DOCTYPE 只能出现在根元素之前，所以只解析到根元素的开始标签为止，通常只读几百字节。
    用 expat 解析而不是比字节：编码由 expat 自己认（UTF-8、带或不带 BOM 的 UTF-16……），
    和 ElementTree、openpyxl 用的是同一套规则，「换个编码就绕过」行不通。

    **压缩包里每个条目都查，不看扩展名。** 工作表、共享字符串表的路径来自关系文件和内容类型，
    改名成 sheet1.bin 照样会被这里的扫描和 openpyxl 当 XML 解析，只查 .xml/.rels 就漏了。
    strict（名字是 .xml/.rels）时序言解析不了也拒收：OOXML 规定部件是 UTF-8 或 UTF-16 的合法
    XML，expat 连序言都读不过去的，别的解析器读出来是什么样说不准。其余名字（图片、
    printerSettings、vbaProject.bin）解析不了是正常的，跳过：expat 读不过序言的，ElementTree
    和 openpyxl 也不可能把它当 XML 读出内容。

    唯一的例外：.svg 图片可以带不含内部子集的 DOCTYPE（老版本 Illustrator 导出的 SVG 都带一行
    `<!DOCTYPE svg PUBLIC …>`）。没有内部子集就没有实体定义，expat 也不会去取外部 DTD，
    改名成 .svg 的工作表因此展开不了任何实体；内部子集、实体声明照样拒收。
    """
    svg = path.lower().endswith(".svg")

    def doctype(_name: str, _sysid: str | None, _pubid: str | None, has_internal_subset: int) -> None:
        if has_internal_subset or not svg:
            raise UnsupportedTable(_DTD_MESSAGE)

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = _no_dtd
    parser.StartElementHandler = _root_reached
    # 读序言不受单个部件的大小上限约束（大图片也只读开头几 KB），读到的字节照样计入总量
    with closing(archive.open(path, max(archive.zip.getinfo(path).file_size, 1))) as source:
        try:
            while chunk := source.read(_PROLOG_CHUNK):
                parser.Parse(chunk, False)
        except _RootReached:
            return True
        except (expat.ExpatError, LookupError) as e:
            # LookupError：声明了 Python 不认识的编码，pyexpat 原样抛出
            if strict:
                raise UnsupportedTable(_broken()) from e
            return False
    # 读到结尾也没见到根元素（空部件之类）：没有 DOCTYPE 就不管，读不读得了是真正解析时的事
    return False


def _check_small(archive: _Archive, path: str) -> None:
    """工作表、共享字符串表以外的 XML 部件：字节数和元素个数都设上限，样式里的格式条目单独设上限。

    这些部件 openpyxl 用 lxml 一次整份建树（样式还要给每个格式条目建对象），内存是字节数的
    几十倍；只限字节，几 MB 的「几十万个空元素」就能吃掉几个 GB。这里流式数一遍：
    处理完的元素清空、根下的直接子元素摘掉，数的过程本身不占多少内存。
    """
    if path in archive.checked:
        return
    total = xf = depth = 0
    root: ET.Element | None = None
    with closing(archive.open(path, MAX_PART_BYTES)) as source:
        for event, el in ET.iterparse(source, events=("start", "end")):
            if event == "start":
                depth += 1
                if root is None:
                    root = el
                continue
            depth -= 1
            total += 1
            if total > MAX_PART_ELEMENTS:
                raise UnsupportedTable(_abnormal(f"文件中的「{path}」包含的元素超过 {MAX_PART_ELEMENTS:,} 个"))
            if _local(el.tag) == "xf":
                xf += 1
                if xf > MAX_XF:
                    raise UnsupportedTable(_abnormal(f"样式表中的单元格格式超过 {MAX_XF:,} 种"))
            el.clear()
            if depth == 1 and root is not None:
                root.remove(el)
    archive.checked.add(path)


#: 工作簿主部件的几种内容类型，按 openpyxl 找主部件的顺序（reader/excel.py _find_workbook_part）
_WORKBOOK_TYPES = (
    "application/vnd.ms-excel.template.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
)
_SHARED_STRINGS_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
_CONTENT_TYPES = "[Content_Types].xml"


def _content_types(archive: _Archive) -> tuple[dict[str, list[str]], set[str]]:
    """内容类型清单：(内容类型 → 按出现顺序的部件路径, 默认登记过的内容类型)。"""
    if _CONTENT_TYPES not in archive.names:
        raise UnsupportedTable(_not_xlsx())
    overrides: dict[str, list[str]] = {}
    defaults: set[str] = set()
    for el in archive.tree(_CONTENT_TYPES):
        local = _local(el.tag)
        if local == "Override":
            overrides.setdefault(el.get("ContentType") or "", []).append((el.get("PartName") or "").lstrip("/"))
        elif local == "Default":
            defaults.add(el.get("ContentType") or "")
    return overrides, defaults


def _book_path(archive: _Archive, overrides: dict[str, list[str]], defaults: set[str]) -> str:
    """工作簿主部件的路径。内容类型清单和包关系文件各指一个：openpyxl 按前者找，这里按后者找，
    两边不是同一个就拒收——否则扫描看的是一本工作簿，导入读的是另一本。"""
    claimed = [path for kind in _WORKBOOK_TYPES for path in overrides.get(kind, [])]
    if len(claimed) > 1:
        raise UnsupportedTable(_abnormal("文件中登记了不止一个工作簿主部件"))
    if claimed:
        by_types = claimed[0]
    elif defaults & set(_WORKBOOK_TYPES):
        by_types = "xl/workbook.xml"              # openpyxl 的退路：默认内容类型就是工作簿
    else:
        raise UnsupportedTable(_not_xlsx())
    by_rels = next((path for kind, path in archive.rels("").values() if kind.endswith("/officeDocument")), None)
    if by_rels is not None and by_rels != by_types:
        raise UnsupportedTable(_abnormal("文件中两处登记的工作簿主部件不一致"))
    return by_types


def _shared_strings_path(rels: dict[str, tuple[str, str]], overrides: dict[str, list[str]],
                         archive: _Archive) -> str | None:
    """共享字符串表的路径。openpyxl 按内容类型清单找，这里按工作簿的关系文件找：两边不一致就拒收，
    否则扫描判断「格子空不空」用的是一张表，导入读出来的是另一张。"""
    by_rels = next((path for kind, path in rels.values()
                    if kind.endswith("/sharedStrings") and path in archive.names), None)
    claimed = overrides.get(_SHARED_STRINGS_TYPE, [])
    if len(claimed) > 1 or (claimed and claimed[0] != by_rels):
        raise UnsupportedTable(_abnormal("文件中两处登记的共享字符串表不一致"))
    return by_rels


def _not_xlsx() -> str:
    return ("无法打开该 Excel 文件：文件不是有效的 .xlsx（可能已损坏，或只是修改了扩展名）。"
            "请在 Excel 中另存为 .xlsx 或 .csv 后重新上传")


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def _broken() -> str:
    return "无法打开该 Excel 文件：文件内容不完整或格式不正确。请在 Excel 中另存为 .xlsx 或 .csv 后重新上传"


def scan(raw: bytes) -> WorkbookScan:
    """xlsx 字节 → 版式元数据。读不了就抛 UnsupportedTable（message 给用户看）。"""
    archive = _Archive(raw)
    try:
        return _scan(archive)
    except UnsupportedTable:
        raise
    except (ET.ParseError, KeyError, ValueError, zipfile.BadZipFile, EOFError) as e:
        # KeyError：压缩包里缺部件；ValueError：坐标、数字写坏了；ParseError：XML 不完整
        raise UnsupportedTable(_broken()) from e
    finally:
        archive.zip.close()


def _scan(archive: _Archive) -> WorkbookScan:
    # 先把每个条目的序言查一遍，再开始真正的解析：openpyxl 之后还要读样式、主题这些
    # 这里不读的部件，DTD 的拒收要覆盖到它们（不看扩展名，见 _check_prolog）
    xml_parts = [info for info in archive.zip.infolist() if not info.is_dir() and _check_prolog(
        archive, info.filename, strict=info.filename.lower().endswith((".xml", ".rels")))]
    overrides, defaults = _content_types(archive)
    book_path = _book_path(archive, overrides, defaults)
    book = archive.tree(book_path)
    rels = archive.rels(book_path)
    result = WorkbookScan()

    sst_path = _shared_strings_path(rels, overrides, archive)
    # 工作表和共享字符串表可以很大，按 MAX_XML_BYTES 和逐行的上限管。这里自己整份解析的小部件
    # （内容类型清单、工作簿、关系文件、表格定义）经 _Archive.tree 按小部件管；openpyxl 整份解析、
    # 这里不读的，在这里先查
    for path in _openpyxl_parts(archive, rels, {info.filename for info in xml_parts}):
        _check_small(archive, path)

    sst_flags = _scan_shared_strings(archive, sst_path) if sst_path else None

    sheet_names: list[str] = []
    seen_names: dict[str, str] = {}
    for el in book.iter():
        local = _local(el.tag)
        if local == "calcPr":
            result.full_calc_on_load = el.get("fullCalcOnLoad") in ("1", "true")
        elif local == "sheet":
            rid = next((v for k, v in el.attrib.items() if k.endswith("}id")), None)
            kind, path = rels.get(rid or "", ("", ""))
            name = el.get("name") or ""
            # Excel 的工作表名不区分大小写、不许重复。重名时 openpyxl 按名字取表会拿到第一个
            # （可能是隐藏的那个），导入的内容就和回执说的对不上了
            folded = name.casefold()
            if folded in seen_names:
                raise UnsupportedTable(_abnormal(
                    f"工作簿中有两个工作表同名（「{seen_names[folded]}」与「{name}」，工作表名不区分大小写）"))
            seen_names[folded] = name
            sheet_names.append(name)
            # 图表工作表、对话框工作表没有单元格，openpyxl 的 worksheets 里也不含它们
            if not kind.endswith("/worksheet") or path not in archive.names:
                continue
            sheet = SheetScan(name=name, state=el.get("state") or "visible", path=path)
            # 表格对象先读：扫单元格时要用它的位置认出「销售表[金额]」这种结构化引用汇总了哪几行
            tables = _scan_tables(archive, path, name)
            result.tables.extend(tables)
            _scan_sheet(archive, sheet, sst_flags, {t.name.casefold(): t for t in tables if t.name})
            result.sheets.append(sheet)

    for el in book.iter():
        if _local(el.tag) != "definedName":
            continue
        name = el.get("name") or ""
        local_id = el.get("localSheetId")
        if local_id is not None and local_id.isdigit() and int(local_id) < len(sheet_names):
            name = f"{sheet_names[int(local_id)]}!{name}"
        result.defined_names[name] = (el.text or "").strip()
    return result


#: openpyxl 按固定路径整份解析的部件：文档属性、自定义属性、样式（reader/excel.py、styles/stylesheet.py）
_OPENPYXL_FIXED = ("docProps/core.xml", "docProps/custom.xml", "xl/styles.xml")


def _openpyxl_parts(archive: _Archive, rels: dict[str, tuple[str, str]], xml_names: set[str]) -> list[str]:
    """openpyxl 只读打开时整份建树、而这里的扫描不读的 XML 部件。

    只管真会被解析的：计算链、批注、透视表缓存这些部件可以很大（和公式数、数据行数成正比），
    但没人解析它们，按小部件设限只会误拒正常文件。外部链接 openpyxl 默认也会读，导入时已关掉
    （tabular._open_book 的 keep_links=False）。图表工作表连同它引用的绘图、图表一并算进来。
    """
    out = [path for path in _OPENPYXL_FIXED if path in xml_names]
    todo = [path for kind, path in rels.values() if kind.endswith("/chartsheet") and path in xml_names]
    while todo:
        path = todo.pop()
        if path in out:
            continue
        out.append(path)
        folder, _, name = path.rpartition("/")
        rels_path = f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"
        if rels_path in xml_names:
            out.append(rels_path)
            todo += [dep for _kind, dep in archive.rels(path).values() if dep in xml_names and dep not in out]
    return out


def _scan_tables(archive: _Archive, sheet_path: str, sheet_name: str) -> list[TableScan]:
    out = []
    for tkind, tpath in archive.rels(sheet_path).values():
        if not tkind.endswith("/table") or tpath not in archive.names:
            continue
        t = archive.tree(tpath)
        columns = [c.get("name") or "" for c in t.iter() if _local(c.tag) == "tableColumn"]
        out.append(TableScan(
            name=t.get("displayName") or t.get("name") or "",
            ref=t.get("ref") or "",
            sheet=sheet_name,
            totals_row_count=int(t.get("totalsRowCount") or 0),
            header_row_count=int(t.get("headerRowCount") or 1),
            columns=columns,
        ))
    return out


def _text_info(container: ET.Element) -> tuple[bool, int]:
    """<si> 或 <is> 里的文字：(有没有非空白文字, 总字符数)。直接的 <t>，或者富文本 <r><t>；注音 <rPh> 不算。"""
    nonblank, length = False, 0
    for child in container:
        local = _local(child.tag)
        if local == "t":
            text = child.text
            if text:
                length += len(text)
                nonblank = nonblank or not text.isspace()
        elif local == "r":
            for t in child:
                text = t.text
                if text and _local(t.tag) == "t":
                    length += len(text)
                    nonblank = nonblank or not text.isspace()
    return nonblank, length


def _text_nonblank(container: ET.Element) -> bool:
    """<si> 或 <is> 里有没有非空白文字。"""
    return _text_info(container)[0]


def _scan_shared_strings(archive: _Archive, path: str) -> bytearray:
    """共享字符串表 → 每一项「非空」与否（1/0）。不还原全文：只需要知道引用它的格算不算空。

    条目数、单个条目的文字长度和元素个数都设上限：openpyxl 会把整张表读进一个列表，
    超出 Excel 自己上限的条目只可能是构造出来撑爆内存的。根下的元素处理完就摘掉
    （不只是 <si>：写文件的程序塞进来的未知元素也一样），根下不留空壳。
    """
    flags = bytearray()
    root: ET.Element | None = None
    depth = inner = 0
    with closing(archive.open(path)) as source:
        for event, el in ET.iterparse(source, events=("start", "end")):
            if event == "start":
                depth += 1
                if root is None:
                    root = el
                continue
            depth -= 1
            if depth > 1:
                inner += 1                 # 条目内部的元素：条目结束时随它一起清掉
                if inner > MAX_SI_ELEMENTS:
                    raise UnsupportedTable(_abnormal("共享字符串表中有一个条目包含的元素过多"))
                continue
            if depth == 1:
                inner = 0
                if _local(el.tag) == "si":
                    if len(flags) >= MAX_SHARED_STRINGS:
                        raise UnsupportedTable(_abnormal(f"共享字符串表的条目超过 {MAX_SHARED_STRINGS:,} 条"))
                    nonblank, length = _text_info(el)
                    if length > EXCEL_MAX_TEXT:
                        raise UnsupportedTable(_abnormal(
                            f"共享字符串表中有一项文字超过 Excel 单元格的上限（{EXCEL_MAX_TEXT:,} 个字符）"))
                    flags.append(1 if nonblank else 0)
                el.clear()
                if root is not None:
                    root.remove(el)        # 处理完就摘掉，根下不留空壳
    return flags


#: 公式里的区域引用：C22:C25、$C$22:$C$25。前面紧挨着「!」的是别的工作表，不算
_RANGE_REF = re.compile(
    r"(?<![A-Za-z0-9_.!$'])\$?([A-Za-z]{1,3})\$?(\d{1,7}):\$?([A-Za-z]{1,3})\$?(\d{1,7})(?![A-Za-z0-9_(])",
    re.ASCII,
)
#: 任何像区域的写法（包括别的工作表的）：找单格引用之前先整个抹掉，免得把「B2:B9」的 B9 当成单格
_ANY_RANGE = re.compile(r"\$?[A-Za-z]{1,3}\$?\d{1,7}:\$?[A-Za-z]{1,3}\$?\d{1,7}", re.ASCII)
#: 单格引用：B2、$B$2。边界和 _RANGE_REF 一样；后面跟「(」的是函数名（LOG10(），跟「!」的是工作表名
_CELL_REF = re.compile(r"(?<![\w.!$'\]])\$?([A-Za-z]{1,3})\$?([0-9]{1,7})(?![\w(!:.\[])")
#: 字符串常量和带引号的工作表名：先抹掉，里面的「B2」不是引用。一条正则从左往右扫，谁先开头算谁
_QUOTED = re.compile(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'')
#: 方括号（结构化引用的列名、外部工作簿的编号），最多两层
_BRACKETS = re.compile(r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]")
#: 结构化引用的表名部分：「销售表[」。表名可以是中文
_TABLE_REF = re.compile(r"(?<![\w.!$'\]])([^\W\d][\w.]*)\[")
_SPECIALS = frozenset({"#all", "#data", "#headers", "#totals", "#this row"})


def _aggregates_above(formula: str, row: int, col: int, tables: dict[str, TableScan] | None = None) -> bool:
    """公式是不是汇总了本格正上方的几行。表内合计行（C26 = SUM(C22:C25)）是交叉表、多块报表的典型信号。

    认三种写法：
    - 区域：整个在本行之上、至少两行、列覆盖本格所在列（SUM(C22:C25)、SUBTOTAL(9,D2:D9)）；
    - 逐格相加：单格引用至少两个，而且全都在本格正上方的同一列（B2+B3+B4、SUM(C2,C3,C4)）；
    - 结构化引用：「销售表[金额]」指的明细行全在本行之上、列覆盖本格（tables 给出本工作表的表格对象，
      键是 casefold 后的表名）。只引用表头、汇总行或本行（[#Totals]、[@金额]）的不算。

    逐行算的公式（D3 = B3*C3）、累计（D3 = SUM(C$2:C3)，区域含本行）、滚动余额（C3 = C2+B3，
    引用了别的列）、占比（D5 = C5/SUM(C$5:C$20)）都不满足。
    """
    if ":" not in formula and "[" not in formula:
        letters = col_letter(col)
        if letters not in formula and letters.lower() not in formula:
            return False                  # 快速路径：绝大多数逐行公式根本不引用本列
    if tables and "[" in formula and _structured_above(formula, row, col, tables):
        return True
    text = _BRACKETS.sub("[]", _QUOTED.sub(lambda m: '""' if m.group(0)[0] == '"' else "'_'", formula))
    for m in _RANGE_REF.finditer(text):
        c1, r1, c2, r2 = _col_number(m.group(1)), int(m.group(2)), _col_number(m.group(3)), int(m.group(4))
        if r1 > r2:
            r1, r2 = r2, r1
        if c1 > c2:
            c1, c2 = c2, c1
        if r2 < row and r2 > r1 and c1 <= col <= c2:
            return True
    refs = {(int(m.group(2)), _col_number(m.group(1))) for m in _CELL_REF.finditer(_ANY_RANGE.sub(" ", text))}
    return len(refs) >= 2 and all(c == col and r < row for r, c in refs)


def _bracket(formula: str, start: int) -> str | None:
    """formula[start] 是「[」：返回配对的括号里的内容。「'」转义下一个字符。括号不成对时 None。"""
    depth = 0
    i = start
    while i < len(formula):
        ch = formula[i]
        if ch == "'":
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return formula[start + 1:i]
        i += 1
    return None


def _structured_above(formula: str, row: int, col: int, tables: dict[str, TableScan]) -> bool:
    for m in _TABLE_REF.finditer(formula):
        table = tables.get(m.group(1).casefold())
        span = table.span() if table is not None else None
        content = _bracket(formula, m.end() - 1) if span is not None else None
        if table is None or span is None or content is None:
            continue
        content = content.strip()
        items = re.findall(r"\[((?:[^\]']|'.)*)\]", content) if content.startswith("[") else [content]
        items = [i.strip() for i in items]
        specials = {i.lower() for i in items if i.lower() in _SPECIALS}
        if "#this row" in specials or any(i.startswith("@") for i in items):
            continue                      # 本行：逐行计算
        if specials and not specials & {"#all", "#data"}:
            continue                      # 只引用表头或汇总行
        r1, c1, r2, c2 = span
        data_lo = r1 + max(table.header_row_count, 0)
        data_hi = r2 - max(table.totals_row_count, 0)
        if data_lo > data_hi:
            continue
        bottom = r2 if "#all" in specials or "#totals" in specials else data_hi
        if bottom >= row:
            continue
        lo, hi = c1, c2
        names = [re.sub(r"'(.)", r"\1", i).casefold() for i in items if i and i.lower() not in _SPECIALS]
        known = [n.casefold() for n in table.columns]
        if names and all(n in known for n in names):
            pos = [known.index(n) for n in names]
            lo, hi = c1 + min(pos), c1 + max(pos)
        if lo <= col <= hi:
            return True
    return False


def _descendants(el: ET.Element) -> int:
    return sum(1 for _ in el.iter()) - 1


def _add_span(spans: list[tuple[int, int]], lo: int, hi: int) -> None:
    if spans and spans[-1][1] + 1 >= lo:
        spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
    else:
        spans.append((lo, hi))


def _scan_sheet(archive: _Archive, sheet: SheetScan, sst_flags: bytearray | None,
                tables: dict[str, TableScan] | None = None) -> None:
    """一张工作表流式扫一遍。按 OOXML 的元素顺序，合并区、筛选在 sheetData 之后，所以必须扫到底。

    只要 end 事件，单元格在所在行结束时一起处理（这时行号、各格的子元素都齐了），处理完把行清空。
    要 start 事件的话事件数翻倍，20 万行的表要多花一秒；代价是清空后的行还挂在 sheetData 下，
    每行几十字节，行以外的元素也一直挂着。所以按 Excel 自己的上限设硬上限：行号和行数不超过
    1,048,576、列号不超过 16,384，树上同时挂着的元素不超过 MAX_ALIVE（行清空时，它下面的
    元素随之释放，从计数里减掉）；单元格文字、公式也按 Excel 的长度上限查。超了就拒收：
    正常的文件到不了这些数，到了的只可能是构造出来撑爆内存的（几十 KB 的压缩包能解出几百万个空行）。
    """
    tags = _SHEET_TAGS
    sst_len = len(sst_flags) if sst_flags is not None else 0
    min_row = min_col = 1 << 30
    max_row = max_col = 0
    nonempty = 0
    far: list[str] = []
    max_col_cell = max_row_cell = ""
    cur_row = 0
    hidden_rows = sheet.hidden_rows
    formulas_uncached, formulas_above, errors = sheet.formulas_uncached, sheet.formulas_above, sheet.errors

    alive = rows = 0
    where = f"工作表「{sheet.name}」"
    # 逐行非空格数的游程。行号单调递增（正常文件）时就地合并，O(1)；遇到行号回退或重复（构造的、
    # 或别的程序写乱的 XML）改用 dict 累加，扫完再压缩成游程
    runs = sheet.row_runs
    by_row: dict[int, int] | None = None
    last_filled_row = 0
    # 上限先取到局部变量：每个元素都要比一次，模块属性每次现查要多花时间（测试里照样能改模块常量）
    max_alive, max_rows, max_cols = MAX_ALIVE, EXCEL_MAX_ROWS, EXCEL_MAX_COLS
    max_text, max_formula = EXCEL_MAX_TEXT, EXCEL_MAX_FORMULA

    with closing(archive.open(sheet.path)) as source:
        for _event, el in ET.iterparse(source, events=("end",)):
            alive += 1
            if alive > max_alive:
                raise UnsupportedTable(_abnormal(f"{where}包含的元素过多"))
            name = tags.get(el.tag)
            if name is None or name in ("c", "v", "f", "is"):
                continue
            if name != "row":
                if name == "mergeCell":
                    ref = el.get("ref")
                    if ref:
                        sheet.merged_total += 1
                        if len(sheet.merged) < MAX_MERGED:
                            sheet.merged.append(ref)
                    el.clear()
                elif name == "col":
                    if el.get("hidden") in ("1", "true"):
                        lo, hi = int(el.get("min") or 0), int(el.get("max") or 0)
                        if max(lo, hi) > max_cols:
                            raise UnsupportedTable(_abnormal(f"{where}超过 Excel 的列数上限（{max_cols:,} 列）"))
                        if lo and hi:
                            _add_span(sheet.hidden_cols, lo, hi)
                elif name == "autoFilter":
                    sheet.autofilter = el.get("ref")
                elif name == "dimension":
                    sheet.dimension = el.get("ref")
                continue

            r = el.get("r")
            row = int(r) if r else cur_row + 1
            cur_row = row
            rows += 1
            if row > max_rows or rows > max_rows:
                raise UnsupportedTable(_abnormal(f"{where}超过 Excel 的行数上限（{max_rows:,} 行）"))
            if el.get("hidden") in ("1", "true"):
                _add_span(hidden_rows, row, row)
            col = 0
            #: 这一行清空时随之释放的元素个数（行本身的空壳留着，不算）
            freed = 0
            row_filled = 0
            for cell in el:
                freed += 1
                if tags.get(cell.tag) != "c":
                    if len(cell):
                        freed += _descendants(cell)
                    continue
                ref = cell.get("r")
                if ref:
                    letters = ref.rstrip(_DIGITS)
                    col = _col_number(letters)
                else:
                    col += 1
                if col > max_cols:
                    raise UnsupportedTable(_abnormal(f"{where}超过 Excel 的列数上限（{max_cols:,} 列）"))
                ctype = cell.get("t") or "n"
                v_el = f_el = is_el = None
                inline_filled, inline_len = False, 0
                for child in cell:
                    freed += 1
                    cname = tags.get(child.tag)
                    if cname == "v":
                        v_el = child
                    elif cname == "f":
                        f_el = child
                    else:
                        # <is> 下的文字、未知元素下的子孙也随行释放。<v>、<f> 正常没有子元素：
                        # 构造出来的子元素不减，只会让计数偏大、更早拒收
                        if len(child):
                            freed += _descendants(child)
                        if cname == "is":
                            is_el = child
                            inline_filled, inline_len = _text_info(child)
                vtext = v_el.text if v_el is not None else None
                if inline_len > max_text or (vtext is not None and len(vtext) > max_text):
                    raise UnsupportedTable(_abnormal(
                        f"{where}的单元格 {cell_ref(row, col)} 中的文字超过 Excel 的上限（{max_text:,} 个字符）"))
                if f_el is not None and f_el.text and len(f_el.text) > max_formula:
                    raise UnsupportedTable(_abnormal(
                        f"{where}的单元格 {cell_ref(row, col)} 中的公式超过 Excel 的上限（{max_formula:,} 个字符）"))
                if ctype == "s":
                    if vtext:
                        idx = int(vtext)
                        filled = sst_flags[idx] == 1 if 0 <= idx < sst_len and sst_flags is not None else True
                    else:
                        filled = False
                elif ctype == "inlineStr":
                    filled = inline_filled
                elif ctype == "str":
                    filled = bool(vtext and vtext.strip())
                else:
                    filled = bool(vtext)
                if ctype == "e" and vtext:
                    errors.add(row, col)
                if f_el is not None:
                    sheet.formulas += 1
                    # 公式返回空字符串时 Excel 写 t="str" 加空的 <v>：那是有缓存的空值，不算「无缓存」
                    if v_el is None or (not vtext and ctype != "str"):
                        formulas_uncached.add(row, col)
                    ftext = f_el.text
                    if ftext and _aggregates_above(ftext, row, col, tables):
                        formulas_above.add(row, col, ftext)
                    filled = True          # 有公式的格不算空，哪怕缓存值是空的
                if not filled:
                    continue
                nonempty += 1
                row_filled += 1
                if row < min_row:
                    min_row = row
                if col < min_col:
                    min_col = col
                # 同一个格子可能既往右又往下跳得很远（XFD1048576）：只记一次
                jumped = False
                if col > max_col:
                    jumped = bool(max_col) and col - max_col > FAR_COL_JUMP
                    max_col = col
                    max_col_cell = cell_ref(row, col)
                if row > max_row:
                    jumped = jumped or (bool(max_row) and row - max_row > FAR_ROW_JUMP)
                    max_row = row
                    max_row_cell = cell_ref(row, col)
                if jumped and len(far) < MAX_FAR:
                    far.append(cell_ref(row, col))
            if row_filled:
                if by_row is None and row > last_filled_row:
                    if runs and runs[-1][1] + 1 == row and runs[-1][2] == row_filled:
                        runs[-1] = (runs[-1][0], row, row_filled)
                    else:
                        runs.append((row, row, row_filled))
                    last_filled_row = row
                else:
                    if by_row is None:
                        by_row = {}
                        for lo, hi, n in runs:
                            for rr in range(lo, hi + 1):
                                by_row[rr] = n
                    by_row[row] = by_row.get(row, 0) + row_filled
            el.clear()
            alive -= freed

    if by_row is not None:
        runs.clear()
        for rr in sorted(by_row):
            n = by_row[rr]
            if runs and runs[-1][1] + 1 == rr and runs[-1][2] == n:
                runs[-1] = (runs[-1][0], rr, n)
            else:
                runs.append((rr, rr, n))
    sheet.nonempty = nonempty
    if nonempty:
        sheet.bounds = Bounds(min_row, min_col, max_row, max_col)
        # 没有明显的跳跃（比如区域是慢慢变稀的），就拿撑出右边界、下边界的格子当示例
        sheet.far_cells = far or list(dict.fromkeys(c for c in (max_col_cell, max_row_cell) if c))


# --------------------------------------------------------------------------
# 序列化：暂存区存扫描结果，试运行不必再扫一遍（配方导入，P2-SPEC 4.1）
# --------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """asdict 的结果里元组照样是元组：转成列表，返回值和 json 往返一次之后逐字相等。"""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def scan_to_json(scan: WorkbookScan) -> dict[str, Any]:
    """WorkbookScan → 可以 json.dumps 的 dict（dataclasses.asdict 的形状，元组写成列表）。"""
    return _jsonable(asdict(scan))


def _pairs(items: Any, width: int) -> list[tuple[int, ...]]:
    out = []
    for item in items or []:
        seq = tuple(int(x) for x in item)
        if len(seq) != width:
            raise ValueError(f"扫描结果里的区间应有 {width} 项：{item!r}")
        out.append(seq)
    return out


def _cell_list(data: Any) -> CellList:
    data = data or {}
    return CellList(cells=list(data.get("cells") or []), total=int(data.get("total") or 0),
                    max_row=int(data.get("max_row") or 0), texts=list(data.get("texts") or []))


def _known(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """只取 cls 认识的键：老版本存下的 JSON 少了新字段时取默认值，多出的键（更新的版本写的）忽略。"""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def scan_from_json(data: dict[str, Any]) -> WorkbookScan:
    """scan_to_json 的逆操作。字段缺了取默认值（老暂存区里没有 row_runs），多了忽略。"""
    sheets = []
    for raw in data.get("sheets") or []:
        d = _known(SheetScan, dict(raw))
        bounds = d.get("bounds")
        sheets.append(SheetScan(
            name=str(d["name"]), state=str(d.get("state") or "visible"), path=str(d.get("path") or ""),
            bounds=Bounds(**_known(Bounds, bounds)) if bounds else None,
            nonempty=int(d.get("nonempty") or 0),
            merged=list(d.get("merged") or []), merged_total=int(d.get("merged_total") or 0),
            hidden_rows=_pairs(d.get("hidden_rows"), 2), hidden_cols=_pairs(d.get("hidden_cols"), 2),
            autofilter=d.get("autofilter"), formulas=int(d.get("formulas") or 0),
            formulas_uncached=_cell_list(d.get("formulas_uncached")), errors=_cell_list(d.get("errors")),
            formulas_above=_cell_list(d.get("formulas_above")), far_cells=list(d.get("far_cells") or []),
            dimension=d.get("dimension"), row_runs=_pairs(d.get("row_runs"), 3),
        ))
    tables = [TableScan(**_known(TableScan, dict(t))) for t in data.get("tables") or []]
    for t in tables:
        t.columns = list(t.columns)
    return WorkbookScan(sheets=sheets, full_calc_on_load=bool(data.get("full_calc_on_load")), tables=tables,
                        defined_names=dict(data.get("defined_names") or {}))
