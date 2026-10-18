"""驳回出口（docs/design/orchestration-round/00-总方案.md §3）。

审批、补充输入、编辑稿件、工具调用审批、代码执行审批（动作确认、双盲裁决在 test_process_actions.py、test_dual_check_node.py
里，要假的外部系统、口径卡）在画布上多一个可选的「驳回」出口（sourceHandle="rejected"）：
- 接了：驳回（含超时驳回）时这一步正常结束、产出 {rejected, decision, actor, note, kind, text}，运行沿驳回那条边往下走；
  node.finished 带 outcome=rejected（哪种关卡、结论、谁、备注、转到哪几步），运行照它的路径结束，终态事件照实带
  rejected_via；批准了照旧走普通出口；
- 没接：行为一字不变（cancelled + end_reason=rejected，test_run_rejected.py 照过）；审批没设驳回即终止的老规矩照旧；
- 「失败重试」「出错时继续」照旧不管驳回；
- 发布检查：驳回出口那条路走不到出口或结束（只能绕回去）的拦下；
- 实例进度写「被驳回，转到 X」。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal, utcnow
from app.db.models import Run, RunEvent, Task
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


START = node("start", "input", fields=[{"name": "q"}])
DONE = node("done", "output", "办结", fields=[{"name": "结果", "value": "通过了"}])
BACK = node("back", "output", "退回申请人", fields=[{"name": "结果", "value": "退回：{{ nodes.gate.note }}"},
                                                  {"name": "谁", "value": "{{ nodes.gate.actor }}"}])

#: (关卡, 驳回之后的原话)
GATES = {
    "tool": (node("gate", "tool", label="算一下", tool="calculator", args={"expression": "1+1"}, approval="always"),
             "工具 calculator 的调用已被驳回：不对"),
    "code": (node("gate", "code", label="跑代码", code="print(1)", language="python", approval="always"),
             "代码执行已被驳回：不对"),
    "approve": (node("gate", "human", label="把关", mode="approve", title="可以吗"), "人工驳回：不对"),
    "edit": (node("gate", "human", label="改稿", mode="edit", title="改一下", draft="初稿"), "人工驳回：不对"),
    "input": (node("gate", "human", label="补充", mode="input", title="补一下"), "人工驳回：不对"),
}


def gated(gate: dict, *, exit_to: dict | None = BACK, normal_handle: str | None = None) -> dict:
    """入口 → 关卡 → 办结；关卡的「驳回」出口接到 exit_to。审批模式的普通出口是 approved，别的是 out。"""
    normal = normal_handle or ("approved" if gate["data"]["config"].get("mode") == "approve" else "out")
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


async def _events(run_id: str, kind: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        query = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        if kind:
            query = query.where(RunEvent.type == kind)
        return list((await session.execute(query)).scalars())


async def _answered(graph: dict, reply: dict) -> Run:
    run = await run_manager.start(graph=graph, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    await run_manager.resume(run.id, reply, actor="张工")
    return await _wait(run.id, ("cancelled", "failed", "succeeded"))


@pytest.mark.parametrize("kind", list(GATES))
async def test_a_rejection_takes_the_reject_exit_and_the_run_finishes_along_it(kind):
    gate, said = GATES[kind]
    final = await _answered(gated(gate), {"approved": False, "note": "不对"})
    # 接了驳回出口：运行没有被驳回结束，照驳回那条路走完
    assert (final.status, final.end_reason, final.error) == ("succeeded", None, None)
    assert final.output == {"结果": "退回：不对", "谁": "张工"}
    finished = {e.node_id: e for e in await _events(final.id, "node.finished")}
    assert "back" in finished and "done" not in finished
    assert not [e for e in await _events(final.id, "node.failed") if e.node_id == "gate"], "被驳回走出口不是失败"
    data = finished["gate"].data
    assert {k: data.get(k) for k in ("outcome", "kind", "decision", "actor", "note")} == {
        "outcome": "rejected", "kind": kind, "decision": "reject", "actor": "张工", "note": "不对"}
    assert data["to"] == [{"node_id": "back", "label": "退回申请人"}]
    assert data["preview"]["__decision__"] == "rejected" and data["preview"]["rejected"] is True
    # 这一步的产出：哪种关卡、结论、谁、备注、原话
    async with SessionLocal() as session:
        row = await session.get(Run, final.id)
    # 运行照路径结束，终态事件照实说途经驳回
    [ended] = await _events(final.id, "run.finished")
    assert ended.data["rejected_via"] == [{"node_id": "gate", "label": gate["data"]["label"], "kind": kind,
                                           "decision": "reject", "actor": "张工", "note": "不对",
                                           "to": [{"node_id": "back", "label": "退回申请人"}]}]
    assert row.manifest_seq == ended.seq
    from app.core.artifact_store import load

    payload = load(data["artifact"])
    assert {k: payload.get(k) for k in ("rejected", "decision", "actor", "note", "kind", "text")} == {
        "rejected": True, "decision": "reject", "actor": "张工", "note": "不对", "kind": kind, "text": said}


@pytest.mark.parametrize("kind", list(GATES))
async def test_approving_takes_the_normal_exit(kind):
    gate, _ = GATES[kind]
    final = await _answered(gated(gate), {"approved": True, "value": "补好了"})
    assert final.status == "succeeded", final.error
    assert final.output == {"结果": "通过了"}
    finished = {e.node_id: e for e in await _events(final.id, "node.finished")}
    assert "done" in finished and "back" not in finished
    assert "outcome" not in finished["gate"].data
    [ended] = await _events(final.id, "run.finished")
    assert "rejected_via" not in ended.data, "没有途经驳回的运行，终态事件和以前一字不差"


async def test_a_plain_out_handle_without_a_name_is_the_normal_exit_too():
    """普通出口的连线没写 sourceHandle（老图、助手建的图）：照样是普通出口。"""
    gate, _ = GATES["input"]
    graph = gated(gate)
    graph["edges"][1].pop("sourceHandle")
    final = await _answered(graph, {"approved": True, "value": "补好了"})
    assert final.status == "succeeded" and final.output == {"结果": "通过了"}


async def test_without_the_exit_nothing_changes():
    """没接驳回出口：照旧被驳回结束（cancelled + end_reason=rejected、node.failed reason=rejected）。"""
    gate, said = GATES["edit"]
    final = await _answered(gated(gate, exit_to=None), {"approved": False, "note": "不对"})
    assert (final.status, final.end_reason, final.error) == ("cancelled", "rejected", said)
    [failed] = [e for e in await _events(final.id, "node.failed") if e.node_id == "gate"]
    assert failed.data["reason"] == "rejected"
    [cancelled] = await _events(final.id, "run.cancelled")
    assert "rejected_via" not in cancelled.data


async def test_an_approval_without_stop_on_reject_keeps_its_old_routing():
    """审批没设驳回即终止、也没接驳回边：照旧按 __decision__ 路由，驳回就结束在这里、运行成功，产出和以前一样。"""
    gate = node("gate", "human", label="把关", mode="approve", title="可以吗", assign_to="ok")
    final = await _answered(gated(gate, exit_to=None), {"approved": False, "note": "不对"})
    assert final.status == "succeeded" and final.output in ({}, None)
    finished = {e.node_id: e for e in await _events(final.id, "node.finished")}
    assert finished["gate"].data["preview"] == {"approved": False, "note": "不对", "__decision__": "rejected"}
    assert "outcome" not in finished["gate"].data and "done" not in finished


async def test_stop_on_reject_still_stops_even_with_a_reject_edge():
    """审批明说了「驳回即终止运行」：照旧收成被驳回，驳回边走不到（以前就是这样），画布校验另给一条提示。"""
    from app.engine.schema import validate_graph

    gate = node("gate", "human", label="把关", mode="approve", title="可以吗", stop_on_reject=True)
    final = await _answered(gated(gate), {"approved": False, "note": "不对"})
    assert (final.status, final.end_reason, final.error) == ("cancelled", "rejected", "人工驳回：不对")
    assert not [e for e in await _events(final.id, "node.finished") if e.node_id in ("gate", "back")]
    [hint] = [i for i in validate_graph(_spec(gated(gate))).issues if i.code == "human.stop_on_reject_exit"]
    assert hint.level == "warning" and hint.node_id == "gate" and hint.field == "stop_on_reject"


async def test_an_approval_with_a_reject_edge_keeps_approved_false_and_the_variable():
    """接了驳回边的审批（不管设没设驳回即终止）：产出照旧带 approved=false，「结果存为变量」照旧写 false。"""
    gate = node("gate", "human", label="把关", mode="approve", title="可以吗", assign_to="ok")
    back = node("back", "output", "退回申请人", fields=[{"name": "批了", "value": "{{ vars.ok }}"},
                                                      {"name": "approved", "value": "{{ nodes.gate.approved }}"}])
    final = await _answered(gated(gate, exit_to=back), {"approved": False, "note": "不对"})
    assert final.status == "succeeded" and final.output == {"批了": "False", "approved": "False"}
    [gate_done] = [e for e in await _events(final.id, "node.finished") if e.node_id == "gate"]
    assert gate_done.data["outcome"] == "rejected" and gate_done.data["kind"] == "approve"


async def test_a_timeout_rejection_takes_the_exit_too():
    from app.process import tasks

    gate = node("gate", "human", label="把关", mode="approve", title="可以吗", due="PT30M",
                on_timeout={"action": "reject"})
    run = await run_manager.start(graph=gated(gate), input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    ticked = await tasks.tick(utcnow() + timedelta(hours=1))
    async with SessionLocal() as session:
        mine = (await session.execute(select(Task.id).where(Task.run_id == run.id))).scalar_one()
    assert mine in ticked
    final = await _wait(run.id, ("cancelled", "failed", "succeeded"))
    assert final.status == "succeeded", final.error
    [gate_done] = [e for e in await _events(final.id, "node.finished") if e.node_id == "gate"]
    assert (gate_done.data["outcome"], gate_done.data["decision"], gate_done.data["actor"]) == (
        "rejected", "expired_reject", None)
    assert final.output["结果"].startswith("退回")


async def test_retries_and_continue_on_error_still_leave_rejections_alone():
    """「失败重试」「出错时继续」照旧不管驳回：接了出口的照出口走，没接的照旧被驳回结束（不重试、不吞掉）。"""
    gate, _ = GATES["tool"]
    patient = {**gate, "data": {**gate["data"], "config": {**gate["data"]["config"], "retries": 2,
                                                           "on_error": "continue"}}}
    final = await _answered(gated(patient, exit_to=None), {"approved": False, "note": "不对"})
    assert (final.status, final.end_reason) == ("cancelled", "rejected")
    assert not [e for e in await _events(final.id, "log") if (e.data or {}).get("code") == "node_retry"]
    final = await _answered(gated(patient), {"approved": False, "note": "不对"})
    assert final.status == "succeeded" and final.output["结果"] == "退回：不对"
    assert len(await _events(final.id, "human.requested")) == 1


async def test_the_reject_path_can_send_the_work_back_and_ask_again():
    """「退回修改」：驳回出口接回上游，改完再问一次。第二次批准了走普通出口；时间线上同一个关卡两次执行，第一次被驳回。"""
    draft = node("draft", "transform", "起草", mode="template", template="稿子")
    gate = node("gate", "human", label="把关", mode="approve", title="可以吗")
    graph = {"nodes": [START, draft, gate, DONE],
             "edges": [{"source": "start", "target": "draft"}, {"source": "draft", "target": "gate"},
                       {"source": "gate", "target": "done", "sourceHandle": "approved"},
                       {"source": "gate", "target": "draft", "sourceHandle": "rejected"}]}
    run = await run_manager.start(graph=graph, input_payload={"q": "x"})
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    await run_manager.resume(run.id, {"approved": False, "note": "重写"}, actor="张工")
    await _wait(run.id, ("interrupted",))
    await run_manager.wait_idle(run.id)
    await run_manager.resume(run.id, {"approved": True}, actor="张工")
    final = await _wait(run.id, ("cancelled", "failed", "succeeded"))
    assert final.status == "succeeded" and final.output == {"结果": "通过了"}
    outcomes = [e.data.get("outcome") for e in await _events(run.id, "node.finished") if e.node_id == "gate"]
    assert outcomes == ["rejected", None]
    [ended] = await _events(run.id, "run.finished")
    assert [v["to"] for v in ended.data["rejected_via"]] == [[{"node_id": "draft", "label": "起草"}]]


# --------------------------------------------------------------------------
# 画布校验、发布检查
# --------------------------------------------------------------------------


def _spec(graph: dict):
    from app.engine.schema import GraphSpec

    return GraphSpec.model_validate(graph)


def test_a_reject_exit_on_a_node_that_cannot_be_rejected_is_an_error():
    from app.engine.schema import validate_graph

    calc = node("gate", "transform", "整理", mode="template", template="x")
    graph = gated(calc)
    issues = [i for i in validate_graph(_spec(graph)).issues if i.code == "graph.reject_exit_invalid"]
    assert [(i.level, i.node_id) for i in issues] == [("error", "gate")]
    assert "驳回" in issues[0].message
    assert not [i for i in validate_graph(_spec(gated(GATES["tool"][0]))).issues
                if i.code == "graph.reject_exit_invalid"]


@pytest.mark.parametrize("level", ["published", "governed"])
def test_publishing_refuses_a_reject_path_that_never_reaches_an_end(level):
    from app.engine.governance import lint_for_publish

    # 驳回之后绕回关卡自己的上游，那里只有一条路又回到关卡：除了再驳回一次，走不到任何出口
    loop_a = node("a", "transform", "甲", mode="template", template="x")
    loop_b = node("b", "transform", "乙", mode="template", template="y")
    gate = node("gate", "human", label="把关", mode="approve", title="可以吗")
    graph = {"nodes": [START, gate, DONE, loop_a, loop_b],
             "edges": [{"source": "start", "target": "gate"},
                       {"source": "gate", "target": "done", "sourceHandle": "approved"},
                       {"source": "gate", "target": "a", "sourceHandle": "rejected"},
                       {"source": "a", "target": "b"}, {"source": "b", "target": "a"}]}
    issues = [i for i in lint_for_publish(_spec(graph), level=level).issues if i.code == "reject_exit.no_end"]
    assert [(i.level, i.node_id) for i in issues] == [("error", "gate")]
    assert "「把关」" in issues[0].message and "「甲」" in issues[0].message
    # 走得到出口、走得到结束（没有下游的节点）、退回上游再问一次（重新问还能批准）的都放过
    for exit_to, extra_edges in ((BACK, []), (node("note", "transform", "记一笔", mode="template", template="z"), []),):
        ok = gated(gate, exit_to=exit_to)
        ok["edges"] += extra_edges
        assert not [i for i in lint_for_publish(_spec(ok), level=level).issues if i.code == "reject_exit.no_end"]
    draft = node("draft", "transform", "起草", mode="template", template="稿子")
    again = {"nodes": [START, draft, gate, DONE],
             "edges": [{"source": "start", "target": "draft"}, {"source": "draft", "target": "gate"},
                       {"source": "gate", "target": "done", "sourceHandle": "approved"},
                       {"source": "gate", "target": "draft", "sourceHandle": "rejected"}]}
    assert not [i for i in lint_for_publish(_spec(again), level=level).issues if i.code == "reject_exit.no_end"]


def test_layout_orders_the_reject_exit_below_the_normal_one():
    from app.engine.layout import _handle_order

    spec = _spec(gated(GATES["tool"][0]))
    assert _handle_order(spec.node_map()["gate"], spec) == ["out", "rejected"]
    approve = _spec(gated(GATES["approve"][0]))
    assert _handle_order(approve.node_map()["gate"], approve) == ["approved", "rejected"]
    plain = _spec(gated(GATES["tool"][0], exit_to=None))
    assert _handle_order(plain.node_map()["gate"], plain) == ["out"]


# --------------------------------------------------------------------------
# 应用：实例进度、实例状态
# --------------------------------------------------------------------------


async def test_the_instance_says_rejected_and_where_it_went():
    from types import SimpleNamespace

    from app.apps.resolve import _timeline, _trails, instance_state

    gate, _ = GATES["tool"]

    async def said(final: Run) -> tuple[dict, dict]:
        async with SessionLocal() as session:
            run = await session.get(Run, final.id)
            steps = {s["node"]: s for s in (await _timeline(session, SimpleNamespace(instance=run)))["steps"]}
            return steps, instance_state(run, (await _trails(session, [run]))[run.id])

    # 驳回之后去通知一声就结束（只有一个成果节点）：实例写谁被驳回、转到了哪一步，是人的决定，不画成成功
    notify = node("back", "transform", "通知申请人", mode="template", template="退回了")
    steps, state = await said(await _answered(gated(gate, exit_to=notify), {"approved": False, "note": "不对"}))
    assert (steps["gate"]["state"], steps["gate"]["text"]) == ("rejected", "被驳回，转到「通知申请人」")
    assert state == {"code": "cancelled", "text": "「算一下」被驳回，转到「通知申请人」"}
    # 驳回出口走到另一个成果节点（流程的另一种结局）：照旧写走到的那个成果节点，记成人的决定
    steps, state = await said(await _answered(gated(gate), {"approved": False, "note": "不对"}))
    assert (steps["gate"]["state"], steps["gate"]["text"]) == ("rejected", "被驳回，转到「退回申请人」")
    assert state == {"code": "cancelled", "text": "退回申请人"}
    # 批准了的照旧是办完
    ok = await _answered(gated(gate), {"approved": True})
    async with SessionLocal() as session:
        run = await session.get(Run, ok.id)
        steps = {s["node"]: s for s in (await _timeline(session, SimpleNamespace(instance=run)))["steps"]}
        state = instance_state(run, (await _trails(session, [run]))[run.id])
    assert steps["gate"]["state"] == "done" and state == {"code": "succeeded", "text": "办结"}
