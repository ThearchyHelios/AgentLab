from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import get_session
from app.db.models import Document, MemoryItem, Skill
from app.memory import inverted, kb, store

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
    #: ready | processing | failed。前端据此显示"处理中 3200/7914 段"
    status: str = "ready"
    error: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
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
    background: BackgroundTasks,
    file: UploadFile = File(...),
    collection: str = "default",
    session: AsyncSession = Depends(get_session),
) -> Document:
    # 边读边数，超了立刻停——原来是 raw = await file.read() 读完再判断，
    # 传个 1GB 的文件，内存在报 413 之前就已经吃掉了。那个检查拦的是"入库"，
    # 不是"占内存"
    limit = settings.max_upload_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while piece := await file.read(1024 * 1024):
        total += len(piece)
        if total > limit:
            raise HTTPException(413, f"文件超过 {settings.max_upload_mb}MB")
        chunks.append(piece)
    raw = b"".join(chunks)

    from app.memory.parsing import UnsupportedDocument, extract

    try:
        content = extract(raw, file.filename or "", file.content_type or "")
    except UnsupportedDocument as e:
        # 原样交回：里面写的是"装哪个包"或"这是扫描件"，都能照着做
        raise HTTPException(415, str(e)) from None
    doc = await kb.create_document(
        session,
        collection=collection,
        title=file.filename or "上传文档",
        content=content,
        source=file.filename or "",
        mime=file.content_type or "text/plain",
        status="processing",
    )
    # 立刻返回，切块和算向量在后台跑。实测一个 10MB 文档用本地哈希要两分半，
    # 换成远端 embedding 是几十分钟——压在请求里必然超时，而后端其实还在跑，
    # 界面显示失败、数据其实成功，比直接拒绝还糟
    background.add_task(_process_in_background, doc.id)
    return doc


async def _process_in_background(doc_id: str) -> None:
    """后台切块。进度写回文档行，失败也写回——不然它会永远停在"处理中"。"""
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        if not doc:
            return
        try:
            # 进度由 process_document 自己按批提交——它就在主事务里，
            # 不会和另一个 session 抢 SQLite 的写锁
            await kb.process_document(session, doc)
        except Exception as e:  # noqa: BLE001
            await session.rollback()
            doc = await session.get(Document, doc_id)
            if doc:
                doc.status = "failed"
                doc.error = f"{type(e).__name__}: {e}"[:500]
                await session.commit()


@kb_router.get("/search")
async def search_kb(
    q: str,
    collection: str | None = None,
    limit: int = 5,
    alpha: float | None = Query(default=None, ge=0, le=1,
                                description="1=纯向量, 0=纯关键词；不传则按 embedder 能力取默认"),
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
    from app.memory.embeddings import (
        EMBEDDING_SETTING_KEY, default_alpha, embedder_dim, embedder_id, has_semantics,
        unavailable_reason,
    )
    from app.db.models import Setting

    row = await session.get(Setting, EMBEDDING_SETTING_KEY)
    saved = (row.value if row else {}) or {}
    # 配的是语义模型，实际在用本地哈希：保存的配置没生效（多半是启动时本机的模型
    # 服务还没起）。这时"对不上"的存量向量正是那个模型建好的，界面不能再劝人重建——
    # 那等于拿哈希把它们覆盖掉，连上之后还得再重建一遍
    fallback = saved.get("kind") == "openai" and not has_semantics()
    return {
        "embedder": embedder_id(),
        "dim": embedder_dim(),
        "configured": bool(saved.get("kind")),
        "kind": saved.get("kind", "local"),
        "model": saved.get("model", ""),
        "base_url": saved.get("base_url", ""),
        "stale_chunks": await kb.stale_count(session, collection),
        # 记忆和知识库共用一个 embedder，换模型时一起失效——报一个漏一个
        # 的话，用户点完重建还是想不起事，而且不知道为什么
        "stale_memories": await store.stale_count(session),
        "has_semantics": has_semantics(),
        "fallback": fallback,
        "fallback_reason": (unavailable_reason() or "") if fallback else "",
        "default_alpha": default_alpha(),
        # 没建倒排的片段会走全表扫——结果对，但慢
        "unindexed_chunks": await inverted.missing_count(session, collection),
    }


class EmbeddingIn(BaseModel):
    kind: str = Field(default="local", pattern="^(local|openai)$")
    model: str = ""
    #: 任何讲 OpenAI /v1/embeddings 的地址：本机 LM Studio / Ollama / vLLM / TEI，
    #: 或者你自己的网关。留空才走 api.openai.com
    base_url: str = ""


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
        emb.configure(payload.kind, payload.model, payload.base_url)
    except emb.EmbedderUnavailable as e:
        raise HTTPException(400, str(e)) from e

    row = await session.get(Setting, emb.EMBEDDING_SETTING_KEY)
    value = {"kind": payload.kind, "model": payload.model,
             "base_url": payload.base_url}
    if row:
        row.value = value
    else:
        session.add(Setting(key=emb.EMBEDDING_SETTING_KEY, value=value))
    await session.commit()
    return {
        "embedder": emb.embedder_id(),
        "dim": emb.embedder_dim(),
        "stale_chunks": await kb.stale_count(session),
        "stale_memories": await store.stale_count(session),
    }


@kb_router.get("/embedding/probe")
async def probe_embedding(base_url: str) -> dict[str, Any]:
    """问问这个地址上有哪些模型。

    自己填模型名太容易写错（LM Studio 里叫
    text-embedding-qwen3-embedding-4b，不是 Qwen3-Embedding-4B），
    而填错的表现是切换时报一句 404，人还以为是服务没起。
    """
    import httpx

    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            data = (await client.get(url)).json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"连不上 {url}：{type(e).__name__}: {e}") from e
    models = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
    return {"base_url": base_url, "models": models}


@kb_router.post("/reindex")
async def reindex(
    collection: str | None = None, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """用当前 embedder 重算向量。换了模型之后唯一的恢复手段。

    知识库和长期记忆一起重建：它们共用一个 embedder，换模型时一起失效。
    分成两个按钮的结果是点了一个、以为好了，另一个还在悄悄退回关键词——
    真踩过，记忆那边连个提示都没有。
    """
    out = await kb.reindex(session, collection)
    # 记忆不按 collection 分，重建就是全量
    out["memories_reindexed"] = await store.reindex(session)
    return out


#: 单段返回多少字。切块本来就在千字量级，详情页是给人扫一眼"切得对不对"的，
#: 不是给人通读原文的——整篇几千段全量吐出去，一份大文档能有几兆
_CHUNK_PREVIEW = 1200


@kb_router.get("/documents/{doc_id}")
async def get_document(
    doc_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """一份文档被切成了什么样。

    切块这一层做了不少事——按行边界留重叠、跨块把表头续上（memory/kb.py 的
    chunk_text），而在此之前结果在界面上一处都看不见。检索不准的时候第一个
    该看的就是它：表头有没有续上、重叠对不对、哪一段被截断了。
    """
    from app.db.models import Chunk

    doc = await session.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")

    rows = list((await session.execute(
        select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.ordinal)
    )).scalars())

    return {
        "document": DocumentOut.model_validate(doc).model_dump(mode="json"),
        "chunks": [
            {
                "id": c.id,
                "ordinal": c.ordinal,
                "content": c.content[:_CHUNK_PREVIEW],
                "truncated": len(c.content) > _CHUNK_PREVIEW,
                "chars": len(c.content),
                "token_len": c.token_len,
                # 哪条向量是谁建的。一份文档里混着两个模型建的向量是真会发生的
                # （换模型之后只重建了一部分），而那会让检索悄悄退回关键词
                "embed_model": c.embed_model or "",
                "has_vector": c.embedding is not None,
            }
            for c in rows
        ],
    }


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
