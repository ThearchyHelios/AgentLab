"""microVM 自检：真起一台 VM，跑一行代码。

deploy.sh 用它决定要不要把 /dev/kvm 交给容器。设备在、权限也对，不等于 VM 起得来：
Docker 的系统调用过滤、宿主机没开嵌套虚拟化，都可能让启动失败。起不来还把设备交给容器，
auto 就会选中 microVM，之后每次执行代码都失败。

    python -m app.sandbox.selftest

成功退出码 0，失败 1。输出一行结论，给人看。
"""
from __future__ import annotations

import asyncio
import sys

from app.sandbox.base import SandboxLimits
from app.sandbox.microvm_sandbox import MicroVMSandbox

MARK = "agentlab-microvm-ok"


async def check() -> tuple[bool, str]:
    sandbox = MicroVMSandbox()
    if not sandbox.available():
        return False, f"microVM 不可用：{sandbox.unavailable_reason() or '运行时未就绪'}"
    try:
        result = await sandbox.run(
            f"print({MARK!r})",
            limits=SandboxLimits(timeout=60, memory_mb=256, cpus=1, network=False),
        )
    finally:
        await sandbox.close()
    if result.ok and MARK in result.stdout:
        return True, f"microVM 可用：启动并执行一行代码共用时 {result.duration_ms} ms"
    detail = result.error or result.stderr.strip() or f"退出码 {result.exit_code}，没有输出"
    return False, detail if detail.startswith("microVM") else f"microVM 启动失败：{detail}"


def main() -> int:
    ok, message = asyncio.run(check())
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
