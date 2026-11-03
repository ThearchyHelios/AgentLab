"""文档草稿接上一次真实运行（app/ledger/docattach.py）。

真的跑一次问数据（虚构店铺库），等后台比对做完，再把文档草稿接上这次运行：
- 取数路径来自运行，定义并进口径卡的同名指标，expression / id 一字不改；
- origin 记着用到的文档段落（document_id / chunk_id / ordinal / quote，引用去重）和 draft_id；
- 没成功、没比对、报告没引用查询的运行都接不上（409）；
- 草稿里对不上的指标名进 warnings，不拦这一次接入；
- 只落草稿：不发布、不发起运行。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal
from app.db.models import Chunk, Conversation, ConversationTurn, Document, Run, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.ledger import docattach
from app.ledger import match as ledger_match
from tests import ledger_samples as S

#: 每个用例一句话的问题：问句进全站唯一的聚类，和别的用例用同一句会在 certify 的队列里互相看见
QUESTION = f"文档接上运行后卖了多少（{uuid.uuid4().hex[:8]}）"
START, END = "2026-09-01", "2026-09-08"
SQL = (f"SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '{START}' "
       f"AND paid_at < '{END}'")


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


async def ask(monkeypatch) -> Run:
    """问数据的一轮：建对话和轮次、跑一次探索运行、等比对做完。"""
    async with SessionLocal() as session:
        conv = Conversation(title="问销售", kind="chat")
        session.add(conv)
        await session.commit()
        turn = ConversationTurn(conversation_id=conv.id, question=QUESTION)
        session.add(turn)
        await session.commit()
        turn_id = turn.id
    S.script(monkeypatch, "这一周销售额 [[v:Q1.r0.gmv]]。")
    run = await run_manager.start(graph=S.explore_graph(SQL), input_payload={"question": QUESTION},
                                  origin_kind="chat", origin={"conversation_id": conv.id, "turn_id": turn_id})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        (await session.get(ConversationTurn, turn_id)).run_id = row.id
        await session.commit()
    await ledger_match.drain()
    return row


async def document() -> tuple[str, str]:
    """一份业务文档和其中的一段原文（口径定义的事实来源）。"""
    async with SessionLocal() as session:
        doc = Document(title="销售口径说明", content="销售额按已支付订单统计，退款订单不计入。")
        session.add(doc)
        await session.flush()
        chunk = Chunk(document_id=doc.id, ordinal=3, content="销售额按已支付订单统计，退款订单不计入。")
        session.add(chunk)
        await session.commit()
        return doc.id, chunk.id


def payload(document_id: str, chunk_id: str, *, name: str = "gmv") -> dict:
    citation = {"chunk": chunk_id, "document_id": document_id, "quote": "销售额按已支付订单统计"}
    return {"question": QUESTION, "metrics": [{
        "name": name, "synonyms": ["销售额"], "unit": "元",
        "definition": {"text": "按已支付订单统计，退款不计入", "citations": [citation, dict(citation)]}}]}


async def test_attach_merges_the_definition_into_the_run_derived_draft(monkeypatch):
    run = await ask(monkeypatch)
    doc_id, chunk_id = await document()
    async with SessionLocal() as session:
        made = await docattach.attach(session, payload=payload(doc_id, chunk_id), name="周销售口径",
                                      run_id=run.id, draft_id="draft-1")
    assert made["version"] == 1 and made["warnings"] == []
    [metric] = made["metrics"]
    assert metric["id"] == "m1" and metric["expression"] == "cell(nodes.fetch, 0, 'gmv')"
    assert metric["definition"]["text"] == "按已支付订单统计，退款不计入"

    async with SessionLocal() as session:
        wf = await session.get(Workflow, made["workflow_id"])
        version = (await session.execute(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf.id))).scalar_one()
    assert wf.status == "draft" and wf.tags == ["doc_draft"] and wf.published_version is None
    assert version.version == 1 and version.graph_hash == graph_hash(wf.graph)
    assert version.note == f"从文档草稿接上运行 {run.id}"
    card = next(n for n in wf.graph["nodes"] if n["type"] == "metrics")["data"]["config"]
    [card_metric] = card["metrics"]
    assert card_metric["id"] == "m1" and card_metric["expression"] == "cell(nodes.fetch, 0, 'gmv')", \
        "草稿只并定义，不改取数路径"
    assert card_metric["definition"]["text"] == "按已支付订单统计，退款不计入"
    assert card["caliber"] == "周销售口径"
    # 取数路径照抄运行：字面量都是常量、没有入口参数，SQL 原样
    fetch = next(n for n in wf.graph["nodes"] if n["type"] == "tool")
    assert fetch["data"]["config"]["args"]["sql"] == SQL and fetch["data"]["config"]["tool"] == S.tool()

    # origin：来源运行、草稿 id、引用的文档段落（两条一样的引用去重，ordinal 从库里补）
    assert wf.origin["run_id"] == run.id and wf.origin["freeze"] is True
    assert wf.origin["sources"] == [{"run_id": run.id}] and wf.origin["draft_id"] == "draft-1"
    assert wf.origin["documents"] == [{"document_id": doc_id, "chunk_id": chunk_id, "ordinal": 3,
                                       "quote": "销售额按已支付订单统计"}]
    assert "1 个指标" in wf.description and run.id in wf.description


async def test_attach_refuses_runs_that_are_missing_failed_or_not_matched(monkeypatch):
    async with SessionLocal() as session:
        session.add(Run(id="failed-run", status="failed", graph={}, input={}, output={}))
        session.add(Run(id="unmatched-run", status="succeeded", graph={}, input={}, output={}))
        await session.commit()
        for run_id, message in (("nope", docattach.NOT_FOUND),
                                ("failed-run", docattach.NOT_SUCCEEDED),
                                ("unmatched-run", docattach.NOT_MATCHED)):
            with pytest.raises(docattach.AttachRefused) as caught:
                await docattach.attach(session, payload={}, name="周销售口径", run_id=run_id)
            assert caught.value.status == 409 and caught.value.message == message

    # 成功、也比对过，但报告没引用任何查询：没有可接上的取数
    row = await S.explore(monkeypatch, S.ELSEWHERE, text="一共几家店，没有引用。")
    async with SessionLocal() as session:
        with pytest.raises(docattach.AttachRefused) as caught:
            await docattach.attach(session, payload={}, name="周销售口径", run_id=row.id)
    assert caught.value.status == 409 and caught.value.message == docattach.NO_QUERIES


async def test_a_metric_name_that_does_not_meet_the_card_goes_to_warnings(monkeypatch):
    run = await ask(monkeypatch)
    doc_id, chunk_id = await document()
    async with SessionLocal() as session:
        made = await docattach.attach(session, payload=payload(doc_id, chunk_id, name="毛利率"),
                                      name="毛利口径", run_id=run.id)
    assert len(made["warnings"]) == 1 and "毛利率" in made["warnings"][0]
    [metric] = made["metrics"]
    assert metric["id"] == "m1" and "definition" not in metric, "对不上的定义不往指标上写"
    async with SessionLocal() as session:
        wf = await session.get(Workflow, made["workflow_id"])
    assert wf is not None and wf.status == "draft"
