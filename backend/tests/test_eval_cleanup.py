"""评测的清理与工件回收（app/eval/cleanup.py、core/artifact_store.forget；细案 01 做法 16）。

- 保留规则：最近 N 次、钉住的基线、A/B 的另几臂，还没结束的不动；
- 清掉的：逐题结果、明细工件、底下的运行（作答、算期望的）和它们的工件引用行；总表永久保留，cleaned_at 写明已清理；
- 工件文件一条引用都不剩才删：被别的运行引用着的同一份内容照留；
- 清理过的评测不能重试、不能重评。
题集、问法都是虚构的。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core import artifact_store
from app.db.base import SessionLocal, utcnow
from app.db.models import Artifact, Run
from app.db.models_eval import EvalResult, EvalRun
from app.engine.runner import run_manager
from app.eval import answer as answer_mod
from app.eval import cleanup, runner
from app.main import app
from tests import eval_samples as es
from tests.fixtures.sources import drop_source


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await runner.manager.shutdown()
    await es.drop_suites()
    await run_manager.shutdown()


def test_the_keep_rule():
    now = utcnow()

    def run(i: int, **extra) -> EvalRun:
        return EvalRun(id=f"r{i}", suite_id="s", status=extra.pop("status", "succeeded"),
                       created_at=now - timedelta(minutes=i), **extra)

    runs = [run(1), run(2, pair_id="r2"), run(3, pair_id="r2"), run(4, pinned=True), run(5), run(6, status="running")]
    kept = cleanup.keep_set(runs, 2)
    assert kept == {"r1", "r2", "r3", "r4", "r6"}, "最近 2 次 + A/B 另一臂 + 钉住的 + 还没结束的"


async def test_forget_only_removes_files_nobody_refers_to():
    lonely = await artifact_store.put_json({"评测": "只有一处引用", "n": 1}, kind="test_forget")
    shared = await artifact_store.put_json({"评测": "两处引用", "n": 2}, kind="test_forget")
    async with SessionLocal() as session:
        session.add(Artifact(id=shared, run_id="x" * 32, kind="test_forget"))
        await session.execute(Artifact.__table__.delete().where(Artifact.id.in_([lonely, shared]),
                                                                Artifact.run_id == ""))
        await session.commit()
    removed = await artifact_store.forget([lonely, shared, "not-a-hash", lonely])
    assert removed == [lonely]
    assert artifact_store.load(lonely) is None and artifact_store.load(shared)["n"] == 2
    async with SessionLocal() as session:
        await session.execute(Artifact.__table__.delete().where(Artifact.id == shared))
        await session.commit()
    assert await artifact_store.forget([shared]) == [shared]


async def test_cleaning_keeps_the_summary_and_shared_files(monkeypatch, tmp_path):
    async def drive(self):
        self.turn.path, self.turn.reply = "reply", "不查了"
        return self.turn

    monkeypatch.setattr(answer_mod.Driver, "drive", drive)
    name, sid = await es.make_source(tmp_path)
    try:
        suite_id = await es.make_suite(sid)
        made: list[str] = []
        for i in range(3):
            # 中间那一次换一下数：它的期望查询结果只有它自己有（别的两次查到的是同一份，文件共用）
            _amount(tmp_path, 101.0 if i == 1 else 100.0)
            async with SessionLocal() as session:
                from app.db.models_eval import EvalSuite
                from app.eval.variants import normalize_variant

                suite = await session.get(EvalSuite, suite_id)
                [run] = await runner.start(session, suite, [normalize_variant({})], actor="评测测试")
            await es.wait_eval(run.id)
            made.append(run.id)
        first, middle, last = made
        async with SessionLocal() as session:
            (await session.get(EvalRun, first)).pinned = True
            gold_runs = (await session.execute(select(EvalResult.gold_run_ids).where(
                EvalResult.eval_run_id == middle))).scalar_one()
            owned = list((await session.execute(select(Artifact.id).where(Artifact.run_id.in_(gold_runs)))).scalars())
            details = (await session.execute(select(EvalResult.details_artifact).where(
                EvalResult.eval_run_id == middle))).scalar_one()
            # 同一份内容另有一次运行（不是这次评测的）也引用着：文件要留
            shared = owned[0]
            session.add(Artifact(id=shared, run_id="y" * 32, kind="query_snapshot"))
            await session.commit()
        summary_before = (await _run(middle)).summary
        out = await cleanup.clean_suite(suite_id, keep=1)
        assert out["runs"] == 1 and out["results"] == 1 and out["deleted_runs"] == 2 and out["files"] >= 1
        cleaned = await _run(middle)
        assert cleaned.cleaned_at is not None and cleaned.summary == summary_before, "总表永久保留"
        assert (await _run(first)).cleaned_at is None and (await _run(last)).cleaned_at is None
        async with SessionLocal() as session:
            assert await session.scalar(select(func.count()).select_from(EvalResult).where(
                EvalResult.eval_run_id == middle)) == 0
            assert await session.scalar(select(func.count()).select_from(Run).where(Run.id.in_(gold_runs))) == 0
            assert await session.scalar(select(func.count()).select_from(Artifact).where(
                Artifact.run_id.in_(gold_runs))) == 0
        assert artifact_store.load(shared) is not None, "别的运行还引用着的文件不删"
        async with SessionLocal() as session:
            still = set((await session.execute(select(Artifact.id).where(Artifact.id.in_(owned)))).scalars())
        # 同样的内容被另外两次评测的运行引用着的（同一条期望 SQL 查到同一份结果）也留着；一条引用都不剩的才删
        for artifact in owned:
            assert (artifact_store.load(artifact) is not None) == (artifact in still), artifact
        assert any(a not in still for a in owned), "这次评测独有的工件文件删掉了"
        assert artifact_store.load(details) is None, "明细工件跟着删"
        with pytest.raises(runner.EvalRefused):
            await runner.retry(middle)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post(f"/api/evals/evaluations/{middle}/rescore")
            assert refused.status_code == 409 and refused.json()["code"] == "eval_details_cleaned"
            compared = (await client.get(f"/api/evals/evaluations/{last}/compare",
                                         params={"base": middle})).json()
            assert compared["cleaned"] is True and compared["transitions"] is None
            again = (await client.post(f"/api/evals/suites/{suite_id}/cleanup")).json()
            assert again["runs"] == 0, "已经清过的不再清"
    finally:
        await drop_source(sid)


def _amount(tmp_path, value: float) -> None:
    import sqlite3

    with sqlite3.connect(tmp_path / "shop.db") as db:
        db.execute("UPDATE orders SET amount = ? WHERE id = 1", (value,))


async def _run(run_id: str) -> EvalRun:
    async with SessionLocal() as session:
        return await session.get(EvalRun, run_id)
