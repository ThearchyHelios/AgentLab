"""把 Excel / CSV 变成一个可以用 SQL 查的 SQLite 库。

**为什么不走知识库那条路。** 这个项目有一条不可回退的决策：叙述层无算术权限，
所有算术下沉到 SQL 或口径卡，叙述只能引用（见 engine/issuance.py）。表格数据
传进知识库只能被切块检索，于是数字是模型从片段里"读"出来的——复核层
（engine/review.py）拦得住凭空多出的数字，拦不住"从一堆片段里读错了一格"。
所以表格必须变成表。

**为什么落成 SQLite 而不是新造一种数据源。** kind='sqlite' 早就支持，
engine.build_url 拿 database 当文件路径。落成 .db 文件再建一条 DataSource，
下游一行都不用改：SQL 守卫、结构探查、db_query__<name> 工具、查询快照进
工件库、Copilot 的数据源清单，全都白拿。

**建表必须用这里的直连，不能走 data.engine.run_query。** guard 的
_ALWAYS_DENIED 里有 create，那是对的——模型不该能建表。而导入是我们自己
发起的可信操作，两件事不该共用一条通道。

**不许静默出错。** 以前这里有十几处会把数据悄悄读错：前导零丢掉、19 位卡号变成浮点、
千分位文本求和只取前缀、日期带上 T00:00:00 查不到、隐藏工作表里的口令进了库、
空行和合计行灌进 COUNT、数字列混进一个「N/A」就整列变成 TEXT 按字典序比大小。
现在每一种取舍要么由用户显式选择（NeedsDecision），要么写进导入回执（LoadReport），
读不了的整份拒收（UnsupportedTable）。

读法是两遍：xlsx_scan 先流式扫出真实边界和版式元数据，再用 openpyxl read_only 按边界读值。
值也是两遍：先取前 _TYPE_SAMPLE 行推断类型，再流式写入，写的同时统计整列；整列的结论和样本
不一致（样本没看到的前导零、小数、占位符），就按整列的结论重写这张表。
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import itertools
import math
import re
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import describe_exception
from app.data import xlsx_scan
from app.data.names import NAME_MAX, collide_key, to_sql_name
from app.data.xlsx_scan import SheetScan, TableScan, UnsupportedTable, WorkbookScan, cell_ref, col_letter

__all__ = [
    "CONVERSION_KINDS", "SHAPE_KINDS", "UNSHAPED_NOTE", "LoadReport", "LoadedTable", "NeedsDecision",
    "UnsupportedTable", "infer_type", "load_into",
]

#: 按原样导入（未规整）的表写进表结构的说明（data/table_versions.py 注入 schema_cache 的 comment）。
#: 不含数字和坐标：它会进模型看得到的文本（工具描述、db_schema、写作目录、裁判摘录，H11）。
#: 放在这里而不是 table_versions：证据层、裁判、数据源工具按它认出未规整的表，不必为一句话去导入版本底座
UNSHAPED_NOTE = "本表按原样导入、未经规整：同一列里混有不同口径的行，不能直接对列求和"

#: 先看多少行来推断列类型。整列的统计在写入时另做，样本看错了会按整列的结论重写，
#: 所以这个数只影响速度，不影响结果
_TYPE_SAMPLE = 5000
#: 稀疏判定：边界面积超过 非空格数（至少按 SPARSE_FLOOR 算）的 SPARSE_FACTOR 倍就拒收。
#: 阈值是推测：两百万格的稠密列表和普通交叉表都远在线下，一个误输入在 XFD 列的格子会让
#: 一张几百行的表撑成几千万格
SPARSE_FACTOR = 50
SPARSE_FLOOR = 20_000
#: 「数字列混入非数字」：非空值里数字占比不低于这个数……
MIXED_MIN_SHARE = 0.8
#: ……且非数字的不同取值不超过这么多种（占位符、错误值、「N/A」之类）。再多就是真的文本列
MIXED_MAX_DISTINCT = 5
#: 表头行里有这么多个日期样式的格子，就当作日期横排的交叉表
DATE_HEADER_MIN = 7
#: 超过这么多位的数字文本（卡号、订单号）按文本存：REAL 只有 15 到 17 位有效数字，会撞值
LONG_DIGITS = 15
#: SQLite 一张表最多 2000 列（SQLITE_MAX_COLUMN 的默认值）。超了 CREATE TABLE 会报一句英文
MAX_COLUMNS = 2000

_CSV = {".csv", ".tsv"}
_EXCEL = {".xlsx", ".xlsm"}
_LEGACY_EXCEL = {".xls"}

#: Excel 的错误值。openpyxl 的 data_only 把它们读成这些字符串
EXCEL_ERRORS = frozenset({
    "#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A", "#GETTING_DATA",
    "#SPILL!", "#CALC!", "#FIELD!", "#BLOCKED!", "#UNKNOWN!", "#CONNECT!", "#BUSY!", "#PYTHON!",
})


class NeedsDecision(UnsupportedTable):
    """要用户拍板才能继续导入。

    kind 是 "mixed"（数字列混入非数字：存空值，还是不导入）或 "shape"（交叉表、多块结构：
    按原样导入，还是不导入）；details 可以 JSON 化，交给界面逐条展示；str(e) 是给人看的一句话。
    继承 UnsupportedTable：不认识这个类的老调用方会把它当成一次拒收，不会误导入。
    """

    def __init__(self, message: str, *, kind: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details


@dataclass
class LoadedTable:
    name: str
    #: 原始工作表名（CSV 是文件名）。清洗成表名之后，用户要能对上是哪一张
    sheet: str
    #: [{name, type, header}]：SQL 列名、类型、原表头
    columns: list[dict[str, str]]
    rows: int
    #: 导入区域（A1，表头行到最后一行数据）
    region: str = ""
    blank_rows_skipped: int = 0
    #: 去掉的左右两侧整列全空的列（列字母）
    columns_trimmed: list[str] = field(default_factory=list)
    #: 按原样导入、未经规整（交叉表、多块结构）
    unshaped: bool = False

    @property
    def source_name(self) -> str:
        """旧字段名，给还没改过来的调用方。"""
        return self.sheet

    @property
    def types(self) -> list[str]:
        return [c["type"] for c in self.columns]

    @property
    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]


@dataclass
class LoadReport:
    tables: list[LoadedTable] = field(default_factory=list)
    #: [{sheet, state, reason}]：reason 是 hidden（隐藏工作表不导入）或 empty（没有内容）
    skipped_sheets: list[dict[str, str]] = field(default_factory=list)
    #: [{table, column, kind, count, examples}]，kind 见 CONVERSION_KINDS
    conversions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)


#: 回执里 conversions 的 kind 和它的说法（给界面和日志对照用）
CONVERSION_KINDS = {
    "thousands_separator": "千分位文本按数字存",
    "nonnumeric_to_null": "非数字的值存为空值",
    "kept_as_text": "前导零或超长数字，整列按文本存",
}


def _ext(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""


# --------------------------------------------------------------------------
# 值的分类
# --------------------------------------------------------------------------

_EMPTY, _INT, _FLOAT, _DATE, _KEEP, _TEXT = range(6)

# 只认 ASCII 数字（re.ASCII）：Python 的 \d 默认连全角「１２３」、阿拉伯-印度数字都算，
# 那样会把全角写的编号悄悄转成数字。全角数字按文本，进了数字列就交给用户拍板
_INT_TEXT = re.compile(r"-?\d+", re.ASCII)
_DEC_TEXT = re.compile(r"-?\d+\.\d+", re.ASCII)
#: 千分位：首组 1 到 3 位且不以 0 开头（「0,250」「00,123」不是千分位写法，多半是逗号当小数点）
_THOUSANDS = re.compile(r"-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?", re.ASCII)
#: 逗号当小数点的写法（「0,250」「1,5」「1.234,56」）。认不准逗号是什么意思，按文本存，回执里写明
_COMMA_NUMBER = re.compile(r"-?\d+(?:\.\d{3})*,\d+", re.ASCII)
_DATE_TEXT = re.compile(r"\d{1,2}月\d{1,2}日|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?", re.ASCII)
#: 只写了月日、没有年份的日期文本（「8月1日」）
_MONTH_DAY = re.compile(r"\d{1,2}月\d{1,2}日", re.ASCII)
#: 带百分号、单位或货币符号的数字（「15%」「1,234元」「¥1234」「12kg」「50万」）。按文本存——百分比是 0.15
#: 还是 15、「万」要不要乘上去，口径要人来定——但 SQLite 对这种文本 SUM 只取开头的数字（「1,234元」算成 1，
#: 「¥1234」算成 0），MAX 按字典序取，所以回执里要写明（_units_in_text）。单位只认汉字（不以年月日号时点开头：
#: 「2026年」「8月」「3号」「18时」是日期时间，不是数量）和几个常见的英文单位；「12A」这类编号不算
_UNIT_NUMBER = re.compile(
    r"(?P<pre>[¥￥$€£₩₹])?\s?-?(?P<pre2>[¥￥$€£₩₹])?\s?"
    r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
    r"\s?(?P<suf>[%‰％]|[\u4e00-\u9fff]{1,4}|(?i:kg|g|mg|t|km|m|cm|mm|ml|l|kwh|kw|w|h|min|pcs|pc|usd|rmb|cny))?"
)
_NOT_UNIT_HEAD = frozenset("年月日号时点")
#: 带单位的数字能以哪些字符开头（先挡掉绝大多数普通文本，不必每格都跑一遍正则）
_UNIT_LEAD = frozenset("0123456789-¥￥$€£₩₹")


def _fmt_float(value: float) -> str:
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _fmt_datetime(value: _dt.datetime) -> str:
    """日期型：零点的只留日期，带时间的用空格分隔（不是 T）。

    `WHERE 日期 = '2026-01-05'` 要能查到；存成 2026-01-05T00:00:00 的话一行都查不到，
    BETWEEN 还会漏掉月末那天。
    """
    if value.hour == value.minute == value.second == value.microsecond == 0:
        return value.strftime("%Y-%m-%d")
    text = value.strftime("%Y-%m-%d %H:%M:%S")
    if value.microsecond:
        text += f".{value.microsecond // 1000:03d}"
    return text


def _fmt_timedelta(value: _dt.timedelta) -> str:
    seconds = int(value.total_seconds())
    sign = "-" if seconds < 0 else ""
    hours, rest = divmod(abs(seconds), 3600)
    return f"{sign}{hours}:{rest // 60:02d}:{rest % 60:02d}"


def _classify_text(text: str, thousands: bool = True) -> tuple[int, Any, str | None]:
    if text in EXCEL_ERRORS:
        return _TEXT, text, "error"
    if _INT_TEXT.fullmatch(text):
        body = text.lstrip("-")
        # 前导零是编码（"00123"），超过 15 位是卡号订单号：按数字存就把它们毁了
        if (len(body) > 1 and body[0] == "0") or len(body) > LONG_DIGITS:
            return _KEEP, text, None
        return _INT, int(text), None
    if _DEC_TEXT.fullmatch(text):
        body = text.lstrip("-")
        head = body.split(".", 1)[0]
        if (len(head) > 1 and head[0] == "0") or len(body) - 1 > LONG_DIGITS:
            return _KEEP, text, None
        return _FLOAT, float(text), None
    if "," in text:
        if _THOUSANDS.fullmatch(text):
            # 分号分隔的 CSV（欧洲的区域设置）里「1,500」可能是 1.5：不转，和「0,250」一样按文本、记下来
            if not thousands:
                return _TEXT, text, "comma"
            plain = text.replace(",", "")
            if len(plain.lstrip("-").replace(".", "")) > LONG_DIGITS:
                return _KEEP, text, None
            if "." in plain:
                return _FLOAT, float(plain), "thousands"
            return _INT, int(plain), "thousands"
        if _COMMA_NUMBER.fullmatch(text):
            return _TEXT, text, "comma"
    # "1E5"、"+86"、"12%"、全角数字都不当数字：编码要原样，百分比的口径要人来定。
    # 带百分号、单位、货币符号的另做记号，回执里要说（见 _UNIT_NUMBER）
    if text[0] in _UNIT_LEAD and (m := _UNIT_NUMBER.fullmatch(text)) is not None:
        suffix = m.group("suf")
        if (m.group("pre") or m.group("pre2") or suffix) and not (suffix and suffix[0] in _NOT_UNIT_HEAD):
            return _TEXT, text, "unit"
    return _TEXT, text, None



_EMPTY_C: tuple[int, Any, str | None] = (_EMPTY, None, None)
_LONG_INT = 10 ** LONG_DIGITS


def _classify(value: Any, thousands: bool = True) -> tuple[int, Any, str | None]:
    """单元格 → (类别, 载荷, 备注)。载荷：数字类是数值，其余是要存的文本。

    thousands=False 时千分位文本不转数字（分号分隔的 CSV：逗号可能是小数点）。

    空字符串和 NULL 必须分清楚：存成 "" 的话，一个本该是数字的列就被一个
    空单元格拖成了 TEXT，然后 SUM() 静默地不再是你以为的那个 SUM()。

    最常见的 int、float、str 先按确切类型分派：每个单元格都要走一遍，几十万行时这点差别看得出来。
    """
    kind = type(value)
    if kind is str:
        text = value.strip()
        return _classify_text(text, thousands) if text else _EMPTY_C
    if kind is int:
        # 超过 15 位的整数（程序写进去的卡号）按文本存：Excel 自己存不下这么多位，进了 REAL 会撞值
        return (_INT, value, None) if -_LONG_INT < value < _LONG_INT else (_KEEP, str(value), None)
    if kind is float:
        if math.isnan(value) or math.isinf(value):
            return _TEXT, repr(value), None
        return _FLOAT, value, None
    if value is None:
        return _EMPTY_C
    if isinstance(value, bool):
        # bool 是 int 的子类，但"真/假"不是数量，不该被当成数字去求和
        return _TEXT, "TRUE" if value else "FALSE", None
    if isinstance(value, _dt.datetime):
        return _DATE, _fmt_datetime(value), None
    if isinstance(value, _dt.date):
        return _DATE, value.isoformat(), None
    if isinstance(value, _dt.time):
        return _TEXT, value.strftime("%H:%M:%S"), None
    if isinstance(value, _dt.timedelta):
        return _TEXT, _fmt_timedelta(value), None
    if isinstance(value, str):
        return _classify(str(value), thousands)
    if isinstance(value, int):
        return _classify(int(value))
    if isinstance(value, float):
        return _classify(float(value))
    return _TEXT, str(value), None


def _as_text(value: Any, cls: int, payload: Any) -> str:
    """要存进 TEXT 列的写法：文本原样（去首尾空白），原生数字按常规写法。"""
    if isinstance(value, str):
        return value.strip()
    if cls in (_DATE, _TEXT, _KEEP):
        return payload
    if isinstance(value, float):
        return _fmt_float(value)
    return str(value)


def _is_date_like(value: Any) -> bool:
    return _date_like(_classify(value))


def _date_like(parsed: tuple[int, Any, str | None]) -> bool:
    """日期样式：日期型，或者「8月1日」「2026-08-01」这类写法的文本。"""
    cls, payload, note = parsed
    if cls == _DATE:
        return True
    return cls == _TEXT and note is None and payload[:1].isdigit() and _DATE_TEXT.fullmatch(payload) is not None


def _display(value: Any) -> str:
    """表头和提示里展示一个单元格的写法。"""
    if value is None:
        return ""
    cls, payload, _ = _classify(value)
    return _as_text(value, cls, payload) if cls != _EMPTY else ""


class _ColStats:
    """一列的分类计数。只记数和少量示例，不留值：几十万行也只占几百字节。"""

    __slots__ = ("nonempty", "ints", "floats", "dates", "keep", "texts", "thousands",
                 "thousands_ex", "keep_ex", "errors", "errors_ex", "commas", "commas_ex", "units", "units_ex",
                 "other", "overflow")

    def __init__(self) -> None:
        self.nonempty = self.ints = self.floats = self.dates = self.keep = self.texts = 0
        self.thousands = self.errors = self.commas = self.units = 0
        self.thousands_ex: list[str] = []
        self.keep_ex: list[str] = []
        self.errors_ex: list[str] = []
        #: 逗号当小数点的写法（「0,250」）：按文本存，回执里要说
        self.commas_ex: list[str] = []
        #: 带百分号、单位、货币符号的数字（「15%」「1,234元」）：按文本存，回执里要说
        self.units_ex: list[str] = []
        #: 非数字的取值 → 个数，最多记 MIXED_MAX_DISTINCT 种；再多就只记「超了」
        self.other: dict[str, int] = {}
        self.overflow = False

    def add(self, parsed: tuple[int, Any, str | None], raw: Any) -> None:
        cls, payload, note = parsed
        self.nonempty += 1
        if cls == _INT:
            self.ints += 1
        elif cls == _FLOAT:
            self.floats += 1
        elif cls == _KEEP:
            self.keep += 1
            if len(self.keep_ex) < 3:
                self.keep_ex.append(payload)
            return
        else:
            if cls == _DATE:
                self.dates += 1
            else:
                self.texts += 1
                if note == "error":
                    self.errors += 1
                    if len(self.errors_ex) < 3 and payload not in self.errors_ex:
                        self.errors_ex.append(payload)
                elif note == "comma":
                    self.commas += 1
                    if len(self.commas_ex) < 3 and payload not in self.commas_ex:
                        self.commas_ex.append(payload)
                elif note == "unit":
                    self.units += 1
                    if len(self.units_ex) < 3 and payload not in self.units_ex:
                        self.units_ex.append(payload)
            if payload in self.other:
                self.other[payload] += 1
            elif len(self.other) < MIXED_MAX_DISTINCT:
                self.other[payload] = 1
            else:
                self.overflow = True
            return
        if note == "thousands":
            self.thousands += 1
            if len(self.thousands_ex) < 3:
                self.thousands_ex.append(str(raw).strip())

    @property
    def numeric(self) -> int:
        return self.ints + self.floats


def _decide(st: _ColStats) -> tuple[str, bool]:
    """一列的 (SQLite 类型, 是不是「数字列混入非数字」)。

    SQLite 是动态类型，但列**亲和性**决定比较和排序的行为：整数列存成 TEXT，
    ORDER BY 就变成字典序，'10' 排在 '9' 前面；而这正是"Excel 里的数字能不能
    真的被 SQL 算"的分界线。

    日期统一成 TEXT——SQLite 没有日期类型，而 ISO 格式的字典序恰好等于时间序。
    """
    n = st.nonempty
    if n == 0 or st.keep:
        return "TEXT", False
    num = st.numeric
    if num == 0:
        return "TEXT", False
    numeric_type = "REAL" if st.floats else "INTEGER"
    if num == n:
        return numeric_type, False
    if num >= MIXED_MIN_SHARE * n and not st.overflow:
        return numeric_type, True
    return "TEXT", False


def infer_type(values: Sequence[Any]) -> str:
    """一列值的 SQLite 类型。

    「数字列混入非数字」（数字占八成以上、非数字不超过五种）返回数字类型：那种列导入时要么
    把非数字存空值、按数字存，要么不导入（load_into 的 mixed 参数），不会落成 TEXT。
    """
    st = _ColStats()
    for value in values:
        parsed = _classify(value)
        if parsed[0] != _EMPTY:
            st.add(parsed, value)
    return _decide(st)[0]


# --------------------------------------------------------------------------
# 一张表的行来源
# --------------------------------------------------------------------------


@dataclass
class _Source:
    """一张要导入的表：从表头行开始逐行给值，可以从头再读一遍（两遍都按边界读）。"""

    sheet: str
    rows: Callable[[], Iterator[Sequence[Any]]]
    #: 表头所在的行号（Excel 行号；CSV 是第几条记录）
    first_row: int
    #: 每行第一个值所在的列号
    first_col: int
    scan: SheetScan | None = None
    full_calc_on_load: bool = False
    #: 本工作表上的 Excel 表格对象（插入 → 表格）
    tables: list[TableScan] = field(default_factory=list)
    #: 千分位文本按数字存。分号分隔的 CSV 关掉：那种文件里逗号多半是小数点
    thousands: bool = True
    #: 来自 CSV / TSV：没有工作表，提示里说「文件「…」」
    csv: bool = False

    @property
    def where(self) -> str:
        """提示里指这张表的说法：工作表「Sheet1」，CSV 是文件「订单」。"""
        return f"{'文件' if self.csv else '工作表'}「{self.sheet}」"


@dataclass
class _Plan:
    """怎么建这张表：保留哪几列（读取矩形里的下标 [lo, hi)），每列的类型。"""

    lo: int
    hi: int
    types: list[str]
    mixed: list[bool]

    def key(self) -> tuple[int, int, tuple[str, ...]]:
        return self.lo, self.hi, tuple(self.types)


class _Observer:
    """逐行看值：每列的分类计数、空行、分段标题和日期行的候选。两遍各用一个。"""

    #: 候选最多记多少个。正常的表一个都没有；记满了说明根本不是列表，不用再记
    MAX_CANDIDATES = 200

    def __init__(self, first_row: int, thousands: bool = True) -> None:
        self.thousands = thousands
        self.stats: list[_ColStats] = []
        self.row_no = first_row          # 当前行号；表头行之后从 first_row + 1 起
        self.data_rows = 0
        self.blank_rows = 0
        self.first_data_row = 0
        self.last_data_row = 0
        #: (行号, 列下标, 文字)：整行只有一个非空格、而且是文字。最多记 MAX_CANDIDATES 个，总数另计
        self.single_text: list[tuple[int, int, str]] = []
        self.single_text_total = 0
        #: (行号, 日期格的列下标)：一行里日期样式的格子达到 DATE_HEADER_MIN
        self.date_rows: list[tuple[int, tuple[int, ...]]] = []
        self._pending_date: tuple[int, tuple[int, ...]] | None = None

    def feed(self, row: Sequence[Any]) -> list[tuple[int, Any, str | None]] | None:
        """看一行。空行返回 None，否则返回每格的分类（写入时直接用）。"""
        self.row_no += 1
        stats = self.stats
        n = len(row)
        if len(stats) < n:
            stats.extend(_ColStats() for _ in range(n - len(stats)))
        parsed = [_EMPTY_C] * n
        filled = 0
        first = -1
        thousands = self.thousands
        for i, value in enumerate(row):
            if value is None:
                continue
            item = _classify(value, thousands)
            if item[0] == _EMPTY:
                continue
            parsed[i] = item
            stats[i].add(item, value)
            filled += 1
            if first < 0:
                first = i
        if not filled:
            self.blank_rows += 1
            return None
        self.data_rows += 1
        if not self.first_data_row:
            self.first_data_row = self.row_no
        self.last_data_row = self.row_no

        if filled == 1 and parsed[first][0] == _TEXT and parsed[first][2] is None:
            self.single_text_total += 1
            if len(self.single_text) < self.MAX_CANDIDATES:
                self.single_text.append((self.row_no, first, parsed[first][1]))

        dates = 0
        if filled >= DATE_HEADER_MIN:
            idx = [i for i, item in enumerate(parsed) if item[0] != _EMPTY and _date_like(item)]
            dates = len(idx)
        # 日期行要和下一行比：下一行也全是日期，那是一张有很多日期列的明细表，不是横排的表头
        if self._pending_date is not None:
            if dates < DATE_HEADER_MIN and len(self.date_rows) < self.MAX_CANDIDATES:
                self.date_rows.append(self._pending_date)
            self._pending_date = None
        if dates >= DATE_HEADER_MIN:
            self._pending_date = (self.row_no, tuple(idx))
        return parsed

    def width(self) -> int:
        return len(self.stats)


def _plan(header: Sequence[Any], obs: _Observer) -> _Plan | None:
    """按观察到的值定下保留哪些列、每列什么类型。左右两侧表头和值都空的列去掉。"""
    width = max(len(header), obs.width())
    used = [
        (i < len(header) and _display(header[i]) != "") or (i < len(obs.stats) and obs.stats[i].nonempty > 0)
        for i in range(width)
    ]
    if not any(used):
        return None
    lo = used.index(True)
    hi = width - used[::-1].index(True)
    types, mixed = [], []
    for i in range(lo, hi):
        sql_type, is_mixed = _decide(obs.stats[i]) if i < len(obs.stats) else ("TEXT", False)
        types.append(sql_type)
        mixed.append(is_mixed)
    return _Plan(lo, hi, types, mixed)


def _unique(base: str, taken: set[str]) -> str:
    """撞名（按 collide_key，比 SQLite 严）就加 _2、_3……，加完仍不超过名字上限。"""
    name, n = base, 2
    while collide_key(name) in taken:
        suffix = f"_{n}"
        name = base[: NAME_MAX - len(suffix)].rstrip("_") + suffix
        n += 1
    taken.add(collide_key(name))
    return name


def _column_names(header: Sequence[Any], plan: _Plan) -> list[tuple[str, str]]:
    """[(SQL 列名, 原表头)]。列名满足 names.name_problem；原表头另存作显示名。"""
    taken: set[str] = set()
    out = []
    for pos, i in enumerate(range(plan.lo, plan.hi), start=1):
        raw = _display(header[i]) if i < len(header) else ""
        out.append((_unique(to_sql_name(raw, fallback=f"col_{pos}"), taken), raw))
    return out


def _to_integer(value: Any, item: tuple[int, Any, str | None]) -> int | None:
    return item[1] if item[0] == _INT else None


def _to_real(value: Any, item: tuple[int, Any, str | None]) -> float | None:
    cls = item[0]
    return float(item[1]) if cls == _INT or cls == _FLOAT else None


def _to_text(value: Any, item: tuple[int, Any, str | None]) -> str | None:
    return None if item[0] == _EMPTY else _as_text(value, item[0], item[1])


#: 按列类型把值转成要插入的 Python 值。表是 STRICT 的，类型对不上 SQLite 会报错——
#: 但不靠它：这里先转好，INTEGER 列只给 int，REAL 只给 float，TEXT 只给 str，
#: 绝不让 SQLite 的亲和性去悄悄转换。
#:
#: 数字列里的非数字给 NULL：要么是用户选了「存空值」，要么是样本没看到、整列结论会变，
#: 这一遍写的会被重写（_Job.finish）。
_CONVERT: dict[str, Callable[[Any, tuple[int, Any, str | None]], Any]] = {
    "INTEGER": _to_integer, "REAL": _to_real, "TEXT": _to_text,
}


# --------------------------------------------------------------------------
# 结构检测：交叉表、多块
# --------------------------------------------------------------------------

#: NeedsDecision(kind="shape") 的 details.reasons[].kind 和它的说法
SHAPE_KINDS = {
    "date_header": "表头是横排的日期",
    "date_row": "表内有横排的日期行",
    "section_title": "表内有分段标题行",
    "table_totals": "表内有表格对象的汇总行",
    "formula_above": "表内有汇总上方各行的公式",
}


def _clip(text: str, limit: int = 24) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _numeric_col(obs: _Observer, idx: int) -> bool:
    """这一列往下大多是数字（过半即可，不要求够得上「混合列」的八成：叠放的第二块表头、
    分段里的文字也在这一列里）。"""
    st = obs.stats[idx] if idx < len(obs.stats) else None
    return st is not None and st.numeric > 0 and st.numeric * 2 >= st.nonempty


def _section_rows(plan: _Plan, obs: _Observer) -> tuple[list[tuple[int, int, str]], list[tuple[int, int, str]]]:
    """符合分段标题判据的行：整行只有一个文字格、其余值格全空，而且不是第一行数据。

    返回 (要用户拍板的, 照常导入但写进回执的)。其余列里至少有一列是数字列才要拍板：分段标题
    混进 COUNT、把数字列切成几段，是会算错的；全是文字列的表（名单、备注表）一行只填了名字很正常，
    照常导入，但逐行写进回执，不静默放过。只有一列的表没有「其余列」，这条判据无从谈起，不判。
    """
    if plan.hi - plan.lo < 2:
        return [], []
    numeric = [_numeric_col(obs, k) for k in range(plan.lo, plan.hi)]
    total = sum(numeric)
    flagged: list[tuple[int, int, str]] = []
    exempt: list[tuple[int, int, str]] = []
    for row, idx, text in obs.single_text:
        if row == obs.first_data_row or not plan.lo <= idx < plan.hi:
            continue
        (flagged if total - numeric[idx - plan.lo] > 0 else exempt).append((row, idx, text))
    return flagged, exempt


def _shape_reasons(src: _Source, header: Sequence[Any], plan: _Plan, obs: _Observer) -> list[dict[str, Any]]:
    """这张表是不是交叉表或多块结构。每条带坐标，给人看的说明在 message 里。"""
    reasons: list[dict[str, Any]] = []
    row0, col0 = src.first_row, src.first_col

    def ref(row: int, idx: int) -> str:
        return cell_ref(row, col0 + idx)

    # 1. 表头行有一串日期：「8月1日 8月2日 …」横着排，一列一天
    hits = [i for i in range(plan.lo, min(plan.hi, len(header))) if _is_date_like(header[i])]
    if len(hits) >= DATE_HEADER_MIN:
        span = f"{ref(row0, hits[0])}:{ref(row0, hits[-1])}"
        # 「8月1日」这种只写了月日：哪一年只能靠文件名、标题行猜，导入后按日期筛选、跨年比较都没有依据
        no_year = all(isinstance(header[i], str) and _MONTH_DAY.fullmatch(header[i].strip()) for i in hits)
        reasons.append({
            "kind": "date_header", "cells": [span],
            "message": f"第 {row0} 行（表头）有 {len(hits)} 个日期样式的单元格（{span}），疑似日期横排的交叉表"
                       + ("；这些日期没有写年份" if no_year else ""),
            **({"no_year": True} if no_year else {}),
        })

    # 2. 表内有一行日期、下一行不是，而且这些列往下大多是数字：表头行号没填对，或者表格上下叠了
    #    好几块。日期列本来就是日期的（一行一个项目、好几个里程碑日期）不算
    shown_rows = 0
    for row, idx in obs.date_rows:
        cols = [i for i in idx if plan.lo <= i < plan.hi]
        if not cols or sum(1 for i in cols if _numeric_col(obs, i)) * 2 < len(cols):
            continue
        span = f"{ref(row, cols[0])}:{ref(row, cols[-1])}"
        reasons.append({
            "kind": "date_row", "cells": [span],
            "message": f"第 {row} 行有 {len(cols)} 个日期样式的单元格（{span}），疑似表格中间的另一行表头",
        })
        shown_rows += 1
        if shown_rows >= 5:
            break

    # 3. 分段标题：整行只有一个文字格，其余值格全空，而且不是第一行数据（判据见 _section_rows）
    hits3, _ = _section_rows(plan, obs)
    if hits3:
        shown = "、".join(f"{ref(r, i)}「{_clip(t)}」" for r, i, t in hits3[:5])
        rows = "、".join(str(r) for r, _, _ in hits3[:5]) + (" 等" if len(hits3) > 5 else "")
        reasons.append({
            "kind": "section_title", "cells": [ref(r, i) for r, i, _ in hits3[:50]],
            "message": f"第 {rows} 行只有一个单元格有文字、其余列为空（{shown}），疑似分段标题或备注",
        })

    # 4. Excel 表格对象（插入 → 表格）勾了「汇总行」：最后一行是 SUBTOTAL(109,销售表[金额]) 之类，
    #    导入后会和明细一起被求和。表格对象的位置是文件里写明的，比看公式可靠
    bounds = src.scan.bounds if src.scan is not None else None
    bottom = bounds.max_row if bounds is not None else max(obs.last_data_row, row0)
    left, right = col0 + plan.lo, col0 + plan.hi - 1
    totals_rows: set[int] = set()
    for table in src.tables:
        rows_span, span4 = table.totals_rows(), table.span()
        if rows_span is None or span4 is None:
            continue
        top, end = max(rows_span[0], row0 + 1), min(rows_span[1], bottom)
        c_lo, c_hi = max(span4[1], left), min(span4[3], right)
        if top > end or c_lo > c_hi:
            continue
        cells = f"{cell_ref(top, c_lo)}:{cell_ref(end, c_hi)}"
        totals_rows.update(range(top, end + 1))
        which = f"第 {top} 行" if top == end else f"第 {top} 到 {end} 行"
        reasons.append({
            "kind": "table_totals", "cells": [cells],
            "message": f"表格对象「{_clip(table.name)}」（{table.ref}）的{which}是汇总行（{cells}），"
                       "导入后会和明细行一起被求和",
        })

    # 5. 公式汇总了上方的行（C26 = SUM(C22:C25)、B5 = B2+B3+B4）：表内合计。扫描覆盖整张表，
    #    只看表头以下的；已经按表格对象的汇总行报过的那几行不重复报
    if src.scan is not None and src.scan.formulas_above.max_row > row0:
        fa = src.scan.formulas_above
        pairs = [(c, t) for c, t in zip(fa.cells, fa.texts)
                 if (r := xlsx_scan.parse_ref(c)[0]) > row0 and r not in totals_rows]
        complete = fa.total == len(fa.cells)
        if pairs or not complete:
            # 扫描只记了前 MAX_CELLS 个坐标；没记全时说不准表头以下有几个，就不报个数
            count = f" {len(pairs)} 个" if complete else ""
            example = f"如 {pairs[0][0]} 的公式 {_clip(pairs[0][1], 40)}" if pairs else "见文件中的合计行"
            reasons.append({
                "kind": "formula_above", "cells": [c for c, _ in pairs],
                "message": f"有{count}单元格的公式汇总了上方的行（{example}），疑似表内合计行",
            })
    return reasons


# --------------------------------------------------------------------------
# 一张表从头到尾
# --------------------------------------------------------------------------


class _Job:
    def __init__(self, src: _Source) -> None:
        self.src = src
        self.header: Sequence[Any] = ()
        self.plan: _Plan | None = None
        self.final: _Plan | None = None
        self.full: _Observer | None = None
        self.table = ""
        self.rows_written = 0
        self.early_reasons: list[dict[str, Any]] = []
        self.reasons: list[dict[str, Any]] = []
        #: 第一遍在表头行之后读到了几行（含空行）。表头行和之后都读不到东西，说明表头行号超出了内容
        self.rows_seen = 0
        #: 第一遍（样本）的观察。结构拒收时据此预告混合列：不必等用户选了「按原样导入」再退回一次
        self.sample_obs: _Observer | None = None
        #: 样本是不是已经读完了整张表（没到 _TYPE_SAMPLE 行就到底了）。没读完时预告的混合列可能不全
        self.sampled_all = True

    def sample(self) -> None:
        """第一遍：只读前 _TYPE_SAMPLE 行数据，定下列和类型，顺便看结构。"""
        obs = _Observer(self.src.first_row, self.src.thousands)
        it = self.src.rows()
        try:
            self.header = tuple(next(it, ()))
            for row in it:
                if obs.feed(row) is not None and obs.data_rows >= _TYPE_SAMPLE:
                    self.sampled_all = next(it, None) is None
                    break
        finally:
            _close(it)
        self.sample_obs = obs
        self.rows_seen = obs.data_rows + obs.blank_rows
        self.plan = _plan(self.header, obs)
        if self.plan is not None:
            self.early_reasons = _shape_reasons(self.src, self.header, self.plan, obs)

    def write(self, conn: sqlite3.Connection, plan: _Plan, *, observe: bool) -> None:
        """建表并流式写入。observe=True 时同时统计整列（第二遍）；重写时不再统计。"""
        if plan.hi - plan.lo > MAX_COLUMNS:
            raise UnsupportedTable(
                f"{self.src.where}有 {plan.hi - plan.lo} 列，超过 {MAX_COLUMNS} 列的上限。"
                "请删除不需要的列，或检查是否有远处的单元格误输入了内容"
            )
        names = _column_names(self.header, plan)
        cols = ", ".join(f'"{n}" {t}' for (n, _), t in zip(names, plan.types))
        conn.execute(f'DROP TABLE IF EXISTS "{self.table}"')
        conn.execute(f'CREATE TABLE "{self.table}" ({cols}) STRICT')
        obs = _Observer(self.src.first_row, self.src.thousands)
        lo, hi = plan.lo, plan.hi
        convert = [(_CONVERT[t], lo + k) for k, t in enumerate(plan.types)]
        written = 0

        def rows() -> Iterator[list[Any]]:
            nonlocal written
            it = self.src.rows()
            try:
                next(it, None)
                for row in it:
                    parsed = obs.feed(row)
                    if parsed is None:
                        continue
                    if len(row) < hi:           # CSV 的短行：缺的尾巴按空值
                        row = list(row) + [None] * (hi - len(row))
                        parsed = parsed + [_EMPTY_C] * (hi - len(parsed))
                    written += 1
                    yield [conv(row[j], parsed[j]) for conv, j in convert]
            finally:
                _close(it)

        marks = ", ".join("?" * (hi - lo))
        conn.executemany(f'INSERT INTO "{self.table}" VALUES ({marks})', rows())
        self.rows_written = written
        if observe:
            self.full = obs
            self.final = _plan(self.header, obs)

    def finish(self, conn: sqlite3.Connection) -> None:
        """整列的结论和样本不一样（样本外的小数、前导零、占位符、更宽的行）：按整列的结论重写。"""
        if self.final is not None and self.plan is not None and self.final.key() != self.plan.key():
            self.write(conn, self.final, observe=False)


def _close(it: Any) -> None:
    close = getattr(it, "close", None)
    if close is not None:
        close()


# --------------------------------------------------------------------------
# 读文件 → 行来源
# --------------------------------------------------------------------------

#: CSV 的编码。中文环境里 GBK 系的 CSV 极其常见（Excel 另存为 CSV 的默认行为），
#: 一律按 UTF-8 读会得到一堆乱码列名——而乱码是不报错的，只是搜不到、看不懂。
#: 不引入 chardet：按真实场景的频次逐个试就够了，最后 latin-1 保证不抛异常。
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "latin-1")


def _decode_csv(raw: bytes) -> str:
    # 带 BOM 的 UTF-16（Excel 的「Unicode 文本」）：按 latin-1 读会混进 NUL，列名全乱
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _csv_sources(raw: bytes, filename: str, header_row: int) -> list[_Source]:
    text = _decode_csv(raw)
    delimiter = "\t" if _ext(filename) == ".tsv" else None
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","      # 嗅不出来就按最常见的来

    def rows() -> Iterator[Sequence[Any]]:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        return itertools.islice(reader, header_row - 1, None)

    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    # 分号分隔是欧洲区域设置的 CSV：那里逗号是小数点，「1,500」是 1.5 而不是一千五
    return [_Source(sheet=stem, rows=rows, first_row=header_row, first_col=1, thousands=delimiter != ";",
                    csv=True)]


def _open_book(raw: bytes) -> Any:
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise UnsupportedTable(
            "读取 Excel 需要安装解析库：pip install 'agentlab-backend[docs]'"
        ) from e
    try:
        # read_only 走流式；data_only 取公式算出来的值而不是公式本身——用户要的是数。
        # keep_links=False：外部链接里是别的工作簿的缓存数据（可以很大），导入用不上，不让它整份建树
        return load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    except Exception as e:  # noqa: BLE001
        # 这句话原样进上传的 400：异常类名留在日志（from e），界面上只说能照着做的
        raise UnsupportedTable(
            f"无法打开该 Excel 文件：{describe_exception(e)}。请在 Excel 中另存为 .xlsx 或 .csv 后重新上传"
        ) from e


def _worksheet_for(book: Any, sheet: xlsx_scan.SheetScan) -> Any:
    """扫描结果里的一张工作表 → openpyxl 里的同一张。按部件路径配对，不按名字。

    按名字取（book[名字]）时，openpyxl 返回第一个同名的表；扫描说「可见」的那张和导入实际读的
    那张就可能不是同一张。路径、名字、可见状态三样都要对得上，对不上就拒收，不猜。
    _worksheet_path 是 openpyxl 的私有属性，tests/test_tabular.py 有用例钉住它。
    """
    found = [ws for ws in book.worksheets if getattr(ws, "_worksheet_path", None) == sheet.path]
    if len(found) != 1 or found[0].title != sheet.name or getattr(found[0], "sheet_state", None) != "visible":
        raise UnsupportedTable(
            f"无法确认工作表「{sheet.name}」对应的内容，已拒绝导入。请在 Excel 中另存为 .xlsx 后重新上传")
    return found[0]


def _excel_sources(scan: WorkbookScan, book: Any, header_row: int, report: LoadReport) -> list[_Source]:
    sources: list[_Source] = []
    #: 因为表头行号超出了内容而跳过的可见工作表：(表名, 最后一行)。全被这样跳过时报的是行号填错了
    beyond: list[tuple[str, int]] = []
    for sheet in scan.sheets:
        # 隐藏、深度隐藏的工作表不导入：实测有人把口令放在 veryHidden 表里
        if sheet.state != "visible":
            report.skipped_sheets.append({"sheet": sheet.name, "state": sheet.state, "reason": "hidden"})
            continue
        bounds = sheet.bounds
        if bounds is None:
            report.skipped_sheets.append({"sheet": sheet.name, "state": sheet.state, "reason": "empty"})
            continue
        if header_row > bounds.max_row:
            report.skipped_sheets.append({"sheet": sheet.name, "state": sheet.state, "reason": "empty"})
            report.warnings.append(f"工作表「{sheet.name}」在第 {header_row} 行（表头行）及以下没有内容，已跳过")
            beyond.append((sheet.name, bounds.max_row))
            continue
        if bounds.area > SPARSE_FACTOR * max(sheet.nonempty, SPARSE_FLOOR):
            far = "、".join(list(dict.fromkeys(sheet.far_cells))[:3])
            raise UnsupportedTable(
                f"工作表「{sheet.name}」的已用区域 {bounds.a1()} 共 {bounds.area:,} 个单元格，"
                f"但只有 {sheet.nonempty:,} 个单元格有内容；远处的单元格（如 {far}）可能是误输入或残留格式。"
                "请在 Excel 中删除这些单元格后重新上传"
            )
        uncached = sheet.formulas_uncached
        if uncached.max_row >= header_row:
            cells = [c for c in uncached.cells if xlsx_scan.parse_ref(c)[0] >= header_row][:3]
            example = f"（如 {'、'.join(cells)}）" if cells else ""
            raise UnsupportedTable(
                f"工作表「{sheet.name}」中有公式没有保存计算结果{example}，文件未经计算保存。"
                "请在 Excel 中打开并保存后再上传"
            )
        ws = _worksheet_for(book, sheet)
        ws.reset_dimensions()          # 不信 <dimension>：边界用扫描算出来的

        def rows(ws: Any = ws, top: int = header_row, b: xlsx_scan.Bounds = bounds) -> Iterator[Sequence[Any]]:
            return ws.iter_rows(min_row=top, max_row=b.max_row, min_col=b.min_col, max_col=b.max_col,
                                values_only=True)

        sources.append(_Source(sheet=sheet.name, rows=rows, first_row=header_row, first_col=bounds.min_col,
                               scan=sheet, full_calc_on_load=scan.full_calc_on_load,
                               tables=[t for t in scan.tables if t.sheet == sheet.name]))
    if not sources:
        if beyond:
            # 表里有内容，只是都在表头行之上：说「没有内容」会让人以为文件坏了，其实是行号填大了
            shown = "、".join(f"「{name}」到第 {last} 行" for name, last in beyond[:3])
            raise UnsupportedTable(
                f"表头行号第 {header_row} 行超出了工作表的内容范围（{shown}{' 等' if len(beyond) > 3 else ''}），"
                "请检查表头行号"
            )
        hidden = sum(1 for s in report.skipped_sheets if s["reason"] == "hidden")
        tail = f"（已跳过 {hidden} 个隐藏的工作表）" if hidden else ""
        raise UnsupportedTable(f"该 Excel 文件中没有包含内容的可见工作表{tail}")
    return sources


# --------------------------------------------------------------------------
# 回执
# --------------------------------------------------------------------------


def _spans_in(spans: list[tuple[int, int]], lo: int, hi: int) -> list[int]:
    """闭区间列表里落在 [lo, hi] 的编号（最多给前几个示例用，个数另算）。"""
    out: list[int] = []
    for a, b in spans:
        a, b = max(a, lo), min(b, hi)
        if a <= b:
            out.extend(range(a, min(b, a + 5) + 1))
    return out


def _count_in(spans: list[tuple[int, int]], lo: int, hi: int) -> int:
    return sum(max(0, min(b, hi) - max(a, lo) + 1) for a, b in spans)


def _merge_hits(merged: list[str], top: int, bottom: int, left: int, right: int) -> list[str]:
    out = []
    for ref in merged:
        a, _, b = ref.partition(":")
        r1, c1 = xlsx_scan.parse_ref(a.replace("$", ""))
        r2, c2 = xlsx_scan.parse_ref((b or a).replace("$", ""))
        if r2 >= top and r1 <= bottom and c2 >= left and c1 <= right:
            out.append(ref)
    return out


def _sheet_warnings(job: _Job, report: LoadReport) -> None:
    src, plan, obs = job.src, job.final, job.full
    if plan is None or obs is None:
        return
    top = src.first_row
    bottom = max(obs.last_data_row, top)
    left, right = src.first_col + plan.lo, src.first_col + plan.hi - 1
    if all(_display(v) == "" for v in job.header):
        report.warnings.append(
            f"{src.where}的表头行（第 {top} 行）没有内容，列名按位置生成；"
            "如果表头不在这一行，请修改表头行号后重新上传"
        )
    sc = src.scan
    if sc is None:
        return
    hits = _merge_hits(sc.merged, top, bottom, left, right)
    if hits:
        report.warnings.append(
            f"工作表「{src.sheet}」的导入区域内有 {len(hits)} 处合并单元格（如 {'、'.join(hits[:3])}）；"
            "合并区域只有左上角的单元格有值，其余单元格按空值导入"
        )
    n_rows = _count_in(sc.hidden_rows, top + 1, bottom)
    if n_rows:
        first = _spans_in(sc.hidden_rows, top + 1, bottom)[0]
        report.warnings.append(
            f"工作表「{src.sheet}」的导入区域内有 {n_rows} 行在 Excel 中处于隐藏或筛选状态"
            f"（如第 {first} 行），已一并导入"
        )
    n_cols = _count_in(sc.hidden_cols, left, right)
    if n_cols:
        first = _spans_in(sc.hidden_cols, left, right)[0]
        report.warnings.append(
            f"工作表「{src.sheet}」的导入区域内有 {n_cols} 列在 Excel 中被隐藏（如 {col_letter(first)} 列），已一并导入"
        )
    if src.full_calc_on_load and sc.formulas:
        report.warnings.append(
            f"工作簿设置了打开时重新计算，工作表「{src.sheet}」中公式单元格保存的值可能只是占位（例如 0）。"
            "请在 Excel 中打开并保存后重新上传，以确认这些数值"
        )


def _column_report(job: _Job, report: LoadReport) -> list[dict[str, str]]:
    plan, obs = job.final, job.full
    assert plan is not None and obs is not None
    names = _column_names(job.header, plan)
    columns = []
    for k, ((name, header), sql_type) in enumerate(zip(names, plan.types)):
        st = obs.stats[plan.lo + k] if plan.lo + k < len(obs.stats) else _ColStats()
        columns.append({"name": name, "type": sql_type, "header": header})
        entry = {"table": job.table, "column": name}
        if sql_type != "TEXT" and st.thousands:
            report.conversions.append({**entry, "kind": "thousands_separator", "count": st.thousands,
                                       "examples": st.thousands_ex})
        if plan.mixed[k]:
            report.conversions.append({**entry, "kind": "nonnumeric_to_null",
                                       "count": st.nonempty - st.numeric, "examples": list(st.other)})
        if sql_type == "TEXT" and st.keep:
            report.conversions.append({**entry, "kind": "kept_as_text", "count": st.keep, "examples": st.keep_ex})
        if sql_type == "TEXT" and st.errors:
            report.warnings.append(
                f"表「{job.table}」的列「{name}」中有 {st.errors} 个错误值（如 {'、'.join(st.errors_ex)}），已按文本保存"
            )
        if sql_type == "TEXT" and st.commas:
            report.warnings.append(
                f"表「{job.table}」的列「{name}」有 {st.commas} 个带逗号的数字写法"
                f"（如 {'、'.join(f'「{v}」' for v in st.commas_ex)}），无法确定逗号是小数点还是千分位，已按文本保存；"
                "如需按数字导入，请在 Excel 中统一数字格式后重新上传"
            )
        if not _units_in_text(job.table, name, sql_type, st, report):
            _numbers_in_text(job.table, name, sql_type, st, report)
    return columns


def _units_in_text(table: str, name: str, sql_type: str, st: _ColStats, report: LoadReport) -> bool:
    """TEXT 列以「带百分号、单位或货币符号的数字」为主：回执里写明，返回写没写。

    「15%」「1,234元」「¥1234」按文本存（口径要人来定），但列看起来就是一列数：SUM 只取开头的数字
    （「1,234元」算成 1、「¥1234」算成 0），MAX 按字典序（「9%」大于「29%」）。以前回执里一个字都没有。
    只在这类值加上纯数字过半时写：备注列里偶尔出现一个「50%」不提。写了这条，就不再写泛泛的
    「TEXT 列里有数字」（_numbers_in_text），说的是同一件事。
    """
    if sql_type != "TEXT" or not st.units or (st.units + st.numeric) * 2 < st.nonempty:
        return False
    examples = "、".join(f"「{_clip(v, 12)}」" for v in st.units_ex)
    plain = f"（另有 {st.numeric} 个不带单位的数字）" if st.numeric else ""
    report.warnings.append(
        f"表「{table}」的列「{name}」有 {st.units} 个带百分号、单位或货币符号的数字（如 {examples}）{plain}，"
        "已整列按文本保存：对这一列求和、取最大值不会按数值计算，结果是错的。"
        "如需按数字导入，请在 Excel 中去掉单位或符号、把这一列设为数字格式后重新上传"
    )
    return True


def _numbers_in_text(table: str, name: str, sql_type: str, st: _ColStats, report: LoadReport) -> None:
    """TEXT 列里有数字：回执里写明，不静默。

    够不上「数字列混入非数字」（数字不到八成）的列照文本存，但数字一旦进了 TEXT 列，MAX 就按字典序取、
    SUM 只认能转的前缀。所以只要非数字的取值属于占位符一类（不超过 MIXED_MAX_DISTINCT 种），或者数字
    过半，就写一条警告；日期列里混进来的数字（多半是没设日期格式的序列号）另写一条。非数字的写法很多、
    数字只是零星几个的，是真正的文本列（备注、名称），不提。前导零、超长数字另有 kept_as_text 的记录。
    """
    if sql_type != "TEXT" or st.keep or not st.numeric:
        return
    other = st.nonempty - st.numeric
    if st.overflow and st.dates and st.numeric * 2 < st.nonempty:
        report.warnings.append(
            f"表「{table}」的列「{name}」有 {st.dates} 个日期、{st.numeric} 个数字，数字没有当作日期换算，"
            "已整列按文本保存；如果这些数字是日期，请在 Excel 中把这一列统一设为日期格式后重新上传"
        )
        return
    if st.overflow and st.numeric * 2 < st.nonempty:
        return
    if st.overflow:
        examples = "、".join(f"「{_clip(v, 12)}」" for v in list(st.other)[:3])
        why = f"非数字的写法超过 {MIXED_MAX_DISTINCT} 种"
    else:
        examples = "、".join(f"「{_clip(v, 12)}」{c} 个" for v, c in st.other.items())
        why = f"非数字的值超过 {round((1 - MIXED_MIN_SHARE) * 100)}%"
    report.warnings.append(
        f"表「{table}」的列「{name}」有 {st.numeric} 个数字、{other} 个非数字的值"
        f"（如 {examples}），{why}，已整列按文本保存，不能直接对这一列求和"
    )


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _mixed_columns(jobs: list[_Job], *, sample: bool = False) -> list[dict[str, Any]]:
    """「数字列混入非数字」的列：[{sheet, table, column, header, numeric, nonnumeric, values}]。

    sample=True 按第一遍（样本）的观察算，给结构拒收时预告用；否则按整列的统计（写入之后）。
    样本阶段还没定表名，table 先按工作表名清洗（和写入时的规则一样，只是没处理撞名）。
    """
    columns = []
    for job in jobs:
        plan, obs = (job.plan, job.sample_obs) if sample else (job.final, job.full)
        if plan is None or obs is None:
            continue
        names = _column_names(job.header, plan)
        table = job.table or to_sql_name(job.src.sheet, fallback="sheet")
        for k, is_mixed in enumerate(plan.mixed):
            if not is_mixed:
                continue
            st = obs.stats[plan.lo + k]
            columns.append({
                "sheet": job.src.sheet, "table": table, "column": names[k][0], "header": names[k][1],
                "numeric": st.numeric, "nonnumeric": st.nonempty - st.numeric,
                "values": [{"value": v, "count": c} for v, c in st.other.items()],
            })
    return columns


def _shown_values(columns: list[dict[str, Any]]) -> str:
    values = list(dict.fromkeys(v["value"] for c in columns for v in c["values"]))
    return "、".join(f"「{_clip(v, 12)}」" for v in values[:4])


def _mixed_decision(jobs: list[_Job]) -> NeedsDecision | None:
    columns = _mixed_columns(jobs)
    if not columns:
        return None
    return NeedsDecision(
        f"有 {len(columns)} 列以数字为主，但混有非数字的值（如 {_shown_values(columns)}）。"
        "请选择把这些值存为空值、整列按数字导入，或者不导入",
        kind="mixed", details={"columns": columns},
    )


def _shape_decision(jobs: list[_Job], late: bool) -> NeedsDecision | None:
    """交叉表、多块结构的拒收。顺带预告混合列（details.mixed）：选了「按原样导入」之后还会因为它们再退回一次，
    第一次就把「·」这类占位符列出来，用户一次看全要拍板的事（以前要等第二轮才看得到）。

    预告按样本算（late=False，写入之前）或按整列算（late=True）；样本没读完整张表时 mixed_complete 为假，
    之后的混合列可能比预告的多。预告只是告知，不是选择：存不存空值仍在第二轮由用户明确选。
    """
    reasons = []
    for job in jobs:
        for r in (job.reasons if late else job.early_reasons):
            reasons.append({"sheet": job.src.sheet, **r})
    if not reasons:
        return None
    sheets = list(dict.fromkeys(r["sheet"] for r in reasons))
    kinds = "、".join(dict.fromkeys(SHAPE_KINDS[r["kind"]] for r in reasons))
    where = "".join(f"「{s}」" for s in sheets[:3]) + (" 等" if len(sheets) > 3 else "")
    # CSV 没有工作表：一个文件就是一张表
    noun = "文件" if all(job.src.csv for job in jobs) else "工作表"
    message = (f"{noun}{where}不是一行一条记录的规整表格（{kinds}），默认不导入。"
               "如需导入，可以选择「按原样导入（未规整）」，导入后不能直接对列求和")
    details: dict[str, Any] = {"reasons": reasons}
    if mixed := _mixed_columns(jobs, sample=not late):
        message += (f"。另有 {len(mixed)} 列以数字为主、混有非数字的值（如 {_shown_values(mixed)}），"
                    "选择按原样导入后，还需要选择这些值的处理方式")
        details["mixed"] = mixed
        details["mixed_complete"] = late or all(job.sampled_all for job in jobs)
    return NeedsDecision(message, kind="shape", details=details)


def load_into(
    db_path: str,
    raw: bytes,
    filename: str,
    *,
    header_row: int = 1,
    mixed: str = "reject",
    raw_mode: bool = False,
    scan: WorkbookScan | None = None,
) -> LoadReport:
    """把文件里的每张可见工作表写进 db_path 指的 SQLite 库，返回导入回执。

    - mixed：数字列混入非数字时，"reject" 抛 NeedsDecision(kind="mixed")，"null" 把非数字存空值、
      列按数字存（记进回执的 conversions）。
    - raw_mode：「按原样导入（未规整）」。为假时检测到交叉表或多块结构抛 NeedsDecision(kind="shape")；
      为真时照常导入，检测到这类结构的表标记 unshaped。
    - scan：调用方已经扫描过就传进来，免得再扫一遍；不传时这里扫（只对 Excel）。

    所有表在一个事务里写完：任何一步失败，库里什么都不变。同名表整张替换（DROP + CREATE）；
    版本化的调用方每次给一个新文件，这条规则只对旧的就地导入有意义。

    用 sqlite3 直连而不是 data.engine：那条路上有 SQL 守卫，而守卫拒绝
    CREATE——那是对的，模型不该能建表。导入是我们自己发起的可信操作。
    """
    if mixed not in ("reject", "null"):
        # 调用方（接口层）该先校验；漏过来的也不能把参数名、取值原样露给界面
        raise UnsupportedTable("导入选项无效：数字列混入非数字时，只能选择存为空值或不导入")
    if header_row < 1:
        raise UnsupportedTable("表头行号从 1 开始")
    ext = _ext(filename)
    report = LoadReport()
    book = None
    if ext in _EXCEL:
        scan = scan if scan is not None else xlsx_scan.scan(raw)
        book = _open_book(raw)
    elif ext in _LEGACY_EXCEL:
        raise UnsupportedTable(
            f"无法读取 {filename}：.xls 是旧版二进制格式，需要外部转换器。"
            "请在 Excel 中另存为 .xlsx 或 .csv 后重新上传。"
        )
    elif ext not in _CSV:
        raise UnsupportedTable(f"无法读取 {filename}：仅支持 Excel（.xlsx）和 CSV / TSV。")

    conn: sqlite3.Connection | None = None
    try:
        if book is not None:
            assert scan is not None
            sources = _excel_sources(scan, book, header_row, report)
        else:
            sources = _csv_sources(raw, filename, header_row)

        jobs = [_Job(src) for src in sources]
        for job in jobs:
            job.sample()
        if header_row > 1 and all(not j.header and not j.rows_seen for j in jobs):
            # 表头行那一行根本不存在（CSV 的行数不到表头行号）：是行号填大了，不是文件里没数据
            raise UnsupportedTable(f"表头行号第 {header_row} 行超出了文件的内容范围，请检查表头行号")
        jobs = [j for j in jobs if j.plan is not None]
        if not jobs:
            raise UnsupportedTable("文件中没有数据行")
        # 结构问题在写入之前就能下结论：样本里看到的已经足够拒收
        if not raw_mode and (decision := _shape_decision(jobs, late=False)):
            raise decision

        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("BEGIN")
        taken: set[str] = set()
        for job in jobs:
            job.table = _unique(to_sql_name(job.src.sheet, fallback="sheet"), taken)
            assert job.plan is not None
            job.write(conn, job.plan, observe=True)
            if job.final is None:
                job.final = job.plan
            assert job.full is not None
            job.reasons = _shape_reasons(job.src, job.header, job.final, job.full)
        if not raw_mode and (decision := _shape_decision(jobs, late=True)):
            raise decision
        if mixed == "reject" and (decision := _mixed_decision(jobs)):
            raise decision
        for job in jobs:
            job.finish(conn)
            _report_table(job, report, raw_mode)
        conn.execute("COMMIT")
    except csv.Error as e:
        if conn is not None and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise UnsupportedTable(
            "CSV 文件格式有误（可能是某个单元格过长，或引号没有成对）。请检查后重新上传"
        ) from e
    except BaseException:
        if conn is not None and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        if conn is not None:
            conn.close()
        if book is not None:
            book.close()
    return report


def _report_table(job: _Job, report: LoadReport, raw_mode: bool) -> None:
    plan, obs, src = job.final, job.full, job.src
    assert plan is not None and obs is not None
    columns = _column_report(job, report)
    left = src.first_col + plan.lo
    right = src.first_col + plan.hi - 1
    bottom = max(obs.last_data_row, src.first_row)
    trimmed_left = range(src.first_col, left)
    trimmed_right = range(right + 1, src.first_col + max(len(job.header), obs.width()))
    unshaped = raw_mode and bool(job.reasons)
    report.tables.append(LoadedTable(
        name=job.table,
        sheet=src.sheet,
        columns=columns,
        rows=job.rows_written,
        region=f"{cell_ref(src.first_row, left)}:{cell_ref(bottom, right)}",
        blank_rows_skipped=obs.blank_rows,
        columns_trimmed=[col_letter(c) for c in itertools.chain(trimmed_left, trimmed_right)],
        unshaped=unshaped,
    ))
    if unshaped:
        kinds = "、".join(dict.fromkeys(SHAPE_KINDS[r["kind"]] for r in job.reasons))
        report.warnings.append(
            f"表「{job.table}」按原样导入、未经规整（{kinds}）：同一列里混有不同口径的行，不能直接对列求和"
        )
    _, exempt = _section_rows(plan, obs)
    if exempt:
        capped = obs.single_text_total > len(obs.single_text)
        shown = "、".join(f"{cell_ref(r, src.first_col + i)}「{_clip(t)}」" for r, i, t in exempt[:3])
        report.warnings.append(
            f"表「{job.table}」有{'至少 ' if capped else ' '}{len(exempt)} 行只有一个单元格有文字、其余列为空"
            f"（如 {shown}），已按数据行导入，会计入行数；如果这些是分段标题或备注，请在 Excel 中删除后重新上传"
        )
    _sheet_warnings(job, report)
