"""评测页（第 4 波，细案 01 第 18 项）在 app/api/evals.py 里补的几样：统计、能不能做都由服务端给，界面只显示。

- 题集列表每个题集带最近一次评测、同一变体最近几次评完的准确率（旧的在前，换了变体的不连成一条线）、目标是它的定时任务；
- 评测详情带 actions：只重试没做完的、重新评分、钉住 / 取消基线现在能不能做，和接口拒收的条件同一份；
- 写 SQL 定期望的预览：交回结果的列和前几行；用到遮罩列的照保存时那一道拒收，没过只读守卫的指明是哪一格（都不回 422）；
- 「口径命中与重复度」：判据、窗口趋势（出自 ledger/stats.compute_stats）、成员不少于 3 的问题类（出自 chat/questions），
  评测运行不进问题类。

库、表、题集名、问法都是虚构的。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal, utcnow
from app.db.models import Conversation, ConversationTurn, Run, Schedule
from app.db.models_eval import EvalResult, EvalRun
from app.engine.runner import run_manager
from app.main import app
from tests import eval_samples as es
from tests.fixtures import dual_check_shop as shop
from tests.fixtures.sources import drop_source


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def source(tmp_path):
    name, sid = await shop.add_source(shop.build(tmp_path / "shop.db"))
    yield {"name": name, "id": sid}
    await drop_source(sid)


def _summary(passed: int, n: int) -> dict:
    return {"accuracy": {"passed": passed, "n": n, "rate": round(passed / n, 4) if n else None, "ci": None},
            "traced_accuracy": {"passed": passed, "n": n, "rate": round(passed / n, 4) if n else None, "ci": None}}


async def _evaluation(suite_id: str, *, status: str, key: str, passed: int, n: int, minutes_ago: int,
                      arm: str | None = None, pinned: bool = False, cleaned: bool = False) -> str:
    async with SessionLocal() as session:
        row = EvalRun(suite_id=suite_id, status=status, trigger="manual", variant={}, resolved_models={},
                      fingerprint={"variant_key": key}, summary=_summary(passed, n) if status != "queued" else {},
                      counts={"total": n, "done": n}, arm=arm, pinned=pinned,
                      cleaned_at=utcnow() if cleaned else None)
        session.add(row)
        await session.flush()
        row.created_at = utcnow() - timedelta(minutes=minutes_ago)
        await session.commit()
        return row.id


async def test_the_suite_list_carries_the_latest_run_a_trend_of_the_same_variant_and_its_schedules(client, source):
    suite_id = await es.make_suite(source["id"], name=f"虚构题集_{uuid.uuid4().hex[:6]}")
    old = await _evaluation(suite_id, status="succeeded", key="A", passed=1, n=4, minutes_ago=50)
    other = await _evaluation(suite_id, status="succeeded", key="B", passed=4, n=4, minutes_ago=40)
    failed = await _evaluation(suite_id, status="failed", key="A", passed=0, n=4, minutes_ago=30)
    mid = await _evaluation(suite_id, status="partial", key="A", passed=2, n=4, minutes_ago=20)
    new = await _evaluation(suite_id, status="succeeded", key="A", passed=3, n=4, minutes_ago=10)
    async with SessionLocal() as session:
        sched = Schedule(name="虚构每周回归", workflow_id="", workflow_name="虚构题集", target_kind="eval_suite",
                         target_id=suite_id, cron="0 9 * * 1", timezone="Asia/Shanghai", enabled=True)
        session.add(sched)
        await session.commit()
        sched_id = sched.id
    try:
        listed = {s["id"]: s for s in (await client.get("/api/evals/suites")).json()["suites"]}
        got = listed[suite_id]
        assert got["latest"]["id"] == new and got["latest"]["accuracy"]["rate"] == 0.75
        # 趋势只连同一变体、评完了的：B 那一次、失败的那一次都不进；旧的在前
        assert [p["id"] for p in got["trend"]] == [old, mid, new], got["trend"]
        assert [p["rate"] for p in got["trend"]] == [0.25, 0.5, 0.75]
        assert other not in [p["id"] for p in got["trend"]] and failed not in [p["id"] for p in got["trend"]]
        [schedule] = got["schedules"]
        assert schedule["id"] == sched_id and schedule["enabled"] is True and "09:00" in schedule["description_text"]
        detail = (await client.get(f"/api/evals/suites/{suite_id}")).json()
        assert detail["latest"]["id"] == new and [s["id"] for s in detail["schedules"]] == [sched_id]
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Schedule).where(Schedule.id == sched_id))
            await session.commit()
        await es.drop_suites()


async def test_the_evaluation_says_what_can_be_done_now(client, source):
    suite_id = await es.make_suite(source["id"], name=f"虚构题集_{uuid.uuid4().hex[:6]}")
    try:
        run_id = await _evaluation(suite_id, status="partial", key="A", passed=1, n=2, minutes_ago=5)
        async with SessionLocal() as session:
            session.add(EvalResult(eval_run_id=run_id, seq=1, case_id=None, case_seq=1, case_hash="h", repeat=0,
                                   status="ok", verdict="pass"))
            session.add(EvalResult(eval_run_id=run_id, seq=2, case_id=None, case_seq=2, case_hash="h", repeat=0,
                                   status="cancelled", error="评测已停止"))
            await session.commit()
        actions = (await client.get(f"/api/evals/evaluations/{run_id}")).json()["actions"]
        assert actions == {"stop": False, "retry": True, "rescore": True, "pin": True, "unpin": False, "open": 1,
                           "cleaned": False}
        pinned = await client.post(f"/api/evals/evaluations/{run_id}/pin", json={"pinned": True})
        assert pinned.status_code == 200
        actions = (await client.get(f"/api/evals/evaluations/{run_id}")).json()["actions"]
        assert actions["pin"] is False and actions["unpin"] is True

        done = await _evaluation(suite_id, status="succeeded", key="A", passed=2, n=2, minutes_ago=4, cleaned=True)
        actions = (await client.get(f"/api/evals/evaluations/{done}")).json()["actions"]
        # 明细清理了的：不能重试、不能重评（和接口拒收的条件一致）
        assert actions["retry"] is False and actions["rescore"] is False and actions["cleaned"] is True
        assert (await client.post(f"/api/evals/evaluations/{done}/rescore")).status_code == 409
    finally:
        await es.drop_suites()


async def test_previewing_an_expected_sql_gives_columns_and_rows(client, source):
    made = await client.post("/api/evals/suites", json={"name": f"虚构题集_{uuid.uuid4().hex[:6]}"})
    suite_id = made.json()["id"]
    try:
        body = {"datasource_id": source["id"],
                "sql": "SELECT status AS 状态, SUM(amount) AS gmv FROM orders GROUP BY status ORDER BY status"}
        got = (await client.post(f"/api/evals/suites/{suite_id}/cases/preview", json=body)).json()
        assert got["status"] == "ok" and got["columns"] == ["状态", "gmv"], got
        assert got["rows"] and all(len(r) == 2 for r in got["rows"]) and got["run_id"]
        assert got["rendered"]["sql"].startswith("SELECT status")
        # 没过只读守卫：指明是 SQL 那一格，不回 422
        res = await client.post(f"/api/evals/suites/{suite_id}/cases/preview",
                                json={"datasource_id": source["id"], "sql": "DELETE FROM orders"})
        assert res.status_code == 200 and res.json()["status"] == "invalid" and res.json()["field"] == "gold.sql"
        res = await client.post(f"/api/evals/suites/{suite_id}/cases/preview", json={"sql": "SELECT 1"})
        assert res.json()["status"] == "invalid" and res.json()["field"] == "gold.datasource_id"
    finally:
        await es.drop_suites()


async def test_a_preview_touching_a_masked_column_is_refused(client, tmp_path):
    name, sid = await shop.add_source(shop.build(tmp_path / "masked.db"), options={"mask_columns": ["region"]})
    made = await client.post("/api/evals/suites", json={"name": f"虚构题集_{uuid.uuid4().hex[:6]}"})
    try:
        got = (await client.post(f"/api/evals/suites/{made.json()['id']}/cases/preview", json={
            "datasource_id": sid, "sql": "SELECT region AS 地区, SUM(amount) AS gmv FROM orders GROUP BY region"})).json()
        assert got["status"] == "refused" and got["code"] == "eval_gold_masked" and "遮罩" in got["reason"], got
        assert got["rows"] == [] and got["columns"] == [], "遮罩列的值一格都不能交出去"
    finally:
        await es.drop_suites()
        await drop_source(sid)


async def _asked(question: str, n: int, *, origin: str = "chat") -> list[str]:
    async with SessionLocal() as session:
        conv = Conversation(kind="chat", title="虚构对话")
        session.add(conv)
        await session.flush()
        ids = []
        for i in range(n):
            run = Run(workflow_name="问数据", status="succeeded", graph={}, input={"question": question},
                      origin_kind=origin, run_class="exploratory")
            session.add(run)
            await session.flush()
            session.add(ConversationTurn(conversation_id=conv.id, seq=i, question=question, run_id=run.id,
                                         status="done"))
            ids.append(run.id)
        await session.commit()
        return ids


async def test_signals_give_criteria_trend_and_repeated_question_clusters(client):
    question = f"虚构门店 {uuid.uuid4().hex[:6]} 上周的销售额是多少？"
    runs = await _asked(question, 3)
    eval_question = f"虚构评测题 {uuid.uuid4().hex[:6]} 本月退款率？"
    await _asked(eval_question, 3, origin="eval")
    got = (await client.get("/api/evals/signals", params={"days": 30, "points": 3})).json()
    assert [c["band"] for c in got["criteria"]["e1"]] == ["route", "normalize", "reconcile"]
    assert got["criteria"]["e3"][0]["at_least"] == 0.2
    assert len(got["trend"]) == 3 and got["current"] == got["trend"][-1]
    assert got["trend"][0]["until"] <= got["trend"][1]["since"] or got["trend"][0]["until"] == got["trend"][1]["since"]
    assert got["current"]["e3"]["n"] >= 3 and got["current"]["e3"]["band"] in ("worth", "not_yet")
    mine = [c for c in got["clusters"] if c["representative"] == question]
    assert mine and mine[0]["count"] == 3 and mine[0]["run_id"] in runs, got["clusters"]
    assert not [c for c in got["clusters"] if c["representative"] == eval_question], "评测运行不进问题类"
    assert got["threshold"] == 3 and got["embedder"]
    assert (await client.get("/api/evals/signals", params={"days": 0})).status_code == 422


def test_bands_follow_the_reference_lines():
    from app.api import evals

    assert [evals._e1_band(x) for x in (None, 0.05, 0.1, 0.29, 0.3)] == [None, "reconcile", "normalize", "normalize",
                                                                           "route"]
    assert [evals._e3_band(x) for x in (None, 0.19, 0.2)] == [None, "not_yet", "worth"]
