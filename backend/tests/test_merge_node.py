"""合并查询节点（merge）：画图时的校验、执行、证据链和下钻、助手说明。

端到端用两个独立的 SQLite 夹具（tests/fixtures/merge/）：门店库的销售订单、会员库的到店记录。两条查询各自在自己的
库里聚合到「日期 + 门店」，合并查询在库外按键合并，口径卡用 cell(nodes.merge, …) 算转化率和客单价，报告引用合并
结果的单元格和口径卡的指标。断言报告里的数能追到两边的原始查询，下钻不指错格子。

不调用任何模型：报告的写作者换成脚本（monkeypatch MockChatModel._decide）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.core.config import settings
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import iter_segments
from app.engine.merge_query import MERGE_SOURCE
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, NodeType, validate_graph
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures" / "merge"

SALES_SQL = ("SELECT order_date AS 日期, store_id AS 门店, COUNT(*) AS 订单数, SUM(amount) AS 销售额 FROM orders "
             "GROUP BY order_date, store_id ORDER BY order_date, store_id")
# 故意和门店库排成不同的顺序：合并结果的第 0 行在会员库的结果里是第 2 行
VISITS_SQL = ("SELECT visit_date AS 日期, store_code AS 门店, COUNT(*) AS 到店人数 FROM visits "
              "GROUP BY visit_date, store_code ORDER BY visit_date DESC, store_code")
MERGE_SQL = ("SELECT s.日期, s.门店, s.订单数, s.销售额, v.到店人数 FROM s JOIN v ON s.日期 = v.日期 AND s.门店 = v.门店 "
             "ORDER BY s.日期, s.门店")
TEXT = ("5 月 1 日湖滨店到店 [[v:Q3.r0.到店人数]] 人，转化率 [[m:conversion]]，客单价 [[m:aov]]。\n\n"
        "[[table:Q3 cols=日期,门店,订单数,到店人数 rows=0-3]]")


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _source(name: str, path: Path, options: dict | None = None) -> str:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, options or {}
        await session.commit()
        return row.id


@pytest.fixture
async def sources(tmp_path, engine_up):
    """把两个夹具脚本建成两个独立的 SQLite 库，各接成一个只读数据源。"""
    ids = []
    for name in ("stores", "members"):
        path = tmp_path / f"{name}.db"
        db = sqlite3.connect(path)
        db.executescript((FIXTURES / f"{name}.sql").read_text("utf-8"))
        db.commit()
        db.close()
        ids.append(await _source(f"merge_{name}", path))
    for sid in ids:
        await engines.invalidate(sid)
    yield
    for sid in ids:
        await engines.invalidate(sid)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def edge(a, b):
    return {"source": a, "target": b}


def store_graph(*, sales_sql=SALES_SQL, visits_sql=VISITS_SQL, merge_sql=MERGE_SQL, visits_args=None, report=True,
                inputs=None):
    """入口分两路查两个库，汇合到合并查询，再接口径卡、报告、成果。"""
    nodes = [
        node("start", "input"),
        node("q_sales", "tool", tool="db_query__merge_stores", args={"sql": sales_sql}),
        node("q_visits", "tool", tool="db_query__merge_members", args={"sql": visits_sql, **(visits_args or {})}),
        node("merge", "merge", inputs=inputs or {"s": "q_sales", "v": "q_visits"}, sql=merge_sql),
        node("card", "metrics", caliber="门店经营", caliber_version="v1", metrics=[
            {"id": "conversion", "name": "转化率", "decimals": 4, "format": "percent_of_ratio",
             "expression": "cell(nodes.merge, 0, '订单数') / cell(nodes.merge, 0, '到店人数')"},
            {"id": "aov", "name": "客单价", "unit": "元", "decimals": 1,
             "expression": "cell(nodes.merge, 0, '销售额') / cell(nodes.merge, 0, '订单数')"}]),
    ]
    edges = [edge("start", "q_sales"), edge("start", "q_visits"), edge("q_sales", "merge"), edge("q_visits", "merge"),
             edge("merge", "card")]
    if report:
        nodes += [node("write", "report", instructions="写门店日报", on_violation="flag"),
                  node("out", "output", fields=[{"name": "日报", "value": "{{ nodes.write.text }}"}])]
        edges += [edge("card", "write"), edge("write", "out")]
    else:
        nodes.append(node("out", "output", fields=[{"name": "转化率", "value": "{{ nodes.card.metrics }}"}]))
        edges.append(edge("card", "out"))
    return {"nodes": nodes, "edges": edges}


def script(monkeypatch, text=TEXT):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没有结束：{row.status}")


async def run_graph(monkeypatch, graph=None, text=TEXT) -> Run:
    script(monkeypatch, text)
    return await wait((await run_manager.start(graph=graph or store_graph(), input_payload={})).id)


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[dict]:
    async with SessionLocal() as session:
        query = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            query = query.where(RunEvent.node_id == node_id)
        return [e.data for e in (await session.execute(query.order_by(RunEvent.seq))).scalars()]


def load_doc(row: Run) -> dict:
    doc_id = row.output["_evidence"]["doc_artifact"]
    return json.loads((settings.data_dir / "artifacts" / doc_id[:2] / f"{doc_id}.json").read_text("utf-8"))


def seg_of(doc: dict, text: str, n: int = 0) -> dict:
    return [s for s in iter_segments(doc) if s["text"] == text and s.get("cite")][n]


def alias_of(doc: dict, node_id: str) -> str:
    [alias] = [a for a, e in doc["catalog"].items() if e.get("kind") == "query" and e.get("node_id") == node_id]
    return alias


# --------------------------------------------------------------------------
# 画图时的校验
# --------------------------------------------------------------------------


def merge_spec(merge_cfg: dict, *, extra_nodes=(), extra_edges=()) -> GraphSpec:
    base = store_graph(report=False)
    nodes = [n if n["id"] != "merge" else node("merge", "merge", **merge_cfg) for n in base["nodes"]]
    return GraphSpec.model_validate({"nodes": [*nodes, *extra_nodes], "edges": [*base["edges"], *extra_edges]})


def merge_errors(spec: GraphSpec) -> list:
    return [i for i in validate_graph(spec).issues if i.node_id == "merge" and i.level == "error"]


def test_node_type_exists():
    assert NodeType.MERGE == "merge"


def test_a_well_formed_merge_validates():
    spec = merge_spec({"inputs": {"s": "q_sales", "v": "q_visits"}, "sql": MERGE_SQL})
    assert merge_errors(spec) == []


def test_merge_of_a_merge_is_allowed():
    spec = merge_spec({"inputs": {"s": "q_sales", "v": "q_visits"}, "sql": MERGE_SQL},
                      extra_nodes=[node("again", "merge", inputs={"m": "merge"}, sql="SELECT * FROM m")],
                      extra_edges=[edge("merge", "again")])
    assert [i for i in validate_graph(spec).issues if i.node_id == "again" and i.level == "error"] == []


def test_inputs_are_required():
    [issue] = merge_errors(merge_spec({"inputs": {}, "sql": MERGE_SQL}))
    assert issue.field == "inputs" and issue.code == "merge.inputs_missing"


def test_input_pointing_to_a_missing_node():
    [issue] = merge_errors(merge_spec({"inputs": {"s": "q_sales", "v": "nope"}, "sql": MERGE_SQL}))
    assert issue.field == "inputs.v" and "不存在" in issue.message and issue.code == "merge.input_invalid"


@pytest.mark.parametrize("extra, why", [
    (node("think", "llm", prompt="写点什么"), "不产出查询结果"),
    (node("calc", "tool", tool="calculator", args={"expression": "1+1"}), "不是数据库查询工具"),
    (node("asker", "agent", prompt="查一下", tools=["db_query__merge_stores"]), "Agent"),
])
def test_input_must_produce_a_query_result(extra, why):
    spec = merge_spec({"inputs": {"s": "q_sales", "v": extra["id"]}, "sql": MERGE_SQL},
                      extra_nodes=[extra], extra_edges=[edge("start", extra["id"]), edge(extra["id"], "merge")])
    [issue] = merge_errors(spec)
    assert issue.field == "inputs.v" and why in issue.message and issue.code == "merge.input_invalid"


def test_agent_inputs_explain_how_to_fix():
    extra = node("asker", "agent", prompt="查一下", tools=["db_query__merge_stores"])
    spec = merge_spec({"inputs": {"s": "q_sales", "v": "asker"}, "sql": MERGE_SQL},
                      extra_nodes=[extra], extra_edges=[edge("start", "asker"), edge("asker", "merge")])
    [issue] = merge_errors(spec)
    assert "查询多次" in issue.message and "调用工具" in issue.message


def test_input_must_be_upstream():
    lonely = node("q_other", "tool", tool="db_query__merge_stores", args={"sql": "SELECT 1 AS x"})
    spec = merge_spec({"inputs": {"s": "q_sales", "v": "q_other"}, "sql": MERGE_SQL},
                      extra_nodes=[lonely], extra_edges=[edge("start", "q_other")])
    [issue] = merge_errors(spec)
    assert issue.field == "inputs.v" and "上游" in issue.message


@pytest.mark.parametrize("alias", ["1s", "a-b", "select", "sqlite_x"])
def test_alias_must_be_a_legal_table_name(alias):
    [issue] = merge_errors(merge_spec({"inputs": {"s": "q_sales", alias: "q_visits"}, "sql": MERGE_SQL}))
    assert issue.field == f"inputs.{alias}" and issue.code == "merge.alias_invalid" and alias in issue.message


@pytest.mark.parametrize("sql, why", [
    ("", "还没有填写"),
    ("DELETE FROM s", "SELECT 或 WITH"),
    ("PRAGMA table_info(s)", "SELECT 或 WITH"),
    ("SELECT * FROM s; SELECT * FROM v", "一条"),
])
def test_sql_must_be_one_select_or_with(sql, why):
    [issue] = merge_errors(merge_spec({"inputs": {"s": "q_sales", "v": "q_visits"}, "sql": sql}))
    assert issue.field == "sql" and why in issue.message and issue.code == "merge.sql_invalid"


def test_merge_inputs_count_as_references_to_the_upstream_nodes():
    from app.engine.variables import analyze

    report = analyze(merge_spec({"inputs": {"s": "q_sales", "v": "q_visits"}, "sql": MERGE_SQL}))
    refs = {v.path: v.refs for v in report.variables}
    assert any(r.node_id == "merge" for r in refs["nodes.q_sales"])


def test_publish_checks_treat_the_merge_as_traceable_evidence():
    """受管级别的发布检查：合并结果喂给口径卡、报告，和数据源查询一样算可追溯的证据，不在合并节点上报任何问题。"""
    from app.engine.governance import publish_issues

    graph = store_graph()
    for n in graph["nodes"]:
        if n["id"] == "write":
            n["data"]["config"].update(numbers="strict", on_violation="fail", claims="require_citation")
        if n["id"] == "out":
            n["data"]["config"]["contract"] = {"report_from": "write", "metrics_from": ["card"],
                                               "required": ["conversion"], "strict": True}
    issues = publish_issues(GraphSpec.model_validate(graph), level="governed")
    assert [i for i in issues if i.node_id == "merge"] == []
    assert [i for i in issues if i.level == "error"] == [], [i.message for i in issues if i.level == "error"]


def test_labels_and_governance_know_the_type():
    from app.engine.governance import _traceable
    from app.engine.labels import field_label, type_label
    from app.engine.upgrade import _queries

    spec = merge_spec({"inputs": {"s": "q_sales", "v": "q_visits"}, "sql": MERGE_SQL})
    merge = spec.node_map()["merge"]
    assert type_label("merge") == "合并查询"
    assert field_label("sql", "merge") == "合并 SQL" and field_label("inputs", "merge") == "输入"
    assert _traceable(merge) and _queries(merge)


# --------------------------------------------------------------------------
# 执行与证据链
# --------------------------------------------------------------------------


async def test_end_to_end_two_databases(monkeypatch, sources, client):
    row = await run_graph(monkeypatch)
    assert row.status == "succeeded", row.error

    [finished] = await events(row.id, "node.finished", "merge")
    output = artifact_store.load(finished["artifact"])
    assert output["columns"] == ["日期", "门店", "订单数", "销售额", "到店人数"]
    assert output["rows"] == [["2026-05-01", "S01", 3, 300, 6], ["2026-05-01", "S02", 2, 150, 4],
                              ["2026-05-02", "S01", 2, 250, 5], ["2026-05-02", "S02", 1, 75, 4]]
    assert output["source"] == MERGE_SOURCE and output["warnings"] == []

    # 台账：一条和数据源查询同形的 query 条目，报告据此编号
    [entry] = finished["evidence"]
    assert entry["kind"] == "query" and entry["source"] == MERGE_SOURCE and entry["artifact"] == output["artifact"]
    assert entry["rows"] == 4 and entry["truncated"] is False
    snapshot = artifact_store.load(entry["artifact"])
    assert [i["alias"] for i in snapshot["inputs"]] == ["s", "v"]
    assert snapshot["sources"] == ["merge_stores", "merge_members"]
    assert snapshot["sql"] == MERGE_SQL

    # 运行事件：合并的输入、SQL、行数，运行详情据此展示
    [done] = await events(row.id, "merge.end", "merge")
    assert done["sql"] == MERGE_SQL and done["rows"] == 4 and done["warnings"] == []
    assert [(i["alias"], i["node_id"], i["rows"], i["source"]) for i in done["inputs"]] == [
        ("s", "q_sales", 4, "merge_stores"), ("v", "q_visits", 4, "merge_members")]
    assert done["query_artifact"] == entry["artifact"]

    doc = load_doc(row)
    q3 = doc["catalog"]["Q3"]
    assert q3["node_id"] == "merge" and q3["source"] == MERGE_SOURCE and q3["artifact"] == entry["artifact"]
    assert {alias_of(doc, "q_sales"), alias_of(doc, "q_visits")} == {"Q1", "Q2"}
    metric = doc["catalog"]["m:conversion"]
    assert metric["value"] == 0.5 and metric["rendered"] == "50.00%"
    # 口径卡的两个输入都按 cell() 落在合并结果的那一格上，并按快照核对过
    card = artifact_store.load(metric["artifact"])
    conversion = next(m for m in card["metrics"] if m["id"] == "conversion")
    assert [(i["via"], i["status"], i["artifact"], i["locator"]) for i in conversion["inputs"]] == [
        ("tool_cell", "verified", entry["artifact"], {"row": 0, "column": "订单数"}),
        ("tool_cell", "verified", entry["artifact"], {"row": 0, "column": "到店人数"})]
    text = row.output["日报"]
    assert "到店 6 人" in text and "50.00%" in text and "100.0元" in text


async def test_cell_segment_traces_to_both_original_queries(monkeypatch, sources, client):
    row = await run_graph(monkeypatch)
    assert row.status == "succeeded", row.error
    doc = load_doc(row)
    qv = alias_of(doc, "q_visits")
    qs = alias_of(doc, "q_sales")
    seg = seg_of(doc, "6")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    merge_step, visits_step = body["chain"]

    assert merge_step["step"] == "query" and merge_step["alias"] == "Q3" and merge_step["source"] == MERGE_SOURCE
    assert merge_step["hash_ok"] is True and merge_step["sealed"] is True
    assert "mask_note" not in merge_step       # 合并查询不是数据源，不能说它「已不存在」
    merged = merge_step["merge"]
    assert merged["sql"] == MERGE_SQL and merged["warnings"] == []
    assert [(i["alias"], i["node_id"], i["query"], i["rows"], i["source"], i["sealed"]) for i in merged["inputs"]] == [
        ("s", "q_sales", qs, 4, "merge_stores", True), ("v", "q_visits", qv, 4, "merge_members", True)]
    assert merged["traced"] == [{"cell": [0, "到店人数"], "input": "v", "query": qv, "row": 2, "column": "到店人数"}]

    # 接着给出会员库那次查询，高亮的正是那一格，值相同
    assert visits_step["step"] == "query" and visits_step["alias"] == qv and visits_step["merged_into"] == "Q3"
    assert visits_step["highlight"]["cells"] == [[2, "到店人数"]]
    at = visits_step["row_index"].index(2)
    assert visits_step["rows"][at][visits_step["columns"].index("到店人数")] == 6


async def test_metric_segment_traces_through_the_merge(monkeypatch, sources, client):
    row = await run_graph(monkeypatch)
    doc = load_doc(row)
    qs, qv = alias_of(doc, "q_sales"), alias_of(doc, "q_visits")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg_of(doc, '50.00%')['id']}")).json()
    metric, *inputs = body["chain"][:3]
    assert metric["step"] == "metric" and metric["value"] == 0.5
    assert [(i["via"], i["status"], i["query"], i["cell"]) for i in inputs] == [
        ("tool_cell", "verified", "Q3", "Q3.r0.订单数"), ("tool_cell", "verified", "Q3", "Q3.r0.到店人数")]
    steps = [s for s in body["chain"] if s["step"] == "query"]
    assert [s["alias"] for s in steps] == ["Q3", qs, qv]
    merge_step, sales_step, visits_step = steps
    assert sorted(t["input"] for t in merge_step["merge"]["traced"]) == ["s", "v"]
    assert sales_step["highlight"]["cells"] == [[0, "订单数"]] and sales_step["merged_into"] == "Q3"
    assert visits_step["highlight"]["cells"] == [[2, "到店人数"]]


async def test_provenance_of_a_merged_cell_follows_the_trace(monkeypatch, sources, client):
    """手工源上的格：追到会员库那次查询，结论和直接引用那一格相同（not_upload），并记下经过的合并。"""
    row = await run_graph(monkeypatch)
    doc = load_doc(row)
    seg = seg_of(doc, "6")
    out = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}/provenance")).json()
    assert (out["status"], out["reason"]["code"]) == ("none", "not_upload")
    assert out["cell"] == {"alias": "Q3", "row": 0, "column": "到店人数", "artifact": doc["catalog"]["Q3"]["artifact"]}
    assert out["merge"] == [{"alias": "Q3", "node_id": "merge", "input": "v", "query": alias_of(doc, "q_visits"),
                             "row": 2, "column": "到店人数"}]


async def test_a_computed_merge_column_has_no_cell_lineage(monkeypatch, sources, client):
    sql = ("SELECT s.日期, s.门店, s.订单数 + v.到店人数 AS 合计 FROM s JOIN v ON s.日期 = v.日期 AND s.门店 = v.门店 "
           "ORDER BY s.日期, s.门店")
    graph = store_graph(merge_sql=sql)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "card"]
    graph["edges"] = [e for e in graph["edges"] if "card" not in (e["source"], e["target"])] + [edge("merge", "write")]
    row = await run_graph(monkeypatch, graph, text="合计 [[v:Q3.r0.合计]]。")
    assert row.status == "succeeded", row.error
    doc = load_doc(row)
    seg = seg_of(doc, "9")
    body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}")).json()
    [merge_step] = body["chain"]
    [traced] = merge_step["merge"]["traced"]
    assert traced["input"] is None and traced["query"] is None and "逐格来历" in traced["note"]
    out = (await client.get(f"/api/runs/{row.id}/evidence/segments/{seg['id']}/provenance")).json()
    assert (out["status"], out["reason"]["code"], out["version"]) == ("none", "merge_no_lineage", None)
    assert out["merge"] == [{"alias": "Q3", "node_id": "merge", "input": None, "query": None, "row": None,
                             "column": None}]


async def test_evidence_graph_links_the_merge_to_its_inputs(monkeypatch, sources, client):
    row = await run_graph(monkeypatch)
    doc = load_doc(row)
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    merged = sorted((e["to"], e["input"]) for e in body["edges"] if e["rel"] == "merged_from" and e["from"] == "Q3")
    assert merged == sorted([(alias_of(doc, "q_sales"), "s"), (alias_of(doc, "q_visits"), "v")])


def test_judge_excerpt_names_the_inputs_and_masks_their_sources():
    """裁判看合并结果时，知道合并了哪几个输入；输入数据源现在设的遮罩照样生效。"""
    from app.engine import judge
    from app.engine.merge_query import MergeInput, execute

    sales = {"columns": ["门店", "订单数"], "rows": [["S01", 3]], "truncated": False, "source": "merge_stores"}
    visits = {"columns": ["门店", "手机号"], "rows": [["S01", "13800000000"]], "truncated": False,
              "source": "merge_members"}
    snap = execute([MergeInput("s", "q_sales", "a" * 64, sales), MergeInput("v", "q_visits", "b" * 64, visits)],
                   "SELECT s.门店, s.订单数, v.手机号 FROM s JOIN v ON s.门店 = v.门店").snapshot()
    entry = {"kind": "query", "artifact": "m" * 64, "source": MERGE_SOURCE}
    text = judge._query_excerpt("Q3", entry, {0}, ["订单数"], False, lambda _a: snap,
                                {"merge_members": ["手机号"]})
    assert text.startswith("【Q3】合并查询结果（共 1 行）")
    assert "合并的输入：s（节点 q_sales，数据源 merge_stores，1 行）、v（节点 q_visits，数据源 merge_members，1 行）" in text
    assert "13800000000" not in text and "遮罩" in text


async def test_a_truncated_input_fails_the_node(monkeypatch, sources):
    row = await run_graph(monkeypatch, store_graph(visits_args={"limit": 2}, report=False))
    assert row.status == "failed"
    [failed] = await events(row.id, "node.failed", "merge")
    assert "「v」" in failed["error"] and "截断" in failed["error"] and "聚合" in failed["error"]


async def test_warnings_reach_the_output_and_the_events(monkeypatch, sources):
    """门店编号一边是文本 '01'、一边是数 1；只按门店合并、没按日期：两道警告都要看得见。"""
    sales_sql = ("SELECT order_date AS 日期, substr(store_id, 2) AS 门店, COUNT(*) AS 订单数, SUM(amount) AS 销售额 "
                 "FROM orders GROUP BY order_date, store_id ORDER BY 1, 2")
    visits_sql = ("SELECT visit_date AS 日期, CAST(substr(store_code, 2) AS INTEGER) AS 门店, COUNT(*) AS 到店人数 "
                  "FROM visits GROUP BY visit_date, store_code ORDER BY 1, 2")
    merge_sql = "SELECT s.门店, s.订单数, s.销售额, v.到店人数 FROM s JOIN v ON s.门店 = v.门店"
    row = await run_graph(monkeypatch, store_graph(sales_sql=sales_sql, visits_sql=visits_sql, merge_sql=merge_sql,
                                                   report=False))
    assert row.status == "succeeded", row.error
    [finished] = await events(row.id, "node.finished", "merge")
    output = artifact_store.load(finished["artifact"])
    assert sorted(w["code"] for w in output["warnings"]) == ["key_type_mismatch", "rows_grew"]
    [done] = await events(row.id, "merge.end", "merge")
    assert sorted(w["code"] for w in done["warnings"]) == ["key_type_mismatch", "rows_grew"]
    logs = [e for e in await events(row.id, "log", "merge") if e.get("level") == "warn"]
    assert sorted(e["code"] for e in logs) == ["merge_key_type", "merge_rows_grew"]
    assert all(e["message"] for e in logs)


async def test_rerunning_the_node_on_the_same_inputs_gives_the_same_snapshot(monkeypatch, sources):
    """重放、继续运行时节点从头再执行：同样的输入得到同一份快照、同一条台账，下游的出处不变。"""
    from app.engine.context import NodeContext, RunContext
    from app.engine.nodes.merge import run_merge

    row = await run_graph(monkeypatch, store_graph(report=False))
    assert row.status == "succeeded", row.error
    outputs = {}
    for nid in ("q_sales", "q_visits"):
        [finished] = await events(row.id, "node.finished", nid)
        outputs[nid] = artifact_store.load(finished["artifact"])
    spec = GraphSpec.model_validate(store_graph(report=False))
    ctx = NodeContext(node=spec.node_map()["merge"],
                      run=RunContext(run_id=row.id, thread_id=row.id, spec=spec, agent_limits={}))
    state = {"nodes": outputs, "trail": [], "evidence": []}
    first, second = await run_merge(state, ctx), await run_merge(state, ctx)
    assert first == second
    assert first["evidence"][0]["artifact"] == first["nodes"]["merge"]["artifact"]


# --------------------------------------------------------------------------
# 助手
# --------------------------------------------------------------------------


def test_node_reference_explains_when_to_use_merge():
    from app.api.copilot import NODE_REFERENCE

    assert "- merge：" in NODE_REFERENCE
    block = NODE_REFERENCE.split("- merge：", 1)[1].split("\n- ", 1)[0]
    for phrase in ("inputs", "sql", "不同的库", "同一个库", "一条 SQL", "口径卡", "截断", "cell(nodes."):
        assert phrase in block, phrase
