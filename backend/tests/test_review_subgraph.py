"""复核规则（engine/review.scan）对子工作流里失败的那一步只报一条：子图里那一步一条 node.failed、子工作流节点又一条，原话
相同，以前合成两条「执行失败」，问数据页的复核提示里同一件事写两遍。保留更具体的那一步，名字写清在哪个子工作流里
（「子工作流 › 那一步」，不写内部 id）。图和数据都是虚构的。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import review as rv
from app.engine.runner import run_manager
from tests import ledger_samples as S

OK = {"结果": "x"}


def ev(etype, **data):
    return {"type": str(etype), "node_id": data.pop("node_id", ""), "data": data}


def test_a_failure_inside_a_subworkflow_is_reported_once_with_where_it_happened():
    signals = rv.scan([
        ev(EventType.NODE_STARTED, node_id="sub", label="复核子流程"),
        ev(EventType.NODE_STARTED, node_id="sub/calc", label="算一下"),
        ev(EventType.NODE_FAILED, node_id="sub/calc", error="表达式写错了"),
        ev(EventType.NODE_FAILED, node_id="sub", error="表达式写错了"),
    ], OK)
    [signal] = [s for s in signals if s.kind == "node_failed"]
    assert signal.detail == "节点「复核子流程 › 算一下」执行失败：表达式写错了"
    assert "sub/" not in signal.detail and signal.severity == rv.BROKEN


def test_nested_two_levels_and_different_errors_stay_apart():
    signals = rv.scan([
        ev(EventType.NODE_STARTED, node_id="a", label="外层"),
        ev(EventType.NODE_STARTED, node_id="a/b", label="内层"),
        ev(EventType.NODE_STARTED, node_id="a/b/c", label="取数"),
        ev(EventType.NODE_FAILED, node_id="a/b/c", error="连不上"),
        ev(EventType.NODE_FAILED, node_id="a/b", error="连不上"),
        ev(EventType.NODE_FAILED, node_id="a", error="连不上"),
        # 父图里另一个节点原话不同的失败照报
        ev(EventType.NODE_STARTED, node_id="other", label="另一步"),
        ev(EventType.NODE_FAILED, node_id="other", error="超时"),
    ], OK)
    assert [s.detail for s in signals if s.kind == "node_failed"] == [
        "节点「外层 › 内层 › 取数」执行失败：连不上", "节点「另一步」执行失败：超时"]


def test_a_rejection_inside_a_subworkflow_is_reported_once_too():
    signals = rv.scan([
        ev(EventType.NODE_STARTED, node_id="sub", label="复核子流程"),
        ev(EventType.NODE_STARTED, node_id="sub/gate", label="把关"),
        ev(EventType.NODE_FAILED, node_id="sub/gate", error="人工驳回：不对", reason="rejected"),
        ev(EventType.NODE_FAILED, node_id="sub", error="人工驳回：不对", reason="rejected"),
    ], OK)
    assert [s.detail for s in signals if s.kind == "node_rejected"] == ["节点「复核子流程 › 把关」被驳回：人工驳回：不对"]


def test_a_subworkflow_failing_on_its_own_still_reports():
    """子工作流节点自己失败（没选工作流、版本不存在）：子图里没有哪一步失败，照报这一条。"""
    signals = rv.scan([
        ev(EventType.NODE_STARTED, node_id="sub", label="复核子流程"),
        ev(EventType.NODE_FAILED, node_id="sub", error="「子工作流」节点尚未选择要嵌套的工作流"),
    ], OK)
    assert [s.detail for s in signals if s.kind == "node_failed"] == [
        "节点「复核子流程」执行失败：「子工作流」节点尚未选择要嵌套的工作流"]


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


async def test_a_real_run_failing_inside_a_subworkflow_gives_one_signal(engine_up):
    inner = S.chain(node("s", "input"), node("calc", "transform", "算一下", mode="expression", expression="1 / 0"),
                    node("o", "output", fields=[{"name": "r", "value": "{{ nodes.calc.text }}"}]))
    sub_id = await S.publish("复核子流程", graph=inner, level="draft")
    graph = S.chain(node("start", "input"), node("sub", "subgraph", "复核子流程", workflow_id=sub_id, workflow_version=1),
                    node("out", "output", fields=[{"name": "r", "value": "{{ nodes.sub.output.r }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("failed", "succeeded") and not run_manager.is_active(run.id):
            break
    assert row.status == "failed"
    async with SessionLocal() as session:
        rows = list((await session.execute(select(RunEvent).where(RunEvent.run_id == run.id)
                                           .order_by(RunEvent.seq))).scalars())
    events = [{"type": r.type, "node_id": r.node_id, "data": r.data} for r in rows]
    failed = [s for s in rv.scan(events, {}) if s.kind == "node_failed"]
    assert len(failed) == 1, [s.detail for s in failed]
    assert failed[0].detail.startswith("节点「复核子流程 › 算一下」执行失败：")
