"""身份三种模式（应用平台 A7）：off 默认、local、oidc。

要守住的：
- 关登录（off）时一切照旧：X-Actor 自报署名，所有接口放行，非 GET 不用带任何新请求头；
- 从 off 切到 local 必须同一个请求建出第一个管理员，建好就以他登录；
- 开登录后：没登录 401、角色不够 403；非 GET 要带 X-AgentLab-Request 头、Origin 不能是别的站点；
  伪造的 X-Actor 一律不看，审计记的是认证过的用户名；登录失败退避；会话闲置过期、登出、停用都立刻失效；
- 环境变量指定的登录方式不能在设置里改；只有 local 模式下、还没有管理员时能 bootstrap；
- oidc：授权码 + PKCE，id_token 按 JWKS 核验，state 只能用一次，groups 同步成只读的组、按映射定角色；
- python -m app.auth reset-admin 能找回管理员；
- WebSocket 只给 builder 及以上。
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.api.deps import Principal, actor_of, reset_principal, set_principal, signer_of
from app.db.base import SessionLocal, utcnow
from app.db.models import AuditEvent, AuthSession, Group, User
from tests.fakes import auth_env
from tests.fakes.fake_oidc import FakeOidc


@pytest.fixture(autouse=True)
async def clean():
    await auth_env.reset()
    try:
        yield
    finally:
        await auth_env.reset()


# ---------------------------------------------------------------- off


async def test_off_mode_behaves_as_before():
    c = auth_env.client()
    c.headers.pop("X-AgentLab-Request")
    me = (await c.get("/api/auth/me", headers={"X-Actor": "%E5%BC%A0%E4%B8%89"})).json()
    assert me == {**me, "mode": "off", "role": "admin", "username": "张三", "verified": False}
    # 管理员接口、非 GET 都不用登录、不用新请求头
    assert (await c.get("/api/users")).status_code == 200
    r = await c.post("/api/groups", json={"name": "ops"}, headers={"X-Actor": "lee"})
    assert r.status_code == 201, r.text
    async with SessionLocal() as s:
        audit = (await s.execute(select(AuditEvent).where(AuditEvent.action == "group.create"))).scalar_one()
    assert (audit.actor, audit.verified) == ("lee", False), "关登录时审计照实记自报的署名、未认证"


async def test_actor_of_ignores_header_once_logged_in():
    token = set_principal(Principal(username="alice", role="user", verified=True, mode="local"))
    try:
        assert actor_of("mallory") == "alice"
        assert signer_of("请求体署名", "mallory") == "alice", "开了登录：数据导入这类请求体里自带的署名也不看"
    finally:
        reset_principal(token)
    assert actor_of("mallory") == "mallory"
    assert signer_of("  请求体署名  ", "mallory") == "请求体署名" and signer_of("", "mallory") == "mallory", \
        "关登录：请求体里填的优先，其次 X-Actor"


# ---------------------------------------------------------------- local


async def test_switching_to_local_needs_the_first_admin_in_the_same_request():
    c = auth_env.client()
    r = await c.put("/api/auth/settings", json={"mode": "local"})
    assert r.status_code == 422 and "第一个管理员" in r.json()["detail"]
    r = await c.put("/api/auth/settings", json={"mode": "local", "bootstrap": {"username": "root", "password": "short"}})
    assert r.status_code == 422, "密码太短也不能切过去"
    r = await c.put("/api/auth/settings", json={"mode": "local",
                                                 "bootstrap": {"username": "root", "password": "password-123"}})
    assert r.status_code == 200, r.text
    assert r.json()["principal"]["role"] == "admin"
    me = (await c.get("/api/auth/me")).json()
    assert (me["mode"], me["username"], me["verified"]) == ("local", "root", True), "建出来就是登录状态"
    async with SessionLocal() as s:
        row = (await s.execute(select(AuthSession))).scalar_one()
    assert len(row.token_hash) == 64 and c.cookies.get("agentlab_session") not in (None, row.token_hash), \
        "库里只存 token 的 sha256"


async def test_roles_401_403_and_forged_actor():
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("bob", "builder")
    await auth_env.make_user("uma", "user")
    anon = auth_env.client()
    r = await anon.get("/api/workflows")
    assert r.status_code == 401
    me = await anon.get("/api/auth/me")
    assert me.status_code == 401 and me.json()["mode"] == "local", "登录页据此显示账号密码"
    assert me.json()["can_bootstrap"] is False, "已经有管理员：登录页不给建管理员"

    uma = await auth_env.login("uma")
    assert (await uma.get("/api/workflows")).status_code == 403
    assert (await uma.get("/api/users")).status_code == 403
    bob = await auth_env.login("bob")
    assert (await bob.get("/api/workflows")).status_code == 200
    assert (await bob.get("/api/users")).status_code == 403, "用户、组、审计只给管理员"

    r = await admin.post("/api/groups", json={"name": "fin"}, headers={"X-Actor": "mallory"})
    assert r.status_code == 201
    async with SessionLocal() as s:
        audit = (await s.execute(select(AuditEvent).where(AuditEvent.action == "group.create"))).scalar_one()
    assert (audit.actor, audit.verified) == ("root", True), "伪造的 X-Actor 被忽略，审计照实记"


async def test_directory_is_read_only_for_builders_and_leaves_out_the_rest():
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("bob", "builder", email="bob@example.com")
    await auth_env.make_user("uma", "user", disabled=True)
    assert (await admin.post("/api/groups", json={"name": "fin", "label": "财务"})).status_code == 201
    bob = await auth_env.login("bob")
    r = await bob.get("/api/directory")
    assert r.status_code == 200, "搭建者绑岗位、授权要选人：名录给搭建者"
    body = r.json()
    assert {u["username"]: u["disabled"] for u in body["users"]} == {"root": False, "bob": False, "uma": True}, \
        "停用的人也列出来，标明停用"
    assert set(body["users"][0]) == {"username", "display_name", "disabled"}, "不含邮箱、角色这些"
    assert body["groups"] == [{"name": "fin", "label": "财务"}]
    assert (await bob.post("/api/directory", json={})).status_code == 405, "只读"
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.username == "uma").values(disabled=False))
        await s.commit()
    uma = await auth_env.login("uma")
    assert (await uma.get("/api/directory")).status_code == 403, "使用者不给"


async def test_csrf_header_and_origin():
    admin = await auth_env.turn_on_local()
    r = await admin.post("/api/groups", json={"name": "a"}, headers={"X-AgentLab-Request": ""})
    # 头带了但为空也算带了；去掉才算没带
    del admin.headers["X-AgentLab-Request"]
    r = await admin.post("/api/groups", json={"name": "b"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_header"
    admin.headers["X-AgentLab-Request"] = "1"
    r = await admin.post("/api/groups", json={"name": "c"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_origin"
    r = await admin.post("/api/groups", json={"name": "d"}, headers={"Origin": "http://test"})
    assert r.status_code == 201
    # GET 不查
    del admin.headers["X-AgentLab-Request"]
    assert (await admin.get("/api/groups")).status_code == 200


async def test_login_backoff_logout_idle_and_disabled():
    await auth_env.turn_on_local()
    await auth_env.make_user("uma", "user")
    c = auth_env.client()
    for _ in range(5):
        assert (await c.post("/api/auth/login", json={"username": "uma", "password": "wrong-pass"})).status_code == 401
    r = await c.post("/api/auth/login", json={"username": "uma", "password": "password-123"})
    assert r.status_code == 429, "连错之后要等一会儿，密码对了也一样"
    from app.auth import sessions

    sessions._failures.clear()
    uma = await auth_env.login("uma")
    assert (await uma.get("/api/auth/me")).status_code == 200
    assert (await uma.post("/api/auth/logout")).status_code == 204
    assert (await uma.get("/api/auth/me")).status_code == 401, "登出之后同一个 cookie 认不出来"

    uma = await auth_env.login("uma")
    async with SessionLocal() as s:
        await s.execute(update(AuthSession).values(last_seen_at=utcnow() - timedelta(hours=13)))
        await s.commit()
    assert (await uma.get("/api/auth/me")).status_code == 401, "闲置 12 小时过期"

    uma = await auth_env.login("uma")
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.username == "uma").values(disabled=True))
        await s.commit()
    assert (await uma.get("/api/auth/me")).status_code == 401, "停用立刻生效"


async def test_last_admin_cannot_be_demoted():
    admin = await auth_env.turn_on_local()
    users = (await admin.get("/api/users")).json()["items"]
    root = next(u for u in users if u["username"] == "root")
    r = await admin.patch(f"/api/users/{root['id']}", json={"role": "builder"})
    assert r.status_code == 409


async def test_env_mode_wins_and_bootstrap(monkeypatch):
    monkeypatch.setenv("AGENTLAB_AUTH_MODE", "local")
    c = auth_env.client()
    assert (await c.get("/api/workflows")).status_code == 401
    me = await c.get("/api/auth/me")
    assert me.status_code == 401 and me.json()["can_bootstrap"] is True, "新部署：登录页据此给「建第一个管理员」"
    r = await c.post("/api/auth/bootstrap", json={"username": "first", "password": "password-123"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    me = await auth_env.client().get("/api/auth/me")
    assert me.status_code == 401 and me.json()["can_bootstrap"] is False, "有了管理员就不再给"
    assert (await c.post("/api/auth/bootstrap", json={"username": "second", "password": "password-123"})
            ).status_code == 409
    r = await c.put("/api/auth/settings", json={"mode": "off"})
    assert r.status_code == 409, "环境变量指定的登录方式不能在设置里改"
    r = await c.put("/api/auth/settings", json={"user_can_chat": True})
    assert r.status_code == 200 and r.json()["user_can_chat"] is True and r.json()["env_locked"] is True


async def test_reset_admin_recovers_access():
    from app.auth.__main__ import reset_admin

    await auth_env.turn_on_local()
    await auth_env.make_user("ops", "user", disabled=True)
    assert await reset_admin("ops", "brand-new-pass") == "已重置"
    ops = await auth_env.login("ops", "brand-new-pass")
    me = (await ops.get("/api/auth/me")).json()
    assert me["role"] == "admin"
    assert await reset_admin("fresh", "brand-new-pass") == "已新建"
    async with SessionLocal() as s:
        actions = list((await s.execute(select(AuditEvent.actor).where(AuditEvent.action == "user.reset_admin"))
                        ).scalars())
    assert actions == ["cli", "cli"]


async def test_websocket_needs_a_login_and_the_route_judges_the_run():
    """中间件只挡没登录的 WebSocket；登录了的交给路由按运行判（api/deps.py 的 run_scope，见 test_user_chat_access.py）。"""
    from app.auth.middleware import AuthMiddleware

    await auth_env.turn_on_local()
    await auth_env.make_user("uma", "user")
    uma = await auth_env.login("uma")
    reached: list[str] = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/api/runs/r1/stream", "headers": []}
    await AuthMiddleware(inner)(scope, receive, send)
    assert not reached and sent == [{"type": "websocket.close", "code": 1008}], "没登录的握手前就关掉"

    scope["headers"] = [(b"cookie", f"agentlab_session={uma.cookies.get('agentlab_session')}".encode())]
    await AuthMiddleware(inner)(scope, receive, send)
    assert reached == ["/api/runs/r1/stream"], "登录了的使用者交给路由按运行判，不再一刀切"

    await auth_env.make_user("bob", "builder")
    bob = await auth_env.login("bob")
    scope["headers"] = [(b"cookie", f"agentlab_session={bob.cookies.get('agentlab_session')}".encode())]
    await AuthMiddleware(inner)(scope, receive, send)
    assert reached == ["/api/runs/r1/stream"] * 2


# ---------------------------------------------------------------- oidc


async def _oidc_login(c: httpx.AsyncClient, fake: FakeOidc, next_path: str = "/apps") -> httpx.Response:
    start = await c.get("/api/auth/oidc/start", params={"next": next_path})
    assert start.status_code == 302, start.text
    async with httpx.AsyncClient(trust_env=False) as browser:
        granted = await browser.get(start.headers["location"])
    assert granted.status_code == 302
    callback = httpx.URL(granted.headers["location"])
    return await c.get(callback.path, params=dict(callback.params))


async def test_oidc_login_maps_roles_and_syncs_groups():
    with FakeOidc() as fake:
        c = auth_env.client()
        r = await c.put("/api/auth/settings", json={"mode": "oidc", "oidc": {
            "issuer": fake.issuer, "client_id": "agentlab", "client_secret": "s3cret"}})
        assert r.status_code == 422, "没有管理员映射不能切到单点登录"
        r = await c.put("/api/auth/settings", json={"mode": "oidc", "oidc": {
            "issuer": fake.issuer, "client_id": "agentlab", "client_secret": "s3cret",
            "admin_groups": ["ops"], "builder_groups": ["analysts"]}})
        assert r.status_code == 200, r.text
        assert r.json()["oidc"]["client_secret"] != "s3cret", "客户端密钥只回掩码"

        fake.next_claims = {"sub": "u-1", "preferred_username": "alice", "name": "Alice",
                            "groups": ["ops", "finance"]}
        done = await _oidc_login(c, fake)
        assert done.status_code == 302 and done.headers["location"] == "/apps", done.headers.get("location")
        me = (await c.get("/api/auth/me")).json()
        assert (me["username"], me["role"], me["verified"]) == ("alice", "admin", True)
        assert set(me["groups"]) == {"ops", "finance"}

        groups = {g["name"]: g for g in (await c.get("/api/groups")).json()["items"]}
        assert groups["finance"]["source"] == "oidc" and groups["finance"]["members"] == ["alice"]
        r = await c.put(f"/api/groups/{groups['finance']['id']}/members", json={"members": []})
        assert r.status_code == 409, "同步来的组只读"

        # 换一个人登录：只在 analysts 组，是搭建者；alice 不再在 finance 组时，下次登录成员关系跟着撤
        other = auth_env.client()
        fake.next_claims = {"sub": "u-2", "preferred_username": "bert", "groups": ["analysts"]}
        await _oidc_login(other, fake)
        assert (await other.get("/api/auth/me")).json()["role"] == "builder"
        fake.next_claims = {"sub": "u-1", "preferred_username": "alice", "groups": ["ops"]}
        await _oidc_login(c, fake)
        async with SessionLocal() as s:
            finance = (await s.execute(select(Group).where(Group.name == "finance"))).scalar_one()
        assert (await c.get("/api/groups")).json()["items"] and \
            next(g for g in (await c.get("/api/groups")).json()["items"] if g["id"] == finance.id)["members"] == []


async def test_oidc_state_cannot_be_replayed():
    with FakeOidc() as fake:
        c = auth_env.client()
        r = await c.put("/api/auth/settings", json={"mode": "oidc", "oidc": {
            "issuer": fake.issuer, "client_id": "agentlab", "client_secret": "s3cret", "admin_users": ["alice"]}})
        assert r.status_code == 200, r.text
        start = await c.get("/api/auth/oidc/start")
        async with httpx.AsyncClient(trust_env=False) as browser:
            granted = await browser.get(start.headers["location"])
        callback = httpx.URL(granted.headers["location"])
        params = dict(callback.params)
        first = await c.get(callback.path, params=params)
        assert first.headers["location"] == "/"
        c.cookies.clear()
        again = await c.get(callback.path, params=params)
        assert again.headers["location"].startswith("/login?error="), "state cookie 已作废，回放被拒"
        wrong = auth_env.client()
        await wrong.get("/api/auth/oidc/start")
        bad = await wrong.get(callback.path, params={**params, "state": "forged"})
        assert bad.headers["location"].startswith("/login?error=")
        assert fake.token_calls == 1, "对不上的请求不去换 token"
