"""运行对比的守护用例：对抗复验找出的几处「拿掉规则、测试仍全绿」和一处遮罩下的放行。

- 区域列设了遮罩时，部分行挤出引用的行段照样核对得出（说明只写行号），和「真的少了」分得清；什么都没挪的照常是数据变化
- 单格「同一行」的规则两个方向都守住（基准运行里那个区域只有引用的那一行）
- 归因按哪组维度说，就按哪组维度在两边各自引用的行上核对
- 写入输入的节点改了配置时，严格拆分不拆
- 记不出写入者时，并行支路上的查询也算进「它上游变了什么」

夹具全部虚构，形状照 tests/fixtures/compare_runs.py。
"""
from __future__ import annotations

import json

from app.engine import compare
from tests.fixtures.compare_runs import (
    BASE_ROWS, COLUMNS, NEXT_ROWS, TOOL, TYPES, WEEK37, Fake, amounts_in, chain, metric, node, region_sql, scalar_in,
)

REGIONS = ("东区", "南区", "西区", "北区", "中区")

#: 区域 × 日期：日期每期都不同、不当维度，同一个区域占几行
DAY = {"sql": "SELECT region, day, SUM(amount) AS gmv FROM orders GROUP BY region, day ORDER BY region, day",
       "columns": ["region", "day", "gmv"], "types": {"region": "text", "day": "date", "gmv": "number"}}
#: 区域 × 渠道 × 日期
CH = {"sql": "SELECT region, channel, day, SUM(amount) AS gmv FROM orders GROUP BY region, channel, day ORDER BY gmv DESC",
      "columns": ["region", "channel", "day", "gmv"],
      "types": {"region": "text", "channel": "text", "day": "date", "gmv": "number"}}


def spanned(run_id, rows, span, *, columns, types, sql, masked=()):
    """Agent 交一段行（span），口径卡对它求和。masked：这次运行每份快照都遮的列。"""
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


def element(run_id, rows, span, *, index=0, sql=None, columns=COLUMNS, types=TYPES):
    """口径卡取 Agent 数组字段的第 index 个元素（单格）。"""
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


def compared(base, target):
    result = compare.compare(base.side(), target.side())
    json.dumps(result, allow_nan=False)
    return result


def one(base, target, metric_id):
    return {m["metric_id"]: m for m in compared(base, target)["metrics"]}[metric_id]


# --------------------------------------------------------------------------- 遮罩下的核对

#: 东、南、西各两天；引用第 1 至 4 行
SIX = [["东区", "09-01", 100.0], ["东区", "09-02", 100.0], ["南区", "09-01", 50.0], ["南区", "09-02", 50.0],
       ["西区", "09-01", 30.0], ["西区", "09-02", 30.0]]
#: 东区这次多了一天，南区的第二天挤到第 5 行（还在快照里，只是不在引用的行里）
PUSHED = [["东区", "09-07", 100.0], ["东区", "09-08", 100.0], ["东区", "09-09", 100.0], ["南区", "09-08", 50.0],
          ["南区", "09-09", 50.0], ["西区", "09-08", 30.0], ["西区", "09-09", 30.0]]
#: 对照：南区这次真的只有一天
FEWER = [["东区", "09-07", 100.0], ["东区", "09-08", 100.0], ["东区", "09-09", 100.0], ["南区", "09-08", 50.0],
         ["西区", "09-08", 30.0], ["西区", "09-09", 30.0]]


def test_a_masked_dimension_still_tells_rows_pushed_out_from_rows_gone():
    """区域列设了遮罩（两边都遮、只有一边遮）：它是唯一认得出行的那一列。以前遮了就不拿它认、只能按行号认，南区第二天
    挤出引用的行段和南区真的少了一天的输出一字不差，都是「数据变化」。现在照样拿它核对，挤出去的说不清，说明只写行号；
    真的少了的照常是数据变化。区域名在哪儿都不出现。"""
    for label, mb, mt in (("两边都遮", ("region",), ("region",)), ("只有这次遮", (), ("region",)),
                          ("只有基准遮", ("region",), ())):
        moved = compared(spanned("r1", SIX, (0, 3), **DAY, masked=mb), spanned("r2", PUSHED, (0, 3), **DAY, masked=mt))
        total = {m["metric_id"]: m for m in moved["metrics"]}["total"]
        assert total["classification"] == "incomparable" and total["attribution"] is None, label
        assert "第 5 行" in total["reasons"][0] and "维度列设了遮罩" in total["reasons"][0], label
        assert compare.MASKED_NOTE in moved["notes"], label
        gone = compared(spanned("r1", SIX, (0, 3), **DAY, masked=mb), spanned("r2", FEWER, (0, 3), **DAY, masked=mt))
        total = {m["metric_id"]: m for m in gone["metrics"]}["total"]
        assert total["classification"] == "data_only", (label, total["reasons"])
        for result in (moved, gone):
            text = json.dumps(result, ensure_ascii=False)
            assert not [r for r in REGIONS if r in text], label


def test_a_masked_dimension_with_nothing_moved_is_still_a_data_change():
    """遮罩下照样核对，但没挪的不能因为看不见值就一律说不清：两次引用的是同样几个区域的同样几天、只是金额变了，照常是
    数据变化（定时周报常见的取前几名、区域列设了遮罩，不能每周都说不清）。"""
    changed = [[r, d, v * 1.1] for r, d, v in SIX]
    total = one(spanned("r1", SIX, (0, 3), **DAY, masked=("region",)),
                spanned("r2", changed, (0, 3), **DAY, masked=("region",)), "total")
    assert total["classification"] == "data_only", total["reasons"]


# --------------------------------------------------------------------------- 单格、归因维度

def test_an_element_whose_region_had_one_row_at_base_is_not_the_same_row():
    """单格（Agent 两次都交第 2 至 3 行，口径卡取第一个元素即第 2 行）：基准运行那是南区唯一的一天（50）；这次东区没有
    数据，南区两天排在第 1、2 行，第 2 行是南区的第二天（45），同一天的南区其实是 60。两次都是南区，可不是同样的那一行，
    说不清——按行号认会写成 50 → 45，正负号都反了。基准运行那边南区只有引用的那一行时，「排在前面的行数」也要比。"""
    before = [["东区", "2026-09-01", 100.0], ["南区", "2026-09-01", 50.0], ["西区", "2026-09-01", 30.0]]
    after = [["南区", "2026-09-08", 60.0], ["南区", "2026-09-09", 45.0], ["西区", "2026-09-08", 30.0]]
    m = one(element("r1", before, (1, 2), **DAY), element("r2", after, (1, 2), **DAY), "first")
    assert m["classification"] == "incomparable", m["reasons"]
    m = one(element("r1", after, (1, 2), **DAY), element("r2", before, (1, 2), **DAY), "first")
    assert m["classification"] == "incomparable", m["reasons"]


def test_attribution_dimensions_are_checked_on_each_side_own_cited_rows():
    """渠道在引用的行里两边没有相同的值（不当维度），归因按区域说。南区这次有一行（线下）在引用的行外面：按两边各自
    引用的行认维度，查得出南区不是同一组数；只拿一边的引用行认维度（两边都用基准的、或都用这次的），会放过它，归因
    把南区写成 80 → 90，和南区真的只有一行的对照一字不差。两个方向都核对。"""
    base = [["东区", "线上", "09-01", 100.0], ["东区", "线下", "09-01", 50.0], ["南区", "线上", "09-01", 80.0],
            ["西区", "线上", "09-01", 30.0]]
    this = [["东区", "团购", "09-08", 100.0], ["东区", "团购", "09-09", 60.0], ["南区", "团购", "09-08", 90.0],
            ["南区", "线下", "09-08", 40.0], ["西区", "线上", "09-08", 30.0]]
    for b, t in ((base, this), (this, base)):
        m = one(spanned("r1", b, (0, 2), **CH), spanned("r2", t, (0, 2), **CH), "total")
        assert m["classification"] == "incomparable", m["reasons"]


def test_an_element_on_a_misshapen_snapshot_is_not_taken_as_the_same():
    """单格，一边快照里有一行和列对不上（少了一格）：认不出各行是哪个维度值，说不清，不按行号当成同一个数。"""
    broken = [["北区", 90.0, 0.0], *NEXT_ROWS[:2], ["西区", 600.0]]
    for b, t in ((BASE_ROWS, broken), (broken, BASE_ROWS)):
        m = one(element("r1", b, (0, 2)), element("r2", t, (0, 2)), "first")
        assert m["classification"] == "incomparable" and "同一个维度值" in m["reasons"][0], m["reasons"]


# --------------------------------------------------------------------------- 严格拆分与写入者

def test_no_strict_split_when_the_writer_of_an_input_was_reconfigured():
    """口径 ×2 改成 ×3（v1 → v2），输入 vars.adj 由代码节点「调整」写入，这次「调整」也改了代码：旧口径用到的输入本身
    就因为配置变了而不同，拆成「口径效应 12、数据效应 4」会把代码改动带来的变化算成数据效应。不拆，说明原因。"""

    def run(run_id, rows, value, factor, version, code):
        graph = chain(node("start", "input", "开始"), node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": region_sql()}),
                      node("calc", "code", "调整", code=code, assign_to="adj"),
                      node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version=version),
                      node("out", "output", "成果"))
        r = Fake(run_id, graph=graph)
        r.tool("fetch", region_sql(), rows=rows)
        r.code("calc", value, "adj")
        r.card([metric("calc", f"vars.adj * {factor}", value * factor,
                       [scalar_in("vars.adj", value, "calc", "code")])], version=version)
        return r

    m = one(run("r1", BASE_ROWS, 10, 2, "v1", "x = 1"), run("r2", NEXT_ROWS, 12, 3, "v2", "x = 2"), "calc")
    assert m["decomposition"] is None and "无法严格拆分" in m["decomposition_note"], m


def test_an_unknown_writer_counts_queries_on_parallel_branches():
    """记不出 vars.adj 的写入者：并行支路（开始 → 旁路取数 → 成果）上的查询在口径卡之前可能执行过，也算进它上游。只有
    旁路取数的结果变了是数据变化；旁路取数只是代入的日期不同，是参数变化。"""

    def run(run_id, value, aside_rows, aside_sql):
        graph = chain(node("start", "input", "开始"),
                      node("fetch", "tool", "区域取数", tool=TOOL, args={"sql": region_sql()}),
                      node("calc", "code", "调整", code="x", assign_to="adj"),
                      node("card", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1"),
                      node("out", "output", "成果"))
        graph["nodes"].append(node("aside", "tool", "旁路取数", tool=TOOL, args={"sql": region_sql()}))
        graph["edges"] += [{"source": "start", "target": "aside"}, {"source": "aside", "target": "out"}]
        r = Fake(run_id, graph=graph)
        r.tool("fetch", region_sql(), rows=BASE_ROWS)
        r.tool("aside", aside_sql, rows=aside_rows)
        r.code("calc", value, "adj")
        r.card([metric("calc", "vars.adj * 2", value * 2, [scalar_in("vars.adj", value, None, "state")])])
        return r

    m = one(run("r1", 10, BASE_ROWS, region_sql()), run("r2", 12, NEXT_ROWS, region_sql()), "calc")
    assert m["classification"] == "data_only", m["reasons"]
    m = one(run("r1", 10, BASE_ROWS, region_sql()), run("r2", 12, NEXT_ROWS, region_sql(WEEK37)), "calc")
    assert m["classification"] == "params_only", m["reasons"]
