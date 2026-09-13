from __future__ import annotations

import asyncio
import logging
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


class McpManager:
    """管理外部 MCP server 的连接与工具缓存。

    工具在图里用 `mcp:<server>/<tool>` 引用。连接是懒建立的，
    并且按 server 缓存，避免每个节点都去重新拉起一次 stdio 子进程。
    """

    def __init__(self) -> None:
        self._tools: dict[str, list[BaseTool]] = {}  # server name -> tools
        self._lock = asyncio.Lock()

    async def _load_servers(self) -> list[McpServer]:
        async with SessionLocal() as session:
            rows = await session.execute(select(McpServer).where(McpServer.enabled.is_(True)))
            return list(rows.scalars())

    async def _connect(self, rows: list[McpServer]) -> dict[str, list[BaseTool]]:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        out: dict[str, list[BaseTool]] = {}
        for row in rows:
            try:
                client = MultiServerMCPClient({row.name: _server_config(row)})
                tools = await asyncio.wait_for(client.get_tools(), timeout=30)
                out[row.name] = list(tools)
                await self._mark(row.id, "ok", None, [t.name for t in tools])
            except Exception as e:  # noqa: BLE001 - 一个 server 挂了不影响其他的
                logger.warning("MCP server %s 连接失败: %s", row.name, e)
                out[row.name] = []
                await self._mark(row.id, "error", f"{type(e).__name__}: {e}", [])
        return out

    async def _mark(
        self, server_id: str, status: str, error: str | None, tool_names: list[str]
    ) -> None:
        async with SessionLocal() as session:
            row = await session.get(McpServer, server_id)
            if row:
                row.status = status
                row.last_error = error
                row.tools_cache = tool_names
                await session.commit()

    async def refresh(self) -> dict[str, list[str]]:
        async with self._lock:
            rows = await self._load_servers()
            self._tools = await self._connect(rows)
        return {name: [t.name for t in tools] for name, tools in self._tools.items()}

    async def ensure_loaded(self) -> None:
        if not self._tools:
            await self.refresh()

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.ensure_loaded()
        out: list[dict[str, Any]] = []
        for server, tools in self._tools.items():
            for tool in tools:
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
        """按 `mcp:<server>/<tool>` 取工具；`mcp:<server>/*` 表示该 server 全部工具。"""
        await self.ensure_loaded()
        wanted: list[BaseTool] = []
        for ref in refs:
            body = ref[4:] if ref.startswith("mcp:") else ref
            server, _, tool_name = body.partition("/")
            tools = self._tools.get(server, [])
            if tool_name in ("", "*"):
                wanted.extend(tools)
                continue
            for tool in tools:
                if tool.name == tool_name:
                    wanted.append(tool)
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
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


mcp_manager = McpManager()
