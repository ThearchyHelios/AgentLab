"""流程：待办、派给谁、生效记录、通知（应用平台 A4、A5，docs/design/app-platform-and-look.md §2.4、§2.5、§3.7、§3.8）。

一次中断对应一条 Approval（给引擎）和一条 Task（给人）。Task 下挂若干条 TaskAssignment：派给具体的人或组，
按 policy（any / all / quorum）汇总后才恢复运行。核实待办（kind=reconcile）没有 Approval，approval_id 为空。

ActionRecord 是写外部系统的权威记录：先写记录再发送；节点每次执行都先按 action_key 查，已生效的不再发，
结果未知的永不自动重发。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id, utcnow

#: 待办的种类。人工节点三种 + 现有的几种中断 + 动作确认、核实、等外部系统 + 个人应用提交审核（review，
#: docs/design/normal-user-apps.md N7，app/process/tasks.REVIEW_KIND）+ 口径召回（recall，docs/design/next-round/02-口径账本.md
#: 做法 7：口径升版后答案的数变了、答案依据的目录或表结构变了；挂在召回运行或答案的运行上，没有 Approval，app/ledger/recall）
TASK_KINDS = ("approve", "input", "edit", "tool", "code", "dual_check", "action_confirm", "reconcile", "signal",
              "review", "recall")
#: pending 等人；resolving 已凑齐、正在恢复运行（条件 UPDATE 抢到的那个请求负责）；resolved / cancelled / expired 终态
TASK_STATUSES = ("pending", "resolving", "resolved", "cancelled", "expired")
#: 动作状态。simulated：探索运行里端点没有 test 环境时的演练，不发送
ACTION_STATES = ("pending", "sent", "confirmed", "failed", "unknown", "simulated")


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_status_due", "status", "due_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    approval_id: Mapped[str | None] = mapped_column(String(32), default=None, unique=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), default=None)
    app_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    app_version: Mapped[int | None] = mapped_column(Integer, default=None)
    kind: Mapped[str] = mapped_column(String(20), default="approve")
    title: Mapped[str] = mapped_column(String(300), default="")
    # agentlab.form/1；没有表单的（批准 / 驳回）为空
    form: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    # {"kind": "any"} | {"kind": "all"} | {"kind": "quorum", "m": 2}
    policy: Mapped[dict[str, Any]] = mapped_column(default=lambda: {"kind": "any"})
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    due_at: Mapped[datetime | None] = mapped_column(default=None)
    remind_at: Mapped[datetime | None] = mapped_column(default=None)
    # {"action": "remind" | "escalate" | "reject", "every"?: ISO 时长, "to_slot"?: 岗位}。没有「自动批准」
    on_timeout: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    # 等外部系统（kind=signal）时，入站 signal 按它找到这条待办
    correlation_key: Mapped[str | None] = mapped_column(String(200), default=None, index=True)
    # 判定结果：{"decision": ..., "values": ..., "signoffs": [...]}
    outcome: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    resolved_by: Mapped[str | None] = mapped_column(String(100), default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)


class TaskAssignment(Base):
    __tablename__ = "task_assignments"
    __table_args__ = (Index("ix_assign_principal", "principal_kind", "principal", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    principal_kind: Mapped[str] = mapped_column(String(20))  # user | group | anyone | hook
    principal: Mapped[str] = mapped_column(String(100), default="")
    # 从哪来：slot:<岗位> | initiator | expression | escalation | reassign
    source: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | responded | withdrawn
    response: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    responded_by: Mapped[str | None] = mapped_column(String(100), default=None)
    responded_at: Mapped[datetime | None] = mapped_column(default=None)
    # 回复人是不是认证过的身份（off 模式下是自报署名，记 False）
    verified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ActionRecord(Base, TimestampMixin):
    __tablename__ = "action_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # sha256(run_id, node_id, exec)；同一次执行第 n 次发送的幂等键是 f"{action_key}:{attempt}"
    action_key: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    idem_key: Mapped[str] = mapped_column(String(80), unique=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(64))
    exec_no: Mapped[int] = mapped_column(Integer, default=1)
    app_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    workflow_version: Mapped[int | None] = mapped_column(Integer, default=None)
    graph_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    capability: Mapped[str] = mapped_column(String(200))
    capability_version: Mapped[int | None] = mapped_column(Integer, default=None)
    endpoint_id: Mapped[str | None] = mapped_column(String(32), default=None)
    environment: Mapped[str] = mapped_column(String(20), default="prod")  # prod | test | simulated
    payload_hash: Mapped[str] = mapped_column(String(64))
    # 遮罩后的预览（确认卡上给人看的那一份）
    preview: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    confirm_task_id: Mapped[str | None] = mapped_column(String(32), default=None)
    request_at: Mapped[datetime | None] = mapped_column(default=None)
    response_status: Mapped[int | None] = mapped_column(Integer, default=None)
    # 回执：外部单号等，原文落工件，这里存 {artifact, summary}
    receipt: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    lookup_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_lookup_at: Mapped[datetime | None] = mapped_column(default=None)
    # 核实结论：{"by": ..., "how": "lookup" | "manual", "outcome": "confirmed" | "not_applied" | "resend" | "abandon", "external_id"?}
    resolution: Mapped[dict[str, Any] | None] = mapped_column(default=None)


class NotifyChannel(Base, TimestampMixin):
    """通知渠道。格式、签名、发送同定时任务的 Webhook（四种：generic / feishu / wecom / dingtalk）。"""

    __tablename__ = "notify_channels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(String(20), default="global")  # global | app | user
    owner_id: Mapped[str | None] = mapped_column(String(32), default=None)
    format: Mapped[str] = mapped_column(String(20), default="generic")
    url: Mapped[str] = mapped_column(Text)  # 密文
    secret: Mapped[str | None] = mapped_column(Text, default=None)  # 密文
    events: Mapped[list[Any]] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(default=True)


class NotifyDelivery(Base):
    """通用投递记录。定时任务的投递仍写 webhook_deliveries（schedule_id 非空，SQLite 改不了可空性）。"""

    __tablename__ = "notify_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    channel_id: Mapped[str] = mapped_column(ForeignKey("notify_channels.id", ondelete="CASCADE"), index=True)
    subject_kind: Mapped[str] = mapped_column(String(20))  # task | run | action | channel（渠道的测试消息）
    subject_id: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    task_id: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[str] = mapped_column(String(20))
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
