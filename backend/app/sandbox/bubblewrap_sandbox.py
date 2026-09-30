from __future__ import annotations

import asyncio
import logging
import os
import resource
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from app.core.config import settings, sanitize_session
from app.core.errors import describe_exception, raw
from app.sandbox import interpreter
from app.sandbox.base import ENTRY_PREFIX, ExecResult, Sandbox, SandboxLimits, entry_name, truncate

logger = logging.getLogger(__name__)

# 入口脚本后缀 + 执行命令前缀（沙箱内路径在运行时拼）。文件名不能写死：
# 同会话并发执行都写 main.py 会互相覆盖。
_LANG = {
    "python": (".py", None),  # 运行时用 interpreter.python_argv()
    "bash": (".sh", ["/bin/bash"]),
    "sh": (".sh", ["/bin/sh"]),
    "node": (".js", ["node"]),
    "javascript": (".js", ["node"]),
}

# 只读挂进沙箱的系统目录，存在才挂
_RO_BINDS = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/alternatives", "/etc/ssl"]


def _on_linux() -> bool:
    return sys.platform.startswith("linux")


@lru_cache(maxsize=4)
def _probe(bwrap: str) -> str:
    """真起一次 bwrap，返回失败原因；空串表示能用。

    装了 bwrap 不等于建得了隔离环境：Docker 默认的 seccomp 配置不许非特权进程建
    user namespace，Ubuntu 24.04 的 AppArmor 默认也限制它。以前 available() 只看
    二进制在不在，于是容器里 auto 选中 bubblewrap，每次执行都报 "No permissions to
    create a new namespace"——而本地子进程明明能用。

    探测带上执行时同样要用的命名空间、/proc 和 /dev，任何一样建不起来都算不可用。
    结果缓存：内核和容器权限在进程生命周期里不会变。
    """
    argv = [
        bwrap, "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
        "--", shutil.which("true") or "/bin/true",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return raw(e)
    if proc.returncode == 0:
        return ""
    lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else f"退出码 {proc.returncode}"


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
        if not _on_linux():
            return False
        bwrap = shutil.which("bwrap")
        return bwrap is not None and not _probe(bwrap)

    @staticmethod
    def probe_failure() -> str:
        """装了 bwrap 却建不起隔离环境时的原因；没装或者能用都返回空串。"""
        bwrap = shutil.which("bwrap") if _on_linux() else None
        return _probe(bwrap) if bwrap else ""

    @classmethod
    def unavailable_reason(cls) -> str:
        failure = cls.probe_failure()
        if failure:
            return f"已安装 bwrap，但无法创建隔离环境（容器默认权限或系统策略不允许）：{failure}"
        return "未找到 bwrap，可执行 apt install bubblewrap 安装"

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
            "interpreter": interpreter.describe(),
            **({} if ok else {"error": self.unavailable_reason()}),
        }

    def _session_dir(self, session_id: str) -> Path:
        # 净化规则统一在 config.sanitize_session 里，四个后端和文件工具共用一份：
        # 这段逻辑以前各抄各的，文件工具那份漏抄了，就成了越界读取的口子。
        path = self._root / sanitize_session(session_id)
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
            return ExecResult(ok=False, backend=self.name, error=self.unavailable_reason())

        ext, cmd_prefix = _LANG[language]
        # python 的命令前缀运行时才定：要挑一个看不见后端依赖的解释器
        cmd_prefix = cmd_prefix if cmd_prefix is not None else interpreter.python_argv()
        entry = entry_name(ext)
        cmd = [*cmd_prefix, f"/work/{entry}"]
        workdir = self._session_dir(session_id or f"tmp-{int(time.time() * 1000)}")
        (workdir / entry).write_text(code)
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
                error=f"执行超过 {limits.timeout} 秒，已终止",
            )
        except Exception as e:  # noqa: BLE001
            # 起进程和收输出在同一个 try 里。首行只说原因，类名和 errno 这些留给日志
            logger.warning("%s 沙箱进程出错：%s", self.name, raw(e))
            return ExecResult(ok=False, backend=self.name, error=f"沙箱进程出错：{describe_exception(e)}")

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
            files_written=[p.name for p in workdir.iterdir() if p.is_file() and not p.name.startswith(ENTRY_PREFIX)][:50],
        )

    async def cleanup(self, session_id: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._session_dir(session_id), True)
