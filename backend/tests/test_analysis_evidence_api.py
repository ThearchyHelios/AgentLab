"""分析结论进证据链（第 1 波 W1-b）：证据接口、审计、报告导出。

运行是直接落库的虚构封存运行（虚构零售库 shop）：一次查询（Q1）、一份按地区拆的分析结论（A1，分析卡或 Agent 做的），
报告引用判定语、具名标量、结果表的格。结论用 app.analysis 真算（tests.test_analysis_evidence.analyzed），出处链里的复算是
真的 recompute：按 sha256 取回封存的输入快照、下钻的上一层结论，逐字段比。只有要它出错、升版的几处用 monkeypatch。

- 证据图：A1 条目带算子、判定；「派生自」的边 A1 → Q1
- 片段接口：判定 → 方法 → 计算 → 输入（接查询步骤）→ 复算 → 封存；「在验证什么」谁写的照实标（Agent 写的假设 / 分析卡上
  写的『在验证什么』）；复算不了照实写，不让接口出错
- 审计：分析判定、分析结论的叫法；分析说法核对的违规单列一行
- 报告导出：数据块里有分析结论那几步；文档带「分析范围」的，正文后面渲染、编号点得开
- 指标步骤带可比性：口径卡指标上的可比性结论（不进台账）照卡上记的 sha256 取回，点得开（判定 → … → 复算 → 封存）
"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import analysis
from app.analysis import contract
from app.analysis.contract import ARTIFACT_KIND, ledger_entry
from app.core import artifact_store
from app.core.artifact_store import manifest_hash
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.evidence import build_catalog, compose_doc, iter_segments
from app.main import app
from tests.test_analysis_evidence import CHANNELS, SNAPSHOT, analyzed

GRAPH = {"nodes": [{"id": n, "type": t, "data": {"label": label}} for n, t, label in [
    ("fetch", "tool", "按地区取数"), ("an", "agent", "分析"), ("card", "metrics", "周报口径"),
    ("write", "report", "写周报")]]}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _query_entry(content: dict[str, Any], artifact: str) -> dict[str, Any]:
    return {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": artifact, "tool": "db_query__shop",
            "source": "shop", "columns": content["columns"], "rows": len(content["rows"]), "truncated": False}


async def sealed_run(text: str, *, item: str | None = "drop", call_id: str | None = None,
                     result: dict[str, Any] | None = None, doc_extra: dict[str, Any] | None = None,
                     card: dict[str, Any] | None = None, queries: Sequence[dict[str, Any]] = (),
                     parents: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """落一次封存好的运行：查询快照（SNAPSHOT，再加 queries）、分析结论（parents 在前，result 在后；result 缺省按 SNAPSHOT
    真算一份按地区拆的可加拆解）、（可选）口径卡、报告文档都按真实的工件写，事件按 runner 的算法封存。"""
    run_id = uuid.uuid4().hex
    q = await artifact_store.put_json(SNAPSHOT, kind="query_snapshot", run_id=run_id, node_id="fetch")
    more = [await artifact_store.put_json(c, kind="query_snapshot", run_id=run_id, node_id="fetch") for c in queries]
    res = result or await analyzed({"Q2": (SNAPSHOT, q)})
    qentries = [_query_entry(SNAPSHOT, q), *(_query_entry(c, a) for c, a in zip(queries, more))]
    aentries = []
    for i, parent in enumerate(parents):
        found = await artifact_store.put_json(parent, kind=ARTIFACT_KIND, run_id=run_id, node_id="an")
        aentries.append(ledger_entry(parent, node_id="an", exec_no=1, artifact=found, item=f"up{i}"))
    a = await artifact_store.put_json(res, kind=ARTIFACT_KIND, run_id=run_id, node_id="an")
    aentries.append(ledger_entry(res, node_id="an", exec_no=1, artifact=a, item=item, call_id=call_id))
    nodes: dict[str, Any] = {}
    events: list[tuple[int, str, str | None, dict[str, Any]]] = [
        (1, "run.started", None, {"input": {}}), (2, "node.finished", "fetch", {"evidence": qentries}),
        (3, "node.finished", "an", {"evidence": aentries})]
    if card is not None:
        card_id = await artifact_store.put_json(card, kind="metric_set", run_id=run_id, node_id="card")
        nodes["card"] = {**card, "artifact": card_id}
        events.append((4, "node.finished", "card", {"evidence": [{"kind": "metric_set", "node_id": "card",
                                                                 "exec": 1, "artifact": card_id}]}))
    catalog = build_catalog(nodes=nodes, ledger=[*qentries, *aentries])
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
    return {"run": run_id, "doc": doc, "query": q, "analysis": a, "result": res, "queries": more}


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
    assert judged["phrase"] == made["result"]["phrase"] == "销售额下降 194（-8.7%），主要来自华东（-120，占 61.9%）"
    assert judged["criterion"].startswith("一段占总变化的一半以上算主要来源")
    assert judged["verdict"] == {"code": "driver", "subjects": [["华东"]], "direction": "down"}
    assert judged["cited"] == {"what": "phrase"} and judged["render_ok"] is True and judged["eid_ok"] is True
    assert judged["origin"] == "card" and judged["item"] == "drop" and judged["label"] == "分析"
    # 分析卡做的：模板作者写的「在验证什么」，不标成 Agent 写的假设
    assert judged["hypothesis"] == "华东拖累" and judged["hypothesis_label"] == "分析卡上写的『在验证什么』"
    assert judged["hypothesis_note"].startswith("分析卡上写的『在验证什么』")
    assert {c["code"] for c in judged["caveats"]} == {"measure_unlabeled", "headline_basis", "comparability"}
    assert method["method"] == "decompose.additive@1" and method["method_label"] == "变化拆解（可加）"
    assert {s["key"]: s["label"] for s in method["settings"]}["main_share"] == "「主要来源」的占比"
    assert {s["key"] for s in method["settings"]} >= {"main_share", "concentration", "table_top_k"}
    assert "k_warn" not in {s["key"] for s in method["settings"]}
    assert calc["table"]["columns"][0] == "region" and calc["table"]["labels"]["share"] == "占变化的比重"
    assert calc["table"]["total_rows"] == 4 and calc["table"]["rows"][0][:2] == ["华东", "820"]
    assert {f["label"]: f["text"] for f in calc["formulas"]}["占比"] == \
        "华东的占比 = 华东的变化 ÷ 总变化 = -120 ÷ （-194） = 61.9%"
    assert given == {"step": "analysis_input", "of": "A1", "ref": "Q2", "role": "data", "role_label": "数据",
                     "artifact": made["query"], "columns": ["region", "week", "gmv"], "rows": "all", "query": "Q1",
                     "node_id": "fetch", "sealed": True}
    assert query["alias"] == "Q1" and query["input_of"] == "A1" and query["total_rows"] == 8
    assert query["highlight"]["cols"] == ["region", "week", "gmv"]
    # 真的复算：按 sha256 取回封存的输入快照，逐字段和封存的比
    assert again == {"step": "analysis_recompute", "alias": "A1", "status": "same", "method": "decompose.additive@1",
                     "text": "复算一致：按封存的输入重新计算，结论逐项相同"}
    assert seal["sealed"] is True and seal["ledger"] is True and seal["hash_ok"] is True
    assert body["seal"]["covered"] is True
    assert body["note"].startswith("判定语由系统按平台的方法算出判定")
    assert body["segment"]["kind"] == "analysis"


async def test_named_values_and_result_cells_point_at_what_they_cite(client):
    made = await sealed_run(TEXT)
    value = (await client.get(
        f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1.top_share|pct')}")).json()
    assert value["chain"][0]["cited"] == {"what": "value", "key": "top_share", "raw": "0.618556701031", "conv": "pct"}
    assert value["segment"]["text"] == "61.9%" and value["chain"][0]["render_ok"] is True
    calc = value["chain"][2]
    assert next(v for v in calc["values"] if v["key"] == "top_share") == {
        "key": "top_share", "raw": "0.618556701031", "rendered": "61.9%", "cited": True}
    assert value["chain"][-2]["text"] == "复算一致：按封存的输入重新计算，结论逐项相同"
    assert value["note"] == "这个数取自分析结论的具名的数，由系统按固定规则显示，未经模型转写"

    cell = (await client.get(
        f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'v:A1.r0.delta')}")).json()
    assert cell["chain"][0]["cited"] == {"what": "cell", "row": 0, "column": "delta", "column_label": "变化",
                                         "raw": "-120"}
    assert cell["chain"][2]["table"]["highlight"] == {"cells": [[0, "delta"]]}
    assert cell["note"] == "该值由系统从分析结论的结果表中取出、按固定规则显示，未经模型转写"


#: 按门店拆的快照：门店码是文本（"1203"），渲染时不加千分位
STORES: dict[str, Any] = {
    "columns": ["store", "week", "gmv"], "column_types": {"store": "text", "week": "text", "gmv": "number"},
    "rows": [["1203", "W41", 820], ["1203", "W42", 700], ["1305", "W41", 600], ["1305", "W42", 560],
             ["2201", "W41", 700], ["2201", "W42", 666]],
    "sql": "SELECT store, week, SUM(gmv) AS gmv FROM orders GROUP BY store, week ORDER BY store, week",
    "source": "shop", "truncated": False, "row_count": 6,
}
#: 按时段拆的快照：时段是 ISO 日期时间（带 T），文档按 2 版显示规则写成空格
SLOTS: dict[str, Any] = {
    "columns": ["slot", "week", "gmv"], "column_types": {"slot": "text", "week": "text", "gmv": "number"},
    "rows": [["2026-10-12T08:00:00", "W41", 820], ["2026-10-12T08:00:00", "W42", 700],
             ["2026-10-12T20:00:00", "W41", 600], ["2026-10-12T20:00:00", "W42", 560]],
    "sql": "SELECT slot, week, SUM(gmv) AS gmv FROM orders GROUP BY slot, week ORDER BY slot, week",
    "source": "shop", "truncated": False, "row_count": 4,
}


@pytest.mark.parametrize("snapshot, by, shown", [
    (STORES, "store", "1203"),
    (SLOTS, "slot", "2026-10-12 08:00:00"),
])
async def test_a_result_cell_rerenders_the_way_it_was_composed(client, monkeypatch, snapshot, by, shown):
    """证据接口按组装时同一套规则重新渲染结果表的格（evidence.resolve_ref，按文档记下的单元格显示规则）：文本码值照结果表
    记的列类型不加千分位，ISO 日期时间的 T 换成空格。以前不看列类型和显示规则，渲染成「1,203」「2026-10-12T08:00:00」，
    render_ok 为假，证据面板和导出页报假的「与按结论渲染的结果不一致」。"""
    import tests.test_analysis_evidence_api as here

    monkeypatch.setattr(here, "SNAPSHOT", snapshot)
    q = await artifact_store.put_json(snapshot, kind="query_snapshot")
    result = await analyzed({"Q2": (snapshot, q)}, by=[by])
    ref = f"v:A1.r0.{by}"
    made = await sealed_run(f"{by} [[{ref}]] 变化 [[v:A1.r0.delta]]。[[see:A1]]", result=result)
    segment = next(s for s in iter_segments(made["doc"]) if s.get("ref") == ref)
    assert segment["text"] == shown
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{segment['id']}")).json()
    judged = body["chain"][0]
    assert judged["rendered"] == shown and judged["render_ok"] is True
    # column_label 是结果表记的列名：维度列照输入快照里的列名（store、slot），不另起叫法
    assert judged["cited"] == {"what": "cell", "row": 0, "column": by, "column_label": by,
                               "raw": snapshot["rows"][0][0]}
    # 导出页的数据块是同一条链
    data = data_block((await client.get(f"/api/runs/{made['run']}/report.html")).text)
    chain = data["chains"][data["refs"][segment["id"]]]
    assert chain[0]["step"] == "analysis" and chain[0]["render_ok"] is True


async def test_the_recompute_gets_the_sealed_inputs_by_hash(client, monkeypatch):
    """复算的 load 交工件原内容（不加出处、不改列的类型），只认封存范围内交回过的查询；别的一律当取不回。"""
    seen: dict[str, Any] = {}
    real = analysis.recompute

    def recompute(result, load, *, load_result=None):
        seen["input"] = load(result["inputs"][0]["artifact"])
        seen["missing"] = load("0" * 64)
        return real(result, load, load_result=load_result)

    monkeypatch.setattr(analysis, "recompute", recompute)
    made = await sealed_run(TEXT)
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
    assert again["status"] == "same" and seen["input"] == SNAPSHOT and seen["missing"] is None


async def test_an_input_outside_the_sealed_scope_is_not_recomputed_from(client):
    """结论记着的输入快照不是这次运行交回过的（台账里没有）：复算照实说取不回，不去工件库里捞。"""
    stray = {**SNAPSHOT, "sql": SNAPSHOT["sql"] + " -- 另一次"}
    s_id = await artifact_store.put_json(stray, kind="query_snapshot")
    made = await sealed_run(TEXT, result=await analyzed({"Q2": (stray, s_id)}))
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
    assert again["status"] == "failed" and again["text"] == "复算失败：取不回 Q2 的查询快照"
    given = next(s for s in body["chain"] if s["step"] == "analysis_input")
    assert given["sealed"] is False and given["query"] is None


async def test_an_upgraded_method_names_both_versions(client, monkeypatch):
    made = await sealed_run(TEXT)
    monkeypatch.setitem(contract.METHODS, ("decompose", "additive"), "decompose.additive@2")
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
    assert again["method"] == "decompose.additive@1" and again["method_now"] == "decompose.additive@2"
    assert again["text"] == "运行时用的是 decompose.additive@1，现在是 decompose.additive@2，复算结果相同"


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
    own = await analyzed({"Q2": (SNAPSHOT, q)}, hypothesis=f"只给这一次：{uuid.uuid4().hex}")
    assert own["hypothesis"].startswith("只给这一次：")
    made = await sealed_run(TEXT, result=own)
    path = artifact_store._path_of(made["analysis"])
    path.write_text(path.read_text("utf-8").replace("华东", "华南"), "utf-8")
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    judged, seal = body["chain"]
    assert judged["note"] == "分析结论与哈希不一致，疑似被修改，不展示其中的判定和数" and "phrase" not in judged
    assert seal["hash_ok"] is False and seal["sealed"] is False and body["seal"]["covered"] is False


def test_suspicious_name_hints_do_not_read_the_analysis_results(monkeypatch):
    """可疑实体找「最接近的名字」时按封存的台账重建目录（_scope_catalog）：分析结论不进，不读盘、不复验哈希（候选只看表和
    字段，用不到结论的内容）。以前每点开一个可疑的名字，就把这次运行的分析结论全读一遍。"""
    from app.api import evidence as api
    from app.engine import evidence as engine_evidence

    read: list[str] = []
    monkeypatch.setattr(engine_evidence, "load_artifact", lambda artifact: read.append(artifact))
    q, a = "1" * 64, "2" * 64
    entries = [_query_entry(SNAPSHOT, q), {"kind": "analysis", "node_id": "an", "exec": 1, "artifact": a,
                                           "op": "decompose", "mode": "additive", "item": "drop", "inputs": [q]}]
    graph = {**GRAPH, "edges": [{"source": "fetch", "target": "an"}, {"source": "an", "target": "write"}]}
    sealed = api._Sealed(run_id="r", graph=graph, seal={"sealed": True, "ok": True}, events=[], entries=entries)
    for report in ("write", "nope"):
        catalog = api._scope_catalog(sealed, report)
        assert "Q1" in catalog and not [e for e in catalog.values() if e.get("kind") == "analysis"]
    assert read == []


async def test_agent_analyses_are_marked_as_such(client):
    """Agent 现场做的：谁做的写 Agent，「在验证什么」照旧标「Agent 写的假设」（分析卡上那一句是模板作者写的，另说）"""
    made = await sealed_run(TEXT, item=None, call_id="call-9")
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    judged = body["chain"][0]
    assert judged["origin"] == "agent" and judged["call_id"] == "call-9"
    assert judged["hypothesis_label"] == "Agent 写的假设" and judged["hypothesis_note"].startswith("Agent 写的假设")


async def test_headline_inputs_show_the_closure(client):
    """给了总数（headline）的拆解：两格各是一份输入，和结论里的总数闭合；复算照样按 sha256 取回两份快照。"""
    totals = {"columns": ["gmv_base", "gmv"], "column_types": {"gmv_base": "number", "gmv": "number"},
              "rows": [[2220, 2026]], "sql": "SELECT 2220 AS gmv_base, 2026 AS gmv", "source": "shop",
              "truncated": False}
    t = artifact_store.content_hash(artifact_store.canonical_json(totals))
    q = artifact_store.content_hash(artifact_store.canonical_json(SNAPSHOT))
    result = await analyzed({"Q2": (SNAPSHOT, q), "Q5": (totals, t)}, headline=["Q5.r0.gmv_base", "Q5.r0.gmv"])
    made = await sealed_run("[[a:A1]]。", result=result, queries=[totals])
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    inputs = [s for s in body["chain"] if s["step"] == "analysis_input"]
    assert [s["role"] for s in inputs] == ["headline_base", "headline_target", "data"]
    assert inputs[0]["closure"] == {"mode": "additive", "total_key": "base_total", "total": "2,220", "headline": "2,220",
                                    "closed": True}
    assert inputs[1]["closure"] == {"mode": "additive", "total_key": "target_total", "total": "2,026",
                                    "headline": "2,026", "closed": True}
    assert [s["role_label"] for s in inputs[:2]] == ["基期总数", "本期总数"]
    # 那一格的值服务端渲染好了（render_cell：千分位），和闭合核对里的字同一个写法；页面照原样显示
    assert inputs[0]["value"] == "2220" and inputs[0]["value_rendered"] == "2,220"
    assert inputs[1]["value"] == "2026" and inputs[1]["value_rendered"] == "2,026"
    assert inputs[1]["query"] == "Q2" and inputs[1]["sealed"] is True and inputs[2]["query"] == "Q1"
    assert next(s for s in body["chain"] if s["step"] == "analysis_recompute")["status"] == "same"


async def ratio_run() -> dict[str, Any]:
    """一次封存的运行：按渠道拆退款率（比率拆解），给了整体比率的两格（headline，0.035 → 0.053），报告引用判定语。"""
    rates = {"columns": ["channel", "week", "refunds", "orders"],
             "column_types": {"channel": "text", "week": "text", "refunds": "number", "orders": "number"},
             "rows": [["线上", "W41", 50, 1000], ["线上", "W42", 80, 1000], ["线下", "W41", 20, 1000],
                      ["线下", "W42", 26, 1000]],
             "sql": "SELECT channel, week, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY 1, 2",
             "source": "shop", "truncated": False, "row_count": 4}
    head = {"columns": ["week", "rate"], "column_types": {"week": "text", "rate": "number"},
            "rows": [["W41", "0.035"], ["W42", "0.053"]],
            "sql": "SELECT week, SUM(refund_flag) * 1.0 / COUNT(*) AS rate FROM orders GROUP BY week",
            "source": "shop", "truncated": False, "row_count": 2}
    r, h = (artifact_store.content_hash(artifact_store.canonical_json(c)) for c in (rates, head))
    result = await analyzed({"Q2": (rates, r), "Q5": (head, h)}, {"op": "decompose", "mode": "ratio", "params": {}},
                            query="Q2", by=["channel"], period="week", base="W41", target="W42", numerator="refunds",
                            denominator="orders", label="退款率", headline={"base": "Q5.r0.rate", "target": "Q5.r1.rate"})
    assert result["status"] == "ok" and result["verdict"]["code"] != "unclosed"
    return await sealed_run("[[a:A1]]。", result=result, queries=[rates, head])


async def test_a_ratio_headline_closes_against_the_overall_ratio(client):
    """比率拆解给了整体比率的两格（headline）：闭合核对对着结论里的整体比率，即各段分子之和 ÷ 分母之和（r_base_total、
    r_target_total），角色写「基期整体比率」「本期整体比率」，closure.mode 写明是比率（导出页照它说「各段分子之和 ÷ 分母
    之和」，不说「各段之和」）。以前只认可加拆解的总数键，比率拆解的 headline 不显示闭合核对。"""
    made = await ratio_run()
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")).json()
    inputs = {s["role"]: s for s in body["chain"] if s["step"] == "analysis_input"}
    assert inputs["headline_base"]["closure"] == {"mode": "ratio", "total_key": "r_base_total", "total": "3.5%",
                                                  "headline": "3.5%", "closed": True}
    assert inputs["headline_target"]["closure"] == {"mode": "ratio", "total_key": "r_target_total", "total": "5.3%",
                                                    "headline": "5.3%", "closed": True}
    assert inputs["headline_base"]["role_label"] == "基期整体比率"
    assert inputs["headline_target"]["role_label"] == "本期整体比率"
    assert next(s for s in body["chain"] if s["step"] == "analysis_recompute")["status"] == "same"


async def test_a_drill_down_points_at_its_parent_and_recomputes_from_it(client):
    """下钻（华东按渠道再拆）：输入里有上一层结论（parent），落到报告目录的 A1；复算按 sha256 取回台账记过的上一层。"""
    q = artifact_store.content_hash(artifact_store.canonical_json(SNAPSHOT))
    c = artifact_store.content_hash(artifact_store.canonical_json(CHANNELS))
    top = await analyzed({"Q2": (SNAPSHOT, q)})
    child = await analyzed({"Q3": (CHANNELS, c)}, parents={"A1": top}, query="Q3", by=["channel"],
                           parent={"of": "A1", "key": ["华东"]})
    made = await sealed_run("[[a:A2]]。", result=child, parents=[top], queries=[CHANNELS])
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A2')}")).json()
    inputs = [s for s in body["chain"] if s["step"] == "analysis_input"]
    assert [s["role"] for s in inputs] == ["parent", "data"]
    assert inputs[0]["alias"] == "A1" and inputs[0]["phrase"] == top["phrase"] and inputs[0]["sealed"] is True
    assert inputs[0]["role_label"] == "上一层拆解" and inputs[1]["query"] == "Q2"
    again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
    assert again["status"] == "same", again
    graph = (await client.get(f"/api/runs/{made['run']}/evidence")).json()
    edges = {(e["from"], e["to"], e["rel"]) for e in graph["edges"]}
    assert {("A2", "A1", "derived_from"), ("A2", "Q2", "derived_from"), ("A1", "Q1", "derived_from")} <= edges


#: 本周、上周两份同形查询（只差日期）：口径卡跨这两份算环比时自动做可比性检查
WINDOW = "SELECT SUM(gmv) AS gmv FROM orders WHERE order_date >= '{a}' AND order_date < '{b}'"
LAST_WEEK = {"columns": ["gmv"], "column_types": {"gmv": "number"}, "rows": [[2220]],
             "sql": WINDOW.format(a="2026-10-05", b="2026-10-12"), "source": "shop", "truncated": False}
THIS_WEEK = {**LAST_WEEK, "rows": [[2026]], "sql": WINDOW.format(a="2026-10-12", b="2026-10-19")}


async def comparability_card() -> tuple[dict[str, Any], dict[str, Any]]:
    """一张跨两周算环比的口径卡，指标上记着口径卡执行时自动做的可比性检查（结论落成工件，不进台账）：(卡, 可比性结论)。"""
    last = artifact_store.content_hash(artifact_store.canonical_json(LAST_WEEK))
    this = artifact_store.content_hash(artifact_store.canonical_json(THIS_WEEK))
    result = await analyzed({"fetch·base": (LAST_WEEK, last), "fetch·target": (THIS_WEEK, this)},
                            {"op": "comparable", "mode": None, "params": {}},
                            base_query="fetch·base", target_query="fetch·target")
    found = await artifact_store.put_json(result, kind=ARTIFACT_KIND, node_id="card")
    verdict = result["verdict"]["code"]
    card = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1", "metrics": [
        {"id": "wow", "name": "环比", "value": -0.0874, "unit": "",
         "expression": "cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, 0, 'gmv') - 1",
         "substituted": "2026 / 2220 - 1", "recompute_ok": True,
         "inputs": [{"path": "cell(nodes.fetch, 0, 'gmv')", "value": 2026, "node_id": "fetch", "via": "tool_cell",
                     "artifact": this, "locator": {"row": 0, "column": "gmv"}},
                    {"path": "cell(nodes.fetch, 0, 'gmv')", "value": 2220, "node_id": "fetch", "via": "tool_cell",
                     "artifact": last, "locator": {"row": 0, "column": "gmv"}}],
         "comparability": {"artifact": found, "verdict": verdict, "phrase": result["phrase"], "reasons": []}}]}
    return card, result


async def test_the_metric_step_opens_the_comparability_result(client):
    """可比性结论不进台账、只从口径卡的指标集里引用：照卡上记的 sha256 取回，随卡封存；点得开它的整条出处链，复算是真的。"""
    card, result = await comparability_card()
    made = await sealed_run("环比 [[m:wow]]。", card=card, queries=[LAST_WEEK, THIS_WEEK])
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'm:wow')}")).json()
    view = body["chain"][0]["comparability"]
    assert view["verdict"] == result["verdict"]["code"] and view["phrase"] == result["phrase"]
    assert view["sealed"] is True and view["hash_ok"] is True and view["method"] == "comparable@1"
    steps = view["steps"]
    assert [s["step"] for s in steps] == ["analysis", "analysis_method", "analysis_calc", "analysis_input",
                                          "analysis_input", "analysis_recompute", "analysis_seal"]
    judged, method, calc, base, target, again, seal = steps
    assert judged["origin"] == "metric" and judged["cited"] == {"what": "phrase"} and judged["render_ok"] is True
    assert judged["method_label"] == "可比性检查" and judged["node_id"] == "card"
    assert [r[0] for r in calc["table"]["rows"]] == ["同口径", "窗口长度", "整期", "星期构成", "数据问题"]
    assert (base["role"], target["role"]) == ("base", "target")
    assert base["query"] == "Q2" and target["query"] == "Q3" and base["sealed"] is True
    assert again["status"] == "same" and again["text"] == "复算一致：按封存的输入重新计算，结论逐项相同"
    assert seal["via"] == "metric_set" and seal["ledger"] is False and seal["sealed"] is True
    assert seal["note"].startswith("这份可比性结论不进台账")
    # 两份输入快照的查询步骤在指标链里（指标的输入就是这两份），可比性的链里不再给
    assert [s["step"] for s in body["chain"]].count("query") == 2 and "query" not in [s["step"] for s in steps]


async def test_a_card_without_the_comparability_artifact_says_only_what_it_recorded(client):
    card = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1", "metrics": [
        {"id": "wow", "name": "环比", "value": -0.087, "unit": "", "expression": "1", "inputs": [],
         "comparability": {"artifact": "", "verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据",
                           "reasons": ["本期只有 3 天数据"]}}]}
    made = await sealed_run("环比 [[m:wow]]。", card=card)
    body = (await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'm:wow')}")).json()
    assert body["chain"][0]["comparability"] == {
        "verdict": "incomparable", "phrase": "两期不可比：本期只有 3 天数据", "reasons": ["本期只有 3 天数据"],
        "artifact": None, "hash_ok": None, "sealed": False}


async def test_the_app_metric_endpoint_opens_the_comparability_too(client):
    card, _ = await comparability_card()
    made = await sealed_run("环比 [[m:wow]]。", card=card, queries=[LAST_WEEK, THIS_WEEK])
    body = (await client.get(f"/api/runs/{made['run']}/evidence/metrics/card/wow")).json()
    view = body["chain"][0]["comparability"]
    assert view["sealed"] is True and view["steps"][-2]["status"] == "same"
    assert [s["query"] for s in view["steps"] if s["step"] == "analysis_input"] == [None, None]


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
    "phenomena": [{"alias": "A1", "op": "decompose", "phrase": "销售额下降 194（-8.7%），主要来自华东（-120，占 61.9%）"}],
    "decomposed": [{"alias": "A1", "op": "decompose", "mode": "additive", "dims": ["地区"], "verdict": "driver",
                    "phrase": "主要来自华东"}],
    "excluded": [], "refused": [{"alias": "A1", "op": "baseline", "reason": "历史只有 5 期，至少要 8 期"}],
    "not_decomposed": ["渠道"], "tests": 2, "corrected": False}}


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
    assert "<dt>算不了</dt>" in html and "<dd>渠道</dd>" in html
    assert "这次做了 2 次差异检验，几次检验之间没有按次数校正" in html
    assert 'data-seg="scope:A1"' in html and "（按地区）" in html
    assert data["refs"]["scope:A1"] == data["refs"][phrase]
    assert f'data-seg="{phrase}"' in html and 'data-kind="analysis"' in html


async def test_the_export_recomputes_each_conclusion_once(client, monkeypatch):
    """导出页里同一份结论被判定语、具名的数、整表的每一格、句末依据、分析范围各引一处，引用键不同、各接一条链，复算只做
    一次（按结论工件记在这一次请求里）；分析范围的编号和正文的判定语同一个引用键，不再另建一条链。以前每条链各复算一次，
    1000 段的拆解导出一次复算三十多次。"""
    calls: list[str] = []
    real = analysis.recompute

    def counting(result, load, *, load_result=None):
        calls.append(str(result.get("method")))
        return real(result, load, load_result=load_result)

    monkeypatch.setattr(analysis, "recompute", counting)
    text = ("[[a:A1]]。华东占 [[a:A1.top_share|pct]]。[[see:A1]]\n\n"
            "[[table:A1 cols=region,delta,share rows=0-3]]")
    made = await sealed_run(text, doc_extra=SCOPE)
    data = data_block((await client.get(f"/api/runs/{made['run']}/report.html")).text)
    assert calls == ["decompose.additive@1"]
    cells = [s["id"] for s in iter_segments(made["doc"]) if str(s.get("ref") or "").startswith("v:A1.")]
    assert len(cells) == 12
    keys = {data["refs"][sid] for sid in [*cells, seg_id(made["doc"], "a:A1"), seg_id(made["doc"], "a:A1.top_share|pct")]}
    assert len(keys) == 14                                  # 每处引用各一条链
    for key in keys:
        [again] = [s for s in data["chains"][key] if s["step"] == "analysis_recompute"]
        assert again["status"] == "same"
    assert data["refs"]["scope:A1"] == data["refs"][seg_id(made["doc"], "a:A1")]
    # 下一次请求重新复算（缓存只活在一次请求里）
    await client.get(f"/api/runs/{made['run']}/evidence/segments/{seg_id(made['doc'], 'a:A1')}")
    assert len(calls) == 2


async def test_docs_without_a_scope_render_no_scope(client):
    made = await sealed_run(TEXT)
    html = (await client.get(f"/api/runs/{made['run']}/report.html")).text
    assert '<section class="scope"' not in html and 'data-seg="scope:A1"' not in html
