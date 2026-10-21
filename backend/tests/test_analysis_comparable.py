"""可比性检查（app/analysis/comparable.py，细案 01 §6）：只差日期 / 差了别的筛选 / 结构不同、窗口不等（流量不可比、比率只提醒）、
本期不完整、周末天数不同、探查失败退回剖析事实、都没有就核对不了。每个用例都顺带核对复算和运行逐字段相同（复算用记下的
probes，不现查）。"""
from __future__ import annotations

from analysis_samples import analyze, fixed_prober, snap
from app.analysis import comparable_reasons

WINDOW = "SELECT SUM(amount) AS gmv FROM orders WHERE order_date >= '{a}' AND order_date < '{b}'"
RANGE = {("orders", "order_date"): ("2025-01-01", "2026-10-20")}


def period(ref, a, b, sql=WINDOW, **kw):
    return snap(ref, sql.format(a=a, b=b), ["gmv"], [[100]], types={"gmv": "number"}, **kw)


async def compare(base, target, *, probe=None, **params):
    params = {"base_query": base["ref"], "target_query": target["ref"], **params}
    return await analyze("comparable", snaps=[base, target], probe=probe, **params)


def checks(result):
    return {r[0]: r[1] for r in result["table"]["rows"]}


async def test_two_weeks_that_differ_only_in_dates_are_comparable():
    result = await compare(period("Q1", "2026-10-05", "2026-10-12"), period("Q2", "2026-10-12", "2026-10-19"),
                           probe=fixed_prober(RANGE), measure="gmv")
    assert result["method"] == "comparable@1" and result["mode"] is None
    assert result["verdict"] == {"code": "comparable", "subjects": [], "direction": None}
    assert result["phrase"] == "两期可比"
    assert checks(result) == {"同口径": "通过", "窗口长度": "通过", "整期": "通过", "星期构成": "通过", "数据问题": "通过"}
    assert result["values"]["base_days"] == "7" and result["values"]["target_start"] == "2026-10-12"
    assert result["values"]["window"] == "pass" and result["settings"] == {}
    assert result["params"]["today"] == "2026-10-21" and result["params"]["measure"] == "gmv"
    assert [p["column"] for p in result["probes"]] == ["order_date"]
    assert result["table"]["columns"] == ["check", "status", "base", "target", "note"]


async def test_other_literals_differing_mean_two_segments_not_two_periods():
    sql = WINDOW + " AND region = '{r}'"
    base = snap("Q1", sql.format(a="2026-10-05", b="2026-10-12", r="华南"), ["gmv"], [[100]])
    target = snap("Q2", sql.format(a="2026-10-12", b="2026-10-19", r="华东"), ["gmv"], [[100]])
    result = await compare(base, target, probe=fixed_prober(RANGE), measure="gmv")
    assert result["verdict"]["code"] == "incomparable"
    assert result["phrase"] == "两期不可比：两边除了日期，筛选的还不一样"
    assert comparable_reasons(result) == ["两边除了日期，筛选的还不一样"]


async def test_a_different_structure_says_where_it_differs():
    base = period("Q1", "2026-10-05", "2026-10-12")
    target = period("Q2", "2026-10-12", "2026-10-19", sql=WINDOW + " AND status = 'paid'")
    result = await compare(base, target, probe=fixed_prober(RANGE), measure="gmv")
    assert result["verdict"]["code"] == "incomparable"
    assert result["phrase"] == "两期不可比：两份查询的口径结构不同（差在筛选条件）"
    other = await compare(base, period("Q2", "2026-10-12", "2026-10-19", source="crm"), measure="gmv")
    assert "两份查询来自不同的数据源" in other["phrase"]


async def test_unequal_windows_fail_for_flows_and_only_warn_otherwise():
    base, target = period("Q1", "2026-10-05", "2026-10-12"), period("Q2", "2026-10-12", "2026-10-15")
    flow = await compare(base, target, probe=fixed_prober(RANGE), measure="gmv")       # SUM(amount)：目录标的流量
    assert flow["verdict"]["code"] == "incomparable" and checks(flow)["窗口长度"] == "不通过"
    assert "两段的天数不同：7 天对 3 天" in flow["phrase"]
    avg_sql = "SELECT AVG(amount) AS aov FROM orders WHERE order_date >= '{a}' AND order_date < '{b}'"
    ratio = await compare(period("Q1", "2026-10-05", "2026-10-12", sql=avg_sql),
                          period("Q2", "2026-10-12", "2026-10-15", sql=avg_sql), probe=fixed_prober(RANGE),
                          measure="gmv")
    assert ratio["verdict"]["code"] == "conditional" and checks(ratio)["窗口长度"] == "提醒"
    unlabeled = await compare(base, target, probe=fixed_prober(RANGE), measure="gmv", catalogs={})
    assert unlabeled["verdict"]["code"] == "conditional"
    assert any(c["code"] == "measure_unlabeled" for c in unlabeled["caveats"])


async def test_whole_months_count_as_the_same_length():
    result = await compare(period("Q1", "2026-08-01", "2026-09-01"), period("Q2", "2026-09-01", "2026-10-01"),
                           probe=fixed_prober(RANGE), measure="gmv")
    assert result["verdict"]["code"] == "comparable" and result["values"]["base_days"] == "31"


async def test_an_unfinished_period_is_not_a_whole_period():
    result = await compare(period("Q1", "2026-10-12", "2026-10-19"), period("Q2", "2026-10-19", "2026-10-26"),
                           probe=fixed_prober(RANGE), measure="gmv")
    assert result["verdict"]["code"] == "incomparable"
    assert result["phrase"] == "两期不可比：本期数据只到 2026-10-20，还差 5 天"
    late = fixed_prober({("orders", "order_date"): ("2025-01-01", "2026-10-16")})
    behind = await compare(period("Q1", "2026-10-05", "2026-10-12"), period("Q2", "2026-10-12", "2026-10-19"),
                           probe=late, measure="gmv")
    assert behind["phrase"] == "两期不可比：本期数据只到 2026-10-16，还差 2 天"


async def test_weekend_composition_of_short_windows():
    # 周一到周三 对 周五到周日
    result = await compare(period("Q1", "2026-10-05", "2026-10-08"), period("Q2", "2026-10-16", "2026-10-19"),
                           probe=fixed_prober(RANGE), measure="gmv")
    assert result["verdict"]["code"] == "conditional"
    assert result["phrase"] == "两期有条件可比：两段的周末天数不同：0 天对 2 天"


async def test_a_failed_probe_falls_back_to_profile_facts_then_says_it_cannot_check():
    facts = {"shop": {"orders": {"at": "2026-10-20T03:00:00", "date_ranges": [
        {"column": "order_date", "min": "2025-01-01", "max": "2026-10-19"}]}}}
    base, target = period("Q1", "2026-10-05", "2026-10-12"), period("Q2", "2026-10-12", "2026-10-19")
    fallback = await compare(base, target, probe=fixed_prober({}), facts=facts, measure="gmv")
    assert fallback["verdict"]["code"] == "comparable" and fallback["phrase"] == "两期可比"
    assert fallback["probes"] == [{"source": "shop", "table": "orders", "column": "order_date", "min": "2025-01-01",
                                   "max": "2026-10-19", "basis": "profile", "at": "2026-10-20"}]
    nothing = await compare(base, target, measure="gmv")
    assert nothing["verdict"]["code"] == "comparable"
    assert nothing["phrase"] == "两期可比，整期核对不了" and nothing["probes"] == []


async def test_bounds_computed_from_the_run_day_are_evaluated_in_the_database_and_recorded():
    sql = "SELECT SUM(amount) AS gmv FROM orders WHERE order_date >= date('now', '{a}') AND order_date < date('now', '{b}')"
    base = snap("Q1", sql.format(a="-16 days", b="-9 days"), ["gmv"], [[100]])
    target = snap("Q2", sql.format(a="-9 days", b="-2 days"), ["gmv"], [[90]])
    bounds = {"DATE('now', '-16 days')": "2026-10-05", "DATE('now', '-9 days')": "2026-10-12",
              "DATE('now', '-2 days')": "2026-10-19"}
    probe = fixed_prober(RANGE, bounds=bounds)
    result = await compare(base, target, probe=probe, measure="gmv")
    assert result["verdict"]["code"] == "comparable"
    assert checks(result)["同口径"] == "通过"                         # 只差按运行当天现算的界
    assert result["values"]["base_start"] == "2026-10-05" and result["values"]["target_end"] == "2026-10-18"
    exprs = [p["expr"] for p in result["probes"] if "expr" in p]
    assert exprs == ["DATE('now', '-16 days')", "DATE('now', '-9 days')", "DATE('now', '-2 days')"]
    assert all(p["basis"] == "live" for p in result["probes"])
    # 界算不出来：窗口核对不了，不判不可比
    blind = await compare(base, target, probe=fixed_prober(RANGE), measure="gmv")
    assert blind["verdict"]["code"] == "comparable" and "窗口长度" in blind["phrase"]


async def test_data_problems_on_the_snapshots():
    base = period("Q1", "2026-10-05", "2026-10-12")
    empty = period("Q2", "2026-10-12", "2026-10-19", no_data=[{"columns": ["gmv"], "table": "orders",
                                                                "column": "order_date", "message": "没有数据"}])
    result = await compare(base, empty, probe=fixed_prober(RANGE), measure="gmv")
    assert result["verdict"]["code"] == "incomparable" and "本期查询的区间里这张表没有数据" in result["phrase"]
    checked = period("Q2", "2026-10-12", "2026-10-19", checks=[{"code": "fanout_sum", "level": "error",
                                                                 "message": "关联订单明细后对订单金额求和，会重复计算"}])
    warned = await compare(base, checked, probe=fixed_prober(RANGE), measure="gmv")
    assert warned["verdict"]["code"] == "conditional"
    assert "本期查询的 SQL 检查有错误：关联订单明细后对订单金额求和，会重复计算" in warned["phrase"]


async def test_one_snapshot_grouped_by_period_is_naturally_the_same_query():
    sql = "SELECT order_date AS day, SUM(amount) AS gmv FROM orders WHERE order_date >= '2026-10-01' GROUP BY 1"
    rows = [[f"2026-10-{d:02d}", 100] for d in range(1, 20)]
    s = snap("Q1", sql, ["day", "gmv"], rows, types={"day": "text", "gmv": "number"})
    probe = fixed_prober(RANGE)
    same_weekday = await analyze("comparable", snaps=[s], probe=probe, query="Q1", period="day", base="2026-10-05",
                                 target="2026-10-12")
    assert same_weekday["verdict"]["code"] == "comparable" and same_weekday["params"]["measure"] == "gmv"
    assert same_weekday["params"]["grain"] == "day"
    weekend = await analyze("comparable", snaps=[s], probe=probe, query="Q1", period="day", base="2026-10-09",
                            target="2026-10-10")
    assert weekend["phrase"] == "两期有条件可比：两段的周末天数不同：0 天对 1 天"
    # 期次有一截落在 SQL 的时间条件之外：基期被截掉了；本期（10 月）还没过完
    monthly_rows = [["2026-09-01", 100], ["2026-10-01", 90]]
    month = snap("Q1", "SELECT DATE(order_date, 'start of month') AS month, SUM(amount) AS gmv FROM orders "
                       "WHERE order_date >= '2026-09-05' GROUP BY 1", ["month", "gmv"], monthly_rows)
    partial = await analyze("comparable", snaps=[month], probe=probe, query="Q1", period="month", base="2026-09-01",
                            target="2026-10-01", measure="gmv")
    assert partial["verdict"]["code"] == "incomparable"
    assert "基期数据从 2026-09-05 才开始，缺前 4 天" in partial["phrase"]
    assert "本期数据只到 2026-10-20，还差 11 天" in partial["phrase"]


async def test_reasons_are_split_one_sentence_each():
    result = await compare(period("Q1", "2026-10-05", "2026-10-08"), period("Q2", "2026-10-16", "2026-10-21"),
                           probe=fixed_prober(RANGE), measure="gmv")
    reasons = comparable_reasons(result)
    assert "两段的天数不同：3 天对 5 天" in reasons and "本期数据只到 2026-10-20，还差 0 天" not in reasons
    assert all("；" not in r for r in reasons)
    assert comparable_reasons({"table": None}) == []


async def test_a_masked_date_column_is_neither_used_nor_probed():
    probe = fixed_prober(RANGE)
    result = await compare(period("Q1", "2026-10-05", "2026-10-12", masked=["order_date"]),
                           period("Q2", "2026-10-12", "2026-10-19", masked=["order_date"]), probe=probe, measure="gmv")
    assert probe.calls == [] and result["probes"] == []
    assert result["verdict"]["code"] == "comparable" and "窗口长度" in result["phrase"]
