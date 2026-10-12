"""审批质询的回答：依据写进括号不再断句，收尾超时记时长上限，占位词不当回答（期 5 真实模型验收 M1）。

真实模型验收里，质询 agent 写的「结果一致（[[see:Q3]]，原报告 SQL 未按 status 过滤）」「合计 [[v:Q1.r1.金额合计]]
（[[see:Q1]]）」渲染成「（，原报告…）」「（）」，随正式运行封存；另一次回答查完了，收尾那次调用超过时长上限，报错写着
「回答超过时长上限（180 秒）」，answer.limit 却是 null，界面上缺了「已达时长上限」。

回答只写了依据（「（[[see:Q1]]）」）时显示出来是空的：以前照样交回，封存之后的追问末尾补上「注：以上回答依据的是数据源
当前的数据…」，回答正文只剩这句系统说明、却记成回答成功。现在按没给回答记。回答就是一个「无。」的，照常是回答。

模型一律是 MockChatModel 剧本，区域、金额都是虚构的。
"""
from __future__ import annotations

import pytest

from app.core import artifact_store
from app.engine import verify_agent
from app.engine.inquiry import NO_ANSWER
from tests.test_inquiry_api import (  # noqa: F401 - 夹具按名字引入
    EAST,
    NO_TEST,
    QUESTION,
    Models,
    Play,
    ask,
    client,
    engine_up,
    events,
    finish,
    pause,
    pending,
    report_doc,
    set_verify,
    shop,
    unit_of,
)
from tests.test_verify_rationale_rules import slow_final

#: 依据写进了括号：开头跟着逗号的、只有依据的
BRACKETED = ("仍居各区域之首（[[see:Q1]]，已排除测试账号）：华东为 [[v:Q1.r0.销售额]]（[[see:Q1]]），"
             "华南为 [[v:Q1.r1.销售额]]（同口径，[[see:Q1]]）。")
BRACKETED_TEXT = "仍居各区域之首（已排除测试账号）：华东为 1,200，华南为 900（同口径）。"
#: 两个只装着依据的括号连着写、数字包着粗体（复核第 2 轮）：去掉括号之后两个数不能挨成一个（「1,200900」），句末前悬着的
#: 逗号也去掉
TOGETHER = ("华东、华南分别为 **[[v:Q1.r0.销售额]]**（[[see:Q1]]）（[[see:Q1]]）[[v:Q1.r1.销售额]]，"
            "华东仍居各区域之首，（[[see:Q1]]）（[[see:Q1]]）。")
TOGETHER_TEXT = "华东、华南分别为 1,200 900，华东仍居各区域之首。"
#: 依据直接夹在两个数之间（两边什么都没有）、写进方头括号（复核第 3 轮）：两个数不能挨成一个，括号不能剩下「【】」
BARE = "华东、华南分别为 [[v:Q1.r0.销售额]][[see:Q1]][[v:Q1.r1.销售额]]，华东仍居各区域之首【[[see:Q1]]】。"
BARE_TEXT = "华东、华南分别为 1,200 900，华东仍居各区域之首。"


async def _asked(client, shop) -> tuple[dict, list]:
    paused = await pause(shop)
    [approval] = await pending(paused.id)
    r = await ask(client, paused.id, report="write", unit=None, question=QUESTION, approval_id=approval.id)
    assert r.status_code == 200, r.text
    return r.json()["inquiry"], await events(paused.id, "approval.inquiry")


@pytest.mark.parametrize(("said", "shown"), [(BRACKETED, BRACKETED_TEXT), (TOGETHER, TOGETHER_TEXT), (BARE, BARE_TEXT)],
                         ids=["bracketed", "together", "bare"])
async def test_see_in_brackets_does_not_break_the_answer(monkeypatch, shop, client, said, shown):
    Models(monkeypatch, shop, plays={"排除测试账号": Play([NO_TEST], said)})
    item, [event] = await _asked(client, shop)
    answer = item["answer"]
    assert answer["ok"] is True and answer["text"] == shown
    doc = artifact_store.load(answer["doc_artifact"])
    assert doc["markdown"] == shown and doc["source"] == said
    assert doc["violations"] == []
    assert event.data["answer_text"] == shown


async def test_a_final_answer_that_runs_out_of_time_reports_the_time_limit(monkeypatch, shop, client):
    """质询 agent 查完了、自己停下，交回答那次调用超过时长上限：报错说时长上限，limit 也是 timeout_s（事件里同样）。"""
    await set_verify({"inquiry": {"timeout_s": 0.5}})
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 0.3)
    models = Models(monkeypatch, shop)
    slow_final(monkeypatch)
    item, [event] = await _asked(client, shop)
    answer = item["answer"]
    assert answer["ok"] is False and answer["text"] == "" and answer["doc_artifact"] is None
    assert answer["error"] == "回答超过时长上限（0.5 秒），未能给出回答，可缩小问题范围后重新提问"
    assert answer["limit"] == "timeout_s"
    assert [q["sql"] for q in item["queries"]] == [NO_TEST], "查到的照记"
    assert (event.data["limit"], event.data["error"]) == ("timeout_s", answer["error"])
    assert models.finals == [], "交回答那次没等到回复"


async def test_a_placeholder_answer_is_no_answer(monkeypatch, shop, client):
    Models(monkeypatch, shop, plays={"排除测试账号": Play([NO_TEST], "placeholder")})
    item, _ = await _asked(client, shop)
    answer = item["answer"]
    assert (answer["ok"], answer["text"], answer["limit"]) == (False, "", None)
    assert answer["error"] == NO_ANSWER


async def test_an_answer_that_shows_nothing_is_no_answer_even_after_the_seal(monkeypatch, shop, client):
    """封存之后的追问：回答只写了依据，显示出来是空的。以前回答文档照样组装，末尾补上「查的是现在的数据」那句说明，
    回答正文只剩系统说明、却记成回答成功。"""
    Models(monkeypatch, shop, plays={"排除测试账号": Play([NO_TEST], "（[[see:Q1]]）")})
    done = await finish(shop)
    assert done.status == "succeeded", done.error
    r = await ask(client, done.id, report="write", unit=unit_of(await report_doc(done.id), EAST), question=QUESTION)
    assert r.status_code == 200, r.text
    answer = r.json()["inquiry"]["answer"]
    assert (answer["ok"], answer["text"], answer["doc_artifact"]) == (False, "", None)
    assert answer["error"] == NO_ANSWER


async def test_a_one_word_answer_is_an_answer(monkeypatch, shop, client):
    """问的是「有没有」，答「无。」：这是回答，不是占位词，不能报「模型没有给出回答内容」让提问人重问、再花一次钱。"""
    Models(monkeypatch, shop, plays={"排除测试账号": Play([NO_TEST], "无。[[see:Q1]]")})
    item, [event] = await _asked(client, shop)
    answer = item["answer"]
    assert (answer["ok"], answer["text"], answer["error"]) == (True, "无。", None)
    assert event.data["answer_text"] == "无。"
