from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import CustomTool, McpServer
from app.tools.custom import run_custom_tool
from app.tools.mcp_manager import mcp_manager
from app.tools.registry import ToolContext, all_specs, call_tool

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    """把内置、自定义、MCP 三类工具拉平成一个列表，前端节点面板直接用。"""
    out: list[dict[str, Any]] = []
    for name, spec in sorted(all_specs().items()):
        out.append(
            {
                "id": name,
                "name": name,
                "description": spec.description,
                "category": spec.category,
                "source": "builtin",
                "dangerous": spec.dangerous,
                "schema": spec.json_schema(),
            }
        )

    rows = (
        await session.execute(select(CustomTool).where(CustomTool.enabled.is_(True)))
    ).scalars()
    for row in rows:
        out.append(
            {
                "id": row.name,
                "name": row.name,
                "description": row.description,
                "category": f"自定义 · {row.kind}",
                "source": "custom",
                "dangerous": True,
                "schema": row.parameters or {},
            }
        )

    try:
        out.extend({**t, "source": "mcp", "dangerous": True} for t in await mcp_manager.list_tools())
    except Exception:  # noqa: BLE001 - MCP 连不上不该让整个工具列表挂掉
        pass
    return out


class RunToolIn(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    sandbox_session: str = "playground"


@router.post("/{tool_name:path}/run")
async def run_tool(
    tool_name: str, payload: RunToolIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """直接试跑一个工具。工具面板上的"试一下"用它，不用为了测工具去搭一张图。"""
    ctx = ToolContext(run_id="playground", node_id="playground",
                      sandbox_session=payload.sandbox_session)
    import time

    started = time.perf_counter()
    try:
        result = await call_tool(tool_name, payload.args, ctx, session=session)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    return {
        "ok": True,
        "result": result,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


# --------------------------------------------------------------------------
# 自定义工具
# --------------------------------------------------------------------------

custom_router = APIRouter(prefix="/api/custom-tools", tags=["tools"])


class CustomToolIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    description: str = ""
    kind: str = "http"  # http | python
    parameters: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CustomToolOut(CustomToolIn):
    id: str

    model_config = {"from_attributes": True}


@custom_router.get("", response_model=list[CustomToolOut])
async def list_custom(session: AsyncSession = Depends(get_session)) -> list[CustomTool]:
    return list((await session.execute(select(CustomTool))).scalars())


@custom_router.post("", response_model=CustomToolOut, status_code=201)
async def create_custom(
    payload: CustomToolIn, session: AsyncSession = Depends(get_session)
) -> CustomTool:
    if (await session.execute(select(CustomTool).where(CustomTool.name == payload.name))).scalar_one_or_none():
        raise HTTPException(409, "同名工具已存在")
    if payload.name in all_specs():
        raise HTTPException(409, "这个名字和内置工具冲突")
    row = CustomTool(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@custom_router.patch("/{tool_id}", response_model=CustomToolOut)
async def update_custom(
    tool_id: str, payload: CustomToolIn, session: AsyncSession = Depends(get_session)
) -> CustomTool:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "工具不存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


@custom_router.delete("/{tool_id}", status_code=204)
async def delete_custom(tool_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "工具不存在")
    await session.delete(row)
    await session.commit()


@custom_router.post("/{tool_id}/test")
async def test_custom(
    tool_id: str, payload: RunToolIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "工具不存在")
    ctx = ToolContext(run_id="playground", node_id="test", sandbox_session="playground")
    try:
        result = await run_custom_tool(session, row.name, payload.args, ctx)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "result": result}


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------

mcp_router = APIRouter(prefix="/api/mcp", tags=["tools"])


class McpIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    transport: str = "stdio"  # stdio | http
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    enabled: bool = True


class McpOut(McpIn):
    id: str
    status: str
    last_error: str | None
    tools_cache: list[Any]

    model_config = {"from_attributes": True}


@mcp_router.get("/servers", response_model=list[McpOut])
async def list_servers(session: AsyncSession = Depends(get_session)) -> list[McpServer]:
    return list((await session.execute(select(McpServer))).scalars())


@mcp_router.post("/servers", response_model=McpOut, status_code=201)
async def create_server(
    payload: McpIn, session: AsyncSession = Depends(get_session)
) -> McpServer:
    if (await session.execute(select(McpServer).where(McpServer.name == payload.name))).scalar_one_or_none():
        raise HTTPException(409, "同名 MCP server 已存在")
    row = McpServer(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@mcp_router.patch("/servers/{server_id}", response_model=McpOut)
async def update_server(
    server_id: str, payload: McpIn, session: AsyncSession = Depends(get_session)
) -> McpServer:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "MCP server 不存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


@mcp_router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "MCP server 不存在")
    await session.delete(row)
    await session.commit()


@mcp_router.post("/servers/{server_id}/probe")
async def probe_server(
    server_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "MCP server 不存在")
    result = await mcp_manager.probe(row)
    row.status = "ok" if result.get("ok") else "error"
    row.last_error = result.get("error")
    row.tools_cache = [t["name"] for t in result.get("tools", [])]
    await session.commit()
    return result


@mcp_router.post("/refresh")
async def refresh_mcp() -> dict[str, Any]:
    return {"servers": await mcp_manager.refresh()}
