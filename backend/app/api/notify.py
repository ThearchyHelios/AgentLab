"""通知渠道与投递记录（BE-2）。设计文档 §2.4、§3.10。

渠道的地址和签名密钥加密落库，接口只回主机名、配没配密钥；改渠道时不传地址（或传空）表示保持原值。格式和地址的
校验同定时任务的 Webhook（app/notify/core.py）。个人渠道（scope=user）只有开了登录才有：它挂在用户 id 上，只有本人
能看能改；全局、应用的渠道关登录时谁都能改，开了登录要 builder 以上。定时任务的通知仍在定时任务上设，不迁移（Q7）。

个人渠道谁都能建（权限最低的使用者也能），地址另过一遍 SSRF 检查（tools/net.py，不开内网），保存和发送时各一次：
不然服务端替它往内网地址发 POST，测试接口交回的状态码和报错就成了探测内网的信号（A6.2：只有登记的端点能访问内网）。
全局、应用的渠道只有搭建者以上能建，和定时任务的 Webhook 同一个信任级别，地址照旧只查格式（内网的接收端很常见）。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.api.deps import current_principal
from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import NotifyChannel, NotifyDelivery
from app.notify import core
from app.notify.service import EVENTS, EVENT_LABEL, channel_out

router = APIRouter(prefix="/api/notify", tags=["notify"])


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scope: Literal["global", "app", "user"] = "global"
    owner_id: str | None = None
    format: str = "generic"
    #: 新建必填；修改时不传或传空表示保持原值
    url: str | None = None
    #: 签名密钥：不传保持原值，传空字符串表示去掉
    secret: str | None = None
    events: list[str] = Field(default_factory=list)
    enabled: bool = True


def _can_edit(row_scope: str, owner_id: str | None) -> None:
    principal = current_principal()
    if principal.mode == "off":
        if row_scope == "user":
            raise HTTPException(422, "个人通知渠道需要开启登录后才能使用：关闭登录时没有用户，无法认出是谁的渠道")
        return
    if row_scope == "user":
        if owner_id != principal.user_id:
            raise HTTPException(403, "只能修改自己的通知渠道")
        return
    if not principal.at_least("builder"):
        raise HTTPException(403, "只有搭建者和管理员可以修改全局或应用的通知渠道")


async def _safe_personal(scope: str, url: str) -> None:
    """个人渠道的地址过 SSRF 检查（见模块说明）。"""
    from app.notify.service import personal_url_problem

    if scope == "user" and (problem := await personal_url_problem(url)):
        raise HTTPException(422, problem)


def _validated(payload: ChannelIn, *, url: str | None) -> None:
    if payload.format not in core.FORMATS:
        raise HTTPException(422, f"消息格式只能是{'、'.join(core.FORMAT_LABEL.values())}之一")
    unknown = [e for e in payload.events if e not in EVENTS]
    if unknown:
        raise HTTPException(422, f"不认识的通知事件：{'、'.join(unknown)}。可选：{'、'.join(EVENT_LABEL.values())}")
    if url is not None and (problem := core.format_problem(payload.format, url)):
        raise HTTPException(422, problem)


@router.get("/channels")
async def list_channels() -> dict[str, Any]:
    """{items}：形状同前端的接口约定（frontend/src/api/capabilities.ts）。"""
    principal = current_principal()
    stmt = select(NotifyChannel).order_by(NotifyChannel.created_at)
    if principal.mode != "off":
        # 个人渠道只给本人看
        stmt = stmt.where(or_(NotifyChannel.scope != "user", NotifyChannel.owner_id == principal.user_id))
    async with SessionLocal() as session:
        return {"items": [channel_out(r) for r in (await session.execute(stmt)).scalars()]}


@router.get("/events")
async def list_events() -> list[dict[str, str]]:
    return [{"event": e, "label": EVENT_LABEL[e]} for e in EVENTS]


@router.post("/channels", status_code=201)
async def create_channel(payload: ChannelIn) -> dict[str, Any]:
    owner = current_principal().user_id if payload.scope == "user" else payload.owner_id
    _can_edit(payload.scope, owner)
    try:
        url = core.check_url(payload.url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    _validated(payload, url=url)
    await _safe_personal(payload.scope, url)
    row = NotifyChannel(name=payload.name.strip(), scope=payload.scope, owner_id=owner, format=payload.format,
                        url=encrypt(url), secret=encrypt(payload.secret) if payload.secret else None,
                        events=list(dict.fromkeys(payload.events)), enabled=payload.enabled)
    async with SessionLocal() as session:
        session.add(row)
        await session.commit()
        return channel_out(row)


class ChannelPatch(BaseModel):
    """改一部分：没给的项保持原值（界面上的启用开关只传 enabled）。"""
    name: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Literal["global", "app", "user"] | None = None
    owner_id: str | None = None
    format: str | None = None
    url: str | None = None
    secret: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


@router.patch("/channels/{channel_id}")
async def patch_channel(channel_id: str, payload: ChannelPatch) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await session.get(NotifyChannel, channel_id)
        if row is None:
            raise HTTPException(404, "通知渠道不存在，可能已被删除")
    current = {"name": row.name, "scope": row.scope, "owner_id": row.owner_id, "format": row.format,
               "events": list(row.events or []), "enabled": row.enabled}
    given = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    return await update_channel(channel_id, ChannelIn(**{**current, **given}))


@router.put("/channels/{channel_id}")
async def update_channel(channel_id: str, payload: ChannelIn) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await session.get(NotifyChannel, channel_id)
        if row is None:
            raise HTTPException(404, "通知渠道不存在，可能已被删除")
        _can_edit(row.scope, row.owner_id)
        if payload.scope != row.scope:
            raise HTTPException(422, "通知渠道的范围建好之后不能修改，请新建一个")
        url = None
        if payload.url:
            try:
                url = core.check_url(payload.url)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        if url is None:
            from app.core.crypto import decrypt

            current = decrypt(row.url) or ""
            _validated(payload, url=current or None)
        else:
            _validated(payload, url=url)
            await _safe_personal(row.scope, url)
            row.url = encrypt(url)
        row.name, row.format = payload.name.strip(), payload.format
        row.events, row.enabled = list(dict.fromkeys(payload.events)), payload.enabled
        if payload.secret is not None:
            row.secret = encrypt(payload.secret) if payload.secret else None
        if row.scope == "app":
            row.owner_id = payload.owner_id
        await session.commit()
        return channel_out(row)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(channel_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(NotifyChannel, channel_id)
        if row is None:
            raise HTTPException(404, "通知渠道不存在，可能已被删除")
        _can_edit(row.scope, row.owner_id)
        await session.delete(row)
        await session.commit()


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str) -> dict[str, Any]:
    """发一条测试消息（不重试），记一条 event=test 的投递记录。返回 {ok, http_status, message}，同定时任务的测试。"""
    from app.notify import send

    async with SessionLocal() as session:
        row = await session.get(NotifyChannel, channel_id)
        if row is None:
            raise HTTPException(404, "通知渠道不存在，可能已被删除")
        _can_edit(row.scope, row.owner_id)
    message = {"event": "test", "title": f"通知渠道「{row.name}」测试", "ok": True, "retry": False,
               "lines": [("说明", "这是一条测试消息。收到即表示渠道地址配置正确"
                                  + ("；请求头带有签名，接收端可据此核对签名密钥" if row.secret and row.format == "generic"
                                     else ""))]}
    outcome = await send(row, message)
    async with SessionLocal() as session:
        session.add(NotifyDelivery(channel_id=row.id, subject_kind="channel", subject_id=row.id, event="test",
                                   status="ok" if outcome.ok else "failed", http_status=outcome.http_status,
                                   attempts=outcome.attempts, error=outcome.error))
        await session.commit()
    if outcome.ok:
        return {"ok": True, "http_status": outcome.http_status, "message": "请到接收端确认已收到"}
    return {"ok": False, "http_status": outcome.http_status, "message": outcome.error or "发送失败"}


@router.get("/deliveries")
async def list_deliveries(
    channel_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """投递记录。开登录后 user 角色只看发往自己的个人渠道的：别的渠道的投递记录里有所有应用、所有运行的待办和动作。"""
    stmt = select(NotifyDelivery).order_by(NotifyDelivery.created_at.desc()).limit(limit)
    principal = current_principal()
    if principal.mode != "off" and not principal.at_least("builder"):
        stmt = stmt.join(NotifyChannel, NotifyChannel.id == NotifyDelivery.channel_id).where(
            NotifyChannel.scope == "user", NotifyChannel.owner_id == principal.user_id)
    if channel_id:
        stmt = stmt.where(NotifyDelivery.channel_id == channel_id)
    if run_id:
        stmt = stmt.where(NotifyDelivery.run_id == run_id)
    if task_id:
        stmt = stmt.where(NotifyDelivery.task_id == task_id)
    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).scalars()
        return {"items": [{
            "id": r.id, "channel_id": r.channel_id, "subject_kind": r.subject_kind, "subject_id": r.subject_id,
            "event": r.event, "run_id": r.run_id, "task_id": r.task_id, "status": r.status,
            "http_status": r.http_status, "attempts": r.attempts, "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]}
