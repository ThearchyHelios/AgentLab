"""对账结果的「说明有用 / 看不懂」（next-round 02 做法 10 的 E2；app/ledger/feedback.py）。

真的跑：虚构店铺库里的问数据答案（算上了退款）和受管口径「周销售」（只算已支付）对一次账，再在结果上给反馈：
- 反馈记成对账运行上的封存后事件 ledger.feedback（批注不是证据，不进封存范围）；同一个人改主意只算最后一次；
- 账本接口 reconciled 每一项带上反馈（看的这个人给过的、各几个），界面照着画选中的那一个；
- 统计 E2 数到它（不再写「第 4 波接上」）；
- 不认的反馈、不是这个答案的对账、还没跑完的对账照实拒绝。
数据都是编的。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import RunEvent
from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.main import app
from tests import ledger_samples as S

CHECKED = {"start": "2026-09-01", "end": "2026-09-08"}


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    path = tmp_path / "shop.db"
    await S.fresh_source(path)
    yield path
    await ledger_match.drain()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def reconciled(client, monkeypatch) -> tuple[str, str]:
    """问一次（算上了退款）、按核对过的参数和「周销售」对一次账，等它跑完。返回 (答案, 对账运行)。"""
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    body = (await client.get(f"/api/runs/{row.id}/ledger")).json()
    [action] = [a for a in body["actions"] if a["kind"] == "reconcile"]
    res = await client.post(f"/api/ledger/answers/{row.id}/reconcile",
                            json={"query": action["query"], "workflow_id": wf, "version": 1, "inputs": CHECKED,
                                  "confirm": True})
    assert res.status_code == 201, res.text
    done = await S.wait(res.json()["run_id"])
    assert done.status == "succeeded", done.error
    return row.id, done.id


async def test_feedback_is_a_post_seal_note_counted_by_e2(client, monkeypatch):
    answer, rec = await reconciled(client, monkeypatch)
    before = (await client.get("/api/ledger/stats")).json()["e2"]["feedback"]
    assert before["available"] is True and "第 4 波" not in str(before)

    res = await client.post(f"/api/ledger/answers/{answer}/reconcile/{rec}/feedback", json={"verdict": "useful"})
    assert res.status_code == 200, res.text
    assert res.json() == {"run_id": rec, "feedback": {"mine": "useful", "useful": 1, "unclear": 0}}
    async with SessionLocal() as session:
        [data] = (await session.execute(select(RunEvent.data).where(
            RunEvent.run_id == rec, RunEvent.type == EventType.LEDGER_FEEDBACK))).scalars()
    assert data["verdict"] == "useful" and data["post_seal"] is True and data["source_run"] == answer

    # 改主意：同一个人只算最后一次
    res = await client.post(f"/api/ledger/answers/{answer}/reconcile/{rec}/feedback", json={"verdict": "unclear"})
    assert res.json()["feedback"] == {"mine": "unclear", "useful": 0, "unclear": 1}
    [item] = (await client.get(f"/api/runs/{answer}/ledger")).json()["reconciled"]
    assert item["run_id"] == rec and item["feedback"] == {"mine": "unclear", "useful": 0, "unclear": 1}

    after = (await client.get("/api/ledger/stats")).json()["e2"]["feedback"]
    assert after["useful"] - before["useful"] == 0 and after["unclear"] - before["unclear"] == 1
    assert after["runs"] - before["runs"] == 1 and after["available"] is True


async def test_feedback_refusals(client, monkeypatch):
    answer, rec = await reconciled(client, monkeypatch)
    bad = await client.post(f"/api/ledger/answers/{answer}/reconcile/{rec}/feedback", json={"verdict": "great"})
    assert bad.status_code == 422
    other = await S.explore(monkeypatch, S.ELSEWHERE, text="门店 [[v:Q1.r0.n]] 家。")
    res = await client.post(f"/api/ledger/answers/{other.id}/reconcile/{rec}/feedback", json={"verdict": "useful"})
    assert res.status_code == 404 and res.json()["code"] == "ledger_feedback_refused", "不是这个答案的对账"
    res = await client.post(f"/api/ledger/answers/{answer}/reconcile/{answer}/feedback", json={"verdict": "useful"})
    assert res.status_code == 404, "答案自己不是对账运行"
    # 还没封存的对账（正在跑、刚发起）：没有结果可评
    from app.db.models import Run

    async with SessionLocal() as session:
        row = await session.get(Run, rec)
        sealed = row.manifest_hash
        row.manifest_hash = None
        await session.commit()
    try:
        res = await client.post(f"/api/ledger/answers/{answer}/reconcile/{rec}/feedback", json={"verdict": "useful"})
        assert res.status_code == 409 and "还没运行完" in res.json()["detail"], res.text
    finally:
        async with SessionLocal() as session:
            (await session.get(Run, rec)).manifest_hash = sealed
            await session.commit()


def test_offline_page_and_summary_know_the_feedback_event():
    import re
    from pathlib import Path

    from app.bundle.build import _summary

    script = (Path(__file__).resolve().parents[1] / "app" / "bundle" / "verifier.js").read_text(encoding="utf-8")
    block = re.search(r"const EVENT_LABEL = \{(.*?)\n  \}", script, re.S)
    labels = dict(re.findall(r"'([a-z_.]+)': '([^']+)'", block.group(1)))
    assert labels["ledger.feedback"] == "对账说明的反馈"
    assert _summary("ledger.feedback", {"verdict": "useful"}) == "反馈：说明有用"
    assert _summary("ledger.feedback", {"verdict": "unclear"}) == "反馈：看不懂"
