from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any, Sequence

import numpy as np

_DIM = 512
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]")


def _tokens(text: str) -> list[str]:
    """英文按词、中文按字切，再补 2-gram。

    中文没有空格分词，只用单字召回太糊；加上相邻二字组合能显著提升短语匹配。
    """
    raw = _TOKEN_RE.findall(text.lower())
    grams = [a + b for a, b in zip(raw, raw[1:]) if len(a) == 1 and len(b) == 1]
    return raw + grams


def _hash_index(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big") % _DIM


class LocalEmbedder:
    """特征哈希 + TF 的本地向量器。

    不联网、不下模型、零配置就能用。语义泛化能力当然不如真 embedding 模型，
    所以检索侧用它和 BM25 做混合，把关键词精确匹配的能力补回来。
    """

    dim = _DIM
    name = "local-hashing"

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(_DIM, dtype=np.float32)
        toks = _tokens(text)
        if not toks:
            return vec
        for tok in toks:
            vec[_hash_index(tok)] += 1.0
        # 次线性缩放，压掉高频词的统治力
        vec = np.sign(vec) * np.log1p(np.abs(vec))
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def embed_many(self, texts: Sequence[str]) -> np.ndarray:
        return np.vstack([self.embed(t) for t in texts]) if texts else np.zeros((0, _DIM), np.float32)


class OpenAIEmbedder:
    """任何讲 OpenAI /v1/embeddings 的服务：官方 API、也可以是本地起的
    LM Studio / Ollama / vLLM / TEI。区别只在 base_url。"""

    name = "openai"

    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None,
                 base_url: str | None = None) -> None:
        from langchain_openai import OpenAIEmbeddings

        self.model = model
        self.base_url = base_url
        kwargs: dict[str, Any] = {
            "model": model,
            # 本地服务通常不校验 key，但 SDK 要求非空
            "api_key": api_key or os.environ.get("OPENAI_API_KEY") or "not-needed",
            "base_url": base_url,
            # 远端接口单次批量有上限（OpenAI 是 2048），langchain 按这个分块发
            "chunk_size": 512,
        }
        if base_url:
            # langchain 默认先把文本分词、发 token 数组过去——OpenAI 官方认，
            # 但 LM Studio 这类本地服务只收字符串，会报
            # "'input' field must be a string or an array of strings"。
            # 关掉之后发原文。我们的片段都在 800 字上下，也用不着那套长度保护
            kwargs["check_embedding_ctx_length"] = False
        self._impl = OpenAIEmbeddings(**kwargs)
        # 维度必须问出来，不能按模型名猜。本地部署的模型五花八门——
        # bge-m3 是 1024、Qwen3-Embedding-4B 是 2560，猜错的后果是所有存量
        # 向量都被判成"和当前 embedder 对不上"，检索整体退回关键词
        self.dim = len(self._impl.embed_query("dim probe"))

    async def aembed(self, text: str) -> np.ndarray:
        vec = await self._impl.aembed_query(text)
        return np.array(vec, dtype=np.float32)

    async def aembed_many(self, texts: Sequence[str]) -> np.ndarray:
        vecs = await self._impl.aembed_documents(list(texts))
        return np.array(vecs, dtype=np.float32)


_local = LocalEmbedder()

#: 设置表里存 embedding 配置的键。和 Copilot 模型同一套路子
EMBEDDING_SETTING_KEY = "embedding"

#: 进程内缓存。get_embedder 在检索热路径上被反复调用，不该每次都去读库
_cached: tuple[str, Any] | None = None

#: 保存的配置为什么没生效。启动时本机的模型服务还没起就会这样：整个进程一直用
#: 本地哈希，服务后来起了也不会自己连上。以前只在服务端日志里留一句，界面上看着
#: 一切正常，检索却一直是纯关键词
_unavailable: str | None = None


class EmbedderUnavailable(RuntimeError):
    """配了远端 embedder 但建不起来。message 直接给用户看。"""


def _make(kind: str, model: str, base_url: str = "", *,
          strict: bool) -> "LocalEmbedder | OpenAIEmbedder":
    if kind != "openai":
        return _local
    try:
        return OpenAIEmbedder(model=model or "text-embedding-3-small",
                              base_url=base_url or None)
    except Exception as e:  # noqa: BLE001
        if strict:
            # 人明确选了远端就不能偷偷退回本地：他会以为自己在用语义检索，
            # 而实际拿到的是哈希词袋——搜不出同义表达时完全无从怀疑到这里
            where = base_url or "api.openai.com"
            raise EmbedderUnavailable(
                f"用不了 {where} 上的 embedding 模型 "
                f"{model or 'text-embedding-3-small'}：{_why(e)}。"
                f"检查服务在不在、模型名对不对；走官方接口还要看 OPENAI_API_KEY。"
            ) from e
        return _local


def configure(kind: str, model: str = "", base_url: str = "") -> None:
    """切换当前 embedder。设置页保存后调用，也用于测试。建不起来会抛。"""
    global _cached, _unavailable
    _cached = (f"{kind}:{model}:{base_url}", _make(kind, model, base_url, strict=True))
    _unavailable = None


def unavailable_reason() -> str | None:
    """保存的配置没能生效的原因；生效了（或还没试过）是 None。"""
    return _unavailable


def get_embedder() -> "LocalEmbedder | OpenAIEmbedder":
    """当前 embedder。

    优先级：进程内已配置 > 环境变量兜底 > 本地。设置页写库后会调 configure，
    所以这里不再每次读库——检索热路径上一次查询要走几百次。

    以前这里只认 AGENTLAB_USE_OPENAI_EMBEDDINGS 一个环境变量：没有界面、
    没人会发现，于是所有人都在用那个没有语义能力的哈希向量，而 alpha 默认
    0.5 还给了它一半权重。
    """
    if _cached is not None:
        return _cached[1]
    if os.environ.get("AGENTLAB_USE_OPENAI_EMBEDDINGS") and os.environ.get("OPENAI_API_KEY"):
        return _make("openai", "", strict=False)
    return _local


def embedder_id() -> str:
    """当前 embedder 的身份。存进 chunk 那一列，检索时拿它判断存量向量还能不能用。"""
    emb = get_embedder()
    name = getattr(emb, "name", "unknown")
    model = getattr(emb, "model", "")
    return f"{name}:{model}" if model else name


def embedder_dim() -> int:
    return int(getattr(get_embedder(), "dim", _DIM))


def has_semantics() -> bool:
    """当前 embedder 有没有语义泛化能力。

    LocalEmbedder 是词频哈希：它和 BM25 吃同一批词，给它权重等于把关键词
    信号打个折再加回自己身上，同义改写一条都召不回。
    """
    return getattr(get_embedder(), "name", "") != "local-hashing"


def _why(e: BaseException) -> str:
    from app.api.errors import explain

    return explain(e)[0]


def default_alpha() -> float:
    """向量那一路默认占多少权重。

    实测（backend/tests/test_retrieval_quality.py，24 篇语料 / 16 问）：
    本地哈希向量下 alpha=0 的 hit@1 是 88%，alpha 一旦大于 0 就掉到 81%
    ——那一路不是"弱一点"，是纯噪音。所以没有语义能力时默认给 0。
    """
    return 0.5 if has_semantics() else 0.0


def usable(embed_model: str | None, embed_dim: int | None) -> bool:
    """这条存量向量还能不能和当前查询比对。

    维度对不上 cosine 会直接抛 ValueError（shapes (1536,) and (512,) not
    aligned），所以这不是"质量差一点"，是会 500。模型名也要比：同维度不同
    模型的向量空间没有可比性，算出来的相似度是随机数。

    存量数据这两列是空的（迁移回填的默认值），一律当成不可用——保守，
    但总比拿旧模型的向量冒充当前空间里的坐标强。
    """
    return bool(embed_model) and embed_model == embedder_id() and embed_dim == embedder_dim()


async def load_setting(session: Any) -> None:
    """从库里读 embedding 配置并生效。应用启动时调一次。"""
    from app.db.models import Setting

    global _unavailable
    row = await session.get(Setting, EMBEDDING_SETTING_KEY)
    saved = (row.value if row else {}) or {}
    kind = saved.get("kind")
    if kind:
        try:
            configure(kind, saved.get("model", ""), saved.get("base_url", ""))
        except EmbedderUnavailable as e:
            _unavailable = str(e)
            raise


async def embed_text(text: str) -> np.ndarray:
    emb = get_embedder()
    if isinstance(emb, OpenAIEmbedder):
        return await emb.aembed(text)
    return emb.embed(text)


async def embed_texts(texts: Sequence[str]) -> np.ndarray:
    emb = get_embedder()
    if isinstance(emb, OpenAIEmbedder):
        return await emb.aembed_many(texts)
    return emb.embed_many(texts)


def to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def from_blob(blob: bytes | None, dim: int | None = None) -> np.ndarray | None:
    if not blob:
        return None
    vec = np.frombuffer(blob, dtype=np.float32)
    if dim and vec.shape[0] != dim:
        return None
    return vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# --------------------------------------------------------------------------
# BM25：关键词侧的一半
# --------------------------------------------------------------------------


class BM25:
    def __init__(self, corpus: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [_tokens(d) for d in corpus]
        self.n = len(self.docs)
        self.avg_len = sum(len(d) for d in self.docs) / self.n if self.n else 0.0
        self.df: dict[str, int] = {}
        for doc in self.docs:
            for tok in set(doc):
                self.df[tok] = self.df.get(tok, 0) + 1

    def score(self, query: str) -> list[float]:
        q_tokens = _tokens(query)
        scores = [0.0] * self.n
        for tok in q_tokens:
            df = self.df.get(tok, 0)
            if df == 0:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for i, doc in enumerate(self.docs):
                tf = doc.count(tok)
                if tf == 0:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * len(doc) / (self.avg_len or 1))
                scores[i] += idf * (tf * (self.k1 + 1)) / denom
        return scores


def hybrid_rank(
    query: str,
    texts: Sequence[str],
    vectors: Sequence[np.ndarray | None],
    query_vec: np.ndarray,
    *,
    alpha: float = 0.5,
    keyword_scores: Sequence[float] | None = None,
) -> list[tuple[int, float, dict[str, float]]]:
    """向量相似度和 BM25 各归一化后加权合并。

    纯向量会漏掉精确的专有名词，纯 BM25 又抓不住同义表达，
    混合排序在本地小知识库上稳定优于任何一个单独使用。

    keyword_scores 传进来就不再自己算 BM25。倒排索引那条路必须传：它按**全集**
    统计（N、avg_len、df）算好了分，而这里只拿到候选子集——在子集上重算，
    df 和平均长度全是另一套数，排出来的名次和全表扫不一样。真踩过，
    test_the_index_ranks_the_same_as_a_full_scan 就是为此写的。
    """
    if not texts:
        return []
    kw_scores = list(keyword_scores) if keyword_scores is not None else BM25(texts).score(query)
    vec_scores = [cosine(query_vec, v) if v is not None else 0.0 for v in vectors]

    def _norm(xs: list[float]) -> list[float]:
        lo, hi = min(xs), max(xs)
        if hi - lo < 1e-9:
            # 全都一样分，说明这一路信号区分不出高下——那是"都一样好"，
            # 不是"都一样差"。返回 0 会让候选只有一条时它的合并分恒为 0，
            # 被 min_score 过滤掉，于是新建 scope 写入第一条后永远搜不到，
            # 而"先写一条、下一轮再 recall"正是 agent 最常见的用法。
            return [1.0 for _ in xs]
        return [(x - lo) / (hi - lo) for x in xs]

    kw_n, vec_n = _norm(kw_scores), _norm(vec_scores)
    ranked = [
        (i, alpha * vec_n[i] + (1 - alpha) * kw_n[i],
         {"vector": vec_scores[i], "keyword": kw_scores[i]})
        for i in range(len(texts))
    ]
    ranked.sort(key=lambda t: t[1], reverse=True)
    return ranked
