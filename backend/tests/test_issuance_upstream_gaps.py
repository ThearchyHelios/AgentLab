"""上游节点报上来的出具缺口（产出里的 issuance_gaps）：成果节点的出具契约照单收下，出具降档。

这是给双盲复算、反方审稿这类把关节点留的通用口子。节点只管在产出里写一句缺口，
不带节点名；契约统一加上「节点类型「节点名」：」的前缀，和别的缺口一样读得出是谁报的。
没写这个键的图，出具和以前一模一样。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": label or nid, "config": config}}


def graph(gate: dict, *, side: dict | None = None) -> dict:
    """口径卡 → 叙述 → 把关节点 → 成果。叙述里的数都回指得到口径卡，不加把关节点的话是完整出具。
    side 是挂在另一条支路上、不通向成果的节点。"""
    nodes = [
        node("start", "input"),
        node("card", "metrics", caliber="销售口径", caliber_version="v1", metrics=[{"id": "orders", "expression": "120"}]),
        node("draft", "transform", mode="template", assign_to="summary", template="本周订单 120 单"),
        gate,
        node("out", "output", fields=[{"name": "结论", "value": "{{ vars.summary }}"}],
             contract={"metrics_from": "card", "narrative": "{{ vars.summary }}"}),
    ]
    edges = [{"source": "start", "target": "card"}, {"source": "card", "target": "draft"},
             {"source": "draft", "target": gate["id"]}, {"source": gate["id"], "target": "out"}]
    if side:
        nodes.append(side)
        edges.append({"source": "start", "target": side["id"]})
    return {"nodes": nodes, "edges": edges}


def gaps_node(nid: str, label: str, gaps: list) -> dict:
    """用数据整形的 JSON 模板产出 issuance_gaps：和真正的把关节点产出同一个形状"""
    return node(nid, "transform", label, mode="json", template=json.dumps({"issuance_gaps": gaps}, ensure_ascii=False))


async def finish(spec: dict) -> Run:
    run = await run_manager.start(graph=spec, input_payload={})
    for _ in range(200):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed"):
                break
    assert row.status == "succeeded", row.error
    return row


async def issuance_event(run_id: str) -> dict:
    async with SessionLocal() as session:
        event = (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "issuance")
        )).scalars().one()
    return event.data


async def test_upstream_gap_degrades_issuance_and_names_the_node():
    row = await finish(graph(gaps_node("recheck", "复核销售额", ["两路结果不一致：订单数 120 对 118", "  ", 42])))

    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    # 空白和不是字符串的条目跳过，只收那一句；前缀是节点类型的叫法加节点名
    assert issuance["gaps"] == ["数据整形「复核销售额」：两路结果不一致：订单数 120 对 118"]
    # 数字照常回指：降档只因为这条缺口
    assert issuance["unmatched_numbers"] == [] and issuance["matched_numbers"] == 1
    assert (await issuance_event(row.id))["gaps"] == issuance["gaps"]


async def test_stub_gate_nodes_pass_through_without_touching_issuance():
    """反方审稿目前是占位：放在图里照样能运行，出具不受影响。双盲复算已经实现，没有可复算的指标时直接放行、
    不碰出具，见 test_dual_check_node.py"""
    for kind in ("challenge",):
        row = await finish(graph(node("gate", kind)))
        issuance = row.output["_issuance"]
        assert issuance["tier"] == "formal", (kind, issuance)
        assert issuance["gaps"] == []
        async with SessionLocal() as session:
            logs = (await session.execute(
                select(RunEvent).where(RunEvent.run_id == row.id, RunEvent.type == "log", RunEvent.node_id == "gate")
            )).scalars().all()
        assert [e.data.get("level") for e in logs] == ["info"]
        assert "开发中" in logs[0].data["message"]


async def test_without_issuance_gaps_nothing_changes():
    """没写 issuance_gaps 的节点，和别的支路上写了的节点，都不影响这份成果"""
    plain = node("recheck", "transform", "复核销售额", mode="json", template='{"ok": true}')
    row = await finish(graph(plain, side=gaps_node("elsewhere", "别处的检查", ["和这份成果无关"])))

    issuance = row.output["_issuance"]
    assert issuance["tier"] == "formal"
    assert issuance["gaps"] == []
