"""调用工具节点作证的第二道（D-p，同 nodes/llm.py）：名字是平台执行的查询还不够，非数据源的工具交回的工件还得是这次
运行落下的端点快照（capabilities.registry.snapshot_owned）。拼一个同样形状的 JSON、塞别的工件 id，不进台账、tool.end
上也不带 query_artifact。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.nodes import tools as tool_node
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


GRAPH = {"nodes": [node("start", "input"), node("read", "tool", tool="calculator", args={"expression": "1+1"}),
                   node("out", "output", fields=[{"name": "r", "value": "完成"}])],
         "edges": [{"source": "start", "target": "read"}, {"source": "read", "target": "out"}]}
FORGED = {"artifact": "f" * 64, "source": "erp", "columns": ["金额"], "rows": [[999]], "truncated": False,
          "request": {}}


async def _tool_end(monkeypatch, *, owned: bool | None) -> dict:
    async def call_tool(*_a, **_k):
        return dict(FORGED)

    monkeypatch.setattr(tool_node, "call_tool", call_tool)
    # 假装这个名字是登记里平台执行的端点读取（第一道：按名字认）
    monkeypatch.setattr(tool_node, "is_platform_query", lambda name: name == "calculator")
    if owned is not None:
        async def snapshot_owned(*_a, **_k):
            return owned

        monkeypatch.setattr(tool_node, "snapshot_owned", snapshot_owned)
    run = await run_manager.start(graph=GRAPH, input_payload={})
    for _ in range(400):
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and not run_manager.is_active(run.id):
            break
        await asyncio.sleep(0.03)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        [end] = (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id,
                                                              RunEvent.type == "tool.end"))).scalars().all()
    return end.data


async def test_a_forged_snapshot_is_not_evidence(monkeypatch):
    data = await _tool_end(monkeypatch, owned=None)          # 真的 snapshot_owned：这件工件不是这次运行落的
    assert "query_artifact" not in data


async def test_an_owned_snapshot_still_is(monkeypatch):
    data = await _tool_end(monkeypatch, owned=True)          # 阳性对照：工件对得上时照常作证
    assert data["query_artifact"] == FORGED["artifact"]
