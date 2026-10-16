"""口径账本测试共用的虚构样本：一个店铺库、一份受管口径「周销售」、跑一次探索运行。数据全是编的。

- 店铺库 shop（SQLite）：orders(id, region, status, amount, paid_at, phone)，九单，状态有 paid / refunded / open。
- 口径「周销售」v1（受管）：入口两个字段 start / end，取数节点 fetch 的 SQL 是
  SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'，
  口径卡 card 上一个指标 gmv = cell(nodes.fetch, 0, 'gmv')。
- 探索运行：入口 → 调用工具 q（执行的 SQL 由测试给）→ 可选的口径卡 → 报告撰写（模型的答复由测试给，引用 [[v:Q1.r0.gmv]] /
  [[m:gmv]]）→ 出口。
"""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.artifact_store import graph_hash
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, Workflow, WorkflowVersion

TEMPLATE = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
            "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")
#: 和口径等价的写法（别名不同、条件换序）
EQUIVALENT = ("select sum(o.amount) as 销售额 from orders o where o.paid_at >= '2026-09-01' and o.status = 'paid' "
              "and o.paid_at < '2026-09-08'")
#: 少了「只算已支付」这个条件（含退款的周销售）
WITH_REFUNDS = "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-09-08'"
#: 查的是别的表（没有相关口径）
ELSEWHERE = "SELECT COUNT(*) AS n FROM stores"
ROWS = [(1, "east", "paid", 100.0, "2026-09-01", "13800000001"), (2, "east", "paid", 50.5, "2026-09-03", "13800000002"),
        (3, "west", "refunded", 30.0, "2026-09-04", "13800000003"), (4, "west", "paid", 20.0, "2026-09-07", "13800000004"),
        (5, "east", "open", 12.5, "2026-09-07", "13800000005"), (6, "west", "paid", 70.0, "2026-09-09", "13800000006"),
        (7, "east", "paid", 5.0, "2026-08-31", "13800000007"), (8, "west", "refunded", 8.0, "2026-09-02", "13800000008"),
        (9, "east", "paid", 1.0, "2026-09-08", "13800000009")]


def node(nid: str, ntype: str, **config: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def build_shop(path) -> None:
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, status TEXT, amount REAL, "
                     "paid_at TEXT, phone TEXT); CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    db.executemany("INSERT INTO stores VALUES (?, ?)", [(1, "s1"), (2, "s2")])
    db.commit()
    db.close()


async def set_source(path, *, options: dict[str, Any] | None = None) -> str:
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly = str(path), "sqlite", True, True
        row.options = dict(options or {})
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    return source_id


def caliber_graph(sql: str = TEMPLATE, *, expression: str = "cell(nodes.fetch, 0, 'gmv')") -> dict[str, Any]:
    return chain(
        node("start", "input", fields=[{"name": "start", "required": True}, {"name": "end", "required": True},
                                       {"name": "store", "default": "all"}]),
        node("fetch", "tool", tool="db_query__shop", args={"sql": sql}),
        node("card", "metrics", caliber="周销售", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands", "expression": expression}]),
        node("write", "report", instructions="写周销售"),
        node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}]),
    )


async def publish(name: str = "周销售", *, graph: dict[str, Any] | None = None, level: str = "governed",
                  version: int = 1, workflow_id: str | None = None) -> str:
    """建一个工作流并把第 version 版按 level 发布（直接写库，和发布接口写下的几列一样）。返回工作流 id。"""
    graph = graph or caliber_graph()
    async with SessionLocal() as session:
        wf = await session.get(Workflow, workflow_id) if workflow_id else None
        if wf is None:
            wf = Workflow(name=name, graph=graph, status="published")
            session.add(wf)
            await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=version, graph=graph, graph_hash=graph_hash(graph),
                                    level=level))
        wf.published_version, wf.version, wf.graph = version, version, graph
        await session.commit()
        return wf.id


def explore_graph(sql: str, *, card_expression: str | None = None) -> dict[str, Any]:
    parts = [node("start", "input"), node("q", "tool", tool="db_query__shop", args={"sql": sql})]
    if card_expression:
        parts.append(node("mine", "metrics", caliber="我的口径", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands",
             "expression": card_expression}]))
    parts += [node("write", "report", instructions="写答案"),
              node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}])]
    return chain(*parts)


def script(monkeypatch, text: str) -> None:
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def explore(monkeypatch, sql: str, *, text: str = "本周销售额 [[v:Q1.r0.gmv]]。", origin: str | None = "canvas",
                  card_expression: str | None = None, run_class: str = "exploratory") -> Run:
    """跑一次探索运行、等它封存完、等后台的口径比对做完。"""
    from app.engine.runner import run_manager
    from app.ledger import match as ledger_match

    script(monkeypatch, text)
    run = await run_manager.start(graph=explore_graph(sql, card_expression=card_expression), input_payload={},
                                  run_class=run_class, origin_kind=origin)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    await ledger_match.drain()
    return row
