"""应用平台里管理类操作的留痕（audit_events）：应用发布、授权、钩子、版本退役。

操作人记各「人」列的同一个名字（api/deps.actor_of：开登录是认证过的用户名，关登录是自报的署名），verified 照实记
这个名字是不是认证过的（关登录时恒为假）。和要留痕的改动同一个事务提交，调用方负责 commit。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_identity import AuditEvent


async def record(session: AsyncSession, *, action: str, subject_kind: str, subject_id: str | None,
                 actor: str | None, data: dict[str, Any] | None = None) -> AuditEvent:
    from app.api.deps import current_principal

    row = AuditEvent(actor=actor, verified=current_principal().verified, action=action, subject_kind=subject_kind,
                     subject_id=subject_id, data=_jsonable(data or {}))
    session.add(row)
    return row


def _jsonable(value: Any) -> Any:
    """审计数据进 JSON 列：时间写成 ISO 字符串。"""
    from datetime import datetime

    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
