"""数据事件触发的规则（docs/design/orchestration-round/00-总方案.md §4）：触发条件的写法与校验、给人看的说法、输入模板里的
{{ event.* }}、示例事件、防环。

**一套定时机制（X4）。** 事件触发不是另一套调度：它就是一个 trigger_kind=event 的定时任务，目标只能是工作流。认领、重叠跳过、
触发记录、通知、正式运行只从发布版本发起，全部沿用定时任务那一套（scheduler/service.fire_once）；不同的只是「什么时候触发」
——不是到点，而是三种数据事件之一（已拍板：不接「口径发布」）：

- table.version：一张按期累积的上传表格启用了新的一期（data/table_versions 启用那一下）。可选只认新增的一期（period=new）：
  重传、替换已有那一期（改错数）的不算；
- workflow.finished：一个工作流的正式运行成功结束。可选只认某个应用（app_id）发起的、某种来源（origin_kind）的；
  下游的输入模板能引用这次运行的出口字段（{{ event.outputs.<字段> }}）。两种成功结束缺省不认：途经驳回的（审批接了驳回出口、
  按驳回那条路走完，状态仍是 succeeded，事件里带 rejected_via）——下游会以为「A 成功出了结果」，其实那一期被驳回了，
  include_rejected 时才认、模板里有 {{ event.rejected }}；应用补算的（app_backfill，升版后按历次输入补算，一连发好几条、
  各是某一期的历史输入），include_backfill 或只认这种来源时才认；
- source.changed：一个数据源出现「要处理」级的变化。按源去抖：同一次探查、剖析产生的一批只记一条事件（data/source_changes）。

trigger 的形状：{event, 对象（source_id / workflow_id / source_id）, 过滤（period；app_id、origin_kind、include_rejected、
include_backfill）, debounce_s（缺省 60）}。同一个定时任务在 debounce_s 内的几次事件合成一次触发，上传表格按期次分窗口
（window_key，scheduler/outbox）。

**防环。** workflow.finished 能把工作流串起来：A 跑完触发 B、B 跑完又触发 A 就停不下来。保存时查串联成不成环（find_cycle，
停用的任务也算：启用不该绕过这道检查）；运行时按 origin 里记的触发链深度兜底，超过 MAX_DEPTH 层不发起、照实记一次未能发起
（手改过库、经别处间接串成环时还有这一道）。

这里不碰调度循环、不发起运行：派发在 scheduler/outbox.py，发起在 scheduler/service.py。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

#: 事件种类，顺序即界面上的顺序（和 db/models_triggers.EVENT_KINDS 是同一份）
EVENTS = ("table.version", "workflow.finished", "source.changed")
#: 事件种类的叫法（界面另有一份在 lib/termsTrigger.ts，逐字对照）
EVENT_LABEL = {
    "table.version": "上传表格出了新的一期",
    "workflow.finished": "上游工作流运行成功结束",
    "source.changed": "数据源有要处理的变化",
}
#: 每种事件的对象写在 trigger 的哪个键上、叫什么
SUBJECT_KEY = {"table.version": "source_id", "workflow.finished": "workflow_id", "source.changed": "source_id"}
SUBJECT_LABEL = {"table.version": "上传表格", "workflow.finished": "工作流", "source.changed": "数据源"}
DEFAULT_DEBOUNCE_S = 60
MAX_DEBOUNCE_S = 24 * 3600
#: 触发链最多几层：A 触发 B 是第 1 层，B 再触发 C 是第 2 层……超过就不发起
MAX_DEPTH = 5
#: 「期次规则」：any 新增的一期和替换已有的一期都认（缺省）；new 只认新增的一期
PERIOD_RULES = ("any", "new")
PERIOD_RULE_LABEL = {"any": "新增的一期和替换已有的一期都认", "new": "只认新增的一期"}
#: 能发起正式运行的来源：workflow.finished 的「只认某种来源」从这几种里选（问数据、画布试跑、评测发起的都是探索运行，
#: 不会有 workflow.finished）
FORMAL_ORIGINS = ("launcher", "schedule", "app_entry", "app_view", "app_backfill", "hook", "api", "ledger_issue")
#: 来源的叫法（同前端 lib/terms.ts 的 ORIGIN_LABEL）
ORIGIN_LABEL = {
    "launcher": "手动正式运行", "schedule": "定时任务", "app_entry": "应用入口", "app_view": "应用视图",
    "app_backfill": "应用补算", "hook": "入站钩子", "api": "接口调用", "ledger_issue": "按口径正式出具",
}
#: trigger 里认得的键（多出来的拒收：拼错的过滤条件悄悄不生效，比报错更糟）。subject_name 是保存时记下的对象名（接口不给、
#: 带回来也不认，对象删掉之后说法里用它）
_KEYS = {"event", "source_id", "workflow_id", "period", "app_id", "origin_kind", "debounce_s", "subject_name",
         "include_rejected", "include_backfill"}
#: 应用补算发起的运行的来源（engine/origin.py）
BACKFILL_ORIGIN = "app_backfill"
#: 示例事件里给不出真实值时写的占位说明
EXAMPLE_NOTE = "示例值"


class TriggerError(ValueError):
    """触发条件写错了、对象不在或不合用。str() 是给人看的中文说明，field 是表单上的那一项（缺省 trigger）。"""

    def __init__(self, message: str, field: str = "trigger") -> None:
        super().__init__(message)
        self.field = field


# --------------------------------------------------------------------------
# 写法
# --------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalize(value: Any) -> dict[str, Any]:
    """校验 trigger 的形状并规范化（不碰库）：{event, 对象键, debounce_s, 过滤…}，没写的过滤不出现。不对抛 TriggerError。"""
    if not isinstance(value, dict):
        raise TriggerError("事件触发需要写明事件种类和对象，例如 {event: table.version, source_id: …}")
    event = value.get("event")
    if event not in EVENTS:
        labels = "、".join(EVENT_LABEL[e] for e in EVENTS)
        raise TriggerError(f"事件种类需要是{labels}之一")
    unknown = sorted(k for k in value if k not in _KEYS)
    if unknown:
        raise TriggerError(f"事件触发的设置里有不认识的项：{'、'.join(unknown)}")
    key = SUBJECT_KEY[event]
    subject = _text(value.get(key))
    if subject is None:
        raise TriggerError(f"请选择{SUBJECT_LABEL[event]}")
    for other in {"source_id", "workflow_id"} - {key}:
        if _text(value.get(other)):
            raise TriggerError(f"「{EVENT_LABEL[event]}」只选{SUBJECT_LABEL[event]}，不写别的对象")
    out: dict[str, Any] = {"event": event, key: subject}
    period = value.get("period")
    if period not in (None, ""):
        if event != "table.version":
            raise TriggerError("只有「上传表格出了新的一期」能按期次过滤")
        if period not in PERIOD_RULES:
            raise TriggerError("期次规则需要是「新增的一期和替换已有的一期都认」或「只认新增的一期」")
        if period != "any":
            out["period"] = period
    for name in ("app_id", "origin_kind"):
        raw = value.get(name)
        if raw in (None, ""):
            continue
        if event != "workflow.finished":
            raise TriggerError("只有「上游工作流运行成功结束」能按应用、来源过滤")
        text = _text(raw)
        if text is None:
            raise TriggerError("只认的应用、来源需要从列表里选")
        if name == "origin_kind" and text not in FORMAL_ORIGINS:
            labels = "、".join(ORIGIN_LABEL[o] for o in FORMAL_ORIGINS)
            raise TriggerError(f"只认的来源需要是{labels}之一")
        out[name] = text
    for name in ("include_rejected", "include_backfill"):
        raw = value.get(name)
        if raw in (None, False):
            continue
        if event != "workflow.finished":
            raise TriggerError("只有「上游工作流运行成功结束」能选也认途经驳回的、应用补算的")
        if raw is not True:
            raise TriggerError("也认途经驳回的、应用补算的需要是 true 或 false")
        # 只认补算的：补算本来就认，不再另记
        if name == "include_backfill" and out.get("origin_kind") == BACKFILL_ORIGIN:
            continue
        out[name] = True
    debounce = value.get("debounce_s", DEFAULT_DEBOUNCE_S)
    if isinstance(debounce, bool) or not isinstance(debounce, int) or not 0 <= debounce <= MAX_DEBOUNCE_S:
        raise TriggerError(f"去抖需要是 0 到 {MAX_DEBOUNCE_S} 之间的整数秒数（最长 1 天）", "debounce_s")
    out["debounce_s"] = debounce
    return out


def subject_of(trigger: dict[str, Any] | None) -> str:
    """触发条件里的对象 id；不成形的为空串。"""
    trigger = trigger if isinstance(trigger, dict) else {}
    key = SUBJECT_KEY.get(trigger.get("event") or "")
    return str(trigger.get(key) or "") if key else ""


def debounce_of(trigger: dict[str, Any] | None) -> int:
    value = (trigger or {}).get("debounce_s", DEFAULT_DEBOUNCE_S) if isinstance(trigger, dict) else DEFAULT_DEBOUNCE_S
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else DEFAULT_DEBOUNCE_S


def matches(trigger: dict[str, Any] | None, event: str, subject_id: str, payload: dict[str, Any] | None) -> bool:
    """这条事件合不合这个触发条件：种类、对象对得上，过滤条件都满足。"""
    if not isinstance(trigger, dict) or trigger.get("event") != event or subject_of(trigger) != subject_id:
        return False
    payload = payload or {}
    if event == "table.version" and trigger.get("period") == "new" and payload.get("replaced"):
        return False
    if event == "workflow.finished":
        if trigger.get("app_id") and payload.get("app_id") != trigger["app_id"]:
            return False
        if trigger.get("origin_kind") and payload.get("origin_kind") != trigger["origin_kind"]:
            return False
        if payload.get("rejected") and not trigger.get("include_rejected"):
            return False
        if payload.get("origin_kind") == BACKFILL_ORIGIN and not (
                trigger.get("include_backfill") or trigger.get("origin_kind") == BACKFILL_ORIGIN):
            return False
    return True


# --------------------------------------------------------------------------
# 核对对象（建任务、改触发条件时）
# --------------------------------------------------------------------------


@dataclass
class Checked:
    """核对通过的触发条件：对象现在的名字，过滤里的应用名（有的话）。"""

    subject_name: str
    app_name: str | None = None


async def accumulating(session: Any, source: Any) -> bool:
    """这个上传表格是不是按期累积的：当前版本是累积快照，或者现行配方写的是按期累积（还没导入过第二期的也算）。"""
    from app.db.models import SourceSnapshot, TableRecipe

    if source.current_snapshot_id:
        snap = await session.get(SourceSnapshot, source.current_snapshot_id)
        if snap is not None and (snap.mode or "replace") == "accumulate":
            return True
    if source.current_recipe_id:
        recipe = await session.get(TableRecipe, source.current_recipe_id)
        if recipe is not None and isinstance(recipe.recipe, dict) and recipe.recipe.get("mode") == "accumulate":
            return True
    return False


async def check(session: Any, trigger: dict[str, Any]) -> Checked:
    """核对触发条件里的对象（trigger 已经 normalize 过）：表格在、是按期累积的上传表格；工作流在、已发布（只有正式运行成功
    结束才算事件）；数据源在；过滤里的应用在。不对抛 TriggerError。"""
    from app.db.models import App, DataSource, Workflow

    event, subject = trigger["event"], subject_of(trigger)
    if event in ("table.version", "source.changed"):
        source = await session.get(DataSource, subject)
        if source is None:
            raise TriggerError(f"{SUBJECT_LABEL[event]}不存在，可能已被删除")
        if event == "table.version":
            if (source.origin or "manual") != "upload":
                raise TriggerError(f"「{source.name}」不是上传表格：只有上传表格有「出了新的一期」")
            if not await accumulating(session, source):
                raise TriggerError(f"「{source.name}」不是按期累积的上传表格：每次上传替换整张表的没有「新的一期」。"
                                   "请在导入设置里改为按期累积，或改用「数据源有要处理的变化」")
        return Checked(source.name)
    workflow = await session.get(Workflow, subject)
    if workflow is None:
        raise TriggerError("上游工作流不存在，可能已被删除")
    if not workflow.published_version:
        raise TriggerError(f"上游工作流「{workflow.name}」还没有发布版本：只有正式运行成功结束才算事件，"
                           "正式运行只能从发布版本发起。请先在编排页发布")
    app_name = None
    if trigger.get("app_id"):
        app = await session.get(App, trigger["app_id"])
        if app is None:
            raise TriggerError("只认的应用不存在，可能已被删除")
        app_name = app.name
    return Checked(workflow.name, app_name)


async def find_cycle(session: Any, *, schedule_id: str | None, trigger: dict[str, Any],
                     target_workflow_id: str) -> list[str] | None:
    """workflow.finished 串成的环：这个任务（A 跑完触发 B）加进去之后，从 B 沿着别的事件触发任务能不能回到 A。成环时返回经过的
    工作流 id（A, B, …, A），否则 None。停用的任务也算进去：启用不该绕过这道检查；schedule_id 是正在改的那个任务（它原来的
    那条边不算）。"""
    if trigger.get("event") != "workflow.finished" or not target_workflow_id:
        return None
    from app.db.models import Schedule

    start, target = subject_of(trigger), target_workflow_id
    if start == target:
        return [start, start]

    async def edges_from(nodes: list[str]) -> dict[str, set[str]]:
        """这几个工作流跑完会触发哪些工作流：只读「上游工作流运行成功结束」、对象是它们的那几个任务（按触发条件里的事件种类和
        对象筛，SQLAlchemy 的通用 JSON 访问），不把全部事件触发任务读出来。"""
        rows = (await session.execute(select(Schedule.id, Schedule.trigger, Schedule.workflow_id, Schedule.target_kind)
                                      .where(Schedule.trigger_kind == "event",
                                             Schedule.trigger["event"].as_string() == "workflow.finished",
                                             Schedule.trigger["workflow_id"].as_string().in_(nodes)))).all()
        out: dict[str, set[str]] = {}
        for sid, other, workflow_id, kind in rows:
            if sid == schedule_id or (kind or "workflow") != "workflow" or not isinstance(other, dict):
                continue
            if other.get("event") == "workflow.finished" and subject_of(other) and workflow_id:
                out.setdefault(subject_of(other), set()).add(workflow_id)
        return out

    # 从 B 出发找回 A 的路（广度优先，一层查一次，取最短的一条说给人听）
    parent: dict[str, str | None] = {target: None}
    frontier = [target]
    while frontier:
        edges = await edges_from(frontier)
        nxt_level: list[str] = []
        for node in frontier:
            for nxt in sorted(edges.get(node, ())):
                if nxt in parent:
                    continue
                parent[nxt] = node
                if nxt == start:
                    back = [start]                  # A ← … ← B，倒着记
                    while back[-1] != target:
                        back.append(parent[back[-1]] or target)
                    return [start, *reversed(back)]
                nxt_level.append(nxt)
        frontier = nxt_level
    return None


def cycle_text(names: list[str]) -> str:
    """成环时的说明（422）：「A」→「B」→「A」，箭头说的是「正式运行成功结束后触发」。"""
    chain = "→".join(f"「{n}」" for n in names)
    return (f"这样设置会循环触发：{chain}（每个箭头表示前一个工作流的正式运行成功结束后触发后一个），"
            "运行会一直接力下去。请去掉其中一环")


# --------------------------------------------------------------------------
# 给人看的说法
# --------------------------------------------------------------------------


def describe(trigger: dict[str, Any], subject_name: str | None, *, app_name: str | None = None) -> str:
    """一个事件触发任务的说法（列表、详情页头、预览，同 cron 任务的 description_text）：
    「上传表格「门店销售」出了新的一期时触发（只认新增的一期；去抖 60 秒）」。对象不在了写保存时记的名字。"""
    event = trigger.get("event")
    name = f"「{subject_name}」" if subject_name else ""
    if event == "table.version":
        head = f"上传表格{name}出了新的一期时触发"
    elif event == "workflow.finished":
        head = f"工作流{name}的正式运行成功结束时触发"
    elif event == "source.changed":
        head = f"数据源{name}出现要处理的变化时触发"
    else:
        return "由事件触发（触发条件无法识别）"
    notes: list[str] = []
    if trigger.get("period") == "new":
        notes.append(PERIOD_RULE_LABEL["new"])
    if trigger.get("app_id"):
        notes.append(f"只认应用「{app_name}」发起的" if app_name else "只认指定的应用发起的")
    if trigger.get("origin_kind"):
        notes.append(f"只认来源为「{ORIGIN_LABEL.get(trigger['origin_kind'], trigger['origin_kind'])}」的")
    if event == "workflow.finished":
        # 缺省不认的两种成功结束，照实写出来
        kinds = [("途经驳回", bool(trigger.get("include_rejected")))]
        if trigger.get("origin_kind") != BACKFILL_ORIGIN:
            kinds.append(("应用补算", bool(trigger.get("include_backfill"))))
        excluded = "、".join(k for k, on in kinds if not on)
        included = "、".join(k for k, on in kinds if on)
        notes += [f"{included}的也算"] if included else []
        notes += [f"{excluded}的不算"] if excluded else []
    debounce = debounce_of(trigger)
    from app.scheduler.service import duration_text

    notes.append(f"去抖 {duration_text(debounce)}" if debounce else "不去抖")
    return f"{head}（{'；'.join(notes)}）"


def event_text(event: str, subject_name: str, payload: dict[str, Any] | None) -> str:
    """一次事件的说法（触发历史、运行来源、通知）：「上传表格「门店销售」出了新的一期（2026-09）」。不写出口字段的值、变化的
    内容（可能带着数，通知会推进群里）。"""
    payload = payload or {}
    name = f"「{subject_name}」" if subject_name else ""
    if event == "table.version":
        period = payload.get("period") or ""
        what = "替换了已有的一期" if payload.get("replaced") else "出了新的一期"
        return f"上传表格{name}{what}{f'（{period}）' if period else ''}"
    if event == "workflow.finished":
        run = str(payload.get("run_id") or "")
        rejected = "（途经驳回）" if payload.get("rejected") else ""
        return f"工作流{name}的正式运行成功结束{f'（#{run[:6]}）' if run else ''}{rejected}"
    if event == "source.changed":
        n = (payload.get("counts") or {}).get("new")
        return f"数据源{name}出现要处理的变化{f'（新记 {n} 条）' if isinstance(n, int) and n else ''}"
    return "数据事件"


# --------------------------------------------------------------------------
# 输入模板里的 {{ event.* }}
# --------------------------------------------------------------------------

#: 每种事件能用的变量：(变量名, 说明, 示例)。outputs.<字段>、counts.<项> 之下的名字是开放的，见 allowed_path
VARIABLES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "table.version": (
        ("table", "上传表格的名字", "门店销售"),
        ("version", "启用的版本编号", "3f2a9c…"),
        ("period", "这一期（整月写 YYYY-MM，整周写 YYYY-Www，单日写 YYYY-MM-DD）", "2026-09"),
        ("period_start", "这一期的第一天（YYYY-MM-DD）", "2026-09-01"),
        ("period_end", "这一期的最后一天（YYYY-MM-DD）", "2026-09-30"),
        ("activated_at", "启用的时间（带时区偏移的 ISO 时间）", "2026-10-08T09:30:00+08:00"),
    ),
    "workflow.finished": (
        ("workflow", "上游工作流的名字", "门店日报"),
        ("workflow_id", "上游工作流的编号", "wf…"),
        ("run_id", "上游那次运行的编号", "run…"),
        ("finished_at", "上游运行结束的时间（带时区偏移的 ISO 时间）", "2026-10-08T09:30:00+08:00"),
        ("rejected", "上游这次运行有没有途经驳回（true / false；缺省不认途经驳回的，勾了「也认」才会是 true）", "false"),
        ("outputs", "上游运行的出口字段（写 event.outputs.<字段名>）", "{…}"),
    ),
    "source.changed": (
        ("source", "数据源的名字", "门店库"),
        ("source_id", "数据源的编号", "ds…"),
        ("counts", "变化的条数（写 event.counts.new 这次新记的要处理的条数，"
                   "event.counts.action / warning / info 各级还要人看的条数）", "{…}"),
    ),
}
#: 开放的那一级：event.outputs.<字段>、event.counts.<项>
_OPEN = {"workflow.finished": "outputs", "source.changed": "counts"}
_COUNT_KEYS = ("new", "action", "warning", "info", "pending")


def names(event: str) -> tuple[str, ...]:
    """这种事件能写在模板里的变量（带 event. 前缀），给报错列出来。"""
    out = [f"event.{key}" for key, _, _ in VARIABLES.get(event, ())]
    if event == "workflow.finished":
        out = [n if n != "event.outputs" else "event.outputs.<字段名>" for n in out]
    if event == "source.changed":
        out = [n for n in out if n != "event.counts"] + [f"event.counts.{k}" for k in _COUNT_KEYS]
    return tuple(out)


def allowed_path(path: str, event: str) -> bool:
    """event.* 的路径合不合这种事件：固定的几个名字，加上 outputs.<字段>（可以再往下取）、counts.<项>。"""
    if not path.startswith("event."):
        return False
    rest = path[len("event."):]
    keys = {key for key, _, _ in VARIABLES.get(event, ())}
    head, _, tail = rest.partition(".")
    if head not in keys:
        return False
    if head == _OPEN.get(event):
        if event == "source.changed":
            return not tail or tail in _COUNT_KEYS
        return bool(tail.strip())
    return not tail


def _zoned(text: Any, tz: str) -> str:
    """存的 UTC ISO 时间换成任务时区、只到秒的 ISO（同 schedule.fire_time 的写法）；读不出来的照原样。"""
    if not isinstance(text, str) or not text:
        return ""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    from app.scheduler import cron

    try:
        return moment.astimezone(cron.zone(tz)).isoformat(timespec="seconds")
    except ValueError:
        return moment.isoformat(timespec="seconds")


def values(event: str, payload: dict[str, Any] | None, tz: str, *, outputs: dict[str, Any] | None = None,
           ) -> dict[str, Any]:
    """模板里 event 下面的值（render_inputs 的 ctx["event"]）。时间按任务时区写；workflow.finished 的出口字段由派发时读到的
    那次运行的成果交进来。"""
    payload = payload or {}
    if event == "table.version":
        return {"table": payload.get("table") or "", "version": payload.get("version") or "",
                "period": payload.get("period") or "", "period_start": payload.get("period_start") or "",
                "period_end": payload.get("period_end") or "", "activated_at": _zoned(payload.get("activated_at"), tz)}
    if event == "workflow.finished":
        return {"workflow": payload.get("workflow") or "", "workflow_id": payload.get("workflow_id") or "",
                "run_id": payload.get("run_id") or "", "finished_at": _zoned(payload.get("finished_at"), tz),
                "rejected": "true" if payload.get("rejected") else "false", "outputs": dict(outputs or {})}
    if event == "source.changed":
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        return {"source": payload.get("source") or "", "source_id": payload.get("source_id") or "",
                "counts": {k: int(counts.get(k) or 0) for k in _COUNT_KEYS}}
    return {}


def variables(event: str) -> list[dict[str, str]]:
    """变量清单接口里 event.* 的那几项：[{name, label, example, note}]。"""
    out = []
    for key, label, example in VARIABLES.get(event, ()):
        name = "event.outputs.<字段名>" if (event, key) == ("workflow.finished", "outputs") else f"event.{key}"
        if (event, key) == ("source.changed", "counts"):
            name = "event.counts.new"
        out.append({"name": name, "label": label, "example": example, "note": ""})
    return out


# --------------------------------------------------------------------------
# 期次的写法
# --------------------------------------------------------------------------


def period_label(start: str | None, end: str | None) -> str:
    """这一期写成一个词：整月 2026-09、整季 2026-Q3、整年 2026、ISO 整周 2026-W40、单日 2026-09-30；别的写「起~止」。"""
    if not start:
        return ""
    if not end or end == start:
        return start
    try:
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return f"{start}~{end}"
    last_of_month = (d1 + timedelta(days=1)).day == 1
    if d0.day == 1 and last_of_month:
        if (d0.year, d0.month) == (d1.year, d1.month):
            return f"{d0.year:04d}-{d0.month:02d}"
        if d0.year == d1.year and d0.month == 1 and d1.month == 12:
            return f"{d0.year:04d}"
        if d0.year == d1.year and d0.month in (1, 4, 7, 10) and d1.month == d0.month + 2:
            return f"{d0.year:04d}-Q{(d0.month - 1) // 3 + 1}"
    if d0.weekday() == 0 and d1 - d0 == timedelta(days=6):
        year, week, _ = d0.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{start}~{end}"


# --------------------------------------------------------------------------
# 示例事件（预览、立即运行）
# --------------------------------------------------------------------------


@dataclass
class Sample:
    """按对象现在的情况凑出的一次事件：real 为假时是示例值（对象还没发生过这种事件），立即运行不拿它发起用到 event.* 的运行。"""

    event: str
    subject_id: str
    subject_name: str
    payload: dict[str, Any]
    outputs: dict[str, Any] = field(default_factory=dict)
    real: bool = True


async def sample(session: Any, trigger: dict[str, Any]) -> Sample:
    """预览、立即运行用的事件：上传表格取当前版本里最近的一期，上游工作流取最近一次成功的正式运行，数据源取现在的变化计数。
    对象不在、还没有过这种事件时给示例值（real=False）。"""
    from app.db.models import DataSource, Run, SourceSnapshot, TableImport, Workflow

    event, subject = trigger.get("event") or "", subject_of(trigger)
    if event in ("table.version", "source.changed"):
        source = await session.get(DataSource, subject) if subject else None
        name = source.name if source is not None else ""
        if event == "source.changed":
            if source is None:
                return Sample(event, subject, name, {"source": name or EXAMPLE_NOTE, "source_id": subject,
                                                     "counts": {"new": 2, "action": 2}}, real=False)
            from app.data import source_changes

            got = await source_changes.counts(session, subject)
            by = got.get("by_severity") or {}
            return Sample(event, subject, name, {"source": name, "source_id": subject, "counts": {
                "new": int(by.get("action") or 0), "action": int(by.get("action") or 0),
                "warning": int(by.get("warning") or 0), "info": int(by.get("info") or 0),
                "pending": int(got.get("pending") or 0)}})
        snap = await session.get(SourceSnapshot, source.current_snapshot_id) \
            if source is not None and source.current_snapshot_id else None
        imports = list((await session.execute(select(TableImport).where(TableImport.id.in_(
            [str(x) for x in snap.imports or []])))).scalars()) if snap is not None else []
        latest = max(imports, key=lambda i: (i.period_end or "", i.seq or 0), default=None)
        if snap is None or latest is None:
            return Sample(event, subject, name, {"table": name or EXAMPLE_NOTE, "version": "", "period": "2026-09",
                                                 "period_start": "2026-09-01", "period_end": "2026-09-30",
                                                 "activated_at": ""}, real=False)
        return Sample(event, subject, name, table_payload(
            name, snapshot_id=snap.id, period_start=latest.period_start, period_end=latest.period_end,
            activated_at=snap.activated_at or snap.created_at, replaced=False, seq=latest.seq))
    workflow = await session.get(Workflow, subject) if subject else None
    name = workflow.name if workflow is not None else ""
    query = select(Run).where(Run.workflow_id == subject, Run.run_class == "formal", Run.status == "succeeded")
    if trigger.get("app_id"):
        query = query.where(Run.app_id == trigger["app_id"])
    if trigger.get("origin_kind"):
        query = query.where(Run.origin_kind == trigger["origin_kind"])
    candidates = list((await session.execute(query.order_by(Run.finished_at.desc(), Run.id.desc()).limit(SAMPLE_SCAN)))
                      .scalars()) if subject else []
    # 和真派发同一套条件（matches）：缺省不认的途经驳回、应用补算跳过，取合条件的最近一次
    for run in candidates:
        payload = run_payload(run, await _rejected_via_of(session, run.id))
        if matches(trigger, event, subject, payload):
            return Sample(event, subject, name, payload, outputs=outputs_of(run.output))
    return Sample(event, subject, name, {"workflow": name or EXAMPLE_NOTE, "workflow_id": subject, "run_id": "",
                                         "finished_at": "", "rejected": False}, real=False)


#: 示例事件往回找几次成功的正式运行（找合条件的最近一次）
SAMPLE_SCAN = 20


async def _rejected_via_of(session: Any, run_id: str) -> list[Any] | None:
    """这次运行途经的驳回：run.finished 事件里的 rejected_via（W1-b 的驳回出口写的），没有为 None。"""
    from app.db.models import RunEvent

    data = (await session.execute(select(RunEvent.data).where(RunEvent.run_id == run_id, RunEvent.type == "run.finished")
                                  .order_by(RunEvent.seq.desc()).limit(1))).scalar()
    via = data.get("rejected_via") if isinstance(data, dict) else None
    return list(via) if isinstance(via, list) and via else None


def outputs_of(output: Any) -> dict[str, Any]:
    """给模板的出口字段（{{ event.outputs.* }}）：「_」开头的是系统写的（出具档位之类），不给。真派发、预览、立即运行共用。"""
    return {k: v for k, v in output.items() if not str(k).startswith("_")} if isinstance(output, dict) else {}


def table_payload(name: str, *, snapshot_id: str, period_start: str | None, period_end: str | None,
                  activated_at: datetime | None, replaced: bool, seq: int | None) -> dict[str, Any]:
    """table.version 的事件内容（发生处和示例共用一份写法）。"""
    return {"table": name, "version": snapshot_id, "period": period_label(period_start, period_end),
            "period_start": period_start or "", "period_end": period_end or "",
            "activated_at": _iso(activated_at), "replaced": bool(replaced), "seq": seq}


def run_payload(run: Any, rejected_via: Any = None) -> dict[str, Any]:
    """workflow.finished 的事件内容：不含出口字段（派发时按 run_id 读成果）。rejected_via 是途经了哪几个关卡的驳回（W1-b 的
    驳回出口；runner 收尾时交进来），没途经驳回为 None。"""
    via = (list(rejected_via) if isinstance(rejected_via, (list, tuple)) else [rejected_via]) if rejected_via else None
    return {"workflow": run.workflow_name or "", "workflow_id": run.workflow_id or "", "run_id": run.id,
            "finished_at": _iso(run.finished_at), "app_id": run.app_id, "origin_kind": run.origin_kind,
            "version": run.version, "rejected": bool(via), "rejected_via": via}


def window_key(event: str, payload: dict[str, Any] | None) -> str:
    """去抖时按什么分窗口：上传表格按期次分（9 月、10 月连着启用，各有各的触发，不把 9 月合掉），别的不分。"""
    if event != "table.version":
        return ""
    payload = payload or {}
    return str(payload.get("period") or f"{payload.get('period_start') or ''}~{payload.get('period_end') or ''}")


def _iso(moment: datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def uses_event(template: dict[str, str] | None) -> bool:
    """输入模板里有没有用到 {{ event.* }}（立即运行拿示例值时据此决定能不能发起）。"""
    from app.scheduler.service import _TEMPLATE_RE

    for text in (template or {}).values():
        for match in _TEMPLATE_RE.finditer(text if isinstance(text, str) else ""):
            if match.group(1).split("|")[0].strip().startswith("event."):
                return True
    return False
