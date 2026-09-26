from __future__ import annotations

import json
from typing import Any

from app.core.events import EventType
from app.engine.context import NodeContext, NodeError
from app.engine.expressions import ExpressionError, eval_expression
from app.engine.state import GraphState, template_context


async def run_input(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """入口节点。把运行输入按声明的字段整理进变量池，补上默认值。"""
    raw = dict(state.get("input") or {})
    fields = ctx.cfg("fields", []) or []
    resolved: dict[str, Any] = dict(raw)

    for field in fields:
        name = field.get("name")
        if not name:
            continue
        if name not in resolved or resolved[name] in (None, ""):
            if field.get("required") and field.get("default") in (None, ""):
                raise NodeError(ctx.node.id, f"缺少必填输入：{name}")
            resolved[name] = field.get("default")

    return {
        "input": resolved,
        "vars": resolved,
        "nodes": {ctx.node.id: resolved},
    }


async def run_output(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """出口节点。按配置把散落在各节点的结果收成一个结构化成果。

    配了 contract 就升级成出具契约：核对指标齐不齐、叙述里的数字
    能不能回指到指标集，然后给出三档判定（formal / degraded / withheld）。
    判定和缺口清单一起印在成果上——降档不是悄悄地降。
    """
    mapping = ctx.cfg("fields", None)
    tctx = template_context(state)

    if mapping:
        result: dict[str, Any] = {}
        for field in mapping:
            name = field.get("name")
            if not name:
                continue
            result[name] = ctx.render(field.get("value", ""), state)
    else:
        # 没配映射就把最后一条消息当作结果，让新建的图开箱即用
        result = {"result": tctx.get("last_message", "")}

    contract = ctx.cfg("contract")
    if contract:
        result["_issuance"] = _apply_contract(contract, state, ctx)

    return {"output": result, "nodes": {ctx.node.id: result}}


def _apply_contract(
    contract: dict[str, Any], state: GraphState, ctx: NodeContext
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.engine.issuance import decide_tier, trace_numbers

    nodes = state.get("nodes") or {}

    # 1) 收指标集：contract.metrics_from 指向一个或多个口径卡节点
    metrics: list[dict[str, Any]] = []
    calibers: list[dict[str, str]] = []
    sources = contract.get("metrics_from") or []
    if isinstance(sources, str):
        sources = [sources]
    # gaps 记的是"校验本身哪里没跑起来"，和"校验跑了但没通过"是两回事。
    # 没有它，一个写错的节点名会让所有列表都是空的，看起来和"全部通过"一模一样。
    gaps: list[str] = []
    unresolved: list[str] = []
    for source in sources:
        payload = nodes.get(source)
        if isinstance(payload, dict) and payload.get("kind") == "metric_set":
            metrics.extend(payload.get("metrics") or [])
            calibers.append(
                {
                    "node": source,
                    "caliber": payload.get("caliber", ""),
                    "version": payload.get("caliber_version", ""),
                }
            )
        else:
            # 节点不存在、还没跑到、或者根本不是口径卡——指标集就是缺的，
            # 不能当作"这次没有指标要查"
            unresolved.append(source)
    if unresolved:
        gaps.append(f"指标源未解析：{', '.join(unresolved[:5])}")

    present = {m["id"] for m in metrics}
    missing_required = [m for m in (contract.get("required") or []) if m not in present]
    missing_expected = [m for m in (contract.get("expected") or []) if m not in present]

    # 2) 叙述数字回指
    declared_narrative = str(contract.get("narrative", ""))
    narrative = ctx.render_str(declared_narrative, state)
    # 模板取不到路径时 render 出来是空串，所以"声明了叙述但渲染成空"必须单独识别：
    # 节点改名、vars 写错都会让回指校验静默变成空操作
    if declared_narrative.strip() and not narrative.strip():
        gaps.append("叙述模板渲染为空（路径可能写错了）")
    elif not declared_narrative.strip():
        gaps.append("契约没有声明叙述，数字回指未执行")

    trace = (
        trace_numbers(narrative, metrics, allow=contract.get("allow_numbers"))
        if narrative.strip()
        else None
    )
    unmatched = trace.unmatched if trace else []

    # 声明了 metrics_from 却一个指标都没收到，同样是"没查成"
    if sources and not metrics:
        gaps.append("指标集为空，叙述里的数字无从回指")

    tier = decide_tier(
        missing_required=missing_required,
        missing_expected=missing_expected,
        unmatched=unmatched,
        strict=bool(contract.get("strict")),
        gaps=gaps,
    )

    issuance = {
        "tier": tier,
        "calibers": calibers,  # 口径版本照常印
        "metrics_checked": len(metrics),
        "missing_required": missing_required,
        "missing_expected": missing_expected,  # 缺数据声明照常印
        "unmatched_numbers": unmatched[:20],
        "matched_numbers": len(trace.matched) if trace else 0,
        # 校验没跑全的原因照实印出来——读的人要能分辨"查过都对"和"根本没查"
        "gaps": gaps,
        "declared_at": datetime.now(timezone.utc).isoformat(),
    }
    ctx.emit(
        EventType.ISSUANCE,
        tier=tier,
        missing_required=missing_required,
        missing_expected=missing_expected,
        unmatched=len(unmatched),
        calibers=calibers,
        # 降档的原因要跟着事件走：只有 gaps 的时候，时间线上的出具步骤以前只能写
        # 一个光秃秃的「降档出具」，横幅还说「请对照下方声明」，下方却什么都没有
        gaps=gaps,
        metrics_checked=len(metrics),
        matched_numbers=len(trace.matched) if trace else 0,
    )
    return issuance


async def run_transform(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """数据整形。用受限表达式把上游输出揉成下游要的形状，不用为此专门起一个 LLM。"""
    tctx = template_context(state)
    mode = ctx.cfg("mode", "expression")
    output: Any

    if mode == "expression":
        expr = ctx.cfg("expression", "")
        if not expr:
            raise NodeError(ctx.node.id, "整形节点没有填表达式")
        try:
            output = eval_expression(expr, tctx)
        except ExpressionError as e:
            raise NodeError(ctx.node.id, f"表达式错误：{e}") from e
    elif mode == "template":
        output = ctx.render_str(ctx.cfg("template", ""), state)
    elif mode == "json":
        rendered = ctx.render_str(ctx.cfg("template", "{}"), state)
        try:
            output = json.loads(rendered)
        except json.JSONDecodeError as e:
            raise NodeError(
                ctx.node.id,
                f"模板渲染出来的不是合法 JSON（第 {e.lineno} 行第 {e.colno} 列附近）。"
                "检查模板里的引号、逗号，字符串值要用 | json 过滤器输出",
            ) from e
    else:
        raise NodeError(ctx.node.id, f"未知的整形模式：{mode}")

    updates: dict[str, Any] = {"nodes": {ctx.node.id: output}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: output}
    return updates
