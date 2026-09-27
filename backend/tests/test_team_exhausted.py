"""协作团队用完轮数、调度者始终没判定完成：不许把成员原话当作成功结果交出去。

一次真实运行：成员没有工具，模型把工具调用标记当文字写了四轮；调度者每轮都说
「核对员还没查到数据，本轮继续派核对员」，四轮用完，节点把核对员最后那段标记
当作结论交了出去，运行 succeeded。负责写结论的成员一次都没被派到。

新配置 on_exhausted：fail（默认）判失败，说清调度者最后一轮的理由、谁从没被派到；
degrade 照样交付，但产出里带 exhausted: true、发 team_exhausted 警告，复核和出具
都据此降档。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine import review
from app.engine.runner import run_manager


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def _never_done(monkeypatch) -> None:
    """调度者每轮都继续派核对员，理由一样；核对员每次都交不出东西。"""
    from app.providers import mock_model

    def _decide(self, messages):
        if self.response_format:
            return AIMessage(content=json.dumps({
                "assignments": [{"agent": "核对员", "instruction": "独立重查一遍"}],
                "done": False, "reason": "核对员还没拿到真实条数，定稿要等复核结果",
            }, ensure_ascii=False))
        return AIMessage(content="我还需要再查一次。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


def _graph(**team):
    return {
        "nodes": [
            node("start", "input", fields=[{"name": "question"}]),
            node("team", "supervisor", goal="核对订单条数", agents=[
                {"name": "核对员", "system": "你负责复核", "max_steps": 1},
                {"name": "撰稿员", "system": "你负责写结论", "max_steps": 1},
            ], **{"max_rounds": 3, **team}),
            node("out", "output", fields=[{"name": "r", "value": "{{ nodes.team.text }}"}],
                 contract={"metrics_from": "card", "narrative": "{{ nodes.team.text }}"}),
            node("card", "metrics", metrics=[{"id": "n", "expression": "1"}]),
        ],
        "edges": [{"source": "start", "target": "card"}, {"source": "card", "target": "team"},
                  {"source": "team", "target": "out"}],
    }


async def _finish(graph) -> tuple[Run, list[RunEvent]]:
    run = await run_manager.start(graph=graph, input_payload={"question": "x"})
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row.status in ("succeeded", "failed"):
                break
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )).scalars())
    return row, events


async def test_running_out_of_rounds_fails_by_default(monkeypatch):
    _never_done(monkeypatch)
    row, events = await _finish(_graph())
    assert row.status == "failed", f"成员原话被当成了结论：{row.output}"
    assert "协作团队用完 3 轮仍未完成：核对员还没拿到真实条数，定稿要等复核结果" in row.error
    assert "撰稿员" in row.error, "从没被派过的成员要点出来"
    assert row.error_node_id == "team"


async def test_degrade_delivers_but_says_so(monkeypatch):
    _never_done(monkeypatch)
    row, events = await _finish(_graph(on_exhausted="degrade"))
    assert row.status == "succeeded", row.error

    finished = next(e.data for e in events if e.type == "node.finished" and e.node_id == "team")
    assert finished["preview"]["exhausted"] is True
    warn = next(e.data for e in events if e.type == "log" and e.data.get("code") == "team_exhausted")
    assert "撰稿员" in warn["message"] and "3 轮" in warn["message"]

    # 出具降档，理由写在 gaps 里
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded"
    assert any("用完 3 轮" in g for g in issuance["gaps"]), issuance["gaps"]
    # 复核也认这条警告
    signals = review.scan(events, row.output)
    assert any(s.kind == "team_exhausted" for s in signals), signals


async def test_a_team_that_finishes_is_unaffected(monkeypatch):
    from app.providers import mock_model

    turns = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            turns["n"] += 1
            plan = ({"assignments": [{"agent": "核对员", "instruction": "查"}]} if turns["n"] == 1
                    else {"assignments": [], "done": True, "reason": "查清楚了"})
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        return AIMessage(content="一共 1 条。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    row, events = await _finish(_graph())
    assert row.status == "succeeded", row.error
    assert not [e for e in events if e.data.get("code") == "team_exhausted"]


# --------------------------------------------------------------------------
# 最后一轮派出去的成员交回来之后，调度者还要看一眼
# --------------------------------------------------------------------------


async def test_a_pipeline_that_needs_exactly_max_rounds_succeeds(monkeypatch):
    """2 轮的流水线（查数 → 撰稿），最多轮数也是 2：第二轮的撰稿员交回来之后，调度者
    以前再没机会说「完成」，轮数一到就算用完，缺省判失败。"""
    from app.providers import mock_model

    routed = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            text = "\n".join(str(m.content) for m in messages)
            if "轮数已经用完" in text:
                plan = {"assignments": [], "done": "撰稿员" in text and "结论" in text,
                        "reason": "结论已经写好"}
            else:
                routed["n"] += 1
                who = "核对员" if routed["n"] == 1 else "撰稿员"
                plan = {"assignments": [{"agent": who, "instruction": "做你那部分"}],
                        "done": False, "reason": f"交给{who}"}
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        return AIMessage(content="结论：一共 1 条。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    row, events = await _finish(_graph(max_rounds=2))
    assert row.status == "succeeded", row.error
    assert row.output["r"] == "结论：一共 1 条。"
    closing = [e.data for e in events if e.type == "agent.route.end" and e.data.get("closing")]
    assert len(closing) == 1 and closing[0]["done"] is True, closing
    assert not [e for e in events if e.data.get("code") == "team_exhausted"]


async def test_the_closing_look_can_still_say_not_done(monkeypatch):
    _never_done(monkeypatch)
    row, events = await _finish(_graph(max_rounds=2))
    assert row.status == "failed"
    closing = [e.data for e in events if e.type == "agent.route.end" and e.data.get("closing")]
    assert len(closing) == 1 and closing[0]["done"] is False, closing
    # 收尾那一次也是调度者的一次模型调用，要记账
    coordinator = [e for e in events if e.type == "llm.end" and e.data.get("agent") == "调度者"]
    assert len(coordinator) == 3


# --------------------------------------------------------------------------
# 接着跑一个失败的团队节点：前几轮取自断点，时间线上不再出现一遍
# --------------------------------------------------------------------------


async def test_continuing_a_failed_team_does_not_replay_its_rounds(monkeypatch):
    _never_done(monkeypatch)
    row, events = await _finish(_graph(max_rounds=2))
    assert row.status == "failed"

    def tally(evs):
        kinds = ("agent.route.start", "agent.route.end", "agent.step.start", "agent.step.end",
                 "llm.end")
        counts = {k: sum(1 for e in evs if e.type == k and e.node_id == "team") for k in kinds}
        counts["调度"] = sum(1 for e in evs if e.type == "log" and e.node_id == "team"
                           and str(e.data.get("message", "")).startswith("调度 →"))
        return counts

    before = tally(events)
    assert before["agent.step.start"] == 2 and before["agent.route.start"] == 3, before

    graph = _graph(max_rounds=2, on_exhausted="degrade")
    await run_manager.continue_failed(row.id, graph=graph)
    for _ in range(300):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, row.id)
            if row.status in ("succeeded", "failed"):
                break
    assert row.status == "succeeded", row.error
    async with SessionLocal() as session:
        after_events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == row.id).order_by(RunEvent.seq)
        )).scalars())
    assert tally(after_events) == before, "接着跑把前几轮的调度和成员又发了一遍"
    # 前几轮取自断点，用量不能跟着丢：成员的调用也要算进去
    assert row.usage["calls"] == before["llm.end"], (row.usage, before)


# --------------------------------------------------------------------------
# 调度者说完成了，可派出去的成员一个都没交回结果
# --------------------------------------------------------------------------

DSML = ('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="db_query__shop">\n'
        '<｜｜DSML｜｜ parameter name="sql">SELECT COUNT(*) FROM orders</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')


def _gives_up_after(monkeypatch, *plan: str, writes: dict[str, str]) -> None:
    """调度者按 plan 依次派人，派完就判完成；成员按 system 认人，照 writes 回话。"""
    from app.providers import mock_model

    routed = {"n": 0}

    def _decide(self, messages):
        if self.response_format:
            routed["n"] += 1
            if routed["n"] <= len(plan):
                who = plan[routed["n"] - 1]
                decision = {"assignments": [{"agent": who, "instruction": "做你那部分"}],
                            "done": False, "reason": f"交给{who}"}
            else:
                decision = {"assignments": [], "done": True, "reason": "查不到了，先这样"}
            return AIMessage(content=json.dumps(decision, ensure_ascii=False))
        role = str(messages[0].content)
        return AIMessage(content=next(v for k, v in writes.items() if k in role))

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


async def test_nobody_delivering_is_not_a_finished_team(monkeypatch):
    """验收时的一次：成员没绑工具，两次都把工具调用写成文字；调度者读到失败说明，
    判定完成。团队把「（<成员> 这一步失败：…）」当成果交了出去，运行 succeeded，
    画布头部是绿色的已完成——一条数据都没查到。"""
    _gives_up_after(monkeypatch, "核对员", writes={"复核": DSML, "结论": "不该轮到我"})
    row, events = await _finish(_graph())
    assert row.status == "failed", f"失败说明被当成了成果：{row.output}"
    assert row.error_node_id == "team"
    assert "没有交出结论" in row.error and "查不到了，先这样" in row.error, row.error
    # 失败的原因一起带上：界面照它说「模型没有真正调用工具」、指到画布上去绑工具
    assert "工具调用的原始标记" in row.error, row.error
    assert "撰稿员" in row.error, "从没被派过的成员要点出来"


async def test_nobody_delivering_fails_even_when_set_to_degrade(monkeypatch):
    """「用完轮数时 · 降档交付」交的是成员原话。没人交回结果时手上只有失败说明，
    交下去不是降档、是冒充：照样判失败，也不借用「用完 N 轮」的说法。"""
    _gives_up_after(monkeypatch, "核对员", writes={"复核": DSML, "结论": "不该轮到我"})
    row, events = await _finish(_graph(on_exhausted="degrade"))
    assert row.status == "failed", f"失败说明被当成了成果：{row.output}"
    assert "没有交出结论" in row.error and "用完" not in row.error, row.error
    assert not [e for e in events if e.data.get("code") == "team_exhausted"]


async def test_a_failed_last_step_is_not_handed_over_as_the_result(monkeypatch):
    """核对员查到了，撰稿员失败，调度者判完成：成果是核对员查到的，不是撰稿员的失败说明。"""
    _gives_up_after(monkeypatch, "核对员", "撰稿员", writes={"复核": "一共 1 条。", "结论": DSML})
    row, events = await _finish(_graph())
    assert row.status == "succeeded", row.error
    assert row.output["r"] == "一共 1 条。", row.output
    warn = [e.data for e in events if e.type == "log" and e.data.get("code") == "team_last_failed"]
    assert warn and "撰稿员" in warn[0]["message"] and "核对员" in warn[0]["message"], warn
    signals = review.scan(events, row.output)
    assert any(s.kind == "team_last_failed" for s in signals), signals
