from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.core.config import settings
from app.sandbox.base import ExecResult, Sandbox, SandboxLimits
from app.sandbox.bubblewrap_sandbox import BubblewrapSandbox
from app.sandbox.local_sandbox import LocalSandbox
from app.sandbox.seatbelt_sandbox import SeatbeltSandbox


class DisabledSandbox(Sandbox):
    name = "off"

    async def run(self, code: str, **kwargs: Any) -> ExecResult:  # type: ignore[override]
        return ExecResult(
            ok=False,
            backend=self.name,
            error="沙箱已在设置中关闭（AGENTLAB_SANDBOX_BACKEND=off）",
        )

    async def health(self) -> dict[str, Any]:
        return {"backend": self.name, "available": False, "isolated": False, "reason": "已关闭"}


# 按隔离强度从高到低排，auto 模式挑第一个能用的
_BACKENDS: list[tuple[str, type[Sandbox]]] = [
    ("bubblewrap", BubblewrapSandbox),
    ("seatbelt", SeatbeltSandbox),
    ("local", LocalSandbox),
]


class SandboxManager:
    """挑选并持有代码执行后端。

    用操作系统自带的隔离原语，不依赖容器运行时：macOS 走 Seatbelt
    （`sandbox-exec`，系统自带），Linux 走 bubblewrap。两者都是进程级启动，
    没有虚拟机开销，冷启动在几十毫秒量级。
    """

    def __init__(self) -> None:
        self._backend: Sandbox | None = None
        self._lock = asyncio.Lock()
        self._resolved_from: str = ""

    async def get(self) -> Sandbox:
        if self._backend is not None:
            return self._backend
        async with self._lock:
            if self._backend is not None:
                return self._backend
            self._backend = self._resolve()
            return self._backend

    def _resolve(self) -> Sandbox:
        choice = (settings.sandbox_backend or "auto").lower()

        if choice == "off":
            self._resolved_from = "配置为关闭"
            return DisabledSandbox()

        named = dict(_BACKENDS)
        if choice in named:
            impl = named[choice]
            backend = impl()
            available = getattr(impl, "available", lambda: True)()
            self._resolved_from = (
                f"配置指定 {choice}" if available
                else f"配置指定 {choice}，但当前环境不可用"
            )
            return backend

        for name, impl in _BACKENDS:
            if getattr(impl, "available", lambda: True)():
                self._resolved_from = f"自动选择 {name}（{_why(name)}）"
                return impl()

        self._resolved_from = "没有可用的隔离后端，降级到本地子进程"
        return LocalSandbox()

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
        backend = await self.get()
        return await backend.run(
            code,
            language=language,
            limits=limits,
            session_id=session_id,
            env=env,
            files=files,
        )

    async def health(self) -> dict[str, Any]:
        backend = await self.get()
        info = await backend.health()
        info["selected_because"] = self._resolved_from
        info["configured"] = settings.sandbox_backend
        info["defaults"] = SandboxLimits().model_dump()
        info["candidates"] = [
            {"name": name, "available": getattr(impl, "available", lambda: True)()}
            for name, impl in _BACKENDS
        ]
        return info

    async def cleanup(self, session_id: str) -> None:
        backend = await self.get()
        await backend.cleanup(session_id)

    async def close(self) -> None:
        if self._backend:
            await self._backend.close()


def _why(name: str) -> str:
    return {
        "seatbelt": "macOS 自带 sandbox-exec",
        "bubblewrap": "检测到 bwrap",
        "local": "无可用隔离原语",
    }.get(name, "")


sandbox_manager = SandboxManager()
