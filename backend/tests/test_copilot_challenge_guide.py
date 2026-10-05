"""助手的节点手册要教反方审稿节点：什么时候加（多个来源汇总的分析报告、要正式出具的报告），接在哪里，各配置项怎么写。

手册里没写的节点，助手就不会用；写错的键名、取值，画布校验会一直把图打回来。所以这里不只查关键词，还拿校验认的那一份
配置项和取值来对。
"""
from __future__ import annotations

from app.api.copilot import NODE_REFERENCE
from app.engine.schema import CHALLENGE_LABEL, CHALLENGE_ON_REFUTED


def _challenge_entry() -> str:
    start = NODE_REFERENCE.index("- challenge：反方审稿")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n\n", start)]


def test_the_guide_names_every_setting_the_validator_knows():
    entry = _challenge_entry()
    assert all(key in entry for key in CHALLENGE_LABEL), [k for k in CHALLENGE_LABEL if k not in entry]
    assert all(value in entry for value in CHALLENGE_ON_REFUTED)


def test_the_guide_says_when_to_add_it_and_where():
    entry = _challenge_entry()
    # 什么时候加：多个来源汇总的分析报告、要正式出具的报告；串在报告和成果之间（降档靠成果节点收上游的缺口，
    # 接在侧枝上不生效），审批节点在后面时引用报告正文
    assert "多个来源" in entry and "正式出具" in entry and "report → challenge → output" in entry
    assert "侧枝" in entry
    assert "{{ nodes.报告节点id.text }}" in entry
    # 「报错并停止运行」配「出错时：记录错误并继续」画布校验会挡：助手别这么配
    assert "on_error: continue" in entry
    # 反方模型建议和写作模型不同：同一个模型等于自己审自己
    assert "自己审自己" in entry
    # 不写上限就取设置里的默认：助手不要替人把核查的花费写死
    assert "取设置里的默认" in entry
