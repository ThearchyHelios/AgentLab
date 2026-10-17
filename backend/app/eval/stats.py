"""评测的汇总与统计（细案 01 做法 11、12；评测-D10）。所有汇总都在后端算好，界面直接显示（界面层不放逻辑：同一个数不能
有两套算法）。纯计算，输入是一次评测的逐题结果（EvalResult 的列，result_row 的形状）。

- 准确率和可追溯准确率：分母是能判的题（通过 + 不通过）；不能评分、跳过的单列，既不算对也不算错，没跑的（停止、中断、
  到了上限、止损没开评）另列 not_run，出错的另列 errors。都带 Wilson 95% 区间和样本数——题集小、模型有随机性，差两三题就可能被读成「变好了」「变差了」（评测-D10：只展示、不卡门）。
- 作答路径、出具档位（声明了出具的按 formal / degraded / withheld，没声明的单列「探索问答」）、复核结论（verdict × severity）
  和自动重来的次数、复核用时超过问数据页等待（25 秒）的题数。
- 结论裁判（打开了才有）：supported / partial / contradicted / insufficient / 未判的句数；文字要点的判断单列。
- look：每题平均几次（分模型和系统），用满这一轮上限的比例；主模型（助手）调用次数。
- 耗时：建图、运行（墙钟、执行）、复核、整题的 P50 / P90。
- token 和成本按角色：助手 = 建图 + 挑表 + 复核；写作 = 答案运行里的模型调用（按模型分）；裁判；期望探针不调模型，为 0。
- 报告引用率：numbers_cited / numbers，另给 uncited_numbers、unresolved、unknown_entities。
- 逐题 E1：答案的查询和期望的取数等价的比例、差异方面的分布（score.sql_match，口径账本同一个比对函数）。
- 重复跑 k 次的题：每题的通过率。

McNemar（精确、双侧）给 A/B 的配对表用（compare.py）：只看「只 A 对」「只 B 对」的那几题，二项分布 p=0.5 的双侧概率。
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: 95% 区间的 z
Z95 = 1.959963984540054
#: 问数据页等复核最多这么久（毫秒，同 answer.REVIEW_PAGE_WAIT_S）
REVIEW_PAGE_WAIT_MS = 25_000
TIERS = ("formal", "degraded", "withheld", "explore")
TIMING_KEYS = ("build_ms", "run_wall_ms", "run_active_ms", "review_ms", "total_ms")


def wilson(passed: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson 区间（缺省 95%）。n 为 0 时没有区间。"""
    if n <= 0:
        return None
    p = passed / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """精确 McNemar 双侧 p 值：不一致的那几题里，「只 A 对」服从 Binomial(b + c, 0.5)。没有不一致的题时为 1。"""
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 6)


def percentile(values: Sequence[float], q: float) -> float | None:
    """最近秩法的百分位（P50、P90）。没有值时为 None。"""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    rank = max(1, math.ceil(q / 100 * len(data)))
    return data[min(rank, len(data)) - 1]


def rate(part: int, whole: int) -> dict[str, Any]:
    """{passed, n, rate, ci}：比例和它的 Wilson 区间。"""
    return {"passed": part, "n": whole, "rate": round(part / whole, 4) if whole else None, "ci": wilson(part, whole)}


def _usage_total(rows: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    from app.eval.answer import add_usage

    out = add_usage(None, None)
    for row in rows:
        out = add_usage(out, row)
    return out


def summarize(results: Sequence[Mapping[str, Any]], *, judge_on: bool = False) -> dict[str, Any]:
    """一次评测（一臂）的总表。results 是逐题结果（result_row 的形状），只算已经结束的那些。"""
    from app.eval.score import SCORER_VERSION

    done = [r for r in results if r.get("status") in ("ok", "skipped", "failed", "cancelled", "partial")]
    #: 评完了的（status=ok）；没跑的（停止、中断、到了上限、止损没开评）单列成 not_run，出错的单列成 errors（同 runner._counts）
    evaluated = [r for r in done if r.get("status") == "ok"]
    verdicts = Counter(r.get("verdict") or "none" for r in done)
    graded = [r for r in done if r.get("verdict") in ("pass", "fail")]
    passed = [r for r in graded if r.get("verdict") == "pass"]
    traced = [r for r in passed if r.get("traced")]
    outcomes = Counter(s.get("outcome") for r in done for s in r.get("scores") or []
                       if s.get("required") and s.get("kind") != "text")
    text = Counter(s.get("outcome") for r in done for s in r.get("scores") or [] if s.get("kind") == "text")
    answered = [r for r in done if r.get("path")]
    tiers = Counter((r.get("tier") or "explore") for r in answered if r.get("path") == "ran")
    reviews = [r.get("review") or {} for r in answered if r.get("review")]
    looks = [r.get("looks") or {} for r in answered]
    timings = {k: [float((r.get("timings") or {}).get(k)) for r in answered if (r.get("timings") or {}).get(k) is not None]
               for k in TIMING_KEYS}
    usage = {role: _usage_total((r.get("usage") or {}).get(role) for r in done)
             for role in ("assistant", "writer", "judge")}
    cost = round(sum(u["cost_usd"] for u in usage.values()), 6)
    unpriced = sum(int(u.get("unpriced_calls") or 0) for u in usage.values())
    stats = [((r.get("answer_stats") or {})) for r in answered]
    numbers = sum(int(s.get("numbers") or 0) for s in stats)
    cited = sum(int(s.get("numbers_cited") or 0) for s in stats)
    e1 = [r.get("sql_match") for r in done if isinstance(r.get("sql_match"), Mapping)
          and (r["sql_match"].get("status") in ("proven", "related", "none"))]
    aspects = Counter(a for m in e1 if m.get("status") == "related" for a in m.get("aspects") or [])
    claims = Counter()
    for r in done:
        for key, n in (((r.get("judge") or {}).get("claims") or {}).get("counts") or {}).items():
            claims[key] += int(n or 0)
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    for r in graded:
        by_case.setdefault(str(r.get("case_id")), []).append(r)
    repeated = {cid: rate(sum(1 for r in rows if r.get("verdict") == "pass"), len(rows))
                for cid, rows in by_case.items() if len(rows) > 1}
    return {
        "scorer_version": SCORER_VERSION,
        "items": len(results), "done": len(done),
        "cases": len({r.get("case_id") for r in results}),
        "verdicts": {k: verdicts.get(k, 0) for k in ("pass", "fail", "ungradable", "skipped")},
        "not_run": sum(1 for r in done if r.get("status") in ("cancelled", "skipped")),
        "errors": sum(1 for r in done if r.get("status") == "failed"),
        "accuracy": rate(len(passed), len(graded)),
        "traced_accuracy": rate(len(traced), len(graded)),
        "outcomes": dict(outcomes),
        "text": dict(text),
        "paths": dict(Counter(r.get("path") for r in answered)),
        "tiers": {k: tiers.get(k, 0) for k in TIERS},
        "review": {
            "verdicts": dict(Counter(str(v.get("verdict") or "ok") for v in reviews)),
            "severity": dict(Counter(str(v.get("severity")) for v in reviews if v.get("severity"))),
            "retries": sum(1 for r in answered if int(r.get("attempts") or 0) > 0),
            "slow": sum(1 for r in answered if ((r.get("timings") or {}).get("review_ms") or 0) > REVIEW_PAGE_WAIT_MS),
        },
        "judge": {"on": judge_on, "claims": dict(claims) if judge_on else None},
        "looks": {
            "model": sum(int(x.get("model") or 0) for x in looks),
            "system": sum(int(x.get("system") or 0) for x in looks),
            "per_case": round(sum(int(x.get("model") or 0) + int(x.get("system") or 0) for x in looks) / len(looks), 2)
            if looks else None,
            "limit_hit": rate(sum(1 for x in looks if x.get("limit_hit")), len(looks))["rate"] if looks else None,
        },
        "calls": {"assistant": int(usage["assistant"].get("calls") or 0),
                  "per_case": round(int(usage["assistant"].get("calls") or 0) / len(answered), 2) if answered else None},
        "timings": {k: {"p50": percentile(v, 50), "p90": percentile(v, 90), "n": len(v)} for k, v in timings.items()},
        "usage": {**usage, "gold": {"cost_usd": 0.0, "calls": 0}},
        "cost_usd": cost, "unpriced_calls": unpriced,
        # 每题平均花费（下一次开跑前的估算按它）：只按评完了的题平均，没开评的不摊。有估不出金额的调用时 cost_usd 只是估得出
        # 的那部分：一分都没估出来（模型都不在价格目录里）的为 None，不写成 0（像不花钱）；估出了一部分的照给，unpriced_calls
        # 说明它只是下限
        "per_case_cost_usd": round(cost / len(evaluated), 6) if evaluated and (cost > 0 or not unpriced) else None,
        "citations": {"numbers": numbers, "numbers_cited": cited,
                      "uncited_numbers": sum(int(s.get("uncited_numbers") or 0) for s in stats),
                      "unresolved": sum(int(s.get("unresolved") or 0) for s in stats),
                      "unknown_entities": sum(int(s.get("unknown_entities") or 0) for s in stats),
                      "rate": round(cited / numbers, 4) if numbers else None},
        "e1": {**rate(sum(1 for m in e1 if m.get("status") == "proven"), len(e1)), "aspects": dict(aspects)},
        "pass_rates": repeated,
    }


__all__ = ["REVIEW_PAGE_WAIT_MS", "TIERS", "Z95", "mcnemar_exact", "percentile", "rate", "summarize", "wilson"]
