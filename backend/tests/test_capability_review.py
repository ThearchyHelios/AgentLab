"""能力字段语义的审阅（D-i）：写进来的规矩、待审阅的队列、批量审阅、权限。

- 字段语义沿用数据目录「项」：人工新填、改过的记为人工填写、已确认；值没动的保持原状态；客户端冒充不了有确证的来源；
  明确标成推断（模型起草、命名推断）的新项按推断收下；取值不合规的报错用「返回字段」的说法；
- 队列只列推断的项，按用到它的运行次数、是不是写入能力、项的风险、来源排；来源删掉了的能力不列；
- 批量审阅逐个能力按 version 乐观锁、一次写入、version 加一、写审计；冲突、位置不对、能力不存在只记在那几条上；
- 搭建者能看队列，审阅只给管理员。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.data import capability_review as review
from app.data.capability_review import BatchItem, FieldsInvalid, normalize_fields
from app.db.base import SessionLocal
from app.db.models import AuditEvent, Capability, Run
from tests.fakes import auth_env


# ---------------------------------------------------------------- 写进来


def test_human_edits_are_confirmed_and_shorthand_is_accepted():
    out = normalize_fields({}, {"/amount": {"label": "金额", "unit": {"value": "元"}}}, at="t")
    assert out["/amount"]["label"] == {"value": "金额", "source": "human", "status": "confirmed", "updated_at": "t"}
    assert out["/amount"]["unit"]["status"] == "confirmed"


def test_unchanged_items_keep_their_state_and_sources_cannot_be_faked():
    existing = {"/amount": {"label": {"value": "金额", "source": "llm", "status": "proposed", "updated_at": "a"}}}
    kept = normalize_fields(existing, existing, at="t")
    assert kept == existing, "值没动、状态没改：原样留着"
    faked = normalize_fields({}, {"/id": {"label": {"value": "单号", "source": "fk", "status": "verified"}}}, at="t")
    assert faked["/id"]["label"]["source"] == "human" and faked["/id"]["label"]["status"] == "confirmed", \
        "客户端冒充不了有确证的来源"
    proposed = normalize_fields({}, {"/id": {"label": {"value": "单号", "source": "llm", "status": "proposed"}}},
                                at="t")
    assert proposed["/id"]["label"] == {"value": "单号", "source": "llm", "status": "proposed", "updated_at": "t"}
    confirmed = normalize_fields(existing, {"/amount": {"label": {**existing["/amount"]["label"],
                                                                  "status": "confirmed"}}}, at="t")
    assert confirmed["/amount"]["label"]["status"] == "confirmed", "等同单项审阅"
    assert normalize_fields(existing, {}, at="t") == {}, "提交里没有的项删掉"


@pytest.mark.parametrize("submitted, said", [
    ({"amount": {"label": "金额"}}, "以 / 开头"),
    ({"/amount": "金额"}, "按项分开的对象"),
    ({"/amount": {"measure": "很多"}}, "返回字段 /amount 的度量类型"),
    ({"/amount": {"colour": "红"}}, "返回字段 /amount 含有不认识的字段「colour」"),
    ({"/avg": {"meaning": "客单价=金额/人数"}}, "返回字段 /avg 的含义看起来是计算公式"),
    ([], "按 JSON 指针分项"),
])
def test_bad_fields_are_refused_in_capability_words(submitted, said):
    with pytest.raises(FieldsInvalid) as e:
        normalize_fields({}, submitted)
    assert said in str(e.value) and "列 " not in str(e.value) and "数据目录" not in str(e.value), str(e.value)


# ---------------------------------------------------------------- 队列与批量审阅


def _proposed(value, source="llm"):
    return {"value": value, "source": source, "status": "proposed"}


@pytest.fixture
async def caps():
    """一项写入能力（被 2 次运行用到）、一项只读能力（没用过）、一项来源已删掉的能力，各带几项推断。"""
    tag = uuid.uuid4().hex[:6]
    names = {"pay": f"erp_pay_{tag}", "orders": f"erp_orders_{tag}", "gone": f"gone_{tag}"}
    async with SessionLocal() as s:
        s.add_all([
            Capability(name=names["pay"], system="erp", title="付款", effect_system="write", fields={
                "/amount": {"label": _proposed("金额"), "measure": _proposed("flow", "name")},
                "/card": {"label": {"value": "卡号", "source": "human", "status": "confirmed"}}}),
            Capability(name=names["orders"], system="erp", title="订单", effect_system="read", fields={
                "/status": {"codes": _proposed({"1": "已付"}, "comment"), "label": _proposed("状态")}}),
            Capability(name=names["gone"], system="erp", effect_system="read", missing=True,
                       fields={"/x": {"label": _proposed("某字段")}}),
        ])
        runs = [Run(workflow_name="用过", options={"capabilities": {names["pay"]: {"version": 1}}}) for _ in range(2)]
        s.add_all(runs)
        await s.commit()
        run_ids = [r.id for r in runs]
    try:
        yield names
    finally:
        async with SessionLocal() as s:
            await s.execute(delete(Capability).where(Capability.name.in_(list(names.values()))))
            await s.execute(delete(Run).where(Run.id.in_(run_ids)))
            await s.execute(delete(AuditEvent).where(AuditEvent.subject_id.in_(list(names.values()))))
            await s.commit()


async def test_the_queue_ranks_by_runs_effect_risk_and_source(caps):
    async with SessionLocal() as s:
        counts = await review.run_counts(s)
        queue = await review.review_queue(s, limit=200)
    assert counts[caps["pay"]] == 2
    mine = [i for i in queue["items"] if i["capability"] in caps.values()]
    assert [(i["capability"], i["pointer"], i["field"]) for i in mine] == [
        (caps["pay"], "/amount", "measure"),      # 用过 2 次 × 写入 × 度量类型（影响能不能加总）
        (caps["pay"], "/amount", "label"),
        (caps["orders"], "/status", "codes"),
        (caps["orders"], "/status", "label"),
    ], "已确认的、来源已删掉的能力不列"
    top = mine[0]
    assert top["field_label"] == "返回字段 /amount 的度量类型" and top["display"] == "可累加"
    assert top["runs"] == 2 and top["effect"] == "write" and top["version"] == 1
    assert top["reasons"][0] == "被 2 次运行用到" and "写入能力" in top["reasons"][1] and top["reasons"][-1] == \
        "命名推断，未确认"
    async with SessionLocal() as s:
        facts = await review.review_queue(s, kind="facts", capability=caps["pay"])
        names = await review.review_queue(s, kind="names", capability=caps["orders"])
    assert [i["field"] for i in facts["items"]] == ["measure"] and facts["total"] == 1
    assert {i["field"] for i in names["items"]} == {"codes", "label"}


async def test_batch_review_locks_per_capability_and_audits(caps):
    pay, orders = caps["pay"], caps["orders"]
    items = [
        BatchItem(pay, "/amount", "label", "confirm", 1),
        BatchItem(pay, "/amount", "measure", "reject", 1),
        BatchItem(pay, "/nope", "label", "confirm", 1),
        BatchItem(orders, "/status", "label", "confirm", 7),            # 版本对不上
        BatchItem(caps["gone"], "/x", "label", "confirm", 1),
        BatchItem("no_such_capability", "/x", "label", "confirm", 1),
    ]
    async with SessionLocal() as s:
        results = await review.review_batch(s, items, actor="root", verified=True)
    assert [(r["ok"], r["code"]) for r in results] == [
        (True, None), (True, None), (False, review.CODE_PATH), (False, review.CODE_CONFLICT),
        (False, review.CODE_MISSING), (False, review.CODE_MISSING)]
    assert results[0]["version"] == 2 and "返回字段 /nope 的中文名" in results[2]["error"]
    async with SessionLocal() as s:
        row = await s.get(Capability, pay)
        audits = list((await s.execute(select(AuditEvent).where(AuditEvent.subject_id == pay))).scalars())
        untouched = await s.get(Capability, orders)
    assert row.version == 2 and row.updated_by == "root"
    assert row.fields["/amount"]["label"]["status"] == "confirmed"
    assert row.fields["/amount"]["measure"]["status"] == "rejected"
    assert untouched.version == 1 and untouched.fields["/status"]["label"]["status"] == "proposed"
    assert [a.action for a in audits] == ["capability.review"] and audits[0].data["version"] == 2
    assert {(c["field"], c["before"], c["after"]) for c in audits[0].data["items"]} == {
        ("label", "proposed", "confirmed"), ("measure", "proposed", "rejected")}

    # 撤销审阅回到来源的初始状态；人工填写的撤销就是删掉（同数据目录）
    async with SessionLocal() as s:
        again = await review.review_batch(s, [BatchItem(pay, "/amount", "label", "reset", 2),
                                              BatchItem(pay, "/card", "label", "reset", 2)], actor="root",
                                          verified=True)
        assert all(r["ok"] for r in again)
        row = await s.get(Capability, pay)
        await s.refresh(row)
    assert row.fields["/amount"]["label"]["status"] == "proposed" and "/card" not in row.fields


# ---------------------------------------------------------------- 接口与权限


async def test_review_api_and_permissions(caps):
    await auth_env.reset()
    try:
        async with auth_env.client() as c:
            queue = (await c.get("/api/capabilities/review-queue", params={"capability": caps["pay"]})).json()
            assert [i["field"] for i in queue["items"]] == ["measure", "label"]
            r = await c.put(f"/api/capabilities/{caps['orders']}/governance",
                            json={"version": 1, "fields": {"/status": {"label": "订单状态"}}})
            assert r.status_code == 200, r.text
            assert r.json()["fields"]["/status"]["label"]["status"] == "confirmed", "治理写进来的人工填写记为已确认"
            assert "codes" not in r.json()["fields"]["/status"], "提交里没有的项删掉"
            bad = await c.put(f"/api/capabilities/{caps['orders']}/governance",
                              json={"version": 2, "fields": {"/status": {"measure": "很多"}}})
            assert bad.status_code == 422 and "返回字段 /status 的度量类型" in bad.json()["detail"]

        admin = await auth_env.turn_on_local()
        await auth_env.make_user("bob", "builder")
        bob = await auth_env.login("bob")
        assert (await bob.get("/api/capabilities/review-queue")).status_code == 200, "搭建者能看"
        body = {"items": [{"capability": caps["pay"], "pointer": "/amount", "field": "label", "action": "confirm",
                           "if_version": 1}]}
        assert (await bob.post("/api/capabilities/review/batch", json=body)).status_code == 403, "审阅只给管理员"
        r = await admin.post("/api/capabilities/review/batch", json=body)
        assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
        async with SessionLocal() as s:
            audit = (await s.execute(select(AuditEvent).where(
                AuditEvent.subject_id == caps["pay"], AuditEvent.action == "capability.review"))).scalar_one()
        assert audit.actor == "root" and audit.verified is True
    finally:
        await auth_env.reset()
