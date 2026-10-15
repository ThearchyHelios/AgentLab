"""个人应用（docs/design/normal-user-apps.md N6 的服务端部分、N7、§3.9）。

- 开关：apps.user_personal_apps 默认关；关着时使用者存个人应用、刷新、提交审核一律 403（说人话），搭建者及以上不受它管；
- 存：图要满足使用者的约束（同问数据的白名单：没有人工节点、不写入），要先试运行过，卡片有错的不存（问题带白话）；
  建出来的是 scope=personal、owner=拥有者的应用，固定的是蓝图（带图哈希），视图只取探索运行；再存一次是同一个应用；
- 刷新：以拥有者的身份发起探索运行（origin_kind=personal_app），强加 MASK_RESULTS / READ_ONLY，按人计每日上限（和按需
  视图共用设置项），交回剩余次数；正在刷新的接上那一次；输入按实例化时记下的定时任务输入模板算；
- 只有拥有者看得见；管理员只看得到它存在、能归档，看不到内容；拥有者能读自己的刷新，别人不能；个人应用不能发布、授权；
- 提交审核：要先成功刷新一次，只能交给搭建者；开一条 review 待办；派给的搭建者取得共享副本（copied_from），原个人应用
  不动；在待办里只能退回；副本发布后待办记为已发布，提交人收到通知；
- 解析：exploratory 来源取最近一次成功封存的刷新，印章写「探索 · 未经发布审核」；趋势按每次刷新排；共享应用里写了
  exploratory 的照实报错；隐藏的组件不下发，按草稿解析时带着 hidden；
- 页面编辑的候选：按固定版本列出能加的数据卡片，已经在页面上的不列，交回的组件能直接 app_add；
- 三条新检查：app.view_source_personal、app.hidden_bound、personal.policy，都有白话。
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.api.deps import Principal
from app.api.settings import APPS_GROUP
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import Run, Setting, Workflow, WorkflowVersion
from app.db.models_apps import App
from app.db.models_blueprints import Blueprint, BlueprintEvent
from app.db.models_process import Task, TaskAssignment
from app.engine.runner import run_manager
from tests.fakes import auth_env

# --------------------------------------------------------------------------
# 准备
# --------------------------------------------------------------------------


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


#: 只读数据、整理的看板：入口（统计周，有默认值）→ 数据整形 → 口径卡 → 成果。不调模型，跑得快
KPI = chain(
    node("start", "input", "开始", fields=[{"name": "week", "default": "2026-W41"}]),
    node("fetch", "transform", "取数", mode="expression", expression="{'gmv': 4321.5}", assign_to="kpi"),
    node("card", "metrics", "口径卡", caliber="周报口径", caliber_version="v1",
         metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]),
    node("out", "output", "成果", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))

HUMAN = chain(node("start", "input", "开始"), node("ok", "human", "主管审批", mode="approve"),
              node("out", "output", "成果"))
WRITE = chain(node("start", "input", "开始"), node("w", "tool", "写回系统", tool="http_request"),
              node("out", "output", "成果"))


def item(value):
    return {"value": value, "source": "user", "status": "confirmed"}


async def blueprint(owner: str | None, graph: dict | None = None, *, trialed: bool = True, purpose: str | None = "每周看销售额",
                    name: str = "我的周报", scope: str = "personal") -> str:
    graph = copy.deepcopy(KPI if graph is None else graph)
    spec = {"format": "agentlab.blueprint/1", "template": {"id": "freeform", "version": 1},
            "purpose": item(purpose) if purpose else {"value": None, "source": "assistant", "status": "missing"}}
    async with SessionLocal() as session:
        row = Blueprint(name=name, owner=owner, scope=scope, status="trialed" if trialed else "drafting", spec=spec,
                        template_id="freeform", template_version=1, source={"kind": "run"}, graph=graph,
                        graph_hash=graph_hash(graph), last_trial_run_id="trial-run" if trialed else None)
        session.add(row)
        await session.commit()
        return row.id


async def switch(on: bool, limit: int = 200) -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, APPS_GROUP)
        value = {"on_demand_daily_limit": limit, "user_personal_apps": on}
        if row is None:
            session.add(Setting(key=APPS_GROUP, value=value))
        else:
            row.value = value
        await session.commit()


async def people() -> tuple[AsyncClient, dict[str, AsyncClient]]:
    """开账号密码登录：管理员 root，使用者 uma、ula，搭建者 bob、bea。"""
    root = await auth_env.turn_on_local()
    for name, role in (("uma", "user"), ("ula", "user"), ("bob", "builder"), ("bea", "builder")):
        await auth_env.make_user(name, role)
    return root, {n: await auth_env.login(n) for n in ("uma", "ula", "bob", "bea")}


async def saved(client: AsyncClient, blueprint_id: str) -> str:
    r = await client.post(f"/api/blueprints/{blueprint_id}/save-personal")
    assert r.status_code == 200, r.text
    return r.json()["app_id"]


async def wait(run_id: str) -> Run:
    row = None
    for _ in range(600):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status if row else None}")


async def refreshed(client: AsyncClient, app_id: str) -> Run:
    r = await client.post(f"/api/apps/{app_id}/refresh")
    assert r.status_code == 200, r.text
    run = await wait(r.json()["run_id"])
    assert run.status == "succeeded", run.error
    return run


def walk(tree):
    for n in tree:
        yield n
        yield from walk(n.get("children") or [])


@pytest.fixture(autouse=True)
async def env():
    await auth_env.reset()
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == APPS_GROUP))
        # 每日上限按人计、按当天的刷新数：前面的测试留下的刷新不算进来
        await session.execute(delete(Run).where(Run.origin_kind == "personal_app"))
        await session.commit()
    yield
    await run_manager.shutdown()
    from app.notify.service import drain

    await drain()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key == APPS_GROUP))
        await session.commit()
    await auth_env.reset()


# --------------------------------------------------------------------------
# 开关
# --------------------------------------------------------------------------


async def test_the_switch_gates_users_but_not_builders():
    _, c = await people()
    uma_bp = await blueprint("uma")
    r = await c["uma"].post(f"/api/blueprints/{uma_bp}/save-personal")
    assert r.status_code == 403 and r.json()["code"] == "personal.off", r.text
    assert "使用者可以建个人应用" in r.json()["detail"] and "user_personal_apps" not in r.json()["detail"]
    assert (await c["uma"].get("/api/apps")).json()["can_create"] == {"shared": False, "personal": False}

    # 搭建者不受开关管（§3.12）
    assert (await c["bob"].get("/api/apps")).json()["can_create"] == {"shared": True, "personal": True}
    await saved(c["bob"], await blueprint("bob"))

    await switch(True)
    assert (await c["uma"].get("/api/apps")).json()["can_create"] == {"shared": False, "personal": True}
    app_id = await saved(c["uma"], uma_bp)

    await switch(False)
    assert (await c["uma"].post(f"/api/apps/{app_id}/refresh")).json()["code"] == "personal.off"
    r = await c["uma"].post(f"/api/apps/{app_id}/submit-review", json={"to": [{"kind": "user", "name": "bob"}]})
    assert r.status_code == 403 and r.json()["code"] == "personal.off"


# --------------------------------------------------------------------------
# 存成个人应用
# --------------------------------------------------------------------------


async def test_saving_checks_the_user_constraints_and_builds_a_personal_app():
    _, c = await people()
    await switch(True)
    uma = c["uma"]

    r = await uma.post(f"/api/blueprints/{await blueprint('uma', HUMAN)}/save-personal")
    assert r.status_code == 403 and r.json()["code"] == "personal.policy", r.text
    assert "使用者在个人应用中不能使用" in r.json()["detail"] and "「主管审批」" in r.json()["detail"]
    r = await uma.post(f"/api/blueprints/{await blueprint('uma', WRITE)}/save-personal")
    assert r.status_code == 403 and "「写回系统」" in r.json()["detail"], "写入（调用工具节点）一样不许"

    r = await uma.post(f"/api/blueprints/{await blueprint('uma', trialed=False)}/save-personal")
    assert r.status_code == 409 and r.json()["code"] == "personal.not_trialed"
    r = await uma.post(f"/api/blueprints/{await blueprint('uma', purpose=None)}/save-personal")
    assert r.status_code == 422 and r.json()["code"] == "bp.missing", r.text
    assert r.json()["issues"][0]["path"] == "/purpose" and r.json()["issues"][0]["plain"]["title"]

    bp = await blueprint("uma")
    assert (await c["ula"].post(f"/api/blueprints/{bp}/save-personal")).status_code == 404, "别人的蓝图当它不存在"
    r = await uma.post(f"/api/blueprints/{bp}/save-personal")
    assert r.status_code == 200, r.text
    app_id = r.json()["app_id"]
    assert r.json()["blueprint"]["status"] == "personal" and r.json()["blueprint"]["app_id"] == app_id

    detail = (await uma.get(f"/api/apps/{app_id}")).json()
    assert (detail["scope"], detail["owner"], detail["blueprint_id"], detail["my_role"]) == ("personal", "uma", bp,
                                                                                            "manage")
    assert detail["published_version"] is None and detail["published"] is None
    draft = detail["draft"]
    assert [(p["blueprint"], p["graph_hash"], p["workflow_id"], p["version"]) for p in draft["pins"]] == [
        (bp, graph_hash(KPI), None, None)]
    assert {v["source"]["kind"] for v in draft["views"].values()} == {"exploratory"}
    assert draft["entries"] == [] and draft["hooks"] == [] and draft["slots"] == {}
    types = {n["type"] for page in draft["pages"] for n in walk(page["body"])}
    assert "metric" in types and not types & {"entry_button", "instance_list", "instance_header"}
    assert detail["personal"]["remaining_today"] == 200 and detail["personal"]["graph_changed"] is False

    # 再存一次：同一个应用，页面上改过的留着
    draft["pages"][0]["title"] = "我改过的标题"
    assert (await uma.patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200
    again = await uma.post(f"/api/blueprints/{bp}/save-personal")
    assert again.json()["app_id"] == app_id
    assert (await uma.get(f"/api/apps/{app_id}")).json()["draft"]["pages"][0]["title"] == "我改过的标题"
    async with SessionLocal() as session:
        ops = list((await session.execute(select(BlueprintEvent.op).where(BlueprintEvent.blueprint_id == bp)
                                          .order_by(BlueprintEvent.seq))).scalars())
    assert ops == ["save_personal", "save_personal"]


async def test_after_the_blueprint_changes_refresh_waits_for_a_new_save():
    _, c = await people()
    await switch(True)
    bp = await blueprint("uma")
    app_id = await saved(c["uma"], bp)
    draft = (await c["uma"].get(f"/api/apps/{app_id}")).json()["draft"]
    draft["pages"][0]["title"] = "我改过的标题"
    assert (await c["uma"].patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200

    # 改了取数（节点还在，绑定都找得到）：没重新存之前不刷新；重新存之后页面上改过的留着
    changed = copy.deepcopy(KPI)
    changed["nodes"][1]["data"]["config"]["expression"] = "{'gmv': 99.0}"
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp)
        row.graph, row.graph_hash = changed, graph_hash(changed)
        await session.commit()
    assert (await c["uma"].get(f"/api/apps/{app_id}")).json()["personal"]["graph_changed"] is True
    r = await c["uma"].post(f"/api/apps/{app_id}/refresh")
    assert r.status_code == 409 and r.json()["code"] == "personal.graph_changed"
    await saved(c["uma"], bp)
    detail = (await c["uma"].get(f"/api/apps/{app_id}")).json()
    assert detail["draft"]["pins"][0]["graph_hash"] == graph_hash(changed) and detail["personal"]["graph_changed"] is False
    assert detail["draft"]["pages"][0]["title"] == "我改过的标题"
    run = await refreshed(c["uma"], app_id)
    assert run.origin["graph_hash"] == graph_hash(changed)

    # 口径卡换了（原来的绑定找不到了）：按新图重新派生，页面上不挂找不到数的块
    renamed = copy.deepcopy(changed)
    renamed["nodes"][2]["id"] = "card2"
    renamed["edges"] = [{"source": "fetch", "target": "card2"} if e["target"] == "card" else
                        {"source": "card2", "target": "out"} if e["source"] == "card" else e for e in renamed["edges"]]
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp)
        row.graph, row.graph_hash = renamed, graph_hash(renamed)
        await session.commit()
    await saved(c["uma"], bp)
    draft = (await c["uma"].get(f"/api/apps/{app_id}")).json()["draft"]
    metrics = [n["bind"]["metric"] for page in draft["pages"] for n in walk(page["body"]) if n["type"] == "metric"]
    assert metrics == ["card2.gmv"]


async def test_the_live_draft_only_takes_presentation_changes():
    _, c = await people()
    await switch(True)
    app_id = await saved(c["uma"], await blueprint("uma"))
    draft = (await c["uma"].get(f"/api/apps/{app_id}")).json()["draft"]

    moved = copy.deepcopy(draft)
    moved["pins"][0]["graph_hash"] = "0" * 64
    r = await c["uma"].patch(f"/api/apps/{app_id}", json={"draft": moved})
    assert r.status_code == 422 and "回到蓝图" in r.json()["detail"]
    formal = copy.deepcopy(draft)
    next(iter(formal["views"].values()))["source"] = {"kind": "latest_sealed", "pin": "main"}
    assert (await c["uma"].patch(f"/api/apps/{app_id}", json={"draft": formal})).status_code == 422
    entry = {**copy.deepcopy(draft), "entries": [{"id": "go", "label": "发起", "pin": "main", "form": {}}]}
    r = await c["uma"].patch(f"/api/apps/{app_id}", json={"draft": entry})
    assert r.status_code == 422 and "入口" in r.json()["detail"]

    # 个人应用不能分享，也没有发布
    r = await c["uma"].put(f"/api/apps/{app_id}/grants", json={"items": [
        {"principal_kind": "user", "principal": "ula", "role": "use"}]})
    assert r.status_code == 409 and r.json()["code"] == "personal.not_shareable"
    r = await c["uma"].post(f"/api/apps/{app_id}/publish", json={})
    assert r.status_code == 409 and r.json()["code"] == "personal.not_shareable"


# --------------------------------------------------------------------------
# 刷新
# --------------------------------------------------------------------------


async def test_refresh_is_masked_read_only_and_limited_per_person():
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    _, c = await people()
    await switch(True, limit=2)
    app_id = await saved(c["uma"], await blueprint("uma"))
    r = await c["uma"].post(f"/api/apps/{app_id}/refresh")
    assert r.status_code == 200 and r.json()["remaining"] == 1, r.text
    run = await wait(r.json()["run_id"])
    assert (run.status, run.run_class, run.origin_kind, run.started_by, run.app_id) == (
        "succeeded", "exploratory", "personal_app", "uma", app_id)
    assert run.options[MASK_RESULTS_OPTION] is True and run.options[READ_ONLY_OPTION] is True
    assert run.memory_scope == "chat:uma", "记忆固定为拥有者自己的范围（同问数据）"
    assert run.origin["graph_hash"] == graph_hash(KPI) and run.workflow_name == "我的周报"

    second = await c["uma"].post(f"/api/apps/{app_id}/refresh")
    assert second.json()["remaining"] == 0
    await wait(second.json()["run_id"])
    third = await c["uma"].post(f"/api/apps/{app_id}/refresh")
    assert third.status_code == 429 and third.json()["code"] == "personal.daily_limit", third.text
    assert (await c["uma"].get(f"/api/apps/{app_id}")).json()["personal"]["remaining_today"] == 0

    # 按人计：uma 用完了不影响 ula
    other = await saved(c["ula"], await blueprint("ula"))
    r = await c["ula"].post(f"/api/apps/{other}/refresh")
    assert r.status_code == 200 and r.json()["remaining"] == 1
    await wait(r.json()["run_id"])


async def test_a_refresh_in_flight_is_joined_and_inputs_follow_the_trial_rule(monkeypatch):
    import app.blueprints.instantiate as instantiate

    _, c = await people()
    await switch(True)
    bp = await blueprint("uma")
    app_id = await saved(c["uma"], bp)
    async with SessionLocal() as session:
        active = Run(workflow_name="我的周报", graph=KPI, status="running", origin_kind="personal_app",
                     origin={"graph_hash": graph_hash(KPI)}, app_id=app_id, started_by="uma")
        session.add(active)
        await session.commit()
        active_id = active.id
    left = (await c["uma"].get(f"/api/apps/{app_id}")).json()["personal"]["remaining_today"]
    assert left == 199, "正在运行的那一次算今天的一次"
    r = await c["uma"].post(f"/api/apps/{app_id}/refresh")
    assert r.json() == {"run_id": active_id, "remaining": left}, "正在刷新：接上那一次，不另花一次"
    async with SessionLocal() as session:
        (await session.get(Run, active_id)).status = "cancelled"
        await session.commit()

    # 输入和试运行同一个规则：按「多久更新」算出最近一期（blueprints/instantiate.trial_inputs）
    seen = []

    def latest_period(spec, **_):
        seen.append(spec.get("purpose", {}).get("value"))
        return {"week": "2026-W40"}

    monkeypatch.setattr(instantiate, "trial_inputs", latest_period)
    r = await c["uma"].post(f"/api/apps/{app_id}/refresh")
    run = await wait(r.json()["run_id"])
    assert run.input == {"week": "2026-W40"} and seen == ["每周看销售额"]


# --------------------------------------------------------------------------
# 谁看得见
# --------------------------------------------------------------------------


async def test_only_the_owner_sees_a_personal_app():
    from app.auth import run_readable

    _, c = await people()
    await switch(True)
    app_id = await saved(c["uma"], await blueprint("uma"))
    run = await refreshed(c["uma"], app_id)
    for who in ("ula", "bob"):
        assert (await c[who].get(f"/api/apps/{app_id}")).status_code == 404, who
        assert (await c[who].get(f"/api/apps/{app_id}/pages/home")).status_code == 404, who
        assert (await c[who].post(f"/api/apps/{app_id}/refresh")).status_code == 404, who
        assert (await c[who].post(f"/api/apps/{app_id}/archive")).status_code == 404, who
        assert app_id not in [a["id"] for a in (await c[who].get("/api/apps")).json()["items"]], who
    mine = (await c["uma"].get("/api/apps?scope=personal")).json()["items"]
    assert {a["scope"] for a in mine} == {"personal"} and {a["owner"] for a in mine} == {"uma"}
    assert next(a["my_role"] for a in mine if a["id"] == app_id) == "manage"
    assert app_id not in [a["id"] for a in (await c["uma"].get("/api/apps?scope=shared")).json()["items"]]

    def who(name: str) -> Principal:
        return Principal(username=name, role="user", verified=True, mode="local")

    assert await run_readable(who("uma"), run.id)
    assert not await run_readable(who("ula"), run.id), "别人读不了个人应用的刷新"
    assert (await c["uma"].get(f"/api/runs/{run.id}/evidence")).status_code == 200
    assert (await c["ula"].get(f"/api/runs/{run.id}/evidence")).status_code == 403


async def test_an_admin_sees_that_it_exists_and_can_archive_it_but_not_its_content():
    root, c = await people()
    await switch(True)
    app_id = await saved(c["uma"], await blueprint("uma"))
    await refreshed(c["uma"], app_id)
    for path in (f"/api/apps/{app_id}", f"/api/apps/{app_id}/pages/home", f"/api/apps/{app_id}/pages/home?draft=1",
                 f"/api/apps/{app_id}/candidates", f"/api/apps/{app_id}/versions"):
        assert (await root.get(path)).status_code == 404, path
    listed = next(a for a in (await root.get("/api/apps")).json()["items"] if a["id"] == app_id)
    assert (listed["owner"], listed["scope"], listed["my_role"], listed["description"], listed["name"]) == (
        "uma", "personal", None, "", "我的周报")
    r = await root.post(f"/api/apps/{app_id}/archive")
    assert r.status_code == 200 and r.json()["status"] == "archived", r.text
    assert "draft" not in r.json() and "personal" not in r.json()
    r = await c["uma"].post(f"/api/apps/{app_id}/refresh")
    assert r.status_code == 409 and "已归档" in r.json()["detail"]


# --------------------------------------------------------------------------
# 提交审核、共享副本
# --------------------------------------------------------------------------


async def _user_id(name: str) -> str:
    from app.db.models import User

    async with SessionLocal() as session:
        return await session.scalar(select(User.id).where(User.username == name))


async def test_review_gives_the_builder_a_shared_copy_and_leaves_the_original(monkeypatch):
    import app.notify as notify
    from app.blueprints.personal import review_published
    from app.core.crypto import encrypt
    from app.db.models_process import NotifyChannel
    from app.notify.core import Outcome
    from app.notify.service import drain

    _, c = await people()
    await switch(True)
    bp = await blueprint("uma")
    app_id = await saved(c["uma"], bp)
    to_bob = {"to": [{"kind": "user", "name": "bob"}], "note": "请帮我做成正式的周报"}
    r = await c["uma"].post(f"/api/apps/{app_id}/submit-review", json=to_bob)
    assert r.status_code == 409 and r.json()["code"] == "personal.no_run", "先成功刷新一次"
    run = await refreshed(c["uma"], app_id)
    r = await c["uma"].post(f"/api/apps/{app_id}/submit-review", json={"to": [{"kind": "user", "name": "ula"}]})
    assert r.status_code == 422 and r.json()["code"] == "personal.review_target", "只能交给搭建者"
    r = await c["uma"].post(f"/api/apps/{app_id}/submit-review", json=to_bob)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    again = await c["uma"].post(f"/api/apps/{app_id}/submit-review", json=to_bob)
    assert again.status_code == 409 and again.json()["code"] == "personal.in_review"

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert (task.kind, task.run_id, task.app_id, task.status) == ("review", run.id, app_id, "pending")
        rows = list((await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task_id))).scalars())
        assert [(a.principal_kind, a.principal, a.source) for a in rows] == [("user", "bob", "review")]
        assert (await session.get(Blueprint, bp)).status == "in_review"
        before = copy.deepcopy((await session.get(App, app_id)).draft)
    inbox = (await c["bob"].get("/api/tasks")).json()["items"]
    mine = next(t for t in inbox if t["id"] == task_id)
    assert mine["payload"]["kind"] == "review" and mine["payload"]["submitted_by"] == "uma"
    assert mine["payload"]["note"] == "请帮我做成正式的周报" and mine["can_respond"]

    assert (await c["bea"].post(f"/api/blueprints/{bp}/copy")).status_code == 404, "没派给她的搭建者取不到"
    assert (await c["uma"].post(f"/api/blueprints/{bp}/copy")).status_code == 403
    r = await c["bob"].post(f"/api/blueprints/{bp}/copy")
    assert r.status_code == 201, r.text
    dup = r.json()
    assert (dup["copied_from"], dup["scope"], dup["owner"], dup["status"]) == (bp, "shared", "bob", "drafting")
    assert dup["graph_hash"] == graph_hash(KPI) and dup["spec"]["purpose"]["value"] == "每周看销售额"
    assert (await c["bob"].post(f"/api/blueprints/{bp}/copy")).json()["id"] == dup["id"], "再取是同一份"
    async with SessionLocal() as session:
        original = await session.get(Blueprint, bp)
        assert original.status == "in_review" and original.scope == "personal" and original.graph == KPI
        assert (await session.get(App, app_id)).draft == before, "原个人应用不动"
    assert any(cp["id"] == dup["id"] for cp in (await c["bob"].get(f"/api/tasks/{task_id}")).json()["payload"]["copies"])

    # 在待办里不能同意：要在副本上走发布单
    r = await c["bob"].post(f"/api/tasks/{task_id}/respond", json={"decision": "approve"})
    assert r.status_code == 422 and "共享副本" in r.json()["detail"], r.text

    # 发布单做完：待办记为已发布，提交人收到通知（他自己订阅了的个人渠道）
    sent = []

    async def fake_send(channel, message):
        sent.append((channel.name, message))
        return Outcome(True, 200, 1, None)

    monkeypatch.setattr(notify, "send", fake_send)
    async with SessionLocal() as session:
        session.add(NotifyChannel(name="uma 的手机", scope="user", owner_id=await _user_id("uma"),
                                  url=encrypt("https://example.com/hook"), events=["review.done"]))
        await session.commit()
        copy_row = await session.get(Blueprint, dup["id"])
        bob = Principal(username="bob", role="builder", verified=True, mode="local")
        assert await review_published(session, copy_row, app_id="formal-app", principal=bob) == task_id
    await drain()
    assert [(name, m["title"]) for name, m in sent] == [("uma 的手机", "提交审核的「我的周报」已发布成正式应用")]
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.status == "resolved" and task.outcome["decision"] == "published"
        assert task.outcome["app_id"] == "formal-app" and task.resolved_by == "bob"
        assert (await session.get(Blueprint, bp)).status == "personal"
    review = (await c["uma"].get(f"/api/apps/{app_id}")).json()["personal"]["review"]
    assert (review["decision"], review["app_id"], review["status"]) == ("published", "formal-app", "resolved")


async def test_the_reviewer_can_send_it_back():
    from app.db.models import Group, GroupMember

    _, c = await people()
    await switch(True)
    async with SessionLocal() as session:
        group = Group(name="搭建者组")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=await _user_id("bob")))
        await session.commit()
    bp = await blueprint("uma")
    app_id = await saved(c["uma"], bp)
    await refreshed(c["uma"], app_id)
    r = await c["uma"].post(f"/api/apps/{app_id}/submit-review", json={"to": [{"kind": "group", "name": "搭建者组"}]})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    assert (await c["bea"].post(f"/api/tasks/{task_id}/respond", json={"decision": "reject"})).status_code == 403
    r = await c["bob"].post(f"/api/tasks/{task_id}/respond", json={"decision": "reject", "note": "口径要先和财务对一下"})
    assert r.status_code == 200 and r.json()["resolved"], r.text
    review = (await c["uma"].get(f"/api/apps/{app_id}")).json()["personal"]
    assert review["blueprint_status"] == "personal"
    assert (review["review"]["decision"], review["review"]["note"]) == ("reject", "口径要先和财务对一下")
    assert (await c["uma"].post(f"/api/apps/{app_id}/submit-review", json={
        "to": [{"kind": "user", "name": "bob"}]})).status_code == 200, "退回之后可以再提交"


# --------------------------------------------------------------------------
# 解析：探索来源、印章、隐藏
# --------------------------------------------------------------------------


async def test_the_page_reads_the_latest_refresh_and_stamps_it_as_exploratory():
    _, c = await people()
    await switch(True)
    app_id = await saved(c["uma"], await blueprint("uma"))
    page = (await c["uma"].get(f"/api/apps/{app_id}/pages/home")).json()
    view = next(iter(page["sources"]))
    assert page["sources"][view]["kind"] == "exploratory" and page["sources"][view]["state"] == "empty"
    assert "刷新" in page["sources"][view]["message"] and page["problems"] == []
    assert page["app"]["needs_upgrade"] is False

    first = await refreshed(c["uma"], app_id)
    page = (await c["uma"].get(f"/api/apps/{app_id}/pages/home")).json()
    source = page["sources"][view]
    assert (source["state"], source["run_id"]) == ("ok", first.id)
    assert source["stamp"]["from"] == "探索 · 未经发布审核" and source["stamp"]["pin"] == "我的周报"
    metric = next(n for n in walk(page["tree"]) if n["type"] == "metric")
    assert metric["data"]["state"] == "ok" and metric["data"]["evidence"]["run_id"] == first.id

    # 趋势按每次刷新排（页面上加一块趋势是改呈现，可以直接改）
    draft = (await c["uma"].get(f"/api/apps/{app_id}")).json()["draft"]
    draft["pages"][0]["body"].append({"id": "trend", "type": "series", "bind": {"view": view, "series": "card.gmv"}})
    assert (await c["uma"].patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200
    second = await refreshed(c["uma"], app_id)
    page = (await c["uma"].get(f"/api/apps/{app_id}/pages/home")).json()
    trend = next(n for n in walk(page["tree"]) if n["id"] == "trend")["data"]
    assert [p["run_id"] for p in trend["series"][0]["points"]] == [first.id, second.id]
    assert trend["series"][0]["label"] == "每次刷新" and trend["state"] == "ok"

    # 最近一次刷新没成功：给上一次的结果，并写明
    async with SessionLocal() as session:
        session.add(Run(workflow_name="我的周报", graph=KPI, status="failed", origin_kind="personal_app",
                        origin={"graph_hash": graph_hash(KPI)}, app_id=app_id, started_by="uma",
                        finished_at=utcnow() + timedelta(seconds=1)))
        await session.commit()
    source = (await c["uma"].get(f"/api/apps/{app_id}/pages/home")).json()["sources"][view]
    assert source["run_id"] == second.id and "没有成功" in source["message"]
    assert any("没有成功" in note for note in source["stamp"]["notes"])


async def test_hidden_components_are_not_sent_but_the_editor_sees_them():
    _, c = await people()
    await switch(True)
    app_id = await saved(c["uma"], await blueprint("uma"))
    await refreshed(c["uma"], app_id)
    draft = (await c["uma"].get(f"/api/apps/{app_id}")).json()["draft"]
    section = next(n for n in draft["pages"][0]["body"] if n["type"] == "section")
    section["hidden"] = True
    assert (await c["uma"].patch(f"/api/apps/{app_id}", json={"draft": draft})).status_code == 200
    page = (await c["uma"].get(f"/api/apps/{app_id}/pages/home")).json()
    ids = {n["id"] for n in walk(page["tree"])}
    assert section["id"] not in ids and not any(n["type"] == "metric" for n in walk(page["tree"])), "里面的也不下发"
    editor = (await c["uma"].get(f"/api/apps/{app_id}/pages/home?draft=1")).json()
    shown = next(n for n in walk(editor["tree"]) if n["id"] == section["id"])
    assert shown["hidden"] is True and any(n["type"] == "metric" for n in walk(shown["children"]))


async def test_hidden_components_are_skipped_in_shared_apps_too():
    """关登录：共享应用里隐藏了的组件同样不下发；只被它绑定的视图不取运行。"""
    wf, hash_ = await _workflow(KPI)
    spec = {"format": "agentlab.app/1", "name": "周报",
            "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": hash_, "use": "view"}],
            "views": {"latest": {"source": {"kind": "latest_sealed", "pin": "main"}},
                      "ask": {"source": {"kind": "on_demand", "pin": "main", "auto": True, "ttl_s": 60}}},
            "pages": [{"id": "home", "title": "首页", "body": [
                {"id": "stamp", "type": "stamp", "bind": {"view": "latest"}},
                {"id": "gone", "type": "metric", "hidden": True, "bind": {"view": "ask", "metric": "card.gmv"}}]}]}
    async with _client() as client:
        app_id = (await client.post("/api/apps", json={"name": "周报", "spec": spec})).json()["id"]
        out = (await client.post(f"/api/apps/{app_id}/publish", json={})).json()
        assert out["ok"], out
        assert "app.hidden_bound" in {i["code"] for i in out["issues"]}
        page = (await client.get(f"/api/apps/{app_id}/pages/home")).json()
        assert [n["id"] for n in page["tree"]] == ["stamp"] and set(page["sources"]) == {"latest"}, \
            "按需视图只被隐藏的组件绑定：不取数、不自动发起"
    async with SessionLocal() as session:
        assert await session.scalar(select(Run.id).where(Run.app_id == app_id)) is None


# --------------------------------------------------------------------------
# 页面编辑的候选
# --------------------------------------------------------------------------


def _client() -> AsyncClient:
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _workflow(graph: dict) -> tuple[str, str]:
    async with SessionLocal() as session:
        wf = Workflow(name=f"周报-{uuid.uuid4().hex[:6]}", graph=graph, status="published", published_version=1,
                      version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, graph_hash=graph_hash(graph),
                                    level="published", published_at=utcnow()))
        await session.commit()
        return wf.id, graph_hash(graph)


BOARD = chain(
    node("start", "input", "开始", fields=[{"name": "week", "default": "2026-W41"}]),
    node("q", "tool", "查订单", tool="db_query__shop", args={"sql": "SELECT SUM(amount) AS gmv FROM orders"}),
    node("card", "metrics", "核心指标", caliber="周报口径", caliber_version="v1",
         metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.q, 0, 'gmv')"},
                  {"id": "orders", "name": "订单数", "expression": "cell(nodes.q, 0, 'gmv')"}]),
    node("rep", "report", "周报"),
    node("out", "output", "成果"))


async def test_candidates_list_what_can_be_added_from_the_pinned_version():
    from app.apps.lint import lint_app
    from app.apps.ops import apply_app_op

    wf, _ = await _workflow(BOARD)
    async with _client() as client:
        spec = (await client.post("/api/apps/derive", json={"workflow_id": wf, "version": 1})).json()["spec"]
        # 去掉派生出来的表格和一个指标：它们就该出现在候选里
        home = spec["pages"][0]["body"]
        spec["pages"][0]["body"] = [n for n in home if n["type"] != "table"]
        cols = next(n for n in walk(spec["pages"][0]["body"]) if n["type"] == "columns")
        cols["children"] = [m for m in cols["children"] if m["bind"]["metric"] != "card.orders"]
        app_id = (await client.post("/api/apps", json={"name": "周报", "spec": spec})).json()["id"]
        items = (await client.get(f"/api/apps/{app_id}/candidates")).json()["items"]
    got = {(i["kind"], i["label"], i["group"]) for i in items}
    assert got == {("metric", "订单数", "核心指标"), ("series", "销售额的趋势", "核心指标"),
                   ("series", "订单数的趋势", "核心指标"), ("table", "查订单", "查订单")}, got
    assert not any(i["kind"] == "report" for i in items), "报告块已经在页面上"
    for cand in items:
        draft = copy.deepcopy(spec)
        assert apply_app_op(draft, {"op": "app_add", "parent": "home", "component": cand["component"]}).changed
        async with SessionLocal() as session:
            errors = [i for i in await lint_app(session, draft, {}) if i["level"] == "error"]
        assert errors == [], (cand, errors)


async def test_candidates_need_manage_and_work_for_personal_apps():
    _, c = await people()
    await switch(True)
    app_id = await saved(c["uma"], await blueprint("uma"))
    items = (await c["uma"].get(f"/api/apps/{app_id}/candidates")).json()["items"]
    assert [(i["kind"], i["component"]["bind"]["series"]) for i in items] == [("series", "card.gmv")]
    assert (await c["ula"].get(f"/api/apps/{app_id}/candidates")).status_code == 404

    wf, _ = await _workflow(BOARD)
    spec = (await c["bob"].post("/api/apps/derive", json={"workflow_id": wf, "version": 1})).json()["spec"]
    shared = (await c["bob"].post("/api/apps", json={"name": "周报", "spec": spec})).json()["id"]
    r = await c["bob"].put(f"/api/apps/{shared}/grants", json={"items": [
        {"principal_kind": "user", "principal": "ula", "role": "use"}]})
    assert r.status_code == 200, r.text
    r = await c["ula"].get(f"/api/apps/{shared}/candidates")
    assert r.status_code == 403, "只有使用权限的人不能改页面"


# --------------------------------------------------------------------------
# 三条新检查
# --------------------------------------------------------------------------


async def test_the_three_new_checks_and_their_plain_wording():
    from app.apps.lint import lint_app
    from app.plain import plain_of, with_plain

    def personal(bp_id: str, graph: dict, **extra) -> dict:
        return {"format": "agentlab.app/1", "name": "我的",
                "pins": [{"alias": "main", "blueprint": bp_id, "graph_hash": graph_hash(graph), "use": "view"}],
                "views": {"v": {"source": {"kind": "exploratory", "pin": "main"}}},
                "pages": [{"id": "home", "title": "首页", "body": [{"id": "s", "type": "stamp", "bind": {"view": "v"}}]}],
                **extra}

    def codes(issues, level=None):
        return {i["code"] for i in issues if level is None or i["level"] == level}

    wf, hash_ = await _workflow(KPI)
    shared = {"format": "agentlab.app/1", "name": "周报",
              "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": hash_}],
              "views": {"v": {"source": {"kind": "exploratory", "pin": "main"}}},
              "pages": [{"id": "home", "title": "首页", "body": [{"id": "s", "type": "stamp", "bind": {"view": "v"}}]}]}
    ok_bp = await blueprint("uma")
    human_bp = await blueprint("uma", HUMAN)
    async with SessionLocal() as session:
        assert "app.view_source_personal" in codes(await lint_app(session, shared, {}), "error"), "共享应用不能取探索运行"
        assert codes(await lint_app(session, personal(ok_bp, KPI), {}, scope="personal"), "error") == set()
        formal = personal(ok_bp, KPI)
        formal["views"]["v"]["source"] = {"kind": "latest_sealed", "pin": "main"}
        assert "app.view_source_personal" in codes(await lint_app(session, formal, {}, scope="personal"), "error")
        assert "app.pin_unpublished" in codes(await lint_app(session, personal(ok_bp, KPI), {}), "error"), \
            "共享应用不能固定蓝图"

        issues = await lint_app(session, personal(human_bp, HUMAN), {}, scope="personal")
        policy = [i for i in issues if i["code"] == "personal.policy"]
        assert policy and policy[0]["level"] == "error" and "「主管审批」" in policy[0]["message"]
        with_entry = personal(ok_bp, KPI, entries=[{"id": "go", "label": "发起", "pin": "main", "form": {}}])
        assert any("入口" in i["message"] for i in await lint_app(session, with_entry, {}, scope="personal")
                   if i["code"] == "personal.policy")

        hidden = personal(ok_bp, KPI)
        hidden["pages"][0]["body"][0]["hidden"] = True
        bound = [i for i in await lint_app(session, hidden, {}, scope="personal") if i["code"] == "app.hidden_bound"]
        assert [(i["level"], i["path"]) for i in bound] == [("warning", "pages[0].body[0].hidden")]
        inside = personal(ok_bp, KPI)
        inside["pages"][0]["body"] = [{"id": "box", "type": "section", "hidden": True,
                                       "children": [{"id": "s", "type": "stamp", "bind": {"view": "v"}}]}]
        assert "app.hidden_bound" in codes(await lint_app(session, inside, {}, scope="personal"), "warning"), \
            "放在隐藏的布局组件里也算"
        twice = personal(ok_bp, KPI)
        twice["pages"][0]["body"].append({"id": "s2", "type": "stamp", "bind": {"view": "v"}, "hidden": False})
        twice["pages"][0]["body"][0]["hidden"] = True
        assert "app.hidden_bound" not in codes(await lint_app(session, twice, {}, scope="personal"))

    for code in ("app.view_source_personal", "app.hidden_bound", "personal.policy"):
        assert plain_of(code) is not None, code
        assert with_plain({"code": code})["plain"]["who"] == "you", code


# --------------------------------------------------------------------------
# 复核修复：蓝图对话、审核待办补收、个人蓝图不建记录表、认人接口带开关
# --------------------------------------------------------------------------


async def test_a_users_blueprint_conversation_follows_the_blueprint():
    """使用者没开问数据也读得到自己蓝图的对话（按蓝图判，不走问数据的路由）；别人的个人蓝图的对话，搭建者也读不到、
    列表里也不列。认人接口带上个人应用的开关，新建应用的入口按它给。"""
    _, c = await people()
    assert (await c["uma"].get("/api/auth/me")).json()["user_personal_apps"] is False
    await switch(True)
    assert (await c["uma"].get("/api/auth/me")).json()["user_personal_apps"] is True
    r = await c["uma"].post("/api/blueprints", json={"source": {"kind": "conversation"}, "scope": "personal",
                                                     "name": "我的退款周报"})
    assert r.status_code == 201, r.text
    bp = r.json()
    got = await c["uma"].get(f"/api/blueprints/{bp['id']}/conversation")
    assert got.status_code == 200 and got.json()["id"] == bp["conversation_id"], got.text
    assert (await c["uma"].get(f"/api/conversations/{bp['conversation_id']}")).status_code == 403, "问数据没开"
    assert (await c["bob"].get(f"/api/conversations/{bp['conversation_id']}")).status_code == 404
    assert (await c["bob"].get(f"/api/blueprints/{bp['id']}/conversation")).status_code == 404
    listed = (await c["bob"].get("/api/conversations", params={"kind": "blueprint"})).json()
    assert bp["conversation_id"] not in {x["id"] for x in listed}


async def test_a_review_left_open_by_a_failed_close_is_settled_when_someone_looks(monkeypatch):
    """副本已经发布、原蓝图的审核待办却还挂着（发布单做完时没能结束它）：拥有者打开个人应用时补上，记为已发布、处理人是
    发布那份副本的人，原蓝图回到「个人应用」。"""
    _, c = await people()
    await switch(True)
    bp = await blueprint("uma")
    app_id = await saved(c["uma"], bp)
    await refreshed(c["uma"], app_id)
    r = await c["uma"].post(f"/api/apps/{app_id}/submit-review", json={"to": [{"kind": "user", "name": "bob"}]})
    task_id = r.json()["task_id"]
    dup = (await c["bob"].post(f"/api/blueprints/{bp}/copy")).json()
    async with SessionLocal() as session:
        copy_row = await session.get(Blueprint, dup["id"])
        copy_row.status, copy_row.app_id = "published", "formal-app"
        await session.commit()
        assert (await session.get(Task, task_id)).status == "pending"
    state = (await c["uma"].get(f"/api/apps/{app_id}")).json()["personal"]
    assert state["blueprint_status"] == "personal"
    assert (state["review"]["decision"], state["review"]["app_id"]) == ("published", "formal-app")
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
    assert task.status == "resolved" and task.resolved_by == "bob"
    from app.notify.service import drain

    await drain()


async def test_a_personal_blueprint_never_creates_a_records_table():
    """填报登记不能做成个人应用（只读，建了表也没人写）：个人蓝图检查报 bp.template_mismatch；实例化、填关节这些地方也不替
    个人蓝图建记录表、登记全局数据源。"""
    from app.blueprints.ops import blueprint_issues
    from app.blueprints.store import instantiate_context
    from app.blueprints.templates._common import EXTRAS
    from app.db.models import DataSource
    from app.db.models_blueprints import RecordsTable

    spec = EXTRAS["records"].example()
    assert any(i["code"] == "bp.template_mismatch" and i["path"] == "/template"
               for i in blueprint_issues(spec, for_="personal"))
    async with SessionLocal() as session:
        row = Blueprint(name="我的报损", owner="uma", scope="personal", status="drafting", spec=spec,
                        template_id="records", template_version=1, source={"kind": "template"})
        session.add(row)
        await session.flush()
        sources = int(await session.scalar(select(func.count(DataSource.id))) or 0)
        ctx = await instantiate_context(session, row, create_records=True)
        await session.commit()
        assert "records" not in ctx
        assert not (await session.execute(select(RecordsTable).where(RecordsTable.table_key == row.id))).first()
        assert int(await session.scalar(select(func.count(DataSource.id))) or 0) == sources
