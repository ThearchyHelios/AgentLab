"""报告导出的接口：GET /api/runs/{run_id}/report.html。

照可验证报告的夹具真跑一次：两个虚构的 SQLite 数据源（门店流水没设遮罩，会员库遮了 phone 列），口径卡、
报告引用两份快照里的格子和一个指标。核对：

- 导出的页面：正文是服务端渲染好的 HTML（数字是带 data-seg 的按钮、表格列表引用代码块都排出来），
  不执行脚本也读得到；数据块能解析回来；
- 出处链：数字的链里指标步骤带着口径（口径卡名、版本）、计算方式（原式、代入式）、输入和复算，
  查询步骤带着 SQL 和被引用的行；同一处引用只算一条（同样的 [[m:gmv]] 共用）；
- 遮罩：会员库上选出了号码的那次查询，行里 phone 列写「已遮罩」；
- 附件名是「<运行名>-报告.html」，中文名按 RFC 5987 编码；
- ?report= 指定报告节点；没有报告文档的运行回 404；
- 报告文档被改过（哈希对不上）回 409；
- 只读：导出前后运行记录、事件一条不多一条不少。
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from app.core.artifact_store import canonical_json
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager
from app.main import app
from tests.test_bundle_api import (  # noqa: F401 - 夹具和搭法照可验证报告的
    CRM_SQL,
    SHOP_SQL,
    TEXT,
    client,
    engine_up,
    file_of,
    sealed_run,
    set_source,
    sources,
    wait,
    weekly,
)

TWO = ("## 门店周报\n\n青石店流水 [[v:Q1.r0.amount]]，会员消费 [[v:Q2.r1.spend]]，口径卡销售额 [[m:gmv]]。"
       "另有 42 笔退款待入账。")

RICH = (TWO + "\n\n- 青石店流水最高\n- 白沙店流水最低\n\n[[table:Q1 cols=store,amount rows=0-1]]")


def data_of(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    assert m, "页面里没有数据块"
    return json.loads(m.group(1))


def seg_button(html: str, seg_id: str) -> str:
    """正文里这个片段的按钮（服务端渲染的）。"""
    m = re.search(rf'<button type="button"[^>]*data-seg="{seg_id}"[^>]*>', html)
    return m.group(0) if m else ""


def all_segments(doc: dict) -> list[dict]:
    return [seg for block in doc.get("blocks") or [] for unit in block.get("units") or []
            for seg in unit.get("segments") or []]


def doc_of_export(page: str) -> dict:
    return json.loads(file_of(data_of(page)["meta"]["doc_artifact"]).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


async def test_export_renders_body_and_embeds_chains(client, monkeypatch):
    row = await sealed_run(monkeypatch, RICH)
    resp = await client.get(f"/api/runs/{row.id}/report.html")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    header = resp.headers["content-disposition"]
    assert header.startswith("attachment;")
    assert f"filename*=UTF-8''{quote('未保存的工作流-报告.html', safe='')}" in header
    assert resp.headers["cache-control"] == "no-store"
    page = resp.text

    # 正文是服务端渲染的：不执行脚本也读得到整份报告
    for text in ("门店周报", "青石店", "1280.5", "1,280.5", "42"):
        assert text in page, f"正文里没有「{text}」"
    assert "<table>" in page and "<ul><li" in page.replace("\n", "")
    assert 'class="num num-ok"' in page and 'class="num num-na"' in page

    data = data_of(page)
    assert data["schema"] == "agentlab.report/1"
    assert data["meta"]["run_id"] == row.id
    assert data["meta"]["report"]["node_id"] == "write"
    assert data["meta"]["doc_hash_ok"] is True
    assert data["meta"]["seal"]["sealed"] is True and data["meta"]["seal"]["ok"] is True

    # 每个有状态的数字、取值都指得到一条链，正文里也画得出按钮
    doc = doc_of_export(page)
    numbers = [s for s in all_segments(doc) if s.get("kind") in ("number", "value") and s.get("id")]
    assert len(numbers) >= 3
    for seg in numbers:
        assert data["refs"][seg["id"]], f"片段 {seg['id']} 没有出处链"
        assert seg_button(page, seg["id"]), f"正文里没有片段 {seg['id']} 的按钮"

    # 指标的链：口径、计算方式（原式、代入式）、输入、复算、查询
    metric_seg = next(s for s in numbers if (s.get("cite") or {}).get("kind") == "metric")
    chain = data["chains"][data["refs"][metric_seg["id"]]]
    metric = next(s for s in chain if s["step"] == "metric")
    assert metric["caliber"] == "周报口径" and metric["version"] == "v2"
    assert metric["expression"] == "cell(nodes.fetch, 0, 'amount')"
    assert metric["substituted"] == "1280.5"
    assert metric["recompute_ok"] is True
    assert metric["sealed"] is True
    inputs = [s for s in chain if s["step"] == "input"]
    assert len(inputs) == 1 and inputs[0]["path"] == "cell(nodes.fetch, 0, 'amount')"
    queries = [s for s in chain if s["step"] == "query"]
    assert len(queries) == 1 and queries[0]["sql"] == SHOP_SQL
    assert queries[0]["rows"] and "青石店" in [r[1] for r in queries[0]["rows"]]
    assert queries[0]["highlight"]["cells"] == [[0, "amount"]]

    # 单元格的链：直接到查询快照
    cell_seg = next(s for s in numbers if (s.get("cite") or {}).get("kind") == "cell")
    chain = data["chains"][data["refs"][cell_seg["id"]]]
    assert [s["step"] for s in chain] == ["query"]
    assert chain[0]["artifact"] == queries[0]["artifact"]

    # 无证据的数字（正文里直接写出的 42）：点得开，链是空的，说明写清楚了
    bare = next(s for s in all_segments(doc) if s.get("issue") == "uncited_number")
    assert data["refs"][bare["id"]]
    assert data["chains"][data["refs"][bare["id"]]] == []
    assert "没有出处" in data["notes"][bare["id"]]
    assert 'data-state="none"' in seg_button(page, bare["id"])


async def test_export_shares_one_chain_per_citation(client, monkeypatch):
    """同一处引用（同样的 [[m:gmv]] 写两遍）共用一条链：数据块里不把整条链抄两遍。"""
    from app.providers import mock_model

    twice = TEXT + " 再说一遍销售额 [[m:gmv]]。"
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=twice))
    row = await wait((await run_manager.start(graph=weekly(), input_payload={})).id)
    assert row.status == "succeeded", row.error
    resp = await client.get(f"/api/runs/{row.id}/report.html")
    assert resp.status_code == 200, resp.text
    page = resp.text
    doc = doc_of_export(page)
    metrics = [s for s in all_segments(doc) if (s.get("cite") or {}).get("kind") == "metric"]
    assert len(metrics) == 2
    data = data_of(page)
    assert data["refs"][metrics[0]["id"]] == data["refs"][metrics[1]["id"]]
    assert len([c for c in data["chains"].values() if c and c[0].get("step") == "metric"]) == 1


async def test_export_masks_masked_columns(client, monkeypatch):
    """会员库遮了 phone 列：查询步骤的行里那一列写「已遮罩」。"""
    row = await sealed_run(monkeypatch, TWO)
    resp = await client.get(f"/api/runs/{row.id}/report.html")
    assert resp.status_code == 200, resp.text
    page = resp.text
    doc = doc_of_export(page)
    spend = next(s for s in all_segments(doc) if s.get("ref") == "v:Q2.r1.spend")
    data = data_of(page)
    chain = data["chains"][data["refs"][spend["id"]]]
    assert [s["step"] for s in chain] == ["query"]
    query = chain[0]
    assert query["masked"] == ["phone"]
    phone_at = query["columns"].index("phone")
    assert all("已遮罩" in str(r[phone_at]) for r in query["rows"])
    assert query["sql"] == CRM_SQL


async def test_export_picks_the_named_report(client, monkeypatch):
    """一次运行两份报告：不指定取成果标注的那份，?report= 取指定的；点不存在的节点回 404。"""
    from app.providers import mock_model

    from tests.test_bundle_api import node

    nodes = [n for n in weekly()["nodes"] if n["id"] != "out"]
    nodes.append(node("monthly", "report", instructions="写月报", on_violation="flag"))
    nodes.append(node("out", "output", fields=[{"name": "月报", "value": "{{ nodes.monthly.text }}"}]))
    ids = [n["id"] for n in nodes]
    graph = {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}

    calls = {"n": 0}

    def decide(self, messages):
        calls["n"] += 1
        return AIMessage(content=TEXT if calls["n"] == 1 else "## 月报\n\n本月销售额 [[m:gmv]]。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    row = await wait((await run_manager.start(graph=graph, input_payload={})).id)
    assert row.status == "succeeded", row.error

    named = await client.get(f"/api/runs/{row.id}/report.html", params={"report": "monthly"})
    assert named.status_code == 200, named.text
    assert data_of(named.text)["meta"]["report"]["node_id"] == "monthly"
    assert "月报" in named.text
    assert "门店周报" not in named.text

    default = await client.get(f"/api/runs/{row.id}/report.html")
    assert default.status_code == 200
    assert data_of(default.text)["meta"]["report"]["node_id"] == "monthly"   # 成果标注的是月报

    missing = await client.get(f"/api/runs/{row.id}/report.html", params={"report": "nope"})
    assert missing.status_code == 404
    assert missing.json()["code"] == "evidence_report_not_found"


async def test_export_without_report_documents(client, monkeypatch):
    """图里没有报告节点：没有 report.checked 事件，回 404 说没有可导出的报告。"""
    from app.providers import mock_model

    nodes = [n for n in weekly()["nodes"] if n["type"] != "report"]
    nodes = [({**n, "data": {**n["data"], "config": {**n["data"].get("config", {}),
                                                      "fields": [{"name": "结果", "value": "没有报告"}]}}}
             if n["type"] == "output" else n) for n in nodes]
    ids = [n["id"] for n in nodes]
    graph = {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content="没有报告"))
    row = await wait((await run_manager.start(graph=graph, input_payload={})).id)
    resp = await client.get(f"/api/runs/{row.id}/report.html")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "evidence_report_not_found"


async def test_export_refuses_tampered_document(client, monkeypatch):
    """报告文档在封存之后被改过（内容与哈希对不上）：不能导出，回 409。"""
    row = await sealed_run(monkeypatch)
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "report.checked"))).scalars()
        artifact = event.data["doc_artifact"]
    path = file_of(artifact)
    keep = path.read_bytes()
    try:
        path.write_bytes(canonical_json({"blocks": [], "run_id": row.id, "node_id": "write"}).encode())
        resp = await client.get(f"/api/runs/{row.id}/report.html")
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "evidence_doc_tampered"
    finally:
        path.write_bytes(keep)


async def test_export_is_read_only(client, monkeypatch):
    """导出前后运行记录、事件一条不多一条不少。"""
    row = await sealed_run(monkeypatch)

    async def counts() -> tuple:
        async with SessionLocal() as session:
            events = await session.execute(
                select(func.count()).select_from(RunEvent).where(RunEvent.run_id == row.id))
            runs = await session.get(Run, row.id)
            return events.scalar_one(), runs.status, runs.manifest_seq

    before = await counts()
    for _ in range(2):
        resp = await client.get(f"/api/runs/{row.id}/report.html")
        assert resp.status_code == 200
    assert await counts() == before
