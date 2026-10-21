"""问题意图（app/analysis/intent.py）：一句问题是不是分析类问题，给助手搭问数据的图时自查用。

只收明确的问法、宁可漏判：漏判只是少一条自查提醒，误判要让助手多改一轮。所以反例和正例一样多——问一个字段（退款原因）、
问业务状态（异常订单）、问服务（系统正常吗）、筛选（波动超过 10%）、问时长（持续几天）都不算。问法都是虚构的零售场景。
"""
from __future__ import annotations

import pytest

from app.analysis.intent import INTENT_VERSION, KINDS, Intent, analysis_intent, is_analytical

POSITIVE = [
    # 原因
    ("上周销售额为什么下降了", "why"),
    ("华东的退款率为什么这么高", "why"),
    ("为什么华东比华南的客单价低", "why"),
    ("销售额下降了，为什么", "why"),
    ("这周订单怎么会少了这么多", "why"),
    ("销售额下滑的主要原因是什么", "why"),
    ("什么原因导致线上订单减少", "why"),
    ("本周销售额下滑了，原因是什么？", "why"),
    # 来源
    ("这周和上周比，销售额怎么变了", "source"),
    ("上周各渠道的销售额有什么变化", "source"),
    ("哪个地区拖累了整体销售额", "source"),
    ("谁贡献最大", "source"),
    ("增长主要来自哪个地区", "source"),
    ("各地区对销售额增长的贡献是多少", "source"),
    ("销售额增长的驱动因素有哪些", "source"),
    ("什么带动了订单增长", "source"),
    ("把销售额的变化按地区拆开看看", "source"),
    ("做个归因分析", "source"),
    # 异常
    ("本周销售额是否异常", "anomaly"),
    ("这周退款单数异常吗？", "anomaly"),
    ("下降 12% 正常吗", "anomaly"),
    ("这个降幅算不算异常", "anomaly"),
    ("上周订单有没有异动", "anomaly"),
    ("取消单是不是暴增了，有没有激增", "anomaly"),
    ("这周的销售额是不是超出了正常范围", "anomaly"),
    # 波动
    ("最近销售额波动大吗", "fluctuation"),
    ("这是不是正常波动", "fluctuation"),
    ("销售额同比变化大不大", "fluctuation"),
    # 趋势
    ("线上销售额的趋势怎么样", "trend"),
    ("看一下近半年订单数的走势", "trend"),
    ("线上销售额是不是在持续上升", "trend"),
    ("下降会持续吗", "trend"),
    ("华南的销售额连续三周下降了吗", "trend"),
    ("客单价是不是越来越低", "trend"),
    ("退款率是不是一直在涨", "trend"),
    # 差异
    ("华东的退款率是不是真的比华南高", "differ"),
    ("线上和线下的转化率差异显著吗", "differ"),
    ("两组的客单价有没有明显差别", "differ"),
    ("这个差距是不是偶然", "differ"),
    # 英文
    ("Why did sales drop last week?", "why"),
    ("What drove the decline in online orders?", "source"),
    ("Is this normal for a Monday?", "anomaly"),
    ("Show me the trend of refunds", "trend"),
    ("Is the refund rate in East significantly higher than South?", "differ"),
]

NEGATIVE = [
    "上周销售额是多少",
    "列出华东、华南、华北、西南四个地区的订单数",
    "shop 里都有哪些表",
    "退款原因有哪些",
    "按取消原因统计上周的订单数",
    "订单主要来自哪些渠道",
    "有多少异常订单",
    "有没有异常记录",
    "列出状态为异常的订单",
    "系统正常吗",
    "数据源连接正常吗",
    "门店是否正常营业",
    "列出价格波动超过 10% 的商品",
    "网络波动大吗",
    "促销活动持续了几天",
    "这个活动会持续多久",
    "为什么报错了",
    "为什么查不到数据",
    "表结构有什么变化",
    "口径有什么变化",
    "按地区拆分订单",
    "导出上周的销售明细",
    "",
    "   ",
    "How many orders did we get last week?",
    "Why is the query slow?",
]


@pytest.mark.parametrize("question, kind", POSITIVE)
def test_analysis_questions_are_recognized(question, kind):
    found = analysis_intent(question)
    assert found is not None, question
    assert found.kind == kind, (question, found)
    assert found.text and found.text.rstrip("…") in question, found
    assert is_analytical(question)


@pytest.mark.parametrize("question", NEGATIVE)
def test_other_questions_are_left_alone(question):
    assert analysis_intent(question) is None, (question, analysis_intent(question))
    assert not is_analytical(question)


def test_the_lexicon_is_versioned_and_every_kind_has_a_label():
    assert INTENT_VERSION == 1
    assert {kind for _, kind in POSITIVE} == set(KINDS)
    found = analysis_intent("上周销售额为什么下降了")
    assert isinstance(found, Intent) and found.version == INTENT_VERSION and found.label == "原因"


def test_sentences_are_read_one_at_a_time():
    """问原因的词和变化要在同一句：前一句问报错、后一句问销售额，不拼在一起认；逗号不分句。"""
    assert analysis_intent("为什么报错了？上周销售额是多少") is None
    assert analysis_intent("上周销售额是多少？跟前一周比为什么少了").kind == "why"
    # 只有那一句是在问服务的，别的句子照认
    assert analysis_intent("系统正常吗？另外这周销售额是否异常").kind == "anomaly"


def test_the_quoted_text_is_short():
    question = "为什么" + "华东" * 30 + "的销售额下降"
    found = analysis_intent(question)
    assert found is not None and len(found.text) <= 24 and found.text.endswith("…")


def test_the_intent_lexicon_is_not_the_claims_lexicon():
    """意图判用户的问题，说法核对判写作者的句子：「主要来自哪」是在问（意图认），「主要来自华东」是下判断（意图不认）。"""
    assert analysis_intent("下降主要来自哪个地区").kind == "source"
    assert analysis_intent("本周订单主要来自线上渠道") is None
