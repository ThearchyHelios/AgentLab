"""问数据的一轮在服务端跑（docs/design/orchestration-round/00-总方案.md §1；app/chat/job.py、app/api/chat_turns.py）。

守的是：
- 一轮跑完（建图 → 运行 → 复核 → 落库）不依赖任何前端请求：发起之后只等后台任务，conversation_turns 那一行由服务端写好；
- 事件日志：每一步按 seq 记下，实时流先回放、再推实时，中途断开、按 seq 重连，补齐、不重复；思考增量合并成一条；
- 停止：建图中、运行中都能停，运行跟着取消，那一行收成「已取消」；
- 服务重启：建图中的标中断（照实说、可重试本轮）；已发起运行的接着等那次运行、照常复核收尾；开关关着（拷来的数据目录）时停着，
  页面请服务端接着收尾（mode=resume）才做；
- 按复核重来：复核给了 retry_instruction，带着这次运行的图重建一次，换下来的运行的用量并进这一轮；
- 用户点的重试本轮：上一次的结局归档进 meta.attempts，交给助手的话照以前页面上的措辞；
- 同一段对话一次只有一轮在跑（停在审批上等人的不算）；
- 使用者（user 角色）：只碰得到自己的对话的轮次；发起的运行照旧遮罩、只读、记忆是自己的范围；访问密钥调不了这些接口。

库、问题、名字都是虚构的；助手和报告撰写都是剧本，不调真接口。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.chat import job as chat_job
from app.chat import log as turn_log
from app.db.base import SessionLocal
from app.db.models import Approval, Conversation, ConversationTurn, Run
from app.db.models_chat import ChatTurnEvent, ChatTurnJob
from app.engine.runner import run_manager
from app.main import app
from tests import eval_samples as es
from tests.fixtures.sources import drop_source

QUESTION = es.QUESTION


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await chat_job.shutdown()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def source(tmp_path):
    name, sid = await es.make_source(tmp_path)
    yield {"name": name, "id": sid}
    await drop_source(sid)


async def _conversation(client, title: str = "虚构的问数据会话") -> str:
    r = await client.post("/api/conversations", json={"kind": "chat", "title": title})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _ask(client, conversation_id: str, question: str = QUESTION, **extra: Any) -> str:
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "question": question, **extra})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["turn"]["status"] == "running" and body["turn"]["job"]["status"] in ("queued", "running")
    return body["turn_id"]


async def _done(turn_id: str, timeout: float = 60.0) -> ChatTurnJob:
    await chat_job.manager.wait(turn_id, timeout=timeout)
    async with SessionLocal() as session:
        job = await session.get(ChatTurnJob, turn_id)
    assert job is not None and job.status not in ("queued", "running"), job and (job.status, job.stage)
    return job


async def _row(turn_id: str) -> ConversationTurn:
    async with SessionLocal() as session:
        return await session.get(ConversationTurn, turn_id)


async def _events(turn_id: str) -> list[dict[str, Any]]:
    return await turn_log.read(turn_id, 0, limit=10_000)


async def _until(cond, timeout: float = 20.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if await cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等不到条件成立")


async def _stage(turn_id: str, stage: str) -> None:
    async def hit() -> bool:
        async with SessionLocal() as session:
            job = await session.get(ChatTurnJob, turn_id)
        return job is not None and job.stage == stage
    await _until(hit)


async def _launched(turn_id: str) -> str:
    """等这一轮发起了运行，交回运行 id（stage=run 在发起之前就记下了，要等的是任务上的 run_id）。"""
    async def hit() -> bool:
        async with SessionLocal() as session:
            job = await session.get(ChatTurnJob, turn_id)
        return job is not None and bool(job.run_id)
    await _until(hit)
    async with SessionLocal() as session:
        return (await session.get(ChatTurnJob, turn_id)).run_id


def _sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


# --------------------------------------------------------------------------
# 一轮跑完不依赖页面
# --------------------------------------------------------------------------


async def test_a_turn_runs_to_the_end_without_the_page(client, monkeypatch, source):
    """发起之后页面一个请求都不再发：建图、运行、复核、落库都在服务端做完。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    job = await _done(turn_id)
    assert job.status == "succeeded" and job.stage is None and job.run_id, (job.status, job.error)

    row = await _row(turn_id)
    assert row.status == "done", (row.status, row.error)
    assert "330" in row.answer, row.answer
    assert row.run_id == job.run_id and row.graph and row.graph.get("nodes")
    meta = row.meta
    assert meta["runId"] == job.run_id and meta["runClass"] == "exploratory" and meta["runStatus"] == "succeeded"
    assert meta["queries"] == 1 and isinstance(meta["ms"], int)
    assert meta["scope"] == [{"id": source["id"], "name": source["name"]}]
    assert meta["looks"] and meta["looks"][0]["op"] == "look", "查看记录照页面以前的形状存进 meta.looks"
    assert set(meta["usage"]) >= {"copilot", "run"}, "这一轮的花费 = 助手建图 + 运行（+ 复核）"
    assert isinstance(row.review, dict) and row.review.get("verdict") == "ok", "复核原样落库（含 verdict=ok）"

    async with SessionLocal() as session:
        run = await session.get(Run, job.run_id)
    assert run.origin_kind == "chat" and run.origin.get("conversation_id") == conversation_id
    assert run.origin.get("turn_id") == turn_id and run.input.get("question") == QUESTION

    events = await _events(turn_id)
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), "seq 从 1 起连续"
    kinds = [e["type"] for e in events]
    assert kinds[0] == "turn.attempt" and kinds[-1] == "end"
    assert [e["data"]["stage"] for e in events if e["type"] == "stage"] == ["build", "run", "review"]
    ops = [e["data"]["op"] for e in events if e["type"] == "op"]
    assert {"model", "look", "add_node", "final", "usage"} <= set(ops), ops
    [launched] = [e for e in events if e["type"] == "run"]
    assert launched["data"]["run_id"] == job.run_id
    end = events[-1]["data"]
    assert end["outcome"] == "done" and end["turn"]["status"] == "done" and end["turn"]["answer"] == row.answer
    assert end["run"]["id"] == job.run_id and end["output"], "end 带完整成果，页面不必再回源"

    # 对话详情里这一轮带着它的任务；列表状态是 done
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert detail["turns"][-1]["job"]["status"] == "succeeded" and detail["last_status"] == "done"
    state = (await client.get(f"/api/chat/turns/{turn_id}")).json()
    assert state["job"]["status"] == "succeeded" and state["job"]["last_seq"] == seqs[-1]
    assert state["job"]["attempt_seq"] == 1 and state["turn"]["id"] == turn_id


async def test_reply_and_ask_settle_like_the_page_did(client, monkeypatch, source):
    """直接回答：done、没查库（noQuery）；助手反问：done，答案列是那句问题，meta.asked 记着问题和候选。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"], mode="reply"))
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, "刚才那个数是多少")
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "done" and row.answer == "销售额是 330。" and row.meta["noQuery"] is True
    assert (await _events(turn_id))[-1]["data"]["outcome"] == "reply"

    es.use_copilot(monkeypatch, es.Copilot(source["name"], mode="ask"))
    turn_id = await _ask(client, conversation_id, "上周销售额")
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "done" and row.answer == "要按下单日还是支付日？"
    assert row.meta["asked"]["options"] == ["下单日", "支付日"] and not row.meta.get("noQuery")


async def test_a_refusal_before_the_stream_is_recorded_as_its_cause(client, monkeypatch, source):
    """限定的数据源都不在了：开流之前就被拒，这一轮记 error，原因带机器码（页面据此给「不限数据源重试」）。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=["not-a-source"])
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "error" and row.meta["cause"]["kind"] == "refused"
    assert row.meta["cause"]["code"] == "datasource_scope_empty" and row.error.startswith("限定的数据源")


# --------------------------------------------------------------------------
# 事件日志、实时流
# --------------------------------------------------------------------------


class Gated(es.Copilot):
    """第一次起草前停在闸门上，放行之后照剧本走（测实时流中途接入、断开重连）。思考先吐几段增量。"""

    def __init__(self, source: str, gate: asyncio.Event, **kw: Any) -> None:
        super().__init__(source, **kw)
        self.gate = gate

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        if len(self.calls) == 0:
            for piece in ("先想一想", "要查哪张表", "……"):
                yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": piece})
            await self.gate.wait()
        async for chunk in super().astream(messages):
            yield chunk


async def test_the_stream_replays_then_follows_and_reconnects_by_seq(client, monkeypatch, source):
    """订阅方中途接入：先回放已有的、再推实时的；断开之后按收到的最后一个 seq 重连，补齐、不重复，最后是 stream.end。"""
    gate = asyncio.Event()
    es.use_copilot(monkeypatch, Gated(source["name"], gate))
    es.use_report(monkeypatch)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    await _stage(turn_id, "build")

    async def active() -> str | None:
        return await chat_job.stream_status(turn_id)

    first: list[dict[str, Any]] = []
    stream = turn_log.stream(turn_id, 0, active=active, heartbeat=0.2)
    async for frame in stream:
        # 助手的心跳不带 seq、不落库（chat/log.beat）：按 seq 补齐时不算它，同页面
        if frame.startswith("data: ") and "seq" in json.loads(frame[6:]):
            first.append(json.loads(frame[6:]))
        if any(e.get("type") == "stage" for e in first):
            break
    await stream.aclose()   # 断开
    assert first[0]["type"] == "turn.attempt" and first[0]["seq"] == 1

    gate.set()
    rest: list[dict[str, Any]] = []
    pings = 0
    async for frame in turn_log.stream(turn_id, first[-1]["seq"], active=active, heartbeat=0.2):
        if frame == turn_log.PING:
            pings += 1
            continue
        event = json.loads(frame[6:])
        if "seq" in event or event.get("type") == "stream.end":
            rest.append(event)
    assert rest[-1]["type"] == "stream.end" and rest[-1]["status"] == "succeeded"
    got = [e["seq"] for e in first + rest[:-1]]
    assert got == list(range(1, len(got) + 1)), "补齐、不重复"
    assert got[-1] == (await _events(turn_id))[-1]["seq"]

    # 跑完之后按 seq 重连（HTTP）：只给 seq 之后的，再给 stream.end
    after = got[-3]
    text = (await client.get(f"/api/chat/turns/{turn_id}/stream", params={"after": after})).text
    frames = _sse(text)
    assert [f["seq"] for f in frames[:-1]] == got[-2:] and frames[-1]["type"] == "stream.end"
    assert [f["seq"] for f in _sse((await client.get(f"/api/chat/turns/{turn_id}/stream")).text)[:-1]] == got

    # 思考增量合并成一条，不逐条落库
    thinking = [e for e in await _events(turn_id) if e["type"] == "op" and e["data"]["op"] == "thinking"]
    assert len(thinking) == 1 and thinking[0]["data"]["delta"] == "先想一想要查哪张表……", thinking


# --------------------------------------------------------------------------
# 停止
# --------------------------------------------------------------------------


async def test_stop_while_building(client, monkeypatch, source):
    gate = asyncio.Event()
    es.use_copilot(monkeypatch, Gated(source["name"], gate))
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id)
    await _stage(turn_id, "build")
    r = await client.post(f"/api/chat/turns/{turn_id}/cancel")
    assert r.status_code == 200, r.text
    job = await _done(turn_id)
    assert job.status == "cancelled"
    row = await _row(turn_id)
    assert row.status == "error" and row.error == "已取消" and row.meta["outcome"] == "cancelled"
    assert (await _events(turn_id))[-1]["data"]["outcome"] == "cancelled"
    r = await client.post(f"/api/chat/turns/{turn_id}/cancel")
    assert r.status_code == 409 and r.json()["code"] == "chat_turn_finished"


async def test_stop_while_running_cancels_the_run(client, monkeypatch, source):
    """运行中停止：运行跟着取消（不只是断开订阅——以前点了停止，模型和 SQL 其实还在跑、还在计费）。"""
    from app.providers import mock_model

    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    hold = asyncio.Event()

    async def slow(self, messages, stop=None, run_manager=None, **kwargs):
        await hold.wait()
        raise AssertionError("不该放行")

    async def slow_stream(self, messages, stop=None, run_manager=None, **kwargs):
        await hold.wait()
        raise AssertionError("不该放行")
        yield  # noqa: unreachable - 让它成为异步生成器

    # 报告撰写卡在模型调用上（流式、非流式都卡住）：运行一直在跑，等着被停止
    monkeypatch.setattr(mock_model.MockChatModel, "_agenerate", slow)
    monkeypatch.setattr(mock_model.MockChatModel, "_astream", slow_stream)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    run_id = await _launched(turn_id)
    assert (await client.post(f"/api/chat/turns/{turn_id}/cancel")).status_code == 200
    job = await _done(turn_id)
    assert job.status == "cancelled"
    await run_manager.wait_idle(run_id)
    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).status == "cancelled"
    row = await _row(turn_id)
    assert row.status == "error" and row.error == "已取消" and row.meta["runStatus"] == "cancelled"


# --------------------------------------------------------------------------
# 服务重启
# --------------------------------------------------------------------------


async def _stranded(conversation_id: str, *, run: Run | None, stage: str, mode: str = "ask",
                    principal: dict[str, Any] | None = None) -> str:
    """造一轮「上次进程没了时还在跑」的：那一行 running、任务 running（不在这个进程里）。principal 是发起时的身份快照。"""
    async with SessionLocal() as session:
        if run is not None:
            session.add(run)
            await session.flush()
        turn = ConversationTurn(conversation_id=conversation_id, seq=0, question="上周各区域销售额", status="running",
                                run_id=run.id if run else None, meta={"v": 1, "runId": run.id if run else None})
        session.add(turn)
        await session.flush()
        session.add(ChatTurnJob(id=turn.id, conversation_id=conversation_id, status="running", stage=stage, mode=mode,
                                run_id=run.id if run else None, request={"started_ms": 0}, principal=principal or {}))
        await session.commit()
        return turn.id


async def test_restart_interrupts_builds_and_finishes_launched_runs(client, monkeypatch):
    conversation_id = await _conversation(client)
    building = await _stranded(conversation_id, run=None, stage="build")
    finished = await _stranded(conversation_id, run=Run(status="succeeded", output={"答案": "华东 300。"},
                                                        graph={"nodes": [], "edges": []}, run_class="exploratory",
                                                        origin_kind="chat"), stage="review")
    suspended = await _stranded(conversation_id, run=Run(status="interrupted", output={}, graph={"nodes": [], "edges": []},
                                                         run_class="exploratory", origin_kind="chat"), stage="run")
    counts = await chat_job.startup(resume=True)
    assert counts["interrupted"] >= 1 and counts["resumed"] >= 2
    for turn_id in (finished, suspended):
        await chat_job.manager.wait(turn_id, timeout=30)

    row = await _row(building)
    assert row.status == "error" and row.meta["cause"] == {"kind": "build_interrupted"}, "建图中的照实标中断"
    async with SessionLocal() as session:
        assert (await session.get(ChatTurnJob, building)).status == "interrupted"
    assert (await _events(building))[-1]["data"]["outcome"] == "error"

    row = await _row(finished)
    assert row.status == "done" and row.answer == "华东 300。", "已发起运行的：接着等那次运行、照常复核收尾"
    assert row.review.get("verdict") == "ok"

    row = await _row(suspended)
    assert row.status == "error" and row.meta["outcome"] == "suspended" and row.error == "服务重启，本轮已中断"


async def test_with_the_switch_off_launched_runs_wait_for_someone_to_open_the_conversation(client):
    """拷来的数据目录（AGENTLAB_SCHEDULER=0）：启动时不替人收尾，那一行不动；页面请服务端接着收尾（resume）才做。"""
    conversation_id = await _conversation(client)
    turn_id = await _stranded(conversation_id, run=Run(status="succeeded", output={"答案": "华北 30。"},
                                                       graph={"nodes": [], "edges": []}, run_class="exploratory",
                                                       origin_kind="chat"), stage="review")
    counts = await chat_job.startup(resume=False)
    assert counts["parked"] >= 1
    assert (await _row(turn_id)).status == "running"
    async with SessionLocal() as session:
        assert (await session.get(ChatTurnJob, turn_id)).status == "interrupted"
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id,
                                                   "mode": "resume"})
    assert r.status_code == 202, r.text
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "done" and row.answer == "华北 30。"
    assert not row.meta.get("attempts"), "接着收尾不是重试：不归档"


async def test_a_legacy_turn_stuck_building_is_marked_interrupted_at_startup(client):
    """这一波之前页面推的老轮次（没有任务）停在 running、又没有运行的：它的建图那条流随上次的进程一起没了。"""
    conversation_id = await _conversation(client)
    async with SessionLocal() as session:
        turn = ConversationTurn(conversation_id=conversation_id, seq=0, question="老轮次", status="running")
        session.add(turn)
        await session.commit()
        legacy = turn.id
    await chat_job.startup(resume=False)
    row = await _row(legacy)
    assert row.status == "error" and row.meta["cause"] == {"kind": "build_interrupted"}


async def test_a_turn_waits_for_the_approver_and_finishes_after(client, monkeypatch):
    """停在审批上：照旧等人（评测是取消）。审批过了运行接着跑，这一轮照常复核收尾。"""
    conversation_id = await _conversation(client)
    run = Run(status="interrupted", output={}, graph={"nodes": [], "edges": []}, run_class="exploratory", origin_kind="chat")
    turn_id = await _stranded(conversation_id, run=run, stage="run")
    async with SessionLocal() as session:
        session.add(Approval(run_id=run.id, node_id="ask", mode="approve", title="确认一下", payload={}, status="pending"))
        await session.commit()
    monkeypatch.setattr(chat_job, "APPROVAL_POLL_S", 0.1)
    await chat_job.startup(resume=True)
    await _stage(turn_id, "waiting")
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert detail["last_status"] == "waiting"
    # 别的轮次照样能问（停在审批上的不算占着这段对话）
    async with SessionLocal() as session:
        busy = await chat_job._busy(session, conversation_id)
    assert busy is False
    # 审批人处理了、运行跑完了
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
        row.status, row.output = "succeeded", {"答案": "审批之后的答案"}
        approval = (await session.execute(select(Approval).where(Approval.run_id == run.id))).scalar_one()
        approval.status = "approved"
        await session.commit()
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "done" and row.answer == "审批之后的答案"
    stages = [e["data"]["stage"] for e in await _events(turn_id) if e["type"] == "stage"]
    assert stages[-3:] == ["waiting", "run", "review"], stages


# --------------------------------------------------------------------------
# 重来：按复核自动重来一次、用户点的重试本轮
# --------------------------------------------------------------------------


async def test_the_review_retries_once_with_the_run_graph(client, monkeypatch, source):
    import app.api.copilot as copilot
    from app.chat import turn as policy

    model = es.Copilot(source["name"])
    es.use_copilot(monkeypatch, model)
    es.use_report(monkeypatch)
    real = copilot.review_of_run
    attempts: list[int] = []

    async def review(session, run, *, question, attempt=0, **kw):
        attempts.append(attempt)
        out = await real(session, run, question=question, attempt=attempt, **kw)
        if attempt == 0:
            out = {**out, "verdict": "annotated", "severity": "broken", "note": "查到一半就停了", "retry": True,
                   "signals": [{"kind": "step_limit", "detail": "用满了步数", "severity": "broken"}],
                   "retry_instruction": policy.retry_instruction(question, "用满了步数", True),
                   "usage": {"calls": 1, "cost_usd": 0.003}}
        return out

    monkeypatch.setattr(copilot, "review_of_run", review)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    await _done(turn_id)
    assert attempts == [0, 1], "第二次复核按第二次算，不再给重来"
    events = await _events(turn_id)
    [retry] = [e["data"] for e in events if e["type"] == "turn.retry"]
    assert retry["kind"] == "review" and retry["reason"] == "用满了步数" and retry["base"] is True
    runs = [e["data"]["run_id"] for e in events if e["type"] == "run"]
    assert len(runs) == 2 and runs[0] != runs[1]
    row = await _row(turn_id)
    assert row.status == "done" and row.meta["runId"] == runs[1]
    usage = row.meta["usage"]
    assert usage["review"]["calls"] == 1 and usage["review"]["cost_usd"] == 0.003, "复核那一次的用量记进这一轮"
    assert "earlier" in usage, "换下来的那次运行的用量并进 earlier"
    # 重来那一次交给助手的就是复核给的那句话，带着上一次运行的图
    last = model.calls[-2] if len(model.calls) >= 2 else model.calls[-1]
    assert any("这个工作流上一次运行没有成功：用满了步数" in str(m[1]) for m in last)


async def test_retrying_a_turn_archives_the_previous_attempt(client, monkeypatch, source):
    """用户点「重试本轮」：上一次的结局归档进 meta.attempts（照以前页面上的说法），交给助手的话照以前的措辞，那一行回到 running。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"], mode="reply"))
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, "上周各区域销售额")
    await _done(turn_id)

    seen: list[str] = []
    real = chat_job.ChatDriver.generate_in

    def spy(self, instruction, base_graph):
        seen.append(instruction)
        return real(self, instruction, base_graph)

    monkeypatch.setattr(chat_job.ChatDriver, "generate_in", spy)
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id,
                                                   "mode": "retry"})
    assert r.status_code == 202, r.text
    assert r.json()["turn"]["status"] == "running"
    await _done(turn_id)
    row = await _row(turn_id)
    [archived] = row.meta["attempts"]
    assert archived["n"] == 1 and archived["outcome"] == "done" and archived["summary"] == "已给出答案"
    assert archived["answer"] == "销售额是 330。"
    assert seen == ["上周各区域销售额\n\n（上一次没有成功：已给出答案。请针对这个原因调整，重新给出完整的工作流。）"]
    attempt_events = [e for e in await _events(turn_id) if e["type"] == "turn.attempt"]
    assert [e["data"]["mode"] for e in attempt_events] == ["ask", "retry"]
    assert attempt_events[-1]["data"]["attempts"] == row.meta["attempts"]
    async with SessionLocal() as session:
        job = await session.get(ChatTurnJob, turn_id)
    assert job.attempt_seq == attempt_events[-1]["seq"], "刷新时从这一次尝试的第一条回放"


async def test_one_turn_at_a_time_per_conversation(client, monkeypatch, source):
    gate = asyncio.Event()
    es.use_copilot(monkeypatch, Gated(source["name"], gate))
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id)
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "question": "再问一句"})
    assert r.status_code == 409 and r.json()["code"] == "chat_turn_busy"
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id})
    assert r.status_code == 409
    other = await _conversation(client, "另一段对话")
    other_turn = await _ask(client, other, "别的对话照常问")
    for t in (turn_id, other_turn):
        await client.post(f"/api/chat/turns/{t}/cancel")
        await _done(t)


async def test_continuing_a_finished_run_is_refused_like_the_runs_api(client):
    conversation_id = await _conversation(client)
    run = Run(status="succeeded", output={"答案": "x"}, graph={"nodes": [], "edges": []}, run_class="exploratory")
    async with SessionLocal() as session:
        session.add(run)
        await session.flush()
        turn = ConversationTurn(conversation_id=conversation_id, seq=0, question="q", status="done", answer="x",
                                run_id=run.id, meta={"v": 1, "runId": run.id})
        session.add(turn)
        await session.commit()
        turn_id = turn.id
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id,
                                                   "mode": "continue"})
    assert r.status_code == 409 and "无需继续运行" in r.json()["detail"], r.text
    assert (await _row(turn_id)).status == "done", "被拒时那一行不动"


async def test_deleting_a_conversation_stops_its_turn(client, monkeypatch, source):
    gate = asyncio.Event()
    es.use_copilot(monkeypatch, Gated(source["name"], gate))
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id)
    await _stage(turn_id, "build")
    assert (await client.delete(f"/api/conversations/{conversation_id}")).status_code == 204
    assert not chat_job.manager.running(turn_id)
    async with SessionLocal() as session:
        assert await session.get(ChatTurnJob, turn_id) is None
        assert not (await session.execute(select(ChatTurnEvent).where(ChatTurnEvent.turn_id == turn_id))).first()


# --------------------------------------------------------------------------
# 使用者（user 角色）：归属、遮罩、只读、访问密钥
# --------------------------------------------------------------------------


@pytest.fixture
async def people():
    from tests.fakes import auth_env

    await auth_env.reset()
    admin = await auth_env.turn_on_local()
    assert (await admin.put("/api/auth/settings", json={"user_can_chat": True})).status_code == 200
    for name, role in (("uma", "user"), ("ula", "user"), ("bob", "builder")):
        await auth_env.make_user(name, role)
    clients = {name: await auth_env.login(name) for name in ("uma", "ula", "bob")}
    try:
        yield {"admin": admin, **clients}
    finally:
        async with SessionLocal() as s:
            ids = list((await s.execute(select(Conversation.id).where(
                Conversation.created_by.in_(["uma", "ula", "bob"])))).scalars())
            await s.execute(delete(ConversationTurn).where(ConversationTurn.conversation_id.in_(ids)))
            await s.execute(delete(Conversation).where(Conversation.id.in_(ids)))
            await s.execute(delete(Run).where(Run.started_by.in_(["uma", "ula", "bob"])))
            await s.commit()
        await auth_env.reset()


async def test_a_users_turns_are_theirs_and_their_runs_stay_masked_and_read_only(people, monkeypatch, source):
    from app.tools.datasource import MASK_RESULTS_OPTION, READ_ONLY_OPTION

    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    uma, ula, bob = people["uma"], people["ula"], people["bob"]
    mine = await _conversation(uma, "uma 的对话")
    theirs = await _conversation(ula, "ula 的对话")
    turn_id = await _ask(uma, mine, datasource_ids=[source["id"]])
    job = await _done(turn_id)
    assert job.status == "succeeded", job.error
    async with SessionLocal() as session:
        run = await session.get(Run, job.run_id)
    assert run.started_by == "uma" and run.origin_kind == "chat"
    assert run.options.get(MASK_RESULTS_OPTION) is True and run.options.get(READ_ONLY_OPTION) is True
    assert run.memory_scope == "chat:uma", "记忆固定为这位使用者自己的范围"
    assert (await _row(turn_id)).status == "done"

    # 只碰得到自己的对话的轮次
    for path in (f"/api/chat/turns/{turn_id}", f"/api/chat/turns/{turn_id}/stream"):
        assert (await ula.get(path)).status_code == 403, path
        assert (await uma.get(path)).status_code == 200, path
    assert (await ula.post(f"/api/chat/turns/{turn_id}/cancel")).status_code == 403
    r = await uma.post("/api/chat/turns", json={"conversation_id": theirs, "question": "偷看别人的对话"})
    assert r.status_code == 403
    r = await ula.post("/api/chat/turns", json={"conversation_id": mine, "retry_of": turn_id})
    assert r.status_code == 403
    # 搭建者照旧看得到（对话按人隔离只收窄使用者）
    assert (await bob.get(f"/api/chat/turns/{turn_id}")).status_code == 200

    # 管理员关了问数据：使用者整组接口都用不了
    assert (await people["admin"].put("/api/auth/settings", json={"user_can_chat": False})).status_code == 200
    r = await uma.post("/api/chat/turns", json={"conversation_id": mine, "question": "再问一句"})
    assert r.status_code == 403 and r.json()["code"] == "chat_forbidden"


async def test_a_user_cannot_launch_a_graph_outside_the_chat_whitelist(people, monkeypatch, source):
    """使用者的图里有白名单之外的节点：自查挡下（blocked）或发起被拒（start），这一轮照实记 error，不会跑起来。"""
    class CodeNode(es.Copilot):
        async def astream(self, messages):
            from tests.eval_samples import _line

            self.calls.append(list(messages))
            if "上线前自查" in messages[1][1]:
                yield _line({"op": "done"})
                return
            for op in ({"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
                       {"op": "add_node", "node": {"id": "c", "type": "code", "label": "算一下",
                                                   "config": {"code": "result = 1"}}},
                       {"op": "add_edge", "edge": {"source": "start", "target": "c"}},
                       {"op": "add_node", "node": {"id": "out", "type": "output", "label": "答案", "config": {}}},
                       {"op": "add_edge", "edge": {"source": "c", "target": "out"}},
                       {"op": "done", "explanation": "", "run": True}):
                yield _line(op)

    es.use_copilot(monkeypatch, CodeNode(source["name"]))
    conversation_id = await _conversation(people["uma"], "uma 的对话")
    turn_id = await _ask(people["uma"], conversation_id)
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "error", (row.status, row.error)
    cause = row.meta.get("cause") or {}
    assert cause.get("kind") in ("blocked", "start"), cause


async def test_access_keys_cannot_reach_the_chat_turn_api(people):
    """访问密钥照旧调不了问数据（不在 auth/keys.KEY_ROUTES 里）：发起、看、停一律 403 key_route_forbidden。"""
    from app.db.models_identity import User
    from tests.fakes import auth_env

    await auth_env.personal_keys(True)
    async with SessionLocal() as s:
        uma = (await s.execute(select(User).where(User.username == "uma"))).scalar_one()
    _, secret = await auth_env.make_key(uma)
    keyed = auth_env.keyed(secret)
    for method, path in (("post", "/api/chat/turns"), ("get", "/api/chat/turns/x"),
                         ("get", "/api/chat/turns/x/stream"), ("post", "/api/chat/turns/x/cancel")):
        r = await getattr(keyed, method)(path, **({"json": {"conversation_id": "c"}} if method == "post" else {}))
        assert r.status_code == 403 and r.json().get("code") == "key_route_forbidden", (path, r.text)


# --------------------------------------------------------------------------
# 各种结局照以前页面落库的写法
# --------------------------------------------------------------------------


async def _seed_events(run_id: str, *events: tuple[str, dict[str, Any]]) -> None:
    import time as _time

    from app.db.models import RunEvent

    async with SessionLocal() as session:
        for seq, (kind, data) in enumerate(events, start=1):
            session.add(RunEvent(run_id=run_id, seq=seq, type=kind, ts=_time.time(), data=data))
        await session.commit()


async def test_rejected_failed_and_refused_runs_settle_with_their_cause(client, monkeypatch, source):
    conversation_id = await _conversation(client)
    rejected = Run(status="cancelled", end_reason="rejected", output={}, graph={"nodes": [], "edges": []},
                   run_class="exploratory", error="人工驳回：数字对不上")
    failed = Run(status="failed", output={}, graph={"nodes": [], "edges": []}, run_class="exploratory", error="查询超时")
    r_id = await _stranded(conversation_id, run=rejected, stage="run")
    f_id = await _stranded(conversation_id, run=failed, stage="run")
    await _seed_events(rejected.id, ("run.cancelled", {"reason": "rejected", "actor": "张工", "label": "查数",
                                                       "note": "数字对不上", "decision": "reject"}))
    await _seed_events(failed.id, ("run.failed", {"error": "查询超时", "detail": "TimeoutError: 30s"}))
    await chat_job.startup(resume=True)
    for t in (r_id, f_id):
        await chat_job.manager.wait(t, timeout=30)
    row = await _row(r_id)
    assert row.status == "error" and row.error == "已取消" and row.meta["outcome"] == "cancelled"
    assert row.meta["rejected"] == {"actor": "张工", "label": "查数", "note": "数字对不上", "decision": "reject"}
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert [t for t in detail["turns"] if t["id"] == r_id][0]["meta"]["rejected"]["actor"] == "张工"
    row = await _row(f_id)
    assert row.status == "error" and row.error == "查询超时"
    assert row.meta["cause"] == {"kind": "run", "error": "查询超时", "detail": "TimeoutError: 30s"}

    # 发起被拒：照 LaunchRefused 记下说法、机器码、状态码
    import app.engine.launch as launch
    from app.engine.launch import LaunchRefused

    async def refuse(*_a, **_k):
        raise LaunchRefused(422, "绑定的工具不存在：「查订单」（调用工具）绑定的 db_query__nope。请到「数据」页接入", "run_tool_missing")

    monkeypatch.setattr(launch, "launch_run", refuse)
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    other = await _conversation(client, "发起被拒")
    turn_id = await _ask(client, other, datasource_ids=[source["id"]])
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "error" and row.meta["cause"]["kind"] == "start"
    assert row.meta["cause"]["code"] == "run_tool_missing" and row.meta["cause"]["status"] == 422
    assert row.meta.get("runId") is None


async def test_a_late_review_still_counts_toward_the_turn(client, monkeypatch, source):
    """复核慢过 REVIEW_WAIT_S：先交原答案（不等它），那一次复核晚到了照样把用量记进这一轮（review.late）。"""
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
    row = await _row(turn_id)
    assert row.status == "done" and "330" in row.answer and row.review == {}, "等不及：原答案照交、没有复核结论"
    assert "review" not in row.meta["usage"]
    assert chat_job.late_pending(turn_id) and await chat_job.stream_status(turn_id) is None, "实时流等复核回来再关"
    release.set()
    await _until(lambda: _not_late(turn_id))
    row = await _row(turn_id)
    assert row.meta["usage"]["review"] == {"calls": 1, "cost_usd": 0.004}
    assert (await _events(turn_id))[-1]["type"] == "review.late"
    assert await chat_job.stream_status(turn_id) == "succeeded"


async def _not_late(turn_id: str) -> bool:
    return not chat_job.late_pending(turn_id)


async def test_run_and_rerun_reuse_the_built_graph_without_rebuilding(client, monkeypatch, source):
    """只建不跑的那一轮点「运行」：直接跑那张图，不归档、不重建；重新运行本轮：上一次归档，原图再跑一次。两种都不按复核
    自动重来（同以前页面上 launch 不带 retry）。"""
    import app.api.copilot as copilot

    model = es.Copilot(source["name"])
    es.use_copilot(monkeypatch, model)
    es.use_report(monkeypatch)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    await _done(turn_id)
    built = (await _row(turn_id)).graph
    async with SessionLocal() as session:
        row = await session.get(ConversationTurn, turn_id)
        row.meta = {k: v for k, v in (row.meta or {}).items() if k not in ("runId",)} | {"runId": None}
        row.run_id, row.answer = None, ""
        await session.commit()
    calls = len(model.calls)
    real = copilot.review_of_run

    async def review(session, run, *, question, attempt=0, **kw):
        out = await real(session, run, question=question, attempt=attempt, **kw)
        return {**out, "retry_instruction": "（复核说要重来）"}

    monkeypatch.setattr(copilot, "review_of_run", review)
    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id, "mode": "run"})
    assert r.status_code == 202, r.text
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "done" and "330" in row.answer and not row.meta.get("attempts")
    assert len(model.calls) == calls, "不重建图"
    assert not [e for e in await _events(turn_id) if e["type"] == "turn.retry"], "不按复核自动重来"

    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id,
                                                   "mode": "rerun"})
    assert r.status_code == 202, r.text
    await _done(turn_id)
    row = await _row(turn_id)
    assert row.status == "done" and len(row.meta["attempts"]) == 1 and row.graph == built
    assert len(model.calls) == calls, "重新运行本轮不重建图"

    r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id,
                                                   "mode": "max_steps"})
    assert r.status_code == 422 and r.json()["code"] == "chat_turn_invalid", "放宽到几步要写清楚"


def test_max_steps_only_touches_agent_nodes():
    from app.chat import record

    graph = {"nodes": [{"id": "a", "type": "agent", "data": {"config": {"max_steps": 8, "tools": ["x"]}}},
                       {"id": "o", "type": "output", "data": {"config": {}}}], "edges": []}
    out = record.with_max_steps(graph, 40)
    assert out["nodes"][0]["data"]["config"] == {"max_steps": 40, "tools": ["x"]}
    assert out["nodes"][1] == graph["nodes"][1] and graph["nodes"][0]["data"]["config"]["max_steps"] == 8


def test_attempt_records_say_what_the_page_used_to_say():
    """归档那一次的说法同以前页面上的 attemptOf：已手动停止、服务重启、被驳回（谁在哪一步）、结论不可用、存在缺口、失败原文。"""
    from types import SimpleNamespace

    from app.chat import record

    def row(**kw):
        base = {"status": "done", "error": "", "answer": "答案", "review": {}, "run_id": "r1", "meta": None,
                "updated_at": None}
        return SimpleNamespace(**{**base, **kw})

    assert record.attempt_record(row(status="error", error="已取消"), {"outcome": "cancelled"}, n=1)["summary"] == "已手动停止"
    rej = record.attempt_record(row(status="error", error="已取消"),
                                {"outcome": "cancelled", "rejected": {"actor": "张工", "label": "查数", "note": "不对"}}, n=1)
    assert rej["outcome"] == "rejected" and rej["summary"] == "被驳回：张工 驳回了「查数」：不对"
    old = record.attempt_record(row(status="error"), {"outcome": "cancelled", "rejected": {"sub": "页面记过的那句"}}, n=1)
    assert old["summary"] == "被驳回：页面记过的那句"
    assert record.attempt_record(row(status="error"), {"outcome": "suspended"}, n=2)["summary"] == "服务重启，运行中断"
    broken = record.attempt_record(row(review={"severity": "broken", "note": "查到一半",
                                               "signals": [{"detail": "用满了步数"}]}), {}, n=1)
    assert (broken["outcome"], broken["summary"], broken["detail"]) == ("unusable", "结论不可用：查到一半", "用满了步数")
    assert record.attempt_reason(broken) == "结论不可用：查到一半（用满了步数）"
    assert record.attempt_record(row(review={"severity": "degraded", "note": "检索降级"}), {}, n=1)["summary"] == "存在缺口：检索降级"
    failed = record.attempt_record(row(status="error", error="查询超时"), {"cause": {"kind": "run", "error": "查询超时"}}, n=1)
    assert failed["outcome"] == "failed" and failed["summary"] == "查询超时" and failed["cause"]["kind"] == "run"
    legacy = record.attempt_record(row(status="error", error="x"), {"failure": {"title": "操作超时", "reason": "查询超时。"}}, n=1)
    assert legacy["summary"] == "操作超时：查询超时。"
    assert record.attempt_record(row(answer="字" * 5000), {}, n=1)["answer"].endswith("…")


async def test_an_empty_generation_is_retried_once_on_the_server(client, monkeypatch):
    """助手只写了思考、一个操作都没给（error.next=retry）：服务端带着原因重来一次（turn.retry kind=empty），不用页面在。"""
    import app.api.copilot as copilot
    from tests.test_usage_meter import Scripted

    reply = json.dumps({"op": "reply", "text": "上周没有华中区的数据"}, ensure_ascii=False)
    model = Scripted(model="claude-opus-5", replies=[[""], [reply]])

    async def _model(*_a, **_k):
        return model, "claude-opus-5"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, "上周华中区销售额")
    await _done(turn_id)
    events = await _events(turn_id)
    [retry] = [e["data"] for e in events if e["type"] == "turn.retry"]
    # 原因是重来时交给助手的那句纠正（这次连思考都没有），诊断（不含原文）随事件记下
    assert retry["kind"] == "empty" and retry["reason"].startswith("什么都没有输出"), retry
    assert "正文 0 字" in retry["detail"], retry
    row = await _row(turn_id)
    assert row.status == "done" and row.answer == "上周没有华中区的数据" and row.meta["noQuery"] is True
    assert row.meta["usage"]["copilot"]["calls"] == 2, "两次建图都是这一轮花的"


async def test_several_stranded_turns_of_one_conversation_can_be_resumed_together(client):
    """一段对话里停在半路的几轮（老轮次、服务重启后停着的）一起交给服务端收尾：接着收尾不建图、不发起运行，不算占着这段对话。"""
    conversation_id = await _conversation(client)
    ids = []
    for answer in ("甲", "乙"):
        async with SessionLocal() as session:
            run = Run(status="succeeded", output={"答案": answer}, graph={"nodes": [], "edges": []}, run_class="exploratory")
            session.add(run)
            await session.flush()
            turn = ConversationTurn(conversation_id=conversation_id, seq=len(ids), question=f"问{answer}", status="running",
                                    run_id=run.id, meta={"v": 1, "runId": run.id})
            session.add(turn)
            await session.commit()
            ids.append(turn.id)
    for turn_id in ids:
        r = await client.post("/api/chat/turns", json={"conversation_id": conversation_id, "retry_of": turn_id,
                                                       "mode": "resume"})
        assert r.status_code == 202, r.text
    for turn_id, answer in zip(ids, ("甲", "乙")):
        await _done(turn_id)
        row = await _row(turn_id)
        assert row.status == "done" and row.answer == answer


async def test_a_stop_that_lands_while_launching_still_cancels_the_run(client, monkeypatch, source):
    """停止正好落在发起运行的那一下：发起不能被打断到一半（运行建好了、这一轮却不知道它，就成了没人管的运行）——等它发起完、
    记下运行，再照常取消它，那一行收成已取消。"""
    import app.engine.launch as launch
    from app.providers import mock_model

    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    hold = asyncio.Event()

    async def slow(self, messages, stop=None, run_manager=None, **kwargs):
        await hold.wait()
        raise AssertionError("不该放行")

    async def slow_stream(self, messages, stop=None, run_manager=None, **kwargs):
        await hold.wait()
        raise AssertionError("不该放行")
        yield  # noqa: unreachable - 让它成为异步生成器

    monkeypatch.setattr(mock_model.MockChatModel, "_agenerate", slow)
    monkeypatch.setattr(mock_model.MockChatModel, "_astream", slow_stream)
    entered, release = asyncio.Event(), asyncio.Event()
    real = launch.launch_run

    async def gated(session, **kw):
        entered.set()
        await release.wait()
        return await real(session, **kw)

    monkeypatch.setattr(launch, "launch_run", gated)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    await asyncio.wait_for(entered.wait(), timeout=20)
    stopping = asyncio.ensure_future(client.post(f"/api/chat/turns/{turn_id}/cancel"))
    await asyncio.sleep(0.2)
    release.set()
    assert (await stopping).status_code == 200
    job = await _done(turn_id)
    assert job.status == "cancelled" and job.run_id, "发起完了才停：运行记在这一轮上"
    await run_manager.wait_idle(job.run_id)
    async with SessionLocal() as session:
        assert (await session.get(Run, job.run_id)).status == "cancelled", "这次运行跟着取消了，没有没人管的运行"
    row = await _row(turn_id)
    assert row.status == "error" and row.error == "已取消" and row.meta["runId"] == job.run_id


# --------------------------------------------------------------------------
# 事件日志：心跳不落库、已结束的轮次按期清理
# --------------------------------------------------------------------------


async def test_heartbeats_go_out_on_the_stream_but_are_not_stored(client, monkeypatch, source):
    """心跳（模型几秒没出声时助手发的一拍 heartbeat 操作）：换了阶段的那一拍照落（刷新后过程里那一行阶段回放得出来），同一阶段
    接着的是纯保活，只在实时流上发、不落库，重连时流自己补最近的一拍；别的事件（思考、操作、阶段……）照旧。以前每 3 秒落一条，
    一轮下来几百条心跳。"""
    from app.api import copilot

    gate = asyncio.Event()
    es.use_copilot(monkeypatch, Gated(source["name"], gate))
    monkeypatch.setattr(copilot, "_HEARTBEAT_SECONDS", 0.05)   # use_copilot 把心跳调成了一小时一拍
    es.use_report(monkeypatch)
    conversation_id = await _conversation(client)
    turn_id = await _ask(client, conversation_id, datasource_ids=[source["id"]])
    await _stage(turn_id, "build")

    async def active() -> str | None:
        return await chat_job.stream_status(turn_id)

    beats: list[dict[str, Any]] = []
    stream = turn_log.stream(turn_id, 0, active=active, heartbeat=0.2)
    async for frame in stream:
        if frame.startswith("data: "):
            event = json.loads(frame[6:])
            if event.get("type") == "op" and event["data"].get("op") == "heartbeat":
                beats.append(event)
        if any("seq" not in b for b in beats):
            break
    await stream.aclose()
    assert [b for b in beats if "seq" not in b], "同一阶段接着的心跳不占 seq：重连按 seq 补齐时不会多出来、也不会漏"
    # 重连（从最后一条落库的事件接着读）：流先补一拍最近的心跳，页面接着知道还在等模型
    stored = await _events(turn_id)
    again = turn_log.stream(turn_id, stored[-1]["seq"], active=active, heartbeat=0.2)
    first = await again.__anext__()
    await again.aclose()
    assert first.startswith("data: ") and json.loads(first[6:])["data"]["op"] == "heartbeat"

    gate.set()
    await _done(turn_id)
    events = await _events(turn_id)
    stored = [e for e in events if e["type"] == "op" and e["data"].get("op") == "heartbeat"]
    # 只落换了阶段的那一拍（规划、查看……各一拍），同一阶段接着的几十拍都不落
    phases = [e["data"]["phase"] for e in stored]
    assert phases and phases[0] == "planning" and all(a != b for a, b in zip(phases, phases[1:])), phases
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert turn_log.latest_beat(turn_id) is None, "这一轮结束了，进程里记的那一拍跟着清掉"


async def test_a_later_event_supersedes_the_last_heartbeat():
    """心跳只说「上一条事件之后还在等」：之后落了新事件，重连时不再补那一拍（不然页面的进度会退回去）。"""
    async with SessionLocal() as session:
        conversation = Conversation(kind="chat", title="虚构")
        session.add(conversation)
        await session.flush()
        turn = ConversationTurn(conversation_id=conversation.id, question="q", status="running")
        session.add(turn)
        await session.flush()
        session.add(ChatTurnJob(id=turn.id, conversation_id=conversation.id, status="running"))
        await session.commit()
        turn_id = turn.id
    turn_log.beat(turn_id, {"op": "heartbeat", "phase": "planning", "elapsed_ms": 3000})
    assert turn_log.latest_beat(turn_id)["phase"] == "planning"
    async with SessionLocal() as session:
        await turn_log.append(session, turn_id, "stage", {"stage": "run"})
    assert turn_log.latest_beat(turn_id) is None
    turn_log.forget(turn_id)


async def _finished_turn(*, days_ago: float, status: str = "succeeded", events: int = 3) -> str:
    from datetime import timedelta

    from app.db.base import utcnow

    async with SessionLocal() as session:
        conversation = Conversation(kind="chat", title="虚构的旧会话")
        session.add(conversation)
        await session.flush()
        turn = ConversationTurn(conversation_id=conversation.id, question="虚构的问题", answer="虚构的回答",
                                status="done" if status == "succeeded" else "error", meta={"outcome": "done"})
        session.add(turn)
        await session.flush()
        when = utcnow() - timedelta(days=days_ago)
        session.add(ChatTurnJob(id=turn.id, conversation_id=conversation.id, status=status, last_seq=events,
                                finished_at=None if status in ("queued", "running") else when))
        for seq in range(1, events + 1):
            session.add(ChatTurnEvent(turn_id=turn.id, seq=seq, type="op", ts=0.0, data={"op": "thinking"}))
        await session.commit()
        return turn.id


async def test_finished_turns_lose_their_event_log_after_the_retention_period():
    """已结束超过保留期的轮次整轮删事件日志：页面重开已结束的一轮读的是 conversation_turns 那一行（答案、图、复核、用量都在
    那里），不读事件日志；任务行留着，last_seq 接着发号，之后再在这一轮上重试、继续运行不撞号。还在跑的、刚结束的不动。"""
    from app.db.base import utcnow

    old = await _finished_turn(days_ago=turn_log.RETAIN_DAYS + 1)
    stuck = await _finished_turn(days_ago=turn_log.RETAIN_DAYS + 1, status="interrupted")
    recent = await _finished_turn(days_ago=1)
    live = await _finished_turn(days_ago=turn_log.RETAIN_DAYS + 1, status="running")
    purged = await turn_log.purge(utcnow())
    assert set(purged) >= {old, stuck} and not {recent, live} & set(purged)
    assert await _events(old) == [] and await _events(stuck) == []
    assert len(await _events(recent)) == 3 and len(await _events(live)) == 3
    async with SessionLocal() as session:
        job = await session.get(ChatTurnJob, old)
        row = await session.get(ConversationTurn, old)
    assert job is not None and job.last_seq == 3, "任务行留着：seq 接着往下发"
    assert row.answer == "虚构的回答", "那一行不动"
    async with SessionLocal() as session:
        assert await turn_log.add(session, old, "turn.attempt", {"mode": "retry"}) == 4
        await session.rollback()


async def test_the_cleanup_runs_in_the_background_tick(monkeypatch):
    """清理挂在调度循环的那一拍里（scheduler/loop.process_due），一小时最多清一次。"""
    from app.db.base import utcnow
    from app.scheduler import loop

    old = await _finished_turn(days_ago=turn_log.RETAIN_DAYS + 2)
    monkeypatch.setattr(turn_log, "_last_purge", 0.0)
    await loop.process_due(utcnow())
    assert await _events(old) == []
    again = await _finished_turn(days_ago=turn_log.RETAIN_DAYS + 2)
    await loop.process_due(utcnow())
    assert len(await _events(again)) == 3, "一小时内不再清第二次"


# --------------------------------------------------------------------------
# 服务重启后接着收尾：按接手时的账号重新核对
# --------------------------------------------------------------------------


async def _snapshot_of(name: str) -> dict[str, Any]:
    from app.db.models_identity import User

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == name))).scalar_one()
    return {"username": user.username, "role": user.role, "display_name": user.display_name, "user_id": user.id,
            "groups": [], "verified": True, "mode": "local", "via": "session"}


async def _set_user(name: str, **values: Any) -> None:
    from sqlalchemy import update as sa_update

    from app.db.models_identity import User

    async with SessionLocal() as session:
        await session.execute(sa_update(User).where(User.username == name).values(**values))
        await session.commit()


def _finished_run(by: str) -> Run:
    return Run(status="succeeded", output={"答案": "华东 300。"}, graph={"nodes": [], "edges": []}, run_class="exploratory",
               origin_kind="chat", started_by=by)


async def test_resuming_after_a_restart_checks_the_account_again(people, monkeypatch, source):
    """服务重启后接着收尾一轮（已发起运行的接着等、复核），按接手这一刻的账号重新核对，不再照发起时的身份快照接着做：
    发起之后账号被停用的，照实标中断、写清原因；那一行不再往下改答案。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    mine = await _conversation(people["uma"], "uma 的对话")
    turn_id = await _stranded(mine, run=_finished_run("uma"), stage="review", principal=await _snapshot_of("uma"))
    await _set_user("uma", disabled=True)
    await chat_job.startup(resume=True)
    await chat_job.manager.wait(turn_id, timeout=30)
    row = await _row(turn_id)
    assert row.status == "error" and row.meta["cause"]["kind"] == "identity", row.meta
    assert row.error == "发起这一轮的账号「uma」已停用，本轮没有接着收尾" and not row.answer
    async with SessionLocal() as session:
        assert (await session.get(ChatTurnJob, turn_id)).status == "interrupted"
    assert (await _events(turn_id))[-1]["data"]["outcome"] == "error"


async def test_a_downgraded_account_without_chat_is_interrupted(people, monkeypatch, source):
    """发起时是搭建者，之后被降成使用者、管理员又没给使用者开问数据：已经没有权限了，照实标中断。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    theirs = await _conversation(people["bob"], "bob 的对话")
    turn_id = await _stranded(theirs, run=_finished_run("bob"), stage="review", principal=await _snapshot_of("bob"))
    await _set_user("bob", role="user")
    assert (await people["admin"].put("/api/auth/settings", json={"user_can_chat": False})).status_code == 200
    await chat_job.startup(resume=True)
    await chat_job.manager.wait(turn_id, timeout=30)
    row = await _row(turn_id)
    assert row.status == "error" and row.meta["cause"]["kind"] == "identity"
    assert row.error == "发起这一轮的账号「bob」已经没有问数据的权限，本轮没有接着收尾"


async def test_a_downgraded_account_that_can_still_chat_finishes_as_who_they_are_now(people, monkeypatch, source):
    """降了角色、但仍然用得了问数据（使用者开着问数据）：照常收尾，按现在的身份（角色、组）接着做，不按快照里的搭建者。"""
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    seen: list[Any] = []
    real = chat_job.ChatDriver.drive

    async def spy(self, *a, **k):
        from app.api.deps import current_principal

        seen.append(current_principal())
        return await real(self, *a, **k)

    monkeypatch.setattr(chat_job.ChatDriver, "drive", spy)
    theirs = await _conversation(people["bob"], "bob 的对话")
    turn_id = await _stranded(theirs, run=_finished_run("bob"), stage="review", principal=await _snapshot_of("bob"))
    await _set_user("bob", role="user")
    await chat_job.startup(resume=True)
    await chat_job.manager.wait(turn_id, timeout=30)
    row = await _row(turn_id)
    assert row.status == "done" and row.answer == "华东 300。"
    assert seen and seen[-1].username == "bob" and seen[-1].role == "user"


async def test_the_cleanup_tick_without_a_time_uses_now(monkeypatch):
    """调度循环那一拍不一定给 now（process_due(None)）：清理按当前时间算，不报错。"""
    from app.scheduler import loop

    old = await _finished_turn(days_ago=turn_log.RETAIN_DAYS + 2)
    monkeypatch.setattr(turn_log, "_last_purge", 0.0)
    assert old in await turn_log.purge_tick()
    assert await _events(old) == []
    again = await _finished_turn(days_ago=turn_log.RETAIN_DAYS + 2)
    monkeypatch.setattr(turn_log, "_last_purge", 0.0)
    await loop.process_due(None)
    assert await _events(again) == []
