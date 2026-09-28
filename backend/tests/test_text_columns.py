"""文本列按文本渲染：查询快照带着 column_types（查询时从驱动的原始值记下的）。

以前快照里没有列类型，VARCHAR 的 "2026" 和小数位为 0 的 DECIMAL "45678" 长得一模一样，只能
「像数字就当数字」：报告里 [[v:Q1.r0.week]] 渲染成「2,026」。现在 text 列一律按文本：
render_cell、[[v:]] 的渲染和取值、写作目录、cite_fields 没声明类型的字段。拿不到 column_types
的老快照保持原来的启发式。

口径卡表达式里的 cell() 照旧按值猜：那是在算数，数字写成文本的列（CAST 成文本、VARCHAR 存金额）
多半就是要拿来算的；要文本写 str(cell(...))。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.evidence import (
    RenderError,
    build_catalog,
    catalog_prompt,
    compose_doc,
    iter_segments,
    render_cell,
    verify_doc,
)
from app.engine.expressions import cell_value, column_kind, eval_expression
from app.engine.runner import run_manager

BASE = {"columns": ["week", "gmv", "code"], "rows": [["2026", "45678", "10001"], ["2027", "12.50", "10002"]],
        "row_count": 2, "truncated": False, "elapsed_ms": 1, "sql": "SELECT week, gmv, code FROM orders",
        "source": "shop"}
TYPED = {**BASE, "column_types": {"week": "text", "gmv": "number", "code": "text"}}


def _store(snap: dict) -> str:
    text = artifact_store.canonical_json(snap)
    artifact = artifact_store.content_hash(text)
    path = artifact_store._path_of(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return artifact


def catalog_of(snap: dict) -> dict:
    artifact = _store(snap)
    return build_catalog(nodes={}, ledger=[{
        "kind": "query", "node_id": "fetch", "exec": 1, "artifact": artifact, "via": "b" * 64, "tool": "db_query__shop",
        "source": "shop", "columns": snap["columns"], "rows": len(snap["rows"]), "truncated": False}], inputs={})


def test_cell_value_keeps_text_columns_as_text():
    assert cell_value("2026", "text") == "2026"
    assert cell_value("45678", "number") == 45678
    assert cell_value("2026") == 2026                       # 老快照：照旧按值猜
    assert cell_value("2026", None) == 2026
    assert column_kind(TYPED, "week") == "text" and column_kind(TYPED, "gmv") == "number"
    assert column_kind(BASE, "week") is None and column_kind(TYPED, "nope") is None
    assert column_kind({"column_types": "bad"}, "week") is None


def test_render_cell_reads_the_column_type():
    assert render_cell("2026", kind="text") == "2026"
    assert render_cell("10001", kind="text") == "10001"
    assert render_cell("45678", kind="number") == "45,678"
    assert render_cell("2026") == "2,026"
    with pytest.raises(RenderError):
        render_cell("2026", "万", kind="text")


def test_cell_refs_render_text_columns_as_text():
    doc = compose_doc("[[v:Q1.r0.week]] 周销售额 [[v:Q1.r0.gmv]]，编号 [[v:Q1.r1.code]]。", catalog_of(TYPED))
    assert doc["markdown"] == "2026 周销售额 45,678，编号 10002。"
    week, gmv, code = [s for s in iter_segments(doc) if s.get("ref")]
    assert week["kind"] == "value" and week["cite"]["value"] == "2026"
    assert gmv["kind"] == "number" and gmv["cite"]["value"] == 45678
    assert code["kind"] == "value" and code["cite"]["value"] == "10002"
    assert doc["violations"] == []
    assert verify_doc(doc, catalog_of(TYPED))["violations"] == []


def test_old_snapshots_keep_the_heuristic():
    doc = compose_doc("[[v:Q1.r0.week]] 周", catalog_of(BASE))
    assert doc["markdown"] == "2,026 周"


def test_a_text_column_cannot_be_converted():
    doc = compose_doc("[[v:Q1.r0.week|万]]", catalog_of(TYPED))
    [bad] = [v for v in doc["violations"] if v["code"] == "unresolved_ref"]
    assert "不是数" in bad["message"]


def test_tables_render_text_columns_as_text():
    doc = compose_doc("[[table:Q1 cols=week,gmv]]", catalog_of(TYPED))
    assert "| 2026 | 45,678 |" in doc["markdown"] and "| 2027 | 12.5 |" in doc["markdown"]


def test_the_writing_catalog_shows_what_the_report_will_show():
    prompt = catalog_prompt(catalog_of(TYPED))
    assert "week=2026，gmv=45,678，code=10001" in prompt
    # 示例挑第一个数：文本列不算
    assert "例：[[v:Q1.r0.gmv]]" in prompt
    assert "例：[[v:Q1.r0.week]]" in catalog_prompt(catalog_of(BASE))


def test_cite_fields_without_a_declared_type_follow_the_column():
    from app.engine.nodes.llm import cited_fields, verify_cited_fields

    queries = [{"alias": "Q1", "artifact": "a" * 64, "via": "b" * 64}]
    parsed = {"week": {"value": "2026", "from": {"call": "Q1", "row": 0, "column": "week"}},
              "gmv": {"value": 45678, "from": {"call": "Q1", "row": 0, "column": "gmv"}},
              "weeks": {"value": ["2026", "2027"], "from": {"call": "Q1", "rows": "0-1", "column": "week"}}}
    schema = {"type": "object", "properties": {"week": {}, "gmv": {}, "weeks": {"type": "array", "items": {}}}}
    data, ev = verify_cited_fields({"parsed": parsed}, cited_fields(schema), queries, loader=lambda a: TYPED)
    assert data == {"week": "2026", "gmv": 45678, "weeks": ["2026", "2027"]}
    assert {e["status"] for e in ev.values()} == {"verified"}
    # 声明了类型的照声明：写 schema 的人说了它是数
    declared = {"type": "object", "properties": {"week": {"type": "integer"}, "gmv": {}, "weeks": {
        "type": "array", "items": {"type": "number"}}}}
    data, _ = verify_cited_fields({"parsed": parsed}, cited_fields(declared), queries, loader=lambda a: TYPED)
    assert data == {"week": 2026, "gmv": 45678, "weeks": [2026, 2027]}
    # 老快照照旧按值猜
    data, _ = verify_cited_fields({"parsed": parsed}, cited_fields(schema), queries, loader=lambda a: BASE)
    assert data["week"] == 2026


def test_card_expressions_keep_computing_on_number_like_text():
    assert eval_expression("cell(vars.q, 0, 'week') + 1", {"vars": {"q": TYPED}}) == 2027
    assert eval_expression("str(cell(vars.q, 0, 'week'))", {"vars": {"q": TYPED}}) == "2026"


# --------------------------------------------------------------------------
# 端到端：列类型从驱动一路带到报告
# --------------------------------------------------------------------------


@pytest.fixture
async def shop(tmp_path):
    await run_manager.setup()
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, week TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "2026", 100.5), (2, "2026", 200.0)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        else:
            row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield
    await engines.invalidate(source_id)
    await run_manager.shutdown()


async def test_a_report_quotes_a_text_year_as_written(shop, monkeypatch):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide",
                        lambda model, messages: AIMessage(content="[[v:Q1.r0.week]] 年销售额 [[v:Q1.r0.gmv]]。"))

    def node(nid, ntype, **config):
        return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}

    nodes = [node("start", "input"),
             node("fetch", "tool", tool="db_query__shop",
                  args={"sql": "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week"}),
             node("write", "report", instructions="写一句话", on_violation="flag"),
             node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}])]
    graph = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed"):
            break
    assert row.status == "succeeded", row.error
    assert row.output["答案"] == "2026 年销售额 300.5。"
