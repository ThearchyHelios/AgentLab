"""合并查询里对遮罩列改名、套函数、拼接、汇总、经 WITH 转一道得来的结果列，证据面板、勾行、报告自查、指标都认得出。

复核发现：合并查询的遮罩以前只按合并当时记下的列名和逐格来历认，逐格来历又只认「单层 SELECT、选取项是输入表的列」
这一种写法。合并 SQL 写 SELECT a.tel AS contact, SUM(b.amount) … GROUP BY a.tel、substr(a.phone, 1, 11) AS p2、
a.tel || '' AS p2、WITH x AS (SELECT a.tel AS p2 FROM a) … 时没有逐格来历，号码在片段接口的查询步骤、推演的勾行接口里
原样给出，报告自查也认不出 [[v:Q3.r0.contact]]；可验证报告对同一份快照只给哈希。现在合并 SQL 本身也按 SQL 认
（api/evidence.py 的 _merge_mask_sets）：各输入里要遮的列名当作遮罩列，按 SQLite 的读法认合并 SQL。另外：

- 输入快照读不出来、又可能带着遮罩的：它的列全部当遮罩列（宁可多遮）
- 合并警告里举的例子（类型不一致的键、重复的键）是号码的，换成「已遮罩」

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
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

CUSTOMERS = [(1, "客户甲", "19900000101"), (2, "客户乙", "19900000102"), (3, "客户丙", "19900000103")]
ORDERS = [(1, 1, 120.5), (2, 2, 300.0), (3, 2, 80.0), (4, 3, 210.25), (5, 1, 99.5)]
PHONES = [c[2] for c in CUSTOMERS]
#: 输入里给号码起了别名（tel）；另一份按客户汇总金额
Q_TEL = "SELECT id, phone AS tel FROM customers ORDER BY id"
Q_PHONE = "SELECT id, phone FROM customers ORDER BY id"
Q_ORD = "SELECT customer_id AS id, SUM(amount) AS amount FROM orders GROUP BY customer_id ORDER BY id"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
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
async def mall(tmp_path, engine_up):
    path = tmp_path / "mall.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);"
                     "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", ORDERS)
    db.commit()
    db.close()
    source_id = await set_source(path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes) -> dict:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def merge_graph(cust_sql: str, merge_sql: str, *, metrics: list | None = None, **report) -> dict:
    nodes = [node("start", "input"),
             node("q_cust", "tool", tool="db_query__mall", args={"sql": cust_sql}),
             node("q_ord", "tool", tool="db_query__mall", args={"sql": Q_ORD}),
             node("merged", "merge", inputs={"a": "q_cust", "b": "q_ord"}, sql=merge_sql)]
    if metrics:
        nodes.append(node("card", "metrics", caliber="合并口径", caliber_version="v1", metrics=metrics))
    nodes += [node("write", "report", instructions="写一句", **{"on_violation": "flag", **report}),
              node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}])]
    return chain(*nodes)


async def finish(monkeypatch, spec: dict, text: str) -> Run:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))
    run_id = (await run_manager.start(graph=spec, input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


def leaks(value) -> list[str]:
    """写着的号码，按千分位写的（19,900,000,101）也算。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


async def doc_of(client, run_id: str) -> dict:
    [report] = (await client.get(f"/api/runs/{run_id}/evidence")).json()["reports"]
    return artifact_store.load(report["doc_artifact"])


async def segment(client, run_id: str, ref: str) -> dict:
    doc = await doc_of(client, run_id)
    seg = next(s for s in iter_segments(doc) if s.get("ref") == ref)
    res = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg['id']}")
    assert res.status_code == 200, res.text
    return res.json()


def merged_step(body: dict) -> dict:
    return next(s for s in body["chain"] if s["step"] == "query" and s.get("merge"))


async def checked_of(run_id: str) -> dict:
    async with SessionLocal() as session:
        rows = list((await session.execute(select(RunEvent).where(RunEvent.run_id == run_id,
                                                                  RunEvent.type == "report.checked"))).scalars())
    return rows[-1].data


#: (输入查询, 合并 SQL, 取自遮罩列的结果列)：都没有逐格来历，或者来历指不到这一列
CASES = [
    # 改名再按它汇总（验收配方的合并写法）
    (Q_TEL, "SELECT a.tel AS contact, SUM(b.amount) AS spend FROM a JOIN b ON a.id = b.id GROUP BY a.tel "
            "ORDER BY spend DESC", "contact"),
    # 输入没改名，合并里改名、按它分组
    (Q_PHONE, "SELECT a.phone AS contact, SUM(b.amount) AS spend FROM a JOIN b ON a.id = b.id GROUP BY a.phone "
              "ORDER BY spend DESC", "contact"),
    # 套函数
    (Q_PHONE, "SELECT a.id, substr(a.phone, 1, 11) AS contact, b.amount AS spend FROM a JOIN b ON a.id = b.id "
              "ORDER BY a.id", "contact"),
    (Q_TEL, "SELECT a.id, upper(a.tel) AS contact, b.amount AS spend FROM a JOIN b ON a.id = b.id ORDER BY a.id",
     "contact"),
    # 拼接
    (Q_TEL, "SELECT a.id, a.tel || '' AS contact, b.amount AS spend FROM a JOIN b ON a.id = b.id ORDER BY a.id",
     "contact"),
    # 经 WITH 转一道
    (Q_TEL, "WITH x AS (SELECT a.id AS id, a.tel AS contact FROM a) SELECT x.id, x.contact, b.amount AS spend "
            "FROM x JOIN b ON x.id = b.id ORDER BY x.id", "contact"),
]


@pytest.mark.parametrize("cust_sql, merge_sql, column", CASES)
async def test_the_query_step_and_the_rows_page_mask_a_column_derived_in_the_merge_sql(
        client, monkeypatch, mall, cust_sql, merge_sql, column):
    """点开正文里引用的金额：合并步骤里那一列遮掉，勾行接口同样遮，号码一个都不交出来。"""
    row = await finish(monkeypatch, merge_graph(cust_sql, merge_sql), "合并后第一行金额 [[v:Q3.r0.spend]]。")
    body = await segment(client, row.id, "v:Q3.r0.spend")
    step = merged_step(body)
    assert column in step["masked"], step["masked"]
    at = step["columns"].index(column)
    assert all(r[at] == "已遮罩" for r in step["rows"]) and leaks(body) == [], step["rows"]
    page = (await client.get(f"/api/runs/{row.id}/evidence/whatif/rows", params={"artifact": step["artifact"]})).json()
    assert column in page["masked"] and leaks(page) == [], page["masked"]


@pytest.mark.parametrize("cust_sql, merge_sql, column", CASES)
async def test_the_self_check_flags_a_cell_derived_in_the_merge_sql(monkeypatch, mall, cust_sql, merge_sql, column):
    """写作者直接引用合并结果里取自遮罩列的那一格：自查记 masked_cell，别的格照常。"""
    row = await finish(monkeypatch, merge_graph(cust_sql, merge_sql),
                       f"联系方式 [[v:Q3.r0.{column}]]，金额 [[v:Q3.r0.spend]]。")
    checked = await checked_of(row.id)
    assert [v["ref"] for v in checked["violations"] if v["code"] == "masked_cell"] == [f"v:Q3.r0.{column}"], \
        checked["violations"]
    assert leaks(checked["violations"]) == []


async def test_a_metric_taking_a_cell_derived_in_the_merge_sql_is_withheld(client, monkeypatch, mall):
    """口径卡用 cell() 取合并结果里那一列：指标步骤的输入、指标的值都不给，推演按遮罩处理、原值也不给。"""
    metrics = [{"id": "contact_len", "name": "联系方式位数", "unit": "位", "format": "thousands",
                "expression": "len(str(cell(nodes.merged, 0, 'contact')))"},
               {"id": "top_spend", "name": "最高消费", "unit": "元", "decimals": 2, "format": "thousands",
                "expression": "cell(nodes.merged, 0, 'spend')"}]
    row = await finish(monkeypatch, merge_graph(Q_TEL, CASES[0][1], metrics=metrics),
                       "位数 [[m:contact_len]]，最高消费 [[m:top_spend]]。")
    body = await segment(client, row.id, "m:contact_len")
    assert body["chain"][0]["value"] == "已遮罩" and leaks(body) == []
    [cell] = [s for s in body["chain"] if s["step"] == "input"]
    assert cell["value"] == "已遮罩" and cell["masked"] == ["contact"], cell
    res = await client.post(f"/api/runs/{row.id}/evidence/whatif", json={"card": "card"})
    metrics_out = {m["id"]: m for m in res.json()["metrics"]}
    assert metrics_out["contact_len"]["status"] == "masked" and metrics_out["contact_len"]["old"] == "已遮罩"
    assert metrics_out["top_spend"]["status"] == "unchanged" and metrics_out["top_spend"]["old"] == 380.0
    assert leaks(res.json()) == []


async def test_without_masks_the_merge_step_is_unchanged(client, monkeypatch, mall):
    await set_source(mall, {})
    row = await finish(monkeypatch, merge_graph(Q_TEL, CASES[0][1]), "合并后第一行金额 [[v:Q3.r0.spend]]。")
    step = merged_step(await segment(client, row.id, "v:Q3.r0.spend"))
    assert step["masked"] == [] and step["rows"][0][0] in PHONES


# --------------------------------------------------------------------------
# 输入快照读不出来：它的列全部当遮罩列
# --------------------------------------------------------------------------


async def test_an_unreadable_input_masks_every_column_that_may_come_from_it(engine_up, mall):
    """输入快照读不出来（文件丢了、被改了），它的数据源又设了遮罩：认不出输入里哪一列改过名字，它的每一列都当遮罩列，
    合并结果里取自它的列（逐格来历指向它的、合并 SQL 里取自它的）都遮；另一个读得出、没遮罩的输入照常。"""
    from app.api.evidence import _mask_sets, _Masks

    ord_snap = await artifact_store.put_json({"columns": ["id", "amount"], "rows": [[1, 220.0], [2, 380.0]],
                                              "source": "mall", "sql": Q_ORD}, kind="query")
    snap = {"columns": ["contact", "spend"], "rows": [["已脱敏", 380.0]], "source": "合并查询",
            "sql": "SELECT a.tel AS contact, SUM(b.amount) AS spend FROM a JOIN b ON a.id = b.id GROUP BY a.tel",
            "sources": ["mall"],
            "inputs": [{"alias": "a", "artifact": "0" * 64, "columns": ["id", "tel"], "source": "mall"},
                       {"alias": "b", "artifact": ord_snap, "columns": ["id", "amount"], "source": "mall"}]}
    sets = await _mask_sets(snap, "合并查询", _Masks())
    assert "contact" in sets.hidden and "spend" not in sets.hidden, sets.hidden
    # 确定取自遮罩列的只认数据源设的列名：tel 是不是号码核对不了，报告自查不为它判违规
    assert "contact" not in sets.sure
    # 连输入有哪几列都不知道（老快照）：整份结果的列都遮
    old = {**snap, "inputs": [{"alias": "a", "artifact": "0" * 64, "source": "mall"}, snap["inputs"][1]]}
    assert {"contact", "spend"} <= (await _mask_sets(old, "合并查询", _Masks())).hidden


# --------------------------------------------------------------------------
# 合并警告举的例子
# --------------------------------------------------------------------------


async def test_merge_warnings_do_not_quote_masked_values(client, monkeypatch, tmp_path, engine_up):
    """按号码合并、两边类型不一致又有重复的键：合并警告举的例子是号码，查询步骤里换成「已遮罩」，说明写清只是展示时换。"""
    path = tmp_path / "keys.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, phone TEXT);"
                     "CREATE TABLE orders (id INTEGER PRIMARY KEY, phone INTEGER, amount REAL);")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)",
                   [(1, "客户甲", "19900000101"), (2, "客户甲家属", "19900000101"), (3, "客户乙", "19900000102")])
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)",
                   [(1, 19900000101, 120.5), (2, 19900000101, 30.0), (3, 19900000102, 80.0)])
    db.commit()
    db.close()
    source_id = await set_source(path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    spec = chain(node("start", "input"),
                 node("q_cust", "tool", tool="db_query__mall", args={"sql": "SELECT name, phone FROM customers ORDER BY id"}),
                 node("q_ord", "tool", tool="db_query__mall", args={"sql": "SELECT phone, amount FROM orders ORDER BY id"}),
                 node("merged", "merge", inputs={"a": "q_cust", "b": "q_ord"},
                      sql="SELECT a.name, b.amount FROM a JOIN b ON a.phone = b.phone"),
                 node("write", "report", instructions="写一句", on_violation="flag"),
                 node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]))
    row = await finish(monkeypatch, spec, "第一行金额 [[v:Q3.r0.amount]]。")
    step = merged_step(await segment(client, row.id, "v:Q3.r0.amount"))
    warnings = step["merge"]["warnings"]
    assert {w["code"] for w in warnings} >= {"key_type_mismatch", "rows_grew"}, warnings
    assert leaks(warnings) == [] and all("已遮罩" in w["message"] for w in warnings), warnings
    assert "仅在展示时" in step["mask_note"], step.get("mask_note")
    await engines.invalidate(source_id)
