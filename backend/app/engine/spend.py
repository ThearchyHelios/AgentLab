"""核查花费账：双盲复算、反方审稿、审批质询里的模型调用，按本地日期记一本每日账。

照结论句裁判的每日计数做（judge.py 的 SPEND_KEY、_charge、_settle）：调用前按目录价估算、先预留，加上这笔会超过
每日上限就不记；调用后按实际用量找补差额。账落在设置表里，按本地日期滚动。

**为什么和裁判分开记。** 核查是一个 agent 查库、来回好几轮，一次花的钱比裁判判一份报告多得多。记在同一本账上，
核查一多，当天的裁判就全因为「每日上限」判不了——报告的出具会因此降档，而降档的原因和这份报告毫无关系。两本账
各管各的上限，互不挤占。

目录里没有价格的模型估不出金额：只计次数（unpriced_calls），金额上限对它不起作用，由调用方照实说明。
"""
from __future__ import annotations

import asyncio
import weakref
from datetime import datetime
from typing import Any

from app.db.base import SessionLocal
from app.db.models import Setting

#: 每日计数存在设置表的这一行：{date: 本地日期, usd, calls, unpriced_calls}。不从设置接口进出（api/settings.py）
SPEND_KEY = "verify_spend"

_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    """读改写这一行时持的锁，按事件循环各一把（asyncio.Lock 不能跨循环用）。

    几个核查并行时各自预留：不串行的话两边读到同一个余额，会一起越过上限。
    """
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


def _today() -> str:
    """本地日期：每日上限按它滚动。"""
    return datetime.now().astimezone().date().isoformat()


def _spend_of(row: Any) -> dict[str, Any]:
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    today = _today()
    if value.get("date") != today:
        return {"date": today, "usd": 0.0, "calls": 0, "unpriced_calls": 0}
    return {"date": today, "usd": float(value.get("usd") or 0.0), "calls": int(value.get("calls") or 0),
            "unpriced_calls": int(value.get("unpriced_calls") or 0)}


async def daily_spend() -> dict[str, Any]:
    """今天（本地日期）核查已经花了多少：{date, usd, calls, unpriced_calls}。估不出金额的调用只计次数。"""
    async with SessionLocal() as session:
        return _spend_of(await session.get(Setting, SPEND_KEY))


async def charge(amount: float, *, limit: float | None, priced: bool) -> str | None:
    """记一次调用、先按估算预留金额。加上这笔会超过每日上限就不记，返回 None；记下了返回记在哪天（找补时要用）。

    limit 为 None 表示不限。估不出金额的调用（priced=False）照记次数，金额记 0，调用方应传 limit=None：
    拿 0 去比上限，等于说它不花钱。
    """
    async with _lock():
        async with SessionLocal() as session:
            row = await session.get(Setting, SPEND_KEY)
            spend = _spend_of(row)
            if limit is not None and spend["usd"] + amount > limit + 1e-12:
                return None
            spend["usd"] = round(spend["usd"] + amount, 8)
            spend["calls"] += 1
            spend["unpriced_calls"] += 0 if priced else 1
            if row is None:
                session.add(Setting(key=SPEND_KEY, value=spend))
            else:
                row.value = spend
            await session.commit()
            return spend["date"]


async def settle(date: str, delta: float) -> None:
    """调用结束后按实际用量找补预留的差额。跨了天就不找补：预留记在前一天，今天从零算。"""
    if not delta:
        return
    async with _lock():
        async with SessionLocal() as session:
            row = await session.get(Setting, SPEND_KEY)
            spend = _spend_of(row)
            if row is None or spend["date"] != date:
                return
            spend["usd"] = max(0.0, round(spend["usd"] + delta, 8))
            row.value = spend
            await session.commit()


__all__ = ["SPEND_KEY", "charge", "daily_spend", "settle"]
