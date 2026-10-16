"""口径账本的读接口（next-round 02 做法 3、9、10）：GET /api/runs/{id}/ledger、证据片段的 ledger 键、报告导出的「口径」一步、
导出留痕、GET /api/ledger/stats。

- 比对过的运行：标签、整题判断、每条查询的比对，写明封存后比对、不在封存范围内；工作流名按现在的取；
- 没比对过的：能比的排上按需比对（pending → 比完 trigger=on_demand、写明按现在的口径回看），不比的照实说为什么；
- 遮罩：绑定的值是这次运行查到过的遮罩值时不给值，差异条目里的 SQL 片段换成「已遮罩」，整份答复里找不到那个号码；
- 证据片段：比对过的带 ledger 键，没比对过的没有这个键；
- 报告导出：数据块带同一份标签和那句说明；没比对过的没有这个键；每次导出（报告、可验证报告）各记一条审计 report.exported；
- 统计：E1–E4 的口径和夹具上手算的一致，skipped 单独计。
"""
from __future__ import annotations

import json
import re

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import AuditEvent, Workflow
from app.engine.runner import run_manager
from app.ledger import match as ledger_match
from app.main import app
from tests import ledger_samples as S


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


async def ledger_of(client, run_id: str) -> dict:
    res = await client.get(f"/api/runs/{run_id}/ledger")
    assert res.status_code == 200, res.text
    return res.json()


def first_segment(row, text: str) -> str:
    from app.core.artifact_store import load
    from app.engine.evidence import iter_segments

    doc = load(row.output["_evidence"]["doc_artifact"])
    return next(s["id"] for s in iter_segments(doc) if s.get("text") == text and s.get("cite"))


async def test_a_matched_run_gives_labels_and_the_whole_judgement(client, monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    body = await ledger_of(client, row.id)
    assert body["state"] == "matched" and body["note"] == "封存后比对，不在封存范围内" and body["post_seal"] is True
    assert body["trigger"] == "sealed" and body["retro"] is False and "retro_note" not in body
    # 第 3 波起整题等价时有「按口径出具」这一项（详细的在 tests/test_ledger_issue.py）
    assert [a["kind"] for a in body["actions"]] == ["issue"] and body["primary"] == "write"
    [caliber] = body["calibers"]
    assert caliber["workflow_name"] == "周销售" and caliber["level_text"] == "受管" and caliber["caliber"] == f"{wf}@1"
    [label] = body["reports"]["write"]["labels"].values()
    assert label["kind"] == "caliber" and label["how"] == "independent" and label["workflow_name"] == "周销售"
    # 工件是规范 JSON（键排好序），绑定按字段名的先后给
    assert label["binding"] == [{"field": "end", "value": "2026-09-08"}, {"field": "start", "value": "2026-09-01"}]
    assert body["whole"]["equivalent"] is True and body["whole"]["missing"] == ["store"]
    [query] = body["queries"]
    assert query["status"] == "proven" and query["candidates"][0]["status"] == "proven"
    # 工作流改了名：按现在的名字给
    async with SessionLocal() as session:
        (await session.get(Workflow, wf)).name = "周销售（新）"
        await session.commit()
    assert (await ledger_of(client, row.id))["calibers"][0]["workflow_name"] == "周销售（新）"


async def test_an_unmatched_run_is_matched_on_demand_and_says_it_looks_back(client, monkeypatch):
    await S.publish()
    real = ledger_match.schedule
    monkeypatch.setattr(ledger_match, "schedule", lambda run_id, **kw: False)
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    monkeypatch.setattr(ledger_match, "schedule", real)
    body = await ledger_of(client, row.id)
    assert body["state"] == "pending"
    await ledger_match.drain()
    body = await ledger_of(client, row.id)
    assert body["state"] == "matched" and body["trigger"] == "on_demand" and body["retro"] is True
    assert body["retro_note"] == "按现在的口径回看"


async def test_runs_that_are_not_compared_say_why(client, monkeypatch):
    row = await S.explore(monkeypatch, S.EQUIVALENT, origin="api", text="本周销售额 [[v:Q1.r0.销售额]]。")
    body = await ledger_of(client, row.id)
    assert body["state"] == "ineligible" and body["reason"] == ledger_match.NOT_EXPLORE
    res = await client.get("/api/runs/nope/ledger")
    assert res.status_code == 404 and res.json()["code"] == "run_not_found"


async def test_masked_binding_values_and_sql_fragments_are_withheld(client, monkeypatch, engine_up):
    """口径按手机号筛（{{ input.phone }}）；这次运行先查过手机号（数据源设了遮罩），再按其中一个号码筛：绑定的值是遮罩值，
    不给值；差异条目、逐行对照里的那个号码也换成「已遮罩」。"""
    await S.set_source(engine_up, options={"mask_columns": ["phone"]})
    template = "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND phone = '{{ input.phone }}'"
    await S.publish(graph=S.caliber_graph(template))
    graph = S.chain(
        S.node("start", "input"),
        S.node("p", "tool", tool=S.tool(), args={"sql": "SELECT phone FROM orders ORDER BY id"}),
        S.node("q", "tool", tool=S.tool(),
               args={"sql": "SELECT SUM(amount) AS gmv FROM orders WHERE phone = '13800000001'"}),
        S.node("write", "report", instructions="写答案"),
        S.node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]))
    S.script(monkeypatch, "这位会员的消费 [[v:Q2.r0.gmv]]。")
    run = await run_manager.start(graph=graph, input_payload={}, origin_kind="canvas")
    row = await S.wait(run.id)
    assert row.status == "succeeded", row.error
    await ledger_match.drain()
    body = await ledger_of(client, row.id)
    assert body["state"] == "matched"
    [label] = body["reports"]["write"]["labels"].values()
    assert label["kind"] == "related"
    [related] = label["related"]
    assert related["binding"] == [{"field": "phone", "masked": True, "value": "已遮罩"}]
    assert "13800000001" not in json.dumps(body, ensure_ascii=False)
    assert related["diff"]["items"] and "13800000001" not in json.dumps(related["diff"], ensure_ascii=False)


def test_diff_fragments_go_through_the_same_mask_as_the_evidence_panel():
    """差异条目、逐行对照里的 SQL 片段走证据面板同一套换法（api/evidence._Shown）：这次运行查到过的遮罩值换成「已遮罩」。"""
    from app.ledger.labels import view_label
    from app.ledger.view import _Hide

    class Secrets:
        def hit(self, value):
            return str(value) == "13800000001"

        def weak_hit(self, text):
            return text == "13800000001"

        def scrub(self, text, *, prose=False, loose=None):
            return text.replace("13800000001", "已遮罩")

    hider = _Hide(Secrets())
    label = {"kind": "related", "related": [{
        "caliber": "wf@1", "workflow_id": "wf", "version": 1, "level": "published", "binding": {"phone": "13800000001"},
        "diff": {"items": [{"aspect": "filter", "a": "phone = '13800000001'", "b": None,
                            "text": "这次只保留 phone = '13800000001' 的行，口径没有这个条件"}],
                 "fallback": False, "text_diff": ["- WHERE phone = '13800000001'", "+ WHERE TRUE"]}}]}
    shown = view_label(label, hide=hider.hide, shown=hider.shown, names={"wf": "周销售"})
    text = json.dumps(shown, ensure_ascii=False)
    assert "13800000001" not in text and text.count("已遮罩") >= 4
    assert shown["related"][0]["workflow_name"] == "周销售" and shown["related"][0]["level_text"] == "已发布"


async def test_the_evidence_segment_carries_the_label_only_when_matched(client, monkeypatch):
    await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    seg = first_segment(row, "170.5")
    res = await client.get(f"/api/runs/{row.id}/evidence/segments/{seg}")
    assert res.status_code == 200, res.text
    ledger = res.json()["ledger"]
    assert ledger["label"]["kind"] == "caliber" and ledger["note"] == "封存后比对，不在封存范围内"
    assert ledger["post_seal"] is True and ledger["artifact"]

    monkeypatch.setattr(ledger_match, "schedule", lambda run_id, **kw: False)
    other = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    res = await client.get(f"/api/runs/{other.id}/evidence/segments/{first_segment(other, '170.5')}")
    assert res.status_code == 200 and "ledger" not in res.json()


def _data_block(page: str) -> dict:
    found = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', page, re.S)
    assert found, "页面里没有数据块"
    return json.loads(found.group(1))


async def exported(run_id: str) -> list[AuditEvent]:
    async with SessionLocal() as session:
        return list((await session.execute(select(AuditEvent).where(
            AuditEvent.action == "report.exported", AuditEvent.subject_id == run_id).order_by(AuditEvent.at))).scalars())


async def test_report_export_shows_the_label_and_records_the_export(client, monkeypatch):
    await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    res = await client.get(f"/api/runs/{row.id}/report.html")
    assert res.status_code == 200, res.text
    data = _data_block(res.text)
    assert data["ledger"]["note"] == "封存后比对，不在封存范围内"
    [label] = data["ledger"]["labels"].values()
    assert label["kind"] == "caliber"
    [audit] = await exported(row.id)
    assert audit.subject_kind == "run" and audit.data["format"] == "html"
    assert audit.data["report_node"] == "write" and audit.data["doc_artifact"] == row.output["_evidence"]["doc_artifact"]

    res = await client.get(f"/api/runs/{row.id}/bundle.html")
    assert res.status_code == 200, res.text
    audits = await exported(row.id)
    assert [a.data["format"] for a in audits] == ["html", "bundle"]
    assert audits[1].data["reports"] == [{"report_node": "write", "doc_artifact": audit.data["doc_artifact"]}]
    # 可验证报告把封存之后追加的比对列出来，只说几条，不带绑定
    assert "封存后比对已发布口径：1 条等价已证明" in res.text


async def test_an_unmatched_report_export_has_no_ledger_block(client, monkeypatch):
    monkeypatch.setattr(ledger_match, "schedule", lambda run_id, **kw: False)
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    res = await client.get(f"/api/runs/{row.id}/report.html")
    assert res.status_code == 200 and "ledger" not in _data_block(res.text)


async def test_stats_count_what_the_fixtures_did(client, monkeypatch):
    from datetime import datetime, timezone

    since = datetime.now(timezone.utc)
    wf = await S.publish()
    await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    await S.explore(monkeypatch, S.WITH_REFUNDS, text="含退款 [[v:Q1.r0.gmv]]。")
    await S.explore(monkeypatch, S.ELSEWHERE, text="一共 [[v:Q1.r0.n]] 家店。")
    monkeypatch.setattr(ledger_match, "schedule", lambda run_id, **kw: False)
    timed = await S.explore(monkeypatch, S.EQUIVALENT + " -- 超时用", text="本周销售额 [[v:Q1.r0.销售额]]。")
    await ledger_match.match_run(timed.id, timeout=0)
    res = await client.get("/api/ledger/stats", params={"since": since.isoformat()})
    assert res.status_code == 200, res.text
    body = res.json()
    e1 = body["e1"]
    assert e1["runs"] == 4 and e1["queries"] == 4
    assert e1["status"] == {"proven": 1, "composed": 0, "related": 1, "none": 1, "skipped": 1}
    assert e1["with_candidates"] == 3 and e1["rates"]["proven"] == round(1 / 3, 4)
    assert e1["diff_aspects"]["filter"] == 1 and e1["labels"]["caliber"] == 1 and e1["labels"]["unknown"] == 1
    assert e1["whole_equivalent"] == 1
    assert body["e2"]["launched"] == 0 and body["e2"]["feedback"]["available"] is False
    assert body["e3"]["chat_runs"] == 0 and body["e3"]["threshold"] == {"runs": 3, "conversations": 2}
    usage = {u["caliber"]: u for u in body["e4"]["usage"]}
    assert usage[f"{wf}@1"]["proven_runs"] == 1
    # 召回（第 4 波）：这一段时间里没有发布新版本，没有召回
    assert body["e4"]["per_publish"]["available"] is True and body["e4"]["per_publish"]["groups"] == 0


async def test_stats_take_the_run_ids_the_caller_names(monkeypatch):
    """评测（第 3 波）直接调 compute_stats，点名自己的运行：只算这几次。"""
    from app.ledger.stats import compute_stats

    await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    await S.explore(monkeypatch, S.WITH_REFUNDS, text="含退款 [[v:Q1.r0.gmv]]。")
    async with SessionLocal() as session:
        body = await compute_stats(session, run_ids=[row.id])
    assert body["e1"]["runs"] == 1 and body["e1"]["status"]["proven"] == 1 and body["window"]["runs"] == [row.id]
