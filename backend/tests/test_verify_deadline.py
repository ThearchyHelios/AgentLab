"""核查节点在运行时长上限内收尾（engine/deadline.py）：反方审稿逐句、双盲复算逐项开始之前，看这一段执行还剩多少时间。

- 反方审稿：5 句、截止时刻只够审 2 句 → 审 2 句，其余 3 句记为未能反查（理由、时间线写明运行时长上限将至），运行
  成功、不被中止，日志和产出的 unreviewed 对得上；剩下的时间不够整段时这句的时长收紧，到点按查到的结果收尾，说的是
  运行时长上限将至，不说「每句时长上限 90 秒」；接着运行时取 checkpoint 里的审稿，不重审
- 双盲复算：同理，没来得及开始的盲算按无法复算记（照现有的路径记缺口）；收紧过的时长到点也按无法复算记，不发提醒
  调高上限的 dual_check_limit；审批恢复后重放，盲算不再执行、哪几项没来得及照旧
- 时间充裕时（默认 600 秒的一段）时长上限照传原值：和没有这道检查时一样

两种钟：精确的计数用假的钟（每派出一项就把 allot() 看到的钟往前拨若干秒，真实只过去几毫秒，runner 的 asyncio.timeout
远远不到）；「运行不被中止」用真的钟、真的短时长上限和真的慢 agent，只断言不随机器快慢变的事。模型都是 MockChatModel
剧本，数据源是现造的 SQLite 文件，区域、金额都是虚构的。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core import artifact_store
from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Approval, Run
from app.engine import deadline, verify_agent
from app.engine.nodes import challenge as challenge_mod
from app.engine.nodes import dual_check as dual_check_mod
from app.engine.runner import run_manager, verify_manifest
from app.main import app
from app.providers.mock_model import MockChatModel
from tests.fixtures.dual_check_shop import WEEK, Blind, add_source, build
from tests.fixtures.sources import drop_source
from tests.test_challenge_node import (  # noqa: F401 - engine_up、shop 是夹具，按名字引入
    NET,
    REFUNDS,
    Models,
    Play,
    engine_up,
    finish,
    graph,
    logs,
    output_of,
    shop,
    wait,
)

#: 五句都挂了依据的结论，按文档先后反查：华东居首（被削弱）、华南超过华东（预埋的错误，被推翻）、其余三句经受住
FIVE = ("华东销售额 [[v:Q1.r0.销售额]]，居各区域之首。[[see:Q1]]华南销售额超过华东。[[see:Q1]]"
        "华北销售额垫底。[[see:Q1]]华东领先华北。[[see:Q1]]华南高于华北。[[see:Q1]]")
FIVE_PLAYS = {
    "居各区域之首": Play([REFUNDS], "weakened", "华东含 [[v:Q1.r0.退款笔数]] 笔退款，扣除后仍居首，但销售额被高估。"),
    "华南销售额超过华东": Play([NET], "refuted",
                              "扣除退款后华南为 [[v:Q1.r1.净销售额]]，仍低于华东的 [[v:Q1.r0.净销售额]]。[[see:Q1]]"),
    "华北销售额垫底": Play([NET], "stands", "华北净销售额为 [[v:Q1.r2.净销售额]]，确实最低。"),
    "华东领先华北": Play([NET], "stands", "没有找到反例。"),
    "华南高于华北": Play([NET], "stands", "没有找到反例。"),
}
ORDER = list(FIVE_PLAYS)
#: 截止时刻将至、不再开始的句子：每句的理由，时间线上的原因（默认 600 秒的一段，剩下 3 句）
LATE_CLAIM = "本次运行即将达到 600 秒的时长上限，为保证运行按时完成，这句未反查"
LATE_REST = "本次运行即将达到 600 秒的时长上限，为保证运行按时完成，剩下 3 句未审"


class Ticker:
    """假的钟：allot() 看到的「现在」= 真的事件循环时间 + offset。包住节点调用的核查 agent：每派出一项先把钟往前拨
    step 秒（像这一项真花了这么久），再照常执行，并记下这一项拿到的时长上限。hit：拿到的时长上限满足它时，把结果改成
    触了时长上限（像收紧过的时长真到了点），用来看收紧过的触顶怎么说。"""

    def __init__(self, monkeypatch, module, *, step: float, start: float = 0.0,
                 hit: Callable[[float], bool] | None = None) -> None:
        self.offset = start
        self.step = step
        self.timeouts: list[float | None] = []
        real = module.run_verify_agent
        monkeypatch.setattr(deadline, "_now", lambda: asyncio.get_running_loop().time() + self.offset)

        async def spy(**kwargs):
            self.timeouts.append(kwargs.get("timeout_s"))
            self.offset += self.step
            outcome = await real(**kwargs)
            if hit is not None and kwargs.get("timeout_s") is not None and hit(kwargs["timeout_s"]):
                outcome.limit = "timeout_s"
            return outcome

        monkeypatch.setattr(module, "run_verify_agent", spy)


# --------------------------------------------------------------------------
# 反方审稿
# --------------------------------------------------------------------------


async def test_five_claims_with_time_for_two_review_two(monkeypatch, shop):
    """截止时刻只够审 2 句（每句像花了 250 秒）：审 2 句，其余 3 句不再派 agent、记为未能反查；运行成功，被推翻的
    那句照样让出具降档；时间线和产出的 unreviewed 都是 3。"""
    models = Models(monkeypatch, shop, writer=(FIVE,), plays=FIVE_PLAYS)
    ticker = Ticker(monkeypatch, challenge_mod, step=250)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ORDER[:2], "第 3 句开始前只剩约 100 秒：留出 60 秒余量和 30 秒收尾宽限，不够再审一句"
    assert ticker.timeouts == [90, 90], "时间充裕的两句照传每句时长上限，不收紧"

    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 0, "weakened": 1, "refuted": 1, "inconclusive": 3}
    assert out["unreviewed"] == 3, "没来得及审的 3 句是未能反查，不是查过了判断不了"
    assert row.output["_issuance"]["tier"] == "degraded", "审到的那句被推翻：照样降档"

    record = artifact_store.load(out["challenge_artifact"])
    assert [c["verdict"] for c in record["claims"]] == ["weakened", "refuted", "inconclusive", "inconclusive",
                                                        "inconclusive"]
    for claim in record["claims"][2:]:
        assert claim["rationale"] == LATE_CLAIM and claim["queries"] == [] and claim["rationale_doc"] is None
        assert claim["usage"]["calls"] == 0, "没派 agent：一分钱都没花"

    [warn] = await logs(row.id, "challenge_unreviewed")
    assert warn["claims_skipped"] == 3 and warn["reason"] == LATE_REST
    assert warn["message"] == f"反方审稿有 3 句结论未能反查，已记为无法判断：{LATE_REST}"
    assert await logs(row.id, "challenge_limit") == [], "没有哪一句触了自己的上限"
    [done] = await logs(row.id, "challenge_done")
    assert done["counts"] == out["counts"]
    assert "其中 3 句未能反查" in done["message"]
    assert (await verify_manifest(row.id))["ok"] is True


async def test_a_claim_squeezed_by_the_deadline_says_so(monkeypatch, shop):
    """第 2 句开始前只剩约 160 秒：留出余量和收尾宽限，这句的时长收紧到 70 秒以内；它到点按查到的结果收尾时，时间线
    说的是运行时长上限将至、时长已收紧，不说「每句时长上限 90 秒」（那会叫人去调大每句的上限，调大了只会更快撞线）。"""
    models = Models(monkeypatch, shop, writer=(FIVE,), plays=FIVE_PLAYS)
    ticker = Ticker(monkeypatch, challenge_mod, step=440, hit=lambda timeout: timeout < 90)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert models.reviewed() == ORDER[:2]
    first, second = ticker.timeouts
    assert first == 90 and 60 < second < 70, ticker.timeouts

    [limit] = await logs(row.id, "challenge_limit")
    assert limit["limit"] == "timeout_s" and limit["claims_skipped"] == 0
    assert "本次运行即将达到 600 秒的时长上限" in limit["message"] and "收紧" in limit["message"]
    assert "每句时长上限" not in limit["message"]
    out = await output_of(row.id, "chal")
    assert out["counts"]["refuted"] == 1, "收紧后按查到的结果收尾：反方给的结论照常采信"
    assert out["unreviewed"] == 3
    [warn] = await logs(row.id, "challenge_unreviewed")
    assert warn["claims_skipped"] == 3


async def test_no_time_at_all_reviews_nothing_but_finishes(monkeypatch, shop):
    """一开始就只剩 50 秒：一句都不派，5 句都记为未能反查，节点照常收尾、运行成功；没有审到的句子，不降档。"""
    models = Models(monkeypatch, shop, writer=(FIVE,), plays=FIVE_PLAYS)
    ticker = Ticker(monkeypatch, challenge_mod, step=0, start=550)
    row = await finish(graph(shop))
    assert row.status == "succeeded", row.error
    assert models.calls == [] and ticker.timeouts == []
    out = await output_of(row.id, "chal")
    assert out["counts"] == {"stands": 0, "weakened": 0, "refuted": 0, "inconclusive": 5} and out["unreviewed"] == 5
    [warn] = await logs(row.id, "challenge_unreviewed")
    assert warn["claims_skipped"] == 5 and "剩下 5 句未审" in warn["reason"]
    assert row.output["_issuance"]["tier"] == "formal"


async def test_continuing_takes_the_review_from_the_checkpoint(monkeypatch, shop):
    """审了 2 句、3 句没来得及之后，节点落盘时出错；接着运行时这一段有了新的截止时刻，审稿照样取 checkpoint 里的：
    不再派 agent，没来得及的 3 句照旧记为未能反查。重放确定，不看接着运行时还剩多少时间。"""
    models = Models(monkeypatch, shop, writer=(FIVE,), plays=FIVE_PLAYS)
    ticker = Ticker(monkeypatch, challenge_mod, step=250)
    real = challenge_mod._finish
    broken = {"on": True}

    def flaky(*args, **kwargs):
        if broken["on"]:
            raise RuntimeError("落盘时断了")
        return real(*args, **kwargs)

    monkeypatch.setattr(challenge_mod, "_finish", flaky)
    run = await run_manager.start(graph=graph(shop), input_payload={}, run_class="formal")
    row = await wait(run.id)
    assert row.status == "failed" and "落盘时断了" in (row.error or ""), row.error
    calls, timeouts = len(models.calls), list(ticker.timeouts)
    assert models.reviewed() == ORDER[:2]

    broken["on"] = False
    ticker.offset = 0          # 新的一段：时间又充裕了，重审的话 5 句都会派出去
    await run_manager.continue_failed(run.id)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    assert len(models.calls) == calls and ticker.timeouts == timeouts, "接着运行时又派了 agent：审稿没取 checkpoint"
    out = await output_of(row.id, "chal")
    assert out["unreviewed"] == 3 and out["counts"]["inconclusive"] == 3
    assert len(await logs(row.id, "challenge_done")) == 1 and len(await logs(row.id, "challenge_unreviewed")) == 1


async def test_a_slow_review_no_longer_aborts_the_run(monkeypatch, shop):
    """真的钟：这一段只有 7 秒，每句反查都要查满 1.5 秒的时长上限（模型每次调用 0.1 秒，查不完的查询、不限次数），5 句串行
    要 8 秒多——以前整次运行在审到第 5 句时被中止。现在审到时间不够就停下，剩下的记为未能反查，运行成功。
    几句来得及审随机器快慢变，只断言不变的：至少审了 1 句、至少 1 句没来得及，没来得及的都写明了原因、计数对得上。"""
    monkeypatch.setattr(settings, "max_run_seconds", 7)
    monkeypatch.setattr(deadline, "RESERVE_S", 2.0)
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 0.5)
    endless = [f"SELECT COUNT(*) AS n FROM sales WHERE id > {i}" for i in range(60)]
    plays = {key: Play(endless, "stands", "查到的 [[v:Q1.r0.n]] 行里没有反例。") for key in ORDER}
    models = Models(monkeypatch, shop, writer=(FIVE,), plays=plays, latency=0.1)
    row = await finish(graph(shop, timeout_s=1.5, max_queries=100))
    assert row.status == "succeeded", row.error
    assert "时长上限，已中止" not in (row.error or "")

    out = await output_of(row.id, "chal")
    record = artifact_store.load(out["challenge_artifact"])
    late = [c for c in record["claims"] if "即将达到" in c["rationale"] and not c["queries"]]
    reviewed = [c for c in record["claims"] if c["queries"]]
    assert reviewed and late, [c["rationale"] for c in record["claims"]]
    assert len(reviewed) + len(late) == 5
    assert models.reviewed() == ORDER[:len(reviewed)], "按顺序审，停下之后一句都不再派"
    assert out["unreviewed"] == len(late)
    [warn] = await logs(row.id, "challenge_unreviewed")
    assert warn["claims_skipped"] == len(late) and f"剩下 {len(late)} 句未审" in warn["reason"]
    assert "本次运行即将达到 7 秒的时长上限" in warn["reason"]


# --------------------------------------------------------------------------
# 双盲复算
# --------------------------------------------------------------------------

#: 一条查询算出五个指标（按支付日、排除退款）：销售额 330.0、订单数 3、付款客户数 2、最大单笔 200.0、最小单笔 30.0
FIVE_SQL = (f"SELECT SUM(amount) AS gmv, COUNT(*) AS orders, COUNT(DISTINCT customer_id) AS buyers, "
            f"MAX(amount) AS top, MIN(amount) AS low FROM orders WHERE status <> 'refunded' AND paid_date {WEEK}")
NAMES = {"gmv": "销售额", "orders": "订单数", "buyers": "付款客户数", "top": "最大单笔", "low": "最小单笔"}
AGGS = {"gmv": "SUM(amount)", "orders": "COUNT(*)", "buyers": "COUNT(DISTINCT customer_id)", "top": "MAX(amount)",
        "low": "MIN(amount)"}
METRICS = [{"id": mid, "name": name, "expression": f"cell(nodes.fetch, 0, '{mid}')"} for mid, name in NAMES.items()]
#: 盲算各自写一条只算这一个数的 SQL：和原流程的结果一致
PLANS = {name: (f"SELECT {AGGS[mid]} AS blind_v FROM orders WHERE status <> 'refunded' AND paid_date {WEEK}", "blind_v")
         for mid, name in NAMES.items()}
#: 没来得及开始的盲算（默认 600 秒的一段）：指标的原因、出具缺口
LATE_BLIND = "本次运行即将达到 600 秒的时长上限，为保证运行按时完成，未进行盲算"


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def dual_graph(source: str, **dual) -> dict:
    """取数 → 口径卡（五个指标）→ 双盲复算 → 成果。成果的叙述里没有数字：没有缺口时是完整出具。"""
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool=f"db_query__{source}", args={"sql": FIVE_SQL}),
        node("card", "metrics", caliber="销售口径", caliber_version="v1", metrics=METRICS),
        node("dual", "dual_check", "复核五个指标", **dual),
        node("out", "output", fields=[{"name": "结论", "value": "本周指标见口径卡"}],
             contract={"metrics_from": "card", "narrative": "本周指标见口径卡"}),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


@pytest.fixture
async def orders(tmp_path):
    name, source_id = await add_source(build(tmp_path / "orders.db"))
    yield name
    await drop_source(source_id)


async def dual_run(spec: dict, **kwargs) -> Run:
    run = await run_manager.start(graph=spec, input_payload={}, run_class="formal", **kwargs)
    return await wait(run.id, ("succeeded", "failed", "interrupted"))


def unchecked_reasons(out: dict) -> dict[str, str]:
    return {m["metric_id"]: m["reason"] for m in out["metrics"] if m["status"] == "unchecked"}


async def test_five_metrics_with_time_for_two_check_two(monkeypatch, orders):
    """截止时刻只够盲算 2 项（每项像花了 260 秒）：前 2 项照常盲算、一致，后 3 项不再开始，按无法复算记缺口；运行成功、
    出具降档。时间线上 dual_check_unchecked 点名这 3 项和原因，不发提醒调高上限的 dual_check_limit。"""
    blind = Blind(monkeypatch, orders, PLANS)
    ticker = Ticker(monkeypatch, verify_agent, step=260)
    row = await dual_run(dual_graph(orders))
    assert row.status == "succeeded", row.error
    assert blind.runs == 2 and ticker.timeouts == [120, 120], "时间充裕的两项照传每项时长上限，不收紧"

    out = await output_of(row.id, "dual")
    assert out["status"] == "partial" and out["counts"] == {"agreed": 2, "disagreed": 0, "unchecked": 3}
    assert [m["status"] for m in out["metrics"]] == ["agreed", "agreed", "unchecked", "unchecked", "unchecked"]
    assert unchecked_reasons(out) == {"buyers": LATE_BLIND, "top": LATE_BLIND, "low": LATE_BLIND}
    gaps = [f"指标「{NAMES[m]}」无法复算：{LATE_BLIND}" for m in ("buyers", "top", "low")]
    assert out["issuance_gaps"] == gaps
    assert row.output["_issuance"]["tier"] == "degraded"
    assert row.output["_issuance"]["gaps"] == [f"双盲复算「复核五个指标」：{gap}" for gap in gaps]

    [unchecked] = await logs(row.id, "dual_check_unchecked")
    assert unchecked["metrics"] == [{"id": m, "name": NAMES[m], "reason": LATE_BLIND} for m in ("buyers", "top", "low")]
    assert await logs(row.id, "dual_check_limit") == []
    assert (await verify_manifest(row.id))["ok"] is True


async def test_no_time_for_even_the_first_blind_check(monkeypatch, orders):
    """一开始就只剩 40 秒：第一项都不开始（后面的也就不再一个个去试），五项都按无法复算记，节点状态是一个都没核成。"""
    blind = Blind(monkeypatch, orders, PLANS)
    ticker = Ticker(monkeypatch, verify_agent, step=0, start=560)
    row = await dual_run(dual_graph(orders))
    assert row.status == "succeeded", row.error
    assert blind.calls == [] and ticker.timeouts == []
    out = await output_of(row.id, "dual")
    assert out["status"] == "unchecked" and out["counts"] == {"agreed": 0, "disagreed": 0, "unchecked": 5}
    assert set(unchecked_reasons(out).values()) == {LATE_BLIND}
    assert len(out["issuance_gaps"]) == 5


async def test_a_blind_check_squeezed_by_the_deadline_says_so(monkeypatch, orders):
    """第 2 项开始前只剩约 150 秒：时长收紧到 60 秒以内；它到点时按无法复算记，原因说运行时长上限将至、时长已收紧，
    不说「已达每项时长上限（120 秒）」，也不发叫人调大上限的 dual_check_limit。"""
    blind = Blind(monkeypatch, orders, PLANS)
    ticker = Ticker(monkeypatch, verify_agent, step=450, hit=lambda timeout: timeout < 120)
    row = await dual_run(dual_graph(orders))
    assert row.status == "succeeded", row.error
    assert blind.runs == 2
    first, second = ticker.timeouts
    assert first == 120 and 50 < second < 60, ticker.timeouts
    out = await output_of(row.id, "dual")
    reasons = unchecked_reasons(out)
    assert set(reasons) == {"orders", "buyers", "top", "low"}
    assert "本次运行即将达到 600 秒的时长上限" in reasons["orders"] and "收紧" in reasons["orders"]
    assert "每项时长上限" not in reasons["orders"]
    assert {reasons[m] for m in ("buyers", "top", "low")} == {LATE_BLIND}
    assert await logs(row.id, "dual_check_limit") == []


async def test_resuming_after_the_review_replays_the_blind_checks(monkeypatch, orders):
    """销售额两路不一致、挂起等审核人裁决；后 3 项没来得及。审批恢复后节点从头重放：盲算取 checkpoint 里的，不再执行，
    没来得及的 3 项照旧——不看恢复后这一段还剩多少时间。"""
    plans = dict(PLANS)
    plans["销售额"] = (f"SELECT SUM(amount) AS blind_v FROM orders WHERE paid_date {WEEK}", "blind_v")   # 没排除退款：380.0
    blind = Blind(monkeypatch, orders, plans)
    ticker = Ticker(monkeypatch, verify_agent, step=260)
    row = await dual_run(dual_graph(orders))
    assert row.status == "interrupted", row.error
    calls, timeouts = list(blind.calls), list(ticker.timeouts)
    assert blind.runs == 2

    ticker.offset = 0          # 恢复后的一段：时间又充裕了，重做的话五项都会盲算
    async with SessionLocal() as session:
        approval = (await session.execute(select(Approval).where(
            Approval.run_id == row.id, Approval.status == "pending"))).scalar_one()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(f"/api/approvals/{approval.id}/decide",
                                json={"approved": True, "value": {"choices": {"gmv": "a"}, "note": ""}})
    assert res.status_code == 200, res.text
    row = await wait(row.id)
    assert row.status == "succeeded", row.error
    assert blind.calls == calls and ticker.timeouts == timeouts, "恢复后盲算又执行了：没取 checkpoint"
    out = await output_of(row.id, "dual")
    assert out["status"] == "resolved" and out["choices"] == {"gmv": "a"}
    assert [m["status"] for m in out["metrics"]] == ["disagreed", "agreed", "unchecked", "unchecked", "unchecked"]
    assert set(unchecked_reasons(out).values()) == {LATE_BLIND}
    assert len(await logs(row.id, "dual_check_unchecked")) == 1, "盲算那一步的日志只发一次"


async def test_slow_blind_checks_no_longer_abort_the_run(monkeypatch, orders):
    """真的钟：这一段只有 5 秒，五项盲算一项接一项（并行数 1），每项约 1 秒（模型每次调用 0.3 秒，每项 3 次），合起来
    超过 5 秒——以前整次运行在盲算到一半时被中止。现在时间不够就不再开始、开始的按剩余时间收紧，没来得及的按无法复算
    记，运行成功。每项时长上限 3 秒，一项自己用不完：无法复算的只会是因为运行时长上限将至。几项来得及随机器快慢变，
    只断言不变的。"""
    monkeypatch.setattr(settings, "max_run_seconds", 5)
    monkeypatch.setattr(deadline, "RESERVE_S", 1.2)
    monkeypatch.setattr(verify_agent, "SETTLE_GRACE_S", 0.4)
    monkeypatch.setattr(dual_check_mod, "BLIND_CONCURRENCY", 1)

    async def slow_model(session, spec):
        return MockChatModel(model_name="slow-verify", latency=0.3), "slow-verify"

    monkeypatch.setattr(verify_agent, "get_chat_model", slow_model)
    Blind(monkeypatch, orders, PLANS)
    row = await dual_run(dual_graph(orders), options={"verify": {"dual_check": {"timeout_s": 3}}})
    assert row.status == "succeeded", row.error
    assert "时长上限，已中止" not in (row.error or "")

    out = await output_of(row.id, "dual")
    reasons = unchecked_reasons(out)
    assert out["counts"]["agreed"] >= 1 and reasons, out["metrics"]
    assert out["counts"]["agreed"] + len(reasons) == 5 and out["counts"]["disagreed"] == 0
    assert all("本次运行即将达到 5 秒的时长上限" in why for why in reasons.values()), reasons
    assert [m["status"] for m in out["metrics"]][:out["counts"]["agreed"]] == ["agreed"] * out["counts"]["agreed"], \
        "一项接一项：来得及的在前"
    [unchecked] = await logs(row.id, "dual_check_unchecked")
    assert {m["id"] for m in unchecked["metrics"]} == set(reasons)
