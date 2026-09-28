"""出具时逐个说清：叙述里的每个数字回指到了哪张口径卡的哪个指标。

以前事件和成果里只有 matched_numbers 这一个计数。界面想在正文里把数字逐个标出
「来自口径卡指标 orders · 销售口径 @ v2」，拿不到对应关系，只能标出没对上的那些。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


async def test_each_traced_number_names_its_metric_and_caliber():
    graph = {
        "nodes": [
            node("start", "input"),
            node("card", "metrics", caliber="销售口径", caliber_version="v2", metrics=[
                {"id": "orders", "expression": "120"},
                {"id": "aov", "expression": "35.5"},
            ]),
            node("draft", "transform", mode="template", assign_to="summary",
                 template="本周订单 120 单，客单价 35.5 元，另有 7 单"),
            node("out", "output", fields=[{"name": "结论", "value": "{{ vars.summary }}"}],
                 contract={"metrics_from": "card", "narrative": "{{ vars.summary }}"}),
        ],
        "edges": [{"source": "start", "target": "card"}, {"source": "card", "target": "draft"},
                  {"source": "draft", "target": "out"}],
    }
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(200):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed"):
                break
    assert row.status == "succeeded", row.error

    # start / end 是 token 在叙述里的位置：同一个数出现两次时，界面按位置标，不再按字符串全局匹配
    expected = [{"token": "120", "metric": "orders", "caliber": "销售口径 @ v2", "start": 5, "end": 8},
                {"token": "35.5", "metric": "aov", "caliber": "销售口径 @ v2", "start": 15, "end": 19}]
    assert row.output["_issuance"]["matched"] == expected
    assert row.output["_issuance"]["matched_numbers"] == 2
    async with SessionLocal() as session:
        issuance = (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "issuance")
        )).scalars().one()
    assert issuance.data["matched"] == expected
    assert issuance.data["matched_numbers"] == 2
