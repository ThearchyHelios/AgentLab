"""Linux 上 microVM 没有 KVM 就起不来，可用性判断要把这一条算进去。

以前 available() 只看运行时装没装。运行时和沙箱镜像打进 Docker 镜像以后，没有 KVM 的机器上
（Mac 上的 Docker Desktop、没开嵌套虚拟化的云主机）auto 会选中 microVM，每次执行代码都启动失败。
selftest 是部署脚本用的：真起一台 VM，起得来才把 /dev/kvm 交给容器。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.sandbox import manager as mgr
from app.sandbox import microvm_sandbox as mv
from app.sandbox import selftest
from app.sandbox.base import ExecResult
from app.sandbox.local_sandbox import LocalSandbox


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """在 Linux 上、运行时已装好、镜像已缓存；返回假的 /dev/kvm 路径，默认不存在。"""
    kvm = tmp_path / "kvm"
    monkeypatch.setattr(mv, "_on_linux", lambda: True)
    monkeypatch.setattr(mv, "_KVM_DEVICE", kvm)
    monkeypatch.setattr(mv.MicroVMSandbox, "_runtime_installed", staticmethod(lambda: True))
    monkeypatch.setattr(mv.MicroVMSandbox, "image_ready", staticmethod(lambda: True))
    return kvm


def test_no_kvm_device_means_unavailable(linux: Path) -> None:
    assert mv.MicroVMSandbox.available() is False
    reason = mv.MicroVMSandbox.unavailable_reason()
    assert "/dev/kvm" in reason and "挂进容器" in reason


def test_kvm_without_permission_means_unavailable(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    linux.touch()
    monkeypatch.setattr(mv.os, "access", lambda path, mode: False)
    assert mv.MicroVMSandbox.available() is False
    assert "kvm 组" in mv.MicroVMSandbox.unavailable_reason()


def test_kvm_present_and_writable(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    linux.touch()
    monkeypatch.setattr(mv.os, "access", lambda path, mode: True)
    assert mv.MicroVMSandbox.available() is True
    assert mv.MicroVMSandbox.unavailable_reason() == ""


def test_macos_does_not_need_kvm(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mv, "_on_linux", lambda: False)
    assert not linux.exists()
    assert mv.kvm_problem() == ""
    assert mv.MicroVMSandbox.available() is True


def test_runtime_missing_is_reported_before_kvm(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mv.MicroVMSandbox, "_runtime_installed", staticmethod(lambda: False))
    assert "运行时未就绪" in mv.MicroVMSandbox.unavailable_reason()


def test_auto_skips_microvm_without_kvm(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 运行时装了、镜像也缓存了，只差 KVM：auto 不能选它
    monkeypatch.setattr(mgr.BubblewrapSandbox, "available", staticmethod(lambda: False))
    monkeypatch.setattr(mgr.BubblewrapSandbox, "probe_failure", staticmethod(lambda: ""))
    monkeypatch.setattr(mgr.SeatbeltSandbox, "available", staticmethod(lambda: False))
    monkeypatch.setattr(mgr.settings, "sandbox_backend", "auto")
    manager = mgr.SandboxManager()
    assert isinstance(manager._resolve(), LocalSandbox)
    # 节点上选 strict 档也点不到它
    assert manager._resolve_named("strict") is None


def test_auto_picks_microvm_with_kvm(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    linux.touch()
    monkeypatch.setattr(mv.os, "access", lambda path, mode: True)
    monkeypatch.setattr(mgr.settings, "sandbox_backend", "auto")
    manager = mgr.SandboxManager()
    assert isinstance(manager._resolve(), mv.MicroVMSandbox)


def test_run_says_why_without_booting(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def must_not_boot(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("没有 KVM 还去启动 VM")

    monkeypatch.setattr(mv.MicroVMSandbox, "_lease", must_not_boot)
    result = asyncio.run(mv.MicroVMSandbox().run("print(1)"))
    assert result.ok is False
    assert result.error and result.error.startswith("microVM 不可用：") and "/dev/kvm" in result.error


def test_health_reports_missing_kvm(linux: Path) -> None:
    info = asyncio.run(mv.MicroVMSandbox().health())
    assert info["available"] is False
    assert "/dev/kvm" in info["error"]
    assert "KVM" in info["isolation"]


# ---------------- selftest ----------------


def _fake_run(result: ExecResult, calls: list[str]):
    async def run(self, code, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(code)
        return result

    return run


def test_selftest_without_kvm_does_not_boot(linux: Path, monkeypatch: pytest.MonkeyPatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mv.MicroVMSandbox, "run", _fake_run(ExecResult(), calls))
    assert selftest.main() == 1
    assert calls == []
    out = capsys.readouterr().out.strip()
    assert out.startswith("microVM 不可用：") and "/dev/kvm" in out


def test_selftest_passes_when_the_vm_prints_the_mark(linux: Path, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    linux.touch()
    monkeypatch.setattr(mv.os, "access", lambda path, mode: True)
    calls: list[str] = []
    ok = ExecResult(stdout=selftest.MARK + "\n", duration_ms=812, backend="microvm")
    monkeypatch.setattr(mv.MicroVMSandbox, "run", _fake_run(ok, calls))
    assert selftest.main() == 0
    assert len(calls) == 1 and selftest.MARK in calls[0]
    assert "812 ms" in capsys.readouterr().out


def test_selftest_reports_boot_failure(linux: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    linux.touch()
    monkeypatch.setattr(mv.os, "access", lambda path, mode: True)
    failed = ExecResult(ok=False, backend="microvm", error="microVM 启动失败：虚拟机没有应答")
    monkeypatch.setattr(mv.MicroVMSandbox, "run", _fake_run(failed, []))
    assert selftest.main() == 1
    assert capsys.readouterr().out.strip() == "microVM 启动失败：虚拟机没有应答"


def test_selftest_does_not_trust_exit_zero_without_output(linux: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    # 退出码 0 但什么都没打印：VM 里的代码其实没跑起来，不能当成功
    linux.touch()
    monkeypatch.setattr(mv.os, "access", lambda path, mode: True)
    monkeypatch.setattr(mv.MicroVMSandbox, "run", _fake_run(ExecResult(backend="microvm"), []))
    assert selftest.main() == 1
    assert capsys.readouterr().out.strip().startswith("microVM 启动失败：")
