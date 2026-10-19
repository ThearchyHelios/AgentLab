"""驳回出口之后，待办、通知、定时任务的 Webhook 照实说「被驳回，转到 X」（docs/design/orchestration-round/00-总方案.md §3）。

实例页已经这么说（test_reject_exit.test_the_instance_says_rejected_and_where_it_went）。这里对的是另外几处：
- 待办：中断内容带着驳回之后去哪（reject_route）：接了驳回出口的写转到哪几步，没接的写运行就此结束，审批模式没接、没设
  驳回即终止的写这一路到此为止。待办卡的提示、驳回后的提示、处理过的结果照它说，不再一律写「转入驳回分支」「本次运行终止」；
- 子工作流里的关卡走了驳回出口：父运行的终态事件照实带上（「子工作流 › 把关」被驳回，转到「子工作流 › 退回」），实例页、
  通知说这一句，不写内部 id、不写成「已完成」；
- 定时任务的 Webhook：途经驳回、照驳回那条路成功结束的，标题和状态写途经驳回，另起一行写谁被驳回、转到哪一步。
图和数据都是虚构的。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Task
from app.engine.runner import run_manager
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


START = node("start", "input", fields=[{"name": "q"}])
DONE = node("done", "output", "办结", fields=[{"name": "结果", "value": "通过了"}])
BACK = node("back", "output", "退回申请人", fields=[{"name": "结果", "value": "退回：{{ nodes.gate.note }}"}])


def gated(gate: dict, *, exit_to: dict | None = BACK) -> dict:
    normal = "approved" if gate["data"]["config"].get("mode") == "approve" else "out"
    nodes = [START, gate, DONE] + ([exit_to] if exit_to else [])
    edges = [{"source": "start", "target": "gate"}, {"source": "gate", "target": "done", "sourceHandle": normal}]
    if exit_to:
        edges.append({"source": "gate", "target": exit_to["id"], "sourceHandle": "rejected"})
    return {"nodes": nodes, "edges": edges}


async def _wait(run_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row is not None and row.status in statuses and not run_manager.is_active(run_id):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"运行 {run_id} 没有进入 {statuses}")


async def _parked(graph: dict) -> tuple[Run, dict]:
    """发起、停在关卡上，交回运行和那条待办的中断内容。"""
    run = await run_manager.start(graph=graph, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    async with SessionLocal() as session:
        [task] = list((await session.execute(select(Task).where(Task.run_id == run.id))).scalars())
        from app.db.models import Approval

        approval = await session.get(Approval, task.approval_id)
    return run, dict(approval.payload or {})


APPROVE = node("gate", "human", "把关", mode="approve", title="可以吗")
INPUT = node("gate", "human", "补充", mode="input", title="补一下")
TOOL = node("gate", "tool", "算一下", tool="calculator", args={"expression": "1+1"}, approval="always")


@pytest.mark.parametrize("gate", [APPROVE, INPUT, TOOL], ids=["approve", "input", "tool"])
async def test_the_task_says_where_a_rejection_goes(gate):
    _, payload = await _parked(gated(gate))
    assert payload["reject_route"] == {"to": [{"node_id": "back", "label": "退回申请人"}]}


@pytest.mark.parametrize("gate", [INPUT, TOOL], ids=["input", "tool"])
async def test_without_the_exit_the_task_says_the_run_ends(gate):
    _, payload = await _parked(gated(gate, exit_to=None))
    assert payload["reject_route"] == {"ends": "run"}


async def test_an_approval_without_the_exit_says_this_path_stops_here():
    _, payload = await _parked(gated(APPROVE, exit_to=None))
    assert payload["reject_route"] == {"ends": "path"}
    stop = node("gate", "human", "把关", mode="approve", title="可以吗", stop_on_reject=True)
    _, payload = await _parked(gated(stop))
    assert payload["reject_route"] == {"ends": "run"}, "驳回即终止优先：驳回出口那条线走不到"


async def test_an_agent_tool_approval_has_no_route():
    """agent 自己调工具的审批不是关卡：驳回了 agent 照常往下想，中断内容不带 reject_route。"""
    from app.engine.replay import reject_route
    from app.engine.schema import GraphSpec

    spec = GraphSpec.model_validate(gated(node("gate", "agent", "助手", prompt="x")))

    class Ctx:
        class run:  # noqa: N801 - 只要 spec 和 global_id
            pass

    ctx = Ctx()
    ctx.node = spec.node_map()["gate"]
    ctx.run.spec = spec
    assert reject_route(ctx) is None


# --------------------------------------------------------------------------
# 子工作流里的关卡走了驳回出口
# --------------------------------------------------------------------------


async def _sub_rejected() -> Run:
    inner = {"nodes": [node("s", "input"), node("gate", "human", "把关", mode="approve", title="可以吗"),
                       node("ok", "output", "通过", fields=[{"name": "r", "value": "ok"}]),
                       node("back", "output", "退回", fields=[{"name": "r", "value": "退回：{{ nodes.gate.note }}"}])],
             "edges": [{"source": "s", "target": "gate"},
                       {"source": "gate", "target": "ok", "sourceHandle": "approved"},
                       {"source": "gate", "target": "back", "sourceHandle": "rejected"}]}
    sub_id = await S.publish("复核子流程", graph=inner, level="draft")
    graph = S.chain(node("start", "input"), node("sub", "subgraph", "复核子流程", workflow_id=sub_id, workflow_version=1),
                    node("out", "output", "成果", fields=[{"name": "r", "value": "{{ nodes.sub.output.r }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    await run_manager.resume(run.id, {"approved": False, "note": "不对"}, actor="张工")
    return await _wait(run.id, ("cancelled", "failed", "succeeded"))


async def test_a_reject_exit_inside_a_subworkflow_is_named_in_the_parent():
    from app.apps.resolve import _trails, instance_state

    final = await _sub_rejected()
    assert final.status == "succeeded", final.error
    assert final.output == {"r": "退回：不对"}
    async with SessionLocal() as session:
        [ended] = list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == final.id, RunEvent.type == "run.finished"))).scalars())
        run = await session.get(Run, final.id)
        state = instance_state(run, (await _trails(session, [run]))[run.id])
    [via] = ended.data["rejected_via"]
    assert (via["node_id"], via["label"]) == ("sub/gate", "复核子流程 › 把关")
    assert via["to"] == [{"node_id": "sub/back", "label": "复核子流程 › 退回"}]
    # 实例页、通知（notify_run 取的就是这一句）：照实说被驳回、转到哪，不写「sub/gate」，不画成已完成
    assert state == {"code": "cancelled", "text": "「复核子流程 › 把关」被驳回，转到「复核子流程 › 退回」"}


async def test_the_inner_task_names_the_route_with_the_subworkflow():
    inner = {"nodes": [node("s", "input"), node("gate", "human", "补充", mode="input", title="补一下"),
                       node("back", "output", "退回", fields=[{"name": "r", "value": "x"}]),
                       node("ok", "output", "通过", fields=[{"name": "r", "value": "y"}])],
             "edges": [{"source": "s", "target": "gate"}, {"source": "gate", "target": "ok"},
                       {"source": "gate", "target": "back", "sourceHandle": "rejected"}]}
    sub_id = await S.publish("补充子流程", graph=inner, level="draft")
    graph = S.chain(node("start", "input"), node("sub", "subgraph", "补充子流程", workflow_id=sub_id, workflow_version=1),
                    node("out", "output", fields=[{"name": "r", "value": "{{ nodes.sub.output.r }}"}]))
    _, payload = await _parked(graph)
    assert payload["reject_route"] == {"to": [{"node_id": "sub/back", "label": "补充子流程 › 退回"}]}


# --------------------------------------------------------------------------
# 定时任务的 Webhook
# --------------------------------------------------------------------------


def test_the_webhook_says_a_run_went_through_a_rejection():
    from app.scheduler.webhook import Notice, message

    via = [{"node_id": "gate", "label": "把关", "kind": "approve", "decision": "reject", "actor": "张工", "note": "不对",
            "to": [{"node_id": "back", "label": "退回申请人"}]}]
    title, lines, _ = message(Notice(event="finished", schedule_id="s1", schedule_name="周报", timezone="UTC",
                                     status="succeeded", rejected_via=via))
    assert "途经驳回" in title
    said = dict(lines)
    assert said["状态"] == "已完成（途经驳回）"
    assert said["驳回"] == "「把关」被驳回，转到「退回申请人」"
    # 没途经驳回的照旧
    title, lines, _ = message(Notice(event="finished", schedule_id="s1", schedule_name="周报", timezone="UTC",
                                     status="succeeded"))
    assert title == "定时任务「周报」运行完成" and dict(lines)["状态"] == "已完成" and "驳回" not in dict(lines)


async def test_the_webhook_notice_reads_the_route_from_the_end_event():
    from app.scheduler.webhook import _rejected_via_of

    final = await _sub_rejected()
    async with SessionLocal() as session:
        run = await session.get(Run, final.id)
        via = await _rejected_via_of(session, run)
    assert [v["label"] for v in via] == ["复核子流程 › 把关"]
