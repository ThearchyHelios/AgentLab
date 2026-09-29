"""Copilot 的节点手册要教三期、四期的写法：表名字段名、知识库原话、结论句策略（含模型裁判 judge），以及受管模板的
报告节点怎么配。

手册里没写的配置，Copilot 就不会用；写错的写法，报告节点会一直被打回。所以这里不只查关键词，
还把手册里举的标记例子交给真正的解析器，确认写法和系统认的是同一套。
"""
from __future__ import annotations

import re

from app.api.copilot import _ASSIST_RULES, NODE_REFERENCE, GenerateIn, _user_message
from app.engine.evidence import QUOTE_MIN, SUPPORTED_KINDS, parse_markers
from app.engine.schema import CLAIMS_VALUES, JUDGE_KEYS, JUDGE_ON_UNSUPPORTED


def _report_section() -> str:
    start = NODE_REFERENCE.index("- report：")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n- output", start)]


def test_the_report_entry_teaches_entities_quotes_and_claims():
    report = _report_section()
    for words in (
        ["[[t:", "[[c:", "反引号", "可能是编造的名字"],               # 表名、字段名
        ["[[q:K1|", "逐字", f"至少 {QUOTE_MIN} 个字", "[[see:K1]]"],     # 知识库原话
        ["claims", "require_citation", "off", "judge"],               # 结论句策略
        ["judge.max_cost_usd", "null", "不限", "rewrite_once", "on_unsupported", "按需"],   # 模型裁判怎么配
        ["entities", "link"],                                         # 实体层开关
    ):
        assert all(w in report for w in words), [w for w in words if w not in report]
    # 结论句策略的取值和校验认的是同一份；judge 已经能用，不能再说「后续版本」「跑不起来」
    assert all(value in report for value in CLAIMS_VALUES)
    assert all(value in report for value in JUDGE_ON_UNSUPPORTED)
    assert "后续版本" not in report and "跑不起来" not in report
    # 裁判模型建议和写作模型不同：否则等于自己审自己
    assert "不同" in report and "自己审自己" in report


def test_the_judge_keys_in_the_guide_are_the_ones_the_validator_knows():
    named = set(re.findall(r"judge\.(\w+)", NODE_REFERENCE))
    assert named and named <= set(JUDGE_KEYS), named - set(JUDGE_KEYS)


def test_the_markers_in_the_guide_parse_as_the_system_reads_them():
    kinds = {m["kind"] for m in parse_markers(_report_section())}
    assert {"t", "c", "q", "m", "v", "table", "i", "see"} <= kinds
    assert kinds <= SUPPORTED_KINDS
    [quote] = [m for m in parse_markers(_report_section()) if m["kind"] == "q"]
    assert quote["ref"] == "K1" and quote["quote"]


def test_governed_templates_spell_out_the_report_settings():
    ref = NODE_REFERENCE
    governed = ref[ref.index("要按受管级别发布的模板"):]
    for words in (['numbers: "strict"', 'on_violation: "fail"', 'claims: "require_citation"'],
                  ['claims: "judge"', "judge.max_cost_usd", "null"],
                  ["report_from", "narrative"], ["cite_fields: true"], ["evidence_role", "source"]):
        assert all(w in governed for w in words), [w for w in words if w not in governed]


def test_the_publish_assist_knows_judge_is_the_stricter_one():
    """发布前的 Copilot 兜底只许往严里改：它要知道 judge 比 require_citation 严，不能为了过门禁把 judge 改回去。"""
    assert "judge" in _ASSIST_RULES and "max_cost_usd" in _ASSIST_RULES
    # update_node 把 judge 整个换掉：补预算只写 {max_cost_usd} 会把 withhold 连同裁判模型一起丢了
    rules = " ".join(_ASSIST_RULES.split())
    assert re.search(r"on_unsupported.{0,20}withhold.{0,20}不改回\s*degrade", rules), "withhold 不许改回 degrade"
    assert re.search(r"补预算.{0,40}整份", rules), "补预算时 judge 要整份带上原来的键"


def test_the_answer_branch_mentions_names_and_quotes():
    text = _user_message(GenerateIn(instruction="上周哪张表的退款最多", intent="answer"), patch=False)
    assert "[[t:" in text and "反引号" in text and "编造" in text
    assert "[[q:K1|" in text and "逐字" in text
    # 问数是探索运行：不要替人打开结论句策略（结论句由读的人点开时按需请模型判断）
    assert not re.search(r"claims\s*[:=]\s*['\"]?(require_citation|judge)", text)
    assert "按需" in text
