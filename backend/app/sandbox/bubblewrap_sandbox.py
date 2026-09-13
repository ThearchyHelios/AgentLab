from __future__ import annotations

import asyncio
import os
import resource
import shutil
import sys
import time
from pathlib import Path

from app.core.config import settings
from app.sandbox.base import ExecResult, Sandbox, SandboxLimits, truncate

_LANG = {
    "python": ("main.py", ["python3", "-I", "-u", "/work/main.py"]),
    "bash": ("main.sh", ["/bin/bash", "/work/main.sh"]),
    "sh": ("main.sh", ["/bin/sh", "/work/main.sh"]),
    "node": ("main.js", ["node", "/work/main.js"]),
    "javascript": ("main.js", ["node", "/work/main.js"]),
}

# 只读挂进沙箱的系统目录，存在才挂
_RO_BINDS = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/alternatives", "/etc/ssl"]


class BubblewrapSandbox(Sandbox):
    """Linux 上的对等方案：bubblewrap（Flatpak 的沙箱内核）。

    一个几百 KB 的二进制，直接用 namespace + seccomp 建隔离环境，
    不需要 daemon、不需要 root、不需要镜像。相比 Seatbelt 它还多隔离了
    PID、IPC 和挂载视图，隔离强度更接近容器。
    """

    name = "bubblewrap"

    def __init__(self) -> None:
        self._root = settings.workspace_dir / "bwrap"
        self._bwrap = shutil.which("bwrap")

    @staticmethod
    def available() -> bool:
        return sys.platform.startswith("linux") and shutil.which("bwrap") is not None

    async def health(self) -> dict[str, object]:
        ok = self.available()
        return {
            "backend": self.name,
            "available": ok,
            "isolated": ok,
            "isolation": "Linux namespace（mount/pid/ipc/uts/net）+ 只读根",
            "limits_note": "内存与 CPU 由 rlimit 限制；如需 cgroups 级配额请配合 systemd-run",
            "enforced": {"timeout": True, "cpu": True, "memory": True, "network": True, "fsize": True},
            "root": str(self._root),
            **({} if ok else {"error": "未找到 bwrap，可执行 apt install bubblewrap 安装"}),
        }

    def _session_dir(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "default"
        path = self._root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _preexec(limits: SandboxLimits):  # pragma: no cover - 子进程里执行
        def _apply() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.timeout, limits.timeout + 2))
            # Linux 上 RLIMIT_AS 是真正生效的（macOS 不是）
            nbytes = limits.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (nbytes, nbytes))
            resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            os.setsid()

        return _apply

    def _argv(self, workdir: Path, limits: SandboxLimits, cmd: list[str]) -> list[str]:
        argv = [
            self._bwrap or "bwrap",
            "--unshare-all",              # mount/pid/ipc/uts/cgroup/net 全部隔离
            *(["--share-net"] if limits.network else []),
            "--die-with-parent",          # 父进程没了，沙箱跟着走
            "--new-session",              # 摘掉控制终端，防 TIOCSTI 注入
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", str(workdir), "/work",
            "--chdir", "/work",
            "--setenv", "HOME", "/work",
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        ]
        for path in _RO_BINDS:
            if Path(path).exists():
                argv += ["--ro-bind", path, path]
        return argv + ["--", *cmd]

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
        limits = limits or SandboxLimits()
        language = (language or "python").lower()
        if language not in _LANG:
            return ExecResult(ok=False, backend=self.name, error=f"不支持的语言：{language}")
        if not self.available():
            return ExecResult(ok=False, backend=self.name, error="bwrap 不可用")

        filename, cmd = _LANG[language]
        workdir = self._session_dir(session_id or f"tmp-{int(time.time() * 1000)}")
        (workdir / filename).write_text(code)
        for rel, content in (files or {}).items():
            target = (workdir / rel).resolve()
            if not str(target).startswith(str(workdir.resolve())):
                return ExecResult(ok=False, backend=self.name, error=f"非法文件路径：{rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        argv = self._argv(workdir, limits, cmd)
        for key, value in (env or {}).items():
            argv[1:1] = ["--setenv", key, value]

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._preexec(limits),
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=limits.timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.wait()
            return ExecResult(
                ok=False, backend=self.name, timed_out=True, exit_code=124,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"执行超过 {limits.timeout}s 被终止",
            )
        except Exception as e:  # noqa: BLE001
            return ExecResult(ok=False, backend=self.name, error=f"{type(e).__name__}: {e}")

        stdout, t1 = truncate(out.decode(errors="replace"), limits.max_output)
        stderr, t2 = truncate(err.decode(errors="replace"), limits.max_output)
        code_ = proc.returncode or 0
        return ExecResult(
            ok=code_ == 0,
            exit_code=code_,
            stdout=stdout,
            stderr=stderr,
            truncated=t1 or t2,
            timed_out=code_ in (-24, 152),
            backend=self.name,
            duration_ms=int((time.perf_counter() - started) * 1000),
            files_written=[p.name for p in workdir.iterdir() if p.is_file() and p.name != filename][:50],
        )

    async def cleanup(self, session_id: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._session_dir(session_id), True)
