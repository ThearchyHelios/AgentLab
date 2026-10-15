"""按模板实例化蓝图（N3、§3.3–§3.4）：确定性关节按卡片填，生成关节留给模型（TemplateApplier 的 fill_joint），
实例化后的图和骨架逐节点比对，只许关节位置不同。模型失败时生成关节留空、标「待填」（P3）。

实例化是纯函数：不读库、不落库。读库的那几样由调用方先读好放进上下文（工作流 id、蓝图 id、定时任务 id、记录表、
自由形导入的图）。图按画布保存时的样子规范化（GraphSpec 解析、自动排版、序列化）：图哈希、试运行和发布单按哈希对齐
（N4）都认这一份。

生成关节的值和依据存在蓝图的 joints 上（{joint_id: {status, by, note?, value?, basis?}}）：卡片改了、这一组关节的
说明变了（basis 对不上），之前填的就不算数，重新标「待填」（filled_from）。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: 关节的状态
JOINT_STATUSES = ("filled", "pending", "refused")
#: 骨架比对的问题编号
SKELETON_CHANGED = "tpl.skeleton_changed"
JOINT_UNFILLED = "tpl.joint_unfilled"


@dataclass
class Instantiated:
    #: 实例化出来的图（GraphSpec 的 JSON）。生成关节没填的位置是占位
    graph: dict[str, Any]
    #: {joint_id: {"status": filled|pending|refused, "by": template|assistant|user, "note"?: …}}
    joints: dict[str, Any]
    #: 按模板排布的应用草稿（agentlab.app/1），钉住项的 version 为空（还没发布）
    app_spec: dict[str, Any]
    #: 定时任务参数（看板类）或 None
    schedule: dict[str, Any] | None
    problems: list[dict[str, Any]] = field(default_factory=list)
    #: 岗位绑定（应用的 draft_bindings 形状）：「谁来做」里写了具体的人或组的那几个岗位
    bindings: dict[str, Any] = field(default_factory=dict)
    #: 模板的骨架（规范化后的、生成关节是占位的那张图）：骨架比对、TemplateChecker 用
    skeleton: dict[str, Any] = field(default_factory=dict)
    #: 模板声明的关节（blueprints/templates.Joint）
    joint_defs: list[Any] = field(default_factory=list)
    #: 每组生成关节给模型的说明 {group: 说明}，和依据的指纹 {group: basis}
    briefs: dict[str, str] = field(default_factory=dict)
    basis: dict[str, str] = field(default_factory=dict)


# ==========================================================================
# 图上的位置（关节的 field 是节点配置里的 JSON 指针）
# ==========================================================================


def _tokens(pointer: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")] if pointer.strip("/") else []


def read_at(config: Any, pointer: str) -> Any:
    cur = config
    for token in _tokens(pointer):
        if isinstance(cur, list) and token.isdigit() and int(token) < len(cur):
            cur = cur[int(token)]
        elif isinstance(cur, dict):
            cur = cur.get(token)
        else:
            return None
    return cur


def write_at(config: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = _tokens(pointer)
    cur: Any = config
    for token in tokens[:-1]:
        cur = cur[int(token)] if isinstance(cur, list) else cur.setdefault(token, {})
    if isinstance(cur, list):
        cur[int(tokens[-1])] = value
    else:
        cur[tokens[-1]] = value


def _blank_at(config: dict[str, Any], pointer: str) -> None:
    """骨架比对时把关节位置抹掉：两边都抹，只比别的地方。"""
    tokens = _tokens(pointer)
    cur: Any = config
    for token in tokens[:-1]:
        if isinstance(cur, list) and token.isdigit() and int(token) < len(cur):
            cur = cur[int(token)]
        elif isinstance(cur, dict) and token in cur:
            cur = cur[token]
        else:
            return
    if isinstance(cur, list) and tokens[-1].isdigit() and int(tokens[-1]) < len(cur):
        cur[int(tokens[-1])] = None
    elif isinstance(cur, dict):
        cur.pop(tokens[-1], None)


def node_config(graph: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    for n in graph.get("nodes") or []:
        if isinstance(n, dict) and n.get("id") == node_id:
            data = n.setdefault("data", {})
            if not isinstance(data.get("config"), dict):
                data["config"] = {}
            return data["config"]
    return None


def normalize(graph: dict[str, Any]) -> dict[str, Any]:
    """按画布保存时的样子规范化：GraphSpec 解析、自动排版、序列化。坐标由结构定，同一张图每次都一样。"""
    from app.engine.layout import auto_layout
    from app.engine.schema import GraphSpec

    return auto_layout(GraphSpec.model_validate(graph)).model_dump(mode="json")


# ==========================================================================
# 骨架比对
# ==========================================================================


def skeleton_changes(template_graph: dict[str, Any], graph: dict[str, Any], joints: list[Any]) -> list[dict[str, Any]]:
    """骨架比对：除关节位置外的任何差异都交回（tpl.skeleton_changed）。照 engine/autofix.forbidden_changes 的写法逐节点比：
    节点增删、换类型、改标题、配置里关节以外的地方、连线、全图默认。坐标不比（排版的事）。"""
    from app.engine.schema import type_label

    def nodes_of(g: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(n["id"]): n for n in g.get("nodes") or [] if isinstance(n, dict) and n.get("id")}

    def title(n: dict[str, Any]) -> str:
        return str((n.get("data") or {}).get("label") or n.get("id"))

    old, new = nodes_of(template_graph), nodes_of(graph)
    at: dict[str, list[str]] = {}
    for j in joints:
        at.setdefault(j.node_id, []).append(j.field)
    out: list[dict[str, Any]] = []

    def add(message: str, node_id: str | None = None, field: str | None = None) -> None:
        out.append({"code": SKELETON_CHANGED, "level": "error", "node_id": node_id, "field": field,
                    "message": f"改动超出了模板的关节：{message}"})

    for nid, n in old.items():
        if nid not in new:
            add(f"删除了节点「{title(n)}」", nid)
    for nid, n in new.items():
        was = old.get(nid)
        if was is None:
            add(f"多出了节点「{title(n)}」", nid)
            continue
        if was.get("type") != n.get("type"):
            add(f"把「{title(was)}」从{type_label(was.get('type'))}换成了{type_label(n.get('type'))}", nid)
        if title(was) != title(n):
            add(f"改了节点「{title(was)}」的标题", nid)
        before = copy.deepcopy((was.get("data") or {}).get("config") or {})
        after = copy.deepcopy((n.get("data") or {}).get("config") or {})
        for pointer in at.get(nid, []):
            _blank_at(before, pointer)
            _blank_at(after, pointer)
        if before != after:
            keys = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            add(f"改了「{title(n)}」关节以外的配置（{'、'.join(keys)}）", nid, keys[0] if keys else None)

    def edges_of(g: dict[str, Any]) -> set[tuple[str, str, str]]:
        return {(str(e.get("source")), str(e.get("target")), str(e.get("sourceHandle") or ""))
                for e in g.get("edges") or [] if isinstance(e, dict)}

    for s, t, _ in sorted(edges_of(template_graph) - edges_of(graph)):
        add(f"删除了连线「{title(old.get(s) or {'id': s})}」→「{title(old.get(t) or {'id': t})}」")
    for s, t, _ in sorted(edges_of(graph) - edges_of(template_graph)):
        add(f"多出了连线「{title(new.get(s) or {'id': s})}」→「{title(new.get(t) or {'id': t})}」")
    if (template_graph.get("defaults") or {}) != (graph.get("defaults") or {}):
        add("改了全图默认")
    return out


# ==========================================================================
# 实例化
# ==========================================================================


def _spec_of(blueprint: Any) -> dict[str, Any]:
    from app.blueprints.spec import parse_blueprint

    raw = getattr(blueprint, "spec", blueprint)
    return parse_blueprint(raw or {}).model_dump(mode="json", by_alias=True)


def instantiate(blueprint: dict[str, Any], *, filled: dict[str, Any] | None = None, name: str | None = None,
                workflow_id: str | None = None, blueprint_id: str | None = None, schedule_id: str | None = None,
                records: dict[str, str] | None = None, graph: dict[str, Any] | None = None) -> Instantiated:
    """蓝图 + 已填的生成关节（{joint_id: value}）→ 图、关节状态、应用草稿、定时任务参数。纯函数，不落库。

    blueprint 是 agentlab.blueprint/1（或带 spec 的蓝图行）。另几样是调用方读好的上下文：name 是应用和工作流的名字；
    workflow_id 给了，应用草稿的钉住项挂在这个工作流上，没给挂在蓝图上（blueprint_id）；schedule_id 是看板视图要绑的
    定时任务；records 是填报登记的记录表 {table, source}；graph 是自由形导入的图。"""
    from app.blueprints.ops import blueprint_issues
    from app.blueprints.templates import GENERATED_JOINT_KINDS, get_template
    from app.blueprints.templates import _common as c

    spec = _spec_of(blueprint)
    ref = spec.get("template") or {}
    template = get_template(str(ref.get("id") or ""))
    problems = [i for i in blueprint_issues(spec, for_="trial") if i["level"] == "error"]
    if template is None:
        from app.apps.spec import empty_spec

        return Instantiated(graph={"nodes": [], "edges": []}, joints={}, app_spec=empty_spec(name or "应用"),
                            schedule=None, problems=problems)
    bp = {**copy.deepcopy(spec), c.CONTEXT_KEY: {
        "name": name, "workflow_id": workflow_id, "blueprint_id": blueprint_id, "schedule_id": schedule_id,
        "records": records, "graph": graph}}
    raw, joint_defs = template.skeleton(bp)
    # 自由形导入的图原样用、不重新排版：它的哈希要和导入的那一版对得上，发布单才按哈希复用那一版
    skeleton = copy.deepcopy(raw) if template.imports_graph else normalize(raw)
    out = copy.deepcopy(skeleton)
    briefs = template.briefs(bp) if template.briefs is not None else {}
    basis = {g: c.basis_of(text, template.id, template.version) for g, text in briefs.items()}
    joints: dict[str, Any] = {}
    filled = filled or {}
    for j in joint_defs:
        # 模板声明的说法、类型、分组随状态一起存（界面写「第几组、填的是什么」，不只写关节 id）
        said = {"label": j.label or j.id, "kind": j.kind, "group": j.group}
        if j.kind not in GENERATED_JOINT_KINDS:
            joints[j.id] = {"status": "filled", "by": "template", **said}
            continue
        value = filled.get(j.id)
        config = node_config(out, j.node_id)
        if isinstance(value, str) and value.strip() and config is not None:
            write_at(config, j.field, value.strip())
            joints[j.id] = {"status": "filled", "by": "assistant", "value": value.strip(), **said,
                            **({"basis": basis[j.group]} if j.group in basis else {})}
        else:
            joints[j.id] = {"status": "pending", "by": "template", **said,
                            "note": "这一项助手还没填上：可以请助手填，或者换个说法再试"}
            problems.append({"code": JOINT_UNFILLED, "level": "error", "node_id": j.node_id, "field": j.field,
                             "joint": j.id, "message": f"「{j.label or j.id}」还没填"})
    problems += skeleton_changes(skeleton, out, joint_defs)
    problems += _graph_errors(out)
    app_spec = template.app_skeleton(bp, out) if template.app_skeleton is not None else {}
    schedule = template.schedule(bp) if template.schedule is not None else None
    return Instantiated(graph=out, joints=joints, app_spec=app_spec, schedule=schedule,
                        problems=_dedupe(problems), bindings=c.bindings_of(bp), skeleton=skeleton,
                        joint_defs=list(joint_defs), briefs=briefs, basis=basis)


def _graph_errors(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """实例化出来的图过画布校验（engine/schema.validate_graph）：error 照实交回。"""
    from app.engine.schema import GraphSpec, validate_graph

    return [i.model_dump() for i in validate_graph(GraphSpec.model_validate(graph)).issues if i.level == "error"]


def _dedupe(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for p in problems:
        key = (p.get("code"), p.get("path"), p.get("node_id"), p.get("field"), p.get("message"))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def filled_from(blueprint: dict[str, Any], joints: dict[str, Any] | None, *,
                graph: dict[str, Any] | None = None, records: dict[str, str] | None = None) -> dict[str, Any]:
    """蓝图上存的生成关节里还算数的值：状态是已填、依据（这一组的说明的指纹）没变。卡片改过、说明变了的不算（要重新填）。"""
    probe = instantiate(blueprint, records=records, graph=graph)
    out: dict[str, Any] = {}
    for j in probe.joint_defs:
        state = (joints or {}).get(j.id) or {}
        if state.get("status") == "filled" and isinstance(state.get("value"), str) \
                and state.get("basis") == probe.basis.get(j.group or ""):
            out[j.id] = state["value"]
    return out


def generated_groups(inst: Instantiated) -> dict[str, list[Any]]:
    """生成关节按组：{group: [Joint]}。不同组的并行填（N11）。"""
    from app.blueprints.templates import GENERATED_JOINT_KINDS

    out: dict[str, list[Any]] = {}
    for j in inst.joint_defs:
        if j.kind in GENERATED_JOINT_KINDS:
            out.setdefault(j.group or j.id, []).append(j)
    return out


def trial_inputs(blueprint: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
    """试运行的输入（N4）：看板类按「多久更新」算出最近一期（和定时任务到点渲染的是同一份），流程类没有（用户填示例表单）。"""
    from datetime import timezone

    from app.scheduler import service

    inst = instantiate(blueprint)
    if inst.schedule is None:
        return {}
    moment = at or datetime.now(timezone.utc)
    values = service.template_values(moment, inst.schedule["timezone"], inst.schedule["cron"])
    return service.render_inputs(inst.schedule["input_template"], values)


# ==========================================================================
# 模板登记的核对（启动时、测试里）
# ==========================================================================


def registry_problems() -> list[str]:
    """模板登记齐不齐（templates.check_registry 调这里）：首批都登记了、关节类型都认得、每个模板都给了示例蓝图
    （TemplateDef.example / example_fill），示例实例化出来过得了画布校验、骨架比对没有差异（模板定义、实例化、测试
    三样都要给，缺一样启动即报错，N3）。"""
    from app.blueprints.templates import JOINT_KINDS, TEMPLATE_IDS, get_template, list_templates

    problems = [f"模板「{t}」没有登记" for t in TEMPLATE_IDS if get_template(t) is None]
    for template in list_templates():
        if template.example is None or template.example_fill is None:
            problems.append(f"模板「{template.id}」没有示例蓝图")
            continue
        try:
            graph = None
            if template.imports_graph:
                from app.blueprints.templates.freeform import example_graph

                graph = example_graph()
            inst = instantiate(template.example(), filled=template.example_fill(), graph=graph)
        except Exception as e:  # noqa: BLE001 - 核对本身出错也照实报，不让启动卡在异常上
            problems.append(f"模板「{template.id}」的示例实例化出错：{e}")
            continue
        problems += [f"模板「{template.id}」的关节类型「{j.kind}」不认得" for j in inst.joint_defs
                     if j.kind not in JOINT_KINDS]
        problems += [f"模板「{template.id}」的示例：{p['message']}" for p in inst.problems if p["level"] == "error"]
    return problems
