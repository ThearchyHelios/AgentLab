"""双盲复算、反方审稿各项配置的叫法放在 engine/labels.py（和别的节点一样），校验、执行报错和通用的 field_label /
option_label 取的是同一份。

以前双盲复算的字段叫法写在 schema.py 的 DUAL_CHECK_FIELD_LABEL 里（那时 labels.py 不归双盲复算的工作包），反方审稿的
写在 schema.py 的 CHALLENGE_LABEL 里：发布门禁、升级预览这些走 field_label 的地方拿不到它们，说成通用的「模型」「启用」，
和检查器上的「盲算模型」「启用双盲复算」对不上。和检查器（lib/terms.ts 的 DUAL_CHECK_TEXT.field、CHALLENGE_TEXT.field
与下拉选项）逐项比对在 check-verify、check-challenge 里做。
"""
from __future__ import annotations

from app.engine import labels, schema


def test_the_field_labels_live_in_labels_and_schema_reuses_them():
    assert schema.DUAL_CHECK_FIELD_LABEL is labels.DUAL_CHECK_FIELD_LABEL
    assert schema.CHALLENGE_LABEL is labels.CHALLENGE_LABEL
    assert schema.DUAL_CHECK_ON_REJECT_LABEL is labels.DUAL_CHECK_ON_REJECT_LABEL
    assert schema.CHALLENGE_ON_REFUTED_LABEL is labels.CHALLENGE_ON_REFUTED_LABEL
    # 每个可选的取值都有叫法，叫法里没有多出来的取值
    assert set(labels.DUAL_CHECK_ON_REJECT_LABEL) == set(schema.DUAL_CHECK_ON_REJECT)
    assert set(labels.CHALLENGE_ON_REFUTED_LABEL) == set(schema.CHALLENGE_ON_REFUTED)


def test_field_label_names_the_fields_of_the_two_nodes_like_the_inspector():
    assert labels.field_label("model", "dual_check") == "盲算模型"
    assert labels.field_label("provider", "dual_check") == "盲算模型 · 接入"
    assert labels.field_label("enabled", "dual_check") == "启用双盲复算"
    assert labels.field_label("on_reject", "dual_check") == "未采用原流程时"
    assert labels.field_label("model", "challenge") == "反方模型"
    assert labels.field_label("on_refuted", "challenge") == "结论被推翻时"
    assert labels.field_label("max_claims", "challenge") == "最多审几句"
    # 别的节点上的同名字段照旧
    assert labels.field_label("model", "agent") == "模型"
    for key, words in labels.DUAL_CHECK_FIELD_LABEL.items():
        assert labels.field_label(key, "dual_check") == words, key
    for key, words in labels.CHALLENGE_LABEL.items():
        assert labels.field_label(key, "challenge") == words, key


def test_option_label_uses_the_short_words_of_the_dropdowns():
    assert labels.option_label("on_refuted", "degrade") == "出具降档"
    assert labels.option_label("on_refuted", "flag") == "只标注"
    assert labels.option_label("on_refuted", "fail") == "报错并停止运行"
    assert labels.option_label("on_reject", "degrade") == "计入缺口、出具降档"
    assert labels.option_label("on_reject", "fail") == "判为失败"
