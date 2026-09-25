"""汇合节点只跑一次：等该到的都到了，再动手。

多条边汇入同一个节点时，编译器以前对每条入边各调一次 add_edge。LangGraph
对这种接法的语义是"任何一条入边的上游跑完，就触发一次"——两条支路一样长时，
两次触发落在同一步里被合并成一次执行，看起来一切正常；一长一短时，汇合节点
先拿着短支路的结果跑一次（长支路的数据还是旧的），长支路到了再跑一次。模型
调用、工具副作用全部翻倍，下游也跟着跑两遍，而第一次的半截结果已经进了事件流。

修法不能是一律改成屏障（add_edge([a, b], j)）：分支之后的"二选一"汇合，
没走的那条永远不会到，屏障会一直等下去——而 LangGraph 在没有可执行任务时
会安静地结束运行，汇合节点和它下游的一切就这么被跳过了，连报错都没有。
所以这里守两头：长短支路只跑一次，分支剪掉的那条不许把汇合节点饿死。
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.compiler import compile_graph
from app.engine.context import RunContext
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {
        "id": nid, "type": ntype, "position": {"x": 0, "y": 0},
        "data": {"label": nid, "config": config},
    }


def text(nid, template, assign_to=None):
    cfg = {"mode": "template", "template": template}
    if assign_to:
        cfg["assign_to"] = assign_to
    return node(nid, "transform", **cfg)


async def _run(graph, *, timeout=20.0):
    run = await run_manager.start(graph=graph, input_payload={"question": "随便"})
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row and row.status in ("succeeded", "failed", "cancelled"):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run.id} 没有在 {timeout}s 内结束")


async def _starts(run_id: str) -> list[str]:
    async with SessionLocal() as session:
        rows = list((await session.execute(
            RunEvent.__table__.select().where(RunEvent.run_id == run_id)
            .order_by(RunEvent.seq)
        )).mappings())
    return [r["node_id"] for r in rows if r["type"] == "node.started" and r["node_id"]]


async def test_unequal_branches_join_once_with_both_results():
    """一长一短两条支路汇合：汇合节点只跑一次，而且拿到的是两边的最终结果。"""
    graph = {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            text("a1", "A", assign_to="a"),
            text("a2", "{{ vars.a }}!", assign_to="a"),
            text("b", "B", assign_to="b"),
            text("j", "{{ vars.a }}+{{ vars.b }}", assign_to="joined"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.joined }}"}]),
        ],
        "edges": [
            {"source": "start", "target": "a1"},
            {"source": "a1", "target": "a2"},
            {"source": "a2", "target": "j"},
            {"source": "start", "target": "b"},
            {"source": "b", "target": "j"},
            {"source": "j", "target": "out"},
        ],
    }
    run = await _run(graph)
    assert run.status == "succeeded", run.error

    starts = await _starts(run.id)
    assert starts.count("j") == 1, f"汇合节点跑了 {starts.count('j')} 次：{starts}"
    assert starts.count("out") == 1, f"下游跟着重跑了：{starts}"
    assert run.output["结果"] == "A!+B"


async def test_branch_merge_does_not_wait_for_the_road_not_taken():
    """分支之后的二选一汇合：没走的那条永远不会到，不能一直等它。"""
    graph = {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("br", "branch", cases=[{"key": "yes", "condition": "true"}]),
            text("x", "走了 yes", assign_to="road"),
            text("y", "走了 default", assign_to="road"),
            text("j", "汇合：{{ vars.road }}", assign_to="joined"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.joined }}"}]),
        ],
        "edges": [
            {"source": "start", "target": "br"},
            {"source": "br", "target": "x", "sourceHandle": "yes"},
            {"source": "br", "target": "y", "sourceHandle": "default"},
            {"source": "x", "target": "j"},
            {"source": "y", "target": "j"},
            {"source": "j", "target": "out"},
        ],
    }
    run = await _run(graph)
    assert run.status == "succeeded", run.error

    starts = await _starts(run.id)
    assert "y" not in starts
    assert starts.count("j") == 1, starts
    assert run.output["结果"] == "汇合：走了 yes"


async def test_join_still_runs_when_one_feeder_is_pruned_by_a_branch():
    """一条支路被分支整个剪掉：汇合节点照样要跑，下游不能被安静地跳过。"""
    graph = {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            text("c", "C", assign_to="c"),
            node("br", "branch", cases=[{"key": "yes", "condition": "false"}]),
            text("x", "X", assign_to="x"),
            text("j", "只有 {{ vars.c }}", assign_to="joined"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.joined }}"}]),
        ],
        "edges": [
            {"source": "start", "target": "c"},
            {"source": "start", "target": "br"},
            # default 出口没接线：分支判 default 就在这里结束，x 永远不会跑
            {"source": "br", "target": "x", "sourceHandle": "yes"},
            {"source": "c", "target": "j"},
            {"source": "x", "target": "j"},
            {"source": "j", "target": "out"},
        ],
    }
    run = await _run(graph)
    assert run.status == "succeeded", run.error

    starts = await _starts(run.id)
    assert "x" not in starts
    assert starts.count("j") == 1, starts
    assert run.output["结果"] == "只有 C"


async def test_join_inside_a_loop_runs_once_per_iteration():
    """循环体里的长短汇合：每一轮汇合一次，不多不少。"""
    graph = {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("lp", "loop", mode="foreach", items="[1, 2]", max_iterations=5),
            text("p", "第 {{ vars.item }} 轮"),
            text("q1", "长"),
            text("q2", "长长"),
            text("r", "短"),
            text("j", "合"),
            node("out", "output", fields=[{"name": "结果", "value": "完"}]),
        ],
        "edges": [
            {"source": "start", "target": "lp"},
            {"source": "lp", "target": "p", "sourceHandle": "body"},
            {"source": "p", "target": "q1"},
            {"source": "q1", "target": "q2"},
            {"source": "p", "target": "r"},
            {"source": "q2", "target": "j"},
            {"source": "r", "target": "j"},
            {"source": "j", "target": "lp"},
            {"source": "lp", "target": "out", "sourceHandle": "done"},
        ],
    }
    run = await _run(graph)
    assert run.status == "succeeded", run.error

    starts = await _starts(run.id)
    assert starts.count("p") == 2, starts
    assert starts.count("j") == 2, f"两轮应该各汇合一次：{starts}"


def test_loop_back_edge_does_not_make_the_loop_node_a_join():
    """循环节点的入边是"入口 + 回边"，这不是汇合，不该被推迟。

    推迟只会让它白等图里其他还在跑的支路；真正需要等的是有两条以上
    前向入边的节点。
    """
    graph = GraphSpec.model_validate({
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("lp", "loop", mode="foreach", items="[1]"),
            text("body", "x"),
            text("a", "a"),
            text("b", "b"),
            text("j", "j"),
            node("out", "output", fields=[]),
        ],
        "edges": [
            {"source": "start", "target": "lp"},
            {"source": "lp", "target": "body", "sourceHandle": "body"},
            {"source": "body", "target": "lp"},
            {"source": "lp", "target": "a", "sourceHandle": "done"},
            {"source": "lp", "target": "b", "sourceHandle": "done"},
            {"source": "a", "target": "j"},
            {"source": "b", "target": "j"},
            {"source": "j", "target": "out"},
        ],
    })
    builder = compile_graph(graph, RunContext(run_id="t", thread_id="t", spec=graph))
    deferred = {nid for nid, spec in builder.nodes.items() if spec.defer}
    assert deferred == {"j"}
