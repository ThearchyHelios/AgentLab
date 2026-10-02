"""名字规范化：导入和证据层共用的口径。"""
from __future__ import annotations

import pytest

from app.data.names import canon, collide_key, name_key, name_problem, to_sql_name


@pytest.mark.parametrize(("raw", "expected"), [
    ("8-9", "8-9"),
    ("8–9", "8-9"),            # en dash
    ("8—9", "8-9"),            # em dash
    ("8−9", "8-9"),            # 减号
    ("８－９", "8-9"),          # 全角
    (" 18-22  时合计 ", "18-22 时合计"),
    ("金​额", "金额"),     # 零宽空格（Cf）
    ("（人次）", "(人次)"),
])
def test_canon(raw, expected):
    assert canon(raw) == expected


@pytest.mark.parametrize("name", ["日客流", "sales", "Sales_2026", "时段客流_表内合计", "a"])
def test_good_names(name):
    assert name_problem(name) is None


@pytest.mark.parametrize("name", [
    "", "日客流ㅤ", "ＡＢＣ", "①期", "a b", "2026销售", "金额(元)", "sqlite_master", "select",
    "x" * 49, "a​b", "é",
])
def test_bad_names(name):
    assert name_problem(name)


def test_name_key_matches_sqlite():
    """name_key 和 SQLite 比较标识符完全一致：只对 ASCII 不区分大小写，不做 NFKC。逐对拿真的 SQLite 对照。"""
    import sqlite3

    pairs = [("ABC", "abc", True), ("Дата", "дата", False), ("ＡＢＣ", "abc", False), ("明细", "明细", True),
             ("Ｏrders", "orders", False), ("Sales_2026", "SALES_2026", True)]
    for a, b, same in pairs:
        db = sqlite3.connect(":memory:")
        db.execute(f'CREATE TABLE "{a}" (x)')
        try:
            db.execute(f'CREATE TABLE "{b}" (y)')
            sqlite_same = False
        except sqlite3.OperationalError:        # table … already exists：SQLite 认为是同一个名字
            sqlite_same = True
        db.close()
        assert sqlite_same is same, (a, b)
        assert (name_key(a) == name_key(b)) is same, (a, b)


def test_collide_key_is_stricter():
    assert collide_key("Дата") == collide_key("дата")
    assert collide_key("ABC") == collide_key("abc")


@pytest.mark.parametrize(("header", "expected"), [
    ("金额(元)", "金额_元"),
    ("销量\n(件)", "销量_件"),
    ("单价[元]", "单价_元"),
    ("Unit Price", "Unit_Price"),
    ("2026年", "c_2026年"),
    ("order", "order_"),
    ("sqlite_x", "t_sqlite_x"),
    ("", "col_3"),
    (None, "col_3"),
    ("（）", "col_3"),
    ("全日客流（人次）", "全日客流_人次"),
])
def test_to_sql_name(header, expected):
    out = to_sql_name(header, fallback="col_3")
    assert out == expected
    assert name_problem(out) is None
