"""最近一次测连接的结果：数据源、模型接入、MCP 服务共用的一套记法。

以前这份结果只在浏览器的 localStorage 里，换台电脑就全回到「未测试」。现在记在
对象上（数据源、MCP 是 last_check 列，模型接入是 extra.last_check），列表接口
平铺成 last_checked_at / last_check_ok / last_latency_ms / last_error 四个字段。

表单里「先测再保存」的那一次也要算数：测的是草稿，保存时后端按连接配置的指纹
认出「刚测过的就是这一份」，把结果带到新对象上。指纹只在进程里记一小会儿，
用的是哈希，不留明文密码。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.db.base import utcnow

#: 草稿测完多久之内保存还算数。测完去倒杯水回来再点保存，也该认得出来
_DRAFT_TTL = 15 * 60
_DRAFT_MAX = 64
_drafts: dict[str, tuple[float, dict[str, Any]]] = {}


def record(ok: bool, latency_ms: int | None, error: str | None = None) -> dict[str, Any]:
    return {
        "at": utcnow().isoformat(),
        "ok": bool(ok),
        "latency_ms": None if latency_ms is None else int(latency_ms),
        "error": None if ok else (error or "连不上"),
    }


def fields(last: dict[str, Any] | None) -> dict[str, Any]:
    """对象上记的那份 → 列表接口的四个字段。没测过全是 None。"""
    last = last if isinstance(last, dict) else {}
    ok = last.get("ok")
    return {
        "last_checked_at": last.get("at"),
        "last_check_ok": ok if isinstance(ok, bool) else None,
        "last_latency_ms": last.get("latency_ms"),
        "last_error": last.get("error"),
    }


def fingerprint(*parts: Any) -> str:
    """连接配置的指纹。同一份配置得出同一个值；只拿来比对，不可逆。"""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def remember_draft(key: str | None, last: dict[str, Any]) -> None:
    if not key:
        return
    now = time.monotonic()
    for stale in [k for k, (at, _) in _drafts.items() if now - at > _DRAFT_TTL]:
        _drafts.pop(stale, None)
    if len(_drafts) >= _DRAFT_MAX:
        _drafts.pop(min(_drafts, key=lambda k: _drafts[k][0]), None)
    _drafts[key] = (now, last)


def draft_result(key: str | None) -> dict[str, Any] | None:
    """这份配置最近在表单里测过的结果，没有或太久了返回 None。"""
    hit = _drafts.get(key or "")
    if not hit or time.monotonic() - hit[0] > _DRAFT_TTL:
        return None
    return dict(hit[1])
