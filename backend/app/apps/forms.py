"""表单 agentlab.form/1：校验和输入节点字段的转换（§3.3）。

前端只校验同一份子集（lib/schemaForm.ts）；这里用 jsonschema 复核，以这里为准。错误格式两边一样：{path, code, message}。
path 是字段名（表单只有一层；数组的某一项出错也挂在这个字段上）。

子集之外的写法一律不收，最要紧的是 pattern：JS 和 Python 的正则语义不同，会出现前端放行、后端拒收（P5）。format 也不交给
jsonschema 的 FormatChecker：date-time、uri 要装额外的包才查，装没装决定查不查，两边就对不上。这里自己按同一套规则查。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, TypedDict
from urllib.parse import urlsplit

from app.apps.spec import FORM_FORMAT, FORM_FORMATS, FORM_TYPES, FORM_WIDGETS


class FormError(TypedDict):
    path: str
    code: str
    message: str


_TOP_KEYS = frozenset({"format", "type", "title", "description", "required", "properties", "x-order"})
_FIELD_KEYS = frozenset({
    "type", "title", "description", "default", "readOnly", "enum", "x-enum-labels", "minimum", "maximum",
    "minLength", "maxLength", "maxItems", "format", "items", "x-widget",
})
_NUMBER_KEYS = ("minimum", "maximum")
_COUNT_KEYS = ("minLength", "maxLength", "maxItems")
_LIMIT_LABEL = {"minimum": "最小值", "maximum": "最大值", "minLength": "最少字数", "maxLength": "最多字数",
                "maxItems": "最多选几项"}
_TYPE_LABEL = {"string": "文字", "number": "数", "integer": "整数", "boolean": "是或否", "array": "多选"}
_FORMAT_LABEL = {"date": "日期（如 2026-10-14）", "date-time": "日期时间（如 2026-10-14T09:30:00）",
                 "email": "邮箱地址", "uri": "网址（如 https://example.com）"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_form_schema(form: Any) -> list[str]:
    """表单本身合不合目录（只用允许的 type / format / x-widget，没有 pattern……）。返回问题说明，空表示合规。"""
    if not isinstance(form, dict):
        return ["表单需要是一个对象"]
    problems: list[str] = []
    if form.get("format") not in (None, FORM_FORMAT):
        problems.append(f"表单格式只能是 {FORM_FORMAT}")
    if form.get("type") != "object":
        problems.append("表单最外层的类型只能是对象")
    if extra := sorted(k for k in form if k not in _TOP_KEYS):
        problems.append(f"表单不支持这些写法：{'、'.join(extra)}")
    props = form.get("properties")
    if not isinstance(props, dict):
        return [*problems, "表单需要列出字段"]
    required = form.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        problems.append("必填项需要是字段名的列表")
    else:
        problems += [f"必填项「{r}」不是表单里的字段" for r in required if r not in props]
    order = form.get("x-order", [])
    if not isinstance(order, list) or not all(isinstance(r, str) for r in order):
        problems.append("字段顺序需要是字段名的列表")
    else:
        problems += [f"字段顺序里的「{r}」不是表单里的字段" for r in order if r not in props]
    for name, field in props.items():
        problems += [f"字段「{name}」{p}" for p in _field_problems(field)]
    return problems


def _field_problems(field: Any) -> list[str]:
    if not isinstance(field, dict):
        return ["需要是一个对象"]
    out: list[str] = []
    if "pattern" in field:
        out.append("不支持正则校验：前端和服务端的正则规则不同，会出现一边放行、一边拒收。请改用枚举、长度或格式")
    if extra := sorted(k for k in field if k not in _FIELD_KEYS and k != "pattern"):
        out.append(f"不支持这些写法：{'、'.join(extra)}")
    kind = field.get("type")
    if kind not in FORM_TYPES:
        out.append(f"的类型只能是 {' / '.join(FORM_TYPES)}")
    if "format" in field and field["format"] not in FORM_FORMATS:
        out.append(f"的格式只能是 {' / '.join(FORM_FORMATS)}")
    if "format" in field and kind != "string":
        out.append("只有文字字段能设格式")
    if "x-widget" in field and field["x-widget"] not in FORM_WIDGETS:
        out.append(f"的控件只能是 {' / '.join(FORM_WIDGETS)}")
    if "enum" in field:
        enum = field["enum"]
        if not isinstance(enum, list) or not enum or not all(isinstance(v, (str, int, float)) for v in enum):
            out.append("的可选值需要是非空的列表")
        labels = field.get("x-enum-labels")
        if labels is not None and (not isinstance(labels, list) or len(labels) != len(enum or [])):
            out.append("的选项文字需要和可选值一一对应")
    out += [f"的{_LIMIT_LABEL[k]}需要是数" for k in _NUMBER_KEYS if k in field and not _number(field[k])]
    out += [f"的{_LIMIT_LABEL[k]}需要是不小于 0 的整数" for k in _COUNT_KEYS
            if k in field and not (isinstance(field[k], int) and not isinstance(field[k], bool) and field[k] >= 0)]
    if kind == "array":
        items = field.get("items")
        if not (isinstance(items, dict) and items.get("type") == "string" and isinstance(items.get("enum"), list)
                and items["enum"] and set(items) <= {"type", "enum"}):
            out.append("是多选：只能从一组文字选项里选，需要写明可选项")
    elif "items" in field:
        out.append("不是多选，不能写可选项列表")
    return out


def _clean_schema(form: dict[str, Any]) -> dict[str, Any]:
    """交给 jsonschema 的那一份：去掉展示用的键和 format（format 自己查，见文件开头）。"""
    props: dict[str, Any] = {}
    for name, field in (form.get("properties") or {}).items():
        if not isinstance(field, dict):
            continue
        props[name] = {k: v for k, v in field.items()
                       if k in _FIELD_KEYS and k not in ("format", "x-widget", "x-enum-labels", "title",
                                                         "description", "readOnly", "default")}
    return {"type": "object", "properties": props, "required": [r for r in form.get("required") or [] if r in props]}


_CODES = {"required": "required", "type": "type", "enum": "enum", "minimum": "minimum", "maximum": "maximum",
          "minLength": "min_length", "maxLength": "max_length", "maxItems": "max_items"}


def validate_form(form: dict[str, Any], values: Any) -> list[FormError]:
    """按表单校验一份填写的值。表单本身不合目录的不在这里报（发布检查报），按能认的部分校验。

    没填（值为 None 或空串）的非必填字段不校验：前端把清空的输入框交成空串，不该报「格式不对」。"""
    from jsonschema import Draft202012Validator

    if not isinstance(values, dict):
        return [{"path": "", "code": "type", "message": "提交的内容需要是一组字段和值"}]
    props = form.get("properties") or {}
    required = [r for r in form.get("required") or [] if r in props]
    present = {k: v for k, v in values.items() if v is not None and v != ""}
    # 必填自己按顺序查（jsonschema 的 required 错误不带是哪个字段的路径）
    errors: list[FormError] = [_error(name, "required", props.get(name) or {}) for name in required
                               if name not in present]
    validator = Draft202012Validator(_clean_schema(form))
    seen: set[tuple[str, str]] = {(e["path"], e["code"]) for e in errors}
    for err in sorted(validator.iter_errors(present), key=lambda e: list(e.absolute_path)):
        code = _CODES.get(str(err.validator))
        if code is None or code == "required":
            continue
        path = str(err.absolute_path[0]) if err.absolute_path else ""
        if (path, code) in seen:
            continue
        seen.add((path, code))
        errors.append(_error(path, code, props.get(path) or {}))
    for name, value in present.items():
        field = props.get(name)
        if isinstance(field, dict) and field.get("format") and isinstance(value, str) and (name, "type") not in seen \
                and not _format_ok(field["format"], value):
            errors.append(_error(name, "format", field))
    return errors


def _format_ok(fmt: str, value: str) -> bool:
    if fmt == "date":
        try:
            return bool(_DATE.match(value)) and isinstance(date.fromisoformat(value), date)
        except ValueError:
            return False
    if fmt == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return "T" in value or " " in value
        except ValueError:
            return False
    if fmt == "email":
        return bool(_EMAIL.match(value))
    if fmt == "uri":
        parts = urlsplit(value)
        return bool(parts.scheme in ("http", "https") and parts.netloc)
    return True


def _error(path: str, code: str, field: dict[str, Any]) -> FormError:
    name = field.get("title") or path or "提交的内容"
    if code == "required":
        message = f"请填写「{name}」"
    elif code == "type":
        message = f"「{name}」需要填{_TYPE_LABEL.get(str(field.get('type')), '正确类型的值')}"
    elif code == "enum":
        message = f"「{name}」只能从给出的选项里选"
    elif code == "minimum":
        message = f"「{name}」不能小于 {field.get('minimum')}"
    elif code == "maximum":
        message = f"「{name}」不能大于 {field.get('maximum')}"
    elif code == "min_length":
        message = f"「{name}」至少要 {field.get('minLength')} 个字"
    elif code == "max_length":
        message = f"「{name}」最多 {field.get('maxLength')} 个字"
    elif code == "max_items":
        message = f"「{name}」最多选 {field.get('maxItems')} 项"
    else:
        message = f"「{name}」需要是{_FORMAT_LABEL.get(str(field.get('format')), '正确的格式')}"
    return {"path": path, "code": code, "message": message}


def input_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """输入节点的 fields（name / required / default / description / example，以及新增的 type / enum / format）→
    agentlab.form/1。没写 type 的按 string。

    有默认值的必填字段不进 required：没填时入口节点补默认值（engine/nodes/io.py 的 run_input），表单不该逼人填。
    不认识的 type、format 退回文字：老图里随手写的值不该让整张表单作废。"""
    props: dict[str, Any] = {}
    required: list[str] = []
    for f in fields or []:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not f["name"].strip():
            continue
        name = f["name"].strip()
        kind = f.get("type") if f.get("type") in FORM_TYPES else "string"
        prop: dict[str, Any] = {"type": kind, "title": str(f.get("label") or name)}
        if isinstance(f.get("description"), str) and f["description"].strip():
            prop["description"] = f["description"].strip()
        enum = f.get("enum")
        if isinstance(enum, list) and enum and all(isinstance(v, (str, int, float)) for v in enum):
            if kind == "array":
                prop["items"] = {"type": "string", "enum": [str(v) for v in enum]}
            else:
                prop["enum"] = list(enum)
        elif kind == "array":
            # 多选没写可选项的，不能当多选填：退回文字，照旧按 JSON 交（同以前的发起表单）
            prop["type"] = kind = "string"
        if kind == "string" and f.get("format") in FORM_FORMATS:
            prop["format"] = f["format"]
        default = f.get("default")
        if default not in (None, ""):
            prop["default"] = default
        props[name] = prop
        if f.get("required") and default in (None, ""):
            required.append(name)
    return {"format": FORM_FORMAT, "type": "object", "properties": props, "required": required,
            "x-order": list(props)}
