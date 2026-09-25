from __future__ import annotations

import re
from collections import defaultdict
from enum import StrEnum
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class NodeType(StrEnum):
    """画布上可用的节点类型。新增类型 = 加一个枚举值 + 一个执行器 + 一个前端渲染。"""

    INPUT = "input"
    OUTPUT = "output"

    LLM = "llm"  # 单次模型调用
    AGENT = "agent"  # 带工具循环的 ReAct agent
    SUPERVISOR = "supervisor"  # 多 agent 调度

    TOOL = "tool"  # 直接调用一个工具
    CODE = "code"  # 沙箱里跑代码

    BRANCH = "branch"  # 条件分支
    LOOP = "loop"  # 循环 / 迭代
    SUBGRAPH = "subgraph"  # 嵌套另一个工作流

    MEMORY = "memory"  # 长期记忆读写
    RETRIEVE = "retrieve"  # 知识库检索
    TRANSFORM = "transform"  # 数据整形

    HUMAN = "human"  # 人工介入
    VALIDATE = "validate"  # 结构化校验 + 自动重试
    METRICS = "metrics"  # 口径卡：受控指标集，叙述层唯一合法的数字来源


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
            raise ValueError(f"节点 id 只能包含字母数字、下划线和连字符：{v!r}")
        # LangGraph 保留了这两个名字
        if v in ("__start__", "__end__"):
            raise ValueError(f"{v!r} 是保留节点名")
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
    level: Literal["error", "warning"] = "error"
    node_id: str | None = None
    edge_id: str | None = None
    message: str


class ValidationResult(BaseModel):
    ok: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)

    def add(self, message: str, *, level: str = "error", node_id: str | None = None,
            edge_id: str | None = None) -> None:
        self.issues.append(
            ValidationIssue(level=level, message=message, node_id=node_id, edge_id=edge_id)  # type: ignore[arg-type]
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


def expression_fields(node: GraphNode) -> list[tuple[str, str]]:
    """这个节点上按表达式求值的字段：(给人看的名字, 原文)。

    跟着模式走：foreach 循环的 condition、模板模式整形的 expression 不参与求值，
    拿它们报错就是误报。
    """
    cfg = node.config
    out: list[tuple[str, str]] = []

    def add(label: str, value: Any) -> None:
        if isinstance(value, str) and value.strip():
            out.append((label, value))

    add("跳过条件", cfg.get("skip_if"))
    if node.type == NodeType.BRANCH and cfg.get("mode", "expression") == "expression":
        for case in cfg.get("cases") or []:
            case = case or {}
            add(f"分支「{case.get('label') or case.get('key') or '?'}」的条件", case.get("condition"))
    elif node.type == NodeType.LOOP and cfg.get("mode", "foreach") == "while":
        add("循环条件", cfg.get("condition"))
    elif node.type == NodeType.TRANSFORM and cfg.get("mode", "expression") == "expression":
        add("整形表达式", cfg.get("expression"))
    elif node.type == NodeType.METRICS:
        for d in cfg.get("metrics") or []:
            d = d or {}
            add(f"指标「{d.get('id') or '?'}」的表达式", d.get("expression"))
    return out


# 代码节点可能往 stdout 写东西的迹象。宁可漏判不可误判：这条是 error 级、会挡住运行，
# 所以间接的写法（子进程、exec、附带文件里的 print）都算。整行只有一个 {{ }} 的，
# 那行代码是运行时从上游拿的（⑤号示例整段就是 {{ vars.code }}），看不见，不猜；
# 而 `state = {{ vars.state | json }}` 这种只是填一个值，照查
_MAY_WRITE_STDOUT = re.compile(
    r"\b(print|pprint|exec)\s*\(|\bstdout\b|\bos\.(system|write|popen)\b|\bsubprocess\b"
    r"|^\s*\{\{.*\}\}\s*$", re.M)


def validate_graph(spec: GraphSpec) -> ValidationResult:
    """编译前的静态检查。错误会挡住运行，警告只在画布上提示。"""
    result = ValidationResult()
    nodes = spec.node_map()

    if not spec.nodes:
        result.add("图是空的，先拖一个节点进来")
        return result

    # 引用完整性
    for edge in spec.edges:
        if edge.source not in nodes:
            result.add(f"边指向了不存在的源节点 {edge.source!r}", edge_id=edge.id)
        if edge.target not in nodes:
            result.add(f"边指向了不存在的目标节点 {edge.target!r}", edge_id=edge.id)

    if not spec.entry_nodes():
        result.add("找不到入口：每个节点都有入边，图里存在环且没有起点")

    # 节点级必填项
    for node in spec.nodes:
        cfg = node.config
        if node.type == NodeType.BRANCH:
            cases = cfg.get("cases") or []
            if not cases:
                result.add("分支节点至少要配一个条件分支", node_id=node.id)
            handles = {e.sourceHandle for e in spec.outgoing(node.id)}
            for case in cases:
                key = case.get("key")
                if key and key not in handles:
                    result.add(
                        f"分支 {key!r} 没有连出去的边", level="warning", node_id=node.id
                    )
            if "default" not in handles:
                result.add(
                    "建议给分支节点连一条 default 边，兜住所有条件都不满足的情况",
                    level="warning",
                    node_id=node.id,
                )
        elif node.type == NodeType.TOOL:
            if not cfg.get("tool"):
                result.add("工具节点还没选工具", node_id=node.id)
        elif node.type == NodeType.SUBGRAPH:
            if not cfg.get("workflow_id"):
                result.add("子图节点还没选要嵌套的工作流", node_id=node.id)
        elif node.type == NodeType.VALIDATE:
            if not cfg.get("schema"):
                result.add("校验节点需要一个 JSON Schema", node_id=node.id)
        elif node.type == NodeType.METRICS:
            defs = cfg.get("metrics") or []
            if not defs:
                result.add("口径卡还没有定义指标", node_id=node.id)
            for d in defs:
                if not d.get("id") or not d.get("expression"):
                    result.add(f"指标定义缺 id 或 expression：{d.get('id') or '(空)'}",
                               node_id=node.id)
        elif node.type == NodeType.OUTPUT:
            contract = cfg.get("contract")
            if contract and not contract.get("metrics_from"):
                result.add("出具契约缺 metrics_from（指标来自哪个口径卡节点）",
                           node_id=node.id)
        elif node.type == NodeType.LOOP:
            if not spec.outgoing(node.id):
                result.add("循环节点没有循环体", node_id=node.id)

        if node.type in (NodeType.LLM, NodeType.AGENT, NodeType.SUPERVISOR):
            if not (cfg.get("model") or spec.defaults.get("model")):
                result.add(
                    "没有指定模型，将回退到默认 provider",
                    level="warning",
                    node_id=node.id,
                )

    # 表达式：用运行时同一个 parse_expression 先解析一遍。以前写错了只有跑到那一步
    # 才知道——前面的步骤白跑、钱白花，报错还停在半路（开发库里四次运行死在
    # "表达式里不允许出现 Set"，就是条件里套了 {{ }}）
    from app.engine.expressions import ExpressionError, parse_expression

    for node in spec.nodes:
        for label, text in expression_fields(node):
            try:
                _, unwrapped, unknown = parse_expression(text)
            except ExpressionError as e:
                result.add(f"{label}写错了：{e}（原文：{text}）", node_id=node.id)
                continue
            if unwrapped:
                result.add(
                    f"{label}里的 {{{{ }}}} 是多余的——这里是表达式、不是模板，"
                    f"已按 {'、'.join(unwrapped)} 理解", level="warning", node_id=node.id,
                )
            if unknown:
                result.add(
                    f"{label}里的 {'、'.join(unknown)} 不是能用的名字，运行时会取到空值"
                    "（变量要写全：vars.x、input.x、nodes.某节点.text）",
                    level="warning", node_id=node.id,
                )
        cfg = node.config
        if node.type == NodeType.LOOP and cfg.get("mode", "foreach") == "while" \
                and not str(cfg.get("condition") or "").strip():
            result.add("while 循环没有写条件，循环体一次都不会跑", level="warning", node_id=node.id)
        if node.type == NodeType.TRANSFORM and cfg.get("mode", "expression") == "expression" \
                and not str(cfg.get("expression") or "").strip():
            result.add("整形节点没有填表达式", node_id=node.id)
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
                    f"代码把输出交给 vars.{var}，可代码里没有 print：Python 脚本最后一行的表达式"
                    f"不会自动输出，vars.{var} 会是空的。把结果 print 出来；要交给下游一个对象，"
                    "就 print(json.dumps(结果))", node_id=node.id,
                )

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
        result.add(issue.message, level=issue.level, node_id=issue.node_id)

    return result
