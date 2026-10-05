"""定时任务的业务规则：输入模板、和工作流入口字段的对照、发起一次触发。

调度循环（loop.py）到点调 fire_once，接口的「立即运行」也调它：重叠跳过、渲染输入、记触发、发起运行这一套
只有一份。发起本身走 engine/launch.py 的 launch_run，和 HTTP 发起是同一套规则——正式运行只能从当前发布版本
发起，这一条不会在定时任务这边被绕过。
"""
from __future__ import annotations

import asyncio
import logging
import re
import weakref
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionLocal, utcnow
from app.db.models import Run, Schedule, ScheduleFire
from app.engine.expressions import _FILTERS, render_template
from app.scheduler import cron

logger = logging.getLogger(__name__)

#: 变量清单的示例按这个时区算（GET /api/schedules/variables 没带时区时）
DEFAULT_TIMEZONE = "Asia/Shanghai"
RUN_CLASSES = ("formal", "exploratory")
#: 这几个状态的运行算「还没结束」：停在审批上、服务重启挂起的（interrupted）也算，它们随时会接着执行
ACTIVE_RUN = ("queued", "running", "interrupted")

#: 模板里能用的变量：(变量名, 说明)。说明写在变量清单接口里，和示例一起给界面
VARIABLES: tuple[tuple[str, str], ...] = (
    ("today", "今天（YYYY-MM-DD）"),
    ("yesterday", "昨天（YYYY-MM-DD）"),
    ("this_week_start", "本周一（YYYY-MM-DD）"),
    ("last_week_start", "上周一（YYYY-MM-DD）"),
    ("last_week_end", "上周日（YYYY-MM-DD）"),
    ("this_month_start", "本月 1 日（YYYY-MM-DD）"),
    ("last_month_start", "上月 1 日（YYYY-MM-DD）"),
    ("last_month_end", "上月最后一天（YYYY-MM-DD）"),
    ("fire_time", "本次计划触发时间（带时区偏移的 ISO 时间）"),
    ("last_fire_time", "上一次计划触发时间（带时区偏移的 ISO 时间）"),
)
_NAMES = tuple(f"schedule.{key}" for key, _ in VARIABLES)
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)

#: 触发记下来、运行还没发起时先写成这样：发起成功再改成 started。进程在这中间被强杀，留下的记录照实说没有发起
INTERRUPTED_REASON = "服务在发起运行前停止，这次触发没有发起运行"
#: launch_run 抛了 LaunchRefused 以外的异常（库出错之类）。详细信息只进日志
GENERIC_REASON = "发起运行时出错，详细信息见服务日志"


class TemplateError(ValueError):
    """输入模板写错了。str() 是给人看的中文说明，指出是哪个输入。"""


# --------------------------------------------------------------------------
# 模板变量
# --------------------------------------------------------------------------


def template_values(fire_at: datetime, tz: str, cron_expr: str | cron.CronSpec) -> dict[str, str]:
    """按计划触发时间在任务时区算出的变量（键不带 schedule. 前缀）。日期写 YYYY-MM-DD，时间写带偏移的 ISO、只到秒。

    按 fire_at 算而不是按「现在」：补发晚了一小时、立即运行在零点前后点下去，「昨天」都是那次触发的昨天。
    last_fire_time 是按 cron 往前找的上一次计划触发，不看历史记录：同一个 (cron, 时区, fire_at) 永远渲染出同一份输入。
    """
    zone = cron.zone(tz)
    local = fire_at.astimezone(zone)
    today = local.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    previous = cron.prev_fire(cron_expr, zone, fire_at)
    return {
        "today": _day(today),
        "yesterday": _day(today - timedelta(days=1)),
        "this_week_start": _day(week_start),
        "last_week_start": _day(week_start - timedelta(days=7)),
        "last_week_end": _day(week_start - timedelta(days=1)),
        "this_month_start": _day(month_start),
        "last_month_start": _day(last_month_end.replace(day=1)),
        "last_month_end": _day(last_month_end),
        "fire_time": local.isoformat(timespec="seconds"),
        "last_fire_time": previous.astimezone(zone).isoformat(timespec="seconds") if previous else "",
    }


def _day(value: date) -> str:
    return value.isoformat()


def render_inputs(template: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    """渲染输入模板。用项目通用的 {{ }} 渲染（engine/expressions.render_template），变量挂在 schedule 下。"""
    ctx = {"schedule": values}
    return {name: render_template(text, ctx) for name, text in template.items()}


def check_template(template: Any) -> dict[str, str]:
    """校验输入模板，原样返回（None 当空模板）。写错抛 TemplateError。

    项目的模板渲染取不到值时渲染成空串、不报错——画布上的半成品图要能运行。定时任务没人盯着，拼错的变量名
    会让每一次运行都拿到空的日期，所以保存和预览时就要说出来。
    """
    if template is None:
        return {}
    if not isinstance(template, dict):
        raise TemplateError("输入模板需要是「字段名：模板」的对象，例如 stat_date 对应 {{ schedule.yesterday }}")
    out: dict[str, str] = {}
    for raw, text in template.items():
        # 字段名去掉首尾空白再存：「 stat_date」交给运行，入口节点认不出它，必填项照样报缺
        name = raw.strip() if isinstance(raw, str) else ""
        if not name:
            raise TemplateError("输入模板里有空的字段名，请填写工作流的输入字段名")
        if name in out:
            raise TemplateError(f"输入模板里字段「{name}」写了两次，请只保留一个")
        if not isinstance(text, str):
            raise TemplateError(f"输入「{name}」的值需要是文本（可以带 {{{{ schedule.yesterday }}}} 这样的变量），当前为「{text}」")
        _check_text(name, text)
        out[name] = text
    return out


def _check_text(name: str, text: str) -> None:
    for match in _TEMPLATE_RE.finditer(text):
        expr = match.group(1).strip()
        if not expr:
            raise TemplateError(f"输入「{name}」的模板里有空的 {{{{ }}}}，请在中间写变量名，例如 schedule.yesterday")
        path, *filters = (part.strip() for part in expr.split("|"))
        if path not in _NAMES:
            raise TemplateError(f"输入「{name}」的模板里「{path}」不是可用的变量，可用的变量：{'、'.join(_NAMES)}")
        for filt in filters:
            if filt not in _FILTERS:
                raise TemplateError(f"输入「{name}」的模板里「{filt}」不是可用的过滤器，可用的过滤器：{'、'.join(_FILTERS)}")
    rest = _TEMPLATE_RE.sub("", text)
    if "{{" in rest or "}}" in rest:
        raise TemplateError(f"输入「{name}」的模板里 {{{{ 和 }}}} 没有成对出现，请检查是否少写了一半")


def variables(now: datetime | None = None, tz: str = DEFAULT_TIMEZONE) -> list[dict[str, str]]:
    """变量清单：[{name, label, example}]。示例按 now（默认现在）这一刻触发算；上一次计划触发时间没有 cron
    可依，示例按每天同一时刻触发算。"""
    moment = (now or utcnow()).replace(second=0, microsecond=0)
    local = moment.astimezone(cron.zone(tz))
    daily = f"{local.minute} {local.hour} * * *"
    values = template_values(moment, tz, daily)
    return [{"name": f"schedule.{key}", "label": label, "example": values[key]} for key, label in VARIABLES]


# --------------------------------------------------------------------------
# 和工作流入口字段对照
# --------------------------------------------------------------------------


def input_fields(graph: Any) -> list[dict[str, Any]]:
    """工作流入口节点声明的字段 [{name, required, default}]，没声明（或图不成形）是空列表。"""
    from app.engine.schema import GraphSpec, NodeType

    try:
        spec = GraphSpec.model_validate(graph)
    except Exception:  # noqa: BLE001 - 图不成形交给发起时报
        return []
    out: list[dict[str, Any]] = []
    for node in spec.nodes:
        if node.type != NodeType.INPUT:
            continue
        for field in node.config.get("fields") or []:
            if isinstance(field, dict) and isinstance(field.get("name"), str) and field["name"].strip():
                out.append({"name": field["name"].strip(), "required": bool(field.get("required")),
                            "default": field.get("default")})
    return out


def check_fields(template: dict[str, str], fields: list[dict[str, Any]], workflow_name: str, *,
                 require: bool = True) -> None:
    """入口声明了字段时，模板里的字段名要在里面；require 时必填且没有默认值的字段要有模板。不对抛 TemplateError。

    入口没声明字段的工作流，输入是开放的（运行时传什么键都收），不查。预览时用户还在填，只查多出来的名字。
    """
    if not fields:
        return
    names = [f["name"] for f in fields]
    unknown = [name for name in template if name not in names]
    if unknown:
        raise TemplateError(f"输入「{unknown[0]}」不是工作流「{workflow_name}」入口声明的字段，可用的字段：{'、'.join(names)}")
    if not require:
        return
    missing = [f["name"] for f in fields
               if f["required"] and f["default"] in (None, "") and f["name"] not in template]
    if missing:
        raise TemplateError(f"工作流「{workflow_name}」的必填输入「{'、'.join(missing)}」没有填写，"
                            "到点发起的运行会因缺少输入而失败")


# --------------------------------------------------------------------------
# 发起一次触发
# --------------------------------------------------------------------------

#: 每个事件循环一把锁：发起（查重叠 → 记触发 → 发起运行 → 改记录）和通知扫描查库互斥。单进程部署，进程内
#: 互斥就够；按事件循环分开是因为测试每个用例一个新循环，跨循环用同一把 asyncio.Lock 会报错
_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def launch_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


def actor_label(name: str) -> str:
    """定时任务发起的运行记在谁名下。"""
    return f"定时任务：{name}"[:100]


def next_run(cron_expr: str, tz: str, enabled: bool, now: datetime | None = None) -> datetime | None:
    """下一次触发的时刻（UTC）；停用的任务没有下一次。"""
    if not enabled:
        return None
    return cron.next_fire(cron_expr, tz, now or utcnow())


async def fire_once(schedule_id: str, fire_at: datetime, kind: str, *, actor: str | None = None,
                    ) -> ScheduleFire | None:
    """处理一次触发，返回记下的触发记录。同一 (任务, fire_at, kind) 已经有记录（两拍撞在一起、立即运行连点两下）
    时不再发起，返回已有的那一条；任务不存在返回 None。

    上一次运行还没结束就记 skipped_overlap；输入渲染不了、launch_run 拒绝或出错记 failed_to_start（拒绝写原话，
    其他异常写通用说明、详细信息进日志）；发起成功记 started 和运行 id。
    """
    async with launch_lock():
        async with SessionLocal() as session:
            schedule = await session.get(Schedule, schedule_id)
            if schedule is None:
                return None
            name, workflow_id, run_class = schedule.name, schedule.workflow_id, schedule.run_class
            tz, cron_expr, template = schedule.timezone, schedule.cron, schedule.input_template
        try:
            inputs = render_inputs(check_template(template), template_values(fire_at, tz, cron_expr))
        except (TemplateError, cron.CronError, cron.TimezoneError) as e:
            return await _record(schedule_id, fire_at, kind, "failed_to_start", f"输入模板无法渲染：{e}", {})
        busy = await _active_run(schedule_id)
        if busy is not None:
            reason = f"上一次运行（{busy[:8]}）还没有结束，按设置跳过这次触发"
            return await _record(schedule_id, fire_at, kind, "skipped_overlap", reason, inputs)
        fire = await _record(schedule_id, fire_at, kind, "failed_to_start", INTERRUPTED_REASON, inputs, fresh=True)
        if fire is None:
            return await _existing(schedule_id, fire_at, kind)
        from app.engine.launch import LaunchRefused, launch_run

        try:
            async with SessionLocal() as session:
                run = await launch_run(session, workflow_id=workflow_id, run_class=run_class, input_payload=inputs,
                                       actor=actor or actor_label(name), schedule_id=schedule_id)
        except LaunchRefused as e:
            return await _finish(fire.id, status="failed_to_start", reason=e.message)
        except Exception:  # noqa: BLE001 - 发起失败不能让调度循环停下；详细信息进日志，记录上写通用说明
            logger.exception("定时任务「%s」（%s）发起运行失败", name, schedule_id)
            return await _finish(fire.id, status="failed_to_start", reason=GENERIC_REASON)
        return await _finish(fire.id, status="started", reason=None, run_id=run.id)


async def record_skip(schedule_id: str, fire_at: datetime, reason: str) -> ScheduleFire | None:
    """记一次错过、没有补发的触发（kind=misfire，status=skipped_misfire）。"""
    return await _record(schedule_id, fire_at, "misfire", "skipped_misfire", reason, {})


async def _active_run(schedule_id: str) -> str | None:
    """这个任务发起的、还没结束的运行 id（最近的那一个），没有返回 None。"""
    async with SessionLocal() as session:
        return await session.scalar(
            select(Run.id).where(Run.schedule_id == schedule_id, Run.status.in_(ACTIVE_RUN))
            .order_by(Run.created_at.desc()).limit(1))


async def _record(schedule_id: str, fire_at: datetime, kind: str, status: str, reason: str | None,
                  inputs: dict[str, Any], *, fresh: bool = False) -> ScheduleFire | None:
    """插入一条触发记录。撞上唯一约束（同一次触发已经记过）或任务已被删除（外键）时：fresh 返回 None，否则返回
    已有的那一条（没有就是 None）。"""
    fire = ScheduleFire(schedule_id=schedule_id, fire_at=fire_at, kind=kind, status=status, reason=reason,
                        inputs=inputs)
    async with SessionLocal() as session:
        session.add(fire)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None if fresh else await _existing(schedule_id, fire_at, kind)
        await session.refresh(fire)
        return fire


async def _existing(schedule_id: str, fire_at: datetime, kind: str) -> ScheduleFire | None:
    async with SessionLocal() as session:
        return await session.scalar(select(ScheduleFire).where(
            ScheduleFire.schedule_id == schedule_id, ScheduleFire.fire_at == fire_at, ScheduleFire.kind == kind))


async def _finish(fire_id: str, *, status: str, reason: str | None, run_id: str | None = None) -> ScheduleFire | None:
    async with SessionLocal() as session:
        await session.execute(update(ScheduleFire).where(ScheduleFire.id == fire_id)
                              .values(status=status, reason=reason, run_id=run_id))
        await session.commit()
        return await session.get(ScheduleFire, fire_id)


def local_text(moment: datetime, tz: str) -> str:
    """给人看的本地时间「2026-10-05 09:00」，写进跳过原因、通知里。"""
    return moment.astimezone(cron.zone(tz)).strftime("%Y-%m-%d %H:%M")


def duration_text(seconds: int) -> str:
    """补发宽限这类秒数的说法：3600 →「1 小时」，600 →「10 分钟」，90 →「90 秒」。"""
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
