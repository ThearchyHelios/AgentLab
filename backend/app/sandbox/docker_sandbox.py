from __future__ import annotations

import asyncio
import base64
import shlex
import time
import uuid
from typing import Any

from app.core.config import settings
from app.sandbox.base import ExecResult, Sandbox, SandboxLimits, truncate

_WORKDIR = "/workspace"

_LANG_CMD = {
    "python": ["python", "-u", "/workspace/.main.py"],
    "bash": ["bash", "/workspace/.main.sh"],
    "sh": ["sh", "/workspace/.main.sh"],
    "node": ["node", "/workspace/.main.js"],
    "javascript": ["node", "/workspace/.main.js"],
}
_LANG_FILE = {
    "python": ".main.py",
    "bash": ".main.sh",
    "sh": ".main.sh",
    "node": ".main.js",
    "javascript": ".main.js",
}


# 单条 shell 命令里塞的 base64 分块大小。留足余量避开 ARG_MAX。
_CHUNK = 48 * 1024
_MAX_FILE_BYTES = 4 * 1024 * 1024


def _write_commands(files: dict[str, str]) -> list[list[str]]:
    """生成往容器里写文件的 shell 命令。

    这里不用 docker 的 put_archive：那个 API 会被 `read_only` 的容器直接拒掉
    （即便目标是可写的 tmpfs 挂载点）。改成 exec + base64 管道写入，
    就能同时保住只读根文件系统和往 /workspace 落文件这两件事。
    """
    commands: list[list[str]] = []
    for path, content in files.items():
        rel = path.lstrip("/")
        if ".." in rel.split("/"):
            raise ValueError(f"非法文件路径：{path}")
        full = f"{_WORKDIR}/{rel}"
        data = content.encode()
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError(f"{path} 超过 {_MAX_FILE_BYTES // 1024 // 1024}MB 上限")
        blob = base64.b64encode(data).decode()
        quoted = shlex.quote(full)
        commands.append(["sh", "-c", f"mkdir -p $(dirname {quoted})"])
        if not blob:
            commands.append(["sh", "-c", f": > {quoted}"])
            continue
        for i in range(0, len(blob), _CHUNK):
            chunk = blob[i : i + _CHUNK]
            redirect = ">" if i == 0 else ">>"
            commands.append(["sh", "-c", f"printf %s {chunk} {redirect} {quoted}.b64"])
        commands.append(["sh", "-c", f"base64 -d < {quoted}.b64 > {quoted} && rm -f {quoted}.b64"])
    return commands


class DockerSandbox(Sandbox):
    """容器隔离的代码执行。

    安全姿态：丢掉所有 capability、禁 setuid 提权、只读根文件系统 + 可写 tmpfs、
    默认断网、非 root 用户、限制内存 / CPU / 进程数。
    同一个 session_id 复用容器，这样多次执行之间文件是连续的。
    """

    name = "docker"

    def __init__(self, image: str | None = None) -> None:
        self.image = image or settings.sandbox_image
        self._client: Any = None
        self._sessions: dict[str, str] = {}  # session_id -> container id
        self._lock = asyncio.Lock()
        self._image_ready = False

    # ---------------- 基础设施 ----------------

    def _get_client(self) -> Any:
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    async def health(self) -> dict[str, Any]:
        try:
            info = await asyncio.to_thread(lambda: self._get_client().version())
            has_image = await asyncio.to_thread(self._has_image)
            return {
                "backend": self.name,
                "available": True,
                "docker_version": info.get("Version"),
                "image": self.image,
                "image_pulled": has_image,
                "sessions": len(self._sessions),
            }
        except Exception as e:  # noqa: BLE001
            return {"backend": self.name, "available": False, "error": str(e)}

    def _has_image(self) -> bool:
        import docker.errors

        try:
            self._get_client().images.get(self.image)
            return True
        except docker.errors.ImageNotFound:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def ensure_image(self) -> None:
        """首次执行时拉镜像。拉取可能要几十秒，所以单独暴露出来让 API 可以预热。"""
        if self._image_ready:
            return
        if await asyncio.to_thread(self._has_image):
            self._image_ready = True
            return
        await asyncio.to_thread(lambda: self._get_client().images.pull(self.image))
        self._image_ready = True

    def _create_container(self, limits: SandboxLimits) -> Any:
        client = self._get_client()
        return client.containers.run(
            self.image,
            command=["sleep", str(max(limits.timeout * 20, 600))],
            detach=True,
            working_dir=_WORKDIR,
            network_disabled=not limits.network,
            mem_limit=f"{limits.memory_mb}m",
            memswap_limit=f"{limits.memory_mb}m",  # 禁止用 swap 绕过内存限制
            nano_cpus=int(limits.cpus * 1e9),
            pids_limit=128,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            read_only=True,
            # mode=1777：tmpfs 默认属 root、755，非 root 的 nobody 会写不进去
            tmpfs={
                _WORKDIR: "rw,exec,size=128m,mode=1777",
                "/tmp": "rw,exec,size=64m,mode=1777",
            },
            user="nobody",
            environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
            labels={"agentlab": "sandbox"},
            auto_remove=False,
        )

    async def _get_container(self, session_id: str | None, limits: SandboxLimits) -> Any:
        client = self._get_client()
        async with self._lock:
            if session_id and session_id in self._sessions:
                try:
                    container = await asyncio.to_thread(
                        client.containers.get, self._sessions[session_id]
                    )
                    if container.status == "running":
                        return container
                except Exception:  # noqa: BLE001 - 容器没了就重建
                    self._sessions.pop(session_id, None)
            container = await asyncio.to_thread(self._create_container, limits)
            if session_id:
                self._sessions[session_id] = container.id
            return container

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
        limits = limits or SandboxLimits()
        language = (language or "python").lower()
        if language not in _LANG_CMD:
            return ExecResult(
                ok=False, backend=self.name, error=f"不支持的语言：{language}"
            )

        started = time.perf_counter()
        ephemeral = session_id is None
        sid = session_id or f"tmp-{uuid.uuid4().hex[:8]}"
        container = None
        try:
            await self.ensure_image()
            container = await self._get_container(sid, limits)

            payload = dict(files or {})
            payload[_LANG_FILE[language]] = code

            def _stage() -> tuple[int, bytes]:
                for cmd in _write_commands(payload):
                    res = container.exec_run(cmd, workdir=_WORKDIR, user="nobody")
                    if res.exit_code != 0:
                        return res.exit_code, res.output or b""
                return 0, b""

            stage_code, stage_out = await asyncio.to_thread(_stage)
            if stage_code != 0:
                return ExecResult(
                    ok=False,
                    backend=self.name,
                    exit_code=stage_code,
                    error=f"写入代码失败：{stage_out.decode(errors='replace')[:500]}",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

            cmd = _LANG_CMD[language]
            exec_env = {"HOME": "/tmp", **(env or {})}

            def _exec() -> tuple[int, bytes, bytes]:
                res = container.exec_run(
                    cmd,
                    workdir=_WORKDIR,
                    environment=exec_env,
                    demux=True,
                    user="nobody",
                )
                out, err = res.output if isinstance(res.output, tuple) else (res.output, b"")
                return res.exit_code, out or b"", err or b""

            try:
                exit_code, out, err = await asyncio.wait_for(
                    asyncio.to_thread(_exec), timeout=limits.timeout
                )
            except asyncio.TimeoutError:
                # 超时就把容器整个干掉，防止里面还有进程在烧 CPU
                await self._kill(sid)
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
            return ExecResult(
                ok=exit_code == 0,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                truncated=t1 or t2,
                backend=self.name,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as e:  # noqa: BLE001
            return ExecResult(
                ok=False,
                backend=self.name,
                error=f"{type(e).__name__}: {e}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            if ephemeral:
                await self._kill(sid)

    async def _kill(self, session_id: str) -> None:
        cid = self._sessions.pop(session_id, None)
        if not cid:
            return
        client = self._get_client()

        def _remove() -> None:
            try:
                client.containers.get(cid).remove(force=True)
            except Exception:  # noqa: BLE001
                pass

        await asyncio.to_thread(_remove)

    async def cleanup(self, session_id: str) -> None:
        await self._kill(session_id)

    async def close(self) -> None:
        for sid in list(self._sessions):
            await self._kill(sid)

    async def reap_orphans(self) -> int:
        """清掉上次进程崩溃留下的沙箱容器。"""

        def _reap() -> int:
            client = self._get_client()
            containers = client.containers.list(
                all=True, filters={"label": "agentlab=sandbox"}
            )
            live = set(self._sessions.values())
            count = 0
            for c in containers:
                if c.id in live:
                    continue
                try:
                    c.remove(force=True)
                    count += 1
                except Exception:  # noqa: BLE001
                    pass
            return count

        try:
            return await asyncio.to_thread(_reap)
        except Exception:  # noqa: BLE001
            return 0
