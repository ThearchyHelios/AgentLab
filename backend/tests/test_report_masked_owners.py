"""报告自查只查引用，而且只认真取自遮罩表里那一列的格子（复核第 3 轮）。

复核发现：
- 第 2 轮加的按值自查把两个字的遮罩值当子串去认：客户里有一位叫「高峰」，正文写「本周销售高峰」就判 masked_cell，正式
  运行（默认违规即失败）因此失败，报错还叫人去改写作指令
- 按值自查连同名列的值一起收：mask_columns 设了 name（本意是客户姓名），正文提到门店名（stores.name）同样判失败
- 引用同理：遮罩按列名设在整个数据源上（不带表名），s.name AS store 按列名认也算取自遮罩列「name」

现在自查只认写作者用 [[v:Qn.rX.列]] 引用的格子（正文里照抄的号码本来就会被「数字没有出处」拦下），并且按 SQL 的血缘核一遍
这一列的值取自哪张表的哪一列：遮罩列在几张表里都有、其中有的表里另有别的遮罩列时（customers 有 name 和 phone，stores 只有
name），只算那几张表。证据面板、可验证报告照列名多遮，不受影响。

真跑的部分用虚构的 SQLite 零售库（号码、姓名、门店名都是编的），数据源探查过表结构（查询快照里冻结着表结构），写作者是
MockChatModel 照剧本写。
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
ORDERS = [(1, 1, 1, 120.5), (2, 2, 2, 300.0), (3, 1, 2, 80.0), (4, 2, 3, 210.25)]
PHONES = [c[2] for c in CUSTOMERS]

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
    db.executescript("CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT, region TEXT);"
                     "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);"
                     "CREATE TABLE orders (id INTEGER PRIMARY KEY, store_id INTEGER, customer_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO stores VALUES (?, ?, ?)", STORES)
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
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


# --------------------------------------------------------------------------
# 正文里写着的字不按值认
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, text", [
    # 客户里有一位叫「高峰」：常用词「销售高峰」不是在写这位客户
    (SQL_CUSTOMER, "本周销售高峰出现在周五，最高一位客户消费 [[v:Q1.r0.spend]]。"),
    # 设了 name 本意是客户姓名：正文用文字写门店名照常
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
# 引用：只认真取自遮罩表里那一列的
# --------------------------------------------------------------------------


async def test_citing_a_store_name_is_not_flagged_though_name_is_masked(monkeypatch, shop):
    """s.name AS store：门店表只有 name、没有别的遮罩列，customers 里 name 和 phone 都在——设了 name 管的是客户姓名。"""
    row = await finish(monkeypatch, graph(SQL_STORE, on_violation="fail"),
                       "销售额最高的门店是 [[v:Q1.r0.store]]，销售额 [[v:Q1.r0.sales]]。")
    assert row.status == "succeeded", row.error
    assert masked_of((await checked_of(row.id))["violations"]) == []


async def test_citing_a_customer_name_is_flagged_and_names_the_masked_column(monkeypatch, shop):
    """c.name AS customer：取自客户表的 name，照旧拦下；说明只点名它取自的那一列遮罩列，不把 phone 也列进来。"""
    row = await finish(monkeypatch, graph(SQL_CUSTOMER, on_violation="flag"),
                       "消费最高的客户是 [[v:Q1.r0.customer]]，共消费 [[v:Q1.r0.spend]]。")
    assert row.status == "succeeded", row.error
    [v] = masked_of((await checked_of(row.id))["violations"])
    assert v["ref"] == "v:Q1.r0.customer" and "取自设置了遮罩的列「name」" in v["message"], v["message"]


# --------------------------------------------------------------------------
# 认法：_cited_masks（报告节点经 _MaskedCells 调用）
# --------------------------------------------------------------------------

#: 冻结的表结构（introspect 的形状）：stores 只有 name，customers 有 name 和 phone
TABLES = {"stores": ["id", "name", "region"], "customers": ["id", "name", "phone"],
          "orders": ["id", "store_id", "customer_id", "amount"]}


async def schema_of(tables: dict[str, list[str]]) -> str:
    return await artifact_store.put_json(
        {"source": SOURCE, "tables": {t: {"columns": [{"name": c, "type": "TEXT"} for c in cols]}
                                       for t, cols in tables.items()}}, kind=SCHEMA_SNAPSHOT)


async def cited(sql: str, columns: list[str], *, masks: list[str], schema: bool = True,
                tables: dict[str, list[str]] = TABLES) -> dict[str, list[str]]:
    """一份快照按报告自查认出的列：{结果列: 它取自的遮罩列}。"""
    from app.api.evidence import _cited_masks, _Masks

    await set_source(":memory:", {"mask_columns": masks}, probe=False)
    snap = {"sql": sql, "columns": columns, "rows": [], "source": SOURCE,
            **({"schema_artifact": await schema_of(tables)} if schema else {})}
    art = await artifact_store.put_json(snap, kind="query")
    return await _cited_masks(snap, SOURCE, _Masks(), art)


MIXED = ("SELECT s.name AS store, c.name AS customer, c.phone AS tel, SUM(o.amount) AS spend FROM orders o "
         "JOIN stores s ON s.id = o.store_id JOIN customers c ON c.id = o.customer_id GROUP BY s.name, c.name, c.phone")


@pytest.mark.parametrize("label, sql, columns, masks, expected", [
    ("两张表的同名列在一条查询里：只认客户表的", MIXED, ["store", "customer", "tel", "spend"], ["name", "phone"],
     {"customer": ["name"], "tel": ["phone"]}),
    ("只设了 name：几张表都有 name、哪张都没有别的遮罩列，照列名认", MIXED, ["store", "customer", "tel", "spend"],
     ["name"], {"store": ["name"], "customer": ["name"]}),
    ("直接选出门店表的 name（列名对上遮罩列）", "SELECT name, region FROM stores", ["name", "region"],
     ["name", "phone"], {}),
    ("* 展开门店表", "SELECT * FROM stores", ["id", "name", "region"], ["name", "phone"], {}),
    ("* 展开客户表", "SELECT * FROM customers", ["id", "name", "phone"], ["name", "phone"],
     {"name": ["name"], "phone": ["phone"]}),
    ("经 WITH 转两道", "WITH a AS (SELECT c.phone AS tel FROM customers c), b AS (SELECT tel AS contact FROM a) "
     "SELECT contact FROM b", ["contact"], ["phone"], {"contact": ["phone"]}),
    ("列名和遮罩列一样，值却取自别的列", "SELECT region AS phone FROM stores", ["phone"], ["phone"], {}),
    ("UNION：有一支取自客户表就算", "SELECT name FROM stores UNION ALL SELECT name FROM customers", ["name"],
     ["name", "phone"], {"name": ["name"]}),
])
async def test_cited_masks_follow_the_table_the_value_comes_from(engine_up, label, sql, columns, masks, expected):
    assert await cited(sql, columns, masks=masks) == expected, label


async def test_without_a_frozen_schema_the_mask_is_read_by_column_name(engine_up):
    """数据源没探查过表结构（快照里没有冻结的表结构）：认不出同名的列还在哪几张表里，照列名认，门店名也算；值根本不取自
    遮罩列的（region AS phone）照样不算。"""
    assert await cited(MIXED, ["store", "customer", "tel", "spend"], masks=["name", "phone"], schema=False) == \
        {"store": ["name"], "customer": ["name"], "tel": ["phone"]}
    assert await cited("SELECT region AS phone FROM stores", ["phone"], masks=["phone"], schema=False) == {}


@pytest.mark.parametrize("sql, columns", [
    # 表值函数：拆的是号码，血缘认不出出处，照遮罩那一套认
    ("SELECT x.value AS p FROM customers c, json_each(c.phone) x", ["p"]),
    # 没起别名的表达式：血缘按列名找不到这一列
    ("SELECT id, UPPER(phone) FROM customers", ["id", "UPPER(phone)"]),
])
async def test_columns_the_lineage_cannot_place_stay_flagged(engine_up, sql, columns):
    assert set(await cited(sql, columns, masks=["name", "phone"])) == {columns[-1].lower()}


async def test_the_report_node_flags_only_the_customer_column(engine_up):
    """报告节点的自查（_MaskedCells）按同一个函数认：同一条查询里引用门店名不算、引用客户名算。"""
    from app.engine.nodes.report import _MaskedCells

    await set_source(":memory:", {"mask_columns": ["name", "phone"]}, probe=False)
    art = await artifact_store.put_json({"sql": MIXED, "columns": ["store", "customer", "tel", "spend"],
                                         "rows": [["广州天河店", "高峰", PHONES[1], 300.0]], "source": SOURCE,
                                         "schema_artifact": await schema_of(TABLES)}, kind="query")
    catalog = {"Q1": {"alias": "Q1", "kind": "query", "artifact": art, "columns": ["store", "customer", "tel", "spend"],
                      "rows": 1, "node_id": "top", "source": SOURCE}}
    doc = compose_doc("门店 [[v:Q1.r0.store]]，客户 [[v:Q1.r0.customer]]，消费 [[v:Q1.r0.spend]]。", catalog,
                      node_id="write", run_id="r1")
    [v] = masked_of((await _MaskedCells(catalog)(doc))["violations"])
    assert v["ref"] == "v:Q1.r0.customer" and "「name」" in v["message"] and leaks(v) == [], v
