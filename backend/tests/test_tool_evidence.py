"""取数链路进证据台账：tool 节点、agent、知识检索都记账，口径卡能精确到单元格。

一期的台账里只有口径卡的指标集：报告点开一个数，最远追到「vars.q.rows[0][0]」这条
路径，追不到那次查询的哪一格。二期：

- tool 节点落 tool 条目，查库再落 query 条目（工件是查询快照，外面包着工具快照）
- agent 每次查库都记账，给模型看的工具结果前面标上本节点内的编号「【证据 Q1】」
- 知识检索落 retrieval 条目
- 报告目录按全局台账顺序编出 Q1… / K1…
- 口径卡用 cell(nodes.fetch, 0, "amount") 取数时，来历精确到那一格（via tool_cell）
- 数据源工具返回「查询失败：…」「SQL 被拒绝：…」时，tool 节点判失败，报错写原话
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import build_catalog, make_eid
from app.engine.expressions import (
    CellError,
    ExpressionError,
    eval_expression,
    leaf_refs,
    parse_expression,
    substitute,
    table_cell,
)
from app.engine.runner import run_manager

TOOL = "db_query__shop"
SQL = "SELECT SUM(amount) AS gmv, SUM(prev) AS gmv_prev, COUNT(*) AS orders FROM orders WHERE week = '2026-W37'"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def shop(tmp_path):
    """示例 shop 库：本周 3 单，金额 100.5 + 200 + 300。"""
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, amount REAL, prev REAL, refunded INTEGER);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", [
        (1, "2026-W37", 100.5, 90.0, 0), (2, "2026-W37", 200.0, 100.0, 1), (3, "2026-W37", 300.0, 110.0, 0),
        (4, "2026-W36", 50.0, 40.0, 0)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        else:
            row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        await session.commit()
        source_id = row.id
    # 连接池按数据源 id 缓存：换了库文件不作废，查的还是上一个测试的库
    await engines.invalidate(source_id)
    yield
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def finish(graph, statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str | None = None, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def ledger(run_id: str) -> list[dict]:
    tup = await run_manager.checkpointer.aget_tuple({"configurable": {"thread_id": run_id}})
    return list(tup.checkpoint["channel_values"].get("evidence") or [])


# --------------------------------------------------------------------------
# cell()：纯函数
# --------------------------------------------------------------------------

PAYLOAD = {"columns": ["gmv", "orders"], "rows": [[600.5, 3], ["45678.50", 7]], "artifact": "a" * 64}


def test_cell_reads_json_text_and_objects():
    text = json.dumps(PAYLOAD)
    assert table_cell(text, 0, "gmv") == 600.5
    assert table_cell(PAYLOAD, 0, 1) == 3                   # 列号也认
    assert table_cell(PAYLOAD, 1, "gmv") == 45678.5         # DECIMAL 字符串能参与运算
    assert table_cell({"rows": [{"gmv": 1}]}, 0, "gmv") == 1
    assert eval_expression("cell(nodes.q, 0, 'gmv') * 2", {"nodes": {"q": text}}) == 1201.0


@pytest.mark.parametrize("args, words", [
    ((PAYLOAD, 2, "gmv"), "只有 2 行"),
    ((PAYLOAD, 0, "amount"), "没有列「amount」"),
    ((PAYLOAD, 0, 5), "只有 2 列"),
    ((PAYLOAD, -1, "gmv"), "从 0 数"),
    ((PAYLOAD, True, "gmv"), "整数"),
    (("查询失败：no such table: orders", 0, "gmv"), "查询失败：no such table: orders"),
    (({"columns": ["gmv"], "rows": []}, 0, "gmv"), "0 行"),
    ((None, 0, "gmv"), "查询结果"),
])
def test_cell_errors_are_readable(args, words):
    with pytest.raises(CellError) as info:
        table_cell(*args)
    assert words in str(info.value) and isinstance(info.value, ExpressionError)
    assert str(info.value).startswith("cell()")


# 驱动把 DECIMAL 一律落成 str(Decimal)：小数位为 0 的——MySQL 对整数列 SUM()（包括
# SUM(CASE WHEN … THEN 1 ELSE 0 END) 这种计数写法）、Postgres 的 SUM(bigint)——是没有小数点的
# "45678"；很小的数写成 "1E-7"
INT_DECIMAL = {"columns": ["gmv", "refunds", "code", "tiny"], "rows": [["45678", "29", "00123", "1E-7"]],
               "artifact": "a" * 64}


def test_cell_treats_integer_decimal_text_as_numbers():
    gmv = table_cell(INT_DECIMAL, 0, "gmv")
    assert gmv == 45678 and isinstance(gmv, int)
    assert table_cell(INT_DECIMAL, 0, "refunds") == 29
    assert table_cell(INT_DECIMAL, 0, "tiny") == 1e-7
    assert table_cell(INT_DECIMAL, 0, "code") == "00123"          # 带前导零的是编号，照文本
    ctx = {"nodes": {"q": json.dumps(INT_DECIMAL)}}
    assert eval_expression("cell(nodes.q, 0, 'gmv') / 2", ctx) == 22839.0
    assert eval_expression("cell(nodes.q, 0, 'refunds') + 1", ctx) == 30


def test_leaf_refs_treat_a_cell_call_as_one_input():
    tree = parse_expression("round(cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, vars.i, 'orders'), 2)")[0]
    assert leaf_refs(tree) == ["cell(nodes.fetch, 0, 'gmv')", "cell(nodes.fetch, vars.i, 'orders')", "vars.i"]
    text = substitute(tree, {"cell(nodes.fetch, 0, 'gmv')": 600.5, "cell(nodes.fetch, vars.i, 'orders')": 3,
                             "vars.i": 0})
    assert text == "round(600.5 / 3, 2)"


# --------------------------------------------------------------------------
# tool 节点
# --------------------------------------------------------------------------


def fetch_graph(*after, **tool_cfg):
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool=TOOL, args={"sql": SQL}, **tool_cfg),
        *after,
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.fetch }}"}]),
    )


async def test_tool_node_writes_tool_and_query_entries(engine_up, shop):
    row = await finish(fetch_graph())
    assert row.status == "succeeded", row.error
    [end] = await events(row.id, "tool.end", "fetch")
    payload = json.loads(row.output["r"])
    assert end.data["query_artifact"] == payload["artifact"]
    [finished] = await events(row.id, "node.finished", "fetch")
    tool_entry, query_entry = finished.data["evidence"]
    assert tool_entry == {"kind": "tool", "node_id": "fetch", "exec": 1, "artifact": end.data["artifact"], "tool": TOOL}
    assert query_entry == {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": payload["artifact"],
                           "via": end.data["artifact"], "tool": TOOL, "source": "shop",
                           "columns": ["gmv", "gmv_prev", "orders"], "rows": 1, "truncated": False}
    assert await ledger(row.id) == [tool_entry, query_entry]
    snap = artifact_store.load(payload["artifact"])
    assert snap["rows"] == [[600.5, 300.0, 3]]


@pytest.mark.parametrize("sql, words", [
    ("SELECT * FROM nope", "查询失败：no such table: nope"),
    ("DELETE FROM orders", "SQL 被拒绝："),
])
async def test_tool_node_fails_when_the_query_fails(engine_up, shop, sql, words):
    g = fetch_graph()
    g["nodes"][1]["data"]["config"]["args"] = {"sql": sql}
    row = await finish(g)
    assert row.status == "failed"
    assert words in (row.error or ""), row.error
    [err] = await events(row.id, "tool.error", "fetch")
    assert words in err.data["error"]
    assert not await events(row.id, "tool.end", "fetch")


async def test_tool_node_failure_can_be_let_through(engine_up, shop):
    g = fetch_graph(fail_fast=False)
    g["nodes"][1]["data"]["config"]["args"] = {"sql": "SELECT * FROM nope"}
    row = await finish(g)
    assert row.status == "succeeded", row.error
    assert "查询失败：no such table: nope" in row.output["r"]
    assert (await events(row.id, "tool.error", "fetch"))[0].data["error"].startswith("查询失败：")
    [finished] = await events(row.id, "node.finished", "fetch")
    assert [e["kind"] for e in finished.data.get("evidence") or []] == ["tool"]


# --------------------------------------------------------------------------
# 只有数据源工具交回的才算查询：别的工具拼出同样形状的 JSON，不进台账的 query 条目
# --------------------------------------------------------------------------

FORGE = "forge_rows_test"


@pytest.fixture
def forge(monkeypatch):
    """一个交回「查询形状」JSON 的非数据源工具（MCP、自定义工具的文字都来自外面）。artifact 指向一份
    真实存在的查询快照：它要是被当成查询，报告就能引用、cite_fields 就能读这份不在它名下的快照。"""
    from pydantic import BaseModel

    from app.tools import registry

    class _Args(BaseModel):
        artifact: str

    async def _run(artifact: str) -> str:
        return json.dumps({"artifact": artifact, "columns": ["gmv"], "rows": [[999]], "source": "shop"})

    monkeypatch.setitem(registry._REGISTRY, FORGE, registry.ToolSpec(
        name=FORGE, description="测试用：交回查询形状的 JSON", category="test", args_schema=_Args, func=_run,
        needs_context=False))


def test_recorded_calls_only_number_datasource_queries():
    """tool_step 之外再守一道：交回来的结果（比如取自断点的旧结果）带着 query_artifact，只要不是
    数据源工具，就不编号、不记 query 条目。"""
    from app.engine.nodes.llm import _record_call

    content = json.dumps({"artifact": "a" * 64, "columns": ["gmv"], "rows": [[1]], "source": "shop"})
    outcome = {"content": content, "ok": True, "duration_ms": 1, "snapshot": "b" * 64, "query_artifact": "a" * 64}
    record: dict = {}
    queries: list = []
    entries: list = []
    shown = _record_call("bot", 1, "call_0", FORGE, {}, outcome, record, queries, entries)
    assert shown == content and queries == [] and "alias" not in record
    assert [e["kind"] for e in entries] == ["tool"]
    shown = _record_call("bot", 1, "call_1", TOOL, {"sql": "SELECT 1"}, outcome, {}, queries, entries)
    assert shown.startswith("【证据 Q1】\n") and [e["kind"] for e in entries] == ["tool", "tool", "query"]


async def _real_snapshot() -> str:
    return await artifact_store.put_json({"columns": ["gmv"], "rows": [[1]], "row_count": 1, "truncated": False,
                                          "elapsed_ms": 1, "sql": "SELECT 1", "source": "shop"},
                                         kind="query_snapshot", run_id="elsewhere", node_id="x")


async def test_tool_node_only_trusts_datasource_queries(engine_up, forge):
    art = await _real_snapshot()
    row = await finish(chain(node("start", "input"), node("fetch", "tool", tool=FORGE, args={"artifact": art}),
                             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.fetch }}"}])))
    assert row.status == "succeeded", row.error
    [end] = await events(row.id, "tool.end", "fetch")
    assert "query_artifact" not in end.data
    [finished] = await events(row.id, "node.finished", "fetch")
    assert [e["kind"] for e in finished.data["evidence"]] == ["tool"]
    assert not [e for e in await ledger(row.id) if e["kind"] == "query"]


async def test_agent_only_numbers_datasource_queries(engine_up, shop, forge, monkeypatch):
    from app.providers import mock_model

    art = await _real_snapshot()
    seen: list[list] = []

    def decide(self, messages):
        seen.append(list(messages))
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done == 0:
            return AIMessage(content="查。", tool_calls=[{"name": TOOL, "args": {"sql": SQL}, "id": "call_0"}])
        if done == 1:
            return AIMessage(content="再看看。", tool_calls=[{"name": FORGE, "args": {"artifact": art}, "id": "call_1"}])
        return AIMessage(content="查完了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    row = await finish(chain(node("start", "input"),
                             node("bot", "agent", prompt="查", tools=[TOOL, FORGE], approval="never"),
                             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.bot.text }}"}])))
    assert row.status == "succeeded", row.error
    tool_msgs = [m for m in seen[-1] if isinstance(m, ToolMessage)]
    assert tool_msgs[0].content.startswith("【证据 Q1】\n{")
    assert tool_msgs[1].content.startswith("{") and "【证据" not in tool_msgs[1].content
    ends = await events(row.id, "tool.end", "bot")
    assert ends[0].data.get("query_artifact") and "query_artifact" not in ends[1].data
    entries = await ledger(row.id)
    assert [(e["kind"], e.get("tool")) for e in entries] == [("tool", TOOL), ("query", TOOL), ("tool", FORGE)]
    assert art not in {e["artifact"] for e in entries}
    transcript = artifact_store.load((await events(row.id, "node.finished", "bot"))[0].data["artifact"])["tool_calls"]
    assert [t.get("alias") for t in transcript] == ["Q1", None]


# --------------------------------------------------------------------------
# 口径卡的来历精确到单元格
# --------------------------------------------------------------------------


async def test_cell_lineage_points_at_the_exact_cell(engine_up, shop):
    card = node("card", "metrics", caliber="周报口径", metrics=[
        {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands",
         "expression": "cell(nodes.fetch, 0, 'gmv')"},
        {"id": "wow", "name": "环比", "decimals": 1, "format": "plain",
         "expression": "round((cell(nodes.fetch, 0, 'gmv') - cell(vars.q, 0, 1)) / cell(vars.q, 0, 1) * 100, 1)"},
    ])
    parse = node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="q")
    row = await finish(fetch_graph(parse, card))
    assert row.status == "succeeded", row.error
    query = next(e for e in await ledger(row.id) if e["kind"] == "query")
    finished = (await events(row.id, "node.finished", "card"))[0]
    metrics = {m["id"]: m for m in artifact_store.load(finished.data["artifact"])["metrics"]}

    gmv = metrics["gmv"]
    assert gmv["value"] == 600.5 and gmv["substituted"] == "600.5" and gmv["recompute_ok"] is True
    [cell] = gmv["inputs"]
    assert cell == {"path": "cell(nodes.fetch, 0, 'gmv')", "value": 600.5, "node_id": "fetch", "via": "tool_cell",
                    "status": "verified", "locator": {"row": 0, "column": "gmv"}, "artifact": query["artifact"],
                    "eid": make_eid("cell", query["artifact"], {"row": 0, "column": "gmv"})}

    # 经 transform 解析过的对象照样认得出是哪次查询；列号换成列名，eid 和按列名写的一致
    prev = next(i for i in metrics["wow"]["inputs"] if i["path"] == "cell(vars.q, 0, 1)")
    assert prev["via"] == "tool_cell" and prev["node_id"] == "fetch" and prev["status"] == "verified"
    assert prev["locator"] == {"row": 0, "column": "gmv_prev"}
    assert prev["eid"] == make_eid("cell", query["artifact"], {"row": 0, "column": "gmv_prev"})


async def test_card_computes_on_integer_decimal_text(engine_up, shop):
    """sqlite 没有 DECIMAL：CAST(… AS TEXT) 落进快照的，和 MySQL 整数列 SUM() 的 str(Decimal) 一模一样。"""
    sql = ("SELECT CAST(SUM(refunded) AS TEXT) AS refunds, CAST(COUNT(*) AS TEXT) AS orders "
           "FROM orders WHERE week = '2026-W37'")
    card = node("card", "metrics", caliber="周报口径", metrics=[
        {"id": "rate", "name": "退款率", "decimals": 1, "format": "plain",
         "expression": "cell(nodes.fetch, 0, 'refunds') / cell(nodes.fetch, 0, 'orders') * 100"}])
    g = fetch_graph(card)
    g["nodes"][1]["data"]["config"]["args"] = {"sql": sql}
    row = await finish(g)
    assert row.status == "succeeded", row.error
    assert json.loads(row.output["r"])["rows"] == [["1", "3"]]
    finished = (await events(row.id, "node.finished", "card"))[0]
    [rate] = artifact_store.load(finished.data["artifact"])["metrics"]
    assert rate["value"] == pytest.approx(33.3) and rate["recompute_ok"] is True
    assert {i["status"] for i in rate["inputs"]} == {"verified"}
    assert [i["value"] for i in rate["inputs"]] == [1, 3]


async def test_cell_out_of_range_fails_the_card_readably(engine_up, shop):
    card = node("card", "metrics", metrics=[{"id": "x", "expression": "cell(nodes.fetch, 3, 'gmv')"}])
    row = await finish(fetch_graph(card))
    assert row.status == "failed"
    assert "cell() 要第 3 行，但这份查询结果只有 1 行" in (row.error or ""), row.error


# --------------------------------------------------------------------------
# agent 与知识检索也记账；目录按全局顺序编号
# --------------------------------------------------------------------------


def script_agent(monkeypatch, sqls):
    from app.providers import mock_model

    seen: list[list] = []

    def decide(self, messages):
        if self.response_format:
            return AIMessage(content="{}")
        seen.append(list(messages))
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(sqls):
            return AIMessage(content="查。", tool_calls=[{"name": TOOL, "args": {"sql": sqls[done]}, "id": f"call_{done}"}])
        return AIMessage(content="查完了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    return seen


async def test_agent_and_retrieval_write_the_ledger_and_the_catalog_numbers_them(engine_up, shop, monkeypatch):
    from app.memory import kb

    async def fake_search(session, **kw):
        return [{"title": "口径说明", "ordinal": 1, "content": "退款按发生周统计", "score": 0.9, "document_id": "d1"}]

    monkeypatch.setattr(kb, "search", fake_search)
    seen = script_agent(monkeypatch, [SQL, "SELECT * FROM nope", "SELECT COUNT(*) AS refunds FROM orders WHERE refunded = 1"])
    g = chain(
        node("start", "input"),
        node("docs", "retrieve", query="退款口径", collection="docs"),
        node("bot", "agent", prompt="查本周销售额和退款", tools=[TOOL], approval="never"),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.bot.text }}"}]),
    )
    row = await finish(g)
    assert row.status == "succeeded", row.error

    # 给模型看的：查成功的结果前面有本节点内的编号，失败的原话照旧喂回去、不编号
    tool_msgs = [m for m in seen[-1] if isinstance(m, ToolMessage)]
    assert tool_msgs[0].content.startswith("【证据 Q1】\n{")
    assert tool_msgs[1].content.startswith("查询失败：no such table: nope")
    assert tool_msgs[2].content.startswith("【证据 Q2】\n{")

    entries = await ledger(row.id)
    assert [(e["kind"], e["node_id"]) for e in entries] == [
        ("retrieval", "docs"), ("tool", "bot"), ("query", "bot"), ("tool", "bot"), ("tool", "bot"), ("query", "bot")]
    retrieval = entries[0]
    [retrieve_end] = await events(row.id, "retrieve.end", "docs")
    assert retrieval == {"kind": "retrieval", "node_id": "docs", "exec": 1, "artifact": retrieve_end.data["artifact"],
                         "source": "docs", "rows": 1}
    ends = await events(row.id, "tool.end", "bot")
    queries = [e for e in entries if e["kind"] == "query"]
    assert [q["artifact"] for q in queries] == [e.data["query_artifact"] for e in ends if e.data.get("query_artifact")]
    assert [q["via"] for q in queries] == [ends[0].data["artifact"], ends[2].data["artifact"]]
    assert queries[0]["call_id"] == "call_0" and queries[1]["call_id"] == "call_2"
    assert queries[1]["columns"] == ["refunds"] and queries[1]["rows"] == 1

    # 工具调用记录带上编号和工件
    bot = (await events(row.id, "node.finished", "bot"))[0].data
    assert [e["kind"] for e in bot["evidence"]] == ["tool", "query", "tool", "tool", "query"]
    transcript = artifact_store.load(bot["artifact"])["tool_calls"]
    assert [t.get("alias") for t in transcript] == ["Q1", None, "Q2"]
    assert transcript[0]["artifact"] == queries[0]["artifact"] and transcript[0]["via"] == queries[0]["via"]
    assert transcript[0]["call_id"] == "call_0"

    catalog = build_catalog(nodes={}, ledger=entries)
    assert catalog["Q1"]["artifact"] == queries[0]["artifact"] and catalog["Q2"]["artifact"] == queries[1]["artifact"]
    assert catalog["K1"]["artifact"] == retrieval["artifact"] and "1 条" in catalog["K1"]["label"]


# --------------------------------------------------------------------------
# 问数据那条路：input → agent → report → output，报告直接引用 agent 查过的格
# --------------------------------------------------------------------------


async def test_report_cites_cells_the_agent_queried(engine_up, shop, monkeypatch):
    from app.engine.evidence import iter_segments
    from app.providers import mock_model

    answer = "本周销售额 [[v:Q1.r0.gmv]]，共 [[v:Q1.r0.orders]] 单。[[see:Q1]]\n\n[[table:Q1 cols=gmv,orders]]"

    def decide(self, messages):
        if self.tools:
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content="查。", tool_calls=[{"name": TOOL, "args": {"sql": SQL}, "id": "call_0"}])
            return AIMessage(content="查完了。")
        return AIMessage(content=answer)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    g = chain(
        node("start", "input"),
        node("bot", "agent", prompt="本周卖了多少", tools=[TOOL], approval="never"),
        node("write", "report", instructions="回答用户的问题"),
        node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]),
    )
    row = await finish(g)
    assert row.status == "succeeded", row.error
    assert row.output["答案"] == ("本周销售额 600.5，共 3 单。\n\n| gmv | orders |\n| --- | --- |\n| 600.5 | 3 |")
    assert row.output["_evidence"]["fields"] == ["答案"]

    [checked] = await events(row.id, "report.checked", "write")
    assert checked.data["ok"] is True and checked.data["violations"] == []
    doc = artifact_store.load(checked.data["doc_artifact"])
    query = next(e for e in await ledger(row.id) if e["kind"] == "query")
    cited = [s for s in iter_segments(doc) if s.get("ref") == "v:Q1.r0.gmv"]
    assert len(cited) == 2            # 正文一处、表里一处，指向同一格
    assert {s["cite"]["eid"] for s in cited} == {make_eid("cell", query["artifact"], {"row": 0, "column": "gmv"})}
    assert doc["catalog"]["Q1"]["artifact"] == query["artifact"] and doc["catalog"]["Q1"]["node_id"] == "bot"
