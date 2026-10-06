"""这一段执行的截止时刻（engine/deadline.py）：runner 进入时长上限时放进 ContextVar、退出时清掉，核查节点开始每一项之前
问 allot() 够不够、时长收紧到多少。

- 没有截止时刻（不经 runner 执行）：原样放行，时长上限还是传进来的那个值
- 有截止时刻：先给下游留出余量（60 秒或这一段总时长的 10%，取大的）、再扣收尾宽限；比配置的时长短就收紧，收紧之后
  不到 min(配置的时长, 收尾宽限) 就不再开始；配置的是不限时也按剩余时间收紧
- runner 每一段执行都放一份：节点、节点里的 @task 都看得到，截止时刻就是 asyncio.timeout 的那一个；审批恢复后的那一段
  是新的截止时刻；运行结束后清掉
"""
from __future__ import annotations

import asyncio

import pytest
from langgraph.func import task
from sqlalchemy import select

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Approval, Run
from app.engine import compiler, deadline
from app.engine.runner import run_manager
from app.engine.schema import NodeType


class Clock:
    """假的事件循环钟：allot() 看到的「现在」由测试定。"""

    def __init__(self, monkeypatch, now: float = 1000.0) -> None:
        self.now = now
        monkeypatch.setattr(deadline, "_now", lambda: self.now)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    return Clock(monkeypatch)


async def test_without_a_deadline_everything_passes_unchanged(clock):
    assert deadline.current() is None
    for timeout in (90, 0.4, None):
        answer = deadline.allot(timeout, grace=30.0)
        assert answer == deadline.Allot(True, timeout), "不经 runner 执行：照旧放行，时长不动"
        assert answer.timeout_s is timeout, "传进来的就是那个值（int 不变成 float），和没有这道检查时逐字一样"


async def test_plenty_of_time_keeps_the_configured_limit(clock):
    async with deadline.segment(clock.now + 600, 600):
        answer = deadline.allot(90, grace=30.0)
    assert answer.start and answer.timeout_s == 90 and isinstance(answer.timeout_s, int) and not answer.tightened
    assert answer.seconds == 600


@pytest.mark.parametrize("left, start, limit", [
    (180.0, True, 90),          # 60 秒余量 + 90 秒 + 30 秒宽限：刚好够整段
    (150.0, True, 60.0),        # 够收紧到 60 秒
    (120.0, True, 30.0),        # 收紧到 30 秒：和收尾宽限一样长，还开始
    (119.0, False, None),       # 不到 30 秒：连一次模型调用都不够，不再开始
    (10.0, False, None),
    (-5.0, False, None),        # 截止时刻已经过了
])
async def test_the_limit_is_tightened_by_what_is_left(clock, left, start, limit):
    async with deadline.segment(clock.now + left, 600):
        answer = deadline.allot(90, grace=30.0)
    assert answer.start is start
    assert answer.timeout_s == limit
    assert answer.tightened is (start and limit != 90)
    assert answer.seconds == 600, "不再开始、收紧过的说明里要写这一段的总时长"


async def test_a_configured_limit_shorter_than_the_grace_runs_whole_or_not_at_all(clock):
    """配置的时长上限本来就比收尾宽限短（10 秒）：不收紧，够整段才开始。"""
    async with deadline.segment(clock.now + 100, 600):
        assert deadline.allot(10, grace=30.0) == deadline.Allot(True, 10, seconds=600)
    async with deadline.segment(clock.now + 99, 600):
        assert deadline.allot(10, grace=30.0) == deadline.Allot(False, None, seconds=600)


async def test_a_tightened_limit_is_cut_to_a_tenth_of_a_second(clock):
    """收紧到的时长取到 0.1 秒、往下取：时间线上写「时长上限 69.7 秒」，不写 69.7012；往下取不会超出算好的余量。"""
    async with deadline.segment(clock.now + 159.7012, 600):
        assert deadline.allot(90, grace=30.0).timeout_s == 69.7
    async with deadline.segment(clock.now + 120.05, 600):
        assert deadline.allot(90, grace=30.0).timeout_s == 30.0
    async with deadline.segment(clock.now + 119.99, 600):
        assert not deadline.allot(90, grace=30.0).start, "差一点也是不够：往下取之后不到 30 秒"


async def test_an_unlimited_item_is_bounded_by_the_deadline(clock):
    """配置的是不限（None）：有截止时刻时照样按剩余时间收紧，不然它能一直查到运行被中止。"""
    async with deadline.segment(clock.now + 400, 600):
        answer = deadline.allot(None, grace=30.0)
    assert answer.start and answer.tightened and answer.timeout_s == pytest.approx(310.0)
    async with deadline.segment(clock.now + 119, 600):
        assert not deadline.allot(None, grace=30.0).start


@pytest.mark.parametrize("seconds, kept", [(600, 60.0), (300, 60.0), (1800, 180.0), (3600, 360.0)])
def test_the_reserve_is_sixty_seconds_or_a_tenth(seconds, kept):
    assert deadline.reserve(seconds) == kept


async def test_the_reserve_follows_the_segment_length(clock):
    """时长上限调到 1800 秒：给下游留 180 秒，剩 300 秒时这一项最多 90 秒（300 − 180 − 30）。"""
    async with deadline.segment(clock.now + 300, 1800):
        assert deadline.allot(120, grace=30.0).timeout_s == pytest.approx(90.0)


async def test_the_segment_is_cleared_on_the_way_out(clock):
    async with deadline.segment(clock.now + 600, 600):
        assert deadline.current() == deadline.Segment(clock.now + 600, 600.0)
        async with deadline.segment(None, 600):
            assert deadline.current() is None, "没有上限的一段：里面看不到外面的截止时刻"
        assert deadline.current() is not None
    assert deadline.current() is None
    with pytest.raises(RuntimeError):
        async with deadline.segment(clock.now + 600, 600):
            raise RuntimeError("节点出错")
    assert deadline.current() is None, "出错退出也清掉"


# --------------------------------------------------------------------------
# runner 放进来的截止时刻
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("succeeded", "failed", "interrupted")) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return row
    raise AssertionError(f"运行 {run_id} 停在 {row.status}：{row.error}")


@task
async def _peek_in_task() -> object:
    return deadline.current()


async def test_the_runner_hands_each_segment_its_deadline(monkeypatch, engine_up):
    """节点、节点里的 @task 都看得到这一段的截止时刻：就是 asyncio.timeout 的那一个（开始时刻 + max_run_seconds）。
    审批恢复后的那一段是新的截止时刻；运行结束后发起它的地方看不到。"""
    seen: list[tuple[str, object, object, float]] = []

    async def peek(state, ctx):
        seen.append((ctx.node.id, deadline.current(), await _peek_in_task(), asyncio.get_running_loop().time()))
        return {}

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, peek)
    monkeypatch.setattr(settings, "max_run_seconds", 321)
    peeked = {"mode": "template", "template": "好", "assign_to": "x"}
    spec = chain(node("start", "input"), node("before", "transform", **peeked),
                 node("gate", "human", mode="approve", title="继续吗"), node("after", "transform", **peeked),
                 node("out", "output", fields=[{"name": "结果", "value": "好"}]))
    run = await run_manager.start(graph=spec, input_payload={})
    row = await wait(run.id)
    assert row.status == "interrupted", row.error
    async with SessionLocal() as session:
        approval = (await session.execute(select(Approval).where(Approval.run_id == run.id))).scalar_one()
    await run_manager.resume(run.id, {"approved": True}, approval_id=approval.id)
    row = await wait(run.id, ("succeeded", "failed"))
    assert row.status == "succeeded", row.error

    (first_id, first, first_task, first_at), (second_id, second, second_task, second_at) = seen
    assert (first_id, second_id) == ("before", "after")
    for segment, in_task, at in ((first, first_task, first_at), (second, second_task, second_at)):
        assert isinstance(segment, deadline.Segment) and segment.seconds == 321
        assert in_task == segment, "节点里的 @task 看到的是同一个截止时刻"
        assert 300 < segment.deadline - at <= 321, "截止时刻是这一段开始时刻 + max_run_seconds，用事件循环的钟"
    assert second.deadline > first.deadline, "审批恢复后的那一段从恢复时重新计时"
    assert deadline.current() is None, "截止时刻只在 runner 的执行里，发起运行的地方看不到"
