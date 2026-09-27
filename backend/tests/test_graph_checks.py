"""校验要指到字段上；提示词点名要用的工具，节点得真的绑了它。

field：检查器要把问题落到具体的输入框下面。以前 ValidationIssue 只有 node_id，前端
只能拿消息原文去猜是哪个字段（认 {{ path }}、认几句固定说法），认不出就只挂在
面板顶上。

工具绑定：一次真实的运行里，助手改图时把三个查库 agent 的 tools 丢了，提示词里却
还写着「用 db_query__… 查」。模型拿不到工具，只能写一句「假设调用工具」，再往下
编。这种图在跑之前就能认出来：提示词点名了工具、节点却没绑定，判 error——
Copilot 的自查读的是同一份 error，搭图时就会交回模型去修。
"""
from __future__ import annotations

import pytest

from app.engine.schema import GraphSpec, validate_graph


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def graph(*nodes, edges=None):
    ids = [n["id"] for n in nodes]
    return GraphSpec.model_validate({
        "nodes": list(nodes),
        "edges": edges if edges is not None else
        [{"source": a, "target": b} for a, b in zip(ids, ids[1:])],
    })


def issues_of(spec, node_id=None):
    return [i for i in validate_graph(spec).issues if node_id is None or i.node_id == node_id]


# --------------------------------------------------------------------------
# 提示词点名的工具
# --------------------------------------------------------------------------


def test_a_named_tool_that_is_not_bound_is_an_error():
    spec = graph(
        node("start", "input", fields=[{"name": "question"}]),
        node("query", "agent", model="m", system="你负责取数。",
             prompt="用 db_query__shop 查 orders 表的总数：{{ input.question }}", tools=[]),
        node("out", "output"),
    )
    errors = [i for i in issues_of(spec, "query") if i.level == "error"]
    assert errors, issues_of(spec)
    assert "提示词要求用「db_query__shop」，但节点没有绑定它" in errors[0].message
    assert errors[0].field == "prompt"
    assert not validate_graph(spec).ok


def test_builtin_tool_names_are_recognised_too():
    spec = graph(
        node("start", "input"),
        node("calc", "agent", model="m", system="算完用 python_exec 核对一遍", tools=["calculator"]),
    )
    hit = [i for i in issues_of(spec, "calc") if "python_exec" in i.message]
    assert hit and hit[0].level == "error" and hit[0].field == "system"


def test_a_bound_tool_is_fine():
    spec = graph(
        node("start", "input"),
        node("query", "agent", model="m", prompt="用 db_query__shop 查一下", tools=["db_query__shop"]),
    )
    assert not [i for i in issues_of(spec, "query") if "提示词要求用" in i.message]


def test_saying_not_to_use_a_tool_is_not_asking_for_it():
    spec = graph(
        node("start", "input"),
        node("query", "agent", model="m", prompt="不要用 python_exec，直接心算", tools=[]),
    )
    assert not [i for i in issues_of(spec, "query") if "python_exec" in i.message]


def test_a_vague_request_for_tools_without_any_bound_is_a_warning():
    spec = graph(
        node("start", "input"),
        node("query", "agent", model="m", prompt="调用工具查一下数据库，再回答", tools=[]),
    )
    vague = [i for i in issues_of(spec, "query") if "没有绑定任何工具" in i.message]
    assert vague and vague[0].level == "warning" and vague[0].field == "prompt"
    assert validate_graph(spec).ok


def test_team_members_are_checked_against_their_own_tools():
    spec = graph(
        node("start", "input"),
        node("team", "supervisor", model="m", goal="核对迁移记录", agents=[
            {"name": "核对员", "system": "用 db_query__shop 独立重查一遍条数", "tools": []},
            {"name": "撰稿员", "system": "根据复核结果写结论"},
        ]),
    )
    errors = [i for i in issues_of(spec, "team") if i.level == "error"]
    assert errors and "核对员" in errors[0].message and "db_query__shop" in errors[0].message
    assert errors[0].field == "agents[0].system"


def test_a_team_goal_naming_a_tool_nobody_has():
    spec = graph(
        node("start", "input"),
        node("team", "supervisor", model="m", goal="先用 db_schema__shop 看表结构", agents=[
            {"name": "甲", "system": "你负责查", "tools": ["db_query__shop"]}]),
    )
    errors = [i for i in issues_of(spec, "team") if i.level == "error"]
    assert errors and errors[0].field == "goal", issues_of(spec)


# 误报：这条 error 会挡住运行，还会作为自查的阻断项交回 Copilot（模型可能反过来把
# 禁用的工具绑上去），宁可漏判不可误判


def _errors_about(tool, **config):
    spec = graph(node("start", "input"), node("bot", "agent", model="m", **config))
    return [i for i in issues_of(spec, "bot") if i.level == "error" and tool in i.message]


@pytest.mark.parametrize("prompt", [
    "Never call web_search; answer from the notes only.",
    "Avoid web_fetch.",
    "Do not use web_search.",
    "Don't use the web_search tool, and never call web_fetch or http_request.",
    "You must not call web_search under any circumstances.",
    "You cannot use web_fetch here, and no http_request either.",
])
def test_english_negation_is_not_a_request(prompt):
    for tool in ("web_search", "web_fetch", "http_request"):
        assert not _errors_about(tool, prompt=prompt, tools=[]), (prompt, tool)


@pytest.mark.parametrize("prompt", [
    "查过一次就不再调用 python_exec，直接给结论",
    "表结构已经给你了，无须再调用 db_schema__shop",
    "拿到结果后别再用 db_query__shop",
    "毋须使用 web_search",
])
def test_more_chinese_negations(prompt):
    for tool in ("python_exec", "db_schema__shop", "db_query__shop", "web_search"):
        assert not _errors_about(tool, prompt=prompt, tools=[]), (prompt, tool)


def test_an_mcp_wildcard_covers_its_tools():
    assert not _errors_about("mcp:fs/read_file", prompt="用 mcp:fs/read_file 读 notes.md",
                             tools=["mcp:fs/*"])
    assert not _errors_about("mcp:fs/read_file", prompt="用 mcp:fs/read_file 读 notes.md",
                             tools=["mcp:fs"])
    # 别的 server 的通配不算
    assert _errors_about("mcp:fs/read_file", prompt="用 mcp:fs/read_file 读 notes.md",
                         tools=["mcp:web/*"])


def test_a_team_goal_is_covered_by_a_member_wildcard():
    spec = graph(
        node("start", "input"),
        node("team", "supervisor", model="m", goal="先用 mcp:fs/list_dir 看目录", agents=[
            {"name": "甲", "system": "你负责查", "tools": ["mcp:fs/*"]}]),
    )
    assert not [i for i in issues_of(spec, "team") if i.level == "error"], issues_of(spec)


@pytest.mark.parametrize("prompt", [
    "You may use a calculator if needed.",
    "Work it out like a calculator would, step by step.",
    "如有需要可以用 calculator 核对",
    "必要时用 python_exec 验算一下",
    "You can call db_query__shop when needed.",
])
def test_plain_words_and_optional_mentions_are_only_warnings(prompt):
    spec = graph(node("start", "input"), node("bot", "agent", model="m", prompt=prompt, tools=[]))
    found = issues_of(spec, "bot")
    assert not [i for i in found if i.level == "error"], (prompt, found)


def test_a_plain_word_tool_still_counts_when_clearly_asked_for():
    for prompt in ("用 calculator 算出总价", "Use calculator to add them up.", "call `calculator` twice"):
        errors = _errors_about("calculator", prompt=prompt, tools=[])
        assert errors and errors[0].field == "prompt", prompt
    # 提到了但没明确要求：给一条 warning，不挡运行
    spec = graph(node("start", "input"),
                 node("bot", "agent", model="m", prompt="You may use a calculator if needed.", tools=[]))
    warned = [i for i in issues_of(spec, "bot") if "calculator" in i.message]
    assert warned and warned[0].level == "warning", warned
    assert validate_graph(spec).ok


# --------------------------------------------------------------------------
# field：指到字段
# --------------------------------------------------------------------------


def test_issues_carry_the_field_they_are_about():
    spec = graph(
        node("start", "input", fields=[{"name": "question"}]),
        node("route", "branch", cases=[
            {"key": "a", "condition": "vars.x == 1"},
            {"key": "b", "condition": "vars.y ==="},
        ]),
        node("shape", "transform", mode="expression", expression=""),
        node("say", "llm", model="m", prompt="回答 {{ input.questoin }}"),
        edges=[{"source": "start", "target": "route"},
               {"source": "route", "target": "shape", "sourceHandle": "a"},
               {"source": "shape", "target": "say"}],
    )
    by = {(i.node_id, i.field) for i in validate_graph(spec).issues}
    assert ("route", "cases[1].condition") in by, by
    assert ("route", "cases[1].key") in by or ("route", "cases") in by, by   # b 没有连出去的边
    assert ("shape", "expression") in by, by
    assert ("say", "prompt") in by, by       # 变量拼错


def test_graph_level_issues_have_no_field():
    spec = GraphSpec.model_validate({"nodes": []})
    assert all(i.field is None for i in validate_graph(spec).issues)


# --------------------------------------------------------------------------
# 报错的人话规则只有一份
# --------------------------------------------------------------------------


def test_one_set_of_words_for_both_layers():
    import importlib.util

    import httpx

    from app.core.errors import describe_exception, explain

    for old in ("app.engine.errors", "app.api.errors"):
        assert importlib.util.find_spec(old) is None, f"{old} 不该再有自己的一份"
    req = httpx.Request("GET", "https://x/v1/models")
    denied = httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))
    assert describe_exception(denied) == explain(denied)[0]
    assert describe_exception(TimeoutError()) == explain(TimeoutError())[0]
    # 分叉只在程序自己的毛病上：接口里是后端 bug，节点里多半是参数传错了
    wrong = TypeError("_run() got an unexpected keyword argument 'query'")
    assert "内部" in explain(wrong)[0]
    assert "query" in describe_exception(wrong)


def test_a_list_of_forbidden_tools_is_not_a_request():
    spec = graph(
        node("start", "input"),
        node("team", "supervisor", model="m", goal="只用内部资料，严禁调用 web_search、web_fetch、http_request 等联网工具",
             agents=[{"name": "甲", "system": "禁止使用 web_fetch 和 http_request，只查知识库",
                      "tools": ["kb_search"]}]),
    )
    assert not [i for i in issues_of(spec, "team") if i.level == "error"], issues_of(spec)
