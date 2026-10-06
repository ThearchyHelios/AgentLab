"""核查 agent 看到的查询结果先遮、存下的快照也遮（engine/verify_agent.py 建查询工具时打开 tools/datasource 的遮罩开关）。

双盲复算、反方审稿、审批质询共用的核查 agent 以前把查询结果原样交给模型，遮罩是事后按值做的（审批质询的 _Secrets），
每一轮复核都找到新的漏法。现在在工具边界先遮：数据源设了遮罩时，按 SQL 认出取自遮罩列的结果列（别名、函数、子查询、
WITH、SELECT *），连同列名直接对上的，这些格子在交给模型的结果、落库的快照里都是「已遮罩」，快照记下哪些列在查询时
就遮了；别的文字格里夹着这份结果里遮罩值的那一截同样换掉（换了写法的号码也认）。运行详情「工件」页签走的通用接口
GET /api/artifacts/{id} 给的也就是遮好的。没碰遮罩列的聚合查询原样。遮掉的值只在内存里交回调用方（审批质询按值遮的
第二道防线用），不进快照、不交给模型。遮罩开关缺省关着，别的调用方（Agent 节点、调用工具节点）照旧。

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，手机号、姓名、金额都是虚构的。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core import artifact_store
from app.core.events import EventType
from app.data import introspect
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Setting
from app.engine import masking, spend
from app.engine.verify_agent import run_verify_agent
from app.main import app
from app.tools.datasource import build_datasource_tools
from app.tools.registry import ToolContext, Versions
from tests.test_verify_agent import SPEC, Script, q

MASKED = "已遮罩"
#: 遮罩列里的值（和备注里换了写法的号码）：交给模型的、存下的、接口给的都不能出现
SECRETS = ("1390000000", "张伟", "李娜", "王芳", "刘洋", "139-0000-0003")
SALES = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, buyer_name TEXT, amount INTEGER,"
         " note TEXT);"
         "INSERT INTO sales VALUES (1, '华东', '13900000001', '张伟', 1200, '客户13900000001来电改址'),"
         " (2, '华东', '13900000002', '李娜', 300, '例行回访'), (3, '华南', '13900000003', '王芳', 900, '回访：联系 139-0000-0003'),"
         " (4, '华北', '13900000004', '刘洋', 400, '');")
VALUE_SCHEMA = {"type": "object", "properties": {"value": {"type": "number", "description": "要核对的数"}},
                "required": ["value"]}


@pytest.fixture(autouse=True)
async def clean_spend():
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([spend.SPEND_KEY, "verify"])))
        await session.commit()
    yield


async def _source(tmp_path, options: dict | None) -> tuple[str, int]:
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(SALES)
    db.commit()
    db.close()
    name = f"shop_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True, options=options or {})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        return name, row.id


@pytest.fixture
async def masked_shop(tmp_path):
    """四笔订单，买家手机号、姓名两列设了遮罩；备注没设遮罩，其中两条写着手机号（一条换了写法）。返回源名。"""
    name, source_id = await _source(tmp_path, {"mask_columns": "buyer_phone, buyer_name"})
    yield name
    await engines.invalidate(source_id)


@pytest.fixture
async def plain_shop(tmp_path):
    name, source_id = await _source(tmp_path, None)
    yield name
    await engines.invalidate(source_id)


async def ask(name: str, **kwargs):
    """按剧本跑一次核查 agent（剧本由各个测试先用 Script 装好），交回结果和发出的事件。"""
    events: list[tuple] = []
    out = await run_verify_agent(spec=SPEC, system="核对一个数。", task="查一下。", sources=[name],
                                 output_schema=VALUE_SCHEMA, data_versions=Versions.CURRENT, run_id="run-mask",
                                 node_id="check", emit=lambda kind, **data: events.append((kind, data)), **kwargs)
    return out, events


def _clean(text: str) -> bool:
    return not any(s in text for s in SECRETS)


def _cells(snapshot: dict, column: str) -> list:
    i = snapshot["columns"].index(column)
    return [row[i] for row in snapshot["rows"]]


@pytest.mark.parametrize("sql, masked", [
    pytest.param("SELECT buyer_phone, amount FROM sales ORDER BY id", ["buyer_phone"], id="直接选出"),
    pytest.param("SELECT buyer_phone AS 手机号, amount AS 金额 FROM sales ORDER BY id", ["手机号"], id="起别名"),
    pytest.param("SELECT UPPER(buyer_name) AS 姓名, amount FROM sales ORDER BY id", ["姓名"], id="套函数"),
    pytest.param("SELECT t.p AS 号码, t.amount FROM (SELECT id, buyer_phone AS p, amount FROM sales) t ORDER BY t.id",
                 ["号码"], id="子查询"),
    pytest.param("WITH t AS (SELECT id, buyer_phone AS p, amount FROM sales) SELECT p, amount FROM t ORDER BY id",
                 ["p"], id="WITH"),
    pytest.param("SELECT * FROM sales ORDER BY id", ["buyer_phone", "buyer_name"], id="SELECT *"),
])
async def test_masked_columns_are_masked_before_the_model_sees_them(monkeypatch, masked_shop, sql, masked):
    name = masked_shop
    amount = "amount" if "金额" not in sql else "金额"
    script = Script(monkeypatch, [q(name, sql, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": amount}, "rationale": "见 [[see:Q1]]。"})
    out, events = await ask(name)
    assert out.error is None and out.result["value"] == 1200, "没遮的列照常取值"

    # 交给模型的工具结果：遮罩列是「已遮罩」，别的列照原样
    seen = script.tool_results()["c1"]
    assert seen.startswith("【证据 Q1】") and MASKED in seen and _clean(seen), seen
    payload = json.loads(seen.split("\n", 1)[1])
    for column in masked:
        assert set(_cells(payload, column)) == {MASKED}
    assert _cells(payload, amount) == [1200, 300, 900, 400]
    assert "mask_columns" not in payload and "masked_at_query" not in payload, "交给模型的结果不多出字段"

    # 落库的快照：同样遮好，记下查询时就遮了的列；数据源设的遮罩列照记，换了名字的结果列也记进去（证据面板据此画成遮罩）
    [query] = out.queries
    snapshot = artifact_store.load(query["artifact"])
    assert _clean(json.dumps(snapshot, ensure_ascii=False))
    for column in masked:
        assert set(_cells(snapshot, column)) == {MASKED}
        assert snapshot.get("column_types", {}).get(column, "text") == "text"
    assert _cells(snapshot, amount) == [1200, 300, 900, 400]
    assert snapshot["masked_at_query"] == masked
    assert {c.lower() for c in snapshot["mask_columns"]} == {"buyer_phone", "buyer_name", *(c.lower() for c in masked)}

    # 运行详情「工件」页签走的通用接口：给的就是遮好的
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        got = await client.get(f"/api/artifacts/{query['artifact']}")
    assert got.status_code == 200 and got.json()["content"] == snapshot and _clean(got.text)

    # 事件（tool.end 的预览）、收尾时交给模型的查询结果里也没有
    [end] = [d for k, d in events if k == EventType.TOOL_END]
    assert MASKED in end["preview"] and _clean(end["preview"])
    final = "\n".join(str(m.content) for m in script.calls[-1][1])
    assert MASKED in final and _clean(final)

    # 遮掉的值只在内存里交回调用方，不跟着结果往外交
    assert out.masked_values is not None and out.masked_values.strong
    assert "masked_values" not in out.as_dict()


@pytest.mark.parametrize("sql, value", [
    ("SELECT SUM(amount) AS total FROM sales", 2800),
    ("SELECT COUNT(DISTINCT buyer_phone) AS total FROM sales", 4),
    ("SELECT SUM(amount) AS total FROM sales WHERE buyer_phone = '13900000001'", 1200),
    ("SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY total DESC", 1500),
    ("SELECT region, note, amount AS total FROM sales ORDER BY id", 1200),
])
async def test_queries_that_do_not_read_masked_values_are_untouched(monkeypatch, masked_shop, sql, value):
    """没碰遮罩列的值（汇总、只数个数、只在条件里用到）：结果原样，取值照常，快照记下查询时一列都没遮。只遮这份结果
    里的遮罩值：备注里写着手机号、这份结果却没有选出手机号的，按值认不出（那是数据源的遮罩没设全）。"""
    name = masked_shop
    script = Script(monkeypatch, [q(name, sql, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "total"}, "rationale": "见 [[see:Q1]]。"})
    out, _ = await ask(name)
    assert out.error is None and out.result["value"] == value
    seen = script.tool_results()["c1"]
    assert MASKED not in seen
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["masked_at_query"] == [] and MASKED not in json.dumps(snapshot, ensure_ascii=False)
    assert [c.lower() for c in snapshot["mask_columns"]] == ["buyer_phone", "buyer_name"]


async def test_masked_values_inside_other_text_cells_are_cut_out(monkeypatch, masked_shop):
    """同一份结果里选出了手机号：备注里夹着的同一个号码（换了写法的也算）换成「已遮罩」，备注别的字照原样。"""
    name = masked_shop
    sql = "SELECT buyer_phone, note, amount AS total FROM sales ORDER BY id"
    script = Script(monkeypatch, [q(name, sql, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "total"}, "rationale": "见 [[see:Q1]]。"})
    out, _ = await ask(name)
    seen = script.tool_results()["c1"]
    assert _clean(seen)
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert _cells(snapshot, "note") == [f"客户{MASKED}来电改址", "例行回访", f"回访：联系 {MASKED}", ""]
    assert snapshot["masked_at_query"] == ["buyer_phone"], "备注只换了一截，不算整列遮"


async def test_a_cell_reference_into_a_masked_column_is_not_taken(monkeypatch, masked_shop):
    """模型交的格子引用指到查询时就遮了的那一列：不采信，照实说是遮罩列，不说成「不是数值」。"""
    name = masked_shop
    sql = "SELECT buyer_phone AS 手机号, amount FROM sales ORDER BY id"
    Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 0, "column": "手机号"}, "rationale": "x"})
    out, _ = await ask(name)
    assert out.result["value"] is None
    [cell] = out.cells
    assert cell["status"] == "unresolved" and "1390000000" not in json.dumps(cell)
    assert cell["reason"] == "Q1 的「手机号」取自设置了遮罩的列，查询时已遮罩，不采信"


async def test_sql_that_cannot_be_read_but_names_a_masked_column_masks_the_whole_result(monkeypatch, masked_shop):
    """SQL 认不出哪一列取自遮罩列、里面又写着遮罩列：整份结果的列都遮（和双盲复算、审批质询同一个保守规则）。"""
    name = masked_shop
    monkeypatch.setattr(masking, "_tainted", lambda *args, **kwargs: None)
    sql = "SELECT buyer_phone, amount FROM sales ORDER BY id"
    Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 0, "column": "amount"}, "rationale": "x"})
    out, _ = await ask(name)
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["masked_at_query"] == ["buyer_phone", "amount"]
    assert set(_cells(snapshot, "amount")) == {MASKED} and out.result["value"] is None


async def test_the_switch_is_off_by_default_and_a_source_without_masks_is_untouched(masked_shop, plain_shop):
    """遮罩开关缺省关着：Agent 节点、调用工具节点建的查询工具照旧交回原样的结果。没设遮罩的数据源开着开关也一字不差
    （快照里不多出键）。"""
    sql = "SELECT buyer_phone, amount FROM sales ORDER BY id"
    ctx = ToolContext(run_id="run-switch", node_id="n1", data_versions=Versions.CURRENT)
    async with SessionLocal() as session:
        [plain_tool] = await build_datasource_tools([f"db_query__{masked_shop}"], ctx, session)
        [masking_tool] = await build_datasource_tools([f"db_query__{masked_shop}"], ctx, session, mask_results=True)
        [unmasked_tool] = await build_datasource_tools([f"db_query__{plain_shop}"], ctx, session, mask_results=True)
    raw = json.loads(await plain_tool.ainvoke({"sql": sql}))
    assert _cells(raw, "buyer_phone")[0] == "13900000001"
    stored = artifact_store.load(raw["artifact"])
    assert _cells(stored, "buyer_phone")[0] == "13900000001" and "masked_at_query" not in stored
    shown = json.loads(await masking_tool.ainvoke({"sql": sql}))
    assert set(_cells(shown, "buyer_phone")) == {MASKED}
    untouched = json.loads(await unmasked_tool.ainvoke({"sql": sql}))
    stored = artifact_store.load(untouched["artifact"])
    assert _cells(stored, "buyer_phone")[0] == "13900000001"
    assert "masked_at_query" not in stored and "mask_columns" not in stored


async def test_column_names_the_model_wrote_are_kept(monkeypatch, masked_shop):
    """列名是模型自己写在 SQL 里的（哪怕写着一个号码）：工具边界只遮格子、不改列名，它引用的列名才对得上。"""
    name = masked_shop
    sql = 'SELECT buyer_phone, amount AS "13900000001 的金额" FROM sales ORDER BY id'
    Script(monkeypatch, [q(name, sql, "c1")],
           {"value": {"alias": "Q1", "row": 0, "column": "13900000001 的金额"}, "rationale": "x"})
    out, _ = await ask(name)
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["columns"] == ["buyer_phone", "13900000001 的金额"]
    assert set(_cells(snapshot, "buyer_phone")) == {MASKED} and out.result["value"] == 1200
