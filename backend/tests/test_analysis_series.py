"""波动基线、趋势（app/analysis/series.py，细案 01 §3、§4）：常规 / 偏离 / 明显偏离、σ̂ = 0、历史不够、本期不完整、缺期、日数据
lag 7、新高新低；上升 / 下降 / 无趋势、连续期数、缺期。每个用例都顺带核对复算和运行逐字段相同。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from fractions import Fraction

import pytest

from app.analysis.series import MAD_SCALE, run_length
from tests.analysis_samples import analyze, call_error, fixed_prober, snap

SQL = "SELECT DATE(order_date, 'weekday 0', '-6 days') AS week, COUNT(*) AS orders FROM orders GROUP BY 1"
#: 每周一的日期：最后一周是 2026-10-12（到 10-18，运行当天 10-21 之前已经过完）
LAST_MONDAY = date(2026, 10, 12)


def weeks(values, *, last=LAST_MONDAY):
    start = last - timedelta(days=7 * (len(values) - 1))
    return [[(start + timedelta(days=7 * i)).isoformat(), v] for i, v in enumerate(values)]


def series(rows, sql=SQL, cols=("week", "orders")):
    return snap("Q1", sql, list(cols), rows, types={cols[0]: "text", cols[1]: "number"})


async def baseline(rows, **params):
    params = {"query": "Q1", "period": "week", "measure": "orders", "label": "订单数", **params}
    return await analyze("baseline", snaps=[series(rows)], **params)


async def trend(rows, **params):
    params = {"query": "Q1", "period": "week", "measure": "orders", "label": "线上销售额", **params}
    return await analyze("trend", snaps=[series(rows)], **params)


NOISE = [100, 102, 98, 101, 99, 103, 100, 97, 102, 101, 99, 100, 98, 103, 101, 100, 99, 102, 100]


# --------------------------------------------------------------------------
# 波动基线
# --------------------------------------------------------------------------


async def test_level_basis_with_hand_computed_median_and_mad():
    rows = [[i + 1, v] for i, v in enumerate([10, 12, 11, 13, 9, 10, 12, 11, 16])]
    result = await baseline(rows, basis="level")
    # 历史中位数 11，MAD 1，σ̂ = 1.4826，z = (16 − 11) / 1.4826
    values = result["values"]
    assert values["median"] == "11" and values["n_history"] == "8" and values["rank"] == "1"
    assert Fraction(values["z"]) == pytest.approx(Fraction(5) / MAD_SCALE)
    assert values["band_lo"] == "8.0348" and values["band_hi"] == "13.9652"
    assert result["verdict"] == {"code": "far_outside_band", "subjects": [], "direction": "up"}
    assert result["phrase"].startswith("订单数本期为 16，明显超出过去 8 期的常规波动范围（8.03 至 13.97）")
    assert any(c["code"] == "record_high" for c in result["caveats"])
    assert result["params"]["grain"] == "seq" and result["params"]["lag"] == 1
    assert result["settings"] == {"k_warn": 2.0, "k_alert": 3.0, "min_history": 8, "max_history": 52}
    assert result["table"]["rows"][-1][-1] == "本期"
    assert result["table"]["column_types"] == {"week": "text", "value": "number", "change": "number", "role": "text"}


async def test_change_basis_within_outside_and_far_outside():
    probe = fixed_prober({("orders", "order_date"): ("2025-01-01", "2026-10-20")})
    within = await analyze("baseline", snaps=[series(weeks(NOISE + [101]))], probe=probe, query="Q1", period="week",
                           measure="orders", label="订单数")
    assert within["verdict"]["code"] == "within_band"
    assert within["phrase"].startswith("订单数本期变化 +1.0%，在过去 18 周的常规波动范围内（")
    assert within["params"]["today"] == "2026-10-21" and within["params"]["grain"] == "week"
    assert within["probes"][0]["table"] == "orders" and within["probes"][0]["column"] == "order_date"
    assert not any(c["code"] == "comparability" for c in within["caveats"])
    outside = await analyze("baseline", snaps=[series(weeks(NOISE + [107]))], probe=probe, query="Q1",
                            period="week", measure="orders")
    assert outside["verdict"]["code"] == "outside_band" and "超出过去 18 周" in outside["phrase"]
    far = await analyze("baseline", snaps=[series(weeks(NOISE + [70]))], probe=probe, query="Q1", period="week",
                        measure="orders", label="订单数")
    assert far["verdict"] == {"code": "far_outside_band", "subjects": [], "direction": "down"}
    assert far["phrase"].startswith("订单数本期变化 -30.0%，明显超出过去 18 周的常规波动范围")
    assert any(c["code"] == "record_low" and c["text"] == "本期的订单数是过去 18 周里最低的" for c in far["caveats"])


async def test_without_a_table_range_completeness_is_said_to_be_unverified_but_not_in_the_phrase():
    result = await baseline(weeks(NOISE + [101]))
    note = next(c for c in result["caveats"] if c["code"] == "comparability")
    assert note["text"] == "没能核对本期的数据是否到齐（查不到表里这一列的日期范围）"
    assert note["text"] not in result["phrase"]


async def test_flat_history_has_no_spread():
    rows = [[i + 1, 50] for i in range(10)]
    same = await baseline(rows, basis="level")
    assert same["verdict"]["code"] == "within_band" and same["values"]["z"] is None
    assert same["phrase"] == "订单数本期为 50，和过去 9 期一样（每期都是 50）"
    jump = await baseline(rows[:-1] + [[10, 51]], basis="level")
    assert jump["verdict"] == {"code": "far_outside_band", "subjects": [], "direction": "up"}
    assert jump["phrase"].startswith("订单数本期为 51，明显超出过去 9 期的常规波动范围（每期都是 50）")


async def test_short_history_partial_target_gaps_and_zero_bases_are_refused():
    short = await baseline(weeks([100, 101, 99, 102, 98, 100]))
    # 6 周：第一周没有上一周可比，本期不算历史，历史的变化只有 4 个
    assert short["reason_code"] == "history_short" and short["reason"] == "只有 4 期历史，至少要 8 期"
    assert short["phrase"] == "波动基线没有做成：只有 4 期历史，至少要 8 期"
    # 最后一周是 10-19 到 10-25：运行当天 10-21 还没过完
    partial = await baseline(weeks(NOISE + [60], last=date(2026, 10, 19)))
    assert partial["reason_code"] == "partial_target"
    assert partial["reason"] == "本期数据只到 2026-10-20，还差 5 天，和完整的周比没有意义"
    # 周过完了，但表里的数据只到 10-17
    probe = fixed_prober({("orders", "order_date"): ("2025-01-01", "2026-10-17")})
    late = await analyze("baseline", snaps=[series(weeks(NOISE + [60]))], probe=probe, query="Q1", period="week",
                         measure="orders")
    assert late["reason"] == "本期数据只到 2026-10-17，还差 1 天，和完整的周比没有意义"
    gap_rows = weeks(NOISE + [100, 100])
    del gap_rows[-2]                                   # 本期的上一周没有
    gap = await baseline(gap_rows)
    assert gap["reason_code"] == "target_gap"
    zero = await baseline(weeks(NOISE + [0, 10]))
    assert zero["reason_code"] == "zero_base"
    unreadable = await baseline([["上周", 1], ["本周", 2]])
    assert unreadable["reason_code"] == "period_unreadable" and "上周" in unreadable["reason"]


async def test_daily_series_compare_with_the_same_weekday_last_week():
    start = date(2026, 7, 1)
    rows = [[(start + timedelta(days=i)).isoformat(), 100 + (i % 7) * 10] for i in range(100)]
    result = await analyze("baseline", snaps=[series(rows, cols=("day", "orders"))], query="Q1", period="day",
                           measure="orders", label="订单数", check=True)
    assert result["params"]["grain"] == "day" and result["params"]["lag"] == 7
    # 星期效应：和上周同一天比，每天的变化都是 0
    assert result["values"]["median"] == "0" and result["verdict"]["code"] == "within_band"
    assert result["phrase"].startswith("订单数比上周同一天变化 0.0%，和过去 52 天一样")
    yoy = await baseline(weeks(NOISE * 3), lag=52)            # 57 周：能和 52 周前比的历史只有 4 期
    assert yoy["reason_code"] == "history_short" and yoy["reason"] == "只有 4 期历史，至少要 8 期"
    assert (await baseline(weeks(NOISE * 4), lag=52))["status"] == "ok"
    assert (await call_error("baseline", snaps=[series(weeks(NOISE))], query="Q1", period="week", measure="orders",
                             lag=0)).code == "bad_lag"


async def test_duplicate_periods_and_given_targets():
    rows = weeks(NOISE + [101])
    error = await call_error("baseline", snaps=[series(rows + [rows[-1]])], query="Q1", period="week",
                             measure="orders")
    assert error.code == "duplicate_rows" and "在 SQL 里按 week 汇总" in error.message
    earlier = await baseline(rows, target=rows[-3][0])
    assert earlier["params"]["target"] == rows[-3][0] and earlier["values"]["n_history"] == "16"
    missing = await call_error("baseline", snaps=[series(rows)], query="Q1", period="week", measure="orders",
                               target="2020-01-06")
    assert missing.code == "period_not_found"


async def test_iso_week_labels_and_months():
    labels = [[f"2026-W{w:02d}", v] for w, v in zip(range(22, 42), NOISE + [101])]
    result = await baseline(labels)
    assert result["params"]["grain"] == "week" and result["verdict"]["code"] == "within_band"
    months = [[f"2025-{m:02d}", 100 + m] for m in range(1, 13)] + [[f"2026-{m:02d}", 112 + m] for m in range(1, 10)]
    monthly = await baseline(months)
    assert monthly["params"]["grain"] == "month" and "过去 19 个月" in monthly["phrase"]


# --------------------------------------------------------------------------
# 趋势
# --------------------------------------------------------------------------


async def test_rising_and_falling_trends_with_run_length():
    rising = await trend(weeks([100 + 3 * i + (i % 3) for i in range(12)]))
    assert rising["verdict"] == {"code": "rising", "subjects": [], "direction": "up"}
    assert rising["values"]["n"] == "12" and rising["values"]["run_length"] == "11"
    assert rising["phrase"].startswith("过去 12 周线上销售额呈上升趋势（每周约 +")
    assert "，已连续 11 周上升" in rising["phrase"]
    assert rising["settings"] == {"trend_min_points": 6, "trend_alpha": 0.05, "max_history": 52}
    assert rising["table"]["column_types"] == {"week": "text", "value": "number", "change": "number"}
    falling = await trend(weeks([200, 196, 197, 190, 185, 186, 180, 181, 175, 170]))
    assert falling["verdict"]["code"] == "falling"
    assert falling["values"]["run_length"] == "2" and "已连续" not in falling["phrase"]


async def test_no_trend_and_exact_small_sample_p():
    flat = await trend(weeks([100, 104, 98, 103, 97, 101, 99, 102]))
    assert flat["verdict"] == {"code": "no_trend", "subjects": [], "direction": None}
    assert flat["phrase"] == "过去 8 周线上销售额没有明显的上升或下降趋势"
    six = await trend(weeks([1, 2, 3, 4, 5, 6]))
    # 没有并列、n ≤ 10：精确分布，单调的 6 期双侧 p = 2 / 6!
    assert Fraction(six["values"]["p"]) == pytest.approx(Fraction(2, 720))
    assert six["verdict"]["code"] == "rising" and six["values"]["slope"] == "1"


def test_run_length_counts_consecutive_changes_from_the_end():
    assert run_length([Fraction(v) for v in (1, 2, 3, 2, 3, 4, 5)]) == (3, "up")
    assert run_length([Fraction(v) for v in (5, 4, 3)]) == (2, "down")
    assert run_length([Fraction(v) for v in (1, 2, 2)]) == (0, None)


async def test_trend_needs_enough_consecutive_points():
    rows = weeks([100 + i for i in range(10)])
    del rows[-4]                                   # 最后连着的只有 3 期
    gap = await trend(rows)
    assert gap["reason_code"] == "series_gap" and gap["reason"] == "最后连着的只有 3 期，再往前缺期（至少要连着的 6 期）"
    short = await trend(weeks([1, 2, 3, 4]))
    assert short["reason_code"] == "history_short" and short["reason"] == "只有 4 期，至少要 6 期"
    partial = await trend(weeks([100 + i for i in range(10)], last=date(2026, 10, 19)))
    assert partial["reason_code"] == "partial_target"


async def test_daily_trends_warn_about_weekday_effects():
    start = date(2026, 8, 1)
    rows = [[(start + timedelta(days=i)).isoformat(), 100 + i] for i in range(30)]
    result = await analyze("trend", snaps=[series(rows, cols=("day", "orders"))], query="Q1", period="day",
                           measure="orders")
    assert result["verdict"]["code"] == "rising"
    assert any(c["code"] == "weekday_effect" and c["text"] == "日数据有星期效应，看趋势建议按周汇总"
               for c in result["caveats"])
    assert "日数据有星期效应" in result["phrase"]


# --------------------------------------------------------------------------
# 周号按 SQL 的算法算起止（SQLite 的 strftime('%W') 不是 ISO 周）
# --------------------------------------------------------------------------

#: SQLite 按 %W 写的周：第 1 周从这一年第一个周一起，之前的几天是第 0 周，跨年那一周被切成两期
WEEKNO = "SELECT strftime('%Y-W%W', order_date) AS week, COUNT(*) AS orders FROM orders GROUP BY week ORDER BY week"


def sqlite_weeks(first, last, sql=WEEKNO):
    """真用 SQLite 按 sql 汇总：每天 100 笔订单，从 first 到 last（含）。"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE orders (order_date TEXT)")
    day = first
    while day <= last:
        db.executemany("INSERT INTO orders VALUES (?)", [(day.isoformat(),)] * 100)
        day += timedelta(days=1)
    rows = [list(r) for r in db.execute(sql)]
    db.close()
    return rows


def weekly(rows, sql=WEEKNO, **kw):
    return snap("Q1", sql, ["week", "orders"], rows, types={"week": "text", "orders": "number"}, **kw)


async def test_a_sqlite_week_that_is_not_over_is_not_a_whole_week():
    # 运行当天 10-21、数据到 10-20：SQLite 的 2026-W42 是 10-19 到 10-25，只有两天的数据（ISO 的 W42 是 10-12 到 10-18）
    rows = sqlite_weeks(date(2026, 6, 1), date(2026, 10, 20))
    assert rows[-1] == ["2026-W42", 200]
    probe = fixed_prober({("orders", "order_date"): ("2026-06-01", "2026-10-20")})
    result = await analyze("baseline", snaps=[weekly(rows)], probe=probe, query="Q1", period="week", measure="orders",
                           label="订单数")
    assert result["reason_code"] == "partial_target"
    assert result["reason"] == "本期数据只到 2026-10-20，还差 5 天，和完整的周比没有意义"
    assert result["params"]["target"] == "2026-W42"
    whole = await analyze("baseline", snaps=[weekly(rows)], probe=probe, query="Q1", period="week", measure="orders",
                          label="订单数", target="2026-W41")
    assert whole["status"] == "ok" and whole["verdict"]["code"] == "within_band"
    # 不知道方言（认不出算法）：起止为空，本期齐不齐核对不了，绝不判齐
    unknown = await analyze("baseline", snaps=[weekly(rows)], query="Q1", period="week", measure="orders",
                            label="订单数", target="2026-W41")
    note = next(c["text"] for c in unknown["caveats"] if c["code"] == "comparability")
    assert note == "没能核对本期的数据是否到齐（期次写成「年-W周号」，认不出 SQL 是按哪种周号算的，不知道是哪几天）"


async def test_sqlite_weeks_across_new_year_are_readable_and_the_cut_weeks_are_left_out():
    rows = sqlite_weeks(date(2025, 9, 29), date(2026, 3, 1))
    assert ["2025-W52", 300] in rows and ["2026-W00", 400] in rows
    probe = fixed_prober({("orders", "order_date"): ("2025-09-29", "2026-03-01")})
    ask = {"snaps": [weekly(rows)], "probe": probe, "query": "Q1", "period": "week", "measure": "orders",
           "label": "订单数"}
    trend = await analyze("trend", **ask)
    # 最后连着的整周是 2026-W01 到 W08：再往前的 2026-W00 只有 4 天
    assert trend["status"] == "ok" and trend["values"]["n"] == "8" and trend["verdict"]["code"] == "no_trend"
    assert [r[0] for r in trend["table"]["rows"]][0] == "2026-W01"
    year_end = await analyze("baseline", **ask, target="2026-W00")
    assert year_end["reason_code"] == "partial_target"
    assert year_end["reason"] == "本期只有 2026-01-01 到 2026-01-04 这 4 天，不是整周，和完整的周比没有意义"
    after = await analyze("baseline", **ask, target="2026-W01")
    assert after["reason_code"] == "target_gap"
    assert after["reason"] == "比较期（2026-W00）只有 4 天，不是整周，和它比没有意义"
    later = await analyze("baseline", **ask, target="2026-W03")
    # 历史：2025-W40 到 W51 各和上一周比（12 个），2026-W02 和 W01 比（1 个）；W01 和 W00、W00 和 2025-W52 都不算
    assert later["status"] == "ok" and later["values"]["n_history"] == "13"
    assert {"code": "partial_periods", "text": "有 2 期不到整周（跨年被切开），没有算进历史"} in later["caveats"]
    assert "跨年" not in later["phrase"]
    short = await analyze("trend", **{**ask, "snaps": [weekly([r for r in rows if r[0] <= "2026-W03"])]})
    assert short["reason_code"] == "series_gap"
    assert short["reason"] == "最后连着的只有 3 期，再往前的 2026-W00 只有 4 天，不是整周（至少要连着的 6 期）"


async def test_unreadable_periods_name_the_values_that_cannot_be_read():
    rows = [[f"2026-W{w:02d}", 100] for w in range(30, 42)] + [["上周", 100]]
    result = await baseline(rows)
    assert result["reason_code"] == "period_unreadable"
    assert result["reason"] == "期次列「week」的值认不出是哪一期（上周）：要日期、YYYY-MM、YYYY-Www、YYYY-Qn 或整数序号"
    mixed = await baseline([[f"2026-W{w:02d}", 100] for w in range(30, 42)] + [["2026-10-12", 100]])
    assert mixed["reason"] == "期次列「week」的写法不一：2026-10-12 和其余的不是同一种写法"
    missing = await analyze("baseline", snaps=[weekly([["2024-W00", 1], ["2024-W01", 2]])], probe=fixed_prober(),
                            query="Q1", period="week", measure="orders")
    assert missing["reason"] == "期次列「week」里的 2024-W00 按 SQL 里的周号算法没有这一周"


async def test_an_empty_comparison_period_is_said_as_empty_not_as_a_gap():
    rows = weeks(NOISE + [100, 101])
    rows[-2][1] = None                                 # 本期的上一周有这一行，值为空
    result = await baseline(rows)
    assert result["reason_code"] == "target_gap"
    assert result["reason"] == f"比较期（{rows[-2][0]}）的订单数为空，没有比较期的值"
    tail = weeks([100 + i for i in range(10)])
    tail[-4][1] = None
    broken = await trend(tail)
    assert broken["reason"] == f"最后连着的只有 3 期，再往前的 {tail[-4][0]} 那一期的线上销售额为空（至少要连着的 6 期）"


async def test_sequence_periods_say_completeness_cannot_be_checked():
    rows = [[i + 1, v] for i, v in enumerate([10, 12, 11, 13, 9, 10, 12, 11, 16])]
    result = await baseline(rows, basis="level")
    assert {"code": "comparability", "text": "没能核对本期的数据是否到齐（期次不是日期，不知道是哪几天）"} \
        in result["caveats"]


async def test_a_falling_trend_whose_last_weeks_rise_says_but():
    falling = await trend(weeks([200, 196, 190, 188, 181, 176, 170, 165, 160, 162, 165, 168]))
    assert falling["verdict"]["code"] == "falling" and falling["values"]["run_direction"] == "up"
    assert falling["phrase"].endswith("，但最近已连续 3 周上升")


async def test_a_period_column_named_like_a_result_column_is_a_call_error():
    rows = [[r[0], r[1]] for r in weeks(NOISE + [101])]
    named = snap("Q1", "SELECT DATE(order_date, 'weekday 0', '-6 days') AS value, COUNT(*) AS orders FROM orders "
                       "GROUP BY 1", ["value", "orders"], rows)
    error = await call_error("baseline", snaps=[named], query="Q1", period="value", measure="orders")
    assert error.code == "reserved_column" and "期次列「value」和结果表的列重名" in error.message
