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
    assert "重建索引" in notes[0] and "2 条" in notes[0], notes[0]


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


# --------------------------------------------------------------------------
# 文档解析
# --------------------------------------------------------------------------
#
# 以前 upload 只接受 UTF-8，PDF 传上去直接 415；而 mime 字段记了却从不参与
# 任何判断。知识库里最常见的恰恰是 PDF 和 Word。


def test_plain_text_still_goes_straight_through() -> None:
    from app.memory.parsing import extract

    assert extract("就是一段文本".encode(), "note.txt") == "就是一段文本"
    assert extract("# 标题".encode(), "readme.md") == "# 标题"


def test_html_is_stripped_to_text() -> None:
    from app.memory.parsing import extract

    html = """<html><head><style>p{color:red}</style></head>
    <body><nav>导航</nav><h1>权限说明</h1><p>只有管理员能改角色。</p>
    <script>alert(1)</script><footer>版权</footer></body></html>""".encode()
    out = extract(html, "doc.html")
    assert "权限说明" in out and "只有管理员能改角色" in out
    # 脚本样式导航页脚都不是正文，留着只会污染检索
    assert "alert" not in out and "color:red" not in out
    assert "导航" not in out and "版权" not in out


def test_an_unreadable_binary_says_what_to_do() -> None:
    """报错要能照着做，不是一句 UnicodeDecodeError。"""
    from app.memory.parsing import UnsupportedDocument, extract

    with pytest.raises(UnsupportedDocument) as e:
        extract(b"\x00\x01\x02\xff\xfe", "mystery.bin")
    assert "UTF-8" in str(e.value) and "PDF" in str(e.value)


def test_a_missing_parser_tells_you_which_package(monkeypatch) -> None:
    """没装可选依赖时给的必须是"装哪个包"，而不是 ImportError。"""
    import builtins

    from app.memory.parsing import UnsupportedDocument, extract

    real_import = builtins.__import__

    def _no_pypdf(name, *a, **kw):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_pypdf)
    with pytest.raises(UnsupportedDocument) as e:
        extract(b"%PDF-1.4 fake", "doc.pdf")
    assert "agentlab-backend[docs]" in str(e.value)


def test_a_scanned_pdf_is_called_out_rather_than_ingested_empty() -> None:
    """扫描件提不出文字。悄悄存一份空文档，用户会以为传成功了、却永远搜不到。"""
    pytest.importorskip("pypdf")
    import pypdf

    from app.memory.parsing import UnsupportedDocument, extract

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = __import__("io").BytesIO()
    writer.write(buf)

    with pytest.raises(UnsupportedDocument) as e:
        extract(buf.getvalue(), "scan.pdf")
    assert "扫描件" in str(e.value)


# --------------------------------------------------------------------------
# 长期记忆：和知识库同一个坑，以前只有知识库有恢复手段
# --------------------------------------------------------------------------


async def _memory(scope: str, content: str):
    from app.memory import store

    async with SessionLocal() as session:
        return await store.remember(session, scope=scope, content=content, dedupe=False)


async def test_memory_records_who_built_the_vector() -> None:
    from app.db.models import MemoryItem

    item = await _memory("memtest_mark", "后端跑在 8001 端口。")
    async with SessionLocal() as session:
        row = await session.get(MemoryItem, item.id)
    assert row.embed_model == emb.embedder_id() and row.embed_dim == emb.embedder_dim()


async def test_memory_recall_degrades_out_loud() -> None:
    """以前退得悄无声息。知识库那边至少会发一条 warn，记忆这边只是"最近想不起
    事"，而人根本不会想到要去重建索引。"""
    from sqlalchemy import select

    from app.db.models import MemoryItem
    from app.memory import store

    await _memory("memtest_stale", "公司年会定在十二月二十号。")
    async with SessionLocal() as session:
        row = (await session.execute(select(MemoryItem).where(
            MemoryItem.scope == "memtest_stale"))).scalars().first()
        row.embed_model, row.embed_dim = "openai:text-embedding-3-small", 1536
        row.embedding = to_blob(np.ones(1536, dtype=np.float32))
        await session.commit()

    notes: list[str] = []
    async with SessionLocal() as session:
        hits = await store.recall(session, scope="memtest_stale", query="年会",
                                  on_degrade=notes.append)
    assert hits, "退回关键词也要能召回，而不是空手"
    assert notes and "重建索引" in notes[0], notes


async def test_memory_reindex_restores_it() -> None:
    from sqlalchemy import select

    from app.db.models import MemoryItem
    from app.memory import store

    await _memory("memtest_fix", "张三的邮箱是 zhangsan@example.com。")
    async with SessionLocal() as session:
        row = (await session.execute(select(MemoryItem).where(
            MemoryItem.scope == "memtest_fix"))).scalars().first()
        row.embed_model, row.embed_dim = "别的模型", 99
        await session.commit()

        assert await store.stale_count(session, "memtest_fix") == 1
        assert await store.reindex(session, "memtest_fix") == 1
        assert await store.stale_count(session, "memtest_fix") == 0

    notes: list[str] = []
    async with SessionLocal() as session:
        hits = await store.recall(session, scope="memtest_fix", query="邮箱",
                                  on_degrade=notes.append)
    assert hits and not notes, "重建之后不该再降级"


async def test_one_rebuild_covers_both_stores() -> None:
    """知识库和记忆共用一个 embedder，换模型时一起失效。

    分成两个按钮的结果是点了一个、以为好了，另一个还在悄悄退回关键词。
    """
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.db.models import Chunk, MemoryItem
    from app.main import app
    from app.memory import store

    await _seed("both_fix", ["平台管理员有六个。"])
    await _memory("both_mem", "商家账户共五个。")

    async with SessionLocal() as session:
        for model_cls, scope_col, scope_val in (
            (Chunk, Chunk.collection, "both_fix"), (MemoryItem, MemoryItem.scope, "both_mem")
        ):
            for row in (await session.execute(
                    select(model_cls).where(scope_col == scope_val))).scalars():
                row.embed_model, row.embed_dim = "旧模型", 7
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        status = (await c.get("/api/kb/embedding")).json()
        assert status["stale_chunks"] >= 1 and status["stale_memories"] >= 1

        out = (await c.post("/api/kb/reindex")).json()
        assert out["reindexed"] >= 1
        assert out["memories_reindexed"] >= 1, "重建只管了知识库，记忆还留在旧模型上"

        after = (await c.get("/api/kb/embedding")).json()
    assert after["stale_chunks"] == 0 and after["stale_memories"] == 0


# --------------------------------------------------------------------------
# 表格横跨多块时还读不读得懂
# --------------------------------------------------------------------------


def _table_doc(rows: int = 60) -> str:
    """一份表格长到必然被切开的文档。"""
    body = ["门店 | 城市 | 销售额 | 同比"]
    body += [f"门店{i:02d} | 上海 | {100000 + i * 3137} | {i - 10}%" for i in range(rows)]
    return "季度报告\n\n" + "\n\n".join(body)


def test_a_split_table_keeps_its_header_in_every_chunk() -> None:
    """一张几十行的表会横跨好几块，除了第一块其余都只有数据行。

    「上海 | 172151 | 13%」这样的片段召回出来，模型没法知道这几个数是什么。
    表头才几十个字，每块带一份换来的是这块单独拿出来也读得懂。
    """
    chunks = kb.chunk_text(_table_doc())
    assert len(chunks) > 1, "语料不够长，没切开就测不到这件事"
    for i, c in enumerate(chunks[1:], 1):
        assert "门店 | 城市 | 销售额 | 同比" in c, f"块{i} 丢了表头"


def test_the_header_leads_the_chunk_not_buried_in_it() -> None:
    """表头要在最前面。

    第一版写成 tail+header+block，表头被夹在重叠的那几行后面成了中间一行，
    块的开头仍然是一串没有主语的数——查 has_header 是 True，看起来修好了，
    实际没有。
    """
    chunks = kb.chunk_text(_table_doc())
    for i, c in enumerate(chunks[1:], 1):
        assert c.splitlines()[0].startswith("门店 | 城市"), (
            f"块{i} 的第一行是「{c.splitlines()[0][:30]}」，表头没领头")


def test_overlap_never_severs_a_row() -> None:
    """重叠原来按字符切（buf[-120:]），会从行中间断开。

    实测切出来的块开头是「| 上海 | 172151 | 13%」——门店名被切没了，
    剩一列没有主语的数字。
    """
    for c in kb.chunk_text(_table_doc()):
        first = c.splitlines()[0].lstrip()
        assert not first.startswith("|"), f"首行被腰斩：{first[:40]}"


def test_plain_prose_is_unaffected_by_the_table_handling() -> None:
    """表格那套不能把普通文档搞坏。"""
    text = "\n\n".join(f"第{i}段。" + "内容" * 60 for i in range(8))
    chunks = kb.chunk_text(text)
    assert len(chunks) > 1
    assert not any(c.startswith("门店") for c in chunks)
    # 正文一个字都不能少
    assert all(f"第{i}段。" in "".join(chunks) for i in range(8))


async def test_a_word_document_with_a_table_survives_the_whole_pipeline() -> None:
    """解析 → 切块 → 检索，端到端拿回来的片段要能读懂。"""
    docx = pytest.importorskip("docx")

    import io

    from app.memory.parsing import extract

    d = docx.Document()
    d.add_heading("门店运营手册", 0)
    d.add_paragraph("平台管理员拥有 platform 级别角色。")
    t = d.add_table(rows=1, cols=4)
    for i, h in enumerate(("门店", "城市", "销售额", "同比")):
        t.cell(0, i).text = h
    for i in range(60):
        row = t.add_row()
        for j, v in enumerate((f"门店{i:02d}", "上海", str(100000 + i * 3137), f"{i - 10}%")):
            row.cells[j].text = v
    buf = io.BytesIO()
    d.save(buf)

    text = extract(buf.getvalue(), "手册.docx")
    assert "平台管理员" in text and "门店00 | 上海" in text

    async with SessionLocal() as session:
        await kb.ingest_document(session, collection="docx_e2e", title="手册", content=text)
        hits = await kb.search(session, collection="docx_e2e", query="门店 销售额", limit=3)

    assert hits
    # 命中的片段里必须能看出这些数是什么
    assert any("门店 | 城市 | 销售额" in h["content"] for h in hits), (
        "召回的片段全都没有表头，拿回去也读不懂")
