from __future__ import annotations

import math
import re
from collections import defaultdict
from enum import StrEnum
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.engine.labels import TYPE_LABEL, field_label, judge_label, option_label, q, type_label  # noqa: F401


class NodeType(StrEnum):
    """画布上可用的节点类型。新增类型 = 加一个枚举值 + 一个执行器 + 一个前端渲染。"""

    INPUT = "input"
    OUTPUT = "output"

    LLM = "llm"  # 单次模型调用
    AGENT = "agent"  # 带工具循环的 ReAct agent
    SUPERVISOR = "supervisor"  # 多 agent 调度

    TOOL = "tool"  # 直接调用一个工具
    CODE = "code"  # 沙箱里跑代码
    MERGE = "merge"  # 合并查询：几次查询的完整结果在库外按键合并（engine/merge_query.py）

    BRANCH = "branch"  # 条件分支
    LOOP = "loop"  # 循环 / 迭代
    SUBGRAPH = "subgraph"  # 嵌套另一个工作流

    MEMORY = "memory"  # 长期记忆读写
    RETRIEVE = "retrieve"  # 知识库检索
    TRANSFORM = "transform"  # 数据整形

    HUMAN = "human"  # 人工介入
    VALIDATE = "validate"  # 结构化校验 + 自动重试
    METRICS = "metrics"  # 口径卡：受控指标集，叙述层唯一合法的数字来源
    REPORT = "report"  # 报告撰写：模型只写引用标记，数字由系统从口径卡取出来渲染


# 节点类型在画布上的叫法（TYPE_LABEL / type_label）从 labels.py 导入：报错和门禁文案用它，
# 而不是枚举值——用户在界面上从没见过 supervisor、llm 这些词


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeData(BaseModel):
    """节点上的可编辑内容。config 保持松散 dict，各执行器自己解析成强类型模型，
    这样前端加字段不会让整张图反序列化失败。"""

    label: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class GraphNode(BaseModel):
    id: str
    type: NodeType
    position: Position = Field(default_factory=Position)
    data: NodeData = Field(default_factory=NodeData)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not v or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"节点 ID 只能包含字母、数字、下划线和连字符，当前为「{v}」")
        # LangGraph 保留了这两个名字
        if v in ("__start__", "__end__"):
            raise ValueError(f"「{v}」是保留的节点 ID，请换一个")
        return v

    @property
    def config(self) -> dict[str, Any]:
        return self.data.config

    @property
    def title(self) -> str:
        return self.data.label or self.id


class GraphEdge(BaseModel):
    id: str = ""
    source: str
    target: str
    # 分支节点用 sourceHandle 区分走哪条路："true"/"false" 或自定义 case key
    sourceHandle: str | None = None
    targetHandle: str | None = None
    label: str = ""

    @model_validator(mode="after")
    def _fill_id(self) -> "GraphEdge":
        if not self.id:
            self.id = f"{self.source}:{self.sourceHandle or ''}->{self.target}"
        return self

    @property
    def branch_key(self) -> str | None:
        return self.sourceHandle if self.sourceHandle not in (None, "out", "source") else None


class GraphSpec(BaseModel):
    """一整张可执行的图。前端 React Flow 的画布状态直接序列化成它。"""

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    viewport: dict[str, Any] = Field(default_factory=dict)
    # 图级别的默认值，节点没配 provider/model 时回退到这里
    defaults: dict[str, Any] = Field(default_factory=dict)

    def node_map(self) -> dict[str, GraphNode]:
        return {n.id: n for n in self.nodes}

    def outgoing(self, node_id: str) -> list[GraphEdge]:
        return [e for e in self.edges if e.source == node_id]

    def incoming(self, node_id: str) -> list[GraphEdge]:
        return [e for e in self.edges if e.target == node_id]

    def entry_nodes(self) -> list[GraphNode]:
        """入口 = input 节点；没有 input 节点时退化为所有无入边的节点。"""
        inputs = [n for n in self.nodes if n.type == NodeType.INPUT]
        if inputs:
            return inputs
        return [n for n in self.nodes if not self.incoming(n.id)]

    def terminal_nodes(self) -> list[GraphNode]:
        outputs = [n for n in self.nodes if n.type == NodeType.OUTPUT]
        if outputs:
            return outputs
        return [n for n in self.nodes if not self.outgoing(n.id)]


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    #: info 是建议、不是问题（evidence.upgrade_available「可以升级为可追溯结构」）：不挡运行、不挡发布，
    #: 自查、门禁、修复复核都只数 error
    level: Literal["error", "warning", "info"] = "error"
    node_id: str | None = None
    edge_id: str | None = None
    message: str
    #: 节点配置里出问题的那个字段：prompt、cases[1].condition、agents[0].system。
    #: 检查器靠它把问题落到具体的输入框下面；说不清是哪个字段的（连线、图级）为空
    field: str | None = None
    #: 机器认的问题编号（governed.agent_approval_never、contract.required_missing…）。发布前自动修复
    #: 靠它认出是哪一类问题；文案会改，编号不改。没有编号的问题只能人看着修
    code: str | None = None
    #: 这条问题对应的修复 id（engine/autofix.py 算出来的）。只在发布检查的结果里填
    fix: str | None = None
    #: 发布门禁里基于数据目录的 SQL 检查（governance._lint_sql）：这条检查本来的级别（error / warning）。level 说的是
    #: 拦不拦（已发布档一律 warning），前端要按它说清「错误级问题，已发布只提醒、受管会拦」。别的问题没有这一项，
    #: 序列化时也不出现
    sql_level: Literal["error", "warning"] | None = Field(default=None, exclude_if=lambda v: v is None)


class ValidationResult(BaseModel):
    ok: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)

    def add(self, message: str, *, level: str = "error", node_id: str | None = None,
            edge_id: str | None = None, field: str | None = None, code: str | None = None,
            sql_level: str | None = None) -> None:
        self.issues.append(
            ValidationIssue(level=level, message=message, node_id=node_id,  # type: ignore[arg-type]
                            edge_id=edge_id, field=field, code=code, sql_level=sql_level)  # type: ignore[arg-type]
        )
        if level == "error":
            self.ok = False


def topology_of(spec: GraphSpec) -> tuple[tuple, tuple]:
    """图的骨架：节点的 id 与类型、加上所有的边。**不含任何配置。**

    断点续跑要用它。LangGraph 的 checkpoint 是按节点名存的（编译时
    `builder.add_node(node.id, …)`），而节点配置是运行时从 spec 现读的
    （`NodeContext.cfg`）。所以拿一张只改了配置的图去续，checkpoint 对得上，
    前面跑过的节点结果照样能用；一旦增删节点或改连线，checkpoint 里的
    通道状态就和新图对不上了——续下去跑出来的是一份似是而非的结果，
    比直接报错糟得多。

    这正是 resume 那边踩过的坑：不看有没有 checkpoint 就发 Command，
    LangGraph 拿空 state 从 START 重跑，最后还落成 succeeded。
    """
    return (
        tuple(sorted((n.id, str(n.type)) for n in spec.nodes)),
        tuple(sorted((e.source, e.target, e.sourceHandle or "") for e in spec.edges)),
    )


def back_edges(
    node_ids: Iterable[str], edges: Iterable[GraphEdge], *, roots: Iterable[str] = (),
) -> set[tuple[str, str, str]]:
    """DFS 森林里的回边，以 (source, target, sourceHandle) 表示。剔掉它们，剩下的必然无环。

    哪条边算"回边"取决于 DFS 从哪里出发。排版不在乎，按节点顺序起算就行；
    执行语义在乎——得从入口（roots）起算，否则循环体里的一条普通前向边可能被
    误判成回边。两端有一端不在 node_ids 里的悬空边直接忽略。

    迭代式而不是递归：一张几百个节点的图不该因为递归深度挂掉。
    """
    WHITE, GREY, BLACK = 0, 1, 2
    state = dict.fromkeys(node_ids, WHITE)
    adj: dict[str, list[GraphEdge]] = defaultdict(list)
    for e in edges:
        if e.source in state and e.target in state:
            adj[e.source].append(e)

    back: set[tuple[str, str, str]] = set()
    for root in [*roots, *state]:
        if state.get(root) != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = GREY
        while stack:
            node_id, i = stack[-1]
            out = adj[node_id]
            if i >= len(out):
                state[node_id] = BLACK
                stack.pop()
                continue
            stack[-1] = (node_id, i + 1)
            edge = out[i]
            target = edge.target
            if state[target] == GREY:
                back.add((edge.source, edge.target, edge.sourceHandle or ""))
            elif state[target] == WHITE:
                state[target] = GREY
                stack.append((target, 0))
    return back


def loop_steps(spec: GraphSpec) -> int:
    """图里的循环按各自声明的轮数跑满，最多要走多少步（LangGraph 的 superstep）。

    整次运行的步数上限（max_graph_steps）防的是没人把关的环；loop 节点自己带着
    max_iterations，那是作者明说的轮数上限。以前两者打架时全局值悄悄赢：「测试 1」
    的 while 循环配了 1005 轮、每轮 2 步，跑到 100 轮上下就被 GraphRecursionError
    掐断——校验通过，跑到一半才死。runner 把这个数加在全局上限之上。

    取上界不求精确：一个节点最多跑 Π(包住它的各层循环的 max_iterations + 1) 次，
    逐个加起来。一步至少跑一个节点，所以步数只会比这少。
    """
    bodies = loop_bodies(spec)
    if not bodies:
        return 0
    loops = spec.node_map()
    runs: dict[str, int] = defaultdict(lambda: 1)
    for loop_id, body in bodies.items():
        try:
            cap = int(loops[loop_id].config.get("max_iterations", 10) or 10)
        except (TypeError, ValueError):
            cap = 10    # 运行到这个节点时同样会报错，这里不替它报
        for n in body | {loop_id}:
            runs[n] *= max(cap, 0) + 1
    return sum(runs.values())


def loop_bodies(spec: GraphSpec) -> dict[str, set[str]]:
    """每个循环节点的循环体（不含循环节点本身）。

    循环体 = 从 body 出口出发、不经过循环节点本身、又能绕回它的那些节点。
    没标出口的边路由时按 default 走，body 也会走它。
    """
    loops = [n for n in spec.nodes if n.type == NodeType.LOOP]
    if not loops:
        return {}
    succ: dict[str, list[str]] = defaultdict(list)
    pred: dict[str, list[str]] = defaultdict(list)
    for e in spec.edges:
        succ[e.source].append(e.target)
        pred[e.target].append(e.source)

    def reach(starts: Iterable[str], step: dict[str, list[str]], avoid: str) -> set[str]:
        seen: set[str] = set()
        stack = [s for s in starts if s != avoid]
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(x for x in step[n] if x != avoid)
        return seen

    out: dict[str, set[str]] = {}
    for loop in loops:
        entries = [e.target for e in spec.outgoing(loop.id) if e.sourceHandle != "done"]
        out[loop.id] = reach(entries, succ, loop.id) & reach(pred[loop.id], pred, loop.id)
    return out


def innermost_loops(spec: GraphSpec) -> dict[str, str]:
    """节点 -> 直接包住它的那一层循环。嵌套时取循环体最小的那个。"""
    bodies = loop_bodies(spec)
    out: dict[str, str] = {}
    for loop_id, body in sorted(bodies.items(), key=lambda kv: -len(kv[1])):
        for n in body:
            out[n] = loop_id    # 由大到小覆盖，最后留下的是最内层
    return out


def expression_fields(node: GraphNode) -> list[tuple[str, str, str]]:
    """这个节点上按表达式求值的字段：(给人看的名字, 原文, 字段路径)。

    跟着模式走：foreach 循环的 condition、模板模式整形的 expression 不参与求值，
    拿它们报错就是误报。
    """
    cfg = node.config
    out: list[tuple[str, str, str]] = []

    def add(label: str, value: Any, field: str) -> None:
        if isinstance(value, str) and value.strip():
            out.append((label, value, field))

    add("跳过条件", cfg.get("skip_if"), "skip_if")
    if node.type == NodeType.BRANCH and cfg.get("mode", "expression") == "expression":
        for i, case in enumerate(cfg.get("cases") or []):
            case = case or {}
            add(f"分支「{case.get('label') or case.get('key') or '?'}」的条件", case.get("condition"),
                f"cases[{i}].condition")
    elif node.type == NodeType.LOOP and cfg.get("mode", "foreach") == "while":
        add("循环条件", cfg.get("condition"), "condition")
    elif node.type == NodeType.TRANSFORM and cfg.get("mode", "expression") == "expression":
        add("整形表达式", cfg.get("expression"), "expression")
    elif node.type == NodeType.METRICS:
        for i, d in enumerate(cfg.get("metrics") or []):
            d = d or {}
            add(f"指标「{d.get('id') or '?'}」的表达式", d.get("expression"), f"metrics[{i}].expression")
    return out


# 代码节点可能往 stdout 写东西的迹象。宁可漏判不可误判：这条是 error 级、会挡住运行，
# 所以间接的写法（子进程、exec、附带文件里的 print）都算。整行只有一个 {{ }} 的，
# 那行代码是运行时从上游拿的（⑤号示例整段就是 {{ vars.code }}），看不见，不猜；
# 而 `state = {{ vars.state | json }}` 这种只是填一个值，照查
_MAY_WRITE_STDOUT = re.compile(
    r"\b(print|pprint|exec)\s*\(|\bstdout\b|\bos\.(system|write|popen)\b|\bsubprocess\b"
    r"|^\s*\{\{.*\}\}\s*$", re.M)


def _check_case_keys(node: GraphNode, cases: list[Any], result: ValidationResult) -> None:
    """分支出口的 id 就是 case 的 key，所以 key 得唯一、非空，而且不能占用兜底出口的名字。

    default 只报警告：现存的 ⑥ 模板就写着 key=default、条件为 true，运行语义上等价于
    兜底，是自洽的；升成 error 会让它和所有从它复制出来的图直接不能跑。可它在画布上
    会和「其他」撞成同一个出口，跑完两条一起点亮——得说出来。
    重复和空 key 是真坏了：路由只认 key，重复的那个永远轮不到，空的根本连不出边。
    """
    seen: set[str] = set()
    for i, case in enumerate(cases, start=1):
        case = case or {}
        key = str(case.get("key") or "").strip()
        name = case.get("label") or key or f"第 {i} 个"
        field = f"cases[{i - 1}].key"
        if not key:
            result.add(f"「条件分支」的分支「{name}」没有标识，无法连线，也不会被选中",
                       node_id=node.id, field=field)
            continue
        if key == "default":
            result.add(
                f"分支「{name}」的标识用了「default」：这是「其他」出口的保留名，两者会合并成"
                "同一个出口，运行后无法区分走的是哪一条。请换一个标识，例如 team",
                level="warning", node_id=node.id, field=field,
            )
        elif key in seen:
            result.add(f"「条件分支」中有两个分支的标识都是「{key}」：路由只按标识区分，"
                       "后一个分支不会被选中。请换一个不重复的标识", node_id=node.id, field=field)
        seen.add(key)


def _ancestors(spec: GraphSpec, node_id: str) -> set[str]:
    """沿连线往回走能到的所有节点（不含自己）。循环里的节点彼此都算上游。"""
    parents: dict[str, list[str]] = defaultdict(list)
    for e in spec.edges:
        parents[e.target].append(e.source)
    seen: set[str] = set()
    stack = list(parents[node_id])
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(parents[cur])
    seen.discard(node_id)
    return seen


def _check_report_sources(node: GraphNode, spec: GraphSpec, result: ValidationResult) -> None:
    """报告的 metrics_from 只能指向它上游的口径卡。

    指错了运行时不会报错：目录里就是没有那张卡的指标，模型要么写不出数、要么被
    判成引用不存在——在画图时说破比跑完再猜原因便宜得多。
    """
    sources = node.config.get("metrics_from")
    if sources in (None, "", []):
        return      # 不写就取所有上游口径卡
    if isinstance(sources, str):
        sources = [sources]
    code = "report.metrics_from_invalid"
    if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
        result.add("「报告撰写」的「指标来自」需要选择口径卡节点", node_id=node.id,
                   field="metrics_from", code=code)
        return
    nodes = spec.node_map()
    upstream = _ancestors(spec, node.id)
    for src in sources:
        target = nodes.get(src)
        if target is None:
            result.add(f"「报告撰写」的「指标来自」选择了「{src}」，但工作流中不存在这个节点",
                       node_id=node.id, field="metrics_from", code=code)
        elif target.type != NodeType.METRICS:
            result.add(f"「报告撰写」的「指标来自」选择了「{target.title}」，它是「{type_label(target.type)}」"
                       "节点，不是口径卡；报告中的指标只能来自口径卡",
                       node_id=node.id, field="metrics_from", code=code)
        elif src not in upstream:
            result.add(f"「报告撰写」的「指标来自」选择的口径卡「{target.title}」不在它的上游：撰写报告时"
                       "该口径卡尚未计算，报告无法引用它的指标。请把口径卡连到报告撰写节点之前",
                       node_id=node.id, field="metrics_from", code=code)


def _produces_query(node: GraphNode) -> bool:
    """这个节点的产出是一份存成快照的查询结果：调用数据库查询工具的「调用工具」节点，或另一个合并查询。"""
    if node.type == NodeType.MERGE:
        return True
    return node.type == NodeType.TOOL and str(node.config.get("tool") or "").startswith("db_query__")


def _check_merge(node: GraphNode, spec: GraphSpec, result: ValidationResult) -> None:
    """合并查询的输入和合并 SQL。规则和执行时（engine/merge_query.py）同一套，画图时就说破，不等运行到这一步才失败。

    输入只认能稳定取到一份查询快照的节点。Agent 查过的库不行：一次循环里可能查好几次，取哪一次说不准，
    拿错一次合并出来的数照样像模像样——所以报 error，并说清楚把那条查询单独放进「调用工具」节点。
    """
    from app.engine.merge_query import MAX_INPUTS, alias_problem, sql_problem

    cfg = node.config
    inputs = cfg.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        result.add("「合并查询」还没有配置输入：至少选择一个上游的查询节点，并给它起一个别名（合并 SQL 里的表名）",
                   node_id=node.id, field="inputs", code="merge.inputs_missing")
    else:
        if len(inputs) > MAX_INPUTS:
            result.add(f"「合并查询」最多 {MAX_INPUTS} 个输入，这里有 {len(inputs)} 个。请先在源库里合并一部分",
                       node_id=node.id, field="inputs", code="merge.inputs_missing")
        nodes = spec.node_map()
        upstream = _ancestors(spec, node.id)
        seen: dict[str, str] = {}
        for alias, source in inputs.items():
            field = f"inputs.{alias}"
            if problem := alias_problem(alias):
                result.add(f"「合并查询」的{problem}", node_id=node.id, field=field, code="merge.alias_invalid")
            elif alias.lower() in seen:
                result.add(f"「合并查询」的别名「{seen[alias.lower()]}」和「{alias}」只差大小写：SQLite 的表名不分大小写，"
                           "会被当成同一张表。请换一个别名", node_id=node.id, field=field, code="merge.alias_invalid")
            else:
                seen[alias.lower()] = alias
            target = nodes.get(source) if isinstance(source, str) else None
            who = f"「合并查询」的输入「{alias}」"
            if not isinstance(source, str) or not source:
                result.add(f"{who}还没有选择节点", node_id=node.id, field=field, code="merge.input_invalid")
            elif target is None:
                result.add(f"{who}选择了「{source}」，但工作流中不存在这个节点", node_id=node.id, field=field,
                           code="merge.input_invalid")
            elif target.type == NodeType.AGENT:
                result.add(f"{who}选择了「{target.title}」（Agent）：Agent 可能查询多次，无法确定合并哪一次的结果。"
                           "请把要合并的那条查询单独放进「调用工具」节点（选择数据库查询工具），再把它选作输入",
                           node_id=node.id, field=field, code="merge.input_invalid")
            elif target.type == NodeType.TOOL and not _produces_query(target):
                result.add(f"{who}选择的「{target.title}」调用的不是数据库查询工具：合并查询只能合并数据库查询的结果",
                           node_id=node.id, field=field, code="merge.input_invalid")
            elif not _produces_query(target):
                result.add(f"{who}选择的「{target.title}」是「{type_label(target.type)}」节点，不产出查询结果。"
                           "输入只能是调用数据库查询工具的「调用工具」节点，或另一个「合并查询」节点",
                           node_id=node.id, field=field, code="merge.input_invalid")
            elif source not in upstream:
                result.add(f"{who}选择的「{target.title}」不在合并查询的上游：合并时它还没有运行。请把它连到合并查询之前",
                           node_id=node.id, field=field, code="merge.input_invalid")
    sql = cfg.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        result.add("「合并查询」还没有填写合并 SQL", node_id=node.id, field="sql", code="merge.sql_invalid")
    elif problem := sql_problem(sql):
        result.add(problem, node_id=node.id, field="sql", code="merge.sql_invalid")


#: 报告撰写节点的结论句策略（claims）。off：结论句不参与判档（默认，保持以前的行为）；
#: require_citation：没挂引用的结论句计入缺口、按出具档位降档。这两档是确定性的（发布前自动修复
#: 给人选的也只有这两档）
REPORT_CLAIMS = ("off", "require_citation")
#: judge：结论句由另一个模型按证据逐句判断（engine/judge.py），子配置写在节点的 judge 里
CLAIMS_JUDGE = "judge"
CLAIMS_VALUES = (*REPORT_CLAIMS, CLAIMS_JUDGE)
#: judge 子配置能写的键。三个上限写 null 表示不限；没写的键运行时取设置里的默认（设置 → judge 一组）
JUDGE_KEYS = ("provider", "model", "max_claims", "max_cost_usd", "timeout_s", "rewrite_once", "on_unsupported")
#: 证据相矛盾时正式运行怎么判档：degrade 降档（默认）/ withhold 不予出具。探索运行只标注
JUDGE_ON_UNSUPPORTED = ("degrade", "withhold")


def claims_problem(value: Any) -> str | None:
    """claims 的取值有什么问题，没问题返回 None。校验和报告撰写节点执行时报的是同一句话。"""
    if value in (None, "") or (isinstance(value, str) and value in CLAIMS_VALUES):
        return None
    allowed = [f"「{option_label('claims', v)}」" for v in CLAIMS_VALUES]
    return (f"「{field_label('claims')}」只能选{'、'.join(allowed[:-1])}或{allowed[-1]}，当前为「{value}」")


def _positive(value: Any, *, integer: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return False
    return not integer or int(value) == value


def judge_problems(value: Any) -> list[tuple[str, str]]:
    """claims: judge 的子配置有什么问题：[(字段, 人话)]，没问题返回 []。校验和节点执行时报的是同一句话。

    上限写 null 就是不限（和没写不一样：没写取设置里的默认）。键写错了也报：写成 max_cost 的预算会被
    悄悄当成没写、按默认值花钱。
    """
    if value in (None, ""):
        return []
    if not isinstance(value, dict):
        return [("judge", f"「{field_label('judge')}」设置格式有误，需要是一个对象"
                          f'（例如 {{"max_cost_usd": 0.05, "max_claims": 40}}），当前为「{value}」')]
    out: list[tuple[str, str]] = []
    for key, item in value.items():
        field = f"judge.{key}"
        if key not in JUDGE_KEYS:
            known = "、".join(f"「{judge_label(k)}」" for k in JUDGE_KEYS)
            out.append((field, f"「{field_label('judge')}」中无法识别「{key}」，可设置的项为：{known}"))
        elif key in ("provider", "model") and item is not None and not isinstance(item, str):
            what = "模型接入的名称" if key == "provider" else "模型 ID"
            out.append((field, f"「{judge_label(key)}」需要填写{what}，留空则使用设置中的「证据裁判」模型，"
                               f"当前为「{item}」"))
        elif key == "max_claims" and item is not None and not _positive(item, integer=True):
            out.append((field, f"「{judge_label(key)}」需要填写正整数（每份报告最多裁判的句数），或选择不限，"
                               f"当前为「{item}」"))
        elif key == "max_cost_usd" and item is not None and not _positive(item):
            out.append((field, f"「{judge_label(key)}」需要填写大于 0 的金额（每份报告的裁判费用上限），或选择不限，"
                               f"当前为「{item}」"))
        elif key == "timeout_s" and item is not None and not _positive(item):
            out.append((field, f"「{judge_label(key)}」需要填写大于 0 的秒数（每份报告的裁判时长上限），或选择不限，"
                               f"当前为「{item}」"))
        elif key == "rewrite_once" and not isinstance(item, bool):
            out.append((field, f"「{judge_label(key)}」只能是开启或关闭，当前为「{item}」"))
        elif key == "on_unsupported" and item not in JUDGE_ON_UNSUPPORTED:
            allowed = "或".join(f"「{option_label(key, v)}」" for v in JUDGE_ON_UNSUPPORTED)
            out.append((field, f"「{judge_label(key)}」只能选{allowed}，当前为「{item}」"))
    return out


def _check_report_claims(node: GraphNode, spec: GraphSpec, result: ValidationResult) -> None:
    """claims 写错了挡住运行：执行时报告节点照样会报这一句，与其跑完取数、算完口径卡才死在写报告
    那一步，不如在画图时说破。claims 是 judge 时再查 judge 子配置的形状；裁判模型和写作模型写成
    同一个的给警告——等于自己审自己。

    和运行时同一个取值规则：节点上没写就取图级 defaults（NodeContext.cfg）。
    """
    def effective(key: str) -> Any:
        value = node.config.get(key)
        return spec.defaults.get(key) if value in (None, "") else value

    value = effective("claims")
    if problem := claims_problem(value):
        result.add(problem, node_id=node.id, field="claims", code="report.claims_invalid")
    if value != CLAIMS_JUDGE:
        return
    judge = effective("judge")
    for field, message in judge_problems(judge):
        result.add(message, node_id=node.id, field=field, code="report.judge_invalid")
    judge_model = judge.get("model") if isinstance(judge, dict) else None
    if isinstance(judge_model, str) and judge_model and judge_model == effective("model"):
        result.add(f"裁判模型与写作模型相同（「{judge_model}」），难以发现写作模型自身的错误。"
                   f"请在「{field_label('judge')}」中选择另一个模型", level="warning", node_id=node.id, field="judge.model",
                   code="report.judge_same_model")


def contract_needs_metrics(contract: dict[str, Any]) -> bool:
    """出具契约要不要写 metrics_from（指标来自哪几张口径卡）。

    旧写法按数值回指叙述，没有口径卡就无从回指，一定要写。引用模式（report_from）下，数字回指靠
    报告里的引用标记：只引查询单元格（cells）的问数据图根本没有口径卡，不该逼它写；契约里列了
    required / expected 指标的，指标总得有个来处，照样要写。运行时 io.py 也是这么判的。
    """
    if contract.get("report_from") in (None, ""):
        return True
    return bool(contract.get("required") or contract.get("expected"))


def _check_report_from(node: GraphNode, report_from: Any, spec: GraphSpec,
                       result: ValidationResult) -> None:
    """引用模式的契约：report_from 必须是出口上游的报告撰写节点。"""
    field, code = "contract.report_from", "contract.report_from_invalid"
    if not isinstance(report_from, str):
        result.add("出具契约的「报告来自」需要选择一个「报告撰写」节点", node_id=node.id, field=field, code=code)
        return
    target = spec.node_map().get(report_from)
    if target is None:
        result.add(f"出具契约的「报告来自」选择了「{report_from}」，但工作流中不存在这个节点",
                   node_id=node.id, field=field, code=code)
    elif target.type != NodeType.REPORT:
        result.add(f"出具契约的「报告来自」选择了「{target.title}」，它是「{type_label(target.type)}」节点，"
                   "不是「报告撰写」节点：出具契约只核对报告撰写节点产出的文档",
                   node_id=node.id, field=field, code=code)
    elif report_from not in _ancestors(spec, node.id):
        result.add(f"出具契约的「报告来自」选择的「{target.title}」不在该成果节点的上游：核对时这份报告还没有写出。"
                   "请把报告撰写节点连到成果节点之前",
                   node_id=node.id, field=field, code=code)


def validate_graph(spec: GraphSpec) -> ValidationResult:
    """编译前的静态检查。错误会挡住运行，警告只在画布上提示。"""
    result = ValidationResult()
    nodes = spec.node_map()

    if not spec.nodes:
        result.add("画布上还没有节点，请先从节点库拖入一个节点")
        return result

    # 引用完整性
    for edge in spec.edges:
        if edge.source not in nodes:
            result.add(f"连线的起点节点「{edge.source}」不存在", edge_id=edge.id)
        if edge.target not in nodes:
            result.add(f"连线的终点节点「{edge.target}」不存在", edge_id=edge.id)

    if not spec.entry_nodes():
        result.add("找不到起始节点：每个节点都有上游连线，工作流中存在环路且没有起点")

    # 节点级必填项
    for node in spec.nodes:
        cfg = node.config
        if node.type == NodeType.BRANCH:
            cases = cfg.get("cases") or []
            if not cases:
                result.add("「条件分支」至少需要配置一个分支条件", node_id=node.id, field="cases")
            handles = {e.sourceHandle for e in spec.outgoing(node.id)}
            _check_case_keys(node, cases, result)
            for i, case in enumerate(cases):
                key = str((case or {}).get("key") or "").strip()
                if key and key != "default" and key not in handles:
                    result.add(
                        f"分支「{key}」还没有连线", level="warning", node_id=node.id,
                        field=f"cases[{i}].key",
                    )
            if "default" not in handles:
                result.add(
                    "建议为「条件分支」的「其他」出口添加连线，处理所有条件都不满足的情况",
                    level="warning",
                    node_id=node.id,
                    field="cases",
                )
        elif node.type == NodeType.TOOL:
            if not cfg.get("tool"):
                result.add("「调用工具」节点还没有选择工具", node_id=node.id, field="tool")
        elif node.type == NodeType.SUBGRAPH:
            if not cfg.get("workflow_id"):
                result.add("「子工作流」节点还没有选择要嵌套的工作流", node_id=node.id,
                           field="workflow_id")
        elif node.type == NodeType.VALIDATE:
            if not cfg.get("schema"):
                result.add("「结构校验」节点需要一个 JSON Schema", node_id=node.id, field="schema")
        elif node.type == NodeType.METRICS:
            defs = cfg.get("metrics") or []
            # 钉住别的工作流里的口径卡时，指标定义运行时从那一版里取，本地不用再写
            if not defs and not cfg.get("caliber_from"):
                result.add("口径卡还没有定义指标", node_id=node.id, field="metrics")
            for i, d in enumerate(defs):
                if not d.get("id") or not d.get("expression"):
                    result.add(f"指标定义缺少 ID 或表达式：{d.get('id') or '（空）'}",
                               node_id=node.id, field=f"metrics[{i}]")
        elif node.type == NodeType.REPORT:
            _check_report_sources(node, spec, result)
            _check_report_claims(node, spec, result)
        elif node.type == NodeType.MERGE:
            _check_merge(node, spec, result)
        elif node.type == NodeType.OUTPUT:
            contract = cfg.get("contract")
            if contract and not isinstance(contract, dict):
                # 字符串、列表、数字：后面的 .get 会崩，发布、发布检查、发起运行一起 500。
                # 报成 error，两档发布和运行前都挡住
                result.add("出具契约必须是一个 JSON 对象", node_id=node.id, field="contract",
                           code="contract.not_object")
            elif contract and not contract.get("metrics_from") and contract_needs_metrics(contract):
                result.add("出具契约缺少「指标来自」（提供指标的口径卡）",
                           node_id=node.id, field="contract.metrics_from", code="contract.metrics_from_missing")
            if isinstance(contract, dict) and contract.get("report_from") not in (None, ""):
                _check_report_from(node, contract["report_from"], spec, result)
        elif node.type == NodeType.LOOP:
            if not spec.outgoing(node.id):
                result.add("「循环」节点没有循环体", node_id=node.id)

        if node.type in (NodeType.LLM, NodeType.AGENT, NodeType.SUPERVISOR, NodeType.REPORT):
            if not (cfg.get("model") or spec.defaults.get("model")):
                result.add(
                    "没有指定模型，将使用默认模型",
                    level="warning",
                    node_id=node.id,
                    field="model",
                )

    # 表达式：用运行时同一个 parse_expression 先解析一遍。以前写错了只有跑到那一步
    # 才知道——前面的步骤白跑、钱白花，报错还停在半路（开发库里四次运行死在
    # "表达式里不允许出现 Set"，就是条件里套了 {{ }}）
    from app.engine.expressions import ExpressionError, parse_expression

    for node in spec.nodes:
        for label, text, field in expression_fields(node):
            try:
                _, unwrapped, unknown = parse_expression(text)
            except ExpressionError as e:
                result.add(f"{label}有误：{e}（原文：{text}）", node_id=node.id, field=field)
                continue
            if unwrapped:
                result.add(
                    f"{label}中的 {{{{ }}}} 是多余的：这里是表达式，不是模板，"
                    f"已按 {'、'.join(unwrapped)} 理解", level="warning", node_id=node.id,
                    field=field,
                )
            if unknown:
                result.add(
                    f"{label}中的 {'、'.join(unknown)} 不是可用的名称，运行时会取到空值"
                    "（变量需写完整路径，例如 vars.x、input.x、nodes.某节点.text）",
                    level="warning", node_id=node.id, field=field,
                )
        cfg = node.config
        if node.type == NodeType.LOOP and cfg.get("mode", "foreach") == "while" \
                and not str(cfg.get("condition") or "").strip():
            result.add("条件循环没有设置「继续条件」，循环体不会执行", level="warning", node_id=node.id,
                       field="condition")
        if node.type == NodeType.TRANSFORM and cfg.get("mode", "expression") == "expression" \
                and not str(cfg.get("expression") or "").strip():
            result.add("「数据整形」节点还没有填写表达式", node_id=node.id, field="expression")
        # assign_to 拿到的是 stdout。脚本最后一行写个裸表达式不会输出（那是 notebook
        # 的行为）——变量是空的，下游的循环条件一上来就不成立、成果也是空的，而整次
        # 运行照样"成功"。开发库里的「测试 1」就是这样：修好条件之后跑通了，结果为空
        if node.type == NodeType.CODE and cfg.get("assign_to") \
                and (cfg.get("language") or "python").lower() == "python":
            files = cfg.get("files")
            source = "\n".join([str(cfg.get("code") or ""),
                                 *(map(str, files.values()) if isinstance(files, dict) else ())])
            if not _MAY_WRITE_STDOUT.search(source):
                var = cfg["assign_to"]
                result.add(
                    f"代码把输出交给 vars.{var}，但代码中没有 print：Python 脚本最后一行的表达式"
                    f"不会自动输出，vars.{var} 将为空。请用 print 输出结果；如需向下游传递对象，"
                    "请使用 print(json.dumps(结果))", node_id=node.id, field="code",
                )

    _check_named_tools(spec, result)
    for issue in [*text_parse_issues(spec), *evidence_issues(spec)]:
        result.add(issue.message, level=issue.level, node_id=issue.node_id, field=issue.field, code=issue.code)

    # 旧结构（报告是模型调用写的、问数据的答案是 agent 的原话…）能一键升级成可追溯的：只给一条建议，
    # 画布上的快速修复据此打开升级预览。放在函数里导入：upgrade 反过来要用这里的 GraphSpec
    from app.engine.upgrade import HINT_CODE, upgrade_hint

    if hint := upgrade_hint(spec):
        result.add(hint, level="info", code=HINT_CODE)

    # 孤儿节点
    for node in spec.nodes:
        if node.type in (NodeType.INPUT, NodeType.OUTPUT):
            continue
        if not spec.incoming(node.id) and not spec.outgoing(node.id):
            result.add("节点没有任何连线，不会被执行", level="warning", node_id=node.id)

    # 变量问题也进校验结果：模板取不到值只会渲染成空字符串，不报错，
    # 所以拼错一个变量名在运行时是完全静默的——只能在这里说破。
    # 放在最后导入，避免 variables 反过来 import schema 形成环。
    from app.engine.variables import analyze as _analyze_vars

    for issue in _analyze_vars(spec).issues:
        # info 级是"产出没人用"这类提示，不进校验（它不影响能不能跑）
        if issue.level == "info":
            continue
        result.add(issue.message, level=issue.level, node_id=issue.node_id,
                   field=issue.field or None)

    return result


# --------------------------------------------------------------------------
# 提示词点名的工具，节点得真的绑了它
#
# 一次真实运行：助手改图时把三个查库 agent 的 tools 丢了，提示词里还写着「用
# db_query__… 查」。模型拿不到工具，只能写一句「假设调用工具」，再往下编；校验节点
# 的修复又替它编了个 0。这张图在跑之前就能认出来。Copilot 的自查读的是同一份
# error，搭图时就会交回模型去修。
#
# 这条是 error，会挡住运行，还会作为 Copilot 自查的阻断项交回模型（模型可能反过来
# 把提示词里禁用的工具绑上去），所以宁可漏判不可误判：
#   - 认得出的工具名才算点名：内置工具、db_query__ / db_schema__ / mcp: 这几种形状，
#     以及这张图里别的节点绑过的名字；
#   - 「不要用 X」「Never call X」「X 的结果」不是在要求用它；
#   - calculator 这种本身就是个普通词的工具名，只有写在反引号里、或者紧跟在「用 /
#     调用 / use / call」后面才算点名；「可以用」「如有需要」「if needed」这类非强制的
#     说法也一样。这两种只报 warning。
# --------------------------------------------------------------------------

_DYNAMIC_TOOL = r"db_(?:query|schema)__[A-Za-z0-9_\-]+|mcp:[\w\-]+/[\w\-.]+"
# 工具名紧挨着汉字是常态（「用python_exec核对」），所以边界只排 ASCII 标识符字符
_EDGE_BEFORE, _EDGE_AFTER = r"(?<![A-Za-z0-9_:/\-])", r"(?![A-Za-z0-9_\-])"
_TEMPLATE_SPAN = re.compile(r"\{\{.*?\}\}", re.S)
_NEGATED = re.compile(
    r"(?:(?:不要|不用|不必|无需|无须|毋须|不需要|不再|别再|别|禁止|停止|不能|不得|不应|不可|避免|切勿|勿|严禁)"
    r"\s*(?:再|去)?(?:使用|调用|用|执行|运行|跑)?"
    r"|(?<![A-Za-z])(?:don['’]?t|do\s+not|never|avoid|without|must\s+not|mustn['’]?t|should\s+not"
    r"|shouldn['’]?t|cannot|can['’]t|no\s+need\s+to|not|no)\s+(?:(?:use|call|invoke|run|execute|using|calling"
    r"|invoking|running|rely\s+on|touch)\s+)?(?:the\s+|any\s+)?)"
    r"\s*[「“\"'`]?\s*$", re.I)
_REFERENCED = re.compile(r"^\s*[」”\"'`]?\s*(的|返回|给出|查到|产出|输出|结果"
                         r"|(?:'s\b)|(?:results?|outputs?|returned|returns)\b)", re.I)
# 普通词形的工具名（calculator）要有明确的「用」字眼紧挨着才算点名
_PLAIN_WORD = re.compile(r"[A-Za-z][a-z]*(?:-[a-z]+)*")
_USE_VERB = re.compile(r"(?:使用|调用|用|(?<![A-Za-z])(?:use|call|invoke|run))\s*$", re.I)
# 非强制的说法：同一句里出现就只算提到，不算要求
_OPTIONAL = re.compile(r"可以|可选|如有需要|如需|若需|必要时|需要时|有需要|酌情"
                       r"|(?<![A-Za-z])(?:may|might|can|could|optionally|if\s+(?:needed|necessary|required)"
                       r"|when\s+needed|feel\s+free)(?![A-Za-z])", re.I)
_CLAUSE_END = re.compile(r"[。；;！!？?\n]|\.(?:\s|$)")
# 笼统地要求用工具、却没点名
_VAGUE = re.compile(r"(调用|使用|用|借助|通过)\s*(相关|对应|可用|数据库|查询)?\s*工具"
                    r"|查询?数据库|查库|执行\s*SQL|跑\s*SQL", re.I)


def _bound_tools(cfg: dict[str, Any]) -> set[str]:
    return {t for t in cfg.get("tools") or [] if isinstance(t, str)}


def _covers(bound: set[str], tool: str) -> bool:
    """tool 在不在 bound 里。mcp:<server>/*（或只写 mcp:<server>）绑的是这个 server 的全部工具。"""
    if tool in bound:
        return True
    if not tool.startswith("mcp:"):
        return False
    server = tool[4:].partition("/")[0]
    return any(b in (f"mcp:{server}", f"mcp:{server}/", f"mcp:{server}/*") for b in bound)


def _named_tool_pattern(spec: GraphSpec) -> re.Pattern[str]:
    from app.tools.registry import all_specs

    bound: set[str] = set()
    for node in spec.nodes:
        cfg = node.config
        bound.update(_bound_tools(cfg))
        if isinstance(cfg.get("tool"), str):
            bound.add(cfg["tool"])
        for member in cfg.get("agents") or []:
            if isinstance(member, dict):
                bound.update(_bound_tools(member))
    names = sorted({*all_specs(), *bound}, key=len, reverse=True)
    literal = "|".join(re.escape(n) for n in names if n)
    body = f"{_DYNAMIC_TOOL}|{literal}" if literal else _DYNAMIC_TOOL
    return re.compile(f"{_EDGE_BEFORE}(?:{body}){_EDGE_AFTER}")


# 列举里的分隔：「严禁调用 web_search、web_fetch、http_request 等」「never call a, b or c」
_LIST_TAIL = re.compile(r"(?:[\s、，,/和或及与跟以「」“”\"'`]|(?<![A-Za-z])(?:or|and|nor)(?![A-Za-z]))+$",
                        re.I)


def _negated(plain: str, start: int, pattern: re.Pattern[str]) -> bool:
    """这个工具名前面是不是一句「不要用」。列举里排在后面的也算：往回跳过前面的工具名和顿号。"""
    head = plain[max(0, start - 120):start]
    while True:
        trimmed = _LIST_TAIL.sub("", head)
        last = None
        for last in pattern.finditer(trimmed):
            pass
        if last is not None and last.end() == len(trimmed):
            trimmed = trimmed[:last.start()]
        if trimmed == head:
            break
        head = trimmed
    return bool(_NEGATED.search(head[-40:] + " "))


def _clause(plain: str, start: int, end: int) -> str:
    """工具名所在的那一句。"""
    before = [m.end() for m in _CLAUSE_END.finditer(plain, 0, start)]
    after = _CLAUSE_END.search(plain, end)
    return plain[before[-1] if before else 0:after.start() if after else len(plain)]


def _asked_tools(text: str, pattern: re.Pattern[str]) -> dict[str, bool]:
    """文本里被要求使用的工具名，按出现顺序。值是「是不是明确要求」：否的只算提到了。"""
    # {{ }} 里的是模板引用，不是在点名工具；换成等长空白，位置不变
    plain = _TEMPLATE_SPAN.sub(lambda m: " " * len(m.group(0)), text)
    found: dict[str, bool] = {}
    for m in pattern.finditer(plain):
        if _negated(plain, m.start(), pattern) or _REFERENCED.match(plain[m.end():m.end() + 12]):
            continue
        name = m.group(0)
        explicit = not _OPTIONAL.search(_clause(plain, m.start(), m.end()))
        if explicit and _PLAIN_WORD.fullmatch(name):
            quoted = plain[m.start() - 1:m.start()] == "`" and plain[m.end():m.end() + 1] == "`"
            explicit = quoted or bool(_USE_VERB.search(plain[max(0, m.start() - 12):m.start()]))
        found[name] = found.get(name, False) or explicit
    return found


def _asks_vaguely(text: str) -> bool:
    plain = _TEMPLATE_SPAN.sub(" ", text)
    return any(not _NEGATED.search(plain[max(0, m.start() - 12):m.start()])
               for m in _VAGUE.finditer(plain))


def _check_named_tools(spec: GraphSpec, result: ValidationResult) -> None:
    agents = [n for n in spec.nodes if n.type in (NodeType.AGENT, NodeType.SUPERVISOR)]
    if not agents:
        return
    pattern = _named_tool_pattern(spec)

    def check(node: GraphNode, texts: list[tuple[str, Any]], tools: Any, who: str = "") -> None:
        bound = {t for t in tools or [] if isinstance(t, str)}
        vague_at: str | None = None
        reported: set[str] = set()
        asked: dict[str, tuple[bool, str]] = {}
        for field, text in texts:
            if not isinstance(text, str) or not text.strip():
                continue
            for tool, explicit in _asked_tools(text, pattern).items():
                # system 和 prompt 里都点了同一个工具，报一次就够：指到先出现的那处，
                # 哪一处是明确要求就按要求算
                seen = asked.get(tool)
                asked[tool] = (explicit or bool(seen and seen[0]), seen[1] if seen else field)
            if not bound and vague_at is None and _asks_vaguely(text):
                vague_at = field
        for tool, (explicit, field) in asked.items():
            if _covers(bound, tool):
                continue
            reported.add(tool)
            if explicit:
                message = (f"{who}的提示词要求用「{tool}」，但没有给这个成员绑定它" if who
                           else f"提示词要求用「{tool}」，但节点没有绑定它")
                where = "该成员的工具" if who else f"「{field_label('tools')}」"
                result.add(f"{message}：运行时模型无法调用该工具，可能会编造结果。"
                           f"请把它加入{where}，或删除提示词中的这项要求",
                           node_id=node.id, field=field)
            else:
                result.add(f"{who + '的' if who else ''}提示词提到了「{tool}」，但{'这个成员' if who else '节点'}没有"
                           "绑定它：如果需要模型调用它，运行时将无法调用。如有需要，请把它加入工具",
                           level="warning", node_id=node.id, field=field)
        # 已经点名报过了，那句笼统的就不再重复
        if vague_at is not None and not reported:
            result.add(f"{who or '提示词'}要求调用工具，但{'这个成员' if who else '这个节点'}没有绑定任何"
                       "工具：模型无法调用工具，只能假设调用结果。请绑定要用的工具，或删除这项要求",
                       level="warning", node_id=node.id, field=vague_at)

    for node in agents:
        cfg = node.config
        if node.type == NodeType.AGENT:
            check(node, [("system", cfg.get("system")), ("prompt", cfg.get("prompt"))],
                  cfg.get("tools"))
            continue
        members = [m for m in cfg.get("agents") or [] if isinstance(m, dict)]
        team_tools: set[str] = set()
        for i, member in enumerate(members):
            name = member.get("name") or f"agent{i}"
            team_tools.update(_bound_tools(member))
            check(node, [(f"agents[{i}].system", member.get("system"))], member.get("tools"),
                  who=f"成员「{name}」")
        # 协作目标是给调度者看的：点了名的工具，至少得有一个成员拿得到
        goal = cfg.get("goal")
        if isinstance(goal, str):
            for tool, explicit in _asked_tools(goal, pattern).items():
                if _covers(team_tools, tool):
                    continue
                if explicit:
                    result.add(f"团队目标要求用「{tool}」，但没有任何成员绑定该工具：调度者分派的成员"
                               "都无法调用它。请为负责的成员添加该工具，或删除目标中的这项要求",
                               node_id=node.id, field="goal")
                else:
                    result.add(f"团队目标提到了「{tool}」，但没有任何成员绑定该工具：如果需要成员"
                               "调用它，目前都无法调用。如有需要，请为负责的成员添加",
                               level="warning", node_id=node.id, field="goal")


# --------------------------------------------------------------------------
# 不按 JSON 解析模型写的文字
#
# 真实踩过的一张图：agent（自由文字）→ transform `{{ nodes.query_logs.text }}`（按 JSON
# 解析）→ 口径卡。模型在 JSON 字符串里夹了没转义的英文引号，整形节点一解析就失败。模型写的
# 文字不是数据：数要交给口径卡，就让 agent 按 output_schema 交结构化字段、开 cite_fields
# 逐个标出处。这里只报 warning（旧工作流照跑）；Copilot 自查把它当成要打回的问题。
# --------------------------------------------------------------------------

_MODEL_TEXT_TYPES = (NodeType.AGENT, NodeType.LLM, NodeType.SUPERVISOR)
_TEXT_REF = re.compile(
    r"nodes\s*(?:\.\s*(?P<a>[\w-]+)|\[\s*['\"](?P<b>[^'\"]+)['\"]\s*\])"
    r"\s*(?:\.\s*text(?![\w-])|\[\s*['\"]text['\"]\s*\])")
_VAR_REF = re.compile(r"vars\s*(?:\.\s*(?P<a>[\w-]+)|\[\s*['\"](?P<b>[^'\"]+)['\"]\s*\])")


def _writes_text(node: GraphNode) -> bool:
    """这个节点的产出是不是模型写的文字（而不是系统序列化的结构化数据）。

    llm 配了 output_schema 走原生结构化输出，text 是系统 json.dumps 的；agent 只有同时
    开了 cite_fields，assign_to 才拿到核对过的 data——它的 .text 仍然是模型的原话。
    """
    if node.type == NodeType.LLM:
        return not node.config.get("output_schema")
    return node.type in _MODEL_TEXT_TYPES


def _text_sources(text: str, spec: GraphSpec, *, template: bool) -> list[GraphNode]:
    """一段模板（或表达式）读了哪些节点的模型原话。模板里过了 | json 的不算：那是合法的 JSON 字符串。"""
    nodes = spec.node_map()
    pieces = [m.group(0)[2:-2] for m in _TEMPLATE_SPAN.finditer(text)] if template else [text]
    found: dict[str, GraphNode] = {}
    for piece in pieces:
        head, *filters = [p.strip() for p in piece.split("|")] if template else [piece]
        if "json" in filters:
            continue
        for m in _TEXT_REF.finditer(head):
            node = nodes.get(m.group("a") or m.group("b"))
            if node is not None and _writes_text(node):
                found.setdefault(node.id, node)
        for m in _VAR_REF.finditer(head):
            var = m.group("a") or m.group("b")
            for node in spec.nodes:
                if str(node.config.get("assign_to") or "").strip() != var or node.type not in _MODEL_TEXT_TYPES:
                    continue
                structured = (node.type == NodeType.LLM and node.config.get("output_schema")) or (
                    node.type == NodeType.AGENT and node.config.get("output_schema")
                    and node.config.get("cite_fields") is True)
                if not structured:
                    found.setdefault(node.id, node)
    return list(found.values())


def _descendants(spec: GraphSpec, node_id: str) -> set[str]:
    children: dict[str, list[str]] = defaultdict(list)
    for e in spec.edges:
        children[e.source].append(e.target)
    seen: set[str] = set()
    stack = list(children[node_id])
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(children[cur])
    seen.discard(node_id)
    return seen


def _feeds_caliber(node: GraphNode, spec: GraphSpec) -> bool:
    """下游有口径卡读了这个节点的产出（nodes.<id> 或 vars.<assign_to>）。"""
    var = str(node.config.get("assign_to") or "").strip()
    reads = [rf"nodes\s*(?:\.\s*{re.escape(node.id)}(?![\w-])|\[\s*['\"]{re.escape(node.id)}['\"]\s*\])"]
    if var:
        reads.append(rf"vars\s*(?:\.\s*{re.escape(var)}(?![\w-])|\[\s*['\"]{re.escape(var)}['\"]\s*\])")
    pattern = re.compile("|".join(reads))
    nodes = spec.node_map()
    for nid in _descendants(spec, node.id):
        card = nodes.get(nid)
        if card is not None and card.type == NodeType.METRICS and any(
                pattern.search(str((d or {}).get("expression") or "")) for d in card.config.get("metrics") or []):
            return True
    return False


def _structured_fix(source: GraphNode) -> str:
    if source.type == NodeType.AGENT:
        var = str(source.config.get("assign_to") or "").strip() or "变量名"
        return (f"为「{source.title}」配置「结构化输出 Schema」并开启「按出处核对字段」，口径卡直接读取 "
                f"vars.{var}.字段（每个字段都核对到查询结果中的对应单元格）")
    if source.type == NodeType.LLM:
        return (f"为「{source.title}」配置「结构化输出 Schema」；数字需要进入口径卡时，请改用 Agent 并开启"
                "「按出处核对字段」")
    return "数字需要进入口径卡时，请改用 Agent，配置「结构化输出 Schema」并开启「按出处核对字段」"


def _json_spans(template: str) -> list[tuple[str, bool]]:
    """模板里每个 {{ }}：(里面的表达式, 它是不是落在 JSON 的引号里)。只数模板自己写的字，跳过 {{ }}。"""
    out: list[tuple[str, bool]] = []
    inside, last = False, 0
    for m in _TEMPLATE_SPAN.finditer(template):
        text, i = template[last:m.start()], 0
        while i < len(text):
            if inside and text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                inside = not inside
            i += 1
        out.append((m.group(0)[2:-2].strip(), inside))
        last = m.end()
    return out


def text_parse_issues(spec: GraphSpec) -> list[ValidationIssue]:
    """整形节点解析 agent / llm 写的文字、产出流向口径卡或按 JSON 解析：一个节点一条 warning。"""
    out: list[ValidationIssue] = []
    for node in spec.nodes:
        if node.type != NodeType.TRANSFORM:
            continue
        mode = node.config.get("mode", "expression")
        field = "expression" if mode == "expression" else "template"
        sources = _text_sources(str(node.config.get(field) or ""), spec, template=field == "template")
        if not sources:
            continue
        who = "、".join(f"「{s.title}」（{type_label(s.type)}）" for s in sources[:3])
        spans = [(expr, quoted) for expr, quoted in _json_spans(str(node.config.get(field) or ""))
                 if _text_sources(f"{{{{ {expr} }}}}", spec, template=True)] if mode == "json" else []
        if spans and all(quoted for _, quoted in spans):
            # 模型的文字放在模板的引号里：不是在解析它，是在拼字符串，少的是 | json
            expr = spans[0][0]
            message = (f"模板中的 {{{{ {expr} }}}} 位于 JSON 引号内：{who}写的文字只要包含英文引号或换行，JSON 就会"
                       f"格式错误，导致整个节点失败。请改写为 {{{{ {expr} | json }}}}（外面不要再加引号）")
        elif mode == "json":
            message = (f"「数据整形」节点按 JSON 解析{who}写的文字：模型写的 JSON 常含有未转义的英文引号，一旦解析失败，"
                       f"整个节点就会失败。请改为让它直接输出结构化数据：{_structured_fix(sources[0])}")
        elif _feeds_caliber(node, spec):
            message = (f"口径卡的数字需要经过这个「数据整形」节点，从{who}写的文字中提取：文字不是结构化数据，提取不可靠，"
                       f"也无法追溯出处。{_structured_fix(sources[0])}")
        else:
            continue
        out.append(ValidationIssue(level="warning", node_id=node.id, field=field, message=message))
    return out


# --------------------------------------------------------------------------
# 证据链相关的配置：agent 的结构化字段、沙箱代码的角色、钉住别处的口径卡
# --------------------------------------------------------------------------

EVIDENCE_ROLES = ("source", "compute")
_REF_HEAD = re.compile(r"^(vars|nodes)\.([\w-]+)")


def _caliber_inputs(card: GraphNode, spec: GraphSpec) -> list[tuple[int, GraphNode]]:
    """口径卡每个指标表达式里读到的节点：(指标下标, 产出它的节点)。"""
    from app.engine.expressions import ExpressionError, leaf_refs, parse_expression

    nodes = spec.node_map()
    out: list[tuple[int, GraphNode]] = []
    for i, d in enumerate(card.config.get("metrics") or []):
        try:
            tree, _, _ = parse_expression(str((d or {}).get("expression") or ""))
        except ExpressionError:
            continue
        for path in leaf_refs(tree):
            m = _REF_HEAD.match(path.split("cell(", 1)[-1].lstrip())
            if not m:
                continue
            root, name = m.groups()
            if root == "nodes":
                producers = [nodes[name]] if name in nodes else []
            else:
                producers = [n for n in spec.nodes if str(n.config.get("assign_to") or "").strip() == name]
            out.extend((i, n) for n in producers)
    return out


def evidence_issues(spec: GraphSpec) -> list[ValidationIssue]:
    from app.engine.governance import UPGRADE_POLICIES, UPGRADE_POLICY_LABELS

    out: list[ValidationIssue] = []

    def add(message: str, node: GraphNode, field: str, level: str = "warning", code: str | None = None) -> None:
        out.append(ValidationIssue(level=level, node_id=node.id, field=field, message=message,  # type: ignore[arg-type]
                                   code=code))

    for node in spec.nodes:
        cfg = node.config
        if node.type == NodeType.AGENT and cfg.get("output_schema") and cfg.get("cite_fields") is not True:
            add("「结构化输出 Schema」需要同时开启「按出处核对字段」才会生效（会多一次抽取调用）：开启后，Agent 执行"
                "结束时按 Schema 抽取字段，每个字段都核对到查询结果中的对应单元格；不开启时，「结果存为变量」得到的"
                "仍是自由文本",
                node, "output_schema", code="evidence.cite_fields_off")
        elif node.type == NodeType.CODE:
            role = cfg.get("evidence_role")
            if role not in (None, "") and role not in EVIDENCE_ROLES:
                add(f"「证据角色」只能是「取数」或「计算」，当前为「{role}」", node,
                    "evidence_role", level="error")
        elif node.type == NodeType.METRICS:
            if cfg.get("caliber_from") not in (None, "", {}):
                out.extend(_caliber_from_issues(node, UPGRADE_POLICIES, UPGRADE_POLICY_LABELS))
            warned: set[str] = set()
            for i, producer in _caliber_inputs(node, spec):
                role = str(producer.config.get("evidence_role") or "compute")
                if producer.type != NodeType.CODE or role == "source" or producer.id in warned:
                    continue
                warned.add(producer.id)
                add(f"口径卡的输入来自沙箱代码「{producer.title}」，它的「证据角色」是「计算」：沙箱中计算出的数字"
                    "无法追溯出处。负责取数的代码节点请将「证据角色」设为「取数」；业务计算请移到口径卡的表达式中",
                    node, f"metrics[{i}].expression",
                    code="evidence.caliber_compute_input")
    return out


def _caliber_from_issues(node: GraphNode, policies: set[str], labels: dict[str, str]) -> list[ValidationIssue]:
    """钉住别的工作流里一张口径卡：{workflow_id, workflow_version, node_id} 三项都要写对。"""
    cfg = node.config
    ref = cfg.get("caliber_from")
    out: list[ValidationIssue] = []

    def add(message: str, field: str, level: str = "error") -> None:
        out.append(ValidationIssue(level=level, node_id=node.id, field=field, message=message))  # type: ignore[arg-type]

    if not isinstance(ref, dict) or not str(ref.get("workflow_id") or "").strip() \
            or not str(ref.get("node_id") or "").strip():
        add(f"「{field_label('caliber_from')}」需要选定工作流、版本和其中的口径卡", "caliber_from")
        return out
    version = ref.get("workflow_version")
    if isinstance(version, bool) or not (isinstance(version, int) or (isinstance(version, str) and version.isdigit())) \
            or int(version) < 1:
        add(f"「{field_label('caliber_from')}」需要选择具体的版本号（1、2…），当前为「{version}」：不固定版本，"
            "口径会随上游变化", "caliber_from.workflow_version")
    if cfg.get("metrics"):
        add(f"已设置「{field_label('caliber_from')}」，将按所引用的口径卡计算，此处填写的「{field_label('metrics')}」"
            "不会生效", "metrics", level="warning")
    policy = cfg.get("upgrade_policy")
    if policy not in (None, "") and policy not in policies:
        allowed = [f"「{v}」" for v in labels.values()]
        add(f"「{field_label('upgrade_policy')}」只能选{'、'.join(allowed[:-1])}或{allowed[-1]}，当前为「{policy}」",
            "upgrade_policy")
    return out
