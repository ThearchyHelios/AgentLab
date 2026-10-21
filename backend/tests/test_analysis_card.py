"""分析卡（节点类型 analysis，docs/design/analysis-round/00-总方案.md §3.2）：画图时的校验、执行、产出的变量、受管规则、
登记处（叫法、变量表、助手查看、目录影响面、升级提醒）。运行对比按项对齐见 tests/test_analysis_compare.py。

算子（app.analysis.run）换成按请求交回假结论的替身（tests/fixtures/analysis_ops.FakeOps）。库是虚构的零售库 shop。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import select

from app.analysis import contract
from app.analysis.contract import CallError
from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, NodeType, validate_graph
from tests.fixtures.analysis_ops import FakeOps

TOOL = "db_query__shop"
WEEKLY = "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week ORDER BY week"
REGION = "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week ORDER BY region, week"
ROWS = [(1, "华东", "2026-W41", 820.0), (2, "华东", "2026-W42", 700.0), (3, "华南", "2026-W41", 700.0),
        (4, "华南", "2026-W42", 666.0), (5, "华北", "2026-W41", 600.0), (6, "华北", "2026-W42", 560.0)]
WOW = {"id": "wow", "op": "baseline", "from": "weekly", "params": {"period": "week", "measure": "gmv",
                                                                     "label": "销售额"}}
WHERE = {"id": "where", "op": "decompose", "mode": "additive", "from": "by_region",
         "params": {"by": ["region"], "period": "week", "base": "2026-W41", "target": "2026-W42", "measure": "gmv",
                    "headline": {"base": "weekly.r0.gmv", "target": "weekly.r1.gmv"}, "label": "销售额"},
         "hypothesis": "下降是不是集中在某个地区"}
DRILL = {"id": "east", "op": "decompose", "mode": "additive", "from": "by_region",
         "params": {"by": ["week"], "period": "week", "base": "2026-W41", "target": "2026-W42", "measure": "gmv",
                    "parent": {"of": "where", "key": ["华东"]}}}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def _shop(tmp_path, options=None):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, week TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, options or {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    return source_id


@pytest.fixture
async def shop(tmp_path):
    source_id = await _shop(tmp_path)
    yield
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def edge(a, b, handle=None):
    return {"source": a, "target": b, **({"sourceHandle": handle} if handle else {})}


def card_graph(items=(WOW, WHERE), *, extra_nodes=(), extra_edges=(), **card):
    nodes = [node("start", "input"),
             node("weekly", "tool", tool=TOOL, args={"sql": WEEKLY}),
             node("by_region", "tool", tool=TOOL, args={"sql": REGION}),
             node("card", "analysis", items=[dict(i) if isinstance(i, dict) else i for i in items], **card),
             node("out", "output", fields=[{"name": "判定", "value": "{{ nodes.card.results.wow.phrase }}"}]),
             *extra_nodes]
    edges = [edge("start", "weekly"), edge("weekly", "by_region"), edge("by_region", "card"), edge("card", "out"),
             *extra_edges]
    return {"nodes": nodes, "edges": edges}


def card_issues(graph, level="error"):
    return [i for i in validate_graph(GraphSpec.model_validate(graph)).issues
            if i.node_id == "card" and (level is None or i.level == level)]


async def wait(run_id, statuses=("succeeded", "failed")):
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没有结束：{row.status}")


async def events(run_id, etype=None, node_id=None):
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return [e.data for e in (await session.execute(q.order_by(RunEvent.seq))).scalars()]


async def run_graph(graph, **kw):
    run = await run_manager.start(graph=graph, input_payload={}, **kw)
    return await wait(run.id)


async def output_of(run_id, node_id):
    [finished] = await events(run_id, "node.finished", node_id)
    return finished, artifact_store.load(finished["artifact"])


# --------------------------------------------------------------------------
# 画图时的校验
# --------------------------------------------------------------------------


def test_node_type_and_labels():
    from app.engine.labels import field_label, type_label

    assert NodeType.ANALYSIS == contract.NODE_TYPE == "analysis"
    assert type_label("analysis") == "分析卡"
    assert field_label("items", "analysis") == "分析项" and field_label("overrides", "analysis") == "阈值覆盖"


def test_a_well_formed_card_validates():
    assert card_issues(card_graph(items=(WOW, WHERE, DRILL), overrides={"main_share": 0.6}), level=None) == []


@pytest.mark.parametrize("items, field, code, why", [
    ([], "items", "analysis.items_missing", "至少写一项"),
    (["wow"], "items[0]", "analysis.item_invalid", "需要是一个对象"),
    ([{**WOW, "id": "1st"}], "items[0].id", "analysis.item_invalid", "字母、数字和下划线"),
    ([WOW, {**WHERE, "id": "wow"}], "items[1].id", "analysis.item_invalid", "唯一"),
    ([{**WOW, "op": "forecast"}], "items[0].op", "analysis.op_invalid", "不认识"),
    ([{**WHERE, "mode": None}], "items[0].mode", "analysis.mode_invalid", "要选一种模式"),
    ([{**WOW, "mode": "additive"}], "items[0].mode", "analysis.mode_invalid", "没有模式可选"),
    ([{**WOW, "from": None}], "items[0].from", "analysis.input_invalid", "还没有选择输入"),
    ([{**WOW, "from": "nope"}], "items[0].from", "analysis.input_invalid", "不存在"),
    ([{**WOW, "params": {**WOW["params"], "k_warn": 3}}], "items[0].params.k_warn", "analysis.params_invalid",
     "阈值覆盖"),
    ([{**WOW, "params": {**WOW["params"], "query": "weekly"}}], "items[0].params.query", "analysis.params_invalid",
     "输入里"),
    ([{**WOW, "params": {**WOW["params"], "bogus": 1}}], "items[0].params.bogus", "analysis.params_invalid",
     "没有「bogus」"),
    # 运行当天、目录事实、下钻层数由系统填（算子自己记），分析项里写了不收
    ([{**WOW, "params": {**WOW["params"], "today": "2026-10-21"}}], "items[0].params.today",
     "analysis.params_invalid", "没有「today」"),
    ([{**WHERE, "params": {**WHERE["params"], "headline": {"base": "weekly.gmv", "target": "weekly.r1.gmv"}}}],
     "items[0].params.headline.base", "analysis.params_invalid", "节点 id.r行号.列名"),
    ([{**WHERE, "params": {**WHERE["params"], "headline": "weekly.r0.gmv"}}], "items[0].params.headline",
     "analysis.params_invalid", "两格"),
    ([DRILL, WHERE], "items[0].params.parent", "analysis.parent_invalid", "排在它前面"),
    ([WOW, {**DRILL, "params": {**DRILL["params"], "parent": {"of": "wow", "key": ["华东"]}}}],
     "items[1].params.parent", "analysis.parent_invalid", "不是变化拆解"),
    ([WHERE, {**DRILL, "params": {**DRILL["params"], "parent": {"of": "where"}}}], "items[1].params.parent",
     "analysis.parent_invalid", "写清是哪一项里的哪一段"),
    ([WHERE, {**WOW, "params": {**WOW["params"], "parent": {"of": "where", "key": ["华东"]}}}],
     "items[1].params.parent", "analysis.params_invalid", "没有「parent」"),
])
def test_item_problems(items, field, code, why):
    found = [i for i in card_issues(card_graph(items=items)) if i.field == field]
    assert found and found[0].code == code and why in found[0].message, [i.message for i in card_issues(
        card_graph(items=items))]


@pytest.mark.parametrize("extra, why", [
    (node("think", "llm", prompt="写点什么"), "不产出查询结果"),
    (node("asker", "agent", prompt="查一下", tools=[TOOL]), "Agent 可能查询多次"),
])
def test_inputs_must_be_query_nodes(extra, why):
    graph = card_graph(items=[{**WOW, "from": extra["id"]}], extra_nodes=[extra],
                       extra_edges=[edge("by_region", extra["id"]), edge(extra["id"], "card")])
    [issue] = card_issues(graph)
    assert issue.field == "items[0].from" and why in issue.message and issue.code == "analysis.input_invalid"


def test_inputs_must_be_upstream_and_other_tools_only_warn():
    lonely = node("lonely", "tool", tool=TOOL, args={"sql": WEEKLY})
    [issue] = card_issues(card_graph(items=[{**WOW, "from": "lonely"}], extra_nodes=[lonely],
                                     extra_edges=[edge("start", "lonely")]))
    assert "不在分析卡的上游" in issue.message
    # headline 那两格落在的节点同样要是上游的查询节点
    headline = {**WHERE["params"], "headline": {"base": "lonely.r0.gmv", "target": "weekly.r1.gmv"}}
    [issue] = card_issues(card_graph(items=[{**WHERE, "params": headline}], extra_nodes=[lonely],
                                     extra_edges=[edge("start", "lonely")]))
    assert issue.field == "items[0].params.headline.base" and "不在分析卡的上游" in issue.message
    # 调用别的工具的调用工具节点：登记里声明了表格的端点读取也交回快照，静态看不出，只提醒
    other = node("ext", "tool", tool="crm_report", args={})
    graph = card_graph(items=[{**WOW, "from": "ext"}], extra_nodes=[other],
                       extra_edges=[edge("by_region", "ext"), edge("ext", "card")])
    assert card_issues(graph) == []
    [warning] = card_issues(graph, level="warning")
    assert warning.code == "analysis.input_invalid" and "不是数据库查询工具" in warning.message


@pytest.mark.parametrize("overrides, why", [
    ({"k_warn": 3.0, "k_alert": 2.5}, "要大于"),
    ({"main_share": 2}, "之间"),
    ({"bogus": 1}, "不认识的分析设置"),
    ("tight", "需要是一个对象"),
])
def test_overrides_are_checked_by_the_contract(overrides, why):
    [issue] = card_issues(card_graph(overrides=overrides))
    assert issue.field == "overrides" and issue.code == "analysis.overrides_invalid" and why in issue.message


def test_two_snapshot_items_need_both_nodes():
    two = {"id": "same", "op": "comparable", "params": {"base_query": "weekly", "target_query": "by_region"}}
    assert card_issues(card_graph(items=[two])) == []
    half = {"id": "same", "op": "comparable", "params": {"base_query": "weekly"}}
    [issue] = card_issues(card_graph(items=[half]))
    assert issue.field == "items[0].from" and "两个查询节点" in issue.message


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


async def test_the_card_runs_each_item_and_records_it(monkeypatch, shop):
    ops = FakeOps(monkeypatch, verdicts=["outside_band", "driver", "driver"], peek=["wow", "where", "east"])
    row = await run_graph(card_graph(items=(WOW, WHERE, DRILL), overrides={"main_share": 0.6}),
                          options={contract.SETTINGS_KEY: {"k_warn": 2.5}})
    assert row.status == "succeeded", row.error

    # 算子拿到的：请求、按上游节点 id 解析的快照（只认台账里的那几份）、阈值（缺省 ← 发起时的快照 ← 卡上的覆盖）
    wow, where, east = ops.calls
    assert wow["request"] == {"op": "baseline", "mode": None,
                              "params": {"period": "week", "measure": "gmv", "label": "销售额", "query": "weekly"}}
    assert where["request"]["hypothesis"] == "下降是不是集中在某个地区" and where["request"]["mode"] == "additive"
    assert where["request"]["params"]["headline"] == {"base": "weekly.r0.gmv", "target": "weekly.r1.gmv"}
    weekly = wow["snapshots"]["weekly"]
    [tool_end] = await events(row.id, "tool.end", "weekly")
    assert weekly["ref"] == "weekly" and weekly["node_id"] == "weekly" and weekly["artifact"] == tool_end["query_artifact"]
    assert weekly["columns"] == ["week", "gmv"] and weekly["rows"] == [["2026-W41", 2120.0], ["2026-W42", 1926.0]]
    assert weekly["sql"] == WEEKLY and weekly["source"] == "shop" and weekly["masked"] == []
    assert wow["settings"] == {**contract.DEFAULTS, "k_warn": 2.5, "main_share": 0.6}
    # 下钻取到同卡里前一项的结论；只看得到排在前面、已经做完的项
    assert east["parent"]["op"] == "decompose" and east["parent"]["params"]["query"] == "by_region"
    assert east["visible"] == {"wow": True, "where": True, "east": False}
    assert wow["visible"] == {"wow": False, "where": False, "east": False}

    finished, output = await output_of(row.id, "card")
    results = output["results"]
    assert set(results) == {"wow", "where", "east"}      # 产出工件按规范 JSON 存，键的先后看配置里项的先后
    assert results["wow"]["verdict"] == "outside_band" and results["wow"]["status"] == "ok"
    assert results["wow"]["phrase"].startswith("销售额本期变化") and results["wow"]["direction"] == "down"
    assert results["where"]["values"]["top_share"] == "0.6186"
    # 台账：每项一条 kind analysis（带 item），工件就是整份结论
    entries = finished["evidence"]
    assert [(e["kind"], e["item"]) for e in entries] == [("analysis", "wow"), ("analysis", "where"),
                                                         ("analysis", "east")]
    for entry, item in zip(entries, ("wow", "where", "east")):
        stored = artifact_store.load(entry["artifact"])
        assert entry == contract.ledger_entry(stored, node_id="card", exec_no=1, artifact=entry["artifact"],
                                              item=item)
        assert results[item]["artifact"] == entry["artifact"]
    # 每项一条 analysis.end
    ends = await events(row.id, "analysis.end", "card")
    assert [(e["item"], e["op"], e["verdict"], e["analysis_artifact"]) for e in ends] == [
        (e["item"], {"wow": "baseline"}.get(e["item"], "decompose"), results[e["item"]]["verdict"],
         results[e["item"]]["artifact"]) for e in ends]
    # 成果字段取判定语
    assert row.output["判定"] == results["wow"]["phrase"]


async def test_a_refused_item_does_not_fail_the_card(monkeypatch, shop):
    FakeOps(monkeypatch, statuses=["refused", "ok"])
    row = await run_graph(card_graph())
    assert row.status == "succeeded", row.error
    _, output = await output_of(row.id, "card")
    assert output["results"]["wow"]["status"] == "refused" and output["results"]["wow"]["verdict"] is None
    assert output["results"]["wow"]["phrase"].endswith("历史只有 5 期，至少要 8 期")
    assert output["results"]["where"]["status"] == "ok"
    [warn] = [e for e in await events(row.id, "log", "card") if e.get("code") == "analysis_refused"]
    assert warn["item"] == "wow" and warn["reason_code"] == "history_short" and "wow" in warn["message"]


async def test_a_call_error_fails_the_card_and_names_the_item(monkeypatch, shop):
    FakeOps(monkeypatch, errors=[None, CallError("ratio_as_additive", "比率不能按段相加，改用 ratio 模式，给分子、分母两列")])
    row = await run_graph(card_graph())
    assert row.status == "failed"
    assert "分析项「where」（变化拆解）" in row.error and "比率不能按段相加" in row.error and "改正这一项" in row.error


async def test_a_branch_can_follow_the_verdict(monkeypatch, shop):
    FakeOps(monkeypatch, verdicts=["outside_band", "driver"])
    graph = card_graph(extra_nodes=[
        node("route", "branch", cases=[{"key": "alert", "condition": "nodes.card.results.wow.verdict == 'outside_band'"}]),
        node("alert", "output", fields=[{"name": "转人工", "value": "{{ nodes.card.results.wow.phrase }}"}])])
    graph["edges"] = [e for e in graph["edges"] if e["target"] != "out"] + [
        edge("card", "route"), edge("route", "alert", "alert"), edge("route", "out", "default")]
    row = await run_graph(graph)
    assert row.status == "succeeded", row.error
    assert "转人工" in row.output


async def test_inputs_that_are_not_query_snapshots_fail_clearly(monkeypatch, shop):
    FakeOps(monkeypatch)
    other = node("calc", "tool", tool="json_query", args={"data": '{"a": 1}', "path": "a"})
    graph = card_graph(items=[{**WOW, "from": "calc"}], extra_nodes=[other],
                       extra_edges=[edge("by_region", "calc"), edge("calc", "card")])
    row = await run_graph(graph)
    assert row.status == "failed" and "不是存成快照的查询结果" in row.error


async def test_masked_columns_reach_the_operator(monkeypatch, tmp_path):
    source_id = await _shop(tmp_path, {"mask_columns": ["region"]})
    try:
        ops = FakeOps(monkeypatch)
        row = await run_graph(card_graph(items=[WHERE]))
        assert row.status == "succeeded", row.error
        assert "region" in ops.calls[0]["snapshots"]["by_region"]["masked"]
    finally:
        await engines.invalidate(source_id)


# --------------------------------------------------------------------------
# 受管规则、登记处
# --------------------------------------------------------------------------


def _governed(graph):
    from app.engine.governance import lint_for_publish

    return lint_for_publish(GraphSpec.model_validate(graph), level="governed").issues


def test_governed_cards_must_analyze_platform_queries():
    other = node("ext", "tool", tool="crm_report", args={})
    graph = card_graph(items=[{**WOW, "from": "ext"}], extra_nodes=[other],
                       extra_edges=[edge("by_region", "ext"), edge("ext", "card")])
    [issue] = [i for i in _governed(graph) if i.code == "governed.analysis_input"]
    assert issue.level == "error" and issue.node_id == "card" and issue.field == "items[0].from"
    assert [i for i in _governed(card_graph()) if i.code == "governed.analysis_input"] == []
    # 发布时：受管级按错误挡、画布那条同义的提醒不再列；已发布级只有画布那条提醒
    from app.engine.governance import publish_issues

    spec = GraphSpec.model_validate(graph)
    governed = [(i.code, i.level) for i in publish_issues(spec, level="governed") if i.node_id == "card"]
    assert ("governed.analysis_input", "error") in governed and ("analysis.input_invalid", "warning") not in governed
    published = [(i.code, i.level) for i in publish_issues(spec, level="published") if i.node_id == "card"]
    assert published == [("analysis.input_invalid", "warning")]


def test_a_caliber_card_reading_analysis_results_gets_a_hint():
    from app.engine.governance import _traceable

    graph = card_graph(extra_nodes=[node("kpi", "metrics", metrics=[
        {"id": "share", "expression": "nodes.card.results.where.values.top_share"}])],
        extra_edges=[edge("card", "kpi")])
    [issue] = [i for i in _governed(graph) if i.code == "governed.caliber_analysis_input"]
    assert issue.level == "warning" and issue.node_id == "card" and "追不到原始数据" in issue.message
    assert _traceable(GraphSpec.model_validate(graph).node_map()["card"])


def test_plain_wording_for_the_new_codes():
    from app.plain import plain_of

    for code in ("governed.analysis_input", "governed.caliber_analysis_input"):
        assert plain_of(code) is not None, code


async def test_a_caliber_card_reading_an_analysis_card_is_untraced(monkeypatch, shop):
    from app.engine.lineage import untraced

    FakeOps(monkeypatch)
    graph = card_graph(extra_nodes=[node("kpi", "metrics", metrics=[
        {"id": "share", "expression": "float(nodes.card.results.where.values.top_share)"}])],
        extra_edges=[edge("card", "kpi")])
    row = await run_graph(graph)
    assert row.status == "succeeded", row.error
    _, card = await output_of(row.id, "kpi")
    [item] = card["metrics"][0]["inputs"]
    assert item["via"] == "analysis" and item["node_id"] == "card" and untraced([item]) == [item]


def test_variables_list_each_item_and_count_the_inputs_as_references():
    from app.engine.variables import analyze

    report = analyze(GraphSpec.model_validate(card_graph(items=(WOW, WHERE))))
    found = {v.path: v for v in report.variables}
    assert {"nodes.card.results.wow", "nodes.card.results.where"} <= set(found)
    assert found["nodes.card.results.wow"].produced_by == "card"
    assert any(r.node_id == "card" and r.field == "items[0].from" for r in found["nodes.weekly"].refs)
    assert any(r.node_id == "card" and r.field == "items[1].params.headline.base" for r in found["nodes.weekly"].refs)
    assert not [i for i in report.issues if i.level != "info"]


def test_describe_impact_and_upgrade_know_the_card():
    from types import SimpleNamespace

    from app.apps.describe import describe_version
    from app.data.catalog_impact import template_refs
    from app.engine.upgrade import _EVIDENCE_TYPES

    graph = card_graph(items=(WOW, WHERE))
    text = describe_version(SimpleNamespace(graph=graph, version=2, graph_hash="ab" * 32, level="published"),
                            workflow_name="区域周报")
    assert "分析卡「card」（card），分析项 2 个" in text and "card.results.where 变化拆解" in text
    refs = template_refs(GraphSpec.model_validate(graph), "shop", "orders", None)
    [card] = [r for r in refs if r["node_id"] == "card"]
    assert card["type"] == "analysis" and card["impact"] == "direct"
    assert {(v["node_id"], v["alias"]) for v in card["via"]} == {("weekly", "wow"), ("by_region", "where"),
                                                                 ("weekly", "where")}
    assert NodeType.ANALYSIS in _EVIDENCE_TYPES
