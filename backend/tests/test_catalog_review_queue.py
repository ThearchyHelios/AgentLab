"""数据目录的审阅队列、批量审阅、表被哪些运行用过、审阅进度（data/catalog_review.py）。

- GET  /catalog/review-queue：只列推断（proposed）的项，按「使用次数权重 × 风险权重 × 来源权重」从高到低，同分按表名、
  路径；facts（有效记录条件、去重规则、业务日期、关联关系、业务主键、度量类型）/ names（中文名、说明、含义、单位、码值、
  粒度、表类型）两组；按表筛选；分页；field_label 是给人看的位置（「列 status 的码值」）；reasons 写为什么排在这里
  （被几次运行用到，和 /usage 的 total 一致）；table_usage 是使用次数（同目录清单）；表结构里已经没有的表、列不列
- POST /catalog/review/batch：逐表按 if_version 乐观锁，一张表冲突不影响别的表；同一张表的几条一次写入；路径不对、表不存在
  逐条报
- GET  /catalog/{table}/usage：用过这张表的运行，新的在前，取法同 catalog.table_usage
- GET  /catalog/review-progress：常用 20 张表（使用次数前 20 张，同起草的默认范围：用到的不够 20 张时按表结构的顺序
  补齐）、全部、影响数字的项各自的确认情况

表名、列名、业务数字都是虚构的。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog, catalog_review
from app.data.catalog import make_item
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run
from app.main import app as _fastapi_app
from tests.fixtures.sources import drop_source

ACTOR = {"X-Actor": quote("林晓")}
TERMS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "terms.ts"


def _col(name: str, type_: str = "INTEGER") -> dict:
    return {"name": name, "type": type_, "nullable": True, "comment": None}


def _tables() -> dict:
    return {
        "orders": {"qualified": "orders", "primary_key": ["id"], "comment": None, "is_view": False,
                   "foreign_keys": [], "unique": [],
                   "columns": [_col("id"), _col("status"), _col("amount", "REAL"), _col("member_id"),
                               _col("paid_at", "TEXT")]},
        "members": {"qualified": "members", "primary_key": ["id"], "comment": None, "is_view": False,
                    "foreign_keys": [], "unique": [],
                    "columns": [_col("id"), _col("nick", "TEXT"), _col("tier")]},
        "coupons": {"qualified": "coupons", "primary_key": ["id"], "comment": None, "is_view": False,
                    "foreign_keys": [], "unique": [], "columns": [_col("id"), _col("face_value", "REAL")]},
    }


def _member_rel(status: str = "proposed", source: str = "name") -> dict:
    return {"id": catalog.relation_id("orders", ["member_id"], "members", ["id"]), "columns": ["member_id"],
            "to_table": "members", "to_columns": ["id"], "cardinality": "many_to_one", "coverage": 0.98,
            "source": source, "status": status}


ORDERS = {
    "label": make_item("订单", "llm"),
    "grain": make_item("每笔订单一行", "llm", "confirmed"),
    "valid_filter": make_item("status <> 9", "llm", note="剖析核对：排除 3.2% 的行（共 12,345 行）"),
    "kind": make_item("fact", "llm"),
    "columns": {
        "status": {"label": make_item("状态", "comment"), "codes": make_item({"1": "已支付", "9": ""}, "profile")},
        "amount": {"measure": make_item("flow", "llm"), "unit": make_item("元", "llm", "rejected")},
        # 表结构里已经没有这一列：不进队列
        "legacy_flag": {"label": make_item("旧标记", "llm")},
    },
    "relations": [_member_rel()],
}
MEMBERS = {"label": make_item("会员", "llm"), "keys": make_item(["id"], "name"),
           "columns": {"nick": {"label": make_item("昵称", "comment")}}}
COUPONS = {"label": make_item("优惠券", "comment")}
#: 表结构里已经没有这张表：不进队列
GONE = {"label": make_item("旧表", "llm")}

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


async def _use(sid: str, name: str, table_runs: dict[str, int]) -> list[str]:
    """给每张表造几次运行查询它（每次运行一份查询快照），返回运行 id（先建的在前、开始得早）。"""
    runs: list[str] = []
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    async with SessionLocal() as session:
        for table, n in table_runs.items():
            for _ in range(n):
                rid = uuid.uuid4().hex
                session.add(Run(id=rid, graph={}, workflow_name=f"周报-{table}", run_class="formal",
                                status="succeeded", started_at=base + timedelta(hours=len(runs))))
                session.add(Artifact(id=uuid.uuid4().hex, run_id=rid, kind=catalog.QUERY_SNAPSHOT_KIND,
                                     meta={"source_id": sid, "source": name, "tables": [table]}))
                runs.append(rid)
        await session.commit()
    return runs


async def _source(*, usage: dict[str, int] | None = None) -> str:
    name = f"rq_{uuid.uuid4().hex[:10]}"
    async with SessionLocal() as session:
        row = DataSource(name=name, kind="sqlite", database=":memory:", schema_cache={"tables": _tables()})
        session.add(row)
        await session.commit()
        sid = row.id
    _CREATED.append(sid)
    for table, notes in (("orders", ORDERS), ("members", MEMBERS), ("coupons", COUPONS), ("gone_table", GONE)):
        await _write(sid, table, notes)
    if usage:
        await _use(sid, name, usage)
    return sid


def _base(sid: str) -> str:
    return f"/api/datasources/{sid}/catalog"


# --------------------------------------------------------------------------
# 排序规则（纯函数）
# --------------------------------------------------------------------------


def test_priority_multiplies_usage_risk_and_source_weights():
    p = catalog_review.priority
    # 风险：有效记录条件 > 业务主键 > 码值 > 中文名
    assert p(0, ("t", "valid_filter"), "llm") > p(0, ("t", "keys"), "llm") > p(0, ("c", "x", "codes"), "llm") \
        > p(0, ("c", "x", "label"), "llm")
    assert p(0, ("r", "r1"), "name") == p(0, ("t", "dedup"), "name") == p(0, ("t", "business_date"), "name")
    assert p(0, ("c", "x", "measure"), "llm") == p(0, ("t", "keys"), "llm")
    assert p(0, ("t", "grain"), "llm") == p(0, ("t", "kind"), "llm") == p(0, ("c", "x", "codes"), "llm")
    assert p(0, ("c", "x", "meaning"), "llm") == p(0, ("c", "x", "unit"), "llm") == p(0, ("t", "label"), "llm")
    # 来源：模型起草、命名推断比数据库注释优先
    assert p(0, ("t", "label"), "llm") == p(0, ("t", "label"), "name") > p(0, ("t", "label"), "comment")
    # 使用次数：用得多的在前，但按对数增长，不压过风险
    assert p(37, ("t", "label"), "llm") > p(1, ("t", "label"), "llm") > p(0, ("t", "label"), "llm")
    assert p(37, ("t", "label"), "llm") == pytest.approx(
        p(0, ("t", "label"), "llm") * p(37, ("t", "label"), "comment") / p(0, ("t", "label"), "comment"))


def test_source_labels_follow_the_ui():
    text = TERMS.read_text(encoding="utf-8")
    block = re.search(r"export const CATALOG_SOURCE_LABEL\b[^=]*=\s*\{(.*?)\n\}", text, re.S)
    assert block
    ui = dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))
    assert ui == catalog_review.SOURCE_LABEL


# --------------------------------------------------------------------------
# 队列
# --------------------------------------------------------------------------


async def test_queue_lists_only_proposed_items_by_priority(client):
    sid = await _source(usage={"orders": 37, "members": 2})
    r = await client.get(f"{_base(sid)}/review-queue")
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"]
    paths = [(i["table_name"], i["path"]) for i in items]
    # 已确认、已驳回的不列；表结构里没有的表、列不列
    assert ("orders", "grain") not in paths and ("orders", "columns.amount.unit") not in paths
    assert not any(t == "gone_table" for t, _ in paths)
    assert ("orders", "columns.legacy_flag.label") not in paths
    assert body["total"] == len(items) == 11
    assert all(i["status"] == "proposed" for i in items)
    # 从高到低，同分按表名、路径
    keys = [(-i["priority"], i["table_name"], i["path"]) for i in items]
    assert keys == sorted(keys)
    first = items[0]
    rel_id = _member_rel()["id"]
    assert (first["table_name"], first["path"]) in {("orders", "valid_filter"), ("orders", f"relations.{rel_id}")}
    vf = next(i for i in items if i["path"] == "valid_filter")
    assert vf["field"] == "valid_filter" and vf["field_label"] == "表的有效记录条件"
    assert vf["value"] == "status <> 9" and vf["display"] == "status <> 9"
    assert vf["source"] == "llm" and vf["note"] == "剖析核对：排除 3.2% 的行（共 12,345 行）"
    assert vf["table_usage"] == 37 and vf["version"] == 1
    assert vf["reasons"] == ["常用表（被 37 次运行用到）", "影响数字的项（有效记录条件）", "模型起草，未确认"]
    # 常用表上的中文名排在没人用的表上的中文名前面；同一张表上影响数字的项排在中文名前面
    order = [(i["table_name"], i["path"]) for i in items]
    assert order.index(("orders", "label")) < order.index(("coupons", "label"))
    assert order.index(("members", "keys")) < order.index(("members", "label"))


async def test_queue_values_are_displayed_in_ui_words(client):
    sid = await _source()
    items = {(i["table_name"], i["path"]): i for i in (await client.get(f"{_base(sid)}/review-queue")).json()["items"]}
    rel = items[("orders", f"relations.{_member_rel()['id']}")]
    assert rel["field"] == "relation" and rel["field_label"] == "指向 members 的关联关系"
    assert rel["value"] == {"columns": ["member_id"], "to_table": "members", "to_columns": ["id"],
                            "cardinality": "many_to_one", "coverage": 0.98}
    assert rel["display"] == "member_id → members.id（多对一，覆盖率 98%）"
    assert rel["reasons"][-1] == "命名推断，未确认"
    kind = items[("orders", "kind")]
    assert kind["display"] == "明细表" and kind["value"] == "fact"
    codes = items[("orders", "columns.status.codes")]
    assert codes["field"] == "codes" and codes["field_label"] == "列 status 的码值" and codes["column"] == "status"
    assert codes["display"] == "1=已支付、9=含义待填写"
    assert "1 个码值的含义待填写" in codes["reasons"] and "数据剖析，未确认" in codes["reasons"]
    assert items[("orders", "columns.amount.measure")]["display"] == "可累加"
    assert items[("members", "keys")]["display"] == "id" and items[("members", "keys")]["field_label"] == "表的业务主键"
    assert items[("orders", "columns.amount.measure")]["field_label"] == "列 amount 的度量类型"
    assert items[("members", "label")]["reasons"] == ["名称和说明（中文名）", "模型起草，未确认"]
    assert items[("coupons", "label")]["reasons"] == ["名称和说明（中文名）", "数据库注释，未确认"]


async def test_queue_reasons_count_runs_like_the_usage_list(client):
    """同一次运行里查了几次这张表：原因写「被几次运行用到」（和 /usage 的 total 一样），table_usage 是使用次数（和目录清单的
    「使用 N 次」一样），两个数不一样时原因里两个都写；优先级按使用次数（同清单的排序）。"""
    sid = await _source()
    async with SessionLocal() as session:
        name = (await session.get(DataSource, sid)).name
    runs = await _use(sid, name, {"orders": 2, "members": 1})
    async with SessionLocal() as session:
        for _ in range(3):
            session.add(Artifact(id=uuid.uuid4().hex, run_id=runs[0], kind=catalog.QUERY_SNAPSHOT_KIND,
                                 meta={"source_id": sid, "source": name, "tables": ["orders"]}))
        await session.commit()
        usage = await catalog.table_usage(session, await session.get(DataSource, sid))
    assert usage["orders"] == 5
    items = {(i["table_name"], i["path"]): i for i in (await client.get(f"{_base(sid)}/review-queue")).json()["items"]}
    vf = items[("orders", "valid_filter")]
    assert vf["table_usage"] == 5
    assert vf["reasons"][0] == "常用表（被 2 次运行用到，使用 5 次）"
    assert items[("members", "label")]["reasons"][0] == "常用表（被 1 次运行用到）"
    assert vf["priority"] == round(catalog_review.priority(5, ("t", "valid_filter"), "llm"), 4)
    assert (await client.get(f"{_base(sid)}/orders/usage")).json()["total"] == 2


async def test_queue_filters_by_kind_and_table_and_pages(client):
    sid = await _source(usage={"orders": 3})
    facts = (await client.get(f"{_base(sid)}/review-queue", params={"kind": "facts"})).json()
    assert {i["field"] for i in facts["items"]} == {"valid_filter", "relation", "measure", "keys"}
    names = (await client.get(f"{_base(sid)}/review-queue", params={"kind": "names"})).json()
    assert {i["field"] for i in names["items"]} == {"label", "codes", "kind"}
    assert facts["total"] + names["total"] == 11
    only = (await client.get(f"{_base(sid)}/review-queue", params={"table": "MEMBERS"})).json()
    assert {i["table_name"] for i in only["items"]} == {"members"} and only["total"] == 3
    everything = (await client.get(f"{_base(sid)}/review-queue")).json()["items"]
    page = (await client.get(f"{_base(sid)}/review-queue", params={"limit": 4, "offset": 4})).json()
    assert page["total"] == 11 and page["items"] == everything[4:8]
    bad = await client.get(f"{_base(sid)}/review-queue", params={"kind": "other"})
    assert bad.status_code == 422
    assert (await client.get(f"{_base(sid)}/review-queue", params={"limit": 0})).status_code == 422
    missing = await client.get("/api/datasources/nope/catalog/review-queue")
    assert missing.status_code == 404


# --------------------------------------------------------------------------
# 批量审阅
# --------------------------------------------------------------------------


async def test_batch_review_writes_each_table_once_and_isolates_conflicts(client):
    sid = await _source()
    rel_path = f"relations.{_member_rel()['id']}"
    body = {"items": [
        {"table_name": "orders", "path": "valid_filter", "action": "confirm", "if_version": 1},
        {"table_name": "orders", "path": rel_path, "action": "reject", "if_version": 1},
        {"table_name": "orders", "path": "columns.nothing.label", "action": "confirm", "if_version": 1},
        {"table_name": "members", "path": "label", "action": "confirm", "if_version": 7},      # 版本不对
        {"table_name": "coupons", "path": "label", "action": "reject", "if_version": 1},
        {"table_name": "ghost", "path": "label", "action": "confirm", "if_version": 0},
    ]}
    r = await client.post(f"{_base(sid)}/review/batch", json=body, headers=ACTOR)
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [(x["table_name"], x["path"], x["ok"]) for x in results] == [
        ("orders", "valid_filter", True), ("orders", rel_path, True), ("orders", "columns.nothing.label", False),
        ("members", "label", False), ("coupons", "label", True), ("ghost", "label", False)]
    # 同一张表的两条一次写进去：版本只加 1
    assert results[0]["version"] == results[1]["version"] == 2
    assert results[2]["code"] == "catalog_path_invalid" and "找不到" in results[2]["error"]
    assert results[2]["version"] is None
    assert results[3]["code"] == "catalog_conflict" and "重新载入" in results[3]["error"]
    assert results[4]["version"] == 2 and results[4]["error"] is None and results[4]["code"] is None
    assert results[5]["code"] == "table_missing"
    async with SessionLocal() as session:
        orders = await catalog.read_entry(session, sid, "orders")
        members = await catalog.read_entry(session, sid, "members")
        coupons = await catalog.read_entry(session, sid, "coupons")
    assert orders.version == 2 and orders.updated_by == "林晓"
    assert orders.notes["valid_filter"]["status"] == "confirmed"
    assert orders.notes["relations"][0]["status"] == "rejected"
    assert members.version == 1 and members.notes["label"]["status"] == "proposed"
    assert coupons.notes["label"]["status"] == "rejected"
    # 撤销审阅：回到来源的初始状态
    again = await client.post(f"{_base(sid)}/review/batch", json={"items": [
        {"table_name": "orders", "path": "valid_filter", "action": "reset", "if_version": 2}]})
    assert again.json()["results"][0]["ok"] is True
    async with SessionLocal() as session:
        assert (await catalog.read_entry(session, sid, "orders")).notes["valid_filter"]["status"] == "proposed"


async def test_batch_review_limits(client):
    sid = await _source()
    many = [{"table_name": "orders", "path": "label", "action": "confirm", "if_version": 1}] * 201
    assert (await client.post(f"{_base(sid)}/review/batch", json={"items": many})).status_code == 422
    assert (await client.post(f"{_base(sid)}/review/batch", json={"items": []})).status_code == 422
    wrong = [{"table_name": "orders", "path": "label", "action": "approve", "if_version": 1}]
    assert (await client.post(f"{_base(sid)}/review/batch", json={"items": wrong})).status_code == 422
    ok = [{"table_name": "orders", "path": "label", "action": "confirm", "if_version": 1}] * 200
    r = await client.post(f"{_base(sid)}/review/batch", json={"items": ok})
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"])


# --------------------------------------------------------------------------
# 被哪些运行用过
# --------------------------------------------------------------------------


async def test_table_usage_lists_runs_newest_first(client):
    sid = await _source()
    async with SessionLocal() as session:
        name = (await session.get(DataSource, sid)).name
    runs = await _use(sid, name, {"orders": 3, "members": 1})
    # 同一次运行查了两次 orders：只算一次运行
    async with SessionLocal() as session:
        session.add(Artifact(id=uuid.uuid4().hex, run_id=runs[0], kind=catalog.QUERY_SNAPSHOT_KIND,
                             meta={"source_id": sid, "source": name, "tables": ["orders", "members"]}))
        # 别的数据源的同名表不算
        other = uuid.uuid4().hex
        session.add(Run(id=other, graph={}, workflow_name="别处", status="succeeded"))
        session.add(Artifact(id=uuid.uuid4().hex, run_id=other, kind=catalog.QUERY_SNAPSHOT_KIND,
                             meta={"source_id": "elsewhere", "source": "elsewhere", "tables": ["orders"]}))
        await session.commit()
    r = await client.get(f"{_base(sid)}/orders/usage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert [x["run_id"] for x in body["runs"]] == [runs[2], runs[1], runs[0]]
    first = body["runs"][0]
    assert first["workflow_name"] == "周报-orders" and first["run_class"] == "formal"
    assert first["status"] == "succeeded" and first["started_at"].startswith("2026-09-01T02:00")
    assert set(first) == {"run_id", "workflow_id", "workflow_name", "run_class", "status", "started_at"}
    limited = (await client.get(f"{_base(sid)}/orders/usage", params={"limit": 1})).json()
    assert limited["total"] == 3 and len(limited["runs"]) == 1
    members = (await client.get(f"{_base(sid)}/members/usage")).json()
    assert members["total"] == 2
    assert (await client.get(f"{_base(sid)}/ghost/usage")).status_code == 404


async def test_usage_counts_queries_and_the_runs_list_counts_runs(client):
    """两个数口径不同、说明照实：表的「使用次数」按查询计（同一次运行里查了两次算两次），「用过这张表的运行」按运行计。
    接口说明以前写「取法同使用次数」，看着该是同一个数，验收时 4 对 3 对不上。"""
    sid = await _source()
    async with SessionLocal() as session:
        name = (await session.get(DataSource, sid)).name
    runs = await _use(sid, name, {"orders": 3})
    async with SessionLocal() as session:
        session.add(Artifact(id=uuid.uuid4().hex, run_id=runs[0], kind=catalog.QUERY_SNAPSHOT_KIND,
                             meta={"source_id": sid, "source": name, "tables": ["orders"]}))
        await session.commit()
    listed = (await client.get(f"/api/datasources/{sid}/catalog")).json()
    assert next(t for t in listed["tables"] if t["table_name"] == "orders")["usage"] == 4
    assert (await client.get(f"{_base(sid)}/orders/usage")).json()["total"] == 3
    doc = _fastapi_app.openapi()["paths"]["/api/datasources/{source_id}/catalog/{table}/usage"]["get"]["description"]
    assert "取法同使用次数" not in doc and "按运行" in doc and "按查询" in doc, doc
    from app.data import catalog_review

    assert "取法同使用次数" not in (catalog_review.table_runs.__doc__ or "")


# --------------------------------------------------------------------------
# 进度
# --------------------------------------------------------------------------


async def test_review_progress_counts_top_tables_all_and_facts(client, monkeypatch):
    sid = await _source(usage={"orders": 5})
    r = await client.get(f"{_base(sid)}/review-progress")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["top_n"] == 20
    # 表结构里只有 3 张表：都算常用表（使用次数前 20 张）；表结构里没有的表、列不算
    assert body["top"] == {"tables": 3, "items_total": 13, "items_confirmed": 1}
    assert body["all"] == {"items_total": 13, "items_confirmed": 1, "items_proposed": 11, "items_rejected": 1}
    # 影响数字的项：有效记录条件、关联关系、度量类型、业务主键
    assert body["facts"] == {"proposed": 4, "confirmed": 0}
    await client.post(f"{_base(sid)}/review/batch", json={"items": [
        {"table_name": "orders", "path": "valid_filter", "action": "confirm", "if_version": 1}]})
    after = (await client.get(f"{_base(sid)}/review-progress")).json()
    assert after["top"]["items_confirmed"] == 2 and after["facts"] == {"proposed": 3, "confirmed": 1}
    # 只取前 2 张：用得多的在前，没人用的按表结构的顺序补（同起草的默认范围）
    monkeypatch.setattr(catalog_review, "TOP_N", 2)
    used = await _source(usage={"coupons": 1})
    top = (await client.get(f"{_base(used)}/review-progress")).json()["top"]
    assert top == {"tables": 2, "items_total": 1 + 9, "items_confirmed": 1}


def test_top_tables_are_the_draft_default_scope():
    """常用表和起草对话框「使用次数最多的 20 张表」是同一批：使用次数从多到少，一样多（包括都没用过）的按表结构里的顺序。"""
    tables = {f"t{i:02d}": {} for i in range(30)}
    usage = {"t29": 4, "t05": 4, "t17": 9}
    top = catalog_review.top_tables(tables, usage)
    assert catalog_review.TOP_N == catalog.DRAFT_DEFAULT_TABLES == 20
    assert top[:3] == ["t17", "t05", "t29"] and len(top) == 20
    assert top[3:] == [n for n in tables if n not in usage][:17]
    assert catalog_review.top_tables({"a": {}, "b": {}}, {}) == ["a", "b"]
