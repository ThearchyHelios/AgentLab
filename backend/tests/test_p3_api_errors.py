"""期 3 接口的错误码与遗留项（WP-5，P3-SPEC 9.1、第 8 节遗留项 1、5）：真管线、经 HTTP。

- 遗留项 1（接口部分）：重传撞上被改过的构建文件，用这次的文件恢复（build_restored=true）；记录的哈希也被改掉时
  409 build_conflict、指针不动。/upload 和配方提交各一例；
- 遗留项 5（接口部分）：另一个进程占着版本存储的守卫时，写版本存储的接口一律 503 store_unavailable（body 照样是
  {detail, code}），只读的查询照常，过期处理跳过不抛；
- 修改请求的形状（edit_invalid）、PUT 的 origin、redraft_mismatch、undo_stale、试运行的 part_tampered / part_missing、
  提交的 union_tampered、清除原件的机读码。

夹具一律合成、假名、随机数；不调用任何模型。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import update

from app.core.config import settings
from app.data import table_versions
from app.db.base import SessionLocal
from app.db.models import DataSource, ImportStaging, SourceSnapshot, TableBuild, TableImport
from tests.fixtures.xlsx.drift import DRIFT_CASES, OCT, SEP
from tests.fixtures.xlsx.flow import flow_workbook
from tests.test_table_imports_api_p3 import (  # noqa: F401 - client、store 是夹具
    API, XLSX, client, commit, db_get, first_import, reupload, store, trial, unique,
)


def sheet_bytes(seed: int) -> bytes:
    """一份最简单的表格（期 1 的简单上传用）：表头加两行，数字随机。"""
    import random

    rnd = random.Random(seed)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "甲表"
    ws.append(["分区", "数量"])
    ws.append(["分区甲", rnd.randint(1, 999)])
    ws.append(["分区乙", rnd.randint(1, 999)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def upload(client, name: str, raw: bytes):
    return await client.post(f"{API}/upload", files={"file": ("甲表.xlsx", raw, XLSX)}, data={"name": name})


def corrupt(path: Path, data: bytes = b"") -> None:
    os.chmod(path, 0o644)
    path.write_bytes(data)


# ==========================================================================
# 遗留项 1：被改过的构建文件（接口部分）
# ==========================================================================


async def test_upload_restores_a_tampered_build_and_reports_build_restored(client, store):
    name, raw = unique(), sheet_bytes(501)
    first = (await upload(client, name, raw)).json()
    assert first["build_restored"] is False
    snap = await db_get(SourceSnapshot, first["snapshot_id"])
    corrupt(Path(snap.db_path))
    resp = await upload(client, name, raw)
    assert resp.status_code == 201, resp.text
    assert resp.json()["build_restored"] is True
    moved = list((store / "uploads" / "quarantine" / first["source"]["id"]).rglob("*.db"))
    assert len(moved) == 1


async def test_upload_build_conflict_is_a_409_and_keeps_the_pointer(client):
    name, raw = unique(), sheet_bytes(502)
    first = (await upload(client, name, raw)).json()
    snap = await db_get(SourceSnapshot, first["snapshot_id"])
    imp = await db_get(TableImport, first["import_id"])
    corrupt(Path(snap.db_path))
    async with SessionLocal() as session:
        await session.execute(update(TableBuild).where(TableBuild.id == imp.build_id).values(db_sha256="0" * 64))
        await session.commit()
    resp = await upload(client, name, raw)
    assert resp.status_code == 409 and resp.json()["code"] == "build_conflict"
    src = await db_get(DataSource, first["source"]["id"])
    assert src.current_snapshot_id == first["snapshot_id"]


async def test_recipe_commit_restores_a_tampered_build_and_reports_build_restored(client):
    """配方提交（publish_build 的路径）：8 月 → 9 月 → 8 月原文件再传（构建与第一次相同），第一次的构建文件被改成
    0 字节：用这次试运行的文件恢复，CommitOut.build_restored=true。"""
    name = unique()
    base = await first_import(client, name, seed=503, mode="replace")
    sid = base["source_id"]
    raw9, fn9 = flow_workbook(SEP, 30, seed=504)
    st = await reupload(client, sid, raw9, fn9)
    assert (await commit(client, st)).status_code == 201
    imp = await db_get(TableImport, base["commit"]["import_id"])
    build = await db_get(TableBuild, imp.build_id)
    corrupt(Path(build.db_path))
    st = await reupload(client, sid, base["raw"], base["file_name"])
    assert st["trial"]["status"] == "passed", st["trial"]["problems"]
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["build_restored"] is True and out["build_id"] == imp.build_id and out["parts"] == 1


# ==========================================================================
# 遗留项 5：只读进程（接口部分）
# ==========================================================================

_HOLD = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def held_guard(store):
    """另一个进程占着 uploads/.store.lock：本进程取守卫拿不到，进入只读。用完放掉、复原守卫状态。"""
    path = settings.uploads_dir / ".store.lock"
    proc = subprocess.Popen([sys.executable, "-c", _HOLD, str(path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    yield proc
    proc.stdin.close()
    proc.wait(timeout=10)
    table_versions._release_guard()


async def test_read_only_process_refuses_writes_with_503_and_keeps_reads(client, held_guard):
    name = unique()
    base = await first_import(client, name, seed=505)
    sid = base["source_id"]
    raw9, fn9 = flow_workbook(SEP, 30, seed=506)
    st = await reupload(client, sid, raw9, fn9)
    assert table_versions.acquire_store_guard() is False

    def refused(resp) -> None:
        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "store_unavailable" and "单进程" in resp.json()["detail"]

    refused(await upload(client, unique(), sheet_bytes(507)))
    refused(await client.post(f"{API}/imports/stage", files={"file": (fn9, raw9, XLSX)}, data={"name": unique()}))
    refused(await client.post(f"{API}/{sid}/reupload", files={"file": (fn9, raw9, XLSX)}))
    refused(await client.post(f"{API}/imports/{st['id']}/trial", json={}))
    refused(await commit(client, st))
    refused(await client.post(f"{API}/imports/{st['id']}/answers", json={"answers": {}}))
    refused(await client.put(f"{API}/imports/{st['id']}/recipe", json={"recipe": st["recipe"]}))
    refused(await client.post(f"{API}/imports/{st['id']}/redraft-rules", json={}))
    refused(await client.delete(f"{API}/imports/{st['id']}"))
    refused(await client.post(f"{API}/{sid}/imports/{base['commit']['import_id']}/purge-raw",
                              json={"reason": "合成理由"}))
    refused(await client.delete(f"{API}/{sid}"))
    # 只读的查询照常；查看暂存区会顺手做过期处理，只读进程里跳过、不抛
    assert (await client.get(API)).status_code == 200
    assert (await client.get(f"{API}/{sid}/snapshots")).status_code == 200
    assert (await client.get(f"{API}/{sid}/imports")).status_code == 200
    got = await client.get(f"{API}/imports/{st['id']}")
    assert got.status_code == 200 and got.json()["status"] == "trialed"
    async with SessionLocal() as session:
        assert await table_versions.expire_stagings(session) == []


async def test_read_only_process_refuses_activate_and_commit_keeps_the_trial(client, held_guard):
    """启用旧版本 503；提交 503 时不消耗试运行库（守卫拿回来之后照常提交）。"""
    name = unique()
    base = await first_import(client, name, seed=508)
    sid = base["source_id"]
    raw9, fn9 = flow_workbook(SEP, 30, seed=509)
    st = await reupload(client, sid, raw9, fn9)
    s2 = (await commit(client, st)).json()["snapshot_id"]
    raw10, fn10 = flow_workbook(OCT, 31, seed=510)
    st = await reupload(client, sid, raw10, fn10)
    assert table_versions.acquire_store_guard() is False
    resp = await client.post(f"{API}/{sid}/snapshots/{base['commit']['snapshot_id']}/activate",
                             json={"confirm": True, "expected_current_snapshot_id": s2})
    assert resp.status_code == 503 and resp.json()["code"] == "store_unavailable"
    resp = await commit(client, st)
    assert resp.status_code == 503
    row = await db_get(ImportStaging, st["id"])
    assert row.status == "trialed" and Path(row.trial_path).is_file(), "只读进程不消耗试运行库"
    held_guard.stdin.close()
    held_guard.wait(timeout=10)
    assert table_versions.acquire_store_guard() is True
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    assert resp.json()["parts"] == 3


async def test_read_only_process_refuses_version_and_edit_writes_before_doing_any_work(client, held_guard, store,
                                                                                    monkeypatch):
    """只读进程：移除一期（要物化）、作废接受（累积模式，要物化）、edits/apply、edits/undo、POST /{id}/redraft 一律 503，
    而且在组装各期、物化并集之前就停下：版本存储里不留任何临时文件（第 8 节遗留项 5「本进程不写入版本存储」，评审
    意见：原来先物化、写进 snapshots/ 之后才由 publish_snapshot 拒绝）。"""
    from app.data import source_versions
    from tests.test_source_versions_api import accepted_september
    from tests.test_table_imports_api_p3 import apply, preview

    v = await accepted_september(client, seed=521, mode="accumulate")
    sid = v["source_id"]
    raw10, fn10 = flow_workbook(OCT, 31, seed=522)
    resp = await commit(client, await reupload(client, sid, raw10, fn10))
    assert resp.status_code == 201, resp.text
    s3 = resp.json()["snapshot_id"]
    # 一个已应用修复的暂存区（撤销用），另一个源上一个预览好、还没应用的修复（应用用）
    raw, fn = DRIFT_CASES["D04"].build(523)
    applied = await reupload(client, sid, raw, fn)
    fx = next(f for f in applied["fixes"] if f["kind"] == "remove_label")
    body = {"fix": {"id": fx["id"], "option": "remove"}}
    pv = (await preview(client, applied["id"], body)).json()
    assert (await apply(client, applied["id"], body, pv["recipe_sha256_after"])).status_code == 200
    other = await first_import(client, unique(), seed=524)
    pending = await reupload(client, other["source_id"], raw, fn)
    fx2 = next(f for f in pending["fixes"] if f["kind"] == "remove_label")
    body2 = {"fix": {"id": fx2["id"], "option": "remove"}}
    pv2 = (await preview(client, pending["id"], body2)).json()
    assert pv2["ok"], pv2

    calls: list[str] = []
    orig = source_versions.table_versions_slot

    async def slot(fn, *a, **kw):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await orig(fn, *a, **kw)

    monkeypatch.setattr(source_versions, "table_versions_slot", slot)
    assert table_versions.acquire_store_guard() is False

    def refused(resp) -> None:
        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "store_unavailable"

    version_body = {"confirm": True, "expected_current_snapshot_id": s3, "reason": "合成理由"}
    refused(await client.post(f"{API}/{sid}/imports/{v['sep']}/remove", json=version_body))
    refused(await client.post(f"{API}/{sid}/imports/{v['sep']}/revoke-acceptance", json=version_body))
    refused(await client.post(f"{API}/imports/{applied['id']}/edits/undo", json={}))
    refused(await apply(client, pending["id"], body2, pv2["recipe_sha256_after"]))
    refused(await client.post(f"{API}/{sid}/redraft", json={}))
    assert calls == [], "只读进程不该开始物化"
    assert list((store / "uploads").rglob("*.tmp-*")) == []
    assert (await db_get(TableImport, v["sep"])).revoked is None
    async with SessionLocal() as session:
        assert (await session.get(DataSource, sid)).current_snapshot_id == s3
    assert len((await db_get(ImportStaging, applied["id"])).edits) == 1, "撤销没有生效"


async def test_read_only_process_still_deletes_a_manual_source(client, held_guard):
    """手工登记的源没有导入记录、配方、暂存区和隔离文件，不碰版本存储：只读进程里照常删除（9.1 只要求上传源 503）。"""
    sid = "manual-" + unique("m")
    async with SessionLocal() as session:
        session.add(DataSource(id=sid, name=unique("man"), kind="sqlite", database="/tmp/none.db", origin="manual"))
        await session.commit()
    assert table_versions.acquire_store_guard() is False
    resp = await client.delete(f"{API}/{sid}")
    assert resp.status_code == 204, resp.text
    assert await db_get(DataSource, sid) is None


# ==========================================================================
# 修改请求、PUT、撤销
# ==========================================================================


async def test_edit_request_shape_errors_are_edit_invalid(client):
    name = unique()
    base = await first_import(client, name, seed=511)
    raw, fn = DRIFT_CASES["D04"].build(511)
    st = await reupload(client, base["source_id"], raw, fn)
    fx = next(f for f in st["fixes"] if f["kind"] == "remove_label")
    url = f"{API}/imports/{st['id']}/edits/preview"
    bad = [
        {},
        {"fix": {"id": fx["id"], "option": "remove"}, "selection": {"sheet": "客流汇总", "ref": "B5", "as": "list"}},
        {"fix": {"id": fx["id"]}},
        {"fix": {"id": fx["id"], "option": "nope"}},
        {"selection": {"sheet": "客流汇总", "ref": "b5:c6", "as": "list"}},
        {"selection": {"sheet": "客流汇总", "ref": "B5", "as": "picture"}},
        {"selection": {"sheet": "没有这张表", "ref": "B5:C6", "as": "list"}},
    ]
    for body in bad:
        resp = await client.post(url, json=body)
        assert resp.status_code == 422 and resp.json()["code"] == "edit_invalid", (body, resp.text)
    resp = await client.post(url, json={"fix": {"id": "fx-000000000000", "option": "remove"}})
    assert resp.status_code == 409 and resp.json()["code"] == "fix_stale"
    resp = await client.post(f"{API}/imports/{st['id']}/edits/apply",
                             json={"fix": {"id": fx["id"], "option": "remove"}, "expected_sha256": "0" * 64})
    assert resp.status_code == 409 and resp.json()["code"] == "edit_stale"
    resp = await client.post(f"{API}/imports/nope/edits/preview", json={"fix": {"id": fx["id"], "option": "remove"}})
    assert resp.status_code == 404 and resp.json()["code"] == "staging_not_found"


async def test_put_origin_and_redraft_mismatch_and_undo_stale(client):
    name = unique()
    base = await first_import(client, name, seed=512)
    raw, fn = DRIFT_CASES["D04"].build(512)
    st = await reupload(client, base["source_id"], raw, fn)
    url = f"{API}/imports/{st['id']}/recipe"
    resp = await client.put(url, json={"recipe": st["recipe"], "origin": "ai"})
    assert resp.status_code == 422 and resp.json()["code"] == "body_invalid"
    resp = await client.put(url, json={"recipe": st["recipe"], "origin": "rules_redraft"})
    assert resp.status_code == 409 and resp.json()["code"] == "redraft_mismatch", "没有重新起草过就不能按采用记"
    fx = next(f for f in st["fixes"] if f["kind"] == "remove_label")
    body = {"fix": {"id": fx["id"], "option": "remove"}}
    pv = (await client.post(f"{API}/imports/{st['id']}/edits/preview", json=body)).json()
    resp = await client.post(f"{API}/imports/{st['id']}/edits/apply",
                             json={**body, "expected_sha256": pv["recipe_sha256_after"]})
    assert resp.status_code == 200, resp.text
    # 起点在修改之后又被改过（正常流程不会出现：PUT 会清空撤销栈），撤销回 undo_stale 兜底
    async with SessionLocal() as session:
        row = await session.get(ImportStaging, st["id"])
        changed = dict(row.answers_base)
        changed["tables"] = [{**t, "note": "合成说明"} if i == 0 else t for i, t in enumerate(changed["tables"])]
        row.answers_base = changed
        await session.commit()
    resp = await client.post(f"{API}/imports/{st['id']}/edits/undo", json={})
    assert resp.status_code == 409 and resp.json()["code"] == "undo_stale"


# ==========================================================================
# 按期累积：被改过、不在了的各期，被改过的并集试运行文件
# ==========================================================================


async def accumulated(client, *, seed: int) -> dict[str, str]:
    name = unique()
    base = await first_import(client, name, seed=seed)
    raw9, fn9 = flow_workbook(SEP, 30, seed=seed + 1)
    st = await reupload(client, base["source_id"], raw9, fn9)
    resp = await commit(client, st)
    assert resp.status_code == 201, resp.text
    return {"name": name, "source_id": base["source_id"], "aug": base["commit"]["import_id"],
            "sep": resp.json()["import_id"], "s2": resp.json()["snapshot_id"]}


async def test_tampered_period_column_rejects_the_trial_with_part_tampered(client):
    """某一期导入记录的 period_start 库列被改：统计期取自导入清单，对不上 → 试运行拒收（11.4 数据变异）。"""
    v = await accumulated(client, seed=513)
    async with SessionLocal() as session:
        await session.execute(update(TableImport).where(TableImport.id == v["aug"]).values(period_start="2026-08-02"))
        await session.commit()
    raw, fn = flow_workbook(OCT, 31, seed=515)
    st = await reupload(client, v["source_id"], raw, fn)
    assert st["trial"]["status"] == "rejected"
    tampered = next(p for p in st["trial"]["problems"] if p["code"] == "part_tampered")
    assert tampered["category"] == "structure" and "统计期" in tampered["message"]


async def test_tampered_part_file_rejects_and_missing_part_file_is_a_409(client):
    """已发布的某一期构建库被改字节 → 试运行拒收 part_tampered；构建库不在了 → 409 part_missing，消息指向「版本」。"""
    v = await accumulated(client, seed=516)
    imp = await db_get(TableImport, v["aug"])
    build = await db_get(TableBuild, imp.build_id)
    path = Path(build.db_path)
    original = path.read_bytes()
    corrupt(path, original[:-1] + bytes([original[-1] ^ 1]))
    raw, fn = flow_workbook(OCT, 31, seed=518)
    st = await reupload(client, v["source_id"], raw, fn)
    assert st["trial"]["status"] == "rejected"
    assert "part_tampered" in {p["code"] for p in st["trial"]["problems"]}
    path.unlink()
    resp = await client.post(f"{API}/imports/{st['id']}/trial", json={})
    assert resp.status_code == 409 and resp.json()["code"] == "part_missing"
    assert "版本" in resp.json()["detail"]


async def test_tampered_or_missing_union_trial_file_is_refused_at_commit(client):
    name = unique()
    base = await first_import(client, name, seed=519)
    raw9, fn9 = flow_workbook(SEP, 30, seed=520)
    st = await reupload(client, base["source_id"], raw9, fn9)
    union = table_versions.union_trial_path(Path((await db_get(ImportStaging, st["id"])).trial_path))
    with open(union, "ab") as f:
        f.write(b"\0")
    resp = await commit(client, st)
    assert resp.status_code == 409 and resp.json()["code"] == "union_tampered"
    st = await trial(client, st["id"])
    union = table_versions.union_trial_path(Path((await db_get(ImportStaging, st["id"])).trial_path))
    union.unlink()
    resp = await commit(client, st)
    assert resp.status_code == 409 and resp.json()["code"] == "trial_required"
    assert (await db_get(ImportStaging, st["id"])).status == "drafting"


# ==========================================================================
# 清除原件（7.6）的机读码
# ==========================================================================


async def test_purge_raw_errors_carry_codes(client):
    name, raw = unique(), sheet_bytes(521)
    first = (await upload(client, name, raw)).json()
    sid, iid = first["source"]["id"], first["import_id"]
    url = f"{API}/{sid}/imports/{iid}/purge-raw"
    resp = await client.post(url, json={"reason": "x", "confirm": False})
    assert resp.status_code == 422 and resp.json()["code"] == "confirm_required"
    resp = await client.post(url, json={})
    assert resp.status_code == 422 and resp.json()["code"] == "reason_required"
    resp = await client.post(url, content=b"[1]", headers={"content-type": "application/json"})
    assert resp.status_code == 422 and resp.json()["code"] == "body_invalid"
    resp = await client.post(f"{API}/{sid}/imports/nope/purge-raw", json={"reason": "x"})
    assert resp.status_code == 404 and resp.json()["code"] == "import_not_found"
    resp = await client.post(url, json={"reason": "合成理由", "confirm": True}, headers={"X-Actor": "header-actor"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["import"]["purged"]["signed_by"] == "header-actor" and out["import"]["raw_state"] == "purged"
    resp = await client.post(url, json={"reason": "再来"})
    assert resp.status_code == 409 and resp.json()["code"] == "raw_already_purged"
    async with SessionLocal() as session:
        await session.execute(update(TableImport).where(TableImport.id == iid).values(raw_state="absent"))
        await session.commit()
    resp = await client.post(url, json={"reason": "x"})
    assert resp.status_code == 409 and resp.json()["code"] == "raw_absent"
