"""通知的发送核心（附录 C，BE-2 提供）。从 scheduler/webhook.py 抽出：四种格式、签名、发送、脱敏（core.py）；
webhook.py 照原名再导出，定时任务的通知存储不迁移（F14）。新对象（待办、实例、动作）的订阅和投递记录在
service.py：渠道（notify_channels）、投递记录（notify_deliveries）。"""
from __future__ import annotations

from typing import Any

from app.notify.core import Message, Outcome


async def send(channel: Any, message: dict[str, Any]) -> Any:
    """按渠道的格式发一条通知。message = {title, status, lines: [...], links: [(文字, 地址)]}，不含报告里的具体数字。

    channel 是一行 NotifyChannel（地址、密钥是密文）。返回 core.Outcome。"""
    from app.core.crypto import decrypt
    from app.notify import core
    from app.notify.service import generic_body

    url = decrypt(channel.url) or ""
    secret = decrypt(channel.secret) if channel.secret else None
    msg = Message(title=str(message.get("title") or ""),
                  lines=[(str(k), str(v)) for k, v in message.get("lines") or []],
                  links=[(str(k), str(v)) for k, v in message.get("links") or []],
                  ok=bool(message.get("ok")))
    if not url:
        return Outcome(False, None, 0, "渠道的地址无法解密（加密密钥可能已更换），请重新填写地址")
    if getattr(channel, "scope", None) == "user":
        from app.notify.service import personal_url_problem

        if problem := await personal_url_problem(url):
            return Outcome(False, None, 0, problem)
    return await core.post(channel.format, url, secret,
                           lambda: core.build_request(channel.format, url, secret, msg,
                                                      generic=generic_body(message, msg)),
                           label=f"通知渠道「{channel.name}」", retry=bool(message.get("retry", True)))
