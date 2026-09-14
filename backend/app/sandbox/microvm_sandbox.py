from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import sanitize_session, settings
from app.sandbox.base import (
    ENTRY_PREFIX,
    ExecResult,
    Sandbox,
    SandboxLimits,
    entry_name,
    truncate,
)

_WORKDIR = "/workspace"

# 各语言的入口脚本后缀与执行命令。文件名在运行时生成（见 entry_name），
# 不能写死 —— 同会话并发执行会互相覆盖。
_LANG = {
    "python": (".py", ["python", "-u"]),
    "bash": (".sh", ["/bin/bash"]),
    "sh": (".sh", ["/bin/sh"]),
    "node": (".js", ["node"]),
    "javascript": (".js", ["node"]),
}

_MAX_FILE_BYTES = 8 * 1024 * 1024
_MANIFEST_CACHE = Path.home() / ".microsandbox" / "cache" / "manifests"


@dataclass
class _Lease:
    """一台长驻的 microVM。

    SDK 的 create() 直接返回已启动的 Sandbox，没有 handle/connect 两层——
    这点是实测确认的，早先按别的 SDK 惯例推断的写法在这里是错的。
    """

    vm: Any
    network: bool
    memory_mb: int
    cpus: int
    last_used: float = field(default_factory=time.monotonic)
    # 这台是因为限额变更重建出来的，也就是说上一次的会话文件已经没了
    was_reset: bool = False


class MicroVMSandbox(Sandbox):
    """硬件级隔离：每个会话一台独立内核的 microVM（libkrun + Hypervisor.framework）。

    和 Seatbelt 的差别不是"更严格的访问控制"，而是换了一类边界。本机实测：
    宿主是 Darwin 27.0.0，VM 里是 Linux 6.12.99（libkrunfw 编译），PID 1 是
    init.krun，/Users 在 VM 里根本不存在，`free` 只看得见 512MB。

    因此 Seatbelt 上那个最难受的缺口在这里是真的没了：申请 900MB 会被内核
    OOM killer 杀掉（进程退出码为负），而 VM 本身照常存活——macOS 上
    RLIMIT_AS 形同虚设的问题不复存在。

    但有一处**必须说清楚的残留缺口**：网络关不干净。limits.network=False 时
    HTTP/HTTPS、域名解析都会断，可 UDP/53 拦不住——实测手写 DNS 包仍能拿到
    真实响应。试过 default_egress=DENY、Rule.deny_dns()、显式 deny UDP、
    max_connections=0，UDP 那条都堵不上（deny_dns 生成的规则只针对宿主，
    而 DNS 出口在 microsandbox 的网络栈里是硬编码放行的）。也就是说
    **DNS 隧道外泄通道始终是开的**。防"代码意外联网装包"够用，防"恶意代码
    偷数据"不够——health() 里如实标成部分生效，不写 True。

    成本也照实说：运行时约 50MB，OCI 镜像首次拉取本机实测 54.5s；之后热启动
    0.19s、VM 内执行 7~30ms。所以贵的是第一次，不是每一次。
    """

    name = "microvm"

    def __init__(self) -> None:
        self._leases: dict[str, _Lease] = {}
        self._lock = asyncio.Lock()
        self._image = settings.microvm_image

    # ---------------- 可用性 ----------------

    @staticmethod
    def available() -> bool:
        """SDK 装了、运行时也装好了才算可用。"""
        try:
            import microsandbox as ms
        except ImportError:
            return False
        try:
            return bool(ms.is_installed())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def image_ready() -> bool:
        """镜像是否已经拉到本地。

        auto 模式拿这个做判断：镜像没缓存时第一次跑代码要等 50 秒以上，
        与其让人卡在那儿，不如安静地先用 Seatbelt——显式选 strict 的人
        另说，那是他自己要的硬隔离，等就等。
        """
        try:
            return _MANIFEST_CACHE.is_dir() and any(_MANIFEST_CACHE.iterdir())
        except OSError:
            return False

    async def health(self) -> dict[str, Any]:
        try:
            import microsandbox as ms
        except ImportError:
            return {
                "backend": self.name,
                "available": False,
                "isolated": True,
                "error": "未安装 microsandbox（pip install 'agentlab-backend[microvm]'）",
            }
        installed = False
        try:
            installed = bool(ms.is_installed())
        except Exception:  # noqa: BLE001
            pass
        cached = self.image_ready()
        return {
            "backend": self.name,
            "available": installed,
            "isolated": True,
            "isolation": "microVM：独立内核 + 独立内存 + 独立进程表（libkrun / Hypervisor.framework）",
            # 只有 network 不是真的——别的都由虚拟化边界强制执行
            "enforced": {
                "timeout": True,
                "cpu": True,
                "memory": True,
                "fsize": True,
                "pid": True,
                "network": "partial",
            },
            "limits_note": (
                "内存/CPU/进程表由内核边界强制执行（超限进程被 OOM killer 杀掉，VM 存活）。"
                "网络只能算部分生效：HTTP/HTTPS 与域名解析可断，但 UDP/53 拦不住，"
                "DNS 隧道外泄通道仍然开着。"
            ),
            "image": self._image,
            "image_cached": cached,
            "live_vms": len(self._leases),
            **({} if installed else {"error": "运行时未就绪（await microsandbox.install()，约 50MB）"}),
            **({} if cached else {"first_run_cost": "镜像未缓存，首次启动需拉取（本机实测约 55s）"}),
        }

    # ---------------- 会话（长驻 VM）----------------

    def _vm_name(self, session_id: str) -> str:
        # SDK 要求首字符是字母数字，其余只能是字母数字/点/横线/下划线。
        # 这里直接用共用的净化规则（比 SDK 的要求更严，不放行点号），
        # 让 VM 名和别处的会话目录名保持一致。
        return f"agentlab-{sanitize_session(session_id)}"

    async def _lease(self, session_id: str, limits: SandboxLimits) -> _Lease:
        """拿到该会话的 VM。限额变了就重建，否则复用（热复用约 7ms）。"""
        import microsandbox as ms

        cpus = max(1, int(limits.cpus))
        async with self._lock:
            reset = False
            lease = self._leases.get(session_id)
            if lease is not None:
                if (lease.network == limits.network
                        and lease.memory_mb == limits.memory_mb
                        and lease.cpus == cpus):
                    lease.last_used = time.monotonic()
                    return lease
                # 限额变了：VM 的内存/CPU 是启动时定死的，只能销毁重建。
                # 但重建等于把会话工作区清空——同一个 run 里两个代码节点各填了
                # 不同的 memory_mb 就会触发，而调用方对此毫无察觉。标记出来。
                await self._destroy(session_id)
                reset = True

            # network 必须是 Network 实例（传 bool 会被 SDK 拒掉）
            network = ms.Network.allow_all() if limits.network else ms.Network.none()
            vm = await asyncio.wait_for(
                ms.Sandbox.create(
                    self._vm_name(session_id),
                    image=ms.Image.oci(self._image),
                    cpus=cpus,
                    memory=limits.memory_mb,
                    network=network,
                ),
                timeout=settings.microvm_boot_timeout,
            )
            try:
                await vm.fs.mkdir(_WORKDIR)
            except Exception:  # noqa: BLE001 - 已存在就算了
                pass

            lease = _Lease(vm=vm, network=limits.network,
                           memory_mb=limits.memory_mb, cpus=cpus)
            lease.was_reset = reset
            self._leases[session_id] = lease
            return lease

    async def _destroy(self, session_id: str) -> None:
        lease = self._leases.pop(session_id, None)
        if lease is None:
            return
        try:
            await lease.vm.destroy(force=True)
        except Exception:  # noqa: BLE001 - 已经没了就算
            pass

    async def _kill_leftovers(self, session_id: str, entry: str) -> bool:
        """超时后在 VM 内把跑飞的进程杀掉，保住会话工作区。

        返回 True 表示 VM 还能用；False 表示没收拾干净，调用方该销毁重建。
        """
        lease = self._leases.get(session_id)
        if lease is None:
            return False
        try:
            # 只杀这次的入口脚本引出来的进程——入口名带唯一后缀，
            # 不会误伤同会话里别的并发执行
            await asyncio.wait_for(
                lease.vm.shell(f"pkill -9 -f {entry} || true", timeout=10),
                timeout=15,
            )
            # 确认 VM 还答应
            out = await asyncio.wait_for(
                lease.vm.exec("sh", ["-c", "echo alive"], timeout=10), timeout=15
            )
            return "alive" in str(getattr(out, "stdout_text", "") or "")
        except Exception:  # noqa: BLE001 - 收拾不动就让调用方销毁
            return False

    async def _reap_idle(self) -> None:
        """回收闲置 VM：它们各自占着几百 MB 内存，不能一直挂着。"""
        cutoff = time.monotonic() - settings.microvm_idle_seconds
        stale = [sid for sid, lease in self._leases.items() if lease.last_used < cutoff]
        for sid in stale:
            await self._destroy(sid)

    # ---------------- 执行 ----------------

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
        import microsandbox as ms

        limits = limits or SandboxLimits()
        language = (language or "python").lower()
        if language not in _LANG:
            return ExecResult(ok=False, backend=self.name, error=f"不支持的语言：{language}")
        if not self.available():
            return ExecResult(
                ok=False, backend=self.name,
                error="microVM 运行时未就绪：pip install 'agentlab-backend[microvm]' 后执行一次 install",
            )

        ext, cmd = _LANG[language]
        entry = entry_name(ext)
        sid = session_id or f"tmp{int(time.time() * 1000)}"
        ephemeral = session_id is None
        started = time.perf_counter()

        try:
            await self._reap_idle()
            lease = await self._lease(sid, limits)
        except asyncio.TimeoutError:
            return ExecResult(
                ok=False, backend=self.name, timed_out=True,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"microVM 启动超过 {settings.microvm_boot_timeout}s"
                      + ("" if self.image_ready() else "（镜像未缓存，首次需拉取约 55s）"),
            )
        except Exception as e:  # noqa: BLE001
            return ExecResult(
                ok=False, backend=self.name,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"microVM 启动失败：{type(e).__name__}: {e}",
            )

        try:
            payload = dict(files or {})
            payload[entry] = code
            for rel, content in payload.items():
                rel = rel.lstrip("/")
                if ".." in rel.split("/"):
                    return ExecResult(ok=False, backend=self.name, error=f"非法文件路径：{rel}")
                data = content.encode()
                if len(data) > _MAX_FILE_BYTES:
                    return ExecResult(ok=False, backend=self.name, error=f"{rel} 超过 8MB 上限")
                target = f"{_WORKDIR}/{rel}"
                if "/" in rel:
                    try:
                        await lease.vm.fs.mkdir(target.rsplit("/", 1)[0])
                    except Exception:  # noqa: BLE001
                        pass
                await lease.vm.fs.write(target, data)

            # cwd / env / timeout 都是 exec 的原生参数，不用再拿 shell 拼
            out = await lease.vm.exec(
                cmd[0], [*cmd[1:], entry],
                cwd=_WORKDIR,
                env=({k: str(v) for k, v in env.items()} if env else None),
                timeout=limits.timeout,
            )
        except ms.ExecTimeoutError:
            # 超时的 VM 里多半还有进程在烧 CPU，得收拾掉。但**先别销毁整台 VM**：
            # 会话契约说同 session 共享文件系统（上一步写的文件下一步要读回来），
            # 销毁 VM 等于把工作区连同用户数据一起清空，而另外三个后端超时只
            # killpg、工作目录照留。先试着在 VM 里把残留进程杀干净，
            # 实在不行才销毁——并且如实告诉调用方会话被重置了。
            killed = await self._kill_leftovers(sid, entry)
            if not killed:
                await self._destroy(sid)
            return ExecResult(
                ok=False, backend=self.name, timed_out=True, exit_code=124,
                duration_ms=int((time.perf_counter() - started) * 1000),
                session_reset=not killed,
                error=f"执行超过 {limits.timeout}s 被终止"
                      + ("" if killed else "；VM 已重置，该会话之前写的文件已丢失"),
            )
        except Exception as e:  # noqa: BLE001
            return ExecResult(
                ok=False, backend=self.name,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            if ephemeral:
                await self._destroy(sid)
            elif sid in self._leases:
                # 入口脚本是这次执行的私有临时文件，跑完就收掉，
                # 免得会话工作区被历年的 __entry_xxx 堆满
                try:
                    await lease.vm.fs.remove(f"{_WORKDIR}/{entry}")
                except Exception:  # noqa: BLE001 - 删不掉不影响执行结果
                    pass

        stdout, t1 = truncate(str(getattr(out, "stdout_text", "") or ""), limits.max_output)
        stderr, t2 = truncate(str(getattr(out, "stderr_text", "") or ""), limits.max_output)
        exit_code = int(getattr(out, "exit_code", 0) or 0)

        # 退出码为负 = 进程被信号杀掉（SDK 用负值表示，具体是哪个信号它没给，
        # 所以这里不编）。VM 里最常见的原因就是撞了内存上限，而且这种情况
        # stdout/stderr 通常都是空的——不解释一句，用户只会看到"失败了"。
        error = None
        if exit_code < 0 and not stderr.strip():
            error = (f"进程被内核终止（退出码 {exit_code}），"
                     f"最常见的原因是超过了 {limits.memory_mb}MB 内存上限")

        written: list[str] = []
        if not ephemeral:
            try:
                entries = await lease.vm.fs.list(_WORKDIR)
                # FsEntry 给的是 path（全路径），不是 name —— 取尾段当文件名。
                # 早先按 name 取，结果 files_written 里塞的是对象 repr。
                names = [str(getattr(e, "path", "") or "").rsplit("/", 1)[-1] for e in entries]
                written = [n for n in names if n and not n.startswith(ENTRY_PREFIX)][:50]
            except Exception:  # noqa: BLE001 - 列目录失败不影响执行结果
                written = []

        return ExecResult(
            ok=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=t1 or t2,
            backend=self.name,
            duration_ms=int((time.perf_counter() - started) * 1000),
            files_written=written,
            session_reset=lease.was_reset,
            error=error,
        )

    async def cleanup(self, session_id: str) -> None:
        await self._destroy(session_id)

    async def close(self) -> None:
        for sid in list(self._leases):
            await self._destroy(sid)
