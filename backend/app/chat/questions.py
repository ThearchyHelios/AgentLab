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
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

#: 缺省的相似度阈值（同治理接口一直以来的 0.62）
DEFAULT_THRESHOLD = 0.62


@dataclass(frozen=True)
class Question:
    """问过的一个问题。turn_id 是问数据的那一轮（别的探索运行没有）；run_id 是它挂着的运行（只回了一句的轮次没有）。"""

    text: str
    at: datetime | None
    run_id: str | None = None
    turn_id: str | None = None
    workflow: str | None = None
    origin: str | None = None


@dataclass
class QuestionCluster:
    """一类问题：members 按时间从新到旧，第一个是种子。"""

    members: list[Question] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.members)


async def recent_questions(session: AsyncSession, principal: Any, *, limit: int) -> list[Question]:
    """最近问过的问题，从新到旧，最多 limit 条（取法见模块开头）。principal 用来去掉这个人读不了的只给拥有者的运行。"""
    from app.auth.access import hidden_runs
    from app.db.models import Conversation, ConversationTurn, Run
    from app.engine.origin import counted_runs

    hidden = hidden_runs(principal)
    turns = (await session.execute(
        select(ConversationTurn.id, ConversationTurn.question, ConversationTurn.created_at, ConversationTurn.run_id,
               Run.workflow_name)
        .join(Conversation, Conversation.id == ConversationTurn.conversation_id)
        .outerjoin(Run, Run.id == ConversationTurn.run_id)
        .where(Conversation.kind == "chat", func.trim(ConversationTurn.question) != "", counted_runs(), not_(hidden))
        .order_by(ConversationTurn.created_at.desc()).limit(limit)
    )).all()
    runs = (await session.execute(
        select(Run.id, Run.input, Run.workflow_name, Run.created_at, Run.origin_kind)
        .where(Run.run_class != "formal", func.coalesce(Run.origin_kind, "") != "chat", counted_runs(), not_(hidden),
               func.json_type(Run.input, "$.question") == "text")
        .order_by(Run.created_at.desc()).limit(limit)
    )).all()
    out = [Question(text=question.strip(), at=at, run_id=run_id, turn_id=turn_id, workflow=workflow, origin="chat")
           for turn_id, question, at, run_id, workflow in turns]
    for run_id, payload, workflow, at, origin in runs:
        text = (payload or {}).get("question") if isinstance(payload, dict) else None
        if isinstance(text, str) and text.strip():
            out.append(Question(text=text.strip(), at=at, run_id=run_id, workflow=workflow, origin=origin))
    out.sort(key=lambda q: q.at.timestamp() if q.at is not None else 0.0, reverse=True)
    return out[:limit]


async def cluster_questions(questions: Sequence[Question], *, threshold: float = DEFAULT_THRESHOLD,
                            ) -> list[QuestionCluster]:
    """向量贪心聚类（顺序同 questions：从新到旧）。"""
    from app.memory import embeddings

    if not questions:
        return []
    vectors = await embeddings.embed_texts([q.text for q in questions])
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


__all__ = ["DEFAULT_THRESHOLD", "Question", "QuestionCluster", "cluster_questions", "embedder", "recent_questions"]
