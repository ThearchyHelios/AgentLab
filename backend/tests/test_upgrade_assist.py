"""一键升级的语义层（assist: true）：请 Copilot 把喂口径卡的纯算术沙箱代码改写成口径卡表达式。

模型一律用剧本，不调真接口。守这几件事：
- 纯算术的改写被采纳：口径卡的表达式改成直接读上游，代码节点留在图上，只是预览、不改库
- 删节点、放宽要求的改写整个作废，确定性改写的结果照样交出去
- 不是纯算术的保留原样并给警告；模型要人拿主意的原样放进 questions
- 改出新 error 的按自查循环交回去改，改不好就作废
- 没有要改写的代码节点时不调模型
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


def node(nid, ntype, label=None, x=0, **config):
    return {"id": nid, "type": ntype, "position": {"x": x, "y": 80}, "data": {"label": label or nid, "config": config}}


def graph(*, summary_llm: bool = False) -> dict:
    """查库 → 沙箱代码算比率 → 口径卡读代码的产出 → 报告撰写（或者模型调用写总结）→ 出口。"""
    writer = (node("write", "llm", "写总结", x=1120, prompt="写一句总结") if summary_llm
              else node("write", "report", "报告撰写", x=1120, instructions="写一句话"))
    return {
        "nodes": [
            node("start", "input", "输入", x=0),
            node("fetch", "tool", "查库", x=280, tool="db_query__shop",
                 args={"sql": "SELECT SUM(amount) AS a, COUNT(*) AS b FROM orders"}),
            node("calc", "code", "算比率", x=560, language="python", assign_to="calc",
                 code="import json\nprint(json.dumps({'ratio': 3 / 4}))"),
            node("card", "metrics", "口径卡", x=840, metrics=[
                {"id": "ratio", "name": "客单价", "expression": "vars.calc.ratio"}]),
            writer,
            node("out", "output", "成果", x=1400, fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
        ],
        "edges": [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
            [("start", "fetch"), ("fetch", "calc"), ("calc", "card"), ("card", "write"), ("write", "out")])],
    }


class Scripted:
    """第一轮吐 first；收到自查请求（human 里有「上线前自查」）时吐 fix。记下每次的 human。"""

    def __init__(self, first: list[dict], fix: list[dict] | None = None) -> None:
        self.first = [json.dumps(o, ensure_ascii=False) for o in first]
        self.fix = [json.dumps(o, ensure_ascii=False) for o in fix or []]
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        human = next(text for role, text in messages if role == "human")
        self.calls.append(human)
        for line in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=line + "\n")


def script(monkeypatch, first: list[dict], fix: list[dict] | None = None) -> Scripted:
    import app.api.copilot as copilot

    model = Scripted(first, fix)

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    return model


def wrap(*ops) -> list[dict]:
    return [{"op": "plan", "summary": "把纯算术的代码挪进口径卡"}, *ops,
            {"op": "done", "explanation": "客单价改由口径卡直接算"}]


REWRITE = {"op": "update_node", "id": "card", "config": {"metrics": [
    {"id": "ratio", "name": "客单价", "expression": "cell(nodes.fetch, 0, 'a') / cell(nodes.fetch, 0, 'b')"}]}}


def by_id(g: dict) -> dict:
    return {n["id"]: n for n in g["nodes"]}


async def upgrade(client, g: dict, **extra) -> dict:
    r = await client.post("/api/copilot/upgrade-evidence", json={"graph": g, "assist": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def rows() -> list:
    async with SessionLocal() as session:
        return [sorted(repr({c.name: getattr(r, c.name) for c in m.__table__.columns})
                       for r in (await session.execute(select(m))).scalars())
                for m in (Workflow, WorkflowVersion)]


async def test_a_pure_arithmetic_rewrite_is_adopted_as_a_preview(client, monkeypatch):
    model = script(monkeypatch, wrap(REWRITE))
    await client.post("/api/workflows", json={"name": "升级-兜底", "graph": graph()})
    before = await rows()
    body = await upgrade(client, graph())
    assert body["assist"]["ok"] is True and body["assist"]["summary"] == "客单价改由口径卡直接算"
    assert body["assist"]["warnings"] == [] and "assist" in body["applied"]
    g = body["graph"]
    assert by_id(g)["card"]["data"]["config"]["metrics"][0]["expression"] == REWRITE["config"]["metrics"][0]["expression"]
    assert by_id(g)["calc"]["data"]["config"] == graph()["nodes"][2]["data"]["config"]      # 代码节点留着、不替人标 source
    assert [c for c in body["changes"] if c["rule"] == "assist"] == [
        {"fix_id": "assist", "rule": "assist", "node_id": "card", "node_title": "口径卡", "field": "metrics",
         "before": graph()["nodes"][3]["data"]["config"]["metrics"], "after": REWRITE["config"]["metrics"],
         "label": "助手的修改"}]
    assert body["ops"][-1] == REWRITE
    assert not [n for n in body["notes"] if n["rule"] == "R5"]                 # 口径卡不再读它，建议随之作废
    assert await rows() == before                                              # 只是预览
    human = model.calls[0]
    assert "纯算术" in human and "「算比率」（calc）" in human and "vars.calc.ratio" in human
    assert "不许降低要求" in human and "不替人选" in human and "只改上面这些口径卡指标的 expression" in human
    assert "只修下面列出的问题" not in human


async def test_the_prompt_names_the_metrics_that_read_the_code_through_a_reshaper(client, monkeypatch):
    # 代码 → 整形 → 口径卡：口径卡的指标读的是整形节点，追到头才是这段代码，要改写的表达式照样得列给模型
    g = graph()
    g["nodes"].insert(3, node("shape", "transform", "整形", x=700, template="{{ vars.calc }}", assign_to="shaped"))
    g["nodes"][4]["data"]["config"]["metrics"] = [
        {"id": "ratio", "name": "客单价", "expression": "vars.shaped.ratio"},
        {"id": "orders", "name": "订单数", "expression": "cell(nodes.fetch, 0, 'b')"}]
    g["edges"] = [{"id": f"e{i}", "source": a, "target": b} for i, (a, b) in enumerate(
        [("start", "fetch"), ("fetch", "calc"), ("calc", "shape"), ("shape", "card"), ("card", "write"),
         ("write", "out")])]
    model = script(monkeypatch, wrap({"op": "question", "node_id": "calc", "text": "拿不准"}))
    await upgrade(client, g)
    [line] = [ln for ln in model.calls[0].splitlines() if ln.startswith("- 「算比率」（calc）")]
    assert "ratio" in line and "vars.shaped.ratio" in line
    assert "orders" not in line                                   # 不读这段代码的指标不列


async def test_deleting_the_code_node_is_refused_and_the_deterministic_result_stays(client, monkeypatch):
    script(monkeypatch, wrap(REWRITE, {"op": "remove_node", "id": "calc"}))
    body = await upgrade(client, graph(summary_llm=True))
    assert body["assist"]["ok"] is False and "assist" not in body["applied"]
    [rejected] = [r for r in body["rejected"] if r["fix_id"] == "assist"]
    assert "降低" in rejected["reason"] and "算比率" in rejected["reason"]
    g = body["graph"]
    assert "calc" in by_id(g) and by_id(g)["write"]["type"] == "report"      # R2 照样生效
    assert by_id(g)["card"]["data"]["config"]["metrics"][0]["expression"] == "vars.calc.ratio"
    assert body["applied"] == ["R2:write"]
    assert any("算比率" in w for w in body["assist"]["warnings"])


async def test_non_arithmetic_code_is_kept_with_a_warning(client, monkeypatch):
    script(monkeypatch, wrap({"op": "question", "node_id": "calc", "text": "它读了一个文件，像是在取数，请确认"}))
    body = await upgrade(client, graph())
    assert body["assist"]["ok"] is True and body["applied"] == []
    assert body["assist"]["questions"] == ["「算比率」：它读了一个文件，像是在取数，请确认"]
    [warning] = body["assist"]["warnings"]
    assert "算比率" in warning and "「证据角色」" in warning and "口径卡" in warning
    assert body["graph"] == graph()
    assert [n["node_id"] for n in body["notes"] if n["rule"] == "R5"] == ["calc"]


async def test_nothing_to_rewrite_means_no_model_call(client, monkeypatch):
    g = graph()
    g["nodes"][2]["data"]["config"]["evidence_role"] = "source"
    model = script(monkeypatch, wrap(REWRITE))
    body = await upgrade(client, g)
    assert model.calls == [] and body["assist"]["ok"] is True and body["assist"]["warnings"] == []
    assert "无需交给助手改写" in body["assist"]["summary"]


async def test_a_new_error_is_sent_back_once_and_fixed(client, monkeypatch):
    broken = {"op": "update_node", "id": "card", "config": {"metrics": [
        {"id": "ratio", "name": "客单价", "expression": "cell(nodes.fetch, 0, 'a') /"}]}}
    model = script(monkeypatch, wrap(broken), [REWRITE, {"op": "done"}])
    body = await upgrade(client, graph())
    assert len(model.calls) == 2 and "上线前自查" in model.calls[1] and "表达式" in model.calls[1]
    assert body["assist"]["ok"] is True and "assist" in body["applied"]
    assert body["assist"]["summary"] == "客单价改由口径卡直接算"                 # 修补轮的 done 不盖掉说明
    expr = by_id(body["graph"])["card"]["data"]["config"]["metrics"][0]["expression"]
    assert expr == REWRITE["config"]["metrics"][0]["expression"]


async def test_a_rewrite_that_stays_broken_is_refused(client, monkeypatch):
    broken = {"op": "update_node", "id": "card", "config": {"metrics": [
        {"id": "ratio", "name": "客单价", "expression": "cell(nodes.fetch, 0, 'a') /"}]}}
    model = script(monkeypatch, wrap(broken), [broken, {"op": "done"}])
    body = await upgrade(client, graph())
    assert len(model.calls) == 3                                               # 最多交回去改两轮
    assert body["assist"]["ok"] is False and "assist" not in body["applied"]
    [rejected] = [r for r in body["rejected"] if r["fix_id"] == "assist"]
    assert "未能改善工作流" in rejected["reason"]
    assert by_id(body["graph"])["card"]["data"]["config"]["metrics"][0]["expression"] == "vars.calc.ratio"


async def test_marking_the_code_as_a_source_is_not_the_copilots_call(client, monkeypatch):
    script(monkeypatch, wrap({"op": "update_node", "id": "calc", "config": {"evidence_role": "source"}}))
    body = await upgrade(client, graph())
    [rejected] = [r for r in body["rejected"] if r["fix_id"] == "assist"]
    assert "「取数」" in rejected["reason"] and body["graph"] == graph()


async def test_without_assist_there_is_no_model_call(client, monkeypatch):
    model = script(monkeypatch, wrap(REWRITE))
    r = await client.post("/api/copilot/upgrade-evidence", json={"graph": graph()})
    assert r.json()["assist"] is None and model.calls == []
