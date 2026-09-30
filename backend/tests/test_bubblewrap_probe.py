"""装了 bwrap 不等于建得了隔离环境。

Docker 默认的 seccomp 配置不许非特权进程建 user namespace，Ubuntu 24.04 的 AppArmor 默认也限制它。
以前 available() 只看 bwrap 在不在：容器里 auto 选中 bubblewrap，每次执行都报
「No permissions to create a new namespace」，而本地子进程明明能用。现在真起一次 bwrap 试，
起不来就算不可用，auto 往下退。
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest

from app.sandbox import bubblewrap_sandbox as bw
from app.sandbox import manager as mgr
from app.sandbox.local_sandbox import LocalSandbox

DENIED = (
    "bwrap: No permissions to create a new namespace, likely because the kernel does not "
    "allow non-privileged user namespaces.\n"
)


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch: pytest.MonkeyPatch):
    bw._probe.cache_clear()
    monkeypatch.setattr(bw, "_on_linux", lambda: True)
    yield
    bw._probe.cache_clear()


def _fake_run(returncode: int, stderr: str = "", calls: list[Any] | None = None):
    def run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

    return run


def test_binary_present_but_namespaces_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(bw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bw.subprocess, "run", _fake_run(1, DENIED, calls))

    assert bw.BubblewrapSandbox.available() is False
    assert "No permissions" in bw.BubblewrapSandbox.probe_failure()
    # 探测真的在起 bwrap，并且带上执行时同样要用的命名空间和 /proc
    argv = calls[0]
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in argv and "--proc" in argv
    # 结果缓存：内核和容器权限在进程生命周期里不会变
    bw.BubblewrapSandbox.available()
    assert len(calls) == 1


async def test_health_explains_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bw.subprocess, "run", _fake_run(1, DENIED))
    info = await bw.BubblewrapSandbox().health()
    assert info["available"] is False
    assert "No permissions" in info["error"]


def test_binary_present_and_working(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bw.subprocess, "run", _fake_run(0))
    assert bw.BubblewrapSandbox.available() is True
    assert bw.BubblewrapSandbox.probe_failure() == ""


def test_probe_that_cannot_start_counts_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(bw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bw.subprocess, "run", boom)
    assert bw.BubblewrapSandbox.available() is False


def test_no_binary_skips_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(bw.shutil, "which", lambda name: None)
    monkeypatch.setattr(bw.subprocess, "run", _fake_run(0, calls=calls))
    assert bw.BubblewrapSandbox.available() is False
    assert bw.BubblewrapSandbox.probe_failure() == ""
    assert calls == []


def test_auto_falls_back_to_local_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bw.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bw.subprocess, "run", _fake_run(1, DENIED))
    monkeypatch.setattr(mgr.MicroVMSandbox, "available", staticmethod(lambda: False))
    monkeypatch.setattr(mgr.SeatbeltSandbox, "available", staticmethod(lambda: False))
    monkeypatch.setattr(mgr.settings, "sandbox_backend", "auto")

    manager = mgr.SandboxManager()
    backend = manager._resolve()
    assert isinstance(backend, LocalSandbox)
    assert "bwrap" in manager._resolved_from
    assert "本地子进程" in manager._resolved_from
    # 节点上选 fast 档也不能点到一个起不来的 bubblewrap
    monkeypatch.setattr(mgr.sys, "platform", "linux")
    assert manager._resolve_named("fast") is None
