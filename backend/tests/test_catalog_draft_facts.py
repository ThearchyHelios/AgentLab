"""防错事实的提议：模型起草时提议有效行过滤（valid_filter）、去重规则（dedup），附一句依据。

- 提示词写清两项只在表结构、注释、码值能看出来时才写；结构化输出、纯文本两条路都认这两项；
- 有效行过滤按数据源的方言用 sqlglot 解析成一个条件：只能引用这张表的列，不能有子查询、聚合、窗口函数、分号，只允许
  比较、IN、BETWEEN、LIKE、IS NULL、AND / OR / NOT 和 UPPER / LOWER / TRIM / COALESCE，不超过 500 字。不过就丢掉；
- 开了数据剖析、能连上的源：经剖析的只读守卫数出排除比例，排除 0% 或 100% 的不收，收下的在 note 里写剖析核对的结果；
  没开剖析、连不上的照收，note 写没有核对的原因；
- 开了剖析的源，目录里记下的码值交给模型做背景（遮罩的列不给）；
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
