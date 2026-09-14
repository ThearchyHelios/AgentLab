from __future__ import annotations

import asyncio
import os
import resource
import shutil
import sys
import time
from pathlib import Path

from app.core.config import sanitize_session, settings
from app.sandbox import interpreter
from app.sandbox.base import ENTRY_PREFIX, ExecResult, Sandbox, SandboxLimits, entry_name, truncate

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

def _real(path: str | Path) -> str:
    """Seatbelt 按真实路径匹配，符号链接必须先解开。

    macOS 上 /tmp 其实是 /private/tmp，不解析的话策略会整条失效。
    """
    return str(Path(path).resolve())


def sandbox_python() -> str:
    """沙箱里用的 Python 解释器路径。挑选逻辑见 app.sandbox.interpreter。"""
    return interpreter.resolve()[0]


# 入口脚本后缀 + 执行命令前缀（解释器路径要延迟求值，所以是 lambda）。
# 文件名运行时生成：同会话并发执行都写 main.py 会互相覆盖。
_LANG = {
    "python": (".py", interpreter.python_argv),
    "bash": (".sh", lambda: ["/bin/bash"]),
    "sh": (".sh", lambda: ["/bin/sh"]),
    "node": (".js", lambda: [_real(shutil.which("node") or "node")]),
    "javascript": (".js", lambda: [_real(shutil.which("node") or "node")]),
}


def _interpreter_roots(language: str) -> list[str]:
    """解释器自身需要能读到的目录。

    只放行解释器的安装根（只读），不放行 sys.path —— 后者会把后端的
    site-packages 一起开出去。uv 把 Python 装在 ~/.local/share/uv/python/ 下，
    正好落在要禁读的家目录里，所以这条放行是必需的。
    """
    roots: set[str] = {
        "/usr", "/System", "/Library", "/bin", "/sbin", "/opt",
        "/private/var/select", "/private/var/db",
    }
    if language == "python":
        # 放行**实际选中**的那个解释器的安装根，而不是 sys.base_prefix ——
        # conda 环境下 base_prefix 就是项目环境自己，挑出来的往往是别的解释器
        exe = Path(interpreter.resolve()[0])
        roots.add(_real(exe.parent.parent))
    elif language in ("node", "javascript"):
        node = shutil.which("node")
        if node:
            roots.add(str(Path(_real(node)).parent.parent))
    return sorted(roots)


def build_policy(workdir: Path, *, language: str, network: bool) -> str:
    """生成 Seatbelt 策略（SBPL）。

    用「默认允许 + 精确拒绝」而不是 `(deny default)`：后者要把解释器启动所需的
    每一次 syscall 和路径都枚举出来，稍有遗漏进程就直接 SIGABRT，实测 Python
    根本起不来。这里换个思路 —— 默认放行，然后把真正要守的三件事收紧：
    写盘只限工作区、家目录不可读、默认断网。

    规则从上往下匹配，后面的覆盖前面的，所以 allow 必须写在对应 deny 之后。
    """
    work = _real(workdir)
    home = _real(Path.home())
    lines = [
        "(version 1)",
        "(allow default)",
        "",
        ";; ---- 网络 ----",
    ]
    if network:
        lines.append("(allow network*)")
    else:
        lines.append("(deny network*)")

    lines += [
        "",
        ";; ---- 写：只有工作区 ----",
        "(deny file-write*)",
        f'(allow file-write* (subpath "{work}"))',
        '(allow file-write-data (literal "/dev/null") (literal "/dev/stdout")'
        ' (literal "/dev/stderr") (literal "/dev/dtracehelper"))',
        '(allow file-write* (subpath "/private/var/folders"))',  # 临时目录，Python 要用
        "",
        ";; ---- 读：家目录挡住，放行工作区和解释器 ----",
        f'(deny file-read* (subpath "{home}"))',
        f'(allow file-read* (subpath "{work}"))',
    ]
    for root in _interpreter_roots(language):
        lines.append(f'(allow file-read* (subpath "{root}"))')

    lines += [
        "",
        ";; ---- 其他 ----",
        "(allow process-exec process-fork)",  # 子进程同样受这份策略约束
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(deny file-read* (subpath \"/Users/Shared\"))",
    ]
    return "\n".join(lines) + "\n"


class SeatbeltSandbox(Sandbox):
    """基于 macOS 原生 Seatbelt（sandbox-exec）的沙箱。

    内核级的强制访问控制，不需要虚拟机，启动开销约等于起一个进程。
    策略对子进程是继承的，所以沙箱里再 exec 出来的东西一样跑不掉。

    和容器方案的差别要说清楚：这里做的是**访问控制**而不是虚拟化 ——
    文件系统和网络守得住，但进程表是共享的（能看到宿主机进程列表），
    而且 macOS 不认 RLIMIT_AS，**内存用量限不住**。CPU 时间、文件大小、
    墙钟超时这几项由 rlimit 和超时杀进程组来兜。
    """

    name = "seatbelt"

    def __init__(self) -> None:
        self._root = settings.workspace_dir / "seatbelt"
        self._policies = settings.data_dir / "sandbox-policies"

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and Path(SANDBOX_EXEC).exists()

    async def health(self) -> dict[str, object]:
        ok = self.available()
        return {
            "backend": self.name,
            "available": ok,
            "isolated": ok,
            "isolation": "macOS Seatbelt：文件系统 + 网络（内核强制，子进程继承）",
            "limits_note": "macOS 不支持 RLIMIT_AS，内存用量无法限制；CPU 时间与墙钟超时有效",
            # 如实申报：界面按这个把限不住的项划掉，避免给人虚假的安全感
            "enforced": {"timeout": True, "cpu": True, "memory": False, "network": True, "fsize": True},
            "root": str(self._root),
            "interpreter": interpreter.describe(),
            **({} if ok else {"error": "当前系统不是 macOS 或缺少 /usr/bin/sandbox-exec"}),
        }

    def _session_dir(self, session_id: str) -> Path:
        # 净化规则统一在 config.sanitize_session 里，四个后端和文件工具共用一份：
        # 这段逻辑以前各抄各的，文件工具那份漏抄了，就成了越界读取的口子。
        path = self._root / sanitize_session(session_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _preexec(limits: SandboxLimits):  # pragma: no cover - 只在子进程里执行
        def _apply() -> None:
            # CPU 时间：能挡住纯计算的死循环（sleep 不计入，墙钟超时另外兜）
            resource.setrlimit(resource.RLIMIT_CPU, (limits.timeout, limits.timeout + 2))
            # 单文件大小，防止把磁盘写满
            resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # 注意：不设 RLIMIT_NPROC —— macOS 上它按 uid 全局计数，
            # 设成小值会让沙箱里任何 fork 都失败（连 bash 都起不来）。
            # 也不设 RLIMIT_AS —— Darwin 不强制执行，设了是自欺欺人。
            os.setsid()  # 独立进程组，超时能整组干掉

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
        if not self.available():
            return ExecResult(ok=False, backend=self.name, error="sandbox-exec 不可用")

        ext, cmd_of = _LANG[language]
        entry = entry_name(ext)
        workdir = self._session_dir(session_id or f"tmp-{int(time.time() * 1000)}")
        (workdir / entry).write_text(code)

        for rel, content in (files or {}).items():
            target = (workdir / rel).resolve()
            if not str(target).startswith(str(workdir.resolve())):
                return ExecResult(ok=False, backend=self.name, error=f"非法文件路径：{rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        self._policies.mkdir(parents=True, exist_ok=True)
        policy_file = self._policies / f"{workdir.name}-{language}.sb"
        policy_file.write_text(build_policy(workdir, language=language, network=limits.network))

        argv = [SANDBOX_EXEC, "-f", str(policy_file), *cmd_of(), entry]

        # HOME 指向工作区：既挡住家目录，又让解释器的缓存有地方落
        child_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
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
            self._kill(proc)
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
        code_ = proc.returncode or 0
        # CPU 时间用尽是 SIGXCPU(24)，归到超时里更好理解
        timed_out = code_ in (-24, 152)
        written = [
            p.name for p in workdir.iterdir()
            if p.is_file() and not p.name.startswith(ENTRY_PREFIX)
        ][:50]

        return ExecResult(
            ok=code_ == 0,
            exit_code=code_,
            stdout=stdout,
            stderr=stderr if not timed_out else (stderr or "CPU 时间超限，已终止"),
            truncated=t1 or t2,
            timed_out=timed_out,
            backend=self.name,
            duration_ms=int((time.perf_counter() - started) * 1000),
            files_written=written,
        )

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    async def cleanup(self, session_id: str) -> None:
        path = self._session_dir(session_id)
        await asyncio.to_thread(shutil.rmtree, path, True)
        for stale in self._policies.glob(f"{path.name}-*.sb"):
            stale.unlink(missing_ok=True)
