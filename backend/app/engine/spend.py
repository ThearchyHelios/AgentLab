"""模型调用的每日花费账：一本账一个键，按本地日期记，先预留、后找补（docs/design/next-round/01-效果度量与回归评测.md 做法 17）。

调用前按目录价估算、先预留，加上这笔会超过每日上限就不记；调用后按实际用量找补差额。账落在设置表里，按本地日期
滚动，不从设置接口进出（api/settings.py 只给只读的「今天花了多少」）。

**一套代码，几本账。** 以前核查（这个模块）和结论句裁判（judge.py 的 _charge、_settle）各写一份一模一样的读改写，评测
要是再抄一份就是第三份。现在都是 Ledger(key) 的实例，各记各的键：

- verify_spend：核查——双盲复算、反方审稿、审批质询里的模型调用；
- judge_spend：结论句裁判（实例在 engine/judge.py，键 judge_spend：日历跟着 judge._today，裁判的测试换它模拟跨天）；
- eval_spend：评测（预留，第 3 波的评测执行接上；设置和接口在那时加）。

**为什么各记各的。** 核查是一个 agent 查库、来回好几轮，一次花的钱比裁判判一份报告多得多；评测一次几十道题。记在同一本
账上，一边一多，另一边当天就全因为「每日上限」做不了——报告的出具会因此降档，而降档的原因和这份报告毫无关系。几本账
各管各的上限，互不挤占。三本按同一个本地日期滚动（_today）。

目录里没有价格的模型估不出金额：只计次数（unpriced_calls），金额上限对它不起作用，由调用方照实说明。

**多进程。** 先读后写以前只靠进程内的锁：两个进程各读到同一个余额，会一起越过上限。现在每次记账先占住设置表里这本账的那一行
（_locked：PostgreSQL 上是行锁，SQLite 上拿到写锁），读、判断、写在同一个事务里，几个进程一个接一个；这本账还没有那一行时
插一行，撞了说明别的进程刚插上，重来一次。进程内的锁留着：同一个进程里的几个并行核查先在本进程排队，少占库的锁。
"""
from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.db.base import SessionLocal
from app.db.models import Setting

#: 每本账的读改写锁：{事件循环: {键: 锁}}。asyncio.Lock 不能跨循环用（测试每个用例一个循环），按循环各一组；同一个键
#: 同一把——几个核查并行时各自预留，不串行的话两边读到同一个余额，会一起越过上限
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()


def _lock(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    group = _locks.get(loop)
    if group is None:
        group = _locks[loop] = {}
    lock = group.get(key)
    if lock is None:
        lock = group[key] = asyncio.Lock()
    return lock


async def _locked(session: Any, key: str) -> Any:
    """占住设置表里 key 这一行再读它（见模块说明「多进程」）：没有这一行交回 None。updated_at 不动。"""
    from sqlalchemy import update

    await session.execute(update(Setting).where(Setting.key == key).values(value=Setting.value,
                                                                           updated_at=Setting.updated_at)
                          .execution_options(synchronize_session=False))
    return await session.get(Setting, key, populate_existing=True)


def _today() -> str:
    """本地日期：每日上限按它滚动。"""
    return datetime.now().astimezone().date().isoformat()


class Ledger:
    """一本每日账，存在设置表 key 这一行：{date: 本地日期, usd, calls, unpriced_calls}。

    today 是这本账的日历（测试换掉它来模拟跨天）；不给就按本模块的 _today，调用时才取，换掉 _today 对没给 today 的几本
    账都生效。"""

    def __init__(self, key: str, *, today: Callable[[], str] | None = None) -> None:
        self.key = key
        self._clock = today

    def _date(self) -> str:
        return self._clock() if self._clock is not None else _today()

    def _spend_of(self, row: Any) -> dict[str, Any]:
        value = row.value if row is not None and isinstance(row.value, dict) else {}
        today = self._date()
        if value.get("date") != today:
            return {"date": today, "usd": 0.0, "calls": 0, "unpriced_calls": 0}
        return {"date": today, "usd": float(value.get("usd") or 0.0), "calls": int(value.get("calls") or 0),
                "unpriced_calls": int(value.get("unpriced_calls") or 0)}

    async def daily(self) -> dict[str, Any]:
        """今天（本地日期）这本账已经花了多少：{date, usd, calls, unpriced_calls}。估不出金额的调用只计次数。"""
        async with SessionLocal() as session:
            return self._spend_of(await session.get(Setting, self.key))

    async def charge(self, amount: float, *, limit: float | None, priced: bool) -> str | None:
        """记一次调用、先按估算预留金额。加上这笔会超过每日上限就不记，返回 None；记下了返回记在哪天（找补时要用）。

        limit 为 None 表示不限。估不出金额的调用（priced=False）照记次数，金额记 0，调用方应传 limit=None：
        拿 0 去比上限，等于说它不花钱。
        """
        from sqlalchemy.exc import IntegrityError

        async with _lock(self.key):
            for _ in range(3):
                async with SessionLocal() as session:
                    row = await _locked(session, self.key)
                    spend = self._spend_of(row)
                    if limit is not None and spend["usd"] + amount > limit + 1e-12:
                        await session.rollback()
                        return None
                    spend["usd"] = round(spend["usd"] + amount, 8)
                    spend["calls"] += 1
                    spend["unpriced_calls"] += 0 if priced else 1
                    if row is None:
                        session.add(Setting(key=self.key, value=spend))
                    else:
                        row.value = spend
                    try:
                        await session.commit()
                    except IntegrityError:
                        # 这本账的那一行刚被别的进程插上：重来一次，这回占得住它
                        await session.rollback()
                        continue
                    return spend["date"]
            raise RuntimeError(f"记账「{self.key}」失败：设置表里这一行一直插不进去")

    async def settle(self, date: str, delta: float) -> None:
        """调用结束后按实际用量找补预留的差额。跨了天就不找补：预留记在前一天，今天从零算。"""
        if not delta:
            return
        async with _lock(self.key):
            async with SessionLocal() as session:
                row = await _locked(session, self.key)
                spend = self._spend_of(row)
                if row is None or spend["date"] != date:
                    await session.rollback()
                    return
                spend["usd"] = max(0.0, round(spend["usd"] + delta, 8))
                row.value = spend
                await session.commit()


#: 核查、评测各一本（裁判那本在 engine/judge.py 的 judge_spend）
verify_spend = Ledger("verify_spend")
eval_spend = Ledger("eval_spend")

#: 核查那本账的老名字（verify_agent、设置接口、已有测试从这里取）
SPEND_KEY = verify_spend.key


async def daily_spend() -> dict[str, Any]:
    """今天核查已经花了多少（verify_spend.daily）。"""
    return await verify_spend.daily()


async def charge(amount: float, *, limit: float | None, priced: bool) -> str | None:
    """核查记一次调用（verify_spend.charge）。"""
    return await verify_spend.charge(amount, limit=limit, priced=priced)


async def settle(date: str, delta: float) -> None:
    """核查找补（verify_spend.settle）。"""
    await verify_spend.settle(date, delta)


__all__ = ["Ledger", "SPEND_KEY", "charge", "daily_spend", "eval_spend", "settle", "verify_spend"]
