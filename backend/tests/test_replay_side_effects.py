"""审批恢复之后，节点从头重放；已经执行过的工具不能再执行一次，也不能在轨迹里再"执行"一次。

LangGraph 恢复一个停在 interrupt 上的节点，是把整个节点函数从头再跑一遍，
interrupt() 按出现的顺序返回已经给过的答复。节点里一连串的「问人 → 调工具」
于是每恢复一次就重放一遍前面所有的步骤：

- 工具真的再执行一次，副作用就翻倍（写库、发请求、沙箱里改文件）；
- 就算结果取自缓存，重放时照样发一遍 human.requested / human.resolved /
  tool.start / tool.end，时间线和审计轨迹里同一次调用出现两回——运行 fac480ff
  的轨迹里 python_exec 就这么「执行了两次」，排查的人只能照字面理解。

这里用一个带副作用计数的工具，在同一个节点里连续审批两次、恢复两次。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager
from app.tools.registry import register

#: 真实执行次数。注册表是进程级的，所以工具只注册一次，计数按测试清零
CALLS: list[dict] = []


class _TallyArgs(BaseModel):
    label: str


@register("tally_side_effect", "测试用：每执行一次记一笔", "test", _TallyArgs,
          dangerous=True, needs_context=False)
async def _tally(label: str) -> str:
    CALLS.append({"label": label})
    return json.dumps({"label": label, "n": len(CALLS)})


@pytest.fixture(autouse=True)
async def engine_up():
    CALLS.clear()
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"等超时了，run {run_id} 没有进入 {statuses}")


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars())


def _script_two_calls(monkeypatch) -> None:
    """第一轮调一次、看到结果再调一次、看到两次结果就收口。"""
    from app.providers import mock_model

    def _decide(self, messages):
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if self.tools and done < 2:
            return AIMessage(content="", tool_calls=[
                {"name": "tally_side_effect", "args": {"label": f"第{done + 1}次"},
                 "id": f"call_{done + 1}"}])
        return AIMessage(content="两次都记好了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


async def _approve_until_done(run_id: str, times: int) -> Run:
    for _ in range(times):
        run = await _wait(run_id, ("interrupted", "failed", "succeeded"))
        assert run.status == "interrupted", f"还没审批就结束了：{run.status} {run.error}"
        await run_manager.resume(run_id, {"approved": True})
    return await _wait(run_id, ("failed", "succeeded"))


async def test_an_agent_that_asks_twice_runs_each_tool_exactly_once(monkeypatch):
    _script_two_calls(monkeypatch)
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="记两笔", tools=["tally_side_effect"],
             approval="always", max_steps=5),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.bot.text }}"}]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "记两笔"})
    final = await _approve_until_done(run.id, 2)

    assert final.status == "succeeded", final.error
    assert [c["label"] for c in CALLS] == ["第1次", "第2次"], f"副作用翻倍了：{CALLS}"

    events = await _events(run.id)
    starts = [e.data["call_id"] for e in events if e.type == "tool.start"]
    ends = [e.data["call_id"] for e in events if e.type == "tool.end"]
    # 每次调用在轨迹里只出现一回：重放取的是缓存，不是又执行了一次
    assert starts == ["call_1", "call_2"], starts
    assert ends == ["call_1", "call_2"], ends
    asked = [e.data.get("tool") for e in events if e.type == "human.requested"]
    answered = [e.data.get("approved") for e in events if e.type == "human.resolved"]
    assert asked == ["tally_side_effect", "tally_side_effect"], asked
    assert answered == [True, True], answered


async def test_parallel_tool_calls_after_one_approval_are_not_replayed(monkeypatch):
    """一轮里两个调用、各要审批一次：第二次恢复时第一个已经跑过的不能再跑。"""
    from app.providers import mock_model

    def _decide(self, messages):
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="", tool_calls=[
                {"name": "tally_side_effect", "args": {"label": "甲"}, "id": "call_a"},
                {"name": "tally_side_effect", "args": {"label": "乙"}, "id": "call_b"},
            ])
        return AIMessage(content="都记了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="记两笔", tools=["tally_side_effect"],
             approval="always", parallel_tools=True, max_steps=3),
        node("out", "output", fields=[]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "记"})
    final = await _approve_until_done(run.id, 2)

    assert final.status == "succeeded", final.error
    assert sorted(c["label"] for c in CALLS) == ["乙", "甲"], CALLS
    starts = [e.data["call_id"] for e in await _events(run.id) if e.type == "tool.start"]
    assert starts == ["call_a", "call_b"], starts


async def test_a_tool_node_asks_once_in_the_trail():
    """工具节点恢复时整个节点重放：请求审批那条以前会再发一遍，时间线上像是又问了一次。"""
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("t", "tool", tool="tally_side_effect", args={"label": "一次"}, approval="always"),
        node("out", "output", fields=[]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "x"})
    final = await _approve_until_done(run.id, 1)
    assert final.status == "succeeded", final.error
    assert len(CALLS) == 1

    kinds = [e.type for e in await _events(run.id) if e.node_id == "t"]
    assert kinds.count("human.requested") == 1, kinds
    assert kinds.count("human.resolved") == 1, kinds
    assert kinds.count("tool.start") == 1, kinds


async def test_a_human_node_is_requested_once():
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("h", "human", mode="approve", title="看一眼"),
        node("out", "output", fields=[]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "x"})
    final = await _approve_until_done(run.id, 1)
    assert final.status == "succeeded", final.error
    kinds = [e.type for e in await _events(run.id) if e.node_id == "h"]
    assert kinds.count("human.requested") == 1, kinds
    assert kinds.count("human.resolved") == 1, kinds


async def test_continuing_after_a_failure_does_not_repeat_the_tool_in_the_trail(monkeypatch):
    """模型调用在工具之后挂了，接着跑：工具结果取自断点，轨迹里也不能再出现一次执行。"""
    from app.providers import mock_model

    state = {"fail": True}

    def _decide(self, messages):
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="", tool_calls=[
                {"name": "tally_side_effect", "args": {"label": "只一次"}, "id": "call_once"}])
        if state["fail"]:
            raise RuntimeError("供应商那边断了")
        return AIMessage(content="好了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="记一笔", tools=["tally_side_effect"], approval="never",
             max_steps=3),
        node("out", "output", fields=[]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "x"})
    first = await _wait(run.id, ("failed", "succeeded"))
    assert first.status == "failed"

    state["fail"] = False
    await run_manager.continue_failed(run.id)
    final = await _wait(run.id, ("failed", "succeeded"))
    assert final.status == "succeeded", final.error
    assert len(CALLS) == 1
    starts = [e.data["call_id"] for e in await _events(run.id) if e.type == "tool.start"]
    assert starts == ["call_once"], starts


# --------------------------------------------------------------------------
# 升级前停下的断点
#
# LangGraph 认 task 缓存靠「函数名 + 节点里第几次调用」。这一版多了 once / ask 的记录
# task、tool_step 也换了位置：旧版停下的断点照新代码重放，节点里已经执行过的工具对不上
# 旧位置，会再执行一次。checkpoint 的 metadata 里记着重放协议版本，对不上又有这种风险
# 时拒绝恢复。这里把「旧版」模拟成协议版本低一号写下的断点。
# --------------------------------------------------------------------------


def _older_protocol(monkeypatch) -> None:
    from app.engine import runner

    monkeypatch.setattr(runner, "PROTOCOL", runner.PROTOCOL - 1)


def _upgrade(monkeypatch) -> None:
    from app.engine import replay, runner

    monkeypatch.setattr(runner, "PROTOCOL", replay.PROTOCOL)


async def test_an_old_pause_after_tools_ran_is_refused(monkeypatch):
    _script_two_calls(monkeypatch)
    _older_protocol(monkeypatch)
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="记两笔", tools=["tally_side_effect"],
             approval="always", max_steps=5),
        node("out", "output", fields=[]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "记两笔"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    await run_manager.resume(run.id, {"approved": True})        # 第一笔：旧版里批的、执行的
    await asyncio.sleep(0.1)
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    assert len(CALLS) == 1

    _upgrade(monkeypatch)
    with pytest.raises(ValueError, match="重复执行"):
        await run_manager.resume(run.id, {"approved": True})
    assert len(CALLS) == 1
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
    assert row.status == "interrupted", "拒绝恢复不能动运行的状态：它还可以被放弃"
    assert await run_manager.cancel(run.id) == "cancelled"


async def test_an_old_pause_before_anything_ran_still_resumes(monkeypatch):
    """停在第一次审批上、之前什么都没做：重放只会多发一条 human.requested，照常放行。"""
    _script_two_calls(monkeypatch)
    _older_protocol(monkeypatch)
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="记两笔", tools=["tally_side_effect"],
             approval="always", max_steps=5),
        node("out", "output", fields=[]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "记两笔"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)

    _upgrade(monkeypatch)
    final = await _approve_until_done(run.id, 2)
    assert final.status == "succeeded", final.error
    assert [c["label"] for c in CALLS] == ["第1次", "第2次"]


async def test_continuing_an_old_failure_after_tools_ran_is_refused(monkeypatch):
    from app.providers import mock_model

    def _decide(self, messages):
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="", tool_calls=[
                {"name": "tally_side_effect", "args": {"label": "只一次"}, "id": "call_once"}])
        raise RuntimeError("供应商那边断了")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    _older_protocol(monkeypatch)
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="记一笔", tools=["tally_side_effect"], approval="never",
             max_steps=3),
        node("out", "output", fields=[]),
    )
    run = await run_manager.start(graph=graph, input_payload={"question": "x"})
    assert (await _wait(run.id, ("failed", "succeeded"))).status == "failed"
    await run_manager.wait_idle(run.id)

    _upgrade(monkeypatch)
    with pytest.raises(ValueError, match="重复执行"):
        await run_manager.continue_failed(run.id)
    assert len(CALLS) == 1
