"""取值观察的比较器（docs/design/next-round/03-数据接入与目录维护.md B2 ④）：纯函数，不连库。

**为什么要有它。** 确认过的码值列以前不再取分布，新状态永远发现不了；大表只看最老的前 10 万行；没有最新业务日期、按日
行数、总行数的历史。剖析引擎的四类观察检查（data/catalog_profile_observe）把每次看到的记进 table_observations，这里拿这一次
和**同一口径**（basis_key 相同）的历史比，得出取值变化（写进 source_changes）。口径不同不比、照实说（comparable）。

规则（阈值第一版，要用真实库跑一段时间再调）：

- **新码值**（codes_new）：码值表标了「已列全」的，出现任何一个目录里没有的取值都算——数据证明它不再列全，这是机器唯一能
  写进目录的事（撤销「已列全」，revoke_complete），严重程度是「要处理」。没列全的，新值要占非空行的至少 0.5%，而且要有同
  口径的历史（第一次只建基线），已见过的（目录里的、历史里出现过的）不算新。
- **比例剧变**（codes_shift）：和上一次同口径的观察算 PSI，两次都至少 1,000 行、都没被截断；PSI ≥ 0.25 记 warning，0.1–0.25
  记 info，列出变化最大的几个取值。
- **断档**（date_stalled）：最新业务日期没前进，且这个日期离今天超过 max(2 × 以前前进的中位间隔, 3 天)；要见过至少一次
  前进才知道它多久前进一次。
- **缺天**（date_gap）：窗口里（有数据的第一天到最后一天之间）有日子没数据，而以前同口径的窗口都没有过。
- **量跌**（volume_drop）：最近一个整天的行数不到它前面 28 天中位数的 30%（前 28 天至少 14 天有数据、中位数至少 20 行）。
  最后一天和观察当天同一天（还在写入）时往前退一天。只看这一次的窗口，不需要历史。
- **行数**（rows_drop / rows_jump）：比上一次同口径的少了 30% 以上 / 涨到 3 倍以上；依据是数据库统计的估算值时，要连续
  两次都这样（和跌、涨之前那一次比）才报。数到上限、只知道「至少多少行」的不比。
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

NEW_CODE_SHARE = 0.005
PSI_MIN_ROWS = 1000
PSI_WARN = 0.25
PSI_INFO = 0.1
PSI_EPS = 1e-4
PSI_TOP = 5
STALL_MIN_DAYS = 3
VOLUME_RATIO = 0.3
VOLUME_BASELINE_DAYS = 28
VOLUME_MIN_DAYS = 14
VOLUME_MIN_MEDIAN = 20
ROWS_DROP = 0.3
ROWS_JUMP = 3.0
GAPS_SHOWN = 10


@dataclass(frozen=True)
class Obs:
    """一次观察：kind、口径（basis_key）、观察到的（data）、什么时候（at）。"""

    kind: str
    basis_key: str
    data: Mapping[str, Any]
    at: datetime | None = None


@dataclass(frozen=True)
class Finding:
    """比出来的一条取值变化。key 是去重指纹的一部分；revoke_complete：要撤销码值项的「已列全」。"""

    kind: str
    severity: str
    before: Any = None
    after: Any = None
    key: Any = None
    revoke_complete: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def comparable(current: Obs, history: Iterable[Obs]) -> tuple[list[Obs], str | None]:
    """同口径的历史（新的在前，照传进来的顺序）和一句说明：有历史、但口径都不同时说「口径不同，不比」；没有历史为 None。"""
    items = list(history)
    same = [h for h in items if h.basis_key == current.basis_key]
    if items and not same:
        return [], f"以前的观察口径不同（{items[0].basis_key} → {current.basis_key}），这一次只建新口径的基线、不比"
    return same, None


def _counts(data: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pair in data.get("values") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[1], (int, float)):
            out[str(pair[0])] = out.get(str(pair[0]), 0) + int(pair[1])
    return out


def _share(n: int, total: int) -> float:
    return round(n / total, 4) if total else 0.0


def psi(before: Mapping[str, int], after: Mapping[str, int]) -> float:
    """两个分布的 PSI（群体稳定性指数）：Σ (q − p) · ln(q / p)，p、q 是各取值占比，没出现的取值按 PSI_EPS 算。"""
    total_b, total_a = sum(before.values()), sum(after.values())
    if not total_b or not total_a:
        return 0.0
    value = 0.0
    for code in set(before) | set(after):
        p = max(before.get(code, 0) / total_b, PSI_EPS)
        q = max(after.get(code, 0) / total_a, PSI_EPS)
        value += (q - p) * math.log(q / p)
    return value


def codes(current: Obs, history: Iterable[Obs], *, known: Iterable[str] = (), complete: bool = False) -> list[Finding]:
    """码值列的这一次和同口径历史比：新码值、比例剧变。known 是目录里码值项列出的取值；complete 是它标了「已列全」。"""
    same, _note = comparable(current, history)
    now = _counts(current.data)
    rows = int(current.data.get("rows") or sum(now.values()))
    listed = {str(k) for k in known}
    out: list[Finding] = []
    if complete:
        fresh = [c for c in now if c not in listed]
        if fresh:
            out.append(Finding("codes_new", "action", before={"known": sorted(listed), "complete": True},
                               after={"codes": fresh, "shares": {c: _share(now[c], rows) for c in fresh}},
                               key=tuple(sorted(fresh)), revoke_complete=True))
    elif same and not current.data.get("truncated") and not same[0].data.get("truncated"):
        seen = listed | {c for h in same for c in _counts(h.data)}
        fresh = [c for c, n in now.items() if c not in seen and rows and n / rows >= NEW_CODE_SHARE]
        if fresh:
            out.append(Finding("codes_new", "warning", before={"known": sorted(seen)},
                               after={"codes": fresh, "shares": {c: _share(now[c], rows) for c in fresh}},
                               key=tuple(sorted(fresh))))
    if same:
        prev = same[0]
        then = _counts(prev.data)
        prev_rows = int(prev.data.get("rows") or sum(then.values()))
        if (rows >= PSI_MIN_ROWS and prev_rows >= PSI_MIN_ROWS and not current.data.get("truncated")
                and not prev.data.get("truncated")):
            value = psi(then, now)
            if value >= PSI_INFO:
                top = sorted(set(then) | set(now),
                             key=lambda c: -abs(_share(now.get(c, 0), rows) - _share(then.get(c, 0), prev_rows)))
                moved = [[c, _share(then.get(c, 0), prev_rows), _share(now.get(c, 0), rows)] for c in top[:PSI_TOP]]
                out.append(Finding("codes_shift", "warning" if value >= PSI_WARN else "info",
                                   before={"rows": prev_rows}, after={"psi": round(value, 4), "rows": rows, "top": moved},
                                   key=(current.basis_key, current.at.isoformat() if current.at else None)))
    return out


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def freshness(current: Obs, history: Iterable[Obs], *, today: date) -> list[Finding]:
    """最新业务日期没前进、而且离今天太久（断档）。"""
    same, _note = comparable(current, history)
    now = _day(current.data.get("max"))
    if now is None or not same:
        return []
    previous = [d for d in (_day(h.data.get("max")) for h in same) if d is not None]
    if not previous or now > max(previous):
        return []
    distinct = sorted(set(previous) | {now})
    gaps = [(b - a).days for a, b in zip(distinct, distinct[1:])]
    if not gaps:
        return []
    median = statistics.median(gaps)
    threshold = max(2 * median, STALL_MIN_DAYS)
    age = (today - now).days
    if age <= threshold:
        return []
    return [Finding("date_stalled", "warning", before={"max": max(previous).isoformat(), "median_days": median},
                    after={"max": now.isoformat(), "age_days": age, "threshold_days": threshold},
                    key=now.isoformat())]


def _series(data: Mapping[str, Any]) -> dict[date, int]:
    out: dict[date, int] = {}
    for raw, n in (data.get("days") or {}).items():
        d = _day(raw)
        if d is not None and isinstance(n, (int, float)):
            out[d] = out.get(d, 0) + int(n)
    return out


def _holes(days: Mapping[date, int]) -> list[date]:
    if not days:
        return []
    first, last = min(days), max(days)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)
            if days.get(first + timedelta(days=i), 0) == 0]


def daily(current: Obs, history: Iterable[Obs], *, today: date) -> list[Finding]:
    """按日行数：缺天（要和同口径的历史比）、量跌（只看这一次的窗口）。"""
    same, _note = comparable(current, history)
    days = _series(current.data)
    out: list[Finding] = []
    holes = _holes(days)
    if holes and same and all(not _holes(_series(h.data)) for h in same):
        out.append(Finding("date_gap", "warning", before={"gaps": 0},
                           after={"gaps": len(holes), "days": [d.isoformat() for d in holes[:GAPS_SHOWN]]},
                           key=tuple(d.isoformat() for d in holes)))
    if days:
        last = max(days)
        if last >= today:
            last -= timedelta(days=1)
        base = [days.get(last - timedelta(days=i), 0) for i in range(1, VOLUME_BASELINE_DAYS + 1)]
        present = sum(1 for n in base if n > 0)
        median = statistics.median(base) if base else 0
        value = days.get(last, 0)
        if present >= VOLUME_MIN_DAYS and median >= VOLUME_MIN_MEDIAN and value < VOLUME_RATIO * median:
            out.append(Finding("volume_drop", "warning", before={"median": median, "days": VOLUME_BASELINE_DAYS},
                               after={"day": last.isoformat(), "rows": value, "ratio": round(value / median, 4)},
                               key=last.isoformat()))
    return out


def _exact(data: Mapping[str, Any]) -> int | None:
    rows = data.get("rows")
    return int(rows) if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0 else None


def rows(current: Obs, history: Iterable[Obs]) -> list[Finding]:
    """行数：跌 30% 以上、涨到 3 倍以上。统计估算的要连续两次（都和跌、涨之前那一次比）。"""
    same, _note = comparable(current, history)
    now = _exact(current.data)
    if now is None or not same:
        return []
    estimated = current.data.get("method") == "stats"
    if estimated:
        if len(same) < 2:
            return []
        prev, ref = _exact(same[0].data), _exact(same[1].data)
        if prev is None or ref is None or not ref:
            return []
        dropped = now <= (1 - ROWS_DROP) * ref and prev <= (1 - ROWS_DROP) * ref
        jumped = now >= ROWS_JUMP * ref and prev >= ROWS_JUMP * ref
    else:
        ref = _exact(same[0].data)
        if ref is None or not ref:
            return []
        dropped = now <= (1 - ROWS_DROP) * ref
        jumped = now >= ROWS_JUMP * ref
    if dropped:
        return [Finding("rows_drop", "warning", before={"rows": ref}, after={"rows": now, "estimated": estimated},
                        key=(current.basis_key, ref))]
    if jumped:
        return [Finding("rows_jump", "info", before={"rows": ref}, after={"rows": now, "estimated": estimated},
                        key=(current.basis_key, ref))]
    return []


def compare(current: Obs, history: Iterable[Obs], *, today: date, known: Iterable[str] = (),
            complete: bool = False) -> list[Finding]:
    """按种类分派。"""
    if current.kind == "codes_observe":
        return codes(current, history, known=known, complete=complete)
    if current.kind == "freshness":
        return freshness(current, history, today=today)
    if current.kind == "daily_volume":
        return daily(current, history, today=today)
    if current.kind == "rows":
        return rows(current, history)
    return []


__all__ = ["Finding", "NEW_CODE_SHARE", "Obs", "PSI_INFO", "PSI_MIN_ROWS", "PSI_WARN", "codes", "comparable",
           "compare", "daily", "freshness", "psi", "rows"]
