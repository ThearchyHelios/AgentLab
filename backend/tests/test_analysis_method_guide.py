"""平台内置的分析方法（tools/builtin/analysis.py 的 ANALYSIS_METHOD_PROMPT，第 2 波定稿，guide id analysis.method@2）。

方法随代码升版、绑了任一个分析工具就注入 Agent 的 system（注入本身见 tests/test_analysis_tools.py）。这里守方法里该写清的几件事
都在：按问题选基期、周序列的期次怎么写、先现象后拆解再下钻的顺序、写明在验证什么、拆了不集中也是结论、不作因果断言、阈值
不由模型定、算不了照实说、最后列出做过的分析；方法里点名的工具都是真有的那五个。
"""
from __future__ import annotations

import re

from app.analysis import contract
from app.tools.builtin.analysis import ANALYSIS_METHOD_PROMPT, METHOD_GUIDE
from app.tools.registry import all_specs


def test_the_guide_is_the_second_version_and_shared_by_all_five_tools():
    assert METHOD_GUIDE.id == "analysis.method@2" and METHOD_GUIDE.text == ANALYSIS_METHOD_PROMPT
    specs = all_specs()
    assert {name for name, spec in specs.items() if spec.guide is METHOD_GUIDE} == set(contract.TOOL_NAMES.values())


def test_the_method_says_how_to_pick_the_base_period():
    first = ANALYSIS_METHOD_PROMPT.split("\n2.")[0]
    for words in ("上周", "去年同期", "和目标比", "上一个整期", "lag", "52", "库里没有目标就照实说", "已经过完的整期"):
        assert words in first, words


def test_the_method_says_how_to_write_weekly_periods():
    for words in ("每周第一天的日期", "ISO 周「YYYY-Www」", "SQLite 的 %W", "不是 ISO 周", "date(日期列, '-6 days', 'weekday 1')",
                  "核对不了"):
        assert words in ANALYSIS_METHOD_PROMPT, words


def test_the_method_orders_the_steps_and_names_only_real_tools():
    text = ANALYSIS_METHOD_PROMPT
    # 先可比性和基线，再拆因子与结构，再按维度由粗到细，找到主要来源再下钻一层
    order = [text.index(w) for w in ("analysis_comparable", "analysis_baseline", "factor", "ratio", "additive", "parent")]
    assert order == sorted(order), order
    named = set(re.findall(r"\banalysis_[a-z]+\b", text))
    assert named == set(contract.TOOL_NAMES.values()), named
    assert "由粗到细" in text and "下钻一层" in text and "headline" in text


def test_the_method_keeps_the_round_rules():
    for words in ("hypothesis", "拆了没发现集中", "也是结论", "不作因果断言", "待核实的推测", "工具参数里没有阈值",
                  "算不了", "照实说原因", "【分析 An】", "判定语原话", "没做的分析不要写成做过"):
        assert words in ANALYSIS_METHOD_PROMPT, words
