"""助手搭完图的自查接上基于数据目录的 SQL 检查（data/sqlcheck.py）。

- 调用工具节点里写死的 SQL 走现有的自查：error 级（一对多关联后重复计算、存量跨期求和）交回模型改，进修正轮；
  warning、info 不返工，放进 final 的问题清单。
- Agent 节点的 SQL 要到运行时才由模型写出来，静态拿不到，不查（运行时由数据源查询工具检查）。
- 这一轮没碰的节点不查：用户自己写的 SQL 不替他改（和列名核对同一个规矩）。
- 没有目录的数据源什么都不查。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog
from app.data.engine import engines
from app.data.introspect import introspect
from app.data.sqlcheck import SqlChecker
from tests.fixtures.catalog import scenic_notes
from tests.fixtures.sources import drop_source

FANOUT_SQL = ("SELECT SUM(o.total_amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id "
              "WHERE o.status = 1")
#: 改好的：对明细求和；没按订单状态筛，留一条 warning
FIXED_SQL = "SELECT SUM(i.amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id"
CLEAN_SQL = "SELECT SUM(i.amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1"

_CACHE: dict[str, dict] = {}


async def _schema(path: str) -> dict:
    if path not in _CACHE:
        probe = SimpleNamespace(id=f"probe-{uuid.uuid4().hex[:6]}", name="scenic", kind="sqlite", database=path,
                                host=None, port=None, username=None, password=None, options={}, readonly=True,
                                description="", schema_cache={}, origin="manual")
        try:
            _CACHE[path] = await introspect(probe)
        finally:
            await engines.invalidate(probe.id)
    return _CACHE[path]


def _spec(*nodes):
    from app.engine.schema import GraphSpec

    return GraphSpec.model_validate({"nodes": list(nodes), "edges": [
        {"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]})


def _node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "data": {"label": nid, "config": config}}


def _fetch(sql, source="scenic"):
    return _node("fetch", "tool", tool=f"db_query__{source}", args={"sql": sql})


async def _checkers(scenic_db, status="confirmed"):
    cache = await _schema(scenic_db)
    source = SimpleNamespace(id="s1", name="scenic", kind="sqlite", schema_cache=cache)
    checker = SqlChecker(kind="sqlite", schema_cache=cache, notes=scenic_notes.notes(status), source_name="scenic")
    return source, {"scenic": checker}


# --------------------------------------------------------------------------
# authored_issues / _blocking_issues
# --------------------------------------------------------------------------


async def test_sql_check_errors_block_and_warnings_do_not(scenic_db):
    from app.api.copilot import _blocking_issues, authored_issues

    source, checkers = await _checkers(scenic_db)
    spec = _spec(_node("start", "input"), _fetch(FANOUT_SQL), _node("out", "output", fields=[]))
    [issue] = authored_issues(spec, [source], checkers=checkers)
    assert issue["code"] == "fanout_sum" and issue["level"] == "error"
    assert issue["node_id"] == "fetch" and issue["field"] == "args.sql"
    assert issue["table"] == "orders" and issue["column"] == "total_amount" and issue["sql_excerpt"]
    assert issue["message"].startswith("「订单」关联「订单明细」是一对多") and "order_items" in issue["for_model"]
    nodes = {n.id: n.model_dump(mode="json") for n in spec.nodes}
    assert [i["code"] for i in _blocking_issues(nodes, [], sources=[source], checkers=checkers)] == ["fanout_sum"]

    # 只有 warning：不挡（不返工），但 authored_issues 照样带出来，收尾时放进 final
    warn = _spec(_node("start", "input"), _fetch(FIXED_SQL), _node("out", "output", fields=[]))
    [w] = authored_issues(warn, [source], checkers=checkers)
    assert (w["code"], w["level"]) == ("missing_valid_filter", "warning")
    nodes = {n.id: n.model_dump(mode="json") for n in warn.nodes}
    assert _blocking_issues(nodes, [], sources=[source], checkers=checkers) == []

    # 依据只是推断：降为 info，也不挡
    _, proposed = await _checkers(scenic_db, "proposed")
    assert {i["level"] for i in authored_issues(spec, [source], checkers=proposed)} == {"info"}
    # 不给检查器（没有目录）：和以前一样什么都不查
    assert authored_issues(spec, [source]) == []


async def test_unknown_columns_come_first(scenic_db):
    """列名都对不上的 SQL 先改列名：这时不再叠加目录检查的结果，免得一轮里塞给模型两套说法。"""
    from app.api.copilot import authored_issues

    source, checkers = await _checkers(scenic_db)
    bad = ("SELECT SUM(o.total_amt) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id "
           "WHERE o.status = 1")
    spec = _spec(_node("start", "input"), _fetch(bad), _node("out", "output", fields=[]))
    assert [i["code"] for i in authored_issues(spec, [source], checkers=checkers)] == ["sql_unknown_column"]


async def test_agent_prompts_and_untouched_nodes_are_not_checked(scenic_db):
    from app.api.copilot import authored_issues

    source, checkers = await _checkers(scenic_db)
    agent = _node("ask", "agent", prompt=f"用这条 SQL 查：{FANOUT_SQL}", tools=["db_query__scenic"])
    spec = _spec(_node("start", "input"), agent, _node("out", "output", fields=[]))
    assert authored_issues(spec, [source], checkers=checkers) == []
    # 用户自己写的、这一轮没改的 SQL 不替他改
    fetch = _fetch(FANOUT_SQL)
    untouched = _spec(_node("start", "input"), fetch, _node("out", "output", fields=[]))
    assert authored_issues(untouched, [source], baseline=[fetch], checkers=checkers) == []
    # 模板占位照常检查其余部分
    templated = _fetch("SELECT SUM(o.total_amount) FROM orders o JOIN order_items i ON i.order_id = o.id "
                       "WHERE o.status = 1 AND o.ordered_at >= '{{ input.since }}'")
    out = authored_issues(_spec(_node("start", "input"), templated, _node("out", "output", fields=[])), [source],
                          checkers=checkers)
    assert [i["code"] for i in out] == ["fanout_sum"]


# --------------------------------------------------------------------------
# 端到端：假模型搭图，error 进修正轮，warning 留在 final
# --------------------------------------------------------------------------


class _Scripted:
    """第一轮吐 first；收到自查请求（human 里有「上线前自查」）时吐 fix。"""

    def __init__(self, first, fix):
        self.first, self.fix = first, fix
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        human = next(text for role, text in messages if role == "human")
        self.calls.append(human)
        for op in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


def _ops(source, sql):
    return [
        {"op": "plan", "summary": "查订单金额"},
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
        {"op": "add_node", "node": {"id": "fetch", "type": "tool", "label": "取数",
                                    "config": {"tool": f"db_query__{source}", "args": {"sql": sql}}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "fetch"}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果",
                                    "config": {"fields": [{"name": "r", "value": "{{ nodes.fetch }}"}]}}},
        {"op": "add_edge", "edge": {"source": "fetch", "target": "out"}},
        {"op": "done", "explanation": "查一下", "run": True},
    ]


@pytest.fixture
async def scenic_source(scenic_db):
    """库里一个带目录的景区数据源：表结构真实探查，目录按 scenic_notes 写进 catalog_notes。"""
    from app.db.base import SessionLocal
    from app.db.models import DataSource

    name = f"scenic_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=scenic_db, readonly=True)
        row.schema_cache = await _schema(scenic_db)
        session.add(row)
        await session.commit()
        for table, notes in scenic_notes.notes().items():
            await catalog.write_entry(session, row.id, table, notes, if_version=0, actor="王敏")
        source_id = row.id
    yield name
    await drop_source(source_id)


async def _stream(monkeypatch, model, source):
    import app.api.copilot as copilot
    from app.main import app

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("POST", "/api/copilot/generate-stream",
                                 json={"instruction": "上周订单金额多少", "intent": "answer",
                                       "datasource_ids": [source]}) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    return events


async def test_fanout_is_sent_back_and_fixed_before_running(scenic_source, monkeypatch):
    model = _Scripted(_ops(scenic_source, FANOUT_SQL),
                      [{"op": "update_node", "id": "fetch", "config": {"args": {"sql": FIXED_SQL}}},
                       {"op": "done", "explanation": "改好了"}])
    events = await _stream(monkeypatch, model, scenic_source)
    checks = [e for e in events if e["op"] == "check"]
    assert [c["status"] for c in checks] == ["repairing", "passed"], checks
    [issue] = checks[0]["issues"]
    assert issue["code"] == "fanout_sum" and issue["node_id"] == "fetch"
    # 交回模型的是 for_model：写 SQL 里的真名和改法
    assert "orders 关联 order_items 是一对多" in model.calls[1] and "「fetch」" in model.calls[1]
    final = events[-1]
    assert final["op"] == "final" and final["autorun"] is True
    sql = next(n for n in final["graph"]["nodes"] if n["id"] == "fetch")["data"]["config"]["args"]["sql"]
    assert sql == FIXED_SQL
    # 剩下的 warning 不返工，留在 final 里看得见
    [warn] = [i for i in final["issues"] if i.get("code") == "missing_valid_filter"]
    assert warn["level"] == "warning" and warn["node_id"] == "fetch" and "status = 1" in warn["message"]
    assert not [i for i in final["issues"] if i["level"] == "error"]


async def test_a_fanout_that_is_not_fixed_stops_autorun(scenic_source, monkeypatch):
    model = _Scripted(_ops(scenic_source, FANOUT_SQL),
                      [{"op": "update_node", "id": "fetch", "config": {"args": {"sql": FANOUT_SQL + " "}}},
                       {"op": "done", "explanation": "改了"}])
    events = await _stream(monkeypatch, model, scenic_source)
    checks = [e for e in events if e["op"] == "check"]
    assert [c["status"] for c in checks] == ["repairing", "repairing", "failed"], checks
    final = events[-1]
    assert final["autorun"] is False
    assert [i["code"] for i in final["issues"] if i["level"] == "error"] == ["fanout_sum"]


async def test_clean_sql_costs_no_extra_call(scenic_source, monkeypatch):
    model = _Scripted(_ops(scenic_source, CLEAN_SQL), [])
    events = await _stream(monkeypatch, model, scenic_source)
    assert len(model.calls) == 1
    assert [e["status"] for e in events if e["op"] == "check"] == ["passed"]
    final = events[-1]
    assert final["autorun"] is True
    assert not [i for i in final["issues"] if i.get("code") in ("fanout_sum", "missing_valid_filter")]


# --------------------------------------------------------------------------
# 一键升级的语义层（assist_upgrade）：自查和最终判断同样带 SQL 检查器，和发布门禁一个口径
# --------------------------------------------------------------------------


def _upgrade_graph(source: str) -> dict:
    """查库 → 沙箱代码算比率 → 口径卡读代码的产出 → 报告撰写 → 出口。查库的 SQL 本身没问题。"""
    def n(nid, ntype, x, **config):
        return {"id": nid, "type": ntype, "position": {"x": x, "y": 80}, "data": {"label": nid, "config": config}}

    nodes = [
        n("start", "input", 0),
        n("fetch", "tool", 280, tool=f"db_query__{source}", args={"sql": CLEAN_SQL}),
        n("calc", "code", 560, language="python", assign_to="calc", code="import json\nprint(json.dumps({'ratio': 3 / 4}))"),
        n("card", "metrics", 840, metrics=[{"id": "ratio", "name": "客单价", "expression": "vars.calc.ratio"}]),
        n("write", "report", 1120, instructions="写一句话"),
        n("out", "output", 1400, fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"id": f"e{i}", "source": a["id"], "target": b["id"]}
                                      for i, (a, b) in enumerate(zip(nodes, nodes[1:]))]}


async def test_upgrade_assist_checks_sql_like_the_publish_gate(scenic_source, monkeypatch):
    """助手改写口径卡时顺手把查询改成了一对多关联后求和：受管级别的发布门禁会挡，升级的自查也得交回去改，
    改不好就作废——以前自查和最终判断都没带 SQL 检查器，这样的改写会被采纳，交出去的图发布时才被拦。"""
    import app.api.copilot as copilot
    from app.db.base import SessionLocal

    first = [{"op": "plan", "summary": "把纯算术的代码挪进口径卡"},
             {"op": "update_node", "id": "card", "config": {"metrics": [
                 {"id": "ratio", "name": "客单价", "expression": "cell(nodes.fetch, 0, 'gmv') / 4"}]}},
             {"op": "update_node", "id": "fetch", "config": {"args": {"sql": FANOUT_SQL}}},
             {"op": "done", "explanation": "客单价改由口径卡直接算"}]
    fix = [{"op": "update_node", "id": "fetch", "config": {"args": {"sql": FANOUT_SQL + " "}}},
           {"op": "done", "explanation": "改了"}]
    model = _Scripted(first, fix)

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    async with SessionLocal() as session:
        out = await copilot.assist_upgrade(session, _upgrade_graph(scenic_source), level="governed")
    # 自查把一对多求和交回模型，带着给模型的改法
    assert len(model.calls) >= 2 and "上线前自查" in model.calls[1]
    assert "orders 关联 order_items 是一对多" in model.calls[1]
    # 改不好：整个作废，原因写的是这条 SQL 检查
    assert out["accepted"] is False and out["ok"] is False
    assert "一对多" in (out["reason"] or ""), out["reason"]
