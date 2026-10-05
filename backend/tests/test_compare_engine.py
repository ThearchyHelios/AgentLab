"""运行对比的纯函数（engine/compare.py）：同一个工作流的两次运行，每个指标的变化拆成数据、口径、模板 / SQL。

两次运行都是照真实运行的事件形状手写的虚构数据（区域、金额、日期都是编的）：取数节点 fetch 查一条按区域汇总的
SQL，Agent 节点 bot 自己再查一次、把各区域的金额作为数组字段交给口径卡，口径卡算几个指标。工件放在字典里。守的是：
- 只数据变（SQL 一字不差、结果变了）是 data_only；只改日期字面量是 params_only；SQL 结构变了是 sql_changed、不拆
- 口径变了（表达式、口径版本）是 caliber_and_data：旧表达式的叶子路径在这次的输入里都取得到时严格拆分，
  口径效应 + 数据效应 = 总变化（逐位相等）；取不到就不拆，说明缺的是哪一个
- 维度归因：按维度键配对两边的行，按变化的绝对值取前 10；度量列之和等于输入的值（两边都等）才给占比，占比之和
  为 1；对不上时占比为空并说明；截断的快照不归因，照实写
- 遮罩列的值绝不出现在结果里；用到遮罩列的指标不拆分
- 模板对比：新增、删除的节点，配置有变化的节点和配置项名
"""
from __future__ import annotations

import json
from decimal import Decimal

from app.engine import compare
from tests.fixtures.compare_runs import (
    BASE_ROWS,
    EAST,
    NEXT_ROWS,
    REFUND,
    WEEK37,
    Fake,
    amounts_in,
    cell_in,
    default_metrics,
    metric,
    node,
    region_sql,
    scalar_in,
    week,
    weekly_graph,
)


def run_compare(base, target):
    result = compare.compare(base.side(), target.side())
    json.dumps(result, allow_nan=False)        # 结果要能原样交给接口
    return result


def by_id(result):
    return {m["metric_id"]: m for m in result["metrics"]}


def exact(value):
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


# --------------------------------------------------------------------------
# 分类：只数据变、只参数变、SQL 结构变
# --------------------------------------------------------------------------


def test_same_sql_new_rows_is_data_only():
    result = run_compare(week("r1"), week("r2", NEXT_ROWS))
    metrics = by_id(result)
    east, total = metrics["east"], metrics["total"]
    assert (east["classification"], total["classification"]) == ("data_only", "data_only")
    assert (east["base"], east["target"], east["delta"]) == (1200.5, 1500.5, 300.0)
    assert (east["base_rendered"], east["target_rendered"], east["delta_rendered"]) == ("1,200.5元", "1,500.5元", "+300.0元")
    assert east["delta_pct"] == round(300 / 1200.5 * 100, 2)
    assert east["card_id"] == "card" and east["card_title"] == "周报口径卡" and east["name"] == "东区金额"
    assert east["decomposition"] is None and east["decomposition_note"] is None and east["reasons"]
    assert total["delta"] == 200.0 and total["delta_rendered"] == "+200.0元"
    assert result["summary"] == {"unchanged": 0, "data_only": 2, "caliber_and_data": 0, "sql_changed": 0,
                                 "params_only": 0, "added": 0, "removed": 0, "incomparable": 0}
    # 输入两边的值都列出来，带着各自的查询快照
    [cell] = east["inputs"]
    assert (cell["path"], cell["base"], cell["target"]) == (EAST, 1200.5, 1500.5)
    assert cell["base_artifact"] != cell["target_artifact"]
    # 查询：节点 + 第几次 + 工具配上对，SQL 一字不差
    assert [(q["node_id"], q["ordinal"], q["structure"]) for q in result["queries"]] == [
        ("fetch", 1, "same"), ("bot", 1, "same")]
    fetch = result["queries"][0]
    assert fetch["node_title"] == "区域取数" and fetch["base_sql"] == fetch["target_sql"] == region_sql()
    assert fetch["literals"] == [] and (fetch["base_rows"], fetch["target_rows"]) == (3, 3)
    assert (fetch["base_truncated"], fetch["target_truncated"]) == (False, False)
    assert result["template"]["same_graph"] is True and result["template"]["nodes"] == []
    assert (result["base"]["id"], result["target"]["id"]) == ("r1", "r2")


def test_identical_runs_are_unchanged():
    result = run_compare(week("r1"), week("r2"))
    assert {m["classification"] for m in result["metrics"]} == {"unchanged"}
    east = by_id(result)["east"]
    assert east["delta"] == 0 and east["delta_rendered"] is None and east["attribution"] is None
    assert result["summary"]["unchanged"] == 2


def test_only_date_literals_changed_is_params_only():
    # 图里的 SQL 是模板（两次一样），执行时代进去的日期不同
    graph = weekly_graph(region_sql(("{{ input.start }}", "{{ input.end }}")))
    result = run_compare(week("r1", graph=graph), week("r2", NEXT_ROWS, sql=region_sql(WEEK37), graph=graph))
    metrics = by_id(result)
    assert {m["classification"] for m in metrics.values()} == {"params_only"}
    assert any("参数" in r for r in metrics["east"]["reasons"])
    fetch = result["queries"][0]
    assert fetch["structure"] == "params_only"
    assert fetch["literals"] == [{"base": "'2026-08-31'", "target": "'2026-09-07'"},
                                 {"base": "'2026-09-06'", "target": "'2026-09-13'"}]
    # 定时运行换了日期属于参数变化：模板（图）一样
    assert result["summary"]["params_only"] == 2 and result["template"]["same_graph"] is True


def test_a_changed_sql_structure_is_flagged_not_split():
    paid = region_sql(extra=" AND status = 'paid'")
    result = run_compare(week("r1"), week("r2", NEXT_ROWS, sql=paid))
    metrics = by_id(result)
    assert {m["classification"] for m in metrics.values()} == {"sql_changed"}
    east = metrics["east"]
    assert east["decomposition"] is None and east["attribution"] is None
    assert any("区域取数" in r and "SQL" in r for r in east["reasons"])
    fetch = result["queries"][0]
    assert fetch["structure"] == "changed" and fetch["literals"] == []
    assert any("status = 'paid'" in d for d in fetch["differences"])
    # 图里 fetch 的 SQL 也改了：模板对比列出它和改了的配置项
    [changed] = result["template"]["nodes"]
    assert (changed["node_id"], changed["change"], changed["keys"]) == ("fetch", "config", ["args.sql"])
    assert result["template"]["same_graph"] is False


# --------------------------------------------------------------------------
# 口径变了：严格拆分
# --------------------------------------------------------------------------


def net_metrics(expression):
    """净额指标 net：表达式由调用方定（v1 只取金额，v2 减去退款）。"""
    def build(fetch, bot, rows, bot_rows):
        gmv, refund = rows[0][1], rows[0][2]
        inputs = [cell_in(0, "gmv", gmv, fetch)]
        value = gmv
        if REFUND in expression:
            inputs.append(cell_in(0, "refund", refund, fetch))
            value = gmv - refund
        return [metric("net", expression, value, inputs, name="东区净额")]
    return build


def test_a_caliber_change_is_split_into_caliber_and_data_effects():
    base = week("r1", metrics=net_metrics(EAST), version="v1")
    target = week("r2", NEXT_ROWS, metrics=net_metrics(f"{EAST} - {REFUND}"), version="v2")
    net = by_id(run_compare(base, target))["net"]
    assert net["classification"] == "caliber_and_data"
    assert any("表达式" in r for r in net["reasons"]) and any("v1" in r and "v2" in r for r in net["reasons"])
    assert (net["base"], net["target"], net["delta"]) == (1200.5, 1380.5, 180.0)
    split = net["decomposition"]
    assert net["decomposition_note"] is None
    # 口径效应 = f_new(x_new) − f_old(x_new) = 1380.5 − 1500.5；数据效应 = f_old(x_new) − f_old(x_old) = 1500.5 − 1200.5
    assert (split["caliber_effect"], split["data_effect"]) == (-120.0, 300.0)
    assert (split["caliber_rendered"], split["data_rendered"]) == ("-120.0元", "+300.0元")
    assert exact(split["caliber_effect"]) + exact(split["data_effect"]) == exact(net["delta"])


def test_the_split_adds_up_exactly_with_awkward_decimals():
    """小数位不同、浮点相减有误差的数：两效应之和逐位等于总变化。"""
    def build(expression, decimals):
        def make(fetch, bot, rows, bot_rows):
            gmv, refund = rows[0][1], rows[0][2]
            inputs = [cell_in(0, "gmv", gmv, fetch), cell_in(0, "refund", refund, fetch)]
            value = round(eval(expression.replace(EAST, repr(gmv)).replace(REFUND, repr(refund))), decimals)
            return [metric("ratio", expression, value, inputs, decimals=decimals, unit="")]
        return make

    base_rows = [["东区", 1000.1, 0.3], *BASE_ROWS[1:]]
    next_rows = [["东区", 1000.7, 0.2], *NEXT_ROWS[1:]]
    base = week("r1", base_rows, metrics=build(f"{EAST} * 0.1 - {REFUND}", 2))
    target = week("r2", next_rows, metrics=build(f"{EAST} * 0.3 + {REFUND}", 3), version="v2")
    ratio = by_id(run_compare(base, target))["ratio"]
    split = ratio["decomposition"]
    assert split is not None, ratio["decomposition_note"]
    # f_old(x_new) 按旧口径的小数位取整：round(1000.7 * 0.1 - 0.2, 2) = 99.87
    assert exact(split["data_effect"]) == exact(99.87) - exact(ratio["base"])
    assert exact(split["caliber_effect"]) + exact(split["data_effect"]) == exact(ratio["delta"])
    assert exact(ratio["delta"]) == exact(ratio["target"]) - exact(ratio["base"])


def test_a_version_bump_with_the_same_formula_is_all_data():
    base = week("r1", metrics=net_metrics(EAST), version="v1")
    target = week("r2", NEXT_ROWS, metrics=net_metrics(EAST), version="v2")
    net = by_id(run_compare(base, target))["net"]
    assert net["classification"] == "caliber_and_data"
    assert (net["decomposition"]["caliber_effect"], net["decomposition"]["data_effect"]) == (0, 300.0)
    assert net["decomposition"]["caliber_rendered"] == "0.0元"


def test_no_split_when_an_old_leaf_is_not_recorded_this_time():
    base = week("r1", metrics=net_metrics(f"{EAST} - {REFUND}"), version="v1")
    target = week("r2", NEXT_ROWS, metrics=net_metrics(EAST), version="v2")
    net = by_id(run_compare(base, target))["net"]
    assert net["classification"] == "caliber_and_data" and net["decomposition"] is None
    assert REFUND in net["decomposition_note"] and "无法严格拆分" in net["decomposition_note"]
    # 总变化照算
    assert net["delta"] == 1500.5 - (1200.5 - 100.0)


def test_no_split_when_the_sql_changed_too():
    base = week("r1", metrics=net_metrics(EAST), version="v1")
    target = week("r2", NEXT_ROWS, sql=region_sql(extra=" AND status = 'paid'"), metrics=net_metrics(EAST), version="v2")
    net = by_id(run_compare(base, target))["net"]
    assert net["classification"] == "caliber_and_data" and net["decomposition"] is None
    assert "SQL" in net["decomposition_note"]


def test_a_unit_change_is_not_subtracted():
    def build(unit):
        def make(fetch, bot, rows, bot_rows):
            return [metric("east", EAST, rows[0][1], [cell_in(0, "gmv", rows[0][1], fetch)], unit=unit)]
        return make
    east = by_id(run_compare(week("r1", metrics=build("元")), week("r2", NEXT_ROWS, metrics=build("万元"))))["east"]
    assert east["classification"] == "caliber_and_data" and any("单位" in r for r in east["reasons"])
    assert east["delta"] is None and east["delta_rendered"] is None and east["decomposition"] is None
    assert "单位" in east["decomposition_note"]


# --------------------------------------------------------------------------
# 维度归因
# --------------------------------------------------------------------------


def test_attribution_pairs_keys_and_shares_add_up_to_one():
    total = by_id(run_compare(week("r1"), week("r2", NEXT_ROWS)))["total"]
    attribution = total["attribution"]
    assert attribution["columns"] == ["region"] and attribution["measure"] == "gmv"
    assert attribution["total_delta"] == 200.0
    rows = attribution["rows"]
    assert [(r["key"], r["base"], r["target"], r["delta"]) for r in rows] == [
        (["东区"], 1200.5, 1500.5, 300.0), (["南区"], 800.0, 700.0, -100.0), (["西区"], 600.0, 600.0, 0.0)]
    assert [r["share"] for r in rows] == [1.5, -0.5, 0.0]
    assert abs(sum(r["share"] for r in rows) - 1) < 1e-12


def test_attribution_pairs_by_key_not_by_row_position():
    """这次多了一个区域、顺序也变了：按区域配对，只有一边有的那一边写空。"""
    next_rows = [["北区", 90.0, 0.0], ["东区", 1500.5, 120.0], ["西区", 600.0, 10.0], ["南区", 700.0, 40.0]]
    total = by_id(run_compare(week("r1"), week("r2", next_rows)))["total"]
    rows = {tuple(r["key"]): r for r in total["attribution"]["rows"]}
    assert rows[("北区",)]["base"] is None and rows[("北区",)]["target"] == 90.0 and rows[("北区",)]["delta"] == 90.0
    assert rows[("东区",)]["delta"] == 300.0 and rows[("南区",)]["delta"] == -100.0
    assert abs(sum(r["share"] for r in rows.values()) - 1) < 1e-12


def test_attribution_keeps_the_ten_largest_changes():
    names = [f"{n}区" for n in "一二三四五六七八九十甲乙"]
    base_rows = [[name, 100.0, 0.0] for name in names]
    next_rows = [[name, 100.0 + (i + 1) * 10 * (-1) ** i, 0.0] for i, name in enumerate(names)]
    total = by_id(run_compare(week("r1", base_rows), week("r2", next_rows)))["total"]
    rows = total["attribution"]["rows"]
    assert len(rows) == 10
    assert [abs(r["delta"]) for r in rows] == [120.0, 110.0, 100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0, 30.0]
    assert "12" in total["attribution"]["note"]
    # 占比按全部维度值的总变化算：前 10 个之和不必为 1，全部之和为 1
    assert total["attribution"]["total_delta"] == sum((i + 1) * 10 * (-1) ** i for i in range(12))


def test_no_share_when_the_measure_does_not_add_up_to_the_input():
    """东区金额只是快照里的一格：各区域之和不等于它，各键的变化照列，占比为空并说明。"""
    east = by_id(run_compare(week("r1"), week("r2", NEXT_ROWS)))["east"]
    attribution = east["attribution"]
    assert attribution["measure"] == "gmv" and [r["key"] for r in attribution["rows"]] == [["东区"], ["南区"], ["西区"]]
    assert all(r["share"] is None for r in attribution["rows"]) and attribution["note"]


def test_a_total_row_is_left_out_of_its_own_breakdown():
    """合计行和各区域在同一份结果里：指标取合计那一格，按其余各行归因，占比之和为 1。"""
    base_rows = [*BASE_ROWS, ["合计", 2600.5, 150.0]]
    next_rows = [*NEXT_ROWS, ["合计", 2800.5, 170.0]]

    def build(fetch, bot, rows, bot_rows):
        return [metric("all", "cell(nodes.fetch, 3, 'gmv')", rows[3][1], [cell_in(3, "gmv", rows[3][1], fetch)])]
    total = by_id(run_compare(week("r1", base_rows, metrics=build), week("r2", next_rows, metrics=build)))["all"]
    rows = total["attribution"]["rows"]
    assert ["合计"] not in [r["key"] for r in rows] and len(rows) == 3
    assert abs(sum(r["share"] for r in rows) - 1) < 1e-12 and total["attribution"]["total_delta"] == 200.0


def test_a_truncated_snapshot_is_not_attributed():
    base, target = week("r1"), Fake("r2")
    sql = region_sql()
    fetch = target.tool("fetch", sql, rows=NEXT_ROWS)
    bot = target.agent("bot", sql, rows=NEXT_ROWS, truncated=True)
    target.card(default_metrics(fetch, bot, NEXT_ROWS, NEXT_ROWS))
    total = by_id(run_compare(base, target))["total"]
    attribution = total["attribution"]
    assert attribution["rows"] == [] and "截断" in attribution["note"]
    assert result_queries(run_compare(base, target), "bot")["target_truncated"] is True


def result_queries(result, node_id):
    return next(q for q in result["queries"] if q["node_id"] == node_id)


def test_a_period_column_that_never_matches_is_not_a_dimension():
    """每期都不同的列（这里是日期写成的文本）不拿来配对：按其余的维度列归因。"""
    columns = ["day", "region", "gmv"]
    types = {"day": "text", "region": "text", "gmv": "number"}

    def make(run_id, days, values):
        run = Fake(run_id)
        rows = [[day, region, value] for day in days for region, value in zip(("东区", "南区"), values)]
        sql = "SELECT day, region, SUM(amount) AS gmv FROM orders GROUP BY day, region"
        bot = run.agent("bot", sql, columns, rows, types=types)
        total = sum(r[2] for r in rows)
        run.card([metric("total", "sum(vars.kpi.amounts)", total, [amounts_in(bot, rows)])])
        return run

    total = by_id(run_compare(make("r1", ("2026-09-05", "2026-09-06"), (10.0, 20.0)),
                              make("r2", ("2026-09-12", "2026-09-13"), (15.0, 18.0))))["total"]
    attribution = total["attribution"]
    assert attribution["columns"] == ["region"]
    assert [(r["key"], r["delta"]) for r in attribution["rows"]] == [(["东区"], 10.0), (["南区"], -4.0)]
    assert "day" in attribution["note"]


# --------------------------------------------------------------------------
# 遮罩列
# --------------------------------------------------------------------------


def test_masked_columns_never_show_up():
    """数据源遮了 manager（经理名）和 refund：经理名不当维度、不出现在结果里；用到 refund 的指标输入不给值、不拆分。"""
    columns = ["region", "manager", "gmv", "refund"]
    types = {"region": "text", "manager": "text", "gmv": "number", "refund": "number"}

    def make(run_id, rows, expression, version):
        run = Fake(run_id)
        sql = "SELECT region, manager, SUM(amount) AS gmv, SUM(refund) AS refund FROM orders GROUP BY region, manager"
        fetch = run.tool("fetch", sql, columns, rows, types=types)
        bot = run.agent("bot", sql, columns, rows, types=types)
        run.masked = {fetch: frozenset({"manager", "refund"}), bot: frozenset({"manager", "refund"})}
        inputs = [cell_in(0, "gmv", rows[0][2], fetch)]
        value = rows[0][2]
        if "refund" in expression:
            inputs.append(cell_in(0, "refund", rows[0][3], fetch))
            value = rows[0][2] - rows[0][3]
        run.card([metric("net", expression, value, inputs),
                  metric("total", "sum(vars.kpi.amounts)", sum(r[2] for r in rows), [amounts_in(bot, rows)])],
                 version=version)
        return run

    base_rows = [["东区", "经理甲", 1200.5, 101.25], ["南区", "经理乙", 800.0, 52.75]]
    next_rows = [["东区", "经理甲", 1500.5, 133.5], ["南区", "经理乙", 700.0, 47.25]]
    result = run_compare(make("r1", base_rows, EAST, "v1"),
                         make("r2", next_rows, "cell(nodes.fetch, 0, 'gmv') - cell(nodes.fetch, 0, 'refund')", "v2"))
    text = json.dumps(result, ensure_ascii=False)
    for secret in ("经理甲", "经理乙", "101.25", "52.75", "133.5", "47.25"):
        assert secret not in text, secret
    metrics = by_id(result)
    net = metrics["net"]
    refund = next(i for i in net["inputs"] if "refund" in i["path"])
    assert refund["masked"] is True and refund["base"] is None and refund["target"] is None
    assert net["decomposition"] is None and "遮罩" in net["decomposition_note"]
    # 维度只用没遮的 region；占比照给
    attribution = metrics["total"]["attribution"]
    assert attribution["columns"] == ["region"] and abs(sum(r["share"] for r in attribution["rows"]) - 1) < 1e-12
    assert any("遮罩" in n for n in result["notes"])


def test_a_masked_measure_is_not_attributed():
    def masked(run):
        run.masked = {a: frozenset({"gmv"}) for a in run.queries}
        return run
    result = run_compare(masked(week("r1")), masked(week("r2", NEXT_ROWS)))
    total = by_id(result)["total"]
    assert total["attribution"]["rows"] == [] and "遮罩" in total["attribution"]["note"]
    east = by_id(result)["east"]
    assert east["inputs"][0]["base"] is None and east["inputs"][0]["masked"] is True
    assert "1200.5" not in json.dumps(east["inputs"]) and "800.0" not in json.dumps(result)


# --------------------------------------------------------------------------
# 对不上具体查询的输入、运行输入、一边才有的指标
# --------------------------------------------------------------------------


def code_metrics(value):
    def build(fetch, bot, rows, bot_rows):
        return [metric("calc", "vars.adj * 2", value * 2, [scalar_in("vars.adj", value, "calc", "code")])]
    return build


def test_an_input_from_a_code_node_follows_its_upstream():
    def make(run_id, rows, value, sql=None):
        run = Fake(run_id, graph=weekly_graph(sql, between=(node("calc", "code", "调整", code="print(1)",
                                                                assign_to="adj"),)))
        sql = sql or region_sql()
        run.tool("fetch", sql, rows=rows)
        run.agent("bot", sql, rows=rows)
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, rows, rows))
        return run

    # 上游什么都没变：变化来自数据
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10), make("r2", NEXT_ROWS, 12)))["calc"]
    assert calc["classification"] == "data_only" and calc["delta"] == 4
    # 上游的 SQL 结构变了：说不清它变没变，不能报成数据变化
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10),
                             make("r2", NEXT_ROWS, 12, sql=region_sql(extra=" AND status = 'paid'"))))["calc"]
    assert calc["classification"] == "incomparable" and any("调整" in r for r in calc["reasons"])


def test_the_code_node_itself_changed_is_a_template_change():
    def make(run_id, code, value):
        run = Fake(run_id, graph=weekly_graph(between=(node("calc", "code", "调整", code=code, assign_to="adj"),)))
        run.tool("fetch", region_sql())
        run.agent("bot", region_sql())
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, BASE_ROWS, BASE_ROWS))
        return run
    calc = by_id(run_compare(make("r1", "x = 1", 10), make("r2", "x = 2", 11)))["calc"]
    assert calc["classification"] == "sql_changed" and any("调整" in r for r in calc["reasons"])


def test_a_run_input_change_is_a_parameter_change():
    def make(run_id, goal):
        run = Fake(run_id, inputs={"goal": goal})
        run.add("node.started", "start", {"node_type": "input"})
        run.add("node.finished", "start", {"artifact": run.put({"goal": goal})})
        fetch = run.tool("fetch", region_sql())
        run.agent("bot", region_sql())
        run.card([metric("reach", f"round({EAST} / input.goal * 100, 1)", round(1200.5 / goal * 100, 1),
                         [cell_in(0, "gmv", 1200.5, fetch), scalar_in("input.goal", goal, "start", "input")],
                         unit="%", fmt="plain")])
        return run
    result = run_compare(make("r1", 1000), make("r2", 1500))
    reach = by_id(result)["reach"]
    assert reach["classification"] == "params_only" and any("input.goal" in r for r in reach["reasons"])
    assert reach["delta_rendered"] == "-40.0 个百分点"
    assert any("goal" in n for n in result["notes"])


def test_metrics_and_cards_on_one_side_only():
    def extra(fetch, bot, rows, bot_rows):
        return [*default_metrics(fetch, bot, rows, bot_rows),
                metric("west", "cell(nodes.fetch, 2, 'gmv')", rows[2][1], [cell_in(2, "gmv", rows[2][1], fetch)])]
    base = week("r1")
    target = week("r2", metrics=extra)
    target.card([metric("cnt", "len(vars.kpi.amounts)", 3, [amounts_in(target.queries[1], BASE_ROWS)], unit="个",
                        decimals=None)], node_id="card2", caliber="补充口径")
    result = run_compare(base, target)
    metrics = by_id(result)
    assert metrics["west"]["classification"] == "added" and metrics["west"]["base"] is None
    assert metrics["cnt"]["classification"] == "added" and metrics["cnt"]["card_id"] == "card2"
    assert result["summary"]["added"] == 2
    swapped = by_id(run_compare(target, base))
    assert swapped["west"]["classification"] == "removed" and swapped["west"]["target"] is None


def test_an_unreadable_card_makes_its_metrics_incomparable():
    base, target = week("r1"), week("r2", NEXT_ROWS)
    base.tampered.add(base.cards[0][1])
    base.cards = []          # 接口只交回复验过哈希的口径卡
    result = run_compare(base, target)
    assert {m["classification"] for m in result["metrics"]} == {"incomparable"}
    assert any("哈希" in r for r in by_id(result)["east"]["reasons"])


def test_a_card_executed_several_times_uses_the_last_one():
    base = week("r1")
    target = Fake("r2")
    fetch = target.tool("fetch", region_sql(), rows=NEXT_ROWS)
    bot = target.agent("bot", region_sql(), rows=NEXT_ROWS)
    target.card(default_metrics(fetch, bot, BASE_ROWS, BASE_ROWS))
    target.card(default_metrics(fetch, bot, NEXT_ROWS, NEXT_ROWS), runs=2)
    result = run_compare(base, target)
    assert by_id(result)["east"]["target"] == 1500.5
    assert any("执行了 3 次" in n for n in result["notes"])


def test_verify_queries_are_left_out():
    base = week("r1")
    target = Fake("r2")
    fetch = target.tool("fetch", region_sql(), rows=NEXT_ROWS)
    bot = target.agent("bot", region_sql(), rows=NEXT_ROWS, verify=["SELECT COUNT(*) AS n FROM orders"])
    target.card(default_metrics(fetch, bot, NEXT_ROWS, NEXT_ROWS))
    result = run_compare(base, target)
    assert [(q["node_id"], q["structure"]) for q in result["queries"]] == [("fetch", "same"), ("bot", "same")]


# --------------------------------------------------------------------------
# 查询配对、模板对比
# --------------------------------------------------------------------------


def test_queries_fall_back_to_normalized_sql_pairing():
    """节点换了 ID：按「节点 + 第几次 + 工具」配不上，再按规范化 SQL 配（大小写、空白不同也认）。"""
    base = week("r1")
    target = Fake("r2", graph=weekly_graph())
    target.tool("pull", region_sql(WEEK37).lower(), rows=NEXT_ROWS)
    target.agent("bot", region_sql(WEEK37), rows=NEXT_ROWS)
    result = run_compare(base, target)
    pull = result_queries(result, "pull")
    assert pull["structure"] == "params_only" and pull["base_sql"] == region_sql()
    assert len(result["queries"]) == 2


def test_an_agent_asking_in_another_order_is_paired_by_sql():
    """Agent 这次换了查询的先后：同一节点里先按规范化 SQL 配，不按第几次硬配成两条「结构变了」。"""
    by_region = region_sql()
    by_day = "SELECT day, SUM(amount) AS gmv FROM orders WHERE day >= '2026-08-31' GROUP BY day"

    def make(run_id, sqls):
        run = Fake(run_id)
        run.add("node.started", "bot", {"node_type": "agent"})
        for i, sql in enumerate(sqls):
            artifact = run.snapshot(sql, ["k", "gmv"], [["甲", 1.0]])
            run.add("tool.start", "bot", {"tool": "db_query__shop", "args": {"sql": sql}, "call_id": f"c{i}"})
            run.add("tool.end", "bot", {"tool": "db_query__shop", "call_id": f"c{i}", "query_artifact": artifact})
        run.add("node.finished", "bot", {"duration_ms": 1, "attempt": 1})
        return run

    result = run_compare(make("r1", [by_region, by_day]),
                         make("r2", [by_day.replace("2026-08-31", "2026-09-07"), region_sql(WEEK37)]))
    pairs = [(q["structure"], q["base_sql"][:20]) for q in result["queries"]]
    assert pairs == [("params_only", by_day[:20]), ("params_only", by_region[:20])]


def test_an_agent_reconfigured_with_the_same_sql_is_a_template_change():
    """Agent 改了提示词、SQL 一字不差：它按出处交来的字段可能换了取法，不能报成数据变化。"""
    graph = weekly_graph()
    graph["nodes"][2]["data"]["config"]["prompt"] = "按区域列出金额，从大到小"
    result = run_compare(week("r1"), week("r2", NEXT_ROWS, graph=graph))
    metrics = by_id(result)
    assert metrics["total"]["classification"] == "sql_changed"
    assert any("区域分析" in r for r in metrics["total"]["reasons"])
    # 取数节点的格子不经过这个 Agent：照旧是数据变化
    assert metrics["east"]["classification"] == "data_only"


def test_unpaired_queries_are_added_or_removed():
    base = week("r1")
    target = week("r2")
    target.tool("extra", "SELECT COUNT(*) AS n FROM refunds", ["n"], [[7]], types={"n": "number"})
    result = run_compare(base, target)
    extra = result_queries(result, "extra")
    assert extra["structure"] == "added" and extra["base_sql"] is None and extra["target_rows"] == 1
    removed = result_queries(run_compare(target, base), "extra")
    assert removed["structure"] == "removed" and removed["target_sql"] is None


def test_the_template_lists_added_removed_and_reconfigured_nodes():
    base_graph = weekly_graph(between=(node("calc", "code", "调整", code="x = 1"),))
    target_graph = weekly_graph(region_sql(WEEK37), between=(node("check", "dual_check", "双盲复算"),))
    target_graph["nodes"][-2]["data"]["label"] = "写周报（新）"
    target_graph["nodes"][-1]["data"]["config"] = {"fields": [{"name": "周报", "value": "{{ nodes.write.text }}"}]}
    base = week("r1", graph=base_graph)
    target = week("r2", graph=target_graph, version="v1")
    base.version, target.version = 3, 4
    template = run_compare(base, target)["template"]
    assert (template["base_version"], template["target_version"], template["same_graph"]) == (3, 4, False)
    changes = {n["node_id"]: (n["change"], n["keys"]) for n in template["nodes"]}
    assert changes == {"fetch": ("config", ["args.sql"]), "check": ("added", []), "write": ("config", ["label"]),
                       "out": ("config", ["fields"]), "calc": ("removed", [])}
    labels = {n["node_id"]: n["labels"] for n in template["nodes"]}
    assert labels["write"] == ["节点名称"] and labels["out"] == ["成果字段"]
    # 配置的值（这里是 SQL 和模板）一个字都不展示
    assert "2026-09" not in json.dumps(template, ensure_ascii=False)


def test_a_missing_snapshot_file_is_reported_and_withheld():
    """基准运行的快照文件不在了：SQL 从 tool.start 的参数里取，结构照比；输入的值核对不了遮没遮，不给；不归因。"""
    base, target = week("r1"), week("r2", NEXT_ROWS)
    fetch = base.queries[0]
    del base.store[fetch]
    result = run_compare(base, target)
    query = result_queries(result, "fetch")
    assert query["structure"] == "same" and query["base_sql"] == region_sql() and query["base_rows"] is None
    assert "无法读取" in query["problem"]
    east = by_id(result)["east"]
    assert east["classification"] == "data_only" and east["inputs"][0]["base"] is None
    assert east["attribution"]["rows"] == [] and "无法读取" in east["attribution"]["note"]


def test_runs_without_metric_cards_still_compare_queries():
    base, target = Fake("r1"), Fake("r2")
    base.tool("fetch", region_sql())
    target.tool("fetch", region_sql(WEEK37), rows=NEXT_ROWS)
    result = run_compare(base, target)
    assert result["metrics"] == [] and sum(result["summary"].values()) == 0
    assert any("没有口径卡" in n for n in result["notes"])
    assert [q["structure"] for q in result["queries"]] == ["params_only"]


def test_the_comparison_does_not_touch_the_runs():
    base, target = week("r1"), week("r2", NEXT_ROWS)
    before = (list(base.events), list(target.events), dict(base.store), dict(target.store))
    run_compare(base, target)
    assert (base.events, target.events, base.store, target.store) == before
