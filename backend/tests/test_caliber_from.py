"""口径卡钉住另一个已发布工作流某个版本里的口径卡（caliber_from），版本纳入升版处置。

用户拍板：口径卡的版本和子工作流一样纳入升版处置，不新造实体。

- caliber_from: {workflow_id, workflow_version, node_id}：运行时从那个 WorkflowVersion 的图里
  取出口径卡的 caliber、caliber_version、metrics 定义，metric_set 里记下来源
- 引用的工作流、版本或节点不存在：节点失败，报错写清楚
- 上游有更新的版本时，正式运行前必须声明 upgrade_policy：没声明就挡住，声明了记
  caliber.upgrade 事件（node_id、pinned、latest、policy、policy_label）
- 证据面板的指标步骤带上来源工作流和版本，以及升版处置
"""
from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

KPI = "{'gmv': 45678.5, 'orders': 1234}"
STANDARD = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands", "expression": "vars.kpi.gmv"},
    {"id": "orders", "name": "订单数", "unit": "单", "format": "thousands", "expression": "vars.kpi.orders"},
]


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def library(metrics=STANDARD, version="v3"):
    """上游：一张只放口径卡的工作流（口径库）。"""
    return chain(node("start", "input"),
                 node("caliber", "metrics", "周报口径卡", caliber="周报口径", caliber_version=version, metrics=metrics),
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.caliber.text }}"}]))


async def publish(graph, *, name="口径库", versions=1, status="published", later=None) -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=name, graph=graph, status=status, published_version=versions, version=versions)
        session.add(wf)
        await session.flush()
        for v in range(1, versions + 1):
            session.add(WorkflowVersion(workflow_id=wf.id, version=v, graph=later if later and v > 1 else graph))
        await session.commit()
        return wf.id


def weekly(pinned: dict, *, report=False, **card):
    nodes = [
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
        node("card", "metrics", "本地口径卡", caliber_from=pinned, **card),
    ]
    if report:
        nodes.append(node("write", "report", instructions="写周报", on_violation="flag"))
        nodes.append(node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]))
    else:
        nodes.append(node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))
    return chain(*nodes)


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def explore(graph) -> Run:
    return await wait((await run_manager.start(graph=graph, input_payload={})).id)


# --------------------------------------------------------------------------
# 取定义、记来源
# --------------------------------------------------------------------------


async def test_definitions_come_from_the_pinned_version():
    wf_id = await publish(library())
    pinned = {"workflow_id": wf_id, "workflow_version": 1, "node_id": "caliber"}
    row = await explore(weekly(pinned))
    assert row.status == "succeeded", row.error

    [finished] = await events(row.id, "node.finished", "card")
    card = artifact_store.load(finished.data["artifact"])
    assert (card["caliber"], card["caliber_version"]) == ("周报口径", "v3")
    assert [(m["id"], m["value"], m["rendered"]) for m in card["metrics"]] == [
        ("gmv", 45678.5, "45,678.5元"), ("orders", 1234, "1,234单")]
    assert card["source"] == pinned
    # 落进工件库的指标集里也记着来源：证据接口从这里取
    [entry] = finished.data["evidence"]
    stored = artifact_store.load(entry["artifact"])
    assert stored["source"] == pinned and entry["source"] == pinned


async def test_later_versions_do_not_leak_into_a_pinned_card():
    """钉住 v1 就用 v1 的定义：上游 v2 改了算法，这次运行照旧按 v1 算。"""
    changed = [{**STANDARD[0], "expression": "vars.kpi.gmv * 2"}, STANDARD[1]]
    wf_id = await publish(library(), versions=2, later=library(changed, version="v4"))
    row = await explore(weekly({"workflow_id": wf_id, "workflow_version": 1, "node_id": "caliber"}))
    [finished] = await events(row.id, "node.finished", "card")
    card = artifact_store.load(finished.data["artifact"])
    assert card["caliber_version"] == "v3" and card["metrics"][0]["value"] == 45678.5


async def test_graphs_without_caliber_from_are_unchanged():
    row = await explore(chain(node("start", "input"),
                              node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
                              node("card", "metrics", caliber="周报口径", metrics=STANDARD),
                              node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}])))
    [finished] = await events(row.id, "node.finished", "card")
    card = artifact_store.load(finished.data["artifact"])
    assert "source" not in card and "source" not in finished.data["evidence"][0]


# --------------------------------------------------------------------------
# 引用不存在
# --------------------------------------------------------------------------


@pytest.mark.parametrize("pin, words", [
    (lambda wf: {"workflow_id": "no-such-workflow", "workflow_version": 1, "node_id": "caliber"},
     ["no-such-workflow", "不存在"]),
    (lambda wf: {"workflow_id": wf, "workflow_version": 7, "node_id": "caliber"}, ["口径库", "v7"]),
    (lambda wf: {"workflow_id": wf, "workflow_version": 1, "node_id": "nope"}, ["口径库", "v1", "nope"]),
    (lambda wf: {"workflow_id": wf, "workflow_version": 1, "node_id": "out"}, ["不是口径卡"]),
])
async def test_missing_references_fail_the_node_clearly(pin, words):
    wf_id = await publish(library())
    row = await explore(weekly(pin(wf_id)))
    assert row.status == "failed"
    [failed] = await events(row.id, "node.failed", "card")
    assert all(w in failed.data["error"] for w in words), failed.data["error"]


async def test_pinning_a_card_that_is_itself_pinned_is_refused():
    base = await publish(library(), name="源头口径")
    middle = await publish(chain(node("start", "input"),
                                 node("caliber", "metrics", caliber_from={"workflow_id": base, "workflow_version": 1,
                                                                          "node_id": "caliber"}),
                                 node("out", "output", fields=[{"name": "r", "value": "x"}])), name="转手口径")
    row = await explore(weekly({"workflow_id": middle, "workflow_version": 1, "node_id": "caliber"}))
    [failed] = await events(row.id, "node.failed", "card")
    assert "源头" in failed.data["error"] and "源头口径" in failed.data["error"], failed.data["error"]


# --------------------------------------------------------------------------
# 升版处置：正式运行
# --------------------------------------------------------------------------


async def formal(client, graph):
    wf_id = await publish(graph, name="周报")
    return await client.post("/api/runs", json={"workflow_id": wf_id, "run_class": "formal"})


async def test_a_newer_upstream_blocks_formal_runs_without_a_policy(client):
    lib = await publish(library(), versions=2, later=library(version="v4"))
    res = await formal(client, weekly({"workflow_id": lib, "workflow_version": 1, "node_id": "caliber"}))
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert "「本地口径卡」" in detail and "v1" in detail and "v2" in detail and "upgrade_policy" in detail


async def test_a_declared_policy_is_recorded(client):
    lib = await publish(library(), versions=2, later=library(version="v4"))
    res = await formal(client, weekly({"workflow_id": lib, "workflow_version": 1, "node_id": "caliber"},
                                      upgrade_policy="dual"))
    assert res.status_code == 201, res.text
    row = await wait(res.json()["id"])
    assert row.status == "succeeded", row.error
    [event] = await events(row.id, "caliber.upgrade")
    assert event.node_id == "card"
    assert {k: event.data.get(k) for k in ("workflow_id", "pinned", "latest", "policy", "policy_label",
                                            "caliber_node")} == {
        "workflow_id": lib, "pinned": 1, "latest": 2, "policy": "dual", "policy_label": "并排双印新旧口径",
        "caliber_node": "caliber"}


async def test_pinned_to_the_latest_needs_no_policy(client):
    lib = await publish(library())
    res = await formal(client, weekly({"workflow_id": lib, "workflow_version": 1, "node_id": "caliber"}))
    assert res.status_code == 201, res.text
    row = await wait(res.json()["id"])
    assert await events(row.id, "caliber.upgrade") == []


# --------------------------------------------------------------------------
# 证据面板的指标步骤
# --------------------------------------------------------------------------


async def test_metric_step_shows_the_source_and_the_upgrade(client, monkeypatch):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda self, messages: AIMessage(content="销售额 [[m:gmv]]。"))
    lib = await publish(library(), versions=2, later=library(version="v4"))
    pinned = {"workflow_id": lib, "workflow_version": 1, "node_id": "caliber"}
    res = await formal(client, weekly(pinned, report=True, upgrade_policy="incomparable"))
    row = await wait(res.json()["id"])
    assert row.status == "succeeded", row.error
    doc_id = row.output["_evidence"]["doc_artifact"]
    doc = artifact_store.load(doc_id)
    seg = next(s for s in iter_segments(doc) if s["text"] == "45,678.5元")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    metric = body["chain"][0]
    assert metric["source"] == {**pinned, "workflow_name": "口径库"}
    assert (metric["caliber"], metric["version"]) == ("周报口径", "v3")
    assert metric["caliber_upgrade"] == {"workflow_id": lib, "caliber_node": "caliber", "pinned": 1, "latest": 2,
                                         "policy": "incomparable", "policy_label": "标注与历史不可比"}
    assert json.dumps(metric, ensure_ascii=False)


async def test_subgraphs_keep_the_same_upgrade_rule(client):
    """子工作流的升版处置原样不变：口径卡是并进同一套规则，不是另起一套。"""
    lib = await publish(library(), versions=2, later=library(version="v4"))
    graph = chain(node("start", "input"),
                  node("sub", "subgraph", "方法卡", workflow_id=lib, workflow_version=1),
                  node("out", "output", fields=[{"name": "r", "value": "{{ nodes.sub.text }}"}]))
    res = await formal(client, graph)
    assert res.status_code == 409 and "「方法卡」（子工作流）钉在 v1，但上游已有 v2" in res.json()["detail"]
