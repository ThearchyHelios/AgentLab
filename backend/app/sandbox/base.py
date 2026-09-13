from __future__ import annotations

import abc
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import settings


class SandboxLimits(BaseModel):
    timeout: int = Field(default_factory=lambda: settings.sandbox_timeout)
    memory_mb: int = Field(default_factory=lambda: settings.sandbox_memory_mb)
    cpus: float = Field(default_factory=lambda: settings.sandbox_cpus)
    network: bool = Field(default_factory=lambda: settings.sandbox_network)
    max_output: int = Field(default_factory=lambda: settings.sandbox_max_output)


class ExecResult(BaseModel):
    ok: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    truncated: bool = False
    backend: str = ""
    files_written: list[str] = Field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        if self.timed_out:
            return f"执行超时（>{'?'}s）"
        if self.error:
            return self.error
        return self.stdout or self.stderr or "(无输出)"


class Sandbox(abc.ABC):
    """代码执行后端的统一接口。"""

    name: str = "base"

    @abc.abstractmethod
    async def run(
        self,
        code: str,
        *,
        language: str = "python",
        limits: SandboxLimits | None = None,
        session_id: str | None = None,
        env: dict[str, str] | None = None,
        files: dict[str, str] | None = None,
    ) -> ExecResult:
        """执行一段代码。session_id 相同的调用共享同一个文件系统，
        这样 agent 可以先写文件、再在下一步读回来。"""

    async def health(self) -> dict[str, Any]:
        return {"backend": self.name, "available": True}

    async def cleanup(self, session_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return (
        text[:half] + f"\n\n…（省略 {len(text) - limit} 字符）…\n\n" + text[-half:],
        True,
    )
