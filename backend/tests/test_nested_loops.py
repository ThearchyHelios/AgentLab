"""嵌套循环：内层循环每被外层带进来一次，都从头数。

运行 87daca70：一个计时器，外层每一拍里内层跑两个 50ms 的子拍。5 秒该推进 50 拍，
结果推进了 138 拍——内层循环的计数走 done 出口时原样留着，外层下一轮再把它带进来，
就从上次停下的地方接着往上数。内层 max_iterations 是 5：第一拍用掉 2 次，第二拍
2 次，第三拍 1 次，从第四拍起一进来就撞上限直接退出，子拍一次都不跑（撞上限的
warn 136 条）。max_iterations 于是成了"整次运行合计多少轮"，而不是"每进来一次
最多多少轮"。

嵌套的 foreach 更糟：外层第二轮时内层游标已经在末尾，内层一项都不再跑，没有报错，
结果只是悄悄少了一大截。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.runner import run_manager


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "data": {"label": nid, "config": config}}


def edge(source, target, handle=None):
    return {"source": source, "target": target, "sourceHandle": handle}


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


async def test_an_inner_while_restarts_on_every_outer_round(engine_up):
    """87daca70 的形状：外层 3 拍，每拍内层 2 个子拍，合计 6 次。

    内层上限给 3（够一拍用，不够三拍合计用）：计数不复位的话第二拍只跑 1 次、
    第三拍一次不跑，合计 3。
    """
    row = await _finish({
        "nodes": [
            node("start", "input", fields=[]),
            node("init_i", "transform", expression="0", assign_to="i"),
            node("init_total", "transform", expression="0", assign_to="total"),
            node("outer", "loop", mode="while", condition="vars.i < 3", max_iterations=10),
            node("reset_j", "transform", expression="0", assign_to="j"),
            node("inner", "loop", mode="while", condition="vars.j < 2", max_iterations=3),
            node("bump_j", "transform", expression="vars.j + 1", assign_to="j"),
            node("count", "transform", expression="vars.total + 1", assign_to="total"),
            node("bump_i", "transform", expression="vars.i + 1", assign_to="i"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.total }}"}]),
        ],
        "edges": [
            edge("start", "init_i"), edge("init_i", "init_total"), edge("init_total", "outer"),
            edge("outer", "reset_j", "body"), edge("reset_j", "inner"),
            edge("inner", "bump_j", "body"), edge("bump_j", "count"), edge("count", "inner"),
            edge("inner", "bump_i", "done"), edge("bump_i", "outer"),
            edge("outer", "out", "done"),
        ],
    })
    assert row.status == "succeeded", row.error
    assert row.output["结果"] == "6"


async def test_an_inner_foreach_walks_its_items_on_every_outer_round(engine_up):
    """外层 3 项 × 内层 2 项。游标不复位的话，外层第二轮起内层一项都不跑，只剩 1a、1b。"""
    row = await _finish({
        "nodes": [
            node("start", "input", fields=[]),
            node("init_log", "transform", expression="[]", assign_to="log"),
            node("outer", "loop", mode="foreach", items="[1, 2, 3]", item_var="o",
                 max_iterations=10),
            node("inner", "loop", mode="foreach", items='["a", "b"]', item_var="x",
                 max_iterations=10),
            node("add", "transform", expression="vars.log + [str(vars.o) + vars.x]",
                 assign_to="log"),
            node("out", "output", fields=[{"name": "结果", "value": "{{ vars.log | json }}"}]),
        ],
        "edges": [
            edge("start", "init_log"), edge("init_log", "outer"),
            edge("outer", "inner", "body"),
            edge("inner", "add", "body"), edge("add", "inner"),
            edge("inner", "outer", "done"),
            edge("outer", "out", "done"),
        ],
    })
    assert row.status == "succeeded", row.error
    assert json.loads(row.output["结果"]) == ["1a", "1b", "2a", "2b", "3a", "3b"]
