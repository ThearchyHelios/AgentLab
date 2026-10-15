"""应用操作：编辑器和助手润色共用（L1 的 AppApplier 调这里）。

操作（一行一个 JSON）：
  {"op":"app_add","parent":"<组件 id>|<页面 id>","index":0,"component":{...}}
  {"op":"app_update","id":"<组件 id>","props":{...},"bind":{...},"visible":"...","hidden":true}
  {"op":"app_remove","id":"<组件 id>"}
  {"op":"app_move","id":"<组件 id>","parent":"...","index":0}
  {"op":"app_page","page":{"id","title"}}                    新增或改页面标题
  {"op":"app_set","path":"entries|views|forms|slots|task_ui","key":"…","value":{...}}

只改形状，不做语义检查：绑定的节点在不在、表达式合不合法……交给应用检查（lint.py），和图操作落地后再自查是同一个分工。
同 id 的组件已经在、内容也一样的 app_add 不算改动、不转发（look §2.5.5 的去重：模型接着写时常把落过的再写一遍）。
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

# 落地结果和图操作共用一个形状（L1 的 Applier 接口）
from app.look.base import Applied

from app.apps.spec import Component, Entry, Slot, View
from app.apps.tree import components

OPS = ("app_add", "app_update", "app_remove", "app_move", "app_page", "app_set")
_SET_PATHS = ("entries", "views", "forms", "slots", "task_ui")

#: 助手润色能用的操作（D-h）：只改呈现。app_set 另外只放行 task_ui，和 forms 里只动外观键的改动（polish_problem）
POLISH_OPS = ("app_add", "app_update", "app_remove", "app_move", "app_page")
#: 表单里的外观键：顶层和每个字段上都只能改这几个，别的（type、required、enum、取值范围、format……）是数据约束
FORM_LOOK_KEYS = frozenset({"title", "description", "x-order", "x-widget", "x-enum-labels"})
_SET_LABEL = {"entries": "入口", "views": "视图", "slots": "岗位", "pins": "固定的版本"}


def _refuse(problem: str) -> Applied:
    return Applied(False, False, problem)


def _locate(spec: dict[str, Any], cid: str) -> tuple[list[Any], int] | None:
    """组件所在的那个列表和下标。"""
    for page in spec.get("pages") or []:
        found = _find_in(page.get("body") or [], cid)
        if found:
            return found
    return None


def _find_in(items: list[Any], cid: str) -> tuple[list[Any], int] | None:
    for i, comp in enumerate(items):
        if not isinstance(comp, dict):
            continue
        if comp.get("id") == cid:
            return items, i
        found = _find_in(comp.get("children") or [], cid)
        if found:
            return found
    return None


def _container(spec: dict[str, Any], parent: Any) -> list[Any] | None:
    """parent 指的那个列表：页面 id → 页面的 body；组件 id → 它的 children（只有能放子组件的布局组件）。"""
    from app.apps.catalog import BY_NAME

    for page in spec.get("pages") or []:
        if isinstance(page, dict) and page.get("id") == parent:
            page.setdefault("body", [])
            return page["body"]
    for comp, _, _ in components(spec):
        if comp.get("id") == parent:
            if not (BY_NAME.get(str(comp.get("type"))) or {}).get("children"):
                return None
            comp.setdefault("children", [])
            return comp["children"]
    return None


def _insert(items: list[Any], index: Any, item: Any) -> None:
    at = index if isinstance(index, int) and not isinstance(index, bool) and 0 <= index <= len(items) else len(items)
    items.insert(at, item)


def _ids(spec: dict[str, Any]) -> set[str]:
    return {str(c.get("id")) for c, _, _ in components(spec)} | {
        str(p.get("id")) for p in spec.get("pages") or [] if isinstance(p, dict)}


def apply_app_op(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    """把一个应用操作应用到 spec（原地修改）。认不出、目标不存在的返回 Applied(False, False, problem)。"""
    kind = op.get("op") if isinstance(op, dict) else None
    if kind not in OPS:
        return _refuse(f"不认识的应用操作：{kind}")
    spec.setdefault("pages", [])
    return {"app_add": _add, "app_update": _update, "app_remove": _remove, "app_move": _move, "app_page": _page,
            "app_set": _set}[kind](spec, op)


def _add(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    raw = op.get("component")
    try:
        comp = Component.model_validate(raw).model_dump(mode="json", exclude_none=True)
    except ValidationError as e:
        return _refuse(f"组件的写法不对：{_first(e)}")
    comp = {k: v for k, v in comp.items() if v not in ({}, []) or k in ("id", "type")}
    existing = _locate(spec, comp["id"])
    if existing is not None:
        items, i = existing
        same = Component.model_validate(items[i]).model_dump(mode="json", exclude_none=True)
        same = {k: v for k, v in same.items() if v not in ({}, []) or k in ("id", "type")}
        return Applied(False, False) if same == comp else _refuse(f"组件「{comp['id']}」已经存在")
    if comp["id"] in _ids(spec):
        return _refuse(f"「{comp['id']}」已经是一个页面的名字，请给组件换一个名字")
    if [c for c, _, _ in components({"pages": [{"body": [comp]}]}) if c is not comp and c.get("id") in _ids(spec)]:
        return _refuse("新组件的子组件和已有的组件重名")
    items = _container(spec, op.get("parent"))
    if items is None:
        return _refuse(f"找不到可以放组件的位置「{op.get('parent')}」：只能放进页面或布局组件")
    _insert(items, op.get("index"), comp)
    return Applied(True)


def _update(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    found = _locate(spec, str(op.get("id")))
    if found is None:
        return _refuse(f"找不到组件「{op.get('id')}」")
    items, i = found
    if "hidden" in op and op["hidden"] not in (True, False, None):
        return _refuse("隐藏只能写是或否")
    before = copy.deepcopy(items[i])
    comp = items[i]
    if isinstance(op.get("props"), dict):
        props = dict(comp.get("props") or {})
        for k, v in op["props"].items():
            # 写 null 表示去掉这一项
            if v is None:
                props.pop(k, None)
            else:
                props[k] = v
        comp["props"] = props
    for key in ("bind", "visible", "scope"):
        if key in op:
            if op[key] in (None, "", {}):
                comp.pop(key, None)
            else:
                comp[key] = op[key]
    # 隐藏（normal-user-apps N6）：作者不要它，只是呈现；为真才写，取消隐藏去掉这一项（组件模型的默认值是 false）
    if "hidden" in op:
        if op["hidden"] is True:
            comp["hidden"] = True
        else:
            comp.pop("hidden", None)
    try:
        Component.model_validate(comp)
    except ValidationError as e:
        items[i] = before
        return _refuse(f"改完之后组件的写法不对：{_first(e)}")
    changed = comp != before
    return Applied(changed, changed)


def _remove(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    found = _locate(spec, str(op.get("id")))
    if found is None:
        return _refuse(f"找不到组件「{op.get('id')}」")
    items, i = found
    del items[i]
    return Applied(True)


def _move(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    cid = str(op.get("id"))
    found = _locate(spec, cid)
    if found is None:
        return _refuse(f"找不到组件「{cid}」")
    items, i = found
    comp = items[i]
    inside = {str(c.get("id")) for c, _, _ in components({"pages": [{"body": [comp]}]})}
    if op.get("parent") in inside:
        return _refuse("不能把组件移进它自己里面")
    target = _container(spec, op.get("parent"))
    if target is None:
        return _refuse(f"找不到可以放组件的位置「{op.get('parent')}」：只能放进页面或布局组件")
    if target is items and op.get("index") in (i, i + 1):
        return Applied(False, False)
    del items[i]
    index = op.get("index")
    if target is items and isinstance(index, int) and index > i:
        index -= 1
    _insert(target, index, comp)
    return Applied(True)


def _page(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    page = op.get("page")
    if not isinstance(page, dict) or not isinstance(page.get("id"), str) or not page["id"].strip():
        return _refuse("页面需要有名字")
    title = page.get("title")
    for existing in spec["pages"]:
        if isinstance(existing, dict) and existing.get("id") == page["id"]:
            if not isinstance(title, str) or existing.get("title") == title:
                return Applied(False, False)
            existing["title"] = title
            return Applied(True)
    if page["id"] in _ids(spec):
        return _refuse(f"「{page['id']}」已经是一个组件的名字，请给页面换一个名字")
    spec["pages"].append({"id": page["id"], "title": title if isinstance(title, str) else page["id"], "body": []})
    return Applied(True)


_MODELS = {"entries": Entry, "views": View, "slots": Slot}


def _set(spec: dict[str, Any], op: dict[str, Any]) -> Applied:
    from app.apps.forms import validate_form_schema

    path, key, value = op.get("path"), op.get("key"), op.get("value")
    if path not in _SET_PATHS:
        return _refuse(f"只能设置入口、视图、表单、岗位或待办外观，不能设置「{path}」")
    if not isinstance(key, str) or not key.strip():
        return _refuse("需要写明设置哪一项")
    if value is not None:
        if path in _MODELS:
            try:
                value = _MODELS[path].model_validate(value if path != "entries" else {**value, "id": key}) \
                    .model_dump(mode="json", by_alias=True, exclude_none=True)
            except (ValidationError, TypeError) as e:
                return _refuse(f"写法不对：{_first(e) if isinstance(e, ValidationError) else e}")
        elif path == "forms":
            if problems := validate_form_schema(value):
                return _refuse(f"表单不合规：{problems[0]}")
        elif not isinstance(value, dict):
            return _refuse("需要是一个对象")
    if path == "entries":
        entries = spec.setdefault("entries", [])
        at = next((i for i, e in enumerate(entries) if isinstance(e, dict) and e.get("id") == key), None)
        if value is None:
            if at is None:
                return Applied(False, False)
            del entries[at]
            return Applied(True)
        if at is not None:
            if entries[at] == value:
                return Applied(False, False)
            entries[at] = value
        else:
            entries.append(value)
        return Applied(True)
    table = spec.setdefault(path, {})
    if value is None:
        if key not in table:
            return Applied(False, False)
        del table[key]
        return Applied(True)
    if table.get(key) == value:
        return Applied(False, False)
    table[key] = value
    return Applied(True)


def _first(e: ValidationError) -> str:
    err = e.errors()[0] if e.errors() else {}
    where = ".".join(str(x) for x in err.get("loc") or ())
    return f"{where} {err.get('msg', '')}".strip()


# --------------------------------------------------------------------------
# 助手润色的边界（D-h）
# --------------------------------------------------------------------------


def polish_problem(spec: dict[str, Any], op: dict[str, Any]) -> str | None:
    """助手润色时这个操作有没有越界：越界返回交回模型的一句话，没越界返回 None。

    润色只改呈现（布局、文案、控件，A8 第 2 条）。钉住的版本、入口、视图、岗位决定的是这个应用发起什么、读哪次运行、
    派给谁，表单的数据约束决定收什么值——这些要人在编辑器里改、发布时过检查，助手改了会悄悄换掉应用的承诺。
    编辑器自己调 apply_app_op 不受这一层限制。"""
    kind = op.get("op") if isinstance(op, dict) else None
    if kind != "app_set":
        return None
    path, key = op.get("path"), op.get("key")
    if path == "task_ui":
        return None
    if path == "forms":
        return _form_look_problem(spec, key, op.get("value"))
    what = _SET_LABEL.get(str(path), str(path))
    return (f"润色只改呈现，不能改{what}：{what}决定应用发起什么、读哪次运行、派给谁，要由人在编辑器里改。"
            "请只用组件操作、待办外观（task_ui）和表单的外观键")


def _looks_stripped(form: Any) -> Any:
    """去掉外观键之后的表单：两份表单去掉外观键后一样，就只差在外观上。"""
    if not isinstance(form, dict):
        return form
    out = {k: v for k, v in form.items() if k not in FORM_LOOK_KEYS}
    props = form.get("properties")
    if isinstance(props, dict):
        out["properties"] = {name: ({k: v for k, v in field.items() if k not in FORM_LOOK_KEYS}
                                    if isinstance(field, dict) else field) for name, field in props.items()}
    return out


def _form_look_problem(spec: dict[str, Any], key: Any, value: Any) -> str | None:
    forms = spec.get("forms") if isinstance(spec.get("forms"), dict) else {}
    if not isinstance(key, str) or key not in forms:
        return f"润色不能新增表单「{key}」：表单决定入口收什么值，要由人在编辑器里加"
    if value is None:
        return f"润色不能删掉表单「{key}」"
    if _looks_stripped(forms[key]) != _looks_stripped(value):
        return (f"表单「{key}」只能改外观（{'、'.join(sorted(FORM_LOOK_KEYS))}），不能改字段、类型、必填、可选值、"
                "取值范围这些数据约束")
    return None

