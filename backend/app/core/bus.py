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

    def attach(self, run_id: str) -> "Subscription":
        """立刻占一个位，之后再慢慢消费。

        必须能在读历史**之前**占位。`subscribe()` 做不到这件事：它是异步
        生成器，订阅者要等到第一次迭代才真正注册——而调用方通常先读完历史
        才开始迭代。这中间发出的事件既不在历史里、也不在实时流里，就这么
        没了。一次跑图从发起到订阅之间正好有几毫秒，丢掉的往往是开头那
        两三条 node.started/node.finished，表现为"某个节点永远在转圈、
        它的子步骤跑到了顶层"。
        """
        sub = _Subscriber()
        self._subs[run_id].add(sub)
        return Subscription(self, run_id, sub)

    def _detach(self, run_id: str, sub: _Subscriber) -> None:
        self._subs.get(run_id, set()).discard(sub)
        if run_id in self._subs and not self._subs[run_id]:
            self._subs.pop(run_id, None)

    async def subscribe(self, run_id: str) -> AsyncIterator[RunEventModel]:
        """便捷入口：占位即消费。只适合不需要补历史的场景。"""
        with self.attach(run_id) as sub:
            async for event in sub.stream():
                yield event

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))


class Subscription:
    """一个已经在收事件的订阅位。"""

    def __init__(self, bus: EventBus, run_id: str, sub: _Subscriber) -> None:
        self._bus = bus
        self._run_id = run_id
        self._sub = sub

    @property
    def dropped(self) -> int:
        """因为消费太慢被丢掉的条数。不是 0 就说明这条时间线有洞。"""
        return self._sub.dropped

    async def stream(self, after: int = 0) -> AsyncIterator[RunEventModel]:
        """吐出实时事件。

        after 用来去重：占位到读完历史之间发出的事件，两边都有一份。
        按 seq 过滤掉已经补过的，前端就不会看到同一步出现两次。
        """
        while True:
            item = await self._sub.queue.get()
            if item is None:
                break
            if after and (item.seq or 0) <= after:
                continue
            yield item

    def close(self) -> None:
        self._bus._detach(self._run_id, self._sub)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


bus = EventBus()
