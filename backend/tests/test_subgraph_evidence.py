"""子工作流的证据交回父图（docs/design/orchestration-round/00-总方案.md §2）。

以前子图只把出口的值交回父图：子图里的查询快照、口径卡留在子图自己的状态里，子图里每一步在运行的事件里一条都没有（只设了
节点 id 前缀没用上，LangGraph 的 ainvoke 不把子图的 custom 事件交给父图的流），父图报告引用不到子图里的数，父图口径卡读
子图出口的字段一律停在子工作流节点。现在：
- 子图里每一步进同一个运行的事件流，节点 id「子工作流节点 id/…」，封存覆盖它们；
- 子图的台账（查询快照、口径卡）交回父图：父图报告的目录收进它们，引用标记、证据面板一路点到子图里的那一格；
- 子图出口的每个字段带出处，父图口径卡读它时跨子图往下追：追到原始数据的完整出具，子图里追不到的父图照样降档；
  静态检查（发布前、应用检查）和运行时同一套认法；嵌套两层照样；
- 子图里的遮罩照父图的运行。
数据是虚构店铺库（tests/ledger_samples.py）。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import RunEvent
from app.engine.lineage import traced, untraced
from app.engine.runner import run_manager
from tests import ledger_samples as S

PAID = "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid'"
GMV = {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "expression": "cell(nodes.fetch, 0, 'gmv')"}


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid: str, ntype: str, label: str | None = None, **config) -> dict:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def metric(mid: str, expression: str, name: str = "") -> dict:
    return {"id": mid, "name": name or mid, "unit": "元", "decimals": 2, "expression": expression}


def fetch_card_graph(*, card: dict | None = None, out: list[dict] | None = None, sql: str = PAID) -> dict:
    """子工作流：取数 → 口径卡 → 出口（gmv 原样交出口径卡的指标，table 原样交出查询结果）。"""
    return S.chain(
        node("s", "input"),
        node("fetch", "tool", tool=S.tool(), args={"sql": sql}),
        card or node("card", "metrics", caliber="子口径", caliber_version="v1", metrics=[GMV]),
        node("o", "output", fields=out or [{"name": "gmv", "value": "{{ nodes.card.metrics[0].value }}"},
                                              {"name": "table", "value": "{{ nodes.fetch }}"}]))


async def publish(graph: dict, name: str = "子流程") -> str:
    return await S.publish(name, graph=graph)


def parent(sub_id: str, *middle: dict, contract_from: str | None = "top", out: list[dict] | None = None,
           sub_input: dict | None = None) -> dict:
    return S.chain(
        node("start", "input", fields=[{"name": "region"}]),
        node("sub", "subgraph", "取数子流程", workflow_id=sub_id, workflow_version=1,
               **({"input": sub_input} if sub_input else {})),
        *middle,
        node("out", "output", fields=out or [{"name": "数", "value": "{{ nodes.top.metrics[0].value }}"}],
               **({"contract": {"metrics_from": contract_from}} if contract_from else {})))


async def run(graph: dict, inputs: dict | None = None):
    started = await run_manager.start(graph=graph, input_payload=inputs or {})
    row = await S.wait(started.id)
    assert row.status == "succeeded", row.error
    return row


async def events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id)
                                           .order_by(RunEvent.seq))).scalars())


async def card(run_id: str, node_id: str) -> dict:
    from app.api.evidence import _sealed, _sealed_cards

    sealed = await _sealed(run_id)
    [found] = [c for n, _, c in _sealed_cards(sealed) if n == node_id]
    return {m["id"]: m for m in found["metrics"]}


# --------------------------------------------------------------------------
# 子图里每一步进同一个运行的事件流
# --------------------------------------------------------------------------


async def test_the_subgraph_steps_are_in_the_run_with_prefixed_ids():
    sub_id = await publish(fetch_card_graph())
    row = await run(parent(sub_id, contract_from=None, out=[{"name": "数", "value": "{{ nodes.sub.output.gmv }}"}]))
    evs = await events(row.id)
    started = [(e.node_id, e.data.get("label")) for e in evs if e.type == "node.started"]
    assert [n for n, _ in started] == ["start", "sub", "sub/s", "sub/fetch", "sub/card", "sub/o", "out"]
    assert dict(started)["sub/fetch"] == "fetch"
    [end] = [e for e in evs if e.type == "tool.end" and e.node_id == "sub/fetch"]
    assert end.data.get("query_artifact")
    # 子图里的台账条目在子图那一步的 node.finished 上（节点 id 带前缀），子工作流节点自己的 node.finished 不再记一遍
    finished = {e.node_id: e for e in evs if e.type == "node.finished"}
    assert [x["kind"] for x in finished["sub/fetch"].data["evidence"]][:1] == ["tool"]
    assert "evidence" not in finished["sub"].data
    # 封存覆盖它们
    assert row.manifest_seq == evs[-1].seq
    assert row.output == {"数": "246.5"}


async def test_a_step_inside_the_subgraph_is_named_by_its_label_not_its_id():
    """子图里的一步被驳回、出错时，运行记下的出错节点是「sub/x」：实例页、通知照终态事件里的名字写「子工作流 › x」，
    不露出内部 id。"""
    from app.apps.resolve import _trails, instance_state, run_node_label
    from app.db.models import Run

    sub = S.chain(node("s", "input"),
                  node("gate", "tool", "算一下", tool="calculator", args={"expression": "1+1"}, approval="always"),
                  node("o", "output", fields=[{"name": "x", "value": "{{ nodes.gate }}"}]))
    sub_id = await publish(sub)
    graph = S.chain(node("start", "input"), node("sub", "subgraph", "取数子流程", workflow_id=sub_id, workflow_version=1),
                    node("out", "output", fields=[{"name": "x", "value": "{{ nodes.sub.output.x }}"}]))
    started = await run_manager.start(graph=graph, input_payload={})
    for _ in range(200):
        async with SessionLocal() as session:
            if (await session.get(Run, started.id)).status == "interrupted" and not run_manager.is_active(started.id):
                break
        await __import__("asyncio").sleep(0.05)
    await run_manager.resume(started.id, {"approved": False, "note": "不对"}, actor="张工")
    for _ in range(200):
        async with SessionLocal() as session:
            row = await session.get(Run, started.id)
        if row.status == "cancelled" and not run_manager.is_active(started.id):
            break
        await __import__("asyncio").sleep(0.05)
    assert (row.status, row.end_reason, row.error_node_id) == ("cancelled", "rejected", "sub/gate")
    async with SessionLocal() as session:
        trail = (await _trails(session, [row]))[row.id]
    assert instance_state(row, trail) == {"code": "cancelled", "text": "「取数子流程 › 算一下」被驳回"}
    assert run_node_label(row, trail, "sub/gate") == "取数子流程 › 算一下"

    failing = await publish(fetch_card_graph(sql="SELECT nope FROM nowhere"), "出错的")
    started = await run_manager.start(graph=parent(failing, contract_from=None,
                                                   out=[{"name": "数", "value": "{{ nodes.sub.output.gmv }}"}]),
                                      input_payload={})
    row = await S.wait(started.id)
    assert row.status == "failed" and row.error_node_id == "sub/fetch"
    async with SessionLocal() as session:
        trail = (await _trails(session, [row]))[row.id]
    assert run_node_label(row, trail, row.error_node_id) == "取数子流程 › fetch"
    assert run_node_label(row, trail, "sub") == "取数子流程"


async def test_the_instance_timeline_counts_the_subgraph_as_one_step():
    """应用的实例进度只说这张图上的步骤：子图里的那几步（「sub/…」）不单列，子工作流算一步。"""
    from types import SimpleNamespace

    from app.apps.resolve import _timeline
    from app.db.models import Run

    sub_id = await publish(fetch_card_graph())
    row = await run(parent(sub_id, contract_from=None, out=[{"name": "数", "value": "{{ nodes.sub.output.gmv }}"}]))
    async with SessionLocal() as session:
        steps = (await _timeline(session, SimpleNamespace(instance=await session.get(Run, row.id))))["steps"]
    assert [s["node"] for s in steps] == ["start", "sub", "out"]


async def test_an_error_inside_the_subgraph_names_the_step_inside_it():
    sub_id = await publish(fetch_card_graph(sql="SELECT nope FROM nowhere"))
    started = await run_manager.start(graph=parent(sub_id, contract_from=None,
                                                   out=[{"name": "数", "value": "{{ nodes.sub.output.gmv }}"}]),
                                      input_payload={})
    row = await S.wait(started.id)
    assert row.status == "failed" and row.error_node_id == "sub/fetch"
    [failed] = [e for e in await events(row.id) if e.type == "run.failed"]
    assert (failed.data["node_id"], failed.data["label"]) == ("sub/fetch", "取数子流程 › fetch")


# --------------------------------------------------------------------------
# 父图口径卡读子图出口的字段：跨子图往下追
# --------------------------------------------------------------------------


async def test_a_parent_card_reading_a_subgraph_field_traces_to_the_query_cell_and_issues_formally(client):
    sub_id = await publish(fetch_card_graph())
    top = node("top", "metrics", caliber="父口径", caliber_version="v1",
                 metrics=[metric("gmv", "float(nodes.sub.output.gmv)"), metric("raw", "cell(nodes.sub.output.table, 0, 'gmv')")])
    row = await run(parent(sub_id, top))
    metrics = await card(row.id, "top")
    [copy] = metrics["gmv"]["inputs"]
    # 子图出口 gmv 原样交出子图口径卡的指标：接上那个指标自己的出处，一路追到取数查询
    assert (copy["via"], copy["node_id"], copy["subgraph"], copy["field"]) == ("metric", "sub/card", "sub", "gmv")
    assert copy["path"] == "nodes.sub.output.gmv" and copy["value"] == "246.5"
    assert copy["upstream"]["traced"] is True and traced(copy)
    # 子图出口 table 原样交出查询结果：cell() 按台账认出是子图里的那份快照
    [cell] = metrics["raw"]["inputs"]
    assert (cell["via"], cell["node_id"], cell["status"]) == ("tool_cell", "sub/fetch", "verified")
    assert untraced(metrics["gmv"]["inputs"] + metrics["raw"]["inputs"]) == []
    assert row.output["_issuance"]["tier"] == "formal", row.output["_issuance"]["gaps"]

    # 证据面板：转抄的那一项后面接一步 upstream（子图里的卡），一直到子图里的取数查询
    body = (await client.get(f"/api/runs/{row.id}/evidence/metrics/top/gmv")).json()
    chain = body["chain"]
    assert chain[0]["untraced"] == []
    [inp] = [s for s in chain if s["step"] == "input"]
    assert inp["subgraph"] == "sub" and inp["subgraph_label"] == "取数子流程"
    assert inp["node_id"] == "sub/card" and inp["node_label"] == "取数子流程 › card"
    [up] = [s for s in chain if s["step"] == "upstream"]
    assert (up["node_id"], up["label"], up["sealed"], up["traced"]) == ("sub/card", "取数子流程 › card", True, True)
    [read] = up["inputs"]
    assert read["node_id"] == "sub/fetch" and read["node_label"] == "取数子流程 › fetch"
    [query] = [s for s in chain if s["step"] == "query"]
    assert query["artifact"] == read["artifact"] and query["node_id"] == "sub/fetch" and query["sealed"] is True


async def test_an_untraced_field_inside_the_subgraph_degrades_the_parent():
    inner = node("card", "metrics", caliber="子口径", caliber_version="v1", metrics=[metric("gmv", "vars.calc.v")])
    sub = S.chain(node("s", "input"),
                  node("calc", "transform", mode="json", template='{"v": 5}', assign_to="calc"), inner,
                  node("o", "output", fields=[{"name": "gmv", "value": "{{ nodes.card.metrics[0].value }}"}]))
    sub_id = await publish(sub)
    top = node("top", "metrics", caliber="父口径", caliber_version="v1", metrics=[metric("gmv", "float(nodes.sub.output.gmv)")])
    row = await run(parent(sub_id, top))
    [copy] = (await card(row.id, "top"))["gmv"]["inputs"]
    assert copy["upstream"]["traced"] is False and not traced(copy)
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert any("指标「gmv」的输入没追到原始数据：nodes.sub.output.gmv（停在「取数子流程」）" in g
               for g in issuance["gaps"]), issuance["gaps"]


async def test_a_field_that_is_not_a_whole_reference_stops_at_the_subgraph():
    sub_id = await publish(fetch_card_graph(out=[{"name": "gmv", "value": "合计 {{ nodes.card.metrics[0].value }}"}]))
    top = node("top", "metrics", caliber="父口径", caliber_version="v1", metrics=[metric("n", "len(nodes.sub.output.gmv)")])
    row = await run(parent(sub_id, top))
    [item] = (await card(row.id, "top"))["n"]["inputs"]
    assert (item["via"], item["node_id"]) == ("subgraph", "sub") and not traced(item)
    assert row.output["_issuance"]["tier"] == "degraded"


async def test_the_subgraph_run_input_is_traced_back_through_the_parent_input_template():
    """子图出口交出它的运行输入：子图的输入是父图按「输入」模板渲染的。模板整串就是父图的运行输入时追到原始数据；拼了
    别的字的停在子工作流的入口。"""
    sub = S.chain(node("s", "input", fields=[{"name": "region"}]),
                  node("o", "output", fields=[{"name": "region", "value": "{{ input.region }}"}]))
    sub_id = await publish(sub)
    top = node("top", "metrics", caliber="父口径", caliber_version="v1", metrics=[metric("r", "nodes.sub.output.region")])
    row = await run(parent(sub_id, top, sub_input={"region": "{{ input.region }}"}), {"region": "east"})
    [item] = (await card(row.id, "top"))["r"]["inputs"]
    assert (item["via"], item["path"], item["subgraph"]) == ("input", "nodes.sub.output.region", "sub") and traced(item)
    assert row.output["_issuance"]["tier"] == "formal", row.output["_issuance"]["gaps"]

    row = await run(parent(sub_id, top, sub_input={"region": "{{ input.region }}区"}), {"region": "east"})
    [item] = (await card(row.id, "top"))["r"]["inputs"]
    assert (item["via"], item["node_id"], item["field"]) == ("subgraph_input", "sub", "region") and not traced(item)


async def test_two_levels_of_nesting_trace_all_the_way_down(client):
    inner_id = await publish(fetch_card_graph(), "内层")
    middle = S.chain(node("m_in", "input"),
                     node("inner", "subgraph", "内层", workflow_id=inner_id, workflow_version=1),
                     node("m_out", "output", fields=[{"name": "gmv", "value": "{{ nodes.inner.output.gmv }}"}]))
    middle_id = await publish(middle, "中层")
    top = node("top", "metrics", caliber="父口径", caliber_version="v1", metrics=[metric("gmv", "float(nodes.sub.output.gmv)")])
    row = await run(parent(middle_id, top))
    evs = await events(row.id)
    assert "sub/inner/fetch" in {e.node_id for e in evs if e.type == "node.finished"}
    [copy] = (await card(row.id, "top"))["gmv"]["inputs"]
    assert (copy["via"], copy["node_id"], copy["subgraph"]) == ("metric", "sub/inner/card", "sub")
    assert copy["upstream"]["traced"] is True
    assert row.output["_issuance"]["tier"] == "formal", row.output["_issuance"]["gaps"]
    chain = (await client.get(f"/api/runs/{row.id}/evidence/metrics/top/gmv")).json()["chain"]
    [up] = [s for s in chain if s["step"] == "upstream"]
    assert up["node_id"] == "sub/inner/card" and up["sealed"] is True and up["label"] == "取数子流程 › 内层 › card"
    [query] = [s for s in chain if s["step"] == "query"]
    assert query["node_id"] == "sub/inner/fetch" and query["sealed"] is True


# --------------------------------------------------------------------------
# 静态检查：和运行时同一套认法
# --------------------------------------------------------------------------


async def _static(graph: dict) -> list[str]:
    """受管级按指标出具的发布检查里「指标追不到原始数据」的那几条（governed.metric_untraced），子工作流读好放进来。"""
    from app.engine.governance import lint_for_publish
    from app.engine.lint_process import load_subgraphs, with_subgraphs
    from app.engine.schema import GraphSpec

    spec = GraphSpec.model_validate(graph)
    async with SessionLocal() as session:
        graphs = await load_subgraphs(session, spec)
    with with_subgraphs(graphs):
        issues = lint_for_publish(spec, level="governed").issues
    return [i.message for i in issues if i.code == "governed.metric_untraced"]


async def test_the_static_check_agrees_with_the_run():
    from app.engine.governance import lint_for_publish
    from app.engine.schema import GraphSpec

    traced_sub = await publish(fetch_card_graph())
    inner = node("card", "metrics", caliber="子口径", caliber_version="v1", metrics=[metric("gmv", "vars.calc.v")])
    loose_sub = await publish(S.chain(
        node("s", "input"), node("calc", "transform", mode="json", template='{"v": 5}', assign_to="calc"), inner,
        node("o", "output", fields=[{"name": "gmv", "value": "{{ nodes.card.metrics[0].value }}"}])), "松的")
    top = node("top", "metrics", caliber="父口径", caliber_version="v1",
                 metrics=[metric("gmv", "float(nodes.sub.output.gmv)"), metric("raw", "cell(nodes.sub.output.table, 0, 'gmv')")])
    good = parent(traced_sub, top)
    bad = parent(loose_sub, node("top", "metrics", caliber="父口径", caliber_version="v1",
                                   metrics=[metric("gmv", "float(nodes.sub.output.gmv)")]))
    assert await _static(good) == []
    [said] = await _static(bad)
    assert "停在「取数子流程」" in said
    # 运行时同一个结论
    assert (await run(good)).output["_issuance"]["tier"] == "formal"
    assert (await run(bad)).output["_issuance"]["tier"] == "degraded"
    # 没把子工作流读好放进来（只看图本身的地方）：看不到子图，照旧停在子工作流节点
    plain = [i for i in lint_for_publish(GraphSpec.model_validate(good), level="governed").issues
             if i.code == "governed.metric_untraced"]
    assert plain and "停在「取数子流程」" in plain[0].message


async def test_the_static_check_follows_two_levels_and_the_input_template():
    inner_id = await publish(fetch_card_graph(), "内层")
    middle_id = await publish(S.chain(
        node("m_in", "input"), node("inner", "subgraph", "内层", workflow_id=inner_id, workflow_version=1),
        node("m_out", "output", fields=[{"name": "gmv", "value": "{{ nodes.inner.output.gmv }}"}])), "中层")
    top = node("top", "metrics", caliber="父口径", caliber_version="v1", metrics=[metric("gmv", "float(nodes.sub.output.gmv)")])
    assert await _static(parent(middle_id, top)) == []

    echo_id = await publish(S.chain(node("s", "input", fields=[{"name": "region"}]),
                                    node("o", "output", fields=[{"name": "region", "value": "{{ input.region }}"}])),
                            "回声")
    top = node("top", "metrics", caliber="父口径", caliber_version="v1", metrics=[metric("r", "nodes.sub.output.region")])
    assert await _static(parent(echo_id, top, sub_input={"region": "{{ input.region }}"})) == []
    assert await _static(parent(echo_id, top, sub_input={"region": "{{ input.region }}区"}))


# --------------------------------------------------------------------------
# 父图报告直接引用子图里的查询和口径卡
# --------------------------------------------------------------------------


def report_parent(sub_id: str) -> dict:
    return S.chain(
        node("start", "input"),
        node("sub", "subgraph", "取数子流程", workflow_id=sub_id, workflow_version=1),
        node("write", "report", instructions="写答案"),
        node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}],
               contract={"report_from": "write"}))


async def _segments(client: AsyncClient, run_id: str) -> dict[str, dict]:
    body = (await client.get(f"/api/runs/{run_id}/evidence")).json()
    assert body["mode"] == "cited", body
    return body


async def test_the_parent_report_cites_the_subgraph_query_and_card(client, monkeypatch):
    sub_id = await publish(fetch_card_graph())
    S.script(monkeypatch, "子流程里查到的销售额 [[v:Q1.r0.gmv]]，口径算的是 [[m:gmv]]。")
    started = await run_manager.start(graph=report_parent(sub_id), input_payload={})
    row = await S.wait(started.id)
    assert row.status == "succeeded", row.error
    body = await _segments(client, row.id)
    items = {e["alias"]: e for e in body["evidence"]}
    assert (items["Q1"]["node_id"], items["Q1"]["sealed"]) == ("sub/fetch", True)
    assert (items["m:gmv"]["node_id"], items["m:gmv"]["sealed"]) == ("sub/card", True)
    cited = {e["to"]: e["from"] for e in body["edges"] if e["rel"] == "cites"}
    # 单元格引用一路点到子图里的那一格
    cell = (await client.get(f"/api/runs/{row.id}/evidence/segments/{cited['Q1']}")).json()["chain"]
    [query] = [s for s in cell if s["step"] == "query"]
    assert query["node_id"] == "sub/fetch" and query["sealed"] is True and query["artifact"] == items["Q1"]["artifact"]
    # 指标引用：子图里的卡、它的输入、子图里的查询
    chain = (await client.get(f"/api/runs/{row.id}/evidence/segments/{cited['m:gmv']}")).json()["chain"]
    step = chain[0]
    assert (step["step"], step["node_id"], step["sealed"], step["untraced"]) == ("metric", "sub/card", True, [])
    [inp] = [s for s in chain if s["step"] == "input"]
    assert (inp["node_id"], inp["node_label"], inp["via"]) == ("sub/fetch", "取数子流程 › fetch", "tool_cell")
    [q] = [s for s in chain if s["step"] == "query"]
    assert q["node_id"] == "sub/fetch" and q["sealed"] is True
    # 封存核对通过，可验证报告照常（事件本来就在同一个运行里）
    from app.engine.runner import verify_manifest

    assert (await verify_manifest(row.id))["ok"] is True
    res = await client.get(f"/api/runs/{row.id}/report.html")
    assert res.status_code == 200 and "246.5" in res.text


#: 载入可验证报告的核对脚本，交回引用链上每份报告文档的核对结果（同 tests/test_bundle_rejected.py 的做法）
VERIFY_CHAIN = r"""
const fs = require('fs')
const { createHash } = require('crypto')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const mod = { exports: {} }
new Function('module', fs.readFileSync(process.argv[1], 'utf8'))(mod)
const digest = async (bytes) => createHash('sha256').update(bytes).digest('hex')
mod.exports.verify(input.data, digest, input.data.meta.manifest_hash).then((out) => {
  process.stdout.write(JSON.stringify(out.chain.filter((c) => c.kind === 'report_doc')
    .map((c) => ({ node: c.report && c.report.node_id, result: c.result, reason: c.reason || '' }))))
}, (e) => { console.error((e && e.stack) || String(e)); process.exit(1) })
"""


async def test_a_report_inside_the_subgraph_is_not_tampered_and_not_the_parents_answer(client, monkeypatch):
    """子图里有报告撰写节点：它的 report.checked 进了父运行（节点 id「sub/write」），报告文档记的也是这次运行里的 id，
    不被认成「疑似被修改」。父图自己没有报告：父图的答案是子图出口的值，不是子图那份报告——证据图、评测读答案都按父图
    自己认（和子图事件进运行之前一样：没有报告），子图的报告按 ?report=sub/write 点得开，可验证报告里核对通过。"""
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    import app.bundle
    from app.api.evidence import _reports, _sealed
    from app.eval.score import read_answer

    sub = S.chain(node("s", "input"), node("fetch", "tool", tool=S.tool(), args={"sql": PAID}),
                  node("write", "report", instructions="写答案"),
                  node("o", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}],
                       contract={"report_from": "write"}))
    sub_id = await publish(sub)
    graph = S.chain(node("start", "input"), node("sub", "subgraph", "取数子流程", workflow_id=sub_id, workflow_version=1),
                    node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.sub.output.答案 }}"}]))
    S.script(monkeypatch, "子流程里查到的销售额 [[v:Q1.r0.gmv]]。")
    started = await run_manager.start(graph=graph, input_payload={})
    row = await S.wait(started.id)
    assert row.status == "succeeded", row.error

    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    # 父图没有报告、没有契约：照没有契约的答案按数值猜候选（legacy_text，写明是猜的）。子图查过的数如今在封存范围里，
    # 猜得到它；不再把子图的报告当成父图的、更不说它被改过
    assert (body["mode"], body["reports"]) == ("legacy_text", []), body
    [guessed] = [c for f in body["legacy"]["fields"] for seg in f["segments"] for c in seg.get("candidates") or []]
    assert (guessed["node_id"], guessed["value"]) == ("sub/fetch", 246.5)
    # 子图自己的出具契约照常核对它那份报告（文档上记的 id 和这一层的报告节点对得上）
    from app.core.artifact_store import load

    inner_issuance = load((await _sealed(row.id)).outputs["sub"])["output"]["_issuance"]
    assert inner_issuance["tier"] == "formal" and inner_issuance["report"]["node_id"] == "sub/write", inner_issuance
    answer = await read_answer(row.id)
    assert answer.problem is None and answer.report is None and "246.5" in row.output["答案"], answer
    # 子图的报告本身：这次运行里的 id、哈希核对通过、点得开
    [inner] = _reports(await _sealed(row.id), nested=True)
    assert (inner.node_id, inner.hash_ok, (inner.doc or {}).get("node_id")) == ("sub/write", True, "sub/write")
    seg = next(s["id"] for b in inner.doc["blocks"] for u in b["units"] for s in u["segments"]
               if (s.get("cite") or {}).get("kind") == "cell")
    res = await client.get(f"/api/runs/{row.id}/evidence/segments/{seg}", params={"report": "sub/write"})
    assert res.status_code == 200, res.text
    [query] = [st for st in res.json()["chain"] if st["step"] == "query"]
    assert query["node_id"] == "sub/fetch" and query["sealed"] is True
    # 可验证报告：子图那份报告的文档在引用链上核对通过（文档记的节点 id 和自查事件对得上）
    node_bin = shutil.which("node")
    if node_bin:
        html = (await client.get(f"/api/runs/{row.id}/bundle.html")).text
        data = json.loads(re.search(r'<script type="application/json" id="bundle-data">(.*?)</script>', html, re.S).group(1))
        verifier = Path(app.bundle.__file__).parent / "verifier.js"
        done = subprocess.run([node_bin, "-e", VERIFY_CHAIN, str(verifier)], input=json.dumps({"data": data}),
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-2000:]
        assert json.loads(done.stdout) == [{"node": "sub/write", "result": "pass", "reason": ""}]
        # 这次运行没有出具判定（子图成果节点的那条是子图自己的）：元信息不写档位
        assert data["meta"]["tier"] is None, data["meta"]["tier"]


@pytest.mark.parametrize("namesake", [False, True])
async def test_the_bundle_and_whatif_read_a_subgraph_card_in_its_own_layer(client, monkeypatch, namesake):
    """子图里一张卡转抄同一张子图里的另一张（card1 ← card0），父图报告引用 [[m:g1]]，全程没设遮罩：卡上记的是子图里的 id
    （nodes.card0…），可验证报告、推演都按卡所在的那一层找产出——找得到，不当成「核对不了」整张遮住；父图里另有一个同名的
    card0（namesake）时，也不拿它当依据（推演还原转抄来的值，还原成子图那张的 246.5，不是父图那张的 1）。"""
    import json
    import re

    # card0 另有一个按位置取的格（vars.rows.rows[0][0]，经原样解析查询结果的 parse）：可验证报告、推演都要顺着取值链从
    # parse 那一次的产出走
    sub = S.chain(node("s", "input"), node("fetch", "tool", tool=S.tool(), args={"sql": PAID}),
                  node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="rows"),
                  node("card0", "metrics", caliber="子口径", caliber_version="v1",
                       metrics=[GMV, metric("first", "vars.rows.rows[0][0]"),
                                metric("cnt", "len(nodes.parse.rows)")]),
                  node("card1", "metrics", caliber="转抄", caliber_version="v1",
                       metrics=[metric("g1", "nodes.card0.metrics[0].value")]),
                  node("o", "output", fields=[{"name": "gmv", "value": "{{ nodes.card1.metrics[0].value }}"}]))
    sub_id = await publish(sub)
    parts = [node("start", "input")]
    if namesake:
        parts += [node("card0", "metrics", caliber="父口径", caliber_version="v1", metrics=[metric("one", "1")]),
                  node("parse", "transform", mode="json", template='{"rows": [[1], [2]]}', assign_to="other")]
    parts += [node("sub", "subgraph", "取数子流程", workflow_id=sub_id, workflow_version=1),
              node("write", "report", instructions="写答案"),
              node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}])]
    S.script(monkeypatch, "转抄的口径 [[m:g1]]，第一行 [[m:first]]。")
    started = await run_manager.start(graph=S.chain(*parts), input_payload={})
    row = await S.wait(started.id)
    assert row.status == "succeeded", row.error
    card1 = next(e["artifact"] for e in (await card_entries(row.id)) if e["node_id"] == "sub/card1")
    html = (await client.get(f"/api/runs/{row.id}/bundle.html")).text
    data = json.loads(re.search(r'<script type="application/json" id="bundle-data">(.*?)</script>', html, re.S).group(1))
    entry = next(a for a in data["artifacts"] if a["id"] == card1)
    assert "masked" not in entry and entry.get("b64"), entry
    # 推演：转抄的那个输入按子图里 card0 那次的产出还原，什么都不改时值不变、没有推演不了的
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "sub/card1"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["unsupported"] == [], body["unsupported"]
    [g1] = [m for m in body["metrics"] if m.get("id") == "g1"]
    assert g1.get("changed") in (False, None) and g1.get("new") == g1.get("old") == 246.5, g1
    card0 = next(e["artifact"] for e in (await card_entries(row.id)) if e["node_id"] == "sub/card0")
    entry = next(a for a in data["artifacts"] if a["id"] == card0)
    assert "masked" not in entry and entry.get("b64"), entry
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "sub/card0"})
    assert res.status_code == 200 and res.json()["unsupported"] == [], res.json()
    # nodes.parse.rows 这种整份产出的输入从 parse 那一次的产出还原：子图里的 parse（1 行），不是父图里同名的那个（2 行）
    [cnt] = [m for m in res.json()["metrics"] if m.get("id") == "cnt"]
    assert cnt.get("new") == cnt.get("old") == 1, cnt


async def card_entries(run_id: str) -> list[dict]:
    from app.api.evidence import _sealed

    return [e for e in (await _sealed(run_id)).entries if e.get("kind") == "metric_set"]


async def test_masks_inside_the_subgraph_follow_the_parent_run(client, monkeypatch, tmp_path):
    """数据源遮了 phone：子图里查出来的号码在父图报告的证据面板里照样写「已遮罩」。"""
    await S.set_source(tmp_path / "shop.db", options={"mask_columns": "phone"})
    amount = node("card", "metrics", caliber="子口径", caliber_version="v1",
                  metrics=[metric("gmv", "cell(nodes.fetch, 0, 'amount')")])
    sub_id = await publish(fetch_card_graph(card=amount, sql="SELECT phone, amount FROM orders WHERE id = 1"))
    S.script(monkeypatch, "第一单的电话 [[v:Q1.r0.phone]]。")
    started = await run_manager.start(graph=report_parent(sub_id), input_payload={})
    row = await S.wait(started.id)
    assert row.status == "succeeded", row.error
    body = await _segments(client, row.id)
    cited = {e["to"]: e["from"] for e in body["edges"] if e["rel"] == "cites"}
    chain = (await client.get(f"/api/runs/{row.id}/evidence/segments/{cited['Q1']}")).json()["chain"]
    [query] = [s for s in chain if s["step"] == "query"]
    assert query["node_id"] == "sub/fetch"
    assert "13800000001" not in str(query), "子图里的遮罩列照父图的运行遮"
    assert "已遮罩" in str(query) and query["sealed"] is True
