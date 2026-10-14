"""内置模板「⑨ 可追溯周报」：tool 查库 → 口径卡 → 报告撰写 → 出具（引用模式）。

模板既是示范也是活文档：它要能通过 validate_graph，接上一个真实的库（这里用
SQLite 建一个示例 shop 库）要能从头跑到尾、完整出具，报告里每个数都点得开。
口径卡用 cell() 按列名取查询结果的格，每个指标的输入都一路追到查询快照（engine/lineage.py）：
模板教的就是这种写法。模板里只许出现通用示例名。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.seed import TEMPLATES

NAME = "⑨ 可追溯周报"


def template() -> dict:
    return next(t for t in TEMPLATES if t["name"] == NAME)


def graph() -> dict:
    t = template()
    return {"nodes": t["nodes"], "edges": t["edges"]}


def test_template_passes_validation():
    report = validate_graph(GraphSpec.model_validate(graph()))
    assert report.ok, [i.message for i in report.issues if i.level == "error"]
    types = {n["id"]: n["type"] for n in template()["nodes"]}
    assert list(types.values()).count("report") == 1 and "metrics" in types.values()
    out = next(n for n in template()["nodes"] if n["type"] == "output")
    contract = out["data"]["config"]["contract"]
    report_id = next(k for k, v in types.items() if v == "report")
    assert contract["report_from"] == report_id and contract["strict"] is True
    # 成果字段原样引用报告正文，才能逐段对应证据
    assert {"name": "周报", "value": f"{{{{ nodes.{report_id}.text }}}}"} in out["data"]["config"]["fields"]


def test_every_metric_of_the_template_reaches_the_query_snapshot():
    """口径卡的每个指标都用 cell() 取数：静态地认（和应用检查同一套），没有一个输入停在半路。"""
    from app.apps.tree import node_map
    from app.engine.lineage import static_stops

    nodes = node_map(graph())
    [card] = [n for n in nodes.values() if n["type"] == "metrics"]
    metrics = card["config"]["metrics"]
    assert metrics and all("cell(nodes.fetch" in m["expression"] for m in metrics)
    assert {m["id"]: static_stops(nodes, m["expression"]) for m in metrics} == {m["id"]: [] for m in metrics}


async def test_every_metric_of_a_run_is_traced_to_the_snapshot(engine_up, shop, monkeypatch, client):
    """跑一遍：每个指标的输入都是单元格取数、与快照一致；证据接口的指标步骤说输入都追到了原始数据。"""
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda self, messages: AIMessage(content="销售额 [[m:gmv]]。[[see:m:gmv]]"))
    run = await run_manager.start(graph=graph(), input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    for metric in ("gmv", "orders", "wow", "aov", "refund_rate"):
        chain = (await client.get(f"/api/runs/{run.id}/evidence/metrics/caliber/{metric}")).json()["chain"]
        step = chain[0]
        assert step["step"] == "metric" and step["untraced"] == [], (metric, step.get("untraced"))
        inputs = [s for s in chain if s["step"] == "input"]
        assert inputs and all(i["via"] == "tool_cell" and i["status"] == "verified" for i in inputs), metric
        assert chain[-1]["step"] == "query"


def test_template_is_listed_once_and_keeps_template_eight():
    names = [t["name"] for t in TEMPLATES]
    assert names.count(NAME) == 1
    assert any(n.startswith("⑧ 周报") for n in names)     # 老模板不改，免得覆盖用户复制过的那一份


@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, amount REAL, refunded INTEGER);")
    rows = [(i, "2026-W37", 100.0 + i, 1 if i % 10 == 0 else 0) for i in range(1, 41)]
    rows += [(100 + i, "2026-W36", 90.0 + i, 0) for i in range(1, 31)]
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        existing = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if existing is None:
            session.add(DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True))
        else:
            existing.database, existing.kind, existing.enabled = str(path), "sqlite", True
        await session.commit()
    yield


async def test_template_runs_end_to_end_and_issues_formally(engine_up, shop, monkeypatch):
    from app.providers import mock_model

    text = ("## 本周概览\n\n[[i:week]] 销售额 [[m:gmv]]，环比 [[m:wow]]。[[see:m:gmv,m:wow]]\n\n"
            "- 订单 [[m:orders]]，客单价 [[m:aov]]。[[see:m:orders,m:aov]]\n"
            "- 退款率 [[m:refund_rate]]。[[see:m:refund_rate]]")
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))
    run = await run_manager.start(graph=graph(), input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    issuance = row.output["_issuance"]
    assert issuance["mode"] == "citations" and issuance["tier"] == "formal", issuance
    # 40 单，金额 101..140 → 4820；上周 30 单 91..120 → 3165；退款 4 单
    report = row.output["周报"]
    for shown in ("2026-W37", "4,820.00元", "52.3%", "40单", "120.50元", "10.00%"):
        assert shown in report, report
    assert row.output["_evidence"]["fields"] == ["周报"]
    assert issuance["matched_numbers"] == 5 and issuance["unmatched_numbers"] == []
