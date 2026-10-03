"""数据剖析（阶段 2A）里不连库的部分：设置的解析与校验、SQL 字面量、各方言的剖析 SQL 都过得了守卫、
目录对剖析结果的容纳（待填含义的码值、撤销审阅回到剖析的结论）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data import catalog_profile, guard
from app.data.catalog_profile import PROFILE_OPTION, ProfileSettings, profile_settings, profile_settings_problem
from app.data.engine import build_url

# ---------------------------------------------------------------- 设置


def test_settings_default_off_and_parse_leniently():
    s = profile_settings({})
    assert s == ProfileSettings()
    assert s.enabled is False and s.max_queries == 60 and s.query_timeout_s == 10 and s.sample_size == 2000
    assert s.max_scan_rows == 100_000 and s.max_total_s == 120
    on = profile_settings({PROFILE_OPTION: {"enabled": True, "max_queries": "30", "sample_size": 500}})
    assert on.enabled is True and on.max_queries == 30 and on.sample_size == 500
    # 库里存着的值不对（老数据、手改过）：那一项用缺省，不因此开启，也不报错
    odd = profile_settings({PROFILE_OPTION: {"enabled": "yes", "max_queries": 0, "query_timeout_s": "x"}})
    assert odd.enabled is False and odd.max_queries == 60 and odd.query_timeout_s == 10
    assert profile_settings({PROFILE_OPTION: "on"}) == ProfileSettings()


@pytest.mark.parametrize("value, label", [
    ({"max_queries": 0}, "查询次数上限"),
    ({"max_queries": 501}, "查询次数上限"),
    ({"max_queries": 2.5}, "查询次数上限"),
    ({"query_timeout_s": 61}, "单条查询时限"),
    ({"sample_size": 5}, "抽样键值数"),
    ({"max_scan_rows": -1}, "整表统计行数上限"),
    ({"max_total_s": 5}, "总时长上限"),
    ({"enabled": "true"}, "开启数据剖析"),
    ({"max_queries": True}, "查询次数上限"),
])
def test_settings_problem_names_the_field_in_chinese(value, label):
    problem = profile_settings_problem({PROFILE_OPTION: value})
    assert problem and label in problem and "数据剖析" in problem


def test_settings_problem_rejects_unknown_keys_and_wrong_shape():
    assert "无法识别" in profile_settings_problem({PROFILE_OPTION: {"enabled": True, "rows": 1}})
    assert profile_settings_problem({PROFILE_OPTION: ["enabled"]})
    assert profile_settings_problem({}) is None
    assert profile_settings_problem({PROFILE_OPTION: None}) is None
    assert profile_settings_problem({PROFILE_OPTION: {"enabled": True, "max_queries": 500, "query_timeout_s": 1.5,
                                                      "sample_size": 10, "max_scan_rows": 0,
                                                      "max_total_s": 600}}) is None


def test_profile_option_never_reaches_the_connection_url():
    """剖析设置是 AgentLab 自己的配置：拼进连接串，驱动会把它当未知参数拒掉。"""
    source = SimpleNamespace(kind="mysql", host="db.example", port=3306, database="shop", username="ro",
                             password=None, options={"charset": "utf8mb4", PROFILE_OPTION: {"enabled": True}})
    url = build_url(source)
    assert "charset=utf8mb4" in url and PROFILE_OPTION not in url and "enabled" not in url


# ---------------------------------------------------------------- 字面量


@pytest.mark.parametrize("value, kind, expected", [
    (42, "number", "42"),
    (-7, None, "-7"),
    (3.0, "number", "3"),
    ("12345678901234567890", "number", "12345678901234567890"),   # DECIMAL 经查询层转成了字符串
    ("A-01", "text", "'A-01'"),
    ("O'Neil", "text", "'O''Neil'"),
    ("12:30", "text", "'12:30'"),
])
def test_sql_literal_quotes_safe_values(value, kind, expected):
    assert catalog_profile.sql_literal(value, kind) == expected


@pytest.mark.parametrize("value, kind", [
    (True, "boolean"),                 # 布尔不当键
    (2.5, "number"),                   # 非整数的数不当键：浮点相等比较靠不住
    ("12.50", "number"),
    ("1e3", "number"),
    ("2026-07-01", "date"),            # 日期的字面量各家写法不同，不猜
    ("a\\b", "text"),                  # 反斜杠：MySQL 不同设置下读法不同，守卫会拒
    ("a :b", "text"),                  # 「:名字」会被查询层当成绑定参数
    (":b", "text"),
    ("a\nb", "text"),                  # 控制字符
    ("x" * 201, "text"),
    (None, "text"),
    ([1], None),
])
def test_sql_literal_refuses_unsafe_or_unsupported_values(value, kind):
    assert catalog_profile.sql_literal(value, kind) is None


# ---------------------------------------------------------------- 各方言的剖析 SQL


_META = {"visits": {"schema": None, "columns": [{"name": "memberTagId", "type": "INTEGER"}], "primary_key": ["id"]},
         "orders": {"schema": "sales", "columns": [{"name": "status", "type": "INTEGER"}], "primary_key": ["id"]}}


@pytest.mark.parametrize("kind", ["mysql", "mariadb", "postgresql", "oracle", "sqlite"])
def test_profile_sql_passes_readonly_guard_on_every_dialect(kind):
    """剖析的每一种查询（含读统计信息的系统视图）都得过只读守卫——守卫不为剖析放宽。"""
    d = catalog_profile.SqlDialect(kind)
    sqls = []
    for table in ("visits", "orders"):
        stats = d.stats_sql(_META[table], table)
        if kind == "sqlite":
            assert stats is None           # SQLite 没有可读的统计信息，改为数到上限
        else:
            assert stats
            sqls.append(stats)
        tbl = d.table(_META[table], table)
        col = d.quote(_META[table]["columns"][0]["name"])
        sqls += [
            d.bounded_count_sql(tbl, 100_001),
            d.distinct_sample_sql(tbl, col, 2000, scan_cap=None),
            d.distinct_sample_sql(tbl, col, 2000, scan_cap=100_000),
            d.match_count_sql(tbl, col, ["1", "'A'"]),
            d.unique_check_sql(tbl, col),
            d.value_counts_sql(tbl, col, 21, scan_cap=None),
            d.value_counts_sql(tbl, col, 21, scan_cap=100_000),
            d.min_max_sql(tbl, [col]),
        ]
    for sql in sqls:
        assert guard.check(sql, readonly=True, dialect=kind) == sql, sql


def test_dialect_quoting_and_row_limits():
    my, ora, pg = (catalog_profile.SqlDialect(k) for k in ("mysql", "oracle", "postgresql"))
    assert my.quote("memberTagId") == "`memberTagId`" and pg.quote("memberTagId") == '"memberTagId"'
    assert my.table(_META["orders"], "orders") == "sales.orders"
    assert pg.table({"schema": "Sales"}, "memberTags") == '"Sales"."memberTags"'
    # Oracle 没有 LIMIT（11g 也没有 FETCH FIRST）：一律用 ROWNUM
    sample = ora.distinct_sample_sql("visits", '"memberTagId"', 50, scan_cap=None)
    assert "LIMIT" not in sample and "ROWNUM <= 50" in sample
    assert "LIMIT 50" in pg.distinct_sample_sql("visits", "x", 50, scan_cap=None)
    # 统计信息按方言读各自的系统视图
    assert "information_schema.TABLES" in my.stats_sql(_META["visits"], "visits")
    assert "pg_class" in pg.stats_sql(_META["visits"], "visits")
    stats = ora.stats_sql(_META["orders"], "orders")
    assert "ALL_TABLES" in stats and "'ORDERS'" in stats and "'SALES'" in stats
