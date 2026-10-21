"""放回历史看（细案 01 §3、§4）：波动基线（这个变化大不大、算不算异常）和趋势（是持续在变，还是一次波动）。

**参数**：query（一份快照：一条序列；要看某一段就在 SQL 里筛好）、period（期次列）、measure（度量列）；可选 grain（day /
week / month / quarter，缺省按期次值推断，见 analysis/periods）、label。波动基线另有 target（本期，缺省最后一期）、basis
（change 缺省：和比较期比的变化率；level：值本身，给存量、比率用）、lag（和往前第几期比，缺省日数据 7、其余 1；同比写 52 /
12 / 4，历史要相应够长）。

**前提**（不满足就算不了，原因照实写）：期次认得出（period_unreadable）、本期完整（同可比性检查的整期：没过完、数据没到、
SQL 截掉一部分 → partial_target；表的日期范围查不到时只按运行当天判，附一句核对不了）、本期和比较期之间不缺期（target_gap）、
比较期不为 0（zero_base）、历史够长（history_short）；趋势要最后几期连着（中间缺期 series_gap）。度量格为空的那一期跳过。

**波动基线**：历史的中位数 m、中位绝对偏差 MAD，σ̂ = 1.4826·MAD，稳健 z = (x − m)/σ̂；常规波动范围 m ± k_warn·σ̂；全程
精确的有理数。σ̂ = 0（历史每期都一样）时本期一样判常规、不一样判明显超出。本期的值（不论 basis）是历史各期里最高 / 最低的
加 record_high / record_low：「历史新高」说的是值本身。为什么不用均值和标准差：促销周这类极端值会把它们拉宽。

**趋势**：最后 n 期（期次连着的那一段，trend_min_points ≤ n ≤ max_history + 1）的 Theil–Sen 斜率、Mann–Kendall 双侧 p，
相对斜率 = 斜率 / |序列中位数|。p < trend_alpha 时按斜率的正负判上升、下降。末尾连续同向的期数从最后一期往前数（变化为 0
断开）。已知局限：Mann–Kendall 假设各期独立，业务序列常有自相关，p 会偏小；所以缺省 n 至少 6、α 取 0.05，判定语只说
「呈上升趋势」，不说「显著上升」。日数据附一句星期效应。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from app.analysis import comparable as cmp
from app.analysis import periods as pd
from app.analysis import phrases as ph
from app.analysis import stats
from app.analysis.common import Env, build, column_label, label_for, need, refuse, sign_word, today_param
from app.analysis.contract import CallError
from app.analysis.inputs import Snap, same_value
from app.analysis.numbers import canon, to_fraction

#: 稳健标准差 = 1.4826 × MAD（正态时等于标准差）
MAD_SCALE = Fraction(14826, 10000)
#: lag 的上限（周数据同比是 52）
_LAG_MAX = 260


@dataclass
class Series:
    snap: Snap
    period: str
    measure: str
    grain: str
    label: str
    points: list[tuple[pd.Period, Fraction]]      # 按期次排好、度量不为空的
    nulls: int
    params: dict[str, Any]


def _load(raw: Mapping[str, Any], env: Env, used: list[str]) -> Series | Any:
    """读出一条序列；算不了时直接交回 refused 的结论。"""
    snap = env.inputs.snap(need(raw, "query", "查询编号"), "查询编号")
    period = snap.column(need(raw, "period", "期次列"), "期次列")
    measure = snap.column(need(raw, "measure", "度量列"), "度量列")
    grain = raw.get("grain")
    if grain is not None and grain not in (*pd.GRAINS, "seq"):
        raise CallError("bad_grain", "粒度只能写 day、week、month、quarter，或者不写（按期次值推断）")
    params: dict[str, Any] = {"query": snap.ref, "period": period, "measure": measure}
    label = label_for(env, raw.get("label"), snap, measure)
    if raw.get("label") is not None:
        params["label"] = ph.clean_label(raw.get("label")) or None
    criterion = _criterion(env, None, "期")
    env.inputs.use(snap, "data", [period, measure])
    if snap.truncated:
        return refuse(env, params, used, "truncated", f"{snap.ref} 只取回了前 {len(snap.rows)} 行，序列不完整",
                      criterion=criterion)
    i_period = snap.index(period)
    values = [row[i_period] if i_period < len(row) else None for row in snap.rows]
    parsed = pd.parse(values, grain)
    if parsed is None:
        sample = "、".join(dict.fromkeys(ph.key_text([v]) for v in values[:3]))
        return refuse(env, params, used, "period_unreadable",
                      f"期次列「{period}」的值认不出是哪一期（{sample}）：要日期、YYYY-MM、YYYY-Www、YYYY-Qn 或整数序号",
                      criterion=criterion)
    items, grain = parsed
    params["grain"] = grain
    seen: set[int] = set()
    points: list[tuple[pd.Period, Fraction]] = []
    nulls = 0
    for r, item in enumerate(items):
        if item.ordinal in seen:
            raise CallError("duplicate_rows", ph.say(f"{snap.ref} 里同一期有不止一行：在 SQL 里按 ",
                                                     column_label(env, snap, period), " 汇总，每期一行"))
        seen.add(item.ordinal)
        value = snap.number(r, measure, "度量列")
        if value is None:
            nulls += 1
            continue
        points.append((item, value))
    points.sort(key=lambda p: p[0].ordinal)
    return Series(snap, period, measure, grain, label, points, nulls, params)


def _criterion(env: Env, n: int | None, unit: str) -> str:
    if env.op == "baseline":
        return ph.criterion_baseline(env.settings, n, unit)
    return ph.criterion_trend(env.settings, n, unit)


def _completeness(env: Env, series: Series, item: pd.Period) -> tuple[str, str | None]:
    """本期齐不齐（同可比性检查的整期）：(pass / short / unknown, 不齐的那一句)。"""
    series.params["today"] = today_param(env)
    if item.start is None:
        return "unknown", None
    span = cmp.period_span("本期", series.snap, series.period, item, series.grain, series.measure, env.facts, env.book)
    return cmp.span_coverage(span, env.today, env.book)  # type: ignore[arg-type]


def _shown_period(item: pd.Period) -> Any:
    return item.raw


# --------------------------------------------------------------------------
# 波动基线
# --------------------------------------------------------------------------


def baseline(raw: Mapping[str, Any], env: Env) -> Any:
    used = ["k_warn", "k_alert", "min_history", "max_history"]
    loaded = _load(raw, env, used)
    if not isinstance(loaded, Series):
        return loaded
    series, params = loaded, loaded.params
    basis = raw.get("basis") or "change"
    if basis not in ("change", "level"):
        raise CallError("bad_basis", "比法只能写 change（和比较期比的变化率）或 level（值本身）")
    lag = raw.get("lag", pd.DEFAULT_LAG[series.grain])
    if isinstance(lag, bool) or not isinstance(lag, int) or not 1 <= lag <= _LAG_MAX:
        raise CallError("bad_lag", f"往前比几期要写 1 到 {_LAG_MAX} 的整数（同比：周数据 52、月数据 12、季度 4）")
    params.update(basis=basis, lag=lag)
    unit, per = pd.COUNT_WORD[series.grain], pd.PER_WORD[series.grain]
    criterion = _criterion(env, None, unit)
    if not series.points:
        return refuse(env, params, used, "history_short", f"只有 0 期历史，至少要 {int(env.settings['min_history'])} 期",
                      criterion=criterion)
    if raw.get("target") is not None:
        found = next((p for p in series.points if same_value(raw["target"], p[0].raw)), None)
        if found is None:
            raise CallError("period_not_found", f"期次列「{series.period}」里没有「{raw['target']}」（或那一期为空）："
                                                "本期要写序列里有值的一期，不写就是最后一期")
    else:
        found = series.points[-1]
    target, x_level = found
    params["target"] = target.raw
    caveats: list[dict[str, str]] = []
    status, why = _completeness(env, series, target)
    if status == "short":
        return refuse(env, params, used, "partial_target", f"{why}，和完整的{per}比没有意义", criterion=criterion)
    if status == "unknown" and target.start is not None:
        caveats.append(ph.caveat("comparability", ph.UNVERIFIED_COMPLETE))
    by_ordinal = {p.ordinal: v for p, v in series.points}
    before = [(p, v) for p, v in series.points if p.ordinal < target.ordinal]
    if basis == "change":
        prev = by_ordinal.get(target.ordinal - lag)
        if prev is None:
            return refuse(env, params, used, "target_gap",
                          f"本期和它的比较期（往前第 {lag} {'期' if series.grain == 'seq' else unit}）之间缺期，没有比较期的值",
                          criterion=criterion)
        if prev == 0:
            return refuse(env, params, used, "zero_base", ph.say("比较期的", series.label, "为 0，算不了变化率"),
                          criterion=criterion)
        x = (x_level - prev) / abs(prev)
        history = [(p, (v - by_ordinal[p.ordinal - lag]) / abs(by_ordinal[p.ordinal - lag]), v) for p, v in before
                   if by_ordinal.get(p.ordinal - lag) not in (None, 0)]
    else:
        x = x_level
        history = [(p, v, v) for p, v in before]
    history = history[-int(env.settings["max_history"]):]
    n = len(history)
    criterion = _criterion(env, n, unit)
    if n < int(env.settings["min_history"]):
        return refuse(env, params, used, "history_short", f"只有 {n} 期历史，至少要 {int(env.settings['min_history'])} 期",
                      criterion=criterion)
    hv = [h for _, h, _ in history]
    m = stats.median(hv)
    sigma = MAD_SCALE * stats.mad(hv, m)
    k_warn, k_alert = env.threshold("k_warn"), env.threshold("k_alert")
    if sigma == 0:
        z = None
        code = "within_band" if x == m else "far_outside_band"
    else:
        z = (x - m) / sigma
        code = "within_band" if abs(z) < k_warn else "outside_band" if abs(z) < k_alert else "far_outside_band"
    lo, hi = m - k_warn * sigma, m + k_warn * sigma
    levels = [lv for _, _, lv in history]
    if x_level > max(levels):
        caveats.append(ph.caveat("record_high", ph.record_text(True, series.label, n, unit)))
    elif x_level < min(levels):
        caveats.append(ph.caveat("record_low", ph.record_text(False, series.label, n, unit)))
    if series.nulls:
        caveats.append(ph.caveat("null_values", ph.null_periods_text(series.nulls, series.label)))
    current = ph.current_text(x, basis=basis, lag=lag, grain=series.grain)
    if sigma == 0:
        shown = ph.pct(m) if basis == "change" else ph.amount(m)
        main = ph.baseline_flat_phrase(code, series.label, current, n, unit, per, shown)
    else:
        main = ph.baseline_phrase(code, series.label, current, n, unit, ph.band_text(lo, hi, basis=basis))
    rows = [[p.raw, canon(lv), canon(h) if basis == "change" else None, "历史"] for p, h, lv in history]
    rows.append([target.raw, canon(x_level), canon(x) if basis == "change" else None, "本期"])
    table = {"columns": [series.period, "value", "change", "role"],
             "labels": {series.period: column_label(env, series.snap, series.period), "value": series.label,
                        "change": "变化率", "role": "说明"},
             "rows": rows}
    values = {"x": canon(x), "value": canon(x_level), "median": canon(m), "band_lo": canon(lo), "band_hi": canon(hi),
              "z": canon(z) if z is not None else None, "rank": str(stats.rank_desc(x, hv)), "n_history": str(n),
              "hist_min": canon(min(hv)), "hist_max": canon(max(hv))}
    verdict = {"code": code, "subjects": [], "direction": sign_word(x - m)}
    return build(env, params, used, table=table, values=values, verdict=verdict, criterion=criterion,
                 phrase=ph.with_caveats(main, caveats, skip=("comparability",)), caveats=caveats)


# --------------------------------------------------------------------------
# 趋势
# --------------------------------------------------------------------------


def run_length(values: list[Fraction]) -> tuple[int, str | None]:
    """末尾连续同向：从最后一期往前数，相邻变化同号的有几个（变化为 0 断开）。"""
    changes = [b - a for a, b in zip(values, values[1:])]
    if not changes or changes[-1] == 0:
        return 0, None
    sign = changes[-1] > 0
    count = 0
    for c in reversed(changes):
        if c == 0 or (c > 0) != sign:
            break
        count += 1
    return count, "up" if sign else "down"


def trend(raw: Mapping[str, Any], env: Env) -> Any:
    used = ["trend_min_points", "trend_alpha", "max_history"]
    loaded = _load(raw, env, used)
    if not isinstance(loaded, Series):
        return loaded
    series, params = loaded, loaded.params
    unit, per = pd.COUNT_WORD[series.grain], pd.PER_WORD[series.grain]
    minimum = int(env.settings["trend_min_points"])
    criterion = _criterion(env, None, unit)
    if not series.points:
        return refuse(env, params, used, "history_short", f"只有 0 期，至少要 {minimum} 期", criterion=criterion)
    last = series.points[-1][0]
    caveats: list[dict[str, str]] = []
    status, why = _completeness(env, series, last)
    if status == "short":
        return refuse(env, params, used, "partial_target", f"{why}，和完整的{per}比没有意义", criterion=criterion)
    if status == "unknown" and last.start is not None:
        caveats.append(ph.caveat("comparability", ph.UNVERIFIED_COMPLETE))
    cap = int(env.settings["max_history"]) + 1
    tail = [series.points[-1]]
    for point in reversed(series.points[:-1]):
        if len(tail) >= cap or point[0].ordinal != tail[-1][0].ordinal - 1:
            break
        tail.append(point)
    tail.reverse()
    n = len(tail)
    criterion = _criterion(env, n, unit)
    if n < minimum:
        gap = len(series.points) > n and len(tail) < cap
        if gap:
            return refuse(env, params, used, "series_gap",
                          f"最后连着的只有 {n} 期，再往前缺期（至少要连着的 {minimum} 期）", criterion=criterion)
        return refuse(env, params, used, "history_short", f"只有 {n} 期，至少要 {minimum} 期", criterion=criterion)
    ys = [v for _, v in tail]
    xs = [Fraction(i) for i in range(n)]
    slope = stats.theil_sen(xs, ys)
    med = stats.median(ys)
    rel = slope / abs(med) if med != 0 else None
    mk = stats.mann_kendall(ys)
    alpha = env.threshold("trend_alpha")
    p = mk.p if isinstance(mk.p, Fraction) else to_fraction(mk.p)
    if p is not None and p < alpha and slope > 0:
        code = "rising"
    elif p is not None and p < alpha and slope < 0:
        code = "falling"
    else:
        code = "no_trend"
    run, direction = run_length(ys)
    if series.grain == "day":
        caveats.append(ph.caveat("weekday_effect", ph.WEEKDAY_EFFECT))
    if series.nulls:
        caveats.append(ph.caveat("null_values", ph.null_periods_text(series.nulls, series.label)))
    slope_text = ph.pct(rel) if rel is not None else ph.amount(slope, signed=True)
    main = ph.trend_phrase(code, series.label, n, unit, per, slope_text,
                           (run, direction) if run >= 3 and direction else None)  # type: ignore[arg-type]
    rows = [[p_.raw, canon(v), canon(v - tail[i - 1][1]) if i else None] for i, (p_, v) in enumerate(tail)]
    table = {"columns": [series.period, "value", "change"],
             "labels": {series.period: column_label(env, series.snap, series.period), "value": series.label,
                        "change": "比上一期"},
             "rows": rows}
    values = {"slope": canon(slope), "rel_slope": canon(rel) if rel is not None else None, "p": canon(mk.p),
              "s": str(mk.s), "n": str(n), "run_length": str(run), "run_direction": direction}
    verdict = {"code": code, "subjects": [], "direction": "up" if code == "rising" else "down" if code == "falling"
               else None}
    return build(env, params, used, table=table, values=values, verdict=verdict, criterion=criterion,
                 phrase=ph.with_caveats(main, caveats, skip=("comparability",)), caveats=caveats)


__all__ = ["MAD_SCALE", "baseline", "run_length", "trend"]
