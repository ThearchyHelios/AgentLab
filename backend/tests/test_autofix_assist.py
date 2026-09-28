"""自动修复的 Copilot 兜底（autofix 的 assist: true）。

确定性修复修不了的 error 交给 Copilot：模型给剧本，看采纳规则——
- 正常的修复（error 变少、没有新 error、不降低要求）被采纳，只是预览，不落库
- 试图删契约的被拒
- 让错误变多的被拒
- 要人拿主意的放进 questions，不硬改
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Workflow, WorkflowVersion
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 40, "y": 80}, "data": {"label": label or nid, "config": config}}


CONTRACT = {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True}


def two_exits() -> dict:
    """受管模板没有带契约的出口，而且出口有两个：该给哪个写契约说不准，确定性修复只能交给 Copilot。"""
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": "SELECT SUM(amount) AS gmv FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", "报告撰写", instructions="写周报"),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        node("raw", "output", "原始数", fields=[{"name": "数", "value": "{{ nodes.fetch }}"}]),
    ]
    edges = [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
        [("start", "fetch"), ("fetch", "card"), ("card", "write"), ("write", "done"), ("fetch", "raw")])]
    return {"nodes": nodes, "edges": edges, "viewport": {"x": 3, "y": 4, "zoom": 1.2}}


class Scripted:
    """按剧本吐操作流的模型；记下每次收到的请求。"""

    def __init__(self, ops: list[dict]) -> None:
        self.lines = [json.dumps(o, ensure_ascii=False) for o in ops]
        self.calls: list[list] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        self.calls.append(messages)
        for line in self.lines:
            yield AIMessageChunk(content=line + "\n")


def script(monkeypatch, ops: list[dict]) -> Scripted:
    import app.api.copilot as copilot

    model = Scripted(ops)

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    return model


async def create(client, graph: dict) -> str:
    return (await client.post("/api/workflows", json={"name": "周报-兜底", "graph": graph})).json()["id"]


async def rows(wf: str) -> list:
    async with SessionLocal() as session:
        w = await session.get(Workflow, wf)
        vs = (await session.execute(select(WorkflowVersion).where(WorkflowVersion.workflow_id == wf))).scalars()
        return [{c.name: getattr(w, c.name) for c in Workflow.__table__.columns},
                [{c.name: getattr(v, c.name) for c in WorkflowVersion.__table__.columns} for v in vs]]


async def assist(client, wf: str, **extra) -> dict:
    r = await client.post(f"/api/workflows/{wf}/autofix", json={"level": "governed", "apply": [], "assist": True,
                                                               **extra})
    assert r.status_code == 200, r.text
    return r.json()


def done(ops: list[dict]) -> list[dict]:
    return [{"op": "plan", "summary": "给出具节点补契约"}, *ops, {"op": "done", "explanation": "补上了出具契约"}]


async def test_a_sound_fix_is_adopted_as_a_preview(client, monkeypatch):
    model = script(monkeypatch, done([{"op": "update_node", "id": "done", "config": {"contract": CONTRACT}}]))
    wf = await create(client, two_exits())
    before = await rows(wf)
    out = await assist(client, wf)
    assert out["assist"]["ok"] is True and out["assist"]["summary"] == "补上了出具契约"
    assert out["applied"] == ["assist"] and out["ok"] is True, out
    fixed = out["graph"]
    assert next(n for n in fixed["nodes"] if n["id"] == "done")["data"]["config"]["contract"] == CONTRACT
    assert fixed["viewport"] == two_exits()["viewport"] and [e["id"] for e in fixed["edges"]] == [
        e["id"] for e in two_exits()["edges"]]
    assert out["ops"] == [{"op": "update_node", "id": "done", "config": {"contract": CONTRACT}}]
    assert [(c["fix_id"], c["node_id"], c["field"]) for c in out["changes"]] == [("assist", "done", "contract")]
    assert await rows(wf) == before                           # 只是预览
    # 交给模型的请求里有剩下的问题和不许降低要求、不许替人选的约定
    human = next(text for role, text in model.calls[0] if role == "human")
    assert "受管模板至少要有一个「成果 / 出具」节点声明出具契约" in human
    assert "不许降低要求" in human and "不替人选" in human and "question" in human


async def test_deleting_the_contract_is_refused(client, monkeypatch):
    graph = two_exits()
    graph["nodes"][4]["data"]["config"]["contract"] = {**CONTRACT, "report_from": "fetch"}
    graph["nodes"][3] = node("write", "llm", "写周报", prompt="写周报")       # 没有报告撰写节点：只能交给 Copilot
    script(monkeypatch, done([{"op": "update_node", "id": "done", "config": {"contract": None}}]))
    wf = await create(client, graph)
    out = await assist(client, wf)
    assert out["assist"]["ok"] is False and "assist" not in out["applied"]
    [rejected] = [r for r in out["rejected"] if r["fix_id"] == "assist"]
    assert "契约" in rejected["reason"] and "降低" in rejected["reason"]
    assert out["graph"] == graph and out["ok"] is False


async def test_a_fix_that_makes_errors_grow_is_refused(client, monkeypatch):
    script(monkeypatch, done([
        {"op": "update_node", "id": "done", "config": {"contract": CONTRACT}},
        {"op": "update_node", "id": "card", "config": {"metrics": [{"id": "gmv"}]}},       # 口径卡的表达式被写丢了
    ]))
    wf = await create(client, two_exits())
    out = await assist(client, wf)
    assert out["assist"]["ok"] is False
    [rejected] = [r for r in out["rejected"] if r["fix_id"] == "assist"]
    assert "新的问题" in rejected["reason"] and "expression" in rejected["reason"]
    assert out["graph"] == two_exits()


async def test_removing_a_node_is_refused_even_if_it_helps(client, monkeypatch):
    script(monkeypatch, done([
        {"op": "remove_node", "id": "raw"},
        {"op": "update_node", "id": "done", "config": {"contract": CONTRACT}},
    ]))
    wf = await create(client, two_exits())
    out = await assist(client, wf)
    [rejected] = [r for r in out["rejected"] if r["fix_id"] == "assist"]
    assert "删" in rejected["reason"] and out["graph"] == two_exits()


async def test_questions_are_passed_on_without_changes(client, monkeypatch):
    script(monkeypatch, [
        {"op": "plan", "summary": "要先问清楚"},
        {"op": "question", "node_id": "done", "text": "两个出口里，哪个是要出具的？「出具」还是「原始数」？"},
        {"op": "question", "node_id": "card", "text": "required 要包括哪些指标？候选：gmv"},
        {"op": "question", "text": "   "},
        {"op": "done", "explanation": "等你决定出具的是哪个出口"},
    ])
    wf = await create(client, two_exits())
    out = await assist(client, wf)
    # 一句一条的文字，前面标上是哪个节点的事（句子里已经点了名的不重复标）
    assert out["assist"] == {"ok": False, "summary": "等你决定出具的是哪个出口", "questions": [
        "两个出口里，哪个是要出具的？「出具」还是「原始数」？", "「周报口径卡」：required 要包括哪些指标？候选：gmv"]}
    assert out["rejected"] == [] and out["graph"] == two_exits()


async def test_deterministic_fixes_run_first_and_copilot_sees_what_is_left(client, monkeypatch):
    graph = two_exits()
    graph["nodes"].insert(2, node("ask", "agent", "查数员", tools=["db_query__shop"], approval="never",
                                  prompt="用 db_query__shop 查"))
    graph["edges"] += [{"source": "start", "target": "ask"}, {"source": "ask", "target": "card"}]
    model = script(monkeypatch, done([{"op": "update_node", "id": "done", "config": {"contract": CONTRACT}}]))
    wf = await create(client, graph)
    out = await assist(client, wf, apply=["governed.agent_approval_never:ask"])
    assert out["applied"] == ["governed.agent_approval_never:ask", "assist"] and out["ok"] is True
    human = next(text for role, text in model.calls[0] if role == "human")
    assert "的审批策略是「全部自动放行」" not in human and '"approval": "dangerous"' in human


async def test_nothing_left_means_no_model_call(client, monkeypatch):
    model = script(monkeypatch, [])
    graph = two_exits()
    graph["nodes"][4]["data"]["config"]["contract"] = CONTRACT
    wf = await create(client, graph)
    out = await assist(client, wf)
    assert out["assist"]["ok"] is True and model.calls == []


async def test_an_unconfigured_model_is_said_plainly(client, monkeypatch):
    import app.api.copilot as copilot
    from app.providers.factory import ProviderNotConfigured

    async def _nothing(*_a, **_k):
        raise ProviderNotConfigured("没有可用的 provider")

    monkeypatch.setattr(copilot, "get_chat_model", _nothing)
    wf = await create(client, two_exits())
    out = await assist(client, wf)
    assert out["assist"]["ok"] is False and "设置" in out["assist"]["summary"]
    assert out["rejected"] == []


async def test_blocking_issues_only_add_the_gate_when_asked():
    from app.api.copilot import _blocking_issues

    graph = two_exits()
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert _blocking_issues(nodes, graph["edges"]) == []
    gated = _blocking_issues(nodes, graph["edges"], level="governed")
    assert [i["code"] for i in gated] == ["governed.no_contract"]
    assert _blocking_issues(nodes, graph["edges"], level="published") == []


def guarded() -> dict:
    """受管图里只剩一处 error：协作团队节点。团队后面还有一个「人工把关」节点。"""
    nodes = [
        node("start", "input", "周期"),
        node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": "SELECT SUM(amount) AS gmv FROM orders"}),
        node("card", "metrics", "周报口径卡", caliber="周报口径", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("team", "supervisor", "复核团队", goal="复核", agents=[{"name": "checker", "tools": []}]),
        node("gate", "human", "人工把关", prompt="确认数字"),
        node("write", "report", "报告撰写", instructions="写周报"),
        node("done", "output", "出具", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}], contract=CONTRACT),
    ]
    edges = [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
        [("start", "fetch"), ("fetch", "card"), ("card", "team"), ("team", "gate"), ("gate", "write"),
         ("write", "done")])]
    return {"nodes": nodes, "edges": edges}


@pytest.mark.parametrize("ops, word", [
    # 同一个 id 换了类型：团队换成模型调用，顺手把人工把关换成整形——等于删了重建
    ([{"op": "add_node", "node": {"id": "team", "type": "llm", "label": "复核团队", "config": {"prompt": "复核"}}},
      {"op": "add_node", "node": {"id": "gate", "type": "transform", "label": "人工把关",
                                  "config": {"mode": "expression", "expression": "vars"}}}], "人工把关"),
    # 类型没变，也是整个覆盖掉旧节点（配置、位置全丢）
    ([{"op": "add_node", "node": {"id": "gate", "type": "human", "label": "人工把关", "config": {"prompt": "随便"}}},
      {"op": "add_node", "node": {"id": "team", "type": "llm", "label": "复核团队", "config": {"prompt": "复核"}}}],
     "覆盖"),
])
async def test_overwriting_an_existing_node_with_add_node_is_refused(client, monkeypatch, ops, word):
    graph = guarded()
    script(monkeypatch, done(ops))
    wf = await create(client, graph)
    before = await rows(wf)
    out = await assist(client, wf)
    assert out["assist"]["ok"] is False and "assist" not in out["applied"] and out["ok"] is False
    [rejected] = [r for r in out["rejected"] if r["fix_id"] == "assist"]
    assert "降低" in rejected["reason"] and word in rejected["reason"], rejected
    assert out["graph"] == graph and out["changes"] == [] and out["ops"] == []
    assert [(n["id"], n["type"]) for n in out["graph"]["nodes"]] == [(n["id"], n["type"]) for n in graph["nodes"]]
    assert await rows(wf) == before


async def test_renaming_a_node_while_fixing_another_error_is_adopted(client, monkeypatch):
    """改了名的节点上没修好的 error 文案跟着变了，但还是同一处问题：不算冒出新问题。"""
    graph = guarded()
    graph["nodes"][6]["data"]["config"].pop("contract")        # 两处 error：协作团队、没有带契约的出口
    script(monkeypatch, done([
        {"op": "update_node", "id": "team", "label": "复核团队（待拆成固定步骤）"},
        {"op": "update_node", "id": "done", "config": {"contract": CONTRACT}},
    ]))
    wf = await create(client, graph)
    out = await assist(client, wf)
    assert out["applied"] == ["assist"] and out["assist"]["ok"] is True, out["rejected"]
    assert [(i["code"], i["node_id"]) for i in out["remaining"] if i["level"] == "error"] == [
        ("governed.supervisor", "team")]
    assert "复核团队（待拆成固定步骤）" in out["remaining"][0]["message"] or any(
        "复核团队（待拆成固定步骤）" in i["message"] for i in out["remaining"])
    assert {(c["node_id"], c["field"]) for c in out["changes"]} == {("team", "label"), ("done", "contract")}
