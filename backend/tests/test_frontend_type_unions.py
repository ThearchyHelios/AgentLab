"""前端约定（frontend/src/typesApp.ts、typesBlueprint.ts）里的取值和服务端的常量逐个对上。

前端的联合类型是照服务端的常量抄的：服务端加了一种（待办种类 review、运行来源 trial / personal_app、查看对象 caliber、
表单错误 file……），前端没跟上，界面就把它当成别的、或者写不出名字；反过来前端多写了一种，界面就在等一个永远不会来的值。
这里直接读两份 .ts 文件，把联合类型里的字面量和服务端的常量逐个比（能比顺序的连顺序一起比），改了一边没改另一边就过不去。
"""
from __future__ import annotations

import re
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TYPES_APP = ROOT / "frontend" / "src" / "typesApp.ts"
TYPES_BLUEPRINT = ROOT / "frontend" / "src" / "typesBlueprint.ts"
TYPES = ROOT / "frontend" / "src" / "types.ts"

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _source(path: Path) -> str:
    """去掉注释的源码：注释里举例写的字面量不算进联合类型。"""
    return _COMMENT.sub("", path.read_text(encoding="utf-8"))


def _union(path: Path, name: str) -> list[str]:
    """`export type Name = | 'a' | 'b'` 里的字面量，按写的顺序。"""
    text = _source(path)
    found = re.search(rf"export type {name} =(.*?)(?=\nexport |\n\n|\Z)", text, re.S)
    assert found is not None, f"{path.name} 里没有 export type {name}"
    return re.findall(r"'([^']+)'", found.group(1))


def _field(path: Path, interface: str, field: str) -> list[str]:
    """`export interface X { field: 'a' | 'b' }` 里这一项的字面量，按写的顺序。"""
    text = _source(path)
    body = re.search(rf"export interface {interface}\b[^{{]*\{{(.*?)\n\}}", text, re.S)
    assert body is not None, f"{path.name} 里没有 export interface {interface}"
    line = re.search(rf"^\s*{re.escape(field)}\??: ([^\n]+)$", body.group(1), re.M)
    assert line is not None, f"{interface} 里没有 {field}"
    return re.findall(r"'([^']+)'", line.group(1))


def test_task_kinds_and_statuses():
    from app.db.models_process import TASK_KINDS, TASK_STATUSES

    assert _union(TYPES_APP, "TaskKind") == list(TASK_KINDS), "待办种类（含 review）"
    assert _union(TYPES_APP, "TaskStatus") == list(TASK_STATUSES)


def test_origin_kinds_in_the_same_order():
    from app.engine.origin import ORIGIN_KINDS

    # 运行列表按来源筛选的下拉照这个顺序（lib/terms.ORIGIN_LABEL 的类型是 Record<OriginKind, string>，少一个编译不过）
    assert _union(TYPES_APP, "OriginKind") == list(ORIGIN_KINDS)


def test_look_object_kinds():
    from app.look.base import LOOK_KINDS

    assert _field(TYPES_APP, "LookObject", "kind") == list(LOOK_KINDS), "查看对象（含口径卡 caliber）"


def test_form_contract():
    from app.apps.spec import FILE_FIELD_TYPE, FORM_ERROR_CODES, FORM_FORMATS, FORM_TYPES, FORM_WIDGETS

    assert _field(TYPES_APP, "FormError", "code") == list(FORM_ERROR_CODES), "表单错误（含文件控件的 file）"
    assert _field(TYPES_APP, "FormField", "type") == [*FORM_TYPES, FILE_FIELD_TYPE], "字段类型：object 只给文件控件"
    assert _union(TYPES_APP, "FormWidget") == list(FORM_WIDGETS)
    assert _field(TYPES_APP, "FormField", "format") == list(FORM_FORMATS)
    file_ref = re.search(r"export interface FileRef \{([^}]*)\}", _source(TYPES_BLUEPRINT))
    assert file_ref is not None
    from app.apps.spec import FILE_REF_KEYS

    assert re.findall(r"(\w+):", file_ref.group(1)) == list(FILE_REF_KEYS), "文件引用的五个键"


def test_app_view_source_kinds():
    from app.apps.spec import ViewSource

    kinds = list(typing.get_args(ViewSource.model_fields["kind"].annotation))
    assert _field(TYPES_APP, "AppViewSource", "kind") == kinds, "视图来源（个人应用的 exploratory）"
    # 解析出来的来源另有试运行预览的 trial（apps/resolve.Source.out）
    assert _field(TYPES_APP, "ViewSourceState", "kind") == [*kinds, "trial"]


def test_blueprint_enums():
    from app.blueprints.publish_plan import LEVELS, PROBLEM_WHO, STEP_KINDS
    from app.blueprints.spec import BP_OPS, ITEM_SOURCES, ITEM_STATUSES
    from app.blueprints.templates import JOINT_KINDS
    from app.db.models_blueprints import BLUEPRINT_SCOPES, BLUEPRINT_SOURCES, BLUEPRINT_STATUSES, PLAN_STATUSES

    assert _union(TYPES_BLUEPRINT, "ItemSource") == list(ITEM_SOURCES)
    assert _union(TYPES_BLUEPRINT, "ItemStatus") == list(ITEM_STATUSES)
    assert _union(TYPES_BLUEPRINT, "BpOpName") == list(BP_OPS)
    assert _union(TYPES_BLUEPRINT, "BlueprintScope") == list(BLUEPRINT_SCOPES)
    assert _union(TYPES_BLUEPRINT, "BlueprintStatus") == list(BLUEPRINT_STATUSES)
    assert _union(TYPES_BLUEPRINT, "BlueprintSourceKind") == list(BLUEPRINT_SOURCES)
    assert _union(TYPES_BLUEPRINT, "JointKind") == list(JOINT_KINDS)
    assert _union(TYPES_BLUEPRINT, "PlanStepKind") == list(STEP_KINDS), "发布单的步骤（顺序即执行顺序）"
    assert _field(TYPES_BLUEPRINT, "PublishPlanRow", "status") == list(PLAN_STATUSES)
    assert _field(TYPES_BLUEPRINT, "PublishPlanRow", "level") == list(LEVELS)
    assert _union(TYPES_BLUEPRINT, "ProblemWho") == list(PROBLEM_WHO)


def test_trial_refusal_codes():
    from app.blueprints import trial

    assert _union(TYPES_BLUEPRINT, "TrialRefusalCode") == [trial.NOT_READY, trial.NO_GRAPH, trial.POLICY, trial.FORM,
                                                          trial.NO_RUN]


def test_record_rows_carry_every_system_column():
    from app.records import SYSTEM_COLUMNS

    row = re.search(r"export interface RecordRow \{(.*?)\n\}", _source(TYPES_BLUEPRINT), re.S)
    assert row is not None
    fields = re.findall(r"^\s*(\w+)\??:", row.group(1), re.M)
    assert set(SYSTEM_COLUMNS) <= set(fields), f"RecordRow 少了系统列：{set(SYSTEM_COLUMNS) - set(fields)}"
    assert "_version" in fields, "这一行在第几版表里（records/listing._shown）"


def test_conversation_kinds_include_what_the_server_creates():
    # 前端建的只有 chat、canvas（api/conversations.ConversationIn）；服务端另建应用助手的 app、蓝图对话的 blueprint
    assert _field(TYPES, "Conversation", "kind") == ["chat", "canvas", "app", "blueprint"]
