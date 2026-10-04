"""合并查询把输入查询的 SQL 检查结果传到指标上。

口径卡判「来源查询有没有 error 级问题」读的是来源快照上的 checks（data/sqlcheck.py 在数据源查询时写）。
合并查询在内存 SQLite 上执行，不对照数据目录检查，它的快照没有 checks——于是源库那条查询明明一对多关联后
重复计算了，经过一次合并，指标就看不出来了，照样完整出具。合并快照记着 inputs（每个输入的快照 id），
顺着它往下找才对得上。
"""
from __future__ import annotations

import app.engine.nodes.metrics as metrics_mod
from app.engine.merge_query import MERGE_SOURCE
from app.engine.nodes.metrics import _error_checks

FANOUT = {"code": "fanout_sum", "level": "error", "message": "「订单」关联「订单明细」是一对多，求和会重复计算"}
NOTICE = {"code": "missing_valid_filter", "level": "warning", "message": "没有过滤作废记录"}


def _query(rows, checks=None):
    snap = {"columns": ["日期", "门店", "销售额"], "rows": rows, "row_count": len(rows), "truncated": False}
    return {**snap, "checks": checks} if checks else snap


def _merged(inputs):
    return {"columns": ["日期", "门店", "销售额"], "rows": [["2026-05-01", "S01", 10]], "row_count": 1,
            "truncated": False, "sql": "SELECT * FROM s JOIN v USING (日期, 门店)", "source": MERGE_SOURCE,
            "inputs": [{"alias": a, "node_id": f"q_{a}", "artifact": art, "rows": 1} for a, art in inputs]}


def _store(monkeypatch, snapshots: dict):
    monkeypatch.setattr(metrics_mod.artifact_store, "load", lambda artifact: snapshots.get(artifact))


def test_an_error_in_a_merge_input_reaches_the_metric(monkeypatch):
    _store(monkeypatch, {
        "sales": _query([["2026-05-01", "S01", 10]], [FANOUT, NOTICE]),
        "visits": _query([["2026-05-01", "S01", 3]]),
        "merged": _merged([("s", "sales"), ("v", "visits")]),
    })
    assert [c["code"] for c in _error_checks("merged")] == ["fanout_sum"]


def test_clean_inputs_leave_the_merge_clean(monkeypatch):
    _store(monkeypatch, {"sales": _query([["2026-05-01", "S01", 10]], [NOTICE]),
                         "visits": _query([["2026-05-01", "S01", 3]]),
                         "merged": _merged([("s", "sales"), ("v", "visits")])})
    assert _error_checks("merged") == []


def test_merges_of_merges_are_followed_and_cycles_do_not_hang(monkeypatch):
    _store(monkeypatch, {
        "sales": _query([["2026-05-01", "S01", 10]], [FANOUT]),
        "inner": _merged([("s", "sales")]),
        "outer": _merged([("m", "inner"), ("self", "outer")]),     # 自引用：工件内容寻址做不出来，读坏的库也不能卡死
    })
    assert [c["code"] for c in _error_checks("outer")] == ["fanout_sum"]


def test_a_plain_query_snapshot_is_read_as_before(monkeypatch):
    _store(monkeypatch, {"q": _query([["2026-05-01", "S01", 10]], [FANOUT, NOTICE])})
    assert [c["code"] for c in _error_checks("q")] == ["fanout_sum"]
