from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from langchain_core.tools import BaseTool
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import McpServer

logger = logging.getLogger(__name__)


def _server_config(row: McpServer) -> dict[str, Any]:
    if row.transport == "http":
        return {"transport": "streamable_http", "url": row.url or ""}
    return {
        "transport": "stdio",
        "command": row.command or "",
        "args": list(row.args or []),
        "env": dict(row.env or {}),
    }


def _config_key(row: McpServer) -> str:
    """一个服务的连接配置，缓存据此判断手上的工具是不是按现在这份配置建的。"""
    return json.dumps(_server_config(row), sort_keys=True, ensure_ascii=False)


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
                client = MultiServerMCPClient({row.name: _server_config(row)})
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
            client = MultiServerMCPClient({row.name: _server_config(row)})
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
