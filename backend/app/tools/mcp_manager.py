from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from langchain_core.tools import BaseTool
from sqlalchemy import select

from app.capabilities import secrets as vault
from app.db.base import SessionLocal
from app.db.models import McpServer

logger = logging.getLogger(__name__)


def _server_config(row: McpServer) -> dict[str, Any]:
    """连接配置。env、headers 的值存的是密文（A6 第 6 条），这里解开交给客户端；接口只回掩码。
    绑定了登记端点的 http 服务，地址和认证头在连接前由 _endpoint_config 换成端点的。"""
    if row.transport == "http":
        config: dict[str, Any] = {"transport": "streamable_http", "url": row.url or ""}
        if row.headers:
            config["headers"] = vault.reveal(row.headers)
        return config
    return {
        "transport": "stdio",
        "command": row.command or "",
        "args": list(row.args or []),
        "env": vault.reveal(row.env),
    }


def _config_key(row: McpServer) -> str:
    """一个服务的连接配置，缓存据此判断手上的工具是不是按现在这份配置建的。按存库的样子算（密文），明文不进缓存键。"""
    return json.dumps({"transport": row.transport, "command": row.command, "args": list(row.args or []),
                       "env": dict(row.env or {}), "url": row.url, "headers": dict(row.headers or {}),
                       "endpoint_id": row.endpoint_id}, sort_keys=True, ensure_ascii=False)


async def check_url(row: McpServer) -> dict[str, Any]:
    """http 服务连接前的地址检查，交回连接配置。

    没绑定端点的：地址整串过 SSRF 检查（tools/net.py），和模型给的地址一样对待——MCP 的地址是搭建者填的，搭建者不能
    借它访问内网。绑定了登记端点的：地址是端点生产环境的 base_url 拼上这里填的路径，认证头是端点的加上这里的，
    内网访问跟端点的开关走（元数据地址照样拦）。stdio 不经网络，原样交回。"""
    config = _server_config(row)
    if row.transport != "http":
        return config
    from app.tools.net import UnsafeUrlError, assert_safe_url

    if not row.endpoint_id:
        try:
            await assert_safe_url(config["url"])
        except UnsafeUrlError as e:
            # 指向内网、本机的 MCP 服务要绑定登记的端点（管理员开内网访问），这里说清去哪改
            raise UnsafeUrlError(f"{e}。内网或本机上的 MCP 服务请改为绑定登记的端点：由管理员在「工具 → 端点」登记并开启"
                                 "内网访问，再在这个服务里选择该端点、地址只填路径") from e
        return config
    from app.db.models import Endpoint

    async with SessionLocal() as session:
        endpoint = await session.get(Endpoint, row.endpoint_id)
    env = ((endpoint.environments or {}).get("prod") if endpoint else None) or {}
    if not env.get("base_url"):
        raise UnsafeUrlError("绑定的端点不存在或没有配置生产环境")
    path = (row.url or "").strip()
    if path.startswith(("http://", "https://")):
        raise UnsafeUrlError("绑定端点时，地址只填路径（例如 /mcp），主机和协议由端点决定")
    url = env["base_url"].rstrip("/") + (path if not path or path.startswith("/") else f"/{path}")
    await assert_safe_url(url, allow_intranet=bool(env.get("allow_intranet")))
    headers = {**vault.reveal(env.get("headers")), **vault.reveal(row.headers)}
    return {"transport": "streamable_http", "url": url, **({"headers": headers} if headers else {})}


#: 升级后第一次启动时，存量的内网 MCP 服务改为绑定登记端点（adopt_intranet_servers）。审计里有这一条就是做过了
ADOPT_ACTION = "mcp.intranet_review"
#: 每个服务的地址检查最多等这么久（解析域名）：启动时做，不能让一个解析不了的域名拖住服务
_ADOPT_CHECK_S = 10


async def adopt_intranet_servers() -> list[dict[str, Any]]:
    """升级前建的 http MCP 服务没有绑定端点、地址也不过 SSRF 检查——升级之后没绑定端点的地址一律按模型给的地址对待
    （check_url），指向 localhost、内网主机的存量服务就连不上了。这里在升级后第一次启动时把它们各自改成绑定一个登记的
    端点：生产环境就是它原来的主机（开内网访问，元数据地址和高危端口照样拦），服务的地址改成路径，连接和以前一样。每一项
    写审计（endpoint.adopt_mcp），管理员事后能在「工具 → 端点」里看到、收紧。

    只做一次：做完写一条 ADOPT_ACTION 审计，之后的启动看到它就不再做。之后新建的服务照规则走（内网只有登记的端点能访问，
    开内网访问是管理员的事）——不然搭建者先填一个公网域名、保存后把它改指向内网，下次重启就被自动收编了。
    交回收编了的 [{server, endpoint, base_url}]。"""
    from urllib.parse import urlsplit, urlunsplit

    from app.auth.audit import record
    from app.db.models import AuditEvent, Endpoint
    from app.tools.net import UnsafeUrlError, assert_safe_url

    async with SessionLocal() as session:
        if (await session.execute(select(AuditEvent.id).where(AuditEvent.action == ADOPT_ACTION).limit(1))).first():
            return []
        servers = list((await session.execute(select(McpServer).where(
            McpServer.transport == "http", McpServer.endpoint_id.is_(None)))).scalars())
        taken = set((await session.execute(select(Endpoint.name))).scalars())
        adopted: list[dict[str, Any]] = []
        for server in servers:
            url = (server.url or "").strip()
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                continue
            try:
                await asyncio.wait_for(assert_safe_url(url), _ADOPT_CHECK_S)
                continue                      # 公网地址：照旧不绑定
            except (UnsafeUrlError, asyncio.TimeoutError):
                pass
            try:
                # 开了内网访问也照样拦的（元数据地址、高危端口、解析不了的）不收编：以前就连不上，或者本来就不该连
                await asyncio.wait_for(assert_safe_url(url, allow_intranet=True), _ADOPT_CHECK_S)
            except (UnsafeUrlError, asyncio.TimeoutError):
                continue
            base = f"{parts.scheme}://{parts.netloc}"
            name = _free_name(f"mcp-{'-'.join(server.name.split()).replace('/', '-')}", taken)
            taken.add(name)
            endpoint = Endpoint(name=name, description=f"升级时为 MCP 服务「{server.name}」自动登记（原地址指向内网）",
                                environments={"prod": {"base_url": base, "headers": {}, "allow_intranet": True,
                                                       "verify_tls": True}},
                                updated_by=None)
            session.add(endpoint)
            await session.flush()
            server.endpoint_id = endpoint.id
            server.url = urlunsplit(("", "", parts.path, parts.query, parts.fragment))
            item = {"server": server.name, "endpoint": name, "base_url": base}
            await record(session, "endpoint.adopt_mcp", "endpoint", endpoint.id, actor=None, verified=False,
                         data={**item, "allow_intranet": True})
            adopted.append(item)
        await record(session, ADOPT_ACTION, "endpoint", None, actor=None, verified=False,
                     data={"adopted": adopted})
        await session.commit()
    if adopted:
        mcp_manager.invalidate(*(a["server"] for a in adopted))
    return adopted


def _free_name(wanted: str, taken: set[str]) -> str:
    name, n = wanted[:100], 2
    while name in taken:
        name = f"{wanted[:95]}-{n}"
        n += 1
    return name


class McpManager:
    """管理外部 MCP server 的连接与工具缓存。

    工具在图里用 `mcp:<server>/<tool>` 引用。连接是懒建立的，
    并且按 server 缓存，避免每个节点都去重新拉起一次 stdio 子进程。

    缓存按服务逐个补、逐个作废。以前只在「一个都没加载过」时整批建一次：改了
    启动命令，运行照样用旧命令建出来的工具；删掉的服务照样调得到；缓存建好之后
    新加的服务，工具列表里一直没有它，除非有人去点刷新。现在每次取工具都先对一遍
    库里启用着的服务：缺的连上，配置变了的重连，删了、停用了的不再交出去。
    """

    def __init__(self) -> None:
        self._tools: dict[str, list[BaseTool]] = {}  # server name -> tools
        #: 每个服务的工具是按哪份连接配置建的（_config_key）
        self._built_from: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def invalidate(self, *names: str) -> None:
        """服务改了、删了：丢掉按旧配置建的工具，下次用到时再连。"""
        for name in names:
            self._tools.pop(name, None)
            self._built_from.pop(name, None)

    def cached_tools(self, name: str) -> list[BaseTool]:
        """这个服务已经连上过、缓存着的工具（不去连）。能力登记同步时取描述、参数和 readOnlyHint 用。"""
        return list(self._tools.get(name, []))

    def _fresh(self, row: McpServer) -> bool:
        return row.name in self._tools and self._built_from.get(row.name) == _config_key(row)

    async def _load_servers(self, names: list[str] | None = None) -> list[McpServer]:
        async with SessionLocal() as session:
            query = select(McpServer).where(McpServer.enabled.is_(True))
            if names is not None:
                query = query.where(McpServer.name.in_(names))
            rows = await session.execute(query)
            return list(rows.scalars())

    async def _connect(self, rows: list[McpServer]) -> None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        for row in rows:
            key = _config_key(row)
            started = time.perf_counter()
            try:
                client = MultiServerMCPClient({row.name: await check_url(row)})
                tools = list(await asyncio.wait_for(client.get_tools(), timeout=30))
                status, error = "ok", None
            except Exception as e:  # noqa: BLE001 - 一个 server 挂了不影响其他的
                logger.warning("MCP server %s 连接失败: %s", row.name, e)
                tools, status, error = [], "error", _reason(e, row.command)
            # 连不上也缓存成空：不然每次取工具都要再等一轮 30 秒超时。改配置或刷新时重连
            self._tools[row.name] = tools
            self._built_from[row.name] = key
            await self._mark(row.id, key, status, error, [t.name for t in tools], started)

    async def _mark(
        self, server_id: str, key: str, status: str, error: str | None, tool_names: list[str],
        started: float,
    ) -> None:
        from app.api.health import record

        async with SessionLocal() as session:
            row = await session.get(McpServer, server_id)
            # 连到一半服务被改了：这次连的是旧配置，它的结论不能盖掉改完之后的「未检查」
            if row and _config_key(row) == key:
                row.status = status
                row.last_error = error
                row.tools_cache = tool_names
                # 加载工具本身就是一次连接：记下来，工具页不用再单独测一次才知道
                row.last_check = record(
                    status == "ok", int((time.perf_counter() - started) * 1000), error)
                await session.commit()

    async def _ensure(self, rows: list[McpServer]) -> None:
        """rows 里还没连过、或者是按旧配置连的，连一次补上。"""
        if all(self._fresh(r) for r in rows):
            return
        async with self._lock:
            # 等锁的时候别人可能已经连好了
            stale = [r for r in rows if not self._fresh(r)]
            if stale:
                await self._connect(stale)

    async def refresh(self) -> dict[str, list[str]]:
        """全部重连一遍（服务端的工具变了，配置没变时用）。"""
        async with self._lock:
            rows = await self._load_servers()
            self.invalidate(*(set(self._tools) - {r.name for r in rows}))
            await self._connect(rows)
        return {r.name: [t.name for t in self._tools.get(r.name, [])] for r in rows}

    async def ensure_loaded(self) -> None:
        await self._ensure(await self._load_servers())

    async def list_tools(self) -> list[dict[str, Any]]:
        rows = await self._load_servers()
        await self._ensure(rows)
        out: list[dict[str, Any]] = []
        for row in rows:
            server = row.name
            for tool in self._tools.get(server, []):
                out.append(
                    {
                        "id": f"mcp:{server}/{tool.name}",
                        "name": tool.name,
                        "server": server,
                        "description": tool.description or "",
                        "category": f"MCP · {server}",
                        "schema": tool.args_schema.model_json_schema()
                        if getattr(tool, "args_schema", None)
                        else {},
                    }
                )
        return out

    async def get_tools(self, refs: list[str]) -> list[BaseTool]:
        """按 `mcp:<server>/<tool>` 取工具；`mcp:<server>/*` 表示该 server 全部工具。

        只交出库里还在、并且启用着的服务的工具：删了、停用了的，缓存里就算还有也不给。
        """
        parsed = []
        for ref in refs:
            body = ref[4:] if ref.startswith("mcp:") else ref
            server, _, tool_name = body.partition("/")
            parsed.append((server, tool_name))
        rows = await self._load_servers(sorted({server for server, _ in parsed}))
        await self._ensure(rows)
        live = {r.name for r in rows}
        wanted: list[BaseTool] = []
        for server, tool_name in parsed:
            tools = self._tools.get(server, []) if server in live else []
            if tool_name in ("", "*"):
                wanted.extend(_keyed(server, t) for t in tools)
                continue
            for tool in tools:
                if tool.name == tool_name:
                    wanted.append(_keyed(server, tool))
                    break
        return wanted

    async def probe(self, row: McpServer) -> dict[str, Any]:
        """连一次看看，用于设置页的"测试连接"。"""
        from langchain_mcp_adapters.client import MultiServerMCPClient

        try:
            client = MultiServerMCPClient({row.name: await check_url(row)})
            tools = await asyncio.wait_for(client.get_tools(), timeout=30)
            return {
                "ok": True,
                "tools": [
                    {"name": t.name, "description": t.description or ""} for t in tools
                ],
            }
        except Exception as e:  # noqa: BLE001
            from app.core.errors import raw

            return {"ok": False, "error": _reason(e, row.command), "detail": raw(e)}


def _reason(e: BaseException, command: str | None = None) -> str:
    """连不上 MCP 服务的原因，写成工具页上看得懂的话。

    最常见的两种：命令不存在（没装 npx / uvx）和超时（进程起来了但不讲 MCP）。
    """
    from app.core.errors import explain
    from app.tools.net import UnsafeUrlError

    if isinstance(e, UnsafeUrlError):
        return f"地址未通过安全检查：{e}"
    if isinstance(e, FileNotFoundError):
        # stdio 客户端抛的这个异常常常不带文件名，只剩「找不到命令 」半句：补上配置的命令
        missing = e.filename or command or ""
        return f"无法启动：找不到命令 {missing}。请确认已安装，且位于服务端的 PATH 中"
    if isinstance(e, asyncio.TimeoutError):
        return "已连接，但 30 秒内没有响应：请确认该命令启动的是 MCP 服务，并检查参数"
    reason, hint = explain(e)
    return f"{reason}。{hint}" if hint else reason


mcp_manager = McpManager()


def _keyed(server: str, tool: BaseTool) -> BaseTool:
    """标上信任三档用的 key（`mcp:<server>/<tool>`，和工具列表里的 id 一致）。

    缓存里的工具对象是共用的，key 只由 server 和工具名决定，重复标同一个值不会串。
    """
    tool.metadata = {**(tool.metadata or {}), "trust_key": f"mcp:{server}/{tool.name}"}
    return tool
