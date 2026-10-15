"""登记的端点与平台执行器（A6 第 2、3、7 条，§3.9）。

端点 = 管理员登记的外部系统：按环境（test / prod）存 base_url、认证头（值加密）、allow_intranet、verify_tls。
绑定端点的 HTTP 能力（自定义工具 kind=http，config 写 {endpoint, method, path, query, body}）不由模型给地址：
- 地址只能是登记的 base_url 加上路径模板，路径里的参数逐段 URL 编码后代入（/ ? # 都编码掉），模型拿不到任意 URL；
- 只有登记的端点能开内网访问，开了也照样拦元数据地址和高危端口（tools/net.py）；
- 不跟随跳转：跳到 base_url 之外就不再是登记的那个系统了，3xx 原样交回。

环境：正式运行走 prod；探索运行走 test，端点没有 test 时演练（不发送，environment=simulated，Q4）。读取在探索运行里
没有 test 时走 prod：读不改外部系统，挡掉它只会让问数据用不上这个端点（pick_environment）。

声明了 tabular 的读取把响应落成和查询快照同一个形状的工件（kind=endpoint_snapshot）：{source, columns, rows, truncated,
request, ...}，交给模型的结果再带上 artifact。台账照 query 处理，报告的单元格标记、口径卡取值、运行对比都不用改。
外部响应的原文嵌在快照的 response 里，顶层字段全是平台自己拼的，外部系统伪造不了。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capabilities import secrets as vault
from app.capabilities.registry import EndpointResult, effective_effect
from app.core.config import settings
from app.db.models import Capability, CustomTool, Endpoint
from app.engine.expressions import render_template
from app.tools.net import UnsafeUrlError, assert_safe_url

logger = logging.getLogger(__name__)

ENVIRONMENTS = ("test", "prod")
SNAPSHOT_KIND = "endpoint_snapshot"
#: 落快照最多收多少行，和数据源查询的缺省行数上限一致
MAX_ROWS = 1000
_WHOLE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


class EndpointError(ValueError):
    """端点或绑定它的能力配置不对：给人看的一句话。"""


# ---------------------------------------------------------------- 端点本身


def env_out(env: dict[str, Any] | None) -> dict[str, Any] | None:
    if not env:
        return None
    return {"base_url": env.get("base_url") or "", "headers": vault.masked(env.get("headers")),
            "allow_intranet": bool(env.get("allow_intranet")), "verify_tls": env.get("verify_tls", True) is not False}


def endpoint_out(row: Endpoint) -> dict[str, Any]:
    envs = row.environments or {}
    return {"id": row.id, "name": row.name, "description": row.description or "",
            "environments": {k: env_out(envs.get(k)) for k in ENVIRONMENTS if envs.get(k)},
            "idempotency_header": row.idempotency_header, "timeout_s": row.timeout_s, "version": row.version,
            "updated_by": row.updated_by}


def seal_environments(given: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any]:
    """提交上来的 environments → 存库的样子。只认 test、prod；base_url 必须是 http(s)；头的值加密，提交掩码值保持原值。
    只配了测试环境的也收（接入阶段先在测试环境上试）：正式运行要用的写入能力由发布检查 action.prod_missing 拦。"""
    before = previous or {}
    out: dict[str, Any] = {}
    for key, env in (given or {}).items():
        if key not in ENVIRONMENTS:
            raise EndpointError(f"环境只能是测试（test）或生产（prod），当前为「{key}」")
        if not env:
            continue
        base = str(env.get("base_url") or "").strip().rstrip("/")
        if not re.match(r"^https?://[^/?#]+", base):
            raise EndpointError(f"{_ENV_LABEL[key]}的地址需要以 http:// 或 https:// 开头")
        if "?" in base or "#" in base:
            raise EndpointError(f"{_ENV_LABEL[key]}的地址不能带查询参数，参数请写在能力的配置里")
        headers = vault.seal(env.get("headers"), (before.get(key) or {}).get("headers"))
        # 按掩码换回原值之后再查：界面提交的是掩码，查的是真正要发出去的那一份
        if problem := vault.header_problem(vault.reveal(headers)):
            raise EndpointError(f"{_ENV_LABEL[key]}的认证头有误：{problem}")
        out[key] = {"base_url": base, "headers": headers, "allow_intranet": bool(env.get("allow_intranet")),
                    "verify_tls": env.get("verify_tls", True) is not False}
    if not out:
        raise EndpointError("端点至少需要配置一个环境")
    return out


def idempotency_header_problem(name: str) -> str | None:
    """幂等请求头的名字合不合法：写入时按它带上幂等键，名字不合法每次写入都发不出去。"""
    problem = vault.header_problem({name: ""})
    return f"幂等请求头有误：{problem}" if problem else None


_ENV_LABEL = {"test": "测试环境", "prod": "生产环境"}


def pick_environment(endpoint: Endpoint, *, formal: bool, effect: str) -> str:
    """这一次走哪个环境。正式运行 prod；探索运行 test，没有 test 时写入演练、读取走 prod。"""
    if formal:
        return "prod"
    if (endpoint.environments or {}).get("test"):
        return "test"
    return "prod" if effect == "read" else "simulated"


async def find_endpoint(session: AsyncSession, ref: str | None) -> Endpoint | None:
    """按 id 找，找不到再按名字找：自定义工具的配置里两种写法都认。"""
    if not ref:
        return None
    row = await session.get(Endpoint, ref)
    if row is not None:
        return row
    return (await session.execute(select(Endpoint).where(Endpoint.name == ref))).scalar_one_or_none()


def bound_endpoint_ref(tool: CustomTool) -> str | None:
    """自定义工具绑定的端点（config.endpoint），没绑定为 None。"""
    if tool.kind != "http":
        return None
    ref = (tool.config or {}).get("endpoint")
    return str(ref) if ref else None


# ---------------------------------------------------------------- 拼请求


def _fill(value: Any, args: dict[str, Any]) -> Any:
    """请求体、查询参数里的模板。整个值就是一个 {{ 参数 }} 的按参数原样代入（金额还是数），别的按文字渲染。"""
    if isinstance(value, str):
        whole = _WHOLE.match(value.strip())
        if whole:
            return args.get(whole.group(1))
        return render_template(value, args)
    if isinstance(value, dict):
        return {k: _fill(v, args) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, args) for v in value]
    return value


def render_path(template: str, args: dict[str, Any]) -> str:
    """路径模板逐段代入：每个参数值整段 URL 编码（/ ? # % 都编码掉），模型给的值走不出这一段。. 和 .. 不收：
    它们编码前后一样，拼进去就成了往上走一层。"""
    encoded: dict[str, Any] = {}
    for key, value in args.items():
        text = "" if value is None else str(value)
        if text in (".", ".."):
            raise EndpointError(f"路径参数 {key} 的值不能是「{text}」")
        encoded[key] = quote(text, safe="")
    path = render_template(template or "", encoded)
    return path if not path or path.startswith("/") else f"/{path}"


def build_request(endpoint: Endpoint, environment: str, config: dict[str, Any], args: dict[str, Any]
                  ) -> dict[str, Any]:
    """{method, url, path, query, body}。没写 body 的：GET / HEAD / DELETE 不带请求体，其余把参数整个作为 JSON 请求体。"""
    env = (endpoint.environments or {}).get(environment if environment != "simulated" else "test") \
        or (endpoint.environments or {}).get("prod") or {}
    method = str(config.get("method") or "GET").upper()
    path = render_path(str(config.get("path") or ""), args)
    query = {str(k): v for k, v in (_fill(config.get("query") or {}, args) or {}).items()
             if v is not None and v != ""}
    if "body" in config and config.get("body") is not None:
        body = _fill(config["body"], args)
    else:
        body = None if method in ("GET", "HEAD", "DELETE") else dict(args)
    base = str(env.get("base_url") or "").rstrip("/")
    return {"method": method, "url": f"{base}{path}", "path": path, "query": query, "body": body}


# ---------------------------------------------------------------- 表格化


def pointer(doc: Any, ptr: str) -> Any:
    """JSON 指针（RFC 6901）。取不到为 None。"""
    if ptr in ("", "/"):
        return doc
    cur = doc
    for raw in ptr.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def tabulate(body: Any, tabular: dict[str, Any]) -> tuple[list[str], list[list[Any]], bool]:
    """按 tabular = {rows: 行所在的指针, columns: {列名: 行内指针}} 把响应排成表。"""
    items = pointer(body, str(tabular.get("rows") or ""))
    if not isinstance(items, list):
        raise EndpointError("响应里按「行」的位置找不到列表，请检查能力登记里表格的行位置")
    columns = {str(k): str(v) for k, v in (tabular.get("columns") or {}).items()}
    names = list(columns)
    rows = [[pointer(item, columns[n]) for n in names] for item in items[:MAX_ROWS]]
    return names, rows, len(items) > MAX_ROWS


def masked_columns_of(tabular: dict[str, Any], masked: list[Any]) -> list[str]:
    """遮罩字段（JSON 指针）落在表格的哪几列：快照里记成 mask_columns，证据面板按它遮，口径同查询快照。"""
    hidden = {str(m) for m in masked or []}
    return [str(name) for name, ptr in (tabular.get("columns") or {}).items() if str(ptr) in hidden]


# ---------------------------------------------------------------- 平台执行器


def _client(**kwargs: Any) -> httpx.AsyncClient:
    """发请求用的客户端。测试里换成 MockTransport。"""
    return httpx.AsyncClient(**kwargs)


def _parse(resp: httpx.Response) -> Any:
    content = resp.content[: settings.http_tool_max_bytes]
    text = content.decode(resp.encoding or "utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _resolve(session: AsyncSession, capability: Any, tool: CustomTool | None = None
                   ) -> tuple[Capability | None, CustomTool, Endpoint]:
    """能力 → (登记行, 自定义工具, 端点)。给了 tool 就按它拼请求（工具页试运行的草稿：发的是眼前编辑的配置），没给按能力名
    找已保存的那一行。"""
    name = capability if isinstance(capability, str) else capability.name
    cap = capability if isinstance(capability, Capability) else await session.get(Capability, name)
    if tool is None:
        tool = (await session.execute(select(CustomTool).where(CustomTool.name == name))).scalar_one_or_none()
    if tool is None or bound_endpoint_ref(tool) is None:
        raise EndpointError(f"能力「{name}」没有绑定登记的端点，不能由平台执行")
    endpoint = await find_endpoint(session, bound_endpoint_ref(tool))
    if endpoint is None:
        raise EndpointError(f"能力「{name}」绑定的端点不存在，可能已被删除。请到「工具 → 端点」检查")
    return cap, tool, endpoint


async def preview_request(session: AsyncSession, capability: Any, args: dict[str, Any], *,
                          environment: str) -> dict[str, Any]:
    """这次会发出的请求（不含认证头）：动作确认卡、演练回执用。"""
    _, tool, endpoint = await _resolve(session, capability)
    req = build_request(endpoint, environment, tool.config or {}, args)
    return {**req, "endpoint": endpoint.name, "environment": environment}


REHEARSAL_MESSAGE = "演练：写入能力不在这里真的发送，下面是将要发送的请求"


def rehearsal(endpoint: Endpoint, config: dict[str, Any], capability: Any, args: dict[str, Any]) -> dict[str, Any]:
    """写入能力在工具页试运行、工具库执行（不在运行里）时交回的演练回执：不发送，交回将要发送的方法、地址、查询参数、
    请求体和请求头的名字（值不交回）。地址按探索运行定环境：有测试环境写测试环境的，没有时写正式运行要发往的生产环境。
    对外写入只经调用工具节点发送：要确认、带幂等键、留生效记录（A5）——试运行绕过这些真发一次，打到的就是对方的系统。"""
    envs = endpoint.environments or {}
    environment = "test" if envs.get("test") else "prod"
    req = build_request(endpoint, environment, config, args)
    headers = sorted((envs.get(environment) or {}).get("headers") or {})
    headers.append(endpoint.idempotency_header or "Idempotency-Key")
    if capability is not None and getattr(capability, "credential_mode", None) == "service_on_behalf":
        from app.auth.signing import ASSERTION_HEADER

        headers.append(ASSERTION_HEADER)
    return {"rehearsal": True, "message": REHEARSAL_MESSAGE, "endpoint": endpoint.name, "environment": environment,
            "request": {"method": req["method"], "url": req["url"], "query": req["query"], "body": req["body"],
                        "headers": headers}}


def describe_status(status: int) -> str:
    """对方回的 HTTP 状态码怎么说（非 2xx）。试运行、工具库执行和端点的测试连接共用，说法只有这一份。"""
    if 300 <= status < 400:
        return f"对方返回跳转（HTTP {status}），平台不跟随跳转"
    if status in (401, 403):
        return f"对方拒绝了认证（HTTP {status}）"
    if status == 404:
        return "对方系统上没有这个地址（HTTP 404）"
    if 400 <= status < 500:
        return f"对方拒绝了这个请求（HTTP {status}）"
    if status >= 500:
        return f"对方系统出错（HTTP {status}）"
    return f"对方返回 HTTP {status}"


def not_sent_reason(e: BaseException, timeout_s: float) -> str:
    """请求没拿到响应的原因（测试连接用）。连接没建立、请求在发出之前就被拒（头不合法）和发出之后没回分开说。"""
    text = str(e).lower()
    if isinstance(e, httpx.ConnectTimeout):
        return f"无法连接：{timeout_s:g} 秒内没有连上，请检查地址、端口和网络"
    if isinstance(e, httpx.ConnectError):
        if "certificate" in text or "ssl" in text:
            return "无法连接：对方证书校验没有通过。内网自签的证书可在端点里关闭「校验证书」"
        return "无法连接：地址无法解析、端口上没有服务，或网络不通"
    if isinstance(e, httpx.LocalProtocolError):
        return "请求没有发出：请求头的名称或值不合法，请检查认证头"
    if isinstance(e, httpx.TimeoutException):
        return f"已连接，但对方 {timeout_s:g} 秒内没有响应"
    return "连接中途断开，没有拿到响应"


def on_behalf_parts(on_behalf: Any) -> tuple[str | None, str | None]:
    """on_behalf 的两种写法 → (发起人, 确认人)。

    - 字符串（老写法，工具库试用、agent 里的读取）：就是发起人，没有确认人；
    - {"sub": 发起人, "act": 确认人}：调用工具节点的写入。sub 一律是运行的发起人，确认动作的人放在 act（D-b）。"""
    if isinstance(on_behalf, dict):
        sub, act = on_behalf.get("sub"), on_behalf.get("act")
        return (str(sub) if sub else None), (str(act) if act else None)
    return (str(on_behalf) if on_behalf else None), None


async def endpoint_request(session: AsyncSession, capability: Any, args: dict[str, Any], *, environment: str,
                           idem_key: str | None = None, on_behalf: str | dict[str, Any] | None = None,
                           run_id: str | None = None, node_id: str | None = None,
                           tool: CustomTool | None = None) -> EndpointResult:
    """平台执行器（registry.endpoint_request 的实现，签名同附录 C）。environment 由调用方按运行类别定（pick_environment）；
    传 test 而端点没有测试环境时演练。超时、连接中断交 unknown=True：对方可能已经处理了，写入要转核实，不能重发。
    on_behalf 是发起人（字符串），或者 {"sub": 发起人, "act": 确认人}（on_behalf_parts）。tool 见 _resolve。"""
    try:
        cap, tool, endpoint = await _resolve(session, capability, tool)
        envs = endpoint.environments or {}
        if environment == "test" and not envs.get("test"):
            environment = "simulated"
        req = build_request(endpoint, environment, tool.config or {}, args)
    except EndpointError as e:
        return EndpointResult(status=None, body=None, error=str(e), environment=environment)
    shown = {k: req[k] for k in ("method", "path", "query", "body")}
    if environment == "simulated":
        return EndpointResult(status=None, body={"simulated": True, "endpoint": endpoint.name, "request": shown},
                              environment="simulated")
    env = envs.get(environment)
    if not env:
        return EndpointResult(status=None, body=None, environment=environment,
                              error=f"端点「{endpoint.name}」没有配置{_ENV_LABEL.get(environment, environment)}")
    try:
        await assert_safe_url(req["url"], allow_intranet=bool(env.get("allow_intranet")))
    except UnsafeUrlError as e:
        return EndpointResult(status=None, body=None, error=str(e), environment=environment)

    headers = vault.reveal(env.get("headers"))
    if idem_key:
        headers[endpoint.idempotency_header or "Idempotency-Key"] = idem_key
    if cap is not None and cap.credential_mode == "service_on_behalf":
        from app.auth.signing import ASSERTION_HEADER, assertion

        from app.auth import current_mode

        subject, actor = on_behalf_parts(on_behalf)
        # 开了登录，发起人就是认证过的用户名；关登录时是自报的署名，断言里照实写 verified=false
        headers[ASSERTION_HEADER] = assertion(endpoint.name, subject, actor=actor,
                                              verified=bool(subject) and current_mode() != "off")
    if problem := vault.header_problem(headers):
        # 存量的坏头（加校验之前存进去的 {"": ""}）：发不出去，照实说是哪一项、去哪里改，不是一句 LocalProtocolError
        return EndpointResult(status=None, body=None, environment=environment,
                              error=f"端点「{endpoint.name}」的{_ENV_LABEL.get(environment, environment)}认证头有误："
                                    f"{problem}。请到「工具 → 端点」修改")
    try:
        async with _client(timeout=endpoint.timeout_s or settings.http_tool_timeout, follow_redirects=False,
                           trust_env=False, verify=env.get("verify_tls", True) is not False) as client:
            resp = await client.request(req["method"], req["url"], params=req["query"] or None, headers=headers,
                                        json=req["body"] if req["body"] is not None else None)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # 连接没建立：请求没发出去，是确定的失败
        return EndpointResult(status=None, body=None, error=f"无法连接端点「{endpoint.name}」：{e}",
                              environment=environment)
    except httpx.LocalProtocolError:
        # 请求在发出之前就被拒（头、地址不合法）：同样没发出去，不能当成结果未知去核实
        return EndpointResult(status=None, body=None, environment=environment,
                              error=f"端点「{endpoint.name}」的请求没有发出：请求头的名称或值不合法，"
                                    "请到「工具 → 端点」检查认证头")
    except httpx.HTTPError as e:
        # 发出去之后超时、连接中断：对方可能已经处理了，结果未知
        return EndpointResult(status=None, body=None, unknown=True, environment=environment,
                              error=f"端点「{endpoint.name}」没有返回确定的结果：{type(e).__name__}")

    body = _parse(resp)
    snapshot: str | None = None
    if (cap is not None and cap.tabular and effective_effect(cap) == "read" and 200 <= resp.status_code < 300):
        try:
            snapshot = await _store_snapshot(cap, endpoint, environment, shown, resp.status_code, body,
                                             run_id=run_id, node_id=node_id)
        except EndpointError as e:
            return EndpointResult(status=resp.status_code, body=body, error=str(e), environment=environment)
    return EndpointResult(status=resp.status_code, body=body, snapshot_artifact=snapshot, environment=environment)


async def _store_snapshot(cap: Capability, endpoint: Endpoint, environment: str, request: dict[str, Any],
                          status: int, body: Any, *, run_id: str | None, node_id: str | None) -> str:
    from app.core.artifact_store import put_json

    columns, rows, truncated = tabulate(body, cap.tabular or {})
    content: dict[str, Any] = {"source": endpoint.name, "capability": cap.name, "columns": columns, "rows": rows,
                               "row_count": len(rows), "truncated": truncated,
                               "request": {**request, "environment": environment}, "status": status,
                               "response": body}
    if hidden := masked_columns_of(cap.tabular or {}, cap.masked or []):
        content["mask_columns"] = hidden
    return await put_json(content, kind=SNAPSHOT_KIND, run_id=run_id or "", node_id=node_id or "",
                          meta={"capability": cap.name, "endpoint": endpoint.name})


def tool_result(result: EndpointResult) -> dict[str, Any]:
    """交给模型的结果。落了快照的照查询快照的样子交（带 artifact），别的给状态码和响应体。"""
    if result.error and not result.unknown:
        return {"error": result.error}
    if result.unknown:
        return {"error": result.error, "unknown": True}
    if result.environment == "simulated":
        return {"simulated": True, "message": "演练：探索运行里端点没有测试环境，未发送请求", **(result.body or {})}
    if result.snapshot_artifact:
        from app.core.artifact_store import load

        snapshot = load(result.snapshot_artifact) or {}
        return {"artifact": result.snapshot_artifact,
                **{k: snapshot.get(k) for k in ("source", "columns", "rows", "truncated", "request")}}
    return {"status": result.status, "data": result.body, "environment": result.environment}


async def probe(endpoint: Endpoint, environment: str) -> dict[str, Any]:
    """测一下这个环境：对 base_url 带认证头发一个 GET。{ok, reachable, status, message}：reachable 是连没连上、拿没拿到
    响应；ok 只在 2xx 时为真——能连上但对方回 404（基础地址上没有内容）、401（认证没过）不能说成「连接成功」。"""
    env = (endpoint.environments or {}).get(environment)
    if not env:
        return {"ok": False, "reachable": False, "status": None,
                "message": f"端点「{endpoint.name}」没有配置{_ENV_LABEL.get(environment, environment)}"}
    headers = vault.reveal(env.get("headers"))
    if problem := vault.header_problem(headers):
        return {"ok": False, "reachable": False, "status": None, "message": f"认证头有误，请求没有发出：{problem}"}
    timeout = endpoint.timeout_s or settings.http_tool_timeout
    try:
        await assert_safe_url(env["base_url"], allow_intranet=bool(env.get("allow_intranet")))
        async with _client(timeout=timeout, follow_redirects=False, trust_env=False,
                           verify=env.get("verify_tls", True) is not False) as client:
            resp = await client.get(env["base_url"], headers=headers)
    except UnsafeUrlError as e:
        return {"ok": False, "reachable": False, "status": None, "message": str(e)}
    except httpx.HTTPError as e:
        return {"ok": False, "reachable": False, "status": None, "message": not_sent_reason(e, timeout)}
    status = resp.status_code
    if 200 <= status < 300:
        return {"ok": True, "reachable": True, "status": status, "message": f"连接成功：基础地址返回 HTTP {status}"}
    message = f"能连上，但{describe_status(status)}"
    if status == 404:
        # 不少系统只在具体路径上提供接口，基础地址本身就是 404：说清这一点，别让人以为端点配错了
        message += "。对方只在具体路径上提供接口时这不影响调用，可用一个读取能力试运行确认"
    elif status in (401, 403):
        message += "，请检查认证头"
    return {"ok": False, "reachable": True, "status": status, "message": message}
