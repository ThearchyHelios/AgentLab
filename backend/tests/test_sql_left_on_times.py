"""日期条件写在 LEFT JOIN 的 ON 里时的差异说明（data/sql_semantics.py 的 _Explainer._left_on_times）。

以前两边来源一样、只差在一个 LEFT JOIN 的 ON 里的日期条件时，整段退回「两条 SQL 的写法不同，未能归纳成具体差异」。现在
说得准的照实说（aspect 是 join：说的是关联条件，不会被当成业务日期写进数据目录），说不准的照实兜底。只改说明：判等价的
结论一个不变；说出来的不同在虚构库上确实让结果不一样。
"""
from __future__ import annotations

import pytest

from app.data.sql_semantics import explain_sql_diff
from tests.fixtures import sql_pairs

SCHEMA = sql_pairs.schema_cache()
BASE = "SELECT u.city, SUM(o.amount) AS s FROM users u LEFT JOIN orders o ON o.user_id = u.id{on} GROUP BY u.city"

SAID = [
    # 一边多一个日期条件
    (BASE.format(on=" AND o.paid_at >= '2026-09-10'"), BASE.format(on=""),
     "这次用 LEFT JOIN 关联 orders 表时只关联 orders.paid_at >= '2026-09-10' 的行，口径没有这个条件"),
    (BASE.format(on=""), BASE.format(on=" AND o.created_at < '2026-09-20'"),
     "口径用 LEFT JOIN 关联 orders 表时只关联 orders.created_at < '2026-09-20' 的行，这次没有这个条件"),
    # 按不同的时间列筛
    (BASE.format(on=" AND o.paid_at >= '2026-09-10'"), BASE.format(on=" AND o.created_at >= '2026-09-10'"),
     "这次用 LEFT JOIN 关联 orders 表时按 paid_at 筛时间，口径按 created_at"),
    # 同一列、范围不同
    (BASE.format(on=" AND o.paid_at >= '2026-09-10' AND o.paid_at < '2026-10-01'"),
     BASE.format(on=" AND o.paid_at >= '2026-09-01' AND o.paid_at < '2026-10-01'"),
     "这次用 LEFT JOIN 关联 orders 表时对 paid_at 的条件是 orders.paid_at >= '2026-09-10' AND orders.paid_at < "
     "'2026-10-01'，口径是 orders.paid_at >= '2026-09-01' AND orders.paid_at < '2026-10-01'"),
]


@pytest.fixture(scope="module")
def databases():
    conns = sql_pairs.build_databases()
    yield conns
    for c in conns:
        c.close()


@pytest.mark.parametrize("a, b, text", SAID, ids=["extra-on-ours", "extra-on-caliber", "other-column", "range"])
@pytest.mark.parametrize("schema", [None, SCHEMA], ids=["bare", "schema"])
def test_date_conditions_in_a_left_join_on_are_said(a, b, text, schema, databases):
    diff = explain_sql_diff(a, b, "sqlite", schema=schema, labels=("这次", "口径"))
    assert diff.equivalent is False and diff.fallback is False
    assert [(i.aspect, i.text) for i in diff.items] == [("join", text)]
    # 说出来的不同是真的：在虚构库的某组数据上两条结果确实不一样
    assert any(sql_pairs.run(c, a) != sql_pairs.run(c, b) for c in databases)


@pytest.mark.parametrize("a, b", [
    # 套着日期函数，按取值认不准两边是不是一样：照实兜底
    (BASE.format(on=" AND DATE(o.paid_at) >= '2026-09-10' AND DATE(o.paid_at) <= '2026-09-30'"),
     BASE.format(on=" AND o.paid_at >= '2026-09-10' AND o.paid_at < '2026-10-01'")),
    # 可空一侧在 WHERE 里用到了（反连接）：说的就不是时间窗口了
    ("SELECT u.city, COUNT(*) AS n FROM users u LEFT JOIN orders o ON o.user_id = u.id AND o.paid_at >= '2026-09-10' "
     "WHERE o.id IS NULL GROUP BY u.city",
     "SELECT u.city, COUNT(*) AS n FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.id IS NULL "
     "GROUP BY u.city"),
    # 结果里没用到可空一侧的列：ON 里的不同只影响行会不会重复，说不准
    ("SELECT u.city, COUNT(*) AS n FROM users u LEFT JOIN orders o ON o.user_id = u.id AND o.paid_at >= '2026-09-10' "
     "GROUP BY u.city",
     "SELECT u.city, COUNT(*) AS n FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.city"),
    # ON 里多出来的不是时间条件
    (BASE.format(on=" AND o.status = 'paid'"), BASE.format(on="")),
], ids=["date-function", "anti-join", "not-in-output", "not-time"])
def test_what_cannot_be_said_precisely_still_gets_the_fallback(a, b):
    diff = explain_sql_diff(a, b, "sqlite", labels=("这次", "口径"))
    assert diff.equivalent is False and diff.fallback is True
    assert not [i for i in diff.items if i.aspect == "join"]


@pytest.mark.parametrize("a, b, same", [
    (BASE.format(on=" AND o.paid_at >= '2026-09-10'"), BASE.format(on=" AND o.paid_at >= '2026-09-10'"), True),
    ("SELECT u.city, SUM(o.amount) AS s FROM users u LEFT JOIN orders o ON o.paid_at >= '2026-09-10' AND "
     "o.user_id = u.id GROUP BY u.city", BASE.format(on=" AND o.paid_at >= '2026-09-10'"), True),
    *[(a, b, False) for a, b, _ in SAID],
])
def test_only_the_explanation_changed_not_the_verdict(a, b, same):
    for schema in (None, SCHEMA):
        assert explain_sql_diff(a, b, "sqlite", schema=schema).equivalent is same
