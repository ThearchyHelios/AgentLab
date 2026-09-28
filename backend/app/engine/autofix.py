"""发布前自动修复。

发布门禁拦下的问题，以前只能自己动手、或者打开 Copilot 描述一遍。这里把「答案唯一、不降低
要求」的那些做成确定性的修复，一律先给预览，人确认后前端走现有的保存接口：

- auto：答案唯一。出口上游恰好一张口径卡、审批从「全部自动放行」改成「仅危险工具需要审批」…
- choice：要人拿主意。required 选哪几个指标、多张口径卡里选哪几张——列候选，不替人选，
  也不给「默认全选」
- assist：结构性的（协作团队节点、多个出口…），交给 Copilot，产出同样只是预览

任何修复都不能降低要求（forbidden_changes）；应用后重跑 validate 和门禁，错误必须变少、
不能冒出新的错误（judge），做不到的修复丢弃并写明原因。

本模块是纯函数：库里的东西（子工作流当前的发布版本、钉在别处的口径卡有哪些指标）由调用方
查好传进来。改图直接改原始的图字典，只动修复涉及的那几个字段：坐标、连线 id、视口这些
前端的东西原样留着，存回去不会丢。
"""
from __future__ import annotations

import copy
from typing import Any, Callable

from pydantic import BaseModel

from app.engine.governance import publish_issues
from app.engine.schema import GraphNode, GraphSpec, NodeType, _ancestors

#: 被选中的修复按这个顺序应用：先补契约骨架，再补契约里的各项，最后是节点上的开关。
#: 前面的修复可能改变后面的候选（metrics_from 决定 required 从哪几张卡里挑）
_ORDER = (
    "governed.no_contract", "contract.metrics_from_missing", "contract.metrics_from_invalid",
    "contract.report_from_missing", "contract.report_from_invalid", "report.metrics_from_invalid",
    "contract.required_missing", "contract.strict_off", "governed.agent_approval_never",
    "governed.default_approval_never", "governed.subgraph_unpinned", "governed.supervisor", "contract.not_object",
)

#: 修复落在全图上、不落在任何节点上的问题：问题各挂在跟随全图默认的那几个节点上（点一条能定位），
#: 修复只有一条（id 是 code:graph），改的是图级 defaults，一改几个节点一起好
_GRAPH_FIXES = frozenset({"governed.default_approval_never"})

#: 除了 error，这几条警告也给修复（受管档位下）。已发布档位的门禁只给警告，不借「一键修复」
#: 替人把审批、strict 这些往受管的要求上拧
_WARNING_FIXES = {"governed": frozenset({"contract.strict_off"})}

ASSIST_ONLY = "这一处是结构性的问题，要交给 Copilot：请求里带上 assist: true"
STALE = "这处问题现在已经没有了（修复 id 过期），重新检查一次"
SOLVED = "前面的修复已经一并解决了这一处"
NEED_CHOICE = "这一处要你来选：{label}"

_TEXT_NODES = (NodeType.LLM, NodeType.AGENT)
#: 修复和 Copilot 兜底只许改节点配置、补节点和连线。其余的图操作（删节点、删连线）和
#: 任何不认识的操作（发布、改级别…）一律当降低要求处理
_GRAPH_OPS = frozenset({"add_node", "update_node", "add_edge"})


# --------------------------------------------------------------------------
# 给出修复
# --------------------------------------------------------------------------


class _Ctx:
    def __init__(self, spec: GraphSpec, versions: dict[str, Any] | None, cards: dict[str, Any] | None) -> None:
        self.spec = spec
        self.nodes = spec.node_map()
        self.versions = versions or {}
        self.cards = cards or {}

    def upstream(self, node_id: str, *types: NodeType) -> list[GraphNode]:
        """这个节点上游的某几类节点，按画布上的节点顺序。"""
        above = _ancestors(self.spec, node_id)
        return [n for n in self.spec.nodes if n.id in above and n.type in types]

    def metrics_of(self, card: GraphNode) -> list[dict[str, Any]]:
        """一张口径卡定义的指标。钉在别处（caliber_from）的卡本地没写，用调用方从那一版里查出来的。"""
        local = [d for d in card.config.get("metrics") or [] if isinstance(d, dict) and d.get("id")]
        if local and not isinstance(card.config.get("caliber_from"), dict):
            return local
        return [d for d in self.cards.get(card.id) or [] if isinstance(d, dict) and d.get("id")]


def _title(node: GraphNode | None) -> str:
    return node.title if node is not None else ""


def _auto(label: str, field: str, before: Any, after: Any) -> dict[str, Any]:
    return {"kind": "auto", "label": label, "field": field,
            "preview": {"field": field, "before": copy.deepcopy(before), "after": copy.deepcopy(after)},
            "_set": {field: after}}


def _choice(label: str, field: str, options: list[dict[str, Any]], *, multiple: bool = False,
            default: Any = None, **extra: Any) -> dict[str, Any]:
    out = {"kind": "choice", "label": label, "field": field, "options": options, "multiple": multiple, **extra}
    if default is not None:
        out["default"] = default
    return out


def _assist(label: str, field: str | None = None) -> dict[str, Any]:
    # 界面上这类修复的按钮就叫「交给 Copilot」，label 只写要它做什么、或者为什么确定性修复做不了
    return {"kind": "assist", "label": label, "field": field}


def _node_options(nodes: list[GraphNode]) -> list[dict[str, Any]]:
    from app.engine.schema import type_label

    return [{"value": n.id, "label": f"「{n.title}」", "hint": type_label(n.type)} for n in nodes]


def _contract(node: GraphNode) -> dict[str, Any]:
    contract = node.config.get("contract")
    return contract if isinstance(contract, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return [value] if isinstance(value, str) else list(value) if isinstance(value, list) else [value]


def _cards_choice(ctx: _Ctx, node: GraphNode, before: Any, field: str) -> dict[str, Any]:
    cards = ctx.upstream(node.id, NodeType.METRICS)
    if len(cards) == 1:
        return _auto(f"出具契约的指标来自上游唯一的口径卡「{cards[0].title}」（metrics_from）", field, before,
                     [cards[0].id])
    if cards:
        return _choice("选出具契约的指标来自哪几张口径卡（metrics_from）", field, _node_options(cards),
                       multiple=True)
    return _assist("出口上游没有口径卡，出具契约的 metrics_from 无处可指", field)


def _plan_metrics_from_missing(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return _cards_choice(ctx, node, _contract(node).get("metrics_from"), "contract.metrics_from")


def _plan_metrics_from_invalid(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    before = _contract(node).get("metrics_from")
    keep = [s for s in _as_list(before) if isinstance(s, str) and getattr(ctx.nodes.get(s), "type", None)
            == NodeType.METRICS]
    if keep:
        bad = [str(s) for s in _as_list(before) if s not in keep and str(s).strip()]
        return _auto(f"出具契约的 metrics_from 去掉不存在的 {'、'.join(bad) or '空项'}", "contract.metrics_from",
                     before, keep)
    return _cards_choice(ctx, node, before, "contract.metrics_from")


def _reports_choice(ctx: _Ctx, node: GraphNode, before: Any, *, narrative: bool) -> dict[str, Any]:
    field = "contract.report_from"
    reports = ctx.upstream(node.id, NodeType.REPORT)
    if len(reports) == 1:
        return _auto(f"出具契约核对上游唯一的「报告撰写」节点「{reports[0].title}」的文档（report_from）", field,
                     before, reports[0].id)
    if reports:
        return _choice("选出具契约核对哪个「报告撰写」节点的文档（report_from）", field, _node_options(reports))
    writers = ctx.upstream(node.id, *_TEXT_NODES) if narrative else []
    if writers:
        # 没有报告撰写节点时退回旧的叙述模式：数字按叙述回指口径卡。只有一个候选也要人确认——
        # 它是不是那段该核对的叙述，只有作者知道
        options = [{"value": f"{{{{ nodes.{n.id}.text }}}}", "label": f"「{n.title}」写的文字",
                    "hint": f"narrative = {{{{ nodes.{n.id}.text }}}}"} for n in writers]
        return _choice("选出具契约要核对的叙述来自哪个节点（narrative）", "contract.narrative", options,
                       default=options[0]["value"] if len(options) == 1 else None)
    return _assist("出口上游没有「报告撰写」节点，也没有写叙述的模型节点", field)


def _plan_report_from_missing(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return _reports_choice(ctx, node, _contract(node).get("report_from"), narrative=True)


def _plan_report_from_invalid(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return _reports_choice(ctx, node, _contract(node).get("report_from"), narrative=False)


def _plan_report_metrics_from(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    before = node.config.get("metrics_from")
    above = _ancestors(ctx.spec, node.id)
    keep = [s for s in _as_list(before) if isinstance(s, str) and s in above
            and getattr(ctx.nodes.get(s), "type", None) == NodeType.METRICS]
    cards = ctx.upstream(node.id, NodeType.METRICS)
    added = cards[0] if len(cards) == 1 and cards[0].id not in keep else None
    after = [*keep, *([added.id] if added else [])]
    bad = [str(s) for s in _as_list(before) if s not in keep and str(s).strip()]
    label = f"「{node.title}」的 metrics_from " + (f"去掉无效的 {'、'.join(bad)}" if bad else "改成口径卡节点的 id 列表")
    return _auto(label + (f"，补上上游唯一的口径卡「{added.title}」" if added else ""), "metrics_from", before, after)


def _metric_options(ctx: _Ctx, cards: list[GraphNode]) -> list[dict[str, Any]]:
    options: dict[str, dict[str, Any]] = {}
    for card in cards:
        for d in ctx.metrics_of(card):
            mid = str(d["id"])
            name = str(d.get("name") or mid)
            unit = str(d.get("unit") or "")
            options.setdefault(mid, {"value": mid, "label": name if name == mid else f"{name}（{mid}）",
                                     "hint": f"口径卡「{card.title}」" + (f" · {unit}" if unit else "")})
    return list(options.values())


def _plan_required(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    named = [ctx.nodes[s] for s in _as_list(_contract(node).get("metrics_from"))
             if isinstance(s, str) and getattr(ctx.nodes.get(s), "type", None) == NodeType.METRICS]
    options = _metric_options(ctx, named or ctx.upstream(node.id, NodeType.METRICS))
    if not options:
        return _assist("列不出口径卡里的指标，没法给出 required 的候选", "contract.required")
    # 不给默认：哪些指标缺了就不予出具，是作者的决定
    return _choice("选受管出具必需的指标（required）：缺了其中任何一个就不予出具", "contract.required", options,
                   multiple=True)


def _plan_strict(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return _auto("把出具契约设为 strict：没有出处的数字直接拦下，而不是只降档", "contract.strict",
                 _contract(node).get("strict"), True)


def _plan_approval(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return _auto(f"把「{node.title}」的审批策略改成「仅危险工具需要审批」", "approval", node.config.get("approval"),
                 "dangerous")


def _plan_default_approval(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    followers = [n.title for n in ctx.spec.nodes if n.type == NodeType.AGENT and n.config.get("tools")
                 and n.config.get("approval") in (None, "")]
    who = "、".join(f"「{t}」" for t in followers[:3]) + (f" 等 {len(followers)} 个节点" if len(followers) > 3 else "")
    return _auto(f"把全图默认的审批策略改成「仅危险工具需要审批」（{who or '跟随它的节点'}一并生效）",
                 "defaults.approval", ctx.spec.defaults.get("approval"), "dangerous")


def _plan_subgraph(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    wf = node.config.get("workflow_id")
    version = ctx.versions.get(wf) if wf else None
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        return _auto(f"把「{node.title}」钉到上游当前的发布版本 v{version}", "workflow_version",
                     node.config.get("workflow_version"), version)
    # 上游没发布过：不替人挑一个草稿版本钉上去
    return _assist(f"「{node.title}」嵌套的工作流还没有发布版本，没法替你钉：先发布它，或者请 Copilot 看看",
                   "workflow_version")


def _plan_no_contract(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return _assist("出口不止一个（或者没有出口），该给哪个出口写契约说不准")
    cards = ctx.upstream(node.id, NodeType.METRICS)
    reports = ctx.upstream(node.id, NodeType.REPORT)
    if len(cards) != 1 or len(reports) != 1:
        return _assist(f"「{node.title}」上游的口径卡、报告撰写节点不是恰好各一个，契约该指向哪个说不准", "contract")
    options = _metric_options(ctx, cards)
    if not options:
        return _assist(f"列不出口径卡「{cards[0].title}」里的指标，没法给出 required 的候选", "contract")
    return _choice(f"给「{node.title}」生成出具契约：核对「{reports[0].title}」的文档，指标来自「{cards[0].title}」，"
                   "再选出必需的指标（required）", "contract", options, multiple=True,
                   _skeleton={"report_from": reports[0].id, "metrics_from": [cards[0].id], "strict": True})


def _plan_supervisor(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    # 修复不许删节点、不许覆盖或换类型，Copilot 替换不了它，只能拟一个拆分方案交给人
    return _assist(f"「{_title(node)}」要换成固定编排的 Agent 或「模型调用」节点：修复不能删节点，"
                   "Copilot 只会给出拆分建议，替换要你来做")


def _plan_not_object(ctx: _Ctx, issue: dict[str, Any], node: GraphNode | None) -> dict[str, Any] | None:
    return _assist(f"「{_title(node)}」的出具契约不是一个 JSON 对象", "contract")


_PLANNERS: dict[str, Callable[[_Ctx, dict[str, Any], GraphNode | None], dict[str, Any] | None]] = {
    "governed.agent_approval_never": _plan_approval,
    "governed.default_approval_never": _plan_default_approval,
    "governed.subgraph_unpinned": _plan_subgraph,
    "governed.supervisor": _plan_supervisor,
    "governed.no_contract": _plan_no_contract,
    "contract.not_object": _plan_not_object,
    "contract.metrics_from_missing": _plan_metrics_from_missing,
    "contract.metrics_from_invalid": _plan_metrics_from_invalid,
    "contract.report_from_missing": _plan_report_from_missing,
    "contract.report_from_invalid": _plan_report_from_invalid,
    "contract.required_missing": _plan_required,
    "contract.strict_off": _plan_strict,
    "report.metrics_from_invalid": _plan_report_metrics_from,
}


def _as_dict(issue: Any) -> dict[str, Any]:
    return issue.model_dump() if isinstance(issue, BaseModel) else dict(issue)


def _target(ctx: _Ctx, issue: dict[str, Any]) -> str | None:
    """修复落在哪个节点上。图级的「没有带契约的出口」落在唯一的那个出口上；改全图默认的不落在节点上。"""
    if issue.get("code") in _GRAPH_FIXES:
        return None
    if issue.get("node_id"):
        return str(issue["node_id"])
    if issue.get("code") == "governed.no_contract":
        outputs = [n for n in ctx.spec.nodes if n.type == NodeType.OUTPUT]
        return outputs[0].id if len(outputs) == 1 else None
    return None


def plan_fixes(spec: GraphSpec, issues: list[Any], *, level: str, versions: dict[str, Any] | None = None,
               cards: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """这批问题能怎么修：Fix 列表，按问题出现的顺序，同一处只出一条。

    versions：子工作流 id → 它当前的发布版本号（没发布过为 None）。
    cards：钉在别处（caliber_from）的口径卡节点 id → 那一版里的指标定义，用来列 required 的候选。
    返回的 Fix 里以 _ 开头的键是应用时要用的内部信息，交给前端前用 public() 去掉。
    """
    ctx = _Ctx(spec, versions, cards)
    warned = _WARNING_FIXES.get(level, frozenset())
    out: dict[str, dict[str, Any]] = {}
    for issue in map(_as_dict, issues):
        code = issue.get("code")
        planner = _PLANNERS.get(code or "")
        if planner is None or (issue.get("level") != "error" and code not in warned):
            continue
        target = _target(ctx, issue)
        fid = f"{code}:{target or 'graph'}"
        if fid in out:
            continue
        fix = planner(ctx, issue, ctx.nodes.get(target) if target else None)
        if fix:
            out[fid] = {"id": fid, "code": code, "node_id": target, **fix}
    return list(out.values())


def public(fix: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fix.items() if not k.startswith("_")}


def annotate(issues: list[Any], fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """问题的字典形状，带上对应的修复 id（fix）。validate 和门禁报的同一处问题指向同一个修复。

    先按（编号、节点）认；认不出时，图级问题（没有节点）认同一编号的第一条修复，图级修复（不落在
    节点上，比如改全图默认）认同一编号的每一条问题。前端 canvas/issues.fixFor 是同一个退路。
    """
    by_node = {(f["code"], f["node_id"]): f["id"] for f in fixes}
    by_code: dict[str, str] = {}
    by_graph: dict[str, str] = {}
    for f in fixes:
        by_code.setdefault(f["code"], f["id"])
        if f["node_id"] is None:
            by_graph.setdefault(f["code"], f["id"])
    out = []
    for issue in map(_as_dict, issues):
        code = issue.get("code")
        fid = by_node.get((code, issue.get("node_id"))) if code else None
        if fid is None and code:
            fid = by_code.get(code) if issue.get("node_id") is None else by_graph.get(code)
        out.append({**issue, "fix": fid})
    return out


def check(spec: GraphSpec, *, level: str, versions: dict[str, Any] | None = None,
          cards: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """发布前检查：和真正发布同一套口径的问题，以及能怎么修。只读。"""
    issues = publish_issues(spec, level=level)
    fixes = plan_fixes(spec, issues, level=level, versions=versions, cards=cards)
    return {"level": level, "ok": not any(i.level == "error" for i in issues),
            "issues": annotate(issues, fixes), "fixes": [public(f) for f in fixes]}


# --------------------------------------------------------------------------
# 应用修复
# --------------------------------------------------------------------------


def _node(graph: dict[str, Any], node_id: Any) -> dict[str, Any] | None:
    return next((n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id") == node_id), None)


def _config(node: dict[str, Any]) -> dict[str, Any]:
    data = node.setdefault("data", {})
    if not isinstance(data.get("config"), dict):
        data["config"] = {}
    return data["config"]


def _write(config: dict[str, Any], path: str, value: Any) -> Any:
    """按 a.b 写进配置，返回原值。中间缺的层补成对象。"""
    *heads, last = path.split(".")
    for key in heads:
        if not isinstance(config.get(key), dict):
            config[key] = {}
        config = config[key]
    before = copy.deepcopy(config.get(last))
    config[last] = copy.deepcopy(value)
    return before


def _edit(graph: dict[str, Any], fix: dict[str, Any], value: Any) -> list[tuple[str, str, Any, Any]]:
    """把一个修复写进图（原地改）。返回 [(节点 id, 字段, 原值, 新值)]。"""
    node = _node(graph, fix["node_id"])
    if node is None:
        return []
    if fix["kind"] == "auto":
        writes = dict(fix.get("_set") or {})
    elif "_skeleton" in fix:
        writes = {"contract": {**fix["_skeleton"], "required": value}}
    else:
        writes = {fix["field"]: value}
    config = _config(node)
    return [(str(node["id"]), path, _write(config, path, after), copy.deepcopy(after))
            for path, after in writes.items()]


def _edit_defaults(graph: dict[str, Any], fix: dict[str, Any], value: Any) -> list[tuple[str | None, str, Any, Any]]:
    """改全图默认（graph.defaults）：不落在任何节点上，节点 id 记 None，字段写全路径 defaults.x。"""
    if not isinstance(graph.get("defaults"), dict):
        graph["defaults"] = {}
    return [(None, path, _write(graph["defaults"], path.split(".", 1)[1], after), copy.deepcopy(after))
            for path, after in (fix.get("_set") or {}).items() if path.startswith("defaults.")]


#: 每类问题怎么改图。大多是同一种「按节点字段写值」，改全图默认的另走一格；留着这张表是为了哪天
#: 某类修复要特殊处理时只换它自己的那一格（测试里也借它模拟一条出错的修复规则）
_EDITORS: dict[str, Callable[[dict[str, Any], dict[str, Any], Any], list[tuple[str | None, str, Any, Any]]]] = {
    code: (_edit_defaults if code in _GRAPH_FIXES else _edit) for code in _PLANNERS
}


def _chosen(fix: dict[str, Any], choices: dict[str, Any]) -> tuple[Any, str]:
    """choice 类修复里人选的值，按候选校验。返回 (值, 不合格的原因)。"""
    if fix["kind"] != "choice":
        return None, ""
    if fix["id"] not in choices:
        return None, NEED_CHOICE.format(label=fix["label"])
    value = choices[fix["id"]]
    allowed = [o["value"] for o in fix.get("options") or []]
    if fix.get("multiple"):
        if not isinstance(value, list) or not value:
            return None, f"至少选一个：{fix['label']}"
        if any(v not in allowed for v in value):
            return None, f"选的 {'、'.join(str(v) for v in value if v not in allowed)} 不在候选里"
        return [v for v in allowed if v in value], ""
    if value not in allowed:
        return None, f"选的 {value!r} 不在候选里"
    return value, ""


def _op(graph: dict[str, Any], node_id: str | None, edits: list[tuple[str | None, str, Any, Any]]) -> dict[str, Any]:
    """一次修复落成的操作。落在节点上的是 update_node，形状同 Copilot 的操作流：config 只写改过的
    顶层字段（整份新值）；改全图默认的是 update_defaults，defaults 写改过的那几项。"""
    if node_id is None:
        defaults = graph.get("defaults") if isinstance(graph.get("defaults"), dict) else {}
        keys = dict.fromkeys(path.split(".", 1)[1] for _, path, _, _ in edits if path.startswith("defaults."))
        return {"op": "update_defaults", "defaults": {k: copy.deepcopy(defaults.get(k)) for k in keys}}
    config = _config(_node(graph, node_id) or {})
    tops = dict.fromkeys(path.split(".")[0] for _, path, _, _ in edits)
    return {"op": "update_node", "id": node_id, "config": {k: copy.deepcopy(config.get(k)) for k in tops}}


def apply_fixes(graph: dict[str, Any] | GraphSpec, fix_ids: list[str], choices: dict[str, Any] | None = None, *,
                level: str, versions: dict[str, Any] | None = None,
                cards: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """在副本上逐个应用修复，每一个都复核：降低要求、错误没变少或者冒出新错误的，丢弃并写明原因。

    返回 {graph, changes, ops, applied, rejected, remaining, fixes, ok}。graph 只是预览，不落库。
    """
    raw = graph.model_dump(mode="json") if isinstance(graph, GraphSpec) else graph
    current = copy.deepcopy(raw)
    spec = GraphSpec.model_validate(current)
    issues = publish_issues(spec, level=level)
    plan = plan_fixes(spec, issues, level=level, versions=versions, cards=cards)
    known = {f["id"] for f in plan}
    rank = {code: i for i, code in enumerate(_ORDER)}
    wanted = sorted(dict.fromkeys(fix_ids), key=lambda fid: rank.get(fid.split(":")[0], len(rank)))
    applied: list[str] = []
    rejected: list[dict[str, str]] = []
    changes: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []
    for fid in wanted:
        fix = next((f for f in plan if f["id"] == fid), None)
        if fix is None:
            rejected.append({"fix_id": fid, "reason": SOLVED if fid in known else STALE})
            continue
        if fix["kind"] == "assist":
            rejected.append({"fix_id": fid, "reason": ASSIST_ONLY})
            continue
        value, why = _chosen(fix, choices or {})
        if why:
            rejected.append({"fix_id": fid, "reason": why})
            continue
        trial = copy.deepcopy(current)
        edits = _EDITORS.get(fix["code"], _edit)(trial, fix, value)
        try:
            trial_spec = GraphSpec.model_validate(trial)
        except Exception:  # noqa: BLE001 - 修复规则把图改坏了：丢弃，不交给人一张存不回去的图
            rejected.append({"fix_id": fid, "reason": "改完的图结构读不懂，这条修复已丢弃"})
            continue
        trial_issues = publish_issues(trial_spec, level=level)
        why = "；".join(forbidden_changes(current, trial)) or judge(
            issues, trial_issues, target=(fix["code"], fix["node_id"]))
        if why:
            rejected.append({"fix_id": fid, "reason": why})
            continue
        current, spec, issues = trial, trial_spec, trial_issues
        applied.append(fid)
        titles = spec.node_map()
        changes += [{"fix_id": fid, "node_id": nid, "node_title": _title(titles.get(nid)), "field": path,
                     "before": before, "after": after, "label": fix["label"]} for nid, path, before, after in edits]
        if edits:
            ops.append(_op(current, fix["node_id"], edits))
        plan = plan_fixes(spec, issues, level=level, versions=versions, cards=cards)
    return {"graph": current, "changes": changes, "ops": ops, "applied": applied, "rejected": rejected,
            "remaining": annotate(issues, plan), "fixes": [public(f) for f in plan],
            "ok": not any(i.level == "error" for i in issues)}


# --------------------------------------------------------------------------
# 复核：修完必须更好，不许降低要求
# --------------------------------------------------------------------------


def _sig(issue: dict[str, Any]) -> tuple[Any, ...]:
    """同一处问题的认法。有编号的按（级别、编号、节点、连线、字段）认：文案里带节点标题，改个名
    文案就变了，那还是同一处问题，不是冒出来的新问题。没编号的只能连文案一起比。"""
    head = (issue.get("level"), issue.get("code"), issue.get("node_id"), issue.get("edge_id"), issue.get("field"))
    return head if issue.get("code") else (*head, issue.get("message"))


def judge(before: list[Any], after: list[Any], *, target: tuple[str, str | None] | None = None) -> str | None:
    """改完是不是更好了：不能冒出新的 error，error 不能变多；要修的那一处必须没了。

    target 是 (问题编号, 节点 id)：修的是一条警告（strict）时，error 不增加、那条警告消失就算更好；
    修的是 error，或者不指定 target（Copilot 兜底），error 必须变少。返回 None 表示更好，否则是原因。
    """
    b = [_as_dict(i) for i in before]
    a = [_as_dict(i) for i in after]
    b_err = [i for i in b if i.get("level") == "error"]
    a_err = [i for i in a if i.get("level") == "error"]
    seen = {_sig(i) for i in b_err}
    new = [i for i in a_err if _sig(i) not in seen]
    if new:
        return f"改完会冒出新的问题：{new[0].get('message')}"
    if len(a_err) > len(b_err):
        return f"改完错误反而变多了（{len(b_err)} → {len(a_err)}）"
    if target is not None:
        code, node_id = target

        def hit(i: dict[str, Any]) -> bool:
            # 图级修复（node_id 为 None，改全图默认）管的是这个编号下挂在各个节点上的每一条
            return i.get("code") == code and (node_id is None or i.get("node_id") in (node_id, None))

        still = [i for i in a if hit(i)]
        if still:
            return f"改完这处问题还在：{still[0].get('message')}"
        if not any(i.get("level") == "error" for i in b if hit(i)):
            return None
    if len(a_err) >= len(b_err):
        return "改完错误没有变少"
    return None


def _nodes_of(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n.get("id")): n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id")}


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str]:
    handle = edge.get("sourceHandle")
    return (str(edge.get("source")), str(edge.get("target")),
            "" if handle in (None, "out", "source") else str(handle))


def _cfg(node: dict[str, Any]) -> dict[str, Any]:
    config = (node.get("data") or {}).get("config")
    return config if isinstance(config, dict) else {}


def _name(node: dict[str, Any]) -> str:
    return str((node.get("data") or {}).get("label") or node.get("id"))


#: 审批策略从严到宽：每次调用都审批 > 仅危险工具需要审批 > 全部自动放行
_APPROVAL_RANK = {"never": 0, "dangerous": 1, "always": 2}
_APPROVAL_LABEL = {"never": "全部自动放行", "dangerous": "仅危险工具需要审批", "always": "每次调用都审批"}


def _approval(value: Any) -> str | None:
    """审批策略的取值：没写、写 null、写空串都算没写（运行时一样往下回退）。"""
    return None if value in (None, "") else str(value)


def _approval_word(value: str) -> str:
    return f"「{_APPROVAL_LABEL.get(value, value)}」"


def _approval_lowered(who: str, prev: str | None, now: str | None, *, inherited: str | None,
                      fallback: str | None) -> str | None:
    """审批策略有没有往宽里改。返回一句原因，没有就是 None。

    prev / now：改之前、之后写明的值（None 是没写）。没写时运行时往下回退：节点退到全图默认，
    全图默认退到全局设置——全局设置至多是「仅危险工具需要审批」，也可能是「全部自动放行」，
    所以退到全局一律按可能全部放行算。inherited：改之前没写时实际跟随的值（节点跟全图默认；
    全图默认没有可跟的，为 None）。fallback：改之后没写时退到的值（同理）。
    """
    if prev == now:
        return None
    if now is None:
        if prev is None or prev == "never":
            return None
        if fallback in _APPROVAL_RANK and _APPROVAL_RANK[fallback] >= _APPROVAL_RANK.get(prev, 0):
            return None
        return (f"去掉了{who}的审批策略（原来是{_approval_word(prev)}），会退回"
                + (f"全图默认的{_approval_word(fallback)}" if fallback else "全局设置，可能是「全部自动放行」"))
    if now not in _APPROVAL_RANK:
        return f"把{who}的审批策略改成了不认识的值 {now!r}"
    base = prev if prev is not None else inherited
    if base in _APPROVAL_RANK and _APPROVAL_RANK[now] < _APPROVAL_RANK[base]:
        was = _approval_word(base) + ("" if prev is not None else "（跟随全图默认）")
        return f"把{who}的审批策略从{was}放宽成了{_approval_word(now)}"
    return None


def _numbers(value: Any) -> list[str]:
    return [str(v).strip() for v in _as_list(value) if str(v).strip()]


def _contract_loosened(name: str, prior: Any, contract: Any) -> list[str]:
    """出具契约里替作者放宽口子的改动：allow_numbers 多出来的数（白名单里的数不用引用也能过），
    cells 从没开变成开（受管级别要作者显式声明，不由模型替人打开）。原来没有契约也一样查。"""
    if not isinstance(contract, dict):
        return []
    prior = prior if isinstance(prior, dict) else {}
    out = []
    had = set(_numbers(prior.get("allow_numbers")))
    extra = [n for n in dict.fromkeys(_numbers(contract.get("allow_numbers"))) if n not in had]
    if extra:
        out.append(f"往「{name}」出具契约的 allow_numbers 里加了 {'、'.join(extra)}（不带出处也能放行的数）")
    if contract.get("cells") is True and prior.get("cells") is not True:
        out.append(f"替你打开了「{name}」出具契约的 cells（允许报告直接引用查询单元格），这要你自己决定")
    return out


def forbidden_changes(before: dict[str, Any], after: dict[str, Any],
                      ops: list[dict[str, Any]] | None = None) -> list[str]:
    """降低要求的改动，一条一句。空列表表示没有。确定性修复和 Copilot 兜底过的是同一道检查。

    禁止：
    - 删节点、删连线；用 add_node 覆盖已有的节点、把节点换成别的类型（等于删了重建）
    - 删掉或清空出具契约、删掉 required 里的指标、把 strict 改成 false；
      往 allow_numbers 里加数、替人打开 cells
    - 审批策略往宽里改（每次调用都审批 > 仅危险工具需要审批 > 全部自动放行）、删掉写明的审批策略，
      把审批写成（或新加一个）「全部自动放行」；全图默认同理
    - 取消子工作流钉住的版本
    - 改发布级别——级别不在图里，修复接口也从不写工作流的 status，所以只可能以一个不认识的
      操作出现，按不认识的操作拒掉
    """
    old, new = _nodes_of(before), _nodes_of(after)
    reasons: list[str] = []
    for op in ops or []:
        kind = op.get("op")
        if kind == "remove_node":
            reasons.append(f"想删节点「{op.get('id')}」")
        elif kind == "remove_edge":
            reasons.append(f"想删连线 {op.get('source')} → {op.get('target')}")
        elif kind == "add_node":
            # Copilot 的改图流程遇到已有的 id 会拿新节点整个盖掉旧的：配置、位置全丢，类型也可能换了
            added = op.get("node")
            target = added.get("id") if isinstance(added, dict) else None
            if target is not None and str(target) in old:
                reasons.append(f"想用新节点覆盖已有的「{_name(old[str(target)])}」（等于删了重建）")
        elif kind not in _GRAPH_OPS:
            reasons.append(f"用了修复不许用的操作 {kind}（只能改节点配置、补节点和连线）")
    reasons += [f"删了节点「{_name(n)}」" for nid, n in old.items() if nid not in new]
    kept = {_edge_key(e) for e in after.get("edges") or [] if isinstance(e, dict)}
    reasons += [f"删了连线 {s} → {t}" for s, t, _ in
                dict.fromkeys(_edge_key(e) for e in before.get("edges") or [] if isinstance(e, dict))
                if (s, t, _) not in kept]
    old_default = _approval((before.get("defaults") or {}).get("approval"))
    new_default = _approval((after.get("defaults") or {}).get("approval"))
    for nid, node in new.items():
        was = old.get(nid)
        cfg, prev = _cfg(node), _cfg(was) if was else {}
        name = _name(node)
        if was is not None and was.get("type") != node.get("type"):
            from app.engine.schema import type_label

            reasons.append(f"把「{_name(was)}」从{_type_word(was.get('type'), type_label)}换成了"
                           f"{_type_word(node.get('type'), type_label)}（等于删了重建）")
        approval, prior_approval = _approval(cfg.get("approval")), _approval(prev.get("approval"))
        if approval == "never" and prior_approval != "never":
            reasons.append(f"把「{name}」的审批策略改成了「全部自动放行」" if was
                           else f"新加的「{name}」审批策略是「全部自动放行」")
        elif why := _approval_lowered(f"「{name}」", prior_approval, approval, inherited=old_default,
                                      fallback=new_default):
            reasons.append(why)
        contract, prior = cfg.get("contract"), prev.get("contract")
        reasons += _contract_loosened(name, prior, contract)
        if was is None:
            continue
        if prev.get("workflow_version") and not cfg.get("workflow_version"):
            reasons.append(f"取消了「{name}」钉住的版本")
        if not isinstance(prior, dict) or not prior:
            continue
        if not isinstance(contract, dict) or not contract:
            reasons.append(f"删掉或清空了「{name}」的出具契约")
            continue
        if prior.get("strict") and not contract.get("strict"):
            reasons.append(f"把「{name}」出具契约的 strict 改成了 false")
        dropped = [m for m in _as_list(prior.get("required")) if m not in _as_list(contract.get("required"))]
        if dropped:
            reasons.append(f"删掉了「{name}」出具契约 required 里的 {'、'.join(map(str, dropped))}")
    if new_default == "never" and old_default != "never":
        reasons.append("把全图默认的审批策略改成了「全部自动放行」")
    elif why := _approval_lowered("全图默认", old_default, new_default, inherited=None, fallback=None):
        reasons.append(why)
    return reasons


def _type_word(node_type: Any, type_label: Callable[[Any], str]) -> str:
    try:
        return f"「{type_label(NodeType(node_type))}」"
    except ValueError:
        return f"「{node_type}」"


def diff_changes(before: dict[str, Any], after: dict[str, Any], *, fix_id: str, label: str) -> list[dict[str, Any]]:
    """两张图之间给人看的逐项变更（Copilot 兜底用）：节点配置按顶层字段比，新加的节点、连线各一条。"""
    old, new = _nodes_of(before), _nodes_of(after)
    out: list[dict[str, Any]] = []
    for nid, node in new.items():
        was = old.get(nid)
        if was is None:
            out.append({"fix_id": fix_id, "node_id": nid, "node_title": _name(node), "field": None, "before": None,
                        "after": {"type": node.get("type"), "config": copy.deepcopy(_cfg(node))},
                        "label": f"{label}：新加节点"})
            continue
        if was.get("type") != node.get("type"):
            # forbidden_changes 现在就拒掉换类型；万一哪天放行，预览里也得看得出节点换了类型
            out.append({"fix_id": fix_id, "node_id": nid, "node_title": _name(node), "field": "type",
                        "before": was.get("type"), "after": node.get("type"), "label": label})
        if _name(was) != _name(node):
            out.append({"fix_id": fix_id, "node_id": nid, "node_title": _name(node), "field": "label",
                        "before": _name(was), "after": _name(node), "label": label})
        cfg, prev = _cfg(node), _cfg(was)
        for key in dict.fromkeys([*prev, *cfg]):
            if prev.get(key) != cfg.get(key):
                out.append({"fix_id": fix_id, "node_id": nid, "node_title": _name(node), "field": key,
                            "before": copy.deepcopy(prev.get(key)), "after": copy.deepcopy(cfg.get(key)),
                            "label": label})
    known = {_edge_key(e) for e in before.get("edges") or [] if isinstance(e, dict)}
    for edge in after.get("edges") or []:
        if isinstance(edge, dict) and _edge_key(edge) not in known:
            out.append({"fix_id": fix_id, "node_id": None, "node_title": "", "field": "edge", "before": None,
                        "after": {"source": edge.get("source"), "target": edge.get("target"),
                                  "sourceHandle": edge.get("sourceHandle")}, "label": f"{label}：新加连线"})
    return out
