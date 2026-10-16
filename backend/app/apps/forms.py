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

from app.apps.spec import FILE_FIELD_TYPE, FILE_REF_KEYS, FORM_FORMAT, FORM_FORMATS, FORM_TYPES, FORM_WIDGETS


class FormError(TypedDict):
    path: str
    code: str
    message: str


_TOP_KEYS = frozenset({"format", "type", "title", "description", "required", "properties", "x-order"})
_FIELD_KEYS = frozenset({
    "type", "title", "description", "default", "readOnly", "enum", "x-enum-labels", "minimum", "maximum",
    "minLength", "maxLength", "maxItems", "format", "items", "x-widget",
    # 文件控件的限制：accept（类型或扩展名的列表）、max_mb（docs/design/normal-user-apps.md §3.8）
    "x-file",
    # 选人控件的候选池：{pool: app | group:<组> | slot:<岗位>}（auth/directory.py，05-已知遗留 (b)）
    "x-principal",
})
#: 文件控件只认这几项写法
_FILE_FIELD_KEYS = frozenset({"type", "title", "description", "readOnly", "x-widget", "x-file"})
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
    if field.get("x-widget") == "file" or kind == FILE_FIELD_TYPE:
        return out + _file_field_problems(field)
    if "x-file" in field:
        out.append("不是文件控件，不能写文件的限制")
    if "x-principal" in field:
        from app.auth.directory import pool_problem

        if field.get("x-widget") != "principal":
            out.append("不是选人控件，不能写候选范围")
        elif problem := pool_problem(field["x-principal"]):
            out.append(problem)
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


def _file_field_problems(field: dict[str, Any]) -> list[str]:
    """文件控件：类型只能是对象、控件只能是 file（只有这个控件能用对象），限制写在 x-file 里。"""
    out: list[str] = []
    if field.get("x-widget") != "file":
        out.append(f"的类型只能是 {' / '.join(FORM_TYPES)}（对象只有文件控件能用）")
    if field.get("type") != FILE_FIELD_TYPE:
        out.append("是文件控件，类型只能是对象")
    if extra := sorted(k for k in field if k not in _FILE_FIELD_KEYS and k != "pattern"):
        out.append(f"是文件控件，不支持这些写法：{'、'.join(extra)}")
    setting = field.get("x-file")
    if setting is not None:
        if not isinstance(setting, dict) or not set(setting) <= {"accept", "max_mb"}:
            out.append("的文件限制只能写可选的类型（accept）和大小上限（max_mb）")
        else:
            accept = setting.get("accept")
            if accept is not None and not (isinstance(accept, list) and all(isinstance(a, str) and a for a in accept)):
                out.append("的可选类型需要是类型或扩展名的列表，例如 [\".pdf\", \"image/png\"]")
            max_mb = setting.get("max_mb")
            if max_mb is not None and not (_number(max_mb) and max_mb > 0):
                out.append("的大小上限需要是大于 0 的数（MB）")
    return out


def _file_fields(form: dict[str, Any]) -> set[str]:
    return {name for name, field in (form.get("properties") or {}).items()
            if isinstance(field, dict) and (field.get("x-widget") == "file" or field.get("type") == FILE_FIELD_TYPE)}


def _clean_schema(form: dict[str, Any]) -> dict[str, Any]:
    """交给 jsonschema 的那一份：去掉展示用的键和 format（format 自己查，见文件开头）。文件控件自己查（_file_errors）。"""
    props: dict[str, Any] = {}
    files = _file_fields(form)
    for name, field in (form.get("properties") or {}).items():
        if not isinstance(field, dict) or name in files:
            continue
        props[name] = {k: v for k, v in field.items()
                       if k in _FIELD_KEYS and k not in ("format", "x-widget", "x-enum-labels", "title",
                                                         "description", "readOnly", "default", "x-principal")}
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
    errors += _file_errors(form, present)
    return errors


def _file_errors(form: dict[str, Any], present: dict[str, Any]) -> list[FormError]:
    """文件控件的值：上传后拿回的引用 {file_id, name, size, sha256, type}，按 x-file 的类型、大小核对。引用是不是这个人
    传的、有没有用过，提交时绑定那一步（files/store.py）按登记核对。"""
    from app.files.store import FILE_ERROR_CODE, accept_problem

    out: list[FormError] = []
    props = form.get("properties") or {}
    for name in sorted(_file_fields(form)):
        if name not in present:
            continue
        field = props.get(name) or {}
        label = field.get("title") or name
        value = present[name]
        if not (isinstance(value, dict) and set(value) == set(FILE_REF_KEYS) and isinstance(value.get("file_id"), str)
                and value["file_id"] and isinstance(value.get("size"), int) and isinstance(value.get("name"), str)
                and isinstance(value.get("type"), str) and isinstance(value.get("sha256"), str)):
            out.append({"path": name, "code": FILE_ERROR_CODE, "message": f"「{label}」需要先上传文件"})
            continue
        setting = field.get("x-file") if isinstance(field.get("x-file"), dict) else None
        if problem := accept_problem(setting, name=value["name"], mime=value["type"], size=value["size"]):
            out.append({"path": name, "code": FILE_ERROR_CODE, "message": f"「{label}」{problem}"})
    return out


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
    """输入节点的 fields（name / required / default / description / example，以及新增的 type / enum / format，数字类型的
    minimum / maximum）→ agentlab.form/1。没写 type 的按 string。上下限只带数字类型的、写成数的那几个：表单子集只有闭区间的
    minimum / maximum（没有 exclusiveMinimum），「大于 0」的金额写成 minimum 0.01。type 为 file 的是文件字段（可带 accept、
    max_mb，进 x-file）。

    有默认值的必填字段不进 required：没填时入口节点补默认值（engine/nodes/io.py 的 run_input），表单不该逼人填。
    不认识的 type、format 退回文字：老图里随手写的值不该让整张表单作废。"""
    props: dict[str, Any] = {}
    required: list[str] = []
    for f in fields or []:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not f["name"].strip():
            continue
        name = f["name"].strip()
        if f.get("type") == "file" or f.get("widget") == "file":
            # 文件字段（docs/design/normal-user-apps.md N9）：值是上传后拿回的引用；限制（可选类型、大小上限）照写
            upload: dict[str, Any] = {"type": FILE_FIELD_TYPE, "x-widget": "file", "title": str(f.get("label") or name)}
            if isinstance(f.get("description"), str) and f["description"].strip():
                upload["description"] = f["description"].strip()
            limits = {k: f[k] for k in ("accept", "max_mb") if f.get(k) not in (None, "", [])}
            if limits:
                upload["x-file"] = limits
            props[name] = upload
            if f.get("required"):
                required.append(name)
            continue
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
        if kind in ("number", "integer"):
            prop.update({k: f[k] for k in _NUMBER_KEYS if _number(f.get(k))})
        default = f.get("default")
        if default not in (None, ""):
            prop["default"] = default
        props[name] = prop
        if f.get("required") and default in (None, ""):
            required.append(name)
    return {"format": FORM_FORMAT, "type": "object", "properties": props, "required": required,
            "x-order": list(props)}
