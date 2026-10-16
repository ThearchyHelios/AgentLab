"""通知渠道与投递记录（BE-2）。设计文档 §2.4、§3.10。

渠道的地址和签名密钥加密落库，接口只回主机名、配没配密钥；改渠道时不传地址（或传空）表示保持原值。格式和地址的
校验同定时任务的 Webhook（app/notify/core.py）。个人渠道（scope=user）只有开了登录才有：它挂在用户 id 上，只有本人
能看能改；全局、应用的渠道关登录时谁都能改，开了登录要 builder 以上。定时任务的通知仍在定时任务上设，不迁移（Q7）。

个人渠道谁都能建（权限最低的使用者也能），地址另过一遍 SSRF 检查（tools/net.py，不开内网），保存和发送时各一次：
不然服务端替它往内网地址发 POST，测试接口交回的状态码和报错就成了探测内网的信号（A6.2：只有登记的端点能访问内网）。
全局、应用的渠道只有搭建者以上能建，和定时任务的 Webhook 同一个信任级别，地址照旧只查格式（内网的接收端很常见）。

邮件（第五种格式，notify/mail.py，04 细案 B2）：个人邮件渠道不收地址，只发到本人在名录里的账号邮箱（能填任意地址的话，个人
渠道就成了谁都能用的邮件中继）；全局、应用的邮件渠道填固定的收件地址，最多 20 个。邮件服务（设置的 mail 一组）没启用时
不收新的邮件渠道：建了也发不出去，就是「看着生效、其实没生效」。渠道列表另给 mail：邮件服务启没启用、本人的账号邮箱，
设置页据此决定列不列「邮件」这一格式、个人邮件渠道写「发到账号邮箱 …」。
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
from app.notify import core, mail
from app.notify.service import EVENTS, channel_out, subscribe_problem

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


def _validated(payload: ChannelIn, *, url: str | None, saved: list[str] | None = None) -> None:
    """saved 是渠道已经订阅着的事件：只核对这次新勾的（notify/service.subscribe_problem）。以前存下的照旧——当时界面对
    哪种渠道都列全部事件，现在才按事件推给哪几条路核对，不能让这些渠道连开关、改名都 422。"""
    if payload.format not in core.CHANNEL_FORMATS:
        raise HTTPException(422, f"消息格式只能是{'、'.join(core.FORMAT_LABEL[f] for f in core.CHANNEL_FORMATS)}之一")
    for event in dict.fromkeys(payload.events):
        if event not in (saved or ()) and (problem := subscribe_problem(event, payload.scope)):
            raise HTTPException(422, problem)
    if url is not None and payload.format != "email" and (problem := core.format_problem(payload.format, url)):
        raise HTTPException(422, problem)


#: 个人邮件渠道带了地址：不收（见模块说明）
PERSONAL_MAIL_NO_URL = "个人邮件渠道只发到你的账号邮箱，不用填地址"


async def _mail_target(scope: str, url: str | None, *, saved: str | None = None) -> str:
    """邮件渠道 url 列存什么（明文，调用方加密）：个人渠道存占位，全局、应用的是校验过的收件地址（逗号分隔）。
    url 没给、原来就是邮件渠道的（saved）沿用原来的地址。邮件服务没启用时不收（见模块说明）。"""
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        cfg = await mail.load_config(session)
    if not cfg.enabled:
        raise HTTPException(422, mail.NOT_ENABLED)
    if scope == "user":
        if url and url.strip():
            raise HTTPException(422, PERSONAL_MAIL_NO_URL)
        return mail.PERSONAL_PLACEHOLDER
    if not (url and url.strip()):
        if saved:
            return saved
        raise HTTPException(422, "请填写收件地址，多个地址用逗号或换行分隔")
    try:
        return ", ".join(mail.parse_recipients(url))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


async def _owner_emails(rows: list[NotifyChannel]) -> dict[str, str | None]:
    """个人邮件渠道的拥有者 → 账号邮箱（channel_out 写「发到账号邮箱 …」用）。"""
    from app.db.models import User

    owners = {r.owner_id for r in rows if r.scope == "user" and r.format == "email" and r.owner_id}
    if not owners:
        return {}
    async with SessionLocal() as session:
        found = (await session.execute(select(User.id, User.email).where(User.id.in_(owners)))).all()
    return {uid: email for uid, email in found}


@router.get("/channels")
async def list_channels() -> dict[str, Any]:
    """{items, mail}：形状同前端的接口约定（frontend/src/api/capabilities.ts）。mail 是邮件服务启没启用、本人的账号邮箱
    （关登录时没有账号，为 null）：设置页据此列不列「邮件」格式、个人邮件渠道发到哪。"""
    from app.db.models import User

    principal = current_principal()
    stmt = select(NotifyChannel).order_by(NotifyChannel.created_at)
    if principal.mode != "off":
        # 个人渠道只给本人看
        stmt = stmt.where(or_(NotifyChannel.scope != "user", NotifyChannel.owner_id == principal.user_id))
    async with SessionLocal() as session:
        rows = list((await session.execute(stmt)).scalars())
        cfg = await mail.load_config(session)
        mine = await session.get(User, principal.user_id) if principal.mode != "off" and principal.user_id else None
    emails = await _owner_emails(rows)
    return {"items": [channel_out(r, emails.get(r.owner_id or "")) for r in rows],
            "mail": {"enabled": cfg.enabled, "account_email": (mine.email or None) if mine is not None else None}}


@router.get("/events")
async def list_events() -> list[dict[str, Any]]:
    """登记的通知事件和推给哪几条路（typesApp.ts 的 NotifyEventRow）：设置页、应用的通知按 scopes 列可订阅的事件。
    scopes 是空的，是登记了、还没有推送的，照实给出来，由界面不列。"""
    return [{"event": name, "label": event.label, "scopes": list(event.scopes)} for name, event in EVENTS.items()]


@router.post("/channels", status_code=201)
async def create_channel(payload: ChannelIn) -> dict[str, Any]:
    owner = current_principal().user_id if payload.scope == "user" else payload.owner_id
    _can_edit(payload.scope, owner)
    if payload.format == "email":
        _validated(payload, url=None)
        url = await _mail_target(payload.scope, payload.url)
        secret = None   # 邮件没有签名密钥
    else:
        try:
            url = core.check_url(payload.url)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        _validated(payload, url=url)
        await _safe_personal(payload.scope, url)
        secret = encrypt(payload.secret) if payload.secret else None
    row = NotifyChannel(name=payload.name.strip(), scope=payload.scope, owner_id=owner, format=payload.format,
                        url=encrypt(url), secret=secret, events=list(dict.fromkeys(payload.events)),
                        enabled=payload.enabled)
    async with SessionLocal() as session:
        session.add(row)
        await session.commit()
    return channel_out(row, (await _owner_emails([row])).get(row.owner_id or ""))


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
        from app.core.crypto import decrypt

        url = None
        if payload.format == "email":
            _validated(payload, url=None, saved=list(row.events or []))
            # 只开关、改名、改订阅（地址不传）的不重新核对邮件服务：停用了邮件服务，已有的渠道照样能关掉、改名
            if payload.url or row.format != "email":
                saved = decrypt(row.url) if row.format == "email" and row.scope != "user" else None
                row.url = encrypt(await _mail_target(row.scope, payload.url, saved=saved))
            row.secret = None
        else:
            if payload.url:
                try:
                    url = core.check_url(payload.url)
                except ValueError as e:
                    raise HTTPException(422, str(e)) from e
            elif row.format == "email":
                # 邮件渠道换成 Webhook：原来存的是收件地址，不是能发的 Webhook 地址
                raise HTTPException(422, "换成 Webhook 格式要填写 Webhook 地址")
            if url is None:
                current = decrypt(row.url) or ""
                _validated(payload, url=current or None, saved=list(row.events or []))
            else:
                _validated(payload, url=url, saved=list(row.events or []))
                await _safe_personal(row.scope, url)
                row.url = encrypt(url)
            if payload.secret is not None:
                row.secret = encrypt(payload.secret) if payload.secret else None
        row.name, row.format = payload.name.strip(), payload.format
        row.events, row.enabled = list(dict.fromkeys(payload.events)), payload.enabled
        if row.scope == "app":
            row.owner_id = payload.owner_id
        await session.commit()
    return channel_out(row, (await _owner_emails([row])).get(row.owner_id or ""))


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
               "lines": [("说明", ("这是一封测试邮件。收到即表示邮件服务和收件地址配置正确" if row.format == "email" else
                                   "这是一条测试消息。收到即表示渠道地址配置正确")
                                  + ("；请求头带有签名，接收端可据此核对签名密钥" if row.secret and row.format == "generic"
                                     else ""))]}
    outcome = await send(row, message)
    async with SessionLocal() as session:
        session.add(NotifyDelivery(channel_id=row.id, subject_kind="channel", subject_id=row.id, event="test",
                                   status="ok" if outcome.ok else "failed", http_status=outcome.http_status,
                                   attempts=outcome.attempts, error=outcome.error))
        await session.commit()
    if outcome.ok:
        if row.format == "email":
            # 交给了邮件服务器不等于进了收件箱：退信不追踪，照实说
            return {"ok": True, "http_status": None,
                    "message": f"{outcome.error or mail.HANDED_OFF}，是否送达以邮件服务器为准，请到收件箱确认"}
        return {"ok": True, "http_status": outcome.http_status, "message": "请到接收端确认已收到"}
    return {"ok": False, "http_status": outcome.http_status, "message": outcome.error or "发送失败"}


@router.get("/deliveries")
async def list_deliveries(
    channel_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """投递记录。开登录后 user 角色只看发往自己的个人渠道的：别的渠道的投递记录里有所有应用、所有运行的待办和动作。

    format 是渠道的格式：邮件渠道成功的那条界面上写「已交给邮件服务器」，不写「已送达」（退信不追踪）。"""
    stmt = (select(NotifyDelivery, NotifyChannel.format)
            .join(NotifyChannel, NotifyChannel.id == NotifyDelivery.channel_id)
            .order_by(NotifyDelivery.created_at.desc()).limit(limit))
    principal = current_principal()
    if principal.mode != "off" and not principal.at_least("builder"):
        stmt = stmt.where(NotifyChannel.scope == "user", NotifyChannel.owner_id == principal.user_id)
    if channel_id:
        stmt = stmt.where(NotifyDelivery.channel_id == channel_id)
    if run_id:
        stmt = stmt.where(NotifyDelivery.run_id == run_id)
    if task_id:
        stmt = stmt.where(NotifyDelivery.task_id == task_id)
    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).all()
        return {"items": [{
            "id": r.id, "channel_id": r.channel_id, "subject_kind": r.subject_kind, "subject_id": r.subject_id,
            "event": r.event, "run_id": r.run_id, "task_id": r.task_id, "status": r.status,
            "http_status": r.http_status, "attempts": r.attempts, "error": r.error, "format": fmt,
            "created_at": r.created_at.isoformat() if r.created_at else None} for r, fmt in rows]}
