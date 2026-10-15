"""流程相关的检查（应用平台 §3.13）：人工节点的新配置、按流程使用时会出事的接法。

两类问题分两处报，是因为门禁的 flag 只能在受管级别出错误：
- human.*：配置本身不成立（时限写不成时长、会签用在补填模式、升级没写岗位、表单不合目录）。运行时没法照它执行，
  所以由 schema.validate_graph 报错误（human_issues），各级发布、发起运行都挡；publish_issues 本来就把 validate_graph
  并进来，这里不再登记一遍，免得同一处问题说两遍。
- process.*：图本身能运行，按流程使用（应用钉住它、实例一等几天、中途升级）才出事。登记进 governance.EXTRA_LINTS，
  发布工作流时一律警告；应用把它作为流程钉住时，应用检查（app/apps/lint.py）把它们升成 app.process_unsafe 报错。

子工作流里有没有人工节点要读库才知道，门禁本身不查库：调用方先用 load_subgraphs 读好、with_subgraphs 放进来，
和 SQL 检查器、模型接入表同一个做法。没放进来时这一条不查。

子工作流固定的版本是不是「已发布且未退役」（D-q）也要读库，随 load_subgraphs 一起读好（Subgraphs.pin_problems），由
lint_subgraph_pins 报：这一条各级都是错误——正式运行发起时按同一条规则拒（engine/launch.py），发布时放过去只会让之后的
每一次正式运行都在发起时才失败。
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

from app.engine.schema import GraphNode, GraphSpec, NodeType

#: ISO 8601 时长里认的部分：周、天、时、分、秒。不认年、月——「一个月」有几天说不准，时限按它算会差出几天
_DURATION = re.compile(
    r"^P(?!$)(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?(?:T(?=\d)(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$")

#: 人工节点 policy 的写法：any / all / {quorum: m}
POLICIES = ("any", "all")
#: on_timeout 的动作。没有「自动批准」
TIMEOUT_ACTIONS = ("remind", "escalate", "reject")
#: assignee 的种类
ASSIGNEE_KINDS = ("slot", "initiator", "expression", "hook")

_MODE_LABEL = {"approve": "审批", "input": "补充输入", "edit": "编辑稿件"}


def parse_duration(text: Any) -> timedelta | None:
    """ISO 8601 时长（PT4H、P2D、P1DT30M）→ timedelta；不认得、或者是零的返回 None。"""
    if not isinstance(text, str):
        return None
    m = _DURATION.match(text.strip().upper())
    if not m:
        return None
    parts = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    delta = timedelta(weeks=parts["w"], days=parts["d"], hours=parts["h"], minutes=parts["m"], seconds=parts["s"])
    return delta if delta.total_seconds() > 0 else None


def normalize_policy(value: Any) -> dict[str, Any] | None:
    """节点配置里的 policy → 待办上存的形状 {"kind": "any" | "all"} / {"kind": "quorum", "m": m}；写法不对返回 None。"""
    if value in (None, ""):
        return {"kind": "any"}
    if isinstance(value, str) and value in POLICIES:
        return {"kind": value}
    if isinstance(value, dict):
        if value.get("kind") in POLICIES and set(value) <= {"kind"}:
            return {"kind": value["kind"]}
        m = value.get("quorum", value.get("m") if value.get("kind") == "quorum" else None)
        if isinstance(m, int) and not isinstance(m, bool) and m >= 1:
            return {"kind": "quorum", "m": m}
    return None


def assignee_problem(value: Any) -> str | None:
    """assignee 的写法问题，没问题返回 None。人工节点和写入动作的调用工具节点共用。"""
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict) or value.get("kind") not in ASSIGNEE_KINDS:
        return "「派给谁」只能是岗位、发起人、表达式或外部系统"
    kind = value["kind"]
    if kind == "slot" and not str(value.get("slot") or "").strip():
        return "「派给谁」选了岗位，还没有填写岗位名"
    if kind == "expression" and not str(value.get("expr") or "").strip():
        return "「派给谁」选了表达式，还没有填写表达式"
    if kind == "hook" and not str(value.get("correlation") or "").strip():
        return "「派给谁」选了外部系统，还没有填写关联键模板"
    return None


def human_issues(node: GraphNode) -> list[tuple[str, str, str]]:
    """人工节点新配置的问题：[(编号, 说明, 字段)]。schema.validate_graph 按错误报。

    form 合不合目录用应用包的 validate_form_schema（表单只有一种说法，P5）；只有写了 form 的节点才去问它。"""
    cfg = node.config
    out: list[tuple[str, str, str]] = []
    mode = cfg.get("mode") or "approve"

    if problem := assignee_problem(cfg.get("assignee")):
        out.append(("human.assignee_invalid", problem, "assignee"))
    policy = normalize_policy(cfg.get("policy"))
    if policy is None:
        out.append(("human.policy_invalid", "「会签方式」只能是任一人、全体同意或其中若干人同意", "policy"))
    elif policy["kind"] != "any" and mode != "approve":
        out.append(("human.policy_mode",
                    f"「会签方式」的全体同意、若干人同意只用于审批模式；当前是{_MODE_LABEL.get(mode, mode)}模式，"
                    "请改为任一人处理", "policy"))
    due = cfg.get("due")
    if due not in (None, "") and parse_duration(due) is None:
        out.append(("human.due_invalid", f"「时限」需要写成 ISO 8601 时长，例如 PT4H（4 小时）、P2D（2 天），当前为「{due}」",
                    "due"))
    timeout = cfg.get("on_timeout")
    if timeout not in (None, "", {}):
        action = timeout.get("action") if isinstance(timeout, dict) else None
        if action not in TIMEOUT_ACTIONS:
            out.append(("human.timeout_invalid", "「超时处理」只能是提醒、升级或驳回", "on_timeout"))
        elif action == "escalate" and not str(timeout.get("to_slot") or "").strip():
            out.append(("human.escalate_slot", "「超时处理」选了升级，还没有填写升级给哪个岗位", "on_timeout.to_slot"))
        elif action == "remind" and timeout.get("every") not in (None, "") and parse_duration(timeout["every"]) is None:
            out.append(("human.due_invalid", f"提醒间隔需要写成 ISO 8601 时长，例如 PT1H，当前为「{timeout['every']}」",
                        "on_timeout.every"))
        if due in (None, ""):
            out.append(("human.due_invalid", "设置了「超时处理」，还需要填写「时限」", "due"))
    form = cfg.get("form")
    if form not in (None, "", {}):
        if not isinstance(form, dict):
            out.append(("human.form_invalid", "「表单」必须是一个 JSON 对象", "form"))
        else:
            from app.apps.forms import validate_form_schema

            if problems := validate_form_schema(form):
                out.append(("human.form_invalid", f"「表单」不符合组件目录：{problems[0]}", "form"))
    source = cfg.get("form_from")
    if source not in (None, "") and not (isinstance(source, str) and re.fullmatch(r"(nodes|vars)\.[\w.\-]+", source.strip())):
        out.append(("human.form_from_invalid", "「表单来自」要写成 nodes.某节点.字段 或 vars.变量名", "form_from"))
    return out


# --------------------------------------------------------------------------
# process.*：登记进发布门禁
# --------------------------------------------------------------------------

#: 子工作流的图：(工作流 id, 固定的版本，没固定为 None) → GraphSpec。见 load_subgraphs
_SUBGRAPHS: ContextVar[Mapping[tuple[str, int | None], GraphSpec] | None] = ContextVar("process_subgraphs", default=None)


class Subgraphs(dict):
    """load_subgraphs 读好的东西：(工作流 id, 固定的版本，没固定为 None) → GraphSpec；pin_problems 另记固定的版本不是
    「已发布且未退役」的子工作流节点（apps/versions.subgraph_pin_problems）。with_subgraphs 两样一起放进门禁。"""

    def __init__(self) -> None:
        super().__init__()
        self.pin_problems: list[Any] = []


async def load_subgraphs(session: Any, spec: GraphSpec) -> Subgraphs:
    """读出图里子工作流节点引用的图（固定了版本的读那一版，没固定的读工作流现在的图）。读不到、不成形的跳过。
    固定的版本能不能用也一并读好（Subgraphs.pin_problems）。"""
    from sqlalchemy import select

    from app.apps.versions import subgraph_pin_problems
    from app.db.models import Workflow, WorkflowVersion

    out = Subgraphs()
    out.pin_problems = await subgraph_pin_problems(session, spec)
    for node in spec.nodes:
        if node.type != NodeType.SUBGRAPH:
            continue
        wid, pinned = node.config.get("workflow_id"), node.config.get("workflow_version")
        version = int(pinned) if str(pinned or "").isdigit() else None
        if not wid or (str(wid), version) in out:
            continue
        query = (select(WorkflowVersion.graph).where(WorkflowVersion.workflow_id == str(wid),
                                                     WorkflowVersion.version == version)
                 if version is not None else select(Workflow.graph).where(Workflow.id == str(wid)))
        graph = (await session.execute(query)).scalar_one_or_none()
        try:
            out[(str(wid), version)] = GraphSpec.model_validate(graph)
        except Exception:  # noqa: BLE001 - 读不到、不成形：子工作流节点运行时自己会说清楚
            continue
    return out


@contextlib.contextmanager
def with_subgraphs(graphs: Mapping[tuple[str, int | None], GraphSpec]) -> Iterator[None]:
    token = _SUBGRAPHS.set(graphs)
    try:
        yield
    finally:
        _SUBGRAPHS.reset(token)


def _who(node: GraphNode) -> str:
    from app.engine.labels import type_label

    return f"「{node.title}」（{type_label(node.type)}）"


def _approval_of(spec: GraphSpec, cfg: Mapping[str, Any]) -> str | None:
    return cfg.get("approval") or spec.defaults.get("approval")


def _may_ask(name: str) -> bool:
    """这个工具在正式运行的信任快照下可能要人审批：MCP、自定义工具一律要（tools/trust.py 的 FORMAL_MARK），
    内置工具看它是不是危险的。数据源查询看这一次的 SQL，静态说不准，不报。"""
    from app.tools.datasource import QUERY_PREFIX, SCHEMA_PREFIX
    from app.tools.registry import get_spec

    if name.startswith((QUERY_PREFIX, SCHEMA_PREFIX)) or "{{" in name:
        return False
    spec = get_spec(name)
    return spec is None or bool(spec.dangerous)


def _agent_tool_sets(node: GraphNode) -> list[tuple[str, Mapping[str, Any], list[str]]]:
    """(给人看的位置, 审批配置所在的 dict, 工具名)：Agent 自己，加上协作节点的各个成员。"""
    cfg = node.config
    out: list[tuple[str, Mapping[str, Any], list[str]]] = []
    if node.type == NodeType.AGENT:
        out.append(("", cfg, [t for t in cfg.get("tools") or [] if isinstance(t, str)]))
    for member in cfg.get("agents") or []:
        if isinstance(member, dict):
            label = f"成员「{member.get('name') or '?'}」"
            # 成员没写审批策略的跟随节点
            approval_cfg = member if member.get("approval") else cfg
            out.append((label, approval_cfg, [t for t in member.get("tools") or [] if isinstance(t, str)]))
    return out


def _subgraph_humans(spec: GraphSpec, node: GraphNode) -> list[str] | None:
    graphs = _SUBGRAPHS.get()
    if graphs is None:
        return None
    wid, pinned = node.config.get("workflow_id"), node.config.get("workflow_version")
    version = int(pinned) if str(pinned or "").isdigit() else None
    sub = graphs.get((str(wid or ""), version))
    if sub is None:
        return None
    return [n.title for n in sub.nodes if n.type == NodeType.HUMAN]


def lint_process(spec: GraphSpec, flag: Callable[..., None], *, level: str, strict: bool) -> None:
    """process.* 一律警告（hard=False）：发布工作流时只提醒，应用按流程钉住时由应用检查升成错误。"""
    for node in spec.nodes:
        cfg = node.config
        who = _who(node)
        if node.type in (NodeType.AGENT, NodeType.SUPERVISOR):
            for label, approval_cfg, tools in _agent_tool_sets(node):
                if _approval_of(spec, approval_cfg) == "never":
                    continue
                asking = [t for t in tools if _may_ask(t)]
                if asking:
                    flag(f"{who}{label}的工具「{'、'.join(asking[:3])}」在正式运行中可能需要人工审批，运行会停在 Agent 中途；"
                         "按流程使用时，系统升级后这种断点无法恢复。请把需要审批的调用移到「调用工具」节点，"
                         "或在能力登记里把只读的工具设为免审批",
                         code="process.wait_mid_agent", node_id=node.id, field="tools")
                    break
        elif node.type == NodeType.SUBGRAPH:
            humans = _subgraph_humans(spec, node)
            if humans:
                flag(f"{who}引用的工作流里有人工节点「{'、'.join(humans[:3])}」：子工作流不保存断点，"
                     "恢复时整个子工作流从头重新执行。按流程使用时请把人工节点放到外层工作流",
                     code="process.human_in_subgraph", node_id=node.id, field="workflow_id")
        elif node.type == NodeType.CODE and cfg.get("network"):
            flag(f"{who}开启了「允许联网」：按流程使用时，对外部系统的写入请放到「调用工具」节点，"
                 "那里先留生效记录再发送，结果未知时可以核实",
                 code="process.code_network", node_id=node.id, field="network")


def lint_subgraph_pins(spec: GraphSpec, flag: Callable[..., None], *, level: str, strict: bool) -> None:
    """子工作流固定的版本不是「已发布且未退役」的（D-q）：各级都报错（always），机器码和发起时拒绝的一样。
    load_subgraphs 没读好、没放进来时不查（门禁本身不查库）。"""
    from app.apps.versions import pinned_upstream

    graphs = _SUBGRAPHS.get()
    nodes = spec.node_map()
    for problem in getattr(graphs, "pin_problems", None) or []:
        node = nodes.get(problem.node_id)
        # 读好的结果对的是读的那张图；拿去查改过的图（自动修复的预览）时，节点改了固定的版本就不再算它
        if node is None or pinned_upstream(node) is None \
                or pinned_upstream(node)[1:] != (problem.workflow_id, problem.version):
            continue
        flag(f"{problem.who}固定的{problem.reason}：正式运行发起时会被拒绝。{problem.advice}",
             code=problem.code, node_id=problem.node_id, field=problem.field, always=True)


def _register() -> None:
    from app.engine.governance import EXTRA_LINTS

    for check in (lint_process, lint_subgraph_pins):
        if check not in EXTRA_LINTS:
            EXTRA_LINTS.append(check)


_register()
