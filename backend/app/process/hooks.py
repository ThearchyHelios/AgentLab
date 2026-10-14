"""入站签名钩子（附录 C，BE-2 提供）。签名同出站通用格式：HMAC-SHA256(f"{ts}." + body)，时间窗 5 分钟，
Idempotency-Key 去重。start 按入口发起实例；signal 按 correlation 解决一条「等外部系统」的待办。

请求头：
- X-AgentLab-Timestamp：秒级 Unix 时间戳；
- X-AgentLab-Signature：sha256=<十六进制签名>，签的是「时间戳.请求体」的原始字节；
- Idempotency-Key：同一个键只处理一次。处理成功的，再来同一个键直接交回上次的结果；被拒的（表单不对、没有在等的待办）
  可以用同一个键重试——外部系统多半会原样重发，拒过一次就永远拒，它只能换键，去重也就落空了。处理中途服务停了的
  （回执停在 processing 超过 STALE_S），同一个键可以重新认领：start 先找这个键已经发起的实例，signal 先找这个关联键
  已经解决的待办，找到就交回它，不重复发起、不重复回调。

幂等键不在签名串里（签名和出站通用格式是同一套），所以同一个签名只认一个幂等键（_bind_signature）：截获一个合法请求、
在时间窗里换个幂等键重放，签名对得上也拒。原样重发（同一个签名、同一个键）照常按幂等键交回上次的结果。

signal 的请求体：{"correlation": 关联键, "decision"?: "approve" | "reject", "note"?: …, 其余字段原样交给节点}。关联键
也可以放在 X-AgentLab-Correlation 头里。

钩子的增删放在 /api/apps/{id}/hooks（应用包的接口），这里给 create_hook / list_hooks / delete_hook 三个函数；
密钥只在新建时交回一次，库里存密文。
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal, utcnow
from app.db.models import AppHook, HookReceipt, Task

logger = logging.getLogger(__name__)

#: 签名时间窗（秒）
WINDOW_S = 300
#: 回执停在 processing 超过这么久（秒），同一个幂等键可以重新认领：处理一次请求用不了这么久，多半是中途服务停了
STALE_S = 600
#: 「这个签名认了哪个幂等键」的记录也放在回执表里（idem_key 是这个前缀加时间戳和签名，detail 是认的幂等键）：同一个
#: 唯一约束管两件事，不另起表；过了两个时间窗就删
SIGNATURE_PREFIX = "sig:"
IDEMPOTENCY_HEADER = "Idempotency-Key"
CORRELATION_HEADER = "X-AgentLab-Correlation"
KINDS = ("start", "signal")


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


def _header(headers: Any, name: str) -> str:
    """请求头不分大小写（Starlette 的 Headers 本来就不分；普通 dict 按小写再找一遍）。"""
    value = headers.get(name)
    if value is None and isinstance(headers, dict):
        lowered = {str(k).lower(): v for k, v in headers.items()}
        value = lowered.get(name.lower())
    return str(value or "").strip()


def verify(secret: str, headers: dict[str, str], body: bytes) -> None:
    """签名不对、时间窗外抛 HookRejected。"""
    from app.notify.core import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

    ts = _header(headers, TIMESTAMP_HEADER)
    given = _header(headers, SIGNATURE_HEADER).removeprefix("sha256=")
    if not ts or not given:
        raise HookRejected("请求缺少签名，请按通用格式在请求头里带上时间戳和签名")
    try:
        moment = int(ts)
    except ValueError:
        raise HookRejected("签名的时间戳不是秒级的 Unix 时间戳") from None
    if abs(time.time() - moment) > WINDOW_S:
        raise HookRejected("签名的时间戳超出 5 分钟的时间窗，请用当前时间重新签名")
    if not hmac.compare_digest(sign(secret, ts, body), given):
        raise HookRejected("签名不正确")


async def receive(hook_id: str, headers: Any, body: bytes) -> dict[str, Any]:
    """处理一次入站请求：认钩子 → 验签 → 按幂等键认领 → start 发起实例 / signal 解决待办。

    返回 {status: accepted, kind, run_id?, task_id?, duplicate?}。被拒抛 HookRejected（带 HTTP 状态码）；入口表单校验
    不通过抛 apps.entries.FormRejected。"""
    from app.core.crypto import decrypt

    async with SessionLocal() as session:
        hook = await session.get(AppHook, hook_id)
        if hook is None or not hook.enabled:
            raise HookRejected("钩子不存在或已停用", status_code=404)
        secret = decrypt(hook.secret) or ""
    if not secret:
        raise HookRejected("钩子的密钥无法解密（加密密钥可能已更换），请在应用里重新生成钩子", status_code=409)
    verify(secret, headers, body)
    idem_key = _header(headers, IDEMPOTENCY_HEADER)[:200]
    if not idem_key:
        raise HookRejected(f"请求缺少 {IDEMPOTENCY_HEADER} 请求头：同一件事重发时要带同一个键", status_code=400)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HookRejected("请求体不是 JSON", status_code=400) from None
    if not isinstance(payload, dict):
        raise HookRejected("请求体需要是一个 JSON 对象", status_code=400)

    from app.notify.core import SIGNATURE_HEADER, TIMESTAMP_HEADER

    await _bind_signature(hook.id, _header(headers, TIMESTAMP_HEADER),
                          _header(headers, SIGNATURE_HEADER).removeprefix("sha256="), idem_key)
    previous, reclaimed = await _claim(hook.id, idem_key)
    if previous is not None:
        return previous
    request = HookRequest(hook_id=hook.id, kind=hook.kind, idem_key=idem_key, payload=payload)
    try:
        if hook.kind == "start":
            out = await _start(hook, request, reclaimed=reclaimed)
        else:
            out = await _signal(hook, request, _header(headers, CORRELATION_HEADER), reclaimed=reclaimed)
    except Exception as e:
        await _finish(hook.id, idem_key, status="rejected", detail=str(e)[:500])
        raise
    await _finish(hook.id, idem_key, status="accepted", run_id=out.get("run_id"), task_id=out.get("task_id"))
    return {"status": "accepted", "kind": hook.kind, **out}


async def _bind_signature(hook_id: str, ts: str, signature: str, idem_key: str) -> None:
    """同一个签名只认一个幂等键（见模块说明）。头一回见到这个签名就记下它认的键；再见到时键对不上就拒。顺带删掉过了
    两个时间窗的记录：时间窗外的签名验签那一步就拒了，记着没用。"""
    from datetime import timedelta

    from sqlalchemy import delete

    key = f"{SIGNATURE_PREFIX}{ts}.{signature}"[:200]
    async with SessionLocal() as session:
        await session.execute(delete(HookReceipt).where(
            HookReceipt.hook_id == hook_id, HookReceipt.status == "signature",
            HookReceipt.received_at < utcnow() - timedelta(seconds=2 * WINDOW_S)))
        session.add(HookReceipt(hook_id=hook_id, idem_key=key, status="signature", detail=idem_key))
        try:
            await session.commit()
            return
        except IntegrityError:
            await session.rollback()
        bound = await session.scalar(select(HookReceipt.detail).where(HookReceipt.hook_id == hook_id,
                                                                      HookReceipt.idem_key == key))
    if bound != idem_key:
        raise HookRejected("这个签名已经用过：同一个请求重发时要带同一个幂等键，新的请求请重新签名")


async def _claim(hook_id: str, idem_key: str) -> tuple[dict[str, Any] | None, bool]:
    """按 (钩子, 幂等键) 认领这一次，交回 (上次的结果, 是不是重新认领了一条停在处理中的)。处理成功过的交回上次的结果
    （duplicate: true）；被拒过的重新认领；正在处理的 409，停在处理中超过 STALE_S 的重新认领（条件 UPDATE，并发的两次
    只有一次认领得到）。"""
    from datetime import timedelta

    async with SessionLocal() as session:
        session.add(HookReceipt(hook_id=hook_id, idem_key=idem_key, status="processing"))
        try:
            await session.commit()
            return None, False
        except IntegrityError:
            await session.rollback()
        row = await session.scalar(select(HookReceipt).where(HookReceipt.hook_id == hook_id,
                                                             HookReceipt.idem_key == idem_key))
        if row.status == "accepted":
            return {"status": "accepted", "duplicate": True, "run_id": row.run_id, "task_id": row.task_id}, False
        stale = row.status == "processing" and row.received_at < utcnow() - timedelta(seconds=STALE_S)
        if row.status == "rejected" or stale:
            again = await session.execute(update(HookReceipt).where(
                HookReceipt.id == row.id, HookReceipt.status == row.status,
                HookReceipt.received_at == row.received_at)
                .values(status="processing", detail=None, received_at=utcnow()))
            await session.commit()
            if again.rowcount == 1:
                return None, stale
    raise HookRejected("同一个幂等键的请求正在处理，请稍后再查", status_code=409)


async def _finish(hook_id: str, idem_key: str, *, status: str, detail: str | None = None,
                  run_id: str | None = None, task_id: str | None = None) -> None:
    async with SessionLocal() as session:
        await session.execute(update(HookReceipt).where(HookReceipt.hook_id == hook_id,
                                                        HookReceipt.idem_key == idem_key)
                              .values(status=status, detail=detail, run_id=run_id, task_id=task_id))
        await session.commit()


def _caller(hook: AppHook) -> Any:
    """钩子发起、回复时的身份：签名认过的外部系统。没有用户名（发起人一栏留空），名字写钩子名，审计里记 verified。"""
    from app.api.deps import Principal, auth_mode

    return Principal(username=None, role="admin", display_name=f"外部系统（{hook.name}）", verified=True,
                     mode=auth_mode())


async def _start(hook: AppHook, request: HookRequest, *, reclaimed: bool = False) -> dict[str, Any]:
    from app.apps.entries import start_entry

    if not hook.entry_id:
        raise HookRejected("这个钩子没有指定入口，无法发起", status_code=409)
    if reclaimed and (run_id := await _started_before(hook, request.idem_key)):
        # 上一次处理到一半服务停了：实例其实已经发起，交回它，不再发起一个
        return {"run_id": run_id}
    run = await start_entry(hook.app_id, hook.entry_id, request.payload, _caller(hook), origin_kind="hook",
                            hook=hook.name, hook_key=request.idem_key)
    return {"run_id": run.id}


async def _started_before(hook: AppHook, idem_key: str) -> str | None:
    """这个钩子按这个幂等键发起过的实例（来源里记着 hook 和 hook_key）。"""
    from app.db.models import Run

    async with SessionLocal() as session:
        rows = (await session.execute(select(Run.id, Run.origin).where(
            Run.app_id == hook.app_id, Run.origin_kind == "hook").order_by(Run.created_at.desc()).limit(500))).all()
    for run_id, origin in rows:
        if isinstance(origin, dict) and origin.get("hook") == hook.name and origin.get("hook_key") == idem_key:
            return run_id
    return None


async def _signal(hook: AppHook, request: HookRequest, header_key: str, *, reclaimed: bool = False) -> dict[str, Any]:
    from app.process.tasks import RawResponse, respond

    key = str(request.payload.get("correlation") or header_key or "").strip()
    if not key:
        raise HookRejected("请求里没有关联键（correlation）", status_code=400)
    async with SessionLocal() as session:
        task_id = await session.scalar(select(Task.id).where(
            Task.app_id == hook.app_id, Task.kind == "signal", Task.status == "pending",
            Task.correlation_key == key).order_by(Task.created_at).limit(1))
        if task_id is None and reclaimed:
            # 上一次处理到一半服务停了：在等的待办其实已经解决，交回它，不当成没人在等
            done = (await session.execute(select(Task.id, Task.run_id).where(
                Task.app_id == hook.app_id, Task.kind == "signal", Task.status != "pending",
                Task.correlation_key == key).order_by(Task.created_at.desc()).limit(1))).first()
            if done is not None:
                return {"task_id": done[0], "run_id": done[1]}
    if task_id is None:
        raise HookRejected("没有在等这个关联键的待办（可能已处理过，或还没走到这一步）", status_code=404)
    decision = str(request.payload.get("decision") or "approve").lower()
    value = {k: v for k, v in request.payload.items() if k not in ("correlation", "decision", "note")}
    response = {"approved": decision not in ("reject", "rejected"), "note": str(request.payload.get("note") or ""),
                "value": value, "via": "hook", "hook": hook.name}
    out = await respond(task_id, _caller(hook), decision=RawResponse(response))
    return {"task_id": task_id, "run_id": getattr(out.get("run"), "id", None)}


# --------------------------------------------------------------------------
# 钩子的增删（给 /api/apps/{id}/hooks 用）
# --------------------------------------------------------------------------


def hook_out(row: AppHook) -> dict[str, Any]:
    return {"id": row.id, "app_id": row.app_id, "name": row.name, "kind": row.kind, "entry_id": row.entry_id,
            "enabled": row.enabled, "url_path": f"/api/hooks/{row.id}",
            "created_at": row.created_at.isoformat() if row.created_at else None}


async def create_hook(session: AsyncSession, app_id: str, *, name: str, kind: str,
                      entry_id: str | None = None) -> tuple[AppHook, str]:
    """新建一个钩子，返回 (钩子, 密钥明文)。密钥只在这一次交回，库里存密文。调用方提交。"""
    from app.core.crypto import encrypt

    if kind not in KINDS:
        raise ValueError("钩子只能是发起实例或回调待办两种")
    if kind == "start" and not entry_id:
        raise ValueError("发起实例的钩子需要指定入口")
    secret = secrets.token_urlsafe(32)
    row = AppHook(app_id=app_id, name=name.strip()[:100] or kind, kind=kind,
                  entry_id=entry_id if kind == "start" else None, secret=encrypt(secret), enabled=True)
    session.add(row)
    await session.flush()
    return row, secret


async def list_hooks(session: AsyncSession, app_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(select(AppHook).where(AppHook.app_id == app_id)
                                  .order_by(AppHook.created_at))).scalars()
    return [hook_out(r) for r in rows]


async def delete_hook(session: AsyncSession, app_id: str, hook_id: str) -> bool:
    """删掉一个钩子（去重记录随外键一起删）。不是这个应用的、不存在的返回 False。调用方提交。"""
    row = await session.get(AppHook, hook_id)
    if row is None or row.app_id != app_id:
        return False
    await session.delete(row)
    return True
