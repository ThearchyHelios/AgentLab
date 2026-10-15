"""蓝图的读写与留痕（N2、§3.1、§3.10）。蓝图接口（api/blueprints.py 的 PATCH）、蓝图助手和填关节的流式接口
（api/copilot_blueprint.py）都经这里落地，规则只有一处：

- 权限：共享蓝图要搭建者；个人蓝图只给拥有者，而且管理员开了个人应用（blueprints/personal.allowed）。别人的个人蓝图
  当没有（404），不说它在不在；
- 落地：bp_* 操作按 blueprints/ops.apply_bp_op 逐个落到存着的那一份上，每个操作写一条 blueprint_events（谁、核实过没有、
  来源、位置、改之前、改之后，P2）。复用口径卡的操作先按库里那一版核对（look/calibers.verify_reuse）并补上它读的列；
- 关节：卡片改了，生成关节的依据跟着变的那几组标回「待填」（instantiate.filled_from），蓝图上的关节状态随之更新。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blueprints.ops import BpRefused, apply_bp_op, parse_pointer

#: 蓝图不存在（或者是别人的个人蓝图）
BLUEPRINT_GONE = "蓝图不存在，可能已被删除"
PERSONAL_OFF = "管理员还没有开放个人应用：想给自己做一个看板，可以请管理员打开，或请搭建者做成应用"


@dataclass
class Saved:
    """apply_ops_and_save 的结果：落地后的蓝图、落地了的操作、被拒的操作（partial 时才有）。"""

    spec: dict[str, Any]
    applied: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)


# ==========================================================================
# 读与权限
# ==========================================================================


async def load(session: AsyncSession, blueprint_id: str) -> Any:
    """按 id 读蓝图并核对权限（ensure_access）。不存在、看不到的一律 404。"""
    from app.db.models_blueprints import Blueprint

    row = await session.get(Blueprint, blueprint_id)
    if row is None:
        raise HTTPException(404, BLUEPRINT_GONE)
    await ensure_access(session, row)
    return row


async def ensure_access(session: AsyncSession, blueprint: Any) -> None:
    """这个人能不能读写这张蓝图：共享的要搭建者及以上；个人的要拥有者，且个人应用开着（关登录时只看开关）。"""
    from app.api.deps import current_principal, ensure_role
    from app.blueprints import personal

    principal = current_principal()
    if getattr(blueprint, "scope", "shared") == "personal":
        if not personal.owns(principal, blueprint.owner):
            raise HTTPException(404, BLUEPRINT_GONE)
        if not await personal.allowed(session, principal):
            raise HTTPException(403, PERSONAL_OFF)
        return
    ensure_role("builder")


# ==========================================================================
# 落地与留痕
# ==========================================================================


def _at(spec: dict[str, Any], path: str | None) -> Any:
    """蓝图上某个位置现在的值（留痕的改之前 / 改之后）。位置不存在为 None。"""
    if not path:
        return None
    try:
        tokens = parse_pointer(path)
    except BpRefused:
        return None
    cur: Any = spec
    for token in tokens:
        if isinstance(cur, list) and token.isdigit() and int(token) < len(cur):
            cur = cur[int(token)]
        elif isinstance(cur, dict):
            cur = cur.get(token)
        else:
            return None
    return copy.deepcopy(cur)


async def _verified_reuse(session: AsyncSession, op: dict[str, Any]) -> dict[str, Any]:
    """复用口径卡的操作（设 /data/i/reuse，或加一项带 reuse 的数据）：按库里那一版核对，补上那张卡读的取数节点和列。"""
    from app.look.calibers import verify_reuse

    path = str(op.get("path") or "")
    if op.get("op") == "bp_set" and path.startswith("/data/") and path.endswith("/reuse") \
            and isinstance(op.get("value"), dict):
        return {**op, "value": await verify_reuse(session, op["value"])}
    if op.get("op") == "bp_add" and path.startswith("/data") and isinstance(op.get("value"), dict) \
            and isinstance(op["value"].get("reuse"), dict):
        return {**op, "value": {**op["value"], "reuse": await verify_reuse(session, op["value"]["reuse"])}}
    return op


async def _next_seq(session: AsyncSession, blueprint_id: str) -> int:
    from app.db.models_blueprints import BlueprintEvent

    return int((await session.execute(select(func.coalesce(func.max(BlueprintEvent.seq), 0))
                                      .where(BlueprintEvent.blueprint_id == blueprint_id))).scalar_one()) + 1


async def record_event(session: AsyncSession, blueprint: Any, *, op: str, data: dict[str, Any], actor: str | None,
                       verified: bool) -> None:
    """记一条蓝图留痕（不提交）。op 是 bp_* 操作，或 instantiate、fill_joint、trial、publish 这类蓝图上的动作。"""
    from app.db.models_blueprints import BlueprintEvent

    session.add(BlueprintEvent(blueprint_id=blueprint.id, seq=await _next_seq(session, blueprint.id), actor=actor,
                               verified=verified, op=op, data=data))
    await session.flush()


async def apply_ops_and_save(session: AsyncSession, blueprint: Any, ops: list[dict[str, Any]], actor: str | None,
                             verified: bool, source: str, *, partial: bool = False) -> Saved:
    """把一串 bp_* 操作落到蓝图上、写留痕、提交。source 是 user / assistant。

    partial=False（卡片上的修改）：一个被拒就整串不落，抛 BpRefused。partial=True（助手这一轮提的）：被拒的跳过、照实交回，
    别的照常落——助手的操作是按它看到的那一份写的，人在这期间改过卡片的话，冲突的那几个让人看见，不悄悄吞掉。"""
    from app.blueprints.spec import empty_blueprint

    spec = copy.deepcopy(blueprint.spec or empty_blueprint())
    saved = Saved(spec=spec)
    events: list[tuple[str, dict[str, Any]]] = []
    for raw in ops:
        try:
            op = await _verified_reuse(session, raw)
            before = _at(spec, op.get("path"))
            spec = apply_bp_op(spec, op, source=source)
        except BpRefused as e:
            if not partial:
                raise
            saved.refused.append({"op": raw, "message": e.message, "code": e.code})
            continue
        saved.applied.append(op)
        events.append((str(op.get("op")), _event_data(op, before, spec, source)))
    saved.spec = spec
    if not saved.applied:
        return saved
    for op_name, data in events:
        await record_event(session, blueprint, op=op_name, data=data, actor=actor, verified=verified)
    blueprint.spec = spec
    template = spec.get("template") or None
    blueprint.template_id = template["id"] if template else None
    blueprint.template_version = template["version"] if template else None
    blueprint.joints = refreshed_joints(blueprint, spec, ctx=await instantiate_context(session, blueprint))
    blueprint.updated_by = actor
    await session.commit()
    return saved


def _event_data(op: dict[str, Any], before: Any, spec: dict[str, Any], source: str) -> dict[str, Any]:
    kind = op.get("op")
    if kind == "bp_ask":
        return {"source": source, "question": op.get("question"), "options": op.get("options") or []}
    if kind == "bp_template":
        return {"source": source, "template": op.get("template"), "after": copy.deepcopy(spec.get("template"))}
    data: dict[str, Any] = {"source": source, "path": op.get("path"), "before": before}
    if kind != "bp_remove":
        data["after"] = _at(spec, op.get("path")) if kind == "bp_set" else copy.deepcopy(op.get("value"))
    return data


# ==========================================================================
# 关节
# ==========================================================================


#: 不算「卡片内容」的键：助手反问的记录（问答不改要发布的东西）
_DIGEST_SKIP = ("asks",)


def spec_digest(blueprint: Any) -> str:
    """卡片内容的指纹（加上名字，名字进工作流和应用）：发布单出单时记下，执行、接着做之前再算一遍，对不上就是出单之后
    卡片又改过——改「谁来做」「多久更新」「谁能看」这类不一定改图哈希的卡片，也认得出来（P1：摘要里说的就是要发布的）。"""
    from app.core.artifact_store import canonical_json, content_hash

    spec = {k: v for k, v in (blueprint.spec or {}).items() if k not in _DIGEST_SKIP}
    return content_hash(canonical_json({"name": blueprint.name or "", "spec": spec}))


def context_of(blueprint: Any) -> dict[str, Any]:
    """实例化这张蓝图要的上下文（instantiate 的关键字参数）：名字、工作流、蓝图 id；自由形导入的图存在蓝图的 graph 上。"""
    from app.blueprints.templates.freeform import ID as FREEFORM

    out: dict[str, Any] = {"name": blueprint.name or None, "workflow_id": blueprint.workflow_id,
                           "blueprint_id": blueprint.id}
    if blueprint.template_id == FREEFORM or ((blueprint.spec or {}).get("template") or {}).get("id") == FREEFORM:
        out["graph"] = blueprint.graph or None
    return out


async def records_context(session: AsyncSession, blueprint: Any, *, create: bool = False) -> dict[str, str] | None:
    """填报登记的记录表 {table, source}（records/tables）：不是填报登记的为 None。实例化、试运行、填关节、发布单都从这里取，
    同一张蓝图只有这一种写法（P5）。

    table 写「键.v版本」：写入时 records/tables.find_table 认得它，图也就固定在这一个表版本上（N8：应用版本固定表版本，
    表单不兼容地改了出新版本，图跟着变、要重新发布）。source 是这一版登记成的数据源名（汇总的 SQL 查它）。
    create=False 只读（records/tables.pick_table：和「填什么」兼容的那一版；要出新版本才对得上的、还没建过的为 None，
    模板按记录表服务的起名规则猜第一版）；create=True 没有就建（records/tables.draft_table：草稿不影响线上——兼容的
    只补列，不兼容的出下一版、线上那一版照常收提交，切换在发布单里做；只 flush，调用方提交）。个人蓝图从不建表：
    个人应用只读，建了也没人往里写，使用者也不该凭一张草稿在全局的数据源里添一个（只读现有的）。"""
    from app.blueprints.templates.records import ID as RECORDS
    from app.db.models import DataSource
    from app.records.tables import columns_from_fields, draft_table, pick_table

    spec = blueprint.spec or {}
    if ((spec.get("template") or {}).get("id") or blueprint.template_id) != RECORDS:
        return None
    from app.records.store import RecordsRefused

    columns = columns_from_fields(spec.get("fields") or [])
    if create and getattr(blueprint, "scope", "shared") != "personal":
        # 字段名不合规（records.column_invalid）照实抛给调用方：试运行、填关节要说清是哪个字段
        row = await draft_table(session, blueprint, columns)
    else:
        try:
            row = await pick_table(session, blueprint.id, columns)
        except RecordsRefused:
            row = None
    if row is None:
        return None
    source = await session.get(DataSource, row.source_id) if row.source_id else None
    return {"table": f"{row.table_key}.v{row.version}", "source": source.name if source is not None else ""}


async def instantiate_context(session: AsyncSession, blueprint: Any, *, create_records: bool = False) -> dict[str, Any]:
    """context_of 再加上填报登记的记录表：生成关节的依据（取数说明）里有记录表的数据源名，算依据要用真名。"""
    ctx = context_of(blueprint)
    if (records := await records_context(session, blueprint, create=create_records)) is not None:
        ctx["records"] = records
    return ctx


def refreshed_joints(blueprint: Any, spec: dict[str, Any], *, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """卡片改过之后的关节状态：还算数的生成关节（依据没变）保留，别的标「待填」；确定性关节按卡片重新填。没选模板的为空。
    ctx 是实例化的上下文（instantiate_context），不给按 context_of。"""
    from app.blueprints.instantiate import filled_from, instantiate

    if not (spec.get("template") or None):
        return {}
    ctx = ctx if ctx is not None else context_of(blueprint)
    keep = filled_from(spec, blueprint.joints or {}, graph=ctx.get("graph"), records=ctx.get("records"))
    return instantiate(spec, filled=keep, **ctx).joints


# ==========================================================================
# 交给前端的形状（typesBlueprint.Blueprint）
# ==========================================================================


def blueprint_issues_of(blueprint: Any) -> list[dict[str, Any]]:
    """蓝图卡片上显示的问题：共享蓝图按发布的口径（缺项、待确认都列），个人蓝图按个人应用的口径；再加还没填的生成关节。
    每条带白话（app/plain.with_plain）。"""
    from app.blueprints.ops import blueprint_issues
    from app.plain import with_plain

    spec = blueprint.spec or {}
    issues = blueprint_issues(spec, for_="personal" if blueprint.scope == "personal" else "publish")
    for joint_id, state in (blueprint.joints or {}).items():
        if isinstance(state, dict) and state.get("status") in ("pending", "refused"):
            issues.append({"code": "tpl.joint_unfilled" if state["status"] == "pending" else "tpl.joint_refused",
                           "level": "error", "path": None, "joint": joint_id,
                           "message": state.get("note") or "这一项助手还没填上"})
    return [with_plain(i) for i in issues]


def blueprint_out(blueprint: Any, *, issues: bool = True) -> dict[str, Any]:
    """一张蓝图交给前端的样子（frontend/src/typesBlueprint.ts 的 Blueprint）。关节只给状态，不给填的值（值在图上看）。"""
    joints = {jid: {k: v for k, v in state.items() if k in ("status", "by", "note")}
              for jid, state in (blueprint.joints or {}).items() if isinstance(state, dict)}
    out = {
        "id": blueprint.id, "name": blueprint.name, "owner": blueprint.owner, "scope": blueprint.scope,
        "status": blueprint.status, "spec": blueprint.spec or {}, "template_id": blueprint.template_id,
        "template_version": blueprint.template_version, "source": blueprint.source or {},
        "conversation_id": blueprint.conversation_id, "workflow_id": blueprint.workflow_id, "app_id": blueprint.app_id,
        "last_trial_run_id": blueprint.last_trial_run_id, "graph_hash": blueprint.graph_hash, "joints": joints,
        "copied_from": blueprint.copied_from,
        "created_at": blueprint.created_at.isoformat() if blueprint.created_at else None,
        "updated_at": blueprint.updated_at.isoformat() if blueprint.updated_at else None,
    }
    if issues:
        out["issues"] = blueprint_issues_of(blueprint)
    return out
