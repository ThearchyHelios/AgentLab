"""开登录后 user 角色能读什么（§3.12「运行、证据、工件（只读）」那一行，A7 第 6 条）。

- 能读：自己发起的运行；派给过自己（或自己所在的组）的待办所在的实例；有授权的应用的视图运行；有授权的应用作为视图
  固定的工作流版本的正式运行（视图读的是定时任务那次运行，它不带应用 id）；
- 不能读：别人的运行、没授权的应用；记录页的列表；
- 工件：按白名单给——查询快照按派生遮罩给（数据源设了遮罩的列、快照记着的遮罩列）、指标集按证据面板的判断遮、动作
  回执按能力的遮罩字段遮、报告文档照给；工具快照、节点产出、口径卡取值的原值这些一律不给；
- 运行接口（/api/runs）只在开了问数据时给，只给自己的问数据运行的记录和事件（发起时查询结果就在工具边界遮了）；自己
  发起的应用实例、有授权的运行经应用页和证据接口读；使用者只能发起问数据的探索运行；
- 问数据默认不给 user（Q2），管理员开了 user_can_chat 才给；
- 应用授权给 user 的 manage 按 observe 算：发布、钩子这些都不给；生效记录的列表不给。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.api.deps import Principal, can_read_artifact, can_read_run
from app.core.artifact_store import put_json
from app.db.base import SessionLocal
from app.db.models import (
    App, AppGrant, AppVersion, DataSource, Group, GroupMember, Run, Task, TaskAssignment, Workflow,
)
from tests.fakes import auth_env


@pytest.fixture
async def world():
    """开账号密码登录；builder「bob」、user「uma」（在组 fin 里）；几次运行和一个授权给 fin 的应用。"""
    await auth_env.reset()
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("bob", "builder")
    uma_row = await auth_env.make_user("uma", "user")
    async with SessionLocal() as s:
        fin = Group(name="fin", label="财务", source="local")
        s.add(fin)
        await s.flush()
        s.add(GroupMember(group_id=fin.id, user_id=uma_row.id))
        app_a = App(name="报销", published_version=1)
        app_b = App(name="周报", published_version=1)
        s.add_all([app_a, app_b])
        await s.flush()
        s.add(AppGrant(app_id=app_a.id, principal_kind="group", principal="fin", role="use"))
        s.add(AppGrant(app_id=app_b.id, principal_kind="everyone", principal="", role="use"))
        s.add(AppVersion(app_id=app_b.id, version=1, spec={}, spec_hash="h",
                         pins=[{"alias": "w", "workflow_id": "wf-weekly", "version": 3, "use": "view"}]))
        runs = {
            "others": Run(workflow_name="别人的", started_by="bob", run_class="exploratory"),
            # 开登录后使用者发起的问数据运行：查询结果在工具边界遮（api/runs.py 发起时就定下）
            "own": Run(workflow_name="自己的", started_by="uma", run_class="exploratory", origin_kind="chat",
                       options={"mask_results": True}),
            # 关登录时自报同名留下的老问数据运行：没在边界遮过，开了登录也不给原始事件
            "own_unmasked": Run(workflow_name="自己的老运行", started_by="uma", run_class="exploratory",
                                origin_kind="chat"),
            "own_instance": Run(workflow_name="自己发起的实例", run_class="formal", app_id=app_a.id,
                                origin_kind="app_entry", started_by="uma"),
            "view": Run(workflow_name="视图", run_class="formal", app_id=app_a.id, origin_kind="app_view"),
            "instance": Run(workflow_name="实例", run_class="formal", app_id=app_a.id, origin_kind="app_entry",
                            started_by="bob"),
            "schedule": Run(workflow_name="定时", run_class="formal", workflow_id="wf-weekly", version=3),
            "ungranted": Run(workflow_name="没授权", run_class="formal", app_id="nope", origin_kind="app_view"),
        }
        # 定时任务那次运行：工作流版本被应用 B 作为视图固定着
        s.add(Workflow(id="wf-weekly", name="周报"))
        await s.flush()
        s.add_all(runs.values())
        await s.flush()
        task = Task(run_id=runs["instance"].id, kind="approve")
        s.add(task)
        await s.flush()
        s.add(TaskAssignment(task_id=task.id, principal_kind="group", principal="fin", source="slot:finance"))
        s.add(DataSource(name="crm", kind="sqlite", options={"mask_columns": ["phone"]}))
        await s.commit()
        ids = {k: r.id for k, r in runs.items()}
    uma = await auth_env.login("uma")
    bob = await auth_env.login("bob")
    try:
        yield {"admin": admin, "uma": uma, "bob": bob, "runs": ids}
    finally:
        async with SessionLocal() as s:
            for model in (TaskAssignment, Task, AppGrant, AppVersion, App):
                await s.execute(delete(model))
            await s.execute(delete(Run).where(Run.id.in_(list(ids.values()))))
            await s.execute(delete(Workflow).where(Workflow.id == "wf-weekly"))
            await s.execute(delete(DataSource).where(DataSource.name == "crm"))
            await s.commit()
        await auth_env.reset()


def _uma() -> Principal:
    return Principal(username="uma", role="user", groups=("fin",), verified=True, mode="local")


async def test_which_runs_a_user_can_read(world):
    runs = world["runs"]
    assert await can_read_run(_uma(), runs["own"])
    assert await can_read_run(_uma(), runs["view"]), "有授权的应用视图运行"
    assert await can_read_run(_uma(), runs["instance"]), "派给自己所在的组的待办所在的实例"
    assert await can_read_run(_uma(), runs["schedule"]), "应用作为视图固定的版本的正式运行"
    assert not await can_read_run(_uma(), runs["others"])
    assert not await can_read_run(_uma(), runs["ungranted"])
    builder = Principal(username="bob", role="builder", verified=True, mode="local")
    assert await can_read_run(builder, runs["others"])


async def test_run_routes_for_a_user(world):
    uma, runs = world["uma"], world["runs"]
    assert (await uma.get(f"/api/runs/{runs['own']}")).status_code == 403, "没开问数据时运行接口一律不给"
    assert (await world["admin"].put("/api/auth/settings", json={"user_can_chat": True})).status_code == 200
    assert (await uma.get("/api/runs")).status_code == 403, "记录页的列表只给搭建者"
    assert (await uma.get(f"/api/runs/{runs['own']}")).status_code == 200
    assert (await uma.get(f"/api/runs/{runs['own']}/events")).status_code == 200
    assert (await uma.get(f"/api/runs/{runs['own_unmasked']}/events")).status_code == 403, \
        "查询结果没在工具边界遮过的运行，事件里是原文"
    assert (await uma.post(f"/api/runs/{runs['own_unmasked']}/cancel")).status_code != 403, "停止自己的问数据运行照旧可以"
    assert (await uma.get(f"/api/runs/{runs['own']}/state")).status_code == 403, "运行状态是查询结果的原文"
    assert (await uma.get(f"/api/runs/{runs['own']}/artifacts")).status_code == 403
    assert (await uma.delete(f"/api/runs/{runs['own']}")).status_code == 403
    assert (await uma.get(f"/api/runs/{runs['own_instance']}/events")).status_code == 403, \
        "自己发起的应用实例经应用页和证据接口读，不给原始事件"
    assert (await uma.get(f"/api/runs/{runs['own_instance']}")).status_code == 403
    assert (await uma.get(f"/api/runs/{runs['view']}")).status_code == 403, "有授权的运行经证据接口读，不给原始记录"
    assert (await uma.get(f"/api/runs/{runs['view']}/events")).status_code == 403
    assert (await uma.get(f"/api/runs/{runs['view']}/evidence")).status_code != 403
    assert (await uma.get(f"/api/runs/{runs['others']}/evidence")).status_code == 403
    assert (await uma.post(f"/api/runs/{runs['view']}/evidence/judge", json={})).status_code == 403
    assert (await uma.get(f"/api/runs/{runs['view']}/compare")).status_code == 403
    assert (await world["bob"].get(f"/api/runs/{runs['others']}/events")).status_code == 200


async def test_artifacts_are_masked_for_a_user(world):
    uma, runs = world["uma"], world["runs"]
    snap = {"source": "crm", "columns": ["name", "phone"], "rows": [["张三", "13900000001"]],
            "sql": "SELECT name, phone FROM customers", "truncated": False}
    query_id = await put_json(snap, kind="query_snapshot", run_id=runs["view"], node_id="q")
    raw_id = await put_json({"tool": "db_query__crm", "args": {}, "result": snap}, kind="tool_snapshot",
                            run_id=runs["own"], node_id="q")
    hidden_id = await put_json({**snap, "rows": [["李四", "13900000002"]]}, kind="query_snapshot",
                               run_id=runs["others"], node_id="q")

    got = (await uma.get(f"/api/artifacts/{query_id}")).json()["content"]
    assert got["rows"][0][1] != "13900000001" and got["rows"][0][0] == "张三", "数据源设了遮罩的列按派生遮罩换掉"
    assert (await uma.get(f"/api/artifacts/{raw_id}")).status_code == 403, "工具快照的原文不给，自己的运行也一样"
    assert (await uma.get(f"/api/artifacts/{hidden_id}")).status_code == 403
    assert await can_read_artifact(_uma(), query_id) and not await can_read_artifact(_uma(), hidden_id)

    raw = (await world["bob"].get(f"/api/artifacts/{query_id}")).json()["content"]
    assert raw["rows"][0][1] == "13900000001", "搭建者照旧拿原文"

    # 白名单以外的种类一律不给：节点的整份产出里有遮罩列的查询行，口径卡取值的完整原值同理
    output_id = await put_json({"q": snap}, kind="node_output", run_id=runs["view"], node_id="q")
    input_id = await put_json({"path": "vars.res", "value": snap["rows"]}, kind="metric_input",
                              run_id=runs["view"], node_id="card")
    assert (await uma.get(f"/api/artifacts/{output_id}")).status_code == 403
    assert (await uma.get(f"/api/artifacts/{input_id}")).status_code == 403
    assert (await world["bob"].get(f"/api/artifacts/{output_id}")).status_code == 200

    # 指标集照给（没有取自遮罩列的指标，值照旧）；报告文档照给
    card = {"kind": "metric_set", "metrics": [{"id": "m1", "name": "人数", "value": 3, "rendered": "3",
                                               "inputs": [{"path": "input.n", "value": 3}], "substituted": "3"}]}
    card_id = await put_json(card, kind="metric_set", run_id=runs["view"], node_id="card")
    assert (await uma.get(f"/api/artifacts/{card_id}")).json()["content"]["metrics"][0]["value"] == 3
    doc_id = await put_json({"segments": []}, kind="report_doc", run_id=runs["view"], node_id="r")
    assert (await uma.get(f"/api/artifacts/{doc_id}")).status_code == 200


async def test_action_receipt_body_is_masked_for_a_user(world):
    from app.db.models import ActionRecord, Capability

    runs = world["runs"]
    async with SessionLocal() as s:
        s.add(Capability(name="erp_pay", masked=["/card_no"]))
        s.add(ActionRecord(action_key="k" * 64, idem_key="k:1", run_id=runs["view"], node_id="pay",
                           capability="erp_pay", payload_hash="h", state="confirmed"))
        await s.commit()
    try:
        receipt = await put_json({"status": 200, "body": {"card_no": "6222000011112222", "id": "PAY-1"}},
                                 kind="action_receipt", run_id=runs["view"], node_id="pay")
        body = (await world["uma"].get(f"/api/artifacts/{receipt}")).json()["content"]["body"]
        assert body["id"] == "PAY-1" and body["card_no"] != "6222000011112222"
        raw = (await world["bob"].get(f"/api/artifacts/{receipt}")).json()["content"]["body"]
        assert raw["card_no"] == "6222000011112222"
        assert (await world["uma"].get("/api/actions")).status_code == 403, "生效记录的列表只给搭建者"
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(ActionRecord).where(ActionRecord.run_id == runs["view"]))
            await s.execute(delete(Capability).where(Capability.name == "erp_pay"))
            await s.commit()


async def test_a_manage_grant_does_not_make_a_user_an_app_builder(world):
    """§3.12：搭建、发布、绑定岗位、授权、钩子 user 一律不给。授给使用者的 manage 按 observe 算。"""
    uma = world["uma"]
    async with SessionLocal() as s:
        app_id = (await s.execute(select(App.id).where(App.name == "报销"))).scalar_one()
        s.add(AppGrant(app_id=app_id, principal_kind="user", principal="uma", role="manage"))
        await s.commit()
    assert (await uma.post(f"/api/apps/{app_id}/publish", json={})).status_code == 403
    assert (await uma.post(f"/api/apps/{app_id}/hooks/x/rotate")).status_code == 403
    assert (await uma.put(f"/api/apps/{app_id}/grants", json={"items": []})).status_code == 403


async def test_chat_is_off_for_users_until_an_admin_allows_it(world):
    uma, admin = world["uma"], world["admin"]
    assert (await uma.post("/api/copilot/generate-stream", json={})).status_code == 403
    assert (await uma.get("/api/conversations")).status_code == 403
    assert (await uma.post("/api/runs", json={})).status_code == 403
    r = await admin.put("/api/auth/settings", json={"user_can_chat": True})
    assert r.status_code == 200
    assert (await uma.get("/api/conversations")).status_code == 200
    assert (await uma.post("/api/copilot/generate-stream", json={})).status_code != 403
    assert (await uma.put("/api/copilot/model", json={})).status_code == 403, "开了问数据也不能改助手模型"
    assert (await uma.post("/api/runs", json={"origin": "chat"})).status_code != 403
    # 使用者只能发起问数据的探索运行：已发布工作流的正式运行、画布试跑、不说来源的都不给
    assert (await uma.post("/api/runs", json={})).status_code == 403
    assert (await uma.post("/api/runs", json={"origin": "chat", "workflow_id": "wf-weekly",
                                              "run_class": "formal"})).status_code == 403
    assert (await uma.post("/api/runs", json={"origin": "canvas", "graph": {}})).status_code == 403


async def test_a_users_chat_run_masks_query_results_at_the_tool_boundary(world, monkeypatch):
    """开登录后使用者发起的问数据运行：发起时就定下查询结果在工具边界遮、数据源查询一律只读（run options），数据源工具
    按它们建。"""
    from types import SimpleNamespace

    from app.api import runs as runs_api
    from app.engine.context import RunContext
    from app.engine.schema import GraphSpec
    from app.tools import datasource, registry
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION, run_masks_results, run_reads_only

    seen: list[dict] = []

    async def launch_run(session, **kw):
        seen.append(kw)
        return SimpleNamespace(id="r1", workflow_id=None, workflow_name="x", status="queued", input={}, output={},
                               error=None, usage={})

    monkeypatch.setattr(runs_api, "launch_run", launch_run)
    assert (await world["admin"].put("/api/auth/settings", json={"user_can_chat": True})).status_code == 200
    assert (await world["uma"].post("/api/runs", json={"origin": "chat", "graph": {}})).status_code == 201
    assert (await world["bob"].post("/api/runs", json={"origin": "chat", "graph": {}})).status_code == 201
    assert seen[0]["options"] == {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}
    assert seen[1]["options"] is None, "搭建者照旧原样"

    spec = GraphSpec.model_validate({"nodes": [], "edges": []})
    ctx = RunContext(run_id="r1", thread_id="r1", spec=spec,
                     extra={"run_options": {MASK_RESULTS_OPTION: True, READ_ONLY_OPTION: True}})
    plain = RunContext(run_id="r2", thread_id="r2", spec=spec)
    assert run_masks_results(ctx) and not run_masks_results(plain)
    assert run_reads_only(ctx) and not run_reads_only(plain)
    built: list[tuple[bool, bool]] = []

    async def build_datasource_tools(names, ctx, session, *, force_readonly=False, mask_results=False):
        built.append((force_readonly, mask_results))
        return []

    monkeypatch.setattr(datasource, "build_datasource_tools", build_datasource_tools)
    await registry.build_tools(["db_query__crm"], registry.ToolContext(run_id="r1", mask_results=True,
                                                                         force_readonly=True), session=object())
    assert built == [(True, True)]


async def test_admin_only_areas(world):
    bob = world["bob"]
    assert (await bob.get("/api/capabilities")).status_code == 200, "搭建者能看能力登记"
    assert (await bob.post("/api/endpoints", json={"name": "erp", "environments": {
        "prod": {"base_url": "https://erp.example.com"}}})).status_code == 403
    assert (await bob.post("/api/mcp/servers", json={"name": "fs", "transport": "stdio", "command": "x"})
            ).status_code == 403, "MCP 的 stdio 只有管理员能建"
    assert (await bob.get("/api/settings")).status_code == 200
    assert (await bob.put("/api/settings", json={})).status_code == 403, "模型接入、运行默认值搭建者只读"
    assert (await bob.get("/api/audit")).status_code == 403


async def test_governed_publish_needs_admin(world):
    from fastapi import HTTPException

    from app.api.deps import ensure_can_publish, reset_principal, set_principal

    token = set_principal(Principal(username="bob", role="builder", verified=True, mode="local"))
    try:
        ensure_can_publish("published")
        with pytest.raises(HTTPException) as e:
            ensure_can_publish("governed")
        assert e.value.status_code == 403
    finally:
        reset_principal(token)
