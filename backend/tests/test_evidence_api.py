"""证据接口：整次运行的证据图，和点开一个片段时的出处链。

只认封存范围内的事件（seq ≤ manifest_seq）能追到的东西：报告文档从 report.checked
找，口径卡的指标集从 node.finished.evidence 找，成果从出口节点 node.finished 的工件找。
artifacts 表可以事后插行、封存之后追加的事件不在核对范围里，都不能当证据的来源。

旧运行没有报告文档：有旧契约的按位置信息把旧的 matched 标出来（legacy_contract），
什么都没有的照实说没有（none）。本期不按数值猜来源。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.evidence import input_eid, iter_segments
from app.engine.runner import run_manager
from app.main import app


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


KPI = "{'gmv': 45678.5, 'gmv_prev': 42010.0, 'orders': 1234}"
STANDARD = [
    {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
    {"id": "wow", "name": "环比增幅", "unit": "%", "decimals": 1, "format": "plain",
     "expression": "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)"},
    {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.orders"},
]
CLEAN = "## 本周概览\n\n本周（[[i:week]]）销售额 [[m:gmv]]，环比 [[m:wow]]；订单 [[m:orders]]。[[see:m:gmv,m:wow]]"
WOW = "round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)"


def weekly(*, between=(), report=None):
    nodes = [
        node("start", "input", fields=[{"name": "week", "default": "2026-W37"}]),
        node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=STANDARD),
        node("write", "report", instructions="写周报", **({"on_violation": "flag"} | (report or {}))),
        *between,
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"metrics_from": ["caliber"], "report_from": "write", "required": ["gmv"],
                       "strict": True}),
    ]
    chain = ["start", "fetch", "caliber", "write", *[n["id"] for n in between], "out"]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(chain, chain[1:])]}


def script(monkeypatch, text):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str, statuses=("succeeded", "failed")) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            # 状态先提交、封存随后才写：终态要等 manifest_seq 落下来再看
            if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
                return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def run_graph(graph, statuses=("succeeded", "failed")) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    return await wait(run.id, statuses)


async def events(run_id: str, etype: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
            .order_by(RunEvent.seq))).scalars())


def load_doc(doc_id: str) -> dict:
    return json.loads((settings.data_dir / "artifacts" / doc_id[:2] / f"{doc_id}.json").read_text("utf-8"))


def segment_of(doc: dict, text: str) -> dict:
    return next(s for s in iter_segments(doc) if s["text"] == text)


async def reseal(run_id: str) -> None:
    """按改过之后的事件重算封存哈希：模拟「封存链完好、但台账里没有这件工件」。"""
    from app.core.artifact_store import manifest_hash

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.seq <= run.manifest_seq).order_by(RunEvent.seq))]
        run.manifest_hash = manifest_hash(rows)
        await session.commit()


async def cited_run(monkeypatch, text=CLEAN, **kw) -> tuple[Run, dict]:
    script(monkeypatch, text)
    row = await run_graph(weekly(**kw))
    assert row.status == "succeeded", row.error
    return row, load_doc(row.output["_evidence"]["doc_artifact"])


# --------------------------------------------------------------------------
# 证据图
# --------------------------------------------------------------------------


async def test_graph_of_a_cited_run(client, monkeypatch):
    row, doc = await cited_run(monkeypatch)
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()

    assert body["run_id"] == row.id and body["schema"] == "agentlab.evidence/1"
    assert body["mode"] == "cited"
    assert body["seal"] == {"sealed": True, "ok": True, "manifest_seq": row.manifest_seq, "legacy": False}
    [report] = body["reports"]
    assert report["node_id"] == "write" and report["doc_artifact"] == row.output["_evidence"]["doc_artifact"]
    assert report["doc_sealed"] is True and report["hash_ok"] is True and report["ok"] is True
    assert report["fields"] == ["周报"] and report["stats"]["numbers_cited"] == 3

    by_alias = {e["alias"]: e for e in body["evidence"]}
    card = next(e for e in await events(row.id, "node.finished") if e.node_id == "caliber").data
    gmv = by_alias["m:gmv"]
    assert gmv["kind"] == "metric" and gmv["node_id"] == "caliber" and gmv["report"] == "write"
    assert gmv["artifact"] == card["evidence"][0]["artifact"] and gmv["sealed"] is True
    assert gmv["label"] == "销售额 = 45,678.5元" and gmv["eid"].startswith("ev:metric:")
    assert gmv["cited_by"] == [segment_of(doc, "45,678.5元")["id"]]
    week = by_alias["i:week"]
    assert week["kind"] == "input" and week["sealed"] is True
    assert week["eid"] == input_eid("week", "2026-W37")

    edges = body["edges"]
    wow_seg = segment_of(doc, "8.7%")["id"]
    assert {"from": wow_seg, "to": "m:wow", "rel": "cites", "report": "write"} in edges
    inputs = [e for e in edges if e["rel"] == "input" and e["from"] == "m:wow"]
    assert [(e["to"], e["node_id"]) for e in inputs] == [("vars.kpi.gmv", "fetch"), ("vars.kpi.gmv_prev", "fetch")]


async def test_runs_without_a_report(client):
    plain = await run_graph({
        "nodes": [node("start", "input", fields=[{"name": "q", "default": "x"}]),
                  node("out", "output", fields=[{"name": "r", "value": "{{ input.q }}"}])],
        "edges": [{"source": "start", "target": "out"}]})
    body = (await client.get(f"/api/runs/{plain.id}/evidence")).json()
    assert body["mode"] == "none" and body["reports"] == [] and body["evidence"] == []
    assert body["note"]
    miss = await client.get(f"/api/runs/{plain.id}/evidence/segments/s0")
    assert miss.status_code == 404 and miss.json()["code"] == "evidence_report_not_found"


async def test_a_citations_contract_without_a_document_is_not_called_legacy(client, monkeypatch):
    """契约是引用模式，报告却被跳过了：没有文档可看，但这不是「旧版出具」，照实说并带上缘由。"""
    script(monkeypatch, CLEAN)
    row = await run_graph(weekly(report={"skip_if": "input.week == '2026-W37'"}))
    assert row.status == "succeeded", row.error
    assert row.output["_issuance"]["mode"] == "citations"
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert body["mode"] == "none" and "legacy" not in body and body["reports"] == []
    assert "引用模式" in body["note"] and "write" in body["note"] and "报告文档" in body["note"], body["note"]


async def test_legacy_contract_marks_old_matches_by_position(client):
    """旧契约的 matched 带着位置：按位置标出来，并认出偏移对得上的是哪个成果字段。不按数值猜。"""
    story = "本周销售额 45678.5 元，订单 1234 单，另有 999 笔待核。"
    row = await run_graph({
        "nodes": [
            node("start", "input"),
            node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
            node("caliber", "metrics", caliber="周报口径", metrics=STANDARD),
            node("narr", "transform", mode="template", template=story, assign_to="story"),
            # 另一个字段也是文本：偏移只在叙述那个字段上对得上，认字段靠核对位置，不靠「只有一个」
            node("out", "output", fields=[{"name": "说明", "value": "本报告由系统按周生成，数据截至周日"},
                                          {"name": "r", "value": "{{ vars.story }}"}],
                 contract={"metrics_from": ["caliber"], "narrative": "{{ vars.story }}"}),
        ],
        "edges": [{"source": a, "target": b} for a, b in [
            ("start", "fetch"), ("fetch", "caliber"), ("caliber", "narr"), ("narr", "out")]]})
    assert row.status == "succeeded", row.error
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert body["mode"] == "legacy_contract" and body["reports"] == [] and body["evidence"] == []
    legacy = body["legacy"]
    assert "按数值匹配" in legacy["note"] and legacy["tier"] == "degraded" and legacy["field"] == "r"
    assert [(m["token"], m["metric"], m["positioned"]) for m in legacy["matched"]] == [
        ("45678.5", "gmv", True), ("1234", "orders", True)]
    for m in legacy["matched"] + legacy["unmatched"]:
        assert story[m["start"]:m["end"]] == m["token"]
    assert [u["token"] for u in legacy["unmatched"]] == ["999"]


# --------------------------------------------------------------------------
# 片段
# --------------------------------------------------------------------------


async def test_a_metric_segment_opens_its_chain(client, monkeypatch):
    row, doc = await cited_run(monkeypatch)
    seg = segment_of(doc, "8.7%")
    resp = await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["segment"]["id"] == seg["id"] and body["segment"]["text"] == "8.7%"
    assert body["segment"]["state"] == "deterministic" and body["segment"]["span"] == seg["span"]
    assert body["unit"]["kind"] == "claim" and "环比 8.7%" in body["unit"]["text"]
    assert body["segment"]["unit"] == body["unit"]["id"]
    assert body["report"] == {"node_id": "write", "doc_artifact": row.output["_evidence"]["doc_artifact"]}

    metric, *inputs = body["chain"]
    assert metric["step"] == "metric" and metric["alias"] == "m:wow" and metric["name"] == "环比增幅"
    assert metric["value"] == 8.7 and metric["rendered"] == "8.7%" and metric["unit"] == "%"
    assert (metric["caliber"], metric["version"]) == ("周报口径", "v2")
    assert metric["expression"] == WOW
    assert metric["substituted"] == "round((45678.5 - 42010.0) / 42010.0 * 100, 1)"
    assert metric["recompute_ok"] is True and metric["caliber_upgrade"] is None
    assert metric["hash_ok"] is True and metric["eid_ok"] is True and metric["render_ok"] is True
    assert metric["sealed"] is True and metric["node_id"] == "caliber"
    assert [(i["step"], i["path"], i["value"], i["node_id"], i["via"]) for i in inputs] == [
        ("input", "vars.kpi.gmv", 45678.5, "fetch", "transform"),
        ("input", "vars.kpi.gmv_prev", 42010.0, "fetch", "transform")]
    assert body["seal"] == {"sealed": True, "ok": True, "covered": True}
    assert body["redacted"] == {"columns": []}


async def test_an_input_segment_opens_the_run_input(client, monkeypatch):
    row, doc = await cited_run(monkeypatch)
    seg = segment_of(doc, "2026-W37")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    [step] = body["chain"]
    assert step["step"] == "run_input" and step["field"] == "week" and step["value"] == "2026-W37"
    assert step["eid_ok"] is True and step["sealed"] is True and step["node_id"] == "start"
    assert body["seal"]["covered"] is True


async def test_uncited_and_connective_segments_explain_themselves(client, monkeypatch):
    row, doc = await cited_run(monkeypatch, "本周销售额 45678 元。下面分区域看。")
    bare = next(s for s in iter_segments(doc) if s.get("issue") == "uncited_number")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{bare['id']}")).json()
    assert body["chain"] == [] and "没有出处" in body["note"]
    assert [v["code"] for v in body["violations"]] == ["uncited_number"]

    link = next(s for s in iter_segments(doc) if s["text"] == "下面分区域看。")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{link['id']}")).json()
    assert body["unit"]["kind"] == "connective" and "连接性" in body["note"] and body["chain"] == []


async def test_missing_things_are_coded_404s(client, monkeypatch):
    row, _ = await cited_run(monkeypatch)
    resp = await client.get(f"/api/runs/{row.id}/evidence/segments/s9999")
    assert resp.status_code == 404 and resp.json()["code"] == "evidence_segment_not_found"
    assert resp.json()["detail"]
    resp = await client.get(f"/api/runs/{row.id}/evidence/segments/s0", params={"report": "nope"})
    assert resp.status_code == 404 and resp.json()["code"] == "evidence_report_not_found"
    for path in ("/api/runs/no-such-run/evidence", "/api/runs/no-such-run/evidence/segments/s0"):
        resp = await client.get(path)
        assert resp.status_code == 404 and resp.json()["code"] == "run_not_found"


# --------------------------------------------------------------------------
# 只信封存链
# --------------------------------------------------------------------------


async def test_events_appended_after_the_seal_are_ignored(client, monkeypatch):
    """封存之后插进来的 report.checked，哪怕指向一份真的文档，也不能让一次没有报告的运行
    变成「有证据」。"""
    cited, _ = await cited_run(monkeypatch)
    doc_id = cited.output["_evidence"]["doc_artifact"]
    plain = await run_graph({
        "nodes": [node("start", "input", fields=[{"name": "q", "default": "x"}]),
                  node("out", "output", fields=[{"name": "r", "value": "{{ input.q }}"}])],
        "edges": [{"source": "start", "target": "out"}]})
    async with SessionLocal() as session:
        session.add(RunEvent(run_id=plain.id, seq=plain.manifest_seq + 5, type="report.checked",
                             node_id="write", ts=0.0, data={"doc_artifact": doc_id, "ok": True}))
        await session.commit()
    body = (await client.get(f"/api/runs/{plain.id}/evidence")).json()
    assert body["mode"] == "none" and body["reports"] == []
    assert body["seal"]["ok"] is True          # 封存范围内的事件没被动过


async def test_a_tampered_document_is_reported_not_served(client, monkeypatch):
    row, doc = await cited_run(monkeypatch)
    doc_id = row.output["_evidence"]["doc_artifact"]
    path = settings.data_dir / "artifacts" / doc_id[:2] / f"{doc_id}.json"
    body = json.loads(path.read_text("utf-8"))
    body["markdown"] = body["markdown"].replace("45,678.5", "54,678.5")
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    [report] = graph["reports"]
    assert report["hash_ok"] is False and graph["evidence"] == []
    resp = await client.get(f"/api/runs/{row.id}/evidence/segments/s0")
    assert resp.status_code == 409 and resp.json()["code"] == "evidence_doc_tampered"


async def test_an_unsealed_run_is_marked_as_such(client, monkeypatch):
    """停在审批上的运行还没封存：证据照样能看，但每一件都标「未封存」。"""
    script(monkeypatch, CLEAN)
    run = await run_manager.start(graph=weekly(between=[node("gate", "human", mode="approve", title="确认")]),
                                  input_payload={})
    await wait(run.id, ("interrupted", "failed"))
    body = (await client.get(f"/api/runs/{run.id}/evidence")).json()
    assert body["mode"] == "cited" and body["seal"]["sealed"] is False
    assert body["reports"][0]["doc_sealed"] is False and body["reports"][0]["fields"] == []
    assert all(e["sealed"] is False for e in body["evidence"])
    doc = load_doc(body["reports"][0]["doc_artifact"])
    seg = segment_of(doc, "45,678.5元")
    chain = (await client.get(f"/api/runs/{run.id}/evidence/segments/{seg['id']}")).json()
    assert chain["chain"][0]["step"] == "metric" and chain["seal"] == {"sealed": False, "ok": None,
                                                                       "covered": False}


async def test_a_missing_document_file_is_not_called_tampered(client, monkeypatch):
    row, _ = await cited_run(monkeypatch)
    doc_id = row.output["_evidence"]["doc_artifact"]
    (settings.data_dir / "artifacts" / doc_id[:2] / f"{doc_id}.json").unlink()
    [report] = (await client.get(f"/api/runs/{row.id}/evidence")).json()["reports"]
    assert report["hash_ok"] is None
    resp = await client.get(f"/api/runs/{row.id}/evidence/segments/s0")
    assert resp.status_code == 404 and resp.json()["code"] == "evidence_doc_missing"


async def test_a_metric_the_sealed_ledger_does_not_know_is_not_sealed(client, monkeypatch):
    """文档目录里写着指标集工件 X，但封存的事件里口径卡从没记过 X：链照样能看，却不能标「已封存」。
    这里改掉事件里的台账后重新封存，封存核对是通过的——拦下它的只能是台账核对。"""
    row, doc = await cited_run(monkeypatch)
    async with SessionLocal() as session:
        finished = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "node.finished", RunEvent.node_id == "caliber"))).scalar_one()
        finished.data = {k: v for k, v in finished.data.items() if k != "evidence"}
        await session.commit()
    await reseal(row.id)
    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert graph["seal"]["ok"] is True
    sealed = {e["alias"]: e["sealed"] for e in graph["evidence"]}
    assert sealed["m:gmv"] is False and sealed["i:week"] is True
    seg = segment_of(doc, "45,678.5元")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    assert body["chain"][0]["sealed"] is False and body["chain"][0]["hash_ok"] is True
    assert body["seal"] == {"sealed": True, "ok": True, "covered": False}


async def test_a_broken_seal_marks_nothing_as_sealed(client, monkeypatch):
    """封存范围里随便一条事件被改过，封存链就断了：哪一件证据都不能再标「已封存」，
    哪怕它自己的那几条事件没被碰过。只看 sealed 字段的消费方也不会画出绿色。"""
    row, doc = await cited_run(monkeypatch)
    async with SessionLocal() as session:
        started = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "node.started", RunEvent.node_id == "start"))).scalar_one()
        started.data = {**(started.data or {}), "note": "事后改过"}
        await session.commit()

    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert graph["seal"]["sealed"] is True and graph["seal"]["ok"] is False
    assert graph["mode"] == "cited" and graph["evidence"]
    assert all(e["sealed"] is False for e in graph["evidence"]), graph["evidence"]
    assert graph["reports"][0]["doc_sealed"] is False
    for text in ("45,678.5元", "2026-W37"):
        body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{segment_of(doc, text)['id']}")).json()
        assert body["chain"][0]["sealed"] is False, body["chain"][0]
        assert body["seal"] == {"sealed": True, "ok": False, "covered": False}
