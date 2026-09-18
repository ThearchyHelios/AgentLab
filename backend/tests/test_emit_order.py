"""事件的落库与广播顺序。

订阅者的完整性靠"先订阅 → 读历史 → 接实时"三步拼起来，它需要这条保证：

    提交早于我这次 SELECT 的，在历史里；
    提交晚于我这次 SELECT 的，广播也晚于我订阅，在实时流里。

后半句只有在"提交先于广播"时才成立。反过来（先广播后落库）会漏掉这样一条
事件：在订阅建立之前广播、却在读历史之后才提交——两边都拿不到。

这不是理论。实测跑一次 25 毫秒的图，六条事件里的 run.started 有一半几率
就这么没了，界面上表现为某个节点永远在转圈、它的子步骤跑到了顶层。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import runner as runner_mod
from app.engine.runner import run_manager


@pytest.mark.asyncio
async def test_event_is_in_db_before_it_is_broadcast(monkeypatch) -> None:
    """广播的那一刻，这条事件必须已经能从库里查到。"""
    run_id = f"emit-order-1-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add(Run(id=run_id, status="running", graph={}, input={}))
        await session.commit()

    visible_at_publish: list[bool] = []
    original = runner_mod.bus.publish

    async def spy(event):
        # 用一个独立 session 查，模拟另一个连接（WebSocket 的历史读就是这样）
        async with SessionLocal() as s:
            row = (
                await s.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == event.run_id, RunEvent.seq == event.seq
                    )
                )
            ).scalar_one_or_none()
        visible_at_publish.append(row is not None)
        await original(event)

    monkeypatch.setattr(runner_mod.bus, "publish", spy)

    await run_manager._emit(run_id, EventType.RUN_STARTED, data={"nodes": 3})
    await run_manager._emit(run_id, EventType.NODE_STARTED, node_id="a", data={})

    assert visible_at_publish == [True, True], (
        "广播时还查不到这条事件——说明是先广播后落库，"
        "订阅者会漏掉在它订阅之前广播、在它读历史之后提交的那些"
    )


@pytest.mark.asyncio
async def test_ephemeral_events_still_broadcast(monkeypatch) -> None:
    """不落库的流式增量照样要广播出去，不能因为跳过落库就一起跳过广播。"""
    run_id = f"emit-order-2-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add(Run(id=run_id, status="running", graph={}, input={}))
        await session.commit()

    published: list[str] = []
    bus_mod = runner_mod.bus
    original = bus_mod.publish

    async def spy(event):
        published.append(str(event.type))
        await original(event)

    monkeypatch.setattr(bus_mod, "publish", spy)
    await run_manager._emit(run_id, EventType.LLM_TOKEN, node_id="a", data={"delta": "x"})

    assert published == ["llm.token"]
    async with SessionLocal() as s:
        rows = (
            await s.execute(select(RunEvent).where(RunEvent.run_id == run_id))
        ).scalars().all()
    assert rows == [], "llm.token 是流式增量，不该落库"


@pytest.mark.asyncio
async def test_seq_is_monotonic_across_emits() -> None:
    """序号按发出顺序递增——前端排序和去重都建立在这上面。"""
    run_id = f"emit-order-3-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add(Run(id=run_id, status="running", graph={}, input={}))
        await session.commit()

    for i in range(5):
        await run_manager._emit(run_id, EventType.LOG, data={"message": str(i)})

    async with SessionLocal() as s:
        seqs = [
            e.seq
            for e in (
                await s.execute(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            ).scalars()
        ]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
