from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.db.base import SessionLocal
from app.memory import kb, store
from app.tools.registry import ToolArgsError, ToolContext, register


class KbSearchArgs(BaseModel):
    query: str = Field(description="检索问题")
    limit: int = Field(default=5, ge=1, le=20)
    collection: str | None = Field(default=None, description="限定知识库集合，留空则用节点配置的")


@register(
    name="kb_search",
    category="知识",
    description="在知识库里做混合检索（向量 + 关键词），返回最相关的片段。",
    args_schema=KbSearchArgs,
)
async def kb_search(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = KbSearchArgs(**kwargs)
    if ctx.fixed_scope and args.collection and args.collection != ctx.collection:
        # 遮罩、只读的运行（使用者的问数据、个人应用、个人蓝图的试运行）：知识库的内容不经遮罩，只能检索运行定下的那一个。
        # 拒而不是悄悄改用默认的：检索的不是参数里写的那个库，结果却照常交回，看的人会当成查过了那一个
        raise ToolArgsError(f"这次运行只能检索管理员设好的知识库，不能另指定知识库「{args.collection}」："
                            "去掉 collection 参数即可")
    async with SessionLocal() as session:
        hits = await kb.search(
            session,
            collection=args.collection or ctx.collection,
            query=args.query,
            limit=args.limit,
        )
    return {"query": args.query, "hits": hits, "count": len(hits)}


class MemoryRecallArgs(BaseModel):
    query: str = Field(description="想回忆什么")
    limit: int = Field(default=5, ge=1, le=20)


@register(
    name="memory_recall",
    category="知识",
    description="从长期记忆里取回与当前问题相关的内容。",
    args_schema=MemoryRecallArgs,
)
async def memory_recall(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = MemoryRecallArgs(**kwargs)
    async with SessionLocal() as session:
        hits = await store.recall(
            session, scope=ctx.memory_scope, query=args.query, limit=args.limit
        )
    return {"scope": ctx.memory_scope, "memories": hits, "count": len(hits)}


class MemoryWriteArgs(BaseModel):
    content: str = Field(description="要记住的内容，一条一个事实，写完整句子")
    kind: str = Field(default="fact", description="fact | preference | episode")
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


@register(
    name="memory_write",
    category="知识",
    description="把一条值得长期记住的信息写入记忆。相似内容会自动去重。",
    args_schema=MemoryWriteArgs,
    dangerous=False,
    # 写的是平台自己的记忆：默认不用人确认，但不是只读
    writes_state=True,
)
async def memory_write(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = MemoryWriteArgs(**kwargs)
    async with SessionLocal() as session:
        item = await store.remember(
            session,
            scope=ctx.memory_scope,
            content=args.content,
            kind=args.kind,
            importance=args.importance,
            meta={"run_id": ctx.run_id, "node_id": ctx.node_id},
        )
    return {"id": item.id, "scope": item.scope, "saved": True}
