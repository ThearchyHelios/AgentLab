"""查询快照记下每列的类型（column_types）。

驱动把 DECIMAL 读成 Decimal，快照里统一写成字符串保精度：小数位为 0 的 DECIMAL（"45678"）和
文本列里的 "2026" 在快照里长得一模一样，下游只能按「像数字就当数字」猜。类型要在读结果的那一刻
记下来——那时手上还有驱动给的原始值。拿不准的列（全是空值、类型混着）不记，下游照旧用启发式。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.data.engine import column_types, engines, run_query


def test_types_are_read_from_the_driver_values():
    rows = [
        (Decimal("45678"), "2026", 3, 1.5, None, datetime(2026, 9, 1, 8), date(2026, 9, 1), True, "a"),
        (Decimal("12.50"), "2027", 4, None, None, datetime(2026, 9, 2, 8), date(2026, 9, 2), False, 7),
    ]
    names = ["gmv", "year", "orders", "rate", "empty", "at", "day", "flag", "mixed"]
    assert column_types(names, rows) == {
        "gmv": "number", "year": "text", "orders": "number", "rate": "number",
        "at": "datetime", "day": "date", "flag": "boolean"}


async def test_run_query_puts_column_types_into_the_payload(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER, week TEXT, amount REAL, note TEXT);"
                     "INSERT INTO orders VALUES (1, '2026', 100.5, NULL), (2, '2027', 200.0, NULL);")
    db.commit()
    db.close()
    source = SimpleNamespace(id="column-types", name="shop", kind="sqlite", database=str(path), readonly=True,
                             options={}, host=None, port=None, username=None, password=None)
    try:
        payload = (await run_query(source, "SELECT id, week, amount, note FROM orders")).to_payload()
    finally:
        await engines.invalidate(source.id)
    assert payload["column_types"] == {"id": "number", "week": "text", "amount": "number"}
    assert payload["rows"] == [[1, "2026", 100.5, None], [2, "2027", 200.0, None]]


@pytest.mark.parametrize("rows", [[], [(None,)]])
def test_nothing_is_recorded_when_nothing_can_be_told(rows):
    assert column_types(["x"], rows) == {}
