"""取值观察的比较器（app/data/value_drift.py，next-round 03 B2 ④）：纯函数。

守的是阈值两侧：已列全的码值表出现任何新值都算、要撤销「已列全」；没列全的新值要占 0.5% 以上、第一次只建基线；PSI 刚好落在
0.1 / 0.25 两侧；断档要超过 max(2 × 中位间隔, 3 天)；缺天要以前的窗口都没有过；量跌按前 28 天的中位数；行数估算要连续两次；
口径不同不比。取值都是虚构的。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.data import value_drift as vd

AT = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 10)


def _codes(values: dict[str, int], basis: str = "full", **extra) -> vd.Obs:
    return vd.Obs("codes_observe", basis, {"values": [[k, v] for k, v in values.items()],
                                            "rows": sum(values.values()), **extra}, AT)


def test_complete_code_table_flags_any_new_value_and_revokes_complete():
    [f] = vd.codes(_codes({"paid": 900, "void": 99, "held": 1}), [], known=["paid", "void"], complete=True)
    assert f.kind == "codes_new" and f.severity == "action" and f.revoke_complete is True
    assert f.after["codes"] == ["held"], "只出现一次也算：它说的是「只有这些取值」"
    assert vd.codes(_codes({"paid": 900, "void": 100}), [], known=["paid", "void"], complete=True) == []


def test_not_complete_needs_history_and_half_a_percent():
    base = _codes({"paid": 900, "void": 100})
    assert vd.codes(_codes({"paid": 900, "void": 95, "held": 5}), [], known=[]) == [], "第一次只建基线"
    [f] = vd.codes(_codes({"paid": 900, "void": 95, "held": 5}), [base], known=[])
    assert f.kind == "codes_new" and f.severity == "warning" and not f.revoke_complete
    assert vd.codes(_codes({"paid": 996, "void": 100, "held": 4}), [base], known=[]) == [], "不到 0.5%：不算"
    seen = _codes({"paid": 900, "void": 90, "held": 10})
    assert [x.kind for x in vd.codes(_codes({"paid": 900, "void": 95, "held": 5}), [seen, base])] == [], "历史里见过的不算新"


def test_psi_on_both_sides_of_the_thresholds():
    before = _codes({"a": 500, "b": 500})
    # PSI(0.5/0.5 → 0.6/0.4) ≈ 0.0405：不报
    assert vd.codes(_codes({"a": 600, "b": 400}), [before]) == []
    # 0.5/0.5 → 0.66/0.34 ≈ 0.1035：info
    [info] = vd.codes(_codes({"a": 660, "b": 340}), [before])
    assert info.kind == "codes_shift" and info.severity == "info" and 0.1 <= info.after["psi"] < 0.25
    # 0.5/0.5 → 0.75/0.25 ≈ 0.2747：warning，列出变化最大的取值
    [warn] = vd.codes(_codes({"a": 750, "b": 250}), [before])
    assert warn.severity == "warning" and warn.after["psi"] >= 0.25 and warn.after["top"][0][0] in ("a", "b")
    small = _codes({"a": 50, "b": 50})
    assert vd.codes(_codes({"a": 90, "b": 10}), [small]) == [], "两次都要至少 1,000 行"


def test_different_basis_is_not_compared():
    before = _codes({"a": 500, "b": 500}, basis="first_rows:1000")
    assert vd.codes(_codes({"a": 900, "b": 100}, basis="window:paid_on:35"), [before]) == []
    same, note = vd.comparable(_codes({}, basis="window:paid_on:35"), [before])
    assert same == [] and "口径不同" in note


def _fresh(day: str) -> vd.Obs:
    return vd.Obs("freshness", "full", {"max": day}, AT)


def test_stalled_date_needs_more_than_twice_the_median_interval_and_three_days():
    history = [_fresh("2026-10-05"), _fresh("2026-10-04"), _fresh("2026-10-03")]   # 每天前进一次
    assert vd.freshness(_fresh("2026-10-05"), history, today=date(2026, 10, 8)) == [], "3 天：没超过 max(2, 3)"
    [f] = vd.freshness(_fresh("2026-10-05"), history, today=date(2026, 10, 9))
    assert f.kind == "date_stalled" and f.after["age_days"] == 4 and f.after["threshold_days"] == 3
    assert vd.freshness(_fresh("2026-10-08"), history, today=date(2026, 10, 30)) == [], "前进了就不算断档"
    weekly = [_fresh("2026-09-28"), _fresh("2026-09-21")]
    assert vd.freshness(_fresh("2026-09-28"), weekly, today=date(2026, 10, 10)) == [], "每周一次：14 天内不算"
    assert vd.freshness(_fresh("2026-10-05"), [_fresh("2026-10-05")], today=date(2026, 10, 30)) == [], \
        "没见过前进，不知道它多久前进一次"


def _days(start: date, counts: list[int], basis: str = "window:paid_on:35") -> vd.Obs:
    return vd.Obs("daily_volume", basis, {"days": {(start + timedelta(days=i)).isoformat(): n
                                                   for i, n in enumerate(counts) if n}}, AT)


def test_gap_only_when_previous_windows_had_none():
    full = _days(date(2026, 9, 1), [100] * 30)
    holed = _days(date(2026, 9, 2), [100] * 10 + [0, 0] + [100] * 18)
    [f] = vd.daily(holed, [full], today=TODAY)
    assert f.kind == "date_gap" and f.after["gaps"] == 2
    assert not [x for x in vd.daily(holed, [holed], today=TODAY) if x.kind == "date_gap"], "以前就有缺天：不算新问题"
    assert not [x for x in vd.daily(holed, [], today=TODAY) if x.kind == "date_gap"], "没有历史：只建基线"


def test_volume_drop_against_the_previous_28_days():
    start = TODAY - timedelta(days=31)
    normal = _days(start, [100] * 30)                       # 最后一天是昨天
    assert vd.daily(normal, [], today=TODAY) == []
    [f] = vd.daily(_days(start, [100] * 29 + [20]), [], today=TODAY)
    assert f.kind == "volume_drop" and f.after["rows"] == 20 and f.before["median"] == 100
    assert vd.daily(_days(start, [100] * 29 + [35]), [], today=TODAY) == [], "35% 不算跌"
    partial = _days(TODAY - timedelta(days=30), [100] * 30 + [5])   # 今天还在写入：往前退一天
    assert vd.daily(partial, [], today=TODAY) == []


def test_future_business_dates_do_not_turn_today_into_the_last_full_day():
    """业务日期有未来值（最晚到明天）：最近一个整天仍是昨天，今天这个还没写完的日子不算量跌；昨天真跌了照样报。以前取
    max(days) 再退一天，max 在明天时就落到了今天（只有几行）上，记出一条量跌。"""
    start = TODAY - timedelta(days=30)                      # start + 30 是今天
    ahead = _days(start, [100] * 30 + [5, 40])              # 昨天 100、今天 5 行、明天 40 行
    assert not [f for f in vd.daily(ahead, [], today=TODAY) if f.kind == "volume_drop"]
    [f] = [f for f in vd.daily(_days(start, [100] * 29 + [20, 5, 40]), [], today=TODAY) if f.kind == "volume_drop"]
    assert f.after["day"] == (TODAY - timedelta(days=1)).isoformat() and f.after["rows"] == 20


def test_days_after_today_are_not_gaps_now_or_in_history():
    """缺天只看到今天为止：今天以后两天没数据、第三天又有（预约、计划类的日期）不算缺天；以前那次观察窗口里它自己的「今天」
    以后的空日子也不算以前有过缺天，挡不住这一次真缺的天。"""
    start = TODAY - timedelta(days=30)
    full = _days(start - timedelta(days=1), [100] * 31)
    ahead = _days(start, [100] * 31 + [0, 0, 3])            # 今天之后空两天、第三天 3 行
    assert not [f for f in vd.daily(ahead, [full], today=TODAY) if f.kind == "date_gap"]
    then = TODAY - timedelta(days=7)
    old = vd.Obs("daily_volume", "window:paid_on:35", {
        "until": then.isoformat(),
        "days": {**{(then - timedelta(days=i)).isoformat(): 100 for i in range(30)},
                 (then + timedelta(days=5)).isoformat(): 2}}, AT)  # 那次的「今天」以后空了几天、又有一行
    holed = _days(start, [100] * 10 + [0, 0] + [100] * 19)
    [gap] = [f for f in vd.daily(holed, [old], today=TODAY) if f.kind == "date_gap"]
    assert gap.after["gaps"] == 2


def _rows(n: int, method: str = "count") -> vd.Obs:
    return vd.Obs("rows", method, {"rows": n, "method": method}, AT)


def test_rows_drop_and_jump_estimates_need_two_in_a_row():
    assert [f.kind for f in vd.rows(_rows(600), [_rows(1000)])] == ["rows_drop"]
    assert vd.rows(_rows(750), [_rows(1000)]) == [], "跌 25%：不报"
    assert [f.kind for f in vd.rows(_rows(3000), [_rows(1000)])] == ["rows_jump"]
    est = lambda n: _rows(n, "stats")  # noqa: E731
    assert vd.rows(est(600), [est(1000), est(1000)]) == [], "估算值跌了一次：先不报"
    assert [f.kind for f in vd.rows(est(600), [est(650), est(1000)])] == ["rows_drop"], "连续两次都跌：报"
    assert vd.rows(_rows(600), [_rows(1000, "stats")]) == [], "数出来的和估算的口径不同：不比"
    assert vd.rows(vd.Obs("rows", "count", {"rows": None, "at_least": 1001}, AT), [_rows(5000)]) == [], \
        "只知道「至少多少行」的不比"
