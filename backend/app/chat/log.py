"""问数据一轮的事件日志（chat_turn_events，见 db/models_chat.py）：追加、回放、实时推送。

**为什么「先落库、再通知、订阅方回库里读」。** 页面要的是：先回放、再接实时，断了按 seq 补齐，刷新、关掉再打开都能接着看。
事件一律先写库，再叫醒等着的订阅方，订阅方按自己收到的最后一个 seq 回库里读——实时和回放是同一段代码，补齐不会漏、不会重复，
也不用像运行的事件总线那样处理慢消费者的积压。订阅方在读库**之前**先登记等待（watch），读完没东西才睡：读和写之间新写进来的
那一条会把它叫醒，不会漏掉。

叫醒在本进程里是进程内的事件；多进程（PostgreSQL）时另发 NOTIFY（app/cluster/relay.py），别的进程写的事件、改的状态马上叫醒这里
的流；NOTIFY 丢了的话，隔一拍心跳（HEARTBEAT_S）订阅方也会回库里看一次。SQLite 只有一个进程，不额外轮询。

线上格式（SSE，每条一行 data: <JSON>）：{seq, type, data, ts}；回放完、这一轮也不在进行中了，补一条不带 seq 的
{type: stream.end, status}（任务的状态）再关；隔一拍没有新事件发一行注释心跳「: ping」（页面据此知道连接还活着）。
事件种类见 app/chat/job.py 的模块说明。

**助手的心跳只落换了阶段的那一拍。** 模型几秒没出声时助手发一拍 {op: heartbeat, phase, elapsed_ms}（页面据此写阶段、已等多久）：
以前每 3 秒落一条，一轮几百条。现在换了阶段的那一拍（规划 → 查看 → 搭建……，说的是到了哪一步）照落，刷新、重开时过程里那一行
阶段回放得出来；同一阶段接着的那些是纯保活，只记在本进程里（beat：每一轮最近的一拍），流上发一条不带 seq 的 {type: op, data}；
重连时流先补最近的那一拍。之后落了任何一条事件，那一拍就作废（心跳只说「上一条事件之后还在等」，重连时补旧的会让页面的进度
退回去）。其余事件照落：跨进程的实时流从库里按 seq 追；保活心跳不落库，执行这一轮的进程只在别的进程有流在看时把它转过去
（app/cluster/relay.py 的 w / b 两种通知），丢了无妨——心跳本来就不进回放。

**清理。** 已结束（成功、失败、取消、中断）超过 RETAIN_DAYS 天的轮次整轮删事件日志（purge，挂在调度循环的那一拍里，一小时最多一次）：
页面重开已结束的一轮读的是 conversation_turns 那一行（答案、图、复核、用量、几次尝试都在那里），只有还在进行中的轮次才回放
事件日志。任务行留着：last_seq 接着发号，之后在这一轮上重试、继续运行不撞号。
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal

logger = logging.getLogger(__name__)

#: 隔多久没有新事件就发一拍心跳（秒），也是订阅方回库里看一次的间隔
HEARTBEAT_S = 15.0
#: 一次回放最多读多少条
READ_BATCH = 500
#: 回放、实时推送完、这一轮不在进行中时的结束标记（不落库、不带 seq）
STREAM_END = "stream.end"
#: 已结束的轮次的事件日志留几天
RETAIN_DAYS = 14
#: 清理多久最多跑一次（秒）、一次最多清几轮
PURGE_EVERY_S = 3600.0
PURGE_BATCH = 500
#: 结束了的任务状态（core/job_runtime 的状态词里不再往下走的那几种）
FINISHED = ("succeeded", "failed", "cancelled", "interrupted")


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
    """这一轮有了变化（新事件、任务状态变了）：叫醒本进程里等着的订阅方；多进程时另发 NOTIFY t|turn_id，别的进程上的流
    也叫醒（app/cluster/relay.py；SQLite 只有一个进程，什么都不发）。"""
    waiters.notify(turn_id)
    from app.cluster.signal import signals

    signals.publish("t", turn_id)


#: 每一轮最近的一拍心跳：(第几拍, 操作)。只在本进程里，不落库（见模块说明）
_beats: dict[str, tuple[int, dict[str, Any]]] = {}
_beat_no = itertools.count(1)


def beat(turn_id: str, op: dict[str, Any]) -> None:
    """助手的一拍心跳：记下最近的这一拍、叫醒订阅方，由流发出去（不落库、不占 seq）。别的进程上有流在看这一轮时转过去
    （app/cluster/relay.py：不落库的东西只能这样跨进程）。"""
    _beats[turn_id] = (next(_beat_no), dict(op))
    waiters.notify(turn_id)
    from app.cluster.relay import relay

    relay.beat(turn_id, op)


def remote_beat(turn_id: str, op: dict[str, Any]) -> None:
    """别的进程转过来的一拍心跳（这一轮在那边执行、这边有流在看）：同本进程的一拍。"""
    _beats[turn_id] = (next(_beat_no), dict(op))
    waiters.notify(turn_id)


def latest_beat(turn_id: str) -> dict[str, Any] | None:
    got = _beats.get(turn_id)
    return dict(got[1]) if got else None


def forget(turn_id: str) -> None:
    """这一轮结束了：进程里记的那一拍清掉。"""
    _beats.pop(turn_id, None)


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
    # 落了新事件，上一拍心跳作废：重连时不再补它（心跳只说「上一条事件之后还在等」）
    _beats.pop(turn_id, None)
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
    from app.cluster.relay import relay

    with relay.watch_turn(turn_id):
        async for frame_ in _stream(turn_id, after, active=active, heartbeat=heartbeat):
            yield frame_


async def _stream(turn_id: str, after: int, *, active: Callable[[], Awaitable[str | None]],
                  heartbeat: float | None) -> AsyncIterator[str]:
    interval = HEARTBEAT_S if heartbeat is None else heartbeat
    last = after
    #: 这条连接发过的最近一拍助手心跳（_beats 的第几拍）：新连上的从 0 起，先补最近的那一拍
    sent = 0
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
            got = _beats.get(turn_id)
            if got is not None and got[0] > sent:
                sent = got[0]
                # 不带 seq：页面按 seq 补齐时不算它（frontend/src/api/chatTurns.ts）
                yield frame({"type": "op", "data": got[1], "ts": time.time()})
                continue
            try:
                await asyncio.wait_for(woke.wait(), timeout=interval)
            except asyncio.TimeoutError:
                yield PING


async def purge(now: datetime, *, retain_days: float = RETAIN_DAYS, limit: int = PURGE_BATCH) -> list[str]:
    """结束超过 retain_days 天的轮次整轮删事件日志（见模块说明），交回清了哪几轮。任务行、conversation_turns 那一行不动。
    结束的时刻按任务的 finished_at，没记的按 updated_at。"""
    from app.db.models_chat import ChatTurnEvent, ChatTurnJob

    cutoff = now - timedelta(days=retain_days)
    async with SessionLocal() as session:
        ended = select(ChatTurnJob.id).where(ChatTurnJob.status.in_(FINISHED),
                                             func.coalesce(ChatTurnJob.finished_at, ChatTurnJob.updated_at) < cutoff)
        turns = list((await session.execute(select(ChatTurnEvent.turn_id).where(ChatTurnEvent.turn_id.in_(ended))
                                            .distinct().limit(limit))).scalars())
        if turns:
            await session.execute(delete(ChatTurnEvent).where(ChatTurnEvent.turn_id.in_(turns))
                                  .execution_options(synchronize_session=False))
            await session.commit()
    return turns


_last_purge = 0.0


async def purge_tick(now: datetime) -> list[str]:
    """调度循环每一拍调（scheduler/loop.process_due）：一小时最多清一次。"""
    global _last_purge
    if time.monotonic() - _last_purge < PURGE_EVERY_S and _last_purge:
        return []
    _last_purge = time.monotonic()
    return await purge(now)


__all__ = ["FINISHED", "HEARTBEAT_S", "PING", "READ_BATCH", "RETAIN_DAYS", "STREAM_END", "add", "append", "beat", "forget",
           "frame", "latest_beat", "notify", "purge", "purge_tick", "read", "stream", "waiters", "wire"]
