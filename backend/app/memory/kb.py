from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document
from app.memory import inverted
from app.memory.embeddings import (
    default_alpha, embed_text, embed_texts, embedder_dim, embedder_id, from_blob,
    hybrid_rank, to_blob, usable,
)

_TARGET = 800
_OVERLAP = 120


def chunk_text(text: str, target: int = _TARGET, overlap: int = _OVERLAP) -> list[str]:
    """按语义边界切块。

    先按空行分段，段落塞不下再按句子切，实在超长才硬切。
    这样切出来的块不会把一句话拦腰截断，检索命中后读起来是完整的。
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks: list[str] = []
    for para in paragraphs:
        if len(para) <= target:
            blocks.append(para)
            continue
        sentences = re.split(r"(?<=[。！？.!?\n])\s*", para)
        current = ""
        for sentence in sentences:
            if not sentence:
                continue
            if len(current) + len(sentence) <= target:
                current += sentence
            else:
                if current:
                    blocks.append(current)
                while len(sentence) > target:
                    blocks.append(sentence[:target])
                    sentence = sentence[target:]
                current = sentence
        if current:
            blocks.append(current)

    # 把相邻小块合并到接近目标长度，并留一点重叠保住上下文
    merged: list[str] = []
    buf = ""
    for block in blocks:
        if len(buf) + len(block) + 1 <= target:
            buf = f"{buf}\n{block}" if buf else block
        else:
            if buf:
                merged.append(buf)
                tail = buf[-overlap:] if overlap else ""
                buf = f"{tail}\n{block}" if tail else block
            else:
                merged.append(block)
                buf = ""
    if buf:
        merged.append(buf)
    return [m.strip() for m in merged if m.strip()]


async def ingest_document(
    session: AsyncSession,
    *,
    collection: str,
    title: str,
    content: str,
    source: str = "",
    mime: str = "text/plain",
    meta: dict[str, Any] | None = None,
) -> Document:
    doc = Document(
        collection=collection,
        title=title or source or "未命名文档",
        source=source,
        mime=mime,
        content=content,
        meta=meta or {},
    )
    session.add(doc)
    await session.flush()

    pieces = chunk_text(content)
    made: list[Chunk] = []
    if pieces:
        vectors = await embed_texts(pieces)
        for i, (piece, vec) in enumerate(zip(pieces, vectors)):
            chunk = Chunk(
                document_id=doc.id,
                collection=collection,
                ordinal=i,
                content=piece,
                embedding=to_blob(vec),
                embed_model=embedder_id(),
                embed_dim=embedder_dim(),
                meta={"title": doc.title},
            )
            session.add(chunk)
            made.append(chunk)
        # 先 flush 拿到 chunk.id，倒排行要引用它
        await session.flush()
        for chunk in made:
            await inverted.index_chunk(session, chunk)
    doc.chunk_count = len(pieces)
    await session.commit()
    await session.refresh(doc)
    return doc


async def search(
    session: AsyncSession,
    *,
    collection: str | None,
    query: str,
    limit: int = 5,
    alpha: float | None = None,
    on_degrade: Any = None,
) -> list[dict[str, Any]]:
    """混合检索。

    存量向量和当前 embedder 对不上时**整个查询退回纯关键词**，并通过
    on_degrade 把原因交出去。不把对不上的那些当 0 分：那会让它们静默沉底，
    用户看到的是结果莫名其妙变差，而不是"这里有一批索引该重建了"。

    在此之前这里连维度都不查，换个 embedder 直接 ValueError
    （shapes (1536,) and (512,) not aligned），整个检索 500。
    """
    # 没显式指定就跟着 embedder 走——写死 0.5 是在给一个没有语义能力的信号
    # 一半权重，实测会把 hit@1 从 88% 拉到 81%
    if alpha is None:
        alpha = default_alpha()

    # 先用倒排把候选收窄。以前这里是把整个集合载入内存、再在 Python 里重建
    # 一遍 BM25 倒排——每次查询都重新分词一遍所有片段。
    #
    # 多捞一些（limit 的 8 倍）：倒排只按关键词收，最终名次还要混进向量那一路，
    # 卡得太紧会把"关键词一般但语义很近"的那些提前筛掉。
    rows: list[Chunk] = []
    kw_scores: list[float] | None = None
    picked = await inverted.candidates(
        session, collection=collection, query=query, limit=max(limit * 8, 50))
    if picked:
        found = dict(
            (c.id, c) for c in (await session.execute(
                select(Chunk).where(Chunk.id.in_([cid for cid, _ in picked])))).scalars())
        pairs = [(found[cid], sc) for cid, sc in picked if cid in found]
        rows = [c for c, _ in pairs]
        # 倒排算好的分要带下去。在候选子集上重算 BM25，df 和平均长度是另一套数
        kw_scores = [sc for _, sc in pairs]

    if not rows:
        # 一个查询词都没命中，或者这批片段还没建倒排（存量数据升级上来就是这样）。
        # 退回全表扫，慢但不会漏——宁可慢，也不要因为索引没建好就说"没搜到"
        stmt = select(Chunk)
        if collection:
            stmt = stmt.where(Chunk.collection == collection)
        rows = list((await session.execute(stmt)).scalars())
        kw_scores = None   # 全表扫这条路照旧自己算
    if not rows:
        return []

    # 按 id 定序再排。hybrid_rank 用的是稳定排序，分数并列时保留输入顺序——
    # 而倒排那条路和全表扫那条路喂进去的顺序天生不同，同样的查询会因为走了
    # 哪条路给出不同的名次。并列本来就该有个确定的先后，用 id 就够了
    if kw_scores is not None:
        paired = sorted(zip(rows, kw_scores), key=lambda t: t[0].id)
        rows = [r for r, _ in paired]
        kw_scores = [s2 for _, s2 in paired]
    else:
        rows = sorted(rows, key=lambda r: r.id)

    stale = [r for r in rows if not usable(r.embed_model, r.embed_dim)]
    if stale:
        if on_degrade:
            names = sorted({r.embed_model or "未知模型" for r in stale})
            # 报的是整个集合里要重建多少条，不是本次候选里有几条——候选被倒排
            # 收窄过，用它会把问题说小，而用户要照着做的恰恰是"重建多少"
            total_stale = await stale_count(session, collection)
            on_degrade(
                f"{collection or '全部集合'}里有 {total_stale} 条向量由"
                f"「{'、'.join(names)}」建立，和当前的「{embedder_id()}」对不上，"
                f"本次已退回纯关键词检索。重建索引后恢复。"
            )
        alpha = 0.0   # 纯关键词
        vectors: list[Any] = [None] * len(rows)
        query_vec = await embed_text(query)
    else:
        query_vec = await embed_text(query)
        vectors = [from_blob(r.embedding, embedder_dim()) for r in rows]

    ranked = hybrid_rank(
        query,
        [r.content for r in rows],
        vectors,
        query_vec,
        alpha=alpha,
        keyword_scores=kw_scores,
    )
    out: list[dict[str, Any]] = []
    for idx, score, parts in ranked[:limit]:
        row = rows[idx]
        out.append(
            {
                "chunk_id": row.id,
                "document_id": row.document_id,
                "title": (row.meta or {}).get("title", ""),
                "ordinal": row.ordinal,
                "content": row.content,
                "score": round(score, 4),
                "signals": {k: round(v, 4) for k, v in parts.items()},
            }
        )
    return out


async def reindex(session: AsyncSession, collection: str | None = None) -> dict[str, Any]:
    """用当前 embedder 重算向量。

    换了 embedding 模型之后唯一的恢复手段。按批做：一次几千条 chunk 全塞给
    远端 embedding 接口，要么超时要么被限流，而中途失败又没有断点。
    """
    stmt = select(Chunk)
    if collection:
        stmt = stmt.where(Chunk.collection == collection)
    rows = list((await session.execute(stmt)).scalars())
    if not rows:
        return {"collection": collection, "reindexed": 0, "embedder": embedder_id()}

    model_id, dim = embedder_id(), embedder_dim()
    batch = 64
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vectors = await embed_texts([r.content for r in part])
        for row, vec in zip(part, vectors):
            row.embedding = to_blob(vec)
            row.embed_model = model_id
            row.embed_dim = dim
        await session.commit()
    # 倒排一起重建：两者都是"存量数据升级"的恢复手段，分成两个按钮只会漏点一个
    await inverted.rebuild(session, collection)
    return {"collection": collection, "reindexed": len(rows), "embedder": model_id}


async def stale_count(session: AsyncSession, collection: str | None = None) -> int:
    """有多少条向量和当前 embedder 对不上。知识库页据此提示要不要重建。

    用 SQL 数而不是把片段全载进来再在 Python 里过一遍——这个数在检索降级时
    也要取，那条路径上不该再来一次全表扫。
    """
    from sqlalchemy import func, or_

    stmt = select(func.count(Chunk.id)).where(
        or_(Chunk.embed_model != embedder_id(), Chunk.embed_dim != embedder_dim()))
    if collection:
        stmt = stmt.where(Chunk.collection == collection)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def delete_document(session: AsyncSession, doc_id: str) -> bool:
    doc = await session.get(Document, doc_id)
    if not doc:
        return False
    chunk_ids = list((await session.execute(
        select(Chunk.id).where(Chunk.document_id == doc_id))).scalars())
    await inverted.drop_chunk_terms(session, chunk_ids)
    await session.delete(doc)
    await session.commit()
    return True


async def list_collections(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(
            Document.collection,
            func.count(Document.id),
            func.sum(Document.chunk_count),
        ).group_by(Document.collection)
    )
    return [
        {"collection": name, "documents": docs, "chunks": chunks or 0}
        for name, docs, chunks in rows
    ]
