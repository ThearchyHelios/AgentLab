"""应用描述 agentlab.app/1 与表单 agentlab.form/1 的结构（docs/design/app-platform-and-look.md §3.2–§3.4）。

这里只管「形状」：pydantic 认得出字段、类型、必填。语义检查（钉住的版本存不存在、绑定的节点在不在、表达式合不合法、
数字有没有证据……）在 app/apps/lint.py，编号见设计文档 §3.13。

界面层不放逻辑（P7）：绑定是带类型的对象，不是模板；可见性表达式只能用 APP_ROOTS 里的根名字，禁止算术，
由 engine/expressions.py 和画布校验、运行时同一个解析器求值，服务端求值、不可见的不下发。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

APP_FORMAT = "agentlab.app/1"
FORM_FORMAT = "agentlab.form/1"
VIEW_FORMAT = "agentlab.view/1"

#: 可见性表达式能用的根名字（engine/expressions.py 的 roots=）
APP_ROOTS = frozenset({"run", "input", "metric", "instance", "task", "user", "view"})

#: 绑定的种类：bind 对象里出现哪个键就是哪种（§3.4）
BINDING_KINDS = ("metric", "series", "cell", "query", "report", "input", "run", "output", "task", "action")

#: 钉住项的用途。process 的版本按流程规则检查（process.*）
PIN_USES = ("view", "entry", "process")

#: 视图来源
SOURCE_KINDS = ("latest_sealed", "on_demand", "instance")

#: 钉住项升版的处置（同口径升版，engine/governance.py 的 UPGRADE_POLICIES）
DISPOSITIONS = ("recompute", "dual", "incomparable")

#: 表单控件（agentlab.form/1 的 x-widget）
FORM_WIDGETS = (
    "text", "textarea", "number", "integer", "date", "datetime", "select", "radio", "multiselect", "checkbox",
    "principal", "note", "json",
    # 文件（docs/design/normal-user-apps.md N9）：值是上传后拿回的引用 {file_id, name, size, sha256, type}，
    # 字段 type 为 object，只有这个控件能用 object
    "file",
)
#: 表单字段能用的 type
FORM_TYPES = ("string", "number", "integer", "boolean", "array")
#: 文件控件（x-widget: file）的字段类型：只有这个控件能用 object（apps/forms.py 按控件放行）
FILE_FIELD_TYPE = "object"
#: 文件引用的形状：上传后拿回、提交时原样带回
FILE_REF_KEYS = ("file_id", "name", "size", "sha256", "type")
#: 表单字段能用的 format。不支持 pattern：JS 和 Python 的正则语义不同，会出现前端放行、后端拒收
FORM_FORMATS = ("date", "date-time", "email", "uri")
#: 表单校验错误的 code
FORM_ERROR_CODES = ("required", "type", "enum", "minimum", "maximum", "min_length", "max_length", "max_items", "format")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Pin(_Strict):
    """一个钉住项。共享应用钉工作流版本；还没发布的（蓝图试运行时派生的草稿）version 为空，应用检查按
    app.pin_unpublished 拦发布。个人应用（N7）没有工作流：写 blueprint（蓝图 id），version 为空，图在蓝图上。
    workflow_id 和 blueprint 恰好写一个。"""

    alias: str
    workflow_id: str | None = None
    version: int | None = None
    graph_hash: str
    level: str | None = None
    use: Literal["view", "entry", "process"] = "view"
    blueprint: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> Pin:
        if (self.workflow_id is None) == (self.blueprint is None):
            raise ValueError("固定版本要写 workflow_id 或 blueprint 其中一个")
        return self


class Slot(_Strict):
    label: str


class InstanceTitle(_Strict):
    """实例标题：只能按名字引用入口字段，由服务端拼。例：{"pattern": "{applicant} 的报销", "fields": ["applicant"]}"""

    pattern: str
    fields: list[str] = Field(default_factory=list)


class Entry(_Strict):
    id: str
    label: str
    pin: str
    # 内联的 agentlab.form/1，或 {"$ref": "#/forms/<名字>"}
    form: dict[str, Any]
    # 入口字段 ← 表单值 / 当前用户 / 常量：{"amount": "form.amount", "applicant": "user.display_name", "kind": "'报销'"}
    map: dict[str, str] = Field(default_factory=dict)
    instance_title: InstanceTitle | None = None


class ViewSource(_Strict):
    # exploratory 只在个人应用里合法（N7）：取这个应用、这个拥有者、这张图下最近一次成功封存的探索运行
    kind: Literal["latest_sealed", "on_demand", "instance", "exploratory"]
    pin: str | None = None
    # latest_sealed：schedule:<id> | formal
    from_: str | None = Field(default=None, alias="from")
    # on_demand：参数表单（agentlab.form/1 或 $ref）、缓存时效、打开页面时是否自动发起
    params: dict[str, Any] | None = None
    ttl_s: int | None = None
    auto: bool = False


class View(_Strict):
    source: ViewSource


class Component(_Strict):
    id: str
    type: str
    props: dict[str, Any] = Field(default_factory=dict)
    # 带类型的绑定（BINDING_KINDS 之一），不是模板
    bind: dict[str, Any] | None = None
    # 可见性表达式（APP_ROOTS，禁止算术）
    visible: str | None = None
    # 可见性表达式里 run / metric / input 指的是哪个视图
    scope: str | None = None
    # 作者隐藏了它（N6）：解析时跳过，编辑状态下照常画成半透明。和可见性表达式不是一回事
    hidden: bool = False
    children: list[Component] = Field(default_factory=list)


class Page(_Strict):
    id: str
    title: str
    body: list[Component] = Field(default_factory=list)


class Hook(_Strict):
    name: str
    kind: Literal["start", "signal"]
    entry: str | None = None


class NotifySub(_Strict):
    channel: str
    events: list[str] = Field(default_factory=list)


class AppSpec(_Strict):
    format: Literal["agentlab.app/1"] = "agentlab.app/1"
    name: str
    description: str = ""
    icon: str = "app"
    pins: list[Pin] = Field(default_factory=list)
    slots: dict[str, Slot] = Field(default_factory=dict)
    entries: list[Entry] = Field(default_factory=list)
    forms: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 按人工节点 id 覆盖待办的外观（标题、x-order、x-widget），不能改数据约束
    task_ui: dict[str, dict[str, Any]] = Field(default_factory=dict)
    views: dict[str, View] = Field(default_factory=dict)
    pages: list[Page] = Field(default_factory=list)
    hooks: list[Hook] = Field(default_factory=list)
    notify: list[NotifySub] = Field(default_factory=list)


def parse_spec(data: Any) -> AppSpec:
    """按形状解析一份 spec；形状不对抛 pydantic.ValidationError（lint 把它翻成 app.format）。"""
    return AppSpec.model_validate(data)


def empty_spec(name: str) -> dict[str, Any]:
    return AppSpec(name=name).model_dump(mode="json", by_alias=True)
