"""知识库调试台看得见每条命中的分从哪来；重建索引有真实进度。

总分是向量、关键词两路各自在候选集上归一化之后按 α 加权的和。前端拿到的只有
两路的原始分（余弦、BM25），复现不了那个归一化，于是命中只能画一根总分条，
说不清「这条是靠语义排上来的还是靠关键词」。contrib 把两路的贡献直接交出去，
两者之和就是总分。

重建索引以前压在一个请求里：几千段配远端 embedding 要好几分钟，界面只能转圈
加一个已用时间。改成后台任务之后按批报「总数 / 已完成」，界面才画得出真进度。
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Chunk, MemoryItem
from app.main import app
from app.memory import embeddings as emb
from app.memory import kb, store


@pytest.fixture(autouse=True)
def _local_embedder():
    emb.configure("local")
    yield
    emb.configure("local")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


DOCS = [
    "平台管理员一共有六个人，负责审批权限。",
    "商家账户共五个，按地区划分。",
    "订单表 orders 记录每一笔订单的金额和门店。",
    "每日销售汇总在 sales_daily 表里，按门店和日期聚合。",
]


async def _seed(collection: str) -> None:
    async with SessionLocal() as session:
        for text in DOCS:
            await kb.ingest_document(session, collection=collection, title=text[:8], content=text)


# --------------------------------------------------------------------------
# 命中的分数贡献
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alpha", [0.0, 0.3, 0.7, 1.0])
async def test_each_hit_says_how_much_each_signal_contributed(client, alpha):
    await _seed(f"contrib_{int(alpha * 10)}")
    r = await client.get("/api/kb/search", params={
        "q": "订单 门店", "collection": f"contrib_{int(alpha * 10)}", "limit": 4, "alpha": alpha,
    })
    hits = r.json()["results"]
    assert hits
    for hit in hits:
        contrib = hit["contrib"]
        assert set(contrib) == {"vector", "keyword"}
        assert abs(contrib["vector"] + contrib["keyword"] - hit["score"]) < 1e-6, hit
        assert 0 <= contrib["vector"] <= alpha + 1e-9
        assert 0 <= contrib["keyword"] <= 1 - alpha + 1e-9
    if alpha == 0.0:
        assert all(h["contrib"]["vector"] == 0 for h in hits)
    if alpha == 1.0:
        assert all(h["contrib"]["keyword"] == 0 for h in hits)
    # 原始信号照旧给：调试台上还要写余弦和 BM25 的原数
    assert set(hits[0]["signals"]) == {"vector", "keyword"}


async def test_the_contributions_come_from_the_same_normalisation_as_the_score():
    from app.memory.embeddings import hybrid_contrib, hybrid_rank

    texts = ["订单金额", "门店数量", "订单和门店"]
    vectors = [await emb.embed_text(t) for t in texts]
    query = await emb.embed_text("订单")
    ranked = hybrid_rank("订单", texts, vectors, query, alpha=0.4)
    detailed = hybrid_contrib("订单", texts, vectors, query, alpha=0.4)
    assert [(i, s) for i, s, _ in ranked] == [(r.index, r.score) for r in detailed]
    for r in detailed:
        assert abs(r.contrib["vector"] + r.contrib["keyword"] - r.score) < 1e-12


async def test_a_degraded_search_puts_everything_on_keywords(client):
    await _seed("contrib_stale")
    async with SessionLocal() as session:
        for row in (await session.execute(
                select(Chunk).where(Chunk.collection == "contrib_stale"))).scalars():
            row.embed_model, row.embed_dim = "旧模型", 7
        await session.commit()
    body = (await client.get("/api/kb/search", params={
        "q": "订单", "collection": "contrib_stale", "alpha": 0.7})).json()
    assert body["degraded"]
    assert all(h["contrib"]["vector"] == 0 for h in body["results"])


# --------------------------------------------------------------------------
# 重建索引：后台跑，按批报进度
# --------------------------------------------------------------------------


async def test_reindex_reports_progress_batch_by_batch(monkeypatch):
    await _seed("progress_kb")
    monkeypatch.setattr(kb, "_EMBED_BATCH", 2)
    seen: list[tuple[int, int]] = []
    async with SessionLocal() as session:
        out = await kb.reindex(session, "progress_kb", on_progress=lambda d, t: seen.append((d, t)))
    assert out["reindexed"] == len(DOCS)
    assert seen == [(2, 4), (4, 4)]


async def test_memory_reindex_reports_progress_too(monkeypatch):
    async with SessionLocal() as session:
        for i in range(3):
            await store.remember(session, scope="progress_mem", content=f"第 {i} 条要记住的口径",
                                 dedupe=False)
    monkeypatch.setattr(kb, "_EMBED_BATCH", 2)
    seen: list[tuple[int, int]] = []
    async with SessionLocal() as session:
        n = await store.reindex(session, "progress_mem", on_progress=lambda d, t: seen.append((d, t)))
    assert n == 3 and seen == [(2, 3), (3, 3)]


async def _wait_done(client, timeout: float = 10.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        job = (await client.get("/api/kb/reindex")).json()
        if job["state"] != "running" or loop.time() > deadline:
            return job
        await asyncio.sleep(0.02)


async def test_a_background_reindex_returns_at_once_and_can_be_polled(client):
    await _seed("progress_bg")
    async with SessionLocal() as session:
        for row in (await session.execute(
                select(Chunk).where(Chunk.collection == "progress_bg"))).scalars():
            row.embed_model, row.embed_dim = "旧模型", 7
        await session.commit()

    r = await client.post("/api/kb/reindex", params={"collection": "progress_bg", "background": "true"})
    assert r.status_code == 202, r.text
    started = r.json()
    assert started["state"] in ("running", "done")
    assert started["collection"] == "progress_bg"
    memories = await _count_memories()
    assert started["chunks"]["total"] == len(DOCS), "总数要在开始时就知道，进度条才有分母"
    assert started["total"] == len(DOCS) + memories

    job = await _wait_done(client)
    assert job["state"] == "done", job
    assert job["id"] == started["id"]
    assert job["done"] == job["total"]
    assert job["chunks"] == {"total": len(DOCS), "done": len(DOCS)}
    assert job["result"]["reindexed"] == len(DOCS)
    assert job["result"]["memories_reindexed"] == memories
    assert job["finished_at"]
    async with SessionLocal() as session:
        assert await kb.stale_count(session, "progress_bg") == 0


async def test_a_second_reindex_while_one_is_running_is_refused(client, monkeypatch):
    from app.api import knowledge

    gate = asyncio.Event()
    real = kb.reindex

    async def slow(session, collection=None, *, on_progress=None):
        await gate.wait()
        return await real(session, collection, on_progress=on_progress)

    monkeypatch.setattr(kb, "reindex", slow)
    try:
        # 后台跑的话这一步立刻返回；压在请求里跑的话会一直等着 gate
        first = await asyncio.wait_for(
            client.post("/api/kb/reindex", params={"background": "true"}), timeout=5)
        assert first.status_code == 202
        again = await client.post("/api/kb/reindex", params={"background": "true"})
        assert again.status_code == 409
        assert "重建" in again.json()["detail"]
        sync = await asyncio.wait_for(client.post("/api/kb/reindex"), timeout=5)
        assert sync.status_code == 409, "同步那条路也不能和后台的并发跑"
    finally:
        gate.set()
    job = await _wait_done(client)
    assert job["state"] == "done"
    assert knowledge.reindex_job() is not None


async def test_two_reindexes_started_at_the_same_moment_do_not_both_run(client, monkeypatch):
    """连点两下、两个标签页同时点：只能起一个。

    先查「有没有在跑」、再 await 两次计数、最后才记下任务，中间的 await 让第二个
    请求也看到「没在跑」——两个都 202，embedding 被重复计费，第一个的进度被覆盖。
    """
    gate = asyncio.Event()
    real = kb.reindex
    started: list[str | None] = []

    async def slow(session, collection=None, *, on_progress=None):
        started.append(collection)
        await gate.wait()
        return await real(session, collection, on_progress=on_progress)

    monkeypatch.setattr(kb, "reindex", slow)
    try:
        replies = await asyncio.wait_for(asyncio.gather(*(
            client.post("/api/kb/reindex", params={"background": "true"}) for _ in range(3)
        )), timeout=5)
        codes = sorted(r.status_code for r in replies)
        assert codes == [202, 409, 409], [(r.status_code, r.text) for r in replies]
        await asyncio.sleep(0.05)
        assert len(started) == 1, "只能真的起一个重建"
    finally:
        gate.set()
    job = await _wait_done(client)
    assert job["state"] == "done"
    accepted = next(r for r in replies if r.status_code == 202).json()
    assert job["id"] == accepted["id"], "进度查到的得是被接下来的那一个"


async def test_a_failed_reindex_says_why(client, monkeypatch):
    async def broken(session, collection=None, *, on_progress=None):
        raise RuntimeError("Connection refused")

    monkeypatch.setattr(kb, "reindex", broken)
    r = await client.post("/api/kb/reindex", params={"background": "true"})
    assert r.status_code == 202
    job = await _wait_done(client)
    assert job["state"] == "failed"
    assert job["error"] and "RuntimeError" not in job["error"]


async def test_before_any_reindex_the_status_is_idle(client, monkeypatch):
    from app.api import knowledge

    monkeypatch.setattr(knowledge, "_REINDEX", None)
    job = (await client.get("/api/kb/reindex")).json()
    assert job == {"state": "idle"}


async def _count_memories() -> int:
    from sqlalchemy import func

    async with SessionLocal() as session:
        return int((await session.execute(select(func.count(MemoryItem.id)))).scalar_one())
