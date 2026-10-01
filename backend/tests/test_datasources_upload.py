"""上传表格的接口：发布成版本、要用户先做决定时回 422、不可变的强制、导入记录与清除原件。

库层面的发布、回收、迁移在 test_table_versions.py；这里看接口的形状和限制。解析器替身、
数据目录隔离两个夹具从那边拿（autouse，导入即生效）。
"""
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.data import raw_store, table_versions, tabular
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, SourceSnapshot, TableImport
from app.main import app
from tests.test_table_versions import (  # noqa: F401 - parser、store 是 autouse 夹具
    V1, V2, fresh, imports_of, parser, scalar, store, unique, xlsx,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def upload(client, name, content, filename="客流.xlsx", **form):
    return await client.post(
        "/api/datasources/upload",
        files={"file": (filename, content, XLSX)},
        data={"name": name, **{k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in form.items()}},
    )


async def by_name(name: str) -> DataSource | None:
    async with SessionLocal() as session:
        return (await session.execute(select(DataSource).where(DataSource.name == name))).scalar_one_or_none()


async def resolve(source_id: str, pinned: str | None = None):
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        return await table_versions.resolve_source(session, row, pinned)


# --------------------------------------------------------------------------
# 上传
# --------------------------------------------------------------------------


async def test_upload_response_carries_the_receipt(client):
    name = unique()
    resp = await upload(client, name, xlsx(V1))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replaced"] is False and body["build_reused"] is False
    for key in ("import_id", "snapshot_id", "skipped_sheets", "conversions", "warnings"):
        assert key in body
    src = body["source"]
    assert src["origin"] == "upload" and src["kind"] == "sqlite" and src["readonly"] is True
    assert src["current_snapshot"]["id"] == body["snapshot_id"]
    assert src["current_snapshot"]["file_name"] == "客流.xlsx"
    assert src["current_snapshot"]["raw_state"] == "kept"
    assert src["table_count"] == 2
    table = next(t for t in body["tables"] if t["name"] == "甲表")
    assert table["sheet"] == "甲表" and table["rows"] == 2 and table["unshaped"] is False
    assert {c["name"]: c["type"] for c in table["columns"]} == {"区域": "TEXT", "数量": "INTEGER"}
    assert [c["header"] for c in table["columns"]] == ["区域", "数量"]
    for key in ("region", "blank_rows_skipped", "columns_trimmed"):
        assert key in table
    row = await by_name(name)
    assert await scalar(await resolve(row.id), 'SELECT SUM("数量") FROM "甲表"') == 3


async def test_reupload_switches_version_and_keeps_name_and_id(client):
    name = unique()
    first = (await upload(client, name, xlsx(V1))).json()
    again = await upload(client, name, xlsx(V2), description="新的说明")
    assert again.status_code == 201, again.text
    body = again.json()
    assert body["replaced"] is True
    assert body["source"]["id"] == first["source"]["id"] and body["source"]["name"] == name
    assert body["snapshot_id"] != first["snapshot_id"]
    assert body["source"]["description"] == "新的说明"
    view = await resolve(body["source"]["id"])
    assert await scalar(view, 'SELECT SUM("数量") FROM "甲表"') == 100


async def test_same_file_twice_reuses_the_build(client):
    name, raw = unique(), xlsx(V1)
    first = (await upload(client, name, raw)).json()
    second = (await upload(client, name, raw)).json()
    assert second["build_reused"] is True and second["import_id"] != first["import_id"]
    assert len(await imports_of(first["source"]["id"])) == 2


async def test_a_failed_reupload_leaves_the_old_version_queryable(client, store):
    name = unique()
    first = (await upload(client, name, xlsx(V1))).json()
    before = sorted(p for p in (store / "uploads").rglob("*") if p.is_file())
    resp = await upload(client, name, b"not really an xlsx")
    assert resp.status_code == 400
    assert sorted(p for p in (store / "uploads").rglob("*") if p.is_file()) == before
    row = await by_name(name)
    assert row.current_snapshot_id == first["snapshot_id"]
    view = await resolve(row.id)
    assert await scalar(view, 'SELECT SUM("数量") FROM "甲表"') == 3
    assert await scalar(view, 'SELECT SUM("客流") FROM "乙表"') == 30


def _front_accepts(decision: dict) -> bool:
    """照 frontend/src/api/client.ts 的 uploadDecision 逐项校验：形状对不上，界面就当普通报错弹一句话，
    不画选择页。再加上 UploadDecisionBody 画的时候读的字段（types.ts 的 UploadMixedColumn、UploadShapeReason）。"""
    details = decision.get("details")
    if not isinstance(details, dict):
        return False
    if decision.get("kind") == "mixed":
        cols = details.get("columns")
        return isinstance(cols, list) and bool(cols) and all(
            isinstance(c, dict) and isinstance(c.get("column"), str) and isinstance(c.get("values"), list)
            and isinstance(c.get("sheet"), str) and isinstance(c.get("table"), str)
            and isinstance(c.get("header"), str)
            and isinstance(c.get("numeric"), int) and isinstance(c.get("nonnumeric"), int)
            and all(isinstance(v, dict) and set(v) == {"value", "count"} and isinstance(v["count"], int)
                    for v in c["values"])
            for c in cols)
    if decision.get("kind") == "shape":
        reasons = details.get("reasons")
        if "mixed" in details and not _front_accepts({"kind": "mixed", "details": {"columns": details["mixed"]}}):
            return False        # 预告的混合列和第二轮的混合列同一个形状
        return isinstance(reasons, list) and bool(reasons) and all(
            isinstance(r, dict) and isinstance(r.get("message"), str) and isinstance(r.get("kind"), str)
            and isinstance(r.get("sheet"), str) and isinstance(r.get("cells"), list)
            and all(isinstance(c, str) for c in r["cells"])
            for r in reasons)
    return False


async def test_needs_decision_is_a_422_with_the_choice(client, store):
    """真解析器走接口：两种决定的 422 形状都是前端认得的（以前这里用替身，values 是 dict，前端会拒收）。"""
    from tests.test_table_versions import REAL_PARSER, stub_load_into

    assert REAL_PARSER and tabular.load_into is not stub_load_into      # 走的是真解析器，不是替身
    # 数字列混入非数字
    mixed = xlsx({"明细": [["地区", "销量"]] + [[f"分区{i}", i * 10] for i in range(1, 10)]
                  + [["分区x", "N/A"], ["分区y", "·"]]})
    name = unique()
    resp = await upload(client, name, mixed)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "混有非数字的值" in body["detail"]
    decision = body["decision"]
    assert decision["kind"] == "mixed" and _front_accepts(decision), decision
    [col] = decision["details"]["columns"]
    assert (col["sheet"], col["table"], col["column"], col["header"]) == ("明细", "明细", "销量", "销量")
    assert (col["numeric"], col["nonnumeric"]) == (9, 2)
    assert col["values"] == [{"value": "N/A", "count": 1}, {"value": "·", "count": 1}]
    # 交叉表、多块结构：上下叠了两张表，中间一行分段标题
    stacked = xlsx({"汇总": [["项目", "金额"], ["分区甲", 10], ["分区乙", 20], ["二、费用", None],
                             ["分区丙", 5], ["分区丁", 7]]})
    resp = await upload(client, name, stacked)
    assert resp.status_code == 422, resp.text
    decision = resp.json()["decision"]
    assert decision["kind"] == "shape" and _front_accepts(decision), decision
    [reason] = decision["details"]["reasons"]
    assert (reason["sheet"], reason["kind"], reason["cells"]) == ("汇总", "section_title", ["A4"])
    # 被拒收的上传：不建源、不留原件
    assert await by_name(name) is None
    assert not [p for p in (store / "uploads").rglob("*") if p.is_file()]


async def test_crosstab_is_refused_with_every_reason_on_the_first_try(client, store):
    """仿客流表的合成交叉表：第一次退回就列出结构原因（日期横排且没写年份、分段标题、表内合计）和要预告的
    占位符「·」；选「按原样导入」重传，退回的混合列正是预告的那些；再选存空值，导入成功、标未规整。"""
    from tests.test_tabular import crosstab

    name, raw = unique(), crosstab()
    resp = await upload(client, name, raw, header_row=4)
    assert resp.status_code == 422, resp.text
    decision = resp.json()["decision"]
    assert decision["kind"] == "shape" and _front_accepts(decision), decision
    kinds = [r["kind"] for r in decision["details"]["reasons"]]
    assert kinds == ["date_header", "section_title", "formula_above"]
    assert decision["details"]["reasons"][0]["no_year"] is True
    preview = decision["details"]["mixed"]
    assert {v["value"] for c in preview for v in c["values"]} == {"·"}
    assert "「·」" in resp.json()["detail"]

    second = await upload(client, name, raw, header_row=4, raw_mode=True)
    assert second.status_code == 422
    assert second.json()["decision"]["kind"] == "mixed"
    assert second.json()["decision"]["details"]["columns"] == preview

    done = await upload(client, name, raw, header_row=4, raw_mode=True, mixed="null")
    assert done.status_code == 201, done.text
    assert done.json()["tables"][0]["unshaped"] is True


def _empty_dirs(root: Path) -> list[Path]:
    """上传目录下空着的子目录。tables/、raw/ 这两个根目录本身不算。"""
    roots = {root / "tables", root / "raw"}
    return sorted(p for p in root.rglob("*") if p.is_dir() and p not in roots and not any(p.iterdir()))


async def test_rejected_uploads_of_new_names_leave_no_empty_directories(client, store):
    """新名字的上传被拒（打不开、表头行号不对、要先做决定）：不建源，也不留下 tables/<源 id>/builds/ 空目录。
    「422 → 用户选择 → 重传」每一轮都是一个新 id，以前每轮多一个空目录，重启也不清。"""
    uploads = store / "uploads"
    mixed = xlsx({"明细": [["地区", "销量"]] + [[f"分区{i}", i] for i in range(1, 10)] + [["分区x", "缺"]]})
    attempts = [(b"not an xlsx", {}, 400), (xlsx(V1), {"header_row": 500}, 400), (mixed, {}, 422)]
    for raw, form, status in attempts:
        name = unique()
        resp = await upload(client, name, raw, **form)
        assert resp.status_code == status, resp.text
        assert await by_name(name) is None
        assert _empty_dirs(uploads) == [], _empty_dirs(uploads)
    # 已有的源重传被拒：它的目录里有当前版本，原样留着
    name = unique()
    assert (await upload(client, name, xlsx(V1))).status_code == 201
    assert (await upload(client, name, b"broken")).status_code == 400
    assert _empty_dirs(uploads) == []

    # 以前留下的空目录（升级前被拒收的上传、清除原件后的前缀目录），启动时清掉
    leftovers = [uploads / "tables" / uuid.uuid4().hex / "builds", uploads / "raw" / "ab"]
    for folder in leftovers:
        folder.mkdir(parents=True)
    async with SessionLocal() as session:
        await table_versions.startup(session)
    assert _empty_dirs(uploads) == []
    assert (await by_name(name)).current_snapshot_id      # 正常的源不受影响


def test_the_front_end_check_refuses_shapes_it_cannot_draw():
    """校验本身要能拒：dict 形状的 values（以前替身给的）、缺 message 的原因，前端都不认。"""
    assert not _front_accepts({"kind": "mixed", "details": {"columns": [
        {"sheet": "甲", "table": "甲表", "column": "数量", "header": "数量", "numeric": 9, "nonnumeric": 3,
         "values": {"·": 3}}]}})
    assert not _front_accepts({"kind": "shape", "details": {"reasons": [{"kind": "section_title", "cells": []}]}})
    assert not _front_accepts({"kind": "other", "details": {}})


async def test_mixed_and_raw_mode_are_passed_to_the_parser(client, monkeypatch):
    seen: dict = {}
    real = tabular.load_into

    def spy(db_path, raw, filename, **kw):
        seen.update(kw)
        return real(db_path, raw, filename, **kw)

    monkeypatch.setattr(tabular, "load_into", spy)
    resp = await upload(client, unique(), xlsx(V1), mixed="null", raw_mode=True, header_row=1)
    assert resp.status_code == 201, resp.text
    assert seen["mixed"] == "null" and seen["raw_mode"] is True and seen["header_row"] == 1


async def test_unknown_mixed_choice_is_refused(client):
    resp = await upload(client, unique(), xlsx(V1), mixed="text")
    assert resp.status_code == 400
    assert "拒收" in resp.json()["detail"]


async def test_upload_still_refuses_to_take_over_other_sources(client, tmp_path):
    name = unique()
    async with SessionLocal() as session:
        session.add(DataSource(name=name, kind="sqlite", database=str(tmp_path / "manual.db")))
        await session.commit()
    resp = await upload(client, name, xlsx(V1))
    assert resp.status_code == 409
    assert "请换一个名称" in resp.json()["detail"]


async def test_reupload_over_a_tampered_build_file_restores_it(client, store):
    """重传同一文件、服务端的构建文件被改成 0 字节（期 1 这里回 500）：用这次上传恢复，原文件挪进隔离区。

    响应里的 build_restored 字段由 WP-5 加，那条断言在 test_p3_api_errors.py；这里只看状态码、隔离区和指针。
    """
    name, raw = unique(), xlsx(V1)
    first = (await upload(client, name, raw)).json()
    async with SessionLocal() as session:
        snap = await session.get(SourceSnapshot, first["snapshot_id"])
        path, registered = Path(snap.db_path), snap.db_sha256
    os.chmod(path, 0o644)
    path.write_bytes(b"")
    resp = await upload(client, name, raw)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    row = await by_name(name)
    assert row.current_snapshot_id == body["snapshot_id"] != first["snapshot_id"]
    assert raw_store.sha256_file(path) == registered
    moved = sorted(p for p in (store / "uploads" / "quarantine" / row.id).rglob("*.db"))
    assert len(moved) == 1 and moved[0].stat().st_size == 0


# --------------------------------------------------------------------------
# 不可变的强制
# --------------------------------------------------------------------------


async def test_upload_sources_refuse_connection_edits(client):
    name = unique()
    src = (await upload(client, name, xlsx(V1))).json()["source"]
    sid = src["id"]
    for patch in ({"readonly": False}, {"database": "/tmp/other.db"}, {"kind": "mysql"},
                  {"host": "h"}, {"password": "secret"}):
        resp = await client.patch(f"/api/datasources/{sid}", json=patch)
        assert resp.status_code == 422, (patch, resp.text)
        assert "重新上传" in resp.json()["detail"]
    # 原样带回的连接字段不算修改；说明、启用、遮罩照常能改
    resp = await client.patch(f"/api/datasources/{sid}", json={
        "readonly": True, "database": src["database"], "kind": "sqlite", "password": "",
        "description": "改过的说明", "options": {"mask_columns": "区域"},
    })
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["description"] == "改过的说明" and out["origin"] == "upload"
    assert out["current_snapshot"]["id"] == src["current_snapshot"]["id"]
    row = await by_name(name)
    assert row.readonly is True and row.database == src["database"]


async def test_upload_sources_refuse_a_schema_option(client):
    """上传库只有 main：设了 schema，下一次发布的探查会落空、把空结构冻结进快照。"""
    name = unique()
    src = (await upload(client, name, xlsx(V1))).json()["source"]
    resp = await client.patch(f"/api/datasources/{src['id']}", json={"options": {"schema": "nope"}})
    assert resp.status_code == 422, resp.text
    assert "schema" in resp.json()["detail"] and "重新上传" in resp.json()["detail"]
    # 空的 schema 摘掉就行，其余配置照常保存
    resp = await client.patch(f"/api/datasources/{src['id']}",
                              json={"options": {"schema": "", "mask_columns": "区域"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["options"] == {"mask_columns": "区域"}
    again = await upload(client, name, xlsx(V2))
    assert again.status_code == 201, again.text
    assert again.json()["source"]["table_count"] == 2 and not again.json()["source"]["schema_error"]


def test_under_uploads_sees_through_sqlite_uri_and_query_tricks(tmp_path):
    target = settings.uploads_dir / "tables" / "s" / "builds" / "b.db"
    inside = [
        str(target),
        f"file:{target}?uri=true",
        f"file:{target}?mode=rwc&uri=true",
        f"file://{target}?uri=true",
        f"file://localhost{target}?uri=true",
        "file:" + str(target).replace("/", "%2F") + "?uri=true",
        f"FILE:{target}?uri=true",
        f"{target}?x=/../../../../../../../../tmp/elsewhere",
        f"{target}?uri=true",
    ]
    for path in inside:
        assert table_versions.under_uploads(path), path
    for path in (str(tmp_path / "fine.db"), f"file:{tmp_path / 'fine.db'}?uri=true", ":memory:", "", None):
        assert not table_versions.under_uploads(path), path


async def test_manual_sqlite_sources_cannot_reach_uploads_through_uris(client, tmp_path):
    """手工源的路径原样拼进连接串：file: URI、带 ?…/../ 的写法都不能指进上传目录读上传的数据。"""
    body = (await upload(client, unique(), xlsx(V1))).json()
    async with SessionLocal() as session:
        build_db = (await session.get(SourceSnapshot, body["snapshot_id"])).db_path
    for database in (f"file:{build_db}?uri=true",
                     "file:" + build_db.replace("/", "%2F") + "?uri=true",
                     f"{build_db}?x=/../../../../../../../../../tmp/q",
                     f"file:{Path(build_db).parent / 'new.db'}?mode=rwc&uri=true"):
        resp = await client.post("/api/datasources", json={
            "name": unique(), "kind": "sqlite", "database": database, "readonly": False})
        assert resp.status_code == 422, (database, resp.text)
    assert sorted(p.name for p in Path(build_db).parent.iterdir()) == [Path(build_db).name]
    ok = await client.post("/api/datasources", json={
        "name": unique(), "kind": "sqlite", "database": str(tmp_path / "fine.db"), "readonly": False})
    assert ok.status_code == 201
    resp = await client.patch(f"/api/datasources/{ok.json()['id']}",
                              json={"database": f"file:{build_db}?uri=true"})
    assert resp.status_code == 422


async def test_manual_sqlite_sources_cannot_point_into_uploads(client, tmp_path):
    inside = settings.uploads_dir / "tables" / "sneaky.db"
    resp = await client.post("/api/datasources", json={
        "name": unique(), "kind": "sqlite", "database": str(inside)})
    assert resp.status_code == 422
    assert "上传表格" in resp.json()["detail"]

    # 符号链接绕不过去：按 realpath 判断
    link = tmp_path / "alias"
    link.symlink_to(settings.uploads_dir, target_is_directory=True)
    resp = await client.post("/api/datasources", json={
        "name": unique(), "kind": "sqlite", "database": str(link / "x.db")})
    assert resp.status_code == 422

    ok = await client.post("/api/datasources", json={
        "name": unique(), "kind": "sqlite", "database": str(tmp_path / "fine.db")})
    assert ok.status_code == 201, ok.text
    assert ok.json()["origin"] == "manual" and ok.json()["current_snapshot"] is None
    sid = ok.json()["id"]
    resp = await client.patch(f"/api/datasources/{sid}", json={"database": str(inside)})
    assert resp.status_code == 422
    resp = await client.patch(f"/api/datasources/{sid}", json={"description": "只改说明"})
    assert resp.status_code == 200


async def test_legacy_manual_sources_in_uploads_stay_readonly(client, tmp_path):
    """升级前就指向上传目录的手工源：启动时强制只读，之后只改「只读」也不许改回可写；别的字段照常能改。"""
    nested = settings.uploads_dir / "tables" / "deeper"
    nested.mkdir(parents=True)
    import sqlite3

    db = sqlite3.connect(nested / "x.db")
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = DataSource(name=unique(), kind="sqlite", database=str(nested / "x.db"), readonly=False)
        session.add(row)
        await session.commit()
        sid = row.id
        await table_versions.startup(session)
    resp = await client.patch(f"/api/datasources/{sid}", json={"readonly": False})
    assert resp.status_code == 422
    assert "只能以只读方式连接" in resp.json()["detail"]
    resp = await client.patch(f"/api/datasources/{sid}", json={"description": "只改说明", "readonly": True})
    assert resp.status_code == 200 and resp.json()["readonly"] is True


async def test_testing_a_draft_that_points_into_uploads_fails_like_saving_it(client, tmp_path):
    """表单里先测连接：指向上传目录的路径不能先报「连接成功」、到保存时才被拒。"""
    first = (await upload(client, unique(), xlsx(V1))).json()
    async with SessionLocal() as session:
        build = (await session.get(SourceSnapshot, first["snapshot_id"])).db_path
    legacy = settings.uploads_dir / "tables" / "legacy_stock.db"
    import sqlite3

    sqlite3.connect(legacy).close()
    before = sorted(p for p in settings.uploads_dir.rglob("*"))
    for path in (build, str(legacy)):
        for readonly in (True, False):
            resp = await client.post("/api/datasources/test", json={
                "name": "draft", "kind": "sqlite", "database": path, "readonly": readonly})
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is False and "上传表格的存放目录" in body["error"], body
            saved = await client.post("/api/datasources", json={
                "name": unique(), "kind": "sqlite", "database": path, "readonly": readonly})
            assert saved.status_code == 422
    assert sorted(p for p in settings.uploads_dir.rglob("*")) == before
    # 上传目录外的照常测
    fine = tmp_path / "fine.db"
    sqlite3.connect(fine).close()
    resp = await client.post("/api/datasources/test", json={"name": "draft", "kind": "sqlite", "database": str(fine)})
    assert resp.json()["ok"] is True, resp.json()


FIRMLINK = "/System/Volumes/Data"


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _other_spellings(path: str) -> list[str]:
    """同一个上传目录里的文件的另几种写法：数据目录、上传目录两段各换大小写，加 firmlink 前缀。

    只留文件系统真认的写法（和原文件是同一个文件）：区分大小写的文件系统上大小写变体会被去掉，
    不是 macOS 时 firmlink 那种也会被去掉；一种都不剩就跳过（这类绕法在那里本来就不成立）。
    """
    real = os.path.realpath(path)
    data, uploads = os.path.realpath(settings.data_dir), os.path.realpath(settings.uploads_dir)
    rest = real[len(uploads):]
    candidates = [
        os.path.join(os.path.dirname(data), os.path.basename(data).upper(), "uploads") + rest,
        os.path.join(data, "Uploads") + rest,
        FIRMLINK + real,
    ]
    found = [c for c in candidates if c != real and _same_file(c, real)]
    if not found:
        pytest.skip("这个文件系统上没有同一文件的另一种写法（区分大小写、也没有 firmlink）")
    return found


def test_under_uploads_sees_through_case_and_firmlink_spellings(tmp_path):
    """APFS 不区分大小写、realpath 不解析 firmlink：按路径字符串比，这几种写法都会被当成上传目录以外。"""
    target = settings.uploads_dir / "tables" / "s" / "builds" / "b.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    spellings = _other_spellings(str(target))
    for path in spellings:
        for variant in (path, f"file:{path}?uri=true", f"FILE:{path}?mode=rwc&uri=true",
                        "file:" + path.replace("/", "%2F") + "?uri=true",
                        # 文件和中间几级目录还不存在（mode=rwc 新建）：靠已存在的祖先认
                        os.path.join(os.path.dirname(path), "new", "x.db")):
            assert table_versions.under_uploads(variant), variant
    # 硬链接：路径在上传目录以外，文件却是同一个
    alias = tmp_path / "alias.db"
    os.link(target, alias)
    assert table_versions.under_uploads(str(alias))
    elsewhere = tmp_path / "fine.db"
    elsewhere.write_bytes(b"")
    assert not table_versions.under_uploads(str(elsewhere))


async def test_manual_sqlite_sources_cannot_reach_uploads_through_other_spellings(client, tmp_path):
    """大小写不同的写法、firmlink 前缀、它们的 file: URI：创建、测连接、修改都要拒，不能直接读出上传的数据。"""
    body = (await upload(client, unique(), xlsx(V1))).json()
    async with SessionLocal() as session:
        build_db = (await session.get(SourceSnapshot, body["snapshot_id"])).db_path
    spellings = _other_spellings(build_db)
    for path in spellings:
        for database in (path, f"file:{path}?uri=true"):
            for readonly in (True, False):
                resp = await client.post("/api/datasources", json={
                    "name": unique(), "kind": "sqlite", "database": database, "readonly": readonly})
                assert resp.status_code == 422, (database, resp.text)
                assert "上传表格的存放目录" in resp.json()["detail"]
                probe = (await client.post("/api/datasources/test", json={
                    "name": "draft", "kind": "sqlite", "database": database, "readonly": readonly})).json()
                assert probe["ok"] is False and "上传表格的存放目录" in probe["error"], probe
    ok = await client.post("/api/datasources", json={
        "name": unique(), "kind": "sqlite", "database": str(tmp_path / "fine.db"), "readonly": False})
    assert ok.status_code == 201, ok.text
    for path in spellings:
        resp = await client.patch(f"/api/datasources/{ok.json()['id']}", json={"database": path})
        assert resp.status_code == 422, (path, resp.text)


async def test_startup_locks_legacy_sources_registered_under_other_spellings(client):
    """升级前用另一种写法登记、可写的手工源：启动时同样强制只读，之后改不回可写。"""
    body = (await upload(client, unique(), xlsx(V1))).json()
    async with SessionLocal() as session:
        build_db = (await session.get(SourceSnapshot, body["snapshot_id"])).db_path
    ids = []
    async with SessionLocal() as session:
        for path in _other_spellings(build_db):
            row = DataSource(name=unique(), kind="sqlite", database=path, readonly=False)
            session.add(row)
            await session.commit()
            ids.append(row.id)
        await table_versions.startup(session)
    for sid in ids:
        async with SessionLocal() as session:
            assert (await session.get(DataSource, sid)).readonly is True
        resp = await client.patch(f"/api/datasources/{sid}", json={"readonly": False})
        assert resp.status_code == 422, resp.text


def _app_db() -> str:
    """测试进程实际连着的元数据库（conftest 建的那个）。data_dir 被夹具换过，和 settings.db_path 不是同一个。"""
    from app.db import base as db_base

    return str(db_base.engine.url.database)


async def test_manual_sqlite_sources_cannot_point_at_app_files(client, tmp_path):
    """应用自己的数据文件不能登记成数据源：可写登记后，一条经审批的 UPDATE 就能把上传源改成手工源、
    指向别处，快照登记也能改；只读登记也会把全部元数据交给模型。"""
    live = _app_db()
    link = tmp_path / "meta-link.db"
    link.symlink_to(live)
    hard = tmp_path / "meta-hard.db"
    os.link(live, hard)
    data = settings.data_dir
    targets = [
        live, f"{live}-wal", f"{live}-journal", f"file:{live}?mode=ro&uri=true",
        "file:" + live.replace("/", "%2F") + "?uri=true", str(link), str(hard),
        str(settings.db_path), str(settings.checkpoint_path), f"{settings.db_path}-shm",
        str(data / ".secret_key"), str(data / "artifacts" / "ab" / "x.db"),
        str(data / "sandbox-policies" / "x.db"),
    ]
    upper = os.path.join(os.path.dirname(live), os.path.basename(live).upper())
    if _same_file(upper, live):
        targets.append(upper)
    for database in targets:
        for readonly in (True, False):
            resp = await client.post("/api/datasources", json={
                "name": unique(), "kind": "sqlite", "database": database, "readonly": readonly})
            assert resp.status_code == 422, (database, resp.text)
            assert "AgentLab 自己的数据文件" in resp.json()["detail"]
            probe = (await client.post("/api/datasources/test", json={
                "name": "draft", "kind": "sqlite", "database": database, "readonly": readonly})).json()
            assert probe["ok"] is False and "AgentLab 自己的数据文件" in probe["error"], (database, probe)
    fine = tmp_path / "fine.db"
    ok = await client.post("/api/datasources", json={
        "name": unique(), "kind": "sqlite", "database": str(fine), "readonly": False})
    assert ok.status_code == 201, ok.text
    resp = await client.patch(f"/api/datasources/{ok.json()['id']}", json={"database": live})
    assert resp.status_code == 422
    # 工作区不在其中：登记模型生成的库是正当用法
    workspace_db = settings.workspace_dir / "s" / "made.db"
    workspace_db.parent.mkdir(parents=True)
    workspace_db.write_bytes(b"")
    resp = await client.post("/api/datasources", json={
        "name": unique(), "kind": "sqlite", "database": str(workspace_db)})
    assert resp.status_code == 201, resp.text


async def test_startup_disables_legacy_sources_pointing_at_app_files(client, tmp_path):
    """升级前登记的、指向元数据库的手工源：启动时停用并改成只读；之后除了改路径和删除，修改一律拒，测连接也不去连。"""
    async with SessionLocal() as session:
        row = DataSource(name=unique(), kind="sqlite", database=_app_db(), readonly=False, enabled=True)
        session.add(row)
        await session.commit()
        sid = row.id
        await table_versions.startup(session)
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        assert row.readonly is True and row.enabled is False
    for patch in ({"enabled": True}, {"readonly": False}, {"description": "只改说明"}):
        resp = await client.patch(f"/api/datasources/{sid}", json=patch)
        assert resp.status_code == 422, (patch, resp.text)
        assert "AgentLab 自己的数据文件" in resp.json()["detail"]
    probe = (await client.post(f"/api/datasources/{sid}/test")).json()
    assert probe["ok"] is False and "AgentLab 自己的数据文件" in probe["error"]
    assert not any(k.startswith(f"{sid}:") for k in engines._engines)
    resp = await client.patch(f"/api/datasources/{sid}", json={"database": str(tmp_path / "fine.db")})
    assert resp.status_code == 200, resp.text


async def test_testing_an_upload_source_with_other_connection_fields_fails_like_saving_it(client):
    """测连接带上传源的 id：连接字段改了就和保存一样拒，不能拿表单里的路径去连上传目录里的别的文件。"""
    a = (await upload(client, unique(), xlsx(V1))).json()
    b = (await upload(client, unique(), xlsx(V2))).json()
    async with SessionLocal() as session:
        b_build = (await session.get(SourceSnapshot, b["snapshot_id"])).db_path
        [imp] = list((await session.execute(
            select(TableImport).where(TableImport.id == a["import_id"]))).scalars())
    raw_path = str(raw_store.raw_path(imp.raw_sha256))
    src = a["source"]
    form = {"id": src["id"], "name": src["name"], "kind": "sqlite", "database": src["database"],
            "readonly": True, "options": {}}
    before = sorted(settings.uploads_dir.rglob("*"))
    for change in ({"database": b_build}, {"readonly": False}, {"database": raw_path},
                   {"database": str(settings.uploads_dir / "tables" / "nope.db")}, {"kind": "mysql"}):
        probe = (await client.post("/api/datasources/test", json={**form, **change})).json()
        assert probe["ok"] is False and "不能修改" in probe["error"], (change, probe)
        saved = await client.patch(f"/api/datasources/{src['id']}", json=change)
        assert saved.status_code == 422 and saved.json()["detail"] == probe["error"]
    assert sorted(settings.uploads_dir.rglob("*")) == before
    # 没改连接字段：测的是它当前版本的快照（带版本的缓存键），不是表单里的路径
    probe = (await client.post("/api/datasources/test", json=form)).json()
    assert probe["ok"] is True, probe
    assert f"{src['id']}:{a['snapshot_id']}" in engines._engines
    assert f"{src['id']}:" not in engines._engines
    await engines.invalidate(src["id"])


async def test_testing_an_upload_source_checks_its_snapshot(client):
    """上传源的「测连接」走快照：核对哈希，不在连接缓存里留下不带版本、不核对的引擎。"""
    body = (await upload(client, unique(), xlsx(V1))).json()
    sid = body["source"]["id"]
    resp = (await client.post(f"/api/datasources/{sid}/test")).json()
    assert resp["ok"] is True, resp
    assert set(k for k in engines._engines if k.startswith(f"{sid}:")) == {f"{sid}:{body['snapshot_id']}"}

    async with SessionLocal() as session:
        path = Path((await session.get(SourceSnapshot, body["snapshot_id"])).db_path)
    os.chmod(path, 0o644)
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute('UPDATE "甲表" SET "数量" = 999')
    conn.commit()
    conn.close()
    for _ in range(2):            # 第一次撞上缓存的引擎（指纹对不上），第二次重新核对：都要拒
        resp = (await client.post(f"/api/datasources/{sid}/test")).json()
        assert resp["ok"] is False and "可能被修改过" in resp["error"], resp
    assert f"{sid}:" not in engines._engines
    assert (await by_name(body["source"]["name"])).last_check["ok"] is False

    path.unlink()
    resp = (await client.post(f"/api/datasources/{sid}/test")).json()
    assert resp["ok"] is False and "已不存在" in resp["error"], resp
    await engines.invalidate(sid)


async def test_upload_sources_are_not_reintrospected_and_schema_comes_from_the_snapshot(client):
    name = unique()
    body = (await upload(client, name, xlsx(V1))).json()
    sid = body["source"]["id"]
    for params in ({}, {"dry_run": "true"}):
        resp = await client.post(f"/api/datasources/{sid}/introspect", params=params)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "上传的表格无需重新探查，重新上传即可更新"

    # 数据源上的缓存被别处改坏了，接口仍按当前快照回答
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        row.schema_cache = {"tables": {}}
        await session.commit()
    resp = await client.get(f"/api/datasources/{sid}/schema")
    assert resp.status_code == 200
    assert set(resp.json()["tables"]) == {"甲表", "乙表"}
    one = (await client.get(f"/api/datasources/{sid}/schema", params={"table": "甲表"})).json()
    assert one["found"] is True and [c["name"] for c in one["columns"]] == ["区域", "数量"]


async def test_list_shows_origin_and_current_snapshot(client, tmp_path):
    name = unique()
    body = (await upload(client, name, xlsx(V1))).json()
    rows = {r["name"]: r for r in (await client.get("/api/datasources")).json()}
    assert rows[name]["origin"] == "upload"
    snap = rows[name]["current_snapshot"]
    assert snap["id"] == body["snapshot_id"] and snap["file_name"] == "客流.xlsx"
    assert snap["created_at"] and snap["raw_state"] == "kept"


# --------------------------------------------------------------------------
# 删除、导入记录、清除原件
# --------------------------------------------------------------------------


async def test_delete_retires_imports_and_reclaims_files(client):
    name = unique()
    body = (await upload(client, name, xlsx(fresh(V1)))).json()
    sid = body["source"]["id"]
    async with SessionLocal() as session:
        snap = await session.get(SourceSnapshot, body["snapshot_id"])
        path = Path(snap.db_path)
    [imp] = await imports_of(sid)
    assert path.exists() and raw_store.raw_exists(imp.raw_sha256)
    resp = await client.delete(f"/api/datasources/{sid}")
    assert resp.status_code == 204
    [imp] = await imports_of(sid)
    assert imp.status == "retired"
    assert not path.exists() and not raw_store.raw_exists(imp.raw_sha256)


async def test_list_imports_newest_first(client):
    name = unique()
    first = (await upload(client, name, xlsx(V1))).json()
    second = (await upload(client, name, xlsx(V2), filename="第二期.xlsx")).json()
    sid = first["source"]["id"]
    rows = (await client.get(f"/api/datasources/{sid}/imports")).json()
    assert [r["id"] for r in rows] == [second["import_id"], first["import_id"]]
    assert rows[0]["current"] is True and rows[0]["status"] == "active"
    assert rows[0]["file_name"] == "第二期.xlsx" and rows[0]["raw_state"] == "kept"
    assert rows[1]["current"] is False and rows[1]["status"] != "active"
    assert (await client.get(f"/api/datasources/{uuid.uuid4().hex}/imports")).status_code == 404


async def test_purge_raw_deletes_the_file_and_keeps_the_record(client):
    name = unique()
    body = (await upload(client, name, xlsx(fresh(V1)))).json()
    sid, iid = body["source"]["id"], body["import_id"]
    [imp] = await imports_of(sid)
    path = raw_store.raw_path(imp.raw_sha256)
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o444

    resp = await client.post(f"/api/datasources/{sid}/imports/{iid}/purge-raw",
                             json={"reason": "含有不应保留的内容", "signed_by": "测试员"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["file_deleted"] is True and out["also_purged"] == []
    assert out["import"]["raw_state"] == "purged"
    purged = out["import"]["purged"]
    assert purged["reason"] == "含有不应保留的内容" and purged["signed_by"] == "测试员"
    assert purged["signed_by_verified"] is False and purged["at"]
    assert not path.exists()
    assert not path.parent.exists()     # 原件所在的 raw/<前两位>/ 空了，一并删掉
    [imp] = await imports_of(sid)
    assert imp.raw_state == "purged" and imp.raw_sha256      # 哈希留着：说得清当时用的是哪份文件
    # 库照常可查，当前版本显示原件已清除
    assert await scalar(await resolve(sid), 'SELECT COUNT(*) FROM "甲表"') == 2
    src = next(r for r in (await client.get("/api/datasources")).json() if r["id"] == sid)
    assert src["current_snapshot"]["raw_state"] == "purged"

    again = await client.post(f"/api/datasources/{sid}/imports/{iid}/purge-raw", json={"reason": "再来"})
    assert again.status_code == 409
    blank = await client.post(f"/api/datasources/{sid}/imports/{iid}/purge-raw", json={"reason": "  "})
    assert blank.status_code == 422
    missing = await client.post(f"/api/datasources/{sid}/imports/{uuid.uuid4().hex}/purge-raw",
                                json={"reason": "x"})
    assert missing.status_code == 404


async def test_purge_is_by_content_and_lists_every_affected_import(client):
    """同一份内容被别的导入共用（另一个源、同一个源传过两次）：清除时文件照删，那些导入一并标成已清除。

    清除是为了让含敏感内容的原件从服务器上消失（D13）；只标记不删，用户拿到 200 却什么都没去掉。
    """
    raw = xlsx(fresh(V1))
    a_name, b_name = unique(), unique()
    a = (await upload(client, a_name, raw)).json()
    b = (await upload(client, b_name, raw)).json()
    b2 = (await upload(client, b_name, raw, filename="再传一次.xlsx")).json()
    [imp_a] = await imports_of(a["source"]["id"])
    path = raw_store.raw_path(imp_a.raw_sha256)
    assert path.exists()

    resp = await client.post(
        f"/api/datasources/{a['source']['id']}/imports/{a['import_id']}/purge-raw",
        json={"reason": "甲方要求", "signed_by": "测试员"},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["file_deleted"] is True and not path.exists()
    assert out["import"]["raw_state"] == "purged"
    affected = {o["id"]: o for o in out["also_purged"]}
    # b 的两次导入（第一次已被替换，记录上原件仍是保留着的）都随之标成已清除
    assert set(affected) == {b["import_id"], b2["import_id"]}
    assert {o["source_name"] for o in affected.values()} == {b_name}
    assert affected[b2["import_id"]]["file_name"] == "再传一次.xlsx"
    imps_b = await imports_of(b["source"]["id"])
    assert [i.raw_state for i in imps_b] == ["purged", "purged"]
    assert {i.purged["via_import"] for i in imps_b} == {a["import_id"]}
    assert {i.purged["reason"] for i in imps_b} == {"甲方要求"}
    # 两个源的库照常可查
    for src in (a, b):
        assert await scalar(await resolve(src["source"]["id"]), 'SELECT COUNT(*) FROM "甲表"') == 2
    again = await client.post(
        f"/api/datasources/{b['source']['id']}/imports/{b2['import_id']}/purge-raw", json={"reason": "再来"})
    assert again.status_code == 409


async def test_purge_refuses_imports_without_a_raw_file(client):
    """迁移来的老上传没有原件。"""
    name = unique()
    body = (await upload(client, name, xlsx(V1))).json()
    sid, iid = body["source"]["id"], body["import_id"]
    async with SessionLocal() as session:
        imp = await session.get(TableImport, iid)
        imp.raw_state = "absent"
        await session.commit()
    resp = await client.post(f"/api/datasources/{sid}/imports/{iid}/purge-raw", json={"reason": "x"})
    assert resp.status_code == 409
    assert "没有保存原件" in resp.json()["detail"]
