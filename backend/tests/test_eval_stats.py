"""评测的统计（app/eval/stats.py；细案 01 做法 11，评测-D10）。纯计算，和手算对照。

- Wilson 95% 区间、精确 McNemar 双侧 p 值、P50 / P90；
- 总表：准确率和可追溯准确率分开报（分母只算能判的）、作答路径、出具档位、复核、look、按角色的用量、引用率、逐题 E1、
  重复 k 次的题给通过率。
和上一次比、A/B 配对表、选基准在 test_eval_compare.py。
"""
from __future__ import annotations

import pytest

import app.db.models  # noqa: F401 - conftest 按已导入的模型建表
from app.eval import compare, stats


def test_wilson_and_mcnemar_match_hand_computation():
    assert stats.wilson(8, 10) == (0.4902, 0.9433)
    assert stats.wilson(0, 0) is None
    low, high = stats.wilson(0, 5)
    assert low == 0.0 and 0.43 < high < 0.44
    assert stats.mcnemar_exact(1, 0) == 1.0
    assert stats.mcnemar_exact(6, 0) == 0.03125
    assert stats.mcnemar_exact(7, 2) == 0.179688
    assert stats.mcnemar_exact(0, 0) == 1.0
    assert stats.percentile([5, 1, 3, 2, 4], 50) == 3 and stats.percentile([5, 1, 3, 2, 4], 90) == 5
    assert stats.percentile([], 50) is None


def _row(case: str, verdict: str | None, *, path: str = "ran", status: str = "ok", repeat: int = 0, **extra) -> dict:
    scores = [{"id": "e1", "kind": "scalar", "required": True,
               "outcome": {"pass": "traced", "fail": "wrong"}.get(verdict or "", "ungradable")}]
    return {"case_id": case, "case_seq": int(case[-1]) if case[-1].isdigit() else 1, "case_hash": f"h-{case}",
            "verdict": verdict, "path": path, "status": status, "repeat": repeat, "scores": scores,
            "traced": verdict == "pass", **extra}


def test_summary_counts_only_gradable_items_and_splits_roles():
    usage = {"assistant": {"by_model": {"m": {"calls": 3, "cost_usd": 0.03}}, "calls": 3, "cost_usd": 0.03,
                           "input_tokens": 10, "output_tokens": 5, "unpriced_calls": 0},
             "writer": {"by_model": {}, "calls": 1, "cost_usd": 0.01, "input_tokens": 1, "output_tokens": 1,
                        "unpriced_calls": 1},
             "judge": {"cost_usd": 0.0, "calls": 0}}
    rows = [
        _row("c1", "pass", tier="formal", usage=usage, looks={"model": 3, "system": 0, "limit_hit": True},
             timings={"build_ms": 1000, "total_ms": 4000, "review_ms": 30_000}, review={"verdict": "annotated",
                                                                                         "severity": "degraded"},
             answer_stats={"numbers": 2, "numbers_cited": 1, "uncited_numbers": 1}, attempts=1,
             sql_match={"status": "proven", "aspects": []}),
        _row("c2", "fail", usage=usage, timings={"build_ms": 3000, "total_ms": 6000},
             sql_match={"status": "related", "aspects": ["time"]}),
        _row("c3", "ungradable"),
        _row("c4", None, path=None, status="skipped"),
    ]
    rows[0]["scores"][0]["outcome"] = "traced"
    summary = stats.summarize(rows)
    assert summary["accuracy"] == {"passed": 1, "n": 2, "rate": 0.5, "ci": stats.wilson(1, 2)}
    assert summary["traced_accuracy"]["passed"] == 1
    assert summary["verdicts"] == {"pass": 1, "fail": 1, "ungradable": 1, "skipped": 0} and summary["not_run"] == 1
    assert summary["paths"] == {"ran": 3} and summary["tiers"]["formal"] == 1 and summary["tiers"]["explore"] == 2
    assert summary["review"]["retries"] == 1 and summary["review"]["slow"] == 1
    assert summary["review"]["severity"] == {"degraded": 1}
    assert summary["looks"]["model"] == 3 and summary["looks"]["limit_hit"] == round(1 / 3, 4)
    assert summary["timings"]["build_ms"] == {"p50": 1000, "p90": 3000, "n": 2}
    assert summary["usage"]["assistant"]["calls"] == 6 and summary["usage"]["writer"]["unpriced_calls"] == 2
    assert summary["cost_usd"] == 0.08 and summary["unpriced_calls"] == 2
    assert summary["per_case_cost_usd"] == round(0.08 / 3, 6), "按评完了的题平均，没开评的不摊"
    assert summary["usage"]["gold"] == {"cost_usd": 0.0, "calls": 0}, "期望探针不调模型"
    assert summary["citations"]["rate"] == 0.5
    assert summary["e1"]["passed"] == 1 and summary["e1"]["n"] == 2 and summary["e1"]["aspects"] == {"time": 1}


def test_repeats_give_a_pass_rate_per_case():
    rows = [_row("c1", "pass", repeat=0), _row("c1", "fail", repeat=1), _row("c1", "pass", repeat=2)]
    summary = stats.summarize(rows)
    assert summary["pass_rates"]["c1"]["passed"] == 2 and summary["pass_rates"]["c1"]["n"] == 3
    assert compare.case_outcome(rows)["verdict"] == "pass", "多数决"


@pytest.mark.parametrize("n, k", [(1, 0), (3, 3)])
def test_rates_carry_their_sample_size(n, k):
    r = stats.rate(k, n)
    assert r["n"] == n and r["passed"] == k and r["ci"] == stats.wilson(k, n)
