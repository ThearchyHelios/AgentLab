"""问数据的一轮在服务端跑（docs/design/orchestration-round/00-总方案.md §1）：这一轮的后台任务和它的事件日志。

由 app/db/models.py 末尾整模块导入，create_all 建表（都是新表，不改已有表）。这里不导入 app.db.models：models.py 导入本模块时
自己还没加载完，外键一律按表名写。

**为什么一轮一行任务、id 就是 conversation_turns 的 id。** 一轮问数据（建图 → 运行 → 复核 → 按复核重来、落库）以前是浏览器在推，
页面一断这一轮就没人管了。现在它是服务端的一个后台任务：发起、重试本轮、重新运行本轮、继续运行都还是「这一轮」，用户看到的
是同一轮里的几次尝试，所以任务按轮记、一轮一行，重试时把这一行重新排队（上一次的结局归档进 conversation_turns.meta.attempts）。

- ChatTurnJob：状态用后台任务的状态词（core/job_runtime：queued → running → succeeded / failed / cancelled / interrupted），
  stage 是这一轮现在在哪一步（build 建图、run 运行、waiting 停在审批上等人、review 复核），run_id 是当前这次运行。request 是
  这次要做什么（问题、限定的数据源、方式、重试的原因、放宽到几步……），principal 是发起的人（身份快照：服务重启后接着做时
  按这个人的身份——遮罩、只读、对话归属都看它，见 app/chat/job.py）。attempt_seq 是这一次尝试在事件日志里从哪一条开始：
  刷新、重开页面时从这里回放，不必把之前几次尝试的过程整条重放。
- ChatTurnEvent：这一轮的事件日志。每一步（助手的操作、心跳、查看、final、用量、运行 id、复核结论、重来、终局）按 seq
  一条，页面先回放、再接实时，断了按 seq 补齐（GET /api/chat/turns/{id}/stream?after=）。不进运行的封存范围：运行自己的
  事件仍在 run_events，这里只记「这一轮」的事。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id

#: 这一轮现在在哪一步
STAGES = ("build", "run", "waiting", "review")


class ChatTurnJob(Base, TimestampMixin):
    """问数据一轮的后台任务（见模块说明）。"""

    __tablename__ = "chat_turn_jobs"

    #: 就是 conversation_turns.id
    id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.id", ondelete="CASCADE"), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    #: queued | running | succeeded | failed | cancelled | interrupted（core/job_runtime 的状态词）
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str | None] = mapped_column(String(20), default=None)
    #: ask | retry | rerun | max_steps | run | continue | resume（app/chat/job.py 的 MODES）
    mode: Mapped[str] = mapped_column(String(20), default="ask")
    run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    #: 这一次尝试里自动重来了几次（从 0 数，同 chat/turn.MAX_ATTEMPTS 的口径）
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict[str, Any]] = mapped_column(default=dict)
    principal: Mapped[dict[str, Any]] = mapped_column(default=dict)
    actor: Mapped[str | None] = mapped_column(String(100), default=None)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    attempt_seq: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class ChatTurnEvent(Base):
    """问数据一轮的事件日志里的一条（见模块说明）。"""

    __tablename__ = "chat_turn_events"
    __table_args__ = (UniqueConstraint("turn_id", "seq", name="uq_chat_turn_events_turn_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    ts: Mapped[float] = mapped_column(Float)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)


__all__ = ["STAGES", "ChatTurnEvent", "ChatTurnJob"]
