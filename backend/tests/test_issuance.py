"""出具校验的数字回指：叙述里写出来的每个量都得在指标集里找得到。

以前只认阿拉伯数字——"增长三成、客户两千五百家"在 strict 契约下判 formal，
同样的意思写成 2500 却会被 withheld。叙述层换一种写法就绕过了整套"不许现编
数字"的机制，而"出错自动改一次"的复核回路甚至会把模型往这条路上推。

中文数字难在两头：漏抽就是绕法，误抽就是满篇降档（一个问题、十分重要、从三个
方面看都不是数据）。所以这组用例两头都守。
"""
from __future__ import annotations

import pytest

from app.engine import review as rv
from app.engine.issuance import cn_to_number, decide_tier, extract_numbers, trace_numbers


def values(text: str) -> list[tuple[str, float]]:
    return [(t.raw, round(t.value, 2)) for t in extract_numbers(text)]


@pytest.mark.parametrize("text, expected", [
    ("十二", 12), ("二十三", 23), ("一百零五", 105), ("两千五百", 2500),
    ("两千五", 2500),              # 省略写法：千后面省掉了"百"
    ("一万二", 12000), ("三万零五百", 30500), ("一亿两千万", 1.2e8),
    ("三点五", 3.5), ("三点五万", 35000), ("三万亿", 3e12), ("万", 1e4),
    ("五一", None), ("三三两两", None), ("一一", None), ("十十", None),
    ("万一", None),                # 不是一万一千
    ("二〇二六", None),            # 年份的逐位写法不是一个数
])
def test_cn_to_number(text, expected):
    assert cn_to_number(text) == (None if expected is None else float(expected))


@pytest.mark.parametrize("text, expected", [
    ("本周营收增长三成", [("三成", 30.0)]),
    ("客户两千五百家", [("两千五百", 2500.0)]),
    ("其中一半来自华东", [("一半", 50.0)]),
    ("翻了两倍", [("两倍", 2.0)]),
    ("约三分之一的门店", [("三分之一", 33.33)]),
    ("达到百分之三十", [("百分之三十", 30.0)]),
    ("关闭了两家门店，用时五天", [("两", 2.0), ("五", 5.0)]),
    ("新增两千余家", [("两千余", 2500.0)]),       # 按区间 [2000, 3000] 回指
    ("营收 3.5万，客户 2千家", [("3.5万", 35000.0), ("2千", 2000.0)]),
])
def test_quantities_written_in_chinese_are_extracted(text, expected):
    assert values(text) == expected


@pytest.mark.parametrize("text", [
    "这是一个积极的信号，一些门店一起统一调价，十分重要",
    "万一出问题千万不要慌，五花八门、千方百计、独一无二",
    "第三季度和三季度营收，周三开会",
    "十二月十五日，二〇二六年九月",
    "从三个方面看，主要原因之一",
    "几十家、数百家、上千家、十几家",
    "十一假期期间，下午三点给出三点建议",
])
def test_figurative_numerals_are_not_extracted(text):
    assert values(text) == [], "误抽了"


def test_the_bypass_that_used_to_pass_as_formal():
    """strict 契约下，指标集里没有的中文数字要让它 withheld，而不是盖 formal 章。"""
    narrative = "本周营收增长三成、客户两千五百家。"
    report = trace_numbers(narrative, [{"id": "revenue", "value": 1_234_567}])
    assert [u["token"] for u in report.unmatched] == ["三成", "两千五百"]
    tier = decide_tier(missing_required=[], missing_expected=[], unmatched=report.unmatched,
                       strict=True)
    assert tier == "withheld"


def test_chinese_numerals_that_trace_back_pass():
    """换成中文写法本身不是问题——回指得上就照常 formal。"""
    narrative = "本周营收增长三成、客户两千五百家，近一半来自华东。"
    metrics = [
        {"id": "growth", "value": 0.2987},     # 比率：三成 = 30% ± 半成
        {"id": "customers", "value": 2500},
        {"id": "east_share", "value": 48.6},   # 百分数：一半 = 50% ± 5
    ]
    report = trace_numbers(narrative, metrics)
    assert report.ok, report.unmatched
    assert decide_tier(missing_required=[], missing_expected=[], unmatched=[], strict=True) == "formal"


def test_wan_suffix_tolerance_follows_written_precision():
    """3.5万写到千位，按 ±500 回指——以前按 3.5 的一位小数给 ±0.05，只认 35000 整。"""
    assert trace_numbers("营收 3.5万", [{"id": "r", "value": 35123}]).ok
    assert not trace_numbers("营收 3.5万", [{"id": "r", "value": 36000}]).ok
    assert trace_numbers("营收 35万", [{"id": "r", "value": 351234}]).ok


def test_ranges_trace_within_their_span():
    assert trace_numbers("新增两千余家", [{"id": "n", "value": 2150}]).ok
    assert not trace_numbers("新增两千余家", [{"id": "n", "value": 3150}]).ok


def test_review_may_rewrite_numbers_in_chinese_but_not_invent_them():
    """复核重写时把 6 写成"六"不算新数字；多出来一个"五家"才算。"""
    assert rv.check_numbers("管理员有 6 个。", "管理员有六个。") == []
    assert rv.check_numbers("管理员有 6 个。", "管理员有六个，商家有五家。") == ["五"]
