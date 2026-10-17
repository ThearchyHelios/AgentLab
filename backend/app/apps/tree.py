"""spec 的读法：页面树的遍历、表单引用、钉住项。检查、操作、派生、解析共用，说法只有一处。"""
from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable, Iterator
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


#: 图里引用岗位的几处：人工节点派给谁、人工节点超时升级给谁、调用工具节点（写入动作）的发送确认派给谁
SLOT_ROLES = ("assignee", "escalation", "confirm")


def slot_refs(graph: Any, writes: Collection[str] = ()) -> list[dict[str, str]]:
    """图里引用到的岗位：[{slot, node_id, title, role}]，role 见 SLOT_ROLES，按节点出现的顺序。派生（列出岗位）和检查
    （岗位没声明、没绑定）共用这一份：以前两边都只认人工节点的派给谁，超时升级的岗位、动作确认的岗位一个也没列，发布后
    升级、确认悄悄派给了任何人。

    writes 是写外部系统的调用工具节点（pins.write_nodes，和运行时同一个认法）：只有它们开发送确认卡，派给谁才算用到岗位。
    只读的调用工具节点从不开确认卡，它写的（或从图级 defaults 继承来的）派给谁不算——不然派生多出用不到的岗位，检查说
    「调用工具节点「取数」的发送确认派给它」，可取数从不要人确认。

    取值规则和运行时一样（NodeContext.cfg）：节点上没写的取图级 defaults。核实岗位（reconciler）不在图里——写入能力没登记
    查询时才要，派生和检查按能力另算。"""
    defaults = (graph or {}).get("defaults") or {}
    out: list[dict[str, str]] = []
    for nid, node in node_map(graph).items():
        if node["type"] != "human" and not (node["type"] == "tool" and nid in writes):
            continue
        cfg = node["config"]

        def pick(key: str) -> Any:
            value = cfg.get(key)
            return defaults.get(key) if value in (None, "") else value

        assignee = pick("assignee")
        if isinstance(assignee, dict) and assignee.get("kind") == "slot" and str(assignee.get("slot") or "").strip():
            out.append({"slot": str(assignee["slot"]).strip(), "node_id": nid, "title": node["title"],
                        "role": "assignee" if node["type"] == "human" else "confirm"})
        timeout = pick("on_timeout") if node["type"] == "human" else None
        if isinstance(timeout, dict) and timeout.get("action") == "escalate" \
                and str(timeout.get("to_slot") or "").strip():
            out.append({"slot": str(timeout["to_slot"]).strip(), "node_id": nid, "title": node["title"],
                        "role": "escalation"})
    return out


#: 按用到它的节点给岗位起显示名（slot_label）：派给它的写节点名，超时升级给它的写「…超时升级」（同蓝图模板 slot_labels 的
#: 叫法），动作确认派给它的写「…发送前确认」
_SLOT_USE_LABEL = {"assignee": "{title}", "escalation": "{title}超时升级", "confirm": "{title}发送前确认"}


def slot_label(slot: str, refs: Iterable[dict[str, str]]) -> str:
    """岗位给人看的名字（应用里岗位的显示名）。图里只写了岗位名——画布上随手写的 id，常是英文（finance）：以前显示名就是
    它，看着像起好的名字。现在按用到它的节点在图上的名字拼（refs 是 slot_refs 的结果，只看这个岗位的那几条，几处用到用顿号
    连）；节点没起名字（名字就是节点 id）、或者没有节点用到它的，照实用岗位名——界面上显示名和岗位名一样时提示还没起名字。"""
    names = [_SLOT_USE_LABEL[r["role"]].format(title=r["title"]) for r in refs
             if r.get("slot") == slot and r.get("role") in _SLOT_USE_LABEL and r.get("title")
             and r["title"] != r.get("node_id")]
    return "、".join(dict.fromkeys(names)) or slot


def input_fields(graph: Any) -> list[dict[str, Any]]:
    """入口节点声明的字段（原样，含新增的 type / enum / format / description）。"""
    out: list[dict[str, Any]] = []
    for node in node_map(graph).values():
        if node["type"] == "input":
            out += [f for f in node["config"].get("fields") or []
                    if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"].strip()]
    return out
