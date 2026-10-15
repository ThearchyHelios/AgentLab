"""表单 agentlab.form/1（应用平台 §3.3）与可见性表达式的 roots / arithmetic（A2、F10）。

- 表单只认子集：pattern 一律不收（JS 和 Python 的正则语义不同）；数组只能是枚举字符串的多选
- 校验错误统一 {path, code, message}，code 取那九种；没填的非必填字段不校验格式
- 输入节点字段 → 表单：没写类型按 string；有默认值的必填字段不逼人填
- 表达式：不给 roots / arithmetic 时和以前一模一样；应用里禁止算术、只认 APP_ROOTS
"""
from __future__ import annotations

import pytest

from app.apps.forms import input_schema, validate_form, validate_form_schema
from app.apps.spec import APP_ROOTS
from app.engine.expressions import ExpressionArithmetic, ExpressionError, eval_expression, parse_expression

FORM = {
    "format": "agentlab.form/1", "type": "object", "required": ["amount", "kind"],
    "properties": {
        "amount": {"type": "number", "title": "金额（元）", "minimum": 0, "maximum": 100000},
        "kind": {"type": "string", "title": "类别", "enum": ["差旅", "餐饮"], "x-enum-labels": ["差旅", "餐饮"]},
        "note": {"type": "string", "title": "说明", "maxLength": 10, "x-widget": "textarea"},
        "day": {"type": "string", "title": "日期", "format": "date"},
        "mail": {"type": "string", "title": "邮箱", "format": "email"},
        "tags": {"type": "array", "title": "标签", "items": {"type": "string", "enum": ["a", "b", "c"]}, "maxItems": 2},
        "count": {"type": "integer", "title": "张数"},
        "urgent": {"type": "boolean", "title": "加急"},
    },
    "x-order": ["amount", "kind", "note", "day", "mail", "tags", "count", "urgent"],
}


def test_the_subset_is_accepted():
    assert validate_form_schema(FORM) == []


@pytest.mark.parametrize("patch, needle", [
    ({"properties": {"x": {"type": "string", "pattern": "^a+$"}}}, "不支持正则校验"),
    ({"properties": {"x": {"type": "object"}}}, "类型只能是"),
    ({"properties": {"x": {"type": "string", "format": "ipv4"}}}, "格式只能是"),
    ({"properties": {"x": {"type": "string", "x-widget": "slider"}}}, "控件只能是"),
    ({"properties": {"x": {"type": "array", "items": {"type": "number"}}}}, "多选"),
    ({"properties": {"x": {"type": "string", "minLength": -1}}}, "最少字数"),
    ({"required": ["ghost"], "properties": {"x": {"type": "string"}}}, "必填项「ghost」"),
    ({"properties": {"x": {"type": "string"}}, "allOf": []}, "不支持这些写法"),
])
def test_anything_outside_the_subset_is_refused(patch, needle):
    form = {"type": "object", **patch}
    problems = validate_form_schema(form)
    assert any(needle in p for p in problems), problems


def test_values_are_checked_with_one_error_shape():
    good = {"amount": 12.5, "kind": "差旅", "note": "出差", "day": "2026-10-14", "mail": "a@b.cn", "tags": ["a"],
            "count": 2, "urgent": True}
    assert validate_form(FORM, good) == []
    bad = {"amount": -1, "kind": "住宿", "note": "这是一段很长很长的说明文字", "day": "2026/10/14", "mail": "nope",
           "tags": ["a", "b", "c"], "count": 1.5, "urgent": "是"}
    errors = validate_form(FORM, bad)
    got = {(e["path"], e["code"]) for e in errors}
    assert got == {("amount", "minimum"), ("kind", "enum"), ("note", "max_length"), ("day", "format"),
                   ("mail", "format"), ("tags", "max_items"), ("count", "type"), ("urgent", "type")}
    assert all(set(e) == {"path", "code", "message"} and e["message"] for e in errors)
    assert next(e for e in errors if e["path"] == "amount")["message"] == "「金额（元）」不能小于 0"


def test_required_and_blank_values():
    errors = validate_form(FORM, {"amount": "", "day": ""})
    assert {(e["path"], e["code"]) for e in errors} == {("amount", "required"), ("kind", "required")}
    assert next(e for e in errors if e["path"] == "kind")["message"] == "请填写「类别」"
    # 没填的非必填字段不查格式；类型不对的不再另报格式
    assert validate_form(FORM, {"amount": 1, "kind": "差旅", "mail": ""}) == []
    assert [(e["path"], e["code"]) for e in validate_form(FORM, {"amount": "abc", "kind": "差旅"})] == [("amount", "type")]
    assert validate_form(FORM, None)[0]["code"] == "type"


def test_input_fields_become_a_form():
    fields = [
        {"name": "week", "required": True, "description": "统计周"},
        {"name": "region", "type": "string", "enum": ["华东", "华南"], "default": "华东", "required": True},
        {"name": "day", "type": "string", "format": "date"},
        {"name": "top", "type": "integer", "label": "前几名"},
        {"name": "regions", "type": "array", "enum": ["华东", "华南"]},
        {"name": "loose", "type": "array"},
        {"name": "odd", "type": "object"},
        {"name": ""},
    ]
    form = input_schema(fields)
    assert form["format"] == "agentlab.form/1" and form["type"] == "object"
    props = form["properties"]
    assert list(props) == ["week", "region", "day", "top", "regions", "loose", "odd"] == form["x-order"]
    assert props["week"] == {"type": "string", "title": "week", "description": "统计周"}
    assert props["region"] == {"type": "string", "title": "region", "enum": ["华东", "华南"], "default": "华东"}
    assert props["day"]["format"] == "date" and props["top"] == {"type": "integer", "title": "前几名"}
    assert props["regions"] == {"type": "array", "title": "regions", "items": {"type": "string", "enum": ["华东", "华南"]}}
    # 没写可选项的多选、认不出的类型退回文字
    assert props["loose"]["type"] == props["odd"]["type"] == "string"
    # 有默认值的必填字段：没填时入口节点补默认值，不逼人填
    assert form["required"] == ["week"]
    assert validate_form_schema(form) == []


def test_number_bounds_carry_into_the_form_and_are_enforced():
    """数字字段的上下限带进表单（只带数字类型、写成数的）；服务端校验照它拦，前端同一份子集（lib/schemaForm.ts）照它拦。"""
    form = input_schema([
        {"name": "amount", "type": "number", "minimum": 0.01, "maximum": 50000, "required": True},
        {"name": "count", "type": "integer", "minimum": 1},
        {"name": "note", "minimum": 3},
        {"name": "bad", "type": "number", "minimum": "零"},
    ])
    props = form["properties"]
    assert (props["amount"]["minimum"], props["amount"]["maximum"]) == (0.01, 50000) and props["count"]["minimum"] == 1
    assert "minimum" not in props["note"] and "minimum" not in props["bad"]
    assert validate_form_schema(form) == []
    assert [e["code"] for e in validate_form(form, {"amount": -5})] == ["minimum"]
    assert [e["code"] for e in validate_form(form, {"amount": 0})] == ["minimum"]
    assert validate_form(form, {"amount": 60000})[0]["message"] == "「amount」不能大于 50000"
    assert validate_form(form, {"amount": 0.01, "count": 1}) == []


def test_the_seeded_expense_template_refuses_a_negative_amount():
    """样例⑩：金额是数、不能小于 0.01（表单子集没有 exclusiveMinimum，「大于 0」写成 0.01）；待办标题带上金额。"""
    from app.apps.tree import input_fields
    from app.seed import TEMPLATES

    template = next(t for t in TEMPLATES if t["name"] == "⑩ 报销审批（会签）")
    form = input_schema(input_fields({"nodes": template["nodes"]}))
    assert form["properties"]["amount"] == {"type": "number", "title": "amount", "description": "金额（元）",
                                            "minimum": 0.01}
    assert validate_form(form, {"applicant": "钱七", "amount": -5, "reason": "出差"})[0]["code"] == "minimum"
    assert validate_form(form, {"applicant": "钱七", "amount": "很多", "reason": "出差"})[0]["code"] == "type"
    assert validate_form(form, {"applicant": "钱七", "amount": 1280.5, "reason": "出差"}) == []
    review = next(n for n in template["nodes"] if n["id"] == "review")["data"]["config"]
    assert "{{ input.amount }}" in review["title"] and "{{ input.amount }}" in review["message"]


# --------------------------------------------------------------------------
# 表达式
# --------------------------------------------------------------------------


def test_defaults_keep_todays_behaviour():
    assert eval_expression("vars.a * 2 + 1", {"vars": {"a": 3}}) == 7
    assert parse_expression("vars.a - nodes.b")[2] == []
    assert parse_expression("run.tier == 'formal'")[2] == ["run"]


@pytest.mark.parametrize("expr", [
    "metric.kpi.gmv * 2 > 100", "run.version + 1 == 3", "-metric.kpi.gmv < 0", "abs(metric.kpi.gmv) > 1",
    "round(metric.kpi.gmv) == 4", "max(metric.kpi.gmv, 1) > 0", "metric.kpi.gmv % 2 == 0",
])
def test_apps_refuse_arithmetic(expr):
    with pytest.raises(ExpressionArithmetic):
        parse_expression(expr, roots=APP_ROOTS, arithmetic=False)
    with pytest.raises(ExpressionArithmetic):
        eval_expression(expr, {"metric": {"kpi": {"gmv": 4}}, "run": {"version": 2}}, roots=APP_ROOTS,
                        arithmetic=False)


def test_apps_allow_comparison_boolean_lookup_and_in():
    ctx = {"run": {"tier": "degraded", "status": "succeeded"}, "metric": {"kpi": {"gmv": -5}},
           "user": {"role": "user", "groups": ["财务"]}, "vars": {"secret": 1}}
    expr = "run.tier != 'formal' and run.status in ['succeeded'] and metric.kpi.gmv < -1 and '财务' in user.groups"
    assert parse_expression(expr, roots=APP_ROOTS, arithmetic=False)[2] == []
    assert eval_expression(expr, ctx, roots=APP_ROOTS, arithmetic=False) is True
    # 不认识的根名字报出来；就算上下文里有，也够不着
    assert parse_expression("vars.secret == 1", roots=APP_ROOTS, arithmetic=False)[2] == ["vars"]
    assert eval_expression("vars.secret", ctx, roots=APP_ROOTS, arithmetic=False) is None
    with pytest.raises(ExpressionError):
        parse_expression("run.tier ==", roots=APP_ROOTS, arithmetic=False)
