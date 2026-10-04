"""核查花费账（engine/spend.py）：双盲复算、反方审稿、审批质询的模型调用按本地日期记一本每日账。

照结论句裁判的每日花费做（judge.py 的 SPEND_KEY、_charge、_settle），但是独立的一行设置：

- 调用前按估算预留，加上这笔会超过每日上限就不记、返回 None；上限是 None 表示不限
- 调用后按实际用量找补差额；跨了天的不找补（预留记在前一天，今天从零算）
- 价格目录里没有的模型估不出金额：只计次数（unpriced_calls），不计金额
- 和裁判的账互不挤占：核查花掉的钱不算进裁判的每日上限，反过来也一样
- 并发预留不会一起越过上限
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import Setting
from app.engine import spend
from app.engine import judge as judge_mod


@pytest.fixture(autouse=True)
async def clean(monkeypatch):
    monkeypatch.setattr(spend, "_today", lambda: "2026-10-02")
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([spend.SPEND_KEY, judge_mod.SPEND_KEY])))
        await session.commit()
    yield


async def stored() -> dict | None:
    async with SessionLocal() as session:
        row = await session.get(Setting, spend.SPEND_KEY)
        return row.value if row is not None else None


async def test_a_call_within_the_cap_is_recorded():
    assert await spend.daily_spend() == {"date": "2026-10-02", "usd": 0.0, "calls": 0, "unpriced_calls": 0}
    assert await spend.charge(0.4, limit=1.0, priced=True) == "2026-10-02"
    assert await spend.daily_spend() == {"date": "2026-10-02", "usd": 0.4, "calls": 1, "unpriced_calls": 0}


async def test_a_call_that_would_cross_the_cap_is_refused_and_nothing_is_recorded():
    assert await spend.charge(0.9, limit=1.0, priced=True)
    assert await spend.charge(0.2, limit=1.0, priced=True) is None
    assert await spend.daily_spend() == {"date": "2026-10-02", "usd": 0.9, "calls": 1, "unpriced_calls": 0}
    # 正好到上限的还能记：上限是「不超过」
    assert await spend.charge(0.1, limit=1.0, priced=True) == "2026-10-02"


async def test_no_cap_means_unlimited():
    assert await spend.charge(50.0, limit=None, priced=True) == "2026-10-02"
    assert (await spend.daily_spend())["usd"] == 50.0


async def test_unpriced_calls_are_counted_without_money():
    assert await spend.charge(0.0, limit=0.5, priced=False) == "2026-10-02"
    assert await spend.daily_spend() == {"date": "2026-10-02", "usd": 0.0, "calls": 1, "unpriced_calls": 1}


async def test_yesterdays_spend_does_not_count_today():
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": "2026-10-01", "usd": 1.9, "calls": 7,
                                                        "unpriced_calls": 2}))
        await session.commit()
    assert await spend.daily_spend() == {"date": "2026-10-02", "usd": 0.0, "calls": 0, "unpriced_calls": 0}
    assert await spend.charge(1.5, limit=2.0, priced=True) == "2026-10-02"
    assert await stored() == {"date": "2026-10-02", "usd": 1.5, "calls": 1, "unpriced_calls": 0}


async def test_settle_corrects_the_reservation_and_never_goes_below_zero():
    day = await spend.charge(0.5, limit=None, priced=True)
    await spend.settle(day, -0.2)
    assert (await spend.daily_spend())["usd"] == pytest.approx(0.3)
    await spend.settle(day, 0.15)
    assert (await spend.daily_spend())["usd"] == pytest.approx(0.45)
    await spend.settle(day, -5)
    assert (await spend.daily_spend())["usd"] == 0.0
    assert (await spend.daily_spend())["calls"] == 1, "找补只改金额，不改次数"


async def test_settling_after_midnight_does_not_touch_the_new_day(monkeypatch):
    day = await spend.charge(0.5, limit=None, priced=True)
    monkeypatch.setattr(spend, "_today", lambda: "2026-10-03")
    assert await spend.charge(0.2, limit=None, priced=True) == "2026-10-03"
    await spend.settle(day, -0.4)
    assert (await spend.daily_spend())["usd"] == pytest.approx(0.2), "预留记在前一天，今天从零算"


async def test_the_verify_ledger_is_separate_from_the_judges(monkeypatch):
    monkeypatch.setattr(judge_mod, "_today", lambda: "2026-10-02")
    assert spend.SPEND_KEY != judge_mod.SPEND_KEY
    await spend.charge(1.2, limit=2.0, priced=True)
    assert (await judge_mod.daily_spend())["usd"] == 0.0, "核查花的钱不进裁判的账"
    async with SessionLocal() as session:
        session.add(Setting(key=judge_mod.SPEND_KEY, value={"date": "2026-10-02", "usd": 1.9, "calls": 4}))
        await session.commit()
    assert await spend.charge(0.7, limit=2.0, priced=True) == "2026-10-02", "裁判花的钱也不挤占核查的上限"
    assert (await spend.daily_spend())["usd"] == pytest.approx(1.9)


async def test_concurrent_reservations_never_cross_the_cap_together():
    results = await asyncio.gather(*(spend.charge(0.3, limit=1.0, priced=True) for _ in range(10)))
    assert sum(1 for r in results if r) == 3
    assert (await spend.daily_spend())["usd"] == pytest.approx(0.9)
