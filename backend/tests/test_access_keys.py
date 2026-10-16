"""访问密钥与服务账号：签发、认人、轮换、身份来源、限流、留痕（docs/design/next-round/04-对外集成与触达.md A1、A2、A6）。

要守住的：
- 密钥只在建、轮换时显示一次，库里只有 sha256 和开头一截；到期必填、最长 365 天；
- 认不出的密钥各回 401 带各自的 code（无效、到期、撤销、拥有者停用、拥有者被删、个人密钥开关关了），绝不退回匿名或会话；
- 轮换后旧密钥 24 小时内还能用，过了就失效；关登录时带密钥回 409（keys_need_login）；
- 带密钥的请求只按密钥认：不查 CSRF、不看 cookie、不看 X-Actor；带 cookie、不带来源标记的 POST 照旧 403；
- 每把密钥每分钟 120 次，超了 429 带 Retry-After；
- 建、撤销、轮换密钥，建、停用服务账号，开关个人密钥都写审计；请求记录批量写之后查得到、超过保留期的清掉；
- 服务账号 svc- 开头、不能登录、角色固定；名录里标出，岗位绑定、选人候选里排除，授权照收。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.auth import keys
from app.db.base import SessionLocal, utcnow
from app.db.models import AuditEvent, User
from app.db.models_identity import AccessKey, AccessKeyRequest
from tests.fakes import auth_env


@pytest.fixture(autouse=True)
async def clean():
    await auth_env.reset()
    try:
        yield
    finally:
        await auth_env.reset()


async def _audits(action: str) -> list[AuditEvent]:
    async with SessionLocal() as s:
        return list((await s.execute(select(AuditEvent).where(AuditEvent.action == action))).scalars())


# ---------------------------------------------------------------- 签发


async def test_a_personal_key_is_shown_once_and_only_its_hash_is_stored():
    await auth_env.turn_on_local()
    await auth_env.make_user("ana", "builder")
    ana = await auth_env.login("ana")
    r = await ana.post("/api/access-keys", json={"name": "日报脚本"})
    assert r.status_code == 409 and r.json()["code"] == "personal_keys_off", "开关缺省关：建不了个人密钥"
    await auth_env.personal_keys(True)
    r = await ana.post("/api/access-keys", json={"name": "日报脚本"})
    assert r.status_code == 201, r.text
    body = r.json()
    secret = body["key"]
    assert secret.startswith("agl_") and len(secret) >= 4 + 43
    assert body["scopes"] == list(keys.READ_SCOPES), "新建时缺省只勾读的"
    assert body["prefix"] == secret[:12] and body["state"] == "active"
    async with SessionLocal() as s:
        row = await s.get(AccessKey, body["id"])
    assert row.key_hash == keys.key_hash(secret) and secret not in (row.key_hash, row.prefix), "库里没有明文"
    listed = (await ana.get("/api/access-keys")).json()["items"]
    assert [i["id"] for i in listed] == [body["id"]] and "key" not in listed[0], "列表里不再有明文"
    days = (keys.aware(row.expires_at) - utcnow()).days
    assert days in (89, 90), "缺省 90 天"
    too_long = await ana.post("/api/access-keys", json={"name": "x", "expires_in_days": 366})
    assert too_long.status_code == 422, "最长 365 天"
    bad_scope = await ana.post("/api/access-keys", json={"name": "x", "scopes": ["runs.read"]})
    assert bad_scope.status_code == 422 and bad_scope.json()["field"] == "scopes"
    assert [a.data["name"] for a in await _audits("access_key.create")] == ["日报脚本"]


async def test_each_unusable_key_gets_its_own_401_and_never_falls_back():
    admin = await auth_env.turn_on_local()
    await auth_env.personal_keys(True)
    uma = await auth_env.make_user("uma", "user")
    ok_row, ok = await auth_env.make_key(uma)
    _, expired = await auth_env.make_key(uma, days=-1)
    revoked_row, revoked = await auth_env.make_key(uma)
    assert (await admin.post(f"/api/access-keys/{revoked_row.id}/revoke")).status_code == 200

    async def code(secret: str, client=None) -> tuple[int, str | None]:
        r = await (client or auth_env.keyed(secret)).get("/api/auth/me")
        return r.status_code, r.json().get("code")

    assert (await code(ok)) == (200, None)
    assert (await code("agl_not-a-real-key")) == (401, "key_invalid")
    assert (await code(expired)) == (401, "key_expired")
    assert (await code(revoked)) == (401, "key_revoked")
    # 带着管理员的会话 cookie 也不退回会话：认不出的密钥就是 401
    admin.headers["Authorization"] = f"Bearer {revoked}"
    assert (await code(revoked, admin)) == (401, "key_revoked")
    del admin.headers["Authorization"]
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == uma.id).values(disabled=True))
        await s.commit()
    assert (await code(ok)) == (401, "key_owner_disabled")
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == uma.id).values(disabled=False))
        await s.commit()
    await auth_env.personal_keys(False)
    assert (await code(ok)) == (401, "key_personal_off"), "管理员关了开关：已有的个人密钥一并不认"
    await auth_env.personal_keys(True)
    assert (await code(ok)) == (200, None)
    async with SessionLocal() as s:
        await s.delete(await s.get(User, uma.id))
        await s.commit()
        assert await s.get(AccessKey, ok_row.id) is None, "拥有者删了，密钥随外键一起删"
    assert (await code(ok)) == (401, "key_invalid")


async def test_me_tells_who_the_key_is_and_its_scope_and_expiry():
    await auth_env.turn_on_local()
    svc = await auth_env.make_service()
    row, secret = await auth_env.make_key(svc, scopes=("apps.read", "tasks.respond"), app_ids=["app-a"])
    me = (await auth_env.keyed(secret).get("/api/auth/me")).json()
    assert (me["username"], me["role"], me["verified"]) == ("svc-portal", "user", True)
    assert me["key"]["id"] == row.id and me["key"]["service"] is True
    assert me["key"]["scopes"] == ["apps.read", "me"], "服务账号的密钥去掉只给个人密钥的回复待办"
    assert me["key"]["app_ids"] == ["app-a"] and me["key"]["expires_at"]


async def test_rotation_keeps_the_old_key_for_24_hours():
    admin = await auth_env.turn_on_local()
    svc = await auth_env.make_service()
    created = (await admin.post("/api/access-keys", json={"name": "门户", "service_account_id": svc.id,
                                                          "scopes": ["apps.read"], "expires_in_days": 30})).json()
    old = created["key"]
    rotated = await admin.post(f"/api/access-keys/{created['id']}/rotate")
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    new = body["key"]
    assert new != old and body["rotated_from"] == created["id"] and body["scopes"] == ["apps.read"]
    assert (await auth_env.keyed(old).get("/api/auth/me")).status_code == 200, "旧密钥 24 小时内还能用"
    assert (await auth_env.keyed(new).get("/api/auth/me")).status_code == 200
    async with SessionLocal() as s:
        row = await s.get(AccessKey, created["id"])
        grace = keys.aware(row.expires_at) - utcnow()
        assert timedelta(hours=23) < grace <= timedelta(hours=24)
        fresh = await s.get(AccessKey, body["id"])
        assert 29 <= (keys.aware(fresh.expires_at) - utcnow()).days <= 30, "新密钥的有效期长短照旧"
        row.expires_at = utcnow() - timedelta(seconds=1)  # 24 小时过去了
        await s.commit()
    assert (await auth_env.keyed(old).get("/api/auth/me")).json()["code"] == "key_expired"
    again = await admin.post(f"/api/access-keys/{created['id']}/rotate")
    assert again.status_code == 409, "过期的不能再轮换"
    assert [a.subject_id for a in await _audits("access_key.rotate")] == [created["id"]]


async def test_keys_do_nothing_with_login_off():
    c = auth_env.keyed("agl_whatever")
    r = await c.get("/api/apps")
    assert r.status_code == 409 and r.json()["code"] == "keys_need_login", "关登录时不能让调用方以为自己被收窄了"
    off = auth_env.client()
    assert (await off.post("/api/access-keys", json={"name": "x"})).json()["code"] == "keys_need_login"
    assert (await off.get("/api/access-keys")).json() == {"items": []}
    assert (await off.get("/api/access-keys/settings")).json()["login_on"] is False


# ---------------------------------------------------------------- 身份来源与 CSRF


async def test_bearer_skips_csrf_but_cookie_requests_still_need_it():
    admin = await auth_env.turn_on_local()
    await auth_env.personal_keys(True)
    uma = await auth_env.make_user("uma", "user")
    _, secret = await auth_env.make_key(uma)
    # 带密钥的 POST 不带来源标记也能过 CSRF（到了接口：应用不存在 404，不是 csrf_header）
    r = await auth_env.keyed(secret).post("/api/apps/nope/entries/x", json={"values": {}},
                                          headers={"Idempotency-Key": "k1", "Origin": "https://evil.example"})
    assert r.status_code == 404 and r.json()["code"] == "app_not_found", r.text
    # 带 cookie、不带来源标记的 POST 照旧拒
    del admin.headers["X-AgentLab-Request"]
    assert (await admin.post("/api/groups", json={"name": "g"})).json()["code"] == "csrf_header"
    # 同时带 cookie 和密钥：按密钥认，cookie 不起作用；X-Actor 一律不看
    admin.headers["Authorization"] = f"Bearer {secret}"
    me = (await admin.get("/api/auth/me", headers={"X-Actor": "mallory"})).json()
    assert (me["username"], me["role"]) == ("uma", "user")
    assert (await admin.get("/api/users")).json()["code"] == "key_route_forbidden", "管理员的 cookie 不起作用"


async def test_the_principal_from_a_key_is_capped_at_user():
    await auth_env.turn_on_local()
    await auth_env.personal_keys(True)
    root = await _user("root")
    _, secret = await auth_env.make_key(root)
    me = (await auth_env.keyed(secret).get("/api/auth/me")).json()
    assert (me["username"], me["role"]) == ("root", "user"), "管理员的密钥也按使用者算"


async def _user(username: str) -> User:
    async with SessionLocal() as s:
        return (await s.execute(select(User).where(User.username == username))).scalar_one()


# ---------------------------------------------------------------- 限流


async def test_each_key_is_rate_limited_per_minute(monkeypatch):
    await auth_env.turn_on_local()
    svc = await auth_env.make_service()
    row, secret = await auth_env.make_key(svc)
    monkeypatch.setattr(keys, "RATE_PER_MINUTE", 3)
    c = auth_env.keyed(secret)
    assert [(await c.get("/api/auth/me")).status_code for _ in range(3)] == [200, 200, 200]
    r = await c.get("/api/auth/me")
    assert r.status_code == 429 and r.json()["code"] == "key_rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    _, other = await auth_env.make_key(svc, name="另一把")
    assert (await auth_env.keyed(other).get("/api/auth/me")).status_code == 200, "按密钥各算各的"
    assert keys.take(row.id, now=10**9) == 0, "过一会儿补满了就又能用"


# ---------------------------------------------------------------- 留痕


async def test_requests_are_buffered_written_in_batches_and_purged():
    admin = await auth_env.turn_on_local()
    svc = await auth_env.make_service()
    row, secret = await auth_env.make_key(svc, scopes=("apps.read",))
    c = auth_env.keyed(secret)
    await c.get("/api/apps")
    await c.get("/api/tasks")                         # 缺 scope：被拒的也记
    async with SessionLocal() as s:
        assert (await s.execute(select(AccessKeyRequest))).first() is None, "先攒在进程里，不是每个请求同步写一行"
    from app.scheduler.loop import process_due

    await process_due()                               # 调度循环的一拍
    async with SessionLocal() as s:
        rows = list((await s.execute(select(AccessKeyRequest).order_by(AccessKeyRequest.at))).scalars())
    assert [(r.method, r.route, r.status) for r in rows] == [("GET", "/api/apps", 200), ("GET", "/api/tasks", 403)]
    assert all(r.key_id == row.id for r in rows)
    listed = (await admin.get(f"/api/access-keys/{row.id}/requests")).json()
    assert [i["route"] for i in listed["items"]] == ["/api/tasks", "/api/apps"] and listed["retain_days"] == 90
    async with SessionLocal() as s:
        await s.execute(update(AccessKeyRequest).where(AccessKeyRequest.route == "/api/apps")
                        .values(at=utcnow() - timedelta(days=91)))
        await s.commit()
    await keys.tick(utcnow())
    async with SessionLocal() as s:
        left = [r.route for r in (await s.execute(select(AccessKeyRequest))).scalars()]
    assert left == ["/api/tasks"], "超过 90 天的清掉"


async def test_last_used_is_written_at_most_once_a_minute():
    await auth_env.turn_on_local()
    svc = await auth_env.make_service()
    row, secret = await auth_env.make_key(svc)
    c = auth_env.keyed(secret)
    await c.get("/api/auth/me")
    async with SessionLocal() as s:
        first = (await s.get(AccessKey, row.id)).last_used_at
    await c.get("/api/auth/me")
    async with SessionLocal() as s:
        assert (await s.get(AccessKey, row.id)).last_used_at == first, "一分钟内不再写"
        await s.execute(update(AccessKey).where(AccessKey.id == row.id)
                        .values(last_used_at=utcnow() - timedelta(minutes=2)))
        await s.commit()
    await c.get("/api/auth/me")
    async with SessionLocal() as s:
        assert keys.aware((await s.get(AccessKey, row.id)).last_used_at) > utcnow() - timedelta(seconds=30)


async def test_only_owners_and_admins_manage_keys_and_everything_is_audited():
    admin = await auth_env.turn_on_local()
    r = await admin.put("/api/access-keys/settings", json={"personal_keys": True})
    assert r.status_code == 200 and r.json()["personal_keys"] is True
    assert len(await _audits("auth.personal_keys")) == 1
    await auth_env.make_user("ana", "builder")
    await auth_env.make_user("bo", "user")
    ana, bo = await auth_env.login("ana"), await auth_env.login("bo")
    assert (await ana.put("/api/access-keys/settings", json={"personal_keys": False})).status_code == 403
    mine = (await ana.post("/api/access-keys", json={"name": "a"})).json()
    assert (await bo.post(f"/api/access-keys/{mine['id']}/revoke")).status_code == 404, "别人的密钥当不存在"
    assert (await bo.get(f"/api/access-keys/{mine['id']}/requests")).status_code == 404
    assert (await bo.get("/api/access-keys")).json()["items"] == []
    assert len((await admin.get("/api/access-keys")).json()["items"]) == 1, "管理员看得到全部"
    assert (await ana.post(f"/api/access-keys/{mine['id']}/revoke")).json()["state"] == "revoked"
    assert len(await _audits("access_key.revoke")) == 1
    # 服务账号：只给管理员建
    assert (await ana.post("/api/access-keys/service-accounts", json={"username": "svc-x"})).status_code == 403
    bad = await admin.post("/api/access-keys/service-accounts", json={"username": "portal"})
    assert bad.status_code == 422 and bad.json()["code"] == "service_account_invalid", "统一 svc- 开头"
    svc = (await admin.post("/api/access-keys/service-accounts", json={"username": "svc-portal",
                                                                         "display_name": "门户"})).json()
    assert svc["username"] == "svc-portal" and len(await _audits("service_account.create")) == 1
    no_respond = await admin.post("/api/access-keys", json={"name": "k", "service_account_id": svc["id"],
                                                            "scopes": ["apps.read", "tasks.respond"]})
    assert no_respond.status_code == 422, "服务账号不能被派待办，它的密钥不能勾回复待办"
    key = (await admin.post("/api/access-keys", json={"name": "k", "service_account_id": svc["id"]})).json()
    assert (await auth_env.keyed(key["key"]).get("/api/auth/me")).status_code == 200
    out = await admin.patch(f"/api/access-keys/service-accounts/{svc['id']}", json={"disabled": True})
    assert out.json()["disabled"] is True and len(await _audits("service_account.disable")) == 1
    assert (await auth_env.keyed(key["key"]).get("/api/auth/me")).json()["code"] == "key_owner_disabled", \
        "停用之后密钥失效"
    ref = (await bo.get("/api/access-keys/reference")).json()
    assert {"method": "POST", "path": "/api/apps/{app_id}/entries/{entry_id}"}.items() <= next(
        r for r in ref["routes"] if r["path"].endswith("/entries/{entry_id}")).items()
    assert (await auth_env.client().get("/api/access-keys/reference")).status_code == 401, "接口说明要登录"


# ---------------------------------------------------------------- 服务账号


async def test_service_accounts_cannot_log_in_or_get_a_password_or_role():
    admin = await auth_env.turn_on_local()
    svc = (await admin.post("/api/access-keys/service-accounts", json={"username": "svc-bi"})).json()
    r = await auth_env.client().post("/api/auth/login", json={"username": "svc-bi", "password": "password-123"})
    assert r.status_code == 401
    assert (await admin.patch(f"/api/users/{svc['id']}", json={"password": "password-123"})).status_code == 409
    assert (await admin.patch(f"/api/users/{svc['id']}", json={"role": "admin"})).status_code == 409
    users = {u["username"]: u["kind"] for u in (await admin.get("/api/users")).json()["items"]}
    assert users["svc-bi"] == "service" and users["root"] == "person"
    taken = await admin.post("/api/users", json={"username": "svc-me", "role": "user"})
    assert taken.status_code == 422, "svc- 开头留给服务账号"
    # 万一有会话指向服务账号也认不出来
    from app.auth import sessions

    async with SessionLocal() as s:
        user = await s.get(User, svc["id"])
        token = await sessions.create_session(s, user, ip=None, user_agent=None)
        await s.commit()
        assert await sessions.user_of(s, token) is None


async def test_service_accounts_are_marked_in_the_directory_and_kept_out_of_assignments():
    from app.auth.directory import load_directory, mismatch

    admin = await auth_env.turn_on_local()
    await auth_env.make_service("svc-bi")
    await auth_env.make_user("ana", "builder")
    r = await admin.post("/api/groups", json={"name": "ops"})
    await admin.put(f"/api/groups/{r.json()['id']}/members", json={"members": ["ana", "svc-bi"]})
    users = {u["username"]: u for u in (await admin.get("/api/directory")).json()["users"]}
    assert users["svc-bi"]["service"] is True and "service" not in users["ana"]
    async with SessionLocal() as s:
        known = await load_directory(s)
        assert mismatch(known, "local", "user", "svc-bi").problem == "service", "岗位绑定：服务账号算错"
        assert mismatch(known, "local", "user", "svc-bi", allow_service=True) is None, "授权：照收"
        from app.auth.directory import _members, review_pool

        assert set(await _members(s, "ops")) == {"ana"}, "候选池里不列服务账号"
        assert "svc-bi" not in (await review_pool(s)).users


async def test_grants_take_service_accounts_but_slot_bindings_do_not():
    """服务账号能拿应用授权（外部系统按授权读应用），岗位绑定里算错：派给它的待办没人能处理。"""
    from app.api.apps import grants_problem
    from app.apps.lint import _Issues, _lint_slot_principals

    await auth_env.turn_on_local()
    await auth_env.make_service("svc-bi")
    async with SessionLocal() as s:
        wanted, problem = await grants_problem(s, [("user", "svc-bi", "observe")])
        assert problem is None and wanted == [("user", "svc-bi", "observe")]
        out = _Issues()
        await _lint_slot_principals(s, {"approver": {"label": "审批人"}},
                                    {"approver": [{"kind": "user", "name": "svc-bi"}]}, out)
    assert [(i["level"], i["code"], i["problem"]) for i in out.items] == [
        ("error", "app.slot_principal_unknown", "service")]
    assert "服务账号" in out.items[0]["message"]


async def test_scheduler_stop_flushes_the_request_buffer():
    """关停时还攒在进程里的请求记录写进库（没起调度循环时也写）。"""
    from app.scheduler.loop import runner

    await auth_env.turn_on_local()
    svc = await auth_env.make_service()
    row, secret = await auth_env.make_key(svc)
    await auth_env.keyed(secret).get("/api/auth/me")
    await runner.stop()
    async with SessionLocal() as s:
        assert [r.key_id for r in (await s.execute(select(AccessKeyRequest))).scalars()] == [row.id]
