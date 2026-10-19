"""往停下的运行里追加事件（engine/run_append.py 的 append_events）。

- 停在审批时追加：事件接在最后一条后面、落进以后的封存范围。批准、运行结束封存之后 verify 照旧一致，
  改动追加的事件 verify 就对不上——它们确实在封存范围里
- 追加途中运行被恢复（条件更新落空）：什么都不写，返回 None，last_seq 不动
- 质询指明问的是哪张审批卡（approval_id）时，那张卡仍待审批才写；返回的事件和落库的 id、序号一样
- 先落库再广播，没写入的一条都不广播
- 已封存的运行追加：带 post_seal、落在 manifest_seq 之后，verify 照旧一致；封存不是这次结束之后封的、
  封存和调用方开始时看到的不同、没有终态事件的老封存，都不追加
- 连续追加、并发追加，序号严格递增，整条事件流没有重复的序号
- last_seq 落后于库里实际的最后一条（按需裁判的追加和封存后追加交错提交时会这样）：进程重启后恢复、
  继续运行、放弃、按需裁判都接在实际的最后一条后面，不撞号；runner 记事件不把 last_seq 写小
- 恢复后的重放不受影响：前面的节点不重复执行，Agent 里批过的工具不重复执行（含升级前写下的断点）
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel
from sqlalchemy import select

from app.core.bus import bus
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Approval, Run, RunEvent
from app.engine import compiler
from app.engine.context import NodeError
from app.engine.runner import RunManager, run_manager, verify_manifest
from app.engine.schema import NodeType
from app.tools.registry import register
from tests import appdb

#: Agent 场景里工具真正执行的次数。注册表是进程级的，工具只注册一次，计数按测试清零
CALLS: list[str] = []


class _TallyArgs(BaseModel):
    label: str


@register("inquiry_tally", "测试用：每执行一次记一笔", "test", _TallyArgs, dangerous=True, needs_context=False)
async def _tally(label: str) -> str:
    CALLS.append(label)
    return json.dumps({"label": label, "n": len(CALLS)}, ensure_ascii=False)


@pytest.fixture(autouse=True)
async def engine_up():
    CALLS.clear()
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


GATED = chain(node("start", "input"),
              node("work", "transform", mode="template", template="本周销售额 4321.5 元"),
              node("gate", "human", mode="approve", title="这份周报可以出具吗"),
              node("out", "output", fields=[{"name": "r", "value": "{{ nodes.work }}"}]))
SIMPLE = chain(node("start", "input"), node("out", "output", fields=[{"name": "r", "value": "完成"}]))
TWO_GATES = chain(node("start", "input"),
                  node("g1", "human", mode="approve", title="第一道：口径对吗"),
                  node("g2", "human", mode="approve", title="第二道：可以出具吗"),
                  node("out", "output", fields=[{"name": "r", "value": "完成"}]))
#: 「boom」节点按 flaky 的开关失败：先让运行失败、封存，再关掉开关继续运行
FLAKY = chain(node("start", "input"), node("boom", "transform", mode="template", template="门店 17 家"),
              node("out", "output", fields=[{"name": "r", "value": "{{ nodes.boom }}"}]))


def inquiry(question: str) -> tuple:
    return (EventType.APPROVAL_INQUIRY, None, {"question": question, "answer": f"关于「{question}」：按订单表核对一致"})


@pytest.fixture
def executed(monkeypatch) -> Counter:
    """每个「数据整形」节点真正执行了几次。"""
    seen: Counter = Counter()
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def counting(state, ctx):
        seen[ctx.node.id] += 1
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, counting)
    return seen


@pytest.fixture
def flaky(monkeypatch) -> dict:
    """FLAKY 里的「boom」在 fail 为真时失败。"""
    state = {"fail": True}
    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def boom(st, ctx):
        if ctx.node.id == "boom" and state["fail"]:
            raise NodeError(ctx.node.id, "数据源暂时不可用")
        return await original(st, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, boom)
    return state


async def _row(run_id: str) -> Run:
    async with SessionLocal() as session:
        return await session.get(Run, run_id)


async def _until(run_id: str, statuses: tuple[str, ...], sealed: bool = False) -> Run:
    for _ in range(500):
        row = await _row(run_id)
        if row.status in statuses and (not sealed or row.manifest_seq is not None):
            await run_manager.wait_idle(run_id)
            return await _row(run_id)
        await asyncio.sleep(0.03)
    raise AssertionError(f"运行 {run_id} 停在 {row.status}")


async def _paused(graph=GATED) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    return await _until(run.id, ("interrupted",))


async def _finished(graph=SIMPLE) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    return await _until(run.id, ("succeeded", "failed"), sealed=True)


async def _events(run_id: str, etype: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        query = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            query = query.where(RunEvent.type == etype)
        return list((await session.execute(query.order_by(RunEvent.seq))).scalars())


def _strictly_increasing(seqs: list[int]) -> bool:
    return all(a < b for a, b in zip(seqs, seqs[1:]))


async def _no_repeated_seq(run_id: str) -> None:
    seqs = [e.seq for e in await _events(run_id)]
    assert len(seqs) == len(set(seqs)), f"事件流里有重复的序号：{seqs}"


async def _pending(run_id: str) -> list[Approval]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(Approval).where(Approval.run_id == run_id, Approval.status == "pending"))).scalars())


def _sql(statement: str, *params) -> None:
    """另开一条连接改库：模拟别处（恢复、继续运行、另一个写入者）抢在中间提交。"""
    appdb.execute((statement, params))


# --------------------------------------------------------------------------
# 停在审批时追加
# --------------------------------------------------------------------------


async def test_appended_while_waiting_are_sealed_with_the_run(executed):
    from app.engine.run_append import append_events

    paused = await _paused()
    subscription = bus.attach(paused.id)
    try:
        written = await append_events(paused.id, [inquiry("销售额为什么涨了"), inquiry("退款算进去了吗")],
                                      require="interrupted")
        assert written is not None and len(written) == 2
        assert written[0].seq > paused.last_seq and _strictly_increasing([e.seq for e in written])
        # 提交之后才广播：在线的客户端收到的就是落了库的这两条
        stream = subscription.stream()
        live = [await asyncio.wait_for(stream.__anext__(), 2) for _ in range(2)]
        assert [e.seq for e in live] == [e.seq for e in written]
    finally:
        subscription.close()
    row = await _row(paused.id)
    assert row.status == "interrupted" and row.last_seq == written[-1].seq
    stored = await _events(paused.id, "approval.inquiry")
    assert [(e.seq, e.node_id, e.data["question"]) for e in stored] == [
        (written[0].seq, None, "销售额为什么涨了"), (written[1].seq, None, "退款算进去了吗")]
    assert all("post_seal" not in e.data for e in stored), "进封存范围的事件不该标成封存之后"

    await run_manager.resume(paused.id, {"approved": True})
    final = await _until(paused.id, ("succeeded", "failed"), sealed=True)
    assert final.status == "succeeded", final.error
    assert final.manifest_seq > written[-1].seq
    check = await verify_manifest(paused.id)
    assert check["ok"] is True and check["sealed_at"] == final.manifest_seq
    # 恢复之后前面的节点没有再执行一遍
    assert executed["work"] == 1
    assert len([e for e in await _events(paused.id, "node.started") if e.node_id == "work"]) == 1
    await _no_repeated_seq(paused.id)

    # 追加的事件确实在封存范围里：改它一个字，封存核对就对不上
    async with SessionLocal() as session:
        event = await session.get(RunEvent, stored[0].id)
        event.data = {**event.data, "answer": "改过的回答"}
        await session.commit()
    assert (await verify_manifest(paused.id))["ok"] is False


async def test_a_resume_that_wins_the_row_leaves_nothing_behind(monkeypatch):
    """读完运行、写入之前，恢复先把运行占成了 running：条件更新落空，一条都不写、last_seq 不动。"""
    from app.engine.run_append import append_events

    paused = await _paused()
    set_seq = bus.set_seq

    def resumed_in_between(run_id, value):
        if run_id == paused.id:
            appdb.execute(("UPDATE runs SET status = 'running' WHERE id = ?", (paused.id,)))
        set_seq(run_id, value)

    monkeypatch.setattr(bus, "set_seq", resumed_in_between)
    assert await append_events(paused.id, [inquiry("毛利率怎么算的")], require="interrupted") is None
    row = await _row(paused.id)
    assert row.status == "running" and row.last_seq == paused.last_seq
    assert await _events(paused.id, "approval.inquiry") == []


async def test_events_written_elsewhere_in_between_win(monkeypatch):
    """读完运行、写入之前，别处（另一个进程，或者没走这把锁的追加）已经接着写了一条：状态没变，last_seq 变了。
    条件更新落空、不写——否则两边各拿着同一个序号，事件流里撞号。"""
    from app.engine.run_append import append_events

    paused = await _paused()
    taken = paused.last_seq + 1
    set_seq = bus.set_seq

    def written_in_between(run_id, value):
        if run_id == paused.id:
            appdb.execute(("INSERT INTO run_events (id, run_id, seq, type, node_id, ts, data) "
                           "VALUES ('elsewhere-1', ?, ?, 'log', NULL, 0, '{}')", (paused.id, taken)),
                          ("UPDATE runs SET last_seq = ? WHERE id = ?", (taken, paused.id)))
        set_seq(run_id, value)

    monkeypatch.setattr(bus, "set_seq", written_in_between)
    bus._seq.pop(paused.id, None)  # 另一个进程的序号本进程不知道
    assert await append_events(paused.id, [inquiry("同时写进来的")], require="interrupted") is None
    assert (await _row(paused.id)).last_seq == taken
    assert await _events(paused.id, "approval.inquiry") == []
    await _no_repeated_seq(paused.id)


async def test_a_last_seq_left_behind_does_not_cause_a_clash():
    """last_seq 比库里实际的最后一条小（别处的追加交错着把它写小了）：照实际的最后一条接号，顺手把 last_seq 写对。"""
    from app.engine.run_append import append_events

    paused = await _paused()
    async with SessionLocal() as session:
        run = await session.get(Run, paused.id)
        run.last_seq = paused.last_seq - 3
        await session.commit()
    bus._seq.pop(paused.id, None)
    written = await append_events(paused.id, [inquiry("接在哪里")], require="interrupted")
    assert written[0].seq == paused.last_seq + 1
    assert (await _row(paused.id)).last_seq == written[0].seq
    await _no_repeated_seq(paused.id)


async def test_a_run_that_already_moved_on_gets_nothing(executed):
    from app.engine.run_append import append_events

    paused = await _paused()
    await run_manager.resume(paused.id, {"approved": True})
    final = await _until(paused.id, ("succeeded",), sealed=True)
    assert await append_events(paused.id, [inquiry("晚到的问题")], require="interrupted") is None
    assert (await _row(paused.id)).last_seq == final.last_seq
    assert await _events(paused.id, "approval.inquiry") == []


async def test_an_inquiry_about_an_approval_needs_that_approval_still_pending(monkeypatch):
    """质询问的是某一张审批卡：开始问模型时那张卡还在等，问完时运行可能已经批过它、停到了下一张上。只看
    「运行停在审批」的话，关于第一张的问答会写进第二次停下里。传 approval_id 时那张卡仍待审批才写，
    和其余条件在同一次条件更新里核对。"""
    from app.engine.run_append import append_events

    paused = await _paused(TWO_GATES)
    [first] = await _pending(paused.id)
    await run_manager.resume(paused.id, {"approved": True}, approval_id=first.id)
    await asyncio.sleep(0.05)
    later = await _until(paused.id, ("interrupted",))
    [second] = await _pending(paused.id)
    assert (first.node_id, second.node_id) == ("g1", "g2")

    assert await append_events(paused.id, [inquiry("第一道的口径")], require="interrupted",
                               approval_id=first.id) is None
    assert await append_events(paused.id, [inquiry("不存在的卡")], require="interrupted",
                               approval_id="no-such-approval") is None
    assert (await _row(paused.id)).last_seq == later.last_seq
    assert await _events(paused.id, "approval.inquiry") == []

    written = await append_events(paused.id, [inquiry("出具前再确认一次")], require="interrupted",
                                  approval_id=second.id)
    assert written is not None
    # 返回的就是落了库的那几条：调用方按 id 或序号回头找，两边对得上
    stored = await _events(paused.id, "approval.inquiry")
    assert [(e.id, e.seq) for e in stored] == [(e.id, e.seq) for e in written]

    # 读完运行、写入之前那张卡被人批了（并行的审批还没凑齐，运行仍停着）：不写
    set_seq = bus.set_seq

    def answered_in_between(run_id, value):
        if run_id == paused.id:
            _sql("UPDATE approvals SET status = 'answered' WHERE id = ?", second.id)
        set_seq(run_id, value)

    monkeypatch.setattr(bus, "set_seq", answered_in_between)
    assert await append_events(paused.id, [inquiry("批完才问到的")], require="interrupted",
                               approval_id=second.id) is None
    assert (await _row(paused.id)).last_seq == written[-1].seq
    assert len(await _events(paused.id, "approval.inquiry")) == 1
    with pytest.raises(ValueError):
        await append_events(paused.id, [inquiry("封存后的卡")], require="sealed", approval_id=second.id)


async def test_published_only_once_stored_and_never_when_not_written(monkeypatch):
    """先落库再广播：在线的客户端收到的每一条库里都已经有了（订阅者靠「先读历史、再接实时」拼完整，见
    RunManager._emit）。没写进运行记录的一条都不广播，否则界面上会多出一条运行记录里没有的问答。"""
    from app.engine.run_append import append_events

    published: list[tuple[int, int]] = []
    publish = bus.publish

    async def stored_when_published(event):
        if event.type == EventType.APPROVAL_INQUIRY:
            [(count,)] = appdb.query("SELECT count(*) FROM run_events WHERE run_id = ? AND seq = ?",
                                     event.run_id, event.seq)
            published.append((event.seq, count))
        await publish(event)

    monkeypatch.setattr(bus, "publish", stored_when_published)
    paused = await _paused()
    written = await append_events(paused.id, [inquiry("客单价怎么算的"), inquiry("含不含退款")],
                                  require="interrupted")
    assert published == [(e.seq, 1) for e in written]

    published.clear()
    set_seq = bus.set_seq

    def resumed_in_between(run_id, value):
        if run_id == paused.id:
            _sql("UPDATE runs SET status = 'running' WHERE id = ?", paused.id)
        set_seq(run_id, value)

    monkeypatch.setattr(bus, "set_seq", resumed_in_between)
    assert await append_events(paused.id, [inquiry("恢复之后才到的")], require="interrupted") is None
    assert published == []


# --------------------------------------------------------------------------
# 封存之后追加
# --------------------------------------------------------------------------


async def test_post_seal_appends_stay_outside_the_seal():
    from app.engine.run_append import append_events

    final = await _finished()
    assert final.status == "succeeded", final.error
    before = await verify_manifest(final.id)
    written = await append_events(final.id, [inquiry("这个数是含税的吗"), inquiry("和上周口径一样吗")],
                                  require="sealed")
    assert written is not None and all(e.data["post_seal"] is True for e in written)
    assert written[0].seq > final.manifest_seq and _strictly_increasing([e.seq for e in written])
    row = await _row(final.id)
    assert (row.status, row.manifest_seq, row.manifest_hash) == ("succeeded", final.manifest_seq, final.manifest_hash)
    assert row.last_seq == written[-1].seq
    after = await verify_manifest(final.id)
    assert after["ok"] is True and (after["sealed_at"], after["events"]) == (before["sealed_at"], before["events"])
    assert [e.data["post_seal"] for e in await _events(final.id, "approval.inquiry")] == [True, True]
    await _no_repeated_seq(final.id)


async def test_a_seal_pinned_by_the_caller_must_still_be_the_seal():
    from app.engine.run_append import append_events

    final = await _finished()
    seal = (final.manifest_seq, final.manifest_hash)
    assert await append_events(final.id, [inquiry("甲")], require="sealed", seal=(seal[0] - 1, seal[1])) is None
    assert await append_events(final.id, [inquiry("乙")], require="sealed", seal=(seal[0], "0" * 64)) is None
    assert await _events(final.id, "approval.inquiry") == []
    written = await append_events(final.id, [inquiry("丙")], require="sealed", seal=seal)
    assert written is not None and written[0].data["question"] == "丙"


@pytest.mark.parametrize("change", [
    "UPDATE runs SET status = 'running' WHERE id = ?",               # 继续运行占住了它
    "UPDATE runs SET manifest_hash = 'resealed' WHERE id = ?",       # 继续运行之后重新封存了
    "UPDATE runs SET manifest_seq = manifest_seq + 1 WHERE id = ?",
], ids=["continued", "resealed-hash", "resealed-seq"])
async def test_a_sealed_run_that_changes_in_between_gets_nothing(monkeypatch, change):
    """封存之后追加也是核对和写入同一行上的条件更新：读完运行、写入之前它被继续运行占住、或者重新封存了，就不写。"""
    from app.engine.run_append import append_events

    final = await _finished()
    set_seq = bus.set_seq

    def changed_in_between(run_id, value):
        if run_id == final.id:
            appdb.execute((change, (final.id,)))
        set_seq(run_id, value)

    monkeypatch.setattr(bus, "set_seq", changed_in_between)
    assert await append_events(final.id, [inquiry("这期为什么少了")], require="sealed") is None
    assert (await _row(final.id)).last_seq == final.last_seq
    assert await _events(final.id, "approval.inquiry") == []


async def test_a_seal_left_from_an_earlier_ending_is_not_the_seal(monkeypatch):
    """继续运行后又失败：终态和 run.failed 先提交、封存后算。这中间运行记录上还是上一次结束时的封存，
    这时追加的事件会被接下来的封存收进去，post_seal 就成了假话——不追加。"""
    from app.engine.run_append import append_events

    original = compiler.RUNNERS[NodeType.TRANSFORM]

    async def boom(state, ctx):
        if ctx.node.id == "boom":
            raise NodeError(ctx.node.id, "数据源暂时不可用")
        return await original(state, ctx)

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, boom)
    first = await _finished(chain(node("start", "input"), node("boom", "transform", mode="template", template="x"),
                                  node("out", "output", fields=[{"name": "r", "value": "x"}])))
    assert first.status == "failed"

    entered, release = asyncio.Event(), asyncio.Event()
    real_seal = RunManager._seal

    async def held_seal(self, run_id):
        if run_id == first.id and not entered.is_set():
            entered.set()
            await release.wait()
        return await real_seal(self, run_id)

    monkeypatch.setattr(RunManager, "_seal", held_seal)
    await run_manager.continue_failed(first.id)
    await asyncio.wait_for(entered.wait(), 10)
    try:
        row = await _row(first.id)
        failures = [e.seq for e in await _events(first.id, "run.failed")]
        assert row.status == "failed" and row.manifest_seq == first.manifest_seq < failures[-1]
        assert await append_events(first.id, [inquiry("为什么又失败了")], require="sealed") is None
    finally:
        release.set()
    await run_manager.wait_idle(first.id)
    resealed = await _row(first.id)
    assert resealed.manifest_seq >= failures[-1]
    written = await append_events(first.id, [inquiry("为什么又失败了")], require="sealed")
    assert written is not None and written[0].seq > resealed.manifest_seq
    assert (await verify_manifest(first.id))["ok"] is True


async def _legacy_sealed(ending: str | None) -> Run:
    """加 manifest_seq 这一列之前封存的老运行：只记了清单哈希，封到哪一条要由 verify_manifest 按事件类型推。"""
    from app.core.artifact_store import manifest_hash

    rows = [(1, "run.started", None, {"nodes": 2}), (2, "node.started", "out", {}),
            (3, "node.finished", "out", {"output": {"r": "完成"}})]
    if ending:
        rows.append((4, ending, None, {}))
    # 老口径：run.finished 在算哈希之后才落库，不在清单里；run.failed / run.cancelled 在
    covered = rows[:-1] if ending == "run.finished" else rows
    async with SessionLocal() as session:
        run = Run(workflow_name="老运行·月报", status="failed" if ending == "run.failed" else "succeeded",
                  graph={}, input={}, last_seq=rows[-1][0], manifest_hash=manifest_hash(covered), manifest_seq=None)
        session.add(run)
        await session.flush()
        for seq, kind, node_id, data in rows:
            session.add(RunEvent(run_id=run.id, seq=seq, type=kind, node_id=node_id, ts=float(seq), data=data))
        await session.commit()
        return run


@pytest.mark.parametrize("ending", [None, "run.finished", "run.failed"], ids=["no-ending", "finished", "failed"])
async def test_a_legacy_seal_takes_appends_only_when_its_range_does_not_move(ending):
    """老封存的范围是推出来的：有终态事件的，封到最后一条终态事件为止，追加在后面不进范围；没有终态事件的，
    封到最后一行——追加一条，范围就跟着挪到它身上，我们自己写的批注被当成篡改。这种不追加。"""
    from app.engine.run_append import append_events

    legacy = await _legacy_sealed(ending)
    before = await verify_manifest(legacy.id)
    assert before["ok"] is True and before["legacy"] is True
    written = await append_events(legacy.id, [inquiry("这期为什么少了一家店")], require="sealed")
    if ending is None:
        assert written is None
        assert (await _row(legacy.id)).last_seq == legacy.last_seq
    else:
        assert written is not None and written[0].data["post_seal"] is True
    after = await verify_manifest(legacy.id)
    assert after["ok"] is True and (after["sealed_at"], after["events"]) == (before["sealed_at"], before["events"])


async def test_the_wrong_kind_of_run_gets_nothing():
    from app.engine.run_append import append_events

    paused, final = await _paused(), await _finished()
    assert await append_events(paused.id, [inquiry("甲")], require="sealed") is None, "停在审批的运行还没封存"
    assert await append_events(final.id, [inquiry("乙")], require="interrupted") is None, "已结束的运行不在等审批"
    assert await append_events("no-such-run", [inquiry("丙")], require="interrupted") is None
    assert await append_events(paused.id, [], require="interrupted") == []
    with pytest.raises(ValueError):
        await append_events(paused.id, [inquiry("丁")], require="running")
    with pytest.raises(ValueError):
        await append_events(paused.id, [("approval.unknown", None, {})], require="interrupted")
    for run in (paused, final):
        assert (await _row(run.id)).last_seq == run.last_seq
        assert await _events(run.id, "approval.inquiry") == []


# --------------------------------------------------------------------------
# 序号
# --------------------------------------------------------------------------


async def test_seqs_strictly_increase_and_never_repeat():
    from app.engine.run_append import append_events

    paused = await _paused()
    seqs: list[int] = []
    for batch in ([inquiry("一")], [inquiry("二"), inquiry("三")], [inquiry("四")]):
        written = await append_events(paused.id, batch, require="interrupted")
        seqs += [e.seq for e in written]
    assert seqs[0] > paused.last_seq and _strictly_increasing(seqs)

    # 同时来几个：同一运行在本进程里排队，读 last_seq 到写入之间不会被另一次插进来，所以一个都不落空
    results = await asyncio.gather(*(append_events(paused.id, [inquiry(f"并发{i}")], require="interrupted")
                                     for i in range(6)))
    assert all(r is not None for r in results), "并发的追加有落空的"
    together = sorted(r[0].seq for r in results)
    assert together[0] > seqs[-1] and _strictly_increasing(together)
    assert (await _row(paused.id)).last_seq == together[-1]
    await _no_repeated_seq(paused.id)

    await run_manager.resume(paused.id, {"approved": True})
    final = await _until(paused.id, ("succeeded",), sealed=True)
    assert (await verify_manifest(final.id))["ok"] is True
    await _no_repeated_seq(final.id)


async def test_a_restarted_process_continues_after_the_stored_events():
    """进程重启过，内存里的序号从 0 起：追加接在库里最后一条后面。"""
    from app.engine.run_append import append_events

    paused = await _paused()
    bus._seq.pop(paused.id, None)
    written = await append_events(paused.id, [inquiry("重启之后的问题")], require="interrupted")
    assert written[0].seq == paused.last_seq + 1
    await _no_repeated_seq(paused.id)


@pytest.mark.parametrize("action", ["continue", "resume", "abandon"])
async def test_the_runner_numbers_after_the_stored_events_when_last_seq_lags(flaky, action):
    """last_seq 比库里实际的最后一条小（怎么来的见下一条），本进程又没有这次运行的序号计数（重启过）：继续运行、
    恢复、放弃都接在实际的最后一条后面。只按 last_seq 接，写出的 run.resumed / run.cancelled 撞上已有的序号，
    重新封存时两条同号的事件一起进了清单。"""
    if action == "continue":
        run = await _finished(FLAKY)
        assert run.status == "failed"
    else:
        run = await _paused()
    stored = (await _events(run.id))[-1].seq
    _sql("UPDATE runs SET last_seq = last_seq - 3 WHERE id = ?", run.id)
    bus._seq.pop(run.id, None)

    if action == "continue":
        flaky["fail"] = False
        await run_manager.continue_failed(run.id)
        first_new = "run.resumed"
    elif action == "resume":
        await run_manager.resume(run.id, {"approved": True})
        first_new = "run.resumed"
    else:
        assert await run_manager.cancel(run.id) == "cancelled"
        first_new = "run.cancelled"
    final = await _until(run.id, ("succeeded", "cancelled"), sealed=True)
    assert final.status == ("cancelled" if action == "abandon" else "succeeded"), final.error
    assert [e.seq for e in await _events(run.id, first_new)][-1] == stored + 1
    await _no_repeated_seq(run.id)
    assert (await verify_manifest(run.id))["ok"] is True


async def test_runner_event_writes_never_lower_last_seq():
    """runner 记事件时 last_seq 只往大里改。取号和提交的先后可以不一样：别处另开会话记的事件（run_manager.note）
    和执行任务同时在写，号大的先提交、号小的后提交，以前后提交的那条把 last_seq 写回小的。"""
    from app.engine.runner import _stage

    paused = await _paused()
    early = run_manager._event(paused.id, EventType.LOG, data={"level": "info", "message": "先取号"})
    late = run_manager._event(paused.id, EventType.LOG, data={"level": "info", "message": "后取号"})
    for event in (late, early):
        async with SessionLocal() as session:
            await _stage(session, event)
            await session.commit()
    assert (await _row(paused.id)).last_seq == late.seq


def _judged_args(run: Run) -> tuple:
    """直接调按需裁判记判定的那一步（api/evidence.py 的 _append_judged）要的参数：问过一次模型、什么都没判成。"""
    from app.api import evidence

    sealed = evidence._Sealed(run_id=run.id, graph={}, seal={"sealed": True, "ok": True}, events=[],
                              manifest_hash=run.manifest_hash, run_manifest_seq=run.manifest_seq)
    report = evidence._Report(node_id="out", doc_artifact="doc-1", ok=None, repairs=None, stats=None, doc=None,
                              hash_ok=None, fields=[])
    outcome = {key: {} for key in ("verdicts", "keys", "unjudged", "skipped", "budget")}
    outcome.update(model="judge-model", priced=True, same_model=False, writer_model=None, cost_usd=0.0021,
                   calls=1, duration_ms=1200, limits_hit=[], gaps=[], notes=[])
    return sealed, report, ["u1"], outcome


async def test_a_judge_interleaved_with_a_post_seal_append_survives_a_restart(flaky, monkeypatch):
    """按需裁判记判定（_append_judged）和封存后追加交错：裁判取了号、还没写，封存后追加抢先提交了。以前裁判的条件更新不带
    last_seq 条件，照样提交、把 last_seq 写回自己那个更小的号（同一进程里计数只增不减，不撞号；多进程时两个进程各自的计数
    会撞号）。现在同 append_events：last_seq 也要还是读到的那个，后到的裁判这一次不记（判定照样交回给点的人），last_seq
    停在追加的那一条上。启动时照旧按库里实际的最后一条改正 last_seq：之后再裁判、继续运行，都接在实际的最后一条后面。"""
    from app.api import evidence
    from app.engine.run_append import append_events

    first = await _finished(FLAKY)
    assert first.status == "failed"
    held, opened = asyncio.Event(), evidence.SessionLocal

    class HeldBeforeUpdate:
        """裁判的会话：执行条件更新之前停住，等封存后追加先提交。"""

        def __init__(self):
            self._cm = opened()

        async def __aenter__(self):
            session = await self._cm.__aenter__()
            execute = session.execute

            async def held_execute(statement, *args, **kwargs):
                if getattr(statement, "is_update", False):
                    await held.wait()
                return await execute(statement, *args, **kwargs)

            session.execute = held_execute
            return session

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    monkeypatch.setattr(evidence, "SessionLocal", HeldBeforeUpdate)
    judging = asyncio.create_task(evidence._append_judged(*_judged_args(first)))
    for _ in range(200):                       # 裁判取到号了
        if bus._seq.get(first.id, 0) > first.last_seq:
            break
        await asyncio.sleep(0.01)
    written = await append_events(first.id, [inquiry("这次为什么失败")], require="sealed")
    held.set()
    assert await judging is None, "追加先提交了：后到的裁判不记，免得两条撞号"
    assert written is not None and written[0].seq == first.last_seq + 2
    assert (await _row(first.id)).last_seq == written[0].seq
    monkeypatch.setattr(evidence, "SessionLocal", opened)

    # 进程重启：内存里的序号计数没了，启动时 setup 照常执行一遍。只改落后的那条，别的运行原样不动
    lagging = await _row(first.id)
    others = [await _finished(), await _paused()]
    await run_manager.shutdown()
    bus._seq.pop(first.id, None)
    await run_manager.setup()
    repaired = await _row(first.id)
    assert (repaired.last_seq, repaired.updated_at) == (written[-1].seq, lagging.updated_at)
    for other in others:
        now = await _row(other.id)
        assert (now.last_seq, now.updated_at) == (other.last_seq, other.updated_at)
    assert await evidence._append_judged(*_judged_args(first)) == {"seq": written[-1].seq + 1}
    await _no_repeated_seq(first.id)

    flaky["fail"] = False
    await run_manager.continue_failed(first.id)
    final = await _until(first.id, ("succeeded",), sealed=True)
    assert final.manifest_seq > written[-1].seq + 1
    await _no_repeated_seq(first.id)
    assert (await verify_manifest(first.id))["ok"] is True


# --------------------------------------------------------------------------
# 恢复后的重放：Agent 里批过的工具不重复执行
# --------------------------------------------------------------------------


def _script_two_calls(monkeypatch) -> None:
    from app.providers import mock_model

    def _decide(self, messages):
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if self.tools and done < 2:
            return AIMessage(content="", tool_calls=[
                {"name": "inquiry_tally", "args": {"label": f"第{done + 1}次"}, "id": f"call_{done + 1}"}])
        return AIMessage(content="两笔都记好了。")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", _decide)


@pytest.mark.parametrize("older_pause", [False, True], ids=["current", "older-protocol"])
async def test_inquiries_do_not_disturb_the_replay(monkeypatch, older_pause):
    """Agent 停在工具审批上时追加问答，批准后工具各执行一次。升级前写下的断点要按事件流判断能不能
    安全恢复（runner._stale_replay 只看 tool.start / human.resolved），追加的问答不影响判断。"""
    from app.engine import replay, runner
    from app.engine.run_append import append_events

    _script_two_calls(monkeypatch)
    if older_pause:
        monkeypatch.setattr(runner, "PROTOCOL", runner.PROTOCOL - 1)
    graph = chain(node("start", "input"),
                  node("bot", "agent", prompt="记两笔", tools=["inquiry_tally"], approval="always", max_steps=5),
                  node("out", "output", fields=[{"name": "r", "value": "{{ nodes.bot.text }}"}]))
    run = await run_manager.start(graph=graph, input_payload={})
    for round_ in range(2):
        paused = await _until(run.id, ("interrupted", "failed", "succeeded"))
        assert paused.status == "interrupted", (paused.status, paused.error)
        assert await append_events(run.id, [inquiry(f"第{round_ + 1}笔记的是什么")], require="interrupted")
        if older_pause:
            monkeypatch.setattr(runner, "PROTOCOL", replay.PROTOCOL)
        await run_manager.resume(run.id, {"approved": True})
        await asyncio.sleep(0.05)
    final = await _until(run.id, ("succeeded", "failed"), sealed=True)
    assert final.status == "succeeded", final.error
    assert CALLS == ["第1次", "第2次"], "批过的工具又执行了一次"
    assert len(await _events(run.id, "approval.inquiry")) == 2
    assert (await verify_manifest(run.id))["ok"] is True
    await _no_repeated_seq(run.id)
