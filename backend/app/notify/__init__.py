"""通知的发送核心（附录 C，BE-2 提供）。从 scheduler/webhook.py 抽出：四种格式、签名、发送、脱敏；
webhook.py 照原名再导出，定时任务的通知存储不迁移（F14）。新对象（待办、实例、动作）写 notify_deliveries。"""
from __future__ import annotations

from typing import Any


async def send(channel: Any, message: dict[str, Any]) -> Any:
    """按渠道的格式发一条通知。message = {title, status, lines: [...], links: [(文字, 地址)]}，不含报告里的具体数字。"""
    raise NotImplementedError("BE-2")
