"""问数据一轮的事件日志（chat_turn_events，见 db/models_chat.py）：追加、回放、实时推送。

**为什么「先落库、再通知、订阅方回库里读」。** 页面要的是：先回放、再接实时，断了按 seq 补齐，刷新、关掉再打开都能接着看。
事件一律先写库，再叫醒等着的订阅方，订阅方按自己收到的最后一个 seq 回库里读——实时和回放是同一段代码，补齐不会漏、不会重复，
也不用像运行的事件总线那样处理慢消费者的积压。订阅方在读库**之前**先登记等待（watch），读完没东西才睡：读和写之间新写进来的
那一条会把它叫醒，不会漏掉。

叫醒只在本进程里（进程内的事件）；隔一拍心跳（HEARTBEAT_S）订阅方也会回库里看一次，所以换成多进程（第 2 波）时，别的进程写的
事件最迟一拍心跳就送到——那时再换成 PostgreSQL 的 LISTEN/NOTIFY，这里的接口不用变。

线上格式（SSE，每条一行 data: <JSON>）：{seq, type, data, ts}；回放完、这一轮也不在进行中了，补一条不带 seq 的
{type: stream.end, status}（任务的状态）再关；隔一拍没有新事件发一行注释心跳「: ping」（页面据此知道连接还活着）。
事件种类见 app/chat/job.py 的模块说明。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal

#: 隔多久没有新事件就发一拍心跳（秒），也是订阅方回库里看一次的间隔
HEARTBEAT_S = 15.0
#: 一次回放最多读多少条
READ_BATCH = 500
#: 回放、实时推送完、这一轮不在进行中时的结束标记（不落库、不带 seq）
STREAM_END = "stream.end"


class _Waiters:
    """本进程里等某一轮新事件的订阅方：一个订阅方一个 asyncio.Event。"""

    def __init__(self) -> None:
        self._by_turn: dict[str, set[asyncio.Event]] = defaultdict(set)

    @contextlib.contextmanager
    def watch(self, turn_id: str) -> Iterator[asyncio.Event]:
        event = asyncio.Event()
        self._by_turn[turn_id].add(event)
        try:
            yield event
        finally:
            waiters = self._by_turn.get(turn_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    self._by_turn.pop(turn_id, None)

    def notify(self, turn_id: str) -> None:
        for event in list(self._by_turn.get(turn_id, ())):
            event.set()


waiters = _Waiters()


def notify(turn_id: str) -> None:
    """这一轮有了变化（新事件、任务状态变了）：叫醒本进程里等着的订阅方。"""
    waiters.notify(turn_id)


async def add(session: AsyncSession, turn_id: str, type_: str, data: dict[str, Any] | None = None) -> int:
    """在调用方的事务里加一条（不提交），交回它的 seq。

    seq 由任务行上的计数器发：UPDATE chat_turn_jobs SET last_seq = last_seq + 1 … RETURNING 是一句原子的写，同一轮同时追加的
    几处（这一轮的任务、停止、晚到的复核、发起下一次尝试）各拿到不同的号，唯一约束 (turn_id, seq) 兜底。以前按库里已有的最大值
    往下接，同时两处追加就撞号。任务行的 last_seq 也就是 GET /api/chat/turns/{id} 说的「现在到哪了」。没有任务行的（不该有）
    退回按最大值往下接。"""
    from app.db.models_chat import ChatTurnEvent, ChatTurnJob

    seq = (await session.execute(update(ChatTurnJob).where(ChatTurnJob.id == turn_id)
                                 .values(last_seq=ChatTurnJob.last_seq + 1).returning(ChatTurnJob.last_seq)
                                 .execution_options(synchronize_session=False))).scalar_one_or_none()
    if seq is None:
        seq = int(await session.scalar(select(func.coalesce(func.max(ChatTurnEvent.seq), 0))
                                       .where(ChatTurnEvent.turn_id == turn_id)) or 0) + 1
    session.add(ChatTurnEvent(turn_id=turn_id, seq=int(seq), type=type_, ts=time.time(), data=dict(data or {})))
    await session.flush()
    return int(seq)


async def append(session: AsyncSession, turn_id: str, type_: str, data: dict[str, Any] | None = None) -> int:
    """追加一条，提交，叫醒订阅方。交回它的 seq（见 add）。"""
    seq = await add(session, turn_id, type_, data)
    await session.commit()
    notify(turn_id)
    return seq


def wire(row: Any) -> dict[str, Any]:
    """库里的一条 → 线上的一条。"""
    return {"seq": row.seq, "type": row.type, "data": row.data or {}, "ts": row.ts}


async def read(turn_id: str, after: int = 0, limit: int = READ_BATCH) -> list[dict[str, Any]]:
    """seq > after 的事件（按 seq），最多 limit 条。"""
    from app.db.models_chat import ChatTurnEvent

    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ChatTurnEvent).where(ChatTurnEvent.turn_id == turn_id, ChatTurnEvent.seq > after)
            .order_by(ChatTurnEvent.seq).limit(limit))).scalars().all()
    return [wire(r) for r in rows]


def frame(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


#: 心跳（SSE 注释行）：页面的解析器只认 data: 开头的帧，这一行只是告诉它连接还活着
PING = ": ping\n\n"


async def stream(turn_id: str, after: int, *, active: Callable[[], Awaitable[str | None]],
                 heartbeat: float | None = None) -> AsyncIterator[str]:
    """SSE 的每一帧：先回放 seq > after 的，再推实时的；这一轮不在进行中、也没有新的了，补一条 stream.end 再结束。

    active() 交回这一轮还在进行中时为 None，不在进行中时交回任务的状态（stream.end 里带的就是它）。"""
    beat = HEARTBEAT_S if heartbeat is None else heartbeat
    last = after
    while True:
        with waiters.watch(turn_id) as woke:
            rows = await read(turn_id, last)
            for row in rows:
                last = max(last, int(row["seq"]))
                yield frame(row)
            if rows:
                continue
            status = await active()
            if status is not None:
                yield frame({"type": STREAM_END, "status": status, "data": {"status": status}})
                return
            try:
                await asyncio.wait_for(woke.wait(), timeout=beat)
            except asyncio.TimeoutError:
                yield PING


__all__ = ["HEARTBEAT_S", "PING", "READ_BATCH", "STREAM_END", "add", "append", "frame", "notify", "read", "stream", "waiters",
           "wire"]
