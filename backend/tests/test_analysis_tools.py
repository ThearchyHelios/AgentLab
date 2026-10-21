"""agent 分析工具（docs/design/analysis-round/00-总方案.md §3.2）：参数、方法说明、查询容器、台账、编号、下钻、次数上限、
遮罩列、续跑。

算子（app.analysis.run）还没落地：换成按请求交回假结论的替身（tests/fixtures/analysis_ops.FakeOps），断言入口交给算子的
东西、算子交回后入口做的事。模型换成脚本（MockChatModel._decide），不调用任何模型。库是虚构的零售库 shop。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from sqlalchemy import select

from app.analysis import contract
from app.analysis.contract import CallError
from app.core import artifact_store
from app.core.artifact_store import canonical_json, content_hash
from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import Artifact, DataSource, Run, RunEvent
from app.engine.runner import run_manager
from app.tools.analysis import CALL_ERROR_HEAD, REFUSED_CODE
from app.tools.builtin.analysis import ANALYSIS_METHOD_PROMPT, ARGS, MAX_CALLS, METHOD_GUIDE, NO_SCOPE
from tests.fixtures.analysis_ops import FakeOps

TOOL = "db_query__shop"
DECOMPOSE = contract.TOOL_NAMES["decompose"]
BASELINE = contract.TOOL_NAMES["baseline"]
SQL = "SELECT region, week, SUM(amount) AS gmv FROM orders GROUP BY region, week ORDER BY region, week"
MASKED_SQL = "SELECT region, phone AS tel, SUM(amount) AS gmv FROM orders GROUP BY region, phone ORDER BY region"
ROWS = [(1, "华东", "2026-W41", 820.0, "139-0000-0001"), (2, "华东", "2026-W42", 700.0, "139-0000-0001"),
        (3, "华南", "2026-W41", 700.0, "139-0000-0002"), (4, "华南", "2026-W42", 666.0, "139-0000-0002"),
        (5, "华北", "2026-W41", 600.0, "139-0000-0003"), (6, "华北", "2026-W42", 560.0, "139-0000-0003")]
DECOMPOSE_ARGS = {"mode": "additive", "query": "Q1", "by": ["region"], "period": "week", "base": "2026-W41",
                  "target": "2026-W42", "measure": "gmv", "label": "销售额", "hypothesis": "下降是不是集中在某个地区"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def _shop(tmp_path, options=None):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, week TEXT, amount REAL, phone TEXT);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", ROWS)
    db.commit()
    db.close()
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == "shop"))).scalar_one_or_none()
        if row is None:
            row = DataSource(name="shop", kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.options = str(path), "sqlite", True, True, options or {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    return source_id


@pytest.fixture
async def shop(tmp_path):
    source_id = await _shop(tmp_path)
    yield
    await engines.invalidate(source_id)


class Script:
    """Agent（和协作成员）依次发 calls 里的工具调用，再给结论；seen 记下每次带工具的调用看到的消息。"""

    def __init__(self, monkeypatch, calls, *, final="销售额下降，主要来自华东。", route=None):
        from app.providers import mock_model

        self.seen: list[list] = []
        self.plain: list[list] = []
        routed = {"n": 0}

        def decide(model, messages):
            if model.response_format:
                routed["n"] += 1
                if route is not None and routed["n"] == 1:
                    return AIMessage(content=json.dumps(route, ensure_ascii=False))
                return AIMessage(content=json.dumps({"assignments": [], "done": True, "reason": "完成"}))
            if not model.tools:
                self.plain.append(list(messages))
                return AIMessage(content="收尾")
            self.seen.append(list(messages))
            done = sum(isinstance(m, ToolMessage) for m in messages)
            if done < len(calls):
                name, args = calls[done]
                return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{done}"}])
            return AIMessage(content=final)

        monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def agent_graph(tools=(TOOL, DECOMPOSE), approval="never", **cfg):
    nodes = [node("start", "input"),
             node("bot", "agent", prompt="销售额为什么下降", tools=list(tools), approval=approval, **cfg),
             node("out", "output", fields=[{"name": "answer", "value": "{{ nodes.bot.text }}"}])]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def wait(run_id, statuses=("succeeded", "failed")):
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in statuses and (row.status == "interrupted" or row.manifest_seq is not None):
            return row
    raise AssertionError(f"run {run_id} 没有结束：{row.status}")


async def events(run_id, etype=None, node_id=None):
    async with SessionLocal() as session:
        q = select(RunEvent).where(RunEvent.run_id == run_id)
        if etype:
            q = q.where(RunEvent.type == etype)
        if node_id:
            q = q.where(RunEvent.node_id == node_id)
        return [e.data for e in (await session.execute(q.order_by(RunEvent.seq))).scalars()]


async def run_graph(graph, **kw):
    run = await run_manager.start(graph=graph, input_payload={}, **kw)
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    return row


def text(message):
    return message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)


def tool_messages(script):
    return [text(m) for m in script.seen[-1] if isinstance(m, ToolMessage)]


async def ledger(run_id, node_id="bot"):
    [finished] = await events(run_id, "node.finished", node_id)
    return finished["evidence"]


# --------------------------------------------------------------------------
# 工具定义：参数只有「算什么」，五个工具共用一份方法说明
# --------------------------------------------------------------------------


def test_the_five_tools_take_no_thresholds_and_share_one_method():
    from app.capabilities.registry import is_platform_query
    from app.tools.registry import all_specs

    specs = all_specs()
    for op, name in contract.TOOL_NAMES.items():
        spec = specs[name]
        props = spec.args_schema.model_json_schema()["properties"]
        assert not set(props) & set(contract.DEFAULTS), f"{name} 的参数里有阈值：{set(props) & set(contract.DEFAULTS)}"
        assert spec.args_schema is ARGS[op] and spec.category == "分析" and spec.description
        assert spec.guide is METHOD_GUIDE and not spec.dangerous and not spec.writes_state
        # 不查库：能力登记、出处链都不把它的结果当查询快照
        assert not is_platform_query(name)
    assert "query" in ARGS["decompose"].model_json_schema()["properties"]
    assert {"mode", "by", "period", "base", "target", "measure", "numerator", "denominator", "product", "factors",
            "headline", "parent", "label", "hypothesis"} <= set(ARGS["decompose"].model_fields)
    assert {"basis", "grain", "lag"} <= set(ARGS["baseline"].model_fields)
    assert {"reference", "pairs", "mode"} <= set(ARGS["differ"].model_fields)
    assert {"base_query", "target_query"} <= set(ARGS["comparable"].model_fields)


def test_unknown_parameters_and_thresholds_are_rejected_like_any_tool():
    from app.tools.registry import ToolArgsError, prepare_args

    with pytest.raises(ToolArgsError) as found:
        prepare_args(ARGS["decompose"], {**DECOMPOSE_ARGS, "main_share": 0.3})
    assert "main_share 不是该工具的参数" in str(found.value)
    with pytest.raises(ToolArgsError):
        prepare_args(ARGS["decompose"], {**DECOMPOSE_ARGS, "mode": "average"})


def test_chat_users_may_bind_the_analysis_tools():
    from app.auth.chat_policy import CHAT_BUILTIN_TOOLS, CHAT_NODE_TYPES, chat_graph_problem

    assert set(contract.TOOL_NAMES.values()) <= CHAT_BUILTIN_TOOLS and contract.NODE_TYPE in CHAT_NODE_TYPES
    assert chat_graph_problem(agent_graph()) is None


def test_a_tool_node_cannot_call_an_analysis_tool():
    from app.engine.schema import GraphSpec, validate_graph

    graph = {"nodes": [node("start", "input"), node("t", "tool", tool=DECOMPOSE, args=DECOMPOSE_ARGS)],
             "edges": [{"source": "start", "target": "t"}]}
    [issue] = [i for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.code == "analysis.tool_node"]
    assert issue.level == "error" and issue.field == "tool" and "分析卡" in issue.message


async def test_calling_it_outside_an_agent_says_where_it_belongs():
    from app.tools.registry import ToolContext, Versions, call_tool

    with pytest.raises(CallError) as found:
        await call_tool(DECOMPOSE, DECOMPOSE_ARGS, ToolContext(data_versions=Versions.CURRENT))
    assert found.value.message == NO_SCOPE


# --------------------------------------------------------------------------
# 方法说明：绑了分析工具的 Agent 注入一次，模型调用节点、没绑的 Agent 不注入
# --------------------------------------------------------------------------


async def test_the_method_is_injected_once_into_the_agent_system(monkeypatch, shop):
    FakeOps(monkeypatch)
    script = Script(monkeypatch, [(TOOL, {"sql": SQL})])
    await run_graph(agent_graph(tools=(TOOL, DECOMPOSE, BASELINE), system="你是零售分析师。"))
    [system] = [m for m in script.seen[0] if isinstance(m, SystemMessage)]
    assert system.content.startswith("你是零售分析师。") and system.content.count(ANALYSIS_METHOD_PROMPT) == 1

    script = Script(monkeypatch, [(TOOL, {"sql": SQL})])
    await run_graph(agent_graph(tools=(TOOL,), system="你是零售分析师。"))
    [system] = [m for m in script.seen[0] if isinstance(m, SystemMessage)]
    assert system.content == "你是零售分析师。", "没绑分析工具的 Agent，system 和以前一字不差"


async def test_model_call_nodes_get_no_method(monkeypatch):
    from app.providers import mock_model

    seen: list = []

    def decide(model, messages):
        seen.append(list(messages))
        return AIMessage(content="好")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    nodes = [node("start", "input"), node("llm", "llm", system="你是零售分析师。", prompt="说一句"),
             node("out", "output", fields=[{"name": "a", "value": "{{ nodes.llm.text }}"}])]
    await run_graph({"nodes": nodes, "edges": [{"source": "start", "target": "llm"}, {"source": "llm", "target": "out"}]})
    assert ANALYSIS_METHOD_PROMPT not in json.dumps([text(m) for m in seen[0]], ensure_ascii=False)


# --------------------------------------------------------------------------
# 一次分析：查询容器、台账、工件、编号、交回模型的那一段
# --------------------------------------------------------------------------


async def test_an_analysis_reads_this_nodes_query_and_is_recorded(monkeypatch, shop):
    ops = FakeOps(monkeypatch)
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS)])
    row = await run_graph(agent_graph())

    # 算子拿到的：请求只有「算什么」，Q1 解析成本节点那次查询的快照
    [call] = ops.calls
    assert call["request"] == {"op": "decompose", "mode": "additive", "hypothesis": "下降是不是集中在某个地区",
                               "params": {k: v for k, v in DECOMPOSE_ARGS.items() if k not in ("mode", "hypothesis")}}
    ends = await events(row.id, "tool.end", "bot")
    query_artifact = ends[0]["query_artifact"]
    snap = call["snapshots"]["Q1"]
    assert snap["ref"] == "Q1" and snap["artifact"] == query_artifact and snap["node_id"] == "bot"
    assert snap["source"] == "shop" and snap["sql"] == SQL and snap["truncated"] is False
    assert snap["columns"] == ["region", "week", "gmv"] and snap["rows"][0] == ["华东", "2026-W41", 820.0]
    assert snap["column_types"] == ["text", "text", "number"] and snap["masked"] == []
    # 阈值：没有发起时的快照，现读设置（没存过就是缺省）
    assert call["settings"] == contract.DEFAULTS
    assert set(call["context"]) == {"today", "probe", "catalogs", "facts"}
    assert call["context"]["probe"] is None, "还没有 W1-a 的 make_prober 时不给探查"

    # 结论落成工件，tool.end 带着它；台账记一条 kind analysis（带 call_id），在那次查询之后
    analysis_artifact = ends[1]["analysis_artifact"]
    stored = artifact_store.load(analysis_artifact)
    assert stored["op"] == "decompose" and stored["verdict"]["code"] == "driver"
    assert stored["hypothesis"] == "下降是不是集中在某个地区"
    entries = await ledger(row.id)
    assert [e["kind"] for e in entries] == ["tool", "query", "tool", "analysis"]
    analysis = entries[-1]
    assert analysis == contract.ledger_entry(stored, node_id="bot", exec_no=1, artifact=analysis_artifact,
                                             call_id="call_1")
    assert analysis["inputs"] == [query_artifact]
    async with SessionLocal() as session:
        kinds = {r.kind for r in (await session.execute(
            select(Artifact).where(Artifact.id == analysis_artifact))).scalars()}
    assert kinds == {contract.ARTIFACT_KIND}

    # 交回模型的：查询是「【证据 Q1】」，分析是「【分析 A1】」加判定语、关键数、结果表的前几段
    first, second = tool_messages(script)
    assert first.startswith("【证据 Q1】\n")
    head, body = second.split("\n", 1)
    assert head == "【分析 A1】"
    reply = json.loads(body)
    assert reply["verdict"] == "driver" and reply["subjects"] == [["华东"]] and reply["status"] == "ok"
    assert reply["phrase"] == stored["phrase"] and reply["values"]["top_share"] == "0.6186"
    assert reply["table"]["rows"][0][0] == "华东" and "criterion" in reply
    # 调用记录里记着编号和结论工件
    output = artifact_store.load((await events(row.id, "node.finished", "bot"))[0]["artifact"])
    assert [c.get("alias") for c in output["tool_calls"]] == ["Q1", "A1"]
    assert output["tool_calls"][1]["analysis_artifact"] == analysis_artifact


async def test_a_refused_analysis_is_still_a_recorded_conclusion(monkeypatch, shop):
    FakeOps(monkeypatch, statuses=["refused"])
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (BASELINE, {"query": "Q1", "period": "week",
                                                                    "measure": "gmv"})])
    row = await run_graph(agent_graph(tools=(TOOL, BASELINE)))
    entries = await ledger(row.id)
    assert entries[-1]["kind"] == "analysis" and entries[-1]["status"] == "refused" and entries[-1]["verdict"] is None
    reply = json.loads(tool_messages(script)[1].split("\n", 1)[1])
    assert reply["status"] == "refused" and reply["reason_code"] == "history_short" and "8 期" in reply["reason"]
    [warn] = [e for e in await events(row.id, "log", "bot") if e.get("code") == REFUSED_CODE]
    assert warn["level"] == "warn" and warn["reason_code"] == "history_short"


async def test_call_errors_go_back_to_the_model_and_are_not_recorded(monkeypatch, shop):
    ops = FakeOps(monkeypatch, errors=[CallError("masked_column", "列「tel」设置了遮罩，不能当维度"), None])
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS),
                                  (DECOMPOSE, {**DECOMPOSE_ARGS, "by": ["week"]})])
    row = await run_graph(agent_graph())
    assert len(ops.calls) == 2
    _, refused, done = tool_messages(script)
    assert refused == f"{CALL_ERROR_HEAD}列「tel」设置了遮罩，不能当维度"
    assert done.startswith("【分析 A1】"), "写错的那次不占编号"
    [error] = [e for e in await events(row.id, "tool.error", "bot")]
    assert error["call_error"] == "masked_column" and error["error"] == refused
    entries = await ledger(row.id)
    assert [e["call_id"] for e in entries if e["kind"] == "analysis"] == ["call_2"]


async def test_drill_down_reads_this_nodes_earlier_conclusion(monkeypatch, shop):
    ops = FakeOps(monkeypatch)
    drill = {**DECOMPOSE_ARGS, "by": ["week"], "parent": {"of": "A1", "key": ["华东"]}, "hypothesis": "华东内部"}
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS), (DECOMPOSE, drill)])
    row = await run_graph(agent_graph())
    first, second = ops.calls
    # parent 按本节点的编号取回上一层那份结论：就是存下的那一份（按内容寻址，哈希对得上）
    stored = artifact_store.load((await events(row.id, "tool.end", "bot"))[1]["analysis_artifact"])
    assert second["parent"] == stored
    assert content_hash(canonical_json(second["parent"])) == (await events(row.id, "tool.end", "bot"))[1][
        "analysis_artifact"]
    assert second["parents"]("A9") is None
    assert tool_messages(script)[2].startswith("【分析 A2】")


async def test_the_number_of_analyses_per_node_is_capped(monkeypatch, shop):
    ops = FakeOps(monkeypatch)
    # 第二次换一个维度（同样的参数会被护栏当成重复调用、直接交回上次的结果，不走到分析）
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS),
                                  (DECOMPOSE, {**DECOMPOSE_ARGS, "by": ["week"]})])
    await run_graph(agent_graph(), options={contract.SETTINGS_KEY: {"max_calls": 1}})
    assert len(ops.calls) == 1
    capped = tool_messages(script)[2]
    assert capped == CALL_ERROR_HEAD + MAX_CALLS.format(done=1, label=contract.LABELS["max_calls"], limit=1)
    # 阈值来自发起时的快照
    assert ops.calls[0]["settings"]["max_calls"] == 1


async def test_masked_columns_reach_the_operator(monkeypatch, tmp_path):
    source_id = await _shop(tmp_path, {"mask_columns": ["phone"]})
    try:
        ops = FakeOps(monkeypatch)
        Script(monkeypatch, [(TOOL, {"sql": MASKED_SQL}), (DECOMPOSE, {**DECOMPOSE_ARGS, "by": ["tel"]})])
        await run_graph(agent_graph())
        masked = ops.calls[0]["snapshots"]["Q1"]["masked"]
        # 按 SQL 认出改了名字的遮罩列（phone AS tel）排在前面，数据源设的列名也在
        assert masked[0] == "tel" and "phone" in masked
    finally:
        await engines.invalidate(source_id)


async def test_mask_results_runs_hand_over_the_masked_snapshot(monkeypatch, tmp_path):
    source_id = await _shop(tmp_path, {"mask_columns": ["phone"]})
    try:
        ops = FakeOps(monkeypatch)
        Script(monkeypatch, [(TOOL, {"sql": MASKED_SQL}), (DECOMPOSE, {**DECOMPOSE_ARGS, "by": ["region"]})])
        await run_graph(agent_graph(), options={"mask_results": True, "read_only_data": True})
        snap = ops.calls[0]["snapshots"]["Q1"]
        assert "tel" in snap["masked"]
        tel = snap["columns"].index("tel")
        assert {r[tel] for r in snap["rows"]} == {"已遮罩"}, "遮罩运行里算子拿到的是遮过的快照"
    finally:
        await engines.invalidate(source_id)


# --------------------------------------------------------------------------
# 续跑：审批恢复时节点整个重放，分析不再执行、编号不错位
# --------------------------------------------------------------------------


async def test_replay_after_approvals_does_not_repeat_or_renumber(monkeypatch, shop):
    ops = FakeOps(monkeypatch)
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS),
                                  (DECOMPOSE, {**DECOMPOSE_ARGS, "parent": {"of": "A1", "key": ["华东"]}})])
    run = await run_manager.start(graph=agent_graph(approval="always"), input_payload={})
    for _ in range(3):
        row = await wait(run.id, ("interrupted", "failed", "succeeded"))
        assert row.status == "interrupted", row.error
        await run_manager.resume(run.id, {"approved": True})
    row = await wait(run.id, ("succeeded", "failed", "interrupted"))
    assert row.status == "succeeded", row.error

    assert len(ops.calls) == 2, "分析在重放时又执行了一次"
    ends = await events(row.id, "tool.end", "bot")
    assert len(ends) == 3
    # 重放之后模型看到的编号还是同一套，下钻取到的是同一份结论
    assert [m.split("\n", 1)[0] for m in tool_messages(script)] == ["【证据 Q1】", "【分析 A1】", "【分析 A2】"]
    assert ops.calls[1]["parent"] == artifact_store.load(ends[1]["analysis_artifact"])
    entries = [e for e in await ledger(row.id) if e["kind"] == "analysis"]
    assert [(e["call_id"], e["artifact"]) for e in entries] == [("call_1", ends[1]["analysis_artifact"]),
                                                                 ("call_2", ends[2]["analysis_artifact"])]


# --------------------------------------------------------------------------
# 协作成员：同样注入方法说明、同样编号（协作节点不记证据台账）
# --------------------------------------------------------------------------


async def test_a_team_member_gets_the_method_and_numbers(monkeypatch, shop):
    ops = FakeOps(monkeypatch)
    script = Script(monkeypatch, [(TOOL, {"sql": SQL}), (DECOMPOSE, DECOMPOSE_ARGS)],
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
    [system] = [m for m in script.seen[0] if isinstance(m, SystemMessage)]
    assert system.content.startswith("你负责拆解") and system.content.count(ANALYSIS_METHOD_PROMPT) == 1
    assert [m.split("\n", 1)[0] for m in tool_messages(script)] == ["【证据 Q1】", "【分析 A1】"]
    assert ops.calls[0]["snapshots"]["Q1"]["node_id"] == "team"
    [end] = [e for e in await events(row.id, "tool.end", "team") if e.get("analysis_artifact")]
    assert artifact_store.load(end["analysis_artifact"])["op"] == "decompose"
