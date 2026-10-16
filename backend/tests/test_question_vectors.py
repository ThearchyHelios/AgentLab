"""问题聚类的向量只取一次（app/chat/questions.py 模块说明「向量只取一次」）。

评测页「口径命中与重复度」一次打开要按窗口聚好几遍问题，页面另调账本统计；配了远程向量模型时每遍都把几百句问题重发一次。
这里守进程里那一份问题原文 → 向量：

- 同一句话只取一次，一次里重复的问法也只发一句；顺序、对应关系照旧；
- 换了向量模型（或取向量的函数）就整份丢掉，不把两个向量空间的坐标混着比；取的这一会儿换了的，这一批照用、不留；
- 最多留 VECTOR_CACHE_MAX 句，最近用到的留着；一次超过上限也照样每句拿到自己的向量。

问题都是虚构的。
"""
from __future__ import annotations

import pytest

from app.chat import questions as qmod
from app.chat.questions import Question, cluster_questions
from app.memory import embeddings


@pytest.fixture(autouse=True)
def fresh_cache():
    qmod._VECTORS.clear()
    qmod._VECTORS_OF = None
    yield
    qmod._VECTORS.clear()
    qmod._VECTORS_OF = None


DIM = 32


def _fake(calls: list[list[str]], *, scale: float = 1.0, made: dict[str, list[float]] | None = None):
    """每句话一个确定的向量：「同类」开头的几句彼此相同（聚得到一起），别的各占一个方向（互不相像）。made 记下给过的向量。"""
    seen: dict[str, int] = {}

    async def embed(texts):
        calls.append(list(texts))
        out = []
        for t in texts:
            vec = [0.0] * DIM
            vec[0 if t.startswith("同类") else 1 + seen.setdefault(t, len(seen))] = scale
            out.append(vec)
            if made is not None:
                made[t] = vec
        return out

    return embed


def _qs(*texts: str) -> list[Question]:
    return [Question(text=t, at=None) for t in texts]


async def test_each_text_is_embedded_once_and_duplicates_in_one_call_are_sent_once(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(embeddings, "embed_texts", _fake(calls))
    got = await cluster_questions(_qs("同类：上周销售额", "同类：上周销售额", "别的：库存", "同类：上周销售额"))
    assert calls == [["同类：上周销售额", "别的：库存"]], "一次里重复的问法只发一句"
    assert [c.count for c in got] == [3, 1]
    again = await cluster_questions(_qs("别的：库存", "同类：上周销售额", "新的：退款率"))
    assert calls[1:] == [["新的：退款率"]], "已经取过的不再取"
    assert [c.count for c in again] == [1, 1, 1]


async def test_a_new_embedder_drops_every_vector(monkeypatch):
    first: list[list[str]] = []
    second: list[list[str]] = []
    monkeypatch.setattr(embeddings, "embed_texts", _fake(first))
    await cluster_questions(_qs("同类：本月退款率", "别的：毛利"))
    monkeypatch.setattr(embeddings, "embed_texts", _fake(second, scale=2.0))
    await cluster_questions(_qs("同类：本月退款率", "别的：毛利"))
    assert second == [["同类：本月退款率", "别的：毛利"]], "换了取向量的函数：整份重取"
    assert qmod._VECTORS["同类：本月退款率"][0] == 2.0

    # 设置页换了向量模型（configure 换了 embedder 对象）同理
    other = embeddings.LocalEmbedder()
    monkeypatch.setattr(embeddings, "get_embedder", lambda: other)
    await cluster_questions(_qs("同类：本月退款率"))
    assert second[1:] == [["同类：本月退款率"]]


async def test_switching_embedder_mid_call_keeps_the_batch_but_not_the_cache(monkeypatch):
    calls: list[list[str]] = []
    later: list[list[str]] = []

    async def switching(texts):
        calls.append(list(texts))
        # 取的这一会儿设置页换了向量模型
        monkeypatch.setattr(embeddings, "embed_texts", _fake(later))
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    monkeypatch.setattr(embeddings, "embed_texts", switching)
    got = await cluster_questions(_qs("同类：华东门店", "同类：华北门店"))
    assert [c.count for c in got] == [2], "这一批照用旧向量算完"
    assert not qmod._VECTORS, "旧向量不留给下一次"
    await cluster_questions(_qs("同类：华东门店"))
    assert later == [["同类：华东门店"]]


async def test_the_cache_is_bounded_and_keeps_the_recently_used(monkeypatch):
    calls: list[list[str]] = []
    made: dict[str, list[float]] = {}
    monkeypatch.setattr(embeddings, "embed_texts", _fake(calls, made=made))
    monkeypatch.setattr(qmod, "VECTOR_CACHE_MAX", 3)
    texts = [f"别的：第{i}问" for i in range(5)]
    got = await qmod._vectors(texts)
    assert got == [made[t] for t in texts] and len({tuple(v) for v in got}) == 5, "一次超过上限也每句拿到自己的向量"
    assert list(qmod._VECTORS) == texts[2:], "只留最近的 3 句"
    await qmod._vectors([texts[2]])
    await qmod._vectors(["别的：又一问"])
    assert list(qmod._VECTORS) == [texts[4], texts[2], "别的：又一问"], "最近用到的留着，最久没用的先丢"


async def test_two_callers_at_once_send_the_batch_once(monkeypatch):
    """页面并行取 /api/evals/signals 和 /api/ledger/stats：同一批问题不各发一遍。"""
    import asyncio

    calls: list[list[str]] = []
    inner = _fake(calls)

    async def slow(texts):
        await asyncio.sleep(0.02)
        return await inner(texts)

    monkeypatch.setattr(embeddings, "embed_texts", slow)
    batch = _qs("同类：上周客单价", "别的：会员数")
    a, b = await asyncio.gather(cluster_questions(batch), cluster_questions(batch))
    assert calls == [["同类：上周客单价", "别的：会员数"]]
    assert [c.count for c in a] == [c.count for c in b] == [1, 1]
