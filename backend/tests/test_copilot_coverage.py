"""助手搭图时就发现「查的区间里表里没有数据」（copilot_gen.GraphChecker.coverage、data/coverage.sql_gaps）。

部署上撞过：周报的取数节点用一张只存最新一天的表算上周、上上周，跑完才发现全是 0。现在：

- 自查：助手搭的图里工具节点写死的 SQL，按今天跑一遍覆盖检查（不跑原 SQL，只跑算界和现查范围的两条小查询，按表去重、
  限时）；完全不重叠的作为一条问题交回模型改一轮（换有这段数据的表，或者在说明里照实告诉用户）；修正后仍在的不挡运行，
  作为提醒留在检查结果里；
- look / db_schema 的剖析信息：日期范围最早和最晚是同一天时加一句「可能每天整表重灌：算历史周期请换有历史的表」；
- 助手提示：写按周期取数的 SQL 前先看表的日期范围。

库是现造的 SQLite 文件，表名、列名、数字都是虚构的。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessageChunk

from app.data import introspect
from app.data.engine import engines
from app.data.schema_view import PROFILE_HINT_PROMPT, profile_lines
from app.db.base import SessionLocal
from app.db.models import DataSource
from tests.test_copilot_answer_ops import _sse, _use, client  # noqa: F401

NO_DATA_SQL = "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '20261012' AND event_date < '20261019'"
HAS_DATA_SQL = "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '20200101'"


@pytest.fixture
async def plant(tmp_path):
    path = tmp_path / f"{uuid.uuid4().hex[:8]}.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE gate_events (event_date TEXT, member_id INTEGER);"
                         "INSERT INTO gate_events VALUES ('20200101', 1), ('20200101', 2);")
    name = f"plant_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        source_id = row.id
    async with SessionLocal() as session:
        source = await session.get(DataSource, source_id)
    yield name, source
    await engines.invalidate(source_id)


def _node(nid: str, ntype: str, config: dict) -> dict:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _graph(name: str, sql: str) -> tuple[dict, list]:
    nodes = {"start": _node("start", "input", {"fields": [{"name": "question"}]}),
             "fetch": _node("fetch", "tool", {"tool": f"db_query__{name}", "args": {"sql": sql}}),
             "out": _node("out", "output", {"fields": [{"name": "n", "value": "{{ nodes.fetch }}"}]})}
    return nodes, [{"source": "start", "target": "fetch"}, {"source": "fetch", "target": "out"}]


async def test_the_checker_returns_it_once_then_keeps_it_as_a_warning(plant):
    from app.api.copilot_gen import GraphApplier, GraphChecker

    name, source = plant
    nodes, edges = _graph(name, NO_DATA_SQL)
    checker = GraphChecker(GraphApplier(nodes, edges), sources=[source])
    [issue] = [i for i in await checker.issues() if i["code"] == "sql_no_data"]
    assert issue["level"] == "error" and issue["node_id"] == "fetch" and issue["field"] == "args.sql"
    assert "查的区间是 2026-10-12 到 2026-10-18，但 gate_events 的 event_date 只有 2020-01-01 这一天的数据" \
        in issue["message"]
    assert "换有这段数据的表" in issue["for_model"] and "explanation" in issue["for_model"]
    # 交回模型改过一轮：之后不再挡；收尾时作为提醒
    assert not [i for i in await checker.issues() if i["code"] == "sql_no_data"]
    [warning] = await checker.coverage(level="warning")
    assert warning["level"] == "warning" and warning["code"] == "sql_no_data"


async def test_a_window_with_data_and_templated_sql_are_left_alone(plant):
    from app.api.copilot_gen import GraphApplier, GraphChecker

    name, source = plant
    for sql in (HAS_DATA_SQL, "SELECT COUNT(*) AS n FROM gate_events WHERE event_date >= '{{ input.day }}'"):
        nodes, edges = _graph(name, sql)
        checker = GraphChecker(GraphApplier(nodes, edges), sources=[source])
        assert not [i for i in await checker.issues() if i["code"] == "sql_no_data"], sql


async def test_the_stream_asks_for_one_repair_then_runs_with_a_warning(client, monkeypatch, plant):  # noqa: F811
    name, _ = plant
    nodes, edges = _graph(name, NO_DATA_SQL)
    build = [{"op": "plan", "summary": "数一数"},
             *({"op": "add_node", "node": {"id": n["id"], "type": n["type"], "label": n["id"],
                                           "config": n["data"]["config"]}} for n in nodes.values()),
             *({"op": "add_edge", "edge": e} for e in edges), {"op": "done", "explanation": "数一数", "run": True}]
    keep = [{"op": "done", "explanation": "gate_events 只有 2020-01-01 的数据，这段时间没有数据"}]
    seen: list[str] = []

    class Scripted:
        async def astream(self, messages):
            human = next(text for role, text in messages if role == "human")
            seen.append(human)
            for op in (keep if "上线前自查" in human else build):
                yield AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")

    _use(monkeypatch, Scripted())
    got = await _sse(client, {"instruction": "上周到访多少次", "intent": "answer"})
    [repairing] = [op for op in got if op["op"] == "check" and op["status"] == "repairing"]
    assert [i["code"] for i in repairing["issues"]] == ["sql_no_data"]
    assert any("上线前自查" in h and "换有这段数据的表" in h for h in seen), "交回模型改一轮"
    [passed] = [op for op in got if op["op"] == "check" and op["status"] == "passed"]
    assert [w["code"] for w in passed["warnings"]] == ["sql_no_data"]
    [final] = [op for op in got if op["op"] == "final"]
    assert final["next"] == "run", "修正后仍在的不挡运行"
    assert any(i["code"] == "sql_no_data" and i["level"] == "warning" for i in final["issues"])


def test_a_one_day_profile_says_so():
    facts = {"at": "2026-10-18T02:00:00+00:00", "date_ranges": [{"column": "event_date", "min": "20261017",
                                                                  "max": "20261017"}]}
    lines = profile_lines(facts, set(), None)
    assert any("剖析时这张表的 event_date 只有一天（2026-10-17）的数据，可能每天整表重灌：算历史周期请换有历史的表" in ln
               for ln in lines), lines
    assert lines[-1].strip() == PROFILE_HINT_PROMPT
    wide = {**facts, "date_ranges": [{"column": "event_date", "min": "20260101", "max": "20261017"}]}
    assert not any("整表重灌" in ln for ln in profile_lines(wide, set(), None))


def test_the_copilot_is_told_to_look_at_the_dates_first():
    from app.api.copilot import NODE_REFERENCE

    assert "先看表的日期范围" in NODE_REFERENCE and "只有一天的数据" in NODE_REFERENCE
