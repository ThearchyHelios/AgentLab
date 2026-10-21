"""分析卡（节点类型 analysis，docs/design/analysis-round/00-总方案.md §3.2）：对上游查询节点的结果固定做几项确定性分析。

**模板定骨架。** 做哪几项、每项分析哪个上游查询节点、用哪种方法，写在节点上（items），随版本固定；阈值来自发起时的设置
快照，卡上可以写覆盖（overrides，同样随版本固定）。问数据里要看了数才知道拆哪个维度的，交给 Agent 的分析工具——两个入口
共用一套算子（app.analysis.run）、一种结论形状、一个证据种类。

**读工件库里那份完整结果。** 照合并查询的做法（engine/nodes/merge.py）从 state.nodes 取上游节点交回的查询快照 id，按 id
取回快照（取回时复验哈希），而且只认这次运行台账里 kind query 的那几份：别的工具拼一个同形的 JSON，里面的快照不是平台
落的。遮罩列（数据源设的、按 SQL 认出改了名字的）不当维度、不当度量，和证据面板同一套认法（app/tools/analysis.py）。

**产出。** nodes.<卡>.results.<项> = {status, verdict, direction, values, phrase, artifact}：verdict 是判定码（outside_band、
driver……），下游条件分支能按它走（周报「超出常规波动就转人工」）。每项的整份结论落成工件，台账记一条 kind analysis
（contract.ledger_entry，带 item），报告的写作目录编成 A{n}；每项另发一条 analysis.end（运行详情、运行对比按项对齐）。

**一项算不了不让卡失败。** 算不了（历史不够、本期不完整、截断……）就是这一项的结论（status refused），照实记、发一条
analysis_refused 提醒，接着做下一项。调用写错（列不存在、比率当可加……）是模板的毛病：整张卡失败，说清是哪一项、怎么改。

分析的结果不作口径卡的输入（D16）：口径卡读 nodes.<卡>.results 的，按追不到原始数据处理（受管级发布时另有提示）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from app.analysis import contract
from app.analysis.contract import AnalysisResult, CallError, InputSnapshot
from app.core import artifact_store
from app.core.errors import describe_exception
from app.core.events import EventType
from app.engine.context import NodeContext, NodeError
from app.engine.evidence import ledger_enabled, next_exec
from app.engine.labels import ANALYSIS_OP_LABEL
from app.engine.nodes.merge import _FAILED_PREFIXES, _input_payload
from app.engine.state import GraphState
from app.tools.analysis import (
    REFUSED_CODE, context_of, hypothesis_of, input_snapshot, masked_of, run_snapshot, settings_of, store, tool_context,
)


def card_view(result: AnalysisResult, artifact: str) -> dict[str, Any]:
    """一项结论在节点产出里的样子（nodes.<卡>.results.<项>）：状态、判定码、方向、具名标量、判定语、结论工件。"""
    verdict = result.get("verdict") or {}
    return {"status": result.get("status"), "verdict": verdict.get("code"), "direction": verdict.get("direction"),
            "values": dict(result.get("values") or {}), "phrase": result.get("phrase") or "", "artifact": artifact}


async def run_analysis(state: GraphState, ctx: NodeContext) -> dict[str, Any]:
    from app.engine.schema import analysis_issues, analysis_refs

    # 配置的规矩和画图时同一套（schema.analysis_issues）：发起前的校验已经挡过，这里兜底，写错了照样说清是哪一项
    if errors := [i for i in analysis_issues(ctx.run.spec, ctx.node) if i.level == "error"]:
        raise NodeError(ctx.node.id, errors[0].message)
    items: list[dict[str, Any]] = ctx.config["items"]
    settings = await settings_of(run_snapshot(ctx.run), ctx.config.get("overrides") or None)

    queries = [e for e in state.get("evidence") or [] if isinstance(e, dict) and e.get("kind") == "query"]
    inputs: dict[str, InputSnapshot] = {}
    for item in items:
        for _, node_id in analysis_refs(item):
            if node_id not in inputs:
                inputs[node_id] = await _load_input(state, ctx, node_id, queries)
    context = await context_of(list(inputs.values()), tool_ctx=tool_context(ctx.run, ctx.node.id, ctx.emit))

    import app.analysis as analysis_ops   # 按模块取 run：测试、合并前的替身都只换模块上的这一个函数

    exec_no = next_exec(state, ctx.node.id)
    done: dict[str, AnalysisResult] = {}
    results: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    for item in items:
        item_id, op = item["id"], item["op"]
        who = f"分析项「{item_id}」（{ANALYSIS_OP_LABEL[op]}）"
        request = _request(item, state, ctx, who)
        try:
            # 下钻（parent）只认这张卡里排在前面、已经做完的项：done 里此刻只有它们
            result = await analysis_ops.run(request, inputs.get, settings=settings, context=context, parents=done.get)
        except CallError as e:
            raise NodeError(ctx.node.id, f"{who}：{e.message}。请在分析卡中改正这一项") from None
        except Exception as e:  # noqa: BLE001 - 算子本身出错：说清是哪一项，原因照实
            raise NodeError(ctx.node.id, f"{who}执行出错：{describe_exception(e)}") from e
        try:
            artifact = await store(result, run_id=ctx.run.run_id, node_id=ctx.node.id)
        except Exception as e:  # noqa: BLE001 - 存不下就没有出处可言，不能带着一份引用不了的结论往下走
            raise NodeError(ctx.node.id, f"{who}的结论没能存下来（工件库写入失败），下游无法引用它。"
                                         "请稍后点「继续运行」重试", retryable=True) from e
        done[item_id] = result
        results[item_id] = card_view(result, artifact)
        entries.append(contract.ledger_entry(result, node_id=ctx.node.id, exec_no=exec_no, artifact=artifact,
                                             item=item_id))
        verdict = result.get("verdict") or {}
        ctx.emit(EventType.ANALYSIS_END, item=item_id, op=op, mode=result.get("mode"), status=result.get("status"),
                 verdict=verdict.get("code"), direction=verdict.get("direction"), phrase=result.get("phrase") or "",
                 reason_code=result.get("reason_code"), analysis_artifact=artifact)
        if result.get("status") == "refused":
            # 算不了是一条结论，不让卡失败；照实提醒一句（结构化字段给界面读，不让它从 message 里解析中文）
            ctx.emit(EventType.LOG, level="warn", code=REFUSED_CODE, item=item_id, op=op,
                     reason_code=result.get("reason_code"), reason=result.get("reason"),
                     message=f"「{ctx.node.title}」的{who}没有做成：{result.get('reason') or '原因未知'}")

    output: dict[str, Any] = {"results": results}
    updates: dict[str, Any] = {"nodes": {ctx.node.id: output}}
    if ledger_enabled(ctx.run) and entries:
        updates["evidence"] = entries
    var_name = str(ctx.config.get("assign_to") or "").strip()
    if var_name:
        updates["vars"] = {var_name: output}
    return updates


def _request(item: dict[str, Any], state: GraphState, ctx: NodeContext, who: str) -> contract.AnalysisRequest:
    """一项 → 一次分析的请求。参数里的 {{ }} 先按当前状态渲染（按运行输入选期次）；主输入（from）就是 agent 工具的
    query。参数按 agent 工具同一份参数模型校验（tools/builtin/analysis.ARGS），写错了说清是哪一项、它接受什么。"""
    from app.tools.builtin.analysis import ARGS, request_of
    from app.tools.registry import describe_args

    op = item["op"]
    params = ctx.render(dict(item.get("params") or {}), state)
    if isinstance(item.get("from"), str) and item["from"].strip():
        params["query"] = item["from"].strip()
    if op in contract.MODES:
        params["mode"] = item.get("mode")
    if hypothesis := hypothesis_of(item.get("hypothesis")):
        params["hypothesis"] = hypothesis
    try:
        args = ARGS[op].model_validate(params)
    except ValidationError as e:
        raise NodeError(ctx.node.id, f"{who}的{describe_args(ARGS[op], params, e)}") from None
    return request_of(op, args)


async def _load_input(state: GraphState, ctx: NodeContext, node_id: str,
                      queries: list[dict[str, Any]]) -> InputSnapshot:
    """上游一个查询节点这次交回的查询快照 → 算子的一份输入（含遮罩列）。只认这次运行台账里 kind query 的快照。"""
    target = ctx.run.spec.node_map()[node_id]       # 节点在不在、在不在上游、是不是查询节点，执行前已经校验过
    who = f"输入「{target.title}」"
    value = (state.get("nodes") or {}).get(node_id)
    if value is None:
        raise NodeError(ctx.node.id, f"{who}还没有运行结果：请确认它连在分析卡之前")
    if isinstance(value, dict) and value.get("skipped"):
        raise NodeError(ctx.node.id, f"{who}满足跳过条件被跳过了，没有查询结果可以分析")
    if isinstance(value, dict) and value.get("failed"):
        raise NodeError(ctx.node.id, f"{who}执行失败，没有查询结果可以分析：{value.get('error') or '原因未知'}")
    if isinstance(value, str) and value.startswith(_FAILED_PREFIXES):
        raise NodeError(ctx.node.id, f"{who}的查询没有成功，无法分析：{value[:300]}")
    payload = _input_payload(value)
    artifact = payload.get("artifact") if payload else None
    if not isinstance(artifact, str) or not artifact:
        raise NodeError(ctx.node.id, f"{who}的结果不是存成快照的查询结果。分析卡只能分析数据库查询（调用数据库查询工具的"
                                     "「调用工具」节点）或「合并查询」的结果")
    if not any(e.get("artifact") == artifact for e in queries):
        raise NodeError(ctx.node.id, f"{who}交回的不是这次运行里平台执行的查询（台账里没有这份查询快照），不能分析。"
                                     "请改选调用数据库查询工具的「调用工具」节点或「合并查询」")
    try:
        content = await asyncio.to_thread(artifact_store.load, artifact)
    except ValueError:
        raise NodeError(ctx.node.id, f"{who}的查询快照与哈希不一致，疑似被修改，不能分析") from None
    if not isinstance(content, dict):
        raise NodeError(ctx.node.id, f"{who}的查询快照已不存在，无法分析。请重新运行它")
    snapshot = input_snapshot(content, ref=node_id, artifact=artifact, node_id=node_id)
    snapshot["masked"] = await masked_of(content, artifact)
    return snapshot


__all__ = ["REFUSED_CODE", "card_view", "run_analysis"]
