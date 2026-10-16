"""口径模板的参数绑定与等价证明（data/sql_semantics.py 的 bind_template / match_template，next-round 02 做法 1）。

绑定只是启发：按位置配出参数之后，等价与否只由 explain_sql_diff(执行的 SQL, 按绑定渲染的口径 SQL) 判。这里守三件事：
- 认得出：槽位在引号里、裸写、在半截字面量里、带 trim / compact，同一个槽位出现几处；别名不同、条件换序、BETWEEN 改写；
- 不错给信任：写死的码值不同（'paid' 对 'refunded'）、IN 列表不同、少一个条件、同一个槽位取到两个值，一律不等价；
- 说得清：不可比的（vars.、nodes.、file_text、参数写在名字里、别的过滤器）照实给原因；等价时按位置给列映射。

执行比对的守门（判等价的在虚构库上结果必须一样）在 tests/test_sql_equivalence.py 的「口径模板」一节。库和表结构用
tests/fixtures/sql_pairs.py 的虚构店铺库。
"""
from __future__ import annotations

import pytest

from app.data.sql_semantics import bind_template, explain_sql_diff, match_template, render_bound
from tests.fixtures import sql_pairs

SCHEMA = sql_pairs.schema_cache()

WEEKLY = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
          "AND paid_at >= '{{ input.start }}' AND paid_at < '{{ input.end }}'")


def _sound(m, sql: str) -> None:
    """判了等价的，按绑定渲染的口径 SQL 交给 explain_sql_diff 一定也判等价（结论只能来自它）。"""
    if m.equivalent:
        assert m.rendered is not None
        assert explain_sql_diff(sql, m.rendered, "sqlite").equivalent or \
            explain_sql_diff(sql, m.rendered, "sqlite", schema=SCHEMA).equivalent


# --------------------------------------------------------------------------
# 认得出
# --------------------------------------------------------------------------


@pytest.mark.parametrize("schema", [None, SCHEMA], ids=["bare", "schema"])
def test_quoted_slots_bind_and_prove(schema):
    sql = ("select sum(o.amount) as 销售额 from orders o where o.paid_at >= '2026-09-01' "
           "and o.status = 'paid' and o.paid_at < '2026-10-01'")
    m = match_template(WEEKLY, None, sql, "sqlite", schema=schema)
    assert m.equivalent and m.comparable
    assert m.binding == {"start": "2026-09-01", "end": "2026-10-01"}
    assert m.unbound == ()
    assert m.level in ("optimizer", "light", "parsed", "heuristic")
    # 外层别名不计、按位置对应
    assert m.columns is not None and [(c["ours"], c["caliber"]) for c in m.columns] == [("销售额", "gmv")]
    _sound(m, sql)


def test_bind_template_gives_the_binding_without_proving():
    sql = "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '2026-08-01' " \
          "AND paid_at < '2026-09-01'"
    b = bind_template(WEEKLY, None, sql, "sqlite")
    assert b.comparable and b.binding == {"start": "2026-08-01", "end": "2026-09-01"} and b.level == "optimizer"
    assert [s.context for s in b.slots] == ["quoted", "quoted"]


def test_bare_slot_binds_a_number():
    template = "SELECT COUNT(*) AS n FROM orders WHERE amount > {{ input.min }}"
    m = match_template(template, [{"name": "min", "type": "number"}], "SELECT count(*) AS n FROM orders "
                       "WHERE amount > 100", "sqlite")
    assert m.equivalent and m.binding == {"min": 100}


def test_half_literal_slot_binds_the_inner_part():
    template = "SELECT COUNT(*) AS n FROM orders WHERE created_at LIKE '{{ input.month }}-%' AND amount > {{ input.min }}"
    sql = "SELECT COUNT(*) AS n FROM orders WHERE amount > 10 AND created_at LIKE '2026-09-%'"
    m = match_template(template, None, sql, "sqlite")
    assert m.equivalent and m.binding == {"month": "2026-09", "min": 10}
    _sound(m, sql)


def test_slot_with_trim_and_compact_filters():
    template = ("SELECT COUNT(*) AS n FROM orders WHERE region = '{{ input.region | trim }}' "
                "AND amount >= {{ input.floor | compact }}")
    sql = "SELECT COUNT(*) AS n FROM orders WHERE region = 'east' AND amount >= 12.5"
    m = match_template(template, None, sql, "sqlite")
    assert m.equivalent and m.binding == {"region": "east", "floor": 12.5}


def test_same_slot_in_several_places_takes_one_value():
    template = ("SELECT COUNT(*) AS n FROM orders WHERE created_at >= '{{ input.day }}' "
                "AND paid_at >= '{{ input.day }}'")
    same = "SELECT COUNT(*) AS n FROM orders WHERE created_at >= '2026-09-01' AND paid_at >= '2026-09-01'"
    m = match_template(template, None, same, "sqlite")
    assert m.equivalent and m.binding == {"day": "2026-09-01"}
    # 两处取到不同的值：没有哪一组参数渲染得出它，不等价
    different = "SELECT COUNT(*) AS n FROM orders WHERE created_at >= '2026-09-01' AND paid_at >= '2026-09-02'"
    m = match_template(template, None, different, "sqlite")
    assert not m.equivalent
    _sound(m, different)


@pytest.mark.parametrize("sql", [
    # 换别名、换表别名
    "SELECT SUM(x.amount) AS total FROM orders AS x WHERE x.status = 'paid' AND x.paid_at >= '2026-09-01' "
    "AND x.paid_at < '2026-10-01'",
    # 条件换序
    "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at < '2026-10-01' AND paid_at >= '2026-09-01' "
    "AND status = 'paid'",
    # 关键字大小写、空白
    "select   sum(amount)  gmv\nfrom orders\nwhere status='paid' and paid_at>='2026-09-01' and paid_at<'2026-10-01'",
], ids=["alias", "order", "spacing"])
def test_writing_differences_are_recognized(sql):
    m = match_template(WEEKLY, None, sql, "sqlite", schema=SCHEMA)
    assert m.equivalent, m.diff.as_dict() if m.diff else m
    assert m.binding == {"start": "2026-09-01", "end": "2026-10-01"}
    _sound(m, sql)


def test_between_rewrite_is_recognized():
    template = "SELECT COUNT(*) AS n FROM orders WHERE amount BETWEEN {{ input.low }} AND {{ input.high }}"
    sql = "SELECT COUNT(*) AS n FROM orders WHERE amount >= 10 AND amount <= 100"
    m = match_template(template, None, sql, "sqlite", schema=SCHEMA)
    assert m.equivalent and m.binding == {"low": 10, "high": 100}
    _sound(m, sql)


def test_conditions_sorted_differently_still_bind_by_heuristic():
    """规范化按字面量的值排 AND 的各项，哨兵和真实的值排出来的先后不同时结构那几级配不上，退回同列同运算符的配对；
    等价照旧只由 explain_sql_diff 判。"""
    template = "SELECT SUM(amount) AS s FROM orders WHERE region = '{{ input.region }}' AND status = 'paid'"
    sql = "SELECT SUM(amount) AS s FROM orders WHERE status = 'paid' AND region = 'west'"
    m = match_template(template, None, sql, "sqlite")
    assert m.equivalent and m.binding == {"region": "west"}
    _sound(m, sql)


def test_template_without_slots_is_compared_as_is():
    template = "SELECT COUNT(*) AS n FROM orders WHERE status = 'paid'"
    m = match_template(template, None, "select count(*) as n from orders where status = 'paid'", "sqlite")
    assert m.equivalent and m.binding == {} and m.unbound == ()


def test_unbound_slot_falls_back_to_the_default():
    """槽位只在注释里：取不到值，按入口字段的缺省值渲染（同入口节点）。渲染出来等价的照样等价。"""
    template = "SELECT COUNT(*) AS n FROM orders -- 门店 {{ input.store }}\nWHERE status = 'paid'"
    m = match_template(template, [{"name": "store", "default": "s1"}], "SELECT COUNT(*) AS n FROM orders "
                       "WHERE status = 'paid'", "sqlite")
    assert m.equivalent and m.unbound == ("store",)


def test_render_bound_uses_the_runtime_renderer_and_defaults():
    rendered = render_bound("SELECT * FROM orders WHERE region = '{{ input.r }}' AND amount > {{ input.m }}",
                            {"r": "east"}, [{"name": "m", "default": 5}])
    assert rendered == "SELECT * FROM orders WHERE region = 'east' AND amount > 5"


# --------------------------------------------------------------------------
# 不错给信任
# --------------------------------------------------------------------------


def test_hard_coded_code_values_differ():
    """status = 'paid' 和 'refunded' 的 normalize_sql 文本相同，绑定之后照样不等价：口径里写死的常量不是参数。"""
    sql = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'refunded' AND paid_at >= '2026-09-01' "
           "AND paid_at < '2026-10-01'")
    m = match_template(WEEKLY, None, sql, "sqlite", schema=SCHEMA)
    assert not m.equivalent and m.diff is not None and m.diff.items
    _sound(m, sql)


def test_in_list_differences_are_not_equivalent():
    template = "SELECT COUNT(*) AS n FROM orders WHERE status IN ('paid', 'done') AND region = '{{ input.r }}'"
    sql = "SELECT COUNT(*) AS n FROM orders WHERE status IN ('paid', 'void') AND region = 'east'"
    m = match_template(template, None, sql, "sqlite", schema=SCHEMA)
    assert not m.equivalent
    # 槽位在 IN 列表里：取到的值照样只能让渲染结果和执行的一样才算
    template = "SELECT COUNT(*) AS n FROM orders WHERE status IN ('paid', '{{ input.s }}')"
    assert match_template(template, None, "SELECT COUNT(*) AS n FROM orders WHERE status IN ('done', 'paid')",
                          "sqlite").equivalent
    assert not match_template(template, None, "SELECT COUNT(*) AS n FROM orders WHERE status IN ('done', 'void')",
                              "sqlite").equivalent


def test_missing_condition_says_where_it_differs():
    """少一个条件：不等价，差在哪写成「口径只保留 … 的行，这次没有这个条件」。参数仍按启发配出来（给对账预填）。"""
    sql = "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"
    m = match_template(WEEKLY, None, sql, "sqlite", schema=SCHEMA)
    assert not m.equivalent
    assert m.binding == {"start": "2026-09-01", "end": "2026-10-01"} and m.level == "heuristic"
    texts = [d.text for d in m.diff.items]
    assert any("口径只保留" in t and "这次没有这个条件" in t for t in texts), texts


def test_different_time_column_is_not_equivalent():
    sql = "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND created_at >= '2026-09-01' " \
          "AND created_at < '2026-10-01'"
    m = match_template(WEEKLY, None, sql, "sqlite", schema=SCHEMA)
    assert not m.equivalent and any(d.aspect == "time" for d in m.diff.items)


def test_a_different_query_shape_is_not_equivalent():
    m = match_template(WEEKLY, None, "SELECT COUNT(*) FROM orders", "sqlite")
    assert not m.equivalent and m.comparable


def test_unparseable_executed_sql_is_not_equivalent():
    m = match_template(WEEKLY, None, "SELEC broken", "sqlite")
    assert not m.equivalent and m.diff is not None and m.diff.items[0].aspect == "parse"


# --------------------------------------------------------------------------
# 不可比：照实给原因
# --------------------------------------------------------------------------


@pytest.mark.parametrize("template, word", [
    ("SELECT * FROM orders WHERE id = {{ vars.id }}", "运行时"),
    ("SELECT * FROM orders WHERE id = {{ nodes.q.rows }}", "运行时"),
    ("SELECT * FROM orders WHERE note = '{{ file_text(input.附件) }}'", "附件"),
    ("SELECT * FROM \"{{ input.table }}\"", "表名或列名"),
    ("SELECT * FROM orders WHERE region = '{{ input.r | upper }}'", "过滤器"),
])
def test_incomparable_templates_say_why(template, word):
    m = match_template(template, None, "SELECT * FROM orders WHERE id = 1", "sqlite")
    assert not m.equivalent and not m.comparable and m.diff is None
    assert word in (m.reason or "")
    b = bind_template(template, None, "SELECT * FROM orders WHERE id = 1", "sqlite")
    assert not b.comparable and word in (b.reason or "")


# --------------------------------------------------------------------------
# 列映射
# --------------------------------------------------------------------------


def test_columns_map_by_position_with_different_aliases():
    template = "SELECT region AS r, SUM(amount) AS gmv FROM orders WHERE status = '{{ input.s }}' GROUP BY region"
    sql = "SELECT region AS 区域, SUM(amount) AS 销售额 FROM orders WHERE status = 'paid' GROUP BY region"
    m = match_template(template, None, sql, "sqlite", columns=["区域", "销售额"])
    assert m.equivalent
    assert [(c["index"], c["ours"], c["caliber"]) for c in m.columns] == [(0, "区域", "r"), (1, "销售额", "gmv")]


def test_select_star_expands_by_the_schema():
    template = "SELECT * FROM stores WHERE region = '{{ input.r }}'"
    sql = "SELECT id, name, city, region, owner_id FROM stores WHERE region = 'east'"
    m = match_template(template, None, sql, "sqlite", schema=SCHEMA)
    assert m.equivalent
    assert [c["caliber"] for c in m.columns] == ["id", "name", "city", "region", "owner_id"]
    # 不给表结构认不出 * 有哪几列：等价照样判，列映射不给
    m = match_template(template, None, "SELECT * FROM stores WHERE region = 'east'", "sqlite")
    assert m.equivalent and m.columns is None


def test_reordered_columns_are_not_equivalent():
    """选择列的先后计：换了先后判不等价（列映射靠的就是这一点）。"""
    template = "SELECT region, SUM(amount) AS gmv FROM orders WHERE status = '{{ input.s }}' GROUP BY region"
    sql = "SELECT SUM(amount) AS gmv, region FROM orders WHERE status = 'paid' GROUP BY region"
    assert not match_template(template, None, sql, "sqlite", schema=SCHEMA).equivalent


def test_deadline_stops_trying_more_bindings():
    import time

    sql = "SELECT SUM(amount) AS gmv FROM orders WHERE paid_at >= '2026-09-01' AND paid_at < '2026-10-01'"
    m = match_template(WEEKLY, None, sql, "sqlite", deadline=time.monotonic() - 1)
    assert not m.equivalent and m.diff is not None


def test_a_slot_canonicalized_away_without_a_default_does_not_prove():
    """EXISTS 里的 SELECT 列表规范化时写成 SELECT 1：裸写在那里的槽位配不上值。没有缺省值时渲染出来是 SELECT  FROM …
    （口径实际不会执行的 SQL，sqlglot 照样解析得了），不能拿它证明等价；有缺省值的按缺省值渲染、照常证明。"""
    template = ("SELECT COUNT(*) AS n FROM orders o WHERE EXISTS (SELECT {{ input.one }} FROM refunds r "
                "WHERE r.order_id = o.id)")
    # 这次写的是 SELECT *：只有规范化的两级对得上，那两级里 EXISTS 的选择列表已经换成了 SELECT 1，配不出槽位的值
    sql = "SELECT COUNT(*) AS n FROM orders o WHERE EXISTS (SELECT * FROM refunds r WHERE r.order_id = o.id)"
    m = match_template(template, None, sql, "sqlite")
    assert not m.equivalent and m.diff is not None and m.diff.fallback
    m = match_template(template, [{"name": "one", "default": 1}], sql, "sqlite")
    assert m.equivalent
