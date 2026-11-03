"""口径可调用描述（app/ledger/descriptor.py、GET /api/ledger/calibers）：目录页、应用表单、外部集成用的机器可读契约。

- 只列最新「已发布且未退役」版本里的源头口径卡：入口字段成 JSON Schema、指标照配置、依赖只报数据源和表名；
- 能不能组合按 look.calibers.card_inputs（和对账、use_caliber 同一个认法）；
- 出具只描述版本策略：受管级别、图里没有人工节点或写外部系统的节点才自动出具，原因照写；
- 转抄别处口径卡的卡不列；响应里没有 SQL、没有参数值。
数据是虚构店铺库。
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests import ledger_samples as S


@pytest.fixture(autouse=True)
async def shop(tmp_path):
    await S.fresh_source(tmp_path / "shop.db")
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def descriptors(client: AsyncClient, wf: str) -> list[dict]:
    body = (await client.get("/api/ledger/calibers", params={"workflow_id": wf})).json()
    assert set(body) == {"calibers"}, body
    return body["calibers"]


def with_human() -> dict:
    graph = S.caliber_graph()
    graph["nodes"].append(S.node("approve", "human", assignee="运营"))
    return graph


def with_copy() -> dict:
    graph = S.caliber_graph()
    graph["nodes"].append(S.node(
        "copy", "metrics", caliber="抄来的周销售", caliber_version="v1",
        caliber_from={"workflow_id": "elsewhere", "workflow_version": 1, "node_id": "card"},
        metrics=[{"id": "gmv", "name": "销售额", "expression": "cell(nodes.fetch, 0, 'gmv')"}]))
    return graph


async def test_a_source_caliber_is_described_as_a_callable_contract(client):
    wf = await S.publish()
    [made] = await descriptors(client, wf)
    assert (made["workflow_id"], made["workflow_name"], made["version"]) == (wf, "周销售", 1)
    assert made["level"] == "governed" and made["graph_hash"]
    assert made["card"] == {"node_id": "card", "title": "card", "caliber": "周销售", "caliber_version": "v1"}
    assert made["metrics"] == [{"id": "gmv", "name": "销售额", "unit": "元", "decimals": 2, "format": "thousands"}]
    schema = made["entry_schema"]
    assert schema["format"] == "agentlab.form/1" and schema["type"] == "object"
    assert schema["required"] == ["start", "end"]
    assert schema["properties"]["store"]["default"] == "all"
    assert made["composable"] is True and made["compose_reason"] is None
    assert made["dependencies"]["sources"] == [S.CURRENT["source"]]
    assert made["dependencies"]["tables"] == ["orders"]
    assert made["dependencies"]["catalog_versions"] is None and made["dependencies"]["schema_versions"] is None
    issuance = made["issuance"]
    assert issuance["level"] == "governed" and issuance["auto_issue"] is True
    assert issuance["requires_independent_proof"] is True
    assert issuance["side_effects"] is None and issuance["reasons"] == []

    # 单条接口交同一份描述；转抄卡、没发布过的版本、不存在的节点都是 404
    single = (await client.get(f"/api/ledger/calibers/{wf}/1/card")).json()
    assert single == made
    for path in (f"/api/ledger/calibers/{wf}/1/nope", f"/api/ledger/calibers/{wf}/9/card",
                 "/api/ledger/calibers/nope/1/card"):
        assert (await client.get(path)).status_code == 404, path

    # 契约里没有取数 SQL、没有参数值
    text = json.dumps((await client.get("/api/ledger/calibers", params={"workflow_id": wf})).json())
    assert "SELECT" not in text and '"sql"' not in text
    assert "SUM(" not in text and "paid" not in text


async def test_a_human_node_turns_auto_issue_off_with_a_reason(client):
    wf = await S.publish(name="审批周销售", graph=with_human(), level="governed")
    [made] = await descriptors(client, wf)
    assert made["level"] == "governed"
    assert made["issuance"]["auto_issue"] is False
    assert "approve" in made["issuance"]["side_effects"]
    assert any("人工节点" in r for r in made["issuance"]["reasons"])
    assert made["issuance"]["requires_independent_proof"] is True


async def test_a_published_level_version_only_supports_one_click_issuance(client):
    wf = await S.publish(name="已发布周销售", graph=S.caliber_graph(), level="published")
    [made] = await descriptors(client, wf)
    assert made["level"] == "published" and made["issuance"]["auto_issue"] is False
    assert any("已发布级别" in r and "一键出具" in r for r in made["issuance"]["reasons"])


async def test_a_card_that_copies_another_caliber_is_not_listed(client):
    wf = await S.publish(graph=with_copy())
    listed = await descriptors(client, wf)
    assert [d["card"]["node_id"] for d in listed] == ["card"]
    assert (await client.get(f"/api/ledger/calibers/{wf}/1/copy")).status_code == 404


async def test_metric_definition_and_params_are_described(client):
    graph = S.caliber_graph()
    metric = graph["nodes"][2]["data"]["config"]["metrics"][0]
    metric["definition"] = {"text": "已支付订单的销售额", "scope": "全部门店", "owner": "运营"}
    metric["params"] = [{"name": "store", "type": "string", "default": "all"}]
    wf = await S.publish(graph=graph)
    [made] = await descriptors(client, wf)
    [m] = made["metrics"]
    assert m["definition"]["text"] == "已支付订单的销售额" and m["definition"]["owner"] == "运营"
    assert m["params"] == [{"name": "store", "type": "string", "default": "all"}]


async def test_an_external_only_card_is_not_promised_automatic_issuance(client):
    graph = S.chain(
        S.node("start", "input"),
        S.node("card", "metrics", caliber="年度目标", caliber_version="v1", metrics=[{
            "id": "target", "name": "年度目标", "unit": "元", "kind": "external",
            "external": {"value": 100, "source": "预算", "as_of": "2026-01-01"}}]),
        S.node("out", "output", fields=[{"name": "v", "value": "{{ nodes.card.metrics[0].value }}"}]))
    wf = await S.publish(name="只有外部常量", graph=graph)
    [made] = await descriptors(client, wf)
    assert made["composable"] is True
    assert made["issuance"]["auto_issue"] is False
    assert any("没有取数节点" in r for r in made["issuance"]["reasons"])


async def test_metric_synonyms_reach_the_descriptor(client):
    """文档带来的同义词要出现在机器可读描述里：按名字找口径时，「月活」和指标名一样是入口。"""
    graph = S.caliber_graph()
    for node in graph["nodes"]:
        if node["type"] == "metrics":
            node["data"]["config"]["metrics"][0]["synonyms"] = ["月活", "MAU"]
    wf = await S.publish(graph=graph)
    [made] = await descriptors(client, wf)
    [metric] = made["metrics"]
    assert metric["synonyms"] == ["月活", "MAU"]
