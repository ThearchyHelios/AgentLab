"""写作指引：[[see:]] 只放句末；涉及码值时写含义、不写码值本身（R2 走查的 A4、B4）。

- A4：报告正文出现「以 为依据看」这样的断句。[[see:]] 渲染后不显示，写作者把它写进了句子中间
  （「以 [[see:Q1]] 为依据看，……」）。指引写明只放句末；组装文档时能稳妥认出的写法（「以 [[see:…]] 为」
  「根据 [[see:…]]」之类）报出来，交回写作者重写；重写不好也不判失败——数字和出处都没问题，只是句子不通。
- B4：报告核对把码值 1、0、9 当成没有出处的数字，每次运行都强制重写一轮。数字核对不放宽（不可回退的约定），
  指引里写明：涉及码值时写它的含义（「作废记录」），不写码值本身。
"""
from __future__ import annotations

import pytest

from app.api.copilot import NODE_REFERENCE as COPILOT_PROMPT
from app.engine.evidence import SEE_AND_CODE_RULES, build_catalog, compose_doc
from app.engine.nodes.report import REWRITE_ONLY
from tests.test_report_node import (  # noqa: F401 - 夹具按名字引入
    CLEAN,
    engine_up,
    events,
    finish,
    script,
    weekly,
)


def test_the_writer_is_told_to_put_see_at_the_end_and_write_code_meanings():
    assert "[[see:…]]" in SEE_AND_CODE_RULES and "句末" in SEE_AND_CODE_RULES
    assert "以 [[see:Q1]] 为依据" in SEE_AND_CODE_RULES          # 点名不要这样写
    assert "码值" in SEE_AND_CODE_RULES and "含义" in SEE_AND_CODE_RULES and "作废记录" in SEE_AND_CODE_RULES


def test_the_copilot_tells_report_instructions_the_same():
    """助手写报告节点的指引（copilot 的系统提示）同样说清：see 只放句末，码值写含义。"""
    assert "只能放在句末" in COPILOT_PROMPT and "以 [[see:" not in COPILOT_PROMPT.replace("不要写「以 [[see:", "")
    assert "码值" in COPILOT_PROMPT and "含义" in COPILOT_PROMPT


@pytest.fixture
def catalog():
    card = {"kind": "metric_set", "caliber": "周报口径", "caliber_version": "v1",
            "metrics": [{"id": "gmv", "name": "销售额", "value": 45678.5, "unit": "元", "format": "thousands"}]}
    return build_catalog(nodes={"card": card}, ledger=[{"kind": "metric_set", "node_id": "card", "artifact": "c" * 64}])


@pytest.mark.parametrize("text", [
    "以 [[see:m:gmv]] 为依据看，本周销售额 [[m:gmv]] 偏高。",
    "以[[see:m:gmv]]为依据，本周销售额 [[m:gmv]] 偏高。",
    "根据 [[see:m:gmv]]，本周销售额 [[m:gmv]] 偏高。",
    "本周销售额 [[m:gmv]] 偏高，详见 [[see:m:gmv]]。",
    "基于 [[see:m:gmv]] 的结果，本周销售额 [[m:gmv]] 偏高。",
])
def test_see_used_as_part_of_the_sentence_is_flagged(catalog, text):
    doc = compose_doc(text, catalog)
    [v] = [v for v in doc["violations"] if v["code"] == "inline_see"]
    assert "[[see:" in v["text"] and "句末" in v["for_model"]
    assert "断句" in v["message"] and "[[see:" not in doc["markdown"]


@pytest.mark.parametrize("text", [
    "本周销售额 [[m:gmv]] 偏高。[[see:m:gmv]]",
    "本周销售额 [[m:gmv]] 偏高 [[see:m:gmv]]。",
    "本周销售额 [[m:gmv]] 偏高 [[see:m:gmv]]，所以 [[see:m:gmv]] 下周要关注。",   # 「所以」不是介词
    "这是结论的依据 [[see:m:gmv]]。",
    "可以 [[see:m:gmv]]。",
])
def test_see_at_the_end_of_a_clause_is_fine(catalog, text):
    doc = compose_doc(text, catalog)
    assert not [v for v in doc["violations"] if v["code"] == "inline_see"], doc["violations"]


INLINE = "## 本周概览\n\n以 [[see:m:gmv]] 为依据看，本周（[[i:week]]）销售额 [[m:gmv]]，环比 [[m:wow]]；订单 [[m:orders]]。"


async def test_inline_see_is_sent_back_for_one_rewrite(engine_up, monkeypatch):
    seen = script(monkeypatch, INLINE, CLEAN)
    row = await finish(weekly({"on_violation": "fail"}))
    assert row.status == "succeeded", row.error
    assert len(seen) == 2, "句中的依据应当交回重写一次"
    assert "只能挂在句末" in str(seen[1][-1].content)


async def test_inline_see_that_survives_the_rewrite_does_not_fail_the_node(engine_up, monkeypatch):
    """数字、出处都对得上，只是句子不通：重写还这样也不判失败，留在核对结果里。"""
    assert "inline_see" in REWRITE_ONLY
    seen = script(monkeypatch, INLINE)
    row = await finish(weekly({"on_violation": "fail"}))
    assert row.status == "succeeded", row.error
    assert len(seen) == 2
    [checked] = [e.data for e in await events(row.id, "report.checked")]
    assert checked["failed"] is False and [v["code"] for v in checked["violations"]] == ["inline_see"]
