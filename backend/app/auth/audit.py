"""管理类操作的留痕（audit_events）：登录方式、用户和角色、组、应用授权、端点、能力治理、版本退役、应用发布。

只记管理动作，不记登录、查看这类日常操作。操作人取当前请求的身份：开了登录是认证过的用户名（verified=True），
关登录时是自报的署名（verified=False）——审计照实记，不因为没登录就不记。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def record(session: AsyncSession, action: str, subject_kind: str, subject_id: str | None, *,
                 actor: str | None = None, verified: bool | None = None, data: dict[str, Any] | None = None) -> None:
    """记一条审计。不提交：和这次管理操作本身同一个事务，操作没成就不留这一条。actor / verified 不给时取当前请求的身份。"""
    from app.api.deps import current_principal
    from app.db.models import AuditEvent

    principal = current_principal()
    session.add(AuditEvent(
        actor=actor if actor is not None else principal.username,
        verified=principal.verified if verified is None else verified,
        action=action, subject_kind=subject_kind, subject_id=subject_id,
        data=_plain(data or {}),
    ))


def _plain(value: Any) -> Any:
    """审计数据进 JSON 列：时间、集合这类转成能存的样子。"""
    import json

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
