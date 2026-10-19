"""模型把操作写成了工具调用的格式时，照操作读；一个操作都没认出时当场纠正一次；报错照实说。

部署上撞过：画布上让助手搭一张按天出数的图，连着几轮都是「助手本轮未完成：模型没有给出任何修改，也没有说明原因」，
又试了一次才成。用同一个模型重放：模型思考里写着「先输出 look」，正文却是一段 DeepSeek 的 DSML 工具调用
（<｜｜DSML｜｜ invoke name="look"> + need / targets 两个参数），意图和参数都对，只是不是约定的单行 JSON。读流只认 JSON 和
原生工具调用，于是一个操作都没认出、整轮报错；画布上没有自动重来，点重试还是同样的几率。现在：

- 写成文字的工具调用（DSML、<invoke>、<tool_call>、DeepSeek 特殊 token）一次调用写完就按名字读成操作（名字是操作名的才认），
  流式一行行攒、读到 look 照样停下去查看；
- 起草时一次调用读完、一个 plan 以外的操作都没认出：把原话交回去、附一句纠正，请它当场重写一次（每轮一次）；
- 还是没有，报错的说法按诊断照实写，不再一律是「没有给出任何修改，也没有说明原因」；
- 提示词里写明这里没有工具调用接口。

需求、库名、表名都是虚构的。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk

from app.api.copilot_gen import FORMAT_NUDGE_PROMPT, Generation, GraphApplier, StreamDiagnosis, _iter_ops
from app.engine.toolcalls import LeakedCall, has_markup, leaked_markup, take_leaked_calls
from tests.test_copilot_look import START, Turns, _events, _graph_ops, _line, _ops, _tables, client, shop  # noqa: F401

#: 部署上见到的那种写法：竖线两个、DSML 后面带空格，外层包的是 calls，参数带 string 属性
DSML_LOOK = (
    "<｜｜DSML｜｜ calls>\n"
    '<｜｜DSML｜｜ invoke name="look">\n'
    '<｜｜DSML｜｜ parameter name="need" string="false">订单金额相关的表，要看清表名和字段</｜｜DSML｜｜ parameter>\n'
    '<｜｜DSML｜｜ parameter name="targets" string="false">[{"kind": "table", "name": "orders"}]</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>\n"
)
LOOK = {"op": "look", "need": "订单金额相关的表，要看清表名和字段", "targets": [{"kind": "table", "name": "orders"}]}


class _Model:
    def __init__(self, chunks: list[AIMessageChunk]) -> None:
        self.chunks = chunks

    async def astream(self, messages):
        for chunk in self.chunks:
            yield chunk


async def _read(text: str, *, size: int = 0, lines: list[str] | None = None) -> tuple[list[dict], StreamDiagnosis]:
    """把 text 当作模型正文读一遍（size>0 时按这么多字切成小段流过来）。"""
    pieces = [text[i:i + size] for i in range(0, len(text), size)] if size else [text]
    diag = StreamDiagnosis()
    model = _Model([AIMessageChunk(content=p) for p in pieces]
                   + [AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})])
    ops = [op async for op in _iter_ops(model, [], lines=lines, diag=diag)]
    return [op for op in ops if op["op"] != "thinking"], diag


# ---------------------------------------------------------------- 认标记

@pytest.mark.parametrize("text, call", [
    # 部署上见到的 DSML
    (DSML_LOOK, LeakedCall("look", {k: v for k, v in LOOK.items() if k != "op"})),
    # 标准的 DSML：竖线一个、外层是 function_calls；string="true" 原样是字符串（哪怕看着像数字）
    ('<｜DSML｜function_calls>\n<｜DSML｜invoke name="look">\n'
     '<｜DSML｜parameter name="need" string="true">2024 年的订单</｜DSML｜parameter>\n'
     '<｜DSML｜parameter name="limit" string="true">3</｜DSML｜parameter>\n'
     "</｜DSML｜invoke>\n</｜DSML｜function_calls>",
     LeakedCall("look", {"need": "2024 年的订单", "limit": "3"})),
    # 去掉 DSML 前缀的 <invoke>：没写 string 的，像 JSON 就按 JSON 读
    ('<function_calls><invoke name="add_node"><parameter name="node">{"id": "q", "type": "llm"}</parameter>'
     '<parameter name="label">查订单</parameter></invoke></function_calls>',
     LeakedCall("add_node", {"node": {"id": "q", "type": "llm"}, "label": "查订单"})),
    # <tool_call>：arguments 可能是一段 JSON 字符串
    ('<tool_call>{"name": "look", "arguments": "{\\"need\\": \\"订单表\\"}"}</tool_call>',
     LeakedCall("look", {"need": "订单表"})),
    ('<tool_call>{"function": {"name": "done", "arguments": {"explanation": "好了"}}}</tool_call>',
     LeakedCall("done", {"explanation": "好了"})),
    # DeepSeek 特殊 token 的两种排法
    ('<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>look\n```json\n{"need": "订单表"}\n```'
     "<｜tool▁call▁end｜><｜tool▁calls▁end｜>",
     LeakedCall("look", {"need": "订单表"})),
    ('<｜tool▁call▁begin｜>look<｜tool▁sep｜>{"need": "订单表"}<｜tool▁call▁end｜>', LeakedCall("look", {"need": "订单表"})),
])
def test_each_markup_style_gives_its_call(text, call):
    assert has_markup(text)
    assert take_leaked_calls(text) == ([call], "")


def test_an_unfinished_call_is_kept_until_its_end_arrives():
    calls, rest = take_leaked_calls('说明文字 <｜DSML｜function_calls>\n<｜DSML｜invoke name="look">\n')
    assert calls == [] and rest.startswith('<invoke name="look">')
    calls, rest = take_leaked_calls(rest + '<｜DSML｜parameter name="need" string="true">订单</｜DSML｜parameter>\n'
                                    "</｜DSML｜invoke>\n<｜DSML｜invoke name=\"done\">\n")
    assert calls == [LeakedCall("look", {"need": "订单"})] and rest.startswith('<invoke name="done">')
    # 流读完了还差结尾：参数都写完整了的补上结尾再读；参数写到一半、补上也读不出的照样交回（调用方记「没写完」）
    assert take_leaked_calls(rest, final=True) == ([LeakedCall("done", {})], "")
    half = '<invoke name="look">\n<parameter name="need" string="true">没写完'
    assert take_leaked_calls(half, final=True) == ([], half)
    assert take_leaked_calls("<tool_call>{坏的", final=True) == ([], "<tool_call>{坏的")


def test_closing_dsml_tags_and_call_wrappers_count_as_leaked_markup():
    # 节点里的 agent 也用这个认：DSML 的结尾标签、<function_calls> 包装以前漏认
    assert leaked_markup("</｜DSML｜invoke>") and leaked_markup("<function_calls>") and leaked_markup("<tool_calls>")
    assert not has_markup('{"op": "look", "need": "订单"}') and not has_markup("先看一下 orders 表")


# ---------------------------------------------------------------- 读流

@pytest.mark.parametrize("size", [0, 7])
async def test_a_dsml_look_is_read_as_a_look_and_the_stream_stops_there(size):
    lines: list[str] = []
    ops, diag = await _read(DSML_LOOK + _line({"op": "add_node", "node": {"id": "ghost"}}) + "\n", size=size,
                            lines=lines)
    assert ops == [LOOK]
    assert diag.markup == 1 and diag.ops == 1 and diag.dropped == {}
    # 接着写时交回模型的是约定的写法，不是它写的那段标记
    assert lines == [_line(LOOK)]


async def test_markup_and_json_lines_mix():
    text = (_line({"op": "plan", "summary": "先看表"}) + "\n" + DSML_LOOK.replace('name="look"', 'name="update_node"')
            .replace('name="need"', 'name="id"').replace('name="targets"', 'name="config"') + _line({"op": "done"}))
    ops, diag = await _read(text)
    assert [op["op"] for op in ops] == ["plan", "update_node", "done"]
    assert ops[1]["id"] == "订单金额相关的表，要看清表名和字段" and diag.markup == 1 and diag.dropped == {}


async def test_markup_that_cannot_be_read_is_diagnosed():
    text = ('<｜DSML｜function_calls>\n<｜DSML｜invoke name="run_sql">\n'
            '<｜DSML｜parameter name="sql" string="true">SELECT 1</｜DSML｜parameter>\n</｜DSML｜invoke>\n'
            '<｜DSML｜invoke name="look">\n<｜DSML｜parameter name="need" string="true">没写完')
    ops, diag = await _read(text)
    assert ops == [] and diag.markup == 1
    assert diag.dropped == {"工具调用标记的名字不认识": 1, "工具调用标记没写完": 1}, diag.dropped


# ---------------------------------------------------------------- 当场纠正

async def test_a_dsml_look_drives_a_real_look_and_the_graph_lands(shop):
    """部署上那一幕：第一次调用就把 look 写成了 DSML。现在照样去查看、接着写、图落地，模型只调两次。"""
    model = Turns([AIMessageChunk(content=DSML_LOOK)], [START, *_graph_ops(shop.name)])
    nodes: dict = {}
    gen = Generation(model, system="SYS", applier=GraphApplier(nodes, []), looks=await _tables(shop))
    events = [op async for op in gen.draft("REQ")]
    assert len(model.calls) == 2 and not gen.diagnosis.nudged
    assert model.calls[1][2] == ("ai", _line(LOOK))
    look = next(e for e in events if e["op"] == "look")
    assert look["status"] == "ok" and look["objects"][0]["named"] == ["orders"]
    assert set(nodes) == {"start", "q", "out"}


async def test_prose_gets_one_correction_with_its_own_words_handed_back(shop):
    model = Turns([AIMessageChunk(content="我先看一下订单表的结构，\n再决定怎么搭。\n")], [START, *_graph_ops(shop.name)])
    nodes: dict = {}
    gen = Generation(model, system="SYS", applier=GraphApplier(nodes, []), looks=await _tables(shop))
    [op async for op in gen.draft("REQ")]
    assert len(model.calls) == 2 and gen.diagnosis.nudged and set(nodes) == {"start", "q", "out"}
    system, human, ai, nudge = model.calls[1]
    assert human == ("human", "REQ") and ai == ("ai", "我先看一下订单表的结构，\n再决定怎么搭。")
    assert nudge[0] == "human" and nudge[1].startswith("上面这次输出里没有一行能认出的操作：")
    assert "每个操作单独一行" in nudge[1] and nudge[1].endswith("请按输出格式重新给出这一轮的操作。")


async def test_an_empty_continuation_after_a_look_is_corrected_too(shop):
    """真模型重放里撞过：先 look，查看完接着写的那一次交回一大段读不成的东西，整轮照样是空的。look 不算进展，照样纠正。"""
    look = {"op": "look", "need": "订单金额", "targets": [{"kind": "table", "name": "orders"}]}
    model = Turns([look], [AIMessageChunk(content='{"op": "add_node", "node": {"id": "q"}}{"op": "done"\n')],
                  [START, *_graph_ops(shop.name)])
    nodes: dict = {}
    gen = Generation(model, system="SYS", applier=GraphApplier(nodes, []), looks=await _tables(shop))
    [op async for op in gen.draft("REQ")]
    assert len(model.calls) == 3 and gen.diagnosis.nudged and set(nodes) == {"start", "q", "out"}
    *_, result, said, nudge = model.calls[2]
    assert result[1].startswith("查看结果") and said == ("ai", '{"op": "add_node", "node": {"id": "q"}}{"op": "done"')
    assert nudge[1].startswith("上面这次输出里没有一行能认出的操作：")


async def test_only_thinking_gets_the_correction_appended_to_the_request(shop):
    thinking = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先看表。"})
    model = Turns([thinking], [START, *_graph_ops(shop.name)])
    gen = Generation(model, system="SYS", applier=GraphApplier({}, []), looks=await _tables(shop))
    [op async for op in gen.draft("REQ")]
    assert len(model.calls) == 2
    system, human = model.calls[1]
    assert human[0] == "human" and human[1].startswith("REQ\n\n上面这次输出里没有一行能认出的操作：只写了思考")


async def test_the_correction_is_given_once_and_never_after_real_ops(shop):
    # 只交了 plan、又什么都没给：纠正一次，还是这样就收场（调用方报 copilot_empty）
    plan_only = Turns([{"op": "plan", "summary": "打算先看表"}])
    gen = Generation(plan_only, system="SYS", applier=GraphApplier({}, []), looks=await _tables(shop))
    [op async for op in gen.draft("REQ")]
    assert len(plan_only.calls) == 2 and gen.diagnosis.nudged and gen.diagnosis.calls == 2
    assert "没有给出任何操作" in plan_only.calls[1][-1][1]
    assert gen.empty().args[0] == "模型只说了打算，没有给出任何修改"
    # 交过真操作（这里是「无需修改」的 done）不算跑偏，不纠正
    done = Turns([{"op": "done", "explanation": "不用改"}])
    gen = Generation(done, system="SYS", applier=GraphApplier({}, []), looks=await _tables(shop))
    [op async for op in gen.draft("REQ")]
    assert len(done.calls) == 1 and not gen.diagnosis.nudged


async def test_the_canvas_stream_recovers_without_an_error(client, monkeypatch, shop):  # noqa: F811
    model = Turns([AIMessageChunk(content="好的，我来设计。\n")], [START, *_graph_ops(shop.name)])
    events = await _events(client, monkeypatch, model, {"instruction": "订单总金额", "intent": "build",
                                                         "datasource_ids": [shop.id]})
    assert "error" not in _ops(events) and events[-1]["op"] == "final", _ops(events)


# ---------------------------------------------------------------- 报错的说法、提示词

@pytest.mark.parametrize("diag, headline", [
    (StreamDiagnosis(markup=1, text_chars=200), "模型把操作写成了工具调用的格式，没有按约定逐行输出，系统认不出"),
    (StreamDiagnosis(tool_calls=["run_sql"]), "模型把操作写成了工具调用的格式，没有按约定逐行输出，系统认不出"),
    (StreamDiagnosis(thinking_chars=30), "模型只写了思考，没有给出任何操作"),
    (StreamDiagnosis(), "模型什么都没有输出"),
    (StreamDiagnosis(text_chars=40, dropped={"不是完整的 JSON": 2}), "模型的输出格式不对，没有一行能认出的操作"),
    (StreamDiagnosis(text_chars=40, ops=1), "模型只说了打算，没有给出任何修改"),
])
def test_the_headline_says_what_actually_came_back(diag, headline):
    assert diag.headline() == headline


def test_the_prompts_say_there_is_no_tool_call_interface():
    from app.api import copilot, copilot_blueprint
    from app.look.service import GUIDE_HEAD_PROMPT

    assert "这里没有工具调用接口" in GUIDE_HEAD_PROMPT and "DSML" in GUIDE_HEAD_PROMPT
    for protocol in (copilot._STREAM_PROTOCOL, copilot.APP_PROTOCOL_PROMPT, copilot_blueprint.BLUEPRINT_PROTOCOL_PROMPT):
        assert "这里没有工具调用接口" in protocol
    assert "{correction}" in FORMAT_NUDGE_PROMPT
