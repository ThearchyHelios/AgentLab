"""口径可调用描述（descriptor）：给目录页、应用表单、外部集成一份机器可读的稳定描述（next-round 02 的后续）。

口径已经能按固定版本实例化（look/calibers 的查看结果、ledger/compose 的 use_caliber），但入口字段、依赖、能不能出具这些
契约只写在给模型看的文字里。这里把它们写成结构化的数据：

- 只列每个工作流最新「已发布且未退役」版本里的**源头口径卡**（metrics 节点、caliber_from 为空），和 look/calibers、
  ledger/derive 的认法一致：转抄卡不列，源头才能复用；
- 入口字段用 ledger/derive.entry_fields，按 apps/forms.input_schema 转成 JSON Schema（和表单、use_caliber 校验同一个）；
- 依赖只从图里的取数节点认数据源和表名，**不返回 SQL、不返回参数值**（SQL 里可能写着遮罩值，组合时由服务端照抄）；
- 出具只描述这一版的策略：级别、图里有没有人工节点或写外部系统的节点。全局的自动出具开关、每日上限是运行时条件，这里
  不查——descriptor 说的是「这一版允许什么」，不代表这次运行一定能出具。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps import versions as app_versions
from app.apps.backfill import graph_side_effect
from app.apps.forms import input_schema
from app.apps.tree import node_map
from app.db.models import Workflow
from app.engine.evidence import sql_tables
from app.ledger.derive import entry_fields
from app.look.calibers import card_inputs
from app.tools.datasource import QUERY_PREFIX

#: 指标只写口径卡配置里真有的这几个键
METRIC_KEYS = ("id", "name", "unit", "decimals", "format", "kind", "definition", "params", "external", "rule",
               "assessment")


async def list_calibers(session: AsyncSession, *, workflow_id: str | None = None) -> list[dict[str, Any]]:
    """每个工作流最新「已发布且未退役」版本里的源头口径卡，一张卡一条描述。给了 workflow_id 只列那个工作流。

    同一版图里的 graph_side_effect 按 (workflow_id, version) 只算一次：它要读子图、跑发布检查，比其余字段慢得多。"""
    query = select(Workflow).order_by(Workflow.name, Workflow.id)
    if workflow_id:
        query = query.where(Workflow.id == workflow_id)
    side_cache: dict[tuple[str, int], str | None] = {}
    out: list[dict[str, Any]] = []
    for workflow in (await session.execute(query)).scalars().all():
        row = await app_versions.latest_published(session, workflow.id)
        if row is None:
            continue
        for node_id, node in node_map(row.graph or {}).items():
            if node["type"] != "metrics" or node["config"].get("caliber_from") not in (None, "", {}):
                continue
            made = await _describe(session, workflow, row, node_id, side_cache)
            if made is not None:
                out.append(made)
    return out


async def caliber_descriptor(session: AsyncSession, workflow_id: str, version: int,
                             node_id: str) -> dict[str, Any] | None:
    """一个工作流版本里一张源头口径卡的描述；工作流不存在、版本没发布过或已退役、节点不是源头口径卡时返回 None。"""
    workflow = await session.get(Workflow, str(workflow_id or ""))
    if workflow is None:
        return None
    row = await app_versions.version_row(session, workflow.id, int(version))
    if row is None or not app_versions.is_published(row, current=workflow.published_version):
        return None
    return await _describe(session, workflow, row, str(node_id), {})


async def _describe(session: AsyncSession, workflow: Workflow, row: Any, node_id: str,
                    side_cache: dict[tuple[str, int], str | None]) -> dict[str, Any] | None:
    """一张卡的描述；节点不是源头口径卡时返回 None。"""
    graph = row.graph or {}
    nodes = node_map(graph)
    card = nodes.get(node_id)
    if card is None or card["type"] != "metrics" or card["config"].get("caliber_from") not in (None, "", {}):
        return None
    cfg = card["config"]
    level = str(row.level or "")
    _, reason = card_inputs(graph, node_id)
    key = (workflow.id, int(row.version))
    if key not in side_cache:
        side_cache[key] = await graph_side_effect(session, graph, level=level or None)
    side_effect = side_cache[key]
    sources, tables = _fetches(nodes)
    reasons: list[str] = []
    if level != "governed":
        reasons.append("这一版是已发布级别，只支持一键出具")
    if side_effect is not None:
        reasons.append(f"这一版{side_effect}，不能替人重跑")
    return {
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "version": int(row.version),
        "level": level,
        "graph_hash": row.graph_hash,
        "card": {"node_id": node_id, "title": card["title"], "caliber": cfg.get("caliber"),
                 "caliber_version": cfg.get("caliber_version")},
        "metrics": [{k: m[k] for k in METRIC_KEYS if k in m}
                    for m in cfg.get("metrics") or []
                    if isinstance(m, dict) and m.get("id")],
        "entry_schema": input_schema(list(entry_fields(graph))),
        "composable": reason is None,
        "compose_reason": reason,
        "dependencies": {"sources": sorted(sources), "tables": sorted(tables),
                         "catalog_versions": row.catalog_versions, "schema_versions": row.schema_versions},
        "issuance": {"level": level, "auto_issue": level == "governed" and side_effect is None,
                     "requires_independent_proof": True, "side_effects": side_effect, "reasons": reasons},
    }


def _fetches(nodes: dict[str, dict[str, Any]]) -> tuple[set[str], set[str]]:
    """图里调用数据库查询工具、而且写了 SQL 的节点：数据源名、SQL 里 FROM / JOIN 的表名（去库名前缀）。只认表名，不读 SQL。"""
    sources: set[str] = set()
    tables: set[str] = set()
    for node in nodes.values():
        if node["type"] != "tool":
            continue
        tool = str(node["config"].get("tool") or "")
        args = node["config"].get("args")
        sql = args.get("sql") if isinstance(args, dict) else None
        # 工具名带模板的不是固定的数据源，认不出「哪一版库」
        if not tool.startswith(QUERY_PREFIX) or "{{" in tool or not isinstance(sql, str) or not sql.strip():
            continue
        sources.add(tool[len(QUERY_PREFIX):])
        tables.update(t.rsplit(".", 1)[-1] for t in sql_tables(sql))
    return sources, tables


__all__ = ["caliber_descriptor", "list_calibers"]
