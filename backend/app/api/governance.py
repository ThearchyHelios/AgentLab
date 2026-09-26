from __future__ import annotations

import json
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
        raise HTTPException(404, "这个工作流不存在，可能已经被删了")
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

    run_ids = [
        rid
        for (rid,) in await session.execute(
            select(Run.id)
            .where(Run.workflow_id == workflow_id)
            .order_by(Run.created_at.desc())
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
        "hint": "授权了却很多轮都没用过的工具，是白名单在慢慢放宽的信号，考虑从节点里摘掉",
    }


@router.get("/exploratory-clusters")
async def exploratory_clusters(
    limit: int = Query(default=200, le=500),
    threshold: float = Query(default=0.62, ge=0.3, le=0.95),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """探索性问题聚类：同一个问题被反复问，就该长成模板里的固定一节。

    这就是模板生长的触发器。用本地 embedding 做贪心聚类，
    出现 ≥3 次的簇标记为晋升候选。
    """
    from app.memory.embeddings import cosine, embed_texts

    rows = list(
        await session.execute(
            select(Run.id, Run.input, Run.workflow_name, Run.created_at)
            .where(Run.run_class != "formal")
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
    )
    items = []
    for rid, input_payload, wf_name, created in rows:
        text = json.dumps(input_payload or {}, ensure_ascii=False, sort_keys=True)
        if len(text) <= 4:  # 空输入没有聚类意义
            continue
        items.append({"run_id": rid, "text": text, "workflow": wf_name,
                      "at": created.isoformat() if created else None})
    if not items:
        return {"clusters": [], "considered": 0}

    vectors = await embed_texts([i["text"] for i in items])
    clusters: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        placed = False
        for cluster in clusters:
            if cosine(vectors[i], vectors[cluster["seed"]]) >= threshold:
                cluster["members"].append(item)
                placed = True
                break
        if not placed:
            clusters.append({"seed": i, "members": [item]})

    out = [
        {
            "count": len(c["members"]),
            "representative": c["members"][0]["text"][:200],
            "workflow": c["members"][0]["workflow"],
            "run_ids": [m["run_id"] for m in c["members"]][:20],
            "first_at": c["members"][-1]["at"],
            "last_at": c["members"][0]["at"],
            "promote_candidate": len(c["members"]) >= 3,
        }
        for c in clusters
        if len(c["members"]) >= 2
    ]
    out.sort(key=lambda c: c["count"], reverse=True)
    return {
        "clusters": out,
        "considered": len(items),
        "hint": "标为晋升候选的问题反复出现，考虑用「提取模板」把它固定成工作流里的一步",
    }
