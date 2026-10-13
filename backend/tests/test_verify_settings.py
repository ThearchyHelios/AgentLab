"""核查设置（api/settings.py 的 verify 一组）：双盲复算、反方审稿、审批质询共用。

- 核查模型（接入、模型）：节点上写了用节点的，没写用设置里的，设置也没写就沿用证据裁判模型的取值链
  （裁判设置 > 助手模型 > 默认接入），哪一级写了就整组用哪一级，不跨级拼接入和模型
- 三项各有开关和上限：双盲复算 {启用, 允许的相对误差, 查询次数, 时长}、反方审稿 {启用, 句数, 查询次数, 时长}、
  审批质询 {启用, 查询次数, 时长}；另有全局每日金额上限。上限存 null 表示不限，不会被默认值悄悄盖回去
- 设置页写进来的值先查后写：有一项不对整个请求回 422，说明写设置页上的叫法，一项都不落库
- 核查的每日计数是引擎记的账（engine/spend.py），不从设置接口进出；有一个只读接口给设置页看今天花了多少
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api.settings import (
    VERIFY_DEFAULTS,
    VERIFY_GROUP,
    VERIFY_SETTING_LABEL,
    verify_model_spec,
    verify_settings,
)
from app.db.base import SessionLocal
from app.db.models import Provider, Setting
from app.engine import spend
from app.engine.judge import JUDGE_DEFAULTS, MAX_TOKENS
from app.main import app


@pytest.fixture(autouse=True)
async def clean():
    async with SessionLocal() as session:
        await session.execute(delete(Setting).where(Setting.key.in_(
            [VERIFY_GROUP, spend.SPEND_KEY, "judge", "copilot"])))
        await session.commit()
    yield


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------
# 默认值与读写
# --------------------------------------------------------------------------


def test_the_defaults_are_the_agreed_ones():
    assert VERIFY_GROUP == "verify"
    assert VERIFY_DEFAULTS == {
        "provider": None, "model": None,
        "dual_check": {"enabled": True, "tolerance": 1e-6, "max_queries": 6, "timeout_s": 120},
        "challenge": {"enabled": True, "max_claims": 5, "max_queries": 4, "timeout_s": 90},
        "inquiry": {"enabled": True, "max_queries": 6, "timeout_s": 120},
        "daily_max_usd": 2.0,
    }


async def test_settings_expose_the_verify_group_with_defaults():
    async with client() as c:
        got = (await c.get("/api/settings")).json()
    assert got["verify"] == VERIFY_DEFAULTS
    assert got["judge"] == JUDGE_DEFAULTS, "裁判一组不受影响"
    assert spend.SPEND_KEY not in got


@pytest.fixture
async def house():
    """设置里点名的接入要真的存在、启用着（保存时会查）：建一个演示类的，用完删掉。"""
    async with SessionLocal() as session:
        session.add(Provider(name="house", kind="mock", models=[{"id": "verify-model"}], default_model="verify-model",
                             enabled=True, extra={}))
        await session.commit()
    yield "house"
    async with SessionLocal() as session:
        await session.execute(delete(Provider).where(Provider.name == "house"))
        await session.commit()


async def test_a_full_group_round_trips_and_null_stays_unlimited(house):
    value = {
        "provider": "house", "model": "verify-model",
        "dual_check": {"enabled": False, "tolerance": 0, "max_queries": None, "timeout_s": None},
        "challenge": {"enabled": True, "max_claims": None, "max_queries": 2, "timeout_s": 30.5},
        "inquiry": {"enabled": False, "max_queries": 1, "timeout_s": None},
        "daily_max_usd": None,
    }
    async with client() as c:
        r = await c.put("/api/settings", json={"values": {"verify": value}})
        assert r.status_code == 200, r.text
        assert r.json()["verify"] == value
        assert (await c.get("/api/settings")).json()["verify"] == value
    async with SessionLocal() as session:
        assert await verify_settings(session) == value


async def test_a_partial_group_keeps_the_other_defaults_down_to_each_item():
    async with client() as c:
        r = await c.put("/api/settings", json={"values": {"verify": {"dual_check": {"enabled": False},
                                                                     "daily_max_usd": 5}}})
        assert r.status_code == 200, r.text
        got = (await c.get("/api/settings")).json()["verify"]
    assert got == {**VERIFY_DEFAULTS, "daily_max_usd": 5,
                   "dual_check": {**VERIFY_DEFAULTS["dual_check"], "enabled": False}}


@pytest.mark.parametrize("key,value,label", [
    ("daily_max_usd", -1, "daily_max_usd"),
    ("daily_max_usd", "2", "daily_max_usd"),
    ("model", 3, "model"),
    ("dual_check", "on", "dual_check"),
    ("dual_check", {"enabled": "yes"}, "dual_check.enabled"),
    ("dual_check", {"tolerance": -0.1}, "dual_check.tolerance"),
    ("dual_check", {"tolerance": "0.01"}, "dual_check.tolerance"),
    ("dual_check", {"tolerance": None}, "dual_check.tolerance"),
    ("dual_check", {"max_queries": 2.5}, "dual_check.max_queries"),
    ("dual_check", {"max_queries": 0}, "dual_check.max_queries"),
    ("challenge", {"max_claims": True}, "challenge.max_claims"),
    ("challenge", {"timeout_s": -3}, "challenge.timeout_s"),
    ("inquiry", {"timeout_s": 0}, "inquiry.timeout_s"),
    ("inquiry", {"max_claims": 3}, "inquiry.max_claims"),
    ("unknown_key", 1, "unknown_key"),
])
async def test_bad_values_are_refused_with_the_settings_page_wording(key, value, label):
    async with client() as c:
        r = await c.put("/api/settings", json={"values": {"verify": {key: value}, "ui": {"theme": "dark"}}})
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        # 报错写设置页上的叫法（「双盲复算 · 允许的相对误差」），不写键名；认不出的键原样点名
        assert VERIFY_SETTING_LABEL.get(label, label.rpartition(".")[2]) in detail, detail
        got = (await c.get("/api/settings")).json()
    assert got["verify"] == VERIFY_DEFAULTS and got["ui"]["theme"] != "dark", "整个请求都没写进去"


@pytest.mark.parametrize("raw", ["NaN", "Infinity"])
async def test_non_finite_numbers_are_refused(raw):
    async with client() as c:
        r = await c.put("/api/settings", content='{"values": {"verify": {"dual_check": {"tolerance": %s}}}}' % raw,
                        headers={"content-type": "application/json"})
        assert r.status_code == 422, r.text
        assert VERIFY_SETTING_LABEL["dual_check.tolerance"] in r.json()["detail"]


async def test_bad_rows_already_in_the_database_fall_back_to_defaults():
    """库里存坏了的值（旧版本、手改）读出来按默认，不让核查拿着负数上限、文字开关去查库。"""
    async with SessionLocal() as session:
        session.add(Setting(key=VERIFY_GROUP, value={
            "model": "  ", "daily_max_usd": -3,
            "dual_check": {"enabled": "yes", "tolerance": -1, "max_queries": None},
            "challenge": "broken", "inquiry": {"timeout_s": 15, "extra": 1}}))
        await session.commit()
        got = await verify_settings(session)
    assert got["model"] is None and got["daily_max_usd"] == VERIFY_DEFAULTS["daily_max_usd"]
    assert got["dual_check"] == {**VERIFY_DEFAULTS["dual_check"], "max_queries": None}
    assert got["challenge"] == VERIFY_DEFAULTS["challenge"]
    assert got["inquiry"] == {**VERIFY_DEFAULTS["inquiry"], "timeout_s": 15}


# --------------------------------------------------------------------------
# 每日计数
# --------------------------------------------------------------------------


async def test_the_daily_counter_does_not_travel_through_the_settings_api():
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": spend._today(), "usd": 1.5, "calls": 3}))
        await session.commit()
    async with client() as c:
        assert spend.SPEND_KEY not in (await c.get("/api/settings")).json()
        await c.put("/api/settings", json={"values": {spend.SPEND_KEY: {"date": spend._today(), "usd": 0}}})
    async with SessionLocal() as session:
        row = await session.get(Setting, spend.SPEND_KEY)
    assert row.value["usd"] == 1.5, "设置页整组回写不能把今天的核查花费清零"


async def test_todays_spend_is_readable_next_to_the_daily_cap(monkeypatch):
    monkeypatch.setattr(spend, "_today", lambda: "2026-10-02")
    async with SessionLocal() as session:
        session.add(Setting(key=spend.SPEND_KEY, value={"date": "2026-10-02", "usd": 0.42, "calls": 3,
                                                        "unpriced_calls": 1}))
        await session.commit()
    async with client() as c:
        got = (await c.get("/api/settings/verify/spend")).json()
        assert got == {"date": "2026-10-02", "usd": 0.42, "calls": 3, "unpriced_calls": 1, "daily_max_usd": 2.0}
        await c.put("/api/settings", json={"values": {"verify": {"daily_max_usd": None}}})
        assert (await c.get("/api/settings/verify/spend")).json()["daily_max_usd"] is None


# --------------------------------------------------------------------------
# 核查模型的取值链
# --------------------------------------------------------------------------


async def test_the_verify_model_falls_back_along_the_judge_chain():
    async with SessionLocal() as session:
        spec = await verify_model_spec(session)
        assert (spec.provider, spec.model) == (None, None), "什么都没配：默认接入"
        assert spec.thinking == "off" and spec.max_tokens == MAX_TOKENS

        session.add(Setting(key="copilot", value={"provider": "", "model": "copilot-model"}))
        await session.commit()
        assert (await verify_model_spec(session)).model == "copilot-model", "沿用裁判的取值链：助手模型"

        session.add(Setting(key="judge", value={"provider": "house", "model": "judge-model"}))
        await session.commit()
        spec = await verify_model_spec(session)
        assert (spec.provider, spec.model) == ("house", "judge-model"), "沿用裁判的取值链：证据裁判模型"

        session.add(Setting(key=VERIFY_GROUP, value={"model": "verify-model"}))
        await session.commit()
        spec = await verify_model_spec(session)
        assert (spec.provider, spec.model) == (None, "verify-model"), "核查设置写了模型就整组用它，不和裁判的接入拼"
        assert spec.thinking == "off" and spec.max_tokens == MAX_TOKENS

        spec = await verify_model_spec(session, {"provider": "other", "model": "node-model", "tolerance": 0.1})
        assert (spec.provider, spec.model) == ("other", "node-model"), "节点上写了就按节点"
        spec = await verify_model_spec(session, {"provider": " ", "model": None})
        assert spec.model == "verify-model", "节点上留空等于没写"
