"""发布前检查（publish-check）和自动修复（autofix）两个接口。

- publish-check 的问题和真正发布时拦下的完全一致（同一套 validate + 门禁）
- 两个接口都只读：调用前后库里的工作流、版本、状态一个字都不变
- autofix 返回的图（首批里能 auto 的那几类）真的能过发布检查，存下来也真能发布
"""
from __future__ import annotations

import copy

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Workflow, WorkflowVersion
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 40, "y": 80}, "data": {"label": label or nid, "config": config}}


def weekly(**contract_overrides) -> dict:
    contract = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}
    contract.update(contract_overrides)
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool="db_query__shop",
             args={"sql": "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "orders", "name": "订单数", "unit": "单", "expression": "cell(nodes.fetch, 0, 'orders')"}]),
        node("write", "report", "报告撰写", instructions="写周报"),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={k: v for k, v in contract.items() if v is not None}),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def broken(lib_id: str | None = None) -> dict:
    """首批里 auto 能修的几类问题各来一处。"""
    graph = weekly(metrics_from=None, report_from="fetch", strict=False)
    graph["nodes"].insert(2, node("ask", "agent", "查数员", tools=["db_query__shop"], approval="never",
                                  prompt="用 db_query__shop 查"))
    graph["edges"] += [{"source": "start", "target": "ask"}, {"source": "ask", "target": "card"}]
    if lib_id:
        graph["nodes"].insert(1, node("lib", "subgraph", "方法卡", workflow_id=lib_id))
        graph["edges"] += [{"source": "start", "target": "lib"}, {"source": "lib", "target": "fetch"}]
    return graph


async def create(client, graph: dict, name: str = "周报-发布检查") -> str:
    r = await client.post("/api/workflows", json={"name": name, "graph": graph})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def library(client) -> str:
    """一张已发布过 v1、之后又存了 v2 草稿的上游：当前的发布版本是 v1。"""
    lib = await create(client, weekly(), name="方法卡库")
    assert (await client.post(f"/api/workflows/{lib}/publish", json={"level": "published"})).json()["ok"]
    changed = weekly()
    changed["nodes"][3]["data"]["label"] = "改过的报告"
    await client.patch(f"/api/workflows/{lib}", json={"graph": changed})
    return lib


async def state(*workflow_ids: str) -> list:
    """库里这些工作流和它们全部版本的每一列。"""
    async with SessionLocal() as session:
        rows = []
        for wid in workflow_ids:
            wf = await session.get(Workflow, wid)
            rows.append({c.name: getattr(wf, c.name) for c in Workflow.__table__.columns})
            versions = (await session.execute(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wid)
                                              .order_by(WorkflowVersion.version))).scalars()
            rows += [{c.name: getattr(v, c.name) for c in WorkflowVersion.__table__.columns} for v in versions]
        count = len((await session.execute(select(WorkflowVersion.id))).all())
        return [rows, count]


def shape(issues: list[dict]) -> list[tuple]:
    return [(i["level"], i.get("code"), i.get("node_id"), i.get("field"), i["message"]) for i in issues]


@pytest.mark.parametrize("level", ["governed", "published"])
async def test_publish_check_matches_what_publishing_blocks(client, level):
    lib = await library(client)
    wf = await create(client, broken(lib))
    checked = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": level})).json()
    published = (await client.post(f"/api/workflows/{wf}/publish", json={"level": level})).json()
    assert checked["level"] == level and checked["ok"] is published["ok"] is False
    assert shape(checked["issues"]) == shape(published["issues"])
    assert all("fix" in i for i in checked["issues"])


async def test_publish_check_reports_codes_and_fixes(client):
    lib = await library(client)
    wf = await create(client, broken(lib))
    body = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "governed"})).json()
    fixes = {f["id"]: f for f in body["fixes"]}
    assert {"governed.agent_approval_never:ask", "governed.subgraph_unpinned:lib", "contract.metrics_from_missing:done",
            "contract.report_from_invalid:done", "contract.strict_off:done"} <= set(fixes)
    # 钉的是当前的发布版本 v1，不是最新存的草稿 v2
    assert fixes["governed.subgraph_unpinned:lib"]["preview"]["after"] == 1
    assert all(not k.startswith("_") for f in body["fixes"] for k in f)
    by_code = {i["code"]: i for i in body["issues"] if i.get("code")}
    assert by_code["governed.agent_approval_never"]["fix"] == "governed.agent_approval_never:ask"
    # 没有修复的问题 fix 是 null（老前端不认这两个字段也不影响）
    assert all(i["fix"] is None for i in body["issues"] if not i.get("code"))


async def test_publish_check_takes_an_unsaved_graph(client):
    wf = await create(client, weekly())
    assert (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "governed"})).json()["ok"] is True
    body = (await client.post(f"/api/workflows/{wf}/publish-check",
                              json={"level": "governed", "graph": weekly(required=None)})).json()
    assert body["ok"] is False and body["fixes"][0]["id"] == "contract.required_missing:done"


async def test_both_endpoints_leave_the_database_alone(client):
    lib = await library(client)
    wf = await create(client, broken(lib))
    before = await state(wf, lib)
    await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "governed"})
    await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "published", "graph": weekly()})
    fixes = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "governed"})).json()["fixes"]
    out = (await client.post(f"/api/workflows/{wf}/autofix", json={
        "level": "governed", "apply": [f["id"] for f in fixes if f["kind"] == "auto"]})).json()
    assert out["applied"]
    await client.post(f"/api/workflows/{wf}/autofix", json={"level": "governed", "graph": weekly(required=None),
                                                            "apply": ["contract.required_missing:done"],
                                                            "choices": {"contract.required_missing:done": ["gmv"]}})
    assert await state(wf, lib) == before


async def test_autofixed_graph_passes_the_check_and_really_publishes(client):
    lib = await library(client)
    graph = broken(lib)
    wf = await create(client, graph)
    fixes = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "governed"})).json()["fixes"]
    autos = [f["id"] for f in fixes if f["kind"] == "auto"]
    assert len(autos) == 5, autos
    out = (await client.post(f"/api/workflows/{wf}/autofix", json={"level": "governed", "apply": autos})).json()
    assert sorted(out["applied"]) == sorted(autos) and out["rejected"] == [] and out["ok"] is True
    assert [i for i in out["remaining"] if i["level"] == "error"] == []
    assert {c["fix_id"] for c in out["changes"]} == set(autos)
    assert all(op["op"] == "update_node" for op in out["ops"])
    # 预览只改了修复涉及的字段
    fixed = out["graph"]
    assert [n["position"] for n in fixed["nodes"]] == [n["position"] for n in graph["nodes"]]
    checked = (await client.post(f"/api/workflows/{wf}/publish-check",
                                 json={"level": "governed", "graph": fixed})).json()
    assert checked["ok"] is True, [i for i in checked["issues"] if i["level"] == "error"]
    # 人确认后前端走现有的保存接口，再发布
    await client.patch(f"/api/workflows/{wf}", json={"graph": fixed, "note": "发布前自动修复"})
    published = (await client.post(f"/api/workflows/{wf}/publish", json={"level": "governed"})).json()
    assert published["ok"] is True, published["issues"]


async def test_autofix_with_a_choice_and_without_it(client):
    wf = await create(client, weekly(required=None))
    fid = "contract.required_missing:done"
    out = (await client.post(f"/api/workflows/{wf}/autofix", json={"level": "governed", "apply": [fid]})).json()
    assert out["applied"] == [] and out["rejected"][0]["fix_id"] == fid and out["ok"] is False
    assert [i["fix"] for i in out["remaining"] if i.get("code") == "contract.required_missing"] == [fid]
    out = (await client.post(f"/api/workflows/{wf}/autofix", json={
        "level": "governed", "apply": [fid], "choices": {fid: ["orders", "gmv"]}})).json()
    assert out["ok"] is True
    contract = next(n for n in out["graph"]["nodes"] if n["id"] == "done")["data"]["config"]["contract"]
    assert contract["required"] == ["gmv", "orders"]


async def test_required_candidates_come_from_the_pinned_card(client):
    lib = await create(client, weekly(), name="口径库")
    local = weekly(required=None)
    card = local["nodes"][2]["data"]["config"]
    card.pop("metrics")
    card["caliber_from"] = {"workflow_id": lib, "workflow_version": 1, "node_id": "card"}
    wf = await create(client, local)
    body = (await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "governed"})).json()
    [fix] = [f for f in body["fixes"] if f["id"] == "contract.required_missing:done"]
    assert fix["kind"] == "choice" and [o["value"] for o in fix["options"]] == ["gmv", "orders"]


async def test_bad_requests(client):
    wf = await create(client, weekly())
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": "gold"})
    assert r.status_code == 400 and "「已发布」" in r.json()["detail"]
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "gold", "apply": []})
    assert r.status_code == 400
    r = await client.post("/api/workflows/nope/publish-check", json={"level": "governed"})
    assert r.status_code == 404
    r = await client.post("/api/workflows/nope/autofix", json={"level": "governed", "apply": []})
    assert r.status_code == 404
    body = (await client.post(f"/api/workflows/{wf}/publish-check",
                              json={"level": "governed", "graph": {"nodes": [{"id": "a", "type": "nope"}]}})).json()
    assert body["ok"] is False and body["fixes"] == [] and "读不懂" in body["issues"][0]["message"]
    body = (await client.post(f"/api/workflows/{wf}/autofix", json={
        "level": "governed", "graph": {"nodes": [{"id": "a", "type": "nope"}]}, "apply": ["x:y"]})).json()
    assert body["ok"] is False and body["applied"] == [] and "读不懂" in body["remaining"][0]["message"]


async def test_the_draft_in_the_database_is_the_default(client):
    wf = await create(client, weekly(strict=False))
    graph = copy.deepcopy(weekly(strict=False))
    out = (await client.post(f"/api/workflows/{wf}/autofix",
                             json={"level": "governed", "apply": ["contract.strict_off:done"]})).json()
    assert out["applied"] == ["contract.strict_off:done"]
    contract = next(n for n in out["graph"]["nodes"] if n["id"] == "done")["data"]["config"]["contract"]
    assert contract["strict"] is True
    assert (await client.get(f"/api/workflows/{wf}")).json()["graph"] == graph


@pytest.mark.parametrize("contract", ["abc", ["gmv"], 5])
def test_a_contract_that_is_not_an_object_is_a_validation_error(contract):
    """非空、却不是对象的契约：validate 报一条 error（两档发布、发起运行都挡住），不在 .get 上崩。"""
    from app.engine.governance import publish_issues
    from app.engine.schema import GraphSpec, validate_graph

    graph = weekly()
    graph["nodes"][4]["data"]["config"]["contract"] = contract
    spec = GraphSpec.model_validate(graph)
    [issue] = [i for i in validate_graph(spec).issues if i.code == "contract.not_object"]
    assert issue.level == "error" and issue.node_id == "done" and issue.field == "contract"
    assert "JSON 对象" in issue.message
    for level in ("published", "governed"):
        assert any(i.code == "contract.not_object" and i.level == "error" for i in publish_issues(spec, level=level))


@pytest.mark.parametrize("level", ["governed", "published"])
async def test_a_string_contract_is_reported_not_a_crash(client, level):
    graph = weekly()
    graph["nodes"][4]["data"]["config"]["contract"] = "report_from=write"
    wf = await create(client, graph)
    before = await state(wf)
    r = await client.post(f"/api/workflows/{wf}/publish-check", json={"level": level})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert [(i["code"], i["node_id"]) for i in body["issues"] if i["level"] == "error"][0] == (
        "contract.not_object", "done")
    # 修不了确定性修复：交给 Copilot
    [fix] = [f for f in body["fixes"] if f["code"] == "contract.not_object"]
    assert fix["kind"] == "assist" and fix["node_id"] == "done"
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": level, "apply": [fix["id"]]})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["applied"] == []
    r = await client.post(f"/api/workflows/{wf}/publish", json={"level": level})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert await state(wf) == before
