"""剖析 SQL 的方言金样（下一轮 03 细案 C2）：没有真库时挡住方言写法的回退。

三层，各挡各的：

1. **金样**：每种方言（mysql、mariadb、postgresql、oracle、sqlite）× 每类查询（统计信息、有上限的计数、取不同键值、
   对键值、唯一性、取值分布、日期最小最大、时间窗口里的取值分布、按日行数、块采样），完整 SQL 钉在
   tests/fixtures/dialect/<方言>.sql，人审过一次；以后拼法有任何改动都会出 diff。要更新金样：
   ``UPDATE_DIALECT_GOLDEN=1 pytest tests/test_profile_dialect_golden.py``，再逐行看 git diff。
2. **sqlglot 逐条解析**：按方言读一遍（mariadb 按 mysql 读），挡住语法写坏了（少括号、关键字拼错）。
3. **IN 两边类型族一致**：用 sqlglot 的 qualify + annotate_types，配上表结构里的类型，核对 IN 列表的字面量和被指向
   列是同一类型族（PostgreSQL 上 varchar 父列配整数子列时，字面量必须带引号）。

挡不住的照实写：sqlglot 不知道真库的统计信息是不是过期、零日期驱动怎么返回、Oracle 小写对象是否存在、排序规则
怎么比——这些要靠 scripts/dialect-it.sh 在本机容器里跑真库（不进 CI）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify

from app.data import catalog, guard
from app.data.catalog_profile import IN_CHUNK, SqlDialect, key_literals

GOLDEN = Path(__file__).parent / "fixtures" / "dialect"
DIALECTS = ("mysql", "mariadb", "postgresql", "oracle", "sqlite")
#: sqlglot 的方言名
READ = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgres", "oracle": "oracle", "sqlite": "sqlite"}

#: 夹具表结构：一张带 schema 的订单表、一张门店表（编码是文字），Oracle 的一张小写带引号的表和一个小写带引号的列
META = {
    "orders": {"schema": "sales", "columns": [
        {"name": "id", "type": "INTEGER"}, {"name": "store_code", "type": "INTEGER"},
        {"name": "status", "type": "CHAR(2)"}, {"name": "paid_at", "type": "TIMESTAMP"},
        {"name": "biz_day", "type": "VARCHAR(10)"}, {"name": "level", "type": "INTEGER"}],
        "primary_key": ["id"]},
    "stores": {"schema": None, "columns": [{"name": "code", "type": "VARCHAR(10)"}], "primary_key": ["code"]},
    "visits": {"schema": None, "quote_name": True, "columns": [
        {"name": "id", "type": "INTEGER"}, {"name": "level", "type": "INTEGER", "quote_name": True}],
        "primary_key": ["id"]},
}


def _queries(kind: str) -> list[tuple[str, str | None]]:
    """一种方言的全部剖析、观察查询：(名字, SQL)。没有这种查询（SQLite 没有统计信息、MySQL 没有块采样）的 SQL 为 None。"""
    d = SqlDialect(kind)
    orders, stores, visits = d.table(META["orders"], "orders"), d.table(META["stores"], "stores"), \
        d.table(META["visits"], "visits")
    status, code = d.column(META["orders"], "status"), d.column(META["stores"], "code")
    level = d.column(META["visits"], "level")
    keys, _ = key_literals([12, 7], "number", parent="text", child_type="INTEGER")
    out: list[tuple[str, str | None]] = [
        ("stats", d.stats_sql(META["orders"], "orders")),
        ("stats_quoted", d.stats_sql(META["visits"], "visits")),
        ("bounded", d.bounded_count_sql(orders, 100_001)),
        ("distinct", d.distinct_sample_sql(orders, d.column(META["orders"], "store_code"), 2000, scan_cap=None)),
        ("distinct_capped", d.distinct_sample_sql(orders, d.column(META["orders"], "store_code"), 2000,
                                                  scan_cap=100_000)),
        ("match", d.match_count_sql(stores, code, keys)),
        ("unique", d.unique_check_sql(visits, level)),
        ("value_counts", d.value_counts_sql(orders, status, 21, scan_cap=None)),
        ("value_counts_capped", d.value_counts_sql(orders, status, 21, scan_cap=100_000)),
        ("min_max", d.min_max_sql(orders, [(d.column(META["orders"], "paid_at"), False),
                                           (d.column(META["orders"], "biz_day"), True)])),
        ("date_window", d.window_value_counts_sql(orders, status, d.column(META["orders"], "paid_at"),
                                                  "2026-08-27", 21)),
        ("day_volume", d.day_volume_sql(orders, d.column(META["orders"], "paid_at"), "2026-08-27")),
        ("sample", d.sample_value_counts_sql(orders, status, 1, 21)),
    ]
    return out


def _render(kind: str) -> str:
    lines = [f"-- 剖析与取值观察的 SQL 金样：{kind}（tests/test_profile_dialect_golden.py 生成，改动要人审）"]
    for name, sql in _queries(kind):
        lines.append(f"-- {name}")
        lines.append(f"{sql};" if sql is not None else "-- （这种方言没有这类查询）")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("kind", DIALECTS)
def test_profile_sql_matches_the_golden_file(kind):
    path = GOLDEN / f"{kind}.sql"
    text = _render(kind)
    if os.environ.get("UPDATE_DIALECT_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, f"{kind} 的剖析 SQL 和金样不一致：看 diff，确认无误后重新生成金样"


@pytest.mark.parametrize("kind", DIALECTS)
def test_every_profile_query_parses_and_passes_the_guard(kind):
    for name, sql in _queries(kind):
        if sql is None:
            continue
        parsed = sqlglot.parse(sql, read=READ[kind], error_level=sqlglot.ErrorLevel.RAISE)
        assert len(parsed) == 1 and parsed[0] is not None, (kind, name)
        assert guard.check(sql, readonly=True, dialect=kind) == sql, (kind, name)


@pytest.mark.parametrize("kind", ["postgresql", "oracle", "mysql"])
def test_observation_queries_use_the_right_day_and_sample_functions(kind):
    """按天截断和块采样各家写法不同，解析成语法树再看用的是哪个函数，不只比字符串。"""
    sql = dict(_queries(kind))
    day = sqlglot.parse_one(sql["day_volume"], read=READ[kind])
    if kind == "oracle":
        assert any(isinstance(n, (exp.DateTrunc, exp.TimestampTrunc, exp.Anonymous)) or n.key == "trunc"
                   for n in day.walk()), sql["day_volume"]
        assert "TRUNC(paid_at)" in sql["day_volume"]
    elif kind == "postgresql":
        assert any(isinstance(n, exp.Cast) for n in day.walk())
    else:
        assert "DATE(paid_at)" in sql["day_volume"]
    if kind == "mysql":
        assert sql["sample"] is None
    else:
        sampled = sqlglot.parse_one(sql["sample"], read=READ[kind])
        assert any(isinstance(n, exp.TableSample) for n in sampled.walk()), sql["sample"]


# ---------------------------------------------------------------- IN 两边的类型族


_FAMILY_OF = [(exp.DataType.TEXT_TYPES, "text"), (exp.DataType.NUMERIC_TYPES, "number"),
              (exp.DataType.TEMPORAL_TYPES, "time")]


def _sg_family(t: exp.DataType | None) -> str | None:
    if t is None:
        return None
    for group, name in _FAMILY_OF:
        if t.this in group:
            return name
    return None


def _in_families(sql: str, kind: str, schema: dict[str, dict[str, str]]) -> list[tuple[str | None, set[str | None]]]:
    """sql 里每个 IN：(被比较的列的类型族, 字面量的类型族集合)，按 annotate_types 配上表结构的类型推出来。"""
    tree = qualify(sqlglot.parse_one(sql, read=READ[kind]), schema=schema, dialect=READ[kind],
                   validate_qualify_columns=False)
    tree = annotate_types(tree, schema=schema, dialect=READ[kind])
    return [(_sg_family(node.this.type), {_sg_family(x.type) for x in node.expressions})
            for node in tree.find_all(exp.In)]


@pytest.mark.parametrize("kind", DIALECTS)
@pytest.mark.parametrize("parent_type, child_values, child_type", [
    ("VARCHAR(10)", [12, 7, 30], "INTEGER"),          # 父列文字、子列数：字面量要带引号
    ("INTEGER", ["12", "0007", "A1"], "VARCHAR(10)"),  # 父列数、子列文字：只收整数写法，写成数字
    ("VARCHAR(10)", ["A ", "BC"], "CHAR(2)"),          # 定长子列：去掉补齐的空格
    ("INTEGER", [1, 2], "INTEGER"),
])
def test_in_list_literals_have_the_parent_columns_family(kind, parent_type, child_values, child_type):
    d = SqlDialect(kind)
    parent = {"name": "code", "type": parent_type}
    literals, _ = key_literals(child_values, None, parent=catalog.column_family(parent), child_type=child_type)
    assert literals
    sql = d.match_count_sql("stores", d.quote("code"), literals[:IN_CHUNK])
    found = _in_families(sql, kind, {"stores": {"code": parent_type}})
    assert found, sql
    for column_family, literal_families in found:
        assert literal_families == {column_family}, (kind, sql, column_family, literal_families)


def test_the_old_way_would_mismatch_on_postgres():
    """对照：以前按子表取回的值写字面量，varchar 父列配整数子列在 sqlglot 推出的类型上就两边不同——PostgreSQL 真库上
    这是「operator does not exist: character varying = integer」。"""
    from app.data.catalog_profile import sql_literal

    old = [lit for v in (12, 7) if (lit := sql_literal(v, "number"))]
    sql = SqlDialect("postgresql").match_count_sql("stores", "code", old)
    [(column_family, literal_families)] = _in_families(sql, "postgresql", {"stores": {"code": "VARCHAR(10)"}})
    assert column_family == "text" and literal_families == {"number"}
