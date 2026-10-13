"""数字推演的几处补测（期 4 打磨）：

- 取整只有一个函数：推演重算（engine/recompute.py）用的就是口径卡（engine/nodes/metrics.py）那一个，口径卡的取整
  规则哪天改了，推演跟着变，不会一边改了、另一边还按旧的算
- _doubt 里 moot 的那一支：一个输入既嵌在下标里、又直接参与计算，包着它的引用给了新值之后，它直接参与的那一处照常
  拿它的值——排除行后它变没变说不准，指标仍得说不准，不能借着「嵌在里面的那一处已无关紧要」放过去
- 决定取哪一格的单格照给行段：它自己那一行勾得了，勾掉它确定是排除后无法计算

夹具沿用 test_recompute 的虚构快照和卡。只导入夹具，不导入它的测试函数：导进来的 test_* 会在这里再收集一遍。
"""
from __future__ import annotations

import pytest

from app.engine import recompute
from app.engine.nodes import metrics as metrics_node
from app.engine.recompute import Changes, whatif
from tests.test_recompute import AMOUNTS, FEE, REGION, S1, cell, lone, scalar, sources

# --------------------------------------------------------------------------
# 取整：和口径卡共用一个函数
# --------------------------------------------------------------------------


def test_the_whatif_rounds_with_the_cards_own_function():
    assert recompute.round_metric is metrics_node.round_metric


@pytest.mark.parametrize("value, decimals, rounded", [
    # 二进制里落在半中间的数照 Python 的 round：口径卡当时就是这么算的
    (2.675, 2, 2.67),
    (0.125, 2, 0.12),
    # 负的小数位：取整到十位、百位
    (1234.5, -1, 1230.0),
    (-1250.0, -2, -1200.0),
    # 只取整浮点：整数、文本、空值原样
    (7, 2, 7),
    ("3.14159", 2, "3.14159"),
    (None, 2, None),
    # 没写小数位、小数位不是整数（老卡上的怪值）：不取整
    (1.25, None, 1.25),
    (1.25, True, 1.25),
    (1.25, 1.0, 1.25),
])
def test_round_metric_rounds_only_floats_by_an_integer_number_of_places(value, decimals, rounded):
    got = metrics_node.round_metric(value, decimals)
    assert got == rounded and type(got) is type(rounded)


# --------------------------------------------------------------------------
# _doubt 里 moot 的那一支
# --------------------------------------------------------------------------

#: 按区域取费率，区域名本身还拿来数字数。区域名是查询交回之后才算出来的（label 节点，对不上快照）
RATE = "vars.kpi.rates[vars.kpi.region]"
TWICE = lone("fee_twice", f"round({RATE} * sum(vars.kpi.amounts), 1) + len(vars.kpi.region)",
             "round(0.05 * sum(vars.kpi.amounts), 1) + len('华南')", 19.0,
             [scalar(RATE, 0.05), scalar("vars.kpi.region", "华南", node="label"), AMOUNTS], decimals=1)
TIMED = {"returned": {S1: 10}, "finished": {"fetch": 5, "label": 12}}


def test_an_input_used_directly_and_as_an_index_stays_doubted_after_the_item_has_a_new_value():
    timed = sources(**TIMED)
    assert recompute.recompute(TWICE["metrics"][0], Changes(), timed)["new"] == 19.0
    # 费率给了新值：区域名嵌在下标里的那一处已无关紧要。可它还直接参与计算（len），排除行后它变没变说不准
    [fee] = whatif(TWICE, Changes(inputs={RATE: 0.1}, exclude={S1: frozenset({1})}), timed)["metrics"]
    assert (fee["status"], fee["new"], fee["changed"]) == ("unsupported", None, None), fee
    assert "排除行对 vars.kpi.region 的影响无法确定" in fee["reason"]
    # 对照：不排除行，费率的新值照常算进去
    [fee] = whatif(TWICE, Changes(inputs={RATE: 0.1}), timed)["metrics"]
    assert (fee["status"], fee["new"]) == ("ok", round(0.1 * 340.0, 1) + 2), fee


def test_an_index_used_only_inside_the_item_is_still_moot():
    """对照：同一个区域名只嵌在下标里（不直接参与计算）时，费率给了新值就无关紧要，按剩下的行照常算。"""
    card = lone("fee_once", f"round({RATE} * sum(vars.kpi.amounts), 1)", "round(0.05 * sum(vars.kpi.amounts), 1)", 17.0,
                [scalar(RATE, 0.05), scalar("vars.kpi.region", "华南", node="label"), AMOUNTS], decimals=1)
    [fee] = whatif(card, Changes(inputs={RATE: 0.1}, exclude={S1: frozenset({1})}), sources(**TIMED))["metrics"]
    assert (fee["status"], fee["new"]) == ("ok", round(0.1 * (340.0 - 80.0), 1)), fee


# --------------------------------------------------------------------------
# 决定取哪一格的单格照给行段
# --------------------------------------------------------------------------


def test_a_cell_that_picks_the_item_still_offers_its_own_row():
    """区域那一格决定取哪一项费率：它不能改（改它要重新取数），可它自己那一行勾得了——勾掉它，取哪一项不知道，
    确定是排除后无法计算，不是说不准。"""
    card = lone("fee_pick", f"{FEE} * 100", "0.05 * 100", 5.0, [scalar(FEE, 0.05), cell("pull", 1, "region", "华南")],
                decimals=1)
    timed = sources(returned={S1: 10}, finished={"fetch": 5})
    baseline = whatif(card, Changes(), timed)
    inputs = {i["path"]: i for i in baseline["metrics"][0]["inputs"]}
    assert inputs[REGION]["row_span"] == [1, 1] and inputs[REGION]["editable"] is False
    assert inputs[FEE]["editable"] is True and "row_span" not in inputs[FEE]
    assert [(u["input"], u["reason"]) for u in baseline["unsupported"]] == [(REGION, recompute.SELECTOR)]
    [fee] = whatif(card, Changes(exclude={S1: frozenset({1})}), timed)["metrics"]
    assert (fee["status"], fee["changed"]) == ("excluded", True) and "第 2 行" in fee["reason"], fee
    [row] = [i for i in fee["inputs"] if i["path"] == REGION]
    assert row["excluded_rows"] == [1]
