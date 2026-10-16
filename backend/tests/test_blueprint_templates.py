"""场景模板与实例化（docs/design/normal-user-apps.md N3、§3.3、§3.4）。

每个模板用一张完整的示例蓝图实例化：
- 生成关节没填时：画布校验（engine/schema.validate_graph）过得了，关节标「待填」，问题里是 tpl.joint_unfilled（P3）；
- 骨架比对没有差异；生成关节都填上之后，发布前检查（governance.publish_issues，已发布级）没有 error；
- 应用草稿合 agentlab.app/1 的形状，固定版本还没有版本号；定时任务、岗位绑定按卡片来。

另外守：骨架比对认得出关节以外的改动；卡片改了，那一组生成关节的依据变了，之前填的就不算数；模板登记齐全。
"""
from __future__ import annotations

import copy

import pytest

from app.blueprints.instantiate import filled_from, generated_groups, instantiate, registry_problems, skeleton_changes
from app.blueprints.templates import GENERATED_JOINT_KINDS, TEMPLATE_IDS, check_registry, get_template
from app.blueprints.templates import _common as c
from app.engine.governance import publish_issues
from app.engine.schema import GraphSpec, validate_graph


def _graph_for(template_id: str):
    if get_template(template_id).imports_graph:
        from app.blueprints.templates.freeform import example_graph

        return example_graph()
    return None


def test_registry_is_complete():
    assert registry_problems() == []
    assert check_registry() == [], "启动核对（check_registry）走的就是 registry_problems 这一份"
    assert all(get_template(t) is not None for t in TEMPLATE_IDS)
    for t in TEMPLATE_IDS:
        template = get_template(t)
        assert template.applies_when and template.cards and template.skeleton
        assert all(note.startswith(("必填", "可选")) for note in template.cards.values()), t
        # 示例蓝图、预置、每组关节的说明、模板自己认的问题都登记在 TemplateDef 上，没有旁路登记
        assert callable(template.example) and callable(template.example_fill), t
        assert template.imports_graph == (t == "freeform")
    assert not hasattr(c, "EXTRAS")


def test_a_template_without_an_example_is_reported(monkeypatch):
    """模板定义、实例化、测试三样缺一样启动即报错：少了示例蓝图的模板，check_registry 照实报出来。"""
    import dataclasses

    from app.blueprints.templates import _REGISTRY

    monkeypatch.setitem(_REGISTRY, "approval", dataclasses.replace(get_template("approval"), example=None))
    assert check_registry() == ["模板「approval」没有示例蓝图"]


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_every_template_instantiates_validates_and_publishes(template_id):
    extras = get_template(template_id)
    example = extras.example()
    graph = _graph_for(template_id)

    pending = instantiate(example, graph=graph, workflow_id="wf_1", schedule_id="sch_1")
    assert not [i for i in validate_graph(GraphSpec.model_validate(pending.graph)).issues if i.level == "error"]
    generated = [j for j in pending.joint_defs if j.kind in GENERATED_JOINT_KINDS]
    assert {p["joint"] for p in pending.problems if p["code"] == "tpl.joint_unfilled"} == {j.id for j in generated}
    assert all(pending.joints[j.id]["status"] == "pending" for j in generated)
    assert skeleton_changes(pending.skeleton, pending.graph, pending.joint_defs) == []

    full = instantiate(example, filled=extras.example_fill(), graph=graph, workflow_id="wf_1", schedule_id="sch_1")
    assert full.problems == [], full.problems
    assert all(state["status"] == "filled" for state in full.joints.values())
    assert set(extras.example_fill()) == {j.id for j in generated}, "示例把每个生成关节都填上了"
    assert skeleton_changes(full.skeleton, full.graph, full.joint_defs) == []
    errors = [(i.code, i.message) for i in publish_issues(GraphSpec.model_validate(full.graph), level="published")
              if i.level == "error"]
    assert errors == []
    # 应用草稿：固定在这个工作流上，还没有版本号（发布单发布工作流之后换成那一版）
    pin = full.app_spec["pins"][0]
    assert pin["workflow_id"] == "wf_1" and pin["version"] is None
    from app.core.artifact_store import graph_hash

    assert pin["graph_hash"] == graph_hash(full.graph)
    assert full.app_spec["pages"] and full.app_spec["pages"][0]["id"] == "home"
    # 同一张蓝图实例化两次，图一模一样（图哈希、试运行对齐都靠它）
    assert instantiate(example, filled=extras.example_fill(), graph=graph, workflow_id="wf_1",
                       schedule_id="sch_1").graph == full.graph


def test_board_details():
    extras = get_template("periodic_board")
    inst = instantiate(extras.example(), filled=extras.example_fill(), schedule_id="sch_1", blueprint_id="bp_1")
    nodes = {n["id"]: n for n in inst.graph["nodes"]}
    assert nodes["fetch_shop"]["data"]["config"]["tool"] == "db_query__shop"
    contract = nodes["done"]["data"]["config"]["contract"]
    assert contract == {"metrics_from": ["caliber"], "required": ["gmv", "orders", "refund_rate"], "strict": True}
    from app.engine.governance import board_only

    assert board_only(contract, GraphSpec.model_validate(inst.graph), "done"), "按指标出具"
    assert inst.schedule["cron"] == "0 9 * * 1" and inst.schedule["enabled"] is False
    assert inst.schedule["input_template"]["period_start"] == "{{ schedule.last_week_start }}"
    # 没有工作流时固定在蓝图上；首页看定时任务出的数，按条件查看是按需视图
    assert inst.app_spec["pins"][0]["blueprint"] == "bp_1"
    assert inst.app_spec["views"]["latest"]["source"] == {"kind": "latest_sealed", "pin": "main", "from": "schedule:sch_1",
                                                         "params": None, "ttl_s": None, "auto": False}
    assert inst.app_spec["views"]["custom"]["source"]["kind"] == "on_demand"
    home = [comp["type"] for comp in inst.app_spec["pages"][0]["body"]]
    assert home[0] == "stamp" and "section" in home and "table" in home
    groups = generated_groups(inst)
    assert list(groups) == ["fetch_shop"]
    assert "'{{ input.period_start }}'" in inst.briefs["fetch_shop"] and "销售额" in inst.briefs["fetch_shop"]


def test_board_with_reused_caliber_and_two_sources():
    spec = get_template("periodic_board").example()
    spec["data"].append({"id": "visits", "label": {"value": "访客数", "source": "user", "status": "confirmed", "why": None},
                         "unit": {"value": "人", "source": "user", "status": "confirmed", "why": None},
                         "table": {"value": {"source": "crm", "table": "visits"}, "source": "user",
                                   "status": "confirmed", "why": None},
                         "measure": {"value": "count(distinct uid)", "source": "user", "status": "confirmed", "why": None},
                         "date": {"value": None, "source": "user", "status": "missing", "why": None},
                         "filter": {"value": None, "source": "user", "status": "missing", "why": None},
                         "grain": {"value": None, "source": "user", "status": "missing", "why": None}, "reuse": None})
    spec["data"].append({**copy.deepcopy(spec["data"][0]), "id": "aov", "table": {
        "value": None, "source": "user", "status": "missing", "why": None},
        "reuse": {"workflow_id": "wf_old", "version": 2, "node_id": "kpi", "metric_id": "aov",
                  "inputs": [{"node": "fetch", "tool": "db_query__shop", "columns": ["gmv", "order_cnt"]}]}})
    inst = instantiate(spec)
    groups = generated_groups(inst)
    # 复用的口径卡读原工作流的 fetch：新图里 shop 的取数节点就叫 fetch，本地算 shop 上的数也在这一组
    assert sorted(groups) == ["fetch", "fetch_crm"]
    assert {j.id for j in groups["fetch"]} == {"sql_fetch", "metric_gmv", "metric_orders", "metric_refund_rate"}
    nodes = {n["id"]: n for n in inst.graph["nodes"]}
    assert nodes["reuse_aov"]["data"]["config"]["caliber_from"] == {"workflow_id": "wf_old", "workflow_version": 2,
                                                                    "node_id": "kpi"}
    assert "aov" in nodes["done"]["data"]["config"]["contract"]["required"]
    assert "gmv、order_cnt" in inst.briefs["fetch"]
    assert any(comp["id"] == "home_reused" for comp in inst.app_spec["pages"][0]["body"])
    # 复用的口径卡没核对过读哪些列：照实说对不上
    spec["data"][-1]["reuse"].pop("inputs")
    assert any(p["code"] == "bp.data_unmapped" for p in instantiate(spec).problems)


def test_approval_details():
    inst = instantiate(get_template("approval").example())
    nodes = {n["id"]: n for n in inst.graph["nodes"]}
    finance = nodes["review_finance"]["data"]["config"]
    assert finance["assignee"] == {"kind": "slot", "slot": "finance"} and finance["policy"] == "all"
    assert finance["due"] == "PT48H" and finance["on_timeout"] == {"action": "escalate", "to_slot": "finance_up"}
    assert nodes["gate_finance"]["data"]["config"]["cases"][0]["condition"] == "input.amount > 5000"
    edges = {(e["source"], e["target"], e.get("sourceHandle")) for e in inst.graph["edges"]}
    # 金额线以下的直接跳过财务会签
    assert ("gate_finance", "approved", "default") in edges and ("gate_finance", "review_finance", "hit") in edges
    assert ("review_lead", "rejected", "rejected") in edges
    assert inst.bindings == {"slots": {"lead": [{"kind": "user", "name": "zhang"}],
                                       "finance": [{"kind": "group", "name": "财务组"}],
                                       "finance_up": [{"kind": "user", "name": "director"}]}}
    assert set(inst.app_spec["slots"]) == {"lead", "finance", "finance_up"}
    assert inst.app_spec["forms"]["start"]["required"] == ["amount", "reason"]
    assert inst.app_spec["entries"][0]["map"] == {"amount": "form.amount", "reason": "form.reason"}
    assert inst.schedule is None
    # 确定性关节按卡片换：改了金额线，分支跟着变；骨架比对只许关节位置不同
    other = get_template("approval").example()
    other["people"][1]["threshold"]["value"]["value"] = 8000
    changed = instantiate(other)
    assert {n["id"]: n for n in changed.graph["nodes"]}["gate_finance"]["data"]["config"]["cases"][0]["condition"] \
        == "input.amount > 8000"


def test_approval_writeback_confirms_with_last_step(monkeypatch):
    spec = get_template("approval").example()
    item = c.template_item
    spec["writeback"] = {"capability": item("erp_reimburse"), "mapping": item({"amount": "form.amount", "kind": "'报销'"}),
                         "confirm": item(True)}
    inst = instantiate(spec)
    write = {n["id"]: n for n in inst.graph["nodes"]}["writeback"]["data"]["config"]
    assert write == {"tool": "erp_reimburse", "args": {"amount": "{{ input.amount }}", "kind": "报销"},
                     "approval": "always", "assignee": {"kind": "slot", "slot": "finance"}}
    edges = {(e["source"], e["target"], e.get("sourceHandle")) for e in inst.graph["edges"]}
    assert ("review_finance", "writeback", "approved") in edges and ("writeback", "approved", None) in edges
    # 写入能力没登记查询：要有人核实结果未知的写入
    assert "reconciler" in inst.app_spec["slots"]
    spec["writeback"]["mapping"] = item({"amount": "metric.total"})
    assert any(p["code"] == "bp.template_mismatch" for p in instantiate(spec).problems)


def test_records_routes_submit_and_summary():
    from app.records import RECORDS_APPEND

    extras = get_template("records")
    inst = instantiate(extras.example(), records={"table": "loss", "source": "records_loss"}, blueprint_id="bp_9")
    nodes = {n["id"]: n for n in inst.graph["nodes"]}
    append = nodes["append"]["data"]["config"]
    assert append["tool"] == RECORDS_APPEND and append["approval"] == "never"
    assert append["args"] == {"table": "loss", "values": {"store": "{{ input.store }}", "amount": "{{ input.amount }}",
                                                          "reason": "{{ input.reason }}"}}
    assert nodes["summary_records_loss"]["data"]["config"]["tool"] == "db_query__records_loss"
    # 入口节点上表单字段都不是必填（汇总那条路不带表单），必填由应用的入口表单把关
    fields = {f["name"]: f for f in nodes["start"]["data"]["config"]["fields"]}
    assert not any(f.get("required") for f in fields.values())
    assert inst.app_spec["forms"]["start"]["required"] == ["store", "amount"]
    assert inst.app_spec["entries"][0]["map"]["mode"] == "'submit'"
    assert inst.schedule["input_template"]["mode"] == "summary"
    assert inst.app_spec["pages"][0]["body"][0]["type"] == "records_list"
    # 汇总看板不放逐行表格（会把每个人的记录列给所有人）
    assert "table" not in [comp["type"] for comp in inst.app_spec["pages"][0]["body"]]


def test_exception_and_reconcile_details():
    inst = instantiate(get_template("exception_handling").example())
    nodes = {n["id"]: n for n in inst.graph["nodes"]}
    assert nodes["handle"]["data"]["config"]["mode"] == "input"
    assert nodes["handle"]["data"]["config"]["assignee"] == {"kind": "slot", "slot": "owner"}
    assert nodes["review"]["data"]["config"]["assignee"] == {"kind": "slot", "slot": "区域经理"}
    assert nodes["gate"]["data"]["config"]["cases"][0]["condition"] == "nodes.parse.row_count > 0"
    # 只看清单（个人应用能用的那一部分）：没有人工节点
    spec = get_template("exception_handling").example()
    spec["people"] = []
    assert not [n for n in instantiate(spec).graph["nodes"] if n["type"] == "human"]

    inst = instantiate(get_template("reconcile_writeback").example())
    write = {n["id"]: n for n in inst.graph["nodes"]}["writeback"]["data"]["config"]
    assert write["args"] == {"items": "{{ nodes.parse.rows | compact }}", "total": "{{ nodes.caliber.metrics[1].value }}"}
    assert write["assignee"] == {"kind": "slot", "slot": "check"}
    assert [j.id for j in generated_groups(inst)["fetch_shop"]] == \
        ["sql_fetch_shop", "metric_diff_count", "metric_diff_amount", "sql_detail"]


def test_skeleton_changes_catch_anything_but_joints():
    extras = get_template("periodic_board")
    inst = instantiate(extras.example(), filled=extras.example_fill())
    joints = inst.joint_defs
    graph = copy.deepcopy(inst.graph)
    nodes = {n["id"]: n for n in graph["nodes"]}
    nodes["fetch_shop"]["data"]["config"]["args"]["sql"] = "SELECT 1 AS gmv"  # 关节位置：不算
    assert skeleton_changes(inst.skeleton, graph, joints) == []
    nodes["fetch_shop"]["data"]["config"]["tool"] = "db_query__other"
    nodes["done"]["data"]["config"]["contract"]["strict"] = False  # 出具契约是确定性关节：模型改了也算越界
    graph["nodes"].append(c.node("extra", "llm", "多出来的"))
    graph["edges"] = [e for e in graph["edges"] if e["target"] != "done"]
    messages = [p["message"] for p in skeleton_changes(inst.skeleton, graph, joints)]
    assert any("tool" in m for m in messages) and any("多出了节点" in m for m in messages)
    assert any("删除了连线" in m for m in messages)
    assert all(p["code"] == "tpl.skeleton_changed" for p in skeleton_changes(inst.skeleton, graph, joints))


def test_filled_values_go_stale_when_the_cards_change():
    extras = get_template("periodic_board")
    spec = extras.example()
    inst = instantiate(spec, filled=extras.example_fill())
    joints = inst.joints
    assert filled_from(spec, joints) == extras.example_fill()
    # 改了「怎么算」：这一组的说明变了，之前填的 SQL 和指标式都不算数
    spec["data"][0]["measure"]["value"] = "sum(amount) - sum(refund)"
    assert filled_from(spec, joints) == {}
    # 只改目的：说明没变，照旧算数
    spec = extras.example()
    spec["purpose"]["value"] = "换个说法"
    assert filled_from(spec, joints) == extras.example_fill()


def test_trial_inputs_follow_the_cadence():
    from datetime import datetime, timezone

    from app.blueprints.instantiate import trial_inputs

    inputs = trial_inputs(get_template("periodic_board").example(), at=datetime(2026, 10, 15, 3, tzinfo=timezone.utc))
    assert inputs == {"period": "2026-W41", "period_start": "2026-10-05", "period_end": "2026-10-11"}
    assert trial_inputs(get_template("approval").example()) == {}


def test_freeform_people_rows_follow_the_imported_graph():
    """自由形的「谁来做」按导入的图列（05-已知遗留 (d)）：同一个岗位用在几处合成一行、说明写全；发起人、表达式派单的步骤是
    只读行；没有人工节点和写入的图一行都没有。卡片上留着、图里已经没有的岗位照实提醒，不静默删掉。"""
    from app.blueprints.ops import blueprint_issues
    from app.blueprints.templates import freeform

    graph = {"nodes": [
        c.node("start", "input", "发起", fields=[{"name": "q"}]),
        c.node("a", "human", "初审", mode="approve", assignee={"kind": "slot", "slot": "finance"},
               on_timeout={"action": "escalate", "to_slot": "director"}, due="PT1H"),
        c.node("b", "human", "复审", mode="approve", assignee={"kind": "slot", "slot": "finance"}),
        c.node("me", "human", "本人确认", mode="approve", assignee={"kind": "initiator"}),
        c.node("calc", "human", "按金额派", mode="approve", assignee={"kind": "expression", "expression": "input.q"}),
        c.node("done", "output", "完成", fields=[])],
        "edges": [c.edge("start", "a"), c.edge("a", "b"), c.edge("b", "me"), c.edge("me", "calc"), c.edge("calc", "done")]}
    spec = {**get_template("freeform").example(), "people": [
        {"id": "finance", "who": c.user_item([{"kind": "user", "name": "zhang"}])},
        {"id": "gone", "who": c.user_item([{"kind": "user", "name": "li"}])}]}
    rows = freeform.people_rows(c.with_graph(spec, graph))
    assert [(r["kind"], r["slot"], r["path"]) for r in rows] == [
        ("slot", "finance", "/people/0/who"), ("slot", "director", "/people/1/who"),
        ("initiator", None, None), ("expression", None, None)]
    assert rows[0]["label"] == "「初审」、「复审」" and rows[0]["detail"] == "「初审」处理；「复审」处理"
    assert rows[1]["detail"] == "「初审」超时升级" and rows[0]["who"] == [{"kind": "user", "name": "zhang"}]
    issues = blueprint_issues(spec, for_="publish", graph=graph)
    unbound = [i for i in issues if i["code"] == "bp.people_unbound"]
    assert [(i["path"], i["slot"], i["level"]) for i in unbound] == [("/people/1/who", "director", "error")]
    assert [i["level"] for i in blueprint_issues(spec, for_="trial", graph=graph) if i["code"] == "bp.people_unbound"] \
        == ["warning"], "试运行时待办派给试运行的人自己，不挡"
    stale = [i for i in issues if i["code"] == "bp.template_mismatch" and "gone" in i["message"]]
    assert stale and stale[0]["level"] == "warning"
    assert freeform.people_rows(c.with_graph(get_template("freeform").example(), freeform.example_graph())) == []
