"""期次：快照里期次列的值认成可以排序、能判断连不连续、知道覆盖哪几天的期（细案 01 §3「期次认得出、能排序」）。

认这几种写法（一列里要是同一种）：

- 日期：DATE / DATETIME（零点）、'YYYY-MM-DD'、'YYYYMMDD'、8 位整数。粒度不写时按值推断：都是某月 1 日（或都是月末）
  且相邻两期隔整月 → month（月份都在 1、4、7、10 月且隔整季 → quarter）；相邻两期都隔 7 天的整数倍 → week（这个日期当
  一周的第一天）；否则 day。
- 月份：'YYYY-MM'、'YYYYMM'（6 位，月份 01–12）→ month。
- 周号：'YYYY-Www' → week。同样写成 2026-W42，不同的数据库、不同的格式串算出来的是不同的日子（ISO 周是 10-12 到 10-18，
  SQLite 的 strftime('%Y-W%W') 是 10-19 到 10-25），所以起止按 SQL 里写这一列的格式串算（week_scheme 认出周号算法，见
  WEEK_SCHEMES）；认不出算法的（方言不知道、只取了一列现成的字符串、年份和周号不配套）起止为空，本期齐不齐核对不了，先后按
  周号排（同一年里周号连着的算连着，上一年的第 52、53 周和下一年的第 0、1 周算连着）。按年截断的周号算法里，跨年那一周
  被切成两期，年末、年初那两期不到 7 天（short_days）：不是整周，不能当本期、比较期，也不算进历史。
- 季度：'YYYY-Qn'、'YYYYQn' → quarter。
- 整数序号（不是上面几种的整数）→ seq：能排序、能判连续，不知道覆盖哪几天（本期完不完整核对不了）。

带时刻（非零点）的、写法混着的、认不出的都算认不出（period_unreadable）。

每一期记 ordinal：同一粒度里相邻两期的 ordinal 差 1（日：天数，周：日期写的是从第一期起第几个 7 天、周号按它的算法排，
月：年 × 12 + 月，季：年 × 4 + 季，序号：本身），据此排序、判缺期、按 lag 找比较期。start / end 是这一期覆盖的第一天和最后
一天（含），seq 和认不出算法的周号没有。
"""
from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from app.data import coverage
from app.data.names import name_key

GRAINS = ("day", "week", "month", "quarter")
#: 粒度的叫法：数量词（过去 12 周、12 个月）、每…（每周、每月）
COUNT_WORD = {"day": "天", "week": "周", "month": "个月", "quarter": "个季度", "seq": "期"}
PER_WORD = {"day": "天", "week": "周", "month": "月", "quarter": "季度", "seq": "期"}
#: 粒度不写时各粒度的缺省 lag：日数据和上周同一天比（避开星期效应），其余和上一期比
DEFAULT_LAG = {"day": 7, "week": 1, "month": 1, "quarter": 1, "seq": 1}

_MONTH = re.compile(r"^(\d{4})-?(\d{2})$")
_WEEK = re.compile(r"^(\d{4})-?W(\d{1,2})$", re.I)
_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.I)

#: 「YYYY-Www」的周号算法（week_scheme 按 SQL 认，week_span 按它算一周的起止）：
#: - iso：ISO 8601。周一开始，第 1 周是有 4 天以上落在这一年的那周，年份是 ISO 年（年初、年末的几天算到相邻的年）
#: - monday：周一开始，第 1 周从这一年第一个周一起，之前的几天是第 0 周，按年截断（SQLite、C 的 %W）
#: - sunday：周日开始，其余同 monday（%U；MySQL WEEK 模式 0）
#: - monday4：周一开始，第 1 周是有 4 天以上落在这一年的那周，之前的几天是第 0 周，按年截断（MySQL 的 %u，WEEK 模式 1）
#: - sunday_year：周日开始，第 1 周从这一年第一个周日起，年初之前的几天算上一年的最后一周（MySQL 的 %X 配 %V，WEEK 模式 2）
#: - jan1：从 1 月 1 日起每 7 天一周，最后一周只有一两天（PostgreSQL、Oracle to_char 的 WW）
WEEK_SCHEMES = ("iso", "monday", "sunday", "monday4", "sunday_year", "jan1")
#: 各方言格式串里配套的（年份, 周号）写法 → 周号算法。不配套的（%Y 配 ISO 周号、IYYY 配 WW……）同一个标签会落在两段
#: 日子上，认不出
_WEEK_FORMATS: dict[str, dict[tuple[str, str], str]] = {
    "sqlite": {("%Y", "%W"): "monday", ("%Y", "%U"): "sunday", ("%G", "%V"): "iso"},
    "mysql": {("%Y", "%U"): "sunday", ("%Y", "%u"): "monday4", ("%X", "%V"): "sunday_year", ("%x", "%v"): "iso"},
    "postgres": {("YYYY", "WW"): "jan1", ("IYYY", "IW"): "iso"},
    "oracle": {("YYYY", "WW"): "jan1", ("IYYY", "IW"): "iso"},
}
#: 各方言格式串里年份、周号的写法（数格式串里有几个年份、几个周号用；方言不同，同一个 %u 意思不同）
_YEAR_MARKS = {"sqlite": ("%Y", "%G"), "mysql": ("%Y", "%X", "%x"), "postgres": ("YYYY", "IYYY"),
               "oracle": ("YYYY", "IYYY")}
_WEEK_MARKS = {"sqlite": ("%W", "%U", "%V"), "mysql": ("%U", "%u", "%V", "%v"), "postgres": ("WW", "IW"),
               "oracle": ("WW", "IW")}
#: to_char 的格式串里先认长的：IYYY 里有 YYYY，IW、WW 都有 W（一个月里的第几周）
_CHAR_MARKS = ("IYYY", "YYYY", "IW", "WW")
#: 期次列从 CTE、子查询里原样取出时，最多往里找几层
_NEST_MAX = 4


@dataclass(frozen=True)
class Period:
    raw: Any                # 快照里的原值
    ordinal: int
    start: date | None
    end: date | None


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _quarter_end(d: date) -> date:
    start = _quarter_start(d)
    return _month_end(date(start.year, start.month + 2, 1))


def _as_date(value: Any) -> date | None:
    """日期写法的期次 → 那一天；带非零点时刻的、认不出的为 None。"""
    if isinstance(value, datetime):
        return value.date() if value.time() == datetime.min.time() else None
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        return None
    day = coverage.as_day(value)
    if day is None or day.timed:
        return None
    return day.day


def _shape(value: Any) -> str | None:
    """一个期次值是哪种写法：date / month / week / quarter / seq；认不出为 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (date, datetime)):
        return "date" if _as_date(value) else None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        text = str(value)
        if len(text) == 8 and _as_date(value):
            return "date"
        if len(text) == 6 and (m := _MONTH.match(text)) and 1 <= int(m.group(2)) <= 12:
            return "month"
        return "seq"
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (m := _MONTH.match(text)) and 1 <= int(m.group(2)) <= 12 and 1900 <= int(m.group(1)) <= 2200:
        return "month"
    if _WEEK.match(text):
        return "week"
    if _QUARTER.match(text):
        return "quarter"
    if _as_date(text):
        return "date"
    if re.fullmatch(r"-?\d+", text):
        return "seq"
    return None


def _month_of(value: Any) -> date:
    m = _MONTH.match(str(int(value) if isinstance(value, (int, float)) else value).strip())
    assert m is not None
    return date(int(m.group(1)), int(m.group(2)), 1)


# --------------------------------------------------------------------------
# 周号：按算法算起止、排先后
# --------------------------------------------------------------------------


def _week_label(value: Any) -> tuple[int, int]:
    m = _WEEK.match(str(value).strip())
    assert m is not None
    return int(m.group(1)), int(m.group(2))


def _first_weekday(start: date, weekday: int) -> date:
    """start 当天或之后的第一个星期几（周一 0 … 周日 6）。"""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


def week_span(scheme: str, year: int, week: int) -> tuple[date, date] | None:
    """按周号算法，year 年第 week 周覆盖的第一天和最后一天（含）。这种算法里没有这一周（ISO 只有 52 周的年份写了第 53 周、
    1 月 1 日就是周一的年份写了第 0 周……）为 None。"""
    if scheme == "iso":
        try:
            start = date.fromisocalendar(year, week, 1)
        except ValueError:
            return None
        return start, start + timedelta(days=6)
    try:
        jan1, dec31 = date(year, 1, 1), date(year, 12, 31)
    except ValueError:
        return None
    if scheme == "jan1":
        if not 1 <= week <= 53:
            return None
        start = jan1 + timedelta(days=7 * (week - 1))
        return start, min(start + timedelta(days=6), dec31)
    if scheme == "sunday_year":
        start = _first_weekday(jan1, 6) + timedelta(days=7 * (week - 1))
        return (start, start + timedelta(days=6)) if week >= 1 and start.year == year else None
    if scheme == "monday":
        week1 = _first_weekday(jan1, 0)
    elif scheme == "sunday":
        week1 = _first_weekday(jan1, 6)
    elif scheme == "monday4":
        week1 = date.fromisocalendar(year, 1, 1)            # ISO 第 1 周的周一，可能在上一年的 12 月
    else:
        return None
    if week == 0:                                          # 第 1 周之前的那几天；第 1 周从 1 月 1 日（或更早）起就没有
        return (jan1, week1 - timedelta(days=1)) if week1 > jan1 else None
    start = week1 + timedelta(days=7 * (week - 1))
    if week < 0 or start > dec31:
        return None
    return max(start, jan1), min(start + timedelta(days=6), dec31)


def _week_start(day: date, weekday: int) -> date:
    """day 所在那一周（从星期几开始）的第一天。"""
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _week_ordinal(scheme: str, span: tuple[date, date], year: int, base: int) -> int:
    """一周在这种算法的先后里排第几（相邻两期差 1）。整 7 天一周的按那一周第一天的序数除以 7（同一个星期几开头的日子序数
    模 7 同余）；按年截断的，跨年那一周切成了两期，每过一个不是从这周第一天开始的元旦多一期（base 是这一列里最早的年份）。"""
    if scheme == "jan1":
        return year * 53 + (span[0] - date(year, 1, 1)).days // 7
    if scheme in ("iso", "sunday_year"):
        return span[0].toordinal() // 7
    weekday = 6 if scheme == "sunday" else 0
    splits = sum(1 for y in range(base + 1, year + 1) if date(y, 1, 1).weekday() != weekday)
    return _week_start(span[0], weekday).toordinal() // 7 + splits


def _loose_ordinals(labels: Iterable[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """认不出周号算法时的先后：按（年，周号）排，同一年里周号连着的算连着；上一年的第 52、53 周和下一年的第 0、1 周算连着
    （核对不了跨年那里是不是缺了一周）；别的跨年当缺期（隔几期只是估计，至少隔一期）。"""
    out: dict[tuple[int, int], int] = {}
    prev: tuple[int, int] | None = None
    n = 0
    for year, week in sorted(set(labels)):
        if prev is not None:
            if year == prev[0]:
                n += week - prev[1]
            elif year == prev[0] + 1 and prev[1] >= 52 and week <= 1:
                n += 1
            else:
                n += max(2, (year - prev[0]) * 53 + week - prev[1])
        out[(year, week)] = n
        prev = (year, week)
    return out


def short_days(item: Period, grain: str) -> int | None:
    """这一期不到整期（按年截断的周号算法里跨年切开的那两期、WW 的最后一周）时有几天；整期、不知道起止的为 None。"""
    if grain != "week" or item.start is None or item.end is None:
        return None
    days = (item.end - item.start).days + 1
    return days if days < 7 else None


@lru_cache(maxsize=256)
def week_scheme(sql: str, column: str, dialect: str | None) -> str | None:
    """期次列 column 是按哪种周号算法写成「YYYY-Www」的（WEEK_SCHEMES 之一）：看最外层 SELECT 里这一列怎么写（从 CTE、子查询
    里原样取出的往里找），格式化函数（SQLite 的 strftime、MySQL 的 DATE_FORMAT、PostgreSQL / Oracle 的 to_char）的格式串里
    年份和周号是不是配套的一对（_WEEK_FORMATS），而且只引用了一列。认不出（方言不知道、不是这几个函数、年份和周号不配套或
    不止一个、只是取了一列现成的字符串）为 None。"""
    from sqlglot import exp

    pairs = _WEEK_FORMATS.get(dialect or "")
    if pairs is None or not sql:
        return None
    try:
        value = _period_expr(sql, column, dialect)
        if value is None:
            return None
        refs = {(name_key(c.table), name_key(c.name)) for c in value.find_all(exp.Column)
                if not isinstance(c.this, exp.Star)}
        texts = [_native_format(node, dialect) for node in value.find_all(exp.TimeToStr, exp.ToChar)]  # type: ignore[arg-type]
        if len(refs) != 1 or not texts or any(t is None for t in texts):
            return None
        marks = [m for t in texts for m in _format_marks(t, dialect)]  # type: ignore[arg-type]
        years = [m for m in marks if m in _YEAR_MARKS[dialect]]  # type: ignore[index]
        weeks = [m for m in marks if m in _WEEK_MARKS[dialect]]  # type: ignore[index]
        if len(years) != 1 or len(weeks) != 1:
            return None
        return pairs.get((years[0], weeks[0]))
    except Exception:  # noqa: BLE001 - 认不出就当不知道算法
        return None


def _period_expr(sql: str, column: str, dialect: str | None) -> Any:
    """结果列 column 的写法（sqlglot 节点）：最外层 SELECT 里那一项；是从 CTE、子查询里原样取出的一列时往里找。"""
    from sqlglot import exp

    from app.engine import cell_format as cf

    tree = coverage.parse_sql(sql, dialect)
    if not isinstance(tree, exp.Select):
        return None
    ctes = {name_key(c.alias_or_name): c.this for c in tree.find_all(exp.CTE)}
    select, name = tree, column
    for _ in range(_NEST_MAX + 1):
        item = cf._projection(select, name)
        if item is None:
            return None
        value = cf._unwrap(item.unalias())
        if not isinstance(value, exp.Column) or isinstance(value.this, exp.Star):
            return value
        inner = _inner_select(select, value, ctes)
        if inner is None:
            return value
        select, name = inner, value.name
    return None


def _inner_select(select: Any, column: Any, ctes: dict[str, Any]) -> Any:
    """外层取的这一列来自哪个 CTE、子查询（那一层的 SELECT）；来自实表、认不准是哪个来源的为 None。"""
    from sqlglot import exp

    sources = [select.args.get("from_") or select.args.get("from"), *(select.args.get("joins") or [])]
    found = [s.this for s in sources if s is not None and getattr(s, "this", None) is not None]
    if column.table:
        found = [n for n in found if name_key(n.alias_or_name) == name_key(column.table)]
    if len(found) != 1:
        return None
    node = found[0]
    if isinstance(node, exp.Subquery):
        inner = node.this
    elif isinstance(node, exp.Table) and not node.db and name_key(node.name) in ctes:
        inner = ctes[name_key(node.name)]
    else:
        return None
    return inner if isinstance(inner, exp.Select) else None


def _native_format(node: Any, dialect: str) -> str | None:
    """格式化函数的格式串，按这个方言原本的写法（sqlglot 解析时换成了自己的写法，再换回去；Oracle 的 to_char 原样留着）。"""
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect

    fmt = node.args.get("format")
    if not isinstance(fmt, exp.Literal) or not fmt.is_string:
        return None
    if isinstance(node, exp.ToChar):
        return str(fmt.this)
    text = Dialect.get_or_raise(dialect).generator().format_time(node)
    if not text or len(text) < 2 or text[0] != "'" or text[-1] != "'":
        return None
    return text[1:-1].replace("''", "'")


def _format_marks(text: str, dialect: str) -> list[str]:
    """格式串里的格式符（按先后）：strftime、DATE_FORMAT 是「%」加一个字（%% 是百分号本身）；to_char 去掉双引号里的原样文字后
    按 _CHAR_MARKS 从左往右认（不分大小写）。"""
    out: list[str] = []
    i = 0
    if dialect in ("sqlite", "mysql"):
        while i < len(text):
            if text[i] == "%" and i + 1 < len(text):
                if text[i + 1] != "%":
                    out.append(text[i:i + 2])
                i += 2
            else:
                i += 1
        return out
    plain = re.sub(r'"[^"]*"', "", text).upper()
    while i < len(plain):
        mark = next((m for m in _CHAR_MARKS if plain.startswith(m, i)), None)
        if mark is not None:
            out.append(mark)
            i += len(mark)
        else:
            i += 1
    return out


def _infer_date_grain(days: Sequence[date]) -> str:
    """一列日期的粒度（见文件头）。"""
    ordered = sorted(set(days))
    if len(ordered) < 2:
        return "day"
    firsts = all(d.day == 1 for d in ordered)
    lasts = all(d == _month_end(d) for d in ordered)
    if firsts or lasts:
        months = [d.year * 12 + d.month - 1 for d in ordered]
        gaps = [b - a for a, b in zip(months, months[1:])]
        if all(g >= 1 for g in gaps):
            if all(d.month in (1, 4, 7, 10) for d in ordered) and all(g % 3 == 0 for g in gaps) and firsts:
                return "quarter"
            return "month"
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:])]
    if all(g % 7 == 0 for g in gaps):
        return "week"
    return "day"


def parse(values: Sequence[Any], grain: str | None = None, *,
          weeks: str | None = None) -> tuple[list[Period], str] | None:
    """一列期次值 → (逐个的 Period，粒度)。认不出（写法不一、带时刻、粒度和写法对不上、周号在 weeks 这种算法里没有）返回 None，
    是哪几个值见 unreadable。weeks 是 week_scheme 认出的周号算法：只用于「YYYY-Www」，None 是认不出算法（起止为空）。"""
    shapes = {_shape(v) for v in values}
    if not values or None in shapes or len(shapes) != 1:
        return None
    shape = shapes.pop()
    if shape == "seq":
        if grain not in (None, "seq"):
            return None
        return [Period(v, int(v), None, None) for v in values], "seq"
    if shape == "month":
        if grain not in (None, "month"):
            return None
        out = []
        for v in values:
            start = _month_of(v)
            out.append(Period(v, start.year * 12 + start.month - 1, start, _month_end(start)))
        return out, "month"
    if shape == "week":
        if grain not in (None, "week"):
            return None
        labels = [_week_label(v) for v in values]
        if weeks is None:
            order = _loose_ordinals(labels)
            return [Period(v, order[label], None, None) for v, label in zip(values, labels)], "week"
        spans = [week_span(weeks, *label) for label in labels]
        if any(s is None for s in spans):
            return None
        base = min(year for year, _ in labels)
        return [Period(v, _week_ordinal(weeks, s, label[0], base), s[0], s[1])  # type: ignore[index, arg-type]
                for v, label, s in zip(values, labels, spans)], "week"
    if shape == "quarter":
        if grain not in (None, "quarter"):
            return None
        out = []
        for v in values:
            m = _QUARTER.match(str(v).strip())
            assert m is not None
            start = date(int(m.group(1)), 3 * (int(m.group(2)) - 1) + 1, 1)
            out.append(Period(v, start.year * 4 + (start.month - 1) // 3, start, _quarter_end(start)))
        return out, "quarter"
    days = [_as_date(v) for v in values]
    assert all(days)
    grain = grain or _infer_date_grain(days)  # type: ignore[arg-type]
    if grain == "day":
        return [Period(v, d.toordinal(), d, d) for v, d in zip(values, days)], "day"  # type: ignore[union-attr]
    if grain == "week":
        first = min(days)  # type: ignore[type-var]
        if any((d - first).days % 7 for d in days):  # type: ignore[operator]
            return None
        return [Period(v, (d - first).days // 7, d, d + timedelta(days=6))  # type: ignore[operator]
                for v, d in zip(values, days)], "week"
    if grain == "month":
        if not all(d.day == 1 or d == _month_end(d) for d in days):  # type: ignore[union-attr]
            return None
        return [Period(v, d.year * 12 + d.month - 1, d.replace(day=1), _month_end(d))  # type: ignore[union-attr]
                for v, d in zip(values, days)], "month"
    if grain == "quarter":
        if not all(d.day == 1 and d.month in (1, 4, 7, 10) for d in days):  # type: ignore[union-attr]
            return None
        return [Period(v, d.year * 4 + (d.month - 1) // 3, d, _quarter_end(d))  # type: ignore[union-attr]
                for v, d in zip(values, days)], "quarter"
    return None


def unreadable(values: Sequence[Any], grain: str | None = None, *,
               weeks: str | None = None) -> tuple[str, list[Any]]:
    """parse 认不出时，为什么、是哪几个值（按先后、去重）：shape 写法认不出（空值、带时刻、不是这几种写法的）；mixed 写法混着
    （不是最多那种写法的几个，一样多时以先出现的为准）；week 周号在 SQL 的算法里没有这一周；grain 和给的粒度对不上（全部）。"""
    shapes = [_shape(v) for v in values]
    odd = [v for v, s in zip(values, shapes) if s is None]
    kinds = list(dict.fromkeys(shapes))
    if odd or not values:
        return "shape", _distinct(odd)
    if len(kinds) > 1:
        main = max(kinds, key=shapes.count)
        return "mixed", _distinct(v for v, s in zip(values, shapes) if s != main)
    if kinds[0] == "week" and weeks is not None and grain in (None, "week"):
        odd = [v for v in values if week_span(weeks, *_week_label(v)) is None]
        if odd:
            return "week", _distinct(odd)
    return "grain", _distinct(values)


def _distinct(values: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for v in values:
        if not any(v == o and type(v) is type(o) for o in out):
            out.append(v)
    return out


def calendar_unit(start: date, end: date) -> str | None:
    """这段日子是不是一个整的日历期：整月 month、整季 quarter、整年 year；都不是为 None。整月对整月算一样长（31 天的 10 月
    和 30 天的 9 月环比是通行的比法），不按天数判窗口不等。"""
    if start.day != 1:
        return None
    if start.month == 1 and end == date(start.year, 12, 31):
        return "year"
    if start.month in (1, 4, 7, 10) and end == _quarter_end(start):
        return "quarter"
    if end == _month_end(start):
        return "month"
    return None


def weekend_days(start: date, end: date) -> int:
    """[start, end] 里周六、周日的天数。"""
    days = (end - start).days + 1
    full, rest = divmod(days, 7)
    count = 2 * full
    for k in range(rest):
        if (start + timedelta(days=7 * full + k)).weekday() >= 5:
            count += 1
    return count


__all__ = ["COUNT_WORD", "DEFAULT_LAG", "GRAINS", "PER_WORD", "WEEK_SCHEMES", "Period", "calendar_unit", "parse",
           "short_days", "unreadable", "week_scheme", "week_span", "weekend_days"]
