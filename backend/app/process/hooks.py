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

钩子按名字认（D-g）：应用 spec 的 hooks 里声明 {name, kind, entry}，app_hooks 按 (应用, 名字) 一行，只存密钥和启用
与否。请求到来时按应用**当前发布版**的 spec 认这个名字的 kind 和 entry——不另存一份入口：钩子做什么跟着发布走，
改草稿不改正在用的钩子，发布版里没声明（或者行被停用）的一律拒收。

钩子的管理放在 /api/apps/{id}/hooks（应用包的接口），这里给 hook_row / rotate_hook / set_enabled / delete_hook 几个
函数；密钥只在生成、轮换时交回一次，库里存密文。

一个名字只认一行（最早的那一行，hook_row）。库里还没有 (app_id, name) 的唯一约束，靠三道守：同一进程里生成、轮换按名字
串行（hook_lock）；插入后再查一遍，前面已经有行的删掉自己、改去轮换那一行（rotate_hook）；万一还是多出一行（多进程各自
插入），收请求时只认最早那一行（receive），停用按名字改全部行（set_enabled）——不然停用管不到另一行，用另一把密钥签名
的请求照样被接受。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal, utcnow
from app.db.models import AppHook, HookReceipt, Task
from app.process import receipts

logger = logging.getLogger(__name__)

#: 签名时间窗（秒）
WINDOW_S = 300
#: 回执停在 processing 超过这么久（秒），同一个幂等键可以重新认领：处理一次请求用不了这么久，多半是中途服务停了
STALE_S = receipts.STALE_S
#: 钩子的收件记录：按 (钩子, 幂等键) 去重的状态机（process/receipts.py，访问密钥的发起也用它）
_BOOK = receipts.Book(HookReceipt, "hook_id", stale_s=STALE_S)
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
        # 同名多出来的行（见模块说明）不认：界面、停用、轮换都只管最早那一行
        if hook is None or not hook.enabled or (await hook_row(session, hook.app_id, hook.name)).id != hook.id:
            raise HookRejected("钩子不存在或已停用", status_code=404)
        secret = decrypt(hook.secret) or ""
        declared = await published_declaration(session, hook.app_id, hook.name)
    if not secret:
        raise HookRejected("钩子的密钥无法解密（加密密钥可能已更换），请在应用里重新生成钩子", status_code=409)
    try:
        verify(secret, headers, body)
    except HookRejected:
        _signature_failed(hook.id)
        raise
    # 验过签名才说「发布版里没有它」：没验签的请求不该探出应用里声明了哪些钩子
    if declared is None:
        raise HookRejected("应用当前的发布版本里没有声明这个钩子，不接收请求", status_code=404)
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
    kind = str(declared.get("kind"))
    request = HookRequest(hook_id=hook.id, kind=kind, idem_key=idem_key, payload=payload)
    try:
        if kind == "start":
            out = await _start(hook, request, declared.get("entry"), reclaimed=reclaimed)
        else:
            out = await _signal(hook, request, _header(headers, CORRELATION_HEADER), reclaimed=reclaimed)
    except Exception as e:
        await _finish(hook.id, idem_key, status="rejected", detail=str(e)[:500])
        raise
    await _finish(hook.id, idem_key, status="accepted", run_id=out.get("run_id"), task_id=out.get("task_id"))
    return {"status": "accepted", "kind": kind, **out}


def declarations(spec: Any) -> dict[str, dict[str, Any]]:
    """spec.hooks 里的声明：{名字: {name, kind, entry}}。形状不对的跳过（应用检查另外报）；同名的取第一个。"""
    out: dict[str, dict[str, Any]] = {}
    hooks = spec.get("hooks") if isinstance(spec, dict) else None
    for item in hooks if isinstance(hooks, list) else []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        name = str(item.get("name") or "").strip()
        if name and name not in out:
            out[name] = {"name": name, "kind": item["kind"],
                         "entry": item.get("entry") if item["kind"] == "start" else None}
    return out


async def published_declaration(session: AsyncSession, app_id: str, name: str) -> dict[str, Any] | None:
    """应用当前发布版里这个名字的钩子声明；应用没发布、归档了、发布版里没有这个名字的返回 None。"""
    from app.db.models import App, AppVersion

    app = await session.get(App, app_id)
    if app is None or app.status == "archived" or not app.published_version:
        return None
    spec = await session.scalar(select(AppVersion.spec).where(AppVersion.app_id == app_id,
                                                              AppVersion.version == app.published_version))
    return declarations(spec).get(name)


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
    （duplicate: true）；被拒过的重新认领；正在处理的 409，停在处理中超过 STALE_S 的重新认领。状态机和访问密钥的发起
    共用（process/receipts.py）。"""
    try:
        return await _BOOK.claim(hook_id, idem_key)
    except receipts.ReceiptBusy:
        raise HookRejected("同一个幂等键的请求正在处理，请稍后再查", status_code=409) from None


async def _finish(hook_id: str, idem_key: str, *, status: str, detail: str | None = None,
                  run_id: str | None = None, task_id: str | None = None) -> None:
    await _BOOK.finish(hook_id, idem_key, status=status, detail=detail, run_id=run_id, task_id=task_id)


def _caller(hook: AppHook) -> Any:
    """钩子发起、回复时的身份：签名认过的外部系统。没有用户名（发起人一栏留空），名字写钩子名，审计里记 verified。"""
    from app.api.deps import Principal, auth_mode

    return Principal(username=None, role="admin", display_name=f"外部系统（{hook.name}）", verified=True,
                     mode=auth_mode(), via="hook")


async def _start(hook: AppHook, request: HookRequest, entry: Any, *, reclaimed: bool = False) -> dict[str, Any]:
    from app.apps.entries import start_entry

    if not entry:
        raise HookRejected("这个钩子在发布版里没有指定入口，无法发起", status_code=409)
    if reclaimed and (run_id := await _started_before(hook, request.idem_key)):
        # 上一次处理到一半服务停了：实例其实已经发起，交回它，不再发起一个
        return {"run_id": run_id}
    run = await start_entry(hook.app_id, str(entry), request.payload, _caller(hook), origin_kind="hook",
                            hook=hook.name, hook_key=request.idem_key)
    return {"run_id": run.id}


async def _started_before(hook: AppHook, idem_key: str) -> str | None:
    """这个钩子按这个幂等键发起过的实例（来源里记着 hook 和 hook_key）。"""
    from app.db.models import Run

    async with SessionLocal() as session:
        rows = (await session.execute(select(Run.id, Run.origin).where(
            Run.app_id == hook.app_id, Run.origin_kind == "hook")
            .order_by(Run.created_at.desc(), Run.id.desc()).limit(500))).all()
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
            Task.correlation_key == key).order_by(Task.created_at, Task.id).limit(1))
        if task_id is None and reclaimed:
            # 上一次处理到一半服务停了：在等的待办其实已经解决，交回它，不当成没人在等
            done = (await session.execute(select(Task.id, Task.run_id).where(
                Task.app_id == hook.app_id, Task.kind == "signal", Task.status != "pending",
                Task.correlation_key == key).order_by(Task.created_at.desc(), Task.id.desc()).limit(1))).first()
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
# 钩子的管理（给 /api/apps/{id}/hooks 用）：按名字，一个名字一行
# --------------------------------------------------------------------------


def hook_out(row: AppHook | None, *, name: str, declared: dict[str, Any] | None, in_draft: bool,
             in_published: bool, secret: str | None = None) -> dict[str, Any]:
    """接口里的一行：声明（草稿优先，没有再看发布版）加上库里的那一行（密钥生成过没有、启用与否、收请求的地址）。"""
    decl = declared or {}
    return {"id": row.id if row is not None else None, "name": name,
            "kind": decl.get("kind") or (row.kind if row is not None else None),
            "entry_id": decl.get("entry") if decl else (row.entry_id if row is not None else None),
            "enabled": bool(row.enabled) if row is not None else False,
            "has_secret": row is not None and bool(row.secret),
            "in_draft": in_draft, "in_published": in_published,
            "url_path": f"/api/hooks/{row.id}" if row is not None else None,
            "created_at": row.created_at.isoformat() if row is not None and row.created_at else None,
            **({"secret": secret} if secret else {})}


async def hook_row(session: AsyncSession, app_id: str, name: str) -> AppHook | None:
    """(应用, 名字) 的那一行。库里没有唯一约束，万一多出一行，认最早的那一行（之后的轮换、停用、删除、收请求都按它）。"""
    return await session.scalar(select(AppHook).where(AppHook.app_id == app_id, AppHook.name == name)
                                .order_by(AppHook.created_at, AppHook.id).limit(1))


#: (应用 id, 名字) → 生成、轮换的锁（见模块说明）。只增不减：一个应用的钩子就那么几个
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def hook_lock(app_id: str, name: str) -> asyncio.Lock:
    """同一个名字的生成、轮换在进程里串行：调用方从读行一直锁到提交（api/apps.py 的 rotate_hook）。"""
    return _LOCKS.setdefault((app_id, name), asyncio.Lock())


async def rotate_hook(session: AsyncSession, app_id: str, name: str, *, kind: str,
                      entry: str | None = None) -> tuple[AppHook, str, bool]:
    """生成或轮换这个名字的密钥，返回 (行, 密钥明文, 是不是新生成的)。密钥只在这一次交回，库里存密文。

    kind / entry 只是照声明记在行上给人看；收请求时以当前发布版的声明为准（published_declaration）。调用方提交。
    (app_id, name) 有唯一索引（ux_app_hooks_app_name）：别人抢先插了这个名字（并发生成，hook_lock 管不到的另一个进程），
    这次插入在保存点里被库拒掉，改去轮换已有的那一行，交回的仍是这一次的密钥——和两个人先后点「轮换」一样，后点的那把生效。"""
    from app.core.crypto import encrypt

    if kind not in KINDS:
        raise ValueError("钩子只能是发起实例或回调待办两种")
    secret = secrets.token_urlsafe(32)
    row = await hook_row(session, app_id, name)
    created = row is None
    if row is None:
        fresh = AppHook(app_id=app_id, name=name[:100], kind=kind, enabled=True, secret="")
        try:
            async with session.begin_nested():
                session.add(fresh)
                await session.flush()
            row = fresh
        except IntegrityError:
            row, created = await hook_row(session, app_id, name), False
            if row is None:
                raise
    row.kind, row.entry_id = kind, (entry if kind == "start" else None)
    row.secret = encrypt(secret) or ""
    await session.flush()
    return row, secret, created


async def set_enabled(session: AsyncSession, app_id: str, name: str, enabled: bool) -> None:
    """启用、停用这个名字的钩子：同名的行一起改（见模块说明），停用管得到每一把密钥。调用方提交。"""
    await session.execute(update(AppHook).where(AppHook.app_id == app_id, AppHook.name == name)
                          .values(enabled=enabled).execution_options(synchronize_session="fetch"))


async def delete_hook(session: AsyncSession, app_id: str, name: str) -> bool:
    """删掉这个名字的钩子行（密钥、去重记录随外键一起删）；没有返回 False。声明留在 spec 里，由人在编辑器里删。调用方提交。"""
    rows = list((await session.execute(select(AppHook).where(AppHook.app_id == app_id, AppHook.name == name)))
                .scalars())
    for row in rows:
        await session.delete(row)
    return bool(rows)


# --------------------------------------------------------------------------
# 收件记录（给 GET /api/apps/{id}/hooks/{name}/receipts 用，docs/design/next-round/04-对外集成与触达.md A6）
#
# 外部系统说「我发过了」，管理应用的人要查得到收到没有、为什么被拒。验过签名的请求在 hook_receipts 里本来就有一行；
# 验签失败的不落库（匿名的人能拿它把库刷满），只在进程里按钩子记最近一小时的次数，界面上写「近 1 小时验签失败 N 次」。
# 单进程部署（同登录失败退避），重启清零
# --------------------------------------------------------------------------

#: 验签失败按多长时间算（秒）
SIGNATURE_WINDOW_S = 3600
#: 一个钩子最多记多少次失败的时间：被刷的时候只要知道「很多」，不用无限长
_FAILURES_MAX = 10_000
#: 钩子 id → 验签失败的时刻（time.monotonic）
_SIGNATURE_FAILURES: dict[str, deque[float]] = {}
#: 一页收件记录最多几条
RECEIPTS_MAX = 100


def _signature_failed(hook_id: str) -> None:
    times = _SIGNATURE_FAILURES.setdefault(hook_id, deque(maxlen=_FAILURES_MAX))
    times.append(time.monotonic())


def signature_failures(hook_ids: list[str]) -> int:
    """这几个钩子（同名多出来的行一起算）最近一小时验签失败了几次。顺带丢掉窗口外的。"""
    floor = time.monotonic() - SIGNATURE_WINDOW_S
    total = 0
    for hook_id in hook_ids:
        times = _SIGNATURE_FAILURES.get(hook_id)
        while times and times[0] < floor:
            times.popleft()
        total += len(times or ())
    return total


async def recent_receipts(session: AsyncSession, app_id: str, name: str,
                          limit: int = RECEIPTS_MAX) -> dict[str, Any]:
    """这个名字的钩子最近收到的请求（新的在前）：幂等键、状态（处理中 / 已接受 / 被拒）、时间、发起的实例或解决的待办、
    被拒的原因；另给近 1 小时验签失败的次数。防换键重放的签名记录（status=signature）不是一次请求，不列。"""
    ids = list((await session.execute(select(AppHook.id).where(AppHook.app_id == app_id, AppHook.name == name)))
               .scalars())
    rows = list((await session.execute(
        select(HookReceipt).where(HookReceipt.hook_id.in_(ids or [""]), HookReceipt.status != "signature")
        .order_by(HookReceipt.received_at.desc(), HookReceipt.id.desc()).limit(max(1, min(limit, RECEIPTS_MAX))))).scalars())
    return {"items": [{"idem_key": r.idem_key, "status": r.status,
                       "received_at": r.received_at.isoformat() if r.received_at else None,
                       "run_id": r.run_id, "task_id": r.task_id, "detail": r.detail} for r in rows],
            "signature_failures": signature_failures(ids), "window_s": SIGNATURE_WINDOW_S}
