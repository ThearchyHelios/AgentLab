"""按新口径补算（D-f；设计文档 §3.4 趋势那一行、X3 的 app_backfill）。

- 只有视图的钉住项最近一次升版选了 recompute 才补算；取旧版本在同一来源上最近 N 期（N 不超过趋势的 last）已封存正式
  运行的输入，用新版本逐个发起正式运行（来源 app_backfill，带 app_id / app_version，按钉住的 graph_hash 核对）；
- 新版本已经算过同一个输入的跳过（补算过的、来源本身按新版本跑过的），再点一次不重复发起；
- 趋势把补算的运行算作新版本的点，按它补的那一期排；「最近一次」不取补算的运行；
- 选了别的处置、视图不存在的拒绝；详情里给管理者列出能补算的视图和会发起几次；
- 有外部副作用的工作流（人工节点、写外部系统、钉作流程）不补算，详情里也不列；
- 同一个视图并发点两次，同一个输入只发起一次。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.main import app


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


def kpi(gmv: int) -> dict:
    nodes = [
        node("start", "input", "开始", fields=[{"name": "week", "required": True}]),
        node("fetch", "transform", "取数", mode="expression", expression=f"{{'gmv': {gmv}}}", assign_to="kpi"),
        node("card", "metrics", "口径卡", caliber="周报口径", caliber_version="v1",
             metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]),
        node("out", "output", "成果", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def workflow(v2: dict | None = None) -> tuple[str, str, str]:
    """v1、v2 两个发布版，口径不同（gmv 100 → 200）。返回 (工作流, v1 哈希, v2 哈希)。"""
    v1, v2 = kpi(100), v2 or kpi(200)
    async with SessionLocal() as session:
        wf = Workflow(name=f"周报-{uuid.uuid4().hex[:6]}", graph=v2, status="published", published_version=2, version=2)
        session.add(wf)
        await session.flush()
        for number, graph in ((1, v1), (2, v2)):
            session.add(WorkflowVersion(workflow_id=wf.id, version=number, graph=graph, graph_hash=graph_hash(graph),
                                        level="published", published_at=utcnow()))
        await session.commit()
        return wf.id, graph_hash(v1), graph_hash(v2)


async def wait(run_id: str) -> Run:
    for _ in range(600):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


async def formal(wf: str, version: int, week: str) -> Run:
    from app.engine.launch import launch_run

    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=wf, run_class="formal", version=version,
                               input_payload={"week": week}, actor="周报编辑", origin_kind="launcher")
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    return row


def spec(wf: str, version: int, hash_: str) -> dict:
    return {
        "format": "agentlab.app/1", "name": "周报看板",
        "pins": [{"alias": "main", "workflow_id": wf, "version": version, "graph_hash": hash_, "use": "view"}],
        "views": {"latest": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}}},
        "pages": [{"id": "home", "title": "首页", "body": [
            {"id": "trend", "type": "series", "props": {"label": "销售额趋势"},
             "bind": {"view": "latest", "series": "card.gmv", "last": 6}},
            {"id": "gmv", "type": "metric", "props": {"label": "销售额"}, "bind": {"view": "latest", "metric": "card.gmv"}},
        ]}],
    }


async def upgraded_app(client, wf: str, h1: str, h2: str, disposition: str) -> str:
    res = await client.post("/api/apps", json={"name": "周报看板", "spec": spec(wf, 1, h1)})
    assert res.status_code == 201, res.text
    app_id = res.json()["id"]
    assert (await client.post(f"/api/apps/{app_id}/publish", json={})).json()["ok"]
    draft = (await client.get(f"/api/apps/{app_id}")).json()["draft"]
    draft["pins"][0].update(version=2, graph_hash=h2)
    assert (await client.patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200
    out = (await client.post(f"/api/apps/{app_id}/publish", json={"dispositions": {"main": disposition}})).json()
    assert out["ok"] and out["version"] == 2, out
    return app_id


def find(vm, cid):
    return next(n for n in vm["tree"] if n["id"] == cid)


async def test_backfill_recomputes_the_old_periods_under_the_new_version(client):
    wf, h1, h2 = await workflow()
    old = [await formal(wf, 1, w) for w in ("W1", "W2", "W3")]
    app_id = await upgraded_app(client, wf, h1, h2, "recompute")
    current = await formal(wf, 2, "W3")         # 升版之后新版本已经跑过 W3：这一期不补

    plan = (await client.get(f"/api/apps/{app_id}/views/latest/backfill")).json()
    assert plan == {"view": "latest", "alias": "main", "source": "formal", "from_version": 1, "to_version": 2,
                    "periods": 6, "will_launch": 2, "will_skip": 1}
    detail = (await client.get(f"/api/apps/{app_id}")).json()
    assert detail["backfill"] == [plan], "管理者在详情里看得到能补算的视图和会发起几次"

    out = (await client.post(f"/api/apps/{app_id}/views/latest/backfill")).json()
    assert (out["launched"], out["skipped"]) == (2, 1) and len(out["run_ids"]) == 2
    runs = [await wait(r) for r in out["run_ids"]]
    assert all(r.status == "succeeded" for r in runs), [r.error for r in runs]
    assert [(r.origin_kind, r.app_id, r.app_version, r.version, r.version_hash, r.run_class) for r in runs] == \
        [("app_backfill", app_id, 2, 2, h2, "formal")] * 2
    assert [r.input for r in runs] == [{"week": "W1"}, {"week": "W2"}]
    assert [r.origin["backfill_of"] for r in runs] == [old[0].id, old[1].id]
    assert runs[0].origin["source"] == "formal" and runs[0].origin["period_at"]

    again = (await client.post(f"/api/apps/{app_id}/views/latest/backfill")).json()
    assert (again["launched"], again["skipped"]) == (0, 3), "同一个输入不再补第二次"

    vm = (await client.get(f"/api/apps/{app_id}/pages/home")).json()
    assert vm["sources"]["latest"]["run_id"] == current.id, "补算的运行不是「最近一次」"
    trend = find(vm, "trend")["data"]
    assert trend["mode"] == "recompute"
    [segment] = trend["series"]
    assert segment["version"] == 2
    assert [p["run_id"] for p in segment["points"]] == [runs[0].id, runs[1].id, current.id], "按补的那一期排"
    assert [p.get("backfill", False) for p in segment["points"]] == [True, True, False]
    assert [p["value"] for p in segment["points"]] == [200, 200, 200]
    assert segment["points"][0]["as_of"].startswith(old[0].finished_at.isoformat()[:19])


async def test_backfill_needs_recompute_and_a_trend(client):
    wf, h1, h2 = await workflow()
    await formal(wf, 1, "W1")
    app_id = await upgraded_app(client, wf, h1, h2, "incomparable")
    refused = await client.post(f"/api/apps/{app_id}/views/latest/backfill")
    assert refused.status_code == 409 and refused.json()["code"] == "app_backfill_refused"
    assert "按新口径重算" in refused.json()["detail"]
    assert (await client.get(f"/api/apps/{app_id}")).json()["backfill"] == []
    gone = await client.get(f"/api/apps/{app_id}/views/nope/backfill")
    assert gone.status_code == 404 and gone.json()["code"] == "app_view_not_found"
    async with SessionLocal() as session:
        launched = (await session.execute(select(Run.id).where(Run.app_id == app_id))).scalars().all()
    assert launched == []


async def test_backfill_refuses_workflows_with_side_effects(client):
    """新版本里有人工节点：补算会对历史输入逐个给人开待办，收不回来。拒绝、详情里不列、一次也不发起。"""
    v2 = kpi(200)
    v2["nodes"].insert(3, node("ok", "human", "经理确认", mode="approve", title="确认周报"))
    v2["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(v2["nodes"], v2["nodes"][1:])]
    wf, h1, h2 = await workflow(v2)
    await formal(wf, 1, "W1")
    app_id = await upgraded_app(client, wf, h1, h2, "recompute")
    for res in (await client.get(f"/api/apps/{app_id}/views/latest/backfill"),
                await client.post(f"/api/apps/{app_id}/views/latest/backfill")):
        assert res.status_code == 409 and res.json()["code"] == "app_backfill_refused"
        assert "人工节点「经理确认」" in res.json()["detail"] and "不能按新口径补算" in res.json()["detail"]
    assert (await client.get(f"/api/apps/{app_id}")).json()["backfill"] == []
    async with SessionLocal() as session:
        launched = (await session.execute(select(Run.id).where(Run.app_id == app_id))).scalars().all()
    assert launched == []


async def test_backfill_writing_to_an_external_system_is_refused(client, monkeypatch):
    """调用工具节点绑的能力会写外部系统（capabilities.registry.writes_externally）：同样拒绝。"""
    from types import SimpleNamespace

    from app.capabilities import registry

    v2 = kpi(200)
    v2["nodes"].insert(3, node("pay", "tool", "付款", tool="erp_pay", args={}))
    v2["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(v2["nodes"], v2["nodes"][1:])]
    wf, h1, h2 = await workflow(v2)
    await formal(wf, 1, "W1")
    app_id = await upgraded_app(client, wf, h1, h2, "recompute")

    async def get_capability(session, name):
        return SimpleNamespace(name=name, effect="write", effect_system="write", endpoint_id=None) \
            if name == "erp_pay" else None

    monkeypatch.setattr(registry, "get_capability", get_capability)
    res = await client.post(f"/api/apps/{app_id}/views/latest/backfill")
    assert res.status_code == 409 and "会写外部系统（erp_pay）" in res.json()["detail"]


async def test_concurrent_backfills_launch_each_input_once(client):
    wf, h1, h2 = await workflow()
    for week in ("W1", "W2"):
        await formal(wf, 1, week)
    app_id = await upgraded_app(client, wf, h1, h2, "recompute")
    first, second = await asyncio.gather(client.post(f"/api/apps/{app_id}/views/latest/backfill"),
                                         client.post(f"/api/apps/{app_id}/views/latest/backfill"))
    outs = sorted((first.json(), second.json()), key=lambda o: -o["launched"])
    assert [(o["launched"], o["skipped"]) for o in outs] == [(2, 0), (0, 2)]
    for run_id in outs[0]["run_ids"]:
        await wait(run_id)
    async with SessionLocal() as session:
        launched = (await session.execute(select(Run.input).where(Run.app_id == app_id,
                                                                  Run.origin_kind == "app_backfill"))).scalars().all()
    assert sorted(i["week"] for i in launched) == ["W1", "W2"]
