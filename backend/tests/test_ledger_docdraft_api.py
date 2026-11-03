"""从业务文档起草口径（app/ledger/docdraft.py，/api/ledger/drafts…）：草稿独立于工作流存在。

- 抽取先建一行 drafting、在后台跑模型，结果写回 payload；缺文档 422、一个草稿都不建；
- 草稿只产出定义：payload 里是 metrics / dropped / open_questions / alignment，没有 SQL；
- PATCH 整体换指标、消掉已答清的问题并重算状态；dismiss 置 rejected 并写审计；列表给计数、按改过的先后排。

模型一律 monkeypatch 掉（docdraft.extract 换成一个固定结果），测试不发真实请求。
"""
from __future__ import annotations

import copy
import sys
import types

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.base import SessionLocal
from app.db.models import AuditEvent, CaliberDraft, Chunk, Document, Workflow
from app.ledger import docdraft
from app.main import app

METRIC = {
    "name": "已支付退款率",
    "synonyms": ["退款率"],
    "unit": "%",
    "grain": "日",
    "time_field": "支付时间",
    "filters": ["只算已支付"],
    "numerator": "已支付后退款的订单数",
    "denominator": "已支付订单数",
    "dimensions": ["门店"],
    "definition": {"text": "已支付订单里发生退款的比例", "scope": "全部门店", "exclusions": [], "owner": "运营",
                   "effective_from": "2026-01-01",
                   "citations": [{"chunk": "c1", "document_id": "d1", "span": [0, 2], "quote": "退款率"}]},
    "open_questions": ["退款算不算已支付当天的"],
}


def result(metrics=None, questions=None, model_id="fake-model"):
    return {"metrics": copy.deepcopy(metrics if metrics is not None else [METRIC]),
            "dropped": [{"name": "第 2 条", "reason": "没有一条引用能在原文里逐字核对上"}],
            "open_questions": list(questions or []), "truncated": False, "model_id": model_id}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def documents():
    """建文档和段落（ordinal 故意倒着插，确认读出来按 ordinal 排）。段落 id 带上文档 id，测试之间不撞。
    测试结束删掉这几份文档、它们的段落和所有草稿。"""
    made: list[str] = []

    async def make(doc_id: str, *, title: str = "销售口径.md", collection: str = "default",
                   chunks=((2, "退款率 = 已支付后退款的订单数 / 已支付订单数"), (1, "退款率按门店拆"))) -> str:
        async with SessionLocal() as session:
            session.add(Document(id=doc_id, collection=collection, title=title, source=title,
                                 content="".join(content for _, content in chunks), chunk_count=len(chunks)))
            for ordinal, content in chunks:
                session.add(Chunk(id=f"{doc_id}-{ordinal}", document_id=doc_id, ordinal=ordinal, content=content))
            await session.commit()
        made.append(doc_id)
        return doc_id

    yield make
    async with SessionLocal() as session:
        for doc_id in made:
            await session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
            doc = await session.get(Document, doc_id)
            if doc is not None:
                await session.delete(doc)
        for draft in (await session.execute(select(CaliberDraft))).scalars():
            await session.delete(draft)
        await session.commit()


def fake_extract(monkeypatch, *results: dict, seen: dict | None = None) -> None:
    """把 docdraft.extract 换成固定结果：每次调用按顺序交一份，交完了重复最后一份。"""
    queue = list(results)

    async def extract(session, *, docs, chunks, guidance="", datasource=None, model=None):
        if seen is not None:
            seen.update(docs=docs, chunks=chunks, guidance=guidance, datasource=datasource)
        return copy.deepcopy(queue.pop(0) if len(queue) > 1 else queue[0])

    monkeypatch.setattr(docdraft, "extract", extract)


async def test_extract_reads_documents_in_order_and_stores_a_questions_open_draft(client, documents, monkeypatch):
    doc = await documents("d-draft-1")
    seen: dict = {}
    fake_extract(monkeypatch, result(questions=["按哪个时区结算"]), seen=seen)

    res = await client.post("/api/ledger/drafts/extract",
                            json={"documents": [doc], "guidance": "只看已支付的订单"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert set(body) == {"draft_id", "status"}
    # 抽取在后台跑：建行那一刻回 drafting，结果用 GET 看
    assert body["status"] == "drafting"

    # 交给模型的是按 ordinal 排好的段落，标题和 guidance 也传下去了
    first, second = f"{doc}-1", f"{doc}-2"
    assert seen["docs"] == [{"document_id": doc, "title": "销售口径.md", "chunk_ids": [first, second]}]
    assert [seen["chunks"][c]["ordinal"] for c in (first, second)] == [1, 2]
    assert seen["chunks"][first]["document_id"] == doc
    assert seen["guidance"] == "只看已支付的订单" and seen["datasource"] is None

    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "questions_open" and made["title"] == "销售口径.md"
    assert made["workflow_id"] is None and made["workflow_name"] is None
    assert made["model_id"] == "fake-model"
    assert [m["name"] for m in made["metrics"]] == ["已支付退款率"]
    assert made["metrics"][0]["definition"]["citations"][0]["span"] == [0, 2]
    assert made["open_questions"] == ["按哪个时区结算"]
    assert made["dropped"] == [{"name": "第 2 条", "reason": "没有一条引用能在原文里逐字核对上"}]
    assert set(made["alignment"]) == {"new", "duplicate", "conflict"}
    assert made["alignment"]["new"] == ["已支付退款率"]
    assert made["truncated"] is False
    assert made["origin"] == {"documents": [doc], "guidance": "只看已支付的订单"}
    # 草稿不写进工作流：库里没有从它建出的图
    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, body["draft_id"])
    assert row.workflow_id is None and row.payload["metrics"][0]["name"] == "已支付退款率"


async def test_extract_without_open_questions_is_path_pending(client, documents, monkeypatch):
    doc = await documents("d-draft-2")
    fake_extract(monkeypatch, result(metrics=[{**METRIC, "open_questions": []}]))
    body = (await client.post("/api/ledger/drafts/extract", json={"documents": [doc]})).json()
    assert body["status"] == "drafting"
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "path_pending"
    assert made["open_questions"] == [] and made["question_count"] == 0


async def test_a_missing_document_is_refused_and_no_draft_is_created(client):
    res = await client.post("/api/ledger/drafts/extract", json={"documents": ["d-nope"]})
    assert res.status_code == 422 and "d-nope" in res.text
    listed = (await client.get("/api/ledger/drafts")).json()
    assert listed["drafts"] == []


async def test_patch_replaces_metrics_and_clears_resolved_questions(client, documents, monkeypatch):
    doc = await documents("d-draft-3")
    fake_extract(monkeypatch, result(questions=["按哪个时区结算"]))
    draft_id = (await client.post("/api/ledger/drafts/extract", json={"documents": [doc]})).json()["draft_id"]

    fixed_metric = {**copy.deepcopy(METRIC), "open_questions": [], "name": "已支付退款率（按支付日）"}
    res = await client.patch(f"/api/ledger/drafts/{draft_id}",
                             json={"title": "退款率口径", "metrics": [fixed_metric],
                                   "resolved": ["按哪个时区结算", "退款算不算已支付当天的"]})
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["title"] == "退款率口径" and made["status"] == "path_pending"
    assert [m["name"] for m in made["metrics"]] == ["已支付退款率（按支付日）"]
    assert made["open_questions"] == [] and made["question_count"] == 0

    # 结构不对的指标 422，草稿不动
    bad = await client.patch(f"/api/ledger/drafts/{draft_id}", json={"metrics": [{"synonyms": ["x"]}]})
    assert bad.status_code == 422
    still = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert [m["name"] for m in still["metrics"]] == ["已支付退款率（按支付日）"]

    # 还有问题（只换指标、带着新问题）→ questions_open
    res = await client.patch(f"/api/ledger/drafts/{draft_id}",
                             json={"metrics": [{**fixed_metric, "open_questions": ["按哪个门店口径"]}]})
    assert res.json()["status"] == "questions_open" and res.json()["question_count"] == 1


async def test_dismiss_marks_rejected_and_writes_an_audit(client, documents, monkeypatch):
    doc = await documents("d-draft-4")
    fake_extract(monkeypatch, result())
    draft_id = (await client.post("/api/ledger/drafts/extract", json={"documents": [doc]})).json()["draft_id"]

    res = await client.post(f"/api/ledger/drafts/{draft_id}/dismiss", json={"note": "已经有口径了"})
    assert res.status_code == 200 and res.json()["status"] == "rejected"
    async with SessionLocal() as session:
        event = (await session.execute(select(AuditEvent).where(
            AuditEvent.action == "ledger.docdraft.dismiss", AuditEvent.subject_id == draft_id))).scalars().one()
    assert event.subject_kind == "ledger_draft" and event.data["note"] == "已经有口径了"
    assert (await client.get(f"/api/ledger/drafts/{draft_id}")).json()["status"] == "rejected"


async def test_list_counts_orders_by_last_change_and_filters_by_status(client, documents, monkeypatch):
    first = await documents("d-draft-5", title="一.md")
    second = await documents("d-draft-6", title="二.md")
    fake_extract(monkeypatch,
                 result(metrics=[METRIC, {**METRIC, "name": "退款金额", "open_questions": []}],
                        questions=["按哪个时区结算"]),
                 result(metrics=[{**METRIC, "open_questions": []}]))
    one = (await client.post("/api/ledger/drafts/extract", json={"documents": [first]})).json()["draft_id"]
    two = (await client.post("/api/ledger/drafts/extract", json={"documents": [second]})).json()["draft_id"]

    listed = (await client.get("/api/ledger/drafts")).json()["drafts"]
    assert [d["id"] for d in listed] == [two, one]
    latest, earlier = listed
    assert (latest["title"], latest["status"], latest["metric_count"], latest["question_count"]) == \
        ("二.md", "path_pending", 1, 0)
    assert (earlier["status"], earlier["metric_count"], earlier["question_count"]) == ("questions_open", 2, 2)
    assert earlier["workflow_id"] is None and earlier["created_at"] and earlier["error"] is None

    only = (await client.get("/api/ledger/drafts", params={"status": "questions_open"})).json()["drafts"]
    assert [d["id"] for d in only] == [one]
    assert (await client.get("/api/ledger/drafts", params={"status": "nope"})).status_code == 422
    assert (await client.get("/api/ledger/drafts/d-nope")).status_code == 404


async def test_a_model_that_is_unavailable_leaves_the_draft_with_an_error(client, documents, monkeypatch):
    doc = await documents("d-draft-7")

    async def unavailable(session, **kwargs):
        raise docdraft.DraftUnavailable("还没有接入可用的起草模型")

    monkeypatch.setattr(docdraft, "extract", unavailable)
    body = (await client.post("/api/ledger/drafts/extract", json={"documents": [doc]})).json()
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "drafting" and made["error"] == "还没有接入可用的起草模型"
    assert made["metric_count"] == 0 and not made.get("metrics")


async def test_attach_hands_the_draft_to_docattach_and_records_the_workflow(client, documents, monkeypatch):
    """attach 只按约定调 app.ledger.docattach.attach（另一路实现）：这里换成一个假的模块，看接线和写回。"""
    doc = await documents("d-draft-8")
    fake_extract(monkeypatch, result(metrics=[{**METRIC, "open_questions": []}]))
    draft_id = (await client.post("/api/ledger/drafts/extract", json={"documents": [doc]})).json()["draft_id"]

    seen: dict = {}

    async def attach(session, *, payload, name, run_id, draft_id):
        seen.update(payload=payload, name=name, run_id=run_id, draft_id=draft_id)
        return {"workflow_id": "wf-docdraft", "version": 1, "metrics": ["m1"], "warnings": []}

    module = types.ModuleType("app.ledger.docattach")
    module.attach = attach
    monkeypatch.setitem(sys.modules, "app.ledger.docattach", module)
    async with SessionLocal() as session:
        session.add(Workflow(id="wf-docdraft", name="退款率口径"))
        await session.commit()
    try:
        res = await client.post(f"/api/ledger/drafts/{draft_id}/attach", json={"run_id": "run-1"})
        assert res.status_code == 200, res.text
        assert res.json() == {"workflow_id": "wf-docdraft", "version": 1, "metrics": ["m1"], "warnings": []}
        assert (seen["run_id"], seen["name"], seen["draft_id"]) == ("run-1", "销售口径.md", draft_id)
        assert seen["payload"]["metrics"][0]["name"] == "已支付退款率"
        made = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert made["status"] == "attached" and made["workflow_id"] == "wf-docdraft"
        assert made["workflow_name"] == "退款率口径"
    finally:
        async with SessionLocal() as session:
            workflow = await session.get(Workflow, "wf-docdraft")
            if workflow is not None:
                await session.delete(workflow)
            await session.commit()
