"""目录快照与影子起草（app/eval/snapshots.py；细案 01 做法 6）。

- 当前目录的快照：按 data/catalog_view.py 的读法读得出、和拍的那一刻一样；之后线上目录改了，快照不变（内容寻址、不可变）。
- 影子起草：同一个起草引擎、只起草指定（或题集用得到）的表，写进内存里的副本、出快照；线上目录一项不动（版本不变、
  不调 write_entry）；不查业务库（开了剖析也不去数有效行过滤的排除比例）；不占数据源的队；可以先去掉模型起草、还没人
  审过的项；停止的不出快照；服务重启后没结束的标中断。开始前给估算。

库、表、目录内容都是虚构的。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core import artifact_store, job_runtime
from app.data import catalog, catalog_draft, catalog_profile, catalog_view
from app.data.catalog import make_item
from app.data.catalog_view import CatalogView
from app.db.base import SessionLocal
from app.db.models import CatalogNote, DataSource
from app.db.models_eval import EvalCatalogSnapshot, EvalSuite
from app.eval import snapshots
from tests.fixtures import dual_check_shop as shop
from tests.fixtures.catalog.fake_model import FakeModel
from tests.fixtures.sources import drop_source

LIVE_NOTES = {
    "label": make_item("门店订单", "human"),                           # 人工确认
    "grain": make_item("每笔订单一行", "llm"),                           # 模型起草、没人审过
    "columns": {"amount": {"label": make_item("实收金额", "llm", "confirmed")}},   # 模型起草、人工确认过
}
REPLY = {"orders": {"label": "影子订单", "grain": "影子：每单一行", "kind": "fact",
                    "valid_filter": {"condition": "status <> 'refunded'", "reason": "状态列有退款"},
                    "columns": [{"name": "status", "label": "订单状态", "meaning": "paid 已支付，refunded 已退款"}]}}


@pytest.fixture
async def source(tmp_path):
    name, sid = await shop.add_source(shop.build(tmp_path / "shop.db"),
                                      options={catalog_profile.PROFILE_OPTION: {"enabled": True}})
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "orders", LIVE_NOTES, if_version=0, actor="审阅人")
    yield {"name": name, "id": sid}
    await drop_source(sid)


async def live_rows(source_id: str) -> list[tuple[str, int, dict]]:
    async with SessionLocal() as session:
        rows = (await session.execute(select(CatalogNote).where(CatalogNote.source_id == source_id))).scalars().all()
        return sorted((r.table_name, r.version, r.notes) for r in rows)


async def read_view(snapshot_artifact: str, source_id: str) -> dict:
    view = CatalogView(mode="snapshot", snapshot=snapshot_artifact)
    async with SessionLocal() as session:
        found = await catalog_view.read_many(session, view, [source_id])
    return {t: e.notes for t, e in found.get(source_id, {}).items()}


async def wait_done(snap_id: str) -> EvalCatalogSnapshot:
    await snapshots.manager.wait(snap_id, timeout=30)
    async with SessionLocal() as session:
        return await session.get(EvalCatalogSnapshot, snap_id)


# --------------------------------------------------------------------------
# 当前目录
# --------------------------------------------------------------------------


async def test_a_live_snapshot_reads_back_through_the_catalog_view_and_never_changes(source):
    async with SessionLocal() as session:
        snap = await snapshots.take_live(session, [source["id"]], actor="评测人")
    assert snap.status == "succeeded" and snap.origin == "live" and snap.created_by == "评测人"
    assert snap.sources == [{"id": source["id"], "name": source["name"]}]
    content = artifact_store.load(snap.artifact_id)
    assert content["taken"]["origin"] == "live" and content["taken"]["by"] == "评测人"
    assert artifact_store.content_hash(artifact_store.canonical_json(content)) == snap.artifact_id
    assert (await read_view(snap.artifact_id, source["id"]))["orders"] == LIVE_NOTES
    # 之后线上目录改了：快照还是拍的那一份
    async with SessionLocal() as session:
        await catalog.write_entry(session, source["id"], "orders", {"label": make_item("改过的", "human")},
                                  if_version=1, actor="审阅人")
    assert (await read_view(snap.artifact_id, source["id"]))["orders"]["label"]["value"] == "门店订单"
    # 只用已核实的项：读的时候按视图筛，快照本身照原样
    reviewed = CatalogView(mode="snapshot", statuses="reviewed", snapshot=snap.artifact_id)
    async with SessionLocal() as session:
        notes = (await catalog_view.read_many(session, reviewed, [source["id"]]))[source["id"]]["orders"].notes
    assert "grain" not in notes and notes["label"]["value"] == "门店订单"


async def test_a_live_snapshot_needs_existing_sources():
    async with SessionLocal() as session:
        with pytest.raises(snapshots.SnapshotInvalid, match="不存在"):
            await snapshots.take_live(session, ["nope"], actor=None)
        with pytest.raises(snapshots.SnapshotInvalid, match="请选择"):
            await snapshots.take_live(session, [], actor=None)


# --------------------------------------------------------------------------
# 影子起草
# --------------------------------------------------------------------------


async def test_shadow_drafting_writes_only_the_copy(source, monkeypatch):
    model = FakeModel(REPLY)
    monkeypatch.setattr(snapshots.ShadowDraft, "model_override", model)

    async def no_business_queries(*_a, **_k):
        raise AssertionError("影子起草不该查业务库")

    monkeypatch.setattr(catalog_profile, "check_valid_filter", no_business_queries)
    before = await live_rows(source["id"])
    async with SessionLocal() as session:
        plan = await snapshots.plan_shadow(session, {"sources": [{"id": source["id"], "tables": ["orders"]}],
                                                     "guidance": "订单表按支付日统计"}, model_id="mock-fast")
        snap = await snapshots.start_shadow(session, plan, actor="评测人")
    assert snap.status in ("queued", "running")
    # 不占数据源的队：同一个源照样能马上起草、剖析
    await asyncio.sleep(0)
    assert job_runtime.lanes.holders(source["id"]) == []
    done = await wait_done(snap.id)
    assert done.status == "succeeded", (done.error, done.progress)
    # 线上目录一项不动
    assert await live_rows(source["id"]) == before
    notes = (await read_view(done.artifact_id, source["id"]))["orders"]
    assert notes["label"]["value"] == "门店订单"                      # 人工确认的不被起草覆盖
    assert notes["grain"]["value"] == "影子：每单一行"                 # 没人审过的模型项换成这次起草的
    assert notes["columns"]["status"]["label"]["value"] == "订单状态"
    # 开了剖析也不去业务库数排除比例：照收，写明没有核对
    assert catalog_draft.SHADOW_NOT_CHECKED in notes["valid_filter"]["note"]
    # 起草要求交给了模型；只起草了点名的表
    assert len(model.calls) == 1 and "订单表按支付日统计" in model.calls[0]
    content = artifact_store.load(done.artifact_id)
    assert content["taken"]["origin"] == "shadow" and content["taken"]["tables"] == {source["id"]: ["orders"]}
    assert done.usage["calls"] == 1 and done.progress["tables_done"] == 1
    assert done.estimate["tables"] == 1 and done.estimate["calls"] == 1


async def test_shadow_drafting_can_start_without_unreviewed_model_items(source, monkeypatch):
    monkeypatch.setattr(snapshots.ShadowDraft, "model_override", FakeModel({"orders": {"kind": "fact"}}))
    async with SessionLocal() as session:
        plan = await snapshots.plan_shadow(session, {"sources": [{"id": source["id"], "tables": ["orders"]}],
                                                     "strip_llm": True})
        snap = await snapshots.start_shadow(session, plan, actor=None)
    done = await wait_done(snap.id)
    notes = (await read_view(done.artifact_id, source["id"]))["orders"]
    assert "grain" not in notes                                            # 没人审过的模型项去掉了
    assert notes["columns"]["amount"]["label"]["value"] == "实收金额"       # 人工确认过的模型项留着
    assert notes["label"]["value"] == "门店订单" and notes["kind"]["value"] == "fact"


async def test_tables_come_from_the_suite_when_not_listed(source):
    from app.db.models_eval import EvalCase

    async with SessionLocal() as session:
        suite = EvalSuite(name="虚构题集")
        session.add(suite)
        await session.flush()
        session.add(EvalCase(suite_id=suite.id, seq=1, question="q", expectations=[], gold={
            "kind": "sql", "datasource_id": source["id"],
            "sql": "SELECT SUM(amount) FROM orders WHERE paid_date = '{{ schedule.yesterday }}'"}))
        await session.commit()
        found = await snapshots.suite_tables(session, suite.id)
        assert found == {source["id"]: ["orders"]}
        plan = await snapshots.plan_shadow(session, {"sources": [{"id": source["id"]}], "suite_id": suite.id})
        assert plan.sources[0]["tables"] == ["orders"]
        with pytest.raises(snapshots.SnapshotInvalid, match="没有指定要起草的表"):
            await snapshots.plan_shadow(session, {"sources": [{"id": source["id"]}]})
        with pytest.raises(snapshots.SnapshotInvalid, match="表结构里没有"):
            await snapshots.plan_shadow(session, {"sources": [{"id": source["id"], "tables": ["ghost"]}]})


def test_the_estimate_is_said_to_be_an_estimate():
    from types import SimpleNamespace

    cols = lambda n: [{"name": f"c{i}", "type": "TEXT"} for i in range(n)]  # noqa: E731
    src = SimpleNamespace(schema_cache={"tables": {"a": {"columns": cols(10)}, "b": {"columns": cols(10)},
                                                   "wide": {"columns": cols(100)}}})
    est = snapshots.estimate([(src, ["a", "b", "wide"])], "no-such-model")
    # 窄表拼一段；宽表是表级一段 + 列分段（100 列按 40 列一段是 3 段）
    assert est["calls"] == 1 + (1 + 3) and est["tables"] == 3 and est["columns"] == 120
    assert est["unpriced"] is True and est["cost_usd"] is None and "不含思考" in est["note"]
    assert est["output_tokens"] == 3 * snapshots.EST_OUTPUT_PER_TABLE + 120 * snapshots.EST_OUTPUT_PER_COLUMN


async def test_a_stopped_shadow_draft_leaves_no_snapshot(source, monkeypatch):
    monkeypatch.setattr(snapshots.ShadowDraft, "model_override", FakeModel(REPLY, hang=5))
    async with SessionLocal() as session:
        plan = await snapshots.plan_shadow(session, {"sources": [{"id": source["id"], "tables": ["orders"]}]})
        snap = await snapshots.start_shadow(session, plan, actor=None)
    for _ in range(100):
        await asyncio.sleep(0.02)
        async with SessionLocal() as session:
            if (await session.get(EvalCatalogSnapshot, snap.id)).status == "running":
                break
    assert await snapshots.stop(snap.id)
    done = await wait_done(snap.id)
    assert done.status == "cancelled" and done.artifact_id is None and done.error == snapshots.STOPPED
    assert not await snapshots.stop(snap.id)


async def test_unfinished_shadow_drafts_are_marked_interrupted_after_a_restart(source):
    async with SessionLocal() as session:
        row = EvalCatalogSnapshot(origin="shadow", status="running", sources=[{"id": source["id"], "name": "x"}])
        session.add(row)
        await session.commit()
        await snapshots.reconcile_stale(session, [row])
        assert row.status == "interrupted" and row.error == snapshots.INTERRUPTED
        other = EvalCatalogSnapshot(origin="shadow", status="queued", sources=[])
        session.add(other)
        await session.commit()
        other_id = other.id
    assert await snapshots.startup() >= 1
    async with SessionLocal() as session:
        assert (await session.get(EvalCatalogSnapshot, other_id)).status == "interrupted"


async def test_a_mock_provider_is_not_a_drafting_model():
    from app.db.models import Provider

    async with SessionLocal() as session:
        session.add(Provider(name="演示接入_快照", kind="mock", default_model="mock-fast", enabled=True))
        await session.commit()
        try:
            with pytest.raises(snapshots.SnapshotInvalid, match="演示用的模型接入"):
                await snapshots.resolve_model(session, "演示接入_快照", None)
        finally:
            row = (await session.execute(select(Provider).where(Provider.name == "演示接入_快照"))).scalar_one()
            await session.delete(row)
            await session.commit()


async def test_a_partly_failed_shadow_draft_still_snapshots_and_says_which_table(tmp_path, monkeypatch):
    import sqlite3

    from app.data import introspect

    path = tmp_path / "two.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL);"
                         "CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT);")
    async with SessionLocal() as session:
        row = DataSource(name=f"two_{id(path) % 100000}", kind="sqlite", database=str(path))
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        sid = row.id
    try:
        # stores 那一段一直出错：起草引擎拆小重试之后仍没写出来
        monkeypatch.setattr(snapshots.ShadowDraft, "model_override",
                            FakeModel({"orders": {"label": "订单"}, "stores": {"label": "门店"}}, fail={"stores"}))
        monkeypatch.setattr(catalog, "MODEL_RETRIES", 0)
        monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)       # 每张表一段：一段出错只影响那一张
        async with SessionLocal() as session:
            plan = await snapshots.plan_shadow(session, {"sources": [{"id": sid, "tables": ["orders", "stores"]}]})
            snap = await snapshots.start_shadow(session, plan, actor=None)
        done = await wait_done(snap.id)
        assert done.status == "partial" and done.artifact_id, (done.status, done.error)
        assert list(done.progress["failures"]) == [f"{row.name}.stores"] and done.progress["tables_failed"] == 1
        notes = await read_view(done.artifact_id, sid)
        assert notes["orders"]["label"]["value"] == "订单" and "stores" not in notes
    finally:
        await drop_source(sid)
