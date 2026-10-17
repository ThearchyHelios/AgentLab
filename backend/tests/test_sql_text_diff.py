"""explain_sql_diff 的 text_diff（「规范化后不同的行」）：两边相同的条件不因为换行的位置不同被标成差异，不同的行看得出
在哪个子句里。

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

复核（第 2 轮）：ON 单独占一行时，只差一个关联条件的那一栏只剩一行「AND 条件」，看不出是哪个 JOIN 的关联条件、还是
筛选条件；两边 JOIN 的表不同时，两边都有的那一行「ON」被当成相同的行吃掉，条件前面没了 ON。现在 ON 挂在 JOIN 那一行
的末尾（条件缩在下面，比 WHERE 的条件深一级）；不同的行前面带上它的各级上级行——「  」开头、两边相同、只为标出不同的
行在哪个子句里（哪个 WHERE、哪个 JOIN 的 ON、哪层子查询），审批卡上淡色显示。

表名、列名、日期、取值都是虚构的。
"""
from __future__ import annotations

from app.data.sql_semantics import explain_sql_diff

LABELS = ("原流程", "盲算")
#: 包裹表、仓库表、快递员表的结构（虚构）：给了表结构才认列，同验收时双盲复算节点的调用
SCHEMA = {"tables": {
    "parcels": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "fee", "type": "REAL"},
                            {"name": "sent_on", "type": "TEXT"}, {"name": "state", "type": "TEXT"},
                            {"name": "hub", "type": "TEXT"}, {"name": "depot_id", "type": "INTEGER"},
                            {"name": "courier_id", "type": "INTEGER"}]},
    "depots": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "zone", "type": "TEXT"},
                           {"name": "kind", "type": "TEXT"}]},
    "couriers": {"columns": [{"name": "id", "type": "INTEGER"}, {"name": "team", "type": "TEXT"},
                             {"name": "active", "type": "INTEGER"}]},
}}
WEEK = "sent_on BETWEEN '2031-03-03' AND '2031-03-09'"
#: 两个 LEFT JOIN，{d}、{c} 处各可再加一个关联条件
TWO_JOINS = ("SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id{d} "
             "LEFT JOIN couriers c ON c.id = p.courier_id{c}")


def diff(a: str, b: str):
    return explain_sql_diff(a, b, "sqlite", schema=SCHEMA, labels=LABELS)


def depth(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def test_the_same_date_range_is_not_marked_when_one_side_has_one_more_condition():
    """验收时的形状：原流程比盲算多一个排除作废的条件，两边的日期区间一样——只标多出来的那一个条件，前面带上它所在的
    WHERE（两边相同的上下文行）。"""
    a = f"SELECT SUM(fee) AS week_fee FROM parcels WHERE {WEEK} AND state <> 'void'"
    b = f"SELECT SUM(fee) AS total_fee FROM parcels WHERE {WEEK}"
    got = diff(a, b)
    assert got.text_diff == ("  WHERE", "-   AND parcels.state <> 'void'")
    # 只动排版：判定和差异说明照旧
    assert not got.equivalent and not got.fallback and got.level == "optimizer"
    assert [(i.aspect, i.text) for i in got.items] == [("filter", "原流程排除了 state = 'void' 的行和 state 为空的行，盲算没有")]
    # 反过来比也一样，只是换到「+ 」那一边
    assert diff(b, a).text_diff == ("  WHERE", "+   AND parcels.state <> 'void'")


def test_an_extra_condition_sorted_first_does_not_drag_the_shared_ones_along():
    """多出来的条件排在最前面：另一边同一个日期条件只差在前面有没有 AND，不算不同。"""
    a = f"SELECT SUM(fee) FROM parcels WHERE {WEEK} AND hub = 'north'"
    b = f"SELECT SUM(fee) FROM parcels WHERE {WEEK}"
    assert diff(a, b).text_diff == ("  WHERE", "-   parcels.hub = 'north'")
    assert diff(b, a).text_diff == ("  WHERE", "+   parcels.hub = 'north'")


def test_two_conditions_on_each_side_mark_only_the_one_that_differs():
    """同一列上的区间只差结束日期（差异说明说成 time_range，逐行对照照样给）：开始日期那一行两边一样，不标。"""
    a = f"SELECT SUM(fee) FROM parcels WHERE {WEEK}"
    b = "SELECT SUM(fee) FROM parcels WHERE sent_on BETWEEN '2031-03-03' AND '2031-03-10'"
    got = diff(a, b)
    assert not got.fallback and [i.aspect for i in got.items] == ["time_range"]
    assert got.text_diff == ("  WHERE", "-   AND parcels.sent_on <= '2031-03-09'", "+   AND parcels.sent_on <= '2031-03-10'")


def test_on_conditions_get_one_line_each_under_their_join():
    """LEFT JOIN 的 ON 挂在 JOIN 那一行的末尾，条件各占一行缩在下面；多出来的那个条件单独标出来，前面带上它所在的
    JOIN（以前 ON 单独一行，标出来的只剩「AND depots.kind = 'cold'」，看不出是关联条件还是筛选条件）。"""
    a = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id AND d.kind = 'cold'"
    b = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id"
    assert diff(a, b).text_diff == ("  LEFT JOIN depots AS depots ON", "-     AND depots.kind = 'cold'")
    # 关联条件本身不同：只标那一行，JOIN 那一行两边一样，作为上下文列出
    c = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.zone = p.hub"
    assert diff(b, c).text_diff == ("  LEFT JOIN depots AS depots ON", "-     depots.id = parcels.depot_id",
                                    "+     depots.zone = parcels.hub")


def test_two_left_joins_name_the_join_the_differing_condition_belongs_to():
    """两个 LEFT JOIN 只差其中一个的关联条件：标出来的条件前面带上的是它自己的那个 JOIN，不是另一个，也不是 WHERE。"""
    plain = TWO_JOINS.format(d="", c="")
    on_courier = TWO_JOINS.format(d="", c=" AND c.active = 1")
    assert diff(on_courier, plain).text_diff == ("  LEFT JOIN couriers AS couriers ON", "-     AND couriers.active = 1")
    on_depot = TWO_JOINS.format(d=" AND d.kind = 'cold'", c="")
    assert diff(plain, on_depot).text_diff == ("  LEFT JOIN depots AS depots ON", "+     AND depots.kind = 'cold'")


def test_a_different_join_table_keeps_on_with_each_side():
    """两边 LEFT JOIN 的表不同：每一栏都是带着 ON 的 JOIN 那一行、下面紧跟各自的关联条件（以前两边都有的那一行 ON 被
    当成相同的行吃掉，表名下面直接跟条件）。"""
    a = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id"
    b = "SELECT COUNT(*) FROM parcels p LEFT JOIN couriers c ON c.id = p.courier_id"
    assert diff(a, b).text_diff == ("- LEFT JOIN depots AS depots ON", "-     depots.id = parcels.depot_id",
                                    "+ LEFT JOIN couriers AS couriers ON", "+     couriers.id = parcels.courier_id")


def test_a_join_condition_never_floats_without_its_join():
    """关联的对象换了写法（原流程关联一个子查询，盲算关联两张表）：每个关联条件紧跟在它所在的、带着 ON 的那一行下面——
    子查询的右括号后面也带着 ON，不会出现条件上面是子查询的最后一行、或者上面是别的 JOIN 的情况。"""
    a = ("SELECT COUNT(*) FROM parcels p LEFT JOIN (SELECT depot_id AS k, SUM(fee) AS s FROM parcels GROUP BY depot_id) "
         "AS sub ON p.depot_id = sub.k")
    b = ("SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id "
         "LEFT JOIN couriers c ON c.id = p.courier_id")
    got = diff(a, b).text_diff
    assert [line for line in got if line.startswith("+ ")] == [
        "+ LEFT JOIN couriers AS couriers ON", "+     couriers.id = parcels.courier_id",
        "+ LEFT JOIN depots AS depots ON", "+     depots.id = parcels.depot_id"]
    original = [line for line in got if line.startswith("- ")]
    assert original[0] == "- LEFT JOIN (" and original[-2:] == ["- ) AS sub ON", "-     parcels.depot_id = sub.c1"], got
    # 每一行都只在一边，没有两边相同的上下文行，也没有单独一行的 ON
    assert len(original) + 4 == len(got) and not any(line.strip() in ("ON", "- ON", "+ ON") for line in got)


def test_the_same_condition_in_on_and_in_where_is_not_one_line():
    """同一个条件一边写在 LEFT JOIN 的 ON 里、一边写在 WHERE 里（意思不同：写在 ON 里不筛掉左表的行）：ON 里的条件比
    WHERE 的缩进深一级，不会被当成两边相同的行。"""
    a = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id AND p.hub = 'north'"
    b = "SELECT COUNT(*) FROM parcels p LEFT JOIN depots d ON d.id = p.depot_id WHERE p.hub = 'north'"
    assert diff(a, b).text_diff == ("  LEFT JOIN depots AS depots ON", "+ WHERE", "-     AND parcels.hub = 'north'",
                                    "+   parcels.hub = 'north'")


def test_a_top_level_line_carries_no_context():
    """不同的行本身顶格（FROM 换了表）：没有上级行，不带上下文。"""
    assert diff("SELECT COUNT(*) FROM parcels", "SELECT COUNT(*) FROM depots").text_diff == (
        "- FROM parcels AS parcels", "+ FROM depots AS depots")


def test_having_conditions_get_one_line_each_too():
    a = "SELECT hub, SUM(fee) FROM parcels GROUP BY hub HAVING SUM(fee) > 10 AND COUNT(*) > 2"
    b = "SELECT hub, SUM(fee) FROM parcels GROUP BY hub HAVING SUM(fee) > 10"
    assert diff(a, b).text_diff == ("  HAVING", "-   AND COUNT(*) > 2")


def test_conditions_inside_a_subquery_get_one_line_each_too():
    """子查询里的条件也是每个条件一行；上下文一层层带到顶格：外层的 WHERE、EXISTS 那一行、子查询的 WHERE。"""
    a = ("SELECT COUNT(*) FROM parcels p WHERE p.state <> 'void' "
         "AND EXISTS (SELECT 1 FROM depots d WHERE d.id = p.depot_id AND d.zone = 'east')")
    b = ("SELECT COUNT(*) FROM parcels p WHERE p.state <> 'void' "
         "AND EXISTS (SELECT 1 FROM depots d WHERE d.id = p.depot_id)")
    *context, line = diff(a, b).text_diff
    assert line.startswith("-       AND ") and line.endswith(".zone = 'east'")
    assert [c.strip() for c in context] == ["WHERE", "EXISTS(", "WHERE"] and all(c.startswith("  ") for c in context)
    assert [depth(c[2:]) for c in context] == [0, 2, 4]


def test_an_or_condition_reads_the_same_alone_and_among_others():
    """OR 连起来的条件和别的条件并列时要加括号，单独一个时以前不加：同一个条件两边写法不同，就被标成了差异。现在条件
    本身是 OR 的一律加括号，两边写法一样；加括号不改意思。"""
    either = "(hub = 'north' OR hub = 'south')"
    a = f"SELECT COUNT(*) FROM parcels WHERE {either} AND state <> 'void'"
    b = f"SELECT COUNT(*) FROM parcels WHERE {either}"
    assert diff(a, b).text_diff == ("  WHERE", "-   parcels.state <> 'void'")
    c = "SELECT COUNT(*) FROM parcels WHERE (hub = 'north' OR hub = 'west') AND state <> 'void'"
    assert diff(a, c).text_diff == ("  WHERE", "-   AND (parcels.hub = 'north' OR parcels.hub = 'south')",
                                    "+   AND (parcels.hub = 'north' OR parcels.hub = 'west')")


def test_context_cut_off_by_the_forty_line_cap_is_dropped():
    """截到 40 行时，最后剩下的上级行后面的不同行被截掉了：这种上级行不留，最后一行总是不同的行。"""
    cols = 19
    a = ", ".join(f"SUM(fee * {i}) AS c{i}" for i in range(cols))
    b = ", ".join(f"SUM(fee * {i + 100}) AS c{i}" for i in range(cols))
    got = diff(f"SELECT {a} FROM parcels WHERE fee > 0 AND state <> 'void'",
               f"SELECT {b} FROM parcels WHERE fee > 0").text_diff
    # 上级行 SELECT、两边各 19 行的选择列共 39 行；第 40 行本该是 WHERE，它下面那一行被截掉了
    assert got[0] == "  SELECT" and len(got) == 1 + 2 * cols
    assert got[-1][:2] in ("- ", "+ ") and "  WHERE" not in got
