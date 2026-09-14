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
        model, _ = await get_chat_model(session, await copilot_model_spec(session, payload))
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
  {"op":"done","explanation":"两三句话说明这张图怎么跑"}    ← 最后一行
- 按执行顺序添加节点（先入口后出口）；每加一个节点，立刻把连向它的边（两端都已存在的）输出出来，让图连贯地生长
- 修改现有图时只输出改动，没提到的节点不要动"""


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
    """流式读模型输出，按行切出操作。done 之后即停——后面就算有闲话也不要了。"""
    from app.engine.state import message_text as _text

    buffer = ""
    async for chunk in model.astream(messages):
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


@router.post("/generate-stream")
async def generate_stream(payload: GenerateIn, session: AsyncSession = Depends(get_session)):
    """流式版生成：SSE 逐操作推送，前端边收边把节点摆上画布。

    结束时（done 或流断）发一条 final：服务端把累积的图排版、校验后整体给出——
    过程可见性来自操作流，最终质量仍由和手工编排相同的 layout + validate 保证。
    """
    from fastapi.responses import StreamingResponse

    tool_list = "\n".join(
        f"- {name}（{spec.category}）：{spec.description}"
        for name, spec in sorted(all_specs().items())
    )
    system = (
        "你是一个 agent 工作流编排专家。根据用户需求，以操作流的方式逐步搭出一张可执行的工作流图。\n\n"
        f"{NODE_REFERENCE}\n\n可用工具：\n{tool_list}\n\n"
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
        user = f"请设计一个工作流：{payload.instruction}"

    try:
        spec_ = await copilot_model_spec(session, payload)
        model, model_id = await get_chat_model(session, spec_)
    except ProviderNotConfigured as e:
        raise HTTPException(400, str(e)) from e

    messages = [("system", system), ("human", user)]

    async def event_stream():
        explanation = ""
        yield f"data: {json.dumps({'op': 'model', 'model': model_id}, ensure_ascii=False)}\n\n"
        try:
            async for op in _iter_ops(model, messages):
                if op.get("op") == "done":
                    explanation = str(op.get("explanation", ""))
                changed = _apply_op(nodes, edges, op)
                if changed or op.get("op") in ("plan", "done"):
                    yield f"data: {json.dumps(op, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'op': 'error', 'message': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
            return

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
