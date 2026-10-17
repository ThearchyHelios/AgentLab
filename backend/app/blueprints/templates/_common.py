"""场景模板共用的零件（N3、§3.3–§3.4）：画节点、按卡片填确定性关节、按库分组的取数、口径卡、应用骨架、定时任务。

每个模板模块只写自己的骨架长什么样，怎么从卡片换成节点配置都在这里，各模板说法一致（P5）：
- 统计期：入口字段 period / period_start / period_end，定时任务的输入模板按「多久更新」卡片取（scheduler 的模板变量）；
- 谁来做：人工节点只写岗位（派给谁、超时升级给谁），岗位绑到哪些人由应用的岗位绑定定（bindings），和样例⑩同一个做法；
- 看哪些数：按取数节点分组，同一组一条 SQL（生成关节 sql），口径卡每个数一个指标式（生成关节 metric_expr），复用已发布
  口径卡的项落成 caliber_from；
- 应用骨架：apps/derive.py 的派生规则（_results、_entries、岗位、待办外观）加模板自己的排布。

每个模板的登记（TemplateDef）连同示例蓝图（测试和启动核对用）、选模板时的预置、给模型看的每组关节说明、模板自己认的问题
一起写，不另开旁路登记；这里的 prefill / template_problems 按模板 id 取登记来用。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.blueprints.templates import Joint, get_template

#: 实例化时交给模板的上下文（蓝图 id、工作流 id、定时任务 id、记录表、导入的图）：放在 bp 的这个键下，模板读、不写回
CONTEXT_KEY = "_context"
#: 生成关节没填时的占位。SQL 是一句注释（试运行和发布前由 tpl.joint_unfilled 挡住）；指标式是空值（照常能过画布校验）
SQL_PLACEHOLDER = "-- 待填：助手还没写这条 SQL"
METRIC_PLACEHOLDER = "None"
#: 口径卡、成果、解析节点的固定 id
CALIBER = "caliber"
DONE = "done"
START = "start"

# ==========================================================================
# 写给模型的文字（每组关节的说明）
# ==========================================================================

SQL_BRIEF_HEAD_PROMPT = ("取数节点 {node}（工具 db_query__{source}）：写一条只读 SQL（只能 SELECT，一条语句），结果{shape}。"
                         "这个工具已经连到数据源「{source}」：表名照下面写的（就是这个数据源里的表名），前面不要加「{source}.」，"
                         "数据库里没有叫这个名字的库。{period}")
SQL_PERIOD_PROMPT = ("统计期用入口字段：从 '{{ input.period_start }}' 到 '{{ input.period_end }}'（日期，含两端，"
                     "写在 SQL 里要带单引号）。")
SQL_ONE_ROW_PROMPT = "只出一行，每个要算的数一列（或者算它要用的基础量各一列），列名用英文小写和下划线"
SQL_ITEM_PROMPT = "- {label}（指标 id {id}{unit}）：怎么算：{measure}；表：{table}；业务日期：{date}；筛选：{filter}；粒度：{grain}"
SQL_REUSED_PROMPT = "复用的已发布口径卡要这条 SQL 交出这几列（列名照抄，别改）：{columns}"
METRIC_BRIEF_PROMPT = ("口径卡 {card} 每个数写一个指标式（关节 metric_<指标 id>）：{metrics}。只能用 cell(nodes.{node}, 0, '列名') "
                       "取上面那条 SQL 第一行的格，再做加减乘除、round、abs、min、max、比较和 … if … else …；分母可能为 0 "
                       "时写成 … if 分母 else None。不能用别的函数，不能引用别的节点。")
METRIC_ITEM_PROMPT = "{id}（{label}{unit}）"
UNKNOWN_PROMPT = "未写"


# ==========================================================================
# 节点、连线、卡片上的值
# ==========================================================================


def node(node_id: str, node_type: str, label: str, **config: Any) -> dict[str, Any]:
    """一个节点（和 seed.py 的 _n 同一种写法）。"""
    return {"id": node_id, "type": node_type, "position": {"x": 0, "y": 0},
            "data": {"label": label, "config": config}}


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"source": source, "target": target}
    if handle:
        out["sourceHandle"] = handle
    return out


def value(item: Any) -> Any:
    """卡片上一项的值（{value, source, status} → value）。"""
    return item.get("value") if isinstance(item, dict) else None


def context(bp: dict[str, Any]) -> dict[str, Any]:
    ctx = bp.get(CONTEXT_KEY)
    return ctx if isinstance(ctx, dict) else {}


def with_graph(spec: dict[str, Any], graph: Any) -> dict[str, Any]:
    """只有卡片（Blueprint.spec）、没有实例化上下文的地方，带上蓝图存的图再交给模板的函数：自由形的「谁来做」、要不要
    核实人都按导入的图算（freeform.people_rows / needs_reconciler）。"""
    return {**spec, CONTEXT_KEY: {**context(spec), "graph": graph}}


def is_freeform(spec: dict[str, Any]) -> bool:
    """卡片上选的是导入图的模板（自由形）。"""
    ref = spec.get("template")
    template = get_template(str(ref.get("id") or "")) if isinstance(ref, dict) else None
    return template is not None and template.imports_graph


def app_name(bp: dict[str, Any], fallback: str) -> str:
    """应用和工作流的名字：实例化时给的名字，没给就用目的的前一段。"""
    name = str(context(bp).get("name") or "").strip()
    if name:
        return name[:200]
    purpose = str(value(bp.get("purpose")) or "").strip()
    return (re.split(r"[，。；,;\n]", purpose)[0][:40] if purpose else "") or fallback


def safe_id(text: str) -> str:
    """节点 id 用的一段：小写字母、数字、下划线；全是中文的名字换不出来时为空串，由调用方编号。"""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", text.lower())).strip("_")[:24]


# ==========================================================================
# 统计期与定时任务
# ==========================================================================

#: 统计哪一期 → (说法, 期名、第一天、最后一天各取哪个定时任务模板变量，scheduler/service.VARIABLES)
PERIODS: dict[str, tuple[str, str, str, str]] = {
    "today": ("今天", "today", "today", "today"),
    "yesterday": ("昨天", "yesterday", "yesterday", "yesterday"),
    "this_iso_week": ("本周", "this_week", "this_week_start", "today"),
    "last_iso_week": ("上周", "last_week", "last_week_start", "last_week_end"),
    "this_month": ("本月", "this_month", "this_month_start", "today"),
    "last_month": ("上月", "last_month", "last_month_start", "last_month_end"),
}
#: 没写统计哪一期时按更新频率取：每天看昨天，每周看上周，每月看上月
DEFAULT_PERIOD = {"daily": "yesterday", "weekly": "last_iso_week", "monthly": "last_month"}
PERIOD_FIELDS = ("period", "period_start", "period_end")


def period_fields() -> list[dict[str, Any]]:
    """统计期的入口字段。第一天、最后一天都含在内（SQL 说明里照这个写）。"""
    return [
        {"name": "period", "label": "统计期", "description": "这一期叫什么，例如 2026-W41、2026-09"},
        {"name": "period_start", "label": "第一天", "type": "string", "format": "date", "required": True,
         "description": "统计期的第一天（含）"},
        {"name": "period_end", "label": "最后一天", "type": "string", "format": "date", "required": True,
         "description": "统计期的最后一天（含）"},
    ]


def period_of(cadence: dict[str, Any] | None) -> str:
    if not isinstance(cadence, dict):
        return "yesterday"
    return cadence.get("period") or DEFAULT_PERIOD.get(cadence.get("preset") or "", "yesterday")


def cron_of(cadence: dict[str, Any]) -> str:
    """更新频率 → 五字段 cron。周几按卡片的 1–7（周一到周日），cron 里周日写 0。"""
    hour, minute = (int(x) for x in str(cadence["time"]).split(":"))
    if cadence["preset"] == "weekly":
        return f"{minute} {hour} * * {int(cadence['weekday']) % 7}"
    if cadence["preset"] == "monthly":
        return f"{minute} {hour} {int(cadence['day'])} * *"
    return f"{minute} {hour} * * *"


def period_template(cadence: dict[str, Any] | None) -> dict[str, str]:
    """定时任务的输入模板：统计期三个字段各取哪个变量。"""
    _, name, start, end = PERIODS[period_of(cadence)]
    return {"period": f"{{{{ schedule.{name} }}}}", "period_start": f"{{{{ schedule.{start} }}}}",
            "period_end": f"{{{{ schedule.{end} }}}}"}


def schedule_of(bp: dict[str, Any], name: str, *, inputs: dict[str, str] | None = None) -> dict[str, Any] | None:
    """定时任务参数（api/schedules 建任务的字段）：正式运行、先停用（发布单在应用发布后才启用，N5）。没定更新频率为 None。"""
    cadence = value(bp.get("cadence"))
    if not isinstance(cadence, dict):
        return None
    return {"name": f"{name}（定时）", "cron": cron_of(cadence), "timezone": cadence["tz"], "run_class": "formal",
            "input_template": period_template(cadence) if inputs is None else inputs, "enabled": False}


# ==========================================================================
# 填什么：入口字段与表单
# ==========================================================================

#: 「填什么」的字段属性里能带进入口节点字段的键（engine/nodes/io.py、apps/forms.input_schema 认的那几样）
_INPUT_KEYS = ("type", "description", "default", "enum", "format", "minimum", "maximum", "x-widget", "x-enum-labels")


def form_fields(bp: dict[str, Any], *, required: bool = True) -> list[dict[str, Any]]:
    """「填什么」→ 入口节点的字段。required=False 时都不在入口节点上必填（同一张图另有不填表单的路径，必填由应用的入口
    表单把关）。"""
    out: list[dict[str, Any]] = []
    for f in bp.get("fields") or []:
        prop = value(f.get("spec")) or {}
        out.append({"name": f["name"], "label": str(prop.get("title") or f["name"]),
                    **{k: copy.deepcopy(prop[k]) for k in _INPUT_KEYS if k in prop},
                    **({"required": True} if required and prop.get("required") else {})})
    return out


def entry_form(bp: dict[str, Any], title: str = "") -> dict[str, Any]:
    """入口表单（agentlab.form/1）：直接用「填什么」卡片上的表单属性，选项文字、控件这些外观也带上。"""
    from app.apps.spec import FORM_FORMAT

    props: dict[str, Any] = {}
    required: list[str] = []
    for f in bp.get("fields") or []:
        prop = dict(value(f.get("spec")) or {"type": "string"})
        if prop.pop("required", False):
            required.append(f["name"])
        prop.setdefault("title", f["name"])
        props[f["name"]] = prop
    return {"format": FORM_FORMAT, "type": "object", **({"title": title} if title else {}), "properties": props,
            "required": required, "x-order": list(props)}


def field_ref(name: str) -> str:
    return f"{{{{ input.{name} }}}}"


def fields_message(bp: dict[str, Any]) -> str:
    """待办里列出表单内容：每个字段一行「名称：值」。"""
    lines = []
    for f in bp.get("fields") or []:
        title = (value(f.get("spec")) or {}).get("title") or f["name"]
        lines.append(f"{title}：{field_ref(f['name'])}")
    return "\n".join(lines)


# ==========================================================================
# 谁来做：人工节点的确定性关节
# ==========================================================================


def slot_of(step: dict[str, Any]) -> str:
    """这一步派给哪个岗位：处理人就是一个岗位时用它的名字（同一个岗位可以出现在几步里），否则用步骤 id。"""
    who = value(step.get("who")) or []
    if len(who) == 1 and who[0].get("kind") == "slot":
        return str(who[0]["name"])
    return str(step["id"])


def escalate_slot(step: dict[str, Any]) -> str | None:
    timeout = value(step.get("on_timeout"))
    if not isinstance(timeout, dict) or timeout.get("action") != "escalate":
        return None
    to = timeout.get("to") or []
    if len(to) == 1 and to[0].get("kind") == "slot":
        return str(to[0]["name"])
    return f"{step['id']}_up"


def assignee_of(step: dict[str, Any]) -> dict[str, Any]:
    who = value(step.get("who")) or []
    if len(who) == 1 and who[0].get("kind") == "initiator":
        return {"kind": "initiator"}
    return {"kind": "slot", "slot": slot_of(step)}


def policy_of(step: dict[str, Any]) -> Any:
    policy = value(step.get("policy")) or {"kind": "any"}
    if policy["kind"] == "quorum":
        return {"quorum": int(policy["m"])}
    return policy["kind"]


def timeout_of(step: dict[str, Any]) -> dict[str, Any] | None:
    timeout = value(step.get("on_timeout"))
    if not isinstance(timeout, dict):
        return None
    out: dict[str, Any] = {"action": timeout["action"]}
    if timeout["action"] == "escalate":
        out["to_slot"] = escalate_slot(step)
    if timeout.get("every"):
        out["every"] = timeout["every"]
    return out


def human(step: dict[str, Any], index: int, node_id: str, *, title: str, message: str, mode: str = "approve",
          form: dict[str, Any] | None = None, **extra: Any) -> tuple[dict[str, Any], list[Joint]]:
    """「谁来做」的一步 → 人工节点和它的确定性关节（派给谁、几个人同意、时限、超时怎么办）。"""
    config: dict[str, Any] = {"mode": mode, "title": title, "message": message, "assignee": assignee_of(step)}
    policy = policy_of(step)
    if mode == "approve" or policy == "any":
        config["policy"] = policy
    if due := value(step.get("due")):
        config["due"] = due
    if timeout := timeout_of(step):
        config["on_timeout"] = timeout
    if form is not None:
        config["form"] = form
    config.update(extra)
    label = str(value(step.get("label")) or step["id"])
    base = f"/people/{index}"
    joints = [Joint(id=f"{node_id}.assignee", kind="assignee", node_id=node_id, field="/assignee",
                    from_card=f"{base}/who", label=f"{label}：派给谁"),
              Joint(id=f"{node_id}.policy", kind="policy", node_id=node_id, field="/policy",
                    from_card=f"{base}/policy", label=f"{label}：几个人同意算通过")]
    if "due" in config:
        joints.append(Joint(id=f"{node_id}.due", kind="due", node_id=node_id, field="/due", from_card=f"{base}/due",
                            label=f"{label}：时限"))
    if "on_timeout" in config:
        joints.append(Joint(id=f"{node_id}.on_timeout", kind="on_timeout", node_id=node_id, field="/on_timeout",
                            from_card=f"{base}/on_timeout", label=f"{label}：超时怎么办"))
    return node(node_id, "human", label, **config), joints


def threshold_condition(threshold: dict[str, Any]) -> str:
    """金额线 → 分支条件「input.字段 比较 常量」（只有比较，没有算术，§3.4 threshold）。"""
    return f"input.{threshold['field']} {threshold['op']} {json.dumps(threshold['value'], ensure_ascii=False)}"


def title_of(bp: dict[str, Any], label: str) -> str:
    """待办标题带上表单的前两个字段：同一个人的两笔申请，待办列表里一眼分得开（同样例⑩）。"""
    refs = [field_ref(f["name"]) for f in (bp.get("fields") or [])[:2]]
    return f"{label}：{' · '.join(refs)}" if refs else label


@dataclass
class Chain:
    """一串审批：入口连到 head，每一步通过去下一步（写了条件的先过分支，不满足直接跳过），驳回都到 rejected。"""

    head: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    joints: list[Joint]
    #: 每一步的意见（成果字段）
    notes: list[dict[str, Any]]


def approval_chain(bp: dict[str, Any], *, after: str, rejected: str) -> Chain:
    """「谁来做」→ 一串人工节点。从后往前排：每一步通过之后去哪（下一步的分支或人工节点，最后是 after）。"""
    steps = bp.get("people") or []
    parts: list[tuple[list[dict[str, Any]], list[dict[str, Any]], list[Joint]]] = []
    notes: list[dict[str, Any]] = []
    for i in range(len(steps) - 1, -1, -1):
        step = steps[i]
        review_id = f"review_{step['id']}"
        label = str(value(step.get("label")) or step["id"])
        review, review_joints = human(step, i, review_id, title=title_of(bp, label), message=fields_message(bp))
        step_nodes = [review]
        step_edges = [edge(review_id, after, "approved"), edge(review_id, rejected, "rejected")]
        step_joints = list(review_joints)
        head = review_id
        threshold = value(step.get("threshold"))
        if isinstance(threshold, dict):
            gate_id = f"gate_{step['id']}"
            step_nodes.insert(0, node(gate_id, "branch", f"是否要{label}", mode="expression", cases=[
                {"key": "hit", "condition": threshold_condition(threshold), "label": f"要{label}"}]))
            step_edges = [edge(gate_id, review_id, "hit"), edge(gate_id, after, "default"), *step_edges]
            step_joints.append(Joint(id=f"{gate_id}.threshold", kind="threshold", node_id=gate_id,
                                     field="/cases/0/condition", from_card=f"/people/{i}/threshold",
                                     label=f"{label}：走这一步的条件"))
            head = gate_id
        parts.insert(0, (step_nodes, step_edges, step_joints))
        notes.insert(0, {"name": f"{label}意见", "value": f"{{{{ nodes.{review_id}.note }}}}"})
        after = head
    return Chain(head=after, nodes=[n for p in parts for n in p[0]], edges=[e for p in parts for e in p[1]],
                 joints=[j for p in parts for j in p[2]], notes=notes)


def bindings_of(bp: dict[str, Any]) -> dict[str, Any]:
    """岗位绑定（应用的 draft_bindings 形状 {"slots": {岗位: [{kind: user|group, name}]}}）：处理人、超时升级给的人
    写的是具体的人或组的，按这一步的岗位绑上；写的是岗位的，岗位绑到谁要在应用里定（发布单的「你来定」）。"""
    slots: dict[str, list[dict[str, str]]] = {}

    def bind(slot: str | None, principals: list[dict[str, Any]]) -> None:
        if not slot:
            return
        people = [{"kind": p["kind"], "name": p["name"]} for p in principals if p.get("kind") in ("user", "group")]
        if people:
            have = slots.setdefault(slot, [])
            have += [p for p in people if p not in have]

    for step in bp.get("people") or []:
        bind(slot_of(step), value(step.get("who")) or [])
        timeout = value(step.get("on_timeout"))
        if isinstance(timeout, dict):
            bind(escalate_slot(step), timeout.get("to") or [])
    if needs_reconciler(bp):
        # 写回的结果不明时谁去核实：只在目标能力没登记查询时绑（应用骨架这时才声明核实岗位，app_base）。补登了查询的，
        # 卡片上留着的人不再绑：平台自己查，用不上
        from app.apps.derive import RECONCILER

        bind(RECONCILER, value((bp.get("writeback") or {}).get("reconciler")) or [])
    return {"slots": slots}


def slot_labels(bp: dict[str, Any]) -> dict[str, str]:
    """岗位 → 给人看的名字：步骤的名字，升级岗位写「……超时升级」。"""
    out: dict[str, str] = {}
    for step in bp.get("people") or []:
        label = str(value(step.get("label")) or step["id"])
        out.setdefault(slot_of(step), slot_of(step) if slot_of(step) != step["id"] else label)
        if up := escalate_slot(step):
            out.setdefault(up, up if up != f"{step['id']}_up" else f"{label}超时升级")
    return out


# ==========================================================================
# 写回哪里：调用工具节点的确定性关节
# ==========================================================================


def write_args(mapping: dict[str, Any], *, metrics: dict[str, str] | None = None,
               rows: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """字段对应 → 调用工具节点的 args（模板写法）。metrics：指标 id → 取值链；rows：核对出来的行的取值链。
    交回 (args, 对不上的取值)。"""
    args: dict[str, Any] = {}
    bad: list[str] = []
    for param, source in (mapping or {}).items():
        if isinstance(source, bool) or isinstance(source, (int, float)):
            args[param] = source
        elif source == "rows" and rows:
            args[param] = f"{{{{ {rows} | compact }}}}"
        elif isinstance(source, str) and source.startswith("form."):
            args[param] = field_ref(source[len("form."):])
        elif isinstance(source, str) and source.startswith("metric.") and source[len("metric."):] in (metrics or {}):
            args[param] = f"{{{{ {(metrics or {})[source[len('metric.'):]]} }}}}"
        elif isinstance(source, str) and len(source) >= 2 and source[0] == source[-1] == "'":
            args[param] = source[1:-1]
        else:
            bad.append(f"{param}：{source}")
    return args, bad


def write_node(bp: dict[str, Any], node_id: str, *, confirm_slot: str | None, metrics: dict[str, str] | None = None,
               rows: str | None = None, label: str = "写回") -> tuple[dict[str, Any], list[Joint], list[str]]:
    """「写回哪里」→ 调用工具节点：工具是目标能力，参数按字段对应；要人确认时审批写「每次都审批」并派给 confirm_slot，
    不要确认时写「全部自动放行」（能力登记里还要允许无人值守写入才真不问人，nodes/tools._run_write）。"""
    wb = bp.get("writeback") or {}
    capability = value(wb.get("capability")) or ""
    args, bad = write_args(value(wb.get("mapping")) or {}, metrics=metrics, rows=rows)
    config: dict[str, Any] = {"tool": capability, "args": args}
    if value(wb.get("confirm")) is False:
        config["approval"] = "never"
    else:
        config["approval"] = "always"
        if confirm_slot:
            config["assignee"] = {"kind": "slot", "slot": confirm_slot}
    joints = [Joint(id=f"{node_id}.capability", kind="capability", node_id=node_id, field="/tool",
                    from_card="/writeback/capability", label=f"{label}：目标能力"),
              Joint(id=f"{node_id}.args", kind="capability", node_id=node_id, field="/args",
                    from_card="/writeback/mapping", label=f"{label}：字段对应")]
    return node(node_id, "tool", label, **config), joints, bad


def write_lookup(capability: str) -> bool:
    """写入能力有没有登记查询（结果未知时按幂等键查实际结果）：没有的要绑核实岗位（apps/derive.RECONCILER）。"""
    from app.capabilities.registry import cached_view

    view = cached_view(capability) or {}
    return bool(view.get("lookup"))


def needs_reconciler(bp: dict[str, Any]) -> bool:
    """「写回哪里」要不要定「结果不明时谁去核实」：模板用到写回卡片、选了目标能力、这项能力没登记查询（和应用骨架加核实
    岗位同一个判断，app_base）。结果不明时平台不自动重发，查不了就只能派人去目标系统里看。蓝图检查、岗位绑定、交给前端的
    蓝图（reconciler_needed）共用这一份。"""
    ref = bp.get("template")
    template = get_template(str(ref.get("id") or "")) if isinstance(ref, dict) else None
    if template is not None and template.imports_graph:
        # 自由形没有「写回哪里」：按导入的图里的写入算（bp 要带着图，with_graph），核实人存在 /writeback/reconciler
        # （05-已知遗留 (d)）
        from app.blueprints.templates import freeform

        return freeform.needs_reconciler(bp)
    if template is None or "writeback" not in template.cards:
        return False
    capability = value((bp.get("writeback") or {}).get("capability"))
    return bool(capability) and not write_lookup(str(capability))


# ==========================================================================
# 看哪些数：按取数节点分组
# ==========================================================================


@dataclass
class Group:
    """一个取数节点和它要算的数。同一组的 SQL 和指标式一起交给模型填（生成关节的 group），不同组并行填。"""

    node_id: str
    source: str
    #: 本地算的数据项（口径卡里各一个指标）
    items: list[dict[str, Any]] = field(default_factory=list)
    #: 复用已发布口径卡的数据项：它们读这个取数节点（reuse.inputs）
    reused: list[dict[str, Any]] = field(default_factory=list)
    #: 复用的口径卡要这条 SQL 交出的列
    columns: list[str] = field(default_factory=list)


def data_groups(bp: dict[str, Any], *, source_map: dict[str, str] | None = None,
                prefix: str = "fetch") -> tuple[list[Group], list[dict[str, Any]]]:
    """「看哪些数」→ 取数分组：同一个库一条 SQL。复用的口径卡读的是原工作流里某个取数节点（cell(nodes.X, …)），新图里
    那个库的取数节点就叫 X、交出同样的列，复用才算得出来。交回 (分组, 问题)。source_map 把模板自己的占位库名换成真名
    （填报登记的记录表）。"""
    from app.tools.datasource import QUERY_PREFIX

    groups: dict[str, Group] = {}
    by_source: dict[str, str] = {}
    problems: list[dict[str, Any]] = []
    source_map = source_map or {}

    def problem(code: str, i: int, message: str) -> None:
        problems.append({"code": code, "level": "error", "path": f"/data/{i}", "message": message})

    for i, item in enumerate(bp.get("data") or []):
        reuse = item.get("reuse")
        if not reuse:
            continue
        label = value(item.get("label")) or item["id"]
        inputs = reuse.get("inputs")
        if not inputs:
            problem("bp.data_unmapped", i, f"「{label}」复用的口径卡还没核对过它读哪些列：请重新从口径卡里选一次")
            continue
        for inp in inputs:
            tool = str(inp.get("tool") or "")
            if not tool.startswith(QUERY_PREFIX):
                problem("bp.template_mismatch", i, f"「{label}」复用的口径卡不是从数据库取数的，模板里接不上：请按表重新对")
                break
            source, node_id = tool[len(QUERY_PREFIX):], str(inp["node"])
            if by_source.get(source, node_id) != node_id or (node_id in groups and groups[node_id].source != source):
                problem("bp.template_mismatch", i, f"「{label}」复用的口径卡和别的口径卡取数的方式对不上：请只复用其中一张，"
                                                   "其余按表重新对")
                break
            by_source[source] = node_id
            group = groups.setdefault(node_id, Group(node_id=node_id, source=source))
            if item not in group.reused:
                group.reused.append(item)
            group.columns += [str(c) for c in inp.get("columns") or [] if str(c) not in group.columns]
    used = set(groups)
    for item in bp.get("data") or []:
        table = value(item.get("table"))
        if item.get("reuse") or not isinstance(table, dict):
            continue
        source = source_map.get(table["source"], table["source"])
        node_id = by_source.get(source)
        if node_id is None:
            base = safe_id(source)
            node_id = f"{prefix}_{base}" if base else f"{prefix}_{len(used) + 1}"
            while node_id in used:
                node_id = f"{node_id}_{len(used) + 1}"
            used.add(node_id)
            by_source[source] = node_id
        groups.setdefault(node_id, Group(node_id=node_id, source=source)).items.append(item)
    return list(groups.values()), problems


def fetch_nodes(groups: list[Group]) -> tuple[list[dict[str, Any]], list[Joint]]:
    """每组一个取数节点（调用工具 db_query__库名），SQL 是生成关节。"""
    nodes: list[dict[str, Any]] = []
    joints: list[Joint] = []
    for g in groups:
        nodes.append(node(g.node_id, "tool", f"取数（{g.source}）", tool=f"db_query__{g.source}",
                          args={"sql": SQL_PLACEHOLDER}))
        joints.append(Joint(id=f"sql_{g.node_id}", kind="sql", node_id=g.node_id, field="/args/sql", group=g.node_id,
                            label=f"取数的 SQL（{g.source}）"))
    return nodes, joints


def caliber_node(bp: dict[str, Any], groups: list[Group], name: str) -> tuple[dict[str, Any] | None, list[Joint]]:
    """本地算的数都进一张口径卡，每个数一个指标式（生成关节）。缺输入记成空值，交给出具契约按必需指标判档。"""
    metrics: list[dict[str, Any]] = []
    joints: list[Joint] = []
    for g in groups:
        for item in g.items:
            metric: dict[str, Any] = {"id": item["id"], "name": str(value(item.get("label")) or item["id"]),
                                      "expression": METRIC_PLACEHOLDER}
            if unit := value(item.get("unit")):
                metric["unit"] = unit
            joints.append(Joint(id=f"metric_{item['id']}", kind="metric_expr", node_id=CALIBER,
                                field=f"/metrics/{len(metrics)}/expression", group=g.node_id,
                                label=f"指标式：{metric['name']}"))
            metrics.append(metric)
    if not metrics:
        return None, []
    return node(CALIBER, "metrics", "口径卡", caliber=f"{name}口径", caliber_version="v1", on_missing="null",
                metrics=metrics), joints


def reuse_nodes(groups: list[Group]) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    """复用已发布口径卡的项各一张卡（caliber_from 固定到那一版），交回 (节点, 数据项, 读的取数节点)。"""
    out = []
    for g in groups:
        for item in g.reused:
            reuse = item["reuse"]
            out.append((node(f"reuse_{item['id']}", "metrics", f"复用口径：{value(item.get('label')) or item['id']}",
                             caliber_from={"workflow_id": reuse["workflow_id"], "workflow_version": reuse["version"],
                                           "node_id": reuse["node_id"]}), item, g.node_id))
    return out


def metric_paths(caliber: dict[str, Any] | None) -> dict[str, str]:
    """口径卡里每个指标的取值链（成果字段、写回参数用）：指标 id → nodes.caliber.metrics[i].value。"""
    if caliber is None:
        return {}
    return {m["id"]: f"nodes.{CALIBER}.metrics[{i}].value"
            for i, m in enumerate(caliber["data"]["config"].get("metrics") or [])}


def board_output(bp: dict[str, Any], caliber: dict[str, Any] | None,
                 reused: list[tuple[dict[str, Any], dict[str, Any], str]],
                 node_id: str = DONE, label: str = "出具") -> tuple[dict[str, Any], list[Joint]]:
    """按指标出具的成果（engine/issuance.metrics_only、governance.board_only）：契约只写「指标来自」和必需指标，成果字段
    每个本地指标一项（值就是指标值，回指得上）。复用的指标在应用里按指标块看，不进成果字段。"""
    paths = metric_paths(caliber)
    fields = []
    for m in (caliber["data"]["config"]["metrics"] if caliber else []):
        unit = f"（{m['unit']}）" if m.get("unit") else ""
        fields.append({"name": f"{m['name']}{unit}", "value": f"{{{{ {paths[m['id']]} }}}}"})
    sources = ([CALIBER] if caliber else []) + [n["id"] for n, _, _ in reused]
    required = [m["id"] for m in (caliber["data"]["config"]["metrics"] if caliber else [])] + \
        [item["reuse"]["metric_id"] for _, item, _ in reused]
    contract = {"metrics_from": sources, "required": required, "strict": True}
    joints = [Joint(id=f"{node_id}.contract", kind="contract", node_id=node_id, field="/contract", from_card="/data",
                    label="出具契约：必需指标")]
    return node(node_id, "output", label, fields=fields, contract=contract), joints


def data_briefs(bp: dict[str, Any], groups: list[Group], *, shape: str = SQL_ONE_ROW_PROMPT,
                period: bool = True) -> dict[str, str]:
    """每组关节给模型的说明：要算哪些数、用哪张表、业务日期、筛选，口径卡的指标式怎么写。"""
    out: dict[str, str] = {}
    for g in groups:
        lines = [SQL_BRIEF_HEAD_PROMPT.format(node=g.node_id, source=g.source, shape=shape,
                                              period=SQL_PERIOD_PROMPT if period else "")]
        for item in g.items:
            lines.append(item_brief(item))
        if g.columns:
            lines.append(SQL_REUSED_PROMPT.format(columns="、".join(g.columns)))
        if g.items:
            metrics = [METRIC_ITEM_PROMPT.format(id=item["id"], label=value(item.get("label")) or item["id"],
                                                 unit=f"，单位 {value(item.get('unit'))}" if value(item.get("unit")) else "")
                       for item in g.items]
            lines.append(METRIC_BRIEF_PROMPT.format(card=CALIBER, node=g.node_id, metrics="、".join(metrics)))
        out[g.node_id] = "\n".join(lines)
    return out


def item_brief(item: dict[str, Any]) -> str:
    table = value(item.get("table")) or {}
    date = value(item.get("date")) or {}
    unit = value(item.get("unit"))

    def text(key: str) -> str:
        return str(value(item.get(key)) or UNKNOWN_PROMPT)

    return SQL_ITEM_PROMPT.format(
        label=value(item.get("label")) or item["id"], id=item["id"], unit=f"，单位 {unit}" if unit else "",
        # 只写数据源里的表名（多 schema 的库，目录里的名字本身带 schema 前缀）：以前写成「数据源名.表名」，模型照抄进 SQL，
        # 数据源名被数据库当成库名，跑起来报库不存在
        measure=text("measure"), table=str(table.get("table") or UNKNOWN_PROMPT) if table else UNKNOWN_PROMPT,
        date=(f"{date.get('column')}" + (f"（{date['rule']}）" if date.get("rule") else "")) if date else UNKNOWN_PROMPT,
        filter=text("filter"), grain=text("grain"))


def basis_of(brief: str, template_id: str, version: int) -> str:
    """一组生成关节的依据的指纹：说明变了（卡片改了），之前填的就不算数了，要重新填。"""
    return hashlib.sha256(f"{template_id}@{version}\n{brief}".encode()).hexdigest()[:16]


# ==========================================================================
# 应用骨架
# ==========================================================================


def app_base(bp: dict[str, Any], graph: dict[str, Any], *, use: str, name: str, description: str = "",
             writes: dict[str, str] | None = None) -> dict[str, Any]:
    """按 apps/derive.py 的派生规则出应用骨架的公共部分：钉住项（还没发布，版本为空）、岗位、待办外观、核实岗位。
    writes 是写外部系统的调用工具节点 → 能力名（模板知道哪几个是写入，不用查库）。页面、视图、入口由各模板排。"""
    from app.apps.derive import PIN, RECONCILER
    from app.apps.spec import APP_FORMAT
    from app.apps.tree import node_map, slot_label, slot_refs
    from app.core.artifact_store import graph_hash

    ctx = context(bp)
    pin: dict[str, Any] = {"alias": PIN, "version": None, "graph_hash": graph_hash(graph), "level": None, "use": use}
    if ctx.get("workflow_id"):
        pin["workflow_id"] = ctx["workflow_id"]
    else:
        # 还没有工作流（个人应用、还没保存的草稿）：固定到蓝图上，图在蓝图里
        pin["blueprint"] = ctx.get("blueprint_id") or "draft"
    spec: dict[str, Any] = {
        "format": APP_FORMAT, "name": name, "description": description, "icon": "app", "pins": [pin], "slots": {},
        "entries": [], "forms": {}, "task_ui": {}, "views": {}, "pages": [], "hooks": [], "notify": [],
    }
    # 岗位的显示名：卡片上起了名字的用它（审批模板的步骤名）；卡片上就是岗位名本身的（处理人写的是岗位、自由形导入的图）按
    # 用到它的节点名拼（apps/tree.slot_label，同派生）
    labels = slot_labels(bp)
    refs = slot_refs(graph, writes or {})
    for ref in refs:
        label = labels.get(ref["slot"])
        spec["slots"].setdefault(ref["slot"], {"label": label if label and label != ref["slot"]
                                               else slot_label(ref["slot"], refs)})
    for nid, n in node_map(graph).items():
        if n["type"] == "human":
            spec["task_ui"][nid] = {"title": n["title"]}
    if writes and not all(write_lookup(cap) for cap in writes.values()):
        spec["slots"][RECONCILER] = {"label": "动作核实"}
    return spec


def instance_page(graph: dict[str, Any], writes: dict[str, str] | None = None, *,
                  results: bool = True) -> dict[str, Any]:
    """实例页（同 apps/derive.py）：概况、进度、各人工节点的待办表单和会签进度、写入动作的状态、这一次的结果。"""
    from app.apps.derive import INSTANCE_VIEW, _results, _safe
    from app.apps.tree import node_map

    nodes = node_map(graph)
    humans = [nid for nid, n in nodes.items() if n["type"] == "human"]
    return {"id": "instance", "title": "实例", "body": [
        {"id": "inst_header", "type": "instance_header"},
        {"id": "inst_timeline", "type": "instance_timeline"},
        *[c for nid in humans for c in (
            {"id": f"task_{_safe(nid)}", "type": "task_form", "bind": {"task": nid}},
            {"id": f"signoffs_{_safe(nid)}", "type": "signoffs", "bind": {"task": nid}})],
        *[{"id": f"action_{_safe(nid)}", "type": "action_status", "props": {"label": nodes[nid]["title"][:200]},
           "bind": {"action": nid}} for nid in (writes or {})],
        *(_results(nodes, INSTANCE_VIEW, "inst") if results else []),
    ]}


def series_section(graph: dict[str, Any], view: str, prefix: str) -> list[dict[str, Any]]:
    """趋势：口径卡每个本地指标一条（同一来源最近几次封存运行的值，apps/resolve._series）。"""
    from app.apps.derive import _safe
    from app.apps.tree import node_map

    card = node_map(graph).get(CALIBER)
    metrics = [m for m in (card or {}).get("config", {}).get("metrics") or [] if isinstance(m, dict) and m.get("id")]
    if not metrics:
        return []
    return [{"id": f"{prefix}_trend", "type": "section", "props": {"title": "趋势"}, "children": [
        {"id": f"{prefix}_series_{_safe(str(m['id']))}", "type": "series",
         "props": {"label": str(m.get("name") or m["id"])[:200], "chart": "line"},
         "bind": {"view": view, "series": f"{CALIBER}.{m['id']}"}} for m in metrics]}]


def reused_section(bp: dict[str, Any], graph: dict[str, Any], view: str, prefix: str) -> list[dict[str, Any]]:
    """复用口径卡的指标：派生规则只认本地写了指标的口径卡，复用的那几个按数据项各放一个指标块。"""
    from app.apps.derive import _safe
    from app.apps.tree import node_map

    nodes = node_map(graph)
    blocks = []
    for item in bp.get("data") or []:
        reuse = item.get("reuse")
        if reuse and f"reuse_{item['id']}" in nodes:
            blocks.append({"id": f"{prefix}_reuse_{_safe(item['id'])}", "type": "metric",
                           "props": {"label": str(value(item.get("label")) or reuse["metric_id"])[:200]},
                           "bind": {"view": view, "metric": f"reuse_{item['id']}.{reuse['metric_id']}"}})
    if not blocks:
        return []
    return [{"id": f"{prefix}_reused", "type": "section", "props": {"title": "复用的口径"},
             "children": [{"id": f"{prefix}_reused_cols", "type": "columns", "props": {"count": min(len(blocks), 4)},
                           "children": blocks}]}]


def latest_view(ctx: dict[str, Any]) -> dict[str, Any]:
    """看板首页的视图：最近封存的正式运行，有定时任务就只看它出的数（派生规则同 apps/derive.py）。"""
    from app.apps.derive import PIN

    schedule = ctx.get("schedule_id")
    return {"source": {"kind": "latest_sealed", "pin": PIN, "from": f"schedule:{schedule}" if schedule else "formal"}}


def entry_and_pages(spec: dict[str, Any], bp: dict[str, Any], graph: dict[str, Any], *, label: str,
                    writes: dict[str, str] | None = None, home_extra: list[dict[str, Any]] | None = None,
                    entry_map: dict[str, str] | None = None) -> None:
    """流程类应用（同 apps/derive.py 的流程规则）：入口表单就是「填什么」，首页放发起按钮、我发起的、派给我的待办，
    每件事的结果在它自己的实例页看。entry_map 另加的入口映射（常量写 'x'）。"""
    from app.apps.derive import ENTRY, INSTANCE_VIEW, PIN, _entries

    spec["forms"][ENTRY] = entry_form(bp)
    names = list(spec["forms"][ENTRY]["properties"])
    first = next((n for n in names if n in spec["forms"][ENTRY]["required"]), names[0] if names else None)
    spec["entries"].append({
        "id": ENTRY, "label": label, "pin": PIN, "form": {"$ref": f"#/forms/{ENTRY}"},
        "map": {**{n: f"form.{n}" for n in names}, **(entry_map or {})},
        **({"instance_title": {"pattern": f"{spec['name']}·{{{first}}}", "fields": [first]}}
           if first and re.fullmatch(r"[A-Za-z0-9_㐀-鿿]+", first) else {}),
    })
    spec["views"][INSTANCE_VIEW] = {"source": {"kind": "instance"}}
    spec["pages"] = [
        {"id": "home", "title": spec["name"], "body": [
            *(home_extra or []), *_entries(spec),
            {"id": "my_tasks", "type": "task_list", "props": {"scope": "mine"}}]},
        instance_page(graph, writes),
    ]


def finish_app(spec: dict[str, Any]) -> dict[str, Any]:
    """形状对不上是模板的错，不是用户的：这里直接报出来（同 apps/derive.py）。"""
    from app.apps.spec import parse_spec

    return parse_spec(spec).model_dump(mode="json", by_alias=True)


# ==========================================================================
# 示例蓝图的写法、预置、模板自己认的问题（各模板写进自己的 TemplateDef）
# ==========================================================================


def user_item(v: Any) -> dict[str, Any]:
    """用户填过、确认过的一项（示例蓝图用）；没填的记「缺」。"""
    return {"value": v, "source": "user", "status": "confirmed" if v is not None else "missing", "why": None}


def template_item(v: Any) -> dict[str, Any]:
    """模板默认的一项：来源记「模板默认」，状态「待确认」（发布前要人看过，P2）。"""
    return {"value": v, "source": "template", "status": "proposed" if v is not None else "missing", "why": None}


def template_problems(template_id: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """模板自己认的问题（TemplateDef.problems）：卡片内容超出模板能力（只能从一个库查、复用的口径卡接不上……）。"""
    template = get_template(template_id)
    if template is None or template.problems is None:
        return []
    return template.problems(spec)


def mismatch(path: str, message: str) -> dict[str, Any]:
    return {"code": "bp.template_mismatch", "level": "error", "path": path, "message": message}


def prefill(template_id: str, spec: dict[str, Any]) -> None:
    """选模板时按模板预置还没填的卡片（TemplateDef.prefill，原地改）。已经写了的不动。"""
    template = get_template(template_id)
    if template is not None and template.prefill is not None:
        template.prefill(spec)


def default_cadence(preset: str = "weekly") -> dict[str, Any]:
    out: dict[str, Any] = {"preset": preset, "time": "09:00", "tz": "Asia/Shanghai", "period": DEFAULT_PERIOD[preset]}
    if preset == "weekly":
        out["weekday"] = 1
    if preset == "monthly":
        out["day"] = 1
    return out


def fill_cadence(spec: dict[str, Any], preset: str = "weekly") -> None:
    if value(spec.get("cadence")) is None:
        spec["cadence"] = template_item(default_cadence(preset))


def step(step_id: str, label: str, *, policy: str = "any", due: str | None = "P2D") -> dict[str, Any]:
    """预置的一步：处理人留空（要人填），几个人同意、时限按模板默认。"""
    return {"id": step_id, "label": template_item(label), "who": template_item(None),
            "policy": template_item({"kind": policy}), "due": template_item(due), "on_timeout": template_item(None),
            "threshold": template_item(None)}
