"""旧契约（按数值回指）的止血：每个数字记下位置，同值的多个指标不再默认算给第一个。

真实反例：code 节点解析失败后 13 个数值全部兜底成 0，叙述里 56 个「命中」全部算给了
同一个指标——`next(...)` 取第一个命中的。界面上那 56 个数都标着「来自指标 A」，
其实哪个都说不清。位置也没记：同一个 token 出现多次共用一个标记，只能按字符串全局
匹配，标错地方是必然的。
"""
from __future__ import annotations

import pytest

from app.engine.issuance import decide_tier, trace_numbers

ZEROS = [{"id": f"k{i}", "value": 0.0} for i in range(5)]


def test_same_value_metrics_are_ambiguous_not_first_wins():
    narrative = "订单 0.0 单，退款 0.0 单，投诉 0.0 起，复购 0.0 次，流失 0.0 户"
    report = trace_numbers(narrative, ZEROS)
    assert len(report.matched) == 5 and not report.unmatched
    for hit in report.matched:
        assert hit["metric"] is None, hit
        assert hit["ambiguous"] is True
        assert hit["candidates"] == ["k0", "k1", "k2", "k3", "k4"]


def test_every_number_carries_its_offsets():
    narrative = "本周订单 1,204 单，客单价 35.5 元，另有 7 单；复购 1,204 单"
    report = trace_numbers(narrative, [{"id": "orders", "value": 1204}, {"id": "aov", "value": 35.5}])
    assert [h["token"] for h in report.matched] == ["1,204", "35.5", "1,204"]
    for hit in [*report.matched, *report.unmatched]:
        assert narrative[hit["start"]:hit["end"]] == hit["token"], hit
    # 同一个 token 出现两次，位置各是各的
    assert report.matched[0]["start"] != report.matched[2]["start"]
    assert report.unmatched == [{"token": "7", "context": report.unmatched[0]["context"],
                                 "start": narrative.index("7 单"), "end": narrative.index("7 单") + 1}]


def test_offsets_for_chinese_numerals_and_suffixes():
    narrative = "客户两千五百家，营收 3.5万，增长三成"
    report = trace_numbers(narrative, [{"id": "c", "value": 2500}, {"id": "r", "value": 35000},
                                       {"id": "g", "value": 0.3}])
    assert [narrative[h["start"]:h["end"]] for h in report.matched] == ["两千五百", "3.5万", "三成"]


def test_unique_hit_keeps_the_old_shape():
    """只有一个指标对得上时，形状和以前一样（多了位置），老前端照常能读。"""
    report = trace_numbers("订单 120 单", [{"id": "orders", "value": 120}, {"id": "aov", "value": 35.5}])
    assert report.matched == [{"token": "120", "metric": "orders", "start": 3, "end": 6}]


def test_same_metric_id_from_two_calibers_is_not_ambiguous():
    """两张口径卡都定义了 orders：出处的指标是同一个，不算出处不唯一。"""
    report = trace_numbers("订单 120 单", [{"id": "orders", "value": 120}, {"id": "orders", "value": 120}])
    assert report.matched == [{"token": "120", "metric": "orders", "start": 3, "end": 6}]


# --------------------------------------------------------------------------
# decide_tier 的新参数：默认都是空，旧调用一个字不改、结果不变
# --------------------------------------------------------------------------

BASE = {"missing_required": [], "missing_expected": [], "unmatched": [], "strict": False}


def test_old_calls_are_unchanged():
    assert decide_tier(**BASE) == "formal"
    assert decide_tier(**{**BASE, "gaps": ["x"]}) == "degraded"
    assert decide_tier(**{**BASE, "unmatched": [{"token": "1"}], "strict": True}) == "withheld"


@pytest.mark.parametrize("strict, tier", [(False, "degraded"), (True, "withheld")])
def test_unresolved_references_count_like_unmatched_numbers(strict, tier):
    assert decide_tier(**{**BASE, "strict": strict}, unresolved=[{"ref": "m:gmvx"}]) == tier


def test_claims_are_ignored_without_a_policy():
    assert decide_tier(**BASE, unsupported=[{"unit": "u1"}], uncited_claims=3) == "formal"
    assert decide_tier(**BASE, unsupported=[{"unit": "u1"}], claims_policy="off") == "formal"


def test_claims_policy_degrades_or_withholds():
    assert decide_tier(**BASE, unsupported=[{"unit": "u1"}], claims_policy="judge") == "degraded"
    assert decide_tier(**BASE, uncited_claims=2, claims_policy="require_citation") == "degraded"
    policy = {"policy": "judge", "on_unsupported": "withhold", "on_uncited": "ignore"}
    assert decide_tier(**BASE, unsupported=[{"unit": "u1"}], claims_policy=policy) == "withheld"
    assert decide_tier(**BASE, uncited_claims=[{"unit": "u2"}], claims_policy=policy) == "formal"
    assert decide_tier(**BASE, unsupported=[], uncited_claims=0, claims_policy="judge") == "formal"
