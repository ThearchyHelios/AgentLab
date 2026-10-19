"""问过的问题和它们的聚类：全站只有这一份（docs/design/next-round/00-总方案.md X2）。

同一个问题被反复问，就该长成模板里的固定一节、沉淀成口径——探索问题聚类（/governance/exploratory-clusters）、以后口径
账本的「可沉淀的问题」、评测的问题重复度（E3）量的都是这件事，各算一份的话几个数对不上，所以取问题和聚类只写在这里。

**取什么。** 只取问题原文，不取整份输入：以前把运行的整份 input（含 history）拿去聚，同一段对话里前几轮的问答会把不同的
问题聚成一类，重复度偏高。

- 问数据：一轮一条（conversation_turns.question，用户原本问的那句话）。同一轮里的自动重来、用户点的「重试」只是换了这一轮
  挂着的运行，不多出一条——按运行数的话，答坏了自动重来一次就被算成问了两遍。没挂运行的轮次（只回了一句、助手反问）
  也算：问题照样被问过。
- 别的探索运行（画布试跑、蓝图试运行、接口……）：输入里有 question 这个字段的取它；没有的不收（输入里别的字段不是问题）。
  问数据的运行由上面一轮一条代表，这里不再数。

**不收什么。** 系统派生的运行（评测、口径对账、口径召回：engine/origin.counted_runs）不收，评测题不能把聚类刷高；
这个人读不了的只给拥有者的运行（auth/access.hidden_runs）不收；正式运行不收（探索才谈得上「沉淀」）。

聚类用 memory/embeddings 的向量贪心聚：按时间从新到旧，和某一类的种子足够像就并进去，否则自立一类。本地向量是词频
哈希、语义能力弱，结果是下限——调用方据 embedder 照实写明用的是哪个向量模型。

**类的 id 由成员定。** 每个问题有稳定的 id（Question.key：问数据是那一轮，别的探索运行是那次运行），一类的 id
（QuestionCluster.id）是成员的 id 排好之后取的哈希：同一批问题两次读取，类的 id 一样。以前按种子问题的原文算，种子是一类里
最新的那一句，同一批问题时间相同的几句先后不定、来了一句新的问法种子就换了，「不是口径」按 id 记就失效了。同一时刻问的
几句按 id 排出先后（recent_questions），不靠库里读出来的先后。来了新成员 id 照样会变：认证队列按成员记「不是口径」
（ledger/certify），不只按 id。

**向量只取一次。** 同一批问题会被聚很多遍：评测页「口径命中与重复度」一次打开，/api/evals/signals 按窗口调 compute_stats
（缺省 6 个窗口，每个都聚一次），页面另调 /api/ledger/stats；认证队列、治理页的探索问题聚类也各聚一遍。配了远程向量模型时
每聚一遍都把几百句问题重新发一次，页面慢、还花钱。所以问题原文 → 向量在进程里留一份（_vectors：最多 VECTOR_CACHE_MAX 句，
最近用到的留着），同一句话只取一次；一次里重复的问法（聚类量的正是这个）也只发一句。缓存认的是「哪一个 embedder、哪一个取向量
的函数」算的：设置页换了向量模型（memory/embeddings.configure 换了对象）就整份丢掉，不把两个向量空间的坐标混着比。取向量按事件
循环串行：页面并行取的两个接口不会把同一批问题各发一遍。
"""
from __future__ import annotations

import asyncio
import hashlib
import weakref
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

#: 缺省的相似度阈值（同治理接口一直以来的 0.62）
DEFAULT_THRESHOLD = 0.62
#: 进程里最多留几句问题的向量：取问题的地方一次最多 500 句（ledger/stats、治理接口的上限），留两批的量，窗口来回切不用重取
VECTOR_CACHE_MAX = 1000


@dataclass(frozen=True)
class Question:
    """问过的一个问题。turn_id 是问数据的那一轮（别的探索运行没有）；run_id 是它挂着的运行（只回了一句的轮次没有）。"""

    text: str
    at: datetime | None
    run_id: str | None = None
    turn_id: str | None = None
    workflow: str | None = None
    origin: str | None = None

    @property
    def key(self) -> str:
        """稳定的 id：问数据的那一轮（turn:…），别的探索运行是那次运行（run:…）；两样都没有的按原文和时间。"""
        if self.turn_id:
            return f"turn:{self.turn_id}"
        if self.run_id:
            return f"run:{self.run_id}"
        stamp = self.at.isoformat() if self.at is not None else ""
        return "text:" + hashlib.sha256(f"{self.text}\n{stamp}".encode()).hexdigest()[:16]


@dataclass
class QuestionCluster:
    """一类问题：members 按时间从新到旧，第一个是种子。"""

    members: list[Question] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def keys(self) -> list[str]:
        """成员的稳定 id，排好的。"""
        return sorted(q.key for q in self.members)

    @property
    def id(self) -> str:
        """类的 id：成员的稳定 id 排好之后取哈希（见模块说明）。同一批问题两次读取一样。"""
        return hashlib.sha256("\n".join(self.keys).encode()).hexdigest()[:16]


async def recent_questions(session: AsyncSession, principal: Any, *, limit: int) -> list[Question]:
    """最近问过的问题，从新到旧，最多 limit 条（取法见模块开头）。principal 用来去掉这个人读不了的只给拥有者的运行。"""
    from app.auth.access import hidden_runs
    from app.db.models import Conversation, ConversationTurn, Run
    from app.db.portable import json_is_string
    from app.engine.origin import counted_runs

    hidden = hidden_runs(principal)
    turns = (await session.execute(
        select(ConversationTurn.id, ConversationTurn.question, ConversationTurn.created_at, ConversationTurn.run_id,
               Run.workflow_name)
        .join(Conversation, Conversation.id == ConversationTurn.conversation_id)
        .outerjoin(Run, Run.id == ConversationTurn.run_id)
        .where(Conversation.kind == "chat", func.trim(ConversationTurn.question) != "", counted_runs(), not_(hidden))
        .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc()).limit(limit)
    )).all()
    runs = (await session.execute(
        select(Run.id, Run.input, Run.workflow_name, Run.created_at, Run.origin_kind)
        .where(Run.run_class != "formal", func.coalesce(Run.origin_kind, "") != "chat", counted_runs(), not_(hidden),
               json_is_string(Run.input, "question"))
        .order_by(Run.created_at.desc(), Run.id.desc()).limit(limit)
    )).all()
    out = [Question(text=question.strip(), at=at, run_id=run_id, turn_id=turn_id, workflow=workflow, origin="chat")
           for turn_id, question, at, run_id, workflow in turns]
    for run_id, payload, workflow, at, origin in runs:
        text = (payload or {}).get("question") if isinstance(payload, dict) else None
        if isinstance(text, str) and text.strip():
            out.append(Question(text=text.strip(), at=at, run_id=run_id, workflow=workflow, origin=origin))
    # 同一时刻问的几句按稳定的 id 排：库里读出来的先后不定，种子、聚类的结果就跟着变（见模块说明）
    out.sort(key=lambda q: (q.at.timestamp() if q.at is not None else 0.0, q.key), reverse=True)
    return out[:limit]


#: 问题原文 → 向量（最近用到的在后）和它们是谁算的（见模块说明「向量只取一次」）
_VECTORS: "OrderedDict[str, Any]" = OrderedDict()
_VECTORS_OF: tuple[Any, Any] | None = None
_VECTOR_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _vector_lock() -> asyncio.Lock:
    """取向量的锁，按事件循环各一把（asyncio.Lock 不能跨循环用）。"""
    loop = asyncio.get_running_loop()
    lock = _VECTOR_LOCKS.get(loop)
    if lock is None:
        lock = _VECTOR_LOCKS[loop] = asyncio.Lock()
    return lock


def _owner() -> tuple[Any, Any]:
    from app.memory import embeddings

    return embeddings.get_embedder(), embeddings.embed_texts


def _same_owner(a: tuple[Any, Any] | None, b: tuple[Any, Any]) -> bool:
    return a is not None and a[0] is b[0] and a[1] is b[1]


async def _vectors(texts: Sequence[str]) -> list[Any]:
    """这些问题的向量（顺序同 texts）：进程里有的直接用，没有的去重后一次取齐、留下来。"""
    global _VECTORS_OF

    async with _vector_lock():
        owner = _owner()
        if not _same_owner(_VECTORS_OF, owner):
            _VECTORS.clear()
            _VECTORS_OF = owner
        got = {t: _VECTORS[t] for t in texts if t in _VECTORS}
        missing = [t for t in dict.fromkeys(texts) if t not in got]
        if missing:
            fresh = await owner[1](missing)
            got.update(zip(missing, fresh))
            # 取的这一会儿设置页换了向量模型：这一批照用（同一次聚类里坐标是一致的），不留给下一次
            if not _same_owner(_VECTORS_OF, _owner()):
                return [got[t] for t in texts]
            for t in missing:
                _VECTORS[t] = got[t]
        for t in dict.fromkeys(texts):
            _VECTORS.move_to_end(t)
        while len(_VECTORS) > VECTOR_CACHE_MAX:
            _VECTORS.popitem(last=False)
        return [got[t] for t in texts]


async def cluster_questions(questions: Sequence[Question], *, threshold: float = DEFAULT_THRESHOLD,
                            ) -> list[QuestionCluster]:
    """向量贪心聚类（顺序同 questions：从新到旧）。向量取自 _vectors：同一句话在进程里只取一次。"""
    from app.memory import embeddings

    if not questions:
        return []
    vectors = await _vectors([q.text for q in questions])
    clusters: list[tuple[int, QuestionCluster]] = []
    for i, question in enumerate(questions):
        for seed, cluster in clusters:
            if embeddings.cosine(vectors[i], vectors[seed]) >= threshold:
                cluster.members.append(question)
                break
        else:
            clusters.append((i, QuestionCluster(members=[question])))
    return [cluster for _, cluster in clusters]


def embedder() -> str:
    """聚类用的是哪个向量模型（memory/embeddings.embedder_id）：本地哈希的结果只是下限，调用方照实写出来。"""
    from app.memory import embeddings

    return embeddings.embedder_id()


__all__ = ["DEFAULT_THRESHOLD", "VECTOR_CACHE_MAX", "Question", "QuestionCluster", "cluster_questions", "embedder",
           "recent_questions"]
