"""「今天、昨天、上周、本月、去年」一律按运行当天算。

部署上撞过：问「上周各条线的产量」，数据只更新到两周前；查库时拿表里最大的日期往前推七天当「上周」，报告照着写成
「上周产量……」，读的人以为是上周的数。现在：
- 助手的节点手册（画布、问数据共用）的搭图规则里有一条：相对日期按运行当天算（SQL 用当天的写法，agent 先调
  current_time），不用 MAX(日期) 去推；
- 报告写作规则告诉写作者运行当天是哪天、上周是哪几天：数据没覆盖问的那段时间时先照实说、写明数据最新到哪天；可以给出
  最近一段的数，但写具体起止日期，不叫它「上周」。
数据、日期都是虚构的。
"""
from __future__ import annotations

from datetime import date

from app.api.copilot import NODE_REFERENCE
from app.engine.evidence import period_rule
from tests.test_report_node import engine_up, finish, node, script  # noqa: F401


def _build_rules() -> str:
    start = NODE_REFERENCE.index("搭图规则（有数字结论时必须遵守）")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n\n模型字段", start)]


def test_the_copilot_reckons_relative_dates_from_the_run_day():
    rules = _build_rules()
    assert "今天、昨天、上周、本月、去年" in rules and "运行当天" in rules
    assert "CURRENT_DATE" in rules and "current_time" in rules        # SQL 用当天的写法；agent 先取当天日期
    assert "MAX(日期)" in rules and "不要" in rules                    # 点名不要拿数据里最大的日期去推


def test_the_writer_rule_names_the_run_day_and_last_week():
    rule = period_rule(date(2031, 3, 12))                              # 虚构的一天：周三
    assert "2031-03-12（周三）" in rule
    assert "上周是 2031-03-03 至 2031-03-09" in rule                    # 上周一到上周日
    assert "不按数据里最新的日期推" in rule
    assert "数据最新到哪天" in rule and "起止日期" in rule and "不要把它叫作「上周」" in rule


def test_last_week_on_a_monday_and_a_sunday():
    assert "上周是 2031-03-03 至 2031-03-09" in period_rule(date(2031, 3, 10))   # 周一
    assert "上周是 2031-03-03 至 2031-03-09" in period_rule(date(2031, 3, 16))   # 周日：本周还没过完，上周照旧是前一周


async def test_the_report_prompt_carries_the_run_day(monkeypatch):
    from app.engine.nodes import report as report_mod

    monkeypatch.setattr(report_mod, "run_today", lambda: date(2031, 3, 12))
    seen = script(monkeypatch, "没有数据。")
    graph = {
        "nodes": [
            node("start", "input", fields=[{"name": "q", "default": "上周的产量是多少"}]),
            node("write", "report", instructions="回答：{{ input.q }}"),
            node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]),
        ],
        "edges": [{"source": "start", "target": "write"}, {"source": "write", "target": "out"}],
    }
    await finish(graph)
    prompt = "\n".join(str(m.content) for m in seen[0])
    assert "今天是 2031-03-12（周三）" in prompt and "上周是 2031-03-03 至 2031-03-09" in prompt
    from app.engine.evidence import UNIT_RULE

    assert UNIT_RULE in prompt, "[[m:]] 自带单位的那条也在写作规则里"
