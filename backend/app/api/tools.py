from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import explain, raw
from app.db.base import get_session
from app.db.models import CustomTool, McpServer
from app.tools.custom import run_custom_tool
from app.tools.mcp_manager import mcp_manager
from app.tools.registry import (
    ToolArgsError, ToolContext, all_specs, build_tools, call_is_dangerous, call_tool, get_spec,
)

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
                # 工作流里 approval=dangerous 的关卡认不认得它。dangerous 说的是「有副作用」，
                # 这一项说的是「运行时真会停下来等审批」——两件事以前混在一个标签里
                "runtime_approval": spec.dangerous,
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
                # 运行时的审批关卡查不到自定义工具的危险标记，不会停下来等人——
                # 在补上之前不能让标签说它会被审批
                "runtime_approval": False,
                "schema": row.parameters or {},
            }
        )

    try:
        out.extend({**t, "source": "mcp", "dangerous": True, "runtime_approval": False}
                   for t in await mcp_manager.list_tools())
    except Exception:  # noqa: BLE001 - MCP 连不上不该让整个工具列表挂掉
        pass
    return out


class RunToolIn(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    # 这个字段会被拼进工作目录路径。净化在 sanitize_session 里兜底，这里
    # 再加一道模式校验做纵深防御——畸形的 session 直接 422 拒掉，
    # 而不是悄悄被净化成 "default" 让调用方以为自己写对了。
    sandbox_session: str = Field(default="playground", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    #: 有副作用的工具要带上它才执行。也可以写在查询串里（?confirm=true）
    confirm: bool = False


def _builtin_effect(name: str, args: dict[str, Any]) -> str:
    if name == "file_write":
        path = args.get("path") or "（没填路径）"
        if args.get("append"):
            return f"会往 playground 工作目录里的「{path}」追加内容"
        return f"会在 playground 工作目录里写入「{path}」，同名文件会被覆盖"
    if name == "http_request":
        return (f"会向 {args.get('url') or '（没填地址）'} 发一个 {args.get('method') or 'GET'} 请求"
                "（内网和回环地址会被拦截）")
    if name == "shell_exec":
        return f"会在沙箱里执行命令：{str(args.get('command') or '')[:120]}"
    if name == "python_exec":
        return "会在沙箱里执行这段 Python 代码"
    if name == "run_code":
        lang = args.get("language") or "python"
        return f"会在沙箱里执行这段 {lang} 代码" + ("，并且允许联网" if args.get("network") else "")
    return f"「{name}」有副作用"


async def _side_effect(name: str, args: dict[str, Any], session: AsyncSession) -> str | None:
    """这次调用有副作用就说清会做什么，没有返回 None。口径和列表上的 dangerous 一致。"""
    spec = get_spec(name)
    if spec is not None:
        return _builtin_effect(name, args) if spec.dangerous else None
    if name.startswith("mcp:"):
        server, _, tool = name[4:].partition("/")
        return (f"会调用 MCP 服务「{server}」上的工具「{tool}」。它是外部进程，"
                "会做什么由它自己决定，这里预知不了")
    row = (await session.execute(
        select(CustomTool).where(CustomTool.name == name)
    )).scalar_one_or_none()
    if row is not None:
        cfg = row.config or {}
        if row.kind == "http":
            return (f"会调用自定义接口「{name}」"
                    f"（{cfg.get('method') or 'GET'} {cfg.get('url') or '（没填地址）'}）")
        return f"会在沙箱里执行自定义工具「{name}」的代码"
    # 数据源工具：只读源上的查询没有副作用，可写源上的写操作有
    tools = await build_tools([name], ToolContext(run_id="playground", node_id="playground"),
                              session=session)
    if tools and call_is_dangerous(tools[0], name, args):
        return f"会在可写数据源上执行写操作：{str(args.get('sql') or '')[:120]}"
    return None


@router.post("/{tool_name:path}/run")
async def run_tool(
    tool_name: str,
    payload: RunToolIn,
    confirm: bool = False,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """直接试跑一个工具。工具面板上的"试一下"用它，不用为了测工具去搭一张图。

    有副作用的工具先 409 说清它会做什么，带上 confirm 再来一次才执行。工作流里
    同一个「需确认」意味着要人工审批，在这里却点一下就真跑了——同一个标签两种
    含义，工具库里试 file_write、shell_exec 的人会以为还有一道关。
    """
    if not (confirm or payload.confirm):
        effect = await _side_effect(tool_name, payload.args, session)
        if effect:
            # 只说后果，不说怎么确认：弹窗还是再点一次是界面的事，写死在这里
            # 放进确认框里读起来就不对
            raise HTTPException(409, f"{effect}。在工具库里执行不经过审批，确认后才会执行。")

    ctx = ToolContext(run_id="playground", node_id="playground",
                      sandbox_session=payload.sandbox_session)
    import time

    started = time.perf_counter()
    try:
        result = await call_tool(tool_name, payload.args, ctx, session=session)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0] if e.args else e)) from e
    except ToolArgsError as e:
        # 参数对不上时报错里已经写清该填什么，原样给
        return {"ok": False, "error": str(e), "hint": "", "detail": raw(e),
                "duration_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        reason, hint = explain(e)
        return {
            "ok": False,
            "error": f"工具执行失败：{reason}",
            "hint": hint,
            "detail": raw(e),
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
        raise HTTPException(409, f"已经有叫「{payload.name}」的工具了，换个名字")
    if payload.name in all_specs():
        raise HTTPException(409, f"「{payload.name}」和内置工具重名了，换个名字")
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
        raise HTTPException(404, "这个工具不存在，可能已经被删了")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


@custom_router.delete("/{tool_id}", status_code=204)
async def delete_custom(tool_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "这个工具不存在，可能已经被删了")
    await session.delete(row)
    await session.commit()


@custom_router.post("/{tool_id}/test")
async def test_custom(
    tool_id: str, payload: RunToolIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "这个工具不存在，可能已经被删了")
    ctx = ToolContext(run_id="playground", node_id="test", sandbox_session="playground")
    try:
        result = await run_custom_tool(session, row.name, payload.args, ctx)
    except Exception as e:  # noqa: BLE001
        reason, hint = explain(e)
        return {"ok": False, "error": f"工具执行失败：{reason}", "hint": hint, "detail": raw(e)}
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
        raise HTTPException(409, f"已经有叫「{payload.name}」的 MCP 服务了，换个名字")
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
        raise HTTPException(404, "这个 MCP 服务不存在，可能已经被删了")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


@mcp_router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "这个 MCP 服务不存在，可能已经被删了")
    await session.delete(row)
    await session.commit()


@mcp_router.post("/servers/{server_id}/probe")
async def probe_server(
    server_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "这个 MCP 服务不存在，可能已经被删了")
    result = await mcp_manager.probe(row)
    row.status = "ok" if result.get("ok") else "error"
    row.last_error = result.get("error")
    row.tools_cache = [t["name"] for t in result.get("tools", [])]
    await session.commit()
    return result


@mcp_router.post("/refresh")
async def refresh_mcp() -> dict[str, Any]:
    return {"servers": await mcp_manager.refresh()}
