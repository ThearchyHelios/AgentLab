"""口径调用审计（next-round 02 的后续）：谁在什么时候、用哪一版口径、带什么参数。

`caliber_match` 是封存后的等价证明，不是调用日志；一次 `use_caliber` 组合或一次按口径正式出具，在这里各记一条
`AuditEvent`（action=caliber.use）。和账本其他部分一样不另建表：audit_events 已经按 subject 建了索引，按
subject_kind=caliber、subject_id=「工作流@版本」查得到。

**只记参数指纹，不记参数值。** 参数可能是遮罩值、手机号这类不该再落一份的东西；记 hash 和字段名，排查「同一次调用
用了哪组参数」够用，要复核参数去运行输入和比对工件里看（那里本来就有遮罩处理）。指标只记 id，不记算出来的值。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

ACTION = "caliber.use"
SUBJECT_KIND = "caliber"
HOWS = ("composed", "issued")

logger = logging.getLogger(__name__)


def params_fingerprint(values: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    """(参数指纹, 字段名)。空值不参与指纹：没填和填空串是同一组参数；字段名排序，保证同一组参数写法不同也相同。"""
    from app.core.artifact_store import canonical_json, content_hash

    clean = {str(k): v for k, v in (values or {}).items() if v is not None and v != ""}
    return content_hash(canonical_json(clean)), sorted(clean)


async def record_use(session: AsyncSession, *, workflow_id: str, version: int, how: str,
                     card: str | None = None, metrics: Iterable[str] = (), params: Mapping[str, Any] | None = None,
                     run_id: str | None = None, conversation_id: str | None = None,
                     actor: str | None = None, auto: bool | None = None) -> None:
    """记一条调用。不提交：和调用方自己的事务一起提交。how 只能是 composed / issued。"""
    from app.apps.audit import record

    if how not in HOWS:
        raise ValueError(f"不认识的调用方式：{how}")
    digest, fields = params_fingerprint(params)
    data: dict[str, Any] = {
        "workflow_id": workflow_id,
        "version": int(version),
        "how": how,
        "params_hash": digest,
        "param_fields": fields,
        "metrics": [str(m) for m in dict.fromkeys(metrics)],
    }
    if card:
        data["card"] = card
    if run_id:
        data["run_id"] = run_id
    if conversation_id:
        data["conversation_id"] = conversation_id
    if auto is not None:
        data["auto"] = bool(auto)
    await record(session, action=ACTION, subject_kind=SUBJECT_KIND,
                 subject_id=f"{workflow_id}@{int(version)}", actor=actor, data=data)


async def audit_composed_uses(uses: Iterable[Mapping[str, Any]], *, conversation_id: str | None = None,
                              actor: str | None = None) -> int:
    """一轮生成里所有 use_caliber 落成审计，返回记了几条。取数组合发生在建图阶段，没有运行可挂，只记对话。"""
    uses = list(uses)
    if not uses:
        return 0
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        for use in uses:
            await record_use(
                session,
                workflow_id=str(use.get("workflow_id") or ""),
                version=int(use.get("version") or 0),
                how="composed",
                card=str(use.get("card") or "") or None,
                metrics=use.get("metrics") or (),
                params=use.get("inputs") if isinstance(use.get("inputs"), Mapping) else None,
                conversation_id=conversation_id,
                actor=actor,
            )
        await session.commit()
    return len(uses)


__all__ = ["ACTION", "HOWS", "SUBJECT_KIND", "audit_composed_uses", "params_fingerprint", "record_use"]
