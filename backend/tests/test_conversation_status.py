"""会话列表认得每个会话的最后一轮停在哪；轮次的可信度元数据有自己的一列。

以前左栏只能给「这次打开页面以来取回过内存」的会话标状态。没打开过的会话，
哪怕最后一轮正停在审批上、或者已经失败，列表里也和正常完成的长得一样——
人得一个个点进去才知道哪里在等他。

元数据（出具档位、运行类别、未查库、结局、耗时…）以前挂在 review.meta 下，
于是没复核过的轮次 review 也不为空，「没复核过」和「复核过、没问题」分不开。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal, utcnow
from app.db.models import Approval, ConversationTurn, Run
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _conversation(client, title: str) -> str:
    r = await client.post("/api/conversations", json={"kind": "chat", "title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _turn(client, conv_id: str, question: str = "订单数多少", **patch) -> dict:
    r = await client.post(f"/api/conversations/{conv_id}/turns", json={"question": question})
    assert r.status_code == 201, r.text
    turn = r.json()
    if patch:
        r = await client.patch(f"/api/conversations/{conv_id}/turns/{turn['id']}", json=patch)
        assert r.status_code == 200, r.text
        turn = r.json()
    return turn


async def _run(status: str, *, pending: bool = False) -> str:
    async with SessionLocal() as session:
        run = Run(workflow_name="状态测试", status=status)
        session.add(run)
        await session.flush()
        if pending:
            session.add(Approval(run_id=run.id, node_id="gate", status="pending"))
        await session.commit()
        return run.id


async def _listed(client) -> dict[str, dict]:
    r = await client.get("/api/conversations")
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()}


# --------------------------------------------------------------------------
# REQ-1：meta 自己一列
# --------------------------------------------------------------------------

META = {"v": 1, "runClass": "exploratory", "noQuery": True, "ms": 8200, "queries": 0}


async def test_turn_meta_is_stored_and_read_back_on_its_own(client):
    conv = await _conversation(client, "元数据")
    turn = await _turn(client, conv, meta=META, status="done", answer="12 家")
    assert turn["meta"] == META
    assert turn["review"] is None, "只写了 meta，review 得还是「没复核过」"

    detail = (await client.get(f"/api/conversations/{conv}")).json()
    assert detail["turns"][0]["meta"] == META


async def test_patching_review_and_meta_do_not_overwrite_each_other(client):
    conv = await _conversation(client, "互不覆盖")
    turn = await _turn(client, conv, meta=META)
    review = {"verdict": "annotated", "note": "检索降级"}
    r = await client.patch(f"/api/conversations/{conv}/turns/{turn['id']}", json={"review": review})
    assert r.json()["meta"] == META
    r = await client.patch(f"/api/conversations/{conv}/turns/{turn['id']}",
                           json={"meta": {**META, "ms": 9000}})
    assert r.json()["review"] == review
    assert r.json()["meta"]["ms"] == 9000


async def test_old_turns_with_meta_under_review_still_read_as_meta(client):
    """迁移之前写进去的：meta 挂在 review 下。读的时候由后端兜底，前端只认 turn.meta。"""
    conv = await _conversation(client, "老数据")
    legacy = {"verdict": "ok", "meta": {"v": 1, "outcome": "cancelled"}}
    turn = await _turn(client, conv, review=legacy, status="error", error="已取消")
    assert turn["meta"] == {"v": 1, "outcome": "cancelled"}
    # 新列一旦写过就以它为准
    r = await client.patch(f"/api/conversations/{conv}/turns/{turn['id']}", json={"meta": META})
    assert r.json()["meta"] == META


# --------------------------------------------------------------------------
# REQ-2：列表里的最后一轮状态
# --------------------------------------------------------------------------


async def test_the_list_says_where_each_conversation_last_stopped(client):
    cases: dict[str, tuple[str, str | None]] = {}

    conv = await _conversation(client, "完成")
    run = await _run("succeeded")
    await _turn(client, conv, run_id=run, status="done", answer="12")
    cases[conv] = ("done", run)

    conv = await _conversation(client, "失败")
    run = await _run("failed")
    await _turn(client, conv, run_id=run, status="error", error="查询超时")
    cases[conv] = ("error", run)

    conv = await _conversation(client, "取消")
    await _turn(client, conv, status="error", error="已取消")
    cases[conv] = ("cancelled", None)

    conv = await _conversation(client, "重启挂起")
    run = await _run("interrupted")
    await _turn(client, conv, run_id=run, status="error", error="服务重启，这一轮中断了",
                meta={"v": 1, "outcome": "suspended", "runId": run})
    cases[conv] = ("suspended", run)

    conv = await _conversation(client, "在跑")
    run = await _run("running")
    await _turn(client, conv, run_id=run)
    cases[conv] = ("running", run)

    conv = await _conversation(client, "等审批")
    run = await _run("interrupted", pending=True)
    await _turn(client, conv, run_id=run)
    cases[conv] = ("waiting", run)

    conv = await _conversation(client, "停着没审批")
    run = await _run("interrupted")
    await _turn(client, conv, run_id=run)
    cases[conv] = ("suspended", run)

    conv = await _conversation(client, "页面关了但跑完了")
    run = await _run("succeeded")
    await _turn(client, conv, run_id=run)
    cases[conv] = ("done", run)

    conv = await _conversation(client, "页面关了而且失败了")
    run = await _run("failed")
    await _turn(client, conv, run_id=run)
    cases[conv] = ("error", run)

    conv = await _conversation(client, "正在建图")
    await _turn(client, conv)
    cases[conv] = ("running", None)

    listed = await _listed(client)
    got = {cid: (listed[cid]["last_status"], listed[cid]["last_run_id"]) for cid in cases}
    assert got == cases


async def test_only_the_last_turn_counts(client):
    conv = await _conversation(client, "后来好了")
    await _turn(client, conv, status="error", error="查询超时")
    run = await _run("succeeded")
    await _turn(client, conv, run_id=run, status="done", answer="好了")
    assert (await _listed(client))[conv]["last_status"] == "done"


async def test_a_build_abandoned_long_ago_is_not_shown_as_running(client):
    """建图是跟着浏览器那条流走的：页面关了就没了，轮次却一直停在 running。"""
    conv = await _conversation(client, "建到一半关了")
    turn = await _turn(client, conv)
    async with SessionLocal() as session:
        row = await session.get(ConversationTurn, turn["id"])
        row.updated_at = utcnow() - timedelta(hours=2)
        await session.commit()
    assert (await _listed(client))[conv]["last_status"] == "error"


async def test_an_empty_conversation_has_no_status(client):
    conv = await _conversation(client, "空的")
    item = (await _listed(client))[conv]
    assert item["last_status"] is None and item["last_run_id"] is None


async def test_the_detail_carries_the_same_status(client):
    conv = await _conversation(client, "详情也带")
    run = await _run("interrupted", pending=True)
    await _turn(client, conv, run_id=run)
    detail = (await client.get(f"/api/conversations/{conv}")).json()
    assert (detail["last_status"], detail["last_run_id"]) == ("waiting", run)
