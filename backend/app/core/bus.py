from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import AsyncIterator

from app.core.events import RunEventModel

# 单个订阅者的积压上限。慢消费者（比如卡住的浏览器标签）不应该拖垮整个 run，
# 所以队列满了就丢最旧的事件并打标记，而不是阻塞发布方。
_QUEUE_MAXSIZE = 2048


class _Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[RunEventModel | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self.dropped = 0


class EventBus:
    """按 run_id 分频道的内存 pub/sub。

    只负责实时分发；历史事件由 DB 承担，订阅者接入时先读库补齐再接实时流，
    这样刷新页面或中途接入都能看到完整时间线。
    """

    def __init__(self) -> None:
        self._subs: dict[str, set[_Subscriber]] = defaultdict(set)
        self._seq: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    def next_seq(self, run_id: str) -> int:
        self._seq[run_id] += 1
        return self._seq[run_id]

    def set_seq(self, run_id: str, value: int) -> None:
        """恢复一个已存在的 run 时，把序号接上，保证前端排序不乱。"""
        self._seq[run_id] = max(self._seq[run_id], value)

    async def publish(self, event: RunEventModel) -> None:
        for sub in list(self._subs.get(event.run_id, ())):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                sub.dropped += 1
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(event)

    async def close(self, run_id: str) -> None:
        """给所有订阅者发结束哨兵，让 WebSocket 干净地收尾。"""
        for sub in list(self._subs.get(run_id, ())):
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    async def subscribe(self, run_id: str) -> AsyncIterator[RunEventModel]:
        sub = _Subscriber()
        async with self._lock:
            self._subs[run_id].add(sub)
        try:
            while True:
                item = await sub.queue.get()
                if item is None:
                    break
                yield item
        finally:
            async with self._lock:
                self._subs[run_id].discard(sub)
                if not self._subs[run_id]:
                    self._subs.pop(run_id, None)

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))


bus = EventBus()
