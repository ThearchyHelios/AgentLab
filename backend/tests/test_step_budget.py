"""循环跑满自己声明的轮数，不被全局步数上限半路掐断。

开发库里的「测试 1」：while 循环 `vars.state.count <= 1000`，max_iterations 配了
1005。每轮是「循环节点 + 循环体」两步，而整次运行的步数上限（max_graph_steps）
是 200——跑到 100 轮上下 LangGraph 抛 GraphRecursionError，报错是一段英文，
叫人去调一个界面上根本碰不到的 recursion_limit。校验是通过的：这张图没写错，
是平台自己的两个上限在打架。

全局上限防的是没人把关的环；loop 节点的 max_iterations 是作者明说的轮数上限。
所以循环按自己的轮数另记一份预算（loop_steps），全局上限照旧管其余的环。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, loop_steps


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "data": {"label": nid, "config": config}}


def edge(source, target, handle=None):
    return {"source": source, "target": target, "sourceHandle": handle}


def counting_loop(rounds: int, cap: int) -> dict:
    """「测试 1」的骨架：while 数到 rounds，循环体一个节点。"""
    return {
        "nodes": [
            node("start", "input", fields=[]),
            node("init", "transform", expression="0", assign_to="n"),
            node("lp", "loop", mode="while", condition=f"vars.n < {rounds}", max_iterations=cap),
            node("inc", "transform", expression="vars.n + 1", assign_to="n"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.n }}"}]),
        ],
        "edges": [edge("start", "init"), edge("init", "lp"), edge("lp", "inc", "body"),
                  edge("inc", "lp"), edge("lp", "out", "done")],
    }


def test_no_loop_no_extra_budget():
    spec = GraphSpec.model_validate({
        "nodes": [node("a", "input"), node("b", "output")], "edges": [edge("a", "b")]})
    assert loop_steps(spec) == 0


def test_budget_covers_the_declared_rounds():
    # 1005 轮 × 每轮 2 步，外加收尾的一次判定
    assert loop_steps(GraphSpec.model_validate(counting_loop(1000, 1005))) >= 1005 * 2 + 1


def test_nested_loops_multiply():
    """内层循环每一轮外层都要从头跑一遍，预算得乘起来，不是加起来。"""
    spec = GraphSpec.model_validate({
        "nodes": [node("start", "input"),
                  node("outer", "loop", mode="foreach", items="[1,2,3]", max_iterations=3),
                  node("inner", "loop", mode="foreach", items="[1,2,3,4]", max_iterations=4),
                  node("work", "transform", expression="1"),
                  node("out", "output")],
        "edges": [edge("start", "outer"), edge("outer", "inner", "body"),
                  edge("inner", "work", "body"), edge("work", "inner"),
                  edge("inner", "outer", "done"), edge("outer", "out", "done")],
    })
    # 实际：外层 4 次判定 + 3 轮 ×（内层 5 次判定 + 4 次干活）= 31 步
    assert loop_steps(spec) >= 31


def test_nodes_after_the_loop_are_not_charged_per_round():
    """done 出口之后的节点只跑一次，不该按轮数记账——否则预算虚高，等于没有上限。"""
    spec = GraphSpec.model_validate(counting_loop(3, 3))
    assert loop_steps(spec) == (3 + 1) * 2   # 只有 lp 和 inc


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def _finish(graph) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(600):
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed", "cancelled", "interrupted"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"没跑完：{row.status}")


async def test_a_loop_runs_past_the_global_step_limit(engine_up, monkeypatch):
    """全局上限压到 20 步，循环照样跑满它声明的 30 轮。"""
    monkeypatch.setattr(settings, "max_graph_steps", 20)
    row = await _finish(counting_loop(30, 35))
    assert row.status == "succeeded", row.error
    assert row.output["结果"] == "30"


async def test_an_unguarded_cycle_still_stops_and_says_why(engine_up, monkeypatch):
    """分支连回上游、没有 loop 节点把关的环，照旧由全局上限拦住——报错得是人话。"""
    monkeypatch.setattr(settings, "max_graph_steps", 12)
    row = await _finish({
        "nodes": [node("start", "input", fields=[]),
                  node("t", "transform", expression="1"),
                  node("br", "branch", cases=[{"key": "again", "condition": "true"}]),
                  node("out", "output", fields=[])],
        "edges": [edge("start", "t"), edge("t", "br"), edge("br", "t", "again"),
                  edge("br", "out", "default")],
    })
    assert row.status == "failed"
    assert "走满了 12 步" in row.error and "loop 节点" in row.error, row.error
    assert "recursion_limit" not in row.error


# --------------------------------------------------------------------------
# 子图：自己的循环自己算预算；撞上限时报子图自己的名字和步数
# --------------------------------------------------------------------------


async def _parent_of(sub_graph: dict, name: str) -> dict:
    from app.db.models import Workflow

    async with SessionLocal() as session:
        wf = Workflow(name=name, graph=sub_graph)
        session.add(wf)
        await session.commit()
        wf_id = wf.id
    return {
        "nodes": [node("start", "input", fields=[]),
                  node("sub", "subgraph", workflow_id=wf_id),
                  node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.sub.output.结果 }}"}])],
        "edges": [edge("start", "sub"), edge("sub", "out")],
    }


async def test_a_loop_inside_a_subgraph_gets_its_own_budget(engine_up, monkeypatch):
    monkeypatch.setattr(settings, "max_graph_steps", 20)
    row = await _finish(await _parent_of(counting_loop(30, 35), "数到 30"))
    assert row.status == "succeeded", row.error
    assert row.output["结果"] == "30"


async def test_an_unguarded_cycle_inside_a_subgraph_says_which_one(engine_up, monkeypatch):
    monkeypatch.setattr(settings, "max_graph_steps", 12)
    spinning = {
        "nodes": [node("start", "input", fields=[]),
                  node("t", "transform", expression="1"),
                  node("br", "branch", cases=[{"key": "again", "condition": "true"}]),
                  node("out", "output", fields=[])],
        "edges": [edge("start", "t"), edge("t", "br"), edge("br", "t", "again"),
                  edge("br", "out", "default")],
    }
    row = await _finish(await _parent_of(spinning, "空转的子图"))
    assert row.status == "failed"
    assert "子图「空转的子图」走满了 12 步" in row.error, row.error
    assert "recursion_limit" not in row.error
