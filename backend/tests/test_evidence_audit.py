"""证据图里的表、字段、检索，和记录页审计表用的审计视图（可以导出 JSON / CSV）。

- 证据图的 evidence 列出报告用到的表、字段、知识库检索；它们算不算「已封存」要看背后的
  表结构快照、查询快照、检索快照是不是封存范围内的事件交回过的
- 审计视图把报告里每个有状态的片段按状态分组：有出处 / 无证据 / 可疑实体 / 旧运行猜测。
  没挂依据的结论句、没有可画线文字的违规（结构片段里的数字、粗体里的可疑名字）也各占一行，
  键盘用户在表里能看全
- 附上封存核对的结果；CSV 表头是中文，字段里的逗号、引号、换行按 RFC 4180 转义，
  以 = + - @ 开头的文字（不是数）前面加一个 '，免得被电子表格当成公式；每行末尾一列「封存核对」，
  文件存下来以后也看得出封存链是好的、断的还是没封存
- 没挂依据的结论句那一行说的「计入缺口」按出具时实际生效的策略（_issuance.claims）说，和出具横幅一致
"""
from __future__ import annotations

import asyncio
import csv
import io
import sqlite3
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core import artifact_store
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent
from app.engine.evidence import iter_segments
from app.engine.runner import run_manager
from app.main import app

SOURCE = "warehouse"
TOOL = f"db_query__{SOURCE}"
SQL = "SELECT region, SUM(amount) AS gmv, COUNT(*) AS order_cnt FROM orders GROUP BY region ORDER BY gmv DESC"
#: 第一句有出处（反引号里的真表名、单元格），ASCII 逗号和引号是给 CSV 转义用的；第二句有编造的名字
#: 和裸数字、没挂依据；第三句是没挂依据的结论
WRITE = ('`orders` 里东区最多, 达 [[v:Q1.r0.gmv]] "含税"。[[see:Q1]]`refund_log` 另算，另有 999 笔待核。'
         "增长主要来自新客。")
HEADER = ["分组", "报告节点", "成果字段", "片段编号", "句子编号", "种类", "原文", "状态", "问题", "引用", "证据",
          "证据种类", "证据编号", "工件", "来源节点", "已封存", "句子", "说明", "封存核对"]


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
    from app.data.introspect import introspect

    path = tmp_path / "warehouse.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL, created_at TEXT);"
        "CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)",
                   [(1, "east", 100.5, "2026-09-01"), (2, "west", 200.0, "2026-09-02"), (3, "east", 300.0, "2026-09-03")])
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


def script(monkeypatch, text):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


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


async def report_run(monkeypatch, text=WRITE) -> tuple[Run, dict]:
    script(monkeypatch, text)
    row = await finish(chain(node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
                             node("write", "report", instructions="写一句", max_repairs=0),
                             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"}])))
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked", "write")
    return row, artifact_store.load(checked.data["doc_artifact"])


def rows_of(body: dict, group: str) -> list[dict]:
    return next(g["rows"] for g in body["groups"] if g["key"] == group)


# --------------------------------------------------------------------------
# 证据图：表、字段、检索
# --------------------------------------------------------------------------


async def test_the_graph_lists_tables_and_columns(client, warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch, "`orders` 里东区最多，达 [[v:Q1.r0.gmv]]，按 [[c:orders.region]] 汇总。"
                                             "[[see:Q1]]")
    [end] = await events(row.id, "tool.end", "fetch")
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    by_alias = {e["alias"]: e for e in body["evidence"]}
    orders = by_alias["t:orders"]
    assert orders["kind"] == "table" and orders["name"] == "orders" and orders["sealed"] is True
    assert orders["artifact"] == end.data["schema_artifact"] and orders["queries"] == ["Q1"]
    assert {s["kind"] for s in orders["sources"]} == {"schema", "sql"}
    assert orders["cited_by"] == [next(s["id"] for s in iter_segments(doc) if s.get("ref") == "t:orders")]
    region = by_alias["c:orders.region"]
    assert region["kind"] == "column" and region["table"] == "orders" and region["sealed"] is True
    assert {"from": "t:orders", "to": "Q1", "rel": "appears_in", "report": "write"} in body["edges"]
    assert {"from": "c:orders.region", "to": "Q1", "rel": "appears_in", "report": "write"} in body["edges"]


async def test_the_graph_lists_retrievals_with_the_quotes_that_cite_them(client, monkeypatch):
    from app.memory import kb

    async def fake_search(session, **kw):
        return [{"chunk_id": "ch-1", "document_id": "doc-1", "title": "售后月报", "ordinal": 3, "score": 0.9,
                 "content": "九月退款主要集中在东区，原因是物流延误。"}]

    monkeypatch.setattr(kb, "search", fake_search)
    script(monkeypatch, "售后说「[[q:K1|退款主要集中在东区]]」。[[see:K1]]")
    row = await finish(chain(node("start", "input"), node("docs", "retrieve", query="退款", collection="kb"),
                             node("write", "report", instructions="引用原话"),
                             node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"}])))
    assert row.status == "succeeded", row.error
    [checked] = await events(row.id, "report.checked", "write")
    doc = artifact_store.load(checked.data["doc_artifact"])
    quote = next(s for s in iter_segments(doc) if s["kind"] == "quote")
    body = (await client.get(f"/api/runs/{row.id}/evidence")).json()
    k1 = next(e for e in body["evidence"] if e["alias"] == "K1")
    assert k1["kind"] == "retrieval" and k1["sealed"] is True and k1["source"] == "kb"
    assert k1["cited_by"] == [quote["id"]]

    audit = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    [cited] = rows_of(audit, "cited")
    assert cited["kind"] == "quote" and cited["text"] == "退款主要集中在东区" and cited["evidence_kind"] == "quote"
    assert cited["alias"] == "K1" and cited["sealed"] is True and cited["artifact"] == k1["artifact"]


# --------------------------------------------------------------------------
# 审计视图
# --------------------------------------------------------------------------


async def test_the_audit_groups_every_stateful_piece(client, warehouse, monkeypatch):
    row, doc = await report_run(monkeypatch)
    body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    assert body["run_id"] == row.id and body["schema"] == "agentlab.evidence.audit/1" and body["mode"] == "cited"
    assert [g["key"] for g in body["groups"]] == ["cited", "none", "suspicious", "candidate"]
    assert [g["label"] for g in body["groups"]] == ["有出处", "无证据", "可疑名称", "按数值猜测"]
    assert body["counts"] == {g["key"]: len(g["rows"]) for g in body["groups"]}
    assert body["total"] == sum(body["counts"].values())

    cited = rows_of(body, "cited")
    assert [(r["kind"], r["text"]) for r in cited] == [("entity", "`orders`"), ("number", "400.5")]
    cell = cited[1]
    assert cell["ref"] == "v:Q1.r0.gmv" and cell["evidence"] == "Q1.r0.gmv" and cell["evidence_kind"] == "cell"
    assert cell["node_id"] == "fetch" and cell["sealed"] is True and cell["state"] == "deterministic"
    assert cell["segment"] and cell["unit"] and doc["markdown"][slice(*cell["span"])] == "400.5"
    assert cited[0]["evidence_kind"] == "table" and cited[0]["sealed"] is True

    # 组内按在正文里的位置排：没挂依据的句子从句首算
    none = rows_of(body, "none")
    assert [(r["kind"], r["issue"]) for r in none] == [
        ("claim", "uncited_claim"), ("number", "uncited_number"), ("claim", "uncited_claim")]
    assert none[1]["text"] == "999" and none[1]["state"] == "none"
    assert [none[0]["text"], none[2]["text"]] == ["`refund_log` 另算，另有 999 笔待核。", "增长主要来自新客。"]
    assert none[0]["segment"] is None and none[0]["unit"] and none[0]["sentence"] == none[0]["text"]

    [fake] = rows_of(body, "suspicious")
    assert fake["kind"] == "entity" and fake["issue"] == "unknown_entity" and fake["text"] == "`refund_log`"
    assert "疑似不存在的名称" in fake["note"]
    assert rows_of(body, "candidate") == []

    seal = body["seal"]
    assert seal["sealed"] is True and seal["ok"] is True and seal["manifest_seq"] == row.manifest_seq
    assert seal["events"] > 0 and seal["message"] == "事件记录与封存时一致"
    [report] = body["reports"]
    assert report["node_id"] == "write" and report["hash_ok"] is True and report["doc_sealed"] is True
    assert report["claims"] == "off" and report["stats"]["unknown_entities"] == 1


async def test_violations_without_a_row_of_their_own_are_listed(client, warehouse, monkeypatch):
    """粗体里的可疑名字、列表序号里的数字没有可以画线的片段，只在违规清单里：审计表照样各给一行。"""
    row, _ = await report_run(monkeypatch, "**`refund_log`** 另算。\n\n100. 东区最多，达 [[v:Q1.r0.gmv]]。[[see:Q1]]")
    body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    [fake] = rows_of(body, "suspicious")
    assert (fake["kind"], fake["issue"], fake["text"]) == ("violation", "unknown_entity", "refund_log")
    assert fake["segment"] and "疑似不存在的名称" in fake["note"]
    [bare] = rows_of(body, "none")
    assert (bare["kind"], bare["issue"], bare["text"]) == ("violation", "uncited_number", "100")
    assert [r["text"] for r in rows_of(body, "cited")] == ["400.5"]


async def test_the_audit_can_show_only_some_groups(client, warehouse, monkeypatch):
    row, _ = await report_run(monkeypatch)
    body = (await client.get(f"/api/runs/{row.id}/evidence/audit", params={"groups": "none,suspicious"})).json()
    assert [g["key"] for g in body["groups"]] == ["none", "suspicious"]
    assert body["total"] == 4
    bad = await client.get(f"/api/runs/{row.id}/evidence/audit", params={"groups": "none,nope"})
    assert bad.status_code == 422 and bad.json()["code"] == "evidence_bad_group"


async def test_the_audit_exports_json_and_csv(client, warehouse, monkeypatch):
    row, _ = await report_run(monkeypatch)
    plain = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()

    as_json = await client.get(f"/api/runs/{row.id}/evidence/audit", params={"format": "json"})
    assert as_json.status_code == 200 and as_json.json() == plain
    assert "attachment" in as_json.headers["content-disposition"]
    assert as_json.headers["content-disposition"].endswith('.json"')

    as_csv = await client.get(f"/api/runs/{row.id}/evidence/audit", params={"format": "csv"})
    assert as_csv.status_code == 200 and as_csv.headers["content-type"].startswith("text/csv")
    assert as_csv.headers["content-disposition"].endswith('.csv"')
    assert as_csv.headers["x-evidence-seal"] == "ok"
    raw = as_csv.content.decode("utf-8")
    assert raw.startswith("﻿"), "Excel 要 BOM 才认 UTF-8"
    table = list(csv.reader(io.StringIO(raw[1:])))
    assert table[0] == HEADER
    flat = [r for g in plain["groups"] for r in g["rows"]]
    assert len(table) - 1 == len(flat) == plain["total"]
    cell = next(line for line in table[1:] if line[6] == "400.5")
    assert cell[0] == "有出处" and cell[7] == "有出处" and cell[15] == "是" and cell[10] == "Q1.r0.gmv"
    # 句子里的 ASCII 逗号和双引号原样读回来
    assert cell[16] == '`orders` 里东区最多, 达 400.5 "含税"。'
    fake = next(line for line in table[1:] if line[8] == "疑似不存在的名称")
    assert fake[0] == "可疑名称" and fake[6] == "`refund_log`"
    # 存下来的文件自己带着封存核对的结果（响应头离了浏览器就没了）
    assert {line[18] for line in table[1:]} == {f"封存核对通过（封存于第 {row.manifest_seq} 条事件）"}

    bad = await client.get(f"/api/runs/{row.id}/evidence/audit", params={"format": "xlsx"})
    assert bad.status_code == 422 and bad.json()["code"] == "evidence_bad_format"


def test_csv_escapes_commas_quotes_newlines_and_formulas():
    from app.api.evidence import audit_csv

    rows = [{"group": "none", "text": 'a,b "c"\nd', "sentence": "=HYPERLINK(\"x\")", "note": "-3.2%",
             "evidence": "+cmd", "ref": "@x", "sealed": None, "kind": "claim"}]
    raw = audit_csv(rows)
    assert raw.startswith("﻿") and "\r\n" in raw
    [header, line] = list(csv.reader(io.StringIO(raw[1:])))
    assert header == HEADER
    got = dict(zip(header, line))
    assert got["原文"] == 'a,b "c"\nd'
    assert got["句子"] == "'=HYPERLINK(\"x\")" and got["证据"] == "'+cmd" and got["引用"] == "'@x"
    assert got["说明"] == "-3.2%", "负数不是公式，不加引号"
    assert got["已封存"] == "—" and got["分组"] == "无证据" and got["种类"] == "结论句"
    assert got["封存核对"] == "", "不给封存结论就空着，不瞎填"
    [_, line] = list(csv.reader(io.StringIO(audit_csv(rows, seal="=封存核对没通过")[1:])))
    assert line[-1] == "'=封存核对没通过", "封存那一格同样过公式防护"


async def test_a_broken_seal_marks_every_row_unsealed(client, warehouse, monkeypatch):
    row, _ = await report_run(monkeypatch)
    async with SessionLocal() as session:
        started = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "node.started", RunEvent.node_id == "start"))).scalar_one()
        started.data = {**(started.data or {}), "note": "事后改过"}
        await session.commit()
    body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    assert body["seal"]["ok"] is False and "不一致" in body["seal"]["message"]
    assert all(r["sealed"] is False for r in rows_of(body, "cited"))
    as_csv = await client.get(f"/api/runs/{row.id}/evidence/audit", params={"format": "csv"})
    assert as_csv.headers["x-evidence-seal"] == "broken"
    table = list(csv.reader(io.StringIO(as_csv.content.decode("utf-8")[1:])))
    assert {line[15] for line in table[1:] if line[0] == "有出处"} == {"否"}
    assert {line[18] for line in table[1:]} == {"封存核对未通过：封存后有事件被修改、删除或插入"}


async def test_a_run_waiting_for_approval_is_audited_as_unsealed(client, warehouse, monkeypatch):
    script(monkeypatch, WRITE)
    run = await run_manager.start(graph=chain(
        node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
        node("write", "report", instructions="写一句", max_repairs=0),
        node("gate", "human", mode="approve", title="确认"),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"}])), input_payload={})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("interrupted", "failed"):
            break
    assert row.status == "interrupted", row.error
    body = (await client.get(f"/api/runs/{run.id}/evidence/audit")).json()
    assert body["mode"] == "cited" and body["seal"]["sealed"] is False and body["seal"]["ok"] is None
    assert rows_of(body, "cited") and all(r["sealed"] is False for r in rows_of(body, "cited"))
    as_csv = await client.get(f"/api/runs/{run.id}/evidence/audit", params={"format": "csv"})
    assert as_csv.headers["x-evidence-seal"] == "unsealed"
    table = list(csv.reader(io.StringIO(as_csv.content.decode("utf-8")[1:])))
    assert len(table) > 1 and {line[18] for line in table[1:]} == {"未封存：运行尚未结束，或正停在人工审批"}


CLAIMED = "`orders` 里东区最多，达 [[m:gmv]]。[[see:m:gmv]]增长主要来自新客。"


async def claimed_run(monkeypatch, *, report=None, contract=None) -> Run:
    """一句有出处、一句没挂依据的结论；contract 为 None 时出口不带契约。"""
    script(monkeypatch, CLAIMED)
    card = node("card", "metrics", caliber="周报口径", caliber_version="v1", metrics=[
        {"id": "gmv", "name": "销售额", "decimals": 1, "expression": "cell(nodes.fetch, 0, 'gmv')"}])
    out = node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"}],
               **({"contract": {"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], **contract}}
                  if contract is not None else {}))
    row = await finish(chain(node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}), card,
                             node("write", "report", instructions="写一句", max_repairs=0, **(report or {})), out))
    assert row.status == "succeeded", row.error
    return row


async def test_uncited_claims_say_what_issuance_did_with_them(client, warehouse, monkeypatch):
    """结论句那一行说的「计入缺口」要和出具横幅一致：按出具时实际生效的策略（_issuance.claims）说，
    不只看报告节点自己写的 claims。"""
    from app.api.evidence import UNCITED_CLAIM, UNCITED_CLAIM_COUNTED, UNCITED_CLAIM_REQUIRED, UNCITED_CLAIM_WITHHELD

    async def claim_notes(row: Run) -> list[str]:
        body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
        return [r["note"] for r in rows_of(body, "none") if r["kind"] == "claim"]

    # 报告节点没写、契约写了 require_citation：出具因为这句降档，审计表也得说计入缺口
    row = await claimed_run(monkeypatch, contract={"claims": "require_citation"})
    assert row.output["_issuance"]["tier"] == "degraded"
    assert await claim_notes(row) == [UNCITED_CLAIM_COUNTED]
    # 契约收紧成 withhold：说的是不予出具
    row = await claimed_run(monkeypatch, contract={"claims": {"policy": "require_citation", "on_uncited": "withhold"}})
    assert row.output["_issuance"]["tier"] == "withheld"
    assert await claim_notes(row) == [UNCITED_CLAIM_WITHHELD]
    # 探索运行里契约自己写 ignore：出具没算它，审计表也不说计入缺口
    row = await claimed_run(monkeypatch, contract={"claims": {"policy": "require_citation", "on_uncited": "ignore"}})
    assert row.output["_issuance"]["tier"] == "formal"
    assert await claim_notes(row) == [UNCITED_CLAIM]
    # 报告节点要求了、契约的 ignore 放不松：计入缺口
    row = await claimed_run(monkeypatch, report={"claims": "require_citation"},
                            contract={"claims": {"policy": "require_citation", "on_uncited": "ignore"}})
    assert row.output["_issuance"]["tier"] == "degraded"
    assert await claim_notes(row) == [UNCITED_CLAIM_COUNTED]
    # 报告节点要求了、但出口没有契约：没有哪次出具按它判档，不能说「出具时计入缺口」
    row = await claimed_run(monkeypatch, report={"claims": "require_citation"})
    assert "_issuance" not in (row.output or {})
    assert await claim_notes(row) == [UNCITED_CLAIM_REQUIRED]
    # 都没要求
    row = await claimed_run(monkeypatch, contract={})
    assert await claim_notes(row) == [UNCITED_CLAIM]


async def test_the_contract_policy_only_speaks_for_the_report_it_checked(client, warehouse, monkeypatch):
    """两份报告，契约只核对 write：另一份（side）没挂依据的结论句不能跟着说「出具时计入缺口」。"""
    from app.api.evidence import UNCITED_CLAIM, UNCITED_CLAIM_COUNTED

    script(monkeypatch, CLAIMED)
    card = node("card", "metrics", caliber="周报口径", caliber_version="v1", metrics=[
        {"id": "gmv", "name": "销售额", "decimals": 1, "expression": "cell(nodes.fetch, 0, 'gmv')"}])
    row = await finish(chain(
        node("start", "input"), node("fetch", "tool", tool=TOOL, args={"sql": SQL}), card,
        node("write", "report", instructions="写一句", max_repairs=0),
        node("side", "report", instructions="再写一句", max_repairs=0),
        node("out", "output", fields=[{"name": "r", "value": "{{ nodes.write.text }}"},
                                      {"name": "s", "value": "{{ nodes.side.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "claims": "require_citation"})))
    assert row.status == "succeeded", row.error
    body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    notes = {r["report"]: r["note"] for r in rows_of(body, "none") if r["kind"] == "claim"}
    assert notes == {"write": UNCITED_CLAIM_COUNTED, "side": UNCITED_CLAIM}


async def test_runs_without_evidence_say_so(client):
    row = await finish(chain(node("start", "input", fields=[{"name": "q", "default": "x"}]),
                             node("out", "output", fields=[{"name": "r", "value": "{{ input.q }}"}])))
    body = (await client.get(f"/api/runs/{row.id}/evidence/audit")).json()
    assert body["mode"] == "none" and body["total"] == 0 and body["note"]
    resp = await client.get("/api/runs/no-such-run/evidence/audit")
    assert resp.status_code == 404 and resp.json()["code"] == "run_not_found"
