"""审批这道门：只认明确的批准，而且每一个会执行危险操作的地方都得过它。

以前的三个口子（都实测过）：
1. 工具节点和代码节点对回复一律 bool()——回"拒绝"、"no"，工具照样执行；
   {"decision": "approve"} 反被当成拒绝。四个节点四套解析，口径各不相同。
2. 数据源工具查不到 ToolSpec，于是可写库上的 DELETE 在任何审批模式下都不问人
   （is_dangerous_call 写好了，全仓库没人调用）。只是因为写入从不提交，才没出事。
3. 协作团队的成员调工具完全不经审批。

另外两处连带的：多条审批并存时，存进库的回复被截断、解包时只留 value——
改过的长稿被砍掉一截，驳回被当成交稿。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Approval, DataSource, Run, RunEvent
from app.engine.approval import read_decision
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {
        "nodes": list(nodes),
        "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])],
    }


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row
        await asyncio.sleep(0.05)
    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
    raise AssertionError(f"等超时了，run {run_id} 还是 {row.status if row else '不存在'}")


async def _start(graph) -> str:
    run = await run_manager.start(graph=graph, input_payload={"question": "随便"})
    return run.id


async def _events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars())


async def _approvals(run_id: str) -> dict[str, Approval]:
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Approval).where(Approval.run_id == run_id, Approval.status == "pending")
        )).scalars()
        return {a.node_id: a for a in rows}


# --------------------------------------------------------------------------
# 解析口径
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reply", [
    "拒绝", "no", "deny", "不同意", "拒绝，这是生产库", "", None, 1, [],
    {"note": "看不懂这个回复"},
    {"approved": "false"},     # bool("false") 是 True——以前就这么被批了
    {"approved": "no"},
    {"decision": "reject"},
])
def test_anything_short_of_a_clear_yes_is_not_approval(reply):
    assert read_decision(reply).approved is False


@pytest.mark.parametrize("reply", [
    True, "yes", "同意", "approve", " OK ",
    {"approved": True}, {"approved": "true"},
    {"decision": "approve"},   # 以前在工具节点里被当成拒绝
    {"decision": "批准"},
])
def test_clear_approvals(reply):
    assert read_decision(reply).approved is True


def test_rejection_is_only_what_was_said_out_loud():
    """填内容的关卡只在明说"不"时才停：没表态的回复是交稿。"""
    assert read_decision({"approved": False}).rejected
    assert read_decision({"decision": "驳回"}).rejected
    assert not read_decision({"value": "改好的稿子"}).rejected
    assert not read_decision("随便写点什么").rejected


def test_note_and_overrides_come_through():
    d = read_decision({"approved": True, "note": "只这一次", "args": {"sql": "SELECT 1"},
                       "code": "print(1)"})
    assert (d.note, d.args, d.code) == ("只这一次", {"sql": "SELECT 1"}, "print(1)")
    # 一句话的拒绝本身就是理由，agent 转告模型时不能只剩"未说明"
    assert read_decision("拒绝，这是生产库").note == "拒绝，这是生产库"


# --------------------------------------------------------------------------
# 工具节点
# --------------------------------------------------------------------------


def _calc_graph():
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("t", "tool", tool="calculator", args={"expression": "1+1"}, approval="always"),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.t.result }}"}]),
    )


async def test_tool_node_takes_a_refusal_as_a_refusal():
    run_id = await _start(_calc_graph())
    await _wait(run_id, ("interrupted",))
    await run_manager.resume(run_id, "拒绝")
    run = await _wait(run_id, ("failed", "succeeded"))

    assert run.status == "failed" and "拒绝" in (run.error or "")
    assert not [e for e in await _events(run_id) if e.type == "tool.start"], "被拒绝的工具执行了"


async def test_tool_node_takes_decision_approve_as_approval():
    run_id = await _start(_calc_graph())
    await _wait(run_id, ("interrupted",))
    await run_manager.resume(run_id, {"decision": "approve"})
    run = await _wait(run_id, ("failed", "succeeded"))
    assert run.status == "succeeded", run.error
    assert run.output["结果"] == "2"


# --------------------------------------------------------------------------
# 可写数据源：写要先审批，批了就真的写
# --------------------------------------------------------------------------


@pytest.fixture
async def writable(tmp_path):
    path = tmp_path / "rw.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    conn.commit()
    conn.close()
    name = f"rw_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add(DataSource(name=name, kind="sqlite", database=str(path), readonly=False,
                               description="可写测试库", options={}, schema_cache={}, enabled=True))
        await session.commit()

    def count() -> int:
        c = sqlite3.connect(path)
        try:
            return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            c.close()

    return f"db_query__{name}", count


def _query_graph(tool: str, sql: str):
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("q", "tool", tool=tool, args={"sql": sql}),      # approval 缺省 = dangerous
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.q }}"}]),
    )


async def test_write_on_a_writable_source_waits_for_approval_then_really_writes(writable):
    tool, count = writable
    run_id = await _start(_query_graph(tool, "DELETE FROM t WHERE x = 0"))
    run = await _wait(run_id, ("interrupted", "failed", "succeeded"))
    assert run.status == "interrupted", f"可写库上的 DELETE 没有等审批：{run.status} {run.error}"
    assert count() == 5

    await run_manager.resume(run_id, {"approved": True})
    run = await _wait(run_id, ("failed", "succeeded"))
    assert run.status == "succeeded", run.error
    assert count() == 4, "批准了，但写没有落库"


async def test_a_refused_write_leaves_the_data_alone(writable):
    tool, count = writable
    run_id = await _start(_query_graph(tool, "WITH k AS (SELECT 1) DELETE FROM t"))
    await _wait(run_id, ("interrupted",))
    await run_manager.resume(run_id, "拒绝")
    run = await _wait(run_id, ("failed", "succeeded"))
    assert run.status == "failed"
    assert count() == 5


async def test_reads_on_a_writable_source_do_not_ask(writable):
    tool, count = writable
    run_id = await _start(_query_graph(tool, "SELECT COUNT(*) AS n FROM t"))
    run = await _wait(run_id, ("interrupted", "failed", "succeeded"))
    assert run.status == "succeeded", run.error


# --------------------------------------------------------------------------
# agent 节点：同一个工具，危不危险看这一次的 SQL
# --------------------------------------------------------------------------


def _script_agent(monkeypatch, tool: str, sql: str) -> None:
    from app.providers import mock_model

    def _decide(self, messages):
        if self.response_format:
            return AIMessage(content="{}")
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="动手。", tool_calls=[
                {"name": tool, "args": {"sql": sql}, "id": "call_w"}])
        return AIMessage(content="好了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


def _agent_graph(tool: str):
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="处理一下", tools=[tool], max_steps=3),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.bot.text }}"}]),
    )


async def test_agent_asks_before_writing_to_a_writable_source(writable, monkeypatch):
    tool, count = writable
    _script_agent(monkeypatch, tool, "DELETE FROM t WHERE x = 0")
    run_id = await _start(_agent_graph(tool))
    run = await _wait(run_id, ("interrupted", "failed", "succeeded"))
    assert run.status == "interrupted", f"agent 没等审批就写了：{run.status}"

    await run_manager.resume(run_id, "拒绝")
    run = await _wait(run_id, ("failed", "succeeded"))
    assert run.status == "succeeded", run.error      # 拒绝喂回给模型，它换个说法收口
    assert count() == 5


async def test_agent_reads_without_asking(writable, monkeypatch):
    tool, count = writable
    _script_agent(monkeypatch, tool, "SELECT COUNT(*) FROM t")
    run_id = await _start(_agent_graph(tool))
    run = await _wait(run_id, ("interrupted", "failed", "succeeded"))
    assert run.status == "succeeded", run.error


# --------------------------------------------------------------------------
# 协作团队：成员停不下来等人，所以要确认的调用不执行
# --------------------------------------------------------------------------


def _script_team(monkeypatch, *, member_call: dict | None, settle_text: str = "收尾结论") -> None:
    from app.providers import mock_model

    turns = {"route": 0}

    def _decide(self, messages):
        if self.response_format:                   # 调度者：第一轮派 dba，第二轮收工
            turns["route"] += 1
            plan = ({"assignments": [{"agent": "dba", "instruction": "清理测试数据"}]}
                    if turns["route"] == 1 else {"assignments": [], "done": True})
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        if not self.tools:                         # 没绑工具 = 步数用完后的收尾轮
            return AIMessage(content=settle_text)
        if member_call and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="动手。", tool_calls=[{**member_call, "id": "call_m"}])
        return AIMessage(content="清理完了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


def _team_graph(tool: str, *, approval: str | None = None, max_steps: int = 3):
    cfg = {"goal": "清理", "max_rounds": 3,
           "agents": [{"name": "dba", "system": "你是 DBA", "tools": [tool], "max_steps": max_steps}]}
    if approval:
        cfg["approval"] = approval
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("team", "supervisor", **cfg),
        node("out", "output", fields=[]),
    )


async def test_team_member_does_not_run_a_call_that_needs_approval(writable, monkeypatch):
    tool, count = writable
    _script_team(monkeypatch, member_call={"name": tool, "args": {"sql": "DELETE FROM t"}})
    run_id = await _start(_team_graph(tool))
    run = await _wait(run_id, ("interrupted", "failed", "succeeded"))

    assert run.status == "succeeded", run.error
    assert count() == 5, "协作成员绕过审批写了库"
    codes = [e.data.get("code") for e in await _events(run_id) if e.type == "log"]
    assert "tool_needs_approval" in codes


async def test_team_with_approval_never_runs_it(writable, monkeypatch):
    """放行是显式的选择：节点上设成 never 才执行。"""
    tool, count = writable
    _script_team(monkeypatch, member_call={"name": tool, "args": {"sql": "DELETE FROM t"}})
    run_id = await _start(_team_graph(tool, approval="never"))
    run = await _wait(run_id, ("interrupted", "failed", "succeeded"))
    assert run.status == "succeeded", run.error
    assert count() == 0


async def test_team_member_out_of_steps_settles_instead_of_handing_in_a_tool_result(
    writable, monkeypatch,
):
    """步数用完时交上去的得是结论，不能是工具的原始返回。"""
    tool, _ = writable
    _script_team(monkeypatch, member_call={"name": tool, "args": {"sql": "SELECT 1 AS one"}})
    run_id = await _start(_team_graph(tool, max_steps=1))
    run = await _wait(run_id, ("interrupted", "failed", "succeeded"))
    assert run.status == "succeeded", run.error

    events = await _events(run_id)
    ends = [e.data for e in events if e.type == "agent.step.end"]
    assert ends and ends[0]["preview"] == "收尾结论", ends
    assert "step_limit_settled" in [e.data.get("code") for e in events if e.type == "log"]


# --------------------------------------------------------------------------
# 多条审批并存
# --------------------------------------------------------------------------


def _two_editors():
    return {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("h1", "human", mode="edit", title="改稿一", draft="草稿一"),
            node("h2", "human", mode="edit", title="改稿二", draft="草稿二"),
            node("out", "output", fields=[
                {"name": "一", "value": "{{ nodes.h1.value }}"},
                {"name": "二", "value": "{{ nodes.h2.value }}"},
            ]),
        ],
        "edges": [
            {"source": "start", "target": "h1"}, {"source": "start", "target": "h2"},
            {"source": "h1", "target": "out"}, {"source": "h2", "target": "out"},
        ],
    }


async def test_parallel_edits_arrive_in_full():
    """两条审批并存时，交给引擎的是库里存的那份——以前存之前先截到 2000 字。"""
    run_id = await _start(_two_editors())
    await _wait(run_id, ("interrupted",))
    pending = await _approvals(run_id)
    assert set(pending) == {"h1", "h2"}

    await run_manager.resume(run_id, {"approved": True, "value": "甲" * 5000},
                             approval_id=pending["h1"].id)
    await run_manager.resume(run_id, {"approved": True, "value": "乙" * 5000},
                             approval_id=pending["h2"].id)
    run = await _wait(run_id, ("failed", "succeeded"))
    assert run.status == "succeeded", run.error
    assert len(run.output["一"]) == 5000 and len(run.output["二"]) == 5000


async def test_parallel_rejection_with_content_is_still_a_rejection():
    """回复里带了 value，以前解包时只留 value——驳回就这么被当成了交稿。"""
    run_id = await _start(_two_editors())
    await _wait(run_id, ("interrupted",))
    pending = await _approvals(run_id)

    await run_manager.resume(run_id, {"approved": True, "value": "甲"},
                             approval_id=pending["h1"].id)
    await run_manager.resume(run_id, {"approved": False, "note": "不行", "value": "乙"},
                             approval_id=pending["h2"].id)
    run = await _wait(run_id, ("failed", "succeeded"))
    assert run.status == "failed" and "驳回" in (run.error or ""), run.error
