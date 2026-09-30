from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.labels import contract_label, field_label, judge_label, option_label, q
from app.engine.schema import (
    CLAIMS_JUDGE,
    GraphNode,
    GraphSpec,
    NodeType,
    ValidationResult,
    _caliber_inputs,
    claims_problem,
    contract_needs_metrics,
    judge_problems,
    type_label,
    validate_graph,
)

# 口径升版的三种合法处置。没有默认值是刻意的：升版最容易静默翻车的地方
# 就是"上周和这周的数其实不可比，但报表看起来一切正常"。
UPGRADE_POLICIES = {"recompute", "dual", "incomparable"}

UPGRADE_POLICY_LABELS = {k: option_label("upgrade_policy", k) for k in ("recompute", "dual", "incomparable")}


def lint_for_publish(spec: GraphSpec, *, level: str) -> ValidationResult:
    """发布门禁。

    published：基础可运行性之外只给警告。
    governed：受管模板，出具用的那一档 —— 把关节层的自由度收进边界里，
    错误会挡住发布。

    每条问题带机器编号（code）：发布前自动修复（engine/autofix.py）按它认问题、出修复，
    文案可以改，编号不能随便改。证据门禁 G1–G5 见 _lint_evidence。
    """
    result = ValidationResult()
    strict = level == "governed"

    def flag(message: str, *, code: str, node_id: str | None = None, hard: bool = False,
             field: str | None = None) -> None:
        result.add(message, level="error" if (hard and strict) else "warning", node_id=node_id,
                   field=field, code=code)

    has_contract_output = False
    for node in spec.nodes:
        cfg = node.config
        # 文案用画布上的叫法：节点标题 +（节点类型），参数写界面上的选项文字
        who = f"「{node.title}」（{type_label(node.type)}）"
        if node.type == NodeType.SUPERVISOR:
            # 全动态规划属于探索层。受管模板里出现它，等于把 XAgent 搬上了生产线。
            flag(
                f"{who}：受管级别不允许使用全动态规划的节点，其分工和轮数由模型在运行时决定，"
                "无法事先审核。请改用配置固定的 Agent 或「模型调用」节点",
                code="governed.supervisor", node_id=node.id, hard=True,
            )
        elif node.type == NodeType.SUBGRAPH:
            if not cfg.get("workflow_version"):
                flag(
                    f"{who}没有固定版本，口径会随上游最新版本变化。请在节点中选定一个版本",
                    code="governed.subgraph_unpinned", node_id=node.id, hard=True, field="workflow_version",
                )
        elif node.type == NodeType.AGENT:
            tools = cfg.get("tools") or []
            if cfg.get("approval") == "never" and tools:
                flag(
                    f"{who}的审批策略是「{option_label('approval', 'never')}」，受管级别要求危险工具至少经过人工审批。"
                    f"请改为「{option_label('approval', 'dangerous')}」",
                    code="governed.agent_approval_never", node_id=node.id, hard=True, field="approval",
                )
            elif cfg.get("approval") in (None, "") and spec.defaults.get("approval") == "never" and tools:
                # 节点自己没写，运行时跟随全图默认（context.approval_mode：节点 > 图级 defaults > 全局）。
                # 全图默认写着全部自动放行，这个 agent 就是全部自动放行，只是没写在它自己身上
                flag(
                    f"{who}没有设置审批策略，跟随{field_label('defaults')}中的「{option_label('approval', 'never')}」；受管级别要求危险工具至少"
                    f"经过人工审批。请将{field_label('defaults')}中的审批策略改为「{option_label('approval', 'dangerous')}」，或在该节点上单独设置",
                    code="governed.default_approval_never", node_id=node.id, hard=True, field="approval",
                )
            if not tools:
                flag(f"{who}没有配置可用工具，建议改用「模型调用」节点",
                     code="governed.agent_no_tools", node_id=node.id, field="tools")
            elif len(tools) > 10:
                flag(
                    f"{who}的可用工具有 {len(tools)} 个，授权范围过宽，建议只保留完成任务所需的工具",
                    code="governed.agent_tools_wide", node_id=node.id, field="tools",
                )
        elif node.type == NodeType.OUTPUT:
            contract = cfg.get("contract")
            if contract:
                has_contract_output = True
                _lint_contract(contract, node_id=node.id, spec=spec, flag=flag, strict=strict)
        elif node.type == NodeType.CODE:
            if cfg.get("network"):
                flag(f"{who}开启了「允许联网」：受管级别建议把取数集中到「调用工具」节点"
                     "（那里会保存取数快照）", code="governed.code_network", node_id=node.id, field="network")

    _lint_evidence(spec, flag, strict=strict)

    if strict and not has_contract_output:
        result.add(
            "受管级别要求至少一个「成果」节点声明出具契约，否则无法判定出具档位",
            level="error", code="governed.no_contract",
        )
    return result


def publish_issues(spec: GraphSpec, *, level: str) -> list[Any]:
    """发布时拦的全部问题：validate_graph 和 lint_for_publish 的并集，顺序也照发布接口。

    发布、发布前检查、自动修复复核都走这一个口径——检查说能发的，真发布时不会被拦。
    """
    gate = lint_for_publish(spec, level=level).issues
    return [*_not_repeated(validate_graph(spec).issues, gate, spec), *gate]


#: validate 里和门禁 G4、G5 说同一件事的两条提示（schema.evidence_issues）
_REPEATS = frozenset({"evidence.caliber_compute_input", "evidence.cite_fields_off"})


def _not_repeated(issues: list[Any], gate: list[Any], spec: GraphSpec) -> list[Any]:
    """门禁已经点了名的，validate 那条同义的提示不再列：门禁那条带编号、带修复，一处问题只说一遍。

    G5 落在 agent 上，validate 那条也落在 agent 上；G4 落在喂数的沙箱代码上，validate 那条落在口径卡读它的
    那条指标上，按口径卡的输入对回去。没被门禁覆盖的（比如不给口径卡供数的 agent）照旧提示。
    """
    cited = {i.node_id for i in gate if i.code == "governed.caliber_agent_cite_fields"}
    compute = {i.node_id for i in gate if i.code == "governed.caliber_compute_input"}
    covered = {("evidence.cite_fields_off", nid, "output_schema") for nid in cited}
    for card in spec.nodes:
        if card.type == NodeType.METRICS and compute:
            covered |= {("evidence.caliber_compute_input", card.id, f"metrics[{i}].expression")
                        for i, producer in _caliber_inputs(card, spec) if producer.id in compute}
    return [i for i in issues if i.code not in _REPEATS or (i.code, i.node_id, i.field) not in covered]


def _lint_contract(
    contract: Any, *, node_id: str, spec: GraphSpec, flag: Any, strict: bool
) -> None:
    """契约要查内容，不能只查这个 key 在不在。

    只看 key 存不存在的话，contract={"metrics_from": "随便一个字符串"} 就满足
    "受管模板必须声明出具契约"这条硬门禁了 —— 一个结构上保证每次都出 formal 的
    空壳模板，能合法发布成受管模板。门禁写成这样，还不如没有：它给的是
    "这张图过过闸"的错觉。
    """
    if not isinstance(contract, dict):
        flag("出具契约必须是一个 JSON 对象", code="contract.not_object", node_id=node_id, hard=True,
             field="contract")
        return

    metric_nodes = {n.id for n in spec.nodes if n.type == NodeType.METRICS}
    sources = contract.get("metrics_from") or []
    if isinstance(sources, str):
        sources = [sources]

    if not sources and contract_needs_metrics(contract):
        flag("出具契约没有设置「指标来自」（提供指标的口径卡），叙述中的数字无法对应到指标",
             code="contract.metrics_from_missing", node_id=node_id, hard=True, field="contract.metrics_from")
    for src in sources:
        if src not in metric_nodes:
            flag(
                f"出具契约的「指标来自」选择了{q(src)}，但工作流中没有这个口径卡节点"
                "（可能填写有误，或该节点尚未创建）",
                code="contract.metrics_from_invalid", node_id=node_id, hard=True, field="contract.metrics_from",
            )

    # 引用模式（report_from）核对的是报告撰写节点产出的文档，数字回指靠文档里的引用标记，
    # 用不上 narrative。report_from 指错了（不是报告撰写节点、不在出口上游）由 validate_graph
    # 报 error（contract.report_from_invalid），发布时两者一起过，照样挡住
    if contract.get("report_from") in (None, "") and not str(contract.get("narrative") or "").strip():
        flag("出具契约既没有设置「报告来自」（核对哪个「报告撰写」节点的文档），也没有填写「叙述」，"
             "不会执行数字核对", code="contract.report_from_missing", node_id=node_id, hard=True,
             field="contract.report_from")

    # 引用模式下整张图没有口径卡（只引查询单元格的问数据图）：没有指标可列，strict + cells 已经保证
    # 每个数点得开出处、对不上就不予出具（用户 2026-09-29 拍板）。有口径卡的照旧要列必需指标——
    # 按图里有没有口径卡判，不按契约写没写 metrics_from，免得不写 metrics_from 就绕过去
    cells_only = contract.get("report_from") not in (None, "") and not metric_nodes
    if strict and not (contract.get("required") or []) and not cells_only:
        flag(
            "受管级别的出具契约必须设置「必需指标」，否则「不予出具」这一档无法触发",
            code="contract.required_missing", node_id=node_id, hard=True, field="contract.required",
        )
    # 没有口径卡的图，报告里的数只能直接引用查询单元格；受管正式运行不认没声明的单元格引用，
    # 报告撰写节点（numbers strict、on_violation fail）每次都会失败。写不写要作者定：修复给选项
    if strict and cells_only and contract.get("cells") is not True:
        flag(
            "工作流中没有口径卡，报告中的数字只能直接引用查询单元格。受管级别需要在出具契约中开启「单元格引用」，"
            "否则正式运行时报告撰写节点无法引用任何数字而失败；也可以添加口径卡，把数字登记为指标",
            code="contract.cells_undeclared", node_id=node_id, hard=True, field="contract.cells",
        )
    if strict and not contract.get("strict"):
        flag(
            "受管级别建议开启出具契约的「严格模式」：未开启时，无法追溯出处的数字只会使出具降档，不会被拦截",
            code="contract.strict_off", node_id=node_id, field="contract.strict",
        )
    claims = contract.get("claims")
    if strict and isinstance(claims, dict) and claims.get("on_uncited") == "ignore":
        # 受管级别的正式运行把 ignore 当 degrade（io._claims），要求没有降低；只是写法看上去像放宽了
        flag(
            f"出具契约中「{contract_label('on_uncited')}」设为「{option_label('on_uncited', 'ignore')}」，但受管级别的"
            "正式运行仍会把未附依据的结论句计入缺口，这项设置不会生效。"
            f"请改为「{option_label('on_uncited', 'degrade')}」或「{option_label('on_uncited', 'withhold')}」",
            code="contract.claims_ignored", node_id=node_id, field="contract.claims",
        )


# --------------------------------------------------------------------------
# 证据门禁 G1–G5：受管出具要在结构上保证「报告里的每个数都点得开出处」
#
# G1 带契约的出口核对报告撰写节点的文档，成果字段的文字只能来自报告撰写节点
# G2 模型写的文字不绕过报告撰写节点直接流进出口（从图上追）
# G3 报告撰写节点写明 numbers: strict、on_violation: fail、claims: require_citation（或 judge，带上预算）
# G4 口径卡的输入不来自计算角色的沙箱代码
# G5 给口径卡供数的 agent 配 output_schema 并开 cite_fields（模型调用节点不能给口径卡供数）
#
# 只看图和配置，不看运行结果。只在发布和发布前检查时起作用：已经发布的版本照常运行，
# 下次发布才按这些规则查。受管级别挡住发布，已发布级别只给警告。
# --------------------------------------------------------------------------

#: 报告撰写节点在受管级别里要写明的三项：(配置键, 要求的值, 为什么)。给人看的时候键和值都换成
#: labels 里的界面叫法
REPORT_POLICY = (
    ("numbers", "strict", "没有出处的数字判为违规"),
    ("on_violation", "fail", "重写后仍有违规时节点失败，不带着问题出具"),
    ("claims", "require_citation", "未附依据的结论句计入缺口，出具按档位降档"),
)
#: 除了 REPORT_POLICY 里写的那个值，还认的更严的写法。claims: judge 在挂引用之外再请另一个模型按证据逐句
#: 裁判，不支持的按 on_unsupported 判档——比 require_citation 严，但要写预算（见 judge_budget_missing）
REPORT_POLICY_ALSO: dict[str, tuple[str, ...]] = {"claims": (CLAIMS_JUDGE,)}
#: 不限的写法：judge.max_cost_usd 写 null。说给人听的时候照 engine/judge 设置项的说法
JUDGE_UNLIMITED = "不设上限，费用只受句数、时长上限和每日上限约束"

#: 产出本身就是证据、或者由系统确定地算出来的节点。成果字段取它们的值，不算「别的节点写的字」
_TRACEABLE = (NodeType.REPORT, NodeType.METRICS, NodeType.INPUT, NodeType.TOOL, NodeType.RETRIEVE)
_WRITERS = (NodeType.LLM, NodeType.AGENT, NodeType.SUPERVISOR)
#: 往对话里写消息的节点（emit_message 没关时），「最后一条消息」就是它们里最后跑的那个写的
_EMITTERS = (*_WRITERS, NodeType.REPORT)
#: 把上游的值原样或加工后往下传的节点：追文字的来路要穿过它们
_RELAYS = (NodeType.TRANSFORM, NodeType.CODE, NodeType.VALIDATE, NodeType.HUMAN, NodeType.LOOP)
#: 往回追口径卡的输入时穿过的整形节点。沙箱代码不穿过：它自己就是 G4 要判的
_RESHAPERS = (NodeType.TRANSFORM, NodeType.VALIDATE)
#: 只决定走哪条路、不把值往下带的配置
_CONTROL_KEYS = frozenset({"skip_if", "condition", "cases"})

_TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.S)
# 模板和表达式里对值的引用。nodes.<id>…、vars.<名字>… 落得到具体节点；last_message、messages
# 是最后写消息的那个节点写的；output、loops（和整个 nodes / vars）说不准来自谁
_REF = re.compile(
    r"(?<![\w.])(?P<root>nodes|vars|last_message|messages|output|loops)(?![\w-])"
    r"(?P<path>(?:\s*(?:\.\s*[\w-]+|\[\s*(?:'[^']*'|\"[^\"]*\"|-?\d+)\s*\]))*)")
_SEGMENT = re.compile(r"\.\s*([\w-]+)|\[\s*(?:'([^']*)'|\"([^\"]*)\"|(-?\d+))\s*\]")

#: (根, 名字, 往里取的路径)。last_message 这类没有名字
Ref = tuple[str, str, tuple[str, ...]]
_LAST: Ref = ("last_message", "", ())


def _refs(text: str, *, template: bool) -> list[Ref]:
    out: list[Ref] = []
    for piece in [m.group(1) for m in _TEMPLATE.finditer(text)] if template else [text]:
        for m in _REF.finditer(piece):
            path = tuple(next(g for g in s.groups() if g is not None) for s in _SEGMENT.finditer(m.group("path")))
            root = m.group("root")
            if root in ("nodes", "vars") and path:
                out.append((root, path[0], path[1:]))
            else:
                out.append((root, "", path))
    return out


def _strings(value: Any) -> list[str]:
    """配置里所有带 {{ }} 的字符串。"""
    if isinstance(value, str):
        return [value] if "{{" in value else []
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _template_refs(value: Any) -> list[Ref]:
    return [r for text in _strings(value) for r in _refs(text, template=True)]


def _flow_refs(node: GraphNode) -> list[Ref]:
    """这个节点的产出读了哪些值。判断走哪条路的条件不算；校验节点没写 source 时校验的是最后一条消息。"""
    out: list[Ref] = []
    for key, value in node.config.items():
        if key in _CONTROL_KEYS:
            continue
        out += _refs(value, template=False) if key == "expression" and isinstance(value, str) \
            else _template_refs(value)
    if node.type == NodeType.VALIDATE and node.config.get("source") in (None, ""):
        out.append(_LAST)
    return out


def _effective(spec: GraphSpec, node: GraphNode, key: str, default: Any = None) -> Any:
    """和运行时 NodeContext.cfg 同一个规则：节点上没写就取图级 defaults。"""
    value = node.config.get(key)
    return spec.defaults.get(key, default) if value in (None, "") else value


def judge_budget_missing(spec: GraphSpec, node: GraphNode) -> bool:
    """claims 是 judge、生效的 judge 配置里却没有 max_cost_usd 这个键（G3）。

    写 null 是显式的不限，算写了；没写的键运行时取设置里的默认——受管出具要作者自己定。judge 本身写坏了
    （不是对象、max_cost_usd 写成了别的东西）由 validate 的 report.judge_invalid 报，这里不重复。
    judge 和 claims 一样按运行时的规则取：节点上写了就整个用节点的，不和图级 defaults 拼。
    """
    if _effective(spec, node, "claims") != CLAIMS_JUDGE:
        return False
    judge = _effective(spec, node, "judge")
    if any(field in ("judge", "judge.max_cost_usd") for field, _ in judge_problems(judge)):
        return False
    return not (isinstance(judge, dict) and "max_cost_usd" in judge)


class _Graph:
    """一张图上反复要用的查找：谁产出了某个值、最后一条消息是谁写的。"""

    def __init__(self, spec: GraphSpec) -> None:
        self.spec = spec
        self.nodes = spec.node_map()
        self.parents: dict[str, list[str]] = defaultdict(list)
        for e in spec.edges:
            self.parents[e.target].append(e.source)

    def producers(self, root: str, name: str) -> list[GraphNode]:
        """nodes.<name> 就是那个节点；vars.<name> 是用 assign_to 写它的节点、同名的入口字段、
        每轮注入它的循环。"""
        if not name or root not in ("nodes", "vars"):
            return []
        if root == "nodes":
            return [self.nodes[name]] if name in self.nodes else []
        out = []
        for n in self.spec.nodes:
            cfg = n.config
            if n.type == NodeType.INPUT:
                writes = {str(f.get("name") or "").strip() for f in cfg.get("fields") or [] if isinstance(f, dict)}
            elif n.type == NodeType.LOOP and cfg.get("mode", "foreach") == "foreach":
                item = str(cfg.get("item_var") or "item").strip()
                writes = {item, f"{item}_index"}
            else:
                writes = set()
            if name in writes or str(cfg.get("assign_to") or "").strip() == name:
                out.append(n)
        return out

    def last_writers(self, node_id: str) -> list[GraphNode]:
        """到这个节点时，最后一条消息可能是谁写的：沿连线往回走，每条路上遇到的第一个写消息的节点。"""
        found: set[str] = set()
        seen = {node_id}
        stack = list(self.parents[node_id])
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.nodes:
                continue
            seen.add(cur)
            n = self.nodes[cur]
            if n.type in _EMITTERS and _effective(self.spec, n, "emit_message", True):
                found.add(cur)
                continue
            stack.extend(self.parents[cur])
        return [n for n in self.spec.nodes if n.id in found]

    def sources(self, consumer: str, ref: Ref) -> list[GraphNode]:
        root, name, _ = ref
        return self.last_writers(consumer) if root in ("last_message", "messages") else self.producers(root, name)


def _who(node: GraphNode) -> str:
    return f"「{node.title}」（{type_label(node.type)}）"


def _traceable(node: GraphNode) -> bool:
    return node.type in _TRACEABLE or (node.type == NodeType.CODE and node.config.get("evidence_role") == "source")


def _model_text(node: GraphNode, ref: Ref) -> bool:
    """顺着这个引用拿到的是不是模型写的话。开了 cite_fields 的 agent，assign_to（和 nodes.<id>.data）
    拿到的是逐格核对过的字段，那是数据；它的原话、最后一条消息照样是模型写的。"""
    if node.type in (NodeType.LLM, NodeType.SUPERVISOR):
        return True
    if node.type != NodeType.AGENT:
        return False
    root, _, path = ref
    cited = bool(node.config.get("output_schema")) and node.config.get("cite_fields") is True
    return not (cited and (root == "vars" or path[:1] == ("data",)))


def _exit_refs(node: GraphNode) -> list[tuple[str, str | None, list[Ref]]]:
    """出口交出去的每个字段：(字段路径, 字段名, 引用)。没配字段时交的是最后一条消息（io.run_output）。"""
    fields = node.config.get("fields")
    if not fields:
        return [("fields", None, [_LAST])]
    return [(f"fields[{i}].value", str(f["name"]), _template_refs(f.get("value", "")))
            for i, f in enumerate(fields if isinstance(fields, list) else [])
            if isinstance(f, dict) and f.get("name")]


def _untraceable_sources(graph: _Graph, exit_id: str, refs: list[Ref]) -> list[str]:
    """一个成果字段里核对不了出处的来源，一个来源一句（「叙述」（模型调用）、最后一条消息…）。"""
    out: list[str] = []
    for ref in refs:
        root, name, _ = ref
        if root in ("output", "loops") or (root in ("nodes", "vars") and not name):
            out.append(f"{q(root)}中的值（无法确定来自哪个节点）")
        elif root in ("last_message", "messages"):
            out += [f"最后一条消息（由{_who(w)}写出）" for w in graph.last_writers(exit_id) if w.type != NodeType.REPORT]
        else:
            # 开了 cite_fields 的 agent 交的字段逐格核对过，是数据不是模型写的话——和 G2（_model_text）同一口径
            out += [_who(p) for p in graph.producers(root, name)
                    if not _traceable(p) and (p.type != NodeType.AGENT or _model_text(p, ref))]
    return list(dict.fromkeys(out))


def exit_text_fields(spec: GraphSpec, exit_node: GraphNode, graph: _Graph | None = None,
                     ) -> list[tuple[str, str | None, list[str]]]:
    """G1：出口里文字不是来自报告撰写节点的字段：(字段路径, 字段名, 来源)。字段名为 None 是没配字段。

    发布前自动修复（autofix）按同一份结果改字段，门禁和修复认的是同一批字段。
    """
    graph = graph or _Graph(spec)
    out = []
    for field, name, refs in _exit_refs(exit_node):
        if bad := _untraceable_sources(graph, exit_node.id, refs):
            out.append((field, name, bad))
    return out


def _text_leaks(graph: _Graph, exit_node: GraphNode) -> dict[str, tuple[GraphNode, set[tuple[str, ...]]]]:
    """流进这个出口、没经过报告撰写节点的模型原话：写字的节点 id → (节点, 途经的节点标题)。

    途经为空的是出口字段直接取的。沿着模板和表达式里的引用往回追，穿过整形、沙箱代码、校验、人工审批、
    循环；碰到报告撰写节点就停——模型写的话进了报告，就只是写作者的素材。
    """
    found: dict[str, tuple[GraphNode, set[tuple[str, ...]]]] = {}
    seen: set[str] = set()
    todo: list[tuple[str, Ref, tuple[str, ...]]] = [
        (exit_node.id, ref, ()) for _, _, refs in _exit_refs(exit_node) for ref in refs]
    while todo:
        consumer, ref, via = todo.pop(0)
        for source in graph.sources(consumer, ref):
            if source.type == NodeType.REPORT or source.id == exit_node.id:
                continue
            if source.type in _WRITERS:
                if _model_text(source, ref):
                    found.setdefault(source.id, (source, set()))[1].add(via)
            elif source.type in _RELAYS and source.id not in seen:
                seen.add(source.id)
                todo += [(source.id, r, (*via, source.title)) for r in _flow_refs(source)]
    return found


def _card_feeders(graph: _Graph, card: GraphNode) -> list[GraphNode]:
    """口径卡的输入是谁产出的：穿过整形、校验节点往回追到真正的来源。"""
    out: dict[str, GraphNode] = {}
    seen: set[str] = set()
    todo = [producer for _, producer in _caliber_inputs(card, graph.spec)]
    while todo:
        node = todo.pop(0)
        if node.id in seen or node.id == card.id:
            continue
        seen.add(node.id)
        if node.type in _RESHAPERS:
            todo += [s for ref in _flow_refs(node) for s in graph.sources(node.id, ref)]
        else:
            out.setdefault(node.id, node)
    return list(out.values())


def _lint_evidence(spec: GraphSpec, flag: Callable[..., None], *, strict: bool) -> None:
    graph = _Graph(spec)
    outputs = [n for n in spec.nodes if n.type == NodeType.OUTPUT]
    contracted = {n.id for n in outputs if isinstance(n.config.get("contract"), dict) and n.config["contract"]}

    # G1
    for node in outputs:
        if node.id not in contracted:
            continue
        contract = node.config["contract"]
        # 两样都没写的由 contract.report_from_missing 报，这里只管「写了叙述、没写 report_from」
        if contract.get("report_from") in (None, "") and str(contract.get("narrative") or "").strip():
            flag(f"{_who(node)}的出具契约使用「叙述」核对，没有设置「报告来自」：数字只能按数值匹配，出处可能不唯一。"
                 "受管级别要求核对「报告撰写」节点产出的文档，每个数字都有唯一的出处。"
                 "请把「报告来自」设为成果节点上游的报告撰写节点",
                 code="governed.report_from_required", node_id=node.id, hard=True, field="contract.report_from")
        for field, name, bad in exit_text_fields(spec, node, graph):
            head = (f"{_who(node)}的成果字段「{name}」取自{'、'.join(bad)}的产出" if name is not None
                    else f"{_who(node)}没有配置成果字段，输出的是{'、'.join(bad)}")
            flag(f"{head}：受管级别出具时，给人看的文字只能来自「报告撰写」节点，其他节点写的文字无法追溯出处。"
                 + ("请改为取报告撰写节点的正文" if name is not None else "请为成果节点添加一个字段，取报告撰写节点的正文"),
                 code="governed.exit_text_source", node_id=node.id, hard=True, field=field)

    # G2：每个出口都算。受管级别是 error，已发布级别是警告——和别的 G 规则一样，级别只管轻重，不管范围
    flagged: set[str] = set()
    for node in outputs:
        for writer, vias in _text_leaks(graph, node).values():
            indirect = sorted(v for v in vias if v)
            # 带契约的出口里字段直接取的，G1 已经点名了那个字段
            if writer.id in flagged or (node.id in contracted and not indirect):
                continue
            flagged.add(writer.id)
            via = f"（经过{'、'.join(f'「{t}」' for t in indirect[0])}）" if indirect else ""
            flag(f"{_who(writer)}写的文字未经「报告撰写」节点，就进入了成果节点「{node.title}」{via}：模型写的文字不能"
                 "作为证据，其中的数字和表名都无法追溯出处。请改由报告撰写节点撰写（它只写引用标记，数字由系统填入），"
                 "成果节点改为取报告的正文",
                 code="governed.text_bypass", node_id=writer.id, hard=True)

    # G3
    for node in spec.nodes:
        if node.type != NodeType.REPORT:
            continue
        loose = []
        for key, want, why in REPORT_POLICY:
            value = _effective(spec, node, key)
            if key == "claims" and claims_problem(value):
                continue        # 写了不认识的值，validate 的 report.claims_invalid 报
            also = REPORT_POLICY_ALSO.get(key, ())
            if value != want and value not in also:
                now = "当前未设置" if value in (None, "") else f"当前为「{option_label(key, value)}」"
                more = "".join(f"或更严格的「{option_label(key, v)}」" for v in also)
                loose.append((key, f"「{field_label(key)}」设为「{option_label(key, want)}」{more}（{now}）"))
        if loose:
            flag(f"{_who(node)}需要调整核对规则：{'；'.join(text for _, text in loose)}。"
                 "受管级别要求报告撰写节点使用最严格的核对规则",
                 code="governed.report_policy", node_id=node.id, hard=True, field=loose[0][0])
        if judge_budget_missing(spec, node):
            flag(f"{_who(node)}的结论句由模型裁判，但没有设置每份报告的裁判金额上限。请在「{field_label('judge')}」中"
                 f"填写「{judge_label('max_cost_usd')}」，或明确选择不限（{JUDGE_UNLIMITED}）。"
                 "受管级别不允许裁判费用沿用设置中的默认值",
                 code="governed.judge_budget", node_id=node.id, hard=True, field="judge.max_cost_usd")

    # G4、G5
    feeders: dict[str, tuple[GraphNode, list[str]]] = {}
    for card in spec.nodes:
        if card.type == NodeType.METRICS:
            for feeder in _card_feeders(graph, card):
                feeders.setdefault(feeder.id, (feeder, []))[1].append(f"「{card.title}」")
    for feeder, cards in feeders.values():
        to = f"口径卡{'、'.join(dict.fromkeys(cards))}"
        cfg = feeder.config
        if feeder.type == NodeType.CODE and cfg.get("evidence_role") != "source":
            role = "" if cfg.get("evidence_role") else "（未设置时默认为「计算」）"
            flag(f"{_who(feeder)}的产出提供给了{to}，但它的「证据角色」是「计算」{role}：沙箱中计算出的数字无法追溯出处。"
                 "若该节点负责取数，请将「证据角色」设为「取数」；若负责计算，请把计算移到口径卡的表达式中",
                 code="governed.caliber_compute_input", node_id=feeder.id, hard=True, field="evidence_role")
        elif feeder.type == NodeType.AGENT and not cfg.get("output_schema"):
            flag(f"{_who(feeder)}为{to}提供数据，但未配置「结构化输出 Schema」，交出的是自由文本，口径卡读取的数字"
                 "无法追溯出处。请配置「结构化输出 Schema」并开启「按出处核对字段」，使每个字段都核对到查询结果中的"
                 "对应单元格",
                 code="governed.caliber_agent_schema", node_id=feeder.id, hard=True, field="output_schema")
        elif feeder.type == NodeType.AGENT and cfg.get("cite_fields") is not True:
            flag(f"{_who(feeder)}为{to}提供数据，已配置「结构化输出 Schema」，但未开启「按出处核对字段」：字段没有"
                 "逐一核对到查询结果，口径卡读取的仍是模型给出的数字。请开启「按出处核对字段」",
                 code="governed.caliber_agent_cite_fields", node_id=feeder.id, hard=True, field="cite_fields")
        elif feeder.type == NodeType.LLM:
            flag(f"{_who(feeder)}为{to}提供数据：「模型调用」节点不调用工具，写出的数字没有出处。请改由 Agent"
                 "（配置「结构化输出 Schema」并开启「按出处核对字段」）或「调用工具」节点取数",
                 code="governed.caliber_model_input", node_id=feeder.id, hard=True)


async def unresolved_caliber_upgrades(
    session: AsyncSession, spec: GraphSpec
) -> tuple[list[str], list[dict[str, Any]]]:
    """检查钉了版本的上游是否有未处置的新版本：子工作流（方法卡），以及用 caliber_from
    钉住别处口径卡的口径卡节点——两者是同一套规则。

    返回 (阻断性错误, 需记录的升版事件)。有新版本但没声明 upgrade_policy
    的直接阻断 formal 启动 —— 这是"被迫处置"，不是提醒。
    """
    from app.db.models import WorkflowVersion

    errors: list[str] = []
    events: list[dict[str, Any]] = []
    for node in spec.nodes:
        extra: dict[str, Any] = {}
        if node.type == NodeType.SUBGRAPH:
            pinned = node.config.get("workflow_version")
            wf_id = node.config.get("workflow_id")
            what = f"「{node.title}」（子工作流）固定在 v{pinned}"
        elif node.type == NodeType.METRICS and isinstance(node.config.get("caliber_from"), dict):
            ref = node.config["caliber_from"]
            pinned, wf_id = ref.get("workflow_version"), ref.get("workflow_id")
            extra = {"caliber_node": ref.get("node_id")}
            what = f"「{node.title}」（口径卡）引用的口径卡固定在 v{pinned}"
        else:
            continue
        if not pinned or not wf_id or not str(pinned).isdigit():
            continue
        latest = (
            await session.execute(
                select(func.max(WorkflowVersion.version)).where(
                    WorkflowVersion.workflow_id == wf_id
                )
            )
        ).scalar()
        if latest is None or latest <= int(pinned):
            continue
        policy = node.config.get("upgrade_policy")
        if policy not in UPGRADE_POLICIES:
            errors.append(
                f"{what}，但上游已有 v{latest}。"
                f"发起正式运行前，请在节点的「{field_label('upgrade_policy')}」中选择一种处置："
                + _either(UPGRADE_POLICY_LABELS.values())
            )
        else:
            events.append(
                {
                    "node_id": node.id,
                    "workflow_id": wf_id,
                    "pinned": int(pinned),
                    "latest": int(latest),
                    "policy": policy,
                    "policy_label": UPGRADE_POLICY_LABELS[policy],
                    **extra,
                }
            )
    return errors, events


def _either(labels: Any) -> str:
    """「甲」、「乙」或「丙」。"""
    items = [f"「{v}」" for v in labels]
    return items[0] if len(items) == 1 else "、".join(items[:-1]) + "或" + items[-1]


#: 发起正式运行时记下「这次按不按受管出具」的那条事件（log · info，不进主流程）
GOVERNANCE_CODE = "run_governance"


def governance_note(*, governed: bool, workflow_name: str, version: int | None = None) -> dict[str, Any]:
    """发起正式运行时落进事件流的那条记录（runs.start_run 调用）。

    governed 由调用方按这次钉住的版本发布时记下的级别定（WorkflowVersion.level），老版本没记过的
    看工作流当时的 status。
    """
    which = f"「{workflow_name}」v{version}" if version else f"「{workflow_name}」"
    message = (f"本次正式运行按受管级别出具：{which}以受管级别发布，之后修改工作流或发布级别不影响本次运行"
               if governed else
               f"本次正式运行按已发布级别出具：{which}未以受管级别发布")
    return {"level": "info", "code": GOVERNANCE_CODE, "governed": governed, "message": message}


async def governed_formal(run_id: str) -> bool:
    """这次运行是不是受管级别的正式运行：从按受管级别发布的版本发起。

    受管的判断沿用发布门禁的口径（lint_for_publish 的 strict 档位）。探索运行、已发布级别的
    正式运行都不是。

    报告节点和出口复核各问一次、前后隔着审批和重放，所以按发起时记下的那条事件算（start_run
    在封存范围内记的 run_governance，来源是钉住的那一版发布时记下的级别）；没有这条记录的是
    加这条之前发起的运行，照旧看工作流现在的 status。
    """
    from app.db.base import SessionLocal
    from app.db.models import Run, RunEvent, Workflow

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or run.run_class != "formal" or not run.workflow_id:
            return False
        # 这条事件没有 node_id，发起时就记下了：只翻不属于任何节点的日志
        notes = (await session.execute(
            select(RunEvent.data).where(RunEvent.run_id == run_id, RunEvent.type == "log",
                                        RunEvent.node_id.is_(None)).order_by(RunEvent.seq))).scalars()
        for data in notes:
            if isinstance(data, dict) and data.get("code") == GOVERNANCE_CODE:
                return data.get("governed") is True
        workflow = await session.get(Workflow, run.workflow_id)
        return workflow is not None and workflow.status == "governed"


def cells_allowed(contract: Any, *, governed: bool) -> bool:
    """报告能不能直接引用查询单元格。受管级别的正式运行要在契约里显式写 "cells": true；
    探索运行和已发布级别一律允许（用户拍板）。"""
    return not governed or (isinstance(contract, dict) and contract.get("cells") is True)
