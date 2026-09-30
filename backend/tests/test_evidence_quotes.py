"""引文：[[q:K1|原话]] 必须在那次检索命中的片段里逐字出现。

知识库的结论以前只能写成 [[see:K1]]：读者知道依据是哪次检索，看不到是哪一句。引文把
原话写进报告，系统拿检索快照（取回时复验哈希）逐字核对：

- 空白归一化后逐字出现才算有出处；改一个字就是没有出处（state none，issue unresolved_ref）
- 正文里显示引文本身；目录里根本没有这次检索时显示占位，和别的解析不了的引用一样
- eid 带上命中片段的定位：第几条命中、原文里的起止
- 出口复核重新取快照、重新比对，不信文档里记的结论
"""
from __future__ import annotations

import asyncio
import random

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.evidence import (
    LEGACY_LATER_REASON,
    PHASE_TWO_REASON,
    StreamRenderer,
    build_catalog,
    catalog_prompt,
    compose_doc,
    iter_segments,
    make_eid,
    render_markers,
    verify_doc,
)
from app.engine.runner import run_manager

K_ART = "c" * 64
HITS = [
    {"chunk_id": "ch-1", "document_id": "doc-1", "title": "售后月报", "ordinal": 3,
     "content": "九月退款主要集中在\n东区，原因是物流延误。退款率 2.35%，高于上月。"},
    {"chunk_id": "ch-2", "document_id": "doc-2", "title": "Ops notes", "ordinal": 0,
     "content": "Net  revenue\nrose after the promotion ended. 物流延误已经解决。"},
]
SNAPSHOT = {"query": "退款", "collection": "kb", "hits": HITS}


def loader_of(content):
    def load(artifact):
        return content if artifact == K_ART else None
    return load


LOAD = loader_of(SNAPSHOT)


@pytest.fixture
def catalog():
    return build_catalog(nodes={}, ledger=[{"kind": "retrieval", "node_id": "kb", "exec": 1, "artifact": K_ART,
                                            "source": "kb", "rows": len(HITS)}])


def quotes(doc):
    return [s for s in iter_segments(doc) if s["kind"] == "quote"]


def test_verbatim_quote_resolves_with_its_location(catalog):
    doc = compose_doc("售后说「[[q:K1|退款主要集中在东区]]」。[[see:K1]]", catalog, loader=LOAD)
    [seg] = quotes(doc)
    assert doc["markdown"] == "售后说「退款主要集中在东区」。"
    assert seg["text"] == "退款主要集中在东区" and seg["state"] == "deterministic"
    cite = seg["cite"]
    assert cite["kind"] == "quote" and cite["role"] == "quote" and cite["alias"] == "K1"
    loc = cite["locator"]
    assert loc["hit"] == 0 and HITS[0]["content"][loc["start"]:loc["end"]] == "退款主要集中在\n东区"
    assert cite["eid"] == make_eid("quote", K_ART, loc)
    assert cite["source"] == {"artifact": K_ART, "document": "doc-1", "chunk": "ch-1", "title": "售后月报",
                              "ordinal": 3}
    assert doc["violations"] == [] and doc["stats"]["quotes"] == 1
    assert doc["blocks"][0]["units"][0]["cites"] == ["K1"]


def test_one_changed_character_is_not_a_quote(catalog):
    doc = compose_doc("售后说「[[q:K1|退款主要集中在西区]]」。", catalog, loader=LOAD)
    [seg] = quotes(doc)
    # 正文照样显示引文本身，只是标成没有出处
    assert seg["text"] == "退款主要集中在西区" and seg["state"] == "none" and seg["issue"] == "unresolved_ref"
    assert "找不到这段引文的原文" in seg["cite"]["reason"]
    assert [v["code"] for v in doc["violations"]] == ["unresolved_ref"]
    assert doc["stats"]["quotes"] == 0


@pytest.mark.parametrize("quote", [
    "Net revenue rose after the promotion ended.",
    "Net revenue rose",
    "退款率 2.35%，高于上月",
    "退款主要集中在 东区",
])
def test_whitespace_is_normalised(catalog, quote):
    doc = compose_doc(f"原话：[[q:K1|{quote}]]", catalog, loader=LOAD)
    [seg] = quotes(doc)
    assert seg["state"] == "deterministic", seg["cite"].get("reason")
    loc = seg["cite"]["locator"]
    assert " ".join(HITS[loc["hit"]]["content"][loc["start"]:loc["end"]].split()).replace(" ", "") \
        == quote.replace(" ", "")


@pytest.mark.parametrize("quote", ["Netrevenue rose", "net revenue rose", "退款主要集中在东区。九月"])
def test_normalising_is_not_fuzzy(catalog, quote):
    doc = compose_doc(f"原话：[[q:K1|{quote}]]", catalog, loader=LOAD)
    assert quotes(doc)[0]["state"] == "none"


def test_numbers_inside_a_verified_quote_are_not_bare(catalog):
    doc = compose_doc("[[q:K1|退款率 2.35%，高于上月]]", catalog, loader=LOAD)
    assert doc["violations"] == [] and doc["stats"]["uncited_numbers"] == 0


@pytest.mark.parametrize("text, words", [
    ("[[q:K9|退款主要集中在东区]]", "检索结果 K9 不存在"),
    ("[[q:K1|]]", "引文为空"),
    ("[[q:K1|东区]]", "太短"),
])
def test_quotes_that_cannot_be_checked(catalog, text, words):
    doc = compose_doc(text, catalog, loader=LOAD)
    [seg] = quotes(doc)
    assert seg["state"] == "none" and words in seg["cite"]["reason"], seg["cite"]["reason"]


def test_missing_retrieval_shows_a_placeholder(catalog):
    doc = compose_doc("[[q:K9|退款主要集中在东区]]。", catalog, loader=LOAD)
    assert doc["markdown"] == "⟦?q:K9|退款主要集中在东区⟧。"


def test_tampered_snapshot_is_not_evidence(catalog):
    def tampered(artifact):
        raise ValueError("hash mismatch")

    doc = compose_doc("[[q:K1|退款主要集中在东区]]", catalog, loader=tampered)
    [seg] = quotes(doc)
    assert seg["state"] == "none" and "哈希" in seg["cite"]["reason"]


def test_exit_check_rereads_the_snapshot(catalog):
    doc = compose_doc("[[q:K1|物流延误已经解决]]。", catalog, loader=LOAD)
    assert doc["violations"] == [] and quotes(doc)[0]["cite"]["locator"]["hit"] == 1
    changed = {**SNAPSHOT, "hits": [HITS[0], {**HITS[1], "content": "物流延误仍在持续。"}]}
    checked = verify_doc(doc, catalog, loader=loader_of(changed))
    assert [v["code"] for v in checked["violations"]] == ["unresolved_ref"]
    assert verify_doc(doc, catalog, loader=LOAD)["ok"] is True


def phase_two(doc):
    """把文档改回二期组装的样子：那时 q / t / c 一律判为解析不了，正文是占位，原因是 LEGACY_LATER_REASON。
    占位的字和现在「目录里没有 K9」时一样，只有记下的原因不同。"""
    for seg in iter_segments(doc):
        if seg.get("issue") == "unresolved_ref" and seg["ref"][:2] in ("q:", "t:", "c:"):
            seg["cite"]["reason"] = LEGACY_LATER_REASON
    return doc


def test_quotes_from_before_the_upgrade_are_rechecked_the_old_way(catalog):
    """二期的报告文档（升级前组装，停在报告和出口之间）升级后被复核：原话今天查得到也不能算
    「渲染对不上」——那会被出口当成完整性问题记缺口。按当时的规矩：占位，解析不了。"""
    doc = phase_two(compose_doc("售后说[[q:K1|退款主要集中在东区]]。", {}, loader=LOAD))
    [seg] = quotes(doc)
    assert seg["text"] == "⟦?q:K1|退款主要集中在东区⟧"
    checked = verify_doc(doc, catalog, loader=LOAD)
    assert [v["code"] for v in checked["violations"]] == ["unresolved_ref"]
    assert PHASE_TWO_REASON in checked["violations"][0]["message"]
    # 只认占位：正文被改成了引文本身、又自称是二期的，照样按现在的规矩查
    seg["text"] = "退款主要集中在东区"
    doc["markdown"] = "售后说退款主要集中在东区。"
    tail = [x for x in iter_segments(doc)][-1]
    seg["span"] = [3, 3 + len(seg["text"])]
    tail["span"] = [seg["span"][1], seg["span"][1] + len(tail["text"])]
    [unit] = [u for b in doc["blocks"] for u in b["units"]]
    unit["span"] = [0, tail["span"][1]]
    assert [v["code"] for v in verify_doc(doc, catalog, loader=LOAD)["violations"]] == ["state_mismatch"]


def test_entity_markers_from_before_the_upgrade_are_rechecked_the_old_way():
    ledger = [{"kind": "query", "node_id": "f", "exec": 1, "artifact": "a" * 64, "tool": "db_query__w", "source": "w",
               "columns": ["gmv"], "rows": 1, "truncated": False, "schema_artifact": "5" * 64, "tables": ["orders"]},
              {"kind": "schema", "node_id": "f", "exec": 1, "artifact": "5" * 64, "source": "w",
               "tables": {"orders": ["id", "gmv"]}}]
    doc = phase_two(compose_doc("看 [[t:orders]] 和 [[c:orders.gmv]]。", {}))
    assert [s["text"] for s in iter_segments(doc) if s.get("ref")] == ["⟦?t:orders⟧", "⟦?c:orders.gmv⟧"]
    checked = verify_doc(doc, build_catalog(nodes={}, ledger=ledger))
    assert [v["code"] for v in checked["violations"]] == ["unresolved_ref", "unresolved_ref"]


def test_streaming_equals_full_render(catalog):
    text = "开头 [[q:K1|退款主要集中在东区]]，又说 [[q:K1|退款主要集中在西区]]；[[q:K9|无此检索]] 完。[[see:K1]]"
    whole = render_markers(text, catalog, loader=LOAD)
    assert whole == "开头 退款主要集中在东区，又说 退款主要集中在西区；⟦?q:K9|无此检索⟧ 完。"
    rng = random.Random(11)
    for _ in range(200):
        renderer = StreamRenderer(catalog, loader=LOAD)
        out, i = [], 0
        while i < len(text):
            step = rng.randint(1, 7)
            out.append(renderer.feed(text[i:i + step]))
            i += step
        out.append(renderer.flush())
        assert "".join(out) == whole


def test_catalog_prompt_shows_hits_to_quote_from(catalog):
    prompt = catalog_prompt(catalog, loader=LOAD)
    assert "[[q:K1|" in prompt and "逐字" in prompt and "[[see:K1]]" in prompt
    assert "售后月报" in prompt and "退款主要集中在" in prompt and "Ops notes" in prompt


# --------------------------------------------------------------------------
# 跑一次：检索 → 报告撰写，引错了原话会被要求重写
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def test_report_node_checks_quotes_against_the_retrieval(engine_up, monkeypatch):
    from app.memory import kb
    from app.providers import mock_model

    async def fake_search(session, **kw):
        return [dict(HITS[0], score=0.9)]

    monkeypatch.setattr(kb, "search", fake_search)
    replies = ["售后说「[[q:K1|退款主要集中在西区]]」。[[see:K1]]", "售后说「[[q:K1|退款主要集中在东区]]」。[[see:K1]]"]
    seen: list = []

    def decide(self, messages):
        seen.append(list(messages))
        return AIMessage(content=replies[min(len(seen), len(replies)) - 1])

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    nodes = [{"id": nid, "type": t, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": cfg}} for nid, t, cfg in [
        ("start", "input", {}), ("docs", "retrieve", {"query": "退款", "collection": "kb"}),
        ("write", "report", {"instructions": "引用售后的原话"}),
        ("out", "output", {"fields": [{"name": "r", "value": "{{ nodes.write.text }}"}]})]]
    graph = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "succeeded", row.error
    assert row.output["r"] == "售后说「退款主要集中在东区」。"

    first = "\n".join(str(m.content) for m in seen[0])
    assert "[[q:K1|" in first and "退款主要集中在" in first and "售后月报" in first
    assert len(seen) == 2 and "找不到这句原话" in str(seen[1][-1].content)
    async with SessionLocal() as session:
        [checked] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "report.checked"))).scalars().all()
    assert checked.data["ok"] is True and checked.data["repairs"] == 1 and checked.data["stats"]["quotes"] == 1
    doc = artifact_store.load(checked.data["doc_artifact"])
    [seg] = quotes(doc)
    retrieval = doc["catalog"]["K1"]["artifact"]
    assert seg["cite"]["eid"] == make_eid("quote", retrieval, seg["cite"]["locator"])
    assert seg["cite"]["source"]["chunk"] == "ch-1"
