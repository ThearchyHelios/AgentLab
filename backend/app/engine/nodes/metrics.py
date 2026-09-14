from __future__ import annotations

from typing import Any

from app.core.events import EventType
from app.engine.context import NodeContext, NodeError
from app.engine.expressions import ExpressionError, eval_expression
from app.engine.state import GraphState, template_context


async def run_metrics(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    """口径卡：把数值计算收进固定层，产出受控的指标集（MetricSet）。

    表达式走 AST 白名单求值器——确定性、无模型参与、可复算。
    下游叙述节点只许引用这里登记过的数，出具校验器按这份清单抽数字回指。
    这就是"叙述层无算术权限"的机制载体：不是求模型别算，而是它算了也过不了校验。
    """
    definitions = ctx.cfg("metrics", []) or []
    if not definitions:
        raise NodeError(ctx.node.id, "口径卡没有定义任何指标")

    tctx = template_context(state)
    caliber = ctx.render_str(ctx.cfg("caliber", "") or ctx.node.title, state)
    caliber_version = str(ctx.cfg("caliber_version", "") or "v1")

    metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    for definition in definitions:
        metric_id = str(definition.get("id") or "").strip()
        expr = str(definition.get("expression") or "").strip()
        if not metric_id or not expr:
            errors.append(f"指标定义不完整：{definition}")
            continue
        try:
            value = eval_expression(expr, tctx)
        except ExpressionError as e:
            errors.append(f"{metric_id}: 表达式错误（{e}）")
            continue
        except ZeroDivisionError:
            errors.append(f"{metric_id}: 除以零")
            continue
        decimals = definition.get("decimals")
        if isinstance(value, float) and decimals is not None:
            value = round(value, int(decimals))
        metrics.append(
            {
                "id": metric_id,
                "name": definition.get("name") or metric_id,
                "unit": definition.get("unit") or "",
                "value": value,
                "expression": expr,
            }
        )

    if errors:
        # 口径卡是固定层：任何一个指标算不出来都是模板缺陷，不能带病出具
        raise NodeError(ctx.node.id, "口径卡计算失败：" + "；".join(errors))

    # text 是给叙述节点 prompt 用的清单——它是叙述层唯一的数字来源
    lines = [f"- {m['id']}（{m['name']}）= {m['value']}{m['unit']}" for m in metrics]
    result = {
        "kind": "metric_set",
        "caliber": caliber,
        "caliber_version": caliber_version,
        "metrics": metrics,
        "text": f"指标清单（口径 {caliber} @ {caliber_version}）：\n" + "\n".join(lines),
    }
    ctx.emit(
        EventType.LOG,
        level="info",
        message=f"口径卡 {caliber}@{caliber_version}：产出 {len(metrics)} 个受控指标",
    )

    updates: dict[str, Any] = {"nodes": {ctx.node.id: result}}
    var_name = ctx.cfg("assign_to", "")
    if var_name:
        updates["vars"] = {var_name: result}
    return updates
