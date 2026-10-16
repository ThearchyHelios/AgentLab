"""导入清单、累积快照清单、作废记录、清除原件记录如实写「是否认证」（docs/design/next-round/05-已知遗留.md (c)）。

以前四处都写死 False：开了登录、署名就是登录的人，封存的证据里却记着「未认证」，和审计表、待办派单、run.origin 的
verified 对不上。现在由 api/deps.signer_verified 在请求那一刻定下、和署名一起往下传：开登录为真，关登录（自报的署名）
为假。已封存的旧清单不改写（关登录时的形状见 test_table_imports_api、test_source_versions_api、test_datasources_upload）。
"""
from __future__ import annotations

import pytest

from app.api import deps
from app.core import artifact_store
from app.db.models import TableImport
from tests.fakes import auth_env
from tests.test_table_imports_api import first_import, get, store, unique  # noqa: F401 - store 是 autouse 夹具


@pytest.fixture
async def admin():
    await auth_env.reset()
    c = await auth_env.turn_on_local("root")
    try:
        yield c
    finally:
        await c.aclose()
        await auth_env.reset()


def test_signer_verified_follows_the_request_identity():
    token = deps._principal.set(deps.Principal(username="root", role="admin", verified=True, mode="local"))
    try:
        assert deps.signer_verified() is True
    finally:
        deps._principal.reset(token)
    token = deps._principal.set(deps.Principal(username="张三", role="admin", verified=False, mode="off"))
    try:
        assert deps.signer_verified() is False, "关登录时署名是自报的"
    finally:
        deps._principal.reset(token)
    assert deps.signer_verified() is False, "不在请求里（启动整理、调度循环）不算认证"


def test_the_records_write_what_they_are_given():
    from app.data import source_versions

    assert source_versions._revoked_record("理由", "root", True)["signed_by_verified"] is True
    assert source_versions._revoked_record("理由", "root")["signed_by_verified"] is False
    assert source_versions._revoked_record("理由", None, True)["signed_by_verified"] is False, "没有署名谈不上认证"


async def test_login_on_import_manifest_and_purge_record_say_verified(admin):
    out = (await first_import(admin, unique("signer")))["commit"]
    imp = await get(TableImport, out["import_id"])
    manifest = artifact_store.load(imp.manifest_artifact)
    assert manifest["signed_by"] == {"name": "root", "verified": True}
    assert imp.signed_by == "root"

    source_id = out["source"]["id"]
    records = (await admin.get(f"/api/datasources/{source_id}/imports")).json()
    assert [(r["signed_by"], r["signed_by_verified"]) for r in records] == [("root", True)], "导入记录按清单里记下的说"
    resp = await admin.post(f"/api/datasources/{source_id}/imports/{imp.id}/purge-raw",
                            json={"reason": "含有不应保留的内容", "signed_by": "随手填的名字"})
    assert resp.status_code == 200, resp.text
    purged = resp.json()["import"]["purged"]
    assert (purged["signed_by"], purged["signed_by_verified"]) == ("root", True)
