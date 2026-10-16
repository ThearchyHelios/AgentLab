"""邮件：通知渠道的第五种格式（docs/design/next-round/04-对外集成与触达.md B2，总方案 X12、集成-I4）。

为什么改写应用平台的 Q6（「待办通知只用 Webhook 的四种格式，不做邮件——SMTP 要另配服务器、还要处理退信」）：
- 待办派给了人却找不到人：群机器人群里人人看得见，算不上个人；用户表里本来就有 email（单点登录时自动填，管理员在
  「设置 → 身份」里也能改），邮件是真正能触达个人、成本最低的办法；
- Q6 担心的两件事这里都照实处理，不假装做到：SMTP 是可选配置（设置里的 mail 一组，缺省关，不配就没有邮件这一格式）；
  退信不追踪——投递记录成功时写的是「已交给邮件服务器」，是否进了收件箱以邮件服务器为准，界面上也照这样写；
- IM 应用消息（企业微信、飞书、钉钉的应用消息）这次不做：要企业应用凭据、令牌刷新和用户 id 映射，和 IM 登录是同一套，
  放到一起做。

一种说法：邮件只是渠道的一种格式，订阅、投递记录（notify_deliveries）、测试、消息结构（core.Message：只有标题、状态、
条数、链接，不放数字）都走同一套；不另建邮件表。

收件人：
- 个人邮件渠道（scope=user）不填地址，只发到拥有者在名录里的账号邮箱（users.email）：可以填任意地址的话，个人渠道就成了
  谁都能用的垃圾邮件中继，而且发到的不是认证过的身份。账号没有邮箱时记一条失败的投递并写明原因（NO_EMAIL）；
- 全局、应用的邮件渠道（搭建者以上能建）填固定的收件地址，最多 MAX_RECIPIENTS 个，和全局 Webhook 同一个信任级别。
  地址同 Webhook 一样加密落库（渠道表不改结构：个人渠道存一个占位）。

发送：标准库 smtplib，放在 asyncio.to_thread 里（不多一个依赖），单次超时 TIMEOUT_S 秒。4xx（临时错误）和连不上按
1/2/4 秒重试，5xx（地址、认证、内容被拒）不重试——重试也一样。报错里只写主机名和服务器的回话，绝不出现口令（scrub）。
测试把 _smtp_factory 换成假的（同 core._transport 的做法）。

邮件内容：纯文本一份、简单的 HTML 一份（全部转义）；带 List-Unsubscribe（指向「设置 → 通知」，没有对外访问地址时不带：
这个头只认完整的地址），Auto-Submitted: auto-generated。

设置的 mail 一组只有管理员能改（整个设置接口的改动都要管理员）；口令用 core/crypto 加密存，读设置时只说配没配。
内网的 SMTP 由管理员配置，不做 SSRF 检查（同全局 Webhook）。
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import smtplib
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

from app.notify.core import Message, Outcome, text_of

logger = logging.getLogger(__name__)

#: 设置里的这一组
GROUP = "mail"
#: 加密方式：starttls（587，明文连上再升级）、ssl（465，一上来就是加密连接）、none（内网明文中继）
SECURITY = ("starttls", "ssl", "none")
SECURITY_LABEL = {"starttls": "STARTTLS", "ssl": "SSL/TLS", "none": "不加密"}
DEFAULTS: dict[str, Any] = {"enabled": False, "host": None, "port": 587, "security": "starttls", "username": None,
                            "password": None, "from_addr": None, "from_name": "AgentLab",
                            "default_for_assignees": False}
LABEL = {"enabled": "启用邮件", "host": "SMTP 服务器", "port": "端口", "security": "加密方式", "username": "用户名",
         "password": "口令", "from_addr": "发件地址", "from_name": "发件人名称",
         "default_for_assignees": "默认发邮件给待办处理人"}
#: 单次连接、读写的超时（秒）
TIMEOUT_S = 10.0
#: 临时错误、连不上时再试 3 次，每次之前等这么久（秒）
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)
#: 全局、应用的邮件渠道最多几个收件地址
MAX_RECIPIENTS = 20
#: 个人邮件渠道 url 列存的占位（加密后落库）：收件人一律取拥有者的账号邮箱
PERSONAL_PLACEHOLDER = "account-email"
#: 管理员开了「默认发邮件给待办处理人」时，自动建的个人邮件渠道默认订阅这几项（本人之后可以改、可以关）
ASSIGNEE_EVENTS = ("task.opened", "action.unknown", "review.done")
ASSIGNEE_CHANNEL_NAME = "待办邮件（自动建）"

NO_EMAIL = "你的账号没有邮箱地址，请联系管理员在「设置 → 身份」里填写"
NOT_ENABLED = "邮件服务没有启用：请管理员在「设置 → 通知 → 邮件服务」里配置并启用"
HANDED_OFF = "已交给邮件服务器"

#: 测试用：换成假的 SMTP 工厂，(host, port, security, timeout) → 有 starttls / login / send_message / quit / close 的对象
_smtp_factory: Callable[[str, int, str, float], Any] | None = None

# 地址只认最常见的写法：本地部分不含空白和尖括号，域名至少有一个点。换行一律拒收（防邮件头注入）
_ADDRESS = re.compile(r"^[^@\s<>\"(),;:\\]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$|^\[[0-9A-Fa-f:.]+\]$")


# --------------------------------------------------------------------------
# 地址、设置
# --------------------------------------------------------------------------


def address_ok(text: Any) -> bool:
    return isinstance(text, str) and len(text) <= 254 and bool(_ADDRESS.match(text))


def parse_recipients(raw: Any) -> list[str]:
    """全局、应用邮件渠道填的收件地址（逗号、分号、空白、换行分隔都行）→ 去重后的列表。不对抛 ValueError（中文说明）。"""
    if isinstance(raw, list):
        parts = [str(p) for p in raw]
    elif isinstance(raw, str):
        parts = re.split(r"[\s,;，；、]+", raw)
    else:
        parts = []
    out = list(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
    if not out:
        raise ValueError("请填写至少一个收件地址，多个地址用逗号或换行分隔")
    if len(out) > MAX_RECIPIENTS:
        raise ValueError(f"收件地址最多 {MAX_RECIPIENTS} 个，当前填了 {len(out)} 个；人多的请改用邮件组的地址")
    bad = [p for p in out if not address_ok(p)]
    if bad:
        raise ValueError(f"收件地址「{bad[0][:80]}」不是有效的邮件地址")
    return out


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _port_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def settings_problem(value: Any, saved: Any = None) -> str | None:
    """设置页写进来的 mail 一组有什么问题，没问题是 None。saved 是库里存着的那份（口令没给就沿用它）。

    启用时要配全：服务器、发件地址；没启用时可以只填一部分先存着。发件人名称、主机名里不许有换行（邮件头注入）。"""
    if not isinstance(value, dict):
        return f"「邮件服务」设置格式有误，需要是一个对象，当前为「{value}」"
    for key, item in value.items():
        if key == "password_set":
            continue   # GET 给的只读项，整组回写时带着它不算错
        if key not in DEFAULTS:
            return "「邮件服务」设置中不支持「" + key + "」，可设置的项：" + "、".join(f"「{LABEL[k]}」" for k in DEFAULTS)
        label = LABEL[key]
        if key in ("enabled", "default_for_assignees"):
            if not isinstance(item, bool):
                return f"「{label}」只能是开或关，当前为「{item}」"
        elif key == "port":
            if not _port_ok(item):
                return f"「{label}」需要是 1 到 65535 之间的整数，当前为「{item}」"
        elif key == "security":
            if item not in SECURITY:
                return f"「{label}」只能是 {'、'.join(SECURITY_LABEL.values())} 之一"
        elif item is not None and not isinstance(item, str):
            return f"「{label}」需要是文字，当前为「{item}」"
        elif isinstance(item, str) and any(c in item for c in "\r\n"):
            return f"「{label}」里不能有换行"
        elif key == "host" and _text(item) and not _HOST.match(item.strip()):
            return f"「{label}」需要是主机名或 IP 地址（例如 smtp.example.com），不带协议和端口，当前为「{item}」"
        elif key == "from_addr" and _text(item) and not address_ok(item.strip()):
            return f"「{label}」不是有效的邮件地址，当前为「{item}」"
        elif key in ("username", "from_name") and isinstance(item, str) and len(item) > 200:
            return f"「{label}」最长 200 个字"
        elif key == "password" and isinstance(item, str) and len(item) > 500:
            return f"「{label}」过长"
    merged = {**public_value(saved), **{k: v for k, v in value.items() if k in DEFAULTS and k != "password"}}
    if merged.get("enabled"):
        if not _text(merged.get("host")):
            return f"启用邮件之前请填写「{LABEL['host']}」"
        if not _text(merged.get("from_addr")):
            return f"启用邮件之前请填写「{LABEL['from_addr']}」"
    return None


def stored_value(value: dict[str, Any], saved: Any = None) -> dict[str, Any]:
    """校验过的 mail 一组 → 落库的样子：补齐默认、去掉首尾空白；口令没给（或给 null）沿用存着的，给空串表示去掉，
    给了新的就加密。"""
    from app.core.crypto import encrypt

    old = saved if isinstance(saved, dict) else {}
    out = {**DEFAULTS, **{k: v for k, v in old.items() if k in DEFAULTS}}
    for key, item in value.items():
        if key not in DEFAULTS or key == "password":
            continue
        out[key] = item.strip() if isinstance(item, str) else item
    for key in ("host", "username", "from_addr", "from_name"):
        out[key] = _text(out.get(key))
    out["from_name"] = out["from_name"] or DEFAULTS["from_name"]
    given = value.get("password")
    if isinstance(given, str):
        out["password"] = encrypt(given) if given else None
    return out


def public_value(stored: Any) -> dict[str, Any]:
    """读设置时给的样子（GET /api/settings 的 mail 一组）：口令不回，只说配没配（password_set）。存坏了的项按默认。"""
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        item = stored.get(key)
        if key in ("enabled", "default_for_assignees"):
            out[key] = item if isinstance(item, bool) else default
        elif key == "port":
            out[key] = item if _port_ok(item) else default
        elif key == "security":
            out[key] = item if item in SECURITY else default
        elif key == "password":
            out[key] = None
        else:
            out[key] = _text(item) or default
    out["password_set"] = bool(stored.get("password"))
    return out


@dataclass(frozen=True)
class MailConfig:
    """发信用的配置（口令已解密，只在发送时用）。"""

    enabled: bool
    host: str | None
    port: int
    security: str
    username: str | None
    password: str | None
    from_addr: str | None
    from_name: str
    default_for_assignees: bool

    @property
    def ready(self) -> bool:
        """配全了能发：服务器、发件地址都有。启不启用另看 enabled。"""
        return bool(self.host and self.from_addr)


def config_of(stored: Any) -> MailConfig:
    from app.core.crypto import decrypt

    shown = public_value(stored)
    secret = stored.get("password") if isinstance(stored, dict) else None
    return MailConfig(enabled=shown["enabled"], host=shown["host"], port=shown["port"], security=shown["security"],
                      username=shown["username"], password=decrypt(secret) if isinstance(secret, str) else None,
                      from_addr=shown["from_addr"], from_name=shown["from_name"],
                      default_for_assignees=shown["default_for_assignees"])


async def load_config(session: Any = None) -> MailConfig:
    from app.db.base import SessionLocal
    from app.db.models import Setting

    if session is None:
        async with SessionLocal() as own:
            row = await own.get(Setting, GROUP)
    else:
        row = await session.get(Setting, GROUP)
    return config_of(row.value if row is not None else None)


# --------------------------------------------------------------------------
# 邮件内容
# --------------------------------------------------------------------------


_FOOTER = "这封邮件由 AgentLab 自动发出，请勿直接回复。不想再收到：在 AgentLab「设置 → 通知」里关掉或删除这个渠道"


def build(cfg: MailConfig, to: list[str], msg: Message, *, unsubscribe: str | None) -> EmailMessage:
    """一封邮件：纯文本一份、HTML 一份（全部转义）。unsubscribe 是「设置 → 通知」的完整地址，没有就不带退订头。"""
    em = EmailMessage()
    # 标题里的换行（应用名、节点名是人起的）合成空格：邮件头里有换行就是头注入，标准库也会直接拒收
    em["Subject"] = f"【AgentLab】{' '.join(msg.title.split())}"[:200]
    em["From"] = formataddr((cfg.from_name or DEFAULTS["from_name"], cfg.from_addr or ""))
    em["To"] = ", ".join(to)
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=(cfg.from_addr or "agentlab.invalid").rpartition("@")[2] or None)
    # 自动发出的通知：自动回复不该回给它（RFC 3834）
    em["Auto-Submitted"] = "auto-generated"
    if unsubscribe:
        em["List-Unsubscribe"] = f"<{unsubscribe}>"
    footer = _FOOTER + (f"：{unsubscribe}" if unsubscribe else "")
    em.set_content(f"{text_of(msg)}\n\n——\n{footer}\n")
    em.add_alternative(_html(msg, unsubscribe), subtype="html")
    return em


def _html(msg: Message, unsubscribe: str | None) -> str:
    esc = html.escape
    color = "#1a7f37" if msg.ok else "#9a6700"
    rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#666;white-space:nowrap">{esc(k)}</td>'
        f'<td style="padding:2px 0;{"color:" + color + ";font-weight:600" if k == "状态" else ""}">{esc(v)}</td></tr>'
        for k, v in msg.lines)
    links = "".join(f'<p style="margin:8px 0"><a href="{esc(url, quote=True)}">{esc(label)}</a></p>'
                    for label, url in msg.links if url.startswith(("http://", "https://")))
    tail = esc(_FOOTER) + (f'：<a href="{esc(unsubscribe, quote=True)}">设置 → 通知</a>' if unsubscribe else "")
    return ('<!doctype html><html><body style="font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;'
            'font-size:14px;line-height:1.6;color:#222">'
            f'<p style="margin:0 0 8px"><strong>【AgentLab】{esc(msg.title)}</strong></p>'
            f'<table style="border-collapse:collapse">{rows}</table>{links}'
            f'<p style="margin-top:16px;color:#888;font-size:12px">{tail}</p></body></html>')


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------


def _default_factory(host: str, port: int, security: str, timeout: float) -> Any:
    if security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def scrub(text: str, cfg: MailConfig) -> str:
    """报错里不出现口令（服务器回话里偶尔会把收到的内容原样带回来）。"""
    if cfg.password and len(cfg.password) >= 3:
        text = text.replace(cfg.password, "…")
    return text


def _reply(error: Any) -> str:
    raw = getattr(error, "smtp_error", b"")
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    return " ".join(text.split())[:200]


def _send_once(cfg: MailConfig, to: list[str], em: EmailMessage) -> Outcome:
    """连一次、发一次（同步，在线程里跑）。结果按 SMTP 的回码分：2xx 已交给邮件服务器；4xx、连不上值得再试；5xx 不再试。"""
    host = f"{cfg.host}:{cfg.port}"
    factory = _smtp_factory or _default_factory
    client = None
    try:
        client = factory(cfg.host or "", cfg.port, cfg.security, TIMEOUT_S)
        if cfg.security == "starttls":
            client.starttls(context=ssl.create_default_context())
        if cfg.username:
            client.login(cfg.username, cfg.password or "")
        refused = client.send_message(em, from_addr=cfg.from_addr, to_addrs=to) or {}
    except smtplib.SMTPNotSupportedError:
        return Outcome(False, None, 1, f"邮件服务器 {host} 不支持 STARTTLS：请把「加密方式」改成 SSL/TLS 或不加密")
    except smtplib.SMTPRecipientsRefused as e:
        codes = [code for code, _ in (e.recipients or {}).values()]
        temporary = any(400 <= int(c) < 500 for c in codes)
        return Outcome(False, None, 1, f"邮件服务器 {host} 拒收了全部 {len(codes) or len(to)} 个收件地址"
                       + ("（临时错误）" if temporary else ""), retryable=temporary)
    except smtplib.SMTPAuthenticationError as e:
        code = int(e.smtp_code or 0)
        return Outcome(False, None, 1, scrub(f"邮件服务器 {host} 不接受这组用户名和口令（{code}）"
                                             + (f"：{_reply(e)}" if _reply(e) else ""), cfg)[:300],
                       retryable=400 <= code < 500)
    except smtplib.SMTPResponseException as e:
        code = int(e.smtp_code or 0)
        detail = _reply(e)
        text = scrub(f"邮件服务器 {host} 回复 {code}" + (f"：{detail}" if detail else ""), cfg)[:300]
        # SMTPConnectError 也走这里（连上了但对方回了错误码）；回码不是 5xx 的都当临时错误
        return Outcome(False, None, 1, text, retryable=not 500 <= code < 600)
    except smtplib.SMTPServerDisconnected:
        return Outcome(False, None, 1, f"邮件服务器 {host} 断开了连接", retryable=True)
    except ssl.SSLError:
        return Outcome(False, None, 1, f"和邮件服务器 {host} 建立加密连接失败：请核对「加密方式」和端口（SSL/TLS 常用 465，"
                                       "STARTTLS 常用 587）")
    except (socket.timeout, TimeoutError):
        return Outcome(False, None, 1, f"连接邮件服务器 {host} 超时（{TIMEOUT_S:g} 秒）", retryable=True)
    except socket.gaierror:
        return Outcome(False, None, 1, f"找不到邮件服务器 {cfg.host}（域名无法解析）", retryable=True)
    except OSError:
        return Outcome(False, None, 1, f"无法连接邮件服务器 {host}", retryable=True)
    except Exception as e:  # noqa: BLE001 - 拼信、编码出错也只记一条失败，不让推送任务抛出去
        logger.exception("发邮件时出错（%s）", host)
        return Outcome(False, None, 1, f"向邮件服务器 {host} 发信时出错（{type(e).__name__}）")
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - 已经断开的连接 quit 会抛，关掉就行
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
    if refused:
        # 部分地址被拒：其余的已经交给邮件服务器，算成功，照实写有几个没收
        return Outcome(True, None, 1, f"{HANDED_OFF}，其中 {len(refused)} 个收件地址被拒收")
    return Outcome(True, None, 1)


async def send(cfg: MailConfig, to: list[str], msg: Message, *, label: str, unsubscribe: str | None = None,
               retry: bool = True, delays: tuple[float, ...] | None = None) -> Outcome:
    """发一封邮件（含重试）。label 是日志里的「谁的推送」。没配全的照实回失败，不连服务器。"""
    if not cfg.ready:
        return Outcome(False, None, 0, NOT_ENABLED)
    em = build(cfg, to, msg, unsubscribe=unsubscribe)
    waits = (RETRY_DELAYS if delays is None else delays) if retry else ()
    last = Outcome(False, None, 0, "没有发出邮件")
    for attempt in range(len(waits) + 1):
        if attempt:
            await asyncio.sleep(waits[attempt - 1])
        last = await asyncio.to_thread(_send_once, cfg, to, em)
        last.attempts = attempt + 1
        if last.ok:
            return last
        logger.warning("%s（邮件服务器 %s）第 %d 次发信失败：%s", label, cfg.host, attempt + 1, last.error)
        if not last.retryable:
            break
    return last


async def recipients_of(session: Any, channel: Any) -> tuple[list[str], str | None]:
    """这个邮件渠道发给谁：(地址, 发不了的原因)。个人渠道取拥有者的账号邮箱，其余解密填的那几个。"""
    from app.core.crypto import decrypt
    from app.db.models import User

    if getattr(channel, "scope", None) == "user":
        user = await session.get(User, channel.owner_id) if channel.owner_id else None
        email = (user.email or "").strip() if user is not None else ""
        if not email or not address_ok(email):
            return [], NO_EMAIL
        return [email], None
    try:
        return parse_recipients(decrypt(channel.url) or ""), None
    except ValueError as e:
        return [], f"收件地址无法使用：{e}"


async def deliver(channel: Any, message: dict[str, Any], msg: Message) -> Outcome:
    """按渠道发一封邮件（notify/__init__.send 的邮件分支）：配置没启用、收件人取不出来都记成失败、写明原因。"""
    from app.core import public_url
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        cfg = await load_config(session)
        to, problem = await recipients_of(session, channel)
        base = await public_url.refresh(session)
    if not cfg.enabled:
        return Outcome(False, None, 0, NOT_ENABLED)
    if problem:
        return Outcome(False, None, 0, problem)
    return await send(cfg, to, msg, label=f"通知渠道「{channel.name}」",
                      unsubscribe=f"{base}/settings/notify" if base else None,
                      retry=bool(message.get("retry", True)))
