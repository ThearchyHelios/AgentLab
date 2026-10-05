"""五字段 cron：解析、下次 / 上次触发、中文描述。

自己写而不加依赖：要的只是五个字段（分 时 日 月 周）的标准写法，再加两条现成的库各有各的做法、说不清楚
的规则——夏令时，和日、周都写了的时候取「或」还是「且」。

- 每个字段可写 *、列表（1,15）、范围（1-5）、步长（*/15、10-50/20、5/20），月和周可写英文缩写（JAN、MON），
  不分大小写；周日写 0 或 7。L、W、#、?、@daily 这些扩展写法不支持，报错说明
- 日和周：两个都受限（都不以 * 开头）时，满足任一个就触发；有一个以 * 开头，就只看另一个。以 * 开头的步长
  （*/2）也算没受限——和 Linux 系统的 cron（Vixie / cronie）一致，同一个表达式在这里和在服务器上意思相同
- 时间按任务时区的墙上时间匹配：夏令时拨快时跳过的本地时间（纽约 3 月的 02:30）那天不触发，不顺延；拨回时
  重复出现的本地时间（纽约 11 月的 01:30）只在第一次出现时触发一次
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

UTC = timezone.utc

#: 字段的键、界面上的叫法、取值范围
_FIELDS = (
    ("minute", "分", 0, 59),
    ("hour", "时", 0, 23),
    ("day", "日", 1, 31),
    ("month", "月", 1, 12),
    ("weekday", "周", 0, 7),
)
_ALLOWED = {
    "minute": "0–59",
    "hour": "0–23",
    "day": "1–31",
    "month": "1–12 或 JAN–DEC",
    "weekday": "0–7（0 和 7 都是周日）或 SUN–SAT",
}
_MONTH_NAMES = {name: i + 1 for i, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_WEEKDAY_NAMES = {name: i for i, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}
#: 每个月最多有几天：2 月按闰年算（2 月 29 日四年一次，不算永远不会触发）
_MONTH_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
#: 往后（往前）找触发时间最多找几年。2 月 29 日最长要等 8 年（2096 年之后是 2104 年）
_HORIZON_YEARS = 9
#: 夏令时一次最多拨动多少（历史上有过拨 2 小时的），往前找时据此跳过不可能的墙上时间
_MAX_SHIFT = timedelta(hours=3)

_WEEKDAY_LABEL = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六"}


class CronError(ValueError):
    """表达式写错了。str() 是给人看的中文说明；field 是出错的字段（minute / hour / day / month / weekday），
    说的是整个表达式（字段个数不对、永远不会触发）时为 None。"""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class TimezoneError(ValueError):
    """时区名认不出。str() 是给人看的中文说明。"""


@dataclass(frozen=True)
class CronSpec:
    """解析好的表达式。expr 是规范化后的原文（字段之间一个空格），描述、报错里引用它。"""

    expr: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    #: 0 是周日，6 是周六（写 7 的已经换成 0）
    weekdays: frozenset[int]
    #: 日、周字段是不是以 * 开头（没受限）。两个都受限时取「或」
    day_star: bool
    weekday_star: bool

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_days = day.day in self.days
        # date.weekday() 周一是 0；cron 周日是 0
        in_weekdays = (day.weekday() + 1) % 7 in self.weekdays
        if self.day_star or self.weekday_star:
            return in_days and in_weekdays
        return in_days or in_weekdays


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def parse(expr: str | CronSpec) -> CronSpec:
    """解析五字段表达式。写错抛 CronError（中文说明，指出字段和允许的范围）。"""
    if isinstance(expr, CronSpec):
        return expr
    parts = str(expr or "").split()
    if not parts:
        raise CronError("cron 表达式不能为空，需要 5 个字段（分 时 日 月 周），例如 0 9 * * 1 表示每周一 09:00")
    if len(parts) != 5:
        raise CronError(f"cron 表达式需要 5 个字段（分 时 日 月 周），用空格分隔；现在有 {len(parts)} 个")
    values = [_parse_field(text, *meta) for text, meta in zip(parts, _FIELDS)]
    spec = CronSpec(
        expr=" ".join(parts),
        minutes=tuple(sorted(values[0])),
        hours=tuple(sorted(values[1])),
        days=frozenset(values[2]),
        months=frozenset(values[3]),
        weekdays=frozenset(v % 7 for v in values[4]),
        day_star=parts[2].startswith("*"),
        weekday_star=parts[4].startswith("*"),
    )
    _refuse_never(spec)
    return spec


def _parse_field(text: str, key: str, label: str, low: int, high: int) -> set[int]:
    out: set[int] = set()
    for item in text.split(","):
        if not item:
            raise CronError(f"「{label}」字段「{text}」里有空的列表项，允许 {_ALLOWED[key]}", key)
        out |= _parse_item(item, key, label, low, high)
    return out


def _parse_item(item: str, key: str, label: str, low: int, high: int) -> set[int]:
    base, slash, step_text = item.partition("/")
    step = 1
    if slash:
        # isdecimal 而不是 isdigit：「²」算 digit，int() 却认不得它
        if not step_text.isdecimal() or int(step_text) == 0:
            raise CronError(f"「{label}」字段「{item}」的步长需要是正整数，现在是「{step_text}」", key)
        step = int(step_text)
    if base == "*":
        start, end = low, high
    elif "-" in base:
        left, _, right = base.partition("-")
        start = _value(left, item, key, label, low, high)
        end = _value(right, item, key, label, low, high, range_end=True)
        if start > end:
            raise CronError(f"「{label}」字段的范围「{base}」起点大于终点", key)
    else:
        start = _value(base, item, key, label, low, high)
        # 「5/20」是从 5 到最大值、每 20 一个；不带步长就是这一个值
        end = high if slash else start
    if key == "weekday" and base == "*":
        end = 6     # * 是周日到周六，7 和 0 是同一天
    return set(range(start, end + 1, step))


def _value(token: str, item: str, key: str, label: str, low: int, high: int, *, range_end: bool = False) -> int:
    word = token.strip().upper()
    if word.isdecimal():
        number = int(word)
        if not low <= number <= high:
            raise CronError(f"「{label}」字段的 {number} 超出范围，允许 {_ALLOWED[key]}", key)
        return number
    names = _MONTH_NAMES if key == "month" else _WEEKDAY_NAMES if key == "weekday" else None
    if word.isalpha() and names is None:
        raise CronError(f"「{label}」字段不能使用英文缩写「{token}」，只有「月」和「周」字段可以，允许 {_ALLOWED[key]}", key)
    if names is not None and word in names:
        number = names[word]
        # MON-SUN 是周一到周日：范围终点的 SUN 当 7
        return 7 if key == "weekday" and range_end and number == 0 else number
    raise CronError(
        f"「{label}」字段无法识别「{item}」，允许 {_ALLOWED[key]}，可以写 *、列表（1,15）、范围（1-5）和步长（*/10）", key)


def _refuse_never(spec: CronSpec) -> None:
    """日受限、周没受限时，所写的日子在所写的月份里一天都不存在（2 月 30 日、小月的 31 日）就永远不会触发。
    周受限时总有符合的那一天，不用查。"""
    if spec.day_star or not spec.weekday_star:
        return
    if any(day <= _MONTH_DAYS[month] for month in spec.months for day in spec.days):
        return
    raise CronError("这个 cron 表达式永远不会触发：所写的日期在所写的月份里不存在（例如 2 月没有 30 日）", "day")


# --------------------------------------------------------------------------
# 时区
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _zones() -> dict[str, str]:
    """小写名 → 规范名。在不区分大小写的文件系统上 ZoneInfo("asia/shanghai") 也能打开，存进去的却不是规范名，
    换到 Linux 上就打不开了：一律对照时区库的清单换成规范名。"""
    return {name.lower(): name for name in available_timezones()}


def timezone_name(name: str) -> str:
    """规范的 IANA 时区名。认不出抛 TimezoneError。"""
    text = str(name or "").strip()
    canonical = _zones().get(text.lower()) if text else None
    if canonical is None:
        shown = f"「{text}」" if text else "（未填写）"
        raise TimezoneError(f"无法识别的时区{shown}，请填写 IANA 时区名，例如 Asia/Shanghai")
    return canonical


def zone(name: str | ZoneInfo) -> ZoneInfo:
    if isinstance(name, ZoneInfo):
        return name
    return ZoneInfo(timezone_name(name))


# --------------------------------------------------------------------------
# 触发时间
# --------------------------------------------------------------------------


def _instant(local: datetime, tz: ZoneInfo) -> datetime | None:
    """本地墙上时间 → UTC 时刻。拨快时跳过的本地时间不存在，返回 None；拨回时重复的取第一次出现（fold=0）。

    zoneinfo 对不存在的本地时间按拨快前的偏移换算，换回本地就对不上原来的墙上时间——据此认出它。按 fold=0
    换算，本地时间越晚时刻也越晚，往后找到的第一个就是最早的那个。"""
    aware = local.replace(tzinfo=tz, fold=0)
    moment = aware.astimezone(UTC)
    if moment.astimezone(tz).replace(tzinfo=None) != local:
        return None
    return moment


def _days(start: date, step: int, spec: CronSpec):
    """从 start 起逐日往后（step=1）或往前（step=-1）走，只交出符合月、日、周的那些天。不在所写月份里的整月跳过。"""
    day = start
    limit = timedelta(days=366 * _HORIZON_YEARS)
    stop = start + limit if step > 0 else start - limit
    while (day <= stop) if step > 0 else (day >= stop):
        if day.month not in spec.months:
            if step > 0:
                day = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
            else:
                day = day.replace(day=1) - timedelta(days=1)
            continue
        if spec.matches_day(day):
            yield day
        day += timedelta(days=step)


def next_fire(expr: str | CronSpec, tz: str | ZoneInfo, after: datetime) -> datetime | None:
    """after 之后（不含）最早的一次触发，UTC。找不到（只在夏令时把仅有的那几个时刻全跳过时）返回 None。"""
    spec, zone_info = parse(expr), zone(tz)
    after = _aware(after)
    local = after.astimezone(zone_info).replace(tzinfo=None)
    floor = local.replace(second=0, microsecond=0)
    for day in _days(local.date(), 1, spec):
        for hour in spec.hours:
            for minute in spec.minutes:
                candidate = datetime(day.year, day.month, day.day, hour, minute)
                # 墙上时间早于 after 的那一分钟，时刻必定也早于 after（拨回那天第二遍的 after 更是如此），不用换算
                if candidate < floor:
                    continue
                moment = _instant(candidate, zone_info)
                if moment is not None and moment > after:
                    return moment
    return None


def prev_fire(expr: str | CronSpec, tz: str | ZoneInfo, before: datetime) -> datetime | None:
    """before 之前（不含）最近的一次触发，UTC。拨回那天第二遍的时刻往前找，第一遍出现的同一本地时间算已经触发过。"""
    spec, zone_info = parse(expr), zone(tz)
    before = _aware(before)
    local = before.astimezone(zone_info).replace(tzinfo=None)
    for day in _days(local.date(), -1, spec):
        for hour in reversed(spec.hours):
            for minute in reversed(spec.minutes):
                candidate = datetime(day.year, day.month, day.day, hour, minute)
                # 只按时刻比较：第二遍的 01:40 之前，墙上时间更晚的第一遍 01:50 也算。超出夏令时最大拨动幅度的不可能
                if candidate > local + _MAX_SHIFT:
                    continue
                moment = _instant(candidate, zone_info)
                if moment is not None and moment < before:
                    return moment
    return None


def last_due(expr: str | CronSpec, tz: str | ZoneInfo, at: datetime) -> datetime | None:
    """at 之前（含）最近的一次触发，UTC。"""
    return prev_fire(expr, tz, _aware(at) + timedelta(microseconds=1))


def preview(expr: str | CronSpec, tz: str | ZoneInfo, n: int = 5, *, after: datetime | None = None) -> list[datetime]:
    """after（默认现在）之后的 n 次触发，UTC。"""
    spec, zone_info = parse(expr), zone(tz)
    out: list[datetime] = []
    cursor = _aware(after) if after is not None else datetime.now(UTC)
    while len(out) < n:
        moment = next_fire(spec, zone_info, cursor)
        if moment is None:
            break
        out.append(moment)
        cursor = moment
    return out


def _aware(value: datetime) -> datetime:
    """不带时区的按 UTC 算（库里读出来的都带，这里只防调用方传 naive 的）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# 中文描述
# --------------------------------------------------------------------------


def describe(expr: str | CronSpec, tz: str) -> str:
    """「每周一 09:00（Asia/Shanghai）」。说不清的写「按 cron 表达式 x（时区）」。"""
    spec = parse(expr)
    return f"{_describe(spec) or f'按 cron 表达式 {spec.expr}'}（{tz}）"


def _describe(spec: CronSpec) -> str | None:
    all_days = spec.days == frozenset(range(1, 32))
    all_weekdays = spec.weekdays == frozenset(range(7))
    all_months = spec.months == frozenset(range(1, 13))
    # 日、周都受限时取「或」：有一边是全部，那就是每天；两边都只写了一部分就说不清，交给兜底
    either = not (spec.day_star or spec.weekday_star)
    every_day = (all_days and all_weekdays) or (either and (all_days or all_weekdays))
    # 日写的 *（以 * 开头、且确实是全部日子）时只看周；周写的 * 时只看日。*/2 这种以 * 开头的步长不是全部
    weekly = not every_day and spec.day_star and all_days
    monthly = not every_day and spec.weekday_star and all_weekdays
    if every_day and all_months:
        if spec.minutes == tuple(range(60)) and spec.hours == tuple(range(24)):
            return "每分钟"
        step = _step_of(spec.minutes, 60)
        if step and spec.hours == tuple(range(24)):
            return f"每 {step} 分钟"
    if len(spec.minutes) != 1:
        return None
    minute = spec.minutes[0]
    if every_day and all_months:
        if spec.hours == tuple(range(24)):
            return "每小时整点" if minute == 0 else f"每小时第 {minute} 分钟"
        step = _step_of(spec.hours, 24)
        if step:
            return f"每 {step} 小时" if minute == 0 else f"每 {step} 小时（第 {minute} 分钟）"
    if len(spec.hours) > 4:
        return None
    times = "、".join(f"{hour:02d}:{minute:02d}" for hour in spec.hours)
    if every_day and all_months:
        return f"每天 {times}"
    if weekly and all_months:
        return f"每{_weekdays_text(spec.weekdays)} {times}"
    if not monthly or len(spec.days) > 4:
        return None
    days = "、".join(f"{day} 日" for day in sorted(spec.days))
    if all_months:
        return f"每月 {days} {times}"
    if len(spec.months) == 1:
        month = next(iter(spec.months))
        if month == 2 and spec.days == frozenset({29}):
            return f"每逢闰年 2 月 29 日 {times}"
        return f"每年 {month} 月 {days} {times}"
    return None


def _step_of(values: tuple[int, ...], size: int) -> int | None:
    """values 是不是从 0 起、每 n 一个、整除 size 的等差（*/n）：是就返回 n（大于 1）。不整除的（*/7 分钟）到了
    整点会重新数，说成「每 7 分钟」就不对了。"""
    if len(values) < 2 or values[0] != 0:
        return None
    step = values[1] - values[0]
    if step <= 1 or size % step or values != tuple(range(0, size, step)):
        return None
    return step


def _weekdays_text(weekdays: frozenset[int]) -> str:
    """周一在前、周日在后。连续三天以上写「周一至周五」。"""
    ordered = sorted(weekdays, key=lambda d: (d - 1) % 7)
    positions = [(d - 1) % 7 for d in ordered]
    if len(ordered) >= 3 and positions == list(range(positions[0], positions[0] + len(positions))):
        return f"{_WEEKDAY_LABEL[ordered[0]]}至{_WEEKDAY_LABEL[ordered[-1]]}"
    return "、".join(_WEEKDAY_LABEL[d] for d in ordered)

