"""期次（app/analysis/periods.py）：日期、YYYY-MM、YYYY-Www、YYYY-Qn、整数序号认得出、能排序、知道覆盖哪几天；粒度按值推断；
写法混着、带时刻的认不出。周号按 SQL 里的格式串认算法（SQLite 的 %W / %U / %G-%V、MySQL 的 %U / %u / %X-%V / %x-%v、to_char
的 WW / IW），按算法算真实起止；认不出算法的起止为空。"""
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
    items, grain = pd.parse(["2026-W41", "2026-W42", "2026-W01"], weeks="iso")
    assert grain == "week" and items[1].start == date(2026, 10, 12) and items[1].end == date(2026, 10, 18)
    assert items[1].ordinal - items[0].ordinal == 1
    items, grain = pd.parse(["2025-W52", "2026-W01"], weeks="iso")               # 跨年也连着
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


# --------------------------------------------------------------------------
# 周号：按 SQL 认算法
# --------------------------------------------------------------------------

WEEK_SQL = {
    ("sqlite", "SELECT strftime('%Y-W%W', day) AS week, SUM(amount) AS gmv FROM orders GROUP BY week"): "monday",
    ("sqlite", "SELECT strftime('%Y-W%U', day) AS week FROM orders GROUP BY 1"): "sunday",
    ("sqlite", "SELECT strftime('%G-W%V', day) AS week FROM orders GROUP BY 1"): "iso",
    ("sqlite", "SELECT strftime('%Y', day) || '-W' || strftime('%W', day) AS week FROM orders GROUP BY 1"): "monday",
    ("sqlite", "WITH w AS (SELECT strftime('%Y-W%W', day) AS wk, amount FROM orders) "
               "SELECT wk AS week, SUM(amount) AS gmv FROM w GROUP BY 1"): "monday",
    ("sqlite", "SELECT t.wk AS week FROM (SELECT strftime('%G-W%V', day) AS wk FROM orders) AS t GROUP BY 1"): "iso",
    ("mysql", "SELECT DATE_FORMAT(day, '%x-W%v') AS week FROM orders GROUP BY 1"): "iso",
    ("mysql", "SELECT DATE_FORMAT(day, '%X-W%V') AS week FROM orders GROUP BY 1"): "sunday_year",
    ("mysql", "SELECT DATE_FORMAT(day, '%Y-W%u') AS week FROM orders GROUP BY 1"): "monday4",
    ("mysql", "SELECT DATE_FORMAT(day, '%Y-W%U') AS week FROM orders GROUP BY 1"): "sunday",
    ("postgres", "SELECT to_char(day, 'IYYY-\"W\"IW') AS week FROM orders GROUP BY 1"): "iso",
    ("postgres", "SELECT to_char(day, 'YYYY-\"W\"WW') AS week FROM orders GROUP BY 1"): "jan1",
    ("oracle", "SELECT TO_CHAR(day, 'IYYY-\"W\"IW') AS week FROM orders GROUP BY TO_CHAR(day, 'IYYY-\"W\"IW')"): "iso",
    ("oracle", "SELECT to_char(day, 'YYYY\"W\"WW') AS week FROM orders"): "jan1",
}


def test_the_week_algorithm_is_read_from_the_sql():
    for (dialect, sql), scheme in WEEK_SQL.items():
        assert pd.week_scheme(sql, "week", dialect) == scheme, (dialect, sql)


def test_week_algorithms_that_cannot_be_told_are_unknown():
    # 年份和周号不配套：同一个标签会落在两段日子上
    assert pd.week_scheme("SELECT strftime('%Y-W%V', day) AS week FROM orders", "week", "sqlite") is None
    assert pd.week_scheme("SELECT DATE_FORMAT(day, '%Y-W%v') AS week FROM orders", "week", "mysql") is None
    assert pd.week_scheme("SELECT to_char(day, 'YYYY-\"W\"IW') AS week FROM orders", "week", "postgres") is None
    # MySQL 的 %W 是星期几的名字，不是周号；方言不知道；取的是现成的一列；两个日期列
    assert pd.week_scheme("SELECT DATE_FORMAT(day, '%Y-W%W') AS week FROM orders", "week", "mysql") is None
    assert pd.week_scheme("SELECT strftime('%Y-W%W', day) AS week FROM orders", "week", None) is None
    assert pd.week_scheme("SELECT week, SUM(amount) AS gmv FROM weekly GROUP BY week", "week", "sqlite") is None
    assert pd.week_scheme("SELECT strftime('%Y', a) || '-W' || strftime('%W', b) AS week FROM t", "week", "sqlite") \
        is None
    assert pd.week_scheme("SELECT strftime('%Y-W%W', day) AS wk FROM orders", "week", "sqlite") is None


def test_sqlite_monday_weeks_cut_at_new_year():
    items, grain = pd.parse(["2025-W52", "2026-W00", "2026-W01", "2026-W42"], weeks="monday")
    spans = [(i.start, i.end) for i in items]
    assert spans == [(date(2025, 12, 29), date(2025, 12, 31)), (date(2026, 1, 1), date(2026, 1, 4)),
                     (date(2026, 1, 5), date(2026, 1, 11)), (date(2026, 10, 19), date(2026, 10, 25))]
    assert [i.ordinal - items[0].ordinal for i in items[:3]] == [0, 1, 2]       # 跨年切开的两期也连着
    assert [pd.short_days(i, grain) for i in items] == [3, 4, None, None]
    # 1 月 1 日就是周一的年份没有第 0 周：认不出，原因里举的是它
    assert pd.parse(["2024-W00", "2024-W01"], weeks="monday") is None
    assert pd.unreadable(["2024-W00", "2024-W01"], weeks="monday") == ("week", ["2024-W00"])
    # ISO 只有 52 周的年份没有第 53 周
    assert pd.parse(["2026-W53"], weeks="iso") is not None and pd.parse(["2025-W53"], weeks="iso") is None


def test_weeks_of_an_unknown_algorithm_have_no_dates_but_keep_their_order():
    items, grain = pd.parse(["2025-W51", "2025-W52", "2026-W00", "2026-W01", "2026-W03"])
    assert grain == "week" and all(i.start is None and i.end is None for i in items)
    assert [i.ordinal - items[0].ordinal for i in items] == [0, 1, 2, 3, 5]        # 同一年里隔了一周就是缺期
    items, _ = pd.parse(["2025-W52", "2026-W01"])                                   # 上一年最后一周接下一年第一周
    assert items[1].ordinal - items[0].ordinal == 1
    items, _ = pd.parse(["2025-W40", "2026-W01"])
    assert items[1].ordinal - items[0].ordinal > 1


def test_unreadable_values_are_named():
    assert pd.unreadable(["2026-W40", "上周", "2026-W41", None]) == ("shape", ["上周", None])
    assert pd.unreadable(["2026-W40", "2026-W41", "2026-10-12"]) == ("mixed", ["2026-10-12"])
    assert pd.unreadable(["2026-10", "2026-11"], "week") == ("grain", ["2026-10", "2026-11"])


def test_a_plain_date_column_is_a_day_even_when_two_values_are_a_week_apart():
    plain = "SELECT region, order_date AS day, SUM(amount) AS gmv FROM orders GROUP BY 1, 2"
    starts = ("SELECT region, date(order_date, '-6 days', 'weekday 1') AS week, SUM(amount) AS gmv FROM orders "
              "GROUP BY 1, 2")
    assert pd.plain_day(plain, "day", "sqlite") and pd.plain_day(plain.replace("order_date AS", "DATE(order_date) AS"),
                                                                  "day", "sqlite")
    assert not pd.plain_day(starts, "week", "sqlite")
    assert pd.parse_compared(["2026-10-13", "2026-10-20"], plain, "day", "sqlite")[1] == "day"
    assert pd.parse_compared(["2026-10-12", "2026-10-19"], starts, "week", "sqlite")[1] == "week"
    assert pd.parse_compared(["2026-09-01", "2026-10-01"], plain, "day", "sqlite")[1] == "month"



def test_week_numbers_out_of_any_algorithm_are_unreadable():
    assert pd.parse(["2026-W41", "2026-W99"]) is None
    assert pd.unreadable(["2026-W41", "2026-W99"]) == ("shape", ["2026-W99"])
    assert pd.week_span("sunday_year", 9999, 53) is None and pd.week_span("iso", 0, 1) is None
