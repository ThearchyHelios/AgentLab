"""版本页接口（期 3，WP-5；P3-SPEC 第 7 节）：版本列表、启用旧版本、清单、移除一期、作废接受、导入记录的新字段。
真管线、经 HTTP；只有接口不提供的观察点（快照记录、导入记录的状态、snapshot_activations）才直接读库。

夹具一律合成、假名（客流表：分区甲、分区乙……），数字是随机数；不调用任何模型。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.core import artifact_store
from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, SnapshotActivation, SourceSnapshot, TableImport
from tests.fixtures.xlsx.drift import AUG, DRIFT_CASES, SEP
from tests.fixtures.xlsx.flow import flow_workbook
from tests.test_table_imports_api_p3 import (  # noqa: F401 - client、store 是夹具
    API, client, commit, confirms, count, db_get, first_import, reupload, store, trial, unique,
)


async def snapshots(client, source_id: str) -> list[dict[str, Any]]:
    resp = await client.get(f"{API}/{source_id}/snapshots")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def imports(client, source_id: str) -> list[dict[str, Any]]:
    resp = await client.get(f"{API}/{source_id}/imports")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def two_periods(client, *, seed: int) -> dict[str, Any]:
    """8 月（按期累积）+ 9 月：S1=[8 月]、S2=[8 月, 9 月]，同一个配方。"""
    name = unique()
    base = await first_import(client, name, seed=seed)
    raw, fn = flow_workbook(SEP, 30, seed=seed + 1)
    st = await reupload(client, base["source_id"], raw, fn)
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    return {"name": name, "source_id": base["source_id"], "s1": base["commit"]["snapshot_id"],
            "s2": resp.json()["snapshot_id"], "aug": base["commit"]["import_id"], "sep": resp.json()["import_id"]}


async def activations(source_id: str) -> list[SnapshotActivation]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(SnapshotActivation).where(SnapshotActivation.source_id == source_id)
            .order_by(SnapshotActivation.created_at))).scalars())


# ==========================================================================
# 列表（7.1）
# ==========================================================================


async def test_snapshot_list_puts_current_first_with_parts_rows_and_periods_diff(client):
    v = await two_periods(client, seed=401)
    items = await snapshots(client, v["source_id"])
    assert [s["id"] for s in items[:2]] == [v["s2"], v["s1"]]
    cur, old = items[0], items[1]
    assert cur["current"] is True and cur["activatable"] is False and cur["reason_code"] == "current"
    assert cur["reason"] == "已是当前版本" and cur["mode"] == "accumulate"
    assert [p["period_start"] for p in cur["parts"]] == ["2026-08-01", "2026-09-01"]
    assert cur["parts"][1]["rows"] == {"日客流": 30, "时段客流": 510, "时段客流_表内合计": 90}
    assert cur["tables"] == {"日客流": 61, "时段客流": 1037, "时段客流_表内合计": 183}
    assert cur["recipe"]["seq"] == 1 and cur["available"] is True and cur["db_size"] > 0
    assert len(cur["db_sha256_prefix"]) == 8 and cur["pinned_runs"] == 0 and cur["mask_lost"] == []
    assert old["activatable"] is True and old["reason_code"] is None and old["reason"] is None
    assert old["periods_diff"] == {"added": [], "removed": [
        {"start": "2026-09-01", "end": "2026-09-30", "file_name": cur["parts"][1]["file_name"]}]}
    assert old["tables"] == {"日客流": 31, "时段客流": 527, "时段客流_表内合计": 93}
    # 手工源返回空列表；不存在的源 404
    async with SessionLocal() as session:
        manual = DataSource(name=unique("manual"), kind="sqlite", database=":memory:")
        session.add(manual)
        await session.commit()
        manual_id = manual.id
    assert await snapshots(client, manual_id) == []
    resp = await client.get(f"{API}/nope/snapshots")
    assert resp.status_code == 404 and resp.json()["code"] == "source_not_found"


# ==========================================================================
# 启用旧版本（7.2）
# ==========================================================================


async def test_activate_rolls_back_and_guards_confirm_expected_current_and_mask(client):
    v = await two_periods(client, seed=403)
    sid, url = v["source_id"], f"{API}/{v['source_id']}/snapshots/{v['s1']}/activate"
    resp = await client.post(url, json={"expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 422 and resp.json()["code"] == "confirm_required"
    resp = await client.post(url, json={"confirm": True})
    assert resp.status_code == 422 and resp.json()["code"] == "expected_current_required"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s1"]})
    assert resp.status_code == 409 and resp.json()["code"] == "base_changed"
    resp = await client.post(f"{API}/{sid}/snapshots/{'0' * 64}/activate",
                             json={"confirm": True, "expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 404 and resp.json()["code"] == "snapshot_not_found"
    resp = await client.post(f"{API}/{sid}/snapshots/{v['s2']}/activate",
                             json={"confirm": True, "expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 409 and resp.json()["code"] == "already_current"
    # 遮罩列在目标版本里没有同名列：不带确认 409 mask_lost，带上同样的列表才成
    async with SessionLocal() as session:
        await session.execute(update(DataSource).where(DataSource.id == sid)
                              .values(options={"mask_columns": "分区丁"}))
        await session.commit()
    items = await snapshots(client, sid)
    assert next(s for s in items if s["id"] == v["s1"])["mask_lost"] == ["分区丁"]
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 409 and resp.json()["code"] == "mask_lost" and "分区丁" in resp.json()["detail"]
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s2"],
                                        "ack_mask_lost": ["分区丁"], "reason": "合成理由：回到八月",
                                        "signed_by": "合成署名乙"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["snapshot_id"] == v["s1"] and out["previous_snapshot_id"] == v["s2"]
    assert out["source"]["current_snapshot"]["id"] == v["s1"] and out["recipe_id"]
    assert await count(client, v["name"], "日客流") == 31
    assert (await db_get(TableImport, v["sep"])).status == "superseded"
    assert (await db_get(TableImport, v["aug"])).status == "active"
    last = (await activations(sid))[-1]
    assert (last.kind, last.snapshot_id, last.previous_snapshot_id) == ("activate", v["s1"], v["s2"])
    assert last.reason == "合成理由：回到八月" and last.signed_by == "合成署名乙"


# ==========================================================================
# 移除一期（7.4）
# ==========================================================================


async def test_remove_period_reuses_the_earlier_snapshot_and_refuses_the_last_one(client):
    """8 月、9 月同一个配方时移除 9 月：结果就是原来的 8 月快照（同一组导入、同一个目标配方、同为累积，复用）。"""
    v = await two_periods(client, seed=405)
    sid = v["source_id"]
    url = f"{API}/{sid}/imports/{v['sep']}/remove"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 422 and resp.json()["code"] == "reason_required"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s1"], "reason": "传错"})
    assert resp.status_code == 409 and resp.json()["code"] == "base_changed"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s2"],
                                        "reason": "合成理由：九月传错了文件"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["snapshot_id"] == v["s1"] and out["reused"] is True and out["removed_import_id"] == v["sep"]
    assert out["source"]["current_snapshot"]["id"] == v["s1"]
    assert await count(client, v["name"], "日客流") == 31
    assert (await db_get(TableImport, v["sep"])).status == "superseded"
    last = (await activations(sid))[-1]
    assert (last.kind, last.import_id) == ("remove_period", v["sep"])
    # 只剩一期：不能移除；不在当前版本里的：not_in_current；不存在的：404
    resp = await client.post(f"{API}/{sid}/imports/{v['aug']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s1"], "reason": "x"})
    assert resp.status_code == 409 and resp.json()["code"] == "last_period"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s1"], "reason": "x"})
    assert resp.status_code == 409 and resp.json()["code"] == "not_in_current"
    resp = await client.post(f"{API}/{sid}/imports/nope/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s1"], "reason": "x"})
    assert resp.status_code == 404 and resp.json()["code"] == "import_not_found"


async def test_remove_keeps_recipe_and_mode_and_materializes_a_single_period_union(client):
    """A17①：替换模式 S1=[8 月]（配方 r1）→ 9 月改为按期累积得 S2=[8 月, 9 月]（目标 r2）→ 移除 9 月：新快照的模式是
    accumulate、配方是 r2，id 不等于 S1；8 月的配方不是目标配方，快照库是物化的单期并集，说明含 single_after_drop。"""
    name = unique()
    base = await first_import(client, name, seed=407, mode="replace")
    sid, s1 = base["source_id"], base["commit"]["snapshot_id"]
    raw, fn = flow_workbook(SEP, 30, seed=408)
    st = await reupload(client, sid, raw, fn)
    assert "q_mode" in {q["id"] for q in st["questions"]}
    resp = await client.post(f"{API}/imports/{st['id']}/answers", json={"answers": {"q_mode": {"value": "accumulate"}}})
    assert resp.status_code == 200, resp.text
    st = await trial(client, st["id"])
    t = st["trial"]
    assert t["status"] == "passed", t["problems"]
    assert t["accumulate"]["action"] == "append" and t["accumulate"]["mode_switch"] == "replace->accumulate"
    assert "mode_switch:replace->accumulate" in confirms(st)
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    s2, r2 = resp.json()["snapshot_id"], resp.json()["recipe_id"]
    sep = resp.json()["import_id"]
    resp = await client.post(f"{API}/{sid}/imports/{sep}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": s2, "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["reused"] is False and out["snapshot_id"] not in (s1, s2)
    snap = await db_get(SourceSnapshot, out["snapshot_id"])
    assert snap.mode == "accumulate" and snap.recipe_id == r2
    assert Path(snap.db_path).parent.name == "snapshots" and snap.manifest_artifact
    src = await db_get(DataSource, sid)
    assert src.current_recipe_id == r2, "移除一期不把配方退回去"
    comment = snap.schema_cache["tables"]["日客流"]["comment"]
    assert "当前版本只含最近一期的数据" in comment and "部分期" not in comment
    manifest = artifact_store.load(snap.manifest_artifact)
    assert manifest["action"] == "remove_period" and manifest["removed"]["import_id"] == sep
    assert await count(client, name, "日客流") == 31


async def test_remove_on_a_replace_mode_source_is_not_accumulate(client):
    name = unique()
    base = await first_import(client, name, seed=409, mode="replace")
    resp = await client.post(f"{API}/{base['source_id']}/imports/{base['commit']['import_id']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": base["commit"]["snapshot_id"],
                                   "reason": "x"})
    assert resp.status_code == 409 and resp.json()["code"] == "not_accumulate"


# ==========================================================================
# 作废接受（7.5）与导入记录的新字段（7.6）
# ==========================================================================


async def accepted_september(client, *, seed: int, mode: str) -> dict[str, Any]:
    """8 月（mode）+ 9 月 D26（R1 某日不成立、写理由接受）提交。返回各快照、导入 id。"""
    name = unique()
    base = await first_import(client, name, seed=seed, mode=mode)
    raw, fn = DRIFT_CASES["D26"].build(seed)
    st = await reupload(client, base["source_id"], raw, fn)
    t = st["trial"]
    assert t["status"] == "needs_decision", t["problems"]
    resp = await commit(client, st, acceptances=[{"check_id": c, "reason": "合成理由：设备故障，人工补录"}
                                                 for c in t["acceptable"]])
    assert resp.status_code == 201, resp.text
    return {"name": name, "source_id": base["source_id"], "s1": base["commit"]["snapshot_id"],
            "s2": resp.json()["snapshot_id"], "aug": base["commit"]["import_id"], "sep": resp.json()["import_id"],
            "trial": st}


async def test_revoke_in_accumulate_mode_removes_the_period_and_blocks_reactivation(client):
    """A9：D26 累积提交 → 说明是「部分期不成立或未能核对」、不含「已逐日核对」→ 作废接受 → remove_period，结果复用 S1，
    配方和模式不变，导入记录 revoked 非空；之后启用含作废导入的版本 409 contains_revoked。"""
    v = await accepted_september(client, seed=411, mode="accumulate")
    sid = v["source_id"]
    snap = await db_get(SourceSnapshot, v["s2"])
    comment = snap.schema_cache["tables"]["日客流"]["comment"]
    assert "已逐日核对" not in comment and "部分期的个别" in comment, comment
    recs = {r["id"]: r for r in await imports(client, sid)}
    sep = recs[v["sep"]]
    assert sep["in_current"] is True and sep["current"] is True and sep["recipe_seq"] == 1
    assert sep["acceptances"] and sep["acceptances"][0]["kind"] == "override"
    assert sep["revoke_plan"]["action"] == "remove_period"
    assert [p["start"] for p in sep["revoke_plan"]["result_parts"]] == ["2026-08-01"]
    assert sep["rows"]["日客流"] == 30 and sep["raw_shared_with"] == [] and sep["raw_open_stagings"] == 0
    assert recs[v["aug"]]["revoke_plan"] is None, "没有接受的期不给作废预案"
    url = f"{API}/{sid}/imports/{v['sep']}/revoke-acceptance"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s2"],
                                        "reason": "合成理由：补录的数字有误"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["action"] == "remove_period" and out["snapshot_id"] == v["s1"]
    imp = await db_get(TableImport, v["sep"])
    assert imp.revoked["reason"] == "合成理由：补录的数字有误" and imp.revoked["signed_by_verified"] is False
    assert imp.status == "superseded"
    src = await db_get(DataSource, sid)
    assert (await db_get(SourceSnapshot, src.current_snapshot_id)).mode == "accumulate"
    items = {s["id"]: s for s in await snapshots(client, sid)}
    assert items[v["s2"]]["reason_code"] == "contains_revoked" and items[v["s2"]]["activatable"] is False
    resp = await client.post(f"{API}/{sid}/snapshots/{v['s2']}/activate",
                             json={"confirm": True, "expected_current_snapshot_id": v["s1"]})
    assert resp.status_code == 409 and resp.json()["code"] == "contains_revoked"
    last = (await activations(sid))[-1]
    assert (last.kind, last.import_id) == ("revoke_acceptance", v["sep"])
    # 没有接受的导入不需要作废
    resp = await client.post(f"{API}/{sid}/imports/{v['aug']}/revoke-acceptance",
                             json={"confirm": True, "expected_current_snapshot_id": v["s1"], "reason": "x"})
    assert resp.status_code == 409 and resp.json()["code"] == "no_acceptance"


async def test_revoke_in_replace_mode_rolls_back_to_the_planned_target(client):
    """替换模式：revoke_plan 给回滚目标（S1），请求带 expected_target_snapshot_id；目标对不上 409 revoke_target_changed。"""
    v = await accepted_september(client, seed=413, mode="replace")
    sid = v["source_id"]
    plan = {r["id"]: r for r in await imports(client, sid)}[v["sep"]]["revoke_plan"]
    assert plan["action"] == "rollback" and plan["target_snapshot_id"] == v["s1"]
    assert plan["target"]["recipe_seq"] == 1 and plan["target"]["mode"] == "replace"
    assert plan["target"]["simple"] is False and plan["mask_lost"] == []
    assert [p["start"] for p in plan["target"]["parts"]] == ["2026-08-01"]
    url = f"{API}/{sid}/imports/{v['sep']}/revoke-acceptance"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s2"], "reason": "合成理由",
                                        "expected_target_snapshot_id": "0" * 64})
    assert resp.status_code == 409 and resp.json()["code"] == "revoke_target_changed"
    resp = await client.post(url, json={"confirm": True, "expected_current_snapshot_id": v["s2"], "reason": "合成理由",
                                        "expected_target_snapshot_id": v["s1"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "rollback" and resp.json()["snapshot_id"] == v["s1"]
    assert (await db_get(TableImport, v["sep"])).revoked is not None
    assert await count(client, v["name"], "日客流") == 31
    # 同一份文件再传（同一个构建）：作废那次的接受理由已被判定为不成立，不再作为「上次接受的理由」给出（第 5 节、H3）
    assert v["trial"]["trial"]["acceptable"], "D26 有要写理由接受的核对"
    raw, fn = DRIFT_CASES["D26"].build(413)
    st = await reupload(client, sid, raw, fn)
    assert st["trial"]["status"] == "needs_decision", st["trial"]["problems"]
    assert st["trial"]["prior_acceptances"] == []


# ==========================================================================
# 清单（7.3）
# ==========================================================================


async def test_manifests_verify_their_hash_and_single_snapshots_get_a_view(client):
    v = await two_periods(client, seed=415)
    sid = v["source_id"]
    resp = await client.get(f"{API}/{sid}/imports/{v['sep']}/manifest")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["kind"] == "import_manifest" and out["verified"] is True and out["content"]["import_id"] == v["sep"]
    resp = await client.get(f"{API}/{sid}/snapshots/{v['s2']}/manifest")
    assert resp.status_code == 200 and resp.json()["kind"] == "snapshot_manifest"
    resp = await client.get(f"{API}/{sid}/snapshots/{v['s1']}/manifest")
    view = resp.json()
    assert view["kind"] == "snapshot_view" and view["artifact_id"] is None
    assert [i["import_id"] for i in view["content"]["imports"]] == [v["aug"]]
    resp = await client.get(f"{API}/{sid}/imports/nope/manifest")
    assert resp.status_code == 404 and resp.json()["code"] == "import_not_found"
    resp = await client.get(f"{API}/{sid}/snapshots/nope/manifest")
    assert resp.status_code == 404 and resp.json()["code"] == "snapshot_not_found"
    # 改掉导入清单的内容：取回时内容哈希对不上 → 409 manifest_tampered
    imp = await db_get(TableImport, v["sep"])
    path = artifact_store._path_of(imp.manifest_artifact)
    os.chmod(path, 0o644)
    path.write_text(path.read_text("utf-8").replace("2026-09-01", "2026-09-02"), "utf-8")
    resp = await client.get(f"{API}/{sid}/imports/{v['sep']}/manifest")
    assert resp.status_code == 409 and resp.json()["code"] == "manifest_tampered"


async def test_every_version_route_is_reached(client):
    """9.1：版本页的每条路由都打一次，确认没被 datasources 的通配路由截走（截走时回的是没有 code 的 404/405）。"""
    v = await two_periods(client, seed=417)
    sid = v["source_id"]
    hits = [
        ("GET", f"{API}/{sid}/snapshots", None, 200),
        ("GET", f"{API}/{sid}/snapshots/{v['s1']}/manifest", None, 200),
        ("GET", f"{API}/{sid}/imports/{v['aug']}/manifest", None, 200),
        ("POST", f"{API}/{sid}/snapshots/{v['s1']}/activate", {}, 422),
        ("POST", f"{API}/{sid}/imports/{v['sep']}/remove", {}, 422),
        ("POST", f"{API}/{sid}/imports/{v['sep']}/revoke-acceptance", {}, 422),
    ]
    for method, url, body, status in hits:
        resp = await client.request(method, url, json=body)
        assert resp.status_code == status, (url, resp.text)
        if status != 200:
            assert resp.json().get("code"), (url, resp.text)
    # 期 1 的两条照旧：导入记录列表、清除原件
    assert (await client.get(f"{API}/{sid}/imports")).status_code == 200
    resp = await client.post(f"{API}/{sid}/imports/{v['aug']}/purge-raw", json={"reason": "  "})
    assert resp.status_code == 422 and resp.json()["code"] == "reason_required"


async def test_retired_and_lost_snapshots_cannot_be_activated(client):
    """已回收的版本 reason_code=retired、数据文件丢了的 file_lost，都不能启用（409 snapshot_unavailable）。"""
    v = await two_periods(client, seed=419)
    sid = v["source_id"]
    snap = await db_get(SourceSnapshot, v["s1"])
    path = Path(snap.db_path)
    moved = path.with_name(path.name + ".bak")
    os.rename(path, moved)
    items = {s["id"]: s for s in await snapshots(client, sid)}
    assert items[v["s1"]]["reason_code"] == "file_lost" and items[v["s1"]]["available"] is False
    assert items[v["s1"]]["db_size"] is None
    resp = await client.post(f"{API}/{sid}/snapshots/{v['s1']}/activate",
                             json={"confirm": True, "expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 409 and resp.json()["code"] == "snapshot_unavailable"
    os.rename(moved, path)
    async with SessionLocal() as session:
        await session.execute(update(SourceSnapshot).where(SourceSnapshot.id == v["s1"])
                              .values(retired_at=utcnow()))
        await session.commit()
    items = {s["id"]: s for s in await snapshots(client, sid)}
    assert items[v["s1"]]["reason_code"] == "retired" and items[v["s1"]]["reason"] == "已回收，无法启用"
    resp = await client.post(f"{API}/{sid}/snapshots/{v['s1']}/activate",
                             json={"confirm": True, "expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 409 and resp.json()["code"] == "snapshot_unavailable"


async def test_removing_a_middle_period_recomputes_the_gap(client):
    """[7 月, 8 月, 9 月] 移除 8 月：两边之间出现空缺，说明换成带空缺的累积句，快照清单的 gaps 记下（7.4 第 5 步）。"""
    name = unique()
    base = await first_import(client, name, seed=421, start=AUG.replace(month=7), days=31)
    sid = base["source_id"]
    ids = {"jul": base["commit"]["import_id"]}
    current = base["commit"]["snapshot_id"]
    for key, start, days, seed in (("aug", AUG, 31, 422), ("sep", SEP, 30, 423)):
        raw, fn = flow_workbook(start, days, seed=seed)
        st = await reupload(client, sid, raw, fn)
        assert st["trial"]["status"] == "passed", st["trial"]["problems"]
        resp = await commit(client, st)
        assert resp.status_code == 201, resp.text
        ids[key], current = resp.json()["import_id"], resp.json()["snapshot_id"]
    resp = await client.post(f"{API}/{sid}/imports/{ids['aug']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": current, "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    snap = await db_get(SourceSnapshot, resp.json()["snapshot_id"])
    manifest = artifact_store.load(snap.manifest_artifact)
    assert manifest["gaps"] == [{"start": "2026-08-01", "end": "2026-08-31"}]
    assert "各期之间有空缺" in snap.schema_cache["tables"]["日客流"]["comment"]
    assert await count(client, name, "日客流") == 61


async def test_revoke_of_a_middle_period_publishes_a_new_version_and_records_revoked(client):
    """[8 月, 9 月 D26（写理由接受）, 10 月] 作废 9 月：剩下 [8 月, 10 月] 没有现成的版本，按目标配方新物化一份；
    revoked 与切换在同一个事务里写进去，含 9 月的版本都不能再启用。"""
    from tests.fixtures.xlsx.drift import OCT

    v = await accepted_september(client, seed=425, mode="accumulate")
    sid = v["source_id"]
    raw, fn = flow_workbook(OCT, 31, seed=426)
    st = await reupload(client, sid, raw, fn)
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    s3 = resp.json()["snapshot_id"]
    resp = await client.post(f"{API}/{sid}/imports/{v['sep']}/revoke-acceptance",
                             json={"confirm": True, "expected_current_snapshot_id": s3, "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["action"] == "remove_period" and out["snapshot_id"] not in (v["s1"], v["s2"], s3)
    imp = await db_get(TableImport, v["sep"])
    assert imp.revoked is not None and imp.status == "superseded"
    snap = await db_get(SourceSnapshot, out["snapshot_id"])
    assert len(snap.imports) == 2 and artifact_store.load(snap.manifest_artifact)["action"] == "revoke_acceptance"
    for old in (v["s2"], s3):
        resp = await client.post(f"{API}/{sid}/snapshots/{old}/activate",
                                 json={"confirm": True, "expected_current_snapshot_id": out["snapshot_id"]})
        assert resp.status_code == 409 and resp.json()["code"] == "contains_revoked"
    assert await count(client, v["name"], "日客流") == 62


# ==========================================================================
# 评审后补：锁外读到的当前版本与确认框绑定（ABA）、过期的 expected_current、移除时复活、并集只取决于剩下的各期、
# 移除坏掉的正是那一期、原件共用的另一个源已删除
# ==========================================================================

NOV = SEP.replace(month=11)


async def three_periods(client, *, seed: int) -> dict[str, Any]:
    """8 月、9 月、10 月按期累积（同一个配方）：S1=[8]、S2=[8, 9]、S3=[8, 9, 10]。"""
    from tests.fixtures.xlsx.drift import OCT

    v = await two_periods(client, seed=seed)
    raw, fn = flow_workbook(OCT, 31, seed=seed + 2)
    st = await reupload(client, v["source_id"], raw, fn)
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    return {**v, "s3": resp.json()["snapshot_id"], "oct": resp.json()["import_id"]}


async def current_periods(source_id: str) -> list[str]:
    async with SessionLocal() as session:
        src = await session.get(DataSource, source_id)
        snap = await session.get(SourceSnapshot, src.current_snapshot_id)
        rows = {i.id: i for i in (await session.execute(
            select(TableImport).where(TableImport.id.in_(snap.imports)))).scalars()}
        return [rows[i].period_start for i in snap.imports]


async def test_revoke_with_a_stale_expected_current_is_base_changed_in_both_modes(client):
    """作废接受带过期的 expected_current：两种模式都 409 base_changed，revoked 不写、指针不动（11.4「base_changed
    在锁内」）。替换模式的回滚目标恰好等于过期的那个版本时也一样，不能悄悄切过去。"""
    for mode, seed in (("accumulate", 431), ("replace", 433)):
        v = await accepted_september(client, seed=seed, mode=mode)
        sid = v["source_id"]
        body = {"confirm": True, "expected_current_snapshot_id": v["s1"], "reason": "合成理由"}
        if mode == "replace":
            body["expected_target_snapshot_id"] = v["s1"]
        resp = await client.post(f"{API}/{sid}/imports/{v['sep']}/revoke-acceptance", json=body)
        assert resp.status_code == 409 and resp.json()["code"] == "base_changed", (mode, resp.text)
        assert (await db_get(TableImport, v["sep"])).revoked is None, mode
        assert (await db_get(DataSource, sid)).current_snapshot_id == v["s2"], mode


async def test_removing_a_middle_period_with_a_stale_expected_current_is_base_changed(client, store):
    """移除中间一期（要物化）带过期的 expected_current：409 base_changed，当前版本三期都在，不留并集临时文件。"""
    v = await three_periods(client, seed=435)
    resp = await client.post(f"{API}/{v['source_id']}/imports/{v['sep']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s2"], "reason": "合成理由"})
    assert resp.status_code == 409 and resp.json()["code"] == "base_changed", resp.text
    assert (await db_get(DataSource, v["source_id"])).current_snapshot_id == v["s3"]
    assert list((store / "uploads").rglob("*.tmp-*")) == []


async def test_remove_binds_the_version_it_read_to_the_confirmed_one(client, monkeypatch):
    """ABA（评审意见）：用户看到的当前版本是 E=[8, 9, 10]；别人先移除 10 月，当前变成 C=[8, 9]；用户随后按 E 发「移除
    9 月」。处理器读到的是 C，算出 keep=[8]；若处理中又有人把 E 启用回来，锁内 E==E 照样通过，结果只剩 [8]，10 月
    悄悄丢了。必须在读到当前版本之后立刻比对 expected_current，409 base_changed，一个切换都不做。"""
    from app.data import table_versions

    v = await three_periods(client, seed=437)
    sid = v["source_id"]
    resp = await client.post(f"{API}/{sid}/imports/{v['oct']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s3"], "reason": "合成理由：别人"})
    assert resp.status_code == 200, resp.text
    c = resp.json()["snapshot_id"]
    assert c == v["s2"]
    orig = table_versions.activate_snapshot
    fired: list[str] = []

    async def activate(session, source, snapshot_id, **kw):
        if kw.get("kind") == "remove_period" and not fired:
            fired.append(snapshot_id)
            async with SessionLocal() as other:
                src = await other.get(DataSource, sid)
                await orig(other, src, v["s3"], expected_current=c, kind="activate", reason="合成：回到 E",
                           signed_by="合成署名乙")
        return await orig(session, source, snapshot_id, **kw)

    monkeypatch.setattr(table_versions, "activate_snapshot", activate)
    resp = await client.post(f"{API}/{sid}/imports/{v['sep']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s3"], "reason": "合成理由：用户"})
    assert resp.status_code == 409 and resp.json()["code"] == "base_changed", resp.text
    assert fired == [], "比对在任何切换之前"
    assert await current_periods(sid) == ["2026-08-01", "2026-09-01"]


async def test_a_commit_landing_during_the_materialization_makes_the_remove_base_changed(client, monkeypatch, store):
    """移除中间一期停在物化里，期间 11 月的提交先完成：移除 409 base_changed（锁内比对），当前版本四期都在，物化出的
    临时文件收拾掉。"""
    import asyncio

    from app.data import source_versions

    v = await three_periods(client, seed=439)
    sid = v["source_id"]
    raw, fn = flow_workbook(NOV, 30, seed=442)
    nov = await reupload(client, sid, raw, fn)
    assert nov["trial"]["status"] == "passed" and nov["trial"]["accumulate"]["action"] == "append"
    entered, release = asyncio.Event(), asyncio.Event()
    orig = source_versions.table_versions_slot

    async def slot(fn, *a, **kw):
        if getattr(fn, "__name__", "") == "materialize_union":
            entered.set()
            await release.wait()
        return await orig(fn, *a, **kw)

    monkeypatch.setattr(source_versions, "table_versions_slot", slot)
    task = asyncio.create_task(client.post(f"{API}/{sid}/imports/{v['sep']}/remove", json={
        "confirm": True, "expected_current_snapshot_id": v["s3"], "reason": "合成理由"}))
    await asyncio.wait_for(entered.wait(), 30)
    resp = await commit(client, nov)
    assert resp.status_code == 201 and resp.json()["parts"] == 4, resp.text
    release.set()
    resp = await task
    assert resp.status_code == 409 and resp.json()["code"] == "base_changed", resp.text
    assert await current_periods(sid) == ["2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01"]
    assert list((store / "uploads").rglob("*.tmp-*")) == []


async def test_remove_revives_a_retired_snapshot_of_the_same_imports(client, monkeypatch):
    """SNAPSHOT_KEEP=0：9 月提交后 [8 月] 的 S1 被回收；移除 9 月按同一组导入、同一个目标配方得到 S1 的 id，复活它
    （7.4 第 4 步、2.6 发布第 4 步），不新建快照。"""
    from app.data import table_versions

    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 0)
    v = await two_periods(client, seed=443)
    assert (await db_get(SourceSnapshot, v["s1"])).retired_at is not None
    resp = await client.post(f"{API}/{v['source_id']}/imports/{v['sep']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s2"], "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["snapshot_id"] == v["s1"] and resp.json()["reused"] is True
    assert (await db_get(SourceSnapshot, v["s1"])).retired_at is None
    assert await count(client, v["name"], "日客流") == 31


async def _fix_until_passed(client, st: dict[str, Any]) -> dict[str, Any]:
    """按提议依次修（加、去标签，更新关系成员），直到试运行通过。"""
    from tests.test_table_imports_api_p3 import fixed

    for _ in range(6):
        trial_ok = (st.get("trial") or {}).get("status") == "passed"
        if trial_ok and not st["recipe_problems"]:
            return st
        kinds = [f["kind"] for f in st["fixes"]]
        kind = next((k for k in ("add_label", "remove_label", "edit_members") if k in kinds), None)
        if kind is None:
            st = await trial(client, st["id"])
            continue
        fx = next(f for f in st["fixes"] if f["kind"] == kind)
        opt = fx["options"][0]
        st = await fixed(client, st, kind, opt["value"], reason="合成理由" if opt["needs_reason"] else None)
        if not st["recipe_problems"]:
            st = await trial(client, st["id"])
    return st


async def test_removing_the_only_period_with_a_column_leaves_no_phantom_retired_column(client):
    """并集只取决于（剩下各期, 目标配方）：8 月 R0；9 月 D13 用修复加「分区丙」（R1）；10 月修复去掉「分区丙」（R2，
    退役）；移除 9 月之后剩下的 8 月、10 月都没有分区丙，新并集不该再有这一列、快照清单也不记它（评审意见：原来按
    当前并集的表结构推退役列，出现全为空值的「幽灵」列，since 还写成第一期）。"""
    from tests.fixtures.xlsx.drift import OCT

    name = unique()
    base = await first_import(client, name, seed=445)
    sid = base["source_id"]
    raw, fn = DRIFT_CASES["D13"].build(445)
    st = await _fix_until_passed(client, await reupload(client, sid, raw, fn))
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    sep = resp.json()["import_id"]
    raw, fn = flow_workbook(OCT, 31, seed=446)
    st = await _fix_until_passed(client, await reupload(client, sid, raw, fn))
    assert st["trial"]["status"] == "passed", (st["trial"]["problems"], st["recipe_problems"])
    assert {"table": "日客流", "column": "分区丙"} in st["trial"]["accumulate"]["retired_new"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    s3 = resp.json()["snapshot_id"]
    snap = await db_get(SourceSnapshot, s3)
    assert "分区丙" in [c["name"] for c in snap.schema_cache["tables"]["日客流"]["columns"]], "9 月的分区丙照旧保留"

    resp = await client.post(f"{API}/{sid}/imports/{sep}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": s3, "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    snap = await db_get(SourceSnapshot, resp.json()["snapshot_id"])
    assert "分区丙" not in [c["name"] for c in snap.schema_cache["tables"]["日客流"]["columns"]]
    assert "分区丙" not in artifact_store.load(snap.manifest_artifact)["columns"].get("日客流", {})
    assert await count(client, name, "日客流") == 62


async def test_removing_back_to_an_earlier_set_revives_it_even_after_the_recipe_returned(client, monkeypatch):
    """SNAPSHOT_KEEP=0。8 月、10 月按 R0 得 S_ao（已回收）→ 补传 9 月 D13（修复加分区丙，R1）→ 11 月修复去掉分区丙（R2，
    与 R0 逐字相同、记录不同）→ 移除 9 月 → 移除 11 月：剩 [8 月, 10 月]、目标 R2，快照 id 正是 S_ao。复活它，不报
    snapshot_conflict（评审意见：原来并集带着幽灵列、id 不同；配方记录 id 也不同）。现行配方回到 S_ao 记的那条，内容不变。"""
    from app.data import table_versions
    from app.db.models import TableRecipe
    from tests.fixtures.xlsx.drift import OCT

    monkeypatch.setattr(table_versions, "SNAPSHOT_KEEP", 0)
    name = unique()
    base = await first_import(client, name, seed=447)
    sid = base["source_id"]
    raw, fn = flow_workbook(OCT, 31, seed=448)
    resp = await commit(client, await reupload(client, sid, raw, fn))
    assert resp.status_code == 201, resp.text
    s_ao, r0 = resp.json()["snapshot_id"], resp.json()["recipe_id"]
    raw, fn = DRIFT_CASES["D13"].build(449)
    st = await _fix_until_passed(client, await reupload(client, sid, raw, fn))
    assert st["trial"]["status"] == "passed" and st["trial"]["accumulate"]["backfill"] is True
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    sep = resp.json()["import_id"]
    raw, fn = flow_workbook(NOV, 30, seed=450)
    st = await _fix_until_passed(client, await reupload(client, sid, raw, fn))
    assert st["trial"]["status"] == "passed", (st["trial"]["problems"], st["recipe_problems"])
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    nov, cur, r2 = resp.json()["import_id"], resp.json()["snapshot_id"], resp.json()["recipe_id"]
    assert r2 != r0
    assert (await db_get(TableRecipe, r2)).recipe_sha256 == (await db_get(TableRecipe, r0)).recipe_sha256
    resp = await client.post(f"{API}/{sid}/imports/{sep}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": cur, "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    assert (await db_get(SourceSnapshot, s_ao)).retired_at is not None
    resp = await client.post(f"{API}/{sid}/imports/{nov}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": resp.json()["snapshot_id"],
                                   "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["snapshot_id"] == s_ao and resp.json()["reused"] is True
    snap = await db_get(SourceSnapshot, s_ao)
    assert snap.retired_at is None
    assert (await db_get(DataSource, sid)).current_recipe_id == r0
    assert await count(client, name, "日客流") == 62


async def test_removing_the_broken_period_itself_is_allowed(client):
    """被移除的那一期先排除、不校验（7.4「移除的正好是那一期时……不报这个错」）：它的构建记录不在了、或者统计期库列
    被改过，照样能把它移出去；坏的是别的期时照旧拒绝（评审意见：原来 part_missing 的消息建议「先移除这一期」，却走
    不通）。"""
    from sqlalchemy import delete

    from app.db.models import TableBuild

    v = await three_periods(client, seed=451)
    imp = await db_get(TableImport, v["sep"])
    async with SessionLocal() as session:
        await session.execute(delete(TableBuild).where(TableBuild.id == imp.build_id))
        await session.commit()
    resp = await client.post(f"{API}/{v['source_id']}/imports/{v['oct']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s3"], "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["snapshot_id"] == v["s2"], "复用 [8, 9] 的现成版本，不经组装"
    resp = await client.post(f"{API}/{v['source_id']}/snapshots/{v['s3']}/activate",
                             json={"confirm": True, "expected_current_snapshot_id": v["s2"]})
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"{API}/{v['source_id']}/imports/{v['aug']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s3"], "reason": "合成理由"})
    assert resp.status_code == 409 and resp.json()["code"] == "part_missing", "坏的是别的期：照旧拒绝"
    assert "可以先移除这一期" in resp.json()["detail"]
    resp = await client.post(f"{API}/{v['source_id']}/imports/{v['sep']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": v["s3"], "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    assert await current_periods(v["source_id"]) == ["2026-08-01", "2026-10-01"]

    w = await three_periods(client, seed=453)
    async with SessionLocal() as session:
        await session.execute(update(TableImport).where(TableImport.id == w["sep"]).values(period_start="2026-09-02"))
        await session.commit()
    resp = await client.post(f"{API}/{w['source_id']}/imports/{w['sep']}/remove",
                             json={"confirm": True, "expected_current_snapshot_id": w["s3"], "reason": "合成理由"})
    assert resp.status_code == 200, resp.text
    assert await current_periods(w["source_id"]) == ["2026-08-01", "2026-10-01"]


async def test_raw_shared_with_names_a_deleted_source_instead_of_null(client):
    """同一份原件传给两个源，删掉其中一个：另一个源的 raw_shared_with 仍列出这条引用（原件还在、清除时会一并清除），
    名字给「已删除的数据源」而不是 null（界面的类型是 string，直接拼进文案，评审意见）。"""
    from tests.test_p3_api_errors import sheet_bytes, upload

    raw = sheet_bytes(455)
    ra, rb = await upload(client, unique("sha"), raw), await upload(client, unique("shb"), raw)
    assert ra.status_code == 201 and rb.status_code == 201, (ra.text, rb.text)
    sid_a, b = ra.json()["source"]["id"], rb.json()["source"]
    [rec] = await imports(client, sid_a)
    assert rec["raw_shared_with"] == [{"source_name": b["name"], "count": 1}]
    assert (await client.delete(f"{API}/{b['id']}")).status_code == 204
    [rec] = await imports(client, sid_a)
    assert rec["raw_shared_with"] == [{"source_name": "已删除的数据源", "count": 1}]
