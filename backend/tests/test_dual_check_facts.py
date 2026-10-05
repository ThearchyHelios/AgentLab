"""双盲复算的选择写进数据目录（engine/dual_check_facts.py）。

- 只差一个方面、又对得上目录事实时才写：时间列 → 业务日期，一边多一个只涉及一张表的筛选条件 → 有效记录条件，
  聚合只差去不去重 → 去重规则；按被采纳那一边的写法记
- 差了不止一处、有兜底说明、两条 SQL 规范化后一致、差在别的方面、被采纳的一边没有可记的写法：不写，照实说原因
- 写的是人工填写、已确认，备注写明运行、节点、指标；同样的说法已确认过就不再写（不升版本），确认过的不同说法不覆盖，
  推断的不同说法照改

差异条目一律由 sql_semantics.explain_sql_diff 真算出来，和节点交给审核人看的是同一份。表名、数字都是虚构的。
"""
from __future__ import annotations

import pytest

from app.data import catalog
from app.data.sql_semantics import explain_sql_diff
from app.db.base import SessionLocal
from app.engine.dual_check_facts import derive, record_choices
from tests.fixtures.dual_check_shop import (
    BOTH_WRONG,
    BUYERS,
    BUYERS_DISTINCT,
    BY_ORDER_DATE,
    RIGHT,
    WITH_REFUNDS,
    add_source,
    build,
)
from tests.fixtures.sources import drop_source

SCHEMA = {"tables": {"orders": {"name": "orders", "columns": [
    {"name": "id", "type": "INTEGER"}, {"name": "region", "type": "TEXT"}, {"name": "customer_id", "type": "INTEGER"},
    {"name": "amount", "type": "REAL"}, {"name": "status", "type": "TEXT"}, {"name": "order_date", "type": "TEXT"},
    {"name": "paid_date", "type": "TEXT"}], "primary_key": ["id"]}}}


def candidate(original: str, blind: str, choice: str, *, metric: str = "销售额", source: str = "shop") -> dict:
    diff = explain_sql_diff(original, blind, "sqlite", schema=SCHEMA, labels=("原流程", "盲算")).as_dict()
    return {"metric": metric, "choice": choice, "source": source, "dialect": "sqlite",
            "sql": {"a": original, "b": blind}, "diff": diff}


# --------------------------------------------------------------------------
# 差异 → 目录事实
# --------------------------------------------------------------------------


@pytest.mark.parametrize("choice, column", [("b", "paid_date"), ("a", "order_date")])
def test_a_different_time_column_becomes_the_business_date(choice, column):
    fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, choice), SCHEMA)
    assert why is None
    assert (fact.table, fact.item, fact.value, fact.choice) == ("orders", "business_date", {"column": column}, choice)


def test_an_extra_filter_on_the_adopted_side_becomes_the_valid_filter():
    fact, why = derive(candidate(WITH_REFUNDS, RIGHT, "b"), SCHEMA)
    assert why is None and (fact.item, fact.value) == ("valid_filter", "status <> 'refunded'")
    # 原流程多出这个条件、审核人采纳原流程：同样记这个条件
    fact, why = derive(candidate(RIGHT, WITH_REFUNDS, "a"), SCHEMA)
    assert why is None and (fact.item, fact.value) == ("valid_filter", "status <> 'refunded'")


def test_the_adopted_side_without_the_extra_filter_has_nothing_to_record():
    fact, why = derive(candidate(RIGHT, WITH_REFUNDS, "b"), SCHEMA)
    assert fact is None and "被采纳的盲算没有这个条件" in why and "销售额" in why


def test_table_names_are_dropped_from_the_recorded_filter():
    qualified = RIGHT.replace("FROM orders WHERE status", "FROM orders o WHERE o.status") \
        .replace("paid_date", "o.paid_date")
    fact, why = derive(candidate(WITH_REFUNDS, qualified, "b"), SCHEMA)
    assert why is None and fact.value == "status <> 'refunded'"


@pytest.mark.parametrize("choice, text", [("b", "对 customer_id 计数时去重"), ("a", "对 customer_id 计数时不去重")])
def test_counting_with_or_without_distinct_becomes_the_dedup_rule(choice, text):
    fact, why = derive(candidate(BUYERS, BUYERS_DISTINCT, choice, metric="付款客户数"), SCHEMA)
    assert why is None and (fact.table, fact.item, fact.value) == ("orders", "dedup", text)


def test_more_than_one_difference_is_not_recorded():
    fact, why = derive(candidate(BOTH_WRONG, RIGHT, "b"), SCHEMA)
    assert fact is None and "不止差一个方面" in why


def test_equivalent_sql_or_fallback_or_other_aspects_are_not_recorded():
    same = candidate(RIGHT, RIGHT.replace("SUM(amount) AS gmv", "SUM(amount) AS total"), "b")
    assert same["diff"]["equivalent"] is True
    assert "规范化后一致" in derive(same, SCHEMA)[1]

    fallback = candidate(BY_ORDER_DATE, RIGHT, "b")
    fallback["diff"]["fallback"] = True
    assert "未能归纳" in derive(fallback, SCHEMA)[1]

    joined = candidate(RIGHT, RIGHT.replace("SUM(amount)", "MAX(amount)"), "b")
    assert [i["aspect"] for i in joined["diff"]["items"]] == ["aggregate"]
    assert "不只是去不去重" in derive(joined, SCHEMA)[1]

    ranged = candidate(RIGHT, RIGHT.replace("'2026-09-07'", "'2026-09-06'"), "b")
    assert [i["aspect"] for i in ranged["diff"]["items"]] == ["time_range"]
    assert "时间范围" in derive(ranged, SCHEMA)[1]

    assert "没有采用任何一边" in derive({**candidate(BY_ORDER_DATE, RIGHT, "b"), "choice": "neither"}, SCHEMA)[1]


def test_a_table_missing_from_the_schema_is_not_recorded():
    fact, why = derive(candidate(BY_ORDER_DATE, RIGHT, "b"), {"tables": {"refunds": {"columns": []}}})
    assert fact is None and "不在数据源的表结构里" in why


# --------------------------------------------------------------------------
# 写进目录
# --------------------------------------------------------------------------


@pytest.fixture
async def shop(tmp_path):
    name, source_id = await add_source(build(tmp_path / "shop.db"))
    yield name, source_id
    await drop_source(source_id)


async def entry_of(source_id: str):
    async with SessionLocal() as session:
        return await catalog.read_entry(session, source_id, "orders")


async def seed(source_id: str, notes: dict) -> None:
    async with SessionLocal() as session:
        await catalog.write_entry(session, source_id, "orders", notes, if_version=0, actor="数据组")


async def test_a_choice_is_written_as_a_confirmed_human_item_with_its_origin(shop):
    name, source_id = shop
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source=name)], run_id="run-42",
                               node_title="复核销售额", actor="张三")
    assert out == {"written": [{"source": name, "table": "orders", "item": "business_date"}], "skipped": []}
    entry = await entry_of(source_id)
    item = entry.notes["business_date"]
    assert item["value"] == {"column": "paid_date"} and (item["source"], item["status"]) == ("human", "confirmed")
    assert "run-42" in item["note"] and "「复核销售额」" in item["note"] and "「销售额」" in item["note"]
    assert "盲算" in item["note"] and entry.version == 1 and entry.updated_by == "张三"


async def test_writing_the_same_choice_again_changes_nothing(shop):
    name, source_id = shop
    choice = [candidate(BY_ORDER_DATE, RIGHT, "b", source=name)]
    await record_choices(choice, run_id="run-1", node_title="复核", actor=None)
    again = await record_choices(choice, run_id="run-2", node_title="复核", actor=None)
    assert again["written"] == [] and "无需重复写入" in again["skipped"][0]["reason"]
    entry = await entry_of(source_id)
    assert entry.version == 1 and "run-1" in entry.notes["business_date"]["note"]


async def test_a_confirmed_different_item_is_kept(shop):
    name, source_id = shop
    await seed(source_id, {"business_date": catalog.make_item({"column": "order_date", "rule": "按下单日"}, "human",
                                                              "confirmed")})
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == []
    reason = out["skipped"][0]["reason"]
    assert "已有人工确认过的说法「order_date」" in reason and "paid_date" in reason and "未覆盖" in reason
    entry = await entry_of(source_id)
    assert entry.version == 1 and entry.notes["business_date"]["value"]["column"] == "order_date"


async def test_an_inferred_different_item_is_replaced(shop):
    name, source_id = shop
    await seed(source_id, {"valid_filter": catalog.make_item("status = 'paid'", "llm")})
    out = await record_choices([candidate(WITH_REFUNDS, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "valid_filter"}]
    item = (await entry_of(source_id)).notes["valid_filter"]
    assert item["value"] == "status <> 'refunded'" and (item["source"], item["status"]) == ("human", "confirmed")


async def test_the_same_column_inferred_is_confirmed_and_keeps_its_rule(shop):
    """目录里推断的业务日期正是这一列（还写了规则、时区）：确认它，规则、时区原样留着。"""
    name, source_id = shop
    await seed(source_id, {"business_date": catalog.make_item(
        {"column": "paid_date", "rule": "按付款成功时间", "timezone": "Asia/Shanghai"}, "profile")})
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == [{"source": name, "table": "orders", "item": "business_date"}]
    item = (await entry_of(source_id)).notes["business_date"]
    assert item["value"] == {"column": "paid_date", "rule": "按付款成功时间", "timezone": "Asia/Shanghai"}
    assert (item["source"], item["status"]) == ("human", "confirmed")


async def test_a_missing_source_or_an_unrecordable_choice_is_reported(shop):
    name, _ = shop
    out = await record_choices([candidate(BY_ORDER_DATE, RIGHT, "b", source="no_such_source"),
                                candidate(BOTH_WRONG, RIGHT, "b", source=name)], run_id="run-1",
                               node_title="复核", actor=None)
    assert out["written"] == []
    assert "数据源「no_such_source」已不存在" in out["skipped"][0]["reason"]
    assert "不止差一个方面" in out["skipped"][1]["reason"]
