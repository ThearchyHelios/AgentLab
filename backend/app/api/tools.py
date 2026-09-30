from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health
from app.core.errors import explain, raw
from app.db.base import get_session
from app.db.models import CustomTool, McpServer
from app.tools.custom import ToolFailure, schema_problem, trial
from app.tools.mcp_manager import mcp_manager
from app.tools.registry import (
    ToolArgsError, ToolBuildError, ToolContext, all_specs, build_tools, call_is_dangerous,
    call_tool, get_spec,
)
from app.tools.trust import load_trust, set_trust

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    """把内置、自定义、MCP 三类工具拉平成一个列表，前端节点面板直接用。"""
    out: list[dict[str, Any]] = []
    trust = await load_trust(session)
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
                # 信任三档（tools/trust.py）：等审批、门控把关都可能停下来等人
                **_trust_fields(row.name, trust),
                "schema": row.parameters or {},
                # 这道关卡之前存进库的坏参数定义：绑上它的节点会失败，列表上先标出来
                "problem": schema_problem(row.parameters or {}),
            }
        )

    try:
        out.extend({**t, "source": "mcp", "dangerous": True, **_trust_fields(t["id"], trust)}
                   for t in await mcp_manager.list_tools())
    except Exception:  # noqa: BLE001 - MCP 连不上不该让整个工具列表挂掉
        pass
    return out


def _trust_fields(key: str, trust: dict[str, str]) -> dict[str, Any]:
    level = trust.get(key, "ask")
    return {"trust": level, "trust_key": key, "runtime_approval": level != "always"}


class TrustIn(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    trust: Literal["ask", "gated", "always"]


@router.put("/trust")
async def put_trust(payload: TrustIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """MCP / 自定义工具的信任三档。改了只影响以后发起的运行：每次运行在发起时快照一份。"""
    await set_trust(session, payload.key, payload.trust)
    return {"key": payload.key, "trust": payload.trust}


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
        path = args.get("path") or "（未填写路径）"
        if args.get("append"):
            return f"将向工作目录中的「{path}」追加内容"
        return f"将在工作目录中写入「{path}」，同名文件会被覆盖"
    if name == "http_request":
        return (f"将向 {args.get('url') or '（未填写地址）'} 发送 {args.get('method') or 'GET'} 请求"
                "（内网和回环地址会被拦截）")
    if name == "shell_exec":
        return f"将在沙箱中执行命令：{str(args.get('command') or '')[:120]}"
    if name == "python_exec":
        return "将在沙箱中执行这段 Python 代码"
    if name == "run_code":
        lang = args.get("language") or "python"
        return f"将在沙箱中执行这段 {lang} 代码" + ("，且允许联网" if args.get("network") else "")
    return f"「{name}」有副作用"


async def _side_effect(name: str, args: dict[str, Any], session: AsyncSession) -> str | None:
    """这次调用有副作用就说清会做什么，没有返回 None。口径和列表上的 dangerous 一致。"""
    spec = get_spec(name)
    if spec is not None:
        return _builtin_effect(name, args) if spec.dangerous else None
    if name.startswith("mcp:"):
        server, _, tool = name[4:].partition("/")
        return (f"将调用 MCP 服务「{server}」上的工具「{tool}」。该工具由外部进程提供，"
                "具体行为无法预先确定")
    row = (await session.execute(
        select(CustomTool).where(CustomTool.name == name)
    )).scalar_one_or_none()
    if row is not None:
        cfg = row.config or {}
        if row.kind == "http":
            return (f"将调用自定义接口「{name}」"
                    f"（{cfg.get('method') or 'GET'} {cfg.get('url') or '（未填写地址）'}）")
        return f"将在沙箱中执行自定义工具「{name}」的代码"
    # 数据源工具：只读源上的查询没有副作用，可写源上的写操作有
    tools = await build_tools([name], ToolContext(run_id="playground", node_id="playground"),
                              session=session)
    if tools and call_is_dangerous(tools[0], name, args):
        return f"将在可写数据源上执行写操作：{str(args.get('sql') or '')[:120]}"
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
            raise HTTPException(409, f"{effect}。在工具库中执行不经过审批，确认后才会执行。")

    ctx = ToolContext(run_id="playground", node_id="playground",
                      sandbox_session=payload.sandbox_session)
    import time

    started = time.perf_counter()
    notes: list[str] = []
    try:
        result = await call_tool(tool_name, payload.args, ctx, session=session, on_fix=notes.append)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0] if e.args else e)) from e
    except (ToolArgsError, ToolBuildError) as e:
        # 参数对不上、工具自己的配置写坏了：报错里已经写清该改什么，原样给
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
        # 参数名写错、被替换成唯一候选跑通了：运行时会报出来，这里也得说
        **({"note": notes[0]} if notes else {}),
    }


# --------------------------------------------------------------------------
# 自定义工具
# --------------------------------------------------------------------------

custom_router = APIRouter(prefix="/api/custom-tools", tags=["tools"])


class CustomToolIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    description: str = ""
    kind: str = "http"  # http | python
    #: JSON Schema。不在这里限定成 dict：写成数组、字符串时要回一句人话（见 _refuse_bad_schema），
    #: 而不是 pydantic 的英文错误列表
    parameters: Any = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CustomToolOut(CustomToolIn):
    id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: 参数定义哪里写坏了（保存时的关卡加上之前存进库的）。绑上它的节点会失败
    problem: str | None = None

    model_config = {"from_attributes": True}


def _custom_out(row: CustomTool) -> CustomToolOut:
    out = CustomToolOut.model_validate(row)
    out.problem = schema_problem(row.parameters or {})
    return out


def _refuse_bad_schema(parameters: Any) -> None:
    """参数定义写坏了就不让存：存进去之后，绑了它的节点在运行时才炸。"""
    problem = schema_problem(parameters)
    if problem:
        raise HTTPException(422, problem)


@custom_router.get("", response_model=list[CustomToolOut])
async def list_custom(session: AsyncSession = Depends(get_session)) -> list[CustomToolOut]:
    return [_custom_out(r) for r in (await session.execute(select(CustomTool))).scalars()]


@custom_router.post("", response_model=CustomToolOut, status_code=201)
async def create_custom(
    payload: CustomToolIn, session: AsyncSession = Depends(get_session)
) -> CustomToolOut:
    if (await session.execute(select(CustomTool).where(CustomTool.name == payload.name))).scalar_one_or_none():
        raise HTTPException(409, f"已存在名为「{payload.name}」的工具，请换一个名称")
    if payload.name in all_specs():
        raise HTTPException(409, f"「{payload.name}」与内置工具重名，请换一个名称")
    _refuse_bad_schema(payload.parameters)
    row = CustomTool(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _custom_out(row)


@custom_router.patch("/{tool_id}", response_model=CustomToolOut)
async def update_custom(
    tool_id: str, payload: CustomToolIn, session: AsyncSession = Depends(get_session)
) -> CustomToolOut:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "工具不存在，可能已被删除")
    _refuse_bad_schema(payload.parameters)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return _custom_out(row)


@custom_router.delete("/{tool_id}", status_code=204)
async def delete_custom(tool_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "工具不存在，可能已被删除")
    await session.delete(row)
    await session.commit()


async def _trial(row: CustomTool, args: dict[str, Any]) -> dict[str, Any]:
    """试跑一次，返回和工具库「执行」同一种形状：{ok, result | error/hint/detail, duration_ms, note?}。"""
    import json
    import time

    ctx = ToolContext(run_id="playground", node_id="test", sandbox_session="playground")
    started = time.perf_counter()

    def took() -> int:
        return int((time.perf_counter() - started) * 1000)

    problem = schema_problem(row.parameters or {})
    if problem:
        # 手写的参数定义写坏了：是配置的事，不是「后端内部出错」
        return {"ok": False, "error": problem,
                "hint": '参数定义是 JSON Schema，格式如 {"type": "object", "properties": '
                        '{"n": {"type": "integer", "description": "…"}}, "required": ["n"]}',
                "detail": "", "duration_ms": 0}
    try:
        result, note = await trial(row, args, ctx)
    except ToolArgsError as e:
        # 参数对不上时报错里已经写清该填什么，原样给
        return {"ok": False, "error": str(e), "hint": "", "detail": raw(e), "duration_ms": took()}
    except Exception as e:  # noqa: BLE001
        reason, hint = explain(e)
        return {"ok": False, "error": f"工具执行失败：{reason}", "hint": hint, "detail": raw(e),
                "duration_ms": took()}
    # 参数名被纠正过：跑通了也要说，用户照着试跑的参数去配工作流
    extra = {"note": note} if note else {}
    if isinstance(result, ToolFailure):
        detail = str(result.get("error") or "")
        if "exit_code" in result:
            error = f"代码执行出错（退出码 {result['exit_code']}）"
            hint = "请根据下方报错修改代码；调用时传入的参数在 args 变量中（一个 dict）"
        else:
            error = f"工具执行失败：{detail.splitlines()[0] if detail else '未提供说明'}"
            hint = "HTTP 工具只能访问公网地址，内网、回环地址会被拦截" if "拦截" in detail else ""
        return {"ok": False, "error": error, "hint": hint,
                "detail": json.dumps(dict(result), ensure_ascii=False), "duration_ms": took(),
                **extra}
    return {"ok": True, "result": result, "duration_ms": took(), **extra}


class CustomToolDraftIn(BaseModel):
    """还没保存（或改了还没保存）的一份工具配置，外加这次试跑的参数。"""

    name: str = "draft_tool"
    kind: str = "http"
    parameters: Any = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    args: dict[str, Any] = Field(default_factory=dict)


def _draft_problem(kind: str, config: dict[str, Any]) -> str | None:
    if kind not in ("http", "python"):
        return f"不支持「{kind}」工具类型：仅支持 http 和 python"
    if kind == "http" and not str(config.get("url") or "").strip():
        return "尚未填写接口地址"
    if kind == "python" and not str(config.get("code") or "").strip():
        return "尚未填写代码"
    return None


@custom_router.post("/test")
async def test_custom_draft(payload: CustomToolDraftIn) -> dict[str, Any]:
    """试跑一份没保存的配置，不落库。

    以前只能试已保存的：新建要先保存才能试，改了代码也得先存——保存等于让节点
    立刻用上一个还没试过的版本。
    """
    problem = _draft_problem(payload.kind, payload.config)
    if problem:
        return {"ok": False, "error": problem, "hint": "", "detail": "", "duration_ms": 0}
    draft = CustomTool(name=payload.name or "draft_tool", kind=payload.kind,
                       parameters=payload.parameters, config=payload.config)
    return await _trial(draft, payload.args)


@custom_router.post("/{tool_id}/test")
async def test_custom(
    tool_id: str, payload: RunToolIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(CustomTool, tool_id)
    if not row:
        raise HTTPException(404, "工具不存在，可能已被删除")
    return await _trial(row, payload.args)


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
    #: 最近一次连接的时刻和耗时（见 app/api/health.py）。status 只有结论，没有「何时」
    last_checked_at: str | None = None
    last_check_ok: bool | None = None
    last_latency_ms: int | None = None

    model_config = {"from_attributes": True}


def _mcp_out(row: McpServer) -> McpOut:
    out = McpOut.model_validate(row)
    checked = health.fields(row.last_check)
    out.last_checked_at = checked["last_checked_at"]
    out.last_check_ok = checked["last_check_ok"]
    out.last_latency_ms = checked["last_latency_ms"]
    return out


def _mcp_connection(row: McpServer) -> tuple[Any, ...]:
    return (row.transport, row.command, list(row.args or []), dict(row.env or {}), row.url)


@mcp_router.get("/servers", response_model=list[McpOut])
async def list_servers(session: AsyncSession = Depends(get_session)) -> list[McpOut]:
    return [_mcp_out(r) for r in (await session.execute(select(McpServer))).scalars()]


@mcp_router.post("/servers", response_model=McpOut, status_code=201)
async def create_server(
    payload: McpIn, session: AsyncSession = Depends(get_session)
) -> McpOut:
    if (await session.execute(select(McpServer).where(McpServer.name == payload.name))).scalar_one_or_none():
        raise HTTPException(409, f"已存在名为「{payload.name}」的 MCP 服务，请换一个名称")
    row = McpServer(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _mcp_out(row)


@mcp_router.patch("/servers/{server_id}", response_model=McpOut)
async def update_server(
    server_id: str, payload: McpIn, session: AsyncSession = Depends(get_session)
) -> McpOut:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "MCP 服务不存在，可能已被删除")
    before, old_name, was_enabled = _mcp_connection(row), row.name, row.enabled
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    if _mcp_connection(row) != before:
        # 启动命令、地址换了，上次的「连得上」说的是另一个进程
        row.status, row.last_error, row.last_check = "unknown", None, None
    await session.commit()
    await session.refresh(row)
    if (_mcp_connection(row), row.name, row.enabled) != (before, old_name, was_enabled):
        # 按旧配置、旧名字建的工具作废，下次用到时按新的连
        mcp_manager.invalidate(old_name, row.name)
    return _mcp_out(row)


@mcp_router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "MCP 服务不存在，可能已被删除")
    await session.delete(row)
    await session.commit()
    mcp_manager.invalidate(row.name)


@mcp_router.post("/servers/{server_id}/probe")
async def probe_server(
    server_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await session.get(McpServer, server_id)
    if not row:
        raise HTTPException(404, "MCP 服务不存在，可能已被删除")
    import time

    started = time.perf_counter()
    result = await mcp_manager.probe(row)
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    row.status = "ok" if result.get("ok") else "error"
    row.last_error = result.get("error")
    row.tools_cache = [t["name"] for t in result.get("tools", [])]
    row.last_check = health.record(result.get("ok"), result["elapsed_ms"], result.get("error"))
    await session.commit()
    return result


@mcp_router.post("/refresh")
async def refresh_mcp() -> dict[str, Any]:
    return {"servers": await mcp_manager.refresh()}
