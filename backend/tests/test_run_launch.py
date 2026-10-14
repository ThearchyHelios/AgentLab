"""发起运行的规则搬到 engine/launch.py：HTTP 发起和定时任务到点发起共用同一个 launch_run。

- 每一种拒绝：POST /api/runs 的状态码、detail、code 先按搬家之前的原话写死（这一组先在旧代码上通过）；
  直接调 launch_run 抛 LaunchRefused，和 HTTP 的返回逐条一致
- 发起时定下的治理事件（受管级别那条日志、口径升版处置）在服务里写：不经 HTTP 发起的正式运行一样有；
  它们和运行记录同一个事务提交，排在 run.started 前面、在封存范围里，写不进去就不建运行
- 旧的导入路径（app.api.runs 的 missing_tools、node_setting、tool_bindings、TOOL_MISSING）照旧可用；
  runner 不再从接口层导入
"""
from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec
from app.main import app


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


SIMPLE = chain(node("start", "input"), node("out", "output", fields=[{"name": "r", "value": "完成"}]))


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def workflow(graph=SIMPLE, *, published=None, versions=(), snapshot=None, name=None) -> tuple[str, str]:
    """建一个工作流。published 是当前发布版本号，versions 是要建快照的版本号，snapshot 是快照里的图（默认同 graph）。"""
    name = name or f"发起测试·周报-{_tag()}"
    async with SessionLocal() as session:
        wf = Workflow(name=name, graph=graph, status="published" if published else "draft",
                      published_version=published, version=max(versions, default=1))
        session.add(wf)
        await session.flush()
        for v in versions:
            session.add(WorkflowVersion(workflow_id=wf.id, version=v,
                                        graph=snapshot if snapshot is not None else graph))
        await session.commit()
        return wf.id, name


def tool_graph(*tools: tuple[str, str]) -> dict:
    """每个 (节点名, 工具名) 一个调用工具节点。"""
    middle = [node(f"t{i}", "tool", label, tool=tool, args={}) for i, (label, tool) in enumerate(tools)]
    return chain(node("start", "input"), *middle, node("out", "output", fields=[{"name": "r", "value": "完成"}]))


# --------------------------------------------------------------------------
# 每一种拒绝：(请求体, (状态码, detail, code))。detail 是 None 时只比两边一致，另由 check 查要点
# --------------------------------------------------------------------------


async def case_formal_without_workflow():
    return {"run_class": "formal"}, (400, "正式运行需要从已保存的工作流发起：请求中没有指定工作流", None)


async def case_formal_with_canvas_graph():
    wf, _ = await workflow(published=1, versions=(1,))
    return ({"run_class": "formal", "workflow_id": wf, "graph": SIMPLE},
            (400, "正式运行只能使用已发布的版本，画布上未发布的修改不会生效。请先保存并发布", None))


async def case_formal_workflow_gone():
    return {"run_class": "formal", "workflow_id": f"gone{_tag()}"}, (404, "工作流不存在，可能已被删除", None)


async def case_formal_never_published():
    wf, name = await workflow()
    return {"run_class": "formal", "workflow_id": wf}, (409, f"「{name}」还没有发布版本，请先在编排页发布", None)


async def case_formal_not_the_published_version():
    """v1 是草稿快照（没有发布级别，也不是当前发布版）。应用平台之后正式运行接受「已发布且未退役」的版本，草稿照旧不行，
    拒绝带上机器码 run_version_unpublished（F1）。"""
    wf, _ = await workflow(published=2, versions=(1, 2))
    return ({"run_class": "formal", "workflow_id": wf, "version": 1},
            (409, "v1 没有发布过（当前发布版本为 v2）。正式运行只能使用已发布的版本；如需运行这一版，请先发布它。",
             "run_version_unpublished"))


async def case_formal_snapshot_missing():
    wf, _ = await workflow(published=3, versions=(1, 2))
    return {"run_class": "formal", "workflow_id": wf}, (404, "版本 v3 不存在", None)


async def case_formal_snapshot_malformed():
    """快照里的图不成形：以前由 main.py 的 ValueError 兜底回 400，detail 是校验的原话。"""
    broken = {"nodes": "坏掉的图"}
    wf, _ = await workflow(published=1, versions=(1,), snapshot=broken)
    with pytest.raises(ValueError) as caught:
        GraphSpec.model_validate(broken)
    return {"run_class": "formal", "workflow_id": wf}, (400, str(caught.value), None)


def library(version="v3"):
    return chain(node("start", "input"),
                 node("caliber", "metrics", "周报口径卡", caliber="周报口径", caliber_version=version,
                      metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "vars.kpi.gmv"}]),
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.caliber.text }}"}]))


def weekly(lib_id: str, **card) -> dict:
    return chain(node("start", "input"),
                 node("fetch", "transform", mode="expression", expression="{'gmv': 4321.5}", assign_to="kpi"),
                 node("card", "metrics", "本地口径卡",
                      caliber_from={"workflow_id": lib_id, "workflow_version": 1, "node_id": "caliber"}, **card),
                 node("out", "output", fields=[{"name": "r", "value": "{{ nodes.card.text }}"}]))


async def upstream_with_two_versions() -> str:
    async with SessionLocal() as session:
        lib = Workflow(name=f"口径库-{_tag()}", graph=library(), status="published", published_version=2, version=2)
        session.add(lib)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=lib.id, version=1, graph=library()))
        session.add(WorkflowVersion(workflow_id=lib.id, version=2, graph=library(version="v4")))
        await session.commit()
        return lib.id


async def case_formal_caliber_upgrade_undeclared():
    lib = await upstream_with_two_versions()
    graph = weekly(lib)
    wf, _ = await workflow(graph, published=1, versions=(1,))
    return {"run_class": "formal", "workflow_id": wf}, (409, None, None)


def check_formal_caliber_upgrade_undeclared(detail: str) -> None:
    assert "「本地口径卡」" in detail and "v1" in detail and "v2" in detail and "「上游发布新版本时」" in detail


async def case_saved_workflow_gone():
    return {"workflow_id": f"gone{_tag()}"}, (404, "工作流不存在，可能已被删除", None)


async def case_nothing_to_run():
    return {}, (400, "没有要运行的内容：请求中既没有工作流 ID，也没有工作流内容", None)


async def case_saved_workflow_is_empty():
    wf, _ = await workflow({})
    return {"workflow_id": wf}, (400, "没有要运行的内容：请求中既没有工作流 ID，也没有工作流内容", None)


async def case_missing_datasource_tool():
    source = f"gone{_tag()}"
    return ({"graph": tool_graph(("取数", f"db_query__{source}"))},
            (422, f"绑定的工具不存在：「取数」（调用工具）绑定的 db_query__{source}。请到「数据」页接入，或在节点中重新选择",
             "run_tool_missing"))


async def case_missing_custom_tool():
    tool = f"ghost_{_tag()}"
    return ({"graph": tool_graph(("查编码", tool))},
            (422, f"绑定的工具不存在：「查编码」（调用工具）绑定的 {tool}。请到「工具」页接入，或在节点中重新选择",
             "run_tool_missing"))


async def case_missing_tools_of_both_kinds():
    source, tool = f"gone{_tag()}", f"ghost_{_tag()}"
    return ({"graph": tool_graph(("取数", f"db_schema__{source}"), ("查编码", tool))},
            (422, f"绑定的工具不存在：「取数」（调用工具）绑定的 db_schema__{source}；「查编码」（调用工具）绑定的 {tool}。"
                  "数据源工具请到「数据」页接入，其他工具请到「工具」页接入，或在节点中重新选择", "run_tool_missing"))


async def case_more_than_eight_missing_tools():
    tag = _tag()
    tools = [(f"第{i}步", f"ghost_{tag}_{i}") for i in range(10)]
    listed = "；".join(f"「{label}」（调用工具）绑定的 {tool}" for label, tool in tools[:8])
    return ({"graph": tool_graph(*tools)},
            (422, f"绑定的工具不存在：{listed}等 10 处。请到「工具」页接入，或在节点中重新选择", "run_tool_missing"))


async def case_formal_missing_tool():
    source = f"gone{_tag()}"
    wf, _ = await workflow(tool_graph(("取数", f"db_query__{source}")), published=1, versions=(1,))
    return ({"run_class": "formal", "workflow_id": wf},
            (422, f"绑定的工具不存在：「取数」（调用工具）绑定的 db_query__{source}。请到「数据」页接入，或在节点中重新选择",
             "run_tool_missing"))


async def case_graph_fails_validation():
    return ({"graph": {"nodes": [], "edges": []}},
            (400, "工作流未通过校验，请先在画布上修正标红的节点再运行：画布上还没有节点，请先从节点库拖入一个节点", None))


#: 「不存在」的工作流、数据源、自定义工具都带随机后缀，库里不会恰好有同名的
CASES = {name[len("case_"):]: fn for name, fn in dict(globals()).items() if name.startswith("case_")}


def _body(status: int, detail: str, code: str | None) -> dict:
    return {"detail": detail, **({"code": code} if code else {})}


async def _run_count() -> int:
    async with SessionLocal() as session:
        return len((await session.execute(select(Run.id))).all())


@pytest.mark.parametrize("case", list(CASES))
async def test_http_refusals_are_unchanged(client, case):
    """HTTP 这一侧逐字写死：搬家前后都得是这些状态码和原话。"""
    body, (status, detail, code) = await CASES[case]()
    before = await _run_count()
    res = await client.post("/api/runs", json=body)
    assert res.status_code == status, res.text
    got = res.json()
    if detail is None:
        globals()[f"check_{case}"](got["detail"])
        assert set(got) == ({"detail", "code"} if code else {"detail"}), got
    else:
        assert got == _body(status, detail, code)
    assert await _run_count() == before, "被拒的请求不该留下运行记录"


def _launch_kwargs(body: dict) -> dict:
    """请求体换成 launch_run 的参数：input 对应 input_payload，其余同名。"""
    out = {k: v for k, v in body.items() if k != "input"}
    if "input" in body:
        out["input_payload"] = body["input"]
    return out


@pytest.mark.parametrize("case", list(CASES))
async def test_launch_run_refuses_exactly_like_http(client, case):
    from app.engine.launch import LaunchRefused, launch_run

    body, (status, detail, code) = await CASES[case]()
    before = await _run_count()
    async with SessionLocal() as session:
        with pytest.raises(LaunchRefused) as caught:
            await launch_run(session, actor="定时任务", **_launch_kwargs(body))
    refused = caught.value
    assert (refused.status_code, refused.code) == (status, code)
    if detail is not None:
        assert refused.message == detail
    res = await client.post("/api/runs", json=body)
    assert res.status_code == refused.status_code
    assert res.json() == _body(refused.status_code, refused.message, refused.code)
    assert await _run_count() == before


# --------------------------------------------------------------------------
# 发起成功：参数照转、治理事件在服务里写
# --------------------------------------------------------------------------


async def _settle(run_id: str) -> Run:
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed", "cancelled") and row.manifest_seq is not None:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 没有结束：{row.status}")


async def _events(run_id: str, etype: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype).order_by(RunEvent.seq)
        )).scalars())


async def test_a_formal_launch_records_the_governance_notes():
    """不经 HTTP 发起的正式运行（定时任务）也记下按什么级别出具、口径升版怎么处置的。"""
    from app.engine.launch import launch_run

    lib = await upstream_with_two_versions()
    wf, name = await workflow(weekly(lib, upgrade_policy="dual"), published=1, versions=(1,))
    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=wf, run_class="formal", input_payload={"week": "第 40 周"},
                               actor="定时任务")
    assert (run.run_class, run.version, run.workflow_name, run.started_by) == ("formal", 1, name, "定时任务")
    assert run.version_hash and run.input == {"week": "第 40 周"}
    row = await _settle(run.id)
    assert row.status == "succeeded", row.error
    notes = [e for e in await _events(run.id, "log") if e.data.get("code") == "run_governance"]
    assert len(notes) == 1 and notes[0].data["governed"] is False and "v1" in notes[0].data["message"]
    [upgrade] = await _events(run.id, "caliber.upgrade")
    assert upgrade.node_id == "card" and upgrade.data["policy"] == "dual" and upgrade.data["latest"] == 2
    # 发起时就有的事件排在 run.started 前面，都在封存范围里
    [started] = await _events(run.id, "run.started")
    assert notes[0].seq < upgrade.seq < started.seq <= row.manifest_seq


async def test_launch_events_are_written_with_the_run_or_not_at_all():
    """发起时定下的事件和运行记录同一个事务提交：写不进去就不建运行，launch_run 抛出异常就是没有发起。以前运行
    建好、开始执行之后才另外写，写失败时运行已经在执行，调用方（定时任务）只拿到一个异常，会把一次正在执行的
    正式运行记成未能发起，封存范围里也少了受管级别那条日志。"""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.db.base import engine
    from app.engine.launch import launch_run

    lib = await upstream_with_two_versions()
    wf, _ = await workflow(weekly(lib, upgrade_policy="dual"), published=1, versions=(1,))
    before = await _run_count()
    # 口径升版处置那条写不进去。走异步引擎建删触发器：同步连接等写锁时会卡住事件循环，而持锁的正是这个循环里的写入
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TRIGGER refuse_upgrade_note BEFORE INSERT ON run_events WHEN NEW.type = 'caliber.upgrade' "
            "BEGIN SELECT RAISE(ABORT, 'refused by test'); END"))
    try:
        async with SessionLocal() as session:
            with pytest.raises(IntegrityError):
                await launch_run(session, workflow_id=wf, run_class="formal", actor="定时任务")
        assert await _run_count() == before, "没写进治理事件，却留下了一条运行"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER IF EXISTS refuse_upgrade_note"))


async def test_an_exploratory_launch_takes_the_saved_graph_and_the_run_defaults():
    from app.engine.launch import launch_run

    wf, name = await workflow()
    async with SessionLocal() as session:
        run = await launch_run(session, workflow_id=wf, actor=None)
    assert (run.run_class, run.workflow_id, run.workflow_name, run.version) == ("exploratory", wf, name, None)
    assert (run.memory_scope, run.collection) == ("default", "default") and run.input == {}
    row = await _settle(run.id)
    assert row.status == "succeeded", row.error
    assert [e for e in await _events(run.id, "log") if e.data.get("code") == "run_governance"] == []


# --------------------------------------------------------------------------
# 旧的导入路径
# --------------------------------------------------------------------------


def test_the_old_import_paths_still_work():
    from app.api import runs as runs_api
    from app.engine import launch

    for name in ("missing_tools", "node_setting", "tool_bindings", "TOOL_MISSING"):
        assert getattr(runs_api, name) is getattr(launch, name), name


def test_the_runner_no_longer_imports_from_the_api_layer():
    from app.engine import runner

    assert "app.api.runs" not in inspect.getsource(runner)
