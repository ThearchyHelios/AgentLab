"""期次（app/analysis/periods.py）：日期、YYYY-MM、YYYY-Www、YYYY-Qn、整数序号认得出、能排序、知道覆盖哪几天；粒度按值推断；
写法混着、带时刻的认不出。"""
from __future__ import annotations

from datetime import date

from app.analysis import periods as pd


def test_dates_infer_their_grain():
    items, grain = pd.parse(["2026-10-01", "2026-10-02", "2026-10-04"])
    assert grain == "day" and [i.start for i in items] == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 4)]
    items, grain = pd.parse(["2026-09-28", "2026-10-05", "2026-10-19"])          # 隔 7 天的整数倍：周（缺一周）
    assert grain == "week" and [i.ordinal for i in items] == [0, 1, 3] and items[0].end == date(2026, 10, 4)
    items, grain = pd.parse(["2026-08-01", "2026-09-01", "2026-10-01"])
    assert grain == "month" and items[1].end == date(2026, 9, 30)
    items, grain = pd.parse(["2026-01-01", "2026-04-01", "2026-07-01"])
    assert grain == "quarter" and items[0].end == date(2026, 3, 31)
    items, grain = pd.parse([20261001, 20261002])
    assert grain == "day" and items[1].start == date(2026, 10, 2)
    assert pd.parse(["2026-10-01 00:00:00", "2026-10-02 00:00:00"])[1] == "day"


def test_labels_for_months_weeks_quarters_and_sequences():
    items, grain = pd.parse(["2026-09", "2026-10"])
    assert grain == "month" and items[1].ordinal - items[0].ordinal == 1 and items[1].end == date(2026, 10, 31)
    items, grain = pd.parse([202609, 202610])
    assert grain == "month"
    items, grain = pd.parse(["2026-W41", "2026-W42", "2026-W01"])
    assert grain == "week" and items[1].start == date(2026, 10, 12) and items[1].end == date(2026, 10, 18)
    assert items[1].ordinal - items[0].ordinal == 1
    items, grain = pd.parse(["2025-W52", "2026-W01"])                            # 跨年也连着
    assert items[1].ordinal - items[0].ordinal == 1
    items, grain = pd.parse(["2026Q3", "2026-Q4"])
    assert grain == "quarter" and items[1].start == date(2026, 10, 1)
    items, grain = pd.parse([3, 1, 2])
    assert grain == "seq" and items[0].start is None and [i.ordinal for i in items] == [3, 1, 2]


def test_unreadable_periods():
    assert pd.parse(["2026-10-01", "2026-W41"]) is None                    # 写法混着
    assert pd.parse(["2026-10-01 08:30:00", "2026-10-02 08:30:00"]) is None    # 带时刻
    assert pd.parse(["上周", "本周"]) is None
    assert pd.parse(["2026-10", "2026-11"], "week") is None                 # 粒度和写法对不上
    assert pd.parse(["2026-10-01", "2026-10-03"], "week") is None           # 按周但不隔 7 天
    assert pd.parse([]) is None and pd.parse([None, "2026-10-01"]) is None


def test_calendar_units_and_weekend_days():
    assert pd.calendar_unit(date(2026, 9, 1), date(2026, 9, 30)) == "month"
    assert pd.calendar_unit(date(2026, 7, 1), date(2026, 9, 30)) == "quarter"
    assert pd.calendar_unit(date(2026, 1, 1), date(2026, 12, 31)) == "year"
    assert pd.calendar_unit(date(2026, 9, 2), date(2026, 9, 30)) is None
    assert pd.weekend_days(date(2026, 10, 12), date(2026, 10, 18)) == 2          # 周一到周日
    assert pd.weekend_days(date(2026, 10, 16), date(2026, 10, 18)) == 2          # 周五到周日
    assert pd.weekend_days(date(2026, 10, 12), date(2026, 10, 14)) == 0
    assert pd.weekend_days(date(2026, 10, 1), date(2026, 10, 31)) == 9
