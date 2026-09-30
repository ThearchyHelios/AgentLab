"""证据接口的实体步骤和引文步骤：点开报告里的表名、字段名、原话，看得到它们的出处。

- 表名、字段名：出现在哪几次查询里（SQL 用到的表、结果的列），表结构快照是什么时候同步的、
  字段是什么类型。表结构快照只认封存范围内的事件交回过的那几件（tool.end.schema_artifact、
  node.finished.evidence 的 schema 条目），取回时复验哈希
- 可疑实体（写了一个哪里都找不到的名字）：照实说本次运行的表结构、查询、结果列里都没有它，
  再附上最接近的几个已知名字当提示——提示只从封存的台账里找
- 引文：原话落在哪次检索的哪条命中、原文里的起止，检索快照同样只认封存范围内的
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.core.config import settings
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import (
    UNKNOWN_ENTITY_REASON,
    UNVERIFIED_ENTITY_REASON,
    iter_segments,
    make_eid,
    normalize_quote,
)
from app.engine.runner import run_manager
from app.main import app

SOURCE = "warehouse"
TOOL = f"db_query__{SOURCE}"
SQL = "SELECT region, SUM(amount) AS gmv, COUNT(*) AS order_cnt FROM orders GROUP BY region ORDER BY gmv DESC"
WRITE = "`orders` 里东区最多，达 [[v:Q1.r0.gmv]]，按 [[c:orders.region]] 汇总。`refund_log` 另算。[[see:Q1]]"
HIT = {"chunk_id": "ch-1", "document_id": "doc-1", "title": "售后月报", "ordinal": 3,
       "content": "九月退款主要集中在\n东区，原因是物流延误。"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _warehouse(tmp_path, *, truncated: bool = False) -> str:
    """探查过结构的示例库：orders、refunds 两张表。truncated 装作库里的表太多、快照只存了一部分。"""
    from app.data.introspect import introspect

    path = tmp_path / "warehouse.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL, created_at TEXT);"
        "CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)",
                   [(1, "east", 100.5, "2026-09-01"), (2, "west", 200.0, "2026-09-02"), (3, "east", 300.0, "2026-09-03")])
    db.commit()
    db.close()
    probe = SimpleNamespace(id=f"probe-{path.name}", name=SOURCE, kind="sqlite", database=str(path), host=None,
                            port=None, username=None, password=None, options={}, readonly=True, schema_cache={})
    cache = await introspect(probe)
    if truncated:
        cache.update(truncated=True, total=500)
    await engines.invalidate(probe.id)
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.schema_cache = str(path), "sqlite", True, True, cache
        row.options = {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    return source_id


@pytest.fixture
async def warehouse(tmp_path):
    source_id = await _warehouse(tmp_path)
    yield source_id
    await engines.invalidate(source_id)


@pytest.fixture
async def partial_warehouse(tmp_path):
    source_id = await _warehouse(tmp_path, truncated=True)
    yield source_id
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def script(monkeypatch, text):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def finish(graph) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def reseal(run_id: str) -> None:
    """按改过之后的事件重算封存哈希：模拟「封存链完好、但事件里追不到这件工件」。"""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.seq <= run.manifest_seq).order_by(RunEvent.seq))]
        run.manifest_hash = artifact_store.manifest_hash(rows)
        await session.commit()


async def rewrite(run_id: str, etype: str, node_id: str, change) -> None:
    async with SessionLocal() as session:
        for row in (await session.execute(select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.type == etype, RunEvent.node_id == node_id))).scalars():
            row.data = change(dict(row.data))
        await session.commit()


async def report_run(monkeypatch, text=WRITE) -> tuple[Run, dict]:
    script(monkeypatch, text)
    row = await finish(chain(node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
                             node("write", "report", instructions="写一句"),
                             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"}])))
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked", "write")
    return row, artifact_store.load(checked.data["doc_artifact"])


async def opened(client, run_id: str, seg: dict) -> dict:
    resp = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg['id']}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def seg_where(doc, **match) -> dict:
    return next(s for s in iter_segments(doc) if all(s.get(k) == v for k, v in match.items()))


# --------------------------------------------------------------------------
# 表名、字段名
# --------------------------------------------------------------------------


async def test_a_table_opens_its_schema_snapshot_and_queries(client, warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch)
    [end] = await events(row.id, "tool.end", "fetch")
    schema_art, query_art = end.data["schema_artifact"], end.data["query_artifact"]
    snap = artifact_store.load(schema_art)

    body = await opened(client, row.id, seg_where(doc, kind="entity", ref="t:orders"))
    [step] = body["chain"]
    assert step["step"] == "entity" and step["status"] == "resolved"
    assert step["kind"] == "table" and step["name"] == "orders" and step["alias"] == "t:orders"
    assert step["code"] is True and step["auto"] is True
    assert step["queries"] == ["Q1"] and step["source"] == SOURCE
    assert step["synced_at"] == snap["synced_at"] and step["columns"] == 4 and step["is_view"] is False
    kinds = {s["kind"]: s for s in step["sources"]}
    assert set(kinds) == {"schema", "sql"}
    assert kinds["schema"]["artifact"] == schema_art and kinds["schema"]["sealed"] is True
    assert kinds["schema"]["hash_ok"] is True and kinds["schema"]["present"] is True
    assert kinds["sql"] == {"kind": "sql", "artifact": query_art, "alias": "Q1", "source": SOURCE,
                            "node_id": "fetch", "tool": TOOL, "sealed": True}
    assert step["eid"] == make_eid("table", schema_art, {"table": "orders"}) and step["eid_ok"] is True
    assert step["sealed"] is True and body["seal"]["covered"] is True
    assert "表结构" in body["note"]


async def test_a_column_carries_its_type_and_result_source(client, warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch)
    body = await opened(client, row.id, seg_where(doc, kind="entity", ref="c:orders.region"))
    [step] = body["chain"]
    assert step["kind"] == "column" and step["table"] == "orders" and step["column"] == "region"
    assert step["type"] == "TEXT" and step["type_source"] == "schema" and step["nullable"] is True
    assert step["primary_key"] is False and "code" not in step and "auto" not in step
    assert {(s["kind"], s.get("alias")) for s in step["sources"]} == {("schema", None), ("result", "Q1")}
    assert step["queries"] == ["Q1"] and step["sealed"] is True


async def test_an_aggregate_alias_takes_its_type_from_the_result(client, warehouse, monkeypatch):
    """聚合的别名（gmv）不在表结构里：类型取查询快照记下的列类型，出处是那份查询快照。"""
    row, doc = await report_run(monkeypatch, "东区最多，汇总在 `gmv` 这一列，见 [[v:Q1.r0.gmv]]。[[see:Q1]]")
    [end] = await events(row.id, "tool.end", "fetch")
    body = await opened(client, row.id, seg_where(doc, kind="entity", ref="c:gmv"))
    [step] = body["chain"]
    assert step["kind"] == "column" and step["column"] == "gmv" and step.get("table") is None
    assert step["type"] == "number" and step["type_source"] == "result"
    assert [(s["kind"], s["artifact"], s["sealed"]) for s in step["sources"]] == [
        ("result", end.data["query_artifact"], True)]
    assert step["eid"] == make_eid("column", end.data["query_artifact"], {"column": "gmv"})
    assert step["sealed"] is True


async def test_a_made_up_name_explains_itself_and_suggests_real_ones(client, warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch)
    seg = seg_where(doc, kind="entity", issue="unknown_entity")
    body = await opened(client, row.id, seg)
    [step] = body["chain"]
    assert step["step"] == "entity" and step["status"] == "unknown" and step["name"] == "refund_log"
    assert step["reason"] == UNKNOWN_ENTITY_REASON
    assert 0 < len(step["closest"]) <= 3
    assert step["closest"][0] == {"alias": "t:refunds", "kind": "table", "name": "refunds"}
    assert step["checked"] == {"schemas": 1, "queries": 1}
    assert "疑似不存在的名称" in body["note"]
    assert [v["code"] for v in body["violations"]] == ["unknown_entity"]


async def test_a_made_up_marker_is_suspicious_too(client, warehouse, monkeypatch):
    """[[t:编造]] 和反引号里编的名字一样是可疑实体：名字取自引用，不是占位。"""
    row, doc = await report_run(monkeypatch, "退款看 [[t:refund_logs]]，东区最多 [[v:Q1.r0.gmv]]。[[see:Q1]]")
    body = await opened(client, row.id, seg_where(doc, kind="entity", issue="unknown_entity"))
    [step] = body["chain"]
    assert step["status"] == "unknown" and step["name"] == "refund_logs" and step["kind"] == "table"
    assert step["closest"][0]["alias"] == "t:refunds"


async def test_a_name_a_partial_snapshot_cannot_check_is_not_called_made_up(client, partial_warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch)
    body = await opened(client, row.id, seg_where(doc, kind="entity", issue="unverified_entity"))
    [step] = body["chain"]
    assert step["status"] == "unverified" and step["name"] == "refund_log"
    assert step["reason"] == UNVERIFIED_ENTITY_REASON and "疑似不存在" not in body["note"] and "无法核实" in body["note"]
    table = await opened(client, row.id, seg_where(doc, kind="entity", ref="t:orders"))
    assert table["chain"][0]["snapshot_truncated"] is True
    assert {s["kind"]: s for s in table["chain"][0]["sources"]}["schema"]["truncated"] is True


async def test_suggestions_come_only_from_the_sealed_ledger(client, warehouse, monkeypatch):
    """台账里的 schema 条目在封存的事件里被去掉了：提示里就不能再有只在那份快照里的表。"""
    row, doc = await report_run(monkeypatch)
    await rewrite(row.id, "node.finished", "fetch",
                  lambda d: {**d, "evidence": [e for e in d["evidence"] if e["kind"] != "schema"]})
    await reseal(row.id)
    body = await opened(client, row.id, seg_where(doc, kind="entity", issue="unknown_entity"))
    [step] = body["chain"]
    assert all(c["alias"] != "t:refunds" for c in step["closest"]), step["closest"]
    assert step["checked"]["schemas"] == 0


async def test_a_schema_snapshot_outside_the_seal_is_not_trusted(client, warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch)
    [end] = await events(row.id, "tool.end", "fetch")
    schema_art = end.data["schema_artifact"]
    await rewrite(row.id, "tool.end", "fetch", lambda d: {k: v for k, v in d.items() if k != "schema_artifact"})
    await rewrite(row.id, "node.finished", "fetch",
                  lambda d: {**d, "evidence": [e for e in d["evidence"] if e["kind"] != "schema"]})
    await reseal(row.id)

    body = await opened(client, row.id, seg_where(doc, kind="entity", ref="c:orders.region"))
    [step] = body["chain"]
    schema = next(s for s in step["sources"] if s["kind"] == "schema")
    assert schema["artifact"] == schema_art and schema["sealed"] is False
    assert "hash_ok" not in schema and "present" not in schema
    # 类型、同步时间只能从封存的快照取：取不到就不给，不从工件库里「顺手」读
    assert "type" not in step and "synced_at" not in step
    assert step["sealed"] is False and body["seal"]["covered"] is False
    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert {e["alias"]: e["sealed"] for e in graph["evidence"]}["c:orders.region"] is False


async def test_a_tampered_schema_snapshot_is_reported(client, warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch)
    [end] = await events(row.id, "tool.end", "fetch")
    art = end.data["schema_artifact"]
    path = settings.data_dir / "artifacts" / art[:2] / f"{art}.json"
    content = json.loads(path.read_text("utf-8"))
    content["tables"]["orders"]["columns"][1]["type"] = "INTEGER"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    body = await opened(client, row.id, seg_where(doc, kind="entity", ref="c:orders.region"))
    [step] = body["chain"]
    schema = next(s for s in step["sources"] if s["kind"] == "schema")
    assert schema["sealed"] is True and schema["hash_ok"] is False
    assert "type" not in step and step["sealed"] is False


# --------------------------------------------------------------------------
# 引文
# --------------------------------------------------------------------------


async def quote_run(monkeypatch, text="售后说「[[q:K1|退款主要集中在东区]]」。[[see:K1]]") -> tuple[Run, dict]:
    from app.memory import kb

    async def fake_search(session, **kw):
        return [dict(HIT, score=0.9)]

    monkeypatch.setattr(kb, "search", fake_search)
    script(monkeypatch, text)
    row = await finish(chain(node("start", "input"), node("docs", "retrieve", query="退款", collection="kb"),
                             node("write", "report", instructions="引用售后的原话"),
                             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"}])))
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked", "write")
    return row, artifact_store.load(checked.data["doc_artifact"])


async def test_a_quote_opens_the_hit_it_was_copied_from(client, monkeypatch):
    row, doc = await quote_run(monkeypatch)
    [end] = await events(row.id, "retrieve.end", "docs")
    body = await opened(client, row.id, seg_where(doc, kind="quote"))
    [step] = body["chain"]
    assert step["step"] == "quote" and step["alias"] == "K1" and step["text"] == "退款主要集中在东区"
    assert step["source"] == {"artifact": end.data["artifact"], "document": "doc-1", "chunk": "ch-1",
                              "title": "售后月报", "ordinal": 3}
    match = step["match"]
    assert match["hit"] == 0 and step["content"] == HIT["content"]
    assert normalize_quote(step["content"][match["start"]:match["end"]]) == normalize_quote(step["text"])
    assert step["collection"] == "kb" and step["query"] == "退款" and step["node_id"] == "docs"
    assert step["hash_ok"] is True and step["match_ok"] is True and step["eid_ok"] is True
    assert step["sealed"] is True and body["seal"]["covered"] is True
    assert "逐字" in body["note"]


async def test_a_retrieval_outside_the_seal_is_not_shown(client, monkeypatch):
    row, doc = await quote_run(monkeypatch)
    await rewrite(row.id, "retrieve.end", "docs", lambda d: {k: v for k, v in d.items() if k != "artifact"})
    await rewrite(row.id, "node.finished", "docs",
                  lambda d: {**d, "evidence": [e for e in d.get("evidence") or [] if e["kind"] != "retrieval"]})
    await reseal(row.id)
    body = await opened(client, row.id, seg_where(doc, kind="quote"))
    [step] = body["chain"]
    assert step["sealed"] is False and step["content"] is None and step["note"]
    assert body["seal"]["covered"] is False


async def test_an_unmatched_quote_has_no_chain(client, monkeypatch):
    row, doc = await quote_run(monkeypatch, "售后说「[[q:K1|退款主要集中在西区]]」。[[see:K1]]")
    seg = seg_where(doc, kind="quote")
    assert seg["state"] == "none"
    body = await opened(client, row.id, seg)
    assert body["chain"] == [] and "找不到这段引文的原文" in body["note"]
