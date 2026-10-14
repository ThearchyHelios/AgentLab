"""助手的生成过程（总方案 L1，look 文档 D9）和五个入口怎么用查看。

模型一律用剧本，不调真接口。守的是：
- 读到 look 就停（look 之后的内容丢弃），查看结果作为下一条消息交回，接着写；消息结构照 look §2.5.5
- 接着写时已落地的操作不重复；每轮查看次数有上限；接着写那一段被截断照实报
- 修正请求带上这一轮的查看结果；模型问了用户（ask）这一轮就结束，不修正、不自动运行
- 画布 / 问数据：system 里只有概览；look 事件发给前端；非流式的 /generate 走同一个过程
- 发布前修复、升级证据：系统按要修的 SQL 代为查看（by=system），不调挑选模型
- 历史里写「上一轮查看过」；演示模型数据类请求看一次、闲聊不看
- 三种对象（表、能力、工作流版本）同一个接口；能力只给名字、参数、返回字段、读写、要不要确认；问数据只列只读能力
- 应用落地（AppApplier）的去重；/app-stream 先代为查看钉住的版本；/from-run 记下来源
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.look.base import LookBudget
from app.look.service import LookService
from app.main import app
from tests.fixtures.sources import drop_source


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def shop(tmp_path):
    """两张表的小库：orders、customers。"""
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, amount REAL NOT NULL,"
        " ordered_at TEXT NOT NULL);")
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=f"lookshop_{uuid.uuid4().hex[:6]}", kind="sqlite", database=str(path),
                         description="商城库")
        session.add(row)
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
    yield row
    await drop_source(row.id)


def _line(op: dict) -> str:
    return json.dumps(op, ensure_ascii=False)


class Turns:
    """按次吐操作流：第 n 次调用吐 scripts[n]（调用多于剧本时重复最后一份）；给了 fix，收到自查请求时吐它。
    记下每次收到的整串消息。"""

    def __init__(self, *scripts: list, fix: list | None = None) -> None:
        self.scripts = [[s if isinstance(s, AIMessageChunk) else _line(s) for s in script] for script in scripts]
        self.fix = [_line(o) for o in (fix or [{"op": "done"}])]
        self.calls: list[list] = []

    async def astream(self, messages):
        self.calls.append(list(messages))
        if "上线前自查" in messages[1][1]:
            lines = self.fix
        else:
            drafts = [m for m in self.calls if "上线前自查" not in m[1][1]]
            lines = self.scripts[min(len(drafts), len(self.scripts)) - 1]
        for line in lines:
            yield line if isinstance(line, AIMessageChunk) else AIMessageChunk(content=line + "\n")


def _use(monkeypatch, model) -> None:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)


async def _events(client, monkeypatch, model, body: dict, path: str = "/api/copilot/generate-stream") -> list[dict]:
    _use(monkeypatch, model)
    out: list[dict] = []
    async with client.stream("POST", path, json=body) as r:
        assert r.status_code == 200, await r.aread()
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def _ops(events: list[dict]) -> list[str]:
    return [e["op"] for e in events if e["op"] not in ("thinking", "heartbeat", "model")]


def _graph_ops(source: str, sql: str = "SELECT SUM(amount) AS 金额 FROM orders") -> list[dict]:
    return [
        {"op": "add_node", "node": {"id": "q", "type": "tool", "label": "查订单", "config": {
            "tool": f"db_query__{source}", "args": {"sql": sql}, "assign_to": "rows"}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "q"}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果", "config": {
            "fields": [{"name": "rows", "value": "{{ vars.rows }}"}]}}},
        {"op": "add_edge", "edge": {"source": "q", "target": "out"}},
        {"op": "done", "explanation": "查一下", "run": True},
    ]


START = {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}}


async def _tables(source) -> LookService:
    from app.look.tables import TableKind

    return LookService([await TableKind.load([source])])


# ---------------------------------------------------------------- 生成过程


async def test_reading_a_look_stops_the_stream_and_the_result_comes_back_as_the_next_message(shop):
    from app.api.copilot_gen import Generation, GraphApplier

    after_look = {"op": "add_node", "node": {"id": "ghost", "type": "llm", "label": "不该出现", "config": {}}}
    model = Turns(
        [{"op": "plan", "summary": "先看订单表"}, START,
         {"op": "look", "need": "订单金额", "targets": [{"kind": "table", "name": "orders"}]}, after_look],
        [START, {"op": "plan", "summary": "又一个开场"}, *_graph_ops(shop.name)],
    )
    nodes: dict = {}
    applier = GraphApplier(nodes, [])
    gen = Generation(model, system="SYS", applier=applier, looks=await _tables(shop))
    events = [op async for op in gen.draft("REQ")]

    assert len(model.calls) == 2
    system, human, ai, result = model.calls[1]
    assert system == ("system", "SYS") and human == ("human", "REQ")
    # 模型到 look 那一行为止的输出；look 之后的内容丢掉
    assert ai[0] == "ai" and ai[1].splitlines() == [_line({"op": "plan", "summary": "先看订单表"}), _line(START),
                                                     _line({"op": "look", "need": "订单金额",
                                                            "targets": [{"kind": "table", "name": "orders"}]})]
    assert "ghost" not in nodes and "ghost" not in ai[1]
    assert result[0] == "human" and result[1].startswith("查看结果（第 1 次，这一轮最多 3 次）")
    assert "amount  REAL" in result[1]
    assert "接着输出操作。已经输出、已经落地的操作不要重复（已落地：start）" in result[1]

    kinds = [e["op"] for e in events]
    # 已落地的 start 不重复，第二个 plan 不要；查看前补一拍「正在查看」，查看完发 look 事件
    assert kinds == ["plan", "add_node", "heartbeat", "look", "add_node", "add_edge", "add_node", "add_edge", "done"]
    assert events[2]["phase"] == "looking"
    look = events[3]
    assert look["by"] == "model" and look["objects"][0]["named"] == ["orders"]
    assert set(nodes) == {"start", "q", "out"}


async def test_landed_edges_and_nodes_are_not_forwarded_twice(shop):
    from app.api.copilot_gen import Generation, GraphApplier

    edge = {"op": "add_edge", "edge": {"source": "start", "target": "q"}}
    q = _graph_ops(shop.name)[0]
    model = Turns(
        [START, q, edge, {"op": "look", "need": "订单", "targets": [{"kind": "table", "name": "orders"}]}],
        [START, q, edge, {**q, "node": {**q["node"], "label": "查订单金额"}}, {"op": "done"}],
    )
    nodes: dict = {}
    edges: list = []
    gen = Generation(model, system="S", applier=GraphApplier(nodes, edges), looks=await _tables(shop))
    events = [e for e in [op async for op in gen.draft("R")] if e["op"] not in ("heartbeat", "look")]
    # 一模一样的节点、连线不转发也不重复追加；改了标签的同 id 节点照旧覆盖、转发
    assert [e["op"] for e in events] == ["add_node", "add_node", "add_edge", "add_node", "done"]
    assert events[3]["node"]["label"] == "查订单金额" and nodes["q"]["data"]["label"] == "查订单金额"
    assert edges == [{"source": "start", "target": "q", "sourceHandle": None}]


async def test_looks_per_round_are_capped(shop):
    from app.api.copilot_gen import Generation, GraphApplier

    model = Turns(*[[{"op": "look", "need": f"第 {i} 次", "targets": [{"kind": "table", "name": t}]}]
                    for i, t in enumerate(["orders", "customers", "orders", "customers", "orders", "customers"])])
    looks = await _tables(shop)
    gen = Generation(model, system="S", applier=GraphApplier({}, []), looks=looks)
    [op async for op in gen.draft("R")]
    # 3 次查看（第 3 次是重复的那一批，短路但算次数），之后回「用完了」；再要一次就不再接着写
    assert len(looks.records) == 2
    assert "查看次数已用完" in model.calls[4][-1][1]
    assert len(model.calls) == 5


async def test_a_cut_in_the_continuation_is_still_an_error(shop):
    from app.api.copilot_gen import CopilotIncomplete, Generation, GraphApplier

    cut = AIMessageChunk(content="", response_metadata={"finish_reason": "length"})
    model = Turns([{"op": "look", "need": "订单", "targets": [{"kind": "table", "name": "orders"}]}],
                  [START, cut])
    gen = Generation(model, system="S", applier=GraphApplier({}, []), looks=await _tables(shop))
    with pytest.raises(CopilotIncomplete):
        [op async for op in gen.draft("R")]


async def test_the_contract_generate_runs_draft_then_repair(shop):
    """附录 C 的 generate(system, request, applier, checker, looks)：起草 → 自查修正，修正请求带上查看结果。"""
    from app.api.copilot_gen import GraphApplier, GraphChecker, generate

    model = Turns([{"op": "look", "need": "订单", "targets": [{"kind": "table", "name": "orders"}]}],
                  [START, *_graph_ops(shop.name, "SELECT o.no_such_col FROM orders o")],
                  fix=[{"op": "update_node", "id": "q", "config": {"args": {"sql": "SELECT o.amount FROM orders o"}}},
                       {"op": "done"}])
    applier = GraphApplier({}, [])
    looks = await _tables(shop)
    events = [op async for op in generate(model, "S", "R", applier, GraphChecker(applier, sources=[shop]), looks)]
    assert [e["status"] for e in events if e["op"] == "check"] == ["repairing"]
    repair = model.calls[-1][1][1]
    assert "上线前自查" in repair and "这一轮查看过的结构" in repair and "amount  REAL" in repair
    assert applier.nodes["q"]["data"]["config"]["args"]["sql"] == "SELECT o.amount FROM orders o"


# ---------------------------------------------------------------- 画布、问数据


async def test_the_stream_sends_look_events_and_the_system_only_has_the_overview(client, monkeypatch, shop):
    model = Turns([{"op": "plan", "summary": "看表"}, {"op": "look", "need": "订单金额",
                                                       "targets": [{"kind": "table", "name": "orders"}]}],
                  [START, *_graph_ops(shop.name)])
    events = await _events(client, monkeypatch, model, {
        "instruction": "订单总金额", "intent": "answer", "datasource_ids": [shop.id]})
    kinds = _ops(events)
    assert kinds.index("look") < kinds.index("add_node") and events[-1]["op"] == "final"
    look = next(e for e in events if e["op"] == "look")
    assert look["status"] == "ok" and look["objects"][0]["source"] == shop.name
    # system：每个库一行概览，工具清单不列表名，不带字段；查看的用法和「什么时候该看」都在
    system = model.calls[0][0][1]
    assert f"- {shop.name}（sqlite，只读）：商城库｜共 2 张表，还没有数据目录" in system
    assert "amount  REAL" not in system and "可查：" not in system
    assert f"db_query__{shop.name}（数据库）：在「{shop.name}」上执行 SQL。商城库 共 2 张表，字段要用时 look" in system
    assert '"op":"look"' in system and "什么时候该看" in system


async def test_saying_hello_calls_the_model_once_and_looks_at_nothing(client, monkeypatch, shop):
    model = Turns([{"op": "reply", "text": "你好"}])
    events = await _events(client, monkeypatch, model, {"instruction": "你好", "intent": "answer"})
    assert _ops(events) == ["reply"] and len(model.calls) == 1


async def test_ask_ends_the_round(client, monkeypatch, shop):
    """模型问了用户：这一轮到此为止，不自查修正、不自动运行；已经落地的改动照常交付。"""
    ask = {"op": "ask", "question": "按下单时间还是付款时间算？"}
    model = Turns([START, _graph_ops(shop.name, "SELECT o.no_such_col FROM orders o")[0], ask,
                   {"op": "add_node", "node": {"id": "late", "type": "llm", "label": "不该出现", "config": {}}}])
    events = await _events(client, monkeypatch, model, {"instruction": "订单金额", "datasource_ids": [shop.id]})
    kinds = _ops(events)
    assert "ask" in kinds and "late" not in json.dumps(events, ensure_ascii=False)
    assert not any(e["op"] == "check" and e.get("status") == "repairing" for e in events)
    assert len(model.calls) == 1
    final = events[-1]
    assert final["op"] == "final" and final["autorun"] is False
    assert {n["id"] for n in final["graph"]["nodes"]} == {"start", "q"}

    # 什么都还没改就问：问题转出去，这一轮就结束
    events = await _events(client, monkeypatch, Turns([ask]), {"instruction": "订单金额"})
    assert _ops(events) == ["ask"]
    # 非流式的 /generate：问题放在说明里，原图照旧
    r = await client.post("/api/copilot/generate", json={"instruction": "订单金额"})
    assert r.status_code == 200, r.text
    assert r.json()["explanation"] == ask["question"] and r.json()["graph"]["nodes"] == []


async def test_the_one_shot_generate_looks_too(client, monkeypatch, shop):
    model = Turns([{"op": "look", "need": "订单", "targets": [{"kind": "table", "name": "orders"}]}],
                  [START, *_graph_ops(shop.name)])
    _use(monkeypatch, model)
    r = await client.post("/api/copilot/generate", json={"instruction": "订单总金额", "datasource_ids": [shop.id]})
    assert r.status_code == 200, r.text
    assert len(model.calls) == 2 and "amount  REAL" in model.calls[1][-1][1]
    assert {n["id"] for n in r.json()["graph"]["nodes"]} == {"start", "q", "out"}


async def test_history_says_what_was_looked_at_last_time(client, monkeypatch, shop):
    from app.db.models import Conversation, ConversationTurn

    async with SessionLocal() as session:
        conv = Conversation(title="看过的表")
        session.add(conv)
        await session.flush()
        session.add(ConversationTurn(conversation_id=conv.id, seq=1, question="订单总额", answer="一共 3 笔",
                                     status="done", meta={"looks": [{"op": "look", "id": "L1", "by": "model",
                                                                      "need": "订单", "status": "ok", "objects": [
                                         {"kind": "table", "source": shop.name, "named": ["orders"],
                                          "picked": [], "joined": ["customers"], "missing": []},
                                         {"kind": "capability", "named": ["db_query__x"], "missing": []}]}]}))
        await session.commit()
        conv_id = conv.id
    model = Turns([{"op": "reply", "text": "好"}])
    await _events(client, monkeypatch, model, {"instruction": "那上个月呢", "intent": "answer",
                                               "conversation_id": conv_id})
    human = model.calls[0][1][1]
    assert f"上一轮查看过：{shop.name} 的 orders、customers；能力 db_query__x（要再看时直接点名）" in human


async def test_the_demo_model_looks_once_for_a_data_question_and_not_for_hello(client, monkeypatch, shop):
    from app.providers.mock_model import MockChatModel

    _use_demo = MockChatModel(model_name="mock-fast", latency=0)
    events = await _events(client, monkeypatch, _use_demo, {
        "instruction": "各客户的订单金额", "intent": "answer", "datasource_ids": [shop.id]})
    kinds = _ops(events)
    assert kinds.count("look") == 1 and kinds[-1] == "final", kinds
    look = next(e for e in events if e["op"] == "look")
    assert look["status"] == "ok" and look["objects"][0]["picked"] == ["customers", "orders"]
    agent = next(n for n in events[-1]["graph"]["nodes"] if n["type"] == "agent")
    assert agent["data"]["config"]["tools"] == [f"db_query__{shop.name}"]

    events = await _events(client, monkeypatch, _use_demo, {"instruction": "你好", "intent": "answer"})
    assert _ops(events) == ["reply"]


# ---------------------------------------------------------------- 发布前修复、升级证据：系统代为查看


def _node(nid, ntype, label, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label, "config": config}}


async def test_publish_fix_looks_on_behalf_of_the_model(monkeypatch, shop):
    import app.api.copilot as copilot
    import app.look.pick as pick

    async def _no_picker(*_a, **_k):
        raise AssertionError("系统代为查看是点名，不该调挑选模型")

    monkeypatch.setattr(pick, "get_chat_model", _no_picker)
    model = Turns([{"op": "plan", "summary": "看一下"}, {"op": "done", "explanation": "无法修改"}])
    _use(monkeypatch, model)
    graph = {"nodes": [
        _node("start", "input", "周期"),
        _node("fetch", "tool", "取数", tool=f"db_query__{shop.name}", args={"sql": "SELECT SUM(amount) AS gmv FROM orders"}),
        _node("write", "report", "报告撰写", instructions="写周报", numbers="strict", on_violation="fail"),
        _node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        _node("raw", "output", "原始数", fields=[{"name": "数", "value": "{{ nodes.fetch }}"}]),
    ], "edges": [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
        [("start", "fetch"), ("fetch", "write"), ("write", "done"), ("fetch", "raw")])]}
    async with SessionLocal() as session:
        out = await copilot.assist_publish_fix(session, graph, level="governed")
    human = model.calls[0][1][1]
    assert "发布前检查" in human and "系统已代为查看（不占你的查看次数）" in human and "amount  REAL" in human
    (look,) = out["looks"]
    assert look["by"] == "system" and look["objects"][0]["named"] == ["orders"]


async def test_upgrade_looks_on_behalf_of_the_model(monkeypatch, shop):
    import app.api.copilot as copilot

    model = Turns([{"op": "plan", "summary": "看一下"}, {"op": "done", "explanation": "不改"}])
    _use(monkeypatch, model)
    graph = {"nodes": [
        _node("start", "input", "输入"),
        _node("fetch", "tool", "查库", tool=f"db_query__{shop.name}",
              args={"sql": "SELECT SUM(amount) AS a, COUNT(*) AS b FROM orders"}),
        _node("calc", "code", "算比率", language="python", assign_to="calc",
              code="import json\nprint(json.dumps({'ratio': 3 / 4}))"),
        _node("card", "metrics", "口径卡", metrics=[{"id": "ratio", "name": "客单价", "expression": "vars.calc.ratio"}]),
        _node("write", "report", "报告撰写", instructions="写一句话"),
        _node("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ], "edges": [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
        [("start", "fetch"), ("fetch", "calc"), ("calc", "card"), ("card", "write"), ("write", "out")])]}
    async with SessionLocal() as session:
        out = await copilot.assist_upgrade(session, graph, level="published")
    human = model.calls[0][1][1]
    assert "系统已代为查看" in human and "ordered_at  TEXT" in human
    assert out["looks"][0]["by"] == "system" and out["looks"][0]["need"].startswith("要升级的工作流")


# ---------------------------------------------------------------- 三种对象，同一个接口


def _capability(name: str, *, effect: str = "read", **extra):
    from app.look.capabilities import CapabilityView

    return CapabilityView(name=name, system=extra.pop("system", "builtin"), title=extra.pop("title", name),
                          description=extra.pop("description", ""),
                          params_schema=extra.pop("params_schema", {}), returns_schema=extra.pop("returns_schema", None),
                          tabular=extra.pop("tabular", None), effect=effect, approval=extra.pop("approval", "inherit"),
                          fields=extra.pop("fields", {}), masked=tuple(extra.pop("masked", ())),
                          version=extra.pop("version", 1))


def _versions(monkeypatch):
    from app.apps import describe
    from app.look.versions import VersionKind, VersionView

    monkeypatch.setattr(describe, "describe_version",
                        lambda v, *, workflow_name: f"入口字段：period\n口径卡：gmv（{workflow_name} v{v.version}）")
    v3 = SimpleNamespace(version=3, level="governed", graph_hash="3fa2c1d0aa")
    v4 = SimpleNamespace(version=4, level="published", graph_hash="77aa0000bb")
    return VersionKind([VersionView("wf_1", "周销售", "每周的销售", v3), VersionView("wf_1", "周销售", "每周的销售", v4)])


async def test_three_kinds_share_one_interface(monkeypatch, shop):
    from app.look.capabilities import CapabilityKind
    from app.look.tables import TableKind

    kinds = [await TableKind.load([shop]), CapabilityKind([_capability("web_search"), _capability("send_mail")]),
             _versions(monkeypatch)]
    named = {"table": {"kind": "table", "name": "orders"}, "capability": {"kind": "capability", "name": "web_search"},
             "workflow_version": {"kind": "workflow_version", "source": "周销售", "name": "v3"}}
    for kind in kinds:
        assert isinstance(kind.kind, str) and isinstance(kind.label, str)
        overview = await kind.overview()
        assert isinstance(overview, list) and overview and all(isinstance(line, str) for line in overview)
        assert isinstance(await kind.index(None), str)
        found, missing = await kind.resolve([named[kind.kind], {"kind": kind.kind, "name": "nope"}])
        assert len(found) == 1 and missing == [{"kind": kind.kind, "name": "nope"}]
        picked, reason = await kind.pick("随便", None)
        assert picked and reason is None
        text, blocks = await kind.detail(found, 12_000, missing=missing)
        assert isinstance(text, str) and text and blocks and all(b["kind"] == kind.kind for b in blocks)
        assert all(isinstance(kind.key(o), str) for o in found)

    looks = LookService(kinds)
    result = await looks.look({"op": "look", "need": "都看看", "targets": list(named.values())}, LookBudget())
    assert [o["kind"] for o in result.record["objects"]] == ["table", "capability", "workflow_version"]
    assert result.record["objects"][2]["named"] == ["周销售 v3"]
    assert "周销售 v3（受管级，哈希 3fa2c1d0）" in result.text and "口径卡：gmv（周销售 v3）" in result.text
    # 只写 kind 不写 name：按需求在这一类里挑；工作流版本按每个工作流的最新已发布版本挑
    result = await looks.look({"op": "look", "need": "周销售", "targets": [{"kind": "workflow_version"}]}, LookBudget())
    assert result.record["objects"][0]["picked"] == ["周销售 v4"]
    # 这一轮不能看的类别照实说
    result = await looks.look({"op": "look", "need": "口径", "targets": [{"kind": "caliber", "name": "x"}]},
                              LookBudget())
    assert "这一轮不能查看这一类对象：caliber" in result.text


async def test_capabilities_show_shape_not_secrets_and_answering_lists_only_reads():
    from app.db.models import Capability
    from app.look.capabilities import CapabilityKind

    names = [f"look_cap_{uuid.uuid4().hex[:6]}" for _ in range(2)]
    async with SessionLocal() as session:
        session.add(Capability(
            name=names[0], system="erp", title="查额度", description="按部门查剩余额度", effect_system="read",
            params_schema={"type": "object", "required": ["dept"], "properties": {
                "dept": {"type": "string", "description": "部门编码", "examples": ["D-0001"]}}},
            tabular={"rows": "/data", "columns": {"部门": "/dept", "负责人电话": "/phone"}},
            fields={"/dept": {"label": {"value": "部门编码", "source": "human", "status": "confirmed"}}},
            masked=["/phone"], endpoint_id="ep_secret_host", version=3))
        session.add(Capability(name=names[1], system="erp", title="付款", effect_system="write", approval="confirm",
                               endpoint_id="ep_secret_host"))
        await session.commit()
    try:
        kind = await CapabilityKind.load()
        looks = LookService([kind])
        result = await looks.look({"op": "look", "need": "额度和付款", "targets": [
            {"kind": "capability", "name": n} for n in names]}, LookBudget())
        text = result.text
        assert "读写：只读；确认：跟随工具页的信任设置" in text and "dept（string，必填）：部门编码" in text
        assert "部门（部门编码）" in text and "负责人电话（遮罩，只给字段名）" in text
        assert "读写：写入；确认：每次都要人确认" in text and "只能放在调用工具节点里" in text
        # 不给地址、凭据、示例值
        assert "ep_secret_host" not in text and "D-0001" not in text
        assert result.record["objects"][0]["versions"][names[0]] == 3

        # 问数据（意图为回答）只列只读能力：写入能力不在概览里，点名也按没找到回
        answering = LookService([await CapabilityKind.load(read_only=True)])
        assert "写入" not in await answering.overview()
        result = await answering.look({"op": "look", "need": "付款", "targets": [
            {"kind": "capability", "name": names[1]}]}, LookBudget())
        assert result.record["objects"][0]["missing"] == [names[1]] and "问数据时只能查看只读能力" in result.text
    finally:
        async with SessionLocal() as session:
            for n in names:
                await session.delete(await session.get(Capability, n))
            await session.commit()


# ---------------------------------------------------------------- 应用润色


def _fake_app_ops(monkeypatch) -> list:
    """应用操作的替身（BE-3 的 apply_app_op 还没接上时）：app_add 加到页面末尾，app_update 合并 props。"""
    from app.apps import ops
    from app.api.copilot_gen import app_component
    from app.look.base import Applied

    calls: list = []

    def apply(spec, op):
        calls.append(op)
        if op["op"] == "app_add":
            page = next(p for p in spec["pages"] if p["id"] == op["parent"])
            page["body"].append(op["component"])
            return Applied(True)
        if op["op"] == "app_update":
            have = app_component(spec, op["id"])
            if have is None:
                return Applied(False, False, "没有这个组件")
            have["props"] = {**have.get("props", {}), **op.get("props", {})}
            return Applied(True)
        return Applied(False, False, "认不出")

    monkeypatch.setattr(ops, "apply_app_op", apply)
    return calls


def test_app_applier_dedupes_when_continuing(monkeypatch):
    from app.api.copilot_gen import AppApplier

    calls = _fake_app_ops(monkeypatch)
    spec = {"pages": [{"id": "home", "title": "首页", "body": [{"id": "gmv", "type": "metric", "props": {}}]}]}
    applier = AppApplier(spec)
    add = {"op": "app_add", "parent": "home", "index": 1, "component": {"id": "note", "type": "text", "props": {}}}
    upd = {"op": "app_update", "id": "gmv", "props": {"label": "销售额"}}
    assert applier.apply({"op": "plan", "summary": "改标题"}).forward
    assert applier.apply(add).changed and applier.apply(upd).changed
    assert applier.landed() == ["note", "gmv"]
    applier.continuing()
    # 接着写：重发的同一个操作、一模一样已经在 spec 里的组件、第二个 plan 都不再落地也不转发
    assert applier.apply(upd).forward is False
    assert applier.apply({**add, "index": 0}).forward is False
    assert applier.apply({"op": "plan", "summary": "又一个开场"}).forward is False
    assert len(calls) == 2
    # 图操作不进应用
    assert applier.apply({"op": "add_node", "node": {"id": "x", "type": "llm"}}).forward is False


async def test_app_stream_looks_at_the_pinned_version_first(client, monkeypatch):
    from app.apps import derive, lint, versions
    from app.db.models import Workflow, WorkflowVersion

    _fake_app_ops(monkeypatch)
    async with SessionLocal() as session:
        wf = Workflow(name=f"周销售_{uuid.uuid4().hex[:4]}", graph={"nodes": [], "edges": []})
        session.add(wf)
        await session.flush()
        v1 = WorkflowVersion(workflow_id=wf.id, version=1, graph={"nodes": [], "edges": []}, graph_hash="h" * 64,
                             level="published")
        session.add(v1)
        await session.commit()
        wf_id, wf_name = wf.id, wf.name

    async def _published(session, workflow_id):
        from sqlalchemy import select

        return list((await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id))).scalars()) if workflow_id == wf_id else []

    async def _derive(session, workflow_id, version, *, name=None):
        return {"format": "agentlab.app/1", "name": "派生", "pages": [{"id": "home", "title": "首页", "body": [
            {"id": "gmv", "type": "metric", "bind": {"view": "wk", "metric": "kpi.gmv"}}]}]}

    async def _lint(session, spec, bindings, **_):
        return [{"level": "error", "code": "app.slot_unbound", "message": "岗位「财务」还没有绑定", "path": "slots"}]

    monkeypatch.setattr(versions, "published_versions", _published)
    monkeypatch.setattr(derive, "derive_app", _derive)
    monkeypatch.setattr(lint, "lint_app", _lint)
    from app.apps import describe

    monkeypatch.setattr(describe, "describe_version", lambda v, *, workflow_name: "口径卡：kpi.gmv 销售额")
    spec = {"format": "agentlab.app/1", "name": "看板", "slots": {"finance": {"label": "财务"}},
            "pins": [{"alias": "wk", "workflow_id": wf_id, "version": 1, "graph_hash": "h" * 64, "use": "view"}],
            "pages": [{"id": "home", "title": "首页", "body": [{"id": "gmv", "type": "metric", "props": {}}]}]}
    model = Turns([{"op": "plan", "summary": "改标题"}, {"op": "app_update", "id": "gmv", "props": {"label": "销售额"}},
                   {"op": "done", "explanation": "改了指标的标题；岗位「财务」等着绑定"}])
    try:
        events = await _events(client, monkeypatch, model, {"instruction": "指标标题写成销售额", "spec": spec},
                               path="/api/copilot/app-stream")
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Workflow, wf_id))
            await session.commit()
    kinds = _ops(events)
    assert kinds == ["look", "plan", "app_update", "done", "check", "final"], kinds
    look = events[1]
    assert look["by"] == "system" and look["objects"][0]["named"] == [f"{wf_name} v1"]
    system, human = model.calls[0][0][1], model.calls[0][1][1]
    assert "指标块" in system and "kpi.gmv" in system and "app_update" in system
    assert "系统已代为查看" in human and "口径卡：kpi.gmv 销售额" in human
    final = events[-1]
    assert final["spec"]["pages"][0]["body"][0]["props"] == {"label": "销售额"}
    # 只有人能处理的问题（岗位没绑）不交回模型改，照常列在交付的问题里
    assert final["issues"][0]["code"] == "app.slot_unbound"
    assert next(e for e in events if e["op"] == "check")["status"] == "passed"


# ---------------------------------------------------------------- 从运行提取


async def test_from_run_records_where_the_workflow_came_from(client):
    from app.db.models import Run, RunEvent, Workflow

    graph = {"nodes": [{"id": "a", "type": "input", "data": {"label": "入口", "config": {}}}], "edges": []}
    async with SessionLocal() as session:
        run = Run(workflow_name="提取", status="succeeded", graph=graph)
        session.add(run)
        await session.flush()
        session.add(RunEvent(run_id=run.id, seq=1, type="node.started", node_id="a", ts=0.0, data={}))
        await session.commit()
        run_id = run.id
    r = await client.post("/api/copilot/from-run", json={"run_id": run_id, "conversation_id": "c1", "turn_id": "t1"})
    assert r.status_code == 200, r.text
    async with SessionLocal() as session:
        wf = await session.get(Workflow, r.json()["workflow_id"])
        assert wf.origin == {"run_id": run_id, "conversation_id": "c1", "turn_id": "t1"}
    await client.delete(f"/api/workflows/{r.json()['workflow_id']}")
