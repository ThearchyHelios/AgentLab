"""和上一次比、A/B 对照（细案 01 做法 12；评测-D10）。纯计算的部分（transitions、pair_table、fingerprint_diff）不读库；
选基准（pick_baseline）读库。

**选基准**：发起时点名的基线（baseline_id）> 同题集里钉住的基线（pinned，同一变体）> 同题集、同一变体（variants.variant_key）
里比这次先发起、已经结束（succeeded / partial）的最近一次。按发起先后，不按结束时间（同 api/compare.pick_base）。

**逐题转移**：每题按题目 id 配对，k 次重复的按多数（通过的次数多于不通过的算通过）。通过→不通过是退步，不通过→通过是进步，
其余不变；题目改过（case_hash 不同）、一边没有、一边不能判的标「不可比」，不算进步也不算退步。

**归因**（只对转移了的题）：期望值变了是「数据变」；期望没变、答案执行的 SQL 规范文本不同是「做法变」；只有字面量不同是
「只参数变」（多半是时间范围理解变了）；作答路径变了是「路径变」；其余「说不清」。SQL 的 same / params_only / changed 和运行
对比（engine/compare）是同一条规则：data/sql_semantics.normalize_sql 的规范文本和字面量。

**指纹差异**：助手模型、写作模型、目录快照、分析工具（开、关；加这一项之前的评测按开）、提示词指纹、表结构、题目、
评分规则、分析设置（有分析期望的题集开评时拍的那一份判定阈值），哪几项变了。

**A/B**：同一次发起的几臂（共用 pair_id、共用期望）逐题配对，给两臂各自的准确率、配对表（都对 / 都错 / 只 A 对 / 只 B 对）和
精确 McNemar 双侧 p 值；p ≥ 0.05 时照实写「这批题上看不出差别」。不能判的题不进配对表（单列个数）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TRANSITIONS = ("regressed", "improved", "same", "incomparable")
TRANSITION_LABEL = {"regressed": "退步", "improved": "进步", "same": "不变", "incomparable": "不可比"}
CAUSES = ("data", "method", "params", "path", "unknown")
CAUSE_LABEL = {"data": "数据变", "method": "做法变", "params": "只参数变", "path": "路径变", "unknown": "说不清"}
#: 指纹里比的几项和叫法
FINGERPRINT_LABEL = {"assistant_model": "助手模型", "writer_model": "写作模型", "catalog": "目录快照",
                     "analysis_tools": "分析工具", "prompt": "提示词", "schemas": "表结构", "cases": "题目",
                     "scorer_version": "评分规则", "analysis": "分析设置"}
#: 指纹里照实写前后两边的几项（别的只说变没变）
_SHOWN = ("assistant_model", "writer_model", "catalog", "analysis_tools", "scorer_version")
NO_DIFFERENCE = "这批题上看不出差别"
SIGNIFICANT = 0.05
#: 基准能是哪几种状态
BASELINE_STATUSES = ("succeeded", "partial")


# ==========================================================================
# 选基准
# ==========================================================================


async def pick_baseline(session: AsyncSession, run: Any, *, explicit: str | None = None) -> Any | None:
    """这一次和哪一次比（见模块说明）。explicit 是发起时点名的（要同一个题集）。"""
    from app.db.models_eval import EvalRun

    if explicit:
        row = await session.get(EvalRun, explicit)
        return row if row is not None and row.suite_id == run.suite_id and row.id != run.id else None
    key = (run.fingerprint or {}).get("variant_key")
    rows = (await session.execute(
        select(EvalRun).where(EvalRun.suite_id == run.suite_id, EvalRun.id != run.id,
                              EvalRun.created_at < run.created_at, EvalRun.status.in_(BASELINE_STATUSES))
        .order_by(EvalRun.created_at.desc(), EvalRun.id.desc()).limit(200))).scalars().all()
    same = [r for r in rows if (r.fingerprint or {}).get("variant_key") == key]
    pinned = next((r for r in same if r.pinned), None)
    return pinned or (same[0] if same else None)


# ==========================================================================
# 逐题
# ==========================================================================


def case_outcome(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """一道题（几次重复）合成一个结论：{verdict: pass|fail|none, passed, graded, path, gold, sql}。多数决；能判的一次都没有
    为 none。gold 是期望值的指纹（作答前那一份），sql 是答案执行的查询的规范化（第一次能判的那一次）。"""
    graded = [r for r in results if r.get("verdict") in ("pass", "fail")]
    passed = sum(1 for r in graded if r.get("verdict") == "pass")
    verdict = "none" if not graded else "pass" if passed > len(graded) - passed else "fail"
    first = graded[0] if graded else (results[0] if results else {})
    return {"verdict": verdict, "passed": passed, "graded": len(graded), "path": first.get("path"),
            "gold": first.get("gold_key"), "sql": first.get("sql_shapes") or [],
            "case_hash": first.get("case_hash"), "case_seq": first.get("case_seq")}


def sql_change(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> str:
    """两次答案执行的查询（[{text, literals}]，normalize_sql 的 as_dict）：same / params_only / changed。"""
    if [x.get("text") for x in a] != [x.get("text") for x in b]:
        return "changed"
    return "params_only" if [x.get("literals") for x in a] != [x.get("literals") for x in b] else "same"


def cause(base: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    if base.get("gold") != target.get("gold"):
        return "data"
    if base.get("path") != target.get("path"):
        return "path"
    change = sql_change(base.get("sql") or [], target.get("sql") or [])
    return {"changed": "method", "params_only": "params"}.get(change, "unknown")


def transitions(base: Sequence[Mapping[str, Any]], target: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """逐题转移和归因。base、target 是两次评测（同一臂）的逐题结果。"""
    def by_case(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
        out: dict[str, list[Mapping[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r.get("case_id")), []).append(r)
        return out

    was, now = by_case(base), by_case(target)
    items = []
    counts = {k: 0 for k in TRANSITIONS}
    causes = {k: 0 for k in CAUSES}
    for case_id in list(dict.fromkeys([*now, *was])):
        b = case_outcome(was.get(case_id, []))
        t = case_outcome(now.get(case_id, []))
        item: dict[str, Any] = {"case_id": case_id, "case_seq": t.get("case_seq") or b.get("case_seq"),
                                "before": b["verdict"], "after": t["verdict"]}
        if case_id not in was or case_id not in now:
            item.update(transition="incomparable", reason="这道题只在一边评过")
        elif b.get("case_hash") != t.get("case_hash"):
            item.update(transition="incomparable", reason="题目改过，不可比")
        elif "none" in (b["verdict"], t["verdict"]):
            item.update(transition="incomparable", reason="有一边不能判")
        elif b["verdict"] == t["verdict"]:
            item.update(transition="same")
        else:
            item.update(transition="regressed" if b["verdict"] == "pass" else "improved")
            item["cause"] = cause(b, t)
            causes[item["cause"]] += 1
        counts[item["transition"]] += 1
        items.append(item)
    return {"counts": counts, "causes": causes, "items": items}


def _tools(fingerprint: Mapping[str, Any]) -> str:
    """分析工具这一项给人看的样子：开 / 关。加这一项之前的评测指纹里没有它，那时一直开着。"""
    from app.eval.variants import ANALYSIS_TOOLS_LABEL

    return ANALYSIS_TOOLS_LABEL["off" if fingerprint.get("analysis_tools") == "off" else "on"]


def fingerprint_diff(base: Mapping[str, Any], target: Mapping[str, Any]) -> list[dict[str, Any]]:
    """两次之间指纹里哪几项变了：[{key, label, before, after}]（表结构、题目只说变没变，不展开；分析工具写开、关）。"""
    out = []
    for key, label in FINGERPRINT_LABEL.items():
        a, b = (_tools(base), _tools(target)) if key == "analysis_tools" else (base.get(key), target.get(key))
        if a == b:
            continue
        item: dict[str, Any] = {"key": key, "label": label}
        if key in _SHOWN:
            item.update(before=a, after=b)
        out.append(item)
    return out


# ==========================================================================
# A/B
# ==========================================================================


def pair_table(arms: Sequence[tuple[str, Sequence[Mapping[str, Any]]]]) -> dict[str, Any]:
    """几臂（[(名字, 逐题结果)]）两两配对：{arms: [{arm, accuracy}], pairs: [{a, b, both, neither, only_a, only_b,
    excluded, p_value, note}]}。"""
    from app.eval.stats import rate

    outcomes: dict[str, dict[str, Mapping[str, Any]]] = {}
    summary = []
    for name, rows in arms:
        cases: dict[str, list[Mapping[str, Any]]] = {}
        for r in rows:
            cases.setdefault(str(r.get("case_id")), []).append(r)
        outcomes[name] = {cid: case_outcome(rs) for cid, rs in cases.items()}
        graded = [r for r in rows if r.get("verdict") in ("pass", "fail")]
        summary.append({"arm": name, "accuracy": rate(sum(1 for r in graded if r.get("verdict") == "pass"), len(graded))})
    pairs = []
    names = [n for n, _ in arms]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = neither = only_a = only_b = excluded = 0
            for cid in dict.fromkeys([*outcomes[a], *outcomes[b]]):
                x, y = outcomes[a].get(cid), outcomes[b].get(cid)
                if x is None or y is None or "none" in (x["verdict"], y["verdict"]):
                    excluded += 1
                    continue
                pa, pb = x["verdict"] == "pass", y["verdict"] == "pass"
                both += pa and pb
                neither += not pa and not pb
                only_a += pa and not pb
                only_b += pb and not pa
            from app.eval.stats import mcnemar_exact

            p = mcnemar_exact(only_a, only_b)
            pairs.append({"a": a, "b": b, "both": both, "neither": neither, "only_a": only_a, "only_b": only_b,
                          "excluded": excluded, "p_value": p, "note": NO_DIFFERENCE if p >= SIGNIFICANT else None})
    return {"arms": summary, "pairs": pairs}


__all__ = ["CAUSES", "CAUSE_LABEL", "FINGERPRINT_LABEL", "NO_DIFFERENCE", "TRANSITIONS", "TRANSITION_LABEL",
           "case_outcome", "cause", "fingerprint_diff", "pair_table", "pick_baseline", "sql_change", "transitions"]
