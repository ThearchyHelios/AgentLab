"""可比性检查（细案 01 §6）：比两期之前先确认能比。三处用到：agent 单独调用（analysis_comparable）；变化拆解给了两份快照时
先做；口径卡跨两份查询算环比、同比时自动做（入口经 run 调）。波动基线、趋势判本期完整用这里的 span_coverage。

五项检查，每项 pass / warn / fail / unknown（unknown 只写「核对不了」，不判不可比）：

- 同口径：两份 SQL 一字不差 → pass；规范化（data/sql_semantics.normalize_sql）后只差字面量：把时间条件的界（data/coverage
  认出的时间谓词的界，整个表达式）和像日期的字面量换成同一个占位再比，一样就是只差日期 → pass，不一样就是比的两段不是
  两期 → fail「两边除了日期，筛选的还不一样」；规范形式不同交给口径账本的 explain_sql_diff，等价 → pass，只差时间范围、排序
  → pass，否则 fail，写出差在哪些方面（只写方面，不写 SQL 原文里的值）。两份来自不同的数据源 fail。同一份快照里按期次分组
  的天然同口径。
- 窗口长度：用 data/coverage 的 filters 取业务日期列（目录的 business_date；没写就取唯一那一列时间谓词）上的区间，界是字面
  量的直接解析，按运行当天现算的在库里算（Book 的 bound，记进 probes）；算两边天数。整月对整月、整季对整季算一样长。
  不相等：度量是流量 fail，比率、存量、没标的 warn。按期次分组的：两期粒度相同，一样长。
- 整期：窗口（或这一期）的终点和表里这一列的实际最大值（Book 的 range：现查，查不成退回剖析事实）、运行当天比；数据从
  窗口起点之后才开始的也算不完整。不完整：流量 fail（「本期数据只到 2026-10-15，还差 3 天」），其余 warn；表的范围查不到时
  只能按运行当天判，判不出的 unknown。
- 星期构成：按天的窗口、天数不是 7 的倍数时数两边的周六、周日天数，不同 warn（「两段的周末天数不同：4 天对 6 天」）。整月、
  整季、按周的不看。
- 数据问题：两边查询结果上「查询区间里这张表没有数据」（快照的 no_data）fail，SQL 检查的错误级结果（快照的 checks，level
  error）warn。

判定：有 fail → incomparable；只有 warn → conditional；其余 comparable（有核对不了的项照实写在判定语后面）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.analysis import periods as pd
from app.analysis import phrases as ph
from app.analysis.facts import Facts, date_source
from app.analysis.inputs import Snap
from app.analysis.probe import Book
from app.data.names import name_key

CHECKS = ("same_query", "window", "complete", "weekdays", "data")
CHECK_NAMES = {"same_query": "同口径", "window": "窗口长度", "complete": "整期", "weekdays": "星期构成",
               "data": "数据问题"}
STATUS_NAMES = {"pass": "通过", "warn": "提醒", "fail": "不通过", "unknown": "核对不了"}
#: explain_sql_diff 的方面 → 叫法
_ASPECTS = {"time": "时间列", "time_range": "时间范围", "filter": "筛选条件", "aggregate": "聚合方式",
            "expression": "选出来的列", "join": "关联", "tables": "查的表", "set_op": "合并方式", "group_by": "分组",
            "having": "分组后的筛选", "limit": "取哪几行", "order": "排序", "parse": "写法", "other": "写法"}
#: 只差这几个方面的两份 SQL 仍是同一个量的两期
_SAME_ASPECTS = frozenset({"time_range", "order"})
#: 一项 SQL 检查的说明最多引多少字
_CHECK_TEXT_MAX = 80


@dataclass
class Check:
    id: str
    status: str = "unknown"
    reasons: list[str] = field(default_factory=list)
    base: str | None = None
    target: str | None = None


@dataclass
class Span:
    """一边的时间范围：start / end 是窗口（或这一期）的头尾（含）；table / column 是这段时间按的那一列（探查表的范围用）；
    sql_window 是 SQL 时间条件在这一列上的区间（按期次分组时看它有没有截掉这一期）。"""

    label: str
    snap: Snap
    start: date | None = None
    end: date | None = None
    table: str | None = None
    column: str | None = None
    sql_window: Any = None
    unit: str | None = None          # 这一段是不是整月、整季、整年（calendar_unit），按期次时是粒度（不是整期的为 None）
    short: int | None = None         # 按期次的这一期不到整期时有几天（periods.short_days：跨年被切开的周）


@dataclass
class Outcome:
    checks: list[Check]
    verdict: str
    reasons: list[str]
    unknown: list[str]
    phrase: str
    structure: str | None            # 同口径不通过的原因（变化拆解据此不拆）
    base: Span | None = None
    target: Span | None = None

    def check(self, cid: str) -> Check:
        return next(c for c in self.checks if c.id == cid)


# --------------------------------------------------------------------------
# 同口径
# --------------------------------------------------------------------------


def _undated(sql: str, read: str | None) -> tuple[str, tuple[str, ...]] | None:
    """时间条件的界、像日期的字面量换成同一个占位之后的规范形式（文本 + 剩下的字面量）。解析不了为 None。"""
    from sqlglot import exp

    from app.data import coverage
    from app.data.sql_semantics import normalize_sql

    tree = coverage.parse_sql(sql, read)
    if tree is None:
        return None
    for flt in coverage.filters(tree):
        for _, node, _ in flt.conds:
            if node.parent is not None:
                node.replace(exp.Literal.string("__day__"))
    for lit in list(tree.find_all(exp.Literal)):
        if coverage.literal_bound(lit)[1] is not None:
            lit.replace(exp.Literal.string("__day__"))
    normalized = normalize_sql(tree.sql(dialect=read), read)
    if normalized.parse_error:
        return None
    return normalized.text, normalized.literals


def same_query(base: Snap, target: Snap) -> Check:
    from app.data.sql_semantics import explain_sql_diff, normalize_sql

    check = Check("same_query")
    if base.source != target.source:
        check.status, check.reasons = "fail", ["两份查询来自不同的数据源"]
        return check
    a, b = base.sql.strip(), target.sql.strip()
    if a == b:
        check.status = "pass"
        return check
    read = base.dialect or target.dialect
    na, nb = normalize_sql(a, read), normalize_sql(b, read)
    if na.parse_error or nb.parse_error:
        check.reasons = ["SQL 解析不了"]
        return check
    if na.text == nb.text:
        ua, ub = _undated(a, read), _undated(b, read)
        if ua is None or ub is None:
            return check
        if ua == ub:
            check.status = "pass"
        else:
            check.status, check.reasons = "fail", ["两边除了日期，筛选的还不一样"]
        return check
    try:
        diff = explain_sql_diff(a, b, read)
    except Exception:  # noqa: BLE001 - 比不了就当核对不了
        return check
    aspects = [d.aspect for d in diff.items]
    if diff.equivalent or (aspects and set(aspects) <= _SAME_ASPECTS):
        check.status = "pass"
        return check
    names = list(dict.fromkeys(_ASPECTS.get(x, "写法") for x in aspects if x not in _SAME_ASPECTS)) or ["写法"]
    check.status, check.reasons = "fail", [f"两份查询的口径结构不同（差在{'、'.join(names)}）"]
    return check


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------


def _bound_days(snap: Snap, conds: Sequence[tuple[str, Any, bool]], book: Book) -> dict[int, Any]:
    from app.data import coverage

    days: dict[int, Any] = {}
    for _, node, _ in conds:
        literal, day = coverage.literal_bound(node)
        if literal:
            days[id(node)] = day
            continue
        try:
            text = node.sql(dialect=snap.dialect)
        except Exception:  # noqa: BLE001
            days[id(node)] = None
            continue
        probe = book.bound(snap.source, text)
        days[id(node)] = coverage.as_day(probe.get("value")) if probe else None
    return days


def _filters_on(snap: Snap, measure: str | None, facts: Facts,
                column: tuple[str, str] | None = None) -> tuple[Any, str | None]:
    """SQL（收窄到度量那一项）里按业务日期列（或唯一那一列时间谓词，或给定的那一列）的时间条件：(那段条件, 认不出的原因)。"""
    from app.data import coverage

    tree = coverage.parse_sql(snap.sql, snap.dialect)
    if tree is None:
        return None, "SQL 解析不了"
    narrowed = coverage.narrowed(tree, measure, snap.dialect) if measure else tree
    flts = coverage.filters(narrowed)
    groups: dict[tuple[str, str], list[Any]] = {}
    for flt in flts:
        groups.setdefault((name_key(flt.table_name), name_key(flt.column_name)), []).append(flt)
    if column is not None:
        wanted = (name_key(column[0]), name_key(column[1]))
        wanted_short = (name_key(column[0].rsplit(".", 1)[-1]), wanted[1])
        chosen = groups.get(wanted) or next((v for k, v in groups.items()
                                             if (k[0].rsplit(".", 1)[-1], k[1]) == wanted_short), None)
        if chosen is None:
            return None, None
    else:
        if not groups:
            return None, "SQL 里没有时间条件"
        dated = [v for v in groups.values()
                 if (bd := facts.business_date(snap.source, v[0].table_name)) and name_key(bd) == name_key(v[0].column_name)]
        if len(dated) == 1:
            chosen = dated[0]
        elif len(groups) == 1:
            chosen = next(iter(groups.values()))
        else:
            return None, "时间条件不止一列，认不出按哪一列算"
    if len({f.key for f in chosen}) > 1:
        return None, "这一列上有好几段时间条件"
    return chosen[0], None


def sql_span(label: str, snap: Snap, measure: str | None, facts: Facts, book: Book) -> tuple[Span, str | None]:
    """一份查询的时间窗口（两份快照比两期时）。"""
    from app.data import coverage

    span = Span(label, snap)
    flt, why = _filters_on(snap, measure, facts)
    if flt is None:
        return span, why
    if flt.column_name.lower() in snap.masked:
        return span, "时间列是遮罩列"                    # 遮罩列不探表的范围
    span.table, span.column = flt.table_name, flt.column_name
    window = coverage.window_of(flt.conds, _bound_days(snap, flt.conds, book))
    if window is None or window.start is None or window.end is None:
        return span, "时间窗口没有两头"
    span.start, span.end = window.start, window.end
    span.unit = pd.calendar_unit(window.start, window.end)
    return span, None


def period_span(label: str, snap: Snap, period: Any, item: pd.Period, grain: str, measure: str | None,
                facts: Facts, book: Book) -> Span:
    """按期次分组的一份快照里的一期：这一期覆盖的日子，加上 SQL 在期次那一列上的时间条件。"""
    from app.data import coverage

    span = Span(label, snap, item.start, item.end, unit=grain, short=pd.short_days(item, grain))
    if span.short:
        span.unit = None
    found = date_source(snap.sql, period)
    if found is None:
        return span
    span.table, span.column = found
    if span.column.lower() in snap.masked:
        span.table = span.column = None
        return span
    flt, _ = _filters_on(snap, measure, facts, column=found)
    if flt is not None:
        span.sql_window = coverage.window_of(flt.conds, _bound_days(snap, flt.conds, book))
    return span


def _range(span: Span, book: Book) -> tuple[bool, date | None, date | None, bool]:
    """(查得到, 最早, 最晚, 一个值都没有)：表里这一列的实际范围。"""
    from app.data import coverage

    if not (span.table and span.column):
        return False, None, None, False
    probe = book.range(span.snap.source, span.table, span.column)
    if probe is None:
        return False, None, None, False
    lo = coverage.as_day(probe.get("min")) if probe.get("min") else None
    hi = coverage.as_day(probe.get("max")) if probe.get("max") else None
    empty = probe.get("basis") == "live" and probe.get("min") is None and probe.get("max") is None
    return True, lo.day if lo else None, hi.day if hi else None, empty


def span_coverage(span: Span, today: date, book: Book) -> tuple[str, str | None]:
    """这一段（窗口或一期）的数据齐不齐：(pass / short / unknown, 不齐的那一句)。这一期本身不到整期（跨年被切开的周）、没过完
    （终点不早于运行当天：当天的数据还没到齐）、SQL 的时间条件截掉了一部分、表里的数据没到（最晚早于终点）或来得晚（最早晚于
    起点），都算不齐。"""
    if span.start is None or span.end is None:
        return "unknown", None
    if span.short:
        return "short", f"{span.label}只有 {span.start.isoformat()} 到 {span.end.isoformat()} 这 {span.short} 天，不是整周"
    lo, hi = span.start, span.end
    window = span.sql_window
    if window is not None:
        if window.start is not None:
            lo = max(lo, window.start)
        if window.end is not None:
            hi = min(hi, window.end)
    if span.end >= today:
        hi = min(hi, today - timedelta(days=1))
    known, dmin, dmax, empty = _range(span, book)
    if empty:
        return "short", f"{span.label}在 {span.start.isoformat()} 到 {span.end.isoformat()} 没有数据"
    if dmin is not None:
        lo = max(lo, dmin)
    if dmax is not None:
        hi = min(hi, dmax)
    if hi < lo:
        return "short", f"{span.label}在 {span.start.isoformat()} 到 {span.end.isoformat()} 没有数据"
    parts = []
    if hi < span.end:
        parts.append(f"{span.label}数据只到 {hi.isoformat()}，还差 {(span.end - hi).days} 天")
    if lo > span.start:
        parts.append(f"{span.label}数据从 {lo.isoformat()} 才开始，缺前 {(lo - span.start).days} 天")
    if parts:
        return "short", "，".join(parts)
    return ("pass", None) if known and dmax is not None else ("unknown", None)


def _days(span: Span) -> int | None:
    return (span.end - span.start).days + 1 if span.start and span.end else None


def _shown(span: Span) -> str | None:
    if span.start is None or span.end is None:
        return None
    if span.start == span.end:
        return f"{span.start.isoformat()}（1 天）"
    return f"{span.start.isoformat()} 到 {span.end.isoformat()}（{_days(span)} 天）"


# --------------------------------------------------------------------------
# 五项检查和判定
# --------------------------------------------------------------------------


def _window_check(base: Span, target: Span, flow: bool, *, grouped: bool) -> Check:
    check = Check("window", base=_shown(base), target=_shown(target))
    a, b = _days(base), _days(target)
    if a is None or b is None:
        return check
    if a == b or grouped or (base.unit and base.unit == target.unit):
        check.status = "pass"
        return check
    check.status = "fail" if flow else "warn"
    check.reasons = [f"两段的天数不同：{a} 天对 {b} 天"]
    return check


def _complete_check(sides: Sequence[Span], today: date, book: Book, flow: bool) -> Check:
    check = Check("complete")
    shorts, unknown = [], False
    for span in sides:
        status, reason = span_coverage(span, today, book)
        if status == "short":
            shorts.append(reason)
        elif status == "unknown":
            unknown = True
        if span.label == "基期":
            check.base = reason or STATUS_NAMES[status if status != "short" else "fail"]
        else:
            check.target = reason or STATUS_NAMES[status if status != "short" else "fail"]
    if shorts:
        check.status, check.reasons = ("fail" if flow else "warn"), [r for r in shorts if r]
    elif not unknown:
        check.status = "pass"
    return check


def _weekday_check(base: Span, target: Span, *, grain: str | None) -> Check:
    check = Check("weekdays")
    a, b = _days(base), _days(target)
    if a is None or b is None:
        return check
    if grain in ("week", "month", "quarter", "seq") or (a % 7 == 0 and b % 7 == 0) or \
            (base.unit and base.unit == target.unit and base.unit in ("month", "quarter", "year")):
        check.status = "pass"
        return check
    wa, wb = pd.weekend_days(base.start, base.end), pd.weekend_days(target.start, target.end)  # type: ignore[arg-type]
    check.base, check.target = f"周末 {wa} 天", f"周末 {wb} 天"
    if wa == wb:
        check.status = "pass"
    else:
        check.status, check.reasons = "warn", [f"两段的周末天数不同：{wa} 天对 {wb} 天"]
    return check


def _data_check(sides: Sequence[tuple[str, Snap]], measure: str | None) -> Check:
    from app.data import coverage

    check = Check("data", status="pass")
    fails, warns = [], []
    for label, snap in sides:
        found = coverage.no_data_of(snap.data, measure) if measure else \
            next(iter(snap.data.get("no_data") or []), None)
        if found:
            fails.append(f"{label}查询的区间里这张表没有数据")
        errors = [c for c in snap.data.get("checks") or [] if isinstance(c, Mapping) and c.get("level") == "error"]
        if errors:
            text = str(errors[0].get("message") or "").strip().replace("；", "，")
            if len(text) > _CHECK_TEXT_MAX:
                text = text[:_CHECK_TEXT_MAX - 1] + "…"
            warns.append(f"{label}查询的 SQL 检查有错误：{text}" if text else f"{label}查询的 SQL 检查有错误")
    if fails:
        check.status = "fail"
    elif warns:
        check.status = "warn"
    check.reasons = fails + warns
    return check


def _decide(checks: list[Check], base: Span | None, target: Span | None) -> Outcome:
    fails = [r for c in checks if c.status == "fail" for r in c.reasons]
    warns = [r for c in checks if c.status == "warn" for r in c.reasons]
    unknown = [CHECK_NAMES[c.id] for c in checks if c.status == "unknown"]
    verdict = "incomparable" if fails else "conditional" if warns else "comparable"
    reasons = fails + warns
    structure = next((c.reasons[0] for c in checks if c.id == "same_query" and c.status == "fail" and c.reasons), None)
    return Outcome(checks, verdict, reasons, unknown, ph.comparable_phrase(verdict, reasons, unknown), structure,
                   base, target)


def compare_queries(base: Snap, target: Snap, measure: str | None, facts: Facts, book: Book, today: date) -> Outcome:
    """两份快照比两期（本周一份、上周一份）。measure 是两边的度量列（收窄 SQL 找时间条件、认度量类型）；没有为 None。"""
    same = same_query(base, target)
    b_span, _ = sql_span("基期", base, measure, facts, book)
    t_span, _ = sql_span("本期", target, measure, facts, book)
    flow = bool(measure) and facts.measure(target, measure)[0] == "flow"  # type: ignore[arg-type]
    checks = [same, _window_check(b_span, t_span, flow, grouped=False),
              _complete_check([b_span, t_span], today, book, flow), _weekday_check(b_span, t_span, grain=None),
              _data_check([("基期", base), ("本期", target)], measure)]
    return _decide(checks, b_span, t_span)


def compare_periods(snap: Snap, period: str, base: pd.Period, target: pd.Period, grain: str, measure: str | None,
                    facts: Facts, book: Book, today: date) -> Outcome:
    """同一份快照里按期次分组的两期：天然同口径；窗口按期次的粒度（有一期不到整期的按实际天数比，星期构成也照算）；整期看
    这两期各自齐不齐。期次不知道是哪几天的（序号、认不出算法的周号）窗口、整期、星期构成都核对不了。"""
    same = Check("same_query", status="pass")
    b_span = period_span("基期", snap, period, base, grain, measure, facts, book)
    t_span = period_span("本期", snap, period, target, grain, measure, facts, book)
    flow = bool(measure) and facts.measure(snap, measure)[0] == "flow"  # type: ignore[arg-type]
    whole = not (b_span.short or t_span.short)
    if grain == "seq" or base.start is None or target.start is None:
        checks = [same, Check("window"), Check("complete"), Check("weekdays"), _data_check([("", snap)], measure)]
    else:
        checks = [same, _window_check(b_span, t_span, flow, grouped=whole),
                  _complete_check([b_span, t_span], today, book, flow),
                  _weekday_check(b_span, t_span, grain=grain if whole else None), _data_check([("", snap)], measure)]
    return _decide(checks, b_span, t_span)


def period_complete(snap: Snap, period: str, item: pd.Period, grain: str, measure: str | None, facts: Facts,
                    book: Book, today: date) -> tuple[str, str | None, str | None]:
    """按期次分组的一份快照里本期齐不齐（波动基线、趋势、单份快照的变化拆解共用）：(pass / short / unknown, 不齐的那一句,
    核对不了时附带说明的那一句)。期次没有日期（序号）、周号认不出算法的，起止不知道，一律核对不了，不判齐。"""
    if item.start is None:
        return "unknown", None, ph.UNKNOWN_WEEKS if grain == "week" else ph.NO_DATES
    status, why = span_coverage(period_span("本期", snap, period, item, grain, measure, facts, book), today, book)
    return status, why, ph.UNVERIFIED_COMPLETE if status == "unknown" else None


def table_of(outcome: Outcome) -> dict[str, Any]:
    """检查表（结论的 table）：检查项、结果、基期、本期、说明。"""
    rows = [[CHECK_NAMES[c.id], STATUS_NAMES[c.status], c.base, c.target, "；".join(c.reasons) or None]
            for c in outcome.checks]
    columns = ["check", "status", "base", "target", "note"]
    return {"columns": columns,
            "labels": {"check": "检查项", "status": "结果", "base": "基期", "target": "本期", "note": "说明"},
            "rows": rows,
            "column_types": dict.fromkeys(columns, "text")}


def values_of(outcome: Outcome) -> dict[str, Any]:
    out: dict[str, Any] = {c.id: c.status for c in outcome.checks}
    for side, span in (("base", outcome.base), ("target", outcome.target)):
        if span is not None and span.start and span.end:
            out[f"{side}_start"] = span.start.isoformat()
            out[f"{side}_end"] = span.end.isoformat()
            out[f"{side}_days"] = str(_days(span))
            out[f"{side}_weekend"] = str(pd.weekend_days(span.start, span.end))
    return out


def reasons(result: Mapping[str, Any]) -> list[str]:
    """一份可比性结论里 fail、warn 的原因，一句一个（口径卡指标的 comparability.reasons 用）。按检查表的说明拆开。"""
    out: list[str] = []
    table = result.get("table") or {}
    for row in table.get("rows") or []:
        if len(row) >= 5 and row[1] in (STATUS_NAMES["fail"], STATUS_NAMES["warn"]) and row[4]:
            out.extend(str(row[4]).split("；"))
    return out


# --------------------------------------------------------------------------
# 算子：analysis_comparable
# --------------------------------------------------------------------------


def comparable(raw: Mapping[str, Any], env: Any) -> Any:
    """可比性检查这个算子。参数：两份快照 base_query、target_query；或一份快照 query + period（期次列）+ base、target（期次
    列里的两个值）、可选 grain。都可以写 measure（度量列：收窄 SQL 找时间条件、按目录认它是不是流量；不写时只有一列数值列
    就用它）。"""
    from app.analysis.common import build, need, today_param
    from app.analysis.contract import CallError
    from app.analysis.inputs import same_value

    params: dict[str, Any] = {}
    two = raw.get("base_query") is not None or raw.get("target_query") is not None
    if two:
        base = env.inputs.snap(need(raw, "base_query", "基期的查询编号"), "基期的查询编号", role="base")
        target = env.inputs.snap(need(raw, "target_query", "本期的查询编号"), "本期的查询编号",
                                 role="target")
        if base is target:
            from app.analysis.decompose import SAME_QUERY

            raise CallError("same_query", SAME_QUERY)
        params.update(base_query=base.ref, target_query=target.ref)
    else:
        base = target = env.inputs.snap(need(raw, "query", "查询编号"), "查询编号")
        params["query"] = base.ref
    measure = target.column(raw["measure"], "度量列") if raw.get("measure") is not None else _only_number(target)
    if measure is not None and two:
        base.column(measure, "度量列")
    params["measure"] = measure
    params["today"] = today_param(env)
    if two:
        env.inputs.use(base, "base", [measure] if measure else [])
        env.inputs.use(target, "target", [measure] if measure else [])
        outcome = compare_queries(base, target, measure, env.facts, env.book, env.today)
    else:
        period = target.column(need(raw, "period", "期次列"), "期次列")
        grain = raw.get("grain")
        if grain is not None and grain not in (*pd.GRAINS, "seq"):
            raise CallError("bad_grain", "粒度只能写 day、week、month、quarter，或者不写（按期次值推断）")
        params.update(period=period, base=need(raw, "base", "基期（期次列里的值）"),
                      target=need(raw, "target", "本期（期次列里的值）"))
        i = target.index(period)
        values = [row[i] if i < len(row) else None for row in target.rows]
        rows = {side: [r for r, v in enumerate(values) if same_value(params[side], v)] for side in ("base", "target")}
        for side, what in (("base", "基期"), ("target", "本期")):
            if not rows[side]:
                raise CallError("period_not_found", f"期次列「{period}」里没有「{params[side]}」：{what}要写 {target.ref} "
                                                    "里有的期次值",
                                reason=f"期次列「{period}」里没有{what}「{ph.key_text([params[side]])}」")
        env.inputs.use(target, "data", [period, *([measure] if measure else [])], rows["base"] + rows["target"])
        parsed = pd.parse(values, grain, weeks=pd.week_scheme(target.sql, period, target.dialect))
        if parsed is None:
            item_b = item_t = pd.Period(None, 0, None, None)
            grain = "seq"
        else:
            items, grain = parsed
            item_b, item_t = items[rows["base"][0]], items[rows["target"][0]]
        params["grain"] = grain
        outcome = compare_periods(target, period, item_b, item_t, grain, measure, env.facts, env.book, env.today)
    caveats = []
    window = outcome.check("window")
    if measure and window.status == "warn" and env.facts.measure(target, measure)[0] is None:
        caveats.append(ph.caveat("measure_unlabeled", ph.unlabeled_text(measure, "窗口不等只作提醒")))
    verdict = {"code": outcome.verdict, "subjects": [], "direction": None}
    return build(env, params, [], table=table_of(outcome), values=values_of(outcome), verdict=verdict,
                 criterion=ph.CRITERION_COMPARABLE, phrase=outcome.phrase, caveats=caveats)


def _only_number(snap: Snap) -> str | None:
    """没写度量列时：快照里唯一一列（没遮罩的）数值列；没有或不止一列为 None。"""
    found = [c for c in snap.columns if c.lower() not in snap.masked and snap.kind(c) == "number"]
    return found[0] if len(found) == 1 else None


__all__ = ["CHECKS", "CHECK_NAMES", "Check", "Outcome", "STATUS_NAMES", "Span", "comparable", "compare_periods",
           "compare_queries", "period_complete", "period_span", "reasons", "same_query", "span_coverage", "sql_span",
           "table_of", "values_of"]
