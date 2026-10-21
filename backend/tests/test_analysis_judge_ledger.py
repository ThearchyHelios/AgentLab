"""分析结论进证据链（第 1 波 W1-b）：结论句裁判的摘录、口径账本。

- 裁判：引用了分析结论的句子，摘录里有方法、判定、判定标准、关键数、结果表和输入查询的 SQL；判定是系统算的，规则里另加
  一条（只判句子说法和判定一致不一致）——没有分析结论的报告，裁判看到的规则一字不差
- 口径账本：分析结论里的数（判定语、具名标量、结果表的格）不标口径、不比对、不进整题判断的计数（D11）
"""
from __future__ import annotations

from typing import Any

from app.analysis.testing import fake_result
from app.engine import judge as judging
from app.engine.evidence import build_catalog, compose_doc, iter_segments
from app.ledger import labels as ledger_labels
from tests.test_analysis_evidence import setup


def composed(text: str, **kw: Any) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    store, ledger, _, _ = setup(**kw)
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load)
    return store, catalog, compose_doc(text, catalog, loader=store.load)


# --------------------------------------------------------------------------
# 裁判
# --------------------------------------------------------------------------


def test_the_judge_gets_the_method_the_judgment_and_the_input_sql():
    store, catalog, doc = composed("[[a:A1]]，华东变化 [[v:A1.r0.delta]]。[[see:A1]]")
    request = judging.prepare(doc, catalog, loader=store.load)
    [cand] = request.cands
    assert cand.aliases == ("A1",) and "A1" in cand.whole and ("A1", 0) in cand.rows and ("A1", "delta") in cand.cols
    excerpt = request.excerpts["A1"]
    assert excerpt.startswith("【A1】分析结论（变化拆解（可加），方法 decompose.additive@1）")
    assert "判定：销售额下降 194 万（-8.7%），主要来自华东（-120 万，占 62%）" in excerpt
    assert "判定标准：一段占总变化一半以上算主要来源" in excerpt
    assert "关键数：base_total=2,220 | target_total=2,026 | delta_total=-194" in excerpt
    assert "结果表（共 4 行）列：地区 | 基期 | 本期 | 变化" in excerpt and "r0：华东 | 820 | 700 | -120" in excerpt
    assert "输入：取自查询 Q1，用到 region、week、gmv 列" in excerpt
    assert "查询 Q1 的 SQL：SELECT region, week, SUM(gmv) AS gmv FROM orders" in excerpt
    assert "地区拖累" not in excerpt                      # agent 写的假设不进摘录

    system = request.messages(request.cands)[0].content
    assert judging.JUDGE_RULES in system and judging.ANALYSIS_JUDGE_PROMPT in system


def test_reports_without_analyses_see_exactly_the_old_rules():
    store, catalog, doc = composed("华东本周 [[v:Q1.r1.gmv]]。[[see:Q1]]")
    request = judging.prepare(doc, catalog, loader=store.load)
    system = request.messages(request.cands)[0].content
    assert system == f"{judging.JUDGE_ROLE}\n\n{judging.JUDGE_RULES}"


def test_excerpts_reload_the_result_from_the_stored_summary_and_say_refusals():
    store, catalog, doc = composed("下降来自哪里还说不清。[[see:A1]]",
                                   result=fake_result(status="refused", reason_code="too_few_segments"))
    request = judging.prepare(doc, doc["catalog"], loader=store.load)   # 文档里的目录只留摘要：按工件取回
    assert "判定：算不了（历史只有 5 期，至少要 8 期）" in request.excerpts["A1"]
    gone = judging.prepare(doc, doc["catalog"], loader=lambda _: None)
    assert "A1" not in gone.excerpts


def test_drill_downs_name_the_parent_in_the_excerpt():
    store, ledger, q, a = setup()
    from app.analysis.contract import ledger_entry

    child = fake_result(inputs=[{"ref": "Q1", "artifact": q, "role": "data", "columns": ["region"]},
                                {"ref": "A3", "artifact": a, "role": "parent"}])
    ledger.append(ledger_entry(child, node_id="an", exec_no=2, artifact=store.put(child), item="drill"))
    catalog = build_catalog(nodes={}, ledger=ledger, loader=store.load)
    doc = compose_doc("华东里主要来自上海。[[see:A2]]", catalog, loader=store.load)
    excerpt = judging.prepare(doc, catalog, loader=store.load).excerpts["A2"]
    assert "下钻自：A1" in excerpt


# --------------------------------------------------------------------------
# 口径账本：分析结论的数不标口径
# --------------------------------------------------------------------------


def test_analysis_numbers_get_no_caliber_label_and_are_not_counted():
    store, catalog, doc = composed("[[a:A1]]，华东占 [[a:A1.top_share|pct]]，变化 [[v:A1.r0.delta]]；"
                                   "华东上周 [[v:Q1.r0.gmv]]。")
    q = catalog["Q1"]["artifact"]
    queries = {q: {"artifact": q, "status": "none", "candidates": []}}
    labels = ledger_labels.label_doc(doc, queries, {}, lambda _: None)
    labelled = {s["id"]: s.get("ref") for s in iter_segments(doc) if s["id"] in labels}
    assert list(labelled.values()) == ["v:Q1.r0.gmv"]
    whole = ledger_labels.whole_of(labels, {})
    assert whole["counts"] == {"caliber": 0, "related": 0, "reasoning": 1, "unknown": 0}
    assert ledger_labels.referenced_columns([doc], lambda _: None) == {q: {"gmv"}}


def test_a_report_citing_only_analyses_has_nothing_to_label():
    store, catalog, doc = composed("[[a:A1]]。[[see:A1]]")
    assert ledger_labels.label_doc(doc, {}, {}, lambda _: None) == {}
    assert ledger_labels.referenced_columns([doc], lambda _: None) == {}
    assert ledger_labels.whole_of({}, {})["reason"] == "报告里没有可标口径的数"
