"""每日账泛化成 Ledger(key)（docs/design/next-round/01-效果度量与回归评测.md 做法 17）。

核查、裁判以前各写一份一模一样的「预留 → 找补 → 按本地日期滚动」，评测要是再抄一份就是第三份。现在三本账是同一个
Ledger 的三个实例，各记各的键、各管各的上限：一本触顶不挤占另外两本。裁判、核查原有的行为（模块级的 SPEND_KEY、
daily_spend、charge / settle）不变，已有的测试照旧守着；这里守账本本身和互不挤占。
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import Setting
from app.engine import judge, spend
from app.engine.spend import Ledger, eval_spend, judge_spend, verify_spend


@pytest.fixture(autouse=True)
async def _clean():
    keys = [verify_spend.key, judge_spend.key, eval_spend.key, "test_ledger"]
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(keys)))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(keys)))
        await session.commit()


def test_three_books_three_keys():
    assert (verify_spend.key, judge_spend.key, eval_spend.key) == ("verify_spend", "judge_spend", "eval_spend")
    # 老名字照旧指向各自那本：核查的模块函数、裁判的 SPEND_KEY / daily_spend
    assert spend.SPEND_KEY == verify_spend.key
    assert judge.SPEND_KEY == judge_spend.key


async def test_reserve_then_settle_and_the_limit():
    book = Ledger("test_ledger")
    day = await book.charge(0.4, limit=1.0, priced=True)
    assert day is not None
    assert await book.charge(0.7, limit=1.0, priced=True) is None, "加上这笔超过上限就不记"
    await book.settle(day, -0.1)
    got = await book.daily()
    assert got["usd"] == pytest.approx(0.3) and got["calls"] == 1 and got["unpriced_calls"] == 0
    # 估不出金额的照记次数，不比上限
    assert await book.charge(0.0, limit=None, priced=False) is not None
    assert (await book.daily())["unpriced_calls"] == 1


async def test_a_new_day_starts_from_zero():
    day = {"today": "2026-09-28"}
    book = Ledger("test_ledger", today=lambda: day["today"])
    first = await book.charge(0.9, limit=1.0, priced=True)
    assert first == "2026-09-28"
    assert await book.charge(0.2, limit=1.0, priced=True) is None
    day["today"] = "2026-09-29"
    assert await book.charge(0.2, limit=1.0, priced=True) == "2026-09-29"
    await book.settle(first, -0.5)   # 跨了天的预留不找补：今天从零算
    assert (await book.daily())["usd"] == pytest.approx(0.2)


async def test_one_book_hitting_its_cap_does_not_squeeze_the_others():
    """核查花光了当天的额度，裁判、评测照常记：三本账互不挤占。"""
    assert await verify_spend.charge(2.0, limit=2.0, priced=True) is not None
    assert await verify_spend.charge(0.01, limit=2.0, priced=True) is None
    assert await judge_spend.charge(1.0, limit=2.0, priced=True) is not None
    assert await eval_spend.charge(1.5, limit=2.0, priced=True) is not None
    assert (await judge.daily_spend())["usd"] == pytest.approx(1.0)
    assert (await spend.daily_spend())["usd"] == pytest.approx(2.0)
    assert (await eval_spend.daily())["usd"] == pytest.approx(1.5)


async def test_all_books_roll_over_on_the_same_clock(monkeypatch):
    """三本账按同一个本地日期滚动（spend._today）：裁判不再自己另有一份时钟。"""
    monkeypatch.setattr(spend, "_today", lambda: "2026-01-02")
    assert await judge_spend.charge(0.1, limit=None, priced=True) == "2026-01-02"
    assert (await judge.daily_spend())["date"] == "2026-01-02"
    assert (await eval_spend.daily())["date"] == "2026-01-02"
