"""数据目录的导出导入（data/catalog_transfer.py）：本机整理好的目录带到部署实例上。

- GET  /catalog/export：附件「<数据源名>-数据目录.json」，{format: agentlab-catalog, version: 1, exported_at,
  source: {name, kind}, guidance, tables: {表名: {notes, version}}}，含全部状态的项（表结构里已经没有的表也带）
- POST /catalog/import/preview：只读，报告每张对上的表新增、更新、保留、冲突、跳过了什么
- POST /catalog/import：同样的报告，加每张表写入后的版本。规则：
  - 表按名字对（忽略大小写；带 schema 前缀的对不上时去掉前缀再对），对不上的列在 unmatched_tables
  - 文件里已确认、已驳回的项照带（来源、状态不变）；目标里已确认、已驳回的项和文件不一样时保留目标的、记冲突
  - 文件里推断、有确证的项只填目标的空缺，不覆盖目标已有的
  - 表结构里没有的列、关系两端对不上的跳过并写原因；目标数据源遮罩的列，码值不导入；有效记录条件用到的列（按目标的方言
    解析）不在目标的表结构里、引用别的表的列、带子查询的跳过（人工写的条件不限函数）
  - 逐表乐观锁，一张表写不进去不影响别的；有排队或进行中的起草任务时 409 catalog_draft_busy；文件格式不对 422
  - 起草要求：勾了导入、目标还没有时才写；目标已有的不覆盖

表名、列名、业务数字都是虚构的。
"""
from __future__ import annotations

import copy
import json
import uuid
from urllib.parse import quote, unquote

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog, catalog_jobs, catalog_transfer
from app.data.catalog import make_item
from app.data.engine import CATALOG_DRAFT_GUIDANCE_OPTION
from app.db.base import SessionLocal
from app.db.models import CatalogDraftJob, DataSource
from app.main import app as _fastapi_app
from tests.fixtures.sources import drop_source

ACTOR = {"X-Actor": quote("沈岚")}


def _col(name: str, type_: str = "INTEGER") -> dict:
    return {"name": name, "type": type_, "nullable": True, "comment": None}


def _tables(*, upper: bool = False) -> dict:
    """两边的表结构：目标那边（upper）表名、列名写法不同，还少了一列 tier。"""
    def n(name: str) -> str:
        return name.upper() if upper else name

    members_cols = [_col(n("id")), _col(n("nick"), "TEXT")] + ([] if upper else [_col("tier")])
    return {
        n("orders"): {"qualified": n("orders"), "primary_key": [n("id")], "comment": None, "is_view": False,
                      "foreign_keys": [], "unique": [],
                      "columns": [_col(n("id")), _col(n("status")), _col(n("amount"), "REAL"), _col(n("member_id")),
                                  _col(n("buyer_phone"), "TEXT")]},
        n("members"): {"qualified": n("members"), "primary_key": [n("id")], "comment": None, "is_view": False,
                       "foreign_keys": [], "unique": [], "columns": members_cols},
        n("refunds"): {"qualified": n("refunds"), "primary_key": [n("id")], "comment": None, "is_view": False,
                       "foreign_keys": [], "unique": [], "columns": [_col(n("id")), _col(n("order_id"))]},
    }


def _rel(to_table: str, columns: list[str], to_columns: list[str], *, status: str, source: str = "name",
         table: str = "orders") -> dict:
    return {"id": catalog.relation_id(table, columns, to_table, to_columns), "columns": columns,
            "to_table": to_table, "to_columns": to_columns, "cardinality": "many_to_one", "coverage": None,
            "source": source, "status": status}


#: 本机（源）的目录
SOURCE_ORDERS = {
    "label": make_item("订单", "human", "confirmed", at="2026-10-01T08:00:00+00:00"),
    "grain": make_item("每笔订单一行", "llm"),
    "valid_filter": make_item("status <> 9", "human", "confirmed"),
    "dedup": make_item("同一订单号取最后一次", "llm", "rejected"),
    "kind": make_item("fact", "llm", "confirmed"),
    "columns": {
        "status": {"codes": make_item({"1": "已支付", "9": "作废"}, "human", "confirmed", complete=True),
                   "label": make_item("状态", "llm")},
        "amount": {"unit": make_item("元", "human", "confirmed"), "measure": make_item("flow", "llm")},
        "buyer_phone": {"codes": make_item({"13900000001": "测试号"}, "human", "confirmed"),
                          "label": make_item("联系电话", "human", "confirmed")},
        # 目标表结构里没有这一列
        "gift_flag": {"label": make_item("赠品标记", "human", "confirmed")},
    },
    "relations": [
        _rel("members", ["member_id"], ["id"], status="confirmed"),
        # 目标那边没有这张表
        _rel("stores", ["member_id"], ["id"], status="rejected"),
    ],
}
SOURCE_MEMBERS = {"label": make_item("会员", "llm"), "keys": make_item(["tier"], "human", "confirmed"),
                  "columns": {"nick": {"label": make_item("昵称", "llm", "verified")}}}
#: 目标那边没有的表
SOURCE_GHOST = {"label": make_item("旧表", "human", "confirmed")}

#: 目标（部署实例）上已有的目录
TARGET_ORDERS = {
    "label": make_item("订单表", "human", "confirmed"),            # 和文件不一样、都确认过：冲突，保留目标
    "grain": make_item("一行一单", "llm"),                         # 文件里是推断：保留目标
    "valid_filter": make_item("STATUS <> 9", "llm"),               # 目标是推断、文件确认过：更新
    "kind": make_item("fact", "human", "confirmed"),               # 一样：保留
    "columns": {"STATUS": {"label": make_item("状态码", "comment")}},
}

_CREATED: list[str] = []


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    while _CREATED:
        await drop_source(_CREATED.pop())


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_fastapi_app), base_url="http://test") as c:
        yield c


async def _write(sid: str, table: str, notes: dict) -> None:
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, sid, table)
        await catalog.write_entry(session, sid, table, notes, if_version=entry.version if entry else 0, actor=None)


async def _source(name: str, tables: dict, entries: dict[str, dict], **kw) -> str:
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=":memory:", schema_cache={"tables": tables}, **kw)
        session.add(row)
        await session.commit()
        sid = row.id
    _CREATED.append(sid)
    for table, notes in entries.items():
        await _write(sid, table, notes)
    return sid


async def _pair() -> tuple[str, str]:
    local = await _source(f"本机库_{uuid.uuid4().hex[:6]}", _tables(),
                          {"orders": SOURCE_ORDERS, "members": SOURCE_MEMBERS, "ghost_table": SOURCE_GHOST},
                          options={CATALOG_DRAFT_GUIDANCE_OPTION: "金额一律按元"})
    remote = await _source(f"deploy_{uuid.uuid4().hex[:6]}", _tables(upper=True), {"ORDERS": TARGET_ORDERS},
                           options={"mask_columns": "buyer_phone"})
    return local, remote


def _base(sid: str) -> str:
    return f"/api/datasources/{sid}/catalog"


async def _export(client, sid: str) -> dict:
    r = await client.get(f"{_base(sid)}/export")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


async def test_export_is_a_json_attachment_with_every_item(client):
    local, _ = await _pair()
    r = await client.get(f"{_base(local)}/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "filename=\"agentlab-catalog-" in disposition
    async with SessionLocal() as session:
        name = (await session.get(DataSource, local)).name
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == f"{name}-数据目录.json"
    body = r.json()
    assert body["format"] == "agentlab-catalog" and body["version"] == 1
    assert body["exported_at"] and body["source"] == {"name": name, "kind": "sqlite"}
    assert body["guidance"] == "金额一律按元"
    assert set(body["tables"]) == {"orders", "members", "ghost_table"}
    assert body["tables"]["orders"]["version"] == 1
    # 含全部状态的项：驳回的也带
    assert body["tables"]["orders"]["notes"]["dedup"]["status"] == "rejected"
    assert body["tables"]["orders"]["notes"] == SOURCE_ORDERS
    assert body["tables"]["ghost_table"] == {"notes": SOURCE_GHOST, "version": 1}


def test_attachment_name_drops_characters_files_cannot_have():
    assert catalog_transfer.attachment_name('景区/客流:库?') == "景区_客流_库_-数据目录.json"
    assert catalog_transfer.attachment_name("   ") == "数据源-数据目录.json"


# --------------------------------------------------------------------------
# 预览与导入
# --------------------------------------------------------------------------


def _by(report: dict) -> dict[str, dict]:
    return {t["matched_as"]: t for t in report["tables"]}


async def test_preview_reports_and_writes_nothing(client):
    local, remote = await _pair()
    content = await _export(client, local)
    r = await client.post(f"{_base(remote)}/import/preview", json={"content": content, "include_guidance": True})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["unmatched_tables"] == ["ghost_table"]
    tables = _by(report)
    assert set(tables) == {"ORDERS", "MEMBERS"}
    orders = tables["ORDERS"]
    assert orders["table_name"] == "orders"
    conflicts = {c["path"]: c for c in orders["conflicts"]}
    assert set(conflicts) == {"label"}
    label = conflicts["label"]
    assert label["label"] == "表的中文名" and label["file_value"] == "订单" and label["target_value"] == "订单表"
    assert "保留" in label["why"]
    skipped = {s["path"]: s["why"] for s in orders["skipped"]}
    assert "columns.gift_flag.label" in skipped and "表结构里没有列 gift_flag" in skipped["columns.gift_flag.label"]
    assert "遮罩" in skipped["columns.buyer_phone.codes"]
    stores = next(p for p in skipped if p.startswith("relations."))
    assert "stores" in skipped[stores]
    # 新增：dedup（驳回的照带）、status 码值、amount 两项、联系电话的中文名、关联关系；更新：有效记录条件
    # 保留：粒度（文件里是推断，目标已有）、表类型（一样）、STATUS 的中文名（文件里是推断，目标已有）
    assert (orders["add"], orders["update"], orders["keep"]) == (6, 1, 3)
    members = tables["MEMBERS"]
    # 业务主键指着的列 tier 目标那边没有：跳过；会员的中文名和昵称填空缺
    assert (members["add"], members["update"], members["keep"]) == (2, 0, 0)
    assert [s["path"] for s in members["skipped"]] == ["keys"]
    assert report["totals"] == {"tables": 2, "add": 8, "update": 1, "keep": 3, "conflicts": 1,
                                "skipped": len(orders["skipped"]) + 1}
    assert report["guidance"] == {"file": "金额一律按元", "target": None, "action": "replace"}
    async with SessionLocal() as session:
        assert (await catalog.read_entry(session, remote, "ORDERS")).version == 1
        assert await catalog.read_entry(session, remote, "MEMBERS") is None
        assert catalog_jobs.guidance_of(await session.get(DataSource, remote)) is None


async def test_import_writes_by_the_rules_and_reports_versions(client):
    local, remote = await _pair()
    content = await _export(client, local)
    r = await client.post(f"{_base(remote)}/import", json={"content": content, "include_guidance": True},
                          headers=ACTOR)
    assert r.status_code == 200, r.text
    report = r.json()
    tables = _by(report)
    assert tables["ORDERS"]["version"] == 2 and tables["MEMBERS"]["version"] == 1
    assert tables["ORDERS"]["error"] is None
    assert report["guidance"]["action"] == "replace"
    async with SessionLocal() as session:
        orders = await catalog.read_entry(session, remote, "ORDERS")
        members = await catalog.read_entry(session, remote, "MEMBERS")
        row = await session.get(DataSource, remote)
    notes = orders.notes
    assert orders.updated_by == "沈岚"
    assert notes["label"]["value"] == "订单表"                                   # 冲突：保留目标的
    assert notes["grain"]["value"] == "一行一单"                                 # 推断只填空缺
    assert notes["valid_filter"] == {**SOURCE_ORDERS["valid_filter"]}            # 照带：来源、状态不变
    assert notes["dedup"]["status"] == "rejected" and notes["dedup"]["source"] == "llm"
    codes = notes["columns"]["STATUS"]["codes"]
    assert codes["value"] == {"1": "已支付", "9": "作废"} and codes["complete"] is True
    assert notes["columns"]["STATUS"]["label"]["value"] == "状态码"
    assert notes["columns"]["AMOUNT"]["measure"] == SOURCE_ORDERS["columns"]["amount"]["measure"]
    # 遮罩列：中文名照带，码值不导入
    assert notes["columns"]["BUYER_PHONE"] == {"label": SOURCE_ORDERS["columns"]["buyer_phone"]["label"]}
    assert "GIFT_FLAG" not in notes["columns"] and "gift_flag" not in notes["columns"]
    [rel] = notes["relations"]
    assert rel["id"] == catalog.relation_id("ORDERS", ["MEMBER_ID"], "MEMBERS", ["ID"])
    assert (rel["columns"], rel["to_table"], rel["to_columns"]) == (["MEMBER_ID"], "MEMBERS", ["ID"])
    assert rel["status"] == "confirmed" and rel["source"] == "name"
    assert catalog.validate_notes(notes) == []
    assert members.notes["label"]["status"] == "proposed" and "keys" not in members.notes
    assert members.notes["columns"]["NICK"]["label"]["status"] == "verified"
    assert catalog_jobs.guidance_of(row) == "金额一律按元"
    # 再导一次：都对上了，什么都不写；目标已经有起草要求，不覆盖
    again = (await client.post(f"{_base(remote)}/import", json={"content": content, "include_guidance": True})).json()
    assert again["totals"]["add"] == again["totals"]["update"] == 0
    assert _by(again)["ORDERS"]["version"] == 2
    assert again["guidance"] == {"file": "金额一律按元", "target": "金额一律按元", "action": "keep"}


async def test_target_rejection_beats_file_confirmation_and_guidance_is_optional(client):
    local, remote = await _pair()
    await _write(remote, "MEMBERS", {"label": make_item("会员", "llm", "rejected")})
    async with SessionLocal() as session:
        row = await session.get(DataSource, remote)
        await catalog_jobs.save_guidance(session, row, "部署实例自己的要求")
    content = await _export(client, local)
    content["tables"]["members"]["notes"]["label"] = make_item("会员", "human", "confirmed")
    report = (await client.post(f"{_base(remote)}/import",
                                json={"content": content, "include_guidance": False})).json()
    members = _by(report)["MEMBERS"]
    [conflict] = members["conflicts"]
    assert conflict["path"] == "label" and conflict["target_status"] == "rejected"
    assert conflict["file_status"] == "confirmed" and "驳回" in conflict["why"]
    assert report["guidance"] == {"file": "金额一律按元", "target": "部署实例自己的要求", "action": "none"}
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, remote, "MEMBERS")
        assert entry.notes["label"]["status"] == "rejected"
        assert catalog_jobs.guidance_of(await session.get(DataSource, remote)) == "部署实例自己的要求"


async def test_one_table_failing_does_not_stop_the_others(client, monkeypatch):
    local, remote = await _pair()
    content = await _export(client, local)
    real = catalog.write_entry

    async def flaky(session, source_id, table, notes, **kw):
        if table == "ORDERS":
            raise catalog.CatalogConflict(table, 99)
        return await real(session, source_id, table, notes, **kw)

    monkeypatch.setattr(catalog, "write_entry", flaky)
    report = (await client.post(f"{_base(remote)}/import", json={"content": content})).json()
    tables = _by(report)
    assert tables["ORDERS"]["error"] and "稍后" in tables["ORDERS"]["error"]
    assert tables["ORDERS"]["add"] == tables["ORDERS"]["update"] == 0
    assert tables["MEMBERS"]["error"] is None and tables["MEMBERS"]["version"] == 1


async def test_import_waits_for_draft_jobs_and_rejects_bad_files(client):
    local, remote = await _pair()
    content = await _export(client, local)
    async with SessionLocal() as session:
        session.add(CatalogDraftJob(id=uuid.uuid4().hex, source_id=remote, status="running"))
        await session.commit()
    busy = await client.post(f"{_base(remote)}/import", json={"content": content})
    assert busy.status_code == 409 and busy.json()["code"] == "catalog_draft_busy"
    # 预览是只读的，照常给
    assert (await client.post(f"{_base(remote)}/import/preview", json={"content": content})).status_code == 200
    bad = [
        "不是对象",
        {**content, "format": "something-else"},
        {**content, "version": 2},
        {**content, "tables": []},
        {**content, "tables": {"orders": {"notes": "x"}}},
        {**content, "guidance": 3},
    ]
    for body in bad:
        r = await client.post(f"{_base(local)}/import/preview", json={"content": body})
        assert r.status_code == 422, body
        assert any("一" <= ch <= "鿿" for ch in r.json()["detail"])
    # 界面直接交来文件原文也认；不是 JSON 的 422
    as_text = await client.post(f"{_base(local)}/import/preview", json={"content": json.dumps(content)})
    assert as_text.status_code == 200 and as_text.json()["totals"]["tables"] == 2
    not_json = await client.post(f"{_base(local)}/import/preview", json={"content": "{不是 JSON"})
    assert not_json.status_code == 422 and "JSON" in not_json.json()["detail"]
    too_long = {**content, "guidance": "长" * (catalog_jobs.GUIDANCE_MAX + 1)}
    r = await client.post(f"{_base(local)}/import/preview", json={"content": too_long, "include_guidance": True})
    assert r.status_code == 422 and "起草要求" in r.json()["detail"]


async def test_invalid_items_are_skipped_not_fatal(client):
    local, remote = await _pair()
    content = await _export(client, local)
    notes = content["tables"]["orders"]["notes"]
    notes["kind"] = {"value": "event", "source": "human", "status": "confirmed"}       # 取值不在可选值内
    notes["formula"] = {"value": "sum(amount)", "source": "human", "status": "confirmed"}  # 不认识的字段
    notes["columns"]["amount"]["colour"] = {"value": "红", "source": "human", "status": "confirmed"}
    report = (await client.post(f"{_base(remote)}/import/preview", json={"content": content})).json()
    skipped = {s["path"]: s["why"] for s in _by(report)["ORDERS"]["skipped"]}
    assert "明细表" in skipped["kind"]
    assert "formula" in skipped["formula"] and "columns.amount.colour" in skipped


async def test_valid_filter_columns_must_be_in_the_target_table(client):
    """有效记录条件是表级的一项，按列跳过的规矩拦不住它：解析出用到的列，不在目标的表结构里、是别的表的列、带子查询的都跳过
    并写原因。人工写的条件不限函数；带本表表名（文件里的写法、目标的写法都认）、大小写不同的照带。"""
    local, remote = await _pair()
    content = await _export(client, local)

    async def outcome(condition: str) -> tuple[str | None, int]:
        body = copy.deepcopy(content)
        body["tables"]["orders"]["notes"]["valid_filter"] = make_item(condition, "human", "confirmed")
        report = (await client.post(f"{_base(remote)}/import/preview", json={"content": body})).json()
        orders = _by(report)["ORDERS"]
        why = {s["path"]: s["why"] for s in orders["skipped"]}.get("valid_filter")
        return why, orders["update"]

    why, update = await outcome("gift_flag = 0 AND status <> 9")
    assert why and "gift_flag" in why and "目标的表结构" in why and update == 0
    why, _ = await outcome("members.tier = 1")
    assert why and "members.tier" in why and "别的表" in why
    why, _ = await outcome("member_id IN (SELECT id FROM members)")
    assert why and "子查询" in why
    why, _ = await outcome("status <> 9; DELETE FROM orders")
    assert why and "条件" in why
    for fine in ("orders.status <> 9", "ORDERS.STATUS <> 9 AND ABS(amount) > 0", "buyer_phone IS NOT NULL"):
        why, update = await outcome(fine)
        assert why is None and update == 1, fine


async def test_import_needs_a_schema(client):
    local, _ = await _pair()
    content = await _export(client, local)
    empty = await _source(f"empty_{uuid.uuid4().hex[:6]}", {}, {})
    r = await client.post(f"{_base(empty)}/import/preview", json={"content": content})
    assert r.status_code == 409 and r.json()["code"] == "schema_missing"


async def test_round_trip_into_an_empty_copy_reproduces_the_catalog(client):
    local, _ = await _pair()
    content = await _export(client, local)
    copy_sid = await _source(f"copy_{uuid.uuid4().hex[:6]}", _tables(), {})
    report = (await client.post(f"{_base(copy_sid)}/import", json={"content": json.loads(json.dumps(content)),
                                                                     "include_guidance": True})).json()
    assert report["unmatched_tables"] == ["ghost_table"]
    assert all(not t["conflicts"] for t in report["tables"])
    async with SessionLocal() as session:
        mine = await catalog.read_entry(session, local, "orders")
        theirs = await catalog.read_entry(session, copy_sid, "orders")
    # 关系两端都对得上的照带；指向目标那边没有的表的跳过
    expected = copy.deepcopy(mine.notes)
    expected["relations"] = [r for r in mine.notes["relations"] if r["to_table"] != "stores"]
    del expected["columns"]["gift_flag"]                     # 表结构里没有这一列
    assert theirs.notes == expected
