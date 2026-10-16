"""身份：用户、组、会话、审计（应用平台 A7，docs/design/app-platform-and-look.md §2.7、§3.1）。

登录方式有三种：off（默认，今天的行为：没有登录，X-Actor 自报署名）、local（用户名 + 密码）、oidc。
off 下这些表都是空的，任何接口都不读它们——关登录时的行为必须和加这些表之前一模一样。

username 一经建立就不改：各「人」列（runs.started_by、approvals.resolved_by、workflows.published_by……）开了登录
就写它，改名会让历史记录对不上人。要换叫法改 display_name。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, new_id, utcnow

#: 角色由高到低：admin ⊇ builder ⊇ user
ROLES = ("admin", "builder", "user")


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str | None] = mapped_column(String(200), default=None)
    role: Mapped[str] = mapped_column(String(20), default="user")
    # person 人；service 服务账号（next-round 04 A1）：管理员建，没有口令、不能登录，只持有访问密钥
    kind: Mapped[str] = mapped_column(String(20), default="person")
    # local 模式：hashlib.scrypt 的结果（含盐和参数）；OIDC 用户为空
    password_hash: Mapped[str | None] = mapped_column(Text, default=None)
    oidc_issuer: Mapped[str | None] = mapped_column(String(300), default=None)
    # (issuer, subject) 认一个 OIDC 用户；只看 subject 会把两个身份源里同号的人当成一个
    oidc_subject: Mapped[str | None] = mapped_column(String(200), default=None)
    disabled: Mapped[bool] = mapped_column(default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[str | None] = mapped_column(String(100), default=None)

    __table_args__ = (UniqueConstraint("oidc_issuer", "oidc_subject"),)


class Group(Base, TimestampMixin):
    """组：岗位绑定、应用授权都可以落到组上。source=oidc 的由登录时按 groups 声明同步，界面上只读。"""

    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    label: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(20), default="local")  # local | oidc


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    added_by: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuthSession(Base):
    """登录会话。库里只存令牌的 sha256，令牌本身只在 HttpOnly cookie 里。"""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)


class AuditEvent(Base):
    """管理类操作的留痕：登录方式、角色、组、授权、应用发布、端点、能力治理、版本退役。

    verified 记这一条的操作人是不是认证过的身份（off 模式下是自报的署名，记 False）。
    """

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_subject", "subject_kind", "subject_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor: Mapped[str | None] = mapped_column(String(100), default=None)
    verified: Mapped[bool] = mapped_column(default=False)
    action: Mapped[str] = mapped_column(String(60))
    subject_kind: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[str | None] = mapped_column(String(64), default=None)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
