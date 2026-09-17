"""事件总线的订阅时序。

这里守的是一个只在**并发**下才出现的缺陷：先读历史、再订阅，两步之间发出的
事件既不在历史里、也不在实时流里，永远丢了。串行地测"发一条收一条"永远
测不出来——必须在"读历史"的那一刻往总线里灌事件。

真实表现：一次跑图开头两三条 node.started/node.finished 消失，界面上某个节点
永远在转圈，它的子步骤跑到了顶层。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.bus import EventBus
from app.core.events import RunEventModel


def _event(run_id: str, seq: int, type_: str = "node.started") -> RunEventModel:
    return RunEventModel(run_id=run_id, seq=seq, type=type_, node_id=None, data={})


@pytest.mark.asyncio
async def test_attach_before_history_loses_nothing() -> None:
    bus = EventBus()
    run_id = "r1"
    got: list[int] = []

    with bus.attach(run_id) as sub:
        # 占位之后、开始消费之前发出的事件要留在队列里等着，不能丢
        await bus.publish(_event(run_id, 1))
        await bus.publish(_event(run_id, 2))

        async def drain() -> None:
            async for event in sub.stream():
                got.append(event.seq)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0)
        await bus.publish(_event(run_id, 3))
        await bus.close(run_id)
        await asyncio.wait_for(task, timeout=2)

    assert got == [1, 2, 3]


@pytest.mark.asyncio
async def test_after_filter_dedups_overlap() -> None:
    """占位到读完历史之间的事件两边都有一份，按 seq 滤掉重复的那份。"""
    bus = EventBus()
    run_id = "r2"
    got: list[int] = []

    with bus.attach(run_id) as sub:
        for seq in (1, 2, 3):
            await bus.publish(_event(run_id, seq))

        # 假设补历史时读到了 seq<=2，那实时流里只该再吐 3
        async def drain() -> None:
            async for event in sub.stream(after=2):
                got.append(event.seq)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0)
        await bus.close(run_id)
        await asyncio.wait_for(task, timeout=2)

    assert got == [3]


@pytest.mark.asyncio
async def test_detach_removes_subscriber() -> None:
    bus = EventBus()
    run_id = "r3"
    sub = bus.attach(run_id)
    assert bus.subscriber_count(run_id) == 1
    sub.close()
    assert bus.subscriber_count(run_id) == 0


@pytest.mark.asyncio
async def test_subscribe_registers_late() -> None:
    """刻画 subscribe() 的行为，说明 attach 为什么必须存在。

    subscribe() 是异步生成器：拿到它并不等于订阅上了，要到第一次迭代才真正
    注册。调用方"先拿生成器、再读历史、最后开始迭代"是最自然的写法，而那
    恰好让整个读历史的窗口都处于无人接收的状态。
    """
    bus = EventBus()
    run_id = "r5"
    gen = bus.subscribe(run_id)
    assert bus.subscriber_count(run_id) == 0, "拿到生成器还不算订阅"

    # 这条发出去时没人收——旧代码丢事件就是丢在这个窗口里
    await bus.publish(_event(run_id, 1))

    got: list[int] = []

    async def drain() -> None:
        async for event in gen:
            got.append(event.seq)

    task = asyncio.create_task(drain())
    await asyncio.sleep(0.01)
    assert bus.subscriber_count(run_id) == 1, "迭代之后才注册上"
    await bus.publish(_event(run_id, 2))
    await bus.close(run_id)
    await asyncio.wait_for(task, timeout=2)

    assert got == [2], "seq=1 确实丢了；attach 就是为了消掉这个窗口"


@pytest.mark.asyncio
async def test_concurrent_publish_during_history_read() -> None:
    """把真实时序演一遍：占位 → 慢慢读历史（期间图还在跑）→ 开始消费。"""
    bus = EventBus()
    run_id = "r4"
    history: list[int] = []
    live: list[int] = []

    with bus.attach(run_id) as sub:
        async def keep_running() -> None:
            # 图不会等前端读完历史
            for seq in range(1, 6):
                await bus.publish(_event(run_id, seq))
                await asyncio.sleep(0.005)
            await bus.close(run_id)

        runner = asyncio.create_task(keep_running())

        # "读历史"——只读到前两条，剩下的还没落库
        await asyncio.sleep(0.012)
        history.extend([1, 2])

        async def drain() -> None:
            async for event in sub.stream(after=max(history)):
                live.append(event.seq)

        await asyncio.wait_for(asyncio.gather(runner, drain()), timeout=3)

    # 3/4/5 一条都不能少——这正是修复前丢掉的那些
    assert live == [3, 4, 5], f"历史={history} 实时={live}"
