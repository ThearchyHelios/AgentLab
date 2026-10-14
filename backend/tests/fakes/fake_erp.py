"""假的外部系统（付款），供动作的测试（设计文档 §7.2）和浏览器验收用。名字、单号都是虚构的。

- 认 Idempotency-Key：同一个键只记一笔，再来同一个键交回那一笔（200），新键记一笔（201）；
- 有查询接口：按幂等键查付款，查到 200，查不到 404；
- 能注入四种情况（mode）：ok 正常；timeout_after_write 写成功之后不回话（客户端超时，结果未知）；error_500 不记账、
  回 500；error_4xx 不记账、回 422（参数不对）。
- requests 记下收到的每一次写请求（幂等键、环境、请求体），测试据此数「外部系统收到了几次」。

两种用法：
- 测试里不起服务：endpoint_request_for(erp) 给出一个和 capabilities.registry.endpoint_request 同签名的替身，
  monkeypatch 上去即可；
- 浏览器验收：scripts/fake-erp.py 起 app(erp)（Starlette），在「工具 → 端点」登记成一个端点（开内网访问，配 test 和
  prod 两个环境，路径 /test 和 /prod 前缀分别记账），付款能力 POST {环境}/payments，查询能力 GET {环境}/payments/lookup。
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

MODES = ("ok", "timeout_after_write", "error_500", "error_4xx")


@dataclass
class FakeErp:
    mode: str = "ok"
    #: (环境, 幂等键) → 付款记录
    payments: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: 收到的每一次写请求
    requests: list[dict[str, Any]] = field(default_factory=list)
    #: 收到的每一次查询
    lookups: list[dict[str, Any]] = field(default_factory=list)
    #: timeout_after_write 时服务端挂多久（秒）。比客户端的超时长就行
    hang_s: float = 30.0
    _ids: Any = field(default_factory=lambda: itertools.count(1001))

    def reset(self, mode: str = "ok") -> None:
        self.mode = mode
        self.payments.clear()
        self.requests.clear()
        self.lookups.clear()

    def writes(self, environment: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.requests if environment is None or r["environment"] == environment]

    def write(self, environment: str, idem_key: str | None, body: Any) -> tuple[int, dict[str, Any], bool]:
        """处理一次写请求：(状态码, 响应体, 是不是写成功之后不回话)。"""
        self.requests.append({"environment": environment, "idem_key": idem_key, "body": body, "mode": self.mode})
        if not idem_key:
            return 400, {"error": "missing Idempotency-Key"}, False
        if self.mode == "error_500":
            return 500, {"error": "internal error"}, False
        if self.mode == "error_4xx":
            return 422, {"error": "invalid amount"}, False
        key = (environment, idem_key)
        if key in self.payments:
            return 200, self.payments[key], False
        payment = {"id": f"PAY-{next(self._ids)}", "status": "paid", "idempotency_key": idem_key,
                   "amount": (body or {}).get("amount") if isinstance(body, dict) else None}
        self.payments[key] = payment
        return 201, payment, self.mode == "timeout_after_write"

    def lookup(self, environment: str, idem_key: str) -> tuple[int, dict[str, Any]]:
        self.lookups.append({"environment": environment, "idem_key": idem_key})
        found = self.payments.get((environment, idem_key))
        return (200, found) if found else (404, {"error": "not found"})


def endpoint_request_for(erp: FakeErp, *, lookup_capability: str = "erp_lookup") -> Any:
    """capabilities.registry.endpoint_request 的替身：名字是 lookup_capability 的当查询，其余当写入。语义同真的执行器：
    写成功之后不回话的交 unknown=True；演练（端点没有测试环境时传进来的是 simulated）不碰假系统。"""
    from app.capabilities.registry import EndpointResult

    async def fake(session: Any, capability: Any, args: dict[str, Any], *, environment: str,
                   idem_key: str | None = None, on_behalf: str | None = None, run_id: str | None = None,
                   node_id: str | None = None) -> EndpointResult:
        if environment == "simulated":
            return EndpointResult(status=None, body={"simulated": True}, environment="simulated")
        if capability.name == lookup_capability:
            status, body = erp.lookup(environment, str(args.get("idempotency_key") or ""))
            return EndpointResult(status=status, body=body, environment=environment)
        status, body, hung = erp.write(environment, idem_key, args)
        if hung:
            return EndpointResult(status=None, body=None, unknown=True, environment=environment,
                                  error="端点没有返回确定的结果：ReadTimeout")
        return EndpointResult(status=status, body=body, environment=environment)

    return fake


def app(erp: FakeErp | None = None) -> Any:
    """ASGI 应用（scripts/fake-erp.py 用）。路径以环境开头：/test/… 和 /prod/… 各记各的账。"""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    erp = erp or FakeErp()

    async def pay(request: Request) -> JSONResponse:
        env = request.path_params["env"]
        try:
            body = await request.json()
        except ValueError:
            body = None
        status, payload, hung = erp.write(env, request.headers.get("Idempotency-Key"), body)
        if hung:
            await asyncio.sleep(erp.hang_s)
        return JSONResponse(payload, status_code=status)

    async def lookup(request: Request) -> JSONResponse:
        status, payload = erp.lookup(request.path_params["env"], request.query_params.get("idempotency_key", ""))
        return JSONResponse(payload, status_code=status)

    async def mode(request: Request) -> JSONResponse:
        wanted = (await request.json()).get("mode")
        if wanted not in MODES:
            return JSONResponse({"error": f"mode must be one of {MODES}"}, status_code=400)
        erp.mode = wanted
        return JSONResponse({"mode": erp.mode})

    async def state(_: Request) -> JSONResponse:
        return JSONResponse({"mode": erp.mode, "requests": erp.requests, "lookups": erp.lookups,
                             "payments": [{"environment": e, **p} for (e, _), p in erp.payments.items()]})

    async def reset(_: Request) -> JSONResponse:
        erp.reset()
        return JSONResponse({"mode": erp.mode})

    return Starlette(routes=[
        Route("/{env:str}/payments", pay, methods=["POST"]),
        Route("/{env:str}/payments/lookup", lookup, methods=["GET"]),
        Route("/_fake/mode", mode, methods=["POST"]),
        Route("/_fake/state", state, methods=["GET"]),
        Route("/_fake/reset", reset, methods=["POST"]),
    ])


#: 测试里登记的两个能力：付款（写入，卡号遮罩）、按幂等键查付款（读）
PAY, LOOKUP = "erp_pay", "erp_lookup"


def install(monkeypatch: Any, erp: FakeErp, endpoint_id: str, *, lookup: bool) -> dict[str, Any]:
    """把能力登记和平台执行器换成替身（治理底座包的接口，测试里不依赖它的实现）。返回登记的能力，测试可以再改。"""
    from types import SimpleNamespace

    from app.capabilities import registry

    caps = {
        PAY: SimpleNamespace(name=PAY, title="付款", effect="write", effect_system="write", approval="inherit",
                             unattended_write=False, masked=["/card"], endpoint_id=endpoint_id,
                             lookup=LOOKUP if lookup else None, version=3, credential_mode="service"),
        LOOKUP: SimpleNamespace(name=LOOKUP, title="查付款", effect="read", effect_system="read", approval="none",
                                unattended_write=False, masked=[], endpoint_id=endpoint_id, lookup=None, version=1,
                                credential_mode="service"),
    }

    async def get_capability(session: Any, name: str) -> Any:
        return caps.get(name)

    monkeypatch.setattr(registry, "get_capability", get_capability)
    monkeypatch.setattr(registry, "endpoint_request", endpoint_request_for(erp, lookup_capability=LOOKUP))
    return caps


async def endpoint_row(*, test: bool) -> str:
    """建一个登记的端点（prod 必有，test 看参数），返回 id。"""
    import uuid

    from app.db.base import SessionLocal
    from app.db.models import Endpoint

    envs: dict[str, Any] = {"prod": {"base_url": "http://erp.example/prod", "headers": {}}}
    if test:
        envs["test"] = {"base_url": "http://erp.example/test", "headers": {}}
    async with SessionLocal() as session:
        row = Endpoint(name=f"erp-{uuid.uuid4().hex[:8]}", environments=envs)
        session.add(row)
        await session.commit()
        return row.id
