from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Document, MemoryItem, Skill
from app.memory import kb, store

# --------------------------------------------------------------------------
# 长期记忆
# --------------------------------------------------------------------------

memory_router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryIn(BaseModel):
    content: str = Field(min_length=1)
    scope: str = "default"
    kind: str = "fact"
    importance: float = Field(default=0.5, ge=0, le=1)
    meta: dict[str, Any] = Field(default_factory=dict)


class MemoryOut(BaseModel):
    id: str
    scope: str
    kind: str
    content: str
    importance: float
    use_count: int
    meta: dict[str, Any]
    created_at: Any = None

    model_config = {"from_attributes": True}


@memory_router.get("/scopes")
async def memory_scopes(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    return await store.list_scopes(session)


@memory_router.get("", response_model=list[MemoryOut])
async def list_memory(
    scope: str | None = None,
    limit: int = Query(default=200, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[MemoryItem]:
    return await store.list_memories(session, scope=scope, limit=limit)


@memory_router.post("", response_model=MemoryOut, status_code=201)
async def add_memory(
    payload: MemoryIn, session: AsyncSession = Depends(get_session)
) -> MemoryItem:
    return await store.remember(
        session,
        scope=payload.scope,
        content=payload.content,
        kind=payload.kind,
        importance=payload.importance,
        meta=payload.meta,
    )


@memory_router.get("/search")
async def search_memory(
    q: str,
    scope: str = "default",
    limit: int = 10,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    hits = await store.recall(session, scope=scope, query=q, limit=limit)
    return {"query": q, "scope": scope, "results": hits}


@memory_router.delete("/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str, session: AsyncSession = Depends(get_session)
) -> None:
    if not await store.forget(session, memory_id):
        raise HTTPException(404, "记忆不存在")


@memory_router.delete("/scope/{scope}")
async def clear_scope(scope: str, session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    return {"removed": await store.clear_scope(session, scope)}


# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------

kb_router = APIRouter(prefix="/api/kb", tags=["knowledge"])


class DocumentOut(BaseModel):
    id: str
    collection: str
    title: str
    source: str
    mime: str
    chunk_count: int
    created_at: Any = None

    model_config = {"from_attributes": True}


@kb_router.get("/collections")
async def collections(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    return await kb.list_collections(session)


@kb_router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    collection: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[Document]:
    stmt = select(Document).order_by(Document.created_at.desc()).limit(300)
    if collection:
        stmt = stmt.where(Document.collection == collection)
    return list((await session.execute(stmt)).scalars())


class IngestIn(BaseModel):
    collection: str = "default"
    title: str = ""
    content: str = Field(min_length=1)
    source: str = ""


@kb_router.post("/documents", response_model=DocumentOut, status_code=201)
async def ingest(payload: IngestIn, session: AsyncSession = Depends(get_session)) -> Document:
    return await kb.ingest_document(
        session,
        collection=payload.collection,
        title=payload.title,
        content=payload.content,
        source=payload.source,
    )


@kb_router.post("/upload", response_model=DocumentOut, status_code=201)
async def upload(
    file: UploadFile = File(...),
    collection: str = "default",
    session: AsyncSession = Depends(get_session),
) -> Document:
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件超过 10MB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "目前只支持 UTF-8 文本文件（txt / md / json / csv 等）") from None
    return await kb.ingest_document(
        session,
        collection=collection,
        title=file.filename or "上传文档",
        content=content,
        source=file.filename or "",
        mime=file.content_type or "text/plain",
    )


@kb_router.get("/search")
async def search_kb(
    q: str,
    collection: str | None = None,
    limit: int = 5,
    alpha: float = Query(default=0.5, ge=0, le=1, description="1=纯向量, 0=纯关键词"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    notes: list[str] = []
    hits = await kb.search(session, collection=collection, query=q, limit=limit,
                           alpha=alpha, on_degrade=notes.append)
    # 退回关键词这件事要跟着结果一起交出去，而不是只写进服务端日志——
    # 调用方（设置页的"试一下"、外部脚本）看到的是结果变差，得知道为什么
    return {"query": q, "collection": collection, "results": hits, "degraded": notes}


@kb_router.get("/embedding")
async def embedding_status(
    collection: str | None = None, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """当前用的是哪个 embedder，以及有多少条向量已经对不上了。"""
    from app.memory.embeddings import EMBEDDING_SETTING_KEY, embedder_dim, embedder_id
    from app.db.models import Setting

    row = await session.get(Setting, EMBEDDING_SETTING_KEY)
    saved = (row.value if row else {}) or {}
    return {
        "embedder": embedder_id(),
        "dim": embedder_dim(),
        "configured": bool(saved.get("kind")),
        "kind": saved.get("kind", "local"),
        "model": saved.get("model", ""),
        "stale_chunks": await kb.stale_count(session, collection),
    }


class EmbeddingIn(BaseModel):
    kind: str = Field(default="local", pattern="^(local|openai)$")
    model: str = ""


@kb_router.put("/embedding")
async def set_embedding(
    payload: EmbeddingIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """换 embedding 模型。

    建不起来就 400 原样交回原因——选了远端却静默退回本地哈希向量，
    用户会以为自己在用语义检索，而搜不出同义表达时完全无从怀疑到这里。
    """
    from app.memory import embeddings as emb
    from app.db.models import Setting

    try:
        emb.configure(payload.kind, payload.model)
    except emb.EmbedderUnavailable as e:
        raise HTTPException(400, str(e)) from e

    row = await session.get(Setting, emb.EMBEDDING_SETTING_KEY)
    value = {"kind": payload.kind, "model": payload.model}
    if row:
        row.value = value
    else:
        session.add(Setting(key=emb.EMBEDDING_SETTING_KEY, value=value))
    await session.commit()
    return {
        "embedder": emb.embedder_id(),
        "dim": emb.embedder_dim(),
        "stale_chunks": await kb.stale_count(session),
    }


@kb_router.post("/reindex")
async def reindex(
    collection: str | None = None, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """用当前 embedder 重算向量。换了模型之后唯一的恢复手段。"""
    return await kb.reindex(session, collection)


@kb_router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: str, session: AsyncSession = Depends(get_session)) -> None:
    if not await kb.delete_document(session, doc_id):
        raise HTTPException(404, "文档不存在")


# --------------------------------------------------------------------------
# Skill（方法论）
# --------------------------------------------------------------------------

skills_router = APIRouter(prefix="/api/skills", tags=["knowledge"])


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    instructions: str = ""
    examples: list[dict[str, Any]] = Field(default_factory=list)
    suggested_tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class SkillOut(SkillIn):
    id: str
    created_at: Any = None

    model_config = {"from_attributes": True}


@skills_router.get("", response_model=list[SkillOut])
async def list_skills(session: AsyncSession = Depends(get_session)) -> list[Skill]:
    return list((await session.execute(select(Skill).order_by(Skill.created_at.desc()))).scalars())


@skills_router.post("", response_model=SkillOut, status_code=201)
async def create_skill(payload: SkillIn, session: AsyncSession = Depends(get_session)) -> Skill:
    if (await session.execute(select(Skill).where(Skill.name == payload.name))).scalar_one_or_none():
        raise HTTPException(409, "同名 Skill 已存在")
    row = Skill(**payload.model_dump())
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@skills_router.patch("/{skill_id}", response_model=SkillOut)
async def update_skill(
    skill_id: str, payload: SkillIn, session: AsyncSession = Depends(get_session)
) -> Skill:
    row = await session.get(Skill, skill_id)
    if not row:
        raise HTTPException(404, "Skill 不存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return row


@skills_router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, session: AsyncSession = Depends(get_session)) -> None:
    row = await session.get(Skill, skill_id)
    if not row:
        raise HTTPException(404, "Skill 不存在")
    await session.delete(row)
    await session.commit()
