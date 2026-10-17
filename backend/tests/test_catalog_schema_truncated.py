"""探查范围外的表要说出来（数据目录完整性审查 F3 / B4；next-round 03 A1 之后按探查范围说）。

以前探查结构每个数据源最多取 200 张表，多出来的不在表结构缓存里，哪儿都没说。现在全库对象进对象索引，探查范围（规则）
圈出要读结构的那部分；范围外的表在对象索引里有名字。这组守的是：

- 目录清单接口返回 schema_truncated（有范围外的表）、schema_total（对象索引里一共几张），界面据此说明
- 助手的概览写「共 205 张表，探查范围内 200 张」；查看记录（look 事件）的 total 是对象总数，另带 explored（范围内几张）
- 查看结果里写明范围外的点名查看会按需加入；交回的表目录也写明只列了范围内的
- 运行时 db_schema 点名范围外的表：按需加入，给出字段
- 没有范围外的库照旧，不多说一句；升级前只探查了一部分的老缓存照旧说「只探查了」

合成库：205 张空表，探查范围规则只收 t0*、t1*（200 张），测试里临时建，用完连同数据源一起删掉。
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.base import SessionLocal
from app.db.models import DataSource
from app.main import app
from tests.fixtures.sources import drop_source, introspect_now

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


async def _source(client, path: str, *, include: list[str] | None = None) -> tuple[str, str]:
    name = f"wide_{uuid.uuid4().hex[:8]}"
    r = await client.post("/api/datasources", json={"name": name, "kind": "sqlite", "database": path,
                                                    "readonly": True})
    assert r.status_code in (200, 201), r.text
    sid = r.json()["id"]
    if include:
        r = await client.put(f"/api/datasources/{sid}/introspect-scope",
                             json={"scope": {"mode": "rules", "include": include}})
        assert r.status_code == 200, r.text
    await introspect_now(client, sid)
    return sid, name


@pytest.fixture
async def wide(client, tmp_path):
    sid, name = await _source(client, _build(tmp_path / "wide.db", WIDE), include=["t0*", "t1*"])
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
    from app.look.service import LookService
    from app.look.tables import TableKind

    sid, name = wide
    kind = await TableKind.load([await _row(sid)])
    looks = LookService([kind])
    overview = await looks.overview()
    assert f"共 {WIDE} 张表，探查范围内 200 张" in overview
    # 点名查看一张范围内的表：记录里 total 是对象总数，另带 explored；文字里说清范围外的点名会按需加入
    result = await looks.look({"op": "look", "need": "看一下 t000", "targets": [
        {"kind": "table", "source": name, "name": "t000"}]})
    (row,) = result.record["objects"]
    assert row["total"] == WIDE and row["explored"] == 200 and "added_to_scope" not in row
    assert f"共 {WIDE} 张表，探查范围内 200 张" in result.text
    # 遇到不认识的表名点名 look 或按 keyword 查，别猜
    assert f"db_schema__{name}" in result.text and "keyword" in result.text and "按需加入" in result.text
    # 交回的表目录也写明只列了范围内的
    assert f"共 {WIDE} 张表，探查范围内 200 张，下面只列范围内的" in await kind.index(None)


async def test_assistant_context_of_a_complete_schema_says_nothing_extra(narrow):
    from app.look.service import LookService
    from app.look.tables import TableKind

    sid, name = narrow
    looks = LookService([await TableKind.load([await _row(sid)])])
    assert "只探查了" not in await looks.overview()
    result = await looks.look({"op": "look", "need": "全部"})
    (row,) = result.record["objects"]
    assert row["total"] == 3 and "explored" not in row
    assert "没有探查到" not in result.text and "只探查了" not in result.text


async def test_schema_tool_adds_an_out_of_scope_table_instead_of_denying_it(wide, narrow):
    """点名一张探查范围外的表不能说「没有」：它在对象索引里，当场读结构、加进范围（每次运行有上限）。以前回「数据源中
    没有 X」，模型据此改用别的表，或者编一个答案。"""
    from app.tools.datasource import build_datasource_tools
    from app.tools.registry import ToolContext, Versions

    sid, name = wide
    async with SessionLocal() as session:
        (tool,) = await build_datasource_tools([f"db_schema__{name}"], ToolContext(data_versions=Versions.CURRENT),
                                               session)
    text = await tool.coroutine(table="t203")
    assert "amount" in text and "按需加入了探查范围" in text
    row = await _row(sid)
    assert "t203" in row.schema_cache["tables"]

    # 探查完整的库：查不存在的表照旧说没有
    async with SessionLocal() as session:
        (tool,) = await build_datasource_tools([f"db_schema__{(await _row(narrow[0])).name}"],
                                               ToolContext(data_versions=Versions.CURRENT), session)
    assert "没有 nope" in await tool.coroutine(table="nope")


def test_legacy_truncated_cache_still_does_not_deny():
    """升级前只探查了前 200 张的老缓存（没有对象索引）：查一张没探查到的表照旧说「只探查了」、请查数据字典。"""
    from types import SimpleNamespace

    from app.data.introspect import describe_table

    row = SimpleNamespace(name="legacy", schema_cache={"tables": {"t000": {"columns": [{"name": "id"}]}},
                                                       "truncated": True, "total": 205})
    text = describe_table(row, "t300")
    assert "中没有" not in text and "只探查了" in text and "information_schema" in text


async def test_catalog_detail_of_an_out_of_scope_table_says_so(client, wide):
    """对象索引里有、在探查范围外的表：GET /catalog/{表} 的 404 说「在探查范围外」，和 /schema、/catalog/search 同一套「不在」
    （下一轮验收：以前说「可能已被删除或尚未探查结构」）；表助手、批量审阅的同一个错误也一样。对象索引里也没有的说「没有
    这张表」。"""
    sid, _ = wide
    r = await client.get(f"/api/datasources/{sid}/catalog/t200")
    assert r.status_code == 404 and "探查范围外" in r.json()["detail"], r.text
    assert "可能已被删除" not in r.json()["detail"]
    found = (await client.get(f"/api/datasources/{sid}/catalog/search", params={"q": "t200"})).json()["items"]
    assert [i["absent_reason"] for i in found if i["name"] == "t200"] in (["out_of_scope"], [None]), found
    r = await client.post(f"/api/datasources/{sid}/catalog/t201/assistant", json={"messages": [
        {"role": "user", "content": "这张表是干什么的"}]})
    assert r.status_code == 404 and "探查范围外" in r.json()["detail"], r.text
    r = await client.post(f"/api/datasources/{sid}/catalog/review/batch", json={"items": [
        {"table_name": "t202", "path": "label", "action": "confirm", "if_version": 0}]})
    [result] = r.json()["results"]
    assert result["ok"] is False and "探查范围外" in result["error"], result
    r = await client.get(f"/api/datasources/{sid}/catalog/no_such_table")
    assert r.status_code == 404 and "没有表 no_such_table" in r.json()["detail"] and "探查范围外" not in r.json()["detail"]
