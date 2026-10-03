"""合并查询的库外合并（engine/merge_query.py）：几次查询的完整快照落进内存 SQLite，再按一条 SELECT / WITH 合并。

纯函数，不连库、不读工件：快照在测试里现造。端到端（真运行、证据面板、推断来源）见 test_merge_node.py。

场景是虚构的门店经营：门店库按「日期 + 门店」聚合的销售，会员库按同样粒度聚合的到店人数。
"""
from __future__ import annotations

import pytest

from app.data.guard import QueryLimits
from app.engine.merge_query import (
    MERGE_SOURCE,
    MergeError,
    MergeInput,
    alias_problem,
    execute,
    sql_problem,
    trace_cell,
)
from app.engine.expressions import same_value

SALES_COLS = ["日期", "门店", "订单数", "销售额"]
SALES = [
    ["2026-05-01", "S01", 40, "3200.50"],
    ["2026-05-01", "S02", 25, "1875.00"],
    ["2026-05-02", "S01", 38, "3010.00"],
    ["2026-05-02", "S02", 30, "2400.00"],
]
VISITS_COLS = ["日期", "门店", "到店人数"]
VISITS = [
    ["2026-05-01", "S01", 160],
    ["2026-05-01", "S02", 90],
    ["2026-05-02", "S02", 100],
    ["2026-05-02", "S01", 150],
]
JOIN_SQL = ("SELECT s.日期, s.门店, s.订单数, s.销售额, v.到店人数 FROM s JOIN v ON s.日期 = v.日期 AND s.门店 = v.门店 "
            "ORDER BY s.日期, s.门店")


def snap(columns, rows, *, types=None, truncated=False, source="stores", **extra):
    return {"columns": list(columns), "rows": [list(r) for r in rows], "row_count": len(rows), "truncated": truncated,
            "sql": "SELECT …", "source": source, **({"column_types": types} if types is not None else {}), **extra}


def sales(**kw):
    return MergeInput(alias="s", node_id="q_sales", artifact="a" * 64,
                      snapshot=snap(SALES_COLS, SALES, types={"日期": "text", "门店": "text", "订单数": "number",
                                                              "销售额": "number"}, **kw))


def visits(rows=VISITS, *, types=None, **kw):
    return MergeInput(alias="v", node_id="q_visits", artifact="b" * 64,
                      snapshot=snap(VISITS_COLS, rows, types=types or {"日期": "text", "门店": "text", "到店人数": "number"},
                                    source="members", **kw))


# --------------------------------------------------------------------------
# 合并本身
# --------------------------------------------------------------------------


def test_join_on_date_and_store():
    out = execute([sales(), visits()], JOIN_SQL)
    assert out.columns == ["日期", "门店", "订单数", "销售额", "到店人数"]
    assert out.rows == [
        ["2026-05-01", "S01", 40, 3200.5, 160],
        ["2026-05-01", "S02", 25, 1875, 90],
        ["2026-05-02", "S01", 38, 3010, 150],
        ["2026-05-02", "S02", 30, 2400, 100],
    ]
    # 数值列按数存：数据源把 DECIMAL 落成的文本（"3200.50"）合并之后能直接参与运算
    assert out.column_types == {"日期": "text", "门店": "text", "订单数": "number", "销售额": "number",
                                "到店人数": "number"}
    assert out.truncated is False and out.warnings == []
    assert out.sql == JOIN_SQL


def test_with_is_allowed_and_text_keeps_leading_zeros():
    a = MergeInput(alias="a", node_id="n1", artifact="c" * 64,
                   snapshot=snap(["会员号", "积分"], [["007", 10], ["070", 20]], types={"会员号": "text", "积分": "number"}))
    out = execute([a], "WITH t AS (SELECT 会员号, 积分 FROM a) SELECT 会员号, 积分 * 2 AS 双倍 FROM t ORDER BY 会员号")
    assert out.rows == [["007", 20], ["070", 40]]


def test_column_names_are_checked_not_treated_as_strings():
    """内存库同样关掉「双引号当字符串」：写错的列名要报错，不能静默得出一列常量。"""
    with pytest.raises(MergeError, match="no such column"):
        execute([sales()], 'SELECT "销售颔" FROM s')


@pytest.mark.parametrize("sql", [
    "DELETE FROM s",
    "UPDATE s SET 订单数 = 0",
    "INSERT INTO s VALUES (1, 2, 3, 4)",
    "DROP TABLE s",
    "PRAGMA table_info(s)",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "WITH k AS (SELECT 1) DELETE FROM s",
    "EXPLAIN SELECT * FROM s",
])
def test_only_select_and_with(sql):
    assert sql_problem(sql) is not None
    with pytest.raises(MergeError):
        execute([sales()], sql)


def test_one_statement_only():
    problem = sql_problem("SELECT * FROM s; SELECT * FROM v")
    assert problem is not None and "一条" in problem
    with pytest.raises(MergeError):
        execute([sales(), visits()], "SELECT * FROM s; SELECT * FROM v")


def test_empty_sql():
    assert "合并 SQL" in sql_problem("  ")


def test_static_check_tolerates_templates():
    assert sql_problem("SELECT * FROM s WHERE 日期 >= {{ input.start | json }}") is None


@pytest.mark.parametrize("alias, ok", [
    ("s", True), ("sales_2026", True), ("_t", True),
    ("", False), ("1s", False), ("a-b", False), ("a b", False), ("订单", False),
    ("select", False), ("ORDER", False), ("join", False), ("sqlite_stat", False), ("x" * 40, False),
])
def test_alias_must_be_a_plain_table_name(alias, ok):
    assert (alias_problem(alias) is None) is ok


def test_aliases_differing_only_in_case_collide():
    a = MergeInput(alias="t", node_id="n1", artifact="c" * 64, snapshot=snap(["x"], [[1]]))
    b = MergeInput(alias="T", node_id="n2", artifact="d" * 64, snapshot=snap(["x"], [[2]]))
    with pytest.raises(MergeError, match="别名"):
        execute([a, b], "SELECT * FROM t")


def test_duplicate_column_names_in_an_input_are_refused_with_a_fix():
    a = MergeInput(alias="t", node_id="n1", artifact="c" * 64, snapshot=snap(["金额", "金额"], [[1, 2]]))
    with pytest.raises(MergeError, match="重名"):
        execute([a], "SELECT * FROM t")


# --------------------------------------------------------------------------
# 三道防护
# --------------------------------------------------------------------------


def test_a_truncated_input_is_refused():
    """被截断的输入只有前若干行：拿它按键合并，缺的行不会报错，只会悄悄少算。"""
    with pytest.raises(MergeError) as e:
        execute([sales(), visits(truncated=True)], JOIN_SQL)
    message = str(e.value)
    assert "「v」" in message and "截断" in message
    assert "聚合" in message and "缩小范围" in message


def test_key_type_mismatch_is_warned():
    """门店编号一边是文本 '001'，一边是数 1：SQLite 比较时会隐式转换，'001'、'01' 都等于 1。"""
    stores = MergeInput(alias="s", node_id="q_sales", artifact="a" * 64,
                        snapshot=snap(["门店", "销售额"], [["001", 100], ["002", 200]],
                                      types={"门店": "text", "销售额": "number"}))
    traffic = MergeInput(alias="v", node_id="q_visits", artifact="b" * 64,
                         snapshot=snap(["门店", "到店人数"], [[1, 10], [2, 20]], types={"门店": "number", "到店人数": "number"},
                                       source="members"))
    out = execute([stores, traffic], "SELECT s.门店, s.销售额, v.到店人数 FROM s JOIN v ON s.门店 = v.门店")
    [warning] = out.warnings
    assert warning["code"] == "key_type_mismatch"
    assert "s.门店" in warning["message"] and "v.门店" in warning["message"]
    assert "'001'" in warning["message"] and "CAST" in warning["message"]


def test_key_type_mismatch_is_found_through_using():
    stores = MergeInput(alias="s", node_id="n1", artifact="a" * 64,
                        snapshot=snap(["门店", "销售额"], [["1", 100]], types={"门店": "text", "销售额": "number"}))
    traffic = MergeInput(alias="v", node_id="n2", artifact="b" * 64,
                         snapshot=snap(["门店", "到店人数"], [[1, 10]], types={"门店": "number", "到店人数": "number"}))
    out = execute([stores, traffic], "SELECT 门店, 销售额, 到店人数 FROM s JOIN v USING (门店)")
    assert [w["code"] for w in out.warnings] == ["key_type_mismatch"]


def test_matching_key_types_raise_no_warning():
    out = execute([sales(), visits()], JOIN_SQL)
    assert out.warnings == []


def test_more_rows_than_the_largest_input_is_warned():
    """两边都有一天同一门店出现两行（没聚合到同一粒度）：多对多匹配，合并后行数被放大。"""
    stores = sales()
    stores.snapshot["rows"].append(["2026-05-01", "S01", 2, "100.00"])
    doubled = [*VISITS, ["2026-05-01", "S01", 5]]
    out = execute([stores, visits(doubled)], JOIN_SQL)
    assert len(out.rows) == 7
    grew = [w for w in out.warnings if w["code"] == "rows_grew"]
    assert len(grew) == 1
    message = grew[0]["message"]
    assert "7 行" in message and "5 行" in message and "不唯一" in message
    # 合并键认得出来时，说出是哪个输入、哪组键重复
    assert "「v」" in message and "2026-05-01" in message and "S01" in message


def test_one_to_many_is_not_warned_as_growth():
    """一边的键唯一时，结果行数不会超过另一边：不报。"""
    doubled = [*VISITS, ["2026-05-01", "S01", 5]]
    out = execute([sales(), visits(doubled)], JOIN_SQL)
    assert len(out.rows) == 5 and out.warnings == []


def test_result_over_the_row_limit_is_truncated_and_warned():
    out = execute([sales(), visits()], "SELECT * FROM s CROSS JOIN v", limits=QueryLimits(max_rows=5))
    assert out.truncated is True and len(out.rows) == 5
    assert "result_truncated" in [w["code"] for w in out.warnings]


def test_exactly_full_result_is_not_truncated():
    """恰好取满上限、后面没有了：和查询层同一个口径，不算截断——截断的合并结果下游会拒收，误报就是把完整的结果拒了。"""
    out = execute([sales(), visits()], "SELECT * FROM s CROSS JOIN v", limits=QueryLimits(max_rows=16))
    assert out.truncated is False and len(out.rows) == 16
    assert "result_truncated" not in [w["code"] for w in out.warnings]


def test_byte_limit_matches_the_query_layer():
    out = execute([sales(), visits()], "SELECT * FROM s CROSS JOIN v", limits=QueryLimits(max_bytes=100))
    assert out.truncated is True and 0 < len(out.rows) < 16


def test_row_collector_is_the_one_cutoff_rule():
    """截断判定只有一份（data.engine.RowCollector）：到了行数或字节上限之后确实还有下一行才算截断，恰好取满不算；
    第一行总是收下（单行就超过字节上限也得交回点什么）。"""
    from app.data.engine import RowCollector

    full = RowCollector(QueryLimits(max_rows=2))
    assert full.take([1]) and full.take([2])
    assert full.rows == [[1], [2]] and full.truncated is False      # 恰好取满、后面没有了
    assert full.take([3]) is False                                  # 取满之后还有一行：截断，这一行不收
    assert full.rows == [[1], [2]] and full.truncated is True

    tiny = RowCollector(QueryLimits(max_bytes=5))
    assert tiny.take(["一行就超过字节上限"]) is True and tiny.take(["下一行"]) is False
    assert len(tiny.rows) == 1 and tiny.truncated is True


def test_merge_reads_through_the_query_layer_cutoff(monkeypatch):
    """合并查询调用查询层的判定，不另写一遍：两边的口径不会再各改各的。"""
    import app.engine.merge_query as merge_query
    from app.data.engine import RowCollector

    made: list[RowCollector] = []

    class Spy(RowCollector):
        def __init__(self, limits):
            super().__init__(limits)
            made.append(self)

    monkeypatch.setattr(merge_query, "RowCollector", Spy)
    out = execute([sales(), visits()], "SELECT * FROM s CROSS JOIN v", limits=QueryLimits(max_rows=5))
    assert len(made) == 1 and made[0].truncated is True and len(out.rows) == len(made[0].rows) == 5


def test_runaway_query_is_stopped():
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    with pytest.raises(MergeError, match="秒"):
        execute([sales()], endless, limits=QueryLimits(timeout_seconds=0.2))


# --------------------------------------------------------------------------
# 逐格来历：宁可不下钻，也不下钻错
# --------------------------------------------------------------------------


def _assert_traces_are_true(inputs, out):
    """凡是给出来历的格，都和它指向的那个输入的那一格是同一个值。"""
    by_alias = {i.alias: i for i in inputs}
    found = 0
    for r, row in enumerate(out.rows):
        for column in out.columns:
            hit = trace_cell(out.snapshot(), r, column)
            if hit is None:
                continue
            found += 1
            source = by_alias[hit.alias].snapshot
            value = source["rows"][hit.row][source["columns"].index(hit.column)]
            assert same_value(row[out.columns.index(column)], value), (r, column, hit)
    return found


def test_direct_columns_of_a_join_trace_to_the_input_rows():
    inputs = [sales(), visits()]
    out = execute(inputs, JOIN_SQL)
    assert out.lineage is not None
    # 2026-05-02 / S01 在会员库里是第 3 行（行号从 0 数），在门店库里是第 2 行
    hit = trace_cell(out.snapshot(), 2, "到店人数")
    assert (hit.alias, hit.row, hit.column) == ("v", 3, "到店人数")
    hit = trace_cell(out.snapshot(), 2, "销售额")
    assert (hit.alias, hit.row, hit.column) == ("s", 2, "销售额")
    assert _assert_traces_are_true(inputs, out) == 20


def test_renamed_columns_still_trace():
    inputs = [sales(), visits()]
    out = execute(inputs, "SELECT s.门店 AS 店, v.到店人数 人数 FROM s JOIN v ON s.日期 = v.日期 AND s.门店 = v.门店 "
                          "WHERE s.日期 = '2026-05-02' ORDER BY 1")
    hit = trace_cell(out.snapshot(), 1, "人数")
    assert (hit.alias, hit.row, hit.column) == ("v", 2, "到店人数")
    assert _assert_traces_are_true(inputs, out) == 4


def test_computed_columns_have_no_trace():
    inputs = [sales(), visits()]
    out = execute(inputs, "SELECT s.门店, s.订单数 * 1.0 / v.到店人数 AS 转化率 FROM s JOIN v "
                          "ON s.日期 = v.日期 AND s.门店 = v.门店 ORDER BY s.日期, s.门店")
    assert trace_cell(out.snapshot(), 0, "转化率") is None
    assert trace_cell(out.snapshot(), 0, "门店").alias == "s"


@pytest.mark.parametrize("sql", [
    "SELECT 日期, sum(订单数) AS 订单数 FROM s GROUP BY 日期",
    "SELECT DISTINCT 门店 FROM s",
    "SELECT 门店 FROM s UNION SELECT 门店 FROM v",
    "SELECT 门店, (SELECT max(到店人数) FROM v) AS 峰值 FROM s",
    "SELECT max(订单数) AS 订单数 FROM s",
    "WITH t AS (SELECT * FROM s) SELECT 订单数 FROM t",
    "SELECT 门店, 订单数, row_number() OVER (ORDER BY 订单数) AS 名次 FROM s",
    "SELECT * FROM s NATURAL JOIN v",
])
def test_shapes_outside_the_grammar_have_no_trace_at_all(sql):
    out = execute([sales(), visits()], sql)
    assert out.lineage is None
    assert all(trace_cell(out.snapshot(), r, c) is None for r in range(len(out.rows)) for c in out.columns)


def test_left_join_misses_have_no_trace_on_the_missing_side():
    short = [["2026-05-01", "S01", 160]]
    inputs = [sales(), visits(short)]
    out = execute(inputs, "SELECT s.日期, s.门店, v.到店人数 FROM s LEFT JOIN v ON s.日期 = v.日期 AND s.门店 = v.门店 "
                          "ORDER BY s.日期, s.门店")
    assert out.rows[1][2] is None
    assert trace_cell(out.snapshot(), 1, "到店人数") is None
    assert trace_cell(out.snapshot(), 1, "门店").alias == "s"
    assert trace_cell(out.snapshot(), 0, "到店人数").row == 0
    _assert_traces_are_true(inputs, out)


def test_star_expands_in_from_order():
    inputs = [sales(), visits()]
    out = execute(inputs, "SELECT * FROM s JOIN v ON s.日期 = v.日期 AND s.门店 = v.门店 ORDER BY s.日期, s.门店")
    assert out.columns == [*SALES_COLS, *VISITS_COLS]
    # 重名的列按第一次出现引用：「日期」指的是门店库那一列
    assert trace_cell(out.snapshot(), 0, "日期").alias == "s"
    assert trace_cell(out.snapshot(), 0, "到店人数").alias == "v"
    _assert_traces_are_true(inputs, out)


def test_using_columns_written_bare_have_no_trace():
    """USING 合并掉的列不写表名时，SQLite 取的是哪一边说不准（RIGHT / FULL JOIN 时是两边合起来的值）。"""
    out = execute([sales(), visits()], "SELECT 日期, 门店, 订单数, 到店人数 FROM s JOIN v USING (日期, 门店) ORDER BY 1, 2")
    assert trace_cell(out.snapshot(), 0, "日期") is None
    assert trace_cell(out.snapshot(), 0, "订单数").alias == "s"


def test_identical_output_rows_are_not_traced():
    """两行结果一模一样时，说不清哪一行来自哪一行输入：不给。"""
    twins = [["2026-05-01", "S01", 7], ["2026-05-01", "S01", 7], ["2026-05-02", "S02", 9]]
    inputs = [MergeInput(alias="v", node_id="n", artifact="b" * 64, snapshot=snap(VISITS_COLS, twins))]
    out = execute(inputs, "SELECT 日期, 门店, 到店人数 FROM v")
    assert trace_cell(out.snapshot(), 0, "到店人数") is None and trace_cell(out.snapshot(), 1, "到店人数") is None
    assert trace_cell(out.snapshot(), 2, "到店人数").row == 2


def test_self_join_traces_each_side_separately():
    inputs = [visits()]
    out = execute(inputs, "SELECT a.门店, a.到店人数 AS 首日, b.到店人数 AS 次日 FROM v AS a JOIN v AS b "
                          "ON a.门店 = b.门店 AND a.日期 = '2026-05-01' AND b.日期 = '2026-05-02' ORDER BY a.门店")
    assert out.rows == [["S01", 160, 150], ["S02", 90, 100]]
    first, second = trace_cell(out.snapshot(), 0, "首日"), trace_cell(out.snapshot(), 0, "次日")
    assert (first.alias, first.row) == ("v", 0) and (second.alias, second.row) == ("v", 3)
    _assert_traces_are_true(inputs, out)


def test_a_rowid_column_in_the_input_does_not_confuse_the_trace():
    rows = [[9, "S01", 1], [8, "S02", 2]]
    inputs = [MergeInput(alias="t", node_id="n", artifact="b" * 64, snapshot=snap(["rowid", "门店", "值"], rows))]
    out = execute(inputs, "SELECT 门店, 值 FROM t ORDER BY 门店 DESC")
    assert out.rows == [["S02", 2], ["S01", 1]]
    assert trace_cell(out.snapshot(), 0, "值").row == 1
    _assert_traces_are_true(inputs, out)


# --------------------------------------------------------------------------
# 快照形状
# --------------------------------------------------------------------------


def test_snapshot_has_the_query_shape_plus_inputs():
    out = execute([sales(), visits()], JOIN_SQL)
    snapshot = out.snapshot()
    for key in ("columns", "rows", "row_count", "truncated", "sql", "column_types"):
        assert key in snapshot
    assert snapshot["source"] == MERGE_SOURCE
    assert snapshot["inputs"] == [
        {"alias": "s", "node_id": "q_sales", "artifact": "a" * 64, "rows": 4, "columns": SALES_COLS, "source": "stores"},
        {"alias": "v", "node_id": "q_visits", "artifact": "b" * 64, "rows": 4, "columns": VISITS_COLS,
         "source": "members"},
    ]
    assert snapshot["sources"] == ["stores", "members"]
    # 内容确定：同样的输入、同样的 SQL，快照逐字相同（重放、继续运行得到同一个工件）
    assert execute([sales(), visits()], JOIN_SQL).snapshot() == snapshot


def test_masked_input_columns_stay_masked_even_when_renamed():
    members = MergeInput(alias="v", node_id="q_visits", artifact="b" * 64,
                         snapshot=snap(["门店", "手机号"], [["S01", "138****"]], source="members",
                                       mask_columns=["手机号"]))
    out = execute([sales(), members], "SELECT s.门店, v.手机号 AS 联系方式 FROM s JOIN v ON s.门店 = v.门店")
    assert set(out.snapshot()["mask_columns"]) == {"手机号", "联系方式"}


def test_nested_merges_keep_the_underlying_sources():
    first = execute([sales(), visits()], JOIN_SQL)
    nested = MergeInput(alias="m", node_id="merge1", artifact="e" * 64, snapshot=first.snapshot())
    out = execute([nested], "SELECT 日期, 门店, 到店人数 FROM m WHERE 门店 = 'S02'")
    assert out.snapshot()["sources"] == ["stores", "members"]
    hit = trace_cell(out.snapshot(), 0, "到店人数")
    assert (hit.alias, hit.row) == ("m", 1)
