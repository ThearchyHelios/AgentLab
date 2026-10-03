"""截断的查询结果：单格照常可信，整组拿来算的数不能当成正式结果出具。

查询撞了行数或字节上限时，数据层只交回前面一截，标 truncated: true。以前这面旗只进了证据台账：
口径卡对这一截求 len()、sum()，算出来的「入园人次」只是前 1000 行的人次，却照样按 formal 出具。
阶段 0 把它接上：

- 口径卡：表达式把截断结果的行当成整组用（len / sum / min / max / sorted / any / all、成员判断、
  倒数第几行、row_count……）时，这个指标标 incomplete，带一句原因；按行号取单格（cell()、rows[0]）
  不受影响——那一格本身是真实的。判定在求值时跟踪，不靠正则猜表达式
- agent 经 cite_fields 交来的数组字段：引用的行段到了截断结果的末行，同样算截断的整组
- 出具：契约收来的指标里有不完整的，记缺口、降档，原因写成一句人话
- 报告：整表引用截断的查询结果时，表格下面带一行说明；单格引用不受影响
- 查询层：恰好取满上限、后面没有更多行的，不算截断（以前会误报）
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines, run_query
from app.data.guard import QueryLimits
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import StreamRenderer, build_catalog, compose_doc, verify_doc
from app.engine.expressions import TruncatedUse, eval_expression
from app.engine.issuance import decide_tier, incomplete_gaps
from app.engine.runner import run_manager

# --------------------------------------------------------------------------
# 求值器：整组用到截断结果时记一笔，单格不记
# --------------------------------------------------------------------------

ROWS = [["东门", 120], ["西门", 80], ["南门", 45]]
QUERY_ART = "a" * 64


def visits(truncated: bool = True) -> dict:
    """数据源工具交回的查询结果经 transform 解析之后的样子。"""
    return {"columns": ["gate", "visitors"], "rows": [list(r) for r in ROWS], "row_count": len(ROWS),
            "truncated": truncated, "sql": "SELECT gate, visitors FROM entries", "artifact": QUERY_ART}


def scope(truncated: bool = True) -> dict:
    return {"vars": {"visits": visits(truncated), "plain": {"rows": [120, 80, 45], "truncated": truncated}},
            "nodes": {}, "input": {}}


WHOLE = [
    "len(vars.visits.rows)",
    "vars.visits.row_count",
    "max(vars.visits.rows)",
    "min(vars.visits.rows)[1]",
    "sorted(vars.visits.rows)[0][1]",
    "any(vars.visits.rows)",
    "all(vars.visits.rows)",
    "['东门', 120] in vars.visits.rows",
    "contains(vars.visits.rows, ['西门', 80])",
    "vars.visits.rows[-1][1]",
    "len(vars.visits['rows'])",
    "len(get(vars.visits, 'rows'))",
    "vars.visits.rows == []",
    "len(vars.visits.rows + [])",
    "len([] + vars.visits.rows)",
    "str(vars.visits.rows)",
    "sum(vars.plain.rows)",
    "max(vars.plain.rows) - min(vars.plain.rows)",
    "len(vars.visits.rows) if vars.visits.rows else 0",
]


@pytest.mark.parametrize("expr", WHOLE)
def test_whole_set_use_of_truncated_rows_is_recorded(expr):
    use = TruncatedUse()
    value = eval_expression(expr, scope(), track=use)
    assert use.sources, f"{expr} 把截断的行当成整组用了，应当记下"
    # 只是记一笔：算出来的值和不跟踪时一模一样
    assert value == eval_expression(expr, scope())


@pytest.mark.parametrize("expr", WHOLE)
def test_untruncated_results_are_not_recorded(expr):
    use = TruncatedUse()
    eval_expression(expr, scope(truncated=False), track=use)
    assert use.sources == {}


@pytest.mark.parametrize("expr", [
    "cell(vars.visits, 0, 'visitors')",
    "cell(vars.visits, 2, 'gate')",
    "vars.visits.rows[0][1]",
    "vars.visits.rows[2][0]",
    "vars.visits.columns",
    "len(vars.visits.columns)",
    "bool(vars.visits.rows)",
    "vars.visits.rows and 1",
    "vars.visits.sql",
])
def test_single_cells_and_rows_are_not_affected(expr):
    """按行号取的那一格、那一行在完整结果里也是这一格、这一行：截断不影响它。"""
    use = TruncatedUse()
    eval_expression(expr, scope(), track=use)
    assert use.sources == {}


def test_reading_past_the_fetched_rows_is_recorded():
    """取回的行之后的行号：完整结果里有这一行，只是没取回来。值是空的，原因是截断。"""
    use = TruncatedUse()
    assert eval_expression("vars.visits.rows[5]", scope(), track=use) is None
    assert use.sources


def test_one_source_per_result_with_rows_and_artifact():
    use = TruncatedUse()
    assert eval_expression("len(vars.visits.rows) + vars.visits.row_count", scope(), track=use) == 6
    assert list(use.sources.values()) == [{"path": "vars.visits", "rows": 3, "artifact": QUERY_ART}]


def test_the_tracking_wrapper_never_leaks_into_results():
    """包装只活在一次求值里：交出去的永远是普通的 list，进状态、落工件都不受影响。"""
    use = TruncatedUse()
    value = eval_expression("vars.visits.rows", scope(), track=use)
    assert type(value) is list and value == ROWS and use.sources
    nested = eval_expression("[vars.visits.rows, {'r': vars.plain.rows}]", scope(), track=TruncatedUse())
    assert type(nested[0]) is list and type(nested[1]["r"]) is list
    json.dumps(nested)


def test_untracked_evaluation_is_unchanged():
    """不跟踪（分支条件、数据整形）时不包装：行为和以前完全一样。"""
    assert type(eval_expression("vars.visits.rows", scope())) is list
    assert eval_expression("len(vars.visits.rows)", scope()) == 3


def test_caller_declared_sets_are_tracked_too():
    """口径卡认得出的截断整组（agent 取到截断边缘的数组字段）经 edge 告诉求值器。"""
    seen: list[str] = []

    def edge(path: str):
        seen.append(path)
        return {"rows": 3, "artifact": "b" * 64} if path in ("vars.kpi.amounts", "vars.kpi['amounts']") else None

    ctx = {"vars": {"kpi": {"amounts": [60.0, 60.0, 30.0], "label": "门票"}}}
    use = TruncatedUse(edge=edge)
    assert eval_expression("sum(vars.kpi.amounts)", ctx, track=use) == 150.0
    assert list(use.sources.values()) == [{"path": "vars.kpi.amounts", "rows": 3, "artifact": "b" * 64}]
    use = TruncatedUse(edge=edge)
    assert eval_expression("vars.kpi.amounts[0]", ctx, track=use) == 60.0 and use.sources == {}
    use = TruncatedUse(edge=edge)
    assert eval_expression("len(get(vars.kpi, 'amounts'))", ctx, track=use) == 3 and use.sources
    assert "vars.kpi['amounts']" in seen


# --------------------------------------------------------------------------
# 出具：不完整的指标记缺口、降档
# --------------------------------------------------------------------------

REASON = "基于被截断的查询结果计算（只取回了前 1000 行），结果不完整"


def test_incomplete_metrics_become_readable_gaps():
    metrics = [{"id": "visits", "name": "入园人次", "value": 1000, "incomplete": True, "incomplete_reason": REASON},
               {"id": "first", "name": "首笔票款", "value": 60}]
    gaps = incomplete_gaps(metrics)
    assert gaps == [f"指标「入园人次」{REASON}"]
    assert decide_tier(missing_required=[], missing_expected=[], unmatched=[], strict=True, gaps=gaps) == "degraded"
    assert incomplete_gaps(metrics[1:]) == []


# --------------------------------------------------------------------------
# 报告：整表引用截断的查询结果，表格下面带一行说明
# --------------------------------------------------------------------------

TICKET_ROWS = [[i, gate, 60.0] for i, gate in enumerate(["东门", "西门", "南门", "北门", "东门", "西门", "南门"], 1)]


def ticket_snap(truncated: bool) -> dict:
    return {"columns": ["id", "gate", "amount"], "rows": TICKET_ROWS, "row_count": len(TICKET_ROWS),
            "truncated": truncated, "elapsed_ms": 2, "sql": "SELECT id, gate, amount FROM tickets", "source": "park"}


def _store(snap: dict) -> str:
    text = artifact_store.canonical_json(snap)
    artifact = artifact_store.content_hash(text)
    path = artifact_store._path_of(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return artifact


def ticket_catalog(truncated: bool) -> dict:
    snap = ticket_snap(truncated)
    entry = {"kind": "query", "node_id": "fetch", "exec": 1, "artifact": _store(snap), "tool": "db_query__park",
             "source": "park", "columns": snap["columns"], "rows": len(snap["rows"]), "truncated": truncated}
    return build_catalog(nodes={}, ledger=[entry])


NOTE_HEAD = "> 注：表中只显示了前 5 行；查询结果已截断，取回的数据并不完整。"


def test_truncated_table_carries_a_note():
    catalog = ticket_catalog(truncated=True)
    doc = compose_doc("各入园口如下：\n\n[[table:Q1 cols=gate,amount]]\n\n以上为当日记录。", catalog)
    assert doc["violations"] == [], doc["violations"]
    assert f"| 东门 | 60 |\n{NOTE_HEAD}\n\n以上为当日记录。" in doc["markdown"]
    assert [b["type"] for b in doc["blocks"]] == ["paragraph", "table", "quote", "paragraph"]
    # 说明是连接性的话：不是结论，不要求挂依据
    note = doc["blocks"][2]["units"][0]
    assert note["kind"] == "connective"
    # 出口复核照样通过：说明里的「前 5 行」是序号，不算裸数字
    assert verify_doc(doc, catalog)["violations"] == []


def test_table_from_the_middle_does_not_claim_the_first_rows():
    doc = compose_doc("[[table:Q1 cols=gate rows=2-3]]", ticket_catalog(truncated=True))
    assert doc["violations"] == []
    assert doc["markdown"].endswith("| 北门 |\n> 注：查询结果已截断，取回的数据并不完整。")


def test_untruncated_table_has_no_note():
    doc = compose_doc("[[table:Q1 cols=gate,amount]]", ticket_catalog(truncated=False))
    assert doc["violations"] == [] and "注：" not in doc["markdown"]


def test_single_cells_of_a_truncated_result_have_no_note():
    doc = compose_doc("首笔票款 [[v:Q1.r0.amount]]。", ticket_catalog(truncated=True))
    assert doc["violations"] == [] and doc["markdown"] == "首笔票款 60。"


def test_streaming_renders_the_note_exactly_like_the_whole_document():
    catalog = ticket_catalog(truncated=True)
    raw = "各入园口如下：\n[[table:Q1 cols=gate,amount]]\n以上为当日记录。"
    renderer = StreamRenderer(catalog)
    streamed = "".join(renderer.feed(ch) for ch in raw) + renderer.flush()
    assert streamed == compose_doc(raw, catalog)["markdown"]
    assert NOTE_HEAD in streamed


# --------------------------------------------------------------------------
# 查询层：恰好取满上限不算截断
# --------------------------------------------------------------------------


@pytest.fixture
def ticket_db(tmp_path):
    path = tmp_path / "park.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE tickets (id INTEGER PRIMARY KEY, gate TEXT, amount REAL);")
    db.executemany("INSERT INTO tickets VALUES (?, ?, ?)", [(1, "东门", 60.0), (2, "西门", 60.0), (3, "南门", 30.0)])
    db.commit()
    db.close()
    return path


@pytest.fixture
async def probe(ticket_db):
    source = SimpleNamespace(id=f"probe-{ticket_db.parent.name}", name="park_probe", kind="sqlite",
                             database=str(ticket_db), host=None, port=None, username=None, password=None,
                             options={}, readonly=True, schema_cache={})
    yield source
    await engines.invalidate(source.id)


async def test_exactly_reaching_the_row_limit_is_not_truncation(probe):
    """以前取满 max_rows 就标截断：查 3 行、上限 3 行，明明是完整结果，下游却会被降档。"""
    source = probe
    full = await run_query(source, "SELECT id FROM tickets ORDER BY id", limits=QueryLimits(max_rows=3))
    assert full.rows == [[1], [2], [3]] and full.truncated is False
    cut = await run_query(source, "SELECT id FROM tickets ORDER BY id", limits=QueryLimits(max_rows=2))
    assert cut.rows == [[1], [2]] and cut.row_count == 2 and cut.truncated is True


async def test_byte_limit_on_the_last_row_is_not_truncation(probe):
    source = probe
    last = await run_query(source, "SELECT id FROM tickets WHERE id = 1", limits=QueryLimits(max_bytes=1))
    assert last.rows == [[1]] and last.truncated is False
    cut = await run_query(source, "SELECT id FROM tickets ORDER BY id", limits=QueryLimits(max_bytes=1))
    assert cut.rows == [[1]] and cut.truncated is True


# --------------------------------------------------------------------------
# agent 的数组字段：引用的行段到了截断结果的末行
# --------------------------------------------------------------------------


def test_array_fields_reaching_the_cut_are_marked():
    from app.engine.nodes.llm import verify_cited_fields

    def run(truncated: bool):
        snap = {"columns": ["id", "amount"], "rows": [[1, 60.0], [2, 60.0]], "row_count": 2, "truncated": truncated}
        queries = [{"alias": "Q1", "artifact": "c" * 64, "via": None}]
        number = {"type": "number"}
        fields = [("amounts", {"type": "array", "items": number}), ("first", {"type": "array", "items": number}),
                  ("one", number),
                  ("rows", {"type": "array", "items": {"type": "object", "properties": {"id": number, "amount": number}}})]
        parsed = {
            "amounts": {"value": [60.0, 60.0], "from": {"call": "Q1", "rows": "0-1", "column": "amount"}},
            "first": {"value": [60.0], "from": {"call": "Q1", "rows": "0-0", "column": "amount"}},
            "one": {"value": 60.0, "from": {"call": "Q1", "row": 1, "column": "amount"}},
            "rows": {"value": [{"id": 1, "amount": 60.0}, {"id": 2, "amount": 60.0}],
                     "from": {"call": "Q1", "rows": "0-1", "columns": {"id": "id", "amount": "amount"}}},
        }
        return verify_cited_fields({"parsed": parsed}, fields, queries, loader=lambda _: snap)[1]

    evidence = run(truncated=True)
    assert evidence["amounts"]["truncated"] is True and evidence["rows"]["truncated"] is True
    # 行段停在末行之前：引用的每一行都在快照里，截断拿不走其中任何一行
    assert "truncated" not in evidence["first"]
    # 单格不是聚合：那一格是真实的
    assert "truncated" not in evidence["one"]
    assert all("truncated" not in e for e in run(truncated=False).values())


# --------------------------------------------------------------------------
# 整条链：查询 → 口径卡 → 报告 → 出具
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def park(ticket_db, engine_up):
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "park"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="park", kind="sqlite", database=str(ticket_db), readonly=True, enabled=True)
            session.add(row)
        else:
            row.database, row.kind, row.enabled, row.readonly, row.options = str(ticket_db), "sqlite", True, True, {}
        await session.commit()
        source_id = row.id
    # 连接池按数据源 id 缓存：换了库文件不作废，查的还是上一个测试的库
    await engines.invalidate(source_id)
    yield
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(nodes: list[dict]) -> dict:
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


VISITS = {"id": "visits", "name": "入园人次", "unit": "人次", "format": "plain", "expression": "len(vars.tickets.rows)"}
FIRST = {"id": "first", "name": "首笔票款", "unit": "元", "format": "plain", "expression": "cell(nodes.fetch, 0, 'amount')"}
DAILY = "今日入园人次 [[m:visits]]，首笔票款 [[m:first]]，首张门票 [[v:Q1.r0.amount]]。\n\n[[table:Q1 cols=gate,amount]]"


def daily(limit: int | None) -> dict:
    args = {"sql": "SELECT id, gate, amount FROM tickets ORDER BY id", **({"limit": limit} if limit else {})}
    return chain([
        node("start", "input"),
        node("fetch", "tool", tool="db_query__park", args=args),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="tickets"),
        node("card", "metrics", caliber="入园口径", caliber_version="v1", metrics=[VISITS, FIRST]),
        node("write", "report", instructions="写入园日报"),
        node("out", "output", fields=[{"name": "日报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["visits", "first"]}),
    ])


class Writer:
    def __init__(self, monkeypatch, text: str = DAILY):
        from app.providers import mock_model

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda model, messages: AIMessage(content=text))


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没有结束：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def output_of(run_id: str, node_id: str) -> dict:
    [finished] = await events(run_id, "node.finished", node_id)
    return artifact_store.load(finished.data["artifact"])


async def test_counting_a_truncated_result_degrades_the_issuance(park, monkeypatch):
    Writer(monkeypatch)
    run = await run_manager.start(graph=daily(limit=2), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    card = await output_of(row.id, "card")
    metrics = {m["id"]: m for m in card["metrics"]}
    [query_end] = [e.data for e in await events(row.id, "tool.end", "fetch")]
    reason = "基于被截断的查询结果计算（只取回了前 2 行），结果不完整"
    assert metrics["visits"]["value"] == 2
    assert metrics["visits"]["incomplete"] is True and metrics["visits"]["incomplete_reason"] == reason
    assert metrics["visits"]["truncated_sources"] == [
        {"path": "vars.tickets", "rows": 2, "artifact": query_end["query_artifact"]}]
    assert metrics["visits"]["status"] == "ok" and metrics["visits"]["recompute_ok"] is True
    # cell() 取的单格不受影响
    assert metrics["first"]["value"] == 60 and "incomplete" not in metrics["first"]
    assert "（不完整：基于被截断的查询结果计算）" in card["text"]
    [warn] = [e.data for e in await events(row.id, "log", "card") if e.data.get("code") == "metric_incomplete"]
    assert warn["level"] == "warn" and warn["message"] == f"指标「入园人次」{reason}"

    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded", issuance
    assert f"指标「入园人次」{reason}" in issuance["gaps"]
    # 报告：整表带说明、单格照常，整篇没有违规
    report = row.output["日报"]
    assert "> 注：表中只显示了前 2 行；查询结果已截断，取回的数据并不完整。" in report
    assert "首张门票 60。" in report
    [checked] = [e.data for e in await events(row.id, "report.checked")]
    assert checked["ok"] is True, checked["violations"]


async def test_the_same_card_on_a_complete_result_issues_formally(park, monkeypatch):
    Writer(monkeypatch)
    run = await run_manager.start(graph=daily(limit=None), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    card = await output_of(row.id, "card")
    assert all("incomplete" not in m for m in card["metrics"])
    assert {m["id"]: m["value"] for m in card["metrics"]} == {"visits": 3, "first": 60}
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == [], issuance
    assert "注：" not in row.output["日报"]


SCHEMA = {"type": "object", "properties": {
    "amounts": {"type": "array", "items": {"type": "number"}, "description": "各张门票的票款"}}}


def ticket_agent() -> dict:
    return chain([
        node("start", "input"),
        node("bot", "agent", prompt="查当日票款", tools=["db_query__park"], approval="never",
             output_schema=SCHEMA, cite_fields=True, assign_to="kpi"),
        node("card", "metrics", caliber="票款口径", metrics=[
            {"id": "total", "name": "票款合计", "unit": "元", "format": "plain", "expression": "sum(vars.kpi.amounts)"},
            {"id": "head", "name": "首张票款", "unit": "元", "format": "plain", "expression": "vars.kpi.amounts[0]"}]),
        node("out", "output", fields=[{"name": "kpi", "value": "{{ vars.kpi | json }}"}],
             contract={"metrics_from": ["card"], "required": ["total", "head"], "narrative": "{{ nodes.card.text }}"}),
    ])


class AgentScript:
    """agent 查一次（limit 2，库里 3 行：截断），结构化抽取按 rows 交数组字段的出处。"""

    def __init__(self, monkeypatch, rows: str, values: list[float]):
        from app.providers import mock_model

        extracted = {"amounts": {"value": values, "from": {"call": "Q1", "rows": rows, "column": "amount"}}}

        def decide(model, messages):
            if model.response_format:
                return AIMessage(content=json.dumps(extracted, ensure_ascii=False))
            if not model.tools:
                return AIMessage(content="收尾")
            if not any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content="查。", tool_calls=[{"name": "db_query__park", "id": "call_0", "args": {
                    "sql": "SELECT id, amount FROM tickets ORDER BY id", "limit": 2}}])
            return AIMessage(content="已查到当日票款。")

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


@pytest.mark.parametrize("rows, values, incomplete", [
    ("0-1", [60.0, 60.0], True),     # 取到了截断结果的末行：后面没取回的票也该算进去
    ("0-0", [60.0], False),           # 只引用第一行：这一行是真实的，求和就是这一格
])
async def test_summing_an_agent_array_cut_by_truncation(park, monkeypatch, rows, values, incomplete):
    AgentScript(monkeypatch, rows, values)
    run = await run_manager.start(graph=ticket_agent(), input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error

    bot = await output_of(row.id, "bot")
    assert bot["data_evidence"]["amounts"].get("truncated", False) is incomplete
    card = await output_of(row.id, "card")
    metrics = {m["id"]: m for m in card["metrics"]}
    assert metrics["total"]["value"] == sum(values)
    assert metrics["total"].get("incomplete", False) is incomplete
    # 按下标取的一个元素就是快照里那一格
    assert "incomplete" not in metrics["head"]
    issuance = row.output["_issuance"]
    if incomplete:
        assert metrics["total"]["incomplete_reason"] == "基于被截断的查询结果计算（只取回了前 2 行），结果不完整"
        assert metrics["total"]["inputs"][0]["truncated"] is True
        assert issuance["tier"] == "degraded"
        assert "指标「票款合计」基于被截断的查询结果计算（只取回了前 2 行），结果不完整" in issuance["gaps"]
    else:
        assert issuance["tier"] == "formal", issuance
