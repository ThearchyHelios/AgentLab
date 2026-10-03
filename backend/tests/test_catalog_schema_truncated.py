"""没探查到的表要说出来（数据目录完整性审查 F3 / B4）。

探查结构每个数据源最多取 200 张表（introspect._MAX_TABLES）。多出来的表不在表结构缓存里：目录页列不出，助手也
看不到字段。以前这件事哪儿都没说——目录清单只给探查到的 200 行，助手上下文里的「共几张表」是探查到的张数，
提示词里也不提。这组守的是：

- 目录清单接口返回 schema_truncated、schema_total（数据库里一共几张），界面据此说明「只探查了前 200 张」
- 助手上下文的 total 是表结构缓存里的真实总数；context 操作里带上 explored（探查到几张）
- 提示词里写明还有几张没探查到，遇到不认识的表名先查结构
- 没截断的库照旧，不多说一句

合成库：205 张空表，测试里临时建，用完连同数据源一起删掉。
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import DataSource
from app.main import app
from tests.fixtures.sources import drop_source

WIDE = 205


def _build(path, count: int) -> str:
    db = sqlite3.connect(path)
    for i in range(count):
        db.execute(f"CREATE TABLE t{i:03d} (id INTEGER PRIMARY KEY, amount INTEGER)")
    db.commit()
    db.close()
    return str(path)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _source(client, path: str) -> tuple[str, str]:
    name = f"wide_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/datasources", json={"name": name, "kind": "sqlite", "database": path,
                                                    "readonly": True})
    assert r.status_code in (200, 201), r.text
    sid = r.json()["id"]
    r = await client.post(f"/api/datasources/{sid}/introspect")
    assert r.status_code == 200, r.text
    return sid, name


@pytest.fixture
async def wide(client, tmp_path):
    sid, name = await _source(client, _build(tmp_path / "wide.db", WIDE))
    yield sid, name
    await drop_source(sid)


@pytest.fixture
async def narrow(client, tmp_path):
    sid, name = await _source(client, _build(tmp_path / "narrow.db", 3))
    yield sid, name
    await drop_source(sid)


async def _row(sid: str) -> DataSource:
    async with SessionLocal() as session:
        row = await session.get(DataSource, sid)
        session.expunge(row)
        return row


async def test_catalog_list_says_how_many_tables_were_not_introspected(client, wide):
    sid, _ = wide
    body = (await client.get(f"/api/datasources/{sid}/catalog")).json()
    assert len(body["tables"]) == 200
    assert body["schema_truncated"] is True and body["schema_total"] == WIDE


async def test_catalog_list_of_a_complete_schema(client, narrow):
    sid, _ = narrow
    body = (await client.get(f"/api/datasources/{sid}/catalog")).json()
    assert len(body["tables"]) == 3
    assert body["schema_truncated"] is False and body["schema_total"] == 3


async def test_assistant_context_counts_the_real_total(wide):
    from app.api import copilot_context

    sid, name = wide
    plan = await copilot_context.plan_context([await _row(sid)])
    ctx = plan.fallback("测试")
    (row,) = ctx.op()["sources"]
    assert row["total"] == WIDE and row["explored"] == 200
    text = ctx.section()
    assert f"共 {WIDE} 张表" in text and "只探查了 200 张" in text and f"还有 {WIDE - 200} 张没有探查到" in text
    # 遇到不认识的表名先查结构，别猜
    assert f"db_schema__{name}" in text and "information_schema" in text
    # 挑表的请求里也写明只列了探查到的
    pick = plan._pick_messages("各表的金额")[1][1]
    assert f"共 {WIDE} 张表，只探查了 200 张" in pick


async def test_assistant_context_of_a_complete_schema_says_nothing_extra(narrow):
    from app.api import copilot_context

    sid, _ = narrow
    plan = await copilot_context.plan_context([await _row(sid)])
    ctx = await plan.resolve(None, need="x")
    (row,) = ctx.op()["sources"]
    assert row["total"] == 3 and "explored" not in row
    assert "没有探查到" not in ctx.section()
