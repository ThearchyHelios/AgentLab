"""场景模板实例化出来的图真的能运行（docs/design/normal-user-apps.md §3.3「实例化 + 检查 + 运行」）：周期看板按示例蓝图
实例化、生成关节填上之后，在一个小库上跑一次，口径卡用 cell() 取到格，成果按指标完整出具。库和数字都是编的。
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest

from app.blueprints.instantiate import instantiate
from app.blueprints.templates import _common as c
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def shop(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL, paid_at TEXT, refunded INTEGER);")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", [
            (1, 100.0, "2026-10-05", 0), (2, 300.0, "2026-10-08", 1), (3, 600.0, "2026-10-11", 0),
            (4, 999.0, "2026-10-12", 0)])  # 统计期外
    name = f"bprun_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=str(path), readonly=True, enabled=True)
        session.add(row)
        await session.commit()
        source_id = row.id
    yield name
    async with SessionLocal() as session:
        await session.delete(await session.get(DataSource, source_id))
        await session.commit()
    await engines.invalidate(source_id)


async def test_the_board_template_runs_and_issues_by_metrics(shop):
    from app.blueprints.templates._common import safe_id

    extras = c.EXTRAS["periodic_board"]
    spec = extras.example()
    for item in spec["data"]:
        item["table"]["value"]["source"] = shop
    node = f"fetch_{safe_id(shop)}"
    fill = {k.replace("fetch_shop", node): v.replace("fetch_shop", node) for k, v in extras.example_fill().items()}
    inst = instantiate(spec, filled=fill)
    assert inst.problems == []
    run = await run_manager.start(graph=inst.graph, input_payload={
        "period": "2026-W41", "period_start": "2026-10-05", "period_end": "2026-10-11"})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    output = row.output or {}
    issuance = output["_issuance"]
    assert (issuance["tier"], issuance["mode"]) == ("formal", "metrics"), issuance
    # 销售额不含已退款的那一单；订单数、退款率按全部订单
    assert output["销售额（元）"] == "700.0" and output["订单数（单）"] == "3"
    assert output["退款率（%）"] == "33.33"
