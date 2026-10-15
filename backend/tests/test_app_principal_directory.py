"""岗位绑定、应用授权里的人和组按名录核对（auth/directory.py，同 /api/directory、同前端 PrincipalPicker）。

界面上选人时拦着，旧客户端、直接调接口、绑好之后人被停用或删除的照样会漏过来：
- 账号密码登录：名录里没有的、停用了的，应用检查报错（说清是哪个岗位、哪一档授权、哪个名字），改授权回 422；
- 单点登录：人第一次登录后才进名录，名录里还没有的只提醒、照收；停用的照样算错；
- 关登录：不核对。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.api import deps
from app.apps.derive import derive_app
from app.apps.lint import acl_issues, lint_app
from app.db.base import SessionLocal
from app.db.models import App, AppGrant, Group
from tests.fakes import auth_env
from tests.test_app_spec_tools import chain, make_workflow, node


def _approval():
    return chain(
        node("start", "input", "开始", fields=[{"name": "amount", "type": "number", "required": True}]),
        node("check", "human", "财务审批", mode="approve", assignee={"kind": "slot", "slot": "finance"}),
        node("out", "output", "成果", fields=[{"name": "r", "value": "完成"}]),
    )


@pytest.fixture
async def people():
    """名录里：在用的 alice、停用的 dora、组 fin。"""
    await auth_env.reset()
    await auth_env.make_user("alice", "user")
    await auth_env.make_user("dora", "user", disabled=True)
    async with SessionLocal() as s:
        s.add(Group(name="fin", label="财务", source="local"))
        await s.commit()
    try:
        yield
    finally:
        async with SessionLocal() as s:
            for model in (AppGrant, App):
                await s.execute(delete(model))
            await s.commit()
        await auth_env.reset()


def _mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(deps, "auth_mode", lambda: mode)


def _bound(*items):
    return {"slots": {"finance": [{"kind": k, "name": n} for k, n in items]}}


def _principal_issues(issues, code="app.slot_principal_unknown"):
    return [i for i in issues if i["code"] == code]


async def test_slot_bindings_are_checked_against_the_directory(people, monkeypatch):
    wf, _ = await make_workflow(_approval())
    async with SessionLocal() as session:
        spec = await derive_app(session, wf, 1)
        spec["slots"]["finance"] = {"label": "财务审批人"}

        _mode(monkeypatch, "local")
        assert not _principal_issues(await lint_app(session, spec, _bound(("user", "alice"), ("group", "fin"))))
        issues = _principal_issues(await lint_app(session, spec, _bound(
            ("user", "alice"), ("user", "wangwu"), ("group", "nogroup"), ("user", "dora"))))
        assert [i["level"] for i in issues] == ["error"] * 3
        assert all(i["path"] == "slots.finance" for i in issues)
        text = [i["message"] for i in issues]
        assert "岗位「财务审批人」绑定的用户「wangwu」名录里没有" in text[0]
        assert "组「nogroup」名录里没有" in text[1]
        assert "用户「dora」已停用" in text[2] and issues[2]["problem"] == "disabled"

        # 单点登录：名录里还没有的只提醒（第一次登录后才进名录），停用的照样报错
        _mode(monkeypatch, "oidc")
        issues = _principal_issues(await lint_app(session, spec, _bound(("user", "wangwu"), ("user", "dora"))))
        assert [(i["level"], i["problem"]) for i in issues] == [("warning", "missing"), ("error", "disabled")]
        assert "第一次登录后才进名录" in issues[0]["message"]

        # 关登录：没有用户表，不核对
        _mode(monkeypatch, "off")
        assert not _principal_issues(await lint_app(session, spec, _bound(("user", "wangwu"), ("user", "dora"))))


async def test_grants_are_checked_against_the_directory(people, monkeypatch):
    async with SessionLocal() as session:
        app = App(name="授权核对")
        session.add(app)
        await session.flush()
        session.add_all([AppGrant(app_id=app.id, principal_kind="user", principal="alice", role="use"),
                         AppGrant(app_id=app.id, principal_kind="everyone", principal="", role="use"),
                         AppGrant(app_id=app.id, principal_kind="user", principal="wangwu", role="observe"),
                         AppGrant(app_id=app.id, principal_kind="user", principal="dora", role="manage")])
        await session.commit()

        _mode(monkeypatch, "local")
        issues = await acl_issues(session, app.id)
        assert [(i["code"], i["level"], i["path"]) for i in issues] == [("app.acl_principal_unknown", "error", "grants")] * 2
        assert "「查看全部实例」授权给的用户「wangwu」名录里没有" in issues[0]["message"]
        assert "以后建了同名的人会直接拿到这一档权限" in issues[0]["message"]
        assert "「管理」授权给的用户「dora」已停用" in issues[1]["message"]

        _mode(monkeypatch, "oidc")
        assert [i["level"] for i in await acl_issues(session, app.id)] == ["warning", "error"]
        _mode(monkeypatch, "off")
        assert await acl_issues(session, app.id) == []


async def test_put_grants_refuses_names_not_in_the_directory(people, monkeypatch):
    admin = await auth_env.turn_on_local()
    app_id = (await admin.post("/api/apps", json={"name": "授权核对"})).json()["id"]
    url = f"/api/apps/{app_id}/grants"

    r = await admin.put(url, json={"items": [{"principal_kind": "user", "principal": "wangwu", "role": "use"},
                                             {"principal_kind": "user", "principal": "dora", "role": "observe"}]})
    assert r.status_code == 422
    assert "「使用」里的用户「wangwu」名录里没有" in r.json()["detail"]
    assert "「查看全部实例」里的用户「dora」已停用" in r.json()["detail"]
    assert (await admin.get(url)).json()["items"] == [], "拒收时一行都不改"

    ok = [{"principal_kind": "user", "principal": "alice", "role": "use"},
          {"principal_kind": "group", "principal": "fin", "role": "observe"},
          {"principal_kind": "everyone", "principal": "", "role": "use"}]
    r = await admin.put(url, json={"items": ok})
    assert r.status_code == 200 and r.json()["items"] == ok

    # 单点登录：名录里还没有的照收（第一次登录后才进名录），停用的照样拒收
    _mode(monkeypatch, "oidc")
    assert (await admin.put(url, json={"items": [{"principal_kind": "user", "principal": "wangwu", "role": "use"}]})
            ).status_code == 200
    assert (await admin.put(url, json={"items": [{"principal_kind": "user", "principal": "dora", "role": "use"}]})
            ).status_code == 422


async def test_directory_endpoint_uses_the_same_source(people):
    admin = await auth_env.turn_on_local()
    body = (await admin.get("/api/directory")).json()
    users = {u["username"]: u for u in body["users"]}
    assert users["dora"]["disabled"] is True and users["alice"]["display_name"] == "alice"
    assert body["groups"] == [{"name": "fin", "label": "财务"}]
