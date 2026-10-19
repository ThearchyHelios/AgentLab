"""普通用户建应用（docs/design/normal-user-apps.md §3.1）：蓝图、蓝图留痕、发布单、文件、记录表。

蓝图（agentlab.blueprint/1）是用户的话和工作流、应用之间的中间件：卡片上每一项都带来源和状态，模型和用户都只能用
bp_* 操作改它（N2）。发布单把「保存版本 → 发布工作流 → 建应用 → 定时任务 → 授权 → 跑第一次」编成可续做的步骤（N5）。
文件按 sha256 内容寻址存放、提交时绑到一次运行（N9）；记录表是平台托管、只许追加的表（N8）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id, utcnow

#: 蓝图的可见范围：个人（使用者自己的，终点是个人应用）、共享（搭建者的，终点是发布单）
BLUEPRINT_SCOPES = ("personal", "shared")
#: 蓝图的状态：起草中 → 试运行过 → 已发布（共享）/ 个人应用（个人）；个人应用提交审核时为审核中
BLUEPRINT_STATUSES = ("drafting", "trialed", "published", "personal", "in_review")
#: 蓝图从哪里来：一段对话、一个模板、一次运行（问数据的一轮回答）、一个已发布的工作流版本
BLUEPRINT_SOURCES = ("conversation", "template", "run", "workflow_version")
#: 发布单的状态
PLAN_STATUSES = ("planned", "running", "stopped", "done")


class Blueprint(Base, TimestampMixin):
    __tablename__ = "blueprints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), default="")
    owner: Mapped[str | None] = mapped_column(String(100), default=None, index=True)
    scope: Mapped[str] = mapped_column(String(20), default="shared")
    status: Mapped[str] = mapped_column(String(20), default="drafting")
    # agentlab.blueprint/1（blueprints/spec.py）
    spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    template_id: Mapped[str | None] = mapped_column(String(64), default=None)
    template_version: Mapped[int | None] = mapped_column(Integer, default=None)
    # {"kind": conversation|template|run|workflow_version, "id": …, "version"?: …}
    source: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 蓝图对话（kind=blueprint 的 Conversation）
    conversation_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 共享蓝图：实例化出来的工作流和发布出来的应用；个人蓝图：个人应用
    workflow_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    app_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    last_trial_run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 最近一次实例化的图（GraphSpec 的 JSON）和它的哈希。个人应用的图只存在这里（使用者没有工作流）
    graph: Mapped[dict[str, Any]] = mapped_column(default=dict)
    graph_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    # 各关节的状态 {joint_id: {"status": filled|pending|refused, "by": assistant|template|user, "note"?: …}}
    joints: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 审核副本指向原蓝图
    copied_from: Mapped[str | None] = mapped_column(String(32), default=None)
    updated_by: Mapped[str | None] = mapped_column(String(100), default=None)


class BlueprintEvent(Base):
    """蓝图的留痕：每一次 bp_* 操作（模型提的、用户改的）、每一次实例化和试运行。"""

    __tablename__ = "blueprint_events"
    __table_args__ = (UniqueConstraint("blueprint_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    blueprint_id: Mapped[str] = mapped_column(ForeignKey("blueprints.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str | None] = mapped_column(String(100), default=None)
    verified: Mapped[bool] = mapped_column(default=False)
    # bp_set | bp_add | bp_remove | bp_ask | bp_template | instantiate | fill_joint | trial | publish | save_personal | copy
    op: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PublishPlan(Base, TimestampMixin):
    """一次确认发布（N5）。steps 是 [{kind, status: pending|done|failed|skipped, result, error_code, message, at}]，
    summary 是白话摘要 [{step, text}]；停在某一步时 status=stopped，修好后从那一步接着做。"""

    __tablename__ = "publish_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    blueprint_id: Mapped[str] = mapped_column(ForeignKey("blueprints.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str | None] = mapped_column(String(100), default=None)
    level: Mapped[str] = mapped_column(String(20), default="published")
    # 发布单针对的那张图（实例化结果的哈希）：执行时图变了就拒绝，要求重新出单
    graph_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    steps: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[list[Any]] = mapped_column(default=list)
    problems: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(20), default="planned")
    #: 执行这份发布单的工作进程（status=running 时，app/cluster）：属主不在了的才按「执行中断」收拾
    owner: Mapped[str | None] = mapped_column(String(120), default=None)


class StoredFile(Base):
    """上传的文件（N9）：内容按 sha256 存在 data/files/<前 2 位>/<sha>（只读），这里记元数据和绑定。
    提交表单时绑到一次运行（一次性）或一行记录；没绑定的上传按时清理。"""

    __tablename__ = "files"
    __table_args__ = (Index("ix_files_unbound", "bound_run_id", "uploaded_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(300))
    size: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    uploaded_by: Mapped[str | None] = mapped_column(String(100), default=None)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 上传时的上下文：{"app_id", "entry"} 或 {"task_id"} 或 {"blueprint_id"}（个人应用、试运行）
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    bound_run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 附在记录上时：{"table_id", "row_id"}
    bound_record: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    purpose: Mapped[str] = mapped_column(String(40), default="input")


class RecordsTable(Base, TimestampMixin):
    """记录表（N8）的一个版本：平台托管的 SQLite 文件 data/records/<table_key>.v<version>.db，只许追加。
    兼容的表单改动（只加可选字段）在同一版本上 ADD COLUMN；不兼容的出新版本，旧版本只读保留。"""

    __tablename__ = "records_tables"
    __table_args__ = (UniqueConstraint("table_key", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 同一张记录表各版本共用的键
    table_key: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200))
    app_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    blueprint_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # 登记成 origin=records 的只读数据源（看板用 db_query__<名字> 查）
    source_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # [{name, label, type, required, sensitive}]
    columns: Mapped[list[Any]] = mapped_column(default=list)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    note: Mapped[str] = mapped_column(Text, default="")
