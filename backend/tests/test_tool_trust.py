"""MCP 和自定义工具的信任三档：等审批 / 始终允许 · 门控把关 / 始终允许。

以前这两类工具在任何审批模式下都不问人：审批关卡只认内置工具的 dangerous 和数据源的
dangerous_if，它们两样都没有。现在默认每次等人批；「始终允许」可以交给门控模型逐次把关。

要守住的几件事：
- 默认等审批，审批请求带 trust_key，审批卡才有「始终允许」；
- 门控出任何意外（没答上来、答不成格式）都按交给人工处理，不能变成放行；
- 点了「始终允许」：写进设置，本节点后面的同一个工具改由门控把关，不再问人；
- 一次运行用发起时的快照；正式运行不看三档；升级前的运行（快照为 None）照旧不问。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Approval, CustomTool, Run, RunEvent, Setting
from app.engine.runner import run_manager
from app.tools import custom as custom_tools
from app.tools.trust import (
    FORMAL_MARK, GATE_SYSTEM, SETTING_KEY, asks_trustable, call_policy, load_trust, parse_verdict,
    set_trust,
)

TOOL = "crm_lookup"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture(autouse=True)
async def clean_trust():
    async with SessionLocal() as session:
        row = await session.get(Setting, SETTING_KEY)
        if row:
            await session.delete(row)
            await session.commit()
    yield


@pytest.fixture
async def crm(monkeypatch):
    """一个自定义 http 工具。真正的请求换成记账：调了几次、参数是什么。"""
    calls: list[dict] = []

    async def fake_http(row, args):
        calls.append(dict(args))
        return {"customer": args.get("name"), "level": "gold"}

    monkeypatch.setattr(custom_tools, "_run_http", fake_http)
    async with SessionLocal() as session:
        session.add(CustomTool(name=TOOL, kind="http", description="按名字查客户等级",
                               parameters={"type": "object", "properties": {"name": {"type": "string"}},
                                           "required": ["name"]},
                               config={"url": "https://example.com/crm"}))
        await session.commit()
    yield calls
    async with SessionLocal() as session:
        for row in (await session.execute(select(CustomTool).where(CustomTool.name == TOOL))).scalars():
            await session.delete(row)
        await session.commit()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def agent_graph(**extra):
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("bot", "agent", prompt="查两位客户的等级", tools=[TOOL], max_steps=5, **extra),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.bot.text }}"}]),
    )


def tool_graph(**extra):
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("t", "tool", tool=TOOL, args={"name": "甲公司"}, **extra),
        node("out", "output", fields=[{"name": "结果", "value": "{{ nodes.t.result }}"}]),
    )


def script(monkeypatch, *, gate=None, names=("甲公司", "乙公司"), team=False):
    """agent 依次查 names 里的客户；gate 是门控模型的回复（字符串、可调用，或 None 表示不该被问到）。"""
    from app.providers import mock_model

    gate_calls: list[str] = []
    turns = {"route": 0}

    def _decide(self, messages):
        if messages and isinstance(messages[0], SystemMessage) and messages[0].content == GATE_SYSTEM:
            gate_calls.append(str(messages[-1].content))
            if gate is None:
                raise AssertionError("不该问门控")
            return AIMessage(content=gate(messages) if callable(gate) else gate)
        if team and self.response_format:
            turns["route"] += 1
            plan = ({"assignments": [{"agent": "sales", "instruction": "查甲公司"}]}
                    if turns["route"] == 1 else {"assignments": [], "done": True})
            return AIMessage(content=json.dumps(plan, ensure_ascii=False))
        if self.response_format:
            return AIMessage(content="{}")
        if not self.tools:
            return AIMessage(content="收尾")
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(names):
            return AIMessage(content="查一下。", tool_calls=[
                {"name": TOOL, "args": {"name": names[done]}, "id": f"call_{done}"}])
        return AIMessage(content="都查完了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)
    return gate_calls


ALLOW = '{"verdict": "allow", "reason": "只读查询，和任务一致"}'
ESCALATE = '{"verdict": "escalate", "reason": "拿不准"}'


async def wait(run_id: str, statuses=("interrupted", "failed", "succeeded"), timeout=20.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
            if row and row.status in statuses:
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"等超时了：{run_id}")


async def events(run_id: str, kind: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))).scalars())
    return [e for e in rows if kind is None or e.type == kind]


async def pending(run_id: str) -> list[Approval]:
    async with SessionLocal() as session:
        return list((await session.execute(select(Approval).where(
            Approval.run_id == run_id, Approval.status == "pending"))).scalars())


async def start(graph, **kw) -> str:
    return (await run_manager.start(graph=graph, input_payload={"question": "查"}, **kw)).id


# ---------------------------------------------------------------- 判定规则


class _NoArgs(BaseModel):
    pass


def _tool(key: str | None, name: str = "x") -> StructuredTool:
    async def run(**_): return ""
    return StructuredTool(name=name, description="", coroutine=run, func=None, args_schema=_NoArgs,
                          metadata={"trust_key": key} if key else {})


def test_policy_for_trustable_tools():
    t = _tool("mcp:demo/search", "search")
    assert call_policy(t, "search", {}, {}) == "ask"
    assert call_policy(t, "search", {}, {"mcp:demo/search": "gated"}) == "gate"
    assert call_policy(t, "search", {}, {"mcp:demo/search": "always"}) == "safe"
    # 正式运行：档位一律不看
    assert call_policy(t, "search", {}, {FORMAL_MARK: "1", "mcp:demo/search": "always"}) == "ask"
    # 升级前发起的运行：照旧不问（它们可能正停在别的审批上，多问一次答复就对错号了）
    assert call_policy(t, "search", {}, None) == "safe"


def test_policy_leaves_builtin_tools_alone():
    """内置工具不看三档：calculator 安全，shell_exec 危险，和以前一样。"""
    assert call_policy(_tool(None, "calculator"), "calculator", {}, {}) == "safe"
    assert call_policy(_tool(None, "shell_exec"), "shell_exec", {}, {"shell_exec": "always"}) == "ask"


def test_always_button_only_where_it_takes_effect():
    t = _tool("crm_lookup")
    assert asks_trustable(t, {}) == "crm_lookup"
    assert asks_trustable(t, {FORMAL_MARK: "1"}) is None
    assert asks_trustable(t, None) is None
    assert asks_trustable(_tool(None), {}) is None


@pytest.mark.parametrize("text, expected", [
    ('{"verdict": "allow", "reason": "只读"}', (True, "只读")),
    ('好的：{"verdict":"escalate","reason":"要删数据"}', (False, "要删数据")),
    ('{"verdict": "allow"}', (True, "（门控没写理由）")),
    ('{"verdict": "yes"}', None),
    ("放行", None),
    ("", None),
    ('{"verdict": "allow", ', None),
])
def test_parse_verdict(text, expected):
    assert parse_verdict(text) == expected


# ---------------------------------------------------------------- 接口


@pytest.fixture
async def client():
    # 不走 TestClient：它会跑应用的启动流程、按环境变量种 provider，后面的运行就不再用 mock
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _row(client) -> dict:
    return next(t for t in (await client.get("/api/tools")).json() if t["id"] == TOOL)


async def test_api_lists_and_sets_trust(crm, client):
    row = await _row(client)
    assert (row["trust"], row["trust_key"], row["runtime_approval"]) == ("ask", TOOL, True)

    assert (await client.put("/api/tools/trust", json={"key": TOOL, "trust": "always"})).json() \
        == {"key": TOOL, "trust": "always"}
    row = await _row(client)
    assert (row["trust"], row["runtime_approval"]) == ("always", False)

    await client.put("/api/tools/trust", json={"key": TOOL, "trust": "gated"})
    row = await _row(client)
    assert (row["trust"], row["runtime_approval"]) == ("gated", True)

    # 改回等审批：从表里删掉，不留一条 ask
    await client.put("/api/tools/trust", json={"key": TOOL, "trust": "ask"})
    stored = (await client.get("/api/settings")).json().get(SETTING_KEY) or {}
    assert TOOL not in stored.get("tools", {})

    assert (await client.put("/api/tools/trust", json={"key": TOOL, "trust": "yes"})).status_code == 422
    assert (await client.put("/api/tools/trust", json={"key": "", "trust": "ask"})).status_code == 422


async def test_settings_page_cannot_overwrite_trust(crm, client):
    """设置页整组回写时带着一份旧的三档，不能把工具页刚改的冲掉。"""
    await client.put("/api/tools/trust", json={"key": TOOL, "trust": "always"})
    await client.put("/api/settings", json={"values": {SETTING_KEY: {"tools": {}}, "run": {"stream_tokens": True}}})
    assert (await _row(client))["trust"] == "always"


async def test_gate_model_settings_have_defaults(client):
    run = (await client.get("/api/settings")).json()["run"]
    assert run["tool_gate_provider"] is None and run["tool_gate_model"] is None


# ---------------------------------------------------------------- agent 节点


async def test_custom_tool_waits_for_approval_by_default(crm, monkeypatch):
    script(monkeypatch, names=("甲公司",))
    run_id = await start(agent_graph())
    run = await wait(run_id)
    assert run.status == "interrupted", f"自定义工具没等审批就执行了：{run.status} {run.error}"
    assert crm == []
    [approval] = await pending(run_id)
    assert approval.payload["trust_key"] == TOOL
    [asked] = await events(run_id, "human.requested")
    assert asked.data["trust_key"] == TOOL

    await run_manager.resume(run_id, {"approved": True})
    run = await wait(run_id, ("failed", "succeeded"))
    assert run.status == "succeeded", run.error
    assert crm == [{"name": "甲公司"}]


async def test_always_runs_without_asking(crm, monkeypatch):
    await _set(TOOL, "always")
    script(monkeypatch, gate=None)
    run = await wait(await start(agent_graph()))
    assert run.status == "succeeded", run.error
    assert [c["name"] for c in crm] == ["甲公司", "乙公司"]
    assert not await events(run.id, "human.requested")


async def test_gated_and_allowed_runs_without_asking(crm, monkeypatch):
    await _set(TOOL, "gated")
    asked = script(monkeypatch, gate=ALLOW)
    run = await wait(await start(agent_graph()))
    assert run.status == "succeeded", run.error
    assert len(asked) == 2 and "甲公司" in asked[0] and "查两位客户的等级" in asked[0]
    gated = await events(run.id, "tool.gated")
    assert [(e.data["verdict"], e.data["reason"]) for e in gated] == [("allow", "只读查询，和任务一致")] * 2
    assert not await events(run.id, "human.requested")
    assert len(crm) == 2


@pytest.mark.parametrize("reply", [ESCALATE, "我觉得可以", '{"verdict": "maybe"}'])
async def test_gate_that_does_not_clearly_allow_hands_over_to_a_person(crm, monkeypatch, reply):
    await _set(TOOL, "gated")
    script(monkeypatch, gate=reply, names=("甲公司",))
    run = await wait(await start(agent_graph()))
    assert run.status == "interrupted", "门控没放行，却没交给人"
    assert crm == []
    [gated] = await events(run.id, "tool.gated")
    assert gated.data["verdict"] == "escalate"


async def test_gate_that_crashes_hands_over_to_a_person(crm, monkeypatch):
    await _set(TOOL, "gated")

    def boom(_):
        raise RuntimeError("网关断开")

    script(monkeypatch, gate=boom, names=("甲公司",))
    run = await wait(await start(agent_graph()))
    assert run.status == "interrupted"
    [gated] = await events(run.id, "tool.gated")
    assert gated.data["verdict"] == "escalate" and "门控模型没答上来" in gated.data["reason"]


async def test_gate_verdict_is_not_asked_again_when_the_node_replays(crm, monkeypatch):
    """门控判了交给人 → 批准 → 节点重放。重放时不能再问门控：这次可能判放行，interrupt 就错号了。"""
    await _set(TOOL, "gated")
    replies = iter([ESCALATE, ALLOW, ALLOW])
    asked = script(monkeypatch, gate=lambda _: next(replies))
    run_id = await start(agent_graph())
    await wait(run_id)
    await run_manager.resume(run_id, {"approved": True})
    run = await wait(run_id, ("failed", "succeeded", "interrupted"))
    assert run.status == "succeeded", run.error
    assert len(asked) == 2, f"门控被问了 {len(asked)} 次，重放时又问了一遍"
    assert [c["name"] for c in crm] == ["甲公司", "乙公司"]


async def test_always_button_persists_and_covers_the_rest_of_the_node(crm, monkeypatch):
    asked = script(monkeypatch, gate=ALLOW)
    run_id = await start(agent_graph())
    await wait(run_id)
    await run_manager.resume(run_id, {"approved": True, "always": True})
    run = await wait(run_id, ("failed", "succeeded", "interrupted"))
    assert run.status == "succeeded", f"点了始终允许，第二次调用还在等审批：{run.status}"
    assert len(await events(run_id, "human.requested")) == 1
    # 第一次是人批的，第二次改由门控把关
    assert len(asked) == 1 and "乙公司" in asked[0]
    resolved = (await events(run_id, "human.resolved"))[0]
    assert resolved.data.get("always") is True
    async with SessionLocal() as session:
        assert (await load_trust(session)) == {TOOL: "gated"}


async def test_always_is_ignored_where_there_is_no_button(monkeypatch):
    """内置工具的审批带了 always 也不理：没有可改的档位。"""
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("t", "tool", tool="calculator", args={"expression": "1+1"}, approval="always"),
        node("out", "output", fields=[]),
    )
    run_id = await start(graph)
    await wait(run_id)
    [approval] = await pending(run_id)
    assert "trust_key" not in approval.payload
    await run_manager.resume(run_id, {"approved": True, "always": True})
    await wait(run_id, ("failed", "succeeded"))
    async with SessionLocal() as session:
        assert await load_trust(session) == {}


async def test_a_run_keeps_the_trust_it_started_with(crm, monkeypatch):
    """运行中途在工具页改了档位，这次运行不受影响：审批恢复时的判定必须和第一次一样。"""
    script(monkeypatch, gate=None)
    run_id = await start(agent_graph())
    await wait(run_id)
    await _set(TOOL, "always")
    await run_manager.resume(run_id, {"approved": True})
    run = await wait(run_id, ("failed", "succeeded", "interrupted"))
    assert run.status == "interrupted", "快照被中途的改动顶掉了：第二次调用没再问"
    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).tool_trust == {}


async def test_formal_runs_ignore_trust(crm, monkeypatch):
    await _set(TOOL, "always")
    script(monkeypatch, gate=None, names=("甲公司",))
    run_id = await start(agent_graph(), run_class="formal")
    run = await wait(run_id)
    assert run.status == "interrupted", "正式运行吃了用户的「始终允许」"
    [approval] = await pending(run_id)
    assert "trust_key" not in approval.payload
    started = (await events(run_id, "run.started"))[0]
    assert started.data["tool_trust"] == "formal"


async def test_run_started_records_the_snapshot(crm, monkeypatch):
    await _set(TOOL, "always")
    await _set("mcp:demo/search", "gated")
    script(monkeypatch, gate=None)
    run = await wait(await start(agent_graph()))
    started = (await events(run.id, "run.started"))[0]
    assert started.data["tool_trust"] == {TOOL: "always", "mcp:demo/search": "gated"}


async def test_node_approval_never_skips_the_gate(crm, monkeypatch):
    await _set(TOOL, "gated")
    script(monkeypatch, gate=None)
    run = await wait(await start(agent_graph(approval="never")))
    assert run.status == "succeeded", run.error
    assert not await events(run.id, "tool.gated")


# ---------------------------------------------------------------- 工具节点


async def test_tool_node_gated_allow_runs(crm, monkeypatch):
    await _set(TOOL, "gated")
    script(monkeypatch, gate=ALLOW)
    run = await wait(await start(tool_graph()))
    assert run.status == "succeeded", run.error
    assert crm == [{"name": "甲公司"}]
    assert [e.data["verdict"] for e in await events(run.id, "tool.gated")] == ["allow"]


async def test_tool_node_escalation_asks_with_the_always_button(crm, monkeypatch):
    await _set(TOOL, "gated")
    asked = script(monkeypatch, gate=ESCALATE)
    run_id = await start(tool_graph())
    assert (await wait(run_id)).status == "interrupted"
    [approval] = await pending(run_id)
    assert approval.payload["trust_key"] == TOOL
    await run_manager.resume(run_id, {"approved": True, "always": True})
    run = await wait(run_id, ("failed", "succeeded"))
    assert run.status == "succeeded", run.error
    assert len(asked) == 1, "重放时又问了一遍门控"
    assert len(await events(run_id, "tool.gated")) == 1
    assert crm == [{"name": "甲公司"}]


async def test_tool_node_default_waits(crm, monkeypatch):
    script(monkeypatch, gate=None)
    run = await wait(await start(tool_graph()))
    assert run.status == "interrupted" and crm == []


# ---------------------------------------------------------------- 协作团队


def team_graph():
    return chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("team", "supervisor", goal="查客户", max_rounds=3,
             agents=[{"name": "sales", "system": "你是销售助理", "tools": [TOOL], "max_steps": 3}]),
        node("out", "output", fields=[]),
    )


async def test_team_member_does_not_run_an_unapproved_custom_tool(crm, monkeypatch):
    script(monkeypatch, gate=None, names=("甲公司",), team=True)
    run = await wait(await start(team_graph()))
    assert run.status == "succeeded", run.error
    assert crm == []
    assert "tool_needs_approval" in [e.data.get("code") for e in await events(run.id, "log")]


async def test_team_member_gated(crm, monkeypatch):
    await _set(TOOL, "gated")
    script(monkeypatch, gate=ALLOW, names=("甲公司",), team=True)
    run = await wait(await start(team_graph()))
    assert run.status == "succeeded", run.error
    assert crm == [{"name": "甲公司"}]
    [gated] = await events(run.id, "tool.gated")
    assert gated.data["agent"] == "sales" and gated.data["verdict"] == "allow"


async def test_team_member_gate_escalation_is_not_executed(crm, monkeypatch):
    await _set(TOOL, "gated")
    script(monkeypatch, gate=ESCALATE, names=("甲公司",), team=True)
    run = await wait(await start(team_graph()))
    assert run.status == "succeeded", run.error
    assert crm == []
    ends = [e.data.get("preview", "") for e in await events(run.id, "tool.end")]
    assert any("门控模型没有放行（拿不准）" in p for p in ends), ends


async def _set(key: str, level: str) -> None:
    async with SessionLocal() as session:
        await set_trust(session, key, level)
