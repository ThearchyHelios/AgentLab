"""问数据的一轮在服务端跑：并发发起、晚到的复核、使用者能做的事、入口的历史、对话的种类（复核意见 A1–A5）。

守的是：
- 同一段对话同时两次新问：只起得了一轮（另一次 409），两轮不会拿到同一个 seq、不会有两个任务同时跑；同一轮同时几次重试、
  两个标签页同时请服务端接着收尾：只有一次起得来，别的 409，不会 500，同一轮不会建两次图；事件的 seq 并发追加也不撞；
  发起写到一半出错时什么都不留下（这段对话不会从此一直「正忙」）；后台任务对同一轮不起第二个；
- 使用者（user 角色）不能「继续运行」：以前 POST /api/runs/{id}/continue 对它是 403（只放行停止），问数据这里照原说法；
- 晚到的复核：开着的实时流先收到 review.late 再收到 stream.end；它记的用量不会被紧接着的重试覆盖掉；
- 运行入口的 history 只取这一轮之前的几轮（照以前页面 historyOf 的规则：截 600 字、跳过没答案的、最多 6 轮）；
- 只有问数据的对话（kind=chat）能发起一轮。

库、问题、名字都是虚构的；助手和报告撰写都是剧本。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.chat import job as chat_job
from app.chat import log as turn_log
from app.db.base import SessionLocal
from app.db.models import Conversation, ConversationTurn, Run
from app.db.models_chat import ChatTurnJob
from app.main import app
from tests import eval_samples as es
from tests.test_chat_turns import (  # noqa: F401 - 夹具照用
    _ask, _conversation, _done, _events, _row, _stage, _until, client, engine_up, people, source,
)


class Counting(es.Copilot):
    """数一数生成过程起了几次（一次生成里的「接着写」「自查」不算）；gate 给了就每次起草前停在闸上。"""

    def __init__(self, source: str, *, gate: asyncio.Event | None = None, **kw: Any) -> None:
        super().__init__(source, **kw)
        self.gate = gate
        self.drafts = 0

    async def astream(self, messages):
        if len(messages) == 2 and "上线前自查" not in messages[1][1]:
            self.drafts += 1
            if self.gate is not None:
                await self.gate.wait()
        async for chunk in super().astream(messages):
            yield chunk


async def _jobs_of(conversation_id: str) -> list[ChatTurnJob]:
    async with SessionLocal() as session:
        return list((await session.execute(select(ChatTurnJob).where(
            ChatTurnJob.conversation_id == conversation_id))).scalars())


# --------------------------------------------------------------------------
# A1 并发发起
# --------------------------------------------------------------------------


async def test_two_asks_at_once_start_only_one_turn(client, monkeypatch, source):
    gate = asyncio.Event()
    model = Counting(source["name"], gate=gate, mode="reply")
    es.use_copilot(monkeypatch, model)
    conversation_id = await _conversation(client)
    body = {"conversation_id": conversation_id, "question": "上周各区域销售额"}
    got = await asyncio.gather(*(client.post("/api/chat/turns", json=body) for _ in range(2)))
    codes = sorted(r.status_code for r in got)
    assert codes == [202, 409], [r.text for r in got]
    assert [r for r in got if r.status_code == 409][0].json()["code"] == "chat_turn_busy"
    async with SessionLocal() as session:
        rows = list((await session.execute(select(ConversationTurn).where(
            ConversationTurn.conversation_id == conversation_id))).scalars())
    assert len(rows) == 1 and rows[0].seq == 0, [(r.id, r.seq) for r in rows]
    assert len(await _jobs_of(conversation_id)) == 1
    gate.set()
    await _done(rows[0].id)
    assert model.drafts == 1 and (await _row(rows[0].id)).status == "done"


async def test_retrying_one_turn_from_several_places_at_once_starts_it_once(client, monkeypatch, source):
    model = Counting(source["name"], mode="reply")
    es.use_copilot(monkeypatch, model)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, "上周各区域销售额")
    await _done(turn_id)
    gate = asyncio.Event()
    model.gate, model.drafts = gate, 0
    body = {"conversation_id": conversation_id, "retry_of": turn_id, "mode": "retry"}
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as raw:
        got = await asyncio.gather(*(raw.post("/api/chat/turns", json=body) for _ in range(3)))
    codes = sorted(r.status_code for r in got)
    assert codes == [202, 409, 409], [r.text for r in got]
    gate.set()
    job = await _done(turn_id)
    assert job.status == "succeeded" and model.drafts == 1, "同一轮只建了一次图"
    events = await _events(turn_id)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert [e["data"]["mode"] for e in events if e["type"] == "turn.attempt"] == ["ask", "retry"]
    assert len((await _row(turn_id)).meta["attempts"]) == 1, "只归档了一次"


async def test_two_tabs_resuming_the_same_turn_start_it_once(client):
    conversation_id = await _conversation(client)
    async with SessionLocal() as session:
        run = Run(status="succeeded", output={"答案": "华东 300。"}, graph={"nodes": [], "edges": []}, run_class="exploratory")
        session.add(run)
        await session.flush()
        turn = ConversationTurn(conversation_id=conversation_id, seq=0, question="q", status="running", run_id=run.id,
                                meta={"v": 1, "runId": run.id})
        session.add(turn)
        await session.commit()
        turn_id = turn.id
    body = {"conversation_id": conversation_id, "retry_of": turn_id, "mode": "resume"}
    got = await asyncio.gather(*(client.post("/api/chat/turns", json=body) for _ in range(2)))
    assert sorted(r.status_code for r in got) == [202, 409], [r.text for r in got]
    await _done(turn_id)
    assert [e["type"] for e in await _events(turn_id)].count("end") == 1
    assert (await _row(turn_id)).answer == "华东 300。"


async def test_event_seqs_never_collide_under_concurrent_appends(client):
    conversation_id = await _conversation(client)
    async with SessionLocal() as session:
        turn = ConversationTurn(conversation_id=conversation_id, seq=0, question="q", status="done")
        session.add(turn)
        await session.flush()
        session.add(ChatTurnJob(id=turn.id, conversation_id=conversation_id, status="succeeded"))
        await session.commit()
        turn_id = turn.id

    async def one(i: int) -> int:
        async with SessionLocal() as session:
            return await turn_log.append(session, turn_id, "op", {"op": "heartbeat", "i": i})

    seqs = await asyncio.gather(*(one(i) for i in range(20)))
    assert sorted(seqs) == list(range(1, 21)), seqs
    async with SessionLocal() as session:
        job = await session.get(ChatTurnJob, turn_id)
    assert job.last_seq == 20


async def test_a_start_that_fails_halfway_leaves_nothing_behind(client, monkeypatch, source):
    """写 turn.attempt 时出错：轮次、任务、事件一样都不留下（不会留着一个排队中、却没有后台任务的任务，让这段对话从此一直正忙）。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"], mode="reply"))
    conversation_id = await _conversation(client)
    real = turn_log.add
    calls = {"n": 0}

    async def flaky(session, turn_id, type_, data=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("写事件时库出错了")
        return await real(session, turn_id, type_, data)

    monkeypatch.setattr(turn_log, "add", flaky)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as raw:
        r = await raw.post("/api/chat/turns", json={"conversation_id": conversation_id, "question": "第一次"})
    assert r.status_code == 500
    async with SessionLocal() as session:
        assert not (await session.execute(select(ConversationTurn).where(
            ConversationTurn.conversation_id == conversation_id))).first()
    assert not await _jobs_of(conversation_id)
    turn_id = await _ask(client, conversation_id, "再问一次")
    await _done(turn_id)
    assert (await _row(turn_id)).status == "done"


async def test_the_job_manager_does_not_start_a_turn_twice(client, monkeypatch, source):
    gate = asyncio.Event()
    es.use_copilot(monkeypatch, Counting(source["name"], gate=gate, mode="reply"))
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id)
    await _stage(turn_id, "build")
    first = chat_job.manager._runs[turn_id]
    assert chat_job.manager.start(turn_id) is False, "还在跑的同一轮不起第二个"
    assert chat_job.manager._runs[turn_id] is first, "也不把登记换成新的（停止要停得到正在跑的那个）"
    gate.set()
    await _done(turn_id)


# --------------------------------------------------------------------------
# A2 使用者不能继续运行
# --------------------------------------------------------------------------


async def test_a_user_cannot_continue_a_run_from_chat_either(people):
    """以前 POST /api/runs/{id}/continue 对使用者是 403（role_required，只放行停止）；问数据这里照原说法，运行不动。"""
    uma = people["uma"]
    conversation_id = await _conversation(uma, "uma 的对话")
    async with SessionLocal() as session:
        run = Run(status="failed", output={}, graph={"nodes": [], "edges": []}, run_class="exploratory",
                  origin_kind="chat", started_by="uma", error="查询超时")
        session.add(run)
        await session.flush()
        turn = ConversationTurn(conversation_id=conversation_id, seq=0, question="q", status="error", error="查询超时",
                                run_id=run.id, meta={"v": 1, "runId": run.id, "runStatus": "failed"})
        session.add(turn)
        await session.commit()
        turn_id, run_id = turn.id, run.id
    old = await uma.post(f"/api/runs/{run_id}/continue", json={})
    r = await uma.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id, "mode": "continue"})
    assert r.status_code == 403 and r.json()["code"] == "role_required", r.text
    assert (old.status_code, old.json()["detail"]) == (r.status_code, r.json()["detail"]), "说法同 POST /api/runs/{id}/continue"
    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).status == "failed"
        assert await session.get(ChatTurnJob, turn_id) is None
    # 重试本轮、重新运行本轮照旧能用（它们另起一次问数据运行）
    r = await uma.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id, "mode": "rerun"})
    assert r.status_code != 403, r.text


# --------------------------------------------------------------------------
# A3 晚到的复核
# --------------------------------------------------------------------------


async def _late_turn(client, monkeypatch, source) -> tuple[str, str, asyncio.Event]:
    import app.api.copilot as copilot

    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    monkeypatch.setattr(chat_job, "REVIEW_WAIT_S", 0.05)
    release = asyncio.Event()

    async def slow(session, run, **kw):
        await release.wait()
        return {"verdict": "ok", "signals": [], "retry_instruction": None, "usage": {"calls": 1, "cost_usd": 0.004}}

    monkeypatch.setattr(copilot, "review_of_run", slow)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    await _done(turn_id)
    assert chat_job.late_pending(turn_id)
    return conversation_id, turn_id, release


async def test_an_open_stream_gets_the_late_review_before_it_ends(client, monkeypatch, source):
    _, turn_id, release = await _late_turn(client, monkeypatch, source)
    last = (await _events(turn_id))[-1]["seq"]

    async def active() -> str | None:
        return await chat_job.stream_status(turn_id)

    async def follow() -> list[dict[str, Any]]:
        got = []
        async for frame in turn_log.stream(turn_id, last, active=active, heartbeat=0.2):
            if frame.startswith("data: "):
                got.append(json.loads(frame[6:]))
        return got

    listening = asyncio.ensure_future(follow())
    await asyncio.sleep(0.3)
    release.set()
    frames = await asyncio.wait_for(listening, timeout=10)
    assert [f["type"] for f in frames] == ["review.late", "stream.end"], frames
    assert frames[0]["data"]["usage"] == {"calls": 1, "cost_usd": 0.004}


async def test_a_retry_right_after_does_not_lose_the_late_review_usage(client, monkeypatch, source):
    """晚到的复核还没回来就点了重试：它回来时记的用量不会被新的一次尝试覆盖（这一轮的花费照实是两次加起来）。"""
    conversation_id, turn_id, release = await _late_turn(client, monkeypatch, source)
    gate = asyncio.Event()
    es.use_copilot(monkeypatch, Counting(source["name"], gate=gate, mode="reply"))
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id, "mode": "retry"})
    assert r.status_code == 202, r.text
    await _stage(turn_id, "build")
    release.set()
    await _until(lambda: _not_late(turn_id))
    gate.set()
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "done" and row.meta["usage"].get("review", {}).get("cost_usd") == 0.004, row.meta["usage"]


async def _not_late(turn_id: str) -> bool:
    return not chat_job.late_pending(turn_id)


# --------------------------------------------------------------------------
# A4 运行入口的 history 只取这一轮之前的
# --------------------------------------------------------------------------


async def test_history_only_takes_the_turns_before_this_one():
    """照以前页面 historyOf 的规则：这一轮之前的、有答案（答案或说明）的，每轮截 600 字，最多 6 轮。"""
    from app.api.conversations import run_history

    async with SessionLocal() as session:
        conversation = Conversation(kind="chat", title="历史")
        session.add(conversation)
        await session.flush()
        rows = [("第一问", "答一", "", "done"), ("没答上的一问", "", "", "error"), ("第三问", "", "只有说明", "error"),
                ("长的一问", "长" * 700, "", "done"), ("这一轮", "", "", "running"), ("后面的一问", "后面的答案", "", "done")]
        for seq, (q, a, e, st) in enumerate(rows):
            session.add(ConversationTurn(conversation_id=conversation.id, seq=seq, question=q, answer=a, explanation=e,
                                         status=st))
        await session.commit()
        text = await run_history(session, conversation.id, before_seq=4)
        assert text == "\n\n".join(["问：第一问\n答：答一", "问：第三问\n答：只有说明", "问：长的一问\n答：" + "长" * 600])
        assert await run_history(session, conversation.id, before_seq=0) == ""
        for seq in range(6, 14):
            session.add(ConversationTurn(conversation_id=conversation.id, seq=seq, question=f"问{seq}", answer=f"答{seq}",
                                         status="done"))
        await session.commit()
        assert (await run_history(session, conversation.id, before_seq=14)).count("问：") == 6


async def test_rerunning_an_earlier_turn_does_not_feed_it_later_turns(client, monkeypatch, source):
    es.use_copilot(monkeypatch, es.Copilot(source["name"], mode="reply"))
    conversation_id = await _conversation(client)
    first = await _ask(client, conversation_id, "第一问")
    await _done(first)
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    second = await _ask(client, conversation_id, "第二问", datasource_ids=[source["id"]])
    await _done(second)
    es.use_copilot(monkeypatch, es.Copilot(source["name"], mode="reply"))
    third = await _ask(client, conversation_id, "第三问")
    await _done(third)
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": second, "mode": "rerun"})
    assert r.status_code == 202, r.text
    job = await _done(second)
    async with SessionLocal() as session:
        run = await session.get(Run, job.run_id)
    history = run.input.get("history", "")
    assert "第一问" in history and "第三问" not in history, history


# --------------------------------------------------------------------------
# A5 只有问数据的对话能发起一轮
# --------------------------------------------------------------------------


async def test_only_chat_conversations_can_start_a_turn(client):
    async with SessionLocal() as session:
        canvas = Conversation(kind="canvas", title="画布助手的对话")
        app_conv = Conversation(kind="app", title="应用润色")
        session.add_all([canvas, app_conv])
        await session.commit()
        ids = [canvas.id, app_conv.id]
    for conversation_id in ids:
        r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "question": "q"})
        assert r.status_code == 422 and r.json()["code"] == "chat_turn_invalid", r.text
        async with SessionLocal() as session:
            n = await session.scalar(select(func.count()).select_from(ConversationTurn).where(
                ConversationTurn.conversation_id == conversation_id))
        assert n == 0

