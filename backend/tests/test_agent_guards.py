"""agent 护栏：取代以前固定的「最多 12 步」。

用户的原话是「12 太少了，撞上就判定运行失败」。真实运行里（719253eb、6310f291）agent
用满 12 步收了尾，结论缺数据，下游的结构化校验缺字段，整个运行失败，报错里看不出根源。

现在：
- 步数只剩一个很高的兜底（默认 100，设置里可改）；以前画布写死的 12 按「没填」处理；
- 同一个调用第二次不再执行，把上次结果交还；连续几步拿不到新信息就收尾；
- 令牌 / 金额预算，可以设成不限；
- 上下文接近窗口先压缩早期工具结果，再不够就收尾；
- 临近上限提醒模型一次；
- 下游校验失败时写明是上游哪个 agent 提前收了尾；
- 升级前发起的运行（没有快照）一字不改走旧逻辑。
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import CustomTool, Run, RunEvent, Setting
from app.engine import runner as runner_mod
from app.engine.guards import node_limits, run_limits
from app.engine.runner import run_manager
from app.tools import custom as custom_tools

FINAL = "结论：都查完了。"
SETTLED = "收尾：按已经查到的部分。"
TOOL = "shop_lookup"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
async def clean_run_settings():
    async def drop():
        async with SessionLocal() as session:
            row = await session.get(Setting, "run")
            if row:
                await session.delete(row)
                await session.commit()
    await drop()
    yield
    await drop()


@pytest.fixture
async def lookup(monkeypatch):
    """自定义工具：按参数返回一段文字，长度可调。真正的请求换成记账。"""
    calls: list[dict] = []
    size = {"chars": 20}

    async def fake_http(row, args):
        calls.append(dict(args))
        return f"门店 {args.get('q')}：" + "数" * size["chars"]

    monkeypatch.setattr(custom_tools, "_run_http", fake_http)
    async with SessionLocal() as session:
        session.add(CustomTool(name=TOOL, kind="http", description="查门店",
                               parameters={"type": "object", "properties": {"q": {"type": "string"}},
                                           "required": ["q"]},
                               config={"url": "https://example.com/shop"}))
        await session.commit()
    yield calls, size
    async with SessionLocal() as session:
        for row in (await session.execute(select(CustomTool).where(CustomTool.name == TOOL))).scalars():
            await session.delete(row)
        await session.commit()


def graph(**config):
    cfg = {"prompt": "查门店", "tools": [TOOL], "approval": "never", **config}
    return {
        "nodes": [
            {"id": "in", "type": "input", "data": {"config": {}}},
            {"id": "work", "type": "agent", "data": {"label": "查数", "config": cfg}},
            {"id": "out", "type": "output", "data": {"config": {}}},
        ],
        "edges": [{"source": "in", "target": "work"}, {"source": "work", "target": "out"}],
    }


def script(monkeypatch, plan, *, usage=None):
    """plan(第几次调用, messages) → 查询参数（字符串）或 None（给结论）。

    流式和非流式都换掉：mock 走流式时不报用量，预算的测试要自己带上（usage）。
    seen 记下每次调用模型时看到的 messages。
    """
    from app.providers import mock_model

    seen: list[list] = []

    def decide(self, messages):
        if not self.tools:
            return AIMessage(content=SETTLED)
        seen.append(list(messages))
        q = plan(len(seen) - 1, messages)
        if q is None:
            return AIMessage(content=FINAL)
        return AIMessage(content="再查一个。", tool_calls=[
            {"name": TOOL, "args": {"q": q}, "id": f"call_{len(seen)}"}])

    async def astream(self, messages, stop=None, run_manager=None, **kwargs):
        reply = decide(self, messages)
        extra = {"usage_metadata": usage(messages, reply)} if usage else {}
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=reply.content, tool_calls=reply.tool_calls, **extra))

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    monkeypatch.setattr(mock_model.MockChatModel, "_astream", astream)
    return seen


async def run(g, *, settings_run: dict | None = None):
    if settings_run is not None:
        async with SessionLocal() as session:
            session.add(Setting(key="run", value=settings_run))
            await session.commit()
    started = await run_manager.start(graph=g, input_payload={"question": "有几家门店"})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, started.id)
            if row.status in ("succeeded", "failed", "cancelled", "interrupted"):
                break
    else:
        pytest.fail("运行没有在 20 秒内结束")
    async with SessionLocal() as session:
        events = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == started.id).order_by(RunEvent.seq))).scalars())
    return row, events


def logs(events, code):
    return [e.data for e in events if e.type == "log" and e.data.get("code") == code]


def node_out(row):
    return row.output


# ---------------------------------------------------------------- 上限怎么定


def test_run_limits_from_settings():
    assert run_limits({}, 100) == {"max_steps": 100, "budget_tokens": 2_000_000, "budget_usd": None}
    # 写 null / 0 / 空都是不限；存坏了的步数按缺省
    assert run_limits({"agent_budget_tokens": None, "agent_budget_usd": 0}, 100)["budget_tokens"] is None
    assert run_limits({"agent_budget_tokens": ""}, 100)["budget_tokens"] is None
    assert run_limits({"agent_max_steps": "abc"}, 100)["max_steps"] == 100
    # 环境变量的硬顶压过设置
    assert run_limits({"agent_max_steps": 500}, 100)["max_steps"] == 100
    assert run_limits({"agent_budget_usd": 1.5}, 100)["budget_usd"] == 1.5


def test_node_overrides_and_the_legacy_twelve():
    snap = {"max_steps": 100, "budget_tokens": 2_000_000, "budget_usd": None}
    # 画布以前自动写进去的 12：当作没填
    assert node_limits({"max_steps": 12}, snap, 100).max_steps == 100
    assert node_limits({"max_steps": "12"}, snap, 100).max_steps == 100
    assert node_limits({"max_steps": 20}, snap, 100).max_steps == 20
    assert node_limits({"max_steps": 300}, snap, 100).max_steps == 100
    got = node_limits({"budget_tokens": 5000, "budget_usd": 0.5}, snap, 100)
    assert (got.budget_tokens, got.budget_usd) == (5000, 0.5)
    assert node_limits({}, {**snap, "budget_tokens": None}, 100).budget_tokens is None


async def test_settings_expose_the_defaults():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        got = (await c.get("/api/settings")).json()["run"]
    assert (got["agent_max_steps"], got["agent_budget_tokens"], got["agent_budget_usd"]) == (100, 2_000_000, None)


# ---------------------------------------------------------------- 步数


async def test_twelve_steps_no_longer_cut_it_short(lookup, monkeypatch):
    """查 15 个门店再下结论：以前第 12 步就被掐断，现在跑完。节点上写着 12 也一样。"""
    calls, _ = lookup
    script(monkeypatch, lambda i, _: f"店{i}" if i < 15 else None)
    row, events = await run(graph(max_steps=12))
    assert row.status == "succeeded", row.error
    assert len(calls) == 15
    assert not logs(events, "step_limit_settled")
    assert "limited" not in str(row.output)


async def test_an_explicit_step_limit_still_settles(lookup, monkeypatch):
    calls, _ = lookup
    script(monkeypatch, lambda i, _: f"店{i}")
    row, events = await run(graph(max_steps=4))
    assert row.status == "succeeded", row.error
    assert len(calls) == 4
    [settled] = logs(events, "step_limit_settled")
    assert settled["reason"] == "steps" and "4 步" in settled["message"]


async def test_the_settings_default_applies(lookup, monkeypatch):
    calls, _ = lookup
    script(monkeypatch, lambda i, _: f"店{i}")
    row, events = await run(graph(), settings_run={"agent_max_steps": 6})
    assert len(calls) == 6
    assert logs(events, "step_limit_settled")[0]["reason"] == "steps"


async def test_run_started_records_the_limits(lookup, monkeypatch):
    script(monkeypatch, lambda i, _: None)
    row, events = await run(graph(), settings_run={"agent_budget_tokens": None, "agent_max_steps": 30})
    started = next(e for e in events if e.type == "run.started")
    assert started.data["agent_limits"] == {"max_steps": 30, "budget_tokens": None, "budget_usd": None}


# ---------------------------------------------------------------- 重复与停滞


async def test_a_repeated_call_is_answered_from_the_first_result(lookup, monkeypatch):
    calls, _ = lookup
    seen = script(monkeypatch, lambda i, _: ["店A", "店A", None][i])
    row, events = await run(graph())
    assert row.status == "succeeded", row.error
    assert calls == [{"q": "店A"}], "同样的调用又执行了一次"
    [repeat] = logs(events, "tool_repeat")
    assert TOOL in repeat["message"]
    reused = [m for m in seen[2] if isinstance(m, ToolMessage)][-1].content
    assert "已经执行过" in reused and "门店 店A" in reused


async def test_going_in_circles_settles(lookup, monkeypatch):
    """一直用同样的参数查：第一次真查，之后每步都拿不到新信息，连续 3 步就收尾。"""
    calls, _ = lookup
    script(monkeypatch, lambda i, _: "店A")
    row, events = await run(graph())
    assert row.status == "succeeded", row.error
    assert calls == [{"q": "店A"}]
    assert len(logs(events, "tool_repeat")) == 3, "没进展的 3 步之后就该收尾，不能一路耗到步数兜底"
    [settled] = logs(events, "step_limit_settled")
    assert settled["reason"] == "stall"
    assert SETTLED in str(row.output)


async def test_new_information_resets_the_stall_count(lookup, monkeypatch):
    calls, _ = lookup
    # A、A、A（两步没进展）、B（有进展，清零）、B、B（又两步）、结论
    order = ["店A", "店A", "店A", "店B", "店B", "店B", None]
    script(monkeypatch, lambda i, _: order[i])
    row, events = await run(graph())
    assert row.status == "succeeded", row.error
    assert calls == [{"q": "店A"}, {"q": "店B"}]
    assert not logs(events, "step_limit_settled")


# ---------------------------------------------------------------- 预算


def per_call(tokens):
    return lambda messages, reply: {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}


async def test_token_budget_settles(lookup, monkeypatch):
    calls, _ = lookup
    script(monkeypatch, lambda i, _: f"店{i}", usage=per_call(1000))
    row, events = await run(graph(budget_tokens=3500))
    assert row.status == "succeeded", row.error
    # 第 4 次调用之后累计 4000，超过 3500：那一步的工具照常跑完，然后收尾
    assert len(calls) == 4
    [settled] = logs(events, "step_limit_settled")
    assert settled["reason"] == "budget_tokens" and "3,500" in settled["message"]


async def test_budget_can_be_unlimited(lookup, monkeypatch):
    calls, _ = lookup
    # 用量记在输出上：输入一大就先撞上下文水位了，这里只测预算
    script(monkeypatch, lambda i, _: f"店{i}" if i < 8 else None,
           usage=lambda m, r: {"input_tokens": 10, "output_tokens": 1_000_000, "total_tokens": 1_000_010})
    row, events = await run(graph(), settings_run={"agent_budget_tokens": None})
    assert row.status == "succeeded", row.error
    assert len(calls) == 8 and not logs(events, "step_limit_settled")


async def test_usd_budget_settles_when_prices_are_known(lookup, monkeypatch):
    from app.providers import catalog

    monkeypatch.setattr(catalog, "estimate_cost", lambda model, i, o: i / 1000 * 0.01)
    calls, _ = lookup
    script(monkeypatch, lambda i, _: f"店{i}", usage=per_call(1000))
    row, events = await run(graph(budget_usd=0.025))
    assert len(calls) == 3
    assert logs(events, "step_limit_settled")[0]["reason"] == "budget_usd"


async def test_reminder_before_the_budget_runs_out(lookup, monkeypatch):
    seen = script(monkeypatch, lambda i, _: f"店{i}" if i < 5 else None, usage=per_call(1000))
    row, _ = await run(graph(budget_tokens=5000))
    notes = [m.content for call in seen for m in call if isinstance(m, HumanMessage) and "系统提醒" in m.content]
    assert notes and "token 预算已用 80%" in notes[0]
    assert len(set(notes)) == 1, "同一种提醒只该说一次"


async def test_reminder_before_the_steps_run_out(lookup, monkeypatch):
    seen = script(monkeypatch, lambda i, _: f"店{i}")
    await run(graph(max_steps=8))
    # 第 5 步（下标 4）之后还剩 3 步：第 6 次调用时看得到提醒
    assert any("还剩 3 步" in m.content for m in seen[5] if isinstance(m, HumanMessage))
    assert not any("系统提醒" in m.content for m in seen[4] if isinstance(m, HumanMessage))


# ---------------------------------------------------------------- 上下文


async def test_old_tool_results_are_compressed_near_the_window(lookup, monkeypatch):
    from app.providers import catalog

    calls, size = lookup
    size["chars"] = 3000
    # 没报用量时按字数的一半估：每条结果约 1500 token，第 6 次调用前后到窗口的 60%
    monkeypatch.setattr(catalog, "find_context", lambda model: 12_000)
    seen = script(monkeypatch, lambda i, _: f"店{i}" if i < 7 else None)
    row, events = await run(graph())
    assert row.status == "succeeded", row.error
    assert logs(events, "context_compressed")
    last = [m.content for m in seen[-1] if isinstance(m, ToolMessage)]
    assert any("早期的工具结果已压缩" in c for c in last[:-3])
    assert all("早期的工具结果已压缩" not in c for c in last[-3:]), "最近 3 条不该压缩"


async def test_too_close_to_the_window_settles(lookup, monkeypatch):
    from app.providers import catalog

    calls, size = lookup
    size["chars"] = 3000
    monkeypatch.setattr(catalog, "find_context", lambda model: 1_000)
    script(monkeypatch, lambda i, _: f"店{i}")
    row, events = await run(graph())
    assert row.status == "succeeded", row.error
    assert logs(events, "step_limit_settled")[0]["reason"] == "context"
    assert len(calls) <= 2


# ---------------------------------------------------------------- 下游校验与老运行


async def test_validation_failure_names_the_agent_that_was_cut_short(lookup, monkeypatch):
    script(monkeypatch, lambda i, _: f"店{i}")
    g = graph(max_steps=3)
    g["nodes"][2] = {"id": "check", "type": "validate", "data": {"label": "校验", "config": {
        "source": "{{ nodes.work.text }}", "repair_with_llm": False,
        "schema": {"type": "object", "required": ["stores"], "properties": {"stores": {"type": "array"}}}}}}
    g["edges"][1] = {"source": "work", "target": "check"}
    row, _ = await run(g)
    assert row.status == "failed"
    assert "上游「查数」因步数用完" in (row.error or ""), row.error


async def test_runs_from_before_the_upgrade_keep_the_old_rules(lookup, monkeypatch):
    """没有快照的运行：默认 12 步，重复调用照样执行——它们可能正停在某个审批上，
    多一次复用、少一次调用，重放时 checkpoint 就对错号了。"""
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    calls, _ = lookup
    script(monkeypatch, lambda i, _: "店A")
    row, events = await run(graph())
    assert row.status == "succeeded", row.error
    assert len(calls) == 12
    [settled] = logs(events, "step_limit_settled")
    assert "reason" not in settled and "已用完 12 步" in settled["message"]
    assert not logs(events, "tool_repeat")


def test_review_signal_carries_the_reason():
    """问数据页只在步数用满时给「放宽步数重跑」：复核信号要带上收尾原因。"""
    from app.engine.review import scan

    def log(code, reason=None):
        data = {"level": "warn", "code": code, "message": f"{code} {reason}"}
        if reason:
            data["reason"] = reason
        return {"type": "log", "node_id": "work", "data": data}

    got = {s.detail: s.as_dict() for s in scan([log("step_limit_settled", "stall"),
                                                log("step_limit_settled", "steps"),
                                                log("step_limit_settled")], {"a": "答案"})}
    assert got["step_limit_settled stall"]["reason"] == "stall"
    assert got["step_limit_settled steps"]["reason"] == "steps"
    assert "reason" not in got["step_limit_settled None"], "老运行的信号不该多出字段"


async def test_settings_limits_reflect_the_real_config():
    """库里存着旧版设置页写进去的 limits（见过 max_agent_steps: 25）：不生效，也不能盖住真实上限。"""
    from httpx import ASGITransport, AsyncClient

    from app.core.config import settings as app_settings
    from app.main import app

    async with SessionLocal() as session:
        row = await session.get(Setting, "limits")
        if row:
            row.value = {"max_agent_steps": 25}
        else:
            session.add(Setting(key="limits", value={"max_agent_steps": 25}))
        await session.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/api/settings")).json()["limits"]["max_agent_steps"] == app_settings.max_agent_steps
            await c.put("/api/settings", json={"values": {"limits": {"max_agent_steps": 7}}})
        async with SessionLocal() as session:
            assert (await session.get(Setting, "limits")).value == {"max_agent_steps": 25}, "limits 不该被写"
    finally:
        async with SessionLocal() as session:
            row = await session.get(Setting, "limits")
            if row:
                await session.delete(row)
                await session.commit()
