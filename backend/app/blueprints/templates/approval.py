"""审批流程 approval（§3.3）：入口（表单）→ [按金额线分支] → 人工（一级或多级）→ [写回] → 已通过 / 已退回。

没有生成关节，全部按卡片确定性地填：
- 表单（填什么）→ 入口节点的字段；
- 每一步（谁来做）→ 一个人工节点：派给谁（岗位）、几个人同意、时限、超时怎么办；写了条件的那一步前面加一个分支，
  条件只许「字段 比较 常量」，不满足的直接跳到下一步；
- 写回（可选）→ 调用工具节点：目标能力、字段对应，要人确认时派给最后一步的岗位。

任何一步驳回都到「已退回」。不能做成个人应用：要有人审批。
"""
from __future__ import annotations

from typing import Any

from app.blueprints.templates import Joint, TemplateDef, register
from app.blueprints.templates import _common as c

ID = "approval"
VERSION = 1
APPROVED = "approved"
REJECTED = "rejected"
WRITE = "writeback"


def skeleton(bp: dict[str, Any]) -> tuple[dict[str, Any], list[Joint]]:
    nodes = [c.node(c.START, "input", "发起", fields=c.form_fields(bp))]
    joints = [Joint(id="start.fields", kind="fields", node_id=c.START, field="/fields", from_card="/fields",
                    label="表单")]
    steps = bp.get("people") or []
    writeback = c.value((bp.get("writeback") or {}).get("capability"))
    chain = c.approval_chain(bp, after=WRITE if writeback else APPROVED, rejected=REJECTED)
    nodes += chain.nodes
    joints += chain.joints
    edges = [c.edge(c.START, chain.head), *chain.edges]
    if writeback:
        write, write_joints, _ = c.write_node(bp, WRITE, confirm_slot=c.slot_of(steps[-1]) if steps else None)
        nodes.append(write)
        joints += write_joints
        edges.append(c.edge(WRITE, APPROVED))
    nodes.append(c.node(APPROVED, "output", "已通过", fields=[{"name": "结论", "value": "已通过"}, *chain.notes]))
    if steps:
        nodes.append(c.node(REJECTED, "output", "已退回", fields=[{"name": "结论", "value": "已退回"},
                                                                  *[dict(n) for n in chain.notes]]))
    return {"nodes": nodes, "edges": edges}, joints


def app_skeleton(bp: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    name = c.app_name(bp, "审批")
    capability = c.value((bp.get("writeback") or {}).get("capability"))
    writes = {WRITE: capability} if capability else {}
    spec = c.app_base(bp, graph, use="process", name=name, description=str(c.value(bp.get("purpose")) or ""),
                      writes=writes)
    from app.apps.derive import start_label

    c.entry_and_pages(spec, bp, graph, label=start_label(name), writes=writes)
    return c.finish_app(spec)


def problems(bp: dict[str, Any]) -> list[dict[str, Any]]:
    """写回的字段对应只能取表单字段和常量：审批流程没有指标，也没有核对出来的行。"""
    if not c.value((bp.get("writeback") or {}).get("capability")):
        return []
    _, bad = c.write_args(c.value(bp["writeback"].get("mapping")) or {})
    return [c.mismatch("/writeback/mapping", f"审批流程写回时只能取表单里的字段或常量，这几个无法取值：{'、'.join(bad)}")] \
        if bad else []


def _prefill(spec: dict[str, Any]) -> None:
    if not spec.get("people"):
        spec["people"] = [c.step("s1", "审批")]
    if not spec.get("fields"):
        spec["fields"] = [{"name": "reason", "spec": c.template_item({"type": "string", "title": "事由",
                                                                       "x-widget": "textarea", "required": True}),
                           "sensitive": False}]


def example() -> dict[str, Any]:
    """示例：报销审批。金额线 5000 以上才要财务组全员同意，48 小时没处理完升级给总监；先过直属主管。"""
    item = c.user_item

    def field(name: str, spec: dict[str, Any]) -> dict[str, Any]:
        return {"name": name, "spec": item(spec), "sensitive": False}

    return {
        "format": "agentlab.blueprint/1", "purpose": item("员工报销，主管先批，大额的再过财务"),
        "template": {"id": ID, "version": VERSION}, "data": [], "cadence": item(None),
        "people": [
            {"id": "lead", "label": item("主管审批"), "who": item([{"kind": "user", "name": "zhang"}]),
             "policy": item({"kind": "any"}), "due": item("P2D"), "on_timeout": item({"action": "remind"}),
             "threshold": item(None)},
            {"id": "finance", "label": item("财务会签"), "who": item([{"kind": "group", "name": "财务组"}]),
             "policy": item({"kind": "all"}), "due": item("PT48H"),
             "on_timeout": item({"action": "escalate", "to": [{"kind": "user", "name": "director"}]}),
             "threshold": item({"field": "amount", "op": ">", "value": 5000})},
        ],
        "fields": [field("amount", {"type": "number", "title": "金额（元）", "minimum": 0.01, "required": True}),
                   field("reason", {"type": "string", "title": "事由", "x-widget": "textarea", "required": True})],
        "writeback": None,
        "access": {"use": [{"kind": "group", "name": "全体员工"}], "observe": [], "manage": []}, "asks": [],
    }


TEMPLATE = register(TemplateDef(
    id=ID, version=VERSION, label="审批流程",
    description="有人发起、有人审批：一级或多级，几个人同意算通过，超时提醒或升级，通过后可以写回别的系统。",
    applies_when="用户要一个申请—审批的流程（报销、请假、采购、合同），有表单、有处理人，可能按金额分级、要会签。",
    cards={"purpose": "必填：一句话说清审批什么",
           "fields": "必填：申请人要填的字段（名称、类型、必填、上下限、选项）",
           "people": "必填：每一步谁处理、几个人同意算通过、时限、超时给谁；某一步只在金额超过某个数时才走，写条件",
           "writeback": "可选：通过后写回哪个系统、哪些字段、要不要人确认；能力没登记查询的，还要定结果不明时谁去核实",
           "access": "可选：谁能发起、谁能看全部、谁能管理"},
    personal=None,
    skeleton=skeleton, app_skeleton=app_skeleton, schedule=None, tags=["流程", "审批"],
    example=example, example_fill=dict, prefill=_prefill, problems=problems,
))
