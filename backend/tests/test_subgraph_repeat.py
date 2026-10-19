"""同一个子工作流节点在一次运行里执行多次（循环体里、退回修改绕回来）：子图里的节点每次执行都要分得开。

以前子图每次都从空状态起步，里面的节点每次都记成「第 1 次执行」：
- 台账里同一个节点（「sub/fetch」）几条查询都记 exec 1，分不出是第几次；
- 写外部系统的幂等键按 (运行, 节点 id, 第几次执行) 算，子图里的节点 id 还是子图里的（pay，不带前缀）：循环第二轮的
  写入算出同一个键，找到第一轮生效的记录，交回第一轮的回执、这一轮不发送——外部系统少收一笔，界面上只有一句「已经生效」；
  父图里同名的节点、两个子工作流节点套同一个工作流时也撞同一个键。
现在：子图里的节点接着父运行里同一个子工作流节点之前几次数（exec 1、2…），幂等键按这次运行里的节点 id（「sub/pay」）算；
升级前停下的运行（子图里的写入按子图里的 id 记的），恢复时照旧认得出已经生效的那一笔，不重发。
图、能力、外部系统都是虚构的。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import ActionRecord, Run, RunEvent
from app.engine.runner import run_manager
from app.process import actions
from tests import ledger_samples as S
from tests.fakes.fake_erp import PAY, FakeErp, endpoint_row, install


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def edge(source, target, handle=None):
    return {"source": source, "target": target, **({"sourceHandle": handle} if handle else {})}


async def _finish(graph: dict, **kwargs) -> Run:
    run = await run_manager.start(graph=graph, input_payload={}, **kwargs)
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed", "interrupted") and not run_manager.is_active(run.id):
            return row
    raise AssertionError(f"运行 {run.id} 没跑完：{row.status}")


async def _events(run_id: str, kind: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == kind)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


def looped(sub_id: str, *, before: list[dict] | None = None) -> dict:
    """开始 → (before…) → 循环两轮，每轮跑一次子工作流 → 成果。"""
    nodes = [node("start", "input"), *(before or []),
             node("each", "loop", "逐项", mode="foreach", items="[1, 2]", item_var="it", max_iterations=5),
             node("sub", "subgraph", "子流程", workflow_id=sub_id, workflow_version=1, input={"n": "{{ vars.it }}"}),
             node("out", "output", fields=[{"name": "r", "value": "完成"}])]
    chain = ["start", *[b["id"] for b in before or []], "each"]
    edges = [edge(a, b) for a, b in zip(chain, chain[1:])]
    edges += [edge("each", "sub", "body"), edge("sub", "each"), edge("each", "out", "done")]
    return {"nodes": nodes, "edges": edges}


async def test_inner_steps_count_on_across_invocations(tmp_path):
    await S.fresh_source(tmp_path / "shop.db")
    inner = S.chain(node("s", "input"), node("fetch", "tool", tool=S.tool(), args={"sql": "SELECT COUNT(*) AS n FROM orders"}),
                    node("o", "output", fields=[{"name": "n", "value": "{{ nodes.fetch.text }}"}]))
    sub_id = await S.publish("取数子流程", graph=inner, level="draft")
    row = await _finish(looped(sub_id))
    assert row.status == "succeeded", row.error
    finished = await _events(row.id, "node.finished", "sub/fetch")
    execs = [e["exec"] for f in finished for e in f.data.get("evidence") or [] if e.get("kind") == "query"]
    assert execs == [1, 2], "第二轮是子图里那一步的第 2 次执行，不再又记成第 1 次"


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp()


async def _unattended(monkeypatch, erp) -> None:
    """付款能力设成可以无人值守写入、节点不要确认：两轮都直接发，不停下等人（和确认无关，只看幂等键分不分得开）。"""
    caps = install(monkeypatch, erp, await endpoint_row(test=True), lookup=False)
    caps[PAY].unattended_write = True


def pay(nid: str = "pay") -> dict:
    return node(nid, "tool", "付款", tool=PAY, approval="never", args={"amount": "{{ input.n }}", "card": "6222020200112233"})


async def test_a_write_inside_a_loop_is_sent_on_every_round(monkeypatch, erp):
    await _unattended(monkeypatch, erp)
    inner = S.chain(node("s", "input", fields=[{"name": "n"}]), pay(),
                    node("o", "output", fields=[{"name": "单号", "value": "{{ nodes.pay.receipt.id }}"}]))
    sub_id = await S.publish("付款子流程", graph=inner, level="draft")
    row = await _finish(looped(sub_id))
    assert row.status == "succeeded", row.error
    assert len(erp.writes("test")) == 2, "第二轮的写入照发，不交回第一轮的回执"
    async with SessionLocal() as session:
        records = list((await session.execute(select(ActionRecord).where(ActionRecord.run_id == row.id)
                                              .order_by(ActionRecord.created_at))).scalars())
    assert [(r.node_id, r.exec_no) for r in records] == [("sub/pay", 1), ("sub/pay", 2)]
    assert len({r.action_key for r in records}) == 2
    assert not [e for e in await _events(row.id, "log") if e.data.get("code") == "action_already_applied"]


async def test_a_namesake_in_the_parent_does_not_share_the_key(monkeypatch, erp):
    """父图里也有一个 pay：子图里的 pay 不再和它撞同一个幂等键。"""
    await _unattended(monkeypatch, erp)
    inner = S.chain(node("s", "input", fields=[{"name": "n"}]), pay(), node("o", "output", fields=[{"name": "r", "value": "x"}]))
    sub_id = await S.publish("付款子流程", graph=inner, level="draft")
    graph = S.chain(node("start", "input", fields=[{"name": "n"}]), pay(),
                    node("sub", "subgraph", "子流程", workflow_id=sub_id, workflow_version=1, input={"n": "7"}),
                    node("out", "output", fields=[{"name": "r", "value": "完成"}]))
    run = await run_manager.start(graph=graph, input_payload={"n": 5})
    for _ in range(600):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and not run_manager.is_active(run.id):
            break
    assert row.status == "succeeded", row.error
    assert len(erp.writes("test")) == 2
    async with SessionLocal() as session:
        ids = sorted(r.node_id for r in (await session.execute(
            select(ActionRecord).where(ActionRecord.run_id == row.id))).scalars())
    assert ids == ["pay", "sub/pay"]


async def test_a_write_recorded_before_the_upgrade_is_still_found(monkeypatch, erp):
    """升级前子图里的写入按子图里的 id（pay）记幂等键。停在子图里的运行恢复时整个子图重跑：照旧按那个键找到已经生效的
    记录、交回回执，不重发。父图里有同名节点时不这样认（那一条可能是父图那个节点的）。"""
    caps = install(monkeypatch, erp, await endpoint_row(test=True), lookup=False)
    async with SessionLocal() as session:
        run = Run(graph={"nodes": [node("sub", "subgraph")], "edges": []}, status="running", run_class="exploratory")
        session.add(run)
        await session.flush()
        legacy = actions.action_key(run.id, "pay", 1)
        session.add(ActionRecord(action_key=legacy, attempt=1, idem_key=f"{legacy}:1", run_id=run.id, node_id="pay",
                                 exec_no=1, capability=PAY, environment="test", payload_hash="0" * 64, state="confirmed",
                                 receipt={"summary": {"id": "PAY-OLD"}}))
        await session.commit()
        run_id = run.id
    args = {"amount": "5", "card": "6222020200112233"}
    found = await actions.prepare(run_id=run_id, node_id="sub/pay", exec_no=1, capability=caps[PAY], args=args,
                                  legacy_node_id="pay")
    assert found.key == legacy and found.receipt is not None
    fresh = await actions.prepare(run_id=run_id, node_id="sub/pay", exec_no=1, capability=caps[PAY], args=args)
    assert fresh.key != legacy and fresh.receipt is None
