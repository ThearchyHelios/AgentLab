"""旧运行的降级展示：候选只从已封存的工件里找。

- legacy_text：没有契约、没有报告的答案，把数字按数值和封存范围内的查询快照单元格、口径卡指标
  比对，每个数字最多 3 个候选，state 记 candidate，写明「猜测的来源，不能当证据」。按
  (run_id, manifest_hash) 放进内存 LRU（再带上数据源遮罩的指纹：事后加了遮罩要重算），猜测放到
  线程里跑，不堵事件循环
- 二期以前的运行没有 tool.end.query_artifact：查询快照的 id 只嵌在 tool_snapshot 的内容里，
  顺着封存事件里的 tool_snapshot（取回时复验哈希）找
- legacy_contract：旧契约 matched 的指标详情同样只从封存的口径卡里取
- 人为插进 artifacts 表的行、封存之后追加的事件指向的工件，一律不当证据
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import evidence as evidence_api
from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run, RunEvent
from app.engine import runner as runner_mod
from app.engine.evidence import GUESS_NOTE, GUESS_SCHEMA
from app.engine.runner import run_manager
from app.main import app

SOURCE = "warehouse"
TOOL = f"db_query__{SOURCE}"
SQL = "SELECT region, SUM(amount) AS gmv, COUNT(*) AS order_cnt FROM orders GROUP BY region ORDER BY gmv DESC"
ANSWER = "东区销售额 400.5，西区 200，一共 7 单。"


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
async def warehouse(tmp_path):
    path = tmp_path / "warehouse.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "east", 100.5), (2, "west", 200.0), (3, "east", 300.0)])
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.schema_cache = str(path), "sqlite", True, True, {}
        row.options = {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield source_id
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def asked(answer=ANSWER):
    return chain(node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
                 node("out", "output", fields=[{"name": "answer", "value": answer}]))


async def finish(graph) -> Run:
    run = await run_manager.start(graph=graph, input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run.id} 没跑完：{row.status}")


async def events(run_id: str, etype: str, node_id: str | None = None) -> list[RunEvent]:
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return list((await session.execute(q.order_by(RunEvent.seq))).scalars())


async def reseal(run_id: str) -> None:
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.seq <= run.manifest_seq).order_by(RunEvent.seq))]
        run.manifest_hash = artifact_store.manifest_hash(rows)
        await session.commit()


def numbers(field: dict) -> dict[str, dict]:
    return {s["text"]: s for s in field["segments"] if s["kind"] == "number"}


def counting(monkeypatch) -> list[int]:
    """数一数真正猜了几次，并记下在哪个线程里猜的。"""
    calls: list[int] = []
    real = evidence_api.guess_sources

    def spy(*args, **kwargs):
        calls.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(evidence_api, "guess_sources", spy)
    return calls


# --------------------------------------------------------------------------
# legacy_text
# --------------------------------------------------------------------------


async def test_an_answer_without_a_contract_gets_guessed_candidates(client, warehouse):
    row = await finish(asked())
    assert row.status == "succeeded", row.error
    [end] = await events(row.id, "tool.end", "fetch")
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert body["mode"] == "legacy_text" and body["reports"] == [] and body["evidence"] == []
    legacy = body["legacy"]
    assert legacy["schema"] == GUESS_SCHEMA and legacy["mode"] == "legacy_text" and legacy["note"] == GUESS_NOTE
    assert legacy["note"].startswith("猜测的来源，不能当证据") and legacy["sealed"] is True
    [field] = legacy["fields"]
    assert field["field"] == "answer" and field["markdown"] == ANSWER
    found = numbers(field)
    east = found["400.5"]
    assert east["state"] == "candidate" and len(east["candidates"]) <= 3
    top = east["candidates"][0]
    assert top["kind"] == "cell" and top["ref"] == "Q1.r0.gmv" and top["artifact"] == end.data["query_artifact"]
    assert top["node_id"] == "fetch" and top["tool"] == TOOL
    assert found["200"]["candidates"][0]["ref"] == "Q1.r1.gmv"
    assert found["7"]["state"] == "none" and "candidates" not in found["7"]
    assert legacy["stats"] == {"numbers": 3, "guessed": 2, "unguessed": 1,
                               "candidates": sum(len(s.get("candidates") or []) for s in found.values())}

    audit = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    assert audit["mode"] == "legacy_text"
    guessed = next(g for g in audit["groups"] if g["key"] == "candidate")["rows"]
    assert [(r["text"], r["field"], r["state"]) for r in guessed] == [("400.5", "answer", "candidate"),
                                                                       ("200", "answer", "candidate")]
    assert guessed[0]["evidence"].startswith("Q1.r0.gmv") and "猜测" in guessed[0]["note"]
    assert len(guessed[0]["candidates"]) == len(east["candidates"])
    unguessed = next(g for g in audit["groups"] if g["key"] == "none")["rows"]
    assert [(r["text"], r["kind"]) for r in unguessed] == [("7", "number")]


async def test_artifacts_outside_the_sealed_events_are_not_candidates(client, warehouse):
    """人为往 artifacts 表插一行（内容里正好有 7），再在封存之后追加一条指向它的 tool.end：都不认。"""
    row = await finish(asked())
    fake = await artifact_store.put_json(
        {"columns": ["n"], "rows": [[7]], "row_count": 1, "truncated": False, "sql": "SELECT 7 AS n",
         "source": SOURCE}, kind="query_snapshot", run_id=row.id, node_id="fetch")
    async with SessionLocal() as session:
        assert await session.get(Artifact, (fake, row.id)) is not None
        session.add(RunEvent(run_id=row.id, seq=row.manifest_seq + 3, type="tool.end", node_id="fetch", ts=0.0,
                             data={"tool": TOOL, "query_artifact": fake}))
        await session.commit()
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert body["seal"]["ok"] is True
    found = numbers(body["legacy"]["fields"][0])
    assert found["7"]["state"] == "none"
    assert all(c["artifact"] != fake for s in found.values() for c in s.get("candidates") or [])


async def test_guesses_are_cached_per_seal_and_run_off_the_event_loop(client, warehouse, monkeypatch):
    row = await finish(asked())
    calls = counting(monkeypatch)
    first = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    again = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    await client.get(f"/api/runs/{row.id}/evidence/audit")
    assert len(calls) == 1, "同一次封存只猜一次，证据图和审计表共用"
    assert again["legacy"] == first["legacy"]
    assert calls[0] != threading.get_ident(), "猜测要放到线程里跑"


async def test_masked_columns_are_never_candidates_even_after_caching(client, warehouse, monkeypatch):
    row = await finish(asked())
    calls = counting(monkeypatch)
    before = numbers((await client.get(f"/api/runs/{row.id}/evidence")).json()["legacy"]["fields"][0])
    assert before["400.5"]["state"] == "candidate"
    async with SessionLocal() as session:
        src = await session.get(DataSource, warehouse)
        src.options = {"mask_columns": ["gmv"]}
        await session.commit()
    after = numbers((await client.get(f"/api/runs/{row.id}/evidence")).json()["legacy"]["fields"][0])
    assert len(calls) == 2, "遮罩变了，缓存的猜测作废"
    assert after["400.5"]["state"] == "none" and after["200"]["state"] == "none"
    assert all(c["locator"]["column"] != "gmv" for s in after.values() for c in s.get("candidates") or [])


async def test_runs_from_before_phase_two_are_followed_through_the_tool_snapshot(client, warehouse, monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    row = await finish(asked())
    assert row.status == "succeeded", row.error
    [end] = await events(row.id, "tool.end", "fetch")
    assert "query_artifact" not in end.data and end.data["artifact"]
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert body["mode"] == "legacy_text"
    top = numbers(body["legacy"]["fields"][0])["400.5"]["candidates"][0]
    assert top["ref"] == "Q1.r0.gmv" and top["kind"] == "cell"
    snapshot = artifact_store.load(end.data["artifact"])
    assert top["artifact"] in snapshot["result"]


async def test_answers_without_numbers_or_sources_stay_none(client, warehouse):
    body = (await client.get(f"/api/runs/{(await finish(asked('东区最多'))).id}/evidence")).json()
    assert body["mode"] == "none" and "legacy" not in body
    plain = await finish(chain(node("start", "input"), node("out", "output", fields=[{"name": "r", "value": "共 7 单"}])))
    body = (await client.get(f"/api/runs/{plain.id}/evidence")).json()
    assert body["mode"] == "none"


# --------------------------------------------------------------------------
# legacy_contract：指标详情只从封存的口径卡里取
# --------------------------------------------------------------------------

KPI = "{'gmv': 45678.5, 'orders': 1234}"
STANDARD = [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "vars.kpi.gmv"},
            {"id": "orders", "name": "订单数", "unit": "单", "expression": "vars.kpi.orders"}]
STORY = "本周销售额 45678.5 元，订单 1234 单，另有 999 笔待核。"


def narrated():
    return chain(node("start", "input"), node("fetch", "transform", mode="expression", expression=KPI, assign_to="kpi"),
                 node("caliber", "metrics", caliber="周报口径", caliber_version="v2", metrics=STANDARD),
                 node("narr", "transform", mode="template", template=STORY, assign_to="story"),
                 node("out", "output", fields=[{"name": "r", "value": "{{ vars.story }}"}],
                      contract={"metrics_from": ["caliber"], "narrative": "{{ vars.story }}"}))


async def test_legacy_matches_name_their_sealed_metric(client):
    row = await finish(narrated())
    assert row.status == "succeeded", row.error
    [card] = [e for e in await events(row.id, "node.finished", "caliber")]
    sealed_card = next(e["artifact"] for e in card.data["evidence"] if e["kind"] == "metric_set")
    # 人为插一张同名指标的「口径卡」：值不一样，不能被拿来当出处
    await artifact_store.put_json({"kind": "metric_set", "caliber": "别的口径", "caliber_version": "v9",
                                   "metrics": [{"id": "gmv", "name": "销售额", "value": 1.0}]},
                                  kind="metric_set", run_id=row.id, node_id="caliber")
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert body["mode"] == "legacy_contract"
    gmv = next(m for m in body["legacy"]["matched"] if m["metric"] == "gmv")
    [source] = gmv["sources"]
    assert source["kind"] == "metric" and source["ref"] == "m:gmv" and source["artifact"] == sealed_card
    assert source["value"] == 45678.5 and source["rendered"] == "45,678.5元" and source["name"] == "销售额"
    assert (source["caliber"], source["version"], source["node_id"]) == ("周报口径", "v2", "caliber")
    assert source["sealed"] is True

    audit = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    assert audit["mode"] == "legacy_contract"
    guessed = next(g for g in audit["groups"] if g["key"] == "candidate")["rows"]
    assert [r["text"] for r in guessed] == ["45678.5", "1234"]
    assert guessed[0]["evidence"].startswith("m:gmv") and "按数值匹配" in guessed[0]["note"]
    bare = next(g for g in audit["groups"] if g["key"] == "none")["rows"]
    assert [r["text"] for r in bare] == ["999"]
    # 句子一栏取成果字段里这个数前后的原文（对上的数 trace_numbers 不给 context，以前整列是空的）
    for r in guessed + bare:
        assert r["field"] == "r" and r["text"] in r["sentence"] and r["sentence"] in STORY, r
    assert "销售额 45678.5 元" in guessed[0]["sentence"]


async def test_legacy_matches_without_a_sealed_card_have_no_source(client):
    row = await finish(narrated())
    async with SessionLocal() as session:
        finished = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "node.finished", RunEvent.node_id == "caliber"))).scalar_one()
        finished.data = {k: v for k, v in finished.data.items() if k not in ("evidence", "artifact")}
        await session.commit()
    await reseal(row.id)
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert body["seal"]["ok"] is True
    assert all(m["sources"] == [] for m in body["legacy"]["matched"])
