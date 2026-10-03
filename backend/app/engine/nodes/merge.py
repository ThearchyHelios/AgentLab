"""合并查询节点：读上游的查询快照，在库外按键合并，结果存成和数据源查询同形的查询快照。

**读的是工件库里那份完整结果。** 上游 tool 节点交回的 JSON 里带着查询快照的工件 id，这里按 id 取回快照（取回时复验
哈希），不读交给模型的那份文字：Agent 的消息历史里结果可能被截短，拿截短的文字合并会悄悄少行。上游是另一个合并
节点时同理，取它的结果快照。

**结果和数据源查询同形。** 快照有 columns、rows、truncated、sql、source（记为「合并查询」），台账里记一条 kind 为
query 的条目：报告照样按先后编成 Qn，[[v:Qn.rK.列]]、[[table:Qn …]] 和口径卡的 cell(nodes.<本节点>, 行, '列') 都按
原来的规则取值、核对。快照另记 inputs（别名、节点、各自的快照）和合并 SQL，证据面板据此展示合并的来源；逐格来历
记在 lineage 里（见 engine/merge_query.py），下钻据此指回输入的那一格。

**重放结果一致。** 快照里没有耗时这类每次不同的字段：节点重放、继续运行时同样的输入得到同一个工件 id、同一条
台账，报告里已经写下的出处不会因此变成另一份快照。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.core import artifact_store
from app.core.events import EventType
from app.data.guard import QueryLimits
from app.engine.context import NodeContext, NodeError
from app.engine.evidence import ledger_enabled, next_exec
from app.engine.merge_query import MERGE_SOURCE, MergeError, MergeInput, execute
from app.engine.state import GraphState

#: 合并警告进运行事件时的 code（log 事件）。运行详情、运行概况按它认出这是合并查询的警告
LOG_CODES = {"key_type_mismatch": "merge_key_type", "rows_grew": "merge_rows_grew",
             "result_truncated": "merge_truncated"}
#: merge.end 事件里带几行预览：运行详情只是让人看一眼合并出了什么，完整结果在查询快照里
PREVIEW_ROWS = 5
#: 上游查询没查成时，tool 节点交回的原话的开头（engine/nodes/tools.py 的 QUERY_FAILED 同一组）
_FAILED_PREFIXES = ("查询失败", "SQL 被拒绝：")


def _input_payload(value: Any) -> dict[str, Any] | None:
    """上游节点的产出 → 带工件 id 的查询结果（tool 节点交回的是 JSON 文本，合并节点交回的是对象）。"""
    data = value
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


async def _load_input(state: GraphState, ctx: NodeContext, alias: str, node_id: Any) -> MergeInput:
    nodes = ctx.run.spec.node_map()
    target = nodes.get(node_id) if isinstance(node_id, str) else None
    if target is None:
        raise NodeError(ctx.node.id, f"输入「{alias}」选择的节点「{node_id}」不存在，请在节点中重新选择")
    who = f"输入「{alias}」（{target.title}）"
    value = (state.get("nodes") or {}).get(target.id)
    if value is None:
        raise NodeError(ctx.node.id, f"{who}还没有运行结果：请确认它连在合并查询之前")
    if isinstance(value, dict) and value.get("skipped"):
        raise NodeError(ctx.node.id, f"{who}满足跳过条件被跳过了，没有查询结果可以合并")
    if isinstance(value, dict) and value.get("failed"):
        raise NodeError(ctx.node.id, f"{who}执行失败，没有查询结果可以合并：{value.get('error') or '原因未知'}")
    if isinstance(value, str) and value.startswith(_FAILED_PREFIXES):
        raise NodeError(ctx.node.id, f"{who}的查询没有成功，无法合并：{value[:300]}")
    payload = _input_payload(value)
    artifact = payload.get("artifact") if payload else None
    if not isinstance(artifact, str) or not artifact:
        raise NodeError(ctx.node.id, f"{who}的结果不是存成快照的查询结果。合并查询只能合并数据库查询（调用数据库查询工具的"
                                     "「调用工具」节点）或另一个「合并查询」节点的结果")
    try:
        snapshot = await asyncio.to_thread(artifact_store.load, artifact)
    except ValueError:
        raise NodeError(ctx.node.id, f"{who}的查询快照与哈希不一致，疑似被修改，不能合并") from None
    if snapshot is None:
        raise NodeError(ctx.node.id, f"{who}的查询快照已不存在，无法合并。请重新运行它")
    return MergeInput(alias=alias, node_id=target.id, artifact=artifact, snapshot=snapshot, label=target.title)


async def run_merge(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    inputs = ctx.config.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise NodeError(ctx.node.id, "「合并查询」还没有配置输入：至少选择一个上游的查询节点，并给它起一个别名")
    # SQL 里允许 {{ }}（例如按运行输入筛日期），和 tool 节点的 SQL 一样先渲染；快照里存的是渲染之后、守卫规范过的那条
    sql = ctx.render_str(str(ctx.config.get("sql") or ""), state)
    loaded = [await _load_input(state, ctx, str(alias), node_id) for alias, node_id in inputs.items()]

    started = time.perf_counter()
    try:
        # 内存 SQLite 是同步的：放进线程，大一点的合并不卡住事件循环（别的节点的事件照常流出去）
        outcome = await asyncio.to_thread(execute, loaded, sql, limits=QueryLimits())
    except MergeError as e:
        raise NodeError(ctx.node.id, str(e)) from None
    elapsed = int((time.perf_counter() - started) * 1000)

    snapshot = outcome.snapshot()
    try:
        artifact = await artifact_store.put_json(snapshot, kind="query_snapshot", run_id=ctx.run.run_id,
                                                 node_id=ctx.node.id)
    except Exception as e:  # noqa: BLE001 - 存不下就没有出处可言，不能带着一份引用不了的结果往下走
        raise NodeError(ctx.node.id, "合并结果没能存成查询快照（工件库写入失败），下游无法引用它。"
                                     "请稍后点「继续运行」重试", retryable=True) from e

    labels = {inp.alias: inp.label for inp in loaded}
    inputs_view = [{**{k: v for k, v in item.items() if k != "columns"}, "label": labels.get(item["alias"]) or None}
                   for item in outcome.inputs]
    ctx.emit(EventType.MERGE_END, inputs=inputs_view, sql=outcome.sql, rows=len(outcome.rows),
             columns=outcome.columns, truncated=outcome.truncated, warnings=outcome.warnings,
             preview_rows=outcome.rows[:PREVIEW_ROWS], duration_ms=elapsed, query_artifact=artifact,
             lineage=outcome.lineage is not None)
    for warning in outcome.warnings:
        ctx.emit(EventType.LOG, level="warn", message=warning["message"],
                 code=LOG_CODES.get(warning["code"], "merge_warning"))

    output: dict[str, Any] = {
        "artifact": artifact, "columns": outcome.columns, "rows": outcome.rows, "row_count": len(outcome.rows),
        "truncated": outcome.truncated, "sql": outcome.sql, "source": MERGE_SOURCE, "inputs": inputs_view,
        "warnings": outcome.warnings,
    }
    if outcome.column_types:
        output["column_types"] = outcome.column_types
    updates: dict[str, Any] = {"nodes": {ctx.node.id: output}}
    if ledger_enabled(ctx.run):
        # 证据台账：和数据源查询同形的 query 条目，报告据此编 Qn；没有 via（外面不包工具快照）
        updates["evidence"] = [{
            "kind": "query", "node_id": ctx.node.id, "exec": next_exec(state, ctx.node.id), "artifact": artifact,
            "source": MERGE_SOURCE, "columns": outcome.columns, "rows": len(outcome.rows),
            "truncated": outcome.truncated,
        }]
    var_name = str(ctx.config.get("assign_to") or "").strip()
    if var_name:
        updates["vars"] = {var_name: output}
    return updates
