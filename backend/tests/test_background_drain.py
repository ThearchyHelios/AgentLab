"""core/background.drain：等后台任务做完，碰上别的事件循环上做完、还没摘掉的任务不空转。

以前 notify.service.drain、eval.cleanup.drain 写的是 `while _background: await gather(*_background)`：前一个测试的事件循环上
刚做完的推送，done 回调（从集合里摘掉）排在那个已经不转的循环上、永远不跑；gather 拿到的全是做完了的、立刻返回，await 不让出
事件循环，于是空转、整个测试进程卡死（PostgreSQL 全量里 test_personal_apps 收尾时出现过两次）。空转时事件循环也转不动，
asyncio.wait_for 的超时救不了，所以这里在另一个线程里跑、按线程等。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from app.core import background
from app.eval import cleanup
from app.notify import service


def _finished_on_a_dead_loop(tasks: set[asyncio.Future[Any]]) -> asyncio.Future[Any]:
    """在另一个事件循环上做完、但 discard 回调没跑过的任务：照 spawn 的写法登记，结果设上（回调排进那个循环），循环不转就关。"""
    other = asyncio.new_event_loop()
    fut = other.create_future()
    tasks.add(fut)
    fut.add_done_callback(tasks.discard)
    fut.set_result(None)
    other.close()
    return fut


def _returns_within(seconds: float, make: Any) -> bool:
    done = threading.Event()

    def go() -> None:
        asyncio.run(make())
        done.set()

    threading.Thread(target=go, daemon=True).start()
    return done.wait(seconds)


@pytest.mark.parametrize("module", [service, cleanup], ids=["notify", "eval-cleanup"])
def test_drain_does_not_spin_on_tasks_finished_on_another_loop(module):
    stale = _finished_on_a_dead_loop(module._background)
    try:
        assert _returns_within(5, module.drain), "drain 卡住了（空转）"
        assert stale not in module._background, "做完了的摘掉"
    finally:
        module._background.discard(stale)


def test_drain_waits_for_this_loops_tasks_and_skips_other_loops_pending_ones():
    tasks: set[asyncio.Future[Any]] = set()
    other = asyncio.new_event_loop()
    pending_elsewhere = other.create_future()      # 别的循环上没做完的：等不到，不等
    tasks.add(pending_elsewhere)
    seen: list[str] = []

    async def scenario() -> None:
        async def slow() -> None:
            await asyncio.sleep(0.05)
            seen.append("slow")

        task = asyncio.get_running_loop().create_task(slow())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        await background.drain(tasks)
        seen.append("drained")

    try:
        assert _returns_within(5, scenario)
        assert seen == ["slow", "drained"], "本循环上的等它做完"
        assert pending_elsewhere in tasks, "循环还开着的别人的任务留着"
        other.close()
        assert _returns_within(5, lambda: background.drain(tasks))
        assert tasks == set(), "循环关了的一并摘掉"
    finally:
        if not other.is_closed():
            other.close()
