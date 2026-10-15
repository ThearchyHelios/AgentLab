"""填报登记 records（§3.3、N8）：入口（表单）→ [审批] → 平台内置能力 records_append（一次提交落一行）→ 已登记；
另配一张汇总看板（周期看板的骨架，查这个应用的记录表）。

两件事在同一张图里，按入口字段 mode 分两条路：
- 填报：应用的入口表单提交（入口映射把 mode 写成 submit）→ [审批] → 写记录表 → 已登记。写入只经调用工具节点和动作
  （生效记录、幂等键），能力允许无人值守，审批写「全部自动放行」；
- 汇总：定时任务按「多久更新」发起（输入模板把 mode 写成 summary）→ 取数（查记录表，生成关节 SQL）→ 口径卡（生成关节
  指标式）→ 成果（指标出具）。正式运行以服务身份查、看到全部行，所以汇总是全量的（N8：发布摘要写明）。

表单字段在入口节点上都不是必填（汇总那条路不带表单），必填由应用的入口表单把关，记录表写入时按列再核一遍。
记录表的键和登记成的数据源名由实例化的上下文给（records = {table, source}，记录表服务建表时定）；「看哪些数」里对到
本应用记录表的项，库名写 @records。

不能做成个人应用（personal=None）：个人应用只读，这张图的骨架就是往自己的记录表里写，去掉写入只剩一张永远是空的表；
想看自己提交过什么，用正式应用里的「我的记录」。蓝图这边（blueprints/store.records_context）也从不替个人蓝图建记录表。

「填什么」里的文件字段进记录表的文件列（存文件引用），提交时文件先绑到这次运行，写下这一行时再绑到这一行
（records/actions._bind_files），看得到这一行的人能下载。
"""
from __future__ import annotations

from typing import Any

from app.blueprints.templates import Joint, TemplateDef, register
from app.blueprints.templates import _common as c

ID = "records"
VERSION = 1
#: 「看哪些数」里指这个应用自己的记录表
RECORDS_SELF = "@records"
ROUTE = "route"
APPEND = "append"
SUBMITTED = "submitted"
REJECTED = "rejected"
SUMMARY = "summary"
#: 入口字段 mode 的两个值
MODE_SUBMIT = "submit"
MODE_SUMMARY = "summary"


def records_of(bp: dict[str, Any]) -> dict[str, str]:
    """这个应用的记录表：{table: 写入时认的表（records_tables 的 id、数据源名或「键.v版本」，records/tables.find_table），
    source: 登记成的数据源名}，由实例化的上下文给（records/tables.ensure_table 建好之后）。没给时按记录表服务的起名规则
    猜第一版（键是蓝图 id）：只用于还没建表时看骨架，试运行和发布前由调用方给真的。"""
    import re

    from app.records.tables import SOURCE_PREFIX

    ctx = c.context(bp)
    given = ctx.get("records") if isinstance(ctx.get("records"), dict) else {}
    key = str(ctx.get("blueprint_id") or "draft")
    source = f"{SOURCE_PREFIX}{re.sub(r'[^a-z0-9]', '', key.lower())[:12] or 'table'}"
    return {"table": str(given.get("table") or f"{key}.v1"), "source": str(given.get("source") or source)}


def _groups(bp: dict[str, Any]) -> tuple[list[c.Group], list[dict[str, Any]]]:
    return c.data_groups(bp, source_map={RECORDS_SELF: records_of(bp)["source"]}, prefix=SUMMARY)


def skeleton(bp: dict[str, Any]) -> tuple[dict[str, Any], list[Joint]]:
    from app.records import RECORDS_APPEND

    records = records_of(bp)
    groups, _ = _groups(bp)
    name = c.app_name(bp, "填报登记")
    mode = {"name": "mode", "label": "做什么", "enum": [MODE_SUBMIT, MODE_SUMMARY], "default": MODE_SUBMIT,
            "description": "填报（submit）或汇总（summary）：应用的入口表单填报，定时任务汇总"}
    nodes = [c.node(c.START, "input", "填报", fields=[*c.form_fields(bp, required=False), mode,
                                                     *[{**f, "required": False} for f in c.period_fields()]]),
             c.node(ROUTE, "branch", "填报还是汇总", mode="expression", cases=[
                 {"key": "summary", "condition": f"input.mode == '{MODE_SUMMARY}'", "label": "汇总"}])]
    joints = [Joint(id="start.fields", kind="fields", node_id=c.START, field="/fields", from_card="/fields",
                    label="表单与表结构")]
    edges = [c.edge(c.START, ROUTE)]
    # 填报：[审批] → 写记录表
    chain = c.approval_chain(bp, after=APPEND, rejected=REJECTED)
    nodes += chain.nodes
    joints += chain.joints
    edges += [c.edge(ROUTE, chain.head, "default"), *chain.edges]
    values = {f["name"]: c.field_ref(f["name"]) for f in bp.get("fields") or []}
    nodes.append(c.node(APPEND, "tool", "写记录表", tool=RECORDS_APPEND, approval="never",
                        args={"table": records["table"], "values": values}))
    joints.append(Joint(id=f"{APPEND}.args", kind="capability", node_id=APPEND, field="/args", from_card="/fields",
                        label="写记录表：字段对应"))
    nodes.append(c.node(SUBMITTED, "output", "已登记", fields=[{"name": "结论", "value": "已登记"}, *chain.notes]))
    edges.append(c.edge(APPEND, SUBMITTED))
    if bp.get("people"):
        nodes.append(c.node(REJECTED, "output", "已退回", fields=[{"name": "结论", "value": "已退回"},
                                                                  *[dict(n) for n in chain.notes]]))
    # 汇总：周期看板的骨架，查记录表
    fetches, fetch_joints = c.fetch_nodes(groups)
    caliber, metric_joints = c.caliber_node(bp, groups, name)
    reused = c.reuse_nodes(groups)
    nodes += fetches
    joints += fetch_joints
    edges += [c.edge(ROUTE, f["id"], "summary") for f in fetches]
    if caliber is not None:
        nodes.append(caliber)
        joints += metric_joints
        edges += [c.edge(g.node_id, c.CALIBER) for g in groups if g.items]
        edges.append(c.edge(c.CALIBER, c.DONE))
    for card, _item, fetch in reused:
        nodes.append(card)
        edges += [c.edge(fetch, card["id"]), c.edge(card["id"], c.DONE)]
    if fetches:
        done, done_joints = c.board_output(bp, caliber, reused, label="汇总")
        nodes.append(done)
        joints += done_joints
    else:
        # 还没定看哪些数：汇总那条路直接收尾，照实写还没有汇总
        nodes.append(c.node(c.DONE, "output", "汇总", fields=[{"name": "结论", "value": "还没定汇总看哪些数"}]))
        edges.append(c.edge(ROUTE, c.DONE, "summary"))
    return {"nodes": nodes, "edges": edges}, joints


def app_skeleton(bp: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    from app.apps.derive import VIEW, _results
    from app.apps.tree import node_map
    from app.records import RECORDS_APPEND

    name = c.app_name(bp, "填报登记")
    writes = {APPEND: RECORDS_APPEND}
    spec = c.app_base(bp, graph, use="process", name=name, description=str(c.value(bp.get("purpose")) or ""),
                      writes=writes)
    spec["views"][VIEW] = c.latest_view(c.context(bp))
    summary = [comp for comp in _results(node_map(graph), VIEW, "home") if comp.get("type") != "table"]
    # 我的记录：按看的人实时过滤（使用者只看自己的，N8 行级可见），不走封存。汇总表格不放：它会把每一行列给所有人
    mine = [{"id": "my_records", "type": "records_list"}]
    c.entry_and_pages(spec, bp, graph, label=f"填报{name}", writes=writes,
                      home_extra=[*mine, *summary, *c.reused_section(bp, graph, VIEW, "home")],
                      entry_map={"mode": f"'{MODE_SUBMIT}'"})
    return c.finish_app(spec)


def schedule(bp: dict[str, Any]) -> dict[str, Any] | None:
    if not bp.get("data"):
        return None
    cadence = c.value(bp.get("cadence"))
    return c.schedule_of(bp, c.app_name(bp, "填报登记"),
                         inputs={**c.period_template(cadence), "mode": MODE_SUMMARY} if cadence else None)


def briefs(bp: dict[str, Any]) -> dict[str, str]:
    groups, _ = _groups(bp)
    return c.data_briefs(bp, groups)


def problems(bp: dict[str, Any]) -> list[dict[str, Any]]:
    out = _groups(bp)[1]
    if bp.get("writeback") and c.value(bp["writeback"].get("capability")):
        out.append(c.mismatch("/writeback", "填报登记写进平台自己的记录表，不写回别的系统：请删掉「写回哪里」"))
    return out


def _prefill(spec: dict[str, Any]) -> None:
    c.fill_cadence(spec, "weekly")


def example() -> dict[str, Any]:
    """示例：门店每天报损，店长审批；汇总看板每周一看上周的报损金额和笔数。"""
    item = c.user_item

    def data(item_id: str, label: str, unit: str, measure: str) -> dict[str, Any]:
        return {"id": item_id, "label": item(label), "unit": item(unit),
                "table": item({"source": RECORDS_SELF, "table": "records"}), "measure": item(measure),
                "date": item({"column": "_submitted_at", "rule": "按提交时间"}), "filter": item(None),
                "grain": item("全部门店"), "reuse": None}

    return {
        "format": "agentlab.blueprint/1", "purpose": item("门店每天报损，店长审批，每周看汇总"),
        "template": {"id": ID, "version": VERSION},
        "data": [data("loss_amount", "报损金额", "元", "sum(amount)"), data("loss_count", "报损笔数", "笔", "count(*)")],
        "cadence": item(c.default_cadence("weekly")),
        "people": [{"id": "manager", "label": item("店长审批"), "who": item([{"kind": "slot", "name": "店长"}]),
                    "policy": item({"kind": "any"}), "due": item("P1D"), "on_timeout": item(None),
                    "threshold": item(None)}],
        "fields": [
            {"name": "store", "spec": item({"type": "string", "title": "门店", "required": True}), "sensitive": False},
            {"name": "amount", "spec": item({"type": "number", "title": "金额（元）", "minimum": 0, "required": True}),
             "sensitive": False},
            {"name": "reason", "spec": item({"type": "string", "title": "原因", "x-widget": "textarea"}),
             "sensitive": True},
        ],
        "writeback": None,
        "access": {"use": [{"kind": "group", "name": "门店"}], "observe": [{"kind": "group", "name": "区域经理"}],
                   "manage": []},
        "asks": [],
    }


def example_fill() -> dict[str, Any]:
    sql = ("SELECT SUM(amount) AS loss_amount, COUNT(*) AS loss_count FROM records"
           " WHERE date(_submitted_at) BETWEEN '{{ input.period_start }}' AND '{{ input.period_end }}'")
    node = f"{SUMMARY}_rec_draft"
    return {f"sql_{node}": sql, "metric_loss_amount": f"cell(nodes.{node}, 0, 'loss_amount')",
            "metric_loss_count": f"cell(nodes.{node}, 0, 'loss_count')"}


TEMPLATE = register(TemplateDef(
    id=ID, version=VERSION, label="填报登记",
    description="大家按同一张表单提交，每次提交存一行（可以先审批），再按周期汇总着看；每个人只看得到自己提交的记录。",
    applies_when="用户要收集一类记录（报损、巡店、日报、登记），每次提交一行，之后汇总着看；不需要写回别的系统。",
    cards={"purpose": "必填：一句话说清登记什么、谁来填",
           "fields": "必填：每次提交填哪些字段（名称、类型、必填、上下限、选项），可以标敏感",
           "people": "可选：提交后要谁审批（审批通过才落进记录表）",
           "data": "可选：汇总看哪些数（对到本应用的记录表）",
           "cadence": "可选：汇总多久更新一次",
           "access": "可选：谁能填（只看得到自己的记录）、谁能看全部、谁能管理"},
    personal=None,
    skeleton=skeleton, app_skeleton=app_skeleton, schedule=schedule, tags=["流程", "记录", "看板"],
    example=example, example_fill=example_fill, prefill=_prefill, briefs=briefs, problems=problems,
))
