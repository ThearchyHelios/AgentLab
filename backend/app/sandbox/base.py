from __future__ import annotations

import abc
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.config import settings

# 入口脚本的前缀。列"这次执行产出了哪些文件"时要把它们排掉——
# 它们是执行机制自己的临时文件，不是用户代码的产出。
ENTRY_PREFIX = "__entry_"


def entry_name(ext: str) -> str:
    """给这一次执行生成独立的入口脚本名。

    同一个会话共用一个工作区（跨节点传文件要靠它），所以入口脚本**不能**
    用固定的 main.py：一张图里并排的两个代码节点会在同一 superstep 并发执行，
    都写 main.py 就会互相覆盖——后写的赢，而每个节点都以为跑的是自己的代码，
    还都返回 ok。这是静默的结果污染，比报错难查得多。
    """
    return f"{ENTRY_PREFIX}{uuid4().hex[:12]}{ext}"


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
    # 这次执行导致会话工作区被重置了（比如 microVM 因超时或限额变更被销毁）。
    # 会话契约说"同 session_id 共享文件系统，可以先写文件下一步再读回来"，
    # 所以工作区没了必须说出来——否则下一个节点只会看到 FileNotFoundError，
    # 完全不知道是谁把文件弄丢的。
    session_reset: bool = False

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
