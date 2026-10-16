"""对外访问地址：别人打开 AgentLab 用的那个地址（https://agentlab.example.com），全站只有这一个说法（04 细案 B4）。

用到它的地方：通知和定时任务 Webhook 里的链接、单点登录的回调地址、非 GET 请求的来源核对（auth/middleware.py）；
之后的分享链接、接口说明也从这里取。服务端不知道自己在反向代理后面叫什么，只能由人告诉它。

取值（public_base_url）：
- 环境变量 AGENTLAB_PUBLIC_URL 优先：部署时定死，设置页上显示「由环境变量指定」、不能改（同登录方式的 env_locked）；
- 没设时取设置里 notify 一组的 public_base_url：沿用这个键，界面上叫「对外访问地址」，在「设置 → 通用」里改；
- 都没有是 None：通知里不附链接，单点登录的回调和来源核对按请求自己的 Host 认。

以前两处各管各的：环境变量只管回调和来源核对、设置只管通知里的链接——只配了环境变量的部署，通知里不带链接；只在设置里
填了的，回调又按请求的 Host 拼。违背「一种说法」。

设置的值缓存在进程里：中间件按请求核对来源，不能每次查库。启动时读一次（main._platform_startup），改设置的接口写完库就
remember；手里本来就有会话的异步路径（通知、定时任务 Webhook）用 refresh 取，顺手按库里的值对齐缓存。和登录设置的缓存一样
以单进程部署为前提。

这个模块不在顶层导入 core/config：config 的校验器要用这里的 url_problem，顶层互相导入会在启动时绕成环。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

#: 设置里存它的那一组、那一项（api/settings.py 的 NOTIFY_GROUP 就是这一组）
SETTING_GROUP = "notify"
SETTING_KEY = "public_base_url"
#: 环境变量的名字（core/config.Settings.public_url 按 AGENTLAB_ 前缀读它）
ENV_NAME = "AGENTLAB_PUBLIC_URL"

_stored: str | None = None


def url_problem(value: str) -> str | None:
    """地址的形状有什么问题：None 是没问题；"form" 不是 http:// 或 https:// 开头的完整地址；"suffix" 带了 ? 或 #
    （链接要接在地址后面）。设置接口按它出中文的说法，环境变量按它在启动时报错。可以带路径（反向代理挂在子路径下）。"""
    text = value.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return "form"
    if parts.scheme not in ("http", "https") or not parts.hostname or " " in text:
        return "form"
    if parts.query or parts.fragment:
        return "suffix"
    return None


def normalize(value: Any) -> str | None:
    """存着的（或刚填的）值 → 去掉首尾空白和末尾的 /；空的、形状不对的当没填（None）。末尾带 / 的地址拼链接会多出一个斜杠。"""
    if not isinstance(value, str) or not value.strip() or url_problem(value):
        return None
    return value.strip().rstrip("/")


def env_url() -> str | None:
    """环境变量给的地址；没设为 None。形状在启动时由 config 的校验器查过、末尾的 / 去掉了。"""
    from app.core.config import settings

    return settings.public_url or None


def env_locked() -> bool:
    """环境变量设了：设置里的那一项不生效、不能改。"""
    return env_url() is not None


def public_base_url() -> str | None:
    """对外访问地址：环境变量优先，其次设置（进程内缓存），都没有是 None。不查库，中间件里也能调。"""
    return env_url() or _stored


def remember(value: Any) -> None:
    """改设置的接口写完库之后调：缓存换成新存的值（形状不对、空的当没填）。"""
    global _stored
    _stored = normalize(value)


async def refresh(session: Any = None) -> str | None:
    """按库里存的设置对齐缓存，交回对外访问地址。启动时、手里有会话的异步路径用。"""
    from app.db.base import SessionLocal
    from app.db.models import Setting

    if session is None:
        async with SessionLocal() as own:
            row = await own.get(Setting, SETTING_GROUP)
    else:
        row = await session.get(Setting, SETTING_GROUP)
    value = row.value if row is not None else None
    remember(value.get(SETTING_KEY) if isinstance(value, dict) else None)
    return public_base_url()


def origin_of(url: str) -> str:
    """地址里的「协议://主机[:端口]」：来源核对按它比。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
