"""多进程协调用的表（app/cluster，docs/design/orchestration-round/00-总方案.md §5 W2-b）。

由 app/db/models.py 末尾整模块导入，create_all 建表（都是新表）。认领用的 owner 列加在各自的表上（runs、chat_turn_jobs、
各种后台任务表……，见 db/base 的迁移），这里只放协调本身的几张：

- ClusterWorker：一个工作进程一行（id 是 app/cluster/identity.OWNER：主机 + pid + 启动随机串）。进程活着就隔一拍心跳续租约
  （lease_until）；租约过了期就算这个进程没了，它认领的行由别的进程按各自的规矩接手。各行的认领只记 owner，不各自续租：
  租约按进程算，一个进程一拍只写一行，进程一没，它认领的全部一起到期。
- ClusterLease：全站只许一个进程做的事（调度当班）的租约：name 一行，owner 是当班的进程，epoch 每换一次当班加一。
- ClusterRequest：发给别的进程的请求（停止它在跑的运行、问数据、后台任务；往它在执行的运行里记一条事件）。owner 是收件的
  进程：它隔一拍心跳（PostgreSQL 上另有 NOTIFY 叫醒）取走自己的请求照办，办完删掉。
- RunSlot：运行的并发名额（app/cluster/slots.py）：一次运行每段执行要一个名额，按类别（交互、应用、定时、系统）各有全站上限
  和优先级，按库里的计数发。
- LaneTicket：同源队（core/job_runtime 的 SourceLanes）：同一个数据源上的起草、剖析、探查、同步起草、导入先来先到、一个
  接一个；id 自增，先来的号小。
- RateCounter：几个进程共用的计数（访问密钥每分钟限流的令牌桶、登录失败的退避），到期的由当班进程清掉。只在 PostgreSQL
  （可以多进程）时用；SQLite 只有一个进程，计数照旧在进程里（app/cluster/counters.py）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, new_id, utcnow


class ClusterWorker(Base):
    __tablename__ = "cluster_workers"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    host: Mapped[str] = mapped_column(String(255), default="")
    pid: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    lease_until: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    #: 版本、数据目录之类给人看的信息
    info: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ClusterLease(Base):
    __tablename__ = "cluster_leases"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(120), default=None)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    acquired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    epoch: Mapped[int] = mapped_column(Integer, default=0)


class ClusterRequest(Base):
    __tablename__ = "cluster_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    #: 收件的进程
    owner: Mapped[str] = mapped_column(String(120), index=True)
    #: stop 停止 | note 往运行里记一条事件
    kind: Mapped[str] = mapped_column(String(20))
    #: 对什么：run | chat_turn | 后台任务的种类（draft、profile、introspect、eval、shadow_draft）
    target: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str | None] = mapped_column(String(100), default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RunSlot(Base):
    __tablename__ = "run_slots"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: interactive | app | schedule | system（app/cluster/slots.CLASSES）
    klass: Mapped[str] = mapped_column(String(20))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str] = mapped_column(String(120), index=True)
    granted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    granted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class LaneTicket(Base):
    __tablename__ = "lane_tickets"
    __table_args__ = (Index("ix_lane_tickets_source_id_id", "source_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64))
    #: 登记过的一种事（core/job_runtime.register_lane_kind）
    kind: Mapped[str] = mapped_column(String(40))
    job_id: Mapped[str | None] = mapped_column(String(64), default=None)
    owner: Mapped[str] = mapped_column(String(120), index=True)
    granted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RateCounter(Base):
    __tablename__ = "rate_counters"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    #: 令牌桶里还剩几个（访问密钥限流）/ 要等到什么时刻（登录失败，Unix 秒）
    value: Mapped[float] = mapped_column(Float, default=0.0)
    #: 上次补令牌的时刻（Unix 秒）
    at: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class CacheEvent(Base):
    """「某个进程里缓存着的东西变了」（app/cluster/caches.py）：改的进程写一条，别的进程隔一拍心跳（或收到 NOTIFY）按 id
    往后读，照着清掉、重读自己那一份。id 自增；当班进程清掉一个钟头以前的。"""

    __tablename__ = "cache_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    key: Mapped[str | None] = mapped_column(String(255), default=None)
    owner: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
