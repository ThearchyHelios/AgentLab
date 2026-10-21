"""分析工具的 tool.end 带的那一小段判定（app/tools/analysis.event_view）：时间线照它显示算子、判定语和编号，不从 preview 里拼。

preview 只是结论 JSON 的前一截（截到 2000 个字符），可加拆解、可比性检查的判定语都在截掉的那段里；tool.end 的 analysis 另带
编号、算子、模式、状态、判定码、方向、判定语、算不了的原因码。编号和交回模型的「【分析 An】」是同一个；写错的调用（tool.error）
不带这一段、不占编号。接真算子（不用替身），库是虚构的零售库 shop，模型换成脚本（tests/test_analysis_tools.Script）。
"""
from __future__ import annotations

import json

from langchain_core.messages import ToolMessage

from app.analysis import contract
from app.core import artifact_store
from app.engine.runner import run_manager
from app.tools.analysis import event_view
from tests.test_analysis_tools import (  # noqa: F401 - 夹具按名字引入
    DECOMPOSE, DECOMPOSE_ARGS, SQL, TOOL, Script, agent_graph, engine_up, events, node, run_graph, shop, wait,
)

BASELINE = contract.TOOL_NAMES["baseline"]
COMPARABLE = contract.TOOL_NAMES["comparable"]
WRONG = {**DECOMPOSE_ARGS, "measure": "sales"}
#: 整体的周序列（Q2）：波动基线、两周的可比性读它
TOTALS = "SELECT week, SUM(amount) AS gmv FROM orders GROUP BY week ORDER BY week"
WEEKLY = {"query": "Q2", "period": "week", "measure": "gmv", "label": "销售额"}
TWO_WEEKS = {"query": "Q2", "period": "week", "base": "2026-W41", "target": "2026-W42", "measure": "gmv"}


def _replies(script: Script) -> list[str]:
    return [m.content for m in script.seen[-1] if isinstance(m, ToolMessage)]


def test_event_view_has_the_judgment_without_the_hypothesis():
    result = {"op": "decompose", "mode": "additive", "status": "ok", "reason_code": None, "phrase": "销售额下降 194",
              "verdict": {"code": "driver", "subjects": [["华东"]], "direction": "down"}, "hypothesis": "华东拖累",
              "table": {"columns": ["region"], "rows": [["华东"]]}}
    assert event_view(result, "A2") == {"alias": "A2", "op": "decompose", "mode": "additive", "status": "ok",
                                         "verdict": "driver", "direction": "down", "phrase": "销售额下降 194",
                                         "reason_code": None}
    refused = {"op": "baseline", "mode": None, "status": "refused", "reason_code": "history_short",
               "phrase": "波动基线没有做成：只有 1 期历史，至少要 8 期", "verdict": None}
    assert event_view(refused) == {"alias": None, "op": "baseline", "mode": None, "status": "refused", "verdict": None,
                                   "direction": None, "phrase": "波动基线没有做成：只有 1 期历史，至少要 8 期",
                                   "reason_code": "history_short"}


async def test_tool_end_carries_the_phrase_and_the_same_number_the_model_got(monkeypatch, shop):
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (TOOL, {"sql": TOTALS}), (DECOMPOSE, WRONG),
                                  (DECOMPOSE, DECOMPOSE_ARGS), (BASELINE, WEEKLY), (COMPARABLE, TWO_WEEKS)])
    row = await run_graph(agent_graph(tools=(TOOL, DECOMPOSE, BASELINE, COMPARABLE)))
    ends = [e for e in await events(row.id, "tool.end", "bot") if e.get("analysis_artifact")]
    assert [e["analysis"]["alias"] for e in ends] == ["A1", "A2", "A3"]
    # 交回模型的编号同一套：写错的那次不占编号
    replies = [r.split("\n", 1)[0] for r in _replies(script) if r.startswith("【分析")]
    assert replies == ["【分析 A1】", "【分析 A2】", "【分析 A3】"]
    for end in ends:
        result = artifact_store.load(end["analysis_artifact"])
        assert end["analysis"] == event_view(result, end["analysis"]["alias"])
    decompose, baseline, comparable = (e["analysis"] for e in ends)
    assert (decompose["op"], decompose["mode"], decompose["status"], decompose["verdict"]) == (
        "decompose", "additive", "ok", "driver")
    assert decompose["phrase"].startswith("销售额下降") and decompose["phrase"] not in ends[0]["preview"], \
        "可加拆解的判定语在 preview 截掉的那段里，只能从这一段读"
    assert (baseline["status"], baseline["reason_code"]) == ("refused", "history_short")
    assert baseline["phrase"].startswith(contract.OP_LABELS["baseline"] + "没有做成")
    assert comparable["op"] == "comparable" and comparable["phrase"].startswith("两期")
    # 写错的调用：tool.error 带调用错误的代码，不带判定
    [error] = [e for e in await events(row.id, "tool.error", "bot")]
    assert error["call_error"] == "unknown_column" and "analysis" not in error


async def test_a_team_member_analysis_carries_no_number(monkeypatch, shop):
    Script(monkeypatch, [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS)],
           route={"assignments": [{"agent": "分析员", "instruction": "拆一下销售额的变化"}], "done": False})
    nodes = [node("start", "input"),
             node("team", "supervisor", goal="销售额为什么下降", approval="never", max_rounds=2, agents=[
                 {"name": "分析员", "system": "你负责拆解", "tools": [TOOL, DECOMPOSE], "max_steps": 4}]),
             node("out", "output", fields=[{"name": "a", "value": "{{ nodes.team.text }}"}])]
    run = await run_manager.start(graph={"nodes": nodes, "edges": [{"source": "start", "target": "team"},
                                                                   {"source": "team", "target": "out"}]},
                                  input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    ends = [e for e in await events(row.id, "tool.end", "team") if e.get("analysis_artifact")]
    assert [e["agent"] for e in ends] == ["分析员"]
    end = ends[0]
    # 不带编号：成员自己的分析容器里的 A1 不是报告目录里的 A1（协作成员的结论还进不了台账），报告目录另按整次运行的台账编
    assert end["analysis"]["alias"] is None
    assert (end["analysis"]["op"], end["analysis"]["verdict"]) == ("decompose", "driver")
    assert json.dumps(end["analysis"], ensure_ascii=False).count("下降是不是集中在某个地区") == 0, "Agent 写的假设不带"
