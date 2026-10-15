"""蓝图和关节的生成落地（docs/design/normal-user-apps.md N2、N3、§3.4）、蓝图的存与留痕、查看口径（look caliber）。

- TemplateApplier 只收 fill_joint，只收这一组里模板声明过的生成关节；改别的位置、别组的关节、用别的操作一律拒绝并照实
  交回（自查时交回模型一次、交付时列给人看）；
- TemplateChecker：没填的、指标式不合规矩（只许 cell() 取格加算术、行号 0、列要在 SQL 里、追得到原始数据）、SQL 过不了
  只读检查；
- BlueprintApplier：助手的操作来源记 assistant，越界的拒；复用口径卡按读好的口径核对并补上它读的列；
- apply_ops_and_save：每个操作写一条留痕；卡片改了，那一组生成关节标回「待填」；助手这一轮的操作和人改过的卡片冲突时
  照实交回；
- look caliber：只列最新的已发布且未退役版本里的源头口径卡，给出复用写法；读别的东西的卡照实说不能复用；
  verify_reuse 按库里那一版核对。
"""
from __future__ import annotations

import copy
import uuid

import pytest

from app.blueprints.generation import BlueprintApplier, BlueprintChecker, TemplateApplier, TemplateChecker
from app.blueprints.instantiate import generated_groups, instantiate
from app.blueprints.templates import _common as c
from app.blueprints.templates import get_template
from app.db.base import SessionLocal
import app.db.models  # noqa: F401 - 建表前要先登记全部模型（conftest 按 Base.metadata 建）


def _board():
    extras = get_template("periodic_board")
    inst = instantiate(extras.example())
    group = generated_groups(inst)["fetch_shop"]
    return extras, inst, group


def test_template_applier_refuses_anything_but_declared_joints():
    extras, inst, group = _board()
    graph = copy.deepcopy(inst.graph)
    applier = TemplateApplier(graph, inst.joint_defs, group)
    ok = applier.apply({"op": "fill_joint", "joint": "metric_gmv", "value": "cell(nodes.fetch_shop, 0, 'gmv')"})
    assert (ok.changed, ok.forward) == (True, True)
    assert {n["id"]: n for n in graph["nodes"]}["caliber"]["data"]["config"]["metrics"][0]["expression"] \
        == "cell(nodes.fetch_shop, 0, 'gmv')"
    before = copy.deepcopy(graph)
    for op, words in (
        ({"op": "fill_joint", "joint": "done.contract", "value": "{}"}, "按卡片确定地填"),
        ({"op": "fill_joint", "joint": "nope", "value": "x"}, "没有关节"),
        ({"op": "fill_joint", "joint": "sql_fetch_shop", "value": "  "}, "非空"),
        ({"op": "update_node", "id": "done", "config": {"contract": {}}}, "锁住"),
        ({"op": "add_node", "node": {"id": "x", "type": "llm"}}, "锁住"),
        ({"op": "bp_set", "path": "/purpose", "value": "x"}, "fill_joint"),
    ):
        out = applier.apply(op)
        assert (out.changed, out.forward) == (False, False) and words in out.problem, op
    assert graph == before, "被拒的一处都没落地"
    assert [r["joint"] for r in applier.refusals()] == ["done.contract", "nope", "sql_fetch_shop", None, None, None]
    assert all(r["code"] == "tpl.joint_refused" for r in applier.refusals())

    # 别的组的关节也不收
    spec = extras.example()
    spec["data"].append({**copy.deepcopy(spec["data"][0]), "id": "visits", "table": c.template_item(
        {"source": "crm", "table": "visits"})})
    two = instantiate(spec)
    groups = generated_groups(two)
    applier = TemplateApplier(copy.deepcopy(two.graph), two.joint_defs, groups["fetch_shop"])
    assert "不在这一组里" in applier.apply({"op": "fill_joint", "joint": "sql_fetch_crm", "value": "SELECT 1"}).problem


async def test_template_checker_reports_unfilled_expressions_and_sql():
    extras, inst, group = _board()
    applier = TemplateApplier(copy.deepcopy(inst.graph), inst.joint_defs, group)
    checker = TemplateChecker(applier, skeleton=inst.skeleton)
    applier.apply({"op": "fill_joint", "joint": "nope", "value": "x"})
    first = await checker.issues()
    assert {i["code"] for i in first} == {"tpl.joint_refused", "tpl.joint_unfilled"}
    assert {i["joint"] for i in first if i["code"] == "tpl.joint_unfilled"} == {j.id for j in group}
    assert "tpl.joint_refused" not in {i["code"] for i in await checker.issues()}, "越界的只交回一次"

    fill = extras.example_fill()
    for jid, value in fill.items():
        applier.apply({"op": "fill_joint", "joint": jid, "value": value})
    assert await checker.issues() == []

    for jid, value, code, words in (
        ("metric_gmv", "len(nodes.fetch_shop.rows)", "tpl.metric_expr", "不能用 len"),
        ("metric_gmv", "cell(nodes.caliber, 0, 'gmv')", "tpl.metric_expr", "这一组取数"),
        ("metric_gmv", "cell(nodes.fetch_shop, 1, 'gmv')", "tpl.metric_expr", "行号写 0"),
        ("metric_gmv", "cell(nodes.fetch_shop, 0, 'revenue')", "tpl.metric_column", "找不到"),
        ("metric_gmv", "input.period", "tpl.metric_expr", "cell("),
        ("metric_gmv", "cell(nodes.fetch_shop, 0, 'gmv') +", "tpl.metric_expr", "写得不对"),
        ("sql_fetch_shop", "DELETE FROM orders", "tpl.sql_rejected", "只读"),
    ):
        fresh = TemplateApplier(copy.deepcopy(inst.graph), inst.joint_defs, group)
        for k, v in {**fill, jid: value}.items():
            fresh.apply({"op": "fill_joint", "joint": k, "value": v})
        issues = await TemplateChecker(fresh, skeleton=inst.skeleton).issues()
        assert any(i["code"] == code and words in i["message"] for i in issues), (value, issues)
    assert "fill_joint" in checker.repair_request(first, "看过的表")


def test_blueprint_applier_records_assistant_proposals_and_refusals():
    from app.blueprints.spec import empty_blueprint

    applier = BlueprintApplier(empty_blueprint())
    assert applier.apply({"op": "bp_template", "template": {"id": "periodic_board", "version": 1}}).changed
    assert applier.apply({"op": "bp_set", "path": "/purpose", "value": "看销售"}).changed
    assert applier.spec["purpose"]["status"] == "proposed"
    out = applier.apply({"op": "bp_add", "path": "/access/use", "value": {"kind": "group", "name": "门店"}})
    assert (out.changed, out.forward) == (False, False) and "授权" in out.problem
    assert not applier.apply({"op": "add_node", "node": {}}).forward
    assert applier.apply({"op": "plan", "summary": "整理"}).forward
    assert applier.landed() == ["bp_template", "/purpose"]
    assert [r["path"] for r in applier.refusals()] == ["/access/use"]


async def test_blueprint_checker_sends_back_refusals_and_mismatches():
    from app.blueprints.spec import empty_blueprint

    applier = BlueprintApplier(empty_blueprint())
    checker = BlueprintChecker(applier)
    applier.apply({"op": "bp_template", "template": {"id": "periodic_board", "version": 1}})
    applier.apply({"op": "bp_add", "path": "/people", "value": {"id": "s1", "label": "审批"}})
    applier.apply({"op": "bp_set", "path": "/access/use", "value": []})
    issues = await checker.issues()
    assert {i["code"] for i in issues} == {"bp.refused", "bp.template_mismatch"}
    # 缺项不交回模型：缺的是只有用户知道的事
    assert "bp.missing" not in {i["code"] for i in issues}
    assert "bp_ask" in checker.repair_request(issues, "")


async def _blueprint(spec: dict, **extra):
    from app.db.models_blueprints import Blueprint

    async with SessionLocal() as session:
        row = Blueprint(name="测试蓝图", scope="shared", spec=spec, **extra)
        session.add(row)
        await session.commit()
        return row.id


async def test_apply_ops_and_save_writes_events_and_marks_stale_joints():
    from sqlalchemy import select

    from app.blueprints import store
    from app.blueprints.ops import BpRefused
    from app.db.models_blueprints import Blueprint, BlueprintEvent

    extras = get_template("periodic_board")
    spec = extras.example()
    inst = instantiate(spec, filled=extras.example_fill())
    bp_id = await _blueprint(spec, template_id="periodic_board", template_version=1, joints=inst.joints)
    async with SessionLocal() as session:
        row = await session.get(Blueprint, bp_id)
        saved = await store.apply_ops_and_save(session, row, [
            {"op": "bp_set", "path": "/purpose", "value": "每周一看上周"},
            {"op": "bp_set", "path": "/data/0/measure", "value": "sum(amount) - sum(refund)"},
        ], "zhang", True, "user")
        assert len(saved.applied) == 2 and saved.refused == []
        events = list((await session.execute(select(BlueprintEvent).where(BlueprintEvent.blueprint_id == bp_id)
                                             .order_by(BlueprintEvent.seq))).scalars())
        assert [(e.seq, e.op, e.actor, e.verified) for e in events] == [(1, "bp_set", "zhang", True),
                                                                        (2, "bp_set", "zhang", True)]
        assert events[1].data["before"]["value"] == "sum(amount)"
        assert events[1].data["after"]["value"] == "sum(amount) - sum(refund)"
        assert events[1].data["source"] == "user"
        # 「怎么算」改了：这一组的 SQL 和指标式标回「待填」
        assert row.joints["sql_fetch_shop"]["status"] == "pending"
        assert row.joints["done.contract"]["status"] == "filled"
        # 卡片上的修改一个被拒就整串不落
        with pytest.raises(BpRefused):
            await store.apply_ops_and_save(session, row, [
                {"op": "bp_set", "path": "/purpose", "value": "再改"},
                {"op": "bp_set", "path": "/people/0/policy", "value": "any"},
            ], "zhang", True, "user")
        await session.refresh(row)
        assert row.spec["purpose"]["value"] == "每周一看上周"
        # 助手这一轮的：冲突的照实交回，别的照常落
        saved = await store.apply_ops_and_save(session, row, [
            {"op": "bp_set", "path": "/data/0/label", "value": "成交额"},
            {"op": "bp_set", "path": "/data/9/label", "value": "不存在"},
        ], None, False, "assistant", partial=True)
        assert [o["path"] for o in saved.applied] == ["/data/0/label"]
        assert "超出了列表的范围" in saved.refused[0]["message"]
        out = store.blueprint_out(row)
        assert out["spec"]["data"][0]["label"] == {"value": "成交额", "source": "assistant", "status": "proposed",
                                                   "why": None}
        assert "value" not in out["joints"]["sql_fetch_shop"], "关节只给状态"
        codes = {i["code"] for i in out["issues"]}
        assert {"bp.unconfirmed", "tpl.joint_unfilled"} <= codes
        assert all(i.get("plain") for i in out["issues"] if i["code"].startswith(("bp.", "tpl.")))
        await session.delete(row)
        await session.commit()


# ---------------------------------------------------------------- 查看口径


async def _published_card(graph: dict) -> tuple[str, str]:
    from app.db.models import Workflow, WorkflowVersion

    async with SessionLocal() as session:
        wf = Workflow(name=f"周销售_{uuid.uuid4().hex[:6]}", graph=graph, status="published", published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph, graph_hash="h" * 64, level="published"))
        await session.commit()
        return wf.id, wf.name


def _card_graph(expression: str = "cell(nodes.fetch, 0, 'gmv') / cell(nodes.fetch, 0, 'order_cnt')") -> dict:
    return {"nodes": [
        c.node("start", "input", "入口"),
        c.node("fetch", "tool", "取数", tool="db_query__shop", args={"sql": "SELECT 1 AS gmv, 2 AS order_cnt"}),
        c.node("kpi", "metrics", "周报口径卡", caliber="周报口径", caliber_version="v2", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "expression": "cell(nodes.fetch, 0, 'gmv')"},
            {"id": "aov", "name": "客单价", "unit": "元", "expression": expression}]),
    ], "edges": [c.edge("start", "fetch"), c.edge("fetch", "kpi")]}


async def test_look_caliber_lists_published_cards_and_how_to_reuse():
    from app.db.models import Workflow
    from app.look.base import LookBudget
    from app.look.calibers import CaliberKind
    from app.look.service import LookService

    wf_id, wf_name = await _published_card(_card_graph())
    bad_id, bad_name = await _published_card(_card_graph("vars.x * 2"))
    try:
        async with SessionLocal() as session:
            kind = await CaliberKind.load(session)
        mine = [i for i in kind.items if i.workflow_id == wf_id]
        assert [(i.metric_id, i.name, i.version) for i in mine] == [("gmv", "销售额", 1), ("aov", "客单价", 1)]
        assert mine[1].reuse() == {"workflow_id": wf_id, "version": 1, "node_id": "kpi", "metric_id": "aov",
                                   "inputs": [{"node": "fetch", "tool": "db_query__shop", "columns": ["gmv", "order_cnt"]}]}
        assert any(wf_name in line for line in await kind.overview())
        looks = LookService([kind])
        result = await looks.look({"op": "look", "need": "客单价", "targets": [
            {"kind": "caliber", "source": wf_name, "name": "客单价"}, {"kind": "caliber", "name": "不存在的指标"}]},
            LookBudget())
        assert result.record["objects"][0]["kind"] == "caliber"
        assert result.record["objects"][0]["named"] == [f"{wf_name} v1 · 客单价"]
        assert "复用写法" in result.text and '"metric_id": "aov"' in result.text and "fetch（db_query__shop）" in result.text
        assert "没找到口径" in result.text
        # 读别的东西的卡：照实说不能复用
        bad = [i for i in kind.items if i.workflow_id == bad_id]
        assert bad and all(i.reuse() is None for i in bad)
        result = await looks.look({"op": "look", "need": "x", "targets": [{"kind": "caliber", "source": bad_name}]},
                                  LookBudget())
        assert "不能复用" in result.text
        # 蓝图助手按读好的口径核对复用、补上读的列
        reuse, reason = kind.reuse_for({"workflow_id": wf_id, "version": 1, "node_id": "kpi", "metric_id": "aov"})
        assert reason is None and reuse["inputs"][0]["columns"] == ["gmv", "order_cnt"]
        assert kind.reuse_for({"workflow_id": wf_id, "version": 7, "node_id": "kpi", "metric_id": "aov"})[0] is None
    finally:
        async with SessionLocal() as session:
            for wid in (wf_id, bad_id):
                await session.delete(await session.get(Workflow, wid))
            await session.commit()


async def test_saving_a_reuse_checks_the_published_version():
    from app.blueprints import store
    from app.blueprints.ops import BpRefused
    from app.blueprints.spec import empty_blueprint
    from app.db.models import Workflow, WorkflowVersion
    from app.db.models_blueprints import Blueprint

    wf_id, _ = await _published_card(_card_graph())
    spec = empty_blueprint()
    spec["data"] = [{"id": "aov", "label": c.template_item("客单价"), "unit": c.template_item(None),
                     "table": c.template_item(None), "measure": c.template_item(None), "date": c.template_item(None),
                     "filter": c.template_item(None), "grain": c.template_item(None), "reuse": None}]
    bp_id = await _blueprint(spec)
    try:
        async with SessionLocal() as session:
            row = await session.get(Blueprint, bp_id)
            ref = {"workflow_id": wf_id, "version": 1, "node_id": "kpi", "metric_id": "aov"}
            await store.apply_ops_and_save(session, row, [{"op": "bp_set", "path": "/data/0/reuse", "value": ref}],
                                           "zhang", True, "user")
            assert row.spec["data"][0]["reuse"]["inputs"] == [
                {"node": "fetch", "tool": "db_query__shop", "columns": ["gmv", "order_cnt"]}]
            # 退役了的版本不能复用
            version = (await session.execute(WorkflowVersion.__table__.select().where(
                WorkflowVersion.workflow_id == wf_id))).first()
            assert version is not None
            from sqlalchemy import update

            from app.db.base import utcnow

            await session.execute(update(WorkflowVersion).where(WorkflowVersion.workflow_id == wf_id)
                                  .values(retired_at=utcnow()))
            await session.commit()
            with pytest.raises(BpRefused) as e:
                await store.apply_ops_and_save(session, row, [{"op": "bp_set", "path": "/data/0/reuse", "value": ref}],
                                               "zhang", True, "user")
            assert e.value.code == "bp.data_unmapped"
            await session.delete(row)
            await session.commit()
    finally:
        async with SessionLocal() as session:
            await session.delete(await session.get(Workflow, wf_id))
            await session.commit()
