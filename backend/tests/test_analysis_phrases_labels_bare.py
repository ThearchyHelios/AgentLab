"""叫法消毒按词认判断词（scan bare）：叫法是一个名字，整句话才成立的不核规矩（没说变化的「主要来自」不算判断）对它不适用。"""
from __future__ import annotations

from app.analysis.phrases import clean_label


def test_a_label_that_carries_a_judgement_without_a_change_word_is_still_refused():
    assert clean_label("主要来自华东的销售额") == ""
    assert clean_label("集中在周末的订单数") == ""


def test_plain_labels_survive():
    assert clean_label("销售额") == "销售额"
    assert clean_label("线上订单数") == "线上订单数"
