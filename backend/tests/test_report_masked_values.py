"""报告自查（第 2 轮）：只数个数的列不算取自遮罩列；正文里直接写着的遮罩值按值认出来。

复核发现：
- 只数个数、只决定算不算进来的写法——COUNT(*) FILTER (WHERE phone IS NULL)、SUM(phone IS NULL)、AVG(phone IS NOT NULL)、
  SUM(amount) FILTER (WHERE phone IS NOT NULL)——值里没有号码，可认遮罩列的那一套（masking._value_columns）把 FILTER 的
  条件、汇总里的真假条件当成值的来历，自查跟着判 masked_cell，正式运行（默认违规即失败）因此判失败。等价的
  SUM(CASE WHEN phone IS NULL THEN 1 ELSE 0 END) 本来就不算。现在自查认之前先把这几种写法改写成认得出的样子
  （api/evidence.py 的 _counted），证据面板、推演照可验证报告的认法多遮（展示上多遮无妨）
- 自查只认 [[v:]] 引用：写作者照着「删掉引用」把号码、姓名照抄成正文，或者经指标带出来（[[m:phone_raw]] 的值就是号码），
  一条违规都没有。现在按值认一遍（engine/evidence.py 的 masked_values）：写作目录里取自遮罩列的值（只认确定取自遮罩列的
  列，整份算进来的不算）出现在正文里，记一条 masked_cell，不带值；运行输入里写着的值是发起运行的人自己填的，不算

真跑的部分用虚构的 SQLite 商城库（号码、姓名是编的），写作者是 MockChatModel 照剧本写。
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
from app.engine.evidence import compose_doc, describe_violations
from app.engine.runner import run_manager

CUSTOMERS = [(1, "陈思远", "19900000701"), (2, "林若溪", "19900000702"), (3, "周以宁", "19900000703")]
ORDERS = [(1, 1, 120.5), (2, 2, 300.0), (3, 2, 80.0), (4, 3, 210.25), (5, 1, 99.5)]
PHONES = [c[2] for c in CUSTOMERS]
NAMES = [c[1] for c in CUSTOMERS]


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def set_source(path, options=None) -> str:
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


def leaks(value) -> list[str]:
    """写着的号码、姓名，按千分位写的（19,900,000,701）也算。"""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in [*PHONES, *NAMES] if p in raw or p in raw.replace(",", "")]


def masked_of(violations: list[dict]) -> list[dict]:
    return [v for v in violations if v["code"] == "masked_cell"]


# --------------------------------------------------------------------------
# 只数个数的列
# --------------------------------------------------------------------------

#: 值里没有号码的写法：条件只决定这一行算不算进来，汇总里的真假条件只数个数
COUNTING = [
    "SELECT COUNT(*) FILTER (WHERE phone IS NULL) AS missing FROM customers",
    "SELECT SUM(phone IS NULL) AS missing FROM customers",
    "SELECT AVG(phone IS NOT NULL) AS missing FROM customers",
    "SELECT SUM(LENGTH(phone) = 11) AS missing FROM customers",
    "SELECT MAX((phone LIKE '199%')) AS missing FROM customers",
    "SELECT SUM(o.amount) FILTER (WHERE c.phone IS NOT NULL) AS missing FROM orders o "
    "JOIN customers c ON c.id = o.customer_id",
    "SELECT COUNT(*) FILTER (WHERE phone IS NULL) OVER () AS missing FROM customers",
]


@pytest.mark.parametrize("sql", COUNTING)
async def test_counting_columns_are_not_surely_masked(engine_up, mall, sql):
    """自查不为它们判违规（sure 里没有）；证据面板照可验证报告的认法多遮（hidden 里有，只影响展示）。"""
    from app.api.evidence import _mask_sets, _Masks

    sets = await _mask_sets({"sql": sql, "columns": ["missing"], "rows": [[1]], "source": "mall"}, "mall", _Masks())
    assert sets.sure == set() and "missing" in sets.hidden, (sets.sure, sets.hidden)


@pytest.mark.parametrize("sql", [
    # 值本身取自遮罩列的汇总、逐行的函数照旧算
    "SELECT MAX(phone) AS missing FROM customers",
    "SELECT SUBSTR(phone, 1, 3) AS missing FROM customers",
    "SELECT MAX(phone) FILTER (WHERE id > 1) AS missing FROM customers",
    # 逐行的真假条件（不汇总）照旧算：每一行是真是假本身就是这一行的信息，同逐行的 CASE
    "SELECT phone IS NULL AS missing FROM customers",
])
async def test_values_taken_from_the_masked_column_stay_surely_masked(engine_up, mall, sql):
    from app.api.evidence import _mask_sets, _Masks

    sets = await _mask_sets({"sql": sql, "columns": ["missing"], "rows": [[1]], "source": "mall"}, "mall", _Masks())
    assert sets.sure == {"missing"}, sets.sure


@pytest.mark.parametrize("sql, columns", [
    # SQL 解析不了：认不出哪一列取自哪一列，面板整份遮，自查一列都不算（列名就是遮罩列的除外）
    ("SELECT name, phone AS p FROM customers WHERE ((", ["name", "p"]),
    # 派生来源里没起别名的表达式：认遮罩列的那一套整份算，自查同样一列都不算
    ("SELECT name, amount FROM (SELECT c.name, UPPER(c.phone), o.amount FROM customers c "
     "JOIN orders o ON o.customer_id = c.id) t", ["name", "amount"]),
])
async def test_columns_only_counted_because_the_sql_is_unclear_are_not_surely_masked(engine_up, mall, sql, columns):
    from app.api.evidence import _mask_sets, _Masks

    sets = await _mask_sets({"sql": sql, "columns": columns, "rows": [], "source": "mall"}, "mall", _Masks())
    assert sets.sure == set() and set(columns) <= sets.hidden, (sets.sure, sets.hidden)
    direct = await _mask_sets({"sql": "SELECT name, phone FROM customers WHERE ((", "columns": ["name", "phone"],
                               "rows": [], "source": "mall"}, "mall", _Masks())
    assert direct.sure == {"phone"}, direct.sure


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes) -> dict:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def finish(monkeypatch, spec: dict, text: str, payload: dict | None = None) -> Run:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))
    run_id = (await run_manager.start(graph=spec, input_payload=payload or {})).id
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


def one_query(sql: str, **report) -> dict:
    return chain(node("start", "input"), node("top", "tool", tool="db_query__mall", args={"sql": sql}),
                 node("write", "report", instructions="写一句", **report),
                 node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]))


@pytest.mark.parametrize("sql", COUNTING[:2])
async def test_a_formal_report_citing_a_count_of_missing_phones_succeeds(monkeypatch, mall, sql):
    """违规即失败（正式运行的默认值）：数据质量报告引用「手机号缺失的人数」照常出具，不判违规。"""
    row = await finish(monkeypatch, one_query(sql, on_violation="fail"), "手机号缺失的客户有 [[v:Q1.r0.missing]] 位。")
    assert row.status == "succeeded", row.error
    checked = next(e.data for e in await events(row.id) if e.type == "report.checked")
    assert masked_of(checked["violations"]) == [] and checked["ok"] is True, checked["violations"]


# --------------------------------------------------------------------------
# 正文里直接写着的遮罩值
# --------------------------------------------------------------------------


async def masked_catalog(sql: str, columns: list[str], rows: list[list]) -> dict:
    art = await artifact_store.put_json({"columns": columns, "rows": rows, "source": "mall", "sql": sql}, kind="query")
    return {"Q1": {"alias": "Q1", "kind": "query", "artifact": art, "columns": columns, "rows": len(rows),
                   "node_id": "top", "source": "mall"}}


SQL_TOP = "SELECT c.name AS customer, c.phone AS tel, 1 AS spend FROM customers c"
TOP_ROWS = [[NAMES[1], PHONES[1], 380.0], [NAMES[0], PHONES[0], 220.0]]


@pytest.mark.parametrize("text", [
    f"消费最高的客户电话是 {PHONES[1]}。",
    # 换了写法的号码也认
    "消费最高的客户电话是 199-0000-0702。",
    f"消费最高的客户电话是 +86 {PHONES[1][:3]} {PHONES[1][3:7]} {PHONES[1][7:]}。",
])
async def test_a_masked_value_copied_into_the_text_is_flagged(engine_up, mall, text):
    """写作者把号码照抄成正文（没写成引用）：记一条 masked_cell（按值认出来的），不带值；交回写作者的那句说清在哪一句、
    值不能以任何形式写进报告，句子里的值换成「已遮罩」。"""
    from app.engine.nodes.report import _MaskedCells

    catalog = await masked_catalog(SQL_TOP, ["customer", "tel", "spend"], TOP_ROWS)
    doc = await _MaskedCells(catalog)(compose_doc(text, catalog, node_id="write", run_id="r1"))
    [v] = masked_of(doc["violations"])
    assert v["by_value"] is True and v["segment"] and v["unit"] and "ref" not in v, v
    assert "设置了遮罩的列中的值" in v["message"] and "不能以任何形式写进报告" in v["for_model"]
    assert "已遮罩" in v["context"] and leaks(v) == [] and leaks(describe_violations([v])) == []


async def test_a_text_masked_column_copied_as_text_is_flagged(engine_up, mall):
    """文字类的遮罩列（姓名）：数字核对管不到它，按值认才拦得住。"""
    from app.engine.nodes.report import _MaskedCells

    await set_source(mall, {"mask_columns": ["name"]})
    catalog = await masked_catalog(SQL_TOP, ["customer", "tel", "spend"], TOP_ROWS)
    doc = await _MaskedCells(catalog)(compose_doc(f"消费最高的是{NAMES[1]}，花了 [[v:Q1.r0.spend]]。", catalog,
                                                  node_id="write", run_id="r1"))
    [v] = masked_of(doc["violations"])
    assert v["by_value"] is True and leaks(v) == [], v


async def test_a_cited_masked_cell_is_not_counted_twice(engine_up, mall):
    """[[v:Q1.r0.tel]] 已经按引用记过：同一处不再按值另记一条。"""
    from app.engine.nodes.report import _MaskedCells

    catalog = await masked_catalog(SQL_TOP, ["customer", "tel", "spend"], TOP_ROWS)
    doc = await _MaskedCells(catalog)(compose_doc("电话 [[v:Q1.r0.tel]]。", catalog, node_id="write", run_id="r1"))
    [v] = masked_of(doc["violations"])
    assert v["ref"] == "v:Q1.r0.tel" and not v.get("by_value"), v


async def test_values_only_conservatively_masked_or_given_as_run_input_are_not_flagged(engine_up, mall):
    """不算违规的：SQL 认不准、整份算进来的列（姓名没设遮罩，只是面板多遮）；运行输入里写着的号码（发起运行的人自己填的）；
    数据源没设遮罩。"""
    from app.engine.nodes.report import _MaskedCells

    unsure = ("SELECT name, amount FROM (SELECT c.name, UPPER(c.phone), o.amount FROM customers c "
              "JOIN orders o ON o.customer_id = c.id) t ORDER BY amount DESC")
    catalog = await masked_catalog(unsure, ["name", "amount"], [[NAMES[1], 300.0]])
    doc = await _MaskedCells(catalog)(compose_doc(f"金额最大的一笔来自{NAMES[1]}。", catalog, node_id="write", run_id="r1"))
    assert masked_of(doc["violations"]) == []

    catalog = await masked_catalog(SQL_TOP, ["customer", "tel", "spend"], TOP_ROWS)
    asked = _MaskedCells(catalog, protected=[json.dumps({"phone": PHONES[1]})])
    doc = await asked(compose_doc(f"号码 {PHONES[1]} 的客户消费最高。", catalog, node_id="write", run_id="r1"))
    assert masked_of(doc["violations"]) == []

    await set_source(mall, {})
    doc = await _MaskedCells(catalog)(compose_doc(f"电话 {PHONES[1]}。", catalog, node_id="write", run_id="r1"))
    assert masked_of(doc["violations"]) == []


async def test_snapshots_of_unmasked_sources_are_not_read_for_values(engine_up, mall, monkeypatch):
    """按值认要读快照：数据源现在没设遮罩的一份都不读，没引用到格子的也不为引用去读。"""
    from app.core import artifact_store as store
    from app.engine.nodes.report import _MaskedCells

    await set_source(mall, {})
    catalog = await masked_catalog(SQL_TOP, ["customer", "tel", "spend"], TOP_ROWS)
    read: list[str] = []
    real = store.load
    monkeypatch.setattr(store, "load", lambda a: read.append(a) or real(a))
    await _MaskedCells(catalog)(compose_doc("一共三位客户。", catalog, node_id="write", run_id="r1"))
    assert read == [], read


METRICS = [
    {"id": "phone_raw", "name": "首位客户手机号", "format": "plain", "expression": "cell(nodes.top, 0, 'tel')"},
    {"id": "id3", "name": "第三位客户编号", "format": "plain", "expression": "cell(nodes.top, 2, 'id')"},
]


def card_graph(**report) -> dict:
    return chain(node("start", "input"),
                 node("top", "tool", tool="db_query__mall",
                      args={"sql": "SELECT id, name AS customer, phone AS tel FROM customers ORDER BY id"}),
                 node("card", "metrics", caliber="遮罩对照", caliber_version="v1", metrics=METRICS),
                 node("write", "report", instructions="写一句", **report),
                 node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]))


async def test_a_metric_whose_value_is_a_masked_value_is_flagged(monkeypatch, mall):
    """[[m:phone_raw]] 的值就是号码：按值认出来，点名那处引用；取客户编号的 [[m:id3]] 照常。标注照常产出时，时间线那一行
    说清有几处是正文里写着的值，缺口、日志都不写值。"""
    row = await finish(monkeypatch, card_graph(on_violation="flag"), "第三位客户编号 [[m:id3]]，首位客户电话 [[m:phone_raw]]。")
    assert row.status == "succeeded", row.error
    rows = await events(row.id)
    checked = next(e.data for e in rows if e.type == "report.checked")
    [v] = masked_of(checked["violations"])
    assert v["ref"] == "m:phone_raw" and v["by_value"] is True and "[[m:phone_raw]]" in v["message"], v
    [log] = [e.data for e in rows if e.type == "log" and e.data.get("code") == "report_masked_cell"]
    assert log["count"] == 1 and log["written"] == 1 and log["refs"] == ["m:phone_raw"], log
    finished = next(e.data for e in rows if e.type == "node.finished" and e.node_id == "write")
    [gap] = artifact_store.load(finished["artifact"])["issuance_gaps"]
    assert "设置了遮罩的列中的值" in gap and leaks([checked["violations"], log, gap]) == [], (gap, log)


async def test_a_formal_report_copying_a_phone_fails_with_advice_about_the_instructions(monkeypatch, mall):
    row = await finish(monkeypatch, card_graph(on_violation="fail", numbers="off"),
                       f"首位客户电话 {PHONES[0]}，第三位客户编号 [[m:id3]]。")
    assert row.status == "failed" and "设置了遮罩的列中的值" in row.error, row.error
    assert "指令中去掉" in row.error and "指令遵循能力" not in row.error and leaks(row.error) == [], row.error
