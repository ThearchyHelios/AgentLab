"""Copilot 的节点手册要教三期的写法：表名字段名、知识库原话、结论句策略，以及受管模板的报告节点怎么配。

手册里没写的配置，Copilot 就不会用；写错的写法，报告节点会一直被打回。所以这里不只查关键词，
还把手册里举的标记例子交给真正的解析器，确认写法和系统认的是同一套。
"""
from __future__ import annotations

import re

from app.api.copilot import NODE_REFERENCE, GenerateIn, _user_message
from app.engine.evidence import QUOTE_MIN, SUPPORTED_KINDS, parse_markers
from app.engine.schema import REPORT_CLAIMS


def _report_section() -> str:
    start = NODE_REFERENCE.index("- report：")
    return NODE_REFERENCE[start:NODE_REFERENCE.index("\n- output", start)]


def test_the_report_entry_teaches_entities_quotes_and_claims():
    report = _report_section()
    for words in (
        ["[[t:", "[[c:", "反引号", "可能是编造的名字"],               # 表名、字段名
        ["[[q:K1|", "逐字", f"至少 {QUOTE_MIN} 个字", "[[see:K1]]"],     # 知识库原话
        ["claims", "require_citation", "off", "judge", "后续版本"],    # 结论句策略
        ["entities", "link"],                                         # 实体层开关
    ):
        assert all(w in report for w in words), [w for w in words if w not in report]
    # 结论句策略的取值和校验认的是同一份
    assert all(value in report for value in REPORT_CLAIMS)


def test_the_markers_in_the_guide_parse_as_the_system_reads_them():
    kinds = {m["kind"] for m in parse_markers(_report_section())}
    assert {"t", "c", "q", "m", "v", "table", "i", "see"} <= kinds
    assert kinds <= SUPPORTED_KINDS
    [quote] = [m for m in parse_markers(_report_section()) if m["kind"] == "q"]
    assert quote["ref"] == "K1" and quote["quote"]


def test_governed_templates_spell_out_the_report_settings():
    ref = NODE_REFERENCE
    governed = ref[ref.index("受管"):]
    for words in (['numbers: "strict"', 'on_violation: "fail"', 'claims: "require_citation"'],
                  ["report_from", "narrative"], ["cite_fields: true"], ["evidence_role", "source"]):
        assert all(w in governed for w in words), [w for w in words if w not in governed]


def test_the_answer_branch_mentions_names_and_quotes():
    text = _user_message(GenerateIn(instruction="上周哪张表的退款最多", intent="answer"), patch=False)
    assert "[[t:" in text and "反引号" in text and "编造" in text
    assert "[[q:K1|" in text and "逐字" in text
    # 问数是探索运行：不要替人打开结论句策略
    assert not re.search(r"claims\s*[:=]\s*['\"]?require_citation", text)
