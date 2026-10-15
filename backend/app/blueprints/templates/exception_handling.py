"""异常处置 exception_handling（§3.3）：定时 → 入口（统计期）→ 取数（异常清单）→ 分支（有 / 无）→ 人工（负责人填处置
说明，清单附在待办上）→ [复核] → 成果。

- 生成关节：取异常清单的 SQL（一个库，一条 SQL，每条异常一行）；
- 确定性关节：统计期、定时任务、负责人（谁来做的第一步）、复核（第二步，可选）、时限与超时；
- 异常数是清单的行数（口径卡按行数算，不交给模型）；
- 应用页：最近一期的印章、异常数、异常清单，派给我的待办；每一期的处置在它自己的实例页看。

没写「谁来做」时只看清单（个人应用能用的就是这一部分）：入口 → 取数 → 口径卡 → 成果。
"""
from __future__ import annotations

from typing import Any

from app.blueprints.templates import Joint, TemplateDef, register
from app.blueprints.templates import _common as c

ID = "exception_handling"
VERSION = 1
PARSE = "parse"
GATE = "gate"
HANDLE = "handle"
REVIEW = "review"
CLEAR = "clear"
REJECTED = "rejected"
#: 负责人填的处置说明
NOTE_FORM = {"format": "agentlab.form/1", "type": "object",
             "properties": {"note": {"type": "string", "title": "处置说明", "x-widget": "textarea"}},
             "required": ["note"], "x-order": ["note"]}

LIST_SHAPE_PROMPT = ("每条异常一行：列出能认出是哪一条的列（门店、商品、单号、日期……）和判断它异常的那几个数，"
                     "列名用英文小写和下划线；没有异常时返回空结果")


def _group(bp: dict[str, Any]) -> c.Group | None:
    groups, _ = c.data_groups(bp)
    return groups[0] if groups else None


def skeleton(bp: dict[str, Any]) -> tuple[dict[str, Any], list[Joint]]:
    group = _group(bp)
    groups = [group] if group is not None else []
    fetches, joints = c.fetch_nodes(groups)
    nodes = [c.node(c.START, "input", "统计期", fields=c.period_fields()), *fetches]
    joints.append(Joint(id="start.fields", kind="fields", node_id=c.START, field="/fields", from_card="/cadence",
                        label="统计期"))
    edges = [c.edge(c.START, f["id"]) for f in fetches]
    if group is None:
        nodes.append(c.node(c.DONE, "output", "成果", fields=[]))
        return {"nodes": nodes, "edges": edges}, joints
    fetch = group.node_id
    # 原样按 JSON 解析调用工具节点的结果（engine/lineage.parsed_tool）：分支和异常数按它的行数认
    nodes += [c.node(PARSE, "transform", "异常清单", mode="json", template=f"{{{{ nodes.{fetch} }}}}"),
              c.node(c.CALIBER, "metrics", "异常数", caliber=f"{c.app_name(bp, '异常处置')}口径", caliber_version="v1",
                     metrics=[{"id": "exceptions", "name": "异常数", "unit": "条",
                               "expression": f"nodes.{PARSE}.row_count"}])]
    edges += [c.edge(fetch, PARSE), c.edge(PARSE, c.CALIBER)]
    steps = bp.get("people") or []
    fields = [{"name": "异常数（条）", "value": f"{{{{ nodes.{PARSE}.row_count }}}}"}]
    if not steps:
        nodes.append(c.node(c.DONE, "output", "异常清单", fields=fields))
        edges.append(c.edge(c.CALIBER, c.DONE))
        return {"nodes": nodes, "edges": edges}, joints
    nodes.append(c.node(GATE, "branch", "有没有异常", mode="expression", cases=[
        {"key": "hit", "condition": f"nodes.{PARSE}.row_count > 0", "label": "有异常"}]))
    edges += [c.edge(c.CALIBER, GATE), c.edge(GATE, HANDLE, "hit"), c.edge(GATE, CLEAR, "default")]
    owner = steps[0]
    label = str(c.value(owner.get("label")) or "处置")
    handle, handle_joints = c.human(
        owner, 0, HANDLE, mode="input", form=NOTE_FORM,
        title=f"{label}：{{{{ input.period }}}} 有 {{{{ nodes.{PARSE}.row_count }}}} 条异常",
        message="本期的异常清单附在下面，处理完填写处置说明",
        context={"异常清单": f"{{{{ nodes.{PARSE}.rows | json }}}}"})
    nodes.append(handle)
    joints += handle_joints
    fields.append({"name": "处置说明", "value": f"{{{{ nodes.{HANDLE}.value }}}}"})
    if len(steps) > 1:
        checker = steps[1]
        review_label = str(c.value(checker.get("label")) or "复核")
        review, review_joints = c.human(checker, 1, REVIEW, title=f"{review_label}：{{{{ input.period }}}} 的异常处置",
                                        message=f"处置说明：{{{{ nodes.{HANDLE}.value }}}}")
        nodes += [review, c.node(REJECTED, "output", "复核未通过", fields=[
            *fields, {"name": "复核意见", "value": f"{{{{ nodes.{REVIEW}.note }}}}"}])]
        joints += review_joints
        edges += [c.edge(HANDLE, REVIEW), c.edge(REVIEW, c.DONE, "approved"), c.edge(REVIEW, REJECTED, "rejected")]
        fields = [*fields, {"name": "复核意见", "value": f"{{{{ nodes.{REVIEW}.note }}}}"}]
    else:
        edges.append(c.edge(HANDLE, c.DONE))
    nodes += [c.node(c.DONE, "output", "已处置", fields=fields),
              c.node(CLEAR, "output", "本期没有异常", fields=[{"name": "结论", "value": "本期没有异常"}])]
    return {"nodes": nodes, "edges": edges}, joints


def app_skeleton(bp: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    from app.apps.derive import VIEW, _results
    from app.apps.tree import node_map

    name = c.app_name(bp, "异常处置")
    process = bool(bp.get("people"))
    spec = c.app_base(bp, graph, use="process" if process else "view", name=name,
                      description=str(c.value(bp.get("purpose")) or ""))
    spec["views"][VIEW] = c.latest_view(c.context(bp))
    home = _results(node_map(graph), VIEW, "home")
    if process:
        home.append({"id": "my_tasks", "type": "task_list", "props": {"scope": "mine"}})
        spec["views"]["inst"] = {"source": {"kind": "instance"}}
    spec["pages"] = [{"id": "home", "title": name, "body": home}]
    if process:
        spec["pages"].append(c.instance_page(graph))
    return c.finish_app(spec)


def schedule(bp: dict[str, Any]) -> dict[str, Any] | None:
    return c.schedule_of(bp, c.app_name(bp, "异常处置"))


def briefs(bp: dict[str, Any]) -> dict[str, str]:
    group = _group(bp)
    return c.data_briefs(bp, [group], shape=LIST_SHAPE_PROMPT) if group is not None else {}


def problems(bp: dict[str, Any]) -> list[dict[str, Any]]:
    groups, out = c.data_groups(bp)
    if len(groups) > 1:
        out.append(c.mismatch("/data", "异常处置只能从一个库里查异常清单：请把要查的数放在同一个库里，或者拆成几个应用"))
    if any(item.get("reuse") for item in bp.get("data") or []):
        out.append(c.mismatch("/data", "异常清单要按表查出每一条，不能复用口径卡：请按表对"))
    if len(bp.get("people") or []) > 2:
        out.append(c.mismatch("/people", "异常处置最多两步：负责人处置，再加一道复核"))
    return out


def _prefill(spec: dict[str, Any]) -> None:
    c.fill_cadence(spec, "daily")
    if not spec.get("people"):
        spec["people"] = [c.step("owner", "处置", due="P1D")]


def example() -> dict[str, Any]:
    """示例：每天查一次昨天库存低于安全库存的门店商品，派给门店运营填处置说明，区域经理复核。"""
    item = c.user_item

    return {
        "format": "agentlab.blueprint/1", "purpose": item("每天把缺货的门店商品找出来，运营处理，区域经理复核"),
        "template": {"id": ID, "version": VERSION},
        "data": [{"id": "stockout", "label": item("缺货"), "unit": item("条"),
                  "table": item({"source": "shop", "table": "stock"}), "measure": item("库存低于安全库存"),
                  "date": item({"column": "stat_date"}), "filter": item(None), "grain": item("门店 × 商品"),
                  "reuse": None}],
        "cadence": item(c.default_cadence("daily")),
        "people": [
            {"id": "owner", "label": item("运营处置"), "who": item([{"kind": "group", "name": "门店运营"}]),
             "policy": item({"kind": "any"}), "due": item("P1D"), "on_timeout": item(None), "threshold": item(None)},
            {"id": "check", "label": item("区域复核"), "who": item([{"kind": "slot", "name": "区域经理"}]),
             "policy": item({"kind": "any"}), "due": item("P2D"),
             "on_timeout": item({"action": "remind", "every": "PT4H"}), "threshold": item(None)},
        ],
        "fields": [], "writeback": None,
        "access": {"use": [{"kind": "group", "name": "门店运营"}], "observe": [], "manage": []}, "asks": [],
    }


def example_fill() -> dict[str, Any]:
    return {"sql_fetch_shop": ("SELECT store, sku, qty, safety_qty FROM stock"
                               " WHERE stat_date = '{{ input.period_end }}' AND qty < safety_qty")}


TEMPLATE = register(TemplateDef(
    id=ID, version=VERSION, label="异常处置",
    description="定期查出异常（缺货、超期、对不上的单子），派给负责人填处置说明，可以再加一道复核。",
    applies_when="用户要定期找出某类异常并让人处理、留下处置记录；异常从一个库里能查出来。",
    cards={"purpose": "必填：一句话说清找什么异常、谁来处理",
           "data": "必填：什么算异常（对到一张表，写明判断条件），只能从一个库里查",
           "cadence": "必填：多久查一次，统计哪一期",
           "people": "可选：第一步是负责人（填处置说明），第二步是复核（可选）；不写就只看清单",
           "access": "可选：谁能用、谁能看全部、谁能管理"},
    personal="只看清单的那一部分（没有处置和复核）",
    skeleton=skeleton, app_skeleton=app_skeleton, schedule=schedule, tags=["流程", "定时", "异常"],
    example=example, example_fill=example_fill, prefill=_prefill, briefs=briefs, problems=problems,
))
