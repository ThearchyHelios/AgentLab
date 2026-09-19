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

    # 写入必须看得见：这是往长期记忆里存东西，会影响以后每一次对话。
    # 以前发的是 info 日志，而解码层丢弃所有 info，等于系统背着用户记了东西
    ctx.emit(
        EventType.MEMORY_END,
        action=action,
        scope=scope,
        count=result.get("count", result.get("cleared", 1 if action == "write" else 0)),
        # 记了什么要原样说出来，只报"写了 1 条"等于没说
        content=result.get("content", "")[:300],
    )

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
    # 不写死 0.5：没配置就跟着 embedder 的能力走（见 embeddings.default_alpha）
    raw_alpha = ctx.cfg("alpha")
    alpha = float(raw_alpha) if raw_alpha not in (None, "") else None

    # 要重排就先多捞一些：重排只能在初筛给出的候选里挑，初筛只给 5 条的话
    # 它最多把这 5 条换个顺序，第 6 条是对的也救不回来
    mode = ctx.cfg("rerank", "off") or "off"
    fetch = max(limit, 20) if mode == "model" else limit

    degraded: list[str] = []
    async with SessionLocal() as session:
        hits = await kb.search(
            session, collection=collection, query=query, limit=fetch, alpha=alpha,
            on_degrade=degraded.append,
        )

    if mode == "model" and hits:
        from app.engine.nodes.llm import _model_spec
        from app.memory.rerank import rerank
        from app.providers.factory import ProviderNotConfigured, get_chat_model

        # 重排有自己的预算，不跟节点上那个走：
        #
        # 它的输出只有一行几十字的 JSON，但模型为了读完 20 条候选会花掉上千个
        # 输出 token——实测 deepseek-v4-pro 单次用 800~1600 个，一旦顶到上限，
        # 正文就什么都不剩（失败那次 out_tokens 正好等于 max_tokens）。
        #
        # thinking=off 对 Anthropic 有效；openai_compatible 那条路径不读这个字段
        # （factory.py 只在 anthropic 分支里设 thinking），所以真正兜住的是
        # 上面那个预算。打分任务本来也不需要思考模式。
        spec = _model_spec(ctx).model_copy(update={
            "max_tokens": max(int(ctx.cfg("max_tokens") or 0), 4096),
            "thinking": "off",
        })
        try:
            async with SessionLocal() as session:
                model, _ = await get_chat_model(session, spec)
        except ProviderNotConfigured as e:
            degraded.append(f"重排用不了：{e}")
            model = None
        hits = await rerank(model, query, hits, top_n=limit, on_note=degraded.append)
    else:
        hits = hits[:limit]
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
    from app.memory.embeddings import default_alpha, embedder_id

    snapshot: str | None = None
    try:
        snapshot = await put_json(
            {
                "query": query, "collection": collection,
                "alpha": alpha if alpha is not None else default_alpha(),
                "min_score": min_score,
                "embedder": embedder_id(), "rerank": mode, "degraded": degraded,
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
        reranked=mode == "model",
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
