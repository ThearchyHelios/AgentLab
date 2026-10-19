"""实时流跨进程（docs/design/orchestration-round/00-总方案.md §5 W2-b「实时流跨进程」）。

**为什么。** 运行的事件先落库、再在本进程的事件总线上推给订阅方（core/bus）；问数据一轮的事件同样先落库，再叫醒本进程里
等着的流（chat/log）。多进程时，页面的 WebSocket / SSE 连到的进程不一定是执行这次运行、这一轮的那个进程。

**怎么做。** 订阅方在哪个进程，就由那个进程的转发器（Relay）从库里按 seq 追：
- 运行：WebSocket 接上之后登记跟随（follow_run）。执行它的进程每提交一条事件，发 NOTIFY r|run_id|seq（cluster/signal，
  攒一下批量发）；跟随它的进程收到后，只要这次运行不在本进程里执行，就从库里读 seq 大于手上游标的事件、在本进程的总线上
  发布一遍（relayed，不再往外转），订阅方收到的和执行它的进程上的一模一样，序号由执行的进程发、不重复。到了终态（或者
  挂起、属主不在了被标中断）再关掉本进程的订阅方，同执行进程上的规矩。NOTIFY 丢了也不怕：跟随的运行隔 POLL_S 秒回库里看一次。
- 流式增量（正文 token、思考 delta）不落库：跟随的进程隔几秒发一次「我在看」（w|r:run_id），执行的进程只给有人在看的运行把
  增量攒一小段、放进载荷转过去（e|run_id|[事件…]）。
- 问数据一轮：流本来就是从库里读的，这里只管叫醒——追加一条事件、任务状态变了都发 t|turn_id，收到的进程叫醒本进程里等着
  这一轮的流；助手的保活心跳（不落库，chat/log.beat）同样只给有人在看的那几轮转过去（b|turn_id|心跳）。
- SQLite 只有一个进程：signal 不收不发，这里什么都不做（单进程照旧走进程内的叫醒，不额外轮询）。

WebSocket / SSE 的回放、重连、心跳的说法都不变：回放照旧读库，转发器只补「实时」那一段。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterator
from typing import Any

from sqlalchemy import select

from app.cluster.signal import signals
from app.core.events import EventType, RunEventModel
from app.db.base import SessionLocal

logger = logging.getLogger(__name__)

#: 在看的一方隔多久重发一次「我在看」；执行的一方收到后记多久
WATCH_S = 5.0
WATCH_TTL_S = 15.0
#: 流式增量攒多久转一次
EPHEMERAL_FLUSH_S = 0.05
#: 跟随的运行隔多久回库里看一次（NOTIFY 丢了的兜底）
POLL_S = 2.0
#: 一条载荷最多多少字节（PostgreSQL 的 NOTIFY 上限是 8000）
PAYLOAD_MAX = 7000
#: 不落库的事件（同 engine/runner._EPHEMERAL）
EPHEMERAL = {EventType.LLM_TOKEN, EventType.LLM_THINKING_DELTA}
_TERMINAL = ("succeeded", "failed", "cancelled")


class Relay:
    def __init__(self) -> None:
        #: 本进程里在跟随的运行：{run_id: 跟随的连接数}、游标（已经在本进程发布过的最大 seq）
        self._follow: dict[str, int] = {}
        self._cursor: dict[str, int] = {}
        self._pumping: dict[str, asyncio.Lock] = {}
        #: 本进程里在看的问数据一轮
        self._turns: dict[str, int] = {}
        #: 别的进程在看的（执行的一方记）：{run_id / turn_id: 到期的时刻}
        self._watched_runs: dict[str, float] = {}
        self._watched_turns: dict[str, float] = {}
        #: 攒着要转的流式增量：{run_id: [事件]}
        self._buffer: dict[str, list[dict[str, Any]]] = {}
        self._flusher: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._installed = False

    # ---------------- 起停 ----------------

    def install(self) -> None:
        """登记收通知的处理、接上事件总线（起服务时，PostgreSQL 才有意义；重复调用无害）。"""
        from app.core.bus import bus

        if not self._installed:
            signals.on("r", self._on_run)
            signals.on("e", self._on_ephemeral, raw=True)
            signals.on("w", self._on_watch)
            signals.on("t", self._on_turn)
            signals.on("b", self._on_beat, raw=True)
            signals.on_resync(self._resync)
            self._installed = True
        bus.tap = self._tap

    async def start(self) -> None:
        self.install()
        if signals.active and (self._loop_task is None or self._loop_task.done()):
            self._loop_task = asyncio.create_task(self._loop(), name="agentlab-cluster-relay")

    async def stop(self) -> None:
        from app.core.bus import bus

        if bus.tap == self._tap:
            bus.tap = None
        for task in (self._loop_task, self._flusher):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._loop_task = self._flusher = None
        self._buffer.clear()

    # ---------------- 跟随（订阅方所在的进程） ----------------

    @contextlib.contextmanager
    def follow_run(self, run_id: str, after: int = 0) -> Iterator[None]:
        """这条连接在看这次运行（WebSocket 接上、回放完之后）：在本进程里没在执行时从库里追。"""
        self._follow[run_id] = self._follow.get(run_id, 0) + 1
        self._cursor[run_id] = max(self._cursor.get(run_id, 0), int(after or 0))
        if signals.active:
            signals.publish("w", f"r:{run_id}")
            self._kick(run_id)
        try:
            yield
        finally:
            left = self._follow.get(run_id, 1) - 1
            if left <= 0:
                self._follow.pop(run_id, None)
                self._cursor.pop(run_id, None)
                self._pumping.pop(run_id, None)
            else:
                self._follow[run_id] = left

    @contextlib.contextmanager
    def watch_turn(self, turn_id: str) -> Iterator[None]:
        """这条 SSE 在看这一轮：执行它的进程把保活心跳转过来。"""
        self._turns[turn_id] = self._turns.get(turn_id, 0) + 1
        if signals.active:
            signals.publish("w", f"t:{turn_id}")
        try:
            yield
        finally:
            left = self._turns.get(turn_id, 1) - 1
            if left <= 0:
                self._turns.pop(turn_id, None)
            else:
                self._turns[turn_id] = left

    def _kick(self, run_id: str) -> None:
        if run_id in self._follow:
            task = asyncio.ensure_future(self.pump(run_id))
            task.add_done_callback(_quiet)

    async def pump(self, run_id: str) -> int:
        """把库里 seq 大于游标的事件在本进程发布一遍（这次运行在本进程里执行时什么都不做）。交回发布了几条。"""
        from app.core.bus import bus
        from app.db.models import Run, RunEvent
        from app.engine.runner import run_manager

        if run_id not in self._follow or run_manager.is_active(run_id):
            return 0
        lock = self._pumping.setdefault(run_id, asyncio.Lock())
        async with lock:
            after = self._cursor.get(run_id, 0)
            async with SessionLocal() as session:
                # 先读状态、再读事件：终态和终态事件是同一个事务提交的，读到终态时终态事件一定也读得到
                status = await session.scalar(select(Run.status).where(Run.id == run_id))
                rows = list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id,
                                                                          RunEvent.seq > after)
                                                   .order_by(RunEvent.seq))).scalars())
            last_log = None
            for row in rows:
                event = RunEventModel(id=row.id, run_id=run_id, seq=row.seq, type=EventType(row.type),
                                      node_id=row.node_id, ts=row.ts, data=row.data or {})
                await bus.publish(event, relayed=True)
                self._cursor[run_id] = max(self._cursor.get(run_id, 0), int(row.seq or 0))
                last_log = row
            ended = status is None or status in _TERMINAL or (
                status == "interrupted" and last_log is not None and last_log.type == EventType.LOG
                and (last_log.data or {}).get("code") == "server_shutdown")
            if ended and (rows or status is None or status in _TERMINAL):
                # 同执行进程上的规矩（runner._finalize）：到了终态、关停挂起的，让本进程的订阅方收到终态再结束
                await bus.close(run_id)
        return len(rows)

    def _on_run(self, parts: list[str]) -> None:
        if parts:
            self._kick(parts[0])

    def _on_ephemeral(self, parts: list[str]) -> None:
        from app.core.bus import bus
        from app.engine.runner import run_manager

        if len(parts) < 2 or parts[0] not in self._follow or run_manager.is_active(parts[0]):
            return
        try:
            items = json.loads(parts[1])
        except ValueError:
            return
        for item in items if isinstance(items, list) else []:
            try:
                event = RunEventModel(**item)
            except Exception:  # noqa: BLE001
                continue
            asyncio.ensure_future(bus.publish(event, relayed=True)).add_done_callback(_quiet)

    def _on_turn(self, parts: list[str]) -> None:
        if parts:
            from app.chat import log as turn_log

            turn_log.waiters.notify(parts[0])

    def _on_beat(self, parts: list[str]) -> None:
        if len(parts) < 2 or parts[0] not in self._turns:
            return
        try:
            op = json.loads(parts[1])
        except ValueError:
            return
        if isinstance(op, dict):
            from app.chat import log as turn_log

            turn_log.remote_beat(parts[0], op)

    async def _resync(self) -> None:
        for run_id in list(self._follow):
            await self.pump(run_id)
        from app.chat import log as turn_log

        for turn_id in list(self._turns):
            turn_log.waiters.notify(turn_id)

    async def _loop(self) -> None:
        """跟随方：隔 WATCH_S 秒重发「我在看」，隔 POLL_S 秒回库里看一次跟随的运行。"""
        last_watch = 0.0
        while True:
            await asyncio.sleep(POLL_S)
            now = time.monotonic()
            if now - last_watch >= WATCH_S:
                last_watch = now
                for run_id in list(self._follow):
                    signals.publish("w", f"r:{run_id}")
                for turn_id in list(self._turns):
                    signals.publish("w", f"t:{turn_id}")
            for run_id in list(self._follow):
                try:
                    await self.pump(run_id)
                except Exception:  # noqa: BLE001
                    logger.debug("转发运行 %s 的事件出错", run_id, exc_info=True)

    # ---------------- 执行的一方 ----------------

    def _on_watch(self, parts: list[str]) -> None:
        if not parts or ":" not in parts[0]:
            return
        kind, target = parts[0].split(":", 1)
        until = time.monotonic() + WATCH_TTL_S
        if kind == "r":
            self._watched_runs[target] = until
        elif kind == "t":
            self._watched_turns[target] = until

    def _watched(self, book: dict[str, float], key: str) -> bool:
        until = book.get(key)
        if until is None:
            return False
        if until < time.monotonic():
            book.pop(key, None)
            return False
        return True

    def turn_watched(self, turn_id: str) -> bool:
        return signals.active and self._watched(self._watched_turns, turn_id)

    def _tap(self, event: RunEventModel) -> None:
        if not signals.active:
            return
        if event.type in EPHEMERAL:
            if self._watched(self._watched_runs, event.run_id):
                self._buffer.setdefault(event.run_id, []).append(event.model_dump(mode="json"))
                if self._flusher is None or self._flusher.done():
                    self._flusher = asyncio.ensure_future(self._flush())
            return
        if event.run_id in self._follow:
            self._cursor[event.run_id] = max(self._cursor.get(event.run_id, 0), int(event.seq or 0))
        signals.publish("r", event.run_id, event.seq)

    async def _flush(self) -> None:
        await asyncio.sleep(EPHEMERAL_FLUSH_S)
        buffer, self._buffer = self._buffer, {}
        for run_id, items in buffer.items():
            for chunk in _chunks(items):
                signals.publish("e", run_id, json.dumps(chunk, ensure_ascii=False, default=str), keep=True)

    def beat(self, turn_id: str, op: dict[str, Any]) -> None:
        """问数据助手的一拍保活心跳：有别的进程在看这一轮就转过去。"""
        if self.turn_watched(turn_id):
            text = json.dumps(op, ensure_ascii=False, default=str)
            if len(text.encode()) <= PAYLOAD_MAX:
                signals.publish("b", turn_id, text)


def _chunks(items: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """按载荷上限切开（同一个节点接着的正文增量先合成一条）。太长的单条截断增量。"""
    merged: list[dict[str, Any]] = []
    for item in items:
        prev = merged[-1] if merged else None
        if prev is not None and prev.get("type") == item.get("type") and prev.get("node_id") == item.get("node_id") \
                and set((prev.get("data") or {})) == {"delta"} == set((item.get("data") or {})):
            prev["data"] = {"delta": str(prev["data"]["delta"]) + str(item["data"]["delta"])}
            prev["seq"] = item.get("seq", prev.get("seq"))
            continue
        merged.append(dict(item))
    chunk: list[dict[str, Any]] = []
    size = 2
    for item in merged:
        text = json.dumps(item, ensure_ascii=False, default=str)
        n = len(text.encode())
        if n > PAYLOAD_MAX - 2:
            data = dict(item.get("data") or {})
            if "delta" in data:
                data["delta"] = str(data["delta"])[: PAYLOAD_MAX // 4]
                item = {**item, "data": data}
                n = len(json.dumps(item, ensure_ascii=False, default=str).encode())
            if n > PAYLOAD_MAX - 2:
                continue
        if chunk and size + n + 1 > PAYLOAD_MAX:
            yield chunk
            chunk, size = [], 2
        chunk.append(item)
        size += n + 1
    if chunk:
        yield chunk


def _quiet(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.debug("转发出错", exc_info=task.exception())


relay = Relay()

__all__ = ["EPHEMERAL", "POLL_S", "Relay", "WATCH_S", "relay"]
