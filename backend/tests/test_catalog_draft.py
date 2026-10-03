"""数据目录的起草：数据库注释、外键约束、命名推断、模型起草，以及并入已有目录。

这一阶段起草不发数据库查询（数据剖析归阶段 2），只看探查缓存里的结构。
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.data import catalog
from app.data.catalog import make_item
from app.db.base import SessionLocal
from app.db.models import DataSource


def _col(name: str, type_: str = "INTEGER", comment: str | None = None, nullable: bool = True) -> dict:
    return {"name": name, "type": type_, "nullable": nullable, "comment": comment}


def _table(*cols: dict, pk=("id",), fks=None, unique=None, comment=None) -> dict:
    meta = {"qualified": "", "schema": None, "columns": list(cols), "primary_key": list(pk), "comment": comment,
            "is_view": False}
    if fks is not None:
        meta["foreign_keys"] = fks
    if unique is not None:
        meta["unique"] = unique
    return meta


def _source(tables: dict, **kw) -> SimpleNamespace:
    for name, meta in tables.items():
        meta["qualified"] = name
    return SimpleNamespace(id=kw.pop("id", "src"), name="scenic", kind="sqlite", origin=kw.pop("origin", "manual"),
                           schema_cache={"tables": tables, **kw})


def _rels(notes: dict) -> set[tuple]:
    return {(tuple(r["columns"]), r["to_table"], tuple(r["to_columns"]), r["source"], r["status"])
            for r in notes.get("relations", [])}


# ---------------------------------------------------------------- 数据库注释


def test_comments_become_labels_or_meanings():
    src = _source({"visits": _table(
        _col("id"), _col("amount", "REAL", comment="实收金额"),
        _col("status", comment="状态：1 表示有效，0 表示作废；作废记录不计入客流"),
        comment="入园记录")})
    draft = catalog.draft_structure(src, "visits")
    assert draft.notes["label"] == make_item("入园记录", "comment")
    assert draft.notes["columns"]["amount"]["label"] == make_item("实收金额", "comment")
    assert draft.notes["columns"]["status"]["meaning"]["value"].startswith("状态：1 表示有效")
    assert "label" not in draft.notes["columns"]["status"]
    assert "comment" in draft.covered


def test_long_table_comment_goes_to_description():
    src = _source({"visits": _table(_col("id"), comment="游客每次检票入园记一行，团队票按人数拆成多行")})
    draft = catalog.draft_structure(src, "visits")
    assert "label" not in draft.notes
    assert draft.notes["description"]["value"].startswith("游客每次检票入园")


@pytest.mark.parametrize("kw", [{"origin": "upload"}, {"import_mode": "recipe"}])
def test_imported_table_comments_are_not_copied(kw):
    """导入表格的说明是按核对结果生成的（recipe_notes），数据目录不重复它们。"""
    src = _source({"日报": _table(_col("日期", "TEXT", comment="格式 YYYY-MM-DD"), _col("客流", comment="单位：人次"),
                                  pk=(), comment="粒度：每个日期")}, **kw)
    assert catalog.system_notes_source(src)
    draft = catalog.draft_structure(src, "日报")
    assert "label" not in draft.notes and "description" not in draft.notes
    assert "columns" not in draft.notes


# ---------------------------------------------------------------- 外键约束


def test_foreign_keys_become_verified_relations():
    src = _source({
        "visits": _table(_col("id"), _col("park_id"), fks=[
            {"columns": ["park_id"], "to_table": "parks", "to_columns": ["id"]}]),
        "parks": _table(_col("id"), fks=[]),
    })
    draft = catalog.draft_structure(src, "visits")
    assert _rels(draft.notes) == {(("park_id",), "parks", ("id",), "fk", "verified")}
    rel = draft.notes["relations"][0]
    assert rel["cardinality"] == "many_to_one"
    assert rel["id"] == catalog.relation_id("visits", ["park_id"], "parks", ["id"])
    assert "fk" in draft.covered


def test_foreign_key_without_target_columns_points_at_primary_key_and_one_to_one():
    src = _source({
        "member_profiles": _table(_col("member_id"), _col("nickname", "TEXT"), pk=("member_id",),
                                  fks=[{"columns": ["member_id"], "to_table": "members", "to_columns": []}]),
        "members": _table(_col("id"), fks=[]),
    })
    rel = catalog.draft_structure(src, "member_profiles").notes["relations"][0]
    assert rel["to_columns"] == ["id"] and rel["cardinality"] == "one_to_one"


def test_old_cache_without_foreign_keys_does_not_cover_fk():
    """旧缓存里没有 foreign_keys：不知道有没有外键，不能据此删掉已有的外键关系。"""
    src = _source({"visits": _table(_col("id"), _col("park_id")), "parks": _table(_col("id"))})
    draft = catalog.draft_structure(src, "visits")
    assert "fk" not in draft.covered


# ---------------------------------------------------------------- 命名推断


def _infer(tables: dict, table: str) -> set[tuple]:
    src = _source(tables)
    return {(tuple(r["columns"]), r["to_table"], tuple(r["to_columns"]))
            for r in catalog.infer_name_relations(src.schema_cache, table)}


@pytest.mark.parametrize("column, target", [
    ("channel_id", "channels"),            # 复数
    ("category_id", "categories"),         # y → ies
    ("storeId", "stores"),                 # 驼峰
    ("storeID", "stores"),                 # xxxID
    ("ticket_type_id", "ticket_types"),    # 多段下划线
    ("memberTagId", "memberTags"),         # 驼峰表名
    ("member_tag_id", "memberTags"),       # 下划线列名对驼峰表名
    ("park_id", "park"),                   # 单数表名
    ("CHANNEL_ID", "CHANNELS"),            # 大写（Oracle）
])
def test_name_inference_positive(column, target):
    tables = {"facts": _table(_col("id"), _col(column)), target: _table(_col("id"))}
    pk = "id"
    assert _infer(tables, "facts") == {((column,), target, (pk,))}


@pytest.mark.parametrize("tables, why", [
    ({"employees": _table(_col("id"), _col("employee_id", "TEXT"))}, "自引用"),
    ({"facts": _table(_col("id"), _col("guide_id")), "guide": _table(_col("id")), "guides": _table(_col("id"))},
     "多个候选"),
    ({"facts": _table(_col("id"), _col("lot_id")), "parking_lots": _table(_col("id"))}, "没有对应的表"),
    ({"facts": _table(_col("id"), _col("store_id", "VARCHAR(20)")), "stores": _table(_col("id"))}, "类型对不上"),
    ({"facts": _table(_col("id"), _col("day_id")), "days": _table(_col("park_id"), _col("d"), pk=("park_id", "d"))},
     "被指向的表是复合主键"),
    ({"facts": _table(_col("id"), _col("tag_id")), "tags": _table(_col("name", "TEXT"), pk=())}, "被指向的表没有主键"),
    ({"facts": _table(_col("id"), _col("x_id")), "xs": _table(_col("id"))}, "词根太短"),
    ({"facts": _table(_col("id"), _col("paid")), "pas": _table(_col("id"))}, "不是 _id 结尾"),
    ({"facts": _table(_col("id"), _col("store_id"), fks=[{"columns": ["store_id"], "to_table": "shops",
                                                          "to_columns": ["id"]}]),
      "stores": _table(_col("id")), "shops": _table(_col("id"))}, "已有外键约束的列"),
])
def test_name_inference_negative(tables, why):
    assert _infer(tables, next(iter(tables))) == set(), why


def test_name_inference_one_to_one_when_column_is_own_key():
    tables = {"member_profiles": _table(_col("member_id"), pk=("member_id",)), "members": _table(_col("id"))}
    rel = catalog.infer_name_relations(_source(tables).schema_cache, "member_profiles")[0]
    assert rel["cardinality"] == "one_to_one" and rel["source"] == "name" and rel["status"] == "proposed"


# ---------------------------------------------------------------- 模型起草


class FakeModel:
    """假模型：结构化输出返回 replies[表名] 拼成的 {"tables": [...]}；fail 里的表所在的那一批抛异常。"""

    def __init__(self, replies: dict[str, dict], fail: set[str] = frozenset()) -> None:
        self.replies, self.fail, self.calls = replies, set(fail), []

    def with_structured_output(self, schema: Any, **_: Any) -> "FakeModel":
        assert schema["title"]
        return self

    async def ainvoke(self, messages: list[Any]) -> dict:
        text = "\n".join(str(getattr(m, "content", m)) for m in messages)
        self.calls.append(text)
        names = [t for t in self.replies if f"表 {t}" in text]
        if any(t in self.fail for t in names):
            raise RuntimeError("网关超时")
        return {"tables": [{"table": t, **self.replies[t]} for t in names]}


_VISITS_REPLY = {
    "label": "入园记录", "grain": "每张门票每次检票一行", "kind": "fact", "keys": ["ticket_no"],
    "business_date": {"column": "visit_time", "rule": "按检票时间计", "timezone": "+08:00"},
    "columns": [
        {"name": "visitor_count", "label": "入园人数", "meaning": "这次检票进园的人数", "unit": "人", "measure": "flow"},
        {"name": "status", "label": "状态", "measure": "status"},
        {"name": "ghost", "label": "不存在的列"},                    # 丢掉
        {"name": "ticket_no", "measure": "money"},                   # 度量类型不在取值里：丢掉这一项
    ],
}


def _scenic_tables() -> dict:
    return {
        "visits": _table(_col("id"), _col("ticket_no", "TEXT"), _col("visit_time", "TEXT"), _col("visitor_count"),
                         _col("status"), fks=[]),
        "channels": _table(_col("id"), _col("name", "TEXT"), fks=[]),
    }


async def test_model_draft_maps_fields_and_drops_invalid_values():
    src = _source(_scenic_tables())
    model = FakeModel({"visits": _VISITS_REPLY, "channels": {"label": "渠道", "kind": "planet",
                                                             "keys": ["nope"]}})
    got = await catalog.draft_with_model(model, src, ["visits", "channels"])
    visits = got["visits"].notes
    assert visits["label"] == make_item("入园记录", "llm")
    assert visits["grain"]["value"] == "每张门票每次检票一行" and visits["kind"]["value"] == "fact"
    assert visits["keys"]["value"] == ["ticket_no"]
    assert visits["business_date"]["value"] == {"column": "visit_time", "rule": "按检票时间计", "timezone": "+08:00"}
    assert visits["columns"]["visitor_count"]["unit"] == make_item("人", "llm")
    assert visits["columns"]["status"]["measure"]["value"] == "status"
    assert "ghost" not in visits["columns"] and "ticket_no" not in visits["columns"]
    channels = got["channels"].notes
    assert channels == {"label": make_item("渠道", "llm")}          # 表类型、主键不合规的都丢掉
    assert got["visits"].covered == frozenset({"llm"})
    assert catalog.validate_notes(visits) == []


async def test_model_prompt_carries_structure_and_no_data():
    src = _source(_scenic_tables())
    model = FakeModel({"visits": _VISITS_REPLY})
    await catalog.draft_with_model(model, src, ["visits"])
    prompt = model.calls[0]
    assert "表 visits" in prompt and "ticket_no" in prompt and "visit_time" in prompt


async def test_model_failure_only_affects_its_batch():
    tables = {f"t{i:02d}": _table(_col("id"), *[_col(f"c{j}") for j in range(60)]) for i in range(4)}
    src = _source(tables)
    model = FakeModel({name: {"label": f"表{name}"} for name in tables}, fail={"t00"})
    got = await catalog.draft_with_model(model, src, list(tables))
    assert isinstance(got["t00"], str) and "网关超时" in got["t00"]
    assert all(got[n].notes["label"]["value"] == f"表{n}" for n in ("t01", "t02", "t03"))
    assert len(model.calls) >= 2                                   # 按表分批，不是一次全发


async def test_model_draft_respects_system_notes_of_imported_tables():
    """导入表格：表说明已经写了粒度、列说明写了单位，模型起草的粒度和单位不进目录，免得重复或矛盾。"""
    src = _source({"日报": _table(_col("日期", "TEXT", comment="格式 YYYY-MM-DD"), _col("客流"), pk=(),
                                  comment="粒度：每个日期")}, origin="upload")
    model = FakeModel({"日报": {"label": "客流日报", "grain": "每天一行", "columns": [
        {"name": "日期", "meaning": "统计日期", "unit": "天", "measure": "attribute"},
        {"name": "客流", "meaning": "当天入园人次", "unit": "人次", "measure": "flow"}]}})
    got = (await catalog.draft_with_model(model, src, ["日报"]))["日报"].notes
    assert "grain" not in got and got["label"]["value"] == "客流日报"
    assert "meaning" not in got["columns"]["日期"] and "unit" not in got["columns"]["日期"]
    assert got["columns"]["日期"]["measure"]["value"] == "attribute"
    assert got["columns"]["客流"]["unit"]["value"] == "人次"          # 这一列没有系统说明，照常起草
    assert "粒度：每个日期" in model.calls[0]                          # 系统说明交给模型作背景


# ---------------------------------------------------------------- 起草并写入


async def _db_source(tables: dict, **kw) -> DataSource:
    for name, meta in tables.items():
        meta["qualified"] = name
    async with SessionLocal() as session:
        row = DataSource(name=f"cat_{uuid.uuid4().hex[:10]}", kind="sqlite", database=":memory:",
                         schema_cache={"tables": tables}, **kw)
        session.add(row)
        await session.commit()
        return row


async def test_draft_catalog_writes_and_is_idempotent():
    tables = _scenic_tables()
    tables["visits"]["foreign_keys"] = [{"columns": ["channel_id"], "to_table": "channels", "to_columns": ["id"]}]
    tables["visits"]["columns"].append(_col("channel_id"))
    tables["visits"]["columns"].append(_col("gate_id"))
    tables["gates"] = _table(_col("id"), fks=[])
    row = await _db_source(tables)
    model = FakeModel({"visits": _VISITS_REPLY})
    async with SessionLocal() as session:
        report = await catalog.draft_catalog(session, row, tables=["visits"], use_model=True, model=model,
                                             actor="王敏")
    result = report.tables[0]
    assert result.table == "visits" and result.error is None and result.version == 1
    # 表级 5 项（中文名、粒度、表类型、业务主键、业务日期）+ 列级 6 项 + 外键和命名推断各一条关系
    assert (result.added, result.updated, result.removed) == (13, 0, 0)
    assert report.model_used and report.model_error is None
    async with SessionLocal() as session:
        entry = await catalog.read_entry(session, row.id, "visits")
        assert entry.updated_by == "王敏"
        assert _rels(entry.notes) == {(("channel_id",), "channels", ("id",), "fk", "verified"),
                                      (("gate_id",), "gates", ("id",), "name", "proposed")}
        again = await catalog.draft_catalog(session, row, tables=["visits"], use_model=True, model=model, actor=None)
    assert (again.tables[0].added, again.tables[0].updated, again.tables[0].removed) == (0, 0, 0)
    assert again.tables[0].version == 1                             # 没有变化不升版本


async def test_redraft_keeps_confirmed_and_rejected_items():
    tables = _scenic_tables()
    tables["visits"]["columns"].append(_col("gate_id"))
    tables["gates"] = _table(_col("id"), fks=[])
    row = await _db_source(tables)
    model = FakeModel({"visits": _VISITS_REPLY})
    async with SessionLocal() as session:
        await catalog.draft_catalog(session, row, tables=["visits"], use_model=True, model=model, actor=None)
        entry = await catalog.read_entry(session, row.id, "visits")
        rid = entry.notes["relations"][0]["id"]
        entry = await catalog.review_entry(session, row.id, "visits", "grain", "confirm", if_version=entry.version,
                                           actor="李雷")
        entry = await catalog.review_entry(session, row.id, "visits", f"relations.{rid}", "reject",
                                           if_version=entry.version, actor="李雷")
        changed = FakeModel({"visits": {**_VISITS_REPLY, "grain": "每次入园一行", "label": "游客入园"}})
        report = await catalog.draft_catalog(session, row, tables=["visits"], use_model=True, model=changed,
                                             actor=None)
        after = await catalog.read_entry(session, row.id, "visits")
    assert after.notes["grain"]["value"] == "每张门票每次检票一行" and after.notes["grain"]["status"] == "confirmed"
    assert after.notes["relations"][0]["status"] == "rejected"      # 驳回的关系留着，下次起草不再提出
    assert after.notes["label"]["value"] == "游客入园"               # 同一来源、没确认过的项照常更新
    assert report.tables[0].updated == 1


async def test_draft_catalog_model_unavailable_still_writes_other_sources(monkeypatch):
    tables = _scenic_tables()
    tables["visits"]["comment"] = "入园记录"
    row = await _db_source(tables)

    async def unavailable(session):
        raise catalog.CatalogModelUnavailable("未配置模型接入，无法起草。请到「设置 → 模型接入」添加")

    monkeypatch.setattr(catalog, "resolve_draft_model", unavailable)
    async with SessionLocal() as session:
        report = await catalog.draft_catalog(session, row, tables=["visits"], use_model=True, actor=None)
        entry = await catalog.read_entry(session, row.id, "visits")
    assert not report.model_used and "模型接入" in report.model_error
    label = entry.notes["label"]
    assert label["updated_at"] and {k: v for k, v in label.items() if k != "updated_at"} == make_item("入园记录",
                                                                                                    "comment")


async def test_draft_catalog_model_failure_on_one_table(monkeypatch):
    row = await _db_source(_scenic_tables())
    model = FakeModel({"visits": _VISITS_REPLY, "channels": {"label": "渠道"}}, fail={"visits"})
    monkeypatch.setattr(catalog, "MODEL_BATCH_TABLES", 1)
    async with SessionLocal() as session:
        report = await catalog.draft_catalog(session, row, tables=["visits", "channels"], use_model=True,
                                             model=model, actor=None)
    by = {r.table: r for r in report.tables}
    assert by["visits"].model_error and "网关超时" in by["visits"].model_error
    assert by["channels"].model_error is None and by["channels"].added == 1


async def test_draft_catalog_defaults_to_most_used_tables(monkeypatch):
    tables = {f"t{i:02d}": _table(_col("id"), fks=[]) for i in range(25)}
    row = await _db_source(tables)

    async def usage(session, source):
        return {"t24": 9, "t03": 5, "t10": 5}

    monkeypatch.setattr(catalog, "table_usage", usage)
    async with SessionLocal() as session:
        report = await catalog.draft_catalog(session, row, use_model=False, actor=None)
    picked = [r.table for r in report.tables]
    assert len(picked) == catalog.DRAFT_DEFAULT_TABLES == 20
    assert picked[:3] == ["t24", "t03", "t10"]                      # 次数相同按表结构里的顺序
    assert picked[3:6] == ["t00", "t01", "t02"]


async def test_draft_catalog_unknown_table_is_reported():
    row = await _db_source(_scenic_tables())
    async with SessionLocal() as session:
        report = await catalog.draft_catalog(session, row, tables=["nope"], use_model=False, actor=None)
    assert report.tables[0].error and "nope" in report.tables[0].error


def test_draft_prompt_constants_are_model_only():
    """提示词常量的名字带 _SYSTEM / _PROMPT：文案检查按名字把它们排除在界面文案之外。"""
    assert catalog.CATALOG_DRAFT_SYSTEM and catalog.CATALOG_DRAFT_PROMPT
    json.dumps(catalog.CATALOG_DRAFT_PROMPT_SCHEMA, ensure_ascii=False)
