"""助手改节点是合并，不是整体替换；工具绑定变了要说出来。

起因是一次真实运行：助手用 update_node 改写了一个查库的 agent 和一个协作团队。
协议写的是「config 整体替换」，模型只写了要改的提示词、没写 tools，
于是三个要查库的 agent 全部丢了工具——查库的 agent 只能写「假设调用工具」，
协作成员把模型自带的工具调用标记当文字吐了 4 轮，最后一堆原始标记被当成答案
交了出去，数据库一次都没查。

这里守三件事：
1. update_node 只写要改的字段，没写的保留；显式写 null 才删。协作成员按 name
   合并，成员条目里没写 tools 的保留它原来的 tools。
2. 给模型看的协议文案和合并语义一致，不再说「整体替换」。
3. 改完之后工具集合变小、而指令里没让删工具的，自查给 warning，final 里带
   「工具绑定变化」的回执。

示例图全是通用名（db_query__shop、orders），不含任何真实库名。
"""
from __future__ import annotations

import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


QUERY_TOOLS = ["db_query__shop", "db_schema__shop"]

#: 第 1 轮搭好的图：一个查库的 agent，一个带成员的协作团队。工具都绑好了
ROUND_ONE = {
    "nodes": [
        {"id": "start", "type": "input", "position": {"x": 0, "y": 0},
         "data": {"label": "输入", "config": {}}},
        {"id": "query_data", "type": "agent", "position": {"x": 300, "y": 0},
         "data": {"label": "数据查询", "config": {
             "system": "你是取数助手。",
             "prompt": "查 orders 表里每个门店的订单数",
             "tools": list(QUERY_TOOLS),
             "assign_to": "rows",
         }}},
        {"id": "review_team", "type": "supervisor", "position": {"x": 600, "y": 0},
         "data": {"label": "复核团队", "config": {
             "goal": "核对订单数",
             "agents": [
                 {"name": "取数员", "description": "负责查库", "system": "只用 SQL 取数",
                  "tools": ["db_query__shop"]},
                 {"name": "撰稿员", "description": "写结论", "system": "写一段结论",
                  "tools": []},
             ],
             "max_rounds": 4,
         }}},
        {"id": "out", "type": "output", "position": {"x": 900, "y": 0},
         "data": {"label": "成果", "config": {
             "fields": [{"name": "answer", "value": "{{ vars.rows }}"}]}}},
    ],
    "edges": [
        {"source": "start", "target": "query_data"},
        {"source": "query_data", "target": "review_team"},
        {"source": "review_team", "target": "out"},
    ],
}

#: 第 2 轮模型实际发的：两条 update_node，都只写了提示词，没写 tools
ROUND_TWO_OPS = [
    {"op": "plan", "summary": "沿用上一轮的图，按月份拆"},
    {"op": "update_node", "id": "query_data", "label": "数据查询",
     "config": {"system": "你是取数助手，按月份汇总。", "prompt": "按月份统计 orders 的订单数"}},
    {"op": "update_node", "id": "review_team",
     "config": {"goal": "核对按月的订单数",
                "agents": [{"name": "取数员", "system": "按月份取数"},
                           {"name": "撰稿员", "system": "按月写结论"}]}},
    {"op": "done", "explanation": "改成按月份统计", "run": True},
]


class _Scripted:
    """第一轮吐 first；收到自查请求时吐 fix（默认什么都不改）。"""

    def __init__(self, first: list[dict], fix: list[dict] | None = None) -> None:
        self.first = [json.dumps(o, ensure_ascii=False) for o in first]
        self.fix = [json.dumps(o, ensure_ascii=False) for o in (fix or [{"op": "done"}])]
        self.calls: list[str] = []

    async def astream(self, messages):
        from langchain_core.messages import AIMessageChunk

        human = next(text for role, text in messages if role == "human")
        self.calls.append(human)
        for line in (self.fix if "上线前自查" in human else self.first):
            yield AIMessageChunk(content=line + "\n")


async def _stream(client, monkeypatch, model, body: dict) -> list[dict]:
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return model, "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    events: list[dict] = []
    async with client.stream("POST", "/api/copilot/generate-stream", json=body) as r:
        assert r.status_code == 200, await r.aread()
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _node(graph: dict, node_id: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == node_id)


def _config(graph: dict, node_id: str) -> dict:
    return _node(graph, node_id)["data"]["config"]


def _member(graph: dict, node_id: str, name: str) -> dict:
    return next(a for a in _config(graph, node_id)["agents"] if a["name"] == name)


async def _chat_with_round_one(client) -> str:
    r = await client.post("/api/conversations", json={"kind": "chat"})
    conv_id = r.json()["id"]
    r = await client.post(f"/api/conversations/{conv_id}/turns", json={"question": "每个门店多少单"})
    turn_id = r.json()["id"]
    r = await client.patch(f"/api/conversations/{conv_id}/turns/{turn_id}", json={
        "graph": ROUND_ONE, "answer": "一共 12 家店", "status": "done",
    })
    assert r.status_code == 200, r.text
    return conv_id


# --------------------------------------------------------------------------
# 1. 还原那次运行：问数据页的追问，模型只发了不带 tools 的 update_node
# --------------------------------------------------------------------------


async def test_a_follow_up_that_rewrites_prompts_keeps_every_tool(client, monkeypatch):
    conv_id = await _chat_with_round_one(client)
    events = await _stream(client, monkeypatch, _Scripted(ROUND_TWO_OPS), {
        "instruction": "按月份拆一下", "intent": "answer", "conversation_id": conv_id,
    })
    final = events[-1]
    assert final["op"] == "final", final
    graph = final["graph"]

    agent = _config(graph, "query_data")
    assert agent["tools"] == QUERY_TOOLS, "只改提示词不能把工具改没了"
    assert agent["assign_to"] == "rows", "没写的键要保留"
    assert agent["prompt"] == "按月份统计 orders 的订单数", "写了的键要真的改上去"

    team = _config(graph, "review_team")
    assert team["goal"] == "核对按月的订单数"
    assert team["max_rounds"] == 4
    fetcher = _member(graph, "review_team", "取数员")
    assert fetcher["tools"] == ["db_query__shop"], "成员条目没写 tools，要保留成员原来的"
    assert fetcher["system"] == "按月份取数"
    assert fetcher["description"] == "负责查库", "成员没写的字段也保留"
    assert [a["name"] for a in team["agents"]] == ["取数员", "撰稿员"]

    # 工具一个都没变：回执是空的，也不该有「工具被删了」的警告
    assert final["tool_changes"] == []
    assert not [i for i in final["issues"] if i.get("code") == "tools_dropped"]


async def test_the_same_edit_on_the_canvas_keeps_every_tool(client, monkeypatch):
    """画布那条路径（带 base_graph）是同一套合并。"""
    events = await _stream(client, monkeypatch, _Scripted(ROUND_TWO_OPS), {
        "instruction": "按月份拆一下", "base_graph": copy.deepcopy(ROUND_ONE),
    })
    final = events[-1]
    assert final["op"] == "final", final
    assert _config(final["graph"], "query_data")["tools"] == QUERY_TOOLS
    assert _member(final["graph"], "review_team", "取数员")["tools"] == ["db_query__shop"]
    assert final["tool_changes"] == []


async def test_the_streamed_update_op_is_forwarded_as_the_model_wrote_it(client, monkeypatch):
    """转给前端的操作不改写：前端按同一套合并规则自己落到画布上。"""
    events = await _stream(client, monkeypatch, _Scripted(ROUND_TWO_OPS), {
        "instruction": "按月份拆一下", "base_graph": copy.deepcopy(ROUND_ONE),
    })
    ups = [e for e in events if e.get("op") == "update_node"]
    assert [u["id"] for u in ups] == ["query_data", "review_team"]
    assert "tools" not in ups[0]["config"]


# --------------------------------------------------------------------------
# 2. 合并规则本身
# --------------------------------------------------------------------------


def _apply(graph: dict, op: dict) -> dict[str, dict]:
    from app.api.copilot import _apply_op

    nodes = {n["id"]: copy.deepcopy(n) for n in graph["nodes"]}
    edges = copy.deepcopy(graph["edges"])
    assert _apply_op(nodes, edges, op) is True
    return nodes


def test_null_deletes_a_key_and_unmentioned_keys_stay():
    nodes = _apply(ROUND_ONE, {"op": "update_node", "id": "query_data",
                               "config": {"assign_to": None, "temperature": 0.2}})
    cfg = nodes["query_data"]["data"]["config"]
    assert "assign_to" not in cfg
    assert cfg["temperature"] == 0.2
    assert cfg["tools"] == QUERY_TOOLS
    assert cfg["prompt"] == "查 orders 表里每个门店的订单数"


def test_explicit_tools_replace_the_old_list():
    nodes = _apply(ROUND_ONE, {"op": "update_node", "id": "query_data",
                               "config": {"tools": ["db_query__shop"]}})
    assert nodes["query_data"]["data"]["config"]["tools"] == ["db_query__shop"]


def test_members_merge_by_name_and_can_be_added_or_removed():
    nodes = _apply(ROUND_ONE, {"op": "update_node", "id": "review_team", "config": {"agents": [
        {"name": "撰稿员", "remove": True},
        {"name": "核对员", "description": "复核口径", "tools": ["db_schema__shop"]},
        {"name": "取数员", "tools": None},
    ]}})
    agents = nodes["review_team"]["data"]["config"]["agents"]
    assert [a["name"] for a in agents] == ["取数员", "核对员"], "原有成员保持原来的顺序，新成员接在后面"
    assert "tools" not in agents[0], "成员字段写 null 才删"
    assert agents[0]["system"] == "只用 SQL 取数"
    assert agents[1]["tools"] == ["db_schema__shop"]


def test_merging_builds_a_new_config_instead_of_editing_the_old_one():
    """合并出来的 config 是新的：改之前的那份还要拿去比对工具变化。"""
    from app.api.copilot import _apply_op

    base = copy.deepcopy(ROUND_ONE)
    nodes = {n["id"]: n for n in base["nodes"]}
    old = nodes["review_team"]["data"]["config"]
    snapshot = copy.deepcopy(old)
    _apply_op(nodes, [], {"op": "update_node", "id": "review_team",
                          "config": {"goal": "换个目标", "agents": [{"name": "取数员", "tools": []}]}})
    assert old == snapshot, "旧 config（连同成员）被原地改掉了"
    assert nodes["review_team"]["data"]["config"]["agents"][0]["tools"] == []


def test_label_only_updates_leave_the_config_alone():
    nodes = _apply(ROUND_ONE, {"op": "update_node", "id": "query_data", "label": "取数"})
    assert nodes["query_data"]["data"]["label"] == "取数"
    assert nodes["query_data"]["data"]["config"]["tools"] == QUERY_TOOLS


# --------------------------------------------------------------------------
# 3. 协议文案和合并语义一致
# --------------------------------------------------------------------------


def test_the_protocol_no_longer_tells_the_model_to_replace_the_whole_config():
    from app.api.copilot import _STREAM_PROTOCOL, _repair_request

    assert "整体替换" not in _STREAM_PROTOCOL
    assert "只写要改的" in _STREAM_PROTOCOL
    assert "null" in _STREAM_PROTOCOL, "删键的写法要告诉模型"
    assert "name" in _STREAM_PROTOCOL and "remove" in _STREAM_PROTOCOL, "成员按 name 合并、怎么删成员要写清"

    nodes = {n["id"]: copy.deepcopy(n) for n in ROUND_ONE["nodes"]}

    issue = {"level": "error", "node_id": "query_data", "message": "缺了点什么", "field": None}
    text = _repair_request(nodes, list(ROUND_ONE["edges"]), [issue])
    assert "整体替换" not in text and "完整的 config" not in text
    assert "只写要改的" in text


# --------------------------------------------------------------------------
# 4. 工具集合变小了：自查报 warning，回执列出变化
# --------------------------------------------------------------------------


async def test_dropping_tools_without_being_asked_is_called_out(client, monkeypatch):
    ops = [
        {"op": "plan", "summary": "改提示词"},
        {"op": "update_node", "id": "query_data", "config": {"prompt": "简短一点", "tools": []}},
        {"op": "update_node", "id": "review_team",
         "config": {"agents": [{"name": "取数员", "tools": []}]}},
        {"op": "done", "explanation": "改短了"},
    ]
    events = await _stream(client, monkeypatch, _Scripted(ops), {
        "instruction": "提示词写短一点", "base_graph": copy.deepcopy(ROUND_ONE),
    })
    final = events[-1]
    assert final["op"] == "final", final

    changes = {(c["node_id"], c.get("member")): c for c in final["tool_changes"]}
    agent = changes[("query_data", None)]
    assert agent["before"] == QUERY_TOOLS and agent["after"] == []
    assert agent["removed"] == QUERY_TOOLS and agent["added"] == []
    assert agent["label"] == "数据查询"
    member = changes[("review_team", "取数员")]
    assert member["before"] == ["db_query__shop"] and member["after"] == []

    dropped = [i for i in final["issues"] if i.get("code") == "tools_dropped"]
    assert {i["node_id"] for i in dropped} == {"query_data", "review_team"}
    assert all(i["level"] == "warning" for i in dropped)
    text = next(i["message"] for i in dropped if i["node_id"] == "query_data")
    assert "数据查询" in text and "db_query__shop、db_schema__shop" in text and "变成了空" in text
    text = next(i["message"] for i in dropped if i["node_id"] == "review_team")
    assert "取数员" in text and "db_query__shop" in text

    # 自查那一步也要说出来，不能只埋在 final 里
    check = next(e for e in events if e.get("op") == "check" and e["status"] in ("passed", "failed"))
    assert {w["node_id"] for w in check["warnings"]} == {"query_data", "review_team"}


async def test_dropping_tools_on_request_is_listed_but_not_warned(client, monkeypatch):
    ops = [
        {"op": "update_node", "id": "query_data", "config": {"tools": ["db_query__shop"]}},
        {"op": "done", "explanation": "去掉了查表结构"},
    ]
    events = await _stream(client, monkeypatch, _Scripted(ops), {
        "instruction": "数据查询节点去掉查表结构的工具", "base_graph": copy.deepcopy(ROUND_ONE),
    })
    final = events[-1]
    assert [(c["node_id"], c["removed"]) for c in final["tool_changes"]] == [
        ("query_data", ["db_schema__shop"])]
    assert not [i for i in final["issues"] if i.get("code") == "tools_dropped"]


async def test_swapping_one_tool_for_another_is_listed_but_not_warned(client, monkeypatch):
    ops = [
        {"op": "update_node", "id": "query_data",
         "config": {"tools": ["db_query__sales", "db_schema__sales"]}},
        {"op": "done", "explanation": "换到 sales"},
    ]
    events = await _stream(client, monkeypatch, _Scripted(ops), {
        "instruction": "改成查 sales 库", "base_graph": copy.deepcopy(ROUND_ONE),
    })
    final = events[-1]
    change = final["tool_changes"][0]
    assert change["added"] == ["db_query__sales", "db_schema__sales"]
    assert change["removed"] == QUERY_TOOLS
    assert not [i for i in final["issues"] if i.get("code") == "tools_dropped"]


async def test_a_fresh_build_has_no_tool_changes(client, monkeypatch):
    ops = [
        {"op": "add_node", "node": {"id": "start", "type": "input", "label": "输入", "config": {}}},
        {"op": "add_node", "node": {"id": "out", "type": "output", "label": "成果", "config": {}}},
        {"op": "add_edge", "edge": {"source": "start", "target": "out"}},
        {"op": "done", "explanation": "空壳"},
    ]
    events = await _stream(client, monkeypatch, _Scripted(ops), {"instruction": "随便搭一个"})
    assert events[-1]["tool_changes"] == []


async def test_the_one_shot_generate_reports_tool_changes_too(client, monkeypatch):
    """非流式的 /generate 交回的是整张新图：照样比对、照样提醒。"""
    import app.api.copilot as copilot

    rewritten = copy.deepcopy(ROUND_ONE)
    del _config(rewritten, "query_data")["tools"]
    raw = {
        "explanation": "改了提示词",
        "nodes": [{"id": n["id"], "type": n["type"], "label": n["data"]["label"],
                   "config": n["data"]["config"]} for n in rewritten["nodes"]],
        "edges": rewritten["edges"],
    }

    class _OneShot:
        def with_structured_output(self, *_a, **_k):
            return self

        async def ainvoke(self, _messages):
            return raw

    async def _model(*_a, **_k):
        return _OneShot(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)
    r = await client.post("/api/copilot/generate", json={
        "instruction": "提示词写短一点", "base_graph": copy.deepcopy(ROUND_ONE),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(c["node_id"], c["after"]) for c in body["tool_changes"]] == [("query_data", [])]
    assert [i["node_id"] for i in body["issues"] if i.get("code") == "tools_dropped"] == ["query_data"]


# --------------------------------------------------------------------------
# 5. 「指令里让没让删工具」得看删的是不是工具，不能见词就算
# --------------------------------------------------------------------------
#
# 以前只要指令里某处有「不要 / 不用 / 取消」、另一处有「工具」就当成让删了。
# 可出事之后用户最可能的追问恰恰是「这次要真的用工具查库，不要编造数字」——
# 两个词都在，警告被关掉，没工具的图照样自动运行。


def _dropped(instruction: str, *, query: list[str], fetcher: list[str] | None = None) -> set[str]:
    """把数据查询的工具改成 query（取数员改成 fetcher），看哪些节点会报 tools_dropped。"""
    from app.api.copilot import dropped_tool_warnings, tool_changes

    after = copy.deepcopy(ROUND_ONE)
    _config(after, "query_data")["tools"] = query
    if fetcher is not None:
        _member(after, "review_team", "取数员")["tools"] = fetcher
    warnings = dropped_tool_warnings(tool_changes(ROUND_ONE["nodes"], after["nodes"]), instruction)
    assert all(w["code"] == "tools_dropped" and w["level"] == "warning" for w in warnings)
    return {w["node_id"] for w in warnings}


@pytest.mark.parametrize("instruction", [
    "把 system 改成英文，不用改工具",
    "这次要真的用工具查库，不要编造数字",
    "数据查询要真的用工具查库，不要编造数字",
    "取消复核团队，工具保留",
    "别用假设的方式，必须调用工具",
    "别用假设的方式调用工具",
    "不要把工具去掉，提示词短一点",
    "千万别删除工具",
    "工具不用动，按月份拆",
    "去掉提示词里的废话然后让它调用工具",
    "don't remove any tools, just make the prompt shorter",
    "answer without making up numbers, use the tools",
])
def test_asking_to_keep_or_use_tools_still_warns_when_they_vanish(instruction):
    assert _dropped(instruction, query=[], fetcher=[]) == {"query_data", "review_team"}


@pytest.mark.parametrize("instruction", [
    "去掉查表结构的工具",
    "数据查询节点去掉查表结构的工具",
    "不要用 db_schema__shop",
    "把查表结构的工具去掉",
    "db_schema__shop 不要了",
    "查表结构那个工具也删了吧",
    "remove the schema tool",
    "drop db_schema__shop from the query node",
])
def test_asking_to_drop_a_tool_is_not_warned(instruction):
    assert _dropped(instruction, query=["db_query__shop"]) == set()


def test_naming_one_tool_does_not_excuse_losing_the_others():
    """点名删 db_schema__shop，模型却把 db_query__shop 也删了：少掉的那个没人要求。"""
    assert _dropped("去掉 db_schema__shop", query=[]) == {"query_data"}
    assert _dropped("去掉 db_schema__shop", query=["db_query__shop"]) == set()


def test_keeping_only_some_tools_counts_as_asking_to_drop_the_rest():
    assert _dropped("数据查询只保留 db_query__shop", query=["db_query__shop"]) == set()
    # 「只保留」的那个也没了，就不是用户要的
    assert _dropped("数据查询只保留 db_query__shop", query=[]) == {"query_data"}
    assert _dropped("只保留查库的工具", query=["db_query__shop"]) == set()
    assert _dropped("只保留查库的工具", query=[]) == {"query_data"}


def test_deleting_some_other_field_is_not_asking_to_drop_tools():
    assert _dropped("去掉 assign_to", query=[]) == {"query_data"}
    assert _dropped("删除取数员的 description", query=[], fetcher=[]) == {"query_data", "review_team"}


def _resend_everything_without_tools() -> list[dict]:
    """问数据页的追问：模型把整张图用 add_node 重发了一遍，每个节点都没写 tools。"""
    ops: list[dict] = [{"op": "plan", "summary": "重搭一遍"}]
    for n in ROUND_ONE["nodes"]:
        cfg = copy.deepcopy(n["data"]["config"])
        cfg.pop("tools", None)
        for member in cfg.get("agents") or []:
            member.pop("tools", None)
        ops.append({"op": "add_node", "node": {"id": n["id"], "type": n["type"],
                                               "label": n["data"]["label"], "config": cfg}})
    ops += [{"op": "add_edge", "edge": e} for e in ROUND_ONE["edges"]]
    ops.append({"op": "done", "explanation": "按要求重搭", "run": True})
    return ops


async def test_a_chat_follow_up_that_loses_every_tool_is_called_out(client, monkeypatch):
    conv_id = await _chat_with_round_one(client)
    events = await _stream(client, monkeypatch, _Scripted(_resend_everything_without_tools()), {
        "instruction": "这次要真的用工具查库，不要编造数字", "intent": "answer",
        "conversation_id": conv_id,
    })
    final = events[-1]
    assert final["op"] == "final", final
    lost = {(c["node_id"], c["member"]) for c in final["tool_changes"]}
    assert lost == {("query_data", None), ("review_team", "取数员")}

    dropped = [i for i in final["issues"] if i.get("code") == "tools_dropped"]
    assert {i["node_id"] for i in dropped} == {"query_data", "review_team"}
    check = next(e for e in events if e.get("op") == "check" and e["status"] in ("passed", "failed"))
    assert {w["node_id"] for w in check["warnings"]} == {"query_data", "review_team"}
