"""运行对比：同一个工作流的两次运行按「变化三分法」对照——数据变了、口径变了、还是模板 / SQL 变了（engine/compare.py）。

GET /api/runs/{run_id}/compare/base：默认拿哪一次来比，{base_run_id, reason, base}。这次运行是定时任务发起的，取同一
定时任务、结束得比它早的最近一次终态运行（reason: same_schedule）；不是、或者同一任务还没有更早的，取同一工作流、
同一运行类别、结束得比它早的最近一次终态运行（reason: same_workflow）。只挑封存过的：没封存的拿来也比不了。
找不到 404。

GET /api/runs/{run_id}/compare?base={base_id}：{target, base, summary, template, metrics, queries, notes}，每个字段
的形状见 engine/compare.py。两次运行都必须已结束、封存且封存核对一致，封存判定照搬证据接口（api/evidence._sealed）：
没封存的运行事件还会变，封存核对不一致的事件被改过，拿它们比出来的结论都不作数，回 409（还没结束的和已经结束却没有
封存清单的分开说：后者再等也等不来封存）。不是同一个工作流、或者是同一次运行，回 422。

只读：两次运行都不追加事件、不落工件、不改运行记录；结果只交回给发请求的人。只认各自封存范围内的工件（证据接口的
白名单，加上封存的节点产出和指标集里写着的完整值，同推演），遮罩列（查询当时记下的加数据源现在设的）的值不出现在
结果里。读工件、比 SQL、重算都在线程里做：快照大的时候不挡住同一进程里别的请求。

路径挂在 /api/runs/{run_id}/… 下面；这个路由登记在 runs.router 之前，以后加 /api/runs/compare 这类固定路径也不会
被 /{run_id} 吃掉。定时任务结束后自动对比（期 4）直接调 default_base、compare_runs。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.api.coded import CodedHTTPException
from app.api.evidence import _TERMINAL, RUN_NOT_FOUND, _Masks, _Sealed, _sealed, _sealed_cards
from app.api.whatif import _finished_before, _loader, _mask_of, _outputs_before, _returned
from app.db.base import SessionLocal
from app.db.models import DataSource, Run
from app.engine import compare as engine
from app.engine import recompute

router = APIRouter(prefix="/api/runs", tags=["compare"])

BAD_REQUEST = "compare_bad_request"
MISMATCH = "compare_mismatch"
UNSEALED = "compare_unsealed"
SEAL_BROKEN = "compare_seal_broken"
NO_BASE = "compare_no_base"

BAD_REQUEST_TEXT = "请求格式有误：base 应为作为对比基准的运行 ID"
BASE_NOT_FOUND = "作为对比基准的运行记录不存在"
SAME_RUN = "不能和同一次运行自己对比，请选择另一次运行作为基准"
UNSAVED = "{who}不属于任何已保存的工作流，无法确认两次运行是同一个工作流，不能对比"
OTHER_WORKFLOW = "两次运行不属于同一个工作流（「{target}」与「{base}」），不能对比"
UNSEALED_TEXT = "{who}尚未结束并封存：对比只用封存的运行记录，运行结束后才能对比"
#: 已经结束、只是没有封存清单（封存上线前结束的、结束时没封上的）：再等也等不来封存，不能说「运行结束后才能对比」
NO_MANIFEST_TEXT = "{who}没有封存清单，不能用于对比"
SEAL_BROKEN_TEXT = "{who}的封存核对不一致：封存后有事件被修改、删除或插入，不能用于对比。可在记录页点「核对」查看核对结果"
NO_BASE_TEXT = ("没有找到可以对比的上一次运行：需要同一工作流、同一运行类别（定时任务发起的优先找同一定时任务）、"
                "更早结束并已封存的运行")
TARGET = "这次运行"
BASE = "作为基准的运行"


@router.get("/{run_id}/compare/base")
async def compare_base(run_id: str) -> dict[str, Any]:
    """默认拿来对比的那一次运行：{base_run_id, reason: same_schedule | same_workflow, base}。"""
    found = await default_base(run_id)
    if found is None:
        raise CodedHTTPException(404, NO_BASE_TEXT, NO_BASE)
    run, reason = found
    return {"base_run_id": run.id, "reason": reason, "base": run_ref(run)}


@router.get("/{run_id}/compare")
async def compare_with(run_id: str, base: str | None = None) -> dict[str, Any]:
    """这次运行和 base 那一次的对比，形状见 engine/compare.py。"""
    if base is None or not base.strip():
        raise CodedHTTPException(422, BAD_REQUEST_TEXT, BAD_REQUEST)
    return await compare_runs(run_id, base.strip())


async def default_base(run_id: str) -> tuple[Run, str] | None:
    """(默认的基准运行, 怎么选的)；找不到返回 None，这次运行本身不存在 404。

    「早」按结束时间比：这次运行还没结束（还在跑、停在审批）的按开始时间。只挑终态、封存过的运行——没封存的拿来
    对比只会得到 409。定时任务发起的先找同一定时任务（同一个工作流）的；它是这个任务的第一次时，退回同一工作流、
    同一运行类别。没挂在已保存的工作流上的运行（画布上没保存就运行的图）没有可比的。"""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND)
        if not run.workflow_id:
            return None
        before = run.finished_at or run.started_at or run.created_at
        query = (select(Run)
                 .where(Run.id != run.id, Run.workflow_id == run.workflow_id, Run.status.in_(_TERMINAL),
                        Run.manifest_hash.is_not(None), Run.finished_at.is_not(None), Run.finished_at < before)
                 .order_by(Run.finished_at.desc(), Run.created_at.desc()).limit(1))
        if run.schedule_id:
            found = (await session.execute(query.where(Run.schedule_id == run.schedule_id))).scalars().first()
            if found is not None:
                return found, "same_schedule"
        found = (await session.execute(query.where(Run.run_class == run.run_class))).scalars().first()
    return (found, "same_workflow") if found is not None else None


async def compare_runs(run_id: str, base_id: str) -> dict[str, Any]:
    """对比两次运行（run_id 是这次，base_id 是基准）。拒绝时抛 CodedHTTPException：运行不存在 404，同一次运行、
    不是同一个工作流 422，没结束封存、封存核对不一致 409。"""
    async with SessionLocal() as session:
        target = await session.get(Run, run_id)
        base = await session.get(Run, base_id)
        kinds = {str(name): kind for name, kind in (await session.execute(select(DataSource.name, DataSource.kind)))}
    if target is None:
        raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND)
    if base is None:
        raise CodedHTTPException(404, BASE_NOT_FOUND, RUN_NOT_FOUND)
    if base.id == target.id:
        raise CodedHTTPException(422, SAME_RUN, MISMATCH)
    for run, who in ((target, TARGET), (base, BASE)):
        if not run.workflow_id:
            raise CodedHTTPException(422, UNSAVED.format(who=who), MISMATCH)
    if target.workflow_id != base.workflow_id:
        raise CodedHTTPException(422, OTHER_WORKFLOW.format(target=target.workflow_name, base=base.workflow_name),
                                 MISMATCH)
    sealed = []
    for run, who in ((target, TARGET), (base, BASE)):
        found = await _sealed(run.id)
        if found.status not in _TERMINAL:
            raise CodedHTTPException(409, UNSEALED_TEXT.format(who=who), UNSEALED)
        if not found.sealed:
            raise CodedHTTPException(409, NO_MANIFEST_TEXT.format(who=who), UNSEALED)
        if not found.trusted:
            raise CodedHTTPException(409, SEAL_BROKEN_TEXT.format(who=who), SEAL_BROKEN)
        sealed.append(found)
    masks = _Masks()
    target_side = await _side(target, sealed[0], masks, kinds)
    base_side = await _side(base, sealed[1], masks, kinds)
    return await asyncio.to_thread(engine.compare, base_side, target_side)


def run_ref(run: Run) -> dict[str, Any]:
    """一次运行的概要（RunRef）：对比结果和默认基准里都用这一个形状。时间带时区（UTC）。"""
    return {"id": run.id, "workflow_name": run.workflow_name, "version": run.version, "run_class": run.run_class,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "schedule_id": run.schedule_id}


async def _side(run: Run, sealed: _Sealed, masks: _Masks, kinds: dict[str, Any]) -> engine.Side:
    """一次运行交给对比的东西（engine.compare.Side）：封存范围内的事件、复验过哈希的口径卡、只认封存范围内工件的
    读法、每份查询快照要遮的列，以及按口径卡那次执行还原输入用的 Sources。"""
    cards = await asyncio.to_thread(_sealed_cards, sealed)
    loader = _loader(sealed, cards)
    # 遮罩要查库（数据源现在设的），在这里每份快照算一遍交进去，对比本身在线程里不碰库。快照先在线程里读好，
    # 同一个 loader 记着读过的，对比时不再读盘
    snaps = await asyncio.to_thread(_read_all, loader, list(sealed.queries))
    masked = {artifact: frozenset(await _mask_of(snaps.get(artifact), info, masks))
              for artifact, info in sealed.queries.items()}
    returned = _returned(sealed)

    def sources(started: int) -> recompute.Sources:
        return recompute.Sources(load=loader, queries=tuple(sealed.queries), outputs=_outputs_before(sealed, started),
                                 masked=masked, returned=returned, finished=_finished_before(sealed, started))

    return engine.Side(run=run_ref(run), graph=sealed.graph, events=sealed.events, cards=cards, load=loader,
                       masked=masked, sources=sources, dialects=kinds, inputs=dict(run.input or {}))


def _read_all(loader: Callable[[str], Any], artifacts: list[str]) -> dict[str, Any]:
    """读出这几份查询快照：读不出来、被改过的记 None（遮罩照样按台账记的数据源算）。"""
    out: dict[str, Any] = {}
    for artifact in artifacts:
        try:
            content = loader(artifact)
        except ValueError:
            content = None
        out[artifact] = content if isinstance(content, dict) else None
    return out
