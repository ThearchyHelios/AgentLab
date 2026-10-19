"""运行的并发名额（run_slots，docs/design/orchestration-round/00-总方案.md §5 W2-b「优先级与并发」）。

**为什么。** 以前是 runner 里一把进程内的信号量（AGENTLAB_MAX_CONCURRENT_RUNS，缺省 20），谁先到谁先跑：一次评测几十道题、
一批口径召回，就能把 20 个名额占满，问数据、画布试跑在后面排队。多进程之后信号量也只管得了本进程。

**现在。** 每段执行（发起、审批之后接着跑、继续运行；停在审批上的不占）开始前要一个名额，按运行的来源归四类：

  交互 interactive：问数据、画布试跑、正式运行（手动点的）、蓝图试运行、个人应用的刷新——有人在屏幕前等
  应用 app：应用入口、按需视图、外部系统的钩子、接口
  定时 schedule：定时任务（含事件触发）
  系统 system：评测、口径召回、对账、补算、按口径正式出具——服务端替人跑的

每类有全站的并发上限和优先级（设置里「运行并发」一组，管理员可调），另有全站总上限。名额按库里的计数发：等着的按
优先级高的先、同一优先级先来先得，只要它那一类没到上限、全站没到总上限就发给它。所以评测再多也只占得到系统那一类的
几个名额，问数据不用等它；全站满了的时候，空出来的名额先给交互。已经在跑的不抢。

缺省（总上限取 AGENTLAB_MAX_CONCURRENT_RUNS）：交互 = 总上限、应用 2/5、定时 1/5、系统 1/5（至少各 1），优先级 40 / 30 / 20 / 10。
应用、定时、系统三类加起来不到总上限：交互总留得出名额。单进程时总上限和以前的信号量一样大，问数据、试跑不再被评测挤占。

**怎么发。** 要名额的一方先在本进程登记、再插一行「等着」，然后自己去发一次（grant：把所有等着的按规矩过一遍，谁够格就
改成「已发」）；没轮到就等叫醒（本进程放名额时叫醒，PostgreSQL 上别的进程放名额时 NOTIFY slot 叫醒），另隔一会儿自己再
发一次兜底。发的那一下全站串行：PostgreSQL 上在事务里取咨询锁，SQLite 只有一个进程，本进程的锁就够。名额用完删掉那一行。
属主不在了的行（进程没了；本进程里登记已经没了的——测试里事件循环关了留下的）发的时候顺手删掉，名额不会漏。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select, text, update

from app.cluster import identity
from app.cluster.signal import signals
from app.core.config import settings
from app.db.base import SessionLocal, utcnow

logger = logging.getLogger(__name__)

CLASSES = ("interactive", "app", "schedule", "system")
CLASS_LABEL = {"interactive": "交互", "app": "应用", "schedule": "定时", "system": "系统"}
#: 运行来源 → 类别（engine/origin.ORIGIN_KINDS）。没记来源的（直接调 run_manager.start、升级前的）算交互：同以前不设限
ORIGIN_CLASS = {
    "chat": "interactive", "canvas": "interactive", "launcher": "interactive", "trial": "interactive",
    "personal_app": "interactive",
    "app_entry": "app", "app_view": "app", "hook": "app", "api": "app",
    "schedule": "schedule",
    "eval": "system", "recall": "system", "reconcile": "system", "app_backfill": "system", "ledger_issue": "system",
}
SETTING_KEY = "concurrency"
DEFAULT_PRIORITY = {"interactive": 40, "app": 30, "schedule": 20, "system": 10}
#: 上限、优先级的取值范围
LIMIT_MAX = 1000
PRIORITY_MAX = 100
#: 等名额时隔多久自己再发一次（秒）：PostgreSQL 上别的进程放的名额靠 NOTIFY 叫醒，这是兜底；SQLite 只有本进程放名额
POLL_S = {"postgresql": 1.0, "sqlite": 5.0}
#: 设置读一次管多久（秒）：改了设置，最迟这么久之后各进程按新的发
POLICY_TTL_S = 3.0
#: 咨询锁的号（发名额那一下全站串行）
_GRANT_KEY = 0x4167536C6F747321  # "AgSlots!"


def class_of(origin_kind: str | None) -> str:
    return ORIGIN_CLASS.get(origin_kind or "", "interactive")


def defaults(total: int | None = None) -> dict[str, Any]:
    """缺省的设置（总上限取 AGENTLAB_MAX_CONCURRENT_RUNS）。"""
    total = max(1, int(total if total is not None else settings.max_concurrent_runs))
    share = {"interactive": total, "app": max(1, total * 2 // 5), "schedule": max(1, total // 5),
             "system": max(1, total // 5)}
    return {"max_total": total,
            "classes": {k: {"limit": share[k], "priority": DEFAULT_PRIORITY[k]} for k in CLASSES}}


def values(stored: Any) -> dict[str, Any]:
    """存着的（可能缺项、存坏了）→ 生效的：缺的、坏的补缺省。"""
    base = defaults()
    out = {"max_total": base["max_total"], "classes": {k: dict(v) for k, v in base["classes"].items()}}
    if not isinstance(stored, dict):
        return out
    total = stored.get("max_total")
    if isinstance(total, int) and not isinstance(total, bool) and 1 <= total <= LIMIT_MAX:
        out["max_total"] = total
    classes = stored.get("classes") if isinstance(stored.get("classes"), dict) else {}
    for k in CLASSES:
        item = classes.get(k) if isinstance(classes.get(k), dict) else {}
        limit, prio = item.get("limit"), item.get("priority")
        if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= LIMIT_MAX:
            out["classes"][k]["limit"] = limit
        if isinstance(prio, int) and not isinstance(prio, bool) and 0 <= prio <= PRIORITY_MAX:
            out["classes"][k]["priority"] = prio
    return out


def problem(sent: Any) -> str | None:
    """设置接口写进来的这一组有没有问题（说明给人看）。只核对给了的项。"""
    if not isinstance(sent, dict):
        return "运行并发的设置需要是对象"
    total = sent.get("max_total")
    if total is not None and (not isinstance(total, int) or isinstance(total, bool) or not 1 <= total <= LIMIT_MAX):
        return f"全站同时运行的上限需要是 1 到 {LIMIT_MAX} 之间的整数"
    classes = sent.get("classes")
    if classes is None:
        return None
    if not isinstance(classes, dict):
        return "各类运行的设置需要是对象"
    for k, item in classes.items():
        if k not in CLASSES:
            return f"没有「{k}」这一类运行（只有交互、应用、定时、系统）"
        if not isinstance(item, dict):
            return f"「{CLASS_LABEL[k]}」类的设置需要是对象"
        limit, prio = item.get("limit"), item.get("priority")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= LIMIT_MAX):
            return f"「{CLASS_LABEL[k]}」类的并发上限需要是 1 到 {LIMIT_MAX} 之间的整数"
        if prio is not None and (not isinstance(prio, int) or isinstance(prio, bool) or not 0 <= prio <= PRIORITY_MAX):
            return f"「{CLASS_LABEL[k]}」类的优先级需要是 0 到 {PRIORITY_MAX} 之间的整数"
    return None


def merged(saved: Any, sent: dict[str, Any]) -> dict[str, Any]:
    """只给了一部分的（只改一类的上限）补上存着的另几项。"""
    out = values(saved)
    if "max_total" in sent and sent["max_total"] is not None:
        out["max_total"] = sent["max_total"]
    for k, item in (sent.get("classes") or {}).items():
        for field_ in ("limit", "priority"):
            if item.get(field_) is not None:
                out["classes"][k][field_] = item[field_]
    return out


_policy: tuple[float, dict[str, Any]] | None = None


async def policy(*, fresh: bool = False) -> dict[str, Any]:
    """现在生效的设置（读一次管 POLICY_TTL_S 秒）。"""
    global _policy
    now = time.monotonic()
    if not fresh and _policy is not None and now - _policy[0] < POLICY_TTL_S:
        return _policy[1]
    from app.db.models import Setting

    async with SessionLocal() as session:
        row = await session.get(Setting, SETTING_KEY)
    value = values(row.value if row is not None else None)
    _policy = (now, value)
    return value


def forget_policy() -> None:
    """设置刚改了：本进程下一次重读（别的进程最迟 POLICY_TTL_S 秒）。"""
    global _policy
    _policy = None


# --------------------------------------------------------------------------
# 本进程里要名额的登记
# --------------------------------------------------------------------------


@dataclass
class _Holder:
    run_id: str
    loop: asyncio.AbstractEventLoop
    woke: asyncio.Event = field(default_factory=asyncio.Event)


_holders: dict[str, _Holder] = {}
_grant_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _local(run_id: str) -> bool:
    """这一行在本进程里还有人在要 / 在用（别的事件循环留下的不算：那个循环已经没人会放它了）。"""
    holder = _holders.get(run_id)
    if holder is None:
        return False
    try:
        return holder.loop is asyncio.get_running_loop() and not holder.loop.is_closed()
    except RuntimeError:
        return False


def wake_local() -> None:
    for holder in list(_holders.values()):
        if not holder.loop.is_closed():
            holder.loop.call_soon_threadsafe(holder.woke.set)


def _grant_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _grant_locks.get(loop)
    if lock is None:
        lock = _grant_locks[loop] = asyncio.Lock()
    return lock


async def grant() -> list[str]:
    """把等着的按规矩过一遍，够格的改成已发；属主不在了的行删掉。交回这一次发出去的。全站串行（见模块说明）。"""
    from app.cluster import state
    from app.db.models_cluster import RunSlot

    pol = await policy()
    async with _grant_lock():
        async with SessionLocal() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _GRANT_KEY})
            rows = list((await session.execute(select(RunSlot))).scalars())
            live = await state.live_for([r.owner for r in rows])
            dead = [r.run_id for r in rows if not live.alive(r.owner, local=_local(r.run_id))]
            if dead:
                await session.execute(delete(RunSlot).where(RunSlot.run_id.in_(dead)))
            rows = [r for r in rows if r.run_id not in set(dead)]
            used = Counter(r.klass for r in rows if r.granted)
            total = sum(used.values())
            limits = {k: int(v["limit"]) for k, v in pol["classes"].items()}
            granted: list[str] = []
            waiting = sorted((r for r in rows if not r.granted),
                             key=lambda r: (-int(r.priority or 0), r.requested_at, r.run_id))
            now = utcnow()
            for row in waiting:
                if total >= int(pol["max_total"]):
                    break
                if used[row.klass] >= limits.get(row.klass, int(pol["max_total"])):
                    continue
                used[row.klass] += 1
                total += 1
                granted.append(row.run_id)
            if granted:
                await session.execute(update(RunSlot).where(RunSlot.run_id.in_(granted))
                                      .values(granted=True, granted_at=now).execution_options(synchronize_session=False))
            await session.commit()
    if dead:
        logger.info("运行名额：收回了 %d 个属主已经不在的名额", len(dead))
    if granted:
        wake_local()
        signals.publish("slot")
    return granted


async def _granted(run_id: str) -> bool | None:
    """这一行发了没有：True 发了，False 还在等，None 这一行不见了（被当成属主不在了删掉）。"""
    from app.db.models_cluster import RunSlot

    async with SessionLocal() as session:
        return await session.scalar(select(RunSlot.granted).where(RunSlot.run_id == run_id))


async def _request(run_id: str, klass: str, priority: int) -> None:
    from app.db.models_cluster import RunSlot

    async with SessionLocal() as session:
        await session.execute(delete(RunSlot).where(RunSlot.run_id == run_id))
        session.add(RunSlot(run_id=run_id, klass=klass, priority=priority, owner=identity.owner(), granted=False,
                            requested_at=utcnow()))
        await session.commit()


async def _release(run_id: str) -> None:
    from app.db.models_cluster import RunSlot

    async with SessionLocal() as session:
        await session.execute(delete(RunSlot).where(RunSlot.run_id == run_id, RunSlot.owner == identity.owner()))
        await session.commit()


@contextlib.asynccontextmanager
async def hold(run_id: str, origin_kind: str | None) -> AsyncIterator[str]:
    """要一个名额（等到发给自己），出来时放掉。交回类别。取消（停止运行）在等的时候也照常生效。"""
    klass = class_of(origin_kind)
    pol = await policy()
    priority = int(pol["classes"][klass]["priority"])
    holder = _Holder(run_id, asyncio.get_running_loop())
    _holders[run_id] = holder
    poll = POLL_S.get(settings.db_dialect, 5.0)
    try:
        await _request(run_id, klass, priority)
        while True:
            holder.woke.clear()
            await grant()
            got = await _granted(run_id)
            if got:
                break
            if got is None:
                await _request(run_id, klass, priority)
                continue
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(holder.woke.wait(), timeout=poll)
        yield klass
    finally:
        if _holders.get(run_id) is holder:
            _holders.pop(run_id, None)
        try:
            await asyncio.shield(_release(run_id))
        except Exception:  # noqa: BLE001 - 放不掉的名额下次发的时候按「本进程里已经没人用」收回
            logger.exception("运行 %s 的名额没放掉，下次发名额时收回", run_id)
        wake_local()
        signals.publish("slot")


async def snapshot() -> dict[str, Any]:
    """现在各类在跑、在等几个（系统信息、验收脚本看）。"""
    from app.db.models_cluster import RunSlot

    async with SessionLocal() as session:
        rows = list((await session.execute(select(RunSlot.klass, RunSlot.granted))).all())
    out = {k: {"running": 0, "waiting": 0} for k in CLASSES}
    for klass, granted in rows:
        out.setdefault(klass, {"running": 0, "waiting": 0})["running" if granted else "waiting"] += 1
    return {"policy": await policy(), "classes": out}


__all__ = ["CLASSES", "CLASS_LABEL", "ORIGIN_CLASS", "SETTING_KEY", "class_of", "defaults", "forget_policy", "grant",
           "hold", "merged", "policy", "problem", "snapshot", "values", "wake_local"]
