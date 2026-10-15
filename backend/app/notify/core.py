"""通知的发送核心：四种格式、签名、发送、脱敏（从 scheduler/webhook.py 抽出，定时任务和待办、实例、动作共用一份）。

四种格式：通用 JSON、飞书（文本消息）、企业微信（markdown）、钉钉（markdown）。配了签名密钥时：通用格式在请求头
里带 HMAC-SHA256 签名，飞书、钉钉按各自平台的算法签（飞书放在请求体，钉钉放在地址上）；企业微信的 key 本来就在
地址里，没有另外的签名。入站钩子（app/process/hooks.py）验的也是通用格式这一套签名。

Webhook 地址本身就是凭证（企业微信的 key、钉钉的 access_token 都在地址里，飞书的在路径里），和签名密钥一样
加密落库，日志、推送记录、接口返回里只出现主机名。httpx 每发一个请求都会在 INFO 级别记一行完整地址，这里装了
一个过滤器把推送期间的那几行换成主机名。

三个平台出错时都回 HTTP 200、错误码放在响应体里，所以送没送达要看响应体：按地址的主机认平台，不看选的格式——
格式选错了（飞书的地址选了通用格式）正是最需要认出来的情形。群机器人的地址只认自己平台的格式，保存时就拦下
对不上的（见 bot_format）。

消息内容（Message）由调用方拼：只放标题、状态、链接这类，不放报告里的具体数字。
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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit

import httpx

from app.db.base import utcnow

logger = logging.getLogger(__name__)

FORMATS = ("generic", "feishu", "wecom", "dingtalk")
#: 格式在报错里的叫法，和界面上的选项一致
FORMAT_LABEL = {"generic": "通用", "feishu": "飞书", "wecom": "企业微信", "dingtalk": "钉钉"}
#: 单次请求的超时（秒）
TIMEOUT_S = 10.0
#: 失败后再试 3 次，每次之前等这么久（秒）。4xx（429 除外）不重试：地址或参数错了，重试也一样
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
#: 测试用：换成 httpx.MockTransport 接住请求
_transport: httpx.AsyncBaseTransport | None = None
#: 通用格式签名的请求头。入站钩子验同一套
TIMESTAMP_HEADER = "X-AgentLab-Timestamp"
SIGNATURE_HEADER = "X-AgentLab-Signature"


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


def scrub(text: str, url: str, secret: str | None) -> str:
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
                else scrub(arg, url, secret) if isinstance(arg, str) else arg
                for arg in record.args)
        return True


def _install_filter() -> None:
    target = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactWebhookUrls) for f in target.filters):
        target.addFilter(_RedactWebhookUrls())


_install_filter()


# --------------------------------------------------------------------------
# 消息与签名
# --------------------------------------------------------------------------


@dataclass
class Message:
    """一条通知要说的事。lines 是 (项, 内容)，links 是 (文字, 地址)。ok：好消息（企业微信里状态用绿色）。"""

    title: str
    lines: list[tuple[str, str]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    ok: bool = False


def text_of(msg: Message) -> str:
    out = [f"【AgentLab】{msg.title}", *(f"{k}：{v}" for k, v in msg.lines)]
    out += [f"{label}：{url}" for label, url in msg.links]
    return "\n".join(out)


def wecom_markdown(msg: Message) -> str:
    color = "info" if msg.ok else "warning"
    out = [f"**【AgentLab】{msg.title}**"]
    for key, value in msg.lines:
        shown = f'<font color="{color}">{value}</font>' if key == "状态" else value
        out.append(f"> {key}：{shown}")
    out += [f"[{label}]({url})" for label, url in msg.links]
    return "\n".join(out)


def dingtalk_markdown(msg: Message) -> tuple[str, str]:
    out = [f"#### 【AgentLab】{msg.title}", "", *(f"- {k}：{v}" for k, v in msg.lines)]
    for label, url in msg.links:
        out += ["", f"[{label}]({url})"]
    return msg.title, "\n".join(out)


def sign(secret: str, ts: str, body: bytes) -> str:
    """通用格式的签名：HMAC-SHA256(密钥, 「时间戳.请求体」)，十六进制。请求头里写成 sha256=<签名>。"""
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def build_request(fmt: str, url: str, secret: str | None, msg: Message, *, generic: dict[str, Any],
                  now: datetime | None = None) -> tuple[str, dict[str, str], bytes]:
    """(请求地址, 请求头, 请求体)。generic 是通用格式的请求体（各调用方自己的字段）。签名里的时间戳按 now 取，每次
    重试重新算：飞书要求时间戳在一小时内。"""
    now = now or utcnow()
    headers = {"Content-Type": "application/json"}
    if fmt == "feishu":
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": text_of(msg)}}
        if secret:
            ts = str(int(now.timestamp()))
            # 飞书自定义机器人的签名：以「timestamp + 换行 + 密钥」为键，对空串做 HMAC-SHA256，再 base64
            key = f"{ts}\n{secret}".encode()
            body = {"timestamp": ts, "sign": base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode(),
                    **body}
        return url, headers, _json(body)
    if fmt == "wecom":
        return url, headers, _json({"msgtype": "markdown", "markdown": {"content": wecom_markdown(msg)}})
    if fmt == "dingtalk":
        title, text = dingtalk_markdown(msg)
        target = url
        if secret:
            # 钉钉自定义机器人的签名：毫秒时间戳，以密钥为键对「timestamp + 换行 + 密钥」做 HMAC-SHA256，base64 后
            # 再做 URL 编码，和 timestamp 一起加在地址上
            ts = str(int(now.timestamp() * 1000))
            digest = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
            sign_text = quote_plus(base64.b64encode(digest).decode())
            target = f"{url}{'&' if '?' in url else '?'}timestamp={ts}&sign={sign_text}"
        return target, headers, _json({"msgtype": "markdown", "markdown": {"title": title, "text": text}})
    payload = _json(generic)
    if secret:
        # 通用格式：接收端按 HMAC-SHA256(密钥, 「时间戳.请求体」) 验签，时间戳防重放
        ts = str(int(now.timestamp()))
        headers |= {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: f"sha256={sign(secret, ts, payload)}"}
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


def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport or _transport, follow_redirects=False)


async def post(fmt: str, url: str, secret: str | None, build: Callable[[], tuple[str, dict[str, str], bytes]], *,
               label: str, retry: bool = True, transport: httpx.AsyncBaseTransport | None = None,
               delays: tuple[float, ...] | None = None) -> Outcome:
    """推送一条通知。build 每次尝试重新拼请求（签名的时间戳要新）。retry 时失败再试 3 次（退避），4xx（429 除外）
    不重试。label 是日志里的「谁的推送」，失败只记主机名。"""
    host = host_of(url)
    waits = (RETRY_DELAYS if delays is None else delays) if retry else ()
    last = Outcome(False, None, 0, "没有发出请求")
    async with _client(transport) as client:
        for attempt in range(len(waits) + 1):
            if attempt:
                await asyncio.sleep(waits[attempt - 1])
            target, headers, body = build()
            last = await _post_once(client, fmt, target, headers, body, url, secret)
            last.attempts = attempt + 1
            if last.ok:
                return last
            logger.warning("%s（%s）第 %d 次推送失败：%s", label, host, attempt + 1, last.error)
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
        return Outcome(False, status, 1, scrub(text, url, secret)[:300], retryable=status == 429 or status >= 500)
    if problem := _platform_error(fmt, url, reply):
        return Outcome(False, status, 1, scrub(f"{host} {problem}", url, secret)[:300], retryable=True)
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
