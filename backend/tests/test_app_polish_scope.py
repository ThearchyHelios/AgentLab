"""助手润色只改呈现（D-h；设计文档 A8 第 2 条）。

AppApplier 只放行组件操作和页面（app_add / app_update / app_remove / app_move / app_page）；app_set 只放行待办外观
（task_ui）和表单里的外观键（title、description、x-order、x-widget、x-enum-labels）。改钉住的版本、入口、视图、岗位、
表单的数据约束一律拒（Applied(False, False, problem)）；拒掉的原因在自查时交回模型一次，交付时列给人看。
编辑器自己调 apply_app_op 不受这层限制。
"""
from __future__ import annotations

import copy

from app.api.copilot_gen import POLISH_REFUSED, AppApplier, AppChecker
from app.apps.ops import apply_app_op, polish_problem


def spec() -> dict:
    return {
        "format": "agentlab.app/1", "name": "报销",
        "pins": [{"alias": "main", "workflow_id": "wf", "version": 1, "graph_hash": "h" * 64, "use": "entry"}],
        "slots": {"finance": {"label": "财务"}},
        "entries": [{"id": "apply", "label": "发起", "pin": "main", "form": {"$ref": "#/forms/apply"}, "map": {}}],
        "forms": {"apply": {"type": "object", "required": ["amount"], "title": "报销单", "properties": {
            "amount": {"type": "number", "title": "金额", "minimum": 0},
            "kind": {"type": "string", "enum": ["travel", "meal"], "x-enum-labels": ["差旅", "餐费"]}}}},
        "views": {"inst": {"source": {"kind": "instance"}}},
        "task_ui": {},
        "pages": [{"id": "home", "title": "首页", "body": [{"id": "new", "type": "entry_button",
                                                            "props": {"entry": "apply"}}]}],
    }


def test_component_and_page_operations_pass():
    s = spec()
    applier = AppApplier(s)
    assert applier.apply({"op": "app_update", "id": "new", "props": {"label": "我要报销"}}).changed
    assert applier.apply({"op": "app_page", "page": {"id": "mine", "title": "我发起的"}}).changed
    assert applier.apply({"op": "app_set", "path": "task_ui", "key": "review",
                          "value": {"title": "财务审批", "x-order": ["note"]}}).changed
    assert applier.refused == []


def test_form_looks_pass_but_constraints_do_not():
    s = spec()
    applier = AppApplier(s)
    looks = copy.deepcopy(s["forms"]["apply"])
    looks["title"] = "差旅报销单"
    looks["x-order"] = ["kind", "amount"]
    looks["properties"]["amount"].update({"title": "金额（元）", "description": "含税", "x-widget": "number"})
    looks["properties"]["kind"]["x-enum-labels"] = ["出差", "吃饭"]
    assert applier.apply({"op": "app_set", "path": "forms", "key": "apply", "value": looks}).changed
    assert s["forms"]["apply"]["properties"]["amount"]["title"] == "金额（元）"

    for change in (
        lambda f: f["properties"]["amount"].update(minimum=10),          # 取值范围
        lambda f: f.update(required=[]),                                  # 必填
        lambda f: f["properties"]["kind"].update(enum=["travel"]),        # 可选值
        lambda f: f["properties"].update(extra={"type": "string"}),       # 新字段
        lambda f: f["properties"]["amount"].update(type="string"),        # 类型
    ):
        form = copy.deepcopy(s["forms"]["apply"])
        change(form)
        out = applier.apply({"op": "app_set", "path": "forms", "key": "apply", "value": form})
        assert (out.changed, out.forward) == (False, False) and "数据约束" in out.problem
    assert s["forms"]["apply"] == looks, "被拒的改动一处都没落地"
    assert "新增表单" in applier.apply({"op": "app_set", "path": "forms", "key": "other",
                                        "value": {"type": "object", "properties": {}}}).problem
    assert "删掉" in applier.apply({"op": "app_set", "path": "forms", "key": "apply", "value": None}).problem


def test_pins_entries_views_slots_are_refused():
    s = spec()
    before = copy.deepcopy(s)
    applier = AppApplier(s)
    for path, value in (("entries", {"label": "改", "pin": "main", "form": {}, "map": {}}),
                        ("views", {"source": {"kind": "instance"}}), ("slots", {"label": "总监"}),
                        ("pins", {"alias": "x"})):
        out = applier.apply({"op": "app_set", "path": path, "key": "k", "value": value})
        assert (out.changed, out.forward) == (False, False) and "润色只改呈现" in out.problem, path
    assert s == before
    assert [r["path"] for r in applier.refused] == ["entries.k", "views.k", "slots.k", "pins.k"]
    # 编辑器不受这层限制：同一个操作直接落地
    assert apply_app_op(s, {"op": "app_set", "path": "slots", "key": "k", "value": {"label": "总监"}}).changed
    assert polish_problem(s, {"op": "app_add"}) is None


async def test_refusals_go_back_to_the_model_once_and_are_listed_for_people(monkeypatch):
    from app.apps import lint

    async def _lint(session, spec, bindings, **_):
        return []

    monkeypatch.setattr(lint, "lint_app", _lint)
    applier = AppApplier(spec())
    checker = AppChecker(applier)
    applier.apply({"op": "app_set", "path": "views", "key": "inst", "value": {"source": {"kind": "instance"}}})
    first = await checker.issues()
    assert [(i["code"], i["level"], i["path"]) for i in first] == [(POLISH_REFUSED, "error", "views.inst")]
    assert "润色只改呈现" in checker.repair_request(first, "")
    assert await checker.issues() == [], "同一条只交回一次，不在后面几轮反复交回"
    assert [(i["code"], i["level"]) for i in applier.refusals()] == [(POLISH_REFUSED, "warning")]
