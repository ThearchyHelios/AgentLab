"""表单 agentlab.form/1：校验和输入节点字段的转换（§3.3）。

前端只校验同一份子集（lib/schemaForm.ts）；这里用 jsonschema 复核，以这里为准。错误格式两边一样：{path, code, message}。
"""
from __future__ import annotations

from typing import Any, TypedDict


class FormError(TypedDict):
    path: str
    code: str
    message: str


def validate_form_schema(form: Any) -> list[str]:
    """表单本身合不合目录（只用允许的 type / format / x-widget，没有 pattern……）。返回问题说明，空表示合规。"""
    raise NotImplementedError("BE-3")


def validate_form(form: dict[str, Any], values: Any) -> list[FormError]:
    """按表单校验一份填写的值。"""
    raise NotImplementedError("BE-3")


def input_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """输入节点的 fields（name / required / default / description / example，以及新增的 type / enum / format）→
    agentlab.form/1。没写 type 的按 string。"""
    raise NotImplementedError("BE-3")
