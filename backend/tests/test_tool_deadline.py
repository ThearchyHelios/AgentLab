"""工具的时限要按时生效，并且事先告诉界面这个时限是多少。

真实运行里有一次查询：tool.end 的 duration_ms 是 90628，报错却写着「查询超过 30s
被中断」。30 秒时限确实到了，取消也发出去了——但 asyncio.wait_for 取消之后要**等被
取消的那一方收完尾**，而驱动收尾（关连接、回滚）要等数据库把那条语句跑完。于是
「30 秒被中断」实际等了 90 秒，报错还说的是 30。

这里把两件事钉住：
- 到了时限，节点就拿回控制权、如实说「等了多久、放弃了」，驱动的收尾放到后台；
- tool.start 带上 timeout_s，界面据此写「已超出 30s 上限」，不在前端写死。

慢收尾的工具用两种方式造：一个假的（取消后还要磨蹭一阵才肯退出），和一个真的
SQLite 查询（aiosqlite 在线程里跑语句，关连接要排在那条语句后面）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.runner import run_manager

#: 假工具声明的时限。取消之后它还要再磨蹭 CLEANUP 秒才退出，像一个在等服务端的驱动
LIMIT = 0.3
CLEANUP = 2.0


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()
    # 被放弃的调用在后台收尾（假工具要再磨蹭 CLEANUP 秒，SQLite 要等语句跑完才还连接）。
    # 等它们收完再结束，测试的事件循环关掉时不该还挂着占着连接的任务
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=15)


class _Args(BaseModel):
    sql: str


def _slow_tool(name: str = "slow_lookup") -> StructuredTool:
    async def _run(sql: str) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(CLEANUP)      # 驱动收尾：等服务端把语句跑完才肯关连接
            raise
        return "不会走到这里"

    return StructuredTool(name=name, description="慢查询", args_schema=_Args, coroutine=_run,
                          func=None, metadata={"timeout_s": LIMIT})


def _serve(monkeypatch, tool: StructuredTool) -> None:
    """让各节点按名字拿到这个工具。"""
    async def _build(names, ctx, *, session=None):
        return [tool] if tool.name in names else []

    import app.engine.nodes.llm as llm_nodes
    import app.engine.nodes.multi as multi_nodes
    import app.engine.nodes.tools as tool_nodes
    import app.tools.registry as registry

    for module in (llm_nodes, multi_nodes, tool_nodes, registry):
        monkeypatch.setattr(module, "build_tools", _build)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def _finish(graph) -> tuple[Run, list[RunEvent]]:
    run = await run_manager.start(graph=graph, input_payload={"question": "x"})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed", "cancelled", "interrupted"):
                break
    else:
        pytest.fail("运行没有在 20 秒内结束")
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return row, events


def _script_one_call(monkeypatch, tool_name: str, sql: str = "SELECT 1") -> list[str]:
    """第一轮调一次工具，看到结果就收口。返回模型看到的工具结果，供断言。"""
    from app.providers import mock_model

    seen: list[str] = []

    def _decide(self, messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if self.response_format:
            return AIMessage(content=json.dumps(
                {"assignments": [], "done": True} if seen or results else
                {"assignments": [{"agent": "查数员", "instruction": "查一下"}]}))
        if self.tools and not results:
            return AIMessage(content="", tool_calls=[
                {"name": tool_name, "args": {"sql": sql}, "id": "call_slow"}])
        seen.extend(str(m.content) for m in results)
        return AIMessage(content="查询超时了，缩小范围再试。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


# --------------------------------------------------------------------------


async def test_an_agent_gets_control_back_when_the_limit_is_reached(monkeypatch):
    tool = _slow_tool()
    _serve(monkeypatch, tool)
    seen = _script_one_call(monkeypatch, tool.name)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="查", tools=[tool.name], approval="never", max_steps=3),
        node("out", "output", fields=[]),
    ))
    assert row.status == "succeeded", row.error

    start = next(e.data for e in events if e.type == "tool.start")
    assert start["timeout_s"] == LIMIT, start
    end = next(e.data for e in events if e.type in ("tool.end", "tool.error"))
    assert end["duration_ms"] < (LIMIT + CLEANUP) * 1000 * 0.75, \
        f"时限 {LIMIT}s，却等到了驱动收尾：{end['duration_ms']}ms"
    assert end.get("timed_out") is True, end
    # 模型要知道是超时、超了多久，才会去缩小查询范围
    assert seen and "0.3" in seen[0] and "放弃" in seen[0], seen


async def test_a_tool_node_reports_the_real_limit(monkeypatch):
    tool = _slow_tool()
    _serve(monkeypatch, tool)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("q", "tool", tool=tool.name, args={"sql": "SELECT 1"}, approval="never"),
        node("out", "output", fields=[]),
    ))
    assert row.status == "failed"
    assert "0.3" in (row.error or "") and "放弃" in (row.error or ""), row.error
    start = next(e.data for e in events if e.type == "tool.start")
    assert start["timeout_s"] == LIMIT
    err = next(e.data for e in events if e.type == "tool.error")
    assert err["duration_ms"] < (LIMIT + CLEANUP) * 1000 * 0.75, err
    assert err.get("timed_out") is True


async def test_a_team_member_gets_control_back_too(monkeypatch):
    tool = _slow_tool()
    _serve(monkeypatch, tool)
    _script_one_call(monkeypatch, tool.name)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("team", "supervisor", goal="查", max_rounds=3, approval="never", agents=[
            {"name": "查数员", "system": "你负责查库", "tools": [tool.name], "max_steps": 3}]),
        node("out", "output", fields=[]),
    ))
    assert row.status == "succeeded", row.error
    start = next(e.data for e in events if e.type == "tool.start")
    assert start["timeout_s"] == LIMIT
    end = next(e.data for e in events if e.type in ("tool.end", "tool.error"))
    assert end["duration_ms"] < (LIMIT + CLEANUP) * 1000 * 0.75, end


# --------------------------------------------------------------------------
# 真实的驱动：SQLite 的慢查询。关连接要排在那条语句后面
# --------------------------------------------------------------------------

#: 一条在时限之前一定跑不完的查询：pause 是测试里注册给 SQLite 的函数，在执行语句的
#: 那个线程里睡够秒数再返回。以前用递归 CTE 造慢查询，快几倍的机器上它不到 1 秒就跑完了
PAUSE_S = 4
SLOW_SQL = f"SELECT pause({PAUSE_S}) AS n"


@pytest.fixture
async def slow_source(tmp_path, monkeypatch):
    import time

    from sqlalchemy import event

    from app.data import guard
    from app.data.engine import engines
    import app.tools.datasource as datasource_tools

    @dataclasses.dataclass(frozen=True)
    class OneSecond(guard.QueryLimits):
        timeout_seconds: int = 1

    monkeypatch.setattr(guard, "QueryLimits", OneSecond)
    monkeypatch.setattr(datasource_tools, "QueryLimits", OneSecond)

    path = tmp_path / "orders.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER)")
    conn.commit()
    conn.close()
    name = f"shop_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        source = DataSource(name=name, kind="sqlite", database=str(path), readonly=True,
                            description="测试库", options={}, schema_cache={}, enabled=True)
        session.add(source)
        await session.commit()

    def _register(dbapi_conn, _record):
        dbapi_conn.create_function("pause", 1, lambda seconds: time.sleep(seconds) or seconds)

    engine = (await engines.get(source)).sync_engine
    event.listen(engine, "connect", _register)
    yield f"db_query__{name}"
    event.remove(engine, "connect", _register)


async def test_a_slow_query_is_abandoned_on_time(slow_source, monkeypatch):
    _script_one_call(monkeypatch, slow_source, SLOW_SQL)
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="查", tools=[slow_source], max_steps=3),
        node("out", "output", fields=[]),
    ))
    assert row.status == "succeeded", row.error
    start = next(e.data for e in events if e.type == "tool.start")
    assert start["timeout_s"] == 1, start
    end = next(e.data for e in events if e.type in ("tool.end", "tool.error"))
    assert end["duration_ms"] < 3000, f"1s 的查询时限等了 {end['duration_ms']}ms"
    assert "1s" in end["preview"], end


def test_where_each_limit_comes_from():
    from app.data.guard import QueryLimits
    from app.engine.toolcalls import limit_fields, limit_of

    query = limit_of(None, "db_query__shop", {"sql": "SELECT 1"})
    assert query.seconds == QueryLimits().timeout_seconds and query.enforce and query.self_timed
    # 数据源自己声明了时限：用它，但仍按「数据层自己掐、自己收尾」对待
    declared_query = limit_of(_slow_tool("db_query__shop"), "db_query__shop", {"sql": "x"})
    assert declared_query.seconds == LIMIT and declared_query.self_timed
    # 沙箱的时限由沙箱执行：界面要知道上限，引擎不按它掐（里面含着起虚拟机的时间）
    sandbox = limit_of(None, "python_exec", {"code": "print(1)", "timeout": 45})
    assert sandbox.seconds == 45 and not sandbox.enforce
    assert limit_of(None, "calculator", {"expression": "1+1"}) is None
    assert limit_fields(query) == {"timeout_s": QueryLimits().timeout_seconds}
    assert limit_fields(None) == {}
