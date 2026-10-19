"""评测执行（app/eval/runner.py；细案 01 做法 9，评测-D9）。

作答这一步多半换成桩（Driver.drive）：这里守的是后台任务本身——排队、并发上限（不超过 max_parallel，也不超过全局运行
并发的四分之一）、停止、只重试没做完的、重启标中断不续跑、单次和每日金额上限（评测单独一本账，不占裁判、核查的额度）、
连续几题助手调用失败止损、每题超时会取消正在跑的运行；停在审批上的作答走真的运行（评测不代批，取消掉）。
题目多半是「期望反问」的题：没有期望来源，不用跑期望探针。问法、题集名都是虚构的。
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal, utcnow
from app.db.models import Run, Setting
from app.db.models_eval import EvalResult, EvalRun
from app.engine.runner import run_manager
from app.eval import answer as answer_mod
from app.eval import runner
from app.main import app
from tests import eval_samples as es
from tests.fixtures.sources import drop_source


async def _clear_ledger() -> None:
    async with SessionLocal() as session:
        row = await session.get(Setting, "eval_spend")
        if row is not None:
            await session.delete(row)
            await session.commit()


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    await _clear_ledger()
    yield
    await _clear_ledger()
    await runner.manager.shutdown()
    await es.drop_suites()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def ask_cases(n: int) -> list[dict]:
    return [{"question": f"第 {i} 个含糊的问题", "expectations": [{"kind": "ask"}]} for i in range(1, n + 1)]


def stub(monkeypatch, *, delay: float = 0.0, path: str = "asked", cost: float = 0.0, copilot_failed: bool = False,
         current_run: str | None = None, live: list | None = None):
    """作答换成桩：等 delay 秒，交回 path；cost 记在助手的用量上。live 记下同时在答的有几题。"""
    peak = {"now": 0, "max": 0}

    async def drive(self):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        try:
            self.turn.current_run = current_run
            await self._phase("build")
            await asyncio.sleep(delay)
            self.turn.path = path
            self.turn.copilot_failed = copilot_failed
            if copilot_failed:
                self.turn.reason = "助手本轮未完成：接入返回了 502"
            self.turn.usage["assistant"] = {"by_model": {}, "input_tokens": 0, "output_tokens": 0, "calls": 1,
                                            "cost_usd": cost, "unpriced_calls": 0}
            return self.turn
        finally:
            peak["now"] -= 1

    monkeypatch.setattr(answer_mod.Driver, "drive", drive)
    return peak


async def start(suite_id: str, variant: dict | None = None, **kw) -> list[EvalRun]:
    from app.db.models_eval import EvalSuite
    from app.eval.variants import normalize_variant

    async with SessionLocal() as session:
        suite = await session.get(EvalSuite, suite_id)
        return await runner.start(session, suite, [normalize_variant(variant or {})], actor="评测测试", **kw)


async def results(run_id: str) -> list[EvalResult]:
    async with SessionLocal() as session:
        return list((await session.execute(select(EvalResult).where(EvalResult.eval_run_id == run_id)
                                           .order_by(EvalResult.seq))).scalars())


async def get(run_id: str) -> EvalRun:
    async with SessionLocal() as session:
        return await session.get(EvalRun, run_id)


async def test_items_run_in_the_background_within_the_parallel_cap(monkeypatch):
    peak = stub(monkeypatch, delay=0.15)
    suite_id = await es.make_suite("none", cases=ask_cases(5), budget={"max_parallel": 3})
    [run] = await start(suite_id)
    assert run.status == "queued" and run.counts["total"] == 5
    await es.wait_eval(run.id)
    row = await get(run.id)
    assert row.status == "succeeded" and row.counts["done"] == 5 and row.counts["passed"] == 5
    assert peak["max"] == 3, "同时最多 max_parallel 题"
    assert all(r.status == "ok" and r.verdict == "pass" and r.current is None for r in await results(run.id))
    assert row.summary["accuracy"]["passed"] == 5 and row.summary["verdicts"]["pass"] == 5


async def test_the_cap_never_exceeds_a_quarter_of_the_global_concurrency(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_concurrent_runs", 4)
    peak = stub(monkeypatch, delay=0.1)
    suite_id = await es.make_suite("none", cases=ask_cases(3), budget={"max_parallel": 3})
    [run] = await start(suite_id)
    await es.wait_eval(run.id)
    assert peak["max"] == 1, "全局并发 4 的四分之一是 1"


async def test_a_second_evaluation_of_the_same_suite_waits(monkeypatch):
    stub(monkeypatch, delay=0.3)
    suite_id = await es.make_suite("none", cases=ask_cases(1))
    [run] = await start(suite_id)
    with pytest.raises(runner.EvalRefused) as refused:
        await start(suite_id)
    assert refused.value.code == runner.EVAL_BUSY and refused.value.status == 409
    await es.wait_eval(run.id)


async def test_stop_then_retry_only_the_unfinished(monkeypatch):
    stub(monkeypatch, delay=0.05)
    suite_id = await es.make_suite("none", cases=ask_cases(4), budget={"max_parallel": 1})
    [run] = await start(suite_id)
    # 等第一题评完再停
    for _ in range(100):
        if (await get(run.id)).counts.get("done", 0) >= 1:
            break
        await asyncio.sleep(0.02)
    stub(monkeypatch, delay=5.0)
    await asyncio.sleep(0.1)
    assert await runner.stop(run.id)
    row = await get(run.id)
    # 停止是「停下」不是「出错」：原因记在 stop_reason；没跑的题单独计 not_run，不算进 errors，done 不再等于 total
    assert row.status == "cancelled" and row.stop_reason == runner.STOPPED and row.error is None
    before = {r.id: (r.status, r.finished_at) for r in await results(run.id)}
    done = [rid for rid, (status, _) in before.items() if status == "ok"]
    assert done and any(status == "cancelled" for status, _ in before.values())
    assert row.counts["errors"] == 0 and row.counts["done"] == len(done)
    assert row.counts["not_run"] == 4 - len(done) and row.counts["done"] + row.counts["not_run"] == row.counts["total"]
    assert row.summary["not_run"] == 4 - len(done) and row.summary["errors"] == 0
    assert not await runner.stop(run.id), "已经结束的停不了"

    stub(monkeypatch, delay=0.01)
    await runner.retry(run.id)
    await es.wait_eval(run.id)
    after = {r.id: (r.status, r.finished_at) for r in await results(run.id)}
    assert all(status == "ok" for status, _ in after.values())
    assert all(after[rid] == before[rid] for rid in done), "评完了的不重做"
    assert (await get(run.id)).status == "succeeded"
    with pytest.raises(runner.EvalRefused) as nothing:
        await runner.retry(run.id)
    assert nothing.value.code == runner.NOTHING_TO_RETRY


async def test_a_restart_marks_unfinished_evaluations_interrupted_and_does_not_resume():
    suite_id = await es.make_suite("none", cases=ask_cases(1))
    async with SessionLocal() as session:
        row = EvalRun(suite_id=suite_id, status="running", started_at=utcnow())
        session.add(row)
        await session.flush()
        session.add(EvalResult(eval_run_id=row.id, status="running"))
        await session.commit()
        run_id = row.id
    assert await runner.startup() >= 1
    row = await get(run_id)
    assert row.status == "interrupted" and row.stop_reason == runner.INTERRUPTED and row.error is None
    assert row.counts["not_run"] == 1 and row.counts["running"] == 0 and row.counts["errors"] == 0, row.counts
    [item] = await results(run_id)
    assert item.status == "cancelled"
    assert not runner.manager.running(run_id), "不自动续跑"


async def test_stopping_a_queued_evaluation_counts_everything_as_not_run(monkeypatch):
    """排队中就停掉（还没开始）：每一题都没跑，stop_reason 写停止，计数跟着重算（以前还是建的时候那份：pending 照旧）。"""
    stub(monkeypatch)
    suite_id = await es.make_suite("none", cases=ask_cases(2))
    monkeypatch.setattr(runner.manager, "start", lambda *_a, **_k: None)  # 不让它开跑：停在排队
    [run] = await start(suite_id)
    assert await runner.stop(run.id)
    row = await get(run.id)
    assert row.status == "cancelled" and row.stop_reason == runner.STOPPED
    assert (row.counts["not_run"], row.counts["pending"], row.counts["done"]) == (2, 0, 0), row.counts


async def test_the_per_run_cap_stops_new_items(monkeypatch):
    stub(monkeypatch, cost=1.0)
    suite_id = await es.make_suite("none", cases=ask_cases(3), budget={"max_parallel": 1, "per_run_max_usd": 1.5})
    [run] = await start(suite_id)
    await es.wait_eval(run.id)
    row = await get(run.id)
    items = await results(run.id)
    assert [r.status for r in items] == ["ok", "ok", "skipped"]
    assert row.status == "partial" and row.stop_reason == runner.LIMIT_RUN.format(limit=1.5)
    assert (row.counts["done"], row.counts["not_run"], row.counts["errors"], row.counts["skipped"]) == (2, 1, 0, 0), \
        "到了上限没开评的是没跑，不是跳过（跳过是评了、但期望算不出、不评分）"
    assert "金额上限" in items[2].error and row.summary["cost_usd"] == 2.0


async def test_the_daily_cap_is_its_own_ledger(monkeypatch):
    from app.engine import judge
    from app.engine.spend import _today, eval_spend, verify_spend

    stub(monkeypatch, cost=0.4)
    async with SessionLocal() as session:
        for key in ("eval_spend", "judge_spend", "verify_spend"):
            row = await session.get(Setting, key)
            if row is not None:
                await session.delete(row)
        session.add(Setting(key="eval_spend", value={"date": _today(), "usd": 4.9, "calls": 3, "unpriced_calls": 0}))
        await session.commit()
    judge_before, verify_before = await judge.judge_spend.daily(), await verify_spend.daily()
    suite_id = await es.make_suite("none", cases=ask_cases(3), budget={"max_parallel": 1})
    [first] = await start(suite_id)
    await es.wait_eval(first.id)
    # 第一次没有可估的每题花费：按实际累计，越过每日上限之后不再开新题
    spent = await eval_spend.daily()
    assert spent["usd"] > 5.0 - 1e-9 and spent["calls"] == 3 + 1, spent
    row = await get(first.id)
    assert [r.status for r in await results(first.id)] == ["ok", "skipped", "skipped"]
    assert row.stop_reason == runner.LIMIT_DAILY.format(limit=5.0)
    assert await judge.judge_spend.daily() == judge_before and await verify_spend.daily() == verify_before, \
        "评测不占裁判、核查的额度"


async def test_consecutive_assistant_failures_stop_the_evaluation(monkeypatch):
    stub(monkeypatch, path="build_failed", copilot_failed=True)
    suite_id = await es.make_suite("none", cases=ask_cases(5), budget={"max_parallel": 1})
    [run] = await start(suite_id)
    await es.wait_eval(run.id)
    row = await get(run.id)
    statuses = [r.status for r in await results(run.id)]
    assert statuses[:3] == ["ok", "ok", "ok"] and statuses[3:] == ["skipped", "skipped"]
    assert row.stop_reason.startswith("连续 3 题助手调用失败") and "502" in row.stop_reason


async def test_too_many_timeouts_stop_the_evaluation(monkeypatch):
    async def drive(self):
        self.turn.path, self.turn.timed_out, self.turn.reason = "build_failed", True, "超过每题时限"
        return self.turn

    monkeypatch.setattr(answer_mod.Driver, "drive", drive)
    suite_id = await es.make_suite("none", cases=ask_cases(9), budget={"max_parallel": 1})
    [run] = await start(suite_id)
    await es.wait_eval(run.id)
    row = await get(run.id)
    statuses = [r.status for r in await results(run.id)]
    assert statuses == ["ok"] * runner.STOP_TIMEOUTS + ["skipped"] * (9 - runner.STOP_TIMEOUTS)
    assert row.stop_reason == runner.STOP_TIMEOUT_TEXT.format(window=runner.STOP_WINDOW, n=runner.STOP_TIMEOUTS)
    assert row.summary["not_run"] == 9 - runner.STOP_TIMEOUTS


async def test_a_case_over_its_time_limit_stops_its_run(monkeypatch):
    stopped: list[str | None] = []

    async def fake_stop(run_id, actor=None):
        stopped.append(run_id)

    monkeypatch.setattr(answer_mod, "stop_run", fake_stop)
    stub(monkeypatch, delay=5.0, current_run="run-in-flight")
    original = runner.EvalExecution._prepare

    async def prepare(self):
        units = await original(self)
        self.budget["per_case_timeout_s"] = 0.2
        return units

    monkeypatch.setattr(runner.EvalExecution, "_prepare", prepare)
    suite_id = await es.make_suite("none", cases=ask_cases(1))
    [run] = await start(suite_id)
    await es.wait_eval(run.id)
    [item] = await results(run.id)
    assert stopped == ["run-in-flight"], "超时取消正在跑的那次运行"
    assert item.status == "ok" and item.path == "build_failed" and "每题时限" in item.error


async def test_time_queued_for_a_run_slot_does_not_count_against_the_case_limit(monkeypatch):
    """复核：满载时作答运行排队等名额的那一段不算每题时限（排了 0.8 秒、时限 0.3 秒，拿到名额后很快答完：不记超时）。"""
    from app.cluster import slots

    stored = slots.values({"max_total": 2, "interactive_reserve": 1})

    async def fixed(*, fresh: bool = False):  # noqa: ARG001
        return stored

    monkeypatch.setattr(slots, "policy", fixed)
    slots.forget_policy()
    gate = asyncio.Event()

    async def blocker() -> None:
        async with slots.hold("eval-slot-blocker", "eval"):
            await gate.wait()

    async def drive(self):
        self.turn.current_run = "eval-answer-in-queue"
        await self._phase("build")
        async with slots.hold(self.turn.current_run, "eval"):
            await asyncio.sleep(0.05)
        self.turn.path = "asked"
        self.turn.usage["assistant"] = {"by_model": {}, "input_tokens": 0, "output_tokens": 0, "calls": 1,
                                        "cost_usd": 0.0, "unpriced_calls": 0}
        return self.turn

    monkeypatch.setattr(answer_mod.Driver, "drive", drive)
    original = runner.EvalExecution._prepare

    async def prepare(self):
        units = await original(self)
        self.budget["per_case_timeout_s"] = 0.3
        return units

    monkeypatch.setattr(runner.EvalExecution, "_prepare", prepare)
    held = asyncio.create_task(blocker())
    try:
        while not (await slots.snapshot())["classes"]["system"]["running"]:
            await asyncio.sleep(0.02)
        suite_id = await es.make_suite("none", cases=ask_cases(1))
        [run] = await start(suite_id)
        while not await slots.waiting("eval-answer-in-queue"):
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.8)
        gate.set()
        await es.wait_eval(run.id)
    finally:
        gate.set()
        await held
    [item] = await results(run.id)
    assert item.status == "ok" and item.path == "asked" and not item.error, item.error


async def test_within_does_not_count_the_paused_stretches():
    paused = {"now": True}

    async def is_paused() -> bool:
        return paused["now"]

    async def work() -> str:
        await asyncio.sleep(0.5)
        paused["now"] = False
        await asyncio.sleep(0.05)
        return "done"

    assert await runner.within(work(), 0.2, is_paused, tick=0.02) == "done", "暂停的那 0.5 秒不算"

    async def never():
        await asyncio.sleep(10)

    async def awake() -> bool:
        return False

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(asyncio.TimeoutError):
        await runner.within(never(), 0.2, awake, tick=0.02)
    assert loop.time() - t0 < 1.0


async def test_a_run_stopped_at_an_approval_is_cancelled_not_approved(client, monkeypatch, tmp_path):
    from app.eval.answer import APPROVAL_REASON

    class Approving(es.Copilot):
        async def astream(self, messages):
            if "上线前自查" in messages[1][1]:
                yield es._line({"op": "done"})
                return
            for op in [{"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
                       {"op": "add_node", "node": {"id": "ok", "type": "human", "label": "确认",
                                                   "config": {"mode": "approve", "title": "请确认"}}},
                       {"op": "add_edge", "edge": {"source": "start", "target": "ok"}},
                       {"op": "add_node", "node": {"id": "out", "type": "output", "label": "答案",
                                                   "config": {"fields": [{"name": "答案", "value": "好"}]}}},
                       {"op": "add_edge", "edge": {"source": "ok", "target": "out", "sourceHandle": "approved"}},
                       {"op": "done", "explanation": "确认一下", "run": True}]:
                yield es._line(op)

    name, sid = await es.make_source(tmp_path)
    try:
        es.use_copilot(monkeypatch, Approving(name))
        suite_id = await es.make_suite(sid, cases=[{"question": "要人确认的问题", "expectations": [{"kind": "ask"}]}])
        [run] = await start(suite_id)
        await es.wait_eval(run.id)
        [item] = await results(run.id)
        assert item.path == "run_failed" and "评测不代批" in item.error
        async with SessionLocal() as session:
            answered = await session.get(Run, item.run_id)
        assert answered.status == "cancelled", "评测不代批：取消掉"
        details = (await client.get(f"/api/evals/evaluations/{run.id}/results/{item.id}")).json()
        assert details["details"]["turn"]["reason"] == APPROVAL_REASON
    finally:
        await drop_source(sid)


async def test_the_api_starts_estimates_and_reports_progress(client, monkeypatch):
    stub(monkeypatch, delay=0.05)
    suite_id = await es.make_suite("none", cases=ask_cases(2))
    estimate = (await client.post(f"/api/evals/suites/{suite_id}/evaluations/estimate", json={})).json()
    assert estimate["cases"] == 2 and estimate["estimate"]["total_usd"] is None and estimate["parallel"] == 2
    assert estimate["budget"]["daily_max_usd"] == 5.0 and estimate["arms"][0]["models"]["assistant"] == "mock-fast"
    started = await client.post(f"/api/evals/suites/{suite_id}/evaluations", json={"variant": {"repeats": 2}})
    assert started.status_code == 202, started.text
    [ev] = started.json()["evaluations"]
    assert ev["counts"]["total"] == 4 and ev["variant"]["repeats"] == 2
    await es.wait_eval(ev["id"])
    got = (await client.get(f"/api/evals/evaluations/{ev['id']}")).json()
    assert got["status"] == "succeeded" and got["current"] == [] and got["summary"]["pass_rates"]
    listed = (await client.get(f"/api/evals/suites/{suite_id}/evaluations")).json()["evaluations"]
    assert [e["id"] for e in listed] == [ev["id"]]
    rows = (await client.get(f"/api/evals/evaluations/{ev['id']}/results")).json()["results"]
    assert len(rows) == 4 and rows[0]["question"] == "第 1 个含糊的问题"
    second = (await client.post(f"/api/evals/suites/{suite_id}/evaluations/estimate", json={})).json()
    assert second["estimate"]["basis"] == ev["id"], "第二次按上一次的每题平均花费估"
    bad = await client.post(f"/api/evals/suites/{suite_id}/evaluations", json={"variant": {"catalog": "nope"}})
    assert bad.status_code == 422 and bad.json()["code"] == "eval_variant_invalid"


async def test_the_estimate_says_it_cannot_price_instead_of_zero():
    """上一次评测的模型都不在价格目录里（价格目录空）：估算以前写「约 $0」，像不花钱。现在一分都没估出来的写估不出；估出了
    一部分的写「至少」、另有几次估不出（老总表里每题平均记成 0、却有估不出的调用的，也按估不出算）。"""
    from datetime import timedelta

    suite_id = await es.make_suite("none", cases=ask_cases(1))
    now = utcnow()
    async with SessionLocal() as session:
        session.add(EvalRun(suite_id=suite_id, status="succeeded", created_at=now - timedelta(minutes=2),
                            summary={"per_case_cost_usd": 0.0, "unpriced_calls": 6}))
        await session.commit()
        got = await runner.estimate(session, suite_id, 3)
    assert got["total_usd"] is None and got["per_item_usd"] is None and got["unpriced_calls"] == 6, got
    assert "估不出" in got["note"] and got["basis"], got
    async with SessionLocal() as session:
        session.add(EvalRun(suite_id=suite_id, status="succeeded", created_at=now - timedelta(minutes=1),
                            summary={"per_case_cost_usd": 0.02, "unpriced_calls": 2}))
        await session.commit()
        got = await runner.estimate(session, suite_id, 3)
    assert got["total_usd"] == 0.06 and got["at_least"] is True and got["unpriced_calls"] == 2, got
    assert "至少" in got["note"] and "2 次" in got["note"], got["note"]
    async with SessionLocal() as session:
        session.add(EvalRun(suite_id=suite_id, status="succeeded", created_at=now,
                            summary={"per_case_cost_usd": 0.0, "unpriced_calls": 0}))
        await session.commit()
        got = await runner.estimate(session, suite_id, 3)
    assert got["total_usd"] == 0.0 and not got.get("at_least") and not got.get("unpriced_calls"), "目录价是 0 的才是真不花钱"


def test_the_judge_role_keeps_its_unpriced_calls():
    """按角色的用量里裁判那一份以前只有金额和次数：裁判模型不在价格目录里时写成 $0。"""
    usage = runner.judge_usage({"claims": {"cost_usd": 0.0, "calls": 2, "priced": False},
                                "text": {"cost_usd": 0.0, "calls": 1, "usage": {"unpriced_calls": 1}}}, 0.0)
    assert usage == {"cost_usd": 0.0, "calls": 3, "unpriced_calls": 3}
    priced = runner.judge_usage({"claims": {"cost_usd": 0.01, "calls": 2, "priced": True}}, 0.01)
    assert priced == {"cost_usd": 0.01, "calls": 2, "unpriced_calls": 0}


async def test_results_give_the_question_actually_asked_and_the_template_apart(client, monkeypatch):
    """逐题结果的 question 是这一题实际问的（变量按这次评测的截至时间渲染好了，和作答时 Driver 收到的一字不差），模板原文
    另放 question_template（下一轮验收：以前 question 是带 {{ schedule.last_week_start }} 的原始模板）。没有变量的题两个一样。"""
    from datetime import datetime, timezone

    from app.db.models_eval import EvalCase
    from app.eval.cases import render, variable_values

    asked: list[str] = []
    real_init = answer_mod.Driver.__init__

    def init(self, question, **kw):
        asked.append(question)
        real_init(self, question, **kw)

    monkeypatch.setattr(answer_mod.Driver, "__init__", init)
    stub(monkeypatch)
    template = "{{ schedule.last_week_start }} 到 {{ schedule.last_week_end }} 那周的问题"
    suite_id = await es.make_suite("none", cases=[{"question": template, "expectations": [{"kind": "ask"}]},
                                                  {"question": "没有变量的问题", "expectations": [{"kind": "ask"}]}])
    as_of = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
    [run] = await start(suite_id, as_of=as_of)
    await es.wait_eval(run.id)
    rows = (await client.get(f"/api/evals/evaluations/{run.id}/results")).json()["results"]
    expected = render(template, variable_values(as_of, "Asia/Shanghai", None))
    assert "{{" not in expected and rows[0]["question"] == expected, rows[0]["question"]
    assert rows[0]["question"] in asked, "和作答时实际问的一样"
    assert rows[0]["question_template"] == template
    assert rows[1]["question"] == rows[1]["question_template"] == "没有变量的问题"
    # 题目改过（指纹对不上）：现在的模板不一定是当时问的，没有作答运行可取时不编一个，question 为空、模板照给
    async with SessionLocal() as session:
        case = (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite_id, EvalCase.seq == 1))).scalar_one()
        case.question, case.case_hash = "改过的 {{ schedule.today }}", "changed"
        await session.commit()
    rows = (await client.get(f"/api/evals/evaluations/{run.id}/results")).json()["results"]
    assert rows[0]["question"] is None and rows[0]["question_template"] == "改过的 {{ schedule.today }}"


async def test_eval_settings_group_and_spend_endpoint(client):
    got = (await client.get("/api/settings")).json()["eval"]
    assert got == {"max_parallel": 2, "per_case_timeout_s": 900, "per_run_max_usd": 2.0, "daily_max_usd": 5.0,
                   "keep_runs": 20, "judge": False}
    bad = await client.put("/api/settings", json={"values": {"eval": {"keep_runs": 0}}})
    assert bad.status_code == 422 and "保留" in bad.json()["detail"]
    bad = await client.put("/api/settings", json={"values": {"eval": {"per_run_max_usd": 9.0}}})
    assert bad.status_code == 422, "单次上限不能高于每日上限"
    ok = await client.put("/api/settings", json={"values": {"eval": {"daily_max_usd": 3.0, "judge": True},
                                                            "eval_spend": {"usd": 0}}})
    assert ok.status_code == 200 and ok.json()["eval"]["daily_max_usd"] == 3.0 and "eval_spend" not in ok.json()
    spend = (await client.get("/api/settings/eval/spend")).json()
    assert set(spend) == {"date", "usd", "calls", "unpriced_calls", "daily_max_usd"} and spend["daily_max_usd"] == 3.0
    # 题集只能把每日上限往下收
    from app.eval.suites import effective_budget

    assert effective_budget({"daily_max_usd": 9.0}, {"daily_max_usd": 3.0})["daily_max_usd"] == 3.0
    assert effective_budget({"daily_max_usd": 1.0}, {"daily_max_usd": 3.0})["daily_max_usd"] == 1.0
    await client.put("/api/settings", json={"values": {"eval": {}}})


async def test_a_workflow_case_runs_its_pinned_version_as_an_exploratory_run(client, monkeypatch, tmp_path):
    """工作流题：钉住那一版的图按输入模板跑一次探索运行（来源 eval），不发正式运行、不建图、不复核。"""
    from app.core.artifact_store import graph_hash
    from app.db.models import Workflow, WorkflowVersion
    from tests.fixtures import dual_check_shop as shop

    name, sid = await es.make_source(tmp_path)
    try:
        es.use_report(monkeypatch)

        def node(nid, ntype, **config):
            return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}

        nodes = [node("start", "input", fields=[{"name": "week", "required": True}]),
                 node("q", "tool", tool=f"db_query__{name}", args={"sql": shop.RIGHT}),
                 node("write", "report", instructions="写周销售"),
                 node("out", "output", fields=[{"name": "报告", "value": "{{ nodes.write.text }}"}])]
        graph = {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}
        async with SessionLocal() as session:
            wf = Workflow(name="虚构周销售", graph=graph, status="published", published_version=1)
            session.add(wf)
            await session.flush()
            session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, graph_hash=graph_hash(graph),
                                        level="published"))
            await session.commit()
            wf_id = wf.id
        suite_id = await es.make_suite(sid, cases=[{
            "kind": "workflow", "workflow_id": wf_id, "workflow_version": 1,
            "input_template": {"week": "{{ schedule.last_week }}"},
            "gold": {"kind": "sql", "datasource_id": sid, "sql": shop.RIGHT},
            "expectations": [{"kind": "scalar", "column": "gmv"}]}])
        [run] = await start(suite_id)
        await es.wait_eval(run.id)
        [item] = await results(run.id)
        assert item.path == "ran" and item.verdict == "pass", (item.error, item.scores)
        async with SessionLocal() as session:
            answered = await session.get(Run, item.run_id)
        assert answered.run_class == "exploratory" and answered.origin_kind == "eval" and answered.workflow_id is None
        assert set(answered.input) == {"week"} and answered.input["week"].count("-W") == 1
        # 钉住当基线、取消
        pinned = await client.post(f"/api/evals/evaluations/{run.id}/pin", json={"pinned": True})
        assert pinned.status_code == 200 and pinned.json()["pinned"] is True
        assert (await client.post(f"/api/evals/evaluations/{run.id}/pin", json={"pinned": "yes"})).status_code == 422
        assert (await client.get("/api/evals/pairs/nope")).status_code == 404
    finally:
        await drop_source(sid)


async def test_a_review_asking_for_a_retry_rebuilds_on_the_same_graph(monkeypatch, tmp_path):
    """复核给了 retry_instruction（chat/turn 的规则判过可以重来）：带着这次运行的图、照那句话重来一次，两次运行都记在这一题
    名下、用量都算这一题的；第二次复核不再要重来就收尾。"""
    import app.api.copilot as copilot

    name, sid = await es.make_source(tmp_path)
    try:
        model = es.Copilot(name)
        es.use_copilot(monkeypatch, model)
        es.use_report(monkeypatch)
        reviews: list[int] = []
        real = copilot.review_of_run

        async def review(session, run, *, question, provider=None, model=None, always=False, attempt=0):
            reviews.append(attempt)
            out = await real(session, run, question=question, provider=provider, model=model, always=always,
                             attempt=attempt)
            if attempt == 0:
                out.update(verdict="annotated", severity="broken", retry=True,
                           retry_instruction="这个工作流上一次运行没有成功：步数用满了。改完继续回答原来的问题")
            return out

        monkeypatch.setattr(copilot, "review_of_run", review)
        suite_id = await es.make_suite(sid)
        [run] = await start(suite_id)
        await es.wait_eval(run.id)
        [item] = await results(run.id)
        assert reviews == [0, 1], "第一次复核要重来，第二次（attempt=1）不再要"
        assert item.path == "ran" and item.verdict == "pass", item.error
        retry_call = next(c for c in model.calls if "步数用满了" in str(c[-1][1]) or "步数用满了" in str(c[1][1]))
        assert "这是当前的工作流" in str(retry_call[1][1]), "带着原图交回去改"
        async with SessionLocal() as session:
            answers = (await session.execute(select(Run.id).where(Run.origin_kind == "eval"))).scalars().all()
            mine = [r for r in answers if (await session.get(Run, r)).origin.get("eval_run_id") == run.id
                    and (await session.get(Run, r)).origin.get("role") == "answer"]
        assert len(mine) == 2 and item.run_id in mine
    finally:
        await drop_source(sid)
