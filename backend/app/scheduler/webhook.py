"""定时任务的通知：运行结束、失败、停在审批时按任务的设置推送 Webhook。

四种格式：通用 JSON、飞书（文本消息）、企业微信（markdown）、钉钉（markdown）。配了签名密钥时：通用格式在请求头
里带 HMAC-SHA256 签名，飞书、钉钉按各自平台的算法签（飞书放在请求体，钉钉放在地址上）；企业微信的 key 本来就在
地址里，没有另外的签名。

Webhook 地址本身就是凭证（企业微信的 key、钉钉的 access_token 都在地址里，飞书的在路径里），和签名密钥一样
加密落库，日志、推送记录、接口返回里只出现主机名。httpx 每发一个请求都会在 INFO 级别记一行完整地址，这里装了
一个过滤器把推送期间的那几行换成主机名。

通知里只有任务名、状态、出具档位、缺口条数、运行链接、封存哈希前 12 位和起止时间，不放报告里的具体数字，也不放
失败时的原始报错——报错里可能带着查询到的数据，Webhook 推去的群不一定有权看。
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit

import httpx
from sqlalchemy import and_, func, or_, select, update

from app.core.crypto import decrypt
from app.db.base import SessionLocal, utcnow
from app.db.models import Approval, Run, RunEvent, Schedule, ScheduleFire, WebhookDelivery
from app.engine.labels import ISSUANCE_LABEL
from app.scheduler import service

logger = logging.getLogger(__name__)

FORMATS = ("generic", "feishu", "wecom", "dingtalk")
EVENTS = ("finished", "failed", "awaiting_approval")
#: 单次请求的超时（秒）
TIMEOUT_S = 10.0
#: 失败后再试 3 次，每次之前等这么久（秒）。4xx（429 除外）不重试：地址或参数错了，重试也一样
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
#: 只看这么多天内的触发：更早的运行状态不会再变，不用每一拍都扫
SCAN_DAYS = 30
#: 运行结束后等封存（清单哈希）最多等多久。runner 先写终态再封存，两步之间被这一拍撞上就等下一拍
SEAL_WAIT_S = 60
#: 测试用：换成 httpx.MockTransport 接住请求
_transport: httpx.AsyncBaseTransport | None = None

_HEADLINE = {"finished": "运行完成", "failed": "运行失败", "awaiting_approval": "等待审批", "test": "Webhook 测试"}
#: 运行状态在通知里的叫法
_STATUS_LABEL = {"succeeded": "已完成", "failed": "失败", "interrupted": "停在人工审批", "cancelled": "已取消",
                 "failed_to_start": "未能发起"}


# --------------------------------------------------------------------------
# 地址：校验、提示、脱敏
# --------------------------------------------------------------------------


def check_url(url: Any) -> str:
    """校验 Webhook 地址，返回去掉首尾空白的地址。不对抛 ValueError（中文说明，不回显地址）。"""
    text = url.strip() if isinstance(url, str) else ""
    try:
        parts = urlsplit(text)
    except ValueError:
        parts = None
    if (not text or parts is None or parts.scheme not in ("http", "https") or not parts.hostname
            or any(c.isspace() for c in text)):
        raise ValueError("Webhook 地址需要是 http:// 或 https:// 开头的完整地址，例如飞书机器人的 "
                         "https://open.feishu.cn/open-apis/bot/v2/hook/…")
    if len(text) > 2000:
        raise ValueError("Webhook 地址过长（超过 2000 个字符），请检查是否粘贴了多余的内容")
    return text


def host_of(url: str) -> str:
    """主机名（带非默认端口）。日志、报错里只写它。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        return f"{host}:{parts.port}" if parts.port else host
    except ValueError:
        return "?"


def url_hint(url: str) -> str:
    """给界面看的地址提示：只露主机和末 4 位（open.feishu.cn/…ab12）。"""
    return f"{host_of(url)}/…{url[-4:]}"


def _sensitive(url: str, secret: str | None) -> list[str]:
    """一条地址里不能出现在日志、报错里的片段：整条地址、查询参数的值（key、access_token）、路径里的长片段（飞书
    的令牌在路径里）、签名密钥。长的在前，免得先换掉短的、长的就认不出来了。"""
    out = {url, secret or ""}
    try:
        parts = urlsplit(url)
        out |= {value for _, value in parse_qsl(parts.query)}
        out |= {seg for seg in parts.path.split("/") if len(seg) >= 12}
        out.add(f"{parts.netloc}{parts.path}")
    except ValueError:
        pass
    return sorted((s for s in out if len(s) >= 4), key=len, reverse=True)


def _scrub(text: str, url: str, secret: str | None) -> str:
    for piece in _sensitive(url, secret):
        text = text.replace(piece, "…")
    return text


#: 正在推送哪条地址、用的哪个密钥。httpx 记请求日志时还在同一个任务里，过滤器据此只改推送的那几行
_SENDING: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar("webhook_sending", default=None)


class _RedactWebhookUrls(logging.Filter):
    """把 httpx 记下的推送请求的地址换成主机名。别的请求（模型接入、数据源工具）原样放过。"""

    def filter(self, record: logging.LogRecord) -> bool:
        sending = _SENDING.get()
        if sending and isinstance(record.args, tuple):
            url, secret = sending
            record.args = tuple(
                url_hint(str(arg)) if isinstance(arg, httpx.URL)
                else _scrub(arg, url, secret) if isinstance(arg, str) else arg
                for arg in record.args)
        return True


def _install_filter() -> None:
    target = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactWebhookUrls) for f in target.filters):
        target.addFilter(_RedactWebhookUrls())


_install_filter()


# --------------------------------------------------------------------------
# 消息内容
# --------------------------------------------------------------------------


@dataclass
class Notice:
    """一条通知要说的事。event 是 finished / failed / awaiting_approval / test；status 是运行状态，没能发起的是
    failed_to_start。没有的项为 None。"""

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
    if notice.event == "failed" and notice.status == "failed_to_start":
        headline = "未能发起运行"
    else:
        headline = _HEADLINE.get(notice.event, notice.event)
    title = f"定时任务「{notice.schedule_name}」{headline}"
    lines: list[tuple[str, str]] = []
    if notice.event == "test":
        lines.append(("说明", "这是一条测试消息，收到说明 Webhook 地址和签名配置正确"))
        return title, lines, None
    if notice.status:
        lines.append(("状态", _STATUS_LABEL.get(notice.status, notice.status)))
    if notice.tier:
        gaps = f"（缺口 {notice.gaps} 条）" if notice.gaps is not None else ""
        lines.append(("出具档位", f"{ISSUANCE_LABEL.get(notice.tier, notice.tier)}{gaps}"))
    if notice.pending_approvals:
        lines.append(("待审批", f"{notice.pending_approvals} 项，请到 AgentLab 处理"))
    if notice.status == "failed_to_start" and notice.reason:
        lines.append(("原因", notice.reason))
    elif notice.status == "failed":
        lines.append(("原因", "运行失败，详情请查看运行记录"))
    if notice.fire_at is not None:
        lines.append(("计划触发", f"{_local(notice.fire_at, notice.timezone, seconds=False)}（{notice.timezone}）"))
    if notice.started_at is not None:
        lines.append(("开始", _local(notice.started_at, notice.timezone) or ""))
    if notice.finished_at is not None:
        lines.append(("结束", _local(notice.finished_at, notice.timezone) or ""))
    if notice.manifest_hash:
        lines.append(("封存哈希", notice.manifest_hash[:12]))
    if notice.run_id:
        lines.append(("运行编号", notice.run_id))
    return title, lines, notice.run_url


def _text(notice: Notice) -> str:
    title, lines, link = message(notice)
    out = [f"【AgentLab】{title}", *(f"{k}：{v}" for k, v in lines)]
    if link:
        out.append(f"查看运行：{link}")
    return "\n".join(out)


def _wecom_markdown(notice: Notice) -> str:
    title, lines, link = message(notice)
    color = "info" if notice.event in ("finished", "test") else "warning"
    out = [f"**【AgentLab】{title}**"]
    for key, value in lines:
        shown = f'<font color="{color}">{value}</font>' if key == "状态" else value
        out.append(f"> {key}：{shown}")
    if link:
        out.append(f"[查看运行]({link})")
    return "\n".join(out)


def _dingtalk_markdown(notice: Notice) -> tuple[str, str]:
    title, lines, link = message(notice)
    out = [f"#### 【AgentLab】{title}", "", *(f"- {k}：{v}" for k, v in lines)]
    if link:
        out += ["", f"[查看运行]({link})"]
    return title, "\n".join(out)


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
        "reason": notice.reason if notice.status == "failed_to_start" else None,
        "sent_at": _iso(now),
    }


def build_request(fmt: str, url: str, secret: str | None, notice: Notice, *,
                  now: datetime | None = None) -> tuple[str, dict[str, str], bytes]:
    """(请求地址, 请求头, 请求体)。签名里的时间戳按 now 取，每次重试重新算：飞书要求时间戳在一小时内。"""
    now = now or utcnow()
    headers = {"Content-Type": "application/json"}
    if fmt == "feishu":
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": _text(notice)}}
        if secret:
            ts = str(int(now.timestamp()))
            # 飞书自定义机器人的签名：以「timestamp + 换行 + 密钥」为键，对空串做 HMAC-SHA256，再 base64
            key = f"{ts}\n{secret}".encode()
            body = {"timestamp": ts, "sign": base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode(),
                    **body}
        return url, headers, _json(body)
    if fmt == "wecom":
        return url, headers, _json({"msgtype": "markdown", "markdown": {"content": _wecom_markdown(notice)}})
    if fmt == "dingtalk":
        title, text = _dingtalk_markdown(notice)
        target = url
        if secret:
            # 钉钉自定义机器人的签名：毫秒时间戳，以密钥为键对「timestamp + 换行 + 密钥」做 HMAC-SHA256，base64 后
            # 再做 URL 编码，和 timestamp 一起加在地址上
            ts = str(int(now.timestamp() * 1000))
            digest = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
            sign = quote_plus(base64.b64encode(digest).decode())
            target = f"{url}{'&' if '?' in url else '?'}timestamp={ts}&sign={sign}"
        return target, headers, _json({"msgtype": "markdown", "markdown": {"title": title, "text": text}})
    payload = _json(_generic(notice, now))
    if secret:
        # 通用格式：接收端按 HMAC-SHA256(密钥, 「时间戳.请求体」) 验签，时间戳防重放
        ts = str(int(now.timestamp()))
        mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        headers |= {"X-AgentLab-Timestamp": ts, "X-AgentLab-Signature": f"sha256={mac}"}
    return url, headers, payload


def _json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


# --------------------------------------------------------------------------
# 推送
# --------------------------------------------------------------------------


@dataclass
class Outcome:
    """一次推送（含重试）的结果。error 是中文说明，只有主机名，不含地址和密钥。"""

    ok: bool
    http_status: int | None
    attempts: int
    error: str | None = None
    #: 这一次失败值不值得再试（只在单次尝试的结果上有意义）
    retryable: bool = False


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, transport=_transport, follow_redirects=False)


async def send(fmt: str, url: str, secret: str | None, notice: Notice, *, retry: bool = True) -> Outcome:
    """推送一条通知。retry 时失败再试 3 次（退避），4xx（429 除外）不重试。失败只记主机名。"""
    host = host_of(url)
    delays = RETRY_DELAYS if retry else ()
    last = Outcome(False, None, 0, "没有发出请求")
    async with _client() as client:
        for attempt in range(len(delays) + 1):
            if attempt:
                await asyncio.sleep(delays[attempt - 1])
            target, headers, body = build_request(fmt, url, secret, notice)
            last = await _post_once(client, fmt, target, headers, body, url, secret)
            last.attempts = attempt + 1
            if last.ok:
                return last
            logger.warning("定时任务「%s」的 Webhook（%s）第 %d 次推送失败：%s",
                           notice.schedule_name, host, attempt + 1, last.error)
            if not last.retryable:
                break
    return last


async def _post_once(client: httpx.AsyncClient, fmt: str, target: str, headers: dict[str, str], body: bytes,
                     url: str, secret: str | None) -> Outcome:
    host = host_of(url)
    token = _SENDING.set((url, secret))
    try:
        response = await client.post(target, content=body, headers=headers)
    except httpx.TimeoutException:
        return Outcome(False, None, 1, f"请求 {host} 超时（{TIMEOUT_S:g} 秒）", retryable=True)
    except httpx.ConnectError:
        return Outcome(False, None, 1, f"无法连接 {host}", retryable=True)
    except httpx.HTTPError as e:
        return Outcome(False, None, 1, f"请求 {host} 失败（{type(e).__name__}）", retryable=True)
    finally:
        _SENDING.reset(token)
    status = response.status_code
    reply = _reply_json(response)
    if not 200 <= status < 300:
        detail = _platform_message(reply)
        text = f"{host} 返回 HTTP {status}" + (f"：{detail}" if detail else "")
        return Outcome(False, status, 1, _scrub(text, url, secret)[:300], retryable=status == 429 or status >= 500)
    if problem := _platform_error(fmt, reply):
        return Outcome(False, status, 1, _scrub(f"{host} {problem}", url, secret)[:300], retryable=True)
    return Outcome(True, status, 1)


def _reply_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _platform_message(reply: dict[str, Any] | None) -> str:
    if not reply:
        return ""
    text = reply.get("errmsg") or reply.get("msg") or reply.get("StatusMessage") or ""
    return str(text)[:200]


def _platform_error(fmt: str, reply: dict[str, Any] | None) -> str | None:
    """HTTP 200 里的平台错误码：飞书 code（老接口 StatusCode），企业微信、钉钉 errcode。0 是成功。

    说明里写主机名（调用方拼在前面），不写平台名：主机名已经说明是哪个平台，配错了格式时也不会张冠李戴。"""
    if not reply or fmt == "generic":
        return None
    code = reply.get("code", reply.get("StatusCode")) if fmt == "feishu" else reply.get("errcode")
    if code in (None, 0, "0"):
        return None
    detail = _platform_message(reply)
    return f"返回错误码 {code}" + (f"：{detail}" if detail else "")


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
#: 运行状态 → 事件。cancelled 是有人手动停的，不推
_EVENT_OF = {"succeeded": "finished", "failed": "failed", "cancelled": None, "interrupted": "awaiting_approval"}


@dataclass
class _Job:
    fmt: str
    url: str
    secret: str | None
    notice: Notice


async def notify_due(now: datetime | None = None) -> int:
    """查已发起的触发，运行状态有变化的按订阅推送一次。返回这一拍推送了几条（含失败的）。

    处理过的状态记在 notified_status 上，用条件 UPDATE 认领（WHERE notified_status = 旧值）：两拍撞在一起也只
    推一次。没订阅这个事件、任务没配 Webhook 的也记下已处理——之后再配上 Webhook，不会把旧的补推一遍。
    """
    now = now or utcnow()
    jobs: list[_Job] = []
    # 和发起共用一把锁：发起时先记一条「未能发起」再发起运行、成功了才改成 started，扫描不能撞在这中间
    async with service.launch_lock():
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(ScheduleFire, Run)
                .outerjoin(Run, Run.id == ScheduleFire.run_id)
                .where(ScheduleFire.created_at >= now - timedelta(days=SCAN_DAYS), or_(
                    and_(ScheduleFire.status == "failed_to_start", ScheduleFire.notified_status.is_(None)),
                    and_(ScheduleFire.status == "started", Run.status.in_(_WATCHED),
                         or_(ScheduleFire.notified_status.is_(None), ScheduleFire.notified_status != Run.status)),
                ))
                .order_by(ScheduleFire.created_at)
            )).all()
            base_url: str | None = None
            if rows:
                from app.api.settings import notify_settings

                base_url = (await notify_settings(session))["public_base_url"]
            for fire, run in rows:
                state, event, pending = await _state_of(session, fire, run, now)
                if state is None or not await _mark(fire.id, fire.notified_status, state):
                    continue
                schedule = await session.get(Schedule, fire.schedule_id)
                url = decrypt(schedule.webhook_url) if schedule and schedule.webhook_url else None
                if not (event and url and schedule.webhook_format in FORMATS
                        and event in (schedule.webhook_events or [])):
                    continue
                notice = await _notice(session, schedule, fire, run, event, base_url, pending)
                jobs.append(_Job(schedule.webhook_format, url, decrypt(schedule.webhook_secret), notice))
    if jobs:
        await asyncio.gather(*(_deliver(job) for job in jobs))
    return len(jobs)


async def _state_of(session: Any, fire: ScheduleFire, run: Run | None,
                    now: datetime) -> tuple[str | None, str | None, int]:
    """(要记下的已处理状态, 要推送的事件, 待审批数)。还不能下结论（没封存、挂起但没有待审批）时状态为 None。"""
    if fire.status == "failed_to_start":
        return "failed_to_start", "failed", 0
    if run is None:
        return None, None, 0
    if run.status in ("succeeded", "failed", "cancelled"):
        finished = run.finished_at or now
        if not run.manifest_hash and (now - service.as_utc(finished)).total_seconds() < SEAL_WAIT_S:
            return None, None, 0
        return run.status, _EVENT_OF[run.status], 0
    if run.status == "interrupted":
        # 服务重启挂起的运行也是 interrupted，但没人要审批：确有待审批才算停在审批
        pending = await session.scalar(select(func.count()).select_from(Approval).where(
            Approval.run_id == run.id, Approval.status == "pending")) or 0
        return ("interrupted", "awaiting_approval", pending) if pending else (None, None, 0)
    return None, None, 0


async def _mark(fire_id: str, old: str | None, state: str) -> bool:
    cond = ScheduleFire.notified_status.is_(None) if old is None else ScheduleFire.notified_status == old
    async with SessionLocal() as session:
        result = await session.execute(update(ScheduleFire).where(ScheduleFire.id == fire_id, cond)
                                       .values(notified_status=state))
        await session.commit()
        return result.rowcount == 1


async def _notice(session: Any, schedule: Schedule, fire: ScheduleFire, run: Run | None, event: str,
                  base_url: str | None, pending: int) -> Notice:
    tier, gaps = await _issuance_of(session, run) if run is not None else (None, None)
    return Notice(
        event=event, schedule_id=schedule.id, schedule_name=schedule.name, timezone=schedule.timezone,
        fire_id=fire.id, fire_at=fire.fire_at, fire_kind=fire.kind,
        run_id=run.id if run else None,
        status=run.status if run else fire.status,
        tier=tier, gaps=gaps,
        run_url=f"{base_url}/runs/{run.id}" if base_url and run else None,
        manifest_hash=run.manifest_hash if run else None,
        started_at=run.started_at if run else None,
        finished_at=run.finished_at if run else None,
        reason=fire.reason if fire.status == "failed_to_start" else None,
        pending_approvals=pending or None,
    )


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
    没配 Webhook 抛 ValueError。返回 {ok, http_status, message}。"""
    async with SessionLocal() as session:
        schedule = await session.get(Schedule, schedule_id)
        if schedule is None:
            raise KeyError(schedule_id)
        url = decrypt(schedule.webhook_url) if schedule.webhook_url else None
        if not url or schedule.webhook_format not in FORMATS:
            raise ValueError("这个定时任务没有配置 Webhook，请先填写地址并保存")
        fmt, secret = schedule.webhook_format, decrypt(schedule.webhook_secret)
        notice = Notice(event="test", schedule_id=schedule.id, schedule_name=schedule.name,
                        timezone=schedule.timezone)
    outcome = await send(fmt, url, secret, notice, retry=False)
    await _record(schedule_id, None, None, "test", outcome)
    if outcome.ok:
        return {"ok": True, "http_status": outcome.http_status, "message": f"测试消息已送达（HTTP {outcome.http_status}）"}
    return {"ok": False, "http_status": outcome.http_status, "message": f"推送失败：{outcome.error}"}
