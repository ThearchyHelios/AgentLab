"""报告末尾「分析范围」里的编号点得开（GET /runs/{id}/evidence/analyses/{alias}）：那份分析结论的出处链和点开正文里它的判定语
是同一条（api/evidence._scope_chain），正文没引用的结论（算不了的、可比性检查）也点得开；证据目录里没有的编号 404。

真运行、真算子：照 tests/test_analysis_e2e_claims.py 的虚构零售库 shop（数据目录标了地区、渠道是属性），Agent 查两次库、
做一次变化拆解、一次波动基线（两周历史不够，算不了）、一次可比性检查；写作模型只引用拆解的判定语。模型都换成脚本。
"""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage

from app.analysis import contract
from app.core import artifact_store
from app.engine.evidence import iter_segments
from app.main import app
from tests.test_analysis_e2e_claims import (  # noqa: F401 - 夹具按名字引入
    ARGS, BY_REGION, DECOMPOSE, TOOL, TOTALS, _finish, _graph, _report, engine_up, shop,
)

BASELINE = contract.TOOL_NAMES["baseline"]
COMPARABLE = contract.TOOL_NAMES["comparable"]
TEXT = "## 本周销售\n\n[[a:A1]]。[[see:A1]]"
CHAIN = ["analysis", "analysis_method", "analysis_calc", "analysis_input", "analysis_recompute", "analysis_seal"]


def _script(monkeypatch):
    from app.providers import mock_model

    calls = [(TOOL, {"sql": TOTALS}), (TOOL, {"sql": BY_REGION}), (DECOMPOSE, ARGS),
             (BASELINE, {"query": "Q1", "period": "week", "measure": "gmv", "label": "销售额"}),
             (COMPARABLE, {"query": "Q2", "period": "week", "base": "2026-W41", "target": "2026-W42", "measure": "gmv"})]

    def decide(model, messages):
        if not model.tools:
            return AIMessage(content=TEXT)
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(calls):
            name, args = calls[done]
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{done}"}])
        return AIMessage(content="销售额下降，主要来自华东。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


def _graph_with_tools():
    graph = _graph()
    bot = next(n for n in graph["nodes"] if n["id"] == "bot")
    bot["data"]["config"]["tools"] = [TOOL, DECOMPOSE, BASELINE, COMPARABLE]
    return graph


def _steps(chain):
    """同一步连着几条（几份输入、输入后面接的查询步骤）算一步。"""
    steps = [s["step"] for s in chain]
    return [s for i, s in enumerate(steps) if s.startswith("analysis") and steps[i - 1:i] != [s]]


async def test_scope_aliases_open_the_same_chain_as_the_phrase_in_the_text(monkeypatch):
    _script(monkeypatch)
    row = await _finish(_graph_with_tools())
    assert row.status == "succeeded", row.error
    _, doc, _ = await _report(row)
    scope = doc["analysis_scope"]
    assert [i["alias"] for i in scope["decomposed"]] == ["A1"]
    assert [i["alias"] for i in scope["refused"]] == ["A2"] and [i["alias"] for i in scope["phenomena"]] == ["A3"]
    phrase = next(s for s in iter_segments(doc) if s.get("ref") == "a:A1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cited = (await client.get(f"/api/runs/{row.id}/evidence/segments/{phrase['id']}")).json()
        body = (await client.get(f"/api/runs/{row.id}/evidence/analyses/A1")).json()
        # 和点开正文里的判定语同一条链（同一份结论、同样复算一致），只是没有这一处引用的那几样
        assert _steps(body["chain"]) == CHAIN
        assert [s["step"] for s in body["chain"]] == [s["step"] for s in cited["chain"]]
        assert body["rendered"] == phrase["text"] == body["chain"][0]["phrase"]
        assert body["seal"]["covered"] is True and body["report"]["node_id"] == "write"

        # 正文没引用的也点得开：算不了的波动基线照实给原因，可比性检查照样复算
        refused = (await client.get(f"/api/runs/{row.id}/evidence/analyses/A2", params={"report": "write"})).json()
        judged = refused["chain"][0]
        assert (judged["op"], judged["status"], judged["reason_code"]) == ("baseline", "refused", "history_short")
        assert judged["phrase"] == scope["refused"][0]["phrase"]
        assert next(s for s in refused["chain"] if s["step"] == "analysis_recompute")["status"] == "same"
        comparable = (await client.get(f"/api/runs/{row.id}/evidence/analyses/A3")).json()
        assert comparable["chain"][0]["method_label"] == contract.method_label("comparable")

        # 目录里没有的编号、不是分析结论的编号（Q1）：404，说清是哪种
        for alias in ("A9", "Q1"):
            missing = await client.get(f"/api/runs/{row.id}/evidence/analyses/{alias}")
            assert missing.status_code == 404 and missing.json()["code"] == "evidence_analysis_not_found"


async def test_a_tampered_report_does_not_open_its_scope(monkeypatch):
    _script(monkeypatch)
    row = await _finish(_graph_with_tools())
    checked, _, _ = await _report(row)
    # 报告文档被改过（哈希对不上）：和片段接口同一个错误码，不给出处
    path = artifact_store._path_of(checked["doc_artifact"])
    path.write_bytes(path.read_bytes().replace("本周销售".encode(), "本周营业".encode(), 1))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get(f"/api/runs/{row.id}/evidence/analyses/A1")
        assert res.status_code == 409 and res.json()["code"] == "evidence_doc_tampered"
