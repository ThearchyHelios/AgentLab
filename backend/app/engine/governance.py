from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.schema import GraphSpec, NodeType, ValidationResult

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
    """
    result = ValidationResult()
    strict = level == "governed"

    def flag(message: str, *, node_id: str | None = None, hard: bool = False) -> None:
        result.add(message, level="error" if (hard and strict) else "warning", node_id=node_id)

    has_contract_output = False
    for node in spec.nodes:
        cfg = node.config
        if node.type == NodeType.SUPERVISOR:
            # 全动态规划属于探索层。受管模板里出现它，等于把 XAgent 搬上了生产线。
            flag(
                "受管模板不允许 supervisor 节点（全动态规划属于探索层）",
                node_id=node.id, hard=True,
            )
        elif node.type == NodeType.SUBGRAPH:
            if not cfg.get("workflow_version"):
                flag(
                    "子图（方法卡）引用没有钉版本，口径会随上游最新版漂移",
                    node_id=node.id, hard=True,
                )
        elif node.type == NodeType.AGENT:
            tools = cfg.get("tools") or []
            if cfg.get("approval") == "never" and tools:
                flag(
                    "agent 关闭了全部工具审批（approval=never），受管模板要求危险工具至少人工确认",
                    node_id=node.id, hard=True,
                )
            if not tools:
                flag("agent 没有配任何工具，通常意味着它该是个 llm 节点", node_id=node.id)
            elif len(tools) > 10:
                flag(
                    f"工具白名单有 {len(tools)} 个，边界过宽（白名单衰减的前兆）",
                    node_id=node.id,
                )
        elif node.type == NodeType.OUTPUT:
            contract = cfg.get("contract")
            if contract:
                has_contract_output = True
                _lint_contract(contract, node_id=node.id, spec=spec, flag=flag, strict=strict)
        elif node.type == NodeType.CODE:
            if cfg.get("network"):
                flag("代码节点开了网络，受管模板建议把取数收敛到工具节点（有快照）", node_id=node.id)

    if strict and not has_contract_output:
        result.add(
            "受管模板的 output 节点必须声明出具契约（contract），否则三档出具无从谈起",
            level="error",
        )
    return result


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
        flag("contract 必须是对象", node_id=node_id, hard=True)
        return

    metric_nodes = {n.id for n in spec.nodes if n.type == NodeType.METRICS}
    sources = contract.get("metrics_from") or []
    if isinstance(sources, str):
        sources = [sources]

    if not sources:
        flag("契约没有声明 metrics_from，叙述里的数字无从回指", node_id=node_id, hard=True)
    for src in sources:
        if src not in metric_nodes:
            flag(
                f"契约的 metrics_from 指向 {src!r}，但图里没有这个口径卡节点"
                "（写错了，或者那个节点还没建）",
                node_id=node_id, hard=True,
            )

    if not str(contract.get("narrative") or "").strip():
        flag("契约没有声明 narrative，数字回指校验不会执行", node_id=node_id, hard=True)

    if strict and not (contract.get("required") or []):
        flag(
            "受管模板的契约必须声明 required 指标，否则'必需指标缺失'这一档永远触发不了",
            node_id=node_id, hard=True,
        )
    if strict and not contract.get("strict"):
        flag(
            "受管模板建议把契约设为 strict：非 strict 下未回指的数字只降档不拦截",
            node_id=node_id,
        )


async def unresolved_caliber_upgrades(
    session: AsyncSession, spec: GraphSpec
) -> tuple[list[str], list[dict[str, Any]]]:
    """检查钉了版本的子图（方法卡）是否有未处置的新版本。

    返回 (阻断性错误, 需记录的升版事件)。有新版本但没声明 upgrade_policy
    的直接阻断 formal 启动 —— 这是"被迫处置"，不是提醒。
    """
    from app.db.models import WorkflowVersion

    errors: list[str] = []
    events: list[dict[str, Any]] = []
    for node in spec.nodes:
        if node.type != NodeType.SUBGRAPH:
            continue
        pinned = node.config.get("workflow_version")
        wf_id = node.config.get("workflow_id")
        if not pinned or not wf_id:
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
                f"节点 {node.title}（{node.id}）钉在方法卡 v{pinned}，但已有 v{latest}。"
                f"formal 运行前必须声明 upgrade_policy："
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
                }
            )
    return errors, events
