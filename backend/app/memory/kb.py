from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document
from app.memory.embeddings import embed_text, embed_texts, from_blob, hybrid_rank, to_blob

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
    if pieces:
        vectors = await embed_texts(pieces)
        for i, (piece, vec) in enumerate(zip(pieces, vectors)):
            session.add(
                Chunk(
                    document_id=doc.id,
                    collection=collection,
                    ordinal=i,
                    content=piece,
                    embedding=to_blob(vec),
                    meta={"title": doc.title},
                )
            )
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
    alpha: float = 0.5,
) -> list[dict[str, Any]]:
    stmt = select(Chunk)
    if collection:
        stmt = stmt.where(Chunk.collection == collection)
    rows = list((await session.execute(stmt)).scalars())
    if not rows:
        return []

    query_vec = await embed_text(query)
    ranked = hybrid_rank(
        query,
        [r.content for r in rows],
        [from_blob(r.embedding) for r in rows],
        query_vec,
        alpha=alpha,
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


async def delete_document(session: AsyncSession, doc_id: str) -> bool:
    doc = await session.get(Document, doc_id)
    if not doc:
        return False
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
