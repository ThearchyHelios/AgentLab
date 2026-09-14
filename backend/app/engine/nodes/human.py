from __future__ import annotations

import json
from typing import Any

from langgraph.types import interrupt

from app.core.events import EventType
from app.db.base import SessionLocal
from app.engine.context import NodeContext, NodeError
from app.engine.state import GraphState, message_text, template_context
from app.providers.factory import ModelSpec, get_chat_model


async def run_human(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """人工介入节点。

    执行到这里会挂起整张图并落盘 checkpoint；进程重启也不影响，
    人在前端回复之后从断点继续，而不是从头重跑。
    """
    mode = ctx.cfg("mode", "approve")  # approve | input | edit
    payload = {
        "kind": "human_node",
        "node_id": ctx.node.id,
        "mode": mode,
        "title": ctx.render_str(ctx.cfg("title", "需要你确认"), state),
        "message": ctx.render_str(ctx.cfg("message", ""), state),
        "schema": ctx.cfg("schema") or {},
        "context": ctx.render(ctx.cfg("context", {}) or {}, state),
    }
    if mode == "edit":
        payload["draft"] = ctx.render(ctx.cfg("draft", ""), state)

    ctx.emit(EventType.HUMAN_REQUESTED, **payload)
    response = interrupt(payload)
    ctx.emit(EventType.HUMAN_RESOLVED, response=response)

    if mode == "approve":
        approved = _truthy(response)
        result: dict[str, Any] = {
            "approved": approved,
            "note": _note_of(response),
            "__decision__": "approved" if approved else "rejected",
        }
        if not approved and ctx.cfg("stop_on_reject", False):
            raise NodeError(ctx.node.id, f"人工拒绝：{result['note'] or '未说明原因'}")
    else:  # edit / input
        # 驳回在这两种模式下以前是个摆设：__decision__ 硬编码成 approved，
        # approved 字段看都不看。界面上按钮在、toast 说"已驳回"，运行却照常
        # 往下走——比没有这个按钮更糟。
        #
        # 这两种模式在图上只有一个 out 出口（没有 rejected 那条边），所以驳回
        # 的正确语义是终止运行，而不是当作通过继续。缺省仍是通过：只传 value
        # 不带 approved 的老调用方行为不变。
        rejected = isinstance(response, dict) and response.get("approved") is False
        if rejected:
            note = _note_of(response)
            raise NodeError(ctx.node.id, f"人工驳回：{note or '未说明原因'}")
        value = response.get("value") if isinstance(response, dict) else response
        result = {"value": value, "note": _note_of(response), "__decision__": "approved"}

    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: result.get("value", result.get("approved"))}
    return updates


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        if "approved" in value:
            return bool(value["approved"])
        return str(value.get("decision", "")).lower() in ("approve", "yes", "true", "同意")
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "y", "true", "approve", "ok", "同意", "通过")
    return bool(value)


def _note_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("note", "") or value.get("comment", ""))
    return ""


# --------------------------------------------------------------------------
# 结构化校验
# --------------------------------------------------------------------------


async def run_validate(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """按 JSON Schema 校验上游产出。

    校验失败时可以把错误原样喂回模型让它改 —— 这比在 prompt 里祈祷它输出正确格式可靠得多。
    """
    import jsonschema

    schema = ctx.cfg("schema")
    if not schema:
        raise NodeError(ctx.node.id, "校验节点需要一个 JSON Schema")

    source_cfg = ctx.cfg("source", "{{ last_message }}")
    raw = ctx.render(source_cfg, state) if not isinstance(source_cfg, str) else ctx.render_str(source_cfg, state)
    max_retries = int(ctx.cfg("max_retries", 2) or 0)
    repair = bool(ctx.cfg("repair_with_llm", True))

    attempt = 0
    errors: list[str] = []
    value: Any = raw

    while True:
        value, parse_error = _coerce_json(raw)
        if parse_error:
            errors = [parse_error]
        else:
            try:
                jsonschema.validate(value, schema)
                ctx.emit(EventType.LOG, level="info", message=f"校验通过（第 {attempt + 1} 次尝试）")
                result = {"valid": True, "data": value, "attempts": attempt + 1, "errors": []}
                updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
                var_name = ctx.cfg("assign_to", "")
                if var_name:
                    updates["vars"] = {var_name: value}
                return updates
            except jsonschema.ValidationError as e:
                path = "/".join(str(p) for p in e.absolute_path) or "(根)"
                errors = [f"{path}: {e.message}"]

        ctx.emit(EventType.LOG, level="warn", message=f"第 {attempt + 1} 次校验失败：{errors[0]}")
        attempt += 1
        if attempt > max_retries or not repair:
            break
        raw = await _repair(raw, schema, errors, ctx, state)

    result = {"valid": False, "data": value, "attempts": attempt, "errors": errors}
    if ctx.cfg("fail_fast", True):
        raise NodeError(ctx.node.id, f"结构化校验未通过：{'; '.join(errors)}")
    return {"nodes": {ctx.node.id: result}}


def _coerce_json(raw: Any) -> tuple[Any, str | None]:
    """尽量把模型输出解析成 JSON：剥掉 ``` 围栏，再退一步找第一个完整对象。"""
    if isinstance(raw, (dict, list)):
        return raw, None
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1 : -1 if lines[-1].strip().startswith("```") else None]).strip()
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1
    )
    if start >= 0:
        for end in range(len(text), start, -1):
            try:
                return json.loads(text[start:end]), None
            except json.JSONDecodeError:
                continue
    return text, "输出不是合法 JSON"


async def _repair(raw: Any, schema: dict[str, Any], errors: list[str], ctx: NodeContext,
                  state: GraphState) -> str:
    async with SessionLocal() as session:
        model, _ = await get_chat_model(
            session,
            ModelSpec(provider=ctx.cfg("provider"), model=ctx.cfg("model"), max_tokens=4096),
        )
    prompt = (
        "下面这段输出没有通过 JSON Schema 校验，请修正后重新输出。\n\n"
        f"Schema：\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"错误：\n- " + "\n- ".join(errors) + "\n\n"
        f"原始输出：\n{str(raw)[:6000]}\n\n"
        "只输出修正后的 JSON，不要任何解释或代码围栏。"
    )
    return message_text(await model.ainvoke(prompt))
