"""助手搭多来源汇总的分析报告时，默认给报告撰写节点开结论句裁判（claims: judge）。

真实运行里：四个 Agent 分头查库、汇总成一份分析报告，报告节点没写 claims。有一句结论和证据相矛盾，审批人批准时
完全不知道，事后在证据面板点开那句才判出来。分析报告的结论句恰恰最需要核对：几个来源拼在一起，最容易写出
证据撑不住的归因和判断。

取舍：默认写 claims: "judge"、不写 judge 的预算（取设置里的默认）——正式运行在节点里自动判，探索运行不在节点里
花钱、由读的人按需判。和「受管级别的 judge 必须写预算、拿不准就问」不冲突：那条只管要按受管级别发布的模板，
普通草稿不问预算；发布前修复里缺预算要问的规矩不变。用户明确说不要核对结论句的不加。问数据那条路径原本定了
不替人打开结论句策略（一律探索运行，读的人按需判），那条保留，并写明节点说明里的默认在问数里不适用。

这组守的是节点手册和问数据那条路径里的说法，以及它们和受管级别规则、发布前修复规则不打架。
"""
from __future__ import annotations

from app.api.copilot import NODE_REFERENCE, GenerateIn, _publish_fix_request, _user_message


def _report_rule() -> str:
    start = NODE_REFERENCE.index("- report：报告撰写")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n- output 的出具契约", start)]


def test_multi_source_analysis_reports_default_to_the_judge():
    rule = _report_rule()
    assert '默认写 claims: "judge"' in rule
    # 什么算多来源：几个 Agent 分头查、或者几次查询的结果合在一起写
    assert "多个来源汇总成的分析报告" in rule and "几个 agent" in rule and "几次查询" in rule
    # 不写预算，交给设置里的默认值
    assert "不写 judge 的预算" in rule and "设置里的默认" in rule
    # 正式运行在节点里自动判，探索运行由读的人按需判
    assert "正式运行在节点里自动判" in rule and "探索运行由读的人" in rule
    # 用户明确不要的不加
    assert "用户明确说不要" in rule


def test_the_managed_level_budget_rule_still_applies_and_says_so():
    # 受管级别仍然要写预算、拿不准就问；默认开裁判的那一条说明它让位给受管级别的规则
    assert "judge 里必须写 judge.max_cost_usd" in NODE_REFERENCE
    assert "拿不准就问" in NODE_REFERENCE
    rule = _report_rule()
    assert "受管级别" in rule
    # 发布前修复（受管级别）缺预算时照旧问人，不替人定
    request = _publish_fix_request({"nodes": [], "edges": []}, [], "governed")
    assert "judge 缺预算（judge.max_cost_usd）时输出 question 问写多少" in request


def test_answer_path_keeps_its_own_rule_and_says_which_one_wins():
    """问数据那条路径原本就定了不替人打开结论句策略（test_copilot_entity_guide 守着）：问数一律是探索运行，读答案的人
    点开哪句判哪句。节点说明里新加的默认和它说法相反，所以这里写明那条默认在问数里不适用，免得模型两头为难。"""
    text = _user_message(GenerateIn(instruction="各渠道上月表现怎么样", intent="answer"), patch=False)
    assert "report 的 claims 不用写" in text
    assert "分析报告默认开结论句裁判」那条在问数里不适用" in text
