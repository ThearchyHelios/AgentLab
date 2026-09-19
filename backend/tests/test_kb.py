"""知识库：切块、混合检索、换 embedder 之后还能不能用。

这三个模块（kb.py / embeddings.py / memory/store.py，共 526 行检索逻辑）
在此之前**一条测试都没有**。于是有两件事一直没人发现：

- 换 embedding 模型会让检索整体 500：库里是 512 维的本地向量、查询是 1536 维
  的远端向量，cosine 直接 `shapes (1536,) and (512,) not aligned`
- 存量向量属于哪个模型，代码里没有任何地方记着，所以也无从降级

现在这两件事都有守的了。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.db.base import SessionLocal
from app.db.models import Chunk
from app.memory import embeddings as emb
from app.memory import kb
from app.memory.embeddings import hybrid_rank, to_blob


@pytest.fixture(autouse=True)
def _local_embedder():
    """每个用例都从本地 embedder 起步，别让上一个用例的 configure 漏过来。"""
    emb.configure("local")
    yield
    emb.configure("local")


# --------------------------------------------------------------------------
# 切块
# --------------------------------------------------------------------------


def test_short_paragraphs_stay_whole() -> None:
    blocks = kb.chunk_text("第一段。\n\n第二段。\n\n第三段。")
    assert blocks and all("第" in b for b in blocks)


def test_a_long_paragraph_is_cut_on_sentence_boundaries() -> None:
    """命中之后读起来要是完整的句子，不能拦腰截断。"""
    text = "。".join(f"这是第{i}句话，讲了一点内容" for i in range(120)) + "。"
    blocks = kb.chunk_text(text, target=200, overlap=0)
    assert len(blocks) > 1
    # 除了最后一块，其余都该收在句号上
    assert all(b.rstrip().endswith("。") for b in blocks[:-1])


def test_a_sentence_longer_than_the_target_is_hard_cut() -> None:
    """没有标点可切时也不能无限膨胀，硬切是最后的兜底。"""
    blocks = kb.chunk_text("啊" * 2000, target=300, overlap=0)
    assert len(blocks) > 1 and max(len(b) for b in blocks) <= 300


def test_empty_input_yields_nothing() -> None:
    assert kb.chunk_text("") == [] and kb.chunk_text("   \n\n  ") == []


# --------------------------------------------------------------------------
# hybrid_rank 的归一化边界
# --------------------------------------------------------------------------


def test_a_single_candidate_is_not_scored_zero() -> None:
    """min-max 归一化在只有一条候选时 hi==lo。

    返回 0 的话它的合并分恒为 0，会被 min_score 过滤掉——于是新建集合写入
    第一条之后永远搜不到，而"先写一条、下一轮再检索"正是 agent 最常见的用法。
    embeddings.py 里为此写了一段注释，但一直没有测试守着。
    """
    vec = np.ones(4, dtype=np.float32)
    ranked = hybrid_rank("查询", ["只有这一条"], [vec], vec)
    assert len(ranked) == 1 and ranked[0][1] > 0


# --------------------------------------------------------------------------
# 换 embedder：以前这里直接 500
# --------------------------------------------------------------------------


async def _seed(collection: str, texts: list[str]) -> None:
    async with SessionLocal() as session:
        for t in texts:
            await kb.ingest_document(
                session, collection=collection, title=t[:10], content=t)


def test_stale_vectors_are_recognised() -> None:
    """存量数据那两列是空的，一律当成对不上——保守但不会拿旧坐标冒充新空间。"""
    assert emb.usable("", 0) is False
    assert emb.usable("openai:text-embedding-3-small", 1536) is False
    assert emb.usable(emb.embedder_id(), emb.embedder_dim()) is True


async def test_ingest_records_who_built_the_vector() -> None:
    await _seed("kbtest_mark", ["平台管理员一共有六个人。"])
    async with SessionLocal() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(Chunk).where(Chunk.collection == "kbtest_mark"))).scalars().first()
    assert row.embed_model == emb.embedder_id()
    assert row.embed_dim == emb.embedder_dim()


async def test_search_degrades_instead_of_crashing_after_a_model_switch() -> None:
    """整件事的核心。

    以前：cosine(1536 维查询, 512 维存量) → ValueError → 检索 500。
    现在：整体退回纯关键词，并把原因说出来。
    """
    await _seed("kbtest_switch", ["平台管理员一共有六个人。", "商家账户共五个。"])

    # 把库里的向量伪装成别的模型建的（维度也不同）
    async with SessionLocal() as session:
        from sqlalchemy import select
        rows = list((await session.execute(
            select(Chunk).where(Chunk.collection == "kbtest_switch"))).scalars())
        for r in rows:
            r.embedding = to_blob(np.ones(1536, dtype=np.float32))
            r.embed_model = "openai:text-embedding-3-small"
            r.embed_dim = 1536
        await session.commit()

    notes: list[str] = []
    async with SessionLocal() as session:
        hits = await kb.search(session, collection="kbtest_switch", query="管理员",
                               limit=5, on_degrade=notes.append)

    assert hits, "退回关键词也要能搜到东西，而不是空手而归"
    assert notes, "少了一半能力却不吭声，用户只会觉得最近搜得不准"
    assert "重建索引" in notes[0] and "2/2" in notes[0]


async def test_reindex_restores_hybrid_search() -> None:
    await _seed("kbtest_reindex", ["平台管理员一共有六个人。"])
    async with SessionLocal() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(Chunk).where(Chunk.collection == "kbtest_reindex"))).scalars().first()
        row.embed_model, row.embed_dim = "别的模型", 99
        await session.commit()

    async with SessionLocal() as session:
        assert await kb.stale_count(session, "kbtest_reindex") == 1
        out = await kb.reindex(session, "kbtest_reindex")
        assert out["reindexed"] == 1
        assert await kb.stale_count(session, "kbtest_reindex") == 0

    notes: list[str] = []
    async with SessionLocal() as session:
        hits = await kb.search(session, collection="kbtest_reindex", query="管理员",
                               on_degrade=notes.append)
    assert hits and not notes, "重建之后不该再降级"


# --------------------------------------------------------------------------
# 配置 embedder
# --------------------------------------------------------------------------


def test_choosing_a_remote_embedder_that_cannot_be_built_says_so(monkeypatch) -> None:
    """选了远端却静默退回本地哈希向量，用户会以为自己在用语义检索。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(emb.EmbedderUnavailable) as e:
        emb.configure("openai", "text-embedding-3-small")
    assert "OPENAI_API_KEY" in str(e.value)


def test_the_local_embedder_is_always_available() -> None:
    emb.configure("local")
    assert emb.embedder_id() == "local-hashing" and emb.embedder_dim() == 512
