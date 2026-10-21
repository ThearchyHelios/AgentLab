"""助手搭图认分析能力（00-总方案 §3.7）：节点手册教分析卡和分析工具、报告的分析结论引用写法；问数据的包装多一种「分析类
问题」的形状；自查时问题是分析类（app/analysis/intent）、查库的 Agent 却没绑分析工具，交回模型改一轮（只挡一轮，照 coverage
的样板），改过一轮仍在的不挡运行、留作提醒；报告的上游只有分析卡也算有可引用的来源。评测「分析工具开、关」对照的「关」那一臂
（answer_ops 的 analysis_tools、chat/drive.TurnDriver 的同名属性）：工具清单、节点手册、问数据的包装里都没有分析，图里绑了
也按「绑定的工具不存在」挡，别的和开着时一样。

手册里举的引用标记交给真正的解析器，确认写法和系统认的是同一套（同 tests/test_copilot_entity_guide.py）。问法、节点、
表都是虚构的零售场景。
"""
from __future__ import annotations

import json
import re

import pytest
from langchain_core.messages import AIMessageChunk

from app.analysis import contract
from app.api.copilot import (_ASSIST_RULES, ANALYSIS_SHAPE_PROMPT, ANALYSIS_TOOLS_OFF, NODE_REFERENCE, GenerateIn,
                             _evidence_source, _question_of, _tool_catalog, _user_message, node_reference)
from app.api.copilot_gen import ANALYSIS_TOOLS_MISSING, GraphApplier, GraphChecker
from app.engine.evidence import _CELL_REF, _ROW_REF, SUPPORTED_KINDS, parse_markers
from app.engine.schema import ANALYSIS_CLAIMS_VALUES, ANALYSIS_SCOPE_VALUES, GraphNode, NodeType
from tests.test_copilot_answer_ops import _sse, _use, client  # noqa: F401

TOOLS = list(contract.TOOL_NAMES.values())


def _section(start: str, end: str) -> str:
    i = NODE_REFERENCE.index(start)
    return NODE_REFERENCE[i:NODE_REFERENCE.index(end, i)]


def _report() -> str:
    return _section("- report：", "\n- output")


# --------------------------------------------------------------------------
# 节点手册
# --------------------------------------------------------------------------


def test_the_card_entry_teaches_the_analysis_card():
    card = _section("- analysis：分析卡", "\n- dual_check")
    for words in (["items", "overrides", "from", "params", "hypothesis"],               # 配置
                  ["模板里每次都要做的分析", "周报的异动"],                                # 用途
                  ["nodes.<id>.results.<项 id>", "verdict", "branch"],                  # 产出、按判定分支
                  ["tool 节点或 merge", "agent 不能当", "headline", "parent"],           # 输入只认查询节点
                  ["不要把结果交给口径卡", "追不到原始数据"]):                             # D16
        assert all(w in card for w in words), [w for w in words if w not in card]
    assert all(op in card for op in contract.OPS)
    assert all(mode in card for modes in contract.MODES.values() for mode in modes)
    # 例子里的阈值键、判定码都是契约里的
    assert all(key in contract.DEFAULTS for key in re.findall(r"\b(k_warn|main_share|k_alert|flat_pct)\b", card))
    branch = re.search(r"verdict in \[([^\]]+)\]", card).group(1)
    assert {v.strip(" '") for v in branch.split(",")} <= set(contract.VERDICTS["baseline"])


def test_the_agent_entry_names_the_five_analysis_tools():
    agent = _section("- agent：", "\n- supervisor")
    assert all(name in agent for name in TOOLS)
    for words in ("分析方法", "注入", "不要写阈值", "只绑给同时绑了", "db_query__", "不能放进 tool 节点"):
        assert words in agent, words


def test_the_markers_of_analysis_conclusions_parse_as_the_system_reads_them():
    markers = [m for m in parse_markers(_report()) if "A1" in m["body"]]
    kinds = {m["kind"] for m in markers}
    assert {contract.MARKER_KIND, "v", "table", "see"} <= kinds <= SUPPORTED_KINDS
    by_kind = {m["kind"]: m for m in markers}
    plain = [m for m in markers if m["kind"] == contract.MARKER_KIND and m["ref"] == "A1"]
    named = [m for m in markers if m["kind"] == contract.MARKER_KIND and m["ref"] != "A1"]
    assert plain and named and named[0]["ref"] == "A1.top_share" and named[0]["conv"] == "pct"
    cell = _CELL_REF.match(by_kind["v"]["ref"])
    assert cell and cell.group("alias") == "A1" and cell.group("column") == "share"
    assert _ROW_REF.match(by_kind["table"]["ref"]).group("alias") == "A1"
    assert by_kind["see"]["refs"] == ["A1"]
    # 拆解结果表的列 id（判定写法里举的 delta、share）是真结果表的列
    from app.analysis.decompose import _FIXED_ADDITIVE

    cols = by_kind["table"]["options"]["cols"].split(",")
    assert set(cols[1:]) <= set(_FIXED_ADDITIVE), cols


def test_the_report_entry_spells_out_analysis_claims_and_scope():
    report = _report()
    assert "analysis_claims" in report and "analysis_scope" in report
    assert all(v in report for v in ANALYSIS_CLAIMS_VALUES) and all(v in report for v in ANALYSIS_SCOPE_VALUES)
    assert "off < check < strict" in report and "只能往严里改" in report
    for words in ("主要来自", "异常", "持续上升", "待核实的推测", "说明不了为什么"):
        assert words in report, words


def test_governed_templates_and_the_publish_assist_keep_analysis_claims_strict():
    governed = NODE_REFERENCE[NODE_REFERENCE.index("要按受管级别发布的模板"):]
    assert 'analysis_claims: "strict"' in governed[:400]
    rules = " ".join(_ASSIST_RULES.split())
    assert re.search(r"analysis_claims.{0,40}只能往严里改|只能往严里改.{0,80}analysis_claims: strict", rules), rules
    assert "off < check < strict" in rules and "不改回 check" in rules


def test_the_build_rules_bind_analysis_tools_for_analysis_questions():
    rules = NODE_REFERENCE[NODE_REFERENCE.index("搭图规则"):NODE_REFERENCE.index("模型字段")]
    seventh = rules[rules.index("\n7."):]
    assert "分析工具" in seventh and "分析卡" in seventh and "不进口径卡" in seventh


# --------------------------------------------------------------------------
# 问数据的包装
# --------------------------------------------------------------------------


def test_the_answer_request_has_an_analysis_shape():
    text = _user_message(GenerateIn(instruction="上周销售额为什么下降", intent="answer"), patch=False)
    shape = next(line for line in text.splitlines() if line.startswith("- 问的是为什么"))
    assert "input → agent → report → output" in shape
    assert all(name in shape for name in TOOLS) and "db_query__" in shape and "db_schema__" in shape
    assert "按分析结论写" in shape and "判定语" in shape and "不写阈值" in shape
    # 问数是探索运行：不替人打开结论句策略（test_copilot_entity_guide 守着），说法核对用默认、不关
    assert not re.search(r"(?<!analysis_)claims\s*[:=]\s*['\"]?(require_citation|judge)", text)
    assert "analysis_claims " in shape and "不要关" in shape


def test_the_question_for_the_self_check():
    """自查按用户原本的问题判意图：问数据有原问题时用它（重来时 instruction 是带着原因的那段话），没给用 instruction；
    画布上用户说的是流程，不判。"""
    retry = "这个工作流上一次运行没有成功：用满了步数。\n\n请针对这个原因改它，改完继续回答原来的问题：上周销售额多少"
    assert _question_of(GenerateIn(instruction=retry, intent="answer", question="上周销售额多少")) == "上周销售额多少"
    assert _question_of(GenerateIn(instruction="上周销售额为什么下降", intent="answer")) == "上周销售额为什么下降"
    assert _question_of(GenerateIn(instruction="做一个分析销售额为什么下降的流程")) is None


def test_the_chat_drivers_pass_the_original_question():
    from app.chat.drive import TurnDriver

    driver = TurnDriver("上周销售额为什么下降", datasource_ids=[])
    payload = driver.generate_in("这个工作流上一次运行没有成功：…改完继续回答原来的问题：上周销售额为什么下降", None)
    assert payload.question == "上周销售额为什么下降" and payload.intent == "answer"


# --------------------------------------------------------------------------
# 报告的证据来源认分析卡
# --------------------------------------------------------------------------


def test_an_analysis_card_counts_as_an_evidence_source():
    card = GraphNode.model_validate({"id": "check", "type": "analysis", "data": {"label": "异动分析卡", "config": {}}})
    assert card.type == NodeType.ANALYSIS and _evidence_source(card)


# --------------------------------------------------------------------------
# 自查：分析类问题，查库的 Agent 要绑分析工具（只挡一轮）
# --------------------------------------------------------------------------


def _n(nid: str, ntype: str, **config) -> dict:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def _graph(*, tools=("db_query__shop", "db_schema__shop"), card=False):
    nodes = {"start": _n("start", "input"),
             "bot": _n("bot", "agent", prompt="{{ input.question }}", tools=list(tools)),
             "write": _n("write", "report", instructions="回答：{{ input.question }}"),
             "out": _n("out", "output", fields=[{"name": "回答", "value": "{{ nodes.write.text }}"}])}
    if card:
        nodes["check"] = _n("check", "analysis", items=[])
    order = ["start", "bot", "write", "out"]
    return nodes, [{"source": a, "target": b} for a, b in zip(order, order[1:])]


async def test_an_analysis_question_without_analysis_tools_is_returned_once():
    nodes, edges = _graph()
    checker = GraphChecker(GraphApplier(nodes, edges), question="上周销售额为什么下降")
    [issue] = [i for i in await checker.issues() if i["code"] == ANALYSIS_TOOLS_MISSING]
    assert (issue["level"], issue["node_id"], issue["field"]) == ("error", "bot", "tools")
    assert issue["message"].startswith("问题问的是原因（「为什么下降」），查库的「bot」没有绑定分析工具")
    assert all(name in issue["for_model"] for name in TOOLS) and "explanation" in issue["for_model"]
    # 交回模型改过一轮：之后不再挡；收尾时作为提醒
    assert not [i for i in await checker.issues() if i["code"] == ANALYSIS_TOOLS_MISSING]
    [warning] = checker.analysis_gaps(level="warning")
    assert (warning["level"], warning["code"]) == ("warning", ANALYSIS_TOOLS_MISSING)


@pytest.mark.parametrize("question, graph", [
    ("上周销售额为什么下降", _graph(tools=("db_query__shop", "db_schema__shop", "analysis_decompose"))),  # 绑了一个就算
    ("上周销售额为什么下降", _graph(card=True)),                                                    # 有分析卡
    ("上周销售额为什么下降", _graph(tools=("current_time",))),                                     # 没有查库的 Agent
    ("上周销售额是多少", _graph()),                                                               # 不是分析类问题
    (None, _graph()),                                                                           # 画布：没有问题
])
async def test_the_rule_stays_quiet_otherwise(question, graph):
    nodes, edges = graph
    checker = GraphChecker(GraphApplier(nodes, edges), question=question)
    assert not [i for i in await checker.issues() if i["code"] == ANALYSIS_TOOLS_MISSING]
    assert checker.analysis_gaps(level="warning") == []


def _ops(tools: list[str]) -> list[dict]:
    nodes, edges = _graph(tools=tools)
    return [{"op": "plan", "summary": "查销售额"},
            *({"op": "add_node", "node": {"id": n["id"], "type": n["type"], "label": n["id"],
                                          "config": n["data"]["config"]}} for n in nodes.values()),
            *({"op": "add_edge", "edge": e} for e in edges), {"op": "done", "explanation": "查一下", "run": True}]


def _scripted(first: list[dict], fix: list[dict], seen: list[str]):
    class Scripted:
        async def astream(self, messages):
            human = next(text for role, text in messages if role == "human")
            seen.append(human)
            for op in (fix if "上线前自查" in human else first):
                yield AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")

    return Scripted()


async def test_the_stream_asks_for_one_repair_and_the_model_binds_the_tools(client, monkeypatch):  # noqa: F811
    seen: list[str] = []
    fix = [{"op": "update_node", "id": "bot", "config": {"tools": ["db_query__shop", "db_schema__shop", *TOOLS]}},
           {"op": "done", "explanation": "绑上了分析工具"}]
    _use(monkeypatch, _scripted(_ops(["db_query__shop", "db_schema__shop"]), fix, seen))
    got = await _sse(client, {"instruction": "上周销售额为什么下降", "intent": "answer"})
    [repairing] = [op for op in got if op["op"] == "check" and op["status"] == "repairing"]
    assert ANALYSIS_TOOLS_MISSING in [i["code"] for i in repairing["issues"]]
    assert any("上线前自查" in h and "analysis_decompose" in h for h in seen), "交回模型改一轮"
    [final] = [op for op in got if op["op"] == "final"]
    bot = next(n for n in final["graph"]["nodes"] if n["id"] == "bot")
    assert set(TOOLS) <= set(bot["data"]["config"]["tools"])
    assert not [i for i in final["issues"] if i["code"] == ANALYSIS_TOOLS_MISSING]


async def test_a_model_that_keeps_its_graph_still_runs_with_a_warning(client, monkeypatch):  # noqa: F811
    seen: list[str] = []
    keep = [{"op": "done", "explanation": "这个问题其实只要一个数，不用分析"}]
    _use(monkeypatch, _scripted(_ops(["db_query__shop", "db_schema__shop"]), keep, seen))
    got = await _sse(client, {"instruction": "上周销售额为什么下降", "intent": "answer"})
    assert sum("上线前自查" in h for h in seen) == 1, "只挡一轮"
    [final] = [op for op in got if op["op"] == "final"]
    assert final["next"] == "run", "改过一轮仍在的不挡运行"
    assert [i["level"] for i in final["issues"] if i["code"] == ANALYSIS_TOOLS_MISSING] == ["warning"]


async def test_a_retry_reads_the_original_question(client, monkeypatch):  # noqa: F811
    """重来时 instruction 是带着原因的那段话（里面有「原因」两个字）：按原问题判，原问题不是分析类就不提。"""
    seen: list[str] = []
    _use(monkeypatch, _scripted(_ops(["db_query__shop", "db_schema__shop"]), [], seen))
    retry = "这个工作流上一次运行没有成功：用满了步数。\n\n请针对这个原因改它，改完继续回答原来的问题：上周销售额多少"
    got = await _sse(client, {"instruction": retry, "intent": "answer", "question": "上周销售额多少"})
    assert not [op for op in got if op["op"] == "check" and op["status"] == "repairing"]
    [final] = [op for op in got if op["op"] == "final"]
    assert not [i for i in final["issues"] if i["code"] == ANALYSIS_TOOLS_MISSING]


# --------------------------------------------------------------------------
# 关掉分析工具（评测「分析工具开、关」对照的「关」那一臂：answer_ops 的 analysis_tools、TurnDriver 的同名属性）
# --------------------------------------------------------------------------


def test_the_catalog_and_the_manual_leave_analysis_out_when_switched_off():
    assert all(f"- {name}（" in _tool_catalog([]) for name in TOOLS)
    off = _tool_catalog([], analysis_tools=False)
    assert not [line for line in off.splitlines() if line.startswith("- analysis_")]
    assert off == "\n".join(line for line in _tool_catalog([]).splitlines() if not line.startswith("- analysis_"))
    assert node_reference() is NODE_REFERENCE
    manual = node_reference(analysis_tools=False)
    assert not any(name in manual for name in TOOLS)
    # 去掉的是 agent 段里「分析类问题绑分析工具」那一条、分析卡一整条、搭图规则第 7 条；报告段的引用写法、受管规则照留
    assert "- analysis：" not in manual and "\n7. " not in manual and "**问题问的是为什么" not in manual
    # 别的段落一字不少：只去掉那三段
    for kept in ("  **默认不要写 max_steps", "- dual_check：", "\n模型字段", "- report：", "[[a:A1]]", "analysis_claims"):
        assert kept in manual, kept
    request = GenerateIn(instruction="上周销售额为什么下降", intent="answer")
    on, off_text = _user_message(request, patch=False), _user_message(request, patch=False, analysis_tools=False)
    assert ANALYSIS_SHAPE_PROMPT in on and ANALYSIS_SHAPE_PROMPT not in off_text
    assert on.replace(ANALYSIS_SHAPE_PROMPT, "") == off_text


def _recording(first: list[dict], fix: list[dict], seen: list[list[tuple[str, str]]]):
    class Recording:
        async def astream(self, messages):
            seen.append(list(messages))
            human = next(text for role, text in messages if role == "human")
            for op in (fix if "上线前自查" in human else first):
                yield AIMessageChunk(content=json.dumps(op, ensure_ascii=False) + "\n")

    return Recording()


async def _ops_of(question: str, *, analysis_tools: bool) -> list[dict]:
    from app.api.copilot import answer_ops

    return [op async for op in answer_ops(GenerateIn(instruction=question, intent="answer"),
                                          analysis_tools=analysis_tools)
            if op.get("op") not in ("heartbeat", "usage")]


async def test_switched_off_the_copilot_sees_no_analysis_and_bound_tools_are_blocked(monkeypatch):
    seen: list[list[tuple[str, str]]] = []
    fix = [{"op": "update_node", "id": "bot", "config": {"tools": ["db_query__shop", "db_schema__shop"]}},
           {"op": "done", "explanation": "去掉了分析工具"}]
    _use(monkeypatch, _recording(_ops(["db_query__shop", "db_schema__shop", "analysis_decompose"]), fix, seen))
    got = await _ops_of("上周销售额为什么下降", analysis_tools=False)
    system = seen[0][0][1]
    assert seen[0][0][0] == "system" and not any(name in system for name in TOOLS)
    assert ANALYSIS_TOOLS_MISSING not in json.dumps(got, ensure_ascii=False), "问题意图那条自查不提"
    [repairing] = [op for op in got if op["op"] == "check" and op["status"] == "repairing"]
    [issue] = [i for i in repairing["issues"] if i["code"] == ANALYSIS_TOOLS_OFF]
    assert issue["node_id"] == "bot" and issue["message"].startswith("绑定的工具不存在：「bot」绑定的 analysis_decompose")
    [final] = [op for op in got if op["op"] == "final"]
    assert final["next"] == "run" and not [i for i in final["issues"] if i["code"] == ANALYSIS_TOOLS_OFF]

    # 改不好：照实列出、不运行
    seen.clear()
    _use(monkeypatch, _recording(_ops(["db_query__shop", "analysis_decompose"]), [{"op": "done", "explanation": "x"}],
                                 seen))
    got = await _ops_of("上周销售额为什么下降", analysis_tools=False)
    [final] = [op for op in got if op["op"] == "final"]
    assert final["next"] == "fail" and [i["level"] for i in final["issues"] if i["code"] == ANALYSIS_TOOLS_OFF] == ["error"]


async def test_switched_on_bound_analysis_tools_are_fine(monkeypatch):
    """缺省开着：绑了分析工具不挡，和以前一样直接运行。"""
    seen: list[list[tuple[str, str]]] = []
    _use(monkeypatch, _recording(_ops(["db_query__shop", "db_schema__shop", *TOOLS]), [], seen))
    got = await _ops_of("上周销售额为什么下降", analysis_tools=True)
    assert all(name in seen[0][0][1] for name in TOOLS)
    assert not [op for op in got if op["op"] == "check" and op["status"] == "repairing"]
    [final] = [op for op in got if op["op"] == "final"]
    assert final["next"] == "run"


async def test_other_turns_go_the_same_either_way(monkeypatch):
    """没碰分析工具的一轮（不是分析类问题、图里没绑分析工具）：开、关两臂交出的操作逐条一样，差的只是给助手看的那几段。"""
    runs = {}
    for flag in (True, False):
        seen: list[list[tuple[str, str]]] = []
        _use(monkeypatch, _recording(_ops(["db_query__shop", "db_schema__shop"]), [], seen))
        runs[flag] = (await _ops_of("上周销售额是多少", analysis_tools=flag), seen[0])
    assert runs[True][0] == runs[False][0]
    (_, system_on), (_, human_on) = runs[True][1]
    (_, system_off), (_, human_off) = runs[False][1]
    assert human_on.replace(ANALYSIS_SHAPE_PROMPT, "") == human_off and system_on != system_off


async def test_the_turn_driver_passes_the_switch_through(monkeypatch):
    import app.api.copilot as copilot
    from app.chat.drive import TurnDriver

    calls: list[dict] = []

    async def fake_ops(payload, **kwargs):
        calls.append({"question": payload.question, **kwargs})
        yield {"op": "reply", "text": "上一轮已经查过了。"}

    monkeypatch.setattr(copilot, "answer_ops", fake_ops)
    assert TurnDriver("上周销售额为什么下降", datasource_ids=[]).analysis_tools is True
    driver = TurnDriver("上周销售额为什么下降", datasource_ids=[], analysis_tools=False)
    ops, refused = await driver._generate("上周销售额为什么下降", None)
    assert refused is None and ops == [{"op": "reply", "text": "上一轮已经查过了。"}]
    assert calls == [{"question": "上周销售额为什么下降", "catalog_view": None, "analysis_tools": False}]
