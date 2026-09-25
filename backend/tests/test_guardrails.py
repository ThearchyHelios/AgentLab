"""三道"配了、显示了、却没人执行"的护栏。

- max_run_seconds：配置里有，没有任何代码执行它。一个挂住的模型或工具能把运行
  和它占着的并发名额一直拖着。
- 模型调用超时：ModelSpec.timeout 从没被赋过值，所有调用都走 SDK 默认的 10 分钟
  外加重试。
- manifest 哈希：只写不验，而且是在 run.finished 落库之前算的——清单里恰恰少了
  那条宣布"跑完了、成果是什么"的事件。

显示成生效却没生效，比明说没有更危险：它让人以为有东西在兜底。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.core.artifact_store import manifest_hash
from app.core.config import settings
from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import Provider, Run, RunEvent
from app.engine import compiler
from app.engine.runner import run_manager, verify_manifest
from app.engine.schema import NodeType
from app.main import app
from app.providers.factory import ModelSpec, build_chat_model


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes),
            "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


SIMPLE = chain(
    node("start", "input", fields=[{"name": "question"}]),
    node("t", "transform", mode="template", template="好", assign_to="x"),
    node("out", "output", fields=[{"name": "结果", "value": "{{ vars.x }}"}]),
)


async def _run(graph, *, timeout=20.0) -> Run:
    run = await run_manager.start(graph=graph, input_payload={"question": "随便"})
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
            if row and row.status in ("succeeded", "failed", "cancelled", "interrupted"):
                return row
        await asyncio.sleep(0.05)
    raise AssertionError("运行没有按时结束")


# --------------------------------------------------------------------------
# max_run_seconds
# --------------------------------------------------------------------------


async def test_a_run_past_its_time_limit_is_stopped(monkeypatch):
    async def stuck(state, ctx):
        await asyncio.sleep(30)        # 一个不响应的模型或工具
        return {}

    monkeypatch.setitem(compiler.RUNNERS, NodeType.TRANSFORM, stuck)
    monkeypatch.setattr(settings, "max_run_seconds", 1)

    started = time.perf_counter()
    run = await _run(SIMPLE)
    assert run.status == "failed"
    assert "1 秒的上限" in (run.error or "")
    assert time.perf_counter() - started < 10, "上限没起作用，等到了节点自己结束"


# --------------------------------------------------------------------------
# 模型调用超时
# --------------------------------------------------------------------------


def test_models_get_a_timeout_even_when_the_node_sets_none():
    provider = Provider(name="t", kind="anthropic", api_key=encrypt("sk-test"), models=[],
                        default_model="claude-sonnet-5", enabled=True, extra={})
    model = build_chat_model(provider, ModelSpec())
    assert model.default_request_timeout == settings.model_timeout_seconds
    # 节点显式配了的照旧优先
    assert build_chat_model(provider, ModelSpec(timeout=42)).default_request_timeout == 42


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


async def _events(run_id):
    async with SessionLocal() as session:
        return list((await session.execute(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )).scalars())


async def test_the_seal_covers_run_finished_and_verifies():
    run = await _run(SIMPLE)
    assert run.status == "succeeded", run.error
    events = await _events(run.id)
    finished = next(e for e in events if e.type == "run.finished")
    assert run.manifest_seq == finished.seq, "清单没有封到 run.finished"

    report = await verify_manifest(run.id)
    assert report["sealed"] and report["ok"], report


async def test_tampering_with_the_output_is_caught():
    """改成果——以前 run.finished 不在清单里，这一改清单照样对得上。"""
    run = await _run(SIMPLE)
    async with SessionLocal() as session:
        await session.execute(
            update(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.type == "run.finished")
            .values(data={"output": {"结果": "被改过的结论"}})
        )
        await session.commit()
    report = await verify_manifest(run.id)
    assert report["ok"] is False


async def test_events_appended_after_the_seal_are_not_tampering():
    """封存之后追加的事件（复核批注之类）不在范围内，不该让核对失败。"""
    run = await _run(SIMPLE)
    async with SessionLocal() as session:
        row = await session.get(Run, run.id)
        session.add(RunEvent(run_id=run.id, seq=row.manifest_seq + 5, type="log", ts=time.time(),
                             node_id=None, data={"message": "事后追加的批注"}))
        await session.commit()
    assert (await verify_manifest(run.id))["ok"] is True


async def test_runs_sealed_before_manifest_seq_existed_verify_by_the_old_cut():
    """老运行的哈希是在 run.finished 落库之前算的，没有 manifest_seq——按当时的口径核。"""
    run = await _run(SIMPLE)
    rows = [(e.seq, e.type, e.node_id, e.data) for e in await _events(run.id)
            if e.type != "run.finished"]
    async with SessionLocal() as session:
        await session.execute(update(Run).where(Run.id == run.id)
                              .values(manifest_hash=manifest_hash(rows), manifest_seq=None))
        await session.commit()
    report = await verify_manifest(run.id)
    assert report["ok"] is True and report["legacy"] is True


async def test_a_run_waiting_for_a_human_is_not_sealed_yet():
    graph = chain(
        node("start", "input", fields=[{"name": "question"}]),
        node("h", "human", mode="approve", title="批吗"),
        node("out", "output", fields=[]),
    )
    run = await _run(graph)
    assert run.status == "interrupted"
    report = await verify_manifest(run.id)
    assert report["sealed"] is False and report["ok"] is None


async def test_verify_through_the_api():
    run = await _run(SIMPLE)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.get(f"/api/runs/{run.id}/verify")
        assert ok.status_code == 200 and ok.json()["ok"] is True
        assert (await client.get("/api/runs/没有这个/verify")).status_code == 404
