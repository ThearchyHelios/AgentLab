from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.core.config import settings


class UnsafeUrlError(ValueError):
    pass


_BLOCKED_PORTS = {22, 23, 25, 135, 139, 445, 3389}


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local  # 169.254.169.254 —— 云厂商的元数据服务
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_safe_url(url: str) -> str:
    """挡住 SSRF。

    agent 手里的 HTTP 工具就是一把面向内网的扫描枪，所以这里做三件事：
    只放行 http/https、解析域名后逐个 IP 检查是不是内网/回环/元数据地址、
    再按配置里的域名白名单过一遍。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"只支持 http/https，收到 {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL 里没有主机名")
    if parsed.port in _BLOCKED_PORTS:
        raise UnsafeUrlError(f"端口 {parsed.port} 不允许访问")

    allowlist = settings.http_tool_allowlist
    if allowlist:
        if not any(host == d or host.endswith("." + d) for d in allowlist):
            raise UnsafeUrlError(f"{host} 不在白名单里（AGENTLAB_HTTP_TOOL_ALLOWLIST）")

    # 逐个解析结果都要查：一个域名可能同时解析到公网和内网地址
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UnsafeUrlError(f"域名解析失败：{host}") from e
    for info in infos:
        ip = info[4][0]
        if _is_private(ip):
            raise UnsafeUrlError(f"{host} 解析到内网地址 {ip}，已拦截")
    return url


async def safe_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: object | None = None,
    data: str | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, object]:
    """发一个受控的 HTTP 请求：禁跟随跳转（跳转是绕过 SSRF 检查的常见手法），
    逐跳重新校验，并对响应体截断。"""
    timeout = timeout or settings.http_tool_timeout
    max_bytes = max_bytes or settings.http_tool_max_bytes
    current = assert_safe_url(url)

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(5):
            resp = await client.request(
                method.upper(),
                current,
                headers=headers or {},
                params=params,
                json=json_body,
                content=data,
            )
            if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                current = assert_safe_url(
                    str(httpx.URL(current).join(resp.headers["location"]))
                )
                continue
            break

        body = resp.content[:max_bytes]
        truncated = len(resp.content) > max_bytes
        try:
            text = body.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = body.decode("utf-8", errors="replace")
        return {
            "status": resp.status_code,
            "url": str(resp.url),
            "headers": dict(resp.headers),
            "body": text,
            "truncated": truncated,
        }
