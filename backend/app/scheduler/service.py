"""定时任务的业务规则：输入模板、和工作流入口字段的对照、发起一次触发。

调度循环（loop.py）到点调 fire_once，接口的「立即运行」也调它：重叠跳过、渲染输入、记触发、发起运行这一套
只有一份。发起本身走 engine/launch.py 的 launch_run，和 HTTP 发起是同一套规则——正式运行只能从当前发布版本
发起，这一条不会在定时任务这边被绕过。

**事件触发**（docs/design/orchestration-round/00-总方案.md §4）：trigger_kind=event 的任务不按 cron 到点，而是由数据事件触发
（scheduler/triggers.py 的规则、scheduler/outbox.py 的派发）。派发到了照样调 fire_once（kind=event，带上事件 EventFire）：
重叠跳过、记触发、发起运行还是这一份；多出来的只有三样——输入模板里能用 {{ event.* }}、运行来源里记 trigger、触发链超过
上限不发起。触发记录的 inputs 写 {event: 事件的说明, inputs: 渲染出的输入}。

**定时的目标**（docs/design/next-round/00-总方案.md X4：定时只有一套）：一个定时任务指向一个目标——工作流、评测题集
（app/eval/scheduled.py）或数据源维护（app/data/source_maintenance.py）。认领、补跑、重叠跳过、触发记录、通知都只有这一套，
fire_once 按目标种类分派；建任务、改目标时核对目标也按种类交给各自的模块（check_target）。
老表的 workflow_id 是非空约束，只加不改的迁移放宽不了：目标不是工作流时它存空串。这条规矩只在 target_of /
target_columns 这一处，别处读写目标都经它们，不直接碰这两列。
"""
from __future__ import annotations

import asyncio
import logging
import re
import weakref
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.base import SessionLocal, utcnow
from app.db.models import Approval, Run, Schedule, ScheduleFire
from app.engine.expressions import _FILTERS, render_template
from app.scheduler import cron

logger = logging.getLogger(__name__)

#: 变量清单的示例按这个时区算（GET /api/schedules/variables 没带时区时）
DEFAULT_TIMEZONE = "Asia/Shanghai"
RUN_CLASSES = ("formal", "exploratory")
#: 这几个状态的运行算「还没结束」，挡住这个任务之后的触发。interrupted 也算（规格如此）：停在审批上的，批完会
#: 接着执行；服务重启挂起的却不会自己接着执行，要有人去继续或放弃——所以跳过的原因分开写（见 overlap_reason），
#: 挂起的另外按失败推一次通知（webhook._state_of），不然任务就此静默停摆
ACTIVE_RUN = ("queued", "running", "interrupted")

#: ISO 周不直观的那一处：跨年那几天按所在的周算，年份跟着周走。写在变量清单接口里（note），界面放在变量下面
ISO_WEEK_NOTE = ("ISO 周从周一开始，含当年 1 月 4 日的那一周是第 1 周；跨年那几天按所在的周算，"
                 "例如 2027-01-01（周五）属于 2026-W53")

#: 模板里能用的变量：(变量名, 说明, 补充说明)。说明、补充说明写在变量清单接口里，和示例一起给界面
VARIABLES: tuple[tuple[str, str, str], ...] = (
    ("today", "今天（YYYY-MM-DD）", ""),
    ("yesterday", "昨天（YYYY-MM-DD）", ""),
    ("this_week", "本周（ISO 周，YYYY-Www）", ISO_WEEK_NOTE),
    ("last_week", "上周（ISO 周，YYYY-Www）", ISO_WEEK_NOTE),
    ("week_before_last", "上上周（ISO 周，YYYY-Www）", ISO_WEEK_NOTE),
    ("this_week_start", "本周一（YYYY-MM-DD）", ""),
    ("last_week_start", "上周一（YYYY-MM-DD）", ""),
    ("last_week_end", "上周日（YYYY-MM-DD）", ""),
    ("this_month", "本月（YYYY-MM）", ""),
    ("last_month", "上月（YYYY-MM）", ""),
    ("month_before_last", "上上月（YYYY-MM）", ""),
    ("this_month_start", "本月 1 日（YYYY-MM-DD）", ""),
    ("last_month_start", "上月 1 日（YYYY-MM-DD）", ""),
    ("last_month_end", "上月最后一天（YYYY-MM-DD）", ""),
    ("fire_time", "本次计划触发时间（带时区偏移的 ISO 时间）", ""),
    ("last_fire_time", "上一次计划触发时间（带时区偏移的 ISO 时间）", ""),
)
_NAMES = tuple(f"schedule.{key}" for key, _, _ in VARIABLES)
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)

#: 触发记下来、运行还没发起时先写成这样：发起成功再改成 started。进程在这中间被强杀，留下的记录照实说没有发起。
#: 进程还在、只是还没发起完的，接口按「已发起」给（见 launching）
INTERRUPTED_REASON = "服务在发起运行前停止，这次触发没有发起运行"
#: launch_run 抛了 LaunchRefused 以外的异常（库出错之类）。详细信息只进日志
GENERIC_REASON = "发起运行时出错，详细信息见服务日志"


#: 定时的目标种类（Schedule.target_kind），顺序即界面上的顺序。workflow 由本模块发起，另两种交给各自的模块（_TARGET_MODULES）
TARGET_KINDS = ("workflow", "eval_suite", "source_maintenance")
TARGET_LABEL = {"workflow": "工作流", "eval_suite": "评测题集", "source_maintenance": "数据源维护"}
#: 目标种类这个版本还发起不了（库里有别的版本建的任务、手改过库）：照实记「未能发起」，不当成工作流去跑
TARGET_UNSUPPORTED = "这个定时任务的目标是「{kind}」，当前版本还不能按时发起这类目标，这次触发没有发起"


def target_of(schedule: Any) -> tuple[str, str]:
    """一个定时任务的目标：(种类, id)。加列之前建的任务 target_kind 缺省 workflow、target_id 为空，目标就是 workflow_id。

    workflow 时 id 以 workflow_id 为准（两列由 target_columns 一起写，对得上；老任务只有这一列）。"""
    kind = getattr(schedule, "target_kind", None) or "workflow"
    if kind == "workflow":
        return kind, schedule.workflow_id or getattr(schedule, "target_id", None) or ""
    return kind, getattr(schedule, "target_id", None) or ""


#: 非工作流目标各由一个模块处理（next-round X4）：模块里有
#:   async def fire(*, schedule_id, fire_at, kind, actor, target_id, name, tz, cron_expr) -> ScheduleFire | None
#:     到点发起一次（持着 launch_lock 调用）；触发记录用本模块的 record_fire 写，和工作流同一套去重、同一种状态词
#:   async def check_target(session, target_id) -> str | None
#:     建任务、改目标时核对目标（存在、可用），不合规返回给人看的一句，合规返回 None
#: 按需导入（评测、数据源维护的模块要导入本模块，模块级互相导入会成环）
_TARGET_MODULES = {"eval_suite": "app.eval.scheduled", "source_maintenance": "app.data.source_maintenance"}


def target_handler(kind: str) -> Any | None:
    """处理这种目标的模块；不认识的种类为 None。"""
    import importlib

    module = _TARGET_MODULES.get(kind)
    return importlib.import_module(module) if module else None


async def check_target(session: Any, kind: str, target_id: str) -> str | None:
    """建任务、改目标时核对一个非工作流目标（存在、可用）：交给这种目标的模块（check_target）。不合规返回给人看的一句。
    工作流目标由接口自己核对（api/schedules._check_workflow：还要对照入口字段）。"""
    handler = target_handler(kind)
    if handler is None:
        return f"不认识的定时目标种类：{kind}"
    return await handler.check_target(session, target_id)


async def target_name(session: Any, kind: str, target_id: str) -> str | None:
    """非工作流目标现在叫什么（题集名、数据源名），给接口和定时页列表显示；不在了为 None。模块里有 target_name 就用它，
    数据源维护的目标直接读数据源名。"""
    handler = target_handler(kind)
    if handler is not None and hasattr(handler, "target_name"):
        return await handler.target_name(session, target_id)
    if kind == "source_maintenance":
        from app.db.models import DataSource

        source = await session.get(DataSource, target_id)
        return source.name if source is not None else None
    return None


async def record_fire(schedule_id: str, fire_at: datetime, kind: str, status: str, reason: str | None,
                      inputs: dict[str, Any] | None = None, **columns: Any) -> ScheduleFire | None:
    """非工作流目标写触发记录（同 _record：同一次触发已经记过就交回已有的那一条）。columns 写额外的列，比如评测的
    eval_run_id。"""
    fire = await _record(schedule_id, fire_at, kind, status, reason, inputs or {})
    if fire is not None and columns:
        async with SessionLocal() as session:
            row = await session.get(ScheduleFire, fire.id)
            if row is not None:
                for key, value in columns.items():
                    setattr(row, key, value)
                await session.commit()
                await session.refresh(row)
                return row
    return fire


def target_columns(kind: str, target_id: str) -> dict[str, str]:
    """写目标的几列：{target_kind, target_id, workflow_id}。workflow 时 target_id 同 workflow_id；别的种类 workflow_id 存空串
    （老表的非空约束）。种类不认识、id 为空抛 ValueError。"""
    if kind not in TARGET_KINDS:
        raise ValueError(f"不认识的定时目标种类：{kind}")
    if not target_id:
        raise ValueError("定时任务的目标 id 不能为空")
    return {"target_kind": kind, "target_id": target_id, "workflow_id": target_id if kind == "workflow" else ""}


class TemplateError(ValueError):
    """输入模板写错了。str() 是给人看的中文说明，指出是哪个输入。"""


def current_value(value: Any) -> str:
    """报错末尾回显当前值的那半句：没填写说「当前未填写」，文本、数字照原样，true / false 按 JSON 的写法；列表、对象
    不回显——给人看的说明里不出现 Python 的写法（None、['a', 'b']）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "，当前未填写"
    if isinstance(value, bool):
        return f"，当前为「{'true' if value else 'false'}」"
    if isinstance(value, (str, int, float)):
        return f"，当前为「{value}」"
    return ""


# --------------------------------------------------------------------------
# 模板变量
# --------------------------------------------------------------------------


def template_values(fire_at: datetime, tz: str, cron_expr: str | cron.CronSpec) -> dict[str, str]:
    """按计划触发时间在任务时区算出的变量（键不带 schedule. 前缀）。日期写 YYYY-MM-DD，ISO 周写 YYYY-Www（2026-W42），
    月份写 YYYY-MM，时间写带偏移的 ISO、只到秒。

    按 fire_at 算而不是按「现在」：补发晚了一小时、立即运行在零点前后点下去，「昨天」都是那次触发的昨天。
    ISO 周的年份是周所在的年（isocalendar），不是日历年：2027-01-01 是 2026-W53，2025-12-29 是 2026-W01。
    last_fire_time 是按 cron 往前找的上一次计划触发，不看历史记录：同一个 (cron, 时区, fire_at) 永远渲染出同一份输入。
    事件触发的任务没有 cron（存空串），last_fire_time 为空（这个变量在事件触发的模板里也不让用，见 check_template）。
    """
    zone = cron.zone(tz)
    local = fire_at.astimezone(zone)
    today = local.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    previous = cron.prev_fire(cron_expr, zone, fire_at) if cron_expr else None
    return {
        "today": _day(today),
        "yesterday": _day(today - timedelta(days=1)),
        "this_week": _week(today),
        "last_week": _week(today - timedelta(days=7)),
        "week_before_last": _week(today - timedelta(days=14)),
        "this_week_start": _day(week_start),
        "last_week_start": _day(week_start - timedelta(days=7)),
        "last_week_end": _day(week_start - timedelta(days=1)),
        "this_month": _month(month_start),
        "last_month": _month(last_month_start),
        "month_before_last": _month(last_month_start - timedelta(days=1)),
        "this_month_start": _day(month_start),
        "last_month_start": _day(last_month_start),
        "last_month_end": _day(last_month_end),
        "fire_time": local.isoformat(timespec="seconds"),
        "last_fire_time": previous.astimezone(zone).isoformat(timespec="seconds") if previous else "",
    }


def _day(value: date) -> str:
    return value.isoformat()


def _week(value: date) -> str:
    """这一天所在的 ISO 周：2026-W42。周号补足两位，和 ISO 8601 的写法一致（2026-W05，不写 2026-W5）。"""
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def _month(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def render_inputs(template: dict[str, str], values: dict[str, str], *,
                  event: dict[str, Any] | None = None) -> dict[str, str]:
    """渲染输入模板。用项目通用的 {{ }} 渲染（engine/expressions.render_template），变量挂在 schedule 下；事件触发的另有
    event（scheduler/triggers.values）。"""
    ctx: dict[str, Any] = {"schedule": values}
    if event is not None:
        ctx["event"] = event
    return {name: render_template(text, ctx) for name, text in template.items()}


#: 事件触发的模板里不能用的日期变量：没有 cron，就没有「上一次计划触发」
EVENT_UNSUPPORTED = ("schedule.last_fire_time",)


def check_template(template: Any, *, event: str | None = None) -> dict[str, str]:
    """校验输入模板，原样返回（None 当空模板）。写错抛 TemplateError。

    项目的模板渲染取不到值时渲染成空串、不报错——画布上的半成品图要能运行。定时任务没人盯着，拼错的变量名
    会让每一次运行都拿到空的日期，所以保存和预览时就要说出来。

    event 是事件触发任务的事件种类：这时还能用这种事件的 {{ event.* }}（scheduler/triggers），不能用 schedule.last_fire_time。
    不给（按 cron 触发的任务）时规则一字不变。
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
            raise TemplateError(f"输入「{name}」的值需要是文本（可以带 {{{{ schedule.yesterday }}}} 这样的变量）"
                                f"{current_value(text)}")
        _check_text(name, text, event)
        out[name] = text
    return out


def _check_text(name: str, text: str, event: str | None = None) -> None:
    for match in _TEMPLATE_RE.finditer(text):
        expr = match.group(1).strip()
        if not expr:
            raise TemplateError(f"输入「{name}」的模板里有空的 {{{{ }}}}，请在中间写变量名，例如 schedule.yesterday")
        path, *filters = (part.strip() for part in expr.split("|"))
        if event is not None:
            _check_event_path(name, path, event)
        elif path not in _NAMES:
            raise TemplateError(f"输入「{name}」的模板里「{path}」不是可用的变量，可用的变量：{'、'.join(_NAMES)}")
        for filt in filters:
            if filt not in _FILTERS:
                raise TemplateError(f"输入「{name}」的模板里「{filt}」不是可用的过滤器，可用的过滤器：{'、'.join(_FILTERS)}")
    rest = _TEMPLATE_RE.sub("", text)
    if "{{" in rest or "}}" in rest:
        raise TemplateError(f"输入「{name}」的模板里 {{{{ 和 }}}} 没有成对出现，请检查是否少写了一半")


def _check_event_path(name: str, path: str, event: str) -> None:
    """事件触发任务的模板里的一个变量：schedule.*（上一次计划触发时间除外）或这种事件的 event.*。"""
    from app.scheduler import triggers

    if path in _NAMES and path not in EVENT_UNSUPPORTED:
        return
    if path in EVENT_UNSUPPORTED:
        raise TemplateError(f"输入「{name}」的模板里「{path}」在事件触发的任务里不可用：没有按时间排的上一次触发")
    if triggers.allowed_path(path, event):
        return
    usable = [n for n in _NAMES if n not in EVENT_UNSUPPORTED] + list(triggers.names(event))
    raise TemplateError(f"输入「{name}」的模板里「{path}」不是可用的变量，可用的变量：{'、'.join(usable)}")


def variables(now: datetime | None = None, tz: str = DEFAULT_TIMEZONE, *,
              event: str | None = None) -> list[dict[str, str]]:
    """变量清单：[{name, label, example, note}]。示例按 now（默认现在）这一刻触发算；上一次计划触发时间没有 cron
    可依，示例按每天同一时刻触发算。note 是补充说明（ISO 周的跨年规则），没有的是空串。

    event 是事件触发任务的事件种类：去掉上一次计划触发时间，后面接这种事件的 {{ event.* }}（示例是写法示意）。"""
    moment = (now or utcnow()).replace(second=0, microsecond=0)
    local = moment.astimezone(cron.zone(tz))
    daily = f"{local.minute} {local.hour} * * *"
    values = template_values(moment, tz, daily)
    out = [{"name": f"schedule.{key}", "label": label, "example": values[key], "note": note}
           for key, label, note in VARIABLES]
    if event is None:
        return out
    from app.scheduler import triggers

    kept = [({**v, "label": EVENT_FIRE_TIME} if v["name"] == "schedule.fire_time" else v)
            for v in out if v["name"] not in EVENT_UNSUPPORTED]
    return kept + triggers.variables(event)


#: 事件触发的任务里 schedule.fire_time 的说法：不是「计划」的时刻，是去抖窗口结束、合成这次触发的时刻
EVENT_FIRE_TIME = "本次触发时间（去抖结束的时刻，带时区偏移的 ISO 时间）"


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

def launch_lock() -> Any:
    """发起（查重叠 → 记触发 → 发起运行 → 改记录）和通知扫描查库互斥。跨进程的锁（app/cluster/mutex：本进程按事件循环各一把，
    PostgreSQL 时另取咨询锁）：多进程时到点的发起在当班进程、「立即运行」在接到请求的进程，照样互斥。"""
    from app.cluster import mutex

    return mutex.lock("schedule-launch")


#: 正在发起的触发：占位记录已写进库、launch_run 还没返回。通知扫描有 launch_lock 挡着，接口的读（任务列表的
#: last_fire、触发历史）没有：它们据此按「已发起」给，不把还在发起的说成「未能发起」
_LAUNCHING: set[str] = set()


def launching(fire_id: str, fire: Any = None) -> bool:
    """这一次触发是不是还在发起：本进程里在发起；多进程时也可能在别的进程里——占位记录（「未能发起：中断」、还没有运行）写下
    不到一个租约时长的，按还在发起算（进程真没了的，过了租约就照实显示）。"""
    if fire_id in _LAUNCHING:
        return True
    if fire is None or getattr(fire, "status", None) != "failed_to_start" or getattr(fire, "run_id", None) \
            or getattr(fire, "reason", None) != INTERRUPTED_REASON or getattr(fire, "created_at", None) is None:
        return False
    from datetime import timedelta

    from app.cluster.workers import lease_s

    created = fire.created_at if fire.created_at.tzinfo else fire.created_at.replace(tzinfo=timezone.utc)
    return created > utcnow() - timedelta(seconds=lease_s())


def actor_label(name: str) -> str:
    """定时任务发起的运行记在谁名下。"""
    return f"定时任务：{name}"[:100]


def next_run(cron_expr: str, tz: str, enabled: bool, now: datetime | None = None) -> datetime | None:
    """下一次触发的时刻（UTC）；停用的任务没有下一次。"""
    if not enabled:
        return None
    return cron.next_fire(cron_expr, tz, now or utcnow())


async def fire_once(schedule_id: str, fire_at: datetime, kind: str, *, actor: str | None = None,
                    event: Any = None) -> ScheduleFire | None:
    """处理一次触发，返回记下的触发记录。同一 (任务, fire_at, kind) 已经有记录（两拍撞在一起、立即运行连点两下）
    时不再发起，返回已有的那一条；任务不存在返回 None。

    按目标种类分派（target_of）：工作流在这里发起（_fire_workflow），评测题集、数据源维护交给各自模块的 fire（同样持着
    launch_lock，触发记录用 record_fire 写）；不认识的种类记 failed_to_start、写明原因，不发起。

    event 是事件触发的那一次事件（scheduler/outbox.EventFire）：派发时（kind=event）、事件触发任务的立即运行（kind=manual，
    示例事件）带上。事件触发只能指向工作流。
    """
    async with launch_lock():
        async with SessionLocal() as session:
            schedule = await session.get(Schedule, schedule_id)
            if schedule is None:
                return None
            target_kind, target_id = target_of(schedule)
            name, run_class = schedule.name, schedule.run_class
            tz, cron_expr, template = schedule.timezone, schedule.cron, schedule.input_template
        if target_kind != "workflow" and event is not None:
            return await _record(schedule_id, fire_at, kind, "failed_to_start", EVENT_TARGET_ONLY, {})
        if target_kind != "workflow":
            handler = target_handler(target_kind)
            if handler is None:
                reason = TARGET_UNSUPPORTED.format(kind=TARGET_LABEL.get(target_kind, target_kind))
                return await _record(schedule_id, fire_at, kind, "failed_to_start", reason, {})
            return await handler.fire(schedule_id=schedule_id, fire_at=fire_at, kind=kind, actor=actor,
                                      target_id=target_id, name=name, tz=tz, cron_expr=cron_expr)
        return await _fire_workflow(schedule_id, fire_at, kind, actor=actor, name=name, workflow_id=target_id,
                                    run_class=run_class, tz=tz, cron_expr=cron_expr, template=template, event=event)


#: 事件触发只能指向工作流（库里被改成别的目标时照实记）
EVENT_TARGET_ONLY = "事件触发的定时任务只能指向工作流，这次触发没有发起"
#: 触发链超过上限（scheduler/triggers.MAX_DEPTH）
DEPTH_REASON = ("这次运行会是事件触发链上的第 {depth} 层（上限 {limit} 层）：为防止工作流之间循环触发，没有发起。"
                "请检查「上游工作流运行成功结束」的定时任务是不是串成了环")
#: 立即运行事件触发的任务、对象还没发生过这种事件，模板又用到了 {{ event.* }}
NO_SAMPLE_REASON = "还没有发生过这种事件，输入模板里的 {{ event.* }} 没有可用的值，这次立即运行没有发起"


async def _fire_workflow(schedule_id: str, fire_at: datetime, kind: str, *, actor: str | None, name: str,
                         workflow_id: str, run_class: str, tz: str, cron_expr: str, template: Any,
                         event: Any = None) -> ScheduleFire | None:
    """目标是工作流的一次触发（持着 launch_lock）。

    上一次运行还没结束就记 skipped_overlap；输入渲染不了、launch_run 拒绝或出错记 failed_to_start（拒绝写原话，
    其他异常写通用说明、详细信息进日志）；发起成功记 started 和运行 id。

    事件触发（event 不为空）：模板按事件种类校验、渲染 {{ event.* }}；触发链超过上限先记 failed_to_start（防环的兜底，比重叠
    跳过先判）；触发记录的 inputs 写 {event: 说明, inputs: 渲染出的输入}；运行来源里带 trigger。
    """
    from app.scheduler import triggers

    brief = event.brief() if event is not None else None

    def recorded(inputs: dict[str, Any]) -> dict[str, Any]:
        return inputs if brief is None else {"event": brief, "inputs": inputs}

    if event is not None and event.depth > triggers.MAX_DEPTH:
        return await _record(schedule_id, fire_at, kind, "failed_to_start",
                             DEPTH_REASON.format(depth=event.depth, limit=triggers.MAX_DEPTH), recorded({}))
    try:
        inputs = render_inputs(check_template(template, event=event.event if event is not None else None),
                               template_values(fire_at, tz, cron_expr),
                               event=event.values if event is not None else None)
    except (TemplateError, cron.CronError, cron.TimezoneError) as e:
        # 存进库之后才坏掉的（手改过库、时区库升级删了时区）：照实说是哪一项，不发起
        return await _record(schedule_id, fire_at, kind, "failed_to_start", f"无法生成这次运行的输入：{e}", recorded({}))
    if event is not None and not event.real and triggers.uses_event(template):
        return await _record(schedule_id, fire_at, kind, "failed_to_start", NO_SAMPLE_REASON, recorded({}))
    busy = await _active_run(schedule_id)
    if busy is not None:
        return await _record(schedule_id, fire_at, kind, "skipped_overlap", overlap_reason(*busy), recorded(inputs))
    fire = await _record(schedule_id, fire_at, kind, "failed_to_start", INTERRUPTED_REASON, recorded(inputs), fresh=True)
    if fire is None:
        return await _existing(schedule_id, fire_at, kind)
    from app.engine.launch import LaunchRefused, launch_run

    origin: dict[str, Any] = {"schedule_id": schedule_id, "schedule": name, "fire_id": fire.id, "fire_kind": kind}
    if event is not None:
        origin["trigger"] = event.origin()
    _LAUNCHING.add(fire.id)
    try:
        try:
            async with SessionLocal() as session:
                run = await launch_run(session, workflow_id=workflow_id, run_class=run_class,
                                       input_payload=inputs, actor=actor or actor_label(name),
                                       schedule_id=schedule_id, origin_kind="schedule", origin=origin)
        except LaunchRefused as e:
            return await _finish(fire.id, status="failed_to_start", reason=e.message)
        except Exception:  # noqa: BLE001 - 发起失败不能让调度循环停下；详细信息进日志，记录上写通用说明
            logger.exception("定时任务「%s」（%s）发起运行失败", name, schedule_id)
            return await _finish(fire.id, status="failed_to_start", reason=GENERIC_REASON)
        return await _finish(fire.id, status="started", reason=None, run_id=run.id)
    finally:
        _LAUNCHING.discard(fire.id)


async def record_skip(schedule_id: str, fire_at: datetime, reason: str) -> ScheduleFire | None:
    """记一次错过、没有补发的触发（kind=misfire，status=skipped_misfire）。"""
    return await _record(schedule_id, fire_at, "misfire", "skipped_misfire", reason, {})


async def _active_run(schedule_id: str) -> tuple[str, str, bool, bool] | None:
    """这个任务发起的、还没结束的运行（最近的那一个）：(运行 id, 状态, 是否停在审批, 是否等写入核实)，没有返回 None。

    停在审批：有 pending 的审批，或者刚批完（answered）、还没接着执行。interrupted 而两样都没有的是挂起的；挂起的里面
    有写外部系统结果未知、等核实的（app/process/actions.py），处理办法不一样，单独说。"""
    async with SessionLocal() as session:
        row = (await session.execute(
            select(Run.id, Run.status).where(Run.schedule_id == schedule_id, Run.status.in_(ACTIVE_RUN))
            .order_by(Run.created_at.desc(), Run.id.desc()).limit(1))).first()
        if row is None:
            return None
        run_id, status = row
        waiting = status == "interrupted" and bool(await session.scalar(
            select(func.count()).select_from(Approval).where(
                Approval.run_id == run_id, Approval.status.in_(("pending", "answered")))))
        from app.process.actions import unresolved_action

        held = status == "interrupted" and not waiting and await unresolved_action(session, run_id)
        return run_id, status, waiting, held


def overlap_reason(run_id: str, status: str, waiting: bool, action_pending: bool = False) -> str:
    """跳过的原因按挡路的那次运行说清楚该等还是该处理。运行编号和界面上的短编号一样写（#前 6 位）。"""
    tag = f"#{run_id[:6]}"
    if status != "interrupted":
        return f"上一次运行 {tag} 仍在进行中，本次触发已跳过"
    if waiting:
        return f"上一次运行 {tag} 正在等待审批，处理审批、运行结束后才会按时发起新的运行；本次触发已跳过"
    if action_pending:
        return (f"上一次运行 {tag} 有一次写入外部系统的结果未知，正在等待核实，核实后会自动继续；它结束后才会按时发起"
                "新的运行，本次触发已跳过")
    return (f"上一次运行 {tag} 已挂起（通常是服务重启所致），请在「记录」中继续运行或放弃这次运行，"
            "它结束后才会按时发起新的运行；本次触发已跳过")


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
