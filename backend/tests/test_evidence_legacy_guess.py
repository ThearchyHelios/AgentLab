"""旧运行的「猜测来源」：没有契约、没有报告文档的答案，按数值去已封存的证据里找可能的来源。

这不是证据：一期核对真实反例时，168 个数里有 131 个能在快照里找到相同的值，大量是 0、1、2
这种巧合。所以它只做降级展示，而且规矩很死：

- 只从调用方传进来的已封存条目里找（查询快照的单元格、口径卡的指标值），自己不读库——
  人为插进工件表的行进不了这个函数
- 每个数字最多 3 个候选，按接近程度排，距离相同的按传入顺序；同样的输入永远同样的输出
- 片段化返回，有候选的数字 state 记 candidate，一个都没有的记 none
"""
from __future__ import annotations

import copy

import pytest

from app.engine.evidence import (
    GUESS_NOTE,
    GUESS_SCHEMA,
    MAX_CANDIDATES,
    cell_eid,
    guess_sources,
    make_eid,
)

Q_ART, M_ART, OTHER = "a" * 64, "d" * 64, "e" * 64
METRICS = {"kind": "metric_set", "artifact": M_ART, "node_id": "caliber", "content": {
    "caliber": "周报口径", "caliber_version": "v2", "metrics": [
        {"id": "gmv", "name": "销售额", "value": 45678.5, "unit": "元", "decimals": 1},
        {"id": "wow", "name": "环比", "value": 8.7, "unit": "%", "decimals": 1, "format": "plain"},
        {"id": "gone", "name": "缺值", "value": None, "unit": ""}]}}
QUERY = {"kind": "query", "artifact": Q_ART, "alias": "Q1", "node_id": "fetch", "tool": "db_query__warehouse",
         "content": {
    "columns": ["region", "gmv", "rate", "code"],
    "rows": [["east", 45678.5, 0.087, "00123"], ["west", "12004.0", 0.02, "7"], ["north", 45678.2, None, "x"]],
    "column_types": {"region": "text", "gmv": "number", "rate": "number", "code": "text"}}}
TEXT = "本周销售额 45,678.5 元，环比 8.7%，西区 12,004，比率 2%，另有 999 单，编号 123，日期 2026-09-01。"


def numbers(result):
    return [s for s in result["segments"] if s["kind"] == "number"]


def refs(seg):
    return [c.get("ref") for c in seg.get("candidates") or []]


def test_segments_tile_the_text_and_carry_candidates():
    result = guess_sources(TEXT, [METRICS, QUERY])
    assert result["schema"] == GUESS_SCHEMA and result["mode"] == "legacy_text" and result["note"] == GUESS_NOTE
    assert "猜测" in GUESS_NOTE and "不能当证据" in GUESS_NOTE
    assert "".join(s["text"] for s in result["segments"]) == TEXT
    assert [s["id"] for s in result["segments"]] == [f"s{i}" for i in range(len(result["segments"]))]
    for s in result["segments"]:
        assert TEXT[s["span"][0]:s["span"][1]] == s["text"]
    found = {s["text"]: (s["state"], refs(s)) for s in numbers(result)}
    assert found == {
        "45,678.5": ("candidate", ["m:gmv", "Q1.r0.gmv"]),
        "8.7%": ("candidate", ["m:wow", "Q1.r0.rate"]),
        "12,004": ("candidate", ["Q1.r1.gmv"]),
        "2%": ("candidate", ["Q1.r1.rate"]),
        "999": ("none", []),
        "123": ("none", []),                 # 文本列里的 "00123" 是编号，不按数比
    }
    assert result["stats"] == {"numbers": 6, "guessed": 4, "unguessed": 2, "candidates": 6}
    assert all(s["state"] == "neutral" for s in result["segments"] if s["kind"] == "text")


def test_candidate_shapes():
    result = guess_sources("销售额 45,678.5", [METRICS, QUERY])
    metric, cell = numbers(result)[0]["candidates"]
    assert metric == {"kind": "metric", "ref": "m:gmv", "artifact": M_ART, "locator": {"metric": "gmv"},
                      "eid": make_eid("metric", M_ART, {"metric": "gmv"}), "value": 45678.5,
                      "rendered": "45,678.5元", "name": "销售额", "caliber": "周报口径", "version": "v2",
                      "node_id": "caliber", "diff": 0.0}
    assert cell == {"kind": "cell", "ref": "Q1.r0.gmv", "alias": "Q1", "artifact": Q_ART,
                    "locator": {"row": 0, "column": "gmv"}, "eid": cell_eid(Q_ART, 0, "gmv"), "value": 45678.5,
                    "rendered": "45,678.5", "node_id": "fetch", "tool": "db_query__warehouse", "diff": 0.0}


def test_closer_values_come_first_and_ties_keep_the_given_order():
    result = guess_sources("约 45,678", [QUERY, METRICS])
    [seg] = numbers(result)
    # 写的是整数，容差 0.5：45678.2 比 45678.5 更近；两个 45678.5 按传入顺序（查询在前）
    assert refs(seg) == ["Q1.r2.gmv", "Q1.r0.gmv", "m:gmv"]
    assert [c["diff"] for c in seg["candidates"]] == pytest.approx([0.2, 0.5, 0.5])


def test_at_most_three_candidates_per_number():
    zeros = {"kind": "query", "artifact": OTHER, "alias": "Q2", "content": {
        "columns": ["a", "b"], "rows": [[0, 0], [0, 0], [0, 0]]}}
    [seg] = numbers(guess_sources("退款 0 单", [zeros]))
    assert MAX_CANDIDATES == 3 and len(seg["candidates"]) == 3
    assert refs(seg) == ["Q2.r0.a", "Q2.r0.b", "Q2.r1.a"]
    [seg] = numbers(guess_sources("退款 0 单", [zeros], limit=1))
    assert refs(seg) == ["Q2.r0.a"]


def test_only_the_entries_passed_in_are_searched():
    """函数自己不读库：一份没传进来的快照里就算有这个值，也不会成为候选。"""
    result = guess_sources(TEXT, [QUERY])
    assert refs(numbers(result)[0]) == ["Q1.r0.gmv"]
    assert all(not r.startswith("m:") for s in numbers(result) for r in refs(s))
    # 不认识的种类、坏掉的内容一律跳过，不崩
    junk = [{"kind": "tool", "artifact": OTHER, "content": {"columns": ["x"], "rows": [[45678.5]]}},
            {"kind": "retrieval", "artifact": OTHER, "content": {"hits": [{"content": "45678.5"}]}},
            {"kind": "query", "artifact": OTHER, "content": None},
            {"kind": "query", "artifact": OTHER, "content": {"columns": ["x"], "rows": "oops"}},
            {"kind": "metric_set", "content": {"metrics": "oops"}},
            "not a dict"]
    result = guess_sources(TEXT, junk)
    assert all(s["state"] == "none" for s in numbers(result))


def test_same_input_same_output_and_inputs_untouched():
    entries = [METRICS, QUERY]
    before = copy.deepcopy(entries)
    first = guess_sources(TEXT, entries)
    assert guess_sources(TEXT, entries) == first
    assert entries == before


def test_text_without_numbers_and_empty_text():
    assert guess_sources("", [QUERY])["segments"] == []
    result = guess_sources("没有数字，只有 v2 这种标识符。", [QUERY])
    assert numbers(result) == [] and result["stats"]["numbers"] == 0


# --------------------------------------------------------------------------
# 遮罩的列：原始值一律不露，候选里也不行
# --------------------------------------------------------------------------

SALARY = {"kind": "query", "artifact": OTHER, "alias": "Q2", "content": {
    "columns": ["region", "salary", "headcount"],
    "rows": [["east", 8123.45, 12], ["west", 9001.0, 8]],
    "column_types": {"region": "text", "salary": "number", "headcount": "number"},
    "mask_columns": ["salary"]}}


@pytest.mark.parametrize("text", ["东区人均 8,123.45 元", "东区人均 8,123 元", "西区人均 9,001 元"])
def test_columns_masked_at_query_time_are_never_candidates(text):
    """快照记下的遮罩（查询当时数据源的 mask_columns）：按容差对得上也不给候选——候选的 value、
    rendered、diff 都会把原值带出去，写成 8,123 的也会露出 8,123.45。"""
    result = guess_sources(text, [SALARY])
    [seg] = numbers(result)
    assert seg["state"] == "none" and "candidates" not in seg
    assert result["stats"]["candidates"] == 0
    # 没遮的列照样能猜
    [seg] = numbers(guess_sources("东区 12 人", [SALARY]))
    assert refs(seg) == ["Q2.r0.headcount"]


def test_columns_masked_now_are_never_candidates():
    """数据源现在设的遮罩（调用方按 artifact 交进来，不分大小写）：事后加的遮罩同样生效。"""
    [seg] = numbers(guess_sources("销售额 45,678.5", [QUERY], masked={Q_ART: ["GMV"]}))
    assert refs(seg) == []
    [seg] = numbers(guess_sources("销售额 45,678.5", [QUERY, METRICS], masked={Q_ART: ["gmv"]}))
    assert refs(seg) == ["m:gmv"]                       # 口径卡的指标是公开口径，不受列遮罩影响
    [seg] = numbers(guess_sources("比率 2%", [QUERY], masked={OTHER: ["rate"]}))
    assert refs(seg) == ["Q1.r1.rate"]                  # 遮罩只管它那一份快照
    [seg] = numbers(guess_sources("比率 2%", [QUERY], masked={Q_ART: "code, Rate"}))
    assert refs(seg) == []                              # 写成「a, b」的遮罩也认：宁可多遮
    [seg] = numbers(guess_sources("比率 2%", [QUERY], masked="rate"))
    assert refs(seg) == ["Q1.r1.rate"]                  # 形状不对的整个参数不当真，也不崩


def test_a_value_that_cannot_be_rendered_is_skipped_for_the_next_one():
    """渲染不出来的格（非零却显示成 0）不当候选，名额让给下一个：和逐格先渲染的结果一样。"""
    tiny = {"kind": "query", "artifact": OTHER, "alias": "Q2", "content": {
        "columns": ["a"], "rows": [[1e-12], [0.3], [0.2], [0.4]]}}
    # 写的是整数 0，容差 0.5：最近的 1e-12 显示不出来，第四近的 0.4 顶上
    [seg] = numbers(guess_sources("退款 0 单", [tiny]))
    assert refs(seg) == ["Q2.r2.a", "Q2.r1.a", "Q2.r3.a"]
    assert all(c["rendered"] != "—" for c in seg["candidates"])


def test_big_snapshots_are_searched_quickly():
    import time

    cols = [f"c{i}" for i in range(12)]
    rows = [[(r * 12 + c) * 1.25 for c in range(12)] for r in range(5000)]
    sealed = [{"kind": "query", "artifact": f"{i:064x}", "alias": f"Q{i + 1}",
               "content": {"columns": cols, "rows": rows}} for i in range(4)]
    started = time.perf_counter()
    result = guess_sources("数 " + "，".join(str(v * 1.25) for v in range(0, 60000, 997)), sealed)
    assert time.perf_counter() - started < 1.0
    assert result["stats"]["guessed"] == result["stats"]["numbers"] > 0
