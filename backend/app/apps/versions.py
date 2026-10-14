"""「已发布且未退役」的工作流版本：发起正式运行、口径升版判定、子图钉版本、应用钉版本都用这一个说法（P5，A1）。

以前三处各有各的判断：发起只认当前发布版（launch.py:92，F1），口径升版取 max(version) 把草稿也算进去（F3）。
工作流每保存一次就生成一个版本快照，没发布过的快照只是草稿：level 为空、也不是当前发布版。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workflow, WorkflowVersion
from app.db.models_apps import App, AppVersion
from app.db.models_process import ActionRecord

#: 退役被拒时的机器码
RETIRE_CURRENT = "version_retire_current"
RETIRE_UNPUBLISHED = "version_retire_unpublished"
RETIRE_ALREADY = "version_retire_already"


class RetireRefused(Exception):
    """不退役。status_code / message / code 就是 HTTP 回的状态码、detail、机器码。"""

    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def is_published(version: Any, *, current: int | None = None) -> bool:
    """这一版能不能被使用：level 不为空且没退役。level 为空的老数据，只认它就是 current（当前发布版）的情形。

    level 是发布时写在这一版上的（api/workflows.py 的 publish_workflow），记级别之前发布的老版本没有；那时只有
    当前发布版能发起正式运行，照旧只认它。"""
    if getattr(version, "retired_at", None) is not None:
        return False
    if getattr(version, "level", None):
        return True
    return current is not None and getattr(version, "version", None) == current


def _published_clause(current: int | None) -> Any:
    """is_published 的 SQL 写法：读不出整行（图很大）也能筛。"""
    level_set = WorkflowVersion.level.is_not(None)
    either = or_(level_set, WorkflowVersion.version == current) if current is not None else level_set
    return and_(either, WorkflowVersion.retired_at.is_(None))


async def _current(session: AsyncSession, workflow_id: str) -> int | None:
    return await session.scalar(select(Workflow.published_version).where(Workflow.id == workflow_id))


async def published_versions(session: AsyncSession, workflow_id: str) -> list[Any]:
    """这个工作流全部「已发布且未退役」的版本（WorkflowVersion），按版本号升序。"""
    current = await _current(session, workflow_id)
    rows = await session.execute(
        select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id, _published_clause(current))
        .order_by(WorkflowVersion.version))
    return list(rows.scalars())


async def latest_published(session: AsyncSession, workflow_id: str) -> Any | None:
    """最新的「已发布且未退役」的版本；没有就是 None。口径升版的判定用它（替换 governance.py 的 func.max）。"""
    current = await _current(session, workflow_id)
    return (await session.execute(
        select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id, _published_clause(current))
        .order_by(WorkflowVersion.version.desc()).limit(1))).scalar_one_or_none()


async def version_row(session: AsyncSession, workflow_id: str, version: int) -> Any | None:
    return (await session.execute(select(WorkflowVersion).where(
        WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))).scalar_one_or_none()


# --------------------------------------------------------------------------
# 子工作流固定的版本（D-q）
# --------------------------------------------------------------------------

#: 子工作流节点固定的版本不能用的机器码。发起正式运行（engine/launch.py）、发布工作流与发布前检查（engine/lint_process.py
#: 的 lint_subgraph_pins）、应用检查（apps/lint.py）都按 subgraph_pin_problems 判，机器码相同
SUBGRAPH_UNPUBLISHED = "subgraph_version_unpublished"
SUBGRAPH_RETIRED = "subgraph_version_retired"


@dataclass(frozen=True)
class SubgraphPinProblem:
    node_id: str
    #: 子工作流节点的标题
    title: str
    code: str
    #: 节点固定的是哪个工作流的哪一版：门禁拿读好的结果去查另一张图（自动修复后的预览）时，按它核对节点没改过
    workflow_id: str
    version: int
    #: 说到「「月报」v3 没有发布过」为止；前后半句（不能发起正式运行 / 正式运行发起时会被拒绝）由调用方接
    reason: str
    #: 怎么改
    advice: str


async def subgraph_pin_problems(session: AsyncSession, spec: Any) -> list[SubgraphPinProblem]:
    """图里每个固定了版本的子工作流节点，固定的那一版必须「已发布且未退役」（is_published）；不是的逐个交回。

    子工作流固定版本就是方法卡的机制：外层发布时认可的是「用它的 vN」。vN 只是一张保存出来的草稿快照、或者已经退役，
    正式运行照样执行它，就等于绕过了发布门禁、绕过了退役。发起时拦（launch.py），发布时也按同一条拦——不然钉在草稿上的
    工作流照样发得出去，之后每一次正式运行（定时任务、应用入口、按需视图、补算）都在发起时才被拒。
    没固定版本的子工作流跟随最新，那是受管级门禁管的事（governed.subgraph_unpinned），这里不管。"""
    from app.engine.schema import NodeType

    out: list[SubgraphPinProblem] = []
    for node in spec.nodes:
        if node.type != NodeType.SUBGRAPH:
            continue
        wid, pinned = node.config.get("workflow_id"), node.config.get("workflow_version")
        if not wid or not str(pinned or "").isdigit():
            continue
        version = int(pinned)
        workflow = await session.get(Workflow, str(wid))
        row = await version_row(session, str(wid), version) if workflow is not None else None
        if workflow is None or row is None:
            out.append(SubgraphPinProblem(node.id, node.title, SUBGRAPH_UNPUBLISHED, str(wid), version,
                                          f"工作流版本 v{version} 不存在", "请在节点中重新选择版本"))
        elif row.retired_at is not None:
            out.append(SubgraphPinProblem(node.id, node.title, SUBGRAPH_RETIRED, str(wid), version,
                                          f"「{workflow.name}」v{version} 已退役", "请把节点改为固定一个在用的发布版本"))
        elif not is_published(row, current=workflow.published_version):
            out.append(SubgraphPinProblem(node.id, node.title, SUBGRAPH_UNPUBLISHED, str(wid), version,
                                          f"「{workflow.name}」v{version} 没有发布过",
                                          "请先发布那个工作流，或把节点改为固定一个已发布的版本"))
    return out


# --------------------------------------------------------------------------
# 账本（L4）：一个工作流版本被谁固定、生效了几次、正式运行了几次。退役确认、版本详情、删除约束、账本接口共用
# --------------------------------------------------------------------------


async def pinning_apps(session: AsyncSession, workflow_id: str, version: int | None = None) -> list[dict[str, Any]]:
    """当前发布版固定着这个工作流（version 给了就只看这一版）的应用：[{app_id, name, app_version, alias, version}]。

    只看各应用当前的发布版：应用升过级、不再固定它的旧版本不算「正在用」。归档的应用不算。pins 存在 JSON 里，
    应用的数量是人建的、不会很多，读出来逐个看。"""
    rows = (await session.execute(
        select(App.id, App.name, AppVersion.version, AppVersion.pins)
        .join(AppVersion, and_(AppVersion.app_id == App.id, AppVersion.version == App.published_version))
        .where(App.status != "archived"))).all()
    out: list[dict[str, Any]] = []
    for app_id, name, app_version, pins in rows:
        for pin in pins or []:
            if not isinstance(pin, dict) or pin.get("workflow_id") != workflow_id:
                continue
            if version is not None and pin.get("version") != version:
                continue
            out.append({"app_id": app_id, "name": name, "app_version": app_version, "alias": pin.get("alias"),
                        "version": pin.get("version")})
    return out


async def effect_count(session: AsyncSession, workflow_id: str, version: int | None = None) -> int:
    """生效记录的条数（ActionRecord）。演练（simulated）不算生效：它没有发出去。"""
    from sqlalchemy import func

    stmt = select(func.count(ActionRecord.id)).where(ActionRecord.workflow_id == workflow_id,
                                                      ActionRecord.state != "simulated")
    if version is not None:
        stmt = stmt.where(ActionRecord.workflow_version == version)
    return int(await session.scalar(stmt) or 0)


async def formal_run_count(session: AsyncSession, workflow_id: str, version: int) -> int:
    from sqlalchemy import func

    from app.db.models import Run

    return int(await session.scalar(select(func.count(Run.id)).where(
        Run.workflow_id == workflow_id, Run.version == version, Run.run_class == "formal")) or 0)


async def usage(session: AsyncSession, workflow_id: str, version: int) -> dict[str, Any]:
    """{apps, effects, formal_runs}：版本详情、账本接口、退役确认给的同一份。"""
    return {"apps": await pinning_apps(session, workflow_id, version),
            "effects": await effect_count(session, workflow_id, version),
            "formal_runs": await formal_run_count(session, workflow_id, version)}


# --------------------------------------------------------------------------
# 退役
# --------------------------------------------------------------------------


async def retire_version(session: AsyncSession, workflow_id: str, version: int, *, actor: str | None,
                         note: str | None) -> dict[str, Any]:
    """退役一个版本。当前发布版不能退（409）。返回 {retired, affected_apps: [...]}；写审计。

    退役之后：从它发起正式运行被拒（run_version_retired）；固定着它的应用标「需升级」，入口拒绝发起新实例，视图保留
    最后一次的数据并写明固定的版本已退役；进行中的实例照常走完——这里不碰任何运行。"""
    from app.apps.audit import record
    from app.db.base import utcnow

    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise RetireRefused(404, "工作流不存在，可能已被删除")
    row = await version_row(session, workflow_id, version)
    if row is None:
        raise RetireRefused(404, f"「{workflow.name}」不存在 v{version} 版本")
    if row.retired_at is not None:
        raise RetireRefused(409, f"v{version} 已经退役", RETIRE_ALREADY)
    if version == workflow.published_version:
        raise RetireRefused(409, f"v{version} 是当前发布版本，不能退役。请先发布一个新版本，再退役这一版",
                            RETIRE_CURRENT)
    if not is_published(row, current=workflow.published_version):
        raise RetireRefused(409, f"v{version} 没有发布过，不需要退役", RETIRE_UNPUBLISHED)
    affected = await pinning_apps(session, workflow_id, version)
    row.retired_at = utcnow()
    row.retired_by = actor
    row.retire_note = (note or "").strip() or None
    await record(session, action="workflow_version.retire", subject_kind="workflow_version",
                 subject_id=f"{workflow_id}:{version}", actor=actor,
                 data={"workflow_id": workflow_id, "workflow_name": workflow.name, "version": version,
                       "note": row.retire_note, "affected_apps": affected})
    await session.commit()
    return {"retired": {"workflow_id": workflow_id, "version": version, "retired_at": row.retired_at,
                        "retired_by": actor, "retire_note": row.retire_note},
            "affected_apps": affected}
