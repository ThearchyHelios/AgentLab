"""可比性检查的现查（app/analysis/probe.py）：make_prober 在真的数据源上探表的日期范围、在库里算按运行当天现算的界，遮罩列
不探、超时限不探、现查不成退回剖析事实；Book 先登记、查完再算，结论只记最后真用到的。库是现造的 SQLite 文件，表名、列名、
数字都是虚构的。"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from analysis_samples import CATALOGS, recomputed, snap
from app import analysis
from app.analysis.contract import effective_settings, same
from app.analysis.probe import Book, make_prober
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource

CLOCK = "2026-10-21T09:30:00"
#: SQLite 的 date('now') 按 UTC 算：表里的数据造到 UTC 的昨天
UTC_TODAY = datetime.now(timezone.utc).date()
LAST_DAY = UTC_TODAY - timedelta(days=1)


@pytest.fixture
async def shop(tmp_path):
    """orders：order_date 从 2026-01-01 到昨天（UTC），每天一单；customers 的 signup_date、phone 是遮罩列。"""
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (order_date TEXT, region TEXT, amount INTEGER);"
                     "CREATE TABLE customers (signup_date TEXT, phone TEXT);"
                     "INSERT INTO customers VALUES ('2026-01-05', '13900000000');")
    day = date(2026, 1, 1)
    while day <= LAST_DAY:
        db.execute("INSERT INTO orders VALUES (?, '华东', 100)", (day.isoformat(),))
        day += timedelta(days=1)
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True,
                         options={"mask_columns": ["phone", "signup_date"]})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        await session.refresh(row)
    yield row
    await engines.invalidate(row.id)


async def test_the_live_range_of_a_column(shop):
    prober = make_prober({shop.name: shop}, clock=lambda: CLOCK)
    probe = await prober(shop.name, "orders", "order_date")
    assert probe == {"source": shop.name, "table": "orders", "column": "order_date", "min": "2026-01-01",
                     "max": LAST_DAY.isoformat(), "basis": "live", "at": CLOCK}
    assert prober.dialect(shop.name) == "sqlite" and prober.dialect("nope") is None
    assert await prober("nope", "orders", "order_date") is None


async def test_masked_columns_are_never_probed(shop):
    prober = make_prober({shop.name: shop})
    assert await prober(shop.name, "customers", "signup_date") is None        # 数据源设的遮罩列
    explicit = make_prober({shop.name: shop}, masked={shop.name: ["ORDER_DATE"]})
    assert await explicit(shop.name, "orders", "order_date") is None


async def test_a_failed_probe_falls_back_to_profile_facts(shop):
    async def facts(table):
        return {"at": "2026-10-20T02:00:00", "date_ranges": [{"column": "paid_on", "min": "2026-02-01",
                                                              "max": "2026-10-19"}]}

    prober = make_prober({shop.name: shop}, facts={shop.name: facts}, clock=lambda: CLOCK)
    probe = await prober(shop.name, "payments", "paid_on")                    # 没有这张表：现查不成
    assert probe == {"source": shop.name, "table": "payments", "column": "paid_on", "min": "2026-02-01",
                     "max": "2026-10-19", "basis": "profile", "at": "2026-10-20"}


async def test_the_budget_stops_further_probes(shop):
    prober = make_prober({shop.name: shop}, budget=0)
    await prober(shop.name, "orders", "order_date")
    assert await prober(shop.name, "orders", "order_date") is None


async def test_bounds_are_computed_in_the_database(shop):
    prober = make_prober({shop.name: shop}, clock=lambda: CLOCK)
    probe = await prober.bounds(shop.name, "DATE('now', '-7 days')")
    assert probe["expr"] == "DATE('now', '-7 days')" and probe["basis"] == "live" and probe["at"] == CLOCK
    assert date.fromisoformat(probe["value"]) == UTC_TODAY - timedelta(days=7)
    assert await prober.bounds(shop.name, "no_such_function(1)") is None


def test_the_book_registers_first_and_records_only_what_was_used():
    book = Book({})
    assert book.range("shop", "orders", "order_date") is None and book.bound("shop", "DATE('now')") is None
    assert book.missing == [("range", "shop", "orders", "order_date"), ("bound", "shop", "DATE('now')")]
    found = {"source": "shop", "table": "orders", "column": "order_date", "min": "2026-01-01", "max": "2026-10-18",
             "basis": "live", "at": CLOCK}
    filled = Book({("range", "shop", "orders", "order_date"): found, ("bound", "shop", "DATE('now')"): None})
    assert filled.range("shop", "ORDERS", "Order_Date") is found                 # 表名、列名不分大小写
    filled.bound("shop", "DATE('now')")
    assert filled.missing == [] and filled.used == [found]
    replay = Book([found])                                                         # 复算：记下的就是全部
    assert replay.bound("shop", "DATE('now', '-1 day')") is None and replay.missing == []


async def test_a_full_comparability_check_against_the_database_records_its_probes(shop):
    sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE order_date >= date('now', '{a}') "
           "AND order_date < date('now', '{b}')")
    base = snap("Q1", sql.format(a="-14 days", b="-7 days"), ["gmv"], [[700]], source=shop.name)
    target = snap("Q2", sql.format(a="-7 days", b="0 days"), ["gmv"], [[300]], source=shop.name)
    prober = make_prober({shop.name: shop}, clock=lambda: CLOCK)
    result = await analysis.run({"op": "comparable", "params": {"base_query": "Q1", "target_query": "Q2",
                                                                "measure": "gmv"}},
                                {"Q1": base, "Q2": target}.get, settings=effective_settings(None),
                                context={"today": UTC_TODAY, "probe": prober,
                                         "catalogs": {shop.name: CATALOGS["shop"]}})
    exprs = [p["expr"] for p in result["probes"] if "expr" in p]
    assert exprs == ["DATE('now', '-14 days')", "DATE('now', '-7 days')", "DATE('now', '0 days')"]
    ranges = [p for p in result["probes"] if "table" in p]
    assert [(p["table"], p["column"], p["max"], p["basis"]) for p in ranges] == \
        [("orders", "order_date", LAST_DAY.isoformat(), "live")]
    assert result["values"]["base_days"] == "7" and result["values"]["target_days"] == "7"
    assert result["values"]["target_end"] == LAST_DAY.isoformat()
    assert all(i["dialect"] == "sqlite" for i in result["inputs"])
    assert result["verdict"]["code"] == "comparable" and result["phrase"] == "两期可比"
    assert same(result, recomputed(result, [base, target]))
