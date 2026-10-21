"""analysis.run / recompute（app/analysis/__init__.py）：结论的形状严格按契约、同输入两次 run 逐字节相同、复算和运行逐字段相同且
只用记下的 probes（不现查、不读目录）、调用方写的系统字段不收、复算取不回输入时的说法。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app import analysis
from app.analysis import contract as c
from app.analysis.contract import CallError, effective_settings, same
from tests.analysis_samples import CATALOGS, analyze, call_error, fixed_prober, recomputed, snap

WEEKLY_SQL = "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week"
WEEKLY = snap("Q2", WEEKLY_SQL, ["region", "week", "gmv"],
              [["华东", "2026-W41", 820], ["华东", "2026-W42", 700], ["华北", "2026-W41", 600],
               ["华北", "2026-W42", 560], ["华南", "2026-W41", 700], ["华南", "2026-W42", 666]])
SERIES = snap("Q1", "SELECT DATE(order_date, 'weekday 0', '-6 days') AS week, SUM(amount) AS gmv FROM orders GROUP BY 1",
              ["week", "gmv"], [[(date(2026, 6, 1) + timedelta(days=7 * i)).isoformat(), 100 + (i % 4) * 3 + i]
                                for i in range(20)])
RATES = snap("Q3", "SELECT region, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY region",
             ["region", "refunds", "orders"], [["西南", 80, 1000], ["华东", 40, 1000], ["华北", 41, 1000]])
BASE = snap("Q4", "SELECT region, SUM(amount) AS gmv FROM orders WHERE order_date >= '2026-10-05' AND "
                  "order_date < '2026-10-12' GROUP BY region", ["region", "gmv"], [["华东", 820], ["华北", 600]])
TARGET = snap("Q5", "SELECT region, SUM(amount) AS gmv FROM orders WHERE order_date >= '2026-10-12' AND "
                    "order_date < '2026-10-19' GROUP BY region", ["region", "gmv"], [["华东", 700], ["华北", 590]])
SNAPS = [WEEKLY, SERIES, RATES, BASE, TARGET]
PROBE = {("orders", "order_date"): ("2025-01-01", "2026-10-20")}

CALLS = [
    ("decompose", "additive", {"query": "Q2", "by": "region", "period": "week", "base": "2026-W41",
                               "target": "2026-W42", "measure": "gmv", "label": "销售额"}),
    ("decompose", "additive", {"base_query": "Q4", "target_query": "Q5", "by": "region", "measure": "gmv"}),
    ("baseline", None, {"query": "Q1", "period": "week", "measure": "gmv"}),
    ("trend", None, {"query": "Q1", "period": "week", "measure": "gmv"}),
    ("differ", "rate", {"query": "Q3", "by": "region", "numerator": "refunds", "denominator": "orders"}),
    ("comparable", None, {"base_query": "Q4", "target_query": "Q5", "measure": "gmv"}),
]


async def _run(op, mode, params, *, probe=None, **kw):
    return await analyze(op, mode, snaps=SNAPS, probe=probe or fixed_prober(PROBE), check=False, **params, **kw)


def _assert_shape(result):
    """结论的字段、取值严格按契约。"""
    assert set(result) == set(c.AnalysisResult.__annotations__)
    assert result["op"] in c.OPS and result["method"] == c.METHODS[(result["op"], result["mode"])]
    assert result["status"] in ("ok", "refused")
    if result["status"] == "refused":
        assert result["reason_code"] in c.REASONS and result["reason"] and result["verdict"] is None
    else:
        assert result["reason_code"] is None and result["reason"] is None
        assert result["verdict"]["code"] in c.VERDICTS[result["op"]]
        assert set(result["verdict"]) == {"code", "subjects", "direction"}
        assert all(isinstance(s, list) and all(isinstance(x, str) for x in s) for s in result["verdict"]["subjects"])
        assert result["verdict"]["direction"] in (None, "up", "down")
        table = result["table"]
        assert set(table) == {"columns", "labels", "rows", "column_types"}
        assert set(table["labels"]) == set(table["columns"]) and set(table["column_types"]) <= set(table["columns"])
        assert list(table["column_types"]) == [c for c in table["columns"] if c in table["column_types"]]
        assert set(table["column_types"].values()) <= {"number", "text", "date", "datetime", "time", "boolean"}
        assert all(len(r) == len(table["columns"]) for r in table["rows"])
    assert all(v is None or isinstance(v, str) for v in result["values"].values())
    assert all(set(cv) == {"code", "text"} and cv["code"] in c.CAVEATS for cv in result["caveats"])
    assert set(result["settings"]) <= set(c.DEFAULTS)
    assert all(i.get("artifact") for i in result["inputs"])
    assert result["criterion"] and result["phrase"]
    json.dumps(result, ensure_ascii=False)               # 写得进 JSON


@pytest.mark.parametrize("op,mode,params", CALLS)
async def test_every_result_has_the_contract_shape_and_recomputes_the_same(op, mode, params):
    result = await _run(op, mode, params)
    _assert_shape(result)
    again = recomputed(result, SNAPS)
    assert same(result, again)
    assert json.dumps(again, sort_keys=True, ensure_ascii=False) == json.dumps(result, sort_keys=True, ensure_ascii=False)


@pytest.mark.parametrize("op,mode,params", CALLS)
async def test_the_same_inputs_give_byte_identical_results(op, mode, params):
    one = await _run(op, mode, params)
    two = await _run(op, mode, params)
    assert json.dumps(one, sort_keys=True, ensure_ascii=False) == json.dumps(two, sort_keys=True, ensure_ascii=False)


async def test_recompute_uses_the_recorded_probes_and_facts_not_live_ones():
    result = await _run("comparable", None, {"base_query": "Q4", "target_query": "Q5", "measure": "gmv"})
    assert result["verdict"]["code"] == "comparable" and len(result["probes"]) == 1
    # 记下的范围换一个（表里数据只到 10-16）：复算按记下的算，结论跟着变——它没有现查
    edited = {**result, "probes": [{**result["probes"][0], "max": "2026-10-16"}]}
    assert recomputed(edited, SNAPS)["verdict"]["code"] == "incomparable"
    # 目录事后改了也不影响复算：目录事实记在 params 里
    decompose = await _run(*CALLS[0])
    assert decompose["params"]["catalog"]["labels"]["region"] == "地区"
    CATALOGS["shop"]["orders"]["columns"]["region"]["label"]["value"] = "大区"
    try:
        assert same(decompose, recomputed(decompose, SNAPS))
    finally:
        CATALOGS["shop"]["orders"]["columns"]["region"]["label"]["value"] = "地区"


async def test_system_params_written_by_the_caller_are_ignored():
    params = {"query": "Q1", "period": "week", "measure": "gmv", "today": "2020-01-01",
              "catalog": {"labels": {"gmv": "假的叫法"}}, "depth": 9}
    result = await analysis.run({"op": "baseline", "params": params}, {s["ref"]: s for s in SNAPS}.get,
                                settings=effective_settings(None),
                                context={"today": date(2026, 10, 21), "catalogs": CATALOGS})
    assert result["params"]["today"] == "2026-10-21"
    assert "假的叫法" not in result["phrase"] and "depth" not in result["params"]


async def test_analysis_card_overrides_reach_the_criterion_and_settings():
    params = CALLS[0][2]
    result = await _run("decompose", "additive", params, settings={"main_share": 0.6})
    assert result["settings"]["main_share"] == 0.6 and "占总变化的 60% 以上" in result["criterion"]


async def test_call_errors_for_unknown_ops_and_modes():
    with pytest.raises(CallError) as e:
        await analysis.run({"op": "forecast", "params": {}}, {}.get, settings=effective_settings(None), context={})
    assert e.value.code == "unknown_op" and "变化拆解" in e.value.message
    with pytest.raises(CallError) as e:
        await analysis.run({"op": "decompose", "mode": "share", "params": {}}, {}.get,
                           settings=effective_settings(None), context={})
    assert e.value.code == "unknown_mode" and "additive / ratio / factor" in e.value.message
    missing = await call_error("baseline", snaps=SNAPS, query="Q1", period="week")
    assert missing.code == "missing_param" and missing.message == "没写度量列"


async def test_recompute_says_when_an_input_cannot_be_loaded():
    result = await _run(*CALLS[0])
    with pytest.raises(CallError) as e:
        analysis.recompute(result, lambda artifact: None)
    assert e.value.code == "input_missing" and e.value.message == "复算取不回 Q2 的查询快照"


async def test_the_hypothesis_is_recorded_but_never_rendered():
    result = await _run(*CALLS[0], hypothesis="可能是华东促销结束 30% 的影响")
    assert result["hypothesis"] == "可能是华东促销结束 30% 的影响"
    assert "促销" not in result["phrase"] and "30" not in result["phrase"]
    assert same(result, {**recomputed(result, SNAPS), "hypothesis": None})


async def test_ledger_entries_name_the_real_input_snapshots():
    result = await _run(*CALLS[1])
    entry = c.ledger_entry(result, node_id="agent", exec_no=1, artifact="f" * 64, call_id="call-1")
    assert entry["inputs"] == [BASE["artifact"], TARGET["artifact"]] and entry["op"] == "decompose"


def test_the_call_limit_error_is_spoken_with_the_limit():
    error = analysis.exhausted({"max_calls": 5})
    assert error.code == "max_calls" and error.message == "这个 Agent 这次执行最多做 5 次分析，已经用完：用已有的分析结论写，不再新做分析"


# --------------------------------------------------------------------------
# 随数据变化的调用错误（分析卡传 refuse_data_errors）：记成算不了的结论，复算也成立
# --------------------------------------------------------------------------

ONE_REGION = snap("Q6", WEEKLY_SQL, ["region", "week", "gmv"], [["华东", "2026-W41", 820], ["华东", "2026-W42", 700]])
TOTALS = snap("Q7", "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week", ["week", "gmv"],
              [["2026-W42", 1926], ["2026-W41", None]])
FACTORS = snap("Q8", "SELECT week, SUM(amount) AS gmv, COUNT(*) AS orders, AVG(amount) AS aov FROM orders "
                      "GROUP BY week",
               ["week", "gmv", "orders", "aov"], [["2026-W41", 2120, 20, 106], ["2026-W42", None, 0, None]])
ONE_GROUP = snap("Q9", "SELECT region, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY region",
                 ["region", "refunds", "orders"], [["西南", 80, 1000]])
WEEK_RATES = snap("Q10", "SELECT week, SUM(refund_flag) AS refunds, COUNT(*) AS orders FROM orders GROUP BY week",
                  ["week", "refunds", "orders"], [["2026-W41", 30, 1000], ["2026-W42", None, 1000]])
REFUSALS = [WEEKLY, SERIES, RATES, ONE_REGION, TOTALS, FACTORS, ONE_GROUP, WEEK_RATES]
BY_REGION = {"query": "Q2", "by": ["region"], "period": "week", "base": "2026-W41", "target": "2026-W42",
             "measure": "gmv"}


async def _refused(op, mode=None, *, parents=None, **params):
    """refuse_data_errors 为真跑两次：逐字节相同、形状照契约、复算逐字段相同（analyze 里按真复算）；不传时照旧是调用错误。"""
    one = await analyze(op, mode, snaps=REFUSALS, parents=parents, refuse_data_errors=True, **params)
    two = await analyze(op, mode, snaps=REFUSALS, parents=parents, refuse_data_errors=True, **params)
    assert json.dumps(one, sort_keys=True, ensure_ascii=False) == json.dumps(two, sort_keys=True, ensure_ascii=False)
    _assert_shape(one)
    assert one["status"] == "refused" and one["phrase"] == f"{c.OP_LABELS[op]}没有做成：{one['reason']}"
    error = await call_error(op, mode, snaps=REFUSALS, parents=parents, **params)
    assert error.code in analysis.DATA_ERRORS and error.reason == one["reason"]
    return one


def test_the_data_errors_are_exported():
    assert analysis.DATA_ERRORS == {"parent_refused", "parent_key_missing", "period_not_found", "row_out_of_range",
                                    "headline_empty", "unknown_group", "too_few_groups", "empty_cell"}
    assert {"parent_refused", "input_unavailable"} <= set(c.REASONS)


async def test_a_drill_under_a_refused_parent_is_itself_refused():
    parent = await analyze("decompose", "additive", snaps=REFUSALS, **{**BY_REGION, "query": "Q6"})
    assert parent["reason_code"] == "too_few_segments"
    result = await _refused("decompose", "additive", parents={"where": parent},
                            **{**BY_REGION, "by": ["region"], "parent": {"of": "where", "key": ["华东"]}})
    assert result["reason_code"] == "parent_refused"
    assert result["reason"] == "上一层「where」没有做成（按地区只有 1 段，至少要 2 段才拆得开），不能在它下面下钻"
    # 上一层、取过的快照都记进输入（复算按它们走到同一处）；参数照调用原样，阈值同这个模式自己算不了时
    assert [i["role"] for i in result["inputs"]] == ["parent", "data"]
    assert result["inputs"][1] == {"ref": "Q2", "artifact": WEEKLY["artifact"], "role": "data",
                                   "columns": ["region", "gmv", "week"], "rows": "all"}
    assert result["params"]["parent"] == {"of": "where", "key": ["华东"]}
    assert result["settings"] == {**{k: c.DEFAULTS[k] for k in ("main_share", "concentration", "offset_ratio",
                                                                   "flat_pct", "table_top_k")}, "max_drill_depth": 3}
    assert result["table"] is None and result["verdict"] is None


async def test_segments_periods_cells_and_groups_missing_this_time():
    parent = await analyze("decompose", "additive", snaps=REFUSALS, **BY_REGION)
    key = await _refused("decompose", "additive", parents={"A1": parent},
                         **{**BY_REGION, "parent": {"of": "A1", "key": ["东北"]}})
    assert key["reason_code"] == "input_unavailable" and key["reason"] == "上一层「A1」里没有「东北」这一段"
    period = await _refused("decompose", "additive", **{**BY_REGION, "target": "2026-W43", "label": "销售额，主要来自华东"})
    assert period["reason"] == "期次列「week」里没有本期「2026-W43」" and period["params"]["label"] == "销售额"
    row = await _refused("decompose", "additive", **BY_REGION, headline={"base": "Q7.r5.gmv", "target": "Q7.r0.gmv"})
    assert row["reason"] == "基期总数那一格（Q7.r5.gmv）不在：Q7 这次只有 2 行"
    assert [i["role"] for i in row["inputs"]] == ["data", "headline_base"]
    empty = await _refused("decompose", "additive", **BY_REGION, headline={"base": "Q7.r1.gmv", "target": "Q7.r0.gmv"})
    assert empty["reason"] == "基期总数那一格（Q7.r1.gmv）是空的"
    factor = await _refused("decompose", "factor", query="Q8", period="week", base="2026-W41", target="2026-W42",
                            product="gmv", factors=["orders", "aov"])
    assert factor["reason"] == "本期的「gmv」那一格是空的（Q8 第 1 行）"
    target = await _refused("baseline", query="Q1", period="week", measure="gmv", target="2020-01-06")
    assert target["reason"] == "期次列「week」里没有本期「2020-01-06」（或那一期为空）"
    assert target["settings"] == {k: c.DEFAULTS[k] for k in ("k_warn", "k_alert", "min_history", "max_history")}
    side = await _refused("comparable", query="Q2", period="week", base="2026-W40", target="2026-W42")
    assert side["reason"] == "期次列「week」里没有基期「2026-W40」" and side["settings"] == {}
    rates = {"by": ["region"], "numerator": "refunds", "denominator": "orders"}
    group = await _refused("differ", "rate", query="Q3", **rates, pairs=[["华东", "东北"]])
    assert group["reason"] == "分组列「region」里没有「东北」这一组"
    assert group["settings"] == {"differ_alpha": 0.05, "min_expected": 5}
    few = await _refused("differ", "rate", query="Q9", **rates)
    assert few["reason"] == "分组列「region」里只有 1 组，至少要两组才比得了"
    weeks = {"query": "Q10", "period": "week", "numerator": "refunds", "denominator": "orders"}
    gone = await _refused("differ", "rate", **weeks, base="2026-W40", target="2026-W41")
    assert gone["reason"] == "期次列「week」里没有基期「2026-W40」"
    null = await _refused("differ", "rate", **weeks, base="2026-W41", target="2026-W42")
    assert null["reason"] == "「refunds」那一格是空的（Q10 第 1 行）"


async def test_structural_call_errors_still_fail_even_when_data_errors_are_refused():
    for params in ({**BY_REGION, "by": ["channel"]}, {**BY_REGION, "by": ["region", "week"]}):
        with pytest.raises(CallError) as e:
            await analyze("decompose", "additive", snaps=REFUSALS, refuse_data_errors=True, check=False, **params)
        assert e.value.code not in analysis.DATA_ERRORS and e.value.reason is None
