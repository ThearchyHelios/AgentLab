from __future__ import annotations

import asyncio
import os
import resource
import shutil
import sys
import time
from pathlib import Path

from app.core.config import settings, sanitize_session
from app.sandbox import interpreter
from app.sandbox.base import ENTRY_PREFIX, ExecResult, Sandbox, SandboxLimits, entry_name, truncate

# 入口脚本后缀 + 执行命令。文件名运行时生成（entry_name），不能写死：
# 同会话并发执行都写 main.py 会互相覆盖。
_LANG = {
    "python": (".py", None),  # 运行时用 interpreter.python_argv()
    "bash": (".sh", ["bash"]),
    "sh": (".sh", ["sh"]),
    "node": (".js", ["node"]),
    "javascript": (".js", ["node"]),
}


class LocalSandbox(Sandbox):
    """裸子进程执行，所有隔离后端都不可用时的最后兜底。

    只有 rlimit 和一个独立工作目录，**没有任何访问控制**：代码能读你的整个
    文件系统、能联网。正常情况下不会走到这里 —— macOS 有 Seatbelt、
    Linux 装个 bubblewrap 即可。真要用它跑不信任的代码，别。
    """

    name = "local"

    def __init__(self) -> None:
        self._root = settings.workspace_dir / "local"
        self._root.mkdir(parents=True, exist_ok=True)

    async def health(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "available": True,
            "isolated": False,
            "warning": "裸子进程执行，无访问控制，不要跑不信任的代码",
            "enforced": {"timeout": True, "cpu": True,
                         "memory": not sys.platform == "darwin", "network": False, "fsize": True},
            "root": str(self._root),
            "interpreter": interpreter.describe(),
        }

    def _session_dir(self, session_id: str) -> Path:
        # 净化规则统一在 config.sanitize_session 里，四个后端和文件工具共用一份：
        # 这段逻辑以前各抄各的，文件工具那份漏抄了，就成了越界读取的口子。
        path = self._root / sanitize_session(session_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _preexec(limits: SandboxLimits):  # pragma: no cover - 只在子进程里跑
        def _apply() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.timeout, limits.timeout + 2))
            # 注意 macOS 不强制执行 RLIMIT_AS，这条在 Darwin 上是无效的
            nbytes = limits.memory_mb * 1024 * 1024
            for res_name in ("RLIMIT_AS", "RLIMIT_DATA"):
                res_id = getattr(resource, res_name, None)
                if res_id is not None:
                    try:
                        resource.setrlimit(res_id, (nbytes, nbytes))
                    except (ValueError, OSError):
                        pass
            resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # 不设 RLIMIT_NPROC：macOS 上它按 uid 全局计数，设小了会让沙箱里
            # 任何 fork 都失败（bash 直接起不来），而不是只限制沙箱自己。
            os.setsid()  # 独立进程组，超时时能整组 kill

        return _apply

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

        ext, cmd = _LANG[language]
        # python 的命令前缀运行时才定：要挑一个看不见后端依赖的解释器
        cmd = cmd if cmd is not None else interpreter.python_argv()
        entry = entry_name(ext)
        argv = [*cmd, entry]
        if shutil.which(argv[0]) is None and argv[0] != sys.executable:
            return ExecResult(
                ok=False, backend=self.name, error=f"宿主机上找不到 {argv[0]}"
            )

        workdir = self._session_dir(session_id or f"tmp-{int(time.time()*1000)}")
        (workdir / entry).write_text(code)
        for rel, content in (files or {}).items():
            target = (workdir / rel).resolve()
            if not str(target).startswith(str(workdir.resolve())):
                return ExecResult(ok=False, backend=self.name, error=f"非法文件路径：{rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        # 只透传最小环境变量，避免把宿主机的 API key 泄漏给被执行的代码
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workdir),
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            **(env or {}),
        }

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._preexec(limits),
            )
        except Exception as e:  # noqa: BLE001
            return ExecResult(ok=False, backend=self.name, error=f"{type(e).__name__}: {e}")

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=limits.timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.wait()
            return ExecResult(
                ok=False,
                backend=self.name,
                timed_out=True,
                exit_code=124,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=f"执行超过 {limits.timeout}s 被终止",
            )

        stdout, t1 = truncate(out.decode(errors="replace"), limits.max_output)
        stderr, t2 = truncate(err.decode(errors="replace"), limits.max_output)
        written = [
            p.name
            for p in workdir.iterdir()
            if p.is_file() and not p.name.startswith(ENTRY_PREFIX)
        ][:50]
        return ExecResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            truncated=t1 or t2,
            backend=self.name,
            duration_ms=int((time.perf_counter() - started) * 1000),
            files_written=written,
        )

    async def cleanup(self, session_id: str) -> None:
        path = self._session_dir(session_id)
        await asyncio.to_thread(shutil.rmtree, path, True)
