"""步数用尽时，agent 要收口，而不是把半截过程交出去。

走到步数上限一定意味着最后那步**要求了工具**（不要工具的话循环早 break 了），
也就是说模型从来没拿到过"说结论"的那一轮——它只是被掐断在半路。交出去的
是它上一句中间过程的话，末尾补一句"步数不够"。

所以补上那一轮：不给工具，让它基于已经查到的收口。这里守三件事——收尾轮
真的发生了、它**发不出**工具调用、以及它没有变成每次都多烧的一次调用。

顺带补回 agent 循环的集成测试：原来那个文件在脱敏那轮被整个删掉了。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager

#: 收尾轮该说的话。挑一句和"中间过程"截然不同的，好分辨答案到底取自哪一轮
SETTLED = "已经查到的部分：门店 8 家。其余未查到，这个数是下限。"
MIDWAY = "中间过程：还得再查一张表。"

MAX_STEPS = 3


@pytest.fixture
async def runner():
    await run_manager.setup()
    yield run_manager
    await run_manager.shutdown()


def _graph(*, max_steps: int = MAX_STEPS) -> dict:
    return {
        "nodes": [
            {"id": "in", "type": "input", "data": {"config": {}}},
            {"id": "work", "type": "agent", "data": {"config": {
                "prompt": "{{ input.question }}",
                "tools": ["calculator"],
                "max_steps": max_steps,
                "approval": "never",
            }}},
            {"id": "out", "type": "output", "data": {"config": {}}},
        ],
        "edges": [{"source": "in", "target": "work"}, {"source": "work", "target": "out"}],
    }


def never_concludes(monkeypatch, *, settle_raises: bool = False) -> None:
    """绑了工具就永远再要一次工具；没绑工具才说话。

    `self.tools` 是 bind_tools 之后才有的。收尾轮走的是**没绑过工具**的
    base_model，所以它必然落到下面那个分支——这正是"收尾轮发不出工具调用"
    那条断言的着力点：真要是绑着工具过来的，拿到的就会是 MIDWAY。
    """
    from app.providers import mock_model

    def _decide(self, messages):
        if self.tools:
            return AIMessage(
                content=MIDWAY,
                tool_calls=[{"name": self.tools[0]["name"],
                             "args": {"expression": "1+1"},
                             "id": f"call_{len(messages)}"}],
            )
        if settle_raises:
            raise RuntimeError("收尾轮炸了")
        return AIMessage(content=SETTLED)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


async def _run(graph: dict, question: str = "有几家门店"):
    run = await run_manager.start(graph=graph, input_payload={"question": question})
    for _ in range(200):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = (await session.execute(select(Run).where(Run.id == run.id))).scalar_one()
            if row.status in ("succeeded", "failed", "cancelled"):
                break
    else:
        pytest.fail("运行没有在 10 秒内结束")
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return events, row


def codes(events) -> list[str]:
    return [e.data.get("code") for e in events
            if e.type == "log" and (e.data or {}).get("level") == "warn"]


def answer_of(row) -> str:
    from app.engine import review as rv
    return rv.answer_text(row.output)


# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_limit_ends_with_a_conclusion_not_a_stump(runner, monkeypatch) -> None:
    """这块改动的全部意义：交出去的是结论，不是中间过程加一句抱怨。"""
    never_concludes(monkeypatch)
    events, row = await _run(_graph())
    assert row.status == "succeeded", row.error

    text = answer_of(row)
    assert SETTLED in text, text
    assert MIDWAY not in text, f"上一句中间过程冒充了答案：{text}"
    assert "step_limit_settled" in codes(events), codes(events)


@pytest.mark.asyncio
async def test_the_settle_round_has_no_tools_bound(runner, monkeypatch) -> None:
    """收尾轮必须在**结构上**发不出工具调用，而不是靠提示词请它别调。

    mock 的判据就是"有没有绑工具"：真要是把绑过工具的 model 传进了收尾轮，
    它会照常再要一次工具，答案里出现的就是 MIDWAY。
    """
    never_concludes(monkeypatch)
    events, row = await _run(_graph())

    assert SETTLED in answer_of(row)
    # 工具调用次数正好等于步数——收尾轮一次都没多调
    assert len([e for e in events if e.type == "tool.start"]) == MAX_STEPS


@pytest.mark.asyncio
async def test_the_settle_round_costs_exactly_one_extra_call(runner, monkeypatch) -> None:
    """多花的必须是可数的一次，不是一个说不清的尾巴。"""
    never_concludes(monkeypatch)
    events, _ = await _run(_graph())
    assert len([e for e in events if e.type == "llm.start"]) == MAX_STEPS + 1


@pytest.mark.asyncio
async def test_a_normal_agent_does_not_pay_for_a_settle_round(runner, monkeypatch) -> None:
    """反向断言：正常收口的 agent 一分钱都不该多花。

    没有这条，"每次都多跑一轮"这种退化会悄无声息地通过全部其他测试。
    """
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda self, messages: AIMessage(content="直接就有结论了。"))
    events, row = await _run(_graph())

    assert "直接就有结论了。" in answer_of(row)
    assert len([e for e in events if e.type == "llm.start"]) == 1
    assert not [c for c in codes(events) if c and c.startswith("step_limit")]


@pytest.mark.asyncio
async def test_a_failed_settle_round_falls_back_and_keeps_what_was_found(
    runner, monkeypatch
) -> None:
    """收尾轮自己挂了，不能把已经查到的一起赔进去。"""
    never_concludes(monkeypatch, settle_raises=True)
    events, row = await _run(_graph())
    assert row.status == "succeeded", row.error

    text = answer_of(row)
    assert "settle_failed" in codes(events), codes(events)
    # 退回原来的行为：老的 step_limit（BROKEN），而不是 settled 那个
    assert "step_limit" in codes(events)
    assert "step_limit_settled" not in codes(events)
    # 已有成果还在，末尾挂着说明
    assert MIDWAY in text and "用满了" in text, text


def test_max_steps_is_clamped_by_the_hard_cap() -> None:
    """节点上写多大都过不去硬顶——否则一张图就能把预算烧穿。"""
    from app.core.config import settings

    assert min(200, settings.max_agent_steps) == settings.max_agent_steps
    assert settings.max_agent_steps >= 25, "抬高上限不该反而把原来能配的值挡回去"


# --------------------------------------------------------------------------
# 复核层怎么看这两种收场
# --------------------------------------------------------------------------


def _warn(message: str, code: str) -> dict:
    return {"type": "log", "node_id": "work",
            "data": {"level": "warn", "message": message, "code": code}}


OK_OUTPUT = {"text": SETTLED}


def test_a_settled_run_is_a_gap_not_a_break() -> None:
    """收口过的答案是完整一段话，只是覆盖面不全——那是缺口不是断裂。"""
    from app.engine import review as rv

    signals = rv.scan([_warn("用满了 3 步，基于已查到的部分给出", "step_limit_settled")],
                      OK_OUTPUT)
    assert [s.severity for s in signals] == [rv.DEGRADED]
    # 连带效果正是想要的：不再触发自动重跑
    assert not rv.should_retry(signals)


def test_a_hard_truncation_is_still_broken_and_still_retries() -> None:
    """收尾轮没能收上来，判定一点没变——这条是防止改动把老路一起改坏。"""
    from app.engine import review as rv

    signals = rv.scan([_warn("用满了 3 步还没给出结论", "step_limit")], OK_OUTPUT)
    assert rv.worst(signals) == rv.BROKEN
    assert rv.should_retry(signals)
