"""分析说法核对（engine/analysis_claims.py，细案 01 §9）：判断词要挂对应的分析结论、判定对得上，因果词要有依据。

- 说法表：每类的判断词认得出，常见的非判断写法（持续关注、异常订单、相当于、数据异常）不算；「大幅」收进异常类，看清它在
  常见写法里的表现
- 否定：判断词前 4 个字以内（跳过填充词）有否定的翻到反面类；「不但」「不少」不是否定
- 绑定：类要的 op 在这句的依据里出现、判定对得上；点名的段在 subjects 里（全半角归一）；拖累、拉动看那一段的方向；
  新高新低看 caveat；「连续」看末尾连续的期数；挂了算不了的结论算对不上
- 来源与因果的分界：因果词点名的是拆解的段 → 按来源核；不是 → 按因果核；推测说法、知识库原话、运行输入豁免因果
- 不扫：判定语（a:）、知识库原话（q:）、别的引用渲染出来的字、标题、代码块、表头
- 文档：只有记了这一项的文档在复核时核，老文档复核结果不变；记了认不出的版本记 bad_schema
"""
from __future__ import annotations

import re

import pytest

from app.analysis.testing import fake_result
from app.engine import analysis_claims as ac
from app.engine import evidence
from app.engine.evidence import build_catalog, compose_doc, make_eid, verify_doc


def hits(text: str) -> list[tuple[str, str, str | None]]:
    return [(h.claim, h.word, h.direction) for h in ac.scan(text)]


def claims(text: str) -> list[str]:
    return [h.claim for h in ac.scan(text)]


def said(text: str, *analyses: str, results=None, **sentence) -> list[dict]:
    """一句话、挂了 analyses 这几份结论：违规清单。"""
    return ac.check_sentence(ac.Sentence(text=text, analyses=list(analyses), **sentence), results or {})


def codes(found: list[dict]) -> list[str]:
    return [v["code"] for v in found]


# --------------------------------------------------------------------------
# 说法表：每类认得出、常见的非判断写法不算
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text, claim", [
    ("下降主要来自华东。", "source"), ("增长主要由线上贡献。", "source"), ("下降集中在华东、华北。", "source"),
    ("绝大部分来自线下。", "source"), ("华东拖累了整体。", "source"), ("线上带动了增长。", "source"),
    ("华东贡献最大。", "source"), ("下降主要是华东下滑造成的。", "source"),
    ("变化分散在各地区。", "dispersed"), ("各地区都在下降。", "dispersed"), ("各渠道普遍下滑。", "dispersed"),
    ("全面下降。", "dispersed"),
    ("华东和华南此消彼长。", "offset"), ("两者互相抵消。", "offset"),
    ("退款率上升来自结构变化。", "mix"), ("这是结构性的上升。", "mix"),
    ("本周销售额异常下降。", "anomaly"), ("取消单激增。", "anomaly"), ("订单数骤降。", "anomaly"),
    ("销售额明显偏离往常。", "anomaly"), ("超出过去 12 周的常规波动范围。", "anomaly"),
    ("销售额明显高于正常水平。", "anomaly"), ("波动剧烈。", "anomaly"),
    ("本周创历史新高。", "record"), ("销售额创新高。", "record"), ("前所未有。", "record"),
    ("在常规波动范围内。", "normal"), ("属于正常波动。", "normal"), ("并不异常。", "normal"),
    ("线上销售额持续上升。", "trend"), ("呈上升趋势。", "trend"), ("一路走低。", "trend"), ("稳步增长。", "trend"),
    ("连续三周下降。", "trend"), ("连续下滑。", "trend"),
    ("没有明显的上升或下降趋势。", "no_trend"), ("上升趋势不明显。", "no_trend"), ("基本平稳。", "no_trend"),
    ("西南的退款率显著高于华东。", "differ"), ("两组差异明显。", "differ"),
    ("两组差异不大。", "no_diff"), ("两组没有明显差异。", "no_diff"), ("两个渠道的退款率相当。", "no_diff"),
    ("由于促销结束，销售额下降。", "causal"), ("受天气影响，客流下降。", "causal"), ("增长得益于新客。", "causal"),
])
def test_every_class_is_recognized(text, claim):
    assert claim in claims(text), hits(text)


@pytest.mark.parametrize("text", [
    "建议持续关注华东的退款。",           # 「持续」后面不是涨跌：建议
    "异常订单 12 单。",                   # 「异常」后面是名词：业务分类
    "本周没有发现数据异常。",             # 数据质量
    "系统无异常。",
    "销售额相当于上周的两倍。",           # 「相当于」
    "华东的销售额相当高。",               # 「相当」是副词
    "这个问题引起关注。",                 # 「引起关注」不是因果
    "主导地位没有变。",                   # 「主导地位」
    "降幅较大。",                         # 不是「大幅」
    "整体平稳。",                         # 只收「基本平稳」：宁可漏报
    "趋势值得关注。",                     # 「趋势」不带涨跌
    "华东销售额下降，华南销售额上升。",   # 只陈述数字和变化
])
def test_ordinary_wording_is_not_a_judgement(text):
    assert ac.scan(text) == []


def test_the_longest_reading_wins_where_two_overlap():
    # 「超出正常范围」是异常，不是正常；「没有明显的上升或下降趋势」是无趋势，不是上升趋势
    assert claims("超出正常范围。") == ["anomaly"]
    assert claims("没有明显的上升或下降趋势。") == ["no_trend"]
    assert claims("下降主要由华东造成。") == ["source", "causal"]


def test_the_word_carries_its_direction():
    assert [h.run for h in ac.scan("连续三周下降，持续下降。")] == [True, False]
    assert hits("取消单激增。") == [("anomaly", "激增", "up")]
    assert hits("订单数骤降。") == [("anomaly", "骤降", "down")]
    assert hits("线上销售额持续上升。") == [("trend", "持续上升", "up")]
    assert hits("本周销售额异常下降。")[0][2] == "down"          # 方向在判断词后面
    assert hits("销售额下降，属于异常。")[0][2] is None            # 分句里没有涨跌
    assert hits("华东拖累了整体。")[0][2] == "down" and hits("线上带动了增长。")[0][2] == "up"


# --------------------------------------------------------------------------
# 「大幅」：收进异常类，看清它在常见写法里的表现
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text, direction", [
    ("本周销售额大幅增长。", "up"), ("订单数大幅下降。", "down"), ("退款大幅度减少。", "down"),
    ("客单价大幅提升，订单数基本不变。", "up"), ("销售额同比大幅下滑。", "down"),
])
def test_dafu_is_an_anomaly_claim_with_the_direction_of_its_verb(text, direction):
    [(claim, word, got)] = hits(text)
    assert claim == "anomaly" and word.startswith("大幅") and got == direction


@pytest.mark.parametrize("text", ["销售额大幅领先华南。", "幅度较大。", "大幅的促销海报已经撤下。"])
def test_dafu_without_a_change_verb_is_not_counted(text):
    assert ac.scan(text) == []


def test_dafu_needs_a_baseline_outside_the_band_in_the_same_direction():
    up = fake_result("baseline", None, "outside_band", direction="up")
    down = fake_result("baseline", None, "outside_band", direction="down")
    normal = fake_result("baseline", None, "within_band", direction="up")
    text = "本周销售额大幅增长。"
    assert codes(said(text)) == [ac.UNCITED]                         # 没挂：要挂波动基线
    assert said(text, "A1", results={"A1": up}) == []
    [miss] = said(text, "A1", results={"A1": down})
    assert miss["code"] == ac.MISMATCH and "方向" in miss["message"]
    [miss] = said(text, "A1", results={"A1": normal})
    assert miss["code"] == ac.MISMATCH and "常规波动范围内" in miss["message"]


# --------------------------------------------------------------------------
# 否定
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text, claim", [
    ("并未出现明显异常。", "normal"),           # 否定隔着「出现明显」
    ("本周没有出现异常。", "normal"),
    ("不在正常范围内。", "anomaly"),
    ("不属于正常波动。", "anomaly"),
    ("没有显著高于华东。", "no_diff"),
    ("并非主要来自华东。", "source_not"),
    ("并不分散。", "source"),
    ("没有持续上升。", "no_trend"),
])
def test_a_negation_flips_to_the_opposite_class(text, claim):
    assert claims(text) == [claim]


@pytest.mark.parametrize("text", ["不但大幅增长，", "不少门店大幅下滑。", "无论是否大幅增长，"])
def test_words_that_only_look_negative_do_not_flip(text):
    assert claims(text) == ["anomaly"]


def test_negated_causes_and_records_are_left_alone():
    assert ac.scan("并非由于促销。") == [] and ac.scan("并非历史新高。") == []


def test_a_negated_source_needs_the_named_segment_outside_the_main_sources():
    driver = fake_result()                                          # 主要来自华东
    assert said("下降并非主要来自华南。", "A1", results={"A1": driver}) == []
    [miss] = said("下降并非主要来自华东。", "A1", results={"A1": driver})
    assert miss["code"] == ac.MISMATCH and "华东" in miss["message"]
    dispersed = fake_result(verdict="dispersed")
    assert said("下降并非主要来自某一个地区。", "A1", results={"A1": dispersed}) == []


# --------------------------------------------------------------------------
# 绑定
# --------------------------------------------------------------------------


def test_the_required_op_must_be_among_the_evidence():
    baseline = fake_result("baseline", None, "outside_band")
    [v] = said("下降主要来自华东。", "A1", results={"A1": baseline})
    assert v["code"] == ac.UNCITED and v["op"] == "decompose" and v["claim"] == "source" and v["analyses"] == ["A1"]
    # 有好几个时任一个对得上就算对
    assert said("下降主要来自华东。", "A1", "A2", results={"A1": baseline, "A2": fake_result()}) == []


def test_named_segments_must_be_among_the_subjects():
    concentrated = fake_result(verdict="concentrated", subjects=[["华东"], ["华北"]])
    assert said("下降集中在华东、华北。", "A1", results={"A1": concentrated}) == []
    [miss] = said("下降集中在华东、华南。", "A1", results={"A1": concentrated})
    assert miss["code"] == ac.MISMATCH and "不是华南" in miss["message"]
    # 别的分句里提到的段不算点名
    assert said("下降主要来自华东，华南基本持平。", "A1", results={"A1": fake_result()}) == []


def test_named_segments_are_matched_across_full_and_half_width():
    result = fake_result(subjects=[["Ａ店"]])
    result["table"]["rows"][0][0] = "Ａ店"
    assert said("下降主要来自A店。", "A1", results={"A1": result}) == []
    assert codes(said("下降主要来自华北。", "A1", results={"A1": result})) == [ac.MISMATCH]


def test_a_dispersed_verdict_cannot_back_a_main_source():
    [miss] = said("下降主要来自华东。", "A3", results={"A3": fake_result(verdict="dispersed")})
    assert miss["code"] == ac.MISMATCH and miss["text"] == "主要来自"
    assert "A3" in miss["message"] and "分散在各地区" in miss["message"]
    assert "[[a:A3]]" in miss["for_model"] and "不能写「主要来自华东」" in miss["for_model"]


def test_drag_and_pull_follow_the_direction_of_the_segment():
    up = fake_result(direction="up")
    up["table"]["rows"][0][3] = "120"                                   # 华东 +120
    assert said("华东带动了增长。", "A1", results={"A1": up}) == []
    [miss] = said("华东拖累了整体。", "A1", results={"A1": up})
    assert miss["code"] == ac.MISMATCH and "上升" in miss["message"]
    assert said("华东拖累了整体。", "A1", results={"A1": fake_result()}) == []      # 华东 −120


def test_a_drag_outside_the_main_sources_counts_when_it_is_a_large_share():
    # 整体上涨、华南是主要来源，华东下跌拖累了涨幅：华东的变化占总变化过半（绝对值）也算拖累
    result = fake_result(subjects=[["华南"]], direction="up")
    result["table"]["rows"][0][3:6] = ["-120", "-0.1463", "-1.5"]
    assert said("华东拖累了涨幅。", "A1", results={"A1": result}) == []
    result["table"]["rows"][0][5] = "-0.2"
    assert codes(said("华东拖累了涨幅。", "A1", results={"A1": result})) == [ac.MISMATCH]


def test_records_need_the_caveat_in_the_same_direction():
    high = fake_result("baseline", None, "far_outside_band", direction="up",
                       caveats=[{"code": "record_high", "text": "本期是历史最高"}])
    assert said("本周创历史新高。", "A1", results={"A1": high}) == []
    assert codes(said("本周创历史新低。", "A1", results={"A1": high})) == [ac.MISMATCH]
    plain = fake_result("baseline", None, "outside_band", direction="up")
    assert codes(said("本周创历史新高。", "A1", results={"A1": plain})) == [ac.MISMATCH]


def test_trends_check_the_direction_and_runs_check_the_length():
    rising = fake_result("trend", None, "rising", direction="up", values={"run_length": "4", "run_direction": "up"})
    assert said("线上销售额持续上升。", "A1", results={"A1": rising}) == []
    assert codes(said("线上销售额持续下降。", "A1", results={"A1": rising})) == [ac.MISMATCH]
    assert said("线上销售额连续四周上升。", "A1", results={"A1": rising}) == []
    short = fake_result("trend", None, "rising", direction="up", values={"run_length": "2", "run_direction": "up"})
    [miss] = said("线上销售额连续上升。", "A1", results={"A1": short})
    assert miss["code"] == ac.MISMATCH and "连续" in miss["message"]
    flat = fake_result("trend", None, "no_trend", direction=None)
    assert said("没有明显的上升或下降趋势。", "A1", results={"A1": flat}) == []
    assert codes(said("线上销售额持续上升。", "A1", results={"A1": flat})) == [ac.MISMATCH]


def test_differences_check_the_verdict_and_the_order_of_the_two_groups():
    differs = fake_result("differ", "rate", "differs", subjects=[["西南"], ["华东"]], direction="up")
    assert said("西南的退款率显著高于华东。", "A1", results={"A1": differs}) == []
    assert said("华东的退款率显著低于西南。", "A1", results={"A1": differs}) == []
    [miss] = said("西南的退款率显著低于华东。", "A1", results={"A1": differs})
    assert miss["code"] == ac.MISMATCH and "西南高于华东" in miss["message"]
    nothing = fake_result("differ", "rate", "no_evidence", subjects=[["西南"], ["华东"]], direction=None)
    assert said("两组差异不大。", "A1", results={"A1": nothing}) == []
    assert codes(said("西南的退款率显著高于华东。", "A1", results={"A1": nothing})) == [ac.MISMATCH]


def test_a_structure_claim_needs_a_ratio_decomposition():
    [miss] = said("退款率上升来自结构变化。", "A1", results={"A1": fake_result()})
    assert miss["code"] == ac.MISMATCH and "比率拆解" in miss["message"]
    mix = fake_result(mode="ratio", verdict="mix_driven")
    assert said("退款率上升来自结构变化。", "A1", results={"A1": mix}) == []


def test_offsets_need_the_caveat():
    assert codes(said("华东和华南此消彼长。", "A1", results={"A1": fake_result()})) == [ac.MISMATCH]
    offset = fake_result(caveats=[{"code": "offset", "text": "下降的段合计 −120"}])
    assert said("华东和华南此消彼长。", "A1", results={"A1": offset}) == []


def test_a_refused_conclusion_backs_nothing():
    refused = fake_result("baseline", None, None, status="refused", reason_code="history_short")
    [miss] = said("本周销售额异常下降。", "A1", results={"A1": refused})
    assert miss["code"] == ac.MISMATCH and "没有做成" in miss["message"] and "历史只有 5 期" in miss["message"]


def test_uncited_advice_points_at_a_conclusion_that_exists():
    [v] = said("下降主要来自华东。", results={"A2": fake_result()})
    assert "[[see:A2]]" in v["for_model"] and "[[a:A2]]" in v["for_model"]
    [v] = said("下降主要来自华东。")
    assert "这次没有变化拆解的结论" in v["for_model"] and "只写各段各自的变化" in v["for_model"]
    # 结构变化只推荐比率拆解；算不了的结论不推荐
    [v] = said("这是结构性的上升。", results={"A2": fake_result(), "A3": fake_result(mode="ratio", verdict="both")})
    assert "[[see:A3]]" in v["for_model"]
    [v] = said("下降主要来自华东。", results={"A2": fake_result(status="refused")})
    assert "[[see:A2]]" not in v["for_model"]


def test_one_violation_per_class_and_clause():
    found = said("由于促销结束导致下降。")
    assert codes(found) == [ac.CAUSAL] and found[0]["text"] == "由于"
    assert "「由于促销结束导致下降」" in found[0]["message"]
    assert codes(said("由于促销结束，下降；由于天气，客流减少。")) == [ac.CAUSAL, ac.CAUSAL]


# --------------------------------------------------------------------------
# 来源与因果
# --------------------------------------------------------------------------


def test_a_cause_naming_a_segment_of_the_decomposition_is_checked_as_a_source():
    driver = fake_result()
    assert said("下降主要由华东造成。", "A1", results={"A1": driver}) == []
    assert said("增长得益于华东。", "A1", results={"A1": fake_result(direction="up")}) == []
    [miss] = said("下降是华南造成的。", "A1", results={"A1": driver})
    assert miss["code"] == ac.MISMATCH and miss["claim"] == "source"


def test_a_cause_outside_the_decomposition_needs_its_own_evidence():
    [v] = said("由于促销结束导致下降。", "A1", results={"A1": fake_result()})
    assert v["code"] == ac.CAUSAL and v["claim"] == "causal" and "op" not in v
    assert "可能与…有关，待核实" in v["for_model"]


@pytest.mark.parametrize("sentence", [
    {"text": "可能与促销结束有关，待核实。"},
    {"text": "下降或许是由于促销结束。"},
    {"text": "疑似由于促销结束导致下降。"},
    {"text": "由于促销结束导致下降。", "quoted": True},
    {"text": "由于促销结束导致下降。", "inputs": True},
    {"text": "由于数据只到 10 月 16 日，本周不完整。"},
    {"text": "由于样本少，差异不能下结论。"},
])
def test_guesses_quotes_inputs_and_data_caveats_are_not_unsupported_causes(sentence):
    assert ac.check_sentence(ac.Sentence(**sentence), {}) == []


# --------------------------------------------------------------------------
# 文档：哪里不扫、怎么挂、版本
# --------------------------------------------------------------------------


def a_entry(alias: str, result: dict) -> dict:
    artifact = "a" * 63 + alias[-1]
    return {"alias": alias, "kind": "analysis", "artifact": artifact, "eid": make_eid("analysis", artifact, {}),
            "locator": {}, "result": result}


def catalog(**analyses) -> dict:
    out = build_catalog(nodes={"caliber": {
        "kind": "metric_set", "caliber": "周报口径", "caliber_version": "v2", "artifact": "f" * 64,
        "metrics": [{"id": "gmv", "name": "销售额", "value": 45678.5, "unit": "元", "decimals": 1}]}},
        inputs={"promo": "满减"})
    out.update({alias: a_entry(alias, result) for alias, result in analyses.items()})
    return out


@pytest.fixture
def see_a(monkeypatch):
    """[[see:A1]] 的解析是证据接入（W1-b）的事：合进来之前这里按它的约定代为解析（编号 A{n}、目录里 kind analysis）。"""
    original = evidence.resolve_support

    def resolve(ref, catalog, *, loader=None):
        ref = ref.strip()
        entry = catalog.get(ref) or {}
        if re.fullmatch(r"A\d+", ref) and entry.get("kind") == "analysis":
            return {"ref": ref, "role": "support", "alias": ref, "kind": "analysis", "status": "resolved",
                    "eid": entry["eid"], "locator": {}}
        return original(ref, catalog, loader=loader)

    monkeypatch.setattr(evidence, "resolve_support", resolve)


def found(doc: dict) -> list[dict]:
    return [v for v in doc["violations"] if v["code"] in ac.CODES]


def test_a_doc_records_the_mode_and_lexicon_only_when_asked():
    raw = "本周销售额 [[m:gmv]]，下降主要来自华东。[[see:m:gmv]]"
    doc = compose_doc(raw, catalog(), analysis_claims="check")
    assert doc[ac.DOC_FIELD] == {"mode": "check", "lexicon": ac.LEXICON_VERSION}
    [v] = found(doc)
    assert v["code"] == ac.UNCITED and doc["markdown"][slice(*v["span"])] == "主要来自"
    assert v["unit"] and v["segment"] and v["context"] == "本周销售额 45,678.5元，下降主要来自华东。"
    assert doc["stats"]["analysis_claims"] == 1
    # 没要求（off、老调用）：文档一字不差，统计也不多这个键
    for mode in (None, "off"):
        plain = compose_doc(raw, catalog(), analysis_claims=mode)
        assert ac.DOC_FIELD not in plain and found(plain) == [] and "analysis_claims" not in plain["stats"]


def test_old_documents_verify_exactly_as_before():
    raw = "本周销售额 [[m:gmv]]，下降主要来自华东，由于促销结束。[[see:m:gmv]]"
    old = compose_doc(raw, catalog())
    assert verify_doc(old, catalog()) == verify_doc(compose_doc(raw, catalog()), catalog())
    assert verify_doc(old, catalog())["violations"] == [] and "analysis_claims" not in verify_doc(old, catalog())["stats"]
    new = compose_doc(raw, catalog(), analysis_claims="strict")
    again = verify_doc(new, catalog())
    assert [v["code"] for v in again["violations"]] == [ac.UNCITED, ac.CAUSAL]
    assert again["stats"]["analysis_claims"] == 2 and again["violations"] == new["violations"]


def test_an_unrecognized_record_is_reported_and_checked_with_the_current_lexicon():
    doc = compose_doc("下降主要来自华东。", catalog(), analysis_claims="check")
    doc[ac.DOC_FIELD] = {"mode": "check", "lexicon": 99}
    checked = verify_doc(doc, catalog())
    assert [v["code"] for v in checked["violations"]] == ["bad_schema", ac.UNCITED]
    assert "分析说法核对规则" in checked["violations"][0]["message"]


def test_system_rendered_phrases_quotes_headings_and_code_are_not_scanned():
    doc = {"markdown": "", "blocks": [
        {"type": "heading", "units": [{"id": "u0", "kind": "heading", "segments": [
            {"id": "s0", "kind": "text", "text": "下降主要来自华东", "span": [0, 8]}]}]},
        {"type": "code", "units": [{"id": "u1", "kind": "code", "segments": [
            {"id": "s1", "kind": "text", "text": "-- 异常订单激增", "span": [8, 17]}]}]},
        {"type": "paragraph", "units": [{"id": "u2", "kind": "claim", "segments": [
            {"id": "s2", "kind": "value", "text": "销售额下降 194 万，主要来自华东（-120 万，占 62%）", "ref": "a:A1"},
            {"id": "s3", "kind": "text", "text": "。售后说「"},
            {"id": "s4", "kind": "quote", "text": "退款主要集中在东区，由于物流延误", "ref": "q:K1|退款主要集中在东区，由于物流延误",
             "state": "deterministic"},
            {"id": "s5", "kind": "text", "text": "」。"}]}]},
        {"type": "table", "units": [{"id": "u3", "kind": "connective", "loc": {"row": -1, "col": 0}, "segments": [
            {"id": "s6", "kind": "text", "text": "是否异常"}]}]},
    ]}
    assert ac.check_doc(doc, catalog()) == []
    # 同一句换成写作者自己写的字就核
    doc["blocks"][2]["units"][0]["segments"][0] = {"id": "s2", "kind": "text", "text": "销售额下降，主要来自华东"}
    assert codes(ac.check_doc(doc, catalog())) == [ac.UNCITED]


def test_rendered_values_name_segments_but_are_not_judgement_words():
    # [[v:…]] 渲染出来的「华东」能点名，渲染出来的「异常」不是写作者下的判断
    doc = {"markdown": "", "blocks": [{"type": "paragraph", "units": [{"id": "u0", "kind": "claim", "cites": ["A1"],
           "segments": [
               {"id": "s0", "kind": "text", "text": "下降主要来自", "span": [0, 6]},
               {"id": "s1", "kind": "value", "text": "华南", "ref": "v:Q1.r1.region", "span": [6, 8]},
               {"id": "s2", "kind": "text", "text": "，状态为", "span": [8, 12]},
               {"id": "s3", "kind": "value", "text": "异常", "ref": "v:Q1.r1.status", "span": [12, 14]}]}]}]}
    [miss] = ac.check_doc(doc, {"A1": a_entry("A1", fake_result())})
    assert miss["code"] == ac.MISMATCH and "不是华南" in miss["message"] and miss["span"] == [2, 6]


def test_compose_and_verify_bind_the_sentence_to_its_see_marker(see_a):
    cat = catalog(A1=fake_result())
    ok = compose_doc("销售额 [[m:gmv]]，下降主要来自华东。[[see:A1]]", cat, analysis_claims="check")
    assert found(ok) == [] and verify_doc(ok, cat)["violations"] == []
    bad = compose_doc("销售额 [[m:gmv]]，下降主要来自华南。[[see:A1]]", cat, analysis_claims="check")
    [miss] = found(bad)
    assert miss["code"] == ac.MISMATCH and miss["analyses"] == ["A1"]
    # 复核按这次重新解析的引用算，不信文档里记的 cites：依据删掉了就是没挂
    tampered = compose_doc("销售额 [[m:gmv]]，下降主要来自华东。[[see:A1]]", cat, analysis_claims="check")
    tampered["blocks"][0]["units"][0]["see"] = []
    assert [v["code"] for v in verify_doc(tampered, cat)["violations"]] == [ac.UNCITED]


def test_inputs_and_quotes_in_the_same_sentence_support_a_cause():
    doc = compose_doc("由于本周有 [[i:promo]] 活动，销售额 [[m:gmv]]。[[see:m:gmv]]", catalog(), analysis_claims="check")
    assert found(doc) == []
    doc = compose_doc("由于本周有活动，销售额 [[m:gmv]]。[[see:m:gmv,i:promo]]", catalog(), analysis_claims="check")
    assert found(doc) == []
    doc = compose_doc("由于本周有活动，销售额 [[m:gmv]]。[[see:m:gmv]]", catalog(), analysis_claims="check")
    assert codes(found(doc)) == [ac.CAUSAL]


def test_the_prompt_rules_depend_on_whether_there_are_conclusions():
    assert ac.rules_prompt(catalog()) == ac.ANALYSIS_NONE_PROMPT
    assert ac.rules_prompt(catalog(A1=fake_result())) == ac.ANALYSIS_RULES_PROMPT
    assert "只陈述数字和变化" in ac.ANALYSIS_NONE_PROMPT and "待核实" in ac.ANALYSIS_NONE_PROMPT
    assert "[[a:A1]]" in ac.ANALYSIS_RULES_PROMPT


def test_judgement_words_make_a_sentence_a_claim_for_the_judge():
    from app.engine.judge import claim_priority

    doc = compose_doc("华东异常。\n\n下面主要看三个方面。\n\n建议持续关注。\n\n近 8 周的趋势图如下：", catalog())
    kinds = {doc["markdown"][slice(*u["span"])]: (u["kind"], claim_priority(u, b, doc["markdown"]))
             for b, u in evidence.iter_units(doc)}
    assert kinds["华东异常。"] == ("claim", 2)
    assert kinds["下面主要看三个方面。"][0] == "connective" and kinds["建议持续关注。"][0] == "connective"
    assert kinds["近 8 周的趋势图如下："][0] == "connective", "引出图表的话不算判断"
