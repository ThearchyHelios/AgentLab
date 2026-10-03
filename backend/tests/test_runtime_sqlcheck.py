"""运行时的 SQL 检查：数据源查询工具每次执行成功后对照数据目录检查一遍。

- 结果写进查询结果（模型看得到，Agent 能据此改写 SQL）和查询快照的 checks 字段；没查出问题时不加这个键，
  快照和以前一字不差。检查出错、超时都不影响查询本身。
- 证据接口的查询步骤带上 checks（不带给模型的那句 for_model）。
- 契约指标的来源查询有 error 级问题：口径卡把这个指标标 sql_check_failed 并写明原因，出具按 gaps 降档；
  写作目录提醒写作者，证据面板的指标步骤标出来。和阶段 0 的「结果不完整」走同一条路。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data import catalog
from app.data.engine import engines
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import iter_segments
from app.engine.issuance import decide_tier, sql_check_gaps
from app.engine.runner import run_manager
from app.main import app
from app.tools.datasource import build_datasource_tools
from app.tools.registry import ToolContext
from tests.fixtures.catalog import scenic_notes

FANOUT_SQL = ("SELECT SUM(o.total_amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id "
              "WHERE o.status = 1")
CLEAN_SQL = "SELECT SUM(i.amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1"
REASON = ("所依据的查询未通过 SQL 检查（「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算），"
          "结果不可靠")

_CACHE: dict[str, dict] = {}


async def _schema(path: str) -> dict:
    if path not in _CACHE:
        probe = SimpleNamespace(id=f"probe-{uuid.uuid4().hex[:6]}", name="scenic", kind="sqlite", database=path,
                                host=None, port=None, username=None, password=None, options={}, readonly=True,
                                description="", schema_cache={}, origin="manual")
        try:
            _CACHE[path] = await introspect(probe)
        finally:
            await engines.invalidate(probe.id)
    return _CACHE[path]


async def _source(path: str, *, with_catalog: bool = True) -> tuple[str, str]:
    name = f"scenic_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=path, readonly=True, enabled=True)
        row.schema_cache = await _schema(path)
        session.add(row)
        await session.commit()
        if with_catalog:
            for table, notes in scenic_notes.notes().items():
                await catalog.write_entry(session, row.id, table, notes, if_version=0, actor="王敏")
        return row.id, name


@pytest.fixture
async def scenic_source(scenic_db):
    source_id, name = await _source(scenic_db)
    yield name
    await engines.invalidate(source_id)


async def _query_tool(name: str):
    ctx = ToolContext(run_id=uuid.uuid4().hex, node_id="n1", data_versions=None)
    async with SessionLocal() as session:
        [tool] = await build_datasource_tools([f"db_query__{name}"], ctx, session)
    return tool


# --------------------------------------------------------------------------
# 数据源查询工具
# --------------------------------------------------------------------------


async def test_query_result_and_snapshot_carry_checks(scenic_source):
    tool = await _query_tool(scenic_source)
    result = json.loads(await tool.coroutine(sql=FANOUT_SQL))
    assert result["rows"] and result["artifact"]
    [check] = result["checks"]
    assert check["code"] == "fanout_sum" and check["level"] == "error" and check["table"] == "orders"
    assert check["message"].startswith("「订单」关联「订单明细」是一对多") and "order_items" in check["for_model"]
    assert artifact_store.load(result["artifact"])["checks"] == result["checks"]

    clean = json.loads(await tool.coroutine(sql=CLEAN_SQL))
    assert "checks" not in clean and "checks" not in artifact_store.load(clean["artifact"])


async def test_sources_without_catalog_are_not_checked(scenic_db):
    source_id, name = await _source(scenic_db, with_catalog=False)
    try:
        result = json.loads(await (await _query_tool(name)).coroutine(sql=FANOUT_SQL))
        assert result["rows"] and "checks" not in result
    finally:
        await engines.invalidate(source_id)


async def test_a_failing_or_slow_check_never_breaks_the_query(scenic_source, monkeypatch):
    from app.data import sqlcheck
    from app.tools import datasource

    tool = await _query_tool(scenic_source)

    def boom(self, sql):
        raise RuntimeError("规则缺陷")

    monkeypatch.setattr(sqlcheck.SqlChecker, "check", boom)
    result = json.loads(await tool.coroutine(sql=FANOUT_SQL))
    assert result["rows"] and "checks" not in result

    def slow(self, sql):
        time.sleep(1.0)
        return []

    monkeypatch.setattr(sqlcheck.SqlChecker, "check", slow)
    monkeypatch.setattr(datasource, "SQL_CHECK_TIMEOUT_S", 0.05)
    started = time.monotonic()
    result = json.loads(await tool.coroutine(sql=FANOUT_SQL))
    assert result["rows"] and "checks" not in result
    assert time.monotonic() - started < 0.9, "检查超时就放弃，不等它跑完"


# --------------------------------------------------------------------------
# 出具：契约指标的来源查询有 error 级问题时降档
# --------------------------------------------------------------------------


def test_sql_check_failures_become_readable_gaps():
    metrics = [{"id": "gmv", "name": "订单金额", "value": 1, "sql_check_failed": True, "sql_check_reason": REASON},
               {"id": "n", "name": "订单数", "value": 2}]
    assert sql_check_gaps(metrics) == [f"指标「订单金额」{REASON}"]
    assert sql_check_gaps(metrics[1:]) == []
    tier = decide_tier(missing_required=[], missing_expected=[], unmatched=[], strict=False,
                       gaps=sql_check_gaps(metrics))
    assert tier == "degraded"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(nodes: list[dict]) -> dict:
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


GMV = {"id": "gmv", "name": "订单金额", "unit": "元", "format": "plain", "expression": "cell(nodes.fetch, 0, 'gmv')"}
#: 同一份结果经数据整形解析后按下标取：来历记成 transform，要顺着取值链找到那份查询结果
GMV2 = {"id": "gmv2", "name": "订单金额（整形后）", "unit": "元", "format": "plain",
        "expression": "vars.res.rows[0][0]"}
WEEKLY = "本周订单金额 [[m:gmv]]。"


def weekly(source: str, sql: str) -> dict:
    return chain([
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": sql}),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="res"),
        node("card", "metrics", caliber="订单口径", caliber_version="v1", metrics=[GMV, GMV2]),
        node("write", "report", instructions="写订单周报"),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"]}),
    ])


class Writer:
    """报告写作者：每次都照 text 写；记下看到的提示。"""

    def __init__(self, monkeypatch, text: str = WEEKLY):
        from app.providers import mock_model

        self.prompts: list[str] = []

        def decide(model, messages):
            self.prompts.append("\n".join(str(m.content) for m in messages))
            return AIMessage(content=text)

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没有结束：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def output_of(run_id: str, node_id: str) -> dict:
    [finished] = await events(run_id, "node.finished", node_id)
    return artifact_store.load(finished.data["artifact"])


async def test_a_fanout_behind_a_contract_metric_degrades_the_issuance(scenic_source, engine_up, monkeypatch):
    writer = Writer(monkeypatch)
    run = await run_manager.start(graph=weekly(scenic_source, FANOUT_SQL), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    card = await output_of(row.id, "card")
    metrics = {m["id"]: m for m in card["metrics"]}
    [query_end] = [e.data for e in await events(row.id, "tool.end", "fetch")]
    for mid in ("gmv", "gmv2"):
        assert metrics[mid]["sql_check_failed"] is True and metrics[mid]["sql_check_reason"] == REASON
        assert metrics[mid]["sql_check_sources"] == [{"artifact": query_end["query_artifact"],
                                                     "codes": ["fanout_sum"]}]
    assert "（存疑：所依据的查询未通过 SQL 检查）" in card["text"]
    warns = [e.data for e in await events(row.id, "log", "card") if e.data.get("code") == "metric_sql_check"]
    assert [w["message"] for w in warns] == [f"指标「订单金额」{REASON}", f"指标「订单金额（整形后）」{REASON}"]

    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded", issuance
    assert f"指标「订单金额」{REASON}" in issuance["gaps"]
    # 写作目录提醒写作者：这个数存疑，引用时要说明
    assert "所依据的查询未通过 SQL 检查，结果存疑" in writer.prompts[0]

    # 证据面板：指标步骤标出来，查询步骤带着检查结果（不带给模型的那句）
    doc = artifact_store.load(row.output["_evidence"]["doc_artifact"])
    assert doc["catalog"]["m:gmv"]["sql_check_failed"] is True
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:gmv")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    metric = body["chain"][0]
    assert metric["step"] == "metric" and metric["sql_check_failed"] is True
    assert metric["sql_check_reason"] == REASON
    [query] = [s for s in body["chain"] if s["step"] == "query"]
    [check] = query["checks"]
    assert check["code"] == "fanout_sum" and check["level"] == "error" and check["table"] == "orders"
    assert check["message"].startswith("「订单」关联「订单明细」是一对多") and "for_model" not in check


async def test_the_same_card_on_a_clean_query_issues_formally(scenic_source, engine_up, monkeypatch):
    Writer(monkeypatch)
    run = await run_manager.start(graph=weekly(scenic_source, CLEAN_SQL), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    card = await output_of(row.id, "card")
    assert all("sql_check_failed" not in m for m in card["metrics"])
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance
    doc = artifact_store.load(row.output["_evidence"]["doc_artifact"])
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "m:gmv")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    assert "sql_check_failed" not in body["chain"][0]
    assert all("checks" not in s for s in body["chain"] if s["step"] == "query")


async def test_warnings_alone_do_not_degrade(scenic_source, engine_up, monkeypatch):
    """只有 warning（这里是没按订单状态筛）：查询结果和快照里照样带着，但不影响出具档位。"""
    Writer(monkeypatch)
    sql = "SELECT SUM(i.amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id"
    run = await run_manager.start(graph=weekly(scenic_source, sql), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    [query_end] = [e.data for e in await events(row.id, "tool.end", "fetch")]
    snapshot = artifact_store.load(query_end["query_artifact"])
    assert [c["code"] for c in snapshot["checks"]] == ["missing_valid_filter"]
    assert row.output["_issuance"]["tier"] == "formal", row.output["_issuance"]
