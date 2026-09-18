"""工具参数的校验、纠错，以及 agent 一轮只跑一个工具。

起因是一条谁也看不懂的报错：

    工具 db_query__cobook 执行失败：
    _make_query_tool.<locals>._run() got an unexpected keyword argument 'query'

参数名写成了 query（它叫 sql），而动态工具那条调用路径当时是裸 `**args` 展开的，
于是 pydantic 的"缺少 sql 字段"漏成了一个只剩内部闭包名的 TypeError —— 既指不到
是哪个工具，也说不出该填什么，人和模型都只能接着猜。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from app.engine.nodes.llm import split_tool_calls
from app.tools.datasource import _make_query_tool, _make_schema_tool
from app.tools.registry import (
    ToolArgsError,
    ToolContext,
    args_model_of,
    describe_args,
    prepare_args,
)


class _QueryLike(BaseModel):
    """和 datasource 的查询参数同形：一个必填 + 一个可选。"""

    sql: str = Field(description="要执行的 SQL，一次一条")
    limit: int | None = Field(default=None, description="最多返回多少行，默认 1000")


class _AllOptional(BaseModel):
    table: str | None = Field(default=None, description="表名；留空则列出所有表")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "shop.db"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL);")
    db.executemany("INSERT INTO orders(id,amount) VALUES(?,?)", [(i, i * 1.5) for i in range(1, 6)])
    db.commit()
    db.close()
    return SimpleNamespace(
        # id 必须和别的测试文件不同：engine 按 source.id 缓存连接池
        # （app/data/engine.py:121），撞了 id 就会连到人家那个库上——
        # 默认的文件顺序下碰巧不冲突，单跑一个文件或换个顺序就串了
        id="tool-args-src", name="cobook", kind="sqlite", database=str(path),
        host=None, port=None, username=None, password=None, options={},
        readonly=True, description="测试库", schema_cache={},
    )


# --------------------------------------------------------------------------
# prepare_args
# --------------------------------------------------------------------------


def test_unique_candidate_is_corrected() -> None:
    """多出来一个 query、少了一个必填的 sql —— 候选唯一，改名就是了。"""
    payload, note = prepare_args(_QueryLike, {"query": "SELECT 1"})
    assert payload["sql"] == "SELECT 1"
    assert note and "query" in note and "sql" in note


def test_correct_args_pass_through_without_a_note() -> None:
    """本来就对的不该被说成"纠正过"，否则轨迹里全是没用的黄条。"""
    payload, note = prepare_args(_QueryLike, {"sql": "SELECT 1", "limit": 10})
    assert (payload["sql"], payload["limit"]) == ("SELECT 1", 10)
    assert note is None


def test_ambiguous_names_are_not_guessed() -> None:
    """两个都不认识时改名就是碰运气，不如把参数表摊开。"""
    with pytest.raises(ToolArgsError) as e:
        prepare_args(_QueryLike, {"foo": "SELECT 1", "bar": 3})
    text = str(e.value)
    assert "sql" in text and "limit" in text
    assert "foo" in text and "bar" in text


def test_typo_on_all_optional_schema_is_not_silently_dropped() -> None:
    """全可选的 schema 上，pydantic 默认会把认不出的键悄悄丢掉。

    真丢了的话 {"tabel": "orders"} 会"成功"执行一次什么都没查的调用 ——
    这种静默失败比报错难查得多。这里必须要么纠正、要么报错。
    """
    payload, note = prepare_args(_AllOptional, {"tabel": "orders"})
    assert payload["table"] == "orders"
    assert note is not None


def test_wrong_type_names_the_field_and_the_expected_type() -> None:
    """名字对但类型不对，不是"写错名字"，别顺手改名把错误盖掉。"""
    with pytest.raises(ToolArgsError) as e:
        prepare_args(_QueryLike, {"sql": "SELECT 1", "limit": "很多"})
    assert "limit 的值不合法，应为整数" in str(e.value)


def test_missing_required_says_what_is_required() -> None:
    with pytest.raises(ToolArgsError) as e:
        prepare_args(_QueryLike, {})
    assert "缺少必填参数 sql" in str(e.value)


def test_every_problem_is_reported_at_once() -> None:
    """改对了名字再回来撞一次类型错误，等于让人分两趟改。"""
    with pytest.raises(ToolArgsError) as e:
        prepare_args(_QueryLike, {"query": "SELECT 1", "limit": "很多"})
    text = str(e.value)
    assert "query 不是它的参数" in text
    assert "limit 的值不合法" in text


def test_the_message_is_a_whole_sentence() -> None:
    """调用方会把它直接拼在工具名后面（"工具 X：…"），不能缺主语。"""
    for args in ({}, {"foo": 1, "bar": 2}, {"sql": "SELECT 1", "limit": "很多"}):
        with pytest.raises(ToolArgsError) as e:
            prepare_args(_QueryLike, args)
        assert str(e.value).startswith("参数不对：")


# --------------------------------------------------------------------------
# describe_args —— 这段话是模型下一步改对的唯一依据
# --------------------------------------------------------------------------


def test_description_carries_the_field_help_text() -> None:
    text = describe_args(_QueryLike, {"query": "SELECT 1"})
    assert "要执行的 SQL" in text          # Field(description=...) 要搬进来
    assert "必填" in text and "可选" in text
    assert "字符串" in text and "整数" in text  # 可选字段是 anyOf[int, null]，要摊开
    assert "query" in text                 # 说清楚"你传的是什么"


# --------------------------------------------------------------------------
# 真实的数据源工具：复现开头那条报错
# --------------------------------------------------------------------------


async def test_the_original_query_typo_now_runs(source) -> None:
    tool = _make_query_tool(source, ToolContext())
    schema = args_model_of(tool)
    assert schema is not None

    payload, note = prepare_args(schema, {"query": "SELECT COUNT(*) AS c FROM orders"})
    assert note is not None
    result = await tool.coroutine(**payload)
    assert '"c"' in result or "c" in result


async def test_raw_expansion_is_what_used_to_explode(source) -> None:
    """记录一下被修掉的是什么：绕开校验直接展开，抛的就是那句天书。"""
    tool = _make_query_tool(source, ToolContext())
    with pytest.raises(TypeError) as e:
        await tool.coroutine(query="SELECT 1")
    assert "unexpected keyword argument" in str(e.value)


async def test_schema_tool_args_are_validated_too(source) -> None:
    tool = _make_schema_tool(source)
    schema = args_model_of(tool)
    payload, note = prepare_args(schema, {"tabel": "orders"})
    assert payload["table"] == "orders" and note is not None


async def test_call_tool_end_to_end_on_the_path_that_broke(source) -> None:
    """走 call_tool 这个真实入口（工具节点和设置页的"试一下"都走它）。

    以前这条路径对动态工具是裸 `**args` 展开的，报错止步于 TypeError；
    现在参数名能被纠正、纠正这件事能通过 on_fix 说出去。
    """
    from app.db.base import SessionLocal
    from app.db.models import DataSource
    from app.tools.registry import call_tool

    async with SessionLocal() as session:
        session.add(DataSource(
            name="cobook", kind="sqlite", database=source.database,
            readonly=True, description="测试库", options={}, schema_cache={}, enabled=True,
        ))
        await session.commit()

    notes: list[str] = []
    async with SessionLocal() as session:
        out = await call_tool(
            "db_query__cobook", {"query": "SELECT COUNT(*) AS c FROM orders"},
            ToolContext(), session=session, on_fix=notes.append,
        )
    assert "5" in str(out)
    # 跑通了，但配置里那个错的参数名还在，必须留下痕迹而不是安静地把事办了
    assert len(notes) == 1 and "sql" in notes[0]

    async with SessionLocal() as session:
        with pytest.raises(ToolArgsError) as e:
            await call_tool("db_query__cobook", {"foo": 1, "bar": 2}, ToolContext(), session=session)
    assert "sql" in str(e.value)
    assert "unexpected keyword argument" not in str(e.value)


def test_tools_without_a_pydantic_schema_are_let_through() -> None:
    """MCP 这类工具的 args_schema 可能是一份 JSON Schema dict，校验不了就放行。"""
    assert args_model_of(SimpleNamespace(args_schema={"type": "object"})) is None
    assert args_model_of(SimpleNamespace(args_schema=None)) is None


# --------------------------------------------------------------------------
# agent 一轮只跑一个工具
# --------------------------------------------------------------------------


def _calls() -> list[dict]:
    return [
        {"name": "db_schema__cobook", "args": {}, "id": "c1"},
        {"name": "db_query__cobook", "args": {"sql": "SELECT 1"}, "id": "c2"},
        {"name": "db_query__cobook", "args": {"sql": "SELECT 2"}, "id": "c3"},
    ]


def test_serial_runs_only_the_first() -> None:
    run_calls, deferred = split_tool_calls(_calls(), parallel=False)
    assert [c["id"] for c in run_calls] == ["c1"]
    assert [c["id"] for c, _ in deferred] == ["c2", "c3"]


def test_every_deferred_call_still_gets_a_result() -> None:
    """两家 API 都要求每个 tool_use 有一条配对的 tool_result，少一条下次调用就是 400。"""
    calls = _calls()
    run_calls, deferred = split_tool_calls(calls, parallel=False)
    answered = {c["id"] for c in run_calls} | {m.tool_call_id for _, m in deferred}
    assert answered == {c["id"] for c in calls}
    assert all(isinstance(m, ToolMessage) and m.content for _, m in deferred)


def test_parallel_keeps_every_call() -> None:
    run_calls, deferred = split_tool_calls(_calls(), parallel=True)
    assert len(run_calls) == 3 and deferred == []


def test_single_call_is_never_deferred() -> None:
    run_calls, deferred = split_tool_calls(_calls()[:1], parallel=False)
    assert len(run_calls) == 1 and deferred == []


# --------------------------------------------------------------------------
# 串行也要告诉模型，省得它白发一堆调用
# --------------------------------------------------------------------------


def _one_tool():
    from app.tools.registry import all_specs, build_tool

    return [build_tool(next(iter(all_specs().values())), ToolContext())]


def test_anthropic_gets_disable_parallel_tool_use() -> None:
    """ChatAnthropic 把它翻成 tool_choice，不是原样的 parallel_tool_calls。

    type 必须是 auto：强制某个工具（any/tool）和 thinking 不能并存，
    而 agent 节点默认是开着 thinking 的。
    """
    from langchain_anthropic import ChatAnthropic

    from app.providers.factory import bind_tools_safely

    model = ChatAnthropic(model="claude-opus-5", api_key="x",
                          thinking={"type": "adaptive", "display": "summarized"})
    choice = bind_tools_safely(model, _one_tool(), parallel=False).kwargs["tool_choice"]
    assert choice == {"type": "auto", "disable_parallel_tool_use": True}
    assert "tool_choice" not in bind_tools_safely(model, _one_tool(), parallel=True).kwargs


def test_openai_gets_parallel_tool_calls() -> None:
    from langchain_openai import ChatOpenAI

    from app.providers.factory import bind_tools_safely

    model = ChatOpenAI(model="gpt-4o", api_key="x")
    assert bind_tools_safely(model, _one_tool(), parallel=False).kwargs["parallel_tool_calls"] is False
    assert "parallel_tool_calls" not in bind_tools_safely(model, _one_tool(), parallel=True).kwargs


def test_binding_survives_models_that_ignore_the_flag() -> None:
    """mock 和各种兼容网关不认这个参数也不能炸——串行本来就有编排层兜底。"""
    from app.providers.factory import bind_tools_safely
    from app.providers.mock_model import MockChatModel

    model = MockChatModel(model_name="mock-fast")
    assert bind_tools_safely(model, _one_tool(), parallel=False) is not None
    # 没有工具时原样返回，不该凭空包一层
    assert bind_tools_safely(model, [], parallel=False) is model


# --------------------------------------------------------------------------
# 步数用完时的收尾
# --------------------------------------------------------------------------


def test_the_skip_note_never_becomes_the_answer() -> None:
    """真出过事：run dd9927e6 交给用户的"答案"是一句内部管道文案。

        本轮只执行了第一个工具。看到它的结果之后，再决定这一个还要不要调…

    步数用完时，收尾取的是 messages[-1]——而串行模式下那很可能是一条给模型看的
    ToolMessage。用户看到的不是"没跑完"，是一句冒充结论的管道说明。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from app.engine.nodes.llm import SKIPPED_NOTE

    messages = [
        HumanMessage(content="CoBook 有几个管理员？"),
        AIMessage(content="我先看看表结构。"),
        ToolMessage(content="表 user …", tool_call_id="c1"),
        ToolMessage(content=SKIPPED_NOTE, tool_call_id="c2"),
    ]
    # 收尾该取的是模型自己说过的最后一句，不是消息数组的最后一条
    picked = next(
        (t for m in reversed(messages)
         if isinstance(m, AIMessage) and (t := m.content.strip())),
        "",
    )
    assert picked == "我先看看表结构。"
    assert SKIPPED_NOTE not in picked


# --------------------------------------------------------------------------
# 查表结构可以一次问好几张
# --------------------------------------------------------------------------


async def test_schema_tool_takes_several_tables_at_once(source) -> None:
    """一次调用就是一步。逐张问 6 张表就是 6 步，一次 8 步的预算直接见底。"""
    from app.data import introspect

    source.schema_cache = await introspect.introspect(source)
    tool = _make_schema_tool(source)

    one = await tool.coroutine(table="orders")
    assert "orders" in one

    many = await tool.coroutine(table=["orders", "orders"])
    assert many.count("表 orders") == 2      # 两张都描述了，不是只认第一张


async def test_schema_tool_accepts_a_comma_separated_string(source) -> None:
    """模型两种写法都会用。为此拒掉一次调用，白白浪费一步。"""
    from app.data import introspect

    source.schema_cache = await introspect.introspect(source)
    out = await _make_schema_tool(source).coroutine(table="orders, orders")
    assert out.count("表 orders") == 2


async def test_schema_tool_still_lists_everything_when_asked_for_nothing(source) -> None:
    from app.data import introspect

    source.schema_cache = await introspect.introspect(source)
    out = await _make_schema_tool(source).coroutine()
    assert "张表" in out and "orders" in out


def test_copilot_is_told_not_to_pin_max_steps() -> None:
    """Copilot 把 max_steps: 8 写进了生成的图，后端默认值根本轮不上。"""
    from app.api.copilot import NODE_REFERENCE

    assert "不要写 max_steps" in NODE_REFERENCE
