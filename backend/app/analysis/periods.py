"""期次：快照里期次列的值认成可以排序、能判断连不连续、知道覆盖哪几天的期（细案 01 §3「期次认得出、能排序」）。

认这几种写法（一列里要是同一种）：

- 日期：DATE / DATETIME（零点）、'YYYY-MM-DD'、'YYYYMMDD'、8 位整数。粒度不写时按值推断：都是某月 1 日（或都是月末）
  且相邻两期隔整月 → month（月份都在 1、4、7、10 月且隔整季 → quarter）；相邻两期都隔 7 天的整数倍 → week（这个日期当
  一周的第一天）；否则 day。
- 月份：'YYYY-MM'、'YYYYMM'（6 位，月份 01–12）→ month。
- ISO 周：'YYYY-Www'（2026-W42 是 2026-10-12 到 10-18）→ week。SQLite 的 strftime('%W') 不是 ISO 周号（年初那几天
  算第 0 周），按它写的期次覆盖的日子会差一周：方法里要求期次写成每周第一天的日期。
- 季度：'YYYY-Qn'、'YYYYQn' → quarter。
- 整数序号（不是上面几种的整数）→ seq：能排序、能判连续，不知道覆盖哪几天（本期完不完整核对不了）。

带时刻（非零点）的、写法混着的、认不出的都算认不出（period_unreadable）。

每一期记 ordinal：同一粒度里相邻两期的 ordinal 差 1（日：天数，周：从第一期起第几个 7 天，月：年 × 12 + 月，季：年 × 4 +
季，序号：本身），据此排序、判缺期、按 lag 找比较期。start / end 是这一期覆盖的第一天和最后一天（含），seq 没有。
"""
from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.data import coverage

GRAINS = ("day", "week", "month", "quarter")
#: 粒度的叫法：数量词（过去 12 周、12 个月）、每…（每周、每月）
COUNT_WORD = {"day": "天", "week": "周", "month": "个月", "quarter": "个季度", "seq": "期"}
PER_WORD = {"day": "天", "week": "周", "month": "月", "quarter": "季度", "seq": "期"}
#: 粒度不写时各粒度的缺省 lag：日数据和上周同一天比（避开星期效应），其余和上一期比
DEFAULT_LAG = {"day": 7, "week": 1, "month": 1, "quarter": 1, "seq": 1}

_MONTH = re.compile(r"^(\d{4})-?(\d{2})$")
_WEEK = re.compile(r"^(\d{4})-?W(\d{1,2})$", re.I)
_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.I)


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


def parse(values: Sequence[Any], grain: str | None = None) -> tuple[list[Period], str] | None:
    """一列期次值 → (逐个的 Period，粒度)。认不出（写法不一、带时刻、粒度和写法对不上）返回 None。"""
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
        out = []
        for v in values:
            m = _WEEK.match(str(v).strip())
            assert m is not None
            try:
                start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
            except ValueError:
                return None
            out.append(Period(v, start.toordinal() // 7, start, start + timedelta(days=6)))
        return out, "week"
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


__all__ = ["COUNT_WORD", "DEFAULT_LAG", "GRAINS", "PER_WORD", "Period", "calendar_unit", "parse", "weekend_days"]
