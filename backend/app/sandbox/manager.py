from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import settings
from app.sandbox.base import ExecResult, Sandbox, SandboxLimits
from app.sandbox.docker_sandbox import DockerSandbox
from app.sandbox.local_sandbox import LocalSandbox


class DisabledSandbox(Sandbox):
    name = "off"

    async def run(self, code: str, **kwargs: Any) -> ExecResult:  # type: ignore[override]
        return ExecResult(
            ok=False,
            backend=self.name,
            error="沙箱已在设置中关闭（AGENTLAB_SANDBOX_BACKEND=off）",
        )

    async def health(self) -> dict[str, Any]:
        return {"backend": self.name, "available": False, "reason": "已关闭"}


class SandboxManager:
    """按配置挑一个可用后端。auto = 有 Docker 用 Docker，没有就降级到本地子进程。"""

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
            self._backend = await self._resolve()
            return self._backend

    async def _resolve(self) -> Sandbox:
        choice = (settings.sandbox_backend or "auto").lower()
        if choice == "off":
            self._resolved_from = "配置关闭"
            return DisabledSandbox()
        if choice == "local":
            self._resolved_from = "配置指定"
            return LocalSandbox()
        if choice == "docker":
            self._resolved_from = "配置指定"
            return DockerSandbox()

        docker = DockerSandbox()
        health = await docker.health()
        if health.get("available"):
            self._resolved_from = "自动探测到 Docker"
            await docker.reap_orphans()
            return docker
        self._resolved_from = f"Docker 不可用（{health.get('error', '未知原因')}），降级到本地"
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
        return info

    async def cleanup(self, session_id: str) -> None:
        backend = await self.get()
        await backend.cleanup(session_id)

    async def close(self) -> None:
        if self._backend:
            await self._backend.close()


sandbox_manager = SandboxManager()
