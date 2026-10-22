"""内置模板「⑪ 异动分析（可追溯）」：取数（整体按周 26 周、按地区两周）→ 分析卡（波动基线 + 带总数的变化拆解）→ 报告撰写
（分析说法严格核对）→ 出具（引用模式，单元格引用）。

模板既是示范也是活文档（同 tests/test_template_traceable.py 守模板 ⑨）：要通过画布校验、能直接按受管级别发布（发布检查
没有错误），接上一份虚构的零售库 shop（orders(order_date, region, amount)：华东、华南、华北、西南四个地区，26 周每天一笔）
要能从头到尾运行、两项分析都做成、完整出具。期次是每周第一天的日期，分析卡认得出每一期覆盖哪几天，本期过没过完核对得了。
"""
from __future__ import annotations

import asyncio
import copy
import sqlite3
from datetime import date, timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.governance import lint_for_publish, publish_issues
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph
from app.seed import TEMPLATES

NAME = "⑪ 异动分析（可追溯）"
REGIONS = {"华东": 400, "华南": 300, "华北": 200, "西南": 100}


def template() -> dict:
    return next(t for t in TEMPLATES if t["name"] == NAME)


def graph() -> dict:
    t = template()
    return copy.deepcopy({"nodes": t["nodes"], "edges": t["edges"]})


def test_the_template_is_listed_once_after_the_others():
    names = [t["name"] for t in TEMPLATES]
    assert names.count(NAME) == 1 and names[-1] == NAME
    assert any(n.startswith("⑨ 可追溯周报") for n in names)


def test_the_template_passes_validation():
    report = validate_graph(GraphSpec.model_validate(graph()))
    assert report.ok, [i.message for i in report.issues if i.level == "error"]
    types = {n["id"]: n["type"] for n in template()["nodes"]}
    assert list(types.values()).count("analysis") == 1 and "metrics" not in types.values()
    card = next(n for n in template()["nodes"] if n["type"] == "analysis")["data"]["config"]
    assert [(i["id"], i["op"], i.get("mode")) for i in card["items"]] == [("wow", "baseline", None),
                                                                          ("where", "decompose", "additive")]
    assert card["items"][1]["params"]["headline"] == {"base": "weekly.r1.销售额", "target": "weekly.r0.销售额"}
    assert "overrides" not in card, "阈值取设置，模板不写死"


def test_the_template_publishes_as_governed():
    """拿来就能按受管级别发布：发布检查（和真正发布同一套）没有错误，证据门禁 G1–G5 和分析卡的受管规则都过。"""
    spec = GraphSpec.model_validate(graph())
    errors = [i for i in publish_issues(spec, level="governed") if i.level == "error"]
    assert errors == [], [i.message for i in errors]
    report = next(n for n in template()["nodes"] if n["type"] == "report")["data"]["config"]
    assert {k: report[k] for k in ("numbers", "on_violation", "claims", "analysis_claims")} == {
        "numbers": "strict", "on_violation": "fail", "claims": "require_citation", "analysis_claims": "strict"}
    out = next(n for n in template()["nodes"] if n["type"] == "output")["data"]["config"]
    assert out["contract"] == {"report_from": "write", "cells": True, "strict": True}
    assert out["fields"] == [{"name": "异动分析", "value": "{{ nodes.write.text }}"}]


def test_template_nine_also_publishes_as_governed():
    """模板 ⑨ 在第 1 波补了分析说法核对设为严格：同样直接按受管级别发布，没有错误。"""
    nine = next(t for t in TEMPLATES if t["name"].startswith("⑨"))
    spec = GraphSpec.model_validate(copy.deepcopy({"nodes": nine["nodes"], "edges": nine["edges"]}))
    errors = [i for i in publish_issues(spec, level="governed") if i.level == "error"]
    assert errors == [], [i.message for i in errors]
    assert not [i for i in lint_for_publish(spec, level="governed").issues if i.code == "governed.report_policy"]


# --------------------------------------------------------------------------
# 接一份虚构的零售库从头到尾运行
# --------------------------------------------------------------------------


def _last_week() -> date:
    """最近一个已经过完的整周的周一（运行当天往前，本周没过完）。"""
    today = date.today()
    return today - timedelta(days=today.weekday() + 7)


@pytest.fixture
async def shop(tmp_path, scratch_source):
    """26 周、每个地区每天一笔：各周略有起伏；最后一周（最近一个过完的整周）华东掉了四成，别的地区照常。"""
    monday = _last_week()
    first = monday - timedelta(weeks=25)
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, order_date TEXT, region TEXT, amount REAL);")
    rows, n = [], 0
    for week in range(26):
        for day in range(7):
            when = first + timedelta(weeks=week, days=day)
            for k, (region, base) in enumerate(REGIONS.items()):
                amount = base + (week * 7 + day * 3 + k * 5) % 11
                if week == 25 and region == "华东":
                    amount = round(amount * 0.6, 2)
                n += 1
                rows.append((n, when.isoformat(), region, float(amount)))
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    await scratch_source("shop", path)
    return monday


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def _wait(run_id: str) -> Run:
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没有结束：{row.status}")


async def test_the_template_runs_end_to_end_and_issues_formally(engine_up, shop, monkeypatch):
    from app.providers import mock_model

    text = ("## 本周异动\n\n本周销售额 [[v:Q1.r0.销售额]]，上周 [[v:Q1.r1.销售额]]。[[see:Q1]]\n\n"
            "[[a:A1]]。[[see:A1]]\n\n[[a:A2]]。[[see:A2]]\n\n[[table:A2 cols=地区,base,target,delta]]")
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))
    monday = shop
    run = await run_manager.start(graph=graph(), input_payload={
        "week": monday.isoformat(), "prev_week": (monday - timedelta(weeks=1)).isoformat()})
    row = await _wait(run.id)
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        ends = [e.data for e in (await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "analysis.end")
            .order_by(RunEvent.seq))).scalars()]
    by_item = {e["item"]: e for e in ends}
    # 两项都做成了：本周的变化放回 25 周的历史里看（期次认得出、本期过完了），华东主导了这次下降（各段之和和总数闭合）
    assert by_item["wow"]["status"] == "ok", by_item["wow"]
    assert by_item["wow"]["verdict"] in ("outside_band", "far_outside_band"), by_item["wow"]
    assert (by_item["where"]["status"], by_item["where"]["verdict"]) == ("ok", "driver"), by_item["where"]
    assert "主要来自华东" in by_item["where"]["phrase"]
    issuance = row.output["_issuance"]
    assert issuance["mode"] == "citations" and issuance["tier"] == "formal", issuance
    report = row.output["异动分析"]
    assert by_item["where"]["phrase"] in report and by_item["wow"]["phrase"] in report
