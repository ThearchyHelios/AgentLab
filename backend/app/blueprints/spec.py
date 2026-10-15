"""应用蓝图 agentlab.blueprint/1（docs/design/normal-user-apps.md §2.2、§3.2）。

蓝图由几张「卡片」组成，每一项都是 {value, source, status}：
- source：user（用户填的）/ assistant（助手提的）/ template（模板默认）/ catalog（从数据目录对到的）/ caliber（复用已发布口径卡）；
- status：confirmed（已确认）/ proposed（待确认）/ missing（缺）。试运行前「缺」都要补齐，发布前「待确认」都要确认。

模型和用户都只能用 bp_* 操作改蓝图（blueprints/ops.py），落地是确定性的。这里只定形状，不做业务校验。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BLUEPRINT_FORMAT = "agentlab.blueprint/1"
ITEM_SOURCES = ("user", "assistant", "template", "catalog", "caliber")
ITEM_STATUSES = ("confirmed", "proposed", "missing")
#: 卡片（界面上一张卡一个），顺序就是界面上的顺序
CARDS = ("purpose", "template", "data", "cadence", "people", "fields", "writeback", "access")
#: bp_* 操作。路径是 JSON 指针（/data/0/table）
BP_OPS = ("bp_set", "bp_add", "bp_remove", "bp_ask", "bp_template")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Item(_Strict):
    """卡片上的一项：值、来源、状态，可选一句「为什么」（例如为什么对到这张表）。"""

    value: Any = None
    source: Literal["user", "assistant", "template", "catalog", "caliber"] = "assistant"
    status: Literal["confirmed", "proposed", "missing"] = "missing"
    why: str | None = None


class TemplateRef(_Strict):
    id: str
    version: int = 1


class DataItem(_Strict):
    """「看哪些数」的一项。table 对到 {source, table}；reuse 对到已发布口径卡 {workflow_id, version, node_id, metric_id}
    时实例化成 caliber_from；measure 是白话或简单聚合的提议，不是最终表达式（最终的在关节里）。"""

    id: str
    label: Item = Field(default_factory=Item)
    unit: Item = Field(default_factory=Item)
    table: Item = Field(default_factory=Item)
    measure: Item = Field(default_factory=Item)
    date: Item = Field(default_factory=Item)
    filter: Item = Field(default_factory=Item)
    grain: Item = Field(default_factory=Item)
    reuse: dict[str, Any] | None = None


class Step(_Strict):
    """「谁来做」的一步：处理人（[{kind: user|group|slot|initiator, name}]）、几个人同意算通过、时限、超时给谁。"""

    id: str
    label: Item = Field(default_factory=Item)
    who: Item = Field(default_factory=Item)
    policy: Item = Field(default_factory=Item)  # {"kind": any|all|quorum, "m"?: int}
    due: Item = Field(default_factory=Item)  # ISO 8601 时长，由卡片上的「2 天」「48 小时」换算
    on_timeout: Item = Field(default_factory=Item)  # {"action": remind|escalate|reject, "to"?: [...]}
    threshold: Item = Field(default_factory=Item)  # {"field", "op", "value"}：什么条件下走这一步（金额线）


class FieldItem(_Strict):
    """「填什么」的一个字段：落到入口表单、待办表单、记录表的列。spec 是 agentlab.form/1 的一个属性。"""

    name: str
    spec: Item = Field(default_factory=Item)
    sensitive: bool = False


class Writeback(_Strict):
    capability: Item = Field(default_factory=Item)
    mapping: Item = Field(default_factory=Item)  # {参数: "form.x" | "metric.y" | 常量}
    confirm: Item = Field(default_factory=Item)  # 要不要人确认（布尔）
    # 结果不明时谁去核实（[{kind: user|group, name}]）：目标能力没登记查询时才要，绑到应用的核实岗位（apps/derive.RECONCILER）。
    # 只由用户定（同「谁能看」）。旧蓝图没有这一项，读进来是空的
    reconciler: Item = Field(default_factory=Item)


class Access(_Strict):
    use: list[dict[str, str]] = Field(default_factory=list)
    observe: list[dict[str, str]] = Field(default_factory=list)
    manage: list[dict[str, str]] = Field(default_factory=list)


class Ask(_Strict):
    """助手的反问：一次问一件事，带候选。answered 之后留着做历史。"""

    id: str
    question: str
    options: list[str] = Field(default_factory=list)
    path: str | None = None
    answer: str | None = None


class BlueprintSpec(_Strict):
    format: Literal["agentlab.blueprint/1"] = BLUEPRINT_FORMAT
    purpose: Item = Field(default_factory=Item)
    template: TemplateRef | None = None
    data: list[DataItem] = Field(default_factory=list)
    cadence: Item = Field(default_factory=Item)  # {preset: daily|weekly|monthly, weekday?, day?, time, tz, period}
    people: list[Step] = Field(default_factory=list)
    fields: list[FieldItem] = Field(default_factory=list)
    writeback: Writeback | None = None
    access: Access = Field(default_factory=Access)
    asks: list[Ask] = Field(default_factory=list)


class BpOp(_Strict):
    """一个 bp_* 操作。bp_set / bp_add 带 value；bp_remove 只要 path；bp_ask 带 question / options；
    bp_template 带 template（{id, version}）。"""

    op: Literal["bp_set", "bp_add", "bp_remove", "bp_ask", "bp_template"]
    path: str | None = None
    value: Any = None
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    template: TemplateRef | None = None


def parse_blueprint(data: Any) -> BlueprintSpec:
    return BlueprintSpec.model_validate(data)


def empty_blueprint() -> dict[str, Any]:
    return BlueprintSpec().model_dump(mode="json", by_alias=True)
