"""口径卡的输入取自「查询区间里这张表没有数据」的列时，指标照实标出来（no_data），出具按缺口降档，复核给出重来的指示。

部署上撞过：一张周报的到访事件表只存最新一天，6 列全是 0，口径卡里 10 个指标是 0 或 0%，周报照写「上周到访人数为 0 人」。
查询工具现在会在结果和快照里记 no_data（data/coverage.py）；这里守它往下走的几步：

- 口径卡按输入的快照 id + 列认（按列：同一条查询里有数的列不受影响），给指标标 no_data、写明原因；清单文字注明；
  运行日志一条 metric_no_data；
- 有出具契约的成果按缺口降档；
- 复核把 metric_no_data / query_no_data 记成缺口（DEGRADED），并给出「按复核重来」的指示：换一张有这段数据的表再建一次，
  大概率就好。

库是现造的 SQLite 文件，表名、列名、数字都是虚构的。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.runner import run_manager

WINDOW = "{c} >= strftime('%Y%m%d', 'now', '-{a} days') AND {c} < strftime('%Y%m%d', 'now', '-{b} days')"
KPI_SQL = ("SELECT (SELECT SUM(pv) FROM daily_pv WHERE day >= date('now', '-14 days')) AS 客流PV, "
           "(SELECT COUNT(DISTINCT member_id) FROM gate_events WHERE " + WINDOW.format(c="event_date", a=14, b=7)
           + ") AS 上周到访人数, (SELECT COUNT(DISTINCT member_id) FROM gate_events WHERE "
           + WINDOW.format(c="event_date", a=21, b=14) + ") AS 上上周到访人数")
WOW = ("round((cell(nodes.q_kpi, 0, '上周到访人数') - cell(nodes.q_kpi, 0, '上上周到访人数')) "
       "/ cell(nodes.q_kpi, 0, '上上周到访人数'), 4) if cell(nodes.q_kpi, 0, '上上周到访人数') else None")


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def plant(tmp_path):
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE gate_events (event_date TEXT, member_id INTEGER);"
                         "INSERT INTO gate_events VALUES ('20200101', 1), ('20200101', 2);"
                         "CREATE TABLE daily_pv (day TEXT, pv INTEGER);")
        for back in range(30):
            db.execute("INSERT INTO daily_pv VALUES (date('now', ?), 100)", (f"-{back} days",))
    name = f"plant_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        source_id = row.id
    yield name
    await engines.invalidate(source_id)


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def board(source: str) -> dict:
    nodes = [
        node("start", "input", "开始"),
        node("q_kpi", "tool", "取数", tool=f"db_query__{source}", args={"sql": KPI_SQL}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1", metrics=[
            {"id": "pv", "name": "客流PV", "unit": "次", "expression": "cell(nodes.q_kpi, 0, '客流PV')"},
            {"id": "visitors", "name": "上周到访人数", "unit": "人", "expression": "cell(nodes.q_kpi, 0, '上周到访人数')"},
            {"id": "wow", "name": "到访周环比", "decimals": 4, "format": "percent_of_ratio", "expression": WOW},
        ]),
        node("out", "output", "成果", fields=[{"name": "指标", "value": "{{ nodes.card.text }}"}],
             contract={"metrics_from": ["card"], "required": ["pv", "visitors"]}),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def finished(graph: dict) -> tuple[Run, list[RunEvent]]:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            async with SessionLocal() as session:
                events = list((await session.execute(
                    select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq))).scalars())
            return row, events
    raise AssertionError(f"运行 {run.id} 没跑完：{row.status}")


def _card(events: list[RunEvent]) -> dict:
    [event] = [e for e in events if e.type == "node.finished" and e.node_id == "card"]
    [entry] = event.data["evidence"]
    return artifact_store.load(entry["artifact"])


async def test_metrics_fed_by_columns_without_data_are_flagged(plant):
    row, events = await finished(board(plant))
    metrics = {m["id"]: m for m in _card(events)["metrics"]}
    visitors, wow, pv = metrics["visitors"], metrics["wow"], metrics["pv"]
    assert visitors["value"] == 0 and visitors["no_data"] is True
    assert visitors["no_data_reason"].startswith("所依据的查询区间里这张表没有数据：")
    assert "gate_events" in visitors["no_data_reason"]
    [source] = visitors["no_data_sources"]
    assert source["column"] == "上周到访人数" and source["table"] == "gate_events" and source["artifact"]
    # 周环比两个输入都在没有数据的列上：照样标；它本身算不出来（分母为 0），两件事都照实记
    assert wow["no_data"] is True and wow["undefined"] is True and wow["value"] is None
    # 同一条查询里有数的列（客流 PV）不受影响
    assert "no_data" not in pv and pv["value"] > 0
    lines = row.output["指标"].split("\n")
    assert "- visitors（上周到访人数）= 0人（这段时间表里没有数据：不要当成 0 解读）" in lines
    assert not any("pv（客流PV）" in ln and "没有数据" in ln for ln in lines)
    # 运行日志：查询一条 query_no_data，口径卡一条 metric_no_data（受影响的指标都列出来）
    codes = [(e.data or {}).get("code") for e in events if e.type == "log"]
    assert "query_no_data" in codes
    [logged] = [e.data for e in events if e.type == "log" and (e.data or {}).get("code") == "metric_no_data"]
    assert logged["level"] == "warn" and logged["metrics"] == ["visitors", "wow"]


async def test_the_contract_degrades_on_a_metric_without_data(plant):
    row, _ = await finished(board(plant))
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    gaps = [g for g in issuance["gaps"] if "没有数据" in g]
    assert any("上周到访人数" in g for g in gaps) and any("到访周环比" in g for g in gaps), issuance["gaps"]


async def test_review_marks_it_and_asks_for_a_rebuild(plant):
    from app.chat import turn
    from app.engine import review as rv

    row, events = await finished(board(plant))
    signals = rv.scan(events, row.output)
    kinds = {s.kind: s.severity for s in signals}
    assert kinds.get("metric_no_data") == rv.DEGRADED and kinds.get("query_no_data") == rv.DEGRADED
    assert rv.should_retry(signals), "换一张有这段数据的表再建一次大概率就好"
    result = rv.ReviewResult(verdict="annotated", note="…", retry=True, signals=signals, severity=rv.worst(signals))
    assert turn.retry_after_review(result.as_dict(), 0) and not turn.retry_after_review(result.as_dict(), 1)
    # 别的降级信号照旧不重来
    other = [rv.Signal(kind="node_retry", detail="重试过一次", severity=rv.DEGRADED)]
    assert not rv.should_retry(other)
    assert not turn.retry_after_review(rv.ReviewResult(verdict="annotated", retry=True, signals=other,
                                                       severity=rv.DEGRADED).as_dict(), 0)


async def test_the_writer_is_told_and_given_the_rule(plant):
    """写作目录里这样的指标、查询格子里这样的列都写清楚；写作规则加一句。"""
    from app.engine.evidence import NO_DATA_RULE, build_catalog, writer_catalog

    row, events = await finished(board(plant))
    card = _card(events)
    [query] = [e for e in events if e.type == "node.finished" and e.node_id == "q_kpi"]
    ledger = [*query.data["evidence"], *[e for e in events if e.type == "node.finished"
                                         and e.node_id == "card"][0].data["evidence"]]
    catalog = build_catalog(nodes={"card": {**card, "artifact": ledger[-1]["artifact"]}}, ledger=ledger, inputs={})
    assert catalog["m:visitors"]["no_data"] is True and "no_data" not in catalog["m:pv"]
    text = writer_catalog(catalog).text
    visitors = next(ln for ln in text.splitlines() if "[[m:visitors]]" in ln)
    assert "这段时间表里没有数据" in visitors and "不要当成 0 解读" in visitors
    assert "这段时间表里没有数据" not in next(ln for ln in text.splitlines() if "[[m:pv]]" in ln)
    assert "（注意：「上周到访人数」" in text and "引用这几列时照实写这段时间表里没有数据" in text
    assert "照实写这段时间表里没有数据" in NO_DATA_RULE and "0%" in NO_DATA_RULE


async def test_the_report_prompt_carries_the_rule(plant, monkeypatch):
    from app.engine.evidence import NO_DATA_RULE
    from tests.test_report_node import script

    seen = script(monkeypatch, "上周到访人数这段时间表里没有数据。")
    graph = board(plant)
    out = graph["nodes"].pop()
    graph["nodes"] += [node("write", "report", "撰写", instructions="写一段周报"),
                       {**out, "data": {**out["data"], "config": {"fields": [{"name": "答案",
                                                                             "value": "{{ nodes.write.text }}"}]}}}]
    graph["edges"] = [{"source": a["id"], "target": b["id"]} for a, b in zip(graph["nodes"], graph["nodes"][1:])]
    await finished(graph)
    prompt = "\n".join(str(m.content) for m in seen[0])
    assert NO_DATA_RULE in prompt and "这段时间表里没有数据" in prompt
