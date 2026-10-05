"""运行对比的纯函数（engine/compare.py）：同一个工作流的两次运行，每个指标的变化拆成数据、口径、模板 / SQL。

两次运行都是照真实运行的事件形状手写的虚构数据（区域、金额、日期都是编的）：取数节点 fetch 查一条按区域汇总的
SQL，Agent 节点 bot 自己再查一次、把各区域的金额作为数组字段交给口径卡，口径卡算几个指标。工件放在字典里。守的是：
- 只数据变（SQL 一字不差、结果变了）是 data_only；只改日期字面量是 params_only；SQL 结构变了是 sql_changed、不拆
- 口径变了（表达式、口径版本）是 caliber_and_data：旧表达式的叶子路径在这次的输入里都取得到时严格拆分，
  口径效应 + 数据效应 = 总变化（逐位相等）；取不到就不拆，说明缺的是哪一个
- 维度归因：按维度键配对两边的行，按变化的绝对值取前 10；度量列之和等于输入的值（两边都等）才给占比，占比之和
  为 1；对不上时占比为空并说明；截断的快照不归因，照实写
- 遮罩列的值绝不出现在结果里（只有一边的快照遮了也算遮了）；用到遮罩列的指标不拆分
- 模板对比：新增、删除的节点，配置有变化的节点和配置项名
- 输入的出处：合并查询追到它的上游查询；Agent 按出处交的字段还要引用同一列、同一段行（行段相同、一边是整份结果
  一边不是的不算，起止行有一个不同的不算；都不是整份结果的，引用过的每个区域两边要被切在同一处——都整个被引用（一边
  没有这个区域的算整个被引用），或者都只引用了它同样的那几行（排在引用的行前面的行数、引用的行数都一样，后面剩几行
  不管）——区域整个挪出去、区域 × 日期的结果里有一天被挤出去、多了一天却只引用了前几天都不算，两边都没引用的区域
  不管；维度有几列的按几列一起认，归因只按引用的行里分得出的那几列说，就按那几列再核对一遍；单格那一行要是同一个
  区域的同一行（排在它前面的、这个区域的行数一样）；快照读不出来、有的行和列对不上的说不清）；没按出处交的数、上游
  数据没变数却变了的、上游的 Agent 两次交出的结果不同的，说不清变化从哪来（incomparable），记不出写入者的只看口径卡
  之前可能执行过的节点（循环回边连回来的算在它之前，只在一次运行的图里有的节点按两次的图合起来认先后，严格拆分也照
  这个范围核对）；图里写死的字面量改了、连线变了、读到的默认设置变了都算模板变化；不归因的指标用到遮罩列也在整体
  说明里提一句
"""
from __future__ import annotations

import ast
import copy
import importlib
import inspect
import json
import pkgutil
import textwrap
from decimal import Decimal
from types import SimpleNamespace

import app.engine.nodes as nodes_package
from app.engine import compare
from app.engine.compiler import RUNNERS
from app.engine.merge_query import MERGE_SOURCE
from app.engine.schema import NodeType
from tests.fixtures.compare_runs import (
    BASE_ROWS,
    COLUMNS,
    EAST,
    NEXT_ROWS,
    REFUND,
    TOOL,
    TYPES,
    WEEK37,
    Fake,
    amounts_in,
    cell_in,
    chain,
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
    assert east["decomposition"] is None and east["decomposition_note"] is None
    # 两个输入都落在查询快照上：直接说取数的 SQL 没变、查询结果变了
    assert east["reasons"] == total["reasons"] == [compare.DATA_FROM_QUERIES]
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
    next_rows = [["东区", 1000.75, 0.2], *NEXT_ROWS[1:]]
    base = week("r1", base_rows, metrics=build(f"{EAST} * 0.1 - {REFUND}", 2))
    target = week("r2", next_rows, metrics=build(f"{EAST} * 0.3 + {REFUND}", 3), version="v2")
    ratio = by_id(run_compare(base, target))["ratio"]
    split = ratio["decomposition"]
    assert split is not None, ratio["decomposition_note"]
    # f_old(x_new) 按旧口径的小数位取整：1000.75 * 0.1 - 0.2 = 99.875，取 2 位是 99.88（按新口径的 3 位是 99.875）
    assert exact(split["data_effect"]) == exact(99.88) - exact(ratio["base"])
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
    # 两条不同的查询的行不拿来按维度配对
    assert net["attribution"] is None


def test_a_caliber_change_still_names_the_sql_change():
    """口径变了、SQL 结构也变了、旧口径的叶子这次又没记录：分类按口径，SQL 的变化照样写进说明。"""
    base = week("r1", metrics=net_metrics(f"{EAST} - {REFUND}"), version="v1")
    paid = region_sql(extra=" AND status = 'paid'")
    target = week("r2", NEXT_ROWS, sql=paid, metrics=net_metrics(EAST), version="v2")
    net = by_id(run_compare(base, target))["net"]
    assert net["classification"] == "caliber_and_data" and net["decomposition"] is None
    assert any("区域取数" in r and "SQL" in r for r in net["reasons"]), net["reasons"]
    # 退款那一格只在旧口径里用到：改表达式带来的，不再另说一遍「只在一边有记录」
    assert not any("只在一边" in r for r in net["reasons"]), net["reasons"]


def test_a_unit_change_is_not_subtracted():
    def build(unit):
        def make(fetch, bot, rows, bot_rows):
            return [metric("east", EAST, rows[0][1], [cell_in(0, "gmv", rows[0][1], fetch)], unit=unit)]
        return make
    east = by_id(run_compare(week("r1", metrics=build("元")), week("r2", NEXT_ROWS, metrics=build("万元"))))["east"]
    assert east["classification"] == "caliber_and_data" and any("单位" in r for r in east["reasons"])
    assert east["delta"] is None and east["delta_rendered"] is None and east["decomposition"] is None
    assert "单位" in east["decomposition_note"]


def test_display_settings_that_change_the_value_are_caliber_changes():
    def build(**kw):
        def make(fetch, bot, rows, bot_rows):
            return [metric("east", EAST, rows[0][1], [cell_in(0, "gmv", rows[0][1], fetch)], **kw)]
        return make

    def east(before, after):
        return by_id(run_compare(week("r1", metrics=build(**before)), week("r2", NEXT_ROWS, metrics=build(**after))))

    # 小数位改变取整
    found = east({"decimals": 1}, {"decimals": 0})
    assert found["east"]["classification"] == "caliber_and_data" and any("小数位" in r for r in found["east"]["reasons"])
    # 比率与否改变数值的含义
    found = east({"unit": "", "fmt": "plain"}, {"unit": "", "fmt": "percent_of_ratio"})
    assert found["east"]["classification"] == "caliber_and_data" and found["east"]["delta"] is None
    # 千分位和不分组只影响显示：分类照数据算，变化写进说明
    result = run_compare(week("r1", metrics=build(fmt="thousands")), week("r2", NEXT_ROWS, metrics=build(fmt="plain")))
    assert by_id(result)["east"]["classification"] == "data_only"
    assert any("显示格式" in n for n in result["notes"])


def test_an_old_card_without_display_settings_is_not_a_caliber_change():
    """升级前封存的运行：卡上的指标没记小数位、显示格式和输入（当时按定义里的小数位取过整）。"""
    def old(run_id, value, graph=None):
        run = Fake(run_id, graph=graph)
        run.tool("fetch", region_sql())
        run.agent("bot", region_sql())
        run.card([{"id": "east", "name": "东区金额", "unit": "元", "value": value, "expression": EAST}])
        return run

    def new(run_id, value):
        run = Fake(run_id)
        fetch = run.tool("fetch", region_sql())
        run.agent("bot", region_sql())
        run.card([metric("east", EAST, value, [cell_in(0, "gmv", value, fetch)], name="东区金额")])
        return run

    east = by_id(run_compare(old("r1", 1200.5), new("r2", 1200.5)))["east"]
    assert (east["classification"], east["reasons"]) == ("unchanged", [])
    east = by_id(run_compare(old("r1", 1200.5), new("r2", 1500.5)))["east"]
    assert east["classification"] == "incomparable" and east["reasons"] == [compare.NO_LINEAGE]
    # 那次运行的图里写着定义：按定义里的小数位核对
    graph = weekly_graph()
    graph["nodes"][3]["data"]["config"]["metrics"] = [{"id": "east", "expression": EAST, "decimals": 2}]
    east = by_id(run_compare(old("r1", 1200.5, graph), new("r2", 1200.5)))["east"]
    assert east["classification"] == "caliber_and_data" and any("小数位由 2 改为 1" in r for r in east["reasons"])


def test_the_split_adds_up_in_json_without_fixed_decimals():
    """没设小数位的指标：三个数各自转成浮点也要逐位相加（接口按 JSON 交回的就是浮点）。"""
    def build(expression):
        def make(fetch, bot, rows, bot_rows):
            gmv, refund = rows[0][1], rows[0][2]
            inputs = [cell_in(0, "gmv", gmv, fetch), cell_in(0, "refund", refund, fetch)]
            value = eval(expression.replace(EAST, repr(gmv)).replace(REFUND, repr(refund)))
            return [metric("m", expression, value, inputs, decimals=None, unit="")]
        return make

    base_rows = [["东区", 0.1, 0.2], *BASE_ROWS[1:]]
    next_rows = [["东区", 0.7, 0.1], *NEXT_ROWS[1:]]
    base = week("r1", base_rows, metrics=build(f"{EAST} + {REFUND}"))                  # 0.30000000000000004
    target = week("r2", next_rows, metrics=build(f"{EAST} * 3 + {REFUND}"), version="v2")  # 2.1999999999999997
    m = by_id(run_compare(base, target))["m"]
    split = m["decomposition"]
    assert split is not None, m["decomposition_note"]
    assert exact(split["caliber_effect"]) + exact(split["data_effect"]) == exact(m["delta"])
    assert abs(m["delta"] - (m["target"] - m["base"])) < 1e-12


def test_a_tiny_base_does_not_break_the_percentage():
    """基准是浮点误差那么小的数、这次很大：百分比写得出来（或者为空），对比不能整个失败。"""
    def build(value):
        def make(fetch, bot, rows, bot_rows):
            m = metric("diff", EAST, 1.0, [cell_in(0, "gmv", rows[0][1], fetch)], decimals=None)
            m["value"], m["rendered"] = value, "—"
            return [m]
        return make
    diff = by_id(run_compare(week("r1", metrics=build(0.30000000000000004 - 0.3)),
                             week("r2", NEXT_ROWS, metrics=build(1e8))))["diff"]
    assert diff["delta"] == 1e8 - (0.30000000000000004 - 0.3)
    assert diff["delta_pct"] is not None and diff["delta_pct"] > 1e25


# --------------------------------------------------------------------------
# 维度归因
# --------------------------------------------------------------------------


def test_attribution_pairs_keys_and_shares_add_up_to_one():
    base, target = week("r1"), week("r2", NEXT_ROWS)
    total = by_id(run_compare(base, target))["total"]
    attribution = total["attribution"]
    assert attribution["columns"] == ["region"] and attribution["measure"] == "gmv"
    # 归因用的是哪个输入、两边各自哪份快照
    assert (attribution["path"], attribution["base_artifact"], attribution["target_artifact"]) == (
        "vars.kpi.amounts", base.queries[1], target.queries[1])
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


def test_no_share_when_only_one_side_adds_up():
    """基准运行里东区就是全部（各区域之和等于它），这次南区也有了：只有一边对得上，不给占比。"""
    base_rows = [["东区", 100.0, 0.0], ["南区", 0.0, 0.0], ["西区", 0.0, 0.0]]
    next_rows = [["东区", 150.0, 0.0], ["南区", 20.0, 0.0], ["西区", 0.0, 0.0]]
    east = by_id(run_compare(week("r1", base_rows), week("r2", next_rows)))["east"]
    attribution = east["attribution"]
    assert east["classification"] == "data_only" and len(attribution["rows"]) == 3
    assert all(r["share"] is None for r in attribution["rows"]) and compare.NO_SHARE in attribution["note"]


def test_a_total_row_summed_in_floating_point_still_adds_up():
    """合计行是库里按浮点加出来的（0.1 + 0.2 + 0.3 = 0.6000000000000001）：差在最后几位也算相等，照给占比。"""
    base_rows = [["东区", 0.1, 0.0], ["南区", 0.2, 0.0], ["西区", 0.3, 0.0], ["合计", 0.1 + 0.2 + 0.3, 0.0]]
    next_rows = [["东区", 0.4, 0.0], ["南区", 0.2, 0.0], ["西区", 0.3, 0.0], ["合计", 0.4 + 0.2 + 0.3, 0.0]]

    def build(fetch, bot, rows, bot_rows):
        return [metric("all", "cell(nodes.fetch, 3, 'gmv')", rows[3][1], [cell_in(3, "gmv", rows[3][1], fetch)],
                       decimals=None)]
    found = by_id(run_compare(week("r1", base_rows, metrics=build), week("r2", next_rows, metrics=build)))["all"]
    rows = found["attribution"]["rows"]
    assert [r["key"] for r in rows] == [["东区"], ["南区"], ["西区"]]
    assert abs(sum(r["share"] for r in rows) - 1) < 1e-9


def test_date_typed_and_constant_columns_are_not_dimensions():
    """日期类型的列（两边有相同的日子也不算）和两边都只有一个值的列（币种）不当维度。"""
    columns = ["day", "region", "currency", "gmv"]
    types = {"day": "date", "region": "text", "currency": "text", "gmv": "number"}

    def make(run_id, rows):
        run = Fake(run_id)
        sql = "SELECT day, region, currency, SUM(amount) AS gmv FROM orders GROUP BY day, region, currency"
        bot = run.agent("bot", sql, columns, rows, types=types)
        run.card([metric("total", "sum(vars.kpi.amounts)", sum(r[3] for r in rows), [amounts_in(bot, rows)])])
        return run

    base_rows = [["2026-09-01", "东区", "CNY", 10.0], ["2026-09-02", "南区", "CNY", 20.0]]
    next_rows = [["2026-09-02", "东区", "CNY", 15.0], ["2026-09-03", "南区", "CNY", 18.0]]
    attribution = by_id(run_compare(make("r1", base_rows), make("r2", next_rows)))["total"]["attribution"]
    assert attribution["columns"] == ["region"]
    assert [(r["key"], r["delta"]) for r in attribution["rows"]] == [(["东区"], 5.0), (["南区"], -2.0)]


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


def test_a_truncated_base_snapshot_is_not_attributed_either():
    base = Fake("r1")
    fetch = base.tool("fetch", region_sql())
    bot = base.agent("bot", region_sql(), truncated=True)
    base.card(default_metrics(fetch, bot, BASE_ROWS, BASE_ROWS))
    total = by_id(run_compare(base, week("r2", NEXT_ROWS)))["total"]
    assert total["classification"] == "data_only"
    assert total["attribution"]["rows"] == [] and "基准运行" in total["attribution"]["note"]


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


def test_odd_values_stay_json_safe():
    """维度列里有非数、度量加起来超出浮点范围：结果照样写得进 JSON（接口按 allow_nan=False 写，写不进就是 500）。"""
    def make(run_id, rows):
        run = Fake(run_id)
        bot = run.agent("bot", region_sql(), ["region", "gmv"], rows, types={"region": "text", "gmv": "number"})
        # 卡上的值随便取一个有限的数（这里不重算它），两次不同就行
        run.card([metric("total", "sum(vars.kpi.amounts)", float(len(rows)), [amounts_in(bot, rows)])])
        return run

    nan = float("nan")
    result = run_compare(make("r1", [["东区", 1.0], [nan, 2.0]]), make("r2", [["东区", 1e308], ["东区", 1e308], [nan, 1.0]]))
    rows = {tuple(r["key"]): r for r in by_id(result)["total"]["attribution"]["rows"]}
    assert rows[("东区",)]["target"] is None and rows[("nan",)]["delta"] == -1.0


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


def test_a_masked_input_is_noted_even_without_attribution():
    """取数的 SQL 这次加了筛选（结构变了，不做归因），指标的输入取自遮罩列：整体说明照样提一句有列设了遮罩——输入的值
    不给，看的人要知道是遮掉了，不是没有。只在归因时才记，不归因的指标就漏了这一句。"""
    def masked(run):
        run.masked = {a: frozenset({"gmv"}) for a in run.queries}
        return run
    result = run_compare(masked(week("r1")), masked(week("r2", NEXT_ROWS, sql=region_sql(extra=" AND paid = 1"))))
    metrics = by_id(result)
    assert {m["classification"] for m in metrics.values()} == {"sql_changed"}
    assert all(m["attribution"] is None for m in metrics.values())
    assert metrics["east"]["inputs"][0]["masked"] is True and metrics["east"]["inputs"][0]["base"] is None
    assert compare.MASKED_NOTE in result["notes"]


# --------------------------------------------------------------------------
# 对不上具体查询的输入、运行输入、一边才有的指标
# --------------------------------------------------------------------------


def code_metrics(value):
    def build(fetch, bot, rows, bot_rows):
        return [metric("calc", "vars.adj * 2", value * 2, [scalar_in("vars.adj", value, "calc", "code")])]
    return build


def calc_node(code="print(1)"):
    return node("calc", "code", "调整", code=code, assign_to="adj")


def test_an_input_from_a_code_node_follows_its_upstream():
    """代码节点接在取数节点后面（不在 Agent 下游）：看它画布上的全部上游。"""
    def make(run_id, rows, value, sql=None):
        run = Fake(run_id, graph=weekly_graph(sql, after_fetch=(calc_node(),)))
        sql = sql or region_sql()
        run.tool("fetch", sql, rows=rows)
        run.agent("bot", sql, rows=rows)
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, rows, rows))
        return run

    # 上游什么都没变：变化来自数据
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10), make("r2", NEXT_ROWS, 12)))["calc"]
    assert calc["classification"] == "data_only" and calc["delta"] == 4
    assert calc["reasons"] == [compare.DATA_FROM_UPSTREAM]
    # 上游的 SQL 结构变了：说不清它变没变，不能报成数据变化
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10),
                             make("r2", NEXT_ROWS, 12, sql=region_sql(extra=" AND status = 'paid'"))))["calc"]
    assert calc["classification"] == "incomparable" and any("调整" in r for r in calc["reasons"])
    # 上游查到的数据两次一模一样，它交的数却变了：变化不来自数据
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10), make("r2", BASE_ROWS, 12)))["calc"]
    assert calc["classification"] == "incomparable" and any("调整" in r for r in calc["reasons"])
    assert compare.DATA_FROM_UPSTREAM not in calc["reasons"]


def test_a_code_node_after_an_agent_that_answered_differently_is_incomparable():
    """代码节点在 Agent 下游，算的是 Agent 没按出处交的占比：Agent 两次交出的结果不同，变化可能来自数据，也可能来自
    模型本身的差异，不能说变化来自上游数据。Agent 两次交出的一模一样时照常（见
    test_swapped_queries_with_the_same_literals_pair_up_exactly）。"""
    def make(run_id, rows, share, value=None):
        value = share * 2 if value is None else value
        run = Fake(run_id, graph=weekly_graph(between=(calc_node("result = nodes.bot.data.kpi.share * 2"),)))
        run.tool("fetch", region_sql(), rows=rows)
        run.plain_agent("bot", [region_sql()], {"kpi": {"share": share}}, rows=rows)
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, rows, rows))
        return run

    calc = by_id(run_compare(make("r1", BASE_ROWS, 0.46), make("r2", NEXT_ROWS, 0.52)))["calc"]
    assert calc["classification"] == "incomparable" and calc["attribution"] is None
    assert calc["reasons"] == ["输入「vars.adj」来自「调整」，它的上游节点「区域分析」（Agent）两次交出的结果不同，"
                               "无法判断变化是否来自数据"]
    # Agent 交的不同，代码节点交的数两次一样：照常没变，没有可说不清的变化
    calc = by_id(run_compare(make("r1", BASE_ROWS, 0.46, value=1.0), make("r2", NEXT_ROWS, 0.52, value=1.0)))["calc"]
    assert (calc["classification"], calc["reasons"]) == ("unchanged", [])

    # Agent 的产出两次都没落成工件（写工件失败）：一样不一样核对不了，同样不报成数据变化
    def unstored(run):
        run.events = [(seq, etype, nid, {k: v for k, v in data.items() if k != "artifact"}
                       if (etype, nid) == ("node.finished", "bot") else data) for seq, etype, nid, data in run.events]
        return run
    calc = by_id(run_compare(unstored(make("r1", BASE_ROWS, 0.46)),
                             unstored(make("r2", NEXT_ROWS, 0.46, value=1.0))))["calc"]
    assert calc["classification"] == "incomparable" and "区域分析" in calc["reasons"][0]


def test_a_reconfigured_node_upstream_of_a_code_node_is_named():
    """代码节点上游的数据整形节点改了表达式（SQL 一字没改）：代码节点交的数变了说不清从哪来，说明里点出改的是哪个节点；
    改了几个的，点名画布上最靠前的那个（节点 ID 的先后和画布上的先后不同）。"""
    def make(run_id, expressions, rows, value):
        shapes = [node(nid, "transform", label, mode="expression", expression=expression, assign_to=f"kept_{nid}")
                  for (nid, label), expression in zip((("shape_m", "整理"), ("shape_z", "再整理"), ("shape_a", "汇总")),
                                                      expressions)]
        run = Fake(run_id, graph=weekly_graph(after_fetch=(*shapes, calc_node())))
        run.tool("fetch", region_sql(), rows=rows)
        run.agent("bot", region_sql(), rows=rows)
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, rows, rows))
        return run

    same = ["nodes.fetch.rows"] * 3
    calc = by_id(run_compare(make("r1", same, BASE_ROWS, 10),
                             make("r2", ["nodes.fetch.rows[:2]", *same[1:]], NEXT_ROWS, 12)))["calc"]
    assert calc["classification"] == "incomparable"
    assert calc["reasons"] == ["输入「vars.adj」来自「调整」，上游节点「整理」的配置有变化，无法判断变化来源"]
    calc = by_id(run_compare(make("r1", same, BASE_ROWS, 10),
                             make("r2", ["nodes.fetch.rows[:2]"] * 3, NEXT_ROWS, 12)))["calc"]
    assert calc["reasons"] == ["输入「vars.adj」来自「调整」，上游节点「整理」的配置有变化，无法判断变化来源"]


def test_a_report_upstream_of_a_code_node_that_wrote_differently_is_incomparable():
    """代码节点上游有报告撰写节点（文字是模型写的），两次写得不同：代码节点交的数变了，可能来自数据，也可能来自模型
    本身的差异；两次写得一字不差的（产出工件一样）照常是数据变化。"""
    def make(run_id, rows, text, value):
        run = Fake(run_id, graph=weekly_graph(after_fetch=(node("draft", "report", "初稿"), calc_node())))
        run.tool("fetch", region_sql(), rows=rows)
        run.add("node.started", "draft", {"node_type": "report"})
        run.add("node.finished", "draft", {"artifact": run.put({"text": text})})
        run.agent("bot", region_sql(), rows=rows)
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, rows, rows))
        return run

    calc = by_id(run_compare(make("r1", BASE_ROWS, "上周初稿", 10), make("r2", NEXT_ROWS, "本周初稿", 12)))["calc"]
    assert calc["classification"] == "incomparable"
    assert calc["reasons"] == ["输入「vars.adj」来自「调整」，它的上游节点「初稿」（报告撰写）两次交出的结果不同，"
                               "无法判断变化是否来自数据"]
    calc = by_id(run_compare(make("r1", BASE_ROWS, "初稿", 10), make("r2", NEXT_ROWS, "初稿", 12)))["calc"]
    assert (calc["classification"], calc["reasons"]) == ("data_only", [compare.DATA_FROM_UPSTREAM])


def test_an_unknown_writer_is_looked_for_only_among_nodes_before_the_card():
    """两边都记不出 vars.adj 是哪个节点写的：只看口径卡之前可能执行过的节点。口径卡自己改了配置、它下游的写周报两次写得
    不同，都写不了它读到的值，不能点名成它的上游；它上游的节点改了配置照常说不清，并行支路上的节点也可能先写了变量，
    同样说不清，只是不说成「上游」。"""
    def make(run_id, rows, value, *, other="1", report="上周", reshape="nodes.fetch.rows", aside="nodes.fetch.rows"):
        graph = chain(node("start", "input", "开始"),
                      node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": region_sql()}),
                      node("shape", "transform", "整理", mode="expression", expression=reshape, assign_to="kept"),
                      node("calc", "code", "调整", code="x", assign_to="adj"),
                      node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1",
                           metrics=[{"id": "other", "expression": other}]),
                      node("write", "report", "写周报"), node("out", "output", "成果"))
        # 并行支路：开始 → 旁路整理 → 成果，和口径卡不在一条线上
        graph["nodes"].append(node("aside", "transform", "旁路整理", mode="expression", expression=aside,
                                   assign_to="extra"))
        graph["edges"] += [{"source": "start", "target": "aside"}, {"source": "aside", "target": "out"}]
        run = Fake(run_id, graph=graph)
        run.tool("fetch", region_sql(), rows=rows)
        run.code("calc", value, "adj")
        run.card([metric("calc", "vars.adj * 2", value * 2, [scalar_in("vars.adj", value, None, "state")])])
        run.add("node.started", "write", {"node_type": "report"})
        run.add("node.finished", "write", {"artifact": run.put({"text": report})})
        return run

    # 口径卡自己改了另一个指标的定义，写周报两次写得不同：都在它之后执行，变化照常来自上游数据
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10), make("r2", NEXT_ROWS, 12, other="2", report="本周")))["calc"]
    assert (calc["classification"], calc["reasons"]) == ("data_only", [compare.DATA_FROM_UPSTREAM_SCOPED])
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10), make("r2", NEXT_ROWS, 12, report="本周")))["calc"]
    assert (calc["classification"], calc["reasons"]) == ("data_only", [compare.DATA_FROM_UPSTREAM])
    # 口径卡上游的整理节点改了表达式
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10),
                             make("r2", NEXT_ROWS, 12, reshape="nodes.fetch.rows[:2]")))["calc"]
    assert calc["classification"] == "incomparable"
    assert calc["reasons"] == ["输入「vars.adj」不知道由哪个节点写入，上游节点「整理」的配置有变化，无法判断变化来源"]
    # 并行支路上的旁路整理改了表达式
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10),
                             make("r2", NEXT_ROWS, 12, aside="nodes.fetch.rows[:2]")))["calc"]
    assert calc["classification"] == "incomparable"
    assert calc["reasons"] == ["输入「vars.adj」不知道由哪个节点写入，节点「旁路整理」的配置有变化，无法判断变化来源"]


def test_an_unknown_writer_inside_a_loop_counts_the_nodes_on_the_loop_back():
    """口径卡在循环里：循环体是 调整 → 口径卡 → 收尾整理，收尾整理再连回循环。收尾整理在口径卡下游，也在它上游——
    下一轮的口径卡执行之前，上一轮的收尾整理已经执行过，可能先写了变量。它改了表达式，记不出写入者的输入说不清从哪来，
    不能当成「只在口径卡之后执行」排除掉（排除了就成了「它上游的节点都没有变化」）；没改的照常是数据变化。"""
    def make(run_id, rows, value, tail):
        graph = {"nodes": [node("start", "input", "开始"),
                           node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": region_sql()}),
                           node("loop", "loop", "逐周", mode="count", max_iterations=2),
                           node("calc", "code", "调整", code="x", assign_to="adj"),
                           node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1"),
                           node("tail", "transform", "收尾整理", mode="expression", expression=tail, assign_to="adj2"),
                           node("out", "output", "成果")],
                 "edges": [{"source": "start", "target": "fetch"}, {"source": "fetch", "target": "loop"},
                           {"source": "loop", "target": "calc", "sourceHandle": "body"},
                           {"source": "calc", "target": "card"}, {"source": "card", "target": "tail"},
                           {"source": "tail", "target": "loop"},
                           {"source": "loop", "target": "out", "sourceHandle": "done"}]}
        run = Fake(run_id, graph=graph)
        run.tool("fetch", region_sql(), rows=rows)
        run.code("calc", value, "adj")
        run.card([metric("calc", "vars.adj * 2", value * 2, [scalar_in("vars.adj", value, None, "state")])])
        return run

    calc = by_id(run_compare(make("r1", BASE_ROWS, 10, "1"), make("r2", NEXT_ROWS, 12, "2")))["calc"]
    assert calc["classification"] == "incomparable"
    assert calc["reasons"] == ["输入「vars.adj」不知道由哪个节点写入，上游节点「收尾整理」的配置有变化，无法判断变化来源"]
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10, "1"), make("r2", NEXT_ROWS, 12, "1")))["calc"]
    assert (calc["classification"], calc["reasons"]) == ("data_only", [compare.DATA_FROM_UPSTREAM])


def unknown_writer(run_id, rows, value, *, up="上周", aside="上周"):
    """记不出 vars.adj 是哪个节点写的：开始 → 取数 → 上游分析（Agent）→ 调整 → 口径卡 → 写周报 → 成果，另有一条并行
    支路 开始 → 旁路分析（Agent）→ 成果。up、aside 是两个 Agent 交出的文字。"""
    graph = chain(node("start", "input", "开始"),
                  node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": region_sql()}),
                  node("up", "agent", "上游分析", prompt="按区域写要点"),
                  node("calc", "code", "调整", code="x", assign_to="adj"),
                  node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1"),
                  node("write", "report", "写周报"), node("out", "output", "成果"))
    graph["nodes"].append(node("aside", "agent", "旁路分析", prompt="写一段旁注"))
    graph["edges"] += [{"source": "start", "target": "aside"}, {"source": "aside", "target": "out"}]
    run = Fake(run_id, graph=graph)
    run.tool("fetch", region_sql(), rows=rows)
    for node_id, text in (("up", up), ("aside", aside)):
        run.add("node.started", node_id, {"node_type": "agent"})
        run.add("node.finished", node_id, {"artifact": run.put({"text": text})})
    run.code("calc", value, "adj")
    run.card([metric("calc", "vars.adj * 2", value * 2, [scalar_in("vars.adj", value, None, "state")])])
    return run


def test_an_unknown_writer_names_a_node_that_answered_differently_by_where_it_sits():
    """记不出 vars.adj 是哪个节点写的，有 Agent 两次交出的不同（说不清）：在口径卡上游的说「上游节点」，并行支路上的
    只说「节点」——它和口径卡不在一条线上，说成上游就指错了位置；写入者不知道，都不能说「它的上游节点」，「它」指不出
    是谁（写入者知道时才这么说，见 test_a_code_node_after_an_agent_that_answered_differently_is_incomparable）。"""
    calc = by_id(run_compare(unknown_writer("r1", BASE_ROWS, 10), unknown_writer("r2", NEXT_ROWS, 12, up="本周")))["calc"]
    assert calc["classification"] == "incomparable"
    assert calc["reasons"] == ["输入「vars.adj」不知道由哪个节点写入，上游节点「上游分析」（Agent）两次交出的结果不同，"
                               "无法判断变化是否来自数据"]
    calc = by_id(run_compare(unknown_writer("r1", BASE_ROWS, 10),
                             unknown_writer("r2", NEXT_ROWS, 12, aside="本周")))["calc"]
    assert calc["classification"] == "incomparable"
    assert calc["reasons"] == ["输入「vars.adj」不知道由哪个节点写入，节点「旁路分析」（Agent）两次交出的结果不同，"
                               "无法判断变化是否来自数据"]
    # 两个都交得不同：点名画布上靠前的那个（口径卡上游的上游分析），不是后加在画布末尾的旁路分析
    calc = by_id(run_compare(unknown_writer("r1", BASE_ROWS, 10),
                             unknown_writer("r2", NEXT_ROWS, 12, up="本周", aside="本周")))["calc"]
    assert calc["reasons"] == ["输入「vars.adj」不知道由哪个节点写入，上游节点「上游分析」（Agent）两次交出的结果不同，"
                               "无法判断变化是否来自数据"]


def test_a_split_with_an_unknown_writer_leaves_out_the_card_and_what_follows_it():
    """口径变了（×2 → ×3、v1 → v2），旧口径用到的 vars.adj 记不出写入者，口径卡下游的写周报两次写得不同：严格拆分
    核对「输入只是数据变了」时，同样只看口径卡之前可能执行过的节点。口径卡自己改了版本、写周报在它之后执行，都写不了
    它读到的值，挡不住拆分，也不能点名成来源。"""
    def make(run_id, rows, value, factor, version, report):
        graph = chain(node("start", "input", "开始"),
                      node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": region_sql()}),
                      node("calc", "code", "调整", code="x", assign_to="adj"),
                      node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version=version),
                      node("write", "report", "写周报"), node("out", "output", "成果"))
        run = Fake(run_id, graph=graph)
        run.tool("fetch", region_sql(), rows=rows)
        run.code("calc", value, "adj")
        run.card([metric("calc", f"vars.adj * {factor}", value * factor,
                         [scalar_in("vars.adj", value, None, "state")])], version=version)
        run.add("node.started", "write", {"node_type": "report"})
        run.add("node.finished", "write", {"artifact": run.put({"text": report})})
        return run

    calc = by_id(run_compare(make("r1", BASE_ROWS, 10, 2, "v1", "上周"),
                             make("r2", NEXT_ROWS, 12, 3, "v2", "本周")))["calc"]
    assert calc["classification"] == "caliber_and_data"
    assert calc["reasons"] == ["表达式由「vars.adj * 2」改为「vars.adj * 3」", "口径版本由 v1 改为 v2"]
    # 20 → 36：口径效应 36 − 24 = 12，数据效应 24 − 20 = 4（24 是旧口径 ×2 配这次的 12）
    assert calc["decomposition"] == {"caliber_effect": 12, "data_effect": 4, "caliber_rendered": "+12.0元",
                                     "data_rendered": "+4.0元"}
    assert calc["decomposition_note"] is None


def test_a_node_after_the_card_in_one_run_only_is_still_after_it():
    """记不出 vars.adj 是哪个节点写的，口径卡下游的写周报只在一次运行的图里有（这次删掉了，或者这次才加上）：按两次
    运行合起来的图，它在口径卡之后执行，写不了口径卡读到的值，不点名、不挡住数据变化。只按这次的图认先后，删掉的写周报
    在这次的图里没有连线，就成了「和口径卡不在一条线上」，被说成来源；只按基准运行的图，这次才加上的也一样。"""
    def make(run_id, rows, value, with_report):
        nodes = [node("start", "input", "开始"),
                 node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": region_sql()}),
                 node("calc", "code", "调整", code="x", assign_to="adj"),
                 node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1"),
                 *([node("write", "report", "写周报")] if with_report else []),
                 node("out", "output", "成果")]
        run = Fake(run_id, graph=chain(*nodes))
        run.tool("fetch", region_sql(), rows=rows)
        run.code("calc", value, "adj")
        run.card([metric("calc", "vars.adj * 2", value * 2, [scalar_in("vars.adj", value, None, "state")])])
        return run

    for label, base, target in (("这次删掉了写周报", make("r1", BASE_ROWS, 10, True), make("r2", NEXT_ROWS, 12, False)),
                                ("这次才加上写周报", make("r1", BASE_ROWS, 10, False), make("r2", NEXT_ROWS, 12, True))):
        calc = by_id(run_compare(base, target))["calc"]
        assert (calc["classification"], calc["reasons"]) == ("data_only", [compare.DATA_FROM_UPSTREAM_SCOPED]), label


def test_a_run_input_upstream_of_a_code_node_is_a_parameter_change():
    """代码节点上游查到的数据两次一模一样，只有运行输入变了：它交的数变了算参数变化。"""
    def make(run_id, goal, value):
        run = Fake(run_id, graph=weekly_graph(after_fetch=(calc_node(),)), inputs={"goal": goal})
        run.add("node.started", "start", {"node_type": "input"})
        run.add("node.finished", "start", {"artifact": run.put({"goal": goal})})
        run.tool("fetch", region_sql())
        run.agent("bot", region_sql())
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, BASE_ROWS, BASE_ROWS))
        return run

    calc = by_id(run_compare(make("r1", 1000, 10), make("r2", 1500, 15)))["calc"]
    assert calc["classification"] == "params_only"
    assert calc["reasons"] == ["输入「vars.adj」来自「调整」，它的上游只有运行输入有变化"]


def test_a_value_from_a_querying_node_that_moved_without_its_data_is_incomparable():
    """调用工具节点整份交出的结果（不是 cell() 取的格）：它查到的行两次一样，交出的值（这里是查询耗时）却变了，
    变化不来自数据；查到的行也变了的，照常是数据变化。"""
    def make(run_id, elapsed, rows=BASE_ROWS, graph=None):
        run = Fake(run_id, graph=graph)
        run.tool("fetch", region_sql(), rows=rows)
        run.agent("bot", region_sql(), rows=rows)
        run.card([metric("cost", "vars.res.elapsed_ms", elapsed,
                         [scalar_in("vars.res.elapsed_ms", elapsed, "fetch", "tool")], unit="毫秒", decimals=None)])
        return run
    cost = by_id(run_compare(make("r1", 3), make("r2", 5)))["cost"]
    assert cost["classification"] == "incomparable" and any("区域取数" in r for r in cost["reasons"])
    cost = by_id(run_compare(make("r1", 3), make("r2", 5, NEXT_ROWS)))["cost"]
    assert cost["classification"] == "data_only"
    # 取数节点自己改了配置（SQL 没动）：它整份交出的结果可能跟着变，算模板变化
    graph = weekly_graph()
    graph["nodes"][1]["data"]["config"]["fail_fast"] = False
    cost = by_id(run_compare(make("r1", 3), make("r2", 5, NEXT_ROWS, graph)))["cost"]
    assert cost["classification"] == "sql_changed", cost["reasons"]
    assert cost["reasons"] == ["上游节点「区域取数」的配置有变化"]


def test_data_from_upstream_is_only_claimed_when_the_graph_is_the_same():
    """改了它下游的节点（成果字段）：它的上游没变，变化照样来自上游数据，但不能说「模板没有变化」。"""
    def make(run_id, rows, value, graph):
        run = Fake(run_id, graph=graph)
        run.tool("fetch", region_sql(), rows=rows)
        run.agent("bot", region_sql(), rows=rows)
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, rows, rows))
        return run

    base_graph = weekly_graph(after_fetch=(calc_node(),))
    target_graph = copy.deepcopy(base_graph)
    target_graph["nodes"][-1]["data"]["config"] = {"fields": [{"name": "周报", "value": "{{ nodes.write.text }}"}]}
    calc = by_id(run_compare(make("r1", BASE_ROWS, 10, base_graph), make("r2", NEXT_ROWS, 12, target_graph)))["calc"]
    assert calc["classification"] == "data_only"
    assert calc["reasons"] == [compare.DATA_FROM_UPSTREAM_SCOPED]


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


def test_rewired_edges_are_a_template_change():
    """调整节点改由取数节点接进来（各节点的配置都没动）：它交的数变了算模板变化，不能说变化来自上游数据。"""
    calc_node = node("calc", "code", "调整", code="result = 1", assign_to="adj")
    base_graph = weekly_graph(between=(calc_node,))
    target_graph = copy.deepcopy(base_graph)
    target_graph["edges"] = [e for e in target_graph["edges"] if (e["source"], e["target"]) != ("bot", "calc")]
    target_graph["edges"].append({"source": "fetch", "target": "calc"})

    def make(run_id, graph, value):
        run = Fake(run_id, graph=graph)
        run.tool("fetch", region_sql())
        run.agent("bot", region_sql())
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, BASE_ROWS, BASE_ROWS))
        return run
    result = run_compare(make("r1", base_graph, 10), make("r2", target_graph, 12))
    calc = by_id(result)["calc"]
    assert calc["classification"] == "sql_changed" and any("连线" in r for r in calc["reasons"]), calc["reasons"]
    assert compare.EDGES_CHANGED in result["notes"]


def uncited(run_id, share, *, graph=None, sqls=None):
    """Agent 没开按出处核对，口径卡用了它交的占比 vars.kpi.share；东区金额照旧取取数节点的那一格。"""
    run = Fake(run_id, graph=graph)
    fetch = run.tool("fetch", region_sql())
    run.plain_agent("bot", sqls or [region_sql()], {"kpi": {"share": share}})
    run.card([metric("east", EAST, BASE_ROWS[0][1], [cell_in(0, "gmv", BASE_ROWS[0][1], fetch)], name="东区金额"),
              metric("share", "vars.kpi.share * 100", share * 100, [scalar_in("vars.kpi.share", share, "bot", "agent")],
                     unit="%", fmt="plain")])
    return run


def test_a_number_the_agent_gave_without_citing_is_incomparable():
    """Agent 没按出处交的数：两次查到的数据一样，数却变了（模型本身的差异），不能说是数据变化。"""
    result = run_compare(uncited("r1", 0.46), uncited("r2", 0.52))
    share = by_id(result)["share"]
    assert share["classification"] == "incomparable" and share["attribution"] is None
    assert any("区域分析" in r and "查询快照" in r for r in share["reasons"]), share["reasons"]
    assert by_id(result)["east"]["classification"] == "unchanged"
    # 交的数一样：照常没变
    assert by_id(run_compare(uncited("r1", 0.46), uncited("r2", 0.46)))["share"]["classification"] == "unchanged"


def test_an_extra_query_by_the_writer_is_a_sql_change():
    extra = "SELECT COUNT(*) AS n FROM refunds"
    share = by_id(run_compare(uncited("r1", 0.46), uncited("r2", 0.52, sqls=[region_sql(), extra])))["share"]
    assert share["classification"] == "sql_changed" and any("多了一次查询" in r for r in share["reasons"])
    # 反过来这次少查了一次：同样是取数变了
    share = by_id(run_compare(uncited("r1", 0.46, sqls=[region_sql(), extra]), uncited("r2", 0.52)))["share"]
    assert share["classification"] == "sql_changed" and any("少了一次查询" in r for r in share["reasons"])


def test_an_input_on_a_snapshot_on_one_side_only_is_incomparable():
    """基准运行没找到东区金额那一格落在哪份快照上（卡上只记了值），这次找到了：只有一边对得上查询快照，比不了取数。"""
    def make(run_id, rows, *, located):
        run = Fake(run_id)
        fetch = run.tool("fetch", region_sql(), rows=rows)
        run.agent("bot", region_sql(), rows=rows)
        item = cell_in(0, "gmv", rows[0][1], fetch)
        if not located:
            kept = {k: v for k, v in item.items() if k not in ("artifact", "locator", "eid")}
            item = {**kept, "status": "unresolved"}
        run.card([metric("east", EAST, rows[0][1], [item], name="东区金额")])
        return run

    east = by_id(run_compare(make("r1", BASE_ROWS, located=False), make("r2", NEXT_ROWS, located=True)))["east"]
    assert east["classification"] == "incomparable" and east["attribution"] is None
    assert east["reasons"] == [f"输入「{EAST}」只有一边对得上查询快照，无法比较取数"]


def test_a_changed_default_model_is_a_template_change_for_agents_that_follow_it():
    base_graph = weekly_graph()
    base_graph["defaults"] = {"provider": "anthropic", "model": "model-a"}
    target_graph = copy.deepcopy(base_graph)
    target_graph["defaults"]["model"] = "model-b"
    result = run_compare(uncited("r1", 0.46, graph=base_graph), uncited("r2", 0.52, graph=target_graph))
    share = by_id(result)["share"]
    assert share["classification"] == "sql_changed"
    assert share["reasons"] == ["上游节点「区域分析」跟随的工作流默认设置（模型）有变化"]
    assert result["template"]["same_graph"] is False and compare.DEFAULTS_CHANGED in result["notes"]
    # 取数节点不用模型：东区金额不受影响
    assert by_id(result)["east"]["classification"] == "unchanged"
    # 按出处交的字段：取法可能跟着模型变了
    total = by_id(run_compare(week("r1", graph=base_graph), week("r2", NEXT_ROWS, graph=target_graph)))["total"]
    assert total["classification"] == "sql_changed"
    assert total["reasons"] == ["上游节点「区域分析」跟随的工作流默认设置（模型）有变化，它按出处交来的字段可能换了取法"]
    # 一次改了好几项：写界面上的叫法（模型接入不写 provider），多的只写项数
    many = copy.deepcopy(base_graph)
    many["defaults"] = {"provider": "openai", "model": "model-b", "temperature": 0.5, "max_tokens": 2000}
    share = by_id(run_compare(uncited("r1", 0.46, graph=base_graph), uncited("r2", 0.52, graph=many)))["share"]
    assert share["reasons"] == ["上游节点「区域分析」跟随的工作流默认设置（模型接入、模型、温度等 4 项）有变化"]
    # 同时改了 Agent 不读的结论句裁判：只写它跟着变的模型
    judged = copy.deepcopy(target_graph)
    judged["defaults"]["judge"] = {"max_cost_usd": 1.0}
    share = by_id(run_compare(uncited("r1", 0.46, graph=base_graph), uncited("r2", 0.52, graph=judged)))["share"]
    assert share["reasons"] == ["上游节点「区域分析」跟随的工作流默认设置（模型）有变化"]
    # Agent 自己指定了模型：默认设置变了它不跟着变
    for graph in (base_graph, target_graph):
        graph["nodes"][2]["data"]["config"]["model"] = "model-x"
    share = by_id(run_compare(uncited("r1", 0.46, graph=base_graph), uncited("r2", 0.52, graph=target_graph)))["share"]
    assert share["classification"] == "incomparable"


def test_defaults_that_agents_do_not_read_leave_them_alone():
    """只改了 Agent 不读的默认设置：结论句裁判、结论句的处理只有报告节点读，审批策略只管调用工具前问不问人。按出处
    交的字段、取数节点的格子照常是数据变化。"""
    for before, after in (({"judge": {"max_cost_usd": 0.5}}, {"judge": {"max_cost_usd": 1.0}}),
                          ({"claims": "off"}, {"claims": "judge"}),
                          ({"approval": "dangerous"}, {"approval": "never"})):
        base_graph, target_graph = weekly_graph(), weekly_graph()
        base_graph["defaults"], target_graph["defaults"] = before, after
        result = run_compare(week("r1", graph=base_graph), week("r2", NEXT_ROWS, graph=target_graph))
        found = {m["metric_id"]: (m["classification"], m["reasons"]) for m in result["metrics"]}
        assert found == {"east": ("data_only", [compare.DATA_FROM_QUERIES]),
                         "total": ("data_only", [compare.DATA_FROM_QUERIES])}, (before, found)
        assert compare.DEFAULTS_CHANGED in result["notes"]


def test_a_model_default_reaches_every_node_that_calls_the_model():
    """默认模型换了，跟着换的不只 Agent：让模型分类的条件分支、让模型修复的结构校验（默认开着）、用模型重排的知识检索
    也是。它们在代码节点上游时，代码节点交的数变了不能说来自上游数据；不调模型的照常。"""
    def calc(extra, before=None, after=None):
        def make(run_id, defaults, rows, value):
            graph = weekly_graph(after_fetch=(extra, calc_node()))
            graph["defaults"] = defaults
            run = Fake(run_id, graph=graph)
            run.tool("fetch", region_sql(), rows=rows)
            run.agent("bot", region_sql(), rows=rows)
            run.code("calc", value, "adj")
            run.card(code_metrics(value)(None, None, rows, rows))
            return run
        before = before or {"provider": "anthropic", "model": "model-a"}
        after = after or {"provider": "anthropic", "model": "model-b"}
        return by_id(run_compare(make("r1", before, BASE_ROWS, 10), make("r2", after, NEXT_ROWS, 12)))["calc"]

    cases = [{"key": "peak", "condition": "true"}]
    for extra in (node("route", "branch", "分流", mode="llm", instruction="判断是否旺季", cases=cases),
                  node("route", "validate", "校验", schema={"type": "object"}),
                  node("route", "retrieve", "检索", query="旺季", rerank="model")):
        found = calc(extra)
        assert found["classification"] == "incomparable", (extra["type"], found["reasons"])
        assert found["reasons"] == [f"输入「vars.adj」来自「调整」，上游节点「{extra['data']['label']}」跟随的工作流默认"
                                    "设置（模型）有变化，无法判断变化来源"]
    for extra in (node("route", "branch", "分流", mode="expression", cases=cases),
                  node("route", "validate", "校验", schema={"type": "object"}, repair_with_llm=False),
                  node("route", "retrieve", "检索", query="旺季")):
        found = calc(extra)
        assert found["classification"] == "data_only", (extra["type"], found["reasons"])

    # 调不调模型也可能由默认设置定：条件分支没写判断方式、默认设置写了让模型判断的，跟着默认模型变；结构校验的默认
    # 设置关了让模型修复的，不跟着变
    asks = node("route", "branch", "分流", instruction="判断是否旺季", cases=cases)
    found = calc(asks, {"mode": "llm", "model": "model-a"}, {"mode": "llm", "model": "model-b"})
    assert found["classification"] == "incomparable", found["reasons"]
    assert found["reasons"] == ["输入「vars.adj」来自「调整」，上游节点「分流」跟随的工作流默认设置（模型）有变化，"
                                "无法判断变化来源"]
    found = calc(node("route", "validate", "校验", schema={"type": "object"}),
                 {"repair_with_llm": False, "model": "model-a"}, {"repair_with_llm": False, "model": "model-b"})
    assert found["classification"] == "data_only", found["reasons"]
    # 默认的判断方式这次改成了按表达式：基准运行那次让模型判断过，跟着变的是判断方式和模型两项
    found = calc(asks, {"mode": "llm", "model": "model-a"}, {"mode": "expression", "model": "model-b"})
    assert found["reasons"] == ["输入「vars.adj」来自「调整」，上游节点「分流」跟随的工作流默认设置（判断方式、模型）"
                                "有变化，无法判断变化来源"]


#: 只顺着节点模块里的函数往下找：别处的函数不经 NodeContext.cfg 读节点配置
_NODE_MODULES = "app.engine.nodes."


def _defined_here(obj):
    """obj 是定义在节点模块里的函数或类（装饰过的取原函数），否则 None。"""
    obj = inspect.unwrap(obj) if inspect.isfunction(obj) else getattr(obj, "func", obj)
    if (inspect.isfunction(obj) or inspect.isclass(obj)) and obj.__module__.startswith(_NODE_MODULES):
        return obj
    return None


def _settings_read(runner):
    """一类节点的执行器运行时按 NodeContext.cfg 读的配置项（节点上没写就回退到默认设置）：执行器自己的函数体，加上它
    用到的、定义在节点模块里的函数和类，一层层往下找（函数里就地 import 的也算）。返回 (配置项, 认不出键名的读法,
    走到的函数)。键名是变量的，只认在字面量元组上循环出来的（Agent 的预算三项）；别的变量键名认不出是哪几项，
    原样交回，测试据此失败。"""
    read, unknown, seen = set(), [], set()
    todo = [runner]
    while todo:
        fn = _defined_here(todo.pop())
        if fn is None or (fn.__module__, fn.__qualname__) in seen:
            continue
        seen.add((fn.__module__, fn.__qualname__))
        names = vars(importlib.import_module(fn.__module__))        # 它所在模块里的名字（类没有 __globals__）
        parts = list(ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))))
        imported = {alias.asname or alias.name: (part.module, alias.name) for part in parts
                    if isinstance(part, ast.ImportFrom) and part.module for alias in part.names}
        looped = {}         # 循环变量 → 它在字面量元组上取到的键名：{k: ctx.cfg(k) for k in ("max_steps", …)}
        for part in parts:
            for gen in getattr(part, "generators", None) or ([part] if isinstance(part, ast.For) else []):
                if isinstance(gen.target, ast.Name) and isinstance(gen.iter, (ast.Tuple, ast.List)) and all(
                        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in gen.iter.elts):
                    looped[gen.target.id] = {e.value for e in gen.iter.elts}
        for part in parts:
            called = part.func if isinstance(part, ast.Call) else None
            if isinstance(called, ast.Attribute) and called.attr == "cfg":
                key = part.args[0] if part.args else None
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    read.add(key.value)
                elif isinstance(key, ast.Name) and key.id in looped:
                    read |= looped[key.id]
                else:
                    unknown.append(f"{fn.__qualname__}: {ast.unparse(part)}")
            elif isinstance(called, ast.Attribute) and called.attr == "approval_mode":
                read.add("approval")        # NodeContext.approval_mode 同样先读节点、再回退到默认设置
            elif isinstance(called, ast.Name) and called.id == "_setting":
                # 报告模块的 _setting(spec, 节点, 键) 和 NodeContext.cfg 同一个规则
                key = part.args[2] if len(part.args) > 2 else None
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    read.add(key.value)
                else:
                    unknown.append(f"{fn.__qualname__}: {ast.unparse(part)}")
            elif isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load):
                # 调用、当回调传出去的函数都算用到了
                target = names.get(part.id)
                if target is None and part.id in imported:
                    module, name = imported[part.id]
                    target = getattr(importlib.import_module(module), name, None)
                todo.append(target)
            elif isinstance(part, ast.Attribute) and isinstance(part.value, ast.Name):
                module = names.get(part.value.id)
                if inspect.ismodule(module):
                    todo.append(getattr(module, part.attr, None))
    return read, unknown, seen


def test_every_setting_a_node_reads_is_known_to_the_comparison():
    """节点运行时按 NodeContext.cfg 读配置，节点上没写就回退到图级默认设置。对比按「哪类节点读哪几项」把默认设置的
    变化落到节点上（compare._CFG_KEYS）：执行器新读了一项、这里没登记，默认设置改了这一项就会被漏掉；登记了不读的，
    默认设置改了它就会被误标。按节点类型比，不按模块比：一个模块管几类节点（Agent 和模型调用、调用工具和代码……），
    其中一类读了、另一类没读的，不能拿另一类的登记充数。"""
    reached = set()
    for kind, runner in RUNNERS.items():
        read, unknown, seen = _settings_read(runner)
        assert not unknown, (str(kind), unknown)
        known = compare._CFG_KEYS.get(str(kind), frozenset()) | compare._CFG_KEYS_IF.get(str(kind), frozenset())
        assert read == known, (str(kind), "没登记", sorted(read - known), "登记了不读", sorted(known - read))
        reached |= seen
    # 键名不是字面量的读法也认得出来
    assert {"max_steps", "budget_tokens", "budget_usd"} <= _settings_read(RUNNERS[NodeType.AGENT])[0]
    # 节点模块里每个按 cfg 读配置的函数都走到了：走不到的说明上面漏了某种用法，查出来的配置项就不全
    holders = set()
    for info in pkgutil.iter_modules(nodes_package.__path__):
        module = importlib.import_module(_NODE_MODULES + info.name)
        for top in ast.parse(inspect.getsource(module)).body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("cfg", "approval_mode") for n in ast.walk(top)):
                holders.add((module.__name__, top.name))
    assert holders and holders <= reached, sorted(holders - reached)


def cited(run_id, rows=BASE_ROWS, *, column="gmv", span=None, sql=None):
    """Agent 按出处交回数组字段：引用快照的哪一列、哪一段行由调用方定。"""
    run = Fake(run_id)
    sql = sql or region_sql()
    run.tool("fetch", sql, rows=rows)
    bot = run.agent("bot", sql, rows=rows, column=column, span=span)
    first, last = span or (0, len(rows) - 1)
    values = [r[COLUMNS.index(column)] for r in rows[first:last + 1]]
    run.card([metric("total", "sum(vars.kpi.amounts)", sum(values),
                     [amounts_in(bot, rows, column=column, span=span)])])
    return run


def test_an_agent_citing_another_column_is_not_a_data_change():
    """SQL 一字不差、数据一模一样，Agent 这次引用的是退款列：取的不是同一个数。"""
    total = by_id(run_compare(cited("r1"), cited("r2", column="refund")))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert any("gmv" in r and "refund" in r for r in total["reasons"]), total["reasons"]


def test_an_agent_citing_other_rows_is_not_a_data_change():
    """Agent 这次只引用了后两行：东区的数还在快照里，不能把它算成「东区的数据没了」。"""
    total = by_id(run_compare(cited("r1"), cited("r2", span=(1, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert any("行" in r for r in total["reasons"]), total["reasons"]
    # 起始行一样、只差最后一行（按金额取前 3 改成前 4，多引用的那一行是这次新出现的北区）：同样不是同一组数。只比起始行
    # 会走到按区域核对，北区只在这次有、算两边都整个被引用，就报成北区多了 400、占比 67%
    top = region_sql().replace("ORDER BY region", "ORDER BY gmv DESC")
    total = by_id(run_compare(
        cited("r1", [["东区", 1200.5, 100.0], ["南区", 800.0, 50.0], ["西区", 600.0, 0.0], ["中区", 30.0, 0.0]],
              span=(0, 2), sql=top),
        cited("r2", [["东区", 1500.5, 120.0], ["南区", 700.0, 40.0], ["西区", 600.0, 10.0], ["北区", 400.0, 0.0],
                     ["中区", 35.0, 0.0]], span=(0, 3), sql=top)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」引用的行由第 1 至 3 行改为第 1 至 4 行，取的不是同一组数"]
    # 两边引用的都是整份结果（这次多了一个区域）：照常是数据变化
    total = by_id(run_compare(cited("r1"), cited("r2", [*NEXT_ROWS, ["北区", 90.0, 0.0]])))["total"]
    assert total["classification"] == "data_only" and total["attribution"]["rows"]


#: 按区域排序的结果这次多了北区，排在南区前面，西区挪到第 4 行
#: 区域列设了遮罩、西区挪出引用的第 1 至 3 行时的说明：只写行号，不写是哪个区域
MASKED_MOVED = ("Agent「区域分析」两次都引用第 1 至 3 行，其中一个维度值这次仍在查询结果第 4 行，不在引用的行里，"
                "取的不是同一组数（维度列设了遮罩，不写出是哪个值）")
WITH_NORTH = [["东区", 1200.5, 100.0], ["北区", 90.0, 0.0], ["南区", 800.0, 50.0], ["西区", 600.0, 0.0]]


def test_the_same_rows_are_not_the_same_citation_when_only_one_side_is_the_whole_result():
    """Agent 两次都引用第 1 至 3 行：基准运行里那是整份结果，这次西区还在第 4 行、没被引用。行段一样也不是同一处，
    不能把西区算成「数据没了」（占比也会超过 100%）；反过来（基准只引用了一部分、这次是整份）也一样。"""
    total = by_id(run_compare(cited("r1"), cited("r2", WITH_NORTH, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」在基准运行中引用了整份结果，这次只引用了第 1 至 3 行（结果共 4 行），"
                                "取的不是同一组数"]
    total = by_id(run_compare(cited("r1", WITH_NORTH, span=(0, 2)), cited("r2")))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」在基准运行中只引用了第 1 至 3 行（结果共 4 行），这次引用了整份结果，"
                                "取的不是同一组数"]
    # 两边都只引用了前 3 行、都不是整份结果：同一处，照常是数据变化
    later = [["东区", 1500.5, 120.0], ["北区", 95.0, 0.0], ["南区", 700.0, 40.0], ["西区", 600.0, 10.0]]
    total = by_id(run_compare(cited("r1", WITH_NORTH, span=(0, 2)), cited("r2", later, span=(0, 2))))["total"]
    assert total["classification"] == "data_only" and total["attribution"]["rows"]


def test_the_same_rows_are_not_the_same_citation_when_a_cited_region_moved_out_of_them():
    """两边都只引用第 1 至 3 行、都不是整份结果：基准运行引用了西区，这次北区排进了前 3 行，西区挪到第 4 行，还在这次
    的快照里，只是没被引用。取的不是同一组数，不能把西区算成「数据没了」（占比会远超 100%）；反过来（这次引用的区域
    在基准运行的快照里、没被引用）也一样。西区这次真的没有了的，照常是数据变化。"""
    with_middle = [["东区", 1200.5, 100.0], ["南区", 800.0, 50.0], ["西区", 600.0, 0.0], ["中区", 30.0, 0.0]]
    total = by_id(run_compare(cited("r1", with_middle, span=(0, 2)), cited("r2", WITH_NORTH, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 3 行，「西区」这次仍在查询结果第 4 行，不在引用的"
                                "行里，取的不是同一组数"]
    total = by_id(run_compare(cited("r1", WITH_NORTH, span=(0, 2)), cited("r2", with_middle, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 3 行，「西区」在基准运行中位于查询结果第 4 行，不在"
                                "引用的行里，取的不是同一组数"]

    # 按金额从大到小取前 3 名：北区这次挤进了前 3，西区落到第 4 行
    top = region_sql().replace("ORDER BY region", "ORDER BY gmv DESC")
    before = [["东区", 1200.0, 0.0], ["南区", 800.0, 0.0], ["西区", 600.0, 0.0], ["中区", 30.0, 0.0]]
    after = [["东区", 1200.0, 0.0], ["南区", 800.0, 0.0], ["北区", 700.0, 0.0], ["西区", 600.0, 0.0]]
    total = by_id(run_compare(cited("r1", before, span=(0, 2), sql=top),
                              cited("r2", after, span=(0, 2), sql=top)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert "「西区」这次仍在查询结果第 4 行" in total["reasons"][0]
    # 对照：西区这次真的没有了（不在这次的快照里），照常是数据变化，按区域归因，占比之和为 1
    gone = [["东区", 1200.0, 0.0], ["南区", 800.0, 0.0], ["北区", 700.0, 0.0], ["中区", 30.0, 0.0]]
    total = by_id(run_compare(cited("r1", before, span=(0, 2), sql=top),
                              cited("r2", gone, span=(0, 2), sql=top)))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["北区"], None, 700.0, 7.0), (["西区"], 600.0, None, -6.0), (["东区"], 1200.0, 1200.0, 0.0),
        (["南区"], 800.0, 800.0, 0.0)]
    assert sum(exact(r["share"]) for r in rows) == 1
    # 对照：前 3 名还是那三个区域，只有排在引用的行后面的第 4 名每周不同（中区换成北区）——两边都没引用的区域不进
    # 核对，照常是数据变化（最常见的取前几名；连它也核对，每周的对比就都说不清了）
    tail = [["东区", 1300.0, 0.0], ["南区", 700.0, 0.0], ["西区", 650.0, 0.0], ["北区", 20.0, 0.0]]
    total = by_id(run_compare(cited("r1", before, span=(0, 2), sql=top),
                              cited("r2", tail, span=(0, 2), sql=top)))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["东区"], 1200.0, 1300.0, 2.0), (["南区"], 800.0, 700.0, -2.0), (["西区"], 600.0, 650.0, 1.0)]
    assert sum(exact(r["share"]) for r in rows) == 1

    # 区域列设了遮罩：遮住的区域名不进说明，但照样拿它核对（它是唯一认得出行的那一列，不核对就和真的没有了分不出来）——
    # 说明只写行号
    def masked(run):
        run.masked = {a: frozenset({"region"}) for a in run.queries}
        return run
    result = run_compare(masked(cited("r1", with_middle, span=(0, 2))), masked(cited("r2", WITH_NORTH, span=(0, 2))))
    total = by_id(result)["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == [MASKED_MOVED]
    text = json.dumps(result, ensure_ascii=False)
    assert all(region not in text for region in ("东区", "南区", "西区", "北区", "中区"))


def test_a_mask_on_one_side_only_still_hides_the_column():
    """只有一边的快照遮了区域列（查询当时设了遮罩，后来撤了；或者反过来）：两边的区域名是同一批字，哪一边遮了都算
    遮了，不拿它认是哪一区，也不写进说明、归因、整体说明。区域挪出了引用的行（西区落到第 4 行）时，只看基准那一边的
    遮罩会写出「西区」，只看这次那一边的也会。"""
    with_middle = [["东区", 1200.5, 100.0], ["南区", 800.0, 50.0], ["西区", 600.0, 0.0], ["中区", 30.0, 0.0]]

    def masked(run):
        run.masked = {a: frozenset({"region"}) for a in run.queries}
        return run

    for label, base, target in (
            ("只有这次运行遮了", cited("r1", with_middle, span=(0, 2)), masked(cited("r2", WITH_NORTH, span=(0, 2)))),
            ("只有基准运行遮了", masked(cited("r1", with_middle, span=(0, 2))), cited("r2", WITH_NORTH, span=(0, 2)))):
        result = run_compare(base, target)
        total = by_id(result)["total"]
        # 说明、归因、整体说明里都不出现区域名（遮住的那一列的值），整份结果里也不出现
        for where, part in (("reasons", total["reasons"]), ("attribution", total["attribution"]),
                            ("notes", result["notes"]), ("整份结果", result)):
            text = json.dumps(part, ensure_ascii=False)
            leaked = [r for r in ("东区", "南区", "西区", "北区", "中区") if r in text]
            assert not leaked, (label, where, leaked)
        # 照样拿遮住的区域列核对出西区挪出了引用的行，说明只写行号（同上一条用例的遮罩那一段）
        assert (total["classification"], total["reasons"]) == ("incomparable", [MASKED_MOVED]), label
        # 整体说明照样提一句有列设了遮罩：归因少了区域这一列，看的人要知道是遮掉了，不是没有
        assert compare.MASKED_NOTE in result["notes"], label


def test_an_input_cites_the_whole_result_only_when_it_covers_every_row():
    """怎么认整份结果（compare._whole）：容器本身就是一份查询结果的（没有行号、行段）、数组字段的行段从第一行到最后
    一行的是；单格、只有一段行的不是；快照读不出来的不知道。Agent 的字段在真实运行里总带着行号或行段，容器这一支
    只是兜底，造不出真实的运行来走它，在这里直接核对。"""
    query = compare._Query(artifact="q1", node_id="bot", tool=TOOL, ordinal=1, snap={"rows": [["东区"], ["南区"], ["西区"]]})
    lost = compare._Query(artifact="q1", node_id="bot", tool=TOOL, ordinal=1)

    def cites(row=None, rows=None):
        return SimpleNamespace(row=row, rows=rows)
    assert compare._whole(cites(), query) is True
    assert compare._whole(cites(rows=(0, 2)), query) is True
    assert compare._whole(cites(rows=(0, 1)), query) is False
    assert compare._whole(cites(row=0), query) is False
    assert compare._whole(cites(rows=(0, 2)), lost) is None


def test_cited_rows_on_an_unreadable_snapshot_are_not_taken_as_the_same():
    """基准运行 Agent 那次查询的快照文件不在了：两边都引用第 1 至 3 行，可基准那次是不是整份结果核对不了，不报成
    数据变化。"""
    base = cited("r1")
    del base.store[base.queries[1]]
    total = by_id(run_compare(base, cited("r2", WITH_NORTH, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable"
    assert total["reasons"] == ["基准运行的查询快照无法读取，无法核对 Agent「区域分析」引用的是不是整份结果"]
    # 这次引用的是整份结果、基准那次的行段不同：同样核对不了是不是两边都是整份
    total = by_id(run_compare(base, cited("r2", WITH_NORTH)))["total"]
    assert total["classification"] == "incomparable" and "无法核对" in total["reasons"][0]

    # 反过来只有这次运行的快照读不出来：说的是这次运行
    def unreadable(run):
        del run.store[run.queries[1]]
        return run
    total = by_id(run_compare(cited("r1"), unreadable(cited("r2", WITH_NORTH, span=(0, 2)))))["total"]
    assert total["classification"] == "incomparable"
    assert total["reasons"] == ["这次运行的查询快照无法读取，无法核对 Agent「区域分析」引用的是不是整份结果"]
    # 基准运行只引用了一部分、这次的行段又不同：这次是不是整份结果都不是同一处，照实说行段换了
    total = by_id(run_compare(cited("r1", WITH_NORTH, span=(0, 2)), unreadable(cited("r2", span=(1, 2)))))["total"]
    assert total["classification"] == "incomparable"
    assert total["reasons"] == ["Agent「区域分析」引用的行由第 1 至 3 行改为第 2 至 3 行，取的不是同一组数"]


def element(run_id, rows, span, *, index=0, sql=None, columns=COLUMNS, types=TYPES):
    """口径卡取 Agent 数组字段 amounts 的第 index 个元素（默认 vars.kpi.amounts[0]）：落在快照上的是 Agent 交的行段的
    第 span[0] + index 行。sql、columns、types 换查询的形状（默认是按区域汇总的那条）。指标的 id 总是 first。"""
    run = Fake(run_id)
    sql = sql or region_sql()
    run.tool("fetch", sql, columns, rows, types=types)
    bot = run.agent("bot", sql, columns, rows, types=types, span=span)
    row, at = span[0] + index, columns.index("gmv")
    item = {"path": f"vars.kpi.amounts[{index}]", "value": rows[row][at], "node_id": "bot", "via": "agent_field",
            "field": f"amounts[{index}]", "ref": f"Q1.r{row}.gmv", "artifact": bot,
            "locator": {"row": row, "column": "gmv"}, "eid": f"ev:cell:{bot[:8]}:{row}:gmv", "status": "verified"}
    run.card([metric("first", f"vars.kpi.amounts[{index}]", rows[row][at], [item])])
    return run


def test_an_element_of_a_cited_array_follows_its_row():
    """口径卡取 Agent 数组字段里的第一个元素：Agent 交的行段换了起点，第一个元素落到了别的行，不是同一个数；起点没变
    的照常是数据变化。"""
    first = by_id(run_compare(element("r1", BASE_ROWS, (0, 2)), element("r2", NEXT_ROWS, (1, 2))))["first"]
    assert first["classification"] == "incomparable"
    assert first["reasons"] == ["Agent「区域分析」引用的行由第 1 行改为第 2 行，取的不是同一个数"]
    first = by_id(run_compare(element("r1", BASE_ROWS, (0, 2)), element("r2", NEXT_ROWS, (0, 2))))["first"]
    assert (first["classification"], first["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    # 起点没变，可这次北区排在了第一行：基准运行取的是东区，东区这次还在第 2 行，取的不是同一个数
    first = by_id(run_compare(element("r1", BASE_ROWS, (0, 2)),
                              element("r2", [["北区", 90.0, 0.0], *NEXT_ROWS], (0, 3))))["first"]
    assert first["classification"] == "incomparable"
    assert first["reasons"] == ["Agent「区域分析」两次都引用第 1 行，「东区」这次仍在查询结果第 2 行，不在引用的行里，"
                                "取的不是同一个数"]
    # 反方向：基准运行北区排在第 1 行、东区在第 2 行，这次北区没了、东区到了第 1 行。这次取的东区在基准运行的快照里，
    # 只是不在这一行，同样不是同一个数（只查「基准运行取的北区这次还在不在」，北区这次没了，会把北区 90 和东区
    # 1500.5 当成同一个数报成数据变化）
    first = by_id(run_compare(element("r1", [["北区", 90.0, 0.0], *BASE_ROWS], (0, 3)),
                              element("r2", NEXT_ROWS, (0, 2))))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["Agent「区域分析」两次都引用第 1 行，「东区」在基准运行中位于查询结果第 2 行，不在引用的"
                                "行里，取的不是同一个数"]


def test_an_element_on_an_unreadable_snapshot_is_not_taken_as_the_same():
    """口径卡取 Agent 数组字段的第一个元素，两次都是第 1 行，可有一边 Agent 那次查询的快照读不出来：那一行两次是不是
    同一个区域核对不了（这次北区排到了第 1 行，东区挪到第 2 行），不能按行号当成同一个数报成数据变化。行段遇到同样
    情况说不清（test_cited_rows_on_an_unreadable_snapshot_are_not_taken_as_the_same），单格一样说不清。"""
    def unreadable(run):
        del run.store[run.queries[1]]          # Agent 那次查询的快照文件不在了
        return run

    north = [["北区", 90.0, 0.0], *NEXT_ROWS]
    first = by_id(run_compare(element("r1", BASE_ROWS, (0, 2)), unreadable(element("r2", north, (0, 3)))))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["这次运行的查询快照无法读取，无法核对 Agent「区域分析」两次引用的第 1 行是不是同一个维度值"]
    first = by_id(run_compare(unreadable(element("r1", BASE_ROWS, (0, 2))), element("r2", north, (0, 3))))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["基准运行的查询快照无法读取，无法核对 Agent「区域分析」两次引用的第 1 行是不是同一个维度值"]


def test_cited_rows_on_a_snapshot_with_misshapen_rows_are_not_taken_as_the_same():
    """有一边的快照里有一行的格数和列对不上（引用的行外面那一行少了一格）：认不出各行是哪个区域，引用的是不是同一批
    区域核对不了，和快照读不出来一样说不清，不退回按行号认（这次北区排进了前 3，西区挪到了第 4 行，按行号认会把西区
    算成「数据没了」）。两个方向都是。"""
    with_middle = [["东区", 1200.5, 100.0], ["南区", 800.0, 50.0], ["西区", 600.0, 0.0], ["中区", 30.0, 0.0]]
    broken = [*WITH_NORTH[:3], ["西区", 600.0]]
    total = by_id(run_compare(cited("r1", with_middle, span=(0, 2)), cited("r2", broken, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["这次运行的查询快照里有的行和列对不上，无法核对 Agent「区域分析」两次引用的第 1 至 3 行"
                                "是不是同一批维度值"]
    total = by_id(run_compare(cited("r1", broken, span=(0, 2)), cited("r2", with_middle, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["基准运行的查询快照里有的行和列对不上，无法核对 Agent「区域分析」两次引用的第 1 至 3 行"
                                "是不是同一批维度值"]


#: 区域 × 日期的结果：日期按设计不当维度（每期都不同，见 _dimensions），一个区域占几行
BY_DAY_SQL = "SELECT region, day, SUM(amount) AS gmv FROM orders GROUP BY region, day ORDER BY region, day"
BY_DAY_COLUMNS = ["region", "day", "gmv"]
BY_DAY_TYPES = {"region": "text", "day": "date", "gmv": "number"}
#: 基准运行：东、南、西各两天，Agent 引用第 1 至 4 行（东区、南区各两行）
BY_DAY_BASE = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 100.0], ["南区", "2026-09-01", 50.0],
               ["南区", "2026-09-02", 50.0], ["西区", "2026-09-01", 30.0], ["西区", "2026-09-02", 30.0]]


def by_day(run_id, rows, *, span=(0, 3), sql=BY_DAY_SQL, columns=BY_DAY_COLUMNS, types=BY_DAY_TYPES, masked=()):
    """Agent 按出处交回一段行的金额（默认第 1 至 4 行），口径卡求和。masked 是这次运行每份快照都遮的列。"""
    run = Fake(run_id)
    run.tool("fetch", sql, columns, rows, types=types)
    bot = run.agent("bot", sql, columns, rows, types=types, span=span)
    first, last = span
    at = columns.index("gmv")
    run.card([metric("total", "sum(vars.kpi.amounts)", sum(r[at] for r in rows[first:last + 1]),
                     [amounts_in(bot, rows, span=span)])])
    if masked:
        run.masked = {a: frozenset(masked) for a in run.queries}
    return run


def test_a_region_partly_pushed_out_of_the_cited_rows_is_not_the_same_citation():
    """一个区域占几行（区域 × 日期）：两次都引用第 1 至 4 行，东区这次多了一天，把南区的第二天挤到了第 5 行——它还在
    快照里，只是没被引用。只看「引用的行里有没有南区」两边都有，查不出来；按区域数两边各引用了几行，南区这次少引用了
    一行、快照里引用的行外面还有南区的行，取的就不是同一组数，不能把南区说成少了一半（占比还会是 -100%）。反过来
    （基准运行里南区有一行在引用的行外面）也一样。南区这次真的只有一天的，照常是数据变化。"""
    more = [["东区", "2026-09-07", 100.0], ["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0],
            ["南区", "2026-09-08", 50.0], ["南区", "2026-09-09", 50.0], ["西区", "2026-09-08", 30.0],
            ["西区", "2026-09-09", 30.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", more)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]
    total = by_id(run_compare(by_day("r1", more), by_day("r2", BY_DAY_BASE)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」在基准运行中有 1 行位于查询结果第 5 行，"
                                "不在引用的行里，取的不是同一组数"]
    # 北区这次排了进来，同样把南区的第二天挤到第 5 行
    north = [["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0], ["北区", "2026-09-08", 10.0],
             ["南区", "2026-09-08", 50.0], ["南区", "2026-09-09", 50.0], ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", north)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]
    # 北区两天都排了进来，南区整个挤出了引用的行：几行都写出来
    both = [["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0], ["北区", "2026-09-08", 10.0],
            ["北区", "2026-09-09", 10.0], ["南区", "2026-09-08", 50.0], ["南区", "2026-09-09", 50.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", both)))["total"]
    assert total["classification"] == "incomparable"
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次仍在查询结果第 5、6 行，不在引用的"
                                "行里，取的不是同一组数"]
    # 东区多了一天、南区也多了一天：南区只引用了一天，两天挤在外面，挤出去的几行都写出来（写的是在外面的行数，不是
    # 少引用的行数）；反过来（基准运行里南区有两行在外面）也是
    crowded = [["东区", "2026-09-07", 100.0], ["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0],
               ["南区", "2026-09-07", 50.0], ["南区", "2026-09-08", 50.0], ["南区", "2026-09-09", 50.0],
               ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", crowded)))["total"]
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 2 行在查询结果第 5、6 行，不在"
                                "引用的行里，取的不是同一组数"]
    total = by_id(run_compare(by_day("r1", crowded), by_day("r2", BY_DAY_BASE)))["total"]
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」在基准运行中有 2 行位于查询结果第 5、6 "
                                "行，不在引用的行里，取的不是同一组数"]
    # 挤出去的行多时只列前 3 个行号，后面写「等」（几十行全列出来说明就读不下去了）；正好 3 行的全列出来，不写「等」
    assert compare._row_numbers([4, 5, 6, 7]) == "第 5、6、7 行等"
    assert compare._row_numbers([4, 5, 6]) == "第 5、6、7 行"
    # 对照：南区这次真的只有一天（快照里引用的行外面没有南区）：照常是数据变化，按区域归因，占比之和为 1
    gone = [["东区", "2026-09-07", 100.0], ["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0],
            ["南区", "2026-09-08", 50.0], ["西区", "2026-09-08", 30.0], ["西区", "2026-09-09", 30.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", gone)))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["东区"], 200.0, 300.0, 2.0), (["南区"], 100.0, 50.0, -1.0)]
    assert sum(exact(r["share"]) for r in rows) == 1


def test_a_masked_column_beside_the_region_is_spoken_of_by_row_number():
    """经理列设了遮罩、区域列没有：只按区域认，一个区域有几位经理就占几行。南区这次有一行挤出了引用的行，说明里只写
    区域和行号，不写遮住的经理名；经理列也不拿来认是哪一行（拿来认、写出「南区 / 经理丁」就把遮住的那一格说出去了）。"""
    sql = "SELECT region, manager, SUM(amount) AS gmv FROM orders GROUP BY region, manager ORDER BY region, manager"
    columns = ["region", "manager", "gmv"]
    types = {"region": "text", "manager": "text", "gmv": "number"}
    base = [["东区", "经理甲", 100.0], ["东区", "经理乙", 100.0], ["南区", "经理丙", 50.0], ["南区", "经理丁", 50.0],
            ["西区", "经理戊", 30.0]]
    this = [["东区", "经理甲", 110.0], ["东区", "经理乙", 100.0], ["东区", "经理庚", 20.0], ["南区", "经理丙", 50.0],
            ["南区", "经理丁", 50.0], ["西区", "经理戊", 30.0]]
    shape = {"sql": sql, "columns": columns, "types": types, "masked": {"manager"}}
    result = run_compare(by_day("r1", base, **shape), by_day("r2", this, **shape))
    total = by_id(result)["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]
    text = json.dumps(result, ensure_ascii=False)
    assert not [name for name in ("经理甲", "经理乙", "经理丙", "经理丁", "经理戊", "经理庚") if name in text]


def test_a_region_cited_whole_on_one_side_and_partly_on_the_other_is_not_the_same_citation():
    """一个区域在一边整个被引用、在另一边只引用了一部分（其余的行还在快照里，在引用的行外面）：引用的行数没变少、甚至
    变多了，也不是同一组数。南区这次三天，引用的第 1 至 4 行只盖住了前两天，第三天（200）在第 5 行：按引用的行算南区是
    100 → 20，快照里南区却是 220——归因会说南区少了 80，正负号都反了，还和「南区这次真的只有两天」的输出一字不差。东区
    这次真的少了一天、南区引用了三天（比基准还多一天）、第四天在外面的也是；反方向（基准运行里南区有一天在引用的行
    外面、这次整个被引用）也一样。对照：南区真的只有引用的那几天，照常是数据变化，按区域归因，占比之和为 1。"""
    def attributed(base_rows, target_rows):
        total = by_id(run_compare(by_day("r1", base_rows), by_day("r2", target_rows)))["total"]
        assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
        rows = total["attribution"]["rows"]
        assert sum(exact(r["share"]) for r in rows) == 1
        return [(r["key"], r["base"], r["target"], r["share"]) for r in rows]

    three = [["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0], ["南区", "2026-09-08", 10.0],
             ["南区", "2026-09-09", 10.0], ["南区", "2026-09-10", 200.0], ["西区", "2026-09-08", 30.0],
             ["西区", "2026-09-09", 30.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", three)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]
    # 对照：南区这次真的只有两天（10、10）
    assert attributed(BY_DAY_BASE, three[:4] + three[5:]) == [
        (["南区"], 100.0, 20.0, 1.0), (["东区"], 200.0, 200.0, 0.0)]

    # 东区这次真的少了一天；南区四天，引用了三天（比基准还多一天），第四天在第 5 行
    four = [["东区", "2026-09-08", 100.0], ["南区", "2026-09-07", 50.0], ["南区", "2026-09-08", 50.0],
            ["南区", "2026-09-09", 50.0], ["南区", "2026-09-10", 50.0], ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", four)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]
    # 对照：南区这次真的只有三天
    assert attributed(BY_DAY_BASE, four[:4] + four[5:]) == [
        (["东区"], 200.0, 100.0, 2.0), (["南区"], 100.0, 150.0, -1.0)]

    # 反方向：基准运行南区三天，第三天（500）在第 5 行；这次南区两天都被引用。按引用的行算是 100 → 120，基准的南区
    # 实际是 600
    base3 = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 100.0], ["南区", "2026-09-01", 50.0],
             ["南区", "2026-09-02", 50.0], ["南区", "2026-09-03", 500.0], ["西区", "2026-09-01", 30.0]]
    now = [["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0], ["南区", "2026-09-08", 60.0],
           ["南区", "2026-09-09", 60.0], ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", base3), by_day("r2", now)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」在基准运行中有 1 行位于查询结果第 5 行，"
                                "不在引用的行里，取的不是同一组数"]
    # 对照：基准运行南区真的只有两天
    assert attributed(base3[:4] + base3[5:], now) == [
        (["南区"], 100.0, 120.0, 1.0), (["东区"], 200.0, 200.0, 0.0)]


def test_a_region_cut_at_the_same_place_on_both_sides_is_the_same_citation():
    """两边都只引用了南区的一部分、引用的行数也一样：行段在同一处切开了南区（两次都是第 3 行被引用、第 4 行在外面），
    取的是同一组数（南区的第一天），照常是数据变化，按区域归因，占比之和为 1——不能因为南区有行在引用的行外面就一概
    说不清，每周结构一样的对比就都比不了了。引用的行数不一样的（一边引用了南区两天、另一边一天）切在了不同处，不是
    同一组数，说引用得少、有行在外面的那一边。两个区域都切法不同时，说基准运行引用的行里先出现的那个。"""
    before = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 100.0], ["南区", "2026-09-01", 50.0],
              ["南区", "2026-09-02", 50.0], ["西区", "2026-09-01", 30.0]]
    after = [["东区", "2026-09-08", 120.0], ["东区", "2026-09-09", 110.0], ["南区", "2026-09-08", 40.0],
             ["南区", "2026-09-09", 50.0], ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", before, span=(0, 2)), by_day("r2", after, span=(0, 2))))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["东区"], 200.0, 230.0, 1.5), (["南区"], 50.0, 40.0, -0.5)]
    assert sum(exact(r["share"]) for r in rows) == 1
    # 切在哪儿只看排在引用的行前面的行数、引用的行数，不看后面还剩几行：南区这次多了一天、排在引用的行后面（后面
    # 剩两天，基准运行剩一天），引用的仍是南区的第一天，照常是数据变化
    longer = after[:4] + [["南区", "2026-09-10", 60.0]] + after[4:]
    total = by_id(run_compare(by_day("r1", before, span=(0, 2)), by_day("r2", longer, span=(0, 2))))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["东区"], 200.0, 230.0, 1.5), (["南区"], 50.0, 40.0, -0.5)]

    # 两边都只引用了南区的一部分、后面都剩一天，引用的行数却不同（基准运行两天，这次东区少了一天、南区引用了三天）：
    # 切在了不同处。两边都有一行在外面，说引用得少的基准运行
    base3 = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 100.0], ["南区", "2026-09-01", 50.0],
             ["南区", "2026-09-02", 50.0], ["南区", "2026-09-03", 500.0], ["西区", "2026-09-01", 30.0]]
    now3 = [["东区", "2026-09-08", 100.0], ["南区", "2026-09-07", 50.0], ["南区", "2026-09-08", 50.0],
            ["南区", "2026-09-09", 50.0], ["南区", "2026-09-10", 500.0], ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", base3), by_day("r2", now3)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」在基准运行中有 1 行位于查询结果第 5 行，"
                                "不在引用的行里，取的不是同一组数"]
    # 南区在基准运行里一行都没被引用（两天都在外面），这次引用了两天、还有一天在外面：说引用得少的基准运行，写它在
    # 外面的那两行
    east4 = [["东区", f"2026-09-0{d}", 100.0] for d in range(1, 5)] + [["南区", "2026-09-01", 50.0],
                                                                         ["南区", "2026-09-02", 50.0]]
    south3 = [["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0], ["南区", "2026-09-08", 50.0],
              ["南区", "2026-09-09", 50.0], ["南区", "2026-09-10", 50.0]]
    total = by_id(run_compare(by_day("r1", east4), by_day("r2", south3)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」在基准运行中位于查询结果第 5、6 行，不在"
                                "引用的行里，取的不是同一组数"]

    # 东区这次少了一天，南区引用了两天、第三天在第 4 行；基准运行只引用了南区一天、第二天在第 4 行：基准引用得少
    more = [["东区", "2026-09-08", 100.0], ["南区", "2026-09-07", 50.0], ["南区", "2026-09-08", 50.0],
            ["南区", "2026-09-09", 50.0], ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", before, span=(0, 2)), by_day("r2", more, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 3 行，「南区」在基准运行中有 1 行位于查询结果第 4 行，"
                                "不在引用的行里，取的不是同一组数"]
    # 反过来，这次引用得少
    total = by_id(run_compare(by_day("r1", more, span=(0, 2)), by_day("r2", before, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 3 行，「南区」这次有 1 行在查询结果第 4 行，不在引用"
                                "的行里，取的不是同一组数"]

    # 按金额取前 3 行：基准运行东区两天都在前 3、南区一天；这次南区两天都在前 3、东区一天。东区、南区都切法不同，
    # 说基准运行引用的行里先出现的东区，不是这次先出现的南区
    top = BY_DAY_SQL.replace("ORDER BY region, day", "ORDER BY gmv DESC")
    ranked_before = [["东区", "2026-09-01", 300.0], ["南区", "2026-09-01", 250.0], ["东区", "2026-09-02", 200.0],
                     ["南区", "2026-09-02", 100.0], ["西区", "2026-09-01", 50.0]]
    ranked_after = [["南区", "2026-09-08", 320.0], ["东区", "2026-09-08", 300.0], ["南区", "2026-09-09", 260.0],
                    ["东区", "2026-09-09", 150.0], ["西区", "2026-09-08", 60.0]]
    total = by_id(run_compare(by_day("r1", ranked_before, span=(0, 2), sql=top),
                              by_day("r2", ranked_after, span=(0, 2), sql=top)))["total"]
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 3 行，「东区」这次有 1 行在查询结果第 4 行，不在引用"
                                "的行里，取的不是同一组数"]


def test_a_region_cut_on_other_rows_of_it_is_not_the_same_citation():
    """引用的行不从第 1 行开始（按金额排的第 2 至 4 行）：两边都只引用了东区的一行，行数一样，可基准运行引用的是东区
    金额第二高的那天（最高的那天排在引用的行前面），这次是最高的那天（第二高的排在后面）——引用的不是它同样的那几行，
    东区按引用的行算是 250 → 300，东区两天的合计却是 550 → 400。要按查询结果的先后数清引用的是它的第几行：排在引用的
    行前面的行数一样、引用的行数也一样，才是同样那几行。对照：两次都是东区第二高的那天被引用，照常是数据变化。"""
    top = BY_DAY_SQL.replace("ORDER BY region, day", "ORDER BY gmv DESC")
    before = [["东区", "2026-09-01", 300.0], ["东区", "2026-09-02", 250.0], ["南区", "2026-09-01", 200.0],
              ["南区", "2026-09-02", 150.0], ["西区", "2026-09-01", 50.0]]
    after = [["西区", "2026-09-08", 400.0], ["南区", "2026-09-08", 350.0], ["南区", "2026-09-09", 320.0],
             ["东区", "2026-09-08", 300.0], ["东区", "2026-09-09", 100.0]]
    total = by_id(run_compare(by_day("r1", before, span=(1, 3), sql=top),
                              by_day("r2", after, span=(1, 3), sql=top)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 2 至 4 行，两次都引用了「东区」的 1 行，可不是它同样的"
                                "那几行（它排在引用的行前面的，基准运行有 1 行，这次有 0 行），取的不是同一组数"]
    # 排在前面的行数要数清，不只看有没有：基准运行引用的是东区第三高的那天（前面有 2 行），这次是第二高的那天（前面
    # 有 1 行）
    third = [["东区", "2026-09-01", 300.0], ["东区", "2026-09-02", 280.0], ["东区", "2026-09-03", 250.0],
             ["南区", "2026-09-01", 200.0], ["西区", "2026-09-01", 50.0]]
    second = [["东区", "2026-09-08", 320.0], ["西区", "2026-09-08", 300.0], ["东区", "2026-09-09", 260.0],
              ["南区", "2026-09-08", 210.0], ["东区", "2026-09-10", 100.0]]
    total = by_id(run_compare(by_day("r1", third, span=(2, 3), sql=top),
                              by_day("r2", second, span=(2, 3), sql=top)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 3 至 4 行，两次都引用了「东区」的 1 行，可不是它同样的"
                                "那几行（它排在引用的行前面的，基准运行有 2 行，这次有 1 行），取的不是同一组数"]
    # 说明里的「引用了它几行」是引用的行里的行数，不是它在外面的行数：基准运行东区在外面有两行（前一后一），引用的
    # 仍是一行
    around = [["东区", "2026-09-01", 300.0], ["东区", "2026-09-02", 250.0], ["南区", "2026-09-01", 200.0],
              ["东区", "2026-09-03", 100.0]]
    shifted = [["西区", "2026-09-08", 400.0], ["东区", "2026-09-08", 300.0], ["南区", "2026-09-08", 250.0],
               ["东区", "2026-09-09", 200.0]]
    total = by_id(run_compare(by_day("r1", around, span=(1, 2), sql=top),
                              by_day("r2", shifted, span=(1, 2), sql=top)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 2 至 3 行，两次都引用了「东区」的 1 行，可不是它同样的"
                                "那几行（它排在引用的行前面的，基准运行有 1 行，这次有 0 行），取的不是同一组数"]
    # 对照：两次都是东区第二高的那天被引用（最高的那天都排在前面）
    same = [["东区", "2026-09-08", 320.0], ["东区", "2026-09-09", 260.0], ["南区", "2026-09-08", 210.0],
            ["南区", "2026-09-09", 140.0], ["西区", "2026-09-08", 60.0]]
    total = by_id(run_compare(by_day("r1", before, span=(1, 3), sql=top),
                              by_day("r2", same, span=(1, 3), sql=top)))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["东区"], 250.0, 260.0, 1.0), (["南区"], 350.0, 350.0, 0.0)]
    assert sum(exact(r["share"]) for r in rows) == 1


def test_a_region_on_one_side_only_counts_as_cited_whole_where_it_is_missing():
    """只在一边的快照里有的区域（这次新出现，或者这次没有了）：没有它的那一边算整个被引用——没什么可漏的。另一边它只
    被引用了一部分、还有行在引用的行外面的，切法不同，也不是同一组数：北区这次新出现、三天里只引用了两天，按引用的行
    算北区是 20，快照里是 30，和「北区这次真的只有两天」的输出一字不差；基准运行里南区只引用了一天、另一天在外面，这次
    南区没有了，归因会说南区少了 50，基准的南区实际是 100。对照：新出现、没有了的区域整个被引用的，照常是数据变化，
    按区域归因，占比之和为 1。"""
    # 南区这次没有了；北区新出现三天，引用的第 1 至 4 行盖住了前两天，第三天在第 5 行
    north = [["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0], ["北区", "2026-09-07", 10.0],
             ["北区", "2026-09-08", 10.0], ["北区", "2026-09-09", 10.0], ["西区", "2026-09-08", 30.0],
             ["西区", "2026-09-09", 30.0]]
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", north)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「北区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]
    # 对照：北区这次真的只有两天
    total = by_id(run_compare(by_day("r1", BY_DAY_BASE), by_day("r2", north[:4] + north[5:])))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["南区"], 100.0, None, 1.25), (["北区"], None, 20.0, -0.25), (["东区"], 200.0, 200.0, 0.0)]
    assert sum(exact(r["share"]) for r in rows) == 1

    # 基准运行引用第 1 至 3 行，南区只引用了一天、第二天在第 4 行；这次南区没有了，新出现的北区排在东区后面
    before = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 100.0], ["南区", "2026-09-01", 50.0],
              ["南区", "2026-09-02", 50.0], ["西区", "2026-09-01", 30.0]]
    after = [["东区", "2026-09-08", 120.0], ["东区", "2026-09-09", 110.0], ["北区", "2026-09-08", 30.0],
             ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(by_day("r1", before, span=(0, 2)), by_day("r2", after, span=(0, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 3 行，「南区」在基准运行中有 1 行位于查询结果第 4 行，"
                                "不在引用的行里，取的不是同一组数"]
    # 对照：基准运行南区真的只有一天（整个被引用）
    total = by_id(run_compare(by_day("r1", before[:3] + before[4:], span=(0, 2)),
                              by_day("r2", after, span=(0, 2))))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["南区"], 50.0, None, -5.0), (["东区"], 200.0, 230.0, 3.0), (["北区"], None, 30.0, 3.0)]
    assert sum(exact(r["share"]) for r in rows) == 1


#: 区域 × 渠道的结果按金额从大到小排：两列都是维度
BY_CHANNEL = {"sql": "SELECT region, channel, SUM(amount) AS gmv FROM orders GROUP BY region, channel "
                     "ORDER BY gmv DESC",
              "columns": ["region", "channel", "gmv"], "types": {"region": "text", "channel": "text", "gmv": "number"}}


def test_dimension_values_are_told_apart_by_every_dimension_column():
    """维度有两列（区域、渠道）：按两列一起认是哪个维度值。按金额取前 3 行，南区 / 线上这次落到第 4 行（还在快照里），
    南区 / 线下排了进来。只按区域认，南区两次都是一行被引用、一行在外面，看着切法一样，会报成数据变化（南区 / 线下
    占 700%、南区 / 线上占 -600%）；说明里两列的值都写出来。对照：南区 / 线上这次真的没有了，新出现的西区 / 线上排进了
    前 3，照常是数据变化，按两列归因，占比之和为 1。

    区域列在一边只有一个值也拿来认（基准运行的结果只有东区）：两边各自都只有一个值的才分不出维度。这次北区排到第一，
    东区有两天挤出了引用的前 2 行，不拿区域认就成了按行号认、报成数据变化。"""
    before = [["东区", "线上", 500.0], ["东区", "线下", 400.0], ["南区", "线上", 300.0], ["南区", "线下", 100.0]]
    after = [["东区", "线上", 500.0], ["东区", "线下", 400.0], ["南区", "线下", 350.0], ["南区", "线上", 200.0]]
    total = by_id(run_compare(by_day("r1", before, span=(0, 2), **BY_CHANNEL),
                              by_day("r2", after, span=(0, 2), **BY_CHANNEL)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 3 行，「南区 / 线上」这次仍在查询结果第 4 行，不在引用"
                                "的行里，取的不是同一组数"]
    gone = [["东区", "线上", 500.0], ["东区", "线下", 400.0], ["西区", "线上", 350.0], ["南区", "线下", 100.0]]
    total = by_id(run_compare(by_day("r1", before, span=(0, 2), **BY_CHANNEL),
                              by_day("r2", gone, span=(0, 2), **BY_CHANNEL)))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    attribution = total["attribution"]
    assert attribution["columns"] == ["region", "channel"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in attribution["rows"]] == [
        (["西区", "线上"], None, 350.0, 7.0), (["南区", "线上"], 300.0, None, -6.0),
        (["东区", "线上"], 500.0, 500.0, 0.0), (["东区", "线下"], 400.0, 400.0, 0.0)]
    assert sum(exact(r["share"]) for r in attribution["rows"]) == 1

    top = BY_DAY_SQL.replace("ORDER BY region, day", "ORDER BY gmv DESC")
    only_east = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 90.0], ["东区", "2026-09-03", 80.0]]
    north_first = [["北区", "2026-09-08", 200.0], ["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 95.0],
                   ["东区", "2026-09-10", 85.0]]
    total = by_id(run_compare(by_day("r1", only_east, span=(0, 1), sql=top),
                              by_day("r2", north_first, span=(0, 1), sql=top)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 2 行，「东区」这次有 2 行在查询结果第 3、4 行，不在"
                                "引用的行里，取的不是同一组数"]
    # 引用的行两边都只有东区、只有这次的整份结果里多了北区：区域要按两边整份结果认（只看基准运行的行，或者只看引用的
    # 行，都认不出区域，退回按行号认）。基准运行东区三天只引用了前两天，这次东区两天都被引用，切法不同
    east3 = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 90.0], ["东区", "2026-09-03", 80.0]]
    east2 = [["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 95.0], ["北区", "2026-09-08", 85.0]]
    total = by_id(run_compare(by_day("r1", east3, span=(0, 1)), by_day("r2", east2, span=(0, 1))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 2 行，「东区」在基准运行中有 1 行位于查询结果第 3 行，"
                                "不在引用的行里，取的不是同一组数"]


def test_the_cited_column_is_never_a_dimension_even_when_stored_as_text():
    """金额列在库里是文本类型（存的是数字文本，查询层记的类型是 text）：Agent 引用的这一列是度量，不拿来认维度。拿来认，
    每个（区域, 金额）都成了只在一边有的维度值，算「真的没有了、这次才有」，南区的第二天挤出引用的行（东区多了一天）
    就查不出来，报成只数据变。"""
    types = {**BY_DAY_TYPES, "gmv": "text"}

    def make(run_id, rows):
        shown = [[region, day, str(gmv)] for region, day, gmv in rows]
        run = Fake(run_id)
        run.tool("fetch", BY_DAY_SQL, BY_DAY_COLUMNS, shown, types=types)
        bot = run.agent("bot", BY_DAY_SQL, BY_DAY_COLUMNS, shown, types=types, span=(0, 3))
        run.card([metric("total", "sum(vars.kpi.amounts)", sum(r[2] for r in rows[:4]),
                         [amounts_in(bot, shown, span=(0, 3))])])
        return run

    before = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 100.0], ["南区", "2026-09-01", 50.0],
              ["南区", "2026-09-02", 40.0], ["西区", "2026-09-01", 30.0]]
    after = [["东区", "2026-09-07", 100.0], ["东区", "2026-09-08", 100.0], ["东区", "2026-09-09", 100.0],
             ["南区", "2026-09-08", 60.0], ["南区", "2026-09-09", 45.0], ["西区", "2026-09-08", 30.0]]
    total = by_id(run_compare(make("r1", before), make("r2", after)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]


#: 区域 × 渠道 × 日期（日期不当维度），按区域、日期排序
BY_CHANNEL_DAY = {"sql": "SELECT region, channel, day, SUM(amount) AS gmv FROM orders GROUP BY region, channel, day "
                         "ORDER BY region, day",
                  "columns": ["region", "channel", "day", "gmv"],
                  "types": {"region": "text", "channel": "text", "day": "date", "gmv": "number"}}


def test_cited_rows_are_checked_by_the_dimensions_the_attribution_uses():
    """归因只在引用的行上认维度（_attribute）：渠道在引用的行里两边都只有「线上」，不当维度，归因按区域说。核对引用的
    是不是同一批区域也要按区域核对一遍——只按整份结果认维度（区域, 渠道），南区 / 线上两边都整个被引用、南区 / 线下
    两边都没被引用，看着都切在同一处，按区域看南区这次却有一行在引用的行外面，归因说的南区就不是同一组数。

    区域 × 渠道 × 日期，两次都引用第 1 至 4 行：东区这次多了一天，南区的第二天这次走的是线下，排到第 5 行（还在快照
    里）。按引用的行归因，南区是 100 → 50（占比 -100%），和「南区这次真的只有一天」的输出一字不差；反方向也是。区域 ×
    渠道按金额取前 2 行：南区这次新开了线下（200），排第 3——快照里南区是 300 → 480，按引用的行归因却是 300 → 280，
    正负号都反了。对照：南区真的只有引用的那几行的，照常是数据变化，按区域归因，占比之和为 1。"""
    base = [["东区", "线上", "2026-09-01", 100.0], ["东区", "线上", "2026-09-02", 100.0],
            ["南区", "线上", "2026-09-01", 50.0], ["南区", "线上", "2026-09-02", 50.0],
            ["西区", "线上", "2026-09-01", 30.0], ["西区", "线上", "2026-09-02", 30.0]]
    this = [["东区", "线上", "2026-09-07", 100.0], ["东区", "线上", "2026-09-08", 100.0],
            ["东区", "线上", "2026-09-09", 100.0], ["南区", "线上", "2026-09-08", 50.0],
            ["南区", "线下", "2026-09-09", 50.0], ["西区", "线上", "2026-09-08", 30.0],
            ["西区", "线上", "2026-09-09", 30.0]]
    total = by_id(run_compare(by_day("r1", base, **BY_CHANNEL_DAY), by_day("r2", this, **BY_CHANNEL_DAY)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」这次有 1 行在查询结果第 5 行，不在引用"
                                "的行里，取的不是同一组数"]
    total = by_id(run_compare(by_day("r1", this, **BY_CHANNEL_DAY), by_day("r2", base, **BY_CHANNEL_DAY)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 4 行，「南区」在基准运行中有 1 行位于查询结果第 5 行，"
                                "不在引用的行里，取的不是同一组数"]
    # 对照：南区这次真的只有一天（没有线下那一行）
    gone = [r for r in this if r[1] != "线下"]
    total = by_id(run_compare(by_day("r1", base, **BY_CHANNEL_DAY), by_day("r2", gone, **BY_CHANNEL_DAY)))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    attribution = total["attribution"]
    assert attribution["columns"] == ["region"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in attribution["rows"]] == [
        (["东区"], 200.0, 300.0, 2.0), (["南区"], 100.0, 50.0, -1.0)]
    assert sum(exact(r["share"]) for r in attribution["rows"]) == 1

    # 区域 × 渠道按金额取前 2 行：南区这次新开了线下，排第 3
    before = [["东区", "线上", 500.0], ["南区", "线上", 300.0], ["西区", "线上", 100.0]]
    after = [["东区", "线上", 600.0], ["南区", "线上", 280.0], ["南区", "线下", 200.0], ["西区", "线上", 120.0]]
    total = by_id(run_compare(by_day("r1", before, span=(0, 1), **BY_CHANNEL),
                              by_day("r2", after, span=(0, 1), **BY_CHANNEL)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 1 至 2 行，「南区」这次有 1 行在查询结果第 3 行，不在引用"
                                "的行里，取的不是同一组数"]
    # 对照：南区真的只有线上（这次多出来的北区排在最后，两边都没引用）
    only_online = [["东区", "线上", 600.0], ["南区", "线上", 280.0], ["西区", "线上", 120.0], ["北区", "线上", 10.0]]
    total = by_id(run_compare(by_day("r1", before, span=(0, 1), **BY_CHANNEL),
                              by_day("r2", only_online, span=(0, 1), **BY_CHANNEL)))["total"]
    assert (total["classification"], total["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    rows = total["attribution"]["rows"]
    assert [(r["key"], r["base"], r["target"], r["share"]) for r in rows] == [
        (["东区"], 500.0, 600.0, 1.25), (["南区"], 300.0, 280.0, -0.25)]
    assert sum(exact(r["share"]) for r in rows) == 1

    # 只按归因用的那组也不够：只引用了一行的，引用的行里每列都只有一个值，认不出维度（也不归因）。按金额排的第 2 行，
    # 基准运行是东区第二高的那天，这次是东区最高的那天——按整份结果认出的区域照样核对，说不清
    top = BY_DAY_SQL.replace("ORDER BY region, day", "ORDER BY gmv DESC")
    ranked_before = [["东区", "2026-09-01", 300.0], ["东区", "2026-09-02", 250.0], ["南区", "2026-09-01", 200.0]]
    ranked_after = [["南区", "2026-09-08", 350.0], ["东区", "2026-09-08", 300.0], ["东区", "2026-09-09", 100.0]]
    total = by_id(run_compare(by_day("r1", ranked_before, span=(1, 1), sql=top),
                              by_day("r2", ranked_after, span=(1, 1), sql=top)))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None
    assert total["reasons"] == ["Agent「区域分析」两次都引用第 2 至 2 行，两次都引用了「东区」的 1 行，可不是它同样的"
                                "那几行（它排在引用的行前面的，基准运行有 1 行，这次有 0 行），取的不是同一组数"]


def test_an_element_does_not_mind_other_rows_of_its_region():
    """口径卡取 Agent 数组字段的第一个元素（单格）：不套行段的切法，只看引用的那一行两次是不是同一个区域的同一行（排在
    它前面的、这个区域的行数一样，见 test_an_element_on_another_row_of_the_same_region_is_not_the_same_number），不看这个
    区域在这一行外面还有没有行。区域 × 日期的结果里，基准运行东区两天（第二天在引用的行外面）、这次只有一天：第 1 行
    两次都是东区的第一天，是同一个数，照常是数据变化。行段要看每个区域被切成什么样（整个、还是只有一部分），因为口径卡
    按引用的几行一起算；单格只有一行，东区还有没有别的行不改变它取的是哪个数。第 1 行的东区这次真的没有了（换成了
    北区，东区不在这次的快照里）也照常比；东区还在快照里、只是不在第 1 行的说不清
    （test_an_element_of_a_cited_array_follows_its_row）。"""
    day = {"sql": BY_DAY_SQL, "columns": BY_DAY_COLUMNS, "types": BY_DAY_TYPES}
    before = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 90.0], ["南区", "2026-09-01", 50.0],
              ["西区", "2026-09-01", 30.0]]
    after = [["东区", "2026-09-08", 110.0], ["南区", "2026-09-08", 60.0], ["西区", "2026-09-08", 30.0]]
    first = by_id(run_compare(element("r1", before, (0, 2), **day), element("r2", after, (0, 2), **day)))["first"]
    assert (first["classification"], first["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    first = by_id(run_compare(element("r1", after, (0, 2), **day), element("r2", before, (0, 2), **day)))["first"]
    assert (first["classification"], first["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])
    gone = [["北区", "2026-09-08", 80.0], ["南区", "2026-09-08", 60.0], ["西区", "2026-09-08", 30.0]]
    first = by_id(run_compare(element("r1", before, (0, 2), **day), element("r2", gone, (0, 2), **day)))["first"]
    assert (first["classification"], first["reasons"]) == ("data_only", [compare.DATA_FROM_QUERIES])


def test_an_element_on_another_row_of_the_same_region_is_not_the_same_number():
    """单格引用的那一行两次是同一个区域，还要是它同样的那一行：按查询结果的先后，排在这一行前面的、它的行数两边一样。
    区域 × 日期的结果里，Agent 两次都交第 3 至 5 行，口径卡取第一个元素（第 3 行）：基准运行那是南区的第一天（50）；
    这次东区真的少了一天，第 3 行成了南区的第二天（45），南区的第一天（60）在第 2 行。只看「是不是南区」会报成南区
    少了 5，同一天的南区其实多了 10，正负号都反了。同一处引用换成只有一行的行段（第 3 至 3 行）说不清，单格也说不清。
    按金额排的取第 2 行（基准运行是东区第二高的那天，这次是最高的那天）、取第二个元素的也一样。

    只比排在前面的行数、不比「它在这一行外面还有没有行」：东区两天还是一天，第 1 行都是东区的第一天，照常是数据变化
    （test_an_element_does_not_mind_other_rows_of_its_region）。"""
    day = {"sql": BY_DAY_SQL, "columns": BY_DAY_COLUMNS, "types": BY_DAY_TYPES}
    before = [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 90.0], ["南区", "2026-09-01", 50.0],
              ["南区", "2026-09-02", 40.0], ["西区", "2026-09-01", 30.0]]
    after = [["东区", "2026-09-08", 110.0], ["南区", "2026-09-08", 60.0], ["南区", "2026-09-09", 45.0],
             ["西区", "2026-09-08", 30.0], ["西区", "2026-09-09", 20.0]]
    first = by_id(run_compare(element("r1", before, (2, 4), **day), element("r2", after, (2, 4), **day)))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["Agent「区域分析」两次都引用第 3 行，两次都是「南区」，可不是它同样的那一行（它排在这一行"
                                "前面的，基准运行有 0 行，这次有 1 行），取的不是同一个数"]
    first = by_id(run_compare(element("r1", after, (2, 4), **day), element("r2", before, (2, 4), **day)))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["Agent「区域分析」两次都引用第 3 行，两次都是「南区」，可不是它同样的那一行（它排在这一行"
                                "前面的，基准运行有 1 行，这次有 0 行），取的不是同一个数"]
    # 同样的两份结果，只有一行的行段：同样说不清，单格不比它松
    total = by_id(run_compare(by_day("r1", before, span=(2, 2)), by_day("r2", after, span=(2, 2))))["total"]
    assert total["classification"] == "incomparable" and total["attribution"] is None

    # 按金额排，取 Agent 交的第 2 至 3 行的第一个元素：基准运行是东区第二高的那天（250），这次是东区最高的那天（300）
    top = {**day, "sql": BY_DAY_SQL.replace("ORDER BY region, day", "ORDER BY gmv DESC")}
    ranked_before = [["东区", "2026-09-01", 300.0], ["东区", "2026-09-02", 250.0], ["南区", "2026-09-01", 200.0]]
    ranked_after = [["南区", "2026-09-08", 350.0], ["东区", "2026-09-08", 300.0], ["东区", "2026-09-09", 100.0]]
    first = by_id(run_compare(element("r1", ranked_before, (1, 2), **top),
                              element("r2", ranked_after, (1, 2), **top)))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["Agent「区域分析」两次都引用第 2 行，两次都是「东区」，可不是它同样的那一行（它排在这一行"
                                "前面的，基准运行有 1 行，这次有 0 行），取的不是同一个数"]
    # 排在前面的行数要数清，不只看有没有：第 3 行基准运行是东区第三高的那天（前面有 2 行），这次是第二高的那天（前面
    # 有 1 行）
    third = [["东区", "2026-09-01", 300.0], ["东区", "2026-09-02", 280.0], ["东区", "2026-09-03", 250.0],
             ["南区", "2026-09-01", 200.0]]
    second = [["东区", "2026-09-08", 320.0], ["西区", "2026-09-08", 300.0], ["东区", "2026-09-09", 260.0],
              ["南区", "2026-09-08", 210.0], ["东区", "2026-09-10", 100.0]]
    first = by_id(run_compare(element("r1", third, (2, 3), **top), element("r2", second, (2, 3), **top)))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["Agent「区域分析」两次都引用第 3 行，两次都是「东区」，可不是它同样的那一行（它排在这一行"
                                "前面的，基准运行有 2 行，这次有 1 行），取的不是同一个数"]
    # 取第二个元素（Agent 交第 1 至 3 行，落在第 2 行）：基准运行是东区的第二天，这次北区排到第 1 行，第 2 行是东区的
    # 第一天
    first = by_id(run_compare(
        element("r1", [["东区", "2026-09-01", 100.0], ["东区", "2026-09-02", 90.0], ["南区", "2026-09-01", 50.0]],
                (0, 2), index=1, **day),
        element("r2", [["北区", "2026-09-08", 70.0], ["东区", "2026-09-08", 95.0], ["南区", "2026-09-08", 55.0]],
                (0, 2), index=1, **day)))["first"]
    assert first["classification"] == "incomparable" and first["attribution"] is None
    assert first["reasons"] == ["Agent「区域分析」两次都引用第 2 行，两次都是「东区」，可不是它同样的那一行（它排在这一行"
                                "前面的，基准运行有 1 行，这次有 0 行），取的不是同一个数"]

    # 对照：第 3 行两次都是南区的第一天（排在它前面的南区行两边都是 0），照常是数据变化：50 → 60
    same = [["东区", "2026-09-08", 110.0], ["东区", "2026-09-09", 100.0], ["南区", "2026-09-08", 60.0],
            ["南区", "2026-09-09", 45.0], ["西区", "2026-09-08", 30.0]]
    first = by_id(run_compare(element("r1", before, (2, 4), **day), element("r2", same, (2, 4), **day)))["first"]
    assert (first["classification"], first["reasons"], first["delta"]) == ("data_only", [compare.DATA_FROM_QUERIES],
                                                                           10.0)


def test_no_split_when_an_old_leaf_now_cites_another_column():
    """口径变了（v1 → v2），旧口径用到的 Agent 字段这次引用的是退款列：说不清它只是数据变了，不拆，说明为什么。"""
    def make(run_id, column, expression, factor, version):
        run = Fake(run_id)
        run.tool("fetch", region_sql())
        bot = run.agent("bot", region_sql(), column=column)
        value = sum(r[COLUMNS.index(column)] for r in BASE_ROWS) * factor
        run.card([metric("total", expression, value, [amounts_in(bot, BASE_ROWS, column=column)])], version=version)
        return run

    total = by_id(run_compare(make("r1", "gmv", "sum(vars.kpi.amounts)", 1, "v1"),
                              make("r2", "refund", "sum(vars.kpi.amounts) * 2", 2, "v2")))["total"]
    assert total["classification"] == "caliber_and_data" and total["decomposition"] is None
    assert total["decomposition_note"].startswith("无法确认旧口径用到的输入只是数据有变化")
    assert "refund" in total["decomposition_note"]


def test_an_unknown_writer_on_one_side_is_named_plainly():
    """一边记不出 vars.adj 是哪个节点写的：说清是哪一边没记录，不写「None」。"""
    def make(run_id, writer, value):
        run = Fake(run_id, graph=weekly_graph(between=(node("calc", "code", "调整", code="x", assign_to="adj"),)))
        run.tool("fetch", region_sql())
        run.agent("bot", region_sql())
        run.code("calc", value, "adj")
        item = scalar_in("vars.adj", value, writer, "code" if writer else "state")
        run.card([metric("calc", "vars.adj * 2", value * 2, [item])])
        return run
    calc = by_id(run_compare(make("r1", None, 10), make("r2", "calc", 12)))["calc"]
    assert calc["classification"] == "incomparable"
    assert not any("None" in r for r in calc["reasons"]) and any("基准运行" in r for r in calc["reasons"])


def test_a_list_run_input_is_compared_by_its_content():
    """运行输入是列表：卡上只记了「[2 项]」，按入口节点的产出还原出完整值再比。"""
    def make(run_id, targets):
        run = Fake(run_id, inputs={"targets": targets})
        run.add("node.started", "start", {"node_type": "input"})
        run.add("node.finished", "start", {"artifact": run.put({"targets": targets})})
        item = {"path": "input.targets", "value": f"[{len(targets)} 项]", "node_id": "start", "via": "input",
                "field": "targets", "status": "ok"}
        run.card([metric("goal", "sum(input.targets)", float(sum(targets)), [item])])
        return run
    goal = by_id(run_compare(make("r1", [100.0, 200.0]), make("r2", [100.0, 500.0])))["goal"]
    assert goal["classification"] == "params_only"
    assert goal["reasons"] == ["运行输入「input.targets」的内容有变化"]
    goal = by_id(run_compare(make("r1", [100.0, 200.0]), make("r2", [100.0, 200.0])))["goal"]
    assert goal["classification"] == "unchanged"


def test_a_list_run_input_without_its_full_value_is_not_taken_as_unchanged():
    """运行输入是列表，入口节点当时的产出不在（还原不出完整值），卡上只记了「[2 项]」：大概一样、指标的值却变了，
    核对不了它变没变，不报成数据变化；项数变了的照常是参数变化；指标的值没变的照常没变。"""
    def make(run_id, targets):
        run = Fake(run_id, inputs={"targets": targets})
        item = {"path": "input.targets", "value": f"[{len(targets)} 项]", "node_id": "start", "via": "input",
                "field": "targets", "status": "ok"}
        run.card([metric("goal", "sum(input.targets)", float(sum(targets)), [item])])
        return run

    goal = by_id(run_compare(make("r1", [100.0, 200.0]), make("r2", [100.0, 500.0])))["goal"]
    assert goal["classification"] == "incomparable"
    assert goal["reasons"] == ["运行输入「input.targets」是列表或对象，当时没有保存完整值，无法核对是否有变化"]
    goal = by_id(run_compare(make("r1", [100.0, 200.0]), make("r2", [100.0, 200.0, 50.0])))["goal"]
    assert goal["classification"] == "params_only"
    assert goal["reasons"] == ["运行输入「input.targets」的内容有变化"]
    goal = by_id(run_compare(make("r1", [100.0, 200.0]), make("r2", [100.0, 200.0])))["goal"]
    assert (goal["classification"], goal["reasons"]) == ("unchanged", [])


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


def test_swapped_queries_with_the_same_literals_pair_up_exactly():
    """Agent 两次都查了东区和南区，只是先后互换：先配字面量也相同的，两条都没变；下游的数照常是数据变化。"""
    def region(name):
        return f"SELECT SUM(amount) AS gmv FROM orders WHERE region = '{name}'"

    def make(run_id, regions, value, gmv):
        run = Fake(run_id, graph=weekly_graph(between=(node("calc", "code", "调整", code="x", assign_to="adj"),)))
        run.plain_agent("bot", [region(r) for r in regions], {"text": "ok"}, ["gmv"], [[gmv]], types={"gmv": "number"})
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, None, None))
        return run

    result = run_compare(make("r1", ["东区", "南区"], 10, 1.0), make("r2", ["南区", "东区"], 12, 2.0))
    assert [(q["structure"], q["literals"]) for q in result["queries"]] == [("same", []), ("same", [])]
    # Agent 两次交出的一模一样（只是查询的先后换了）：它没带进模型本身的差异
    assert by_id(result)["calc"]["classification"] == "data_only"


def test_renamed_nodes_pair_their_queries_by_literals_first():
    """节点换了 ID，同一个模板查了东区和南区、这次先后互换：跨节点按 SQL 配时同样先配字面量也相同的，两条都没变，
    不会配成两条「参数不同」。"""
    def make(run_id, node_id, regions):
        run = Fake(run_id)
        for region in regions:
            run.tool(node_id, f"SELECT SUM(amount) AS gmv FROM orders WHERE region = '{region}'", ["gmv"], [[1.0]],
                     types={"gmv": "number"})
        return run

    result = run_compare(make("r1", "fetch", ["东区", "南区"]), make("r2", "pull", ["南区", "东区"]))
    assert [(q["node_id"], q["structure"], q["literals"]) for q in result["queries"]] == [
        ("pull", "same", []), ("pull", "same", [])]


def test_a_switched_data_source_is_a_structure_change():
    """SQL 一字不差、换了数据源：查的不是同一份数据。"""
    result = run_compare(week("r1"), week("r2", NEXT_ROWS, source="shop_backup"))
    fetch = result_queries(result, "fetch")
    assert fetch["structure"] == "changed" and any("shop_backup" in d for d in fetch["differences"])
    assert by_id(result)["east"]["classification"] == "sql_changed"


def test_a_literal_edited_in_the_workflow_is_a_template_change():
    """图里写死的筛选值改了（'paid' 改成 'refunded'）：执行的 SQL 只差字面量，但这是改模板，不是运行时代入的参数。"""
    paid, refunded = region_sql(extra=" AND status = 'paid'"), region_sql(extra=" AND status = 'refunded'")
    result = run_compare(week("r1", sql=paid, graph=weekly_graph(paid)),
                         week("r2", NEXT_ROWS, sql=refunded, graph=weekly_graph(refunded)))
    east = by_id(result)["east"]
    assert east["classification"] == "sql_changed", east["reasons"]
    assert any("区域取数" in r and "'paid'" in r for r in east["reasons"])
    assert east["decomposition"] is None and east["attribution"] is None
    # 查询表照实写：执行的 SQL 确实只差这一处字面量
    assert result_queries(result, "fetch")["structure"] == "params_only"


def test_reformatting_the_workflow_sql_is_not_a_change():
    loose = region_sql().replace(" FROM ", "\n  FROM ").lower()
    result = run_compare(week("r1"), week("r2", NEXT_ROWS, sql=loose, graph=weekly_graph(loose)))
    assert {m["classification"] for m in result["metrics"]} == {"data_only"}

    # 代码节点在它下游：取数节点不算改了配置，变化照常来自上游数据
    def make(run_id, sql, rows, value):
        run = Fake(run_id, graph=weekly_graph(sql, after_fetch=(calc_node(),)))
        run.tool("fetch", sql, rows=rows)
        run.agent("bot", sql, rows=rows)
        run.code("calc", value, "adj")
        run.card(code_metrics(value)(None, None, rows, rows))
        return run
    calc = by_id(run_compare(make("r1", region_sql(), BASE_ROWS, 10), make("r2", loose, NEXT_ROWS, 12)))["calc"]
    assert (calc["classification"], calc["reasons"]) == ("data_only", [compare.DATA_FROM_UPSTREAM_SCOPED])


def test_renaming_a_node_is_not_a_change():
    graph = weekly_graph()
    graph["nodes"][2]["data"]["label"] = "区域分析（新）"
    result = run_compare(week("r1"), week("r2", NEXT_ROWS, graph=graph))
    assert {m["classification"] for m in result["metrics"]} == {"data_only"}
    [renamed] = result["template"]["nodes"]
    assert (renamed["node_id"], renamed["keys"]) == ("bot", ["label"])


MERGE_SQL = "SELECT region, gmv FROM a ORDER BY region"
OUTER_SQL = "SELECT region, gmv FROM m ORDER BY region"
MERGED_TYPES = {"region": "text", "gmv": "number"}


def merged(run_id, sql, rows, *, template=None, nested=False, twice=False, lost=False, bare=False, unsealed=False):
    """取数节点查一次，合并节点把它当输入 a 再执行一条合并 SQL（nested：再套一层合并；twice：这次多接一个输入 b），
    口径卡取合并结果的一格。追不下去的几种（只用于一层合并）：lost 合并的快照文件不在了；bare 合并没记录输入（快照、
    merge.end 里都没有）；unsealed 上游那次查询不在封存范围内（事件里没有它）。"""
    nodes = [node("start", "input", "开始"), node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": template or sql}),
             node("join", "merge", "合并", sql=MERGE_SQL, inputs={"a": "fetch", **({"b": "fetch"} if twice else {})})]
    if nested:
        nodes.append(node("outer", "merge", "再合并", sql=OUTER_SQL, inputs={"m": "join"}))
    nodes.append(node("card", "metrics", "口径卡", caliber="周报口径", caliber_version="v1"))
    run = Fake(run_id, graph=chain(*nodes))
    fetch = run.tool("fetch", sql, rows=rows)
    table = [[r[0], r[1]] for r in rows]
    inputs = [("a", "fetch", fetch)] + ([("b", "fetch", fetch)] if twice else [])
    top, owner = run.merge("join", MERGE_SQL, inputs, ["region", "gmv"], table, types=MERGED_TYPES), "join"
    if nested:
        top, owner = run.merge("outer", OUTER_SQL, [("m", "join", top)], ["region", "gmv"], table,
                               types=MERGED_TYPES), "outer"
    run.card([metric("east", f"cell(nodes.{owner}, 0, 'gmv')", table[0][1],
                     [cell_in(0, "gmv", table[0][1], top, node_id=owner)], name="东区金额")])
    if lost:
        del run.store[top]
    if bare:
        run.store[top] = {k: v for k, v in run.store[top].items() if k != "inputs"}
        run.events = [(seq, etype, nid, {k: v for k, v in data.items() if k != "inputs"}
                       if etype == "merge.end" else data) for seq, etype, nid, data in run.events]
    if unsealed:
        run.events = [e for e in run.events if e[2] != "fetch"]
    return run


def test_a_merged_cell_follows_the_queries_feeding_the_merge():
    """合并 SQL 一字没改，喂它的取数 SQL 加了筛选：读合并结果的指标是 SQL 变了，不拆、不归因（嵌套的合并也追到底）。"""
    paid = region_sql(extra=" AND status = 'paid'")
    for nested in (False, True):
        east = by_id(run_compare(merged("r1", region_sql(), BASE_ROWS, nested=nested),
                                 merged("r2", paid, NEXT_ROWS, nested=nested)))["east"]
        assert east["classification"] == "sql_changed", (nested, east["reasons"])
        assert east["decomposition"] is None and east["attribution"] is None
        assert any("区域取数" in r and "SQL" in r for r in east["reasons"]), east["reasons"]
    # 上游只是代进去的日期不同：参数变化
    template = region_sql(("{{ input.start }}", "{{ input.end }}"))
    east = by_id(run_compare(merged("r1", region_sql(), BASE_ROWS, template=template),
                             merged("r2", region_sql(WEEK37), NEXT_ROWS, template=template)))["east"]
    assert east["classification"] == "params_only", east["reasons"]
    # 合并 SQL 和上游都没变：数据变化
    east = by_id(run_compare(merged("r1", region_sql(), BASE_ROWS), merged("r2", region_sql(), NEXT_ROWS)))["east"]
    assert east["classification"] == "data_only", east["reasons"]
    # 合并节点这次多接了一个输入
    east = by_id(run_compare(merged("r1", region_sql(), BASE_ROWS), merged("r2", region_sql(), NEXT_ROWS, twice=True)))
    assert east["east"]["classification"] == "sql_changed" and any("「b」" in r for r in east["east"]["reasons"])


def test_a_merged_cell_whose_inputs_cannot_be_followed():
    """合并的快照文件不在了：按 merge.end 记的输入接着追，上游加了筛选照样认得出。合并没记录输入、上游那次查询不在
    封存范围内：说不清上游的取数变没变，不报成数据变化。"""
    paid = region_sql(extra=" AND status = 'paid'")
    east = by_id(run_compare(merged("r1", region_sql(), BASE_ROWS, lost=True),
                             merged("r2", paid, NEXT_ROWS, lost=True)))["east"]
    assert east["classification"] == "sql_changed", east["reasons"]
    assert any("「a」" in r and "区域取数" in r for r in east["reasons"]), east["reasons"]
    for broken, said in (({"bare": True}, "没有记录它的输入"), ({"unsealed": True}, "不在封存范围内")):
        east = by_id(run_compare(merged("r1", region_sql(), BASE_ROWS, **broken),
                                 merged("r2", region_sql(), NEXT_ROWS, **broken)))["east"]
        assert east["classification"] == "incomparable", (broken, east["reasons"])
        assert any(said in r for r in east["reasons"]), (broken, east["reasons"])


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


def test_a_merge_query_is_compared_by_its_merge_sql():
    """合并查询只记在节点的台账里（没有 tool.end），SQL 在快照和 merge.end 里；快照文件不在了也比得了。"""
    def make(run_id, sql, *, drop=False):
        run = Fake(run_id)
        run.tool("fetch", region_sql())
        snap = {"columns": ["region", "gmv"], "rows": [["东区", 1.0]], "truncated": False, "sql": sql,
                "source": MERGE_SOURCE, "sources": ["shop"]}
        artifact = run.put(snap)
        run.add("node.started", "join", {"node_type": "merge"})
        run.add("merge.end", "join", {"sql": sql, "query_artifact": artifact, "rows": 1})
        run.add("node.finished", "join", {"duration_ms": 1, "attempt": 1, "evidence": [
            {"kind": "query", "node_id": "join", "exec": 1, "artifact": artifact, "source": MERGE_SOURCE,
             "columns": ["region", "gmv"], "rows": 1, "truncated": False}]})
        if drop:
            del run.store[artifact]
        return run

    result = run_compare(make("r1", "SELECT region, gmv FROM a WHERE gmv > 10"),
                         make("r2", "SELECT region, gmv FROM a WHERE gmv > 20"))
    join = result_queries(result, "join")
    assert (join["ordinal"], join["structure"], join["literals"]) == (1, "params_only", [{"base": "10", "target": "20"}])
    result = run_compare(make("r1", "SELECT region, gmv FROM a WHERE gmv > 10", drop=True),
                         make("r2", "SELECT region, gmv FROM a WHERE gmv > 10 AND region <> '西区'"))
    join = result_queries(result, "join")
    assert join["structure"] == "changed" and join["base_sql"] == "SELECT region, gmv FROM a WHERE gmv > 10"
    assert "无法读取" in join["problem"]


def test_a_node_run_in_a_loop_is_paired_execution_by_execution():
    def make(run_id, regions):
        run = Fake(run_id)
        for region in regions:
            run.tool("fetch", f"SELECT SUM(amount) AS gmv FROM orders WHERE region = '{region}'", ["gmv"], [[1.0]],
                     types={"gmv": "number"})
        return run

    result = run_compare(make("r1", ["东区", "南区", "西区"]), make("r2", ["东区", "北区"]))
    assert [(q["ordinal"], q["structure"]) for q in result["queries"]] == [(1, "same"), (2, "params_only"), (3, "removed")]
    assert result["queries"][1]["literals"] == [{"base": "'南区'", "target": "'北区'"}]


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
