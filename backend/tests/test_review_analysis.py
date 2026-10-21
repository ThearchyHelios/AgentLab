"""复核认分析能力一轮的信号（engine/review.py，00-总方案 §3.7）：只写说明、不自动重来。

- 几次算不了的分析（analysis_refused）并成一条「有 n 次分析没做成」，点名是哪一项、为什么（叫法取契约的 OP_LABELS，不写工具名）；
- 分析调用写错了（tool.error 带 call_error）：后来同一个分析工具做成了是 Agent 改对了，不报；一直没做成的并成一条
  analysis_call_failed，同样是缺口、不重来——以前它落成 tool_error（严重、可重来），一次写错的分析会让整轮问数据重来；
- 判断词核对重写后仍有的（report_analysis_claims）照报告节点的原话说「报告有 n 处…重写 k 次仍未改掉」；
- 这几种信号都在 NOTE_ONLY 里：全是它们时复核不叫模型，照信号原话写说明。

事件是手写的，名字、数都是虚构的。
"""
from __future__ import annotations

import pytest

from app.core.events import EventType
from app.engine import review as rv
from app.tools.analysis import CALL_ERROR_HEAD, REFUSED_CODE

OK_OUTPUT = {"结果": "周报：销售额 45,678.5元。"}


def ev(etype, **data):
    return {"type": str(etype), "node_id": data.pop("node_id", ""), "data": data}


def started(node_id, label):
    return ev(EventType.NODE_STARTED, node_id=node_id, label=label)


def card_refused(item="wow", op="baseline", reason="历史只有 5 期，至少要 8 期"):
    return ev(EventType.LOG, node_id="card", level="warn", code=REFUSED_CODE, item=item, op=op, reason_code="history_short",
              reason=reason, message=f"「周报分析」的分析项「{item}」没有做成：{reason}")


def agent_refused(tool="analysis_decompose", reason="本期数据只到 2026-10-16，还差 2 天"):
    return ev(EventType.LOG, node_id="bot", level="warn", code=REFUSED_CODE, call_id="c1", tool=tool,
              reason_code="partial_target", reason=reason, message=f"{tool} 没有做成：{reason}")


def call_error(tool="analysis_decompose", message="列「区域」在 Q1 里没有：可用的列是 地区、周、销售额", node_id="bot"):
    text = f"{CALL_ERROR_HEAD}{message}"
    return ev(EventType.TOOL_ERROR, node_id=node_id, tool=tool, call_id="c2", error=text, call_error="unknown_column")


def tool_end(tool="analysis_decompose", node_id="bot"):
    return ev(EventType.TOOL_END, node_id=node_id, tool=tool, call_id="c3")


TITLES = [started("card", "周报分析"), started("bot", "数据查询")]


def test_one_refused_analysis_names_the_item_and_the_reason():
    [signal] = rv.scan([*TITLES, agent_refused()], OK_OUTPUT)
    assert (signal.kind, signal.severity) == (REFUSED_CODE, rv.DEGRADED)
    # 叫法照契约写「变化拆解」，不写工具名
    assert signal.detail == "「数据查询」的变化拆解没有做成：本期数据只到 2026-10-16，还差 2 天"
    assert signal.reason is None
    [signal] = rv.scan([*TITLES, card_refused()], OK_OUTPUT)
    assert signal.detail == "「周报分析」的分析项「wow」（波动基线）没有做成：历史只有 5 期，至少要 8 期"


def test_several_refused_analyses_become_one_count():
    rows = [*TITLES, card_refused(), agent_refused(),
            ev(EventType.LOG, node_id="team", level="warn", code=REFUSED_CODE, agent="取数员", tool="analysis_trend",
               reason="中间缺了 2026-W30", message="取数员 的 analysis_trend 没有做成：中间缺了 2026-W30"),
            started("team", "分析团队"),
            card_refused(item="where", op="decompose", reason="按地区只有 1 段，至少要 2 段才拆得开")]
    [signal] = rv.scan(rows, OK_OUTPUT)
    assert signal.kind == REFUSED_CODE
    assert signal.detail.startswith("有 4 次分析没做成。「周报分析」的分析项「wow」（波动基线）：历史只有 5 期，至少要 8 期；"
                                    "「数据查询」的变化拆解：本期数据只到 2026-10-16，还差 2 天；")
    assert "「分析团队」的成员「取数员」的趋势判断：中间缺了 2026-W30" in signal.detail
    assert signal.detail.endswith("；……") and len(signal.detail) <= 300


def test_old_events_without_the_structured_fields_keep_their_wording():
    plain = ev(EventType.LOG, level="warn", code=REFUSED_CODE, message="「周报分析」的波动基线没有做成：历史只有 5 期")
    assert [s.detail for s in rv.scan([plain], OK_OUTPUT)] == ["「周报分析」的波动基线没有做成：历史只有 5 期"]
    two = rv.scan([plain, ev(EventType.LOG, level="warn", code=REFUSED_CODE, message="趋势判断没有做成：中间缺期")], OK_OUTPUT)
    assert [s.detail for s in two] == ["有 2 次分析没做成。「周报分析」的波动基线没有做成：历史只有 5 期；趋势判断没有做成：中间缺期"]


def test_the_merged_signal_keeps_its_place_among_the_others():
    skipped = ev(EventType.LOG, level="warn", code="metric_comparability_skipped", message="指标「销售额环比」没做成可比性检查")
    claims = ev(EventType.LOG, level="warn", code="report_analysis_claims", message="报告有 1 处分析说法未通过核对")
    kinds = [s.kind for s in rv.scan([*TITLES, claims, agent_refused(), skipped, card_refused()], OK_OUTPUT)]
    assert kinds == ["report_analysis_claims", REFUSED_CODE, "metric_comparability_skipped"]
    kinds = [s.kind for s in rv.scan([*TITLES, call_error(), claims, agent_refused()], OK_OUTPUT)]
    assert kinds == [rv.ANALYSIS_CALL_FAILED, "report_analysis_claims", REFUSED_CODE]


def test_a_call_error_the_agent_fixed_is_not_a_gap():
    """Agent 照交回的说明改了参数、同一个分析工具做成了：不报（以前记成 tool_args_fixed，为一次改对的调用叫一次复核模型）。"""
    assert rv.scan([*TITLES, call_error(), tool_end()], OK_OUTPUT) == []


def test_a_call_error_never_fixed_is_a_note_not_a_retry():
    """一直没改对：那次分析没做成，是缺口不是断裂；不落成 tool_error（严重、可重来）——重来一遍 Agent 照样可能不做这项分析。"""
    [signal] = rv.scan([*TITLES, call_error()], OK_OUTPUT)
    assert (signal.kind, signal.severity) == (rv.ANALYSIS_CALL_FAILED, rv.DEGRADED)
    assert signal.detail == ("「数据查询」的变化拆解的调用写错了、一直没改对，没有做成：列「区域」在 Q1 里没有：可用的列是 地区、周、"
                             "销售额")
    assert not rv.should_retry([signal]) and rv.ANALYSIS_CALL_FAILED in rv.NOTE_ONLY
    exhausted = call_error(tool="analysis_baseline", message="这个 Agent 这次执行最多做 12 次分析，已经用完：用已有的分析结论写，不再新做分析")
    [signal] = rv.scan([*TITLES, call_error(), exhausted], OK_OUTPUT)
    assert signal.detail.startswith("有 2 次分析调用写错了、一直没改对，没有做成。「数据查询」的变化拆解：列「区域」")
    assert "「数据查询」的波动基线：这个 Agent 这次执行最多做 12 次分析，已经用完" in signal.detail


def test_other_failures_of_an_analysis_tool_stay_tool_errors():
    """不是调用写错（没有 call_error：快照取不回来这类）的照旧是工具失败。"""
    broken = ev(EventType.TOOL_ERROR, node_id="bot", tool="analysis_decompose", error="工具执行失败：Q1 的查询快照已不存在")
    assert [(s.kind, s.severity) for s in rv.scan([broken], OK_OUTPUT)] == [("tool_error", rv.BROKEN)]


def test_the_claims_leftover_says_how_many_are_left_after_the_rewrite():
    message = ("报告有 2 处分析说法未通过核对（1 处判断没挂分析结论、1 处原因没有依据：「主要来自」「由于」），已要求模型重写 1 次仍未"
               "改掉：判断要挂对应的分析结论并照判定写，原因要有知识库原话或运行输入作依据。报告照常产出，这几处已标注")
    [signal] = rv.scan([ev(EventType.LOG, node_id="write", level="warn", code="report_analysis_claims", count=2,
                           mode="check", message=message)], OK_OUTPUT)
    assert signal.kind == "report_analysis_claims" and "报告有 2 处分析说法未通过核对" in signal.detail
    assert "重写 1 次仍未改掉" in signal.detail


@pytest.mark.asyncio
async def test_a_run_with_only_analysis_gaps_gets_a_note_without_the_model():
    class Never:
        def with_structured_output(self, *_a, **_k):
            raise AssertionError("不该叫模型")

    signals = rv.scan([*TITLES, agent_refused(), card_refused(), call_error()], OK_OUTPUT)
    result = await rv.review(Never(), question="上周销售额为什么下降", answer="答案", signals=signals, locked=True)
    assert result.verdict == "annotated" and result.retry is False and result.severity == rv.DEGRADED
    assert result.note.startswith("有 2 次分析没做成。") and "「数据查询」的变化拆解的调用写错了" in result.note


def test_every_new_log_code_of_the_analysis_round_is_registered():
    """第 1 波各包新加的分析日志代码都登记了，严重程度是缺口，不在重来的两组里。"""
    from app.engine.nodes.metrics import COMPARABILITY_SKIPPED

    for code in (REFUSED_CODE, COMPARABILITY_SKIPPED, "report_analysis_claims"):
        assert rv.WARN_CODES[code] == (code, rv.DEGRADED), code
        assert code in rv.NOTE_ONLY and code not in rv.RETRYABLE | rv.REBUILD
    assert rv.ANALYSIS_CALL_FAILED not in rv.RETRYABLE | rv.REBUILD
