"""运行对比里的分析卡（engine/compare.py 的 analyses）：两次运行的分析卡按节点和项的标识对齐，列出两次的判定和判定语
（最小可用：不拆变化来源）。运行照 tests/fixtures/compare_runs.py 的虚构周报手写，另加分析卡每项一条 analysis.end。
"""
from __future__ import annotations

from app.engine import compare
from tests.fixtures.compare_runs import week


def _with_card(run, items, *, runs=1):
    for _ in range(runs):
        run.add("node.started", "insight", {"node_type": "analysis"})
        for item, verdict, phrase in items:
            run.add("analysis.end", "insight", {"item": item, "op": "baseline", "mode": None, "status": "ok",
                                                "verdict": verdict, "direction": "down", "phrase": phrase,
                                                "analysis_artifact": f"{item}-{verdict}-{phrase}"})
        run.add("node.finished", "insight", {"duration_ms": 1, "attempt": 1})
    return run


def test_compare_aligns_analysis_items():
    base = _with_card(week("r1"), [("wow", "within_band", "在常规波动范围内"), ("trend", "rising", "呈上升趋势"),
                                   ("gone", "no_trend", "没有明显趋势")])
    target = _with_card(week("r2"), [("wow", "outside_band", "超出常规波动范围"), ("trend", "rising", "呈上升趋势"),
                                     ("new", "rising", "呈上升趋势")], runs=2)
    result = compare.compare(base.side(), target.side())
    found = {a["item"]: a for a in result["analyses"]}
    assert [a["item"] for a in result["analyses"]] == ["wow", "trend", "new", "gone"]
    assert found["wow"]["change"] == "verdict" and found["wow"]["base"]["verdict"] == "within_band"
    assert found["wow"]["target"] == {"status": "ok", "verdict": "outside_band", "direction": "down",
                                      "phrase": "超出常规波动范围", "artifact": "wow-outside_band-超出常规波动范围"}
    assert found["trend"]["change"] == "same" and found["new"]["change"] == "added" and found["new"]["base"] is None
    assert found["gone"]["change"] == "removed" and found["gone"]["target"] is None
    assert found["wow"]["title"] == "insight" and found["wow"]["op"] == "baseline"
    assert "分析卡「insight」在这次运行中执行了 2 次，按最后一次对比" in result["notes"]
    # 判定一样、判定语里的数变了
    target = _with_card(week("r3"), [("wow", "within_band", "在常规波动范围内（-3%）")])
    [wow] = compare.compare(_with_card(week("r4"), [("wow", "within_band", "在常规波动范围内")]).side(),
                            target.side())["analyses"]
    assert wow["change"] == "phrase"
    # 没有分析卡的两次运行：analyses 是空的
    assert compare.compare(week("r5").side(), week("r6").side())["analyses"] == []
