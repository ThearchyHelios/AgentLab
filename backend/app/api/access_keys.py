"""访问密钥与服务账号（docs/design/next-round/04-对外集成与触达.md A1–A7）。

main.py 挂登录即可（require_role("user")）：本人的个人密钥本人管，服务账号和全部密钥在接口里要管理员。第 2 波 W2-d 填。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/access-keys", tags=["access-keys"])
