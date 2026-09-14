from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.core.config import settings
from app.sandbox.base import ExecResult, Sandbox, SandboxLimits
from app.sandbox.bubblewrap_sandbox import BubblewrapSandbox
from app.sandbox.local_sandbox import LocalSandbox
from app.sandbox.microvm_sandbox import MicroVMSandbox
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


# 按隔离强度从高到低排，auto 模式挑第一个能用的。
# microVM 排在最前：它是唯一能真正限住内存、隔离进程表的一档。
_BACKENDS: list[tuple[str, type[Sandbox]]] = [
    ("microvm", MicroVMSandbox),
    ("bubblewrap", BubblewrapSandbox),
    ("seatbelt", SeatbeltSandbox),
    ("local", LocalSandbox),
]


class SandboxManager:
    """挑选并持有代码执行后端。

    隔离强度从高到低：microVM（独立内核，限额真实生效）→ bubblewrap / Seatbelt
    （共享内核的访问控制）→ 裸子进程。

    auto 模式挑第一个**已就绪**的，判断比 available() 更严：microVM 还要求
    OCI 镜像已经拉到本地（实测首次拉取约 55s）。所以镜像没缓存的机器会安静地
    用 Seatbelt，而不是让第一次跑代码的人干等一分钟；拉过一次之后 auto 就会
    自动升到 microVM，不用改配置。

    显式指定（配置写死 microvm，或节点选了 strict 档）不受这条限制——
    那是使用者自己要的硬隔离，该等就等。
    """

    def __init__(self) -> None:
        self._backend: Sandbox | None = None
        self._named: dict[str, Sandbox] = {}  # 按名字缓存，供节点级档位使用
        self._lock = asyncio.Lock()
        self._resolved_from: str = ""

    async def get(self, prefer: str | None = None) -> Sandbox:
        """取后端。prefer 指定档位时给那一档，拿不到就退回默认那台。

        节点级的"隔离档位"走这条路：strict 要硬件隔离、fast 要低延迟，
        而整机默认仍由 auto 决定。
        """
        if prefer:
            resolved = self._resolve_named(prefer)
            if resolved is not None:
                return resolved
        if self._backend is not None:
            return self._backend
        async with self._lock:
            if self._backend is not None:
                return self._backend
            self._backend = self._resolve()
            self._named[self._backend.name] = self._backend
            return self._backend

    def _resolve_named(self, prefer: str) -> Sandbox | None:
        """按档位名取一个**已就绪**的后端；不可用时返回 None 让调用方退回默认。"""
        # 关掉就是关掉。这条必须在最前面：否则配置里 off 只挡得住默认路径，
        # 图里任何一个 isolation=strict 的代码节点照样能跑代码，
        # DisabledSandbox 那句"沙箱已在设置中关闭"根本到不了。
        if (settings.sandbox_backend or "auto").lower() == "off":
            return None

        # 只认这两个档位。以前是 alias.get(prefer, prefer) 原样透传，而 isolation
        # 是图 JSON 里的自由字符串，于是图里写 isolation="local" 就能点名那个
        # 明说了"没有任何访问控制"的兜底后端——档位是用来提高隔离的，不该能降级。
        alias = {"strict": "microvm", "fast": "seatbelt" if sys.platform == "darwin" else "bubblewrap"}
        name = alias.get(prefer)
        if name is None:
            return None
        if name in self._named:
            return self._named[name]
        impl = dict(_BACKENDS).get(name)
        if impl is None or not getattr(impl, "available", lambda: True)():
            return None
        backend = impl()
        self._named[name] = backend
        return backend

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
            if not getattr(impl, "available", lambda: True)():
                continue
            # auto 模式额外要求"开箱即用"：microVM 镜像没拉过就先跳过
            warm = getattr(impl, "image_ready", None)
            if warm is not None and not warm():
                self._resolved_from = f"{name} 可用但镜像未缓存，auto 先跳过"
                continue
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
        backend: str | None = None,
    ) -> ExecResult:
        impl = await self.get(backend)
        return await impl.run(
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
            {
                "name": name,
                "available": getattr(impl, "available", lambda: True)(),
                # 可用但没预热的（microVM 镜像未拉取）要单独标出来：
                # 它不会被 auto 选中，但显式指定仍然能用
                **({} if getattr(impl, "image_ready", None) is None
                   else {"warm": impl.image_ready()}),
            }
            for name, impl in _BACKENDS
        ]
        return info

    async def cleanup(self, session_id: str) -> None:
        # 会话可能落在任意一档上（节点各自选了档位），全部清一遍
        for impl in {id(b): b for b in [*self._named.values(), self._backend] if b}.values():
            await impl.cleanup(session_id)

    async def close(self) -> None:
        for impl in {id(b): b for b in [*self._named.values(), self._backend] if b}.values():
            await impl.close()


def _why(name: str) -> str:
    return {
        "microvm": "microsandbox 运行时已就绪，独立内核",
        "seatbelt": "macOS 自带 sandbox-exec（microVM 未就绪）",
        "bubblewrap": "检测到 bwrap",
        "local": "无可用隔离原语",
    }.get(name, "")


sandbox_manager = SandboxManager()
