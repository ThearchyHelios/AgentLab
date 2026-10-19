from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Run, RunEvent, Workflow
from app.engine.schema import GraphSpec, NodeType

router = APIRouter(prefix="/api/governance", tags=["governance"])


@router.get("/tool-usage")
async def tool_usage(
    workflow_id: str,
    limit_runs: int = Query(default=100, le=500),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """工具白名单衰减报表。

    关节层最可能的退化方式不是模板太死，而是白名单为了长尾一个个放宽，
    半年后等于没有白名单。这份报表把"授了权但从来没用过"的工具摆到台面上，
    放宽才能被定期回收。
    """
    workflow = await session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(404, "工作流不存在，可能已被删除")
    spec = GraphSpec.model_validate(workflow.graph)

    # 各节点声明的白名单
    granted: dict[str, list[str]] = {}
    for node in spec.nodes:
        if node.type == NodeType.AGENT:
            granted[node.id] = list(node.config.get("tools") or [])
        elif node.type == NodeType.TOOL and node.config.get("tool"):
            granted[node.id] = [node.config["tool"]]
        elif node.type == NodeType.SUPERVISOR:
            merged: list[str] = []
            for agent in node.config.get("agents") or []:
                merged.extend(agent.get("tools") or [])
            granted[node.id] = sorted(set(merged))

    from app.engine.origin import counted_runs

    # 只算计入统计的运行（engine/origin.counted_runs）：评测、口径对账、召回替人跑的运行用到了哪些工具不说明白名单该不该收
    run_ids = [
        rid
        for (rid,) in await session.execute(
            select(Run.id)
            .where(Run.workflow_id == workflow_id, counted_runs())
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(limit_runs)
        )
    ]
    usage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if run_ids:
        rows = await session.execute(
            select(RunEvent.node_id, RunEvent.data).where(
                RunEvent.run_id.in_(run_ids), RunEvent.type == "tool.start"
            )
        )
        for node_id, data in rows:
            tool = (data or {}).get("tool")
            if node_id and tool:
                # supervisor 内部子 agent 的 node_id 形如 "team:researcher"
                usage[node_id.split(":")[0]][tool] += 1

    report = []
    for node_id, tools in granted.items():
        used = usage.get(node_id, {})
        report.append(
            {
                "node_id": node_id,
                "granted": tools,
                "used": dict(used),
                "unused": [t for t in tools if not used.get(t)],
                "ungrated_but_used": [t for t in used if t not in tools],
            }
        )
    return {
        "workflow_id": workflow_id,
        "runs_considered": len(run_ids),
        "nodes": report,
        "hint": "授权后多次运行都未使用的工具说明授权范围偏宽，建议从节点中移除",
    }


@router.get("/exploratory-clusters")
async def exploratory_clusters(
    limit: int = Query(default=200, le=500),
    threshold: float = Query(default=0.62, ge=0.3, le=0.95),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """探索性问题聚类：同一个问题被反复问，就该长成模板里的固定一节。

    这就是模板生长的触发器。取问题、聚类全站只有一份（app/chat/questions.py）：只取问题原文（不含 history），问数据一轮
    一条（自动重来不多算），评测这类系统派生的运行、别人的个人应用刷新和个人蓝图试运行都不进。出现 ≥3 次的簇标记为晋升
    候选。embedder 是聚类用的向量模型：本地哈希的语义能力弱，结果只是下限。
    """
    from app.api.deps import current_principal
    from app.chat.questions import cluster_questions, embedder, recent_questions

    questions = await recent_questions(session, current_principal(), limit=limit)
    if not questions:
        return {"clusters": [], "considered": 0, "embedder": embedder()}
    clusters = await cluster_questions(questions, threshold=threshold)

    def _at(value: Any) -> str | None:
        return value.isoformat() if value else None

    out = [
        {
            "count": c.count,
            "representative": c.members[0].text[:200],
            "workflow": c.members[0].workflow,
            "run_ids": [m.run_id for m in c.members if m.run_id][:20],
            "turn_ids": [m.turn_id for m in c.members if m.turn_id][:20],
            "first_at": _at(c.members[-1].at),
            "last_at": _at(c.members[0].at),
            "promote_candidate": c.count >= 3,
        }
        for c in clusters
        if c.count >= 2
    ]
    out.sort(key=lambda c: c["count"], reverse=True)
    return {
        "clusters": out,
        "considered": len(questions),
        "embedder": embedder(),
        "hint": "被标记为可固化的问题反复出现，建议用「提取模板」把它固定为工作流中的一步",
    }
