"""用真数试运行（docs/design/normal-user-apps.md N4、§3.5，包 B2 的 blueprints/trial.py 和 apps/resolve.py 的 trial 旁路）。

- 试运行是探索运行，来源记 trial（origin 里是蓝图 id），蓝图记下这次运行；成功结束后蓝图记「试运行过」；
- 预览：草稿应用对着这次运行解析——视图来源一律是这次运行（kind=trial），印章的来源写「试运行」、trial=true、说明里写
  「这不是正式结果」；入口按钮点不了、说清为什么；
- 趋势在试运行里只有一个点：照实写「没有趋势」，不报错；按草稿预览一个还没有版本号的钉住项，趋势也不报错；
- 流程类：人工步骤派给试运行的人自己（来源 trial），会签、升级的岗位都不派；到了时限调度也不升级；预览里待办写明是
  试运行，试运行的人能处理、同意之后流程走完；
- 缺项挡试运行：409，带问题和白话；
- 从一张图派生（apps/derive.derive_from_graph）：钉住项没有版本号、哈希是这张图的。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import Principal
from app.apps.resolve import TRIAL_FROM, TRIAL_NOTE, TRIAL_SERIES, UNPUBLISHED_EMPTY
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import Run, Task, TaskAssignment
from app.engine.runner import run_manager
from app.main import app
from tests import blueprint_samples as s

ACTOR = {"X-Actor": "tester"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=ACTOR) as c:
        yield c


def walk(tree):
    for n in tree:
        yield n
        yield from walk(n.get("children") or [])


def find(vm, ctype):
    return [n for n in walk(vm["tree"]) if n["type"] == ctype]


async def _board_blueprint(client, tmp_path) -> dict:
    source = await s.register_source(s.make_source(tmp_path))
    wf_id = await s.published_workflow(s.board_graph(source), f"周报-{uuid.uuid4().hex[:4]}",
                                       description="每周看上周销售额")
    r = await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                   "scope": "shared"})
    assert r.status_code == 201, r.text
    bp = r.json()
    await s.edit_spec(bp["id"], purpose=s.item("每周看上周销售额"), cadence=s.item(s.WEEKLY))
    return bp


async def test_trial_runs_the_unpublished_graph_and_previews_with_a_trial_stamp(client, tmp_path):
    bp = await _board_blueprint(client, tmp_path)
    r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": s.PERIOD})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert r.json()["blueprint"]["last_trial_run_id"] == run_id
    run = await s.wait(run_id)
    assert run.status == "succeeded", run.error
    assert run.run_class == "exploratory" and run.origin_kind == "trial"
    assert run.origin["blueprint_id"] == bp["id"] and run.started_by == "tester"
    # 自由形导入的图原样跑（哈希和导入的那一版一样），名字记蓝图名
    assert graph_hash(run.graph) == bp["graph_hash"] and run.workflow_name == bp["name"]
    assert run.input["period_start"] == s.PERIOD["period_start"]
    assert r.json()["inputs"] == run.input, "回包里是这次实际用的输入"

    r = await client.get(f"/api/blueprints/{bp['id']}/preview")
    assert r.status_code == 200, r.text
    vm = r.json()
    assert vm["trial"]["run_id"] == run_id and vm["app"]["needs_upgrade"] is False
    assert all(src["kind"] == "trial" and src["run_id"] == run_id for src in vm["sources"].values())
    stamp = find(vm, "stamp")[0]["data"]["stamp"]
    assert stamp["from"] == TRIAL_FROM and stamp["trial"] is True and TRIAL_NOTE in stamp["notes"]
    assert stamp["pin"] == bp["name"]
    metric = find(vm, "metric")[0]["data"]
    assert metric["state"] == "ok" and metric["text"] and metric["evidence"]["run_id"] == run_id
    assert not vm["problems"]
    # 成功结束了：蓝图记「试运行过」
    assert (await client.get(f"/api/blueprints/{bp['id']}")).json()["status"] == "trialed"

    # 不是这个蓝图的试运行不给预览
    other = await run_manager.start(graph=s.chain(s.node("a", "input")), input_payload={}, started_by="x",
                                    run_class="exploratory", origin_kind="canvas")
    r = await client.get(f"/api/blueprints/{bp['id']}/preview", params={"run": other.id})
    assert r.status_code == 404 and r.json()["code"] == "trial_no_run"


async def test_trial_series_has_one_point_and_a_draft_without_version_does_not_break(tmp_path):
    """趋势：试运行只有一个点，写「没有趋势」；按草稿预览没有版本号的钉住项，趋势是空态（以前 int(None) 报错）。"""
    from app.apps.resolve import resolve_page
    from app.blueprints.trial import PreviewApp

    source = await s.register_source(s.make_source(tmp_path))
    graph = s.board_graph(source)
    run = await run_manager.start(graph=graph, input_payload=s.PERIOD, started_by="tester", run_class="exploratory",
                                  origin_kind="trial", origin={"blueprint_id": "bp-x"})
    run = await s.wait(run.id)
    spec = {"format": "agentlab.app/1", "name": "周报",
            "pins": [{"alias": "main", "blueprint": "bp-x", "version": None, "graph_hash": graph_hash(graph)}],
            "views": {"latest": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}}},
            "pages": [{"id": "home", "title": "周报", "body": [
                {"id": "trend", "type": "line_chart", "bind": {"view": "latest", "series": "caliber.gmv"}},
                {"id": "go", "type": "entry_button", "props": {"entry": "start"}}]}]}
    principal = Principal(username="tester", role="admin")
    preview = PreviewApp(id="bp-x", name="周报", draft=spec)
    async with SessionLocal() as session:
        vm = await resolve_page(session, preview, page_id="home", principal=principal, role="manage", draft=True,
                                trial=run)
        plain = await resolve_page(session, preview, page_id="home", principal=principal, role="manage", draft=True)
    trend = next(n for n in vm["tree"] if n["id"] == "trend")["data"]
    assert trend["state"] == "ok" and trend["note"] == TRIAL_SERIES
    assert len(trend["series"]) == 1 and len(trend["series"][0]["points"]) == 1
    assert trend["series"][0]["points"][0]["run_id"] == run.id and trend["series"][0]["points"][0]["value"] is not None
    button = next(n for n in vm["tree"] if n["id"] == "go")["data"]
    assert button["disabled"] is True and "试运行" in button["reason"]
    assert not vm["problems"]
    # 不是试运行、只是按草稿预览：没有版本号就没有正式运行可看，照实说「还没有发布」，不进 problems
    trend = next(n for n in plain["tree"] if n["id"] == "trend")["data"]
    assert trend["state"] == "empty" and trend["message"] == UNPUBLISHED_EMPTY
    assert not [p for p in plain["problems"] if p["code"] == "view.resolve_failed"]


async def test_process_trial_assigns_every_step_to_the_trial_runner_and_never_escalates(client):
    from app.process import tasks

    wf_id = await s.published_workflow(s.expense_graph(), f"报销-{uuid.uuid4().hex[:4]}", description="报销审批")
    r = await client.post("/api/blueprints", json={"source": {"kind": "workflow_version", "id": wf_id},
                                                   "scope": "shared"})
    bp = r.json()
    await s.edit_spec(bp["id"], purpose=s.item("报销审批"))
    r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={"inputs": {"applicant": "钱七", "amount": 1280}})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    await s.wait(run_id, ("interrupted",))
    async with SessionLocal() as session:
        task = (await session.execute(select(Task).where(Task.run_id == run_id))).scalars().one()
        rows = list((await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id)))
                    .scalars())
    # 图里派给岗位 finance（会签、没有绑定）：试运行一律派给发起试运行的人自己
    assert [(a.principal_kind, a.principal, a.source) for a in rows] == [("user", "tester", "trial")]
    assert task.due_at is not None and task.on_timeout["action"] == "escalate"

    # 时限到了：调度不升级试运行的待办（同一个库里别的测试的待办照常处理，只看这一条）
    assert task.id not in await tasks.tick(utcnow() + timedelta(hours=2))
    async with SessionLocal() as session:
        task = await session.get(Task, task.id)
        count = len(list((await session.execute(select(TaskAssignment).where(
            TaskAssignment.task_id == task.id))).scalars()))
    assert task.escalation_level == 0 and task.status == "pending" and count == 1

    # 预览实例页：流程组件对着这次试运行，待办写明是试运行，试运行的人能处理
    r = await client.get(f"/api/blueprints/{bp['id']}/preview", params={"page": "instance"})
    assert r.status_code == 200, r.text
    vm = r.json()
    form = next(n for n in walk(vm["tree"]) if n["type"] == "task_form")["data"]
    assert form["trial"] is True and form["handler"] is True and form["task"]["id"] == task.id
    timeline = find(vm, "instance_timeline")[0]["data"]
    assert timeline["state"] == "ok" and timeline["status_text"] == "等待审批"
    # 「换一期再试」的表单随预览给（blueprints/trial.retry_form）：流程类是入口的示例表单
    assert set(vm["trial"]["form"]["properties"]) >= {"amount"}, vm["trial"]["form"]
    # 首页的「我发起的」列的就是这次试运行
    home = (await client.get(f"/api/blueprints/{bp['id']}/preview", params={"page": "home"})).json()
    assert [i["run_id"] for i in find(home, "instance_list")[0]["data"]["items"]] == [run_id]
    # 「派给我的待办」列的是这次试运行的待办（试运行的待办不属于任何应用）
    from app.apps.resolve import resolve_page
    from app.blueprints.trial import PreviewApp

    spec = {"format": "agentlab.app/1", "name": "报销", "pages": [{"id": "home", "title": "报销", "body": [
        {"id": "mine", "type": "task_list", "props": {"scope": "mine"}}]}]}
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        vm = await resolve_page(session, PreviewApp(id=bp["id"], name="报销", draft=spec), page_id="home",
                                principal=Principal(username="tester", role="admin"), role="manage", draft=True,
                                trial=run)
    assert [i["id"] for i in vm["tree"][0]["data"]["items"]] == [task.id]
    # 待办箱里同样写明是试运行
    detail = await tasks.detail(task.id, Principal(username="tester", role="admin"))
    assert detail["trial"] is True

    # 以试运行身份同意：流程走完
    await tasks.respond(task.id, Principal(username="tester", role="admin"), decision="approve")
    run = await s.wait(run_id)
    assert run.status == "succeeded"
    assert (await client.get(f"/api/blueprints/{bp['id']}")).json()["status"] == "trialed"


async def test_missing_cards_block_the_trial_with_plain_reasons(client):
    r = await client.post("/api/blueprints", json={"source": {"kind": "conversation"}, "scope": "shared"})
    bp = r.json()
    r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "trial_not_ready" and body["issues"]
    first = body["issues"][0]
    assert first["code"] == "bp.missing" and first["plain"]["title"]
    async with SessionLocal() as session:
        trials = (await session.execute(select(Run.origin).where(Run.origin_kind == "trial"))).scalars().all()
    assert not [o for o in trials if (o or {}).get("blueprint_id") == bp["id"]]


async def test_derive_from_graph_pins_the_graph_hash_without_a_version(tmp_path):
    from app.apps.derive import derive_from_graph

    source = await s.register_source(s.make_source(tmp_path))
    graph = s.board_graph(source)
    async with SessionLocal() as session:
        spec = await derive_from_graph(session, graph, name="周报", blueprint_id="bp-1")
        with_wf = await derive_from_graph(session, graph, name="周报", workflow_id="wf-1", schedule_id="sch-1")
    pin = spec["pins"][0]
    assert pin["version"] is None and pin["graph_hash"] == graph_hash(graph) and pin["blueprint"] == "bp-1"
    assert pin["workflow_id"] is None and pin["use"] == "view"
    assert spec["views"]["latest"]["source"]["from"] == "formal"
    assert any(c["type"] == "stamp" for c in spec["pages"][0]["body"])
    assert with_wf["pins"][0]["workflow_id"] == "wf-1" and with_wf["pins"][0].get("blueprint") is None
    assert with_wf["views"]["latest"]["source"]["from"] == "schedule:sch-1"


@pytest.fixture
async def login():
    from app.db.models import Setting
    from tests.fakes import auth_env

    await auth_env.reset()
    async with SessionLocal() as session:
        row = await session.get(Setting, "apps")
        if row is None:
            session.add(Setting(key="apps", value={"user_personal_apps": True}))
        else:
            row.value = {**(row.value or {}), "user_personal_apps": True}
        await session.commit()
    try:
        yield auth_env
    finally:
        await auth_env.reset()
        async with SessionLocal() as session:
            row = await session.get(Setting, "apps")
            if row is not None:
                await session.delete(row)
                await session.commit()


async def test_a_users_trial_follows_the_chat_rules(login, tmp_path):
    """使用者试运行自己的个人蓝图：和问数据同一份约束——结果遮罩、只读、记忆是他自己的；取数的调用工具节点（查数据源）
    能试（D1），绑了使用者不能用的工具（写入能力）的不试。"""
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    await login.turn_on_local()
    u = uuid.uuid4().hex[:6]
    await login.make_user(f"lee{u}", "user")
    user = await login.login(f"lee{u}")

    async def personal(graph: dict) -> dict:
        run = await run_manager.start(graph=graph, input_payload={**s.PERIOD, "question": "看看订单"},
                                      started_by=f"lee{u}", run_class="exploratory", origin_kind="chat")
        assert (await s.wait(run.id)).status == "succeeded"
        r = await user.post("/api/blueprints", json={"source": {"kind": "run", "id": run.id}, "scope": "personal"})
        assert r.status_code == 201, r.text
        bp = r.json()
        await s.edit_spec(bp["id"], purpose=s.item("看看订单"))
        return bp

    bp = await personal(s.chain(s.node("start", "input", fields=[{"name": "question"}]),
                                s.node("done", "output", fields=[{"name": "问题", "value": "{{ input.question }}"}])))
    r = await user.post(f"/api/blueprints/{bp['id']}/trial", json={})
    assert r.status_code == 200, r.text
    run = await s.wait(r.json()["run_id"])
    assert run.options[MASK_RESULTS_OPTION] is True and run.options[READ_ONLY_OPTION] is True
    assert run.memory_scope == f"chat:lee{u}"

    source = await s.register_source(s.make_source(tmp_path))
    bp = await personal(s.board_graph(source))
    r = await user.post(f"/api/blueprints/{bp['id']}/trial", json={})
    assert r.status_code == 200, r.text
    run = await s.wait(r.json()["run_id"])
    assert run.status == "succeeded" and run.options[READ_ONLY_OPTION] is True, run.error

    # 同一张图里取数节点换成写记录表：使用者不能用的工具，不试
    from app.db.models_blueprints import Blueprint

    writes = s.board_graph(source)
    writes["nodes"][1]["data"]["config"] = {"tool": "records_append", "args": {"table": "x", "values": {}}}
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp["id"])
        row.graph, row.graph_hash = writes, graph_hash(writes)
        await session.commit()
    r = await user.post(f"/api/blueprints/{bp['id']}/trial", json={})
    assert r.status_code == 403 and r.json()["code"] == "trial_policy" and "个人应用" in r.json()["detail"], r.text
    assert "records_append" in r.json()["detail"]


async def test_a_template_blueprint_trials_with_the_latest_period_and_keeps_its_filled_joints(client, tmp_path):
    """按模板（周期看板）：生成关节填好的蓝图实例化、试运行。不给输入时按「多久更新」算出最近一期（每周看上周）；
    重新实例化（改了别的卡片）不会把填好的 SQL、指标式丢掉；印章写试运行。"""
    from datetime import date, timedelta as days

    from app.blueprints.instantiate import instantiate
    from app.blueprints.templates.periodic_board import example
    from app.db.models_blueprints import Blueprint

    source = await s.register_source(s.make_source(tmp_path))
    bp = (await client.post("/api/blueprints", json={"source": {"kind": "template", "id": "periodic_board"},
                                                     "scope": "shared", "name": "门店周报"})).json()
    spec = example()
    spec["data"] = [{**spec["data"][0], "table": s.item({"source": source, "table": "orders"}),
                     "filter": s.item("全部"), "date": s.item({"column": "paid_at", "rule": "按支付日"})}]
    probe = instantiate(spec, name="门店周报", blueprint_id=bp["id"])
    fills = {j.id: ("SELECT ROUND(SUM(amount), 2) AS gmv FROM orders WHERE paid_at BETWEEN "
                    "'{{ input.period_start }}' AND '{{ input.period_end }}'" if j.kind == "sql"
                    else f"cell(nodes.{j.group}, 0, 'gmv')")
             for j in probe.joint_defs if j.kind in ("sql", "metric_expr")}
    filled = instantiate(spec, filled=fills, name="门店周报", blueprint_id=bp["id"])
    assert not [p for p in filled.problems if p["level"] == "error"], filled.problems
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp["id"])
        row.spec, row.joints = spec, filled.joints
        await session.commit()

    r = await client.post(f"/api/blueprints/{bp['id']}/trial", json={})
    assert r.status_code == 200, r.text
    run = await s.wait(r.json()["run_id"])
    assert run.status == "succeeded", run.error
    start = date.fromisoformat(run.input["period_start"])
    end = date.fromisoformat(run.input["period_end"])
    assert start.isoweekday() == 1 and end - start == days(days=6) and "-W" in run.input["period"]
    sql = next(n for n in run.graph["nodes"] if n["type"] == "tool")["data"]["config"]["args"]["sql"]
    assert sql.startswith("SELECT ROUND(SUM(amount), 2)")
    row = await s.blueprint(bp["id"])
    assert all(st["status"] == "filled" for st in row.joints.values())

    vm = (await client.get(f"/api/blueprints/{bp['id']}/preview")).json()
    stamp = find(vm, "stamp")[0]["data"]["stamp"]
    assert stamp["from"] == TRIAL_FROM and stamp["trial"] is True
    # 「换一期再试」的表单随预览给：看板类没有入口，是按条件查看的条件表单（图的输入，统计期在里面），界面照它画
    assert {"period_start", "period_end"} <= set(vm["trial"]["form"]["properties"]), vm["trial"]["form"]
    # 周期看板有趋势：试运行只有一个点，照实说没有趋势
    trends = [n["data"] for n in walk(vm["tree"]) if (n.get("data") or {}).get("note") == TRIAL_SERIES]
    assert trends and len(trends[0]["series"][0]["points"]) == 1


def test_a_period_that_cannot_be_worked_out_is_logged_not_swallowed(monkeypatch, caplog):
    """算不出统计期（卡片还不全、上下文缺了）时试运行只用用户给的输入——照实记一条日志，不悄悄吞掉。"""
    import logging
    from types import SimpleNamespace

    from app.blueprints import instantiate
    from app.blueprints.trial import trial_inputs

    def broken(spec):
        raise KeyError("records")

    monkeypatch.setattr(instantiate, "trial_inputs", broken)
    with caplog.at_level(logging.WARNING, logger="app.blueprints.trial"):
        out = trial_inputs(SimpleNamespace(id="bp-x", spec={}), {"period": "2026-W41", "empty": ""})
    assert out == {"period": "2026-W41"}
    assert any("bp-x" in r.getMessage() and r.exc_info for r in caplog.records)
