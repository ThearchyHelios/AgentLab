"""生产模式下服务端托管前端构建产物（AGENTLAB_WEB_DIST）。

用临时目录伪造一个 dist，覆盖这几条：
- 文件存在就返回文件；其余非 /api 的 GET 回退到 index.html（单页应用）；
- /api 下不存在的路径照旧是 JSON 404，不能被回退吞掉——POST 也一样，不能变成 405；
- WebSocket 不受影响；
- index.html 不缓存，assets/ 下带哈希的文件长缓存；
- 没设 AGENTLAB_WEB_DIST 时行为完全不变。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import BACKEND_DIR, PROJECT_DIR, Settings

INDEX = "<!doctype html><title>fake-agentlab</title><div id=root></div>"
JS = "console.log('fake bundle')"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (root / "assets" / "index-AbC123.js").write_text(JS)
    (root / "assets" / "index-AbC123.css").write_text("body{}")
    # dist 外面的文件：路径穿越不能读到它
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    return root


def _app(dist: Path) -> FastAPI:
    from app.web import mount_web

    app = FastAPI()
    api = APIRouter(prefix="/api")

    @api.get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "1"}

    @api.websocket("/echo")
    async def echo(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(await ws.receive_text())
        await ws.close()

    app.include_router(api)
    mount_web(app, dist)
    return app


async def _get(app: FastAPI, path: str, method: str = "GET") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path)


async def test_index_and_spa_fallback(dist: Path) -> None:
    app = _app(dist)
    for path in ("/", "/index.html", "/studio", "/runs/abc/evidence", "/studio/"):
        r = await _get(app, path)
        assert r.status_code == 200, path
        assert r.text == INDEX, path
        assert r.headers["content-type"].startswith("text/html"), path
        assert "no-cache" in r.headers["cache-control"], path


async def test_existing_files_are_served(dist: Path) -> None:
    app = _app(dist)
    r = await _get(app, "/assets/index-AbC123.js")
    assert r.status_code == 200
    assert r.text == JS
    assert "javascript" in r.headers["content-type"]
    assert "immutable" in r.headers["cache-control"]
    assert "max-age=31536000" in r.headers["cache-control"]

    r = await _get(app, "/assets/index-AbC123.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")

    r = await _get(app, "/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "immutable" not in r.headers.get("cache-control", "")


async def test_missing_asset_is_404_not_index(dist: Path) -> None:
    # 发版后旧页面请求旧哈希的文件：回退成 index.html 会被当脚本解析，还会被长缓存
    r = await _get(_app(dist), "/assets/index-old999.js")
    assert r.status_code == 404
    assert r.text != INDEX


async def test_api_routes_win_and_unknown_api_is_json_404(dist: Path) -> None:
    app = _app(dist)
    r = await _get(app, "/api/ping")
    assert r.status_code == 200 and r.json() == {"pong": "1"}

    for path in ("/api/nope", "/api", "/api/", "/api/runs/xyz/nope"):
        r = await _get(app, path)
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("application/json"), path
        assert r.json() == {"detail": "Not Found"}, path

    # 路径规则里就排除了 /api：否则「路径对上、方法不对」会让 POST 变成 405
    r = await _get(app, "/api/nope", method="POST")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}

    # 只保留 api 这一段，别的前缀照常回退
    r = await _get(app, "/apiary")
    assert r.status_code == 200 and r.text == INDEX


async def test_head_and_non_get(dist: Path) -> None:
    app = _app(dist)
    r = await _get(app, "/studio", method="HEAD")
    assert r.status_code == 200
    r = await _get(app, "/studio", method="POST")
    assert r.status_code == 405


async def test_path_traversal_stays_inside_dist(dist: Path) -> None:
    app = _app(dist)
    for path in ("/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"):
        r = await _get(app, path)
        assert "TOP-SECRET" not in r.text, path


def test_websocket_unaffected(dist: Path) -> None:
    client = TestClient(_app(dist))
    with client.websocket_connect("/api/echo") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "hi"
    # 没有这条 WebSocket 路由的地址不会被页面回退接走
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/studio") as ws:
            ws.receive_text()


def test_missing_index_fails_fast(tmp_path: Path) -> None:
    from app.web import mount_web

    with pytest.raises(RuntimeError, match="index.html"):
        mount_web(FastAPI(), tmp_path)


def test_setting_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTLAB_WEB_DIST", raising=False)
    assert Settings().web_dist is None
    monkeypatch.setenv("AGENTLAB_WEB_DIST", "")
    assert Settings().web_dist is None
    # 相对路径按仓库根目录算，和从哪个目录启动无关
    monkeypatch.setenv("AGENTLAB_WEB_DIST", "frontend/dist")
    assert Settings().web_dist == PROJECT_DIR / "frontend" / "dist"
    monkeypatch.setenv("AGENTLAB_WEB_DIST", "/srv/web")
    assert Settings().web_dist == Path("/srv/web")


async def test_default_app_unchanged_without_setting() -> None:
    from app.core.config import settings
    from app.main import app

    assert settings.web_dist is None
    r = await _get(app, "/studio")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


_PROBE = r"""
import asyncio, json, httpx
from app.main import app

async def main():
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
        out = {}
        for m, p in (("GET", "/api/health"), ("GET", "/"), ("GET", "/studio"),
                     ("GET", "/assets/index-AbC123.js"), ("GET", "/api/nope"), ("POST", "/api/nope")):
            r = await c.request(m, p)
            out[f"{m} {p}"] = [r.status_code, r.text[:200]]
        print(json.dumps(out))

asyncio.run(main())
"""


def test_main_app_mounts_when_setting_present(dist: Path, tmp_path: Path) -> None:
    """真实的 app.main 在设了 AGENTLAB_WEB_DIST 时托管页面、接口照旧。

    settings 和 app 都是模块级单例，同一进程里改不了，所以起一个子进程导入。
    """
    env = {
        **os.environ,
        "AGENTLAB_DATA_DIR": str(tmp_path / "data"),
        "AGENTLAB_WEB_DIST": str(dist),
        "AGENTLAB_SECRET_KEY": "test-only-not-a-real-key",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=BACKEND_DIR, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["GET /api/health"][0] == 200 and '"agentlab"' in out["GET /api/health"][1]
    assert out["GET /"] == [200, INDEX]
    assert out["GET /studio"] == [200, INDEX]
    assert out["GET /assets/index-AbC123.js"] == [200, JS]
    assert out["GET /api/nope"][0] == 404 and json.loads(out["GET /api/nope"][1]) == {"detail": "Not Found"}
    assert out["POST /api/nope"][0] == 404
