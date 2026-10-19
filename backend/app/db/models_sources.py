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

from app.db.base import Base, current_owner, PortableJSON, UTCDateTime, new_id, utcnow


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
              sqlite_where=text("status IN ('queued', 'running')"),
              postgresql_where=text("status IN ('queued', 'running')")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    # queued | running | succeeded | partial | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(20), default="queued")
    #: 认领这一行的工作进程（app/cluster；缺省是建它的进程）。多进程时启动整理、接手只碰属主不在了的行
    owner: Mapped[str | None] = mapped_column(String(120), default=current_owner)
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


# --------------------------------------------------------------------------
# 第 3 波：取值观察、变化记录（docs/design/next-round/03-数据接入与目录维护.md B2、B3）
# --------------------------------------------------------------------------


class TableObservation(Base):
    """一次取值观察（剖析引擎的四类观察检查，data/catalog_profile_observe.py）：码值列的取值分布（codes_observe）、最新业务
    日期（freshness）、最近 35 天按天的行数（daily_volume）、行数（rows）。

    **为什么单独一张表。** 剖析任务的逐表一行只留最近 50 个任务，而且一张表一次剖析只有一份发现；取值漂移要拿同一口径的
    历史比（新码值、比例剧变、断档、缺天、量跌、行数跌），得按「表 × 种类 × 列」留一串。每张表每种观察（码值按列）留最近
    KEEP 条，更早的删掉。

    **口径（basis）。** 每条都记它看的是哪一部分数据：full 整表、first_rows（按存储顺序的前 N 行）、window（业务日期最近 N 天）、
    sample（块采样 p%）、stats / count（行数的来源）。basis_key 是口径的规范写法：只和 basis_key 相同的历史比，口径不同的照实说
    不比。遮罩列不出现在任何一条观察查询里，取值也就不会进这张表。

    挂外键、级联删除：观察是数据源的附属记录。"""

    __tablename__ = "table_observations"
    __table_args__ = (
        Index("ix_table_observations_lookup", "source_id", "table_name", "kind", "column_name", "at"),
    )

    #: 每张表每种观察（码值按列）留几条
    KEEP = 26

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    table_name: Mapped[str] = mapped_column(String(255))
    #: 码值、业务日期观察的那一列；表级的（行数）是空串
    column_name: Mapped[str] = mapped_column(String(255), default="")
    #: codes_observe | freshness | daily_volume | rows
    kind: Mapped[str] = mapped_column(String(20))
    basis: Mapped[dict[str, Any]] = mapped_column(default=dict)
    basis_key: Mapped[str] = mapped_column(String(200), default="")
    #: 观察到的：码值 {values: [[码, 行数]…], rows, truncated}；最新业务日期 {max}；按日 {since, until, days: {日: 行数}}；
    #: 行数 {rows, at_least, method}
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: 哪个剖析任务的第几张表测到的
    job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    task_seq: Mapped[int | None] = mapped_column(Integer, default=None)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SourceChange(Base):
    """数据源的一条变化（data/source_changes.py，格式 agentlab-change/1）：结构变化（重新探查后的前后差异）和取值变化（取值
    观察和同口径历史的比较）。全站只有这一份记录：审阅页的「变化」、数据源卡片的角标、外部通知、口径账本的召回都读它。

    - seq：自增，不复用（sqlite_autoincrement），召回按它当游标拉取；id 是给界面、链接用的编号；
    - kind：结构类 table_added / table_removed / table_left_scope / table_reflect_failed / column_added / column_removed /
      column_type_changed / column_nullable_changed / key_changed / fk_changed / comment_changed / scope_changed；取值类
      codes_new / codes_shift / date_stalled / date_gap / volume_drop / rows_drop / rows_jump / coverage_drop；
    - severity：info / warning / action（现在大概率已经不对了：已确认的项、写死的 SQL 指向被删的列，或者「已列全」被撤销）；
    - detected_by：{job_kind, job_id, task_seq, trigger: manual|schedule|on_demand, actor}；basis：{method, query_refs}；
    - before / after：前后各是什么（码值只记非遮罩列的）；affects：{catalog: [{table, path, status, source}], templates:
      [{workflow_id, name, version, impact, nodes}], rules_touched}；struct_version_before / after：结构变化前后的结构版本；
    - fingerprint：去重——同一个变化再次测到只更新 last_seen_at 和 seen_count；
    - status：open / acknowledged / resolved / dismissed；resolution：{by, at, action, note, catalog_version, job_id}。

    挂外键、级联删除：变化是数据源的附属记录。"""

    __tablename__ = "source_changes"
    __table_args__ = (
        UniqueConstraint("source_id", "fingerprint"),
        Index("ix_source_changes_source_status", "source_id", "status"),
        Index("ix_source_changes_source_table", "source_id", "table_name"),
        {"sqlite_autoincrement": True},
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    #: 测到时这个源叫什么（之后改了名，这条记录说的仍是当时的名字）
    source_name: Mapped[str] = mapped_column(String(100), default="")
    #: 源级的变化（范围规则改了）是空串
    table_name: Mapped[str] = mapped_column(String(255), default="")
    column_name: Mapped[str | None] = mapped_column(String(255), default=None)
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10), default="info")
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    detected_by: Mapped[dict[str, Any]] = mapped_column(default=dict)
    basis: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    before: Mapped[Any] = mapped_column(PortableJSON, nullable=True, default=None)
    after: Mapped[Any] = mapped_column(PortableJSON, nullable=True, default=None)
    affects: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    struct_version_before: Mapped[int | None] = mapped_column(Integer, default=None)
    struct_version_after: Mapped[int | None] = mapped_column(Integer, default=None)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="open")
    resolution: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    #: 记录的格式（agentlab-change/1）：召回按它读，字段以后改要升版本
    schema_tag: Mapped[str] = mapped_column("schema", String(30), default="agentlab-change/1")


__all__ = ["IntrospectJob", "IntrospectTask", "SourceChange", "SourceObject", "TableObservation"]
