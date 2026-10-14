"""spec 的读法：页面树的遍历、表单引用、钉住项。检查、操作、派生、解析共用，说法只有一处。"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

from app.core.artifact_store import canonical_json

#: 表单引用的写法：{"$ref": "#/forms/<名字>"}
FORM_REF_PREFIX = "#/forms/"


def components(spec: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], str]]:
    """每个组件：(组件, 所在页面, 在 spec 里的位置 pages[0].body[2].children[1])，深度优先、按出现顺序。"""
    for pi, page in enumerate(spec.get("pages") or []):
        if not isinstance(page, dict):
            continue
        yield from _walk(page.get("body") or [], page, f"pages[{pi}].body")


def _walk(items: list[Any], page: dict[str, Any], at: str) -> Iterator[tuple[dict[str, Any], dict[str, Any], str]]:
    for i, comp in enumerate(items):
        if not isinstance(comp, dict):
            continue
        here = f"{at}[{i}]"
        yield comp, page, here
        yield from _walk(comp.get("children") or [], page, f"{here}.children")


def page_of(spec: dict[str, Any], page_id: str) -> dict[str, Any] | None:
    return next((p for p in spec.get("pages") or [] if isinstance(p, dict) and p.get("id") == page_id), None)


def resolve_form(spec: dict[str, Any], form: Any) -> dict[str, Any] | None:
    """内联的表单原样交回；{"$ref": "#/forms/x"} 换成 spec.forms.x；引用不到的是 None。"""
    if not isinstance(form, dict):
        return None
    ref = form.get("$ref")
    if ref is None:
        return form
    if isinstance(ref, str) and ref.startswith(FORM_REF_PREFIX):
        found = (spec.get("forms") or {}).get(ref[len(FORM_REF_PREFIX):])
        return found if isinstance(found, dict) else None
    return None


def pins_by_alias(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["alias"]: p for p in spec.get("pins") or [] if isinstance(p, dict) and p.get("alias")}


def entry_of(spec: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
    return next((e for e in spec.get("entries") or [] if isinstance(e, dict) and e.get("id") == entry_id), None)


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()


def app_level(pins: list[dict[str, Any]]) -> str:
    """应用的级别取各钉住项里最低的那一级：全部是受管级，应用才算受管级（A1）。没有钉住项的算已发布级。"""
    levels = [p.get("level") for p in pins]
    return "governed" if levels and all(lv == "governed" for lv in levels) else "published"


def bind_kind(bind: Any) -> str | None:
    """绑定的种类：bind 里出现哪个键就是哪种（spec.BINDING_KINDS）。"""
    from app.apps.spec import BINDING_KINDS

    if not isinstance(bind, dict):
        return None
    return next((k for k in BINDING_KINDS if k in bind), None)


def node_map(graph: Any) -> dict[str, dict[str, Any]]:
    """图里的节点：id → {type, title, config}。图的原始 JSON 形状（data.label / data.config）。"""
    out: dict[str, dict[str, Any]] = {}
    for n in (graph or {}).get("nodes") or []:
        if isinstance(n, dict) and n.get("id"):
            data = n.get("data") or {}
            out[str(n["id"])] = {"type": n.get("type"), "title": data.get("label") or n["id"],
                                 "config": data.get("config") or {}}
    return out


def input_fields(graph: Any) -> list[dict[str, Any]]:
    """入口节点声明的字段（原样，含新增的 type / enum / format / description）。"""
    out: list[dict[str, Any]] = []
    for node in node_map(graph).values():
        if node["type"] == "input":
            out += [f for f in node["config"].get("fields") or []
                    if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"].strip()]
    return out
