from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Workflow
from app.engine.schema import GraphSpec, NodeType, validate_graph
from app.engine.state import message_text
from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model
from app.tools.registry import all_specs

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


async def _tool_catalog(session: AsyncSession) -> str:
    """给 Copilot 看的工具清单。

    all_specs() 只有静态注册的内置工具，数据源工具是运行时按库动态生成的
    （db_query__<源>），不补进来 Copilot 就不知道它们存在，只会退回让用户
    自己写 SQL 的代码节点——接了数据库等于白接。
    """
    lines = [
        f"- {name}（{spec.category}）：{spec.description}"
        for name, spec in sorted(all_specs().items())
    ]

    from app.data import introspect as _introspect
    from app.db.models import DataSource
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX

    rows = list((
        await session.execute(
            select(DataSource).where(DataSource.enabled.is_(True)).order_by(DataSource.name)
        )
    ).scalars())
    for row in rows:
        tables = _introspect.table_names(row)
        listed = "、".join(tables[:12]) + (f" 等 {len(tables)} 个对象" if len(tables) > 12 else "")
        lines.append(
            f"- {QUERY_PREFIX}{row.name}（数据库）：在「{row.name}」上执行 SQL。"
            f"{row.description or ''}"
            + (f" 可查：{listed}" if tables else " ⚠ 结构未探查")
        )
        lines.append(
            f"- {SCHEMA_PREFIX}{row.name}（数据库）：查看「{row.name}」的表结构"
        )
    return "\n".join(lines)


# 数据源摘要的字符预算。超了就退成"只列对象名"，字段让 Copilot 用
# db_schema 工具按需查。实测一个 53 对象的库带字段约 4000 字符，
# 接三五个库就会把真正的需求淹掉——system prompt 里塞满 schema 而挤掉
# 用户想要什么，是本末倒置。
_DATASOURCE_BUDGET_CHARS = 6000
# detail 模式下每个库最多列这么多对象（与 introspect.summary 的默认值一致）。
# 超过就说明会截断，那还不如切紧凑模式把对象名列全。
_DETAIL_TABLE_LIMIT = 40


async def _datasource_section(session: AsyncSession) -> str:
    """数据源与结构摘要。没有数据源时返回空串，不占 prompt。"""
    from app.data import introspect as _introspect
    from app.db.models import DataSource

    rows = list((
        await session.execute(
            select(DataSource).where(DataSource.enabled.is_(True)).order_by(DataSource.name)
        )
    ).scalars())
    if not rows:
        return ""

    # detail 模式每个库最多列 40 个对象，多出来的直接看不见。对象一多，
    # "40 张表带字段"反而不如"全部表只给名字"——Copilot 找不到那张表，
    # 字段写得再全也没用。所以超出截断阈值就整体切到紧凑模式。
    detail = all(len(_introspect.table_names(row)) <= _DETAIL_TABLE_LIMIT for row in rows)
    blocks = [_introspect.summary(row, detail=detail) for row in rows]
    if detail and sum(len(b) for b in blocks) > _DATASOURCE_BUDGET_CHARS:
        detail = False
        blocks = [_introspect.summary(row, detail=False) for row in rows]
    if not detail:
        # Copilot 仍然知道有哪些对象可查，要字段时在图里放一个 db_schema 节点，
        # 或者交给运行期的 agent 自己探
        blocks.append(
            "  （对象较多，上面只列了名字。写 SQL 前用 db_schema 工具确认字段，别猜）"
        )
    return (
        "\n\n已接入的数据源（涉及取数时优先用它们，而不是让用户自己写 SQL 的代码节点）：\n"
        + "\n".join(blocks)
        + "\n\n写 SQL 的硬性要求：\n"
        # 这三条都是真实踩过的：Oracle 上漏 schema 前缀直接 ORA-00942，
        # 写惯 MySQL 的模型会顺手写 LIMIT，而一条没有行数限制的查询能拖垮生产库
        "- 表名照抄上面的全名（含 schema 前缀），漏掉前缀在 Oracle 上会直接报 ORA-00942\n"
        "- 认准方言：Oracle 用 FETCH FIRST n ROWS ONLY，不是 LIMIT\n"
        "- 一次只写一条语句；分号拼接会被拒\n"
        "- 字段拿不准就在图里先放一个 db_schema 工具节点，别猜字段名\n"
        "- 取数结果要进口径卡（metrics 节点）才能被叙述引用，别让 llm 节点直接对数字做算术\n"
    )


NODE_REFERENCE = """\
可用节点类型（type 字段）：
- input：入口。config.fields = [{name, required, default, description}]
- output：出口，收集最终成果。config.fields = [{name, value}]，value 里写模板引用
- llm：单次模型调用。config: {system, prompt, model, temperature, max_tokens, assign_to, output_schema}
- agent：带工具循环的 agent。config: {system, prompt, tools:[工具名], approval, assign_to}
  **不要写 max_steps**。平台默认值是按「一步只调一个工具」校准过的；写死一个
  小数字（见过 8）会让它查完表结构就没额度回答了，用户拿到的是半截结论
- supervisor：多 agent 协作。config: {goal, agents:[{name, description, system, tools, model}], max_rounds}
- tool：直接调一个工具。config: {tool: 工具名, args: {...}, assign_to}
- code：沙箱里跑代码。config: {language: python|bash|node, code, timeout, network, assign_to}
- branch：条件分支。config: {mode: expression|llm, cases:[{key, condition, label}]}
- loop：循环。config: {mode: foreach|while, items, item_var, condition, max_iterations}
- retrieve：知识库检索。config: {query, collection, limit, assign_to}
- memory：长期记忆读写。config: {action: recall|write, query, content, assign_to}
  **不要写 scope**。记忆域由平台给（当前统一是 default）。自己起一个域的后果是
  写进去再也读不回来——真发生过：一轮写进 scope"user"，下一轮没写 scope 落回
  default，于是「我叫张三」存下来了，问「我是谁」却答不上来
- human：人工介入。config: {mode: approve|input|edit, title, message}
- validate：JSON Schema 校验，可自动让模型修复。config: {schema, source, max_retries}
- transform：数据整形。config: {mode: expression|template|json, expression/template, assign_to}
- subgraph：嵌套另一个工作流。config: {workflow_id, input}

模型字段（llm / agent / supervisor 的 config.model）：
- **不要填**。留空表示跟随当前供应商的默认模型，这几乎总是对的。
- 你不知道这套部署里有哪些 model id——凭供应商名字猜（比如看到 DeepSeek 就写
  "deepseek-chat"）会得到 401 invalid_model，整个节点跑不起来。
- 只有用户在需求里明确点名了某个模型，才把他说的那个字符串原样填进去。

模板语法（在任意字符串里用）：
- {{ input.字段名 }}        入口输入
- {{ vars.变量名 }}         某节点 assign_to 写入的变量
- {{ nodes.节点id.text }}   某节点的输出
- {{ last_message }}        最近一条消息文本
- 过滤器：{{ vars.x | json }}

**引用必须有来源**（这条会被校验，不满足整张图跑不起来）：
- 写 {{ vars.X }} 之前，必须有某个节点的 config.assign_to 正好是 "X"
- 写 {{ input.X }} 之前，X 必须在入口节点的 fields 里声明过
- 写 {{ nodes.Y.* }} 之前，Y 必须是真实存在的节点 id
- 引用的那个节点还必须排在**前面**——后面的节点产出的值，前面取不到
模板取不到值时渲染成空字符串、不报错，所以这类错误在运行期完全静默：
下游拿到一段空文本，然后一本正经地基于空内容编一段回答出来。

连线规则：
- edges 里每条边 {source, target, sourceHandle?}
- branch 节点的出边必须带 sourceHandle，取值是 cases 里的 key，另外要有一条 sourceHandle="default" 兜底
- loop 节点：sourceHandle="body" 连循环体，循环体最后一个节点再连回 loop 节点；sourceHandle="done" 连循环结束后的去向
- human 节点（approve 模式）：sourceHandle 用 "approved" 和 "rejected"
- 一个节点连出多条普通边 = 并行执行，多条边汇入同一节点 = 等待汇聚
"""


def auto_layout(spec: GraphSpec, *, x_gap: int = 300, y_gap: int = 150) -> GraphSpec:
    """按拓扑层级排版。

    模型生成的图只有结构没有坐标，全堆在原点就没法看了。
    这里按最长路径分层：同层的节点竖排，层与层横向铺开。
    """
    nodes = spec.node_map()
    depth: dict[str, int] = {}

    def compute(node_id: str, seen: frozenset[str]) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id in seen:  # 环：就地截断，避免递归爆栈
            return 0
        incoming = [e.source for e in spec.incoming(node_id) if e.source in nodes]
        value = 0 if not incoming else 1 + max(
            compute(src, seen | {node_id}) for src in incoming
        )
        depth[node_id] = value
        return value

    for node in spec.nodes:
        compute(node.id, frozenset())

    by_level: dict[int, list[str]] = {}
    for node_id, level in depth.items():
        by_level.setdefault(level, []).append(node_id)

    for level, ids in sorted(by_level.items()):
        offset = -(len(ids) - 1) * y_gap / 2
        for i, node_id in enumerate(sorted(ids)):
            node = nodes[node_id]
            node.position.x = 80 + level * x_gap
            node.position.y = 300 + offset + i * y_gap
    return spec


class GenerateIn(BaseModel):
    instruction: str = Field(min_length=1, description="用自然语言描述想要的工作流")
    base_graph: dict[str, Any] | None = Field(default=None, description="在现有图上修改时传入")
    provider: str | None = None
    model: str | None = None
    #: build = 用户在描述一个流程（画布）；answer = 用户在问一个问题（问数据页）
    intent: str = Field(default="build")
    #: 属于哪次对话。带上它，这一轮才知道前面聊过什么
    conversation_id: str | None = None


def _user_message(payload: GenerateIn, *, patch: bool) -> str:
    """把用户说的话包成给模型的请求。

    两个入口的语义完全不同，包法必须跟着变：

    - 画布上，用户**在描述一个流程**（"读用户的问题，先查知识库…"），
      "请设计一个工作流：<原话>" 是对的。
    - 问数据页，用户**在问一个问题**（"shop 里都有哪些数据？"）。同样
      包成"请设计一个工作流：shop 里都有哪些数据？"，模型会老老实实设计
      一个**关于这句话**的流程——实际见过它生成"给这段文字生成摘要"，
      跑完把用户的问题原样复述一遍还回去。流程是手段，不是用户要的东西。
    """
    if patch:
        return (
            "这是当前的工作流：\n"
            "请按下面的要求修改它（只输出改动操作）：\n" + payload.instruction
        )
    if payload.intent == "answer":
        return (
            "用户问了下面这个问题。请设计一个**能回答它**的工作流——"
            "流程是手段，用户要的是答案。\n\n"
            f"问题：{payload.instruction}\n\n"
            "这条路径的硬性约定：\n"
            # 问题由 ChatPage 注入到 input.question。不把键名说死，模型会自己
            # 编一个（见过 input.text、input.topic），下游取到的就是空字符串——
            # 而模板取不到值不报错，于是模型一本正经地基于空内容编一段回答
            "- 问题已经由系统注入成 `{{ input.question }}`，要用就写这一个。"
            "入口节点**不要声明任何字段**，也不会有人再填一次表单\n"
            "- 涉及数据的问题必须用 db_query__* / db_schema__* 去真查，"
            "不要只对问题本身做文字加工（改写、摘要、分类都不是回答）\n"
            "- 出口节点给出的应当是这个问题的答案本身\n"
            # 只把历史放进 prompt 是不够的：模型拿到上一轮的图，默认仍然会
            # 重新设计一张"更完整"的。而用户说"再按月份拆一下"时要的是上一张
            # 图改个 SQL——重建出来的那张经常接到另一张表上，答案对不上前一轮。
            #
            # "完整输出"这半句是必须的。只说"在它基础上改"，模型会照着改图的
            # 协议只发 update_node——而这条路径的累加器是从空图起步的，那些
            # 操作全都落在不存在的节点上，最后得到一张空图，用户看到的是
            # "图校验未通过：图是空的，先拖一个节点进来"。真踩过。
            "- 如果上面给了上一轮的工作流，就沿用它的节点 id 和 config（数据源、"
            "工具、表都已经对好了），只改需要改的地方（通常是 SQL 或提示词）\n"
            "- 但**每个节点都要用 add_node 完整输出一遍**，包括没有改动的。"
            "这一轮是从空图开始搭的，只发改动操作会得到一张空图\n"
            "- 之前几轮的问答由系统注入成 `{{ input.history }}`。用户用「它们」「这些」"
            "这类指代时，要让回答的节点引用它，否则模型不知道指的是上一轮那批数据\n"
            "\n"
            "先判断这句话该走哪条路径（三选一）：\n"
            # 以前这里没有分叉：每一句话都建一张图、跑一遍库。于是「重试」
            # 「继续找找」这种对过程说的话，也各自重建了一整张图去查 153 张表
            "1. **直接回答**（输出 reply）：这句话不需要新数据就能答——问的是"
            "前面几轮已经查出来的东西（口径、怎么算的、哪张表）、是对过程说的话"
            "（「重试」「换个说法」），或者你需要先反问清楚才能动手。\n"
            "   硬性约束：reply 里**只能复述或解释前面几轮已有的结果**，"
            "不得新断言任何数据事实——不许凭印象给数字、给名单、给结论。"
            "凡是要动到库才知道的，哪怕你觉得上一轮见过，也走第 2 条重新查。\n"
            "2. **建图并执行**（done 的 run 为 true，默认）：要新数据才能答的问题。\n"
            "3. **建图但不执行**（done 的 run 为 false）：用户要的是**流程本身**"
            "——「设计一个工作流」「做一个每天能跑的」这类。他要的是这张图，"
            "不是这一次的结果，跑一遍只是替他多花一次钱。\n"
        )
    return f"请设计一个工作流：{payload.instruction}"


async def _previous_graph(
    session: AsyncSession, conversation_id: str | None
) -> dict[str, Any] | None:
    """这次对话上一轮建出来的图。没有就是 None。"""
    from app.api.conversations import recent_turns

    if not conversation_id:
        return None
    turns = await recent_turns(session, conversation_id)
    return next((t.graph for t in reversed(turns) if t.graph), None)


async def _history_section(session: AsyncSession, conversation_id: str | None) -> str:
    """把之前几轮拼成给模型的上下文。没有会话或没有历史时返回空串。

    带上一轮的图是关键的那一半。只给问答文字的话，"再按月份拆一下"会让模型
    重新设计一张图——它得从头猜该接哪个数据源、哪张表，而这些上一轮已经对好了。
    实测这种重建经常接到另一张表上，答案对不上前一轮，用户完全无从判断。

    图走 _slim 去掉坐标噪音，和 base_graph 那条路径用的是同一份。
    """
    from app.api.conversations import _answer_of, recent_turns

    if not conversation_id:
        return ""
    turns = await recent_turns(session, conversation_id)
    if not turns:
        return ""

    lines = [
        f"第 {i} 轮\n  问：{t.question}\n  答：{_answer_of(t) or '（没有结果）'}"
        for i, t in enumerate(turns, 1)
        if t.question
    ]
    if not lines:
        return ""

    block = "# 这次对话之前聊过的\n" + "\n".join(lines)

    last_graph = next((t.graph for t in reversed(turns) if t.graph), None)
    if last_graph:  # 和 _previous_graph 取的是同一张，兜底重放靠的就是它
        block += (
            "\n\n上一轮用的工作流（数据源和表都已经对好了）：\n"
            f"{json.dumps(_slim(last_graph), ensure_ascii=False, indent=2)}"
        )
    return block


def _with_history(user: str, history: str) -> str:
    """历史垫在请求前面。空历史时原样返回，不留一个空标题。"""
    return f"{history}\n\n---\n\n{user}" if history else user


COPILOT_SETTING_KEY = "copilot"


async def copilot_model_spec(
    session: AsyncSession, payload: GenerateIn, *, max_tokens: int = 8192
) -> ModelSpec:
    """决定 Copilot 用哪个模型。

    优先级：本次请求显式指定 > 设置里存的 Copilot 专用模型 > provider 兜底。
    做成独立设置是因为 Copilot 是元任务——它写的是编排本身，对指令遵循的要求
    和工作流里的节点不是一回事，值得单独选，而不是跟着"第一个启用的 provider"漂。
    """
    from app.db.models import Setting

    provider, model = payload.provider, payload.model
    if not provider and not model:
        row = await session.get(Setting, COPILOT_SETTING_KEY)
        saved = row.value if row else {}
        provider = saved.get("provider") or None
        model = saved.get("model") or None
    return ModelSpec(provider=provider, model=model, max_tokens=max_tokens)


@router.get("/model")
async def get_copilot_model(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """当前 Copilot 会用哪个模型，以及是显式配的还是兜底来的。"""
    from app.db.models import Setting
    from app.providers.factory import resolve_provider

    row = await session.get(Setting, COPILOT_SETTING_KEY)
    saved = row.value if row else {}
    configured = bool(saved.get("provider") or saved.get("model"))
    resolved = await resolve_provider(session, saved.get("provider"), saved.get("model"))
    return {
        "configured": configured,
        "provider": saved.get("provider") or None,
        "model": saved.get("model") or None,
        "effective_provider": resolved.name if resolved else None,
        "effective_model": saved.get("model") or (resolved.default_model if resolved else None),
    }


class CopilotModelIn(BaseModel):
    provider: str | None = None
    model: str | None = None


@router.put("/model")
async def set_copilot_model(
    payload: CopilotModelIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """设定 Copilot 专用模型。两个字段都留空即恢复兜底。"""
    from app.db.models import Setting

    value = {"provider": payload.provider or "", "model": payload.model or ""}
    row = await session.get(Setting, COPILOT_SETTING_KEY)
    if row:
        row.value = value
    else:
        session.add(Setting(key=COPILOT_SETTING_KEY, value=value))
    await session.commit()
    return await get_copilot_model(session)


class GenerateOut(BaseModel):
    graph: dict[str, Any]
    explanation: str = ""
    issues: list[dict[str, Any]] = Field(default_factory=list)


GRAPH_SCHEMA: dict[str, Any] = {
    "title": "workflow_graph",  # langchain 靠 title 识别 JSON Schema dict
    "type": "object",
    "properties": {
        "explanation": {"type": "string", "description": "一两句话说明这张图怎么跑"},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": [t.value for t in NodeType]},
                    "label": {"type": "string"},
                    # additionalProperties 必须显式写 true：config 的键随节点类型
                    # 千变万化（tool 有 tool/args，code 有 language/code…），没法
                    # 提前枚举成 properties。而不少 provider 的结构化输出实现在
                    # 遇到没有 properties 的 object 时按"不允许额外字段"处理，
                    # 于是模型填好的 config 被整个过滤成 {}——生成的图每个节点都
                    # 是空壳，校验直接挂在"工具节点还没选工具"。
                    "config": {"type": "object", "additionalProperties": True},
                },
                "required": ["id", "type", "config"],
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "sourceHandle": {"type": "string"},
                },
                "required": ["source", "target"],
            },
        },
    },
    "required": ["nodes", "edges"],
}


@router.post("/generate", response_model=GenerateOut)
async def generate(
    payload: GenerateIn, session: AsyncSession = Depends(get_session)
) -> GenerateOut:
    """用自然语言生成或改写一张工作流图。

    产物会经过和手工编排完全相同的校验与排版，所以生成完就是可运行、可读的，
    而不是一段还要人去修的 JSON。
    """
    tool_list = await _tool_catalog(session)
    datasources = await _datasource_section(session)

    system = (
        "你是一个 agent 工作流编排专家。根据用户需求产出一张可执行的工作流图。\n\n"
        f"{NODE_REFERENCE}\n\n可用工具：\n{tool_list}{datasources}\n\n"
        "要求：\n"
        "1. 必须有且只有一个 input 节点和至少一个 output 节点\n"
        "2. 节点 id 用简短英文小写下划线，例如 fetch_data、summarize\n"
        "3. 每个节点写清楚 label（中文），让人在画布上一眼看懂它干什么\n"
        "4. 需要把结果传给下游时，用 assign_to 命名变量，下游用 {{ vars.变量名 }} 引用\n"
        "5. 不要凭空发明工具名，只能用上面列出的\n"
        "6. 结构尽量简单：能用 3 个节点解决就别堆 8 个"
    )

    if payload.base_graph and payload.base_graph.get("nodes"):
        user = (
            "这是当前的工作流：\n"
            f"{json.dumps(_slim(payload.base_graph), ensure_ascii=False, indent=2)}\n\n"
            f"请按下面的要求修改它，返回完整的新图（不是补丁）：\n{payload.instruction}"
        )
    else:
        user = _user_message(payload, patch=False)

    try:
        model, _ = await get_chat_model(session, await copilot_model_spec(session, payload))
    except ProviderNotConfigured as e:
        raise HTTPException(400, str(e)) from e

    messages = [("system", system),
                ("human", _with_history(user, await _history_section(session, payload.conversation_id)))]
    try:
        raw = await model.with_structured_output(GRAPH_SCHEMA).ainvoke(messages)
        if not isinstance(raw, dict):
            raw = json.loads(message_text(raw))
    except Exception:  # noqa: BLE001 - 不支持结构化输出的模型退回文本解析
        try:
            text = message_text(await model.ainvoke(messages))
            raw = _extract_json(text)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"模型没有返回可用的图：{type(e).__name__}: {e}") from e

    graph = {
        "nodes": [
            {
                "id": n.get("id") or f"node{i}",
                "type": n.get("type") or "llm",
                "position": {"x": 0, "y": 0},
                "data": {"label": n.get("label") or "", "config": n.get("config") or {}},
            }
            for i, n in enumerate(raw.get("nodes") or [])
        ],
        "edges": [
            {
                "source": e.get("source", ""),
                "target": e.get("target", ""),
                "sourceHandle": e.get("sourceHandle"),
            }
            for e in (raw.get("edges") or [])
        ],
    }

    try:
        spec = GraphSpec.model_validate(graph)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"生成的图结构非法：{e}") from e

    spec = auto_layout(spec)
    report = validate_graph(spec)
    return GenerateOut(
        graph=spec.model_dump(mode="json"),
        explanation=str(raw.get("explanation", "")),
        issues=[i.model_dump() for i in report.issues],
    )


# --------------------------------------------------------------------------
# 流式生成：模型输出逐行操作流（NDJSON），画布看着图长出来
# --------------------------------------------------------------------------

_STREAM_PROTOCOL = """\
输出格式（严格遵守）：
- 每行一个完整的 JSON 对象，除此之外不输出任何东西——不要 markdown 围栏、不要解释文字、不要空行注释
- 可用操作：
  {"op":"plan","summary":"一句话说明打算怎么搭"}          ← 第一行
  {"op":"add_node","node":{"id":"...","type":"...","label":"中文标签","config":{...}}}
  {"op":"update_node","id":"...","label":"...","config":{...}}   ← 修改现有节点（config 整体替换）
  {"op":"remove_node","id":"..."}
  {"op":"add_edge","edge":{"source":"...","target":"...","sourceHandle":"..."}}
  {"op":"remove_edge","source":"...","target":"...","sourceHandle":"..."}
  {"op":"done","explanation":"两三句话说明这张图怎么跑","run":true}    ← 最后一行
- 按执行顺序添加节点（先入口后出口）；每加一个节点，立刻把连向它的边（两端都已存在的）输出出来，让图连贯地生长
- 修改现有图时只输出改动，没提到的节点不要动
- done 里的 run：这张图建完要不要立刻执行。默认 true；用户要的是**流程本身**
  （"设计一个每天跑的工作流"）时给 false —— 他要的是这张图，不是这一次的结果
- 如果这句话根本不需要工作流（见下面的路径约定），**第一行也是最后一行**就输出：
  {"op":"reply","text":"直接回答的内容"}
  这时不要输出任何别的操作"""


def _parse_op_line(line: str) -> dict[str, Any] | None:
    """解析一行操作。围栏行、空行、解析失败、缺 op 的一律返回 None——
    模型偶尔犯规不该毁掉整条流，跳过就是。"""
    text = line.strip()
    if not text or text.startswith("```"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "op" not in obj:
        return None
    return obj


def _apply_op(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], op: dict[str, Any]) -> bool:
    """把一个操作应用到服务端维护的图状态。返回是否真的改了图。"""
    kind = op.get("op")
    if kind == "add_node":
        node = op.get("node") or {}
        if not node.get("id") or not node.get("type"):
            return False
        nodes[node["id"]] = {
            "id": node["id"],
            "type": node["type"],
            "position": {"x": 0, "y": 0},
            "data": {"label": node.get("label") or "", "config": node.get("config") or {}},
        }
        return True
    if kind == "update_node":
        node = nodes.get(op.get("id") or "")
        if not node:
            return False
        if op.get("label") is not None:
            node["data"]["label"] = op["label"]
        if op.get("config") is not None:
            node["data"]["config"] = op["config"]
        return True
    if kind == "remove_node":
        node_id = op.get("id")
        if node_id not in nodes:
            return False
        nodes.pop(node_id)
        edges[:] = [e for e in edges if e["source"] != node_id and e["target"] != node_id]
        return True
    if kind == "add_edge":
        edge = op.get("edge") or {}
        if not edge.get("source") or not edge.get("target"):
            return False
        edges.append(
            {
                "source": edge["source"],
                "target": edge["target"],
                "sourceHandle": edge.get("sourceHandle"),
            }
        )
        return True
    if kind == "remove_edge":
        before = len(edges)
        edges[:] = [
            e
            for e in edges
            if not (
                e["source"] == op.get("source")
                and e["target"] == op.get("target")
                and (op.get("sourceHandle") is None or e.get("sourceHandle") == op.get("sourceHandle"))
            )
        ]
        return len(edges) != before
    return False


async def _iter_ops(model: Any, messages: list[Any]):
    """流式读模型输出，按行切出操作。done 之后即停——后面就算有闲话也不要了。

    除了操作，还把模型的思考原样转出去。原先这里只取正文、丢掉其余一切，
    于是从请求发出到第一个操作到达之间（实测 5~30 秒）前端一个字都收不到，
    只能干挂着"正在起草…"——用户没法判断是在想、还是已经卡死了。

    思考走 thinking_text()（Claude 4.6+ 的 thinking 块），不是"把非 JSON 行
    当成思考"：后者会把模型的 markdown 注释、协议跑偏时的乱输出一并当作思考
    展示出来，反而误导。不支持 thinking 的模型就只有心跳，不硬凑。
    """
    from app.engine.state import message_text as _text
    from app.engine.state import thinking_text

    buffer = ""
    async for chunk in model.astream(messages):
        reasoning = thinking_text(chunk)
        if reasoning:
            yield {"op": "thinking", "delta": reasoning}

        buffer += _text(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            op = _parse_op_line(line)
            if op:
                yield op
                if op.get("op") == "done":
                    return
    op = _parse_op_line(buffer)
    if op:
        yield op


# 多久没动静就补一拍心跳。3 秒是"用户开始怀疑是不是卡了"的量级。
_HEARTBEAT_SECONDS = 3.0


async def _with_heartbeat(source: Any):
    """模型沉默时按拍补心跳，让前端能显示阶段和已用时长。

    必须在这一层做而不是在 _iter_ops 里判断时间差：模型不吐 chunk 时那个
    async for 的循环体根本不执行，压根轮不到检查。
    """
    started = time.monotonic()
    phase = "planning"
    iterator = source.__aiter__()
    while True:
        pending = asyncio.ensure_future(iterator.__anext__())
        while True:
            done, _ = await asyncio.wait({pending}, timeout=_HEARTBEAT_SECONDS)
            if pending in done:
                try:
                    op = pending.result()
                except StopAsyncIteration:
                    return
                kind = op.get("op")
                # 阶段跟着实际操作走，前端据此换文案
                if kind == "add_node":
                    phase = "building"
                elif kind in ("add_edge", "connect"):
                    phase = "wiring"
                elif kind == "done":
                    phase = "finalizing"
                yield op
                break
            yield {
                "op": "heartbeat",
                "phase": phase,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }


@router.post("/generate-stream")
async def generate_stream(payload: GenerateIn, session: AsyncSession = Depends(get_session)):
    """流式版生成：SSE 逐操作推送，前端边收边把节点摆上画布。

    结束时（done 或流断）发一条 final：服务端把累积的图排版、校验后整体给出——
    过程可见性来自操作流，最终质量仍由和手工编排相同的 layout + validate 保证。
    """
    from fastapi.responses import StreamingResponse

    tool_list = await _tool_catalog(session)
    datasources = await _datasource_section(session)
    system = (
        "你是一个 agent 工作流编排专家。根据用户需求，以操作流的方式逐步搭出一张可执行的工作流图。\n\n"
        f"{NODE_REFERENCE}\n\n可用工具：\n{tool_list}{datasources}\n\n"
        "结构要求：\n"
        "1. 必须有且只有一个 input 节点和至少一个 output 节点\n"
        "2. 节点 id 用简短英文小写下划线；label 用中文，一眼看懂\n"
        "3. 用 assign_to 传结果，下游 {{ vars.变量名 }} 引用\n"
        "4. 只能用上面列出的工具名\n"
        "5. 能用 3 个节点解决就别堆 8 个\n\n"
        f"{_STREAM_PROTOCOL}"
    )

    # 现有图状态：修改场景从 base_graph 起步，新建场景从空图起步
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    if payload.base_graph and payload.base_graph.get("nodes"):
        for n in payload.base_graph["nodes"]:
            nodes[n["id"]] = n
        edges = [
            {k: v for k, v in e.items() if k in ("source", "target", "sourceHandle")}
            for e in payload.base_graph.get("edges") or []
        ]
        user = (
            "这是当前的工作流：\n"
            f"{json.dumps(_slim(payload.base_graph), ensure_ascii=False, indent=2)}\n\n"
            f"请按下面的要求修改它（只输出改动操作）：\n{payload.instruction}"
        )
    else:
        user = _user_message(payload, patch=False)

    try:
        spec_ = await copilot_model_spec(session, payload)
        model, model_id = await get_chat_model(session, spec_)
    except ProviderNotConfigured as e:
        raise HTTPException(400, str(e)) from e

    messages = [("system", system),
                ("human", _with_history(user, await _history_section(session, payload.conversation_id)))]
    # 兜底用：模型只发了改动操作时，把它们重放到这张图上
    prev_graph = await _previous_graph(session, payload.conversation_id)

    async def event_stream():
        explanation = ""
        seen_ops: list[dict[str, Any]] = []
        # 建完要不要跑，由模型在 done.run 里表态
        autorun = True
        yield f"data: {json.dumps({'op': 'model', 'model': model_id}, ensure_ascii=False)}\n\n"
        try:
            async for op in _with_heartbeat(_iter_ops(model, messages)):
                kind = op.get("op")
                # 思考和心跳不是图操作，不进 _apply_op，直接转给前端
                if kind in ("thinking", "heartbeat"):
                    yield f"data: {json.dumps(op, ensure_ascii=False)}\n\n"
                    continue
                if kind == "reply":
                    # 这句话不需要工作流。原样转给前端，然后收流——后面那套
                    # 排版校验对着一张空图跑，只会得到"图是空的，先拖一个节点进来"
                    yield f"data: {json.dumps(op, ensure_ascii=False)}\n\n"
                    return
                if kind == "done":
                    explanation = str(op.get("explanation", ""))
                    # 用户要的是流程本身时（"设计一个每天跑的工作流"），
                    # 建完就跑等于替他多花一次钱，而他要的是那张图
                    autorun = op.get("run") is not False
                seen_ops.append(op)
                changed = _apply_op(nodes, edges, op)
                if changed or kind in ("plan", "done"):
                    yield f"data: {json.dumps(op, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'op': 'error', 'message': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
            return

        # 兜底：一个操作都没落地，而上一轮有图。
        #
        # 这说明模型把这一轮当成"改上一张图"，只发了 update_node/add_edge，
        # 而累加器是从空图起步的，那些操作全落在不存在的节点上。上面的 prompt
        # 已经要求它完整重发，但那是约定，不是保证——约定落空的代价是用户收到
        # 一句"图是空的，先拖一个节点进来"，而他只是问了句追问。
        #
        # 与其报错，不如按它本来的意思做：把这些操作重放到上一轮那张图上。
        if not nodes and prev_graph and seen_ops:
            for n in prev_graph.get("nodes") or []:
                if n.get("id"):
                    nodes[n["id"]] = n
            # 原地改，不能重新绑定：给 edges 赋值会让它变成 event_stream 的局部
            # 变量，而它在上面 _apply_op 那一路已经被读过了 —— UnboundLocalError
            edges[:] = [
                {k: v for k, v in e.items() if k in ("source", "target", "sourceHandle") and v}
                for e in prev_graph.get("edges") or []
            ]
            for op in seen_ops:
                _apply_op(nodes, edges, op)

        # 收尾：排版 + 校验，把最终图整体交付
        try:
            spec = auto_layout(
                GraphSpec.model_validate({"nodes": list(nodes.values()), "edges": edges})
            )
            issues = [i.model_dump() for i in validate_graph(spec).issues]
            final = {
                "op": "final",
                "graph": spec.model_dump(mode="json"),
                "issues": issues,
                "explanation": explanation,
                "autorun": autorun,
            }
        except Exception as e:  # noqa: BLE001
            final = {"op": "error", "message": f"生成的图结构非法：{e}"}
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FromRunIn(BaseModel):
    run_id: str
    name: str = ""


@router.post("/from-run")
async def extract_template(
    payload: FromRunIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """轨迹 → 模板：把一次探索性运行实际走过的路径提取成草稿工作流。

    探索层的价值不在单次答案，而在"跑通的路径能沉淀为资产"。
    提取是确定性的：只保留真正执行过的节点和它们之间的边，分支上
    没走的岔路剪掉；输入值参数化成 input 字段默认值。产物落成 draft，
    人审、改名、发布之后才成为正式模板——提取器是模板的作者，不是发布者。
    """
    from sqlalchemy import select as _sel

    from app.api.copilot import auto_layout  # 自引用仅为显式
    from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
    from app.core.artifact_store import graph_hash

    run = await session.get(Run, payload.run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在")
    if not run.graph.get("nodes"):
        raise HTTPException(400, "这次运行没有保存图快照，无法提取")

    events = list(
        (
            await session.execute(
                _sel(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
            )
        ).scalars()
    )
    executed = {e.node_id for e in events if e.type == "node.started" and e.node_id}
    taken = {
        (e.node_id, str((e.data or {}).get("branch")))
        for e in events
        if e.type == "edge.taken" and e.node_id
    }
    if not executed:
        raise HTTPException(400, "事件流里没有节点执行记录")

    spec = GraphSpec.model_validate(run.graph)
    kept_nodes = [n for n in spec.nodes if n.id in executed]
    kept_ids = {n.id for n in kept_nodes}
    kept_edges = []
    for edge in spec.edges:
        if edge.source not in kept_ids or edge.target not in kept_ids:
            continue
        # 分支/循环节点：只保留实际走过的出口，没走的岔路不进模板
        source_node = spec.node_map().get(edge.source)
        if source_node and source_node.type in (NodeType.BRANCH,) and edge.sourceHandle:
            if (edge.source, edge.sourceHandle) not in taken:
                continue
        kept_edges.append(edge)

    # 输入参数化：实际输入值变成字段默认值
    for node in kept_nodes:
        if node.type.value == "input":
            node.data.config["fields"] = [
                {"name": key, "default": value if isinstance(value, (str, int, float)) else json.dumps(value, ensure_ascii=False)}
                for key, value in (run.input or {}).items()
            ] or node.data.config.get("fields", [])

    draft_spec = auto_layout(
        GraphSpec(nodes=kept_nodes, edges=kept_edges, defaults=spec.defaults)
    )
    graph = draft_spec.model_dump(mode="json")
    workflow = Workflow(
        name=payload.name or f"{run.workflow_name or '探索'}·提取模板",
        description=f"从运行 {run.id[:8]} 的实际执行路径提取（{len(kept_nodes)} 节点），待人审后发布",
        graph=graph,
        tags=["extracted"],
        status="draft",
    )
    session.add(workflow)
    await session.flush()
    session.add(
        WorkflowVersion(
            workflow_id=workflow.id, version=1, graph=graph,
            graph_hash=graph_hash(graph), note=f"自运行 {run.id} 提取",
        )
    )
    await session.commit()
    return {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "nodes": len(kept_nodes),
        "edges": len(kept_edges),
        "dropped_nodes": len(spec.nodes) - len(kept_nodes),
        "source_run": run.id,
    }


class ExplainIn(BaseModel):
    graph: dict[str, Any]
    provider: str | None = None
    model: str | None = None


@router.post("/explain")
async def explain(
    payload: ExplainIn, session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    """用大白话讲清楚一张图在干什么。接手别人的编排时很有用。"""
    try:
        model, _ = await get_chat_model(
            session,
            await copilot_model_spec(
                session,
                GenerateIn(instruction="_", provider=payload.provider, model=payload.model),
                max_tokens=2048,
            ),
        )
    except ProviderNotConfigured as e:
        raise HTTPException(400, str(e)) from e

    prompt = (
        "用中文解释下面这个 agent 工作流：它解决什么问题、数据怎么流动、"
        "有哪些分支和风险点。用简洁的分点说明，不要复述 JSON。\n\n"
        f"{json.dumps(_slim(payload.graph), ensure_ascii=False, indent=2)}"
    )
    return {"explanation": message_text(await model.ainvoke(prompt))}


@router.post("/layout")
async def relayout(payload: ExplainIn) -> dict[str, Any]:
    """一键整理画布。"""
    spec = GraphSpec.model_validate(payload.graph)
    return auto_layout(spec).model_dump(mode="json")


def _slim(graph: dict[str, Any]) -> dict[str, Any]:
    """喂给模型时去掉坐标之类的噪音，省 token 也省得它被干扰。"""
    return {
        "nodes": [
            {
                "id": n.get("id"),
                "type": n.get("type"),
                "label": (n.get("data") or {}).get("label"),
                "config": (n.get("data") or {}).get("config"),
            }
            for n in graph.get("nodes") or []
        ],
        "edges": [
            {k: v for k, v in e.items() if k in ("source", "target", "sourceHandle") and v}
            for e in graph.get("edges") or []
        ],
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1 : -1 if lines[-1].strip().startswith("```") else None])
    start = text.find("{")
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    raise ValueError("模型输出里找不到 JSON")
