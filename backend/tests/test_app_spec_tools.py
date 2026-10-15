"""应用操作（ops.apply_app_op）、应用检查（lint.lint_app 的 app.* 编号）、派生（derive.derive_app）、给助手查看的版本
详情（describe.describe_version）、应用级权限（access.role_of）。

都是确定性的：不发起运行，工作流版本直接落库。
"""
from __future__ import annotations

import copy
import uuid

import pytest

from app.api.deps import Principal
from app.apps import lint as lint_mod
from app.apps.access import role_of
from app.apps.derive import derive_app
from app.apps.describe import describe_version
from app.apps.lint import lint_app
from app.apps.ops import apply_app_op, polish_problem
from app.apps.spec import empty_spec, parse_spec
from app.core.artifact_store import graph_hash
from app.db.base import SessionLocal, utcnow
from app.db.models import DataSource, Schedule, Workflow, WorkflowVersion


def node(nid, ntype, label=None, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": label or nid, "config": config}}


def chain(*nodes):
    return {"nodes": list(nodes), "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def weekly(source="shop"):
    return chain(
        node("start", "input", "开始", fields=[{"name": "week", "required": True, "description": "统计周"},
                                              {"name": "region", "default": "华东"}]),
        node("fetch", "tool", "取数", tool=f"db_query__{source}", args={"sql": "SELECT 1 AS gmv"}),
        node("card", "metrics", "周报口径卡", caliber="周销售口径", caliber_version="v1",
             metrics=[{"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", "写周报"),
        node("out", "output", "成果", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"},
                                             {"name": "备注", "value": "本期 {{ input.week }}"},
                                             {"name": "说明", "value": "按支付日统计"}]),
    )


def approval():
    return chain(
        node("start", "input", "开始", fields=[{"name": "amount", "type": "number", "required": True},
                                              {"name": "applicant"}]),
        node("check", "human", "财务审批", mode="approve", assignee={"kind": "slot", "slot": "finance"}),
        node("pay", "tool", "付款", tool="erp_pay", args={}),
        node("out", "output", "成果", fields=[{"name": "r", "value": "完成"}]),
    )


async def make_workflow(graph, *, versions=((1, "governed"),), current=1, status="governed") -> tuple[str, dict]:
    async with SessionLocal() as session:
        wf = Workflow(name=f"周报-{uuid.uuid4().hex[:6]}", graph=graph, status=status, published_version=current,
                      version=max(v for v, _ in versions))
        session.add(wf)
        await session.flush()
        hashes = {}
        for v, level in versions:
            session.add(WorkflowVersion(workflow_id=wf.id, version=v, graph=graph, graph_hash=graph_hash(graph),
                                        level=level, published_at=utcnow() if level else None))
            hashes[v] = graph_hash(graph)
        await session.commit()
        return wf.id, {"name": wf.name, "hash": graph_hash(graph)}


def pin(wf_id, info, version=1, alias="main", use="view"):
    return {"alias": alias, "workflow_id": wf_id, "version": version, "graph_hash": info["hash"], "use": use}


def base_spec(wf_id, info, **extra):
    spec = empty_spec("周报看板")
    spec["pins"] = [pin(wf_id, info)]
    spec["views"] = {"wk": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}}}
    spec["pages"] = [{"id": "home", "title": "首页", "body": []}]
    spec.update(extra)
    return spec


def codes(issues, level=None):
    return {i["code"] for i in issues if level is None or i["level"] == level}


# --------------------------------------------------------------------------
# 应用操作
# --------------------------------------------------------------------------


def test_the_six_operations():
    spec = empty_spec("看板")
    assert apply_app_op(spec, {"op": "app_page", "page": {"id": "home", "title": "首页"}}).changed
    assert not apply_app_op(spec, {"op": "app_page", "page": {"id": "home", "title": "首页"}}).changed
    assert apply_app_op(spec, {"op": "app_page", "page": {"id": "home", "title": "本周概览"}}).changed
    add = {"op": "app_add", "parent": "home", "component": {"id": "cols", "type": "columns", "props": {"count": 2}}}
    assert apply_app_op(spec, add).changed
    # 同 id、同内容再写一遍：不算改动、不转发（look §2.5.5 的去重）
    again = apply_app_op(spec, add)
    assert (again.changed, again.forward, again.problem) == (False, False, None)
    conflict = apply_app_op(spec, {**add, "component": {"id": "cols", "type": "divider"}})
    assert conflict.problem and "已经存在" in conflict.problem
    metric = {"id": "gmv", "type": "metric", "props": {"label": "销售额"}, "bind": {"view": "wk", "metric": "kpi.gmv"}}
    assert apply_app_op(spec, {"op": "app_add", "parent": "cols", "index": 0, "component": metric}).changed
    # 不能放子组件的组件不能当父
    assert apply_app_op(spec, {"op": "app_add", "parent": "gmv", "component": {"id": "x", "type": "divider"}}).problem
    up = apply_app_op(spec, {"op": "app_update", "id": "gmv", "props": {"unit": "元", "label": None},
                             "visible": "run.tier == 'formal'"})
    assert up.changed
    comp = spec["pages"][0]["body"][0]["children"][0]
    assert comp["props"] == {"unit": "元"} and comp["visible"] == "run.tier == 'formal'"
    assert apply_app_op(spec, {"op": "app_move", "id": "gmv", "parent": "home", "index": 0}).changed
    assert [c["id"] for c in spec["pages"][0]["body"]] == ["gmv", "cols"]
    assert apply_app_op(spec, {"op": "app_move", "id": "cols", "parent": "cols"}).problem
    assert apply_app_op(spec, {"op": "app_remove", "id": "cols"}).changed
    assert apply_app_op(spec, {"op": "app_remove", "id": "cols"}).problem
    assert apply_app_op(spec, {"op": "app_set", "path": "views", "key": "wk",
                               "value": {"source": {"kind": "latest_sealed", "pin": "main", "from": "formal"}}}).changed
    assert spec["views"]["wk"]["source"]["from"] == "formal"
    assert apply_app_op(spec, {"op": "app_set", "path": "forms", "key": "f",
                               "value": {"type": "object", "properties": {"x": {"type": "string", "pattern": "a"}}}}).problem
    assert apply_app_op(spec, {"op": "app_set", "path": "entries", "key": "apply",
                               "value": {"label": "发起", "pin": "main", "form": {"$ref": "#/forms/f"}}}).changed
    assert spec["entries"][0]["id"] == "apply"
    assert apply_app_op(spec, {"op": "app_set", "path": "entries", "key": "apply", "value": None}).changed
    assert spec["entries"] == []
    assert apply_app_op(spec, {"op": "app_set", "path": "pins", "key": "x", "value": {}}).problem
    assert apply_app_op(spec, {"op": "nope"}).problem
    parse_spec(spec)


def test_update_hides_and_unhides_a_component():
    """隐藏（normal-user-apps N6）走 app_update：为真才写进组件，取消隐藏去掉这一项；写别的值拒收、组件不动。
    应用页上直接改和编辑器的「隐藏」开关、助手润色用的都是这一个操作（前端 edit/appOps 同一套）。"""
    spec = empty_spec("看板")
    apply_app_op(spec, {"op": "app_page", "page": {"id": "home", "title": "首页"}})
    apply_app_op(spec, {"op": "app_add", "parent": "home", "component": {"id": "tip", "type": "notice", "props": {"text": "说明"}}})
    comp = spec["pages"][0]["body"][0]
    assert apply_app_op(spec, {"op": "app_update", "id": "tip", "hidden": True}).changed
    assert comp["hidden"] is True
    parse_spec(spec)
    again = apply_app_op(spec, {"op": "app_update", "id": "tip", "hidden": True})
    assert (again.changed, again.problem) == (False, None)
    bad = apply_app_op(spec, {"op": "app_update", "id": "tip", "hidden": "yes"})
    assert bad.problem and comp["hidden"] is True
    assert apply_app_op(spec, {"op": "app_update", "id": "tip", "hidden": False}).changed
    assert "hidden" not in comp
    # 润色可以隐藏：只改呈现
    assert polish_problem(spec, {"op": "app_update", "id": "tip", "hidden": True}) is None


# --------------------------------------------------------------------------
# 应用检查
# --------------------------------------------------------------------------


async def test_a_clean_spec_has_no_errors():
    wf, info = await make_workflow(weekly())
    spec = base_spec(wf, info, pages=[{"id": "home", "title": "首页", "body": [
        {"id": "stamp", "type": "stamp", "bind": {"view": "wk"}},
        {"id": "gmv", "type": "metric", "props": {"label": "销售额"}, "bind": {"view": "wk", "metric": "card.gmv"},
         "visible": "run.tier != 'withheld'"},
        {"id": "rep", "type": "report", "bind": {"view": "wk", "report": "write"}},
        {"id": "weekly", "type": "field", "bind": {"view": "wk", "output": "周报"}},
        {"id": "how", "type": "field", "bind": {"view": "wk", "output": "说明"}},
        {"id": "tbl", "type": "table", "bind": {"view": "wk", "query": {"node": "fetch"}}},
    ]}])
    async with SessionLocal() as session:
        issues = await lint_app(session, spec, {})
    assert codes(issues, "error") == set(), issues
    # 还没有运行：只是警告
    assert codes(issues) == {"app.view_source_empty"}


async def test_lint_codes():
    wf, info = await make_workflow(weekly(), versions=((1, "governed"), (2, None), (3, "published")), current=3)
    spec = base_spec(wf, info)
    spec["pins"] += [pin(wf, info, 2, "draft"), {**pin(wf, info, 3, "stale"), "graph_hash": "0" * 64},
                     pin("gone", info, 1, "gone")]
    spec["entries"] = [{"id": "e1", "label": "发起", "pin": "main", "form": {"$ref": "#/forms/f"},
                        "map": {"region": "form.region", "ghost": "form.x", "bad": "user.password"},
                        "instance_title": {"pattern": "{who} 的周报", "fields": []}}]
    spec["forms"] = {"f": {"type": "object", "properties": {"region": {"type": "string"}}}}
    spec["hooks"] = [{"name": "kick", "kind": "start", "entry": "nope"}]
    spec["notify"] = [{"channel": "ch_missing", "events": ["task.opened"]}]
    spec["pages"] = [{"id": "home", "title": "首页", "body": [
        {"id": "a", "type": "sparkline"},
        {"id": "b", "type": "metric", "props": {"colour": "red"}, "bind": {"view": "wk", "metric": "card.gmv"}},
        {"id": "c", "type": "metric", "bind": {"view": "wk", "metric": "card.nope"}},
        {"id": "d", "type": "metric", "bind": {"view": "wk", "report": "write"}},
        {"id": "e", "type": "field", "bind": {"view": "wk", "output": "备注"}},
        {"id": "f", "type": "notice", "props": {"text": "本期 3 家门店停业"}},
        {"id": "g", "type": "divider", "visible": "metric.card.gmv * 2 > 1"},
        {"id": "h", "type": "divider", "visible": "vars.x == 1"},
        {"id": "i", "type": "divider", "visible": "run.tier =="},
        {"id": "j", "type": "metric", "bind": {"view": "nope", "metric": "card.gmv"}},
        {"id": "a", "type": "divider"},
    ]}]
    async with SessionLocal() as session:
        issues = await lint_app(session, spec, {})
    found = codes(issues)
    for code in ("app.component_unknown", "app.props_invalid", "app.pin_unpublished", "app.pin_hash",
                 "app.binding_target_missing", "app.binding_not_evidenced", "app.expr_arith", "app.expr_root",
                 "app.expr_invalid", "app.entry_form_mismatch", "app.entry_unmapped_required", "app.hook_entry_missing",
                 "app.notify_channel_missing", "app.format", "app.text_number"):
        assert code in found, (code, issues)
    # 一个钉住项是受管级、一个是已发布级：应用按最低的算，静态文字里的数字只是警告
    assert next(i for i in issues if i["code"] == "app.text_number")["level"] == "warning"
    arith = next(i for i in issues if i["code"] == "app.expr_arith")
    assert arith["path"] == "pages[0].body[6].visible" and "算术" in arith["message"]
    assert not [i for i in issues if "钉" in i["message"]]


async def test_text_numbers_block_a_governed_app():
    wf, info = await make_workflow(weekly())
    spec = base_spec(wf, info, pages=[{"id": "home", "title": "首页", "body": [
        {"id": "t", "type": "text", "props": {"markdown": "同比增长 12%"}}]}])
    async with SessionLocal() as session:
        issues = await lint_app(session, spec, {})
    assert next(i for i in issues if i["code"] == "app.text_number")["level"] == "error"


async def test_retired_pins_and_upgrades_need_disposition():
    wf, info = await make_workflow(weekly(), versions=((1, "governed"), (2, "governed")), current=2)
    async with SessionLocal() as session:
        row = (await session.execute(WorkflowVersion.__table__.select().where(
            WorkflowVersion.workflow_id == wf, WorkflowVersion.version == 1))).first()
        await session.execute(WorkflowVersion.__table__.update().where(WorkflowVersion.id == row.id)
                              .values(retired_at=utcnow()))
        await session.commit()
    previous = type("Prev", (), {"pins": [pin(wf, info, 1)]})()
    spec = base_spec(wf, info)
    async with SessionLocal() as session:
        assert "app.pin_retired" in codes(await lint_app(session, spec, {}))
        spec["pins"] = [pin(wf, info, 2)]
        issues = await lint_app(session, spec, {}, previous=previous)
        assert "app.upgrade_policy" in codes(issues)
        assert next(i for i in issues if i["code"] == "app.upgrade_policy")["alias"] == "main"
        ok = await lint_app(session, spec, {}, previous=previous, dispositions={"main": "incomparable"})
        assert "app.upgrade_policy" not in codes(ok)
    assert lint_mod.needs_disposition(previous, spec["pins"]) == [{"alias": "main", "from": 1, "to": 2}]


async def test_process_pins(monkeypatch):
    from app.capabilities import registry
    from app.engine import governance
    from app.engine.schema import ValidationIssue

    wf, info = await make_workflow(approval())
    spec = base_spec(wf, info)
    spec["pins"] = [pin(wf, info, use="process")]

    class Cap:
        effect = "write"
        lookup = None

    async def get_capability(session, name):
        return Cap() if name == "erp_pay" else None

    real = governance.publish_issues

    def publish_issues(spec, **kw):
        return [*real(spec, **kw), ValidationIssue(level="warning", message="子工作流里有人工节点", code="process.human_in_subgraph",
                                                   node_id="check")]

    monkeypatch.setattr(registry, "get_capability", get_capability)
    monkeypatch.setattr(governance, "publish_issues", publish_issues)
    async with SessionLocal() as session:
        issues = await lint_app(session, spec, {})
        assert {"app.slot_unbound", "app.reconciler_unbound", "app.process_unsafe"} <= codes(issues, "error")
        unsafe = next(i for i in issues if i["code"] == "app.process_unsafe")
        assert "子工作流里有人工节点" in unsafe["message"] and unsafe["node_id"] == "check"
        spec["slots"] = {"finance": {"label": "财务审批"}}
        bound = {"slots": {"finance": [{"kind": "group", "name": "财务"}], "reconciler": [{"kind": "user", "name": "a"}]}}
        issues = await lint_app(session, spec, bound)
        assert not {"app.slot_unbound", "app.reconciler_unbound"} & codes(issues)


async def test_on_demand_views_on_live_sources_need_a_ttl(tmp_path):
    name = f"live_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        session.add(DataSource(name=name, kind="sqlite", database=str(tmp_path / "x.db"), enabled=True))
        await session.commit()
    wf, info = await make_workflow(weekly(name))
    spec = base_spec(wf, info, views={"od": {"source": {"kind": "on_demand", "pin": "main"}}})
    async with SessionLocal() as session:
        assert "app.on_demand_ttl" in codes(await lint_app(session, spec, {}), "error")
        spec["views"]["od"]["source"]["ttl_s"] = 600
        assert "app.on_demand_ttl" not in codes(await lint_app(session, spec, {}))
        spec["views"]["od"]["source"]["ttl_s"] = 100_000
        assert "app.on_demand_ttl" in codes(await lint_app(session, spec, {}))


async def test_a_bad_shape_is_app_format():
    async with SessionLocal() as session:
        issues = await lint_app(session, {"format": "agentlab.app/1", "name": "x", "pins": [{"alias": "m"}]}, {})
    assert codes(issues) == {"app.format"} and issues[0]["path"].startswith("pins")


# --------------------------------------------------------------------------
# 派生
# --------------------------------------------------------------------------


async def test_derive_a_dashboard_prefers_the_schedule():
    wf, info = await make_workflow(weekly())
    async with SessionLocal() as session:
        sch = Schedule(name="周一", workflow_id=wf, cron="0 9 * * 1", timezone="Asia/Shanghai", run_class="formal")
        session.add(sch)
        await session.commit()
        spec = await derive_app(session, wf, 1)
    assert spec["format"] == "agentlab.app/1" and spec["pins"][0]["use"] == "view"
    assert spec["pins"][0]["graph_hash"] == info["hash"] and spec["pins"][0]["level"] == "governed"
    assert spec["views"]["latest"]["source"] == {"kind": "latest_sealed", "pin": "main", "from": f"schedule:{sch.id}",
                                                 "params": None, "ttl_s": None, "auto": False}
    home = spec["pages"][0]
    types = [c["type"] for c in home["body"]]
    assert types[:4] == ["stamp", "section", "report", "table"]
    metric = home["body"][1]["children"][0]["children"][0]
    assert metric["bind"] == {"view": "latest", "metric": "card.gmv"} and metric["props"]["label"] == "销售额"
    assert spec["forms"]["start"]["required"] == ["week"]
    assert spec["entries"][0]["map"] == {"week": "form.week", "region": "form.region"}
    async with SessionLocal() as session:
        issues = await lint_app(session, spec, {})
    assert codes(issues, "error") == set(), issues


async def test_derive_a_process_leaves_slots_empty(monkeypatch):
    from app.capabilities import registry

    class Cap:
        effect = "write"
        lookup = None

    async def get_capability(session, name):
        return Cap() if name == "erp_pay" else None

    monkeypatch.setattr(registry, "get_capability", get_capability)
    wf, info = await make_workflow(approval())
    async with SessionLocal() as session:
        spec = await derive_app(session, wf, 1, name="报销审批")
    assert spec["name"] == "报销审批" and spec["pins"][0]["use"] == "process"
    assert spec["slots"] == {"finance": {"label": "finance"}, "reconciler": {"label": "动作核实"}}
    # 流程的首页只放发起和我发起的：结果在实例页看，不派生「最近封存运行」（派生出来只会挂一块永远空着的印章）
    assert "latest" not in spec["views"] and spec["views"]["inst"]["source"]["kind"] == "instance"
    home = next(p for p in spec["pages"] if p["id"] == "home")
    assert [c["type"] for c in home["body"]] == ["entry_button", "instance_list"]
    inst = next(p for p in spec["pages"] if p["id"] == "instance")
    assert {"task_form", "signoffs", "action_status", "instance_header"} <= {c["type"] for c in inst["body"]}
    assert spec["task_ui"] == {"check": {"title": "财务审批"}}
    async with SessionLocal() as session:
        issues = await lint_app(session, spec, {})
    # 岗位留空等人绑定：检查拦着
    assert {"app.slot_unbound", "app.reconciler_unbound"} <= codes(issues, "error")


async def test_an_empty_dashboard_says_when_data_will_come():
    """看板还没有封存运行：不是解析出错，写明还没有数据、什么时候会有（定时任务下次运行的时间，或者要等一次正式运行）。"""
    from datetime import datetime, timezone

    from app.apps.pins import load_pins
    from app.apps.resolve import _empty_text

    wf, info = await make_workflow(weekly())
    async with SessionLocal() as session:
        on = Schedule(name="周一早上", workflow_id=wf, cron="0 9 * * 1", timezone="Asia/Shanghai", run_class="formal",
                      next_run_at=datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc))
        paused = Schedule(name="停着的", workflow_id=wf, cron="0 9 * * 1", timezone="Asia/Shanghai",
                          run_class="formal", enabled=False)
        session.add_all([on, paused])
        await session.commit()
        state = (await load_pins(session, base_spec(wf, info)))["main"]
        assert "定时任务「周一早上」下一次在 2026-10-19" in await _empty_text(session, state, f"schedule:{on.id}")
        assert "定时任务「停着的」已停用" in await _empty_text(session, state, f"schedule:{paused.id}")
        assert "已删除" in await _empty_text(session, state, "schedule:gone")
        assert (await _empty_text(session, state, "formal")).endswith(f"「{info['name']} v1」有一次正式运行成功并封存后这里就有数")


def test_slot_refs_cover_every_place_a_graph_names_a_slot():
    """派给谁、超时升级给谁、动作确认派给谁，加上节点上没写、取图级 defaults 的：派生和检查认的是同一份。调用工具节点只有
    写入的（会开发送确认卡）才算用到岗位：只读的写了派给谁、或者从 defaults 继承来的，都不算。"""
    from app.apps.tree import slot_refs

    graph = chain(
        node("a", "human", "财务会签", assignee={"kind": "slot", "slot": " finance "}, due="PT1H",
             on_timeout={"action": "escalate", "to_slot": "director"}),
        node("b", "human", "复核"),
        node("pay", "tool", "付款", tool="erp_pay", assignee={"kind": "slot", "slot": "cashier"}),
        node("fetch", "tool", "取数", tool="erp_orders", assignee={"kind": "slot", "slot": "clerk"}),
        node("sync", "tool", "同步", tool="erp_sync"),
        node("c", "human", "补材料", assignee={"kind": "initiator"}, due="PT1H",
             on_timeout={"action": "remind", "to_slot": "不算"}),
    )
    graph["defaults"] = {"assignee": {"kind": "slot", "slot": "auditor"}}
    assert [(r["slot"], r["node_id"], r["role"]) for r in slot_refs(graph, {"pay", "sync"})] == [
        ("finance", "a", "assignee"), ("director", "a", "escalation"), ("auditor", "b", "assignee"),
        ("cashier", "pay", "confirm"), ("auditor", "sync", "confirm")]
    # 没有写入节点：调用工具节点一个都不算
    assert [r["node_id"] for r in slot_refs(graph)] == ["a", "a", "b"]


async def test_read_only_tool_nodes_do_not_use_slots_and_writes_follow_the_runtime_rule(monkeypatch):
    """只读的调用工具节点从不开确认卡：它写的派给谁（或图级默认的）不派生岗位、检查也不说「发送确认派给它」。哪些是写入
    和运行时同一个认法（writes_externally）：绑定端点、视参数而定的也是写入，要动作状态、要人核实。"""
    from types import SimpleNamespace

    from app.capabilities import registry

    caps = {"erp_pay": SimpleNamespace(effect="write", endpoint_id=None, lookup="erp_lookup"),
            "erp_orders": SimpleNamespace(effect="read", endpoint_id="ep-1", lookup=None),
            "erp_sync": SimpleNamespace(effect="dynamic", endpoint_id="ep-1", lookup=None)}

    async def get_capability(session, name):
        return caps.get(name)

    monkeypatch.setattr(registry, "get_capability", get_capability)
    graph = chain(
        node("start", "input", "开始", fields=[{"name": "amount", "type": "number", "required": True}]),
        node("fetch", "tool", "取数", tool="erp_orders", args={}, assignee={"kind": "slot", "slot": "clerk"}),
        node("check", "human", "财务审批", mode="approve", assignee={"kind": "slot", "slot": "finance"}),
        node("pay", "tool", "付款", tool="erp_pay", args={}),
        node("sync", "tool", "同步", tool="erp_sync", args={}),
        node("out", "output", "成果", fields=[{"name": "r", "value": "完成"}]),
    )
    graph["defaults"] = {"assignee": {"kind": "slot", "slot": "cashier"}}
    wf, _ = await make_workflow(graph)
    async with SessionLocal() as session:
        spec = await derive_app(session, wf, 1)
        assert set(spec["slots"]) == {"finance", "cashier", "reconciler"}
        inst = next(p for p in spec["pages"] if p["id"] == "instance")
        assert [c["bind"]["action"] for c in inst["body"] if c["type"] == "action_status"] == ["pay", "sync"]
        issues = await lint_app(session, spec, {"slots": {"finance": [{"kind": "user", "name": "u"}]}})
    unbound = [i for i in issues if i["code"] == "app.slot_unbound"]
    cashier = next(i for i in unbound if i["path"] == "slots.cashier")
    assert "调用工具节点「付款」的发送确认派给它" in cashier["message"] and "「同步」" in cashier["message"]
    assert not any("取数" in i["message"] for i in issues), [i["message"] for i in issues]
    [reconcile] = [i for i in issues if i["code"] == "app.reconciler_unbound"]
    assert reconcile["node_id"] == "sync" and "「同步」会写外部系统" in reconcile["message"]


async def test_escalation_and_confirm_slots_are_derived_and_must_be_bound(monkeypatch):
    """超时升级的岗位、动作确认的岗位以前既不派生也不检查：发布后升级、确认悄悄派给了任何人。"""
    from app.capabilities import registry

    class Cap:
        effect = "write"
        lookup = "erp_lookup"

    async def get_capability(session, name):
        return Cap() if name == "erp_pay" else None

    monkeypatch.setattr(registry, "get_capability", get_capability)
    graph = approval()
    graph["nodes"][1]["data"]["config"].update(due="PT1H", on_timeout={"action": "escalate", "to_slot": "director"})
    graph["nodes"][2]["data"]["config"]["assignee"] = {"kind": "slot", "slot": "cashier"}
    wf, info = await make_workflow(graph)
    async with SessionLocal() as session:
        spec = await derive_app(session, wf, 1)
        assert set(spec["slots"]) == {"finance", "director", "cashier"}
        everyone = {"slots": {s: [{"kind": "user", "name": "u"}] for s in ("finance", "director", "cashier")}}
        assert not codes(await lint_app(session, spec, everyone)) & {"app.slot_unbound"}

        # 绑了两个、升级给的总监没绑：报错，说清是哪一步要它
        issues = await lint_app(session, spec, {"slots": {k: v for k, v in everyone["slots"].items() if k != "director"}})
        [unbound] = [i for i in issues if i["code"] == "app.slot_unbound"]
        assert unbound["level"] == "error" and unbound["path"] == "slots.director"
        assert "人工节点「财务审批」超时升级给它" in unbound["message"]

        # 应用里根本没声明：按引用它的那一步报，落到节点上
        del spec["slots"]["cashier"]
        issues = await lint_app(session, spec, everyone)
        undeclared = next(i for i in issues if i["code"] == "app.slot_unbound" and i.get("node_id") == "pay")
        assert "调用工具节点「付款」的发送确认派给岗位「cashier」" in undeclared["message"]


async def test_describe_a_version_has_no_secrets():
    graph = copy.deepcopy(weekly())
    graph["nodes"][1]["data"]["config"]["args"] = {"sql": "SELECT * FROM t WHERE phone = '19912345678'"}
    wf, _ = await make_workflow(graph)
    async with SessionLocal() as session:
        row = (await session.execute(WorkflowVersion.__table__.select().where(WorkflowVersion.workflow_id == wf))).first()
    text = describe_version(row, workflow_name="周报")
    assert "工作流「周报」v1（受管级" in text and "card.gmv 销售额（元）" in text and "报告撰写「写周报」" in text
    assert "week（string，必填）：统计周" in text
    assert "19912345678" not in text and "SELECT" not in text


# --------------------------------------------------------------------------
# 权限
# --------------------------------------------------------------------------


class _App:
    created_by = "alice"


class _Grant:
    def __init__(self, kind, who, role):
        self.principal_kind, self.principal, self.role = kind, who, role


@pytest.mark.parametrize("principal, grants, want", [
    (Principal(username=None, mode="off"), [], "manage"),
    (Principal(username="root", role="admin", mode="local", verified=True), [], "manage"),
    (Principal(username="alice", role="builder", mode="local", verified=True), [], "manage"),
    (Principal(username="bob", role="builder", mode="local", verified=True), [], "observe"),
    (Principal(username="bob", role="builder", mode="local", verified=True), [_Grant("user", "bob", "manage")], "manage"),
    (Principal(username="carol", role="user", mode="local", verified=True), [], None),
    (Principal(username="carol", role="user", mode="local", verified=True, groups=("财务",)),
     [_Grant("group", "财务", "observe"), _Grant("everyone", "", "use")], "observe"),
    (Principal(username="dan", role="user", mode="oidc", verified=True), [_Grant("everyone", "", "use")], "use"),
])
def test_app_roles(principal, grants, want):
    assert role_of(principal, _App(), grants) == want


async def test_text_numbers_from_the_pinned_process_have_a_source():
    """流程规则的数（金额线、时限）出处是固定版本里的节点配置：对得上就不报；流程里没有的规则写在说明里照样报——那句话是假的。"""
    graph = approval()
    graph["nodes"][1]["data"]["config"].update(due="PT48H", condition="input.amount > 5000")
    wf, info = await make_workflow(graph)
    text = lambda md: base_spec(wf, info, pages=[{"id": "home", "title": "首页", "body": [  # noqa: E731
        {"id": "t", "type": "text", "props": {"markdown": md}}]}])
    async with SessionLocal() as session:
        ok = await lint_app(session, text("金额超过 5,000 元由财务审批，48 小时内处理"), {})
        bad = await lint_app(session, text("金额超过 8000 元需要总监会签，48 小时内处理"), {})
    assert "app.text_number" not in codes(ok)
    issue = next(i for i in bad if i["code"] == "app.text_number")
    # 只列对不上的那个数，48 小时在节点配置里写着
    assert issue["level"] == "error" and "「8000」" in issue["message"] and "48" not in issue["message"]
