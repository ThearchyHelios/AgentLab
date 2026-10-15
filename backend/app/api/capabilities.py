"""能力登记与端点（BE-1）：/api/capabilities、/api/endpoints。设计文档 §2.6、§3.9、§3.10。

权限（§3.12「MCP stdio、端点和凭据、能力治理、模型接入」那一行）：搭建者只读，改动只给管理员。main.py 给整组挂
require_role("builder")，写接口在里面再要 admin。

- 能力列表每次都先同步一遍（sync_all 只改变了的行，几条查询）：刚建的数据源、刚加的自定义工具马上看得到；
- 治理带 version 乐观锁：读到的是 v3、提交时已经是 v4 的回 409，界面重新读一遍再改；
- 字段语义沿用数据目录「项」的状态和审阅（data/capability_review.py）：GET /capabilities/review-queue 列推断的项，
  POST /capabilities/review/batch 批量确认、驳回、撤销审阅（逐个能力按 version 乐观锁，写审计）；
- 端点的认证头只回掩码，提交掩码值表示保持原值；头的名字和值不合法（空名字、带空格、换行、中文）的 422 说清是哪一项；
  allow_intranet 只有管理员能开（写接口本身只给管理员）。
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal, ensure_role
from app.auth.audit import record
from app.capabilities import endpoints as ep
from app.capabilities import registry
from app.data import capability_review
from app.db.base import get_session
from app.db.models import Capability, CustomTool, Endpoint, McpServer

router = APIRouter(prefix="/api", tags=["capabilities"])


def capability_out(row: Capability) -> dict[str, Any]:
    return {"name": row.name, "system": row.system, "title": row.title, "description": row.description,
            "params_schema": row.params_schema or {}, "returns_schema": row.returns_schema, "tabular": row.tabular,
            "effect_system": row.effect_system, "effect": row.effect, "approval": row.approval,
            "unattended_write": row.unattended_write, "fields": row.fields or {}, "masked": list(row.masked or []),
            "credential_mode": row.credential_mode, "endpoint_id": row.endpoint_id, "lookup": row.lookup,
            "version": row.version, "missing": row.missing,
            "synced_at": row.synced_at.isoformat() if row.synced_at else None, "updated_by": row.updated_by}


@router.get("/capabilities")
async def list_capabilities(system: str | None = None, effect: str | None = None, q: str | None = None,
                            session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await registry.sync_all(session)
    rows = list((await session.execute(select(Capability).order_by(Capability.system, Capability.name))).scalars())
    if system:
        rows = [r for r in rows if r.system == system]
    if effect:
        rows = [r for r in rows if registry.effective_effect(r) == effect]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r.name.lower() or needle in (r.title or "").lower()
                or needle in (r.description or "").lower()]
    return {"items": [capability_out(r) for r in rows]}


@router.post("/capabilities/sync")
async def sync_capabilities(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return {"changed": await registry.sync_all(session)}


# ---- 字段语义的审阅（data/capability_review.py）。要登记在 /capabilities/{name:path} 前面：不然 review-queue 会被当成能力名


@router.get("/capabilities/review-queue")
async def capability_review_queue(limit: int = Query(50, ge=1, le=capability_review.QUEUE_LIMIT_MAX),
                                  offset: int = Query(0, ge=0),
                                  kind: Literal["all", "facts", "names"] = Query("all"),
                                  capability: str | None = Query(None, max_length=200),
                                  session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """待审阅的能力字段：只列推断的项，按用到它的运行次数、是不是写入能力、项的风险、来源排好。kind 筛影响数字的项
    （facts：度量类型）或名称和说明（names），capability 只看一项能力。搭建者能看，审阅只给管理员。只读：不顺带同步，
    来源删掉了的（missing）按上一次同步的结果不列——能力页先读列表（会同步），再读队列。"""
    return await capability_review.review_queue(session, kind=kind, capability=capability or None, limit=limit,
                                                offset=offset)


class CapabilityReviewItem(BaseModel):
    capability: str = Field(min_length=1, max_length=200)
    pointer: str = Field(min_length=1, max_length=500)
    field: Literal["label", "meaning", "unit", "measure", "codes"]
    action: Literal["confirm", "reject", "reset"]
    if_version: int = Field(ge=0)


class CapabilityReviewBatchIn(BaseModel):
    items: list[CapabilityReviewItem] = Field(min_length=1, max_length=capability_review.BATCH_MAX)


@router.post("/capabilities/review/batch")
async def review_capability_batch(payload: CapabilityReviewBatchIn, session: AsyncSession = Depends(get_session)
                                  ) -> dict[str, Any]:
    """批量审阅能力字段：逐条结果和请求一一对应。逐个能力按 if_version（能力的 version）乐观锁，同一个能力的几条一次写入、
    version 加一、写审计；一个能力冲突、位置不对、能力不存在只记在那几条上（ok 为假，code 是 capability_conflict /
    capability_path_invalid / capability_missing），不影响别的。能力治理只给管理员（§3.12），审阅同样。"""
    ensure_role("admin")
    principal = current_principal()
    items = [capability_review.BatchItem(i.capability, i.pointer, i.field, i.action, i.if_version)
             for i in payload.items]
    return {"results": await capability_review.review_batch(session, items, actor=principal.username,
                                                            verified=principal.verified)}


@router.get("/capabilities/{name:path}")
async def get_capability(name: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    await registry.ensure_synced(session, [name])
    row = await session.get(Capability, name)
    if row is None:
        raise HTTPException(404, f"能力「{name}」不存在")
    return capability_out(row)


class GovernanceIn(BaseModel):
    version: int
    model_config = {"extra": "allow"}


@router.put("/capabilities/{name:path}/governance")
async def govern_capability(name: str, payload: GovernanceIn, session: AsyncSession = Depends(get_session),
                            ) -> dict[str, Any]:
    """改治理字段。只认 registry.GOVERNED_FIELDS 里的键，带上读到的 version；改一次 version 加一，写审计。"""
    ensure_role("admin")
    changes = {k: v for k, v in (payload.model_extra or {}).items() if k in registry.GOVERNED_FIELDS}
    if not changes:
        raise HTTPException(422, "没有可修改的治理字段")
    principal = current_principal()
    try:
        row = await registry.govern(session, name, changes, version=payload.version, actor=principal.username,
                                    verified=principal.verified)
    except LookupError as e:
        raise HTTPException(404, f"能力「{name}」不存在") from e
    except registry.GovernanceError as e:
        raise HTTPException(422, str(e)) from e
    except registry.VersionConflict as e:
        raise HTTPException(409, "这项能力的治理设置已被其他人修改，请刷新后再改") from e
    return capability_out(row)


# ---------------------------------------------------------------- 端点


@router.get("/endpoints")
async def list_endpoints(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.execute(select(Endpoint).order_by(Endpoint.name))).scalars()
    return {"items": [ep.endpoint_out(r) for r in rows]}


class EndpointIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[^\s/]+$")
    description: str = ""
    environments: dict[str, Any] = Field(default_factory=dict)
    idempotency_header: str = Field(default="Idempotency-Key", min_length=1, max_length=100)
    timeout_s: int = Field(default=20, ge=1, le=300)


class EndpointPatch(BaseModel):
    version: int
    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[^\s/]+$")
    description: str | None = None
    environments: dict[str, Any] | None = None
    idempotency_header: str | None = Field(default=None, min_length=1, max_length=100)
    timeout_s: int | None = Field(default=None, ge=1, le=300)


def _audit_view(envs: dict[str, Any]) -> dict[str, Any]:
    """审计里记的端点环境：地址和开关照记，认证头只记名字。"""
    return {k: {"base_url": v.get("base_url"), "headers": sorted((v.get("headers") or {})),
                "allow_intranet": v.get("allow_intranet"), "verify_tls": v.get("verify_tls")}
            for k, v in (envs or {}).items()}


@router.post("/endpoints", status_code=201)
async def create_endpoint(payload: EndpointIn, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    ensure_role("admin")
    if (await session.execute(select(Endpoint.id).where(Endpoint.name == payload.name))).first():
        raise HTTPException(409, f"已存在名为「{payload.name}」的端点，请换一个名称")
    if problem := ep.idempotency_header_problem(payload.idempotency_header):
        raise HTTPException(422, problem)
    try:
        envs = ep.seal_environments(payload.environments, None)
    except ep.EndpointError as e:
        raise HTTPException(422, str(e)) from e
    row = Endpoint(name=payload.name, description=payload.description, environments=envs,
                   idempotency_header=payload.idempotency_header, timeout_s=payload.timeout_s,
                   updated_by=current_principal().username)
    session.add(row)
    await session.flush()
    await record(session, "endpoint.create", "endpoint", row.id, data={"name": row.name, "environments": _audit_view(envs)})
    await session.commit()
    await registry.refresh_cache(session)
    return ep.endpoint_out(row)


@router.patch("/endpoints/{endpoint_id}")
async def update_endpoint(endpoint_id: str, payload: EndpointPatch, session: AsyncSession = Depends(get_session)
                          ) -> dict[str, Any]:
    ensure_role("admin")
    row = await session.get(Endpoint, endpoint_id)
    if row is None:
        raise HTTPException(404, "端点不存在，可能已被删除")
    values: dict[str, Any] = {}
    if payload.name is not None and payload.name != row.name:
        if (await session.execute(select(Endpoint.id).where(Endpoint.name == payload.name))).first():
            raise HTTPException(409, f"已存在名为「{payload.name}」的端点，请换一个名称")
        values["name"] = payload.name
    if payload.idempotency_header is not None and (problem := ep.idempotency_header_problem(payload.idempotency_header)):
        raise HTTPException(422, problem)
    if payload.environments is not None:
        try:
            values["environments"] = ep.seal_environments(payload.environments, row.environments)
        except ep.EndpointError as e:
            raise HTTPException(422, str(e)) from e
    for key in ("description", "idempotency_header", "timeout_s"):
        if (value := getattr(payload, key)) is not None:
            values[key] = value
    before = {"name": row.name, "environments": _audit_view(row.environments)}
    done = await session.execute(update(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.version == payload.version)
                                 .values(**values, version=payload.version + 1, updated_by=current_principal().username))
    if done.rowcount == 0:
        await session.rollback()
        raise HTTPException(409, "这个端点已被其他人修改，请刷新后再改")
    await record(session, "endpoint.update", "endpoint", endpoint_id, data={
        "before": before, "after": {"name": values.get("name", row.name),
                                    "environments": _audit_view(values.get("environments", row.environments))}})
    await session.commit()
    session.expire_all()
    row = await session.get(Endpoint, endpoint_id)
    assert row is not None
    # 端点改了名字，绑定它的能力的「所属系统」跟着变；绑定它的 MCP 服务按新的地址、认证头重连
    await registry.sync_all(session)
    from app.tools.mcp_manager import mcp_manager

    mcp_manager.invalidate(*(await session.execute(
        select(McpServer.name).where(McpServer.endpoint_id == endpoint_id))).scalars())
    return ep.endpoint_out(row)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: str, session: AsyncSession = Depends(get_session)) -> None:
    ensure_role("admin")
    row = await session.get(Endpoint, endpoint_id)
    if row is None:
        raise HTTPException(404, "端点不存在，可能已被删除")
    tools = [t.name for t in (await session.execute(select(CustomTool))).scalars()
             if ep.bound_endpoint_ref(t) in (row.id, row.name)]
    servers = list((await session.execute(select(McpServer.name).where(McpServer.endpoint_id == row.id))).scalars())
    if users := tools + [f"MCP 服务「{s}」" for s in servers]:
        raise HTTPException(409, f"端点仍被使用：{'、'.join(users)}。请先解除绑定再删除")
    await record(session, "endpoint.delete", "endpoint", row.id, data={"name": row.name})
    await session.delete(row)
    await session.commit()
    await registry.refresh_cache(session)


@router.post("/endpoints/{endpoint_id}/test")
async def test_endpoint(endpoint_id: str, env: str = "prod", session: AsyncSession = Depends(get_session)
                        ) -> dict[str, Any]:
    ensure_role("admin")
    row = await session.get(Endpoint, endpoint_id)
    if row is None:
        raise HTTPException(404, "端点不存在，可能已被删除")
    if env not in ep.ENVIRONMENTS:
        raise HTTPException(422, "环境只能是测试或生产")
    return await ep.probe(row, env)
