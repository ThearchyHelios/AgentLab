"""报告自查只拦能确定取自遮罩列的格子；判失败时的下一步、时间线和缺口的说法照实；只读引用到的快照；审计表单列。

复核发现：
- 自查套用了证据面板展示用的保守认法：SQL 认不准时面板把整份的列都遮，自查跟着把和遮罩列无关的金额也判成「取自遮罩
  列」，正式运行（默认违规即失败）因此判失败，写作者也改不掉；只装着分档标签的列（CASE … THEN '已留'）同样被判。现在自查
  只认 _mask_sets 的 sure：列名就是遮罩列的、SQL 里认得出取自遮罩列的；整份算进来的、只装着标签的不算。按数据源自己的
  方言认（MySQL 里双引号是字符串）
- 判失败时建议「换用指令遵循能力更强的模型」「先改成标注」：遮罩列多半是写作指令点名要的，换模型没用，改成标注值就随
  报告出去了。现在只说去改写作指令
- 时间线、缺口说「这些值会随报告出具」「出具已按缺口降档」：探索运行、没有出具契约的成果不出具。现在只说记为缺口
- 写作之前把目录里每份查询快照都读盘复验一遍：现在组装之后只读正文引用到的那几份
- 审计表、导出的 CSV 里，引用了遮罩列的格子只是一行普通的「有出处」：现在另列一行

真跑的部分用虚构的 SQLite 商城库（号码是编的），写作者是 MockChatModel 照剧本写。
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
from app.engine.evidence import compose_doc, masked_cells
from app.engine.runner import run_manager
from app.main import app

CUSTOMERS = [(1, "客户甲", "19900000401"), (2, "客户乙", "19900000402"), (3, "客户丙", "19900000403")]
ORDERS = [(1, 1, 120.5), (2, 2, 300.0), (3, 2, 80.0), (4, 3, 210.25), (5, 1, 99.5)]
PHONES = [c[2] for c in CUSTOMERS]
SQL_TOP = ("SELECT c.name AS customer, c.phone AS tel, ROUND(SUM(o.amount), 2) AS spend "
           "FROM orders o JOIN customers c ON c.id = o.customer_id GROUP BY c.id, c.name, c.phone ORDER BY spend DESC")
#: 派生来源里有没起别名的 UPPER(c.phone)：遮罩那一套认不准，整份的列都算进要遮的列（面板宁可多遮）
SQL_UNSURE = ("SELECT name, amount FROM (SELECT c.name, UPPER(c.phone), o.amount FROM customers c "
              "JOIN orders o ON o.customer_id = c.id) t ORDER BY amount DESC")
#: 只装着分档标签的列：格子里是 SQL 写死的字
SQL_LABEL = "SELECT id, CASE WHEN phone IS NULL THEN '未留' ELSE '已留' END AS has_phone FROM customers ORDER BY id"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client(engine_up):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def set_source(name: str, kind: str, path=None, options=None) -> str:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=name, kind=kind, database=str(path or ""), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path or ""), kind, True, True
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
    source_id = await set_source("mall", "sqlite", path, {"mask_columns": ["phone"]})
    await engines.invalidate(source_id)
    yield path
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(sql: str, *, contract: dict | None = None, **report) -> dict:
    out = {"fields": [{"name": "报告", "value": "{{ nodes.write.text }}"}]}
    if contract is not None:
        out["contract"] = contract
    nodes = [node("start", "input"), node("top", "tool", tool="db_query__mall", args={"sql": sql}),
             node("write", "report", instructions="写一句", **report), node("out", "output", **out)]
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


async def events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        return list((await session.execute(q)).scalars())


def masked_of(violations: list[dict]) -> list[str]:
    return [v["ref"] for v in violations if v["code"] == "masked_cell"]


def leaks(value) -> list[str]:
    """写着的号码，按千分位写的（19,900,000,101）也算。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw or p in raw.replace(",", "")]


# --------------------------------------------------------------------------
# 只拦能确定取自遮罩列的格子
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, text", [
    # SQL 认不准、整份算进来的金额：和遮罩列无关
    (SQL_UNSURE, "最大的一笔订单金额 [[v:Q1.r0.amount]]。"),
    # 只装着分档标签的列
    (SQL_LABEL, "第一位客户的手机号[[v:Q1.r0.has_phone]]。"),
])
async def test_a_formal_failure_is_not_raised_for_columns_that_are_not_surely_masked(monkeypatch, mall, sql, text):
    """违规即失败（正式运行的默认值）：认不准、只装着标签的列不判违规，运行照常成功；证据面板照旧多遮（展示）。"""
    row = await finish(monkeypatch, graph(sql, on_violation="fail"), text)
    assert row.status == "succeeded", row.error
    checked = next(e.data for e in await events(row.id) if e.type == "report.checked")
    assert masked_of(checked["violations"]) == [] and checked["ok"] is True, checked["violations"]


async def test_sure_and_hidden_are_told_apart(engine_up, mall):
    """_mask_sets：hidden 给面板（宁可多遮），sure 给自查（确定的）。"""
    from app.api.evidence import _mask_sets, _Masks

    async def sets(sql: str, columns: list[str], rows: list[list]):
        return await _mask_sets({"sql": sql, "columns": columns, "rows": rows, "source": "mall"}, "mall", _Masks())

    top = await sets(SQL_TOP, ["customer", "tel", "spend"], [["客户乙", PHONES[1], 380.0]])
    assert top.sure == {"tel"} and "tel" in top.hidden and top.base == {"phone"}
    unsure = await sets(SQL_UNSURE, ["name", "amount"], [["客户乙", 300.0]])
    assert unsure.hidden >= {"name", "amount"} and unsure.sure == set(), (unsure.hidden, unsure.sure)
    label = await sets(SQL_LABEL, ["id", "has_phone"], [[1, "已留"]])
    assert "has_phone" in label.hidden and label.sure == set(), (label.hidden, label.sure)
    direct = await sets("SELECT name, phone FROM customers", ["name", "phone"], [["客户甲", PHONES[0]]])
    assert direct.sure == {"phone"}


async def test_the_sure_columns_follow_the_dialect_of_the_source(engine_up):
    """自查按数据源自己的方言认：MySQL 里 "phone" 是字符串，SELECT "phone" AS lit 的 lit 不是遮罩列；Postgres 里是列名，
    lit 就是号码。证据面板的 hidden 按可验证报告的办法把几种读法合起来认，两处都遮（宁可多遮）。"""
    from app.api.evidence import _mask_sets, _Masks

    await set_source("mysql_mall", "mysql", options={"mask_columns": ["phone"]})
    await set_source("pg_mall", "postgres", options={"mask_columns": ["phone"]})
    snap = {"sql": 'SELECT id, "phone" AS lit FROM customers', "columns": ["id", "lit"], "rows": [[1, "phone"]]}
    mysql = await _mask_sets({**snap, "source": "mysql_mall"}, "mysql_mall", _Masks())
    pg = await _mask_sets({**snap, "source": "pg_mall"}, "pg_mall", _Masks())
    assert "lit" not in mysql.sure and "lit" in pg.sure, (mysql.sure, pg.sure)
    assert "lit" in mysql.hidden and "lit" in pg.hidden


# --------------------------------------------------------------------------
# 说法：判失败的下一步、违规说明、时间线、缺口
# --------------------------------------------------------------------------


async def test_a_failure_points_at_the_instructions_not_at_the_model(monkeypatch, mall):
    """遮罩列多半是写作指令点名要的：判失败时只说去掉指令里的引用，不建议换模型、不建议先改成标注。说明点名取自哪一列。"""
    row = await finish(monkeypatch, graph(SQL_TOP, on_violation="fail"), "联系电话 [[v:Q1.r0.tel]]。")
    assert row.status == "failed", row.error
    assert "指令中去掉对这些列的引用" in row.error and "按 SQL 认出取自设置了遮罩的列「phone」" in row.error, row.error
    assert "指令遵循能力" not in row.error and "标注" not in row.error and leaks(row.error) == []


def test_mixed_failures_still_point_at_the_instructions_for_the_masked_cells():
    from app.engine.nodes.report import _fail_advice

    masked, bare = {"code": "masked_cell"}, {"code": "uncited_number"}
    assert "指令中去掉" in _fail_advice([masked]) and "模型" not in _fail_advice([masked])
    both = _fail_advice([masked, bare])
    assert "指令中去掉" in both and "指令遵循能力" in both and "标注" not in both
    assert "标注" in _fail_advice([bare])


async def test_the_timeline_and_the_gap_do_not_foretell_the_issuance(monkeypatch, mall):
    """标注照常产出：日志、缺口只说记为缺口，不说「随报告出具」「已按缺口降档」——探索运行、没有出具契约的成果不出具。"""
    row = await finish(monkeypatch, graph(SQL_TOP, on_violation="flag"), "联系电话 [[v:Q1.r0.tel]]。")
    assert row.status == "succeeded", row.error
    rows = await events(row.id)
    [log] = [e.data for e in rows if e.type == "log" and e.data.get("code") == "report_masked_cell"]
    finished = next(e.data for e in rows if e.type == "node.finished" and e.node_id == "write")
    [gap] = artifact_store.load(finished["artifact"])["issuance_gaps"]
    for said in (log["message"], gap):
        assert "出具" not in said and "降档" not in said and leaks(said) == [], said
    assert "已记为缺口" in log["message"] and "不应写进报告" in gap


async def test_the_rewrite_request_says_the_value_must_not_be_written_in_any_form():
    """交回写作者的那句说清：这一列的值不能以任何写法写进报告（只说删掉引用，写作者会把值照抄成文字）。"""
    art = await artifact_store.put_json({"columns": ["customer", "tel"], "rows": [["客户甲", PHONES[0]]],
                                         "source": "mall", "sql": "SELECT name AS customer, phone AS tel FROM customers"},
                                        kind="query")
    catalog = {"Q1": {"alias": "Q1", "kind": "query", "artifact": art, "columns": ["customer", "tel"], "rows": 1,
                      "node_id": "top", "source": "mall"}}
    doc = compose_doc("电话 [[v:Q1.r0.tel]]。", catalog, node_id="write", run_id="r1")
    [v] = masked_cells(doc, catalog, {art: {"tel": ["phone"]}})
    assert "不能以任何形式写进报告" in v["for_model"] and "照抄的文字" in v["for_model"]
    assert "按 SQL 认出取自设置了遮罩的列「phone」" in v["message"]
    # 列名就是遮罩列的照旧说「在数据源中设置了遮罩」
    [own] = masked_cells(doc, catalog, {art: {"tel": ["tel"]}})
    assert "列「tel」在数据源中设置了遮罩" in own["message"]


# --------------------------------------------------------------------------
# 只读正文引用到的快照
# --------------------------------------------------------------------------


async def test_only_the_cited_snapshots_are_read_and_each_only_once(engine_up, mall, monkeypatch):
    """组装好一稿之后只读正文引用到的那几份快照，重写的稿子不再读同一份；没引用到的、没设遮罩的一份都不读。"""
    from app.core import artifact_store as store
    from app.engine.nodes.report import _MaskedCells

    cited = await store.put_json({"columns": ["customer", "tel"], "rows": [["客户甲", PHONES[0]]], "source": "mall",
                                  "sql": "SELECT name AS customer, phone AS tel FROM customers"}, kind="query")
    other = await store.put_json({"columns": ["id"], "rows": [[1]], "source": "mall", "sql": "SELECT id FROM customers"},
                                 kind="query")
    catalog = {"Q1": {"alias": "Q1", "kind": "query", "artifact": cited, "columns": ["customer", "tel"], "rows": 1,
                      "node_id": "a", "source": "mall"},
               "Q2": {"alias": "Q2", "kind": "query", "artifact": other, "columns": ["id"], "rows": 1,
                      "node_id": "b", "source": "mall"}}
    read: list[str] = []
    real = store.load

    def counting(artifact: str):
        read.append(artifact)
        return real(artifact)

    monkeypatch.setattr(store, "load", counting)
    check = _MaskedCells(catalog)
    first = await check(compose_doc("电话 [[v:Q1.r0.tel]]。", catalog, node_id="write", run_id="r1"))
    assert masked_of(first["violations"]) == ["v:Q1.r0.tel"] and read == [cited], read
    second = await check(compose_doc("客户 [[v:Q1.r0.customer]]，电话 [[v:Q1.r0.tel]]。", catalog, node_id="write",
                                     run_id="r1"))
    assert masked_of(second["violations"]) == ["v:Q1.r0.tel"] and read == [cited], "同一份快照不再读第二遍"


# --------------------------------------------------------------------------
# 审计表、CSV
# --------------------------------------------------------------------------


async def test_the_audit_lists_a_masked_cell_on_its_own_row(client, monkeypatch, mall):
    """引用了遮罩列的格子落在一个「有出处」的单元格片段上：审计表、CSV 里另列一行，写明引用了遮罩列，不写值。"""
    row = await finish(monkeypatch, graph(SQL_TOP, on_violation="flag"), "联系电话 [[v:Q1.r0.tel]]。")
    body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    rows = [r for group in body["groups"] for r in group["rows"]]
    flagged = [r for r in rows if r.get("issue") == "masked_cell"]
    assert len(flagged) == 1 and flagged[0]["ref"] == "v:Q1.r0.tel" and "遮罩" in flagged[0]["note"], flagged
    # 这一行自己不写值（所在的句子是报告原文，标注之后照常产出，读者本来就看得到）
    assert leaks([flagged[0][k] for k in ("text", "ref", "note")]) == []
    csv = (await client.get(f"/api/runs/{row.id}/evidence/audit", params={"format": "csv"})).text
    assert "引用了遮罩列" in csv
