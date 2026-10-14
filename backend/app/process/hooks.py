"""入站签名钩子（附录 C，BE-2 提供）。签名同出站通用格式：HMAC-SHA256(f"{ts}." + body)，时间窗 5 分钟，
Idempotency-Key 去重。start 按入口发起实例；signal 按 correlation 解决一条「等外部系统」的待办。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 签名时间窗（秒）
WINDOW_S = 300


@dataclass(frozen=True)
class HookRequest:
    hook_id: str
    kind: str
    idem_key: str
    payload: dict[str, Any]


class HookRejected(Exception):
    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


def verify(secret: str, headers: dict[str, str], body: bytes) -> None:
    """签名不对、时间窗外抛 HookRejected。"""
    raise NotImplementedError("BE-2")
