"""驳回出口和数据事件触发接在一起（docs/design/orchestration-round/00-总方案.md §3、§4；合并时接上的那一下：runner._finalize
把 _rejected_via(state) 交给 outbox.stage_run_finished）。

一次正式运行在审批那一步被驳回、走驳回出口跑完（状态 succeeded）：写下的 workflow.finished 带上途经了哪个关卡的驳回，
缺省不认途经驳回的下游不派；同一张图批准走完的那次，事件里照实说没途经驳回。工作流是虚构的。
"""
from __future__ import annotations

import uuid

from app.db.base import SessionLocal
from app.db.models import Workflow, WorkflowVersion
from app.engine.launch import launch_run
from app.engine.runner import run_manager
from tests.test_reject_exit import GATES, _wait, gated
from tests.test_schedule_events import engine_up, events, settle  # noqa: F401  （engine_up：清掉定时任务和事件）


async def _published(graph: dict) -> str:
    async with SessionLocal() as session:
        wf = Workflow(name=f"报销审批-{uuid.uuid4().hex[:6]}", graph=graph, status="published", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph))
        await session.commit()
        return wf.id


async def _formal_answered(workflow_id: str, reply: dict):
    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=workflow_id, run_class="formal", input_payload={"q": "x"},
                               actor="检查员", origin_kind="launcher")
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    await run_manager.resume(run.id, reply, actor="张工")
    return await settle(run.id)


async def test_a_formal_run_that_went_through_the_reject_exit_stages_an_event_marked_rejected():
    gate, _ = GATES["approve"]
    wf = await _published(gated(gate))
    final = await _formal_answered(wf, {"approved": False, "note": "不对"})
    assert final.status == "succeeded" and final.output["结果"] == "退回：不对"
    [ev] = await events("workflow.finished", wf)
    assert ev.payload["rejected"] is True
    assert [(v["node_id"], v["label"], v["decision"]) for v in ev.payload["rejected_via"]] == [("gate", "把关", "reject")]


async def test_the_same_workflow_approved_stages_an_event_not_marked_rejected():
    gate, _ = GATES["approve"]
    wf = await _published(gated(gate))
    final = await _formal_answered(wf, {"approved": True})
    assert final.status == "succeeded" and final.output == {"结果": "通过了"}
    [ev] = await events("workflow.finished", wf)
    assert ev.payload["rejected"] is False and not ev.payload.get("rejected_via")
