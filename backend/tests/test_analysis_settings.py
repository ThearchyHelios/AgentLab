"""分析设置（设置 → 偏好设置 → 分析，docs/design/analysis-round/01-算子与判定.md §10）。

- 设置接口的 analysis 一组：键、缺省、范围、叫法全取 app/analysis/contract.py；GET 按生效的样子给（存坏了的项按缺省），
  PUT 先查后写（不认识的键、类型、范围、两两之间的大小）
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.analysis import contract
from app.api.settings import ANALYSIS_GROUP, analysis_problem, analysis_values
from app.db.base import SessionLocal
from app.db.models import Setting
from app.engine.runner import run_manager
from app.main import app

@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def clean_group():
    yield
    async with SessionLocal() as session:
        row = await session.get(Setting, ANALYSIS_GROUP)
        if row is not None:
            await session.delete(row)
            await session.commit()


# --------------------------------------------------------------------------
# 设置接口
# --------------------------------------------------------------------------


def test_the_group_is_the_contract():
    assert ANALYSIS_GROUP == contract.SETTINGS_KEY
    assert analysis_values(None) == contract.DEFAULTS
    # 存坏了的项按缺省；两两之间的大小对不上的那一对都按缺省
    broken = {"main_share": "一半", "max_calls": 3.5, "k_warn": 4, "k_alert": 3.5, "min_history": 20, "max_history": 10,
              "table_top_k": 5, "trend_alpha": True}
    assert analysis_values(broken) == {**contract.DEFAULTS, "table_top_k": 5}
    # 浮点的项存成浮点：发起时的快照、结论里记的阈值一个写法
    assert analysis_values({"k_warn": 3, "k_alert": 4})["k_warn"] == 3.0
    assert isinstance(analysis_values({"k_warn": 3, "k_alert": 4})["k_alert"], float)


@pytest.mark.parametrize("value, why", [
    ("宽松", "需要是一个对象"),
    ({"bogus": 1}, "不支持「bogus」"),
    ({"main_share": 0.95}, f"{contract.LABELS['main_share']}要在 0.3 到 0.9 之间"),
    ({"max_calls": 2.5}, f"{contract.LABELS['max_calls']}要填整数"),
    ({"k_warn": 3.5}, f"{contract.LABELS['k_alert']}要大于{contract.LABELS['k_warn']}"),
    ({"min_history": 60}, f"{contract.LABELS['max_history']}不能小于{contract.LABELS['min_history']}"),
])
def test_problems_are_said_with_labels(value, why):
    assert why in (analysis_problem(value) or "")


async def test_get_put_round_trip(client):
    got = (await client.get("/api/settings")).json()
    assert got[ANALYSIS_GROUP] == contract.DEFAULTS
    put = await client.put("/api/settings", json={"values": {ANALYSIS_GROUP: {"main_share": 0.6, "max_calls": 5}}})
    assert put.status_code == 200, put.text
    got = (await client.get("/api/settings")).json()[ANALYSIS_GROUP]
    assert got == {**contract.DEFAULTS, "main_share": 0.6, "max_calls": 5}
    # 整组替换：这次没写的项回到缺省
    await client.put("/api/settings", json={"values": {ANALYSIS_GROUP: {"k_warn": 2.5}}})
    assert (await client.get("/api/settings")).json()[ANALYSIS_GROUP] == {**contract.DEFAULTS, "k_warn": 2.5}


async def test_put_refuses_bad_values_and_stores_nothing(client):
    r = await client.put("/api/settings", json={"values": {ANALYSIS_GROUP: {"main_share": 0.6, "k_alert": 1.5}}})
    assert r.status_code == 422 and contract.LABELS["k_alert"] in r.json()["detail"]
    assert (await client.get("/api/settings")).json()[ANALYSIS_GROUP] == contract.DEFAULTS
