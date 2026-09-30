"""结论句裁判的设置与画布校验。

- 设置里单开一组 judge：证据裁判模型、每份报告的句数 / 金额 / 时长上限、探索运行每次点击的金额上限、
  全局每日金额上限。每一项都能存成 null，表示不限——存了 null 就是不限，不会被默认值悄悄盖回去
- limits 组是环境变量定的、只读，裁判设置不放进去；每日计数存在设置表里，但不从设置接口进出
- 裁判模型的优先级：节点上的 judge.model > 设置里的证据裁判模型 > Copilot 模型 > 默认 provider
- 画布校验放开 claims: judge，并校验 judge 子配置的形状：上限可以写 null（不限），rewrite_once 是布尔值，
  on_unsupported 取 degrade / withhold
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.base import SessionLocal
from app.db.models import Setting
from app.engine.judge import (
    JUDGE_DEFAULTS,
    SETTING_GROUP,
    SPEND_KEY,
    Budget,
    click_budget,
    judge_model_spec,
    judge_settings,
    report_budget,
)
from app.engine.schema import CLAIMS_VALUES, JUDGE_KEYS, GraphSpec, claims_problem, validate_graph
from app.main import app


@pytest.fixture(autouse=True)
async def clean():
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_([SPEND_KEY, SETTING_GROUP, "copilot"])))
        await session.commit()
    yield


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------


def test_the_defaults_are_the_agreed_ones():
    assert SETTING_GROUP == "judge" and SETTING_GROUP != "limits"
    assert JUDGE_DEFAULTS == {
        "provider": None, "model": None,
        "report_max_claims": 40, "report_max_cost_usd": 0.05, "report_timeout_s": 30,
        "click_max_cost_usd": 0.01, "daily_max_usd": 2.0,
    }


async def test_settings_expose_the_judge_group_with_defaults():
    async with client() as c:
        got = (await c.get("/api/settings")).json()
    assert got["judge"] == JUDGE_DEFAULTS
    assert SPEND_KEY not in got


async def test_null_is_stored_as_unlimited_and_stays_unlimited():
    async with client() as c:
        r = await c.put("/api/settings", json={"values": {"judge": {
            **JUDGE_DEFAULTS, "model": "judge-model", "report_max_cost_usd": None, "daily_max_usd": None,
            "report_timeout_s": None, "report_max_claims": None, "click_max_cost_usd": None}}})
        assert r.status_code == 200, r.text
        got = (await c.get("/api/settings")).json()["judge"]
    assert got["model"] == "judge-model"
    assert all(got[k] is None for k in ("report_max_cost_usd", "daily_max_usd", "report_timeout_s",
                                         "report_max_claims", "click_max_cost_usd"))
    async with SessionLocal() as session:
        settings = await judge_settings(session)
    assert report_budget(settings, None) == Budget(max_claims=None, max_cost_usd=None, timeout_s=None,
                                                   daily_max_usd=None)
    assert click_budget(settings).max_cost_usd is None


async def test_a_partial_group_keeps_the_other_defaults():
    async with client() as c:
        await c.put("/api/settings", json={"values": {"judge": {"daily_max_usd": 5}}})
        got = (await c.get("/api/settings")).json()["judge"]
    assert got == {**JUDGE_DEFAULTS, "daily_max_usd": 5}


@pytest.mark.parametrize("key,value", [
    ("daily_max_usd", -1), ("report_max_cost_usd", 0), ("report_max_cost_usd", "0.05"),
    ("report_max_claims", 2.5), ("report_max_claims", True), ("report_timeout_s", 0),
    ("click_max_cost_usd", float("nan")), ("model", 3), ("unknown_key", 1),
])
async def test_bad_values_are_refused_with_a_reason(key, value):
    async with client() as c:
        r = await c.put("/api/settings", json={"values": {"judge": {key: value}}}) if value == value else \
            await c.put("/api/settings", content='{"values": {"judge": {"%s": NaN}}}' % key,
                        headers={"content-type": "application/json"})
        assert r.status_code == 422, r.text
        # 报错写设置页上的叫法（「每日金额上限（美元）」），不写键名；认不出的键原样点名
        from app.engine.judge import SETTING_LABEL

        assert SETTING_LABEL.get(key, key) in r.json()["detail"]
        assert (await c.get("/api/settings")).json()["judge"] == JUDGE_DEFAULTS, "整组都没写进去"


async def test_the_daily_counter_does_not_travel_through_the_settings_api():
    async with SessionLocal() as session:
        session.add(Setting(key=SPEND_KEY, value={"date": "2026-09-29", "usd": 1.5, "calls": 3}))
        await session.commit()
    async with client() as c:
        assert SPEND_KEY not in (await c.get("/api/settings")).json()
        await c.put("/api/settings", json={"values": {SPEND_KEY: {"date": "2026-09-29", "usd": 0, "calls": 0}}})
    async with SessionLocal() as session:
        row = await session.get(Setting, SPEND_KEY)
    assert row.value["usd"] == 1.5, "设置页整组回写不能把今天的花费清零"


async def test_todays_spend_is_readable_next_to_the_daily_cap(monkeypatch):
    from app.engine import judge as judge_mod

    monkeypatch.setattr(judge_mod, "_today", lambda: "2026-09-29")
    async with SessionLocal() as session:
        session.add(Setting(key=SPEND_KEY, value={"date": "2026-09-28", "usd": 1.9, "calls": 7}))
        await session.commit()
    async with client() as c:
        got = (await c.get("/api/settings/judge/spend")).json()
        assert got == {"date": "2026-09-29", "usd": 0.0, "calls": 0, "unpriced_calls": 0, "daily_max_usd": 2.0}, \
            "昨天的花费不算今天的"
        async with SessionLocal() as session:
            row = await session.get(Setting, SPEND_KEY)
            row.value = {"date": "2026-09-29", "usd": 0.42, "calls": 3, "unpriced_calls": 1}
            await session.commit()
        await c.put("/api/settings", json={"values": {"judge": {"daily_max_usd": None}}})
        got = (await c.get("/api/settings/judge/spend")).json()
    assert got == {"date": "2026-09-29", "usd": 0.42, "calls": 3, "unpriced_calls": 1, "daily_max_usd": None}


async def test_bad_rows_already_in_the_database_fall_back_to_defaults():
    """库里存坏了的值（旧版本、手改）读出来按默认，不让裁判拿着一个负数上限跑。"""
    async with SessionLocal() as session:
        session.add(Setting(key=SETTING_GROUP, value={"daily_max_usd": -3, "report_max_claims": "many",
                                                      "report_timeout_s": None}))
        await session.commit()
        settings = await judge_settings(session)
    assert settings["daily_max_usd"] == JUDGE_DEFAULTS["daily_max_usd"]
    assert settings["report_max_claims"] == JUDGE_DEFAULTS["report_max_claims"]
    assert settings["report_timeout_s"] is None


def test_the_node_overrides_key_by_key():
    settings = {**JUDGE_DEFAULTS, "report_max_claims": 10}
    assert report_budget(settings, None) == Budget(max_claims=10, max_cost_usd=0.05, timeout_s=30, daily_max_usd=2.0)
    assert report_budget(settings, {"max_cost_usd": None, "timeout_s": 5}) == \
        Budget(max_claims=10, max_cost_usd=None, timeout_s=5, daily_max_usd=2.0)
    # 探索运行每次点击：金额按点击的上限，句数、时长按每份报告的
    assert click_budget(settings) == Budget(max_claims=10, max_cost_usd=0.01, timeout_s=30, daily_max_usd=2.0)


# --------------------------------------------------------------------------
# 裁判模型的优先级
# --------------------------------------------------------------------------


async def test_the_judge_model_priority():
    async with SessionLocal() as session:
        spec = await judge_model_spec(session, None)
        assert (spec.provider, spec.model) == (None, None), "什么都没配：默认 provider"
        assert spec.thinking == "off" and spec.max_tokens == 4096

        session.add(Setting(key="copilot", value={"provider": "", "model": "copilot-model"}))
        await session.commit()
        assert (await judge_model_spec(session, None)).model == "copilot-model"

        session.add(Setting(key=SETTING_GROUP, value={"provider": "house", "model": "judge-model"}))
        await session.commit()
        spec = await judge_model_spec(session, None)
        assert (spec.provider, spec.model) == ("house", "judge-model")

        spec = await judge_model_spec(session, {"model": "node-model"})
        assert (spec.provider, spec.model) == (None, "node-model"), "节点上的写法整组生效，不和设置里的拼"
        spec = await judge_model_spec(session, {"model": None, "provider": None, "max_claims": 3})
        assert spec.model == "judge-model"


# --------------------------------------------------------------------------
# 画布校验
# --------------------------------------------------------------------------


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def graph(report: dict, defaults: dict | None = None) -> dict:
    nodes = [node("start", "input"),
             node("caliber", "metrics", caliber="周报口径", metrics=[{"id": "gmv", "expression": "1"}]),
             node("write", "report", instructions="写周报", **report),
             node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}])]
    ids = [n["id"] for n in nodes]
    g = {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in zip(ids, ids[1:])]}
    if defaults:
        g["defaults"] = defaults
    return g


def issues(g, code=None):
    return [i for i in validate_graph(GraphSpec.model_validate(g)).issues
            if i.code in ((code,) if code else ("report.claims_invalid", "report.judge_invalid"))]


def test_judge_is_a_supported_policy_now():
    assert CLAIMS_VALUES == ("off", "require_citation", "judge")
    assert claims_problem("judge") is None
    assert issues(graph({"claims": "judge"})) == []
    assert issues(graph({}, defaults={"claims": "judge"})) == []


def test_every_limit_can_be_unlimited():
    judge = {"provider": None, "model": None, "max_claims": None, "max_cost_usd": None, "timeout_s": None,
             "rewrite_once": True, "on_unsupported": "withhold"}
    assert set(judge) == set(JUDGE_KEYS)
    assert issues(graph({"claims": "judge", "judge": judge})) == []
    assert issues(graph({"claims": "judge", "judge": {"max_claims": 40, "max_cost_usd": 0.05, "timeout_s": 30,
                                                       "model": "judge-model", "on_unsupported": "degrade"}})) == []


@pytest.mark.parametrize("judge,field,words", [
    ("fast", "judge", "对象"),
    ({"max_cost": 1}, "judge.max_cost", "「金额上限（美元）」"),
    ({"max_claims": 0}, "judge.max_claims", "正整数"),
    ({"max_claims": 2.5}, "judge.max_claims", "正整数"),
    ({"max_claims": True}, "judge.max_claims", "正整数"),
    ({"max_cost_usd": -1}, "judge.max_cost_usd", "大于 0"),
    ({"max_cost_usd": "0.05"}, "judge.max_cost_usd", "大于 0"),
    ({"timeout_s": 0}, "judge.timeout_s", "大于 0"),
    ({"rewrite_once": "yes"}, "judge.rewrite_once", "开启或关闭"),
    ({"on_unsupported": "ignore"}, "judge.on_unsupported", "「出具降档」或「不予出具」"),
    ({"model": 3}, "judge.model", "模型 ID"),
])
def test_a_malformed_judge_block_is_an_error(judge, field, words):
    [issue] = issues(graph({"claims": "judge", "judge": judge}), "report.judge_invalid")
    assert issue.level == "error" and issue.node_id == "write" and issue.field == field
    assert words in issue.message, issue.message
    if field in ("judge.max_claims", "judge.max_cost_usd", "judge.timeout_s", "judge.model"):
        assert ("留空" if field == "judge.model" else "不限") in issue.message, "上限、模型都说清不填是什么意思"


def test_graph_defaults_are_checked_like_at_run_time():
    [issue] = issues(graph({"claims": "judge"}, defaults={"judge": {"max_claims": -1}}), "report.judge_invalid")
    assert issue.field == "judge.max_claims"


def test_the_judge_block_only_matters_when_claims_is_judge():
    assert issues(graph({"claims": "require_citation", "judge": {"max_claims": -1}})) == []


def test_judging_with_the_writing_model_is_a_warning():
    [warn] = [i for i in validate_graph(GraphSpec.model_validate(graph(
        {"claims": "judge", "model": "writer-x", "judge": {"model": "writer-x"}}))).issues
        if i.code == "report.judge_same_model"]
    assert warn.level == "warning" and warn.field == "judge.model" and "裁判模型与写作模型相同" in warn.message
    assert not [i for i in validate_graph(GraphSpec.model_validate(graph(
        {"claims": "judge", "model": "writer-x", "judge": {"model": "judge-y"}}))).issues
        if i.code == "report.judge_same_model"]
