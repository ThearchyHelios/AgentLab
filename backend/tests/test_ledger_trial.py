"""口径试跑（POST /api/ledger/calibers/{wf}/{version}/{node}/trial）：按固定版本派生只读图，跑一次探索运行。

试跑不是正式出具：来源记 trial；参数按入口 Schema 校验，缺的补缺省值；返回指标值和运行 id。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import Run
from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.main import app
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    await S.fresh_source(tmp_path / "shop.db")
    yield
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_trial_runs_the_pinned_caliber_and_returns_its_metrics(client):
    wf = await S.publish()
    res = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                            json={"inputs": {"start": "2026-09-01", "end": "2026-09-08"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "succeeded" and body["run_id"]
    assert [m["id"] for m in body["metrics"]] == ["gmv"]
    assert float(body["metrics"][0]["value"]) == 170.5
    assert body["metrics"][0]["unit"] == "元"
    async with SessionLocal() as session:
        run = await session.get(Run, body["run_id"])
    assert run is not None and run.origin_kind == "trial" and run.run_class == "exploratory"
    assert run.origin["kind"] == "caliber_trial" and run.origin["workflow_id"] == wf


async def test_trial_refuses_missing_required_inputs_and_unknown_fields(client):
    wf = await S.publish()
    missing = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial", json={"inputs": {}})
    assert missing.status_code == 422 and "start" in missing.text
    unknown = await client.post(f"/api/ledger/calibers/{wf}/1/card/trial",
                                json={"inputs": {"start": "2026-09-01", "end": "2026-09-08", "nope": 1}})
    assert unknown.status_code == 422 and "nope" in unknown.text
