"""待办服务（附录 C，BE-2 提供）。

一次中断对应一条 Approval（给引擎）和一条 Task（给人）。所有人的决定都经 respond：按 policy 汇总，凑齐后在同一个
事务里用条件 UPDATE 把 Task 从 pending 改成 resolving，抢到的请求负责调 run_manager.resume。引擎和重放都不改。

- 种类按中断内容的 kind 认（人工节点按它的 mode），不看 Approval.mode：工具审批的中断里没有 mode，Approval.mode 一律是 approve。
- 派给谁：人工节点（和写入动作的调用工具节点）的 assignee。岗位按应用版本的 bindings 展开到人或组；派给组时在打开这一刻
  展开成当时的成员（快照，写审计）；没有应用绑定的运行（画布、定时任务）派给「任何人」，行为和以前一样。
- 会签只用于批准 / 驳回这一类：all 一票否决，quorum(m) 凑够 m 票通过、驳回超过 n − m 票否决。升级加派的那一份
  （source=escalation）一人说了算：升级就是让上级拍板，不是再多一个要等的人。
- 关登录时谁都能回复（「都算」），回复人记自报的署名。开了登录：派给人的认用户名，派给组的认组，派给「任何人」的要
  builder 以上（这类待办来自画布、定时任务，是给搭建者看的），等外部系统的只有管理员能代为处理。
- tick：提醒、升级（加派一份，escalation_level+1，只升一次）、超时驳回。永不自动批准。
- 蓝图的试运行（origin_kind=trial，docs/design/normal-user-apps.md N4）：每一步都派给发起试运行的人自己，按真实设置显示
  时限，但调度不对它们做提醒、升级、超时驳回——试运行不该把待办推到真实的处理人、上级那里。
- 个人应用提交审核（kind=review，N7）：没有 Approval、不恢复运行，挂在个人应用最近一次成功的刷新上；派给选定的搭建者
  或组。在待办里只能退回，同意要在共享副本上走发布单，发布完自动记为已发布（blueprints/personal.py）。
- 口径召回（kind=recall，docs/design/next-round/02-口径账本.md 做法 7）：没有 Approval、不恢复运行。口径升版后数真变了的
  挂在召回运行上，派给答案的主人（开登录时每人一条，关登录时任何人）；动作「按新版重新出具」（走按口径出具同一套判法）
  和「知道了」。答案依据的目录、表结构变了的另两种只有「知道了」。开、收尾、内容都在 app/ledger/recall.py。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.directory import display_names
from app.core.events import EventType
from app.db.base import SessionLocal, utcnow
from app.db.models import (
    App, Approval, AppVersion, AuditEvent, Group, GroupMember, Run, Task, TaskAssignment, User,
)
from app.db.portable import json_text
from app.engine.lint_process import normalize_policy, parse_duration
from app.process.events import record

logger = logging.getLogger(__name__)

#: 中断内容的 kind → 待办种类。人工节点（human_node）按 mode，派给外部系统的是 signal
_KIND_OF = {"tool_approval": "tool", "code_approval": "code", "dual_check": "dual_check",
            "action_confirm": "action_confirm"}
#: 结论只有批准 / 驳回两种、按会签策略汇总的种类。补填、改稿、双盲裁决、等外部系统只认第一份回复
VOTING = ("approve", "tool", "code", "action_confirm")
#: 核实待办的三种结论（同 actions.reconcile 的 outcome）
RECONCILE_OUTCOMES = ("confirmed", "not_applied_resend", "not_applied_abandon")
#: 超时驳回交给节点的那句备注
EXPIRED_NOTE = "超过时限无人处理，系统已自动驳回"
_TERMINAL_RUN = ("succeeded", "failed", "cancelled")
_STATUS_TEXT = {"resolving": "正在处理", "resolved": "已处理", "cancelled": "已关闭（所属的运行已结束）",
                "expired": "已过期"}


class TaskRefused(Exception):
    """回复不被接受：不是派给你的、已经处理过了、哈希对不上……status_code 照 HTTP 语义（403 / 409 / 422）。"""

    def __init__(self, message: str, *, status_code: int = 409, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class FormInvalid(TaskRefused):
    """填写的值没有通过表单校验（422）。errors 的形状同 agentlab.form/1：{path, code, message}。"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        first = errors[0].get("message") if errors else ""
        super().__init__(f"表单没有通过校验：{first}" if first else "表单没有通过校验", status_code=422,
                         code="form_invalid")
        self.errors = errors


@dataclass(frozen=True)
class RawResponse:
    """老接口（/api/approvals/{id}/decide、/api/runs/{id}/resume）带来的原样回复：「始终允许」、改过的代码、双盲裁决的
    选择都在里面，照原样交给节点。只当一票，结论按 read_decision 认。"""

    response: Any


def kind_of(payload: dict[str, Any]) -> str:
    kind = payload.get("kind")
    if kind == "human_node":
        if (payload.get("assignee") or {}).get("kind") == "hook":
            return "signal"
        mode = payload.get("mode") or "approve"
        return mode if mode in ("approve", "input", "edit") else "approve"
    return _KIND_OF.get(str(kind), "approve")


# --------------------------------------------------------------------------
# 打开
# --------------------------------------------------------------------------


async def open_task(session: AsyncSession, approval: Any) -> Any:
    """为一条新的 Approval 建 Task 和派给谁（按人工节点配置 + 应用版本的岗位绑定）。runner._handle_updates 调。

    和 Approval 同一个事务，调用方提交；task.opened 和通知在提交之后由 announce 发。同一条 Approval 已经有待办的直接返回。"""
    existing = await session.scalar(select(Task).where(Task.approval_id == approval.id))
    if existing is not None:
        return existing
    payload = approval.payload or {}
    run = await session.get(Run, approval.run_id)
    kind = kind_of(payload)
    policy = (normalize_policy(payload.get("policy")) or {"kind": "any"}) if kind in VOTING else {"kind": "any"}
    now = utcnow()
    due = parse_duration(payload.get("due"))
    timeout = payload.get("on_timeout") if isinstance(payload.get("on_timeout"), dict) else None
    due_at = now + due if due else None
    task = Task(
        approval_id=approval.id, run_id=approval.run_id, node_id=approval.node_id or None,
        app_id=run.app_id if run else None, app_version=run.app_version if run else None,
        kind=kind, title=str(payload.get("title") or approval.title or "待处理")[:300],
        form=payload.get("form") if isinstance(payload.get("form"), dict) else None,
        policy=policy, due_at=due_at,
        remind_at=due_at if timeout and timeout.get("action") == "remind" else None,
        on_timeout=timeout if due_at else None,
        correlation_key=(str(payload.get("correlation_key") or "")[:200] or None) if kind == "signal" else None,
    )
    session.add(task)
    await session.flush()
    await _assign(session, task, run, payload.get("assignee"))
    return task


async def open_reconcile(session: AsyncSession, action: Any, *, title: str) -> Task:
    """结果未知、没有登记查询能力的动作：开一条核实待办，派给应用的 reconciler 岗位（没有就派给任何人）。
    核实待办没有 Approval，按 correlation_key = action:<id> 找回动作。"""
    run = await session.get(Run, action.run_id)
    task = Task(run_id=action.run_id, node_id=action.node_id, app_id=run.app_id if run else None,
                app_version=run.app_version if run else None, kind="reconcile", title=title[:300],
                policy={"kind": "any"}, correlation_key=f"action:{action.id}")
    session.add(task)
    await session.flush()
    await _assign(session, task, run, {"kind": "slot", "slot": "reconciler"})
    return task


#: 个人应用提交审核的待办（docs/design/normal-user-apps.md N7）。没有 Approval，按 correlation_key = blueprint:<蓝图 id>
#: 找回提交审核的蓝图
REVIEW_KIND = "review"


async def open_review(session: AsyncSession, *, run: Run, app_id: str, blueprint_id: str, title: str,
                      to: list[tuple[str, str]]) -> Task:
    """个人应用提交审核：开一条 review 待办，派给选定的搭建者或组（组在这一刻展开成成员，同改派），来源记 review。

    待办的 run_id 不能为空（带外键，开待办的事件、通知都按运行走）：挂在个人应用最近一次成功的刷新上，只是外键挂靠——
    个人应用的刷新只有拥有者读得到（auth/access.private_owner），处理人（搭建者、管理员也一样）读不到这次运行、打不开这个
    个人应用；他要看结果，取共享副本（blueprints/personal.copy_for_review）在副本上试运行。这次运行已经结束，不会被恢复：
    回复走 _respond_review，不调 run_manager.resume。调用方提交，提交后 announce。"""
    task = Task(run_id=run.id, node_id=None, app_id=app_id, app_version=None, kind=REVIEW_KIND, title=title[:300],
                policy={"kind": "any"}, correlation_key=f"blueprint:{blueprint_id}")
    session.add(task)
    await session.flush()
    targets: list[tuple[str, str, str]] = []
    for kind, name in dict.fromkeys(to):
        targets += await _expand(session, task, kind, name, REVIEW_KIND)
    for kind, name, source in dict.fromkeys(targets):
        session.add(TaskAssignment(task_id=task.id, principal_kind=kind, principal=name, source=source))
    await session.flush()
    return task


#: 口径召回的待办（app/ledger/recall.py）。没有 Approval，按 correlation_key（来由:运行[:附加]:派给的种类:名字）认同一条
RECALL_KIND = "recall"
#: recall 待办的两种决定：按新版重新出具、知道了（目录、表结构那两种只有「知道了」）
RECALL_DECISIONS = ("reissue", "ack")


async def open_recall(session: AsyncSession, *, run: Run, title: str, owner: tuple[str, str],
                      correlation_key: str) -> Task:
    """开一条 recall 待办（挂在 run 上），派给 owner：人、组（在这一刻展开成成员，同改派）、任何人；来源记 recall。调用方
    提交，提交后 announce。"""
    task = Task(run_id=run.id, node_id=None, app_id=None, app_version=None, kind=RECALL_KIND, title=title[:300],
                policy={"kind": "any"}, correlation_key=correlation_key[:200])
    session.add(task)
    await session.flush()
    kind, name = owner
    targets = await _expand(session, task, kind, name, RECALL_KIND) if kind in ("user", "group") and name \
        else [("anyone", "", RECALL_KIND)]
    for k, n, source in dict.fromkeys(targets):
        session.add(TaskAssignment(task_id=task.id, principal_kind=k, principal=n, source=source))
    await session.flush()
    return task


async def review_handler(session: AsyncSession, task: Task, principal: Any) -> bool:
    """这个人是不是这条审核待办的处理人（派给他或他的组、没撤回，同 handles）：只有他们能取个人蓝图的共享副本。"""
    return handles((await _assignments(session, [task.id])).get(task.id, []), principal)


async def close_review(session: AsyncSession, blueprint_id: str, principal: Any, outcome: dict[str, Any], *,
                       require_handler: bool = False) -> str | None:
    """审核有了结果（副本发布了、或者退回了）：把这个蓝图还在等的审核待办收掉（条件 UPDATE，和别人的回复抢同一行），
    结论写进 outcome。require_handler 为真时只有处理人能收（退回），不是派给他的抛 403。返回待办 id，没有在等的返回
    None。调用方提交。"""
    task = await session.scalar(select(Task).where(Task.correlation_key == f"blueprint:{blueprint_id}",
                                                   Task.kind == REVIEW_KIND, Task.status == "pending"))
    if task is None:
        return None
    rows = (await _assignments(session, [task.id])).get(task.id, [])
    mine = next((a for a in rows if principal is not None and _matches(a, principal)), None)
    if require_handler and mine is None:
        raise TaskRefused("这条待办没有派给你", status_code=403, code="task_not_assigned")
    now = utcnow()
    username = getattr(principal, "username", None)
    claimed = await session.execute(
        update(Task).where(Task.id == task.id, Task.status == "pending")
        .values(status="resolved", resolved_by=username, resolved_at=now, outcome=outcome))
    if claimed.rowcount != 1:
        return None
    if mine is not None:
        await session.execute(update(TaskAssignment).where(TaskAssignment.id == mine.id).values(
            status="responded", responded_by=username, responded_at=now,
            verified=bool(getattr(principal, "verified", False)),
            response={"decision": outcome.get("decision"), "note": outcome.get("note") or ""}))
    await session.execute(update(TaskAssignment).where(TaskAssignment.task_id == task.id,
                                                       TaskAssignment.status == "pending")
                          .values(status="withdrawn"))
    return task.id


async def _assign(session: AsyncSession, task: Task, run: Run | None, assignee: Any, *,
                  source: str | None = None) -> list[TaskAssignment]:
    rows = []
    for kind, name, origin in await _targets(session, task, run, assignee):
        row = TaskAssignment(task_id=task.id, principal_kind=kind, principal=name, source=source or origin)
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


#: 蓝图试运行的运行来源（engine/origin.py）：待办派给发起人自己、调度不处理时限
TRIAL_ORIGIN = "trial"
#: 服务账号（users.kind，docs/design/next-round/04-对外集成与触达.md A1）：只持有访问密钥、回复不了待办，所以不派给它——
#: 组展开时跳过、发起人是它时交给任何人、表达式里点了它的名字跳过、改派给它拒收
SERVICE_KIND = "service"


async def _service_accounts(session: AsyncSession, names: list[str]) -> set[str]:
    """这几个用户名里哪些是服务账号。"""
    wanted = sorted({n for n in names if n})
    if not wanted:
        return set()
    return set((await session.execute(select(User.username).where(
        User.username.in_(wanted), User.kind == SERVICE_KIND))).scalars())


def key_trace(principal: Any) -> dict[str, Any]:
    """用访问密钥回复时 task.responded 另记的：via=key、哪一把、叫什么（auth/keys.trace）。别的认法什么都不加。"""
    from app.auth.keys import trace

    return trace(principal)


def _refuse_key(task: Task, principal: Any) -> None:
    """用访问密钥回复待办（04 细案 A4，集成-I3）：只给个人访问密钥，只限审批、补充输入、编辑稿件三种。确认写入、核实写入
    结果、双盲复算裁决、工具和代码审批要人在界面上看过确认卡再按，一律 403（key_task_kind）。表单里有文件控件的也不行
    （文件要先在应用里上传，422 key_files_unsupported）。不是访问密钥认出来的不管。"""
    if getattr(principal, "via", None) != "key":
        return
    from app.auth import keys
    from app.files import store as files_store

    if task.kind not in keys.TASK_KINDS:
        raise TaskRefused("这种待办不能用访问密钥回复：确认写入、核实写入结果、双盲复算裁决、工具和代码审批要在界面上看过"
                          "确认卡再处理", status_code=403, code=keys.KEY_TASK_KIND)
    if files_store.file_fields(task.form):
        raise TaskRefused("这条待办的表单里要传文件，不能用访问密钥回复：请在应用里处理", status_code=422,
                          code=keys.KEY_FILES_UNSUPPORTED)


async def _targets(session: AsyncSession, task: Task, run: Run | None, assignee: Any) -> list[tuple[str, str, str]]:
    """(principal_kind, principal, source) 列表，去重。"""
    spec = assignee if isinstance(assignee, dict) else {}
    kind = spec.get("kind")
    out: list[tuple[str, str, str]] = []
    if run is not None and run.origin_kind == TRIAL_ORIGIN and kind != "hook":
        # 试运行：不管图里派给谁（岗位、组、表达式），都派给发起试运行的人自己，来源记 trial。等外部系统的照旧等外部系统
        return [("user", run.started_by, TRIAL_ORIGIN) if run.started_by else ("anyone", "", TRIAL_ORIGIN)]
    if kind == "slot":
        slot = str(spec.get("slot") or "")
        bound = await _slot_binding(session, run, slot)
        if bound:
            for item in bound:
                out += await _expand(session, task, str(item.get("kind") or "user"), str(item.get("name") or ""),
                                     f"slot:{slot}")
        else:
            out.append(("anyone", "", f"slot:{slot}"))
    elif kind == "initiator":
        # 用服务账号的访问密钥发起的实例：服务账号不能被派待办（它回复不了），交给「任何人」（搭建者及以上能处理）
        starter = run.started_by if run is not None and run.started_by \
            and not await _service_accounts(session, [run.started_by]) else None
        out.append(("user", starter, "initiator") if starter else ("anyone", "", "initiator"))
    elif kind == "expression":
        service = await _service_accounts(session, [str(n) for n in spec.get("names") or []])
        for name in spec.get("names") or []:
            if str(name) in service:
                continue
            is_group = await session.scalar(select(Group.id).where(Group.name == str(name)))
            out += await _expand(session, task, "group" if is_group else "user", str(name), "expression")
    elif kind == "hook":
        out.append(("hook", "", "hook"))
    out = [t for t in out if t[1] or t[0] in ("anyone", "hook")]
    return list(dict.fromkeys(out)) or [("anyone", "", "")]


async def _slot_binding(session: AsyncSession, run: Run | None, slot: str) -> list[dict[str, Any]] | None:
    """应用版本里这个岗位绑定的人或组。没有应用（画布、定时任务）或岗位没绑定：None（派给任何人）。"""
    if run is None or not run.app_id or run.app_version is None or not slot:
        return None
    bindings = await session.scalar(select(AppVersion.bindings).where(
        AppVersion.app_id == run.app_id, AppVersion.version == run.app_version))
    bound = ((bindings or {}).get("slots") or {}).get(slot)
    return [b for b in bound if isinstance(b, dict)] if isinstance(bound, list) else None


async def _expand(session: AsyncSession, task: Task, kind: str, name: str, source: str) -> list[tuple[str, str, str]]:
    """组在这一刻展开成成员（快照）：之后加入组的人不会补派。组里一个人都没有时仍派给组，开了登录后组里的人能处理。"""
    if kind != "group":
        return [("user", name, source)]
    members = list((await session.execute(
        select(User.username).join(GroupMember, GroupMember.user_id == User.id)
        .join(Group, Group.id == GroupMember.group_id)
        .where(Group.name == name, User.disabled.is_(False), func.coalesce(User.kind, "person") != SERVICE_KIND)
        .order_by(User.username))).scalars())
    if not members:
        return [("group", name, source)]
    session.add(AuditEvent(actor=None, verified=False, action="task.group_expanded", subject_kind="task",
                           subject_id=task.id, data={"group": name, "members": members, "source": source}))
    return [("user", m, source) for m in members]


async def announce(task_id: str, event: EventType = EventType.TASK_OPENED) -> None:
    """待办提交之后：记 task.opened、按订阅推送通知。出错只记日志：开待办这件事已经成了。"""
    try:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            rows = await _assignments(session, [task.id])
        await record(task.run_id, event, node_id=task.node_id, task_id=task.id, kind=task.kind, title=task.title,
                     assignees=[_who(a) for a in rows.get(task.id, [])], policy=task.policy,
                     due_at=_iso(task.due_at))
    except Exception:  # noqa: BLE001
        logger.exception("待办 %s 打开后的事件没能记下", task_id)
        return
    from app.notify.service import spawn

    users = [a.principal for a in rows.get(task_id, []) if a.principal_kind == "user"]
    if task.kind == RECALL_KIND:
        # 口径召回：口径升版、数变了的推 ledger.recall，订阅了 task.opened 的渠道另推一条（同一个渠道只推一次）
        from app.notify.service import notify_recall

        spawn(notify_recall(task_id, users))
        return
    # 推送放到后台：接收端挂住时超时加重试要几十秒，不能让运行停在「执行中」等它
    spawn(_notify(task_id, "task.opened", users))


# --------------------------------------------------------------------------
# 回复
# --------------------------------------------------------------------------


def _matches(a: TaskAssignment, principal: Any) -> bool:
    return a.status == "pending" and _for(a, principal)


def _for(a: TaskAssignment, principal: Any) -> bool:
    """这一份派的是不是这个人（不看回没回复）。"""
    if getattr(principal, "mode", "off") == "off":
        return True
    if a.principal_kind == "user":
        return a.principal == principal.username
    if a.principal_kind == "group":
        return a.principal in (principal.groups or ())
    if a.principal_kind == "anyone":
        return principal.at_least("builder")
    return a.principal_kind == "hook" and principal.at_least("admin")


def handles(rows: list[TaskAssignment], principal: Any) -> bool:
    """这个人是不是这条待办的处理人：有一份没撤回的派给他（已经回复过的也算）。关登录时人人都算。

    实例页的待办组件按它分：处理人给待办卡（能处理的给表单），别人只看这一步的状态，不去取待办——待办里带着中断内容
    （报告概况、动作预览），按 id 读只给处理人和搭建者以上（visible）。"""
    return any(a.status != "withdrawn" and _for(a, principal) for a in rows)


def _pick(task: Task, rows: list[TaskAssignment], principal: Any) -> TaskAssignment:
    """这个人回复的是哪一份。同一个人在一条会签待办上只能投一票（关登录时按自报的署名认，没署名的认不出）。"""
    name = principal.username
    if name and task.kind in VOTING and any(a.status == "responded" and a.responded_by == name for a in rows):
        raise TaskRefused("你已经回复过这条待办，正在等其他人处理", status_code=409, code="task_already_responded")
    candidates = [a for a in rows if _matches(a, principal)]
    if not candidates:
        if not any(a.status == "pending" for a in rows):
            raise TaskRefused("这条待办已经没有待回复的人，请刷新页面查看最新状态", status_code=409)
        raise TaskRefused("这条待办没有派给你", status_code=403, code="task_not_assigned")
    # 派给自己的那一份优先，其次是组，最后是「任何人」
    order = {"user": 0, "group": 1, "hook": 2, "anyone": 3}
    candidates.sort(key=lambda a: (0 if a.principal == name and a.principal_kind == "user" else 1,
                                   order.get(a.principal_kind, 9)))
    return candidates[0]


def _closed(status: str) -> str:
    return f"这条待办{_STATUS_TEXT.get(status, '已处理')}，请刷新页面查看最新状态"


def _verdict_of_raw(task: Task, response: Any) -> str:
    from app.engine.approval import read_decision

    decision = read_decision(response)
    if task.kind in VOTING:
        return "approve" if decision.approved else "reject"
    # 补填、改稿只有明说了「不」才算驳回：那两种模式下裸字符串就是内容本身（同 human 节点）
    return "reject" if isinstance(response, dict) and decision.rejected else "submit"


def _engine_response(task: Task, approval: Approval | None, decision: Any, values: dict[str, Any] | None,
                     note: str | None, payload_hash: str | None) -> tuple[Any, str]:
    """(交给节点的回复, 这一票的结论 approve / reject / submit)。"""
    card = (approval.payload if approval is not None else None) or {}
    if isinstance(decision, RawResponse):
        response = decision.response
        if task.kind == "action_confirm":
            given = response.get("payload_hash") if isinstance(response, dict) else None
            _check_hash(card, given, approving=_verdict_of_raw(task, response) == "approve")
        return response, _verdict_of_raw(task, response)

    values = dict(values or {})
    text = note or ""
    if task.kind in VOTING:
        if decision not in ("approve", "reject"):
            raise TaskRefused("这条待办只能批准或驳回", status_code=422, code="task_decision_invalid")
        out: dict[str, Any] = {"approved": decision == "approve", "note": text}
        if task.kind == "tool":
            if isinstance(values.get("args"), dict):
                out["args"] = values["args"]
            if values.get("always") is True:
                out["always"] = True
        elif task.kind == "code" and isinstance(values.get("code"), str) and values["code"]:
            out["code"] = values["code"]
        elif task.kind == "action_confirm":
            _check_hash(card, payload_hash, approving=decision == "approve")
            out["payload_hash"] = payload_hash
        elif task.form and decision == "approve" and values:
            out["value"] = _validated(task.form, values)
        return out, decision

    if decision not in ("submit", "approve", "reject"):
        raise TaskRefused("这条待办只能提交或驳回", status_code=422, code="task_decision_invalid")
    if decision == "reject":
        return {"approved": False, "note": text}, "reject"
    if task.form:
        value: Any = _validated(task.form, values)
    elif set(values) == {"value"}:
        value = values["value"]       # 老写法：一个输入框的内容
    else:
        value = values or None
    return {"approved": True, "note": text, "value": value}, "submit"


def _check_hash(card: dict[str, Any], given: Any, *, approving: bool) -> None:
    """动作确认按引用确认：确认卡上的哈希原样带回来，和这张卡对得上才算数（驳回不用带）。"""
    if not approving:
        return
    if not given:
        raise TaskRefused("确认发送需要带上确认卡上的内容指纹，请刷新页面后重新确认", status_code=422,
                          code="payload_hash_missing")
    if given != card.get("payload_hash"):
        raise TaskRefused("你确认的内容和现在待发送的不一致（内容指纹对不上），请刷新页面查看最新的确认卡",
                          status_code=422, code="payload_hash_mismatch")


async def _check_principals(session: AsyncSession, task: Task, principal: Any, values: dict[str, Any]) -> None:
    """待办表单里的选人控件：开了登录时按候选池核对（同入口表单，auth/directory.principal_errors）。不属于应用的待办
    （试运行、画布）没有池，不核对。"""
    from app.auth.directory import principal_errors

    if not task.app_id or getattr(principal, "mode", "off") == "off":
        return
    app = await session.get(App, task.app_id)
    if app is None:
        return
    bindings = await session.scalar(select(AppVersion.bindings).where(
        AppVersion.app_id == task.app_id, AppVersion.version == task.app_version)) if task.app_version else None
    if errors := await principal_errors(session, app, bindings, task.form, values, principal.mode):
        raise FormInvalid(errors)


def _validated(form: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    from app.apps.forms import validate_form

    if errors := validate_form(form, values):
        raise FormInvalid([dict(e) for e in errors])
    return values


def _tally(task: Task, rows: list[TaskAssignment], mine: TaskAssignment, verdict: str) -> str | None:
    """凑齐了没有：返回结论（approve / reject / submit），还要等别人返回 None。"""
    policy = task.policy or {"kind": "any"}
    if task.kind not in VOTING or policy.get("kind", "any") == "any" or mine.source == "escalation":
        return verdict
    voters = [a for a in rows if a.status != "withdrawn" and a.source != "escalation"]
    n = len(voters)
    said = [((a.response or {}).get("decision")) for a in voters if a.status == "responded"]
    yes, no = said.count("approve"), said.count("reject")
    if policy["kind"] == "all":
        return "reject" if no else "approve" if yes >= n else None
    m = min(int(policy.get("m") or 1), n)
    return "approve" if yes >= m else "reject" if no > n - m else None


def progress(task: Task, rows: list[TaskAssignment]) -> dict[str, Any] | None:
    """会签进度（给人看的那一份，界面照着画）：只算批准 / 驳回这一类。口径同 _tally——升级加派的那一份不算进会签人数，
    它一人说了算；以前界面把它和原来的人算在一起，升级后写「0/3 已同意」，其实总监一人同意就通过。

    voters 是会签的人数：待处理时按没撤回的算；判定之后剩下没回复的都撤回了，按判定那一刻记下的人数（outcome.voters），
    老数据没记的按回复过的算。escalation 是升级加派的那几份：升级给的岗位、派给了谁、谁一人定了什么。"""
    if task.kind not in VOTING:
        return None
    policy = task.policy or {"kind": "any"}
    own = [a for a in rows if a.source != "escalation"]
    said = [(a.response or {}).get("decision") for a in own if a.status == "responded"]
    recorded = (task.outcome or {}).get("voters")
    if task.status == "pending":
        voters = len([a for a in own if a.status != "withdrawn"])
    else:
        voters = recorded if isinstance(recorded, int) else max(len(said), 1)
    kind = policy.get("kind", "any")
    need = voters if kind == "all" else min(int(policy.get("m") or 1), voters) if kind == "quorum" else 1
    out: dict[str, Any] = {"policy": kind, "voters": voters, "need": need, "agreed": said.count("approve"),
                           "rejected": said.count("reject")}
    escalated = [a for a in rows if a.source == "escalation"]
    if escalated:
        decider = next((a for a in escalated if a.status == "responded"), None)
        out["escalation"] = {"slot": str((task.on_timeout or {}).get("to_slot") or "").strip() or None,
                             "assignees": [_who(a) for a in escalated],
                             "decided": (decider.response or {}).get("decision") if decider else None,
                             "by": decider.responded_by if decider else None}
    return out


def _signoffs(rows: list[TaskAssignment]) -> list[dict[str, Any]]:
    return [{"by": a.responded_by, "decision": (a.response or {}).get("decision"),
             "note": (a.response or {}).get("note") or "", "at": _iso(a.responded_at), "verified": a.verified,
             "source": a.source}
            for a in sorted((a for a in rows if a.status == "responded"), key=lambda a: a.responded_at or utcnow())]


async def respond(task_id: str, principal: Any, *, decision: Any, values: dict[str, Any] | None = None,
                  note: str | None = None, payload_hash: str | None = None) -> dict[str, Any]:
    """一个人的回复。返回 {task, resolved: bool, run?}。

    decision：approve / reject（批准类）、submit / reject（补填、改稿、双盲裁决）、核实待办的三种结论；老接口传
    RawResponse。values：表单的值，工具审批改过的参数（args）、「始终允许」（always），代码审批改过的代码（code），
    核实「已生效」时的外部单号（external_id）。"""
    from app.engine.inquiry import BUSY_DECIDE, INQUIRY_BUSY, inquiry_busy

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise TaskRefused("待办不存在，所属的运行可能已被删除", status_code=404)
        _refuse_key(task, principal)
        if task.kind == "reconcile":
            return await _respond_reconcile(session, task, principal, decision, values, note)
        if task.kind == REVIEW_KIND:
            return await _respond_review(session, task, principal, decision, note)
        if task.kind == RECALL_KIND:
            return await _respond_recall(session, task, principal, decision, note)
        if task.status != "pending":
            raise TaskRefused(_closed(task.status))
        if inquiry_busy(task.run_id):
            raise TaskRefused(BUSY_DECIDE, code=INQUIRY_BUSY)
        approval = await session.get(Approval, task.approval_id) if task.approval_id else None
        rows = (await _assignments(session, [task.id])).get(task.id, [])
        mine = _pick(task, rows, principal)
        response, verdict = _engine_response(task, approval, decision, values, note, payload_hash)
        if task.form and verdict in ("approve", "submit") and values:
            await _check_principals(session, task, principal, values)
        response = await _bind_files(session, task, principal, response)
        now = utcnow()
        marked = await session.execute(
            update(TaskAssignment).where(TaskAssignment.id == mine.id, TaskAssignment.status == "pending")
            .values(status="responded", responded_by=principal.username, responded_at=now,
                    verified=bool(principal.verified), response={"decision": verdict, "note": note or ""}))
        if marked.rowcount != 1:
            await session.rollback()
            raise TaskRefused("这一份已经有人回复过，请刷新页面查看最新状态")
        rows = (await _assignments(session, [task.id], fresh=True)).get(task.id, [])
        mine = next(a for a in rows if a.id == mine.id)
        decided = _tally(task, rows, mine, verdict)
        voters = [a for a in rows if a.status != "withdrawn"]
        responded = sum(1 for a in voters if a.status == "responded")
        final: Any = None
        if decided is not None:
            final = response
            if task.kind in VOTING and (task.policy or {}).get("kind", "any") != "any":
                # 会签：交给节点的是汇总的结论和明细，human.resolved 里写得出谁、怎么决定的
                signoffs = _signoffs(rows)
                final = {"approved": decided == "approve",
                         "note": "；".join(s["note"] for s in signoffs if s["note"]),
                         "signoffs": signoffs, "policy": task.policy}
            # 会签人数记在结论里：判定之后没回复的都撤回了，进度要按判定那一刻的人数说（progress）
            claimed = await session.execute(
                update(Task).where(Task.id == task.id, Task.status == "pending")
                .values(status="resolving", resolved_by=principal.username,
                        outcome={"decision": decided, "by_assignment": mine.id, "signoffs": _signoffs(rows),
                                 "voters": len([a for a in voters if a.source != "escalation"])}))
            if claimed.rowcount != 1:
                await session.rollback()
                raise TaskRefused(_closed("resolving"))
        await session.commit()
        run_id, approval_id, node_id = task.run_id, task.approval_id, task.node_id

    await record(run_id, EventType.TASK_RESPONDED, node_id=node_id, task_id=task_id, actor=principal.username,
                 verified=bool(principal.verified), decision=verdict, index=responded, of=len(voters),
                 **key_trace(principal))
    if decided is None:
        return {"task": await detail(task_id, principal), "resolved": False, "run": await _run(run_id)}

    from app.engine.runner import run_manager

    try:
        run = await run_manager.resume(run_id, final, approval_id=approval_id, actor=principal.username)
    except (ValueError, KeyError) as e:
        await _unclaim(task_id, mine.id)
        raise TaskRefused(str(e.args[0]) if e.args else str(e)) from e
    except Exception as e:  # noqa: BLE001 - 判定之后恢复不成：不管是什么错，待办都不能卡在 resolving
        # 以前只接 ValueError / KeyError：别的异常（库被锁、检查点读不出……）直接冒上去，待办停在 resolving，再也没人
        # 能回复，只有重启时 startup 才收拾。这里一律退回待处理、这一票撤回，原因写进日志、交回调用方
        logger.exception("待办 %s 判定后恢复运行 %s 出错", task_id, run_id)
        closed = await _unclaim(task_id, mine.id)
        from app.core.errors import describe_exception

        after = "运行已经结束，这条待办随之关闭" if closed else "你的回复没有生效，待办已退回待处理，请稍后重试"
        raise TaskRefused(f"恢复运行时出错：{describe_exception(e)}。{after}", status_code=500,
                          code="task_resume_failed") from e
    await _settle(task_id)
    await record(run_id, EventType.TASK_RESOLVED, node_id=node_id, task_id=task_id, decision=decided,
                 actor=principal.username, signoffs=_signoffs(rows))
    return {"task": await detail(task_id, principal), "resolved": True, "run": run}


async def _respond_reconcile(session: AsyncSession, task: Task, principal: Any, decision: Any,
                             values: dict[str, Any] | None, note: str | None) -> dict[str, Any]:
    if task.status != "pending":
        raise TaskRefused(_closed(task.status))
    if decision not in RECONCILE_OUTCOMES:
        raise TaskRefused("核实只能选已生效、未生效重新发送、未生效放弃之一", status_code=422,
                          code="task_decision_invalid")
    rows = (await _assignments(session, [task.id])).get(task.id, [])
    _pick(task, rows, principal)
    action_id = (task.correlation_key or "").removeprefix("action:")
    task_id = task.id
    await session.rollback()
    from app.process.actions import reconcile

    out = await reconcile(action_id, principal, outcome=decision,
                          external_id=str((values or {}).get("external_id") or "") or None, note=note)
    return {"task": await detail(task_id, principal), "resolved": True, "run": out.get("run")}


async def _bind_files(session: AsyncSession, task: Task, principal: Any, response: Any) -> Any:
    """回复里表单的文件（docs/design/normal-user-apps.md N9）：核对是这个人在这条待办上传的、还没用过，绑到这次运行，
    和回复一起提交；交给节点的值换成登记的那一份引用。引用不对按表单错误交回，落到那个字段上。"""
    from app.files import store as files_store

    value = response.get("value") if isinstance(response, dict) else None
    if not isinstance(value, dict) or not files_store.file_fields(task.form):
        return response
    try:
        bound = await files_store.bind_in(session, task.form, value, principal=principal, run_id=task.run_id,
                                          context={"task_id": task.id})
    except files_store.FileRefused as e:
        await session.rollback()
        raise FormInvalid([{"path": e.field or "", "code": files_store.FILE_ERROR_CODE, "message": e.message}]) from e
    return {**response, "value": bound}


async def _respond_review(session: AsyncSession, task: Task, principal: Any, decision: Any,
                          note: str | None) -> dict[str, Any]:
    """审核待办（个人应用提交审核）在待办里只能退回：同意要取共享副本、在副本上走发布单，发布单做完自动记为已发布
    （blueprints/personal.review_published）。退回的结论、蓝图状态和通知由 blueprints/personal.reject_review 做。"""
    if task.status != "pending":
        raise TaskRefused(_closed(task.status))
    if decision != "reject":
        raise TaskRefused("审核个人应用不在这里同意：请取一份共享副本，在副本上用真数试一次、走发布单，发布之后这条待办"
                          "自动记为已发布；不发布请退回", status_code=422, code="task_decision_invalid")
    task_id, run_id = task.id, task.run_id
    await session.rollback()
    from app.blueprints.personal import reject_review

    await reject_review(task_id, principal, note=note)
    return {"task": await detail(task_id, principal), "resolved": True, "run": await _run(run_id)}


async def _respond_recall(session: AsyncSession, task: Task, principal: Any, decision: Any,
                          note: str | None) -> dict[str, Any]:
    """recall 待办：「按新版重新出具」（app/ledger/recall.reissue，走按口径出具同一套判法；不能出具照实拒绝，待办留着）或
    「知道了」。处理完记成已处理（条件 UPDATE，同 close_review），结论和出具的那次正式运行写进 outcome，另记一条审计：运行
    已经封存，事件记不进去。"""
    if task.status != "pending":
        raise TaskRefused(_closed(task.status))
    reason = (task.correlation_key or "").split(":", 1)[0]
    allowed = RECALL_DECISIONS if reason == "caliber" else ("ack",)
    if decision not in allowed:
        raise TaskRefused("这条待办只能「按新版重新出具」或「知道了」" if reason == "caliber"
                          else "这条待办只能「知道了」", status_code=422, code="task_decision_invalid")
    rows = (await _assignments(session, [task.id])).get(task.id, [])
    mine_id = _pick(task, rows, principal).id
    task_id, run_id = task.id, task.run_id
    await session.rollback()
    outcome: dict[str, Any] = {"decision": decision, "note": (note or "").strip()[:2000]}
    issued: Any = None
    if decision == "reissue":
        from app.ledger import recall

        try:
            issued = await recall.reissue(task_id, principal)
        except recall.RecallRefused as e:
            raise TaskRefused(e.message, status_code=e.status_code, code=e.code) from e
        outcome["issued_run"] = issued.id
    async with SessionLocal() as session:
        now = utcnow()
        claimed = await session.execute(
            update(Task).where(Task.id == task_id, Task.status == "pending")
            .values(status="resolved", resolved_by=principal.username, resolved_at=now, outcome=outcome))
        if claimed.rowcount != 1:
            await session.rollback()
            raise TaskRefused(_closed("resolved"))
        await session.execute(update(TaskAssignment).where(TaskAssignment.id == mine_id).values(
            status="responded", responded_by=principal.username, responded_at=now, verified=bool(principal.verified),
            response={"decision": decision, "note": outcome["note"]}))
        await session.execute(update(TaskAssignment).where(TaskAssignment.task_id == task_id,
                                                           TaskAssignment.status == "pending")
                              .values(status="withdrawn"))
        session.add(AuditEvent(actor=principal.username, verified=bool(principal.verified),
                               action=f"ledger.recall.{decision}", subject_kind="task", subject_id=task_id,
                               data={"run_id": run_id, **({"issued_run": issued.id} if issued is not None else {}),
                                     **key_trace(principal)}))
        await session.commit()
    return {"task": await detail(task_id, principal), "resolved": True, "run": await _run(run_id)}


async def close_reconcile(session: AsyncSession, action_id: str, principal: Any, outcome: str,
                          note: str | None) -> str | None:
    """核实之后把这个动作的核实待办收掉（条件 UPDATE，和 tick、别人的回复抢同一行）。返回待办 id，没有返回 None。"""
    task = await session.scalar(select(Task).where(Task.correlation_key == f"action:{action_id}",
                                                   Task.kind == "reconcile", Task.status == "pending"))
    if task is None:
        return None
    now = utcnow()
    username = getattr(principal, "username", None)
    claimed = await session.execute(
        update(Task).where(Task.id == task.id, Task.status == "pending")
        .values(status="resolved", resolved_by=username, resolved_at=now, outcome={"decision": outcome}))
    if claimed.rowcount != 1:
        return None
    rows = (await _assignments(session, [task.id])).get(task.id, [])
    mine = next((a for a in rows if principal is not None and _matches(a, principal)), None)
    if mine is not None:
        await session.execute(update(TaskAssignment).where(TaskAssignment.id == mine.id).values(
            status="responded", responded_by=username, responded_at=now,
            verified=bool(getattr(principal, "verified", False)), response={"decision": outcome, "note": note or ""}))
    await session.execute(update(TaskAssignment).where(TaskAssignment.task_id == task.id,
                                                       TaskAssignment.status == "pending")
                          .values(status="withdrawn"))
    return task.id


async def _unclaim(task_id: str, assignment_id: str) -> bool:
    """恢复没成：运行已经结束的，待办随之关闭；还停着的，退回 pending，这一票撤回、可以再回复。返回是不是关闭了。"""
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        status = await session.scalar(select(Run.status).where(Run.id == task.run_id)) if task else None
        if task is None:
            return True
        if status in _TERMINAL_RUN or status is None:
            task.status = "cancelled"
            task.resolved_at = utcnow()
            await session.execute(update(TaskAssignment).where(TaskAssignment.task_id == task_id,
                                                               TaskAssignment.status == "pending")
                                  .values(status="withdrawn"))
        else:
            task.status, task.outcome, task.resolved_by = "pending", None, None
            await session.execute(update(TaskAssignment).where(TaskAssignment.id == assignment_id)
                                  .values(status="pending", response=None, responded_by=None, responded_at=None,
                                          verified=False))
        await session.commit()
    closed = status in _TERMINAL_RUN or status is None
    logger.warning("待办 %s 判定后恢复运行没有成功，已%s", task_id, "随运行关闭" if closed else "退回待处理")
    return closed


async def _settle(task_id: str) -> None:
    async with SessionLocal() as session:
        await session.execute(update(Task).where(Task.id == task_id, Task.status == "resolving")
                              .values(status="resolved", resolved_at=utcnow()))
        await session.execute(update(TaskAssignment).where(TaskAssignment.task_id == task_id,
                                                           TaskAssignment.status == "pending")
                              .values(status="withdrawn"))
        await session.commit()


async def _run(run_id: str) -> Run | None:
    async with SessionLocal() as session:
        return await session.get(Run, run_id)


# --------------------------------------------------------------------------
# 改派、催办
# --------------------------------------------------------------------------


async def _can_manage(session: AsyncSession, task: Task, principal: Any) -> bool:
    """改派、催办：关登录或管理员都行；builder 要有这个应用的 manage 授权（或者是应用的创建者）；没有应用的待办 builder 就行。"""
    if getattr(principal, "mode", "off") == "off" or principal.at_least("admin"):
        return True
    if not principal.at_least("builder"):
        return False
    if not task.app_id:
        return True
    from app.db.models import AppGrant

    app = await session.get(App, task.app_id)
    if app is not None and app.created_by and app.created_by == principal.username:
        return True
    grant = await session.scalar(select(AppGrant.id).where(
        AppGrant.app_id == task.app_id, AppGrant.role == "manage",
        or_(and_(AppGrant.principal_kind == "user", AppGrant.principal == principal.username),
            and_(AppGrant.principal_kind == "group", AppGrant.principal.in_(list(principal.groups or ()))))))
    return grant is not None


async def reassign(task_id: str, principal: Any, *, to: list[dict[str, str]], note: str | None = None) -> Any:
    """改派：撤回还没回复的那几份，派给新的人或组（组照样按当时的成员展开）。已经回复的票留着。返回待办详情。"""
    targets = [(str(t.get("kind") or ""), str(t.get("name") or "").strip()) for t in to or []]
    if not targets or any(k not in ("user", "group") or not n for k, n in targets):
        raise TaskRefused("改派需要选择至少一位用户或一个组", status_code=422, code="reassign_empty")
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise TaskRefused("待办不存在，所属的运行可能已被删除", status_code=404)
        if task.status != "pending":
            raise TaskRefused(_closed(task.status))
        if not await _can_manage(session, task, principal):
            raise TaskRefused("只有管理员或这个应用的管理者可以改派待办", status_code=403)
        if service := await _service_accounts(session, [n for k, n in targets if k == "user"]):
            raise TaskRefused(f"「{'、'.join(sorted(service))}」是服务账号，不能被派待办：请改派给人或组", status_code=422,
                              code="reassign_service")
        await session.execute(update(TaskAssignment).where(TaskAssignment.task_id == task_id,
                                                           TaskAssignment.status == "pending")
                              .values(status="withdrawn"))
        added: list[TaskAssignment] = []
        for kind, name in dict.fromkeys(targets):
            for k, n, _ in await _expand(session, task, kind, name, "reassign"):
                row = TaskAssignment(task_id=task_id, principal_kind=k, principal=n, source="reassign")
                session.add(row)
                added.append(row)
        await session.commit()
        run_id, node_id = task.run_id, task.node_id
    await record(run_id, EventType.TASK_REASSIGNED, node_id=node_id, task_id=task_id, actor=principal.username,
                 to=[_who(a) for a in added], note=note or "")
    from app.notify.service import spawn

    spawn(_notify(task_id, "task.reassigned", [a.principal for a in added if a.principal_kind == "user"]))
    return await detail(task_id, principal)


async def remind(task_id: str, principal: Any) -> dict[str, Any]:
    """催办：给还没回复的人再推一次通知。返回 {sent: 推送了几条}。"""
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise TaskRefused("待办不存在，所属的运行可能已被删除", status_code=404)
        if task.status != "pending":
            raise TaskRefused(_closed(task.status))
        if not await _can_manage(session, task, principal):
            raise TaskRefused("只有管理员或这个应用的管理者可以催办", status_code=403)
        rows = (await _assignments(session, [task_id])).get(task_id, [])
    users = [a.principal for a in rows if a.status == "pending" and a.principal_kind == "user"]
    await record(task.run_id, EventType.TASK_REMINDED, node_id=task.node_id, task_id=task_id,
                 actor=principal.username, to=[_who(a) for a in rows if a.status == "pending"])
    return {"sent": await _notify(task_id, "task.reminded", users)}


async def _notify(task_id: str, event: str, users: list[str]) -> int:
    try:
        from app.notify.service import notify_task

        return await notify_task(task_id, event, users=users)
    except Exception:  # noqa: BLE001 - 通知推不出去不影响待办本身
        logger.exception("待办 %s 的通知（%s）没能推送", task_id, event)
        return 0


# --------------------------------------------------------------------------
# 时限：提醒、升级、超时驳回
# --------------------------------------------------------------------------


async def tick(now: datetime | None = None) -> list[str]:
    """调度循环每一拍：提醒、升级、超时驳回（永不自动批准）。交回这一拍处理过的待办 id（生效可查：办了哪几条，
    不只是几条；测试按自己造的那条断言，不受同一个库里别的待办影响）。试运行的待办不处理（只显示时限）。"""
    now = now or utcnow()
    action = json_text(Task.on_timeout, "action")
    trial = select(Run.id).where(Run.origin_kind == TRIAL_ORIGIN)
    async with SessionLocal() as session:
        due = list((await session.execute(select(Task.id).where(
            Task.status == "pending", Task.run_id.not_in(trial), or_(
                and_(Task.remind_at.is_not(None), Task.remind_at <= now),
                and_(Task.due_at.is_not(None), Task.due_at <= now, action == "reject"),
                and_(Task.due_at.is_not(None), Task.due_at <= now, action == "escalate", Task.escalation_level == 0),
            )).order_by(Task.due_at, Task.id))).scalars())
    done: list[str] = []
    for task_id in due:
        try:
            if await _on_timeout(task_id, now):
                done.append(task_id)
        except Exception:  # noqa: BLE001 - 一条出错不拖住这一拍的其他待办
            logger.exception("待办 %s 的超时处理失败，下一拍再试", task_id)
    return done


async def _on_timeout(task_id: str, now: datetime) -> int:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None or task.status != "pending":
            return 0
        timeout = task.on_timeout or {}
        action = timeout.get("action")
        if action == "remind" and task.remind_at is not None and _aware(task.remind_at) <= now:
            every = parse_duration(timeout.get("every"))
            claimed = await session.execute(update(Task).where(
                Task.id == task_id, Task.status == "pending", Task.remind_at == task.remind_at)
                .values(remind_at=now + every if every else None))
            await session.commit()
            if claimed.rowcount != 1:
                return 0
            rows = (await _assignments(session, [task_id])).get(task_id, [])
            pending = [a for a in rows if a.status == "pending"]
            await record(task.run_id, EventType.TASK_REMINDED, node_id=task.node_id, task_id=task_id, actor=None,
                         overdue=True, to=[_who(a) for a in pending])
            from app.notify.service import spawn

            spawn(_notify(task_id, "task.reminded", [a.principal for a in pending if a.principal_kind == "user"]))
            return 1
        if action == "escalate" and task.escalation_level == 0:
            # 和派生、检查认的是同一个岗位名（apps/tree.slot_refs 去掉了首尾空白）
            slot = str(timeout.get("to_slot") or "").strip()
            claimed = await session.execute(update(Task).where(
                Task.id == task_id, Task.status == "pending", Task.escalation_level == 0)
                .values(escalation_level=Task.escalation_level + 1))
            if claimed.rowcount != 1:
                await session.rollback()
                return 0
            run = await session.get(Run, task.run_id)
            added = await _assign(session, task, run, {"kind": "slot", "slot": slot}, source="escalation")
            await session.commit()
            await record(task.run_id, EventType.TASK_ESCALATED, node_id=task.node_id, task_id=task_id, slot=slot,
                         level=1, to=[_who(a) for a in added])
            from app.notify.service import spawn

            spawn(_notify(task_id, "task.escalated", [a.principal for a in added if a.principal_kind == "user"]))
            return 1
        if action != "reject" or task.due_at is None or _aware(task.due_at) > now:
            return 0
        rows = (await _assignments(session, [task_id])).get(task_id, [])
        voters = len([a for a in rows if a.status != "withdrawn" and a.source != "escalation"])
        claimed = await session.execute(update(Task).where(Task.id == task_id, Task.status == "pending")
                                        .values(status="resolving",
                                                outcome={"decision": "expired_reject", "voters": voters}))
        await session.commit()
        if claimed.rowcount != 1:
            return 0
        run_id, approval_id, node_id = task.run_id, task.approval_id, task.node_id
    await record(run_id, EventType.TASK_EXPIRED, node_id=node_id, task_id=task_id, action="reject")
    from app.engine.runner import run_manager

    try:
        # expired：节点据此把这次驳回记成超时驳回（engine/approval.Decision.expired → NodeRejected.decision）
        await run_manager.resume(run_id, {"approved": False, "note": EXPIRED_NOTE, "expired": True},
                                 approval_id=approval_id)
    except (ValueError, KeyError):
        logger.warning("待办 %s 超时驳回时运行已无法恢复", task_id)
        async with SessionLocal() as session:
            await session.execute(update(Task).where(Task.id == task_id).values(status="cancelled",
                                                                               resolved_at=utcnow()))
            await session.commit()
        return 1
    except Exception:  # noqa: BLE001 - 同 respond：别的错不能让待办卡在 resolving，退回待处理，下一拍再驳回
        logger.exception("待办 %s 超时驳回时恢复运行出错，退回待处理，下一拍再试", task_id)
        async with SessionLocal() as session:
            await session.execute(update(Task).where(Task.id == task_id, Task.status == "resolving")
                                  .values(status="pending", outcome=None))
            await session.commit()
        return 0
    await _settle(task_id)
    await record(run_id, EventType.TASK_RESOLVED, node_id=node_id, task_id=task_id, decision="expired_reject",
                 actor=None)
    return 1


def _aware(moment: datetime) -> datetime:
    from datetime import timezone

    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 启动时整理、运行结束时收尾
# --------------------------------------------------------------------------


async def startup(*, live: Any = None) -> int:
    """启动时：存量的待审批补建 Task（派给任何人，幂等）；上次停在 resolving 的（判定之后、恢复之前进程没了）按 Approval
    的状态收拾——还在等的退回 pending、撤回那一票，已经交给引擎的记成已处理。返回补建的条数。

    多进程：别的进程活着时，resolving 可能正是它在判定、恢复（几秒的事），只收拾停在 resolving 超过两个租约时长的。"""
    from datetime import timedelta

    from app.cluster import state
    from app.cluster.workers import lease_s

    live = live if live is not None else await state.current(fresh=True)
    stale_before = utcnow() - timedelta(seconds=2 * lease_s()) if live.others else None
    async with SessionLocal() as session:
        orphans = list((await session.execute(
            select(Approval).outerjoin(Task, Task.approval_id == Approval.id)
            .where(Approval.status == "pending", Task.id.is_(None)))).scalars())
        for approval in orphans:
            await open_task(session, approval)
        stuck = list((await session.execute(select(Task).where(
            Task.status == "resolving", *((Task.updated_at < stale_before,) if stale_before is not None else ())))).scalars())
        for task in stuck:
            waiting = task.approval_id and await session.scalar(
                select(Approval.status).where(Approval.id == task.approval_id)) == "pending"
            if waiting:
                assignment = (task.outcome or {}).get("by_assignment")
                task.status, task.outcome, task.resolved_by = "pending", None, None
                if assignment:
                    await session.execute(update(TaskAssignment).where(TaskAssignment.id == assignment).values(
                        status="pending", response=None, responded_by=None, responded_at=None, verified=False))
            else:
                task.status, task.resolved_at = "resolved", task.resolved_at or utcnow()
        await session.commit()
    if orphans:
        logger.info("为 %d 条存量的待审批补建了待办", len(orphans))
    return len(orphans)


async def close_for_run(session: AsyncSession, run_id: str, now: datetime) -> int:
    """运行结束（失败、取消）时，它还没处理的待办一并关闭。和关审批同一个事务（runner._close_approvals 调）。"""
    ids = list((await session.execute(select(Task.id).where(Task.run_id == run_id, Task.status == "pending")))
               .scalars())
    if not ids:
        return 0
    await session.execute(update(Task).where(Task.id.in_(ids)).values(status="cancelled", resolved_at=now))
    await session.execute(update(TaskAssignment).where(TaskAssignment.task_id.in_(ids),
                                                       TaskAssignment.status == "pending")
                          .values(status="withdrawn"))
    return len(ids)


async def task_for_approval(approval_id: str) -> str | None:
    """老接口用：这条审批对应的待办 id。还没有（开待办时出过错、老数据没补建）就现在补一条。"""
    async with SessionLocal() as session:
        task_id = await session.scalar(select(Task.id).where(Task.approval_id == approval_id))
        if task_id:
            return task_id
        approval = await session.get(Approval, approval_id)
        if approval is None:
            return None
        task = await open_task(session, approval)
        await session.commit()
        return task.id


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def _who(a: TaskAssignment) -> dict[str, str]:
    return {"kind": a.principal_kind, "name": a.principal}


def _iso(moment: datetime | None) -> str | None:
    return _aware(moment).isoformat() if moment is not None else None


async def _assignments(session: AsyncSession, task_ids: list[str], *, fresh: bool = False
                       ) -> dict[str, list[TaskAssignment]]:
    if not task_ids:
        return {}
    stmt = (select(TaskAssignment).where(TaskAssignment.task_id.in_(task_ids))
            .order_by(TaskAssignment.created_at, TaskAssignment.id))
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    out: dict[str, list[TaskAssignment]] = {}
    for row in (await session.execute(stmt)).scalars():
        out.setdefault(row.task_id, []).append(row)
    return out


def _mine(principal: Any) -> Any:
    """「派给我的」：有一份没撤回的派给我（off 模式下全部都算）。"""
    if getattr(principal, "mode", "off") == "off":
        return None
    conds = []
    if principal.username:
        conds.append(and_(TaskAssignment.principal_kind == "user", TaskAssignment.principal == principal.username))
    if principal.groups:
        conds.append(and_(TaskAssignment.principal_kind == "group",
                          TaskAssignment.principal.in_(list(principal.groups))))
    if principal.at_least("builder"):
        conds.append(TaskAssignment.principal_kind == "anyone")
    if principal.at_least("admin"):
        conds.append(TaskAssignment.principal_kind == "hook")
    if not conds:
        return Task.id.is_(None)
    return exists().where(TaskAssignment.task_id == Task.id, TaskAssignment.status != "withdrawn", or_(*conds))


async def list_tasks(principal: Any, *, assigned: str = "me", statuses: list[str] | None = None,
                     app_id: str | None = None, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        stmt = select(Task).order_by(Task.created_at.desc(), Task.id.desc()).limit(limit)
        if statuses:
            stmt = stmt.where(Task.status.in_(statuses))
        if app_id:
            stmt = stmt.where(Task.app_id == app_id)
        if run_id:
            stmt = stmt.where(Task.run_id == run_id)
        if assigned == "me" and (clause := _mine(principal)) is not None:
            stmt = stmt.where(clause)
        tasks = list((await session.execute(stmt)).scalars())
        return await _describe(session, tasks, principal)


async def count(principal: Any) -> dict[str, Any]:
    """导航徽标：派给我的、待处理的待办数。apps 是应用里的，other 是不属于应用的（画布、定时任务的审批）。

    latest 是其中最新一条的开出时间：前端的新待办通知看它变没变，而不只看数——同一拍里一条处理掉、一条新来，数不变，
    新的那条照样要通知。"""
    async with SessionLocal() as session:
        stmt = select(Task.app_id.is_not(None), func.count(), func.max(Task.created_at)).where(Task.status == "pending")
        if (clause := _mine(principal)) is not None:
            stmt = stmt.where(clause)
        rows = {has_app: (n, latest) for has_app, n, latest in
                (await session.execute(stmt.group_by(Task.app_id.is_not(None)))).all()}
    apps, other = int(rows.get(True, (0, None))[0]), int(rows.get(False, (0, None))[0])
    stamps = [latest for _, latest in rows.values() if latest is not None]
    latest = max(stamps) if stamps else None
    return {"mine": apps + other, "apps": apps, "other": other,
            "latest": latest.isoformat() if hasattr(latest, "isoformat") else latest}


async def detail(task_id: str, principal: Any) -> dict[str, Any] | None:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return None
        return (await _describe(session, [task], principal))[0]


async def _describe(session: AsyncSession, tasks: list[Task], principal: Any) -> list[dict[str, Any]]:
    """Task 的接口形状（frontend/src/typesApp.ts 的 Task）。payload 原样是中断内容，核实待办换成那个动作的预览。"""
    ids = [t.id for t in tasks]
    assignments = await _assignments(session, ids)
    approvals = {a.id: a for a in (await session.execute(
        select(Approval).where(Approval.id.in_([t.approval_id for t in tasks if t.approval_id])))).scalars()}
    runs = {r.id: r for r in (await session.execute(
        select(Run.id, Run.workflow_name, Run.graph, Run.origin, Run.origin_kind)
        .where(Run.id.in_({t.run_id for t in tasks})))).all()}
    apps = dict((await session.execute(select(App.id, App.name).where(
        App.id.in_({t.app_id for t in tasks if t.app_id})))).all())
    names = await display_names(session, [u for t in tasks for u in people_of(t, assignments.get(t.id, []))])
    out = []
    for task in tasks:
        rows = assignments.get(task.id, [])
        payload = dict(approvals[task.approval_id].payload or {}) if task.approval_id in approvals else {}
        if task.kind == "reconcile":
            payload = await _reconcile_payload(session, task)
        elif task.kind == REVIEW_KIND:
            from app.blueprints.personal import review_payload

            payload = await review_payload(session, task)
        elif task.kind == RECALL_KIND:
            from app.ledger.recall import task_payload

            payload = await task_payload(session, task, principal)
        run = runs.get(task.run_id)
        out.append({
            "id": task.id, "approval_id": task.approval_id, "run_id": task.run_id, "node_id": task.node_id,
            "node_label": _node_label(run.graph if run else None, task.node_id),
            "app_id": task.app_id, "app_name": apps.get(task.app_id), "app_version": task.app_version,
            "workflow_name": run.workflow_name if run else None,
            # 实例的标题（应用入口按 instance_title 起的）：同一个人工节点的待办标题一样，列表里靠它分开
            "instance_title": str((run.origin or {}).get("title") or "") or None if run else None,
            "kind": task.kind, "title": task.title,
            "form": task.form, "policy": task.policy or {"kind": "any"}, "status": task.status,
            "due_at": _iso(task.due_at), "escalation_level": task.escalation_level, "payload": payload,
            "assignments": [{
                "id": a.id, "principal_kind": a.principal_kind, "principal": a.principal, "source": a.source,
                "status": a.status, "responded_by": a.responded_by, "responded_at": _iso(a.responded_at),
                "decision": (a.response or {}).get("decision"), "verified": a.verified} for a in rows],
            "can_respond": task.status == "pending" and any(_matches(a, principal) for a in rows)
                           and not _voted(task, rows, principal),
            "progress": progress(task, rows),
            "my_response": my_response(rows, principal),
            "outcome": task.outcome, "resolved_by": task.resolved_by, "resolved_at": _iso(task.resolved_at),
            "created_at": _iso(task.created_at),
            # 这条待办上出现的人的显示名（派给谁、谁回复、谁处理、升级给谁）：界面写显示名，悬停给用户名
            "people": {u: names[u] for u in people_of(task, rows) if u in names},
            # 蓝图试运行的待办：派给了发起试运行的人自己，界面上写「以试运行身份同意 / 驳回」
            **({"trial": True} if run is not None and run.origin_kind == TRIAL_ORIGIN else {}),
        })
    return out


def people_of(task: Task, rows: list[TaskAssignment]) -> set[str]:
    """待办上按用户名出现的人：派给的人、回复的人、处理的人。"""
    return ({a.principal for a in rows if a.principal_kind == "user"} | {a.responded_by for a in rows if a.responded_by}
            | ({task.resolved_by} if task.resolved_by else set()))


def visible(item: dict[str, Any], principal: Any) -> bool:
    """这条待办（_describe 的形状）这个人看不看得到：关登录、builder 以上都看得到；user 角色只看派给自己（或自己的组）的，
    撤回了的不算。待办里带着中断内容（报告概况、动作预览），不能谁都能按 id 读。"""
    if getattr(principal, "mode", "off") == "off" or principal.at_least("builder"):
        return True
    return any(a["status"] != "withdrawn" and (
        (a["principal_kind"] == "user" and a["principal"] == principal.username)
        or (a["principal_kind"] == "group" and a["principal"] in (principal.groups or ())))
        for a in item["assignments"])


def my_response(rows: list[TaskAssignment], principal: Any) -> dict[str, Any] | None:
    """这个人在这条待办上的回复（决定、时间、是否登录核实）：处理过的人再打开，写「你已处理：同意（时间）」，不写
    「没有派给你」。认不出是谁（关登录又没署名）的是 None。"""
    name = getattr(principal, "username", None)
    mine = next((a for a in rows if name and a.status == "responded" and a.responded_by == name), None)
    if mine is None:
        return None
    return {"decision": (mine.response or {}).get("decision"), "at": _iso(mine.responded_at), "verified": mine.verified}


def _voted(task: Task, rows: list[TaskAssignment], principal: Any) -> bool:
    name = getattr(principal, "username", None)
    return bool(name) and task.kind in VOTING and any(a.status == "responded" and a.responded_by == name
                                                      for a in rows)


def _node_label(graph: dict[str, Any] | None, node_id: str | None) -> str | None:
    for n in (graph or {}).get("nodes") or []:
        if isinstance(n, dict) and n.get("id") == node_id:
            return ((n.get("data") or {}).get("label")) or node_id
    return None


async def _reconcile_payload(session: AsyncSession, task: Task) -> dict[str, Any]:
    from app.db.models import ActionRecord

    action = await session.get(ActionRecord, (task.correlation_key or "").removeprefix("action:"))
    if action is None:
        return {"kind": "reconcile"}
    return {"kind": "reconcile", "action_id": action.id, "capability": action.capability,
            "environment": action.environment, "payload_hash": action.payload_hash, "preview": action.preview,
            "attempt": action.attempt, "idem_key": action.idem_key, "state": action.state, "error": action.error,
            "request_at": _iso(action.request_at), "options": list(RECONCILE_OUTCOMES)}


def principal_with_actor(principal: Any, actor: str | None) -> Any:
    """关登录时请求的身份没有名字：用自报的署名（X-Actor）补上，回复人一栏才写得出人。"""
    if getattr(principal, "mode", "off") == "off" and not principal.username and actor:
        return replace(principal, username=actor)
    return principal
