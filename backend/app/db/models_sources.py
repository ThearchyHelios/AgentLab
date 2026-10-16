"""数据源的对象索引、探查范围、后台探查任务（docs/design/next-round/03-数据接入与目录维护.md A1）。

由 app/db/models.py 末尾整模块导入，create_all 建表。

**为什么要对象索引。** 以前探查结构每个源最多取 200 个对象、按数据库列出的顺序截断，多出来的表哪儿都看不到，目录、look、
剖析还会把落在窗口外的表说成「表已不在」。现在全库每个对象一行（source_objects，一种方言一条字典查询拿名字、类型、注释，
上限 5 万个），探查范围（数据源 options.introspect_scope 的规则，缺省 500 张、最多 2,000 张）圈出要完整反射的那一部分，
只有范围内的行带着反射到的完整结构（meta）。schema_cache 由 data/source_objects.rebuild_schema_cache 从范围内已反射的行
物化出来，形状不变：消费它的两百多处不用改。

**结构版本。** 每张表一个指纹（反射到的结构的规范 JSON 的哈希）和 struct_version：指纹变了才加 1。第 3 波的结构差异按它
钉版本、出前后差异；这一波只把版本写好，不出变化记录。

**探查任务。** 探查改成后台任务（data/introspect_jobs.py），生命周期是 core/job_runtime 那一套：和同源的起草、剖析排同一条
队（SourceLanes 的 introspect），逐表记状态，能停止，服务重启标中断、不续跑。表照剖析任务的写法：任务一行、逐表一行。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, new_id, utcnow


class SourceObject(Base):
    """数据源里的一个对象（表或视图）。同一个源同一个 schema 下按名字一行（一个数据源一个 schema，目录-D2）：换了 schema
    的源，旧 schema 的行留着不动、不算删除，换回来接着用。

    - name：schema_cache["tables"] 的键（SQLAlchemy 规整过的写法：Oracle 全大写的名字是小写）；name_key 按 names.name_key，
      范围规则、按名字找表都按它比；
    - in_scope / scope_reason：在不在探查范围里、为什么（rule 规则命中、explicit 显式列出、pinned 有数据目录或被已发布
      模板写死的 SQL 用到、on_demand 助手或运行时按需加入）；
    - meta：反射到的完整结构（schema_cache 里一张表的那份），只有范围内的才有；离开范围时清掉，指纹和版本留着；
    - reflect_error：在范围里、却反射不出来（读不到列、权限不够）的原因；这时 meta 是上一次反射成功的那份（有的话）；
    - removed_at：对象索引里已经没有它了（真删除）。只有它说「表已不在」：范围外、反射失败都不是删除；
    - added_*：按需加入、在目录页「加入范围」的留痕：谁、经哪个入口（look / db_schema / catalog）、哪一轮对话或哪次运行、
      需求原文、什么时候。"""

    __tablename__ = "source_objects"
    __table_args__ = (
        UniqueConstraint("source_id", "schema_name", "name"),
        Index("ix_source_objects_scope", "source_id", "schema_name", "in_scope"),
        Index("ix_source_objects_run", "added_run_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    #: 这一行是哪个 schema 的（"" 是连接的默认 schema）
    schema_name: Mapped[str] = mapped_column(String(255), default="")
    name: Mapped[str] = mapped_column(String(255))
    name_key: Mapped[str] = mapped_column(String(255))
    #: table | view
    kind: Mapped[str] = mapped_column(String(10), default="table")
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    #: Oracle 库里本来就是小写的名字（SQLAlchemy 反射回来是 quoted_name(quote=True)）：存成字符串就丢了这个标记，反射时
    #: 要按它原样加引号，不然 Oracle 转成大写去找，找的是另一个对象
    quote_name: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 数据字典里的行数估算（MySQL TABLE_ROWS、PostgreSQL reltuples、Oracle NUM_ROWS）；不知道是 NULL
    est_rows: Mapped[int | None] = mapped_column(Integer, default=None)
    in_scope: Mapped[bool] = mapped_column(Boolean, default=False)
    scope_reason: Mapped[str | None] = mapped_column(String(20), default=None)
    meta: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    struct_version: Mapped[int] = mapped_column(Integer, default=0)
    reflect_error: Mapped[str | None] = mapped_column(Text, default=None)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    reflected_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    #: look | db_schema | catalog
    added_via: Mapped[str | None] = mapped_column(String(20), default=None)
    added_by: Mapped[str | None] = mapped_column(String(100), default=None)
    added_run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    added_conversation_id: Mapped[str | None] = mapped_column(String(32), default=None)
    #: look 记录的编号（L1…）、运行里的节点 id：同一轮、同一次运行里是哪一下加的
    added_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    added_need: Mapped[str | None] = mapped_column(Text, default=None)
    added_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class IntrospectJob(Base):
    """一次后台探查。建任务的请求里已经刷新了对象索引、按范围规则圈好了表、核过上限（超了 409，不建任务）；任务只做反射：
    逐表（按批）读结构、写进对象索引的行，最后由 rebuild_schema_cache 物化 schema_cache。

    同一个源同时只有一个排队或进行中的探查任务（接口先查一遍，部分唯一索引兜底）；和同源的起草、剖析按建立的先后排队。
    挂外键、级联删除：任务是数据源的附属记录。"""

    __tablename__ = "introspect_jobs"
    __table_args__ = (
        Index("ix_introspect_jobs_source_created", "source_id", "created_at"),
        Index("ux_introspect_jobs_active", "source_id", unique=True,
              sqlite_where=text("status IN ('queued', 'running')")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    # queued | running | succeeded | partial | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(20), default="queued")
    #: manual 人点的 | schedule 定时维护（第 3 波）
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    #: 这次探查的是哪个 schema（"" 是默认 schema）
    schema_name: Mapped[str] = mapped_column(String(255), default="")
    #: 建任务那一刻的范围：{rules（规则原样）, in_scope, rule, pinned, explicit, on_demand, out_of_scope, cap, index_total,
    #: index_method, index_capped, removed, added}——留痕：这一次按什么规则、圈了多少张
    scope: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    #: 整个任务出错、数据源中途停用时的整句原因
    error: Mapped[str | None] = mapped_column(Text, default=None)


class IntrospectTask(Base):
    """探查任务里的一张表。按 seq 排。状态：pending 排队、running 正在读、ok 读到了、failed 读不到（原因在 error）、
    cancelled 停止或服务重启时还没读。"""

    __tablename__ = "introspect_tasks"
    __table_args__ = (UniqueConstraint("job_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("introspect_jobs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    table_name: Mapped[str] = mapped_column(String(255))
    #: 为什么在范围里（pinned / explicit / rule / on_demand）：开始执行时按它把范围写进对象索引
    reason: Mapped[str | None] = mapped_column(String(20), default=None)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    #: 结构版本：读完以后的（指纹没变就和以前一样）
    struct_version: Mapped[int] = mapped_column(Integer, default=0)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


__all__ = ["IntrospectJob", "IntrospectTask", "SourceObject"]
