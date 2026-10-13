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
SECRETS = ("1390000000", "张伟", "李娜", "王芳", "刘洋", "139-0000-0003", "139 0000 0003")
SALES = ("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, buyer_name TEXT, amount INTEGER,"
         " note TEXT);"
         "INSERT INTO sales VALUES (1, '华东', '13900000001', '张伟', 1200, '客户13900000001来电改址'),"
         " (2, '华东', '13900000002', '李娜', 300, '例行回访'), (3, '华南', '13900000003', '王芳', 900, '回访：联系 139-0000-0003'),"
         " (4, '华北', '13900000004', '刘洋', 400, '');"
         # 联系方式表的号码列、客户资料的 JSON 列都没设遮罩（json_shop 另设了 profile）：同一批号码、姓名写在这里
         "CREATE TABLE contacts (id INTEGER PRIMARY KEY, phone_num TEXT, visits INTEGER);"
         "INSERT INTO contacts VALUES (1, '13900000001', 5), (2, '139 0000 0003', 2), (3, '13800000000', 1);"
         "CREATE TABLE customers (id INTEGER PRIMARY KEY, region TEXT, profile TEXT);"
         "INSERT INTO customers VALUES (1, '华东', '{\"phone\": \"13900000001\", \"name\": \"张伟\"}'),"
         " (2, '华南', '{\"phone\": \"13900000002\", \"name\": \"李娜\"}');")
VALUE_SCHEMA = {"type": "object", "properties": {"value": {"type": "number", "description": "要核对的数"}},
                "required": ["value"]}


@pytest.fixture(autouse=True)
async def clean_spend():
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([spend.SPEND_KEY, "verify"])))
        await session.commit()
    yield


async def _source(tmp_path, options: dict | None, script: str = SALES) -> tuple[str, int]:
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    db = sqlite3.connect(path)
    db.executescript(script)
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
async def json_shop(tmp_path):
    """同一个库，客户资料（JSON：手机号、姓名）这一列设了遮罩。返回源名。"""
    name, source_id = await _source(tmp_path, {"mask_columns": "profile"})
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
    # mask_columns 照记查询当时数据源设的遮罩列（证据面板按它从表的字段清单里去掉列），改了名字的结果列只记在
    # masked_at_query：混进去的话，同源别的表里同名的真实字段（name、region）会从字段清单里消失
    assert snapshot["mask_columns"] == ["buyer_phone", "buyer_name"]

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
    # 汇总里的 CASE 只在条件里用到手机号：同写在 WHERE 里，值全部来自金额
    ("SELECT SUM(CASE WHEN buyer_phone LIKE '139%' THEN amount ELSE 0 END) AS total FROM sales", 2800),
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


# --------------------------------------------------------------------------
# 别的查询里遮掉的值、调用方给的值：后面的查询按值遮（复核第 1 轮）
# --------------------------------------------------------------------------


async def test_values_masked_in_an_earlier_query_are_masked_in_later_ones(monkeypatch, masked_shop):
    """Q1 选出手机号（查询时整列遮掉，值并进内存）；之后的查询里，没设遮罩的联系方式表、备注写着同一批号码（换了写法的
    也算）：整格是号码的整格换，夹在文字里的换那一截。交给模型的、落库的、通用接口给的都是遮好的。以前只按这一份结果
    里的遮罩值换，Q2 原样交给模型、原样落库，质询工件事后也收不到 Q1 的值。"""
    name = masked_shop
    steps = [q(name, "SELECT buyer_phone, amount AS total FROM sales ORDER BY id", "c1"),
             q(name, "SELECT phone_num, visits FROM contacts ORDER BY id", "c2"),
             q(name, "SELECT id, note FROM sales ORDER BY id", "c3")]
    script = Script(monkeypatch, steps, {"value": {"alias": "Q2", "row": 0, "column": "visits"}, "rationale": "x"})
    out, _ = await ask(name)
    assert out.error is None and out.result["value"] == 5
    seen = script.tool_results()
    assert all(_clean(seen[c]) for c in ("c1", "c2", "c3")), seen
    contacts = artifact_store.load(out.queries[1]["artifact"])
    assert contacts["rows"] == [[MASKED, 5], [MASKED, 2], ["13800000000", 1]]
    assert contacts["masked_at_query"] == [], "按值换的不算整列遮：没对上的号码照常显示"
    notes = artifact_store.load(out.queries[2]["artifact"])
    assert _cells(notes, "note") == [f"客户{MASKED}来电改址", "例行回访", f"回访：联系 {MASKED}", ""]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for query in out.queries:
            got = await client.get(f"/api/artifacts/{query['artifact']}")
            assert got.status_code == 200 and _clean(got.text)


async def test_values_given_by_the_caller_are_masked_before_the_model_sees_them(monkeypatch, masked_shop):
    """调用方交进来的遮罩值（审批质询、反方审稿收的报告快照里的值）：第一条查询就按它遮，模型看不到。"""
    name = masked_shop
    given = masking._Secrets()
    given.add({"columns": ["buyer_phone"], "rows": [["13900000001"], ["13900000003"]]}, {"buyer_phone"})
    script = Script(monkeypatch, [q(name, "SELECT id, note, amount AS total FROM sales ORDER BY id", "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "total"}, "rationale": "x"})
    out, _ = await ask(name, masked_values=given)
    assert out.result["value"] == 1200 and _clean(script.tool_results()["c1"])
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert _cells(snapshot, "note") == [f"客户{MASKED}来电改址", "例行回访", f"回访：联系 {MASKED}", ""]
    assert not given.strong - out.masked_values.strong, "交进来的值随遮掉的值一起交回"


async def test_a_reference_to_a_cell_masked_by_value_says_so(monkeypatch, masked_shop):
    """格子引用指到按值换成「已遮罩」的那一格：照实说，不说成「不是数值」。"""
    name = masked_shop
    steps = [q(name, "SELECT buyer_phone FROM sales ORDER BY id", "c1"),
             q(name, "SELECT phone_num, visits FROM contacts ORDER BY id", "c2")]
    Script(monkeypatch, steps, {"value": {"alias": "Q2", "row": 0, "column": "phone_num"}, "rationale": "x"})
    out, _ = await ask(name)
    assert out.result["value"] is None
    cell = out.cells[0]
    assert cell["status"] == "unresolved"
    assert cell["reason"] == "Q2 第 0 行「phone_num」与设置了遮罩的值相同，查询时已遮罩，不采信"


async def test_a_table_function_over_a_masked_json_column_is_masked(monkeypatch, json_shop):
    """遮罩的是 JSON 列（客户资料）：用 json_each 拆开是最自然的写法，拆出来的 key、value 在交给模型、落库之前就遮掉。
    以前原样交给模型并落库，masked_at_query 是空的。"""
    name = json_shop
    sql = "SELECT c.id AS 编号, j.key, j.value FROM customers c, json_each(c.profile) j ORDER BY c.id, j.key"
    script = Script(monkeypatch, [q(name, sql, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "编号"}, "rationale": "x"})
    out, _ = await ask(name)
    assert out.result["value"] == 1 and _clean(script.tool_results()["c1"])
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["masked_at_query"] == ["key", "value"] and _cells(snapshot, "编号") == [1, 1, 2, 2]
    assert _clean(json.dumps(snapshot, ensure_ascii=False))


async def test_a_failed_query_does_not_hand_masked_values_to_the_model(monkeypatch, masked_shop):
    """数据库的报错会带上数据里的值（Postgres 类型转换失败：invalid input syntax for type bigint: "139-0000-0001"）：
    数据源设了遮罩时，引号里、SQL 里没写着的那一段和不带引号的长数换成「已遮罩」；模型自己写的名字照留（它据此改写
    SQL）。遮罩开关没开的（Agent 节点）照旧原话。"""
    from app.tools import datasource

    said = {"message": ""}

    async def failing(source, sql, **kwargs):
        raise RuntimeError(said["message"])

    monkeypatch.setattr(datasource, "run_query", failing)
    sql = "SELECT CAST(buyer_phon AS BIGINT) AS p FROM sales WHERE id = 12345"
    ctx = ToolContext(run_id="run-error", node_id="n1", data_versions=Versions.CURRENT)
    async with SessionLocal() as session:
        [masking_tool] = await build_datasource_tools([f"db_query__{masked_shop}"], ctx, session, mask_results=True)
        [plain_tool] = await build_datasource_tools([f"db_query__{masked_shop}"], ctx, session)
    for message, shown in [
        ('invalid input syntax for type bigint: "139-0000-0001"', 'invalid input syntax for type bigint: "已遮罩"'),
        ("value '13900000001' is out of range for type integer", "value '已遮罩' is out of range for type integer"),
        ("Conversion Error: Type INT64 with value 13900000001 can't be cast to INT32",
         "Conversion Error: Type INT64 with value 已遮罩 can't be cast to INT32"),
        ('column "buyer_phon" does not exist', 'column "buyer_phon" does not exist'),
        ("no rows near id 12345", "no rows near id 12345"),
    ]:
        said["message"] = message
        assert await masking_tool.ainvoke({"sql": sql}) == f"查询失败：{shown}"
        assert await plain_tool.ainvoke({"sql": sql}) == f"查询失败：{message}"


def test_a_result_from_a_source_without_masks_is_masked_by_the_values_seen_so_far():
    """数据源没设遮罩（另一个库里的联系方式表），此前查询遮掉的值照样按值换；这一份结果没有可收的值。"""
    known = masking._Secrets()
    known.add({"columns": ["buyer_phone"], "rows": [["13900000001"]]}, {"buyer_phone"})
    payload = {"sql": "SELECT phone_num, note FROM contacts", "columns": ["phone_num", "note"],
               "rows": [["13900000001", "客户 139 0000 0001 来电"], ["13800000000", "例行回访"]]}
    done = masking.mask_query_result(payload, [], "sqlite", known)
    assert done.payload["rows"] == [[MASKED, f"客户 {MASKED} 来电"], ["13800000000", "例行回访"]]
    assert done.columns == [] and not done.secrets.strong
    assert masking.mask_query_result(payload, [], "sqlite").payload is payload, "什么都没收到时原样"


def test_labels_written_in_this_query_are_not_masked_by_values_from_earlier_ones():
    """分档标签是这条 SQL 自己写的字：此前查询里认不准、当遮罩值收了的同名标签，不把这一份的标签列换掉。"""
    known = masking._Secrets()
    known.add({"columns": ["档"], "rows": [["大单"], ["小单"]]}, {"档"})
    payload = {"sql": "SELECT CASE WHEN amount > 1000 THEN '大单' ELSE '小单' END AS 档, COUNT(*) AS 笔数 FROM sales "
                      "GROUP BY 1", "columns": ["档", "笔数"], "rows": [["大单", 1], ["小单", 3]]}
    assert masking.mask_query_result(payload, ["amount"], "sqlite", known).payload["rows"] == [["大单", 1], ["小单", 3]]


# --------------------------------------------------------------------------
# * 把遮罩列带进别的列名、函数参数里的 JSON 箭头（复核第 2 轮）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sql, masked", [
    pytest.param("SELECT region, amount FROM sales WHERE id < 0 UNION ALL "
                 "SELECT * FROM (SELECT buyer_phone, amount FROM sales ORDER BY id)", ["region"],
                 id="UNION 后一支的 *"),
    pytest.param("WITH t(k, v) AS (SELECT * FROM (SELECT buyer_name, amount FROM sales ORDER BY id)) "
                 "SELECT k AS 姓名, v AS amount FROM t", ["姓名"], id="CTE 列名表套 *"),
    pytest.param("SELECT (SELECT * FROM (SELECT s2.buyer_phone FROM sales s2 WHERE s2.id = s.id)) AS 号码, s.amount "
                 "FROM sales s ORDER BY s.id", ["号码"], id="标量子查询里的 *"),
])
async def test_a_star_renaming_a_masked_column_is_masked_before_the_model_sees_it(monkeypatch, masked_shop, sql,
                                                                                    masked):
    """* 把遮罩列的值带进了别的列名（UNION 后一支、写了列名表的 CTE、标量子查询）：交给模型的、落库的、通用接口给的都是
    「已遮罩」，金额照常。以前 * 本身不算取自哪一列，原值原样交给模型、落进快照。"""
    name = masked_shop
    script = Script(monkeypatch, [q(name, sql, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "amount"}, "rationale": "见 [[see:Q1]]。"})
    out, _ = await ask(name)
    assert out.error is None and out.result["value"] == 1200
    seen = script.tool_results()["c1"]
    assert MASKED in seen and _clean(seen), seen
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["masked_at_query"] == masked and set(_cells(snapshot, masked[0])) == {MASKED}
    assert _cells(snapshot, "amount") == [1200, 300, 900, 400]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        got = await client.get(f"/api/artifacts/{out.queries[0]['artifact']}")
    assert got.status_code == 200 and got.json()["content"] == snapshot and _clean(got.text)


@pytest.mark.parametrize("sql, masked", [
    pytest.param("SELECT c.id AS 编号, j.value AS 值 FROM customers c, json_each(profile->'$') AS j "
                 "ORDER BY c.id, j.key", ["值"], id="json_each 参数带箭头"),
    pytest.param("SELECT id AS 编号, upper(profile->'$.name') AS 姓名 FROM customers ORDER BY id", ["姓名"],
                 id="函数参数里的箭头"),
])
async def test_a_json_arrow_over_the_masked_column_is_masked_before_the_model_sees_it(monkeypatch, json_shop, sql,
                                                                                      masked):
    """遮罩的是 JSON 列：函数参数里没写表名的 profile->'…' 被解析器当成 lambda，以前认不出，拆开、取出来的号码和姓名原样
    交给模型、落库。"""
    name = json_shop
    script = Script(monkeypatch, [q(name, sql, "c1")],
                    {"value": {"alias": "Q1", "row": 0, "column": "编号"}, "rationale": "x"})
    out, _ = await ask(name)
    assert out.result["value"] == 1 and _clean(script.tool_results()["c1"])
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["masked_at_query"] == masked and set(_cells(snapshot, masked[0])) == {MASKED}
    assert _clean(json.dumps(snapshot, ensure_ascii=False))


# --------------------------------------------------------------------------
# 遮罩值的上限：agent 自己查到的值先收（复核第 2 轮）
# --------------------------------------------------------------------------


async def test_values_the_agent_finds_survive_caller_values_that_fill_the_cap(monkeypatch, masked_shop):
    """调用方交进来的值（报告快照里的）收满了上限 SECRETS_MAX：agent 自己查到的号码照样收，后面查到的备注按它遮，交回
    调用方的 masked_values 里也认得出。以前先并调用方的值、满了就停，agent 查到的一个也并不进去：后面的备注原样交给
    模型、原样落库，回答时按值遮也认不出。"""
    monkeypatch.setattr(masking, "SECRETS_MAX", 40)
    name = masked_shop
    given = masking._Secrets()
    given.add({"columns": ["buyer_phone"], "rows": [[str(13800000000 + i)] for i in range(40)]}, {"buyer_phone"})
    assert len(given.weak) == masking.SECRETS_MAX and not given.hit("13900000001")
    steps = [q(name, "SELECT buyer_phone, amount AS total FROM sales ORDER BY id", "c1"),
             q(name, "SELECT id, note FROM sales ORDER BY id", "c2")]
    script = Script(monkeypatch, steps, {"value": {"alias": "Q1", "row": 0, "column": "total"}, "rationale": "x"})
    out, _ = await ask(name, masked_values=given)
    assert out.result["value"] == 1200
    assert _clean(script.tool_results()["c2"]), script.tool_results()["c2"]
    notes = artifact_store.load(out.queries[1]["artifact"])
    assert _cells(notes, "note") == [f"客户{MASKED}来电改址", "例行回访", f"回访：联系 {MASKED}", ""]
    assert out.masked_values.hit("13900000001") and out.masked_values.hit("13900000003")
    assert len(out.masked_values.weak) <= masking.SECRETS_MAX, "合起来照样不超过上限：挤掉的是调用方的值"


#: 两个月的订单表，列一样、次序一样（上传表格里的月度工作表常这样）
MONTHS = ("CREATE TABLE jan (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, amount INTEGER);"
          "CREATE TABLE feb (id INTEGER PRIMARY KEY, region TEXT, buyer_phone TEXT, amount INTEGER);"
          "INSERT INTO jan VALUES (1, '华东', '13900000001', 1200), (2, '华南', '13900000002', 300);"
          "INSERT INTO feb VALUES (3, '华东', '13900000003', 900);")


async def test_a_union_of_monthly_tables_is_counted_with_the_sources_schema(monkeypatch, tmp_path):
    """几张列一样的月度表用 SELECT * 连起来再汇总：按数据源的表结构数得出号码落在第几列，汇总的金额照常交给模型、照常
    取值；选出全部列的，只遮号码那一列。* 展开数据源里的表又不按表结构数位置的话，展开落到的那一段整段都遮，金额也拿不到。"""
    name, source_id = await _source(tmp_path, {"mask_columns": "buyer_phone"}, script=MONTHS)
    try:
        steps = [q(name, "SELECT region, SUM(amount) AS total FROM (SELECT * FROM jan UNION ALL SELECT * FROM feb) t "
                          "GROUP BY region ORDER BY total DESC", "c1"),
                 q(name, "SELECT * FROM jan UNION ALL SELECT * FROM feb ORDER BY id", "c2")]
        script = Script(monkeypatch, steps, {"value": {"alias": "Q1", "row": 0, "column": "total"}, "rationale": "x"})
        out, _ = await ask(name)
        assert out.error is None and out.result["value"] == 2100
        assert all(_clean(text) for text in script.tool_results().values())
        totals, rows = (artifact_store.load(query["artifact"]) for query in out.queries)
        assert totals["masked_at_query"] == [] and totals["rows"] == [["华东", 2100], ["华南", 300]]
        assert rows["masked_at_query"] == ["buyer_phone"] and _cells(rows, "amount") == [1200, 300, 900]
    finally:
        await engines.invalidate(source_id)


# --------------------------------------------------------------------------
# 过期的表结构、写了库名的表、整行与列名表（复核第 3 轮）
# --------------------------------------------------------------------------


#: 一月那张表后来多了一列 extra（排在第二），数据源的表结构是那之前探查的；二月那张号码排在第二列
REBUILT = ("CREATE TABLE jan (id INTEGER PRIMARY KEY, extra TEXT, region TEXT, buyer_phone TEXT, amount INTEGER);"
           "CREATE TABLE feb (id INTEGER PRIMARY KEY, buyer_phone TEXT, region TEXT, amount INTEGER, extra TEXT);"
           "INSERT INTO jan VALUES (1, '加急', '华东', '13900000001', 1200);"
           "INSERT INTO feb VALUES (2, '13900000002', '华南', 300, '普通');")


async def test_a_stale_schema_does_not_place_masked_values_by_its_old_layout(monkeypatch, tmp_path):
    """数据源的表结构过期了（一月那张少了 extra）：UNION 第一支的 * 按它数出的列名和后一支的列数对不上，不按它对位置，整份
    遮。以前照过期的列名对，二月的号码落进的「extra」认不出，原样交给模型、落库。"""
    name, source_id = await _source(tmp_path, {"mask_columns": "buyer_phone"}, script=REBUILT)
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        cache = dict(row.schema_cache)
        jan = dict(cache["tables"]["jan"])
        jan["columns"] = [c for c in jan["columns"] if c["name"] != "extra"]
        row.schema_cache = {**cache, "tables": {**cache["tables"], "jan": jan}}
        await session.commit()
    try:
        sql = ("WITH t AS (SELECT * FROM jan UNION ALL SELECT id, buyer_phone, region, amount, extra FROM feb) "
               "SELECT extra AS 备注, id AS n FROM t ORDER BY id")
        script = Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 0, "column": "n"},
                                                            "rationale": "x"})
        out, _ = await ask(name)
        seen = script.tool_results()["c1"]
        assert MASKED in seen and _clean(seen), seen
        snapshot = artifact_store.load(out.queries[0]["artifact"])
        assert "备注" in snapshot["masked_at_query"] and _clean(json.dumps(snapshot, ensure_ascii=False))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            got = await client.get(f"/api/artifacts/{out.queries[0]['artifact']}")
        assert got.status_code == 200 and _clean(got.text)
    finally:
        await engines.invalidate(source_id)


#: 造出来的 Postgres、Oracle 数据源的表结构：探查的是当前库（没设 schema），sales 一张表
PG_LAYOUT = ["id", "region", "buyer_phone", "buyer_name", "amount", "note"]
PG_SCHEMA = {"tables": {"sales": {"qualified": "sales", "schema": None,
                                  "columns": [{"name": c, "type": "TEXT"} for c in PG_LAYOUT]}}, "schema": None}
PG_ROWS = [[1, "华东", "13900000001", "张伟", 1200, "改址"], [2, "华南", "13900000002", "李娜", 300, "回访"]]


def _json_row(row: list) -> str:
    return json.dumps(dict(zip(PG_LAYOUT, row)), ensure_ascii=False)


#: (数据源类型, SQL, 数据库交回的行, 查询时就整列遮掉的列, 编号那一格取到的值)。结果列按 SQL 里起的别名（j、n）。行照
#: 驱动交回的样子造：JSON 是字符串，记录转成文字；archive.sales 是另一个库里的同名表，号码排在第二列。表的列名表只写了
#: 前几列（Postgres 后面的列照原名）、另一个库里的同名表：说不清号码改叫了哪个名字，那一段整段遮，编号也遮
DRIVER_CASES = [
    ("postgresql", "SELECT json_agg(t) AS j, 1 AS n FROM (SELECT id, buyer_phone FROM sales) t",
     [[json.dumps([{"id": r[0], "buyer_phone": r[2]} for r in PG_ROWS]), 1]], ["j"], 1),
    ("postgresql", "SELECT row_to_json(s) AS j, s.id AS n FROM sales s ORDER BY s.id",
     [[_json_row(r), r[0]] for r in PG_ROWS], ["j"], 1),
    ("postgresql", "SELECT to_json(s.*) AS j, s.id AS n FROM sales s ORDER BY s.id",
     [[_json_row(r), r[0]] for r in PG_ROWS], ["j"], 1),
    ("postgresql", "SELECT s AS j, s.id AS n FROM sales s ORDER BY s.id",
     [["(" + ",".join(map(str, r)) + ")", r[0]] for r in PG_ROWS], ["j"], 1),
    ("postgresql", "SELECT p AS j, i AS n FROM sales AS s(i, r, p) ORDER BY i", [[r[2], r[0]] for r in PG_ROWS],
     ["j", "n"], None),
    ("postgresql", "WITH t(a, b, c, d, e, f) AS (TABLE sales) SELECT c AS j, a AS n FROM t ORDER BY a",
     [[r[2], r[0]] for r in PG_ROWS], ["j"], 1),
    ("postgresql", "WITH t(a, b, c, d, e, f) AS (SELECT * FROM archive.sales) SELECT b AS j, a AS n FROM t",
     [["13900000007", 7]], ["j", "n"], None),
    ("postgresql", "WITH t(a, b, c, d, e, f) AS (SELECT * FROM sales) SELECT c AS j, a AS n FROM t ORDER BY a",
     [[r[2], r[0]] for r in PG_ROWS], ["j"], 1),
    ("oracle", "SELECT JSON_OBJECT(*) AS j, id AS n FROM sales ORDER BY id", [[_json_row(r), r[0]] for r in PG_ROWS],
     ["j"], 1),
    ("oracle", "SELECT id AS n, val AS j FROM sales UNPIVOT (val FOR col IN (buyer_phone, region)) ORDER BY id",
     [[1, "13900000001"], [1, "华东"], [2, "13900000002"], [2, "华南"]], ["n", "j"], None),
]


@pytest.fixture
async def driver_shop():
    """造一个 Postgres / Oracle 数据源（查询交给下面换掉的 run_query，不连库）：买家手机号、姓名设了遮罩。返回建源的函数。"""
    made: list[int] = []

    async def make(kind: str) -> str:
        name = f"pg_{uuid.uuid4().hex[:8]}"
        async with SessionLocal() as session:
            row = DataSource(name=name, kind=kind, host="127.0.0.1", port=5432, username="probe", database="shop",
                             readonly=True, enabled=True, options={"mask_columns": "buyer_phone, buyer_name"},
                             schema_cache=PG_SCHEMA)
            session.add(row)
            await session.commit()
            made.append(row.id)
        return name

    yield make
    for source_id in made:
        await engines.invalidate(source_id)


@pytest.mark.parametrize("kind, sql, rows, masked, value", DRIVER_CASES,
                         ids=["json_agg(t)", "row_to_json(s)", "to_json(s.*)", "SELECT s", "表的列名表", "CTE 里的 TABLE",
                              "另一个库里的同名表", "本库的表按位置数", "JSON_OBJECT(*)", "UNPIVOT"])
async def test_whole_rows_and_renamed_columns_from_other_databases_are_masked(monkeypatch, driver_shop, kind, sql,
                                                                              rows, masked, value):
    """Postgres 的整行（json_agg(t)、row_to_json(s)、to_json(s.*)、SELECT s）、表的列名表、CTE 里的 TABLE sales、另一个库里的
    同名表，Oracle 的 JSON_OBJECT(*)、UNPIVOT：交给模型的、落库的、通用接口给的都是遮好的，编号照常（认不出位置的整份遮）。
    以前这些写法 masked_at_query 是空的，号码、姓名原样交给模型、落库。"""
    from app.data.engine import QueryResult
    from app.tools import datasource

    async def driver(source, statement, **kwargs):
        assert statement == sql
        columns = ["n", "j"] if statement.startswith("SELECT id AS n") else ["j", "n"]
        return QueryResult(columns=columns, rows=[list(r) for r in rows], row_count=len(rows), truncated=False,
                           elapsed_ms=1, sql=statement)

    monkeypatch.setattr(datasource, "run_query", driver)
    name = await driver_shop(kind)
    script = Script(monkeypatch, [q(name, sql, "c1")], {"value": {"alias": "Q1", "row": 0, "column": "n"},
                                                        "rationale": "x"})
    out, events = await ask(name)
    seen = script.tool_results()["c1"]
    assert MASKED in seen and _clean(seen), seen
    snapshot = artifact_store.load(out.queries[0]["artifact"])
    assert snapshot["masked_at_query"] == masked and _clean(json.dumps(snapshot, ensure_ascii=False))
    assert out.result["value"] == value
    [end] = [d for k, d in events if k == EventType.TOOL_END]
    assert _clean(end["preview"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        got = await client.get(f"/api/artifacts/{out.queries[0]['artifact']}")
    assert got.status_code == 200 and _clean(got.text)
