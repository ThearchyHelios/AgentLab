from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.schema import GraphSpec, NodeType, ValidationResult, type_label, validate_graph

# 口径升版的三种合法处置。没有默认值是刻意的：升版最容易静默翻车的地方
# 就是"上周和这周的数其实不可比，但报表看起来一切正常"。
UPGRADE_POLICIES = {"recompute", "dual", "incomparable"}

UPGRADE_POLICY_LABELS = {
    "recompute": "用新口径回算历史",
    "dual": "并排双印新旧口径",
    "incomparable": "标注与历史不可比",
}


def lint_for_publish(spec: GraphSpec, *, level: str) -> ValidationResult:
    """发布门禁。

    published：基础可运行性之外只给警告。
    governed：受管模板，出具用的那一档 —— 把关节层的自由度收进边界里，
    错误会挡住发布。

    每条问题带机器编号（code）：发布前自动修复（engine/autofix.py）按它认问题、出修复，
    文案可以改，编号不能随便改。
    """
    result = ValidationResult()
    strict = level == "governed"

    def flag(message: str, *, code: str, node_id: str | None = None, hard: bool = False,
             field: str | None = None) -> None:
        result.add(message, level="error" if (hard and strict) else "warning", node_id=node_id,
                   field=field, code=code)

    has_contract_output = False
    for node in spec.nodes:
        cfg = node.config
        # 文案用画布上的叫法：节点标题 +（节点类型），参数写界面上的选项文字
        who = f"「{node.title}」（{type_label(node.type)}）"
        if node.type == NodeType.SUPERVISOR:
            # 全动态规划属于探索层。受管模板里出现它，等于把 XAgent 搬上了生产线。
            flag(
                f"{who}：受管模板不允许全动态规划的节点——谁来做、做几轮都由模型临场"
                "决定，属于探索层。改用固定编排的 Agent 或「模型调用」节点",
                code="governed.supervisor", node_id=node.id, hard=True,
            )
        elif node.type == NodeType.SUBGRAPH:
            if not cfg.get("workflow_version"):
                flag(
                    f"{who}没有钉住版本，口径会随上游最新版漂移。在节点里选定一个版本",
                    code="governed.subgraph_unpinned", node_id=node.id, hard=True, field="workflow_version",
                )
        elif node.type == NodeType.AGENT:
            tools = cfg.get("tools") or []
            if cfg.get("approval") == "never" and tools:
                flag(
                    f"{who}的审批策略是「全部自动放行」，受管模板要求危险工具至少人工确认。"
                    "改成「仅危险工具需要审批」",
                    code="governed.agent_approval_never", node_id=node.id, hard=True, field="approval",
                )
            elif cfg.get("approval") in (None, "") and spec.defaults.get("approval") == "never" and tools:
                # 节点自己没写，运行时跟随全图默认（context.approval_mode：节点 > 图级 defaults > 全局）。
                # 全图默认写着全部自动放行，这个 agent 就是全部自动放行，只是没写在它自己身上
                flag(
                    f"{who}没写自己的审批策略，跟随全图默认的「全部自动放行」，受管模板要求危险工具至少"
                    "人工确认。把全图默认改成「仅危险工具需要审批」，或者在这个节点上单独设置",
                    code="governed.default_approval_never", node_id=node.id, hard=True, field="approval",
                )
            if not tools:
                flag(f"{who}没有配任何可用工具，通常意味着它该是个「模型调用」节点",
                     code="governed.agent_no_tools", node_id=node.id, field="tools")
            elif len(tools) > 10:
                flag(
                    f"{who}的可用工具有 {len(tools)} 个，边界过宽（白名单衰减的前兆）",
                    code="governed.agent_tools_wide", node_id=node.id, field="tools",
                )
        elif node.type == NodeType.OUTPUT:
            contract = cfg.get("contract")
            if contract:
                has_contract_output = True
                _lint_contract(contract, node_id=node.id, spec=spec, flag=flag, strict=strict)
        elif node.type == NodeType.CODE:
            if cfg.get("network"):
                flag(f"{who}打开了「允许联网」：受管模板建议把取数收敛到「调用工具」节点"
                     "（那里有取数快照）", code="governed.code_network", node_id=node.id, field="network")

    if strict and not has_contract_output:
        result.add(
            "受管模板至少要有一个「成果 / 出具」节点声明出具契约，否则三档出具无从谈起",
            level="error", code="governed.no_contract",
        )
    return result


def publish_issues(spec: GraphSpec, *, level: str) -> list[Any]:
    """发布时拦的全部问题：validate_graph 和 lint_for_publish 的并集，顺序也照发布接口。

    发布、发布前检查、自动修复复核都走这一个口径——检查说能发的，真发布时不会被拦。
    """
    return [*validate_graph(spec).issues, *lint_for_publish(spec, level=level).issues]


def _lint_contract(
    contract: Any, *, node_id: str, spec: GraphSpec, flag: Any, strict: bool
) -> None:
    """契约要查内容，不能只查这个 key 在不在。

    只看 key 存不存在的话，contract={"metrics_from": "随便一个字符串"} 就满足
    "受管模板必须声明出具契约"这条硬门禁了 —— 一个结构上保证每次都出 formal 的
    空壳模板，能合法发布成受管模板。门禁写成这样，还不如没有：它给的是
    "这张图过过闸"的错觉。
    """
    if not isinstance(contract, dict):
        flag("出具契约必须是一个 JSON 对象", code="contract.not_object", node_id=node_id, hard=True,
             field="contract")
        return

    metric_nodes = {n.id for n in spec.nodes if n.type == NodeType.METRICS}
    sources = contract.get("metrics_from") or []
    if isinstance(sources, str):
        sources = [sources]

    if not sources:
        flag("出具契约没有声明 metrics_from（指标来自哪个「口径卡」节点），叙述里的数字无从回指",
             code="contract.metrics_from_missing", node_id=node_id, hard=True, field="contract.metrics_from")
    for src in sources:
        if src not in metric_nodes:
            flag(
                f"出具契约的 metrics_from 指向 {src!r}，但工作流里没有这个「口径卡」节点"
                "（写错了，或者那个节点还没建）",
                code="contract.metrics_from_invalid", node_id=node_id, hard=True, field="contract.metrics_from",
            )

    # 引用模式（report_from）核对的是报告撰写节点产出的文档，数字回指靠文档里的引用标记，
    # 用不上 narrative。report_from 指错了（不是报告撰写节点、不在出口上游）由 validate_graph
    # 报 error（contract.report_from_invalid），发布时两者一起过，照样挡住
    if contract.get("report_from") in (None, "") and not str(contract.get("narrative") or "").strip():
        flag("出具契约既没有声明 report_from（核对哪个「报告撰写」节点的文档），也没有声明 narrative"
             "（叙述），数字回指校验不会执行", code="contract.report_from_missing", node_id=node_id, hard=True,
             field="contract.report_from")

    if strict and not (contract.get("required") or []):
        flag(
            "受管模板的出具契约必须声明 required（必需指标），否则「不予出具」这一档永远触发不了",
            code="contract.required_missing", node_id=node_id, hard=True, field="contract.required",
        )
    if strict and not contract.get("strict"):
        flag(
            "受管模板建议把出具契约设为 strict：非 strict 下未回指的数字只降档不拦截",
            code="contract.strict_off", node_id=node_id, field="contract.strict",
        )


async def unresolved_caliber_upgrades(
    session: AsyncSession, spec: GraphSpec
) -> tuple[list[str], list[dict[str, Any]]]:
    """检查钉了版本的上游是否有未处置的新版本：子工作流（方法卡），以及用 caliber_from
    钉住别处口径卡的口径卡节点——两者是同一套规则。

    返回 (阻断性错误, 需记录的升版事件)。有新版本但没声明 upgrade_policy
    的直接阻断 formal 启动 —— 这是"被迫处置"，不是提醒。
    """
    from app.db.models import WorkflowVersion

    errors: list[str] = []
    events: list[dict[str, Any]] = []
    for node in spec.nodes:
        extra: dict[str, Any] = {}
        if node.type == NodeType.SUBGRAPH:
            pinned = node.config.get("workflow_version")
            wf_id = node.config.get("workflow_id")
            what = f"「{node.title}」（子工作流）钉在 v{pinned}"
        elif node.type == NodeType.METRICS and isinstance(node.config.get("caliber_from"), dict):
            ref = node.config["caliber_from"]
            pinned, wf_id = ref.get("workflow_version"), ref.get("workflow_id")
            extra = {"caliber_node": ref.get("node_id")}
            what = f"「{node.title}」（口径卡）钉住的口径卡在 v{pinned}"
        else:
            continue
        if not pinned or not wf_id or not str(pinned).isdigit():
            continue
        latest = (
            await session.execute(
                select(func.max(WorkflowVersion.version)).where(
                    WorkflowVersion.workflow_id == wf_id
                )
            )
        ).scalar()
        if latest is None or latest <= int(pinned):
            continue
        policy = node.config.get("upgrade_policy")
        if policy not in UPGRADE_POLICIES:
            errors.append(
                f"{what}，但上游已有 v{latest}。"
                f"正式运行前必须在节点上声明升版策略（upgrade_policy）："
                + " / ".join(f"{k}（{v}）" for k, v in UPGRADE_POLICY_LABELS.items())
            )
        else:
            events.append(
                {
                    "node_id": node.id,
                    "workflow_id": wf_id,
                    "pinned": int(pinned),
                    "latest": int(latest),
                    "policy": policy,
                    "policy_label": UPGRADE_POLICY_LABELS[policy],
                    **extra,
                }
            )
    return errors, events


#: 发起正式运行时记下「这次按不按受管出具」的那条事件（log · info，不进主流程）
GOVERNANCE_CODE = "run_governance"


def governance_note(*, governed: bool, workflow_name: str, version: int | None = None) -> dict[str, Any]:
    """发起正式运行时落进事件流的那条记录（runs.start_run 调用）。

    governed 由调用方按这次钉住的版本发布时记下的级别定（WorkflowVersion.level），老版本没记过的
    看工作流当时的 status。
    """
    which = f"「{workflow_name}」v{version}" if version else f"「{workflow_name}」"
    message = (f"这次正式运行按受管级别出具：{which}是按受管级别发布的，之后改图、改级别都不影响这次"
               if governed else
               f"这次正式运行按已发布级别出具：{which}不是按受管级别发布的")
    return {"level": "info", "code": GOVERNANCE_CODE, "governed": governed, "message": message}


async def governed_formal(run_id: str) -> bool:
    """这次运行是不是受管级别的正式运行：从按受管级别发布的版本发起。

    受管的判断沿用发布门禁的口径（lint_for_publish 的 strict 档位）。探索运行、已发布级别的
    正式运行都不是。

    报告节点和出口复核各问一次、前后隔着审批和重放，所以按发起时记下的那条事件算（start_run
    在封存范围内记的 run_governance，来源是钉住的那一版发布时记下的级别）；没有这条记录的是
    加这条之前发起的运行，照旧看工作流现在的 status。
    """
    from app.db.base import SessionLocal
    from app.db.models import Run, RunEvent, Workflow

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or run.run_class != "formal" or not run.workflow_id:
            return False
        # 这条事件没有 node_id，发起时就记下了：只翻不属于任何节点的日志
        notes = (await session.execute(
            select(RunEvent.data).where(RunEvent.run_id == run_id, RunEvent.type == "log",
                                        RunEvent.node_id.is_(None)).order_by(RunEvent.seq))).scalars()
        for data in notes:
            if isinstance(data, dict) and data.get("code") == GOVERNANCE_CODE:
                return data.get("governed") is True
        workflow = await session.get(Workflow, run.workflow_id)
        return workflow is not None and workflow.status == "governed"


def cells_allowed(contract: Any, *, governed: bool) -> bool:
    """报告能不能直接引用查询单元格。受管级别的正式运行要在契约里显式写 "cells": true；
    探索运行和已发布级别一律允许（用户拍板）。"""
    return not governed or (isinstance(contract, dict) and contract.get("cells") is True)
