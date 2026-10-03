"""数据目录的存储：catalog_notes 表、版本号与乐观锁、结构校验。

目录是多人可改的长期资产（助手起草、人工确认、阶段 2 的数据剖析都往里写），所以每次写入必须带上
「我是基于第几版改的」：两个人同时打开同一张表各改各的，后提交的那个要被拦下来重新载入，不能悄悄
盖掉前一个人的确认。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.data import catalog
from app.db.base import SessionLocal
from app.db.models import CatalogNote, DataSource


async def _source() -> str:
    async with SessionLocal() as session:
        row = DataSource(name=f"cat_{uuid.uuid4().hex[:10]}", kind="sqlite", database=":memory:")
        session.add(row)
        await session.commit()
        return row.id


def _label(value: str, source: str = "human", status: str = "confirmed") -> dict:
    return {"value": value, "source": source, "status": status}


async def test_first_write_creates_version_one_and_records_actor():
    sid = await _source()
    async with SessionLocal() as session:
        entry = await catalog.write_entry(session, sid, "visits", {"label": _label("入园记录")},
                                          if_version=0, actor="王敏")
    assert entry.version == 1
    assert entry.updated_by == "王敏"
    assert entry.notes["label"]["value"] == "入园记录"
    async with SessionLocal() as session:
        again = await catalog.read_entry(session, sid, "visits")
    assert again is not None and again.version == 1 and again.updated_at is not None


async def test_each_write_bumps_version_and_stale_version_conflicts():
    sid = await _source()
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "visits", {"label": _label("入园记录")}, if_version=0, actor=None)
        second = await catalog.write_entry(session, sid, "visits", {"label": _label("入园明细")},
                                           if_version=1, actor="李雷")
        assert second.version == 2
        # 另一个人还拿着第 1 版：被拦下，内容不变
        with pytest.raises(catalog.CatalogConflict):
            await catalog.write_entry(session, sid, "visits", {"label": _label("别的名字")}, if_version=1, actor="韩梅")
        # 以为这张表还没有目录（if_version=0），其实已经有了：同样是冲突
        with pytest.raises(catalog.CatalogConflict):
            await catalog.write_entry(session, sid, "visits", {"label": _label("别的名字")}, if_version=0, actor=None)
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "visits")
    assert entry.version == 2 and entry.notes["label"]["value"] == "入园明细" and entry.updated_by == "李雷"


async def test_unchanged_notes_do_not_bump_version():
    """内容没变就不升版本：冻结进表结构快照的是 {版本, 内容}，空写一次也升版本的话，快照哈希会无故变化。"""
    sid = await _source()
    notes = {"label": _label("渠道")}
    async with SessionLocal() as session:
        first = await catalog.write_entry(session, sid, "channels", notes, if_version=0, actor="甲")
        same = await catalog.write_entry(session, sid, "channels", dict(notes), if_version=1, actor="乙")
    assert same.version == first.version == 1
    assert same.updated_by == "甲"


async def test_unique_per_source_and_table():
    sid = await _source()
    async with SessionLocal() as session:
        session.add(CatalogNote(source_id=sid, table_name="visits", notes={}, version=1))
        await session.commit()
        session.add(CatalogNote(source_id=sid, table_name="visits", notes={}, version=1))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_deleting_source_cascades():
    sid = await _source()
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "visits", {"label": _label("入园记录")}, if_version=0, actor=None)
        row = await session.get(DataSource, sid)
        await session.delete(row)
        await session.commit()
    async with SessionLocal() as session:
        left = (await session.execute(select(CatalogNote).where(CatalogNote.source_id == sid))).scalars().all()
    assert left == []


async def test_read_catalog_lists_all_tables_of_a_source():
    sid, other = await _source(), await _source()
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "visits", {"label": _label("入园记录")}, if_version=0, actor=None)
        await catalog.write_entry(session, sid, "stores", {"label": _label("门店")}, if_version=0, actor=None)
        await catalog.write_entry(session, other, "visits", {"label": _label("别处")}, if_version=0, actor=None)
        entries = await catalog.read_catalog(session, sid)
    assert set(entries) == {"visits", "stores"}
    assert entries["stores"].notes["label"]["value"] == "门店"


@pytest.mark.parametrize("notes, fragment", [
    ({"label": "入园记录"}, "中文名"),                                                      # 不是项
    ({"label": {"value": "x", "source": "guess", "status": "proposed"}}, "来源"),            # 来源不在取值里
    ({"label": {"value": "x", "source": "llm", "status": "maybe"}}, "状态"),
    ({"kind": _label("table")}, "表类型"),
    ({"keys": _label("visit_id")}, "业务主键"),                                              # 应为字符串列表
    ({"business_date": _label({"column": "visit_date", "rule": 1})}, "业务日期"),
    ({"columns": {"amount": {"measure": _label("sum")}}}, "列 amount 的度量类型"),
    ({"columns": {"status": {"codes": _label({"1": 2})}}}, "列 status 的码值"),
    ({"formula": _label("sum(amount)")}, "formula"),                                         # 目录不放公式
    ({"relations": [{"id": "r1", "columns": ["a"], "to_table": "t", "to_columns": [],
                     "source": "fk", "status": "verified"}]}, "关联关系 r1"),
])
def test_validate_rejects_malformed_notes(notes, fragment):
    problems = catalog.validate_notes(notes)
    assert problems, notes
    assert any(fragment in p for p in problems), problems


def test_validate_accepts_full_example():
    notes = {
        "label": _label("入园记录"), "description": _label("每次检票入园记一行", "llm", "proposed"),
        "grain": _label("每次入园"), "keys": _label(["visit_id"]), "kind": _label("fact"),
        "business_date": _label({"column": "visit_date", "rule": "按检票时间计", "timezone": "Asia/Shanghai"}),
        "valid_filter": _label("status = 1"), "dedup": _label("同一票号只计一次"),
        "columns": {"amount": {"label": _label("实收金额"), "meaning": _label("扣除优惠后的金额"),
                               "unit": _label("元"), "measure": _label("flow"),
                               "codes": _label({"1": "有效"}, "comment", "proposed")}},
        "relations": [{"id": catalog.relation_id("visits", ["store_id"], "stores", ["id"]), "columns": ["store_id"],
                       "to_table": "stores", "to_columns": ["id"], "cardinality": "many_to_one", "coverage": None,
                       "source": "fk", "status": "verified"}],
    }
    assert catalog.validate_notes(notes) == []


async def test_write_rejects_invalid_notes():
    sid = await _source()
    async with SessionLocal() as session:
        with pytest.raises(catalog.CatalogInvalid) as e:
            await catalog.write_entry(session, sid, "visits", {"label": "入园记录"}, if_version=0, actor=None)
    assert e.value.problems
