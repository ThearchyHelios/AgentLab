"""蓝图的四个起点和权限（docs/design/normal-user-apps.md N1、§3.10，包 B2 的 api/blueprints.py）。

- conversation：空蓝图，另建一段蓝图对话（kind=blueprint）；
- template：按模板预置卡片（选模板走 bp_template）；不认识的模板 422；
- run：问数据的一轮回答，按「提取成工作流」同一个规则取出执行路径（api/copilot.extract_run_path），不建工作流，模板记
  「自由形」，问题记成「目的」的提议；没成功结束的运行 409；/copilot/from-run 照旧能提取成工作流；
- workflow_version：已发布的版本原样导入，蓝图挂在那个工作流上；没发布过的 409；
- 权限：开了登录，使用者不能建共享蓝图；开关关着不能建个人蓝图；开着能建，别人看不到（404）。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import Conversation, Setting, Workflow
from app.engine.runner import run_manager
from app.main import app
from tests import blueprint_samples as s
from tests.fakes import auth_env


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _succeeded_run(graph: dict, inputs: dict, *, actor: str = "analyst") -> str:
    run = await run_manager.start(graph=graph, input_payload=inputs, started_by=actor, run_class="exploratory",
                                  origin_kind="chat")
    row = await s.wait(run.id)
    assert row.status == "succeeded", row.error
    return run.id


async def _workflows() -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count(Workflow.id))) or 0)


async def test_conversation_start_opens_an_empty_blueprint_with_its_own_conversation(client):
    r = await client.post("/api/blueprints", json={"source": {"kind": "conversation"}, "scope": "shared",
                                                   "name": "门店周报"})
    assert r.status_code == 201, r.text
    bp = r.json()
    assert bp["scope"] == "shared" and bp["status"] == "drafting" and bp["name"] == "门店周报"
    assert bp["spec"]["format"] == "agentlab.blueprint/1" and bp["spec"]["template"] is None
    assert bp["source"] == {"kind": "conversation"} and bp["graph_hash"] is None
    async with SessionLocal() as session:
        conv = await session.get(Conversation, bp["conversation_id"])
    assert conv is not None and conv.kind == "blueprint" and "门店周报" in conv.title
    # 还没选场景：蓝图检查照实说，带白话
    missing = [i for i in bp["issues"] if i["code"] == "bp.missing" and i["path"] == "/template"]
    assert missing and missing[0]["level"] == "error"

    r = await client.get(f"/api/blueprints/{bp['id']}")
    assert r.status_code == 200 and r.json()["id"] == bp["id"]
    assert bp["id"] in {b["id"] for b in (await client.get("/api/blueprints")).json()["items"]}


async def test_template_start_presets_the_cards(client):
    r = await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "periodic_board"},
                                                   "scope": "shared"})
    assert r.status_code == 201, r.text
    bp = r.json()
    assert bp["spec"]["template"] == {"id": "periodic_board", "version": 1}
    assert bp["template_id"] == "periodic_board" and bp["template_version"] == 1
    assert bp["source"] == {"kind": "template", "id": "periodic_board"}
    # 周期看板预置了「多久更新」：模板默认、待确认
    assert bp["spec"]["cadence"]["source"] == "template" and bp["spec"]["cadence"]["value"]["preset"] == "weekly"

    r = await client.post("/api/blueprints", json={"source": {"kind": "template"}, "template": "no_such",
                                                   "scope": "shared"})
    assert r.status_code == 422 and r.json()["code"] == "blueprint_source_invalid"


async def test_run_start_extracts_the_path_without_making_a_workflow(client, tmp_path):
    source = await s.register_source(s.make_source(tmp_path))
    run_id = await _succeeded_run(s.board_graph(source), {**s.PERIOD, "question": "上周销售额多少"})
    before = await _workflows()
    r = await client.post("/api/blueprints", json={"source": {"kind": "run", "id": run_id}, "scope": "shared"})
    assert r.status_code == 201, r.text
    bp = r.json()
    assert bp["spec"]["template"] == {"id": "freeform", "version": 1} and bp["source"] == {"kind": "run", "id": run_id}
    assert bp["spec"]["purpose"]["value"] == "上周销售额多少" and bp["spec"]["purpose"]["status"] == "proposed"
    assert bp["graph_hash"] and bp["workflow_id"] is None
    # 图存在蓝图上，不建工作流（发布单保存版本时才建）
    assert await _workflows() == before
    row = await s.blueprint(bp["id"])
    assert {n["id"] for n in row.graph["nodes"]} == {"start", "q", "caliber", "done"}
    # 输入参数化：这次运行的输入变成入口字段的默认值（同 /copilot/from-run）
    fields = next(n for n in row.graph["nodes"] if n["id"] == "start")["data"]["config"]["fields"]
    assert {"name": "period_start", "default": s.PERIOD["period_start"]} in fields

    # 「提取成工作流」走的是同一个提取规则，照旧建工作流
    r = await client.post("/api/copilot/from-run", json={"run_id": run_id, "name": "提取"})
    assert r.status_code == 200, r.text
    assert r.json()["nodes"] == 4 and await _workflows() == before + 1

    # 没成功结束的运行提取不了
    failed = await run_manager.start(graph=s.chain(s.node("x", "tool", tool="no_such_tool")), input_payload={},
                                     started_by="analyst", run_class="exploratory", origin_kind="chat")
    await s.wait(failed.id)
    r = await client.post("/api/blueprints", json={"source": {"kind": "run", "id": failed.id}, "scope": "shared"})
    assert r.status_code in (400, 409) and "提取" in r.json()["detail"]


async def test_workflow_version_start_imports_the_published_graph(client, tmp_path):
    from app.core.artifact_store import graph_hash

    source = await s.register_source(s.make_source(tmp_path))
    graph = s.board_graph(source)
    wf_id = await s.published_workflow(graph, f"周报-{uuid.uuid4().hex[:4]}", description="每周看上周销售额")
    r = await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id, "version": 1},
                                                   "scope": "shared"})
    assert r.status_code == 201, r.text
    bp = r.json()
    assert bp["workflow_id"] == wf_id and bp["graph_hash"] == graph_hash(graph)
    assert bp["source"] == {"kind": "workflow_version", "id": wf_id, "version": 1}
    assert bp["spec"]["template"]["id"] == "freeform" and bp["spec"]["purpose"]["value"] == "每周看上周销售额"

    # 没发布过的版本不能当起点
    async with SessionLocal() as session:
        from app.db.models import WorkflowVersion

        wf = await session.get(Workflow, wf_id)
        wf.version = 2
        session.add(WorkflowVersion(workflow_id=wf_id, version=2, graph=graph, graph_hash=graph_hash(graph)))
        await session.commit()
    r = await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id, "version": 2},
                                                   "scope": "shared"})
    assert r.status_code == 409 and r.json()["code"] == "blueprint_source_invalid"


@pytest.fixture
async def login():
    await auth_env.reset()
    try:
        yield
    finally:
        await auth_env.reset()
        async with SessionLocal() as session:
            row = await session.get(Setting, "apps")
            if row is not None:
                await session.delete(row)
                await session.commit()


async def _switch(on: bool) -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, "apps")
        if row is None:
            session.add(Setting(key="apps", value={"user_personal_apps": on}))
        else:
            row.value = {**(row.value or {}), "user_personal_apps": on}
        await session.commit()


async def test_who_can_start_what(login):
    await auth_env.turn_on_local()
    u = uuid.uuid4().hex[:6]
    await auth_env.make_user(f"lee{u}", "user")
    await auth_env.make_user(f"ma{u}", "user")
    await auth_env.make_user(f"zhao{u}", "builder")
    user = await auth_env.login(f"lee{u}")
    conversation = {"source": {"kind": "conversation"}}

    # 使用者不能建共享蓝图；开关关着也不能建个人蓝图
    r = await user.post("/api/blueprints", json={**conversation, "scope": "shared"})
    assert r.status_code == 403
    r = await user.post("/api/blueprints", json={**conversation, "scope": "personal"})
    assert r.status_code == 403 and r.json()["code"] == "personal.off"

    # 开关开着：使用者能建个人蓝图，拥有者是他自己；别人当它不存在
    await _switch(True)
    r = await user.post("/api/blueprints", json={**conversation, "scope": "personal"})
    assert r.status_code == 201, r.text
    mine = r.json()
    assert mine["owner"] == f"lee{u}" and mine["scope"] == "personal"
    assert (await user.get(f"/api/blueprints/{mine['id']}")).status_code == 200
    other = await auth_env.login(f"ma{u}")
    assert (await other.get(f"/api/blueprints/{mine['id']}")).status_code == 404
    assert mine["id"] not in {b["id"] for b in (await other.get("/api/blueprints")).json()["items"]}
    # 使用者不能从已发布的工作流开始（工作流是搭建者的东西）
    r = await user.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": "x"},
                                                 "scope": "personal"})
    assert r.status_code == 403

    # 搭建者能建共享蓝图，看得到共享蓝图，看不到别人的个人蓝图
    builder = await auth_env.login(f"zhao{u}")
    r = await builder.post("/api/blueprints", json={**conversation, "scope": "shared"})
    assert r.status_code == 201, r.text
    listed = {b["id"] for b in (await builder.get("/api/blueprints")).json()["items"]}
    assert r.json()["id"] in listed and mine["id"] not in listed
    # 使用者看不到共享蓝图
    # 使用者碰共享蓝图回 404，和看不到的一样，试不出它在不在（D5）
    assert (await user.get(f"/api/blueprints/{r.json()['id']}")).status_code == 404


async def test_patch_applies_card_ops_as_the_user(client):
    bp = (await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "periodic_board"},
                                                     "scope": "shared"})).json()
    r = await client.patch(f"/api/blueprints/{bp['id']}", json={
        "name": "门店周报", "ops": [{"op": "bp_set", "path": "/purpose", "value": "每周一看上周各门店的销售额"}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "门店周报"
    assert out["spec"]["purpose"] == {"value": "每周一看上周各门店的销售额", "source": "user", "status": "confirmed",
                                      "why": None}
    # 越界的操作整批不落，照实说
    r = await client.patch(f"/api/blueprints/{bp['id']}", json={
        "ops": [{"op": "bp_set", "path": "/purpose", "value": "改了"}, {"op": "bp_set", "path": "/nope", "value": 1}]})
    assert r.status_code == 422 and r.json()["code"].startswith("bp.")
    assert (await client.get(f"/api/blueprints/{bp['id']}")).json()["spec"]["purpose"]["value"] == "每周一看上周各门店的销售额"


async def test_renaming_a_blueprint_leaves_a_trace(client):
    """改名也留痕（P2）：名字进工作流、应用的名字和口径卡的标题，谁什么时候从什么改成什么都记下；名字没变不记。"""
    from app.db.models_blueprints import BlueprintEvent

    bp = (await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "periodic_board"},
                                                     "scope": "shared", "name": "门店周报"})).json()
    who = {"X-Actor": "builder1"}
    r = await client.patch(f"/api/blueprints/{bp['id']}", json={"name": "门店周报（新）"}, headers=who)
    assert r.status_code == 200 and r.json()["name"] == "门店周报（新）"
    assert (await client.patch(f"/api/blueprints/{bp['id']}", json={"name": "门店周报（新）"},
                               headers=who)).status_code == 200
    async with SessionLocal() as session:
        events = list((await session.execute(select(BlueprintEvent).where(
            BlueprintEvent.blueprint_id == bp["id"], BlueprintEvent.op == "rename"))).scalars())
    assert len(events) == 1 and (events[0].data["before"], events[0].data["after"]) == ("门店周报", "门店周报（新）")
    assert events[0].actor == "builder1"
