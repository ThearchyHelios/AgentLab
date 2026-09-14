from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Workflow
from app.engine.schema import GraphSpec, NodeType, validate_graph
from app.engine.state import message_text
from app.providers.factory import ModelSpec, ProviderNotConfigured, get_chat_model
from app.tools.registry import all_specs

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


NODE_REFERENCE = """\
可用节点类型（type 字段）：
- input：入口。config.fields = [{name, required, default, description}]
- output：出口，收集最终成果。config.fields = [{name, value}]，value 里写模板引用
- llm：单次模型调用。config: {system, prompt, model, temperature, max_tokens, assign_to, output_schema}
- agent：带工具循环的 agent。config: {system, prompt, tools:[工具名], max_steps, approval, assign_to}
- supervisor：多 agent 协作。config: {goal, agents:[{name, description, system, tools, model}], max_rounds}
- tool：直接调一个工具。config: {tool: 工具名, args: {...}, assign_to}
- code：沙箱里跑代码。config: {language: python|bash|node, code, timeout, network, assign_to}
- branch：条件分支。config: {mode: expression|llm, cases:[{key, condition, label}]}
- loop：循环。config: {mode: foreach|while, items, item_var, condition, max_iterations}
- retrieve：知识库检索。config: {query, collection, limit, assign_to}
- memory：长期记忆读写。config: {action: recall|write, query, content, scope, assign_to}
- human：人工介入。config: {mode: approve|input|edit, title, message}
- validate：JSON Schema 校验，可自动让模型修复。config: {schema, source, max_retries}
- transform：数据整形。config: {mode: expression|template|json, expression/template, assign_to}
- subgraph：嵌套另一个工作流。config: {workflow_id, input}

模板语法（在任意字符串里用）：
- {{ input.字段名 }}        入口输入
- {{ vars.变量名 }}         某节点 assign_to 写入的变量
- {{ nodes.节点id.text }}   某节点的输出
- {{ last_message }}        最近一条消息文本
- 过滤器：{{ vars.x | json }}

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


class GenerateOut(BaseModel):
    graph: dict[str, Any]
    explanation: str = ""
    issues: list[dict[str, Any]] = Field(default_factory=list)


GRAPH_SCHEMA: dict[str, Any] = {
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
                    "config": {"type": "object"},
                },
                "required": ["id", "type"],
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
    tool_list = "\n".join(
        f"- {name}（{spec.category}）：{spec.description}"
        for name, spec in sorted(all_specs().items())
    )

    system = (
        "你是一个 agent 工作流编排专家。根据用户需求产出一张可执行的工作流图。\n\n"
        f"{NODE_REFERENCE}\n\n可用工具：\n{tool_list}\n\n"
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
        user = f"请设计一个工作流：{payload.instruction}"

    try:
        model, _ = await get_chat_model(
            session, ModelSpec(provider=payload.provider, model=payload.model, max_tokens=8192)
        )
    except ProviderNotConfigured as e:
        raise HTTPException(400, str(e)) from e

    messages = [("system", system), ("human", user)]
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
            session, ModelSpec(provider=payload.provider, model=payload.model, max_tokens=2048)
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
