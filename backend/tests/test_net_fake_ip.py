"""本机代理 fake-ip 模式下的 SSRF 检查。

Clash / mihomo 一类代理开 TUN + fake-ip 时，本机查任何域名都先拿到 198.18.0.0/15
里的一个假地址，真正的连接由代理接管。Python 把这个网段算作私有地址，于是网页工具
对所有外网地址都报「解析到内网地址，已拦截」。

不能简单放行这个网段：fake-ip 模式下内网域名同样被映射成 198.18 的假地址，放行
等于关掉防护。做法是配置了 fake-ip 网段之后，落在网段里的解析结果改向公共 DNS
（DNS-over-HTTPS，不会被 TUN 劫持）核实真实地址：公网放行，内网照拦，核实不了按拦截算。
"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from app.core.config import Settings, settings
from app.tools import net
from app.tools.net import UnsafeUrlError, assert_safe_url

FAKE = "198.18.1.183"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    net._real_ip_cache.clear()
    monkeypatch.setattr(settings, "http_tool_allowlist", [])
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", [])
    monkeypatch.setattr(settings, "http_tool_doh_urls", ["https://doh-a.test/resolve", "https://doh-b.test/dns-query"])
    yield
    net._real_ip_cache.clear()


def resolves_to(monkeypatch, *ips):
    async def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)


class DoH:
    """假的公共 DNS：按 (resolver 主机, 记录类型) 返回；记下查了几次。"""

    def __init__(self, answers: dict[str, dict] | None = None, fail: set[str] | None = None):
        self.answers = answers or {}
        self.fail = fail or set()
        self.calls: list[tuple[str, str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        name = request.url.params.get("name")
        rtype = request.url.params.get("type")
        self.calls.append((host, name, rtype))
        assert request.headers.get("accept") == "application/dns-json"
        if host in self.fail:
            raise httpx.ConnectError("resolver unreachable")
        body = self.answers.get(f"{host}:{rtype}", {"Status": 0, "Answer": []})
        return httpx.Response(200, content=json.dumps(body))

    def install(self, monkeypatch):
        monkeypatch.setattr(
            net, "_doh_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=5),
        )


def a_record(*ips, ttl=120, cname=None):
    answers = [{"name": "x", "type": 5, "TTL": ttl, "data": cname}] if cname else []
    answers += [{"name": "x", "type": 1, "TTL": ttl, "data": ip} for ip in ips]
    return {"Status": 0, "Answer": answers}


async def test_without_config_a_fake_ip_is_blocked_with_a_hint(monkeypatch):
    resolves_to(monkeypatch, FAKE)
    with pytest.raises(UnsafeUrlError) as e:
        await assert_safe_url("https://ir.example.com/page")
    message = str(e.value)
    assert "已拦截" in message
    assert "fake-ip" in message and "AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES" in message


async def test_a_fake_ip_is_allowed_when_public_dns_says_the_host_is_public(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE)
    doh = DoH({"doh-a.test:A": a_record("93.184.216.34", cname="edge.example.net")})
    doh.install(monkeypatch)
    assert await assert_safe_url("https://ir.example.com/page") == "https://ir.example.com/page"
    assert ("doh-a.test", "ir.example.com", "A") in doh.calls


async def test_a_fake_ip_hiding_an_intranet_address_is_still_blocked(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE)
    DoH({"doh-a.test:A": a_record("10.0.0.5")}).install(monkeypatch)
    with pytest.raises(UnsafeUrlError) as e:
        await assert_safe_url("http://wiki.corp.example/")
    assert "10.0.0.5" in str(e.value) and "已拦截" in str(e.value)


async def test_an_ipv6_intranet_answer_is_blocked_too(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE)
    DoH({
        "doh-a.test:A": a_record("93.184.216.34"),
        "doh-a.test:AAAA": {"Status": 0, "Answer": [{"name": "x", "type": 28, "TTL": 60, "data": "fd00::1"}]},
    }).install(monkeypatch)
    with pytest.raises(UnsafeUrlError):
        await assert_safe_url("https://mixed.example.com/")


async def test_a_name_public_dns_does_not_know_is_blocked(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE)
    DoH({"doh-a.test:A": {"Status": 3}, "doh-a.test:AAAA": {"Status": 3}}).install(monkeypatch)
    with pytest.raises(UnsafeUrlError) as e:
        await assert_safe_url("http://build-server.internal/")
    assert "查不到" in str(e.value)


async def test_when_no_resolver_answers_it_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE)
    DoH(fail={"doh-a.test", "doh-b.test"}).install(monkeypatch)
    with pytest.raises(UnsafeUrlError) as e:
        await assert_safe_url("https://ir.example.com/")
    assert "核实" in str(e.value)


async def test_the_second_resolver_is_tried_when_the_first_is_down(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE)
    DoH({"doh-b.test:A": a_record("93.184.216.34")}, fail={"doh-a.test"}).install(monkeypatch)
    assert await assert_safe_url("https://ir.example.com/")


async def test_an_ip_literal_inside_the_fake_range_cannot_be_verified_and_is_blocked(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    doh = DoH()
    doh.install(monkeypatch)
    with pytest.raises(UnsafeUrlError):
        await assert_safe_url(f"http://{FAKE}/")
    assert doh.calls == []


async def test_a_real_private_answer_is_blocked_without_asking_public_dns(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE, "192.168.1.10")
    doh = DoH({"doh-a.test:A": a_record("93.184.216.34")})
    doh.install(monkeypatch)
    with pytest.raises(UnsafeUrlError) as e:
        await assert_safe_url("https://split.example.com/")
    assert "192.168.1.10" in str(e.value)
    assert doh.calls == []


async def test_verified_answers_are_cached_for_a_while(monkeypatch):
    monkeypatch.setattr(settings, "http_tool_fake_ip_ranges", ["198.18.0.0/15"])
    resolves_to(monkeypatch, FAKE)
    doh = DoH({"doh-a.test:A": a_record("93.184.216.34")})
    doh.install(monkeypatch)
    await assert_safe_url("https://ir.example.com/a")
    first = len(doh.calls)
    await assert_safe_url("https://ir.example.com/b")
    assert len(doh.calls) == first


async def test_public_and_private_answers_behave_as_before(monkeypatch):
    resolves_to(monkeypatch, "93.184.216.34")
    assert await assert_safe_url("https://www.example.com/")
    resolves_to(monkeypatch, "10.1.2.3")
    with pytest.raises(UnsafeUrlError) as e:
        await assert_safe_url("https://intra.example.com/")
    assert "fake-ip" not in str(e.value)


def test_list_settings_accept_comma_separated_env(monkeypatch):
    monkeypatch.setenv("AGENTLAB_HTTP_TOOL_ALLOWLIST", "example.com, example.org")
    monkeypatch.setenv("AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES", "198.18.0.0/15,fdfe:dcba:9876::/48")
    s = Settings()
    assert s.http_tool_allowlist == ["example.com", "example.org"]
    assert s.http_tool_fake_ip_ranges == ["198.18.0.0/15", "fdfe:dcba:9876::/48"]


def test_a_malformed_fake_ip_range_is_rejected_at_startup(monkeypatch):
    monkeypatch.setenv("AGENTLAB_HTTP_TOOL_FAKE_IP_RANGES", "198.18.0.0/99")
    with pytest.raises(Exception):
        Settings()


def test_the_default_resolver_order_puts_an_unpolluted_one_first(monkeypatch):
    """境内公共 DNS 对被封锁的域名返回污染结果：维基百科的 AAAA 给 2001::1，被当成
    内网拦掉。默认先问境外的，境内的只作兜底"""
    monkeypatch.delenv("AGENTLAB_HTTP_TOOL_DOH_URLS", raising=False)
    assert Settings().http_tool_doh_urls[0] == "https://1.1.1.1/dns-query"
