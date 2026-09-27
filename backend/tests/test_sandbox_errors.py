"""沙箱起不来时的报错：说原因，不带 Python 异常类名（SPEC §3.1）。

四个后端起进程、连 VM 失败时以前都写 f"{type(e).__name__}: {e}"，代码节点再把它
拼进首行，界面上的红字就成了「代码执行失败（exit=0）：FileNotFoundError: [Errno 2]
No such file or directory: …」。原始异常留在日志里，排查时照样找得到。
"""
from __future__ import annotations

import asyncio
import re
import types

import pytest

from app.engine.context import NodeContext, NodeError, RunContext
from app.engine.nodes.tools import run_code
from app.engine.schema import GraphNode, GraphSpec
from app.sandbox import interpreter
from app.sandbox.bubblewrap_sandbox import BubblewrapSandbox
from app.sandbox.local_sandbox import LocalSandbox
from app.sandbox.microvm_sandbox import MicroVMSandbox
from app.sandbox.seatbelt_sandbox import SeatbeltSandbox

_CLASS_NAME = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b")


def _broken_interpreter(tmp_path) -> str:
    """一个存在、可执行，但 shebang 指向不存在的解释器的文件：which 找得到，exec 起不来。"""
    fake = tmp_path / "python-broken"
    fake.write_text("#!/nonexistent/interpreter\n")
    fake.chmod(0o755)
    return str(fake)


def test_a_code_node_whose_interpreter_cannot_start_says_why(tmp_path, monkeypatch) -> None:
    import app.engine.nodes.tools as tools

    monkeypatch.setattr(interpreter, "python_argv", lambda: [_broken_interpreter(tmp_path)])
    local = LocalSandbox()

    class _Manager:
        async def run(self, code, *, backend=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
            return await local.run(code, **kwargs)

    monkeypatch.setattr(tools, "sandbox_manager", _Manager())
    node = GraphNode(id="calc", type="code",
                     data={"label": "计算", "config": {"language": "python", "code": "print(1)",
                                                       "isolation": "fast"}})
    ctx = NodeContext(node=node, run=RunContext(run_id="r1", thread_id="t1",
                                                spec=GraphSpec(nodes=[node], edges=[])))

    with pytest.raises(NodeError) as caught:
        asyncio.run(run_code({"vars": {}}, ctx))
    text = str(caught.value)
    assert text.startswith("代码执行失败"), text
    assert not _CLASS_NAME.search(text), text


async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    raise PermissionError(13, "Permission denied", "/usr/bin/sandbox-exec")


@pytest.mark.parametrize("backend", [LocalSandbox, SeatbeltSandbox, BubblewrapSandbox])
def test_a_process_that_cannot_spawn_has_no_class_name(backend, monkeypatch) -> None:
    if backend is not LocalSandbox:
        monkeypatch.setattr(backend, "available", staticmethod(lambda: True))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    result = asyncio.run(backend().run("print(1)"))
    assert not result.ok
    assert result.error and not _CLASS_NAME.search(result.error), result.error


def _microvm(monkeypatch) -> MicroVMSandbox:
    async def nothing(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(MicroVMSandbox, "available", staticmethod(lambda: True))
    monkeypatch.setattr(MicroVMSandbox, "_reap_idle", nothing)
    monkeypatch.setattr(MicroVMSandbox, "_destroy", nothing)
    return MicroVMSandbox()


def test_a_microvm_that_cannot_boot_has_no_class_name(monkeypatch) -> None:
    async def no_vm(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("虚拟化框架拒绝了这次启动")

    vm = _microvm(monkeypatch)
    monkeypatch.setattr(MicroVMSandbox, "_lease", no_vm)
    result = asyncio.run(vm.run("print(1)"))
    assert result.error and result.error.startswith("microVM 启动失败"), result.error
    assert not _CLASS_NAME.search(result.error), result.error


def test_a_microvm_that_drops_mid_exec_has_no_class_name(monkeypatch) -> None:
    async def write(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return None

    async def gone(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ConnectionResetError(54, "Connection reset by peer")

    lease = types.SimpleNamespace(vm=types.SimpleNamespace(fs=types.SimpleNamespace(write=write, mkdir=write),
                                                           exec=gone))

    async def leased(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return lease

    vm = _microvm(monkeypatch)
    monkeypatch.setattr(MicroVMSandbox, "_lease", leased)
    result = asyncio.run(vm.run("print(1)"))
    assert result.error and not _CLASS_NAME.search(result.error), result.error
