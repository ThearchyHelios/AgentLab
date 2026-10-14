"""按新口径补算（D-f；设计文档 §3.4 趋势那一行、§2.10 X3 的 app_backfill）。

钉住项升版时选了 recompute（按新口径重算），趋势只画新版本的数：升版之后才有的那一两期孤零零的，历史全空着。补算就是
把旧版本在同一来源上算过的那几期，按新版本重新发起一遍正式运行：

- 只对读定时任务或正式运行的视图（latest_sealed）：同一来源才说得清「那几期」是哪几期；
- 只在这个视图的钉住项最近一次升版选了 recompute 时：选 dual、incomparable 的本来就照旧画旧版本的数；
- 只补没有外部副作用的工作流（_side_effect）：钉作流程的、图里（含子工作流）有人工节点的、调用工具节点绑了写外部系统
  能力的、按流程使用会出事（process.*：Agent 中途等审批、联网的代码节点）的一律拒。补算一次最多发起 SERIES_MAX 次正式
  运行，对着历史输入逐个给人开待办、往外部系统重发写入，这些都收不回来，生效记录还会挡住以后删运行、删工作流；
- 取旧版本在同一来源（同一个定时任务，或 formal）上最近 N 期已封存的正式运行的输入（N 不超过视图上趋势的 last，
  最多 SERIES_MAX）。旧版本自己补算过的那几期也算（trend_runs）：连着升过两次版的，历史一样接得上；
- 新版本已经算过同一个输入的跳过：补算过的（没失败的），以及来源本身已经按新版本跑过的那一期；
- 用新版本逐个发起正式运行：来源 app_backfill、按钉住的 graph_hash 核对（expect_hash）、带 app_id / app_version，
  origin 里记补的是哪个来源、哪一次、那一期的时间（period_at），趋势按它排（resolve.period_of）。

补算的运行和别的正式运行一样封存、一样能点开证据；它不是「最近一次」（resolve.sealed_runs 不取它），只是趋势上的点。
同一个视图的补算在进程内串行（_lock）：锁里重新算一遍计划再发起，并发点两次、确认框弹着时别人先补过的，后来的那次
只会跳过。部署是单进程（调度器、运行管理都在进程里），进程内的锁就够。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apps.pins import load_pins
from app.apps.resolve import (BACKFILL_ORIGIN, SERIES_MAX, input_key, period_of, pin_upgrade, sealed_runs,
                              trend_runs)
from app.apps.tree import components
from app.db.models import Run
from app.db.models_apps import AppVersion

#: 补算被拒的机器码
BACKFILL_REFUSED = "app_backfill_refused"
#: 趋势没写 last 时取几期（同 resolve._series）
SERIES_DEFAULT = 12
_LIVE = ("queued", "running", "interrupted", "succeeded")


class BackfillRefused(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = BACKFILL_REFUSED) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


@dataclass
class Plan:
    view: str
    alias: str
    source: str
    from_version: int
    to_version: int
    #: 这个视图上的趋势最多画几期
    periods: int
    #: 要发起的：旧版本那几期的运行，按输入期从早到晚
    todo: list[Run] = field(default_factory=list)
    skipped: int = 0

    def out(self) -> dict[str, Any]:
        return {"view": self.view, "alias": self.alias, "source": self.source, "from_version": self.from_version,
                "to_version": self.to_version, "periods": self.periods, "will_launch": len(self.todo),
                "will_skip": self.skipped}


def series_periods(spec: dict[str, Any], view_id: str) -> int:
    """这个视图上的趋势最多画几期：各趋势组件 last 的最大值（没写按 SERIES_DEFAULT），不超过 SERIES_MAX。没有趋势组件
    返回 0。"""
    wanted = 0
    for comp, _, _ in components(spec):
        bind = comp.get("bind") if isinstance(comp.get("bind"), dict) else None
        if not bind or bind.get("view") != view_id or "series" not in bind:
            continue
        last = bind.get("last")
        n = last if isinstance(last, int) and not isinstance(last, bool) and last > 0 else SERIES_DEFAULT
        wanted = max(wanted, n)
    return min(wanted, SERIES_MAX)


async def _upgrade(session: AsyncSession, app: Any, version: AppVersion, view_id: str
                   ) -> tuple[dict[str, Any], Any, dict[str, Any], str] | None:
    """(视图来源, 新版本的钉住状态, 升版前的钉住项, 来源)；这个视图不需要补算时返回 None。拒绝的情形抛 BackfillRefused。"""
    spec = version.spec or {}
    view = (spec.get("views") or {}).get(view_id)
    src = (view or {}).get("source") if isinstance(view, dict) else None
    if not isinstance(src, dict):
        raise BackfillRefused(404, f"应用里没有视图「{view_id}」", "app_view_not_found")
    if src.get("kind") != "latest_sealed":
        raise BackfillRefused(409, "只有读定时任务或正式运行的视图能按新口径补算：按需运行、实例视图没有「历次」可补")
    alias = str(src.get("pin") or "")
    previous, disposition = await pin_upgrade(session, app.id, version, alias)
    if previous is None or disposition != "recompute":
        return None
    state = (await load_pins(session, spec)).get(alias)
    if state is None or not state.usable:
        raise BackfillRefused(409, "视图固定的工作流版本已退役或不可用，应用需要升级后才能补算", "app_needs_upgrade")
    if reason := await _side_effect(session, state):
        raise BackfillRefused(409, f"「{state.label}」{reason}，补算会对历史输入逐个重做这些事、无法撤回，这类工作流不能"
                                   "按新口径补算")
    return src, state, previous, str(src.get("from") or "formal")


async def _side_effect(session: AsyncSession, state: Any) -> str | None:
    """新版本的图补算时会不会碰到人或外部系统：会的交回一句原因（接在「「周报 v3」」后面），不会的返回 None。

    判断沿用应用检查对流程钉住项的那一套（apps/lint._lint_process）：人工节点、写外部系统的能力
    （capabilities.registry.writes_externally，和运行时、发布检查同一个判断）、process.* 问题（子工作流先读好，
    process.human_in_subgraph 才查得到）。"""
    from app.capabilities.registry import effective_effect, get_capability, writes_externally
    from app.engine.governance import publish_issues
    from app.engine.lint_process import load_subgraphs, with_subgraphs
    from app.engine.schema import GraphSpec, NodeType

    if state.pin.get("use") == "process":
        return "是作为流程固定的"
    try:
        graph = GraphSpec.model_validate(state.graph)
    except ValueError:
        return "的图无法解析"
    if humans := [n.title for n in graph.nodes if n.type == NodeType.HUMAN]:
        return f"里有人工节点「{'、'.join(humans[:3])}」"
    for node in graph.nodes:
        name = node.config.get("tool") if node.type == NodeType.TOOL else None
        if not isinstance(name, str) or not name:
            continue
        cap = await get_capability(session, name)
        if cap is not None and writes_externally(cap.name, effective_effect(cap), cap.endpoint_id):
            return f"的「{node.title}」会写外部系统（{name}）"
    with with_subgraphs(await load_subgraphs(session, graph)):
        issues = publish_issues(graph, level=state.level or "published")
    if process := [i for i in issues if (i.code or "").startswith("process.")]:
        return f"按流程使用不安全：{process[0].message.rstrip('。')}"
    return None


async def plan(session: AsyncSession, app: Any, version: AppVersion, view_id: str) -> Plan:
    """这个视图补算会发起几次、跳过几次（不发起任何运行）。"""
    found = await _upgrade(session, app, version, view_id)
    if found is None:
        raise BackfillRefused(409, "这个视图固定的版本最近一次升版没有选「按新口径重算」，不需要补算")
    src, state, previous, source = found
    periods = series_periods(version.spec or {}, view_id)
    if periods <= 0:
        raise BackfillRefused(409, "这个视图上没有趋势组件，补算出来的历史没有地方展示")
    out = Plan(view=view_id, alias=state.alias, source=source, from_version=int(previous.get("version")),
               to_version=int(state.pin["version"]), periods=periods)
    origin = src.get("from")
    old_runs = await trend_runs(session, app.id, previous.get("workflow_id"), previous.get("version"),
                                previous.get("graph_hash"), origin, periods)
    done = await _computed(session, app.id, state, origin, source)
    for run in old_runs:
        key = input_key(run.input)
        if key in done:
            out.skipped += 1
            continue
        done.add(key)
        out.todo.append(run)
    return out


async def _computed(session: AsyncSession, app_id: str, state: Any, origin: str | None, source: str) -> set[str]:
    """新版本已经算过（或正在算）的输入：这个应用为同一来源补算过、没失败的；来源本身按新版本跑过的。"""
    pin = state.pin
    keys = {input_key(r.input) for r in await sealed_runs(session, pin["workflow_id"], pin["version"],
                                                          pin.get("graph_hash"), origin, SERIES_MAX * 2)}
    rows = (await session.execute(select(Run.input, Run.origin).where(
        Run.app_id == app_id, Run.origin_kind == BACKFILL_ORIGIN, Run.workflow_id == pin["workflow_id"],
        Run.version == pin["version"], Run.version_hash == pin.get("graph_hash"), Run.status.in_(_LIVE)))).all()
    keys |= {input_key(inputs) for inputs, meta in rows if (meta or {}).get("source") == source}
    return keys


async def run_backfill(session: AsyncSession, app: Any, version: AppVersion, view_id: str, *, principal: Any,
                       actor: str | None) -> dict[str, Any]:
    """按计划逐个发起正式运行。返回 {launched, skipped, run_ids, ...plan}。

    一次也没发起成（发起被拒，比如口径升版没处置）时原样抛 LaunchRefused；发起了几次之后被拒的，已经发起的照常算，
    原因写进 refused 交回。"""
    async with _lock(app.id, view_id):
        return await _run_backfill(session, app, version, view_id, principal=principal, actor=actor)


#: (应用 id, 视图) → 补算锁。只增不减：一个应用的视图就那么几个
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(app_id: str, view_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault((app_id, view_id), asyncio.Lock())


async def _run_backfill(session: AsyncSession, app: Any, version: AppVersion, view_id: str, *, principal: Any,
                        actor: str | None) -> dict[str, Any]:
    from app.engine.launch import LaunchRefused, launch_run

    # 计划在锁里算：前一次补算发起的运行已经提交，这里读得到、按「已经补算过」跳过
    todo = await plan(session, app, version, view_id)
    pin = (await load_pins(session, version.spec or {}))[todo.alias].pin
    run_ids: list[str] = []
    refused: str | None = None
    for old in todo.todo:
        period = period_of(old)
        try:
            run = await launch_run(
                session, workflow_id=pin["workflow_id"], run_class="formal", version=pin["version"],
                expect_hash=pin.get("graph_hash"), input_payload=dict(old.input or {}), actor=actor,
                origin_kind=BACKFILL_ORIGIN, app_id=app.id, app_version=version.version,
                origin={"view": view_id, "spec_hash": version.spec_hash, "source": todo.source,
                        "backfill_of": old.id, "from_version": todo.from_version,
                        "period_at": period.isoformat() if period else None,
                        "actor_verified": bool(getattr(principal, "verified", False)),
                        "auth_mode": getattr(principal, "mode", "off")})
        except LaunchRefused as e:
            if not run_ids:
                raise
            refused = e.message
            break
        run_ids.append(run.id)
    from app.apps.audit import record

    await record(session, action="app.backfill", subject_kind="app", subject_id=app.id, actor=actor,
                 data={**todo.out(), "launched": len(run_ids), "run_ids": run_ids, "app_version": version.version})
    await session.commit()
    return {**todo.out(), "launched": len(run_ids), "skipped": todo.skipped, "run_ids": run_ids,
            **({"refused": refused} if refused else {})}


async def backfillable(session: AsyncSession, app: Any) -> list[dict[str, Any]]:
    """当前发布版里能按新口径补算的视图，各自会发起几次（编辑器、应用页上的「按新口径补算历史」按钮用）。只读。"""
    if not app.published_version:
        return []
    version = (await session.execute(select(AppVersion).where(
        AppVersion.app_id == app.id, AppVersion.version == app.published_version))).scalar_one_or_none()
    if version is None:
        return []
    out: list[dict[str, Any]] = []
    for view_id, view in ((version.spec or {}).get("views") or {}).items():
        source = (view or {}).get("source") if isinstance(view, dict) else None
        if not isinstance(source, dict) or source.get("kind") != "latest_sealed":
            continue
        try:
            found = await _upgrade(session, app, version, view_id)
            if found is None or series_periods(version.spec or {}, view_id) <= 0:
                continue
            out.append((await plan(session, app, version, view_id)).out())
        except BackfillRefused:
            continue
    return out
