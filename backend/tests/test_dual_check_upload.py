"""上传表格建的数据源上的双盲复算：业务日期按导入时配方里记的逻辑类型认日期列（engine/dual_check_facts.upload_dates）。

上传表格导入时日期一律按文字保存，表结构里总是 TEXT，以前这类数据源的业务日期永远写不了。导入时配方里其实记着每一列
的逻辑类型（导入确认时界面上标的「日期」）：

- 配方里是日期的两列（列表块里类型为 DATE 的列、交叉表的日期轴）：审核人选了哪一边，业务日期就记哪一列；审批卡的预告
  和真写一字不差
- 配方里是文字的列不写；配方读不出来（简单导入没有配方、配方记录坏了）的照旧不写，原因都照实说是上传的表格按文字保存
- 只认数据源当前版本的配方：表结构出自同一个版本

数据源一律经真实的导入接口建（暂存 → 改配方 → 试运行 → 提交，或者简单导入），工作簿是现造的：表名、地区、金额都是
虚构的。盲算模型是 MockChatModel 剧本（tests/fixtures/dual_check_shop.Blind）。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import delete, select

from app.data.recipe_types import Recipe
from app.db.base import SessionLocal
from app.db.models import DataSource, Setting, SourceSnapshot, TableRecipe
from app.engine import spend
from app.engine.dual_check_facts import recipe_dates, upload_dates
from app.engine.runner import run_manager
from app.main import app
from tests.fixtures.dual_check_shop import ROWS, Blind
from tests.fixtures.sources import drop_source
from tests.fixtures.xlsx import save
from tests.test_dual_check_node import decide, events, graph, logs, notes_of, pending, start

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
API = "/api/datasources"
SHEET = "订单明细"
TABLE = "订单"
HEADER = ("订单号", "地区", "客户", "金额", "状态", "下单日期", "支付日期")
#: 同 dual_check_shop 的五笔单：退款行、下单日和支付日不在同一周的单，只是表名、列名、状态换成中文
STATUS = {"paid": "已支付", "refunded": "已退款"}
WEEK = "BETWEEN '2026-09-01' AND '2026-09-07'"
#: 口径说明要的写法：按支付日、排除退款（1、2、5 号单，330.0）
RIGHT = f"SELECT SUM(\"金额\") AS gmv FROM \"订单\" WHERE \"状态\" <> '已退款' AND \"支付日期\" {WEEK}"
#: 只错在时间列：按下单日（2、4、5 号单，310.0）
BY_ORDER_DATE = f"SELECT SUM(\"金额\") AS gmv FROM \"订单\" WHERE \"状态\" <> '已退款' AND \"下单日期\" {WEEK}"
DEFINITION = "已支付订单的金额合计：按支付日期归属到当周，不含退款"
#: 配方里不是日期、读不出配方时的原因：照旧说按文字保存
STORED_AS_TEXT = "来自上传的表格，表格里的日期一律按文字保存，无法确认存的是日期"
FLOW_RECIPE = json.loads((Path(__file__).parent / "fixtures" / "recipes" / "flow_recipe.json").read_text("utf-8"))


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(["verify", spend.SPEND_KEY])))
        await session.commit()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def workbook() -> bytes:
    """订单明细：第 1 行表头，日期写成 Excel 的日期格（不是文字）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(list(HEADER))
    for oid, region, customer, amount, status, ordered, paid, *_ in ROWS:
        ws.append([oid, region, customer, amount, STATUS[status],
                   dt.date.fromisoformat(ordered), dt.date.fromisoformat(paid)])
    return save(wb)


def list_recipe(*, ordered: str = "DATE", paid: str = "DATE") -> dict:
    """列表配方：两个日期列各按给定的类型导入（DATE 是界面上的「日期」，TEXT 是「文字」）。"""
    types = {"订单号": "INTEGER", "地区": "TEXT", "客户": "INTEGER", "金额": "REAL", "状态": "TEXT",
             "下单日期": ordered, "支付日期": paid}
    return {"recipe_format": "agentlab-recipe/2",
            "sheets": [{"id": "s1", "match": {"name": SHEET}, "blocks": [{
                "id": "orders", "layout": "list", "table": TABLE,
                "columns": [{"header": h, "name": h, "type": types[h]} for h in HEADER]}]}],
            "tables": [{"name": TABLE, "grain": ["订单号"]}]}


async def commit_recipe(client, staging: str, recipe: dict) -> dict:
    """暂存区换成给定的配方 → 试运行 → 勾全部确认项提交。返回提交的响应。"""
    r = await client.put(f"{API}/imports/{staging}/recipe", json={"recipe": recipe})
    assert r.status_code == 200 and r.json()["recipe_problems"] == [], r.text
    r = await client.post(f"{API}/imports/{staging}/trial", json={})
    assert r.status_code == 200, r.text
    trial = r.json()["trial"]
    assert trial["status"] in ("passed", "needs_decision"), trial["problems"]
    r = await client.post(f"{API}/imports/{staging}/commit", json={
        "trial_id": trial["trial_id"], "confirmations": [i["id"] for i in trial.get("confirm_items") or []]})
    assert r.status_code == 201, r.text
    return r.json()


async def recipe_import(client, recipe: dict) -> tuple[str, str]:
    """按配方导入：暂存 → 换成给定的配方 → 试运行 → 提交。返回 (数据源名, id)。"""
    name = f"dc_upload_{uuid.uuid4().hex[:8]}"
    r = await client.post(f"{API}/imports/stage", files={"file": (f"{SHEET}_2026-09.xlsx", workbook(), XLSX)},
                          data={"name": name})
    assert r.status_code == 201, r.text
    out = await commit_recipe(client, r.json()["id"], recipe)
    return name, out["source"]["id"]


async def simple_import(client) -> tuple[str, str]:
    """期 1 的简单导入：按表头行导入，没有配方。"""
    name = f"dc_simple_{uuid.uuid4().hex[:8]}"
    r = await client.post(f"{API}/upload", files={"file": (f"{SHEET}.xlsx", workbook(), XLSX)}, data={"name": name})
    assert r.status_code == 201, r.text
    async with SessionLocal() as session:
        source_id = (await session.execute(select(DataSource.id).where(DataSource.name == name))).scalar_one()
    return name, source_id


async def dates_of(source_id: str) -> dict:
    async with SessionLocal() as session:
        return await upload_dates(session, await session.get(DataSource, source_id))


async def break_recipe(source_id: str) -> None:
    """把数据源当前版本的配方记录改坏（造「配方读不出来」）。当前快照记的就是这一版。"""
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        assert (await session.get(SourceSnapshot, row.current_snapshot_id)).recipe_id == row.current_recipe_id
        record = await session.get(TableRecipe, row.current_recipe_id)
        record.recipe = {"recipe_format": "agentlab-recipe/0"}
        await session.commit()


@pytest.fixture
async def dated(client):
    """两个日期列在配方里都是「日期」的上传源。"""
    name, source_id = await recipe_import(client, list_recipe())
    yield name, source_id
    await drop_source(source_id)


@pytest.fixture
async def paid_as_text(client):
    """支付日期在配方里是「文字」、下单日期是「日期」的上传源。"""
    name, source_id = await recipe_import(client, list_recipe(paid="TEXT"))
    yield name, source_id
    await drop_source(source_id)


# --------------------------------------------------------------------------
# 配方里哪些列是日期
# --------------------------------------------------------------------------


def test_the_date_columns_of_a_list_recipe_are_the_ones_typed_as_dates():
    assert recipe_dates(Recipe.model_validate(list_recipe())) == {TABLE: ["下单日期", "支付日期"]}
    assert recipe_dates(Recipe.model_validate(list_recipe(paid="TEXT"))) == {TABLE: ["下单日期"]}
    assert recipe_dates(Recipe.model_validate(list_recipe(ordered="TEXT", paid="TEXT"))) == {}


def test_the_axis_of_a_crosstab_is_a_date_column_of_every_table_it_writes():
    """交叉表的日期轴（类型恒为日期）写进各分段的表：宽表、长表、表内合计另存的表都有这一列。"""
    assert recipe_dates(Recipe.model_validate(FLOW_RECIPE)) == {
        "日客流": ["日期"], "时段客流": ["日期"], "时段客流_表内合计": ["日期"]}


def test_a_list_total_kept_as_a_table_has_no_date_column():
    """列表的合计行另存成表时只有合计项和数字列：日期列不在里面。"""
    recipe = list_recipe()
    recipe["sheets"][0]["blocks"][0]["rows"] = {"total_row": {"label_column": "地区", "pick": "合计",
                                                              "keep_as": "订单合计"}}
    recipe["tables"].append({"name": "订单合计", "kind": "reported_total"})
    assert recipe_dates(Recipe.model_validate(recipe)) == {TABLE: ["下单日期", "支付日期"]}


# --------------------------------------------------------------------------
# 读当前版本的配方
# --------------------------------------------------------------------------


async def test_an_upload_source_reads_the_date_columns_from_its_current_recipe(client, dated, paid_as_text):
    """真实导入流程建的源：表结构里两列都是 TEXT，配方里是日期的那几列读得出来。"""
    async with SessionLocal() as session:
        schema = (await session.get(DataSource, dated[1])).schema_cache
    types = {c["name"]: c["type"] for c in schema["tables"][TABLE]["columns"]}
    assert (types["下单日期"], types["支付日期"]) == ("TEXT", "TEXT"), "背景：导入时日期一律按文字保存"
    assert await dates_of(dated[1]) == {TABLE: ["下单日期", "支付日期"]}
    assert await dates_of(paid_as_text[1]) == {TABLE: ["下单日期"]}


async def current_snapshot(source_id: str) -> str:
    async with SessionLocal() as session:
        return (await session.get(DataSource, source_id)).current_snapshot_id


async def test_the_dates_follow_the_current_version_of_the_source(client, dated):
    """只认数据源当前版本的配方（表结构出自同一个版本）：修改配方把支付日期改成「文字」、提交之后只剩下单日期；回滚到
    旧版本，两列又都是日期。"""
    _, source_id = dated
    first = await current_snapshot(source_id)
    r = await client.post(f"{API}/{source_id}/redraft", json={})
    assert r.status_code == 201, r.text
    await commit_recipe(client, r.json()["id"], list_recipe(paid="TEXT"))
    second = await current_snapshot(source_id)
    assert second != first
    assert await dates_of(source_id) == {TABLE: ["下单日期"]}
    r = await client.post(f"{API}/{source_id}/snapshots/{first}/activate",
                          json={"confirm": True, "expected_current_snapshot_id": second})
    assert r.status_code == 200, r.text
    assert await current_snapshot(source_id) == first
    assert await dates_of(source_id) == {TABLE: ["下单日期", "支付日期"]}


async def test_without_a_readable_recipe_no_column_is_taken_as_a_date(client, dated, tmp_path):
    """读不出配方的都不认：简单导入没有配方，配方记录坏了，手工登记的库，不存在的数据源。"""
    name, source_id = await simple_import(client)
    try:
        async with SessionLocal() as session:
            row = await session.get(DataSource, source_id)
        assert row.origin == "upload" and row.current_recipe_id is None
        assert await dates_of(source_id) == {}
    finally:
        await drop_source(source_id)
    await break_recipe(dated[1])
    assert await dates_of(dated[1]) == {}
    async with SessionLocal() as session:
        manual = DataSource(name=f"dc_manual_{uuid.uuid4().hex[:8]}", kind="sqlite", database=str(tmp_path / "x.db"))
        assert await upload_dates(session, manual) == {}
        assert await upload_dates(session, None) == {}


async def test_the_node_hands_the_import_dates_over_with_the_source(client, dated):
    """节点认数据源来历的那一步（_source_info）带上 upload_dates，候选据此交给 derive。"""
    from app.engine.nodes.dual_check import _source_info, _Sources

    info = await _source_info(_Sources(), dated[0])
    assert info["uploaded"] is True and info["upload_dates"] == {TABLE: ["下单日期", "支付日期"]}
    assert (await _source_info(_Sources(), "dc_no_such_source"))["upload_dates"] == {}


# --------------------------------------------------------------------------
# 双盲复算：审批卡的预告、裁决后的真写
# --------------------------------------------------------------------------


def upload_graph(name: str, sql: str) -> dict:
    return graph(name, sql, dual={"definitions": {"gmv": DEFINITION}})


async def test_dates_the_import_took_as_dates_are_written_as_the_business_date(monkeypatch, client, dated):
    """两列在配方里都是「日期」：审批卡预告选哪边记哪一列，审核人选盲算，业务日期记「支付日期」，人工确认、备注写明出处。"""
    name, source_id = dated
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(upload_graph(name, BY_ORDER_DATE))
    assert run.status == "interrupted", run.error
    [item] = (await pending(run.id)).payload["summary"]
    assert len(item["diff"]) == 1 and "下单日期" in item["diff"][0] and "支付日期" in item["diff"][0]
    where = {"source": name, "table": TABLE, "item": "business_date"}
    assert item["catalog"] == {"a": {"write": True, **where, "value": {"column": "下单日期"}},
                               "b": {"write": True, **where, "value": {"column": "支付日期"}}}
    done = await decide(client, run.id, {"gmv": "b"})
    assert done.status == "succeeded", done.error
    [wrote] = logs(await events(run.id), "dual_check_catalog")
    assert wrote["written"] == [{**where, "value": {"column": "支付日期"}}] and wrote["skipped"] == []
    entry = await notes_of(source_id, TABLE)
    stored = entry.notes["business_date"]
    assert stored["value"] == {"column": "支付日期"}
    assert (stored["source"], stored["status"]) == ("human", "confirmed") and run.id in stored["note"]


async def test_a_column_the_recipe_kept_as_text_is_not_written(monkeypatch, client, paid_as_text):
    """支付日期在配方里是「文字」：选哪边都不记，原因照旧说按文字保存——被采纳的是它就说它，被采纳的下单日期是日期的，说
    未被采纳的支付日期、另说清为什么也要看它。预告和裁决后跳过的原因一字不差，目录里什么都没写。"""
    name, source_id = paid_as_text
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(upload_graph(name, BY_ORDER_DATE))
    assert run.status == "interrupted", run.error
    said = (await pending(run.id)).payload["summary"][0]["catalog"]
    column = f"「{TABLE}.支付日期」{STORED_AS_TEXT}"
    assert said["b"] == {"write": False, "reason": f"指标「销售额」被采纳的盲算按时间筛的列{column}，不写入数据目录"}
    assert said["a"] == {"write": False, "reason": f"指标「销售额」未被采纳的盲算按时间筛的列{column}："
                                                   "两边都是日期列时，审核人选的才是业务日期按哪一列，不写入数据目录"}
    done = await decide(client, run.id, {"gmv": "b"})
    assert done.status == "succeeded", done.error
    [wrote] = logs(await events(run.id), "dual_check_catalog")
    assert wrote["written"] == [] and [s["reason"] for s in wrote["skipped"]] == [said["b"]["reason"]]
    assert await notes_of(source_id, TABLE) is None


async def test_without_a_readable_recipe_nothing_is_written(monkeypatch, client, dated):
    """配方记录坏了、读不出来：两列都照旧说按文字保存，不写。"""
    name, source_id = dated
    await break_recipe(source_id)
    Blind(monkeypatch, name, {"销售额": (RIGHT, "gmv")})
    run = await start(upload_graph(name, BY_ORDER_DATE))
    assert run.status == "interrupted", run.error
    said = (await pending(run.id)).payload["summary"][0]["catalog"]
    for side, label, column in (("a", "原流程", "下单日期"), ("b", "盲算", "支付日期")):
        assert said[side] == {"write": False, "reason": f"指标「销售额」被采纳的{label}按时间筛的列「{TABLE}.{column}」"
                                                         f"{STORED_AS_TEXT}，不写入数据目录"}
    done = await decide(client, run.id, {"gmv": "a"})
    assert done.status == "succeeded", done.error
    [wrote] = logs(await events(run.id), "dual_check_catalog")
    assert wrote["written"] == [] and [s["reason"] for s in wrote["skipped"]] == [said["a"]["reason"]]
    assert await notes_of(source_id, TABLE) is None
