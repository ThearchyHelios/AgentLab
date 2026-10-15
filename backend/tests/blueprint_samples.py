"""蓝图、试运行、发布单测试共用的样本（docs/design/normal-user-apps.md 包 B2）。数据全是虚构的。

- 一个小的 SQLite 数据源（门店订单，金额、支付日），登记成只读数据源；
- 看板图：统计期 → 查订单 → 口径卡 → 按指标出具（不调模型）；
- 流程图：发起报销 → 财务会签（岗位 finance，1 小时超时升级给 director）→ 已通过 / 已退回；
- 存工作流并直接落一个发布版本（发布门禁另有测试，这里要的是一个已发布的不可变版本）、等运行结束、改蓝图卡片。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, Run, Workflow, WorkflowVersion
from app.db.models_blueprints import Blueprint

GMV_SQL = ("SELECT ROUND(SUM(amount), 2) AS gmv, COUNT(*) AS orders FROM orders "
           "WHERE paid_at BETWEEN '{{ input.period_start }}' AND '{{ input.period_end }}'")
PERIOD = {"period": "2026-W40", "period_start": "2026-09-28", "period_end": "2026-10-04"}


def node(nid: str, ntype: str, label: str | None = None, **config: Any) -> dict[str, Any]:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def make_source(tmp_path: Path) -> str:
    """建库、登记数据源，交回数据源名（每个测试一个，名字不撞）。同步写库文件，登记在调用方的事件循环里做。"""
    path = tmp_path / f"shop_{uuid.uuid4().hex[:6]}.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL, paid_at TEXT)")
        db.executemany("INSERT INTO orders (amount, paid_at) VALUES (?, ?)",
                       [(120.5, "2026-09-29"), (80.0, "2026-10-01"), (999.0, "2026-10-09")])
    return str(path)


async def register_source(path: str) -> str:
    name = f"shop_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        session.add(DataSource(name=name, kind="sqlite", database=path, readonly=True, enabled=True))
        await session.commit()
    return name


def board_graph(source: str) -> dict[str, Any]:
    """统计期 → 查订单 → 口径卡 → 按指标出具。"""
    return chain(
        node("start", "input", "统计期", fields=[
            {"name": "period", "default": PERIOD["period"]},
            {"name": "period_start", "default": PERIOD["period_start"]},
            {"name": "period_end", "default": PERIOD["period_end"]}]),
        node("q", "tool", "查订单", tool=f"db_query__{source}", args={"sql": GMV_SQL}),
        node("caliber", "metrics", "周销售口径卡", caliber="周销售口径", caliber_version="v1", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "expression": "cell(nodes.q, 0, 'gmv')"}]),
        node("done", "output", "出具", fields=[{"name": "销售额", "value": "{{ nodes.caliber.metrics[0].value }}"}],
             contract={"metrics_from": ["caliber"], "required": ["gmv"], "strict": True}),
    )


def expense_graph() -> dict[str, Any]:
    nodes = [node("start", "input", "发起报销", fields=[{"name": "applicant"}, {"name": "amount", "type": "number"}]),
             node("review", "human", "财务会签", mode="approve", title="报销审批：{{ input.applicant }}",
                  assignee={"kind": "slot", "slot": "finance"}, policy="all", due="PT1H",
                  on_timeout={"action": "escalate", "to_slot": "director"}),
             node("ok", "output", "已通过", fields=[{"name": "结论", "value": "已通过"}]),
             node("no", "output", "已退回", fields=[{"name": "结论", "value": "已退回"}])]
    edges = [{"source": "start", "target": "review"},
             {"source": "review", "target": "ok", "sourceHandle": "approved"},
             {"source": "review", "target": "no", "sourceHandle": "rejected"}]
    return {"nodes": nodes, "edges": edges}


async def published_workflow(graph: dict[str, Any], name: str, *, description: str = "",
                             level: str = "published") -> str:
    """存工作流并把 v1 立为发布版本（直接落库）。交回工作流 id。"""
    async with SessionLocal() as session:
        wf = Workflow(name=name, description=description, graph=graph, version=1, published_version=1,
                      status=level)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, graph_hash=graph_hash(graph),
                                    level=level, published_at=utcnow(), published_by="样本"))
        await session.commit()
        return wf.id


async def wait(run_id: str, statuses: tuple[str, ...] = ("succeeded", "failed", "cancelled"),
               timeout: float = 30.0) -> Run:
    """等运行走到这几种状态之一（终态的还要等封存完）。"""
    from app.engine.runner import run_manager

    deadline = asyncio.get_event_loop().time() + timeout
    row = None
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row is not None and row.status in statuses and not run_manager.is_active(run_id) and (
                row.status == "interrupted" or row.manifest_seq is not None):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有走到 {statuses}：{row.status if row else None}")


def item(value: Any, source: str = "user", status: str = "confirmed") -> dict[str, Any]:
    return {"value": value, "source": source, "status": status, "why": None}


async def edit_spec(blueprint_id: str, **cards: Any) -> None:
    """直接改蓝图的卡片（卡片上的修改另有测试，这里要的是一张填好的蓝图）。"""
    async with SessionLocal() as session:
        bp = await session.get(Blueprint, blueprint_id)
        bp.spec = {**(bp.spec or {}), **cards}
        await session.commit()


WEEKLY = {"preset": "weekly", "weekday": 1, "time": "09:00", "tz": "Asia/Shanghai", "period": "last_iso_week"}


async def blueprint(blueprint_id: str) -> Blueprint:
    async with SessionLocal() as session:
        return await session.get(Blueprint, blueprint_id)
