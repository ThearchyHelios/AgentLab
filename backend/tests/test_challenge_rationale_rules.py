"""反方审稿的理由：依据写进括号不再断句，占位词不当理由（期 5 真实模型验收 M1）。

真实模型验收里，反方的理由「先按订单状态做全量概览核对口径（[[see:Q1]]）：」「复算写作者的原查询（同 SQL 同过滤条件，
[[see:Q2]]）」渲染成「（）：」「（同 SQL 同过滤条件，）」：审稿记录（rationale 和 rationale_doc）随正式运行封存，审批卡上
照片段原文显示。

模型一律是 MockChatModel 剧本，区域、金额都是虚构的。
"""
from __future__ import annotations

from app.core import artifact_store
from tests.test_challenge_node import (  # noqa: F401 - 夹具按名字引入
    NET,
    REFUNDS,
    Models,
    Play,
    engine_up,
    finish,
    graph,
    output_of,
    shop,
)

BROKEN = ("（）", "（，", "，）", "()", "(,", ",)")


async def _record(monkeypatch, shop, plays) -> tuple[dict, dict]:
    Models(monkeypatch, shop, plays=plays)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    out = await output_of(row.id, "chal")
    return artifact_store.load(out["challenge_artifact"]), row.output["_issuance"]


async def test_see_in_brackets_does_not_break_the_rationale(monkeypatch, shop):
    plays = {
        "华南销售额超过华东": Play([NET], "refuted", "扣除退款后华南为 [[v:Q1.r1.净销售额]]（[[see:Q1]]），"
                                                "仍低于华东的 [[v:Q1.r0.净销售额]]（同口径，[[see:Q1]]）。"),
        "居各区域之首": Play([REFUNDS], "weakened", "先核对退款口径（[[see:Q1]]）：华东含 [[v:Q1.r0.退款笔数]] 笔退款"
                                              "（[[see:Q1]]，扣除后仍居首）。"),
    }
    record, issuance = await _record(monkeypatch, shop, plays)
    first, second = record["claims"]
    assert first["rationale"] == "先核对退款口径：华东含 1 笔退款（扣除后仍居首）。"
    assert second["rationale"] == "扣除退款后华南为 900，仍低于华东的 1,200（同口径）。"
    for claim in (first, second):
        doc = claim["rationale_doc"]
        assert doc["markdown"] == claim["rationale"] and doc["violations"] == []
        assert not any(mark in claim["rationale"] for mark in BROKEN)
    # 括号里的依据仍挂在句子上：判「被推翻」的引用到了自己的查询，照常采信、出具降档
    assert second["verdict"] == "refuted" and issuance["tier"] == "degraded"


async def test_a_placeholder_rationale_is_recorded_as_no_rationale(monkeypatch, shop):
    """反方交回的理由只是占位词：记录里理由为空（审批卡据此写「反方没有给出理由」），判「被推翻」的照常不予采信。"""
    plays = {"华南销售额超过华东": Play([NET], "refuted", "placeholder"),
             "居各区域之首": Play([REFUNDS], "stands", "placeholder")}
    record, issuance = await _record(monkeypatch, shop, plays)
    first, second = record["claims"]
    assert (first["verdict"], first["rationale"], first["rationale_doc"]) == ("stands", "", None)
    assert second["verdict"] == "inconclusive"
    assert second["rationale"] == "理由没有引用到任何查询结果，反方给出的「被推翻」不予采信，记为无法判断。反方没有给出理由"
    assert "placeholder" not in str(record)
    assert issuance["tier"] == "formal"


async def test_a_rationale_that_shows_nothing_is_recorded_as_no_rationale(monkeypatch, shop):
    """反方的理由只写了依据：显示出来是空的。以前按原文判，理由文档照样记下——审批卡按文档画出一片空白，不说「反方没有
    给出理由」；判「被推翻」的因为引用到了 Q1 被当成有出处，照常采信、出具降档。"""
    plays = {"华南销售额超过华东": Play([NET], "refuted", "[[see:Q1]]"),
             "居各区域之首": Play([REFUNDS], "stands", "（[[see:Q1]]）")}
    record, issuance = await _record(monkeypatch, shop, plays)
    first, second = record["claims"]
    assert (first["verdict"], first["rationale"], first["rationale_doc"]) == ("stands", "", None)
    assert second["verdict"] == "inconclusive" and second["rationale_doc"] is None
    assert second["rationale"].endswith("反方给出的「被推翻」不予采信，记为无法判断。反方没有给出理由")
    assert issuance["tier"] == "formal", "没有理由的「被推翻」不能让出具降档"


async def test_a_placeholder_with_a_see_is_still_a_placeholder(monkeypatch, shop):
    plays = {"华南销售额超过华东": Play([NET], "stands", "placeholder。[[see:Q1]]"),
             "居各区域之首": Play([REFUNDS], "stands", "无。")}
    record, _ = await _record(monkeypatch, shop, plays)
    assert [(c["rationale"], c["rationale_doc"]) for c in record["claims"]] == [("", None), ("", None)]
    assert "placeholder" not in str(record)
