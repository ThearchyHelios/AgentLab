"""结论句的判定，以及渲染时收拾的两处排版。

一次真实运行降档出具：唯一的缺口是一句解释同比、环比是什么意思的话（「同比是将本期与上年同月对比……
反映月度之间的短期波动」），里面有「同比」「环比」「波动」三个方向词，没挂依据就被算成缺依据的结论句。
同一份报告里每个数字都显示成「12,345人次 人次」（指标渲染已带单位，写作者又写了一遍），
「最高月份 [[see:Q2]]。」去掉 see 后剩下「最高月份 。」。
"""
from __future__ import annotations

import random

import pytest

from app.engine.evidence import (
    UNIT_LOOKAHEAD,
    StreamRenderer,
    build_catalog,
    compose_doc,
    iter_segments,
    render_markers,
    verify_doc,
)

ART = "b" * 64


def metric(mid, name, value, unit="", decimals=None, fmt="thousands"):
    return {"id": mid, "name": name, "unit": unit, "value": value, "decimals": decimals,
            "format": fmt, "status": "ok" if value is not None else "missing_input", "expression": "…"}


CARD = {
    "kind": "metric_set", "caliber": "客流口径", "caliber_version": "v1", "artifact": ART,
    "metrics": [
        metric("visits", "客流量", 12345, "人次"),
        metric("heads", "到访人数", 1200, "人"),
        metric("gmv", "销售额", 45678.5, "元", 1),
        metric("yoy", "同比增幅", 12.5, "%", 1, "plain"),
        metric("gone", "缺数", None, "人次"),
    ],
}
LEDGER = [{"kind": "metric_set", "node_id": "cal", "exec": 1, "artifact": ART, "caliber": "客流口径",
           "version": "v1", "metrics": [m["id"] for m in CARD["metrics"]]}]


@pytest.fixture
def catalog():
    return build_catalog(nodes={"cal": CARD}, ledger=LEDGER, inputs={"week": "2026-W37"})


def units(doc):
    return [(u["kind"], "".join(s["text"] for s in u["segments"] if s["kind"] != "structural"))
            for b in doc["blocks"] for u in b["units"]]


def kind_of(doc, fragment):
    return next(k for k, text in units(doc) if fragment in text)


# --------------------------------------------------------------------------
# 结论句
# --------------------------------------------------------------------------

DEFINITION = ("同比与环比的比较基准不同：**同比**是将本期与上年同月对比（本期为 2026 年 9 月，基期为 2025 年 9 月），"
              "剔除季节性因素，反映年度维度的变化；**环比**是将本期与紧邻的上月对比（基期为 2026 年 8 月），"
              "反映月度之间的短期波动。")


def test_the_definition_that_downgraded_a_real_report_is_not_a_claim(catalog):
    doc = compose_doc(f"本期客流 [[m:visits]]。[[see:m:visits]]\n\n{DEFINITION}两者的差异仅在基期。", catalog)
    assert kind_of(doc, "比较基准不同") == "connective"
    assert doc["stats"]["uncited_claims"] == 0
    assert verify_doc(doc, catalog)["violations"] == []


@pytest.mark.parametrize("sentence", [
    "同比与环比使用同一套汇总口径。",               # 只提到同比、环比：说的是跟谁比，不是涨跌
    "增长率是指本期较上期增长的部分占上期的比例。",
    "客单价的计算方式为销售额除以订单数，订单越多摊得越低。",
])
def test_sentences_without_a_conclusion_are_connective(catalog, sentence):
    assert kind_of(compose_doc(sentence, catalog), sentence[:6]) == "connective"


@pytest.mark.parametrize("sentence", [
    "本期客流同比增长，环比回落。",
    "环比波动明显加大。",
    "增长主要是将促销提前带来的。",                 # 有「是将」，但说的是原因
    "同比增幅是指增长的比例，这次的增长由于新客首单。",
])
def test_real_conclusions_stay_claims(catalog, sentence):
    assert kind_of(compose_doc(sentence, catalog), sentence[:6]) == "claim"


def test_a_definition_with_a_number_is_still_a_claim(catalog):
    doc = compose_doc("同比是将本期 [[m:visits]] 与上年同月对比。", catalog)
    assert kind_of(doc, "同比是将") == "claim"


# --------------------------------------------------------------------------
# 重复的单位
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "shown"), [
    ("客流 [[m:visits]] 人次，创新高。", "客流 12,345人次，创新高。"),
    ("客流 [[m:visits]]人次。", "客流 12,345人次。"),
    ("销售额 [[m:gmv]] 元，", "销售额 45,678.5元，"),
    ("销售额 [[m:gmv|万]] 万元。", "销售额 4.57万元。"),
    ("同比 [[m:yoy]] %。", "同比 12.5%。"),
    ("客流 [[m:visits]] 人次的水平", "客流 12,345人次的水平"),      # 多字单位后面跟着字，照样去掉
])
def test_a_repeated_unit_is_dropped(catalog, raw, shown):
    doc = compose_doc(raw, catalog)
    assert doc["markdown"] == shown
    assert render_markers(raw, catalog) == shown
    assert doc["stats"]["numbers_cited"] == 1
    assert verify_doc(doc, catalog)["violations"] == []


@pytest.mark.parametrize(("raw", "shown"), [
    # 口径卡的单位是「人」，写作者写的是「人次」：不是同一个单位，不动，让不一致露出来
    ("到访 [[m:heads]] 人次。", "到访 1,200人 人次。"),
    ("销售额 [[m:gmv]] 美元。", "销售额 45,678.5元 美元。"),
    ("客流 [[m:visits]]   人次。", "客流 12,345人次   人次。"),   # 隔了三个空格：不是紧跟着的
    ("缺数 [[m:gone]] 人次。", "缺数 — 人次。"),                    # 没有值，渲染成「—」，没有单位可比
])
def test_other_text_after_a_number_is_left_alone(catalog, raw, shown):
    assert compose_doc(raw, catalog)["markdown"] == shown
    assert render_markers(raw, catalog) == shown


def test_the_segment_still_carries_the_full_rendered_value(catalog):
    doc = compose_doc("客流 [[m:visits]] 人次。[[see:m:visits]]", catalog)
    number = next(s for s in iter_segments(doc) if s["kind"] == "number")
    assert number["text"] == "12,345人次" and number["ref"] == "m:visits"


# --------------------------------------------------------------------------
# see 前面的空白
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "shown"), [
    ("为最高月份 [[see:m:visits]]。", "为最高月份。"),
    ("增长 [[see:m:visits]] [[see:m:yoy]]，", "增长，"),
    ("前一句 [[see:m:visits]] 后一句", "前一句 后一句"),
    ("前一句 [[see:m:visits]]后一句", "前一句 后一句"),            # see 后面紧跟着字：留一个空格隔开
    ("末尾 [[see:m:visits]]", "末尾"),
    ("| 客流 | 值 [[see:m:visits]] |", "| 客流 | 值 |"),
])
def test_blank_before_see_is_dropped(catalog, raw, shown):
    assert render_markers(raw, catalog) == shown


def test_indentation_before_see_is_kept(catalog):
    raw = "- 第一条\n  [[see:m:visits]] 续行"
    assert render_markers(raw, catalog) == "- 第一条\n   续行"


def test_the_report_from_the_downgraded_run(catalog):
    raw = ("2026 年 9 月，客流量为 [[m:visits]] 人次，为本次统计区间内的最高月份 [[see:m:visits]]。\n\n"
           f"{DEFINITION}\n\n"
           "**同比**：本期 [[m:visits]] 人次，同比增幅 [[m:yoy]] [[see:m:visits,m:yoy]]。")
    doc = compose_doc(raw, catalog)
    assert doc["markdown"] == (
        "2026 年 9 月，客流量为 12,345人次，为本次统计区间内的最高月份。\n\n"
        f"{DEFINITION}\n\n"
        "**同比**：本期 12,345人次，同比增幅 12.5%。")
    assert doc["stats"]["uncited_claims"] == 0 and doc["stats"]["numbers_cited"] == 3
    assert verify_doc(doc, catalog)["violations"] == []


# --------------------------------------------------------------------------
# 流式渲染：分块输出拼起来仍然等于整篇渲染
# --------------------------------------------------------------------------

def _stream(catalog, raw, sizes):
    renderer, out, i = StreamRenderer(catalog), [], 0
    for size in sizes:
        if i >= len(raw):
            break
        out.append(renderer.feed(raw[i:i + size]))
        i += size
    if i < len(raw):
        out.append(renderer.feed(raw[i:]))
    out.append(renderer.flush())
    return out


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, UNIT_LOOKAHEAD, 40])
def test_stream_drops_the_repeated_unit_and_the_blank_like_the_full_render(catalog, size):
    raw = "客流 [[m:visits]] 人次，为最高月份 [[see:m:visits]]。到访 [[m:heads]] 人次 [[see:m:heads]]\n末尾 [[m:gmv]] 元"
    full = render_markers(raw, catalog)
    assert full == "客流 12,345人次，为最高月份。到访 1,200人 人次\n末尾 45,678.5元"
    out = _stream(catalog, raw, [size] * len(raw))
    assert "".join(out) == full, out


@pytest.mark.parametrize("raw", [
    "为最高月份 [[see:m:visits]]。下一句",
    "前一句 [[see:m:visits]]后一句",
    "增长  [[see:m:visits]] [[see:m:yoy]]\n下一行",
])
@pytest.mark.parametrize("size", range(1, 9))
def test_stream_holds_the_blank_before_see_until_it_knows_what_follows(catalog, raw, size):
    # 没有数字标记帮忙攒着：空白和 see 得自己攒到后面的字来了再放
    assert "".join(_stream(catalog, raw, [size] * len(raw))) == render_markers(raw, catalog)


def _corpus(n):
    rng = random.Random(20260930)
    pieces = ["[[m:visits]]", "[[m:gmv|万]]", "[[m:heads]]", "[[m:yoy]]", "[[see:m:visits]]", "[[see:m:yoy]]",
              "人次", "人", "次", "万元", "元", "%", " ", "  ", "　", "\n", "。", "，", "文字", "[[", "]]", "["]
    return ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 16))) for _ in range(n)]


@pytest.mark.parametrize("raw", _corpus(150))
def test_stream_matches_the_full_render_on_random_input(catalog, raw):
    rng = random.Random(raw)
    full = render_markers(raw, catalog)
    for _ in range(3):
        sizes = [rng.randint(1, 20) for _ in range(len(raw))]
        assert "".join(_stream(catalog, raw, sizes)) == full
