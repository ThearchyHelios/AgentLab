from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryItem
from app.memory.embeddings import embed_text, from_blob, hybrid_rank, to_blob


async def remember(
    session: AsyncSession,
    *,
    scope: str,
    content: str,
    kind: str = "fact",
    meta: dict[str, Any] | None = None,
    importance: float = 0.5,
    dedupe: bool = True,
) -> MemoryItem:
    """写一条长期记忆。

    dedupe 会先看同 scope 下有没有近乎重复的内容 —— agent 很容易把同一件事
    反复记下来，不去重的话记忆库几轮就被同义句灌满了。
    """
    content = content.strip()
    if dedupe:
        existing = await recall(session, scope=scope, query=content, limit=1, min_score=0.0)
        if existing and existing[0]["score"] > 0.92:
            item = await session.get(MemoryItem, existing[0]["id"])
            if item:
                item.importance = max(item.importance, importance)
                item.use_count += 1
                await session.commit()
                return item

    vec = await embed_text(content)
    item = MemoryItem(
        scope=scope,
        kind=kind,
        content=content,
        meta=meta or {},
        importance=importance,
        embedding=to_blob(vec),
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def recall(
    session: AsyncSession,
    *,
    scope: str,
    query: str,
    limit: int = 5,
    kind: str | None = None,
    min_score: float = 0.05,
) -> list[dict[str, Any]]:
    """按混合相关度取回记忆，并按重要性和使用次数做轻微加权。"""
    stmt = select(MemoryItem).where(MemoryItem.scope == scope)
    if kind:
        stmt = stmt.where(MemoryItem.kind == kind)
    rows = list((await session.execute(stmt)).scalars())
    if not rows:
        return []

    query_vec = await embed_text(query)
    ranked = hybrid_rank(
        query,
        [r.content for r in rows],
        [from_blob(r.embedding) for r in rows],
        query_vec,
    )

    results: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for idx, score, parts in ranked:
        row = rows[idx]
        # 重要的、被用得多的记忆略微上浮
        boosted = score * (0.8 + 0.4 * row.importance) * (1 + min(row.use_count, 10) * 0.01)
        if boosted < min_score:
            continue
        results.append(
            {
                "id": row.id,
                "content": row.content,
                "kind": row.kind,
                "meta": row.meta,
                "importance": row.importance,
                "score": round(boosted, 4),
                "signals": {k: round(v, 4) for k, v in parts.items()},
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
        if len(results) >= limit:
            break

    # 记一下被召回过，作为下次排序的信号
    for r in results:
        item = await session.get(MemoryItem, r["id"])
        if item:
            item.use_count += 1
            item.last_used_at = now
    await session.commit()
    return results


async def forget(session: AsyncSession, memory_id: str) -> bool:
    item = await session.get(MemoryItem, memory_id)
    if not item:
        return False
    await session.delete(item)
    await session.commit()
    return True


async def clear_scope(session: AsyncSession, scope: str) -> int:
    result = await session.execute(delete(MemoryItem).where(MemoryItem.scope == scope))
    await session.commit()
    return result.rowcount or 0


async def list_memories(
    session: AsyncSession, *, scope: str | None = None, limit: int = 200
) -> list[MemoryItem]:
    stmt = select(MemoryItem).order_by(MemoryItem.created_at.desc()).limit(limit)
    if scope:
        stmt = stmt.where(MemoryItem.scope == scope)
    return list((await session.execute(stmt)).scalars())


async def list_scopes(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(MemoryItem.scope, func.count(MemoryItem.id)).group_by(MemoryItem.scope)
    )
    return [{"scope": scope, "count": count} for scope, count in rows]
