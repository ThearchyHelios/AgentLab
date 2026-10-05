"""调度循环：每一拍认领到点的定时任务、发起运行，再顺便查已发起的运行要不要推送通知。

认领靠条件 UPDATE：UPDATE schedules SET next_run_at = 下一次 WHERE id = … AND next_run_at = 旧值。两拍撞在
一起（或者以后多开了一个进程），只有一拍改得动这一行，另一拍拿到 0 行就知道这一次已经有人处理了。触发记录
(任务, fire_at, kind) 唯一是第二道保险。

错过的触发（服务停机、进程卡住）：只看最近应触发的那一次——在宽限内补一次（kind=misfire），超出就记一条
skipped_misfire；更早的积压只记一条跳过，不补。下一次一律推进到现在之后，不会醒来就把积压的一口气全发起。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from sqlalchemy import select, update

from app.db.base import SessionLocal, utcnow
from app.db.models import Schedule, ScheduleFire
from app.scheduler import cron, service

logger = logging.getLogger(__name__)

#: 一拍的间隔（秒）。测试里 start(interval=…) 调小
TICK_SECONDS = 15.0
#: 晚了这么多秒以内都算准点（kind=scheduled），和补发宽限无关：拍与拍之间的十几秒不该算错过
ON_TIME_S = 60
#: stop 等手上那一拍收尾最多等多久，超过就取消
STOP_GRACE_S = 10.0


async def fire_due(now: datetime | None = None) -> list[ScheduleFire]:
    """处理所有到点的任务（enabled 且 next_run_at <= now），返回这一拍记下的触发。一个任务出错不影响别的。"""
    now = now or utcnow()
    async with SessionLocal() as session:
        due = (await session.execute(
            select(Schedule.id, Schedule.next_run_at)
            .where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None), Schedule.next_run_at <= now)
            .order_by(Schedule.next_run_at)
        )).all()
    out: list[ScheduleFire] = []
    for schedule_id, claimed_at in due:
        try:
            out.extend(await _due_one(schedule_id, claimed_at, now))
        except Exception:  # noqa: BLE001 - 一个任务出错不能拖住这一拍的其他任务，也不能让循环停下
            logger.exception("定时任务 %s 这一拍处理失败", schedule_id)
    return out


async def _due_one(schedule_id: str, claimed_at: datetime, now: datetime) -> list[ScheduleFire]:
    async with SessionLocal() as session:
        row = await session.get(Schedule, schedule_id)
        if row is None or not row.enabled or row.next_run_at != claimed_at:
            return []
        cron_expr, tz, grace = row.cron, row.timezone, max(int(row.misfire_grace_s or 0), 0)
    try:
        spec, zone = cron.parse(cron_expr), cron.zone(tz)
        # 最近应触发的那一次。库里的 next_run_at 理应就是一个触发时刻，万一不是（手改过库）就按它算
        latest = max(cron.last_due(spec, zone, now) or claimed_at, claimed_at)
        upcoming = cron.next_fire(spec, zone, now)
    except (cron.CronError, cron.TimezoneError) as e:
        # 存进库里之后才坏掉的表达式（手改过库、时区库升级删了时区）：停止排期，在触发历史里照实说
        if not await _claim(schedule_id, claimed_at, None):
            return []
        logger.warning("定时任务 %s 的 cron 或时区无法解析，已停止排期：%s", schedule_id, e)
        fire = await service._record(schedule_id, claimed_at, "scheduled", "failed_to_start",
                                     f"cron 表达式或时区无法解析，已停止排期：{e}", {})
        return [fire] if fire else []
    if not await _claim(schedule_id, claimed_at, upcoming):
        return []

    out: list[ScheduleFire | None] = []
    lateness = (now - latest).total_seconds()
    if lateness <= max(ON_TIME_S, grace):
        if latest > claimed_at:
            # 最近这一次照常（或补一次），更早的积压只记一条，不补
            last_missed = cron.prev_fire(spec, zone, latest) or claimed_at
            reason = f"服务未运行，错过了 {_span(claimed_at, last_missed, tz)} 的触发；积压的触发不补，只发起最近一次"
            out.append(await service.record_skip(schedule_id, claimed_at, reason))
        kind = "scheduled" if lateness <= ON_TIME_S else "misfire"
        out.append(await service.fire_once(schedule_id, latest, kind))
    else:
        reason = (f"服务未运行，错过了 {_span(claimed_at, latest, tz)} 的触发，"
                  f"超出补发宽限（{service.duration_text(grace)}），没有发起")
        out.append(await service.record_skip(schedule_id, latest, reason))
    return [fire for fire in out if fire is not None]


async def _claim(schedule_id: str, claimed_at: datetime, upcoming: datetime | None) -> bool:
    """把 next_run_at 从 claimed_at 推进到 upcoming。改动了一行才算认领到。

    updated_at 照旧：它说的是任务最近一次被人修改，推进排期不算。version 也不动——乐观锁防的是两个人同时改设置，
    调度推进 next_run_at 不该让正在编辑的人保存时撞上 409。"""
    async with SessionLocal() as session:
        result = await session.execute(
            update(Schedule)
            .where(Schedule.id == schedule_id, Schedule.enabled.is_(True), Schedule.next_run_at == claimed_at)
            .values(next_run_at=upcoming, updated_at=Schedule.updated_at)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result.rowcount == 1


def _span(first: datetime, last: datetime, tz: str) -> str:
    a, b = service.local_text(first, tz), service.local_text(last, tz)
    return a if a == b else f"{a} 至 {b}"


async def tick(now: datetime | None = None) -> None:
    """完整的一拍：发起到点的，再推送该推送的通知。测试和手动调用用；循环里两件事分开跑（见 _Runner._loop）。"""
    from app.scheduler import webhook

    await fire_due(now)
    await webhook.notify_due(now)


async def _notify(webhook) -> None:  # noqa: ANN001 - 传进来的是 app.scheduler.webhook 模块
    """后台跑一遍通知扫描。出错只记日志：这个任务没人 await，异常留在里面只会变成一句「从未取回的异常」。"""
    try:
        await webhook.notify_due()
    except Exception:  # noqa: BLE001
        logger.exception("定时任务的通知推送这一拍出错，下一拍照常进行")


class _Runner:
    """调度循环的任务。照 sandbox/manager.py 回收闲置会话的写法：一个 asyncio 任务，出错记日志接着下一拍。"""

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        #: 通知推送另起任务：Webhook 超时加重试最长要几十秒，不能拖着下一拍的发起。同一时刻只跑一份
        self.notify: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, interval: float = TICK_SECONDS) -> None:
        if self.running():
            return
        self._stopping = asyncio.Event()
        self.task = asyncio.create_task(self._loop(interval, self._stopping), name="agentlab-scheduler")
        logger.info("定时任务调度已启动，每 %s 秒检查一次", interval)

    async def _loop(self, interval: float, stopping: asyncio.Event) -> None:
        from app.scheduler import webhook

        while not stopping.is_set():
            try:
                await fire_due()
                if self.notify is None or self.notify.done():
                    self.notify = asyncio.create_task(_notify(webhook), name="agentlab-scheduler-notify")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 这一拍出错不该让循环停下
                logger.exception("定时任务调度这一拍出错，下一拍照常进行")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=interval)

    async def stop(self) -> None:
        """让循环收完手上这一拍再停；等不到（STOP_GRACE_S）就取消。没 start 过直接返回。"""
        tasks = [t for t in (self.task, self.notify) if t is not None]
        self.task = self.notify = None
        if self._stopping is not None:
            self._stopping.set()
        for task in tasks:
            if task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=STOP_GRACE_S)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception:  # noqa: BLE001 - 停的时候不再追究这一拍的错
                pass
        if tasks:
            logger.info("定时任务调度已停止")


runner = _Runner()
