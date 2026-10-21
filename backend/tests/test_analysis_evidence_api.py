"""分析结论进证据链（第 1 波 W1-b）：证据接口、审计、报告导出。

运行是直接落库的虚构封存运行（虚构零售库 shop）：一次查询（Q1）、一份按地区拆的分析结论（A1，分析卡或 Agent 做的），
报告引用判定语、具名标量、结果表的格。算子第 1 波还没落地，复算按 monkeypatch 换。

- 证据图：A1 条目带算子、判定；「派生自」的边 A1 → Q1
- 片段接口：判定 → 方法 → 计算 → 输入（接查询步骤）→ 复算 → 封存；Agent 写的假设标出来；复算不了照实写，不让接口出错
- 审计：分析判定、分析结论的叫法；分析说法核对的违规单列一行
- 报告导出：数据块里有分析结论那几步；文档带「分析范围」的，正文后面渲染、编号点得开
- 指标步骤带可比性
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import analysis
from app.analysis.contract import ARTIFACT_KIND, ledger_entry
from app.analysis.testing import fake_result
from app.core import artifact_store
from app.core.artifact_store import manifest_hash
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.evidence import build_catalog, compose_doc, iter_segments
from app.main import app
from tests.test_analysis_evidence import SNAPSHOT

GRAPH = {"nodes": [{"id": n, "type": t, "data": {"label": label}} for n, t, label in [
    ("fetch", "tool", "按地区取数"), ("an", "agent", "分析"), ("card", "metrics", "周报口径"),
    ("write", "report", "写周报")]]}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def sealed_run(text: str, *, item: str | None = "drop", call_id: str | None = None,
                     result: dict[str, Any] | None = None, doc_extra: dict[str, Any] | None = None,
                     card: dict[str, Any] | None = None) -> dict[str, Any]:
    """落一次封存好的运行：查询快照、分析结论、（可选）口径卡、报告文档都按真实的工件写，事件按 runner 的算法封存。"""
    run_id = uuid.uuid4().hex
    q = await artifact_store.put_json(SNAPSHOT, kind="query_snapshot", run_id=run_id, node_id="fetch")
    res = result or fake_result(inputs=[{"ref": "Q2", "artifact": q, "role": "data",
                                         "columns": ["region", "week", "gmv"], "rows": "all"}])
    a = await artifact_store.put_json(res, kind=ARTIFACT_KIND, run_id=run_id, node_id="an")
    qentry = {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": q, "tool": "db_query__shop",
              "source": "shop", "columns": SNAPSHOT["columns"], "rows": len(SNAPSHOT["rows"]), "truncated": False}
    aentry = ledger_entry(res, node_id="an", exec_no=1, artifact=a, item=item, call_id=call_id)
    nodes: dict[str, Any] = {}
    events: list[tuple[int, str, str | None, dict[str, Any]]] = [
        (1, "run.started", None, {"input": {}}), (2, "node.finished", "fetch", {"evidence": [qentry]}),
        (3, "node.finished", "an", {"evidence": [aentry]})]
    if card is not None:
        card_id = await artifact_store.put_json(card, kind="metric_set", run_id=run_id, node_id="card")
        nodes["card"] = {**card, "artifact": card_id}
        events.append((4, "node.finished", "card", {"evidence": [{"kind": "metric_set", "node_id": "card",
                                                                 "exec": 1, "artifact": card_id}]}))
    catalog = build_catalog(nodes=nodes, ledger=[qentry, aentry])
    doc = compose_doc(text, catalog, node_id="write", run_id=run_id)
    doc.update(doc_extra or {})
    doc_id = await artifact_store.put_json(doc, kind="report_doc", run_id=run_id, node_id="write")
    seq = len(events)
    events += [(seq + 1, "report.checked", "write", {"doc_artifact": doc_id, "ok": True}),
               (seq + 2, "run.finished", None, {"output": {}})]
    async with SessionLocal() as session:
        session.add(Run(id=run_id, workflow_name="分析周报", status="succeeded", run_class="exploratory",
                        graph=GRAPH, agent_limits={}, manifest_hash=manifest_hash(events), manifest_seq=events[-1][0]))
        await session.flush()
        for s, etype, node, data in events:
            session.add(RunEvent(run_id=run_id, seq=s, type=etype, node_id=node, ts=float(s), data=data))
        await session.commit()
    return {"run": run_id, "doc": doc, "query": q, "analysis": a, "result": res}


def seg_id(doc: dict[str, Any], ref: str) -> str:
    return next(s["id"] for s in iter_segments(doc) if s.get("ref") == ref)


TEXT = "[[a:A1]]。华东占 [[a:A1.top_share|pct]]，变化 [[v:A1.r0.delta]]。[[see:A1]]"


# --------------------------------------------------------------------------
# 证据图
# --------------------------------------------------------------------------


async def test_the_graph_lists_the_analysis_and_what_it_was_derived_from(client):
    made = await sealed_run(TEXT)
    body = (await client.get(f"/api/runs/{made['run']}/evidence")).json()
    assert body["mode"] == "cited"
    item = next(e for e in body["evidence"] if e["alias"] == "A1")
    assert item["kind"] == "analysis" and item["op"] == "decompose" and item["verdict"] == "driver"
    assert item["item"] == "drop" and item["sealed"] is True and item["artifact"] == made["analysis"]
    assert item["label"].startswith("变化拆解（可加） · ")
    edges = {(e["from"], e["to"], e["rel"]) for e in body["edges"]}
    assert ("A1", "Q1", "derived_from") in edges
    assert ("A1.r0.delta", "A1", "cell_of") in edges


# --------------------------------------------------------------------------
# 片段接口：出处链
# --------------------------------------------------------------------------


async def test_the_phrase_opens_judgment_method_calculation_inputs_recompute_and_seal(client):
    made = await sealed_run(TEXT)
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    chain = body["chain"]
    assert [s["step"] for s in chain] == ["analysis", "analysis_method", "analysis_calc", "analysis_input", "query",
                                         "analysis_recompute", "analysis_seal"]
    judged, method, calc, given, query, again, seal = chain
    assert judged["phrase"] == made["result"]["phrase"] and judged["criterion"] == "一段占总变化一半以上算主要来源"
    assert judged["verdict"] == {"code": "driver", "subjects": [["华东"]], "direction": "down"}
    assert judged["cited"] == {"what": "phrase"} and judged["render_ok"] is True and judged["eid_ok"] is True
    assert judged["origin"] == "card" and judged["item"] == "drop" and judged["label"] == "分析"
    assert judged["hypothesis"] == "地区拖累" and judged["hypothesis_note"].startswith("Agent 写的假设")
    assert method["method"] == "decompose.additive@1" and method["method_label"] == "变化拆解（可加）"
    assert {s["key"]: s["label"] for s in method["settings"]}["main_share"] == "「主要来源」的占比"
    assert {s["key"] for s in method["settings"]} >= {"main_share", "concentration", "table_top_k"}
    assert "k_warn" not in {s["key"] for s in method["settings"]}
    assert calc["table"]["columns"][0] == "region" and calc["table"]["labels"]["share"] == "占变化的比重"
    assert calc["table"]["total_rows"] == 4 and calc["table"]["rows"][0][:2] == ["华东", "820"]
    assert {f["label"]: f["text"] for f in calc["formulas"]}["占比"] == \
        "华东的占比 = 华东的变化 ÷ 总变化 = -120 ÷ -194 = 61.9%"
    assert given == {"step": "analysis_input", "of": "A1", "ref": "Q2", "role": "data", "role_label": "数据",
                     "artifact": made["query"], "columns": ["region", "week", "gmv"], "rows": "all", "query": "Q1",
                     "node_id": "fetch", "sealed": True}
    assert query["alias"] == "Q1" and query["input_of"] == "A1" and query["total_rows"] == 8
    assert query["highlight"]["cols"] == ["region", "week", "gmv"]
    assert again["status"] == "failed" and again["text"] == "复算失败：系统里还没有这种分析的复算实现"
    assert seal["sealed"] is True and seal["ledger"] is True and seal["hash_ok"] is True
    assert body["seal"]["covered"] is True
    assert body["note"].startswith("判定语由系统按平台的方法算出判定")
    assert body["segment"]["kind"] == "analysis"


async def test_named_values_and_result_cells_point_at_what_they_cite(client, monkeypatch):
    monkeypatch.setattr(analysis, "recompute", lambda result, load, *, load_result=None: dict(result))
    made = await sealed_run(TEXT)
    value = (await client.get(
        f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1.top_share|pct')}")).json()
    assert value["chain"][0]["cited"] == {"what": "value", "key": "top_share", "raw": "0.6186", "conv": "pct"}
    calc = value["chain"][2]
    assert next(v for v in calc["values"] if v["key"] == "top_share") == {
        "key": "top_share", "raw": "0.6186", "rendered": "0.6186", "cited": True}
    assert value["chain"][-2]["text"] == "复算一致：按封存的输入重新计算，结论逐项相同"
    assert value["note"] == "这个数取自分析结论的具名的数，由系统按固定规则显示，未经模型转写"

    cell = (await client.get(
        f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'v:A1.r0.delta')}")).json()
    assert cell["chain"][0]["cited"] == {"what": "cell", "row": 0, "column": "delta", "raw": "-120"}
    assert cell["chain"][2]["table"]["highlight"] == {"cells": [[0, "delta"]]}
    assert cell["note"] == "该值由系统从分析结论的结果表中取出、按固定规则显示，未经模型转写"


async def test_the_recompute_gets_the_sealed_inputs_by_hash(client, monkeypatch):
    seen: dict[str, Any] = {}

    def recompute(result, load, *, load_result=None):
        seen["input"] = load(result["inputs"][0]["artifact"])
        seen["missing"] = load("0" * 64)
        return {**result, "method": "decompose.additive@2"}

    monkeypatch.setattr(analysis, "recompute", recompute)
    made = await sealed_run(TEXT)
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
    assert again["text"] == "运行时用的是 decompose.additive@1，现在是 decompose.additive@2，复算结果不同"
    assert seen["input"]["rows"] == SNAPSHOT["rows"] and seen["input"]["node_id"] == "fetch"
    assert seen["missing"] is None


async def test_recompute_errors_do_not_break_the_panel(client, monkeypatch):
    def broken(result, load, *, load_result=None):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(analysis, "recompute", broken)
    made = await sealed_run(TEXT)
    response = await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")
    assert response.status_code == 200
    again = next(s for s in response.json()["chain"] if s["step"] == "analysis_recompute")
    assert again["status"] == "failed" and again["text"].startswith("复算失败：")


async def test_a_tampered_result_is_said_and_not_shown(client):
    # 工件按内容寻址：改的是这一次独有的那份结论（别的测试的结论内容相同、是同一个文件）
    q = await artifact_store.put_json(SNAPSHOT, kind="query_snapshot")
    own = fake_result(inputs=[{"ref": "Q2", "artifact": q, "role": "data"}], criterion=f"只给这一次：{uuid.uuid4().hex}")
    made = await sealed_run(TEXT, result=own)
    path = artifact_store._path_of(made["analysis"])
    path.write_text(path.read_text("utf-8").replace("华东", "华南"), "utf-8")
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    judged, seal = body["chain"]
    assert judged["note"] == "分析结论与哈希不一致，疑似被修改，不展示其中的判定和数" and "phrase" not in judged
    assert seal["hash_ok"] is False and seal["sealed"] is False and body["seal"]["covered"] is False


async def test_agent_analyses_are_marked_as_such(client):
    made = await sealed_run(TEXT, item=None, call_id="call-9")
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    assert body["chain"][0]["origin"] == "agent" and body["chain"][0]["call_id"] == "call-9"


async def test_headline_inputs_show_the_closure_and_drill_downs_the_parent(client):
    base = fake_result()
    made = await sealed_run(TEXT)
    q, a = made["query"], made["analysis"]
    child = fake_result(inputs=[{"ref": "Q2", "artifact": q, "role": "data", "columns": ["region", "gmv"],
                                 "rows": [0, 1]},
                                {"ref": "Q2.r1.gmv", "artifact": q, "role": "headline_target", "row": 1,
                                 "column": "gmv", "value": "2026"},
                                {"ref": "A3", "artifact": a, "role": "parent"}],
                        values={**base["values"], "target_total": "2026"})
    drilled = await sealed_run("[[a:A1]]。", result=child)
    body = (await client.get(f"/api/runs/{drilled['run']}/evidence/segments/{seg_id(drilled['doc'], 'a:A1')}")).json()
    inputs = [s for s in body["chain"] if s["step"] == "analysis_input"]
    assert [s["role"] for s in inputs] == ["data", "headline_target", "parent"]
    assert inputs[1]["closure"] == {"total_key": "target_total", "total": "2,026", "headline": "2,026", "closed": True}
    assert inputs[2]["phrase"] == base["phrase"] and inputs[2]["role_label"] == "上一层拆解"
    query = next(s for s in body["chain"] if s["step"] == "query")
    assert [0, "region"] in query["highlight"]["cells"] and [1, "gmv"] in query["highlight"]["cells"]


async def test_the_metric_step_shows_the_comparability(client):
    card = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1", "metrics": [
        {"id": "wow", "name": "环比", "value": -0.087, "unit": "", "expression": "1", "inputs": [],
         "comparability": {"artifact": "", "verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据",
                           "reasons": ["本期只有 3 天数据"]}}]}
    made = await sealed_run("环比 [[m:wow]]。", card=card)
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'm:wow')}")).json()
    assert body["chain"][0]["comparability"] == {
        "verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据", "reasons": ["本期只有 3 天数据"],
        "artifact": None, "hash_ok": None, "sealed": False}


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


async def test_the_audit_names_analysis_evidence_and_lists_claim_violations(client):
    violation = {"code": "analysis_claim_mismatch", "message": "A1 判的是变化主要来自华东，不能写「主要来自华南」",
                 "segment": "s0", "unit": "u0", "span": [0, 4], "text": "主要来自"}
    made = await sealed_run(TEXT, doc_extra={"violations": [violation]})
    body = (await client.get(f"/api/runs/{made['run']}/evidence/audit")).json()
    rows = [r for g in body["groups"] for r in g["rows"]]
    phrase = next(r for r in rows if r.get("ref") == "a:A1")
    assert phrase["kind"] == "analysis" and phrase["evidence_kind"] == "analysis" and phrase["sealed"] is True
    assert phrase["evidence"].startswith("变化拆解（可加） · ")
    value = next(r for r in rows if r.get("ref") == "a:A1.top_share|pct")
    assert value["evidence"] == "A1.top_share" and value["kind"] == "number"
    cell = next(r for r in rows if r.get("ref") == "v:A1.r0.delta")
    assert cell["evidence"] == "A1.r0.delta" and cell["evidence_kind"] == "analysis" and cell["sealed"] is True
    flagged = next(r for r in rows if r.get("issue") == "analysis_claim_mismatch")
    assert flagged["group"] == "none" and flagged["kind"] == "violation"

    text = (await client.get(f"/api/runs/{made['run']}/evidence/audit?format=csv")).text
    table = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    head = table[0]
    lines = [dict(zip(head, line)) for line in table[1:]]
    assert any(r["种类"] == "分析判定" and r["证据种类"] == "分析结论" for r in lines)
    assert any(r["问题"] == "判断与分析结论不符" for r in lines)


def test_audit_labels_cover_the_new_violation_codes():
    from app.api.evidence import _CSV_LABELS

    issues = _CSV_LABELS["issue"]
    assert issues["analysis_claim_uncited"] == "判断词未挂分析结论"
    assert issues["analysis_claim_mismatch"] == "判断与分析结论不符"
    assert issues["causal_unsupported"] == "因果说法没有依据"
    assert issues["inline_see"] == "依据写在句子中间" and issues["absence_claim"] == "缺失断言"


# --------------------------------------------------------------------------
# 报告导出
# --------------------------------------------------------------------------

SCOPE = {"analysis_scope": {
    "phenomena": [{"alias": "A1", "op": "decompose", "phrase": "销售额下降 194 万（-8.7%），主要来自华东（-120 万，占 62%）"}],
    "decomposed": [{"alias": "A1", "op": "decompose", "mode": "additive", "dims": ["地区"], "verdict": "driver",
                    "phrase": "主要来自华东"}],
    "excluded": [], "refused": [{"alias": "A1", "op": "baseline", "reason": "历史只有 5 期，至少要 8 期"}],
    "not_decomposed": ["渠道"], "tests": 2}}


def data_block(html: str) -> dict[str, Any]:
    raw = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)


async def test_the_html_export_carries_the_analysis_chain_and_the_scope(client):
    made = await sealed_run(TEXT, doc_extra=SCOPE)
    response = await client.get(f"/api/runs/{made['run']}/report.html")
    assert response.status_code == 200
    html = response.text
    data = data_block(html)
    phrase = seg_id(made["doc"], "a:A1")
    chain = data["chains"][data["refs"][phrase]]
    assert [s["step"] for s in chain][:3] == ["analysis", "analysis_method", "analysis_calc"]
    assert data["notes"][phrase].startswith("判定语由系统按平台的方法算出判定")
    # 分析范围：正文后面渲染，编号点得开、和点开判定语是同一条链
    assert '<section class="scope"' in html and "<dt>现象</dt>" in html and "<dt>拆过</dt>" in html
    assert "<dt>算不了</dt>" in html and "<dd>渠道</dd>" in html and "这次做了 2 次差异检验" in html
    assert 'data-seg="scope:A1"' in html and "（按地区）" in html
    assert data["refs"]["scope:A1"] == data["refs"][phrase]
    assert f'data-seg="{phrase}"' in html and 'data-kind="analysis"' in html


async def test_docs_without_a_scope_render_no_scope(client):
    made = await sealed_run(TEXT)
    html = (await client.get(f"/api/runs/{made['run']}/report.html")).text
    assert '<section class="scope"' not in html and 'data-seg="scope:A1"' not in html
