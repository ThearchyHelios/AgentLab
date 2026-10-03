"""数据目录接口：/api/datasources/{id}/catalog 下的表清单、单表、整份提交、单项审阅、起草。

所有写操作记录请求头 X-Actor 的署名；版本不符一律 409，detail 说明这张表刚被修改过、请重新载入。
"""
from __future__ import annotations

import uuid
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.main import app as _fastapi_app
from tests.fixtures.catalog import scenic
from tests.fixtures.catalog.fake_model import FakeModel
from tests.fixtures.sources import drop_source

ACTOR = {"X-Actor": quote("王敏")}


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_fastapi_app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def source_id(client, scenic_db):
    """接上合成景区库并探查结构（走真实的探查接口）。"""
    async with SessionLocal() as session:
        row = DataSource(name=f"cat_{uuid.uuid4().hex[:10]}", kind="sqlite", database=scenic_db)
        session.add(row)
        await session.commit()
        sid = row.id
    r = await client.post(f"/api/datasources/{sid}/introspect")
    assert r.status_code == 200, r.text
    yield sid
    await drop_source(sid)


def _base(sid: str) -> str:
    return f"/api/datasources/{sid}/catalog"


async def test_list_includes_tables_without_notes(client, source_id):
    r = await client.get(_base(source_id))
    assert r.status_code == 200
    body = r.json()
    rows = {t["table_name"]: t for t in body["tables"]}
    assert len(rows) == scenic.TABLE_COUNT + len(scenic.VIEWS)
    visits = rows["visits"]
    assert visits["version"] == 0 and visits["label"] is None and visits["usage"] == 0
    assert visits["counts"] == {"proposed": 0, "verified": 0, "confirmed": 0, "rejected": 0}
    assert visits["relations"] == 0 and visits["updated_at"] is None
    assert body["system_notes"] is False


async def test_draft_then_read_table(client, source_id):
    r = await client.post(f"{_base(source_id)}/draft", json={"tables": ["visits", "nope"], "use_model": False},
                          headers=ACTOR)
    assert r.status_code == 200, r.text
    body = r.json()
    by = {t["table_name"]: t for t in body["tables"]}
    # visits：两条外键（景区、票种）+ 两条命名推断（闸口、会员）
    assert by["visits"]["added"] == 4 and by["visits"]["version"] == 1 and by["visits"]["error"] is None
    assert by["nope"]["error"] and body["model_used"] is False
    assert body["total"] == {"added": 4, "updated": 0, "removed": 0}

    r = await client.get(f"{_base(source_id)}/visits")
    assert r.status_code == 200
    detail = r.json()
    assert detail["version"] == 1 and detail["updated_by"] == "王敏"
    sources = sorted(rel["source"] for rel in detail["notes"]["relations"])
    assert sources == ["fk", "fk", "name", "name"]
    structure = detail["structure"]
    assert structure["primary_key"] == ["id"]
    assert {fk["to_table"] for fk in structure["foreign_keys"]} == {"parks", "ticket_types"}
    assert ["ticket_no"] in structure["unique"]
    assert any(c["name"] == "visit_time" for c in structure["columns"])

    listed = {t["table_name"]: t for t in (await client.get(_base(source_id))).json()["tables"]}
    assert listed["visits"]["counts"] == {"proposed": 2, "verified": 2, "confirmed": 0, "rejected": 0}
    assert listed["visits"]["relations"] == 4 and listed["visits"]["version"] == 1


async def test_put_marks_edits_human_confirmed_and_bumps_version(client, source_id):
    await client.post(f"{_base(source_id)}/draft", json={"tables": ["visits"]}, headers=ACTOR)
    detail = (await client.get(f"{_base(source_id)}/visits")).json()
    notes = detail["notes"]
    notes["label"] = {"value": "入园记录"}
    notes["grain"] = {"value": "每张门票每次检票一行", "source": "llm", "status": "proposed"}
    r = await client.put(f"{_base(source_id)}/visits", json={"notes": notes, "if_version": detail["version"]},
                         headers={"X-Actor": quote("李雷")})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["version"] == 2 and saved["updated_by"] == "李雷"
    assert saved["notes"]["label"]["source"] == "human" and saved["notes"]["label"]["status"] == "confirmed"
    assert saved["notes"]["grain"]["source"] == "human"
    assert {r["source"] for r in saved["notes"]["relations"]} == {"fk", "name"}     # 没动的保持原样


async def test_put_with_stale_version_conflicts(client, source_id):
    await client.post(f"{_base(source_id)}/draft", json={"tables": ["visits"]}, headers=ACTOR)
    r = await client.put(f"{_base(source_id)}/visits", json={"notes": {"label": {"value": "x"}}, "if_version": 0},
                         headers=ACTOR)
    assert r.status_code == 409
    assert "刚被修改过" in r.json()["detail"] and "重新载入" in r.json()["detail"]


async def test_put_rejects_malformed_notes(client, source_id):
    r = await client.put(f"{_base(source_id)}/visits",
                         json={"notes": {"formula": {"value": "sum(amount)"}}, "if_version": 0}, headers=ACTOR)
    assert r.status_code == 422
    assert "formula" in r.json()["detail"]


async def test_review_confirm_reject_and_conflict(client, source_id):
    await client.post(f"{_base(source_id)}/draft", json={"tables": ["visits"]}, headers=ACTOR)
    detail = (await client.get(f"{_base(source_id)}/visits")).json()
    gate = next(r for r in detail["notes"]["relations"] if r["columns"] == ["gate_id"])
    r = await client.post(f"{_base(source_id)}/visits/review",
                          json={"path": f"relations.{gate['id']}", "action": "confirm", "if_version": 1},
                          headers={"X-Actor": quote("韩梅")})
    assert r.status_code == 200, r.text
    after = r.json()
    assert after["version"] == 2 and after["updated_by"] == "韩梅"
    assert next(x for x in after["notes"]["relations"] if x["id"] == gate["id"])["status"] == "confirmed"
    stale = await client.post(f"{_base(source_id)}/visits/review",
                              json={"path": f"relations.{gate['id']}", "action": "reject", "if_version": 1})
    assert stale.status_code == 409 and "重新载入" in stale.json()["detail"]
    bad = await client.post(f"{_base(source_id)}/visits/review",
                            json={"path": "columns.visit_time.formula", "action": "confirm", "if_version": 2})
    assert bad.status_code == 422
    wrong = await client.post(f"{_base(source_id)}/visits/review",
                              json={"path": "grain", "action": "approve", "if_version": 2})
    assert wrong.status_code == 422


async def test_unknown_table_and_source(client, source_id):
    assert (await client.get(f"{_base(source_id)}/nope")).status_code == 404
    assert (await client.get(_base("missing-source"))).status_code == 404
    r = await client.put(f"{_base(source_id)}/nope", json={"notes": {}, "if_version": 0})
    assert r.status_code == 404


async def test_draft_defaults_to_top_tables_and_model_via_settings(client, source_id, monkeypatch):
    model = FakeModel({"visits": {"label": "入园记录", "kind": "fact"}})

    async def fake_resolve(session):
        return model, "fake-model"

    async def usage(session, source):
        return {"visits": 7}

    monkeypatch.setattr(catalog, "resolve_draft_model", fake_resolve)
    monkeypatch.setattr(catalog, "table_usage", usage)
    r = await client.post(f"{_base(source_id)}/draft", json={"use_model": True}, headers=ACTOR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["tables"]) == 20 and body["tables"][0]["table_name"] == "visits"
    assert body["model_used"] is True and body["model"] == "fake-model"
    detail = (await client.get(f"{_base(source_id)}/visits")).json()
    assert detail["notes"]["label"]["source"] == "llm" and detail["usage"] == 7


async def test_draft_reports_model_unavailable(client, source_id, monkeypatch):
    async def unavailable(session):
        raise catalog.CatalogModelUnavailable("未配置模型接入，无法由助手起草数据目录。请到「设置 → 模型接入」添加")

    monkeypatch.setattr(catalog, "resolve_draft_model", unavailable)
    r = await client.post(f"{_base(source_id)}/draft", json={"tables": ["visits"], "use_model": True})
    assert r.status_code == 200
    body = r.json()
    assert body["model_used"] is False and "模型接入" in body["model_error"]
    assert body["tables"][0]["added"] == 4                          # 外键和命名推断照常写入


async def test_draft_without_schema_is_refused(client):
    async with SessionLocal() as session:
        row = DataSource(name=f"cat_{uuid.uuid4().hex[:10]}", kind="sqlite", database=":memory:")
        session.add(row)
        await session.commit()
        sid = row.id
    try:
        r = await client.post(f"{_base(sid)}/draft", json={})
        assert r.status_code == 409 and "探查结构" in r.json()["detail"]
        assert (await client.get(_base(sid))).json()["tables"] == []
    finally:
        await drop_source(sid)


async def test_schema_endpoint_detail_carries_catalog(client, source_id):
    """数据源页「表结构」里给模型看的那段文本和 db_schema 工具同一个说法，也附上目录。"""
    before = (await client.get(f"/api/datasources/{source_id}/schema", params={"table": "visits"})).json()
    assert catalog.NOTES_HEADING not in before["detail"]
    await client.post(f"{_base(source_id)}/draft", json={"tables": ["visits"]}, headers=ACTOR)
    after = (await client.get(f"/api/datasources/{source_id}/schema", params={"table": "visits"})).json()
    assert catalog.NOTES_HEADING in after["detail"] and "gate_id → gates.id" in after["detail"]
    assert after["detail"].startswith(before["detail"])
