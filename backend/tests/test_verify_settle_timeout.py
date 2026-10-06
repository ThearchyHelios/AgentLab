"""核查 agent 收尾超时之后，反方审稿、双盲复算对外怎么说（期 5 真实模型验收 M1 的复核）。

核查 agent 查完了、自己停下，交结果那次调用超过了时长上限：verify_agent 照实记 limit=timeout_s（以前模型自己停下时是
None，test_verify_rationale_rules 钉住了这一层）。两个节点据此按时长上限说，不再把核查 agent 的原话（「收尾时超过时长
上限，没能交结果」）摆上审批卡、时间线：

- 反方审稿：这句计入 challenge_limit（limit=timeout_s，claims_skipped 算上它），不再记成 challenge_unreviewed；审稿记录
  里这句的理由是「反查未完成：已达上限（每句时长上限 N 秒）」。时长按剩余时间收紧过的那组带 cause=run_deadline，理由
  说运行时长上限将至
- 双盲复算：无法复算的原因是「盲算已达每项时长上限（N 秒）」，发 dual_check_limit；收紧过的说运行时长上限将至，不发
  dual_check_limit（它叫人调高每项的上限，调高了只会更快撞线），时间线另起一条带 cause 的 dual_check_unchecked

交结果那次用 slow_final 拖住，收尾宽限调到 0.3 秒；收紧过的那组把 allot() 看到的钟拨到「给下游留出余量、扣掉收尾宽限
之后只剩 0.8 秒」（真的钟照走，核查 agent 按真的 0.8 秒到点）。模型一律是 MockChatModel 剧本，数据源是现造的 SQLite
文件，区域、金额都是虚构的。
"""
from __future__ import annotations

import pytest

from app.core import artifact_store
from app.engine import deadline, verify_agent
from tests.fixtures.dual_check_shop import RIGHT, Blind
from tests.test_challenge_node import (  # noqa: F401 - 夹具按名字引入
    Models,
    engine_up,
    finish,
    graph,
    logs,
    output_of,
    shop,
)
from tests.test_dual_check_node import clean_settings, dual_output, events, start  # noqa: F401
from tests.test_dual_check_node import graph as dual_graph
from tests.test_dual_check_node import logs as dual_logs
from tests.test_dual_check_node import shop as dual_shop  # noqa: F401
from tests.test_verify_rationale_rules import slow_final

#: 收紧过的那组：allot() 算出的查库时长
ROOM = 0.8
#: 收紧过的时长到点时，反方审稿每句的理由、双盲复算的无法复算原因（默认 600 秒的一段）
SQUEEZED_CLAIM = "反查未完成：已达上限（本次运行即将达到 600 秒的时长上限，反查时长已按剩余时间收紧）"
SQUEEZED_BLIND = "本次运行即将达到 600 秒的时长上限，盲算时长已按剩余时间收紧，到时未能完成"


@pytest.fixture
def settles_slowly(monkeypatch):
    """交结果那次调用拖 5 秒，收尾宽限只给 0.3 秒：查完了的核查一律在收尾时超时。"""
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 0.3)
    slow_final(monkeypatch)


def squeeze(monkeypatch) -> None:
    """allot() 看到的「现在」：这一段的截止时刻扣掉给下游的余量（下游只有成果节点，一份）、收尾宽限，再往前 ROOM 秒。
    每开始一项都一样，所以每一项的时长都收紧到 ROOM。"""
    def now() -> float:
        found = deadline.current()
        return found.deadline - deadline.reserve(found.seconds) - verify_agent.SETTLE_GRACE_S - ROOM - 0.05

    monkeypatch.setattr(deadline, "_now", now)


# --------------------------------------------------------------------------
# 反方审稿
# --------------------------------------------------------------------------

async def test_a_claim_whose_verdict_runs_out_of_time_counts_as_hitting_the_limit(monkeypatch, shop, settles_slowly):
    models = Models(monkeypatch, shop)
    row = await finish(graph(shop, timeout_s=0.5), run_class="exploratory")
    assert row.status == "succeeded", row.error
    assert [kind for kind, _, _ in models.calls].count("final") == 0, "交结论那次都没等到回复"

    [limit] = await logs(row.id, "challenge_limit")
    assert (limit["limit"], limit["claims_skipped"]) == ("timeout_s", 2)
    assert "cause" not in limit and "每句时长上限 0.5 秒" in limit["message"] and "2 句未能给出结论" in limit["message"]
    assert await logs(row.id, "challenge_unreviewed") == [], "触了上限的由 challenge_limit 说，不另记成未能反查"

    out = await output_of(row.id, "chal")
    assert out["counts"]["inconclusive"] == 2 and out["unreviewed"] == 2
    record = artifact_store.load(out["challenge_artifact"])
    for claim in record["claims"]:
        assert claim["verdict"] == "inconclusive" and claim["rationale_doc"] is None
        assert claim["rationale"] == "反查未完成：已达上限（每句时长上限 0.5 秒）"
        assert len(claim["queries"]) == 1, "查到的照记"
    assert "没能交结果" not in str(record)
    assert row.output["_issuance"]["tier"] == "formal", "没给出结论的句子不让出具降档"


async def test_a_squeezed_claim_whose_verdict_runs_out_of_time_says_the_run_deadline(monkeypatch, shop,
                                                                                     settles_slowly):
    Models(monkeypatch, shop)
    squeeze(monkeypatch)
    row = await finish(graph(shop), run_class="exploratory")
    assert row.status == "succeeded", row.error

    [limit] = await logs(row.id, "challenge_limit")
    assert (limit["limit"], limit["claims_skipped"], limit["claims"]) == ("timeout_s", 2, 2)
    assert limit["cause"] == deadline.RUN_DEADLINE and limit["run_seconds"] == 600
    assert "每句时长上限" not in limit["message"], "收紧过的不叫人调高每句的上限"
    assert await logs(row.id, "challenge_unreviewed") == []
    record = artifact_store.load((await output_of(row.id, "chal"))["challenge_artifact"])
    assert [c["rationale"] for c in record["claims"]] == [SQUEEZED_CLAIM, SQUEEZED_CLAIM]


# --------------------------------------------------------------------------
# 双盲复算
# --------------------------------------------------------------------------

async def test_a_blind_check_whose_result_runs_out_of_time_names_the_time_limit(monkeypatch, dual_shop,
                                                                                settles_slowly):
    name, _ = dual_shop
    blind = Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(dual_graph(name, RIGHT), options={"verify": {"dual_check": {"timeout_s": 0.5}}})
    assert run.status == "succeeded", run.error
    assert blind.runs == 0, "交结果那次没等到回复"
    evs = await events(run.id)
    assert [(d["limit"], d["metric"]) for d in dual_logs(evs, "dual_check_limit")] == [("timeout_s", "gmv")]
    out = dual_output(run, evs)
    assert out["status"] == "unchecked" and out["metrics"][0]["status"] == "unchecked"
    assert out["metrics"][0]["reason"] == "盲算已达每项时长上限（0.5 秒）"
    assert out["issuance_gaps"] == ["指标「销售额」无法复算：盲算已达每项时长上限（0.5 秒）"]
    [said] = dual_logs(evs, "dual_check_unchecked")
    assert "cause" not in said and said["metrics"][0]["reason"] == "盲算已达每项时长上限（0.5 秒）"
    assert "没能交结果" not in str(out)


async def test_a_squeezed_blind_check_whose_result_runs_out_of_time_says_the_run_deadline(monkeypatch, dual_shop,
                                                                                         settles_slowly):
    name, _ = dual_shop
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    squeeze(monkeypatch)
    run = await start(dual_graph(name, RIGHT))
    assert run.status == "succeeded", run.error
    evs = await events(run.id)
    assert dual_logs(evs, "dual_check_limit") == [], "收紧过的不叫人调高每项的上限"
    out = dual_output(run, evs)
    assert out["metrics"][0]["status"] == "unchecked" and out["metrics"][0]["reason"] == SQUEEZED_BLIND
    [said] = dual_logs(evs, "dual_check_unchecked")
    assert said["cause"] == deadline.RUN_DEADLINE and said["run_seconds"] == 600
