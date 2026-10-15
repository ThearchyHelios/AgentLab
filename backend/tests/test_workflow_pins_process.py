"""子工作流固定版本与流程检查（D-q），删除工作流时的生效记录（D-n 的删工作流部分）。

- 正式运行发起时，图里每个固定了版本的子工作流节点，那一版必须「已发布且未退役」：草稿快照、不存在的拒
  （subgraph_version_unpublished），退役的拒（subgraph_version_retired）；
- 发布、发布前检查、应用检查都先读好子工作流（lint_process.load_subgraphs / with_subgraphs），子工作流里的人工节点
  真的报出 process.human_in_subgraph（应用按流程钉住时升成 app.process_unsafe）；
- 发布、发布前检查、应用检查和发起用同一条规则（apps/versions.subgraph_pin_problems）：子工作流钉在草稿或退役版本上的
  工作流发布不出去，钉住它的应用也报错，机器码和发起时一样；
- 删工作流：任何版本（含已退役的）有生效记录都拒删，演练不算。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import ActionRecord, Run, Workflow, WorkflowVersion
from app.engine.launch import LaunchRefused, launch_run
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


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


SIMPLE = chain(node("start", "input", "开始"), node("out", "output", "成果", fields=[{"name": "r", "value": "完成"}]))
WITH_HUMAN = chain(node("start", "input", "开始"), node("ask", "human", "经理审批", mode="approve", title="批一下"),
                   node("out", "output", "成果", fields=[{"name": "r", "value": "完成"}]))


async def workflow(versions: dict[int, dict], *, current: int | None, graph: dict | None = None) -> str:
    """versions：{版本号: {graph, level, retired}}。"""
    async with SessionLocal() as session:
        wf = Workflow(name=f"子流程-{uuid.uuid4().hex[:6]}", graph=graph or SIMPLE, published_version=current,
                      status="published" if current else "draft", version=max(versions))
        session.add(wf)
        await session.flush()
        for number, spec in versions.items():
            g = spec.get("graph") or SIMPLE
            session.add(WorkflowVersion(workflow_id=wf.id, version=number, graph=g, graph_hash=graph_hash(g),
                                        level=spec.get("level"), published_at=utcnow() if spec.get("level") else None,
                                        retired_at=utcnow() if spec.get("retired") else None))
        await session.commit()
        return wf.id


def outer(inner: str, version: int | None) -> dict:
    # 固定旧版本要声明上游升版时的处置（口径升版是必须处置的事件，另有检查）
    sub = {"workflow_id": inner, "upgrade_policy": "incomparable",
           **({"workflow_version": version} if version is not None else {})}
    return chain(node("start", "input", "开始"), node("sub", "subgraph", "子流程", **sub),
                 node("out", "output", "成果", fields=[{"name": "r", "value": "完成"}]))


async def launch(outer_id: str):
    async with SessionLocal() as session:
        return await launch_run(session, workflow_id=outer_id, run_class="formal", actor="测试")


async def test_formal_runs_refuse_subgraph_pins_that_are_not_in_use():
    inner = await workflow({1: {}, 2: {"level": "published"}, 3: {"level": "published", "retired": True},
                            4: {"level": "published"}}, current=4)
    for version, code in ((1, "subgraph_version_unpublished"), (3, "subgraph_version_retired"),
                          (9, "subgraph_version_unpublished")):
        g = outer(inner, version)
        wf = await workflow({1: {"graph": g, "level": "published"}}, current=1, graph=g)
        with pytest.raises(LaunchRefused) as caught:
            await launch(wf)
        assert caught.value.code == code and caught.value.status_code == 409, version
        assert "子工作流「子流程」" in caught.value.message
    # 已发布、没退役的旧版（不是当前发布版）可以；没固定版本的跟随最新，这里不拦
    for version in (2, None):
        g = outer(inner, version)
        wf = await workflow({1: {"graph": g, "level": "published"}}, current=1, graph=g)
        run = await launch(wf)
        assert run.run_class == "formal"
    # 探索运行不查
    async with SessionLocal() as session:
        run = await launch_run(session, graph=outer(inner, 1), actor="测试")
    assert run.run_class == "exploratory"


async def test_publish_and_app_lint_refuse_the_same_subgraph_pins(client):
    """发布门禁和发起同一条规则：钉在草稿、退役版本上的子工作流各级都是错误，发布前检查列出来、发布被拦；钉在已发布
    版本上的照常发布。应用钉住的版本里子工作流固定的版本后来退役了，应用检查同样报错。"""
    inner = await workflow({1: {}, 2: {"level": "published"}, 3: {"level": "published", "retired": True}}, current=2)
    for version, code in ((1, "subgraph_version_unpublished"), (3, "subgraph_version_retired")):
        g = outer(inner, version)
        wf = await workflow({1: {"graph": g}}, current=None, graph=g)
        check = (await client.post(f"/api/workflows/{wf}/publish-check", json={})).json()
        hit = [i for i in check["issues"] if i.get("code") == code]
        assert not check["ok"] and hit and hit[0]["level"] == "error" and hit[0]["node_id"] == "sub"
        assert "子工作流「子流程」" in hit[0]["message"]
        published = (await client.post(f"/api/workflows/{wf}/publish", json={"version": 1})).json()
        assert not published["ok"] and code in {i.get("code") for i in published["issues"]}
    g = outer(inner, 2)
    wf = await workflow({1: {"graph": g}}, current=None, graph=g)
    assert (await client.post(f"/api/workflows/{wf}/publish", json={"version": 1})).json()["ok"]

    # 发布之后子工作流的 v2 退役了：钉住外层的应用要报错（正式运行发起时会被拒）
    async with SessionLocal() as session:
        row = (await session.execute(WorkflowVersion.__table__.select().where(
            WorkflowVersion.workflow_id == inner, WorkflowVersion.version == 2))).first()
        await session.execute(WorkflowVersion.__table__.update().where(WorkflowVersion.id == row.id)
                              .values(retired_at=utcnow()))
        await session.commit()
    spec = {"format": "agentlab.app/1", "name": "报表",
            "pins": [{"alias": "rep", "workflow_id": wf, "version": 1, "graph_hash": graph_hash(g), "use": "entry"}],
            "pages": [{"id": "home", "title": "首页", "body": []}]}
    app_id = (await client.post("/api/apps", json={"name": "报表", "spec": spec})).json()["id"]
    issues = (await client.post(f"/api/apps/{app_id}/lint")).json()["issues"]
    hit = [i for i in issues if i["code"] == "subgraph_version_retired"]
    assert hit and hit[0]["level"] == "error" and hit[0]["path"] == "pins[0]"


async def test_human_in_subgraph_is_really_checked_on_publish_and_in_apps(client):
    inner = await workflow({1: {"graph": WITH_HUMAN, "level": "published"}}, current=1, graph=WITH_HUMAN)
    g = outer(inner, 1)
    wf = await workflow({1: {"graph": g}}, current=None, graph=g)

    check = (await client.post(f"/api/workflows/{wf}/publish-check", json={})).json()
    hit = [i for i in check["issues"] if i.get("code") == "process.human_in_subgraph"]
    assert hit and hit[0]["level"] == "warning" and "经理审批" in hit[0]["message"]
    published = (await client.post(f"/api/workflows/{wf}/publish", json={"version": 1})).json()
    assert published["ok"] and "process.human_in_subgraph" in {i.get("code") for i in published["issues"]}

    spec = {"format": "agentlab.app/1", "name": "流程",
            "pins": [{"alias": "proc", "workflow_id": wf, "version": 1, "graph_hash": graph_hash(g), "use": "process"}],
            "pages": [{"id": "home", "title": "首页", "body": []}]}
    app_id = (await client.post("/api/apps", json={"name": "流程", "spec": spec})).json()["id"]
    issues = (await client.post(f"/api/apps/{app_id}/lint")).json()["issues"]
    unsafe = [i for i in issues if i["code"] == "app.process_unsafe"]
    assert unsafe and unsafe[0]["level"] == "error" and "子工作流" in unsafe[0]["message"]


async def test_effects_on_a_retired_version_block_deleting_the_workflow(client):
    wf = await workflow({1: {"level": "published", "retired": True}, 2: {"level": "published"}}, current=2)
    async with SessionLocal() as session:
        run = Run(workflow_name="付款", run_class="formal", status="succeeded")
        session.add(run)
        await session.flush()
        key = uuid.uuid4().hex
        rec = ActionRecord(action_key=key, idem_key=f"{key}:1", run_id=run.id, node_id="pay", capability="erp_pay",
                           payload_hash="h", state="confirmed", workflow_id=wf, workflow_version=1)
        session.add(rec)
        await session.commit()
        rec_id = rec.id
    refused = await client.delete(f"/api/workflows/{wf}")
    assert refused.status_code == 409 and refused.json()["code"] == "workflow_in_use"
    assert "v1（已退役） 有 1 条生效记录" in refused.json()["detail"]
    async with SessionLocal() as session:
        (await session.get(ActionRecord, rec_id)).state = "simulated"
        await session.commit()
    assert (await client.delete(f"/api/workflows/{wf}")).status_code == 204, "演练不算生效"
