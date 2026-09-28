"""agent 的 cite_fields：结构化字段逐个标出处，值以快照为准。

以前 agent 的 output_schema 根本不生效，给口径卡供数只能靠 transform 从自由文本里抠，
抠不到的字段模型会顺手填个 0——报告里「退款 0 单」看着像查过，其实是没查到。二期：

- 写了 output_schema 并开 cite_fields: true 时，循环结束后做一次结构化抽取：每个字段
  写 {value, from: {call: "Q1", row: 0, column: "gmv"}}，查不到就填 null
- 系统按 from 去快照里取那一格，以快照为准：
  verified（一致）/ mismatch（不一致，按快照取值、发警告）/ unresolved（取不到，记空值、
  发警告）/ from 为 null（记空值）——永远不兜底成 0
- 口径卡的来历接上字段的出处：via agent_field，带 eid 和核对状态
- 审批恢复时节点整个重放：抽取不重复调用、编号不错位
- 升级前发起的运行一字不改
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
from app.engine import runner as runner_mod
from app.engine.evidence import make_eid
from app.engine.runner import run_manager

TOOL = "db_query__shop"
SQLS = [
    "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders WHERE week = '2026-W37'",
    "SELECT COUNT(*) AS refunds, MAX(week) AS week FROM orders WHERE refunded = 1",
]
FINAL = "本周销售额 600.5，3 单。"
SCHEMA = {
    "type": "object",
    "properties": {
        "gmv": {"type": "number", "description": "本周销售额"},
        "orders": {"type": "integer"},
        "refunds": {"type": "integer"},
        "region": {"type": "string"},
        "week": {"type": "string"},
    },
    "required": ["gmv", "orders"],
}
EXTRACTED = {
    "gmv": {"value": 600.5, "from": {"call": "Q1", "row": 0, "column": "gmv"}},
    "orders": {"value": 30, "from": {"call": "Q1", "row": 0, "column": "orders"}},     # 快照里是 3
    "refunds": {"value": 0, "from": None},                                              # 模型说没查到
    "region": {"value": "华东", "from": {"call": "Q9", "row": 0, "column": "region"}},  # 本节点没有 Q9
    "week": {"value": "2026-W37", "from": {"call": "Q2", "row": 0, "column": "week"}},
}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, amount REAL, refunded INTEGER);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", [
        (1, "2026-W37", 100.5, 0), (2, "2026-W37", 200.0, 1), (3, "2026-W37", 300.0, 0), (4, "2026-W36", 50.0, 0)])
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


class Script:
    """agent 依次查 SQLS 里的两条，再给结论；结构化抽取按 extracted 回。"""

    def __init__(self, monkeypatch, extracted=EXTRACTED, *, fail_extract=False, sqls=SQLS):
        from app.providers import mock_model

        self.loop: list[list] = []
        self.extract: list[list] = []

        def decide(model, messages):
            if model.response_format:
                self.extract.append(list(messages))
                if fail_extract:
                    raise RuntimeError("网关超时")
                return AIMessage(content=json.dumps(extracted, ensure_ascii=False))
            if not model.tools:
                return AIMessage(content="收尾")
            self.loop.append(list(messages))
            done = sum(isinstance(m, ToolMessage) for m in messages)
            if done < len(sqls):
                return AIMessage(content="查。", tool_calls=[
                    {"name": TOOL, "args": {"sql": sqls[done]}, "id": f"call_{done}"}])
            return AIMessage(content=FINAL)

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(*, card=True, **agent):
    cfg = {"prompt": "查本周销售额、订单和退款", "tools": [TOOL], "approval": "never",
           "output_schema": SCHEMA, "cite_fields": True, "assign_to": "kpi", **agent}
    nodes = [node("start", "input"), node("bot", "agent", **cfg)]
    if card:
        nodes.append(node("card", "metrics", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands", "expression": "vars.kpi.gmv"},
            {"id": "orders", "name": "订单", "unit": "单", "format": "thousands", "expression": "vars.kpi.orders"}]))
    nodes.append(node("out", "output", fields=[{"name": "kpi", "value": "{{ vars.kpi | json }}"}]))
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def events(run_id: str, etype: str | None = None, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def output_of(run_id: str, node_id: str) -> dict:
    [finished] = await events(run_id, "node.finished", node_id)
    return artifact_store.load(finished.data["artifact"])


def logs(evts, code):
    return [e.data for e in evts if e.type == "log" and e.data.get("code") == code]


def text_of(message) -> str:
    return message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)


# --------------------------------------------------------------------------
# 四种核对结果
# --------------------------------------------------------------------------


async def test_fields_are_checked_against_the_snapshot(monkeypatch):
    script = Script(monkeypatch)
    run = await run_manager.start(graph=graph(), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    bot = await output_of(row.id, "bot")
    assert bot["text"] == FINAL
    # 以快照为准：orders 模型报 30，快照是 3；没查到的是 None，不是 0
    assert bot["data"] == {"gmv": 600.5, "orders": 3, "refunds": None, "region": None, "week": "2026-W37"}
    assert json.loads(row.output["kpi"]) == bot["data"], "assign_to 拿到的应该是核对过的 data"

    ends = [e.data for e in await events(row.id, "tool.end", "bot")]
    q1, q2 = ends[0]["query_artifact"], ends[1]["query_artifact"]
    ev = bot["data_evidence"]
    assert ev["gmv"] == {"ref": "Q1.r0.gmv", "call": "Q1", "artifact": q1, "via": ends[0]["artifact"],
                         "locator": {"row": 0, "column": "gmv"},
                         "eid": make_eid("cell", q1, {"row": 0, "column": "gmv"}), "status": "verified"}
    assert ev["orders"]["status"] == "mismatch" and ev["orders"]["model_value"] == 30
    assert ev["orders"]["eid"] == make_eid("cell", q1, {"row": 0, "column": "orders"})
    assert ev["refunds"]["status"] == "missing" and ev["refunds"].get("ref") is None
    assert ev["region"]["status"] == "unresolved" and "Q9" in ev["region"]["reason"]
    assert ev["week"]["status"] == "verified" and ev["week"]["artifact"] == q2

    evts = await events(row.id)
    [mismatch] = logs(evts, "agent_field_mismatch")
    assert mismatch["level"] == "warn" and mismatch["fields"] == ["orders"]
    assert "模型报 30" in mismatch["message"] and "快照是 3" in mismatch["message"]
    [unverified] = logs(evts, "agent_field_unverified")
    assert unverified["level"] == "warn" and unverified["fields"] == ["region"]

    # 给模型的工具结果带本节点内的编号；抽取只调一次，提示里有数据、有「禁止估算」
    tool_msgs = [m for m in script.loop[-1] if isinstance(m, ToolMessage)]
    assert [text_of(m).split("\n", 1)[0] for m in tool_msgs] == ["【证据 Q1】", "【证据 Q2】"]
    assert len(script.extract) == 1
    prompt = "\n".join(text_of(m) for m in script.extract[0])
    assert "禁止估算" in prompt and "【证据 Q1】" in prompt and "600.5" in prompt and FINAL in prompt
    assert len(await events(row.id, "llm.end", "bot")) == len(script.loop) + 1

    # 口径卡的来历接上字段的出处
    card = await output_of(row.id, "card")
    gmv = next(m for m in card["metrics"] if m["id"] == "gmv")
    [src] = gmv["inputs"]
    assert src["via"] == "agent_field" and src["node_id"] == "bot" and src["field"] == "gmv"
    assert src["eid"] == ev["gmv"]["eid"] and src["status"] == "verified" and src["ref"] == "Q1.r0.gmv"
    assert src["artifact"] == q1 and src["locator"] == {"row": 0, "column": "gmv"}
    orders = next(m for m in card["metrics"] if m["id"] == "orders")
    assert orders["value"] == 3 and orders["inputs"][0]["status"] == "mismatch"
    assert orders["inputs"][0]["model_value"] == 30


# --------------------------------------------------------------------------
# 小数位为 0 的 DECIMAL：驱动落成没有小数点的 "45678"
# --------------------------------------------------------------------------


def test_integer_decimal_text_follows_the_declared_type():
    """MySQL 整数列 SUM()、Postgres SUM(bigint) 落进快照是 "45678"。声明成数的字段要拿到数（下游口径卡
    要拿它做除法），模型报 45678 或 45678.0 都算一致；声明成文本的照旧是文本。"""
    from app.engine.expressions import eval_expression
    from app.engine.nodes.llm import cited_fields, verify_cited_fields

    snap = {"columns": ["gmv", "refunds", "week", "code"], "rows": [["45678", "29", "2026", "00123"]]}
    schema = {"type": "object", "properties": {
        "gmv": {"type": "number"}, "refunds": {"type": "integer"}, "week": {"type": "string"},
        "code": {"type": ["integer", "null"]}}}
    queries = [{"alias": "Q1", "artifact": "a" * 64, "via": "b" * 64}]

    def at(column):
        return {"call": "Q1", "row": 0, "column": column}

    for told in (45678, 45678.0, "45678"):
        parsed = {"gmv": {"value": told, "from": at("gmv")}, "refunds": {"value": 29.0, "from": at("refunds")},
                  "week": {"value": "2026", "from": at("week")}, "code": {"value": 123, "from": at("code")}}
        data, ev = verify_cited_fields({"parsed": parsed}, cited_fields(schema), queries, loader=lambda a: snap)
        assert {k: e["status"] for k, e in ev.items()} == dict.fromkeys(schema["properties"], "verified"), (told, ev)
        assert data == {"gmv": 45678, "refunds": 29, "week": "2026", "code": 123}
        assert type(data["gmv"]) is int and type(data["refunds"]) is int and type(data["week"]) is str
    assert eval_expression("vars.kpi.gmv / 2", {"vars": {"kpi": data}}) == 22839.0

    rows = {"columns": ["week", "refunds"], "rows": [["2026-W36", "4"], ["2026-W37", "29"]]}
    arrays = {"type": "object", "properties": {
        "weeks": {"type": "array", "items": {"type": "object", "properties": {
            "week": {"type": "string"}, "refunds": {"type": "integer"}}}},
        "counts": {"type": "array", "items": {"type": "number"}}}}
    parsed = {"weeks": {"value": [{"week": "2026-W36", "refunds": 4}, {"week": "2026-W37", "refunds": 29.0}],
                        "from": {"call": "Q1", "rows": "0-1", "columns": {"week": "week", "refunds": "refunds"}}},
              "counts": {"value": [4, 29], "from": {"call": "Q1", "rows": "0-1", "column": "refunds"}}}
    data, ev = verify_cited_fields({"parsed": parsed}, cited_fields(arrays), queries, loader=lambda a: rows)
    assert ev["weeks"]["status"] == "verified" and ev["counts"]["status"] == "verified"
    assert data == {"weeks": [{"week": "2026-W36", "refunds": 4}, {"week": "2026-W37", "refunds": 29}],
                    "counts": [4, 29]}


async def test_integer_decimal_text_feeds_a_card_that_divides(monkeypatch):
    """sqlite 没有 DECIMAL：CAST(… AS TEXT) 落进快照的，和 MySQL 整数列 SUM() 的 str(Decimal) 一模一样。"""
    sql = ("SELECT CAST(SUM(refunded) AS TEXT) AS refunds, CAST(COUNT(*) AS TEXT) AS orders "
           "FROM orders WHERE week = '2026-W37'")
    extracted = {"refunds": {"value": 1.0, "from": {"call": "Q1", "row": 0, "column": "refunds"}},
                 "orders": {"value": 3, "from": {"call": "Q1", "row": 0, "column": "orders"}}}
    Script(monkeypatch, extracted, sqls=[sql])
    g = graph(card=False, output_schema={"type": "object", "properties": {
        "refunds": {"type": "integer"}, "orders": {"type": "integer"}}})
    g["nodes"].insert(2, node("card", "metrics", caliber="周报口径", metrics=[
        {"id": "rate", "name": "退款率", "unit": "%", "decimals": 1, "format": "plain",
         "expression": "vars.kpi.refunds / vars.kpi.orders * 100"}]))
    g["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(g["nodes"], g["nodes"][1:])]
    run = await run_manager.start(graph=g, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    bot = await output_of(row.id, "bot")
    assert bot["data"] == {"refunds": 1, "orders": 3}
    assert {e["status"] for e in bot["data_evidence"].values()} == {"verified"}
    assert not logs(await events(row.id), "agent_field_mismatch")
    [rate] = (await output_of(row.id, "card"))["metrics"]
    assert rate["value"] == pytest.approx(33.3) and rate["status"] == "ok"
    assert [i["status"] for i in rate["inputs"]] == ["verified", "verified"]


async def test_extraction_failure_leaves_fields_empty_not_zero(monkeypatch):
    Script(monkeypatch, fail_extract=True)
    run = await run_manager.start(graph=graph(card=False), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    bot = await output_of(row.id, "bot")
    assert bot["data"] == {k: None for k in SCHEMA["properties"]}
    assert all(e["status"] == "unresolved" for e in bot["data_evidence"].values())
    [warn] = logs(await events(row.id), "agent_field_unverified")
    assert warn["fields"] == list(SCHEMA["properties"]) and "网关超时" in warn["message"]


async def test_output_schema_alone_changes_nothing_but_the_ledger(monkeypatch):
    """只写 output_schema、没开 cite_fields：不抽取，变量照旧是文本。"""
    script = Script(monkeypatch)
    run = await run_manager.start(graph=graph(card=False, cite_fields=False), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert script.extract == []
    bot = await output_of(row.id, "bot")
    assert "data" not in bot and "data_evidence" not in bot
    assert row.output["kpi"] == json.dumps(FINAL, ensure_ascii=False, indent=2)
    [finished] = await events(row.id, "node.finished", "bot")
    assert [e["kind"] for e in finished.data["evidence"]] == ["tool", "query", "tool", "query"]


# --------------------------------------------------------------------------
# 审批恢复重放
# --------------------------------------------------------------------------


async def test_replay_after_approvals_does_not_repeat_or_renumber(monkeypatch):
    script = Script(monkeypatch)
    run = await run_manager.start(graph=graph(approval="always"), input_payload={})
    for _ in SQLS:
        row = await wait(run.id, ("interrupted", "failed", "succeeded"))
        assert row.status == "interrupted", row.error
        await run_manager.resume(run.id, {"approved": True})
    row = await wait(run.id, ("succeeded", "failed", "interrupted"))
    assert row.status == "succeeded", row.error

    assert len(script.extract) == 1, "抽取在重放时又调了一次"
    assert len(await events(row.id, "human.requested", "bot")) == 2
    ends = [e.data for e in await events(row.id, "tool.end", "bot")]
    assert len(ends) == 2, "工具在重放时又执行了一次"
    assert len(await events(row.id, "llm.end", "bot")) == len(script.loop) + 1

    bot = await output_of(row.id, "bot")
    ev = bot["data_evidence"]
    assert ev["gmv"]["artifact"] == ends[0]["query_artifact"] and ev["gmv"]["call"] == "Q1"
    assert ev["week"]["artifact"] == ends[1]["query_artifact"] and ev["week"]["call"] == "Q2"
    assert bot["data"]["gmv"] == 600.5 and bot["data"]["orders"] == 3

    # 重放之后模型看到的编号还是同一套
    tool_msgs = [m for m in script.loop[-1] if isinstance(m, ToolMessage)]
    assert [text_of(m).split("\n", 1)[0] for m in tool_msgs] == ["【证据 Q1】", "【证据 Q2】"]
    [finished] = await events(row.id, "node.finished", "bot")
    queries = [e for e in finished.data["evidence"] if e["kind"] == "query"]
    assert [q["artifact"] for q in queries] == [ends[0]["query_artifact"], ends[1]["query_artifact"]]


async def test_continuing_after_a_failure_past_the_extraction_does_not_extract_again(monkeypatch):
    """抽取之后节点挂了，接着跑：节点整个重放，抽取的结果取自断点，不再调用、不再计费。

    审批恢复那条测不到这一点：抽取在最后一次审批之后才跑，不进 checkpoint 也只调一次。
    """
    from app.engine.nodes import llm as llm_mod

    script = Script(monkeypatch)
    real = llm_mod.verify_cited_fields
    broken = {"on": True}

    def flaky(*args, **kwargs):
        if broken["on"]:
            raise RuntimeError("核对时断了")
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_mod, "verify_cited_fields", flaky)
    run = await run_manager.start(graph=graph(card=False), input_payload={})
    row = await wait(run.id)
    assert row.status == "failed" and "核对时断了" in (row.error or ""), row.error
    assert len(script.extract) == 1
    await run_manager.wait_idle(run.id)

    broken["on"] = False
    await run_manager.continue_failed(run.id)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert len(script.extract) == 1, "接着跑时抽取又调了一次：extract_step 的结果没进 checkpoint"
    assert len(await events(row.id, "tool.end", "bot")) == 2
    assert len([e for e in await events(row.id, "llm.end", "bot") if e.data.get("purpose") == "cite_fields"]) == 1
    bot = await output_of(row.id, "bot")
    assert bot["data"]["gmv"] == 600.5 and bot["data_evidence"]["gmv"]["status"] == "verified"


async def test_run_paused_under_the_previous_code_resumes_cleanly(monkeypatch):
    """护栏之后、二期之前发起的运行（有 agent_limits 快照）停在审批上，升级后恢复：checkpoint 里
    缓存的 tool_step 是旧形状（没有 snapshot / query_artifact）。恢复不能崩、不能重复执行、不能
    重复问人；旧的那次调用不编号、不进台账，之后的照常编号。"""
    from app.engine.nodes import llm as llm_mod

    script = Script(monkeypatch)
    monkeypatch.setattr(llm_mod, "ledger_enabled", lambda run: False)     # 模拟上一版代码
    run = await run_manager.start(graph=graph(card=False, approval="always"), input_payload={})
    row = await wait(run.id, ("interrupted", "failed", "succeeded"))
    await run_manager.resume(run.id, {"approved": True})                  # 第一次调用在旧代码下执行
    row = await wait(run.id, ("interrupted", "failed", "succeeded"))
    assert row.status == "interrupted", row.error
    monkeypatch.undo()                                                     # 升级
    script = Script(monkeypatch)
    await run_manager.resume(run.id, {"approved": True})
    row = await wait(run.id, ("succeeded", "failed", "interrupted"))
    assert row.status == "succeeded", row.error

    assert len(await events(row.id, "human.requested", "bot")) == 2
    ends = [e.data for e in await events(row.id, "tool.end", "bot")]
    assert len(ends) == 2 and "query_artifact" not in ends[0] and ends[1].get("query_artifact")
    tool_msgs = [m for m in script.loop[-1] if isinstance(m, ToolMessage)]
    assert text_of(tool_msgs[0]).startswith("{") and text_of(tool_msgs[1]).startswith("【证据 Q1】")
    [finished] = await events(row.id, "node.finished", "bot")
    assert [(e["kind"], e["call_id"]) for e in finished.data["evidence"]] == [("tool", "call_1"), ("query", "call_1")]
    assert len(script.extract) == 1


# --------------------------------------------------------------------------
# 升级前发起的运行
# --------------------------------------------------------------------------


async def test_runs_from_before_the_upgrade_are_untouched(monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    script = Script(monkeypatch)
    run = await run_manager.start(graph=graph(card=False), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    assert script.extract == [], "老运行不该多出抽取调用"
    tool_msgs = [m for m in script.loop[-1] if isinstance(m, ToolMessage)]
    assert tool_msgs and not any("【证据" in text_of(m) for m in tool_msgs)
    assert all(text_of(m).startswith("{") for m in tool_msgs)

    bot = await output_of(row.id, "bot")
    assert set(bot) == {"text", "steps", "tool_calls", "model"}
    assert all(set(t) == {"tool", "args", "ok", "duration_ms", "result"} for t in bot["tool_calls"])
    assert row.output["kpi"] == json.dumps(FINAL, ensure_ascii=False, indent=2)
    [finished] = await events(row.id, "node.finished", "bot")
    assert "evidence" not in finished.data
    assert all("query_artifact" not in e.data for e in await events(row.id, "tool.end", "bot"))
    assert not logs(await events(row.id), "agent_field_mismatch")


# --------------------------------------------------------------------------
# 抽取的形状
# --------------------------------------------------------------------------


def test_cited_schema_wraps_every_scalar_field():
    from app.engine.nodes.llm import cited_schema

    schema = cited_schema({"type": "object", "properties": {
        "gmv": {"type": "number", "description": "销售额"},
        "meta": {"type": "object", "properties": {"week": {"type": "string"}}},
        "tags": {"type": "array", "items": {"type": "string"}}}})
    gmv = schema["properties"]["gmv"]
    assert gmv["required"] == ["value", "from"]
    assert {"type": "null"} in gmv["properties"]["value"]["anyOf"]
    assert gmv["properties"]["value"]["anyOf"][0] == {"type": "number", "description": "销售额"}
    source = gmv["properties"]["from"]["anyOf"]
    assert {"type": "null"} in source
    assert source[0]["required"] == ["call", "row", "column"]
    week = schema["properties"]["meta"]["properties"]["week"]
    assert week["required"] == ["value", "from"]
    assert schema["title"] and schema["type"] == "object"


# --------------------------------------------------------------------------
# 数组字段按行映射
# --------------------------------------------------------------------------

WEEKLY = "SELECT week, SUM(amount) AS amount FROM orders GROUP BY week ORDER BY week"
ROW_SCHEMA = {"type": "object", "properties": {
    "weeks": {"type": "array", "items": {"type": "object", "properties": {
        "week": {"type": "string"}, "amount": {"type": "number"}}}},
    "labels": {"type": "array", "items": {"type": "string"}},
    "far": {"type": "array", "items": {"type": "string"}},
}}
ROW_EXTRACTED = {
    "weeks": {"value": [{"week": "2026-W36", "amount": 50}, {"week": "2026-W37", "amount": 600.5}],
              "from": {"call": "Q1", "rows": "0-1", "columns": {"week": "week", "amount": "amount"}}},
    "labels": {"value": ["2026-W36", "上周"], "from": {"call": "Q1", "rows": "0-1", "column": "week"}},
    "far": {"value": ["x"], "from": {"call": "Q1", "rows": "0-5", "column": "week"}},
}


async def test_array_fields_map_to_a_range_of_rows(monkeypatch):
    Script(monkeypatch, ROW_EXTRACTED, sqls=[WEEKLY])
    g = graph(card=False, output_schema=ROW_SCHEMA)
    g["nodes"].insert(2, node("card", "metrics", metrics=[
        {"id": "latest", "name": "本周", "format": "thousands", "decimals": 1, "expression": "vars.kpi.weeks[1].amount"}]))
    g["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(g["nodes"], g["nodes"][1:])]
    run = await run_manager.start(graph=g, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    bot = await output_of(row.id, "bot")
    q1 = (await events(row.id, "tool.end", "bot"))[0].data["query_artifact"]
    assert bot["data"]["weeks"] == [{"week": "2026-W36", "amount": 50.0}, {"week": "2026-W37", "amount": 600.5}]
    weeks = bot["data_evidence"]["weeks"]
    assert weeks["status"] == "verified" and weeks["ref"] == "Q1.r0-1"
    assert weeks["locator"] == {"rows": [0, 1], "columns": {"week": "week", "amount": "amount"}}
    assert weeks["eid"] == make_eid("rows", q1, weeks["locator"])
    # 以快照为准：标签第二个模型写成「上周」，按快照取 2026-W37
    assert bot["data"]["labels"] == ["2026-W36", "2026-W37"]
    assert bot["data_evidence"]["labels"]["status"] == "mismatch"
    assert bot["data_evidence"]["labels"]["model_value"] == ["2026-W36", "上周"]
    assert bot["data"]["far"] is None and bot["data_evidence"]["far"]["status"] == "unresolved"
    assert "只有 2 行" in bot["data_evidence"]["far"]["reason"]

    # 口径卡取数组里的一个元素：落到快照里的那一格
    card = await output_of(row.id, "card")
    [src] = card["metrics"][0]["inputs"]
    assert src["via"] == "agent_field" and src["field"] == "weeks[1].amount" and src["status"] == "verified"
    assert src["locator"] == {"row": 1, "column": "amount"}
    assert src["eid"] == make_eid("cell", q1, {"row": 1, "column": "amount"})
    assert card["metrics"][0]["value"] == 600.5


def test_cited_schema_for_arrays():
    from app.engine.nodes.llm import cited_schema

    schema = cited_schema(ROW_SCHEMA)
    weeks = schema["properties"]["weeks"]["properties"]["from"]["anyOf"][0]
    assert weeks["required"] == ["call", "rows", "columns"]
    assert weeks["properties"]["columns"]["required"] == ["week", "amount"]
    labels = schema["properties"]["labels"]["properties"]["from"]["anyOf"][0]
    assert labels["required"] == ["call", "rows", "column"]


async def test_an_array_element_is_checked_on_its_own(monkeypatch):
    """整组对不上，不等于每个元素都对不上：口径卡取的那一个元素按它自己那一格核对。"""
    schema = {"type": "object", "properties": {"weeks": ROW_SCHEMA["properties"]["weeks"]}}
    extracted = {"weeks": {"value": [{"week": "2026-W36", "amount": 51}, {"week": "2026-W37", "amount": 600.5}],
                           "from": {"call": "Q1", "rows": "0-1", "columns": {"week": "week", "amount": "amount"}}}}
    Script(monkeypatch, extracted, sqls=[WEEKLY])
    g = graph(card=False, output_schema=schema)
    g["nodes"].insert(2, node("card", "metrics", metrics=[
        {"id": "latest", "name": "本周", "format": "thousands", "decimals": 1, "expression": "vars.kpi.weeks[1].amount"},
        {"id": "first", "name": "上周", "format": "thousands", "decimals": 1, "expression": "vars.kpi.weeks[0].amount"},
        {"id": "label", "name": "周", "format": "plain", "expression": "len(vars.kpi.weeks[1].week)"}]))
    g["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(g["nodes"], g["nodes"][1:])]
    run = await run_manager.start(graph=g, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    bot = await output_of(row.id, "bot")
    assert bot["data_evidence"]["weeks"]["status"] == "mismatch"          # 整组：第 0 个元素报错了
    q1 = (await events(row.id, "tool.end", "bot"))[0].data["query_artifact"]
    metrics = {m["id"]: m for m in (await output_of(row.id, "card"))["metrics"]}
    [latest] = metrics["latest"]["inputs"]
    assert latest["status"] == "verified" and "model_value" not in latest
    assert latest["locator"] == {"row": 1, "column": "amount"}
    assert latest["eid"] == make_eid("cell", q1, {"row": 1, "column": "amount"})
    [first] = metrics["first"]["inputs"]
    assert first["status"] == "mismatch" and first["model_value"] == 51
    assert metrics["first"]["value"] == 50.0                                 # 值以快照为准
    [label] = metrics["label"]["inputs"]
    assert label["status"] == "verified" and label["locator"] == {"row": 1, "column": "week"}
