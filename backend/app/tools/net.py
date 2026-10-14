from __future__ import annotations

import asyncio
import ipaddress
import time
from urllib.parse import urlparse

import httpx

from app.core.config import settings


class UnsafeUrlError(ValueError):
    pass


_BLOCKED_PORTS = {22, 23, 25, 135, 139, 445, 3389}

# 云厂商的元数据服务：登记的端点开了内网访问也不放行（拿到它等于拿到这台机器的云凭据）。链路本地整段都算
# （169.254.169.254、ECS 任务元数据 169.254.170.2、IPv6 的 fe80::/10），另加不在链路本地段里的几个
_METADATA_HOSTS = frozenset({"100.100.100.200", "fd00:ec2::254", "metadata.google.internal"})


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


# 本机代理 fake-ip 模式最常见的假地址段。没配 fake-ip 网段时，拦截报错里据此提示一句
_COMMON_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
# 核实过的真实地址：host -> (过期时刻, 地址)。TTL 取 DNS 给的，夹在 30s–300s 之间
_real_ip_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_DOH_TYPES = {"A": 1, "AAAA": 28}


def _fake_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(c, strict=False) for c in settings.http_tool_fake_ip_ranges]


def _in(ip: str, networks) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in networks)


def _doh_client() -> httpx.AsyncClient:
    # 不走环境变量里的代理：核实要的就是公共 DNS 的原话
    return httpx.AsyncClient(timeout=5, trust_env=False)


async def _real_ips(host: str) -> tuple[str, ...]:
    """向公共 DNS（DoH）问 host 的真实地址。问不到就拦——核实不了不能当作安全。"""
    cached = _real_ip_cache.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    nxdomain = False
    async with _doh_client() as client:
        for base in settings.http_tool_doh_urls:
            try:
                replies = await asyncio.gather(*(
                    client.get(base, params={"name": host, "type": t},
                               headers={"accept": "application/dns-json"})
                    for t in _DOH_TYPES
                ))
                bodies = [r.json() for r in replies if r.status_code == 200]
            except (httpx.HTTPError, ValueError):
                continue
            if len(bodies) != len(_DOH_TYPES):
                continue
            ips: list[str] = []
            ttls: list[int] = []
            for body in bodies:
                for answer in body.get("Answer") or []:
                    if answer.get("type") in _DOH_TYPES.values() and answer.get("data"):
                        ips.append(str(answer["data"]))
                        ttls.append(int(answer.get("TTL") or 0))
            if ips:
                ttl = min(max(min(ttls), 30), 300)
                _real_ip_cache[host] = (time.monotonic() + ttl, tuple(ips))
                return tuple(ips)
            nxdomain = nxdomain or all(b.get("Status") == 3 for b in bodies)
            if nxdomain:
                break
    if nxdomain:
        raise UnsafeUrlError(f"{host} 在公共 DNS 中无法解析（本机只解析到代理分配的虚拟地址），已拦截")
    raise UnsafeUrlError(f"{host} 解析到代理分配的虚拟地址，且未能向公共 DNS 核实真实地址，已拦截")


def _is_metadata(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr.is_link_local or str(addr) in _METADATA_HOSTS


async def assert_safe_url(url: str, *, allow_intranet: bool = False) -> str:
    """挡住 SSRF。

    agent 手里的 HTTP 工具就是一把面向内网的扫描枪，所以这里做三件事：
    只放行 http/https、解析域名后逐个 IP 检查是不是内网/回环/元数据地址、
    再按配置里的域名白名单过一遍。

    本机代理开了 fake-ip 时，查什么都先拿到 198.18 段的假地址。配了
    http_tool_fake_ip_ranges 的话，落在段里的解析结果改向公共 DNS 核实真实地址
    再判断；直接放行不行——内网域名在 fake-ip 下也是这个段。

    allow_intranet 只给管理员登记、开了内网访问的端点用（app/capabilities/endpoints.py）：内网、回环地址放行，
    元数据地址和高危端口照样拦；域名白名单管的是模型能去哪儿，登记的端点不受它限制。模型给出的地址永远不带这个参数。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"只支持 http/https，当前为「{parsed.scheme}」")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL 中没有主机名")
    if parsed.port in _BLOCKED_PORTS:
        raise UnsafeUrlError(f"端口 {parsed.port} 不允许访问")
    if host.lower() in _METADATA_HOSTS:
        raise UnsafeUrlError(f"{host} 是云服务器的元数据地址，已拦截")

    if allow_intranet:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except OSError as e:
            raise UnsafeUrlError(f"域名解析失败：{host}") from e
        for info in infos:
            if _is_metadata(info[4][0]):
                raise UnsafeUrlError(f"{host} 解析到元数据地址 {info[4][0]}，已拦截")
        return url

    allowlist = settings.http_tool_allowlist
    if allowlist:
        if not any(host == d or host.endswith("." + d) for d in allowlist):
            raise UnsafeUrlError(f"{host} 不在允许访问的域名列表中（AGENTLAB_HTTP_TOOL_ALLOWLIST）")

    # 逐个解析结果都要查：一个域名可能同时解析到公网和内网地址
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as e:
        raise UnsafeUrlError(f"域名解析失败：{host}") from e

    fake = _fake_networks()
    try:
        ipaddress.ip_address(host)
        is_literal = True  # 直接写的 IP 没有域名可以去核实
    except ValueError:
        is_literal = False
    needs_check = False
    for info in infos:
        ip = info[4][0]
        if fake and _in(ip, fake) and not is_literal:
            needs_check = True  # 假地址本身说明不了什么，下面问公共 DNS
            continue
        if _is_private(ip):
            hint = ""
            if not fake and _in(ip, [_COMMON_FAKE_IP]):
                hint = ("。198.18.x.x 通常是本机代理 fake-ip 模式分配的虚拟地址：可设置 "
                        "AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES=198.18.0.0/15，改为向公共 DNS 核实真实地址；"
                        "或将代理的 DNS 改为 redir-host，并把该域名加入 fake-ip-filter")
            raise UnsafeUrlError(f"{host} 解析到内网地址 {ip}，已拦截{hint}")

    if needs_check:
        for ip in await _real_ips(host):
            if _is_private(ip):
                raise UnsafeUrlError(f"{host} 的真实地址 {ip} 是内网地址，已拦截")
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
    current = await assert_safe_url(url)

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
                current = await assert_safe_url(
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
