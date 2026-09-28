from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import DATASOURCE_SCOPE_EMPTY, CodedHTTPException
# 起别名：本文件底下有个叫 explain 的接口，generate 里又有个局部变量叫 raw，同名会互相盖掉
from app.core.errors import explain as explain_error, graph_error, not_configured, raw as raw_error
from app.db.base import get_session
from app.db.models import Workflow
from app.engine.layout import CORRIDOR_MIN, MIN_ROW_GAP, NODE_W, _height, auto_layout
from app.engine.schema import GraphSpec, NodeType, ValidationIssue, validate_graph
from app.engine.state import message_text
from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model
from app.tools.registry import all_specs

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


async def _sources(session: AsyncSession, scope: list[str] | None) -> list[Any]:
    """这一轮给助手看的数据源：启用着的，限定了范围就只留点名的（id、名字都认）。

    点名的一个都不在（删了、停用了）就直接拒绝：悄悄退回「全部数据源」等于
    无视用户的限定，而他正是因为想换一个库问才点的。
    """
    from app.db.models import DataSource

    rows = list((
        await session.execute(
            select(DataSource).where(DataSource.enabled.is_(True)).order_by(DataSource.name)
        )
    ).scalars())
    wanted = {s.strip() for s in scope or [] if isinstance(s, str) and s.strip()}
    if not wanted:
        return rows
    picked = [r for r in rows if r.id in wanted or r.name in wanted]
    if not picked:
        raise CodedHTTPException(
            400, "限定的数据源都不在了：可能已经被删掉或停用。去掉限定再问，"
                 "或者到「数据」页确认它还在、而且是启用的",
            DATASOURCE_SCOPE_EMPTY,
        )
    return picked


def _scope_note(rows: list[Any], scoped: bool) -> str:
    if not scoped:
        return ""
    names = "、".join(r.name for r in rows)
    return (f"\n\n这一轮用户把数据源限定为：{names}。只查它们，只能用它们的 db_query__ / "
            "db_schema__ 工具；前几轮用过别的库，这一轮也不要沿用")


def _tool_catalog(rows: list[Any]) -> str:
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
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX

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


def _datasource_section(rows: list[Any]) -> str:
    """数据源与结构摘要。没有数据源时返回空串，不占 prompt。"""
    from app.data import introspect as _introspect

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
        "- 取数结果要进口径卡（metrics 节点）才能被报告撰写节点（report）引用，别让 llm 节点直接对数字做算术\n"
    )


NODE_REFERENCE = """\
可用节点类型（type 字段）：
- input：入口。config.fields = [{name, required, default, description}]
- output：出口，收集最终成果。config.fields = [{name, value}]，value 里写模板引用
- llm：单次模型调用。config: {system, prompt, model, temperature, max_tokens, assign_to, output_schema}
- agent：带工具循环的 agent。config: {system, prompt, tools:[工具名], approval, assign_to}
  **默认不要写 max_steps**。平台默认值是按「一步只调一个工具」校准过的；写死一个
  小数字（见过 8）会让它查完表结构就没额度回答了，用户拿到的是半截结论。
  唯一的例外：上一次就是因为**步数用尽**没跑完（会在改写理由里写明），
  这时要显式写一个更大的值（比如 24）——那是这种失败唯一的修法
- supervisor：多 agent 协作。config: {goal, agents:[{name, description, system, tools, model}], max_rounds}
- tool：直接调一个工具。config: {tool: 工具名, args: {...}, assign_to}
- code：沙箱里跑代码。config: {language: python|bash|node, code, timeout, network, assign_to}
  assign_to 拿到的是 stdout（尾部的换行已去掉）。**stdout 是 JSON 时会解析成对象**，
  这时下游要用 {{ vars.x.字段 }} 取字段，拿它比字符串永远不成立。要判一个简单结论，
  就 print 一个短字符串（比如 print('ok')）并让下游比它。
  **结果必须 print 出来**：脚本最后一行写个裸表达式（state、{"a": 1}）不会输出，那是
  notebook 的行为——assign_to 会拿到空值。要交给下游一个对象，就 print(json.dumps(结果))
- branch：条件分支。config: {mode: expression|llm, cases:[{key, condition, label}]}
- loop：循环。config: {mode: foreach|while, items, item_var, condition, max_iterations}
- retrieve：知识库检索。config: {query, collection, limit, assign_to}
- memory：长期记忆读写。config: {action: recall|write, query, content, assign_to}
  **不要写 scope**。记忆域由平台给（当前统一是 default）。自己起一个域的后果是
  写进去再也读不回来——真发生过：一轮写进 scope"user"，下一轮没写 scope 落回
  default，于是「我叫张三」存下来了，问「我是谁」却答不上来
- human：人工审批。config: {mode: approve|input|edit, title, message}
- validate：JSON Schema 校验，可自动让模型修复。config: {schema, source, max_retries}
- transform：数据整形。config: {mode: expression|template|json, expression/template, assign_to}
- subgraph：嵌套另一个工作流。config: {workflow_id, input}
- metrics：口径卡。报告里要出现的数字、以及所有派生计算（比率、增幅、占比、差值）都登记在这里。
  config: {caliber, caliber_version, metrics:[{id, name, unit, decimals, format, expression}], on_missing, assign_to}
  expression 是受限表达式（不是模板）：vars.q.rows[0][0]、round((vars.q.rows[0][0] - vars.q.rows[0][1]) / vars.q.rows[0][1] * 100, 1)
  format：thousands（默认，千分位）/ plain（不分组，年份编号这类）/ percent_of_ratio（值是 0.0235 这样的比率，显示成 2.35%）；
  decimals 写 -15 到 15 的整数；percent_of_ratio 的 decimals 按比率算：要显示两位百分比（2.35%）写 4，
  写 2 只剩「2%」，0.004 这样的小比率还会显示不出来、报告引用不了。on_missing 默认 fail（缺输入整张卡失败）；写 null 则缺输入的指标记为空值，交给出具契约判档
  db_query__* 返回的是 JSON 文本 {columns, rows}，口径卡取不到里面的字段：先接一个
  transform（mode=json，template="{{ nodes.取数节点id }}"，assign_to="q"），口径卡再写 vars.q.rows[0][0]
- report：报告撰写（带引用）。config: {instructions, system, metrics_from:[口径卡id], on_violation, max_repairs, assign_to}
  **凡是给人看的、带数字的报告、分析、结论，都用 report，不要用 llm**。它自动收集上游口径卡的指标和运行输入，
  模型只能写 [[m:指标id]]、[[i:输入字段]] 这样的引用标记，数值由系统换成口径卡里的真实值；
  模型自己写的数字会被打回重写。它只能引用口径卡指标和运行输入，所以报告里要出现的每个数都得先在口径卡里登记。
  metrics_from 不写就取所有上游口径卡；on_violation 不写时探索运行是 flag（标出来、照常产出），正式运行是 fail
- output 的出具契约：config.contract = {report_from: 报告节点id, metrics_from:[口径卡id],
  required:[必需指标id], expected:[期望指标id], strict: true}。成果字段写 {{ nodes.报告节点id.text }}，
  前后不要拼别的字——拼了就没法逐段对应证据，出具会降档

搭图规则（有数字结论时必须遵守）：
1. 取数（tool 节点查库，接 transform 解析）→ 口径卡 → report → output（配 report_from 契约）。
   agent 的回答是自由文本，进不了口径卡：要出可追溯的报告，取数用 tool 节点
2. 能用 SUM / COUNT 做的聚合放在 SQL 里，口径卡只做标量运算
3. 不要用 code 节点做业务计算；code 只做格式转换

模型字段（llm / agent / supervisor / report 的 config.model）：
- **不要填**。留空表示跟随当前供应商的默认模型，这几乎总是对的。
- 你不知道这套部署里有哪些 model id——凭供应商名字猜（比如看到 DeepSeek 就写
  "deepseek-chat"）会得到 401 invalid_model，整个节点跑不起来。
- 只有用户在需求里明确点名了某个模型，才把他说的那个字符串原样填进去。

模板语法（在 prompt、template、args、message、query 这类**普通文本字段**里用）：
- {{ input.字段名 }}        入口输入
- {{ vars.变量名 }}         某节点 assign_to 写入的变量
- {{ nodes.节点id.text }}   某节点的输出
- {{ last_message }}        最近一条消息文本
- 过滤器：{{ vars.x | json }}

**表达式字段不是模板，不要套 {{ }}**：branch 的 cases[].condition、loop（while 模式）的
condition、transform（expression 模式）的 expression、口径卡的 metrics[].expression、任意节点的
skip_if。这些字段按受限的 Python 表达式求值，变量直接写路径：
- 对：vars.gate == "ok"            错：{{ vars.gate }} == "ok"（会被当成集合，跑不起来）
- 对：vars.state.count <= 1000     错：'{{ vars.x }}' == "ok"（引号里的模板不会渲染，永远不成立）
- 对：len(vars.items) > 0          错：{{ vars.items | length }} > 0（表达式里没有 | 过滤器）
- 能用：and / or / not、== != < <= > >=、in [列表]、x if 条件 else y；函数只有 len str int float
  round min max sum any all lower upper contains startswith endswith matches get
- 字符串加引号；判断相等写 ==；"是不是其中之一"用列表 x in ['a', 'b']，不要用 {…}

图搭完会用和运行时同一套规则自查：表达式写错、必填没填、引用了不存在的变量，都会被打回来让你改。

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


class GenerateIn(BaseModel):
    instruction: str = Field(min_length=1, description="用自然语言描述想要的工作流")
    base_graph: dict[str, Any] | None = Field(default=None, description="在现有图上修改时传入")
    provider: str | None = None
    model: str | None = None
    #: build = 用户在描述一个流程（画布）；answer = 用户在问一个问题（问数据页）
    intent: str = Field(default="build")
    #: 属于哪次对话。带上它，这一轮才知道前面聊过什么
    conversation_id: str | None = None
    #: 这一轮只查这几个数据源（id 或名字）。不传或空表示不限
    datasource_ids: list[str] | None = None


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
            # 报告撰写节点只能引用口径卡里登记过的数。agent 查到的结论还进不了口径卡，
            # 普通问数也套上 report 的话，答案里的数会全部被判成没有出处
            "- 用户要的是报告、周报，或者要能核对每个数的出处时，按搭图规则走 tool 取数 → 口径卡 → "
            "report → output（配 report_from 契约）；普通的问数照旧用 agent，不必加 report\n"
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
            "\n"
            # 以前只有用户明说"记住"才会写记忆：17 轮里只有 4 轮带记忆节点，
            # 全在用户主动谈记忆的那个会话里。于是他说过一次"我叫张三"，
            # 换个话题再问就谁也不知道了
            "顺带判断这句话里有没有**值得长期记住**的东西。有就在主链路前面加一个\n"
            "memory 节点（action=write，紧跟 input），没有就别加：\n"
            "- 该记：用户是谁、怎么称呼、长期偏好；口径和约定（「我们说的活跃指"
            "当月有订单」）；用户的纠正（「不对，管理员要按 platform 算」）；"
            "稳定的环境事实（「后端跑在 8001」）\n"
            "- 不该记：这一轮的问题本身；查出来的数和结果（会过期，而且能重查）；"
            "一次性的指令（「重来」「继续」）；**用户没说过、你推测出来的东西**\n"
            "- content 要写成一句脱离上下文也成立的陈述——召回时没有这轮对话，"
            "「他说他叫张三」这种读起来就不知道「他」是谁\n"
            "- 拿不准就不记。记错了会污染以后每一次对话，而漏记一条，用户再说一遍就是了\n"
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


def _unconfigured(e: ProviderNotConfigured) -> str:
    return f"助手用的模型还没配好：{not_configured(e)}。到「设置 → 模型接入」检查一下"


class GenerateOut(BaseModel):
    graph: dict[str, Any]
    explanation: str = ""
    issues: list[dict[str, Any]] = Field(default_factory=list)
    layout: dict[str, Any] = Field(default_factory=dict)
    #: 改图前后都在、工具绑定却变了的节点和成员，见 tool_changes()
    tool_changes: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 改图时的排版：用户摆好的位置不动，只给新节点找空处
# --------------------------------------------------------------------------

#: 新节点和旧节点之间至少留的空。比排版的走廊窄一点：这里只求不压住、线有地方走，
#: 不求和整体排版一样舒展——那是「自动排版」按钮的事
_KEEP_GAP_X = 40.0
_KEEP_GAP_Y = 24.0
#: 同一列里上下各找几格，再找不到就往右挪一列
_KEEP_ROWS = 8
_KEEP_COLS = 6


def _pinned_positions(base_graph: dict[str, Any] | None) -> dict[str, tuple[float, float]]:
    """base_graph 里用户摆好的坐标。

    全堆在同一点的（接口直接传进来、从没排过版的图）不算摆过，照常整体排版——
    钉住它们只会让一叠节点永远叠着。
    """
    pos: dict[str, tuple[float, float]] = {}
    for n in (base_graph or {}).get("nodes") or []:
        p = n.get("position") or {}
        x, y = p.get("x"), p.get("y")
        if n.get("id") and isinstance(x, (int, float)) and isinstance(y, (int, float)):
            pos[n["id"]] = (float(x), float(y))
    if len(pos) > 1 and len(set(pos.values())) == 1:
        return {}
    return pos


def _layout_keeping(
    spec: GraphSpec, pinned: dict[str, tuple[float, float]],
) -> tuple[GraphSpec, dict[str, Any]]:
    """已有节点留在原处，新节点按上下游插到最近的空位。

    以前改图无条件 auto_layout 整张图：只改了一句提示词，所有节点也被挪到排版
    位置。人工整理过的布局常带着业务含义（上游取数、口径计算、出具各占一块），
    被打乱之后用户就不敢再让助手改图了。新建（没有旧坐标）照旧整体排版。
    """
    kept = [n for n in spec.nodes if n.id in pinned]
    if not kept:
        auto_layout(spec)
        return spec, {"mode": "full", "placed": [n.id for n in spec.nodes]}

    # 先整体排一遍，只借它的先后次序：上游的新节点先落位，下游才有东西可挨着
    auto_layout(spec)
    ideal = {n.id: (n.position.x, n.position.y) for n in spec.nodes}
    fresh = sorted((n for n in spec.nodes if n.id not in pinned), key=lambda n: ideal[n.id])

    placed: dict[str, tuple[float, float, float, float]] = {}
    for n in kept:
        n.position.x, n.position.y = pinned[n.id]
        placed[n.id] = (n.position.x, n.position.y, NODE_W, _height(n))

    def free(x: float, y: float, h: float) -> bool:
        return all(
            not (x < bx + bw + _KEEP_GAP_X and bx < x + NODE_W + _KEEP_GAP_X
                 and y < by + bh + _KEEP_GAP_Y and by < y + h + _KEEP_GAP_Y)
            for bx, by, bw, bh in placed.values()
        )

    step_x = NODE_W + CORRIDOR_MIN
    for node in fresh:
        h = _height(node)
        preds = [placed[e.source] for e in spec.edges if e.target == node.id and e.source in placed]
        succs = [placed[e.target] for e in spec.edges if e.source == node.id and e.target in placed]
        if preds:
            x0 = max(b[0] for b in preds) + step_x
            y0 = sum(b[1] for b in preds) / len(preds)
        elif succs:
            x0 = min(b[0] for b in succs) - step_x
            y0 = sum(b[1] for b in succs) / len(succs)
        else:
            # 和谁都不连：放到整张图右边，不插进别人的分区里
            x0 = max(b[0] + b[2] for b in placed.values()) + CORRIDOR_MIN
            y0 = min(b[1] for b in placed.values())

        spot = None
        row = h + MIN_ROW_GAP
        for col in range(_KEEP_COLS):
            x = x0 + col * step_x * (1 if preds or not succs else -1)
            for k in range(_KEEP_ROWS * 2 + 1):
                # 0, +1, -1, +2, -2 …：先试正对着上下游的那一格
                y = y0 + ((k + 1) // 2) * row * (1 if k % 2 else -1)
                if free(x, y, h):
                    spot = (x, y)
                    break
            if spot:
                break
        if spot is None:
            # 周围实在挤满了：放到整张图最下面，至少不压住任何东西
            spot = (x0, max(b[1] + b[3] for b in placed.values()) + MIN_ROW_GAP)
        node.position.x, node.position.y = round(spot[0], 1), round(spot[1], 1)
        placed[node.id] = (node.position.x, node.position.y, NODE_W, h)

    return spec, {"mode": "keep", "placed": [n.id for n in fresh]}


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
    sources = await _sources(session, payload.datasource_ids)
    scope = {r.name for r in sources} if payload.datasource_ids else None
    tool_list = _tool_catalog(sources)
    datasources = _datasource_section(sources) + _scope_note(sources, scope is not None)

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
        raise HTTPException(400, _unconfigured(e)) from e

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
            reason, _ = explain_error(e)
            raise HTTPException(
                502, f"模型这次没交回能用的工作流（{reason}）。换个说法再试一次；"
                     "总是这样的话，到设置里给助手换一个更听指令的模型",
            ) from e

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
        raise HTTPException(
            502, f"模型交回的工作流结构不对：{graph_error(e)}。再试一次，或者把需求说得更具体些",
        ) from e

    spec, layout = _layout_keeping(spec, _pinned_positions(payload.base_graph))
    report = validate_graph(spec)
    out = spec.model_dump(mode="json")
    changes = tool_changes((payload.base_graph or {}).get("nodes") or [], out["nodes"])
    return GenerateOut(
        graph=out,
        explanation=str(raw.get("explanation", "")),
        issues=[i.model_dump() for i in report.issues]
        + _issue_dicts(scope_issues(out["nodes"], scope), "datasource_out_of_scope")
        + dropped_tool_warnings(changes, payload.instruction),
        layout=layout,
        tool_changes=changes,
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
  {"op":"update_node","id":"...","label":"...","config":{...}}   ← 修改现有节点（config 只写要改的字段）
  {"op":"remove_node","id":"..."}
  {"op":"add_edge","edge":{"source":"...","target":"...","sourceHandle":"..."}}
  {"op":"remove_edge","source":"...","target":"...","sourceHandle":"..."}
  {"op":"done","explanation":"两三句话说明这张图怎么跑","run":true}    ← 最后一行
- 按执行顺序添加节点（先入口后出口）；每加一个节点，立刻把连向它的边（两端都已存在的）输出出来，让图连贯地生长
- 修改现有图时只输出改动，没提到的节点不要动
- update_node 按字段合并：config 只写要改的字段，没写的（tools、assign_to…）原样保留；
  要删掉某个字段就写成 null。supervisor 的 agents 按成员 name 合并：只写要改的成员和字段，
  成员没写 tools 就沿用它原来的工具；新名字是新增成员；去掉成员写 {"name":"…","remove":true}
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


_NODE_TYPES = frozenset(t.value for t in NodeType)

#: 协作成员条目里写 "remove": true 就把这个成员拿掉。不复用 null：null 在合并里
#: 的意思是「删这个字段」，成员本身不是一个字段
_REMOVE_MEMBER = "remove"


def _member_key(member: dict[str, Any], index: int) -> str:
    # 和 engine/nodes/multi.py 的叫法一致：没起名字的成员按位置叫 agentN
    return str(member.get("name") or f"agent{index}")


def _merge_fields(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """按字段合并：写了的覆盖，写 null 的删掉，没写的原样保留。返回新 dict。"""
    out = dict(old)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _merge_members(old: list[Any], patch: list[Any]) -> list[Any]:
    """协作成员按 name 合并。原有成员保持原来的次序，新名字接在后面。"""
    merged: list[Any] = list(old)
    where = {_member_key(m, i): i for i, m in enumerate(merged) if isinstance(m, dict)}
    dropped: set[int] = set()
    for i, entry in enumerate(patch):
        if not isinstance(entry, dict):
            continue
        key = _member_key(entry, i)
        if entry.get(_REMOVE_MEMBER) is True:
            if key in where:
                dropped.add(where[key])
            continue
        fields = {k: v for k, v in entry.items() if k != _REMOVE_MEMBER}
        if key in where:
            merged[where[key]] = _merge_fields(merged[where[key]], fields)
        else:
            where[key] = len(merged)
            merged.append({k: v for k, v in fields.items() if v is not None})
    return [m for i, m in enumerate(merged) if i not in dropped]


def merge_node_config(
    node_type: str, old: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """update_node 的 config 怎么落到节点上。前端 store/studio.ts 必须是同一套规则。

    以前是整体替换：模型只想改一句提示词，照协议也得把整份 config 抄一遍，
    漏抄的字段就没了。真出过事——改写提示词时漏了 tools，三个要查库的 agent
    全丢了工具，一次库都没查，最后把一堆工具调用的原始标记当答案交了出去。
    改成按字段合并之后，漏写等于不改；真要删一个字段得显式写 null。
    """
    if node_type == NodeType.SUPERVISOR.value and isinstance(patch.get("agents"), list):
        members = old.get("agents") if isinstance(old.get("agents"), list) else []
        patch = {**patch, "agents": _merge_members(members, patch["agents"])}
    return _merge_fields(old, patch)


def _apply_op(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], op: dict[str, Any]) -> bool:
    """把一个操作应用到服务端维护的图状态。返回是否真的改了图。"""
    kind = op.get("op")
    if kind == "add_node":
        node = op.get("node") or {}
        # 类型不认识的节点不进图、也不转给前端：前端按类型查节点定义，查不到
        # 以前是整站白屏，连带没保存的编辑一起丢
        if not node.get("id") or node.get("type") not in _NODE_TYPES:
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
        data = node.setdefault("data", {})
        if op.get("label") is not None:
            data["label"] = op["label"]
        if isinstance(op.get("config"), dict):
            data["config"] = merge_node_config(
                str(node.get("type") or ""), data.get("config") or {}, op["config"])
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


async def _with_heartbeat(source: Any, phase: str = "planning"):
    """模型沉默时按拍补心跳，让前端能显示阶段和已用时长。

    必须在这一层做而不是在 _iter_ops 里判断时间差：模型不吐 chunk 时那个
    async for 的循环体根本不执行，压根轮不到检查。
    """
    started = time.monotonic()
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


#: 自查最多交回去改几轮。一轮只花一次"只输出改动"的调用；两轮还改不好，多半是
#: 需求本身有歧义，该让人看了，而不是继续替他烧钱
_SELF_CHECK_ROUNDS = 2


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _blocking_issues(
    nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], scope: set[str] | None = None,
) -> list[dict[str, Any]] | None:
    """按运行时同一套校验，挑出会挡住运行的问题。图本身不成形时返回 None。

    限定了数据源时，用了范围外的库也算：用户点了「只查这个库」，拿别的库的数
    回答他，比跑不起来更糟——答案看起来是对的。

    每条是 ValidationIssue 的字典形状（node_id、field 都在），和 final.issues 一样：
    界面靠 node_id 定位卡片、靠 field 落到具体的输入框，不用从一行字里抠节点 id。
    """
    try:
        spec = GraphSpec.model_validate({"nodes": list(nodes.values()), "edges": edges})
    except Exception:  # noqa: BLE001 - 结构不合法留给收尾那一步报
        return None
    return ([i.model_dump() for i in validate_graph(spec).issues if i.level == "error"]
            + _issue_dicts(scope_issues(list(nodes.values()), scope), "datasource_out_of_scope"))


def _source_of(tool: str) -> str | None:
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX

    for prefix in (QUERY_PREFIX, SCHEMA_PREFIX):
        if tool.startswith(prefix):
            return tool[len(prefix):]
    return None


def scope_issues(nodes: list[dict[str, Any]], scope: set[str] | None) -> list[ValidationIssue]:
    """用了限定范围之外的数据源的节点，一个节点一条。"""
    if scope is None:
        return []
    out: list[ValidationIssue] = []
    for node in nodes:
        cfg = (node.get("data") or {}).get("config") or {}
        names = _tool_names(cfg.get("tools")) + _tool_names([cfg.get("tool")])
        for member in cfg.get("agents") or []:
            if isinstance(member, dict):
                names += _tool_names(member.get("tools"))
        outside = sorted({src for src in map(_source_of, names) if src and src not in scope})
        if outside:
            out.append(ValidationIssue(
                level="error", node_id=node.get("id"),
                message=f"用了限定范围之外的数据源 {'、'.join(outside)}：这一轮只查 "
                        f"{'、'.join(sorted(scope))}，改用它们的 db_query__ / db_schema__ 工具",
            ))
    return out


def _issue_dicts(issues: list[ValidationIssue], code: str) -> list[dict[str, Any]]:
    return [{**i.model_dump(), "code": code} for i in issues]


def _issue_line(issue: dict[str, Any]) -> str:
    """交回模型的修正请求里，一条问题一行，节点 id 在前。"""
    return f"「{issue['node_id']}」{issue['message']}" if issue.get("node_id") else issue["message"]


# --------------------------------------------------------------------------
# 工具绑定变化：改图前后，每个 agent 和每个协作成员各绑了哪些工具
# --------------------------------------------------------------------------


def _tool_names(value: Any) -> list[str]:
    return [t for t in value if isinstance(t, str) and t] if isinstance(value, list) else []


def _tool_bindings(nodes: list[dict[str, Any]]) -> dict[tuple[str, str | None], dict[str, Any]]:
    """(节点 id, 成员名) → {label, tools, field}。成员名为 None 的是 agent 节点本身。"""
    out: dict[tuple[str, str | None], dict[str, Any]] = {}
    for node in nodes:
        data = node.get("data") or {}
        cfg = data.get("config") or {}
        label = data.get("label") or node.get("id")
        if node.get("type") == NodeType.AGENT.value:
            out[(node["id"], None)] = {"label": label, "field": "tools",
                                       "tools": _tool_names(cfg.get("tools"))}
        elif node.get("type") == NodeType.SUPERVISOR.value:
            for i, member in enumerate(cfg.get("agents") or []):
                if isinstance(member, dict):
                    out[(node["id"], _member_key(member, i))] = {
                        "label": label, "field": f"agents[{i}].tools",
                        "tools": _tool_names(member.get("tools")),
                    }
    return out


def tool_changes(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """改图前后都在、而绑定的工具不一样了的节点和成员，按改后的图排序。

    新加的、删掉的节点不算：那是结构变化，改图回执本来就会列出来。这里要抓的
    是「节点还在、工具悄悄没了」——画布上看起来什么都没变，跑起来才发现查不了库。
    """
    old = _tool_bindings(before)
    out: list[dict[str, Any]] = []
    for (node_id, member), now in _tool_bindings(after).items():
        was = old.get((node_id, member))
        if was is None or set(was["tools"]) == set(now["tools"]):
            continue
        out.append({
            "node_id": node_id, "label": now["label"], "member": member, "field": now["field"],
            "before": was["tools"], "after": now["tools"],
            "added": [t for t in now["tools"] if t not in was["tools"]],
            "removed": [t for t in was["tools"] if t not in now["tools"]],
        })
    return out


# 指令里有没有让删工具。只认「删除动词直接管着工具」：动词和「工具 / 工具名」
# 挨着、在同一个分句里、中间没有夹别的动作。以前是整句里有「不要 / 不用 / 取消」、
# 别处又有「工具」就算——「这次要真的用工具查库，不要编造数字」被当成让删工具，
# 警告关掉，没工具的图照样自动运行。宁可在真让删时多提醒一句，也不能漏掉这种。

#: 删除动词。「不要 / 不用 / 别」只在直接接着工具（或接「用 / 绑」再接工具）时才算
_DROP_VERB = re.compile(
    r"去掉|去除|删掉|删除|删了|删去|移除|拿掉|撤掉|撤下|解绑|解除绑定|禁用|停用|卸掉|剔除|砍掉|清空|取消"
    r"|(?:不要|不用|不需要|无需|用不着|不再|别)再?(?:使用|调用|绑定|用|绑|挂)?"
    r"|\b(?:remov(?:e|ing)|drop(?:ping)?|unbind|detach|disable|without|get rid of|no longer use)\b"
    r"|\b(?:don't|do not|never|stop) us(?:e|ing)\b"
)
#: 工具在前、动词在后：「把查表的工具去掉」「db_schema__shop 不要了」
_DROP_AFTER = re.compile(
    r"去掉|去除|删掉|删除|删了|删去|移除|拿掉|撤掉|撤下|解绑|禁用|停用|卸掉|剔除|砍掉|清空"
    r"|(?:不要|不用|不需要|用不着)再?(?:用|绑)?(?:了|$)|\b(?:removed|dropped)\b"
)
#: 「只保留 X」：别的可以删，但 X 本身不能跟着没了，更不能删光。「只用」只认点了名的——
#: 「只用工具查库」说的是别编，不是让删哪个工具
_KEEP_ONLY = re.compile(r"(只保留|只留下|只留|仅保留|\bkeep only\b|\bonly keep\b)"
                        r"|(只用|仅用|\bonly use\b)")
#: 动词和工具之间夹了这些，动词管的就不是工具了：「不用改工具」「别用假设的方式调用工具」
_GAP_BREAK = re.compile(
    r"改|动|换|变成|编|捏造|虚构|假设|假装|模拟|猜|然后|接着|并且|同时|但|而是|让|把|将|保留|留着|保持|只|必须|一定"
    r"|make up|made up|making up|\b(?:then|but|instead|change|edit|modify|keep|fake|invent|pretend)\b"
)
#: 否定。中文只往前看几个字（「不要去掉工具」「别把工具删了」），英文的否定离动词远，看整个分句
_NEGATED = re.compile(r"不要|不用|不能|不许|不准|不得|不可|不必|不想|不希望|不让|无需|没让|别|勿|千万")
_NEGATED_EN = re.compile(r"\b(?:don't|dont|do not|never|no need|not|can't|cannot)\b")
#: 工具后面紧跟着这些，说的是留着它：「工具保留」「工具不用改」
_KEPT_AFTER = re.compile(r"\s*(?:都|也|就|还|先)?(?:保留|留着|不变|照旧|保持|别动|不动|不要动|不用动|"
                         r"不要改|不用改|别改|别删|不要删|不能删)|\s*(?:stay|unchanged|as is)\b")
_CLAUSE = re.compile(r"[，,。;；！!？?\n]|\.(?=\s|$)")
_GENERIC = ("工具", "tool", "tools")
_GAP, _GAP_AFTER, _LOOKBACK = 16, 8, 10


def _tool_object(removed: list[str]) -> re.Pattern[str]:
    """「工具」或一个工具名：这次删掉的、内置的，以及带 __ 的（db_query__x、MCP 工具）。

    只认像工具的名字：表名、字段名也是下划线写法，「去掉 sales_daily 相关的工具」
    里点名的不是工具。
    """
    names = sorted({t.lower() for t in [*removed, *all_specs()] if t}, key=len, reverse=True)
    return re.compile("|".join([*map(re.escape, names), r"[a-z][a-z0-9_]*__[a-z0-9_-]+",
                                "工具", r"\btools?\b"]))


def _negated(clause: str, pos: int) -> bool:
    return bool(_NEGATED.search(clause, max(0, pos - _LOOKBACK), pos)
                or _NEGATED_EN.search(clause, 0, pos))


def _drop_request(instruction: str, removed: list[str]) -> tuple[bool, set[str], set[str] | None]:
    """指令里让删了哪些工具：(泛指「工具」, 点了名的, 「只保留」留下的——没说只保留是 None)。"""
    target = _tool_object(removed)
    dropped: set[str] = set()
    kept: set[str] | None = None

    def near(clause: str, start: int) -> re.Match[str] | None:
        obj = target.search(clause, start)
        return obj if obj and obj.start() - start <= _GAP else None

    for clause in _CLAUSE.split((instruction or "").lower()):
        for verb in _DROP_VERB.finditer(clause):
            obj = near(clause, verb.end())
            if not obj:
                continue
            gap = clause[verb.end():obj.start()]
            if (_GAP_BREAK.search(gap) or _DROP_VERB.search(gap) or _negated(clause, verb.start())
                    or _KEPT_AFTER.match(clause, obj.end())):
                continue
            dropped.add(obj.group())
        for obj in target.finditer(clause):
            verb = _DROP_AFTER.search(clause, obj.end())
            if not verb or verb.start() - obj.end() > _GAP_AFTER:
                continue
            gap = clause[obj.end():verb.start()]
            if _GAP_BREAK.search(gap) or _NEGATED.search(gap) or _negated(clause, obj.start()):
                continue
            dropped.add(obj.group())
        for verb in _KEEP_ONLY.finditer(clause):
            obj = near(clause, verb.end())
            if obj and not (verb.group(2) and obj.group() in _GENERIC):
                kept = (kept or set()) | {obj.group()}
    return bool(dropped & set(_GENERIC)), dropped - set(_GENERIC), kept


def _unasked_drops(instruction: str, change: dict[str, Any]) -> list[str]:
    """这次少掉的工具里，哪些是指令没让删的。"""
    generic, named, kept = _drop_request(instruction, change["removed"])
    if generic:
        return []

    def asked(tool: str) -> bool:
        if tool.lower() in named:
            return True
        # 「只保留 X」：X 以外的可以删；但一个都不剩就不是「保留」了
        return kept is not None and bool(change["after"]) and tool.lower() not in kept

    return [t for t in change["removed"] if not asked(t)]


def dropped_tool_warnings(changes: list[dict[str, Any]], instruction: str) -> list[dict[str, Any]]:
    """工具集合变小了、而这一轮没让删工具的，逐个报 warning。

    只报变小的：把 db_query__a 换成 db_query__b 是正常的改图，数量没少不必大惊
    小怪。也不打回去让模型重改——删工具有可能是对的，该让人看一眼，而不是再
    赌一次模型。
    """
    out: list[dict[str, Any]] = []
    for c in changes:
        if not c["removed"] or len(set(c["after"])) >= len(set(c["before"])):
            continue
        unasked = _unasked_drops(instruction, c)
        if not unasked:
            continue
        who = f"「{c['label']}」" + (f"的成员「{c['member']}」" if c["member"] else "")
        now = f" {'、'.join(c['after'])}" if c["after"] else "空"
        said = ("这一轮的要求里没有提到去掉工具" if len(unasked) == len(c["removed"])
                else f"这一轮的要求里没有提到去掉 {'、'.join(unasked)}")
        cost = ("没有绑定工具，它只能「假设」调用，查不了库" if not c["after"]
                else f"{'、'.join(unasked)} 它就调不到了")
        out.append({
            "level": "warning", "node_id": c["node_id"], "edge_id": None,
            "code": "tools_dropped", "field": c["field"],
            "message": f"{who}的工具从 {'、'.join(c['before'])} 变成了{now}。"
                       f"{said}，确认一下是不是改漏了：{cost}",
        })
    return out


def _repair_request(
    nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], errors: list[dict[str, Any]],
) -> str:
    graph = {"nodes": list(nodes.values()), "edges": edges}
    return (
        "这是你刚搭好的工作流：\n"
        f"{json.dumps(_slim(graph), ensure_ascii=False, indent=2)}\n\n"
        "上线前自查（和真正运行时同一套规则）发现下面这些问题，不改的话这张图跑不起来：\n"
        + "\n".join(f"- {_issue_line(i)}" for i in errors)
        + "\n\n只修这些问题，别的不要动。用操作流输出修正：改节点用 update_node"
          "（config 只写要改的字段，没写的保持原样；要删掉一个字段写 null）；"
          "缺节点、缺边就补上。最后一行输出 done。"
    )


@router.post("/generate-stream")
async def generate_stream(payload: GenerateIn, session: AsyncSession = Depends(get_session)):
    """流式版生成：SSE 逐操作推送，前端边收边把节点摆上画布。

    结束时（done 或流断）发一条 final：服务端把累积的图排版、校验后整体给出——
    过程可见性来自操作流，最终质量仍由和手工编排相同的 layout + validate 保证。
    """
    from fastapi.responses import StreamingResponse

    sources = await _sources(session, payload.datasource_ids)
    scope = {r.name for r in sources} if payload.datasource_ids else None
    tool_list = _tool_catalog(sources)
    datasources = _datasource_section(sources) + _scope_note(sources, scope is not None)
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
    # 用户摆好的坐标，收尾排版时原样留着
    pinned = _pinned_positions(payload.base_graph)
    if payload.base_graph and payload.base_graph.get("nodes"):
        # 拷一份再改：改之前的样子还要留着比对工具绑定有没有被改掉
        for n in payload.base_graph["nodes"]:
            nodes[n["id"]] = copy.deepcopy(n)
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
        raise HTTPException(400, _unconfigured(e)) from e

    messages = [("system", system),
                ("human", _with_history(user, await _history_section(session, payload.conversation_id)))]
    # 兜底用：模型只发了改动操作时，把它们重放到这张图上
    prev_graph = await _previous_graph(session, payload.conversation_id)
    # 比对工具绑定的基准：画布上是改之前的图；问数据页是上一轮的图（沿用它的节点 id）
    baseline = copy.deepcopy(
        (payload.base_graph or {}).get("nodes") or (prev_graph or {}).get("nodes") or [])

    async def event_stream():
        explanation = ""
        seen_ops: list[dict[str, Any]] = []
        skipped_types: list[str] = []
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
                if kind == "add_node" and not changed:
                    bad_type = (op.get("node") or {}).get("type")
                    if bad_type and bad_type not in _NODE_TYPES:
                        skipped_types.append(str(bad_type))
                if changed or kind in ("plan", "done"):
                    yield f"data: {json.dumps(op, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            reason, hint = explain_error(e)
            yield _sse({"op": "error", "message": f"助手这一轮没跑完：{reason}",
                        "hint": hint, "detail": raw_error(e)})
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
                    nodes[n["id"]] = copy.deepcopy(n)
            # 原地改，不能重新绑定：给 edges 赋值会让它变成 event_stream 的局部
            # 变量，而它在上面 _apply_op 那一路已经被读过了 —— UnboundLocalError
            edges[:] = [
                {k: v for k, v in e.items() if k in ("source", "target", "sourceHandle") and v}
                for e in prev_graph.get("edges") or []
            ]
            for op in seen_ops:
                _apply_op(nodes, edges, op)

        # 自查：按运行时同一套规则把图过一遍，挡住运行的问题交回模型改，改完再查。
        # 以前这些问题照样交付、照样自动开跑——跑到那一步才炸（开发库里四次运行死在
        # 循环 / 分支条件里套了 {{ }}），前面的步骤白跑，报错还停在半路
        repaired = 0
        for round_no in range(1, _SELF_CHECK_ROUNDS + 1):
            errors = _blocking_issues(nodes, edges, scope)
            if not errors:
                break
            yield _sse({"op": "check", "status": "repairing", "round": round_no, "issues": errors})
            fix = [("system", system), ("human", _repair_request(nodes, edges, errors))]
            try:
                async for op in _with_heartbeat(_iter_ops(model, fix), phase="repairing"):
                    kind = op.get("op")
                    if kind in ("thinking", "heartbeat"):
                        yield _sse(op)
                    # plan / done 是修补轮自己的开场和收尾，不是新方案，不覆盖原来的说明
                    elif kind not in ("plan", "done", "reply") and _apply_op(nodes, edges, op):
                        yield _sse(op)
            except Exception as e:  # noqa: BLE001 - 修不成就照实交付，下面列出剩下的问题
                yield _sse({"op": "check", "status": "error",
                            "message": f"自查修正没跑成：{explain_error(e)[0]}", "detail": raw_error(e)})
                break
            repaired = round_no
        remaining = _blocking_issues(nodes, edges, scope)
        # 工具绑定变化不挡运行，但要在自查这一步就说出来：工具被改没了的图照样
        # 能跑，只是跑出来的是模型「假设」查过库的答案
        changes = tool_changes(baseline, list(nodes.values()))
        warnings = dropped_tool_warnings(changes, payload.instruction)
        if remaining:
            # 改不好也照实交付、列出问题，但不自动运行：明知跑不起来的图，开跑只会
            # 在半路报一个用户看不懂的错
            autorun = False
            yield _sse({"op": "check", "status": "failed", "issues": remaining,
                        "warnings": warnings})
        elif remaining is not None:
            yield _sse({"op": "check", "status": "passed", "repaired": repaired,
                        "warnings": warnings})

        # 收尾：排版 + 校验，把最终图整体交付。改图时旧节点留在原处，只排新节点
        try:
            spec, layout = _layout_keeping(
                GraphSpec.model_validate({"nodes": list(nodes.values()), "edges": edges}), pinned,
            )
            issues = [i.model_dump() for i in validate_graph(spec).issues]
            issues += _issue_dicts(scope_issues(list(nodes.values()), scope),
                                   "datasource_out_of_scope")
            # 跳过的节点要说出来，不能安静地少一步。code 给前端认：这一类要单独
            # 提示「少了一步」，不能和普通校验警告混在一起
            issues += [
                {"level": "warning", "node_id": None, "edge_id": None,
                 "code": "unknown_node_type", "type": t,
                 "message": f"模型写了一个不存在的节点类型「{t}」，这一步已跳过"}
                for t in dict.fromkeys(skipped_types)
            ]
            final = {
                "op": "final",
                "graph": spec.model_dump(mode="json"),
                "issues": issues + warnings,
                "explanation": explanation,
                "autorun": autorun,
                "layout": layout,
                # 改图回执：哪些节点、成员的工具变了。画布上看不出来，得明说
                "tool_changes": changes,
            }
        except Exception as e:  # noqa: BLE001
            final = {"op": "error", "message": f"模型交回的工作流结构不对：{graph_error(e)}",
                     "hint": "再试一次，或者把需求说得更具体些", "detail": raw_error(e)}
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

    from app.db.models import Run, RunEvent, Workflow, WorkflowVersion
    from app.core.artifact_store import graph_hash

    run = await session.get(Run, payload.run_id)
    if not run:
        raise HTTPException(404, "这次运行的记录不存在，可能已经被删了")
    # 没跑通的路径提出来是半截骨架：失败节点之后的步骤一步都不在里面，
    # 人拿去审改时却看不出它缺了什么
    if run.status != "succeeded":
        raise HTTPException(
            409, "这次运行没有跑完（没有成功结束），提取出来的只是半截路径。"
                 "先让它跑成功一次，再从那次运行提取",
        )
    if not run.graph.get("nodes"):
        raise HTTPException(400, "这次运行没有保存工作流快照，无法提取")

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
        raise HTTPException(400, _unconfigured(e)) from e

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


# --------------------------------------------------------------------------
# 跑完之后的复核
# --------------------------------------------------------------------------


class ReviewIn(BaseModel):
    run_id: str = Field(min_length=1, description="要复核哪次运行")
    question: str = Field(default="", description="用户原本问的是什么")
    provider: str | None = None
    model: str | None = None
    #: 默认只在扫出异常信号时才叫模型。打开它就每次都复核——排查时有用，
    #: 平时不开：干净的运行没什么可复核的，代价却是每次问数都多一次调用
    always: bool = False


@router.post("/review")
async def review_run(
    payload: ReviewIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """接住一次编排的产出，判断它能不能就这么交给用户。

    「跑完了」和「答得对」是两回事。运行状态只说明流程走完了，而过程中检索
    降级、工具报错、agent 步数用满这些事，原先一件都不会进到答案里——用户拿到
    一个看起来很完整的结论，却无从知道它是在什么条件下得出的。

    规则层先扫一遍（engine/review.py:scan），没扫出东西就到此为止、直接返回，
    不花一次模型调用；扫出来了才叫模型重组答案并把异常说破。
    """
    from app.db.models import Run, RunEvent
    from app.engine import review as rv

    run = await session.get(Run, payload.run_id)
    if not run:
        raise HTTPException(404, "这次运行的记录不存在，可能已经被删了")

    rows = list((await session.execute(
        select(RunEvent).where(RunEvent.run_id == payload.run_id).order_by(RunEvent.seq)
    )).scalars())

    output = run.output if isinstance(run.output, dict) else None
    signals = rv.scan(rows, output)
    answer = rv.answer_text(output)

    if not signals and not payload.always:
        return rv.ReviewResult(verdict="ok", signals=[]).as_dict()

    try:
        spec = await copilot_model_spec(session, GenerateIn(
            instruction="review", provider=payload.provider, model=payload.model,
        ), max_tokens=4096)
        model, _ = await get_chat_model(session, spec)
    except (ProviderNotConfigured, HTTPException) as e:
        # 没配模型不该让这一轮挂掉：规则层已经知道出了什么事，照样说得出话
        return rv.ReviewResult(
            verdict="annotated",
            note="；".join(s.detail for s in signals) or (
                _unconfigured(e) if isinstance(e, ProviderNotConfigured) else str(e.detail)
            ),
            retry=rv.should_retry(signals),
            signals=signals, severity=rv.worst(signals),
        ).as_dict()

    result = await rv.review(
        model, question=payload.question, answer=answer, signals=signals,
        # 报告撰写节点产出的答案逐段对应着证据文档：只能加说明，不能改写
        locked=bool(output and output.get("_evidence")),
    )
    return result.as_dict()
