"""报告自查认「遮罩列」和证据面板同一个口径：遮罩按列名设在整个数据源上，哪张表（含视图）里叫这个名字的列都算。

复核发现（第 4 轮）：
- 第 3 轮给自查加了「遮罩表」的推断：遮罩列在几张表里都有、其中有的表另有别的遮罩列时，只认那几张表（customers 有 name
  和 phone，stores 只有 name，就只认客户表）。视图的列看不出取自哪张表，没有第二个遮罩列的表（employees.name）也被排除：
  经视图 customer_spend 引用客户姓名、引用店员姓名，正式运行照常成功，姓名写进报告；证据面板照列名把同一格遮成「已遮罩」
- 现在不再推断遮罩表：引用的那一格按这份快照的 SQL 认出取自任何一张表里叫这个名字的列，就记 masked_cell，门店名
  （stores.name）也算，和证据面板的遮法一致。违规说明照实写取自哪张表的哪一列，并说明遮罩按列名对所有表生效
- SQL 血缘只用来点名出处，和排除值确实取自别的列的（SELECT region AS phone FROM stores：stores 是表结构里的普通表）；
  视图、表结构里没有的表、没有冻结表结构的快照看不出列的出处，照算

正文里写着的字照旧不按值认（第 3 轮的结论不变）：两个字的姓名会撞上「销售高峰」，照抄的号码由「数字没有出处」拦下。

真跑的部分用虚构的 SQLite 零售库（号码、姓名、门店名都是编的），数据源探查过表结构（查询快照里冻结着表结构，视图标着
is_view），写作者是 MockChatModel 照剧本写。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import SCHEMA_SNAPSHOT, compose_doc
from app.engine.runner import run_manager

SOURCE = "m2shop"
STORES = [(1, "杭州西湖店", "华东"), (2, "广州天河店", "华南")]
CUSTOMERS = [(1, "陈思远", "19900000801"), (2, "高峰", "19900000802"), (3, "林若溪", "19900000803")]
EMPLOYEES = [(1, "王建国", 1), (2, "赵晓梅", 2)]
ORDERS = [(1, 1, 1, 1, 120.5), (2, 2, 2, 2, 300.0), (3, 1, 2, 1, 80.0), (4, 2, 3, 2, 210.25)]
PHONES = [c[2] for c in CUSTOMERS]
NAMES = [c[1] for c in CUSTOMERS] + [e[1] for e in EMPLOYEES]

SQL_STORE = ("SELECT s.name AS store, ROUND(SUM(o.amount), 2) AS sales FROM stores s JOIN orders o ON o.store_id = s.id "
             "GROUP BY s.name ORDER BY sales DESC")
SQL_CUSTOMER = ("SELECT c.name AS customer, ROUND(SUM(o.amount), 2) AS spend FROM customers c "
                "JOIN orders o ON o.customer_id = c.id GROUP BY c.name ORDER BY spend DESC")


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def set_source(path, options: dict, *, probe: bool = True) -> str:
    """数据源 m2shop：probe 时探查表结构（工具查询时把它冻结进快照），不然清空。名字只这个文件用，探查的结构不会
    冻结进别的测试的快照。"""
    from app.data.introspect import introspect

    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options)
        row.schema_cache = (await introspect(row)) if probe else None
        await session.commit()
        return row.id


@pytest.fixture
async def shop(tmp_path, engine_up):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT, region TEXT);"
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);"
        "CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, store_id INTEGER);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, store_id INTEGER, customer_id INTEGER, clerk_id INTEGER,"
        " amount REAL);"
        # 视图里装的就是客户姓名：表结构里只看得到它的列名 name、spend
        "CREATE VIEW customer_spend AS SELECT c.name AS name, SUM(o.amount) AS spend FROM customers c "
        "JOIN orders o ON o.customer_id = c.id GROUP BY c.name;")
    db.executemany("INSERT INTO stores VALUES (?, ?, ?)", STORES)
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.executemany("INSERT INTO employees VALUES (?, ?, ?)", EMPLOYEES)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", ORDERS)
    db.commit()
    db.close()
    source_id = await set_source(path, {"mask_columns": ["name", "phone"]})
    await engines.invalidate(source_id)
    yield path
    await set_source(path, {}, probe=False)
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(sql: str, **report) -> dict:
    nodes = [node("start", "input"), node("top", "tool", tool=f"db_query__{SOURCE}", args={"sql": sql}),
             node("write", "report", instructions="写一句", **report),
             node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def finish(monkeypatch, spec: dict, text: str) -> Run:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))
    run_id = (await run_manager.start(graph=spec, input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def checked_of(run_id: str) -> dict:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "report.checked")
        return (await session.execute(q)).scalars().one().data


def masked_of(violations: list[dict]) -> list[dict]:
    return [v for v in violations if v["code"] == "masked_cell"]


def leaks(value) -> list[str]:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


def named(value) -> list[str]:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [n for n in NAMES if n in raw]


# --------------------------------------------------------------------------
# 正文里写着的字不按值认
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, text", [
    # 客户里有一位叫「高峰」：常用词「销售高峰」不是在写这位客户
    (SQL_CUSTOMER, "本周销售高峰出现在周五，最高一位客户消费 [[v:Q1.r0.spend]]。"),
    # 正文用文字写门店名：不是引用，自查不认（引用门店名才算，见下）
    (SQL_STORE, "销售额最高的门店是广州天河店，销售额 [[v:Q1.r0.sales]]。"),
])
async def test_words_that_happen_to_equal_masked_values_do_not_fail_a_formal_report(monkeypatch, shop, sql, text):
    """违规即失败（正式运行的默认值）：正文里的字和遮罩列中的值相同，不判 masked_cell，运行照常成功。"""
    row = await finish(monkeypatch, graph(sql, on_violation="fail"), text)
    assert row.status == "succeeded", row.error
    checked = await checked_of(row.id)
    assert masked_of(checked["violations"]) == [] and checked["ok"] is True, checked["violations"]


async def test_a_phone_copied_into_the_text_is_stopped_by_the_number_check(monkeypatch, shop):
    """照抄成正文的号码不按值认，「数字没有出处」照样拦下：交回重写、仍不改就判失败；违规里没有 masked_cell。"""
    row = await finish(monkeypatch, graph(SQL_CUSTOMER, on_violation="fail"),
                       f"消费最高的客户电话 {PHONES[1]}，共消费 [[v:Q1.r0.spend]]。")
    assert row.status == "failed", row.error
    checked = await checked_of(row.id)
    assert {v["code"] for v in checked["violations"]} == {"uncited_number"}, checked["violations"]
    assert "遮罩" not in (row.error or "")


# --------------------------------------------------------------------------
# 引用：哪张表（含视图）里叫这个名字的列都算
# --------------------------------------------------------------------------

#: (说明, SQL, 正文, 被引用的那一格的值, 违规说明里点名的出处)
CITED = [
    ("经视图引用客户姓名（改了名字）", "SELECT name AS customer, spend FROM customer_spend ORDER BY spend DESC",
     "消费最高的客户是 [[v:Q1.r0.customer]]，共消费 [[v:Q1.r0.spend]]。", "林若溪",
     "按 SQL 认出取自 customer_spend.name"),
    ("经视图引用客户姓名（列名照旧）", "SELECT name, spend FROM customer_spend ORDER BY spend DESC",
     "消费最高的客户是 [[v:Q1.r0.name]]，共消费 [[v:Q1.r0.spend]]。", "林若溪", "列「name」取自 customer_spend.name"),
    ("店员姓名：所在的表里没有第二个遮罩列",
     "SELECT e.name AS clerk, SUM(o.amount) AS sales FROM employees e JOIN orders o ON o.clerk_id = e.id "
     "GROUP BY e.name ORDER BY sales DESC",
     "业绩最高的店员是 [[v:Q1.r0.clerk]]，销售额 [[v:Q1.r0.sales]]。", "赵晓梅", "按 SQL 认出取自 employees.name"),
    ("门店名：和客户姓名同叫 name", SQL_STORE, "销售额最高的门店是 [[v:Q1.r0.store]]，销售额 [[v:Q1.r0.sales]]。",
     "广州天河店", "按 SQL 认出取自 stores.name"),
    ("客户姓名", SQL_CUSTOMER, "消费最高的客户是 [[v:Q1.r0.customer]]，共消费 [[v:Q1.r0.spend]]。", "林若溪",
     "按 SQL 认出取自 customers.name"),
]


@pytest.mark.parametrize("label, sql, text, value, origin", CITED, ids=[c[0] for c in CITED])
async def test_a_cited_masked_name_fails_the_run_whatever_table_it_comes_from(monkeypatch, shop, label, sql, text,
                                                                             value, origin):
    """违规即失败：引用的那一格取自叫 name 的列（视图、只有一个遮罩列的表、门店表都算），判 masked_cell，运行失败，
    成果里没有这个值；说明点名取自哪张表的哪一列，说清遮罩按列名对所有表生效。"""
    row = await finish(monkeypatch, graph(sql, on_violation="fail"), text)
    assert row.status == "failed", label
    [v] = masked_of((await checked_of(row.id))["violations"])
    assert origin in v["message"] and "数据源对列名「name」设置了遮罩" in v["message"], v["message"]
    assert "按列名对所有表生效" in v["message"] and value not in v["message"], v["message"]
    assert origin in row.error and value not in row.error, row.error
    async with SessionLocal() as session:
        run = await session.get(Run, row.id)
    assert value not in json.dumps(run.output, ensure_ascii=False, default=str), run.output


async def test_flagging_keeps_the_report_and_names_the_view(monkeypatch, shop):
    """违规处置为标注：照常产出，违规记着取自视图的哪一列；违规本身不写值。"""
    row = await finish(monkeypatch, graph(CITED[0][1], on_violation="flag"), CITED[0][2])
    assert row.status == "succeeded", row.error
    [v] = masked_of((await checked_of(row.id))["violations"])
    assert v["ref"] == "v:Q1.r0.customer" and "customer_spend.name" in v["message"] and named(v) == [], v


# --------------------------------------------------------------------------
# 认法：_cited_masks（报告节点经 _MaskedCells 调用）
# --------------------------------------------------------------------------

#: 冻结的表结构（introspect 的形状）：普通表标 is_view=False；视图 customer_spend 装着客户姓名，contact_book 的 contact
#: 装着号码（视图的列看不出取自哪张表）
TABLES = {"stores": ["id", "name", "region"], "customers": ["id", "name", "phone"],
          "employees": ["id", "name", "store_id"], "orders": ["id", "store_id", "customer_id", "clerk_id", "amount"]}
VIEWS = {"customer_spend": ["name", "spend"], "contact_book": ["who", "contact"]}


async def schema_of(tables: dict[str, list[str]], views: dict[str, list[str]] | None = None, *,
                    marked: bool = True) -> str:
    """表结构快照。marked=False 时不写 is_view（看不出是表还是视图的老形状）。"""
    def meta(cols: list[str], view: bool) -> dict:
        return {"columns": [{"name": c, "type": "TEXT"} for c in cols], **({"is_view": view} if marked else {})}

    return await artifact_store.put_json(
        {"source": SOURCE, "tables": {**{t: meta(cols, False) for t, cols in tables.items()},
                                      **{t: meta(cols, True) for t, cols in (views or {}).items()}}},
        kind=SCHEMA_SNAPSHOT)


async def cited(sql: str, columns: list[str], *, masks: list[str], schema: bool = True, marked: bool = True,
                tables: dict[str, list[str]] = TABLES, views: dict[str, list[str]] = VIEWS) -> dict[str, list[str]]:
    """一份快照按报告自查认出的列：{结果列: 它的出处（认得出表的写「表.列」，认不出的是全部遮罩列）}。"""
    from app.api.evidence import _cited_masks, _Masks

    await set_source(":memory:", {"mask_columns": masks}, probe=False)
    snap = {"sql": sql, "columns": columns, "rows": [], "source": SOURCE,
            **({"schema_artifact": await schema_of(tables, views, marked=marked)} if schema else {})}
    art = await artifact_store.put_json(snap, kind="query")
    return await _cited_masks(snap, SOURCE, _Masks(), art)


MIXED = ("SELECT s.name AS store, c.name AS customer, c.phone AS tel, SUM(o.amount) AS spend FROM orders o "
         "JOIN stores s ON s.id = o.store_id JOIN customers c ON c.id = o.customer_id GROUP BY s.name, c.name, c.phone")


@pytest.mark.parametrize("label, sql, columns, masks, expected", [
    ("两张表的同名列在一条查询里：都算，各自点名", MIXED, ["store", "customer", "tel", "spend"], ["name", "phone"],
     {"store": ["stores.name"], "customer": ["customers.name"], "tel": ["customers.phone"]}),
    ("只设了 name", MIXED, ["store", "customer", "tel", "spend"], ["name"],
     {"store": ["stores.name"], "customer": ["customers.name"]}),
    ("直接选出门店表的 name", "SELECT name, region FROM stores", ["name", "region"], ["name", "phone"],
     {"name": ["stores.name"]}),
    ("* 展开门店表", "SELECT * FROM stores", ["id", "name", "region"], ["name", "phone"], {"name": ["stores.name"]}),
    ("* 展开客户表", "SELECT * FROM customers", ["id", "name", "phone"], ["name", "phone"],
     {"name": ["customers.name"], "phone": ["customers.phone"]}),
    ("店员表只有一个遮罩列", "SELECT e.name AS clerk FROM employees e", ["clerk"], ["name", "phone"],
     {"clerk": ["employees.name"]}),
    ("经视图（改了名字）", "SELECT name AS customer, spend FROM customer_spend", ["customer", "spend"], ["name", "phone"],
     {"customer": ["customer_spend.name"]}),
    ("视图的列名不是遮罩列、改成了遮罩列的名字：看不出取自哪里，照算", "SELECT contact AS phone FROM contact_book",
     ["phone"], ["phone"], {"phone": ["phone"]}),
    ("经 WITH 转两道", "WITH a AS (SELECT c.phone AS tel FROM customers c), b AS (SELECT tel AS contact FROM a) "
     "SELECT contact FROM b", ["contact"], ["phone"], {"contact": ["customers.phone"]}),
    ("列名和遮罩列一样，值却取自普通表的别的列", "SELECT region AS phone FROM stores", ["phone"], ["phone"], {}),
    ("UNION：两支都取自叫 name 的列", "SELECT name FROM stores UNION ALL SELECT name FROM customers", ["name"],
     ["name", "phone"], {"name": ["stores.name", "customers.name"]}),
    ("UNION：一支取自普通表的别的列，另一支经视图", "SELECT region AS name FROM stores UNION ALL "
     "SELECT name FROM customer_spend", ["name"], ["name"], {"name": ["customer_spend.name"]}),
])
async def test_cited_masks_count_the_column_name_in_every_table(engine_up, label, sql, columns, masks, expected):
    assert await cited(sql, columns, masks=masks) == expected, label


@pytest.mark.parametrize("label, sql, kwargs", [
    ("没有冻结的表结构：看不出 stores 是表还是视图", "SELECT region AS phone FROM stores", {"schema": False}),
    ("表结构没标 is_view", "SELECT region AS phone FROM stores", {"marked": False}),
    ("表结构里没有这张表", "SELECT region AS phone FROM stores", {"tables": {"customers": ["id", "name", "phone"]}}),
    ("写了库名：表结构探查的是另一个库，同名的表可能是视图", "SELECT s.region AS phone FROM archive.stores s", {}),
])
async def test_a_column_whose_origin_cannot_be_seen_is_flagged(engine_up, label, sql, kwargs):
    """值看起来取自别的列（region AS phone），可这张表看不出是不是视图：排除不了，照列名算（和证据面板一样遮）。"""
    assert await cited(sql, ["phone"], masks=["phone"], **kwargs) == {"phone": ["phone"]}, label


async def test_without_a_frozen_schema_the_sql_still_names_the_tables(engine_up):
    """数据源没探查过表结构：SQL 里写了表的照样点名出处；认不出、排除不了的照算。"""
    assert await cited(MIXED, ["store", "customer", "tel", "spend"], masks=["name", "phone"], schema=False) == \
        {"store": ["stores.name"], "customer": ["customers.name"], "tel": ["customers.phone"]}


@pytest.mark.parametrize("sql, columns", [
    # 表值函数：拆的是号码，血缘认不出出处，照遮罩那一套认
    ("SELECT x.value AS p FROM customers c, json_each(c.phone) x", ["p"]),
    # 没起别名的表达式：血缘按列名找不到这一列
    ("SELECT id, UPPER(phone) FROM customers", ["id", "UPPER(phone)"]),
])
async def test_columns_the_lineage_cannot_place_stay_flagged(engine_up, sql, columns):
    assert set(await cited(sql, columns, masks=["name", "phone"])) == {columns[-1].lower()}


async def test_the_report_node_flags_every_cited_name_column(engine_up):
    """报告节点的自查（_MaskedCells）按同一个函数认：同一条查询里引用门店名、客户名都算，各自点名出处，不写值。"""
    from app.engine.nodes.report import _MaskedCells

    await set_source(":memory:", {"mask_columns": ["name", "phone"]}, probe=False)
    art = await artifact_store.put_json({"sql": MIXED, "columns": ["store", "customer", "tel", "spend"],
                                         "rows": [["广州天河店", "高峰", PHONES[1], 300.0]], "source": SOURCE,
                                         "schema_artifact": await schema_of(TABLES, VIEWS)}, kind="query")
    catalog = {"Q1": {"alias": "Q1", "kind": "query", "artifact": art, "columns": ["store", "customer", "tel", "spend"],
                      "rows": 1, "node_id": "top", "source": SOURCE}}
    doc = compose_doc("门店 [[v:Q1.r0.store]]，客户 [[v:Q1.r0.customer]]，消费 [[v:Q1.r0.spend]]。", catalog,
                      node_id="write", run_id="r1")
    store, customer = masked_of((await _MaskedCells(catalog)(doc))["violations"])
    assert store["ref"] == "v:Q1.r0.store" and "stores.name" in store["message"], store
    assert customer["ref"] == "v:Q1.r0.customer" and "customers.name" in customer["message"], customer
    for v in (store, customer):
        assert "按列名对所有表生效" in v["message"] and "按列名对所有表生效" in v["for_model"], v
        assert leaks(v) == [] and "广州天河店" not in json.dumps(v, ensure_ascii=False) and "高峰" not in v["message"]
