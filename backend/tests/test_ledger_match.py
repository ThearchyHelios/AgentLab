"""认口径：运行封存之后在后台比对已发布口径（app/ledger/match.py、store.py、labels.py，next-round 02 做法 2、3）。

真的跑一遍探索运行（虚构店铺库、mock 模型写报告），等后台比对做完，看比对工件和 ledger.matched：
- 等价的写法：查询 proven、绑定取对、列按位置映射，报告里引用它的数标「来自口径 · 等价已证明」，整题等价、写出还缺的入口字段；
  工件 meta 记 calibers，事件追加在封存之后、封存核对照旧一致；同一份封存再比一次工件哈希一样；
- 少一个条件：related，差在哪写「口径只保留……这次没有这个条件」，数标相关口径；
- 没有相关口径：none，数标这次的推理；正式运行、接口发起的运行、失败的运行不比；
- 超时、快照被改过、SQL 解析不了：skipped，照实写原因，数标没比对；
- 指标：输入等价、式子和口径卡一样才算来自口径，式子不同的是相关口径；
- 启动补比：只补没比过的，trigger=backfill。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.events import EventType
from app.db.base import SessionLocal
from app.db.models import Artifact, Run, RunEvent
from app.engine.runner import run_manager, verify_manifest
from app.ledger import match as ledger_match
from app.ledger import store
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def engine_up(tmp_path):
    await run_manager.setup()
    path = tmp_path / "shop.db"
    S.build_shop(path)
    await S.set_source(path)
    # 每个用例自己发布口径：先把别的用例发布的退役掉，候选口径只看这一个用例的
    from app.db.models import WorkflowVersion
    from app.db.base import utcnow

    async with SessionLocal() as session:
        for row in (await session.execute(select(WorkflowVersion).where(WorkflowVersion.retired_at.is_(None)))).scalars():
            row.retired_at = utcnow()
        await session.commit()
    yield path
    await ledger_match.drain()
    await run_manager.shutdown()


async def matched(run_id: str) -> tuple[store.Recorded, list[RunEvent]]:
    recorded = await store.latest(run_id)
    assert recorded is not None and recorded.content is not None, recorded
    async with SessionLocal() as session:
        events = list((await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == EventType.LEDGER_MATCHED).order_by(RunEvent.seq))).scalars())
    return recorded, events


def only_label(content: dict) -> dict:
    [labels] = [r["labels"] for r in content["reports"].values()]
    [label] = labels.values()
    return label


async def test_an_equivalent_query_is_proven_and_its_number_comes_from_the_caliber(monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    recorded, [event] = await matched(row.id)
    content = recorded.content
    assert content["format"] == store.FORMAT and content["algo"] == store.ALGO and content["run_id"] == row.id
    assert content["manifest_hash"] == row.manifest_hash
    [query] = content["queries"]
    assert query["status"] == "proven" and query["source"] == "shop" and query["tables"] == ["orders"]
    [cand] = query["candidates"]
    assert cand["caliber"] == f"{wf}@1" and cand["level"] == "governed" and cand["node_id"] == "fetch"
    assert cand["binding"] == {"start": "2026-09-01", "end": "2026-09-08"}
    assert cand["columns"] == [{"index": 0, "ours": "销售额", "caliber": "gmv"}]
    [caliber] = content["calibers"]
    assert caliber["caliber"] == f"{wf}@1" and caliber["fetches"] == ["fetch"] and caliber["cards"] == ["card"]
    assert "name" not in caliber, "工件里不存工作流名（名字会改）"

    label = only_label(content)
    assert label["kind"] == "caliber" and label["how"] == "independent" and label["caliber"] == f"{wf}@1"
    assert label["column"] == "gmv" and label["binding"] == {"start": "2026-09-01", "end": "2026-09-08"}
    whole = content["whole"]
    assert whole["equivalent"] is True and whole["caliber"] == f"{wf}@1" and whole["how"] == "independent"
    assert whole["missing"] == ["store"] and whole["missing_required"] == []

    # 事件在封存之后，封存核对照旧一致
    assert event.seq > row.manifest_seq and event.data["post_seal"] is True
    assert event.data["artifact"] == recorded.artifact and event.data["trigger"] == "sealed"
    assert event.data["counts"]["proven"] == 1
    assert (await verify_manifest(row.id))["ok"] is True
    async with SessionLocal() as session:
        ref = await session.get(Artifact, (recorded.artifact, row.id))
    assert ref.kind == store.KIND and ref.meta["calibers"] == [f"{wf}@1"] and ref.meta["related"] == []

    # 同一份封存再比一次：工件一字不差
    again = await ledger_match.match_run(row.id, trigger="on_demand")
    assert again == recorded.artifact
    latest = await store.latest(row.id)
    assert latest.trigger == "on_demand" and latest.retro is True


async def test_a_missing_condition_is_related_and_says_where_it_differs(monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.WITH_REFUNDS)
    recorded, _ = await matched(row.id)
    [query] = recorded.content["queries"]
    assert query["status"] == "related"
    [cand] = query["candidates"]
    assert cand["status"] == "related" and cand["caliber"] == f"{wf}@1"
    assert cand["binding"] == {"start": "2026-09-01", "end": "2026-09-08"} and cand["via"] == "heuristic"
    texts = [item["text"] for item in cand["diff"]["items"]]
    assert any("口径只保留" in t and "这次没有这个条件" in t for t in texts), texts
    label = only_label(recorded.content)
    assert label["kind"] == "related" and label["related"][0]["caliber"] == f"{wf}@1"
    assert recorded.content["whole"]["equivalent"] is False


async def test_without_a_related_caliber_the_number_is_this_runs_reasoning(monkeypatch):
    await S.publish()
    row = await S.explore(monkeypatch, S.ELSEWHERE, text="一共 [[v:Q1.r0.n]] 家店。")
    recorded, _ = await matched(row.id)
    [query] = recorded.content["queries"]
    assert query["status"] == "none" and query["candidates"] == []
    assert only_label(recorded.content)["kind"] == "reasoning"
    assert recorded.content["calibers"] == []


async def test_a_value_differs_in_the_hard_coded_status_is_not_proven(monkeypatch):
    """写死的码值不同（'paid' 对 'refunded'）：normalize_sql 文本相同，照样不等价。"""
    await S.publish()
    sql = S.EQUIVALENT.replace("'paid'", "'refunded'")
    row = await S.explore(monkeypatch, sql, text="退款 [[v:Q1.r0.销售额]]。")
    recorded, _ = await matched(row.id)
    assert recorded.content["queries"][0]["status"] == "related"


@pytest.mark.parametrize("origin, run_class", [("api", "exploratory"), (None, "exploratory")])
async def test_runs_that_are_not_exploration_are_not_compared(monkeypatch, origin, run_class):
    await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, origin=origin, run_class=run_class,
                          text="本周销售额 [[v:Q1.r0.销售额]]。")
    assert await store.latest(row.id) is None
    async with SessionLocal() as session:
        run = await session.get(Run, row.id)
    assert ledger_match.ineligible(run) == ledger_match.NOT_EXPLORE
    assert await ledger_match.match_run(row.id) is None


def test_ineligible_reasons():
    from types import SimpleNamespace

    base = dict(run_class="exploratory", origin_kind="chat", status="succeeded", manifest_hash="h")
    assert ledger_match.ineligible(SimpleNamespace(**base)) is None
    assert ledger_match.ineligible(None) == ledger_match.NOT_FOUND
    assert ledger_match.ineligible(SimpleNamespace(**{**base, "run_class": "formal"})) == ledger_match.FORMAL
    assert ledger_match.ineligible(SimpleNamespace(**{**base, "origin_kind": "eval"})) == ledger_match.NOT_EXPLORE
    assert ledger_match.ineligible(SimpleNamespace(**{**base, "status": "failed"})) == ledger_match.NOT_SUCCEEDED
    assert ledger_match.ineligible(SimpleNamespace(**{**base, "manifest_hash": None})) == ledger_match.NOT_SEALED
    assert all(ledger_match.wanted("exploratory", o) for o in ("chat", "canvas", "trial", "personal_app"))
    assert not ledger_match.wanted("formal", "chat") and not ledger_match.wanted("exploratory", "reconcile")


async def test_a_timeout_is_recorded_as_skipped(monkeypatch):
    await S.publish()
    monkeypatch.setattr(ledger_match, "schedule", lambda run_id, **kw: False)
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    assert await store.latest(row.id) is None
    artifact = await ledger_match.match_run(row.id, timeout=0)
    assert artifact
    recorded, _ = await matched(row.id)
    [query] = recorded.content["queries"]
    assert query["status"] == "skipped" and query["reason"] == ledger_match.TIMEOUT
    assert query["candidates"][0]["status"] == "skipped"
    label = only_label(recorded.content)
    assert label["kind"] == "unknown" and label["reason"] == ledger_match.TIMEOUT


async def test_a_tampered_snapshot_is_skipped(monkeypatch):
    await S.publish()
    monkeypatch.setattr(ledger_match, "schedule", lambda run_id, **kw: False)
    # 工件按内容寻址、各用例共用一个工件库：换一种只在这里用的写法，改的那份快照不会被别的用例读到；改完放回去
    row = await S.explore(monkeypatch, S.EQUIVALENT + " -- 改快照用", text="本周销售额 [[v:Q1.r0.销售额]]。")
    async with SessionLocal() as session:
        [end] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == row.id, RunEvent.type == "tool.end"))).scalars()
    snap_id = end.data["query_artifact"]
    path = settings.data_dir / "artifacts" / snap_id[:2] / f"{snap_id}.json"
    original = path.read_text("utf-8")
    snap = json.loads(original)
    snap["rows"] = [[999999]]
    path.write_text(json.dumps(snap, ensure_ascii=False), "utf-8")
    try:
        await ledger_match.match_run(row.id)
    finally:
        path.write_text(original, "utf-8")
    recorded, _ = await matched(row.id)
    [query] = recorded.content["queries"]
    assert query["status"] == "skipped" and query["reason"] == ledger_match.SNAPSHOT_TAMPERED
    assert only_label(recorded.content)["kind"] == "unknown"


def test_unparseable_sql_is_skipped_with_the_reason():
    caliber = ledger_match.Caliber("wf", 1, "governed", "h", (ledger_match.Fetch("fetch", "shop", S.TEMPLATE,
                                                                             frozenset({"orders"})),))
    query = ledger_match.Query("a1", "q", "shop", "SELEC amount FROM orders WHERE", tables=frozenset({"orders"}),
                               dialect="sqlite")
    [row] = ledger_match.compare_queries([query], [caliber])
    assert row["status"] == "skipped" and row["reason"] == ledger_match.PARSE_FAILED


def test_candidates_are_prefiltered_by_source_and_table():
    fetches = (ledger_match.Fetch("a", "shop", S.TEMPLATE, frozenset({"orders"})),
               ledger_match.Fetch("b", "crm", S.TEMPLATE, frozenset({"orders"})),
               ledger_match.Fetch("c", "shop", "SELECT COUNT(*) FROM stores", frozenset({"stores"})))
    caliber = ledger_match.Caliber("wf", 1, "published", "h", fetches)
    query = ledger_match.Query("a1", "q", "shop", S.EQUIVALENT, tables=frozenset({"orders"}), dialect="sqlite")
    [row] = ledger_match.compare_queries([query], [caliber])
    assert [c["node_id"] for c in row["candidates"]] == ["a"] and row["status"] == "proven"


async def test_a_metric_from_equivalent_inputs_with_the_same_formula_comes_from_the_caliber(monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[m:gmv]]。",
                          card_expression="cell(nodes.q, 0, '销售额')")
    recorded, _ = await matched(row.id)
    label = only_label(recorded.content)
    assert label["kind"] == "caliber" and label["caliber"] == f"{wf}@1"
    assert label["card"] == "card" and label["metric"] == "gmv"
    assert recorded.content["whole"]["equivalent"] is True


async def test_a_metric_with_a_different_formula_is_only_related(monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[m:gmv]]。",
                          card_expression="cell(nodes.q, 0, '销售额') * 2")
    recorded, _ = await matched(row.id)
    label = only_label(recorded.content)
    assert label["kind"] == "related" and label["reason"] == "取数和口径等价，但指标的算法和口径卡上的不同"
    assert label["related"][0]["caliber"] == f"{wf}@1"


async def test_published_and_governed_both_count_and_the_label_says_which(monkeypatch):
    wf = await S.publish(level="published")
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    recorded, _ = await matched(row.id)
    label = only_label(recorded.content)
    assert label["kind"] == "caliber" and label["level"] == "published" and label["caliber"] == f"{wf}@1"


async def test_only_the_latest_published_version_is_a_candidate(monkeypatch):
    wf = await S.publish()
    await S.publish(workflow_id=wf, version=2, graph=S.caliber_graph(S.TEMPLATE.replace("'paid'", "'open'")))
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    recorded, _ = await matched(row.id)
    [query] = recorded.content["queries"]
    assert [c["caliber"] for c in query["candidates"]] == [f"{wf}@2"] and query["status"] == "related"


async def test_backfill_matches_only_runs_never_matched(monkeypatch):
    wf = await S.publish()
    monkeypatch.setattr(ledger_match, "schedule", lambda run_id, **kw: False)
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    assert await store.latest(row.id) is None
    assert await ledger_match.backfill() >= 1
    recorded, [event] = await matched(row.id)
    assert event.data["trigger"] == "backfill" and recorded.retro is True
    assert recorded.content["queries"][0]["candidates"][0]["caliber"] == f"{wf}@1"
    # 再补一次：比过的不再比
    await ledger_match.backfill()
    _, events = await matched(row.id)
    assert len(events) == 1


async def test_backfill_follows_the_scheduler_switch(monkeypatch):
    monkeypatch.setattr(settings, "scheduler", False)
    assert ledger_match.start_backfill() is False


async def test_only_sealed_query_snapshots_are_compared(monkeypatch):
    """比对只读封存范围内交回过的查询快照（api/evidence._sealed 的读法）：事后往工件表里插一份属于这次运行的快照，不比它。"""
    from app.core.artifact_store import put_json

    await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    await put_json({"sql": S.WITH_REFUNDS, "source": "shop", "columns": ["gmv"], "rows": [[1]]},
                   kind="query_snapshot", run_id=row.id, node_id="q")
    await ledger_match.match_run(row.id, trigger="on_demand")
    recorded, _ = await matched(row.id)
    assert [q["status"] for q in recorded.content["queries"]] == ["proven"]


async def test_cells_inside_a_table_are_labelled_the_same_way(monkeypatch):
    wf = await S.publish()
    row = await S.explore(monkeypatch, S.EQUIVALENT,
                          text="本周销售额 [[v:Q1.r0.销售额]]。\n\n[[table:Q1 cols=销售额 rows=0-0]]")
    recorded, _ = await matched(row.id)
    [labels] = [r["labels"] for r in recorded.content["reports"].values()]
    assert len(labels) >= 2 and all(label["kind"] == "caliber" and label["caliber"] == f"{wf}@1"
                                    for label in labels.values())
