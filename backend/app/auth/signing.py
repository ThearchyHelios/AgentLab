"""AgentLab 签发的身份断言（凭据模式 service_on_behalf，A6 第 3 条、Q3）。

登记的端点用服务凭据访问外部系统；声明了 service_on_behalf 的能力另带一个短时的 EdDSA JWT，写明这次是替谁办的：
iss=agentlab、aud=端点名、sub=运行的发起人（有的话）、5 分钟有效。外部系统用 /api/auth/jwks 里的公钥核验。

sub 一律是运行的发起人（Run.started_by）：这件事是替他办的，哪怕流程走了几天、中间换了几个人经手。确认这次发送的人
另放在 act 声明里（RFC 8693 的 actor 声明：{"act": {"sub": 确认人}}）——外部系统据此分得清「替谁办」和「谁按的
确认」，不会把确认人当成申请人。没有人确认（只读、免确认的无人值守写入）就不写 act。

不存用户自己的 token：流程一等就是几天，会超过它的寿命；存刷新令牌会扩大泄露面。

私钥在数据目录里生成一次、之后一直用它（0600，和 .secret_key 放在一起）：换了钥匙，外部系统缓存的公钥就核不过。
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core import atomic
from app.core.config import settings

#: 断言放在这个请求头里
ASSERTION_HEADER = "X-AgentLab-Assertion"
ISSUER = "agentlab"
TTL_S = 300
_KEY_FILE = ".assertion_ed25519.pem"


@lru_cache(maxsize=1)
def _private_key() -> Ed25519PrivateKey:
    path = settings.data_dir / _KEY_FILE
    if not path.exists():
        # 只建一次（core/atomic.create_bytes）：几个进程同时第一次签名，后到的读先到的那一把，验签的一方只认得一把
        fresh = Ed25519PrivateKey.generate()
        atomic.create_bytes(path, fresh.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()), mode=0o600)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def _public_raw() -> bytes:
    return _private_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def key_id() -> str:
    return hashlib.sha256(_public_raw()).hexdigest()[:16]


def jwks() -> dict[str, Any]:
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": _b64(_public_raw()), "kid": key_id(),
                      "alg": "EdDSA", "use": "sig"}]}


def assertion(audience: str, subject: str | None, *, verified: bool, actor: str | None = None) -> str:
    """一次请求的身份断言。subject 为空（定时任务这类没有发起人的运行）时不写 sub，外部系统据此认出是服务自己发起的。
    actor 是确认这次发送的人，写成 act 声明；发起人自己确认的也照写（谁按的确认要说得清），没给就不写。"""
    import jwt

    now = int(time.time())
    claims: dict[str, Any] = {"iss": ISSUER, "aud": audience, "iat": now, "exp": now + TTL_S,
                              "jti": secrets.token_hex(12), "verified": verified}
    if subject:
        claims["sub"] = subject
    if actor:
        claims["act"] = {"sub": actor}
    return jwt.encode(claims, _private_key(), algorithm="EdDSA", headers={"kid": key_id()})
