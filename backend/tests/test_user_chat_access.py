"""开登录后使用者的问数据（D-c）：能发起什么样的运行、查询结果在工具边界遮、运行的 WebSocket 按运行判、数据源名单。

- 图：只许输入、模型、Agent、报告、成果、知识检索、数据整形、条件分支、口径卡、合并查询、调用工具；Agent 和调用工具
  节点只许数据源工具和只读的内置工具（app/auth/chat_policy.py）。沙箱代码、自定义工具、MCP、网页、HTTP、文件、平台的
  写入能力一律 403；
- 调用工具节点查数据源：和 Agent 同一条路，结果在工具边界遮、可写源上的写语句被守卫拒掉；能力治理里改成写入的工具
  在只读运行里不执行；
- 使用者发起的问数据运行一定带 MASK_RESULTS_OPTION：真的执行一次（演示模型调数据源查询），事件、快照、实时流里都没有
  遮罩列的原值；
- WebSocket：没登录的握手前关掉；使用者只能连自己能读、查询结果在工具边界遮过的运行（握手时判，1008 拒）；
- GET /api/datasources/names：开了问数据的使用者能读，只有名字、说明、种类，只列启用着的。

数据源是现造的 SQLite 文件，姓名、手机号都是虚构的。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, select

from app.auth.chat_policy import chat_graph_problem
from app.data import introspect
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run, RunEvent
from app.engine.runner import run_manager
from app.tools.datasource import MASK_RESULTS_OPTION
from tests.fakes import auth_env

PHONE = "13900000001"
MASKED = "已遮罩"


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def chat_graph(*tools: str, middle: dict | None = None) -> dict:
    return chain(node("start", "input", fields=[{"name": "question"}]),
                 middle or node("ask", "agent", prompt="{{ input.question }}", tools=list(tools), max_steps=4),
                 node("out", "output", fields=[{"name": "回答", "value": "{{ nodes.ask.text }}"}]))


# ---------------------------------------------------------------- 图的白名单（纯函数）


@pytest.mark.parametrize("graph", [
    chat_graph("db_query__crm", "db_schema__crm", "calculator", "current_time", "json_query", "kb_search",
               "memory_recall"),
    chain(node("start", "input"), node("kb", "retrieve", query="x"), node("t", "transform", expression="1"),
          node("out", "output")),
    # 调用工具节点：绑定的工具过同一份白名单就行（D1）
    chat_graph(middle=node("ask", "tool", tool="db_query__crm", args={"sql": "SELECT city FROM customers"})),
    chat_graph(middle=node("ask", "tool", tool="calculator", args={"expression": "1 + 1"})),
    chat_graph(middle=node("ask", "tool", tool="kb_search", args={"query": "退款规则"})),
    {"nodes": "坏的"},          # 不成形的图交给 launch_run 照常报校验问题
])
def test_graphs_a_user_may_run_in_chat(graph):
    assert chat_graph_problem(graph) is None


@pytest.mark.parametrize("graph, said", [
    (chat_graph(middle=node("ask", "code", code="print(1)")), "沙箱代码"),
    (chat_graph(middle=node("ask", "tool", tool="http_request")), "http_request"),
    (chat_graph(middle=node("ask", "tool", tool="records_append")), "records_append"),
    (chat_graph(middle=node("ask", "tool", tool="memory_write")), "memory_write"),
    (chat_graph(middle=node("ask", "tool", tool="mcp:fs/read_file")), "mcp:fs/read_file"),
    (chat_graph(middle=node("ask", "tool", tool="{{ vars.tool }}")), "模板"),
    (chat_graph(middle=node("ask", "supervisor", agents=[])), "多 Agent 协作"),
    (chat_graph(middle=node("ask", "subgraph", workflow_id="w")), "子工作流"),
    (chat_graph(middle=node("ask", "human", mode="approve")), "人工审批"),
    (chat_graph(middle=node("ask", "memory", action="write")), "长期记忆"),
    (chat_graph("http_request"), "http_request"),
    (chat_graph("web_fetch"), "web_fetch"),
    (chat_graph("python_exec"), "python_exec"),
    (chat_graph("file_read"), "file_read"),
    (chat_graph("memory_write"), "memory_write"),
    (chat_graph("mcp:fs/read_file"), "mcp:fs/read_file"),
    (chat_graph("my_custom_http"), "my_custom_http"),
    (chat_graph("{{ vars.tool }}"), "模板"),
])
def test_graphs_a_user_may_not_run_in_chat(graph, said):
    problem = chat_graph_problem(graph)
    assert problem and problem.startswith("没有权限") and said in problem, problem


@pytest.mark.parametrize("graph", [
    chat_graph(middle=node("ask", "agent", prompt="x", tools=["memory_recall"], memory_scope="builders")),
    chat_graph(middle=node("kb", "retrieve", query="x", collection="hr")),
    {**chat_graph("kb_search"), "defaults": {"collection": "hr"}},
    # 调用工具节点的参数直接交给工具：在参数里挑知识库同样不许
    chat_graph(middle=node("kb", "tool", tool="kb_search", args={"query": "x", "collection": "hr"})),
    {**chat_graph("memory_recall"), "defaults": {"memory_scope": "  "}},
    # 节点上没写参数、运行时取图级 defaults 里的 args（和运行时同一个取值规则，engine/launch.node_setting）
    {**chat_graph(middle=node("kb", "tool", tool="kb_search")), "defaults": {"args": {"query": "x", "collection": "hr"}}},
])
def test_users_may_not_pick_memory_or_knowledge_scope(graph):
    """记忆、知识库的内容不经遮罩：节点上、图级 defaults 里都不许指定范围，用管理员设好的默认值。"""
    problem = chat_graph_problem(graph)
    assert problem and "记忆范围和知识库" in problem


def test_tools_from_graph_defaults_count_too():
    """Agent 上没写工具时运行时用图级默认值里的：白名单同样按它查（engine/launch.tool_bindings）。"""
    graph = chain(node("start", "input"), node("ask", "agent", prompt="x"), node("out", "output"))
    graph["defaults"] = {"tools": ["web_search"]}
    problem = chat_graph_problem(graph)
    assert problem and "web_search" in problem


# ---------------------------------------------------------------- 接口


async def _masked_source(tmp_path) -> str:
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, city TEXT);"
                     f"INSERT INTO customers VALUES (1, '张三', '{PHONE}', '杭州'), (2, '李四', '13900000002', '宁波');")
    db.commit()
    db.close()
    name = f"crm_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         description="客户", options={"mask_columns": "phone"})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        session.add(DataSource(name=f"off_{uuid.uuid4().hex[:6]}", kind="sqlite", database=str(path), enabled=False))
        await session.commit()
    return name


@pytest.fixture
async def world(tmp_path):
    await auth_env.reset()
    await run_manager.setup()
    admin = await auth_env.turn_on_local()
    await auth_env.make_user("uma", "user")
    await auth_env.make_user("bob", "builder")
    source = await _masked_source(tmp_path)
    uma = await auth_env.login("uma")
    bob = await auth_env.login("bob")
    try:
        yield {"admin": admin, "uma": uma, "bob": bob, "source": source}
    finally:
        await run_manager.shutdown()
        async with SessionLocal() as s:
            await s.execute(delete(Run).where(Run.started_by.in_(["uma", "bob"])))
            await s.execute(delete(DataSource))
            await s.commit()
        await auth_env.reset()


async def _allow_chat(world) -> None:
    r = await world["admin"].put("/api/auth/settings", json={"user_can_chat": True})
    assert r.status_code == 200, r.text


async def _finish(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "interrupted", "cancelled"):
            await run_manager.wait_idle(run_id)
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("运行没有结束")


def _script(monkeypatch, tool: str) -> None:
    """演示模型：第一轮查遮罩列，看到结果后作答。"""
    from app.providers import mock_model

    def _decide(self, messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="杭州有一位客户。")
        return AIMessage(content="", tool_calls=[{"name": tool, "args": {"sql": "SELECT name, phone, city FROM customers"},
                                                  "id": "call_1"}])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


async def test_a_user_can_only_launch_whitelisted_chat_graphs(world):
    await _allow_chat(world)
    uma = world["uma"]
    r = await uma.post("/api/runs", json={"origin": "chat", "graph": chat_graph("http_request"), "input": {}})
    assert r.status_code == 403 and "http_request" in r.json()["detail"]
    r = await uma.post("/api/runs", json={"origin": "chat", "graph": chat_graph(middle=node("ask", "code"))})
    assert r.status_code == 403 and "沙箱代码" in r.json()["detail"]
    r = await uma.post("/api/runs", json={"origin": "chat", "run_class": "formal", "workflow_id": "x"})
    assert r.status_code == 403, "正式运行只经应用入口发起"
    # 搭建者照旧不受这份白名单约束（发起前检查另说：这里只看不是 403）
    r = await world["bob"].post("/api/runs", json={"origin": "chat", "graph": chat_graph(middle=node("ask", "code"))})
    assert r.status_code != 403


async def test_a_users_chat_run_is_masked_end_to_end(world, monkeypatch):
    """真的执行一次：使用者发起、Agent 查了遮罩列。运行选项带着遮罩开关，事件、查询快照、实时流里都没有原值。"""
    await _allow_chat(world)
    tool = f"db_query__{world['source']}"
    _script(monkeypatch, tool)
    r = await world["uma"].post("/api/runs", json={"origin": "chat", "graph": chat_graph(tool),
                                                   "input": {"question": "杭州有几位客户"}})
    assert r.status_code == 201, r.text
    run = await _finish(r.json()["id"])
    assert run.status == "succeeded", run.error
    assert run.options.get(MASK_RESULTS_OPTION) is True and run.started_by == "uma" and run.origin_kind == "chat"
    async with SessionLocal() as s:
        events = list((await s.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars())
        snaps = list((await s.execute(select(Artifact).where(Artifact.run_id == run.id,
                                                             Artifact.kind == "query_snapshot"))).scalars())
    dumped = json.dumps([e.data for e in events], ensure_ascii=False)
    assert "tool.end" in {e.type for e in events}
    assert PHONE not in dumped and "张三" in dumped, "遮罩列换掉，别的列照旧"
    assert snaps, "查询照常落快照"
    from app.core.artifact_store import load

    for snap in snaps:
        assert PHONE not in json.dumps(load(snap.id), ensure_ascii=False)

    # 使用者能读自己这次运行的记录、事件，也能连实时流；实时流里同样没有原值
    assert (await world["uma"].get(f"/api/runs/{run.id}/events")).status_code == 200
    frames = await _ws(f"/api/runs/{run.id}/stream", _cookie(world["uma"]))
    assert frames[0]["type"] == "websocket.accept"
    texts = [f.get("text") or "" for f in frames if f["type"] == "websocket.send"]
    assert texts and '"stream.end"' in texts[-1] and PHONE not in "".join(texts)


async def test_a_users_chat_run_cannot_write_a_writable_source(world, monkeypatch, tmp_path):
    """可写源 + Agent 自己写了 approval=never：写语句照样被守卫拒掉、不执行（只读不交给审批），表里的行一条没少。
    搭建者发起的同一张图照旧能写——只读只给 user 角色的问数据运行。"""
    from app.providers import mock_model
    from app.tools.datasource import READ_ONLY_OPTION

    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE ledger (id INTEGER PRIMARY KEY, amount INTEGER);"
                     "INSERT INTO ledger VALUES (1, 10), (2, 20);")
    db.commit()
    db.close()
    name = f"ledger_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=False, enabled=True)
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
    replies: list[str] = []

    def _decide(self, messages):
        if seen := [m for m in messages if isinstance(m, ToolMessage)]:
            replies.append(str(seen[-1].content))
            return AIMessage(content="好了。")
        return AIMessage(content="", tool_calls=[{"name": f"db_query__{name}", "args": {"sql": "DELETE FROM ledger"},
                                                  "id": "call_1"}])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    graph = chat_graph(middle=node("ask", "agent", prompt="{{ input.question }}", tools=[f"db_query__{name}"],
                                   max_steps=4, approval="never"))
    await _allow_chat(world)
    r = await world["uma"].post("/api/runs", json={"origin": "chat", "graph": graph, "input": {"question": "清空"}})
    assert r.status_code == 201, r.text
    run = await _finish(r.json()["id"])
    assert run.status == "succeeded", run.error
    assert run.options.get(READ_ONLY_OPTION) is True
    assert replies and replies[-1].startswith("SQL 被拒绝"), replies
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2

    replies.clear()
    r = await world["bob"].post("/api/runs", json={"origin": "chat", "graph": graph, "input": {"question": "清空"}})
    assert r.status_code == 201, r.text
    run = await _finish(r.json()["id"])
    assert run.status == "succeeded", run.error
    assert not (run.options or {}).get(READ_ONLY_OPTION)
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0, "搭建者照旧能写"


async def _writable_masked_source(tmp_path) -> tuple[str, object]:
    """可写的客户库（手机号是遮罩列）：调用工具节点查它、想删它。"""
    path = tmp_path / f"crm_rw_{uuid.uuid4().hex[:6]}.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, city TEXT);"
                     f"INSERT INTO customers VALUES (1, '张三', '{PHONE}', '杭州'), (2, '李四', '13900000002', '宁波');")
    db.commit()
    db.close()
    name = f"crm_rw_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=False, enabled=True,
                         description="客户（可写）", options={"mask_columns": "phone"})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
    return name, path


def _tool_chat(tool: str, sql: str) -> dict:
    """问数据当场搭的图：取数用调用工具节点（D1），不经 Agent、不调模型。approval=never 是发起人自己写的：只读不交给
    审批，写成 never 也照样拒写。"""
    return chain(node("start", "input", fields=[{"name": "question"}]),
                 node("q", "tool", tool=tool, args={"sql": sql}, fail_fast=False, approval="never"),
                 node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.q }}"}]))


async def test_a_users_tool_node_is_masked_and_read_only(world, tmp_path):
    """调用工具节点这条路和 Agent 一样（D1）：使用者问数据的运行里，结果在工具边界就遮（事件、查询快照、运行产出里都没有
    原值），可写源上的写语句被守卫拒掉、一行没少（节点上写了免审批也一样）；搭建者发起的同一张写图照旧能写。"""
    from app.core.artifact_store import load
    from app.tools.datasource import FORCED_READONLY, READ_ONLY_OPTION

    await _allow_chat(world)
    name, path = await _writable_masked_source(tmp_path)
    tool = f"db_query__{name}"

    r = await world["uma"].post("/api/runs", json={"origin": "chat", "input": {"question": "客户"},
                                                   "graph": _tool_chat(tool, "SELECT name, phone, city FROM customers")})
    assert r.status_code == 201, r.text
    run = await _finish(r.json()["id"])
    assert run.status == "succeeded", run.error
    assert run.options[MASK_RESULTS_OPTION] is True and run.options[READ_ONLY_OPTION] is True
    async with SessionLocal() as s:
        events = list((await s.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars())
        snaps = list((await s.execute(select(Artifact).where(Artifact.run_id == run.id,
                                                             Artifact.kind == "query_snapshot"))).scalars())
    dumped = json.dumps([e.data for e in events], ensure_ascii=False) + json.dumps(run.output, ensure_ascii=False)
    assert "tool.end" in {e.type for e in events}
    assert PHONE not in dumped and MASKED in dumped and "张三" in dumped, "遮罩列换掉，别的列照旧"
    assert snaps and all(PHONE not in json.dumps(load(snap.id), ensure_ascii=False) for snap in snaps)

    delete_all = _tool_chat(tool, "DELETE FROM customers")
    r = await world["uma"].post("/api/runs", json={"origin": "chat", "input": {"question": "清空"}, "graph": delete_all})
    assert r.status_code == 201, r.text
    run = await _finish(r.json()["id"])
    async with SessionLocal() as s:
        events = list((await s.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars())
    assert FORCED_READONLY[:12] in json.dumps([e.data for e in events], ensure_ascii=False), "写语句被守卫拒掉"
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2

    r = await world["bob"].post("/api/runs", json={"origin": "chat", "input": {"question": "清空"}, "graph": delete_all})
    assert r.status_code == 201, r.text
    await _finish(r.json()["id"])
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0, "搭建者照旧能写"


async def test_a_tool_governed_as_write_is_not_run_in_a_read_only_run(world, tmp_path):
    """能力治理把数据源查询改成「写入」：使用者问数据里的调用工具节点不进动作流程（探索运行在那里会演练、交回假回执），
    直接拒、说清是只读运行；表里的行一条没动。"""
    from app.db.models import ActionRecord, Capability

    await _allow_chat(world)
    name, path = await _writable_masked_source(tmp_path)
    tool = f"db_query__{name}"
    async with SessionLocal() as s:
        s.add(Capability(name=tool, system=name, title="客户库", effect_system="dynamic", effect="write"))
        await s.commit()
    try:
        r = await world["uma"].post("/api/runs", json={"origin": "chat", "input": {"question": "清空"},
                                                       "graph": _tool_chat(tool, "DELETE FROM customers")})
        assert r.status_code == 201, r.text
        run = await _finish(r.json()["id"])
        assert run.status == "failed" and "只能读数据" in (run.error or "") and tool in run.error, run.error
        async with SessionLocal() as s:
            assert not list((await s.execute(select(ActionRecord).where(ActionRecord.run_id == run.id))).scalars())
        assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(Capability).where(Capability.name == tool))
            await s.commit()


async def test_kb_search_keeps_to_the_runs_knowledge_base_when_the_scope_is_fixed(monkeypatch):
    """工具层的那一道（D1）：范围固定的运行里 kb_search 不认 collection 参数——直接写的、参数名写错被纠成 collection 的都拒，
    不悄悄改查默认库；没写的照常查运行定下的那一个。范围不固定（搭建者的运行）照旧按参数查。"""
    from app.memory import kb
    from app.tools.registry import ToolArgsError, ToolContext, Versions, call_tool

    searched: list[str] = []

    async def _search(session, *, collection, query, limit, **_):
        searched.append(collection)
        return []

    monkeypatch.setattr(kb, "search", _search)
    fixed = ToolContext(collection="default", fixed_scope=True, data_versions=Versions.CURRENT)
    for args in ({"query": "x", "collection": "hr"}, {"query": "x", "limit": 5, "coll": "hr"}):
        with pytest.raises(ToolArgsError) as refused:
            await call_tool("kb_search", args, fixed)
        assert "只能检索管理员设好的知识库" in str(refused.value) and "hr" in str(refused.value)
    assert searched == []
    await call_tool("kb_search", {"query": "x"}, fixed)
    await call_tool("kb_search", {"query": "x", "collection": "default"}, fixed)
    await call_tool("kb_search", {"query": "x", "collection": "hr"},
                    ToolContext(collection="default", data_versions=Versions.CURRENT))
    assert searched == ["default", "default", "hr"]


def _kb_search_spy(monkeypatch) -> list[str]:
    from app.memory import kb

    searched: list[str] = []

    async def _search(session, *, collection, query, limit, **_):
        searched.append(collection)
        return [{"content": f"{collection} 库里的一段", "score": 1.0}]

    monkeypatch.setattr(kb, "search", _search)
    return searched


async def test_a_users_run_cannot_reach_another_knowledge_base_through_the_tool_layer(world, monkeypatch):
    """白名单认不出的两种写法，运行时在工具层拒（D1）：调用工具节点的参数名写错（coll 被纠成 collection）、Agent 在参数里
    挑知识库。使用者的问数据只查得到运行定下的知识库；搭建者发起的同一张图照旧按参数查。"""
    from app.providers import mock_model

    await _allow_chat(world)
    searched = _kb_search_spy(monkeypatch)
    typo = chain(node("start", "input", fields=[{"name": "question"}]),
                 node("kb", "tool", tool="kb_search", args={"query": "x", "limit": 5, "coll": "hr"}),
                 node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.kb }}"}]))
    assert chat_graph_problem(typo) is None, "白名单只看字面写的参数名，这一道在工具层"
    r = await world["uma"].post("/api/runs", json={"origin": "chat", "graph": typo, "input": {"question": "x"}})
    assert r.status_code == 201, r.text
    run = await _finish(r.json()["id"])
    assert run.status == "failed" and "只能检索管理员设好的知识库" in (run.error or ""), run.error
    assert "hr" not in searched

    replies: list[str] = []

    def _decide(self, messages):
        if seen := [m for m in messages if isinstance(m, ToolMessage)]:
            replies.append(str(seen[-1].content))
            return AIMessage(content="好了。")
        return AIMessage(content="", tool_calls=[{"name": "kb_search", "args": {"query": "薪酬", "collection": "hr"},
                                                  "id": "call_1"}])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    r = await world["uma"].post("/api/runs", json={"origin": "chat", "graph": chat_graph("kb_search"),
                                                   "input": {"question": "薪酬"}})
    assert r.status_code == 201, r.text
    run = await _finish(r.json()["id"])
    assert run.status == "succeeded", run.error
    assert replies and "只能检索管理员设好的知识库" in replies[-1], replies
    assert "hr" not in searched

    replies.clear()
    r = await world["bob"].post("/api/runs", json={"origin": "chat", "graph": chat_graph("kb_search"),
                                                   "input": {"question": "薪酬"}})
    assert r.status_code == 201, r.text
    assert (await _finish(r.json()["id"])).status == "succeeded"
    assert searched == ["hr"] and "hr 库里的一段" in replies[-1], "搭建者的运行照旧按参数查"


async def test_a_users_chat_run_keeps_to_its_own_memory_scope(world):
    """请求体里带记忆范围、知识库的拒；不带的，记忆是这位使用者自己的范围，知识库是设置里的默认值。"""
    await _allow_chat(world)
    uma = world["uma"]
    graph = chat_graph("memory_recall")
    for extra in ({"memory_scope": "default"}, {"collection": "hr"}):
        r = await uma.post("/api/runs", json={"origin": "chat", "graph": graph, "input": {}, **extra})
        assert r.status_code == 403 and "记忆范围和知识库" in r.json()["detail"]
    r = await uma.post("/api/runs", json={"origin": "chat", "graph": graph, "input": {"question": "x"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["memory_scope"] == "chat:uma" and body["collection"] == "default"
    await _finish(body["id"])


# ---------------------------------------------------------------- 问数据的生成流


class _Scripted:
    """第一轮按节点手册的标准写法搭（tool 节点调 fetch_tool，缺省是查库），收到自查请求时改成 Agent 查库。记下 system 和
    每一轮的请求。"""

    def __init__(self, source: str, fetch_tool: str | None = None) -> None:
        self.source = source
        self.fetch_tool = fetch_tool or f"db_query__{source}"
        self.systems: list[str] = []
        self.humans: list[str] = []

    def _lines(self, repair: bool) -> list[str]:
        if repair:
            ops = [{"op": "remove_node", "id": "fetch"},
                   {"op": "add_node", "node": {"id": "ask", "type": "agent", "label": "查库",
                                               "config": {"prompt": "{{ input.question }}",
                                                          "tools": [f"db_query__{self.source}"]}}},
                   {"op": "add_edge", "edge": {"source": "start", "target": "ask"}},
                   {"op": "add_edge", "edge": {"source": "ask", "target": "out"}},
                   {"op": "done", "explanation": "改成 Agent 查库"}]
        else:
            ops = [{"op": "plan", "summary": "查客户"},
                   {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入",
                                               "config": {"fields": [{"name": "question"}]}}},
                   {"op": "add_node", "node": {"id": "fetch", "type": "tool", "label": "查库",
                                               "config": {"tool": self.fetch_tool,
                                                          "args": {"sql": "SELECT city FROM customers"}}}},
                   {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果",
                                               "config": {"fields": [{"name": "回答", "value": "{{ last_message }}"}]}}},
                   {"op": "add_edge", "edge": {"source": "start", "target": "fetch"}},
                   {"op": "add_edge", "edge": {"source": "fetch", "target": "out"}},
                   {"op": "done", "explanation": "查好了", "run": True}]
        return [json.dumps(o, ensure_ascii=False) for o in ops]

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        self.systems.append(next(text for role, text in messages if role == "system"))
        human = next(text for role, text in messages if role == "human")
        self.humans.append(human)
        for line in self._lines("上线前自查" in human):
            yield AIMessageChunk(content=line + "\n")


async def _generate(client, monkeypatch, model: _Scripted) -> list[dict]:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream", json={"instruction": "杭州有几位客户"}) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def test_generation_for_a_user_keeps_to_the_chat_policy(world, monkeypatch):
    """使用者问数据时，提示词先说清只能用数据源查询和只读的内置工具；tool 节点查库照标准写法搭，一次就过、自动运行（D1）。
    tool 节点绑了不许的工具（http_request）按 chat_policy 交回去改，改好才交付。搭建者照旧按节点手册来。"""
    await _allow_chat(world)
    model = _Scripted(world["source"])
    events = await _generate(world["uma"], monkeypatch, model)
    assert "这位用户是「使用者」" in model.systems[0] and "- http_request" not in model.systems[0]
    assert "tool 节点和 agent 都只能用" in model.systems[0]
    assert len(model.humans) == 1, "tool 节点查库不算问题"
    final = next(e for e in events if e.get("op") == "final")
    assert final["autorun"] is True and "tool" in {n["type"] for n in final["graph"]["nodes"]}
    assert chat_graph_problem(final["graph"]) is None

    model = _Scripted(world["source"], fetch_tool="http_request")
    events = await _generate(world["uma"], monkeypatch, model)
    assert len(model.humans) == 2 and "http_request" in model.humans[1], "绑了不许的工具按 chat_policy 交回去改"
    check = [e for e in events if e.get("op") == "check"][-1]
    final = next(e for e in events if e.get("op") == "final")
    assert check["status"] == "passed" and final["autorun"] is True
    assert {n["type"] for n in final["graph"]["nodes"]} == {"input", "agent", "output"}
    assert chat_graph_problem(final["graph"]) is None

    model = _Scripted(world["source"])
    events = await _generate(world["bob"], monkeypatch, model)
    assert "这位用户是「使用者」" not in model.systems[0] and len(model.humans) == 1
    final = next(e for e in events if e.get("op") == "final")
    assert "tool" in {n["type"] for n in final["graph"]["nodes"]}


async def test_generation_for_a_user_reports_what_it_could_not_fix(world, monkeypatch):
    """改不好的照实交付：自查失败、final 里列出 chat.not_allowed，不自动运行。"""
    await _allow_chat(world)
    model = _Scripted(world["source"], fetch_tool="http_request")
    model._lines = lambda repair: _Scripted(world["source"], fetch_tool="http_request")._lines(False) if not repair \
        else [json.dumps({"op": "done", "explanation": "没改"}, ensure_ascii=False)]
    events = await _generate(world["uma"], monkeypatch, model)
    check = [e for e in events if e.get("op") == "check"][-1]
    final = next(e for e in events if e.get("op") == "final")
    assert check["status"] == "failed" and any(i["code"] == "chat.not_allowed" for i in check["issues"])
    assert final["autorun"] is False and any(i["code"] == "chat.not_allowed" for i in final["issues"])


# ---------------------------------------------------------------- WebSocket


def _cookie(client) -> str:
    return f"agentlab_session={client.cookies.get('agentlab_session')}"


async def _ws(path: str, cookie: str | None) -> list[dict]:
    """把一次 WebSocket 握手原样交给整个应用（中间件 + 路由级依赖 + 路由），记下发出去的帧。"""
    from app.main import app

    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    sent: list[dict] = []

    async def receive():
        return await inbox.get()

    async def send(message):
        sent.append(message)
        if message["type"] == "websocket.close":
            await inbox.put({"type": "websocket.disconnect", "code": 1000})

    headers = [(b"host", b"test")] + ([(b"cookie", cookie.encode())] if cookie else [])
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "http_version": "1.1", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 50000), "server": ("test", 80), "subprotocols": [], "state": {}}
    await asyncio.wait_for(app(scope, receive, send), 10)
    return sent


async def _settled_run(**fields) -> str:
    async with SessionLocal() as s:
        run = Run(workflow_name="问数据", graph={}, input={}, status="succeeded", run_class="exploratory", **fields)
        s.add(run)
        await s.flush()
        s.add(RunEvent(run_id=run.id, seq=1, type="run.started", ts=1.0, data={}))
        await s.commit()
        return run.id


def _refused(frames: list[dict]) -> bool:
    return [f["type"] for f in frames] == ["websocket.close"] and frames[0].get("code") == 1008


async def test_websocket_is_judged_per_run_at_the_handshake(world):
    mine = await _settled_run(started_by="uma", origin_kind="chat", options={MASK_RESULTS_OPTION: True})
    others = await _settled_run(started_by="bob", origin_kind="chat", options={MASK_RESULTS_OPTION: True})
    raw = await _settled_run(started_by="uma", origin_kind="chat")      # 关登录时自报同名留下的，没在边界遮过
    uma, bob = _cookie(world["uma"]), _cookie(world["bob"])

    assert _refused(await _ws(f"/api/runs/{mine}/stream", None)), "没登录的握手前关掉"
    assert _refused(await _ws(f"/api/runs/{mine}/stream", uma)), "没开问数据时使用者连不了运行"
    await _allow_chat(world)
    frames = await _ws(f"/api/runs/{mine}/stream", uma)
    assert frames[0]["type"] == "websocket.accept" and '"stream.end"' in frames[-2]["text"]
    assert _refused(await _ws(f"/api/runs/{others}/stream", uma)), "别人的运行"
    assert _refused(await _ws(f"/api/runs/{raw}/stream", uma)), "查询结果没在工具边界遮过的运行"
    assert _refused(await _ws("/api/runs/nope/stream", uma))
    assert (await _ws(f"/api/runs/{raw}/stream", bob))[0]["type"] == "websocket.accept", "搭建者照旧"


# ---------------------------------------------------------------- 数据源名单


async def test_source_names_for_the_chat_page(world):
    uma, bob = world["uma"], world["bob"]
    assert (await uma.get("/api/datasources/names")).status_code == 403, "没开问数据时不给使用者"
    assert (await uma.get("/api/datasources")).status_code == 403
    await _allow_chat(world)
    body = (await uma.get("/api/datasources/names")).json()
    # 同一个测试进程里别的测试留下的数据源也会在名单里：只核对这里建的两个（一个启用、一个停用）
    names = [i["name"] for i in body["items"]]
    assert world["source"] in names and not [n for n in names if n.startswith("off_")], "只列启用着的"
    item = next(i for i in body["items"] if i["name"] == world["source"])
    assert set(item) == {"id", "name", "description", "kind"}, "连接信息不在名单里"
    assert item["description"] == "客户" and item["kind"] == "sqlite"
    assert (await uma.get("/api/datasources")).status_code == 403, "完整的数据源列表照旧只给搭建者"
    assert (await bob.get("/api/datasources/names")).status_code == 200


async def test_source_names_with_login_off():
    await auth_env.reset()
    async with auth_env.client() as c:
        r = await c.get("/api/datasources/names")
    assert r.status_code == 200 and "items" in r.json()
