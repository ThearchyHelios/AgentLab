"""应用（应用平台 A1–A3，docs/design/app-platform-and-look.md §2.1–§2.3、§3.1–§3.6）。

App 可变（名称、草稿、授权）；AppVersion 不可变，只在发布时生成：spec（agentlab.app/1）、钉住的工作流版本（pins）、
岗位与钩子的绑定（bindings）、级别（取各钉住项里最低的那一级）、spec_hash、钉住项升版的处置（dispositions）。
授权（app_grants）和组成员不进版本，改动写审计。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id, utcnow

#: 应用授权：use 能用（入口、视图、自己的实例）；observe 另能看全部实例；manage 能搭建、发布、改派
GRANT_ROLES = ("use", "observe", "manage")


class App(Base, TimestampMixin):
    __tablename__ = "apps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(40), default="app")
    # 编辑中的 spec（agentlab.app/1），发布前随便改；发布时冻结成一个 AppVersion
    draft: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # 草稿里的岗位、钩子、通知渠道绑定（发布时一并冻结进 AppVersion.bindings）
    draft_bindings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    published_version: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | archived
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)
    updated_by: Mapped[str | None] = mapped_column(String(100), default=None)


class AppVersion(Base):
    __tablename__ = "app_versions"
    __table_args__ = (UniqueConstraint("app_id", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    app_id: Mapped[str] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    spec_hash: Mapped[str] = mapped_column(String(64))
    # [{alias, workflow_id, version, graph_hash, level, use}]，和 spec.pins 一致（冗余存一份便于反查）
    pins: Mapped[list[Any]] = mapped_column(default=list)
    # {"slots": {岗位: [{kind: user|group, name}]}, "hooks": {...}, "channels": {...}}
    bindings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    level: Mapped[str] = mapped_column(String(20), default="published")  # published | governed
    # 钉住项相对上一个应用版本升了版时选的处置：{alias: "recompute" | "dual" | "incomparable"}
    dispositions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    published_by: Mapped[str | None] = mapped_column(String(100), default=None)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppGrant(Base):
    __tablename__ = "app_grants"
    __table_args__ = (UniqueConstraint("app_id", "principal_kind", "principal", "role"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    app_id: Mapped[str] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    principal_kind: Mapped[str] = mapped_column(String(20))  # user | group | everyone
    principal: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(20), default="use")
    granted_by: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppViewCache(Base):
    """按需视图的缓存。key = sha256(graph_hash, 规范化后的输入, data_versions, 能力版本)。"""

    __tablename__ = "app_view_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(32), index=True)
    view_id: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)


class AppHook(Base, TimestampMixin):
    """入站钩子。start：按入口发起实例；signal：按 correlation 解决一条「等外部系统」的待办。"""

    __tablename__ = "app_hooks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    app_id: Mapped[str] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))  # start | signal
    entry_id: Mapped[str | None] = mapped_column(String(100), default=None)
    secret: Mapped[str] = mapped_column(Text)  # 密文
    enabled: Mapped[bool] = mapped_column(default=True)


class HookReceipt(Base):
    """入站请求按 Idempotency-Key 去重的记录。"""

    __tablename__ = "hook_receipts"
    __table_args__ = (UniqueConstraint("hook_id", "idem_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hook_id: Mapped[str] = mapped_column(ForeignKey("app_hooks.id", ondelete="CASCADE"), index=True)
    idem_key: Mapped[str] = mapped_column(String(200))
    run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    task_id: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[str] = mapped_column(String(20))  # accepted | rejected
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
