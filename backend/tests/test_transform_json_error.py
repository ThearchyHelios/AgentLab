"""整形节点按 JSON 解析模型写的文字：报错要说到点子上。

真实踩过的一张图：agent（自由文字）→ transform `{{ nodes.query_logs.text }}` → 口径卡。
agent 在 JSON 字符串里夹了没转义的英文双引号，transform 报「模板渲染出来的不是合法
JSON……字符串值要用 | json 过滤器输出」。模板本身没错，这句提示把人引到了错的方向。

- 出错的位置落在上游模型写的那段文字里：点名上游，说常见原因，指向 output_schema +
  cite_fields；出错位置前后的原文只进技术细节
- 模板自己写错了：照旧是原来那句提示
- 画图时就说破：transform 解析 agent / llm 的文字、产出流向口径卡或按 JSON 解析，给 warning
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run, RunEvent
from app.engine.runner import run_manager
from app.engine.schema import GraphSpec, validate_graph

#: 「他说"好的"」：字符串里的英文引号没转义，json.loads 在「好」那里要逗号
BAD = '{"summary": "他说"好的"然后走了", "count": 3}'
TEMPLATE_HINT = "检查模板里的引号、逗号，字符串值要用 | json 过滤器输出"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def script(monkeypatch, text):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


def weekly(template, *, parse_mode="json"):
    return chain(
        node("start", "input"),
        node("query_logs", "llm", "查询日志", prompt="把结果写成 JSON", assign_to="answer"),
        node("parse", "transform", "解析取数结果为变量", mode=parse_mode, template=template, assign_to="q"),
        node("out", "output", fields=[{"name": "r", "value": "{{ vars.q | json }}"}]),
    )


async def failure(graph, payload=None) -> RunEvent:
    run = await run_manager.start(graph=graph, input_payload=payload or {})
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run.id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            break
    assert row.status == "failed", row.status
    async with SessionLocal() as session:
        [failed] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "node.failed"))).scalars()
    assert failed.node_id == "parse"
    return failed


# --------------------------------------------------------------------------
# 运行时的报错
# --------------------------------------------------------------------------


@pytest.mark.parametrize("template", [
    "{{ nodes.query_logs.text }}",
    "  {{ vars.answer }}\n",
    "{\"wrapped\": {{ nodes.query_logs.text }}}",
])
async def test_unescaped_quotes_in_model_text_blame_the_upstream(monkeypatch, template):
    script(monkeypatch, BAD)
    failed = await failure(weekly(template))
    error, detail = failed.data["error"], failed.data["detail"]

    assert error.startswith("上游「查询日志」输出的不是合法 JSON"), error
    assert "第 17 列附近" in error, error              # 在上游那段文字里的位置，不是在整段模板里的
    assert "没转义的英文引号" in error and "output_schema + cite_fields" in error
    assert TEMPLATE_HINT not in error and "| json" not in error
    # 原文只进技术细节：首行不带模型写的内容
    assert "他说" not in error
    assert detail.startswith("JSONDecodeError: Expecting ',' delimiter")
    assert '"summary": "他说"' in detail and '好的"然后走了' in detail, detail


async def test_context_in_detail_is_thirty_characters_each_side(monkeypatch):
    head, tail = "x" * 50, "y" * 50
    script(monkeypatch, '{"a": "' + head + '"' + tail + '"}')
    failed = await failure(weekly("{{ nodes.query_logs.text }}"))
    near = failed.data["detail"].split("\n", 1)[1]
    assert "x" * 29 + '"' in near and "x" * 31 not in near, near
    assert "y" * 30 in near and "y" * 31 not in near, near


async def test_a_broken_template_keeps_the_template_hint(monkeypatch):
    """模板自己多了个逗号：该查的就是模板，照旧是原来那句提示。"""
    script(monkeypatch, "好的")
    failed = await failure(weekly('{"q": {{ nodes.query_logs.text | json }}, }'))
    error = failed.data["error"]
    assert error.startswith("模板渲染出来的不是合法 JSON（第 1 行第 "), error
    assert error.endswith(TEMPLATE_HINT) and "上游" not in error
    assert failed.data["detail"].startswith("JSONDecodeError: ")


async def test_quoted_model_text_passed_through_the_json_filter_is_not_blamed(monkeypatch):
    """上游的文字过了 | json 就是一个合法的 JSON 字符串：错一定在模板里。"""
    script(monkeypatch, BAD)
    failed = await failure(weekly('{"summary": {{ nodes.query_logs.text | json }} "n": 1}'))
    assert failed.data["error"].startswith("模板渲染出来的不是合法 JSON")


# --------------------------------------------------------------------------
# 模板自己写错了，就不能甩给上游：错的位置落在模型写的那一段里，不等于错在模型
# --------------------------------------------------------------------------

JSON_FIX = "{{ nodes.query_logs.text | json }}"


def template_error(failed: RunEvent) -> str:
    error = failed.data["error"]
    assert error.startswith("模板渲染出来的不是合法 JSON（第 1 行第 "), error
    assert "上游" not in error and "cite_fields" not in error and "output_schema" not in error, error
    assert failed.data["detail"].startswith("JSONDecodeError: ")
    return error


@pytest.mark.parametrize("text", ["GMV is 5", "本周销售额 5 万"])
async def test_prose_dropped_into_a_value_slot_asks_for_the_json_filter(monkeypatch, text):
    """模型本来就只写一句话，模板把它原样放在值的位置：该加的是 | json，模型没做错什么。"""
    script(monkeypatch, text)
    error = template_error(await failure(weekly('{"summary": {{ nodes.query_logs.text }}}')))
    assert "第 13 列附近" in error, error
    assert "模板里 {{ nodes.query_logs.text }} 是模型写的文字" in error and JSON_FIX in error, error
    assert "外面不要再加引号" in error


@pytest.mark.parametrize("template, fix, text", [
    # 前面还有一个带转义引号的字符串：判断「在不在引号里」不能数错
    ('{"note": "a\\"b", "summary": "{{ nodes.query_logs.text }}"}', JSON_FIX, '他说"好的"'),
    ('{"summary": "{{ vars.answer }}", "n": 1}', "{{ vars.answer | json }}", '他说"好的"'),
    # 模型交回的是 JSON，但模板把它当字符串放进了引号：还是模板的事
    ('{"summary": "{{ nodes.query_logs.text }}"}', JSON_FIX, '{"gmv": 1}'),
])
async def test_model_text_inside_template_quotes_asks_for_the_json_filter(monkeypatch, template, fix, text):
    """模板里写的是 "{{ … }}"：模型的文字只要带英文引号就把字符串撑破。上游没错，是模板少了 | json。"""
    script(monkeypatch, text)
    error = template_error(await failure(weekly(template)))
    assert fix in error and "外面不要再加引号" in error, error


@pytest.mark.parametrize("template", [
    '{"x": {{ nodes.query_logs.text }}"b": 2}',       # 紧跟着的逗号漏了
    '{"x": {{ nodes.query_logs.text }}  "b": 2}',     # 隔着空格漏的
    '[1 {{ nodes.query_logs.text }}]',                  # 前面漏了逗号
])
async def test_a_missing_comma_next_to_valid_model_json_keeps_the_template_hint(monkeypatch, template):
    """上游交回的是一个完整、合法的 JSON 对象，解析器停在它前后：缺的是模板里的逗号。"""
    script(monkeypatch, '{"a": 1}')
    error = template_error(await failure(weekly(template)))
    assert error.endswith(TEMPLATE_HINT) and JSON_FIX not in error, error


async def test_a_json_filtered_value_wrapped_in_quotes_says_to_drop_the_quotes(monkeypatch):
    script(monkeypatch, "好的")
    error = template_error(await failure(weekly('{"summary": "{{ nodes.query_logs.text | json }}"}')))
    assert "{{ nodes.query_logs.text | json }} 出来的已经是合法的 JSON（文字自带引号）" in error, error
    assert error.endswith("外面不要再加引号"), error


async def test_an_empty_value_in_a_value_slot_names_the_span(monkeypatch):
    script(monkeypatch, "好的")
    error = template_error(await failure(weekly('{"summary": {{ input.missing }} , "n": 1}')))
    assert "{{ input.missing }} 取出来是空的" in error, error


async def test_plain_text_that_is_not_from_a_model_also_gets_the_json_filter_hint(monkeypatch):
    script(monkeypatch, "好的")
    error = template_error(await failure(weekly('{"q": {{ input.question }}}'), {"question": "上周"}))
    assert "模板里 {{ input.question }} 是一段文字" in error and "{{ input.question | json }}" in error, error


@pytest.mark.parametrize("text, reason", [
    ('{"week": "2026-W37", "rows": [1, 2', "没有收尾"),
    ('```json\n{"gmv": 1}\n```', "代码块"),
    ("好的，本周销售额是 5 万", "一段文字"),
])
async def test_model_text_parsed_as_a_whole_says_what_is_wrong_with_it(monkeypatch, text, reason):
    """整个模板就是上游那段文字：模板不可能写错。原因按实情说，不一律说引号。"""
    script(monkeypatch, text)
    error = (await failure(weekly("{{ nodes.query_logs.text }}"))).data["error"]
    assert error.startswith("上游「查询日志」输出的不是合法 JSON"), error
    assert reason in error and "没转义的英文引号" not in error and "output_schema" in error, error


# --------------------------------------------------------------------------
# 画图时就说破
# --------------------------------------------------------------------------


def warnings_of(graph, node_id="parse"):
    return [i for i in validate_graph(GraphSpec.model_validate(graph)).issues
            if i.node_id == node_id and "cite_fields" in i.message]


CARD = node("card", "metrics", "口径卡", caliber="周报口径", metrics=[{"id": "gmv", "expression": "vars.q.gmv"}])


def agent_graph(template, *, parse_mode="json", after=(CARD,), **agent):
    return chain(
        node("start", "input"),
        node("query_logs", "agent", "查询日志", prompt="查", tools=["db_query__shop"], assign_to="answer",
             **agent),
        node("parse", "transform", "解析", mode=parse_mode, template=template, expression=template,
             assign_to="q"),
        *after,
        node("out", "output", fields=[{"name": "r", "value": "{{ vars.q | json }}"}]),
    )


def test_validate_warns_when_a_transform_parses_agent_text_into_a_caliber():
    [issue] = warnings_of(agent_graph("{{ nodes.query_logs.text }}"))
    assert issue.level == "warning" and issue.field == "template"
    assert "「查询日志」" in issue.message and "output_schema" in issue.message
    # 按 JSON 解析的，不经过口径卡也要说
    assert warnings_of(agent_graph("{{ vars.answer }}", after=()))
    # 表达式模式读文字、流向口径卡
    [expr] = warnings_of(agent_graph("nodes.query_logs.text", parse_mode="expression"))
    assert expr.field == "expression"


def test_validate_stays_quiet_when_nothing_parses_model_text():
    # 过了 | json：是一个合法的 JSON 字符串
    assert not warnings_of(agent_graph('{"note": {{ nodes.query_logs.text | json }}}'))
    # 开了 cite_fields 的 agent：vars 里拿到的是核对过的结构化数据
    schema = {"type": "object", "properties": {"gmv": {"type": "number"}}}
    assert not warnings_of(agent_graph("{{ vars.answer }}", output_schema=schema, cite_fields=True))
    # 模板模式拼一段话、也不进口径卡：没人按 JSON 读它
    assert not warnings_of(agent_graph("结论：{{ nodes.query_logs.text }}", parse_mode="template", after=()))
    # 查询工具交回的是系统拼的 JSON，解析它是正路
    tool = chain(node("start", "input"), node("fetch", "tool", tool="db_query__shop", args={"sql": "SELECT 1"}),
                 node("parse", "transform", mode="json", template="{{ nodes.fetch }}", assign_to="q"), CARD,
                 node("out", "output", fields=[{"name": "r", "value": "{{ vars.q | json }}"}]))
    assert not warnings_of(tool)


def test_validate_points_quoted_model_text_at_the_json_filter():
    """模板把模型的文字放在引号里：画图时也别把人引去 cite_fields，该改的是模板。"""
    graph = agent_graph('{"summary": "{{ nodes.query_logs.text }}", "n": 1}', after=())
    [issue] = [i for i in validate_graph(GraphSpec.model_validate(graph)).issues
               if i.node_id == "parse" and "| json" in i.message]
    assert issue.level == "warning" and issue.field == "template"
    assert "{{ nodes.query_logs.text | json }}" in issue.message and "外面不要再加引号" in issue.message
    assert "cite_fields" not in issue.message and "「查询日志」" in issue.message
    # 值的位置、整段解析：仍然指向结构化输出
    assert warnings_of(agent_graph('{"summary": {{ nodes.query_logs.text }}}', after=()))
