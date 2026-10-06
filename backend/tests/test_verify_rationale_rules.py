"""核查 agent 交理由的写法和收尾超时记的上限（期 5 真实模型验收 M1）。

- 交结果的提示（VERIFY_FINAL_PROMPT，双盲复算、反方审稿、审批质询共用）和写作者一致：依据写在句末，不放进括号或句子
  中间。以前写的是「提到某次查询时写 [[see:Q1]]」，模型就写成「结果一致（[[see:Q3]]，原报告…）」，反方审稿的系统提示
  自己也把它写在括号里（「（[[v:Qn.rX.列名]] 或 [[see:Qn]]）」）
- 写进了括号的依据，理由文档里照样渲染干净（evidence._tidy），这句仍挂着那条依据
- 模型自己停了工具调用、收尾那次调用超过了时长上限：limit 照实记 timeout_s。以前是 None——质询的报错写着「回答超过
  时长上限（180 秒）」，limit 却是空的，界面上缺了「已达时长上限」。不走结构化输出的模型（普通调用收尾）也一样
- 交回的理由是空的、只有标点，或者只是一个占位词（字面的 placeholder、N/A）：当作没给理由，不把占位词当理由展示

模型一律是 MockChatModel 剧本，数据源是现造的 SQLite 文件，数字都是虚构的。
"""
from __future__ import annotations

import asyncio
import re
import time

import pytest

from app.engine import verify_agent
from app.engine.evidence import iter_units
from app.engine.inquiry import INQUIRY_SYSTEM, INQUIRY_TASK_PROMPT
from app.engine.nodes.challenge import CHALLENGE_SYSTEM, CHALLENGE_TASK_PROMPT
from app.engine.nodes.dual_check import BLIND_SYSTEM, BLIND_TASK_PROMPT
from app.engine.verify_agent import VERIFY_FINAL_PROMPT, VERIFY_SYSTEM
from app.providers.mock_model import MockChatModel
from tests.test_verify_agent import VALID_SUM, Script, clean_spend, q, run, shop  # noqa: F401 - 夹具按名字引入

#: 括号里写着依据标记：「（[[v:Qn.rX.列名]] 或 [[see:Qn]]）」「（[[see:Q1]]）」这种示范
_BRACKETED_SEE = re.compile(r"[（(][^（）()]*\[\[see:[^\]]*\]\][^（）()]*[）)]")
FOUND = {"value": {"alias": "Q1", "row": 0, "column": "total"}, "verdict": "一致"}


def slow_final(monkeypatch, seconds: float = 5.0) -> None:
    """交结果那次调用（消息里带着 VERIFY_FINAL_PROMPT：结构化输出、退回普通调用都是）迟迟不返回，查库的几轮照常。"""
    original = MockChatModel._agenerate

    async def held(chat, messages, stop=None, run_manager=None, **kwargs):
        if any(VERIFY_FINAL_PROMPT in str(m.content) for m in messages):
            await asyncio.sleep(seconds)
        return await original(chat, messages, stop, run_manager, **kwargs)

    monkeypatch.setattr(MockChatModel, "_agenerate", held)


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

def test_the_final_prompt_puts_see_at_the_end_of_the_sentence():
    assert "提到某次查询时写 [[see:Q1]]" not in VERIFY_FINAL_PROMPT
    assert "句末" in VERIFY_FINAL_PROMPT and "括号" in VERIFY_FINAL_PROMPT
    assert re.search(r"。\[\[see:Q\d\]\]", VERIFY_FINAL_PROMPT), "示范的写法：依据挂在句号后面"
    assert "渲染后不显示" in VERIFY_FINAL_PROMPT


@pytest.mark.parametrize("prompt", [VERIFY_SYSTEM, VERIFY_FINAL_PROMPT, CHALLENGE_SYSTEM, CHALLENGE_TASK_PROMPT,
                                    INQUIRY_SYSTEM, INQUIRY_TASK_PROMPT, BLIND_SYSTEM, BLIND_TASK_PROMPT])
def test_no_verify_prompt_shows_see_inside_brackets(prompt):
    assert not _BRACKETED_SEE.search(prompt), _BRACKETED_SEE.search(prompt).group(0)


async def test_the_final_call_is_told_the_rule(monkeypatch, shop):
    name, _ = shop
    script = Script(monkeypatch, [q(name, VALID_SUM, "c1")], {**FOUND, "rationale": "合计 [[v:Q1.r0.total]]。[[see:Q1]]"})
    await run(name)
    kind, messages = script.calls[-1]
    assert kind == "final" and VERIFY_FINAL_PROMPT in str(messages[-1].content)


# --------------------------------------------------------------------------
# 写进括号的依据
# --------------------------------------------------------------------------

async def test_see_written_in_brackets_renders_clean_in_the_rationale(monkeypatch, shop):
    name, _ = shop
    said = "未作废订单的金额合计是 [[v:Q1.r0.total]]（[[see:Q1]]），口径一致（[[see:Q1]]，已排除作废单）。"
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], {**FOUND, "rationale": said})
    out = await run(name)
    shown = "未作废订单的金额合计是 230，口径一致（已排除作废单）。"
    assert out.result["rationale"] == shown and out.rationale["markdown"] == shown
    assert out.rationale["source"] == said, "原文照存，依据标记还在"
    [unit] = [unit for _, unit in iter_units(out.rationale)]
    assert [s["alias"] for s in unit["see"]] == ["Q1", "Q1"] and unit["cites"] == ["Q1"]
    assert out.rationale["violations"] == []


# --------------------------------------------------------------------------
# 收尾超时
# --------------------------------------------------------------------------

@pytest.mark.parametrize("structured", [True, False], ids=["structured", "plain"])
async def test_a_final_call_that_runs_out_of_time_records_the_time_limit(monkeypatch, shop, structured):
    """模型查了一次就自己停下（没撞上任何上限），交结果那次调用却一直不返回：到点放弃，limit 是 timeout_s，和原因一致。"""
    name, _ = shop
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 0.3)
    script = Script(monkeypatch, [q(name, VALID_SUM, "c1")], {**FOUND, "rationale": "合计 [[v:Q1.r0.total]]。"})
    slow_final(monkeypatch)
    if not structured:
        def refuse(self, schema, **kwargs):
            raise NotImplementedError("不支持结构化输出")

        monkeypatch.setattr(MockChatModel, "with_structured_output", refuse)
    started = time.monotonic()
    out = await run(name, timeout_s=0.5)
    assert time.monotonic() - started < 3.0, "收尾那次只等到宽限结束"
    assert script.kinds == ["step", "step"], "查了一次、自己停下，交结果那次没等到回复"
    assert out.answered is True and [qd["alias"] for qd in out.queries] == ["Q1"]
    assert out.result is None and out.error == "收尾时超过时长上限，没能交结果"
    assert out.limit == "timeout_s"


async def test_a_final_call_that_runs_out_of_time_after_the_query_limit_records_the_time_limit(monkeypatch, shop):
    """循环先撞上查询次数上限、收尾又超时：没交成结果是因为超时，limit 记 timeout_s，和原因一致——记成查询次数上限的话，
    质询会显示「未能回答（已达查询次数上限）：回答超过时长上限…」，双盲复算会说盲算查满了次数（查满了照样能交结果）。
    金额被拒的收尾照旧报循环里先撞上的那个（见 test_verify_truncation）。"""
    name, _ = shop
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 0.3)
    Script(monkeypatch, [q(name, VALID_SUM, "c1"), q(name, "SELECT COUNT(*) AS n FROM orders", "c2")],
           {**FOUND, "rationale": "x"})
    slow_final(monkeypatch)
    out = await run(name, timeout_s=0.5, max_queries=1)
    assert [qd["alias"] for qd in out.queries] == ["Q1"], "查满了一次"
    assert out.result is None and out.error == "收尾时超过时长上限，没能交结果"
    assert out.limit == "timeout_s"


# --------------------------------------------------------------------------
# 占位词当理由
# --------------------------------------------------------------------------

@pytest.mark.parametrize("said", ["placeholder", "PLACEHOLDER", "  ", "N/A", "（略）", "…", "<rationale>", "TODO"])
async def test_a_placeholder_rationale_counts_as_none(monkeypatch, shop, said):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], {**FOUND, "rationale": said})
    out = await run(name)
    assert out.result["value"] == 230, "数照样取"
    assert out.result["rationale"] == "" and out.rationale is None


@pytest.mark.parametrize("said", ["无异常。", "见 [[v:Q1.r0.total]]。", "略高于上周。"])
async def test_a_short_real_rationale_is_kept(monkeypatch, shop, said):
    name, _ = shop
    Script(monkeypatch, [q(name, VALID_SUM, "c1")], {**FOUND, "rationale": said})
    out = await run(name)
    assert out.result["rationale"] and out.rationale is not None
