"""防错事实的提议：模型起草时提议有效行过滤（valid_filter）、去重规则（dedup），附一句依据。

- 提示词写清两项只在表结构、注释、码值能看出来时才写；结构化输出、纯文本两条路都认这两项；
- 有效行过滤按数据源的方言用 sqlglot 解析成一个条件：只能引用这张表的列，不能有子查询、聚合、窗口函数、分号，只允许
  比较、IN、BETWEEN、LIKE、IS NULL、AND / OR / NOT 和 UPPER / LOWER / TRIM / COALESCE，不超过 500 字。不过就丢掉；
  存的是整理过的写法：原文照旧，列换成表结构里的真名、不带表名、按方言加引号（剖析核对的也是这一条）；
- 开了数据剖析、能连上的源：经剖析的只读守卫数出排除比例，排除 0% 或 100% 的不收，收下的在 note 里写剖析核对的结果；
  没开剖析、连不上的照收，note 写没有核对的原因；
- 开了剖析的源，目录里记下的码值交给模型做背景（遮罩的列不给；「已确认的目录项」里遮罩列的码值也不给）；
- 这两项照旧记模型起草 / 推断；SQL 检查只在人工确认、有确证时才把漏了有效行过滤报成警告，推断的只是提示。

用合成的景区业务库（tests/fixtures/catalog/scenic.py）和虚构的表结构，不调用真实模型。
"""
from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

import pytest

from app.data import catalog, catalog_profile, sqlcheck
from app.data import catalog_draft as D
from app.data.catalog import make_item
from app.data.catalog_profile import PROFILE_OPTION
from app.data.engine import MASK_COLUMNS_OPTION, engines
from app.data.introspect import introspect
from tests.fixtures.catalog.fake_model import FakeModel, answer, requested

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _col(name: str, type_: str = "INTEGER", comment: str | None = None) -> dict:
    return {"name": name, "type": type_, "nullable": True, "comment": comment}


def _orders() -> dict:
    return {"qualified": "orders", "schema": None, "is_view": False, "primary_key": ["id"], "foreign_keys": [],
            "comment": None,
            "columns": [_col("id"), _col("order_no", "TEXT"), _col("status", "TEXT", comment="C 作废，X 测试单"),
                        _col("amount", "REAL"), _col("updated_at", "TEXT")]}


def _plain(tables: dict, **kw) -> SimpleNamespace:
    """不连库的源（没有 options：没开数据剖析）。"""
    return SimpleNamespace(id="facts-src", name="facts", kind=kw.pop("kind", "sqlite"), origin="manual",
                           description="", schema_cache={"tables": tables}, **kw)


_CACHE: dict[str, dict] = {}


async def _scenic_cache(path: str) -> dict:
    if path not in _CACHE:
        probe = SimpleNamespace(id="facts-scenic-probe", name="scenic", kind="sqlite", database=path, host=None,
                                port=None, username=None, password=None, options={}, readonly=True, description="",
                                schema_cache={}, origin="manual")
        try:
            _CACHE[path] = await introspect(probe)
        finally:
            await engines.invalidate(probe.id)
    return _CACHE[path]


@pytest.fixture
async def scenic(scenic_db):
    """接上景区库的源（开了数据剖析）。用完作废连接池。"""
    made: list[str] = []

    async def make(*, options: dict | None = None, database: str | None = None, enabled: bool = True):
        sid = f"facts-{len(made)}-{id(made)}"
        made.append(sid)
        opts = {PROFILE_OPTION: {"enabled": True}} if options is None else options
        return SimpleNamespace(id=sid, name="scenic", kind="sqlite", database=database or scenic_db, host=None,
                               port=None, username=None, password=None, options=opts, readonly=True, description="",
                               schema_cache=await _scenic_cache(scenic_db), origin="manual", enabled=enabled)

    yield make
    for sid in made:
        await engines.invalidate(sid)


def _count(path: str, where: str) -> tuple[int, int]:
    """(全表行数, 不满足条件的行数)：直接用 sqlite3 数，核对剖析算出来的数。"""
    db = sqlite3.connect(path)
    try:
        total = db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        kept = db.execute(f"SELECT COUNT(*) FROM visits WHERE {where}").fetchone()[0]
    finally:
        db.close()
    return total, total - kept


# --------------------------------------------------------------------------
# 校验有效行过滤
# --------------------------------------------------------------------------

META = _orders()


@pytest.mark.parametrize("condition, columns", [
    ("status NOT IN ('C', 'X')", ["status"]),
    ("STATUS NOT IN ('C', 'X')", ["status"]),                     # 列名不分大小写，记表结构里的写法
    ("orders.status <> 'C'", ["status"]),                          # 带这张表的表名
    ("status IS NOT NULL AND amount > 0", ["status", "amount"]),
    ("NOT (status = 'C' OR status = 'X')", ["status"]),
    ("amount BETWEEN 0 AND 100000", ["amount"]),
    ("order_no NOT LIKE 'TEST%'", ["order_no"]),
    ("UPPER(TRIM(status)) <> 'C'", ["status"]),
    ("COALESCE(status, '') <> 'X' AND amount >= -1", ["status", "amount"]),
    ("status = 'C' OR updated_at IS NULL", ["status", "updated_at"]),
])
def test_valid_filters_that_pass(condition, columns):
    parsed = sqlcheck.parse_valid_filter(condition, META, kind="sqlite", table="orders")
    assert parsed.problem is None, parsed.problem
    assert list(parsed.columns) == columns
    assert sqlcheck.valid_filter_problem(condition, META, kind="mysql", table="orders") is None


@pytest.mark.parametrize("condition, said", [
    ("status = 'C'; DROP TABLE orders", "分号"),
    ("status IN (SELECT code FROM codes)", "子查询"),
    ("EXISTS (SELECT 1 FROM refunds)", "子查询"),
    ("COUNT(status) > 1", "聚合"),
    ("ROW_NUMBER() OVER (ORDER BY id) = 1", "窗口"),
    ("LENGTH(status) > 0", "LENGTH"),
    ("amount * 2 > 10", "不支持的写法"),
    ("payments.status = 1", "只能引用这张表的列"),
    ("nope = 1", "nope"),
    ("1 = 1", "没有用到这张表的任何列"),
    ("status", "一个条件"),
    ("status = ?", "不支持的写法"),
    ("status = 'C' -- 注释", "注释"),
    ("status = '{{ input.code }}'", "模板"),
    ("status = '" + "C" * 600 + "'", "500"),
    ("status = (", "解析"),
    ("status = 'C' UNION SELECT 1", "一个条件"),
])
def test_valid_filters_that_are_dropped(condition, said):
    problem = sqlcheck.valid_filter_problem(condition, META, kind="sqlite", table="orders")
    assert problem is not None and said in problem, problem


def test_a_source_kind_without_a_dialect_cannot_be_checked():
    assert "不支持" in sqlcheck.valid_filter_problem("status = 'C'", META, kind="mssql", table="orders")


def test_the_condition_is_rendered_with_the_columns_quoted_like_the_source():
    meta = {**META, "columns": [*META["columns"], _col("isTest")]}
    parsed = sqlcheck.parse_valid_filter("istest = 0 AND STATUS <> 'C'", meta, kind="mysql", table="orders")
    assert parsed.problem is None
    rendered = sqlcheck.render_valid_filter(parsed, quoted=lambda name: name != name.lower())
    assert rendered == "`isTest` = 0 AND status <> 'C'"


#: 带一列混合大小写列名的订单表（ORM 建的 PostgreSQL 库里常见）
MIXED = {**META, "columns": [*META["columns"], _col("OrderStatus", "TEXT")]}


@pytest.mark.parametrize("kind, condition, stored", [
    # PostgreSQL 把没加引号的名字折成小写：混合大小写的列一律加引号，不论模型写的大小写对不对
    ("postgresql", "orderstatus <> 'X' AND STATUS NOT IN ('C', 'X')",
     "\"OrderStatus\" <> 'X' AND status NOT IN ('C', 'X')"),
    ("postgresql", "OrderStatus <> 'X'", "\"OrderStatus\" <> 'X'"),
    ("postgresql", "orders.\"OrderStatus\" IS NOT NULL", "\"OrderStatus\" IS NOT NULL"),
    ("mysql", "orderstatus <> 'X'", "`OrderStatus` <> 'X'"),
    ("oracle", "ORDERS.STATUS <> 'C'", "status <> 'C'"),
    # 不用改的照原文：NOT IN、IS NOT NULL 这些写法不被改写
    ("sqlite", "status NOT IN ('C', 'X') AND updated_at IS NOT NULL",
     "status NOT IN ('C', 'X') AND updated_at IS NOT NULL"),
])
def test_the_stored_filter_names_its_columns_the_way_the_source_needs(kind, condition, stored):
    """存进目录的有效记录条件：原文照旧，只把列换成表结构里的真名、不带表名，按数据源的需要加引号（和剖析核对查询的写法
    一致）。以前存的是模型的原文：PostgreSQL 上 orderstatus 会被当成另一列，核对时却按真名加了引号，存下的和核对过的
    不是同一条，SQL 检查还会让模型照抄这一句。"""
    entry = {"table": "orders", "valid_filter": {"condition": condition, "reason": "注释写明"}}
    notes = catalog._from_model(entry, MIXED, system=False, source_kind=kind, table="orders")
    assert notes["valid_filter"]["value"] == stored
    again = sqlcheck.parse_valid_filter(stored, MIXED, kind=kind, table="orders")
    assert again.problem is None and sqlcheck.stored_valid_filter(again, kind) == stored, "存下的再整理一遍不变"


def test_a_filter_that_grows_past_the_limit_once_quoted_is_dropped_not_written():
    """整理时加了引号、超过 500 字的不收：目录里这一项最多 500 字，写进去会让整张表写入失败。"""
    condition = "orderstatus IN (" + ", ".join(f"'{i:03d}'" for i in range(66)) + ") OR updated_at IS NULL"
    assert len(condition) == 499
    notes = catalog._from_model({"table": "orders", "label": "订单", "valid_filter": condition}, MIXED, system=False,
                                source_kind="postgresql", table="orders")
    assert "valid_filter" not in notes and notes["label"]["value"] == "订单"
    assert not catalog.validate_notes(notes)


def test_columns_that_differ_only_in_case_must_be_named_exactly():
    """表结构里有只差大小写的两列：写得和其中一列一模一样的认那一列，否则说不清是哪一列，不收。"""
    meta = {**META, "columns": [*META["columns"], _col("Status", "TEXT")]}
    problem = sqlcheck.valid_filter_problem("STATUS = 'C'", meta, kind="postgresql", table="orders")
    assert problem == ("有效记录条件里的列「STATUS」在表结构里有只差大小写的几列（status、Status），"
                       "要照表结构的写法写清是哪一列")
    for condition, stored in (("\"Status\" = 'C'", "\"Status\" = 'C'"), ("status = 'C'", "status = 'C'")):
        parsed = sqlcheck.parse_valid_filter(condition, meta, kind="postgresql", table="orders")
        assert parsed.problem is None and sqlcheck.stored_valid_filter(parsed, "postgresql") == stored


# --------------------------------------------------------------------------
# 模型给的两项：校验、依据
# --------------------------------------------------------------------------


def test_the_model_fields_become_llm_items_with_their_reason():
    entry = {"table": "orders", "label": "订单",
             "valid_filter": {"condition": "status NOT IN ('C', 'X')", "reason": "状态注释写明 C 作废、X 测试单"},
             "dedup": {"rule": "同一订单号有多行时只取最后修改的一行", "reason": "订单号可能重复导出"}}
    notes = catalog._from_model(entry, META, system=False, source_kind="sqlite", table="orders")
    vf, dd = notes["valid_filter"], notes["dedup"]
    assert (vf["value"], vf["source"], vf["status"]) == ("status NOT IN ('C', 'X')", "llm", "proposed")
    assert vf["note"] == "依据：状态注释写明 C 作废、X 测试单"
    assert (dd["value"], dd["source"], dd["status"]) == ("同一订单号有多行时只取最后修改的一行", "llm", "proposed")
    assert dd["note"] == "依据：订单号可能重复导出"
    assert not catalog.validate_notes(notes)


def test_plain_strings_are_accepted_and_bad_filters_dropped(caplog):
    entry = {"table": "orders", "label": "订单", "valid_filter": "status <> 'C'", "dedup": "按订单号去重"}
    notes = catalog._from_model(entry, META, system=False, source_kind="sqlite", table="orders")
    assert notes["valid_filter"]["value"] == "status <> 'C'" and "note" not in notes["valid_filter"]
    assert notes["dedup"]["value"] == "按订单号去重"

    with caplog.at_level(logging.INFO, logger="app.data.catalog"):
        notes = catalog._from_model({"table": "orders", "label": "订单",
                                     "valid_filter": {"condition": "status IN (SELECT 1)", "reason": "猜的"}},
                                    META, system=False, source_kind="sqlite", table="orders")
    assert "valid_filter" not in notes and notes["label"]["value"] == "订单", "丢掉这一项，别的照收"
    assert any("有效行过滤" in r.getMessage() and "orders" in r.getMessage() for r in caplog.records), "日志记一条"
    for junk in ({"condition": ""}, {"reason": "只有依据"}, ["status = 1"], 3):
        notes = catalog._from_model({"table": "orders", "valid_filter": junk, "dedup": junk}, META, system=False,
                                    source_kind="sqlite", table="orders")
        assert "valid_filter" not in notes and "dedup" not in notes, junk


def test_the_prompt_asks_for_the_two_facts_only_when_evident():
    system = catalog.CATALOG_DRAFT_SYSTEM
    assert "valid_filter" in system and "dedup" in system
    assert "拿不准就不写" in system and "reason" in system
    assert "UPPER、LOWER、TRIM、COALESCE" in system
    props = catalog.CATALOG_DRAFT_PROMPT_SCHEMA["properties"]["tables"]["items"]["properties"]
    assert set(props["valid_filter"]["properties"]) == {"condition", "reason"}
    assert set(props["dedup"]["properties"]) == {"rule", "reason"}
    assert '"valid_filter": {"condition"' in catalog.CATALOG_DRAFT_TEXT_PROMPT
    assert '"dedup": {"rule"' in catalog.CATALOG_DRAFT_TEXT_PROMPT
    assert "有效行过滤" in catalog.CATALOG_DRAFT_PROMPT_LABELS["only_table"]


# --------------------------------------------------------------------------
# 引擎：没开剖析照收、写明没有核对；开了剖析的源把码值交给模型
# --------------------------------------------------------------------------

REPLY = {"label": "订单", "grain": "每笔订单一行",
         "valid_filter": {"condition": "status NOT IN ('C', 'X')", "reason": "状态注释写明 C 作废、X 测试单"},
         "dedup": {"rule": "同一订单号只取最后一行", "reason": "订单号可能重复"},
         "columns": [{"name": "status", "label": "订单状态"}]}


async def test_without_profiling_the_filter_is_kept_and_says_it_was_not_checked():
    model = FakeModel({"orders": REPLY})
    got = await D.run_model_draft(model, _plain({"orders": _orders()}), ["orders"])
    o = got["orders"]
    assert o.status == "ok"
    vf = o.notes["valid_filter"]
    assert (vf["source"], vf["status"]) == ("llm", "proposed")
    assert vf["note"] == "依据：状态注释写明 C 作废、X 测试单。没有核对排除比例（数据源没有开数据剖析）"
    assert o.notes["dedup"]["note"] == "依据：订单号可能重复"


async def test_an_invalid_filter_from_the_model_is_dropped_not_failed():
    reply = {**REPLY, "valid_filter": {"condition": "status IN (SELECT code FROM bad_codes)", "reason": "猜的"}}
    got = await D.run_model_draft(FakeModel({"orders": reply}), _plain({"orders": _orders()}), ["orders"])
    o = got["orders"]
    assert o.status == "ok" and o.error is None, "不算失败"
    assert "valid_filter" not in o.notes and o.notes["dedup"]["value"] == "同一订单号只取最后一行"


def _codes_existing() -> dict:
    return {"orders": {"columns": {
        "status": {"codes": make_item({"C": "作废", "X": "", "P": "已支付"}, "profile", "proposed")},
        "order_no": {"codes": make_item({"A1": "甲"}, "profile", "proposed")},
        "amount": {"codes": make_item({"0": "零"}, "profile", "rejected")},
    }}}


async def test_codes_go_to_the_model_when_profiling_is_on_but_not_masked_ones():
    on = {PROFILE_OPTION: {"enabled": True}, MASK_COLUMNS_OPTION: ["ORDER_NO"]}
    model = FakeModel({"orders": REPLY})
    await D.run_model_draft(model, _plain({"orders": _orders()}, options=on), ["orders"], existing=_codes_existing())
    text = model.calls[0]
    head = catalog.CATALOG_DRAFT_PROMPT_LABELS["codes"]
    assert head in text
    block = text[text.index(head):]
    assert "- status：C=作废、P=已支付，X 的含义待填写" in block
    assert "A1" not in text, "遮罩的列不给码值"
    assert "零" not in text, "驳回的码值不给"

    plain = FakeModel({"orders": REPLY})
    await D.run_model_draft(plain, _plain({"orders": _orders()}), ["orders"], existing=_codes_existing())
    assert head not in plain.calls[0] and "已支付" not in plain.calls[0], "没开剖析不给码值"


@pytest.mark.parametrize("options", [
    {PROFILE_OPTION: {"enabled": True}, MASK_COLUMNS_OPTION: ["ORDER_NO"]},
    {MASK_COLUMNS_OPTION: "order_no"},
], ids=["profiled", "not-profiled"])
async def test_confirmed_codes_of_a_masked_column_do_not_reach_the_model_either(options):
    """遮罩的列不给码值，人工确认过的也一样：码值就是这一列的取值。以前经「已确认的目录项」照给（开没开剖析都给）；
    没遮罩的列确认过的码值照给（以此为准），遮罩列别的确认过的项（中文名）也照给。"""
    existing = {"orders": {"columns": {
        "order_no": {"codes": make_item({"ZX-0417": "内部测试单"}, "human", "confirmed"),
                     "label": make_item("订单号", "human", "confirmed")},
        "status": {"codes": make_item({"C": "作废"}, "human", "confirmed")},
    }}}
    model = FakeModel({"orders": REPLY})
    await D.run_model_draft(model, _plain({"orders": _orders()}, options=options), ["orders"], existing=existing)
    text = model.calls[0]
    assert "ZX-0417" not in text and "内部测试单" not in text
    confirmed = text[text.index(catalog.CATALOG_DRAFT_PROMPT_LABELS["confirmed"]):]
    assert "status：码值：C=作废" in confirmed and "order_no：订单号" in confirmed


async def test_codes_reach_the_table_part_of_a_wide_table_and_only_their_column_part():
    cols = [_col(f"c{i:03d}") for i in range(1, 46)] + [_col("state", "TEXT")]
    wide = {"qualified": "wide", "schema": None, "is_view": False, "primary_key": ["c001"], "foreign_keys": [],
            "comment": None, "columns": cols}
    existing = {"wide": {"columns": {"state": {"codes": make_item({"9": "作废"}, "profile", "proposed")}}}}
    on = {PROFILE_OPTION: {"enabled": True}}
    model = FakeModel({"wide": {"label": "宽表"}})
    await D.run_model_draft(model, _plain({"wide": wide}, options=on), ["wide"], existing=existing)
    head = catalog.CATALOG_DRAFT_PROMPT_LABELS["codes"]
    with_codes = [requested(c)["wide"] for c in model.calls if "9=作废" in c]
    assert ("table", None) in with_codes, "表级那段要提议有效行过滤：给全部码值"
    assert all(part == "table" or "state" in (cols or []) for part, cols in with_codes)
    assert any(head not in c for c in model.calls), "不含这一列的列分段不给"


# --------------------------------------------------------------------------
# 剖析核对排除比例
# --------------------------------------------------------------------------


async def test_the_check_counts_excluded_rows_through_the_read_only_guard(scenic, scenic_db, monkeypatch):
    seen: list[tuple[str, bool]] = []
    real = catalog_profile.guard.check

    def spy(sql, **kw):
        seen.append((sql, kw.get("readonly")))
        return real(sql, **kw)

    monkeypatch.setattr(catalog_profile.guard, "check", spy)
    source = await scenic()
    meta = source.schema_cache["tables"]["visits"]
    found = await catalog_profile.check_valid_filter(source, "visits", meta, "status = 1")
    total, excluded = _count(scenic_db, "status = 1")
    assert 0 < excluded < total
    assert found.keep and (found.rows, found.excluded) == (total, excluded) and not found.sampled
    share = catalog_profile.excluded_share(excluded, total)
    assert found.note == f"剖析核对：排除 {share} 的行（共 {total:,} 行）"
    assert len(seen) == 1 and seen[0][1] is True, "一条查询，过只读守卫"
    assert "COUNT(*)" in seen[0][0] and "status = 1" in seen[0][0]


async def test_the_check_runs_the_condition_as_it_is_stored(scenic, monkeypatch):
    """剖析核对的就是存进目录的那一条：表名去掉、列换成表结构里的真名，别的照原文（NOT IN 不被改写成 NOT x IN）。"""
    seen: list[str] = []
    real = catalog_profile.guard.check

    def spy(sql, **kw):
        seen.append(sql)
        return real(sql, **kw)

    monkeypatch.setattr(catalog_profile.guard, "check", spy)
    source = await scenic()
    reply = {"label": "入园记录", "valid_filter": {"condition": "VISITS.STATUS NOT IN (0)", "reason": "0 是作废的检票"}}
    got = await D.run_model_draft(FakeModel({"visits": reply}), source, ["visits"])
    vf = got["visits"].notes["valid_filter"]
    assert vf["value"] == "status NOT IN (0)"
    assert len(seen) == 1 and f"CASE WHEN {vf['value']} THEN 0 ELSE 1 END" in seen[0]
    assert "剖析核对：排除" in vf["note"]


@pytest.mark.parametrize("condition", ["status IN (0, 1)", "status = 7"])
async def test_a_filter_that_excludes_nothing_or_everything_is_dropped(scenic, condition):
    source = await scenic()
    found = await catalog_profile.check_valid_filter(source, "visits", source.schema_cache["tables"]["visits"],
                                                      condition)
    assert not found.keep and found.dropped


async def test_a_big_table_is_checked_on_its_first_rows(scenic):
    source = await scenic(options={PROFILE_OPTION: {"enabled": True, "max_scan_rows": 100, "sample_size": 10}})
    found = await catalog_profile.check_valid_filter(source, "visits", source.schema_cache["tables"]["visits"],
                                                      "status = 1")
    assert found.sampled and found.rows == 100
    assert found.note.startswith("剖析核对：按前 100 行统计，")


async def test_unchecked_filters_are_kept_with_the_reason(scenic, tmp_path):
    meta_of = lambda s: s.schema_cache["tables"]["visits"]  # noqa: E731
    off = await scenic(options={})
    found = await catalog_profile.check_valid_filter(off, "visits", meta_of(off), "status = 1")
    assert found.keep and found.note == "没有核对排除比例（数据源没有开数据剖析）"

    masked = await scenic(options={PROFILE_OPTION: {"enabled": True}, MASK_COLUMNS_OPTION: "Status"})
    found = await catalog_profile.check_valid_filter(masked, "visits", meta_of(masked), "status = 1")
    assert found.keep and found.note == "没有核对排除比例（条件里的 status 在数据源设置中被遮罩，不取数）"

    stopped = await scenic(enabled=False)
    found = await catalog_profile.check_valid_filter(stopped, "visits", meta_of(stopped), "status = 1")
    assert found.keep and found.note == "没有核对排除比例（数据源已停用）"

    gone = await scenic(database=str(tmp_path / "missing" / "nope.db"))
    found = await catalog_profile.check_valid_filter(gone, "visits", meta_of(gone), "status = 1")
    assert found.keep and found.note.startswith("没有核对排除比例（查询失败：")


async def test_the_engine_checks_the_filter_on_a_profiled_source(scenic, scenic_db):
    source = await scenic()
    good = {"label": "入园记录", "valid_filter": {"condition": "status = 1", "reason": "0 是作废的检票"}}
    got = await D.run_model_draft(FakeModel({"visits": good}), source, ["visits"])
    vf = got["visits"].notes["valid_filter"]
    total, excluded = _count(scenic_db, "status = 1")
    share = catalog_profile.excluded_share(excluded, total)
    assert vf["note"] == f"依据：0 是作废的检票。剖析核对：排除 {share} 的行（共 {total:,} 行）"

    useless = {"label": "入园记录", "valid_filter": {"condition": "status IN (0, 1)", "reason": "猜的"}}
    got = await D.run_model_draft(FakeModel({"visits": useless}), source, ["visits"])
    assert got["visits"].status == "ok" and "valid_filter" not in got["visits"].notes, "排除 0% 的不收"
    assert got["visits"].notes["label"]["value"] == "入园记录"


async def test_a_confirmed_filter_is_not_checked_again(scenic, monkeypatch):
    source = await scenic()
    called: list[str] = []

    async def counting(*args, **kw):
        called.append("x")
        return catalog_profile.FilterCheck(True, "不该用到")

    monkeypatch.setattr(catalog_profile, "check_valid_filter", counting)
    existing = {"visits": {"valid_filter": make_item("status = 1", "human", "confirmed")}}
    reply = {"label": "入园记录", "valid_filter": {"condition": "status <> 0", "reason": "0 是作废"}}
    await D.run_model_draft(FakeModel({"visits": reply}), source, ["visits"], existing=existing)
    assert called == [], "人工确认过的有效行过滤，起草的提议并入时不会采用，不必再查库"


# --------------------------------------------------------------------------
# SQL 检查：推断的有效行过滤只是提示，确认后才是警告
# --------------------------------------------------------------------------


async def test_a_drafted_filter_only_hints_until_someone_confirms_it(scenic):
    source = await scenic(options={})
    entry = {"table": "visits", "valid_filter": {"condition": "status = 1", "reason": "0 是作废"}}
    meta = source.schema_cache["tables"]["visits"]
    notes = catalog._from_model(entry, meta, system=False, source_kind="sqlite", table="visits")
    sql = "SELECT COUNT(*) FROM visits"

    def levels(table_notes: dict) -> list[tuple[str, str]]:
        checker = sqlcheck.SqlChecker(kind="sqlite", schema_cache=source.schema_cache, notes={"visits": table_notes})
        return [(c.code, c.level) for c in checker.check(sql)]

    assert levels(notes) == [("missing_valid_filter", "info")]
    confirmed = catalog.review_item(notes, "valid_filter", "confirm")
    assert levels(confirmed) == [("missing_valid_filter", "warning")]
    assert levels(catalog.review_item(notes, "valid_filter", "reject")) == []
