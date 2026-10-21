from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import copilot_context
from app.api.coded import DATASOURCE_SCOPE_EMPTY, CodedHTTPException
# 读流、解析、落地搬到了生成过程（copilot_gen），这里留着老名字：测试和别处照旧从这里取
from app.api.copilot_gen import (  # noqa: F401
    AppApplier, AppChecker, CopilotIncomplete, Generation, GraphApplier, GraphChecker, _apply_op, _iter_ops,
    _member_key, _NODE_TYPES, _parse_op_line, merge_node_config,
)
# 起别名：本文件底下有个叫 explain 的接口，generate 里又有个局部变量叫 raw，同名会互相盖掉
from app.core.errors import explain as explain_error, graph_error, raw as raw_error
from app.data.sqlcheck import CHECK_CODES
from app.data.sqlcheck_columns import sql_unknown_columns
from app.db.base import get_session
from app.db.models import Workflow
from app.engine.layout import CORRIDOR_MIN, MIN_ROW_GAP, NODE_W, _height, auto_layout
from app.engine.schema import GraphSpec, NodeType, ValidationIssue, text_parse_issues, validate_graph
from app.engine.state import message_text
# 额度用完的认法、退回小额度的做法挪到了 providers/budget.py（核查、证据裁判也用），这里留着老名字；
# _BUDGET_WORDS 另有 data/recipe_ai.py 的测试拿来比对
from app.providers.budget import BUDGET_WORDS as _BUDGET_WORDS  # noqa: F401
from app.providers.budget import BudgetFallback as _BudgetFallback, cut_off as _cut_off
from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model
from app.tools.registry import all_specs

router = APIRouter(prefix="/api/copilot", tags=["copilot"])
logger = logging.getLogger(__name__)


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
            400, "限定的数据源都已不可用，可能已被删除或停用。请取消限定后重试，"
                 "或到「数据」页确认数据源存在且已启用",
            DATASOURCE_SCOPE_EMPTY,
        )
    return picked


def _scope_note(rows: list[Any], scoped: bool) -> str:
    if not scoped:
        return ""
    names = "、".join(r.name for r in rows)
    return (f"\n\n这一轮用户把数据源限定为：{names}。只查它们，只能用它们的 db_query__ / "
            "db_schema__ 工具；前几轮用过别的库，这一轮也不要沿用")


def _tool_catalog(rows: list[Any], *, builtins: frozenset[str] | None = None, analysis_tools: bool = True) -> str:
    """给助手看的工具清单。

    all_specs() 只有静态注册的内置工具，数据源工具是运行时按库动态生成的
    （db_query__<源>），不补进来助手就不知道它们存在，只会退回让用户
    自己写 SQL 的代码节点——接了数据库等于白接。

    不列表名（look 文档 D3）：以前每个库列前 12 个表名，和这一轮要做什么无关，闲聊也带着；表数写一句，字段要用时 look。

    builtins 给了的话内置工具只列这几个：使用者的问数据（_CHAT_USER_PROMPT）只能用只读的那几个，列出来用不了的工具，
    模型照样会绑。analysis_tools 为假（评测「关掉分析工具」那一臂，answer_ops）时不列五个分析工具。
    """
    from app.tools.analysis import ANALYSIS_TOOLS

    lines = [
        f"- {name}（{spec.category}）：{spec.description}"
        for name, spec in sorted(all_specs().items())
        if (builtins is None or name in builtins) and (analysis_tools or name not in ANALYSIS_TOOLS)
    ]

    from app.data import introspect as _introspect
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX

    for row in rows:
        tables = _introspect.table_names(row)
        lines.append(
            f"- {QUERY_PREFIX}{row.name}（数据库）：在「{row.name}」上执行 SQL。"
            f"{row.description or ''}"
            + (f" 共 {len(tables)} 张表，字段要用时 look" if tables else " ⚠ 结构未探查")
        )
        lines.append(
            f"- {SCHEMA_PREFIX}{row.name}（数据库）：查看「{row.name}」的表结构"
        )
    return "\n".join(lines)


async def _look_service(session: AsyncSession, payload: GenerateIn, sources: list[Any], *,
                        graph: dict[str, Any] | None, kinds: tuple[str, ...] = ("table", "capability"),
                        catalog_view: Any = None) -> Any:
    """这一轮的查看服务（copilot_context.look_service）。挑选模型用同一个助手模型，第一次要挑时才拿。catalog_view：表详情
    里给哪一份数据目录（data/catalog_view.py），不给是当前目录。"""
    spec = await copilot_model_spec(session, payload, max_tokens=copilot_context.PICK_MAX_TOKENS)
    return await copilot_context.look_service(sources, spec=spec, graph=graph, kinds=kinds,
                                              answer=payload.intent == "answer", catalog_view=catalog_view,
                                              trail={"conversation_id": getattr(payload, "conversation_id", None)},
                                              session=session)


NODE_REFERENCE = """\
可用节点类型（type 字段）：
- input：入口。config.fields = [{name, required, default, description}]
- output：出口，收集最终成果。config.fields = [{name, value}]，value 里写模板引用
- llm：单次模型调用。config: {system, prompt, model, temperature, max_tokens, assign_to, output_schema}
- agent：带工具循环的 agent。config: {system, prompt, tools:[工具名], approval, assign_to, output_schema, cite_fields}
  **agent 要把查到的数交给口径卡或报告时**，config 加 output_schema（JSON Schema，写要交的字段和类型）和
  cite_fields: true：循环结束后系统按 Schema 抽一次字段，每个字段都核对到它查过的那一格——以查询快照为准，
  查不到的记空值、不兜底成 0。assign_to 拿到的就是核对过的对象，口径卡直接写 vars.<assign_to>.字段。
  只写 output_schema 不开 cite_fields 不生效。**不要用 transform 解析 agent / llm 的文字**：模型写的 JSON
  常夹着没转义的引号，解析一失败整个节点就失败，也核对不了出处
  **agent 查库时**，prompt 里写明查询结果列用中文别名（AS 月份、AS 曝光量）：报告用 [[table:]] 生成的表格表头、
  [[v:]] 引用、口径卡的 cell() 都用这个列名，写成英文缩写的原始列名读的人看不懂。中文别名按数据源的方言
  加标识符引号：MySQL / MariaDB 用反引号（AS `月份`），PostgreSQL、Oracle、SQLite 用双引号（AS "月份"），不要用
  单引号（那是字符串）。只给最外层 SELECT 的结果列起别名，WHERE、JOIN、GROUP BY 里照旧写原列名；Oracle 旧版本
  的标识符最长 30 字节，别名不超过 10 个字
  **问题问的是为什么、怎么变了、哪里拖累、谁贡献最大、是否异常、波动、趋势、是不是真的更高这类分析问题时**，查库的 agent
  除了 db_query__ / db_schema__ 再绑上五个分析工具（analysis_comparable、analysis_baseline、analysis_trend、
  analysis_decompose、analysis_differ）：变化从哪来、算不算异常、是不是持续在变、差异站不站得住由平台按固定的方法和阈值算，
  判定语由系统写好，报告引用它；agent 只决定算什么（比哪两期、拆哪个维度、往哪一段下钻）。绑了任一个，平台就把分析方法
  注入 agent 的 system，prompt 里不用再教方法，也不要写阈值。分析工具只分析这个 agent 自己查过的结果，所以只绑给同时绑了
  db_query__ 的 agent；不能放进 tool 节点（模板里每次都要做的固定分析用 analysis 分析卡）
  **默认不要写 max_steps、budget_tokens、budget_usd**。平台用的是护栏而不是固定步数：
  步数只是很高的兜底（跟随设置，默认 100），重复调用、连续几步没有新信息、预算用完、
  上下文快满时，它会按查到的部分收尾。写死一个小数字（见过 8）会让它查完表结构就没额度回答了。
  上一次如果是被护栏收了尾（改写理由里会写明原因），按原因改，而不是加步数：
  - 连续几步没有新信息：提示词里点明该查哪几张表、要哪些字段，或者工具参数写法有问题；
  - 预算用完：写一个更大的 budget_tokens；
  - 上下文快满：让它每次查更小的范围（SQL 加 LIMIT、只选需要的列）；
  - 步数用满：通常说明任务拆得太大，拆成几个节点比加步数更可靠
  MCP 工具和自定义工具默认每次调用都要人工审批（用户可以在工具页改成「始终允许」）。
  **不要为了不停下来就把 approval 写成 never**，这是绕过用户的审批设置；要不要放行由用户在工具页决定
- supervisor：多 agent 协作。config: {goal, agents:[{name, description, system, tools, model}], max_rounds}
  成员停不下来等人：需要审批的 MCP / 自定义工具在成员手里不会执行。要用这类工具，交给团队外的 agent 节点
- tool：直接调一个工具。config: {tool: 工具名, args: {...}, assign_to}
  **写外部系统的能力**（能力登记里读写是「写入」的，look 能力时看得到）只能放在 tool 节点里，不能绑给 agent、协作团队
  （发布检查会拦，运行时也会拒）。写入一律先确认再发送：运行停在这个节点开头等人确认，确认卡上是按 args 渲染出来的
  参数（遮罩后）和它的哈希，确认后按同一份参数发出，参数变了就拒绝发送。args 按引用写上游的值（{{ input.x }}、
  {{ vars.x.字段 }}），不要让 agent 拼好整段参数再传过来；要改参数就改上游的表单，确认时不能改。结果未知时系统去核实，
  不会自动重发。探索运行里端点没有测试环境的只演练、不真发
- merge：合并查询，把几次查询的结果在库外按键合并成一张表。config: {inputs: {别名: 上游查询节点id}, sql, assign_to}
  **只在数据分在不同的库、要按行对齐时用**：先在各自的库里用 SQL 聚合到相同的键和粒度（比如「日期 + 门店」），
  再由 merge 按键合并。同一个库里的数据写成一条 SQL（JOIN / WITH），不要拆成两次查询再合并；只组合几个单值时
  用口径卡的 cell()，不用 merge。比率、增幅、占比这类派生计算仍然写在口径卡里，不写进合并 SQL。
  inputs 的值只能是调用 db_query__ 工具的 tool 节点或另一个 merge 节点，而且要连在 merge 之前；agent 查过的库
  不能当输入（它可能查了好几次），要合并的那条查询单独放进 tool 节点。别名就是 sql 里的表名，只用英文字母、数字
  和下划线（例如 s、v）。sql 是一条 SQLite 的 SELECT 或 WITH，例如：SELECT s.日期, s.门店, s.订单数, v.到店人数
  FROM s JOIN v ON s.日期 = v.日期 AND s.门店 = v.门店。合并读的是每个输入的完整结果：任何一个输入被截断（超过
  查询的行数上限）merge 就会失败，所以源查询要先聚合、缩小范围；两边的键类型要一致（文本 '001' 对数值 1 会给警告）；
  结果比行数最多的输入还多，说明合并键不唯一（也会给警告）。结果和查询同形：报告按先后编号引用 [[v:Qn.r0.列名]]，
  口径卡写 cell(nodes.merge节点id, 行, '列名')。选取项直接写输入的列（可以用 AS 改名）时，报告里的数点得开、能追到
  输入的那一格；表达式算出来的列只有表级来历
- code：沙箱里跑代码。config: {language: python|bash|node, code, timeout, network, assign_to}
  evidence_role：source（取数：产出本身就是源数据）/ compute（计算，默认）。负责取数的代码写 source；
  compute 的产出喂给口径卡会被提示「核对不了出处」——业务计算写进口径卡的表达式。
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
- human：人工审批。config: {mode: approve|input|edit, title, message, assignee, policy, due, on_timeout, form, form_from,
  correlation}。后面几项是流程应用用的（写在图里，属于流程语义）；画布试运行、定时任务里没有应用绑定，一律派给任何人，
  和不写一样：
  · assignee 谁来处理：{kind:"slot", slot:"finance"}（岗位：图里只写岗位名，岗位绑到哪些人或组由人在应用里做，不要写
    具体的人）/ {kind:"initiator"}（发起人）/ {kind:"expression", expr:"input.manager"}（只能取输入和变量，结果是用户名
    或组名）/ {kind:"hook", correlation:"…"}（等外部系统回调）
  · policy 怎么汇总：any（默认，第一个人处理就算）/ all（全体同意才通过，任何一人驳回即否决）/ {quorum: m}（m 人同意
    即通过）。all 和 quorum 只用于 approve 模式
  · due 时限，ISO 8601 时长（PT4H、P2D），从待办打开时算起；on_timeout 超时怎么办：{action:"remind", every:"PT1H"} /
    {action:"escalate", to_slot:"director"}（写明升级给哪个岗位）/ {action:"reject"}，没有自动批准
  · form 静态表单（agentlab.form/1：{type:"object", properties:{字段:{type, title, enum…}}, required:[…]}，只有一层，
    不支持 pattern）；form_from 引用上游 agent 拟的表单（"nodes.agent1.form"），不合规时退回 form
  · correlation 只用于 assignee 是 hook 的：模板如 "{{ input.order_id }}"，外部系统按它找到这条待办
  title、message 说批了会怎样时写「批准后输出」「批准后继续」，**不要写「正式出具」「批准后才会出具」**：出具只发生在
  成果节点配了出具契约、并且从已发布版本发起的正式运行里；画布上试运行、问数据都是探索运行，批准后只是输出。
  这次是探索运行还是正式运行，审批卡会自己标明。审的是报告时把 human 接在 report 后面（message 里可以引用
  {{ nodes.报告节点id.text }}），审批卡会附上这份报告的核对概况
- validate：JSON Schema 校验，可自动让模型修复。config: {schema, source, max_retries}
- transform：数据整形。config: {mode: expression|template|json, expression/template, assign_to}
- subgraph：嵌套另一个工作流。config: {workflow_id, workflow_version, input}：workflow_version 写固定的版本号（1、2…），
  不写就跟随那个工作流最新的发布版本。问数据里不要拿子工作流去拼已发布的口径（子工作流里的数报告引用不到）
- metrics：口径卡。报告里要出现的数字、以及所有派生计算（比率、增幅、占比、差值）都登记在这里。
  config: {caliber, caliber_version, metrics:[{id, name, unit, decimals, format, expression}], on_missing, assign_to}
  expression 是受限表达式（不是模板）：vars.kpi.gmv、cell(nodes.fetch, 0, 'gmv')、
  round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1)
  **分母可能为 0**（上期为 0、这段时间没有数据）时写「… if 分母 else None」：
  round((vars.kpi.gmv - vars.kpi.gmv_prev) / vars.kpi.gmv_prev * 100, 1) if vars.kpi.gmv_prev else None。
  算不出来就是空值（报告里显示「—」，写作者照实说算不出来）；不要写 else 0——0 会被读成持平、0%
  format：thousands（默认，千分位）/ plain（不分组，年份编号这类）/ percent_of_ratio（值是 0.0235 这样的比率，显示成 2.35%）；
  decimals 写 -15 到 15 的整数；percent_of_ratio 的 decimals 按比率算：要显示两位百分比（2.35%）写 4，
  写 2 只剩「2%」，0.004 这样的小比率还会显示不出来、报告引用不了。on_missing 默认 fail（缺输入整张卡失败）；写 null 则缺输入的指标记为空值，交给出具契约判档
  取 tool 节点查库的结果用 cell(nodes.取数节点id, 行, '列名')（行号从 0 数，列按列名）：cell() 直接认查询工具
  交回的结果，出处精确到快照里的那一格，不用再接 transform 解析。取 agent 的字段写 vars.<agent 的 assign_to>.字段
- analysis：分析卡。对上游查询节点的结果固定做几项确定性分析，和口径卡平级：用于模板里每次都要做的分析（周报的异动：
  这周的变化在不在常规波动范围内、主要来自哪个地区）。问数据里要看了数才知道拆哪个维度的不用它，给查库的 agent 绑分析工具。
  config: {items:[{id, op, mode, from, params, hypothesis}], overrides, assign_to}
  · op：decompose（变化拆解，mode 选 additive 可加 / ratio 比率 / factor 乘积）、baseline（波动基线）、trend（趋势）、
    differ（差异检验，mode 选 rate / mean / rows）、comparable（可比性检查）；hypothesis 一句话写这一项在验证什么
  · from 是要分析的上游查询节点 id：只能是调用 db_query__ 的 tool 节点或 merge 节点，而且连在分析卡之前；agent 不能当
    输入（它可能查了好几次）。基期、本期是两份查询时不写 from，在 params 里写 base_query、target_query 两个节点 id
  · params 和同名分析工具的参数一样（period、measure、by、base、target、headline……，不写 query），可以用 {{ input.x }}
    按运行输入选期次；headline 的两格写成「节点id.r行号.列名」（行号从 0 数，如 weekly.r0.销售额）；下钻的 parent 写
    {"of": 前面一项的 id, "key": [那一段的维度值]}。params 里不写阈值：阈值来自设置，模板要固定阈值时写在 overrides
    （键同设置里分析一组的键，如 k_warn、main_share）
  · 产出 nodes.<id>.results.<项 id> = {status, verdict, direction, values, phrase}：下游 branch 可以按判定分支，如
    nodes.check.results.wow.verdict in ['outside_band', 'far_outside_band'] 时转人工。报告引用它的结论（见 report 里分析
    结论的写法）；不要把结果交给口径卡：口径卡读分析卡的结果追不到原始数据。一项算不了（历史不够、本期没过完）不让卡
    失败，那一项照实记成算不了
- dual_check：双盲复算。正式出具前，口径卡上输入来自查询的指标另起一个看不到原 SQL 的只读 agent 独立写 SQL 再算一遍：
  一致就放行；不一致时停下来，审批卡并排列出两边的 SQL、差异说明和两个值，由人逐个指标选采用原流程、采用盲算还是
  两边都不对。选盲算或都不对的计入缺口、出具降档；两边只差一个方面（业务日期按哪一列、有效记录条件、去重）时，选择
  会写进数据目录。**要正式出具的模板、口径卡上的指标来自查询（tool 节点查库，或 agent 查库并开了 cite_fields）时加**，
  接在口径卡之后、report 之前（取数 → 口径卡 → dual_check → report → output）；只回答一个问题的问数据、指标都是
  常量或运行输入的不必加。config: {card, metrics, definitions, provider, model, tolerance, on_reject, enabled}：
  card 是要复算的口径卡节点 id（上游只有一张口径卡时不写）；metrics 是要复算的指标 id 列表（不写就是输入来自查询的
  全部指标）；definitions 是 {指标 id: 口径说明}，写清业务定义（按哪个日期归属、排除哪些行、是否去重），盲算 agent
  只看得到它和数据目录，所以要按业务写，不要抄原 SQL；provider / model 指定盲算模型（**默认不写**，取设置里的核查
  模型；建议和取数的 agent 不同）；tolerance 是允许的相对误差（不写取设置）；on_reject：degrade（默认，计入缺口、
  出具降档）/ fail（审核人没采用原流程的结果就判为失败）；enabled 不写跟随设置里的开关，true / false 强制开关。
  不要给它配 retries：它的失败是确定的，重试只会把盲算再做一遍、再请审核人选一次（画布校验会报错）
- report：报告撰写（带引用）。config: {instructions, system, metrics_from:[口径卡id], numbers, on_violation, max_repairs,
  claims, analysis_claims, analysis_scope, entities, assign_to}
  **凡是给人看的、带数字的报告、分析、结论，都用 report，不要用 llm**。它自动收集上游的口径卡指标、查询结果
  （tool 节点和 agent 查过的库、merge 的合并结果，按查询先后编成 Q1、Q2…）、查库当时的表结构、知识库检索（按先后编成 K1、K2…）、
  分析结论（agent 调分析工具、分析卡做的，按先后编成 A1、A2…）和运行输入，模型只能写引用标记，数值由系统从证据里取出来渲染；
  模型自己写的数字会被打回重写：
  - [[m:指标id]]：口径卡指标。比率、增幅、占比、差值这类派生计算只能先在口径卡里登记再引用。渲染出来自带单位（口径卡里的
    unit，「8条」），instructions 里不要写成「覆盖 [[m:line_count]] 条产线」这样后面再跟一个单位，写「覆盖 [[m:line_count]] 产线」
  - [[v:Q1.r0.列名]]：第 1 次查询结果里第 0 行那一列的原值（行号从 0 数）
  - [[table:Q1 cols=列a,列b rows=0-4]]：把查询结果的几行几列原样生成表格，每一格都点得开出处
  - [[i:输入字段]]：运行输入
  - [[t:表名]]、[[c:表名.字段名]]：表和字段，点得开是哪次查询用到的、表结构里是什么类型。写在反引号里的英文名
    （`orders`）也会被核对，反引号里的中文名不核对，所以中文表名、字段名一律用 [[t:]] / [[c:]]：只写本次运行
    查过的数据源里真实存在的名字，表结构、查询、结果列里都没有的会被标成「可能是编造的名字」（受管模板的正式出具因此降档）
  - [[q:K1|逐字引文]]：知识库检索的原话，必须和命中片段里的字逐字一致（空白不计，至少 4 个字），改一个字就算
    没有出处；只是拿检索当依据时写 [[see:K1]]。要引原话，在 report 前面接 retrieve 节点
  - 句末的 [[see:m:指标id,Q1,K1]]：这句结论的依据，不显示，所以只能放在句末；instructions 里不要写「以 [[see:…]] 为依据」
    「根据 [[see:…]]」这样把它当成句子成分的示例，模型照着写会留下「以 为依据」这样的断句
  - 分析结论：[[a:A1]] 渲染成 A1 的判定语（系统按判定写好的一句，连同里面的数）；[[a:A1.top_share|pct]] 是它的一个具名的数
    （换算后缀同 [[m:]]）；[[v:A1.r0.share]]、[[table:A1 cols=地区,delta,share rows=0-4]] 取它的结果表；句末的 [[see:A1]]
    把它当这句的依据。「主要来自」「集中在」「拖累」「异常」「大幅」「持续上升」「趋势」「显著高于」这类判断只能照分析结论的
    判定写、挂它当依据（最好直接引用判定语），没有对应的分析结论就只陈述数字和变化；拆解只说明变化来自哪里、说明不了为什么，
    写原因要有知识库原话或运行输入作依据，否则写成待核实的推测。这些规矩系统会告诉写作者，instructions 里写「按分析结论写、
    判断引用判定语」就够了，不要替它把判断写好
  - 涉及码值（状态、类型这类用数字编码的取值）时，instructions 里让模型写它的含义（「作废记录」），不要写码值本身：
    码值是没有出处的数字，会被数字核对打回重写
  metrics_from 不写就取所有上游口径卡；on_violation 不写时探索运行是 flag（标出来、照常产出），正式运行是 fail；
  numbers 默认 strict（裸数字算违规）。claims 是结论句策略：off（默认，不管）/ require_citation（每句结论都要
  挂引用，没挂的计入缺口、出具降档）/ judge（在 require_citation 之外，再请另一个模型按每句挂的依据判断支不支持：
  正式运行在节点里判，证据相矛盾的按 judge.on_unsupported 处置——degrade 降档（默认）或 withhold 不予出具，
  证据不足的（摘录里没有判断所需的信息）只降档；
  探索运行不在节点里花钱，读的人点开哪句再按需判哪句，只标注；要交给人工审批的报告例外，探索运行也在节点里判，
  审批人批准前就能看到判了几句、几句证据相矛盾）。judge 的子配置写在节点的 judge 里：judge.model、
  judge.provider 指定裁判模型（建议和写作模型不同，同一个模型等于自己审自己；不写就用设置里的「证据裁判模型」）；
  judge.max_cost_usd、judge.max_claims、judge.timeout_s 是每份报告的裁判金额、句数、时长上限，写 null 表示不限，
  不写取设置里的默认；judge.rewrite_once 默认 false，打开后把证据相矛盾、证据不足的句子交回写作者只改一次。
  **多个来源汇总成的分析报告**（几个 agent 分头查库，或者几次查询的结果合在一起写成的分析、结论）默认写 claims: "judge"，
  不写 judge 的预算（judge 整个不写，金额、句数、时长上限都取设置里的默认）：正式运行在节点里自动判，探索运行由读的人
  点开哪句判哪句，不在节点里花钱；报告要交给人工审批的，探索运行也在节点里判。用户明确说不要核对结论句的不加；只回答一个问题、一次查询的不必加。要按受管级别
  发布的模板不适用这条默认，按下面受管级别的规则写预算
  analysis_claims 是分析说法核对：check（默认）判断词没挂对分析结论、原因没有依据的句子交回写作者重写一次，仍有的标在
  报告里，不影响出具档位；strict 按 max_repairs 重写，仍有的计入出具缺口、出具降档；off 不核。只能往严里改
  （off < check < strict），不要为了少重写一次写成 off。analysis_scope：auto（默认：用到分析结论时，系统在报告末尾附一段
  「分析范围」，写明拆过什么、排除了什么、哪些候选维度没拆）/ off，一般不写
  entities 默认 link
  （核对表名、字段名），报告里根本不提表名时才写 off
- output 的出具契约：config.contract = {report_from: 报告节点id, metrics_from:[口径卡id],
  required:[必需指标id], expected:[期望指标id], strict: true}。成果字段写 {{ nodes.报告节点id.text }}，
  前后不要拼别的字——拼了就没法逐段对应证据，出具会降档。受管模板的报告要直接引用查询单元格
  （[[v:]] / [[table:]]）的，契约里写 cells: true，否则这些引用按解析不了算；只引单元格、图里没有口径卡的
  （问数据的典型图），契约不写 metrics_from，也不写 required / expected
  要按受管级别发布的模板（发布前检查不过就发不出去）：报告节点写明 numbers: "strict"、on_violation: "fail"、
  analysis_claims: "strict"、
  claims: "require_citation"（或者更严的 claims: "judge"，这时 judge 里必须写 judge.max_cost_usd——写一个金额，或者
  写 null 表示不限，预算要用户定，拿不准就问；只有受管级别要这样，普通草稿里默认开的 judge 不写预算、不为预算问人）；契约用 report_from（受管级别不收 narrative），成果字段只取报告节点的正文；给口径卡
  供数的 agent 配 output_schema 和 cite_fields: true，code 节点喂口径卡的必须是真取数（evidence_role: "source"），
  否则把计算写进口径卡；llm、agent、协作团队写的文字不能绕过报告节点直接进出口，llm 也不能喂口径卡。分析卡的输入要是
  调用 db_query__ 的 tool 节点或 merge；分析卡的结论总能引用，agent 用分析工具现场做的分析和它查的格同一个规矩（契约
  写 cells: true 才引用得了）
- challenge：反方审稿。报告写完后挑出几句关键结论（挂了依据的、带比较或因果的优先），每句派一个只读 agent 站在反方
  自己写查询找反例，逐句判经受住、被削弱、被推翻或无法判断，理由引用它自己查到的数据；不改报告。
  config: {report, max_claims, max_queries, timeout_s, provider, model, on_refuted, enabled}
  **多个来源汇总成的分析报告、要正式出具的报告**加这个节点，串在 report 和 output 之间：report → challenge → output
  （有审批时 report → challenge → human → output）。challenge 要在 output 上游：只从 report 拉出一条侧枝接 challenge、
  output 不经过它时，降档不会发生。
  report 写要审的报告节点 id（上游只有一个 report 时可以不写）；on_refuted 是结论被推翻时怎么办：degrade（默认，计入
  出具缺口、出具降档）/ flag（只标注，不影响出具）/ fail（报错并停止运行，不要再配 on_error: continue）。max_claims
  （每份报告最多审几句）、max_queries（每句最多查询次数）、timeout_s（每句时长上限，秒）、enabled（不写就跟随设置里
  「核查」一节的开关）一般都不写，取设置里的默认；provider、model 指定反方模型，不写用设置里的核查模型（建议和写作
  模型不同，同一个模型等于自己审自己）。challenge 后面直接接 human 时，审批卡自动附上这份报告的核对和审稿结果；
  human 的 message 里引用 {{ nodes.报告节点id.text }}，审批人能直接读到正文。只回答一个问题、一次查询的不必加

搭图规则（有数字结论时必须遵守）：
1. 取数（tool 节点查库；或者 agent 查库并配 output_schema + cite_fields）→ 口径卡 → report → output
   （要三档出具时配 report_from 契约）。没有比率、增幅这类派生计算时可以不要口径卡，report 直接引用查询单元格
2. 能用 SUM / COUNT 做的聚合放在 SQL 里，口径卡只做标量运算
3. 不要用 code 节点做业务计算；code 只做格式转换（负责取数的 code 标 evidence_role: source）
4. 不要用 transform 解析 agent / llm 的文字：口径卡读 cell(nodes.查库节点id, 行, '列名') 或 vars.<agent 的 assign_to>.字段
5. 数据在两个库、要按行对齐时：两个 tool 节点各自聚合到相同的键和粒度 → merge 按键合并 → 口径卡算派生指标 → report。
   同一个库写成一条 SQL；只组合几个单值用口径卡的 cell()；不要用 code 节点合并（出处会断，喂给口径卡时核对不了）
6. 问题里的「今天、昨天、上周、本月、去年」这类相对日期一律按运行当天算：SQL 用数据源方言里取当天的写法（CURRENT_DATE、
   CURDATE()、date('now')、SYSDATE），agent 先调 current_time 拿到当天日期再写条件（agent 的 prompt 里写明这一点、tools 绑上
   current_time）。不要用表里最大的日期（MAX(日期)）去推「上周」「本月」：数据没更新到那段时间时，推出来的是另一段，报告还会
   把它当成上周来写。想让报告说清数据最新到哪天，另取一列 MAX(日期) AS 数据最新日期。
   写按周期取数的 SQL 前先看表的日期范围（look 结果里的剖析、取值观察）：只有一天的数据、或者最新日期早于要查的区间的表，
   不能用来算这段时间（查出来全是 0）——换有历史的表；没有这样的表，就在说明里照实告诉用户这段时间没有数据
7. 问题问为什么、怎么变了、哪里拖累、谁贡献最大、是否异常、波动、趋势、是不是真的更高这类分析问题时，查库的 agent 绑上
   分析工具（判定由平台算，报告引用判定语）；模板里每次都要做的固定分析（周报的异动）用分析卡：取数 → 分析卡 → report → output。
   分析卡的结果不进口径卡，报告直接引用

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
- human 节点（approve 模式）：批准走 sourceHandle "approved"，驳回走下面的「驳回」出口
- 「驳回」出口：会被人驳回的节点可以多接一条 sourceHandle="rejected" 的边，驳回（包括超时驳回）之后沿它往下走。
  能接的只有：human（审批、补充输入、编辑稿件三种模式）、tool（工具调用审批、写外部系统前的动作确认）、
  code（代码执行审批）、dual_check（双盲复算的裁决）；别的节点接了画布校验会报错。
  · 什么时候接：驳回之后还要做事——通知申请人（接一个通知或写说明的节点，再到成果节点）、退回修改（连回要改的上游
    那一步，改完再到这个节点问一次）、改走另一条路（比如出一份「已退回」的成果）。驳回时这一步照常结束，下游用
    {{ nodes.关卡id.note }} 取驳回备注、{{ nodes.关卡id.actor }} 取谁驳回的；动作确认被驳回时写入不会发出
  · 不接时：补充输入、编辑稿件、动作确认、代码和工具审批、双盲裁决被驳回，运行就此结束、记为被驳回（是人的决定，不算失败）；
    审批模式不接时这一路到审批为止、不再往下走（运行照常结束，没有成果）。审批写了 stop_on_reject（驳回即终止运行）就不要再接驳回出口：
    驳回时运行直接结束，那条边走不到
  · 驳回那条路必须走到成果节点，或者在一个不再往下连的节点结束（退回修改绕回上游、改完再问一次也算），否则驳回之后
    只能一直绕下去，发布检查会报错
  · 驳回不是故障：「失败重试」「出错时继续」不管驳回，不要为了驳回之后接着走去配它们
- 一个节点连出多条普通边 = 并行执行，多条边汇入同一节点 = 等待汇聚
"""

#: 节点手册里只讲分析能力的几段：(从这一行起, 到这一行之前)。评测「关掉分析工具」那一臂（answer_ops 的 analysis_tools 为假）
#: 不给这几段——agent 段里「分析类问题绑分析工具」那一条、分析卡一整条、搭图规则第 7 条：助手不知道有分析工具和分析卡，
#: 也不会被教着去用。报告段里分析结论的引用写法、分析说法核对照留（报告节点照样会核判断词）
_ANALYSIS_PROMPT_SPANS = (
    ("  **问题问的是为什么、怎么变了", "  **默认不要写 max_steps"),
    ("- analysis：分析卡。", "- dual_check："),
    ("7. 问题问为什么、怎么变了", "\n模型字段"),
)


def node_reference(*, analysis_tools: bool = True) -> str:
    """给助手的节点手册。analysis_tools 为假时去掉只讲分析能力的那几段（_ANALYSIS_PROMPT_SPANS）。"""
    if analysis_tools:
        return NODE_REFERENCE
    text = NODE_REFERENCE
    for start, end in _ANALYSIS_PROMPT_SPANS:
        i = text.index(start)
        text = text[:i] + text[text.index(end, i):]
    return text


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
    #: 问数据里用户原本问的那句话。自动重来、重试本轮时 instruction 是带着原因的那段话（「上一次运行没有成功：…」），
    #: 自查按问题判分析意图（app/analysis/intent）时用这一句；不传就用 instruction
    question: str | None = None


def _question_of(payload: GenerateIn) -> str | None:
    """自查判分析意图用的那句问题：只在问数据（intent answer）里有，画布上用户说的是一个流程，不是问题。"""
    if payload.intent != "answer":
        return None
    return (payload.question or "").strip() or payload.instruction


#: 问数据的包装里「分析类问题」那一条形状（00-总方案 §3.7）：问原因、来源、异常、趋势、差异的，判定由平台的分析工具算、系统写
#: 判定语，报告引用它。问题意图另由自查按 app/analysis/intent 的词表再核一遍：分析类问题而查库的 agent 没绑分析工具时交回改一轮
ANALYSIS_SHAPE_PROMPT = (
    "- 问的是为什么、怎么变了、哪里拖累、谁贡献最大、是否异常、正常吗、波动、趋势、是不是真的更高这类分析问题时，搭 "
    "input → agent → report → output：agent 绑 db_query__、db_schema__ 和五个分析工具（analysis_comparable、"
    "analysis_baseline、analysis_trend、analysis_decompose、analysis_differ），prompt 里写清要回答的问题、比哪两期"
    "（问题没说就比上一个整期），不用教分析方法（平台会注入）、不写阈值；report 的 instructions 写「回答：{{ input.question }}。"
    "按分析结论写，判断直接引用判定语；没有分析结论的判断不下，原因没有依据的写成待核实的推测」（可以附上 "
    "{{ nodes.agent的id.text }} 当分析思路）：数引用 agent 查过的单元格，判定引用它做过的分析结论。analysis_claims "
    "用默认，不要关\n"
)


def _user_message(payload: GenerateIn, *, patch: bool, analysis_tools: bool = True) -> str:
    """把用户说的话包成给模型的请求。analysis_tools 为假（评测「关掉分析工具」那一臂）时不给分析类问题那一条形状。

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
            # 二期起报告能直接引用 agent 查过的单元格：答案里的每个数都点得开出处，不必为问数硬塞一张口径卡。
            # 只问表结构、清单的没有数要核对，多一次写作调用只是多等十几秒
            "- 答案里有数字时，搭 input → agent → report → output：agent 绑 db_query__ 去查，report 的 instructions 写"
            "「回答：{{ input.question }}」（可以附上 {{ nodes.agent的id.text }} 当分析思路），数用 [[v:Q1.r0.列名]] "
            "直接引用 agent 查过的单元格，不必经过口径卡；要比率、增幅再在 agent 和 report 之间加口径卡，agent 配 "
            "output_schema + cite_fields，口径卡读 vars.<assign_to>.字段。出口字段写 {{ nodes.report的id.text }}\n"
            # 分析类问题那一条形状（ANALYSIS_SHAPE_PROMPT）：评测「关掉分析工具」那一臂不给
            + (ANALYSIS_SHAPE_PROMPT if analysis_tools else "")
            + "- 只问表结构、清单（有哪些表、有哪些字段、列出名单）的，不加 report，agent → output 就行\n"
            # 三期起报告会核对写出来的表名、字段名：instructions 里让它「列出用到的表」时，编出来的名字会被标出来
            "- report 提到表名、字段名时写 [[t:表名]] / [[c:表名.字段名]]（英文名也可以放进反引号，中文名不行："
            "反引号里的中文名不核对），只写 agent 真查过的名字，"
            "编造的会被标出来；要引用知识库原话就在 report 前接 retrieve，用 [[q:K1|原话]] 逐字引用。问数是探索运行，"
            "report 的 claims 不用写：结论句支不支持，由读答案的人点开时按需请模型判断。节点说明里「多个来源汇总成的"
            "分析报告默认开结论句裁判」那条在问数里不适用，几个 agent 分头查的也一样\n"
            "- 用户要的是周报、要三档出具时，按搭图规则配口径卡和 report_from 契约\n"
            "- 不要用 transform 解析 agent / llm 的文字：要把 agent 查到的数交给下游，就给它配 output_schema + "
            "cite_fields\n"
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


async def _ensure_conversation_access(session: AsyncSession, conversation_id: str | None) -> Any | None:
    """请求带的 conversation_id 这个人能不能用（对话按人隔离，D-d）：规则只有 api/deps.ensure_conversation_access 一处。
    能用交回对话；没给、不存在交回 None（当没有历史）；不能用 403——别人的问答不能经「历史」拼进这一轮的提示。"""
    from app.api.deps import ensure_conversation_access

    return await ensure_conversation_access(session, conversation_id)


async def _previous_graph(
    session: AsyncSession, conversation_id: str | None
) -> dict[str, Any] | None:
    """这次对话上一轮建出来的图。没有就是 None。"""
    from app.api.conversations import recent_turns

    if not conversation_id:
        return None
    turns = await recent_turns(session, conversation_id)
    return next((t.graph for t in reversed(turns) if _has_nodes(t.graph)), None)


def _has_nodes(graph: Any) -> bool:
    """一轮记下的图里有没有节点：只问了一句、没搭图的那一轮记的是空图，不算「上一轮的图」。"""
    return isinstance(graph, dict) and bool(graph.get("nodes"))


def _asked_of(asked: dict[str, Any]) -> dict[str, Any]:
    """模型的反问（ask 操作）→ 交给前端、记进这一轮 meta.asked 的形状 {question, options}。"""
    options = asked.get("options")
    listed = options if isinstance(options, list) else []
    return {"question": str(asked.get("question") or ""),
            "options": [str(o) for o in listed if isinstance(o, (str, int, float))]}


def _asked_final(asked: dict[str, Any], base_graph: dict[str, Any] | None,
                 prev_graph: dict[str, Any] | None) -> dict[str, Any]:
    """只问不改的一轮的收尾：图原样交回（画布上原来的图、问数据上一轮的图，都没有就是空图），不运行、不重排。"""
    graph = base_graph if _has_nodes(base_graph) else prev_graph if _has_nodes(prev_graph) else None
    graph = graph or {"nodes": [], "edges": []}
    return {"op": "final", "graph": graph, "issues": [], "explanation": "", "autorun": False,
            "layout": {"mode": "keep", "placed": []}, "tool_changes": [], "asked": _asked_of(asked)}


async def _history_section(session: AsyncSession, conversation_id: str | None) -> str:
    """把之前几轮拼成给模型的上下文。没有会话或没有历史时返回空串。

    带上一轮的图是关键的那一半。只给问答文字的话，"再按月份拆一下"会让模型
    重新设计一张图——它得从头猜该接哪个数据源、哪张表，而这些上一轮已经对好了。
    实测这种重建经常接到另一张表上，答案对不上前一轮，用户完全无从判断。

    图走 _slim 去掉坐标噪音，和 base_graph 那条路径用的是同一份。

    上一轮查看过的对象（前端存在这一轮的 meta.looks 里）写一行：追问时模型直接点名看，不用再按需求挑（look 文档 D7）。
    """
    from app.api.conversations import _answer_of, recent_turns

    if not conversation_id:
        return ""
    turns = await recent_turns(session, conversation_id)
    if not turns:
        return ""

    lines = [
        f"第 {i} 轮\n  问：{t.question}\n  答：{_asked_line(t) or _answer_of(t) or '（没有结果）'}"
        for i, t in enumerate(turns, 1)
        if t.question
    ]
    if not lines:
        return ""

    block = "# 这次对话之前聊过的\n" + "\n".join(lines)

    last_graph = next((t.graph for t in reversed(turns) if _has_nodes(t.graph)), None)
    if last_graph:  # 和 _previous_graph 取的是同一张，兜底重放靠的就是它
        block += (
            "\n\n上一轮用的工作流（数据源和表都已经对好了）：\n"
            f"{json.dumps(_slim(last_graph), ensure_ascii=False, indent=2)}"
        )
    if looked := _looked_line(turns[-1]):
        block += "\n\n" + looked
    return block


#: 只问不改的那一轮在历史里的写法：下一轮用户的话就是在回答它
ASKED_PROMPT = "（助手反问，等用户回答：{question}{options}）"


def _asked_line(turn: Any) -> str:
    """这一轮是助手反问（前端记进 meta.asked：final 带的 {question, options}，或者只记 true、问题写在答案里）：写成「助手
    反问了什么」——不然下一轮模型把它当成自己给出的答案，不知道用户的话是在回答它的问题。不是反问的轮次是空串。"""
    from app.api.conversations import turn_meta

    asked = (turn_meta(turn) or {}).get("asked")
    if asked is True:
        asked = {"question": turn.answer or ""}
    if not isinstance(asked, dict) or not str(asked.get("question") or "").strip():
        return ""
    options = [str(o) for o in asked.get("options") or [] if isinstance(o, (str, int, float))]
    return ASKED_PROMPT.format(question=str(asked["question"]).strip(),
                               options=f"；候选：{'、'.join(options)}" if options else "")


LOOKED_PROMPT = "上一轮查看过：{items}（要再看时直接点名）"
LOOKED_KIND_PROMPT = {"capability": "能力 {names}", "workflow_version": "工作流版本 {names}"}


def _looked_line(turn: Any) -> str:
    """上一轮 meta.looks 里查看过的对象，一行：「上一轮查看过：库 B 的 refunds、orders；能力 x」。没查看过是空串。"""
    from app.api.conversations import turn_meta

    looks = (turn_meta(turn) or {}).get("looks")
    parts: list[str] = []
    for look in looks if isinstance(looks, list) else []:
        for obj in (look.get("objects") or []) if isinstance(look, dict) else []:
            if not isinstance(obj, dict):
                continue
            names = [n for key in ("named", "picked", "joined") for n in obj.get(key) or [] if isinstance(n, str)]
            if not names:
                continue
            kind = obj.get("kind")
            text = (f"{obj.get('source')} 的 " + "、".join(names) if kind == "table" and obj.get("source")
                    else LOOKED_KIND_PROMPT.get(str(kind), "{names}").format(names="、".join(names)))
            if text not in parts:
                parts.append(text)
    return LOOKED_PROMPT.format(items="；".join(parts)) if parts else ""


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
    """当前 Copilot 会用哪个模型，以及是显式配的还是兜底来的。

    存的接入不存在、已停用（或者只存了模型名、提供它的接入已停用）时实际什么都用不了：effective_* 为空，
    error 写明原因（和生成时报的同一句），能用时 error 为 None。panel_error 是同一件事写在助手栏「助手使用的模型」
    那一格下面的说法：人就在这个下拉框旁边，去向写「上方」，不再把人指回这个下拉框本身。"""
    from app.db.models import Setting
    from app.providers.factory import assistant_unavailable, resolve_provider

    row = await session.get(Setting, COPILOT_SETTING_KEY)
    saved = row.value if row else {}
    configured = bool(saved.get("provider") or saved.get("model"))
    try:
        resolved = await resolve_provider(session, saved.get("provider"), saved.get("model"))
    except ProviderNotConfigured as e:
        return {"configured": configured, "provider": saved.get("provider") or None,
                "model": saved.get("model") or None, "effective_provider": None, "effective_model": None,
                "error": _unconfigured(e), "panel_error": assistant_unavailable(e, here=True)}
    return {
        "configured": configured,
        "provider": saved.get("provider") or None,
        "model": saved.get("model") or None,
        "effective_provider": resolved.name if resolved else None,
        "effective_model": saved.get("model") or (resolved.default_model if resolved else None),
        "error": None,
        "panel_error": None,
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
    # 点名的接入不存在、已停用照实说是哪个、去哪里改，其余说「尚未配置完成」（表格导入、数据目录的 AI 起草同一句）
    from app.providers.factory import assistant_unavailable

    return assistant_unavailable(e)


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


def _graph_system(sources: list[Any], scope: set[str] | None, datasources: str, looks: Any, *,
                  chat_user: bool = False, analysis_tools: bool = True) -> str:
    """画布、问数据（流式和非流式）共用的 system：节点手册、工具清单、数据源概览、结构要求、输出协议、查看的用法。
    这一份一直用到自查修正结束（接着写、修正轮都不换），模型前后看到的是同一套说法。

    chat_user：开登录后 user 角色的问数据（D-c）。内置工具只列只读的那几个，结构要求后面再加一段使用者的约束。
    analysis_tools 为假（评测「关掉分析工具」那一臂）：节点手册去掉只讲分析能力的几段，工具清单不列分析工具。"""
    from app.auth.chat_policy import CHAT_BUILTIN_TOOLS

    catalog = _tool_catalog(sources, builtins=CHAT_BUILTIN_TOOLS if chat_user else None, analysis_tools=analysis_tools)
    return (
        "你是一个 agent 工作流编排专家。根据用户需求，以操作流的方式逐步搭出一张可执行的工作流图。\n\n"
        f"{node_reference(analysis_tools=analysis_tools)}\n\n可用工具：\n{catalog}"
        f"{datasources}{_scope_note(sources, scope is not None)}\n\n"
        "结构要求：\n"
        "1. 必须有且只有一个 input 节点和至少一个 output 节点\n"
        "2. 节点 id 用简短英文小写下划线；label 用中文，一眼看懂\n"
        "3. 用 assign_to 传结果，下游 {{ vars.变量名 }} 引用\n"
        "4. 只能用上面列出的工具名\n"
        "5. 能用 3 个节点解决就别堆 8 个"
        f"{_CHAT_USER_PROMPT if chat_user else ''}\n\n"
        f"{_STREAM_PROTOCOL}"
        # 问数据里能选用已发布口径时（next-round 02 做法 4）：协议多一段 use_caliber，「什么时候该看」换成问数据那一份
        + (f"\n{copilot_context.USE_CALIBER_PROMPT}" if copilot_context.composing(looks) else "")
        + (f"\n\n{looks.guide()}\n{copilot_context.look_when(looks)}" if looks.kinds else "")
    )


def _chat_user_kinds() -> str:
    from app.auth.chat_policy import CHAT_NODE_TYPES

    return "、".join(sorted(CHAT_NODE_TYPES))


#: 开登录后 user 角色在问数据里生成的图（D-c）要守的约束，接在结构要求后面（_graph_system 的 chat_user）。发起时按
#: app/auth/chat_policy.py 收窄：节点只许读数据、整理、撰写这一类，tool 节点和 agent 只能用数据源查询和只读的内置工具，
#: 所以先说给模型听。规则本身只在 chat_policy 一处：节点类型从那里取，自查也按它判（_chat_user_issues），这里只是把它讲成
#: 模型照着搭得出来的写法
_CHAT_USER_PROMPT = (
    "\n\n这位用户是「使用者」：这张图以他的身份运行，只能读数据、整理、撰写。下面几条优先于上面节点手册和搭图规则里的"
    "写法：\n"
    f"1. 节点只用 {_chat_user_kinds()}。不要用 code、supervisor、subgraph、loop、human、memory、validate 等其他节点\n"
    "2. tool 节点和 agent 都只能用 db_query__<数据源>、db_schema__<数据源> 和上面列出的内置工具：取数用 tool 节点"
    "（tool 写 db_query__<数据源>，args.sql 写好 SQL）或 agent（tools 里绑定这两种数据源工具）。不要用 http_request、"
    "records_append 这类写入或连外部的工具，工具名不要用 {{ }} 拼\n"
    "3. 查询一律只读，不要写改数据的 SQL\n"
    "4. 任何节点、图级 defaults、tool 节点的 args 里都不要写 memory_scope、collection：记忆范围和知识库由平台定"
)


def _chat_user_issues(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """这张图使用者在问数据里能不能发起（app/auth/chat_policy.py，和 POST /api/runs 同一个判断）：不能的话是一条挡运行的
    问题，交回模型按 _CHAT_USER_PROMPT 改；改不好照实列在交付的问题里，不自动运行。"""
    from app.auth.chat_policy import chat_graph_problem

    problem = chat_graph_problem({"nodes": list(nodes.values()), "edges": edges})
    if not problem:
        return []
    return [{"level": "error", "node_id": None, "edge_id": None, "field": None, "code": "chat.not_allowed",
             "message": problem,
             "for_model": f"{problem}。按 system 里「使用者」那一段的约束改：取数用 tool 节点或 agent 调数据源查询工具，"
                          "去掉不允许的节点、工具和记忆范围 / 知识库配置"}]


#: 关掉分析工具时（answer_ops 的 analysis_tools 为假）图里还绑着分析工具的那条问题的 code
ANALYSIS_TOOLS_OFF = "analysis_tools_off"


def _analysis_off_issues(nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """评测「关掉分析工具」那一臂：这一轮的工具清单里没有分析工具，图里照样绑了的（Agent、协作成员、调用工具节点）按
    「绑定的工具不存在」挡（同 engine/launch 发起前那一道的说法），交回模型去掉；改不好就不运行。一个节点一条。"""
    from app.tools.analysis import ANALYSIS_TOOLS

    out: list[dict[str, Any]] = []
    for nid, node in nodes.items():
        cfg = ((node or {}).get("data") or {}).get("config") or {}
        ntype = (node or {}).get("type")
        if ntype == NodeType.TOOL:
            names = [cfg.get("tool")]
        elif ntype == NodeType.AGENT:
            names = list(cfg.get("tools") or [])
        elif ntype == NodeType.SUPERVISOR:
            names = [t for m in cfg.get("agents") or [] if isinstance(m, dict) for t in m.get("tools") or []]
        else:
            continue
        bound = list(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip() in ANALYSIS_TOOLS))
        if not bound:
            continue
        title = str(((node or {}).get("data") or {}).get("label") or "").strip() or nid
        field = "tool" if ntype == NodeType.TOOL else "agents" if ntype == NodeType.SUPERVISOR else "tools"
        out.append({"level": "error", "node_id": nid, "edge_id": None, "field": field, "code": ANALYSIS_TOOLS_OFF,
                    "message": f"绑定的工具不存在：「{title}」绑定的 {'、'.join(bound)}。这一轮没有分析工具可用，请在节点中去掉它们",
                    "for_model": f"「{nid}」绑定的 {'、'.join(bound)} 不在这一轮的工具清单里，这一轮没有分析工具可用：从它的"
                                 "工具里去掉这几个，只用上面列出的工具（db_query__、db_schema__ 照留）；数据要怎么比、怎么拆，"
                                 "写进查询"})
    return out


def _graph_request(payload: GenerateIn, *, analysis_tools: bool = True) -> str:
    """这一轮的请求：改现有图时附上原图、只要改动操作；新建时按入口包好（_user_message）。"""
    if payload.base_graph and payload.base_graph.get("nodes"):
        return (
            "这是当前的工作流：\n"
            f"{json.dumps(_slim(payload.base_graph), ensure_ascii=False, indent=2)}\n\n"
            f"请按下面的要求修改它（只输出改动操作）：\n{payload.instruction}"
        )
    return _user_message(payload, patch=False, analysis_tools=analysis_tools)


def _start_graph(payload: GenerateIn) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """服务端维护的图状态：修改场景从 base_graph 起步（拷一份再改：改之前的样子还要留着比对工具绑定），新建从空图起步。"""
    if not (payload.base_graph and payload.base_graph.get("nodes")):
        return {}, []
    nodes = {n["id"]: copy.deepcopy(n) for n in payload.base_graph["nodes"]}
    edges = [{k: v for k, v in e.items() if k in ("source", "target", "sourceHandle")}
             for e in payload.base_graph.get("edges") or []]
    return nodes, edges


@router.post("/generate", response_model=GenerateOut)
async def generate(
    payload: GenerateIn, session: AsyncSession = Depends(get_session)
) -> GenerateOut:
    """用自然语言生成或改写一张工作流图，一次返回整张图。

    和流式生成走同一个生成过程（copilot_gen：起草、按需查看、自查修正），只是不推送过程，收完再排版、校验后返回。
    产物会经过和手工编排完全相同的校验与排版，所以生成完就是可运行、可读的。
    """
    from app.data.sqlcheck import load_checkers

    await _ensure_conversation_access(session, payload.conversation_id)
    sources = await _sources(session, payload.datasource_ids)
    scope = {r.name for r in sources} if payload.datasource_ids else None
    # 先拿模型：没配好就直接 400，不白读一遍目录
    try:
        model, _ = await _op_model(session, payload)
    except ProviderNotConfigured as e:
        raise HTTPException(400, _unconfigured(e)) from e
    checkers = await load_checkers(session, sources)
    prev_graph = await _previous_graph(session, payload.conversation_id)
    looks = await _look_service(session, payload, sources, graph=payload.base_graph if (
        payload.base_graph or {}).get("nodes") else prev_graph)
    system = _graph_system(sources, scope, await copilot_context.section(looks), looks)
    nodes, edges = _start_graph(payload)
    baseline = copy.deepcopy((payload.base_graph or {}).get("nodes") or (prev_graph or {}).get("nodes") or [])
    applier = GraphApplier(nodes, edges, patches=False)
    gen = Generation(model, system=system, applier=applier, looks=looks,
                     checker=GraphChecker(applier, scope=scope, sources=sources, baseline=baseline, checkers=checkers,
                                          question=_question_of(payload)))
    human = _with_history(_graph_request(payload), await _history_section(session, payload.conversation_id))
    try:
        async for _op in gen.draft(human):
            pass
        if applier.replied is None and gen.asked is None and all(o.get("op") == "plan" for o in applier.seen):
            raise gen.empty()
    except Exception as e:  # noqa: BLE001
        reason, _ = explain_error(e)
        if isinstance(e, CopilotIncomplete) and e.detail:
            reason = f"{reason}；{e.detail}"
        raise HTTPException(
            502, f"模型未返回可用的工作流（{reason}）。请换一种描述重试；如反复出现，"
                 "请在输入框的「助手使用的模型」中换用指令遵循能力更强的模型",
        ) from e
    if applier.replied is not None:
        # 这句话不需要工作流：原图照旧，回答放在说明里
        return GenerateOut(graph=payload.base_graph or {"nodes": [], "edges": []},
                           explanation=str(applier.replied.get("text") or ""))
    if not nodes and prev_graph:
        applier.replay_onto(prev_graph)
    async for _op in gen.repair():
        pass
    covered = await gen.checker.coverage(level="warning") if isinstance(gen.checker, GraphChecker) else []
    covered += gen.checker.analysis_gaps(level="warning") if isinstance(gen.checker, GraphChecker) else []

    try:
        spec = GraphSpec.model_validate({"nodes": list(nodes.values()), "edges": edges})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            502, f"模型返回的工作流结构有误：{graph_error(e)}。请重试，或把需求描述得更具体",
        ) from e

    spec, layout = _layout_keeping(spec, _pinned_positions(payload.base_graph))
    report = validate_graph(spec)
    out = spec.model_dump(mode="json")
    changes = tool_changes(baseline, out["nodes"])
    return GenerateOut(
        graph=out,
        # 模型问了用户（ask）：这一轮到此为止，问题放在说明里
        explanation=applier.explanation or str((gen.asked or {}).get("question") or ""),
        issues=[i.model_dump() for i in report.issues if i.level != "info"]
        + _issue_dicts(scope_issues(out["nodes"], scope), "datasource_out_of_scope")
        + dropped_tool_warnings(changes, payload.instruction) + covered,
        layout=layout,
        tool_changes=changes,
    )


# --------------------------------------------------------------------------
# 流式生成：模型输出逐行操作流（NDJSON），画布看着图长出来
# --------------------------------------------------------------------------

_STREAM_PROTOCOL = """\
输出格式（严格遵守）：
- 每行一个完整的 JSON 对象，除此之外不输出任何东西——不要 markdown 围栏、不要解释文字、不要空行注释，
  也不要写成工具调用的格式（这里没有工具调用接口，look 也是一行 JSON）
- 思考和输出共用同一份长度额度：思考里只定方案（加哪些节点、怎么连、每个节点干什么），
  不要在思考里把 JSON、提示词、表达式先写一遍——那些直接写进输出。额度用完时还没输出的改动就全丢了
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
  这时除了下面的 catalog_patch，不要输出任何别的操作
- 要看表结构、能力的参数这类详情时输出一行 look（用法见后面的「按需查看」），输出之后就停下等查看结果，
  拿到结果再接着写；已经输出的操作不要重复
- 必须先问清用户才能动手时，输出一行 {"op":"ask","question":"要问的话，把候选列出来"}，这一轮就此结束：已经输出的改动
  照常交付，用户的回答是下一轮。能按常理定的不要问
- 数据目录修改提案（可选）：用户在这句话里**明确陈述了一条数据事实**时，另输出一行
  {"op":"catalog_patch","source":"数据源名","table":"表名","changes":[{"path":"…","value":…,"reason":"一句话依据"}]}
  它只是提案，用户确认后才写进数据目录。可以和图操作一起出现（写在 done 之前），也可以只配一行 reply（写在 reply 之前）
  · path：表级项直接写字段名（label 中文名、description 说明、grain 粒度、keys 业务主键列表、
    kind 表类型 fact/dimension/snapshot/log/config、business_date {"column","rule","timezone"}、
    valid_filter 有效记录的 SQL 条件、dedup 去重规则）；列级项写 columns.<列名>.<字段>（label、meaning 含义、
    unit 单位、measure 度量类型 flow/stock/ratio/identifier/status/attribute、codes 码值 {"码":"含义"}）；
    已有的关联关系写 relations.<编号>；新增关联关系写 relations.new，value 写
    {"columns":[本表列],"to_table":"目标表","to_columns":[目标列],"cardinality":"many_to_one/one_to_one/one_to_many"}
  · codes 只写要补充或更正的码，原有的码会保留；valid_filter 要写成完整的条件（原有条件也要带上）
  · 例：「在园人数是存量，不能跨天相加」→ columns.<那一列>.measure 写 stock；「status=9 表示作废，统计时要排除」→
    columns.status.codes 写 {"9":"作废"}，再把 valid_filter 补上排除条件；「channel_visits 是渠道和入园记录的关联表」→
    description 和两条 relations.new
  · 只有用户明确说出了数据事实才提，不要从自己的推测、查询结果或提问里提；表名、列名必须是数据源里真实存在的
    （先 look 那张表，看清现有目录再提）
  · 计算公式不进数据目录：「转化率=下单数/访问数」这类指标怎么算的说法属于口径卡，不要提"""


#: 模型只发了目录修改提案、既没回答也没改图时，补的那句回答
PATCH_ONLY_REPLY = "已按你的说明整理出数据目录的修改建议，确认无误后可保存到数据目录。"


async def catalog_patch_op(op: dict[str, Any], sources: list[Any]) -> dict[str, Any] | None:
    """模型发的 catalog_patch → 转给前端的提案；整条不成立时返回 None。**只读，不写目录。**

    核对数据源（只认这一轮给助手看的源，按名字）、表（按表结构认）、每一项的路径和取值格式（catalog.plan_patch，
    和保存时同一套），附上当前版本、改前和改后的值。不合法的项丢掉、记一条日志，其余照转；一项都不剩就整条不转。
    写入只发生在人点「保存到数据目录」之后（/catalog/{table}/patch，带着这里给的版本）。

    开流之后请求的会话不能再用，这里另开一个短会话读目录：版本取提案到达的这一刻，比开流前读的新。
    """
    from app.data import catalog, table_versions
    from app.db.base import SessionLocal

    name = op.get("source")
    table = op.get("table")
    row = next((s for s in sources if isinstance(name, str) and s.name == name), None)
    if row is None:
        logger.warning("目录修改提案被丢弃：数据源「%s」不在这一轮的范围内", name)
        return None
    try:
        async with SessionLocal() as session:
            source = await table_versions.resolve_source(session, row)
            tables = (source.schema_cache or {}).get("tables") or {}
            key = catalog.resolve_table_name(source.schema_cache, table) if isinstance(table, str) else None
            if key is None:
                logger.warning("目录修改提案被丢弃：数据源「%s」中没有表「%s」", name, table)
                return None
            entry = await catalog.read_entry(session, row.id, key)
    except Exception:  # noqa: BLE001 - 提案是附加的，读不到目录就不提，不能让这一轮生成失败
        logger.exception("目录修改提案被丢弃：读取「%s」的数据目录失败", name)
        return None
    plan = catalog.plan_patch(entry.notes if entry else {}, op.get("changes"), table=key, tables=tables)
    for problem in plan.problems:
        logger.warning("目录修改提案（%s.%s）有一项被丢弃：%s", name, key, problem)
    # 已经是这个值、也确认过的项没什么可提的
    changes = [c.as_dict() for c in plan.changes if c.state != "same"]
    if not changes:
        logger.warning("目录修改提案被丢弃：%s.%s 没有需要修改的项", name, key)
        return None
    label = (entry.notes.get("label") if entry else None) or {}
    return {
        "op": "catalog_patch", "source": row.name, "source_id": row.id, "table": key,
        "table_label": label.get("value") if label.get("status") != "rejected" else None,
        "version": entry.version if entry else 0, "changes": changes,
    }


#: 流式改图的输出额度。会思考的模型（DeepSeek、Claude 的 adaptive thinking）思考也算在里面：
#: 8192 时复杂的改图常常想完就没额度了，正文一个字没写（2026-09 部署实例上同类请求 5 轮坏了 4 轮）
COPILOT_MAX_TOKENS = 32768
#: 服务方不收大额度时退回这个（原来的默认值）。退回的做法（_BudgetFallback：只在还没吐出任何东西时重来，
#: 之后的调用即自查修正轮都用小的）和截断的认法（_cut_off）在 providers/budget.py，核查、证据裁判也用它
_FALLBACK_MAX_TOKENS = 8192


async def _op_model(session: AsyncSession, payload: GenerateIn) -> tuple[Any, str]:
    """按操作流改图用的模型：额度见 COPILOT_MAX_TOKENS。"""
    spec = await copilot_model_spec(session, payload, max_tokens=COPILOT_MAX_TOKENS)
    big, model_id = await get_chat_model(session, spec)
    small, _ = await get_chat_model(session, spec.model_copy(update={"max_tokens": _FALLBACK_MAX_TOKENS}))
    return _BudgetFallback(big, small), model_id


# 多久没动静就补一拍心跳。3 秒是"用户开始怀疑是不是卡了"的量级。
_HEARTBEAT_SECONDS = 3.0
#: 蓝图助手和填关节里算「正在搭」的操作（bp_ask 是反问，不算）
_BLUEPRINT_BUILD_OPS = ("bp_set", "bp_add", "bp_remove", "bp_template", "fill_joint")


async def _with_heartbeat(source: Any, phase: str = "planning", *, started: float | None = None):
    """模型沉默时按拍补心跳，让前端能显示阶段和已用时长。

    必须在这一层做而不是在 _iter_ops 里判断时间差：模型不吐 chunk 时那个
    async for 的循环体根本不执行，压根轮不到检查。

    started 给了就从那一刻算已用时长：起草、查看、接着写、自查修正几段的心跳要接着算，
    不能每一段一开始就跳回 0 秒。
    """
    started = time.monotonic() if started is None else started
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
                # 阶段跟着实际操作走，前端据此换文案。查看时生成过程自己发一拍 looking，查看完回到 planning
                # 蓝图助手的 bp_* 操作、填关节的 fill_joint 也是在动手（api/copilot_blueprint.py）
                if kind == "add_node" or kind in APP_OPS or kind in _BLUEPRINT_BUILD_OPS:
                    phase = "building"
                elif kind in ("add_edge", "connect"):
                    phase = "wiring"
                elif kind == "done":
                    phase = "finalizing"
                elif kind == "heartbeat":
                    phase = op.get("phase") or phase
                elif kind == "look":
                    phase = "planning"
                yield op
                break
            yield {
                "op": "heartbeat",
                "phase": phase,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _blocking_issues(
    nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], scope: set[str] | None = None,
    *, sources: list[Any] | None = None, baseline: list[dict[str, Any]] | None = None,
    level: str | None = None, defaults: dict[str, Any] | None = None,
    checkers: dict[str, Any] | None = None, chat_user: bool = False, analysis_tools: bool = True,
) -> list[dict[str, Any]] | None:
    """按运行时同一套校验，挑出会挡住运行的问题。图本身不成形时返回 None。

    chat_user：图要以开登录后 user 角色的身份在问数据里发起（D-c），使用者不能用的节点、工具、配置也挡（_chat_user_issues）。

    analysis_tools 为假（评测「关掉分析工具」那一臂）：图里还绑着分析工具的也挡（_analysis_off_issues）。

    限定了数据源时，用了范围外的库也算：用户点了「只查这个库」，拿别的库的数
    回答他，比跑不起来更糟——答案看起来是对的。

    另有两类运行时不挡、但助手搭图时要打回的写法（authored_issues）：SQL 里的列在数据源
    的结构里查不到、整形节点按 JSON 解析 agent / llm 写的文字。

    level（published / governed）给了的话，那一档发布门禁的 error 也算进来：助手改完的图
    能跑还不够，要能发布。不给就和以前一样只管能不能跑。

    每条是 ValidationIssue 的字典形状（node_id、field 都在），和 final.issues 一样：
    界面靠 node_id 定位卡片、靠 field 落到具体的输入框，不用从一行字里抠节点 id。

    defaults 是全图默认（graph.defaults）：报告撰写节点的 numbers / claims、审批策略都跟随它，不带上的话
    自查看到的问题和真正的门禁对不上。

    checkers 是各数据源基于数据目录的 SQL 检查器（data/sqlcheck.load_checkers）：调用工具节点里写死的 SQL
    查出 error 级的问题（一对多关联后重复计算、存量跨期求和）也算挡住，交回模型改；warning、info 不挡。
    """
    try:
        spec = GraphSpec.model_validate({"nodes": list(nodes.values()), "edges": edges,
                                         **({"defaults": defaults} if isinstance(defaults, dict) else {})})
    except Exception:  # noqa: BLE001 - 结构不合法留给收尾那一步报
        return None
    # 带发布级别时 SQL 检查交给门禁（查全图、按级别定挡不挡），自查这边就不再查一遍，免得同一处报两条
    authored = authored_issues(spec, sources or [], baseline, checkers=None if level else checkers)
    out = ([i.model_dump() for i in validate_graph(spec).issues if i.level == "error"]
           + _issue_dicts(scope_issues(list(nodes.values()), scope), "datasource_out_of_scope")
           + [i for i in authored if i["level"] == "error"]
           + (_chat_user_issues(nodes, edges) if chat_user else [])
           + ([] if analysis_tools else _analysis_off_issues(nodes)))
    if level:
        from app.engine.governance import lint_for_publish

        gate = [i.model_dump() for i in lint_for_publish(spec, level=level, checkers=checkers).issues
                if i.level == "error"]
        out += _sql_model_hints(gate, spec, checkers)
    return out


def _sql_model_hints(issues: list[dict[str, Any]], spec: GraphSpec, checkers: dict[str, Any] | None,
                     ) -> list[dict[str, Any]]:
    """门禁报的 SQL 问题只有给人看的那句（ValidationIssue 不带 for_model）；交回模型改时补上给模型的改法。"""
    if not checkers or not any(i.get("code") in CHECK_CODES for i in issues):
        return issues
    from app.data.sqlcheck import graph_checks

    hints = {(node.id, c.code, c.message): c.for_model for node, c in graph_checks(spec.nodes, checkers)}
    return [{**i, "for_model": hints[key]} if (key := (i.get("node_id"), i.get("code"), i.get("message"))) in hints
            else i for i in issues]


def _changed(spec: GraphSpec, baseline: list[dict[str, Any]] | None) -> set[str]:
    """这一轮新加或改过配置的节点。改图时只查这些：用户自己写的、这一轮没碰的 SQL 不替他改。"""
    before = {n.get("id"): (n.get("data") or {}).get("config") for n in baseline or [] if isinstance(n, dict)}
    return {n.id for n in spec.nodes if n.id not in before or before[n.id] != n.config}


_NUMERIC_TYPES = ("number", "integer", "float")


def _numeric_input(field: Any) -> bool:
    """入口的这个字段看得出是数：写了数值类型，或者默认值是数（画布上填的默认值是字符串，能读成数的也算）。"""
    if not isinstance(field, dict) or not str(field.get("name") or "").strip():
        return False
    if str(field.get("type") or "").strip().lower() in _NUMERIC_TYPES:
        return True
    default = field.get("default")
    if isinstance(default, bool):
        return False
    if isinstance(default, (int, float)):
        return math.isfinite(default)
    if isinstance(default, str) and default.strip():
        try:
            return math.isfinite(float(default.strip().replace(",", "")))
        except ValueError:
            return False
    return False


def _evidence_source(node: Any) -> bool:
    """报告撰写能从这个节点拿到可引用的证据：口径卡、分析卡（结论总能引用，判定语、具名的数、结果表，00-总方案 D10）、
    调用工具、配了工具的 agent、知识检索，以及声明了数值字段的入口（运行输入进证据目录，报告用 [[i:字段]] 引用）。
    子工作流、协作团队里面是什么看不见，一律算有——这条会把图打回去让模型改，宁可漏判不可误判。

    入口只声明了文字字段的（问题、主题）不算：报告能照引的只有问题本身，要写的数照样没有出处。画布上的入口字段
    不写类型，没写默认值的字段看不出是不是数，也不算——提示里说明了怎么声明（写数值默认值）。"""
    if node.type == NodeType.AGENT:
        return bool(node.config.get("tools"))
    if node.type == NodeType.INPUT:
        fields = node.config.get("fields")
        return isinstance(fields, list) and any(_numeric_input(f) for f in fields)
    return node.type in (NodeType.METRICS, NodeType.ANALYSIS, NodeType.TOOL, NodeType.MERGE, NodeType.RETRIEVE,
                         NodeType.SUBGRAPH, NodeType.SUPERVISOR)


def _unsourced_reports(spec: GraphSpec, changed: set[str]) -> list[dict[str, Any]]:
    """这一轮新加或改过的报告撰写节点，上游一个证据来源都没有：运行时只给一条警告，报告里的数全被判成没有出处。"""
    from app.engine.schema import _ancestors

    nodes = spec.node_map()
    out = []
    for node in spec.nodes:
        if node.id not in changed or node.type != NodeType.REPORT:
            continue
        if any(_evidence_source(nodes[a]) for a in _ancestors(spec, node.id) if a in nodes):
            continue
        out.append({"level": "error", "node_id": node.id, "edge_id": None, "field": None, "code": "report_no_source",
                    "message": f"「{node.title}」（报告撰写）的上游没有可引用数字的证据来源（口径卡、查询数据的 Agent、"
                               "调用工具、知识检索，或声明了数值的输入字段），报告中的数字都将被判为没有出处。请在它前面接入"
                               "取数的节点并连线（需要计算比率、增幅时再添加口径卡）；如果要写的数字来自运行输入，请在输入字段"
                               "上填写数值默认值，报告按 [[i:字段名]] 引用",
                    "for_model": f"「{node.title}」（报告撰写）的上游没有能引用数字的证据来源：没有口径卡、查库的 Agent 或调用工具、"
                                 "知识检索，入口也没有看得出是数的字段，报告里写的数都会被判成没有出处。在它前面接取数的节点"
                                 "（要算比率、增幅再加口径卡），连线连到它；要写的数本来就是运行输入的话，在入口字段上写明数值"
                                 "默认值，报告用 [[i:字段名]] 引用"})
    return out


def authored_issues(spec: GraphSpec, sources: list[Any], baseline: list[dict[str, Any]] | None = None,
                    *, checkers: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """助手搭图时要打回、运行时却不挡的写法，按 error 交回模型改：

    - sql_unknown_column：调用工具节点的 SQL 里有数据源结构（schema_cache）里查不到的列——凭空
      猜的列名跑起来只会「no such column」，还白跑前面的步骤
    - sql_source_prefix：表名前面写了数据源名（sql_source_prefix）——数据源名被数据库当成库名，跑起来报库不存在；
      认列的检查按表名找表时会去掉前缀，拦不住它
    - parse_model_text：整形节点按 JSON 解析 agent / llm 写的文字（validate 里只是 warning：旧图照跑）
    - report_no_source：报告撰写节点的上游没有任何证据来源（运行时只给一条警告，报告里的数全没有出处）。
      metrics_from 指向不是口径卡的节点，validate 本来就报 error，自查照样交回去

    另外，给了 checkers（各数据源基于数据目录的 SQL 检查器）时，调用工具节点里写死的 SQL 再对照目录查一遍
    （code 是 data/sqlcheck.CHECK_CODES 里的规则编号），级别照检查结果：error 交回模型改，warning、info
    由调用方放进交付的问题清单。列名都对不上的 SQL 先改列名，这一轮不叠加目录检查。Agent 节点的 SQL 要到
    运行时才写出来，这里拿不到，由数据源查询工具在执行后检查。
    """
    changed = _changed(spec, baseline)
    out: list[dict[str, Any]] = []
    out += _unsourced_reports(spec, changed)
    out += _issuance_promises(spec, changed)
    for issue in text_parse_issues(spec):
        if issue.node_id in changed:
            # 模型的文字放在模板引号里的那种，改法是 | json，不是换结构化输出
            # 给人看的是 validate 那句原话；交回模型改时再补一句该怎么做（for_model）
            tail = "。不要用整形节点解析 agent / llm 的文字" if "按出处核对字段" in issue.message else ""
            out.append({**issue.model_dump(), "level": "error", "code": "parse_model_text",
                        **({"for_model": issue.message + tail} if tail else {})})
    by_name = {getattr(r, "name", None): r for r in sources}
    for node in spec.nodes:
        if node.id not in changed or node.type != NodeType.TOOL:
            continue
        source = by_name.get(_source_of(str(node.config.get("tool") or "")) or "")
        args = node.config.get("args")
        sql = args.get("sql") if isinstance(args, dict) else None
        if source is None or not str(node.config.get("tool") or "").startswith("db_query__") \
                or not isinstance(sql, str):
            continue
        if prefixed := sql_source_prefix(sql, source):
            name, bare = source.name, prefixed[0].split(".", 1)[1]
            out.append({"level": "error", "node_id": node.id, "edge_id": None, "field": "args.sql",
                        "code": "sql_source_prefix",
                        "message": f"SQL 里的表 {'、'.join(prefixed[:5])} 前面写了数据源名「{name}」：数据库会把它当成库名，"
                                   f"运行时报库不存在。去掉「{name}.」，表名照表结构写（如 {bare}）",
                        "for_model": f"SQL 里的表 {'、'.join(prefixed[:5])} 前面写了数据源名「{name}」。工具 db_query__{name} "
                                     f"已经连到这个数据源，数据库里没有叫「{name}」的库或 schema，这样写跑起来会报库不存在。"
                                     f"去掉「{name}.」，表名照表结构里的写（如 {bare}）"})
            continue
        found = sql_unknown_columns(sql, source)
        if not found:
            out += _catalog_issues(node.id, sql, (checkers or {}).get(source.name))
            continue
        unknown, known = found
        listed = "、".join(known[:20]) + (f" 等 {len(known)} 列" if len(known) > 20 else "")
        out.append({"level": "error", "node_id": node.id, "edge_id": None, "field": "args.sql",
                    "code": "sql_unknown_column",
                    "message": f"SQL 中的列 {'、'.join(unknown[:5])} 在数据源「{source.name}」的表结构中不存在（相关表的列："
                               f"{listed}）。请按表结构修改列名，不确定时可先查看表结构",
                    "for_model": f"SQL 里的 {'、'.join(unknown[:5])} 在数据源「{source.name}」的结构里查不到（这几张表的列："
                                 f"{listed}）。照结构里的列名改；拿不准就先用 db_schema__{source.name} 查表结构，"
                                 "别凭空猜列名"})
    return out


#: 人工审批的标题、说明里不该出现的说法：出具要成果节点配了出具契约、从已发布版本发起的正式运行才谈得上
_ISSUANCE_PROMISE = "正式出具"


def _issuance_promises(spec: GraphSpec, changed: set[str]) -> list[dict[str, Any]]:
    """这一轮写的人工审批节点在标题、说明里许诺「正式出具」（human_formal_issuance）。

    真实运行里一次探索运行的审批写着「批准后才会正式出具」，审批人以为自己在签发正式的东西。画布上试运行、问数据
    都是探索运行，成果节点也常常没有出具契约；同一张图发布后发起的正式运行才谈得上出具。审批说明写「批准后输出」，
    这次是哪一类运行由审批卡标明。"""
    out: list[dict[str, Any]] = []
    for node in spec.nodes:
        if node.id not in changed or node.type != NodeType.HUMAN:
            continue
        field = next((key for key in ("title", "message")
                      if _ISSUANCE_PROMISE in str(node.config.get(key) or "")), None)
        if field is None:
            continue
        out.append({
            "level": "error", "node_id": node.id, "edge_id": None, "field": field, "code": "human_formal_issuance",
            "message": f"人工审批「{node.title}」的{'标题' if field == 'title' else '说明'}写了「{_ISSUANCE_PROMISE}」。"
                       "只有成果节点配了出具契约、从已发布版本发起的正式运行才会出具，画布上试运行和问数据都是探索运行，"
                       "批准后只是输出。请改成「批准后输出」这类说法",
            "for_model": f"人工审批的 {field} 里不要写「{_ISSUANCE_PROMISE}」：只有成果节点配了出具契约、从已发布版本发起的"
                         "正式运行才会出具，这张图在画布上试运行、问数据时都是探索运行，批准后只是输出。改成「批准后输出」"
                         "「批准后继续」这类说法，运行类别由审批卡标明",
        })
    return out


def _catalog_issues(node_id: str, sql: str, checker: Any) -> list[dict[str, Any]]:
    """一个调用工具节点的 SQL 对照数据目录查出来的问题，形状和别的自查问题一样（node_id、field 都在），
    另带 table、column、relation_id、sql_excerpt 给界面定位。"""
    if checker is None:
        return []
    return [{**check, "node_id": node_id, "edge_id": None, "field": "args.sql"} for check in checker.check_dicts(sql)]


# --------------------------------------------------------------------------
# SQL 里的列名对照数据源结构（sql_unknown_columns）在 data/sqlcheck_columns.py：表结构变化的影响面也用它
# --------------------------------------------------------------------------

def sql_source_prefix(sql: str, source: Any) -> list[str]:
    """SQL 里 FROM / JOIN 后面写成「数据源名.表名」的表（去重、按出现顺序）。

    数据源名是在 AgentLab 里给连接起的名字，不是库名、schema：取数工具 db_query__<数据源名> 已经连到这个数据源，再写
    前缀，数据库会把它当成库名（MySQL 的 Unknown database、SQLite 的 unknown database、PostgreSQL 的 schema 不存在）。
    认列的检查（introspect.find_table）找表时会去掉前缀，拦不住它。数据源名恰好也是库名、schema 的不算：连接里填的库、
    探查的 schema、候选 schema、表结构里本来就带这个前缀的表。"""
    from app.data.guard import _blank_quoted, strip_comments

    name = str(getattr(source, "name", "") or "")
    if not name:
        return []
    cache = getattr(source, "schema_cache", None) or {}
    real = {str(x).lower() for x in (getattr(source, "database", None), cache.get("schema"),
                                     *(cache.get("available_schemas") or [])) if x}
    if name.lower() in real or any(str(t).lower().startswith(f"{name.lower()}.") for t in cache.get("tables") or {}):
        return []
    # 抹掉注释、模板写法和字符串，带引号的标识符留着（`shop`.`orders` 一样要认）
    text = _blank_quoted(re.sub(r"\{\{.*?\}\}", " 0 ", strip_comments(sql), flags=re.S), identifiers=False)
    pattern = re.compile(rf"\b(?:from|join)\s+[`\"\[]?{re.escape(name)}[`\"\]]?\s*\.\s*[`\"\[]?([^\s`\"\[\].,()]+)", re.I)
    return list(dict.fromkeys(f"{name}.{m.group(1)}" for m in pattern.finditer(text)))


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
                message=f"使用了限定范围之外的数据源 {'、'.join(outside)}：本轮只允许使用 {'、'.join(sorted(scope))}",
            ))
    return out


#: 交回模型改时补在问题后面的一句「该怎么做」，按问题代码。界面上只显示问题本身（message）
_FIX_TAIL = {"datasource_out_of_scope": "，改用它们的 db_query__ / db_schema__ 工具"}


def _issue_dicts(issues: list[ValidationIssue], code: str) -> list[dict[str, Any]]:
    tail = _FIX_TAIL.get(code)
    return [{**i.model_dump(), "code": code, **({"for_model": i.message + tail} if tail else {})} for i in issues]


def _issue_line(issue: dict[str, Any]) -> str:
    """交回模型的修正请求里，一条问题一行，节点 id 在前。

    写给模型的改法（for_model：「照结构里的列名改；拿不准就先用 db_schema__x 查」这类）和给人看的问题（message）
    分开存：界面只显示 message，交回模型时有 for_model 用它。键不叫 fix：前端的问题条目里 fix 是修复 id。"""
    text = issue.get("for_model") or issue["message"]
    return f"「{issue['node_id']}」{text}" if issue.get("node_id") else text


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
        now = f" {'、'.join(c['after'])}" if c["after"] else "无"
        said = ("本轮要求中没有提到移除工具" if len(unasked) == len(c["removed"])
                else f"本轮要求中没有提到移除 {'、'.join(unasked)}")
        cost = ("没有绑定工具时，它无法查询数据库，只能假设调用结果" if not c["after"]
                else f"{'、'.join(unasked)} 将无法调用")
        out.append({
            "level": "warning", "node_id": c["node_id"], "edge_id": None,
            "code": "tools_dropped", "field": c["field"],
            "message": f"{who}的工具从 {'、'.join(c['before'])} 变为{now}。"
                       f"{said}，请确认是否误删：{cost}",
        })
    return out


def _repair_request(
    nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], errors: list[dict[str, Any]], *, looks: str = "",
) -> str:
    """自查交回修正的请求：图、问题，和这一轮查看过的结果（look 文档 D8：和起草时看到的是同一份文字）。

    修正轮和起草用同一条 system；查看结果在起草那段对话里，修正是另起的一段，所以原样带上，模型不用再看一遍。
    修正时还可以再看 1 次（比如 sql_unknown_column 指向了一张没看过的表）。猜错的列名由 sql_unknown_column 的
    for_model 带回那几张表的真实列。
    """
    graph = {"nodes": list(nodes.values()), "edges": edges}
    return (
        "这是你刚搭好的工作流：\n"
        f"{json.dumps(_slim(graph), ensure_ascii=False, indent=2)}\n\n"
        "上线前自查（和真正运行时同一套规则）发现下面这些问题，不改的话这张图跑不起来：\n"
        + "\n".join(f"- {_issue_line(i)}" for i in errors)
        + "\n\n只修这些问题，别的不要动。用操作流输出修正：改节点用 update_node"
          "（config 只写要改的字段，没写的保持原样；要删掉一个字段写 null）；"
          "缺节点、缺边就补上。最后一行输出 done。"
        + (f"\n\n这一轮查看过的结构（和起草时看到的是同一份）：\n{looks}" if looks else "")
    )


@router.post("/generate-stream")
async def generate_stream(payload: GenerateIn):
    """流式版生成：SSE 逐操作推送，前端边收边把节点摆上画布。

    生成过程整个在 answer_ops 里（评测直接迭代它，和问数据页走同一条路），这里只包一层 SSE。开流前的拒绝（模型没配好、
    对话读不了、限定的数据源都不在）发生在 answer_ops 交出第一条操作之前：先取第一条，拒绝照旧是 HTTP 错误，不进流。
    """
    from fastapi.responses import StreamingResponse

    ops = answer_ops(payload)
    first = await ops.__anext__()

    async def event_stream():
        yield _sse(first)
        async for op in ops:
            yield _sse(op)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def answer_ops(payload: GenerateIn, *, catalog_view: Any = None, analysis_tools: bool = True):
    """生成过程：按需求起草、按需查看、自查修正、排版校验，逐条交出操作（op dict）。问数据页（经 SSE）和评测（直接迭代）
    走的是这同一条路（细案 01 做法 2）。

    读到 look 就停下、查看、接着写，查看记录作为 look 操作交出（前端存进这一轮的 meta.looks）。结束时（done 或流断）交一条
    final：累积的图排版、校验后整体给出——过程可见性来自操作流，最终质量仍由和手工编排相同的 layout + validate 保证。模型
    问了用户（ask）的，这一轮到此为止，不自查修正、不自动运行。

    开流前的准备（取数据源、读目录检查器、取助手模型、拼历史、组查看服务）用自己的短会话，在交出第一条操作之前做完：
    拒绝在这里抛 HTTPException（调用方没迭代到第一条就知道）。catalog_view 是这一轮读哪一份数据目录（data/catalog_view.py：
    查看的表详情、自查的 SQL 检查器都按它），不给是当前目录。

    analysis_tools 为假是评测「关掉分析工具」那一臂（分析工具开、关对照，chat/drive.TurnDriver 的 analysis_tools 透传过来）：
    工具清单不列五个分析工具，节点手册、问数据的包装里只讲分析能力的那几段不给，问题意图那条自查不提；图里照样绑了分析工具
    的按「绑定的工具不存在」挡（_analysis_off_issues）。缺省为真，和以前一字不差。

    final 带 next（chat/turn.next_step：run 运行、ready 只建不跑、fail 判失败），问数据页照它走。最后多交一条 usage
    （providers/usage.py）：{op: usage, by_model, input_tokens, output_tokens, calls, cost_usd,
    unpriced_calls}——这一轮助手建图（含挑表、接着写、自查修正）花了多少。前面每一条和抽出来之前逐条一样。运行不在这里，
    不计。
    """
    from app.api.deps import restricted
    from app.chat import turn
    from app.data.catalog_view import CatalogView
    from app.data.sqlcheck import load_checkers
    from app.db.base import SessionLocal
    from app.providers import usage

    view = CatalogView.parse(catalog_view)
    async with SessionLocal() as session:
        # 带了对话的，先核对这个人能不能用它（D-d）：历史、上一轮的图都从它来
        await _ensure_conversation_access(session, payload.conversation_id)
        # 开登录后的 user 角色（D-c）：图只能用 chat_policy 放行的节点和工具，提示词先说、自查按同一个判断挡
        chat_user = restricted()
        sources = await _sources(session, payload.datasource_ids)
        scope = {r.name for r in sources} if payload.datasource_ids else None
        # 基于数据目录的 SQL 检查器：在开流之前读好目录（流里不再碰这个会话），自查和收尾共用
        checkers = await load_checkers(session, sources, catalog_view=view)
        nodes, edges = _start_graph(payload)
        # 用户摆好的坐标，收尾排版时原样留着
        pinned = _pinned_positions(payload.base_graph)

        try:
            model, model_id = await _op_model(session, payload)
        except ProviderNotConfigured as e:
            raise HTTPException(400, _unconfigured(e)) from e

        human = _with_history(_graph_request(payload, analysis_tools=analysis_tools),
                              await _history_section(session, payload.conversation_id))
        # 兜底用：模型只发了改动操作时，把它们重放到这张图上
        prev_graph = await _previous_graph(session, payload.conversation_id)
        # 比对工具绑定的基准：画布上是改之前的图；问数据页是上一轮的图（沿用它的节点 id）
        baseline = copy.deepcopy(
            (payload.base_graph or {}).get("nodes") or (prev_graph or {}).get("nodes") or [])
        # 查看服务：读目录、读能力登记在开流之前做完。现有工作流（画布上的原图、问数据追问时上一轮的图）查过的表列在概览里。
        # 问数据另看已发布口径、可以 use_caliber 选用（next-round 02 做法 4）：只给搭建者及以上——使用者的图里看得到拷进来的
        # 取数 SQL（口径的 SQL 可能写着遮罩值），这一波不给
        kinds = ("table", "capability", "caliber") if payload.intent == "answer" and not chat_user \
            else ("table", "capability")
        looks = await _look_service(session, payload, sources, graph=payload.base_graph if (
            payload.base_graph or {}).get("nodes") else prev_graph, catalog_view=view, kinds=kinds)
    # 这一份 system 一直用到最后：接着写、自查修正都沿用，不换
    system = _graph_system(sources, scope, await copilot_context.section(looks), looks, chat_user=chat_user,
                           analysis_tools=analysis_tools)
    applier = GraphApplier(nodes, edges, composer=copilot_context.composer(looks))
    started = time.monotonic()
    gen = Generation(model, system=system, applier=applier, looks=looks, started=started,
                     checker=GraphChecker(applier, scope=scope, sources=sources, baseline=baseline, checkers=checkers,
                                          chat_user=chat_user, question=_question_of(payload),
                                          analysis_tools=analysis_tools))

    async def steps():
        yield {"op": "model", "model": model_id}
        patches = 0
        try:
            async for op in _with_heartbeat(gen.draft(human), started=started):
                if op.get("op") == "catalog_patch":
                    # 目录修改提案：核对后转给前端做成卡片，人点「保存」才写。不进图，也不算这一轮的图操作。
                    # reply 先压着（applier.replied）：前端收到 reply 就收尾，提案得排在它前面转出
                    if (checked := await catalog_patch_op(op, sources)) is not None:
                        patches += 1
                        yield checked
                    continue
                yield op
            if applier.replied is not None:
                # 这句话不需要工作流：后面那套排版校验对着一张空图跑，只会得到"图是空的，先拖一个节点进来"
                yield applier.replied
                return
            if gen.asked is not None and not applier.effective:
                # 先问清楚才能动手，什么都还没改：这一轮到此为止。照样发一条 final 收尾，带上问题（asked）和没动过的图：
                # 前端据此把这一轮记成「助手反问」、问题进历史（以前流就这么断了，问数据页一直停在「进行中」），
                # 不运行、不重排。图给一份对象（画布上原来的图、问数据上一轮的图，都没有就是空图），老前端照样认得
                asked_final = _asked_final(gen.asked, payload.base_graph, prev_graph)
                yield {**asked_final, "next": turn.next_step(asked_final)}
                return
            # 只发了目录修改提案，既没回答也没改图（用户只是在说一条数据事实）：补一句回答收尾，
            # 不能按下面的「模型没有给出任何修改」报错——提案已经摆在用户面前了
            if patches and all(o.get("op") == "plan" for o in applier.seen):
                yield {"op": "reply", "text": PATCH_ONLY_REPLY}
                return
            # 流正常结束，却没有 done、也没有一个操作能认（格式跑偏，每一行都被跳过）：
            # 往下走就是拿原图去自查、交付，界面上写「无需修改」——而模型什么都没说
            if gen.asked is None and all(o.get("op") == "plan" for o in applier.seen):
                # 只写了思考（deepseek 这类推理模型偶发）、或者格式跑偏：带机读码，问数据页和评测按 chat/turn 的规矩自动
                # 重来一次（error.next = retry），还不行再照实报
                # 错误详情里记诊断（结束原因、正文和思考的长度、跳过的行和原因、工具调用……，不记原文），重来时交给助手的
                # 原因换成一句纠正（Generation.empty）
                raise gen.empty(hint="画布未修改。可以重试，或把需求描述得更具体")
        except CopilotIncomplete as e:
            failed = {"op": "error", "message": f"助手本轮未完成：{e}", "hint": e.hint, "detail": e.detail,
                      "reason": e.correction or str(e), **({"code": e.code} if e.code else {}),
                      **({"diagnostics": e.diagnostics} if e.diagnostics else {})}
            yield {**failed, "next": turn.next_step(failed)}
            return
        except Exception as e:  # noqa: BLE001
            reason, hint = explain_error(e)
            failed = {"op": "error", "message": f"助手本轮未完成：{reason}", "hint": hint, "detail": raw_error(e),
                      "reason": reason}
            yield {**failed, "next": turn.next_step(failed)}
            return

        # 兜底：一个操作都没落地，而上一轮有图。
        #
        # 这说明模型把这一轮当成"改上一张图"，只发了 update_node/add_edge，
        # 而累加器是从空图起步的，那些操作全落在不存在的节点上。上面的 prompt
        # 已经要求它完整重发，但那是约定，不是保证——约定落空的代价是用户收到
        # 一句"图是空的，先拖一个节点进来"，而他只是问了句追问。
        if not nodes and prev_graph and applier.seen:
            applier.replay_onto(prev_graph)

        # 自查：按运行时同一套规则把图过一遍，挡住运行的问题交回模型改，改完再查。
        # 以前这些问题照样交付、照样自动开跑——跑到那一步才炸（开发库里四次运行死在
        # 循环 / 分支条件里套了 {{ }}），前面的步骤白跑，报错还停在半路
        async for op in _with_heartbeat(gen.repair(), phase="repairing"):
            yield op
        remaining = _blocking_issues(nodes, edges, scope, sources=sources, baseline=baseline, checkers=checkers,
                                     chat_user=chat_user, analysis_tools=analysis_tools)
        # 工具绑定变化不挡运行，但要在自查这一步就说出来：工具被改没了的图照样
        # 能跑，只是跑出来的是模型「假设」查过库的答案
        changes = tool_changes(baseline, list(nodes.values()))
        warnings = dropped_tool_warnings(changes, payload.instruction)
        # 工具节点写死的 SQL 查的区间里表里没有数据（data/coverage.py）：自查时交回模型改过一轮，仍在的不挡运行，留在检查
        # 结果里当提醒（模型照要求在说明里告诉用户）
        warnings += await gen.checker.coverage(level="warning") if isinstance(gen.checker, GraphChecker) else []
        # 分析类问题、查库的 Agent 没绑分析工具：同上，交回模型改过一轮仍在的不挡运行，留作提醒
        warnings += gen.checker.analysis_gaps(level="warning") if isinstance(gen.checker, GraphChecker) else []
        # 建完要不要跑，由模型在 done.run 里表态；问了用户的不跑：要等他回答
        autorun = applier.autorun and gen.asked is None
        if remaining:
            # 改不好也照实交付、列出问题，但不自动运行：明知跑不起来的图，开跑只会
            # 在半路报一个用户看不懂的错
            autorun = False
            yield {"op": "check", "status": "failed", "issues": remaining, "warnings": warnings}
        elif remaining is not None:
            yield {"op": "check", "status": "passed", "repaired": gen.repaired, "warnings": warnings}

        # 收尾：排版 + 校验，把最终图整体交付。改图时旧节点留在原处，只排新节点
        try:
            spec, layout = _layout_keeping(
                GraphSpec.model_validate({"nodes": list(nodes.values()), "edges": edges}), pinned,
            )
            # 建议（info，比如「可以升级为可追溯结构」）不进这一轮的问题清单：图刚按规则搭好，画布自己的校验会列出来
            issues = [i.model_dump() for i in validate_graph(spec).issues if i.level != "info"]
            issues += _issue_dicts(scope_issues(list(nodes.values()), scope),
                                   "datasource_out_of_scope")
            # 自查没改好的猜列名留在画布上：运行时不挡（结构缓存可能旧了），但要看得见。
            # 对照数据目录的 SQL 检查照原级别列出：warning、info 本来就不返工，改不好的 error 也在这里看得见
            authored = authored_issues(spec, sources, baseline, checkers=checkers)
            issues += [{**i, "level": "warning"} for i in authored if i["code"] == "sql_unknown_column"]
            issues += [i for i in authored if i["code"] in CHECK_CODES]
            # 使用者发起不了的图（D-c）：自查没改好的照实列出，界面据此不运行、说清原因
            issues += _chat_user_issues(nodes, edges) if chat_user else []
            # 关掉分析工具的那一臂里还绑着分析工具的（自查没改好）：同上，照实列出，不运行
            issues += [] if analysis_tools else _analysis_off_issues(nodes)
            # 跳过的节点要说出来，不能安静地少一步。code 给前端认：这一类要单独
            # 提示「少了一步」，不能和普通校验警告混在一起
            issues += [
                {"level": "warning", "node_id": None, "edge_id": None,
                 "code": "unknown_node_type", "type": t,
                 "message": f"模型使用了不存在的节点类型「{t}」，已跳过这一步"}
                for t in dict.fromkeys(applier.skipped_types)
            ]
            final = {
                "op": "final",
                "graph": spec.model_dump(mode="json"),
                "issues": issues + warnings,
                "explanation": applier.explanation,
                "autorun": autorun,
                "layout": layout,
                # 改图回执：哪些节点、成员的工具变了。画布上看不出来，得明说
                "tool_changes": changes,
                # 改了图又问了用户（这一轮照样结束、不自动运行）：问题跟着 final 交付，前端记进这一轮
                **({"asked": _asked_of(gen.asked)} if gen.asked is not None else {}),
            }
            # 建完之后怎么走（chat/turn.next_step）：run 运行、ready 只建不跑、fail 判失败。问数据页照它走，评测用同一个函数
            final["next"] = turn.next_step(final)
        except Exception as e:  # noqa: BLE001
            final = {"op": "error", "message": f"模型返回的工作流结构有误：{graph_error(e)}",
                     "hint": "请重试，或把需求描述得更具体", "detail": raw_error(e)}
        yield final

    spent = usage.meter()
    async for op in usage.metered(steps(), spent):
        yield op
    yield {"op": "usage", **spent.summary()}


# --------------------------------------------------------------------------
# 发布前自动修复的兜底：确定性修复修不了的 error 交给 Copilot，产出同样只是预览
# --------------------------------------------------------------------------

_LEVEL_WORD = {"published": "已发布", "governed": "受管"}

#: 发布前修复和一键升级共用的硬性约定。拆开写是为了升级的兜底换掉「只修下面列出的问题」那一条，
#: 其余的（不许降低要求、要人拿主意的不替人选、只许用哪几种操作）一字不差
_RULES_HEAD = """\
硬性约定（违反任何一条，这次修改整个作废）：
"""
_NO_LOWERING = """\
- 不许降低要求：不删节点、不删连线，add_node 不许用图里已有的节点 id（那等于把旧节点删了重建），也不换节点类型；
  不删掉或清空出具契约，不删 required 里的指标，不把 strict 改成 false，不往 allow_numbers 里加数，不替人打开 cells；
  审批策略只能往严里改（每次调用都审批 > 仅危险工具需要审批 > 全部自动放行），不放宽、不删掉，更不改成 never；
  不取消子工作流钉住的版本；发布级别不是你能改的。结构要动（比如换掉协作团队），输出 question 说明怎么拆，不要自己动手；
  报告撰写节点的 numbers、on_violation、claims、analysis_claims 只能往严里改（numbers: strict、on_violation: fail、
  claims: require_citation、analysis_claims: strict），不放宽、不删掉；analysis_claims 从宽到严是 off < check < strict，
  已经是 strict 的不改回 check；claims 从宽到严是 off < require_citation < judge，已经是 judge 的不改回
  require_citation；judge 缺预算（judge.max_cost_usd）时输出 question 问写多少、还是写 null 不限，不替人定；
  judge.on_unsupported 是 withhold 的不改回 degrade、不删掉；update_node 会把 judge 整个换掉，补预算时把原来的
  judge（包括从全图默认继承来的）整份带上，只加 max_cost_usd；契约里的 claims 也一样；
  不关 agent 的 cite_fields，不删它的 output_schema
"""
_ASK_HUMAN = """\
- 要人拿主意的，不替人选、不编：required 该包括哪些指标、几张口径卡该用哪张、钉哪个版本、协作团队拆成哪几个
  固定步骤、沙箱代码节点是在取数还是在计算（不许自己把 evidence_role 设成 source）……每一处输出一行
  {"op":"question","node_id":"节点 id","text":"要问的话，把候选列出来"}，那一处不改
"""
_ONLY_LISTED = """\
- 只修下面列出的问题，别的不要动
"""
_OPS_ONLY = """\
- 可用操作只有 update_node（config 只写要改的字段，没写的原样保留）、add_node、add_edge 和 question；
  不要输出 remove_node、remove_edge、reply
"""
_PLAN_DONE = """\
- 第一行 plan，最后一行 done，done 的 explanation 用一两句话说清改了什么、为什么"""

_ASSIST_RULES = _RULES_HEAD + _NO_LOWERING + _ASK_HUMAN + _ONLY_LISTED + _OPS_ONLY + _PLAN_DONE


def _publish_fix_request(graph: dict[str, Any], errors: list[dict[str, Any]], level: str) -> str:
    return (
        "这是当前的工作流：\n"
        f"{json.dumps(_slim(graph), ensure_ascii=False, indent=2)}\n\n"
        f"它要发布成「{_LEVEL_WORD.get(level, level)}」级别，发布前检查（和真正发布同一套规则）还拦着下面这些问题：\n"
        + "\n".join(f"- {_issue_line(i)}" for i in errors)
        + f"\n\n{_ASSIST_RULES}"
    )


def _assist_system(task: str, sources: list[Any], datasources: str, looks: Any) -> str:
    """发布前修复、升级证据的 system：模型、工具目录、操作流协议都和画布上改图是同一套，另可以输出 question。"""
    return (
        f"你是一个 agent 工作流编排专家。现在要做的是：{task}，以操作流的方式输出修改。\n\n"
        f"{NODE_REFERENCE}\n\n可用工具：\n{_tool_catalog(sources)}{datasources}\n\n"
        f"{_STREAM_PROTOCOL}\n\n另外可以输出 question 操作：{{\"op\":\"question\",\"node_id\":\"…\",\"text\":\"…\"}}"
        + (f"\n\n{looks.guide()}" if looks.kinds else "")
    )


async def _with_system_look(looks: Any, request: str, graph: dict[str, Any], sources: list[Any], *,
                            need: str) -> str:
    """不需要人参与的路径由系统代为查看要修的 SQL 用到的表（look 文档 D5），结果接在请求后面：要查哪些表是确定的，
    系统直接查能省掉一轮。记录和模型自己查看的一样，留在 looks.records 里。"""
    looked = await copilot_context.system_look(looks, graph, sources, need=need)
    return f"{request}\n\n{looked.text}" if looked is not None else request


def _questions(applier: GraphApplier, before_nodes: dict[str, dict[str, Any]],
               asked: dict[str, Any] | None = None) -> list[str]:
    """模型要人拿主意的话，原样交给人看，前面标上是哪个节点的事（界面上认节点标题，不认 id）。模型用 ask 问的（这一轮
    就此结束）也算一条。"""
    out: list[str] = []
    ops = [*applier.questions, *([{"text": asked.get("question")}] if asked else [])]
    for op in ops:
        text = str(op.get("text") or "").strip()
        where = before_nodes.get(str(op.get("node_id") or ""))
        title = str((where.get("data") or {}).get("label") or where.get("id")) if where else ""
        if text:
            out.append(f"「{title}」：{text}" if title and title not in text else text)
    return out


async def assist_publish_fix(
    session: AsyncSession, graph: dict[str, Any], *, level: str,
    provider: str | None = None, model: str | None = None,
) -> dict[str, Any]:
    """把挡住发布的 error 交给助手修。模型、工具目录、操作流协议都和画布上改图是同一套（生成过程 copilot_gen）。

    要修的 SQL 用到的表由系统代为查看（look 文档 D5），模型仍然可以再看。拿回的操作在副本上应用，再按同一套规则复核
    （_blocking_issues 带上发布级别：validate、门禁，以及这一轮改过的节点里有没有凭空猜的列名）：error 变少、没有新
    error、没有降低要求的改动（autofix.forbidden_changes）才采纳，否则整个作废并写明原因。模型认为要人拿主意的，放进
    questions 原样交给人，不硬改。

    返回 {accepted, reason, summary, questions（一句一条的文字）, graph, ops, looks}。不落库。
    """
    from app.engine.autofix import forbidden_changes, judge

    from app.data.sqlcheck import load_checkers

    sources = await _sources(session, None)
    # 和发布接口同一套 SQL 检查：受管级别里 SQL 的 error 也是挡住发布的问题，一并交给助手改
    checkers = await load_checkers(session, sources)
    base = copy.deepcopy(graph)
    before_nodes = {str(n["id"]): n for n in copy.deepcopy(base.get("nodes") or []) if isinstance(n, dict)}
    before_edges = [e for e in copy.deepcopy(base.get("edges") or []) if isinstance(e, dict)]
    baseline = list(copy.deepcopy(before_nodes).values())
    errors = _blocking_issues(before_nodes, before_edges, sources=sources, baseline=baseline, level=level,
                              defaults=base.get("defaults"), checkers=checkers) or []
    out: dict[str, Any] = {"accepted": False, "reason": None, "summary": "", "questions": [], "graph": graph,
                           "ops": [], "looks": []}
    if not errors:
        out["summary"] = "没有阻止发布的错误，无需交给助手"
        return out
    request = GenerateIn(instruction="发布前自动修复", provider=provider, model=model)
    try:
        chat, _ = await _op_model(session, request)
    except ProviderNotConfigured as e:
        out["summary"] = _unconfigured(e)
        return out
    looks = await _look_service(session, request, sources, graph=base, kinds=("table",))
    system = _assist_system("让一张工作流过发布前检查", sources, await copilot_context.section(looks), looks)
    human = await _with_system_look(looks, _publish_fix_request(base, errors, level), base, sources,
                                    need=copilot_context.SYSTEM_LOOK_FIX_NEED)
    nodes = copy.deepcopy(before_nodes)
    edges = copy.deepcopy(before_edges)
    # 目录修改提案只在用户亲口陈述数据事实时才有；这里的「用户消息」是系统拼的修复请求，提了也不转
    applier = GraphApplier(nodes, edges, patches=False, questions=True)
    gen = Generation(chat, system=system, applier=applier, looks=looks)
    try:
        async for _op in gen.draft(human):
            pass
    except Exception as e:  # noqa: BLE001 - 修不成就照实说，确定性修复的结果照样交给人
        out["summary"] = f"助手本轮未完成：{explain_error(e)[0]}"
        return out
    finally:
        out["looks"] = list(looks.records)
    out["questions"] = _questions(applier, before_nodes, gen.asked)
    out["summary"] = applier.explanation or str((applier.plan or {}).get("summary") or "")
    if not applier.effective:
        # 没动图：要么全是要人拿主意的问题，要么它没给出能用的修改。都不算「拒绝」
        out["summary"] = out["summary"] or ("助手认为这些问题需要由你决定" if out["questions"]
                                            else "助手本轮没有给出修改")
        return out

    proposed = _proposed_graph(base, nodes, edges)
    reasons = forbidden_changes(base, proposed, applier.tried)
    after = _blocking_issues(_nodes_by_id(proposed), proposed.get("edges") or [], sources=sources,
                             baseline=baseline, level=level, defaults=proposed.get("defaults"), checkers=checkers)
    if reasons:
        out["reason"] = "助手的修改降低了要求，已作废：" + "；".join(reasons)
    elif after is None:
        out["reason"] = "助手修改后的工作流结构无法解析，已作废"
    elif why := judge(errors, after):
        out["reason"] = f"助手的修改未能改善工作流，已作废：{why}"
    else:
        out.update(accepted=True, graph=proposed, ops=applier.effective)
    return out


def _nodes_by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["id"]): n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}


def _proposed_graph(base: dict[str, Any], nodes: dict[str, dict[str, Any]],
                    edges: list[dict[str, Any]]) -> dict[str, Any]:
    """Copilot 改完的整张图：原图里前端的东西（坐标、连线 id、视口）原样留着，新节点找空处摆。"""
    out = copy.deepcopy(base)
    out["nodes"] = list(nodes.values())
    out["edges"] = edges
    pinned = _pinned_positions(base)
    fresh = [nid for nid in nodes if nid not in pinned]
    if fresh:
        try:
            laid, _ = _layout_keeping(GraphSpec.model_validate({"nodes": out["nodes"], "edges": edges}), pinned)
            where = {n.id: n.position.model_dump() for n in laid.nodes}
            for nid in fresh:
                nodes[nid]["position"] = where.get(nid, nodes[nid].get("position"))
        except Exception:  # noqa: BLE001 - 摆不好就留在原点，复核那一步会报结构问题
            pass
    return out


# --------------------------------------------------------------------------
# 一键升级为可追溯结构：先按确定的规则改写（engine/upgrade.py），assist 时再请 Copilot 把纯算术的沙箱代码
# 改写成口径卡表达式。和发布前修复一样只给预览：不改库、不保存，人确认后前端走现有的保存
# --------------------------------------------------------------------------


class UpgradeIn(BaseModel):
    graph: dict[str, Any]
    #: 确定性改写之后，再请 Copilot 做语义层面的改写（纯算术的沙箱代码改成口径卡表达式），同样只是预览
    assist: bool = False
    #: 复核门禁用的发布级别。受管级别下新写出来的报告撰写节点另写明门禁 G3 要的三项
    level: Literal["published", "governed"] = "published"
    provider: str | None = None
    model: str | None = None


_UPGRADE_TASK = """\
逐个判断上面这些沙箱代码节点：
- 代码只是对上游已有的数做纯算术（加减乘除、round、min / max、比较、按条件取值），没有查库、调接口、读写文件，
  也不用随机数、当前时间：把口径卡里读它的那几个指标的 expression 改成直接用上游的值算——上游是查库的调用工具
  节点就写 cell(nodes.查库节点id, 行, '列名')，是开了 cite_fields 的 agent 就写 vars.<它的 assign_to>.字段。
  改完口径卡不再读这个代码节点；代码节点本身留在图上（不删节点、不删连线，要不要删由人决定）
- 不是纯算术的（在取数、调接口、解析文件、用了随机数或时间……）保留原样，输出一行 question 说明为什么没改，
  并请人确认它是不是在取数
"""


def _upgrade_assist_request(graph: dict[str, Any], spec: GraphSpec, feeders: list[Any]) -> str:
    from app.engine.upgrade import metric_feeders

    lines = []
    for code, cards in feeders:
        # 和 compute_feeders 同一条追法：指标读的是整形、校验节点的，追到头是这段代码也要列出来
        reads = [f"口径卡「{card.title}」（{card.id}）的指标 {card.config['metrics'][i].get('id')}："
                 f"{card.config['metrics'][i].get('expression')}"
                 for card in cards for i, producer in metric_feeders(spec, card) if producer.id == code.id]
        lines.append(f"- 「{code.title}」（{code.id}）被这些指标读到：" + "；".join(dict.fromkeys(reads)))
    return (
        "这是一张已经按确定的规则升级成可追溯结构的工作流：\n"
        f"{json.dumps(_slim(graph), ensure_ascii=False, indent=2)}\n\n"
        "口径卡读的是下面这些沙箱代码节点的产出（evidence_role 不是 source）：沙箱里算出来的数核对不了出处。\n"
        + "\n".join(lines) + f"\n\n{_UPGRADE_TASK}\n"
        + _RULES_HEAD + _NO_LOWERING + _ASK_HUMAN
        + "- 只改上面这些口径卡指标的 expression，别的不要动\n" + _OPS_ONLY + _PLAN_DONE
    )


def _kept_code(spec: GraphSpec) -> list[str]:
    """assist 之后仍然喂着口径卡的计算角色沙箱代码：不是纯算术、或者 Copilot 拿不准的，保留原样并警告。"""
    from app.engine.upgrade import compute_feeders

    return [f"「{code.title}」（沙箱代码）未改写为口径卡表达式，已保留原样：口径卡"
            + "、".join(f"「{c.title}」" for c in cards)
            + "读取的数字无法追溯出处。若该节点负责取数，请将「证据角色」设为「取数」；若负责计算，请把计算移到口径卡的表达式中"
            for code, cards in compute_feeders(spec)]


async def assist_upgrade(
    session: AsyncSession, graph: dict[str, Any], *, level: str,
    provider: str | None = None, model: str | None = None,
) -> dict[str, Any]:
    """升级的语义层：请助手把喂口径卡的纯算术沙箱代码改写成口径卡表达式，不是纯算术的保留并警告。

    模型、工具目录、操作流协议都和画布上改图是同一套（生成过程 copilot_gen）；取数节点用到的表由系统代为查看（look
    文档 D5）。改完按搭图时同一套自查循环修正（只把这一轮冒出来的 error 交回去，图上原来就有的不算），最后复核：没有
    降低要求的改动（autofix.forbidden_changes，这里没有例外：助手不换类型、不删连线）、没有比确定性改写的结果更糟
    （autofix.worse）才采纳，否则整个作废并写明原因。

    返回 {ok, accepted, reason, summary, questions, warnings, graph, ops, looks}。不落库。
    """
    from app.engine.autofix import _sig, forbidden_changes, worse
    from app.engine.upgrade import compute_feeders

    base = copy.deepcopy(graph)
    spec = GraphSpec.model_validate(base)
    out: dict[str, Any] = {"ok": True, "accepted": False, "reason": None, "summary": "", "questions": [],
                           "warnings": [], "graph": graph, "ops": [], "looks": []}
    feeders = compute_feeders(spec)
    if not feeders:
        out["summary"] = "口径卡的输入均不来自负责计算的沙箱代码，无需交给助手改写"
        return out
    from app.data.sqlcheck import load_checkers

    sources = await _sources(session, None)
    # 基于数据目录的 SQL 检查器：自查和最终判断都带上，和发布门禁一个口径（受管级别下一对多关联后求和这类 error
    # 会挡发布）。不带的话，助手改出这种 SQL 照样被采纳，交出去的图要到发布时才被拦
    checkers = await load_checkers(session, sources)
    before_nodes = {str(n["id"]): n for n in copy.deepcopy(base.get("nodes") or []) if isinstance(n, dict)}
    before_edges = [e for e in copy.deepcopy(base.get("edges") or []) if isinstance(e, dict)]
    baseline = list(copy.deepcopy(before_nodes).values())
    defaults = base.get("defaults")
    base_errors = _blocking_issues(before_nodes, before_edges, sources=sources, baseline=baseline, level=level,
                                   defaults=defaults, checkers=checkers) or []
    out["warnings"] = _kept_code(spec)
    request = GenerateIn(instruction="升级为可追溯结构", provider=provider, model=model)
    try:
        chat, _ = await _op_model(session, request)
    except ProviderNotConfigured as e:
        out.update(ok=False, summary=_unconfigured(e))
        return out
    looks = await _look_service(session, request, sources, graph=base, kinds=("table",))
    system = _assist_system("把一张工作流里喂给口径卡的纯算术沙箱代码改写成口径卡表达式", sources,
                            await copilot_context.section(looks), looks)
    human = await _with_system_look(looks, _upgrade_assist_request(base, spec, feeders), base, sources,
                                    need=copilot_context.SYSTEM_LOOK_UPGRADE_NEED)
    nodes = copy.deepcopy(before_nodes)
    edges = copy.deepcopy(before_edges)
    # 同发布修复：这里没有用户陈述的数据事实，目录修改提案不收
    applier = GraphApplier(nodes, edges, patches=False, questions=True)
    # 自查：和搭图同一套规则，只把这一轮冒出来的 error 交回去改（原来就有的不归这一轮管）
    checker = GraphChecker(applier, sources=sources, baseline=baseline, level=level, defaults=defaults,
                           checkers=checkers, known={_sig(e) for e in base_errors})
    gen = Generation(chat, system=system, applier=applier, checker=checker, looks=looks)
    try:
        async for _op in gen.draft(human):
            pass
        async for _op in gen.repair():
            pass
        if gen.repair_error is not None:
            raise gen.repair_error
    except Exception as e:  # noqa: BLE001 - 改不成就照实说，确定性改写的结果照样交给人
        out.update(ok=False, summary=f"助手本轮未完成：{explain_error(e)[0]}", looks=list(looks.records))
        return out
    out["looks"] = list(looks.records)
    out["questions"] = _questions(applier, before_nodes, gen.asked)
    out["summary"] = applier.explanation or str((applier.plan or {}).get("summary") or "")
    if not applier.effective:
        out["summary"] = out["summary"] or ("助手认为这些代码不是纯算术，均保留原样" if out["questions"]
                                            else "助手本轮没有给出修改")
        return out

    proposed = _proposed_graph(base, nodes, edges)
    reasons = forbidden_changes(base, proposed, applier.tried)
    after = _blocking_issues(_nodes_by_id(proposed), proposed.get("edges") or [], sources=sources,
                             baseline=baseline, level=level, defaults=proposed.get("defaults"), checkers=checkers)
    if reasons:
        out["reason"] = "助手的修改降低了要求，已作废：" + "；".join(reasons)
    elif after is None:
        out["reason"] = "助手修改后的工作流结构无法解析，已作废"
    elif why := worse(base_errors, after):
        out["reason"] = f"助手的修改未能改善工作流，已作废：{why}"
    else:
        out.update(accepted=True, graph=proposed, ops=applier.effective,
                   warnings=_kept_code(GraphSpec.model_validate(proposed)))
    out["ok"] = out["reason"] is None
    return out


def _placed(base: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    """升级新插进来的节点找空处摆，原有节点一个都不挪（坐标常带着业务上的分区）。"""
    known = {n.get("id") for n in base.get("nodes") or [] if isinstance(n, dict)}
    fresh = {n["id"] for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id") not in known}
    if not fresh:
        return graph
    out = copy.deepcopy(graph)
    try:
        laid, _ = _layout_keeping(GraphSpec.model_validate(out), _pinned_positions(base))
    except Exception:  # noqa: BLE001 - 摆不好就留在规则给的位置上
        return out
    where = {n.id: n.position.model_dump() for n in laid.nodes}
    for n in out["nodes"]:
        if n.get("id") in fresh:
            n["position"] = where.get(n["id"], n.get("position"))
    return out


def _node_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copilot 那一段的逐项变更和确定性改写同一个形状：新加的节点记成 field 为 node、after 是整个节点。"""
    out = []
    for c in changes:
        if c.get("field") is None and isinstance(c.get("after"), dict):
            c = {**c, "field": "node", "after": {"id": c["node_id"], "label": c["node_title"], **c["after"]}}
        out.append({**c, "rule": "assist"})
    return out


@router.post("/upgrade-evidence")
async def upgrade_evidence(payload: UpgradeIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """一键升级为可追溯结构（证据五期）：返回升级后的整张图和逐项变更——只是预览，不改库、不保存。

    返回 {graph, changes, ops, notes, issues, applied, rejected, assist, ok}：changes / notes / rejected 的形状见
    engine/upgrade.upgrade_for_evidence；issues 是升级后的图按 level 重跑 validate 和门禁的结果（和发布同一套口径）；
    assist 只在请求带 assist: true 时有：{ok, summary, questions, warnings, looks}，warnings 是保留没改的沙箱代码，
    looks 是这一次的查看记录（系统按取数节点的 SQL 代为查看的那一条在前）。
    """
    from app.engine.autofix import diff_changes
    from app.engine.governance import publish_issues
    from app.engine.upgrade import upgrade_for_evidence

    try:
        GraphSpec.model_validate(payload.graph)
    except Exception as e:  # noqa: BLE001
        return {"graph": payload.graph, "changes": [], "ops": [], "notes": [], "applied": [], "rejected": [],
                "assist": None, "ok": False,
                "issues": [{"level": "error", "node_id": None, "edge_id": None, "field": None, "code": None,
                            "fix": None, "message": f"无法解析工作流结构：{graph_error(e)}"}]}
    out = upgrade_for_evidence(payload.graph, level=payload.level)
    out["graph"] = _placed(payload.graph, out["graph"])
    out["assist"] = None
    if payload.assist:
        helped = await assist_upgrade(session, out["graph"], level=payload.level, provider=payload.provider,
                                      model=payload.model)
        out["assist"] = {"ok": helped["ok"], "summary": helped["summary"], "questions": helped["questions"],
                         "warnings": helped["warnings"], "looks": helped["looks"]}
        if helped["accepted"]:
            from app.engine.upgrade import compute_feeders

            out["changes"] += _node_changes(
                diff_changes(out["graph"], helped["graph"], fix_id="assist", label="助手的修改"))
            out["ops"] += helped["ops"]
            out["applied"].append("assist")
            out["graph"] = helped["graph"]
            # R5 的建议是改写之前给的：口径卡已经不读的那几个代码节点，不再建议人去标 source
            still = {code.id for code, _ in compute_feeders(GraphSpec.model_validate(out["graph"]))}
            out["notes"] = [n for n in out["notes"] if n["rule"] != "R5" or n["node_id"] in still]
        elif helped["reason"]:
            out["rejected"].append({"fix_id": "assist", "reason": helped["reason"]})
    from app.data.sqlcheck import graph_sources, load_checkers_by_name

    upgraded = GraphSpec.model_validate(out["graph"])
    checkers = await load_checkers_by_name(session, graph_sources(upgraded.nodes))
    issues = publish_issues(upgraded, level=payload.level, checkers=checkers)
    out["issues"] = [i.model_dump() for i in issues]
    out["ok"] = not any(i.level == "error" for i in issues)
    return out


# --------------------------------------------------------------------------
# 应用润色（总方案 A8）：应用草稿由工作流版本确定性派生（app/apps/derive.py），助手只改布局、文案、控件选择。
# 走同一个生成过程：系统先代为查看钉住的版本，模型可以再看别的版本和能力；落地调 app/apps/ops.apply_app_op，
# 自查调 app/apps/lint.lint_app。岗位绑定只能由人来做：助手只留空并在说明里提醒
# --------------------------------------------------------------------------

APP_SYSTEM_PROMPT = """\
你是应用润色助手。应用是已发布工作流版本的使用面：入口（发起用的表单）、视图（看运行产物）、实例、待办。
应用草稿已经按钉住的工作流版本确定性派生好了，你只负责润色：布局（页面、分区、分栏、页签、组件顺序）、文案（标题、
说明、按钮文字）、控件选择（表单字段的 x-widget、x-order，或者换成目录里更合适的组件）。

硬性约定：
- 只能用下面组件目录里的组件，props 按目录里写的字段和类型写，不要编造组件和属性
- 绑定（bind）只取钉住的版本里已有的值：绑定的节点、指标、报告都要在钉住的版本里存在，拿不准就先 look 那一版；
  文字组件里不要写数据的数字，也不要拼算式——数字只来自绑定，界面层不做任何计算。说明流程规则（审批的金额线、时限）
  时写的数要和钉住的版本里节点配置写的一致；用户要写的规则在钉住的版本里没有，不要照写，在 done 的说明里告诉用户
  流程并不按这条执行、要先改工作流
- 可见性表达式（visible）只能用 run、input、metric、instance、task、user、view 这几个根名字，只能比较、取值、in 和
  and / or / not，不能写算术
- 只改呈现：不改钉住的版本（pins）、入口（entries）、视图（views）、岗位（slots），不改表单的数据约束（字段、类型、
  必填、枚举、取值范围）；表单只改已有表单的 title、description、x-order、x-widget、x-enum-labels。越界的操作不会被采纳
- 岗位（slots）绑定到哪些人或组只能由人来做：岗位留着，不要替人绑定；done 的说明里提醒还有哪些岗位等着绑定
- 能按常理定的直接改，必须由用户拿主意的才 ask"""

APP_PROTOCOL_PROMPT = """\
输出格式（严格遵守）：
- 每行一个完整的 JSON 对象，除此之外不输出任何东西——不要 markdown 围栏、不要解释文字，也不要写成工具调用的格式（这里没有工具调用接口）
- 可用操作：
  {"op":"plan","summary":"一句话说明打算怎么改"}          ← 第一行
  {"op":"app_add","parent":"组件 id 或页面 id","index":0,"component":{"id":"…","type":"组件名","props":{},"bind":{}}}
  {"op":"app_update","id":"组件 id","props":{},"bind":{},"visible":"…"}   ← 只写要改的那几项
  {"op":"app_remove","id":"组件 id"}
  {"op":"app_move","id":"组件 id","parent":"组件 id 或页面 id","index":0}
  {"op":"app_page","page":{"id":"…","title":"…"}}          ← 新增页面，或改已有页面的标题
  {"op":"app_set","path":"forms|task_ui","key":"…","value":{}}   ← forms：已有表单的整份新写法，只改外观键；task_ui：按人工节点改待办外观
  {"op":"done","explanation":"两三句话说明改了什么，还有哪些岗位等着绑定"}    ← 最后一行
- 组件 id 用简短英文小写下划线，同一个应用里不重复；已有的组件用 app_update 改，不要删了重建
- 这句话不需要改应用（只是问个问题）时，第一行也是最后一行输出 {"op":"reply","text":"直接回答的内容"}
- 要看工作流版本、能力的详情时输出一行 look（用法见下），输出之后就停下等查看结果，拿到结果再接着写
- 必须先问清用户才能动手时，输出一行 {"op":"ask","question":"要问的话"}，这一轮就此结束：已经输出的改动照常交付"""

APP_CATALOG_PROMPT = "组件目录（只能用这些）：\n{components}"
APP_COMPONENT_PROMPT = "- {name}（{label}，{category}）{children}：绑定 {bindings}；props {props}"
APP_DERIVED_PROMPT = "按钉住的版本确定性派生出的草稿（参考：组件该绑哪些节点、指标、报告，以它为准）：\n{drafts}"
APP_REQUEST_PROMPT = "这是应用现在的草稿：\n{spec}\n\n请按下面的要求润色它（只输出改动操作）：\n{instruction}"
APP_LOOK_WHEN_PROMPT = """\
什么时候看：要绑定的节点、指标、报告在钉住的版本里有没有、叫什么，以系统代为查看的那一版为准，不够再看；要放动作状态、
写入相关的组件时看能力。只改文案、顺序的不看"""
#: 系统代为查看钉住的版本时写进记录的需求（界面上显示）
APP_SYSTEM_LOOK_NEED = "应用引用的工作流版本"
APP_OPS = ("app_add", "app_update", "app_remove", "app_move", "app_page", "app_set")


class AppStreamIn(BaseModel):
    instruction: str = Field(min_length=1, description="用自然语言说想怎么改这个应用")
    #: 哪个应用。spec / bindings 不给时读它的草稿
    app_id: str | None = None
    #: 编辑器里当前的草稿（agentlab.app/1）：编辑器里还没保存的改动以它为准
    spec: dict[str, Any] | None = None
    #: 草稿里的岗位绑定（不进 spec）：应用检查按它判断岗位绑没绑
    bindings: dict[str, Any] | None = None
    conversation_id: str | None = None
    provider: str | None = None
    model: str | None = None


def _app_catalog() -> str:
    """组件目录：名字、中文名、类别、能不能有子组件、允许的绑定、props（只给字段和类型，app/apps/catalog.py）。"""
    from app.apps.catalog import CATEGORY_LABEL, catalog_payload

    lines = []
    for c in catalog_payload()["components"]:
        props = {k: v.get("type") or v.get("enum") or "string" for k, v in
                 ((c.get("props") or {}).get("properties") or {}).items()}
        lines.append(APP_COMPONENT_PROMPT.format(
            name=c["name"], label=c["label"], category=CATEGORY_LABEL.get(c["category"], c["category"]),
            children="，可放子组件" if c.get("children") else "",
            bindings=" / ".join(c.get("bindings") or []) or "无", props=json.dumps(props, ensure_ascii=False)))
    return APP_CATALOG_PROMPT.format(components="\n".join(lines))


async def _derived_drafts(session: AsyncSession, pins: list[dict[str, Any]]) -> str:
    """每个钉住的版本确定性派生出的草稿，给模型对照着润色。派生是参考：哪一个派生不出来（版本没了、派生出错）就不给
    那一份，不挡润色（P3），应用检查照样把问题列出来。"""
    from app.apps import derive

    drafts = []
    for pin in pins:
        try:
            spec = await derive.derive_app(session, str(pin.get("workflow_id")), int(pin.get("version") or 0))
        except Exception:  # noqa: BLE001 - 派生只是参考
            logger.exception("应用润色：钉住的版本 %s v%s 派生不出草稿", pin.get("workflow_id"), pin.get("version"))
            continue
        drafts.append(f"「{pin.get('alias')}」：{json.dumps(spec, ensure_ascii=False)}")
    return APP_DERIVED_PROMPT.format(drafts="\n".join(drafts)) if drafts else ""


async def _require_app_manage(session: AsyncSession, app: Any) -> None:
    """应用润色要这个应用的「管理」权限（同编辑草稿，§3.12）：搭建者不是创建者、也没有管理授权的，只能看不能改。"""
    from app.api.deps import current_principal
    from app.apps.access import app_role, at_least

    role = await app_role(session, current_principal(), app)
    if role is None:
        raise HTTPException(404, "应用不存在，可能已被删除")
    if not at_least(role, "manage"):
        raise HTTPException(403, "需要这个应用的「管理」权限")


@router.post("/app-stream")
async def app_stream(payload: AppStreamIn, session: AsyncSession = Depends(get_session)):
    """应用润色：SSE 推送，形状同 /generate-stream。

    事件：model；系统代为查看钉住版本的 look（by=system）；模型的 plan、应用操作（app_add / app_update / app_remove /
    app_move / app_page / app_set，已经落到服务端维护的草稿上）、look、ask、thinking、heartbeat；自查的 check；最后
    final {spec, issues, explanation}——spec 是润色后的整份草稿，issues 是应用检查的全部问题（只有人能处理的，比如
    岗位没绑，也在里面）。只回答了一句的发 reply 收尾；问了用户、什么都还没改的发完 ask 收尾。不落库：编辑器收到
    final 后照常保存草稿。
    """
    from fastapi.responses import StreamingResponse

    from app.db.models import App
    from app.look.capabilities import CapabilityKind
    from app.look.pick import Picker
    from app.look.service import LookService
    from app.look.versions import VersionKind

    row = await session.get(App, payload.app_id) if payload.app_id else None
    if payload.app_id and row is None:
        raise HTTPException(404, "应用不存在，可能已被删除")
    if row is not None:
        await _require_app_manage(session, row)
    conversation = await _ensure_conversation_access(session, payload.conversation_id)
    if conversation is not None and (conversation.kind != "app" or row is None or conversation.app_id != row.id):
        # 应用助手的历史只取这个应用自己的那段对话：拿问数据、画布、别的应用的对话来拼，模型看到的「上一轮」是别处的事
        raise HTTPException(409, "这段对话不是这个应用的助手对话，请从应用编辑器里重新打开助手")
    spec = copy.deepcopy(payload.spec if payload.spec is not None else (row.draft if row is not None else None))
    if not spec:
        raise HTTPException(400, "没有可润色的应用草稿。请先从工作流版本生成草稿，再请助手润色")
    bindings = payload.bindings if payload.bindings is not None else (row.draft_bindings if row is not None else {})
    request = GenerateIn(instruction=payload.instruction, provider=payload.provider, model=payload.model,
                         conversation_id=payload.conversation_id)
    try:
        model, model_id = await _op_model(session, request)
    except ProviderNotConfigured as e:
        raise HTTPException(400, _unconfigured(e)) from e
    picker = Picker(await copilot_model_spec(session, request, max_tokens=copilot_context.PICK_MAX_TOKENS))
    looks = LookService([await VersionKind.load(session, picker=picker), await CapabilityKind.load(picker=picker)])
    pins = [p for p in spec.get("pins") or [] if isinstance(p, dict)]
    overview = await looks.overview()
    system = "\n\n".join(part for part in (
        APP_SYSTEM_PROMPT, _app_catalog(), await _derived_drafts(session, pins), overview, APP_PROTOCOL_PROMPT,
        looks.guide(), APP_LOOK_WHEN_PROMPT) if part)
    human = _with_history(
        APP_REQUEST_PROMPT.format(spec=json.dumps(spec, ensure_ascii=False, indent=2), instruction=payload.instruction),
        await _history_section(session, payload.conversation_id))
    applier = AppApplier(spec)
    checker = AppChecker(applier, bindings=bindings, scope=(row.scope if row is not None else None) or "shared")
    started = time.monotonic()
    gen = Generation(model, system=system, applier=applier, checker=checker, looks=looks, started=started)

    async def event_stream():
        yield _sse({"op": "model", "model": model_id})
        try:
            # 系统先代为查看钉住的版本（look 文档 D5 的做法）：润色绕不开它们，直接查省掉一轮
            first = human
            targets = [{"kind": "workflow_version", "source": str(p.get("workflow_id")), "name": f"v{p.get('version')}"}
                       for p in pins]
            if targets:
                looked = await looks.look({"op": "look", "need": APP_SYSTEM_LOOK_NEED, "targets": targets}, by="system")
                if looked.record is not None:
                    yield _sse(looked.record)
                first = f"{human}\n\n{looked.text}"
            async for op in _with_heartbeat(gen.draft(first), started=started):
                yield _sse(op)
            if applier.replied is not None:
                yield _sse(applier.replied)
                return
            if gen.asked is not None and not applier.effective:
                return
            if gen.asked is None and gen.done is None and not applier.effective:
                # 流正常结束，却没有 done、也没有一个操作落地：不能按「无需修改」交付
                raise gen.empty(hint="应用草稿未修改。可以重试，或把要求描述得更具体")
            async for op in _with_heartbeat(gen.repair(), phase="repairing"):
                yield _sse(op)
            remaining = await checker.issues()
            yield _sse({"op": "check", "status": "failed" if remaining else "passed", "repaired": gen.repaired,
                        **({"issues": remaining} if remaining else {})})
            # 越界被拒的操作照实列给人看（警告级），排在应用检查的问题前面
            yield _sse({"op": "final", "spec": applier.spec, "issues": [*applier.refusals(), *await checker.lint()],
                        "explanation": applier.explanation})
        except CopilotIncomplete as e:
            yield _sse({"op": "error", "message": f"助手本轮未完成：{e}", "hint": e.hint, "detail": e.detail})
        except Exception as e:  # noqa: BLE001
            reason, hint = explain_error(e)
            yield _sse({"op": "error", "message": f"助手本轮未完成：{reason}", "hint": hint, "detail": raw_error(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class FromRunIn(BaseModel):
    run_id: str
    name: str = ""
    #: 问数据回答上点「做成应用」时带上：这次运行出自哪段对话的哪一轮，记进新工作流的来源（workflows.origin）
    conversation_id: str | None = None
    turn_id: str | None = None
    #: 固化成口径草稿（next-round 02 做法 8，app/ledger/certify.freeze）：报告引用的查询变成固定 SQL 的取数节点，跨轮变化
    #: 的字面量成为参数（不保留 Agent）。peers 是同一组（同一个 SQL 形状）的别的运行，认哪些字面量位置变了
    freeze: bool = False
    peers: list[str] = Field(default_factory=list, max_length=50)


@router.post("/from-run")
async def extract_template(
    payload: FromRunIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """轨迹 → 模板：把一次探索性运行实际走过的路径提取成草稿工作流。

    探索层的价值不在单次答案，而在"跑通的路径能沉淀为资产"。
    提取是确定性的：只保留真正执行过的节点和它们之间的边，分支上
    没走的岔路剪掉；输入值参数化成 input 字段默认值。产物落成 draft，
    人审、改名、发布之后才成为正式模板——提取器是模板的作者，不是发布者。

    新工作流记下来源（workflows.origin = {run_id, conversation_id?, turn_id?}）：从这里发布、再做成应用的那条路
    （总方案 A8）事后查得到它出自哪次运行、哪段对话。
    """
    from app.db.models import WorkflowVersion
    from app.core.artifact_store import graph_hash

    # 对话只是记进来源：不能用的不让记（D-d），不存在的照旧记下 id
    await _ensure_conversation_access(session, payload.conversation_id)
    if payload.freeze:
        # 固化成口径草稿：取数是固定的 SQL（以后比对认得出等价），不是保留 Agent 的执行路径
        from app.ledger.certify import CertifyRefused, freeze

        try:
            return await freeze(session, payload.run_id, peers=payload.peers, name=payload.name,
                                conversation_id=payload.conversation_id, turn_id=payload.turn_id)
        except CertifyRefused as e:
            raise HTTPException(e.status_code, e.message) from e
    try:
        path = await extract_run_path(session, payload.run_id)
    except PathRefused as e:
        raise HTTPException(e.status_code, e.message) from e
    run, graph = path.run, path.graph
    workflow = Workflow(
        name=payload.name or f"{run.workflow_name or '探索'}（提取的模板）",
        description=f"从运行 {run.id[:8]} 的实际执行路径提取（{path.nodes} 个节点），经人工审核后发布",
        graph=graph,
        tags=["extracted"],
        status="draft",
        origin={"run_id": run.id,
                **({"conversation_id": payload.conversation_id} if payload.conversation_id else {}),
                **({"turn_id": payload.turn_id} if payload.turn_id else {})},
    )
    session.add(workflow)
    await session.flush()
    session.add(
        WorkflowVersion(
            workflow_id=workflow.id, version=1, graph=graph,
            graph_hash=graph_hash(graph), note=f"从运行 {run.id} 提取",
        )
    )
    await session.commit()
    return {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "nodes": path.nodes,
        "edges": path.edges,
        "dropped_nodes": path.dropped,
        "source_run": run.id,
    }


class PathRefused(Exception):
    """提取不了：status_code / message 就是 HTTP 回的状态码和 detail。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class RunPath:
    """从一次运行提取出的执行路径：草稿图（已排版）和节点、连线、剪掉的节点数。"""

    run: Any
    graph: dict[str, Any]
    nodes: int
    edges: int
    dropped: int


async def extract_run_path(session: AsyncSession, run_id: str) -> RunPath:
    """一次成功运行实际走过的路径 → 草稿图（确定性）：只留执行过的节点和它们之间的边，分支上没走的岔路剪掉，输入值
    参数化成入口字段的默认值。不落库。「提取成工作流」（/from-run）和蓝图的「从问数据的回答开始」共用。"""
    from sqlalchemy import select as _sel

    from app.api.deps import can_read_run, current_principal
    from app.db.models import Run, RunEvent

    run = await session.get(Run, run_id)
    # 读不到的运行当它不存在：别人的个人应用刷新、个人蓝图试运行只给拥有者（auth/access.private_owner），提取出的图就是
    # 那张蓝图的内容，管理员、搭建者也不给
    if not run or not await can_read_run(current_principal(), run):
        raise PathRefused(404, "运行记录不存在，可能已被删除")
    # 没跑通的路径提出来是半截骨架：失败节点之后的步骤一步都不在里面，
    # 人拿去审改时却看不出它缺了什么
    if run.status != "succeeded":
        raise PathRefused(
            409, "这次运行未成功结束，提取出的执行路径不完整。"
                 "请先让它成功运行一次，再从该次运行提取",
        )
    if not (run.graph or {}).get("nodes"):
        raise PathRefused(400, "这次运行没有保存工作流快照，无法提取")

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
        raise PathRefused(400, "这次运行没有节点执行记录")

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
    return RunPath(run=run, graph=draft_spec.model_dump(mode="json"), nodes=len(kept_nodes), edges=len(kept_edges),
                   dropped=len(spec.nodes) - len(kept_nodes))


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
    #: 这一轮已经自动试了几次（从 0 数，同 chat/turn.retry_after_review）：结果里的 retry_instruction 据此给不给。
    #: 不传按第一次算
    attempt: int = Field(default=0, ge=0)


@router.post("/review")
async def review_run(
    payload: ReviewIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """接住一次编排的产出，判断它能不能就这么交给用户（规则见 review_of_run）。这里只核对能不能读这次运行。"""
    from app.api.deps import PRIVATE_RUN, can_read_run, current_principal
    from app.db.models import Run

    run = await session.get(Run, payload.run_id)
    if not run:
        raise HTTPException(404, "运行记录不存在，可能已被删除")
    if not await can_read_run(current_principal(), run):
        raise HTTPException(403, PRIVATE_RUN)
    return await review_of_run(session, run, question=payload.question, provider=payload.provider,
                               model=payload.model, always=payload.always, attempt=payload.attempt)


async def review_of_run(session: AsyncSession, run: Any, *, question: str, provider: str | None = None,
                        model: str | None = None, always: bool = False, attempt: int = 0) -> dict[str, Any]:
    """复核一次运行（问数据页经 /copilot/review、评测直接调，同一个函数）。

    「跑完了」和「答得对」是两回事。运行状态只说明流程走完了，而过程中检索
    降级、工具报错、agent 步数用满这些事，原先一件都不会进到答案里——用户拿到
    一个看起来很完整的结论，却无从知道它是在什么条件下得出的。

    规则层先扫一遍（engine/review.py:scan），没扫出东西就到此为止、直接返回，
    不花一次模型调用；扫出来了才叫模型重组答案并把异常说破。

    结果是 ReviewResult.as_dict()，另带两项：
    - retry_instruction：要不要带着原图重来一次（chat/turn.retry_after_review，按 attempt 算这一轮还能不能再试）——要的话
      是交给助手的那句话（chat/turn.retry_instruction），不要是 null。问数据页照它走；
    - usage：复核这一次模型调用花了多少（providers/usage.py 的汇总，没调模型时是全 0），这一轮的花费 = 助手 + 运行 + 复核。
    """
    from app.chat import turn
    from app.db.models import RunEvent
    from app.engine import review as rv
    from app.providers import usage

    rows = list((await session.execute(
        select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
    )).scalars())

    output = run.output if isinstance(run.output, dict) else None
    signals = rv.scan(rows, output)
    answer = rv.answer_text(output)
    spent = usage.meter()

    def done(result: Any) -> dict[str, Any]:
        out = result.as_dict()
        out["retry_instruction"] = (
            turn.retry_instruction(question, turn.review_reason(out), turn.has_nodes(run.graph))
            if turn.retry_after_review(out, attempt) else None)
        out["usage"] = spent.summary()
        return out

    if not signals and not always:
        return done(rv.ReviewResult(verdict="ok", signals=[]))

    try:
        spec = await copilot_model_spec(session, GenerateIn(
            instruction="review", provider=provider, model=model,
        ), max_tokens=4096)
        chat_model, _ = await get_chat_model(session, spec)
    except (ProviderNotConfigured, HTTPException) as e:
        # 没配模型不该让这一轮挂掉：规则层已经知道出了什么事，照样说得出话
        return done(rv.ReviewResult(
            verdict="annotated",
            note="；".join(s.detail for s in signals) or (
                _unconfigured(e) if isinstance(e, ProviderNotConfigured) else str(e.detail)
            ),
            retry=rv.should_retry(signals),
            signals=signals, severity=rv.worst(signals),
        ))

    with usage.metering(spent):
        result = await rv.review(
            chat_model, question=question, answer=answer, signals=signals,
            # 报告撰写节点产出的答案逐段对应着证据文档：只能加说明，不能改写
            locked=bool(output and output.get("_evidence")),
            # 已经处理好的（首稿没过、重写后已全部通过……）：不是缺口，照实告诉复核模型
            settled=rv.notes(rows),
        )
    return done(result)
