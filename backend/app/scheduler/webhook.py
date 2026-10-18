"""定时任务的通知：运行结束、失败、停在审批（或因服务重启挂起、写入外部系统的结果未知）时按任务的设置推送 Webhook。

格式、签名、发送、脱敏抽到了 app/notify/core.py，待办、实例、动作的通知（app/notify/service.py）共用同一份。这里留
定时任务自己的部分：消息内容、每一拍的通知扫描、推送记录（webhook_deliveries，不迁移，F14）。原来在这里的名字照原名
再导出，调用方和测试不用改；_transport、RETRY_DELAYS 仍是这个模块自己的（测试照旧替换它们），send 每次调用时读。

通知里只有任务名、状态、出具档位、缺口条数、运行链接、封存哈希（界面上叫清单哈希）前 12 位和起止时间，失败时
另写失败节点的名字和下一步（不许诺能继续运行）；被人驳回的那一期按「运行结束」推「未出具（被驳回）」、写驳回的那一步，
手动停止的不推；运行成功结束后和同一任务上一次成功的运行的对比（auto_compare）只写
各类指标的个数和对比页链接。不放报告里的具体数字，也不放失败时的原始报错——报错里可能带着查询到的数据，Webhook
推去的群不一定有权看。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import and_, func, or_, select, update

from app.core.crypto import decrypt
from app.core.events import EventType
from app.db.base import SessionLocal, utcnow
from app.db.models import ActionRecord, Approval, Run, RunEvent, Schedule, ScheduleFire, WebhookDelivery
from app.engine.labels import ISSUANCE_LABEL
from app.notify import core
# 抽到 app/notify/core.py 的名字照原名再导出
from app.notify.core import (  # noqa: F401
    _BOTS, _PLATFORM_HOSTS, _SENDING, FORMAT_LABEL, FORMATS, TIMEOUT_S, Outcome, _platform_error,
    _platform_message, _RedactWebhookUrls, _reply_json, _sensitive, bot_format, check_url, format_problem, host_of,
    url_hint,
)
from app.notify.core import scrub as _scrub  # noqa: F401
from app.scheduler import auto_compare, service

logger = logging.getLogger(__name__)

EVENTS = ("finished", "failed", "awaiting_approval")
#: 事件在报错里的叫法，和界面上的选项一致
EVENT_LABEL = {"finished": "运行结束", "failed": "运行失败", "awaiting_approval": "等待审批"}
#: 失败后再试 3 次，每次之前等这么久（秒）。测试里换成 (0, 0, 0)
RETRY_DELAYS: tuple[float, ...] = core.RETRY_DELAYS
#: 只看这么多天内的触发：更早结束的运行状态不会再变，不用每一拍都扫。例外见 notify_due：停在审批上、有待审批的不受
#: 这个限制，运行记录这些天内有过变化的也照扫——审批可以等上一个多月，批完接着执行、停到下一个审批点或跑完都要推
SCAN_DAYS = 30
#: 运行结束后等封存（清单哈希）最多等多久。runner 先写终态再封存，两步之间被这一拍撞上就等下一拍
SEAL_WAIT_S = 60
#: 测试用：换成 httpx.MockTransport 接住请求
_transport: httpx.AsyncBaseTransport | None = None

_HEADLINE = {"finished": "运行完成", "failed": "运行失败", "awaiting_approval": "等待审批"}
#: 运行状态在通知里的叫法，和界面一致。interrupted 分两种，见 _status_label
_STATUS_LABEL = {"succeeded": "已完成", "failed": "失败", "cancelled": "已取消", "failed_to_start": "未能发起"}
#: 挂起的运行要人去处理：不处理，这个任务之后的触发都按「上一次还没结束」跳过
SUSPENDED_REASON = "运行停在断点上、没有待审批，通常是服务重启所致"
SUSPENDED_ACTION = "请在 AgentLab 的「记录」中继续运行或放弃这次运行；它结束之前，这个定时任务之后的触发都会跳过"
#: 写外部系统的结果未知、挂起等核实的运行（app/process/actions.py）：核实之后系统自动接着跑，不用人去点继续运行
ACTION_PENDING_REASON = "运行中有一次写入外部系统的结果未知，已挂起等待核实"
ACTION_PENDING_ACTION = ("请在 AgentLab 的「应用」中处理核实待办（登记了查询能力的由系统自动核实）；核实之后运行会自动继续，"
                         "它结束之前，这个定时任务之后的触发都会跳过")
#: 失败的运行怎么处理：不许诺「修正后可继续运行」。开始执行过的也可能没有断点（执行第一个节点前就失败）或已不兼容
#: （系统升级），能不能继续以记录页的提示为准。「记录」是导航里那一页的名字，和挂起那句一致
FAILED_ACTION = "请在 AgentLab 的「记录」中查看失败原因"
#: 从没开始执行就失败的（服务重启时还在排队，被改成失败、没有开始时间）：没有可继续的进度。不照抄运行记录上的报错——
#: 通知一律不放原始报错，这一句是按「没有开始时间」认出来的，自己写
NOT_STARTED_REASON = "运行在开始执行前就已中止（通常是服务重启时仍在排队），没有可继续的进度"
#: 失败的运行不挡之后的触发（挂起的才挡）：任务还在排期上时补这一句，免得人以为这个定时任务就此停了
NEXT_AS_USUAL = "下一次触发会照常发起"
#: 从没开始执行、任务又不在排期上（停用了）：原因刚写过，记录页上也没有更多可看的，下一步是重新发起。「立即运行」
#: 是定时任务页上那个按钮的名字，停用的任务也能点
RERUN_ACTION = "如需重新发起，请在 AgentLab 的「定时任务」中点击「立即运行」"
#: 被人驳回的那一期（cancelled + end_reason=rejected，next-round X9）：是人的决定，不是故障。按「运行结束」推（订阅
#: 「运行失败」的人盯的是故障），标题、状态照实写被驳回；驳回的备注不写（可能带着数），驳回的是哪一步写节点名
REJECTED_HEADLINE = "未出具（被驳回）"
REJECTED_STATUS = "被驳回"
REJECTED_ACTION = "驳回的说明请在 AgentLab 的「记录」中查看"


# --------------------------------------------------------------------------
# 消息内容
# --------------------------------------------------------------------------


@dataclass
class Notice:
    """一条通知要说的事。event 是 finished / failed / awaiting_approval / test；status 是运行状态，没能发起的是
    failed_to_start。没有的项为 None。

    suspended：运行停在断点上、没有待审批（通常是服务重启挂起的），按 failed 事件推。error_node：失败节点的名字。
    next_fire：任务启用着、往后还会触发（失败通知据此补「下一次触发会照常发起」）。
    compare：和同一定时任务上一次成功的运行的对比摘要（auto_compare.compare_fire 的返回：基准是哪一次、只有个数的
    summary、基准是不是紧挨着的上一次、基准开始的时间），compare_url：对比页的链接。
    fmt、signed 只给测试消息用：说明里只说这条消息证明得了的事。"""

    event: str
    schedule_id: str
    schedule_name: str
    timezone: str
    fire_id: str | None = None
    fire_at: datetime | None = None
    fire_kind: str | None = None
    run_id: str | None = None
    status: str | None = None
    tier: str | None = None
    gaps: int | None = None
    run_url: str | None = None
    manifest_hash: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reason: str | None = None
    pending_approvals: int | None = None
    suspended: bool = False
    #: 挂起的原因是写外部系统的结果未知（等核实，核实后自动接着跑），不是服务重启。只在 suspended 时有意义
    action_pending: bool = False
    error_node: str | None = None
    #: 运行结束在 cancelled 的原因（Run.end_reason）：rejected 时按「未出具（被驳回）」说，error_node 是驳回的那一步
    end_reason: str | None = None
    next_fire: bool = False
    #: 事件触发的那一次（fire_kind=event）：事件的说法（scheduler/triggers.event_text），不含出口字段的值、变化的内容
    trigger_text: str | None = None
    compare: dict[str, Any] | None = None
    compare_url: str | None = None
    fmt: str | None = None
    signed: bool = False

    @property
    def rejected(self) -> bool:
        return self.status == "cancelled" and self.end_reason == "rejected"

    @property
    def never_started(self) -> bool:
        """失败的运行从没开始执行（没有开始时间）：服务重启时还在排队、被改成失败的就是这种，没有断点可续。"""
        return self.status == "failed" and self.run_id is not None and self.started_at is None


def _local(moment: datetime | None, tz: str, *, seconds: bool = True) -> str | None:
    if moment is None:
        return None
    try:
        from app.scheduler import cron

        local = moment.astimezone(cron.zone(tz))
    except ValueError:
        local = moment.astimezone(timezone.utc)
    return local.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def message(notice: Notice) -> tuple[str, list[tuple[str, str]], str | None]:
    """(标题, [(项, 内容)…], 运行链接)。三种文本格式都从这一份排出来。"""
    if notice.event == "test":
        return f"定时任务「{notice.schedule_name}」Webhook 测试", [("说明", _test_text(notice))], None
    if notice.status == "failed_to_start":
        headline = "未能发起运行"
    elif notice.suspended:
        headline = "运行已挂起"
    elif notice.rejected:
        headline = REJECTED_HEADLINE
    else:
        headline = _HEADLINE.get(notice.event, notice.event)
    title = f"定时任务「{notice.schedule_name}」{headline}"
    lines: list[tuple[str, str]] = []
    if notice.status:
        lines.append(("状态", _status_label(notice)))
    if notice.tier:
        gaps = f"（缺口 {notice.gaps} 条）" if notice.gaps is not None else ""
        lines.append(("出具档位", f"{ISSUANCE_LABEL.get(notice.tier, notice.tier)}{gaps}"))
    if notice.compare:
        # 只给各类指标的个数：具体的数在对比页上，跟着运行的权限走，不随消息推进群里
        lines.append((_compare_head(notice), auto_compare.summary_text(notice.compare.get("summary") or {})))
    if notice.pending_approvals:
        lines.append(("待审批", f"{notice.pending_approvals} 项，请到 AgentLab 处理；运行结束之前，"
                                "这个定时任务之后的触发都会跳过"))
    if notice.status == "failed_to_start" and notice.reason:
        lines.append(("原因", notice.reason))
    elif notice.suspended and notice.action_pending:
        lines += [("原因", ACTION_PENDING_REASON), ("处理", ACTION_PENDING_ACTION)]
    elif notice.suspended:
        lines += [("原因", SUSPENDED_REASON), ("处理", SUSPENDED_ACTION)]
    elif notice.rejected:
        if notice.error_node:
            lines.append(("驳回节点", f"「{notice.error_node}」"))
        lines.append(("处理", f"{REJECTED_ACTION}；{NEXT_AS_USUAL}" if notice.next_fire else REJECTED_ACTION))
    elif notice.status == "failed":
        # 不写原始报错（可能带着查询到的数据），写失败节点的名字和下一步
        if notice.error_node:
            lines.append(("失败节点", f"「{notice.error_node}」"))
        if notice.never_started:
            # 原因刚写过，也没有断点可续：不再让人去查看原因，说下一次照常，不在排期上的说怎么重新发起
            lines += [("原因", NOT_STARTED_REASON), ("处理", NEXT_AS_USUAL if notice.next_fire else RERUN_ACTION)]
        else:
            lines.append(("处理", f"{FAILED_ACTION}；{NEXT_AS_USUAL}" if notice.next_fire else FAILED_ACTION))
    if notice.fire_at is not None:
        when = f"{_local(notice.fire_at, notice.timezone, seconds=False)}（{notice.timezone}）"
        # 立即运行不写「计划触发」：群里的人会以为报告按时出了
        if notice.fire_kind == "manual":
            lines.append(("手动触发", when))
        elif notice.fire_kind == "event":
            # 事件触发没有「计划」：写事件和合成这次触发的时刻（去抖结束的那一刻）
            lines.append(("事件触发", f"{notice.trigger_text}，{when}" if notice.trigger_text else when))
        elif notice.fire_kind == "misfire":
            lines.append(("计划触发", f"{when}，错过后补发"))
        else:
            lines.append(("计划触发", when))
    if notice.started_at is not None:
        lines.append(("开始", _local(notice.started_at, notice.timezone) or ""))
    if notice.finished_at is not None:
        lines.append(("结束", _local(notice.finished_at, notice.timezone) or ""))
    if notice.manifest_hash:
        lines.append(("清单哈希", notice.manifest_hash[:12]))
    if notice.run_id:
        # 和界面上的短编号一样写（#前 6 位）；完整 id 在运行链接里
        lines.append(("运行", f"#{notice.run_id[:6]}"))
    return title, lines, notice.run_url


def _compare_head(notice: Notice) -> str:
    """对比那一行的项名。基准是紧挨着的上一次时写「与上一次相比」；不是（上一次失败了、取消了）时写明比的是哪一次：
    短编号（同界面，#前 6 位）和开始的日期（按定时任务的时区）。"""
    compare = notice.compare or {}
    if compare.get("previous", True):
        return "与上一次相比"
    when = _date_of(compare.get("base_started_at"), notice.timezone)
    return f"与上一次成功的运行（#{str(compare.get('base_run_id') or '')[:6]}{f'，{when}' if when else ''}）相比"


def _date_of(text: Any, tz: str) -> str | None:
    """ISO 时间（对比结果里运行的开始时间）按定时任务的时区写成日期，读不出来的返回 None。"""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (_local(moment, tz, seconds=False) or "")[:10] or None


def _status_label(notice: Notice) -> str:
    if notice.status == "interrupted":
        if notice.pending_approvals:
            return "等待审批"
        return "已挂起，等待核实" if notice.action_pending else "已挂起，可继续运行"
    if notice.rejected:
        return REJECTED_STATUS
    return _STATUS_LABEL.get(notice.status or "", notice.status or "")


def _test_text(notice: Notice) -> str:
    """测试消息的说明只说收到它证明得了的事：飞书、钉钉的机器人开了签名校验时，验签不过就不收；通用格式的接收端验没
    验签这边不知道；企业微信不签名，填了密钥也不提。"""
    if notice.signed and notice.fmt in ("feishu", "dingtalk"):
        return "这是一条测试消息。收到即表示 Webhook 地址可用；机器人开启了签名校验的，签名密钥也配置正确"
    if notice.signed:
        return "这是一条测试消息。收到即表示 Webhook 地址可用；请求头带有签名，接收端可据此核对签名密钥"
    return "这是一条测试消息。收到即表示 Webhook 地址配置正确"


def _links(notice: Notice) -> list[tuple[str, str]]:
    """消息末尾的链接：运行，有对比时再加对比页。没设对外访问地址时都没有。"""
    return [(label, url) for label, url in (("查看运行", notice.run_url), ("查看对比", notice.compare_url)) if url]


def _message(notice: Notice) -> core.Message:
    title, lines, _ = message(notice)
    # 被驳回的那一期按 finished 推，但没出具：不画成成功的颜色
    return core.Message(title=title, lines=lines, links=_links(notice),
                        ok=notice.event in ("finished", "test") and not notice.rejected)


def _text(notice: Notice) -> str:
    return core.text_of(_message(notice))


def _wecom_markdown(notice: Notice) -> str:
    return core.wecom_markdown(_message(notice))


def _dingtalk_markdown(notice: Notice) -> tuple[str, str]:
    return core.dingtalk_markdown(_message(notice))


def _iso(moment: datetime | None) -> str | None:
    return service.as_utc(moment).isoformat() if moment is not None else None


def _generic(notice: Notice, now: datetime) -> dict[str, Any]:
    title, _, _ = message(notice)
    return {
        "source": "agentlab",
        "event": notice.event,
        "title": title,
        "text": _text(notice),
        "schedule_id": notice.schedule_id,
        "schedule_name": notice.schedule_name,
        "fire_id": notice.fire_id,
        "fire_at": _iso(notice.fire_at),
        "fire_kind": notice.fire_kind,
        "run_id": notice.run_id,
        "status": notice.status,
        "tier": notice.tier,
        "tier_label": ISSUANCE_LABEL.get(notice.tier) if notice.tier else None,
        "gaps": notice.gaps,
        "pending_approvals": notice.pending_approvals,
        "run_url": notice.run_url,
        "manifest_hash": notice.manifest_hash[:12] if notice.manifest_hash else None,
        "started_at": _iso(notice.started_at),
        "finished_at": _iso(notice.finished_at),
        "reason": (notice.reason if notice.status == "failed_to_start"
                   else f"{ACTION_PENDING_REASON}。{ACTION_PENDING_ACTION}" if notice.suspended and notice.action_pending
                   else f"{SUSPENDED_REASON}。{SUSPENDED_ACTION}" if notice.suspended
                   else NOT_STARTED_REASON if notice.never_started else None),
        "error_node": notice.error_node,
        # 对比只给基准是哪一次（是不是紧挨着的上一次）、各类指标的个数和对比页链接
        "compare": ({"base_run_id": notice.compare.get("base_run_id"),
                     "summary": dict(notice.compare.get("summary") or {}),
                     "previous": notice.compare.get("previous", True) is not False, "url": notice.compare_url}
                    if notice.compare else None),
        "sent_at": _iso(now),
    }


def build_request(fmt: str, url: str, secret: str | None, notice: Notice, *,
                  now: datetime | None = None) -> tuple[str, dict[str, str], bytes]:
    """(请求地址, 请求头, 请求体)。签名里的时间戳按 now 取，每次重试重新算：飞书要求时间戳在一小时内。"""
    now = now or utcnow()
    return core.build_request(fmt, url, secret, _message(notice), generic=_generic(notice, now), now=now)


# --------------------------------------------------------------------------
# 推送
# --------------------------------------------------------------------------


async def send(fmt: str, url: str, secret: str | None, notice: Notice, *, retry: bool = True) -> Outcome:
    """推送一条通知。retry 时失败再试 3 次（退避），4xx（429 除外）不重试。失败只记主机名。

    _transport、RETRY_DELAYS 在调用时从这个模块读：测试替换的是它们。"""
    return await core.post(fmt, url, secret, lambda: build_request(fmt, url, secret, notice),
                           label=f"定时任务「{notice.schedule_name}」的 Webhook", retry=retry, transport=_transport,
                           delays=RETRY_DELAYS)


async def _record(schedule_id: str, fire_id: str | None, run_id: str | None, event: str, outcome: Outcome) -> None:
    async with SessionLocal() as session:
        session.add(WebhookDelivery(schedule_id=schedule_id, fire_id=fire_id, run_id=run_id, event=event,
                                    status="ok" if outcome.ok else "failed", http_status=outcome.http_status,
                                    attempts=outcome.attempts, error=outcome.error))
        try:
            await session.commit()
        except Exception:  # noqa: BLE001 - 任务刚被删掉（外键）之类：推送记录记不下不影响别的
            logger.warning("定时任务 %s 的推送记录没能保存", schedule_id)


# --------------------------------------------------------------------------
# 每一拍的通知扫描
# --------------------------------------------------------------------------

#: 运行到了这些状态才可能要推送
_WATCHED = ("succeeded", "failed", "cancelled", "interrupted")
#: 结束的运行 → 事件。cancelled 是有人手动停的，不推；被驳回的（end_reason=rejected）按 finished 推，见 _event_of。
#: interrupted 分停在审批和挂起两种，见 _state_of
_EVENT_OF = {"succeeded": "finished", "failed": "failed", "cancelled": None}


def _event_of(run: Run) -> str | None:
    if run.status == "cancelled" and run.end_reason == "rejected":
        return "finished"
    return _EVENT_OF[run.status]


@dataclass
class _Job:
    fmt: str
    url: str
    secret: str | None
    notice: Notice


@dataclass
class _Due:
    """挑出来的一次状态变化接下来要做的事：运行成功结束时和上一次成功的运行比一次（compare_run），按条件 UPDATE
    认领（把 notified_status 从 old 改成 state），认领到了、订阅了这个事件时推一条（job）。都在锁外做：对比要读快照、
    重算，推送要等接收端，都不能挡着发起。pushed：认领到了、发出了推送（含失败的）。"""

    fire_id: str
    old: str | None
    state: str
    compare_run: str | None
    job: _Job | None
    pushed: bool = False


async def notify_due(now: datetime | None = None) -> int:
    """查已发起的触发，运行状态有变化的按订阅推送一次。返回这一拍推送了几条（含失败的）。

    处理过的状态记在 notified_status 上，用条件 UPDATE 认领（WHERE notified_status = 旧值）：两拍撞在一起也只
    推一次。没订阅这个事件、任务没配 Webhook 的也记下已处理——之后再配上 Webhook，不会把旧的补推一遍。
    停在断点上的运行每一拍都要重新看（见 _state_of）：它的已处理状态带着第几段执行，和运行状态本身对不上。

    运行成功结束、封存完的那一次，认领之前先和同一定时任务上一次成功的运行比一次（auto_compare），结束的通知带上计数
    摘要和对比页链接。没配 Webhook 的也比：触发历史里要用。对比在锁外、认领之前做，最多等 auto_compare.WAIT_S 秒，
    过了就不带对比推送：一次慢的对比最多让这一拍晚这么久，不会把之后所有的通知一直压着，也不拖着发起用的锁。比到一半
    扫描被取消（停机只等通知扫描一个宽限），这次触发还没认领，下次照常比、照常推——以前先认领、后比，取消之后这条
    通知再也不会推。比不了、出错不影响推送。
    """
    now = now or utcnow()
    dues: list[_Due] = []
    base_url: str | None = None
    cutoff = now - timedelta(days=SCAN_DAYS)
    # 扫描窗口按触发记录的创建时间算，两种例外：运行记录窗口内有过变化（运行状态一变 updated_at 就跟着变），以及
    # 停在审批上、有待审批的——以前只看触发记录，创建 30 天以后才停到审批上的运行收不到「等待审批」
    waiting = select(Approval.id).where(Approval.run_id == Run.id, Approval.status == "pending").exists()
    # 和发起共用一把锁：发起时先记一条「未能发起」再发起运行、成功了才改成 started，扫描不能撞在这中间。锁里只挑出
    # 状态有变化的、拼好要推的消息；认领在锁外按条件 UPDATE 做，挑出来的是锁里看到的状态，和别的扫描撞上也只有一个
    # 认领得到
    async with service.launch_lock():
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(ScheduleFire, Run)
                .outerjoin(Run, Run.id == ScheduleFire.run_id)
                .where(or_(ScheduleFire.created_at >= cutoff, Run.updated_at >= cutoff,
                           and_(Run.status == "interrupted", waiting)), or_(
                    and_(ScheduleFire.status == "failed_to_start", ScheduleFire.notified_status.is_(None)),
                    and_(ScheduleFire.status == "started", Run.status.in_(_WATCHED),
                         or_(ScheduleFire.notified_status.is_(None), ScheduleFire.notified_status != Run.status)),
                ))
                .order_by(ScheduleFire.created_at)
            )).all()
            if rows:
                # 链接里的地址：全站同一个对外访问地址（环境变量优先，其次设置，core/public_url.py）
                from app.core import public_url

                base_url = await public_url.refresh(session)
            for fire, run in rows:
                seen = await _state_of(session, fire, run, now)
                if seen.state is None or seen.state == fire.notified_status:
                    continue
                schedule = await session.get(Schedule, fire.schedule_id)
                url = decrypt(schedule.webhook_url) if schedule and schedule.webhook_url else None
                job = None
                if seen.event and url and schedule.webhook_format in FORMATS \
                        and seen.event in (schedule.webhook_events or []):
                    notice = await _notice(session, schedule, fire, run, seen, base_url)
                    job = _Job(schedule.webhook_format, url, decrypt(schedule.webhook_secret), notice)
                # 只比成功结束、封存完的（见 auto_compare）：失败、取消的指标不全，等不来新封存的比了也是旧的
                compare_run = run.id if run is not None and run.status == "succeeded" and seen.sealed else None
                dues.append(_Due(fire.id, fire.notified_status, seen.state, compare_run, job))
    if dues:
        await asyncio.gather(*(_follow_up(due, base_url) for due in dues))
    return sum(1 for due in dues if due.pushed)


async def _follow_up(due: _Due, base_url: str | None) -> None:
    """先比（需要的话），再认领，认领到了再推：对比摘要要写进这条通知；认领放在对比之后，比到一半被取消时这次触发
    还没认领。对比自己兜住了超时和异常，出什么事都不挡推送。"""
    brief = await auto_compare.compare_fire(due.fire_id, due.compare_run) if due.compare_run else None
    if not await _mark(due.fire_id, due.old, due.state) or due.job is None:
        return
    if brief is not None:
        due.job.notice.compare = brief
        if base_url:
            due.job.notice.compare_url = auto_compare.page_url(base_url, due.compare_run, brief["base_run_id"])
    due.pushed = True
    await _deliver(due.job)


@dataclass
class _Seen:
    """_state_of 的结论。state：要记下的已处理状态，还不能下结论时为 None；event：要推送的事件；pending：待审批数；
    sealed：终态的运行封存完了（封存盖住了这一次的结束事件，见 auto_compare.sealed_now），只有这时才比、通知里才写
    清单哈希。"""

    state: str | None
    event: str | None = None
    pending: int = 0
    sealed: bool = False
    action_pending: bool = False


async def _state_of(session: Any, fire: ScheduleFire, run: Run | None, now: datetime) -> _Seen:
    """这次触发现在该记成什么状态、推什么。还不能下结论（没封存、审批刚批完还没接着执行）时状态为 None。

    停在断点上（interrupted）的分两种，各推一次：
    - 有待审批：停在审批，推 awaiting_approval。批完接着执行、又停在下一个审批点时要再推，所以已处理状态里带着这是
      第几段执行（每段执行从一条 run.started 开始），同一个审批点不重复推。
    - 没有待审批：通常是服务重启挂起的。它不会自己接着执行，这个任务之后的触发都会按「上一次还没结束」跳过，
      不说一声任务就此静默停摆——按 failed 推一次，写明要去继续运行或放弃。
      其中写外部系统的结果未知、挂起等核实的（动作挂起，app/process/actions.py）另记一种状态：处理办法不一样（去核实，
      核实后自动接着跑，点继续运行会被拒），说明也不一样。
    审批批完到运行改回 running 之间，审批是 answered、还没有 resolved：那不是挂起，等下一拍。
    """
    if fire.status == "failed_to_start":
        return _Seen("failed_to_start", "failed")
    if run is None:
        return _Seen(None)
    if run.status in ("succeeded", "failed", "cancelled"):
        # runner 先写终态和结束时间、再另起会话封存，两步之间被这一拍撞上就等。失败后接着跑完的运行这时还带着失败时
        # 的清单哈希，不能只看有没有哈希，要看封存盖没盖住这一次的结束事件。没有结束时间的（服务重启时还在排队、被改
        # 成 failed 的）永远不会封存：按它最后一次更新的时间等，以前按「现在」等，每一拍都等、永远不推。等过时限仍没
        # 封存完的照常推，只是不比、不写清单哈希
        sealed = await auto_compare.sealed_now(session, run)
        since = run.finished_at or run.updated_at or fire.created_at
        if not sealed and (now - service.as_utc(since)).total_seconds() < SEAL_WAIT_S:
            return _Seen(None)
        return _Seen(run.status, _event_of(run), sealed=sealed)
    if run.status != "interrupted":
        return _Seen(None)
    counts = dict((await session.execute(
        select(Approval.status, func.count()).where(
            Approval.run_id == run.id, Approval.status.in_(("pending", "answered")))
        .group_by(Approval.status))).all())
    segment = await session.scalar(select(func.count()).select_from(RunEvent).where(
        RunEvent.run_id == run.id, RunEvent.type == EventType.RUN_STARTED.value)) or 0
    if counts.get("pending"):
        return _Seen(f"interrupted:approval:{segment}", "awaiting_approval", counts["pending"])
    if counts.get("answered"):
        return _Seen(None)
    if await session.scalar(select(func.count()).select_from(ActionRecord).where(
            ActionRecord.run_id == run.id, ActionRecord.state.in_(("sent", "unknown")))):
        return _Seen(f"interrupted:action:{segment}", "failed", action_pending=True)
    return _Seen(f"interrupted:held:{segment}", "failed")


async def _mark(fire_id: str, old: str | None, state: str) -> bool:
    cond = ScheduleFire.notified_status.is_(None) if old is None else ScheduleFire.notified_status == old
    async with SessionLocal() as session:
        result = await session.execute(update(ScheduleFire).where(ScheduleFire.id == fire_id, cond)
                                       .values(notified_status=state))
        await session.commit()
        return result.rowcount == 1


async def _notice(session: Any, schedule: Schedule, fire: ScheduleFire, run: Run | None, seen: _Seen,
                  base_url: str | None) -> Notice:
    tier, gaps = await _issuance_of(session, run) if run is not None else (None, None)
    pending = seen.pending
    return Notice(
        event=seen.event or "", schedule_id=schedule.id, schedule_name=schedule.name, timezone=schedule.timezone,
        fire_id=fire.id, fire_at=fire.fire_at, fire_kind=fire.kind,
        run_id=run.id if run else None,
        status=run.status if run else fire.status,
        tier=tier, gaps=gaps,
        run_url=f"{base_url}/runs/{run.id}" if base_url and run else None,
        # 只写这一次结束时封的：停在审批上、封存还没补上的运行，身上可能还留着上一次失败时的清单哈希
        manifest_hash=run.manifest_hash if run is not None and seen.sealed else None,
        started_at=run.started_at if run else None,
        finished_at=run.finished_at if run else None,
        reason=fire.reason if fire.status == "failed_to_start" else None,
        pending_approvals=pending or None,
        suspended=run is not None and run.status == "interrupted" and not pending,
        action_pending=seen.action_pending,
        error_node=_failed_node(run) if run is not None and (run.status == "failed" or run.end_reason == "rejected")
        else None,
        end_reason=run.end_reason if run is not None else None,
        # 事件触发的任务没有下一次「到点」，启用着就是下一次事件来了照常发起
        next_fire=bool(schedule.enabled and (schedule.next_run_at is not None or schedule.trigger_kind == "event")),
        trigger_text=_trigger_text(fire),
    )


def _trigger_text(fire: ScheduleFire) -> str | None:
    event = fire.inputs.get("event") if isinstance(fire.inputs, dict) else None
    text = event.get("text") if isinstance(event, dict) else None
    return text if isinstance(text, str) and text else None


def _failed_node(run: Run) -> str | None:
    """失败节点在图上的名字（没有名字的给节点 id）。节点名是搭图时起的，不带查询到的数据。"""
    if not run.error_node_id:
        return None
    for node in (run.graph or {}).get("nodes") or []:
        if isinstance(node, dict) and node.get("id") == run.error_node_id:
            label = (node.get("data") or {}).get("label") if isinstance(node.get("data"), dict) else None
            return str(label or run.error_node_id)[:80]
    return None


async def _issuance_of(session: Any, run: Run) -> tuple[str | None, int | None]:
    """出具档位和缺口条数：成果上的 _issuance，取不到时退回最后一条 issuance 事件（同 api/evidence.py 的读法）。"""
    issuance = (run.output or {}).get("_issuance") if isinstance(run.output, dict) else None
    if not isinstance(issuance, dict):
        issuance = await session.scalar(select(RunEvent.data).where(
            RunEvent.run_id == run.id, RunEvent.type == "issuance").order_by(RunEvent.seq.desc()).limit(1))
    if not isinstance(issuance, dict) or not issuance.get("tier"):
        return None, None
    gaps = issuance.get("gaps")
    return str(issuance["tier"]), len(gaps) if isinstance(gaps, list) else None


async def _deliver(job: _Job) -> None:
    try:
        outcome = await send(job.fmt, job.url, job.secret, job.notice)
    except Exception:  # noqa: BLE001 - 拼消息、签名出错也不能让这一拍的其他推送跟着失败
        logger.exception("定时任务「%s」的 Webhook（%s）推送出错", job.notice.schedule_name, host_of(job.url))
        outcome = Outcome(False, None, 0, "推送时出错，详细信息见服务日志")
    await _record(job.notice.schedule_id, job.notice.fire_id, job.notice.run_id, job.notice.event, outcome)


async def send_test(schedule_id: str) -> dict[str, Any]:
    """按任务存的 Webhook 发一条测试消息（不重试），记一条 event=test 的推送记录。任务不存在抛 KeyError，
    没配 Webhook、地址解不开抛 ValueError。

    返回 {ok, http_status, message}。message 是 ok、http_status 之外的补充说明：失败时是原因（只有主机名），送达时
    提醒去接收端看一眼。界面在「测试消息已送达（HTTP 200）」「测试消息发送失败」后面接它，这里不再重复那半句。"""
    async with SessionLocal() as session:
        schedule = await session.get(Schedule, schedule_id)
        if schedule is None:
            raise KeyError(schedule_id)
        if not schedule.webhook_url or schedule.webhook_format not in FORMATS:
            raise ValueError("这个定时任务没有配置 Webhook，请先填写地址并保存")
        url = decrypt(schedule.webhook_url)
        if not url:
            raise ValueError("Webhook 地址无法解密（加密密钥可能已更换），请重新填写地址并保存")
        fmt, secret = schedule.webhook_format, decrypt(schedule.webhook_secret)
        notice = Notice(event="test", schedule_id=schedule.id, schedule_name=schedule.name,
                        timezone=schedule.timezone, fmt=fmt, signed=bool(secret) and fmt != "wecom")
    outcome = await send(fmt, url, secret, notice, retry=False)
    await _record(schedule_id, None, None, "test", outcome)
    if outcome.ok:
        return {"ok": True, "http_status": outcome.http_status, "message": "请到接收端确认已收到"}
    return {"ok": False, "http_status": outcome.http_status, "message": outcome.error or "发送失败"}
