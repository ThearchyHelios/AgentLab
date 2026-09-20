"""失败的运行接着跑，而不是整张图从头来一遍。

库里 28 条 failed 的构成说明了为什么值得单独开这条路：模型 id 写错、401、
循环条件语法错、缺必填输入加起来占了大半——全是**配置错了**，改一下就能跑。
而"改一下再跑"原来意味着前面花两分钟查完的表结构、跑完的 SQL 全部重来，
尽管那些结果一直躺在 checkpoint 里。

这里最要紧的不是接口通不通，是两条：
1. 前面跑过的节点**真的没有重跑**（不然"接着跑"只是个说法）；
2. 结构变了就必须拒绝。checkpoint 是按节点名存的，拿一张改了结构的图去续，
   会拿着错位的通道状态跑出一份似是而非的结果——比直接报错糟得多。
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, topology_of
from app.main import app


@pytest.fixture(autouse=True)
async def engine_up():
    """这组测试真的跑图，需要 checkpointer——平时是 FastAPI 的 lifespan 建的。

    不开的话 continue_failed 连 aget_state 都调不了，而"能不能接着跑"整件事
    就建立在 checkpoint 上。
    """
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {
        "id": nid, "type": ntype, "position": {"x": 0, "y": 0},
        "data": {"label": nid, "config": config},
    }


def graph_with(*, condition: str):
    """input → 整形 → 循环（条件由参数给）→ 成果。

    循环条件写错会抛 NodeError，而它前面的整形节点已经跑完了——正好用来验
    "接着跑"到底有没有把前面那步保下来。库里真有 4 条运行是这么挂的。
    """
    return {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("prep", "transform", mode="template", template="备好了", assign_to="prepped"),
            node("lp", "loop", mode="while", condition=condition, max_iterations=1),
            node("body", "transform", mode="template", template="转一圈"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.prepped }}"}]),
        ],
        "edges": [
            {"source": "start", "target": "prep"},
            {"source": "prep", "target": "lp"},
            {"source": "lp", "target": "body", "sourceHandle": "body"},
            {"source": "body", "target": "lp"},
            {"source": "lp", "target": "out", "sourceHandle": "done"},
        ],
    }


BROKEN = "这不是表达式 >>> 啊"
FIXED = "false"


async def _run_until_done(graph, *, timeout=20.0):
    run = await run_manager.start(graph=graph, input_payload={"question": "随便"})
    await _wait(run.id, ("failed", "succeeded", "cancelled"), timeout)
    return run.id


async def _wait(run_id, statuses, timeout=20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row.status
        await asyncio.sleep(0.05)
    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
        raise AssertionError(f"等超时了，run {run_id} 还是 {row.status if row else '不存在'}")


async def _node_starts(run_id: str) -> list[str]:
    """这次运行里哪些节点真的开跑了。接着跑之后不该再出现前面那些。"""
    async with SessionLocal() as session:
        rows = list((await session.execute(
            RunEvent.__table__.select().where(RunEvent.run_id == run_id)
        )).mappings())
    return [r["node_id"] for r in rows if r["type"] == "node.started" and r["node_id"]]


def test_topology_ignores_config_but_not_structure():
    """改配置骨架不变，改结构就变——这是能不能续的唯一判据。"""
    a = GraphSpec.model_validate(graph_with(condition=BROKEN))
    b = GraphSpec.model_validate(graph_with(condition=FIXED))
    assert topology_of(a) == topology_of(b)

    moved = graph_with(condition=FIXED)
    moved["nodes"][1]["position"] = {"x": 500, "y": 900}
    assert topology_of(GraphSpec.model_validate(moved)) == topology_of(a)

    extra = graph_with(condition=FIXED)
    extra["nodes"].append(node("zzz", "transform", mode="template", template="多出来的"))
    assert topology_of(GraphSpec.model_validate(extra)) != topology_of(a)


@pytest.mark.asyncio
async def test_continue_skips_the_nodes_that_already_ran():
    """核心不变量：接着跑不重跑前面的节点。"""
    run_id = await _run_until_done(graph_with(condition=BROKEN))
    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).status == "failed"

    first_pass = await _node_starts(run_id)
    assert "prep" in first_pass, f"第一遍应该跑到整形节点：{first_pass}"
    before = len(first_pass)

    await run_manager.continue_failed(run_id, graph=graph_with(condition=FIXED))
    await _wait(run_id, ("succeeded", "failed"))

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
    assert run.status == "succeeded", run.error
    # 前面那步的结果还在，说明续的是同一条线程而不是重开了一张图
    assert run.output.get("结果") == "备好了"

    second_pass = (await _node_starts(run_id))[before:]
    assert "prep" not in second_pass, f"整形节点被重跑了：{second_pass}"
    assert "start" not in second_pass, f"输入节点被重跑了：{second_pass}"
    assert "out" in second_pass, f"应该跑完剩下的：{second_pass}"


@pytest.mark.asyncio
async def test_continue_refuses_a_restructured_graph():
    """结构变了就对不上。宁可报错，也不能跑出一份似是而非的结果。"""
    run_id = await _run_until_done(graph_with(condition=BROKEN))

    restructured = graph_with(condition=FIXED)
    restructured["nodes"].append(node("extra", "transform", mode="template", template="新加的"))
    restructured["edges"].append({"source": "prep", "target": "extra"})

    with pytest.raises(ValueError, match="不能增删节点"):
        await run_manager.continue_failed(run_id, graph=restructured)

    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).status == "failed"   # 没被动过


@pytest.mark.asyncio
async def test_continue_refuses_a_graph_that_fails_validation():
    """骨架没动，但改出来的配置本身就是非法的——也得拦。

    用出具契约缺 metrics_from：那是纯配置错误，结构一个字没改，所以它必定
    走过了拓扑那一关，考的就是后面那道校验。
    """
    run_id = await _run_until_done(graph_with(condition=BROKEN))
    bad = graph_with(condition=FIXED)
    bad["nodes"][4]["data"]["config"]["contract"] = {"narrative": "{{ vars.prepped }}"}
    assert topology_of(GraphSpec.model_validate(bad)) == topology_of(
        GraphSpec.model_validate(graph_with(condition=BROKEN))
    ), "这个用例的前提是骨架不变"

    with pytest.raises(ValueError, match="没有通过校验"):
        await run_manager.continue_failed(run_id, graph=bad)


@pytest.mark.asyncio
async def test_only_failed_runs_can_continue(client):
    """跑成功的接着跑没有意义——断点在终点，续下去等于从 START 重来一遍。"""
    run_id = await _run_until_done(graph_with(condition=FIXED))
    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).status == "succeeded"

    resp = await client.post(f"/api/runs/{run_id}/continue", json={})
    assert resp.status_code == 409
    assert "只有失败的运行" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_continue_unknown_run_is_404(client):
    resp = await client.post("/api/runs/没这个/continue", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_continue_through_the_api(client):
    """走接口跑一遍，并确认时间线上说清楚了是接着跑、保了几步。"""
    run_id = await _run_until_done(graph_with(condition=BROKEN))

    resp = await client.post(
        f"/api/runs/{run_id}/continue", json={"graph": graph_with(condition=FIXED)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    await _wait(run_id, ("succeeded", "failed"))

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        rows = list((await session.execute(
            RunEvent.__table__.select().where(RunEvent.run_id == run_id)
        )).mappings())
    assert run.status == "succeeded", run.error
    # 改过的图要落库，否则下次再看这次运行，图和实际跑的对不上
    assert run.graph["nodes"][2]["data"]["config"]["condition"] == FIXED

    resumed = [r for r in rows if r["type"] == "run.resumed"]
    assert resumed, "接着跑必须在时间线上留下痕迹"
    msg = resumed[-1]["data"].get("message", "")
    assert "接着跑" in msg and "保留" in msg, msg
