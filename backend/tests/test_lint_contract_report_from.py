"""发布门禁对引用模式契约（report_from）的判断。

以前 `_lint_contract` 不管有没有 report_from，一律要求 narrative：模板⑨这种只用
report_from 核对报告文档的契约，数字回指由报告撰写节点的文档保证、根本用不上
narrative，却因此发不成受管版本。有 report_from 就不再要 narrative；report_from
指错（不是报告撰写节点、不在出口上游）照样挡住发布。
"""
from __future__ import annotations

import copy

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.governance import lint_for_publish
from app.engine.schema import GraphSpec
from app.main import app
from app.seed import TEMPLATES


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def traceable() -> dict:
    """模板⑨（可追溯周报）的图：契约只写 report_from，没有 narrative。"""
    tpl = next(t for t in TEMPLATES if t["name"].startswith("⑨"))
    return copy.deepcopy({"nodes": tpl["nodes"], "edges": tpl["edges"]})


def contract_of(graph: dict) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == "done")["data"]["config"]["contract"]


def lint(graph: dict, level: str = "governed") -> list:
    return lint_for_publish(GraphSpec.model_validate(graph), level=level).issues


async def publish(client, graph: dict, level: str = "governed") -> dict:
    wf = (await client.post("/api/workflows", json={"name": "可追溯周报-门禁", "graph": graph})).json()
    body = (await client.post(f"/api/workflows/{wf['id']}/publish", json={"level": level})).json()
    await client.delete(f"/api/workflows/{wf['id']}")
    return body


def test_the_template_contract_has_no_narrative_and_needs_none():
    graph = traceable()
    assert "narrative" not in contract_of(graph) and contract_of(graph)["report_from"] == "write"
    errors = [i for i in lint(graph) if i.level == "error"]
    assert errors == [], [i.message for i in errors]


async def test_the_template_publishes_as_governed(client):
    body = await publish(client, traceable())
    assert body["ok"] is True, body["issues"]
    assert all(i["level"] == "warning" for i in body["issues"])


@pytest.mark.parametrize("target, why", [("fetch", "不是「报告撰写」"), ("ghost", "不存在")])
async def test_a_report_from_pointing_elsewhere_still_blocks(client, target, why):
    graph = traceable()
    contract_of(graph)["report_from"] = target
    body = await publish(client, graph)
    assert body["ok"] is False
    bad = [i for i in body["issues"] if i["level"] == "error"]
    assert [i["code"] for i in bad] == ["contract.report_from_invalid"], bad
    assert why in bad[0]["message"] and bad[0]["node_id"] == "done"


async def test_a_report_that_is_not_upstream_still_blocks(client):
    graph = traceable()
    # 报告撰写节点挪到出口后面：出口核对时那份报告还没写出来
    graph["edges"] = [e for e in graph["edges"] if e["target"] != "done"] + [
        {"source": "caliber", "target": "done"}, {"source": "done", "target": "write"}]
    body = await publish(client, graph)
    assert body["ok"] is False
    assert any(i["code"] == "contract.report_from_invalid" and "上游" in i["message"]
               for i in body["issues"] if i["level"] == "error"), body["issues"]


@pytest.mark.parametrize("level, expected", [("governed", "error"), ("published", "warning")])
def test_without_report_from_or_narrative_the_gate_still_asks(level, expected):
    graph = traceable()
    del contract_of(graph)["report_from"]
    [issue] = [i for i in lint(graph, level) if i.code == "contract.report_from_missing"]
    assert issue.level == expected and issue.node_id == "done"
    assert "report_from" in issue.message and "narrative" in issue.message


def test_an_empty_report_from_counts_as_missing():
    graph = traceable()
    contract_of(graph)["report_from"] = ""
    assert any(i.code == "contract.report_from_missing" and i.level == "error" for i in lint(graph))


def test_a_narrative_contract_is_unchanged():
    graph = traceable()
    contract = contract_of(graph)
    del contract["report_from"]
    contract["narrative"] = "{{ nodes.write.text }}"
    assert [i for i in lint(graph) if i.level == "error"] == []
