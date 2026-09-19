"""检索质量的基线。

在此之前 alpha 默认值、切块大小、混合权重这些参数改了之后没有任何办法判断
是变好还是变坏——只能搜两下"感觉准了"。这个文件把它变成数字。

两个指标：
  hit@k  前 k 条里有没有正确片段（召回，最要紧的一条）
  MRR    正确片段排第几的倒数平均（排序质量）

评测集刻意分成两类问题：
  lexical  问句和答案有字面重叠（"商家账户是什么"→ 文档里就写着"商家账户"）
  semantic 只有语义相关，字面几乎不沾（"谁能改别人的权限" → "平台管理员…"）

第二类是本地哈希向量的照妖镜：它是词频哈希、没有语义泛化，这类问题基本全挂。
基线把这个事实钉住——将来换成真 embedding 模型，这里的数字必须涨。
"""

from __future__ import annotations

import pytest

from app.db.base import SessionLocal
from app.memory import embeddings as emb
from app.memory import kb

COLLECTION = "evalset"

#: 语料。24 篇，全部围绕权限/账户/计费这一个主题——**互相干扰是刻意的**。
#: 第一版只有 8 篇、主指标用 hit@3，结果各档 alpha 全是 100%，等于什么都没量：
#: 8 选 3 瞎猜都有 37%。检索评测集不制造混淆就是在自我表扬。
DOCS: list[tuple[str, str]] = [
    ("权限总则", "平台管理员拥有 platform 或 regional 级别的角色。"
                 "只有他们可以修改其他用户的角色，也只有他们能停用账户。"),
    ("区域管理员", "区域管理员的角色级别是 regional，只能管理本区域内的商家，"
                   "跨区域的操作会被拒绝并记录一条告警。"),
    ("客服权限", "平台客服可以查看订单和用户资料，但不能修改角色，也不能导出数据。"),
    ("商家账户", "商家账户指 provider 表中的一条记录，每个商家有唯一的 providerID，"
                 "并关联一个联系人邮箱。商家账户不能登录后台管理界面。"),
    ("商家关联用户", "providerUsers 表记录商家和用户的关联关系，"
                     "一个商家可以绑定多个用户，但只有一个主联系人。"),
    ("商家状态", "商家有待审核、正常、暂停三种状态，暂停的商家不出现在前台列表里，"
                 "但历史订单仍然可查。"),
    ("普通用户", "普通用户只能查看和编辑自己的资料，无法看到其他人的订单明细。"),
    ("用户注销", "用户注销后资料进入匿名化流程，订单记录保留但姓名邮箱被替换成占位符。"),
    ("角色变更流程", "调整任何人的角色都要先在工单系统里提申请，"
                     "审批通过后由运维在后台执行，变更会记录在 audit_log 表里。"),
    ("审批时效", "工单默认两个工作日内处理完，超时会自动升级给上一级负责人。"),
    ("操作留痕", "audit_log 记录谁在什么时候改了什么，保留三年，任何人不能删除。"),
    ("登录与会话", "会话有效期为 7 天，超过后需要重新登录。"
                   "连续五次密码错误会锁定账户十五分钟。"),
    ("双因素认证", "管理员账户强制开启双因素认证，普通用户可选，"
                   "关闭时需要邮箱确认。"),
    ("密码规则", "密码至少十位，必须含大小写和数字，九十天过期，不能和前三次重复。"),
    ("数据导出", "导出订单数据需要 export 权限，导出记录会留痕，"
                 "单次最多导出十万行，超过要拆批。"),
    ("导出审批", "导出含手机号或身份证的字段需要额外审批，审批人是数据合规负责人。"),
    ("计费口径", "月度活跃商家指当月至少产生过一笔订单的商家，"
                 "与注册商家总数不是一回事，对账时不要混用。"),
    ("结算周期", "每月一号结算上个月的账单，账期三十天，逾期按日计息。"),
    ("退款规则", "订单完成七天内可申请退款，超过七天需要人工介入，"
                 "退款金额原路返回。"),
    ("发票", "发票按自然月开具，需要在结算完成后申请，电子发票当天到邮箱。"),
    ("通知设置", "系统通知默认发到联系人邮箱，可以在设置页改成短信，"
                 "但验证码类通知始终走短信。"),
    ("告警通道", "线上故障告警走企业微信机器人，值班人五分钟内不响应会电话呼叫。"),
    ("接口限流", "开放接口每个商家每分钟六十次，超过返回 429，"
                 "重试要带指数退避。"),
    ("数据保留", "订单明细保留五年，日志保留半年，超期由归档任务自动清理。"),
]

#: (问题, 应该命中哪篇, 类型)
QUESTIONS: list[tuple[str, str, str]] = [
    ("商家账户是什么", "商家账户", "lexical"),
    ("会话多久过期", "登录与会话", "lexical"),
    ("导出订单最多多少行", "数据导出", "lexical"),
    ("月度活跃商家怎么算", "计费口径", "lexical"),
    ("密码输错几次会锁", "登录与会话", "lexical"),
    ("audit_log 保留多久", "操作留痕", "lexical"),
    ("接口限流是多少", "接口限流", "lexical"),
    ("结算周期是怎样的", "结算周期", "lexical"),
    ("谁能改别人的权限", "权限总则", "semantic"),
    ("改角色要走什么手续", "角色变更流程", "semantic"),
    ("商家能不能登后台", "商家账户", "semantic"),
    ("验证码发到哪里", "通知设置", "semantic"),
    ("注册数和活跃数能混着用吗", "计费口径", "semantic"),
    ("客服可以导数据吗", "客服权限", "semantic"),
    ("下单之后多久还能退", "退款规则", "semantic"),
    ("工单没人处理怎么办", "审批时效", "semantic"),
]


@pytest.fixture(scope="module", autouse=True)
def _corpus():
    """建一次语料，整个模块共用——每个用例重灌一遍太慢，而且语料是只读的。"""
    import asyncio

    emb.configure("local")

    async def build() -> None:
        async with SessionLocal() as session:
            from sqlalchemy import select

            from app.db.models import Document
            existing = list((await session.execute(
                select(Document).where(Document.collection == COLLECTION))).scalars())
            for doc in existing:
                await kb.delete_document(session, doc.id)
            for title, body in DOCS:
                await kb.ingest_document(
                    session, collection=COLLECTION, title=title, content=body)

    asyncio.new_event_loop().run_until_complete(build())
    yield


async def _measure(alpha: float, k: int = 5) -> dict[str, float]:
    """跑一遍评测集。

    主指标是 hit@1（排第一的那条对不对），因为 hit@k 在小语料上太容易满分：
    第一版 8 篇文档取 top-3，各档 alpha 全是 100%——瞎猜都有 37%。
    """
    stats: dict[str, list[float]] = {"all_top1": [], "all_hit": [], "all_rr": [],
                                     "lexical_top1": [], "semantic_top1": []}
    async with SessionLocal() as session:
        for question, want, kind in QUESTIONS:
            hits = await kb.search(session, collection=COLLECTION, query=question,
                                   limit=k, alpha=alpha)
            titles = [h["title"] for h in hits]
            rank = titles.index(want) + 1 if want in titles else 0
            stats["all_top1"].append(1.0 if rank == 1 else 0.0)
            stats["all_hit"].append(1.0 if rank else 0.0)
            stats["all_rr"].append(1.0 / rank if rank else 0.0)
            stats[f"{kind}_top1"].append(1.0 if rank == 1 else 0.0)
    return {k2: (sum(v) / len(v) if v else 0.0) for k2, v in stats.items()}


async def test_baseline_is_recorded_and_printed(capsys) -> None:
    """把各档 alpha 的表现打出来，顺便钉住基线。

    跑 `pytest tests/test_retrieval_quality.py -s` 能看到这张表；改了切块或
    排序之后对比它，就不用再靠"搜两下感觉准了"。
    """
    rows = []
    for alpha in (0.0, 0.2, 0.3, 0.5, 0.8, 1.0):
        rows.append((alpha, await _measure(alpha)))

    with capsys.disabled():
        print("\n  alpha   hit@1   hit@5   MRR     字面    语义   （本地哈希向量）")
        for alpha, m in rows:
            print(f"  {alpha:<7.1f} {m['all_top1']:<7.0%} {m['all_hit']:<7.0%} "
                  f"{m['all_rr']:<7.3f} {m['lexical_top1']:<7.0%} {m['semantic_top1']:<7.0%}")

    best = max(r[1]["all_top1"] for r in rows)
    assert best >= 0.5, f"最好的一档 hit@1 只有 {best:.0%}，检索质量出现回归"


async def test_lexical_questions_are_reliably_hit() -> None:
    """字面有重叠的问题是 BM25 的主场，这一类必须稳。掉下来就是切块或排序坏了。"""
    m = await _measure(alpha=0.2)
    assert m["lexical_top1"] >= 0.75, f"字面匹配的 hit@1 只到 {m['lexical_top1']:.0%}"


async def test_the_hashing_embedder_cannot_do_semantics() -> None:
    """钉住现状，不是庆祝它。

    LocalEmbedder 是词频哈希，纯向量（alpha=1）在语义类问题上接近瞎猜。
    这条测试的意义是：换成真 embedding 模型之后它会失败，那时候把阈值调上去
    ——而不是让"其实没有语义检索"这件事一直没人注意到。
    """
    pure_vector = await _measure(alpha=1.0)
    with_keywords = await _measure(alpha=0.2)
    assert with_keywords["all_top1"] >= pure_vector["all_top1"], (
        "关键词那一路不再比纯哈希向量强了——要么换了真 embedder（那就把这条"
        "测试的期望改掉），要么 BM25 那侧坏了"
    )


# --------------------------------------------------------------------------
# 重排
# --------------------------------------------------------------------------
#
# 质量本身没法在测试里量——那要真模型。这里守的是机制：它确实按分数重排了、
# 模型抽风时不会把检索带崩、以及不会凭空吞掉候选。


class _Scorer:
    """按给定分数作答的假模型。"""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    async def ainvoke(self, prompt):
        self.prompts.append(str(prompt))
        return self._reply


def _hits(n: int) -> list[dict]:
    return [{"chunk_id": f"c{i}", "title": f"文档{i}", "content": f"内容 {i}",
             "ordinal": 0, "score": 1.0 - i * 0.1} for i in range(n)]


async def test_rerank_reorders_by_model_score() -> None:
    from app.memory.rerank import rerank

    model = _Scorer('{"scores": [1, 9, 3]}')
    out = await rerank(model, "问题", _hits(3), top_n=3)
    assert [h["chunk_id"] for h in out] == ["c1", "c2", "c0"]
    assert out[0]["rerank_score"] == 9


async def test_rerank_narrows_to_top_n() -> None:
    """初筛捞 20 条、最后只要 5 条，多出来的是给重排挑的余地，不该漏出去。"""
    from app.memory.rerank import rerank

    model = _Scorer('{"scores": [%s]}' % ", ".join(str(i) for i in range(20)))
    out = await rerank(model, "问题", _hits(20), top_n=5)
    assert len(out) == 5
    assert [h["chunk_id"] for h in out] == ["c19", "c18", "c17", "c16", "c15"]


async def test_the_prompt_carries_the_candidates_and_the_question() -> None:
    from app.memory.rerank import rerank

    model = _Scorer('{"scores": [5, 5]}')
    await rerank(model, "谁能改权限", _hits(2), top_n=2)
    assert "谁能改权限" in model.prompts[0]
    assert "内容 0" in model.prompts[0] and "内容 1" in model.prompts[0]


async def test_a_broken_reply_falls_back_to_the_original_order() -> None:
    """重排是增强。模型抽风不该让整个检索挂掉，也不该按半截数据乱排。"""
    from app.memory.rerank import rerank

    for reply in ('完全不是 JSON', '{"scores": [1, 2]}', '{"scores": ["高", "低", "中"]}'):
        notes: list[str] = []
        out = await rerank(_Scorer(reply), "问题", _hits(3), top_n=3, on_note=notes.append)
        assert [h["chunk_id"] for h in out] == ["c0", "c1", "c2"], reply
        assert notes, f"退回原顺序也要说一声：{reply}"


async def test_a_model_that_raises_does_not_break_retrieval() -> None:
    from app.memory.rerank import rerank

    class _Broken:
        async def ainvoke(self, _prompt):
            raise RuntimeError("额度用完了")

    notes: list[str] = []
    out = await rerank(_Broken(), "问题", _hits(3), top_n=2, on_note=notes.append)
    assert len(out) == 2 and out[0]["chunk_id"] == "c0"
    assert "额度用完了" in notes[0]


async def test_no_model_means_no_rerank() -> None:
    from app.memory.rerank import rerank

    out = await rerank(None, "问题", _hits(4), top_n=2)
    assert [h["chunk_id"] for h in out] == ["c0", "c1"]


# --------------------------------------------------------------------------
# alpha 跟着 embedder 走
# --------------------------------------------------------------------------


def test_a_hashing_embedder_gets_no_vector_weight() -> None:
    """实测：本地哈希向量下 alpha=0 的 hit@1 是 88%，alpha>0 掉到 81%。

    那一路不是"弱一点"，是纯噪音——写死 0.5 等于主动把检索做差。
    """
    emb.configure("local")
    assert emb.has_semantics() is False
    assert emb.default_alpha() == 0.0


async def test_search_without_an_explicit_alpha_uses_the_default() -> None:
    m_default = await _measure_default()
    m_zero = await _measure(alpha=0.0)
    assert m_default["all_top1"] == m_zero["all_top1"]


async def _measure_default(k: int = 5) -> dict[str, float]:
    """不传 alpha，让 kb.search 自己决定。"""
    hit1, hit, rr = [], [], []
    async with SessionLocal() as session:
        for question, want, _kind in QUESTIONS:
            hits = await kb.search(session, collection=COLLECTION, query=question, limit=k)
            titles = [h["title"] for h in hits]
            rank = titles.index(want) + 1 if want in titles else 0
            hit1.append(1.0 if rank == 1 else 0.0)
            hit.append(1.0 if rank else 0.0)
            rr.append(1.0 / rank if rank else 0.0)
    n = len(hit1)
    return {"all_top1": sum(hit1) / n, "all_hit": sum(hit) / n, "all_rr": sum(rr) / n}


# --------------------------------------------------------------------------
# 倒排索引：必须和全表扫给出同一个名次
# --------------------------------------------------------------------------
#
# 这是换索引最要紧的一条。一个更快但排序不同的检索，等于悄悄改了所有人的
# 检索结果——而且因为"看起来还是有结果"，没人会发现。


async def _brute_force(query: str, k: int) -> list[str]:
    """绕开倒排，按原来的方式全表扫 + 现建 BM25。"""
    from sqlalchemy import select

    from app.db.models import Chunk
    from app.memory.embeddings import embed_text, from_blob, hybrid_rank

    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(Chunk).where(Chunk.collection == COLLECTION))).scalars())
        # 和 kb.search 一样按 id 定序：分数并列时排序是稳定的，输入顺序不同
        # 就会给出不同的名次。参照实现也得是确定的，否则这条比对时灵时不灵
        rows.sort(key=lambda r: r.id)
        qv = await embed_text(query)
        ranked = hybrid_rank(query, [r.content for r in rows],
                             [from_blob(r.embedding, emb.embedder_dim()) for r in rows],
                             qv, alpha=emb.default_alpha())
    return [rows[i].meta.get("title", "") for i, _s, _p in ranked[:k]]


async def test_the_index_ranks_the_same_as_a_full_scan() -> None:
    """快是次要的，一致才是前提。"""
    async with SessionLocal() as session:
        for question, _want, _kind in QUESTIONS:
            fast = [h["title"] for h in await kb.search(
                session, collection=COLLECTION, query=question, limit=5)]
            slow = await _brute_force(question, 5)
            assert fast == slow, f"「{question}」倒排给的是 {fast}，全表扫给的是 {slow}"


async def test_the_index_actually_narrows_the_candidates() -> None:
    """不收窄的话这一整套就白做了——查询词只该捞出含有它们的那些片段。"""
    from app.memory import inverted

    async with SessionLocal() as session:
        picked = await inverted.candidates(
            session, collection=COLLECTION, query="会话多久过期", limit=100)
        assert picked, "倒排一条都没捞到"
        assert len(picked) < len(DOCS), (
            f"捞了 {len(picked)} 条，语料才 {len(DOCS)} 篇——等于没收窄")


async def test_chunks_without_an_index_fall_back_to_the_full_scan() -> None:
    """存量数据升级上来时倒排是空的。宁可慢，也不要因为索引没建好就说"没搜到"。"""
    from sqlalchemy import select

    from app.db.models import Chunk
    from app.memory import inverted

    async with SessionLocal() as session:
        ids = list((await session.execute(
            select(Chunk.id).where(Chunk.collection == COLLECTION))).scalars())
        await inverted.drop_chunk_terms(session, ids)
        await session.commit()

        hits = await kb.search(session, collection=COLLECTION, query="会话多久过期", limit=3)
        assert hits, "倒排被清空后不该搜不到东西"
        assert hits[0]["title"] == "登录与会话"

        # 重建回来，别影响后面的用例
        assert await inverted.rebuild(session, COLLECTION) == len(DOCS)


async def test_deleting_a_document_takes_its_index_rows() -> None:
    """倒排不跟着删的话，它会一直把已经不存在的片段捞出来。"""
    from sqlalchemy import func, select

    from app.db.models import ChunkTerm, Document

    async with SessionLocal() as session:
        doc = await kb.ingest_document(
            session, collection="idx_del", title="临时", content="一段会被删掉的内容。")
        before = int((await session.execute(select(func.count(ChunkTerm.id)).where(
            ChunkTerm.collection == "idx_del"))).scalar_one())
        assert before > 0

        await kb.delete_document(session, doc.id)
        after = int((await session.execute(select(func.count(ChunkTerm.id)).where(
            ChunkTerm.collection == "idx_del"))).scalar_one())
    assert after == 0, f"文档删了但倒排还留着 {after} 行"
