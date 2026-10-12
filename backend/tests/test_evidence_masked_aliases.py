"""证据面板认得出改了名字的遮罩列：查询步骤、指标的输入、推演的勾行和遮罩，按 SQL 认。

验收发现：正式运行的报告没写号码，可验证报告也把 top 查询（c.phone AS tel）的快照只给了哈希，点开正文里的消费额，
片段接口的查询步骤却是 columns=[customer, tel, spend]、masked=[]，三个号码原样交出来。证据面板以前只遮列名恰好等于
遮罩列的那几列；可验证报告、核查 agent 早已按 SQL 认出别名、函数、子查询、WITH 转一道得来的结果列。现在证据面板
按同一套认法（engine/masking.py 的 _derived_hidden）：

- 查询步骤：c.phone AS tel 的 tel 列遮掉，面板底下的遮罩说明带上它
- 指标的输入：cell() 取 tel 那一格、按位置取的 vars.res.rows[0][1] 都不给值；指标本身的值也不给
- 推演：勾行接口遮 tel，用到 tel 的指标推演不了、原值不给
- SQL 解析不了的，整份快照的列都遮（保守规则，和可验证报告同一个函数）
- 合并查询：输入快照里改了名字的遮罩列，按逐格来历遮到合并结果里
- 没设遮罩时一切照旧

真跑一遍图（虚构的 SQLite 商城库，号码是编的），报告由 MockChatModel 照剧本写。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

CUSTOMERS = [(1, "客户甲", "199-0000-0101"), (2, "客户乙", "199-0000-0102"), (3, "客户丙", "199-0000-0103"),
             (4, "客户丁", "199-0000-0104")]
ORDERS = [(1, 1, 120.5), (2, 2, 300.0), (3, 2, 80.0), (4, 3, 210.25), (5, 4, 50.0), (6, 1, 99.5)]
PHONES = [c[2] for c in CUSTOMERS]
#: 验收原样的写法：号码起了别名 tel，和消费额排在同一份结果里
SQL_TOP = ("SELECT c.name AS customer, c.phone AS tel, ROUND(SUM(o.amount), 2) AS spend "
           "FROM orders o JOIN customers c ON c.id = o.customer_id GROUP BY c.id, c.name, c.phone ORDER BY spend DESC")
TEL_CELL = "cell(nodes.top, 0, 'tel')"
#: 号码那一格按位置取：vars.res 是数据整形解析好的查询结果，第 1 列是 tel
TEL_POS = "vars.res.rows[0][1]"
METRICS = [
    {"id": "top_spend", "name": "最高消费", "unit": "元", "decimals": 2, "format": "thousands",
     "expression": "cell(nodes.top, 0, 'spend')"},
    {"id": "tel_len", "name": "号码位数", "unit": "位", "format": "thousands", "expression": f"len({TEL_CELL})"},
    {"id": "tel_pos2", "name": "号码位数两倍", "unit": "位", "format": "thousands", "expression": f"len({TEL_POS}) * 2"},
]
TEXT = "消费最高的客户花了 [[v:Q1.r0.spend]]，口径卡记 [[m:top_spend]]；号码 [[m:tel_len]]，两倍 [[m:tel_pos2]]。"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(path, options=None):
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "mall"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="mall", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        return row.id


@pytest.fixture
async def mall(tmp_path):
    path = tmp_path / "mall.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);"
                     "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", ORDERS)
    db.commit()
    db.close()
    source_id = await set_source(path)
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes) -> dict:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def graph() -> dict:
    return chain(
        node("start", "input"),
        node("top", "tool", tool="db_query__mall", args={"sql": SQL_TOP}),
        node("parse", "transform", mode="json", template="{{ nodes.top }}", assign_to="res"),
        node("card", "metrics", caliber="商城口径", caliber_version="v1", metrics=METRICS),
        node("write", "report", instructions="写一句", on_violation="flag"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


async def run_graph(monkeypatch, spec: dict | None = None, text: str = TEXT) -> Run:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))
    run_id = (await run_manager.start(graph=spec or graph(), input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def doc_of(client, run_id: str) -> dict:
    [report] = (await client.get(f"/api/runs/{run_id}/evidence")).json()["reports"]
    return artifact_store.load(report["doc_artifact"])


async def segment(client, run_id: str, *, text: str | None = None, ref: str | None = None) -> dict:
    """按正文里的字或者引用（v:Q1.r0.spend）找片段，取片段接口的答复。"""
    doc = await doc_of(client, run_id)
    seg = next(s for s in iter_segments(doc) if (text is not None and s["text"] == text)
               or (ref is not None and s.get("ref") == ref))
    res = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg['id']}")
    assert res.status_code == 200, res.text
    return res.json()


def leaks(body) -> list[str]:
    raw = json.dumps(body, ensure_ascii=False)
    return [p for p in PHONES if p in raw]


def steps(body: dict, kind: str) -> list[dict]:
    return [s for s in body["chain"] if s["step"] == kind]


# --------------------------------------------------------------------------
# 查询步骤
# --------------------------------------------------------------------------


async def test_a_renamed_masked_column_is_masked_in_the_query_step(client, monkeypatch, mall):
    """验收原样：点开正文里直接引用的消费额，查询步骤里 c.phone AS tel 的 tel 列遮掉，号码一个都不交出来。"""
    await set_source(mall, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, ref="v:Q1.r0.spend")
    [query] = steps(body, "query")
    assert query["columns"] == ["customer", "tel", "spend"] and query["masked"] == ["tel"], query["masked"]
    assert [r[1] for r in query["rows"]] == ["已遮罩"] * len(query["rows"]) and query["rows"][0][2] == 380.0
    assert body["redacted"]["columns"] == ["tel"] and body["redacted"]["note"]
    assert leaks(body) == []


async def test_the_metric_chain_masks_the_renamed_column_too(client, monkeypatch, mall):
    """口径卡指标 → 输入 → 查询：查询步骤同一套认法，tel 列同样遮掉（验收的 s13）。"""
    await set_source(mall, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, text="380.00元")
    [query] = steps(body, "query")
    assert query["masked"] == ["tel"] and leaks(body) == []
    [amount] = steps(body, "input")
    assert amount["value"] == 380.0 and "masked" not in amount


async def test_an_input_taken_from_a_renamed_masked_column_is_withheld(client, monkeypatch, mall):
    """cell() 取 tel 那一格：输入不给值、写明取自哪一列，代入式里那一处写「已遮罩」，指标本身的值也不给。"""
    await set_source(mall, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, text="13位")
    metric = body["chain"][0]
    assert metric["step"] == "metric" and metric["substituted"] == "len(已遮罩)"
    assert metric["value"] == "已遮罩" and metric["rendered"] == "13位"
    [cell] = steps(body, "input")
    assert cell["value"] == "已遮罩" and cell["masked"] == ["tel"] and cell["path"] == TEL_CELL
    assert body["redacted"]["columns"] == ["tel"] and leaks(body) == []


async def test_a_scalar_taken_by_position_from_a_renamed_masked_column_is_withheld(client, monkeypatch, mall):
    """vars.res.rows[0][1]：没有定位，顺着取值链认出取自查询结果的 tel 列，tel 又是 phone 改的名字。"""
    await set_source(mall, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, text="26位")
    assert body["chain"][0]["substituted"] == "len(已遮罩) * 2" and body["chain"][0]["value"] == "已遮罩"
    [pos] = steps(body, "input")
    assert pos["path"] == TEL_POS and pos["value"] == "已遮罩" and pos["masked"] == ["tel"]
    assert leaks(body) == []


async def test_without_masks_nothing_changes(client, monkeypatch, mall):
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, ref="v:Q1.r0.spend")
    [query] = steps(body, "query")
    assert query["masked"] == [] and query["rows"][0][1] == "199-0000-0102" and body["redacted"] == {"columns": []}
    plen = await segment(client, row.id, text="13位")
    assert plen["chain"][0]["value"] == 13 and plen["chain"][0]["substituted"] == "len('199-0000-0102')"


async def test_a_mask_set_after_the_run_also_reaches_renamed_columns(client, monkeypatch, mall):
    """事后才给数据源加的遮罩：按数据源现在设的列认 SQL，改了名字的照样遮。"""
    row = await run_graph(monkeypatch)
    await set_source(mall, {"mask_columns": "phone"})
    body = await segment(client, row.id, ref="v:Q1.r0.spend")
    assert steps(body, "query")[0]["masked"] == ["tel"] and leaks(body) == []


# --------------------------------------------------------------------------
# 推演：勾行接口、用到 tel 的指标
# --------------------------------------------------------------------------


async def test_the_whatif_masks_the_renamed_column(client, monkeypatch, mall):
    await set_source(mall, {"mask_columns": ["phone"]})
    row = await run_graph(monkeypatch)
    body = await segment(client, row.id, ref="v:Q1.r0.spend")
    snapshot = steps(body, "query")[0]["artifact"]
    page = (await client.get(f"/api/runs/{row.id}/evidence/whatif/rows", params={"artifact": snapshot})).json()
    assert page["masked"] == ["tel"] and all(r[1] == "已遮罩" for r in page["rows"]) and leaks(page) == []
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card"})
    assert res.status_code == 200, res.text
    metrics = {m["id"]: m for m in res.json()["metrics"]}
    assert metrics["tel_len"]["status"] == "masked" and metrics["tel_pos2"]["status"] == "masked"
    assert metrics["top_spend"]["status"] == "unchanged" and metrics["top_spend"]["old"] == 380.0
    assert leaks(res.json()) == []
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif",
                            json={"card": "card", "overrides": {"inputs": {TEL_CELL: 1}}})
    assert res.status_code == 422 and "遮罩" in res.json()["detail"]


# --------------------------------------------------------------------------
# 合并查询：输入里改了名字的遮罩列，按逐格来历遮到合并结果里
# --------------------------------------------------------------------------


async def test_a_merged_result_masks_a_column_renamed_in_its_input(client, monkeypatch, mall):
    await set_source(mall, {"mask_columns": ["phone"]})
    spec = chain(
        node("start", "input"),
        node("q_cust", "tool", tool="db_query__mall", args={"sql": "SELECT id, phone AS tel FROM customers ORDER BY id"}),
        node("q_ord", "tool", tool="db_query__mall",
             args={"sql": "SELECT customer_id AS id, SUM(amount) AS amount FROM orders GROUP BY customer_id ORDER BY id"}),
        node("merged", "merge", inputs={"a": "q_cust", "b": "q_ord"},
             sql="SELECT a.id, a.tel, b.amount FROM a JOIN b ON a.id = b.id ORDER BY a.id"),
        node("write", "report", instructions="写一句", on_violation="flag"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )
    row = await run_graph(monkeypatch, spec, text="一号客户共消费 [[v:Q3.r0.amount]]。")
    body = await segment(client, row.id, ref="v:Q3.r0.amount")
    merged = next(s for s in steps(body, "query") if s.get("merge"))
    assert "tel" in merged["masked"] and all(r[merged["columns"].index("tel")] == "已遮罩" for r in merged["rows"])
    assert leaks(body) == []


# --------------------------------------------------------------------------
# 手搭的快照：SQL 的几种写法，解析不了时的保守规则
# --------------------------------------------------------------------------

#: 库里没有这个数据源：遮罩只有查询当时记在快照里的那一份（phone）
GHOST = "ghost_mall"


async def query_step(sql: str, columns: list[str], rows: list[list]) -> dict:
    """手搭一份封存范围内的查询快照（查询当时记下了遮罩列 phone），直接走查询步骤（evidence._query_step）。"""
    from app.api import evidence

    snap = await artifact_store.put_json({"columns": columns, "rows": rows, "source": GHOST, "sql": sql,
                                          "mask_columns": ["phone"]}, kind="query")
    sealed = evidence._Sealed(run_id="syn-alias", graph={}, events=[],
                              seal={"sealed": True, "ok": True, "manifest_seq": 1, "legacy": False})
    sealed.queries = {snap: {"node_id": "q", "tool": f"db_query__{GHOST}", "source": GHOST}}
    return await evidence._query_step(snap, [(0, columns[0])], {"catalog": {}}, sealed, evidence._Masks())


@pytest.mark.parametrize("sql, columns, masked", [
    # 函数套一层再经 WITH 转一道
    ("WITH t AS (SELECT name, UPPER(phone) AS p FROM customers) SELECT name AS customer, p AS contact FROM t",
     ["customer", "contact"], ["contact"]),
    # 子查询里起的别名，外层再选出来
    ("SELECT x.who, x.num FROM (SELECT name AS who, phone AS num FROM customers) x", ["who", "num"], ["num"]),
    # 截一段再拼上星号：值仍取自遮罩列
    ("SELECT name, substr(phone, 1, 3) || '****' AS short_tel FROM customers", ["name", "short_tel"], ["short_tel"]),
    # 只数个数：带不出值，不遮
    ("SELECT COUNT(DISTINCT phone) AS n FROM customers", ["n"], []),
    # 解析不了、又写着遮罩列：整份快照的列都遮
    ("SELECT name, phone AS p FROM customers WHERE ((", ["name", "p"], ["name", "p"]),
    # 每种读法都解析不了的，没写着遮罩列也整份遮：和可验证报告同一个函数（认不出结果里哪一列取自哪一列）
    ("SELECT name, id AS p FROM customers WHERE ((", ["name", "p"], ["name", "p"]),
])
async def test_the_query_step_reads_the_sql_like_the_bundle_and_the_verify_agent(sql, columns, masked):
    rows = [["客户甲" if i == 0 else "199-0000-0101" for i in range(len(columns))]]
    step = await query_step(sql, columns, rows)
    assert step["masked"] == masked, step["masked"]
    assert [v == "已遮罩" for v in step["rows"][0]] == [c in masked for c in columns]
