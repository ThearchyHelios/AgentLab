"""生产部署：由服务端直接托管前端构建产物（AGENTLAB_WEB_DIST）。

开发时页面由 Vite 开发服务器提供、/api 由它转发，用不到这里。生产部署只起一个进程，
页面和接口同源，省掉 CORS，也不用另配静态服务器。

规则：
- 构建产物里有这个文件就返回它；
- 其余非 /api 的 GET 回退到 index.html：前端是单页应用，/studio 这类地址由前端路由处理；
- /api 下不存在的路径照旧是 JSON 404。排除 /api 写在路由的路径规则里，不在处理函数里判断——
  否则 POST /api/不存在 会因为「路径对上了、方法不对」变成 405；
- 只注册 GET / HEAD 的 HTTP 路由，WebSocket 不经过这里；
- index.html 不缓存，发版后刷新即生效；assets/ 下的文件名带内容哈希，长缓存。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.convertors import Convertor, register_url_convertor

_NO_CACHE = "no-cache"
_IMMUTABLE = "public, max-age=31536000, immutable"

# 精简的 Linux 镜像里没有 /etc/mime.types，mimetypes 的内置表又缺几种（woff2 之类）。
# 构建产物里出现的类型写死在这里，别的再交给 mimetypes 猜
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
}


class _WebPath(Convertor[str]):
    """任意路径，但不以 api 这一段开头：/api、/api/… 留给接口路由和它们自己的 404。"""

    regex = r"(?!api(?:/|$)).*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("agentlab_web", _WebPath())


def _media_type(path: Path) -> str | None:
    return _TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]


def mount_web(app: FastAPI, dist: Path) -> None:
    """在 app 上挂页面托管。必须在所有 /api 路由注册之后调用。"""
    root = dist.resolve()
    index = root / "index.html"
    if not index.is_file():
        # 配了却没有产物：启动时就报出来，别等打开页面才看到一个 404
        raise RuntimeError(
            f"AGENTLAB_WEB_DIST 指向的 {root} 下没有 index.html。"
            "先构建前端（cd frontend && pnpm build），或者去掉这个设置"
        )

    def respond(target: Path, *, immutable: bool = False) -> FileResponse:
        return FileResponse(
            target,
            media_type=_media_type(target),
            headers={"Cache-Control": _IMMUTABLE if immutable else _NO_CACHE},
        )

    async def serve(path: str) -> FileResponse:
        if path:
            target = (root / path).resolve()
            # resolve 之后还得在 dist 里面：挡住 %2e%2e 这类穿越和指向外面的软链
            if target.is_file() and target.is_relative_to(root):
                return respond(target, immutable=path.startswith("assets/"))
            if path.startswith("assets/"):
                # 发版后旧页面还在请求旧哈希的文件。回退成 index.html 的话浏览器会把它当脚本
                # 解析、报一个看不懂的 MIME 错误，所以明确给 404
                raise HTTPException(status_code=404)
        return respond(index)

    app.add_api_route(
        "/{path:agentlab_web}", serve, methods=["GET", "HEAD"], include_in_schema=False,
    )
