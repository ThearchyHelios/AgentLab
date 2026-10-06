"""定时任务：按周期自动发起工作流运行，结束、失败、停在审批时推送 Webhook，运行成功结束后和同一任务上一次成功的运行
自动对比。

形状按前后端约定（Schedule、Fire 两种对象，见 _schedule_out、_fire_outs）。Webhook 地址和签名密钥加密存放，接口
只给主机和末 4 位（url_hint）、有没有密钥（has_secret）。请求体自己解析、自己校验：错误一律回中文 detail，表单
据 field 把报错落到那一格（422 {detail, code, field}，同 api/coded.py）。后台的调度循环在 app/scheduler 里。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.coded import CodedHTTPException
from app.api.runs import actor_of
from app.core.crypto import decrypt, encrypt
from app.db.base import get_session, utcnow
from app.db.models import Run, Schedule, ScheduleFire, WebhookDelivery, Workflow, WorkflowVersion
from app.scheduler import auto_compare, cron, service, webhook

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

#: 请求不合规（422）的机器码。field 是出错的那一项：name、workflow_id、cron、timezone、run_class、input_template、
#: enabled、overlap、misfire_grace_s、webhook、version、limit、before
SCHEDULE_INVALID = "schedule_invalid"
_GONE = "定时任务不存在，可能已被删除"
#: 错过的触发在多少秒内补发：默认 1 小时，最长 7 天
DEFAULT_GRACE_S = 3600
MAX_GRACE_S = 7 * 24 * 3600
#: 预览最多给几次
MAX_PREVIEW = 20


def _invalid(detail: str, field: str | None = None) -> CodedHTTPException:
    return CodedHTTPException(422, detail, SCHEDULE_INVALID, field=field)


def _iso(moment: datetime | None) -> str | None:
    return service.as_utc(moment).isoformat() if moment is not None else None


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise _invalid("请求内容需要是 JSON 对象")
    return data


# --------------------------------------------------------------------------
# 逐项校验：返回规范化之后的值，不对抛 422（带 field）
# --------------------------------------------------------------------------


def _name(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise _invalid("请填写定时任务的名称", "name")
    if len(text) > 200:
        raise _invalid("定时任务的名称不能超过 200 个字符", "name")
    return text


def _workflow_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid("请选择要定时运行的工作流", "workflow_id")
    return value.strip()


def _cron(value: Any) -> str:
    try:
        return cron.parse(value if isinstance(value, str) else "").expr
    except cron.CronError as e:
        raise _invalid(str(e), "cron") from e


def _timezone(value: Any) -> str:
    try:
        return cron.timezone_name(value if isinstance(value, str) else "")
    except cron.TimezoneError as e:
        raise _invalid(str(e), "timezone") from e


def _run_class(value: Any) -> str:
    if value not in service.RUN_CLASSES:
        raise _invalid(f"运行类别需要选择正式运行或探索运行{_current(value)}", "run_class")
    return value


def _template(value: Any) -> dict[str, str]:
    try:
        return service.check_template(value)
    except service.TemplateError as e:
        raise _invalid(str(e), "input_template") from e


def _current(value: Any) -> str:
    return service.current_value(value)


def _enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _invalid(f"启用状态需要是 true 或 false{_current(value)}", "enabled")
    return value


def _overlap(value: Any) -> str:
    if value != "skip":
        raise _invalid(f"重叠处理目前只支持「跳过」：上一次运行还没结束时跳过这次触发{_current(value)}", "overlap")
    return value


def _grace(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_GRACE_S:
        raise _invalid(f"补发宽限需要是 0 到 {MAX_GRACE_S} 之间的整数秒数（最长 7 天）{_current(value)}",
                       "misfire_grace_s")
    return value


def _format(value: Any) -> str:
    if value not in webhook.FORMATS:
        labels = "、".join(webhook.FORMAT_LABEL[f] for f in webhook.FORMATS)
        raise _invalid(f"Webhook 格式需要是{labels}之一{_current(value)}", "webhook")
    return value


def _events(value: Any) -> list[str]:
    labels = "、".join(webhook.EVENT_LABEL[e] for e in webhook.EVENTS)
    if not isinstance(value, list):
        raise _invalid(f"Webhook 的通知事件需要从{labels}中选择", "webhook")
    unknown = [e for e in value if e not in webhook.EVENTS]
    if unknown:
        first = unknown[0]
        named = f"「{first}」" if isinstance(first, str) and first.strip() else "中有一项"
        raise _invalid(f"Webhook 的通知事件{named}不可选，只能从{labels}中选择", "webhook")
    return [e for e in webhook.EVENTS if e in value]


def _url(value: Any) -> str:
    try:
        return webhook.check_url(value)
    except ValueError as e:
        raise _invalid(str(e), "webhook") from e


def _secret(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise _invalid("Webhook 签名密钥需要是文本", "webhook")
    return value.strip()


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _matched_format(value: Any, url: str) -> str:
    """校验格式，并要求和地址对得上：群机器人的地址只认自己平台的格式（见 webhook.bot_format）。"""
    fmt = _format(value)
    if problem := webhook.format_problem(fmt, url):
        raise _invalid(problem, "webhook")
    return fmt


def _new_webhook(value: Any) -> dict[str, Any] | None:
    """创建时的 webhook：null 或 url 为空表示不配；否则校验格式、地址、密钥、事件（事件省略表示三种都要）。
    格式省略时按地址认：群机器人的地址用它平台的格式，别的用通用格式。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("Webhook 设置需要是一个对象，或者为空表示不推送", "webhook")
    if _blank(value.get("url")):
        return None
    url = _url(value.get("url"))
    given = value.get("format")
    fmt = _matched_format(given, url) if given is not None else webhook.bot_format(url) or "generic"
    return {"format": fmt, "url": url, "secret": _secret(value.get("secret")),
            "events": _events(value.get("events", list(webhook.EVENTS)))}


def _webhook_columns(hook: dict[str, Any] | None) -> dict[str, Any]:
    if hook is None:
        return {"webhook_format": None, "webhook_url": None, "webhook_secret": None, "webhook_events": []}
    return {"webhook_format": hook["format"], "webhook_url": encrypt(hook["url"]),
            "webhook_secret": encrypt(hook["secret"]) if hook["secret"] else None, "webhook_events": hook["events"]}


def _patched_webhook(row: Schedule, value: Any) -> dict[str, Any]:
    """PATCH 的 webhook → 要写的列。null 或 url 写空串：删除整个 Webhook；url 省略：地址不变；secret 省略：密钥不变、
    写空串：去掉密钥。format 省略：换了地址的按新地址认（群机器人用它平台的格式），没换的不变。

    地址或格式真的变了才查两者对不对得上：库里已有对不上的设置（这条校验之前存的）时，整份表单回写只为停用、
    改名，不能因为 Webhook 被拦下。"""
    if value is None:
        return _webhook_columns(None)
    if not isinstance(value, dict):
        raise _invalid("Webhook 设置需要是一个对象，或者为空表示不推送", "webhook")
    if "url" in value and _blank(value.get("url")):
        return _webhook_columns(None)
    has_url = bool(row.webhook_url)
    if "url" not in value and not has_url:
        raise _invalid("请填写 Webhook 地址", "webhook")
    url = _url(value.get("url")) if "url" in value else None
    if value.get("format") is not None:
        fmt = _format(value["format"])
    elif url is not None:
        fmt = webhook.bot_format(url) or (row.webhook_format if has_url else "generic")
    else:
        fmt = row.webhook_format
    if url is not None or fmt != row.webhook_format:
        target = url or decrypt(row.webhook_url)
        if target and (problem := webhook.format_problem(fmt, target)):
            raise _invalid(problem, "webhook")
    out: dict[str, Any] = {
        "webhook_format": _format(fmt),
        "webhook_events": _events(value.get("events", list(row.webhook_events or []) if has_url
                                            else list(webhook.EVENTS))),
    }
    if url is not None:
        out["webhook_url"] = encrypt(url)
    if "secret" in value:
        secret = _secret(value.get("secret"))
        out["webhook_secret"] = encrypt(secret) if secret else None
    return out


async def _check_workflow(session: AsyncSession, workflow_id: str, run_class: str, template: dict[str, str]) -> Workflow:
    """工作流要在；正式运行要有发布版本；模板里的字段名对得上入口字段，必填项都有（按这次要用的那张图查）。"""
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise _invalid("工作流不存在，可能已被删除", "workflow_id")
    graph: Any = workflow.graph
    if run_class == "formal":
        if not workflow.published_version:
            raise _invalid(f"「{workflow.name}」还没有发布版本，正式运行只能从发布版本发起。"
                           "请先在编排页发布，或改为探索运行", "run_class")
        graph = await _published_graph(session, workflow) or graph
    try:
        service.check_fields(template, service.input_fields(graph), workflow.name)
    except service.TemplateError as e:
        raise _invalid(str(e), "input_template") from e
    return workflow


async def _published_graph(session: AsyncSession, workflow: Workflow) -> Any:
    return await session.scalar(select(WorkflowVersion.graph).where(
        WorkflowVersion.workflow_id == workflow.id, WorkflowVersion.version == workflow.published_version))


# --------------------------------------------------------------------------
# 对外的形状
# --------------------------------------------------------------------------


def _description(row: Schedule) -> str:
    try:
        return cron.describe(row.cron, row.timezone)
    except cron.CronError:
        return f"按 cron 表达式 {row.cron}（{row.timezone}）"


def _webhook_out(row: Schedule) -> dict[str, Any] | None:
    if not row.webhook_url:
        return None
    plain = decrypt(row.webhook_url)
    return {
        "format": row.webhook_format,
        # 解不开（加密密钥换过）时不猜：照实说，请人重新填
        "url_hint": webhook.url_hint(plain) if plain else "（地址无法解密，请重新填写）",
        "has_secret": bool(row.webhook_secret),
        "events": [e for e in webhook.EVENTS if e in (row.webhook_events or [])],
    }


async def _fire_outs(session: AsyncSession, fires: list[ScheduleFire]) -> list[dict[str, Any]]:
    """Fire 对象：run_status 是运行现在的状态，notified 是这次触发的推送记录 [{event, status, at}]（按时间先后），
    compare 是运行成功结束后和同一任务上一次成功的运行的自动对比 {base_run_id, summary, previous}（summary 同对比接口
    的计数，previous 说基准是不是紧挨着的上一次），没有为 null。

    对比和比的时候两边的封存绑定（auto_compare）：哪一边现在不在终态（被继续执行）、被删掉、清单哈希和比的时候不一样
    （重新封存过）都给 null——对比页按现在的封存现算，记下的计数和点进去看到的会对不上，或者干脆比不了。工件读不出来、
    被改过的同样给 null。"""
    compared = {f.id: f.compare_artifact for f in fires if f.compare_artifact and f.run_id}
    compares = await auto_compare.briefs(list(compared.values()))
    run_ids = {f.run_id for f in fires if f.run_id} | {brief["base_run_id"] for brief in compares.values()}
    # 运行现在的状态和清单哈希：run_status 照写，对比按它核对两边的封存
    current = {run_id: (status, seal) for run_id, status, seal in (await session.execute(
        select(Run.id, Run.status, Run.manifest_hash).where(Run.id.in_(run_ids)))).all()} if run_ids else {}

    def compare_of(f: ScheduleFire) -> dict[str, Any] | None:
        brief = compares.get(compared.get(f.id) or "")
        if brief is None or brief["target_run_id"] != f.run_id:
            return None
        for run_id, seal in ((brief["target_run_id"], brief["target_manifest"]),
                             (brief["base_run_id"], brief["base_manifest"])):
            status, now_seal = current.get(run_id, (None, None))
            if status not in auto_compare.TERMINAL or now_seal != seal:
                return None
        return auto_compare.public(brief)

    notified: dict[str, list[dict[str, Any]]] = {}
    if fires:
        rows = (await session.execute(
            select(WebhookDelivery).where(WebhookDelivery.fire_id.in_([f.id for f in fires]))
            .order_by(WebhookDelivery.created_at)
        )).scalars()
        for d in rows:
            notified.setdefault(d.fire_id, []).append({"event": d.event, "status": d.status, "at": _iso(d.created_at)})
    # 正在发起的触发：库里是占位的「未能发起」（为了进程被强杀时照实说），进程还在、只是还没发起完，按已发起给
    launching = {f.id for f in fires if service.launching(f.id)}
    return [{
        "id": f.id,
        "fire_at": _iso(f.fire_at),
        "kind": f.kind,
        "status": "started" if f.id in launching else f.status,
        "run_id": f.run_id,
        "run_status": current.get(f.run_id, (None, None))[0] if f.run_id else None,
        "reason": None if f.id in launching else f.reason,
        "notified": notified.get(f.id, []),
        "compare": compare_of(f),
        "created_at": _iso(f.created_at),
    } for f in fires]


async def _schedule_outs(session: AsyncSession, rows: list[Schedule]) -> list[dict[str, Any]]:
    ids = [r.id for r in rows]
    names: dict[str, str] = {}
    last: dict[str, dict[str, Any]] = {}
    if rows:
        names = dict((await session.execute(
            select(Workflow.id, Workflow.name).where(Workflow.id.in_({r.workflow_id for r in rows})))).all())
        newest = (select(ScheduleFire.schedule_id, func.max(ScheduleFire.created_at).label("at"))
                  .where(ScheduleFire.schedule_id.in_(ids)).group_by(ScheduleFire.schedule_id).subquery())
        fires = list((await session.execute(select(ScheduleFire).join(newest, and_(
            ScheduleFire.schedule_id == newest.c.schedule_id, ScheduleFire.created_at == newest.c.at)))).scalars())
        for out, fire in zip(await _fire_outs(session, fires), fires):
            last.setdefault(fire.schedule_id, out)
    return [{
        "id": r.id,
        "name": r.name,
        "workflow_id": r.workflow_id,
        # 工作流还在就给它现在的名字；删掉之后用保存时记下的那一份
        "workflow_name": names.get(r.workflow_id) or r.workflow_name,
        "cron": r.cron,
        "timezone": r.timezone,
        "run_class": r.run_class,
        "input_template": dict(r.input_template or {}),
        "enabled": r.enabled,
        "overlap": r.overlap,
        "misfire_grace_s": r.misfire_grace_s,
        "webhook": _webhook_out(r),
        "next_run_at": _iso(r.next_run_at),
        "last_fire": last.get(r.id),
        "description_text": _description(r),
        "created_by": r.created_by,
        "created_at": _iso(r.created_at),
        "updated_at": _iso(r.updated_at),
        "version": r.version,
    } for r in rows]


async def _schedule_out(session: AsyncSession, row: Schedule) -> dict[str, Any]:
    return (await _schedule_outs(session, [row]))[0]


async def _get(session: AsyncSession, schedule_id: str) -> Schedule:
    row = await session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(404, _GONE)
    return row


# --------------------------------------------------------------------------
# 固定路径（要排在 /{schedule_id} 前面）
# --------------------------------------------------------------------------


@router.get("/variables")
async def list_variables(tz: str | None = Query(default=None, alias="timezone")) -> list[dict[str, str]]:
    """输入模板能用的变量：[{name, label, example}]。示例按现在这一刻、timezone（默认 Asia/Shanghai）触发算。"""
    zone = _timezone(tz) if tz is not None else service.DEFAULT_TIMEZONE
    return service.variables(tz=zone)


@router.post("/preview")
async def preview(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """{cron, timezone, input_template?, workflow_id?, n?=5} → {description_text, next: [ISO（UTC）…], inputs}。

    inputs 是按下一次触发渲染的输入。带了 workflow_id 时对照入口字段的名字；必填项没填不在这里报——用户还在填。
    """
    body = await _body(request)
    cron_expr, tz = _cron(body.get("cron")), _timezone(body.get("timezone"))
    template = _template(body.get("input_template"))
    n = body.get("n", 5)
    n = min(max(n, 1), MAX_PREVIEW) if isinstance(n, int) and not isinstance(n, bool) else 5
    workflow_id = body.get("workflow_id")
    if isinstance(workflow_id, str) and workflow_id.strip():
        workflow = await session.get(Workflow, workflow_id.strip())
        if workflow is not None:
            # 还不知道按哪一类运行：发布版本和画布的字段都认
            published = await _published_graph(session, workflow) if workflow.published_version else None
            fields = service.input_fields(workflow.graph) + service.input_fields(published)
            try:
                service.check_fields(template, fields, workflow.name, require=False)
            except service.TemplateError as e:
                raise _invalid(str(e), "input_template") from e
    upcoming = cron.preview(cron_expr, tz, n)
    first = upcoming[0] if upcoming else utcnow()
    return {
        "description_text": cron.describe(cron_expr, tz),
        "next": [_iso(m) for m in upcoming],
        "inputs": service.render_inputs(template, service.template_values(first, tz, cron_expr)),
    }


# --------------------------------------------------------------------------
# 增删改查
# --------------------------------------------------------------------------


@router.get("")
async def list_schedules(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(Schedule).order_by(Schedule.created_at.desc()))).scalars())
    return {"items": await _schedule_outs(session, rows)}


@router.post("", status_code=201)
async def create_schedule(request: Request, session: AsyncSession = Depends(get_session),
                          x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    body = await _body(request)
    name = _name(body.get("name"))
    workflow_id = _workflow_id(body.get("workflow_id"))
    cron_expr, tz = _cron(body.get("cron")), _timezone(body.get("timezone"))
    run_class = _run_class(body.get("run_class", "formal"))
    template = _template(body.get("input_template"))
    enabled = _enabled(body.get("enabled", True))
    overlap = _overlap(body.get("overlap", "skip"))
    grace = _grace(body.get("misfire_grace_s", DEFAULT_GRACE_S))
    hook = _new_webhook(body.get("webhook"))
    workflow = await _check_workflow(session, workflow_id, run_class, template)
    row = Schedule(name=name, workflow_id=workflow_id, workflow_name=workflow.name, cron=cron_expr, timezone=tz,
                   run_class=run_class, input_template=template, enabled=enabled, overlap=overlap,
                   misfire_grace_s=grace, next_run_at=service.next_run(cron_expr, tz, enabled),
                   created_by=actor_of(x_actor), **_webhook_columns(hook))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return await _schedule_out(session, row)


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await _schedule_out(session, await _get(session, schedule_id))


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, request: Request,
                          session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """改任意可改的项，带上读到的 version；对不上回 409。只读的项（id、next_run_at、last_fire……）带了也不理。

    看的是值变没变，不是键在不在：编辑表单把整份字段带回来是正常用法。
    - 工作流、运行类别、输入模板真改了才按改后的组合重新校验。只停用、改名不查工作流——工作流删了、撤了发布，
      也要能先把任务停下来。
    - cron、时区、启用状态真改了才重算下一次触发（见 _next_after_edit）。没改的不动它：到点了、这一拍还没来时
      保存一次，这一次照常发起，不会被推到下一次、连记录都没有就丢了。
    """
    body = await _body(request)
    row = await _get(session, schedule_id)
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _invalid("保存时需要带上读到的版本号 version，请刷新后重试", "version")
    if version != row.version:
        raise HTTPException(409, f"这个定时任务已被修改（当前是第 {row.version} 版，提交的是第 {version} 版），"
                                 "请刷新后再保存")
    values: dict[str, Any] = {}
    if "name" in body:
        values["name"] = _name(body["name"])
    if "workflow_id" in body:
        values["workflow_id"] = _workflow_id(body["workflow_id"])
    if "cron" in body:
        values["cron"] = _cron(body["cron"])
    if "timezone" in body:
        values["timezone"] = _timezone(body["timezone"])
    if "run_class" in body:
        values["run_class"] = _run_class(body["run_class"])
    if "input_template" in body:
        values["input_template"] = _template(body["input_template"])
    if "enabled" in body:
        values["enabled"] = _enabled(body["enabled"])
    if "overlap" in body:
        values["overlap"] = _overlap(body["overlap"])
    if "misfire_grace_s" in body:
        values["misfire_grace_s"] = _grace(body["misfire_grace_s"])
    if "webhook" in body:
        values |= _patched_webhook(row, body["webhook"])

    def merged(key: str) -> Any:
        return values.get(key, getattr(row, key))

    def changed(key: str) -> bool:
        if key not in values:
            return False
        stored = getattr(row, key)
        return values[key] != (dict(stored or {}) if key == "input_template" else stored)

    if changed("workflow_id") or changed("run_class") or changed("input_template"):
        workflow = await _check_workflow(session, merged("workflow_id"), merged("run_class"), merged("input_template"))
        values["workflow_name"] = workflow.name
    # cron、时区在上面已经规范化（字段之间一个空格、时区的标准写法），和库里存的同一种写法，直接比
    if changed("cron") or changed("timezone") or changed("enabled"):
        values["next_run_at"] = _next_after_edit(row, merged("cron"), merged("timezone"), merged("enabled"))
    # 条件 UPDATE 才是真的乐观锁：两个人同时读到第 3 版、同时保存，只有先到的那个改得动
    result = await session.execute(
        update(Schedule).where(Schedule.id == schedule_id, Schedule.version == version)
        .values(**values, version=version + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "这个定时任务刚被别人修改过，请刷新后再保存")
    await session.commit()
    session.expire_all()
    return await _schedule_out(session, await _get(session, schedule_id))


def _next_after_edit(row: Schedule, cron_expr: str, tz: str, enabled: bool) -> datetime | None:
    """改了 cron、时区或启用状态之后的下一次触发。

    停用：没有下一次。刚启用：从现在算，不补停用期间错过的。启用着改了 cron、时区：原来那一次已经到点、这一拍还没来
    的，从那一刻接着按新的表达式算——新表达式也覆盖那一刻（每天 → 工作日）就还发起这一次，只属于旧表达式的不再发起。
    """
    if not enabled:
        return None
    now = utcnow()
    due = service.as_utc(row.next_run_at) if row.enabled and row.next_run_at is not None else None
    if due is not None and due <= now:
        return cron.next_fire(cron_expr, tz, due - timedelta(microseconds=1))
    return service.next_run(cron_expr, tz, True, now)


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """删除任务，连同它的触发历史和推送记录。它发起过的运行留着（运行上的来源 id 也留着）：那是追溯凭证。"""
    await _get(session, schedule_id)
    # 外键是 ON DELETE CASCADE；这里照样按顺序删一遍，不依赖连接上开没开外键约束
    await session.execute(delete(WebhookDelivery).where(WebhookDelivery.schedule_id == schedule_id))
    await session.execute(delete(ScheduleFire).where(ScheduleFire.schedule_id == schedule_id))
    await session.execute(delete(Schedule).where(Schedule.id == schedule_id))
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# 立即运行、触发历史、测试 Webhook
# --------------------------------------------------------------------------


@router.post("/{schedule_id}/run")
async def run_now(schedule_id: str, session: AsyncSession = Depends(get_session),
                  x_actor: str | None = Header(default=None)) -> dict[str, Any]:
    """立即触发一次（kind=manual），停用的任务也可以。规则和到点触发相同：上一次运行没结束就跳过，正式运行只从当前
    发布版本发起。运行记在点按钮的人名下，没有署名时记在「定时任务：<名字>」名下。返回 {fire, run_id}。

    触发时间取到秒：连点两下落在同一秒的，交回同一条触发记录，不会发起两次。"""
    await _get(session, schedule_id)
    fire = await service.fire_once(schedule_id, utcnow().replace(microsecond=0), "manual", actor=actor_of(x_actor))
    if fire is None:
        raise HTTPException(404, _GONE)
    return {"fire": (await _fire_outs(session, [fire]))[0], "run_id": fire.run_id}


@router.get("/{schedule_id}/fires")
async def list_fires(schedule_id: str, limit: str | None = None, before: str | None = None,
                     session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """触发历史，新的在前：{items: [Fire], next_before}。翻页把 next_before 原样作为 before 传回来；没有更多时为空。"""
    await _get(session, schedule_id)
    size = 20
    if limit is not None:
        if not limit.strip().isdecimal() or not 1 <= int(limit) <= 100:
            raise _invalid(f"每页条数需要是 1 到 100 之间的整数，当前为「{limit}」", "limit")
        size = int(limit)
    stmt = select(ScheduleFire).where(ScheduleFire.schedule_id == schedule_id)
    if before is not None:
        try:
            cursor = datetime.fromisoformat(before.strip().replace("Z", "+00:00"))
        except ValueError as e:
            raise _invalid(f"翻页位置需要是接口返回的 next_before，当前为「{before}」", "before") from e
        stmt = stmt.where(ScheduleFire.created_at < service.as_utc(cursor))
    rows = list((await session.execute(
        stmt.order_by(ScheduleFire.created_at.desc(), ScheduleFire.id.desc()).limit(size + 1))).scalars())
    items = rows[:size]
    return {"items": await _fire_outs(session, items),
            "next_before": _iso(items[-1].created_at) if len(rows) > size else None}


@router.post("/{schedule_id}/webhook/test")
async def test_webhook(schedule_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """按保存的 Webhook 发一条测试消息（不重试）：{ok, http_status, message}。失败的说明里只有主机名。"""
    await _get(session, schedule_id)
    try:
        return await webhook.send_test(schedule_id)
    except KeyError as e:
        raise HTTPException(404, _GONE) from e
    except ValueError as e:
        raise _invalid(str(e), "webhook") from e
