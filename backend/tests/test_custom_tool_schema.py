"""自定义工具的参数定义写坏了：保存时就拒掉；库里已经存着的坏工具，运行时说清是哪个、坏在哪。

参数定义是用户在编辑器里手写的 JSON Schema。以前保存照单全收，写成
{"city": "string"} 或 {"type": "int"} 都存得进去；到了工作流绑定这个工具的那一刻，
schema_to_model 在 .get 上炸掉，运行记录里只剩一句「执行出错：'str' object has no
attribute 'get'」——既不知道是哪个工具，也不知道该去哪里改。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import CustomTool, Run, RunEvent
from app.engine.runner import run_manager
from app.main import app

GOOD = {"type": "object", "properties": {"store": {"type": "string", "description": "门店"}},
        "required": ["store"]}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _name() -> str:
    return f"lookup_{uuid.uuid4().hex[:6]}"


async def _count() -> int:
    async with SessionLocal() as session:
        return int((await session.execute(select(func.count(CustomTool.id)))).scalar_one())


# --------------------------------------------------------------------------
# 保存时拒掉
# --------------------------------------------------------------------------


@pytest.mark.parametrize("parameters, words", [
    ({"type": "object", "properties": {"city": "string"}}, ["city", '{"type": "string"}']),
    ({"type": "object", "properties": {"n": {"type": "int"}}}, ["n", "「int」", "integer"]),
    ({"type": "object", "properties": {"n": {"type": "integer"}}, "required": "n"}, ["required"]),
    ({"type": "object", "properties": ["city"]}, ["properties"]),
    ({"type": "object", "properties": {"_id": {"type": "string"}}}, ["_id", "下划线"]),
    ({"type": "object", "properties": {"model_config": {"type": "string"}}}, ["model_config"]),
    ({"type": "object", "properties": {"": {"type": "string"}}}, ["参数名"]),
])
async def test_a_malformed_schema_is_refused_with_words(client, parameters, words):
    before = await _count()
    r = await client.post("/api/custom-tools", json={
        "name": _name(), "kind": "http", "parameters": parameters,
        "config": {"url": "https://example.com/api"}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str), f"detail 要是一句话，不是 pydantic 的错误列表：{detail!r}"
    for word in words:
        assert word in detail, (word, detail)
    assert "Error" not in detail and "Traceback" not in detail
    assert await _count() == before, "写坏的工具不该落库"


@pytest.mark.parametrize("spec, fix, not_this", [
    # 照抄报错里的写法就能改好：写的是 "string"，就别让人改成 integer
    ("string", '{"type": "string"}', "integer"),
    ("int", '{"type": "integer"}', None),
    ("Number", '{"type": "number"}', None),
    ("bool", '{"type": "boolean"}', None),
    (5, '{"type": "string"}', None),
])
def test_the_example_in_the_message_is_the_type_they_meant(spec, fix, not_this):
    from app.tools.custom import schema_problem

    problem = schema_problem({"type": "object", "properties": {"store": spec}})
    assert problem and fix in problem, problem
    if not_this:
        assert not_this not in problem, problem


@pytest.mark.parametrize("kind, guess", [("int", "integer"), ("String", "string"), ("float", "number"),
                                         ("dict", "object")])
def test_a_near_miss_type_gets_a_suggestion(kind, guess):
    from app.tools.custom import schema_problem

    problem = schema_problem({"type": "object", "properties": {"n": {"type": kind}}})
    assert problem and f"是不是想写 {guess}" in problem, problem


def test_an_unrecognisable_type_gets_no_guess():
    from app.tools.custom import schema_problem

    problem = schema_problem({"type": "object", "properties": {"n": {"type": "timestamp"}}})
    assert problem and "「timestamp」" in problem and "是不是想写" not in problem, problem


async def test_parameters_that_are_not_an_object_are_refused_in_words_too(client):
    r = await client.post("/api/custom-tools", json={
        "name": _name(), "kind": "http", "parameters": ["city"], "config": {}})
    assert r.status_code == 422
    assert r.json()["detail"] == "参数定义要是一个 JSON 对象"


async def test_editing_a_tool_into_a_broken_schema_is_refused_and_nothing_changes(client):
    r = await client.post("/api/custom-tools", json={
        "name": _name(), "kind": "http", "parameters": GOOD,
        "config": {"url": "https://example.com/api"}})
    assert r.status_code == 201, r.text
    tool = r.json()
    try:
        broken = {**tool, "parameters": {"type": "object", "properties": {"store": "string"}}}
        r = await client.patch(f"/api/custom-tools/{tool['id']}", json=broken)
        assert r.status_code == 422, r.text
        assert "store" in r.json()["detail"]
        async with SessionLocal() as session:
            row = await session.get(CustomTool, tool["id"])
            assert row.parameters == GOOD, "被拒的修改不能落一半"
    finally:
        await client.delete(f"/api/custom-tools/{tool['id']}")


async def test_a_good_schema_still_saves(client):
    r = await client.post("/api/custom-tools", json={
        "name": _name(), "kind": "http", "parameters": GOOD,
        "config": {"url": "https://example.com/api"}})
    assert r.status_code == 201, r.text
    assert r.json()["problem"] is None
    await client.delete(f"/api/custom-tools/{r.json()['id']}")


@pytest.mark.filterwarnings("ignore:Field name .* shadows an attribute")
def test_every_schema_the_check_lets_through_really_builds():
    """schema_problem 放行的，schema_to_model 一定建得出来：它是两者之间唯一的关卡。"""
    from app.tools.custom import schema_problem, schema_to_model

    for name in ("store", "城市", "my-field", "json", "copy", "model_name"):
        schema = {"type": "object", "properties": {name: {"type": "string"}}}
        assert schema_problem(schema) is None, name
        schema_to_model("t", schema).model_json_schema()


# --------------------------------------------------------------------------
# 库里已经存着的坏工具
# --------------------------------------------------------------------------

BROKEN = {"type": "object", "properties": {"store": "string"}}


@pytest.fixture
async def broken_tool():
    """绕过接口直接写库：模拟这次改动之前就存进去的坏工具。"""
    name = _name()
    async with SessionLocal() as session:
        row = CustomTool(name=name, kind="http", parameters=BROKEN,
                         config={"url": "https://example.com/api"}, description="查门店")
        session.add(row)
        await session.commit()
        tool_id = row.id
    yield name
    async with SessionLocal() as session:
        row = await session.get(CustomTool, tool_id)
        if row:
            await session.delete(row)
            await session.commit()


@pytest.fixture
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
    run = await run_manager.start(graph=graph, input_payload={"question": "北区门店"})
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed", "cancelled", "interrupted"):
                break
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return row, events


def _clear(text: str | None, name: str) -> None:
    text = text or ""
    assert name in text, f"没说是哪个工具：{text}"
    assert "参数定义" in text and "store" in text, f"没说坏在哪：{text}"
    assert "工具" in text and "改" in text, f"没说去哪里改：{text}"
    for noise in ("执行出错", "内部", "attribute", "object has no"):
        assert noise not in text, f"还是笼统的内部错误：{text}"


async def test_an_agent_bound_to_a_broken_tool_fails_saying_which_and_why(engine_up, broken_tool):
    row, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("ask", "agent", prompt="查一下门店", tools=[broken_tool], max_steps=2),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.ask.text }}"}]),
    ))
    assert row.status == "failed"
    assert row.error_node_id == "ask"
    _clear(row.error, broken_tool)
    failed = next(e.data for e in events if e.type == "node.failed")
    _clear(failed["error"], broken_tool)


async def test_a_tool_node_bound_to_a_broken_tool_fails_saying_which_and_why(engine_up, broken_tool):
    row, _ = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("call", "tool", tool=broken_tool, args={"store": "north"}, approval="never"),
        node("out", "output", fields=[]),
    ))
    assert row.status == "failed"
    assert row.error_node_id == "call"
    _clear(row.error, broken_tool)


async def test_a_team_member_with_a_broken_tool_fails_that_step_with_the_reason(
    engine_up, broken_tool, monkeypatch,
):
    import json

    from langchain_core.messages import AIMessage

    from app.providers import mock_model

    routed: list[str] = []

    def _decide(self, messages):
        if self.response_format:   # 调度者：派一次，看到结果就收工
            plan = ({"assignments": [{"agent": "查数员", "instruction": "查门店"}]} if not routed
                    else {"assignments": [], "done": True, "reason": "查数员的工具坏了，先收工"})
            routed.append("x")
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        return AIMessage(content="北区 3 家")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    _, events = await _finish(chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("team", "supervisor", goal="查门店", max_rounds=2, approval="never", agents=[
            {"name": "查数员", "system": "你负责查", "tools": [broken_tool], "max_steps": 2}]),
        node("out", "output", fields=[]),
    ))
    step = next(e.data for e in events if e.type == "agent.step.end")
    assert step.get("failed") is True, step
    _clear(step.get("error"), broken_tool)


async def test_the_tool_library_marks_a_stored_broken_tool(client, broken_tool):
    listed = next(t for t in (await client.get("/api/custom-tools")).json() if t["name"] == broken_tool)
    assert listed["problem"] and "store" in listed["problem"]
    item = next(t for t in (await client.get("/api/tools")).json() if t["id"] == broken_tool)
    assert item["problem"] == listed["problem"]
    good = [t for t in (await client.get("/api/tools")).json() if t["source"] == "builtin"]
    assert good and all("problem" not in t for t in good), "内置工具不用带这个字段"


async def test_running_a_broken_tool_from_the_library_says_why(client, broken_tool):
    r = await client.post(f"/api/tools/{broken_tool}/run", json={"args": {"store": "north"},
                                                                 "confirm": True})
    body = r.json()
    assert body["ok"] is False
    _clear(body["error"], broken_tool)


async def test_a_broken_tool_does_not_take_its_neighbours_down(broken_tool):
    """同一个节点绑了好几个工具时，报错只点坏的那一个。"""
    from app.tools.registry import ToolBuildError, ToolContext, build_tools

    good = _name()
    async with SessionLocal() as session:
        session.add(CustomTool(name=good, kind="http", parameters=GOOD,
                               config={"url": "https://example.com/api"}))
        await session.commit()
    try:
        async with SessionLocal() as session:
            ctx = ToolContext(run_id="t", node_id="n")
            built = await build_tools([good], ctx, session=session)
            assert [t.name for t in built] == [good]
            with pytest.raises(ToolBuildError) as caught:
                await build_tools([good, broken_tool], ctx, session=session)
        assert broken_tool in str(caught.value) and good not in str(caught.value)
    finally:
        async with SessionLocal() as session:
            row = (await session.execute(select(CustomTool).where(CustomTool.name == good))).scalar_one()
            await session.delete(row)
            await session.commit()
