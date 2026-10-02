"""配方的固定解析器（期 2 契约文件，波次 0 由编排者提交，实现者不改签名）。

**为什么是固定集合。** 配方里没有正则字段（ReDoS、模型写错正则都会静默改数据）。会变的写法
（各类横线、HH:MM、「时」「点」后缀、全角、第二个日期省略年份、日期格）都在这里宽容归一，
规范写法统一；认不出的一律返回 None，由执行器判成结构类问题，绝不猜。新增一种写法要改代码、
升 PARSER_VER（进构建 id），并对历史原件重放回归。

**输入限长。** 标签 ≤ LABEL_MAX，上下文 ≤ CONTEXT_MAX：超长的直接 None，正则只跑在有界输入上。
所有正则都用 re.ASCII：\\d 只认 ASCII 数字（全角数字先经 canon 的 NFKC 变成 ASCII，别的文字的数字不认）。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.data.names import canon

#: 解析器版本：进构建 id（table_versions.recipe_build_id）。解析规则改了就要升
PARSER_VER = "parsers/2"
#: 标签（行标签、表头、分段标题、合计标签）长度上限
LABEL_MAX = 40
#: 上下文格（统计期所在的格、文件名）长度上限
CONTEXT_MAX = 80
#: 单位封闭词表。新增单位要改代码（说明里由系统渲染「单位：万元」，数字检查时遮盖）
UNITS: tuple[str, ...] = ("人次", "人", "元", "万元", "千元", "亿元", "千克", "吨", "个", "件", "户", "%")
#: 列表合计行标签的候选词（起草器据此认合计行；配方里的 pick 必须取自实际格子文字的候选词）
TOTAL_WORDS: tuple[str, ...] = ("合计", "总计", "小计", "汇总", "累计")
#: 期 2 认得的解析器名（配方 JSON 里出现的取值，recipe_types 的 Literal 与此一致）
LABEL_PARSERS = ("hour_range", "hour_range_total", "text")
AXIS_PARSERS = ("month_day_or_date",)
CONTEXT_PARSERS = ("cn_date_range",)

_WS = re.compile(r"\s+")


def match_key(text: Any) -> str:
    """锚点、分段标题、标签集合比对用的键：canon 之后去掉全部空白。

    「金 额」「销量\\n」「地区　」和「金额」「销量」「地区」是同一个表头。存储用 canon（保留单个空格），
    比对用这个；两者不同的写法进清单的 canonicalized，并在差异卡里报「写法变化」。
    """
    return _WS.sub("", canon("" if text is None else str(text)))


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HourRange:
    start: int
    end: int
    #: 规范写法：「7-8」（不带单位）
    canonical: str


@dataclass(frozen=True)
class HourTotal:
    start: int
    end: int
    #: 合计 / 小计 / 总计
    word: str
    #: 规范写法：「18-22时合计」
    canonical: str


@dataclass(frozen=True)
class Period:
    start: _dt.date
    end: _dt.date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def iso(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()


@dataclass(frozen=True)
class MonthMention:
    """只写到月的「2026年9月」：只能作旁证（统计期须落在这个月内才算一致），不能单独定统计期。"""

    year: int
    month: int

    def contains(self, period: Period) -> bool:
        return (period.start.year, period.start.month) == (self.year, self.month) == (
            period.end.year, period.end.month)


@dataclass(frozen=True)
class AxisDate:
    """日期轴上的一格：「8月1日」（year=None，form=text）、「2026-08-01」（text）、日期格（form=date）。"""

    year: int | None
    month: int
    day: int
    form: Literal["text", "date"]


# --------------------------------------------------------------------------
# 时段
# --------------------------------------------------------------------------

_H = r"(\d{1,2})(?::00)?\s*[时点]?"
_HOUR = re.compile(rf"{_H}\s*-\s*{_H}\s*[时点]?", re.ASCII)
_HOUR_TOTAL = re.compile(rf"{_H}\s*-\s*{_H}\s*[时点]?\s*(合计|小计|总计)", re.ASCII)


def _bounded(text: Any, limit: int) -> str | None:
    if not isinstance(text, str) or len(text) > limit:
        return None
    return canon(text)


def hour_range(text: Any) -> HourRange | None:
    """「7-8」「7–8」「07:00-08:00」「7时-8时」「7点-8点」「7-8时」「７－８」→ HourRange(7, 8, "7-8")。

    拒收：起点不小于终点（「8-7」）、超出 0..24（「25-26」）、半点（「7:30-8:00」）、带合计字样、非文本。
    """
    s = _bounded(text, LABEL_MAX)
    m = _HOUR.fullmatch(s) if s is not None else None
    if m is None:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return HourRange(a, b, f"{a}-{b}") if 0 <= a < b <= 24 else None


def hour_range_total(text: Any) -> HourTotal | None:
    """「18-22 时合计」「18:00-22:00合计」「18-22时小计」「18－24 时合计」→ HourTotal(18, 22, "合计", "18-22时合计")。"""
    s = _bounded(text, LABEL_MAX)
    m = _HOUR_TOTAL.fullmatch(s) if s is not None else None
    if m is None:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return HourTotal(a, b, m.group(3), f"{a}-{b}时{m.group(3)}") if 0 <= a < b <= 24 else None


def text_label(text: Any) -> str | None:
    """不解析的标签（parser="text"）：规范写法就是 canon，空的、超长的、非文本的 None。"""
    s = _bounded(text, LABEL_MAX)
    return s or None


# --------------------------------------------------------------------------
# 日期区间（统计期）
# --------------------------------------------------------------------------

_CN_RANGE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:至|到|~|-)\s*"
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日",
    re.ASCII)
_ISO_RANGE = re.compile(
    r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})\s*(?:至|到|~|_)\s*(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", re.ASCII)
_CN_MONTH = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d)", re.ASCII)
#: 统计期最长多少天：超过的多半是认错了（两个不相干的日期）
PERIOD_MAX_DAYS = 1000


def _period(y1: int, m1: int, d1: int, y2: int, m2: int, d2: int) -> Period | None:
    try:
        a, b = _dt.date(y1, m1, d1), _dt.date(y2, m2, d2)
    except ValueError:
        return None
    if a > b or (b - a).days + 1 > PERIOD_MAX_DAYS:
        return None
    return Period(a, b)


def _ranges(s: str) -> list[Period | None]:
    found: list[Period | None] = []
    for m in _CN_RANGE.finditer(s):
        y1, m1, d1 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        m2, d2 = int(m.group(5)), int(m.group(6))
        # 第二个日期省略年份：不早于第一个日期就是同年，否则跨年（12-15 至 01-14）
        y2 = int(m.group(4)) if m.group(4) else (y1 if (m2, d2) >= (m1, d1) else y1 + 1)
        found.append(_period(y1, m1, d1, y2, m2, d2))
    for m in _ISO_RANGE.finditer(s):
        g = [int(x) for x in m.groups()]
        found.append(_period(*g))
    return found


def cn_date_range(text: Any) -> Period | None:
    """格子文字里的统计期：「统计时间范围：2026年9月1日至2026年9月30日」「…至9月30日」「（2026年9月1日至…）」。

    在文字中**查找**（不要求整格只有区间，标题里带区间也认），但必须恰好一处、日期合法、起点不晚于终点；
    零处、多处或日期不合法都返回 None。ISO 写法「2026-09-01至2026-09-30」同样认（分隔只认 至/到/~/_，
    「-」和日期内部的横线分不开）。
    """
    s = _bounded(text, CONTEXT_MAX)
    if s is None:
        return None
    found = _ranges(s)
    return found[0] if len(found) == 1 else None


def cn_year_month(text: Any) -> MonthMention | None:
    """「（2026年9月）」这类只写到月的提法；文字里同时有完整区间的不算（那由 cn_date_range 认）。恰好一处才返回。"""
    s = _bounded(text, CONTEXT_MAX)
    if s is None or _ranges(s):
        return None
    hits = list(_CN_MONTH.finditer(s))
    if len(hits) != 1:
        return None
    y, mo = int(hits[0].group(1)), int(hits[0].group(2))
    return MonthMention(y, mo) if 1 <= mo <= 12 else None


def filename_period(name: str) -> Period | None:
    """文件名里的区间（旁证）：「月报导出_2026-08-01_2026-08-31.xlsx」或中文写法。只看文件名本身，不看路径。"""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    s = _bounded(stem, CONTEXT_MAX)
    if s is None:
        return None
    found = _ranges(s)
    return found[0] if len(found) == 1 else None


#: 统计期格里「只是在说统计期」的词：去掉区间之后只剩这些（或配方的 prefer_prefix），这格才只算统计期来源
PERIOD_WORDS: tuple[str, ...] = ("统计时间范围", "统计时间", "统计期", "统计周期", "时间范围", "日期范围",
                                 "报告期", "期间", "日期", "时间")
_RESIDUE_STRIP = re.compile(r"[\s:：()\[\]【】<>《》,，。.、;；~_\-至到]+")


def period_residue(text: Any) -> str | None:
    """统计期格去掉区间（或「2026年9月」这类年月）之后剩下的文字，去标点、去空白；认不出统计期返回 None。

    用来区分两种格子（D15、E7）：「统计时间范围：2026年9月1日至…」去掉区间只剩「统计时间范围」，只是在说
    统计期；「注：2026年9月数据为初步统计，待修订」剩「注数据为初步统计待修订」，是带着统计期的说明文字——
    后者除了参与统计期一致性判断，还要按含数字的区域外文字处理（需确认，说明里加提示）。判定见
    is_pure_period_cell。
    """
    s = _bounded(text, CONTEXT_MAX)
    if s is None:
        return None
    spans = [m.span() for m in _CN_RANGE.finditer(s)] + [m.span() for m in _ISO_RANGE.finditer(s)]
    if not spans:
        spans = [m.span() for m in _CN_MONTH.finditer(s)]
    if len(spans) != 1:
        return None
    a, b = spans[0]
    return _RESIDUE_STRIP.sub("", s[:a] + " " + s[b:])


def is_pure_period_cell(text: Any, prefer_prefix: str | None = None) -> bool:
    """去掉区间后什么都不剩、只剩 prefer_prefix、或只剩 PERIOD_WORDS 里的词：这格只是统计期来源。"""
    rest = period_residue(text)
    if rest is None:
        return False
    allowed = {"", *(match_key(w) for w in PERIOD_WORDS)}
    if prefer_prefix:
        allowed.add(_RESIDUE_STRIP.sub("", canon(prefer_prefix)))
    return rest in allowed


# --------------------------------------------------------------------------
# 日期轴
# --------------------------------------------------------------------------

_MD = re.compile(r"(\d{1,2})月(\d{1,2})日", re.ASCII)
_YMD_CN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日", re.ASCII)
_YMD_ISO = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", re.ASCII)


def month_day_or_date(value: Any) -> AxisDate | None:
    """日期轴的一格。认：「8月1日」、「2026年8月1日」、「2026-08-01」「2026/8/1」文本，以及日期格（零点的 datetime 或 date）。

    不认：带时刻的 datetime、数字（没有日期格式的序列号）、「8/1」（中美写法分不清）。月日的合法性在补年份时再查。
    """
    if isinstance(value, _dt.datetime):
        if (value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0):
            return None
        return AxisDate(value.year, value.month, value.day, "date")
    if isinstance(value, _dt.date):
        return AxisDate(value.year, value.month, value.day, "date")
    s = _bounded(value, LABEL_MAX)
    if not s:
        return None
    s = _WS.sub("", s)
    if m := _MD.fullmatch(s):
        mo, d = int(m.group(1)), int(m.group(2))
        return AxisDate(None, mo, d, "text") if 1 <= mo <= 12 and 1 <= d <= 31 else None
    for rx in (_YMD_CN, _YMD_ISO):
        if m := rx.fullmatch(s):
            y, mo, d = (int(x) for x in m.groups())
            try:
                _dt.date(y, mo, d)
            except ValueError:
                return None
            return AxisDate(y, mo, d, "text")
    return None


def complete_year(cell: AxisDate, period: Period) -> _dt.date | None:
    """给轴上的一格定日期。自带年份的原样（必须落在统计期内）；只有月日的，在统计期覆盖的年份里找唯一解。"""
    if cell.year is not None:
        try:
            d = _dt.date(cell.year, cell.month, cell.day)
        except ValueError:
            return None
        return d if period.start <= d <= period.end else None
    hits = []
    for y in range(period.start.year, period.end.year + 1):
        try:
            d = _dt.date(y, cell.month, cell.day)
        except ValueError:
            continue
        if period.start <= d <= period.end:
            hits.append(d)
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------
# 单位、数字、区域外格子的分类
# --------------------------------------------------------------------------

_UNIT_SUFFIX = re.compile(r"(.+?)\s*\(\s*([^()]{1,8}?)\s*\)")


def split_unit_suffix(label: Any) -> tuple[str, str | None]:
    """「全日客流（人次）」→ ("全日客流", "人次")；没有括号后缀 → (canon 原文, None)。

    只拆末尾一对括号（canon 之后全角括号已是半角）。括号里的字不在 UNITS 里也照样拆出来返回，
    由调用方判「单位不在词表中」——不能因为认不出就把它留在列名里。
    """
    s = canon("" if label is None else str(label))
    m = _UNIT_SUFFIX.fullmatch(s)
    if m is None:
        return s, None
    return m.group(1).strip(), m.group(2)


def unit_known(unit: str | None) -> bool:
    return unit in UNITS


_PLAIN_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?", re.ASCII)
_THOUSANDS = re.compile(r"-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?", re.ASCII)
#: 数字后面跟的汉字后缀里不许出现的日期、期间字。「50万」「12万人次」「3天」是数加量级或单位；
#: 「2026年上半年」「2026年度」「9月」「15日」「7时」「第3期」是日期或期间的写法，是文字（含数字），
#: 不是一个数：区域外按 text_digits 只需确认，列表表头里的「2026年上半年」也照样算表头文字。
_DATE_CHARS = "年月日号季期周旬时点"
_NUMBERISH = re.compile(
    r"[¥$€£]?\s*[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:%|‰|(?:(?![" + _DATE_CHARS + r"])[一-鿿]){1,4}|[A-Za-z]{1,4})?")


def thousands_number(text: Any) -> int | float | None:
    """千分位写法的数字文本（「1,234」「12,345.5」）→ 数；别的写法 None。只在配方选了 parse_thousands 时用。"""
    s = _bounded(text, LABEL_MAX)
    if s is None or not _THOUSANDS.fullmatch(s):
        return None
    plain = s.replace(",", "")
    return float(plain) if "." in plain else int(plain)


def looks_numeric_text(text: Any) -> bool:
    """看起来是一个数的文本：「123」「1,234」「12.5%」「¥1234」「50万」「12kg」。区域外遇到这种格按「区域外数字」拒收。

    数后面的汉字后缀只认量级、单位一类（不含 _DATE_CHARS）：「2026年上半年」「2026年度」「9月」不是数。
    """
    s = _bounded(text, LABEL_MAX)
    return bool(s) and _NUMBERISH.fullmatch(s) is not None


OutsideKind = Literal["number", "text_digits", "text"]


def classify_outside(value: Any) -> OutsideKind:
    """区域外的一格：number（数字格、像数的文本）→ 拒收；text_digits（含数字的文字、日期格）→ 需确认；text → 记录。"""
    if isinstance(value, bool):
        return "text"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return "text_digits"
    s = canon("" if value is None else str(value))
    if looks_numeric_text(s):
        return "number"
    return "text_digits" if any(ch.isdigit() for ch in s) else "text"


# --------------------------------------------------------------------------
# 常量的候选词
# --------------------------------------------------------------------------

_SPLIT = re.compile(r"[\s、，,/·\-_:：;；|()]+")
CANDIDATE_MAX = 12


def _strip_unit(title: str) -> str:
    return split_unit_suffix(title)[0]


def candidate_words(title: str, siblings: tuple[str, ...] | list[str] = ()) -> list[str]:
    """分段标题里可以拿来当常量的词。配方里的 const.pick 必须是其中之一（保存时静态校验）；
    重放时只查「pick 仍是新标题的子串」，不在就判结构类问题。

    取法（都在 canon 之后、去掉末尾单位括号之后）：
    1. 有兄弟标题（写进同一张表的其他分段）时，去掉所有标题的公共前缀和公共后缀，剩下的中段。公共部分要收缩到
       每个中段至少 2 个字：「日间时段客流」「夜间时段客流」的公共后缀按字算是「间时段客流」，收缩成「时段客流」，
       中段才是「日间」「夜间」；
    2. 按分隔符（空白、顿号、逗号、斜杠、间隔号、横线、下划线、冒号、分号、竖线、括号）切开的各段；
    3. 整个标题。
    去重保序；含数字的不要（配方里不能写死年份、期次）；超过 CANDIDATE_MAX 个字的不要。
    """
    base = _strip_unit(title)
    group = [base] + [_strip_unit(s) for s in siblings if _strip_unit(s) != base]
    out: list[str] = []
    if len(group) > 1:
        pre = 0
        while all(len(g) > pre for g in group) and len({g[pre] for g in group}) == 1:
            pre += 1
        suf = 0
        while all(len(g) - pre > suf for g in group) and len({g[len(g) - 1 - suf] for g in group}) == 1:
            suf += 1
        # 先收缩后缀、再收缩前缀，直到每个中段至少 2 个字；再补上「不去后缀」「不去前缀」两种切法，
        # 中文没有词边界，「工作日 早高峰 / 工作日 晚高峰」收缩后是「早高」，不去后缀才是「早高峰」
        p, q = pre, suf
        while (p or q) and any(len(g) - p - q < 2 for g in group):
            if q:
                q -= 1
            else:
                p -= 1
        for a, b in ((p, q), (p, 0), (0, q)):
            middle = base[a:len(base) - b].strip()
            if len(middle) >= 2 or (middle and len(base) < 2):
                out.append(middle)
    out.extend(p for p in _SPLIT.split(base) if p)
    out.append(base)
    seen: list[str] = []
    for w in out:
        w = w.strip()
        if w and w not in seen and len(w) <= CANDIDATE_MAX and not any(ch.isdigit() for ch in w):
            seen.append(w)
    return seen
