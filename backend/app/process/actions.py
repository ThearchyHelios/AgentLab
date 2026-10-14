"""动作服务：写外部系统（附录 C，BE-2 提供）。ActionRecord 是权威记录：先写记录再发送，结果未知永不自动重发。"""
from __future__ import annotations

from datetime import datetime
from typing import Any


class ActionPending(Exception):
    """写入的结果未知（或已发出、等核实）。runner 把运行收成 interrupted（挂起，不封存），等核实后接着跑。"""

    def __init__(self, action_id: str, message: str = "写入的结果未知，等待核实") -> None:
        super().__init__(message)
        self.action_id = action_id


def action_key(run_id: str, node_id: str, exec_no: int) -> str:
    """sha256(run_id, node_id, exec)。同一次执行第 n 次发送的幂等键是 f"{action_key}:{attempt}"。"""
    import hashlib

    return hashlib.sha256(f"{run_id}\x1f{node_id}\x1f{exec_no}".encode()).hexdigest()


async def reconcile(action_id: str, principal: Any, *, outcome: str, external_id: str | None = None,
                    note: str | None = None) -> dict[str, Any]:
    """核实：outcome ∈ confirmed | not_applied_resend | not_applied_abandon。核实后系统自动接着跑。"""
    raise NotImplementedError("BE-2")


async def tick(now: datetime | None = None) -> int:
    """调度循环每一拍：对登记了 lookup 的结果未知动作带退避地查询。"""
    raise NotImplementedError("BE-2")
