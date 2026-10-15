"""核对写回 reconcile_writeback（§3.3）：定时 → 取数（核对差异：汇总一行 + 差异明细）→ 口径卡（差异的指标）→ 有没有差异
→ 人工复核（确认要写回）→ 写回 → 成果。

- 生成关节：汇总差异的 SQL、差异明细的 SQL、口径卡每个数的指标式（同一组，一起填：两条 SQL 查同一个库）；
- 确定性关节：统计期、定时任务、复核人（谁来做的第一步）、写回目标与字段对应（明细整组写 rows，指标写 metric.<id>）、
  要不要人确认（确认派给复核的岗位）；
- 两个成果都按指标出具（差异的指标就是这一期核对的结论）。

不能做成个人应用：要人复核、要写回别的系统。
"""
from __future__ import annotations

from typing import Any

from app.blueprints.templates import Joint, TemplateDef, register
from app.blueprints.templates import _common as c

ID = "reconcile_writeback"
VERSION = 1
DETAIL = "detail"
PARSE = "parse"
GATE = "gate"
REVIEW = "review"
WRITE = "writeback"
CLEAR = "clear"
REJECTED = "rejected"
#: 差异明细整组的取值链（写回参数写 rows 时用它）
ROWS = f"nodes.{PARSE}.rows"

DETAIL_BRIEF_PROMPT = ("差异明细节点 {node}（工具 db_query__{source}）：再写一条只读 SQL，每条差异一行，列出要写回的那几列"
                       "（列名和写回的字段对应：{mapping}）；没有差异时返回空结果。统计期同上。")


def _group(bp: dict[str, Any]) -> c.Group | None:
    groups, _ = c.data_groups(bp)
    return groups[0] if groups else None


def skeleton(bp: dict[str, Any]) -> tuple[dict[str, Any], list[Joint]]:
    group = _group(bp)
    groups = [group] if group is not None else []
    name = c.app_name(bp, "核对写回")
    fetches, joints = c.fetch_nodes(groups)
    nodes = [c.node(c.START, "input", "统计期", fields=c.period_fields()), *fetches]
    joints.append(Joint(id="start.fields", kind="fields", node_id=c.START, field="/fields", from_card="/cadence",
                        label="统计期"))
    edges = [c.edge(c.START, f["id"]) for f in fetches]
    caliber, metric_joints = c.caliber_node(bp, groups, name)
    if group is None or caliber is None:
        nodes.append(c.node(c.DONE, "output", "成果", fields=[]))
        return {"nodes": nodes, "edges": edges}, joints
    nodes += [caliber,
              c.node(DETAIL, "tool", f"差异明细（{group.source}）", tool=f"db_query__{group.source}",
                     args={"sql": c.SQL_PLACEHOLDER}),
              c.node(PARSE, "transform", "差异明细", mode="json", template=f"{{{{ nodes.{DETAIL} }}}}"),
              c.node(GATE, "branch", "有没有差异", mode="expression", cases=[
                  {"key": "hit", "condition": f"nodes.{PARSE}.row_count > 0", "label": "有差异"}])]
    joints += metric_joints
    joints.append(Joint(id=f"sql_{DETAIL}", kind="sql", node_id=DETAIL, field="/args/sql", group=group.node_id,
                        label=f"差异明细的 SQL（{group.source}）"))
    edges += [c.edge(group.node_id, c.CALIBER), c.edge(c.START, DETAIL), c.edge(DETAIL, PARSE),
              c.edge(c.CALIBER, GATE), c.edge(PARSE, GATE), c.edge(GATE, CLEAR, "default")]
    steps = bp.get("people") or []
    confirm_slot = None
    after_gate = WRITE
    if steps:
        checker = steps[0]
        label = str(c.value(checker.get("label")) or "复核")
        review, review_joints = c.human(
            checker, 0, REVIEW, title=f"{label}：{{{{ input.period }}}} 有 {{{{ nodes.{PARSE}.row_count }}}} 条差异要写回",
            message="差异明细附在下面，确认后写回", context={"差异明细": f"{{{{ {ROWS} | json }}}}"})
        nodes += [review, c.node(REJECTED, "output", "不写回", fields=[
            {"name": "结论", "value": "复核没有通过，没有写回"},
            {"name": "复核意见", "value": f"{{{{ nodes.{REVIEW}.note }}}}"}])]
        joints += review_joints
        edges += [c.edge(REVIEW, WRITE, "approved"), c.edge(REVIEW, REJECTED, "rejected")]
        confirm_slot = c.slot_of(checker)
        after_gate = REVIEW
    edges.append(c.edge(GATE, after_gate, "hit"))
    write, write_joints, _ = c.write_node(bp, WRITE, confirm_slot=confirm_slot, metrics=c.metric_paths(caliber),
                                          rows=ROWS)
    nodes.append(write)
    joints += write_joints
    edges.append(c.edge(WRITE, c.DONE))
    done, done_joints = c.board_output(bp, caliber, [], label="已写回")
    clear, _ = c.board_output(bp, caliber, [], node_id=CLEAR, label="没有差异")
    nodes += [done, clear]
    joints += done_joints
    return {"nodes": nodes, "edges": edges}, joints


def app_skeleton(bp: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    from app.apps.derive import VIEW, _results
    from app.apps.tree import node_map

    name = c.app_name(bp, "核对写回")
    capability = c.value((bp.get("writeback") or {}).get("capability"))
    writes = {WRITE: capability} if capability and WRITE in node_map(graph) else {}
    spec = c.app_base(bp, graph, use="process", name=name, description=str(c.value(bp.get("purpose")) or ""),
                      writes=writes)
    spec["views"][VIEW] = c.latest_view(c.context(bp))
    spec["views"]["inst"] = {"source": {"kind": "instance"}}
    spec["pages"] = [
        {"id": "home", "title": name, "body": [*_results(node_map(graph), VIEW, "home"),
                                               {"id": "my_tasks", "type": "task_list", "props": {"scope": "mine"}}]},
        c.instance_page(graph, writes),
    ]
    return c.finish_app(spec)


def schedule(bp: dict[str, Any]) -> dict[str, Any] | None:
    return c.schedule_of(bp, c.app_name(bp, "核对写回"))


def briefs(bp: dict[str, Any]) -> dict[str, str]:
    group = _group(bp)
    if group is None:
        return {}
    out = c.data_briefs(bp, [group])
    mapping = c.value((bp.get("writeback") or {}).get("mapping")) or {}
    out[group.node_id] += "\n" + DETAIL_BRIEF_PROMPT.format(
        node=DETAIL, source=group.source, mapping="、".join(f"{k} ← {v}" for k, v in mapping.items()) or c.UNKNOWN_PROMPT)
    return out


def problems(bp: dict[str, Any]) -> list[dict[str, Any]]:
    groups, out = c.data_groups(bp)
    if len(groups) > 1:
        out.append(c.mismatch("/data", "核对写回的两边要在同一个库里，一条 SQL 才对得上：请把要核对的数放在同一个库里"))
    if any(item.get("reuse") for item in bp.get("data") or []):
        out.append(c.mismatch("/data", "核对差异要按表重新算，不能复用口径卡：请按表对"))
    if len(bp.get("people") or []) > 1:
        out.append(c.mismatch("/people", "核对写回只有一步复核：请只留一步"))
    mapping = c.value((bp.get("writeback") or {}).get("mapping")) or {}
    metrics = {item["id"]: "" for item in bp.get("data") or []}
    _, bad = c.write_args(mapping, metrics=metrics, rows=ROWS)
    if bad:
        out.append(c.mismatch("/writeback/mapping", f"写回的字段对应只能取差异明细（rows）、看哪些数里的指标或常量，"
                                                    f"这几个无法取值：{'、'.join(bad)}"))
    return out


def _prefill(spec: dict[str, Any]) -> None:
    c.fill_cadence(spec, "daily")
    if not spec.get("people"):
        spec["people"] = [c.step("check", "复核", due="P1D")]


def example() -> dict[str, Any]:
    """示例：每天核对昨天订单系统和财务系统的金额，差异给财务复核后写回财务系统。"""
    item = c.user_item

    def data(item_id: str, label: str, unit: str, measure: str) -> dict[str, Any]:
        return {"id": item_id, "label": item(label), "unit": item(unit),
                "table": item({"source": "shop", "table": "orders"}), "measure": item(measure),
                "date": item({"column": "paid_at"}), "filter": item("和 ledger 表按单号对"), "grain": item("单号"),
                "reuse": None}

    return {
        "format": "agentlab.blueprint/1", "purpose": item("每天核对订单和财务入账，差异复核后写回财务系统"),
        "template": {"id": ID, "version": VERSION},
        "data": [data("diff_count", "差异笔数", "笔", "金额对不上的单数"),
                 data("diff_amount", "差异金额", "元", "两边金额差的绝对值之和")],
        "cadence": item(c.default_cadence("daily")),
        "people": [{"id": "check", "label": item("财务复核"), "who": item([{"kind": "group", "name": "财务组"}]),
                    "policy": item({"kind": "any"}), "due": item("P1D"), "on_timeout": item(None),
                    "threshold": item(None)}],
        "fields": [],
        "writeback": {"capability": item("fake_ledger_adjust"),
                      "mapping": item({"items": "rows", "total": "metric.diff_amount"}),
                      "confirm": item(True)},
        "access": {"use": [{"kind": "group", "name": "财务组"}], "observe": [], "manage": []}, "asks": [],
    }


def example_fill() -> dict[str, Any]:
    period = "o.paid_at BETWEEN '{{ input.period_start }}' AND '{{ input.period_end }}'"
    return {
        "sql_fetch_shop": ("SELECT COUNT(*) AS diff_count, SUM(ABS(o.amount - l.amount)) AS diff_amount FROM orders o"
                           f" JOIN ledger l ON l.order_id = o.id WHERE o.amount <> l.amount AND {period}"),
        "sql_detail": ("SELECT o.id AS order_id, o.amount AS amount FROM orders o JOIN ledger l ON l.order_id = o.id"
                       f" WHERE o.amount <> l.amount AND {period}"),
        "metric_diff_count": "cell(nodes.fetch_shop, 0, 'diff_count')",
        "metric_diff_amount": "cell(nodes.fetch_shop, 0, 'diff_amount') if cell(nodes.fetch_shop, 0, 'diff_count') else 0",
    }


TEMPLATE = register(TemplateDef(
    id=ID, version=VERSION, label="核对写回",
    description="定期核对两边的数，找出对不上的，交给人复核，确认后写回别的系统；每次写入都留生效记录。",
    applies_when="用户要定期对账或核对（两张表、两个系统的数对不对得上），找出差异后由人确认，再写回某个系统。",
    cards={"purpose": "必填：一句话说清核对什么、对不上怎么办",
           "data": "必填：差异的指标（差异笔数、差异金额），对到同一个库里的表",
           "cadence": "必填：多久核对一次，统计哪一期",
           "people": "必填：谁复核（只有一步）",
           "writeback": "必填：写回哪个系统（能力登记里的写入能力）、字段对应（差异明细写 rows）、要不要人确认",
           "access": "可选：谁能用、谁能看全部、谁能管理"},
    personal=None,
    skeleton=skeleton, app_skeleton=app_skeleton, schedule=schedule, tags=["流程", "定时", "写回"],
))
c.extend(ID, c.Extras(example=example, example_fill=example_fill, prefill=_prefill, briefs=briefs,
                      problems=problems))
