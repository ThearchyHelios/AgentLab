"""系统派生的运行不进使用统计（docs/design/next-round/00-总方案.md X1，细案 01 做法 4）。

评测、口径对账、口径召回（engine/origin.SYSTEM_ORIGINS）是服务端替人跑的完整运行，证据点得开，但不是有人在用。这里守：

- 客户端声明不了这几种来源；ledger_issue（替用户正式出具）照常计入；
- counted_runs() 只去掉这三种，来源为空的老运行照常计入（防 NULL）；
- 探索问题聚类只取问题原文（不含 history），问数据一轮一条（自动重来不多算），评测运行不进；
- 工具白名单衰减、目录的使用次数和「被哪些运行用过」、审阅队列的使用权重都不计评测运行；
- 记录页默认不列这三种，按来源点名就列；GET /api/runs/unlisted 照实说没列出几次。

表名、问题、工作流名都是虚构的。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.data import catalog, catalog_review
from app.db.base import SessionLocal
from app.db.models import Artifact, Conversation, ConversationTurn, DataSource, Run, RunEvent, Workflow
from app.engine.origin import CLIENT_ORIGINS, ORIGIN_KINDS, SYSTEM_ORIGINS, counted_runs
from app.main import app
from tests.fixtures.sources import drop_source

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)
MARK = f"计入统计-{uuid.uuid4().hex[:6]}"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean():
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Run).where(Run.workflow_name.like(f"{MARK}%")))
        await session.execute(delete(Conversation).where(Conversation.title.like(f"{MARK}%")))
        await session.commit()


async def _run(origin: str | None, *, minutes: int = 0, **extra) -> str:
    rid = uuid.uuid4().hex
    async with SessionLocal() as session:
        session.add(Run(id=rid, graph={}, workflow_name=f"{MARK}-{origin}", run_class="exploratory", status="succeeded",
                        origin_kind=origin, created_at=BASE + timedelta(minutes=minutes), **extra))
        await session.commit()
    return rid


def test_system_origins_are_server_only_and_ledger_issue_counts():
    assert set(SYSTEM_ORIGINS) == {"eval", "reconcile", "recall"}
    assert set(SYSTEM_ORIGINS) <= set(ORIGIN_KINDS)
    assert not set(SYSTEM_ORIGINS) & set(CLIENT_ORIGINS)
    assert "ledger_issue" in ORIGIN_KINDS and "ledger_issue" not in SYSTEM_ORIGINS, "替用户正式出具照常计入"


async def test_a_client_cannot_claim_a_system_origin(client):
    for origin in (*SYSTEM_ORIGINS, "ledger_issue"):
        r = await client.post("/api/runs", json={"graph": {"nodes": [], "edges": []}, "origin": origin})
        assert r.status_code == 422 and r.json()["code"] == "origin_not_allowed", (origin, r.text)


async def test_counted_runs_keeps_legacy_null_origins():
    ids = {origin: await _run(origin) for origin in (None, "chat", "ledger_issue", *SYSTEM_ORIGINS)}
    async with SessionLocal() as session:
        counted = set((await session.execute(
            select(Run.id).where(Run.id.in_(list(ids.values())), counted_runs()))).scalars())
    assert counted == {ids[None], ids["chat"], ids["ledger_issue"]}


# --------------------------------------------------------------------------
# 探索问题聚类
# --------------------------------------------------------------------------


async def _turn(conversation_id: str, question: str, run_id: str | None, seq: int, minutes: int) -> str:
    async with SessionLocal() as session:
        turn = ConversationTurn(conversation_id=conversation_id, seq=seq, question=question, run_id=run_id,
                                status="done", created_at=BASE + timedelta(minutes=minutes))
        session.add(turn)
        await session.commit()
        return turn.id


async def _conversation() -> str:
    async with SessionLocal() as session:
        row = Conversation(title=f"{MARK}-对话", kind="chat")
        session.add(row)
        await session.commit()
        return row.id


async def test_clusters_take_only_the_question_once_per_turn_and_skip_eval(client, monkeypatch):
    from app.memory import embeddings

    question = f"{MARK} 上周各门店的销售额"

    async def by_text(texts):
        # 同一句话同一个向量，别的话各不相同：聚类只看取了什么文字
        return [[1.0, 0.0] if t == question else [0.0, float(i + 1)] for i, t in enumerate(texts)]

    monkeypatch.setattr(embeddings, "embed_texts", by_text)
    conv = await _conversation()
    # 第一轮：答坏了自动重来一次——两次运行，轮次只挂着后一次；输入里的 history 各不相同也不影响
    first = await _run("chat", minutes=1, input={"question": question, "history": "问：别的\n答：别的"})
    retried = await _run("chat", minutes=2, input={"question": question, "history": "问：别的\n答：别的"})
    turn_a = await _turn(conv, question, retried, 1, 2)
    # 第二轮：同一个问题又问了一次，这次只回了一句、没有运行
    turn_b = await _turn(conv, question, None, 2, 3)
    # 评测作答的运行：同一个问题，不进聚类
    graded = await _run("eval", minutes=4, input={"question": question})
    # 画布试跑：输入里有 question 的照样收
    canvas = await _run("canvas", minutes=5, input={"question": question})

    body = (await client.get("/api/governance/exploratory-clusters", params={"limit": 500})).json()
    mine = [c for c in body["clusters"] if c["representative"] == question]
    assert len(mine) == 1, body
    cluster = mine[0]
    assert cluster["count"] == 3, "一轮一条（自动重来不多算）+ 画布那一次；评测不算"
    assert set(cluster["turn_ids"]) == {turn_a, turn_b}
    assert set(cluster["run_ids"]) == {retried, canvas}
    assert first not in cluster["run_ids"] and graded not in cluster["run_ids"]
    assert cluster["promote_candidate"] is True
    assert body["embedder"]


async def test_the_history_never_reaches_the_clusterer(monkeypatch):
    from app.api.deps import current_principal
    from app.chat.questions import recent_questions

    conv = await _conversation()
    rid = await _run("chat", minutes=10, input={"question": f"{MARK} 本月退款", "history": "问：上周\n答：100"})
    await _turn(conv, f"{MARK} 本月退款", rid, 1, 10)
    async with SessionLocal() as session:
        got = [q for q in await recent_questions(session, current_principal(), limit=500) if MARK in q.text]
    assert [q.text for q in got] == [f"{MARK} 本月退款"]


# --------------------------------------------------------------------------
# 工具白名单衰减
# --------------------------------------------------------------------------


async def test_tool_usage_ignores_eval_runs(client):
    graph = {"nodes": [{"id": "a", "type": "agent", "position": {"x": 0, "y": 0},
                        "data": {"label": "a", "config": {"tools": ["calc", "kb_search"]}}}], "edges": []}
    async with SessionLocal() as session:
        wf = Workflow(name=f"{MARK}-白名单", graph=graph)
        session.add(wf)
        await session.commit()
        wf_id = wf.id
    try:
        used = await _run("canvas", workflow_id=wf_id)
        graded = await _run("eval", workflow_id=wf_id)
        async with SessionLocal() as session:
            session.add(RunEvent(run_id=used, seq=1, type="tool.start", node_id="a", data={"tool": "calc"}, ts=0.0))
            session.add(RunEvent(run_id=graded, seq=1, type="tool.start", node_id="a", data={"tool": "kb_search"}, ts=0.0))
            await session.commit()
        body = (await client.get("/api/governance/tool-usage", params={"workflow_id": wf_id})).json()
        assert body["runs_considered"] == 1
        node = body["nodes"][0]
        assert node["used"] == {"calc": 1} and node["unused"] == ["kb_search"], "评测用过的工具不能让白名单显得都在用"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Workflow).where(Workflow.id == wf_id))
            await session.commit()


# --------------------------------------------------------------------------
# 目录的使用次数、被哪些运行用过、审阅队列的权重
# --------------------------------------------------------------------------


async def test_catalog_usage_ignores_eval_runs():
    name = f"eo_{uuid.uuid4().hex[:8]}"
    tables = {"orders": {"qualified": "orders", "primary_key": ["id"], "comment": None, "is_view": False,
                         "foreign_keys": [], "unique": [],
                         "columns": [{"name": "id", "type": "INTEGER", "nullable": True, "comment": None}]}}
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=":memory:", schema_cache={"tables": tables})
        session.add(row)
        await session.commit()
        sid = row.id
    try:
        used = await _run("chat")
        graded = await _run("eval")
        legacy = await _run(None)
        async with SessionLocal() as session:
            for rid in (used, graded, legacy):
                session.add(Artifact(id=uuid.uuid4().hex, run_id=rid, kind=catalog.QUERY_SNAPSHOT_KIND,
                                     meta={"source_id": sid, "source": name, "tables": ["orders"]}))
            await session.commit()
            source = await session.get(DataSource, sid)
            assert await catalog.table_usage(session, source) == {"orders": 2}
            counts = await catalog_review.table_usage_counts(session, source)
            assert counts.queries == {"orders": 2} and counts.runs == {"orders": 2}
            listed = await catalog_review.table_runs(session, source, "orders")
            assert listed["total"] == 2 and {r["run_id"] for r in listed["runs"]} == {used, legacy}
    finally:
        await drop_source(sid)


# --------------------------------------------------------------------------
# 记录页
# --------------------------------------------------------------------------


async def test_the_run_list_hides_system_runs_by_default_and_says_how_many(client):
    chat = await _run("chat")
    graded = [await _run("eval", minutes=i) for i in range(2)]
    recon = await _run("reconcile")
    legacy = await _run(None)
    q = {"q": MARK, "limit": 200}

    listed = {r["id"] for r in (await client.get("/api/runs", params=q)).json()}
    assert {chat, legacy} <= listed and not (set(graded) | {recon}) & listed
    unlisted = (await client.get("/api/runs/unlisted", params={"q": MARK})).json()
    assert unlisted == {"total": 3, "by_origin": {"eval": 2, "reconcile": 1}}

    # 按来源点名就列，此时没有藏起来的
    only = {r["id"] for r in (await client.get("/api/runs", params={**q, "origin_kind": "eval"})).json()}
    assert only == set(graded)
    assert (await client.get("/api/runs/unlisted", params={"q": MARK, "origin_kind": "eval"})).json()["total"] == 0
    # 同样的筛选一起作用：按状态筛时只数这个状态的
    assert (await client.get("/api/runs/unlisted", params={"q": MARK, "status": "failed"})).json()["total"] == 0
