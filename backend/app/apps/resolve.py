"""页面解析与视图模型 agentlab.view/1（§3.4–§3.6）。

界面层不放逻辑（P7）：页面上的每个值都在这里按绑定从运行产物里取出来、渲染好，带着证据指针交给前端；前端只按组件名找
渲染器。不可见的组件不下发，遮罩值不出服务端（P4）；取不到的照实放进 problems，不吞掉（P1）。

视图的数据有三种来源（A3）：
- latest_sealed：钉住版本、同一 graph_hash 最近一次成功且已封存的正式运行（scheduler/auto_compare.sealed_now 判断这一次
  结束时封存了）。from=schedule:<id> 时只看这个定时任务发起的。
- on_demand：按参数发起一次正式运行（来源 app_view），结果按 sha256(graph_hash, 规范化输入, 上传源的数据版本, 能力版本)
  缓存。查活库的必须有时效；同一个键在进程内只运行一份；默认点「刷新」才发起，声明了 auto 且没有缓存时打开页面才自动发起；
  每个应用每天有上限（设置 apps.on_demand_daily_limit）。
- instance：实例自己的那次运行。

印章（截至 / 口径 / 出具档位 / 来源）由这里算；定时任务改跑了新版本而应用还钉在旧版本这类错位照实写进 notes。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.apps.pins import PinState, load_pins, needs_upgrade
from app.apps.spec import APP_ROOTS, VIEW_FORMAT
from app.apps.tree import bind_kind, page_of, resolve_form
from app.core.artifact_store import canonical_json
from app.db.base import utcnow
from app.db.models import Run, RunEvent, Schedule, ScheduleFire
from app.db.models_apps import AppViewCache, AppVersion

#: 趋势最多取几次封存运行
SERIES_MAX = 24
#: 表格最多下发几行（页面上只排序、不汇总；完整快照走证据接口）
TABLE_ROWS_MAX = 200
#: 进程内的页面缓存
PAGE_CACHE_SIZE = 256

#: 按新口径补算的运行来源（engine/origin.py）
BACKFILL_ORIGIN = "app_backfill"

VIEW_REFUSED = "app_view_refused"
DAILY_LIMIT = "app_view_daily_limit"
NEEDS_UPGRADE = "app_needs_upgrade"

STATUS_TEXT = {"queued": "排队中", "running": "运行中", "interrupted": "等待处理", "succeeded": "已完成",
               "failed": "失败", "cancelled": "已取消"}
TIER_TEXT = {"formal": "完整出具", "degraded": "降档出具", "withheld": "不予出具"}
NO_TIER = "未声明出具契约"
TIER_BY_METRICS = "（按指标）"
EMPTY_TEXT = "还没有数据"
_ACTIVE = ("queued", "running", "interrupted")
_PERIOD = re.compile(r"(period|week|month|quarter|year|date|day|期|周|月|季|年|日)", re.I)
_DIGIT = re.compile(r"\d")


class ViewRefused(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


# --------------------------------------------------------------------------
# 来源
# --------------------------------------------------------------------------


async def latest_sealed_run(session: AsyncSession, state: PinState, origin: str | None) -> Run | None:
    runs = await sealed_runs(session, state.pin.get("workflow_id"), state.pin.get("version"),
                             state.pin.get("graph_hash"), origin, 1)
    return runs[0] if runs else None


async def sealed_runs(session: AsyncSession, workflow_id: Any, version: Any, graph_hash: Any, origin: str | None,
                      n: int) -> list[Run]:
    """钉住版本（同一 graph_hash）最近 n 次成功且已封存的正式运行，新的在前。

    sealed_now 认的是「这一次结束时封的」：失败后接着跑完、新封存还没补上的那一刻，带着的是旧封存，不算。
    按新口径补算的运行（app_backfill）不算：它们是现在才算的历史期，「最近一次」不能取到它们；趋势另经 trend_runs
    按它们对应的输入期排进去。"""
    from sqlalchemy import or_

    from app.scheduler.auto_compare import sealed_now

    if not workflow_id or not isinstance(version, int) or not graph_hash:
        return []
    query = select(Run).where(Run.workflow_id == workflow_id, Run.version == version, Run.run_class == "formal",
                              Run.status == "succeeded", Run.version_hash == graph_hash,
                              or_(Run.origin_kind.is_(None), Run.origin_kind != BACKFILL_ORIGIN))
    if isinstance(origin, str) and origin.startswith("schedule:"):
        query = query.where(Run.schedule_id == origin[len("schedule:"):])
    rows = list((await session.execute(query.order_by(Run.finished_at.desc(), Run.created_at.desc())
                                       .limit(max(n, 1) * 3 + 5))).scalars())
    out: list[Run] = []
    for run in rows:
        if await sealed_now(session, run):
            out.append(run)
            if len(out) >= n:
                break
    return out


def input_key(inputs: Any) -> str:
    """一次运行的输入的指纹：补算时认「同一个输入算过没有」，趋势里认补算和正常运行是不是同一期。"""
    return hashlib.sha256(canonical_json(inputs if isinstance(inputs, dict) else {}).encode()).hexdigest()


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def period_of(run: Run) -> datetime | None:
    """这次运行对应的输入期，趋势按它排：正常运行就是它结束的时间；补算的运行是现在才算的，按它补的那一次原来的时间
    （origin.period_at，补算发起时记下）。"""
    if run.origin_kind == BACKFILL_ORIGIN:
        raw = (run.origin or {}).get("period_at")
        if isinstance(raw, str):
            try:
                return _aware(datetime.fromisoformat(raw))
            except ValueError:
                pass
    return _aware(run.finished_at or run.created_at)


async def trend_runs(session: AsyncSession, app_id: str | None, workflow_id: Any, version: Any, graph_hash: Any,
                     origin: str | None, n: int) -> list[Run]:
    """趋势要画的那几次：同一来源最近 n 期，按输入期从早到晚。

    来源本身的封存运行（sealed_runs），加上这个应用为同一来源按新口径补算的运行（app_backfill，origin.source 记着补的
    是哪个来源）——补算的是新版本的点，按它补的那一期排进去（period_of）。同一个输入既有正常运行又有补算的，取正常
    运行：补算只是替缺的那几期补上。"""
    from app.scheduler.auto_compare import sealed_now

    own = await sealed_runs(session, workflow_id, version, graph_hash, origin, n)
    backfilled: list[Run] = []
    if app_id and workflow_id and isinstance(version, int) and graph_hash:
        rows = (await session.execute(select(Run).where(
            Run.app_id == app_id, Run.origin_kind == BACKFILL_ORIGIN, Run.workflow_id == workflow_id,
            Run.version == version, Run.version_hash == graph_hash, Run.run_class == "formal",
            Run.status == "succeeded").order_by(Run.created_at.desc()).limit(max(n, 1) * 3 + 5))).scalars()
        seen = {input_key(r.input) for r in own}
        for run in rows:
            key = input_key(run.input)
            if (run.origin or {}).get("source") != (origin or "formal") or key in seen:
                continue
            if await sealed_now(session, run):
                seen.add(key)
                backfilled.append(run)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    merged = sorted([*own, *backfilled], key=lambda r: (period_of(r) or floor, _aware(r.created_at) or floor))
    return merged[-n:] if n > 0 else []


@dataclass
class Source:
    id: str
    kind: str
    state: str
    run: Run | None = None
    pin: PinState | None = None
    sealed: Any = None
    stamp: dict[str, Any] | None = None
    message: str | None = None
    origin: str | None = None
    _masks: Any = None

    def out(self) -> dict[str, Any]:
        # kind（latest_sealed / on_demand / instance）：界面据此在按需视图的印章旁给「刷新」
        return {"state": self.state, "kind": self.kind, "run_id": self.run.id if self.run else None,
                "stamp": self.stamp, **({"message": self.message} if self.message else {})}

    @property
    def masks(self) -> Any:
        from app.api.evidence import _Masks

        if self._masks is None:
            self._masks = _Masks(self.sealed)
        return self._masks


# --------------------------------------------------------------------------
# 印章
# --------------------------------------------------------------------------


def _when(value: datetime | None) -> str:
    if value is None:
        return ""
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


async def _from_text(session: AsyncSession, source: Source) -> str:
    run = source.run
    if source.kind == "instance":
        return "本实例"
    if source.kind == "on_demand":
        return "按需运行"
    if run is not None and run.schedule_id:
        schedule = await session.get(Schedule, run.schedule_id)
        fire = (await session.execute(select(ScheduleFire).where(ScheduleFire.run_id == run.id))).scalars().first()
        if fire is not None:
            n = await session.scalar(select(func.count(ScheduleFire.id)).where(
                ScheduleFire.schedule_id == run.schedule_id, ScheduleFire.status == "started",
                ScheduleFire.created_at <= fire.created_at))
        else:
            n = await session.scalar(select(func.count(Run.id)).where(
                Run.schedule_id == run.schedule_id, Run.created_at <= run.created_at))
        name = schedule.name if schedule is not None else "已删除的定时任务"
        return f"定时任务「{name}」第 {int(n or 1)} 次"
    return "正式运行"


async def stamp_of(session: AsyncSession, source: Source) -> dict[str, Any]:
    """截至（运行结束时间、输入里的统计期、数据版本）、口径（工作流名 vN · 哈希前 8 位，加口径卡的口径名和版本）、出具档位
    （_issuance.tier）、来源。版本错位、已退役、没封存都照实写进 notes。"""
    from app.api.evidence import _input_payloads, _issuance_of, _sealed_cards

    run, sealed, pin = source.run, source.sealed, source.pin
    assert run is not None and sealed is not None
    parts = [f"截至 {_when(run.finished_at)}"] if run.finished_at else ["尚未结束"]
    # 输入取入口节点封存的产出（补过默认值的那一份），不取运行记录上发起时交的
    inputs = _input_payloads(sealed) or dict(run.input or {})
    period = [f"{k} {v}" for k, v in inputs.items()
              if isinstance(v, (str, int)) and not isinstance(v, bool) and _PERIOD.search(str(k))][:2]
    if period:
        parts.append("统计期 " + "、".join(period))
    if run.data_versions:
        parts.append("数据版本 " + "、".join(f"{v.get('name')} {str(v.get('snapshot') or '')[:8]}"
                                         for v in run.data_versions.values() if isinstance(v, dict)))
    calibers: list[dict[str, str]] = []
    for _, _, card in _sealed_cards(sealed):
        item = {"name": str(card.get("caliber") or ""), "version": str(card.get("caliber_version") or "")}
        if item["name"] and item not in calibers:
            calibers.append(item)
    issuance = _issuance_of(sealed)
    tier = issuance.get("tier") if isinstance(issuance, dict) else None
    notes: list[str] = []
    workflow_name = pin.name if pin is not None else run.workflow_name
    if pin is not None and pin.workflow is not None:
        current = pin.workflow.published_version
        if pin.retired:
            notes.append(f"固定的 v{pin.pin.get('version')} 已退役，这里保留最后一次的数据；应用需要升级")
        elif current and current != pin.pin.get("version"):
            if run.schedule_id:
                notes.append(f"定时任务已改为运行 v{current}，本应用固定在 v{pin.pin.get('version')}")
            else:
                notes.append(f"工作流的当前发布版本是 v{current}，本应用固定在 v{pin.pin.get('version')}")
    if not sealed.trusted:
        notes.append("这次运行还没有封存（或封存核对不一致），数字仅供参考")
    return {
        "as_of": run.finished_at.isoformat() if run.finished_at else None,
        "as_of_text": "；".join(parts),
        "pin": f"{workflow_name} v{run.version}" if run.version else workflow_name,
        "graph_hash": (run.version_hash or "")[:8],
        "calibers": calibers,
        "tier": tier if tier in TIER_TEXT else None,
        # 纯指标看板按指标出具（issuance.metrics_only）：印章上说清这一档核的是指标本身，不是没核
        "tier_text": TIER_TEXT.get(str(tier), NO_TIER) + (TIER_BY_METRICS if tier in TIER_TEXT and isinstance(
            issuance, dict) and issuance.get("mode") == "metrics" else ""),
        "from": await _from_text(session, source),
        "sealed": bool(sealed.trusted),
        **({"notes": notes} if notes else {}),
    }


# --------------------------------------------------------------------------
# 按需视图：缓存键、单飞、每日上限
# --------------------------------------------------------------------------

_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


def normalize_params(form: dict[str, Any] | None, params: Any) -> dict[str, Any]:
    """按参数表单收下的输入：表单里没有的键丢掉，空值丢掉（入口节点补默认值），按键排好。没有表单时原样（只去空值）。"""
    if not isinstance(params, dict):
        return {}
    props = (form or {}).get("properties") if form else None
    return {k: params[k] for k in sorted(params)
            if params[k] not in (None, "") and (props is None or k in props)}


async def cache_key(session: AsyncSession, state: PinState, inputs: dict[str, Any]) -> str:
    from app.capabilities.registry import capability_versions_for
    from app.engine.launch import graph_tool_names
    from app.engine.runner import _data_versions
    from app.engine.schema import GraphSpec

    try:
        versions = await _data_versions(session, GraphSpec.model_validate(state.graph))
    except ValueError:
        versions = {}
    capabilities = await capability_versions_for(session, graph_tool_names(state.graph))
    text = canonical_json({"graph_hash": state.hash, "input": inputs,
                           "data_versions": {k: (v or {}).get("snapshot") for k, v in sorted(versions.items())},
                           "capabilities": capabilities})
    return hashlib.sha256(text.encode()).hexdigest()


async def daily_limit(session: AsyncSession) -> int:
    """每个应用每天最多发起几次按需运行：设置里的 apps.on_demand_daily_limit（D-j，默认值和取值范围只在 api/settings.py
    一处定，设置页改的就是它）。"""
    from app.api.settings import apps_settings

    return int((await apps_settings(session))["on_demand_daily_limit"])


async def _cached(session: AsyncSession, key: str) -> Run | None:
    """缓存里还算数的那次运行：没过期，而且没有失败、取消。失败的那一条删掉，下次刷新重新运行。"""
    row = await session.get(AppViewCache, key)
    if row is None:
        return None
    now = utcnow()
    expires = row.expires_at if row.expires_at is None or row.expires_at.tzinfo else row.expires_at.replace(
        tzinfo=timezone.utc)
    run = await session.get(Run, row.run_id)
    if (expires is not None and expires <= now) or run is None or run.status in ("failed", "cancelled"):
        await session.delete(row)
        await session.commit()
        return None
    return run


async def run_view(session: AsyncSession, *, app: Any, version: AppVersion, view_id: str, params: Any,
                   principal: Principal, actor: str | None) -> tuple[Run, bool]:
    """按需视图发起一次运行（或者命中缓存、或者接上同一个键正在运行的那次）：(运行, 是否命中缓存)。"""
    from app.apps.entries import FormRejected
    from app.apps.forms import validate_form
    from app.apps.lint import TTL_DEFAULT, TTL_MAX
    from app.apps.pins import reads_live
    from app.engine.launch import launch_run

    spec = version.spec or {}
    view = (spec.get("views") or {}).get(view_id)
    source = (view or {}).get("source") or {}
    if source.get("kind") != "on_demand":
        raise ViewRefused(409, "这个视图不是按需运行的视图，没有刷新可做", VIEW_REFUSED)
    pins = await load_pins(session, spec)
    state = pins.get(source.get("pin"))
    if state is None or not state.usable:
        raise ViewRefused(409, "视图固定的工作流版本已退役或不可用，应用需要升级后才能刷新", NEEDS_UPGRADE)
    form = resolve_form(spec, source.get("params")) if source.get("params") is not None else None
    if form is not None:
        if errors := validate_form(form, params if isinstance(params, dict) else {}):
            raise FormRejected(errors)
    inputs = normalize_params(form, params)
    key = await cache_key(session, state, inputs)
    async with _lock(key):
        run = await _cached(session, key)
        if run is not None:
            return run, run.status == "succeeded"
        # 「每天」按服务端所在时区的自然日算
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        used = await session.scalar(select(func.count(Run.id)).where(
            Run.app_id == app.id, Run.origin_kind == "app_view", Run.created_at >= start))
        limit = await daily_limit(session)
        if int(used or 0) >= limit:
            raise ViewRefused(429, f"这个应用今天的按需运行已达到上限（{limit} 次），请明天再试，或请管理员调整上限",
                              DAILY_LIMIT)
        run = await launch_run(
            session, workflow_id=state.pin["workflow_id"], run_class="formal", version=state.pin["version"],
            expect_hash=state.pin.get("graph_hash"), input_payload=inputs, actor=actor, origin_kind="app_view",
            app_id=app.id, app_version=version.version,
            origin={"view": view_id, "spec_hash": version.spec_hash, "actor_verified": principal.verified,
                    "auth_mode": principal.mode})
        ttl = source.get("ttl_s")
        if ttl is None and await reads_live(session, state.graph):
            ttl = TTL_DEFAULT
        expires = utcnow() + timedelta(seconds=min(int(ttl), TTL_MAX)) if ttl else None
        await session.execute(delete(AppViewCache).where(AppViewCache.key == key))
        session.add(AppViewCache(key=key, app_id=app.id, view_id=view_id, run_id=run.id, expires_at=expires))
        await session.commit()
        return run, False


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


@dataclass
class _Page:
    app: Any
    spec: dict[str, Any]
    version: AppVersion | None
    principal: Principal
    role: str | None
    instance: Run | None
    params: dict[str, Any]
    draft: bool
    actor: str | None
    #: 正在解析的这一页（组件之间要互相知道的：同一页上有没有绑同一条待办的会签组件）
    page: dict[str, Any] = field(default_factory=dict)
    pins: dict[str, PinState] = field(default_factory=dict)
    sources: dict[str, Source] = field(default_factory=dict)
    problems: list[dict[str, Any]] = field(default_factory=list)
    forms: dict[str, Any] = field(default_factory=dict)

    def problem(self, component: str, code: str, message: str) -> None:
        self.problems.append({"component": component, "code": code, "message": message})


_PAGE_CACHE: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()


def clear_cache() -> None:
    _PAGE_CACHE.clear()


async def _mask_fingerprint(session: AsyncSession) -> str:
    """遮罩设置的指纹：各数据源的遮罩列、各能力的遮罩字段。进页面缓存的键：管理员给数据源新加了遮罩列、改了能力的
    遮罩之后，来源运行没换，缓存里的视图也不能再交出去（P4）。按库里现在的样子算，不靠各改动处记得清缓存——数据源、
    能力登记的改动入口不止一处。"""
    from app.data.engine import masked_columns
    from app.db.models import Capability, DataSource

    sources = sorted((name, sorted(masked_columns(options)))
                     for name, options in (await session.execute(select(DataSource.name, DataSource.options))).all())
    caps = sorted((name, sorted(str(p) for p in masked)) for name, masked in
                  (await session.execute(select(Capability.name, Capability.masked))).all() if masked)
    return hashlib.sha256(canonical_json({"sources": sources, "capabilities": caps}).encode()).hexdigest()


async def _backfilled(session: AsyncSession, app_id: str) -> int:
    """这个应用补算完成了几次：进页面缓存的键。补算的运行不是「最近一次」，来源取的运行不变，趋势却多了点，键得跟着变。"""
    return int(await session.scalar(select(func.count(Run.id)).where(
        Run.app_id == app_id, Run.origin_kind == BACKFILL_ORIGIN, Run.status == "succeeded")) or 0)


async def resolve_page(session: AsyncSession, app: Any, *, page_id: str, principal: Principal, role: str | None,
                       instance: Run | None = None, params: dict[str, Any] | None = None, draft: bool = False,
                       version: AppVersion | None = None, actor: str | None = None) -> dict[str, Any]:
    """解析一页，返回 agentlab.view/1。draft 为真时按草稿解析：不发起任何运行，视图取钉住版本已有的封存运行。"""
    import copy

    from app.apps.tree import app_level

    spec = copy.deepcopy(app.draft if draft else (version.spec if version is not None else {})) or {}
    page = page_of(spec, page_id)
    if page is None:
        raise ViewRefused(404, f"应用里没有页面「{page_id}」", "app_page_not_found")
    ctx = _Page(app=app, spec=spec, version=None if draft else version, principal=principal, role=role,
                instance=instance, params=params or {}, draft=draft, actor=actor, page=page)
    ctx.pins = await load_pins(session, spec)
    upgrade = needs_upgrade(ctx.pins)
    level = version.level if (version is not None and not draft) else app_level(
        [{"level": p.level} for p in ctx.pins.values()])
    wanted = _views_of(page)
    for vid in wanted:
        ctx.sources[vid] = await _source(session, ctx, vid)
    # 键：应用版本、页面、各来源取的那次运行、角色。另加各钉住项所在工作流的当前发布版和退役与否：印章上「定时任务已改为
    # 运行 vN」「已退役」随它们变，运行没换也得重新算；遮罩设置的指纹同理（_mask_fingerprint）
    key = (app.id, "draft" if draft else (version.version if version else None), page_id,
           tuple((v, s.run.id if s.run else None, s.state, s.message) for v, s in sorted(ctx.sources.items())),
           tuple((a, p.workflow.published_version if p.workflow else None, p.retired)
                 for a, p in sorted(ctx.pins.items())),
           role, principal.username, principal.role, await _mask_fingerprint(session),
           await _backfilled(session, app.id))
    cacheable = (not draft and instance is None and all(s.state in ("ok", "empty") for s in ctx.sources.values())
                 and not _dynamic(page))
    if cacheable and key in _PAGE_CACHE:
        _PAGE_CACHE.move_to_end(key)
        return copy.deepcopy(_PAGE_CACHE[key])
    # 还没有数据（没有封存运行、按需视图没刷新、不在实例里）是正常的空态，不是解析出错：各块就地写明还没有数据、什么时候
    # 会有（Source.message），不进 problems——进了，页面顶上挂一块「无法显示」，使用者以为应用坏了
    for source in ctx.sources.values():
        await _load(session, source)
    for entry in spec.get("entries") or []:
        form = resolve_form(spec, entry.get("form"))
        if form is not None and entry.get("id"):
            ctx.forms[str(entry["id"])] = form
    tree = [n for c in page.get("body") or [] if (n := await _node(session, ctx, c)) is not None]
    out = {
        "format": VIEW_FORMAT,
        "app": {"id": app.id, "name": spec.get("name") or app.name,
                "version": (version.version if version is not None and not draft else (app.published_version or 0)),
                "level": level, "needs_upgrade": upgrade},
        "page": {"id": page.get("id"), "title": page.get("title") or ""},
        "pages": [{"id": p.get("id"), "title": p.get("title") or ""} for p in spec.get("pages") or []
                  if isinstance(p, dict)],
        "sources": {vid: s.out() for vid, s in ctx.sources.items()},
        "tree": tree,
        "forms": ctx.forms,
        "problems": ctx.problems,
    }
    if cacheable:
        _PAGE_CACHE[key] = copy.deepcopy(out)
        while len(_PAGE_CACHE) > PAGE_CACHE_SIZE:
            _PAGE_CACHE.popitem(last=False)
    return out


_DYNAMIC = frozenset({"instance_list", "task_list", "instance_header", "instance_timeline", "task_form", "signoffs",
                      "action_status", "entry_button"})


def _dynamic(page: dict[str, Any]) -> bool:
    """页面上有随人、随时变的组件（实例列表、待办……）：不进页面缓存。"""
    from app.apps.tree import components

    return any(c.get("type") in _DYNAMIC for c, _, _ in components({"pages": [page]}))


def _views_of(page: dict[str, Any]) -> list[str]:
    from app.apps.tree import components

    out: list[str] = []
    for comp, _, _ in components({"pages": [page]}):
        for vid in ((comp.get("bind") or {}).get("view") if isinstance(comp.get("bind"), dict) else None,
                    comp.get("scope")):
            if isinstance(vid, str) and vid not in out:
                out.append(vid)
    return out


async def _source(session: AsyncSession, ctx: _Page, vid: str) -> Source:
    """这个视图取哪一次运行（还不读封存范围，见 _load）。"""
    view = (ctx.spec.get("views") or {}).get(vid)
    src = (view or {}).get("source") or {}
    kind = src.get("kind")
    if kind is None:
        return Source(id=vid, kind="", state="error", message=f"视图「{vid}」不存在")
    state = ctx.pins.get(src.get("pin")) if kind != "instance" else None
    run: Run | None = None
    if kind == "instance":
        run = ctx.instance
        if run is None:
            return Source(id=vid, kind=kind, state="empty", message="打开某个实例时才有数据")
        state = next((p for p in ctx.pins.values() if p.pin.get("workflow_id") == run.workflow_id
                      and p.pin.get("version") == run.version), None)
    elif state is None:
        return Source(id=vid, kind=kind, state="error", message=f"视图「{vid}」引用的工作流版本不在应用里")
    elif kind == "latest_sealed":
        run = await latest_sealed_run(session, state, src.get("from"))
        if run is None:
            return Source(id=vid, kind=kind, state="empty", pin=state, origin=src.get("from"),
                          message=await _empty_text(session, state, src.get("from")))
    else:
        run = await _on_demand_run(session, ctx, vid, src, state)
        if run is None:
            return Source(id=vid, kind=kind, state="empty", pin=state,
                          message="按需运行的视图：点「刷新」后按当前参数运行一次")
    if run.status in _ACTIVE:
        return Source(id=vid, kind=kind, state="running", run=run, pin=state, message=await _active_text(session, run))
    source = Source(id=vid, kind=kind, state="ok" if run.status == "succeeded" else "error", run=run, pin=state,
                    origin=src.get("from"))
    if run.status != "succeeded":
        source.message = f"这次运行{STATUS_TEXT.get(run.status, run.status)}"
    return source


#: 来源的运行还没结束时各块写什么。页面在跟着（应用页轮询、实例页轮询或接事件流），不用人去刷新
RUNNING_TEXT = "正在运行，完成后自动刷新"


async def _active_text(session: AsyncSession, run: Run) -> str:
    """来源的运行还没结束：在跑写「正在运行」；停着等人（interrupted）按在等的那一步说等什么（等待审批、等待确认发送、
    等待核实写入结果……，同实例的状态 instance_state），不说「正在运行」——页头写着「等待审批」，这里不能对不上。"""
    if run.status != "interrupted":
        return RUNNING_TEXT
    text = instance_state(run, (await _trails(session, [run]))[run.id])["text"]
    return f"{text}，流程走完后自动显示"


async def _empty_text(session: AsyncSession, state: PinState, origin: Any) -> str:
    """最近封存运行的视图还没有数据时说什么：还没有、什么时候会有（定时任务的下次运行时间，或者要等一次正式运行）。"""
    if isinstance(origin, str) and origin.startswith("schedule:"):
        schedule = await session.get(Schedule, origin[len("schedule:"):])
        if schedule is None:
            return f"{EMPTY_TEXT}：取数的定时任务已删除，请管理应用的人换一个来源"
        if not schedule.enabled:
            return f"{EMPTY_TEXT}：定时任务「{schedule.name}」已停用，启用并运行完成一次后这里才有数"
        if schedule.next_run_at is not None:
            return f"{EMPTY_TEXT}：定时任务「{schedule.name}」下一次在 {_when(schedule.next_run_at)} 运行，完成并封存后这里就有数"
        return f"{EMPTY_TEXT}：定时任务「{schedule.name}」运行完成并封存后这里就有数"
    return f"{EMPTY_TEXT}：「{state.label}」有一次正式运行成功并封存后这里就有数"


async def _load(session: AsyncSession, source: Source) -> None:
    """读封存范围（核对清单哈希）、算印章。挑运行很便宜，这一步不便宜：页面缓存命中时不做。"""
    from app.api.evidence import _sealed

    if source.run is None or source.run.status in _ACTIVE:
        return
    source.sealed = await _sealed(source.run.id)
    source.stamp = await stamp_of(session, source)


async def _on_demand_run(session: AsyncSession, ctx: _Page, vid: str, src: dict[str, Any], state: PinState) -> Run | None:
    """按需视图打开页面时：有缓存用缓存；声明了 auto、没有缓存、不是草稿预览时自动发起一次。"""
    form = resolve_form(ctx.spec, src.get("params")) if src.get("params") is not None else None
    inputs = normalize_params(form, ctx.params)
    run = await _cached(session, await cache_key(session, state, inputs))
    if run is not None or ctx.draft or not src.get("auto") or ctx.version is None or not state.usable:
        return run
    try:
        run, _ = await run_view(session, app=ctx.app, version=ctx.version, view_id=vid, params=ctx.params,
                                principal=ctx.principal, actor=ctx.actor)
    except Exception as e:  # noqa: BLE001 - 自动发起不成（上限、参数不全）照实写进问题，页面照常给
        ctx.problem(vid, "view.auto_refused", getattr(e, "message", None) or "自动发起按需运行未成功")
        return None
    return run


# --------------------------------------------------------------------------
# 组件
# --------------------------------------------------------------------------


async def _node(session: AsyncSession, ctx: _Page, comp: dict[str, Any]) -> dict[str, Any] | None:
    from app.apps.catalog import BY_NAME

    if not isinstance(comp, dict):
        return None
    cid = str(comp.get("id"))
    if comp.get("visible") and not await _visible(session, ctx, comp):
        return None
    node: dict[str, Any] = {"id": cid, "type": comp.get("type"), "props": dict(comp.get("props") or {})}
    entry = BY_NAME.get(str(comp.get("type")))
    if entry is not None and entry["children"]:
        node["children"] = [n for c in comp.get("children") or [] if (n := await _node(session, ctx, c)) is not None]
    try:
        data = await _data(session, ctx, comp)
    except Exception as e:  # noqa: BLE001 - 一个组件解析出错不拖垮整页，照实写进问题
        import logging

        logging.getLogger(__name__).exception("应用组件 %s 解析出错", cid)
        ctx.problem(cid, "view.resolve_failed", f"解析出错：{type(e).__name__}")
        data = {"state": "error"}
    if data is not None:
        node["data"] = data
    return node


async def _visible(session: AsyncSession, ctx: _Page, comp: dict[str, Any]) -> bool:
    from app.engine.expressions import ExpressionError, eval_expression

    scope = comp.get("scope") or ((comp.get("bind") or {}).get("view") if isinstance(comp.get("bind"), dict) else None)
    source = ctx.sources.get(scope) if scope else None
    try:
        return bool(eval_expression(str(comp["visible"]), await _expr_ctx(session, ctx, source), roots=APP_ROOTS,
                                    arithmetic=False))
    except ExpressionError as e:
        ctx.problem(str(comp.get("id")), "app.expr_invalid", f"可见条件无法求值，按不可见处理：{e}")
        return False


async def _expr_ctx(session: AsyncSession, ctx: _Page, source: Source | None) -> dict[str, Any]:
    from app.api.evidence import _issuance_of, _sealed_cards

    run = source.run if source is not None else None
    sealed = source.sealed if source is not None else None
    issuance = _issuance_of(sealed) if sealed is not None else None
    metrics: dict[str, dict[str, Any]] = {}
    if sealed is not None:
        for node_id, _, card in _sealed_cards(sealed):
            metrics[node_id] = {str(m.get("id")): m.get("value") for m in card.get("metrics") or []
                                if isinstance(m, dict) and m.get("id")}
    inst = ctx.instance
    return {
        "run": {"status": run.status if run else None, "tier": (issuance or {}).get("tier"),
                "finished_at": run.finished_at.isoformat() if run and run.finished_at else None,
                "version": run.version if run else None, "sealed": bool(sealed.trusted) if sealed else False},
        "input": dict(run.input or {}) if run else {},
        "metric": metrics,
        "instance": {"status": inst.status, "started_by": inst.started_by,
                     "title": (inst.origin or {}).get("title")} if inst is not None else {},
        "task": await _task_states(session, inst) if inst is not None else {},
        "user": {"username": ctx.principal.username, "display_name": ctx.principal.display_name,
                 "role": ctx.principal.role, "groups": list(ctx.principal.groups), "app_role": ctx.role},
        "view": {vid: {"state": s.state} for vid, s in ctx.sources.items()},
    }


async def _task_states(session: AsyncSession, run: Run) -> dict[str, Any]:
    from app.db.models_process import Task

    rows = (await session.execute(select(Task.node_id, Task.status).where(Task.run_id == run.id)
                                  .order_by(Task.created_at))).all()
    return {str(node): {"status": status} for node, status in rows if node}


async def _data(session: AsyncSession, ctx: _Page, comp: dict[str, Any]) -> dict[str, Any] | None:
    ctype = comp.get("type")
    bind = comp.get("bind") if isinstance(comp.get("bind"), dict) else None
    props = comp.get("props") or {}
    cid = str(comp.get("id"))
    if ctype == "entry_button":
        entry = next((e for e in ctx.spec.get("entries") or [] if e.get("id") == props.get("entry")), None)
        blocked = needs_upgrade({a: p for a, p in ctx.pins.items() if entry and a == entry.get("pin")})
        return {"entry": props.get("entry"), "label": props.get("label") or (entry or {}).get("label") or "",
                "disabled": blocked or entry is None,
                **({"reason": "固定的工作流版本已退役，应用需要升级后才能发起"} if blocked else {})}
    if ctype == "form":
        form = (ctx.spec.get("forms") or {}).get(props.get("form"))
        if isinstance(form, dict):
            ctx.forms[str(props.get("form"))] = form
        return {"form": props.get("form")}
    if ctype == "instance_list":
        return await _instance_list(session, ctx, props)
    if ctype == "instance_header":
        return await _instance_header(session, ctx)
    if ctype == "instance_timeline":
        return await _timeline(session, ctx)
    if ctype == "task_list":
        return await _task_list(session, ctx, props)
    kind = bind_kind(bind)
    if kind in ("task", "action"):
        return await _process_data(session, ctx, ctype, kind, str(bind[kind]))
    if bind is None:
        return None
    source = ctx.sources.get(str(bind.get("view")))
    if source is None:
        ctx.problem(cid, "app.binding_target_missing", f"绑定的视图「{bind.get('view')}」不存在")
        return {"state": "error"}
    if ctype == "stamp":
        return {"view": source.id, "state": source.state, "stamp": source.stamp,
                **({"message": source.message} if source.message else {})}
    if source.state != "ok" or source.sealed is None:
        return {"state": source.state if source.state != "ok" else "empty",
                **({"message": source.message} if source.message else {})}
    if kind == "metric":
        return await _metric(ctx, source, cid, str(bind["metric"]), props.get("conv"))
    if kind == "series":
        return await _series(session, ctx, source, cid, str(bind["series"]), bind.get("last"))
    if kind == "cell":
        return await _cell(ctx, source, cid, bind["cell"], props.get("conv"))
    if kind == "query":
        return await _table(ctx, source, cid, bind["query"], props)
    if kind == "report":
        return _report(ctx, source, cid, str(bind["report"]))
    if kind == "input":
        return _input(ctx, source, cid, str(bind["input"]))
    if kind == "run":
        return _run_info(source, str(bind["run"]))
    if kind == "output":
        return _output(ctx, source, cid, str(bind["output"]))
    return None


def _evidence(source: Source, kind: str, **extra: Any) -> dict[str, Any]:
    assert source.run is not None
    return {"run_id": source.run.id, "kind": kind, **{k: v for k, v in extra.items() if v is not None}}


def _card(source: Source, node_id: str) -> tuple[str, dict[str, Any]] | None:
    """这个口径卡节点最后一次执行封存的指标集：(工件, 指标集)。"""
    from app.api.evidence import _sealed_cards

    found = [(art, card) for n, art, card in _sealed_cards(source.sealed) if n == node_id]
    return found[-1] if found else None


async def _metric_value(source: Source, node_id: str, metric_id: str, conv: Any
                        ) -> tuple[dict[str, Any] | None, str, str | None, str | None]:
    """(指标, 渲染出来的字, 证据 eid, 不给值时写的字)。"""
    from app.api.evidence import MASKED, _metric_in, _metric_masks
    from app.engine.evidence import MISSING, RenderError, make_eid, render_metric

    found = _card(source, node_id)
    if found is None:
        return None, MISSING, None, None
    artifact, card = found
    metric = _metric_in(card, metric_id)
    if metric is None:
        return None, MISSING, None, None
    _, word = await _metric_masks(metric, node_id, artifact, source.sealed, source.masks, card=card)
    eid = make_eid("metric", artifact, {"metric": metric_id})
    if word is not None:
        return metric, word, eid, word
    if metric.get("masked"):
        return metric, MASKED, eid, MASKED
    try:
        return metric, render_metric(metric, conv if isinstance(conv, str) and conv else None), eid, None
    except RenderError:
        return metric, MISSING, eid, None


async def _metric(ctx: _Page, source: Source, cid: str, ref: str, conv: Any) -> dict[str, Any]:
    node_id, _, metric_id = ref.partition(".")
    metric, text, eid, held = await _metric_value(source, node_id, metric_id, conv)
    if metric is None:
        ctx.problem(cid, "app.binding_target_missing", f"这次运行封存的口径卡里没有指标「{ref}」")
        return {"state": "missing", "text": text}
    return {"state": "masked" if held else ("ok" if metric.get("value") is not None else "missing"), "text": text,
            "name": metric.get("name") or metric_id, "unit": metric.get("unit") or "",
            "evidence": _evidence(source, "metric", node=node_id, metric=metric_id, eid=eid)}


async def _series(session: AsyncSession, ctx: _Page, source: Source, cid: str, ref: str, last: Any) -> dict[str, Any]:
    """趋势：同一来源最近 N 次封存运行的指标值，只排成一列、不算差值。跨钉住版本按处置来画：dual 两条线；incomparable
    在升版处断开并标注；recompute 只取新版本（按历次输入补算另行发起，来源记 app_backfill）。"""
    from app.api.evidence import _sealed

    n = min(last, SERIES_MAX) if isinstance(last, int) and not isinstance(last, bool) and last > 0 else 12
    node_id, _, metric_id = ref.partition(".")
    pin = source.pin
    if pin is None:
        return {"state": "error"}
    origin = source.origin
    segments: list[tuple[int, str, list[Run]]] = []
    mode = "single"
    previous, disposition = await _upgrade_of(session, ctx, pin.alias)
    app_id = ctx.app.id if ctx.app is not None else None
    if previous is not None and disposition in ("dual", "incomparable"):
        mode = disposition
        old_runs = await trend_runs(session, app_id, previous.get("workflow_id"), previous.get("version"),
                                    previous.get("graph_hash"), origin, n)
        segments.append((int(previous.get("version")), str(previous.get("graph_hash")), old_runs))
    elif disposition == "recompute":
        mode = "recompute"
    # 新版本这一段带上按新口径补算的那几期（trend_runs）：recompute 升版之后补算过，趋势就按新口径从头画
    runs = await trend_runs(session, app_id, pin.pin.get("workflow_id"), pin.pin.get("version"),
                            pin.pin.get("graph_hash"), origin, n)
    segments.append((int(pin.pin.get("version")), str(pin.pin.get("graph_hash")), runs))
    series: list[dict[str, Any]] = []
    unit = ""
    for version, _, rows in segments:
        points = []
        for run in rows:
            sealed = source.sealed if run.id == source.run.id else await _sealed(run.id)
            point_source = Source(id=source.id, kind=source.kind, state="ok", run=run, pin=pin, sealed=sealed)
            metric, text, eid, held = await _metric_value(point_source, node_id, metric_id, None)
            if metric is not None:
                unit = unit or str(metric.get("unit") or "")
            period = period_of(run)
            points.append({"run_id": run.id, "as_of": period.isoformat() if period else None,
                           "value": None if held or metric is None else metric.get("value"), "text": text,
                           **({"backfill": True} if run.origin_kind == BACKFILL_ORIGIN else {}),
                           "evidence": _evidence(point_source, "metric", node=node_id, metric=metric_id, eid=eid)
                           if metric is not None else None})
        series.append({"version": version, "label": f"v{version}", "points": points})
    breaks = []
    if mode == "incomparable" and len(series) == 2 and series[0]["points"]:
        breaks.append({"after_run": series[0]["points"][-1]["run_id"], "from": series[0]["version"],
                       "to": series[1]["version"],
                       "text": f"口径从 v{series[0]['version']} 升到 v{series[1]['version']}，前后不可比"})
    if mode == "recompute":
        breaks.append({"from": None, "to": series[-1]["version"],
                       "text": f"升版到 v{series[-1]['version']} 时选择了按新口径重算：只画新版本的数"})
    # 一个点都没有是空态（界面写「还没有数据点」），不是解析出错，不进 problems
    return {"state": "ok", "mode": mode, "unit": unit, "series": series, "breaks": breaks}


async def _upgrade_of(session: AsyncSession, ctx: _Page, alias: str) -> tuple[dict[str, Any] | None, str | None]:
    if ctx.version is None:
        return None, None
    return await pin_upgrade(session, ctx.app.id, ctx.version, alias)


async def pin_upgrade(session: AsyncSession, app_id: str, version: AppVersion,
                      alias: str) -> tuple[dict[str, Any] | None, str | None]:
    """这个钉住项最近一次升版：(升版前的钉住项, 当时选的处置)。从这个应用版本往回找第一次换了工作流版本的地方——之后
    又发过几版（改布局、改文案）不影响趋势按那次处置来画、也不影响按新口径补算。没升过版的是 (None, None)。"""
    if version.version <= 1:
        return None, None
    rows = list((await session.execute(select(AppVersion).where(
        AppVersion.app_id == app_id, AppVersion.version <= version.version)
        .order_by(AppVersion.version.desc()))).scalars())
    if not rows:
        return None, None
    rows[0] = version
    for newer, older in zip(rows, rows[1:]):
        new = next((p for p in newer.pins or [] if p.get("alias") == alias), None)
        old = next((p for p in older.pins or [] if p.get("alias") == alias), None)
        if new is None or old is None or new.get("workflow_id") != old.get("workflow_id"):
            return None, None
        if new.get("version") != old.get("version"):
            return old, (newer.dispositions or {}).get(alias)
    return None, None


def _query_artifact(source: Source, node_id: str, exec_no: Any = None) -> str | None:
    """这个节点在封存范围内交回的查询快照：默认最后一次，exec 给了取第几次（从 1 数）。"""
    found = [a for a, info in source.sealed.queries.items() if info.get("node_id") == node_id]
    if not found:
        return None
    if isinstance(exec_no, int) and not isinstance(exec_no, bool) and 1 <= exec_no <= len(found):
        return found[exec_no - 1]
    return found[-1]


async def _snapshot(source: Source, artifact: str) -> tuple[dict[str, Any] | None, set[str]]:
    """查询快照和要遮的列（小写）：查询当时记下的加数据源现在设的，按 SQL 认出改了名字的也遮（同证据面板）。端点快照
    按列名遮（见下）。"""
    from app.api.evidence import _fetch

    snap, ok = _fetch(artifact)
    if ok is not True or not isinstance(snap, dict) or not isinstance(snap.get("rows"), list):
        return None, set()
    if snap.get("capability"):
        # 端点快照：按列名遮（落快照时记下的，加上能力现在的遮罩字段），证据面板用的是同一个函数
        from app.capabilities.registry import snapshot_masked_columns

        return snap, await snapshot_masked_columns(snap)
    sets = await source.masks.sets(artifact, snap.get("source"), snap)
    return snap, set(sets.hidden)


async def _cell(ctx: _Page, source: Source, cid: str, ref: Any, conv: Any) -> dict[str, Any]:
    from app.api.evidence import MASKED
    from app.engine.evidence import MISSING, RenderError, cell_eid, render_cell

    ref = ref if isinstance(ref, dict) else {}
    node_id, row, col = str(ref.get("node")), ref.get("row"), str(ref.get("col"))
    artifact = _query_artifact(source, node_id, ref.get("exec"))
    snap, hidden = await _snapshot(source, artifact) if artifact else (None, set())
    if snap is None:
        ctx.problem(cid, "app.binding_target_missing", f"这次运行没有封存「{node_id}」的查询结果")
        return {"state": "missing", "text": MISSING}
    columns = [str(c) for c in snap.get("columns") or []]
    rows = snap["rows"]
    if col not in columns or not isinstance(row, int) or not 0 <= row < len(rows):
        ctx.problem(cid, "app.binding_target_missing", f"查询结果里没有第 {row} 行的「{col}」")
        return {"state": "missing", "text": MISSING}
    evidence = _evidence(source, "cell", node=node_id, artifact=artifact, eid=cell_eid(artifact, row, col))
    if col.lower() in hidden:
        return {"state": "masked", "text": MASKED, "evidence": evidence}
    value = rows[row][columns.index(col)] if isinstance(rows[row], list) else None
    kinds = snap.get("column_types") if isinstance(snap.get("column_types"), dict) else {}
    try:
        text = render_cell(value, conv if isinstance(conv, str) and conv else None, kind=kinds.get(col))
    except RenderError:
        text = MISSING
    return {"state": "ok", "text": text, "evidence": evidence}


async def _table(ctx: _Page, source: Source, cid: str, ref: Any, props: dict[str, Any]) -> dict[str, Any]:
    """表格、图表：快照的列和行，遮罩列换成「已遮罩」；只排序、不出合计行（叙述层无算术）。"""
    from app.api.evidence import MASKED

    ref = ref if isinstance(ref, dict) else {}
    node_id = str(ref.get("node"))
    artifact = _query_artifact(source, node_id, ref.get("exec"))
    snap, hidden = await _snapshot(source, artifact) if artifact else (None, set())
    if snap is None:
        ctx.problem(cid, "app.binding_target_missing", f"这次运行没有封存「{node_id}」的查询结果")
        return {"state": "missing", "columns": [], "rows": []}
    columns = [str(c) for c in snap.get("columns") or []]
    wanted = [c for c in props.get("columns") or [] if c in columns] or columns
    if props.get("x") or props.get("y"):
        wanted = [c for c in [props.get("x"), *(props.get("y") or [])] if c in columns] or wanted
    index = [columns.index(c) for c in wanted]
    limit = props.get("limit") if isinstance(props.get("limit"), int) else TABLE_ROWS_MAX
    rows = [[MASKED if columns[i].lower() in hidden else (r[i] if isinstance(r, list) and i < len(r) else None)
             for i in index] for r in snap["rows"][:min(limit, TABLE_ROWS_MAX)]]
    sort = props.get("sort")
    if isinstance(sort, str) and sort.lstrip("-") in wanted:
        at = wanted.index(sort.lstrip("-"))
        rows.sort(key=lambda r: (r[at] is None, _sortable(r[at])), reverse=sort.startswith("-"))
    kinds = snap.get("column_types") if isinstance(snap.get("column_types"), dict) else {}
    return {"state": "ok", "columns": wanted, "rows": rows, "column_types": {c: kinds[c] for c in wanted if c in kinds},
            "masked": [c for c in wanted if c.lower() in hidden], "total_rows": len(snap["rows"]),
            "truncated": bool(snap.get("truncated")) or len(snap["rows"]) > len(rows),
            "evidence": _evidence(source, "query", node=node_id, artifact=artifact)}


def _sortable(value: Any) -> tuple[int, Any]:
    from app.engine.expressions import cell_value

    value = cell_value(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, value)
    return (1, str(value))


def _report(ctx: _Page, source: Source, cid: str, node_id: str) -> dict[str, Any]:
    from app.api.evidence import _reports

    report = next((r for r in _reports(source.sealed) if r.node_id == node_id), None)
    if report is None:
        ctx.problem(cid, "app.binding_target_missing", f"这次运行没有封存「{node_id}」写的报告")
        return {"state": "missing"}
    if report.hash_ok is False:
        ctx.problem(cid, "view.report_tampered", "报告文档与哈希不一致，不能作为证据展示")
        return {"state": "error"}
    return {"state": "ok", "node": node_id, "doc_artifact": report.doc_artifact,
            "evidence": _evidence(source, "report", node=node_id, artifact=report.doc_artifact)}


def _input(ctx: _Page, source: Source, cid: str, name: str) -> dict[str, Any]:
    from app.api.evidence import _input_payloads
    from app.engine.evidence import input_eid

    inputs = _input_payloads(source.sealed) or dict(source.run.input or {})
    if name not in inputs:
        ctx.problem(cid, "app.binding_target_missing", f"这次运行的输入里没有「{name}」")
        return {"state": "missing", "text": ""}
    value = inputs[name]
    return {"state": "ok", "text": value if isinstance(value, str) else canonical_json(value),
            "evidence": _evidence(source, "input", eid=input_eid(name, value))}


def _run_info(source: Source, what: str) -> dict[str, Any]:
    run = source.run
    assert run is not None
    if what == "status":
        return {"state": "ok", "value": run.status, "text": STATUS_TEXT.get(run.status, run.status)}
    if what == "tier":
        tier = (source.stamp or {}).get("tier")
        return {"state": "ok", "value": tier, "text": TIER_TEXT.get(str(tier), NO_TIER)}
    return {"state": "ok", "value": run.finished_at.isoformat() if run.finished_at else None,
            "text": _when(run.finished_at)}


def _output(ctx: _Page, source: Source, cid: str, name: str) -> dict[str, Any]:
    """成果字段只允许两种：逐字取自某份报告的（_evidence 里记着），或者不含数字的文本。"""
    from app.api.evidence import _output_evidence

    sealed = source.sealed
    output = sealed.payload_of(sealed.nodes_of("output")) or {}
    if name not in output or name.startswith("_"):
        ctx.problem(cid, "app.binding_target_missing", f"这次运行的成果里没有「{name}」")
        return {"state": "missing", "text": ""}
    value = output[name]
    for item in _output_evidence(sealed):
        if name in (item.get("fields") or []):
            return {"state": "ok", "text": value if isinstance(value, str) else canonical_json(value),
                    "evidence": _evidence(source, "report", node=item.get("report_node"),
                                          artifact=item.get("doc_artifact"))}
    text = value if isinstance(value, str) else canonical_json(value)
    if _DIGIT.search(text):
        ctx.problem(cid, "app.binding_not_evidenced", f"成果字段「{name}」里有数字，却不是逐字取自报告的文字，不展示")
        return {"state": "withheld", "text": ""}
    return {"state": "ok", "text": text}


# --------------------------------------------------------------------------
# 流程组件：实例、待办、动作
# --------------------------------------------------------------------------


def instance_title(run: Run) -> str:
    return str((run.origin or {}).get("title") or run.workflow_name or "")


async def pending_counts(session: AsyncSession, run_ids: list[str]) -> dict[str, int]:
    from app.db.models_process import Task

    if not run_ids:
        return {}
    rows = await session.execute(select(Task.run_id, func.count(Task.id)).where(
        Task.run_id.in_(run_ids), Task.status == "pending").group_by(Task.run_id))
    return dict(rows.all())


#: 等人处理时，按在等的那条待办的种类说在等什么
WAITING_TEXT = {"approve": "等待审批", "tool": "等待审批", "code": "等待审批", "input": "等待补充输入",
                "edit": "等待改稿", "dual_check": "等待复核", "action_confirm": "等待确认发送",
                "reconcile": "等待核实写入结果", "signal": "等待外部系统"}
#: 写入外部系统的结果未知（已发出、没拿到确定的结果），等查询或人工核实
UNKNOWN_TEXT = "等待核实写入结果"
HELD_TEXT = "已挂起，等待继续运行"


@dataclass
class _Trail:
    """一次运行里说得清「办到哪了」的几样：待办（按开出的先后）、动作记录（按先后）、节点事件（按先后）。"""
    tasks: list[Any] = field(default_factory=list)
    actions: list[Any] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)


async def _trails(session: AsyncSession, runs: list[Run]) -> dict[str, _Trail]:
    from app.db.models_process import ActionRecord, Task

    ids = [r.id for r in runs]
    out = {run_id: _Trail() for run_id in ids}
    if not ids:
        return out
    for row in (await session.execute(select(Task.run_id, Task.node_id, Task.kind, Task.status, Task.outcome)
                                      .where(Task.run_id.in_(ids)).order_by(Task.created_at))).all():
        out[row.run_id].tasks.append(row)
    for row in (await session.execute(select(ActionRecord).where(ActionRecord.run_id.in_(ids))
                                      .order_by(ActionRecord.created_at, ActionRecord.attempt))).scalars():
        out[row.run_id].actions.append(row)
    # 节点事件只有结束了的运行要看（走到了哪个成果节点、停在哪个节点上）
    wanted = [r.id for r in runs if r.status in ("succeeded", "failed")]
    if wanted:
        for run_id, etype, node_id in (await session.execute(
                select(RunEvent.run_id, RunEvent.type, RunEvent.node_id).where(
                    RunEvent.run_id.in_(wanted), RunEvent.type.in_(("node.finished", "node.failed")))
                .order_by(RunEvent.seq))).all():
            if node_id:
                out[run_id].events.append((etype, node_id))
    return out


def _decision(trail: _Trail, node_id: str) -> tuple[str | None, str | None]:
    """这个节点最近一条处理完的待办：(种类, 结论 approve / reject / expired_reject / submit)。"""
    done = [t for t in trail.tasks if t.node_id == node_id and t.status in ("resolving", "resolved")]
    if not done:
        return None, None
    return done[-1].kind, (done[-1].outcome or {}).get("decision")


#: 人工节点的「驳回」出口（engine/layout.py 的 approved / rejected）
REJECTED_HANDLE = "rejected"
_REJECTIONS = ("reject", "expired_reject")


def rejected_outputs(graph: Any) -> dict[str, set[str]]:
    """成果节点 → 从哪几个人工节点的「驳回」出口一路走到它（中间不经过别的人工节点：再遇到人工节点，结局由那一个人
    的决定说了算）。按图认，不按成果节点的标题猜。"""
    from app.apps.tree import node_map

    nodes = node_map(graph)
    edges = [e for e in (graph or {}).get("edges") or [] if isinstance(e, dict)]
    out: dict[str, set[str]] = {}
    for gate in (e for e in edges if e.get("sourceHandle") == REJECTED_HANDLE
                 and (nodes.get(str(e.get("source"))) or {}).get("type") == "human"):
        seen: set[str] = set()
        todo = [str(gate.get("target"))]
        while todo:
            nid = todo.pop()
            if nid in seen or nid not in nodes or nodes[nid]["type"] == "human":
                continue
            seen.add(nid)
            if nodes[nid]["type"] == "output":
                out.setdefault(nid, set()).add(str(gate["source"]))
            todo += [str(e.get("target")) for e in edges if str(e.get("source")) == nid]
    return out


def instance_state(run: Run, trail: _Trail) -> dict[str, str]:
    """实例给人看的状态 {code, text}：code 是界面画状态标记用的状态（运行的那几种，加 held），text 说这件事办到哪了、
    结论是什么，不只是运行状态（P1）。

    - 走完了：图里有几个成果节点（分支的几种结局，如「已通过」「已退回」）、恰好走到了其中一个的，写走到的那一个；只有
      一个成果节点的、几个成果节点并行都走到了的（「周报」「明细」一起出）写「已完成」——那不是哪一种结局。走到的是
      否定的那种结局——这次运行里人工节点驳回了、从它的「驳回」出口一路走到的成果节点（rejected_outputs）——code 记
      cancelled（同「未确认（被驳回）」，是人的决定，不画成成功），文字照旧写成果节点的标题；
    - 停着：有已发出、结果未知的动作，写「等待核实写入结果」；否则按在等的待办说（等待审批、等待确认发送……）；
    - 失败：停在一个被人驳回的待办上的——动作确认被驳回（「付款」未确认（被驳回））、审批设了驳回即终止、补充输入被
      驳回——是人的决定，不是故障，code 记 cancelled；核实为未生效、选了放弃的同理。外部系统拒绝之类照写失败。

    运行本身的状态不改（驳回动作确认在引擎里就是节点失败、运行结束）：这里只是把它说清楚。"""
    from app.apps.tree import node_map

    nodes = node_map(run.graph)

    def title(node_id: str) -> str:
        return (nodes.get(node_id) or {}).get("title") or node_id

    if run.status == "interrupted":
        if any(a.state in ("sent", "unknown") for a in trail.actions):
            return {"code": "waiting", "text": UNKNOWN_TEXT}
        pending = [t for t in trail.tasks if t.status == "pending"]
        if pending:
            return {"code": "waiting", "text": WAITING_TEXT.get(pending[-1].kind, STATUS_TEXT["interrupted"])}
        return {"code": "held", "text": HELD_TEXT}
    if run.status == "succeeded":
        outputs = {nid for nid, n in nodes.items() if n["type"] == "output"}
        reached = [nid for etype, nid in trail.events if etype == "node.finished" and nid in outputs]
        if len(outputs) > 1 and len(set(reached)) == 1:
            gates = rejected_outputs(run.graph).get(reached[0], set())
            negative = any(_decision(trail, gate)[1] in _REJECTIONS for gate in gates)
            return {"code": "cancelled" if negative else "succeeded", "text": title(reached[0])}
        return {"code": "succeeded", "text": STATUS_TEXT["succeeded"]}
    if run.status == "failed":
        failed = [nid for etype, nid in trail.events if etype == "node.failed"]
        if failed:
            node_id = failed[-1]
            kind, decision = _decision(trail, node_id)
            if kind == "action_confirm" and decision == "reject":
                return {"code": "cancelled", "text": f"「{title(node_id)}」未确认（被驳回）"}
            if decision in _REJECTIONS:
                return {"code": "cancelled", "text": f"「{title(node_id)}」被驳回"}
            record = next((a for a in reversed(trail.actions) if a.node_id == node_id), None)
            if record is not None and record.state == "failed":
                if (record.resolution or {}).get("outcome") == "abandon":
                    return {"code": "cancelled", "text": f"「{title(node_id)}」核实为未生效，已放弃发送"}
                return {"code": "failed", "text": f"「{title(node_id)}」写入没有生效"}
        return {"code": "failed", "text": STATUS_TEXT["failed"]}
    return {"code": run.status, "text": STATUS_TEXT.get(run.status, run.status)}


def instance_row(run: Run, pending: int = 0, state: dict[str, str] | None = None) -> dict[str, Any]:
    output = run.output if isinstance(run.output, dict) else {}
    issuance = output.get("_issuance") if isinstance(output.get("_issuance"), dict) else {}
    state = state or {"code": run.status, "text": STATUS_TEXT.get(run.status, run.status)}
    return {"run_id": run.id, "title": instance_title(run), "entry_id": (run.origin or {}).get("entry"),
            "status": run.status, "status_code": state["code"], "status_text": state["text"],
            "started_by": run.started_by,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "pending_tasks": pending, "tier": issuance.get("tier")}


async def instance_rows(session: AsyncSession, runs: list[Run]) -> list[dict[str, Any]]:
    """实例列表的那几行（列表、实例页的头）：待处理的待办数和给人看的状态（instance_state）一次取齐。"""
    counts = await pending_counts(session, [r.id for r in runs])
    trails = await _trails(session, runs)
    return [instance_row(r, counts.get(r.id, 0), instance_state(r, trails[r.id])) for r in runs]


async def list_instances(session: AsyncSession, app_id: str, *, mine: str | None, entry: str | None = None,
                         status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """应用的实例（入口、钩子发起的正式运行；按需视图的运行不算实例）。mine 给了只列这个人发起的。"""
    query = select(Run).where(Run.app_id == app_id, Run.origin_kind.in_(("app_entry", "hook")))
    if mine:
        query = query.where(Run.started_by == mine)
    if status:
        query = query.where(Run.status == status)
    rows = list((await session.execute(query.order_by(Run.created_at.desc()).limit(max(1, min(limit, 200)) * 2)))
                .scalars())
    if entry:
        rows = [r for r in rows if (r.origin or {}).get("entry") == entry]
    return await instance_rows(session, rows[:max(1, min(limit, 200))])


async def _instance_list(session: AsyncSession, ctx: _Page, props: dict[str, Any]) -> dict[str, Any]:
    from app.apps.access import at_least

    who = props.get("who") if props.get("who") in ("mine", "all") else "mine"
    if who == "all" and not at_least(ctx.role, "observe"):
        who = "mine"
    # 关登录、又没填署名时分不出谁是谁，「我发起的」就是全部（同待办箱，A4.6）
    mine = (ctx.principal.username or ctx.actor) if who == "mine" else None
    limit = props.get("limit") if isinstance(props.get("limit"), int) else 50
    return {"who": who, "entry": props.get("entry"),
            "items": [] if ctx.draft else await list_instances(session, ctx.app.id, mine=mine, entry=props.get("entry"),
                                                                limit=limit)}


async def _instance_header(session: AsyncSession, ctx: _Page) -> dict[str, Any]:
    run = ctx.instance
    if run is None:
        return {"state": "empty"}
    stamp = next((s.stamp for s in ctx.sources.values() if s.kind == "instance" and s.stamp), None)
    return {"state": "ok", **(await instance_rows(session, [run]))[0], "stamp": stamp}


#: 节点事件 → 进度里这一步的状态。人回复了（human.resolved）只说明这一步接着往下办：人工节点紧接着就结束，调用工具节点
#: 确认之后还要发送——以前把它记成「完成」，结果未知时进度上写「付款 完成」
_STEP_STATE = {"node.started": "running", "node.finished": "done", "node.failed": "failed", "node.skipped": "skipped",
               "human.requested": "waiting", "human.resolved": "running"}
_STEP_TEXT = {"running": "进行中", "done": "完成", "failed": "失败", "skipped": "已跳过", "waiting": "等待处理",
              "unknown": "结果未知，等待核实", "rejected": "被驳回"}
_DECISION_TEXT = {"approve": "已同意", "reject": "已驳回", "expired_reject": "超时驳回"}


async def _timeline(session: AsyncSession, ctx: _Page) -> dict[str, Any]:
    """实例进度：服务端摘要，按节点给一行（标题、状态、给人看的说法、时间），不下发原始事件。

    写入动作这一步按生效记录说（结果未知、核实为未生效）；有待办的步骤按待办说（在等什么、同意还是驳回）。"""
    from app.apps.tree import node_map

    run = ctx.instance
    if run is None:
        return {"state": "empty", "steps": []}
    titles = {k: v["title"] for k, v in node_map(run.graph).items()}
    rows = (await session.execute(select(RunEvent.type, RunEvent.node_id, RunEvent.ts).where(
        RunEvent.run_id == run.id, RunEvent.type.in_(tuple(_STEP_STATE))).order_by(RunEvent.seq))).all()
    trail = (await _trails(session, [run]))[run.id]
    steps: dict[str, dict[str, Any]] = {}
    for etype, node_id, ts in rows:
        if not node_id:
            continue
        step = steps.setdefault(node_id, {"node": node_id, "title": titles.get(node_id, node_id), "state": "running",
                                          "started_at": datetime.fromtimestamp(ts, timezone.utc).isoformat()})
        step["state"] = _STEP_STATE[etype]
        step["at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    for node_id, step in steps.items():
        text = None
        record = next((a for a in reversed(trail.actions) if a.node_id == node_id), None)
        kind, decision = _decision(trail, node_id)
        pending = [t for t in trail.tasks if t.node_id == node_id and t.status == "pending"]
        if record is not None and record.state in ("sent", "unknown"):
            step["state"] = "unknown"
        elif step["state"] == "waiting" and pending:
            text = WAITING_TEXT.get(pending[-1].kind)
        elif step["state"] == "failed" and kind == "action_confirm" and decision == "reject":
            step["state"], text = "rejected", "未确认（被驳回）"
        elif step["state"] in ("failed", "done") and decision in _REJECTIONS:
            # 驳回了：驳回即终止的（节点失败）和从「驳回」出口接着走的（节点完成）都是人的决定，不画成成功
            step["state"], text = "rejected", _DECISION_TEXT[decision]
        elif step["state"] == "failed" and record is not None and (record.resolution or {}).get("outcome") == "abandon":
            step["state"], text = "rejected", "核实为未生效，已放弃发送"
        elif step["state"] == "done" and kind == "approve" and decision in _DECISION_TEXT:
            text = _DECISION_TEXT[decision]
        step["text"] = text or _STEP_TEXT[step["state"]]
    state = instance_state(run, trail)
    return {"state": "ok", "status": run.status, "status_code": state["code"], "status_text": state["text"],
            "steps": list(steps.values())}


def _task_row(task: Any, instance: str | None = None) -> dict[str, Any]:
    """待办的摘要。instance 是所属实例的标题：同一个人工节点开出的待办标题一样，列表里靠它（和开出的时间）分开。"""
    return {"id": task.id, "run_id": task.run_id, "node_id": task.node_id, "kind": task.kind, "title": task.title,
            "status": task.status, "due_at": task.due_at.isoformat() if task.due_at else None,
            "policy": task.policy, "escalation_level": task.escalation_level,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            **({"instance_title": instance} if instance else {})}


async def _task_list(session: AsyncSession, ctx: _Page, props: dict[str, Any]) -> dict[str, Any]:
    from app.db.models_process import Task, TaskAssignment

    scope = props.get("scope") if props.get("scope") in ("mine", "instance") else "mine"
    query = select(Task).where(Task.app_id == ctx.app.id)
    if scope == "instance":
        if ctx.instance is None:
            return {"scope": scope, "items": []}
        query = query.where(Task.run_id == ctx.instance.id)
    else:
        query = query.where(Task.status == "pending")
        p = ctx.principal
        if p.mode != "off":
            mine = select(TaskAssignment.task_id).where(TaskAssignment.status == "pending").where(
                (TaskAssignment.principal_kind == "anyone")
                | ((TaskAssignment.principal_kind == "user") & (TaskAssignment.principal == (p.username or "")))
                | ((TaskAssignment.principal_kind == "group") & TaskAssignment.principal.in_(list(p.groups) or [""])))
            query = query.where(Task.id.in_(mine))
    rows = list((await session.execute(query.order_by(Task.created_at.desc()).limit(100))).scalars())
    runs = {run_id: str((origin or {}).get("title") or name or "") for run_id, origin, name in (await session.execute(
        select(Run.id, Run.origin, Run.workflow_name).where(Run.id.in_({t.run_id for t in rows})))).all()}
    return {"scope": scope, "items": [_task_row(t, runs.get(t.run_id)) for t in rows]}


def _assignment_row(a: Any) -> dict[str, Any]:
    return {"id": a.id, "principal_kind": a.principal_kind, "principal": a.principal, "source": a.source,
            "status": a.status, "responded_by": a.responded_by,
            "responded_at": _aware(a.responded_at).isoformat() if a.responded_at else None,
            "decision": (a.response or {}).get("decision") if isinstance(a.response, dict) else None,
            "verified": bool(a.verified)}


def _action_row(record: Any, *, user: bool) -> dict[str, Any]:
    """一条生效记录给人看的样子。error 只放确定没生效的原因（失败原因）；结果未知、事后核实的经过放 note：核实为已生效
    之后还挂着第一次发送时的「ReadTimeout」当失败原因，看的人会以为钱没付出去。"""
    resolution = record.resolution or {}
    how = resolution.get("how")
    who = "按幂等键查询" if how == "lookup" else f"「{resolution.get('by')}」" if resolution.get("by") else "人工"
    out: dict[str, Any] = {"id": record.id, "state": record.state, "environment": record.environment,
                           "attempt": record.attempt, "payload_hash": (record.payload_hash or "")[:8]}
    first = f"发送时没有拿到确定的结果（{record.error}）" if record.error else "发送时没有拿到确定的结果"
    if record.state in ("sent", "unknown"):
        out["note"] = f"{first}，等待核实：在外部系统里可能已经生效，不会自动重发"
    elif record.state == "confirmed" and how:
        external = resolution.get("external_id")
        out["note"] = f"{first}，经{who}核实为已生效" + (f"（外部单号 {external}）" if external else "")
    elif record.state == "failed" and resolution.get("outcome") == "resend":
        out["note"] = f"{first}，经{who}核实为未生效，已重新发送"
    elif record.state == "failed" and resolution.get("outcome") == "abandon":
        out["note"] = f"{first}，经{who}核实为未生效，已放弃发送"
    elif record.state == "failed":
        out["error"] = record.error or "没有生效"
    # 回执是外部系统的原文：user 角色不给（P4），给哈希和状态就够核对
    if not user:
        out["receipt"] = record.receipt
    return out


async def _process_data(session: AsyncSession, ctx: _Page, ctype: Any, kind: str, node_id: str) -> dict[str, Any]:
    """待办（task_form、signoffs）、动作（action_status）。

    待办这里就判定读者是不是这条待办的处理人（process.tasks.handles）：是的，界面按 id 取完整的待办画待办卡（能处理的
    给表单）；不是的（发起人、旁观的人）只看这一步的状态——派给了谁、会签进度、时限——不去取待办（取了也是 404）。

    同一页上另有绑同一条待办的会签组件（signoffs）时，待办表单不再画会签进度（signoffs_on_page）：同一页上「会签进度
    0/2」不写两遍。人名给显示名（people：用户名 → 显示名），界面写显示名、悬停给用户名。"""
    from app.apps.tree import components
    from app.auth.directory import display_names
    from app.db.models_process import ActionRecord, Task, TaskAssignment
    from app.process.tasks import handles, my_response, people_of, principal_with_actor, progress

    run = ctx.instance
    if run is None:
        return {"state": "empty"}
    if kind == "task":
        task = (await session.execute(select(Task).where(Task.run_id == run.id, Task.node_id == node_id)
                                      .order_by(Task.created_at.desc()))).scalars().first()
        if task is None:
            return {"state": "empty", "node": node_id}
        rows = list((await session.execute(select(TaskAssignment).where(TaskAssignment.task_id == task.id)
                                           .order_by(TaskAssignment.created_at))).scalars())
        reader = principal_with_actor(ctx.principal, ctx.actor)
        resolved_at = _aware(task.resolved_at).isoformat() if task.resolved_at else None
        signoffs = ctype == "task_form" and any(
            c.get("type") == "signoffs" and isinstance(c.get("bind"), dict) and c["bind"].get("task") == node_id
            for c, _, _ in components({"pages": [ctx.page]}))
        return {"state": "ok", "task": {**_task_row(task, instance_title(run)), "form": task.form,
                                        "resolved_by": task.resolved_by, "resolved_at": resolved_at,
                                        "outcome": {"decision": (task.outcome or {}).get("decision")}
                                        if task.outcome else None},
                "task_ui": (ctx.spec.get("task_ui") or {}).get(node_id) or {},
                "handler": handles(rows, reader), "assignments": [_assignment_row(a) for a in rows],
                "progress": progress(task, rows), "my_response": my_response(rows, reader),
                "people": await display_names(session, people_of(task, rows)),
                **({"signoffs_on_page": True} if signoffs else {})}
    records = (await session.execute(select(ActionRecord).where(ActionRecord.run_id == run.id,
                                                                ActionRecord.node_id == node_id)
                                     .order_by(ActionRecord.created_at, ActionRecord.attempt))).scalars()
    user = ctx.principal.mode != "off" and ctx.principal.role == "user"
    return {"state": "ok", "node": node_id, "records": [_action_row(r, user=user) for r in records]}


def instance_page(spec: dict[str, Any]) -> str | None:
    """实例详情用哪一页：叫 instance 的页；没有的话，第一张放着实例组件或绑定实例视图的页。"""
    from app.apps.tree import components

    if page_of(spec, "instance") is not None:
        return "instance"
    instance_views = {vid for vid, v in (spec.get("views") or {}).items()
                      if ((v or {}).get("source") or {}).get("kind") == "instance"}
    for page in spec.get("pages") or []:
        for comp, _, _ in components({"pages": [page]}):
            bind = comp.get("bind") if isinstance(comp.get("bind"), dict) else {}
            if comp.get("type") in ("instance_header", "instance_timeline", "task_form", "signoffs", "action_status") \
                    or bind.get("view") in instance_views:
                return page.get("id")
    return None
