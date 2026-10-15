"""蓝图的 bp_* 操作和卡片白名单（docs/design/normal-user-apps.md N2、§3.2、§3.13 bp.*）。

守的是：
- 每一项带来源和状态：用户写的「已确认」、助手写的「待确认」、写空的「缺」；确认只能由用户来做，值和来源不变；
- 各卡片的白名单：几个人同意只能是 any / all / quorum（quorum 要写人数）；时限只收能换算成 ISO 8601 时长的说法；
  金额线只许「字段 比较 常量」；写回只能选能力登记里的写入能力；字段对应只能取表单字段、指标、核对的行或常量；
  「谁能看」只收用户的；反问只能助手问、用户答；
- bp_template 按模板预置还没填的卡片（来源「模板默认」）；
- 不改入参；不合规的抛 BpRefused，带 bp.* 机器码；
- blueprint_issues：缺项、没对到数据、没人处理、超出模板能力、发布前没确认的项。
"""
from __future__ import annotations

import copy

import pytest

from app.blueprints.ops import BpRefused, apply_bp_op, blueprint_issues, duration_of
from app.blueprints.spec import empty_blueprint


def board() -> dict:
    spec = apply_bp_op(empty_blueprint(), {"op": "bp_template", "template": {"id": "periodic_board", "version": 1}},
                       source="user")
    return spec


def refused(spec: dict, op: dict, *, source: str = "assistant") -> BpRefused:
    with pytest.raises(BpRefused) as e:
        apply_bp_op(spec, op, source=source)
    return e.value


def test_items_carry_source_and_status_and_input_is_not_mutated():
    spec = empty_blueprint()
    before = copy.deepcopy(spec)
    out = apply_bp_op(spec, {"op": "bp_set", "path": "/purpose", "value": " 每周看销售额 "}, source="assistant")
    assert spec == before
    assert out["purpose"] == {"value": "每周看销售额", "source": "assistant", "status": "proposed", "why": None}
    out = apply_bp_op(out, {"op": "bp_set", "path": "/purpose/why", "value": "用户第一句话说的"}, source="assistant")
    assert out["purpose"]["why"] == "用户第一句话说的"
    # 确认只能由用户来做：值和来源不变，只是状态变成已确认
    assert "确认只能由你来做" in refused(out, {"op": "bp_set", "path": "/purpose/status", "value": "confirmed"}).message
    confirmed = apply_bp_op(out, {"op": "bp_set", "path": "/purpose/status", "value": "confirmed"}, source="user")
    assert confirmed["purpose"] == {**out["purpose"], "status": "confirmed"}
    # 用户直接改：已确认，来源记用户；写空：缺
    mine = apply_bp_op(out, {"op": "bp_set", "path": "/purpose", "value": "每天看"}, source="user")
    assert (mine["purpose"]["source"], mine["purpose"]["status"]) == ("user", "confirmed")
    cleared = apply_bp_op(mine, {"op": "bp_remove", "path": "/purpose"}, source="user")
    assert (cleared["purpose"]["value"], cleared["purpose"]["status"]) == (None, "missing")
    assert "没有值" in refused(cleared, {"op": "bp_set", "path": "/purpose/status", "value": "confirmed"},
                              source="user").message


def test_template_prefills_with_template_source():
    spec = board()
    assert spec["template"] == {"id": "periodic_board", "version": 1}
    assert spec["cadence"]["source"] == "template" and spec["cadence"]["status"] == "proposed"
    assert spec["cadence"]["value"]["preset"] == "weekly"
    approval = apply_bp_op(empty_blueprint(), {"op": "bp_template", "template": {"id": "approval", "version": 1}},
                           source="assistant")
    assert approval["people"][0]["policy"]["value"] == {"kind": "any"}
    assert approval["people"][0]["who"]["status"] == "missing", "处理人要人来填，模板不替人定"
    # 已经写了的不动
    mine = apply_bp_op(empty_blueprint(), {"op": "bp_set", "path": "/cadence", "value": {
        "preset": "daily", "time": "08:30", "tz": "Asia/Shanghai"}}, source="user")
    again = apply_bp_op(mine, {"op": "bp_template", "template": {"id": "periodic_board", "version": 1}}, source="user")
    assert again["cadence"]["value"]["time"] == "08:30"
    e = refused(empty_blueprint(), {"op": "bp_template", "template": {"id": "nope", "version": 1}})
    assert e.code == "bp.template_mismatch"
    assert "第 1 版" in refused(empty_blueprint(), {"op": "bp_template", "template": {"id": "approval", "version": 9}}).message


def test_data_items_and_reuse_shape():
    spec = apply_bp_op(board(), {"op": "bp_add", "path": "/data", "value": {
        "id": "gmv", "label": "销售额", "unit": "元", "table": {"source": "shop", "table": "orders"},
        "date": {"column": "paid_at", "rule": "按支付日"}}}, source="assistant")
    item = spec["data"][0]
    assert item["table"]["value"] == {"source": "shop", "table": "orders"} and item["table"]["status"] == "proposed"
    assert item["measure"]["status"] == "missing"
    assert "已经有 id" in refused(spec, {"op": "bp_add", "path": "/data", "value": {"id": "gmv"}}).message
    assert "小写字母" in refused(spec, {"op": "bp_add", "path": "/data", "value": {"id": "GMV!"}}).message
    assert "不能写这些" in refused(spec, {"op": "bp_add", "path": "/data", "value": {"id": "x", "sql": "select"}}).message
    assert "数据源和表名" in refused(spec, {"op": "bp_set", "path": "/data/0/table", "value": {"source": "shop"}}).message
    reuse = {"workflow_id": "wf", "version": 3, "node_id": "kpi", "metric_id": "gmv"}
    spec = apply_bp_op(spec, {"op": "bp_set", "path": "/data/0/reuse", "value": reuse}, source="user")
    assert spec["data"][0]["reuse"] == reuse
    assert "版本号" in refused(spec, {"op": "bp_set", "path": "/data/0/reuse", "value": {**reuse, "version": "v3"}}).message
    spec = apply_bp_op(spec, {"op": "bp_remove", "path": "/data/0/reuse"}, source="user")
    assert spec["data"][0]["reuse"] is None
    # 插到某个位置、删一项
    spec = apply_bp_op(spec, {"op": "bp_add", "path": "/data/0", "value": {"id": "orders", "label": "订单数"}},
                       source="user")
    assert [d["id"] for d in spec["data"]] == ["orders", "gmv"]
    spec = apply_bp_op(spec, {"op": "bp_remove", "path": "/data/1"}, source="user")
    assert [d["id"] for d in spec["data"]] == ["orders"]
    assert "超出了列表的范围" in refused(spec, {"op": "bp_remove", "path": "/data/5"}).message


def test_cadence_whitelist():
    spec = empty_blueprint()
    ok = apply_bp_op(spec, {"op": "bp_set", "path": "/cadence", "value": {
        "preset": "monthly", "day": 1, "time": "07:00", "tz": "asia/shanghai", "period": "last_month"}}, source="user")
    assert ok["cadence"]["value"]["tz"] == "Asia/Shanghai"
    for bad, words in (({"preset": "hourly", "time": "09:00", "tz": "Asia/Shanghai"}, "每天、每周或每月"),
                       ({"preset": "weekly", "time": "09:00", "tz": "Asia/Shanghai"}, "周几"),
                       ({"preset": "monthly", "day": 31, "time": "09:00", "tz": "Asia/Shanghai"}, "28"),
                       ({"preset": "daily", "time": "9点", "tz": "Asia/Shanghai"}, "24 小时制"),
                       ({"preset": "daily", "time": "09:00", "tz": "火星"}, "时区"),
                       ({"preset": "daily", "time": "09:00", "tz": "UTC", "period": "last_year"}, "哪一期")):
        assert words in refused(spec, {"op": "bp_set", "path": "/cadence", "value": bad}).message, bad


def test_people_whitelist():
    spec = apply_bp_op(empty_blueprint(), {"op": "bp_add", "path": "/fields", "value": {
        "name": "amount", "spec": {"type": "number", "title": "金额", "required": True}}}, source="user")
    spec = apply_bp_op(spec, {"op": "bp_add", "path": "/people", "value": {
        "id": "finance", "label": "财务", "who": [{"kind": "group", "name": "财务组"}], "policy": "all", "due": "48 小时",
        "on_timeout": {"action": "escalate", "to": [{"kind": "user", "name": "director"}]},
        "threshold": {"field": "amount", "op": ">", "value": 5000}}}, source="assistant")
    step = spec["people"][0]
    assert step["policy"]["value"] == {"kind": "all"}
    assert step["due"]["value"] == "PT48H"
    assert step["on_timeout"]["value"]["to"] == [{"kind": "user", "name": "director"}]
    # 几个人同意：只能是 any / all / quorum，quorum 要写人数
    assert apply_bp_op(spec, {"op": "bp_set", "path": "/people/0/policy", "value": {"kind": "quorum", "m": 2}},
                       source="user")["people"][0]["policy"]["value"] == {"kind": "quorum", "m": 2}
    for bad in ({"kind": "majority"}, {"kind": "quorum"}, {"kind": "any", "m": 2}, {"kind": "quorum", "m": 0}):
        assert "同意" in refused(spec, {"op": "bp_set", "path": "/people/0/policy", "value": bad}).message, bad
    # 时限：能换算成 ISO 8601 时长的才收
    assert [duration_of(v) for v in ("2 天", "1周", "30 分钟", "P2D", "pt4h", "3 个工作日", "明天")] == \
        ["P2D", "P1W", "PT30M", "P2D", "PT4H", None, None]
    assert "时长" in refused(spec, {"op": "bp_set", "path": "/people/0/due", "value": "3 个工作日"}).message
    # 超时：只能提醒、升级、驳回，升级要写给谁；没有「自动同意」
    assert "升级给谁" in refused(spec, {"op": "bp_set", "path": "/people/0/on_timeout",
                                        "value": {"action": "escalate"}}).message
    assert "不会自动同意" in refused(spec, {"op": "bp_set", "path": "/people/0/on_timeout",
                                          "value": {"action": "approve"}}).message
    # 金额线：只许「字段 比较 常量」
    for bad, words in (({"field": "amount * 2", "op": ">", "value": 1}, "字段名"),
                       ({"field": "amount", "op": "in", "value": 1}, "比较"),
                       ({"field": "amount", "op": ">", "value": {"x": 1}}, "不能写算式"),
                       ({"field": "amount", "op": ">", "value": "5000"}, "文字只能比较相等"),
                       ({"field": "kind", "op": "==", "value": "差旅"}, "不在「填什么」里"),
                       ({"field": "amount", "op": ">", "value": 1, "and": "x"}, "字段 比较 常量")):
        assert words in refused(spec, {"op": "bp_set", "path": "/people/0/threshold", "value": bad}).message, bad
    # 处理人：岗位不和别的混着写，发起人不和别的混着写
    assert "岗位" in refused(spec, {"op": "bp_set", "path": "/people/0/who", "value": [
        {"kind": "slot", "name": "财务"}, {"kind": "user", "name": "zhang"}]}).message
    assert "种类" in refused(spec, {"op": "bp_set", "path": "/people/0/who", "value": [{"kind": "role", "name": "x"}]}).message


def test_fields_follow_the_form_rules():
    spec = apply_bp_op(empty_blueprint(), {"op": "bp_add", "path": "/fields", "value": {
        "name": "kind", "spec": {"type": "string", "title": "类别", "enum": ["a", "b"], "x-enum-labels": ["甲", "乙"]}}},
        source="assistant")
    assert spec["fields"][0]["spec"]["status"] == "proposed"
    assert "正则" in refused(spec, {"op": "bp_set", "path": "/fields/0/spec",
                                    "value": {"type": "string", "pattern": "^a"}}).message
    assert "统计期" in refused(spec, {"op": "bp_add", "path": "/fields", "value": {"name": "period_start"}}).message
    assert "已经有一个" in refused(spec, {"op": "bp_add", "path": "/fields", "value": {"name": "kind"}}).message
    assert "必填" in refused(spec, {"op": "bp_set", "path": "/fields/0/spec",
                                    "value": {"type": "string", "required": "yes"}}).message
    renamed = apply_bp_op(spec, {"op": "bp_set", "path": "/fields/0/name", "value": "类别"}, source="user")
    assert renamed["fields"][0]["name"] == "类别"
    assert apply_bp_op(spec, {"op": "bp_set", "path": "/fields/0/sensitive", "value": True},
                       source="user")["fields"][0]["sensitive"] is True


def test_writeback_only_takes_write_capabilities(monkeypatch):
    from app.capabilities import registry

    monkeypatch.setitem(registry._view, "bp_test_erp_post", {"effect": "write", "endpoint_id": "e1", "missing": False})
    monkeypatch.setitem(registry._view, "bp_test_erp_get", {"effect": "read", "endpoint_id": "e1", "missing": False})
    spec = apply_bp_op(empty_blueprint(), {"op": "bp_set", "path": "/writeback", "value": {
        "capability": "bp_test_erp_post", "mapping": {"amount": "form.amount", "kind": "'报销'", "n": 1, "all": "rows"},
        "confirm": True}}, source="assistant")
    assert spec["writeback"]["capability"]["value"] == "bp_test_erp_post"
    assert spec["writeback"]["mapping"]["status"] == "proposed"
    assert "不是写入能力" in refused(spec, {"op": "bp_set", "path": "/writeback/capability",
                                            "value": "bp_test_erp_get"}).message
    assert "能力登记里没有" in refused(spec, {"op": "bp_set", "path": "/writeback/capability",
                                              "value": "bp_test_nope"}).message
    assert "取值只能是" in refused(spec, {"op": "bp_set", "path": "/writeback/mapping",
                                          "value": {"amount": "input.amount + 1"}}).message
    assert "是或否" in refused(spec, {"op": "bp_set", "path": "/writeback/confirm", "value": "yes"}).message
    assert apply_bp_op(spec, {"op": "bp_remove", "path": "/writeback"}, source="user")["writeback"] is None


def test_access_is_for_people_and_asks_go_one_way():
    spec = empty_blueprint()
    e = refused(spec, {"op": "bp_add", "path": "/access/use", "value": {"kind": "group", "name": "门店"}})
    assert "授权是人的决定" in e.message
    spec = apply_bp_op(spec, {"op": "bp_add", "path": "/access/use", "value": {"kind": "group", "name": "门店"}},
                       source="user")
    assert spec["access"]["use"] == [{"kind": "group", "name": "门店"}]
    assert "已经在这一档" in refused(spec, {"op": "bp_add", "path": "/access/use",
                                            "value": {"kind": "group", "name": "门店"}}, source="user").message
    assert "人或组" in refused(spec, {"op": "bp_add", "path": "/access/observe",
                                      "value": {"kind": "slot", "name": "财务"}}, source="user").message
    spec = apply_bp_op(spec, {"op": "bp_ask", "question": "销售额按下单日还是支付日算？", "options": ["按下单日", "按支付日"],
                              "path": "/data/0/date"}, source="assistant")
    assert spec["asks"][0] == {"id": "q1", "question": "销售额按下单日还是支付日算？", "options": ["按下单日", "按支付日"],
                               "path": "/data/0/date", "answer": None}
    assert "直接回答" in refused(spec, {"op": "bp_ask", "question": "?"}, source="user").message
    assert "由你来回答" in refused(spec, {"op": "bp_set", "path": "/asks/0/answer", "value": "按支付日"}).message
    spec = apply_bp_op(spec, {"op": "bp_set", "path": "/asks/0/answer", "value": "按支付日"}, source="user")
    assert spec["asks"][0]["answer"] == "按支付日"
    assert "bp_template" in refused(spec, {"op": "bp_set", "path": "/template", "value": {}}).message
    assert "路径" in refused(spec, {"op": "bp_set", "path": "purpose", "value": "x"}).message
    assert "只能用这几种操作" in refused(spec, {"op": "add_node"}).message


def test_blueprint_issues():
    assert [i["code"] for i in blueprint_issues(empty_blueprint(), for_="trial")] == ["bp.missing"]
    spec = board()
    codes = {(i["code"], i["path"]) for i in blueprint_issues(spec, for_="trial")}
    assert ("bp.missing", "/purpose") in codes and ("bp.missing", "/data") in codes
    spec = apply_bp_op(spec, {"op": "bp_set", "path": "/purpose", "value": "看销售"}, source="assistant")
    spec = apply_bp_op(spec, {"op": "bp_add", "path": "/data", "value": {"id": "gmv", "label": "销售额"}},
                       source="assistant")
    trial = blueprint_issues(spec, for_="trial")
    assert [(i["code"], i["path"]) for i in trial] == [("bp.data_unmapped", "/data/0/table")]
    # 发布：助手和模板提的、还没确认的都要确认
    publish = {(i["code"], i["path"]) for i in blueprint_issues(spec, for_="publish")}
    assert {("bp.unconfirmed", "/purpose"), ("bp.unconfirmed", "/cadence"), ("bp.unconfirmed", "/data/0/label")} <= publish
    # 模板用不到的卡片：照实说不会生效
    spec = apply_bp_op(spec, {"op": "bp_add", "path": "/people", "value": {"id": "s1", "label": "审批"}}, source="user")
    assert ("bp.template_mismatch", "/people") in {(i["code"], i["path"]) for i in blueprint_issues(spec, for_="trial")}
    # 审批：处理人没定，试运行只提醒（派给试运行的人自己），发布要挡
    approval = apply_bp_op(empty_blueprint(), {"op": "bp_template", "template": {"id": "approval", "version": 1}},
                           source="user")
    trial = {i["code"]: i["level"] for i in blueprint_issues(approval, for_="trial") if i["code"] == "bp.people_unbound"}
    publish = {i["code"]: i["level"] for i in blueprint_issues(approval, for_="publish") if i["code"] == "bp.people_unbound"}
    assert trial == {"bp.people_unbound": "warning"} and publish == {"bp.people_unbound": "error"}
    # 个人应用：模板不支持、或者有审批步骤的都不行
    personal = {i["code"] for i in blueprint_issues(approval, for_="personal")}
    assert "bp.template_mismatch" in personal
