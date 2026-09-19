from __future__ import annotations

import json
from typing import Any

from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState, template_context
from app.memory import kb, store


async def run_memory(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """长期记忆读写节点。

    把记忆做成画布上的一个节点，而不是藏在 agent 内部，是为了让"什么时候记、
    记什么、记到哪个 scope"变成可见、可调的编排决策。
    """
    action = ctx.cfg("action", "recall")  # recall | write | clear
    scope = ctx.render_str(ctx.cfg("scope", "") or ctx.run.memory_scope, state)

    async with SessionLocal() as session:
        if action == "recall":
            query = ctx.render_str(ctx.cfg("query", "{{ last_message }}"), state)
            limit = int(ctx.cfg("limit", 5) or 5)
            hits = await store.recall(
                session, scope=scope, query=query, limit=limit, kind=ctx.cfg("kind") or None
            )
            text = "\n".join(f"- {h['content']}" for h in hits)
            result: dict[str, Any] = {
                "memories": hits,
                "count": len(hits),
                "text": text,
                "scope": scope,
            }
        elif action == "write":
            content = ctx.render_str(ctx.cfg("content", ""), state)
            if not content.strip():
                raise NodeError(ctx.node.id, "记忆写入节点没有内容")
            item = await store.remember(
                session,
                scope=scope,
                content=content,
                kind=ctx.cfg("kind", "fact") or "fact",
                importance=float(ctx.cfg("importance", 0.5) or 0.5),
                meta={"run_id": ctx.run.run_id, "node_id": ctx.node.id},
            )
            result = {"id": item.id, "saved": True, "scope": scope, "content": item.content}
        elif action == "clear":
            removed = await store.clear_scope(session, scope)
            result = {"cleared": removed, "scope": scope}
        else:
            raise NodeError(ctx.node.id, f"未知的记忆操作：{action}")

    ctx.emit(EventType.LOG, level="info",
             message=f"记忆 {action} @ {scope}：{result.get('count', result.get('cleared', 1))} 条")

    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: result.get("text", result)}
    return updates


async def run_retrieve(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """知识库检索节点。输出既有结构化命中列表，也有拼好的上下文文本。"""
    query = ctx.render_str(ctx.cfg("query", "{{ last_message }}"), state)
    if not query.strip():
        raise NodeError(ctx.node.id, "检索节点没有查询内容")

    collection = ctx.render_str(ctx.cfg("collection", "") or ctx.run.collection, state)
    limit = int(ctx.cfg("limit", 5) or 5)
    alpha = float(ctx.cfg("alpha", 0.5) or 0.5)  # 1=纯向量，0=纯关键词

    degraded: list[str] = []
    async with SessionLocal() as session:
        hits = await kb.search(
            session, collection=collection, query=query, limit=limit, alpha=alpha,
            on_degrade=degraded.append,
        )
    for note in degraded:
        # 检索悄悄少了一半能力，不说的话用户只会觉得"最近搜得不准"
        ctx.emit(EventType.LOG, level="warn", message=note)

    min_score = float(ctx.cfg("min_score", 0.0) or 0.0)
    hits = [h for h in hits if h["score"] >= min_score]

    # 拼成可以直接塞进 prompt 的上下文，带编号方便模型引用出处
    context_text = "\n\n".join(
        f"[{i + 1}] {h['title']}（片段 {h['ordinal']}）\n{h['content']}"
        for i, h in enumerate(hits)
    )
    # 命中落工件库。SQL 取数、工具调用、agent 工具调用都有快照，检索一直没有，
    # 于是结论只要来自知识库，"这个数是哪来的"这条链就断在这儿
    from app.core.artifact_store import put_json
    from app.memory.embeddings import embedder_id

    snapshot: str | None = None
    try:
        snapshot = await put_json(
            {
                "query": query, "collection": collection,
                "alpha": alpha, "min_score": min_score,
                "embedder": embedder_id(), "degraded": degraded,
                "hits": hits,
            },
            kind="retrieval_snapshot",
            run_id=ctx.run.run_id, node_id=ctx.node.id,
        )
    except Exception:  # noqa: BLE001 - 存不下不影响这次检索本身
        snapshot = None

    ctx.emit(
        EventType.RETRIEVE_END,
        collection=collection,
        query=query[:200],
        count=len(hits),
        top_score=hits[0]["score"] if hits else 0,
        degraded=bool(degraded),
        artifact=snapshot,
    )

    result = {
        "hits": hits,
        "count": len(hits),
        "text": context_text,
        "query": query,
        "collection": collection,
        "artifact": snapshot,
    }

    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: context_text}
    return updates
