"""分析卡里随数据变化的调用错误（复核 R6）：模板定死了调用，这期的数据里没有要用的那一项（上一层没做成、下钻的段这期没有……）
记成算不了的一项，不让卡失败；结构性的调用错误照旧让卡失败并说清哪一项。接真算子跑（tests/test_analysis_e2e 的虚构零售库
shop），每份结论按封存的工件复算、逐字段相同。"""
from __future__ import annotations

from app.core import artifact_store
from app.engine.runner import run_manager
from tests.test_analysis_e2e import (  # noqa: F401 - engine_up、shop 是自动用的夹具
    BY_REGION, EAST, TOOL, TWO_WEEKS, WEEK, chain, engine_up, events, node, recomputes, shop, wait,
)

ONLY_EAST = (f"SELECT region, {WEEK} AS week, SUM(amount) AS gmv FROM orders "
             "WHERE region = '华东' AND day >= '2026-05-11' GROUP BY region, week ORDER BY region, week")
WHERE = {"id": "where", "op": "decompose", "mode": "additive", "from": "by_region",
         "params": {"by": ["region"], **TWO_WEEKS}}
DRILL = {"id": "east", "op": "decompose", "mode": "additive", "from": "east_q",
         "params": {"by": ["channel"], **TWO_WEEKS, "parent": {"of": "where", "key": ["华东"]}}}


def card_graph(region_sql, items):
    return chain(node("start", "input"), node("by_region", "tool", tool=TOOL, args={"sql": region_sql}),
                 node("east_q", "tool", tool=TOOL, args={"sql": EAST}),
                 node("card", "analysis", items=items),
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.results.east.phrase }}"}]))


async def _card(region_sql, items):
    run = await run_manager.start(graph=card_graph(region_sql, items), input_payload={})
    row = await wait(run.id)
    return row


async def test_a_drill_under_a_refused_parent_is_refused_and_the_card_goes_on():
    row = await _card(ONLY_EAST, [WHERE, DRILL])
    assert row.status == "succeeded", row.error
    [finished] = await events(row.id, "node.finished", "card")
    results = artifact_store.load(finished["artifact"])["results"]
    assert results["where"]["status"] == "refused" and results["east"]["status"] == "refused"
    east = artifact_store.load(results["east"]["artifact"])
    assert east["reason_code"] == "parent_refused"
    assert east["reason"] == "上一层「where」没有做成（按 region 只有 1 段，至少要 2 段才拆得开），不能在它下面下钻"
    assert row.output["r"] == "变化拆解没有做成：" + east["reason"]
    ends = {e["item"]: e for e in await events(row.id, "analysis.end", "card")}
    assert ends["east"]["reason_code"] == "parent_refused" and ends["east"]["status"] == "refused"
    warned = [e for e in await events(row.id, "log", "card") if e.get("code") == "analysis_refused"]
    assert [e["item"] for e in warned] == ["where", "east"]
    assert [(e["item"], e["status"]) for e in finished["evidence"]] == [("where", "refused"), ("east", "refused")]
    recomputes(artifact_store.load(results["where"]["artifact"]))
    recomputes(east)


async def test_a_segment_missing_this_time_is_refused_and_the_card_goes_on():
    drill = {**DRILL, "params": {**DRILL["params"], "parent": {"of": "where", "key": ["东北"]}}}
    row = await _card(BY_REGION, [WHERE, drill])
    assert row.status == "succeeded", row.error
    [finished] = await events(row.id, "node.finished", "card")
    results = artifact_store.load(finished["artifact"])["results"]
    assert results["where"]["status"] == "ok" and results["east"]["status"] == "refused"
    east = artifact_store.load(results["east"]["artifact"])
    assert east["reason_code"] == "input_unavailable" and east["reason"] == "上一层「where」里没有「东北」这一段"
    assert [i["role"] for i in east["inputs"]] == ["parent", "data"]
    recomputes(east)


async def test_a_structural_call_error_still_fails_the_card_and_names_the_item():
    wrong = {**WHERE, "params": {**WHERE["params"], "by": ["channel"]}}
    row = await _card(BY_REGION, [wrong])
    assert row.status == "failed"
    assert "分析项「where」（变化拆解）" in row.error and "没有「channel」这一列" in row.error
    assert "请在分析卡中改正这一项" in row.error
