"""定时任务的通知：运行结束、失败、停在审批（或因服务重启挂起）时按任务的设置推送 Webhook。

四种格式：通用 JSON、飞书（文本消息）、企业微信（markdown）、钉钉（markdown）。配了签名密钥时：通用格式在请求头
里带 HMAC-SHA256 签名，飞书、钉钉按各自平台的算法签（飞书放在请求体，钉钉放在地址上）；企业微信的 key 本来就在
地址里，没有另外的签名。

Webhook 地址本身就是凭证（企业微信的 key、钉钉的 access_token 都在地址里，飞书的在路径里），和签名密钥一样
加密落库，日志、推送记录、接口返回里只出现主机名。httpx 每发一个请求都会在 INFO 级别记一行完整地址，这里装了
一个过滤器把推送期间的那几行换成主机名。

通知里只有任务名、状态、出具档位、缺口条数、运行链接、封存哈希（界面上叫清单哈希）前 12 位和起止时间，失败时
另写失败节点的名字和去哪里查看（不许诺能继续运行）。不放报告里的具体数字，也不放失败时的原始报错——报错里可能带着
查询到的数据，Webhook 推去的群不一定有权看。

三个平台出错时都回 HTTP 200、错误码放在响应体里，所以送没送达要看响应体：按地址的主机认平台，不看选的格式——
格式选错了（飞书的地址选了通用格式）正是最需要认出来的情形。群机器人的地址只认自己平台的格式，保存时就拦下
对不上的（见 bot_format）。
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit

import httpx
from sqlalchemy import and_, func, or_, select, update

from app.core.crypto import decrypt
from app.core.events import EventType
from app.db.base import SessionLocal, utcnow
from app.db.models import Approval, Run, RunEvent, Schedule, ScheduleFire, WebhookDelivery
from app.engine.labels import ISSUANCE_LABEL
from app.scheduler import service

logger = logging.getLogger(__name__)

FORMATS = ("generic", "feishu", "wecom", "dingtalk")
EVENTS = ("finished", "failed", "awaiting_approval")
#: 格式、事件在报错里的叫法，和界面上的选项一致
FORMAT_LABEL = {"generic": "通用", "feishu": "飞书", "wecom": "企业微信", "dingtalk": "钉钉"}
EVENT_LABEL = {"finished": "运行结束", "failed": "运行失败", "awaiting_approval": "等待审批"}
#: 单次请求的超时（秒）
TIMEOUT_S = 10.0
#: 失败后再试 3 次，每次之前等这么久（秒）。4xx（429 除外）不重试：地址或参数错了，重试也一样
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
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
#: 失败的运行怎么处理：不许诺「修正后可继续运行」。没开始执行的没有断点；开始执行过的也可能没有（执行第一个节点前就
#: 失败）或已不兼容（系统升级），能不能继续以记录页的提示为准
FAILED_ACTION = "请在 AgentLab 的运行记录中查看原因"
#: 从没开始执行就失败的（服务重启时还在排队，被改成失败、没有开始时间）：没有可继续的进度。不照抄运行记录上的报错——
#: 通知一律不放原始报错，这一句是按「没有开始时间」认出来的，自己写
NOT_STARTED_REASON = "运行在开始执行前就已中止（通常是服务重启时仍在排队），没有可继续的进度"
#: 失败的运行不挡之后的触发（挂起的才挡）：任务还在排期上时补这一句，免得人以为这个定时任务就此停了
NEXT_AS_USUAL = "下一次触发会照常发起"


# --------------------------------------------------------------------------
# 地址：校验、提示、脱敏
# --------------------------------------------------------------------------


_URL_FORMAT = ("Webhook 地址需要是 http:// 或 https:// 开头的完整地址，例如飞书机器人的 "
               "https://open.feishu.cn/open-apis/bot/v2/hook/…")


def check_url(url: Any) -> str:
    """校验 Webhook 地址，返回去掉首尾空白的地址。不对抛 ValueError（中文说明，不回显地址）。

    除了 http(s)、有主机名，还要 httpx 真发得出去：端口写错、夹着不可见的字符（从聊天软件复制时常带上零宽空格）、
    主机名不合 IDNA 规则的，保存时就拦下。以前这些照样存进去，到「测试 Webhook」才在 httpx 里抛出 500 或英文报错。
    """
    text = url.strip() if isinstance(url, str) else ""
    if not text:
        raise ValueError(_URL_FORMAT)
    if any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in text):
        raise ValueError("Webhook 地址中有空格或不可见的字符（从聊天软件复制时常会带上），请删除后重新填写")
    if len(text) > 2000:
        raise ValueError("Webhook 地址过长（超过 2000 个字符），请检查是否粘贴了多余的内容")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise ValueError(_URL_FORMAT) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(_URL_FORMAT)
    try:
        port = parts.port
    except ValueError:   # 不是数字，或者超出 65535
        port = -1
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Webhook 地址中的端口需要是 1 到 65535 之间的数字")
    try:
        # 照发送时的样子建一个请求（不发出去）：httpx 到这一步才按 IDNA 解码主机名
        httpx.Request("POST", text)
    except (httpx.InvalidURL, ValueError):   # IDNA 的错误是 UnicodeError，也是 ValueError
        raise ValueError("Webhook 地址的主机名不符合规范，无法发送，请检查后重新填写") from None
    return text


#: 群机器人的地址（主机，路径前缀）→ 格式。这些地址只认自己平台的消息格式：格式选错了，平台回错误码，群里收不到
_BOTS = (
    ("open.feishu.cn", "/open-apis/bot/", "feishu"),
    ("open.larksuite.com", "/open-apis/bot/", "feishu"),
    ("qyapi.weixin.qq.com", "/cgi-bin/webhook/", "wecom"),
    ("oapi.dingtalk.com", "/robot/", "dingtalk"),
)
#: 平台的主机 → 按哪个平台读 HTTP 200 里的错误码。同一主机上不是群机器人的接口（飞书集成平台的 Webhook 触发器
#: 收任意 JSON）也是同一套错误码约定
_PLATFORM_HOSTS = {host: fmt for host, _, fmt in _BOTS}


def bot_format(url: str) -> str | None:
    """这条地址是哪个平台的群机器人（feishu / wecom / dingtalk），不是群机器人的返回 None。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    return next((fmt for bot_host, prefix, fmt in _BOTS if host == bot_host and parts.path.startswith(prefix)), None)


def format_problem(fmt: str, url: str) -> str | None:
    """格式和地址对不上时的说明（群机器人的地址选了别的格式），对得上返回 None。"""
    expected = bot_format(url)
    if expected is None or expected == fmt:
        return None
    label = FORMAT_LABEL[expected]
    return f"这是{label}群机器人的地址，只接收{label}的消息格式，Webhook 格式请选择「{label}」"


def host_of(url: str) -> str:
    """主机名（带非默认端口）。日志、报错里只写它。端口写错的地址也照样给出主机名。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
    except ValueError:
        return "?"
    try:
        port = parts.port
    except ValueError:
        return host
    return f"{host}:{port}" if port else host


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
    failed_to_start。没有的项为 None。

    suspended：运行停在断点上、没有待审批（通常是服务重启挂起的），按 failed 事件推。error_node：失败节点的名字。
    next_fire：任务启用着、往后还会触发（失败通知据此补「下一次触发会照常发起」）。
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
    error_node: str | None = None
    next_fire: bool = False
    fmt: str | None = None
    signed: bool = False

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
    else:
        headline = _HEADLINE.get(notice.event, notice.event)
    title = f"定时任务「{notice.schedule_name}」{headline}"
    lines: list[tuple[str, str]] = []
    if notice.status:
        lines.append(("状态", _status_label(notice)))
    if notice.tier:
        gaps = f"（缺口 {notice.gaps} 条）" if notice.gaps is not None else ""
        lines.append(("出具档位", f"{ISSUANCE_LABEL.get(notice.tier, notice.tier)}{gaps}"))
    if notice.pending_approvals:
        lines.append(("待审批", f"{notice.pending_approvals} 项，请到 AgentLab 处理；运行结束之前，"
                                "这个定时任务之后的触发都会跳过"))
    if notice.status == "failed_to_start" and notice.reason:
        lines.append(("原因", notice.reason))
    elif notice.suspended:
        lines += [("原因", SUSPENDED_REASON), ("处理", SUSPENDED_ACTION)]
    elif notice.status == "failed":
        # 不写原始报错（可能带着查询到的数据），写失败节点的名字和去哪里处理
        if notice.error_node:
            lines.append(("失败节点", f"「{notice.error_node}」"))
        if notice.never_started:
            lines.append(("原因", NOT_STARTED_REASON))
        lines.append(("处理", f"{FAILED_ACTION}；{NEXT_AS_USUAL}" if notice.next_fire else FAILED_ACTION))
    if notice.fire_at is not None:
        when = f"{_local(notice.fire_at, notice.timezone, seconds=False)}（{notice.timezone}）"
        # 立即运行不写「计划触发」：群里的人会以为报告按时出了
        if notice.fire_kind == "manual":
            lines.append(("手动触发", when))
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


def _status_label(notice: Notice) -> str:
    if notice.status == "interrupted":
        return "等待审批" if notice.pending_approvals else "已挂起，可继续运行"
    return _STATUS_LABEL.get(notice.status or "", notice.status or "")


def _test_text(notice: Notice) -> str:
    """测试消息的说明只说收到它证明得了的事：飞书、钉钉的机器人开了签名校验时，验签不过就不收；通用格式的接收端验没
    验签这边不知道；企业微信不签名，填了密钥也不提。"""
    if notice.signed and notice.fmt in ("feishu", "dingtalk"):
        return "这是一条测试消息。收到即表示 Webhook 地址可用；机器人开启了签名校验的，签名密钥也配置正确"
    if notice.signed:
        return "这是一条测试消息。收到即表示 Webhook 地址可用；请求头带有签名，接收端可据此核对签名密钥"
    return "这是一条测试消息。收到即表示 Webhook 地址配置正确"


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
        "reason": (notice.reason if notice.status == "failed_to_start"
                   else f"{SUSPENDED_REASON}。{SUSPENDED_ACTION}" if notice.suspended
                   else NOT_STARTED_REASON if notice.never_started else None),
        "error_node": notice.error_node,
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
    except httpx.InvalidURL:
        # 保存时的地址校验加严之前存进库的地址：httpx 抛的不是 HTTPError。重试也一样，请人改地址
        return Outcome(False, None, 1, f"发往 {host} 的 Webhook 地址无效，请重新填写地址")
    except Exception as e:  # noqa: BLE001 - 端口越界之类在传输层才抛（不是 HTTPError），不能让测试接口回 500
        return Outcome(False, None, 1, f"无法向 {host} 发送请求（{type(e).__name__}），请检查 Webhook 地址")
    finally:
        _SENDING.reset(token)
    status = response.status_code
    reply = _reply_json(response)
    if not 200 <= status < 300:
        detail = _platform_message(reply)
        text = f"{host} 返回 HTTP {status}" + (f"：{detail}" if detail else "")
        return Outcome(False, status, 1, _scrub(text, url, secret)[:300], retryable=status == 429 or status >= 500)
    if problem := _platform_error(fmt, url, reply):
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


def _platform_error(fmt: str, url: str, reply: dict[str, Any] | None) -> str | None:
    """HTTP 200 里的平台错误码：飞书 code（老接口 StatusCode），企业微信、钉钉 errcode。0 是成功。

    按地址的主机认平台，不看选的格式：以前只看所选格式自己的字段（通用格式完全不看），飞书的地址选了通用格式、
    平台回 code 19002，照样记「已送达」。主机不是这几个平台的（自建接收端、代理转发）才按所选格式读，通用格式
    不解读——自建接收端的 code 字段是什么意思由它自己定。说明里写主机名（调用方拼在前面），不写平台名。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    platform = _PLATFORM_HOSTS.get(host) or (fmt if fmt != "generic" else None)
    if not reply or platform is None:
        return None
    code = reply.get("code", reply.get("StatusCode")) if platform == "feishu" else reply.get("errcode")
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
#: 结束的运行 → 事件。cancelled 是有人手动停的，不推。interrupted 分停在审批和挂起两种，见 _state_of
_EVENT_OF = {"succeeded": "finished", "failed": "failed", "cancelled": None}


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
    停在断点上的运行每一拍都要重新看（见 _state_of）：它的已处理状态带着第几段执行，和运行状态本身对不上。
    """
    now = now or utcnow()
    jobs: list[_Job] = []
    cutoff = now - timedelta(days=SCAN_DAYS)
    # 扫描窗口按触发记录的创建时间算，两种例外：运行记录窗口内有过变化（运行状态一变 updated_at 就跟着变），以及
    # 停在审批上、有待审批的——以前只看触发记录，创建 30 天以后才停到审批上的运行收不到「等待审批」
    waiting = select(Approval.id).where(Approval.run_id == Run.id, Approval.status == "pending").exists()
    # 和发起共用一把锁：发起时先记一条「未能发起」再发起运行、成功了才改成 started，扫描不能撞在这中间
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
            base_url: str | None = None
            if rows:
                from app.api.settings import notify_settings

                base_url = (await notify_settings(session))["public_base_url"]
            for fire, run in rows:
                state, event, pending = await _state_of(session, fire, run, now)
                if state is None or state == fire.notified_status \
                        or not await _mark(fire.id, fire.notified_status, state):
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
    """(要记下的已处理状态, 要推送的事件, 待审批数)。还不能下结论（没封存、审批刚批完还没接着执行）时状态为 None。

    停在断点上（interrupted）的分两种，各推一次：
    - 有待审批：停在审批，推 awaiting_approval。批完接着执行、又停在下一个审批点时要再推，所以已处理状态里带着这是
      第几段执行（每段执行从一条 run.started 开始），同一个审批点不重复推。
    - 没有待审批：通常是服务重启挂起的。它不会自己接着执行，这个任务之后的触发都会按「上一次还没结束」跳过，
      不说一声任务就此静默停摆——按 failed 推一次，写明要去继续运行或放弃。
    审批批完到运行改回 running 之间，审批是 answered、还没有 resolved：那不是挂起，等下一拍。
    """
    if fire.status == "failed_to_start":
        return "failed_to_start", "failed", 0
    if run is None:
        return None, None, 0
    if run.status in ("succeeded", "failed", "cancelled"):
        # runner 先写终态和结束时间、再封存，两步之间被这一拍撞上就等。没有结束时间的（服务重启时还在排队、被改成
        # failed 的）永远不会封存：按它最后一次更新的时间等，以前按「现在」等，每一拍都等、永远不推
        since = run.finished_at or run.updated_at or fire.created_at
        if not run.manifest_hash and (now - service.as_utc(since)).total_seconds() < SEAL_WAIT_S:
            return None, None, 0
        return run.status, _EVENT_OF[run.status], 0
    if run.status != "interrupted":
        return None, None, 0
    counts = dict((await session.execute(
        select(Approval.status, func.count()).where(
            Approval.run_id == run.id, Approval.status.in_(("pending", "answered")))
        .group_by(Approval.status))).all())
    segment = await session.scalar(select(func.count()).select_from(RunEvent).where(
        RunEvent.run_id == run.id, RunEvent.type == EventType.RUN_STARTED.value)) or 0
    if counts.get("pending"):
        return f"interrupted:approval:{segment}", "awaiting_approval", counts["pending"]
    if counts.get("answered"):
        return None, None, 0
    return f"interrupted:held:{segment}", "failed", 0


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
        suspended=run is not None and run.status == "interrupted" and not pending,
        error_node=_failed_node(run) if run is not None and run.status == "failed" else None,
        next_fire=bool(schedule.enabled and schedule.next_run_at is not None),
    )


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
