"""会话：持久化、列表，以及上下文真的进了 prompt。

起因是三条 run（d983c4e6 / 14103db5 / 08c3de1c）在用户看来是一次对话，
系统里却毫无关联：每轮 base_graph=None 从零建图，每轮一条独立的 thread_id，
而前端的 turns 只活在内存里——刷新页面就全没了。

这里最要紧的不是 CRUD，是最后那组：**上下文确实到了模型手上**。拼字符串的
函数单独测通过、但没接到 messages 里，是这类功能最典型的失败方式，而且界面
上完全看不出来——它只是"答得不太对"。
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.conversations import HISTORY_TURNS, history_text, title_from
from app.db.base import SessionLocal
from app.db.models import Conversation, ConversationTurn
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _conversation(client, **kw) -> dict:
    r = await client.post("/api/conversations", json={"kind": "chat", **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def _turn(client, conv_id: str, question: str, **patch) -> dict:
    r = await client.post(f"/api/conversations/{conv_id}/turns", json={"question": question})
    assert r.status_code == 201, r.text
    turn = r.json()
    if patch:
        r = await client.patch(f"/api/conversations/{conv_id}/turns/{turn['id']}", json=patch)
        assert r.status_code == 200, r.text
        turn = r.json()
    return turn


# --------------------------------------------------------------------------
# 会话本身
# --------------------------------------------------------------------------


async def test_a_conversation_survives_and_lists(client) -> None:
    conv = await _conversation(client)
    await _turn(client, conv["id"], "cobook 里有几家店", answer="12 家", status="done")

    # 列表里认得出是哪次聊天
    rows = (await client.get("/api/conversations")).json()
    mine = next(r for r in rows if r["id"] == conv["id"])
    assert mine["turn_count"] == 1
    assert mine["last_question"] == "cobook 里有几家店"

    # 完整取回：这是刷新页面后重建界面的唯一依据
    detail = (await client.get(f"/api/conversations/{conv['id']}")).json()
    assert [t["question"] for t in detail["turns"]] == ["cobook 里有几家店"]
    assert detail["turns"][0]["answer"] == "12 家"


async def test_title_comes_from_the_first_question(client) -> None:
    """一排"新对话"等于没有列表。"""
    conv = await _conversation(client)
    assert conv["title"] == "新对话"
    await _turn(client, conv["id"], "cobook 里有几家店")
    assert (await client.get(f"/api/conversations/{conv['id']}")).json()["title"] == "cobook 里有几家店"

    # 第二轮不该再改标题
    await _turn(client, conv["id"], "再按月份拆一下")
    assert (await client.get(f"/api/conversations/{conv['id']}")).json()["title"] == "cobook 里有几家店"


async def test_a_renamed_conversation_keeps_its_name(client) -> None:
    conv = await _conversation(client)
    await client.patch(f"/api/conversations/{conv['id']}", json={"title": "门店盘点"})
    await _turn(client, conv["id"], "cobook 里有几家店")
    assert (await client.get(f"/api/conversations/{conv['id']}")).json()["title"] == "门店盘点"


def test_long_titles_are_cut_not_wrapped() -> None:
    assert title_from("短问题") == "短问题"
    assert title_from("") == "新对话"
    long = title_from("请帮我统计一下过去十二个月每个门店每个月的订单总额并按同比排序")
    assert len(long) <= 25 and long.endswith("…")


async def test_deleting_a_conversation_takes_its_turns(client) -> None:
    conv = await _conversation(client)
    turn = await _turn(client, conv["id"], "问点什么")
    assert (await client.delete(f"/api/conversations/{conv['id']}")).status_code == 204
    assert (await client.get(f"/api/conversations/{conv['id']}")).status_code == 404
    async with SessionLocal() as session:
        assert await session.get(ConversationTurn, turn["id"]) is None


async def test_archived_conversations_leave_the_list(client) -> None:
    conv = await _conversation(client)
    await client.patch(f"/api/conversations/{conv['id']}", json={"archived": True})
    assert conv["id"] not in {r["id"] for r in (await client.get("/api/conversations")).json()}
    rows = (await client.get("/api/conversations?include_archived=true")).json()
    assert conv["id"] in {r["id"] for r in rows}


async def test_canvas_conversations_stay_out_of_the_chat_list(client) -> None:
    """画布的对话依附工作流，和问数据页不是一回事，混在一起反而难找。"""
    canvas = await _conversation(client, kind="canvas")
    assert canvas["id"] not in {r["id"] for r in (await client.get("/api/conversations")).json()}
    rows = (await client.get("/api/conversations?kind=canvas")).json()
    assert canvas["id"] in {r["id"] for r in rows}


async def test_a_failed_turn_is_still_recorded(client) -> None:
    """只记成功的轮次，历史就成了一份美化过的记录——而人回来最想搞清楚的
    恰恰是上次卡在哪。"""
    conv = await _conversation(client)
    await _turn(client, conv["id"], "查点什么", status="error", error="模型不存在")
    detail = (await client.get(f"/api/conversations/{conv['id']}")).json()
    assert detail["turns"][0]["status"] == "error"
    assert "模型不存在" in detail["turns"][0]["error"]


# --------------------------------------------------------------------------
# 给模型看的那一份
# --------------------------------------------------------------------------


GRAPH = {
    "nodes": [
        {"id": "ask", "type": "input", "position": {"x": 1, "y": 2}, "data": {"config": {}}},
        {"id": "shop_agent", "type": "agent", "position": {"x": 9, "y": 9},
         "data": {"label": "查门店", "config": {"tools": ["db_query__cobook"]}}},
    ],
    "edges": [{"source": "ask", "target": "shop_agent"}],
}


async def _seeded(client) -> str:
    conv = await _conversation(client)
    await _turn(client, conv["id"], "cobook 里有几家店",
                answer="一共 12 家", graph=GRAPH, status="done")
    return conv["id"]


async def test_history_text_is_plain_qa(client) -> None:
    conv_id = await _seeded(client)
    async with SessionLocal() as session:
        text = await history_text(session, conv_id)
    assert "cobook 里有几家店" in text and "一共 12 家" in text


async def test_history_text_is_empty_without_a_conversation(client) -> None:
    async with SessionLocal() as session:
        assert await history_text(session, None) == ""
        assert await history_text(session, (await _conversation(client))["id"]) == ""


async def test_the_turn_in_flight_is_not_its_own_history(client) -> None:
    """当前这一轮还没有答案，把它算进上下文只会得到一句自己的问题。"""
    conv = await _conversation(client)
    await _turn(client, conv["id"], "正在问的这句")     # 保持 running
    async with SessionLocal() as session:
        assert await history_text(session, conv["id"]) == ""


async def test_history_keeps_only_the_recent_turns(client) -> None:
    conv = await _conversation(client)
    for i in range(HISTORY_TURNS + 3):
        await _turn(client, conv["id"], f"第{i}问", answer=f"第{i}答", status="done")
    async with SessionLocal() as session:
        text = await history_text(session, conv["id"])
    assert "第0问" not in text          # 最早的挤出去了
    assert f"第{HISTORY_TURNS + 2}问" in text
    assert text.count("问：") == HISTORY_TURNS


async def test_a_huge_answer_does_not_crowd_out_the_other_turns(client) -> None:
    conv = await _conversation(client)
    await _turn(client, conv["id"], "给我全部数据", answer="x" * 50_000, status="done")
    await _turn(client, conv["id"], "那总共多少", answer="12", status="done")
    async with SessionLocal() as session:
        text = await history_text(session, conv["id"])
    assert len(text) < 2000
    assert "那总共多少" in text          # 后一轮没被前一轮挤掉


async def test_history_section_carries_the_previous_graph(client) -> None:
    """只给问答文字不够：追问会让模型重新设计一张图，可能接到别的表上。"""
    from app.api.copilot import _history_section

    conv_id = await _seeded(client)
    async with SessionLocal() as session:
        block = await _history_section(session, conv_id)

    assert "cobook 里有几家店" in block
    assert "shop_agent" in block and "db_query__cobook" in block
    assert '"x"' not in block and '"position"' not in block   # _slim 去掉了坐标噪音


# --------------------------------------------------------------------------
# 上下文真的到了模型手上
# --------------------------------------------------------------------------


async def test_the_previous_turn_reaches_the_prompt(client, monkeypatch) -> None:
    """整件事的关键一环。

    拼字符串的函数测通过、却没接进 messages，是这类功能最典型的失败方式：
    界面上完全看不出来，它只是"答得不太对"。所以这里拦在模型入口上，
    看送出去的那条 human message 里到底有没有上一轮。
    """
    import app.api.copilot as copilot

    conv_id = await _seeded(client)
    captured: list = []

    class _Recorder:
        def with_structured_output(self, *_a, **_k):
            return self

        async def ainvoke(self, messages):
            captured.append(messages)
            return {"nodes": [], "edges": []}

    async def _model(*_a, **_k):
        return _Recorder(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)

    r = await client.post("/api/copilot/generate", json={
        "instruction": "再按月份拆一下", "intent": "answer", "conversation_id": conv_id,
    })
    assert r.status_code == 200, r.text

    human = next(text for role, text in captured[0] if role == "human")
    assert "cobook 里有几家店" in human       # 上一轮问的
    assert "一共 12 家" in human              # 上一轮答的
    assert "shop_agent" in human              # 上一轮的图
    assert "再按月份拆一下" in human          # 这一轮问的


async def test_no_conversation_means_no_history_header(client, monkeypatch) -> None:
    """不带 conversation_id 时不能凭空多出一个空的历史标题。"""
    import app.api.copilot as copilot

    captured: list = []

    class _Recorder:
        def with_structured_output(self, *_a, **_k):
            return self

        async def ainvoke(self, messages):
            captured.append(messages)
            return {"nodes": [], "edges": []}

    async def _model(*_a, **_k):
        return _Recorder(), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)

    r = await client.post("/api/copilot/generate", json={
        "instruction": "查一下门店", "intent": "answer",
    })
    assert r.status_code == 200, r.text
    human = next(text for role, text in captured[0] if role == "human")
    assert "之前聊过的" not in human
    assert human.startswith("用户问了下面这个问题")


# --------------------------------------------------------------------------
# 追问不该把图搞丢
# --------------------------------------------------------------------------


class _Streamer:
    """按脚本吐操作行的假模型。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def astream(self, _messages):
        from langchain_core.messages import AIMessageChunk

        for line in self._lines:
            yield AIMessageChunk(content=line + "\n")


async def _stream_graph(client, monkeypatch, conv_id: str, lines: list[str]) -> dict:
    """跑一次 generate-stream，返回最后那条 final/error。"""
    import app.api.copilot as copilot

    async def _model(*_a, **_k):
        return _Streamer(lines), "mock-fast"

    monkeypatch.setattr(copilot, "get_chat_model", _model)

    async with client.stream("POST", "/api/copilot/generate-stream", json={
        "instruction": "哪六个？哪五个？", "intent": "answer", "conversation_id": conv_id,
    }) as r:
        last = {}
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                last = json.loads(line[6:])
    return last


async def test_a_follow_up_that_only_sends_edits_still_produces_a_graph(
    client, monkeypatch,
) -> None:
    """真踩过的那条：f5db9809 的第二轮。

    上下文注入是对的——模型的说明写着"在上一轮已接好 CoBook 数据源的 agent
    节点上更新提示词"，它完全看懂了。但它照着改图的协议只发了 update_node，
    而这条路径的累加器从空图起步，于是那些操作全落空，用户收到的是
    "图校验未通过：图是空的，先拖一个节点进来"——他只是问了句追问。
    """
    conv_id = await _seeded(client)
    final = await _stream_graph(client, monkeypatch, conv_id, [
        '{"op":"plan","summary":"在上一轮的 agent 上改提示词"}',
        '{"op":"update_node","id":"shop_agent","config":{"prompt":"列出具体名单"}}',
        '{"op":"done","explanation":"沿用上一轮的图，只改了提示词"}',
    ])

    assert final.get("op") == "final", final
    ids = [n["id"] for n in final["graph"]["nodes"]]
    assert ids, "重放到上一轮那张图上之后不该还是空的"
    assert "shop_agent" in ids
    # 改动要真的落上去，不是原样把旧图还回来
    agent = next(n for n in final["graph"]["nodes"] if n["id"] == "shop_agent")
    assert agent["data"]["config"]["prompt"] == "列出具体名单"


async def test_a_full_rebuild_does_not_drag_the_old_nodes_along(
    client, monkeypatch,
) -> None:
    """模型照约定完整重发时，兜底不能画蛇添足。

    否则上一轮的 shop_agent 会和新节点一起留在图里，变成一个没人连的孤儿。
    """
    conv_id = await _seeded(client)
    final = await _stream_graph(client, monkeypatch, conv_id, [
        '{"op":"plan","summary":"重新搭"}',
        '{"op":"add_node","node":{"id":"in","type":"input","label":"入口","config":{}}}',
        '{"op":"add_node","node":{"id":"fresh_agent","type":"agent","label":"查","config":{}}}',
        '{"op":"add_edge","edge":{"source":"in","target":"fresh_agent"}}',
        '{"op":"add_node","node":{"id":"out","type":"output","label":"成果","config":{}}}',
        '{"op":"add_edge","edge":{"source":"fresh_agent","target":"out"}}',
        '{"op":"done","explanation":"新图"}',
    ])

    assert final.get("op") == "final", final
    ids = {n["id"] for n in final["graph"]["nodes"]}
    assert ids == {"in", "fresh_agent", "out"}
    assert "shop_agent" not in ids


async def test_the_first_turn_has_nothing_to_fall_back_on(client, monkeypatch) -> None:
    """没有上一轮时，空图就是空图——不能凭空变出一张来掩盖模型什么都没给。"""
    conv = await _conversation(client)
    final = await _stream_graph(client, monkeypatch, conv["id"], [
        '{"op":"plan","summary":"想想"}',
        '{"op":"done","explanation":"什么都没搭"}',
    ])
    assert final["op"] == "final"
    assert final["graph"]["nodes"] == []
    assert any("空" in str(i.get("message", "")) for i in final["issues"]), final["issues"]


# --------------------------------------------------------------------------
# 不是每句话都值得跑一遍库
# --------------------------------------------------------------------------
#
# 以前必执行：final 一到就无条件 launch。于是「重试」「继续找找」这种对过程
# 说的话，也各自重建了一整张图、跑一遍 153 张表的库；而一句明确要「设计一个
# 工作流」的，建完也直接跑了——用户要的是那张图，不是这一次的结果。


async def test_a_question_about_earlier_turns_needs_no_workflow(client, monkeypatch) -> None:
    """reply 这条路径：不建图、不跑、也不该走到那套排版校验上。

    走到了的话，空图会撞出"图校验未通过：图是空的，先拖一个节点进来"。
    """
    conv_id = await _seeded(client)
    final = await _stream_graph(client, monkeypatch, conv_id, [
        '{"op":"reply","text":"第 1 轮走的是 role.level IN (platform, regional) 这个口径。"}',
    ])
    assert final["op"] == "reply"
    assert "口径" in final["text"]
    assert "graph" not in final


async def test_asking_for_a_workflow_builds_it_but_does_not_run_it(client, monkeypatch) -> None:
    """用户要的是流程本身，跑一遍只是替他多花一次钱。"""
    conv_id = await _seeded(client)
    final = await _stream_graph(client, monkeypatch, conv_id, [
        '{"op":"plan","summary":"搭一个每天能跑的"}',
        '{"op":"add_node","node":{"id":"in","type":"input","label":"入口","config":{}}}',
        '{"op":"add_node","node":{"id":"out","type":"output","label":"成果","config":{}}}',
        '{"op":"add_edge","edge":{"source":"in","target":"out"}}',
        '{"op":"done","explanation":"每天跑一次","run":false}',
    ])
    assert final["op"] == "final"
    assert final["autorun"] is False
    assert [n["id"] for n in final["graph"]["nodes"]] == ["in", "out"]


async def test_a_normal_data_question_still_runs(client, monkeypatch) -> None:
    """默认不变：问数据就是建图 + 跑。没写 run 就当 true。"""
    conv_id = await _seeded(client)
    final = await _stream_graph(client, monkeypatch, conv_id, [
        '{"op":"plan","summary":"查一下"}',
        '{"op":"add_node","node":{"id":"in","type":"input","label":"入口","config":{}}}',
        '{"op":"add_node","node":{"id":"out","type":"output","label":"成果","config":{}}}',
        '{"op":"add_edge","edge":{"source":"in","target":"out"}}',
        '{"op":"done","explanation":"跑完给结论"}',
    ])
    assert final["op"] == "final"
    assert final["autorun"] is True


def test_the_three_paths_are_spelled_out_for_the_model() -> None:
    """路径选择全靠这段约定，尤其是那条不许凭印象给数字的硬约束。"""
    from app.api.copilot import GenerateIn, _STREAM_PROTOCOL, _user_message

    text = _user_message(GenerateIn(instruction="随便问问", intent="answer"), patch=False)
    assert "直接回答" in text and "建图并执行" in text and "建图但不执行" in text
    assert "不得新断言任何数据事实" in text
    # 协议里要给得出这两个出口，否则模型没法表达
    assert '"op":"reply"' in _STREAM_PROTOCOL
    assert '"run":true' in _STREAM_PROTOCOL


async def test_copilot_is_told_not_to_invent_a_memory_scope() -> None:
    """记忆域由平台给。图里自己起一个，写进去就再也读不回来。

    真发生过（会话 cbe09f4b 的第 2~3 轮）：一轮的 memory 节点写了
    scope="user"，下一轮没写 scope、落回 default，于是「我叫蒋逸伦」存下来了，
    问「我是谁」却答不上来——记忆在库里好好的，只是读错了地方。
    """
    from app.api.copilot import NODE_REFERENCE

    memory_line = next(
        (l for l in NODE_REFERENCE.splitlines() if l.strip().startswith("- memory：")), "")
    assert memory_line, "节点参考里找不到 memory 这一行"
    assert "scope" not in memory_line, "scope 还列在可填字段里，模型就会去填它"
    assert "不要写 scope" in NODE_REFERENCE
