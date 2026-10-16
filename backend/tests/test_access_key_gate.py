"""访问密钥的门禁（docs/design/next-round/04-对外集成与触达.md A2、A7）：KEY_ROUTES 一张表，挂在全站一次。

合约测试遍历 app.routes：不在表里的 /api 接口，用密钥请求一律 403（key_route_forbidden）；表里的每一行都对得上一个真实的
路由（改了路由、漏改表会在这里报出来）。另守：缺 scope 的 403（key_scope_missing）；限定了应用时别的应用 403
（key_app_forbidden）；个人应用、个人应用的刷新、蓝图的试运行一律 403（key_app_personal）；带密钥连 WebSocket 被拒；
页面和 FastAPI 自带的接口说明不对密钥开放；接口说明（/api/access-keys/reference）和放行的接口出自同一张表。
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from sqlalchemy import delete

from app.auth import keys
from app.db.base import SessionLocal
from app.db.models import App, AppGrant, AppVersion, Run
from tests.fakes import auth_env


@pytest.fixture(autouse=True)
async def clean():
    await auth_env.reset()
    await auth_env.turn_on_local()
    try:
        yield
    finally:
        await auth_env.reset()


def _filled(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _contexts():
    """全站的路由（FastAPI 0.14x 起 include_router 不再把路由摊平进 app.routes，按它给的路由上下文遍历）。"""
    from fastapi.routing import iter_route_contexts

    from app.main import app

    return list(iter_route_contexts(app.routes))


async def test_every_route_not_in_the_table_is_forbidden_for_keys(monkeypatch):
    monkeypatch.setattr(keys, "RATE_PER_MINUTE", 10_000)  # 三百多个接口一口气调完，不让限流挡在门禁前面
    svc = await auth_env.make_service()
    _, secret = await auth_env.make_key(svc, scopes=keys.SCOPES)
    client = auth_env.keyed(secret)
    seen: set[tuple[str, str]] = set()
    checked = 0
    for ctx in _contexts():
        route = ctx.original_route
        if not isinstance(route, APIRoute) or not ctx.path.startswith("/api/"):
            continue
        # 门禁按 scope['route'].path（原路由的模板）比对：include_router 不能再加前缀，不然两边对不上
        assert route.path == ctx.path, f"{ctx.path} 的原路由模板是 {route.path}"
        for method in sorted(ctx.methods or ()):
            seen.add((method, ctx.path))
            if (method, ctx.path) in keys.KEY_ROUTES:
                continue
            r = await client.request(method, _filled(ctx.path), json={} if method in ("POST", "PUT", "PATCH") else None)
            assert r.status_code == 403 and r.json().get("code") == "key_route_forbidden", \
                f"{method} {ctx.path} 用密钥调回了 {r.status_code} {r.text[:120]}"
            checked += 1
    assert checked > 200, "真遍历到了全部接口"
    stale = set(keys.KEY_ROUTES) - seen
    assert not stale, f"KEY_ROUTES 里有对不上路由的行：{stale}"


async def test_routes_in_the_table_need_their_scope():
    svc = await auth_env.make_service()
    _, secret = await auth_env.make_key(svc, scopes=("apps.read",))
    c = auth_env.keyed(secret)
    assert (await c.get("/api/apps")).status_code == 200
    r = await c.get("/api/tasks")
    assert r.status_code == 403 and r.json()["code"] == "key_scope_missing"
    r = await c.post("/api/apps/x/entries/y", json={"values": {}}, headers={"Idempotency-Key": "k"})
    assert r.json()["code"] == "key_scope_missing", "发起要 apps.run"
    assert (await c.get("/api/auth/me")).status_code == 200, "me 不用申请"
    assert (await c.get("/api/health")).status_code == 200


async def test_pages_docs_and_websockets_are_closed_to_keys():
    from app.auth.middleware import AuthMiddleware
    from app.main import app

    svc = await auth_env.make_service()
    _, secret = await auth_env.make_key(svc, scopes=keys.SCOPES)
    c = auth_env.keyed(secret)
    for path in ("/docs", "/openapi.json", "/apps"):
        r = await c.get(path)
        assert r.status_code == 403 and r.json()["code"] == "key_route_forbidden", path

    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/api/runs/x/stream", "headers": [(b"authorization", f"Bearer {secret}".encode())],
             "query_string": b""}
    await AuthMiddleware(app.router)(scope, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}], "带密钥的握手前就关"


async def _apps() -> dict[str, str]:
    async with SessionLocal() as s:
        shared = App(name="周报", published_version=1, created_by="root")
        other = App(name="报销", published_version=1, created_by="root")
        mine = App(name="我的看板", scope="personal", owner="pia")
        s.add_all([shared, other, mine])
        await s.flush()
        for app in (shared, other):
            s.add(AppVersion(app_id=app.id, version=1, spec={"format": "agentlab.app/1", "pages": []}, spec_hash="h"))
            s.add(AppGrant(app_id=app.id, principal_kind="user", principal="pia", role="observe"))
        await s.commit()
        return {"shared": shared.id, "other": other.id, "mine": mine.id}


async def test_app_limits_personal_apps_and_trial_runs():
    await auth_env.personal_keys(True)
    pia = await auth_env.make_user("pia", "user")
    ids = await _apps()
    try:
        _, limited = await auth_env.make_key(pia, app_ids=[ids["shared"]])
        _, free = await auth_env.make_key(pia, name="不限应用")
        c, f = auth_env.keyed(limited), auth_env.keyed(free)
        assert [a["id"] for a in (await c.get("/api/apps")).json()["items"]] == [ids["shared"]], "只列限定的那几个"
        assert {a["id"] for a in (await f.get("/api/apps")).json()["items"]} == {ids["shared"], ids["other"]}, \
            "个人应用不列"
        assert (await c.get(f"/api/apps/{ids['shared']}")).status_code == 200
        r = await c.get(f"/api/apps/{ids['other']}")
        assert r.status_code == 403 and r.json()["code"] == "key_app_forbidden"
        r = await f.get(f"/api/apps/{ids['mine']}")
        assert r.status_code == 403 and r.json()["code"] == "key_app_personal", "自己的个人应用也不行"
        async with SessionLocal() as s:
            trial = Run(workflow_name="试运行", run_class="exploratory", origin_kind="trial", started_by="pia")
            refresh = Run(workflow_name="刷新", run_class="exploratory", origin_kind="personal_app",
                          app_id=ids["mine"], started_by="pia")
            elsewhere = Run(workflow_name="别处", run_class="formal", origin_kind="app_entry", app_id=ids["other"],
                            started_by="pia")
            s.add_all([trial, refresh, elsewhere])
            await s.commit()
            runs = (trial.id, refresh.id, elsewhere.id)
        for run_id in runs[:2]:
            r = await f.get(f"/api/runs/{run_id}/report.html")
            assert r.status_code == 403 and r.json()["code"] == "key_app_personal"
        r = await c.get(f"/api/runs/{runs[2]}/evidence")
        assert r.status_code == 403 and r.json()["code"] == "key_app_forbidden", "限定了应用：别的应用的运行也不行"
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(AppGrant).where(AppGrant.app_id.in_(list(ids.values()))))
            await s.execute(delete(AppVersion).where(AppVersion.app_id.in_(list(ids.values()))))
            await s.execute(delete(App).where(App.id.in_(list(ids.values()))))
            await s.commit()


async def test_the_reference_is_generated_from_the_same_table():
    admin = await auth_env.login("root")
    ref = (await admin.get("/api/access-keys/reference")).json()
    assert {(r["method"], r["path"]) for r in ref["routes"]} == set(keys.KEY_ROUTES)
    entry = next(r for r in ref["routes"] if r["path"] == "/api/apps/{app_id}/entries/{entry_id}")
    assert entry["scope"] == "apps.run" and entry["idempotent"] is True
    assert "key_idempotency_required" in entry["errors"] and "key_files_unsupported" in entry["errors"]
    assert "key_route_forbidden" in ref["common_errors"] and ref["rate_per_minute"] == keys.RATE_PER_MINUTE


def test_websocket_routes_are_never_in_the_table():
    sockets = {c.path for c in _contexts() if isinstance(c.original_route, APIWebSocketRoute)}
    assert sockets and not {p for _, p in keys.KEY_ROUTES} & sockets
