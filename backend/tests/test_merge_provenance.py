"""合并查询的结果接进证据下钻（期 4「推断的来源」）：宁可不下钻，也不下钻错。

上传表格（合成客流表，按配方导入，夹具同 test_provenance_api）查一次、手工源的气温快照现造一份，两者在库外按日期
合并。合并结果里直接来自客流表的格，下钻结论要和直接引用客流表那一格完全相同（同一个原表格子）；合并 SQL 算出来的
列判为没有逐格来历；来自手工源的格照手工源的结论（not_upload）。每份结论都断言 contract_problems 为空。
"""
from __future__ import annotations

import pytest

from app.api import evidence as api
from app.core import artifact_store
from app.data.provenance_types import DOC_PROVENANCE, MergeHop
from app.engine.merge_query import MERGE_SOURCE, MergeInput, execute
from tests.fixtures.xlsx.flow import SHEET
from tests.test_provenance_api import (  # noqa: F401  夹具按名字注入（rep 依赖 upload_world、client）
    cell_seg,
    cell_value,
    client,
    ok,
    put,
    rep,
    report_of,
    run_sql,
    upload_world,
)

FLOW_SQL = 'SELECT "日期", "全日客流" FROM "日客流" ORDER BY "日期" DESC LIMIT 6'
MERGE_SQL = ('SELECT f."日期", f."全日客流", w."气温", f."全日客流" * 2 AS 加倍 FROM f JOIN w ON f."日期" = w."日期" '
             'ORDER BY f."日期"')


async def merged_world(up, c):
    """(文档, 封存范围, 合并结果, 客流快照)。客流按日期倒序查，合并结果按日期正序排：合并结果第 0 行是客流第 5 行。"""
    qid, sid = await run_sql(c, up, FLOW_SQL)
    flow = artifact_store.load(qid)
    dates = sorted(r[0] for r in flow["rows"])
    weather = {"columns": ["日期", "气温"], "rows": [[d, 20 + i] for i, d in enumerate(dates)], "row_count": len(dates),
               "truncated": False, "sql": "SELECT 日期, 气温 FROM 每日气温", "source": "weather_demo",
               "column_types": {"日期": "text", "气温": "number"}}
    wid = await put(weather)
    outcome = execute([MergeInput(alias="f", node_id="q_flow", artifact=qid, snapshot=flow),
                       MergeInput(alias="w", node_id="q_weather", artifact=wid, snapshot=weather)], MERGE_SQL)
    mid = await put(outcome.snapshot())
    doc = {"schema": "agentlab.report/1", "entity_syntax": 2, "run_id": "run-merge", "node_id": "write",
           "provenance": DOC_PROVENANCE, "blocks": [], "catalog": {
               "Q1": {"kind": "query", "artifact": qid, "tool": f"db_query__{up.name}", "node_id": "q_flow",
                      "source": up.name},
               "Q2": {"kind": "query", "artifact": wid, "tool": "db_query__weather_demo", "node_id": "q_weather",
                      "source": "weather_demo"},
               "Q3": {"kind": "query", "artifact": mid, "node_id": "merge", "source": MERGE_SOURCE}}}
    seal = {"sealed": True, "ok": True, "manifest_seq": 9, "legacy": False}
    sealed = api._Sealed(run_id="run-merge", graph={}, seal=seal, events=[], queries={
        qid: {"node_id": "q_flow", "tool": f"db_query__{up.name}"},
        wid: {"node_id": "q_weather", "tool": "db_query__weather_demo"},
        mid: {"node_id": "merge", "source": MERGE_SOURCE}}, schemas={sid: {"node_id": "q_flow"}})
    return doc, sealed, outcome, flow


async def drill(doc, sealed, *, alias, row, column):
    return ok(await api.segment_provenance(report_of(doc), cell_seg(alias=alias, row=row, column=column), sealed,
                                           masks=api._Masks()))


async def test_a_merged_upload_cell_points_at_the_same_original_cell(rep, client):
    doc, sealed, outcome, flow = await merged_world(rep, client)
    assert len(outcome.rows) == 6
    for r in range(len(outcome.rows)):
        via_merge = await drill(doc, sealed, alias="Q3", row=r, column="全日客流")
        direct = await drill(doc, sealed, alias="Q1", row=5 - r, column="全日客流")
        assert via_merge.status == direct.status == "inferred"
        # 同一个原表格子、同一份数据版本：结论逐字相同，只是 cell 仍是报告引用的那一格，并记下经过的合并
        assert via_merge.cell_source == direct.cell_source and via_merge.version == direct.version
        assert (via_merge.cell.alias, via_merge.cell.row, via_merge.cell.column) == ("Q3", r, "全日客流")
        assert via_merge.merge == [MergeHop(alias="Q3", node_id="merge", input="f", query="Q1", row=5 - r,
                                            column="全日客流")]
        # 原表那一格的值就是合并结果里这一格的值
        assert cell_value(rep.raws[0], SHEET, via_merge.cell_source.cell) == outcome.rows[r][1]


async def test_a_computed_merge_column_is_not_drilled(rep, client):
    doc, sealed, _outcome, _flow = await merged_world(rep, client)
    out = await drill(doc, sealed, alias="Q3", row=0, column="加倍")
    assert (out.status, out.reason.code, out.version, out.cell_source) == ("none", "merge_no_lineage", None, None)
    assert out.merge == [MergeHop(alias="Q3", node_id="merge")]
    assert "表级来历" in out.reason.text


async def test_a_manual_source_column_keeps_its_own_answer(rep, client):
    doc, sealed, _outcome, _flow = await merged_world(rep, client)
    out = await drill(doc, sealed, alias="Q3", row=0, column="气温")
    assert (out.status, out.reason.code) == ("none", "not_upload")
    assert out.merge == [MergeHop(alias="Q3", node_id="merge", input="w", query="Q2", row=0, column="气温")]


async def test_an_unsealed_merge_snapshot_is_not_drilled(rep, client):
    doc, sealed, _outcome, _flow = await merged_world(rep, client)
    sealed.queries.pop(doc["catalog"]["Q3"]["artifact"])
    out = await drill(doc, sealed, alias="Q3", row=0, column="全日客流")
    assert (out.status, out.reason.code) == ("none", "not_sealed")


@pytest.mark.parametrize("column, hinted", [("全日客流", True), ("气温", False), ("加倍", False)])
async def test_the_query_step_hint_follows_the_trace(rep, client, column, hinted):
    """界面只在追得到上传表格的格上请求推断来源；合并步骤后面接上追到的输入步骤。"""
    doc, sealed, _outcome, _flow = await merged_world(rep, client)
    steps = await api._query_steps(doc["catalog"]["Q3"]["artifact"], [(0, column)], doc, sealed, api._Masks(),
                                   hint=True)
    assert steps[0]["merge"]["inputs"][0]["query"] == "Q1"
    assert ("provenance" in steps[0]) is hinted
    assert len(steps) == (1 if column == "加倍" else 2)
    if len(steps) == 2:
        assert steps[1]["merged_into"] == "Q3" and "provenance" not in steps[1]
