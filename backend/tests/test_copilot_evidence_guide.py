"""Copilot 的节点手册里要有口径卡、报告撰写和出具契约的写法，以及搭图规则。

手册里没有的节点，Copilot 就不会用：以前手册里连口径卡都没有，只在数据源那一节
提了一句「取数结果要进口径卡」，于是它搭出来的图全是 agent → llm → output，叙述里
的数字没有一个能回指。
"""
from __future__ import annotations

from app.api.copilot import NODE_REFERENCE, GenerateIn, _user_message
from app.engine.evidence import render_number


def test_node_reference_covers_calibers_reports_and_contracts():
    ref = NODE_REFERENCE
    for words in (
        ["- metrics：口径卡", "expression", "decimals", "format", "on_missing"],
        ["- report：报告撰写", "[[m:指标id]]", "[[i:", "metrics_from", "on_violation", "不要用 llm"],
        ["report_from", "required", "strict"],
        ["搭图规则", "口径卡 → report → output", "SUM / COUNT", "code 只做格式转换"],
    ):
        assert all(w in ref for w in words), [w for w in words if w not in ref]
    # percent_of_ratio 的小数位按比率算：照「两位小数」写 2 的话 0.0235 显示成 2%，0.004 直接显示不出来
    assert "percent_of_ratio 的 decimals 按比率算：要显示两位百分比（2.35%）写 4" in ref
    assert render_number(0.0235, decimals=4, fmt="percent_of_ratio") == "2.35%"
    assert render_number(0.0235, decimals=2, fmt="percent_of_ratio") == "2%"
    # 成果字段要原样引用报告：前后拼了字就没法逐段对应证据
    assert "{{ nodes.报告节点id.text }}" in ref
    # 老规矩还在
    assert "不要写 max_steps" in ref


def test_report_model_follows_the_same_model_rule():
    assert "llm / agent / supervisor / report 的 config.model" in NODE_REFERENCE


def test_answer_branch_mentions_traceable_reports():
    text = _user_message(GenerateIn(instruction="上周销售额多少", intent="answer"), patch=False)
    assert "report" in text and "口径卡" in text and "report_from" in text
    # 普通问数不强加报告：本期 agent 的结论还进不了口径卡，强加只会让答案里的数全判成无出处
    assert "普通的问数照旧" in text
