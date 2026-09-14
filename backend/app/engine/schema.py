from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

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

    # 孤儿节点
    for node in spec.nodes:
        if node.type in (NodeType.INPUT, NodeType.OUTPUT):
            continue
        if not spec.incoming(node.id) and not spec.outgoing(node.id):
            result.add("节点没有任何连线，不会被执行", level="warning", node_id=node.id)

    return result
