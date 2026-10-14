"""助手按需查看表结构：查看服务的表这一类（look 文档 §2.4 D2、D3、D6，§2.5.3–§2.5.7）。

以前每一轮开始前服务端按库的大小挑表、塞进 system；现在 system 里每个库一行概览，模型要看表时输出一行 look，
查看服务一次给齐。这组守的是（合成景区库：入园记录和渠道之间要经关联表 channel_visits）：

- 概览：每个库一行，不列表名、不列字段；现有工作流用到的表只列名字
- 点名直取，不调挑选模型；只给需求时候选不超过 12 张全给，否则按需求挑（关思考、额度小）
- 补两跳以内的中间表，合计不超过 16 张；连接条件、连接路径、数据目录照抄；推断的标出来，驳回的关系不用
- 挑选失败（报错、超时、挑出的表都不存在、回复认不出）交回表目录（status=index），挑出来是空的照实说
- 没找到的点名、认不出的库名写进结果和记录；限定的数据源之外的不看
- 遮罩列只给列名和类型（D6），db_schema__ 和 /schema 接口是同一份说法
- 字数上限之外的表只给字段名；同一批表这一轮看过就不再重排
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.data import catalog, schema_view
from app.data.catalog import INFERRED_MARK, make_item
from app.data.introspect import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource
from app.look.base import CONTEXT_TABLE_LIMIT, LookBudget
from app.look.pick import Picker
from app.look.service import LookService, parse_targets
from app.look.tables import TableKind
from app.main import app
from tests.fixtures.catalog import scenic
from tests.fixtures.sources import drop_source


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _add_source(database: str, *, prefix: str = "look", options: dict | None = None) -> DataSource:
    async with SessionLocal() as session:
        row = DataSource(name=f"{prefix}_{uuid.uuid4().hex[:8]}", kind="sqlite", database=database,
                         description="景区业务库", options=options or {})
        session.add(row)
        await session.commit()
        row.schema_cache = await introspect(row)
        await session.commit()
        await session.refresh(row)
        session.expunge(row)
        return row


@pytest.fixture
async def scenic_source(scenic_db):
    """接上合成景区库（50 张表 + 1 个视图），用完删掉。"""
    row = await _add_source(scenic_db)
    yield row
    await drop_source(row.id)


@pytest.fixture
async def small_source(tmp_path):
    """两张表的小库：orders.customer_id 只能靠命名推断连到 customers。"""
    path = tmp_path / "small.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, amount REAL NOT NULL,"
        " ordered_at TEXT NOT NULL);")
    db.commit()
    db.close()
    row = await _add_source(str(path), prefix="look_small")
    yield row
    await drop_source(row.id)


@pytest.fixture
async def masked_source(tmp_path):
    """设了遮罩列 phone 的库，phone 上记了中文名和码值。"""
    path = tmp_path / "masked.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE members (id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, level INTEGER);")
    db.commit()
    db.close()
    row = await _add_source(str(path), prefix="look_mask", options={"mask_columns": "phone"})
    async with SessionLocal() as session:
        await catalog.write_entry(session, row.id, "members", {
            "label": make_item("会员", "human"),
            "columns": {"phone": {"label": make_item("手机号", "human"), "codes": make_item({"138": "移动号段"}, "human"),
                                  "meaning": make_item("会员登记的手机号，例如 13800000000", "human")},
                        "level": {"codes": make_item({"1": "普通", "2": "金卡"}, "human")}}},
            if_version=0, actor="王敏")
    yield row
    await drop_source(row.id)


class FakePicker:
    """挑选用的假模型：结构化输出返回自己，ainvoke 记下收到的提示词，按设定回复、报错或拖延。"""

    def __init__(self, reply=None, *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.reply, self.error, self.delay = reply, error, delay
        self.calls: list[str] = []

    def with_structured_output(self, schema, **_):
        assert schema["title"]
        return self

    async def ainvoke(self, messages):
        self.calls.append("\n".join(text for _role, text in messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.reply


def _use_picker(monkeypatch, fake: FakePicker) -> list:
    """挑选模型换成假的，返回每次拿模型时的规格（核对思考关、额度小）。"""
    import app.look.pick as pick

    specs: list = []

    async def _model(_session, spec):
        specs.append(spec)
        return fake, "fake-pick"

    monkeypatch.setattr(pick, "get_chat_model", _model)
    return specs


async def _service(sources, *, graph=None, picker: Picker | None = None) -> LookService:
    from app.providers.factory import ModelSpec

    return LookService([await TableKind.load(sources, graph=graph,
                                             picker=picker or Picker(ModelSpec(provider=None, model=None)))])


#: 需求和假模型挑出的表：入园记录、渠道。两者之间没有直接关联，要经关联表 channel_visits
NEED = "各渠道上月的入园人次"
PICKED = {"items": [{"name": "visits"}, {"name": "channels"}]}
#: 三张表各自独有的字段（表详情的写法：列名两个空格类型）
SELECTED_COLUMNS = ("visitor_count  INTEGER", "visit_time  TEXT", "ticket_no  TEXT", "channel_type  TEXT",
                    "channel_code  TEXT", "attributed_share  REAL")
#: 没挑中的表独有的字段：不该出现在结果里
OTHER_COLUMNS = ("store_code", "sku", "on_hand", "refund_amount")
JOINS = ("channel_visits.visit_id = visits.id", "channel_visits.channel_id = channels.id")


# ---------------------------------------------------------------- 概览


async def test_overview_is_one_line_per_source_without_tables(scenic_source, small_source):
    graph = {"nodes": [{"id": "q", "type": "tool", "data": {"label": "查门店", "config": {
        "tool": f"db_query__{scenic_source.name}", "args": {"sql": "SELECT s.name FROM stores s"}}}}], "edges": []}
    looks = await _service([scenic_source, small_source], graph=graph)
    text = await looks.overview()
    total = scenic.TABLE_COUNT + len(scenic.VIEWS)
    assert f"- {scenic_source.name}（sqlite，只读）：景区业务库｜共 {total} 张表，还没有数据目录" in text
    assert f"- {small_source.name}（sqlite，只读）：景区业务库｜共 2 张表，还没有数据目录" in text
    # 不列表名、不列字段；现有工作流用到的表只列名字
    assert "channel_visits" not in text and "visitor_count" not in text
    assert f"现有工作流用到的表：{scenic_source.name} 的 stores" in text


# ---------------------------------------------------------------- 点名、挑选


async def test_named_look_brings_in_the_bridge_without_the_picker(monkeypatch, scenic_source):
    fake = FakePicker(error=AssertionError("点名不该调挑选模型"))
    _use_picker(monkeypatch, fake)
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": NEED, "targets": [
        {"kind": "table", "name": "visits"}, {"kind": "table", "name": "CHANNELS"}]}, LookBudget())

    assert not fake.calls
    assert result.text.startswith("查看结果（第 1 次，这一轮最多 3 次）\n需求：" + NEED)
    for col in SELECTED_COLUMNS:
        assert col in result.text, col
    for col in OTHER_COLUMNS:
        assert col not in result.text, col
    for cond in JOINS:
        assert cond in result.text, cond
    assert "表 channel_visits（为连接补上）" in result.text
    record = result.record
    assert record["op"] == "look" and record["id"] == "L1" and record["by"] == "model" and record["status"] == "ok"
    assert isinstance(record["elapsed_ms"], int)
    (block,) = record["objects"]
    assert block["kind"] == "table" and block["source"] == scenic_source.name
    assert block["source_id"] == scenic_source.id
    assert (block["named"], block["picked"], block["joined"], block["missing"]) == (
        ["visits", "channels"], [], ["channel_visits"], [])
    assert block["total"] == scenic.TABLE_COUNT + len(scenic.VIEWS) and block["catalog_tables"] == 0


async def test_need_only_look_picks_with_a_small_fast_model(monkeypatch, scenic_source):
    fake = FakePicker(PICKED)
    specs = _use_picker(monkeypatch, fake)
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": NEED}, LookBudget())

    assert len(fake.calls) == 1
    # 挑选的输入是需求加紧凑的表目录（每张表一行），不是全部字段
    assert NEED in fake.calls[0] and "channel_visits" in fake.calls[0] and "attributed_share" not in fake.calls[0]
    (spec,) = specs
    assert spec.thinking == "off" and spec.max_tokens <= 2048
    (block,) = result.record["objects"]
    assert (block["named"], block["picked"], block["joined"]) == ([], ["visits", "channels"], ["channel_visits"])
    assert f"按需求从 {scenic.TABLE_COUNT + len(scenic.VIEWS)} 张表中挑出 2 张，另为连接补上 1 张" in result.text


async def test_a_small_pool_is_given_whole_without_picking(monkeypatch, small_source):
    fake = FakePicker(error=AssertionError("候选不超过 12 张不该挑"))
    _use_picker(monkeypatch, fake)
    looks = await _service([small_source])
    result = await looks.look({"op": "look", "need": "订单总额"}, LookBudget())
    assert not fake.calls
    assert "候选只有 2 张表，全部给出" in result.text
    assert "表 customers" in result.text and "表 orders" in result.text
    assert f"orders.customer_id = customers.id（多对一）{INFERRED_MARK}" in result.text
    assert result.record["objects"][0]["picked"] == ["customers", "orders"]


@pytest.mark.parametrize("fake", [
    FakePicker(error=RuntimeError("网关超时")),
    FakePicker(PICKED, delay=5),
    FakePicker({"items": [{"name": "no_such_table"}, {"name": "也没有这张"}]}),
    FakePicker("我觉得都需要"),
], ids=["error", "timeout", "unknown", "garbage"])
async def test_pick_failures_hand_back_the_index(monkeypatch, scenic_source, fake):
    import app.look.pick as pick

    monkeypatch.setattr(pick, "PICK_TIMEOUT_S", 0.2)
    _use_picker(monkeypatch, fake)
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": NEED}, LookBudget())

    record = result.record
    assert record["status"] == "index" and record["reason"] and record["objects"] == []
    # 交回的是表目录（每张表一行），请模型点名再看；不给字段
    assert "没能按需求挑出表" in result.text and record["reason"] in result.text
    assert "channel_visits" in result.text and "attributed_share" not in result.text
    assert "点名" in result.text


async def test_an_empty_pick_is_reported_as_such(monkeypatch, scenic_source):
    """挑选模型照提示说「需求和这份目录无关」返回空：照实说没挑出来，不当成失败、不塞表。"""
    _use_picker(monkeypatch, FakePicker({"items": []}))
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": "你好"}, LookBudget())
    assert result.record["status"] == "index"
    assert result.record["reason"] == "按这个需求没有挑出相关的表"
    assert "按这个需求没有挑出相关的表" in result.text and "attributed_share" not in result.text


async def test_the_demo_model_cannot_pick(monkeypatch, scenic_source):
    """没配接入时挑选模型是演示模型：照实说挑不了，交回目录。"""
    import app.look.pick as pick
    from app.providers.mock_model import MockChatModel

    async def _demo(_session, _spec):
        return MockChatModel(model_name="mock-fast"), "mock-fast"

    monkeypatch.setattr(pick, "get_chat_model", _demo)
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": NEED}, LookBudget())
    assert result.record["status"] == "index" and "演示模型" in result.record["reason"]


# ---------------------------------------------------------------- 没找到、限定范围、多个库


async def test_missing_tables_and_sources_are_named(scenic_source):
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": "退款明细", "targets": [
        {"kind": "table", "name": "refunds"}, {"kind": "table", "name": "refund_detail"},
        {"kind": "table", "source": "没有这个库", "name": "refunds"}]}, LookBudget())
    assert "表 refunds" in result.text
    assert f"没找到：{scenic_source.name} 的 refund_detail" in result.text and "别拿名字相近的表顶替" in result.text
    assert "没找到数据源「没有这个库」" in result.text
    blocks = {b["source"]: b for b in result.record["objects"]}
    assert blocks[scenic_source.name]["named"] == ["refunds"]
    assert blocks[scenic_source.name]["missing"] == ["refund_detail"]
    assert blocks["没有这个库"]["source_id"] is None and blocks["没有这个库"]["missing"] == ["refunds"]


async def test_several_sources_and_the_sources_field(monkeypatch, scenic_db, tmp_path):
    a = await _add_source(scenic_db)
    b = await _add_source(str(scenic.build(tmp_path / "b.db")))
    try:
        fake = FakePicker({"items": [{"source": a.name, "name": "visits"}, {"source": b.name, "name": "orders"},
                                     f"{b.name}.payments", {"source": "没有这个库", "name": "refunds"}]})
        _use_picker(monkeypatch, fake)
        looks = await _service([a, b])
        result = await looks.look({"op": "look", "need": "订单和入园"}, LookBudget())
        assert f"数据源「{a.name}」" in fake.calls[0] and f"数据源「{b.name}」" in fake.calls[0]
        blocks = {o["source"]: o for o in result.record["objects"]}
        assert blocks[a.name]["picked"] == ["visits"]
        assert blocks[b.name]["picked"] == ["orders", "payments"]

        # sources 限定只在 b 里挑：挑选的目录里只有 b
        fake.calls.clear()
        fake.reply = {"items": [{"name": "refunds"}]}
        result = await looks.look({"op": "look", "need": "退款", "sources": [b.name]}, LookBudget())
        assert f"数据源「{a.name}」" not in fake.calls[0]
        assert [o["source"] for o in result.record["objects"]] == [b.name]
    finally:
        await drop_source(a.id)
        await drop_source(b.id)


async def test_tables_outside_the_users_scope_are_not_found(scenic_db, small_source):
    """用户限定了数据源：查看服务只认这一轮的源，别的库里的表按没找到回。"""
    looks = await _service([small_source])
    result = await looks.look({"op": "look", "need": "入园", "targets": [
        {"kind": "table", "name": "visits"}]}, LookBudget())
    assert "没找到" in result.text and "visitor_count" not in result.text


# ---------------------------------------------------------------- 连接条件、目录


async def test_inferred_relations_are_marked_and_catalog_is_attached(scenic_source):
    """visits.gate_id → gates 没有外键约束，是按列名推断的，连接条件要标出来；目录里推断的项同样要标。"""
    async with SessionLocal() as session:
        await catalog.write_entry(session, scenic_source.id, "visits", {
            "label": make_item("入园记录", "llm"), "grain": make_item("每张门票每次检票一行", "human")},
            if_version=0, actor="王敏")
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": "各入口闸机的入园人次", "targets": [
        {"kind": "table", "name": "visits"}, {"kind": "table", "name": "gates"}]}, LookBudget())
    assert f"visits.gate_id = gates.id（多对一）{INFERRED_MARK}" in result.text
    assert f"中文名：入园记录{INFERRED_MARK}" in result.text
    assert "粒度：每张门票每次检票一行" in result.text and f"每张门票每次检票一行{INFERRED_MARK}" not in result.text
    assert "推断，未确认」的说明和连接条件" in result.text
    (block,) = result.record["objects"]
    assert block["catalog_tables"] == 1 and set(block["catalog_versions"]) == {"visits"}


async def test_rejected_relations_are_not_used_for_join_paths(scenic_source):
    """人工驳回的关系不能拿来补路径：驳回 channel_visits → visits 后，关联表不会因为补路径被带进来。"""
    rels = [dict(r, status="rejected") if r["to_table"] == "visits" else r
            for r in catalog.fk_relations(scenic_source.schema_cache, "channel_visits")]
    async with SessionLocal() as session:
        await catalog.write_entry(session, scenic_source.id, "channel_visits", {"relations": rels},
                                  if_version=0, actor="王敏")
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": NEED, "targets": [
        {"kind": "table", "name": "visits"}, {"kind": "table", "name": "channels"}]}, LookBudget())
    assert result.record["objects"][0]["joined"] == []
    assert "channel_visits.visit_id = visits.id" not in result.text


async def test_bridges_respect_the_total_limit(scenic_source):
    names = list(scenic_source.schema_cache["tables"])
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": "全部都要", "targets": [
        {"kind": "table", "name": n} for n in names]}, LookBudget())
    (block,) = result.record["objects"]
    assert len(block["named"]) + len(block["joined"]) <= CONTEXT_TABLE_LIMIT
    assert f"一次最多查看 {CONTEXT_TABLE_LIMIT} 张表" in result.text

    spread = ["reviews", "channels", "stores", "tour_groups", "coupons", "products"]
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": "x", "targets": [
        {"kind": "table", "name": n} for n in spread]}, LookBudget())
    (block,) = result.record["objects"]
    assert set(spread) <= set(block["named"])
    assert len(block["named"]) + len(block["joined"]) <= CONTEXT_TABLE_LIMIT


# ---------------------------------------------------------------- 遮罩（D6）


async def test_masked_columns_give_only_name_and_type_everywhere(client, masked_source):
    """遮罩列只给列名和类型：目录里这一列的中文名、码值、带取值的含义都不给。助手查看、db_schema__、/schema 接口
    是同一份说法（look 文档 D6）。"""
    from app.tools.datasource import _catalog_of, _make_schema_tool

    entries = await _catalog_of(masked_source)
    detail = schema_view.table_detail(masked_source, "members",
                                      catalog_notes={t: e.notes for t, e in entries.items()})
    assert "phone  TEXT  已遮罩，只给列名和类型" in detail
    assert "手机号" not in detail and "移动号段" not in detail and "13800000000" not in detail
    # 别的列的目录照给
    assert "1=普通" in detail and "中文名：会员" in detail

    tool = _make_schema_tool(masked_source, catalog=entries)
    assert await tool.coroutine(table="members") == detail
    body = (await client.get(f"/api/datasources/{masked_source.id}/schema", params={"table": "members"})).json()
    assert body["detail"] == detail

    looks = await _service([masked_source])
    result = await looks.look({"op": "look", "need": "会员", "targets": [
        {"kind": "table", "name": "members"}]}, LookBudget())
    assert "已遮罩，只给列名和类型" in result.text and "手机号" not in result.text and "移动号段" not in result.text
    assert "设了遮罩" in await looks.overview()


# ---------------------------------------------------------------- 字数上限、重复查看、次数


async def test_tables_over_the_char_limit_only_get_column_names(monkeypatch, scenic_source):
    import app.look.service as service

    monkeypatch.setattr(service, "LOOK_RESULT_CHARS", 400)
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": "订单", "targets": [
        {"kind": "table", "name": n} for n in ("orders", "order_items", "payments", "refunds")]}, LookBudget())
    assert "字数超了只给字段名" in result.text and "后面的表只给了字段名" in result.text
    # 第一张表给全了，后面的只给字段名
    assert "total_amount  REAL" in result.text
    assert "refunds[表]：" in result.text


async def test_looking_at_the_same_tables_again_is_short_circuited(scenic_source):
    looks = await _service([scenic_source])
    budget = LookBudget()
    line = {"op": "look", "need": "门店", "targets": [{"kind": "table", "name": "stores"}]}
    first = await looks.look(line, budget)
    again = await looks.look(line, budget)
    assert first.record is not None and again.record is None and again.counted
    assert "已经在上面" in again.text and "store_code" not in again.text
    assert budget.used == 2 and [r["id"] for r in looks.records] == ["L1"]
    # 多了一张没看过的就照常查看
    more = await looks.look({**line, "targets": [{"kind": "table", "name": "stores"},
                                                 {"kind": "table", "name": "parks"}]}, budget)
    assert more.record["id"] == "L2"


async def test_need_is_required_and_the_limit_is_kept(scenic_source):
    looks = await _service([scenic_source])
    budget = LookBudget(limit=1)
    nothing = await looks.look({"op": "look", "targets": [{"kind": "table", "name": "stores"}]}, budget)
    assert "look 要写 need" in nothing.text and not nothing.counted and budget.used == 0
    await looks.look({"op": "look", "need": "门店", "targets": [{"kind": "table", "name": "stores"}]}, budget)
    over = await looks.look({"op": "look", "need": "园区", "targets": [{"kind": "table", "name": "parks"}]}, budget)
    assert "查看次数已用完" in over.text and over.record is None and not over.counted


# ---------------------------------------------------------------- 旧写法


def test_the_old_tables_field_is_read_as_table_targets():
    """look 文档初稿的写法 tables:[{source, table}]（以及只写表名的字符串）读进来换成 kind=table 的 targets。"""
    assert parse_targets({"op": "look", "need": "x", "tables": [{"source": "库 B", "table": "refunds"}, "orders"],
                          "targets": [{"kind": "capability", "name": "db_query__b"}, {"name": "没写类别"}]}) == [
        {"kind": "capability", "name": "db_query__b"},
        {"kind": "table", "name": "refunds", "source": "库 B"},
        {"kind": "table", "name": "orders"},
    ]


async def test_the_old_tables_field_still_looks(scenic_source):
    looks = await _service([scenic_source])
    result = await looks.look({"op": "look", "need": "门店",
                               "tables": [{"source": scenic_source.name, "table": "stores"}]}, LookBudget())
    assert result.record["objects"][0]["named"] == ["stores"] and "store_code  TEXT" in result.text
