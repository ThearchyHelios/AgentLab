"""按位置取自遮罩列的一格（vars.res.rows[1][2]、nodes.fetch.rows[0][0]）在推演之外的两处：期 4 I2a 把它接进了
recompute 的还原（_positional），用 recompute 的地方都跟着变，这里把变化钉住。

- 双盲复算的预检（dual_check._plan_metric 什么都不改先重算一遍）：以前这种指标照常盲算，遮着的那一格还会作为字面量
  写进给盲算模型的计算式；现在预检判它用到了遮罩列，按无法复算记，原因不说「推演」（这里不是推演）
- 运行对比（engine/compare.py）：这种输入两边的值都不给，标 masked，和 cell() 取的遮罩格一样

库和数字都是编的：号码写成带横线的文本，区域名是「东区」这类。
"""
from __future__ import annotations

import json

from app.engine import compare
from app.engine.runner import run_manager
from tests.fixtures.compare_runs import BASE_ROWS, NEXT_ROWS, metric, scalar_in, week
from tests.test_evidence_metric_masks import (  # noqa: F401 - 夹具按名字注入（engine_up 自动生效）
    METRICS,
    PHONES,
    SQL,
    client,
    engine_up,
    node,
    set_source,
    shop,
)


def dual_graph():
    """取数 → 数据整形（vars.res）→ 口径卡 → 双盲复算（只复算 mix）→ 成果。"""
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool="db_query__shop", args={"sql": SQL}),
        node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="res"),
        node("card", "metrics", caliber="遮罩口径", caliber_version="v1", metrics=METRICS),
        node("dual", "dual_check", metrics=["mix"]),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.card.text }}"}]),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def dual_output(run_id: str) -> dict:
    from sqlalchemy import select

    from app.core import artifact_store
    from app.db.base import SessionLocal
    from app.db.models import RunEvent

    async with SessionLocal() as session:
        [finished] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "node.finished", RunEvent.node_id == "dual"))).scalars()
    return artifact_store.load(finished.data["artifact"])


async def test_the_dual_check_does_not_blind_compute_a_metric_reading_a_masked_cell_by_position(shop):
    """mix = round(cell(nodes.fetch, 1, 'amount') + len(vars.res.rows[1][2]), 1)：cell() 那一格不在遮罩列上，按位置取的
    号码在。认出它取自遮罩列，按无法复算记缺口，原因点名是哪个输入、不说「推演」；没遮罩时预检照常通过、指标进入盲算。"""
    import asyncio

    from app.db.base import SessionLocal
    from app.db.models import Run

    async def run(options):
        await set_source(shop, options)
        run_id = (await run_manager.start(graph=dual_graph(), input_payload={})).id
        for _ in range(400):
            await asyncio.sleep(0.05)
            async with SessionLocal() as session:
                row = await session.get(Run, run_id)
            if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
                await run_manager.wait_idle(run_id)
                assert row.status == "succeeded", row.error
                return await dual_output(run_id)
        raise AssertionError(f"运行 {run_id} 没跑完：{row.status}")

    masked = await run({"mask_columns": ["phone"]})
    [mix] = masked["metrics"]
    assert mix["metric_id"] == "mix" and mix["status"] == "unchecked"
    assert mix["reason"] == "输入 vars.res.rows[1][2] 取自设置了遮罩的列，不能用于复算"
    assert "推演" not in json.dumps(masked, ensure_ascii=False)
    assert not [p for p in PHONES if p in json.dumps(masked, ensure_ascii=False)]
    assert any("遮罩" in gap for gap in masked["issuance_gaps"])

    # 没遮罩：预检通过，原因不再是遮罩（盲算模型没配，按盲算没交回数记，但已经走到了盲算这一步）
    plain = await run({})
    [mix] = plain["metrics"]
    assert "遮罩" not in (mix.get("reason") or "") and "无法按原表达式重算" not in (mix.get("reason") or "")


def region_len(fetch, bot, rows, bot_rows):
    """东区名称的字数：按位置取自取数结果的第 0 行第 0 列（region）。"""
    return [metric("rlen", "len(nodes.fetch.rows[0][0])", len(rows[0][0]),
                   [scalar_in("nodes.fetch.rows[0][0]", rows[0][0], "fetch", "tool")], name="首个区域名字数",
                   unit="字", decimals=0)]


def test_the_compare_page_withholds_a_masked_cell_read_by_position():
    """运行对比：按位置取自遮罩列（region）的一格两边都不给值、标 masked，和 cell() 取的遮罩格一样；没遮罩时照给。"""
    def masked(run):
        run.masked = {a: frozenset({"region"}) for a in run.queries}
        return run

    result = compare.compare(masked(week("r1", metrics=region_len)).side(),
                             masked(week("r2", NEXT_ROWS, metrics=region_len)).side())
    [rlen] = [m for m in result["metrics"] if m["metric_id"] == "rlen"]
    [inp] = rlen["inputs"]
    assert inp["masked"] is True and inp["base"] is None and inp["target"] is None
    assert "东区" not in json.dumps(result, ensure_ascii=False)

    shown = compare.compare(week("r1", metrics=region_len).side(), week("r2", NEXT_ROWS, metrics=region_len).side())
    [rlen] = [m for m in shown["metrics"] if m["metric_id"] == "rlen"]
    [inp] = rlen["inputs"]
    assert not inp.get("masked") and inp["base"] == BASE_ROWS[0][0] and inp["target"] == NEXT_ROWS[0][0]
