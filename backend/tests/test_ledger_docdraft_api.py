"""从业务文档起草口径（app/ledger/draftplan.py + /api/ledger/drafts…）：一次产出定义、SQL、入口和图。

- 抽取先建一行 drafting、在后台跑模型；结果写成草稿：payload 里有 metrics / sql / entry_fields / trial，工作流和 v1 已建；
- 没有数据源、或数据源没有表结构时 422，一个草稿都不建；SQL 对照不上目录时草稿 failed；
- 有未决问题 → questions_open（图和试跑照样有），没有 → ready；
- 一轮覆盖不完就带着 covered 续跑：第二轮收成第二个取数节点，两组指标挂同一张口径卡；某一轮没交新指标就停；
- PATCH 换指标 / 消问题并重算状态；dismiss 置 rejected 并写审计；列表给计数、按改过的先后排；
- GET /drafts/{id}/trial 交回契约里的形状。

模型一律注入假模型（monkeypatch resolve_draft_model），试跑也换成假的（不启动引擎），测试不发真实请求。
"""
from __future__ import annotations

import copy
import json
import sys
import types
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api import ledger as api_ledger
from app.data import recipe_ai
from app.db.base import SessionLocal
from app.db.models import (AuditEvent, CaliberDraft, Chunk, DataSource, Document, Run, Workflow,
                           WorkflowVersion)
from app.engine.schema import GraphSpec, validate_graph
from app.main import app

#: 真实的试跑实现：autouse 的 fake_trial 会把它换掉，这一个用例换回来跑真引擎
REAL_RUN_TRIAL = api_ledger._run_trial

#: 文档原文：两段，退款率的事实来源
CHUNK_ONE = "退款率 = 已支付后退款的订单数 / 已支付订单数"
CHUNK_TWO = "退款率按门店拆，只算已支付的订单"


def plan_reply(chunk_id: str, *, questions=None, sql: str | None = None, metrics=None) -> str:
    """模型回复：一条指标，引用逐字抄原文。"""
    metric = {
        "id": "m1", "name": "已支付退款率", "synonyms": ["退款率"], "unit": "%", "decimals": 2,
        "expression": "cell(nodes.fetch, 0, 'refund_rate')",
        "definition": {"text": "已支付订单里发生退款的比例", "scope": "全部门店", "exclusions": [],
                       "owner": "运营", "effective_from": "2026-01-01",
                       "citations": [{"chunk": chunk_id, "quote": "只算已支付的订单"}]},
        "open_questions": [],
    }
    return json.dumps({
        "entry_fields": [{"name": "store", "type": "string", "required": False, "default": "",
                          "description": "门店（留空是全部门店）"}],
        "sql": sql or "SELECT COUNT(*) * 1.0 AS refund_rate FROM orders WHERE status = 'paid' "
                      "AND store = '{{ input.store }}'",
        "metrics": metrics if metrics is not None else [metric],
        "open_questions": list(questions or []),
    }, ensure_ascii=False)


class FakeModel:
    """起草模型：ainvoke 返回带 .content 的对象；按顺序交回复，交完重复最后一份。"""

    model_name = "fake-model"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        from types import SimpleNamespace

        return SimpleNamespace(content=self.replies.pop(0) if len(self.replies) > 1 else self.replies[0])


def fake_model(monkeypatch, *replies: str) -> FakeModel:
    model = FakeModel(list(replies))

    async def resolve(_session):
        from types import SimpleNamespace

        return model, model.model_name, SimpleNamespace(available=True, reason="")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", resolve)
    return model


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def fake_trial(monkeypatch):
    """试跑换成写一行 succeeded 的运行：不启动引擎、不查库，接口和 payload 的形状照真的一样。"""
    made: list[str] = []

    async def start(*, draft_id, workflow_id, graph, values, started_by):
        run_id = f"run-draft-{uuid.uuid4().hex[:10]}"
        async with SessionLocal() as session:
            session.add(Run(id=run_id, workflow_id=workflow_id, status="succeeded", graph=graph,
                            input=values, run_class="exploratory", origin_kind="caliber_trial",
                            origin={"kind": "caliber_trial", "draft_id": draft_id,
                                    "workflow_id": workflow_id, "version": 1}))
            await session.commit()
        made.append(run_id)
        return {"run_id": run_id, "status": "succeeded", "error": ""}

    monkeypatch.setattr(api_ledger, "_run_trial", start)
    yield made
    async with SessionLocal() as session:
        for run_id in made:
            row = await session.get(Run, run_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


@pytest.fixture
async def documents():
    """建文档和段落（ordinal 故意倒着插，确认读出来按 ordinal 排）。测试结束删文档、段落、草稿和草稿建出的工作流。"""
    made: list[str] = []

    async def make(doc_id: str, *, title: str = "销售口径.md", collection: str = "default") -> str:
        chunks = ((2, CHUNK_TWO), (1, CHUNK_ONE))
        async with SessionLocal() as session:
            session.add(Document(id=doc_id, collection=collection, title=title, source=title,
                                 content="".join(content for _, content in chunks), chunk_count=len(chunks)))
            for ordinal, content in chunks:
                session.add(Chunk(id=f"{doc_id}-{ordinal}", document_id=doc_id, ordinal=ordinal,
                                  content=content))
            await session.commit()
        made.append(doc_id)
        return doc_id

    yield make
    async with SessionLocal() as session:
        drafts = (await session.execute(select(CaliberDraft))).scalars().all()
        workflow_ids = [draft.workflow_id for draft in drafts if draft.workflow_id]
        # attach 会把 workflow_id 换成接上的那一份；起草时建的那份仍在 payload 里，照样要清掉
        workflow_ids += [draft.payload.get("workflow_id") for draft in drafts
                         if isinstance(draft.payload, dict) and draft.payload.get("workflow_id")]
        workflow_ids = list(dict.fromkeys(workflow_ids))
        for draft in drafts:
            await session.delete(draft)
        if workflow_ids:
            await session.execute(delete(WorkflowVersion).where(WorkflowVersion.workflow_id.in_(workflow_ids)))
            await session.execute(delete(Workflow).where(Workflow.id.in_(workflow_ids)))
        for doc_id in made:
            await session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
            doc = await session.get(Document, doc_id)
            if doc is not None:
                await session.delete(doc)
        await session.commit()


@pytest.fixture
async def datasource():
    """一个有表结构的数据源：SQL 只能照这里的表写。"""
    source_id = f"ds-{uuid.uuid4().hex[:10]}"

    async def make(*, tables: bool = True) -> str:
        cache = {"tables": {"orders": {"columns": [
            {"name": "status", "type": "TEXT"}, {"name": "store", "type": "TEXT"},
            {"name": "amount", "type": "NUMERIC"}, {"name": "paid_at", "type": "TEXT"}]}}, "total": 1} \
            if tables else {}
        async with SessionLocal() as session:
            session.add(DataSource(id=source_id, name=f"shop_{uuid.uuid4().hex[:8]}", kind="sqlite",
                                   description="销售库", schema_cache=cache))
            await session.commit()
        return source_id

    yield make
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        if row is not None:
            await session.delete(row)
        await session.commit()


async def extract(client, doc: str, source_id: str, **extra) -> dict:
    body = (await client.post("/api/ledger/drafts/extract",
                              json={"documents": [doc], "datasource_id": source_id, **extra})).json()
    return body


# ==========================================================================
# 主路径
# ==========================================================================

async def test_extract_produces_a_ready_draft_with_a_graph_and_a_trial(client, documents, datasource, monkeypatch):
    doc = await documents("dd-1")
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))

    body = await extract(client, doc, source_id)
    assert body["status"] == "drafting"

    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "ready"
    assert made["title"] == "销售口径.md"
    assert made["workflow_id"] and made["workflow_name"] == "销售口径.md"
    assert made["metric_count"] == 1 and made["question_count"] == 0
    assert [m["name"] for m in made["metrics"]] == ["已支付退款率"]
    assert made["metrics"][0]["expression"] == "cell(nodes.fetch, 0, 'refund_rate')"
    assert made["metrics"][0]["definition"]["citations"][0]["span"] == [8, 16]
    assert made["sql"].startswith("SELECT COUNT(*)") and "{{ input.store }}" in made["sql"]
    assert [f["name"] for f in made["entry_fields"]] == ["store"]
    assert made["trial"]["run_id"] and made["trial"]["status"] == "succeeded"
    assert [d["document_id"] for d in made["documents"]] == [doc]
    assert made["documents"][0]["chunk_ids"] == [f"{doc}-1", f"{doc}-2"]
    assert made["alignment"]["new"] == ["已支付退款率"]
    assert made["truncated"] is False and made["model_id"] == "fake-model"
    assert made["datasource_id"] == source_id

    # 工作流和 v1 已建：合法的草稿图，取数指向这个数据源
    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, body["draft_id"])
        workflow = await session.get(Workflow, row.workflow_id)
        version = (await session.execute(select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == row.workflow_id))).scalars().one()
    assert workflow.status == "draft" and workflow.tags == ["doc_draft"]
    assert version.version == 1 and version.graph == workflow.graph
    assert [n["id"] for n in workflow.graph["nodes"]] == ["start", "fetch", "card", "write", "out"]
    assert validate_graph(GraphSpec.model_validate(workflow.graph)).ok

    # 试跑只读接口：契约里的形状
    trial = (await client.get(f"/api/ledger/drafts/{body['draft_id']}/trial")).json()
    assert set(trial) == {"run_id", "status", "metrics", "error"}
    assert trial["run_id"] == made["trial"]["run_id"] and trial["status"] == "succeeded"


async def test_open_questions_keep_the_draft_in_questions_open_but_still_run_it(
        client, documents, datasource, monkeypatch):
    doc = await documents("dd-2")
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2", questions=["退款算在哪一天"]))

    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "questions_open"
    assert made["open_questions"] == ["退款算在哪一天"]
    assert made["workflow_id"] and made["sql"]
    assert made["trial"]["status"] == "succeeded"


async def test_a_missing_or_empty_datasource_is_refused_and_no_draft_is_created(client, documents, datasource):
    doc = await documents("dd-3")
    without = await client.post("/api/ledger/drafts/extract", json={"documents": [doc]})
    assert without.status_code == 422 and "数据源" in without.text
    unknown = await client.post("/api/ledger/drafts/extract",
                                json={"documents": [doc], "datasource_id": "ds-nope"})
    assert unknown.status_code == 422 and "数据源不存在" in unknown.text
    empty_source = await datasource(tables=False)
    empty = await client.post("/api/ledger/drafts/extract",
                              json={"documents": [doc], "datasource_id": empty_source})
    assert empty.status_code == 422 and "表结构" in empty.text
    assert (await client.get("/api/ledger/drafts")).json()["drafts"] == []


async def test_a_missing_document_is_refused_and_no_draft_is_created(client, datasource):
    source_id = await datasource()
    res = await client.post("/api/ledger/drafts/extract",
                            json={"documents": ["d-nope"], "datasource_id": source_id})
    assert res.status_code == 422 and "d-nope" in res.text
    assert (await client.get("/api/ledger/drafts")).json()["drafts"] == []


async def test_sql_outside_the_catalog_fails_the_draft_with_a_reason(client, documents, datasource, monkeypatch):
    doc = await documents("dd-4")
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2", sql="SELECT * FROM secrets"))

    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "failed" and made["workflow_id"] is None
    assert "secrets" in made["error"] and "数据目录" in made["error"]
    assert any("secrets" in d["reason"] for d in made["dropped"])
    assert not made.get("sql")
    assert (await client.get(f"/api/ledger/drafts/{body['draft_id']}/trial")).status_code == 404


async def test_a_metric_with_a_bad_expression_is_dropped_but_the_draft_still_goes_on(
        client, documents, datasource, monkeypatch):
    doc = await documents("dd-5")
    source_id = await datasource()
    reply = json.loads(plan_reply(f"{doc}-2"))
    reply["metrics"].append({**reply["metrics"][0], "id": "m2", "name": "订单数", "expression": "SUM(amount)"})
    fake_model(monkeypatch, json.dumps(reply, ensure_ascii=False))

    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    # 一条好、一条坏：坏的那条进 dropped / open_questions，草稿照样 ready 之外还有图
    assert [m["id"] for m in made["metrics"]] == ["m1"]
    assert any(d["name"] == "订单数" and "cell(nodes.fetch, 0" in d["reason"] for d in made["dropped"])
    assert any("订单数" in q for q in made["open_questions"])
    assert made["status"] == "questions_open" and made["workflow_id"]


# ==========================================================================
# 改、驳回、列表、试跑接口
# ==========================================================================

async def test_patch_replaces_metrics_and_clears_resolved_questions(client, documents, datasource, monkeypatch):
    doc = await documents("dd-6")
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2", questions=["按哪个时区结算"]))
    draft_id = (await extract(client, doc, source_id))["draft_id"]

    fixed = {**copy.deepcopy(json.loads(plan_reply(f"{doc}-2"))["metrics"][0]), "name": "已支付退款率（按支付日）"}
    res = await client.patch(f"/api/ledger/drafts/{draft_id}",
                             json={"title": "退款率口径", "metrics": [fixed],
                                   "resolved": ["按哪个时区结算"]})
    assert res.status_code == 200, res.text
    made = res.json()
    assert made["title"] == "退款率口径" and made["status"] == "ready"
    assert [m["name"] for m in made["metrics"]] == ["已支付退款率（按支付日）"]
    assert made["open_questions"] == [] and made["question_count"] == 0

    # 结构不对的指标 422，草稿不动
    bad = await client.patch(f"/api/ledger/drafts/{draft_id}", json={"metrics": [{"synonyms": ["x"]}]})
    assert bad.status_code == 422
    still = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
    assert [m["name"] for m in still["metrics"]] == ["已支付退款率（按支付日）"]

    # 还有问题（只换指标、带着新问题）→ questions_open
    res = await client.patch(f"/api/ledger/drafts/{draft_id}",
                             json={"metrics": [{**fixed, "open_questions": ["按哪个门店口径"]}]})
    assert res.json()["status"] == "questions_open" and res.json()["question_count"] == 1


async def test_dismiss_marks_rejected_and_writes_an_audit(client, documents, datasource, monkeypatch):
    doc = await documents("dd-7")
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))
    draft_id = (await extract(client, doc, source_id))["draft_id"]

    res = await client.post(f"/api/ledger/drafts/{draft_id}/dismiss", json={"note": "已经有口径了"})
    assert res.status_code == 200 and res.json()["status"] == "rejected"
    async with SessionLocal() as session:
        event = (await session.execute(select(AuditEvent).where(
            AuditEvent.action == "ledger.docdraft.dismiss", AuditEvent.subject_id == draft_id))).scalars().one()
    assert event.subject_kind == "ledger_draft" and event.data["note"] == "已经有口径了"
    assert (await client.get(f"/api/ledger/drafts/{draft_id}")).json()["status"] == "rejected"


async def test_list_counts_orders_by_last_change_and_filters_by_status(client, documents, datasource, monkeypatch):
    first = await documents("dd-8", title="一.md")
    second = await documents("dd-9", title="二.md")
    source_id = await datasource()
    fake_model(monkeypatch,
               plan_reply(f"{first}-2", questions=["按哪个时区结算"]),
               plan_reply(f"{second}-2"))
    one = (await extract(client, first, source_id))["draft_id"]
    two = (await extract(client, second, source_id))["draft_id"]

    listed = (await client.get("/api/ledger/drafts")).json()["drafts"]
    assert [d["id"] for d in listed] == [two, one]
    latest, earlier = listed
    assert (latest["title"], latest["status"], latest["metric_count"], latest["question_count"]) == \
        ("二.md", "ready", 1, 0)
    assert (earlier["status"], earlier["metric_count"], earlier["question_count"]) == ("questions_open", 1, 1)
    assert earlier["workflow_id"] and earlier["created_at"] and earlier["error"] is None

    only = (await client.get("/api/ledger/drafts", params={"status": "questions_open"})).json()["drafts"]
    assert [d["id"] for d in only] == [one]
    assert (await client.get("/api/ledger/drafts", params={"status": "nope"})).status_code == 422
    assert (await client.get("/api/ledger/drafts/d-nope")).status_code == 404


async def test_trial_endpoint_returns_metric_values_from_the_run(client, documents, datasource, monkeypatch):
    doc = await documents("dd-10")
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))
    draft_id = (await extract(client, doc, source_id))["draft_id"]

    async def metrics(_run_id):
        return [{"id": "m1", "name": "已支付退款率", "rendered": "3.2%", "value": 0.032}]

    monkeypatch.setattr(api_ledger, "_trial_metrics", metrics)
    res = await client.get(f"/api/ledger/drafts/{draft_id}/trial")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"run_id": body["run_id"], "status": "succeeded",
                    "metrics": [{"id": "m1", "name": "已支付退款率", "rendered": "3.2%", "value": 0.032}],
                    "error": ""}

    # 没有试跑的老草稿
    async with SessionLocal() as session:
        session.add(CaliberDraft(id=f"draft-old-{uuid.uuid4().hex[:8]}", title="老草稿", status="path_pending",
                                 payload={"metrics": []}))
        await session.commit()
        old = (await session.execute(select(CaliberDraft.id).where(
            CaliberDraft.status == "path_pending"))).scalars().first()
    assert (await client.get(f"/api/ledger/drafts/{old}/trial")).status_code == 404


async def test_a_model_that_is_unavailable_leaves_the_draft_with_an_error(client, documents, datasource,
                                                                         monkeypatch):
    doc = await documents("dd-11")
    source_id = await datasource()

    async def unavailable(_session):
        from types import SimpleNamespace

        return None, "", SimpleNamespace(available=False, reason="还没有接入可用的起草模型")

    monkeypatch.setattr(recipe_ai, "resolve_draft_model", unavailable)
    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "failed" and made["error"] == "还没有接入可用的起草模型"
    assert made["metric_count"] == 0 and not made.get("metrics")


async def test_attach_hands_the_draft_to_docattach_and_records_the_workflow(client, documents, datasource,
                                                                           monkeypatch):
    """attach 只按约定调 app.ledger.docattach.attach（辅助入口）：这里换成一个假的模块，看接线和写回。"""
    doc = await documents("dd-12")
    source_id = await datasource()
    fake_model(monkeypatch, plan_reply(f"{doc}-2"))
    draft_id = (await extract(client, doc, source_id))["draft_id"]

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


async def test_a_real_trial_run_feeds_the_trial_endpoint(client, documents, tmp_path, monkeypatch):
    """真引擎跑一次草稿图：数从取数节点进口径卡，GET /trial 交回指标值（不 mock 试跑）。"""
    from app.engine.runner import run_manager
    from tests import ledger_samples as S

    await run_manager.setup()
    run_id = None
    try:
        name = await S.fresh_source(tmp_path / "shop.db")
        async with SessionLocal() as session:
            row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one()
            row.schema_cache = {"tables": {"orders": {"columns": [
                {"name": "status", "type": "TEXT"}, {"name": "region", "type": "TEXT"},
                {"name": "amount", "type": "REAL"}, {"name": "paid_at", "type": "TEXT"}]}}, "total": 1}
            await session.commit()
            source_id = row.id
        doc = await documents("dd-real")
        reply = json.loads(plan_reply(f"{doc}-2"))
        reply["entry_fields"] = [{"name": "region", "type": "string", "required": False,
                                  "default": "east", "description": "区域"}]
        reply["sql"] = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
                        "AND region = '{{ input.region }}'")
        reply["metrics"][0].update({"expression": "cell(nodes.fetch, 0, 'gmv')", "name": "已支付销售额",
                                    "unit": "元", "decimals": 2})
        fake_model(monkeypatch, json.dumps(reply, ensure_ascii=False))
        # 报告节点也要一个模型：用品自带的 mock 模型，正文引用卡上的指标，核对规则照过
        S.script(monkeypatch, "已支付销售额 [[m:m1]]。")
        monkeypatch.setattr(api_ledger, "_run_trial", REAL_RUN_TRIAL)

        draft_id = (await extract(client, doc, source_id))["draft_id"]
        made = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert made["status"] == "ready" and made["trial"]["status"] == "succeeded", made.get("error")
        run_id = made["trial"]["run_id"]

        trial = (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).json()
        assert trial["status"] == "succeeded" and trial["error"] == ""
        assert [m["id"] for m in trial["metrics"]] == ["m1"]
        assert trial["metrics"][0]["name"] == "已支付销售额"
        assert float(trial["metrics"][0]["value"]) == 156.5
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
        assert run.origin_kind == "caliber_trial" and run.run_class == "exploratory"
        assert not [n for n in (run.graph or {}).get("nodes") or [] if n.get("type") == "report"], \
            "试跑只跑取数与口径卡，不该触发报告撰写"
        assert run.origin["kind"] == "caliber_trial" and run.origin["draft_id"] == draft_id
        assert run.origin["workflow_id"] == made["workflow_id"] and run.origin["version"] == 1
    finally:
        await run_manager.shutdown()
        async with SessionLocal() as session:
            if run_id:
                row = await session.get(Run, run_id)
                if row is not None:
                    await session.delete(row)
            source = (await session.execute(select(DataSource).where(
                DataSource.name == S.CURRENT["source"]))).scalars().first()
            if source is not None:
                await session.delete(source)
            await session.commit()


async def test_a_big_document_is_drafted_in_several_rounds_onto_one_multi_fetch_graph(
        client, documents, datasource, monkeypatch):
    """第一轮交一组、uncovered 还剩一条：续跑第二轮，收成一张两个取数节点、卡上两条指标的图。"""
    doc = await documents("dd-rounds")
    source_id = await datasource()
    first = json.loads(plan_reply(f"{doc}-2"))
    first["uncovered"] = ["已支付订单数"]
    second = json.loads(plan_reply(f"{doc}-2"))
    second["sql"] = ("SELECT COUNT(*) AS orders FROM orders WHERE status = 'paid' "
                     "AND store = '{{ input.store }}'")
    second["metrics"] = [{**second["metrics"][0], "id": "m1", "name": "已支付订单数",
                          "expression": "cell(nodes.fetch, 0, 'orders')"}]
    second["uncovered"] = []
    model = fake_model(monkeypatch, json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))

    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()

    assert made["status"] == "ready"
    assert [m["name"] for m in made["metrics"]] == ["已支付退款率", "已支付订单数"]
    assert made["blocks"] == 2
    assert made["progress"] == {"round": 2, "covered": 2, "uncovered": 0}
    assert made["truncated"] is False and made["doc_truncated"] is False and made["catalog_truncated"] is False
    # 第二轮提示词写明上一轮起草过什么：模型据此不再重复起草
    assert len(model.calls) == 2 and "已支付退款率" in str(model.calls[1][-1].content)

    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, body["draft_id"])
        workflow = await session.get(Workflow, row.workflow_id)
    graph = workflow.graph
    assert [n["id"] for n in graph["nodes"]] == ["start", "fetch_1", "fetch_2", "card", "write", "out"]
    card = next(n for n in graph["nodes"] if n["id"] == "card")["data"]["config"]["metrics"]
    # 两组都交 m1：跨组重名按 m1…mN 重排，表达式改写到各自的取数节点
    assert [m["id"] for m in card] == ["m1", "m2"]
    assert [m["expression"] for m in card] == ["cell(nodes.fetch_1, 0, 'refund_rate')",
                                               "cell(nodes.fetch_2, 0, 'orders')"]
    assert graph["nodes"][-1]["data"]["config"]["contract"]["required"] == ["m1", "m2"]
    assert validate_graph(GraphSpec.model_validate(graph)).ok
    assert made["trial"]["status"] == "succeeded" and made["trial"]["run_id"]


async def test_a_round_that_brings_nothing_new_stops_the_loop_without_a_half_built_group(
        client, documents, datasource, monkeypatch):
    """第二轮什么也没交：停在那一轮，不拿空 SQL 拼出第三组取数，也不留一条半成品指标。"""
    doc = await documents("dd-rounds-stop")
    source_id = await datasource()
    first = json.loads(plan_reply(f"{doc}-2"))
    first["uncovered"] = ["已支付订单数"]
    fake_model(monkeypatch, json.dumps(first, ensure_ascii=False), json.dumps({
        "entry_fields": [], "sql": "", "metrics": [], "open_questions": [], "uncovered": ["已支付订单数"],
    }, ensure_ascii=False))

    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "ready" and made["blocks"] == 1 and made["metric_count"] == 1
    assert made["progress"] == {"round": 2, "covered": 1, "uncovered": 1}
    assert made["uncovered"] == ["已支付订单数"]
    async with SessionLocal() as session:
        row = await session.get(CaliberDraft, body["draft_id"])
        workflow = await session.get(Workflow, row.workflow_id)
    assert [n["id"] for n in workflow.graph["nodes"]] == ["start", "fetch", "card", "write", "out"]
    card = next(n for n in workflow.graph["nodes"] if n["id"] == "card")
    assert [m["id"] for m in card["data"]["config"]["metrics"]] == ["m1"]


async def test_conflicting_entry_field_declarations_are_merged_not_failed(
        client, documents, datasource, monkeypatch):
    """两组把 region 声明得不一样：必填取并集、类型取更宽，草稿照常出图，只把不一致摆给人看。"""
    doc = await documents("dd-rounds-clash")
    source_id = await datasource()
    first = json.loads(plan_reply(f"{doc}-2"))
    first["uncovered"] = ["已支付订单数"]
    second = json.loads(plan_reply(f"{doc}-2"))
    second["entry_fields"] = [{**second["entry_fields"][0], "required": True}]
    second["metrics"] = [{**second["metrics"][0], "id": "m2", "name": "已支付订单数",
                          "expression": "cell(nodes.fetch, 0, 'orders')"}]
    second["uncovered"] = []
    fake_model(monkeypatch, json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))

    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] in ("ready", "questions_open") and made["workflow_id"] is not None
    assert made["metric_count"] == 2 and made["blocks"] == 2
    fields = {f["name"]: f for f in made["entry_fields"]}
    assert fields["store"]["required"] is True
    assert any("store" in note for note in made["entry_notes"])


async def test_a_real_trial_run_reads_values_from_both_fetch_nodes(client, documents, tmp_path, monkeypatch):
    """两个取数节点的草稿图真跑一次探针：两条 SQL 各出一个值，卡上两条指标都读得出来（不 mock 试跑）。"""
    from app.engine.runner import run_manager
    from tests import ledger_samples as S

    await run_manager.setup()
    run_id = None
    try:
        name = await S.fresh_source(tmp_path / "shop-two.db")
        async with SessionLocal() as session:
            row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one()
            row.schema_cache = {"tables": {"orders": {"columns": [
                {"name": "status", "type": "TEXT"}, {"name": "region", "type": "TEXT"},
                {"name": "amount", "type": "REAL"}, {"name": "paid_at", "type": "TEXT"}]}}, "total": 1}
            await session.commit()
            source_id = row.id
        doc = await documents("dd-real-two")
        fields = [{"name": "region", "type": "string", "required": False, "default": "east",
                   "description": "区域"}]
        gmv = json.loads(plan_reply(f"{doc}-2"))
        gmv.update({"entry_fields": fields, "uncovered": ["已支付订单数"]})
        gmv["sql"] = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
                      "AND region = '{{ input.region }}'")
        gmv["metrics"][0].update({"expression": "cell(nodes.fetch, 0, 'gmv')", "name": "已支付销售额",
                                  "unit": "元", "decimals": 2})
        count = json.loads(plan_reply(f"{doc}-2"))
        count.update({"entry_fields": fields, "uncovered": []})
        count["sql"] = ("SELECT COUNT(*) AS orders FROM orders WHERE status = 'paid' "
                        "AND region = '{{ input.region }}'")
        count["metrics"] = [{**count["metrics"][0], "id": "m1", "name": "已支付订单数", "unit": "单",
                             "decimals": 0, "expression": "cell(nodes.fetch, 0, 'orders')"}]
        fake_model(monkeypatch, json.dumps(gmv, ensure_ascii=False), json.dumps(count, ensure_ascii=False))
        S.script(monkeypatch, "已支付销售额 [[m:m1]]，已支付订单数 [[m:m2]]。")
        monkeypatch.setattr(api_ledger, "_run_trial", REAL_RUN_TRIAL)

        draft_id = (await extract(client, doc, source_id))["draft_id"]
        made = (await client.get(f"/api/ledger/drafts/{draft_id}")).json()
        assert made["status"] == "ready" and made["trial"]["status"] == "succeeded", made.get("error")
        run_id = made["trial"]["run_id"]

        trial = (await client.get(f"/api/ledger/drafts/{draft_id}/trial")).json()
        assert trial["status"] == "succeeded" and trial["error"] == ""
        assert {m["id"]: float(m["value"]) for m in trial["metrics"]} == {"m1": 156.5, "m2": 4.0}
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
        tools = [n["id"] for n in (run.graph or {}).get("nodes") or [] if n.get("type") == "tool"]
        assert tools == ["fetch_1", "fetch_2"], "试跑要把两组取数都带上"
    finally:
        await run_manager.shutdown()
        async with SessionLocal() as session:
            if run_id:
                row = await session.get(Run, run_id)
                if row is not None:
                    await session.delete(row)
            source = (await session.execute(select(DataSource).where(
                DataSource.name == S.CURRENT["source"]))).scalars().first()
            if source is not None:
                await session.delete(source)
            await session.commit()


async def test_the_model_asking_questions_is_not_a_failure(client, documents, datasource, monkeypatch):
    """模型说「目录里没有这些列、写不了 SQL」时，这是要补信息的草稿，不是失败：问题原样留下，不编指标。"""
    doc = await documents("dd-questions")
    source_id = await datasource()
    fake_model(monkeypatch, json.dumps({
        "entry_fields": [], "sql": "", "metrics": [],
        "open_questions": ["数据目录里没有 attribute01…54 的取值列，写不出时间类指标"],
    }, ensure_ascii=False))

    body = await extract(client, doc, source_id)
    made = (await client.get(f"/api/ledger/drafts/{body['draft_id']}")).json()
    assert made["status"] == "questions_open" and made["error"] == ""
    assert made["open_questions"] == ["数据目录里没有 attribute01…54 的取值列，写不出时间类指标"]
    assert made["metrics"] == [] and made["workflow_id"] is None
    assert made["trial"]["run_id"] == ""


def test_the_schema_text_expands_the_tables_the_document_names_first():
    """目录十万字符、预算只有两万多：先展开文档点到的表，其余只靠列名清单认。"""
    tables = ["orders", "refunds", "stores", "kpi_info", "attr_info"]
    picked = api_ledger._focus_tables(tables, "指标取自 kpi_info，属性在 attr_info 里")
    assert picked[:2] == ["kpi_info", "attr_info"]
    assert set(picked) >= {"kpi_info", "attr_info"}
