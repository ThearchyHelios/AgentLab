"""业务文档 → 口径草稿 → 接上一次真实运行 → 能过图校验 → 对问数/编排不可见（整条链）。

只走公开入口：docdraft.extract 起草（假模型）、docattach.attach 接上运行；取数路径来自封存的查询快照与比对工件，
和 test_caliber_docdraft_attach 用同一套夹具。盯住几件不变量：

- 起草的每条指标都带引用 span，引用核不上就进 dropped；
- 接上之后生成的工作流版本是合法图（GraphSpec + validate_graph 无 error）；
- 文档只贡献定义：expression / id 与 certify.build_graph 直接产出的那份逐字相同；
- origin.documents 的 quote 逐字来自 chunk，definition.citations 的 span 落在原文范围内；
- 没发布的草稿不进 descriptor.list_calibers（问数、编排按已发布口径认它，草稿不该被当成口径）；
- 接入不写 caliber.use 审计（只落草稿，不发布、不发起运行）。

发布之后草稿出现在 list_calibers 里的断言没有覆盖：发布是另一个动作，见 ledger 发布接口的测试。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.artifact_store import load
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Chunk, Conversation, ConversationTurn, DataSource, Document, Run, Workflow
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.ledger import certify, descriptor, docattach, docdraft
from app.ledger import match as ledger_match
from app.ledger import store
from tests import ledger_samples as S

#: 每个用例一句话的问题：问句进全站唯一的聚类，和别的用例用同一句会在 certify 的队列里互相看见
QUESTION = f"文档起草的销售口径接上运行（{uuid.uuid4().hex[:8]}）"
NAME = "周销售口径"
START, END = "2026-09-01", "2026-09-08"
SQL = (f"SELECT SUM(amount) AS gmv, COUNT(*) AS order_count FROM orders WHERE status = 'paid' "
       f"AND paid_at >= '{START}' AND paid_at < '{END}'")

CHUNK_ONE = "销售额按已支付订单的支付时间归期统计，退款订单不计入。"
CHUNK_TWO = "订单数按已支付订单计数，一单记一次，不含退款与未支付订单。"


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


class FakeDraftModel:
    """起草模型：ainvoke 返回带 .content 的对象（docdraft.extract 只读这两个属性）。"""

    model_name = "fake-draft-model"

    def __init__(self, text: str) -> None:
        self.text = text

    async def ainvoke(self, _messages):
        return SimpleNamespace(content=self.text)


async def document() -> tuple[str, dict[str, dict], list[str]]:
    """一份业务文档、两段原文：分别是 gmv 和 order_count 的事实来源。"""
    async with SessionLocal() as session:
        doc = Document(title="销售口径说明", content=f"{CHUNK_ONE}\n{CHUNK_TWO}")
        session.add(doc)
        await session.flush()
        session.add_all([Chunk(document_id=doc.id, ordinal=2, content=CHUNK_ONE),
                         Chunk(document_id=doc.id, ordinal=5, content=CHUNK_TWO)])
        await session.commit()
        rows = (await session.execute(select(Chunk).where(Chunk.document_id == doc.id)
                                      .order_by(Chunk.ordinal))).scalars().all()
    chunks = {c.id: {"ordinal": c.ordinal, "content": c.content, "document_id": doc.id} for c in rows}
    return doc.id, chunks, [c.id for c in rows]


def reply(chunk_ids: list[str]) -> str:
    """模型回复：两条指标，引用逐字抄原文，chunk 写方括号里的 id。"""
    return json.dumps({"metrics": [
        {"name": "gmv", "synonyms": ["销售额"], "unit": "元",
         "definition": {"text": "按已支付订单的支付时间统计，退款不计入",
                        "citations": [{"chunk": chunk_ids[0], "quote": "销售额按已支付订单的支付时间归期统计"}]}},
        {"name": "order_count", "synonyms": ["订单数"], "unit": "单",
         "definition": {"text": "按已支付订单计数，不含退款与未支付",
                        "citations": [{"chunk": chunk_ids[1], "quote": "订单数按已支付订单计数，一单记一次"}]}},
    ], "open_questions": ["退款订单是否计入销售额？"]}, ensure_ascii=False)


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
    S.script(monkeypatch, "这一周销售额 [[v:Q1.r0.gmv]]，订单数 [[v:Q1.r0.order_count]]。")
    run = await run_manager.start(graph=S.explore_graph(SQL), input_payload={"question": QUESTION},
                                  origin_kind="chat", origin={"conversation_id": conv.id, "turn_id": turn_id})
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        (await session.get(ConversationTurn, turn_id)).run_id = row.id
        await session.commit()
    await ledger_match.drain()
    return row


async def shapes_of(run_id: str) -> tuple[list[dict], list[certify.Shape], dict[str, str | None]]:
    """运行封存的查询快照 → (快照, 形状, 数据源种类)，读法和 docattach.attach 一样。"""
    recorded = await store.latest(run_id)
    assert recorded is not None and recorded.content is not None
    queries = certify.referenced_queries(recorded.content)
    assert queries, "报告没有引用任何查询"
    async with SessionLocal() as session:
        kinds = {n: k for n, k in (await session.execute(select(DataSource.name, DataSource.kind))).all()}
    snaps, shapes = [], []
    for artifact in queries:
        snap = load(artifact)
        shape = certify.shape_of(snap, kinds)
        assert shape is not None and shape.problem is None
        snaps.append(snap)
        shapes.append(shape)
    return snaps, shapes, kinds


async def test_document_draft_attaches_to_a_sealed_run_and_stays_out_of_the_catalog(monkeypatch):
    # ---- 1. 文档 → 草稿：两条指标都带 span，没有丢弃 ----
    doc_id, chunks, chunk_ids = await document()
    model = FakeDraftModel(reply(chunk_ids))
    docs = [{"document_id": doc_id, "title": "销售口径说明", "chunk_ids": chunk_ids}]
    result = await docdraft.extract(None, docs=docs, chunks=chunks, model=model)
    assert result["model_id"] == "fake-draft-model" and result["truncated"] is False
    assert result["dropped"] == []
    assert [m["name"] for m in result["metrics"]] == ["gmv", "order_count"]
    for metric, chunk_id in zip(result["metrics"], chunk_ids):
        [citation] = metric["definition"]["citations"]
        assert citation["chunk"] == chunk_id and citation["document_id"] == doc_id
        start, end = citation["span"]
        content = chunks[chunk_id]["content"]
        assert 0 <= start < end <= len(content)
        assert content[start:end] == citation["quote"], "span 指向的就是那一段原文"
    # 同一份回复再过一遍 normalize_metrics，结果一致（extract 内部用的就是它）
    normalized, dropped = docdraft.normalize_metrics(json.loads(reply(chunk_ids)), chunks)
    assert dropped == [] and normalized == result["metrics"]

    # ---- 2. 一次成功运行 + 封存快照/比对工件 → 接上草稿 ----
    run = await ask(monkeypatch)
    snaps, shapes, kinds = await shapes_of(run.id)
    async with SessionLocal() as session:
        before, _fields, before_metrics, _multi = await certify.build_graph(
            shapes=shapes, snaps=snaps, kinds=kinds,
            labels=lambda snap, source: certify._labels(session, snap, source),
            name=NAME, question=QUESTION)
    assert [m["id"] for m in before_metrics] == ["m1", "m2"]

    payload = {"question": QUESTION, "metrics": result["metrics"]}
    async with SessionLocal() as session:
        made = await docattach.attach(session, payload=payload, name=NAME, run_id=run.id, draft_id="draft-e2e")
    assert made["version"] == 1 and made["warnings"] == []
    assert [m["id"] for m in made["metrics"]] == ["m1", "m2"]

    async with SessionLocal() as session:
        wf = await session.get(Workflow, made["workflow_id"])
        assert wf is not None and wf.status == "draft" and wf.published_version is None
        graph = wf.graph

    # 图能过静态校验（ValidationResult 的字段是 ok / issues，error 级别的 issue 为空）
    spec = GraphSpec.model_validate(graph)
    checked = validate_graph(spec)
    errors = [issue for issue in checked.issues if issue.level == "error"]
    assert checked.ok and errors == [], errors

    # 文档只并定义：口径卡指标带 definition，expression / id 与接之前逐字相同
    card = next(n for n in graph["nodes"] if n["type"] == "metrics")["data"]["config"]
    assert card["caliber"] == NAME
    for attached, original in zip(card["metrics"], before_metrics):
        assert attached["id"] == original["id"]
        assert attached["expression"] == original["expression"], "接入不改取数路径"
        assert attached["definition"]["text"]
        assert attached["definition"]["citations"]

    # origin.documents：引用去重、quote 逐字来自 chunk，ordinal 从库里补
    documents = wf.origin["documents"]
    assert {d["chunk_id"] for d in documents} == set(chunk_ids)
    for item in documents:
        chunk = chunks[item["chunk_id"]]
        assert item["document_id"] == doc_id and item["ordinal"] == chunk["ordinal"]
        assert item["quote"] in chunk["content"], "quote 必须在原文里逐字命中"

    # definition.citations 的 span 在 chunk 内容范围内，且指向 quote 本身
    for metric in card["metrics"]:
        for citation in metric["definition"]["citations"]:
            content = chunks[citation["chunk"]]["content"]
            start, end = citation["span"]
            assert 0 <= start < end <= len(content)
            assert content[start:end] == citation["quote"]

    # ---- 3. 可见性：草稿工作流不进 list_calibers（没发布就不是口径） ----
    # 先发布一份无关的口径：list_calibers 非空，下面排除草稿才不是空转
    published = await S.publish("周销售（已发布）")
    async with SessionLocal() as session:
        listed = await descriptor.list_calibers(session)
        ids = {d["workflow_id"] for d in listed}
        assert published in ids
        assert made["workflow_id"] not in ids
        assert await descriptor.list_calibers(session, workflow_id=made["workflow_id"]) == []

    # ---- 4. 接入不写 caliber.use 审计 ----
    async with SessionLocal() as session:
        events = (await session.execute(select(AuditEvent).where(
            AuditEvent.action == "caliber.use",
            AuditEvent.subject_id.like(f"{made['workflow_id']}@%")))).scalars().all()
    assert events == [], "接上草稿只是落一份可试跑的草稿，不算一次口径调用"
