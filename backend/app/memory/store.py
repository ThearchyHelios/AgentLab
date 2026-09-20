from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryItem
from app.memory.embeddings import (
    cosine, embed_text, embed_texts, embedder_dim, embedder_id, from_blob, hybrid_rank,
    to_blob, usable,
)

# 判重阈值：余弦相似度高于它才算同一件事。比排名分数严格得多，
# 因为这里比的是真实语义距离，不是"谁排第一"。
_DEDUPE_THRESHOLD = 0.92


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
    vec = await embed_text(content)

    if dedupe:
        # 判重必须看**真实相似度**，不能用 recall 的分数。recall 走 hybrid_rank，
        # 那里的分数是 min-max 排名归一化的结果：排第一的那条在两路信号上都会
        # 被归成 1.0，与它和查询像不像毫无关系。于是 scope 里只要已有两条记忆，
        # 新内容随便撞上一个字排到第一，就会被判成"重复"而丢弃，接口还返回 201
        # 并把那条不相关的旧记忆当成"刚保存的"交回去——静默丢数据。
        stmt = select(MemoryItem).where(MemoryItem.scope == scope)
        if kind:
            stmt = stmt.where(MemoryItem.kind == kind)
        candidates = list((await session.execute(stmt)).scalars())
        best, best_score = None, 0.0
        for row in candidates:
            # 对不上当前 embedder 的直接跳过：维度不同 cosine 会抛，维度碰巧
            # 相同但模型不同则更糟——两个空间之间的相似度是个随机数，
            # 拿它判重会把不相关的旧记忆认成"重复"然后丢掉新内容
            if not usable(row.embed_model, row.embed_dim):
                continue
            other = from_blob(row.embedding, embedder_dim())
            if other is None:
                continue
            score = cosine(vec, other)
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score > _DEDUPE_THRESHOLD:
            best.importance = max(best.importance, importance)
            best.use_count += 1
            await session.commit()
            await session.refresh(best)
            return best

    item = MemoryItem(
        scope=scope,
        kind=kind,
        content=content,
        meta=meta or {},
        importance=importance,
        embedding=to_blob(vec),
        embed_model=embedder_id(),
        embed_dim=embedder_dim(),
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
    on_degrade: Any = None,
) -> list[dict[str, Any]]:
    """按混合相关度取回记忆，并按重要性和使用次数做轻微加权。

    存量向量和当前 embedder 对不上时整体退回纯关键词。以前这里退得悄无声息
    ——知识库那边至少会发一条 warn，记忆这边只是"最近想不起事"，而人根本
    不会想到要去重建索引。
    """
    stmt = select(MemoryItem).where(MemoryItem.scope == scope)
    if kind:
        stmt = stmt.where(MemoryItem.kind == kind)
    rows = list((await session.execute(stmt)).scalars())
    if not rows:
        return []

    query_vec = await embed_text(query)
    # 和 kb.search 同一个道理：有对不上的就整体退回关键词，而不是让它们静默沉底
    stale = [r for r in rows if not usable(r.embed_model, r.embed_dim)]
    if stale:
        if on_degrade:
            names = sorted({r.embed_model or "未知模型" for r in stale})
            on_degrade(
                f"记忆域「{scope}」里有 {len(stale)}/{len(rows)} 条向量由"
                f"「{'、'.join(names)}」建立，和当前的「{embedder_id()}」对不上，"
                f"本次已退回纯关键词召回。重建索引后恢复。"
            )
        vectors: list[Any] = [None] * len(rows)
        alpha = 0.0
    else:
        vectors = [from_blob(r.embedding, embedder_dim()) for r in rows]
        alpha = 0.5
    ranked = hybrid_rank(
        query,
        [r.content for r in rows],
        vectors,
        query_vec,
        alpha=alpha,
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


async def stale_count(session: AsyncSession, scope: str | None = None) -> int:
    """有多少条记忆的向量和当前 embedder 对不上。"""
    from sqlalchemy import or_

    stmt = select(func.count(MemoryItem.id)).where(
        or_(MemoryItem.embed_model != embedder_id(), MemoryItem.embed_dim != embedder_dim()))
    if scope:
        stmt = stmt.where(MemoryItem.scope == scope)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def reindex(session: AsyncSession, scope: str | None = None) -> int:
    """用当前 embedder 重算记忆的向量，返回重算了多少条。

    换 embedding 模型之后记忆和知识库一起失效，但在此之前只有知识库有重建
    路径——记忆这边悄悄退回关键词，而且没有任何恢复手段。
    """
    stmt = select(MemoryItem)
    if scope:
        stmt = stmt.where(MemoryItem.scope == scope)
    rows = list((await session.execute(stmt)).scalars())
    if not rows:
        return 0

    model_id, dim = embedder_id(), embedder_dim()
    from app.memory.kb import _EMBED_BATCH

    batch = _EMBED_BATCH   # 和 kb 那边统一：一次几千条全发给远端接口必被限流
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vectors = await embed_texts([r.content for r in part])
        for row, vec in zip(part, vectors):
            row.embedding = to_blob(vec)
            row.embed_model = model_id
            row.embed_dim = dim
        await session.commit()
    return len(rows)


async def list_scopes(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(MemoryItem.scope, func.count(MemoryItem.id)).group_by(MemoryItem.scope)
    )
    return [{"scope": scope, "count": count} for scope, count in rows]
