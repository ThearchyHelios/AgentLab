"""片段接口的 on_demand（这句能不能请模型判断）在正式运行里照实说。

验收发现：正式运行的片段接口一律答「正式运行的结论句已在报告撰写节点内完成裁判」，可那次报告的结论句策略是不裁判
（report.checked 记 claims: off，文档里没有裁判摘要），节点里根本没判过；标题这种不是结论句的片段也答同一句。现在：

- 不是结论句的先说（not_a_claim）：标题、连接性文字不论哪类运行都无需裁判
- 正式运行、节点里确实判过（文档里有当场裁判的摘要）：照旧说已在节点内完成裁判
- 正式运行、没开结论句裁判（不裁判、只要求附依据，或者升级前的运行）：照实说这份报告没有开启结论句裁判
- 正式运行、开了裁判节点里却没判（报告没通过核对，节点紧接着判失败，不再花钱裁判）：照实说没有在节点内完成裁判
- 按需裁判接口（POST …/evidence/judge）对正式运行回 409 时说同一句话

写作、裁判都用 MockChatModel 照剧本，不调真接口。
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run
from app.engine import judge as judge_mod
from app.engine.evidence import compose_doc, iter_units
from app.engine.judge import JUDGE_ROLE
from app.engine.runner import run_manager
from app.main import app
from app.providers.mock_model import MockChatModel

METRICS = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"}]
#: 一个标题、一句挂了依据的结论、一句没挂依据的归因
TEXT = "## 本周\n\n本周销售额 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客首单。"
#: 标题那一句（正文里去掉了「## 」），和没挂依据的归因
HEADING, CAUSAL = "本周", "增长主要来自新客首单。"
JUDGED_WORDS = "已在报告撰写节点内完成裁判"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(**report) -> dict:
    nodes = [
        node("start", "input"),
        node("fetch", "transform", mode="expression", expression="{'gmv': 45678.5}", assign_to="kpi"),
        node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=METRICS),
        node("write", "report", instructions="写周报", **report),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


def models(monkeypatch) -> list[str]:
    """写作者回 TEXT；裁判（claims: judge 时节点里当场判）一律判有依据。返回交给裁判的请求原文。"""
    judged: list[str] = []

    def decide(chat, messages):
        if JUDGE_ROLE in str(messages[0].content):
            request = str(messages[-1].content)
            judged.append(request)
            items = [{"unit": uid, "verdict": "supported", "rationale": "有依据", "used": []}
                     for uid, _ in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M)]
            return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
        return AIMessage(content=TEXT)

    monkeypatch.setattr(MockChatModel, "_decide", decide)

    async def judge_model_of(session, spec):
        return MockChatModel(model_name=spec.model or "judge-x"), spec.model or "judge-x"

    monkeypatch.setattr(judge_mod, "get_chat_model", judge_model_of)
    return judged


async def finish(graph, *, run_class="formal") -> Run:
    run = await run_manager.start(graph=graph, input_payload={}, run_class=run_class)
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            assert row.status == "succeeded", row.error
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


def unit_of(doc: dict, text: str) -> str:
    md = doc["markdown"]
    return next(u["id"] for _, u in iter_units(doc) if md[u["span"][0]:u["span"][1]].strip() == text)


async def on_demand(client, row: Run, text: str) -> dict:
    doc = artifact_store.load(row.output["_evidence"]["doc_artifact"])
    uid = unit_of(doc, text)
    seg = next(s["id"] for _, u in iter_units(doc) if u["id"] == uid for s in u["segments"] if s["kind"] != "structural")
    res = await client.get(f"/api/runs/{row.id}/evidence/segments/{seg}?report=write")
    assert res.status_code == 200, res.text
    return res.json()["unit"]["on_demand"]


# --------------------------------------------------------------------------
# 片段接口
# --------------------------------------------------------------------------


@pytest.mark.parametrize("claims", [None, "off", "require_citation"])
async def test_a_formal_report_without_claim_judging_says_it_was_not_judged(client, monkeypatch, claims):
    """验收原样：正式运行、结论句策略不裁判（没写也一样，或者只要求附依据）。不能说「已在节点内完成裁判」。"""
    judged = models(monkeypatch)
    row = await finish(weekly(**({"claims": claims} if claims else {})))
    got = await on_demand(client, row, CAUSAL)
    assert got["available"] is False and got["reason"] == "formal", got
    assert JUDGED_WORDS not in got["message"] and "没有开启结论句裁判" in got["message"], got["message"]
    assert judged == [], "节点里确实没请裁判"


async def test_a_heading_in_a_formal_run_is_not_a_claim(client, monkeypatch):
    """标题不是结论句：不论哪类运行都答 not_a_claim，不答「已在节点内完成裁判」。"""
    models(monkeypatch)
    row = await finish(weekly())
    got = await on_demand(client, row, HEADING)
    assert got["available"] is False and got["reason"] == "not_a_claim" and JUDGED_WORDS not in got["message"], got


async def test_a_formal_report_judged_in_the_node_says_so(client, monkeypatch):
    """开了裁判、节点里当场判过：照旧说已完成裁判；标题照样是 not_a_claim。"""
    judged = models(monkeypatch)
    row = await finish(weekly(claims="judge"))
    assert judged, "正式运行在节点里当场裁判"
    got = await on_demand(client, row, CAUSAL)
    assert got["available"] is False and got["reason"] == "formal" and JUDGED_WORDS in got["message"], got
    assert (await on_demand(client, row, HEADING))["reason"] == "not_a_claim"


async def test_the_judge_endpoint_says_the_same_for_a_formal_report_without_judging(client, monkeypatch):
    """按需裁判接口对正式运行照旧 409，说的话和片段接口一致：没开裁判的照实说没开。"""
    models(monkeypatch)
    row = await finish(weekly())
    doc = artifact_store.load(row.output["_evidence"]["doc_artifact"])
    res = await client.post(f"/api/runs/{row.id}/evidence/judge", json={"units": [unit_of(doc, CAUSAL)]})
    assert res.status_code == 409 and res.json()["code"] == "evidence_judge_formal", res.text
    detail = res.json()["detail"]
    assert JUDGED_WORDS not in detail and "没有开启结论句裁判" in detail, detail
    assert detail == (await on_demand(client, row, CAUSAL))["message"]
    # 点名的不是报告撰写节点：认不出是哪份报告，不替它说判没判过，只说正式运行不能按需裁判
    res = await client.post(f"/api/runs/{row.id}/evidence/judge", json={"units": ["u1"], "report": "caliber"})
    assert res.status_code == 409 and res.json()["code"] == "evidence_judge_formal", res.text
    assert res.json()["detail"] == "正式运行的结论句只在报告撰写节点内裁判，不能按需裁判"


# --------------------------------------------------------------------------
# 手搭：开了裁判、节点里却没判（报告没通过核对，节点紧接着判失败）
# --------------------------------------------------------------------------


def test_a_formal_report_that_opted_in_but_was_never_judged_says_so():
    """claims: judge 的正式运行，报告没通过核对、节点判失败之前不再花钱裁判：文档里没有裁判摘要。不能说已完成裁判。"""
    from app.api.evidence import _on_demand, _Sealed

    doc = compose_doc("增长主要来自新客首单。", {}, node_id="write", run_id="r-unjudged")
    [unit] = [u for _, u in iter_units(doc)]
    sealed = _Sealed(run_id="r-unjudged", graph={}, events=[], run_class="formal", status="failed", ledger_on=True,
                     seal={"sealed": True, "ok": True, "manifest_seq": 3, "legacy": False})
    got = _on_demand(sealed, doc, unit["id"], None, claims="judge")
    assert got["available"] is False and got["reason"] == "formal", got
    assert JUDGED_WORDS not in got["message"] and "没有在报告撰写节点内完成裁判" in got["message"], got["message"]
    # 同一份文档带上当场裁判的摘要、这句判过了：照旧说已完成裁判
    judged = {**doc, "judge": {"mode": "inline", "counts": {}}}
    assert JUDGED_WORDS in _on_demand(sealed, judged, unit["id"], {"status": "supported"}, claims="judge")["message"]
    # 不传策略（老调用方）：看文档里有没有当场裁判的摘要
    assert "没有开启结论句裁判" in _on_demand(sealed, doc, unit["id"], None)["message"]


@pytest.mark.parametrize("verdict", [
    # 触顶：这句没判成，判定里记着原因
    {"status": "unjudged", "reason": "max_claims", "rationale": "已达上限（每份报告最多裁判 1 句），这句未裁判"},
    # 裁判出错、格式不对、被截断都一样是未裁判
    {"status": "unjudged", "reason": "error", "rationale": "裁判失败：连接超时"},
    # 文档里压根没有这句的判定
    None,
])
def test_an_inline_judged_report_that_did_not_judge_this_sentence_says_so(verdict):
    """正式运行在节点里当场裁判过（文档里有裁判摘要），可这一句没判成：触顶、出错，或者没有判定。不能说已完成裁判，
    照实说这句没判成、原因在判定里；判过的句子照旧说已完成裁判。按需裁判接口一次点了几句、有没判成的，也照实说。"""
    from app.api.evidence import FORMAL_INLINE_MISSED_SOME, _formal_message, _on_demand, _Sealed

    doc = compose_doc("增长主要来自新客首单。复购率也在上升。", {}, node_id="write", run_id="r-capped")
    doc["judge"] = {"mode": "inline", "complete": False, "counts": {"unjudged": 1}, "limits_hit": ["max_claims"]}
    first, second = [u["id"] for _, u in iter_units(doc)]
    sealed = _Sealed(run_id="r-capped", graph={}, events=[], run_class="formal", status="succeeded", ledger_on=True,
                     seal={"sealed": True, "ok": True, "manifest_seq": 3, "legacy": False})
    got = _on_demand(sealed, doc, first, verdict, claims="judge")
    assert got["available"] is False and got["reason"] == "formal", got
    assert JUDGED_WORDS not in got["message"] and "不能按需裁判" in got["message"], got["message"]
    if verdict is None:
        # 没有判定可看：不说「原因见判定」
        assert "这句没有判定" in got["message"] and "原因见判定" not in got["message"], got["message"]
    else:
        assert "这句没有判成" in got["message"] and "原因见判定" in got["message"], got["message"]
    # 判过的那一句照旧说已完成裁判
    assert JUDGED_WORDS in _on_demand(sealed, doc, second, {"status": "supported"}, claims="judge")["message"]
    # 一次问几句（按需裁判接口）：有一句没判成就照实说
    assert _formal_message(doc, "judge", [verdict, {"status": "supported"}]) == FORMAL_INLINE_MISSED_SOME
    assert _formal_message(doc, "judge", [{"status": "supported"}, {"status": "partial"}]).startswith("正式运行的结论句已在")
