"""每天的待办汇总、访问密钥快到期：什么时候推（docs/design/next-round/04-对外集成与触达.md B3）。

调度循环每一拍调一次（scheduler/loop.process_due 的步骤列表）；拼消息、推送在 notify/service（notify_digest、
notify_key_expiring），推送放在后台（service.spawn）：接收端挂住时超时加重试要几十秒，不能拖着这一拍别的步骤。

待办汇总（task.digest）：
- 每天到设置里的 notify.digest_at（缺省 09:00，按服务端所在时区）之后的第一拍，给订阅了 task.digest 的个人渠道的拥有者各发
  一条（没有待办的人不发）；
- 按日期在 settings 表里认领一行（键 STATE_PREFIX + "digest:<日期>"，主键冲突就是已经认领过）：同一天只发一次，重启了也不
  重发。认领之后推送失败不补发——通知本来就是至多一次，投递记录里照实记失败。停机错过的那一天不补；
- 认领行只留最近 KEEP_DAYS 天。

访问密钥快到期（key.expiring）：
- 到期前 7 天、1 天各推一次（THRESHOLDS）：个人访问密钥推给拥有者的个人渠道，服务账号的推给订阅了的全局渠道；
- 去重靠审计：推之前先记一条 access_key.expiring（data.days 是哪一档），记过的不再推（进程里另记一份，省得每拍查审计）；
- 不推的：建的时候离到期已经不到这一档（自己设了短有效期，知道它快到期）；轮换下来、24 小时后到期的旧密钥（新的那把已经
  在用）；撤销了的；拥有者停用了的；个人密钥开关关着时的个人密钥（本来就不认了）。

这两种状态（认领行、自动建过邮件渠道的人）存在 settings 表里，键都以 STATE_PREFIX 开头：它们是通知自己记的账，不是用户设置，
GET /api/settings 不列、PUT 不收（api/settings.py）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionLocal, utcnow
from app.db.models import NotifyChannel, Setting, User

logger = logging.getLogger(__name__)

#: settings 表里通知自己记的账（不是用户设置）的键的开头
STATE_PREFIX = "notify_state:"
DIGEST_CLAIM = STATE_PREFIX + "digest:"
#: 汇总的缺省发送时间（服务端所在时区）
DEFAULT_AT = "09:00"
#: 认领行留几天
KEEP_DAYS = 7
#: 访问密钥到期前几天各推一次
THRESHOLDS = (7, 1)

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: 这个进程里已经认领（或者看到别人认领了）的日期：同一天不再去插认领行
_claimed: set[str] = set()
#: 这个进程里已经推过（或者审计里已经有）的 (密钥 id, 档)
_noted: set[tuple[str, int]] = set()


def time_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_TIME.match(value.strip()))


def at_of(stored_notify: Any) -> tuple[int, int]:
    """设置 notify 一组里的 digest_at → (时, 分)；没填、存坏了按缺省。"""
    raw = stored_notify.get("digest_at") if isinstance(stored_notify, dict) else None
    match = _TIME.match(raw.strip()) if isinstance(raw, str) else None
    if match is None:
        match = _TIME.match(DEFAULT_AT)
    assert match is not None
    return int(match.group(1)), int(match.group(2))


async def _claim(day: str, now: datetime) -> bool:
    """认领这一天的汇总：插一行，主键冲突就是已经有人认领过。顺带清掉 KEEP_DAYS 天以前的认领行。"""
    key = DIGEST_CLAIM + day
    async with SessionLocal() as session:
        if await session.get(Setting, key) is not None:
            return False
        session.add(Setting(key=key, value={"claimed_at": now.isoformat()}))
        try:
            await session.commit()
        except IntegrityError:
            return False
        floor = DIGEST_CLAIM + (now.astimezone().date() - timedelta(days=KEEP_DAYS)).isoformat()
        await session.execute(delete(Setting).where(Setting.key.like(DIGEST_CLAIM + "%"), Setting.key < floor))
        await session.commit()
    return True


async def digest_due(now: datetime | None = None) -> list[str]:
    """到点了就认领今天、给订阅了汇总的人各发一条（后台推）。交回这一拍要发给谁（用户 id），没到点、已经发过是空的。"""
    from app.api.settings import NOTIFY_GROUP
    from app.notify import service

    now = now or utcnow()
    local = now.astimezone()
    day = local.date().isoformat()
    if day in _claimed:
        return []
    async with SessionLocal() as session:
        row = await session.get(Setting, NOTIFY_GROUP)
    if (local.hour, local.minute) < at_of(row.value if row is not None else None):
        return []
    claimed = await _claim(day, now)
    _claimed.add(day)
    if not claimed:
        return []
    async with SessionLocal() as session:
        rows = (await session.execute(select(NotifyChannel.owner_id, NotifyChannel.events).where(
            NotifyChannel.scope == "user", NotifyChannel.enabled.is_(True),
            NotifyChannel.owner_id.is_not(None)))).all()
    owners = list(dict.fromkeys(owner for owner, events in rows if "task.digest" in (events or [])))
    for owner in owners:
        service.spawn(_digest_one(owner))
    if owners:
        logger.info("今天（%s）的待办汇总：发给 %d 个人", day, len(owners))
    return owners


async def _digest_one(user_id: str) -> None:
    from app.notify import service

    try:
        await service.notify_digest(user_id)
    except Exception:  # noqa: BLE001 - 一个人的汇总出错不影响别人，待办本身也不受影响
        logger.exception("给用户 %s 发待办汇总失败", user_id)


async def keys_due(now: datetime | None = None) -> list[str]:
    """访问密钥快到期了的，按档推一次（见模块说明）。交回这一拍推了哪几把（密钥 id）。"""
    from app.auth import personal_keys_enabled
    from app.auth.audit import record
    from app.auth.keys import SERVICE_KIND
    from app.db.models import AuditEvent
    from app.db.models_identity import AccessKey
    from app.notify import service

    now = now or utcnow()
    horizon = now + timedelta(days=max(THRESHOLDS))
    async with SessionLocal() as session:
        rows = (await session.execute(select(AccessKey, User).join(User, User.id == AccessKey.owner_user_id).where(
            AccessKey.revoked_at.is_(None), AccessKey.expires_at > now, AccessKey.expires_at <= horizon))).all()
        if not rows:
            return []
        ids = [key.id for key, _ in rows]
        replaced = set((await session.execute(select(AccessKey.rotated_from).where(
            AccessKey.rotated_from.in_(ids)))).scalars())
    out: list[str] = []
    for key, owner in rows:
        if owner.disabled or key.id in replaced:
            continue
        if owner.kind != SERVICE_KIND and not personal_keys_enabled():
            continue
        expires = service._utc(key.expires_at)
        left = expires - now
        days = next(d for d in sorted(THRESHOLDS) if left <= timedelta(days=d))
        created = service._utc(key.created_at) if key.created_at else None
        if created is not None and created > expires - timedelta(days=days):
            continue
        if (key.id, days) in _noted:
            continue
        async with SessionLocal() as session:
            seen = (await session.execute(select(AuditEvent.data).where(
                AuditEvent.action == "access_key.expiring", AuditEvent.subject_id == key.id))).scalars()
            if any(isinstance(d, dict) and d.get("days") == days for d in seen):
                _noted.add((key.id, days))
                continue
            await record(session, "access_key.expiring", "access_key", key.id, actor=None, verified=False,
                         data={"days": days, "expires_at": expires.isoformat(), "name": key.name,
                               "owner": owner.username})
            await session.commit()
        _noted.add((key.id, days))
        service.spawn(_key_one(key.id, days))
        out.append(key.id)
    return out


async def _key_one(key_id: str, days: int) -> None:
    from app.notify import service

    try:
        await service.notify_key_expiring(key_id, days)
    except Exception:  # noqa: BLE001
        logger.exception("访问密钥 %s 快到期的提醒推送失败", key_id)


def forget() -> None:
    """测试用：清掉进程里记的（认领过的日期、推过的密钥档）。"""
    _claimed.clear()
    _noted.clear()
