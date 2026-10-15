"""应用视图里端点快照的遮罩（BE-3 遗留；设计文档 A6 第 7 条、P4）。

端点快照（endpoint_snapshot）的单元格、表格按能力登记现在的遮罩字段（capabilities.registry.masked_paths）遮：快照里记下的
mask_columns 是落快照那一刻的，管理员事后给能力加了遮罩字段，来源运行没换，视图也得跟着遮——同数据源事后加遮罩列。
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete

from app.apps import resolve
from app.core.artifact_store import put_json
from app.db.base import SessionLocal
from app.db.models import Capability

TABULAR = {"rows": "/items", "columns": {"单号": "/id", "金额": "/amount", "电话": "/phone"}}


async def test_endpoint_snapshots_follow_the_capabilitys_current_masks():
    name = f"erp_orders_{uuid.uuid4().hex[:6]}"
    artifact = await put_json({"source": "erp", "capability": name, "columns": ["单号", "金额", "电话"],
                               "rows": [["A1", 12.5, "13900000001"]], "row_count": 1, "truncated": False,
                               "request": {}, "status": 200, "response": {}, "mask_columns": ["电话"]},
                              kind="endpoint_snapshot", run_id=f"r-{uuid.uuid4().hex[:6]}", node_id="read")
    source = resolve.Source(id="v", kind="latest_sealed", state="ok")
    async with SessionLocal() as session:
        session.add(Capability(name=name, tabular=TABULAR, masked=["/phone"]))
        await session.commit()
    try:
        snap, hidden = await resolve._snapshot(source, artifact)
        assert snap is not None and hidden == {"电话"}
        async with SessionLocal() as session:
            (await session.get(Capability, name)).masked = ["/phone", "/amount"]
            await session.commit()
        _, hidden = await resolve._snapshot(resolve.Source(id="v", kind="latest_sealed", state="ok"), artifact)
        assert hidden == {"电话", "金额"}, "事后加的遮罩字段照样遮"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Capability).where(Capability.name == name))
            await session.commit()
    # 能力删掉了：按快照记下的遮，不会把当时遮着的列亮出来
    _, hidden = await resolve._snapshot(resolve.Source(id="v", kind="latest_sealed", state="ok"), artifact)
    assert hidden == {"电话"}
