"""场景模板 agentlab.template/1（docs/design/normal-user-apps.md §2.3、§3.3、§3.4）：骨架锁住，关节可填。

一个模板是一份登记（和组件目录同一种做法）：骨架（带占位的图）、关节、应用骨架、定时任务、卡片约束、说明。
关节分两种：确定性关节由蓝图直接填，不经模型；生成关节只有 sql 和 metric_expr 两类，模型在 TemplateApplier 下
只能用 fill_joint 填模板声明过的关节。实例化后的图和骨架逐节点比对，只许关节位置不同（tpl.skeleton_changed）。

开发者扩展时，模板定义（连同示例蓝图 example / example_fill）、实例化、测试三样都要给；check_registry 在启动时核对。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

TEMPLATE_FORMAT = "agentlab.template/1"
#: 首批五个，外加「自由形」（从已有运行或工作流版本导入，没有关节）
TEMPLATE_IDS = ("periodic_board", "approval", "exception_handling", "records", "reconcile_writeback", "freeform")
#: 关节类型（§3.4）
JOINT_KINDS = ("sql", "metric_expr", "fields", "assignee", "policy", "due", "on_timeout", "threshold", "capability",
               "contract")
#: 交给模型填的关节类型；其余由蓝图确定性地填
GENERATED_JOINT_KINDS = ("sql", "metric_expr")


@dataclass(frozen=True)
class Joint:
    """骨架里允许填的一个位置：node_id 节点的 field（配置里的 JSON 指针，如 /args/sql、/metrics/0/expression）。
    from_card 是确定性关节取自蓝图的哪里（JSON 指针）；生成关节为 None。group 相同的生成关节一起填，不同的并行填。"""

    id: str
    kind: str
    node_id: str
    field: str
    from_card: str | None = None
    group: str | None = None
    label: str = ""


@dataclass
class TemplateDef:
    id: str
    version: int
    label: str
    description: str
    #: 给助手看的适用条件（一两句话）
    applies_when: str
    #: 这个模板用到哪些卡片（blueprints/spec.CARDS 的子集）和各自的约束说明
    cards: dict[str, str]
    #: 个人应用（使用者）能不能用、能用哪一部分（None = 不能用）
    personal: str | None
    #: skeleton(bp) -> (GraphSpec 的 JSON, [Joint])：按蓝图出骨架和关节（关节里的值还没填）
    skeleton: Callable[[dict[str, Any]], tuple[dict[str, Any], list[Joint]]]
    #: app_skeleton(bp, graph) -> agentlab.app/1：在 derive_app 的结果上按模板排布
    app_skeleton: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None
    #: schedule(bp) -> 定时任务参数 | None
    schedule: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    tags: list[str] = field(default_factory=list)
    #: 一张完整的示例蓝图（测试和启动核对用）：实例化、生成关节都填上后要能通过画布校验和发布前检查。必须给
    example: Callable[[], dict[str, Any]] | None = None
    #: 示例蓝图的生成关节怎么填（测试和启动核对用）：{关节 id: 值}。必须给（没有生成关节的给 dict）
    example_fill: Callable[[], dict[str, Any]] | None = None
    #: prefill(spec)：选模板时预置还没填的卡片（原地改，已经写了的不动）
    prefill: Callable[[dict[str, Any]], None] | None = None
    #: briefs(bp) -> {group: 说明}：每组生成关节给模型看的说明（填关节时交给助手，依据指纹按它算）
    briefs: Callable[[dict[str, Any]], dict[str, str]] | None = None
    #: problems(spec) -> [{code, level, path, message}]：卡片内容超出模板能力的地方（bp.template_mismatch 等）
    problems: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    #: 自由形：图是导入的（实例化时从上下文拿），模板不出骨架、不重新排版
    imports_graph: bool = False


_REGISTRY: dict[str, TemplateDef] = {}


def register(template: TemplateDef) -> TemplateDef:
    if template.id in _REGISTRY:
        raise ValueError(f"模板「{template.id}」重复登记")
    _REGISTRY[template.id] = template
    return template


def get_template(template_id: str) -> TemplateDef | None:
    return _REGISTRY.get(template_id)


def list_templates() -> list[TemplateDef]:
    return [_REGISTRY[t] for t in TEMPLATE_IDS if t in _REGISTRY] + [
        t for k, t in _REGISTRY.items() if k not in TEMPLATE_IDS]


def check_registry() -> list[str]:
    """启动时核对（api/templates.py 导入时调）：首批模板都登记了、关节类型都认得、每个模板都给了示例蓝图，示例实例化出来
    过得了画布校验、骨架比对没有差异（blueprints/instantiate.registry_problems）。交回问题清单（空 = 没问题）。"""
    from app.blueprints.instantiate import registry_problems

    return registry_problems()

from app.blueprints.templates import approval, exception_handling, freeform, periodic_board, reconcile_writeback, records  # noqa: E402,F401
