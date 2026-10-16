"""一次确认发布：发布单（docs/design/normal-user-apps.md N5、§3.6，包 B2 的 blueprints/publish_plan.py）。

- 顺利走完：出单只读（什么都没建）、九步依次做完——存版本（工作流记下出自哪个蓝图）、预检、发布工作流、建定时任务
  （先停用）、建应用（钉住项换成发布的那一版、视图绑定时任务）、发布应用、启用定时任务、授权、跑第一次（按定时任务触发），
  每步留痕、审计写 publish_plan.*，蓝图记「已发布」；
- 预检挡住时什么都没写：连工作流版本都不存，没有定时任务、没有应用；
- 流程类岗位没绑人：预检报 app.slot_unbound，白话写「你来定」；
- 中途失败后接着做：停在那一步、写明原因（工作流已发布，应用还没有）；接着做从那一步开始，已完成的按 id 核对、不重做；
  前面建的东西不见了，接着做停下、不另建一份；
- 受管级要管理员：搭建者出单时问题里写「要找管理员」，执行 403；
- 图变了拒绝执行（plan.graph_changed）；发布的图和最近一次试运行的不同，照实提醒（plan.trial_differs，不拦）。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import AppGrant, AuditEvent, Run, Schedule, Workflow, WorkflowVersion
from app.db.models_apps import App, AppVersion
from app.db.models_blueprints import Blueprint
from app.engine.runner import run_manager
from app.main import app
from tests import blueprint_samples as s
from tests.fakes import auth_env

ACTOR = {"X-Actor": "builder1"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=ACTOR) as c:
        yield c


async def _count(model) -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _board_from_run(client, tmp_path, *, access: dict | None = None, cadence: dict | None = None,
                          trial: bool = True) -> dict:
    """问数据的一轮回答（看板图）→ 蓝图（自由形）→ 卡片填好 →（试运行）。"""
    source = await s.register_source(s.make_source(tmp_path))
    run = await run_manager.start(graph=s.board_graph(source), input_payload=s.PERIOD, started_by="builder1",
                                  run_class="exploratory", origin_kind="chat")
    assert (await s.wait(run.id)).status == "succeeded"
    r = await client.post("/api/blueprints", json={"source": {"kind": "run", "id": run.id}, "scope": "shared",
                                                   "name": f"门店周报-{uuid.uuid4().hex[:4]}"})
    assert r.status_code == 201, r.text
    bp = r.json()
    await s.edit_spec(bp["id"], purpose=s.item("每周一看上周销售额"), cadence=s.item(cadence or s.WEEKLY),
                      access=access or {"use": [{"kind": "user", "name": "zhang"}, {"kind": "group", "name": "门店"}],
                                        "observe": [], "manage": []})
    if trial:
        r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": s.PERIOD})
        assert r.status_code == 200, r.text
        assert (await s.wait(r.json()["run_id"])).status == "succeeded"
    else:
        r = await client.post(f"/api/blueprints/{bp['id']}/instantiate")
        assert r.status_code == 200, r.text
    return (await client.get(f"/api/blueprints/{bp['id']}")).json()


def _steps(plan: dict) -> dict:
    return {st["kind"]: st for st in plan["steps"]}


async def test_a_plan_runs_all_nine_steps(client, tmp_path):
    bp = await _board_from_run(client, tmp_path)
    workflows, schedules, apps = await _count(Workflow), await _count(Schedule), await _count(App)

    r = await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["ok"] is True and plan["status"] == "planned", plan["problems"]
    assert [st["kind"] for st in plan["steps"]] == ["save_version", "preflight", "publish_workflow", "create_schedule",
                                                     "create_app", "publish_app", "enable_schedule", "grant",
                                                     "first_run"]
    assert all(st["status"] == "pending" for st in plan["steps"])
    assert plan["options"]["first_run"] is True and plan["graph_hash"] == bp["graph_hash"]
    summary = {x["step"]: x["text"] for x in plan["summary"]}
    assert "每周一 09:00" in summary["create_schedule"] and "zhang" in summary["grant"] and "门店（组）" in summary["grant"]
    assert not [p for p in plan["problems"] if p["level"] == "error"]
    # 出单只读：什么都没建
    assert (await _count(Workflow), await _count(Schedule), await _count(App)) == (workflows, schedules, apps)

    r = await client.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "done", done["steps"]
    steps = _steps(done)
    assert all(st["status"] == "done" for st in steps.values())

    row = await s.blueprint(bp["id"])
    assert row.status == "published" and row.workflow_id and row.app_id
    async with SessionLocal() as session:
        wf = await session.get(Workflow, row.workflow_id)
        version = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == wf.id))).scalars().one()
        schedule = await session.get(Schedule, steps["create_schedule"]["result"]["schedule_id"])
        application = await session.get(App, row.app_id)
        published = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == application.id))).scalars().one()
        grants = {(g.principal_kind, g.principal, g.role) for g in (await session.execute(
            select(AppGrant).where(AppGrant.app_id == application.id))).scalars()}
        audits = [a.action for a in (await session.execute(select(AuditEvent).where(
            AuditEvent.subject_id == plan["id"]).order_by(AuditEvent.at))).scalars()]
    # 存版本：工作流记下出自哪个蓝图、哪个模板、哪次运行；发布的就是蓝图的那张图
    assert wf.origin["blueprint_id"] == bp["id"] and wf.origin["template"] == "freeform@1" and wf.origin["run_id"]
    assert wf.published_version == 1 and version.level == "published" and version.graph_hash == bp["graph_hash"]
    # 定时任务：正式运行、跑这个工作流，应用发布后启用
    assert schedule.workflow_id == wf.id and schedule.run_class == "formal" and schedule.enabled is True
    assert set(schedule.input_template) == {"period", "period_start", "period_end"}
    # 应用：出自这个蓝图，引用发布的那一版，看板视图只看定时任务出的数
    assert application.blueprint_id == bp["id"] and application.published_version == 1
    pin = published.pins[0]
    assert (pin["workflow_id"], pin["version"], pin["graph_hash"]) == (wf.id, 1, bp["graph_hash"])
    assert published.spec["views"]["latest"]["source"]["from"] == f"schedule:{schedule.id}"
    assert grants == {("user", "zhang", "use"), ("group", "门店", "use")}
    assert audits[0] == "publish_plan.plan" and audits[1] == "publish_plan.start"
    assert audits.count("publish_plan.step") == 9 and audits[-1] == "publish_plan.done"

    # 跑第一次：按定时任务触发，应用一打开就有数
    first = await s.wait(steps["first_run"]["result"]["run_id"])
    assert first.status == "succeeded" and first.schedule_id == schedule.id and first.run_class == "formal"
    r = await client.get(f"/api/apps/{application.id}/pages/home")
    assert r.status_code == 200, r.text
    stamp = next(n for n in r.json()["tree"] if n["type"] == "stamp")["data"]["stamp"]
    assert stamp["from"].startswith("定时任务")

    # 做完了的单不能再执行
    r = await client.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 409 and r.json()["code"] == "plan_state"


async def test_preflight_blocks_and_nothing_is_written(client, tmp_path):
    bp = await _board_from_run(client, tmp_path, cadence={**s.WEEKLY, "tz": "Mars/Olympus"}, trial=False)
    workflows, schedules, apps = await _count(Workflow), await _count(Schedule), await _count(App)
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    assert plan["ok"] is False
    codes = {p["code"]: p for p in plan["problems"]}
    assert codes["plan.schedule_invalid"]["level"] == "error" and codes["plan.schedule_invalid"]["plain"]["title"]
    # 没试运行过：照实提醒，不拦
    assert codes["plan.untrialed"]["level"] == "warning"

    r = await client.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 200, r.text
    stopped = r.json()
    steps = _steps(stopped)
    assert stopped["status"] == "stopped" and stopped["ok"] is False
    assert steps["preflight"]["status"] == "failed" and steps["preflight"]["error_code"] == "plan.preflight"
    assert "什么都没写" in steps["preflight"]["message"]
    assert all(st["status"] in ("pending", "skipped") for k, st in steps.items() if k != "preflight")
    # 什么都没写：连工作流版本都没存，没有定时任务、没有应用
    assert (await _count(Workflow), await _count(Schedule), await _count(App)) == (workflows, schedules, apps)
    assert (await s.blueprint(bp["id"])).workflow_id is None


async def test_a_failed_trial_blocks_the_plan(client, tmp_path):
    """最近一次试运行失败了：挡住发布（用真数跑出了错，发布出去每次出数都是同一个错），写明出错的那一句；不再只是
    「还没有成功的试运行」的提醒。"""
    from sqlalchemy import update

    bp = await _board_from_run(client, tmp_path)
    async with SessionLocal() as session:
        await session.execute(update(Run).where(Run.id == bp["last_trial_run_id"]).values(
            status="failed", error="工具 db_query__shop 查询失败：(1049, \"Unknown database 'shop'\")\n第二行不要"))
        await session.commit()
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    assert plan["ok"] is False
    codes = {p["code"]: p for p in plan["problems"]}
    failed = codes["plan.trial_failed"]
    assert failed["level"] == "error" and failed["who"] == "you" and failed["plain"]["title"] == "最近一次试运行失败了"
    assert failed["message"] == "最近一次试运行失败了：工具 db_query__shop 查询失败：(1049, \"Unknown database 'shop'\")"
    assert "plan.untrialed" not in codes


async def test_a_rejected_trial_is_a_warning_not_a_failure(client, tmp_path):
    """最近一次试运行在某一步被驳回（cancelled + end_reason=rejected）：那是人的决定，不说成「试运行失败了」、不挡发布，
    照实提醒没走完（05-已知遗留 (a)）。"""
    from sqlalchemy import update

    bp = await _board_from_run(client, tmp_path)
    async with SessionLocal() as session:
        trial = await session.get(Run, bp["last_trial_run_id"])
        node_id = trial.graph["nodes"][-1]["id"]
        label = trial.graph["nodes"][-1]["data"]["label"]
        await session.execute(update(Run).where(Run.id == trial.id).values(
            status="cancelled", end_reason="rejected", error_node_id=node_id, error="人工驳回：不对"))
        await session.commit()
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    codes = {p["code"]: p for p in plan["problems"]}
    assert "plan.trial_failed" not in codes and "plan.untrialed" not in codes
    rejected = codes["plan.trial_rejected"]
    assert rejected["level"] == "warning" and rejected["message"] == f"最近一次试运行在「{label}」被驳回、没有走完"
    assert rejected["plain"]["title"] == "最近一次试运行被驳回、没有走完"
    assert plan["ok"] is True, [p["code"] for p in plan["problems"] if p["level"] == "error"]
    summary = {x["step"]: x["text"] for x in plan["summary"]}
    assert "被驳回、没有走完" in summary["publish_workflow"]


async def test_a_process_with_an_unbound_step_stops_at_preflight(client):
    wf_id = await s.published_workflow(s.expense_graph(), f"报销-{uuid.uuid4().hex[:4]}", description="报销审批")
    bp = (await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                     "scope": "shared"})).json()
    await s.edit_spec(bp["id"], purpose=s.item("报销审批"))
    assert (await client.post(f"/api/blueprints/{bp['id']}/instantiate")).status_code == 200
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    assert plan["ok"] is False
    # 自由形按导入的图列出「谁来做」（05-已知遗留 (d)）：会签的 finance、超时升级的 director 各一行，没绑人报在那一行上；
    # 应用检查说的是同一件事（app.slot_unbound），不再重复列——它指的「谁来做」以前在自由形蓝图上不存在
    unbound = {p["path"]: p for p in plan["problems"] if p["code"] == "bp.people_unbound"}
    assert set(unbound) == {"/people/0/who", "/people/1/who"}
    assert unbound["/people/0/who"]["plain"]["who"] == "you" and "谁来做" in unbound["/people/0/who"]["plain"]["action"]
    assert "app.slot_unbound" not in {p["code"] for p in plan["problems"]}
    # 流程类不跑第一次；已发布的那一版按哈希复用，不另存
    assert plan["options"]["first_run"] is False
    summary = {x["step"]: x["text"] for x in plan["summary"]}
    assert "沿用" in summary["save_version"] and "流程类" in summary["first_run"]


@pytest.fixture
async def erp_write():
    """一项写入能力：绑定登记端点的 POST，没登记查询（结果未知时平台查不了，要人核实）。"""
    from sqlalchemy import delete

    from app.capabilities import registry
    from app.db.models import Capability, CustomTool, Endpoint

    tag = uuid.uuid4().hex[:6]
    name = f"erp_claim_{tag}"
    async with SessionLocal() as session:
        erp = Endpoint(name=f"erp_{tag}", environments={"prod": {"base_url": "https://erp.example.com", "headers": {}}})
        session.add(erp)
        await session.flush()
        session.add(CustomTool(name=name, kind="http", parameters={"type": "object", "properties": {}},
                               config={"endpoint": erp.id, "method": "POST", "path": "/claims"}))
        await session.commit()
        await registry.sync_all(session)
    try:
        yield name
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(CustomTool).where(CustomTool.name == name))
            await session.execute(delete(Endpoint).where(Endpoint.name == f"erp_{tag}"))
            await session.execute(delete(Capability).where(Capability.name == name))
            await session.commit()
            await registry.refresh_cache(session)


async def test_a_freeform_blueprint_binds_who_does_each_step_of_the_imported_graph(client, erp_write):
    """从已发布的「财务审批（岗位 finance）+ 写回（没登记查询）」工作流建自由形蓝图（05-已知遗留 (d)）：卡片上按图列出两行
    ——finance 和写入结果不明时的核实人；不选人时发布单在这两行上报 bp.people_unbound，不再有指不到卡片的 app.slot_unbound /
    app.reconciler_unbound；选好人之后绑到应用的岗位，发布单不再挡。卡片上只定由谁做，改会签被拒（在画布上改）。"""
    from app.blueprints.publish_plan import bindings_of

    graph = {"nodes": [
        s.node("start", "input", "发起", fields=[{"name": "amount", "type": "number"}]),
        s.node("review", "human", "财务审批", mode="approve", title="审一下", assignee={"kind": "slot", "slot": "finance"}),
        s.node("pay", "tool", "写回报销单", tool=erp_write, args={}),
        s.node("done", "output", "完成", fields=[{"name": "结论", "value": "已写回"}])],
        "edges": [{"source": "start", "target": "review"},
                  {"source": "review", "target": "pay", "sourceHandle": "approved"},
                  {"source": "pay", "target": "done"}]}
    wf_id = await s.published_workflow(graph, f"写回-{uuid.uuid4().hex[:4]}", description="报销写回")
    bp = (await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                     "scope": "shared"})).json()
    await s.edit_spec(bp["id"], purpose=s.item("报销审批后写回"))
    assert (await client.post(f"/api/blueprints/{bp['id']}/instantiate")).status_code == 200
    out = (await client.get(f"/api/blueprints/{bp['id']}")).json()
    rows = out["derived_people"]
    assert [(r["kind"], r["slot"], r["path"], r["who"]) for r in rows] == [
        ("slot", "finance", "/people/0/who", None), ("reconciler", "reconciler", "/writeback/reconciler", None)]
    assert rows[0]["label"] == "「财务审批」" and rows[0]["detail"] == "「财务审批」处理"
    assert rows[1]["detail"] == "「写回报销单」写入结果不明时去核实"

    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    assert plan["ok"] is False
    codes = [p["code"] for p in plan["problems"]]
    assert sorted(p["path"] for p in plan["problems"] if p["code"] == "bp.people_unbound") == [
        "/people/0/who", "/writeback/reconciler"]
    assert "app.slot_unbound" not in codes and "app.reconciler_unbound" not in codes
    summary_you = [p for p in plan["problems"] if p["code"] == "bp.people_unbound"]
    assert all(p["who"] == "you" for p in summary_you)

    refused = await client.patch(f"/api/blueprints/{bp['id']}", json={"ops": [
        {"op": "bp_set", "path": "/people/0/policy", "value": {"kind": "all"}}]})
    assert refused.status_code == 422 and refused.json()["code"] == "bp.template_mismatch"
    assert "在画布上改" in refused.json()["detail"]
    r = await client.patch(f"/api/blueprints/{bp['id']}", json={"ops": [
        {"op": "bp_set", "path": "/people/0/who", "value": [{"kind": "user", "name": "zhang"}]},
        {"op": "bp_set", "path": "/writeback/reconciler", "value": [{"kind": "group", "name": "财务组"}]}]})
    assert r.status_code == 200, r.text
    rows = r.json()["derived_people"]
    assert rows[0]["who"] == [{"kind": "user", "name": "zhang"}] and rows[1]["who"] == [{"kind": "group", "name": "财务组"}]
    assert not [i for i in r.json()["issues"] if i["code"] == "bp.people_unbound"]
    assert bindings_of(await s.blueprint(bp["id"]))["slots"] == {
        "finance": [{"kind": "user", "name": "zhang"}], "reconciler": [{"kind": "group", "name": "财务组"}]}
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    codes = [p["code"] for p in plan["problems"]]
    assert "bp.people_unbound" not in codes and "app.slot_unbound" not in codes and "app.reconciler_unbound" not in codes


async def test_the_reconciler_comes_from_the_writeback_card(client, erp_write):
    """审批通过后写回一项没登记查询的能力：结果不明时要人核实。发布单只列一条、指到「写回哪里」卡片上那一项（应用检查说的
    核实岗位没绑、写入没人核实是同一件事，不再重复列）；在卡片上选了人，绑到应用的核实岗位，这几条都不再有。"""
    from app.blueprints.templates import get_template

    bp = (await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "approval"},
                                                     "scope": "shared"})).json()
    example = get_template("approval").example()
    await s.edit_spec(bp["id"], **{k: example[k] for k in ("purpose", "people", "fields", "access")},
                      writeback={"capability": s.item(erp_write), "mapping": s.item({"amount": "form.amount"}),
                                 "confirm": s.item(True), "reconciler": s.item(None, status="missing")})
    assert (await client.get(f"/api/blueprints/{bp['id']}")).json()["reconciler_needed"] is True

    def about_reconciler(plan: dict) -> list[str]:
        return sorted(p["code"] for p in plan["problems"] if p["code"] in ("bp.reconciler_unbound", "app.reconciler_unbound")
                      or (p["code"] == "app.slot_unbound" and p.get("path") == "slots.reconciler"))

    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    assert plan["ok"] is False and about_reconciler(plan) == ["bp.reconciler_unbound"]
    (problem,) = [p for p in plan["problems"] if p["code"] == "bp.reconciler_unbound"]
    assert problem["who"] == "you" and problem["path"] == "/writeback/reconciler"
    assert "结果不明时谁去核实" in problem["plain"]["action"]

    r = await client.patch(f"/api/blueprints/{bp['id']}", json={"ops": [
        {"op": "bp_set", "path": "/writeback/reconciler", "value": [{"kind": "group", "name": "财务组"}]}]})
    assert r.status_code == 200, r.text
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    assert about_reconciler(plan) == [], [p["code"] for p in plan["problems"]]


async def test_a_failed_step_stops_and_resume_does_not_redo_what_is_done(client, tmp_path, monkeypatch):
    from app.blueprints import publish_plan as plans

    bp = await _board_from_run(client, tmp_path)
    original = plans._STEPS["publish_app"]
    calls = {"n": 0}

    async def flaky(session, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise plans.StepFailed(plans.APP_PUBLISH, "工作流已经发布，应用没有通过检查、还没有发布；使用者暂时看不到它")
        return await original(session, ctx)

    monkeypatch.setitem(plans._STEPS, "publish_app", flaky)
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    stopped = (await client.post(f"/api/publish-plans/{plan['id']}/run")).json()
    steps = _steps(stopped)
    assert stopped["status"] == "stopped"
    assert [k for k, st in steps.items() if st["status"] == "done"] == [
        "save_version", "preflight", "publish_workflow", "create_schedule", "create_app"]
    assert steps["publish_app"]["status"] == "failed" and steps["publish_app"]["error_code"] == "plan.app_publish"
    assert steps["publish_app"]["message"].startswith("工作流已经发布")
    # 中途停下的状态无害：定时任务停用着、应用停在草稿、没授权
    async with SessionLocal() as session:
        schedule = await session.get(Schedule, steps["create_schedule"]["result"]["schedule_id"])
        application = await session.get(App, steps["create_app"]["result"]["app_id"])
        grants = (await session.execute(select(AppGrant).where(AppGrant.app_id == application.id))).scalars().all()
    assert schedule.enabled is False and schedule.next_run_at is None
    assert application.published_version is None and not grants
    # 停下的单不能「执行」，要「接着做」
    r = await client.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 409 and r.json()["code"] == "plan_state"

    versions, schedules, apps = await _count(WorkflowVersion), await _count(Schedule), await _count(App)
    r = await client.post(f"/api/publish-plans/{plan['id']}/resume")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "done", done["steps"]
    again = _steps(done)
    # 已完成的按 id 核对、不重做：同一个工作流版本、同一个定时任务、同一个应用
    for kind in ("save_version", "create_schedule", "create_app"):
        assert again[kind]["result"] == steps[kind]["result"]
    assert (await _count(WorkflowVersion), await _count(Schedule), await _count(App)) == (versions, schedules, apps)
    async with SessionLocal() as session:
        schedule = await session.get(Schedule, steps["create_schedule"]["result"]["schedule_id"])
        application = await session.get(App, steps["create_app"]["result"]["app_id"])
    assert schedule.enabled is True and application.published_version == 1
    await s.wait(again["first_run"]["result"]["run_id"])


async def test_resume_stops_when_something_done_is_gone(client, tmp_path, monkeypatch):
    from app.blueprints import publish_plan as plans

    bp = await _board_from_run(client, tmp_path)

    async def broken(session, ctx):
        raise plans.StepFailed(plans.APP_PUBLISH, "工作流已经发布，应用还没有")

    monkeypatch.setitem(plans._STEPS, "publish_app", broken)
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    stopped = (await client.post(f"/api/publish-plans/{plan['id']}/run")).json()
    schedule_id = _steps(stopped)["create_schedule"]["result"]["schedule_id"]
    async with SessionLocal() as session:
        await session.delete(await session.get(Schedule, schedule_id))
        await session.commit()
    schedules = await _count(Schedule)
    again = (await client.post(f"/api/publish-plans/{plan['id']}/resume")).json()
    steps = _steps(again)
    assert again["status"] == "stopped"
    assert steps["create_schedule"]["status"] == "failed"
    assert steps["create_schedule"]["error_code"] == "plan.object_missing"
    # 不另建一份；这份单不能再接着做，要重新出单
    assert await _count(Schedule) == schedules
    r = await client.post(f"/api/publish-plans/{plan['id']}/resume")
    assert r.status_code == 409 and r.json()["code"] == "plan.object_missing"


async def test_a_changed_graph_refuses_to_run(client, tmp_path):
    bp = await _board_from_run(client, tmp_path)
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    # 出单之后改了图（卡片改了、重新实例化）
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp["id"])
        graph = {**row.graph, "nodes": [{**n, "data": {**n["data"], "label": "改过的"}} if n["id"] == "done" else n
                                         for n in row.graph["nodes"]]}
        row.graph = graph
        await session.commit()
    assert (await client.post(f"/api/blueprints/{bp['id']}/instantiate")).status_code == 200
    r = await client.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 409 and r.json()["code"] == "plan.graph_changed"
    assert (await s.blueprint(bp["id"])).workflow_id is None

    # 重新出单：发布的图和最近一次试运行的不同，照实提醒（不拦）
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    differs = [p for p in plan["problems"] if p["code"] == "plan.trial_differs"]
    assert differs and differs[0]["level"] == "warning" and plan["ok"] is True


@pytest.fixture
async def login():
    await auth_env.reset()
    try:
        yield
    finally:
        await auth_env.reset()


async def test_governed_plans_need_an_admin(login, tmp_path):
    admin = await auth_env.turn_on_local()
    u = uuid.uuid4().hex[:6]
    await auth_env.make_user(f"zhao{u}", "builder")
    builder = await auth_env.login(f"zhao{u}")
    source = await s.register_source(s.make_source(tmp_path))
    wf_id = await s.published_workflow(s.board_graph(source), f"周报-{u}", description="每周看上周销售额")
    bp = (await builder.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                      "scope": "shared"})).json()
    await s.edit_spec(bp["id"], purpose=s.item("每周看上周销售额"))
    assert (await builder.post(f"/api/blueprints/{bp['id']}/instantiate")).status_code == 200

    plan = (await builder.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "governed"})).json()
    admin_only = [p for p in plan["problems"] if p["code"] == "plan.admin_required"]
    assert plan["ok"] is False and admin_only and admin_only[0]["plain"]["who"] == "admin"
    r = await builder.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 403 and r.json()["code"] == "admin_only"
    # 管理员出的受管级单子不报这一条
    plan = (await admin.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "governed"})).json()
    assert not [p for p in plan["problems"] if p["code"] == "plan.admin_required"]
    # 使用者看不到发布单
    await auth_env.make_user(f"lee{u}", "user")
    user = await auth_env.login(f"lee{u}")
    # 使用者碰共享蓝图的发布单回 404，和看不到的一样（D5）
    assert (await user.get(f"/api/publish-plans/{plan['id']}")).status_code == 404


async def test_runs_launched_by_a_plan_are_formal_and_from_the_published_version(client, tmp_path):
    """看板没有定时任务时，跑第一次从发布的那一版发起正式运行（来源 launcher）。"""
    bp = await _board_from_run(client, tmp_path)
    await s.edit_spec(bp["id"], cadence=s.item(None, status="missing"))
    assert (await client.post(f"/api/blueprints/{bp['id']}/instantiate")).status_code == 200
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    steps = _steps(plan)
    assert steps["create_schedule"]["status"] == "skipped" and steps["enable_schedule"]["status"] == "skipped"
    done = (await client.post(f"/api/publish-plans/{plan['id']}/run")).json()
    assert done["status"] == "done", done["steps"]
    run = await s.wait(_steps(done)["first_run"]["result"]["run_id"])
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
    assert row.run_class == "formal" and row.version == 1 and row.origin_kind == "launcher"


async def test_a_published_review_copy_closes_the_review(client, tmp_path, monkeypatch):
    """审核副本（个人应用提交审核，搭建者在副本上走发布单）做完之后，原蓝图的审核待办记为已发布（blueprints/personal.py）。"""
    from app.blueprints import personal

    bp = await _board_from_run(client, tmp_path)
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp["id"])
        row.copied_from = "original-bp"
        await session.commit()
    seen = []

    async def published(session, blueprint, *, app_id, principal):
        seen.append((blueprint.id, app_id, principal.username))
        return "task-1"

    monkeypatch.setattr(personal, "review_published", published)
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published",
                                                                               "first_run": False})).json()
    done = (await client.post(f"/api/publish-plans/{plan['id']}/run")).json()
    assert done["status"] == "done" and _steps(done)["first_run"]["status"] == "skipped"
    assert seen == [(bp["id"], _steps(done)["create_app"]["result"]["app_id"], "builder1")]


async def test_publishing_again_upgrades_the_same_app_and_keeps_its_schedule(client, tmp_path):
    """已经发布过的蓝图改了取数再发一版（N6 改取数回到蓝图）：同一个工作流存新版本、同一个应用升版（口径变了要处置）、
    沿用原来的定时任务（按蓝图更新时间），不另建一份。"""
    bp = await _board_from_run(client, tmp_path)
    first = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published",
                                                                                "first_run": False})).json()
    first = (await client.post(f"/api/publish-plans/{first['id']}/run")).json()
    assert first["status"] == "done", first["steps"]
    schedule_id = _steps(first)["create_schedule"]["result"]["schedule_id"]
    app_id = _steps(first)["create_app"]["result"]["app_id"]

    # 改取数：销售额去掉 999 的那一单；改「多久更新」：每周二
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp["id"])
        nodes = []
        for n in row.graph["nodes"]:
            if n["id"] == "q":
                config = {**n["data"]["config"], "args": {"sql": s.GMV_SQL + " AND amount < 900"}}
                n = {**n, "data": {**n["data"], "config": config}}
            nodes.append(n)
        row.graph = {**row.graph, "nodes": nodes}
        row.spec = {**row.spec, "cadence": s.item({**s.WEEKLY, "weekday": 2})}
        await session.commit()
    r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": s.PERIOD})
    assert (await s.wait(r.json()["run_id"])).status == "succeeded"

    # 口径变了，没选处置：停在预检，你来定
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published",
                                                                               "first_run": False})).json()
    upgrade = [p for p in plan["problems"] if p["code"] == "app.upgrade_policy"]
    assert plan["ok"] is False and upgrade and upgrade[0]["plain"]["who"] == "you"
    summary = {x["step"]: x["text"] for x in plan["summary"]}
    assert "沿用应用正在用的定时任务" in summary["create_schedule"] and "每周二" in summary["create_schedule"]
    assert summary["create_app"].startswith("更新应用")

    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={
        "level": "published", "first_run": False, "dispositions": {"main": "recompute"}})).json()
    assert plan["ok"] is True, plan["problems"]
    done = (await client.post(f"/api/publish-plans/{plan['id']}/run")).json()
    assert done["status"] == "done", done["steps"]
    steps = _steps(done)
    assert steps["create_schedule"]["result"] == {"schedule_id": schedule_id, "enabled": True, "reused": True}
    assert steps["create_app"]["result"] == {"app_id": app_id, "created": False}
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp["id"])
        wf = await session.get(Workflow, row.workflow_id)
        schedule = await session.get(Schedule, schedule_id)
        count = int(await session.scalar(select(func.count(Schedule.id)).where(Schedule.workflow_id == wf.id)) or 0)
        version = (await session.execute(select(AppVersion).where(
            AppVersion.app_id == app_id, AppVersion.version == 2))).scalar_one()
    assert wf.published_version == 2 and count == 1
    assert schedule.cron.endswith(" 2") and schedule.enabled is True
    assert version.pins[0]["version"] == 2 and version.dispositions == {"main": "recompute"}
    assert version.spec["views"]["latest"]["source"]["from"] == f"schedule:{schedule_id}"


async def test_problems_are_split_into_three_kinds(client):
    """预检的问题带白话和归谁处理（摘要按它分三栏）：岗位没绑人是「你来定」。"""
    wf_id = await s.published_workflow(s.expense_graph(), f"报销-{uuid.uuid4().hex[:4]}", description="报销审批")
    bp = (await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                     "scope": "shared"})).json()
    await s.edit_spec(bp["id"], purpose=s.item("报销审批"))
    assert (await client.post(f"/api/blueprints/{bp['id']}/instantiate")).status_code == 200
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).json()
    assert all(p["who"] in ("you", "assistant", "admin") for p in plan["problems"])
    assert {p["source"] for p in plan["problems"]} <= {"blueprint", "plan", "workflow", "schedule", "app", "grants"}
    unbound = next(p for p in plan["problems"] if p["code"] == "bp.people_unbound")
    assert unbound["who"] == "you" and unbound["source"] == "blueprint"


# --------------------------------------------------------------------------
# 复核修复：各包接缝上的几件事
# --------------------------------------------------------------------------


async def _approval_blueprint(client) -> str:
    """审批流程模板的蓝图：只有一步主管审批（处理人写了具体的人，岗位按卡片绑上），没有写回。"""
    from app.blueprints.templates import get_template

    bp = (await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "approval"}, "scope": "shared",
                                                     "name": f"报销-{uuid.uuid4().hex[:4]}"})).json()
    spec = get_template("approval").example()
    spec["people"] = spec["people"][:1]
    spec["access"] = {"use": [{"kind": "user", "name": "zhang"}], "observe": [], "manage": []}
    await s.edit_spec(bp["id"], **spec)
    return bp["id"]


def _human(graph: dict) -> dict:
    return next(n for n in graph["nodes"] if n["type"] == "human")["data"]["config"]


async def test_cards_changed_after_the_trial_are_what_gets_published(client):
    """试运行（实例化）之后改了「谁来做」的时限：图变了，出单时按卡片重新实例化，发布的是新图；和最近一次试运行不同照实
    提醒。出单之后再改卡片（这次图哈希不一定变），执行时按卡片内容的指纹认出来，拒绝执行。"""
    bp_id = await _approval_blueprint(client)
    assert (await client.post(f"/api/blueprints/{bp_id}/instantiate")).status_code == 200
    before = await s.blueprint(bp_id)
    assert _human(before.graph)["due"] == "P2D"

    r = await client.patch(f"/api/blueprints/{bp_id}", json={"ops": [
        {"op": "bp_set", "path": "/people/0/due", "value": "P1D"}]})
    assert r.status_code == 200, r.text
    assert (await s.blueprint(bp_id)).graph_hash == before.graph_hash, "卡片上的修改只刷新关节，不重算图"

    plan = (await client.post(f"/api/blueprints/{bp_id}/publish-plan", json={"level": "published"})).json()
    assert plan["ok"] is True, plan["problems"]
    now = await s.blueprint(bp_id)
    assert plan["graph_hash"] == now.graph_hash != before.graph_hash and _human(now.graph)["due"] == "P1D"
    assert plan["options"]["spec_hash"]

    # 出单之后又改了卡片（换处理人只改岗位绑定、不改图）：执行前按卡片内容的指纹认出来
    r = await client.patch(f"/api/blueprints/{bp_id}", json={"ops": [
        {"op": "bp_set", "path": "/people/0/who", "value": [{"kind": "user", "name": "li"}]}]})
    assert r.status_code == 200, r.text
    assert (await s.blueprint(bp_id)).graph_hash == now.graph_hash
    r = await client.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 409 and r.json()["code"] == "plan.graph_changed"

    plan = (await client.post(f"/api/blueprints/{bp_id}/publish-plan", json={"level": "published"})).json()
    done = (await client.post(f"/api/publish-plans/{plan['id']}/run")).json()
    assert done["status"] == "done", done["steps"]
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp_id)
        version = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == row.workflow_id))).scalars().one()
        application = await session.get(App, row.app_id)
        published = (await session.execute(select(AppVersion).where(AppVersion.app_id == application.id))).scalars().one()
    assert _human(version.graph)["due"] == "P1D", "发布的是卡片上写的时限"
    assert published.bindings["slots"]["lead"] == [{"kind": "user", "name": "li"}], "发布的是卡片上写的处理人"


async def test_a_plan_cannot_take_over_an_app_the_builder_cannot_manage(login, tmp_path):
    """蓝图已经发布成应用：只有对那个应用有管理权限的人能出单、执行（发布单会改它的草稿、发布它、整份替换授权）。

    - 别的搭建者连这张共享蓝图都看不到（D5）：读、改、出单、看单子一律 404，列表里没有；给了那个应用的管理授权之后才
      看得到；
    - 蓝图的拥有者对应用没有管理权限（应用是管理员替他发布的、只授给他使用）时，把自己写进「谁能看」的管理想借发布单
      拿到管理权：出单时预检报 plan.app_forbidden（要找管理员），执行 403；应用的授权没被改。"""
    import json

    root = await auth_env.turn_on_local()
    u = uuid.uuid4().hex[:6]
    await auth_env.make_user(f"zhao{u}", "builder")
    await auth_env.make_user(f"qian{u}", "builder")
    owner = await auth_env.login(f"zhao{u}")
    other = await auth_env.login(f"qian{u}")
    source = await s.register_source(s.make_source(tmp_path))
    wf_id = await s.published_workflow(s.board_graph(source), f"周报-{u}", description="每周看上周销售额")
    bp = (await owner.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                    "scope": "shared"})).json()
    await s.edit_spec(bp["id"], purpose=s.item("每周看上周销售额"),
                      access={"use": [{"kind": "user", "name": f"zhao{u}"}], "observe": [], "manage": []})
    # 管理员能读写、出单（D5）：应用由管理员发布，创建人是管理员
    plan = (await root.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published",
                                                                             "first_run": False})).json()
    assert not [p for p in plan["problems"] if p["code"] == "plan.app_forbidden"]
    done = (await root.post(f"/api/publish-plans/{plan['id']}/run")).json()
    assert done["status"] == "done", done["steps"]
    app_id = (await s.blueprint(bp["id"])).app_id

    # 别的搭建者：当这张蓝图不存在
    assert (await other.get(f"/api/blueprints/{bp['id']}")).status_code == 404
    assert (await other.patch(f"/api/blueprints/{bp['id']}", json={"name": "改个名"})).status_code == 404
    assert (await other.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published"})).status_code \
        == 404
    assert (await other.get(f"/api/publish-plans/{plan['id']}")).status_code == 404
    assert (await other.post(f"/api/publish-plans/{plan['id']}/run")).status_code == 404
    assert bp["id"] not in [b["id"] for b in (await other.get("/api/blueprints")).json()["items"]]
    assert bp["id"] in [b["id"] for b in (await owner.get("/api/blueprints")).json()["items"]]
    up = await other.post("/api/files", files={"file": ("说明.txt", "x".encode(), "text/plain")},
                          data={"context": json.dumps({"blueprint_id": bp["id"]})})
    assert up.status_code == 404 and up.json()["code"] == "files.context_invalid", "往别人的蓝图上传附件同样不收"
    ghost = await other.post("/api/files", files={"file": ("说明.txt", "x".encode(), "text/plain")},
                             data={"context": json.dumps({"blueprint_id": uuid.uuid4().hex})})
    assert (ghost.status_code, ghost.json()) == (up.status_code, up.json()), "看不到的和不存在的一个说法，试不出它在不在"
    assert bp["id"] in [b["id"] for b in (await root.get("/api/blueprints")).json()["items"]]

    # 拥有者把自己写进「谁能看」的管理，想借发布单拿到管理权
    await s.edit_spec(bp["id"], access={"use": [], "observe": [], "manage": [{"kind": "user", "name": f"zhao{u}"}]})
    plan = (await owner.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published",
                                                                              "first_run": False})).json()
    forbidden = [p for p in plan["problems"] if p["code"] == "plan.app_forbidden"]
    assert plan["ok"] is False and forbidden and forbidden[0]["who"] == "admin" and forbidden[0]["plain"]["title"]
    r = await owner.post(f"/api/publish-plans/{plan['id']}/run")
    assert r.status_code == 403 and r.json()["code"] == "plan.app_forbidden", r.text
    async with SessionLocal() as session:
        grants = {(g.principal, g.role) for g in (await session.execute(
            select(AppGrant).where(AppGrant.app_id == app_id))).scalars()}
    assert grants == {(f"zhao{u}", "use")}, "授权没被改"

    # 应用的管理者看得到它出自的蓝图（D5）
    async with SessionLocal() as session:
        session.add(AppGrant(app_id=app_id, principal_kind="user", principal=f"qian{u}", role="manage"))
        await session.commit()
    assert (await other.get(f"/api/blueprints/{bp['id']}")).status_code == 200
    assert bp["id"] in [b["id"] for b in (await other.get("/api/blueprints")).json()["items"]]


async def test_plans_left_running_by_a_dead_process_are_stopped_at_startup(client, tmp_path):
    """执行到一半进程没了：单子停在「正在执行」，谁也接不着。启动时改成停下，停在没做完的那一步、写明执行中断，就能接着做。"""
    from app.blueprints import publish_plan as plans
    from app.db.models_blueprints import PublishPlan

    bp = await _board_from_run(client, tmp_path)
    plan = (await client.post(f"/api/blueprints/{bp['id']}/publish-plan", json={"level": "published",
                                                                               "first_run": False})).json()
    async with SessionLocal() as session:
        row = await session.get(PublishPlan, plan["id"])
        steps = [dict(st) for st in row.steps]
        steps[0].update(status="done", result={"workflow_id": "x"})
        row.steps, row.status = steps, "running"
        await session.commit()
    assert await plans.startup() >= 1
    got = (await client.get(f"/api/publish-plans/{plan['id']}")).json()
    stopped = _steps(got)
    assert got["status"] == "stopped" and stopped["save_version"]["status"] == "done"
    assert stopped["preflight"]["status"] == "failed" and stopped["preflight"]["error_code"] == "plan.interrupted"
    assert "重启" in stopped["preflight"]["message"]
    from app.plain import plain_of

    assert plain_of("plan.interrupted") is not None


# ---------------------------------------------------------------- 填报登记：试运行 → 发布单 → 入口提交


RECORD_FIELDS = [
    {"name": "store", "spec": s.item({"type": "string", "title": "门店", "required": True}), "sensitive": False},
    {"name": "amount", "spec": s.item({"type": "number", "title": "金额", "required": True}), "sensitive": False},
    {"name": "reason", "spec": s.item({"type": "string", "title": "原因"}), "sensitive": True},
]


async def _records_blueprint(client) -> str:
    bp = (await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "records"}, "scope": "shared",
                                                     "name": f"报损-{uuid.uuid4().hex[:4]}"})).json()
    await s.edit_spec(bp["id"], purpose=s.item("门店报损登记"), fields=RECORD_FIELDS, people=[], data=[],
                      cadence=s.item(s.WEEKLY),
                      access={"use": [{"kind": "user", "name": "amy"}, {"kind": "user", "name": "bob"}],
                              "observe": [], "manage": []})
    return bp["id"]


def _append_table(graph: dict) -> str:
    return next(n for n in graph["nodes"] if n["type"] == "tool")["data"]["config"]["args"]["table"]


async def _publish(client, bp_id: str) -> dict:
    plan = (await client.post(f"/api/blueprints/{bp_id}/publish-plan", json={"level": "published"})).json()
    if any(p["code"] == "app.upgrade_policy" for p in plan["problems"]):
        plan = (await client.post(f"/api/blueprints/{bp_id}/publish-plan", json={
            "level": "published", "dispositions": {"main": "incomparable"}})).json()
    assert plan["ok"] is True, plan["problems"]
    done = (await client.post(f"/api/publish-plans/{plan['id']}/run")).json()
    assert done["status"] == "done", done["steps"]
    return done


async def _submit(client, app_id: str, values: dict, who: str) -> Run:
    r = await client.post(f"/api/apps/{app_id}/entries/start", json={"values": values}, headers={"X-Actor": who})
    assert r.status_code == 201, r.text
    run = await s.wait(r.json()["run_id"])
    assert run.status == "succeeded", run.error
    return run


async def test_records_blueprint_trials_publishes_and_submissions_land_in_the_table(client):
    """填报登记走一遍：试运行（示例表单走填报那条路，写入演练）→ 发布单 → 入口提交，记录表里有那一行。图里写的记录表是
    「键.v版本」（和填关节、卡片修改同一种写法），正式运行写得进去。

    之后在蓝图上不兼容地改表单（金额改成文字）再试运行：起草时出第 2 版，线上应用固定在第 1 版、照常收提交；发布之后才
    切到第 2 版、第 1 版停用。记录列表里别人提交的行，敏感列写「已遮罩」。"""
    from app.db.models_blueprints import RecordsTable
    from app.records import store
    from app.records.tables import path_of

    from app.capabilities.registry import sync_all

    async with SessionLocal() as session:
        # 平台能力 records_append 登记进能力表（启动时同步的那一次）：带查询能力，应用不用绑核实岗位
        await sync_all(session)
    bp_id = await _records_blueprint(client)
    r = await client.post(f"/api/blueprints/{bp_id}/trial", json={"inputs": {"store": "一店", "amount": 3}})
    assert r.status_code == 200, r.text
    trial = await s.wait(r.json()["run_id"])
    assert trial.status == "succeeded", trial.error
    assert trial.input["mode"] == "submit" and trial.input["store"] == "一店", "填了示例表单：试的是填报那条路"
    row = await s.blueprint(bp_id)
    assert _append_table(row.graph) == f"{bp_id}.v1"

    await _publish(client, bp_id)
    app_id = (await s.blueprint(bp_id)).app_id
    await _submit(client, app_id, {"store": "一店", "amount": 12.5, "reason": "过期"}, "amy")
    await _submit(client, app_id, {"store": "二店", "amount": 3, "reason": "破损"}, "bob")
    async with SessionLocal() as session:
        v1 = (await session.execute(select(RecordsTable).where(RecordsTable.table_key == bp_id))).scalars().one()
    assert v1.app_id == app_id and v1.retired_at is None
    assert store.high_water(path_of(v1)) == 2, "试运行一行都没写，两次提交各一行"

    # 记录列表：别人提交的行，敏感列写「已遮罩」；自己的照原样
    page = (await client.get(f"/api/apps/{app_id}/records", headers={"X-Actor": "amy"})).json()
    by = {r["_submitted_by"]: r["values"] for r in page["items"]}
    assert by["amy"]["reason"] == "过期" and by["bob"]["reason"] == "已遮罩" and by["bob"]["store"] == "二店"

    # 草稿上不兼容地改表单、试运行：出第 2 版，线上那一版不停用
    fields = [RECORD_FIELDS[0], {**RECORD_FIELDS[1], "spec": s.item({"type": "string", "title": "金额"})}, RECORD_FIELDS[2]]
    await s.edit_spec(bp_id, fields=fields)
    r = await client.post(f"/api/blueprints/{bp_id}/trial", json={"inputs": {"store": "三店", "amount": "三元"}})
    assert (await s.wait(r.json()["run_id"])).status == "succeeded"
    assert _append_table((await s.blueprint(bp_id)).graph) == f"{bp_id}.v2"
    async with SessionLocal() as session:
        versions = list((await session.execute(select(RecordsTable).where(RecordsTable.table_key == bp_id)
                                                .order_by(RecordsTable.version))).scalars())
    assert [(v.version, v.retired_at is None) for v in versions] == [(1, True), (2, True)]
    await _submit(client, app_id, {"store": "一店", "amount": 7, "reason": "过期"}, "amy")
    assert store.high_water(path_of(versions[0])) == 3, "线上应用照常写第 1 版"

    # 发布之后才切换：第 1 版停用（写明换成了第 2 版），新的提交写进第 2 版
    done = await _publish(client, bp_id)
    assert {st["kind"]: st for st in done["steps"]}["publish_app"]["result"]["records"] == [f"{bp_id}.v2"]
    async with SessionLocal() as session:
        versions = list((await session.execute(select(RecordsTable).where(RecordsTable.table_key == bp_id)
                                                .order_by(RecordsTable.version))).scalars())
    assert versions[0].retired_at is not None and "第 2 版" in versions[0].note and versions[1].retired_at is None
    await _submit(client, app_id, {"store": "四店", "amount": "五元"}, "amy")
    assert store.high_water(path_of(versions[1])) == 1 and store.high_water(path_of(versions[0])) == 3
