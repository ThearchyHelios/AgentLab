"""证据接口二期：片段链接上查询步骤，点开一个数一路追到快照里的那一格。

- 指标步骤、输入步骤之后接查询步骤：{step:"query", alias, tool, sql, columns, rows, row_offset, total_rows,
  truncated, highlight:{rows, cols}, artifact, hash_ok}
- [[v:]] 片段（含整表里的格）直接给出查询步骤
- rows 只返回被引用的行加前后各 2 行；完整快照仍走 /api/artifacts/{id}
- 数据源 options.mask_columns 里的列一律换成「已遮罩」，并列进 redacted.columns
- 只认封存范围内的事件（tool.end.query_artifact、node.finished.evidence 的 query 条目）追得到的快照
- 证据图的 evidence 列表里有查询、检索条目
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.core.config import settings
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

SQL = "SELECT id, week, amount, email FROM orders ORDER BY id"
ROWS = [(i, "2026-W37" if i > 3 else "2026-W36", float(i * 100) + 0.5, f"u{i}@example.com") for i in range(1, 11)]
TEXT = ("这一单金额 [[v:Q1.r5.amount]]，口径卡记为 [[m:gmv]]。[[see:K1]]\n\n"
        "[[table:Q1 cols=id,amount rows=4-6]]")


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None):
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, amount REAL, email TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    source_id = await set_source(path)
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def weekly():
    return chain(
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop", args={"sql": SQL}),
        node("docs", "retrieve", query="退款口径", collection="docs"),
        node("card", "metrics", caliber="周报口径", caliber_version="v2", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands",
             "expression": "cell(nodes.fetch, 5, 'amount')"}]),
        node("write", "report", instructions="写周报", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )


def script(monkeypatch, text=TEXT):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


def load_doc(doc_id: str) -> dict:
    return json.loads((settings.data_dir / "artifacts" / doc_id[:2] / f"{doc_id}.json").read_text("utf-8"))


async def run_weekly(monkeypatch, graph=None, text=TEXT) -> tuple[Run, dict]:
    from app.memory import kb

    async def fake_search(session, **kw):
        return [{"title": "口径说明", "ordinal": 1, "content": "退款按发生周统计", "score": 0.9, "document_id": "d1"}]

    monkeypatch.setattr(kb, "search", fake_search)
    script(monkeypatch, text)
    row = await wait((await run_manager.start(graph=graph or weekly(), input_payload={})).id)
    assert row.status == "succeeded", row.error
    return row, load_doc(row.output["_evidence"]["doc_artifact"])


def seg_of(doc: dict, text: str, n: int = 0) -> dict:
    return [s for s in iter_segments(doc) if s["text"] == text and s.get("cite")][n]


async def query_artifact(run_id: str, node_id: str = "fetch") -> str:
    async with SessionLocal() as session:
        [end] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "tool.end", RunEvent.node_id == node_id))).scalars()
    return end.data["query_artifact"]


async def segment(client, run_id: str, seg_id: str) -> dict:
    resp = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# 查询步骤的形状、窗口、高亮
# --------------------------------------------------------------------------


async def test_a_cell_segment_opens_the_query_step(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    artifact = await query_artifact(row.id)
    body = await segment(client, row.id, seg_of(doc, "600.5")["id"])

    [step] = body["chain"]
    assert step["step"] == "query" and step["alias"] == "Q1" and step["tool"] == "db_query__shop"
    assert step["sql"] == SQL and step["columns"] == ["id", "week", "amount", "email"]
    assert step["artifact"] == artifact and step["hash_ok"] is True and step["sealed"] is True
    assert step["node_id"] == "fetch" and step["source"] == "shop"
    assert step["total_rows"] == 10 and step["truncated"] is False
    assert step["column_types"] == {"id": "number", "week": "text", "amount": "number", "email": "text"}
    # 第 5 行（从 0 数）加前后各 2 行
    assert step["row_offset"] == 3 and step["row_index"] == [3, 4, 5, 6, 7]
    assert [r[0] for r in step["rows"]] == [4, 5, 6, 7, 8]
    assert step["highlight"] == {"rows": [5], "cols": ["amount"], "cells": [[5, "amount"]]}
    assert body["seal"]["covered"] is True
    assert body["redacted"] == {"columns": []}
    assert "查询快照" in body["note"]


async def test_the_window_is_clipped_at_both_ends(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch, text="首行 [[v:Q1.r0.amount]]，末行 [[v:Q1.r9.amount]]。")
    first = (await segment(client, row.id, seg_of(doc, "100.5")["id"]))["chain"][0]
    assert first["row_offset"] == 0 and first["row_index"] == [0, 1, 2]
    last = (await segment(client, row.id, seg_of(doc, "1,000.5")["id"]))["chain"][0]
    assert last["row_index"] == [7, 8, 9] and last["highlight"]["rows"] == [9]


async def test_table_cells_open_the_same_query_step(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    cell = seg_of(doc, "600.5", 1)          # 整表里第 5 行 amount 那一格
    assert cell["cite"]["kind"] == "cell"
    [step] = (await segment(client, row.id, cell["id"]))["chain"]
    assert step["alias"] == "Q1" and step["highlight"]["cells"] == [[5, "amount"]]


async def test_a_metric_chain_ends_in_the_query_step(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    artifact = await query_artifact(row.id)
    metric, source, query = (await segment(client, row.id, seg_of(doc, "600.5元")["id"]))["chain"]
    assert metric["step"] == "metric" and metric["alias"] == "m:gmv"
    assert source["step"] == "input" and source["via"] == "tool_cell" and source["status"] == "verified"
    assert source["artifact"] == artifact and source["locator"] == {"row": 5, "column": "amount"}
    assert source["query"] == "Q1" and source["cell"] == "Q1.r5.amount"
    assert query["step"] == "query" and query["artifact"] == artifact
    assert query["highlight"]["cells"] == [[5, "amount"]] and query["row_index"] == [3, 4, 5, 6, 7]


# --------------------------------------------------------------------------
# 遮罩
# --------------------------------------------------------------------------


async def test_masked_columns_are_redacted(client, monkeypatch, shop):
    await set_source(shop, {"mask_columns": ["EMAIL"]})
    row, doc = await run_weekly(monkeypatch)
    body = await segment(client, row.id, seg_of(doc, "600.5")["id"])
    [step] = body["chain"]
    email = step["columns"].index("email")
    assert {r[email] for r in step["rows"]} == {"已遮罩"}
    assert step["rows"][2][2] == 600.5 and step["masked"] == ["email"]
    assert body["redacted"]["columns"] == ["email"]
    assert "不是安全边界" in body["redacted"]["note"]


async def test_mask_columns_written_as_text_are_understood(client, monkeypatch, shop):
    """数据源表单的「高级连接参数」存的是文本：「email, week」和列表是一个意思。"""
    await set_source(shop, {"mask_columns": "email，week"})
    row, doc = await run_weekly(monkeypatch)
    body = await segment(client, row.id, seg_of(doc, "600.5")["id"])
    assert body["redacted"]["columns"] == ["week", "email"]


async def rename_source(old: str, new: str) -> None:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == old))).scalar_one()
        row.name = new
        await session.commit()


async def test_masks_recorded_at_query_time_survive_a_renamed_source(client, monkeypatch, shop):
    """数据源事后改名、删掉：面板按查询当时记下的遮罩处理，不能因为找不到数据源就把原值全亮出来。"""
    await set_source(shop, {"mask_columns": ["email"]})
    row, doc = await run_weekly(monkeypatch)
    artifact = await query_artifact(row.id)
    assert load_doc(artifact)["mask_columns"] == ["email"]
    await rename_source("shop", "shop_renamed")
    try:
        body = await segment(client, row.id, seg_of(doc, "600.5")["id"])
    finally:
        await rename_source("shop_renamed", "shop")
    [step] = body["chain"]
    email = step["columns"].index("email")
    assert {r[email] for r in step["rows"]} == {"已遮罩"} and step["masked"] == ["email"], step
    assert body["redacted"]["columns"] == ["email"]
    assert "「shop」" in step["mask_note"] and "查询当时" in step["mask_note"], step


async def test_masks_added_after_the_query_still_apply(client, monkeypatch, shop):
    row, doc = await run_weekly(monkeypatch)
    assert "mask_columns" not in load_doc(await query_artifact(row.id))     # 没设遮罩的快照和原来一字不差
    await set_source(shop, {"mask_columns": ["week"]})
    [step] = (await segment(client, row.id, seg_of(doc, "600.5")["id"]))["chain"]
    assert step["masked"] == ["week"] and "mask_note" not in step


async def test_recorded_and_current_masks_are_combined(client, monkeypatch, shop):
    """查询时遮 email、事后改成遮 week：两列都遮。事后把某列从遮罩里拿掉，旧证据也不因此亮出来。"""
    await set_source(shop, {"mask_columns": ["email"]})
    row, doc = await run_weekly(monkeypatch)
    await set_source(shop, {"mask_columns": ["week"]})
    [step] = (await segment(client, row.id, seg_of(doc, "600.5")["id"]))["chain"]
    assert step["masked"] == ["week", "email"], step


async def test_a_vanished_source_without_masks_is_named_but_rows_stay(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    await rename_source("shop", "shop_gone")
    try:
        [step] = (await segment(client, row.id, seg_of(doc, "600.5")["id"]))["chain"]
    finally:
        await rename_source("shop_gone", "shop")
    assert step["masked"] == [] and step["rows"][2][2] == 600.5 and "「shop」" in step["mask_note"], step


async def test_the_model_does_not_see_the_mask_record(monkeypatch, shop):
    """遮罩只管证据面板：模型拿到的查询结果和原来一样，不多一个会让它以为数据被遮了的字段。"""
    await set_source(shop, {"mask_columns": ["email"]})
    row, _ = await run_weekly(monkeypatch)
    async with SessionLocal() as session:
        [end] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "tool.end", RunEvent.node_id == "fetch"))).scalars()
    returned = load_doc(end.data["artifact"])
    assert "mask_columns" not in json.dumps(returned, ensure_ascii=False), returned


# --------------------------------------------------------------------------
# 只认封存范围内追得到的快照
# --------------------------------------------------------------------------


async def strip_query(run_id: str, artifact: str) -> None:
    """从封存范围内的事件里抹掉这份快照的痕迹，再按改过的事件重算封存哈希：
    模拟「封存链完好，但事件里没有这件工件」——目录里写着它，也不能认。"""
    from app.core.artifact_store import manifest_hash

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        for event in (await session.execute(select(RunEvent).where(RunEvent.run_id == run_id))).scalars():
            data = dict(event.data or {})
            if event.type == "tool.end" and data.get("query_artifact") == artifact:
                data.pop("query_artifact")
            elif event.type == "node.finished" and data.get("evidence"):
                data["evidence"] = [e for e in data["evidence"] if e.get("artifact") != artifact]
            else:
                continue
            event.data = data
        await session.commit()
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.seq <= run.manifest_seq).order_by(RunEvent.seq))]
        run.manifest_hash = manifest_hash(rows)
        await session.commit()


async def test_a_snapshot_outside_the_seal_is_not_served(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    artifact = await query_artifact(row.id)
    await strip_query(row.id, artifact)
    # 封存之后追加一条指向它的事件，也不算
    async with SessionLocal() as session:
        session.add(RunEvent(run_id=row.id, seq=row.manifest_seq + 5, type="tool.end", node_id="fetch", ts=0.0,
                             data={"tool": "db_query__shop", "query_artifact": artifact}))
        await session.commit()

    body = await segment(client, row.id, seg_of(doc, "600.5")["id"])
    assert body["seal"]["ok"] is True
    [step] = body["chain"]
    assert step["artifact"] == artifact and step["sealed"] is False
    assert step["rows"] == [] and step["columns"] == [] and step["sql"] is None
    assert "封存" in step["note"] and body["seal"]["covered"] is False


async def test_a_tampered_snapshot_shows_no_rows(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    artifact = await query_artifact(row.id)
    path = settings.data_dir / "artifacts" / artifact[:2] / f"{artifact}.json"
    original = path.read_text("utf-8")
    snap = json.loads(original)
    snap["rows"][5][2] = 9999.5
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    try:
        [step] = (await segment(client, row.id, seg_of(doc, "600.5")["id"]))["chain"]
    finally:
        # 快照按内容寻址、不带运行 id：后面的测试查出同样的结果（同一毫秒跑完）会落到同一个文件上
        path.write_text(original, encoding="utf-8")
    assert step["hash_ok"] is False and step["rows"] == [] and "哈希" in step["note"]


# --------------------------------------------------------------------------
# agent 字段：输入步骤带着核对状态，跳到的是全局编号的查询
# --------------------------------------------------------------------------


async def test_agent_field_inputs_carry_their_check_and_jump_to_the_query(client, monkeypatch):
    from app.providers import mock_model

    schema = {"type": "object", "properties": {"gmv": {"type": "number"}, "orders": {"type": "integer"}}}
    extracted = {"gmv": {"value": 601.5, "from": {"call": "Q1", "row": 0, "column": "gmv"}},
                 "orders": {"value": 30, "from": {"call": "Q1", "row": 0, "column": "orders"}}}
    agg = "SELECT SUM(amount) AS gmv, COUNT(*) AS orders FROM orders WHERE week = '2026-W36'"

    def decide(model, messages):
        if model.response_format:
            return AIMessage(content=json.dumps(extracted))
        if not model.tools:
            return AIMessage(content=TEXT_ORDERS)
        if not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="查。", tool_calls=[{"name": "db_query__shop", "args": {"sql": agg}, "id": "c0"}])
        return AIMessage(content="查完了。")

    TEXT_ORDERS = "订单 [[m:orders]]。"
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    graph = chain(
        node("start", "input"),
        node("bot", "agent", prompt="查", tools=["db_query__shop"], approval="never", output_schema=schema,
             cite_fields=True, assign_to="kpi"),
        node("card", "metrics", caliber="周报口径", metrics=[
            {"id": "orders", "name": "订单", "unit": "单", "format": "thousands", "expression": "vars.kpi.orders"}]),
        node("write", "report", instructions="写", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    )
    row = await wait((await run_manager.start(graph=graph, input_payload={})).id)
    assert row.status == "succeeded", row.error
    doc = load_doc(row.output["_evidence"]["doc_artifact"])
    artifact = await query_artifact(row.id, "bot")
    metric, source, query = (await segment(client, row.id, seg_of(doc, "3单")["id"]))["chain"]
    assert source["via"] == "agent_field" and source["status"] == "mismatch" and source["model_value"] == 30
    assert source["ref"] == "Q1.r0.orders" and source["cell"] == "Q1.r0.orders" and source["query"] == "Q1"
    assert query["artifact"] == artifact and query["highlight"]["cells"] == [[0, "orders"]]
    assert query["rows"] == [[601.5, 3]] and query["row_index"] == [0]


# --------------------------------------------------------------------------
# 证据图
# --------------------------------------------------------------------------


async def test_the_graph_lists_queries_and_retrievals(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    by_alias = {e["alias"]: e for e in body["evidence"]}
    q1, k1 = by_alias["Q1"], by_alias["K1"]
    assert q1["kind"] == "query" and q1["sealed"] is True and q1["node_id"] == "fetch"
    assert q1["tool"] == "db_query__shop" and q1["columns"] == ["id", "week", "amount", "email"]
    assert q1["rows"] == 10 and q1["source"] == "shop"
    assert seg_of(doc, "600.5")["id"] in q1["cited_by"]
    assert k1["kind"] == "retrieval" and k1["sealed"] is True and k1["source"] == "docs" and k1["rows"] == 1
    edges = body["edges"]
    assert {"from": "Q1.r5.amount", "to": "Q1", "rel": "cell_of", "report": "write"} in edges
    [card_input] = [e for e in edges if e["rel"] == "input" and e["from"] == "m:gmv"]
    assert card_input["cell"] == "Q1.r5.amount"


# --------------------------------------------------------------------------
# 数据源的 mask_columns 配置
# --------------------------------------------------------------------------


def test_mask_columns_is_not_a_driver_option():
    """驱动不认识 mask_columns：拼进连接串，连接就会因为未知参数被拒。"""
    from types import SimpleNamespace

    from app.data.engine import build_url, masked_columns

    source = SimpleNamespace(kind="postgres", host="db", port=5432, database="shop", username="u", password=None,
                             options={"mask_columns": ["email"], "sslmode": "disable"})
    assert "mask_columns" not in build_url(source) and "sslmode=disable" in build_url(source)
    assert masked_columns({"mask_columns": " email ,phone、email\nnote"}) == ["email", "phone", "note"]
    assert masked_columns({"mask_columns": ["email", " ", "phone"]}) == ["email", "phone"]
    assert masked_columns({}) == [] and masked_columns(None) == []


async def test_datasource_form_offers_mask_columns_and_refuses_garbage(client):
    kinds = {k["value"]: k for k in (await client.get("/api/datasources/kinds")).json()["kinds"]}
    for kind in kinds.values():
        field = next(a for a in kind["advanced"] if a["key"] == "mask_columns")
        assert "不是安全边界" in field["help"]
    bad = await client.post("/api/datasources", json={"name": "masked", "kind": "sqlite", "database": "/tmp/x.db",
                                                      "options": {"mask_columns": {"email": True}}})
    assert bad.status_code == 422 and "遮罩" in bad.json()["detail"]
