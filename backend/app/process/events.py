"""待办、动作在运行之外记的事件（task.* / action.*）。

这些事件大多发生在运行停着的时候（有人回复、到期升级、核实），也有发生在执行当中的（开待办、动作挂起）。
停着的运行要接在库里最后一条后面写（engine/run_append.py：进程重启过内存里的序号从 0 起，直接取号会撞号）；
执行当中的交给 run_manager.note，和执行任务共用一个序号计数。已经结束、封存的运行不再写：事件会落在封存范围外，
又不是批注。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run


async def record(run_id: str, event_type: EventType, *, node_id: str | None = None, **data: Any) -> bool:
    """往运行里记一条事件。写进去了返回 True；运行已经结束（或不存在）返回 False。"""
    from app.engine.run_append import append_events
    from app.engine.runner import run_manager

    async with SessionLocal() as session:
        status = await session.scalar(select(Run.status).where(Run.id == run_id))
    if status == "interrupted":
        if await append_events(run_id, [(event_type, node_id, data)], require="interrupted") is not None:
            return True
        # 读完状态之后运行被恢复了：落到下面按执行当中的记
        async with SessionLocal() as session:
            status = await session.scalar(select(Run.status).where(Run.id == run_id))
    if status in ("running", "queued") or run_manager.is_active(run_id):
        await run_manager.note(run_id, event_type, node_id=node_id, **data)
        return True
    return False
