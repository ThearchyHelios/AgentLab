"""待办、实例、动作的通知：订阅、拼消息、投递记录（notify_channels / notify_deliveries）。

谁收得到：
- 应用版本 spec.notify 里订阅了这个事件的渠道（[{channel: 渠道 id, events: [...]}]）；
- 范围是 global、自己订阅了这个事件的渠道；
- 待办派给的人自己的渠道（范围是 user、owner_id 是他的用户 id；开了登录才有）。
同一个渠道只推一次。待办改派、升级、催办都是「有一条待办要你处理」，订阅了 task.opened 的渠道一并收到。

消息里只放标题、状态、链接（P4，沿用定时任务 Webhook 的规矩）：不放报告里的数字，待办的标题用节点在图上的名字
（搭图时起的），不用按运行数据渲染出来的那一句。链接要设了对外访问地址（设置里的 notify）才有。

推送放在后台（spawn）：接收端挂住时超时加重试要几十秒，不能拖着开待办、运行收尾。测试用 drain 等它们推完。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from sqlalchemy import select

from app.db.base import SessionLocal, utcnow
from app.db.models import App, AppVersion, NotifyChannel, NotifyDelivery, Run, Task, TaskAssignment, User
from app.notify.core import Message, Outcome, host_of

logger = logging.getLogger(__name__)

#: 可以订阅的事件
EVENTS = ("task.opened", "run.finished", "run.failed", "action.unknown")
EVENT_LABEL = {"task.opened": "有待办派给我", "run.finished": "实例完成", "run.failed": "实例失败",
               "action.unknown": "写入结果未知"}
#: 这几种也是「有一条待办要你处理」，按 task.opened 的订阅推
_AS = {"task.reassigned": "task.opened", "task.escalated": "task.opened", "task.reminded": "task.opened"}
_TASK_HEAD = {"task.opened": "新的待办", "task.reassigned": "待办已改派给你", "task.escalated": "待办已超时，升级处理",
              "task.reminded": "待办催办"}
_KIND_TEXT = {"approve": "审批", "input": "补充输入", "edit": "编辑稿件", "tool": "工具调用审批", "code": "代码执行审批",
              "dual_check": "双盲复算裁决", "action_confirm": "确认写入外部系统", "reconcile": "核实写入结果",
              "signal": "等外部系统回调"}
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
    from app.api.settings import notify_settings

    return (await notify_settings(session))["public_base_url"]


async def _channels(session: Any, event: str, run: Run | None, users: list[str]) -> list[NotifyChannel]:
    wanted = _AS.get(event, event)
    found: dict[str, NotifyChannel] = {}
    if run is not None and run.app_id and run.app_version is not None:
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
    for row in (await session.execute(select(NotifyChannel).where(
            NotifyChannel.scope == "global", NotifyChannel.enabled.is_(True)))).scalars():
        if wanted in (row.events or []):
            found.setdefault(row.id, row)
    if users:
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
        node = _label(run.graph if run else None, task.node_id)
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
    """应用实例结束、失败（run.finished / run.failed）。只推应用发起的运行。"""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or not run.app_id:
            return 0
        channels = await _channels(session, event, run, [])
        if not channels:
            return 0
        base = await _base_url(session)
        where = await _where(session, run)
    finished = event == "run.finished"
    lines = [("状态", "已完成" if finished else "失败"), ("实例", f"#{run_id[:6]}")]
    if not finished and run.error_node_id:
        lines.append(("失败节点", f"「{_label(run.graph, run.error_node_id)}」"))
    message = {"event": event, "status": run.status, "ok": finished,
               "title": f"{where or '应用'}的实例{'已完成' if finished else '失败'}", "lines": lines,
               "links": [("查看实例", f"{base}/apps/{run.app_id}/i/{run_id}")] if base else [],
               "subject": {"run_id": run_id, "app_id": run.app_id}}
    return await _push(channels, message, subject_kind="run", subject_id=run_id, event=event, run_id=run_id,
                       task_id=None)


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
