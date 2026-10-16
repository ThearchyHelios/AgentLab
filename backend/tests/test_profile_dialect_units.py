"""剖析与探查的方言修正（下一轮 03 细案 C1）：不连库的部分。

- 列类型按方言编译的完整写法：类型族、日期列、码值候选都认得 ENUM / SET、VARCHAR2(20 CHAR)、INTERVAL 这些写法；
- IN 列表的字面量按被指向列的类型族生成（PG 上文字列配整数子列会直接报错，Oracle 会把父列转成数报 ORA-01722）；
- Oracle 小写带引号的对象（quote_name）强制加引号、不转大写；
- 统计信息估出的行数不超过整表统计上限时再数一次确认；
- 日期列的最小值、最大值排除零日期和哨兵日期，照实写排除了几个；
- 取值观察要用的三个方言方法（date_literal / day_trunc / sample_from）；
- 说明文字不再说「抽样」：大表看的是按存储顺序的前 N 行。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any

import pytest
from sqlalchemy import VARCHAR, Date, Numeric
from sqlalchemy.dialects import mysql, oracle, postgresql, sqlite
from sqlalchemy.sql.elements import quoted_name

from app.data import catalog, catalog_profile, introspect
from app.data.catalog_profile import ProfileSettings, SqlDialect, TableSize
from app.data.engine import QueryResult

DIALECTS = ("mysql", "mariadb", "postgresql", "oracle", "sqlite")


# ---------------------------------------------------------------- 类型的写法


@pytest.mark.parametrize("type_name, family", [
    ("ENUM('paid','refund')", "text"),
    ("ENUM('int','date')", "text"),             # 取值里的词不能让它变成数字或时间
    ("SET('a','b')", "text"),
    ("VARCHAR2(20 CHAR)", "text"),
    ("NVARCHAR2(10)", "text"),
    ("VARCHAR(20) COLLATE utf8mb4_bin", "text"),
    ("NUMBER(10, 0)", "number"),
    ("INTEGER(11)", "number"),
    ("BIGINT UNSIGNED", "number"),
    ("DOUBLE PRECISION", "number"),
    ("TIMESTAMP WITH TIME ZONE", "time"),
    ("TIMESTAMP(3)", "time"),
    ("INTERVAL DAY(2) TO SECOND(6)", None),    # 时长不是数也不是时刻；以前按「INT」认成了数字
    ("order_status", None),
    ("", None),
])
def test_type_family_reads_dialect_full_types(type_name, family):
    assert catalog._type_family(type_name) == family


def test_type_family_of_a_column_uses_its_enum_values():
    """PostgreSQL 的枚举类型编译出来是类型名（order_status），只有取值列表能说明它是文字。"""
    assert catalog.column_family({"name": "status", "type": "order_status", "enum_values": ["a", "b"]}) == "text"
    assert catalog.column_family({"name": "n", "type": "INTEGER"}) == "number"
    assert catalog.column_family({"name": "x"}) is None


def test_enum_values_never_make_a_date_column():
    assert not catalog.is_date_column("status", "ENUM('date','time')")
    assert catalog.is_date_column("paid_at", "TIMESTAMP WITH TIME ZONE")
    assert catalog.is_date_column("biz_dt", "VARCHAR2(10 CHAR)")      # 文字存日期、名字像日期
    assert not catalog.is_date_column("gap", "INTERVAL DAY(2) TO SECOND(6)")


# PostgreSQL 的枚举、域编译出来是类型名本身：名字以 set、enum 开头不能被当成 MySQL 的 SET / ENUM，PG 的枚举一律靠
# 探查记下的 enum_values 认
@pytest.mark.parametrize("type_name, family", [
    ("setup_status", None),
    ("settlement_amount", None),
    ("enum_kind", None),
    ("SET ('a','b')", "text"),
    ("enum('paid')", "text"),
])
def test_only_mysql_enum_and_set_spellings_are_enum(type_name, family):
    assert catalog._type_family(type_name) == family
    assert catalog_profile._is_enum(type_name) is (family == "text")


def test_postgres_type_names_starting_with_set_or_enum():
    # 带取值列表的 PG 枚举：是码值候选、是文字，但不是 MySQL 的 SET
    assert catalog_profile._is_enum("setup_status", ["draft", "done"])
    assert catalog.column_family({"name": "stage", "type": "setup_status", "enum_values": ["draft"]}) == "text"
    # 名字以 set 开头的数值域：不是文字、不是码值候选
    assert not catalog_profile._code_type_ok("settlement_amount")
    # 名字以 set 开头的日期域：照样是日期列
    assert catalog.is_date_column("settled_on", "settlement_date")
    # 枚举的类型名里碰巧有 DATE（UPDATE 里的）：有取值列表就不是日期列
    assert not catalog.is_date_column("reason", "update_reason", enum_values=["manual", "sync"])
    assert not catalog_profile._is_date_column({"name": "reason", "type": "update_reason",
                                                "enum_values": ["manual", "sync"]})


def test_postgres_enum_named_like_set_keeps_its_unseen_values():
    """PG 枚举 setup_status 不是 MySQL 的 SET：列定义里这次没出现的取值也写进码值（含义待填）。"""
    settings = ProfileSettings(enabled=True)
    source = type("S", (), {"id": "s1", "name": "shop", "kind": "postgresql", "options": {}})()
    tables = {"jobs": {"columns": [{"name": "stage", "type": "setup_status",
                                    "enum_values": ["draft", "running", "done"]}]}}
    job = catalog_profile.ProfileContext(source=source, settings=settings, dialect=SqlDialect("postgresql"),
                                         runner=None, tables=tables, masked=set())
    job.sizes["jobs"] = TableSize(10, "count")
    result = catalog_profile.TableResult(table="jobs")
    cx = catalog_profile._TableCx(job, "jobs", result, catalog_profile.TableHooks())

    async def run(sql: str, **_kw: Any) -> QueryResult:
        return QueryResult(columns=["v", "n"], rows=[["draft", 6], ["done", 4]], row_count=2, truncated=False,
                           elapsed_ms=1, sql=sql)
    cx.run = run  # type: ignore[method-assign]
    out = catalog_profile._TableOutcome()
    asyncio.run(catalog_profile._check_codes(cx, "jobs", "stage", out, result))
    [codes] = out.codes
    assert codes.declared == ["running"] and "列定义里另有 1 个取值" in codes.note


@pytest.mark.parametrize("col, ok", [
    ({"type": "VARCHAR2(20 CHAR)"}, True),
    ({"type": "VARCHAR2(200 CHAR)"}, False),
    ({"type": "CHAR(2)"}, True),
    ({"type": "ENUM('paid','refund')"}, True),
    ({"type": "SET('a','b')"}, True),
    ({"type": "order_status", "enum_values": ["a", "b"]}, True),
    ({"type": "NUMBER(10, 0)"}, True),
    ({"type": "NUMBER(10, 2)"}, False),
    ({"type": "TIMESTAMP(3)"}, False),
    ({"type": "INTERVAL DAY(2) TO SECOND(6)"}, False),
])
def test_code_type_accepts_enum_and_char_semantics(col, ok):
    assert catalog_profile._code_type_ok(col.get("type"), col.get("enum_values")) is ok


def test_enum_columns_are_code_candidates_whatever_their_name():
    """ENUM 的取值列表有限，直接算码值候选：pay_way 这种名字不像码值的列以前被漏掉。"""
    meta = {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "pay_way", "type": "ENUM('cash','card')",
                                                             "enum_values": ["cash", "card"]},
                        {"name": "pay_note", "type": "VARCHAR(20)"}],
            "primary_key": ["id"]}
    assert catalog_profile._code_columns(meta, {}) == ["pay_way"]


# ---------------------------------------------------------------- 探查：类型写法、取值列表、引号、索引首列


@pytest.mark.parametrize("dialect, col_type, text", [
    (mysql.dialect(), mysql.ENUM("paid", "refund"), "ENUM('paid','refund')"),
    (mysql.dialect(), mysql.TINYINT(display_width=1), "TINYINT(1)"),
    (oracle.dialect(), oracle.VARCHAR2(20), "VARCHAR2(20 CHAR)"),
    # Oracle 的 VARCHAR2 反射回来是通用的 VARCHAR（容器集成测试实测）：照数据字典写成 VARCHAR2
    (oracle.dialect(), VARCHAR(10), "VARCHAR2(10 CHAR)"),
    (oracle.dialect(), oracle.NUMBER(10, 0), "NUMBER(10, 0)"),
    (postgresql.dialect(), postgresql.ENUM("a", "b", name="order_status"), "order_status"),
    # PG 的域编译出来只是域名，名字里的词（set、UPDATE 里的 DATE）会让它被认错：记它底下的类型
    (postgresql.dialect(), postgresql.DOMAIN("settlement_amount", Numeric(12, 2)), "NUMERIC(12, 2)"),
    (postgresql.dialect(), postgresql.DOMAIN("update_day", postgresql.DOMAIN("biz_day", Date())), "DATE"),
    (sqlite.dialect(), sqlite.DATETIME(), "DATETIME"),
])
def test_reflected_type_is_compiled_for_the_dialect(dialect, col_type, text):
    assert introspect.type_text(col_type, dialect) == text


def test_type_that_cannot_compile_falls_back_to_str():
    from sqlalchemy.types import NullType

    assert introspect.type_text(NullType(), sqlite.dialect()) == "NULL"
    assert introspect.type_text(None, sqlite.dialect()) == ""


def test_column_entry_records_enum_values_and_oracle_quoted_names():
    ora = oracle.dialect()
    entry = introspect.column_entry({"name": quoted_name("level", quote=True), "type": oracle.VARCHAR2(10),
                                     "nullable": True, "comment": None}, ora)
    assert entry == {"name": "level", "type": "VARCHAR2(10 CHAR)", "nullable": True, "comment": None,
                     "quote_name": True}
    plain = introspect.column_entry({"name": "status", "type": mysql.ENUM("a", "b"), "nullable": False}, mysql.dialect())
    assert plain["enum_values"] == ["a", "b"] and "quote_name" not in plain
    members = introspect.column_entry({"name": "tags", "type": mysql.SET("x", "y")}, mysql.dialect())
    assert members["enum_values"] == ["x", "y"] and members["type"] == "SET('x','y')"


def test_domain_over_an_enum_keeps_the_enum_values():
    status = postgresql.ENUM("draft", "done", name="setup_status")
    entry = introspect.column_entry({"name": "stage", "type": postgresql.DOMAIN("stage_kind", status)},
                                    postgresql.dialect())
    assert entry["type"] == "setup_status" and entry["enum_values"] == ["draft", "done"]


def test_index_first_columns_are_recorded():
    indexes = [{"column_names": ["paid_at", "id"]}, {"column_names": [None]}, {"column_names": ["store_id"]},
               {"column_names": ["paid_at"]}]
    assert introspect.index_first_columns(indexes, ["id"]) == ["id", "paid_at", "store_id"]


# ---------------------------------------------------------------- IN 列表的字面量按被指向列的类型族


def test_key_literals_follow_the_parent_family():
    assert catalog_profile.key_literals([12, 7], "number", parent="text", child_type="INTEGER") == (
        ["'12'", "'7'"], 0, 0)
    # 父列是数、子列是文字：只拼整数写法；'A1' 按类型就对不上整数父键，记进 unmatchable（调用方算作没对上）
    assert catalog_profile.key_literals(["12", "A1", "-3", "A1 "], "text", parent="number",
                                        child_type="VARCHAR(10)") == (["12", "-3"], 1, 0)
    # 父列是时间的关系整条不核对（_check_relation 先跳过），这里不收
    with pytest.raises(ValueError):
        catalog_profile.key_literals([1, 2], "number", parent="time", child_type="INTEGER")
    # 父列类型不知道：照子表取回的值的类型写（以前的做法）
    assert catalog_profile.key_literals([3, "x"], None, parent=None, child_type=None) == (["3", "'x'"], 0, 0)


def test_key_literals_count_unsafe_values_but_not_duplicate_spellings():
    """不能安全拼进 SQL 的值记进 unsafe（备注写「类型不支持核对，未计入」）；'0012' 和 '12' 写成同一个字面量，只算一个，
    不能被当成「不支持核对」。"""
    found = catalog_profile.key_literals(["0012", "12", "a\\b", True], "text", parent="text", child_type="TEXT")
    assert found == (["'0012'", "'12'"], 0, 2)
    found = catalog_profile.key_literals(["0012", "12"], "text", parent="number", child_type="TEXT")
    assert found == (["12"], 0, 0)


_REL = {"id": "r1", "columns": ["customer_ref"], "to_table": "customers", "to_columns": ["id"], "source": "llm",
        "status": "proposed"}


def _relation_cx(values: list[Any], parent_type: str, matched: int):
    """orders.customer_ref（文字）→ customers.id（parent_type）的关系核对用的执行状态：取值查询回 values，父表的核对
    查询回 matched，唯一性计数回「有重复」。seen 记下发出的每条 SQL。"""
    settings = ProfileSettings(enabled=True)
    source = type("S", (), {"id": "s1", "name": "shop", "kind": "postgresql", "options": {}})()
    tables = {"orders": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "customer_ref", "type": "TEXT"}],
                         "primary_key": ["id"]},
              "customers": {"columns": [{"name": "id", "type": parent_type}], "primary_key": ["id"]}}
    job = catalog_profile.ProfileContext(source=source, settings=settings, dialect=SqlDialect("postgresql"),
                                         runner=None, tables=tables, masked=set())
    job.sizes.update({"orders": TableSize(130, "count"), "customers": TableSize(3, "count")})
    cx = catalog_profile._TableCx(job, "orders", catalog_profile.TableResult(table="orders"),
                                  catalog_profile.TableHooks())
    seen: list[str] = []

    async def run(sql: str, **_kw: Any) -> QueryResult:
        seen.append(sql)
        if "SELECT DISTINCT" in sql:
            rows = [[v] for v in values]
            return QueryResult(columns=["k"], rows=rows, row_count=len(rows), truncated=False, elapsed_ms=1, sql=sql,
                               column_types={"k": "text"})
        if " IN (" in sql:
            return _result(matched)
        return QueryResult(columns=["a", "b"], rows=[[130, len(values)]], row_count=1, truncated=False, elapsed_ms=1,
                           sql=sql)
    cx.run = run  # type: ignore[method-assign]
    return cx, seen


@pytest.mark.parametrize("values, parent_type, coverage, sample, matched", [
    # 九成是散客代号、只有 1 个整数写法对得上：以前分母里拿掉了对不上类型的，测成 100%
    (["WALKIN", "1"] + [f"GUEST{i}" for i in range(30)], "INTEGER", round(1 / 32, 4), 32, 1),
    # 全都不是整数写法：一条 IN 都不用发，覆盖率 0%，照样是「可能不是这条关系」，不是「无法核对」
    (["WALKIN", "GUEST1"], "INTEGER", 0.0, 2, 0),
    (["1", "2"], "INTEGER", 1.0, 2, 2),
])
def test_relation_coverage_counts_type_mismatches_as_unmatched(values, parent_type, coverage, sample, matched):
    cx, seen = _relation_cx(values, parent_type, matched)
    out, result = catalog_profile._TableOutcome(), catalog_profile.TableResult(table="orders")
    asyncio.run(catalog_profile._check_relation(cx, "orders", _REL, out, result))
    [rel] = out.relations
    assert (rel.coverage, rel.sample, rel.matched, rel.holds) == (coverage, sample, matched, coverage == 1.0)
    if coverage < 1:
        assert "可能不是这条关系" in rel.note and "未计入" not in rel.note
    if sample > matched and matched == 0:
        assert not any("IN (" in sql for sql in seen), "没有整数写法的键值：不发父表的核对查询"


def test_time_keys_are_not_checked_and_send_no_query():
    cx, seen = _relation_cx(["2026-09-01"], "DATE", 0)
    out, result = catalog_profile._TableOutcome(), catalog_profile.TableResult(table="orders")
    with pytest.raises(catalog_profile._Skip) as skipped:
        asyncio.run(catalog_profile._check_relation(cx, "orders", _REL, out, result))
    assert skipped.value.reason == "unsupported" and "日期时间" in str(skipped.value)
    assert "两端类型不同" not in str(skipped.value)
    assert seen == [], "时间键不核对，一条查询都不发"


def test_char_keys_lose_their_padding():
    """PG、Oracle 的 CHAR(n) 取回来带尾随空格：拼成 'A ' 去和 VARCHAR 父列比就对不上。"""
    lits = catalog_profile.key_literals(["A ", "BC"], "text", parent="text", child_type="CHAR(2)").literals
    assert lits == ["'A'", "'BC'"]
    lits = catalog_profile.key_literals(["A "], "text", parent="text", child_type="VARCHAR(2)").literals
    assert lits == ["'A '"]


# ---------------------------------------------------------------- Oracle 小写带引号的对象


def test_oracle_quote_name_forces_quotes_and_keeps_case():
    ora = SqlDialect("oracle")
    meta = {"schema": "sales", "quote_name": True,
            "columns": [{"name": "level", "type": "VARCHAR2(10 CHAR)", "quote_name": True},
                        {"name": "amount", "type": "NUMBER(10, 2)"}]}
    assert ora.table(meta, "visits") == 'sales."visits"'
    assert ora.column(meta, "level") == '"level"' and ora.column(meta, "amount") == "amount"
    stats = ora.stats_sql(meta, "visits")
    assert "TABLE_NAME = 'visits'" in stats and "OWNER = 'SALES'" in stats
    # 没有 quote_name 的照旧：不加引号，Oracle 自己转大写
    assert ora.table({"schema": None}, "visits") == "visits"
    assert "TABLE_NAME = 'VISITS'" in ora.stats_sql({}, "visits")


# ---------------------------------------------------------------- 统计信息过期：再数一次确认


def _result(value: Any) -> QueryResult:
    return QueryResult(columns=["n"], rows=[[value]], row_count=1, truncated=False, elapsed_ms=1, sql="")


def _runner(answers: list[Any], seen: list[str]):
    async def run(sql: str, **_kw: Any) -> QueryResult:
        seen.append(sql)
        return _result(answers.pop(0))
    return run


@pytest.mark.parametrize("stats, counted, expected", [
    (500, 500, TableSize(500, "count")),                       # 统计信息说小、数过也小：准数
    (500, 100_001, TableSize(None, "count", at_least=100_001)),  # 统计信息过期：其实是大表
    (5_000_000, None, TableSize(5_000_000, "stats")),          # 统计信息说大：不必再数
])
def test_small_stats_are_confirmed_with_a_bounded_count(stats, counted, expected):
    seen: list[str] = []
    answers = [stats] + ([counted] if counted is not None else [])
    size, skips = asyncio.run(catalog_profile.estimate_size(_runner(answers, seen), SqlDialect("postgresql"),
                                                            {"schema": None}, "visits", max_scan_rows=100_000))
    assert size == expected and skips == []
    assert len(seen) == (2 if counted is not None else 1)
    if counted is not None:
        assert "LIMIT 100001" in seen[1]


# ---------------------------------------------------------------- 日期列：排除零日期和哨兵日期


@pytest.mark.parametrize("kind", DIALECTS)
def test_min_max_excludes_invalid_dates_and_counts_them(kind):
    d = SqlDialect(kind)
    sql = d.min_max_sql("t", [("paid_at", False), ("biz_day", True)])
    # 日期类型的列按日期字面量比，文字存的日期按字符串比（PG 上 varchar 和 date 比较直接报错）
    lo, hi = d.date_literal("0001-01-02"), d.date_literal("9999-12-31")
    assert f"paid_at >= {lo}" in sql and f"paid_at < {hi}" in sql
    assert "biz_day >= '0001-01-02'" in sql and "biz_day < '9999-12-31'" in sql
    assert sql.count("SUM(CASE WHEN") == 2


def _dates_cx(row: list[Any], kind: str = "mysql"):
    settings = ProfileSettings(enabled=True)
    source = type("S", (), {"id": "s1", "name": "shop", "kind": kind, "options": {}})()
    job = catalog_profile.ProfileContext(source=source, settings=settings, dialect=SqlDialect(kind),
                                         runner=None, tables={"orders": {"columns": [
                                             {"name": "paid_at", "type": "DATETIME"}]}}, masked=set())
    job.sizes["orders"] = TableSize(10, "count")
    result = catalog_profile.TableResult(table="orders")
    cx = catalog_profile._TableCx(job, "orders", result, catalog_profile.TableHooks())

    async def run(sql: str, **_kw: Any) -> QueryResult:
        return QueryResult(columns=["a", "b", "c"], rows=[row], row_count=1, truncated=False, elapsed_ms=1, sql=sql)
    cx.run = run  # type: ignore[method-assign]
    return cx, result


def test_dates_note_says_how_many_invalid_values_were_left_out():
    cx, result = _dates_cx(["2026-01-03 08:00:00", "2026-09-30 21:00:00", 4])
    out = catalog_profile._TableOutcome()
    asyncio.run(catalog_profile._check_dates(cx, "orders", ["paid_at"], 1, out, result))
    assert result.date_ranges == [{"column": "paid_at", "min": "2026-01-03 08:00:00", "max": "2026-09-30 21:00:00",
                                   "invalid": 4}]
    assert "另有 4 个无效日期" in out.business_date.note and "0000-00-00" in out.business_date.note


def test_only_invalid_dates_is_no_data():
    cx, result = _dates_cx([None, None, 9])
    with pytest.raises(catalog_profile._Skip) as e:
        asyncio.run(catalog_profile._check_dates(cx, "orders", ["paid_at"], 1, catalog_profile._TableOutcome(),
                                                 result))
    assert e.value.reason == "no_data" and "9 个无效日期" in e.value.detail


# ---------------------------------------------------------------- 取值观察要用的方言方法


@pytest.mark.parametrize("kind, literal, day, sample", [
    ("mysql", "DATE '2026-09-01'", "DATE(paid_at)", None),
    ("mariadb", "DATE '2026-09-01'", "DATE(paid_at)", None),
    ("postgresql", "DATE '2026-09-01'", "CAST(paid_at AS DATE)", "orders TABLESAMPLE SYSTEM (1.5)"),
    ("oracle", "DATE '2026-09-01'", "TRUNC(paid_at)", "orders SAMPLE BLOCK (1.5)"),
    ("sqlite", "'2026-09-01'", "substr(paid_at, 1, 10)", None),
])
def test_observation_dialect_methods(kind, literal, day, sample):
    d = SqlDialect(kind)
    assert d.date_literal(_dt.date(2026, 9, 1)) == literal == d.date_literal("2026-09-01")
    assert d.date_literal("2026-09-01", text=True) == "'2026-09-01'"
    assert d.day_trunc("paid_at") == day
    assert d.sample_from("orders", 1.5) == sample


@pytest.mark.parametrize("bad", ["2026-9-1", "2026-09-01'; --", "0000-00-00", 20260901, None])
def test_date_literal_refuses_anything_but_a_real_date(bad):
    with pytest.raises(ValueError):
        SqlDialect("postgresql").date_literal(bad)


@pytest.mark.parametrize("bad", [0, -1, 100, 101, float("nan")])
def test_sample_percent_must_be_between_0_and_100(bad):
    with pytest.raises(ValueError):
        SqlDialect("postgresql").sample_from("orders", bad)


# ---------------------------------------------------------------- 说明文字


def test_notes_say_storage_order_not_sampling():
    note = catalog_profile._relation_note(sample=2000, matched=2000, coverage=1.0, scan_cap=100_000, skipped_values=0,
                                          unmatchable=0, parent=(True, "主键"), child=(False, "count"), holds=True,
                                          collation=None)
    assert "按存储顺序只看前 100000 行" in note and "抽样" not in note
    assert catalog_profile.FILTER_SAMPLED.format(rows="100", share="1%").startswith("剖析核对：按存储顺序取前 100 行统计")


def test_mysql_notes_mention_the_collation():
    note = catalog_profile._relation_note(sample=10, matched=10, coverage=1.0, scan_cap=None, skipped_values=0,
                                          unmatchable=0, parent=(True, "主键"), child=(False, "count"), holds=True,
                                          collation=catalog_profile.MYSQL_COLLATION_NOTE)
    assert "排序规则" in note and "不分大小写" in note
