"""模型把工具调用当成文字写出来，这一步没有真的调用工具——要认出来，不能当成功交出去。

一次真实运行的最终答案是一整段 `<｜｜DSML｜｜ invoke name="db_schema__…">` 标记：
协作成员没有绑定工具，模型把自己那套工具调用格式当正文输出了四轮，数据库一次
都没查，运行却是 succeeded。换成 agent 节点、单次模型调用也是一样。

处理：先带一句纠正提示重试一次；仍然如此就判失败，说清原因和怎么办，并发一条
code=tool_markup_leak 的警告。协作成员这样时，这一步记为失败，原因交回调度者。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager
from app.engine.toolcalls import leaked_markup

DSML = ('我将真正执行数据库工具，先取表结构。\n\n<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="db_schema__shop">\n'
        '<｜｜DSML｜｜ parameter name="table">orders</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _finish(graph) -> tuple[Run, list[RunEvent]]:
    run = await run_manager.start(graph=graph, input_payload={"question": "orders 有多少"})
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed"):
                break
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return row, events


def _leaks(monkeypatch, *, until: int | None = None) -> list[list]:
    """模型一直（或前 until 次）把工具调用写成文字。返回每次收到的消息。"""
    from app.providers import mock_model

    seen: list[list] = []

    def _decide(self, messages):
        if self.response_format:           # 协作团队的调度者：先派查数员，之后收工
            routed = sum(1 for batch in seen if batch and batch[0] == "route")
            seen.append(["route", "\n".join(str(m.content) for m in messages)])
            plan = ({"assignments": [{"agent": "查数员", "instruction": "查 orders 有多少"}]}
                    if routed == 0 else {"assignments": [], "done": True, "reason": "够了"})
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        seen.append(list(messages))
        calls = sum(1 for batch in seen if batch and batch[0] != "route")
        if until is None or calls <= until:
            return AIMessage(content=DSML)
        return AIMessage(content="orders 一共 12 条。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    DSML,
    "<|tool▁calls▁begin|><|tool▁call▁begin|>function<|tool▁sep|>db_query__shop",
    '<tool_call>\n{"name": "db_query__shop", "arguments": {"sql": "SELECT 1"}}\n</tool_call>',
    '<function_call>{"name": "python_exec"}</function_call>',
    '<invoke name="db_query__shop"><parameter name="sql">SELECT 1</parameter></invoke>',
    '假设调用工具得到：{"tool": "db_query__shop", "sql": "SELECT COUNT(*) FROM orders"}',
])
def test_markup_is_recognised(text):
    assert leaked_markup(text)


@pytest.mark.parametrize("text", [
    "orders 一共 12 条。",
    "查询用的是 `SELECT COUNT(*) FROM orders`，结果 12。",
    '返回的 JSON 是 {"tool": "calculator", "result": 3}',     # 没自称「假设调用」
    "工具调用失败了，原因是连接超时。",
])
def test_ordinary_answers_are_not(text):
    assert not leaked_markup(text)


async def test_an_agent_that_keeps_writing_markup_fails(monkeypatch):
    _leaks(monkeypatch)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("query", "agent", prompt="查 orders 有多少", tools=[], max_steps=4),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.query.text }}"}]),
    ))
    assert row.status == "failed", f"一堆标记被当成了答案：{row.output}"
    assert "工具调用的原始标记" in (row.error or "") and "没有真正调用工具" in (row.error or "")
    assert row.error_node_id == "query"
    assert "tool_markup_leak" in [e.data.get("code") for e in events if e.type == "log"]


async def test_an_agent_gets_one_corrective_retry(monkeypatch):
    seen = _leaks(monkeypatch, until=1)
    row, _ = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("query", "agent", prompt="查 orders 有多少", tools=[], max_steps=4),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.query.text }}"}]),
    ))
    assert row.status == "succeeded", row.error
    assert row.output["r"] == "orders 一共 12 条。"
    retry = seen[1]
    assert isinstance(retry[-1], HumanMessage) and "没有" in str(retry[-1].content)


async def test_a_plain_model_call_that_writes_markup_fails(monkeypatch):
    _leaks(monkeypatch)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("say", "llm", prompt="回答 {{ input.question }}"),
        node("out", "output", fields=[]),
    ))
    assert row.status == "failed"
    assert "工具调用的原始标记" in (row.error or "")
    ends = [e for e in events if e.type == "llm.end" and e.node_id == "say"]
    assert len(ends) == 2, "重试那一次也是一次模型调用，要记账"


async def test_a_team_member_writing_markup_fails_its_step_and_the_coordinator_is_told(monkeypatch):
    seen = _leaks(monkeypatch)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("team", "supervisor", goal="查 orders 有多少", max_rounds=3, agents=[
            {"name": "查数员", "system": "你负责查库", "max_steps": 3}]),
        node("out", "output", fields=[]),
    ))
    step = next(e.data for e in events if e.type == "agent.step.end")
    assert step.get("failed") is True and "原始标记" in step["preview"], step
    assert "tool_markup_leak" in [e.data.get("code") for e in events if e.type == "log"]
    # 调度者下一轮读到的进展里有这条失败的原因
    routes = [batch[1] for batch in seen if batch and batch[0] == "route"]
    assert len(routes) == 2 and "原始标记" in routes[1], routes
    assert "<｜｜DSML" not in json.dumps(row.output, ensure_ascii=False)


async def test_markup_in_the_settle_round_keeps_what_the_agent_found(monkeypatch):
    """步数用完后的收尾轮（不带工具）写出调用标记：不是「没绑工具」，不能判失败、把查到的丢掉。"""
    from langchain_core.messages import ToolMessage

    from app.providers import mock_model

    def _decide(self, messages):
        if self.tools:                      # 前面的步数：真的在调工具
            done = sum(isinstance(m, ToolMessage) for m in messages)
            return AIMessage(content=f"先算第 {done + 1} 步。", tool_calls=[
                {"name": "calculator", "args": {"expression": f"{done} + 1"}, "id": f"c{done}"}])
        return AIMessage(content=DSML)      # 收尾轮：还想接着查

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("query", "agent", prompt="算一下", tools=["calculator"], max_steps=2),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.query.text }}"}]),
    ))
    assert row.status == "succeeded", row.error
    assert [e.type for e in events].count("tool.end") == 2
    assert "<｜｜DSML" not in row.output["r"]
    assert "先算第 2 步。" in row.output["r"] and "用满了 2 步" in row.output["r"], row.output
    leak = [e.data for e in events if e.type == "log" and e.data.get("code") == "tool_markup_leak"]
    assert leak and "收尾轮" in leak[0]["message"], leak


async def test_markup_to_the_very_end_without_any_real_call_still_fails(monkeypatch):
    """纠正之后步数就用完了、收尾轮还是标记：一次真调用都没有，仍是「调不了工具」。"""
    _leaks(monkeypatch)
    row, _ = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("query", "agent", prompt="查 orders 有多少", tools=["calculator"], max_steps=1),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.query.text }}"}]),
    ))
    assert row.status == "failed" and "工具调用的原始标记" in (row.error or ""), row.error


async def test_a_member_whose_settle_round_writes_markup_keeps_its_findings(monkeypatch):
    from langchain_core.messages import ToolMessage

    from app.providers import mock_model

    routed = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            routed["n"] += 1
            plan = ({"assignments": [{"agent": "查数员", "instruction": "算"}]} if routed["n"] == 1
                    else {"assignments": [], "done": True, "reason": "够了"})
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        if self.tools:
            done = sum(isinstance(m, ToolMessage) for m in messages)
            return AIMessage(content=f"第 {done + 1} 步：先算一下。", tool_calls=[
                {"name": "calculator", "args": {"expression": "1 + 1"}, "id": f"c{done}"}])
        return AIMessage(content=DSML)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("team", "supervisor", goal="算一下", max_rounds=3, agents=[
            {"name": "查数员", "system": "你负责算", "tools": ["calculator"], "max_steps": 2}]),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.team.text }}"}]),
    ))
    assert row.status == "succeeded", row.error
    step = next(e.data for e in events if e.type == "agent.step.end")
    assert not step.get("failed"), step
    assert "第 2 步" in row.output["r"] and "<｜｜DSML" not in row.output["r"], row.output
    assert "tool_markup_leak" in [e.data.get("code") for e in events if e.type == "log"]
