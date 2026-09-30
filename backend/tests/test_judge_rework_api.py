"""结论句裁判改造（JR）的接口：片段接口的字段清单、按需裁判的模型标记、证据图的 judge_meta、审计行。

- 片段接口里表名实体那一步带上字段清单 fields: [{name, type}]（封存范围内的表结构快照，最多 60 个，
  多出来的记 fields_more），遮罩的列不列
- 按需裁判也判断裁判模型和写作模型是否相同（写作模型取这份报告撰写节点实际用的模型），并带上
  priced、writer_model；evidence.judged 事件同样记下
- GET /evidence 每份报告带 judge_meta：最近一次按需裁判的 {model, same_model, priced, writer_model}
- 交给裁判的新摘录（字段清单、指标输入的 SQL）只经封存范围内的 loader 读
- 审计行的 issue 新增 contradicted_claim、insufficient_claim
- 旧的 unsupported 判定照旧算「判过」：封存之后追加的判定盖不掉它

所有裁判都用 mock 模型给剧本，不调真接口。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app.core import artifact_store
from app.core.events import EventType
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Setting
from app.engine import judge as judge_mod
from app.engine.evidence import iter_segments, iter_units
from app.engine.judge import JUDGE_ROLE, SPEND_KEY
from app.engine.runner import run_manager
from app.main import app
from app.providers.mock_model import MockChatModel

PRICED = "claude-sonnet-5"
WRITER = "mock-fast"
SOURCE = "jr_shop"
TOOL = f"db_query__{SOURCE}"
SQL = "SELECT region, SUM(amount) AS gmv FROM orders GROUP BY region ORDER BY gmv DESC"
WIDE_COLS = [f"f{i:02d}" for i in range(65)]
METRIC = "东区销售额最高，达 [[m:gmv]]。[[see:m:gmv]]"
METHOD = "这个数由 [[t:orders]] 按 [[c:orders.region]] 分组求和得出。"
WIDE = "明细在 [[t:wide_orders]] 里。"
TEXT = METRIC + METHOD + WIDE


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    """探查过结构的示例库：orders（带手机号列，数据源设了遮罩）和一张 65 列的宽表。"""
    from app.data.introspect import introspect

    path = tmp_path / "jr_shop.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL, phone TEXT);"
        f"CREATE TABLE wide_orders ({', '.join(f'{c} TEXT' for c in WIDE_COLS)});")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)",
                   [(1, "east", 300.5, "p1"), (2, "west", 200.0, "p2"), (3, "east", 100.0, "p3")])
    db.commit()
    db.close()
    probe = SimpleNamespace(id=f"probe-{path.name}", name=SOURCE, kind="sqlite", database=str(path), host=None,
                            port=None, username=None, password=None, options={}, readonly=True, schema_cache={})
    cache = await introspect(probe)
    await engines.invalidate(probe.id)
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.schema_cache = str(path), "sqlite", True, True, cache
        row.options = {"mask_columns": "phone"}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    yield source_id
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(**report) -> dict:
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
        node("card", "metrics", caliber="周报口径", caliber_version="v2", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", instructions="写周报", on_violation="flag", claims="judge", **report),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}]),
    ]
    ids = [n["id"] for n in nodes]
    return {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}


class Models:
    """写作者回 text；裁判按句子里的字给判定：verdicts = {句中片段: (判定, missing)}。judges 记下请求原文。"""

    def __init__(self, monkeypatch, verdicts=None, *, model=PRICED, text=TEXT):
        self.judges: list[str] = []
        verdicts = verdicts or {}

        def decide(chat, messages):
            if JUDGE_ROLE in str(messages[0].content):
                request = str(messages[-1].content)
                self.judges.append(request)
                items = []
                for uid, line in re.findall(r"^\[(u\d+)\]([^\n]*)$", request, re.M):
                    status, missing = next((v for frag, v in verdicts.items() if frag in line), ("supported", ""))
                    items.append({"unit": uid, "verdict": status, "rationale": f"判的是：{line.strip()[:12]}",
                                  "missing": missing, "used": []})
                return AIMessage(content=json.dumps({"items": items}, ensure_ascii=False))
            return AIMessage(content=text)

        monkeypatch.setattr(MockChatModel, "_decide", decide)
        self.model = model

        async def judge_model_of(session, spec):
            return MockChatModel(model_name=self.model), self.model

        monkeypatch.setattr(judge_mod, "get_chat_model", judge_model_of)


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def explore_run(graph=None) -> tuple[Run, dict]:
    started = await run_manager.start(graph=graph or weekly(), input_payload={}, run_class="exploratory")
    row = await wait(started.id)
    assert row.status == "succeeded", row.error
    return row, artifact_store.load(row.output["_evidence"]["doc_artifact"])


def unit_of(doc: dict, prefix: str) -> str:
    md = doc["markdown"]
    return next(u["id"] for _, u in iter_units(doc) if md[u["span"][0]:u["span"][1]].strip().startswith(prefix))


async def judged_events(run_id: str) -> list[RunEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == EventType.EVIDENCE_JUDGED).order_by(RunEvent.seq))).scalars())


async def ask(client, run_id: str, units: list[str]) -> dict:
    res = await client.post(f"/api/runs/{run_id}/evidence/judge", json={"units": units})
    assert res.status_code == 200, res.text
    return res.json()


async def reseal(run_id: str) -> None:
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        rows = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.seq <= run.manifest_seq).order_by(RunEvent.seq))]
        run.manifest_hash = artifact_store.manifest_hash(rows)
        await session.commit()


async def rewrite(run_id: str, etype: str, node_id: str, change) -> None:
    async with SessionLocal() as session:
        for row in (await session.execute(select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.type == etype, RunEvent.node_id == node_id))).scalars():
            row.data = change(dict(row.data))
        await session.commit()


# --------------------------------------------------------------------------
# 片段接口：表名实体步骤的字段清单
# --------------------------------------------------------------------------


async def open_entity(client, run_id: str, doc: dict, ref: str) -> dict:
    seg = next(s for s in iter_segments(doc) if s.get("kind") == "entity" and s.get("ref") == ref)
    res = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg['id']}")
    assert res.status_code == 200, res.text
    [step] = res.json()["chain"]
    return step


async def test_a_table_step_lists_its_fields_without_masked_columns(client, monkeypatch):
    Models(monkeypatch)
    row, doc = await explore_run()
    step = await open_entity(client, row.id, doc, "t:orders")
    assert step["step"] == "entity" and step["kind"] == "table"
    assert step["fields"] == [{"name": "id", "type": "INTEGER"}, {"name": "region", "type": "TEXT"},
                              {"name": "amount", "type": "REAL"}], "遮罩的 phone 不列"
    assert "fields_more" not in step and step["columns"] == 4


async def test_a_wide_table_step_stops_at_sixty_fields(client, monkeypatch):
    Models(monkeypatch)
    row, doc = await explore_run()
    step = await open_entity(client, row.id, doc, "t:wide_orders")
    assert [f["name"] for f in step["fields"]] == WIDE_COLS[:60]
    assert step["fields_more"] == 5


async def test_a_table_step_outside_the_seal_has_no_fields(client, monkeypatch):
    Models(monkeypatch)
    row, doc = await explore_run()
    await rewrite(row.id, "tool.end", "fetch", lambda d: {k: v for k, v in d.items() if k != "schema_artifact"})
    await rewrite(row.id, "node.finished", "fetch",
                  lambda d: {**d, "evidence": [e for e in d.get("evidence") or [] if e.get("kind") != "schema"]})
    await reseal(row.id)
    step = await open_entity(client, row.id, doc, "t:orders")
    assert "fields" not in step and "fields_more" not in step


# --------------------------------------------------------------------------
# 按需裁判：新的判定、摘录、模型标记
# --------------------------------------------------------------------------


async def test_on_demand_judging_sees_fields_and_metric_sql_and_reports_the_models(client, monkeypatch):
    models = Models(monkeypatch, verdicts={"分组求和": ("insufficient", "字段清单"), "东区销售额": ("contradicted", "")})
    row, doc = await explore_run()
    method, metric = unit_of(doc, "这个数由"), unit_of(doc, "东区销售额")

    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert graph["reports"][0]["judge_meta"] is None, "还没按需裁判过"

    body = await ask(client, row.id, [method, metric])
    request = models.judges[0]
    assert "region TEXT" in request and "id INTEGER" in request, "表附上字段清单和类型"
    assert "phone" not in request.lower(), "遮罩的列不出现"
    assert "查询 Q1 的 SQL" in request and "SUM(amount)" in request and "r0" in request, "指标附上输入的查询和格子"

    assert body["verdicts"][method]["status"] == "insufficient" and body["verdicts"][method]["missing"] == "字段清单"
    assert body["verdicts"][metric]["status"] == "contradicted"
    assert body["same_model"] is False and body["priced"] is True and body["writer_model"] == WRITER

    [event] = await judged_events(row.id)
    assert event.data["same_model"] is False and event.data["priced"] is True
    assert event.data["writer_model"] == WRITER

    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert graph["reports"][0]["judge_meta"] == {"model": PRICED, "same_model": False, "priced": True,
                                                 "writer_model": WRITER}


async def test_judging_with_the_writer_model_is_flagged(client, monkeypatch):
    models = Models(monkeypatch, model=WRITER)
    row, doc = await explore_run()
    body = await ask(client, row.id, [unit_of(doc, "这个数由")])
    assert models.judges and body["model"] == WRITER
    assert body["same_model"] is True and body["writer_model"] == WRITER, "判定照常出，另带标记"
    assert body["verdicts"][unit_of(doc, "这个数由")]["status"] == "supported"
    [event] = await judged_events(row.id)
    assert event.data["same_model"] is True and event.data["writer_model"] == WRITER

    # judge_meta 取最近一次：换一个价格目录里没有的模型再判一句，就按新的这次
    models.model = "house-judge-model"
    body = await ask(client, row.id, [unit_of(doc, "东区销售额")])
    assert body["same_model"] is False and body["priced"] is False, "目录里没有价格的模型：金额上限对它不生效"
    meta = (await client.get(f"/api/runs/{row.id}/evidence")).json()["reports"][0]["judge_meta"]
    assert meta == {"model": "house-judge-model", "same_model": False, "priced": False, "writer_model": WRITER}


async def test_the_new_excerpts_only_read_sealed_artifacts(client, monkeypatch):
    """表结构快照、查询快照被挪出封存范围（事件里追不到了）：字段清单、指标输入的 SQL 都不进裁判的摘录。"""
    models = Models(monkeypatch)
    row, doc = await explore_run()
    await rewrite(row.id, "tool.end", "fetch",
                  lambda d: {k: v for k, v in d.items() if k not in ("schema_artifact", "query_artifact")})
    await rewrite(row.id, "node.finished", "fetch",
                  lambda d: {**d, "evidence": [e for e in d.get("evidence") or []
                                               if e.get("kind") not in ("schema", "query")]})
    await reseal(row.id)
    await ask(client, row.id, [unit_of(doc, "这个数由"), unit_of(doc, "东区销售额")])
    request = models.judges[0]
    assert "INTEGER" not in request and "region TEXT" not in request
    assert "SELECT" not in request


# --------------------------------------------------------------------------
# 审计行
# --------------------------------------------------------------------------


async def test_audit_rows_list_contradicted_and_insufficient_claims(client, monkeypatch):
    Models(monkeypatch, verdicts={"分组求和": ("insufficient", "字段清单"), "东区销售额": ("contradicted", "")})
    row, doc = await explore_run()
    method, metric = unit_of(doc, "这个数由"), unit_of(doc, "东区销售额")
    await ask(client, row.id, [method, metric])

    body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    rows = [r for g in body["groups"] for r in g["rows"] if r["kind"] == "claim"]
    by_issue = {r["issue"]: r for r in rows}
    assert by_issue["contradicted_claim"]["unit"] == metric and by_issue["contradicted_claim"]["group"] == "none"
    assert by_issue["insufficient_claim"]["unit"] == method
    assert "字段清单" in by_issue["insufficient_claim"]["note"]
    assert by_issue["insufficient_claim"]["verdict"]["missing"] == "字段清单"
    assert by_issue["contradicted_claim"]["verdict"]["post_seal"] is True

    text = (await client.get(f"/api/runs/{row.id}/evidence/audit?format=csv")).text
    issues = {line[8] for line in csv.reader(io.StringIO(text.lstrip("﻿")))}
    assert {"证据相矛盾", "证据不足"} <= issues


# --------------------------------------------------------------------------
# 旧的 unsupported：照旧算判过
# --------------------------------------------------------------------------


def test_an_old_unsupported_verdict_still_counts_as_judged():
    from app.api.evidence import _effective_verdicts, _on_demand, _Report, _Sealed
    from app.engine.evidence import compose_doc

    doc = compose_doc("增长主要来自新客首单。", {}, node_id="write", run_id="r1")
    [unit] = [u for _, u in iter_units(doc)]
    unit["verdict"] = {"status": "unsupported", "rationale": "证据不支持", "judge": PRICED, "post_seal": False,
                       "used": []}
    report = _Report(node_id="write", doc_artifact="d" * 64, ok=True, repairs=0, stats={}, doc=doc, hash_ok=True,
                     fields=[], claims="judge")
    later = {"report": "write", "doc_artifact": "d" * 64,
             "verdicts": {unit["id"]: {"status": "supported", "rationale": "有依据", "judge": PRICED}}}
    sealed = _Sealed(run_id="r1", graph={}, seal={"sealed": True, "ok": True, "manifest_seq": 5, "legacy": False},
                     events=[], judged=[(9, "write", later)], run_class="exploratory", status="succeeded",
                     ledger_on=True)
    effective, appended = _effective_verdicts(report, sealed)
    assert effective[unit["id"]]["status"] == "unsupported" and appended == {}, "追加的判定盖不掉封存的判定"
    assert _on_demand(sealed, doc, unit["id"], effective[unit["id"]])["reason"] == "judged"


def test_the_writer_model_of_an_old_report_comes_from_the_sealed_node_output():
    """改造前的文档裁判摘要里没有 writer_model、改造前的事件里没有 same_model：按报告撰写节点封存的产出里
    实际用的模型补上。"""
    from app.api.evidence import _judge_meta, _Report, _Sealed, _writer_model
    from app.engine.evidence import compose_doc

    text = artifact_store.canonical_json({"text": "增长主要来自新客首单。", "model": "writer-old"})
    output = artifact_store.content_hash(text)
    path = artifact_store._path_of(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    doc = compose_doc("增长主要来自新客首单。", {}, node_id="write", run_id="r2")
    doc["judge"] = {"mode": "on_demand", "model": None, "counts": {}}
    report = _Report(node_id="write", doc_artifact="e" * 64, ok=True, repairs=0, stats={}, doc=doc, hash_ok=True,
                     fields=[], claims="judge")
    old_event = {"report": "write", "doc_artifact": "e" * 64, "model": "writer-old", "priced": False,
                 "verdicts": {}}
    sealed = _Sealed(run_id="r2", graph={}, seal={"sealed": True, "ok": True, "manifest_seq": 5, "legacy": False},
                     events=[(3, "node.finished", "write", {"artifact": output})], outputs={"write": output},
                     judged=[(9, "write", old_event)], run_class="exploratory", status="succeeded", ledger_on=True)
    assert _writer_model(sealed, report) == "writer-old"
    assert _judge_meta(report, sealed) == {"model": "writer-old", "same_model": True, "priced": False,
                                           "writer_model": "writer-old"}


# --------------------------------------------------------------------------
# 按需裁判：早于当前规则的判定可以重判，当前规则下判过的照旧复用
# --------------------------------------------------------------------------


def on_demand_of(verdict: dict) -> dict:
    from app.api.evidence import _on_demand, _Sealed
    from app.engine.evidence import compose_doc

    doc = compose_doc("增长主要来自新客首单。", {}, node_id="write", run_id="r3")
    [unit] = [u for _, u in iter_units(doc)]
    sealed = _Sealed(run_id="r3", graph={}, seal={"sealed": True, "ok": True, "manifest_seq": 5, "legacy": False},
                     events=[], run_class="exploratory", status="succeeded", ledger_on=True)
    return _on_demand(sealed, doc, unit["id"], verdict)


@pytest.mark.parametrize("verdict", [
    {"status": "supported", "rationale": "有依据", "post_seal": True},
    {"status": "unsupported", "rationale": "证据不支持", "post_seal": True},
    {"status": "insufficient", "rationale": "缺 SQL", "post_seal": True, "rule_version": 1},
])
def test_an_outdated_appended_verdict_can_be_asked_again(verdict):
    got = on_demand_of(verdict)
    assert got["available"] is True and got["reason"] == "outdated" and got["message"]


def test_a_current_verdict_is_not_asked_again():
    got = on_demand_of({"status": "contradicted", "rationale": "对不上", "post_seal": True,
                        "rule_version": judge_mod.RULES_VERSION})
    assert got["available"] is False and got["reason"] == "judged"


def test_a_sealed_verdict_is_never_reopened_by_version():
    """封存的判定（文档里的，post_seal 为 False）盖不掉：版本再旧也不开放重判。"""
    got = on_demand_of({"status": "unsupported", "rationale": "证据不支持", "post_seal": False})
    assert got["available"] is False and got["reason"] == "judged"


async def age(run_id: str, uid: str, **changes) -> None:
    """把封存之后追加的那条判定改成改造前的样子（封存之后的事件不在封存核对范围里）。"""
    async with SessionLocal() as session:
        for row in (await session.execute(select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.type == EventType.EVIDENCE_JUDGED))).scalars():
            data = json.loads(json.dumps(row.data))
            if uid in data.get("verdicts", {}):
                verdict = {k: v for k, v in data["verdicts"][uid].items() if k != "rule_version"}
                data["verdicts"][uid] = {**verdict, **changes}
                row.data = data
        await session.commit()


async def segment_of(client, run_id: str, doc: dict, uid: str) -> dict:
    seg = next(s["id"] for _, u in iter_units(doc) if u["id"] == uid for s in u["segments"])
    res = await client.get(f"/api/runs/{run_id}/evidence/segments/{seg}")
    assert res.status_code == 200, res.text
    return res.json()["unit"]


async def test_an_outdated_verdict_is_really_judged_again(client, monkeypatch):
    models = Models(monkeypatch, verdicts={"东区销售额": ("contradicted", "")})
    row, doc = await explore_run()
    uid = unit_of(doc, "东区销售额")
    first = await ask(client, row.id, [uid])
    assert first["judged"] == [uid] and len(models.judges) == 1
    assert first["verdicts"][uid]["rule_version"] == judge_mod.RULES_VERSION

    # 当前规则下判过的：照旧复用，不再调用
    again = await ask(client, row.id, [uid])
    assert again["reused"] == [uid] and again["judged"] == [] and len(models.judges) == 1
    assert (await segment_of(client, row.id, doc, uid))["on_demand"]["reason"] == "judged"

    # 改造前判的（旧取值、没有规则版本）：按钮照常出现，POST 真的重判一次，新判定追加在封存之后
    await age(row.id, uid, status="unsupported")
    unit = await segment_of(client, row.id, doc, uid)
    assert unit["verdict"]["status"] == "unsupported"
    assert unit["on_demand"]["available"] is True and unit["on_demand"]["reason"] == "outdated"
    redo = await ask(client, row.id, [uid])
    assert len(models.judges) == 2 and redo["judged"] == [uid] and redo["reused"] == []
    assert redo["verdicts"][uid]["status"] == "contradicted" and redo["event"] is not None
    assert len(await judged_events(row.id)) == 2
    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    latest = graph["reports"][0]["post_seal_verdicts"][uid]
    assert latest["status"] == "contradicted" and latest["rule_version"] == judge_mod.RULES_VERSION
    assert (await segment_of(client, row.id, doc, uid))["on_demand"]["reason"] == "judged"


async def test_a_failed_rejudge_keeps_the_older_verdict_in_force(client, monkeypatch):
    """重判没判成（模型不可用）：旧判定照旧生效，答复和证据图说的一样；按钮还在，可以再点。"""
    models = Models(monkeypatch)
    row, doc = await explore_run()
    uid = unit_of(doc, "东区销售额")
    await ask(client, row.id, [uid])
    await age(row.id, uid)

    async def broken(session, spec):
        raise RuntimeError("网关 502")

    monkeypatch.setattr(judge_mod, "get_chat_model", broken)
    body = await ask(client, row.id, [uid])
    assert body["judged"] == [] and "网关 502" in body["message"]
    assert body["verdicts"][uid]["status"] == "supported" and "rule_version" not in body["verdicts"][uid]
    graph = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    assert graph["reports"][0]["post_seal_verdicts"][uid] == body["verdicts"][uid]
    assert (await segment_of(client, row.id, doc, uid))["on_demand"]["reason"] == "outdated"
    assert len(models.judges) == 1
