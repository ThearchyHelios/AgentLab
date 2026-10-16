"""待办、实例、动作的通知：订阅、拼消息、投递记录（notify_channels / notify_deliveries）。

谁收得到：
- 应用版本 spec.notify 里订阅了这个事件的渠道（[{channel: 渠道 id, events: [...]}]）；
- 范围是 global、自己订阅了这个事件的渠道；
- 待办派给的人自己的渠道（范围是 user、owner_id 是他的用户 id；开了登录才有）。
同一个渠道只推一次。待办改派、升级、催办都是「有一条待办要你处理」，订阅了 task.opened 的渠道一并收到。

消息里只放标题、状态、链接（P4，沿用定时任务 Webhook 的规矩）：不放报告里的数字，待办的标题用节点在图上的名字
（搭图时起的），不用按运行数据渲染出来的那一句。链接要有对外访问地址（环境变量或设置，core/public_url.py）才有。

推送放在后台（spawn）：接收端挂住时超时加重试要几十秒，不能拖着开待办、运行收尾。测试用 drain 等它们推完。

事件只在这里登记（EVENTS，总方案 X6）：每个事件一行，名字、界面上的叫法（前端 lib/terms.ts 的 NOTIFY_TEXT.eventLabel
逐字一致，test_notify_events 对照）、推给哪几条路（scopes：global 全局渠道自己订阅、user 相关的人自己的个人渠道、app 应用
版本 spec.notify 的订阅）。_channels 只走登记了的路，没登记的事件推不出去；接口按 scopes 收订阅，设置页和应用的通知按
GET /api/notify/events 列。scopes 是空的，是登记了、还没有推送的事件：不收订阅、界面上不列——勾了却收不到，就是「看着
生效、其实没生效」（个人渠道勾了实例结束、失败却收不到，04-对外集成与触达 B1，就是这么错的）。本轮第 1 波把六个新事件
一次登记好；实现推送的那一块写自己的 notify_*，只改自己那一行的 scopes，不再改这张表的别处、也不再改前端的叫法表：几块
并行改同一处必然冲突。以前存下的订阅不按 scopes 重新核对（api/notify._validated 只核对新勾的）。

消息的结构，新事件一律照这个写（P4、D9）：标题、状态、条数、链接，外加编号（#前 6 位）和搭建时起的名字（应用、工作流、
节点在图上的名字，题集、数据源、访问密钥的名字）。条数说的是有几件事要处理（几个变化、通过几题、几条待办），可以放。
不放：报告和答案里的数（金额、比例、差值、期望值），表名、列名、取值（码值、样例行），SQL，按运行数据渲染出来的句子。
接收端是群机器人、外部系统，消息出了这个服务就不归遮罩管了。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal, utcnow
from app.db.models import App, AppVersion, NotifyChannel, NotifyDelivery, Run, Task, TaskAssignment, User
from app.notify.core import Message, Outcome, host_of

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Event:
    """一个可订阅的通知事件（见模块说明「事件只在这里登记」）。"""

    #: 界面上的叫法：和前端 NOTIFY_TEXT.eventLabel 同一句
    label: str
    #: 推给哪几条路（_channels）：global 全局渠道自己订阅，user 相关的人自己的个人渠道，app 应用版本 spec.notify 的订阅。
    #: 空的：登记了、还没有推送
    scopes: tuple[str, ...]


_EVERY = ("global", "user", "app")
#: 渠道范围在说法里怎么叫（同前端 NOTIFY_TEXT.scopes）
SCOPE_TEXT = {"global": "全局", "user": "个人", "app": "应用"}

#: 可以订阅的事件，只在这里登记
EVENTS: dict[str, Event] = {
    # 待办派出、改派、升级、催办（_AS）：全局渠道、派给的人自己的个人渠道、应用的订阅
    "task.opened": Event("待办打开", _EVERY),
    # 应用实例结束、失败：个人渠道是发起人自己的（04-对外集成与触达 B1）
    "run.finished": Event("运行结束", _EVERY),
    "run.failed": Event("运行失败", _EVERY),
    # 写入外部系统的结果未知：个人渠道是核实待办派给的人
    "action.unknown": Event("动作结果未知", _EVERY),
    # 个人应用提交审核之后，搭建者发布了或退回了（docs/design/normal-user-apps.md N7）：发给提交人，不归哪个应用
    "review.done": Event("我提交审核的应用有结果", ("global", "user")),
    # ---- 本轮登记、还没有推送（总方案 X6）：实现推送的那一块只改自己这一行的 scopes，消息照模块说明的规矩 ----
    # 数据源有要处理的结构 / 取值变化（03 B3）：只推 severity 为 action 的，按源汇总；源名、各级别条数、链接
    "source.changed": Event("数据源有变化要处理", ()),
    # 口径升版后召回重算，数真变了（02 第 7 项）：答案的主人有 recall 待办；口径名、版本、链接，不放新旧值和差值
    "ledger.recall": Event("口径升版，答案的数变了", ()),
    # 定时评测有题从通过变成不通过 / 评测没跑完（01 第 15 项）：题集名、通过数、退步题数、链接；不放期望值和答案里的数
    "eval.regressed": Event("评测有退步", ()),
    "eval.failed": Event("评测失败", ()),
    # 每天一条待办汇总（04 B3）：只推个人渠道；条数、最早的时限、最多 5 条（节点在图上的名字）、去待办箱的链接
    "task.digest": Event("每天的待办汇总", ()),
    # 访问密钥到期前 7 天、1 天各一次（04 B3）：个人密钥推本人，服务账号的推全局渠道；密钥名、到期日、链接
    "key.expiring": Event("访问密钥快到期", ()),
}
EVENT_LABEL = {name: event.label for name, event in EVENTS.items()}


def subscribe_problem(event: str, scope: str) -> str | None:
    """范围是 scope 的渠道能不能订阅 event：不能的回一句为什么（接口原样回 422），能的是 None。"""
    spec = EVENTS.get(event)
    if spec is None:
        offered = "、".join(e.label for e in EVENTS.values() if scope in e.scopes)
        return f"不认识的通知事件：{event}。可选：{offered}"
    if not spec.scopes:
        return f"「{spec.label}」的通知还没有推送，暂时不能订阅"
    if scope not in spec.scopes:
        return f"「{spec.label}」不推给范围是「{SCOPE_TEXT.get(scope, scope)}」的渠道"
    return None

#: 这几种也是「有一条待办要你处理」，按 task.opened 的订阅推
_AS = {"task.reassigned": "task.opened", "task.escalated": "task.opened", "task.reminded": "task.opened"}
_TASK_HEAD = {"task.opened": "新的待办", "task.reassigned": "待办已改派给你", "task.escalated": "待办已超时，升级处理",
              "task.reminded": "待办催办"}
_KIND_TEXT = {"approve": "审批", "input": "补充输入", "edit": "编辑稿件", "tool": "工具调用审批", "code": "代码执行审批",
              "dual_check": "双盲复算裁决", "action_confirm": "确认写入外部系统", "reconcile": "核实写入结果",
              "signal": "等外部系统回调", "review": "审核个人应用"}
_ENV_TEXT = {"prod": "生产环境", "test": "测试环境", "simulated": "演练"}

_background: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> None:
    """在后台推送。留着引用，免得任务还没跑完就被回收。"""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def drain() -> None:
    """等后台的推送都推完（测试、关停用）。"""
    while _background:
        await asyncio.gather(*list(_background), return_exceptions=True)


async def personal_url_problem(url: str) -> str | None:
    """个人渠道（scope=user，谁都能建）的地址过 SSRF 检查（tools/net.py，不开内网）：内网、回环、元数据地址不发。
    api/notify.py 保存时、send 发送前各查一次（域名可能事后改指向内网）。没问题为 None。

    说法不带检查的细节（解析到了哪个地址、解析失败还是落在内网）：那也是探测内网的信号。"""
    from app.tools.net import UnsafeUrlError, assert_safe_url

    try:
        await assert_safe_url(url)
    except UnsafeUrlError:
        return "个人通知渠道只能发往公网地址（不能是内网、回环或云服务器的元数据地址，域名也要能解析），请改用公网的接收地址"
    return None


def channel_out(row: NotifyChannel) -> dict[str, Any]:
    """渠道的接口形状（typesApp.ts 的 NotifyChannelRow）：地址只回主机名，密钥只说配没配。"""
    from app.core.crypto import decrypt

    url = decrypt(row.url) or ""
    return {"id": row.id, "name": row.name, "scope": row.scope, "owner_id": row.owner_id, "format": row.format,
            "url_host": host_of(url) if url else "", "signed": bool(row.secret), "events": list(row.events or []),
            "enabled": row.enabled}


def generic_body(message: dict[str, Any], msg: Message) -> dict[str, Any]:
    """通用格式的请求体。subject 只有编号，没有内容。"""
    from app.notify.core import text_of

    return {"source": "agentlab", "event": message.get("event"), "title": msg.title, "text": text_of(msg),
            "status": message.get("status"), "subject": message.get("subject") or {},
            "links": [{"label": label, "url": url} for label, url in msg.links], "sent_at": utcnow().isoformat()}


async def _base_url(session: Any) -> str | None:
    """拼链接用的对外访问地址：全站同一个（环境变量优先，其次设置），手里有会话就按库里的值对齐缓存。"""
    from app.core import public_url

    return await public_url.refresh(session)


async def _channels(session: Any, event: str, run: Run | None, users: list[str]) -> list[NotifyChannel]:
    wanted = _AS.get(event, event)
    if (registered := EVENTS.get(wanted)) is None:
        raise ValueError(f"通知事件 {wanted} 没有登记：先在 notify/service.EVENTS 登记，再推送")
    paths = registered.scopes
    found: dict[str, NotifyChannel] = {}
    if "app" in paths and run is not None and run.app_id and run.app_version is not None:
        row = (await session.execute(select(AppVersion.spec, AppVersion.bindings).where(
            AppVersion.app_id == run.app_id, AppVersion.version == run.app_version))).first()
        spec, bindings = (row[0], row[1]) if row is not None else ({}, {})
        # spec 里写的可以是渠道 id，也可以是别名：别名到这个部署的渠道 id 在 bindings.channels 里（spec 可以导出，
        # 渠道是这个部署自己的，同岗位绑定）
        aliases = (bindings or {}).get("channels") or {}
        subscribed: list[str] = []
        for sub in (spec or {}).get("notify") or []:
            if isinstance(sub, dict) and wanted in (sub.get("events") or []):
                target = aliases.get(str(sub.get("channel")), sub.get("channel"))
                subscribed += [str(t) for t in (target if isinstance(target, list) else [target]) if t]
        if subscribed:
            for row in (await session.execute(select(NotifyChannel).where(
                    NotifyChannel.id.in_(subscribed), NotifyChannel.enabled.is_(True)))).scalars():
                found[row.id] = row
    if "global" in paths:
        for row in (await session.execute(select(NotifyChannel).where(
                NotifyChannel.scope == "global", NotifyChannel.enabled.is_(True)))).scalars():
            if wanted in (row.events or []):
                found.setdefault(row.id, row)
    if "user" in paths and users:
        owners = list((await session.execute(select(User.id).where(User.username.in_(users)))).scalars())
        if owners:
            for row in (await session.execute(select(NotifyChannel).where(
                    NotifyChannel.scope == "user", NotifyChannel.owner_id.in_(owners),
                    NotifyChannel.enabled.is_(True)))).scalars():
                if wanted in (row.events or []):
                    found.setdefault(row.id, row)
    return list(found.values())


async def _push(channels: list[NotifyChannel], message: dict[str, Any], *, subject_kind: str, subject_id: str,
                event: str, run_id: str | None, task_id: str | None) -> int:
    from app.notify import send

    for channel in channels:
        try:
            outcome = await send(channel, message)
        except Exception:  # noqa: BLE001 - 拼消息、签名出错也不能让别的渠道跟着失败
            logger.exception("通知渠道「%s」推送出错", channel.name)
            outcome = Outcome(False, None, 0, "推送时出错，详细信息见服务日志")
        async with SessionLocal() as session:
            session.add(NotifyDelivery(channel_id=channel.id, subject_kind=subject_kind, subject_id=subject_id,
                                       event=event, run_id=run_id, task_id=task_id,
                                       status="ok" if outcome.ok else "failed", http_status=outcome.http_status,
                                       attempts=outcome.attempts, error=outcome.error))
            try:
                await session.commit()
            except Exception:  # noqa: BLE001 - 渠道刚被删掉（外键）：推送记录记不下不影响别的
                logger.warning("通知渠道 %s 的推送记录没能保存", channel.id)
    return len(channels)


def _label(graph: dict[str, Any] | None, node_id: str | None) -> str:
    for n in (graph or {}).get("nodes") or []:
        if isinstance(n, dict) and n.get("id") == node_id:
            return str(((n.get("data") or {}).get("label")) or node_id)[:80]
    return str(node_id or "")


async def _where(session: Any, run: Run | None) -> str:
    """消息里「在哪」：应用名，没有应用的写工作流名。"""
    if run is None:
        return ""
    if run.app_id:
        name = await session.scalar(select(App.name).where(App.id == run.app_id))
        if name:
            return f"应用「{name}」"
    return f"工作流「{run.workflow_name}」" if run.workflow_name else ""


async def notify_task(task_id: str, event: str, *, users: list[str]) -> int:
    """待办的通知（task.opened / reassigned / escalated / reminded）。users 是这次要通知到的人（派给的用户名）。返回推了几个渠道。"""
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return 0
        run = await session.get(Run, task.run_id)
        channels = await _channels(session, event, run, users)
        if not channels:
            return 0
        base = await _base_url(session)
        where = await _where(session, run)
        pending = list((await session.execute(select(TaskAssignment).where(
            TaskAssignment.task_id == task_id, TaskAssignment.status == "pending"))).scalars())
        # 不挂在节点上的待办（个人应用的审核）用待办的标题：它按应用名起，不是按运行数据渲染的
        node = _label(run.graph if run else None, task.node_id) if task.node_id else task.title[:80]
    names = "、".join(dict.fromkeys(a.principal or "任何人" for a in pending)) or "任何人"
    lines = [("状态", "待处理"), ("种类", _KIND_TEXT.get(task.kind, task.kind)), ("派给", names[:200])]
    if where:
        lines.insert(0, ("所在", where))
    if task.due_at is not None:
        lines.append(("时限", task.due_at.astimezone().strftime("%Y-%m-%d %H:%M")))
    lines.append(("运行", f"#{task.run_id[:6]}"))
    message = {"event": event, "status": "pending", "title": f"{_TASK_HEAD.get(event, '待办')}：{node}",
               "lines": lines, "links": [("去处理", f"{base}/apps/inbox/{task_id}")] if base else [],
               "subject": {"task_id": task_id, "run_id": task.run_id, "app_id": task.app_id}}
    return await _push(channels, message, subject_kind="task", subject_id=task_id, event=event, run_id=task.run_id,
                       task_id=task_id)


async def notify_run(run_id: str, event: str) -> int:
    """应用实例结束、失败（run.finished / run.failed）。只推应用发起的运行。

    状态那一句取自实例页的说法（apps/resolve.instance_state），两边说同一句话：被驳回的（cancelled + end_reason=rejected）
    走「实例结束」、写「「付款」未确认（被驳回）」；走到「已退回」成果的写「已退回」，不再一律写「已完成」。ok 只在真的
    办成了（code 是 succeeded）时为真。

    发起人是认证过的身份（run.origin.actor_verified）时，他自己订阅了这件事的个人渠道也收得到（「我发起的实例完成 / 失败」，
    04-对外集成与触达 B1）。以前这里给 _channels 的 users 是空的，个人渠道勾了这两项永远收不到。自报的署名不算：关登录
    时谁都能填别人的名字。"""
    from app.apps.resolve import _trails, instance_state

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or not run.app_id:
            return 0
        verified = bool((run.origin or {}).get("actor_verified")) and bool(run.started_by)
        channels = await _channels(session, event, run, [run.started_by] if verified else [])
        if not channels:
            return 0
        base = await _base_url(session)
        where = await _where(session, run)
        state = instance_state(run, (await _trails(session, [run]))[run.id])
    finished = event == "run.finished"
    ok = state["code"] == "succeeded"
    lines = [("状态", state["text"]), ("实例", f"#{run_id[:6]}")]
    if not finished and run.error_node_id and state["code"] == "failed":
        lines.append(("失败节点", f"「{_label(run.graph, run.error_node_id)}」"))
    head = ("已完成" if ok else "已结束") if finished else ("已结束" if state["code"] == "cancelled" else "失败")
    message = {"event": event, "status": run.status, "ok": ok,
               "title": f"{where or '应用'}的实例{head}", "lines": lines,
               "links": [("查看实例", f"{base}/apps/{run.app_id}/i/{run_id}")] if base else [],
               "subject": {"run_id": run_id, "app_id": run.app_id}}
    return await _push(channels, message, subject_kind="run", subject_id=run_id, event=event, run_id=run_id,
                       task_id=None)


async def notify_review(task_id: str, decision: str, *, owner: str, name: str, app_id: str | None) -> int:
    """个人应用提交审核有了结果（review.done，N7）：通知提交人——他自己订阅了这件事的个人渠道，加上订阅了它的全局渠道。
    消息里只有应用名和结论，不放应用里的数（P4）。app_id 是链接过去的应用：发布了的是正式应用，退回的是他的个人应用。"""
    event = "review.done"
    async with SessionLocal() as session:
        channels = await _channels(session, event, None, [owner])
        if not channels:
            return 0
        base = await _base_url(session)
    published = decision == "published"
    message = {"event": event, "status": decision, "ok": published,
               "title": f"提交审核的「{name[:80]}」{'已发布成正式应用' if published else '被退回'}",
               "lines": [("结论", "已发布" if published else "已退回"), ("待办", f"#{task_id[:6]}")],
               "links": [("去看看" if published else "查看我的应用", f"{base}/apps/{app_id}")] if base and app_id else [],
               "subject": {"task_id": task_id, "app_id": app_id}}
    return await _push(channels, message, subject_kind="task", subject_id=task_id, event=event, run_id=None,
                       task_id=task_id)


async def notify_action(action_id: str, event: str) -> int:
    """写入外部系统的结果未知（action.unknown）。有核实待办的链接到待办，没有的（系统按幂等键去查）链接到实例。"""
    from app.db.models import ActionRecord

    async with SessionLocal() as session:
        rec = await session.get(ActionRecord, action_id)
        if rec is None:
            return 0
        run = await session.get(Run, rec.run_id)
        task_id = await session.scalar(select(Task.id).where(Task.correlation_key == f"action:{action_id}",
                                                             Task.kind == "reconcile"))
        users = list((await session.execute(select(TaskAssignment.principal).where(
            TaskAssignment.task_id == task_id, TaskAssignment.principal_kind == "user")))
                     .scalars()) if task_id else []
        channels = await _channels(session, event, run, users)
        if not channels:
            return 0
        base = await _base_url(session)
        where = await _where(session, run)
    lines = [("状态", "等待核实" if task_id else "系统正在按幂等键查询"), ("环境", _ENV_TEXT.get(rec.environment, rec.environment)),
             ("动作", f"#{action_id[:6]}"), ("运行", f"#{rec.run_id[:6]}")]
    if where:
        lines.insert(0, ("所在", where))
    if task_id and base:
        links = [("去核实", f"{base}/apps/inbox/{task_id}")]
    elif base and run is not None and run.app_id:
        links = [("查看实例", f"{base}/apps/{run.app_id}/i/{rec.run_id}")]
    else:
        links = [("查看运行", f"{base}/runs/{rec.run_id}")] if base else []
    message = {"event": event, "status": "unknown", "title": f"写入外部系统的结果未知：{rec.capability}",
               "lines": lines, "links": links,
               "subject": {"action_id": action_id, "run_id": rec.run_id, "task_id": task_id}}
    return await _push(channels, message, subject_kind="action", subject_id=action_id, event=event,
                       run_id=rec.run_id, task_id=task_id)


async def notify_source_changed(source_id: str) -> int:
    """数据源有要处理的变化（source.changed，03 B3 ④、目录-D9）：只推还要人看（开着、知道了还没处理）的「要处理」级变化，
    按源汇总；消息里只有源名、各级别条数和链接，不放表名、列名、取值。探查、剖析任务结束时有新的「要处理」变化才调。

    这一行的 scopes 第 4 波有了「变化」标签页才打开：现在 _channels 推不出去，这里返回 0（照实：没有界面看不了的通知不发）。"""
    from app.data import source_changes
    from app.db.models import DataSource

    event = "source.changed"
    async with SessionLocal() as session:
        row = await session.get(DataSource, source_id)
        if row is None:
            return 0
        counts = await source_changes.counts(session, source_id)
        by = counts["by_severity"]
        if not by.get("action"):
            return 0
        channels = await _channels(session, event, None, [])
        if not channels:
            return 0
        base = await _base_url(session)
    lines = [("数据源", f"「{row.name}」"), ("要处理", f"{by['action']} 条"), ("提醒", f"{by.get('warning', 0)} 条"),
             ("说明", f"{by.get('info', 0)} 条")]
    message = {"event": event, "status": "action", "title": f"数据源「{row.name[:80]}」有 {by['action']} 个变化要处理",
               "lines": lines, "links": [("去看看", f"{base}/data/catalog/{source_id}")] if base else [],
               "subject": {"source_id": source_id}}
    return await _push(channels, message, subject_kind="source", subject_id=source_id, event=event, run_id=None,
                       task_id=None)
