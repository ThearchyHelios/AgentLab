"""集成接口的错误码与 /docs（04 细案 A7）。

外部系统按 code 认是哪种错，不按中文句子：应用（api/apps.py）、待办（api/tasks.py）、工件（api/artifacts.py）的错误，
和角色、运行读权限的拒绝（api/deps.py、auth.require）全部带 code；detail 还是原来那一句，老前端照常显示。

开了登录，/docs、/redoc、/openapi.json 和 /api 一样要登录、要搭建者：以前中间件只拦 /api/ 开头的路径，接口全貌对
匿名的人敞开。关登录照旧（tests/test_provenance_contract.py 的断言不受影响）。
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import coded
from app.db.base import SessionLocal
from app.db.models_apps import App
from app.main import app as main_app
from tests.fakes import auth_env

APP_DIR = Path(__file__).resolve().parents[1] / "app"
DOCS = ("/docs", "/redoc", "/openapi.json")


@pytest.fixture(autouse=True)
async def clean():
    await auth_env.reset()
    try:
        yield
    finally:
        await auth_env.reset()


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=main_app), base_url="http://test")


async def new_app() -> str:
    async with SessionLocal() as session:
        row = App(name=f"报销-{uuid.uuid4().hex[:6]}")
        session.add(row)
        await session.commit()
        return row.id


def test_no_bare_http_exception_left_in_the_integration_modules():
    """这几个模块里不许再直接抛 HTTPException（没有 code）：一律 CodedHTTPException。"""
    for rel in ("api/apps.py", "api/tasks.py", "api/artifacts.py", "api/deps.py", "auth/__init__.py"):
        tree = ast.parse((APP_DIR / rel).read_text())
        bare = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
                and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "HTTPException"]
        assert not bare, f"{rel} 第 {bare} 行抛的 HTTPException 没有 code"


def test_codes_agree_with_the_ones_already_in_use():
    from app.apps import entries

    assert coded.APP_NOT_FOUND == entries.APP_GONE, "入口发起时的「应用不存在」和这里同一个码"


async def test_app_errors_carry_codes_and_keep_their_detail():
    app_id = await new_app()
    async with client() as c:
        r = await c.get("/api/apps/nope-nope")
        assert (r.status_code, r.json()) == (404, {"detail": "应用不存在，可能已被删除", "code": "app_not_found"})
        r = await c.get(f"/api/apps/{app_id}/versions/9")
        assert r.status_code == 404 and r.json()["code"] == "app_version_not_found" and "没有 v9" in r.json()["detail"]
        r = await c.get(f"/api/apps/{app_id}/pages/home", params={"params": "[1, 2]", "draft": 1})
        assert (r.status_code, r.json()) == (422, {"detail": "页面参数需要是 JSON 对象", "code": "app_params_invalid"})
        r = await c.patch(f"/api/apps/{app_id}", json={"draft_bindings": {"slots": {"财务": "不是列表"}}})
        assert r.status_code == 422 and r.json()["code"] == "app_bindings_invalid", r.text
        r = await c.post(f"/api/apps/{app_id}/hooks/nope/rotate")
        assert r.status_code == 404 and r.json()["code"] == "app_hook_not_declared"
        r = await c.patch(f"/api/apps/{app_id}/hooks/nope", json={"enabled": False})
        assert r.status_code == 404 and r.json()["code"] == "app_hook_not_found", r.text
        r = await c.post("/api/apps/derive", json={"workflow_id": "nope-nope", "version": 1})
        assert (r.status_code, r.json()["code"]) == (404, "workflow_not_found")


async def test_task_errors_carry_codes():
    async with client() as c:
        r = await c.get("/api/tasks/nope-nope")
        assert (r.status_code, r.json()) == (404, {"detail": "待办不存在，所属的运行可能已被删除", "code": "task_not_found"})
        # 服务层没带码的拒绝（这里是待办不存在）按状态补上同一类的码
        r = await c.post("/api/tasks/nope-nope/respond", json={"decision": "approve"})
        assert (r.status_code, r.json()["code"]) == (404, "task_not_found")
        r = await c.post("/api/tasks/nope-nope/remind")
        assert (r.status_code, r.json()["code"]) == (404, "task_not_found")


async def test_artifact_errors_carry_codes():
    async with client() as c:
        r = await c.get(f"/api/artifacts/{'0' * 64}")
        assert (r.status_code, r.json()) == (404, {"detail": "工件不存在，可能已被删除", "code": "artifact_not_found"})


async def test_role_and_run_refusals_carry_codes():
    await auth_env.turn_on_local()
    await auth_env.make_user("uma", "user")
    uma = await auth_env.login("uma")
    r = await uma.get("/api/workflows")
    assert r.status_code == 403 and r.json() == {"detail": "没有权限：这项操作需要搭建者及以上的角色",
                                                 "code": "role_required"}
    r = await uma.get("/api/tasks", params={"assigned": "all"})
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    r = await uma.get("/api/runs/nope-nope/evidence")
    assert r.status_code == 403 and r.json() == {"detail": "没有权限查看这次运行", "code": "run_forbidden"}
    r = await uma.get(f"/api/artifacts/{'0' * 64}")
    assert r.status_code == 403 and r.json() == {"detail": "没有权限查看这件工件", "code": "artifact_forbidden"}
    r = await uma.get("/api/runs")
    assert r.status_code == 403 and r.json()["code"] == "role_required"


async def test_ensure_role_inside_an_endpoint_carries_codes():
    from app.api.deps import Principal, ensure_role, reset_principal, set_principal

    for principal, status, code in ((Principal(username=None, role="", mode="local"), 401, "auth_required"),
                                    (Principal(username="bob", role="builder", verified=True, mode="local"), 403,
                                     "role_required")):
        token = set_principal(principal)
        try:
            with pytest.raises(coded.CodedHTTPException) as e:
                ensure_role("admin")
        finally:
            reset_principal(token)
        assert (e.value.status_code, e.value.code) == (status, code)
    assert "管理员" in e.value.detail


# ---------------------------------------------------------------- /docs


async def test_docs_stay_open_when_login_is_off():
    async with client() as c:
        for path in DOCS:
            assert (await c.get(path)).status_code == 200, path


async def test_docs_need_a_builder_once_login_is_on():
    await auth_env.turn_on_local()
    await auth_env.make_user("uma", "user")
    await auth_env.make_user("bob", "builder")
    async with client() as anonymous:
        for path in DOCS:
            r = await anonymous.get(path)
            assert r.status_code == 401 and r.json()["code"] == "auth_required", path
        # 子路径（Swagger 的 OAuth 回跳页）一样
        assert (await anonymous.get("/docs/oauth2-redirect")).status_code == 401
    uma = await auth_env.login("uma")
    for path in DOCS:
        r = await uma.get(path)
        assert r.status_code == 403 and r.json()["code"] == "role_required", path
    bob = await auth_env.login("bob")
    for path in DOCS:
        assert (await bob.get(path)).status_code == 200, path
    assert "/api/apps/{app_id}" in (await bob.get("/openapi.json")).json()["paths"]
