"""和上一次比、A/B 对照（app/eval/compare.py；细案 01 做法 12，评测-D10）。纯计算，选基准读库。

- 逐题转移（退步 / 进步 / 不变 / 不可比）和归因（数据变、做法变、只参数变、路径变），列出指纹里哪几项变了；
- A/B 配对表：都对 / 都错 / 只 A 对 / 只 B 对，不能判的不进表，精确 McNemar；
- 选基准：点名的 > 同一变体里钉住的 > 同一变体、比这次先发起、已经结束的最近一次。
题目、模型名都是虚构的。
"""
from __future__ import annotations

from datetime import timedelta

import app.db.models  # noqa: F401 - conftest 按已导入的模型建表
from app.eval import compare


def _row(case: str, verdict: str | None, *, path: str = "ran", status: str = "ok", repeat: int = 0, **extra) -> dict:
    return {"case_id": case, "case_seq": int(case[-1]) if case[-1].isdigit() else 1, "case_hash": f"h-{case}",
            "verdict": verdict, "path": path, "status": status, "repeat": repeat, **extra}


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


def test_the_analysis_tools_show_as_on_and_off():
    """分析工具这一项写开、关；加这一项之前的评测指纹里没有它，那时一直开着：和现在开着的比不算变了。"""
    old = {"assistant_model": "m1", "catalog": "live:aaa", "scorer_version": 3}
    on, off = {**old, "analysis_tools": "on"}, {**old, "analysis_tools": "off"}
    assert compare.fingerprint_diff(old, on) == [] and compare.fingerprint_diff(on, on) == []
    item = {"key": "analysis_tools", "label": "分析工具"}
    assert compare.fingerprint_diff(on, off) == [{**item, "before": "开", "after": "关"}]
    assert compare.fingerprint_diff(off, old) == [{**item, "before": "关", "after": "开"}]


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
