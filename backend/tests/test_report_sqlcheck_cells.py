"""报告直接引用没通过 SQL 检查的查询时，出具同样降档；同一条查询的问题只记一次。

以前缺口只从口径卡的指标来：报告绕过口径卡、直接写 [[v:Q1.r0.gmv]] 引用一条一对多关联后求和的查询，
数字回指得上快照，出具照样盖「完整出具」的章；写作目录也只给指标标「存疑」，写作者不知道这条查询本身有问题。
另一头，同一条查询算出三个指标时，运行时间线和降档原因里同一句话各重复三遍。
"""
from __future__ import annotations

import pytest

import app.engine.issuance as issuance_mod
from app.core import artifact_store
from app.engine.issuance import sql_check_gaps
from app.engine.merge_query import MERGE_SOURCE as ENGINE_MERGE_SOURCE
from app.engine.runner import run_manager
from app.engine.sql_problems import MERGE_SOURCE, query_problems, reason_text
from tests.test_runtime_sqlcheck import (  # noqa: F401 - 夹具按名字引入
    CLEAN_SQL,
    FANOUT_SQL,
    GMV,
    GMV2,
    REASON,
    Writer,
    _drop_created_sources,
    chain,
    engine_up,
    events,
    node,
    scenic_source,
    wait,
)

PROBLEM = "「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算"


def test_the_merge_source_literal_matches_the_engine():
    assert MERGE_SOURCE == ENGINE_MERGE_SOURCE
    assert reason_text([PROBLEM]) == REASON


def direct(source: str, sql: str, text: str) -> dict:
    """报告不经口径卡，直接引用查询结果里的格。"""
    return chain([
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": sql}),
        node("write", "report", instructions="写订单周报"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write"}),
    ])


async def test_a_cell_cited_from_a_failing_query_degrades_the_issuance(scenic_source, engine_up, monkeypatch):
    writer = Writer(monkeypatch, text="本周订单金额 [[v:Q1.r0.gmv]]。")
    run = await run_manager.start(graph=direct(scenic_source, FANOUT_SQL, ""), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded", issuance
    [gap] = [g for g in issuance["gaps"] if "SQL 检查" in g]
    assert gap.startswith("查询「fetch」（Q1）未通过 SQL 检查") and PROBLEM in gap
    assert "受影响的引用：Q1 第 1 行「gmv」" in gap
    # 写作目录里，这条查询本身就标着「存疑」：写作者引用它的格时知道要交代
    prompt = writer.prompts[0]
    q1 = prompt[prompt.index("- Q1："):]
    assert "这次查询未通过 SQL 检查" in q1.split("\n例：")[0] and PROBLEM in q1


async def test_the_same_report_on_a_clean_query_issues_formally(scenic_source, engine_up, monkeypatch):
    writer = Writer(monkeypatch, text="本周订单金额 [[v:Q1.r0.gmv]]。")
    run = await run_manager.start(graph=direct(scenic_source, CLEAN_SQL, ""), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert row.output["_issuance"]["tier"] == "formal", row.output["_issuance"]
    assert "未通过 SQL 检查" not in writer.prompts[0]


def three(source: str, sql: str) -> dict:
    """同一条查询算出两个指标，报告又直接引用了它的一格、拿它当依据。"""
    return chain([
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": sql}),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="res"),
        node("card", "metrics", caliber="订单口径", caliber_version="v1", metrics=[GMV, GMV2]),
        node("write", "report", instructions="写订单周报"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"]}),
    ])


async def test_one_failing_query_is_one_gap_and_one_timeline_row(scenic_source, engine_up, monkeypatch):
    Writer(monkeypatch, text="本周订单金额 [[m:gmv]]，查询结果 [[v:Q1.r0.gmv]]。[[see:Q1]]")
    run = await run_manager.start(graph=three(scenic_source, FANOUT_SQL), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    [gap] = [g for g in issuance["gaps"] if "SQL 检查" in g]
    assert gap.count(PROBLEM) == 1
    assert "指标「订单金额」" in gap and "指标「订单金额（整形后）」" in gap and "Q1 第 1 行「gmv」" in gap

    # 运行时间线：同一条查询的问题一行，列出受影响的指标；带着结构化字段，界面不用解析中文
    [log] = [e.data for e in await events(row.id, "log", "card") if e.data.get("code") == "metric_sql_check"]
    assert log["metrics"] == ["gmv", "gmv2"] and log["metric_names"] == ["订单金额", "订单金额（整形后）"]
    assert log["message"] == f"指标「订单金额」「订单金额（整形后）」{REASON}"
    assert log["reason"] == REASON and log["problems"] == [PROBLEM]
    assert log["source_node"] == "fetch" and log["source_field"] == "args.sql"
    [check] = log["checks"]
    assert check["code"] == "fanout_sum" and check["level"] == "error" and "for_model" not in check
    [query_end] = [e.data for e in await events(row.id, "tool.end", "fetch")]
    assert log["query_artifact"] == query_end["query_artifact"]


# --------------------------------------------------------------------------
# 合并查询：问题记在出问题的那条源查询上，经几条路径找到都只算一次
# --------------------------------------------------------------------------

FANOUT = {"code": "fanout_sum", "level": "error", "message": f"{PROBLEM}。改法：先汇总再关联"}


def _snap(checks=None, **extra):
    snap = {"columns": ["日期", "门店", "销售额"], "rows": [["2026-05-01", "S01", 10]], "truncated": False, **extra}
    return {**snap, "checks": checks} if checks else snap


@pytest.fixture
def snapshots(monkeypatch):
    store = {
        "sales": _snap([FANOUT]),
        "visits": _snap(),
        "merged": _snap(source=MERGE_SOURCE, inputs=[{"alias": "s", "node_id": "q_sales", "artifact": "sales"},
                                                    {"alias": "v", "node_id": "q_visits", "artifact": "visits"}]),
    }
    monkeypatch.setattr(artifact_store, "load", lambda artifact: store.get(artifact))
    return store


def test_problems_are_recorded_on_the_input_that_failed(snapshots):
    [problem] = query_problems("merged")
    assert problem.artifact == "sales" and problem.node_id == "q_sales" and problem.problems == [PROBLEM]


def test_metrics_and_cells_through_a_merge_share_one_gap(snapshots):
    metrics = [{"id": "gmv", "name": "销售额", "sql_check_failed": True, "sql_check_reason": REASON,
                "sql_check_sources": [{"artifact": "merged", "codes": ["fanout_sum"]}]},
               {"id": "avg", "name": "店均销售额", "sql_check_failed": True, "sql_check_reason": REASON,
                "sql_check_sources": [{"artifact": "merged", "codes": ["fanout_sum"]}]}]
    labels = {"sales": "查询「取销售」（Q1）"}
    gaps = sql_check_gaps(metrics, {"merged": ["Q2 第 1 行「销售额」"], "sales": ["Q1（结论依据）"]},
                          label=lambda artifact, node: labels.get(artifact))
    assert gaps == [f"查询「取销售」（Q1）未通过 SQL 检查（{PROBLEM}），结果不可靠；"
                    "受影响的引用：指标「销售额」、指标「店均销售额」、Q2 第 1 行「销售额」、Q1（结论依据）"]


def test_a_flagged_metric_whose_snapshot_is_gone_still_counts(monkeypatch):
    """快照读不出来（被清理、被改过）时退回口径卡记下的那句：缺口不能因为读不到快照就消失。"""
    monkeypatch.setattr(issuance_mod, "query_problems", lambda artifact, **_: [])
    metrics = [{"id": "gmv", "name": "订单金额", "sql_check_failed": True, "sql_check_reason": REASON,
                "sql_check_sources": [{"artifact": "gone", "codes": ["fanout_sum"]}]}]
    assert sql_check_gaps(metrics) == [f"指标「订单金额」{REASON}"]
    assert sql_check_gaps([{"id": "n", "name": "订单数", "value": 2}]) == []
