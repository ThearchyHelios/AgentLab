"""评测的统计与对比（app/eval/stats.py、app/eval/compare.py；细案 01 做法 11、12，评测-D10）。纯计算，和手算对照。

- Wilson 95% 区间、精确 McNemar 双侧 p 值、P50 / P90；
- 总表：准确率和可追溯准确率分开报（分母只算能判的）、作答路径、出具档位、复核、look、按角色的用量、引用率、逐题 E1、
  重复 k 次的题给通过率；
- 和上一次比：逐题转移（退步 / 进步 / 不变 / 不可比）和归因（数据变、做法变、只参数变、路径变），列出指纹里哪几项变了；
- A/B 配对表：都对 / 都错 / 只 A 对 / 只 B 对，不能判的不进表；
- 选基准：点名的 > 同一变体里钉住的 > 同一变体、比这次先发起、已经结束的最近一次。
"""
from __future__ import annotations

from datetime import timedelta

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
    assert summary["verdicts"] == {"pass": 1, "fail": 1, "ungradable": 1, "skipped": 0}
    assert summary["paths"] == {"ran": 3} and summary["tiers"]["formal"] == 1 and summary["tiers"]["explore"] == 2
    assert summary["review"]["retries"] == 1 and summary["review"]["slow"] == 1
    assert summary["review"]["severity"] == {"degraded": 1}
    assert summary["looks"]["model"] == 3 and summary["looks"]["limit_hit"] == round(1 / 3, 4)
    assert summary["timings"]["build_ms"] == {"p50": 1000, "p90": 3000, "n": 2}
    assert summary["usage"]["assistant"]["calls"] == 6 and summary["usage"]["writer"]["unpriced_calls"] == 2
    assert summary["cost_usd"] == 0.08 and summary["unpriced_calls"] == 2
    assert summary["usage"]["gold"] == {"cost_usd": 0.0, "calls": 0}, "期望探针不调模型"
    assert summary["citations"]["rate"] == 0.5
    assert summary["e1"]["passed"] == 1 and summary["e1"]["n"] == 2 and summary["e1"]["aspects"] == {"time": 1}


def test_repeats_give_a_pass_rate_per_case():
    rows = [_row("c1", "pass", repeat=0), _row("c1", "fail", repeat=1), _row("c1", "pass", repeat=2)]
    summary = stats.summarize(rows)
    assert summary["pass_rates"]["c1"]["passed"] == 2 and summary["pass_rates"]["c1"]["n"] == 3
    assert compare.case_outcome(rows)["verdict"] == "pass", "多数决"


def test_transitions_and_their_causes():
    sql_a = [{"text": "SELECT SUM(x) FROM t WHERE d = ?", "literals": ["'2026-09-01'"]}]
    sql_b = [{"text": "SELECT SUM(x) FROM t WHERE d = ?", "literals": ["'2026-08-01'"]}]
    sql_c = [{"text": "SELECT SUM(x) FROM t WHERE e = ?", "literals": ["'2026-09-01'"]}]
    base = [_row("c1", "pass", gold_key="g1", sql_shapes=sql_a), _row("c2", "pass", gold_key="g2", sql_shapes=sql_a),
            _row("c3", "fail", gold_key="g3", sql_shapes=sql_a), _row("c4", "pass", gold_key="g4", path="ran"),
            _row("c5", "pass", gold_key="g5"), _row("c6", "fail", gold_key="g6")]
    target = [_row("c1", "fail", gold_key="g1-new", sql_shapes=sql_a), _row("c2", "fail", gold_key="g2", sql_shapes=sql_c),
              _row("c3", "pass", gold_key="g3", sql_shapes=sql_b), _row("c4", "fail", gold_key="g4", path="reply"),
              {**_row("c5", "fail", gold_key="g5"), "case_hash": "changed"}, _row("c6", "ungradable", gold_key="g6"),
              _row("c7", "pass")]
    moved = compare.transitions(base, target)
    by = {i["case_id"]: i for i in moved["items"]}
    assert (by["c1"]["transition"], by["c1"]["cause"]) == ("regressed", "data")
    assert (by["c2"]["transition"], by["c2"]["cause"]) == ("regressed", "method")
    assert (by["c3"]["transition"], by["c3"]["cause"]) == ("improved", "params")
    assert (by["c4"]["transition"], by["c4"]["cause"]) == ("regressed", "path")
    assert by["c5"]["transition"] == "incomparable" and "题目改过" in by["c5"]["reason"]
    assert by["c6"]["transition"] == "incomparable" and by["c7"]["transition"] == "incomparable"
    assert moved["counts"] == {"regressed": 3, "improved": 1, "same": 0, "incomparable": 3}
    assert compare.sql_change(sql_a, sql_a) == "same"


def test_fingerprint_diff_lists_what_changed():
    a = {"assistant_model": "m1", "writer_model": None, "catalog": "live:aaa", "prompt": "p1", "schemas": {"s": "x"},
         "cases": "c", "scorer_version": 1}
    b = {**a, "assistant_model": "m2", "schemas": {"s": "y"}}
    diff = compare.fingerprint_diff(a, b)
    assert [d["key"] for d in diff] == ["assistant_model", "schemas"]
    assert diff[0] == {"key": "assistant_model", "label": "助手模型", "before": "m1", "after": "m2"}
    assert "before" not in diff[1], "表结构只说变没变"


def test_the_pair_table():
    a = [_row("c1", "pass"), _row("c2", "pass"), _row("c3", "fail"), _row("c4", "ungradable")]
    b = [_row("c1", "pass"), _row("c2", "fail"), _row("c3", "fail"), _row("c4", "pass")]
    table = compare.pair_table([("当前目录", a), ("关目录", b)])
    [pair] = table["pairs"]
    assert (pair["both"], pair["neither"], pair["only_a"], pair["only_b"], pair["excluded"]) == (1, 1, 1, 0, 1)
    assert pair["p_value"] == 1.0 and pair["note"] == compare.NO_DIFFERENCE
    assert table["arms"][0]["accuracy"]["rate"] == round(2 / 3, 4)


async def test_picking_the_baseline():
    from app.db.base import SessionLocal, utcnow
    from app.db.models_eval import EvalRun, EvalSuite

    now = utcnow()
    async with SessionLocal() as session:
        suite = EvalSuite(name="虚构题集-基准")
        session.add(suite)
        await session.flush()

        def run(minutes: int, key: str, status: str = "succeeded", **extra) -> EvalRun:
            row = EvalRun(suite_id=suite.id, status=status, fingerprint={"variant_key": key},
                          created_at=now - timedelta(minutes=minutes), **extra)
            session.add(row)
            return row

        old = run(50, "k1")
        pinned = run(40, "k1", pinned=True)
        latest = run(30, "k1")
        run(20, "k2")
        run(15, "k1", status="failed")
        target = run(10, "k1")
        later = run(5, "k1")
        await session.flush()
        assert (await compare.pick_baseline(session, target)).id == pinned.id, "同一变体里钉住的优先"
        pinned.pinned = False
        await session.flush()
        assert (await compare.pick_baseline(session, target)).id == latest.id, "比这次先发起、已结束的最近一次"
        assert (await compare.pick_baseline(session, target, explicit=old.id)).id == old.id
        assert (await compare.pick_baseline(session, target, explicit=later.id)).id == later.id
        await session.delete(suite)
        await session.commit()


@pytest.mark.parametrize("n, k", [(1, 0), (3, 3)])
def test_rates_carry_their_sample_size(n, k):
    r = stats.rate(k, n)
    assert r["n"] == n and r["passed"] == k and r["ci"] == stats.wilson(k, n)
