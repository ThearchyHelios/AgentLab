"""身份接口（BE-1）：/me、登录、登出、OIDC、登录方式设置、引导第一个管理员、JWKS。设计文档 §3.10。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/auth", tags=["auth"])
