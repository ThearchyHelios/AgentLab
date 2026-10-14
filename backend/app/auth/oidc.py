"""单点登录：通用 OIDC，授权码 + PKCE（A7 第 1 条，Q5：只做通用 OIDC，飞书、企业微信这类各自的协议以后按需加适配）。

一次登录：
1. /oidc/start：生成 state、nonce、code_verifier，用 core/crypto 加密后放进 10 分钟的短时 cookie（只在回调路径上带），
   跳转到身份提供方的授权地址；
2. /oidc/callback：核对 state，cookie 当场作废（同一次授权不能回放）；用授权码 + verifier 换 token；用 PyJWKClient 取
   签名公钥核 id_token（签名、iss、aud、exp），nonce 必须和 cookie 里的一致；
3. 按 (issuer, subject) 找或建用户；角色按映射算（管理员组、管理员用户名、搭建者组），每次登录都重算；
   groups 声明同步成 source=oidc 的组（只读），只增减这个用户在 oidc 组里的成员关系，本地组不碰。

设置（settings 表 auth.oidc）：issuer、client_id、client_secret（加密）、scopes、username_claim、groups_claim、
admin_groups、admin_users、builder_groups。切到 oidc 时至少要有一个管理员映射（api/auth.py 检查）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto

STATE_COOKIE = "agentlab_oidc"
STATE_PATH = "/api/auth/oidc"
STATE_TTL_S = 600
_DISCOVERY_TTL_S = 3600
_discovery: dict[str, tuple[float, dict[str, Any]]] = {}
_jwk_clients: dict[str, jwt.PyJWKClient] = {}


class OidcError(ValueError):
    """单点登录没有完成：给人看的一句话。"""


def _client() -> httpx.AsyncClient:
    # 不走环境变量里的代理：身份提供方多在内网，走代理反而连不上
    return httpx.AsyncClient(timeout=10, trust_env=False)


async def discover(issuer: str) -> dict[str, Any]:
    issuer = issuer.rstrip("/")
    hit = _discovery.get(issuer)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        async with _client() as client:
            resp = await client.get(f"{issuer}/.well-known/openid-configuration")
            resp.raise_for_status()
            doc = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise OidcError(f"无法读取身份提供方的配置（{issuer}）：{type(e).__name__}") from e
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(key):
            raise OidcError(f"身份提供方的配置缺少 {key}")
    _discovery[issuer] = (time.monotonic() + _DISCOVERY_TTL_S, doc)
    return doc


class _JwkClient(jwt.PyJWKClient):
    """取公钥不走环境变量里的代理（PyJWKClient 自带的 urllib 会读系统代理设置）。"""

    def fetch_data(self) -> Any:
        with httpx.Client(timeout=10, trust_env=False) as client:
            resp = client.get(self.uri)
            resp.raise_for_status()
            data = resp.json()
        if self.jwk_set_cache is not None:
            self.jwk_set_cache.put(data)
        return data


def _jwk_client(uri: str) -> jwt.PyJWKClient:
    if uri not in _jwk_clients:
        _jwk_clients[uri] = _JwkClient(uri, cache_keys=True)
    return _jwk_clients[uri]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def safe_next(target: str | None) -> str:
    """登录后跳回哪里：只认站内的相对路径，免得被拿来跳去别的站点。"""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


async def start(config: dict[str, Any], redirect_uri: str, next_path: str | None) -> tuple[str, str]:
    """交回 (授权地址, 加密后的 state cookie 值)。"""
    doc = await discover(config["issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {"response_type": "code", "client_id": config["client_id"], "redirect_uri": redirect_uri,
              "scope": config.get("scopes") or "openid profile email", "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    cookie = crypto.encrypt(json.dumps({"state": state, "nonce": nonce, "verifier": verifier,
                                        "next": safe_next(next_path), "at": int(time.time())})) or ""
    return f"{doc['authorization_endpoint']}?{urlencode(params)}", cookie


def read_state(cookie: str | None, state: str | None) -> dict[str, Any]:
    if not cookie:
        raise OidcError("登录请求已失效，请重新登录")
    try:
        data = json.loads(crypto.decrypt(cookie) or "")
    except ValueError as e:
        raise OidcError("登录请求已失效，请重新登录") from e
    if not state or not secrets.compare_digest(str(data.get("state") or ""), state):
        raise OidcError("登录请求对不上，请重新登录")
    if int(time.time()) - int(data.get("at") or 0) > STATE_TTL_S:
        raise OidcError("登录请求已超时，请重新登录")
    return data


async def exchange(config: dict[str, Any], code: str, redirect_uri: str, verifier: str, nonce: str
                   ) -> dict[str, Any]:
    """授权码换 token，核 id_token，交回其中的声明。"""
    doc = await discover(config["issuer"])
    secret = crypto.decrypt(config.get("client_secret")) or ""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": config["client_id"], "code_verifier": verifier}
    if secret:
        form["client_secret"] = secret
    try:
        async with _client() as client:
            resp = await client.post(doc["token_endpoint"], data=form, headers={"accept": "application/json"})
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise OidcError(f"无法向身份提供方换取登录凭据：{type(e).__name__}") from e
    if resp.status_code != 200 or not body.get("id_token"):
        raise OidcError(f"身份提供方拒绝了这次登录：{body.get('error_description') or body.get('error') or resp.status_code}")
    token = body["id_token"]
    try:
        key = await asyncio.to_thread(_jwk_client(doc["jwks_uri"]).get_signing_key_from_jwt, token)
        claims = jwt.decode(token, key.key, algorithms=doc.get("id_token_signing_alg_values_supported") or ["RS256"],
                            audience=config["client_id"], issuer=doc.get("issuer") or config["issuer"].rstrip("/"))
    except jwt.PyJWTError as e:
        raise OidcError(f"登录凭据核验失败：{e}") from e
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise OidcError("登录凭据对不上这次请求，请重新登录")
    return claims


def _groups(claims: dict[str, Any], config: dict[str, Any]) -> list[str]:
    raw = claims.get(config.get("groups_claim") or "groups") or []
    if isinstance(raw, str):
        raw = [raw]
    return sorted({str(g).strip() for g in raw if str(g).strip()})


def role_for(username: str, groups: list[str], config: dict[str, Any]) -> str:
    if username in (config.get("admin_users") or []) or set(groups) & set(config.get("admin_groups") or []):
        return "admin"
    if set(groups) & set(config.get("builder_groups") or []):
        return "builder"
    return "user"


async def upsert_user(session: AsyncSession, claims: dict[str, Any], config: dict[str, Any]) -> Any:
    """按 (issuer, subject) 找或建用户，按映射定角色，同步 oidc 组。不提交。"""
    from app.db.models import User

    issuer = str(claims.get("iss") or config["issuer"]).rstrip("/")
    subject = str(claims.get("sub") or "")
    if not subject:
        raise OidcError("登录凭据里没有用户标识")
    username = str(claims.get(config.get("username_claim") or "preferred_username")
                   or claims.get("preferred_username") or claims.get("email") or subject)[:100]
    groups = _groups(claims, config)
    user = (await session.execute(select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject))
            ).scalar_one_or_none()
    if user is None:
        taken = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if taken is not None:
            # 同名的本地账号不自动并过来：那等于让身份提供方那边同名的人接管这个账号
            raise OidcError(f"用户名「{username}」已被本地账号占用，请联系管理员处理")
        user = User(username=username, oidc_issuer=issuer, oidc_subject=subject, created_by="oidc")
        session.add(user)
    if user.disabled:
        raise OidcError("这个账号已被停用，请联系管理员")
    user.display_name = str(claims.get("name") or user.display_name or username)[:100]
    user.email = str(claims.get("email") or user.email or "")[:200] or None
    user.role = role_for(user.username, groups, config)
    await session.flush()
    await _sync_groups(session, user.id, groups)
    return user


async def _sync_groups(session: AsyncSession, user_id: str, names: list[str]) -> None:
    from app.db.models import Group, GroupMember

    existing = {g.name: g for g in (await session.execute(select(Group).where(Group.name.in_(names)))).scalars()} \
        if names else {}
    wanted: set[str] = set()
    for name in names:
        group = existing.get(name)
        if group is None:
            group = Group(name=name, label=name, source="oidc")
            session.add(group)
            await session.flush()
        if group.source == "oidc":
            # 本地同名的组不碰：它的成员由管理员维护
            wanted.add(group.id)
    current = {m.group_id: m for m in (await session.execute(
        select(GroupMember).join(Group, Group.id == GroupMember.group_id)
        .where(GroupMember.user_id == user_id, Group.source == "oidc"))).scalars()}
    for group_id, member in current.items():
        if group_id not in wanted:
            await session.delete(member)
    for group_id in wanted - set(current):
        session.add(GroupMember(group_id=group_id, user_id=user_id, added_by="oidc"))
