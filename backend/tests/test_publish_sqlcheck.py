"""发布门禁接上基于数据目录的 SQL 检查（data/sqlcheck.py）。

- 受管级别：调用工具节点里写死的 SQL 有 error 级问题（一对多关联后重复计算、存量跨期求和）就挡住发布；
- 已发布级别：同样的问题只是警告，不挡；
- warning 级的问题两档都进提示，info（依据只是推断）不进门禁；
- 发布前检查、自动修复和真正发布同一个口径：检查说能发的，真发布时不会被拦。
"""
from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog
from app.data.engine import engines
from app.data.introspect import introspect
from app.data.sqlcheck import SqlChecker
from app.engine.governance import lint_for_publish, publish_issues
from app.engine.schema import GraphSpec
from app.main import app
from tests.fixtures.catalog import scenic_notes
from tests.fixtures.sources import drop_source

FANOUT_SQL = ("SELECT SUM(o.total_amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id "
              "WHERE o.status = 1")
CLEAN_SQL = "SELECT SUM(i.amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = 1"
WARN_SQL = "SELECT SUM(i.amount) AS gmv FROM orders o JOIN order_items i ON i.order_id = o.id"

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


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 10, "y": 20}, "data": {"label": label or nid, "config": config}}


def weekly(source: str, sql: str) -> dict:
    """input → 取数（调用工具，写死的 SQL）→ 口径卡 → 报告撰写 → 出具，其余受管要求都满足。"""
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool=f"db_query__{source}", args={"sql": sql}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "订单金额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", "报告撰写", instructions="写周报",
             numbers="strict", on_violation="fail", claims="require_citation"),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}),
    ]
    edges = [{"id": f"e{i}", "source": a["id"], "target": b["id"]} for i, (a, b) in enumerate(zip(nodes, nodes[1:]))]
    return {"nodes": nodes, "edges": edges, "defaults": {"approval": "dangerous"}}


async def _checkers(scenic_db, status="confirmed"):
    cache = await _schema(scenic_db)
    return {"scenic": SqlChecker(kind="sqlite", schema_cache=cache, notes=scenic_notes.notes(status),
                                 source_name="scenic")}


def _sql_issues(issues):
    return [(i.code, i.level, i.node_id, i.field) for i in issues if i.field == "args.sql"]


async def test_governed_level_blocks_error_level_sql_problems(scenic_db):
    checkers = await _checkers(scenic_db)
    spec = GraphSpec.model_validate(weekly("scenic", FANOUT_SQL))
    assert not [i for i in publish_issues(spec, level="governed") if i.level == "error"], "其余受管要求都满足"
    issues = publish_issues(spec, level="governed", checkers=checkers)
    assert _sql_issues(issues) == [("fanout_sum", "error", "fetch", "args.sql")]
    [issue] = [i for i in issues if i.code == "fanout_sum"]
    assert issue.message.startswith("「订单」关联「订单明细」是一对多")
    # 已发布级别：同一个问题只警告
    published = publish_issues(spec, level="published", checkers=checkers)
    assert _sql_issues(published) == [("fanout_sum", "warning", "fetch", "args.sql")]
    # 拦不拦（level）之外，带着检查本来的级别（sql_level）：界面据此说「错误级问题，已发布只提醒、受管会拦」
    assert [i.sql_level for i in issues if i.field == "args.sql"] == ["error"]
    assert [i.sql_level for i in published if i.field == "args.sql"] == ["error"]
    # 别的问题没有这一项，序列化时也不出现
    assert all("sql_level" not in i.model_dump() for i in published if i.field != "args.sql")
    assert [i.model_dump()["sql_level"] for i in published if i.field == "args.sql"] == ["error"]
    # 依据只是推断：info 不进门禁
    assert _sql_issues(lint_for_publish(spec, level="governed", checkers=await _checkers(scenic_db, "proposed"))
                       .issues) == []
    # 没有检查器（数据源没有目录）：和以前一样
    assert _sql_issues(lint_for_publish(spec, level="governed").issues) == []


async def test_warnings_are_hints_on_both_levels(scenic_db):
    checkers = await _checkers(scenic_db)
    spec = GraphSpec.model_validate(weekly("scenic", WARN_SQL))
    for level in ("governed", "published"):
        assert _sql_issues(publish_issues(spec, level=level, checkers=checkers)) == [
            ("missing_valid_filter", "warning", "fetch", "args.sql")]
    clean = GraphSpec.model_validate(weekly("scenic", CLEAN_SQL))
    assert _sql_issues(publish_issues(clean, level="governed", checkers=checkers)) == []


async def test_autofix_check_and_apply_use_the_same_gate(scenic_db):
    from app.engine.autofix import apply_fixes, check

    checkers = await _checkers(scenic_db)
    graph = weekly("scenic", FANOUT_SQL)
    body = check(GraphSpec.model_validate(graph), level="governed", checkers=checkers)
    assert body["ok"] is False and "fanout_sum" in {i["code"] for i in body["issues"]}
    out = apply_fixes(copy.deepcopy(graph), [], level="governed", checkers=checkers)
    assert out["ok"] is False and "fanout_sum" in {i["code"] for i in out["remaining"]}


# --------------------------------------------------------------------------
# 接口：发布、发布前检查、自动修复
# --------------------------------------------------------------------------


@pytest.fixture
async def scenic_source(scenic_db):
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


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_publish_endpoints_block_governed_fanout(client, scenic_source):
    r = await client.post("/api/workflows", json={"name": f"订单周报-{scenic_source}",
                                                  "graph": weekly(scenic_source, FANOUT_SQL)})
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    checked = (await client.post(f"/api/workflows/{wid}/publish-check", json={"level": "governed"})).json()
    assert checked["ok"] is False
    [issue] = [i for i in checked["issues"] if i["code"] == "fanout_sum"]
    assert issue["level"] == "error" and issue["node_id"] == "fetch" and issue["field"] == "args.sql"
    assert issue["sql_level"] == "error"
    hint = (await client.post(f"/api/workflows/{wid}/publish-check", json={"level": "published"})).json()
    assert [(i["level"], i["sql_level"]) for i in hint["issues"] if i["code"] == "fanout_sum"] == [("warning", "error")]
    assert all("sql_level" not in i for i in hint["issues"] if i.get("field") != "args.sql")
    assert issue["fix"] is None, "SQL 写错没有确定性的修复，交给人或助手改"

    fixed = (await client.post(f"/api/workflows/{wid}/autofix", json={"level": "governed", "apply": []})).json()
    assert fixed["ok"] is False and "fanout_sum" in {i["code"] for i in fixed["remaining"]}

    blocked = (await client.post(f"/api/workflows/{wid}/publish", json={"level": "governed"})).json()
    assert blocked["ok"] is False and "fanout_sum" in {i.get("code") for i in blocked["issues"]}
    # 已发布级别不挡，问题作为警告带回
    published = (await client.post(f"/api/workflows/{wid}/publish", json={"level": "published"})).json()
    assert published["ok"] is True
    assert [i["level"] for i in published["issues"] if i.get("code") == "fanout_sum"] == ["warning"]

    # 改好 SQL：受管级别发得出去，剩下的 warning 进提示
    await client.patch(f"/api/workflows/{wid}", json={"graph": weekly(scenic_source, WARN_SQL)})
    ok = (await client.post(f"/api/workflows/{wid}/publish", json={"level": "governed"})).json()
    assert ok["ok"] is True, ok["issues"]
    assert [i["code"] for i in ok["issues"] if i.get("field") == "args.sql"] == ["missing_valid_filter"]


class _Scripted:
    """按剧本吐操作流的模型；记下每次收到的请求。"""

    def __init__(self, ops):
        self.ops, self.calls = ops, []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        self.calls.append(messages)
        for op in self.ops:
            yield AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")


async def test_publish_fix_assistant_gets_the_sql_error_and_its_fix_is_adopted(client, scenic_source, monkeypatch):
    """确定性修复修不了 SQL：交给助手。助手看到的是给模型的那句改法，改好的 SQL 复核通过才采纳。"""
    import app.api.copilot as copilot

    model = _Scripted([{"op": "plan", "summary": "改 SQL"},
                       {"op": "update_node", "id": "fetch",
                        "config": {"tool": f"db_query__{scenic_source}", "args": {"sql": CLEAN_SQL}}},
                       {"op": "done", "explanation": "改为对明细金额求和"}])

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    wid = (await client.post("/api/workflows", json={"name": f"订单周报-助手-{scenic_source}",
                                                     "graph": weekly(scenic_source, FANOUT_SQL)})).json()["id"]
    out = (await client.post(f"/api/workflows/{wid}/autofix",
                             json={"level": "governed", "apply": [], "assist": True})).json()
    human = next(text for role, text in model.calls[0] if role == "human")
    assert "orders 关联 order_items 是一对多" in human
    assert out["assist"]["ok"] is True and out["applied"] == ["assist"] and out["ok"] is True, out
    sql = next(n for n in out["graph"]["nodes"] if n["id"] == "fetch")["data"]["config"]["args"]["sql"]
    assert sql == CLEAN_SQL
