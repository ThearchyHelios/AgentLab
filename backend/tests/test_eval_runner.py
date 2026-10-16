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
    assert row.status == "cancelled" and row.error == runner.STOPPED
    before = {r.id: (r.status, r.finished_at) for r in await results(run.id)}
    done = [rid for rid, (status, _) in before.items() if status == "ok"]
    assert done and any(status == "cancelled" for status, _ in before.values())
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
    assert row.status == "interrupted" and row.error == runner.INTERRUPTED
    [item] = await results(run_id)
    assert item.status == "cancelled"
    assert not runner.manager.running(run_id), "不自动续跑"


async def test_the_per_run_cap_stops_new_items(monkeypatch):
    stub(monkeypatch, cost=1.0)
    suite_id = await es.make_suite("none", cases=ask_cases(3), budget={"max_parallel": 1, "per_run_max_usd": 1.5})
    [run] = await start(suite_id)
    await es.wait_eval(run.id)
    row = await get(run.id)
    items = await results(run.id)
    assert [r.status for r in items] == ["ok", "ok", "skipped"]
    assert row.status == "partial" and row.stop_reason == runner.LIMIT_RUN.format(limit=1.5)
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
