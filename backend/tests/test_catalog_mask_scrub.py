"""遮罩列的历史清理：新设遮罩时把目录里这些列留下的取值类历史清掉。

遮罩列（数据源 options.mask_columns）以前只在读取侧过滤，目录里写下的码值、含义、备注、确认依据照旧带着取值。
data/catalog.scrub_masked_columns 在「新设遮罩」时清掉这些历史：列级的 meaning、codes 整项删掉，其它列级项和
关系只删 note、reason，列名、中文名、单位、度量类型这些列本身的事实保留。表级说明不动——遮罩的是一个列。

三个入口各验一次：直接调用 scrub（幂等、版本号、不分大小写）；PATCH 数据源时新设遮罩；记录表新增敏感列时
（_sync_registration）跟随发布/起草的那个事务清理。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog
from app.data.catalog import make_item
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.main import app as _fastapi_app
from tests.fixtures.sources import drop_source, introspect_now

ACTOR = {"X-Actor": quote("王敏")}


def _notes(**overrides) -> dict:
    """订单表的目录：手机号（要遮的列，每个字段都带着可能泄值的说明和依据）、订单号（不动的列）、
    两条关系（一条连手机号、一条连订单号）和一段表级说明。"""
    notes = {
        "label": make_item("订单", "human", note="一行一个订单", reason="和业务方核对过"),
        "columns": {
            "phone": {
                "label": make_item("手机号", "human", note="样例 13800000000", reason="按样例确认过"),
                "meaning": make_item("会员手机号", "llm", note="取值形如 138…"),
                "unit": make_item("个", "llm"),
                "measure": make_item("attribute", "llm"),
                "codes": make_item({"138": "移动"}, "profile", note="前三个号码取的样"),
            },
            "order_id": {
                "label": make_item("订单号", "human", note="业务主键"),
                "meaning": make_item("订单的编号", "llm"),
                "unit": make_item("个", "llm"),
                "measure": make_item("identifier", "llm"),
                "codes": make_item({"A": "正常单"}, "human", complete=True),
            },
        },
        "relations": [
            {"id": catalog.relation_id("orders", ["phone"], "members", ["id"]), "columns": ["phone"],
             "to_table": "members", "to_columns": ["id"], "cardinality": "many_to_one", "coverage": None,
             "source": "name", "status": "proposed", "note": "样例会员 13800000000 对得上",
             "reason": "抽查了几行"},
            {"id": catalog.relation_id("orders", ["order_id"], "order_lines", ["order_id"]), "columns": ["order_id"],
             "to_table": "order_lines", "to_columns": ["order_id"], "cardinality": "one_to_many", "coverage": 0.98,
             "source": "profile", "status": "verified", "note": "覆盖率 98%"},
        ],
    }
    notes.update(overrides)
    return notes


async def _source() -> str:
    async with SessionLocal() as session:
        row = DataSource(name=f"mask_{uuid.uuid4().hex[:10]}", kind="sqlite", database=":memory:")
        session.add(row)
        await session.commit()
        return row.id


async def test_scrub_clears_masked_column_values_but_keeps_the_column():
    sid = await _source()
    original = _notes()
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "orders", original, if_version=0, actor="起草人")

    async with SessionLocal() as session:
        changed = await catalog.scrub_masked_columns(session, sid, [" Phone "], actor="王敏")
    assert changed == 1

    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "orders")
    assert entry is not None
    phone = entry.notes["columns"]["phone"]
    assert "meaning" not in phone and "codes" not in phone, "码值和含义可能带取值，整项删掉"
    assert phone["label"]["value"] == "手机号" and "note" not in phone["label"] and "reason" not in phone["label"]
    assert phone["unit"]["value"] == "个" and phone["measure"]["value"] == "attribute"
    # 不遮的列原样不动
    assert entry.notes["columns"]["order_id"] == original["columns"]["order_id"]
    # 表级说明不懂：遮的是列，不是整张表
    assert entry.notes["label"] == original["label"]
    assert entry.version == 2 and entry.updated_by == "王敏"

    # 幂等：没有可清的了，不升版本
    async with SessionLocal() as session:
        assert await catalog.scrub_masked_columns(session, sid, ["PHONE"]) == 0
    async with SessionLocal() as session:
        again = await catalog.read_entry(session, sid, "orders")
    assert again is not None and again.version == 2


async def test_scrub_only_drops_notes_and_reasons_on_relations():
    sid = await _source()
    original = _notes()
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, "orders", original, if_version=0, actor=None)
    async with SessionLocal() as session:
        await catalog.scrub_masked_columns(session, sid, ["phone"], commit=True)

    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, "orders")
    assert entry is not None
    ends_masked, untouched = entry.notes["relations"]
    assert "note" not in ends_masked and "reason" not in ends_masked, "关系说明里可能带样例值"
    assert {k: v for k, v in ends_masked.items() if k not in ("note", "reason")} == {
        k: v for k, v in original["relations"][0].items() if k not in ("note", "reason")}
    assert untouched == original["relations"][1], "两端都不是遮罩列的关系不动"


# --------------------------------------------------------------------------
# 两个入口
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_fastapi_app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def source_id(client, scenic_db):
    """接上合成景区库并探查结构（走真实的探查接口），和 test_catalog_api.py 一样。"""
    async with SessionLocal() as session:
        row = DataSource(name=f"mask_{uuid.uuid4().hex[:10]}", kind="sqlite", database=scenic_db)
        session.add(row)
        await session.commit()
        sid = row.id
    await introspect_now(client, sid)
    yield sid
    await drop_source(sid)


async def test_patch_settings_new_mask_scrubs_the_catalog(client, source_id):
    async with SessionLocal() as session:
        await catalog.write_entry(session, source_id, "visits", {
            "columns": {"ticket_no": {"label": make_item("票号", "human", note="样例 T-0001"),
                                      "meaning": make_item("门票的编号", "llm"),
                                      "measure": make_item("identifier", "llm"),
                                      "codes": make_item({"T": "纸票"}, "human")}},
        }, if_version=0, actor=None)

    # options 是整份替换：带上原来的 options 再加这一项
    listed = (await client.get("/api/datasources")).json()
    options = next(item for item in listed if item["id"] == source_id)["options"]
    r = await client.patch(f"/api/datasources/{source_id}",
                           json={"options": {**options, "mask_columns": "ticket_no"}}, headers=ACTOR)
    assert r.status_code == 200, r.text
    assert r.json()["options"]["mask_columns"] == "ticket_no"

    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, source_id, "visits")
    assert entry is not None
    ticket = entry.notes["columns"]["ticket_no"]
    assert "meaning" not in ticket and "codes" not in ticket
    assert ticket["label"]["value"] == "票号" and "note" not in ticket["label"]
    assert entry.version == 2


async def test_records_sensitive_column_scrubs_inside_the_callers_transaction():
    """记录表新增敏感列时由 _sync_registration 清理：只 flush、不提交，跟随 ensure_table 的事务。"""
    from app.records import store
    from app.records.tables import ensure_table

    owner = SimpleNamespace(id=f"mask{uuid.uuid4().hex[:10]}", app_id=None, name="门店报损")
    columns = [{"name": "门店", "type": "string"}, {"name": "电话", "type": "string"}]
    async with SessionLocal() as session:
        row = await ensure_table(session, owner, columns)
        await session.commit()
        sid = row.source_id
    async with SessionLocal() as session:
        await catalog.write_entry(session, sid, store.TABLE, {
            "columns": {"电话": {"label": make_item("电话", "human"), "meaning": make_item("联系电话", "llm")}},
        }, if_version=0, actor=None)

    # 同一份「填什么」，电话标成敏感：options 里多了这个遮罩列，目录在同一次提交里清掉
    async with SessionLocal() as session:
        await ensure_table(session, owner, [columns[0], {**columns[1], "sensitive": True}])
        await session.commit()

    async with SessionLocal() as session:
        source = await session.get(DataSource, sid)
        entry = await catalog.read_entry(session, sid, store.TABLE)
    assert source is not None and source.options["mask_columns"] == ["电话"]
    assert entry is not None and entry.version == 2
    assert "meaning" not in entry.notes["columns"]["电话"] and entry.notes["columns"]["电话"]["label"]["value"] == "电话"
