"""定时任务的 cron（app/scheduler/cron.py）：自己写的五字段解析、下次触发、中文描述。

- 五个字段（分 时 日 月 周）：*、列表、范围、步长、月份和星期的英文缩写；周日写 0 或 7
- 日和周都受限时取「或」，有一个以 * 开头就只看另一个（和 Linux 系统的 cron 一致）
- 下次触发按任务时区的墙上时间算：夏令时跳过的本地时间不触发，重复的本地时间只在第一次出现时触发一次
- 写错的表达式给中文说明：哪个字段、允许的范围；永远不会触发的（2 月 30 日）也拦下
- 描述：每天 09:00、每周一 09:00、每月 1 日 08:30、每 2 小时……说不清的写「按 cron 表达式 x」，后面带时区

America/New_York 2026 年 3 月 8 日 2:00 拨快到 3:00（2:00–2:59 不存在），11 月 1 日 2:00 拨回 1:00（1:00–1:59 出现两次）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.scheduler import cron

UTC = timezone.utc
NY = "America/New_York"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def nexts(expr: str, tz: str, after: datetime, n: int = 5) -> list[datetime]:
    return cron.preview(expr, tz, n, after=after)


# --------------------------------------------------------------------------
# 解析：*、列表、范围、步长、缩写
# --------------------------------------------------------------------------


def test_star_list_range_step():
    spec = cron.parse("*/15 9-17 * * 1-5")
    assert spec.minutes == (0, 15, 30, 45)
    assert spec.hours == tuple(range(9, 18))
    assert spec.weekdays == frozenset({1, 2, 3, 4, 5})
    assert spec.days == frozenset(range(1, 32)) and spec.months == frozenset(range(1, 13))


def test_range_with_step_and_value_with_step():
    assert cron.parse("10-50/20 * * * *").minutes == (10, 30, 50)
    # 「5/20」是 5 到最大值、每 20 一个（Vixie cron 的写法）
    assert cron.parse("5/20 * * * *").minutes == (5, 25, 45)
    assert cron.parse("0 0,12 1,15 * *").hours == (0, 12)


def test_month_and_weekday_abbreviations_any_case():
    spec = cron.parse("0 9 * jan,Jul,DEC mon-fri")
    assert spec.months == frozenset({1, 7, 12})
    assert spec.weekdays == frozenset({1, 2, 3, 4, 5})
    # 周日写 0、7、SUN 都行；MON-SUN 是周一到周日
    assert cron.parse("0 9 * * 7").weekdays == frozenset({0})
    assert cron.parse("0 9 * * SUN,7").weekdays == frozenset({0})
    assert cron.parse("0 9 * * MON-SUN").weekdays == frozenset(range(7))
    assert cron.parse("0 9 * * 5-7").weekdays == frozenset({5, 6, 0})


def test_expression_is_normalized():
    assert cron.parse("  0   9 *  *\t1 ").expr == "0 9 * * 1"


# --------------------------------------------------------------------------
# 写错的表达式：中文说明，指出哪个字段、允许的范围
# --------------------------------------------------------------------------


@pytest.mark.parametrize("expr, fragments", [
    ("", ["不能为空"]),
    ("* * * *", ["5 个字段", "4 个"]),
    ("0 9 * * 1 2026", ["5 个字段", "6 个"]),
    ("61 * * * *", ["「分」", "61", "0–59"]),
    ("0 24 * * *", ["「时」", "24", "0–23"]),
    ("0 0 0 * *", ["「日」", "1–31"]),
    ("0 0 * 13 *", ["「月」", "1–12"]),
    ("0 0 * * 8", ["「周」", "0–7"]),
    ("*/0 * * * *", ["「分」", "步长"]),
    ("*/x * * * *", ["「分」", "步长"]),
    ("0 20-10 * * *", ["「时」", "20-10", "起点大于终点"]),
    ("0 JAN * * *", ["「时」", "JAN", "缩写"]),
    ("0 9 * * L", ["「周」", "L"]),
    ("0 9 1,,2 * *", ["「日」"]),
    ("@daily", ["5 个字段"]),
])
def test_invalid_expressions_explain_in_chinese(expr, fragments):
    with pytest.raises(cron.CronError) as caught:
        cron.parse(expr)
    message = str(caught.value)
    for fragment in fragments:
        assert fragment in message, message


def test_invalid_expression_names_the_field():
    with pytest.raises(cron.CronError) as caught:
        cron.parse("0 25 * * *")
    assert caught.value.field == "hour"


@pytest.mark.parametrize("expr", ["0 0 30 2 *", "0 0 31 4,6,9,11 *", "0 0 31 FEB *"])
def test_dates_that_never_exist_are_rejected(expr):
    with pytest.raises(cron.CronError) as caught:
        cron.parse(expr)
    assert "永远不会触发" in str(caught.value)


def test_leap_day_is_not_never():
    """2 月 29 日四年一次，不算永远不会触发。日、周都受限时周那一边总会碰上，也不算"""
    cron.parse("0 0 29 2 *")
    cron.parse("0 0 30 2 1")


# --------------------------------------------------------------------------
# 下次触发
# --------------------------------------------------------------------------


def test_next_is_strictly_after():
    assert cron.next_fire("0 9 * * *", "UTC", utc(2026, 10, 5, 9, 0)) == utc(2026, 10, 6, 9, 0)
    assert cron.next_fire("0 9 * * *", "UTC", utc(2026, 10, 5, 8, 59, 59)) == utc(2026, 10, 5, 9, 0)


def test_next_follows_the_task_timezone():
    # 上海 09:00 = UTC 01:00
    assert cron.next_fire("0 9 * * *", "Asia/Shanghai", utc(2026, 10, 5, 2, 0)) == utc(2026, 10, 6, 1, 0)


def test_weekly_and_monthly():
    # 2026-10-05 是周一
    assert nexts("0 9 * * 1", "UTC", utc(2026, 10, 5, 9, 0), 2) == [utc(2026, 10, 12, 9), utc(2026, 10, 19, 9)]
    assert nexts("30 8 1 * *", "UTC", utc(2026, 10, 5), 3) == [
        utc(2026, 11, 1, 8, 30), utc(2026, 12, 1, 8, 30), utc(2027, 1, 1, 8, 30)]


def test_day_and_weekday_both_restricted_means_either():
    """每月 1 日或者每周一：11 月 1 日是周日，也触发"""
    assert nexts("0 9 1 * 1", "UTC", utc(2026, 10, 5, 10), 5) == [
        utc(2026, 10, 12, 9), utc(2026, 10, 19, 9), utc(2026, 10, 26, 9), utc(2026, 11, 1, 9), utc(2026, 11, 2, 9)]


def test_star_on_one_side_means_only_the_other():
    assert nexts("0 9 * * 1", "UTC", utc(2026, 10, 25), 2) == [utc(2026, 10, 26, 9), utc(2026, 11, 2, 9)]
    assert nexts("0 9 1 * *", "UTC", utc(2026, 10, 25), 2) == [utc(2026, 11, 1, 9), utc(2026, 12, 1, 9)]
    # 以 * 开头的步长也算「没限定」（和 Linux 系统的 cron 一致）：单数日里的周一，不是单数日或周一
    assert nexts("0 9 */2 * 1", "UTC", utc(2026, 10, 1), 3) == [
        utc(2026, 10, 5, 9), utc(2026, 10, 19, 9), utc(2026, 11, 9, 9)]


def test_leap_day_every_four_years():
    assert cron.next_fire("0 0 29 2 *", "UTC", utc(2026, 1, 1)) == utc(2028, 2, 29)
    assert cron.next_fire("0 0 29 2 *", "UTC", utc(2028, 2, 29)) == utc(2032, 2, 29)
    # 按任务时区：上海的 2 月 29 日零点是 UTC 2 月 28 日 16:00
    assert cron.next_fire("0 0 29 2 *", "Asia/Shanghai", utc(2026, 1, 1)) == utc(2028, 2, 28, 16)


def test_spring_forward_skips_the_missing_local_time():
    """3 月 8 日没有 02:30：那天不触发，下一次是 3 月 9 日 02:30（夏令时，UTC 06:30）"""
    assert nexts("30 2 * * *", NY, utc(2026, 3, 7, 8), 2) == [utc(2026, 3, 9, 6, 30), utc(2026, 3, 10, 6, 30)]
    # 每半小时：01:30（冬令时）之后是 03:00（夏令时），02:00、02:30 不存在
    assert nexts("*/30 * * * *", NY, utc(2026, 3, 8, 6, 15), 3) == [
        utc(2026, 3, 8, 6, 30), utc(2026, 3, 8, 7, 0), utc(2026, 3, 8, 7, 30)]
    # 拨快之后的 03:00 照常触发
    assert cron.next_fire("0 3 * * *", NY, utc(2026, 3, 8, 5)) == utc(2026, 3, 8, 7)


def test_fall_back_fires_the_repeated_local_time_once():
    """11 月 1 日 01:30 出现两次（UTC 05:30 夏令时、06:30 冬令时）：只在第一次触发"""
    assert nexts("30 1 * * *", NY, utc(2026, 11, 1, 4), 2) == [utc(2026, 11, 1, 5, 30), utc(2026, 11, 2, 6, 30)]
    # 每半小时：01:00、01:30 各一次，第二遍的 01:00、01:30 不再触发，下一次是 02:00（冬令时，UTC 07:00）
    assert nexts("*/30 * * * *", NY, utc(2026, 11, 1, 4, 45), 4) == [
        utc(2026, 11, 1, 5, 0), utc(2026, 11, 1, 5, 30), utc(2026, 11, 1, 7, 0), utc(2026, 11, 1, 7, 30)]
    # 从第二遍的 01:10（UTC 06:10）往后找：01:30 已经在第一遍触发过
    assert cron.next_fire("*/30 * * * *", NY, utc(2026, 11, 1, 6, 10)) == utc(2026, 11, 1, 7, 0)


def test_previous_fire():
    assert cron.prev_fire("0 9 * * 1", "UTC", utc(2026, 10, 5, 9)) == utc(2026, 9, 28, 9)
    assert cron.prev_fire("0 9 * * 1", "UTC", utc(2026, 10, 5, 9, 0, 1)) == utc(2026, 10, 5, 9)
    # 第二遍的 01:40 之前最近的一次是第一遍的 01:30（UTC 05:30），它已经发生过
    assert cron.prev_fire("30 1 * * *", NY, utc(2026, 11, 1, 6, 40)) == utc(2026, 11, 1, 5, 30)
    assert cron.prev_fire("30 2 * * *", NY, utc(2026, 3, 9, 0)) == utc(2026, 3, 7, 7, 30)


def test_last_due_at_or_before():
    assert cron.last_due("*/5 * * * *", "UTC", utc(2026, 10, 5, 9, 7, 30)) == utc(2026, 10, 5, 9, 5)
    assert cron.last_due("*/5 * * * *", "UTC", utc(2026, 10, 5, 9, 5)) == utc(2026, 10, 5, 9, 5)


def test_preview_counts_and_defaults_to_now():
    out = cron.preview("*/10 * * * *", "UTC", 3)
    assert len(out) == 3 and all(b - a == timedelta(minutes=10) for a, b in zip(out, out[1:]))
    assert out[0] > datetime.now(UTC) - timedelta(seconds=1)


# --------------------------------------------------------------------------
# 时区
# --------------------------------------------------------------------------


def test_timezone_names_are_canonical():
    assert cron.timezone_name("Asia/Shanghai") == "Asia/Shanghai"
    assert cron.timezone_name(" asia/shanghai ") == "Asia/Shanghai"
    assert cron.timezone_name("UTC") == "UTC"


@pytest.mark.parametrize("name", ["", "Mars/Olympus", "../etc/passwd", "GMT+8 北京"])
def test_unknown_timezone_explains_in_chinese(name):
    with pytest.raises(cron.TimezoneError) as caught:
        cron.timezone_name(name)
    assert "时区" in str(caught.value) and "Asia/Shanghai" in str(caught.value)


# --------------------------------------------------------------------------
# 中文描述
# --------------------------------------------------------------------------


@pytest.mark.parametrize("expr, text", [
    ("0 9 * * *", "每天 09:00"),
    ("0 9,18 * * *", "每天 09:00、18:00"),
    ("0 9 * * 1", "每周一 09:00"),
    ("0 9 * * MON", "每周一 09:00"),
    ("0 9 * * 1,3,5", "每周一、周三、周五 09:00"),
    ("0 9 * * 1-5", "每周一至周五 09:00"),
    ("0 10 * * 0", "每周日 10:00"),
    ("30 8 1 * *", "每月 1 日 08:30"),
    ("30 8 1,15 * *", "每月 1 日、15 日 08:30"),
    ("0 8 1 3 *", "每年 3 月 1 日 08:00"),
    ("0 0 29 2 *", "每逢闰年 2 月 29 日 00:00"),
    ("0 */2 * * *", "每 2 小时"),
    ("15 */6 * * *", "每 6 小时（第 15 分钟）"),
    ("0 * * * *", "每小时整点"),
    ("20 * * * *", "每小时第 20 分钟"),
    ("*/5 * * * *", "每 5 分钟"),
    ("* * * * *", "每分钟"),
    ("0 9 1 * 1", "按 cron 表达式 0 9 1 * 1"),
    ("*/7 * * * *", "按 cron 表达式 */7 * * * *"),
    ("0 9-17 * * 1-5", "按 cron 表达式 0 9-17 * * 1-5"),
])
def test_describe_in_chinese(expr, text):
    assert cron.describe(expr, "Asia/Shanghai") == f"{text}（Asia/Shanghai）"
