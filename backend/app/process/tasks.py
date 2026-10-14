"""待办服务（附录 C，BE-2 提供）。

一次中断对应一条 Approval（给引擎）和一条 Task（给人）。所有人的决定都经 respond：按 policy 汇总，凑齐后在同一个
事务里用条件 UPDATE 把 Task 从 pending 改成 resolving，抢到的请求负责调 run_manager.resume。引擎和重放都不改。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class TaskRefused(Exception):
    """回复不被接受：不是派给你的、已经处理过了、哈希对不上……status_code 照 HTTP 语义（403 / 409 / 422）。"""

    def __init__(self, message: str, *, status_code: int = 409, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def open_task(session: AsyncSession, approval: Any) -> Any:
    """为一条新的 Approval 建 Task 和派给谁（按人工节点配置 + 应用版本的岗位绑定）。runner._handle_updates 调。"""
    raise NotImplementedError("BE-2")


async def respond(task_id: str, principal: Any, *, decision: Any, values: dict[str, Any] | None = None,
                  note: str | None = None, payload_hash: str | None = None) -> dict[str, Any]:
    """一个人的回复。返回 {task, resolved: bool, run?}。"""
    raise NotImplementedError("BE-2")


async def reassign(task_id: str, principal: Any, *, to: list[dict[str, str]], note: str | None = None) -> Any:
    raise NotImplementedError("BE-2")


async def tick(now: datetime | None = None) -> int:
    """调度循环每一拍：提醒、升级、超时驳回（永不自动批准）。返回处理的条数。"""
    raise NotImplementedError("BE-2")
