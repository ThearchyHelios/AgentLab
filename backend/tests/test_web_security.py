"""安全响应头（04 细案 C1）：app/web_security.py 的纯 ASGI 中间件，加上 mount_web 给页面算内联脚本的哈希。

要守住的：
- 页面、/api 的响应（连同登录检查自己回的 401）都带 nosniff、Referrer-Policy 和 CSP frame-ancestors；
- frame-ancestors 缺省 'self'，只有 'self' 时另加 X-Frame-Options: SAMEORIGIN；配了 AGENTLAB_FRAME_ANCESTORS 白名单时
  以 CSP 为准、不发 XFO（XFO 写不了白名单，两个都发的话老浏览器按 XFO 把白名单里的门户也拦了）；
- 分享页 /s 用 AGENTLAB_EMBED_ORIGINS、Referrer-Policy: no-referrer（路由第 3 波才加，头的规则先守住）；
- 严格的 script-src 只发 Content-Security-Policy-Report-Only，只在页面（index.html）上发，哈希就是构建产物里内联
  脚本的 sha256；接口返回的 HTML（导出的报告自带 meta CSP）不带它；
- 接口自己设过的同名头不覆盖、不重复；WebSocket 不经过它；
- 白名单写错了启动时就报错。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.responses import HTMLResponse, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import BACKEND_DIR, PROJECT_DIR, Settings
from app.main import app as main_app
from app.web_security import SecurityHeaders
from tests.fakes import auth_env

THEME = "\n      try { document.documentElement.setAttribute('data-theme', 'light') } catch (e) {}\n    "
INDEX = ("<!doctype html><html><head><script>" + THEME + "</script>"
         "<script type=\"module\" crossorigin src=\"/assets/index-AbC123.js\"></script></head>"
         "<body><div id=root></div></body></html>")
HASH = base64.b64encode(hashlib.sha256(THEME.encode()).digest()).decode()
REPORT = ("<!doctype html><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'\">"
          "<script>console.log('report')</script>")


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "index-AbC123.js").write_text("console.log('bundle')")
    return root


def _app(dist: Path | None = None, **config) -> FastAPI:
    app = FastAPI()
    api = APIRouter(prefix="/api")

    @api.get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "1"}

    @api.get("/report.html")
    async def report() -> HTMLResponse:
        return HTMLResponse(REPORT)

    @api.get("/download")
    async def download() -> Response:
        # 文件下载自己设了 nosniff（api/files.py）：不能再加一份
        return Response(b"x", headers={"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    @api.websocket("/echo")
    async def echo(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(await ws.receive_text())
        await ws.close()

    app.include_router(api)
    if dist is not None:
        from app.web import mount_web

        mount_web(app, dist)
    app.add_middleware(SecurityHeaders, **config)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.get(path)


def _single(r: httpx.Response, name: str) -> str | None:
    values = r.headers.get_list(name)
    assert len(values) <= 1, f"{name} 发了 {len(values)} 份：{values}"
    return values[0] if values else None


async def test_api_and_page_carry_the_defaults(dist: Path) -> None:
    app = _app(dist, frame_ancestors=[], embed_origins=[])
    for path in ("/api/ping", "/api/nope", "/", "/studio", "/assets/index-AbC123.js"):
        r = await _get(app, path)
        assert _single(r, "x-content-type-options") == "nosniff", path
        assert _single(r, "referrer-policy") == "strict-origin-when-cross-origin", path
        assert _single(r, "content-security-policy") == "frame-ancestors 'self'", path
        assert _single(r, "x-frame-options") == "SAMEORIGIN", path


async def test_report_only_script_policy_is_only_on_the_page(dist: Path) -> None:
    app = _app(dist, frame_ancestors=[], embed_origins=[])
    for path in ("/", "/studio", "/runs/abc/evidence"):
        r = await _get(app, path)
        assert r.text == INDEX
        assert _single(r, "content-security-policy-report-only") == f"script-src 'self' 'sha256-{HASH}'", path
    # 脚本文件、接口不带；接口返回的 HTML（导出的报告）自带 meta CSP，不加一份对不上哈希的上报策略，正文原样
    for path in ("/assets/index-AbC123.js", "/api/ping", "/api/report.html"):
        r = await _get(app, path)
        assert "content-security-policy-report-only" not in r.headers, path
    r = await _get(app, "/api/report.html")
    assert r.text == REPORT


async def test_whitelist_wins_and_drops_x_frame_options() -> None:
    app = _app(frame_ancestors=["https://portal.example.com", "https://*.corp.example"], embed_origins=[])
    r = await _get(app, "/api/ping")
    assert _single(r, "content-security-policy") == \
        "frame-ancestors 'self' https://portal.example.com https://*.corp.example"
    assert "x-frame-options" not in r.headers, "有白名单时以 CSP 为准：XFO 写不了白名单"


async def test_share_page_uses_the_embed_origins(dist: Path) -> None:
    app = _app(dist, frame_ancestors=["https://portal.example.com"], embed_origins=["https://bi.example.org"])
    for path in ("/s", "/s/", "/s/anything"):
        r = await _get(app, path)
        assert _single(r, "content-security-policy") == "frame-ancestors 'self' https://bi.example.org", path
        assert _single(r, "referrer-policy") == "no-referrer", path
        assert "x-frame-options" not in r.headers, path
    # 只认 /s 这一段：/settings、/studio 照整站的规则
    for path in ("/settings", "/studio", "/share"):
        r = await _get(app, path)
        assert _single(r, "content-security-policy") == "frame-ancestors 'self' https://portal.example.com", path
        assert _single(r, "referrer-policy") == "strict-origin-when-cross-origin", path
    # 没配嵌入白名单：分享页只能被本站嵌，同样另加 XFO
    r = await _get(_app(dist, frame_ancestors=[], embed_origins=[]), "/s")
    assert _single(r, "content-security-policy") == "frame-ancestors 'self'"
    assert _single(r, "x-frame-options") == "SAMEORIGIN"


async def test_headers_already_set_are_kept() -> None:
    r = await _get(_app(frame_ancestors=[], embed_origins=[]), "/api/download")
    assert _single(r, "x-content-type-options") == "nosniff"
    assert _single(r, "referrer-policy") == "no-referrer"
    assert _single(r, "content-security-policy") == "frame-ancestors 'self'"


def test_websocket_is_not_touched() -> None:
    with TestClient(_app(frame_ancestors=[], embed_origins=[])) as client:
        with client.websocket_connect("/api/echo") as ws:
            ws.send_text("hi")
            assert ws.receive_text() == "hi"


async def test_main_app_and_its_own_refusals_carry_the_headers() -> None:
    """真实的 app.main：加在最外层，登录检查自己回的 401、CORS 预检也带。"""
    app = main_app
    r = await _get(app, "/api/health")
    assert _single(r, "content-security-policy") == "frame-ancestors 'self'"
    assert _single(r, "x-frame-options") == "SAMEORIGIN" and _single(r, "x-content-type-options") == "nosniff"
    await auth_env.reset()
    try:
        await auth_env.turn_on_local()
        r = await _get(app, "/api/workflows")
        assert r.status_code == 401 and r.json()["code"] == "auth_required"
        assert _single(r, "x-content-type-options") == "nosniff"
        assert _single(r, "content-security-policy") == "frame-ancestors 'self'"
    finally:
        await auth_env.reset()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        r = await client.options("/api/health", headers={"Origin": "http://localhost:5273",
                                                         "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200 and _single(r, "x-content-type-options") == "nosniff"


def test_origin_lists_parse_and_bad_entries_fail_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTLAB_FRAME_ANCESTORS", raising=False)
    monkeypatch.delenv("AGENTLAB_EMBED_ORIGINS", raising=False)
    assert Settings().frame_ancestors == [] and Settings().embed_origins == []
    monkeypatch.setenv("AGENTLAB_FRAME_ANCESTORS", " https://portal.example.com/ , https://*.corp.example,'self'")
    monkeypatch.setenv("AGENTLAB_EMBED_ORIGINS", '["http://bi.example.org:8080"]')
    got = Settings()
    assert got.frame_ancestors == ["https://portal.example.com", "https://*.corp.example"], "末尾的 / 去掉，'self' 本来就有"
    assert got.embed_origins == ["http://bi.example.org:8080"]
    monkeypatch.delenv("AGENTLAB_EMBED_ORIGINS")
    # 只收「协议://主机[:端口]」：路径、通配一切、没写协议、'none' 这类写法启动时就报出来，别到嵌入失败才发现
    for bad in ("*", "portal.example.com", "https://portal.example.com/app", "ftp://portal.example.com", "'none'",
                "https://", "https://portal.example.com?x=1"):
        monkeypatch.setenv("AGENTLAB_FRAME_ANCESTORS", bad)
        with pytest.raises(ValidationError, match="AGENTLAB_FRAME_ANCESTORS"):
            Settings()


def test_deploy_env_documents_both_lists() -> None:
    text = (PROJECT_DIR / "deploy" / "agentlab.env").read_text()
    assert "# AGENTLAB_FRAME_ANCESTORS=" in text and "# AGENTLAB_EMBED_ORIGINS=" in text


_PROBE = r"""
import asyncio, json, httpx
from app.main import app

async def main():
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
        out = {}
        for p in ("/", "/api/health"):
            r = await c.get(p)
            out[p] = {k: r.headers.get(k) for k in ("content-security-policy", "x-frame-options",
                                                    "content-security-policy-report-only")}
        print(json.dumps(out))

asyncio.run(main())
"""


def test_main_app_reads_the_whitelist_and_hashes_the_page(dist: Path, tmp_path: Path) -> None:
    """真实的 app.main：环境变量里的白名单生效、不发 XFO；托管页面时 index.html 带上报策略。settings、app 都是模块级
    单例，起一个子进程导入。"""
    env = {**os.environ, "AGENTLAB_DATA_DIR": str(tmp_path / "data"), "AGENTLAB_WEB_DIST": str(dist),
           "AGENTLAB_SECRET_KEY": "test-only-not-a-real-key", "AGENTLAB_FRAME_ANCESTORS": "https://portal.example.com"}
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    for path in ("/", "/api/health"):
        assert out[path]["content-security-policy"] == "frame-ancestors 'self' https://portal.example.com", path
        assert out[path]["x-frame-options"] is None, path
    assert out["/"]["content-security-policy-report-only"] == f"script-src 'self' 'sha256-{HASH}'"
    assert out["/api/health"]["content-security-policy-report-only"] is None
