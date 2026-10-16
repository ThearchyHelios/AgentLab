"""按口径正式出具：路径一、二a（docs/design/next-round/02-口径账本.md 做法 5，拍板 L1、L5）。

问数据的答案整题等价于一份已发布口径（ledger/labels.whole_of：主报告里每个数都来自同一口径的同一版、参数一致）时，可以
按那份口径发起一次正式运行——同一个问题，从此有了一份按已发布版本出具的、完整走过门禁的答案。

**参数只从比对工件里取。** 发起用的输入 = 那一版入口字段的缺省值 + 比对时绑定出来的参数（whole.binding），再过 apps/forms
同一套表单校验；不收客户端传的参数，前端只能选「按哪份口径」——不然谁都能拿一次问数据去替一份正式口径「签字」任意输入。
绑定的值是这次运行查到过的遮罩值的，不代填出具。

**经 launch_run 发起**（正式运行的全部门禁：已发布未退役、升版处置、子工作流固定版本、工具在不在），来源 ledger_issue
（服务端专用，engine/origin），origin 记 source_run、conversation_id、turn_id、binding_hash、口径和怎么认的、是不是自动的；
按 expect_hash 钉住那一版的 graph_hash（比对时考虑的就是这张图）。出具档位照正式运行原样走。

**能不能出具**（plan_issue，一键和自动同一套，不满足的照实写原因）：
- 这次运行是比对过的探索运行，整题等价于点名的这份口径（工作流、版本都对）；
- 那一版已发布、未退役；
- 图里没有人工节点、不会写外部系统（apps/backfill.graph_side_effect，和按新口径补算同一个判断）：替人重跑一次的正式
  运行不该替人去审批、往外部系统写；
- 补了缺省值之后表单校验通过（缺入口字段的照实说缺哪个）；
- 权限（拍板 L5）：搭建者及以上（关登录时同）都可以；使用者只能按他有权使用的应用固定着的这一版出具（复用应用授权，
  apps/access），别的写「请搭建者出具」；都要读得到这次运行；
- 当天没超上限（设置 ledger.issue_daily_limit，自动的、一键的合计）。
**自动出具**（auto_issue，拍板 L1）另要：设置里开着、认法是独立等价（how=independent：Agent 自己写的 SQL 被证明等价；
「助手选用」的只给一键——选哪份口径是助手凭问法判断的，由人点一下代替证明）、版本是受管级别。只在运行一封存就比完的
那一次触发（ledger/match）。不满足的写进 actions 的 auto_reason。

**同一运行、同一口径、同一组参数只发起一次**：按 input_key（apps/resolve，补算用的同一个指纹）认，已经发起过、没失败的
直接交回那一次。进程内按（运行、工作流、版本）串行，自动和一键同时到也只发一次（单进程部署，同 apps/backfill 的锁）。

结果挂回答案：GET /api/runs/{id}/ledger 的 issued 列出 origin.source_run 等于这次运行的正式运行，档位文字用 TIER_TEXT。
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models import Run, Workflow

logger = logging.getLogger(__name__)

ORIGIN = "ledger_issue"
#: 出具被拒的机器码（接口回 409 / 403 / 429 时带它）
REFUSED = "ledger_issue_refused"
FORBIDDEN = "ledger_issue_forbidden"
DAILY_LIMIT = "ledger_issue_daily_limit"
#: 算「同一次」时看哪几种状态（失败、取消的可以再发）
_LIVE = ("queued", "running", "interrupted", "succeeded")

#: 不能出具的原因
NOT_MATCHED = "这次运行还没有比对口径，比对完才能按口径出具"
NOT_ELIGIBLE = "这次运行不能按口径出具：{why}"
NOT_WHOLE = "整题没有等价于这份口径：{why}"
OTHER_CALIBER = "整题等价的是另一份口径，不是「{name}」v{version}"
MASKED_BINDING = "参数里有这次运行查到过的遮罩值，不代填出具"
GONE = "口径所在的工作流不存在，可能已被删除"
VERSION_GONE = "「{name}」没有 v{version} 这个版本"
RETIRED = "「{name}」v{version} 已退役，不再接受新的正式运行"
UNPUBLISHED = "「{name}」v{version} 没有发布过，正式运行只能从已发布的版本发起"
SIDE_EFFECT = "「{name}」v{version}{reason}，按口径出具不代人做这些事"
INPUTS = "按这次的参数补上缺省值后，入口字段还不齐：{problems}"
NO_RUN_ACCESS = "没有权限查看这次运行"
ASK_BUILDER = "这份口径没有被你能使用的应用固定，不能由你按口径出具，请搭建者出具"
OVER_LIMIT = "今天的按口径出具已达到上限（{limit} 次），请明天再试，或请管理员调整上限"
#: 不自动出具的原因（能一键出具时写进 actions 的 auto_reason）
AUTO_OFF = "设置里关了自动按口径出具"
AUTO_COMPOSED = "数来自助手选用的口径，只给一键出具：选用哪份口径是助手按问法判断的，要由人确认"
AUTO_LEVEL = "这一版是已发布级别，自动出具只限受管级别的口径"
AUTO_RETRO = "这次比对是事后按现在的口径回看的，不自动出具"


class IssueRefused(Exception):
    def __init__(self, status_code: int, message: str, code: str = REFUSED) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


@dataclass
class Plan:
    """按一份口径出具的打算：能不能（reason 为空就能）、发起用的输入、为什么不自动（auto_reason，能自动的为空）。"""

    run_id: str
    workflow_id: str
    version: int
    caliber: str
    how: str | None = None
    level: str | None = None
    workflow_name: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    binding: dict[str, Any] = field(default_factory=dict)
    expect_hash: str | None = None
    match_artifact: str | None = None
    #: 不能出具的原因、HTTP 状态码、机器码
    reason: str | None = None
    status: int = 409
    code: str = REFUSED
    auto_reason: str | None = None
    #: 使用者靠哪个应用的授权出具（apps/access）：记进 origin
    granted_by_app: str | None = None
    #: 已经发起过的同一次（没失败的）
    existing: str | None = None

    @property
    def available(self) -> bool:
        return self.reason is None

    def refuse(self, message: str, status: int = 409, code: str = REFUSED) -> "Plan":
        if self.reason is None:
            self.reason, self.status, self.code = message, status, code
        return self

    def action(self) -> dict[str, Any]:
        """交给界面的那一项动作（GET 账本的 actions）。"""
        from app.ledger.labels import LEVEL_TEXT

        out: dict[str, Any] = {"kind": "issue", "workflow_id": self.workflow_id, "version": self.version,
                               "caliber": self.caliber, "workflow_name": self.workflow_name, "how": self.how,
                               "level": self.level, "level_text": LEVEL_TEXT.get(str(self.level or ""), ""),
                               "available": self.available, "auto": self.available and self.auto_reason is None}
        if self.reason:
            out["reason"] = self.reason
        elif self.auto_reason:
            out["auto_reason"] = self.auto_reason
        if self.existing:
            out["issued_run"] = self.existing
        return out


def _local_midnight() -> datetime:
    return datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def issued_today(session: Any) -> int:
    return int(await session.scalar(select(func.count(Run.id)).where(
        Run.origin_kind == ORIGIN, Run.created_at >= _local_midnight())) or 0)


async def issued_runs(session: Any, run_id: str) -> list[Run]:
    """这次运行按口径出具过的正式运行（origin.source_run 是它），新的在前。origin_kind 有索引，先按它筛再看 origin。"""
    rows = (await session.execute(select(Run).where(Run.origin_kind == ORIGIN).order_by(Run.created_at.desc()))).scalars()
    return [r for r in rows if isinstance(r.origin, dict) and r.origin.get("source_run") == run_id]


def issued_view(run: Run) -> dict[str, Any]:
    """GET 账本 issued 里的一项：哪一次正式运行、状态、出具档位（TIER_TEXT，完整出具 / 降档出具 / 不予出具）。"""
    from app.apps.resolve import TIER_TEXT

    output = run.output if isinstance(run.output, dict) else {}
    issuance = output.get("_issuance") if isinstance(output.get("_issuance"), dict) else {}
    tier = issuance.get("tier")
    origin = run.origin if isinstance(run.origin, dict) else {}
    return {"run_id": run.id, "status": run.status, "end_reason": getattr(run, "end_reason", None),
            "workflow_id": run.workflow_id, "version": run.version,
            "tier": tier if tier in TIER_TEXT else None, "tier_text": TIER_TEXT.get(str(tier)),
            "auto": bool(origin.get("auto")),
            "created_at": run.created_at.isoformat() if run.created_at else None}


async def _starter(session: Any, run: Run) -> Any:
    """自动出具时按谁的权限判（拍板 L5）：这次运行的发起人。关登录时同管理员；账号不在了的按使用者算（宁可少出具）。"""
    from app.api.deps import Principal
    from app.auth import current_mode
    from app.auth.sessions import principal_of
    from app.db.models import User

    mode = current_mode()
    if mode == "off":
        return Principal(username=run.started_by, role="admin", verified=False, mode="off", via="off")
    user = (await session.execute(select(User).where(User.username == run.started_by))).scalar_one_or_none() \
        if run.started_by else None
    if user is None:
        return Principal(username=run.started_by, role="user", verified=False, mode=mode)
    return await principal_of(session, user, mode)


async def _granting_app(session: Any, principal: Any, workflow_id: str, version: int) -> str | None:
    """使用者能按这一版出具，靠的是哪个应用的授权：固定着这一版、他至少能使用的应用（apps/access）。没有为 None。"""
    from app.apps.access import app_role, at_least
    from app.apps.versions import pinning_apps
    from app.db.models_apps import App

    for item in await pinning_apps(session, workflow_id, version):
        app = await session.get(App, item["app_id"])
        if app is not None and at_least(await app_role(session, principal, app), "use"):
            return app.id
    return None


async def plan_issue(session: Any, run: Run | None, content: dict[str, Any] | None, workflow_id: str, version: int, *,
                     principal: Any, hide: Any = None, artifact: str | None = None, retro: bool = False) -> Plan:
    """按 workflow_id@version 出具这次运行的打算（见模块说明）。content 是这次运行最新的比对工件内容（读不出为 None），
    hide 判一个绑定值是不是这次运行查到过的遮罩值。只读，不发起。"""
    from app.api.deps import can_read_run
    from app.api.settings import ledger_settings
    from app.apps import versions as app_versions
    from app.apps.backfill import graph_side_effect
    from app.apps.forms import input_schema, validate_form
    from app.apps.resolve import input_key
    from app.ledger import match as ledger_match
    from app.ledger.derive import entry_fields
    from app.ledger.labels import caliber_key

    plan = Plan(run_id=run.id if run else "", workflow_id=workflow_id, version=int(version),
                caliber=caliber_key(workflow_id, int(version)), match_artifact=artifact)
    if run is None:
        return plan.refuse(NOT_ELIGIBLE.format(why=ledger_match.NOT_FOUND), 404)
    if principal.mode != "off" and not await can_read_run(principal, run):
        return plan.refuse(NO_RUN_ACCESS, 403, FORBIDDEN)
    if (why := ledger_match.ineligible(run)) is not None:
        return plan.refuse(NOT_ELIGIBLE.format(why=why))
    if content is None:
        return plan.refuse(NOT_MATCHED)
    whole = content.get("whole") or {}
    workflow = await session.get(Workflow, workflow_id)
    plan.workflow_name = workflow.name if workflow is not None else ""
    if not whole.get("equivalent"):
        return plan.refuse(NOT_WHOLE.format(why=whole.get("reason") or "没有比对结论"))
    if (whole.get("workflow_id"), whole.get("version")) != (workflow_id, int(version)):
        return plan.refuse(OTHER_CALIBER.format(name=plan.workflow_name or workflow_id, version=version))
    plan.how, plan.level = whole.get("how"), whole.get("level")
    plan.binding = dict(whole.get("binding") or {})
    if hide is not None and any(hide(v) for v in plan.binding.values()):
        return plan.refuse(MASKED_BINDING)
    if workflow is None:
        return plan.refuse(GONE, 404)
    row = await app_versions.version_row(session, workflow_id, int(version))
    if row is None:
        return plan.refuse(VERSION_GONE.format(name=workflow.name, version=version), 404)
    if row.retired_at is not None:
        return plan.refuse(RETIRED.format(name=workflow.name, version=version))
    if not app_versions.is_published(row, current=workflow.published_version):
        return plan.refuse(UNPUBLISHED.format(name=workflow.name, version=version))
    plan.level = row.level or plan.level
    plan.expect_hash = row.graph_hash
    if reason := await graph_side_effect(session, row.graph, level=row.level):
        return plan.refuse(SIDE_EFFECT.format(name=workflow.name, version=version, reason=reason))
    fields = list(entry_fields(row.graph or {}))
    values = {str(f["name"]): f.get("default") for f in fields if f.get("default") not in (None, "")}
    values.update({k: v for k, v in plan.binding.items() if v not in (None, "")})
    plan.inputs = values
    if errors := validate_form(input_schema(fields), values):
        return plan.refuse(INPUTS.format(problems="；".join(e["message"] for e in errors)))
    if principal.mode != "off" and not principal.at_least("builder"):
        plan.granted_by_app = await _granting_app(session, principal, workflow_id, int(version))
        if plan.granted_by_app is None:
            return plan.refuse(ASK_BUILDER, 403, FORBIDDEN)
    key = input_key(values)
    plan.existing = next((r.id for r in await issued_runs(session, run.id)
                          if (r.workflow_id, r.version) == (workflow_id, int(version)) and r.status in _LIVE
                          and input_key(r.input) == key), None)
    settings = await ledger_settings(session)
    if plan.existing is None and await issued_today(session) >= int(settings["issue_daily_limit"]):
        return plan.refuse(OVER_LIMIT.format(limit=settings["issue_daily_limit"]), 429, DAILY_LIMIT)
    plan.auto_reason = (AUTO_OFF if not settings["auto_issue"] else AUTO_COMPOSED if plan.how != "independent"
                        else AUTO_LEVEL if plan.level != "governed" else AUTO_RETRO if retro else None)
    return plan


async def _conversation_of(session: Any, run: Run) -> dict[str, str]:
    """这次运行出自哪段对话的哪一轮：运行来源里封存的（origin_ref，第 2 波）；老运行退回 conversation_turns.run_id。"""
    from app.db.models import ConversationTurn

    origin = run.origin if isinstance(run.origin, dict) else {}
    if origin.get("conversation_id") and origin.get("turn_id"):
        return {"conversation_id": str(origin["conversation_id"]), "turn_id": str(origin["turn_id"])}
    turn = (await session.execute(select(ConversationTurn).where(ConversationTurn.run_id == run.id)
                                  .order_by(ConversationTurn.created_at.desc()))).scalars().first()
    return {"conversation_id": turn.conversation_id, "turn_id": turn.id} if turn is not None else {}


#: 同一（运行、工作流、版本）的出具串行：{事件循环: {键: 锁}}（asyncio.Lock 不能跨循环用，测试每个用例一个循环）
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, str, int], asyncio.Lock]]" = \
    weakref.WeakKeyDictionary()


def _lock(key: tuple[str, str, int]) -> asyncio.Lock:
    group = _locks.setdefault(asyncio.get_running_loop(), {})
    return group.setdefault(key, asyncio.Lock())


async def _hider(run_id: str) -> Any:
    from app.api.evidence import _sealed
    from app.ledger.view import _hider as hider_of

    return (await hider_of(await _sealed(run_id))).hide


async def issue(run_id: str, workflow_id: str, version: int, *, principal: Any, auto: bool = False) -> tuple[Run, bool]:
    """按 workflow_id@version 出具这次运行：(正式运行, 是不是这一次新发起的)。不能出具抛 IssueRefused；已经发起过同一次的
    交回那一次（第二项为 False）。auto：自动出具（自动的条件也要满足）。"""
    from app.engine.launch import LaunchRefused, launch_run
    from app.ledger import store

    async with _lock((run_id, workflow_id, int(version))):
        recorded = await store.latest(run_id)
        hide = await _hider(run_id)
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            plan = await plan_issue(session, run, recorded.content if recorded else None, workflow_id, version,
                                    principal=principal, hide=hide, artifact=recorded.artifact if recorded else None,
                                    retro=bool(recorded and recorded.retro))
            if plan.reason is not None:
                raise IssueRefused(plan.status, plan.reason, plan.code)
            if plan.existing is not None:
                return await session.get(Run, plan.existing), False
            if auto and plan.auto_reason is not None:
                raise IssueRefused(409, plan.auto_reason)
            from app.apps.resolve import input_key

            origin = {"source_run": run_id, **await _conversation_of(session, run), "caliber": plan.caliber,
                      "how": plan.how, "binding_hash": input_key(plan.binding), "match": plan.match_artifact,
                      "auto": auto, **({"granted_by_app": plan.granted_by_app} if plan.granted_by_app else {})}
            actor = run.started_by if auto else principal.username
            try:
                launched = await launch_run(session, workflow_id=workflow_id, run_class="formal", version=int(version),
                                            input_payload=plan.inputs, actor=actor, origin_kind=ORIGIN, origin=origin,
                                            expect_hash=plan.expect_hash)
            except LaunchRefused as e:
                raise IssueRefused(e.status_code, e.message, e.code or REFUSED) from e
            return launched, True


async def plans_for(run_id: str, content: dict[str, Any] | None, *, principal: Any, hide: Any, artifact: str | None,
                    retro: bool) -> list[Plan]:
    """GET 账本的 actions 里出具那几项：整题等价的那一份口径能不能出具、能不能自动（整题不等价的没有这一项）。"""
    whole = (content or {}).get("whole") or {}
    if not whole.get("equivalent") or not whole.get("workflow_id") or whole.get("version") is None:
        return []
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        return [await plan_issue(session, run, content, str(whole["workflow_id"]), int(whole["version"]),
                                 principal=principal, hide=hide, artifact=artifact, retro=retro)]


async def auto_issue(run_id: str) -> str | None:
    """比对完、整题等价时看能不能自动出具（拍板 L1），能就发起；返回新发起的正式运行 id。不能的、出错的只记日志：自动出具
    是辅助步骤，不影响运行本身和答案（照实说为什么没自动出具在 GET 账本的 actions 里）。"""
    from app.ledger import store

    try:
        recorded = await store.latest(run_id)
        whole = ((recorded.content if recorded else None) or {}).get("whole") or {}
        if not whole.get("equivalent"):
            return None
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return None
            principal = await _starter(session, run)
        launched, fresh = await issue(run_id, str(whole["workflow_id"]), int(whole["version"]), principal=principal,
                                      auto=True)
        return launched.id if fresh else None
    except IssueRefused as e:
        logger.info("运行 %s 没有自动按口径出具：%s", run_id, e.message)
        return None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 辅助步骤
        logger.exception("运行 %s 自动按口径出具出错", run_id)
        return None


__all__ = ["DAILY_LIMIT", "FORBIDDEN", "ORIGIN", "REFUSED", "IssueRefused", "Plan", "auto_issue", "issue",
           "issued_runs", "issued_today", "issued_view", "plan_issue", "plans_for"]
