"""真实模型验收用的虚构零售库：陷阱确实埋上了、同一个「今天」生成的库一样、追加一天只动那一天。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from tests.fixtures.acceptance import retail

TODAY = date(2026, 10, 6)


def _rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM orders ORDER BY id").fetchall()


def _week_sum(db, region, start, *, by="paid_date", refunds=False):
    end = (start + timedelta(days=6)).isoformat()
    status = "" if refunds else "AND o.status <> 'refunded'"
    return db.execute(
        f"SELECT ROUND(SUM(o.amount), 2) FROM orders o JOIN stores s ON s.id = o.store_id "
        f"WHERE s.region = ? AND o.{by} BETWEEN ? AND ? {status}", (region, start.isoformat(), end)).fetchone()[0]


def test_the_same_day_builds_the_same_database(tmp_path):
    a = retail.build(tmp_path / "a.db", TODAY)
    b = retail.build(tmp_path / "b.db", TODAY)
    assert _rows(a) == _rows(b) and len(_rows(a)) > 1000


def test_the_traps_are_in_place(tmp_path):
    path = retail.build(tmp_path / "retail.db", TODAY)
    with sqlite3.connect(path) as db:
        total, refunded, cancelled, lagged, null_paid, last = db.execute(
            "SELECT COUNT(*), SUM(status = 'refunded'), SUM(status = 'cancelled'), "
            "SUM(paid_date IS NOT NULL AND paid_date <> order_date), SUM(paid_date IS NULL), MAX(order_date) "
            "FROM orders").fetchone()
        # 退款约 8%、取消约 3%（取消的没有支付日）、约 15% 隔天支付；订单截止昨天
        assert 0.05 < refunded / total < 0.12 and 0.01 < cancelled / total < 0.06
        assert null_paid == cancelled and 0.08 < lagged / total < 0.25
        assert last == (TODAY - timedelta(days=1)).isoformat()
        # 每个整周：华南都高于华东（反方审稿用的错话「华东最高」站不住）；三种错法的金额和口径要的都不一样
        monday = date.fromisoformat(db.execute("SELECT MIN(order_date) FROM orders").fetchone()[0])
        monday += timedelta(days=(7 - monday.weekday()) % 7)
        weeks = 0
        while monday + timedelta(days=8) < TODAY:
            assert _week_sum(db, "华南", monday) > _week_sum(db, "华东", monday)
            right = _week_sum(db, "华东", monday)
            assert right != _week_sum(db, "华东", monday, refunds=True)
            weeks += 1
            monday += timedelta(days=7)
        assert weeks >= 6
        assert db.execute("SELECT COUNT(DISTINCT phone) FROM customers").fetchone()[0] == retail.CUSTOMERS


def test_appending_a_day_only_touches_that_day(tmp_path):
    path = retail.build(tmp_path / "retail.db", TODAY)
    before = _rows(path)
    yesterday = TODAY - timedelta(days=1)
    added = retail.append_day(path, yesterday, seed=7)
    after = _rows(path)
    assert added > 0 and after[:len(before)] == before and len(after) == len(before) + added
    assert all(r[3] == r[4] == yesterday.isoformat() and r[6] == "paid" for r in after[len(before):])
