"""子工作流里那一步结束了运行时，各处写同一个名字（「子工作流 › x」，终态事件上记的 label）：apps/resolve.end_label、
定时任务的 Webhook（scheduler/webhook._failed_node）、发布单的「最近一次试运行在「…」被驳回」（blueprints/publish_plan）。
图上找得到的节点照旧用图上的名字。运行和图都是虚构的。
"""
from __future__ import annotations

from app.apps.resolve import end_label
from app.blueprints.publish_plan import _node_title
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.scheduler.webhook import _failed_node

GRAPH = {"nodes": [{"id": "sub", "type": "subgraph", "data": {"label": "核对子流程", "config": {}}}], "edges": []}


async def _run(error_node: str, label: str | None) -> Run:
    async with SessionLocal() as session:
        run = Run(graph=GRAPH, status="failed", error_node_id=error_node, run_class="exploratory")
        session.add(run)
        await session.flush()
        data = {"node_id": error_node, **({"label": label} if label else {})}
        session.add(RunEvent(run_id=run.id, seq=1, type="run.failed", node_id=error_node, ts=0.0, data=data))
        await session.commit()
        return run


async def test_a_step_inside_a_subworkflow_is_named_by_the_end_event_everywhere():
    run = await _run("sub/fetch", "核对子流程 › 取数")
    async with SessionLocal() as session:
        said = await end_label(session, run)
    assert said == "核对子流程 › 取数"
    assert _failed_node(run, said) == "核对子流程 › 取数", "Webhook 不再在找不到节点时什么都不写"
    assert _node_title(run.graph, run.error_node_id, said) == "核对子流程 › 取数", "发布单不再写内部 id「sub/fetch」"


async def test_nodes_on_the_graph_keep_their_graph_names_and_missing_labels_fall_back():
    run = await _run("sub", "别的说法")
    async with SessionLocal() as session:
        said = await end_label(session, run)
    assert _failed_node(run, said) == "核对子流程" and _node_title(run.graph, "sub", said) == "核对子流程"
    bare = await _run("sub/x", None)
    async with SessionLocal() as session:
        assert await end_label(session, bare) is None
    assert _failed_node(bare, None) is None and _node_title(bare.graph, "sub/x", None) == "sub/x"
