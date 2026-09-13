from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.sandbox.base import SandboxLimits
from app.sandbox.manager import sandbox_manager
from app.tools.registry import ToolContext, register


class PythonExecArgs(BaseModel):
    code: str = Field(description="要执行的 Python 代码。用 print() 输出结果。")
    timeout: int = Field(default=30, ge=1, le=300, description="超时秒数")


@register(
    name="python_exec",
    category="沙箱",
    description=(
        "在隔离沙箱里执行 Python 代码并返回 stdout/stderr。"
        "默认断网、只读根文件系统，/workspace 可写且在同一次运行内持久。"
    ),
    args_schema=PythonExecArgs,
    dangerous=True,
)
async def python_exec(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = PythonExecArgs(**kwargs)
    result = await sandbox_manager.run(
        args.code,
        language="python",
        limits=SandboxLimits(timeout=args.timeout),
        session_id=ctx.sandbox_session,
    )
    return result.model_dump(exclude={"files_written"} if not result.files_written else set())


class ShellExecArgs(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    timeout: int = Field(default=30, ge=1, le=300)


@register(
    name="shell_exec",
    category="沙箱",
    description="在隔离沙箱里执行 shell 命令。同样断网、非 root。",
    args_schema=ShellExecArgs,
    dangerous=True,
)
async def shell_exec(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = ShellExecArgs(**kwargs)
    result = await sandbox_manager.run(
        args.command,
        language="bash",
        limits=SandboxLimits(timeout=args.timeout),
        session_id=ctx.sandbox_session,
    )
    return result.model_dump()


class RunCodeArgs(BaseModel):
    code: str
    language: Literal["python", "bash", "node"] = "python"
    timeout: int = Field(default=30, ge=1, le=300)
    network: bool = Field(default=False, description="是否允许联网")


@register(
    name="run_code",
    category="沙箱",
    description="在沙箱里执行任意支持语言的代码，可单独打开网络。",
    args_schema=RunCodeArgs,
    dangerous=True,
)
async def run_code(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = RunCodeArgs(**kwargs)
    result = await sandbox_manager.run(
        args.code,
        language=args.language,
        limits=SandboxLimits(timeout=args.timeout, network=args.network),
        session_id=ctx.sandbox_session,
    )
    return result.model_dump()
