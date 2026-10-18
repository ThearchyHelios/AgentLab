"""数据事件触发的待派发事件和去抖等待（docs/design/orchestration-round/00-总方案.md §4）。

由 app/db/models.py 末尾整模块导入，create_all 建表（都是新表，只加不改）。这里不导入 app.db.models：models.py 导入本模块时
自己还没加载完，外键一律按表名写。

**为什么要一张「待派发事件」表，而不是在发生处直接发起运行。** 事件发生在别的事务里：启用上传表格的新版本、正式运行的
收尾、记下数据源的变化。在那里直接发起运行，发起失败要不要回滚那次启用？重复发生、服务重启时发起到一半又算什么？说不清。
改成在发生处写一行 TriggerEvent，调度循环一拍取走、按各定时任务的触发条件派发：发起运行的规矩（重叠跳过、正式运行只从
发布版本发起、触发记录唯一）全部沿用定时任务那一套。

什么时候和那件事同一个事务：table.version（切指针那一下）、workflow.finished（写终态那一下）是同一个事务——发生了就一定有
这一行，没发生就一定没有。source.changed 不是：一次探查、剖析的变化分好几批落库，「这一批只触发一次」要等任务结束才知道，
事件在任务结束后另开会话写（data/source_changes.notify_after_job）。中间进程死了、库被锁住写不进，变化记录在、事件没有：
调度的一拍按任务号补写（scheduler/outbox.backfill_source_changes），dedupe_key（source.changed:<任务号>）唯一，补写和正常的
那一次不会记成两条。
调度关着（AGENTLAB_SCHEDULER）时没人取，事件留在表里，接口照实说有几条在等。第 2 波多进程时，取走靠的是条件 UPDATE，
多个进程也只有一个取得到。

- TriggerEvent 一次事件：种类（table.version / workflow.finished / source.changed）、对象 id、给模板用的事件内容、触发链
  的深度和经过的运行、发生时刻、去重键（只有 source.changed 用）。status：pending 还没派发；routed 已经按触发条件派给了
  matched 个定时任务（0 个也算派完）。
- TriggerDelivery 一个事件派给一个定时任务、等着去抖：同一个定时任务窗口内（第一条事件发生之后 debounce_s 秒）的几条
  合成一次触发，fire_id 是那次触发记录；status：pending 等着、fired 合进了某次触发（发起、跳过、未能发起都算）、dropped
  定时任务在等待期间被停用或改了触发条件，不再派发。(定时任务, 事件) 唯一：同一个事件不会派给同一个任务两次。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, new_id, utcnow

#: 事件种类：上传表格启用了新的一期、工作流的正式运行成功结束、数据源出现要处理的变化
EVENT_KINDS = ("table.version", "workflow.finished", "source.changed")
#: 事件的状态：还没派发、已经派发（派给了 matched 个定时任务）
EVENT_STATUSES = ("pending", "routed")
#: 派给一个定时任务之后的状态：等着去抖、合进了一次触发、不再派发
DELIVERY_STATUSES = ("pending", "fired", "dropped")


class TriggerEvent(Base):
    """一次待派发的事件。payload 只放给输入模板用的值（{{ event.* }}，见 scheduler/triggers.py），workflow.finished 的
    出口字段不复制进来：派发时按 run_id 读那次运行的成果（运行记录已经落库、不会再变）。"""

    __tablename__ = "trigger_events"
    __table_args__ = (
        Index("ix_trigger_events_status", "status", "created_at"),
        Index("ix_trigger_events_subject", "event", "subject_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    event: Mapped[str] = mapped_column(String(30))
    #: 对象 id：表格数据源 id（table.version）、工作流 id（workflow.finished）、数据源 id（source.changed）
    subject_id: Mapped[str] = mapped_column(String(32), default="")
    #: 对象当时的名字（给人看的说法里用；对象之后改名、删除都不影响这一条）
    subject_name: Mapped[str] = mapped_column(String(200), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: 触发链的深度：workflow.finished 时是上游那次运行自己由事件触发了几层（不是事件触发的为 0），别的种类为 0。
    #: 派发时新运行的深度是它加 1，超过上限（scheduler/triggers.MAX_DEPTH）不发起
    depth: Mapped[int] = mapped_column(Integer, default=0)
    #: 经过的运行 id（从最上游起），给记录、排查循环用；只截最近的几条
    chain: Mapped[list[Any]] = mapped_column(default=list)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    routed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    #: 派给了几个定时任务
    matched: Mapped[int] = mapped_column(Integer, default=0)
    #: 去重键：同一件事只记一条。source.changed 是 source.changed:<任务号>（任务结束时记、没记上的补写，两边撞上唯一约束）；
    #: 别的种类为空（它们和那件事同一个事务，不会记两遍）
    dedupe_key: Mapped[str | None] = mapped_column(String(80), default=None, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TriggerDelivery(Base):
    """一个事件派给了一个定时任务，等着去抖、合成一次触发。"""

    __tablename__ = "trigger_deliveries"
    __table_args__ = (
        UniqueConstraint("schedule_id", "event_id"),
        Index("ix_trigger_deliveries_waiting", "schedule_id", "status", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(ForeignKey("trigger_events.id", ondelete="CASCADE"))
    #: 同事件的发生时刻（去抖窗口按它算）
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    #: 合进的那次触发（schedule_fires.id）
    fire_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
