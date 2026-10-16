"""使用者选人：按用途给的候选池 + 开登录时服务端核对（docs/design/next-round/05-已知遗留.md (b)）。

以前使用者在入口表单、待办表单的选人控件里没有候选（名录只给搭建者），服务端也不核对：填错的名字会把待办派给一个不存在
的人，没人能处理，也不报错。现在：
- GET /api/directory/candidates 按用途给候选池，只有用户名和显示名，停用的不列；form 用途缺省是能用这个应用的人（使用
  及以上的授权展开组成员，加上管理员和建应用的人），字段可以用 x-principal 声明别的池；应用授权给了所有人时不带前缀不列；
  调用的人不能用这个应用时 403；review 用途是启用的搭建者和管理员；
- 开了登录时，入口表单和待办表单的选人控件按同一个池核对，不在池里 422（errors 的 code=principal）；关登录照旧手填；
- /api/directory 仍然只给搭建者。

名字都是虚构的。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from app.apps.forms import validate_form_schema
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import App, AppGrant, Group, GroupMember, Run, Task, User, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from tests.fakes import auth_env


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


PICK = {"type": "string", "title": "审批人", "x-widget": "principal"}

GRAPH = chain(
    node("start", "input", "开始", fields=[{"name": "approver"}, {"name": "reviewer"}]),
    node("review", "human", "复核", mode="input", title="请指定复核人",
         form={"type": "object", "properties": {"reviewer": {**PICK, "title": "复核人"}}}),
    node("out", "output", "成果", fields=[{"name": "r", "value": "{{ input.approver }}"}]))


async def _workflow() -> tuple[str, str]:
    async with SessionLocal() as session:
        wf = Workflow(name=f"选人-{uuid.uuid4().hex[:6]}", graph=GRAPH, status="published", published_version=1,
                      version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=GRAPH, graph_hash=graph_hash(GRAPH),
                                    level="published", published_at=utcnow()))
        await session.commit()
        return wf.id, graph_hash(GRAPH)


def _spec(wf: str, hash_: str, approver: dict | None = None) -> dict:
    return {
        "format": "agentlab.app/1", "name": "选人",
        "pins": [{"alias": "main", "workflow_id": wf, "version": 1, "graph_hash": hash_, "use": "entry"}],
        "entries": [{"id": "apply", "label": "发起", "pin": "main", "form": {"$ref": "#/forms/apply"},
                     "map": {"approver": "form.approver"}}],
        "forms": {"apply": {"type": "object", "properties": {"approver": approver or PICK}}},
        "pages": [{"id": "home", "title": "首页", "body": [
            {"id": "new", "type": "entry_button", "props": {"entry": "apply"}}]}],
    }


async def _new_app(root, spec: dict) -> str:
    """root 建一个应用并发布，授权使用给 uma、停用的 dan 和组「财务」。"""
    res = await root.post("/api/apps", json={"name": f"选人-{uuid.uuid4().hex[:4]}", "spec": spec})
    assert res.status_code == 201, res.text
    app_id = res.json()["id"]
    published = (await root.post(f"/api/apps/{app_id}/publish", json={})).json()
    assert published["ok"], published
    async with SessionLocal() as session:
        session.add_all([AppGrant(app_id=app_id, principal_kind="user", principal="uma", role="use"),
                         AppGrant(app_id=app_id, principal_kind="user", principal="dan", role="use"),
                         AppGrant(app_id=app_id, principal_kind="group", principal="财务", role="use")])
        await session.commit()
    return app_id


@pytest.fixture
async def world():
    """开账号密码登录：管理员 root（建应用）、搭建者 bob、使用者 uma（授权使用）、ulf（没授权）、停用的 dan（授权使用）；
    组「财务」（fay、fin），整组授权使用。"""
    await auth_env.reset()
    root = await auth_env.turn_on_local("root")
    try:
        await auth_env.make_user("bob", "builder")
        await auth_env.make_user("uma", "user")
        await auth_env.make_user("ulf", "user")
        await auth_env.make_user("dan", "user", disabled=True)
        fay = await auth_env.make_user("fay", "user")
        fin = await auth_env.make_user("fin", "user")
        async with SessionLocal() as session:
            for username, shown in (("uma", "乌玛"), ("fay", "费伊"), ("bob", "鲍勃")):
                user = (await session.execute(select(User).where(User.username == username))).scalar_one()
                user.display_name = shown
            group = Group(name="财务", label="财务组", source="local")
            session.add(group)
            await session.flush()
            session.add_all([GroupMember(group_id=group.id, user_id=fay.id), GroupMember(group_id=group.id, user_id=fin.id)])
            await session.commit()
        wf, hash_ = await _workflow()
        app_id = await _new_app(root, _spec(wf, hash_))
        yield {"root": root, "app_id": app_id, "wf": wf, "hash": hash_}
    finally:
        await root.aclose()
        async with SessionLocal() as session:
            for model in (AppGrant, App):
                await session.execute(delete(model))
            await session.commit()
        await auth_env.reset()


def _params(app_id: str, **more) -> dict:
    return {"purpose": "form", "app_id": app_id, "entry": "apply", "field": "approver", **more}


async def test_users_get_the_people_who_can_use_the_app_and_only_their_names(world):
    uma = await auth_env.login("uma")
    r = await uma.get("/api/directory/candidates", params=_params(world["app_id"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [u["username"] for u in body["users"]] == ["fay", "fin", "root", "uma"]
    assert all(set(u) == {"username", "display_name"} for u in body["users"]), "不给停用、角色、邮箱"
    assert {u["username"]: u["display_name"] for u in body["users"]}["uma"] == "乌玛"
    assert body["groups"] == [{"name": "财务", "label": "财务组"}] and body["truncated"] is False
    # 前缀按用户名或显示名筛
    assert [u["username"] for u in (await uma.get("/api/directory/candidates",
                                                  params=_params(world["app_id"], q="费"))).json()["users"]] == ["fay"]
    # 名录整份仍然只给搭建者
    assert (await uma.get("/api/directory")).status_code == 403
    # 不能用这个应用的人拿不到它的候选
    ulf = await auth_env.login("ulf")
    assert (await ulf.get("/api/directory/candidates", params=_params(world["app_id"]))).status_code == 403
    for c in (uma, ulf):
        await c.aclose()


async def test_an_app_granted_to_everyone_needs_a_prefix(world):
    async with SessionLocal() as session:
        session.add(AppGrant(app_id=world["app_id"], principal_kind="everyone", principal="", role="use"))
        await session.commit()
    ulf = await auth_env.login("ulf")
    body = (await ulf.get("/api/directory/candidates", params=_params(world["app_id"]))).json()
    assert body["users"] == [] and body["needs_query"] is True
    found = (await ulf.get("/api/directory/candidates", params=_params(world["app_id"], q="u"))).json()
    assert [u["username"] for u in found["users"]] == ["ulf", "uma"], "停用的 dan 不列"
    capped = (await ulf.get("/api/directory/candidates", params=_params(world["app_id"], q="f", limit=1))).json()
    assert len(capped["users"]) == 1 and capped["truncated"] is True
    await ulf.aclose()


async def test_review_candidates_are_builders_and_admins(world):
    uma = await auth_env.login("uma")
    body = (await uma.get("/api/directory/candidates", params={"purpose": "review"})).json()
    assert [u["username"] for u in body["users"]] == ["bob", "root"] and body["groups"] == []
    assert (await uma.get("/api/directory/candidates", params={"purpose": "nope"})).status_code == 422
    await uma.aclose()


async def test_starting_with_a_name_outside_the_pool_is_refused(world):
    uma = await auth_env.login("uma")
    url = f"/api/apps/{world['app_id']}/entries/apply"
    bad = await uma.post(url, json={"values": {"approver": "wangwu"}})
    assert bad.status_code == 422, bad.text
    [error] = bad.json()["errors"]
    assert (error["path"], error["code"]) == ("approver", "principal")
    assert error["message"].startswith("「wangwu」不在可选的人里")
    disabled = await uma.post(url, json={"values": {"approver": "dan"}})
    assert disabled.status_code == 422 and disabled.json()["errors"][0]["code"] == "principal"
    for ok in ("fay", "财务"):
        good = await uma.post(url, json={"values": {"approver": ok}})
        assert good.status_code == 201, good.text
    await uma.aclose()


async def test_a_task_form_is_checked_against_the_same_pool(world):
    uma = await auth_env.login("uma")
    started = await uma.post(f"/api/apps/{world['app_id']}/entries/apply", json={"values": {"approver": "fay"}})
    assert started.status_code == 201, started.text
    run_id = started.json()["run_id"]
    task = None
    for _ in range(200):
        async with SessionLocal() as session:
            task = (await session.execute(select(Task).where(Task.run_id == run_id, Task.status == "pending")))\
                .scalars().first()
        if task is not None:
            break
        await asyncio.sleep(0.05)
    assert task is not None
    cands = await uma.get("/api/directory/candidates", params={"purpose": "form", "task_id": task.id,
                                                               "field": "reviewer"})
    assert cands.status_code == 200 and "fin" in [u["username"] for u in cands.json()["users"]]
    root = world["root"]
    bad = await root.post(f"/api/tasks/{task.id}/respond", json={"decision": "submit", "values": {"reviewer": "wangwu"}})
    assert bad.status_code == 422, bad.text
    assert bad.json()["errors"][0]["code"] == "principal"
    good = await root.post(f"/api/tasks/{task.id}/respond", json={"decision": "submit", "values": {"reviewer": "fin"}})
    assert good.status_code == 200, good.text
    await uma.aclose()


async def test_a_declared_pool_narrows_the_candidates(world):
    """x-principal 声明了 group:财务：候选只有组里的人，提交组外的人也拒。"""
    app_id = await _new_app(world["root"], _spec(world["wf"], world["hash"],
                                                {**PICK, "x-principal": {"pool": "group:财务"}}))
    uma = await auth_env.login("uma")
    body = (await uma.get("/api/directory/candidates", params=_params(app_id))).json()
    assert [u["username"] for u in body["users"]] == ["fay", "fin"] and body["groups"] == []
    refused = await uma.post(f"/api/apps/{app_id}/entries/apply", json={"values": {"approver": "uma"}})
    assert refused.status_code == 422 and refused.json()["errors"][0]["code"] == "principal"
    await uma.aclose()


async def test_login_off_takes_any_name():
    await auth_env.reset()
    wf, hash_ = await _workflow()
    from httpx import ASGITransport, AsyncClient

    from app.main import app as asgi

    async with AsyncClient(transport=ASGITransport(app=asgi), base_url="http://test") as c:
        res = await c.post("/api/apps", json={"name": f"关登录-{uuid.uuid4().hex[:4]}", "spec": _spec(wf, hash_)})
        app_id = res.json()["id"]
        assert (await c.post(f"/api/apps/{app_id}/publish", json={})).json()["ok"]
        started = await c.post(f"/api/apps/{app_id}/entries/apply", json={"values": {"approver": "随手填的名字"}})
        assert started.status_code == 201, started.text
    async with SessionLocal() as session:
        await session.execute(delete(Run).where(Run.id == started.json()["run_id"]))
        await session.execute(delete(App).where(App.id == app_id))
        await session.commit()


def test_x_principal_is_checked_in_the_form_schema():
    ok = {"type": "object", "properties": {"a": {**PICK, "x-principal": {"pool": "slot:finance"}}}}
    assert validate_form_schema(ok) == []
    bad = {"type": "object", "properties": {
        "a": {**PICK, "x-principal": {"pool": "everyone"}},
        "b": {"type": "string", "x-principal": {"pool": "app"}},
        "c": {**PICK, "x-principal": "app"}}}
    problems = validate_form_schema(bad)
    assert len(problems) == 3, problems
    assert any("「b」不是选人控件" in p for p in problems)


async def test_users_table_is_untouched_by_candidates(world):
    """候选接口只读：不建用户、不改名录。"""
    async with SessionLocal() as session:
        before = len((await session.execute(select(User.id))).all())
    uma = await auth_env.login("uma")
    await uma.get("/api/directory/candidates", params=_params(world["app_id"], q="zz"))
    await uma.aclose()
    async with SessionLocal() as session:
        assert len((await session.execute(select(User.id))).all()) == before
