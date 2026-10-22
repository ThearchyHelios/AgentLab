"""分析结论的契约（app/analysis/contract.py）：设置的校验与合并、台账条目、复算比较。"""
from __future__ import annotations

from app.analysis import contract as c
from app.analysis.testing import fake_result


def test_every_setting_has_a_default_a_range_and_a_label():
    assert set(c.DEFAULTS) == set(c.RANGES) == set(c.LABELS)
    assert c.settings_problem(c.DEFAULTS) is None
    for key, (kind, low, high) in c.RANGES.items():
        assert low <= c.DEFAULTS[key] <= high, key
        assert isinstance(c.DEFAULTS[key], kind), key


def test_settings_problems_are_spoken_with_labels_not_keys():
    assert c.settings_problem({"k_warn": 3.0, "k_alert": 2.5}) == "「明显超出」的倍数要大于「超出常规波动」的倍数"
    assert c.settings_problem({"min_history": 8.5}) == "至少几期历史要填整数"
    assert c.settings_problem({"main_share": True}) == "「主要来源」的占比要填数字"
    assert c.settings_problem({"main_share": 0.95}) == "「主要来源」的占比要在 0.3 到 0.9 之间"
    assert c.settings_problem({"max_history": 8, "min_history": 9}) == "最多用几期历史不能小于至少几期历史"
    # 「主要」要的占比比「集中」的合计还高：一段占 60% 会判成分散，判定语却说「没有哪一段占到 90%」
    assert c.settings_problem({"main_share": 0.9, "concentration": 0.5}) == "「集中」的合计占比不能小于「主要来源」的占比"
    assert c.settings_problem({"main_share": 0.85}) == "「集中」的合计占比不能小于「主要来源」的占比"   # 缺省的 0.8 补齐了比
    assert c.settings_problem({"main_share": 0.8}) is None and c.settings_problem({"concentration": 0.5}) is None
    assert c.settings_problem({"nope": 1}) == "不认识的分析设置：nope"


def test_effective_settings_layers_defaults_snapshot_and_card_overrides():
    out = c.effective_settings({"k_warn": 2.5, "unknown": 1}, {"k_warn": 1.5, "main_share": 0.6})
    assert out["k_warn"] == 1.5 and out["main_share"] == 0.6 and "unknown" not in out
    assert c.effective_settings(None) == c.DEFAULTS


def test_the_ledger_entry_names_its_input_snapshots_once():
    twice = [{"ref": "Q2", "artifact": "a" * 64, "role": "data"},
             {"ref": "Q2.r0.gmv", "artifact": "a" * 64, "role": "headline_target"},
             {"ref": "Q1", "artifact": "b" * 64, "role": "base"}]
    entry = c.ledger_entry(fake_result(inputs=twice), node_id="n1", exec_no=2, artifact="c" * 64, item="drop")
    assert entry == {"kind": "analysis", "node_id": "n1", "exec": 2, "artifact": "c" * 64, "op": "decompose",
                     "mode": "additive", "status": "ok", "verdict": "driver", "inputs": ["a" * 64, "b" * 64],
                     "item": "drop"}


def test_recompute_comparison_ignores_only_the_hypothesis():
    sealed = fake_result()
    assert c.same(sealed, {**sealed, "hypothesis": "别的说法"})
    assert not c.same(sealed, {**sealed, "phrase": "改过的判定语"})


def test_every_op_has_a_method_and_a_tool_name():
    for op in c.OPS:
        modes = c.MODES.get(op, (None,))
        assert all((op, m) in c.METHODS for m in modes), op
        assert c.TOOL_NAMES[op] == f"analysis_{op}"


def test_catalog_entries_hand_over_the_whole_result_only_for_analysis():
    result = fake_result()
    assert c.catalog_result({"kind": "analysis", "result": result}) is result
    assert c.catalog_result({"kind": "query", "result": result}) is None
    assert c.catalog_result({"kind": "analysis"}) is None
    assert c.catalog_result(None) is None
