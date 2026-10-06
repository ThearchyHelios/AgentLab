"""报告自查认得出引用了取自遮罩列的格子：像没有出处的数字一样记违规、交回写作者重写，仍不改的按「违规处置」办。

验收发现：写作要求点名引用 [[v:Q2.r0.tel]]（Q2 的 SQL 是 c.phone AS tel，phone 设了遮罩），报告自查判 ok、violations 为空，
正文写出号码，导出的可验证报告也把它带了出去，从发起到导出没有一步提示。写作目录不遮罩是已知设计（写作者要看得到数据），
所以拦在自查：

- 组装文档时（compose_doc 的 masked：{查询快照工件: 取自遮罩列的列}），引用了这些列的格子记一条 masked_cell，列名对上
  的、按 SQL 认出改了名字的都算（报告节点按证据面板同一个函数 api/evidence.py 的 _snapshot_masks 算）
- 违规里不带那一格的值（不写 text、context）：违规会进 report.checked、重写的日志、出具缺口，这些地方不能再露一次号码
- 报告节点把它交回写作者重写（不是只标注的那几类）；重写后还在：「违规处置」为失败的判失败，为标注的照常产出，
  时间线上另起一行说清（report_masked_cell），节点产出里写一条出具缺口，出口降档
- 没设遮罩、引用的是别的列：一切照旧

真跑的部分用虚构的 SQLite 商城库（号码是编的），写作者是 MockChatModel 照剧本写。
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
from app.engine import review as rv
from app.engine.evidence import compose_doc, describe_violations, iter_segments
from app.engine.runner import run_manager

# --------------------------------------------------------------------------
# 组装文档：compose_doc 的 masked
# --------------------------------------------------------------------------

ROWS = [["客户乙", "199-0000-0102", 380.0], ["客户甲", "199-0000-0101", 220.0]]
SECRET = [r[1] for r in ROWS]


async def catalog_of() -> tuple[dict, str]:
    art = await artifact_store.put_json({"columns": ["customer", "tel", "spend"], "rows": ROWS, "source": "mall",
                                         "sql": "SELECT c.name AS customer, c.phone AS tel, 1 AS spend FROM customers c",
                                         "mask_columns": ["phone"]}, kind="query")
    entry = {"alias": "Q1", "kind": "query", "artifact": art, "columns": ["customer", "tel", "spend"], "rows": 2,
             "node_id": "top", "source": "mall"}
    return {"Q1": entry}, art


def masked_of(doc: dict) -> list[dict]:
    return [v for v in doc["violations"] if v["code"] == "masked_cell"]


async def test_a_cited_masked_cell_is_a_violation_without_its_value():
    catalog, art = await catalog_of()
    doc = compose_doc("消费最高的是 [[v:Q1.r0.customer]]，电话 [[v:Q1.r0.tel]]，花了 [[v:Q1.r0.spend]]。", catalog,
                      node_id="write", run_id="r1", masked={art: {"phone", "tel"}})
    [v] = masked_of(doc)
    seg = next(s for s in iter_segments(doc) if s.get("ref") == "v:Q1.r0.tel")
    assert (v["ref"], v["column"], v["segment"]) == ("v:Q1.r0.tel", "tel", seg["id"]) and v["unit"] and v["span"]
    assert "遮罩" in v["message"] and "「tel」" in v["message"] and "删掉" in v["for_model"]
    # 违规本身不再写一次号码：它会进事件、日志和出具缺口
    assert not [p for p in SECRET if p in json.dumps(v, ensure_ascii=False)]
    assert "text" not in v and "context" not in v
    assert not [p for p in SECRET if p in describe_violations(masked_of(doc))]


async def test_without_masks_or_for_other_columns_nothing_is_flagged():
    catalog, art = await catalog_of()
    text = "电话 [[v:Q1.r0.tel]]，花了 [[v:Q1.r0.spend]]。"
    assert masked_of(compose_doc(text, catalog, node_id="write", run_id="r1")) == []
    assert masked_of(compose_doc(text, catalog, node_id="write", run_id="r1", masked={})) == []
    assert masked_of(compose_doc("花了 [[v:Q1.r0.spend]]。", catalog, node_id="write", run_id="r1",
                                 masked={art: {"phone", "tel"}})) == []


async def test_a_whole_table_reports_every_masked_cell():
    """整表引用展开成每格一个 [[v:]]：遮罩列的每一格各记一条，写作者看得到要去掉哪一列。"""
    catalog, art = await catalog_of()
    doc = compose_doc("前两名：\n\n[[table:Q1]]\n", catalog, node_id="write", run_id="r1", masked={art: {"tel"}})
    assert [v["ref"] for v in masked_of(doc)] == ["v:Q1.r0.tel", "v:Q1.r1.tel"]
    assert all("cols=" in v["for_model"] for v in masked_of(doc))


def test_the_masked_cell_code_is_a_degraded_signal_of_its_own_kind():
    """复核层认得这条日志：报告照常产出、有缺口，按它自己的 kind 记，不落成笼统的 warn。"""
    assert rv.WARN_CODES["report_masked_cell"] == ("report_masked_cell", rv.DEGRADED)
    warn = {"type": "log", "node_id": "write",
            "data": {"level": "warn", "code": "report_masked_cell", "message": "报告引用了取自遮罩列的格子"}}
    [signal] = rv.scan([warn], {"报告": "首位客户已联系"})
    assert (signal.kind, signal.severity) == ("report_masked_cell", rv.DEGRADED)


# --------------------------------------------------------------------------
# 真跑：报告节点交回重写、按违规处置办、时间线和出具缺口说清
# --------------------------------------------------------------------------

CUSTOMERS = [(1, "客户甲", "199-0000-0101"), (2, "客户乙", "199-0000-0102"), (3, "客户丙", "199-0000-0103")]
ORDERS = [(1, 1, 120.5), (2, 2, 300.0), (3, 2, 80.0), (4, 3, 210.25), (5, 1, 99.5)]
PHONES = [c[2] for c in CUSTOMERS]
SQL_TOP = ("SELECT c.name AS customer, c.phone AS tel, ROUND(SUM(o.amount), 2) AS spend "
           "FROM orders o JOIN customers c ON c.id = o.customer_id GROUP BY c.id, c.name, c.phone ORDER BY spend DESC")
CITES_TEL = "消费最高的客户是 [[v:Q1.r0.customer]]，联系电话 [[v:Q1.r0.tel]]，共消费 [[v:Q1.r0.spend]]。"
CLEAN = "消费最高的客户是 [[v:Q1.r0.customer]]，共消费 [[v:Q1.r0.spend]]。"


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


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


def graph(*, sql: str = SQL_TOP, contract: dict | None = None, **report) -> dict:
    out = {"fields": [{"name": "报告", "value": "{{ nodes.write.text }}"}]}
    if contract is not None:
        out["contract"] = contract
    nodes = [
        node("start", "input"),
        node("top", "tool", tool="db_query__mall", args={"sql": sql}),
        node("write", "report", instructions="写一句，联系电话写 [[v:Q1.r0.tel]]", **report),
        node("out", "output", **out),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def script(monkeypatch, *replies) -> list[list]:
    """写作者依次回这几段，用完了一直回最后一段。返回每次调用收到的消息。"""
    from app.providers import mock_model

    seen: list[list] = []

    def _decide(self, messages):
        seen.append(list(messages))
        return AIMessage(content=replies[min(len(seen), len(replies)) - 1])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return seen


async def finish(spec: dict) -> Run:
    run_id = (await run_manager.start(graph=spec, input_payload={})).id
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")


async def events(run_id: str, etype: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


def logs(rows: list[RunEvent], code: str) -> list[dict]:
    return [e.data for e in rows if e.type == "log" and e.data.get("code") == code]


def leaks(value) -> list[str]:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [p for p in PHONES if p in raw]


async def test_the_writer_is_asked_to_rewrite_and_the_rewrite_passes(monkeypatch, mall):
    """第一稿引用了 tel（phone 改的名字）：自查记违规、交回重写，重写稿不引用就通过。重写请求和日志里不再写号码。"""
    seen = script(monkeypatch, CITES_TEL, CLEAN)
    row = await finish(graph())
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked")
    assert checked.data["ok"] is True and checked.data["repairs"] == 1, checked.data
    repair = str(seen[1][-1].content)
    assert "tel" in repair and "遮罩" in repair and leaks(repair) == []
    [log] = logs(await events(row.id), "report_repair")
    assert "v:Q1.r0.tel" in log["message"] and leaks(log) == []


async def test_a_writer_that_insists_is_flagged_and_the_issuance_says_why(monkeypatch, mall):
    """重写后还引用：「违规处置」为标注时照常产出，自查结果、时间线、出具缺口都说清，这几处都不再写一次号码。"""
    script(monkeypatch, CITES_TEL)
    row = await finish(graph(on_violation="flag", contract={"report_from": "write"}))
    assert row.status == "succeeded", row.error
    rows = await events(row.id)
    [checked] = [e.data for e in rows if e.type == "report.checked"]
    assert checked["ok"] is False and checked["repairs"] == 1 and not checked["failed"]
    [v] = [v for v in checked["violations"] if v["code"] == "masked_cell"]
    assert v["ref"] == "v:Q1.r0.tel" and v["column"] == "tel" and leaks(v) == []
    [log] = logs(rows, "report_masked_cell")
    assert log["count"] == 1 and log["refs"] == ["v:Q1.r0.tel"] and log["columns"] == ["tel"] and log["level"] == "warn"
    assert "重写 1 次" in log["message"] and leaks(log) == []
    finished = next(e.data for e in rows if e.type == "node.finished" and e.node_id == "write")
    payload = artifact_store.load(finished["artifact"])
    [gap] = payload["issuance_gaps"]
    assert "遮罩" in gap and "v:Q1.r0.tel" in gap and leaks(gap) == []
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded" and any(g.startswith("报告撰写「write」：") and "遮罩" in g
                                                   for g in issuance["gaps"]), issuance["gaps"]
    assert leaks(issuance["gaps"]) == []


async def test_a_writer_that_insists_fails_the_node_when_violations_fail(monkeypatch, mall):
    """「违规处置」为失败：重写后还引用就判失败，原因说是遮罩列，不写号码。"""
    script(monkeypatch, CITES_TEL)
    row = await finish(graph(on_violation="fail"))
    assert row.status == "failed" and "遮罩" in (row.error or "") and "tel" in row.error, row.error
    assert leaks(row.error) == []
    [checked] = await events(row.id, "report.checked")
    assert checked.data["failed"] is True
    assert logs(await events(row.id), "report_masked_cell") == [], "节点判失败时不另起「照常产出」的那一行"


async def test_the_masked_column_itself_is_flagged_too(monkeypatch, mall):
    """列名直接就是遮罩列（没起别名）的也算。"""
    script(monkeypatch, "首位客户 [[v:Q1.r0.name]]，电话 [[v:Q1.r0.phone]]。")
    row = await finish(graph(sql="SELECT name, phone FROM customers ORDER BY id", on_violation="flag"))
    [checked] = await events(row.id, "report.checked")
    assert [v["ref"] for v in checked.data["violations"] if v["code"] == "masked_cell"] == ["v:Q1.r0.phone"]


async def test_without_masks_the_report_is_unchanged(monkeypatch, mall):
    await set_source(mall, {})
    script(monkeypatch, CITES_TEL)
    row = await finish(graph(on_violation="flag", contract={"report_from": "write"}))
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked")
    assert checked.data["ok"] is True and checked.data["repairs"] == 0
    assert logs(await events(row.id), "report_masked_cell") == []
    finished = next(e.data for e in await events(row.id, "node.finished") if e.node_id == "write")
    assert "issuance_gaps" not in artifact_store.load(finished["artifact"])
    assert row.output["_issuance"]["tier"] == "formal" or not any("遮罩" in g for g in row.output["_issuance"]["gaps"])
