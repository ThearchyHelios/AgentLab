"""安全响应头（04 细案 C1）：页面不能被别的站点套进 iframe 诱导点击，浏览器不按内容猜类型，链接不把完整地址带给别的站点。

以前服务端一个安全头都不发：任何站点都能把 AgentLab 嵌进 iframe。同站的门户页面（SameSite=Lax 的会话 cookie 会随请求
带上）和关登录的部署，都有被点击劫持的可能；也没有一条「允许谁嵌」的白名单可配。

纯 ASGI 中间件（同 auth/middleware.py，main.py 里加在最外层）：在 http.response.start 上补头，WebSocket 不经过它。
所有 HTTP 响应（页面、/api、登录检查自己回的 401 / 403、CORS 预检）都补：
- X-Content-Type-Options: nosniff；
- Referrer-Policy: strict-origin-when-cross-origin（分享页 /s 用 no-referrer：分享的 key 在地址的 # 后面，本来不随
  Referer 走，这里再收一道）；
- Content-Security-Policy: frame-ancestors。缺省只有 'self'；AGENTLAB_FRAME_ANCESTORS 列出允许嵌整站的同站门户；分享页
  /s 只认 AGENTLAB_EMBED_ORIGINS（分享链接，api/shares.py；页面是前端不进外壳的那一页，由 mount_web 照常托管）；
- 只有 frame-ancestors 是 'self' 时另加 X-Frame-Options: SAMEORIGIN，给不认 CSP 的老浏览器。XFO 写不了白名单，有白名单
  时只发 CSP：两个都发的话，只认 XFO 的老浏览器会把白名单里的门户也拦掉（认 CSP 的浏览器以 frame-ancestors 为准）。
接口自己设过的同名头（文件下载的 nosniff）不覆盖、不重复。

严格的 script-src 先只上报、不拦（Content-Security-Policy-Report-Only）：页面里有一段首帧前定主题的内联脚本，强制执行
的话写错一个哈希页面就会先闪一帧深色，构建会不会产出别的内联内容也没核实。所以 mount_web（app/web.py）启动时给构建产物
index.html 里的内联脚本算 sha256（script_policy），只在页面这一个响应上发；观察一个版本没有误报，再改成强制执行。没配
上报地址：违规只在浏览器的控制台里看得到。接口返回的 HTML（导出的报告、可验证报告）文件里自带 meta CSP，不发它，也不动。

嵌入的两种办法（04 细案 C3，只做配置和文档）：
- 同站门户嵌整站：门户和 AgentLab 在同一个可注册域名下（portal.corp.example 和 agentlab.corp.example）时，把门户的源写进
  AGENTLAB_FRAME_ANCESTORS。会话 cookie 是 SameSite=Lax，同站的 iframe 里会随请求带上：看的人照常登录、单点登录照常，权限
  和直接打开一样。不另做嵌入票据；
- 跨站嵌：嵌整站明确不支持——跨站的 iframe 里带不上 Lax 的会话 cookie，登录不了；改成 SameSite=None 会削弱整站防跨站请求
  伪造的那一层，换来的嵌入在 Safari、Firefox 上默认仍然被拦。跨站要嵌，用分享链接（只读、必设到期、可撤销，/s 只认
  AGENTLAB_EMBED_ORIGINS）。
deploy/agentlab.env 的「嵌入」一节写的是同一套。

HSTS 交给终结 https 的反向代理：这里不知道对外是不是 https、要不要连子域名一起管，发错了浏览器会按 max-age 记很久，
撤不回来。
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

#: 分享页的路径（/s#<分享 key>）：只认 /s 这一段，/settings、/studio 不算
SHARE_PATH = "/s"
_SELF = "'self'"
_DEFAULT_REFERRER = b"strict-origin-when-cross-origin"
_SHARE_REFERRER = b"no-referrer"
#: 页面里不带 src 的 <script>：要算哈希的内联脚本
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only"


def is_share_path(path: str) -> bool:
    return path == SHARE_PATH or path.startswith(SHARE_PATH + "/")


def frame_policy(origins: list[str]) -> tuple[bytes, bool]:
    """(CSP 的值, 要不要另加 X-Frame-Options)。origins 是白名单（config 里校验过、去掉了 'self'）。"""
    sources = [_SELF, *[o for o in origins if o != _SELF]]
    return f"frame-ancestors {' '.join(sources)}".encode(), len(sources) == 1


def script_policy(html: str) -> str:
    """页面的严格 script-src：本站的脚本文件（'self'）加上页面里每段内联脚本的 sha256（按原文的 UTF-8 字节算，同浏览器）。"""
    hashes = [base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
              for body in _INLINE_SCRIPT.findall(html)]
    return " ".join(["script-src", _SELF, *(f"'sha256-{h}'" for h in dict.fromkeys(hashes))])


class SecurityHeaders:
    """frame_ancestors / embed_origins 不传时取 core/config 的 AGENTLAB_FRAME_ANCESTORS、AGENTLAB_EMBED_ORIGINS（测试传别的）。"""

    def __init__(self, app: Any, *, frame_ancestors: list[str] | None = None,
                 embed_origins: list[str] | None = None) -> None:
        from app.core.config import settings

        self.app = app
        self.site = frame_policy(settings.frame_ancestors if frame_ancestors is None else frame_ancestors)
        self.share = frame_policy(settings.embed_origins if embed_origins is None else embed_origins)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        share = is_share_path(scope.get("path") or "")
        frame, xfo = self.share if share else self.site
        wanted = [(b"x-content-type-options", b"nosniff"),
                  (b"referrer-policy", _SHARE_REFERRER if share else _DEFAULT_REFERRER),
                  (b"content-security-policy", frame)]
        if xfo:
            wanted.append((b"x-frame-options", b"SAMEORIGIN"))

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {name.lower() for name, _ in headers}
                headers += [(name, value) for name, value in wanted if name not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
