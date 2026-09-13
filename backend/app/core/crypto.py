from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_PREFIX = "enc::"


def _load_key() -> bytes:
    """取加密密钥：优先用配置里的 secret_key，否则在 data_dir 生成一个持久化的。"""
    raw = settings.secret_key
    if not raw:
        key_file = settings.data_dir / ".secret_key"
        if key_file.exists():
            raw = key_file.read_text().strip()
        else:
            raw = base64.urlsafe_b64encode(os.urandom(32)).decode()
            key_file.write_text(raw)
            key_file.chmod(0o600)
    # 任意字符串都归一化成 32 字节 Fernet key
    digest = hashlib.sha256(raw.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_load_key())


def encrypt(value: str | None) -> str | None:
    """加密明文。已经是密文的原样返回，方便重复保存同一条记录。"""
    if not value:
        return value
    if value.startswith(_PREFIX):
        return value
    return _PREFIX + _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return value
    if not value.startswith(_PREFIX):
        return value  # 兼容历史明文
    try:
        return _fernet.decrypt(value[len(_PREFIX) :].encode()).decode()
    except InvalidToken:
        return None


def mask(value: str | None) -> str:
    """给前端展示用的掩码，永远不把明文 key 发回浏览器。"""
    if not value:
        return ""
    plain = decrypt(value) or ""
    if len(plain) <= 8:
        return "••••"
    return f"{plain[:4]}••••{plain[-4:]}"
