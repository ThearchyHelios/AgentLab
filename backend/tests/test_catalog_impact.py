"""目录的影响面：改了一张表的目录，哪些已发布、受管的模板会受影响（阶段 4A）。

GET /api/datasources/{id}/catalog/{table}/impact，每个模板按它当前的已发布版本算（不是画布上的草稿）：
- 调用工具节点调用这个源的 db_query__，SQL 里用到了这张表：直接引用。表名的取法和证据台账同一个口径
  （evidence.sql_tables，按 name_key 对到表结构上）。
- Agent 节点（协作成员也算）绑定了这个源的查询工具：可能涉及——SQL 是运行时写的，静态看不出用不用这张表。
- 合并查询节点按它的输入追溯：输入里有直接引用的，它也算直接引用，并写明经由哪个输入。
- 没发布过的草稿、别的源的同名表、只在未发布草稿里用到的，都不算。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.data.engine import engines
from app.data.introspect import introspect
# 模型要在建表之前注册进 metadata：conftest 的建表只导入了 app.db.base，这个文件单独跑时没有别的模块先导入它们
from app.db import models  # noqa: F401

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


def _node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "data": {"label": label or nid, "config": config}}


def _graph(*nodes):
    nodes = [_node("start", "input"), *nodes, _node("out", "output", fields=[])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def _query(nid, source, sql, label=None):
    return _node(nid, "tool", label, tool=f"db_query__{source}", args={"sql": sql})


@pytest.fixture
async def world(scenic_db):
    """一个景区数据源、另一个同结构的源，和几份模板；用完全部删掉。"""
    from app.db.base import SessionLocal
    from app.db.models import DataSource, Workflow, WorkflowVersion

    tag = uuid.uuid4().hex[:8]
    src, other = f"scenic_{tag}", f"mirror_{tag}"
    created: list[str] = []
    async with SessionLocal() as session:
        rows = []
        for name in (src, other):
            row = DataSource(name=name, kind="sqlite", database=scenic_db, readonly=True)
            row.schema_cache = await _schema(scenic_db)
            session.add(row)
            rows.append(row)
        await session.commit()

        async def template(name, versions, *, published=None, level="published"):
            wf = Workflow(name=f"{name}-{tag}", graph=versions[-1], version=len(versions),
                          status=level if published else "draft", published_version=published)
            session.add(wf)
            await session.flush()
            for i, g in enumerate(versions, start=1):
                session.add(WorkflowVersion(workflow_id=wf.id, version=i, graph=g,
                                            level=level if published == i else None))
            created.append(wf.id)
            return wf

        counted = _query("q", src, "SELECT COUNT(*) FROM main.VISITS v WHERE v.status = 1", "查询入园人数")
        direct = await template("入园日报", [_graph(counted)], published=1)
        agent = await template("入园分析", [_graph(_node("ask", "agent", "分析入园", prompt="分析",
                                                          tools=[f"db_query__{src}", f"db_schema__{src}"]))],
                               published=1, level="governed")
        team = await template("协作分析", [_graph(_node("team", "supervisor", "协作", agents=[
            {"name": "analyst", "tools": [f"db_query__{src}"]}]))], published=1)
        merged = await template("渠道合并", [_graph(
            _query("q1", src, "SELECT id, park_id FROM visits", "入园"),
            _query("q2", src, "SELECT id, channel_id FROM orders", "订单"),
            _node("m", "merge", "合并入园和订单", inputs={"a": "q1", "b": "q2"}, sql="SELECT * FROM a JOIN b ON a.id = b.id"),
            _node("m2", "merge", "再合并", inputs={"x": "m"}, sql="SELECT * FROM x"),
        )], published=1)
        # 不算的：草稿、别的源的同名表、只在未发布的新版本里用到
        await template("草稿", [_graph(_query("q", src, "SELECT * FROM visits"))])
        await template("镜像", [_graph(_query("q", other, "SELECT * FROM visits"))], published=1)
        await template("后来才用", [_graph(_query("q", src, "SELECT * FROM orders")),
                                    _graph(_query("q", src, "SELECT * FROM visits"))], published=1)
        # 已发布的是旧版本：画布上后来删掉了这张表，影响面仍按已发布的那一版算
        stale = await template("改过画布", [_graph(_query("q", src, "SELECT * FROM visits")),
                                            _graph(_query("q", src, "SELECT * FROM orders"))], published=1)
        await session.commit()
        ids = SimpleNamespace(source=rows[0].id, other=rows[1].id, src=src, direct=direct.id, agent=agent.id,
                              team=team.id, merged=merged.id, stale=stale.id)
    yield ids
    async with SessionLocal() as session:
        for wid in created:
            if (wf := await session.get(Workflow, wid)) is not None:
                await session.delete(wf)
        for sid in (ids.source, ids.other):
            if (row := await session.get(DataSource, sid)) is not None:
                await session.delete(row)
        await session.commit()


async def _impact(source_id: str, table: str) -> dict:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(f"/api/datasources/{source_id}/catalog/{table}/impact")
        assert r.status_code == 200, r.text
        return r.json()


async def test_impact_classifies_direct_possible_and_merge(world):
    out = await _impact(world.source, "visits")
    assert out["table"] == "visits"
    by_id = {t["workflow_id"]: t for t in out["templates"]}
    assert set(by_id) == {world.direct, world.agent, world.team, world.merged, world.stale}

    direct = by_id[world.direct]
    assert direct["impact"] == "direct" and direct["version"] == 1 and direct["level"] == "published"
    assert [(n["node_id"], n["label"], n["impact"]) for n in direct["nodes"]] == [("q", "查询入园人数", "direct")]

    agent = by_id[world.agent]
    assert agent["impact"] == "possible" and agent["level"] == "governed"
    assert [(n["node_id"], n["impact"]) for n in agent["nodes"]] == [("ask", "possible")]
    assert by_id[world.team]["impact"] == "possible"
    assert by_id[world.team]["nodes"][0]["member"] == "analyst"

    merged = by_id[world.merged]
    assert merged["impact"] == "direct"
    nodes = {n["node_id"]: n for n in merged["nodes"]}
    assert set(nodes) == {"q1", "m", "m2"}          # 查订单的 q2 不涉及这张表
    assert nodes["m"]["impact"] == "direct" and nodes["m"]["via"] == [{"node_id": "q1", "label": "入园", "alias": "a"}]
    # 合并的合并：一路追到底
    assert nodes["m2"]["impact"] == "direct" and nodes["m2"]["via"] == [{"node_id": "m", "label": "合并入园和订单",
                                                                          "alias": "x"}]
    # 直接引用的排在前面
    impacts = [t["impact"] for t in out["templates"]]
    assert impacts == sorted(impacts, key=lambda i: i != "direct")


async def test_impact_of_another_table(world):
    out = await _impact(world.source, "orders")
    by_id = {t["workflow_id"]: t for t in out["templates"]}
    # 已发布的旧版本只查了入园记录：画布上后来改成查订单，不算
    assert world.stale not in by_id
    assert by_id[world.merged]["impact"] == "direct"
    assert {n["node_id"] for n in by_id[world.merged]["nodes"]} == {"q2", "m", "m2"}
    # 只绑了查询工具的 Agent 对每张表都是「可能涉及」
    assert by_id[world.agent]["impact"] == "possible"
    assert world.direct not in by_id


async def test_impact_unknown_table_is_404(world):
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(f"/api/datasources/{world.source}/catalog/ghost_table/impact")
        assert r.status_code == 404


def test_graph_sql_tables_uses_the_evidence_rules():
    """发布时记目录版本和影响面共用这一份：只看调用工具节点写死的 SQL，表名按 name_key 对到表结构上。"""
    from app.data.catalog_impact import graph_sql_tables
    from app.engine.schema import GraphSpec

    spec = GraphSpec.model_validate(_graph(
        _query("q1", "shop", "SELECT * FROM main.Orders o JOIN order_items i ON i.order_id = o.id -- FROM ghost"),
        _query("q2", "shop", "WITH t AS (SELECT 1) SELECT * FROM t, customers"),
        _query("q3", "other", "SELECT * FROM orders"),
        _node("ask", "agent", tools=["db_query__shop"]),
    ))
    tables = {"shop": {"orders": {}, "order_items": {}, "customers": {}}, "other": {"orders": {}}}
    assert graph_sql_tables(spec, tables) == {"shop": {"orders", "order_items", "customers"}, "other": {"orders"}}
