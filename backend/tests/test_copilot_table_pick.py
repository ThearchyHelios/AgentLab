"""助手建图时按需求挑表、组装数据源上下文（数据目录阶段 1B）。

背景：大库（几十上百张表）超出详细摘要的阈值时，以前只给模型看表名，助手一个字段都看不到，只能照着
名字编字段、猜怎么连表。现在先用一次轻量的模型调用按需求挑出相关的表，再沿关系图补上连接用的中间表，
挑中的表给全部字段、数据目录和可以照抄的连接条件，其余表只列名字。

这组守的是：
- 挑表、补路径、组装的结果（合成景区库：入园记录和渠道之间要经关联表 channel_visits）
- 挑表失败（报错、超时、返回不存在的表、返回为空）一律退回只列表名，生成照常进行
- 小库保持详细模式，额外带上目录
- 流式输出里有 context 操作；自查修正沿用同一份上下文，不再重新挑表
- 非流式生成、发布前修复、升级共用同一套组装
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog
from app.data.catalog import INFERRED_MARK, make_item
from app.data.engine import engines
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.main import app
from tests.fixtures.catalog import scenic


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _add_source(database: str, *, prefix: str = "p1b") -> DataSource:
    async with SessionLocal() as session:
        row = DataSource(name=f"{prefix}_{uuid.uuid4().hex[:8]}", kind="sqlite", database=database,
                         description="景区业务库")
        session.add(row)
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
        return row


async def _drop_source(row: DataSource) -> None:
    await engines.invalidate(row.id)
    async with SessionLocal() as session:
        live = await session.get(DataSource, row.id)
        if live is not None:
            await session.delete(live)
            await session.commit()


@pytest.fixture
async def scenic_source(scenic_db):
    """接上合成景区库（50 张表 + 1 个视图），用完删掉：留着会让同一进程里别的测试也进入大库模式。"""
    row = await _add_source(scenic_db)
    yield row
    await _drop_source(row)


@pytest.fixture
async def small_source(tmp_path):
    """两张表的小库：orders.customer_id 只能靠命名推断连到 customers。"""
    path = tmp_path / "small.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, amount REAL NOT NULL,"
        " ordered_at TEXT NOT NULL);")
    db.commit()
    db.close()
    row = await _add_source(str(path), prefix="p1b_small")
    yield row
    await _drop_source(row)


class Picker:
    """挑表用的假模型：结构化输出返回自己，ainvoke 记下收到的提示词，按设定回复、报错或拖延。"""

    def __init__(self, reply=None, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.reply, self.error, self.delay = reply, error, delay
        self.calls: list[str] = []

    def with_structured_output(self, schema, **_):
        assert schema["title"]
        return self

    async def ainvoke(self, messages):
        self.calls.append("\n".join(text for _role, text in messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.reply


def _use_picker(monkeypatch, picker: Picker) -> list:
    """挑表模型换成假的，返回每次拿模型时的规格（核对思考关、额度小）。"""
    import app.api.copilot_context as copilot_context

    specs: list = []

    async def _model(_session, spec):
        specs.append(spec)
        return picker, "fake-pick"

    monkeypatch.setattr(copilot_context, "get_chat_model", _model)
    return specs


class Scripted:
    """按剧本吐操作流的建图模型：第一轮吐 first，收到自查请求时吐 fix。记下每次的 system 和 human。"""

    def __init__(self, first: list[dict], fix: list[dict] | None = None) -> None:
        self.first = [json.dumps(o, ensure_ascii=False) for o in first]
        self.fix = [json.dumps(o, ensure_ascii=False) for o in (fix or [{"op": "done"}])]
        self.systems: list[str] = []
        self.humans: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        self.systems.append(next(t for r, t in messages if r == "system"))
        human = next(t for r, t in messages if r == "human")
        self.humans.append(human)
        for line in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=line + "\n")


def _graph_using(tool: str) -> list[dict]:
    return [
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
        {"op": "add_node", "node": {"id": "ask", "type": "agent", "label": "查库", "config": {
            "prompt": "{{ input.question }}", "tools": [tool], "assign_to": "answer"}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "ask"}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果", "config": {
            "fields": [{"name": "answer", "value": "{{ vars.answer }}"}]}}},
        {"op": "add_edge", "edge": {"source": "ask", "target": "out"}},
        {"op": "done", "explanation": "查一下", "run": True},
    ]


async def _stream(client, monkeypatch, model, body: dict) -> list[dict]:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream", json=body) as r:
        assert r.status_code == 200, await r.aread()
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


#: 需求和假模型挑出的表：入园记录、渠道。两者之间没有直接关联，要经关联表 channel_visits
NEED = "各渠道上月的入园人次"
PICKED = {"tables": [{"table": "visits"}, {"table": "channels"}]}
#: 三张表各自独有的字段（describe_table 的写法：列名两个空格类型）
SELECTED_COLUMNS = ("visitor_count  INTEGER", "visit_time  TEXT", "ticket_no  TEXT", "channel_type  TEXT",
                    "channel_code  TEXT", "attributed_share  REAL")
#: 没挑中的表独有的字段：不该出现在提示词里
OTHER_COLUMNS = ("store_code", "sku", "on_hand", "refund_amount")
JOINS = ("channel_visits.visit_id = visits.id", "channel_visits.channel_id = channels.id")


def _context_op(events: list[dict]) -> dict:
    found = [e for e in events if e.get("op") == "context"]
    assert len(found) == 1, [e.get("op") for e in events]
    return found[0]


# ---------------------------------------------------------------- 组装


async def test_pick_brings_in_the_bridge_table_and_spells_out_fields_and_joins(scenic_source):
    from app.api import copilot_context

    picker = Picker(PICKED)
    plan = await copilot_context.plan_context([scenic_source])
    assert plan.needs_pick
    ctx = await plan.resolve(picker, need=NEED)
    text = ctx.section()

    assert len(picker.calls) == 1
    # 挑表的输入是需求加紧凑表目录（每张表一行），不是全部字段
    assert NEED in picker.calls[0] and "channel_visits" in picker.calls[0]
    assert "attributed_share" not in picker.calls[0]

    (source,) = ctx.sources
    assert source.selected_by == "model"
    assert source.tables == ["visits", "channels", "channel_visits"]
    for col in SELECTED_COLUMNS:
        assert col in text, col
    for col in OTHER_COLUMNS:
        assert col not in text, col
    for cond in JOINS:
        assert cond in text, cond
    # 其余表只给名字，让模型知道还有别的表
    assert "stores" in text and "parks" in text and "v_daily_visits" in text
    # 写 SQL 的硬性要求保留
    assert "写 SQL 的硬性要求" in text and "FETCH FIRST n ROWS ONLY" in text


async def test_inferred_relations_are_marked_and_catalog_is_attached(scenic_source):
    """visits.gate_id → gates 没有外键约束，是按列名推断的，连接条件要标出来；目录里推断的项同样要标。"""
    from app.api import copilot_context

    async with SessionLocal() as session:
        await catalog.write_entry(session, scenic_source.id, "visits", {
            "label": make_item("入园记录", "llm"), "grain": make_item("每张门票每次检票一行", "human")},
            if_version=0, actor="王敏")
    plan = await copilot_context.plan_context([scenic_source])
    ctx = await plan.resolve(Picker({"tables": [{"table": "visits"}, {"table": "gates"}]}), need="各入口闸机的入园人次")
    text = ctx.section()
    assert f"visits.gate_id = gates.id（多对一）{INFERRED_MARK}" in text
    assert f"中文名：入园记录{INFERRED_MARK}" in text
    assert "粒度：每张门票每次检票一行" in text and f"每张门票每次检票一行{INFERRED_MARK}" not in text


async def test_rejected_relations_are_not_used_for_join_paths(scenic_source):
    """人工驳回的关系不能拿来补路径：驳回 channel_visits → visits 后，关联表不会因为补路径被带进来。"""
    from app.api import copilot_context

    rels = [dict(r, status="rejected") if r["to_table"] == "visits" else r
            for r in catalog.fk_relations(scenic_source.schema_cache, "channel_visits")]
    async with SessionLocal() as session:
        await catalog.write_entry(session, scenic_source.id, "channel_visits", {"relations": rels},
                                  if_version=0, actor="王敏")
    plan = await copilot_context.plan_context([scenic_source])
    ctx = await plan.resolve(Picker(PICKED), need=NEED)
    assert ctx.sources[0].tables == ["visits", "channels"]
    assert "channel_visits.visit_id = visits.id" not in ctx.section()


async def test_picks_are_capped_and_paths_respect_the_total_limit(scenic_source):
    from app.api import copilot_context

    names = list(scenic_source.schema_cache["tables"])
    plan = await copilot_context.plan_context([scenic_source])
    ctx = await plan.resolve(Picker({"tables": [{"table": n} for n in reversed(names)]}), need="全部都要")
    # 挑中的按模型给的顺序截到上限；补中间表之后合计也不超过总上限
    assert ctx.sources[0].picked == list(reversed(names))[:copilot_context.PICK_LIMIT]
    assert ctx.sources[0].tables[:copilot_context.PICK_LIMIT] == ctx.sources[0].picked
    assert len(ctx.sources[0].tables) <= copilot_context.CONTEXT_TABLE_LIMIT

    # 两两都要补中间表的一组：总数不超过上限
    spread = ["reviews", "channels", "stores", "tour_groups", "coupons", "products"]
    ctx = await plan.resolve(Picker({"tables": [{"table": n} for n in spread]}), need="x")
    assert set(spread) <= set(ctx.sources[0].tables)
    assert len(ctx.sources[0].tables) <= copilot_context.CONTEXT_TABLE_LIMIT


async def test_tables_used_by_the_current_graph_are_always_included(scenic_source):
    """改图、追问时现有工作流已经查了哪些表是确定的事实：挑表成功时并进来，挑表失败时也给它们的字段。"""
    from app.api import copilot_context

    graph = {"nodes": [{"id": "q", "type": "tool", "data": {"label": "查门店", "config": {
        "tool": f"db_query__{scenic_source.name}", "args": {"sql": "SELECT s.name FROM stores s"}}}}], "edges": []}
    plan = await copilot_context.plan_context([scenic_source], graph=graph)
    picker = Picker({"tables": [{"table": "visits"}]})
    ctx = await plan.resolve(picker, need="再按入园人次排一下")
    assert ctx.sources[0].tables[:2] == ["stores", "visits"]
    assert "当前工作流已用到" in picker.calls[0] and "stores" in picker.calls[0]

    failed = await plan.resolve(Picker(error=RuntimeError("网关超时")), need="再按入园人次排一下")
    assert failed.sources[0].selected_by == "fallback"
    assert failed.sources[0].tables == ["stores"]
    assert "store_code  TEXT" in failed.section()


async def test_several_sources_pick_with_source_names(scenic_db, tmp_path):
    from app.api import copilot_context

    a = await _add_source(scenic_db)
    b = await _add_source(str(scenic.build(tmp_path / "b.db")))
    try:
        plan = await copilot_context.plan_context([a, b])
        picker = Picker({"tables": [{"source": a.name, "table": "visits"}, {"source": b.name, "table": "orders"},
                                    f"{b.name}.payments", {"source": "没有这个库", "table": "refunds"}]})
        ctx = await plan.resolve(picker, need="订单和入园")
        assert f"数据源「{a.name}」" in picker.calls[0] and f"数据源「{b.name}」" in picker.calls[0]
        by_name = {s.source.name: s for s in ctx.sources}
        assert by_name[a.name].tables == ["visits"]
        assert by_name[b.name].tables == ["orders", "payments"]
        op = ctx.op()
        assert [s["source"] for s in op["sources"]] == [a.name, b.name]
        assert all(s["selected_by"] == "model" for s in op["sources"])
    finally:
        await _drop_source(a)
        await _drop_source(b)


async def test_small_source_keeps_detail_and_carries_the_catalog(small_source):
    from app.api import copilot_context

    async with SessionLocal() as session:
        await catalog.write_entry(session, small_source.id, "orders", {
            "label": make_item("订单", "llm"),
            "columns": {"amount": {"unit": make_item("元", "human")}}}, if_version=0, actor="王敏")
    picker = Picker(error=AssertionError("小库不该挑表"))
    plan = await copilot_context.plan_context([small_source])
    assert not plan.needs_pick
    ctx = await plan.resolve(picker, need="订单总额")
    text = ctx.section()
    assert not picker.calls
    # 详细模式的写法照旧：每张表一行带字段
    assert "· orders[表]" in text and "· customers[表]" in text
    # 额外带上目录和连接条件
    assert f"中文名：订单{INFERRED_MARK}" in text and "单位：元" in text
    assert f"orders.customer_id = customers.id（多对一）{INFERRED_MARK}" in text
    op = ctx.op()
    assert op["sources"] == [{"source": small_source.name, "tables": ["customers", "orders"], "selected_by": "all",
                              "total": 2}]


# ---------------------------------------------------------------- 回退：绝不阻断生成


@pytest.mark.parametrize("picker", [
    Picker(error=RuntimeError("网关超时")),
    Picker(PICKED, delay=5),
    Picker({"tables": [{"table": "no_such_table"}, {"table": "也没有这张"}]}),
    Picker({"tables": []}),
    Picker("我觉得都需要"),
], ids=["error", "timeout", "unknown", "empty", "garbage"])
async def test_pick_failures_fall_back_to_names_only(client, monkeypatch, scenic_source, picker):
    import app.api.copilot_context as copilot_context

    monkeypatch.setattr(copilot_context, "PICK_TIMEOUT_S", 0.2)
    _use_picker(monkeypatch, picker)
    model = Scripted(_graph_using(f"db_query__{scenic_source.name}"))
    events = await _stream(client, monkeypatch, model, {
        "instruction": NEED, "intent": "answer", "datasource_ids": [scenic_source.id]})

    op = _context_op(events)
    assert op["sources"] == [{"source": scenic_source.name, "tables": [], "selected_by": "fallback",
                              "total": scenic.TABLE_COUNT + len(scenic.VIEWS), "reason": op["sources"][0]["reason"]}]
    assert op["sources"][0]["reason"]
    system = model.systems[0]
    # 和以前一样只列表名、提醒先查字段
    assert "channel_visits" in system and "attributed_share" not in system
    assert "写 SQL 前用 db_schema 工具确认字段" in system
    # 生成照常进行
    assert events[-1]["op"] == "final", events[-1]



# ---------------------------------------------------------------- 流式：context 操作、自查沿用同一份上下文


async def test_stream_reports_the_context_and_prompt_carries_selected_tables(client, monkeypatch, scenic_source):
    from app.api import copilot_context

    picker = Picker(PICKED)
    specs = _use_picker(monkeypatch, picker)
    model = Scripted(_graph_using(f"db_query__{scenic_source.name}"))
    events = await _stream(client, monkeypatch, model, {
        "instruction": NEED, "intent": "answer", "datasource_ids": [scenic_source.id]})

    op = _context_op(events)
    assert op["sources"] == [{"source": scenic_source.name, "tables": ["visits", "channels", "channel_visits"],
                              "selected_by": "model", "total": scenic.TABLE_COUNT + len(scenic.VIEWS)}]
    assert isinstance(op["elapsed_ms"], int) and op["elapsed_ms"] >= 0
    # context 在模型首帧之后、第一条图操作之前
    kinds = [e["op"] for e in events]
    assert kinds.index("model") < kinds.index("context") < kinds.index("add_node")

    # 挑表用同一个助手模型，关掉思考、额度取小
    (spec,) = specs
    assert spec.thinking == "off" and spec.max_tokens == copilot_context.PICK_MAX_TOKENS
    assert spec.max_tokens <= 2048

    system = model.systems[0]
    for col in SELECTED_COLUMNS:
        assert col in system, col
    for col in OTHER_COLUMNS:
        assert col not in system, col
    for cond in JOINS:
        assert cond in system, cond
    # 工具清单里列出的可查对象，先列挑中的表
    tool_line = next(line for line in system.splitlines() if line.startswith(f"- db_query__{scenic_source.name}"))
    assert "可查：visits、channels、channel_visits" in tool_line


async def test_self_check_repair_reuses_the_same_context(client, monkeypatch, scenic_source):
    picker = Picker(PICKED)
    _use_picker(monkeypatch, picker)
    tool = f"db_query__{scenic_source.name}"
    first = [
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
        {"op": "add_node", "node": {"id": "q", "type": "tool", "label": "查入园", "config": {
            "tool": tool, "args": {"sql": "SELECT v.no_such_col FROM visits v"}, "assign_to": "rows"}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "q"}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果", "config": {
            "fields": [{"name": "rows", "value": "{{ vars.rows }}"}]}}},
        {"op": "add_edge", "edge": {"source": "q", "target": "out"}},
        {"op": "done", "explanation": "查一下", "run": False},
    ]
    fix = [{"op": "update_node", "id": "q", "config": {"args": {"sql": "SELECT v.visitor_count FROM visits v"}}},
           {"op": "done"}]
    model = Scripted(first, fix)
    events = await _stream(client, monkeypatch, model, {
        "instruction": NEED, "datasource_ids": [scenic_source.id]})

    assert any(e.get("op") == "check" and e.get("status") == "repairing" for e in events)
    assert len(model.systems) == 2 and "上线前自查" in model.humans[1]
    # 修正轮带的是同一份上下文，没有重新挑表
    assert model.systems[1] == model.systems[0]
    assert "attributed_share  REAL" in model.systems[1]
    assert len(picker.calls) == 1


async def test_small_sources_do_not_call_the_picker(client, monkeypatch, small_source):
    picker = Picker(error=AssertionError("小库不该挑表"))
    _use_picker(monkeypatch, picker)
    model = Scripted(_graph_using(f"db_query__{small_source.name}"))
    events = await _stream(client, monkeypatch, model, {
        "instruction": "订单总额", "intent": "answer", "datasource_ids": [small_source.id]})
    op = _context_op(events)
    assert op["sources"][0]["selected_by"] == "all" and "elapsed_ms" not in op
    assert not picker.calls
    assert "· orders[表]" in model.systems[0]


# ---------------------------------------------------------------- 非流式和两个兜底入口：同一套组装，不发 context


async def test_one_shot_generate_uses_the_same_context(client, monkeypatch, scenic_source):
    import app.api.copilot as copilot

    picker = Picker(PICKED)
    _use_picker(monkeypatch, picker)
    seen: list = []

    class _OneShot:
        def with_structured_output(self, *_a, **_k):
            return self

        async def ainvoke(self, messages):
            seen.append(messages)
            return {"nodes": [], "edges": []}

    async def _model(*_a, **_k):
        return _OneShot(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    r = await client.post("/api/copilot/generate", json={"instruction": NEED, "datasource_ids": [scenic_source.id]})
    assert r.status_code == 200, r.text
    assert len(picker.calls) == 1 and NEED in picker.calls[0]
    system = next(t for role, t in seen[0] if role == "system")
    for cond in JOINS:
        assert cond in system
    assert "attributed_share  REAL" in system


def _node(nid, ntype, label, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label, "config": config}}


async def test_publish_fix_shares_the_context(monkeypatch, scenic_source):
    """发布前修复：现有图查的表一定带上，需求写的是挡住发布的问题；不发 context 操作（返回值里没有）。"""
    import app.api.copilot as copilot

    picker = Picker({"tables": [{"source": scenic_source.name, "table": "visits"},
                                {"source": scenic_source.name, "table": "channels"}]})
    _use_picker(monkeypatch, picker)
    model = Scripted([{"op": "plan", "summary": "看一下"}, {"op": "done", "explanation": "无法修改"}])

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    graph = {"nodes": [
        _node("start", "input", "周期"),
        _node("fetch", "tool", "取数", tool=f"db_query__{scenic_source.name}",
              args={"sql": "SELECT SUM(amount) AS gmv FROM store_sales"}),
        _node("write", "report", "报告撰写", instructions="写周报", numbers="strict", on_violation="fail"),
        _node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        _node("raw", "output", "原始数", fields=[{"name": "数", "value": "{{ nodes.fetch }}"}]),
    ], "edges": [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
        [("start", "fetch"), ("fetch", "write"), ("write", "done"), ("fetch", "raw")])]}
    async with SessionLocal() as session:
        out = await copilot.assist_publish_fix(session, graph, level="governed")
    assert model.systems, out
    assert len(picker.calls) == 1 and "发布前检查" in picker.calls[0]
    system = model.systems[0]
    assert "attributed_share  REAL" in system and "sold_at  TEXT" in system
    assert "context" not in json.dumps(out, ensure_ascii=False)


async def test_upgrade_shares_the_context(monkeypatch, scenic_source):
    import app.api.copilot as copilot

    picker = Picker({"tables": [{"source": scenic_source.name, "table": "no_such_table"}]})
    _use_picker(monkeypatch, picker)
    model = Scripted([{"op": "plan", "summary": "看一下"}, {"op": "done", "explanation": "不改"}])

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    graph = {"nodes": [
        _node("start", "input", "输入"),
        _node("fetch", "tool", "查库", tool=f"db_query__{scenic_source.name}",
              args={"sql": "SELECT SUM(amount) AS a, COUNT(*) AS b FROM store_sales"}),
        _node("calc", "code", "算比率", language="python", assign_to="calc",
              code="import json\nprint(json.dumps({'ratio': 3 / 4}))"),
        _node("card", "metrics", "口径卡", metrics=[{"id": "ratio", "name": "客单价", "expression": "vars.calc.ratio"}]),
        _node("write", "report", "报告撰写", instructions="写一句话"),
        _node("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ], "edges": [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
        [("start", "fetch"), ("fetch", "calc"), ("calc", "card"), ("card", "write"), ("write", "out")])]}
    async with SessionLocal() as session:
        await copilot.assist_upgrade(session, graph, level="published")
    assert model.systems and len(picker.calls) == 1
    # 挑表失败也不挡：现有图查的表照样给全字段，其余表只列名字
    system = model.systems[0]
    assert "sold_at  TEXT" in system and "attributed_share" not in system
