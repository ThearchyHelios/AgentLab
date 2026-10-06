"""explain_sql_diff 的 text_diff（「规范化后不同的行」）：两边相同的条件不因为换行的位置不同被标成差异。

2026-10 验收：双盲复算审批卡上，两边完全相同的日期区间被标成了差异——
    + orders.paid_date >= '2026-09-28' AND orders.paid_date <= '2026-10-04'
    - orders.paid_date >= '2026-09-28'
    - AND orders.paid_date <= '2026-10-04'
    - AND orders.status <> 'refunded'
sqlglot 的 pretty 按宽度决定换不换行：两个条件放得下就排在一行，三个就拆成三行，同一个条件在两边落在不同的行上。原文
SQL 都是单行时原文栏一行都不标，这一栏是唯一逐行指出差在哪的地方，审核人会以为统计期也不一样。

现在逐行比之前两边按同一套排版写：WHERE、ON、HAVING 的合取式每个条件一行（嵌在子查询里的也一样），条件本身是 OR
连起来的加上括号；比的时候不看条件前面的 AND（一边多出来的条件排在最前面时，另一边同一个条件只差在有没有 AND 上）。
只动排版：判不判等价、差异说明（items）不变，sql_semantics、sql_equivalence 原有的测试照样全过。

表名、列名、日期、取值都是虚构的。
"""
from __future__ import annotations

from app.data.sql_semantics import explain_sql_diff

LABELS = ("原流程", "盲算")
#: 包裹表、仓库表的结构（虚构）：给了表结构才认列，同验收时双盲复算节点的调用
SCHEMA = {"tables": {
    "parcels": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "fee", "type": "REAL"},
                            {"name": "sent_on", "type": "TEXT"}, {"name": "state", "type": "TEXT"},
                            {"name": "hub", "type": "TEXT"}, {"name": "depot_id", "type": "INTEGER"}]},
    "depots": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "zone", "type": "TEXT"},
                           {"name": "kind", "type": "TEXT"}]},
}}
WEEK = "sent_on BETWEEN '2031-03-03' AND '2031-03-09'"


def diff(a: str, b: str):
    return explain_sql_diff(a, b, "sqlite", schema=SCHEMA, labels=LABELS)


def test_the_same_date_range_is_not_marked_when_one_side_has_one_more_condition():
    """验收时的形状：原流程比盲算多一个排除作废的条件，两边的日期区间一样——只标多出来的那一个条件。"""
    a = f"SELECT SUM(fee) AS week_fee FROM parcels WHERE {WEEK} AND state <> 'void'"
    b = f"SELECT SUM(fee) AS total_fee FROM parcels WHERE {WEEK}"
    got = diff(a, b)
    assert got.text_diff == ("-   AND parcels.state <> 'void'",)
    # 只动排版：判定和差异说明照旧
    assert not got.equivalent and not got.fallback and got.level == "optimizer"
    assert [(i.aspect, i.text) for i in got.items] == [("filter", "原流程排除了 state = 'void' 的行和 state 为空的行，盲算没有")]
    # 反过来比也一样，只是换到「+ 」那一边
    assert diff(b, a).text_diff == ("+   AND parcels.state <> 'void'",)


def test_an_extra_condition_sorted_first_does_not_drag_the_shared_ones_along():
    """多出来的条件排在最前面：另一边同一个日期条件只差在前面有没有 AND，不算不同。"""
    a = f"SELECT SUM(fee) FROM parcels WHERE {WEEK} AND hub = 'north'"
    b = f"SELECT SUM(fee) FROM parcels WHERE {WEEK}"
    assert diff(a, b).text_diff == ("-   parcels.hub = 'north'",)
    assert diff(b, a).text_diff == ("+   parcels.hub = 'north'",)


def test_two_conditions_on_each_side_mark_only_the_one_that_differs():
    """同一列上的区间只差结束日期（这种只给兜底说明，审核人全靠逐行对照）：开始日期那一行两边一样，不标。"""
    a = f"SELECT SUM(fee) FROM parcels WHERE {WEEK}"
    b = "SELECT SUM(fee) FROM parcels WHERE sent_on BETWEEN '2031-03-03' AND '2031-03-10'"
    got = diff(a, b)
    assert got.fallback
    assert got.text_diff == ("-   AND parcels.sent_on <= '2031-03-09'", "+   AND parcels.sent_on <= '2031-03-10'")


def test_on_conditions_get_one_line_each_too():
    """LEFT JOIN 的 ON：ON 单独一行，条件各占一行，多出来的那个条件单独标出来。"""
    a = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id AND d.kind = 'cold'"
    b = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id"
    assert diff(a, b).text_diff == ("-     AND depots.kind = 'cold'",)
    # 关联条件本身不同：只标那一行，ON 那一行两边一样
    c = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.zone = p.hub"
    assert diff(b, c).text_diff == ("-     depots.id = parcels.depot_id", "+     depots.zone = parcels.hub")


def test_having_conditions_get_one_line_each_too():
    a = "SELECT hub, SUM(fee) FROM parcels GROUP BY hub HAVING SUM(fee) > 10 AND COUNT(*) > 2"
    b = "SELECT hub, SUM(fee) FROM parcels GROUP BY hub HAVING SUM(fee) > 10"
    assert diff(a, b).text_diff == ("-   AND COUNT(*) > 2",)


def test_conditions_inside_a_subquery_get_one_line_each_too():
    a = ("SELECT COUNT(*) FROM parcels p WHERE p.state <> 'void' "
         "AND EXISTS (SELECT 1 FROM depots d WHERE d.id = p.depot_id AND d.zone = 'east')")
    b = ("SELECT COUNT(*) FROM parcels p WHERE p.state <> 'void' "
         "AND EXISTS (SELECT 1 FROM depots d WHERE d.id = p.depot_id)")
    [line] = diff(a, b).text_diff
    assert line.startswith("-       AND ") and line.endswith(".zone = 'east'")


def test_an_or_condition_reads_the_same_alone_and_among_others():
    """OR 连起来的条件和别的条件并列时要加括号，单独一个时以前不加：同一个条件两边写法不同，就被标成了差异。现在条件
    本身是 OR 的一律加括号，两边写法一样；加括号不改意思。"""
    either = "(hub = 'north' OR hub = 'south')"
    a = f"SELECT COUNT(*) FROM parcels WHERE {either} AND state <> 'void'"
    b = f"SELECT COUNT(*) FROM parcels WHERE {either}"
    assert diff(a, b).text_diff == ("-   parcels.state <> 'void'",)
    c = "SELECT COUNT(*) FROM parcels WHERE (hub = 'north' OR hub = 'west') AND state <> 'void'"
    assert diff(a, c).text_diff == ("-   AND (parcels.hub = 'north' OR parcels.hub = 'south')",
                                    "+   AND (parcels.hub = 'north' OR parcels.hub = 'west')")
