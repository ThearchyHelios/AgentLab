from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import func, select

from app.core.crypto import encrypt
from app.db.base import SessionLocal
from app.db.models import Provider, Skill, Workflow, WorkflowVersion
from app.engine.schema import GraphSpec
from app.providers import catalog

logger = logging.getLogger(__name__)


def _n(node_id: str, node_type: str, label: str, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": 0, "y": 0},
        "data": {"label": label, "config": config},
    }


def _e(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    edge: dict[str, Any] = {"source": source, "target": target}
    if handle:
        edge["sourceHandle"] = handle
    return edge


# --------------------------------------------------------------------------
# 内置模板：打开就有得玩，也是各种节点用法的活文档
# --------------------------------------------------------------------------

TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "① 最小问答",
        "description": "一个模型节点跑通全流程。先从这个开始改。",
        "tags": ["入门"],
        "nodes": [
            _n("start", "input", "提问", fields=[{"name": "question", "required": True,
                "description": "你想问什么"}]),
            _n("answer", "llm", "回答", system="你是一个耐心、准确的助手。",
               prompt="{{ input.question }}", assign_to="answer"),
            _n("done", "output", "成果", fields=[{"name": "回答", "value": "{{ vars.answer }}"}]),
        ],
        "edges": [_e("start", "answer"), _e("answer", "done")],
    },
    {
        "name": "② 研究助手（Agent + 工具）",
        "description": "Agent 自主决定搜什么、抓哪篇，再汇总成带出处的简报。",
        "tags": ["agent", "工具"],
        "nodes": [
            _n("start", "input", "研究主题", fields=[{"name": "topic", "required": True}]),
            _n("research", "agent", "调研 Agent",
               system="你是严谨的研究员。先搜索再抓取原文，只写有依据的内容。",
               prompt="围绕这个主题做调研：{{ input.topic }}",
               tools=["web_search", "web_fetch", "current_time"],
               max_steps=8, approval="never", assign_to="findings"),
            _n("brief", "llm", "写成简报",
               system="你是一位把复杂信息讲清楚的编辑。",
               prompt="把下面的调研结果整理成中文简报，分「结论 / 关键发现 / 待确认」三段，"
                      "每条结论后面标注来源链接：\n\n{{ vars.findings }}",
               assign_to="brief"),
            _n("done", "output", "简报", fields=[
                {"name": "简报", "value": "{{ vars.brief }}"},
                {"name": "调研过程", "value": "{{ nodes.research.tool_calls | json }}"}]),
        ],
        "edges": [_e("start", "research"), _e("research", "brief"), _e("brief", "done")],
    },
    {
        "name": "③ 带人工审批的发布流",
        "description": "起草 → 人工把关 → 通过就发布、驳回就改写。演示中断与断点恢复。",
        "tags": ["人工审批", "分支"],
        "nodes": [
            _n("start", "input", "主题", fields=[{"name": "topic", "required": True}]),
            _n("draft", "llm", "起草", system="你是品牌文案。",
               prompt="为「{{ input.topic }}」写一条 100 字以内的对外公告。",
               assign_to="draft"),
            _n("review", "human", "人工把关", mode="approve",
               title="这条公告可以发吗？",
               message="草稿：\n\n{{ vars.draft }}"),
            _n("publish", "transform", "标记发布", mode="template",
               template="✅ 已批准发布\n\n{{ vars.draft }}", assign_to="final"),
            _n("rewrite", "llm", "按意见改写",
               prompt="下面这条公告被驳回了，理由是「{{ nodes.review.note }}」。请据此改写：\n\n{{ vars.draft }}",
               assign_to="final"),
            _n("done", "output", "成果", fields=[
                {"name": "结果", "value": "{{ vars.final }}"},
                {"name": "是否通过", "value": "{{ nodes.review.approved }}"}]),
        ],
        "edges": [
            _e("start", "draft"), _e("draft", "review"),
            _e("review", "publish", "approved"), _e("review", "rewrite", "rejected"),
            _e("publish", "done"), _e("rewrite", "done"),
        ],
    },
    {
        "name": "④ 知识库问答（RAG）",
        "description": "先检索再回答，检索不到就明说。需要先在知识库页上传文档。",
        "tags": ["RAG", "知识库"],
        "nodes": [
            _n("start", "input", "提问", fields=[{"name": "question", "required": True},
                {"name": "collection", "default": "default"}]),
            _n("search", "retrieve", "检索知识库", query="{{ input.question }}",
               collection="{{ input.collection }}", limit=5, alpha=0.5, assign_to="context"),
            _n("gate", "branch", "检索到东西了吗", mode="expression",
               cases=[{"key": "hit", "condition": "nodes.search.count > 0", "label": "有命中"}]),
            _n("answer", "llm", "基于资料回答",
               system="只根据提供的资料回答，每句话后面用 [编号] 标注依据。资料不足就直接说不确定。",
               prompt="资料：\n{{ vars.context }}\n\n问题：{{ input.question }}",
               assign_to="answer"),
            _n("miss", "transform", "没有资料", mode="template",
               template="知识库里没有找到与「{{ input.question }}」相关的内容，请先上传相关文档。",
               assign_to="answer"),
            _n("done", "output", "答案", fields=[
                {"name": "答案", "value": "{{ vars.answer }}"},
                {"name": "引用片段", "value": "{{ nodes.search.hits | json }}"}]),
        ],
        "edges": [
            _e("start", "search"), _e("search", "gate"),
            _e("gate", "answer", "hit"), _e("gate", "miss", "default"),
            _e("answer", "done"), _e("miss", "done"),
        ],
    },
    {
        "name": "⑤ 写代码跑数据（沙箱 + 校验）",
        "description": "模型写 Python，沙箱里真跑，输出还要过 JSON Schema 校验，不合格自动返工。",
        "tags": ["沙箱", "结构化"],
        "nodes": [
            _n("start", "input", "分析需求", fields=[
                {"name": "task", "required": True, "default": "统计 1 到 100 里所有质数的个数与总和"}]),
            _n("write", "llm", "写代码",
               system="你是数据工程师。只输出可直接运行的 Python 代码，不要解释、不要 markdown 围栏。"
                      "结果必须用 print(json.dumps(...)) 输出一个 JSON 对象。",
               prompt="任务：{{ input.task }}", assign_to="code"),
            _n("run", "code", "沙箱执行", language="python", code="{{ vars.code }}",
               timeout=30, network=False, fail_fast=False, assign_to="result"),
            _n("check", "validate", "校验结果", source="{{ nodes.run.stdout }}",
               max_retries=2, repair_with_llm=True, fail_fast=False,
               schema={"type": "object", "minProperties": 1}),
            _n("done", "output", "成果", fields=[
                {"name": "结果", "value": "{{ nodes.check.data | json }}"},
                {"name": "代码", "value": "{{ vars.code }}"},
                {"name": "校验通过", "value": "{{ nodes.check.valid }}"}]),
        ],
        "edges": [_e("start", "write"), _e("write", "run"), _e("run", "check"), _e("check", "done")],
    },
    {
        "name": "⑥ 多 Agent 协作",
        "description": "调度者按进展分派任务给研究员、批评家和写手，直到目标达成。",
        "tags": ["多agent"],
        "nodes": [
            _n("start", "input", "目标", fields=[{"name": "goal", "required": True}]),
            _n("team", "supervisor", "协作团队", goal="{{ input.goal }}", max_rounds=6,
               agents=[
                   {"name": "researcher", "description": "查资料、找事实",
                    "system": "你是研究员，负责收集事实和数据，给出来源。",
                    "tools": ["web_search", "web_fetch"], "max_steps": 4},
                   {"name": "critic", "description": "挑毛病、找漏洞",
                    "system": "你是批评家。指出前面工作里的事实错误、逻辑漏洞和遗漏，要具体。",
                    "tools": [], "max_steps": 2},
                   {"name": "writer", "description": "整合成最终文稿",
                    "system": "你是写手，把团队成果整合成结构清晰的中文文稿。",
                    "tools": [], "max_steps": 2},
               ], assign_to="result"),
            _n("done", "output", "成果", fields=[
                {"name": "结果", "value": "{{ vars.result }}"},
                {"name": "协作记录", "value": "{{ nodes.team.transcript | json }}"}]),
        ],
        "edges": [_e("start", "team"), _e("team", "done")],
    },
    {
        "name": "⑧ 周报（口径卡 + 三档出具）",
        "description": "模板定骨架、agent 填关节的完整示范：取数固定、口径卡算数、"
        "叙述只许引用清单里的数、出具时逐数回指——现编的数字会被点名并降档。",
        "tags": ["出具", "口径卡", "周报"],
        "nodes": [
            _n("start", "input", "周期", fields=[
                {"name": "week", "default": "2026-W37", "description": "报告周期"}]),
            # 取数节点：产出本身就是源数据（evidence_role=source），口径卡读它记得下出处
            _n("fetch", "code", "取数（数据底座）", language="python", network=False,
               assign_to="agg", evidence_role="source",
               code=(
                   "# 固定层：取数与汇总。真实场景换成对数据库/API 的查询，\n"
                   "# 结果会作为快照落工件库，出具溯源指回这里。\n"
                   "import json\n"
                   "print(json.dumps({'orders': 1234, 'amount': 45678.5,\n"
                   "                  'amount_prev': 42010.0, 'returns': 29}))"
               )),
            _n("caliber", "metrics", "周报口径卡",
               caliber="周报口径", caliber_version="v1", assign_to="m",
               metrics=[
                   {"id": "orders", "name": "订单数", "unit": "单",
                    "expression": "vars.agg.orders"},
                   {"id": "amount", "name": "销售额", "unit": "元",
                    "expression": "vars.agg.amount"},
                   {"id": "wow", "name": "环比增幅", "unit": "%",
                    "expression": "round((vars.agg.amount - vars.agg.amount_prev) / vars.agg.amount_prev * 100, 1)"},
                   {"id": "return_rate", "name": "退货率", "unit": "%",
                    "expression": "round(vars.agg.returns / vars.agg.orders * 100, 2)"},
               ]),
            _n("narrate", "llm", "叙述（关节层）",
               system="你是周报撰写人。只允许引用下面指标清单里出现的数值，"
                      "一个清单外的数字都不要写——包括字数统计、序号补充说明等元内容。"
                      "只输出周报正文本身，不要任何撰写说明。"
                      "你的自由度在于：挑哪些变化值得说、怎么归因。",
               prompt="{{ nodes.caliber.text }}\n\n请为 {{ input.week }} 写一段简短的周报正文。",
               assign_to="report"),
            _n("done", "output", "出具",
               fields=[
                   {"name": "周报", "value": "{{ vars.report }}"},
                   {"name": "指标清单", "value": "{{ nodes.caliber.text }}"}],
               contract={
                   "metrics_from": ["caliber"],
                   "narrative": "{{ vars.report }}",
                   "required": ["amount", "orders"],
                   "expected": ["wow", "return_rate"],
                   "allow_numbers": ["37", "120"],
                   "strict": False,
               }),
        ],
        "edges": [
            _e("start", "fetch"), _e("fetch", "caliber"),
            _e("caliber", "narrate"), _e("narrate", "done"),
        ],
    },
    {
        "name": "⑨ 可追溯周报",
        "description": "报告里的每个数都点得开出处：tool 节点查库、口径卡登记指标、报告撰写节点只写引用"
        "标记，数字由系统从口径卡取出来渲染，出口按引用逐段核对后三档出具。取数节点查的是示例数据源 "
        "shop 的 orders 表（week、amount、refunded 三列）：先接入你的库（或者上传一张表格、命名为 "
        "shop），再把 SQL 换成你的口径。报告撰写节点按受管要求写明了 on_violation: fail 和 claims: "
        "require_citation：探索运行里修不掉的数字违规也会让报告节点失败，而不只是标出来；正式运行里没挂引用"
        "的结论句计入缺口、按档降级。",
        "tags": ["出具", "口径卡", "周报", "证据"],
        "nodes": [
            _n("start", "input", "周期", fields=[
                {"name": "week", "default": "2026-W37", "description": "报告周期（ISO 周）"},
                {"name": "prev_week", "default": "2026-W36", "description": "对比的上一周期"}]),
            # 聚合放在 SQL 里做完，口径卡只做标量运算。列的顺序就是口径卡里 rows[0][n] 的 n
            _n("fetch", "tool", "取数（查库）", tool="db_query__shop", args={"sql": (
                "SELECT\n"
                "  SUM(CASE WHEN week = '{{ input.week }}' THEN amount ELSE 0 END) AS gmv,\n"
                "  SUM(CASE WHEN week = '{{ input.prev_week }}' THEN amount ELSE 0 END) AS gmv_prev,\n"
                "  SUM(CASE WHEN week = '{{ input.week }}' THEN 1 ELSE 0 END) AS order_cnt,\n"
                "  SUM(CASE WHEN week = '{{ input.week }}' AND refunded = 1 THEN 1 ELSE 0 END) AS refund_cnt\n"
                "FROM orders")}),
            # 查询工具交回来的是 JSON 文本 {columns, rows}，解析成对象口径卡才取得到里面的数
            _n("parse", "transform", "解析查询结果", mode="json", template="{{ nodes.fetch }}",
               assign_to="q"),
            _n("caliber", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v1",
               metrics=[
                   {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands",
                    "expression": "vars.q.rows[0][0]"},
                   {"id": "orders", "name": "订单数", "unit": "单", "format": "thousands",
                    "expression": "vars.q.rows[0][2]"},
                   # 分母为 0 时记为空值（缺输入），交给契约按 expected 降档，而不是整张卡失败
                   {"id": "wow", "name": "环比增幅", "unit": "%", "decimals": 1, "format": "plain",
                    "expression": "round((vars.q.rows[0][0] - vars.q.rows[0][1]) / vars.q.rows[0][1] * 100, 1)"
                                  " if vars.q.rows[0][1] else None"},
                   {"id": "aov", "name": "客单价", "unit": "元", "decimals": 2, "format": "thousands",
                    "expression": "round(vars.q.rows[0][0] / vars.q.rows[0][2], 2) if vars.q.rows[0][2] else None"},
                   {"id": "refund_rate", "name": "退款率", "decimals": 4, "format": "percent_of_ratio",
                    "expression": "vars.q.rows[0][3] / vars.q.rows[0][2] if vars.q.rows[0][2] else None"},
               ]),
            # 受管出具要求报告撰写节点写明最严的三项（发布门禁 G3），模板照着写，拿来就能发布成受管
            _n("write", "report", "报告撰写", metrics_from=["caliber"],
               numbers="strict", on_violation="fail", claims="require_citation",
               system="你是周报撰写人：结论先行，只写有数据支撑的判断。",
               instructions="为 {{ input.week }} 写一份简短的周报：先用一两句话总结本周，"
                            "再分点说销售额、订单、客单价和退款的变化。"),
            _n("done", "output", "出具",
               fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
               contract={
                   "report_from": "write",
                   "metrics_from": ["caliber"],
                   "required": ["gmv", "orders"],
                   "expected": ["wow", "aov", "refund_rate"],
                   "strict": True,
               }),
        ],
        "edges": [
            _e("start", "fetch"), _e("fetch", "parse"), _e("parse", "caliber"),
            _e("caliber", "write"), _e("write", "done"),
        ],
    },
    {
        "name": "⑦ 批量处理（循环）",
        "description": "对列表逐项处理再汇总。演示 loop 节点的 body / done 两个出口怎么接。",
        "tags": ["循环"],
        "nodes": [
            _n("start", "input", "条目列表", fields=[
                {"name": "items", "required": True,
                 "default": ["降低获客成本", "提升续费率", "缩短上线周期"],
                 "description": "JSON 数组或每行一条"}]),
            _n("each", "loop", "逐条循环", mode="foreach", items="{{ input.items }}",
               item_var="item", max_iterations=10),
            _n("handle", "llm", "处理单条",
               prompt="针对「{{ vars.item }}」给出一条可执行的建议，不超过 40 字。",
               assign_to="one"),
            _n("collect", "transform", "累积结果", mode="expression",
               expression="(vars.collected or '') + '· ' + str(vars.one) + '\\n'",
               assign_to="collected"),
            _n("done", "output", "汇总", fields=[
                {"name": "逐条建议", "value": "{{ vars.collected }}"},
                {"name": "处理条数", "value": "{{ nodes.each.iteration }}"}]),
        ],
        "edges": [
            _e("start", "each"),
            _e("each", "handle", "body"), _e("handle", "collect"), _e("collect", "each"),
            _e("each", "done", "done"),
        ],
    },
]


SKILLS: list[dict[str, Any]] = [
    {
        "name": "结构化分析",
        "description": "把开放问题拆成结论先行的分析",
        "instructions": (
            "分析任何问题时遵循：\n"
            "1. 先给结论，再给理由，不要铺垫\n"
            "2. 区分「事实」「推断」「假设」，并明确标注\n"
            "3. 给出至少一个反面视角\n"
            "4. 指出当前信息不足以判断的部分，不要硬答"
        ),
        "tags": ["通用"],
    },
    {
        "name": "批判性审阅",
        "description": "挑毛病而不是夸奖",
        "instructions": (
            "审阅内容时：\n"
            "1. 只指出问题，不要复述优点\n"
            "2. 每条问题写清楚「问题是什么 / 为什么是问题 / 怎么改」\n"
            "3. 按严重程度排序，最致命的放最前面\n"
            "4. 没有问题就直说没有，不要为凑数硬挑"
        ),
        "tags": ["审阅"],
    },
    {
        "name": "中文写作",
        "description": "干净、不翻译腔的中文",
        "instructions": (
            "写中文时：\n"
            "1. 不用「进行」「作出」这类空动词，直接用实义动词\n"
            "2. 少用被动句和长定语从句\n"
            "3. 术语首次出现时用括号给出英文原文\n"
            "4. 能用短句就不写长句"
        ),
        "tags": ["写作"],
    },
]


async def seed_defaults() -> None:
    """首次启动时灌入默认 provider、模板工作流和 Skill。

    已经存在的不会被覆盖 —— 这个函数每次启动都会跑，必须是幂等的。
    """
    async with SessionLocal() as session:
        # --- provider ---
        count = (await session.execute(select(func.count(Provider.id)))).scalar() or 0
        if count == 0:
            session.add(
                Provider(
                    name="Mock（无需 Key）",
                    kind="mock",
                    models=catalog.MOCK_MODELS,
                    default_model="mock-fast",
                    enabled=True,
                )
            )
            # 环境里已有 Anthropic 凭据就自动配好，省得再手填一遍
            token = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            if token:
                base_url = os.environ.get("ANTHROPIC_BASE_URL")
                is_gateway = bool(base_url) and not os.environ.get("ANTHROPIC_API_KEY")
                session.add(
                    Provider(
                        name="Anthropic" + ("（网关）" if is_gateway else ""),
                        kind="anthropic",
                        base_url=base_url,
                        api_key=encrypt(token),
                        models=catalog.ANTHROPIC_MODELS,
                        default_model="claude-opus-5",
                        enabled=True,
                        # 中转网关通常认 Authorization: Bearer 而不是 x-api-key
                        extra={"auth_style": "bearer"} if is_gateway else {},
                    )
                )
                logger.info("已从环境变量自动创建 Anthropic provider")
            if os.environ.get("OPENAI_API_KEY"):
                session.add(
                    Provider(
                        name="OpenAI",
                        kind="openai",
                        api_key=encrypt(os.environ["OPENAI_API_KEY"]),
                        models=catalog.OPENAI_MODELS,
                        default_model="gpt-5.2",
                        enabled=True,
                    )
                )
            await session.commit()

        # --- Skill ---
        existing_skills = {
            s for (s,) in await session.execute(select(Skill.name))
        }
        for skill in SKILLS:
            if skill["name"] not in existing_skills:
                session.add(Skill(**skill))
        await session.commit()

        # --- 模板工作流 ---
        from app.engine.layout import auto_layout

        existing = {n for (n,) in await session.execute(select(Workflow.name))}
        added = 0
        for template in TEMPLATES:
            if template["name"] in existing:
                continue
            spec = auto_layout(
                GraphSpec.model_validate(
                    {"nodes": template["nodes"], "edges": template["edges"]}
                )
            )
            graph = spec.model_dump(mode="json")
            workflow = Workflow(
                name=template["name"],
                description=template["description"],
                graph=graph,
                tags=template["tags"],
                is_template=True,
            )
            session.add(workflow)
            await session.flush()
            session.add(
                WorkflowVersion(workflow_id=workflow.id, version=1, graph=graph, note="内置模板")
            )
            added += 1
        if added:
            await session.commit()
            logger.info("已导入 %d 个内置模板", added)
