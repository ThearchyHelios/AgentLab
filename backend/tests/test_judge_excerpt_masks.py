"""交给结论句裁判的摘录：按快照逐份遮，SQL 认得确定取自遮罩列的列（改了名字的号码列）不给，金额、计数照给。

复核发现（第 4 轮）：
- 第 3 轮把摘录退回只按数据源设的列名遮：c.phone AS tel 这种改了名字的号码列又进了摘录（「列：spend | tel / r0：380 |
  199…」），裁判理由在证据面板、审计 CSV 里原样显示，裁判一引用号码就露出来。按需裁判（探索运行）和报告撰写节点里当场
  裁判（正式运行）都是这样
- 第 2 轮把面板那套宁可多遮的列按数据源名并给裁判，整份遮的快照、只数个数的列从摘录里消失，被引用的金额、人数裁判看不到

现在每份快照只遮这份快照里确定取自遮罩列的那几列（_mask_sets 的 sure，和证据面板同一套认法）：不按数据源名合并（另一份
快照里同名、却取自别的列的照给），不用宁可多遮的 hidden（整份遮的快照、只数个数的列照给）。摘录变了，复用键随之变，已判过
的句子会重判一次。

真跑的部分用虚构的 SQLite 零售库（号码、热线都是编的），写作者和裁判都是 MockChatModel。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, Setting
from app.engine.evidence import compose_doc, iter_units, make_eid
from app.engine.runner import run_manager
from app.main import app

SOURCE = "judgeshop"
CUSTOMERS = [(1, "客户甲", "19900000611"), (2, "客户乙", "19900000612"), (3, "客户丙", "19900000613")]
STORES = [(1, "一号店", "0571-88000001"), (2, "二号店", "0571-88000002")]
ORDERS = [(1, 1, 1, 120.5), (2, 2, 2, 300.0), (3, 2, 2, 80.0), (4, 2, 3, 210.25)]
PHONES = [c[2] for c in CUSTOMERS]

#: 改了名字的号码列 contact：确定取自遮罩列
SQL_CUSTOMER = ("SELECT c.phone AS contact, ROUND(SUM(o.amount), 2) AS spend FROM customers c "
                "JOIN orders o ON o.customer_id = c.id GROUP BY c.phone ORDER BY spend DESC")
#: 同样叫 contact，取自门店热线（没设遮罩）：照给
SQL_STORE = ("SELECT s.hotline AS contact, COUNT(*) AS orders FROM stores s JOIN orders o ON o.store_id = s.id "
             "GROUP BY s.hotline ORDER BY orders DESC")
TEXT = "消费最高的一位客户共消费 [[v:Q1.r0.spend]]，订单最多的门店有 [[v:Q2.r0.orders]] 笔订单，热线 [[v:Q2.r0.contact]]。"


def leaks(value) -> list[str]:
    """写着的号码，按千分位写的（摘录里数值格按数渲染：19,900,000,611）也算。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None) -> str:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture
async def shop(tmp_path, engine_up):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);"
                     "CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT, hotline TEXT);"
                     "CREATE TABLE orders (id INTEGER PRIMARY KEY, store_id INTEGER, customer_id INTEGER,"
                     " amount REAL);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.executemany("INSERT INTO stores VALUES (?, ?, ?)", STORES)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
    db.commit()
    db.close()
    source_id = await set_source(path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    yield path
    await set_source(path, {})
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def judge_models(monkeypatch) -> list[str]:
    """写作者回 TEXT；裁判一律判有依据。返回每次交给裁判的请求原文。"""
    from app.engine import judge as judge_mod
    from app.engine.judge import JUDGE_ROLE
    from app.providers.mock_model import MockChatModel

    seen: list[str] = []

    def decide(model, messages):
        if JUDGE_ROLE in str(messages[0].content):
            request = str(messages[-1].content)
            seen.append(request)
            items = [{"unit": uid, "verdict": "supported", "rationale": "虚构的理由", "used": []}
                     for uid in re.findall(r"^\[(u\d+)\]", request, re.M)]
            return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
        return AIMessage(content=TEXT)

    monkeypatch.setattr(MockChatModel, "_decide", decide)

    async def judge_model_of(session, spec):
        return MockChatModel(model_name="claude-sonnet-5"), "claude-sonnet-5"

    monkeypatch.setattr(judge_mod, "get_chat_model", judge_model_of)
    return seen


async def judged_run(run_class: str) -> Run:
    from app.engine.judge import SPEND_KEY

    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge"])))
        await session.commit()
    nodes = [node("start", "input"),
             node("q_customer", "tool", tool=f"db_query__{SOURCE}", args={"sql": SQL_CUSTOMER}),
             node("q_store", "tool", tool=f"db_query__{SOURCE}", args={"sql": SQL_STORE}),
             node("write", "report", instructions="写一句", claims="judge", on_violation="flag"),
             node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}])]
    spec = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    run_id = (await run_manager.start(graph=spec, input_payload={}, run_class=run_class)).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


def excerpt_of(request: str, alias: str) -> str:
    """裁判请求里一份查询的那一段摘录。"""
    return request.split(f"【{alias}】", 1)[1].split("【", 1)[0]


def columns_line(excerpt: str) -> str:
    return next(line for line in excerpt.splitlines() if line.startswith("列："))


@pytest.mark.parametrize("where", ["on_demand", "inline"])
async def test_the_judge_does_not_see_a_renamed_masked_column(client, monkeypatch, shop, where):
    """Q1 的 contact 是 c.phone 改了名字：摘录里没有这一列、没有号码，说明有的列遮掉了；被引用的消费额照给。Q2 的 contact
    取自门店热线：同名不同出处，照给（不按数据源名合并）。按需裁判（探索运行）、节点里当场裁判（正式运行）同一个办法。"""
    seen = judge_models(monkeypatch)
    if where == "on_demand":
        row = await judged_run("exploratory")
        [report] = (await client.get(f"/api/runs/{row.id}/evidence")).json()["reports"]
        doc = artifact_store.load(report["doc_artifact"])
        uid = next(u["id"] for _, u in iter_units(doc)
                   if any(seg.get("ref") == "v:Q1.r0.spend" for seg in u.get("segments") or []))
        res = await client.post(f"/api/runs/{row.id}/evidence/judge", json={"units": [uid]})
        assert res.status_code == 200, res.text
    else:
        await judged_run("formal")
    [request] = seen
    q1, q2 = excerpt_of(request, "Q1"), excerpt_of(request, "Q2")
    assert "contact" not in columns_line(q1) and "spend" in columns_line(q1) and "380" in q1, q1
    assert "有的列按数据源的设置遮罩了" in q1, q1
    assert "contact" in columns_line(q2) and "0571-88000002" in q2 and "orders" in columns_line(q2), q2
    assert leaks(request) == []


async def test_an_unaliased_expression_over_every_column_is_masked_in_the_excerpt(engine_up, shop):
    """SELECT UPPER(phone) FROM customers：没起别名、表达式盖住全部结果列，SQL 认得出取自遮罩列（不是认不准整份算的），
    摘录不给这一列。派生来源里有没起别名的号码表达式、整份遮的快照，金额照给（裁判要看得到才判得了）。"""
    from app.api.evidence import _judge_loader
    from app.engine import judge as judging

    upper = await artifact_store.put_json({"sql": "SELECT UPPER(phone) FROM customers", "columns": ["UPPER(phone)"],
                                           "rows": [[p] for p in PHONES], "source": SOURCE}, kind="query")
    whole = await artifact_store.put_json(
        {"sql": "SELECT name, amount FROM (SELECT c.name, UPPER(c.phone), o.amount FROM customers c "
                "JOIN orders o ON o.customer_id = c.id) t ORDER BY amount DESC",
         "columns": ["name", "amount"], "rows": [["客户乙", 300.0]], "source": SOURCE}, kind="query")
    listed = (("Q1", upper, ["UPPER(phone)"], 3), ("Q2", whole, ["name", "amount"], 1))
    catalog = {alias: {"alias": alias, "kind": "query", "artifact": art, "columns": columns, "rows": rows,
                       "node_id": alias, "source": SOURCE, "eid": make_eid("query", art, {})}
               for alias, art, columns, rows in listed}
    doc = compose_doc("客户都留了号码，最大一笔订单 [[v:Q2.r0.amount]]，见 [[see:Q1]]。", catalog, node_id="write",
                      run_id="r1")
    masked = await judging.source_masks(catalog)
    request = judging.prepare(doc, catalog, masked=masked, loader=await _judge_loader(doc, catalog))
    assert leaks(request.excerpts["Q1"]) == [] and "UPPER" not in columns_line(request.excerpts["Q1"]), request.excerpts
    assert "amount" in columns_line(request.excerpts["Q2"]) and "300" in request.excerpts["Q2"], request.excerpts
    # 不经 _judge_loader（只按数据源设的列名遮）时号码就在摘录里：上面的断言测的是这一层
    plain = judging.prepare(doc, catalog, masked=masked)
    assert leaks(plain.excerpts["Q1"]) != []
