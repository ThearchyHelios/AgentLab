"""缺输入的指标也给出链：报告里引用的指标存在、只是这次没有值（missing_input），片段显示「—」。

以前片段接口只给确定性片段出链，这种片段点开只剩一句「缺输入」：说不出是哪个输入空了、
那一格在哪次查询里。现在照样返回指标步骤、它的输入步骤和查询步骤，面板能指着那一格说
「没查到，记为空，没有兜底成 0」。引用本身写错（目录里没有这个指标）的，照旧没有链。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.config import settings
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

SQL = "SELECT SUM(amount) AS gmv, COUNT(*) AS orders, NULL AS refunds FROM orders"
TEXT = "销售额 [[m:gmv]]，退款率 [[m:refund_rate]]，新客 [[m:nope]]。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL);"
                     "INSERT INTO orders VALUES (1, 100.5), (2, 200.0);")
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly() -> dict:
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop", args={"sql": SQL}),
        # 缺输入记为空值，交给契约判档；不整张卡失败
        node("card", "metrics", caliber="周报口径", on_missing="null", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "refund_rate", "name": "退款率", "format": "percent_of_ratio", "decimals": 4,
             "expression": "cell(nodes.fetch, 0, 'refunds') / cell(nodes.fetch, 0, 'orders')"}]),
        node("write", "report", instructions="写周报", on_violation="flag"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def run_weekly(monkeypatch) -> tuple[Run, dict]:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=TEXT))
    run = await run_manager.start(graph=weekly(), input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    doc_id = row.output["_evidence"]["doc_artifact"]
    doc = json.loads((settings.data_dir / "artifacts" / doc_id[:2] / f"{doc_id}.json").read_text("utf-8"))
    return row, doc


def seg_by_ref(doc: dict, ref: str) -> dict:
    return next(s for s in iter_segments(doc) if s.get("ref") == ref)


async def segment(client, run_id: str, seg_id: str) -> dict:
    resp = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_metric_missing_its_input_still_opens_its_chain(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    seg = seg_by_ref(doc, "m:refund_rate")
    assert seg["text"] == "—" and seg["state"] != "deterministic" and seg["cite"]["status"] == "unresolved"
    body = await segment(client, row.id, seg["id"])
    metric, refunds, orders, query = body["chain"]
    assert metric["step"] == "metric" and metric["alias"] == "m:refund_rate" and metric["metric"] == "refund_rate"
    assert metric["status"] == "missing_input" and metric["value"] is None and metric["rendered"] == "—"
    assert metric["eid_ok"] is True and metric["hash_ok"] is True and metric["sealed"] is True
    assert refunds["step"] == "input" and refunds["via"] == "tool_cell" and refunds["value"] is None
    assert refunds["locator"] == {"row": 0, "column": "refunds"} and refunds["cell"] == "Q1.r0.refunds"
    assert orders["value"] == 2
    async with SessionLocal() as session:
        [end] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "tool.end", RunEvent.node_id == "fetch"))).scalars()
    assert query["step"] == "query" and query["artifact"] == end.data["query_artifact"]
    assert query["highlight"]["cells"] == [[0, "refunds"], [0, "orders"]]
    assert query["rows"] == [[300.5, 2, None]]
    # 片段本身的说法不变：引用解析不了，缺输入
    assert "缺输入" in body["note"] and body["segment"]["cite"]["status"] == "unresolved"


async def test_a_wrong_reference_still_has_no_chain(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    body = await segment(client, row.id, seg_by_ref(doc, "m:nope")["id"])
    assert body["chain"] == [] and "目录里没有指标" in body["note"]


async def test_resolved_metrics_are_unchanged(client, monkeypatch):
    row, doc = await run_weekly(monkeypatch)
    body = await segment(client, row.id, seg_by_ref(doc, "m:gmv")["id"])
    metric, source, query = body["chain"]
    assert metric["status"] == "ok" and metric["render_ok"] is True and source["value"] == 300.5
    assert query["highlight"]["cells"] == [[0, "gmv"]]
