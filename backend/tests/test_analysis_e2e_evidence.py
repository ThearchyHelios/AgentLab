"""分析结论从入口到证据的整条链路，接真算子、真复算（不用替身）：分析卡（W1-c）、agent 分析工具（W1-c）、口径卡指标上的
两期可比性（W1-c）各发起一次运行，算子是 W1-a 的，证据接入（W1-b）的片段接口、证据图、审计、报告导出、可验证报告都按
封存的输入复算一遍、逐字段相同。

库是虚构的零售库 shop（tests/test_analysis_card：orders(region, week, amount)，华东、华南、华北两周）。写作模型换成脚本
（MockChatModel._decide），不调用任何模型。
"""
from __future__ import annotations

import json
import re

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage

from app.analysis import contract
from app.core import artifact_store
from app.engine.evidence import iter_segments
from app.main import app
from tests.test_analysis_card import REGION, WEEKLY, edge, engine_up, node, run_graph, shop  # noqa: F401 - 夹具按名字引入
from tests.test_analysis_tools import DECOMPOSE, DECOMPOSE_ARGS, SQL, TOOL

TEXT = "## 异动\n\n[[a:A1]]。华东占 [[a:A1.top_share|pct]]，变化 [[v:A1.r0.delta]]。[[see:A1]]"
WHERE = {"id": "where", "op": "decompose", "mode": "additive", "from": "by_region",
         "params": {"by": ["region"], "period": "week", "base": "2026-W41", "target": "2026-W42", "measure": "gmv",
                    "headline": {"base": "weekly.r0.gmv", "target": "weekly.r1.gmv"}, "label": "销售额"},
         "hypothesis": "下降是不是集中在某个地区"}
#: 判定语的出处链：判定 → 方法 → 计算 → 输入（两格总数 + 拆解的那份快照，后面接两份快照的查询步骤）→ 复算 → 封存
CHAIN = ["analysis", "analysis_method", "analysis_calc", "analysis_input", "analysis_input", "analysis_input", "query",
         "query", "analysis_recompute", "analysis_seal"]
#: 片段：判定语（系统渲染）、具名标量、结果表的一格
SEGMENTS = {"a:A1": ("analysis", "销售额下降 194（-9.2%），主要来自华东（-120，占 61.9%）"),
            "a:A1.top_share|pct": ("number", "61.9%"), "v:A1.r0.delta": ("number", "-120")}


def _report(*before, contract_=None):
    """报告撰写 → 成果（带契约，报告来自 write）；before 是报告前面的节点，按顺序连成一条。"""
    nodes = [node("start", "input"), *before, node("write", "report", instructions="写周报", on_violation="flag"),
             node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
                  contract=contract_ or {"report_from": "write"})]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [edge(a, b) for a, b in zip(ids, ids[1:])]}


def _segments(row):
    doc = artifact_store.load(row.output["_evidence"]["doc_artifact"])
    return {s["ref"]: s for s in iter_segments(doc) if s.get("ref")}


def _embedded(html: str, element: str) -> dict:
    return json.loads(re.search(rf'<script type="application/json" id="{element}">(.*?)</script>', html, re.S).group(1))


async def test_a_card_conclusion_opens_and_rechecks_everywhere(monkeypatch, shop):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=TEXT))
    graph = _report(node("weekly", "tool", tool="db_query__shop", args={"sql": WEEKLY}),
                    node("by_region", "tool", tool="db_query__shop", args={"sql": REGION}),
                    node("card", "analysis", items=[WHERE]))
    row = await run_graph(graph)
    assert row.status == "succeeded", row.error
    segs = _segments(row)
    assert {ref: (s["kind"], s["text"]) for ref, s in segs.items()} == SEGMENTS
    assert not [s for s in segs.values() if s.get("issue")]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{segs['a:A1']['id']}")).json()
        assert [s["step"] for s in body["chain"]] == CHAIN
        judged = body["chain"][0]
        assert (judged["origin"], judged["item"]) == ("card", "where")
        again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
        assert again["status"] == "same" and again["method"] == "decompose.additive@1", again
        inputs = [s for s in body["chain"] if s["step"] == "analysis_input"]
        assert [s["role"] for s in inputs] == ["headline_base", "headline_target", "data"]
        assert all(s["sealed"] for s in inputs)
        assert [s["closure"]["closed"] for s in inputs if s.get("closure")] == [True, True]
        assert body["seal"]["covered"] is True, body["seal"]

        graph_view = (await client.get(f"/api/runs/{row.id}/evidence")).json()
        a1 = next(e for e in graph_view["evidence"] if e["alias"] == "A1")
        assert {k: a1.get(k) for k in ("kind", "op", "verdict", "item", "sealed")} == {
            "kind": "analysis", "op": "decompose", "verdict": "driver", "item": "where", "sealed": True}

        # 报告导出：判定语这一处的出处链同样复算一致
        data = _embedded((await client.get(f"/api/runs/{row.id}/report.html")).text, "report-data")
        chain = data["chains"][data["refs"][segs["a:A1"]["id"]]]
        assert next(s for s in chain if s["step"] == "analysis_recompute")["status"] == "same"

        # 可验证报告：结论按原字节嵌入、导出时服务端复算一致；分析卡那一项的 analysis.end 带规范字符串
        bundle = await client.get(f"/api/runs/{row.id}/bundle.html")
        assert bundle.status_code == 200, bundle.text[:300]
        b = _embedded(bundle.text, "bundle-data")
        items = [a for a in b["artifacts"] if a["kind"] == contract.ARTIFACT_KIND]
        assert items and all("b64" in a and a["recheck"]["status"] == "same" for a in items)
        ends = [e for e in b["events"] if e["type"] == "analysis.end"]
        assert ends and all("canonical" in e for e in ends)

        audit = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
        rows = [r for g in audit["groups"] for r in g["rows"] if r.get("ref") == "a:A1"]
        assert [(r["kind"], r["evidence_kind"], r["sealed"]) for r in rows] == [("analysis", "analysis", True)]


async def test_an_agent_conclusion_opens_and_rechecks_everywhere(monkeypatch, shop):
    from app.providers import mock_model

    calls = [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS)]

    def decide(model, messages):
        if not model.tools:
            return AIMessage(content=TEXT)
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(calls):
            name, args = calls[done]
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{done}"}])
        return AIMessage(content="销售额下降，主要来自华东。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    row = await run_graph(_report(node("bot", "agent", prompt="销售额为什么下降", tools=[TOOL, DECOMPOSE],
                                       approval="never")))
    assert row.status == "succeeded", row.error
    segs = _segments(row)
    assert {ref: (s["kind"], s["text"]) for ref, s in segs.items()} == SEGMENTS

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{segs['a:A1']['id']}")).json()
        judged = body["chain"][0]
        assert (judged["origin"], judged["call_id"]) == ("agent", "call_1")
        again = next(s for s in body["chain"] if s["step"] == "analysis_recompute")
        assert again["status"] == "same", again

        b = _embedded((await client.get(f"/api/runs/{row.id}/bundle.html")).text, "bundle-data")
        # 分析工具的 tool.end（预览里是结论的前一截）和结论同一个认法：结论随文件附带得了才带内容
        ends = [e for e in b["events"] if e["type"] == "tool.end" and e["node_id"] == "bot"]
        assert len(ends) == 2 and all("canonical" in e for e in ends)
        assert [a["recheck"]["status"] for a in b["artifacts"] if a["kind"] == contract.ARTIFACT_KIND] == ["same"]


async def test_a_metric_comparability_opens_and_rechecks_everywhere(monkeypatch, shop):
    from app.providers import mock_model

    this = "SELECT SUM(amount) AS gmv FROM orders WHERE week = '2026-W42'"
    last = "SELECT SUM(amount) AS gmv FROM orders WHERE week = '2026-W41'"
    wow = "(cell(nodes.this_week, 0, 'gmv') - cell(nodes.last_week, 0, 'gmv')) / cell(nodes.last_week, 0, 'gmv')"
    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content="环比 [[m:wow]]。"))
    graph = _report(node("this_week", "tool", tool="db_query__shop", args={"sql": this}),
                    node("last_week", "tool", tool="db_query__shop", args={"sql": last}),
                    node("card", "metrics", caliber="周报口径",
                         metrics=[{"id": "wow", "name": "环比", "expression": wow, "format": "percent_of_ratio",
                                   "decimals": 3}]))
    row = await run_graph(graph)
    assert row.status == "succeeded", row.error
    segs = _segments(row)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get(f"/api/runs/{row.id}/evidence/segments/{segs['m:wow']['id']}")).json()
        view = body["chain"][0]["comparability"]
        # 表里只有周次这一列文字、没有业务日期：窗口长度、整期、星期构成核对不了，不判不可比
        assert (view["verdict"], view["phrase"], view["reasons"]) == (
            "comparable", "两期可比，窗口长度、整期、星期构成核对不了", [])
        assert view["sealed"] is True and view["hash_ok"] is True and view["method"] == "comparable@1"
        steps = view["steps"]
        assert [s["step"] for s in steps] == ["analysis", "analysis_method", "analysis_calc", "analysis_input",
                                             "analysis_input", "analysis_recompute", "analysis_seal"]
        assert steps[-2]["status"] == "same"
        assert (steps[-1]["via"], steps[-1]["sealed"]) == ("metric_set", True)

        b = _embedded((await client.get(f"/api/runs/{row.id}/bundle.html")).text, "bundle-data")
        found = [a for a in b["artifacts"] if a["kind"] == contract.ARTIFACT_KIND]
        assert len(found) == 1 and "b64" in found[0] and found[0]["recheck"]["status"] == "same"
