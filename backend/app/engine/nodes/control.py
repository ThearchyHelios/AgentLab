from __future__ import annotations

import json
from typing import Any

from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine.context import NodeContext, NodeError
from app.engine.expressions import ExpressionError, eval_condition
from app.engine.state import GraphState, message_text, template_context
from app.providers.factory import ModelSpec, get_chat_model

# 分支和循环的"决定"写在 nodes[node_id] 里，编译器的路由函数再读出来。
# 路由函数本身不能有副作用，所以判断必须发生在节点执行阶段。
DECISION_KEY = "__decision__"


async def run_branch(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """条件分支。两种模式：表达式判断，或让模型做语义分类。"""
    cases = ctx.cfg("cases", []) or []
    if not cases:
        raise NodeError(ctx.node.id, "分支节点没有配置任何条件")

    mode = ctx.cfg("mode", "expression")
    tctx = template_context(state)
    chosen = "default"
    reason = ""

    if mode == "llm":
        chosen, reason = await _classify_with_llm(state, ctx, cases)
    else:
        for case in cases:
            expr = case.get("condition", "")
            key = case.get("key") or case.get("label") or "case"
            if not expr:
                continue
            try:
                if eval_condition(expr, tctx):
                    chosen, reason = key, f"命中条件：{expr}"
                    break
            except ExpressionError as e:
                raise NodeError(ctx.node.id, f"分支条件写错了（{expr}）：{e}") from e
        else:
            reason = "所有条件都不满足，走 default"

    ctx.emit(EventType.EDGE_TAKEN, branch=chosen, reason=reason, mode=mode)
    return {
        "nodes": {ctx.node.id: {DECISION_KEY: chosen, "branch": chosen, "reason": reason}},
    }


async def _classify_with_llm(
    state: GraphState, ctx: NodeContext, cases: list[dict[str, Any]]
) -> tuple[str, str]:
    """让模型在给定选项里挑一个。用结构化输出保证它只会吐出合法的 key。"""
    options = [
        {"key": c.get("key") or f"case{i}", "描述": c.get("label") or c.get("condition") or ""}
        for i, c in enumerate(cases)
    ]
    keys = [o["key"] for o in options] + ["default"]
    subject = ctx.render_str(ctx.cfg("input", "{{ last_message }}"), state)
    instruction = ctx.render_str(ctx.cfg("instruction", ""), state)

    prompt = (
        f"{instruction}\n\n" if instruction else ""
    ) + (
        "请把下面的内容归入最合适的一类。\n\n"
        f"可选类别：\n{json.dumps(options, ensure_ascii=False, indent=2)}\n\n"
        f"内容：\n{subject}\n\n"
        "只返回 JSON：{\"key\": <类别key>, \"reason\": <一句话理由>}"
    )

    async with SessionLocal() as session:
        model, _ = await get_chat_model(
            session,
            ModelSpec(
                provider=ctx.cfg("provider"),
                model=ctx.cfg("model"),
                max_tokens=512,
            ),
        )

    schema = {
        "title": "classify",  # langchain 靠 title 识别 JSON Schema dict
        "type": "object",
        "properties": {
            "key": {"type": "string", "enum": keys},
            "reason": {"type": "string"},
        },
        "required": ["key"],
    }
    try:
        value = await model.with_structured_output(schema).ainvoke(prompt)
        if not isinstance(value, dict):
            value = json.loads(message_text(value))
    except Exception:  # noqa: BLE001 - 结构化不支持就退回自由文本再解析
        raw = message_text(await model.ainvoke(prompt))
        value = {}
        for key in keys:
            if key in raw:
                value = {"key": key, "reason": raw[:200]}
                break

    chosen = str(value.get("key") or "default")
    if chosen not in keys:
        chosen = "default"
    return chosen, str(value.get("reason", ""))


async def run_loop(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """循环控制。

    画布上的接法：loop 节点的 `body` 出口连向循环体，循环体最后一个节点再连回 loop 节点；
    `done` 出口连向循环结束后要走的分支。每进一次这个节点就推进一次计数。
    """
    mode = ctx.cfg("mode", "foreach")
    node_id = ctx.node.id
    loops = dict(state.get("loops") or {})
    cursor = int((loops.get(node_id) or {}).get("index", 0)) if isinstance(loops.get(node_id), dict) else 0
    max_iter = int(ctx.cfg("max_iterations", 10) or 10)

    tctx = template_context(state)
    item_var = ctx.cfg("item_var", "item")
    updates_vars: dict[str, Any] = {}
    keep_going = False
    current_item: Any = None
    total = None

    if mode == "foreach":
        items = ctx.render(ctx.cfg("items", ""), state)
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except json.JSONDecodeError:
                items = [line for line in items.splitlines() if line.strip()]
        if not isinstance(items, list):
            items = [items] if items not in (None, "") else []
        total = len(items)
        keep_going = cursor < len(items) and cursor < max_iter
        if keep_going:
            current_item = items[cursor]
            updates_vars = {item_var: current_item, f"{item_var}_index": cursor}
    else:  # while
        condition = ctx.cfg("condition", "")
        if cursor >= max_iter:
            keep_going = False
        elif not condition:
            keep_going = False
        else:
            try:
                keep_going = eval_condition(condition, tctx)
            except ExpressionError as e:
                raise NodeError(ctx.node.id, f"循环条件写错了：{e}") from e

    hit_limit = cursor >= max_iter
    if hit_limit:
        ctx.emit(
            EventType.LOG, level="warn",
            message=f"循环达到上限 {max_iter} 次，强制退出", code="loop_limit",
        )

    decision = "body" if keep_going else "done"
    ctx.emit(
        EventType.EDGE_TAKEN,
        branch=decision,
        iteration=cursor,
        total=total,
        mode=mode,
    )

    result = {
        DECISION_KEY: decision,
        "iteration": cursor,
        "total": total,
        "item": current_item,
        "finished": not keep_going,
    }
    updates: dict[str, Any] = {
        "nodes": {node_id: result},
        "loops": {node_id: {"index": cursor + 1 if keep_going else cursor}},
    }
    if updates_vars:
        updates["vars"] = updates_vars
    return updates
