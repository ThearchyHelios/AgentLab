from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Sequence

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
    """配了 key 就用真 embedding 模型，检索质量明显更好。"""

    name = "openai"

    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None,
                 base_url: str | None = None) -> None:
        from langchain_openai import OpenAIEmbeddings

        self.dim = 1536
        self._impl = OpenAIEmbeddings(
            model=model,
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url,
        )

    async def aembed(self, text: str) -> np.ndarray:
        vec = await self._impl.aembed_query(text)
        return np.array(vec, dtype=np.float32)

    async def aembed_many(self, texts: Sequence[str]) -> np.ndarray:
        vecs = await self._impl.aembed_documents(list(texts))
        return np.array(vecs, dtype=np.float32)


_local = LocalEmbedder()


def get_embedder() -> LocalEmbedder | OpenAIEmbedder:
    """默认走本地；显式打开 AGENTLAB_USE_OPENAI_EMBEDDINGS 且有 key 时才用远端。"""
    if os.environ.get("AGENTLAB_USE_OPENAI_EMBEDDINGS") and os.environ.get("OPENAI_API_KEY"):
        try:
            return OpenAIEmbedder()
        except Exception:  # noqa: BLE001
            return _local
    return _local


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
) -> list[tuple[int, float, dict[str, float]]]:
    """向量相似度和 BM25 各归一化后加权合并。

    纯向量会漏掉精确的专有名词，纯 BM25 又抓不住同义表达，
    混合排序在本地小知识库上稳定优于任何一个单独使用。
    """
    if not texts:
        return []
    bm = BM25(texts)
    kw_scores = bm.score(query)
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
