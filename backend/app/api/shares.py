"""分享链接（docs/design/next-round/04-对外集成与触达.md C2）。

读分享页的接口不用登录，只认请求头里的分享 key（中间件把 /api/shares/ 当公开前缀，第 3 波 W3-d 加）；建、撤销分享在
/api/apps/{id}/shares 下，要应用的 manage 权限。第 3 波 W3-d 填。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/shares", tags=["shares"])
