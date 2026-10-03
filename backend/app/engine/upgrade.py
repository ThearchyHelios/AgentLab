"""一键升级为可追溯结构（证据五期）。

旧图里给人看的报告是模型调用（llm）写的自由文本，问数据的答案是 agent 的原话：数字是模型转写的，
出处只能在出口按数值回头猜。这里按确定的规则把它们改写成可追溯的结构，只给预览，人确认了才保存：

- R1 出具契约的 narrative 指向一个模型调用节点：把它换成报告撰写（system 加 prompt 合成 instructions，
  模型配置照搬），契约改成 report_from
- R2 没有契约，模型调用写的文字进了出口，上游又有取数（查库、口径卡）：同样换成报告撰写
- R3 input → agent → output（问数据页的典型图）：在 agent 和出口之间插一个报告撰写，出口改取它的正文
- R4 配了 output_schema 的 agent：打开 cite_fields（不开的话 Schema 不生效）
- R5 沙箱代码的产出喂给口径卡：不改，只在 notes 里建议标成 source——它在取数还是在算只有作者知道

纯函数、幂等：同一张图升级两次，第二次什么都不改。每一步都在副本上做，过一遍和发布前修复同一套禁止
规则（autofix.forbidden_changes，只多「模型调用换成报告撰写」「R3 改接的那条连线」两个例外），再重跑
validate 和门禁：冒出新 error 的那一步丢弃，写明原因。和 autofix 一样直接改原始的图字典，坐标、连线 id、
视口这些前端的东西原样留着。

受管出具的契约要不要写 cells（报告直接引用查询单元格）、required 列哪些指标，都是作者的决定：R3 不替人
加契约，只在 notes 里把两条路说清楚。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from app.engine.autofix import forbidden_changes, worse
from app.engine.governance import (
    REPORT_POLICY,
    REPORT_POLICY_ALSO,
    Ref,
    _card_feeders,
    _exit_refs,
    _Graph,
    _refs,
    publish_issues,
)
from app.engine.labels import field_label, option_label
from app.engine.schema import GraphNode, GraphSpec, NodeType, _ancestors, _feeds_caliber, innermost_loops

#: 会改图的规则，按这个顺序应用：先换写字的节点，再插报告，最后开 cite_fields（R3 要先把出口从 agent 的
#: 原话改接到报告上，R4 打开以后 vars.<assign_to> 就是核对过的对象、不再是文字了）
RULES = ("R1", "R2", "R3", "R4")
#: validate 给的那条建议（info）的编号和开头
HINT_CODE = "evidence.upgrade_available"
HINT = "可以升级为可追溯结构"
LEVELS = ("published", "governed")

#: 模型调用有、报告撰写用不上的配置。system、prompt 合进 instructions；其余的换成报告撰写后不再生效
_LLM_ONLY = ("prompt", "system", "skills", "use_history", "output_schema")
#: 新的报告撰写节点照搬过去的模型配置（R3 从 agent 带过去的只有 provider、model：温度、上限是查数用的）
_MODEL_KEYS = ("provider", "model")
#: 报告的指令里照搬了这几类节点的文字，就要提醒：报告撰写自己会收集它们的证据
_EVIDENCE_TYPES = (NodeType.METRICS, NodeType.TOOL, NodeType.AGENT, NodeType.RETRIEVE, NodeType.CODE,
                   NodeType.SUBGRAPH, NodeType.MERGE)
#: 写整段文字的节点：出具契约的 narrative 拼了其中几个的文字，就说不准该换哪一个
_WRITER_TYPES = (NodeType.LLM, NodeType.AGENT)
_QUESTION = re.compile(r"(?<![\w.])input\s*\.\s*question(?![\w-])")
_SPAN = re.compile(r"\{\{(.*?)\}\}", re.S)


@dataclass(frozen=True)
class Match:
    """一处符合 R1–R4 的旧结构。blocked 不为空的是看着像、但说不准怎么改的，只进 notes。

    exits 是这一步要改的出口；skipped 是认出来了、但这一步不碰的出口各自的原因（R3 里要经过别的节点才读到
    agent 原话的出口），这一步照改其余的出口，skipped 进 notes。"""

    rule: str
    node_id: str
    exits: tuple[str, ...] = ()
    summary: str = ""
    blocked: str | None = None
    skipped: tuple[str, ...] = ()


@dataclass
class _Step:
    label: str
    changes: list[dict[str, Any]] = field(default_factory=list)
    ops: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    retyped: set[tuple[str, str, str]] = field(default_factory=set)
    rewired: set[tuple[str, str]] = field(default_factory=set)
    created: bool = False


# --------------------------------------------------------------------------
# 认旧结构
# --------------------------------------------------------------------------


def _queries(node: GraphNode) -> bool:
    """这个节点查库：报告撰写能把它查到的结果编成 Q1、Q2… 引用。只认数据源的查询工具，别的工具的
    返回进不了目录。合并查询的结果和数据源查询同形，同样编号。"""
    if node.type == NodeType.MERGE:
        return True
    if node.type == NodeType.TOOL:
        return str(node.config.get("tool") or "").startswith("db_query__")
    if node.type == NodeType.AGENT:
        tools = node.config.get("tools")
        return isinstance(tools, list) and any(str(t).startswith("db_query__") for t in tools)
    return False


def _fetches(node: GraphNode) -> bool:
    """上游的取数：查库、口径卡——报告撰写的目录里就有能当值引用的数。知识库检索不算：文档里的话只能当引文，
    知识库问答的答案换成报告撰写，里面转述的数全会被判成没有出处。"""
    return node.type == NodeType.METRICS or _queries(node)


def _cited(node: GraphNode) -> bool:
    return bool(node.config.get("output_schema")) and node.config.get("cite_fields") is True


def _text_ref(node: GraphNode, ref: Ref) -> bool:
    """顺着这个引用拿到的是不是它写的整段文字：nodes.X、nodes.X.text、vars.<assign_to>、最后一条消息。
    开了 cite_fields 的 agent，assign_to 拿到的是核对过的字段，那是数据不是原话。"""
    root, _, path = ref
    if root in ("last_message", "messages"):
        return True
    if root == "nodes":
        return path in ((), ("text",))
    if root == "vars":
        return not path and not (node.type == NodeType.AGENT and _cited(node))
    return False


def _writers(graph: _Graph, consumer: str, refs: list[Ref], types: tuple[NodeType, ...]) -> list[GraphNode]:
    """这些引用读到了哪些节点写的整段文字，按画布上的节点顺序。"""
    found: set[str] = set()
    for ref in refs:
        found |= {src.id for src in graph.sources(consumer, ref) if src.type in types and _text_ref(src, ref)}
    return [n for n in graph.spec.nodes if n.id in found]


def _exit_text_refs(exit_node: GraphNode) -> list[Ref]:
    return [ref for _, _, refs in _exit_refs(exit_node) for ref in refs]


def _contract(node: GraphNode) -> dict[str, Any]:
    contract = node.config.get("contract")
    return contract if isinstance(contract, dict) else {}


def _names(nodes: list[GraphNode]) -> str:
    return "、".join(f"「{n.title}」" for n in nodes)


def _llm_blocked(spec: GraphSpec, node: GraphNode, loops: dict[str, str]) -> str | None:
    """这个模型调用节点能不能原样换成报告撰写；不能的说清为什么。"""
    who = f"「{node.title}」（模型调用）"
    if node.config.get("output_schema"):
        return f"{who}配置了「结构化输出 Schema」，输出的是结构化数据而不是给人看的文字，因此没有换成报告撰写"
    if node.id in loops:
        return f"{who}位于循环体中，换成报告撰写会导致每一轮都写一份报告：循环结构需要由你调整，升级未作修改"
    if _feeds_caliber(node, spec):
        return (f"{who}写的数字还提供给了口径卡：换成报告撰写后，口径卡将无法读取这些数字。请先改由 Agent（配置"
                "「结构化输出 Schema」、开启「按出处核对字段」）或「调用工具」节点取数，再升级")
    return None


def _add(found: dict[tuple[str, str], Match], match: Match) -> None:
    """同一个节点从几个出口认出来的合成一处：出口、跳过的原因并起来；任何一个出口说不准，整处都不改。
    结果不能看出口在画布上谁排前面。"""
    key = (match.rule, match.node_id)
    was = found.get(key)
    if was is None:
        found[key] = match
        return
    found[key] = Match(was.rule, was.node_id, tuple(dict.fromkeys([*was.exits, *match.exits])),
                       was.summary or match.summary, was.blocked or match.blocked,
                       tuple(dict.fromkeys([*was.skipped, *match.skipped])))


def upgrade_candidates(spec: GraphSpec) -> list[Match]:
    """图上符合 R1–R4 的旧结构，按规则、再按节点在画布上的顺序。只看结构，不改图、不跑门禁：
    validate 每改一下画布就调一次，要便宜；真改的时候 upgrade_for_evidence 再逐步复核。"""
    graph = _Graph(spec)
    nodes = spec.node_map()
    loops = innermost_loops(spec)
    order = {n.id: i for i, n in enumerate(spec.nodes)}
    found: dict[tuple[str, str], Match] = {}
    outputs = [n for n in spec.nodes if n.type == NodeType.OUTPUT]

    for exit_node in outputs:
        contract = _contract(exit_node)
        narrative = contract.get("narrative")
        if contract.get("report_from") not in (None, "") or not isinstance(narrative, str) or not narrative.strip():
            continue
        writers = _writers(graph, exit_node.id, _refs(narrative, template=True), _WRITER_TYPES)
        llms = [w for w in writers if w.type == NodeType.LLM]
        if len(writers) > 1:
            # 叙述拼了几个节点写的文字：换哪一个都会让其余的文字悄悄离开契约的核对。拼进来的每个模型调用都不换，
            # 哪怕它另有一个只读它的出口——不然结果要看两个出口在画布上谁排前面
            why = (f"「{exit_node.title}」的出具契约「叙述」拼接了{_names(writers)}等多个节点的文字，无法确定应替换"
                   "哪一个，因此没有升级。请由一个节点撰写全文（其余内容作为它的素材），再升级")
            for llm in llms:
                _add(found, Match("R1", llm.id, blocked=why))
        elif llms:
            llm = llms[0]
            _add(found, Match("R1", llm.id, (exit_node.id,),
                              f"把「{llm.title}」（模型调用）写的叙述换成报告撰写，出具契约改为核对它的文档",
                              _llm_blocked(spec, llm, loops)))

    r1 = {m.node_id for m in found.values()}
    for exit_node in outputs:
        if _contract(exit_node):
            continue            # 有契约的归 R1（叙述模式）或者已经是引用模式
        for llm in _writers(graph, exit_node.id, _exit_text_refs(exit_node), (NodeType.LLM,)):
            if llm.id in r1 or not any(_fetches(nodes[a]) for a in _ancestors(spec, llm.id) if a in nodes):
                continue
            _add(found, Match("R2", llm.id, (exit_node.id,),
                              f"「{llm.title}」（模型调用）写的文字直接进入了成果节点，换成报告撰写",
                              _llm_blocked(spec, llm, loops)))

    for exit_node in outputs:
        contract = _contract(exit_node)
        if contract.get("report_from") not in (None, ""):
            continue
        refs = _exit_text_refs(exit_node)
        narrated: list[GraphNode] = []
        if isinstance(contract.get("narrative"), str):
            story = _refs(contract["narrative"], template=True)
            refs += story
            narrated = _writers(graph, exit_node.id, story, _WRITER_TYPES)
        for agent in _writers(graph, exit_node.id, refs, (NodeType.AGENT,)):
            # 喂口径卡的 agent 是取数的，不是作答的：它的原话漏进出口由门禁 G2 管，不在这里插报告
            if not _queries(agent) or _feeds_caliber(agent, spec):
                continue
            # 说不准的只跳过这个出口：同一个 agent 直连的出口照样插报告撰写，结果不看出口在画布上的顺序
            if agent.id in loops:
                _add(found, Match("R3", agent.id, blocked=(
                    f"「{agent.title}」位于循环体中，升级不会修改循环结构，因此没有插入报告撰写")))
            elif not any(e.source == agent.id and e.target == exit_node.id for e in spec.edges):
                _add(found, Match("R3", agent.id, skipped=(
                    f"「{agent.title}」的原话要经过其他节点才到达「{exit_node.title}」，升级不会改接中间的连线：请在它"
                    f"后面接一个报告撰写节点，「{exit_node.title}」改为取报告的正文",)))
            elif agent.id in {n.id for n in narrated} and len(narrated) > 1:
                _add(found, Match("R3", agent.id, skipped=(
                    f"「{exit_node.title}」的出具契约「叙述」拼接了{_names(narrated)}等多个节点的文字，改为核对报告撰写"
                    "的文档后，其余文字将不再受核对，因此没有升级这个成果节点。请由一个节点撰写全文后再升级",)))
            else:
                _add(found, Match("R3", agent.id, (exit_node.id,)))

    for key, m in found.items():
        if m.rule != "R3":
            continue
        agent = nodes[m.node_id]
        if m.exits:
            found[key] = replace(m, summary=f"在「{agent.title}」和{_names([nodes[x] for x in m.exits])}之间插入报告撰写，"
                                            "答案中的数字均可查看出处")
        elif not m.blocked:
            found[key] = replace(m, blocked="；".join(m.skipped), skipped=())

    for n in spec.nodes:
        if n.type == NodeType.AGENT and n.config.get("output_schema") and n.config.get("cite_fields") is not True:
            _add(found, Match("R4", n.id, (), f"开启「{n.title}」的「按出处核对字段」，「结构化输出 Schema」才会生效"))

    return sorted(found.values(), key=lambda m: (RULES.index(m.rule), order.get(m.node_id, len(order))))


def upgrade_hint(spec: GraphSpec) -> str | None:
    """validate 的那条建议的文案；没有能升级的旧结构返回 None。

    升级是整张图一起改的，建议挂在图上、不挂在哪个节点上：挂上去就混进了那个节点自己的问题清单。
    要改哪几个节点，预览里逐项列着。"""
    ready = [m for m in upgrade_candidates(spec) if not m.blocked]
    if not ready:
        return None
    parts = [m.summary for m in ready[:3]] + ([f"另有 {len(ready) - 3} 处"] if len(ready) > 3 else [])
    return f"{HINT}：{'；'.join(parts)}。请先预览改动，确认后再保存"


# --------------------------------------------------------------------------
# 改图（原地改副本）
# --------------------------------------------------------------------------


def _raw(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in graph.get("nodes") or [] if isinstance(n, dict) and n.get("id") == node_id)


def _config(node: dict[str, Any]) -> dict[str, Any]:
    data = node.setdefault("data", {})
    if not isinstance(data.get("config"), dict):
        data["config"] = {}
    return data["config"]


def _note(text: str, rule: str, node_id: str | None, level: str = "info") -> dict[str, Any]:
    return {"text": text, "rule": rule, "node_id": node_id, "level": level}


def _change(node_id: str | None, title: str, field_name: str, before: Any, after: Any) -> dict[str, Any]:
    return {"node_id": node_id, "node_title": title, "field": field_name, "before": copy.deepcopy(before),
            "after": copy.deepcopy(after)}


def _config_changes(node_id: str, title: str, before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [_change(node_id, title, key, before.get(key), after.get(key))
            for key in dict.fromkeys([*after, *before]) if before.get(key) != after.get(key)]


def _policy(level: str, config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """受管级别下新写出来的报告撰写节点，照门禁 G3 写明最严的三项：已经（按节点、再按全图默认）是这个
    值、或者更严的（claims: judge）不动。已发布级别不替探索运行拧紧，照缺省走。"""
    if level != "governed":
        return {}
    out = {}
    for key, want, _ in REPORT_POLICY:
        value = config.get(key)
        effective = defaults.get(key) if value in (None, "") else value
        if effective not in (want, *REPORT_POLICY_ALSO.get(key, ())):
            out[key] = want
    return out


def _report_from(contract: dict[str, Any], node_id: str) -> dict[str, Any]:
    """narrative 换成 report_from，放在 narrative 原来的位置，其余的键原样留着。原来写着的空 report_from（等于没声明，
    upgrade_candidates 也这么认）丢掉：它排在 narrative 后面的话，逐键照抄会把新值盖成空的，契约的核对就悄悄关了。"""
    out: dict[str, Any] = {}
    for key, value in contract.items():
        if key == "narrative":
            out["report_from"] = node_id
        elif key != "report_from":
            out[key] = copy.deepcopy(value)
    out.setdefault("report_from", node_id)
    return out


def _span_refs(text: str) -> list[tuple[str, list[Ref]]]:
    return [(m.group(0), _refs(m.group(1), template=False)) for m in _SPAN.finditer(text)]


def _echoed_evidence(graph: _Graph, consumer: str, text: str) -> list[str]:
    """指令里照搬了哪几段上游取数、口径卡的文字（原样的 {{ … }}）。"""
    out = []
    for span, refs in _span_refs(text):
        if any(src.type in _EVIDENCE_TYPES for ref in refs for src in graph.sources(consumer, ref)):
            out.append(span.strip())
    return list(dict.fromkeys(out))


def _retype(graph: dict[str, Any], spec: GraphSpec, match: Match, level: str) -> _Step:
    """R1 / R2：模型调用换成报告撰写。R1 另把契约的 narrative 换成 report_from。"""
    node = spec.node_map()[match.node_id]
    raw = _raw(graph, node.id)
    before = copy.deepcopy(_config(raw))
    text = "\n\n".join(p for p in (str(before.get("system") or "").strip(), str(before.get("prompt") or "").strip()) if p)
    after: dict[str, Any] = {"instructions": text} if text else {}
    after.update({k: copy.deepcopy(v) for k, v in before.items() if k not in _LLM_ONLY})
    after.update(_policy(level, after, spec.defaults))
    raw["type"] = NodeType.REPORT.value
    raw["data"]["config"] = after
    title = node.title
    step = _Step(label=(f"将「{title}」换成报告撰写，出具契约改为核对它的文档" if match.rule == "R1"
                        else f"将「{title}」换成报告撰写"),
                 retyped={(node.id, NodeType.LLM.value, NodeType.REPORT.value)}, created=True)
    step.changes = [_change(node.id, title, "type", NodeType.LLM.value, NodeType.REPORT.value),
                    *_config_changes(node.id, title, before, after)]
    step.ops = [{"op": "replace_node", "id": node.id,
                 "node": {"id": node.id, "type": NodeType.REPORT.value, "label": raw["data"].get("label") or "",
                          "config": copy.deepcopy(after)}}]
    dropped = [k for k in _LLM_ONLY if k not in ("prompt", "system") and before.get(k) not in (None, "", [], {})]
    if before.get("skills"):
        skills = before["skills"] if isinstance(before["skills"], list) else [before["skills"]]
        step.notes.append(_note(f"「{title}」挂载的 Skill {'、'.join(f'「{s}」' for s in skills)} 将不再加载：报告撰写节点"
                                f"不挂载 Skill，其中的写作要求如需保留，请写入「{field_label('instructions')}」",
                                match.rule, node.id, "warning"))
    if other := [k for k in dropped if k != "skills"]:
        named = "、".join(f"「{field_label(k, NodeType.LLM.value)}」" for k in other)
        step.notes.append(_note(f"「{title}」的{named}在报告撰写节点中不适用，已去掉", match.rule, node.id))
    if echoed := _echoed_evidence(_Graph(spec), node.id, text):
        step.notes.append(_note(
            f"「{title}」的「{field_label('instructions')}」中照搬了 {'、'.join(echoed)}：报告撰写节点会自行收集上游的"
            "口径卡指标和查询结果，数字由系统填入；直接写在要求中的数字，模型容易照抄成不带出处的数字（会被要求重写）。"
            "如确认不需要，请删除；升级未作删除", match.rule, node.id))
    if match.rule == "R1":
        for exit_id in match.exits:
            exit_raw = _raw(graph, exit_id)
            cfg = _config(exit_raw)
            was = copy.deepcopy(cfg.get("contract"))
            cfg["contract"] = _report_from(was or {}, node.id)
            step.changes.append(_change(exit_id, spec.node_map()[exit_id].title, "contract", was, cfg["contract"]))
            step.ops.append({"op": "update_node", "id": exit_id, "config": {"contract": copy.deepcopy(cfg["contract"])}})
    return step


def _fresh_id(graph: dict[str, Any], base: str) -> str:
    taken = {str(n.get("id")) for n in graph.get("nodes") or [] if isinstance(n, dict)}
    if base not in taken:
        return base
    return next(f"{base}_{i}" for i in range(2, len(taken) + 3) if f"{base}_{i}" not in taken)


def _answer_instructions(agent: GraphNode) -> str:
    """插进来的报告撰写写什么。问数据的图里问题在 input.question；别的图照搬 agent 的要求。agent 的原话附在
    后面当思路（和 Copilot 手册里问数据的写法一致），明说里面的数不能照抄。"""
    prompt = str(agent.config.get("prompt") or "").strip()
    if _QUESTION.search(prompt):
        head = "回答：{{ input.question }}"
    elif prompt:
        head = f"按下面的要求写给读的人看的回答（查数已经由上游「{agent.title}」做完，这里只写）：\n{prompt}"
    else:
        head = "根据证据目录里的查询结果，写一份简洁的回答"
    return (f"{head}\n\n「{agent.title}」查数时的分析过程附在下面，只作思路参考：里面的数字不能照抄，一律用引用标记"
            f"从证据目录里取。\n{{{{ nodes.{agent.id}.text }}}}")


def _agent_text_head(agent: GraphNode) -> re.Pattern[str]:
    """模板里「这个 agent 写的整段文字」的写法：nodes.X、nodes.X.text、vars.<assign_to>（没开 cite_fields 时）。"""
    nid = re.escape(agent.id)
    heads = [rf"nodes\s*(?:\.\s*{nid}|\[\s*['\"]{nid}['\"]\s*\])(?:\s*(?:\.\s*text|\[\s*['\"]text['\"]\s*\]))?"]
    var = str(agent.config.get("assign_to") or "").strip()
    if var and not _cited(agent):
        heads.append(rf"vars\s*(?:\.\s*{re.escape(var)}|\[\s*['\"]{re.escape(var)}['\"]\s*\])")
    return re.compile("|".join(f"(?:{h})" for h in heads))


def _swap_text(value: str, agent: GraphNode, report_id: str) -> str:
    head_re = _agent_text_head(agent)

    def repl(m: re.Match[str]) -> str:
        head, bar, rest = m.group(1).partition("|")
        if not head_re.fullmatch(head.strip()):
            return m.group(0)
        return f"{{{{ nodes.{report_id}.text{(' |' + rest.rstrip()) if bar else ''} }}}}"

    return _SPAN.sub(repl, value)


def _insert(graph: dict[str, Any], spec: GraphSpec, match: Match, level: str) -> _Step:
    """R3：agent 和出口之间插一个报告撰写。agent → 出口的连线改接成 agent → 报告 → 出口，出口字段里 agent 的
    原话改取报告的正文；没配字段的出口交的是最后一条消息，插了报告以后就是报告写的，不用改。"""
    nodes = spec.node_map()
    agent = nodes[match.node_id]
    raw_agent = _raw(graph, agent.id)
    rid = _fresh_id(graph, "report")
    config: dict[str, Any] = {"instructions": _answer_instructions(agent)}
    config.update({k: copy.deepcopy(agent.config[k]) for k in _MODEL_KEYS if agent.config.get(k) not in (None, "")})
    config.update(_policy(level, config, spec.defaults))
    here = raw_agent.get("position") or {}
    there = _raw(graph, match.exits[0]).get("position") or {}
    position = {"x": round((float(here.get("x") or 0) + float(there.get("x") or 0)) / 2, 1),
                "y": round((float(here.get("y") or 0) + float(there.get("y") or 0)) / 2 + 120, 1)}
    new = {"id": rid, "type": NodeType.REPORT.value, "position": position,
           "data": {"label": "报告撰写", "config": config}}
    graph["nodes"].insert(graph["nodes"].index(raw_agent) + 1, new)
    exits = [nodes[x] for x in match.exits]
    step = _Step(label=f"在「{agent.title}」和{_names(exits)}之间插入报告撰写，成果节点改为取报告的正文",
                 rewired={(agent.id, x.id) for x in exits}, created=True)
    step.changes.append(_change(rid, "报告撰写", "node", None,
                                {"id": rid, "type": NodeType.REPORT.value, "label": "报告撰写", "config": config}))
    step.ops.append({"op": "add_node", "node": {"id": rid, "type": NodeType.REPORT.value, "label": "报告撰写",
                                                "config": copy.deepcopy(config)}})

    def link(source: str, target: str) -> dict[str, Any]:
        return {"id": f"{source}:->{target}", "source": source, "target": target}

    converted: list[dict[str, Any]] = []      # 这一步从叙述模式改成 report_from 的契约
    graph["edges"] = [e for e in graph.get("edges") or []
                      if not (isinstance(e, dict) and e.get("source") == agent.id and e.get("target") in match.exits)]
    graph["edges"].append(link(agent.id, rid))
    step.changes.append(_change(None, "", "edge", None, {"source": agent.id, "target": rid}))
    step.ops.append({"op": "add_edge", "edge": {"source": agent.id, "target": rid}})
    for x in exits:
        graph["edges"].append(link(rid, x.id))
        step.changes.append(_change(None, "", "edge", {"source": agent.id, "target": x.id},
                                    {"source": rid, "target": x.id}))
        step.ops += [{"op": "remove_edge", "source": agent.id, "target": x.id},
                     {"op": "add_edge", "edge": {"source": rid, "target": x.id}}]
        cfg = _config(_raw(graph, x.id))
        updates: dict[str, Any] = {}
        fields = cfg.get("fields")
        if isinstance(fields, list):
            swapped = [{**f, "value": _swap_text(f["value"], agent, rid)}
                       if isinstance(f, dict) and isinstance(f.get("value"), str) else f for f in fields]
            if swapped != fields:
                step.changes.append(_change(x.id, x.title, "fields", fields, swapped))
                updates["fields"] = swapped
                cfg["fields"] = swapped
                exact = f"{{{{ nodes.{rid}.text }}}}"
                mixed = [str(f.get("name")) for f, old in zip(swapped, fields)
                         if isinstance(f, dict) and f != old and f.get("value", "").strip() != exact]
                if mixed:
                    step.notes.append(_note(
                        f"「{x.title}」的成果字段{'、'.join(f'「{n}」' for n in mixed)}在报告正文前后还拼接了其他内容，"
                        f"无法逐段对应证据。如需每个数字都可查看出处，字段中只放 {exact}", "R3", x.id))
        contract = cfg.get("contract")
        narrative = contract.get("narrative") if isinstance(contract, dict) else None
        if isinstance(narrative, str) and agent.id in {
                n.id for n in _writers(_Graph(spec), x.id, _refs(narrative, template=True), (NodeType.AGENT,))}:
            was = copy.deepcopy(contract)
            cfg["contract"] = _report_from(contract, rid)
            converted.append(cfg["contract"])
            step.changes.append(_change(x.id, x.title, "contract", was, cfg["contract"]))
            updates["contract"] = cfg["contract"]
        if updates:
            step.ops.append({"op": "update_node", "id": x.id, "config": copy.deepcopy(updates)})
    if note := _cells_note(spec, exits, converted, level):
        step.notes.append(_note(note[0], "R3", rid, note[1]))
    return step


def _cells_note(spec: GraphSpec, exits: list[GraphNode], converted: list[dict[str, Any]],
                level: str) -> tuple[str, str] | None:
    """插进来的报告撰写会直接引用查询单元格，受管级别的正式运行只在出口契约写了 cells: true 时才认
    （governance.cells_allowed）。已经写了的不提；按受管级别升级的，或者把原有契约改成了 report_from、图上又没有
    口径卡可走的，说成警告：不写的话报告里的数一个都引用不了，报告节点每次正式运行都会失败。"""
    if converted and all(c.get("cells") is True for c in converted):
        return None
    where = _names(exits)
    choose = (f"二选一：在{where}的出具契约中开启「单元格引用」；或改用口径卡，把要写的数字登记为指标，报告只引用"
              "指标。选择哪一种需要由你决定，升级未作修改")
    cards = any(n.type == NodeType.METRICS for n in spec.nodes)
    if level == "governed" or (converted and not cards):
        lead = "按受管级别" if level == "governed" else "当前（已发布级别）不受影响；今后按受管级别发布时"
        numbers = f"「{field_label('numbers')}」为「{option_label('numbers', 'strict')}」"
        violation = f"「{field_label('on_violation')}」为「{option_label('on_violation', 'fail')}」"
        return (f"报告会直接引用查询单元格，{lead}，正式运行只在成果节点的出具契约开启「单元格引用」时才接受单元格引用："
                f"未开启时报告中的数字都无法引用，报告撰写节点（{numbers}、{violation}）每次正式运行都会失败。"
                + choose, "warning")
    return ("报告会直接引用查询单元格。探索运行和已发布级别不受影响；如需按受管级别正式出具，请在成果节点的出具契约中开启"
            "「单元格引用」，或改用口径卡：把要写的数字登记为指标，报告只引用指标。选择哪一种需要由你决定，升级未添加出具契约",
            "info")


def _whole_var_readers(spec: GraphSpec, var: str) -> list[tuple[GraphNode, str]]:
    """模板里把 vars.<var> 整个拿来用的地方：(节点, 原样的 {{ … }})。取字段的（vars.kpi.gmv）不算。"""
    head_re = re.compile(rf"vars\s*(?:\.\s*{re.escape(var)}|\[\s*['\"]{re.escape(var)}['\"]\s*\])")

    def strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return []

    out: dict[tuple[str, str], tuple[GraphNode, str]] = {}
    for n in spec.nodes:
        for text in strings(n.config):
            for m in _SPAN.finditer(text):
                if head_re.fullmatch(m.group(1).partition("|")[0].strip()):
                    out.setdefault((n.id, m.group(0).strip()), (n, m.group(0).strip()))
    return list(out.values())


def _cite(graph: dict[str, Any], spec: GraphSpec, match: Match, level: str) -> _Step:
    """R4：打开 cite_fields。打开以后 assign_to 拿到的是按 Schema 核对过的对象，不再是文字：把它整个当文字
    用的地方说出来，不替人改。"""
    agent = spec.node_map()[match.node_id]
    cfg = _config(_raw(graph, agent.id))
    before = cfg.get("cite_fields")
    cfg["cite_fields"] = True
    step = _Step(label=f"开启「{agent.title}」的「按出处核对字段」：每个字段都核对到查询结果中的对应单元格")
    step.changes = [_change(agent.id, agent.title, "cite_fields", before, True)]
    step.ops = [{"op": "update_node", "id": agent.id, "config": {"cite_fields": True}}]
    var = str(agent.config.get("assign_to") or "").strip()
    for reader, span in _whole_var_readers(spec, var) if var else []:
        step.notes.append(_note(
            f"「{agent.title}」开启「按出处核对字段」后，vars.{var} 得到的是核对过的对象，而不再是文字：「{reader.title}」中的 "
            f"{span} 会显示为对象。如需原话，请改为 {{{{ nodes.{agent.id}.text }}}}", "R4", reader.id, "warning"))
    return step


_APPLY: dict[str, Callable[[dict[str, Any], GraphSpec, Match, str], _Step]] = {
    "R1": _retype, "R2": _retype, "R3": _insert, "R4": _cite}


def compute_feeders(spec: GraphSpec) -> list[tuple[GraphNode, list[GraphNode]]]:
    """喂口径卡、角色又不是取数（evidence_role 不是 source）的沙箱代码：(代码节点, 读它的口径卡)。
    R5 只建议；assist 时交给 Copilot 看是不是纯算术。"""
    graph = _Graph(spec)
    found: dict[str, tuple[GraphNode, list[GraphNode]]] = {}
    for card in spec.nodes:
        if card.type != NodeType.METRICS:
            continue
        for feeder in _card_feeders(graph, card):
            if feeder.type == NodeType.CODE and feeder.config.get("evidence_role") != "source":
                found.setdefault(feeder.id, (feeder, []))[1].append(card)
    return list(found.values())


def metric_feeders(spec: GraphSpec, card: GraphNode) -> list[tuple[int, GraphNode]]:
    """口径卡每个指标追到头读的是谁：(指标下标, 来源节点)。和 compute_feeders 同一条追法（穿过整形、校验节点，
    governance._card_feeders），只是逐个指标追：code → 整形 → 口径卡的，指标记在这段代码名下。"""
    graph = _Graph(spec)
    metrics = card.config.get("metrics")
    out: list[tuple[int, GraphNode]] = []
    for i, metric in enumerate(metrics if isinstance(metrics, list) else []):
        one = card.model_copy(update={"data": card.data.model_copy(update={"config": {**card.config,
                                                                                    "metrics": [metric]}})})
        out += [(i, feeder) for feeder in _card_feeders(graph, one)]
    return out


def _r5_notes(spec: GraphSpec) -> list[dict[str, Any]]:
    return [_note(
        f"「{code.title}」（沙箱代码）的产出提供给了口径卡{_names(cards)}：若它负责取数（查库、调用接口、读文件），"
        "请把「证据角色」设为「取数」，口径卡读取时会记录出处；若负责计算，请把计算移到口径卡的表达式中（可交给助手"
        "把纯算术的代码改写为口径卡表达式）。它负责取数还是计算需要由你确认，升级未作修改", "R5", code.id)
        for code, cards in compute_feeders(spec)]


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def forbidden(before: dict[str, Any], after: dict[str, Any], *,
              retyped: set[tuple[str, str, str]] | frozenset[tuple[str, str, str]] = frozenset(),
              rewired: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset()) -> list[str]:
    """升级的禁止规则：和发布前修复同一套（删节点、删连线、删契约、放宽审批、关 strict、改级别…一律拦），
    只放过这一步自己点名的换类型（模型调用 → 报告撰写）和改接的连线。"""
    return forbidden_changes(before, after, retyped=frozenset(retyped), rewired=frozenset(rewired))


def upgrade_for_evidence(graph: dict[str, Any] | GraphSpec, *, level: str = "published") -> dict[str, Any]:
    """按 R1–R4 改写，R5 只给建议。纯函数，不动传进来的图；同一张图升级两次，第二次什么都不改。

    level 是复核门禁用的发布级别：受管级别下新写出来的报告撰写节点另写明门禁 G3 要的三项。

    返回 {graph, changes, ops, notes, applied, rejected}：
    - changes：[{fix_id, rule, label, node_id, node_title, field, before, after}]；field 为 type 是节点换了类型，
      node 是新插入的节点（after 是节点），edge 是连线（before / after 是 {source, target}，改接的两边都有）
    - ops：形状同 Copilot 的操作流，另有 replace_node {id, node}（换类型，node 是整个新节点）
    - notes：[{text, rule, node_id, level}]，不改、只建议的，以及看着像旧结构、但没法确定怎么改的
    - applied：采用的 "R1:<节点 id>"…；rejected：[{fix_id, reason}]，碰了禁止规则或者会冒出新 error 的
    """
    if level not in LEVELS:
        raise ValueError(f"发布级别只能是「已发布」或「受管」，当前为「{level}」")
    raw = graph.model_dump(mode="json") if isinstance(graph, GraphSpec) else graph
    current = copy.deepcopy(raw)
    spec = GraphSpec.model_validate(current)
    issues = publish_issues(spec, level=level)
    changes: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    applied: list[str] = []
    rejected: list[dict[str, Any]] = []
    created: str | None = None      # 第一处写出报告撰写节点的规则
    for rule in RULES:
        tried: set[str] = set()
        while True:
            # 每一步之后重新认：前一步可能改了后一步要看的结构（插了报告的出口不再读 agent 的原话）
            match = next((m for m in upgrade_candidates(spec) if m.rule == rule and m.node_id not in tried), None)
            if match is None:
                break
            tried.add(match.node_id)
            if match.blocked:
                # 一个说不准的叙述会挡下拼进去的每个模型调用，原因只说一遍
                if not any(n["text"] == match.blocked for n in notes):
                    notes.append(_note(match.blocked, rule, match.node_id, "warning"))
                continue
            notes += [_note(why, rule, match.node_id, "warning") for why in match.skipped]
            fix_id = f"{rule}:{match.node_id}"
            trial = copy.deepcopy(current)
            step = _APPLY[rule](trial, spec, match, level)
            try:
                trial_spec = GraphSpec.model_validate(trial)
            except Exception:  # noqa: BLE001 - 规则把图改坏了：丢弃，不交给人一张存不回去的图
                rejected.append({"fix_id": fix_id, "reason": "修改后的工作流结构无法解析，这一步未采用"})
                continue
            trial_issues = publish_issues(trial_spec, level=level)
            why = "；".join(forbidden(current, trial, retyped=step.retyped, rewired=step.rewired)) \
                or worse(issues, trial_issues)
            if why:
                rejected.append({"fix_id": fix_id, "reason": why})
                notes.append(_note(f"{step.label}：未采用，{why}", rule, match.node_id, "warning"))
                continue
            current, spec, issues = trial, trial_spec, trial_issues
            applied.append(fix_id)
            changes += [{"fix_id": fix_id, "rule": rule, "label": step.label, **c} for c in step.changes]
            ops += step.ops
            notes += step.notes
            created = created or (rule if step.created else None)
    notes += _r5_notes(spec)
    if created and level != "governed":
        notes.append(_note(
            f"报告撰写节点重写后仍有无出处的数字时：探索运行正常产出，并在报告中标注；正式运行中「{field_label('on_violation')}」"
            f"默认为「{option_label('on_violation', 'fail')}」，节点会失败。如需先查看标注，请将「{field_label('on_violation')}」"
            f"设为「{option_label('on_violation', 'flag')}」", created, None))
    return {"graph": current, "changes": changes, "ops": ops, "notes": notes, "applied": applied, "rejected": rejected}
