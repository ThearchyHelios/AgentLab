"""出口契约（引用模式）按报告节点的 claims 和可疑实体判档。

- claims: require_citation：没挂依据的结论句计入缺口，按 decide_tier 的 claims_policy 降档（不是不予出具，
  strict 也一样）；数的是出口重新核对出来的（verify_doc 的 uncited），不信文档里记的 cites。off（默认）
  照旧不管。升级前发起的运行不管图里写没写，一律按 off
- 可疑实体（unknown_entity）：只在受管级别的正式运行里按「有缺口」降档，不拦截；探索运行、已发布级别
  只在 _issuance.entities 里标注。表结构快照不全时核对不了的名字（unverified_entity）在哪都只标注
- 契约自己写 claims: require_citation 的同样生效；写 judge 的仍是后续版本，照实记缺口
- 契约只能收紧、不能放松：报告节点要求了，契约写 ignore / off 照旧降档，写 withhold 收紧成不予出具；
  受管级别的正式运行里 ignore 一律当 degrade（没有哪道门禁查契约里的 claims）
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.data.engine import engines
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, RunEvent, Workflow, WorkflowVersion
from app.engine import runner as runner_mod
from app.engine.runner import run_manager
from app.main import app

SOURCE = "warehouse"
TOOL = f"db_query__{SOURCE}"
SQL = "SELECT region, SUM(amount) AS gmv, COUNT(*) AS order_cnt FROM orders GROUP BY region ORDER BY gmv DESC"
CLEAN = "东区销售额 [[m:gmv]]，来自 `orders`。[[see:m:gmv]]"
UNCITED = CLEAN + "增长主要来自新客。"
SUSPICIOUS = "东区销售额 [[m:gmv]]，来自 `orders`，`refund_log` 另算。[[see:m:gmv]]"


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _warehouse(tmp_path, *, truncated: bool = False):
    from app.data.introspect import introspect

    path = tmp_path / "warehouse.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL);"
        "CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL);")
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, "east", 100.5), (2, "west", 200.0), (3, "east", 300.0)])
    db.commit()
    db.close()
    probe = SimpleNamespace(id=f"probe-{path.name}", name=SOURCE, kind="sqlite", database=str(path), host=None,
                            port=None, username=None, password=None, options={}, readonly=True, schema_cache={})
    cache = await introspect(probe)
    if truncated:
        # 库里的表太多、探查只存了一部分：快照不全，找不到的名字只能说核对不了
        cache.update(truncated=True, total=500)
    await engines.invalidate(probe.id)
    async with SessionLocal() as session:
        row = (await session.execute(select(DataSource).where(DataSource.name == SOURCE))).scalar_one_or_none()
        if row is None:
            row = DataSource(name=SOURCE, kind="sqlite", database=str(path), readonly=True, enabled=True)
            session.add(row)
        row.database, row.kind, row.enabled, row.readonly, row.schema_cache = str(path), "sqlite", True, True, cache
        row.options = {}
        await session.commit()
        source_id = row.id
    await engines.invalidate(source_id)
    return source_id


@pytest.fixture
async def warehouse(tmp_path):
    source_id = await _warehouse(tmp_path)
    yield source_id
    await engines.invalidate(source_id)


@pytest.fixture
async def partial_warehouse(tmp_path):
    source_id = await _warehouse(tmp_path, truncated=True)
    yield source_id
    await engines.invalidate(source_id)


def node(nid, ntype, **config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0}, "data": {"label": nid, "config": config}}


def weekly(*, report=None, contract=None):
    nodes = [
        node("start", "input"),
        node("fetch", "tool", tool=TOOL, args={"sql": SQL}),
        node("card", "metrics", caliber="周报口径", caliber_version="v2", metrics=[
            {"id": "gmv", "name": "销售额", "unit": "元", "decimals": 1, "format": "thousands",
             "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
        node("write", "report", instructions="写周报", on_violation="flag", max_repairs=0, **(report or {})),
        node("out", "output", fields=[{"name": "周报", "value": "{{ nodes.write.text }}"}],
             contract={"report_from": "write", "metrics_from": ["card"], "required": ["gmv"], "strict": True,
                       **(contract or {})}),
    ]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def script(monkeypatch, text):
    from app.providers import mock_model

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", lambda self, messages: AIMessage(content=text))


async def wait(run_id: str) -> Run:
    for _ in range(400):
        await asyncio.sleep(0.05)
        async with SessionLocal() as session:
            row = await session.get(Run, run_id)
        if row.status in ("succeeded", "failed") and row.manifest_seq is not None:
            return row
    raise AssertionError(f"run {run_id} 没跑完：{row.status}")


async def explore(graph) -> dict:
    run = await run_manager.start(graph=graph, input_payload={})
    row = await wait(run.id)
    assert row.status == "succeeded", row.error
    return row.output["_issuance"]


async def formal(client, graph, *, status: str) -> Run:
    """把这张图发布成 status 级别，再从发布版本发起正式运行。"""
    async with SessionLocal() as session:
        wf = Workflow(name=f"claims-{status}-{uuid.uuid4().hex[:6]}", graph=graph, status=status, published_version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=graph))
        await session.commit()
        wf_id = wf.id
    res = await client.post("/api/runs", json={"workflow_id": wf_id, "run_class": "formal"})
    assert res.status_code == 201, res.text
    row = await wait(res.json()["id"])
    assert row.status == "succeeded", row.error
    return row


async def issuance_event(run_id: str) -> dict:
    async with SessionLocal() as session:
        [event] = (await session.execute(select(RunEvent).where(
            RunEvent.run_id == run_id, RunEvent.type == "issuance"))).scalars()
    return event.data


# --------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------


async def test_require_citation_degrades_uncited_claims(warehouse, monkeypatch):
    script(monkeypatch, UNCITED)
    issuance = await explore(weekly(report={"claims": "require_citation"}))
    assert issuance["tier"] == "degraded", "strict 下也只是降档：结论句没挂依据不是数没有出处"
    claims = issuance["claims"]
    assert claims["policy"] == "require_citation" and claims["uncited_claims"] == 1
    [item] = claims["uncited"]
    assert item["text"] == "增长主要来自新客。" and item["unit"] and len(item["span"]) == 2
    assert issuance["unmatched_numbers"] == [] and issuance["unresolved"] == []
    assert not any("结论句" in g for g in issuance["gaps"]), "判了就不是「没核」"


async def test_require_citation_with_every_claim_cited_is_formal(warehouse, monkeypatch):
    script(monkeypatch, CLEAN)
    issuance = await explore(weekly(report={"claims": "require_citation"}))
    assert issuance["tier"] == "formal"
    assert issuance["claims"] == {"policy": "require_citation", "uncited_claims": 0, "uncited": []}


async def test_claims_off_changes_nothing(warehouse, monkeypatch):
    script(monkeypatch, UNCITED)
    issuance = await explore(weekly())
    assert issuance["tier"] == "formal" and "claims" not in issuance


async def test_an_auto_linked_name_does_not_count_as_a_citation(warehouse, monkeypatch):
    """只提到表名的结论句仍算没挂依据：自动链接的名字是系统加的标注，不是写作者给的出处。"""
    script(monkeypatch, CLEAN + "增长主要来自 `orders` 里的新客。")
    issuance = await explore(weekly(report={"claims": "require_citation"}))
    assert issuance["tier"] == "degraded" and issuance["claims"]["uncited_claims"] == 1


async def test_runs_from_before_the_upgrade_ignore_claims(warehouse, monkeypatch):
    async def no_snapshot(session):
        return None

    monkeypatch.setattr(runner_mod, "_agent_limits", no_snapshot)
    script(monkeypatch, UNCITED)
    issuance = await explore(weekly(report={"claims": "require_citation"}))
    assert issuance["tier"] == "formal" and "claims" not in issuance and "entities" not in issuance


async def test_the_contract_can_ask_for_citations_too(warehouse, monkeypatch):
    script(monkeypatch, UNCITED)
    issuance = await explore(weekly(contract={"claims": {"policy": "require_citation", "on_uncited": "withhold"}}))
    assert issuance["tier"] == "withheld" and issuance["claims"]["uncited_claims"] == 1
    script(monkeypatch, CLEAN)
    issuance = await explore(weekly(contract={"claims": "require_citation"}))
    assert issuance["tier"] == "formal" and not any("结论句" in g for g in issuance["gaps"])


LOOSE = {"claims": {"policy": "require_citation", "on_uncited": "ignore"}}


async def test_the_contract_can_tighten_the_report_policy_but_never_loosen_it(warehouse, monkeypatch):
    """报告节点写了 require_citation：契约写 withhold 能收紧成不予出具；写 ignore、写 off 都放不松，照旧降档。"""
    script(monkeypatch, UNCITED)
    asks = {"claims": "require_citation"}
    issuance = await explore(weekly(report=asks, contract=LOOSE))
    assert issuance["tier"] == "degraded", "契约的 ignore 不能把报告节点要求的缺口抹掉"
    assert issuance["claims"]["uncited_claims"] == 1 and "on_uncited" not in issuance["claims"]
    issuance = await explore(weekly(report=asks, contract={"claims": "off"}))
    assert issuance["tier"] == "degraded" and issuance["claims"]["uncited_claims"] == 1
    issuance = await explore(weekly(report=asks, contract={"claims": {"policy": "require_citation",
                                                                      "on_uncited": "withhold"}}))
    assert issuance["tier"] == "withheld" and issuance["claims"]["on_uncited"] == "withhold"


async def test_ignore_cannot_lower_the_governed_floor(client, warehouse, monkeypatch):
    """受管级别的正式运行：契约写 on_uncited: ignore 不算数（没有哪道门禁查契约里的 claims），没挂依据的结论句照样
    计入缺口、降一档。"""
    script(monkeypatch, UNCITED)
    row = await formal(client, weekly(report={"claims": "require_citation"}, contract=LOOSE), status="governed")
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded" and issuance["gaps"] == [], issuance
    assert issuance["claims"]["uncited_claims"] == 1 and "on_uncited" not in issuance["claims"]
    assert (await issuance_event(row.id))["claims"] == {"policy": "require_citation", "uncited_claims": 1}
    # 报告节点没写（门禁之前发布的版本）、只有契约写了 ignore：受管级别同样不认 ignore
    row = await formal(client, weekly(contract=LOOSE), status="governed")
    assert row.output["_issuance"]["tier"] == "degraded"


async def test_outside_governed_a_contract_may_ignore_its_own_policy(client, warehouse, monkeypatch):
    """报告节点没要求时，契约自己写的 ignore 在探索运行、已发布级别照旧生效：不计入缺口，照实记下 ignore。"""
    script(monkeypatch, UNCITED)
    issuance = await explore(weekly(contract=LOOSE))
    assert issuance["tier"] == "formal" and issuance["claims"]["on_uncited"] == "ignore"
    row = await formal(client, weekly(contract=LOOSE), status="published")
    assert row.output["_issuance"]["tier"] == "formal"
    assert (await issuance_event(row.id))["claims"] == {"policy": "require_citation", "uncited_claims": 1,
                                                       "on_uncited": "ignore"}


@pytest.mark.parametrize(("written", "declared", "governed", "effect"), [
    ("require_citation", None, False, "degrade"),
    ("require_citation", "off", False, "degrade"),
    ("require_citation", {"policy": "require_citation", "on_uncited": "ignore"}, False, "degrade"),
    ("require_citation", {"policy": "require_citation", "on_uncited": "degrade"}, False, "degrade"),
    ("require_citation", {"policy": "require_citation", "on_uncited": "withhold"}, False, "withhold"),
    ("require_citation", {"policy": "require_citation", "on_uncited": "bogus"}, False, "degrade"),
    ("off", "require_citation", False, "degrade"),
    ("off", {"policy": "require_citation", "on_uncited": "withhold"}, True, "withhold"),
    ("off", {"policy": "require_citation", "on_uncited": "ignore"}, False, "ignore"),
    ("off", {"policy": "require_citation", "on_uncited": "ignore"}, True, "degrade"),
    ("off", {"policy": "require_citation", "on_uncited": "bogus"}, False, "degrade"),
    ("off", None, True, None),
    ("off", "off", True, None),
])
def test_the_effective_claims_policy_is_the_stricter_one(written, declared, governed, effect):
    """报告节点记下的策略和契约写的取更严的那个；受管级别 ignore 当 degrade；认不出的写法不放松（当 degrade）。"""
    from app.engine.issuance import decide_tier
    from app.engine.nodes.io import _claims

    out = {"gaps": [], "claims_policy": None, "uncited": None, "claims": None}
    uncited = [{"unit": "u1", "span": [0, 4], "text": "增长很快"}]
    _claims(out, {"claims": declared} if declared is not None else {}, {"text": "x", "claims": written},
            evidence_on=True, governed=governed, uncited=uncited)
    assert out["gaps"] == []
    if effect is None:
        assert out["claims_policy"] is None and out["claims"] is None
        return
    assert out["claims"]["uncited_claims"] == 1 and out["claims"].get("on_uncited", "degrade") == effect
    tier = decide_tier(missing_required=[], missing_expected=[], unmatched=[], strict=True, gaps=[],
                       uncited_claims=out["uncited"], claims_policy=out["claims_policy"])
    assert tier == {"degrade": "degraded", "withhold": "withheld", "ignore": "formal"}[effect]


def test_a_run_in_flight_across_the_upgrade_keeps_the_old_gap():
    """升级前写的报告（产出里没有 claims）碰上写了 require_citation 的契约：照旧只记一条缺口，不判档。"""
    from app.engine.nodes.io import _claims

    out = {"gaps": [], "claims_policy": None, "uncited": None, "claims": None}
    _claims(out, {"claims": "require_citation"}, {"text": "x"}, evidence_on=True, governed=False,
            uncited=[{"unit": "u1", "span": [0, 4], "text": "增长很快"}])
    assert out["claims_policy"] is None and out["claims"] is None
    assert len(out["gaps"]) == 1 and "生成于系统升级前" in out["gaps"][0]


# --------------------------------------------------------------------------
# 可疑实体
# --------------------------------------------------------------------------


async def test_suspicious_names_are_only_annotated_in_exploratory_runs(warehouse, monkeypatch):
    script(monkeypatch, SUSPICIOUS)
    issuance = await explore(weekly())
    assert issuance["tier"] == "formal" and issuance["gaps"] == []
    [fake] = issuance["entities"]["unknown"]
    assert fake["name"] == "refund_log" and fake["segment"] and fake["unit"]
    assert issuance["entities"]["unverified"] == [] and issuance["entities"]["counted"] is False


async def test_suspicious_names_degrade_governed_formal_runs(client, warehouse, monkeypatch):
    script(monkeypatch, SUSPICIOUS)
    row = await formal(client, weekly(), status="governed")
    issuance = row.output["_issuance"]
    assert issuance["tier"] == "degraded", "按缺口降档，不是不予出具（契约是 strict 的）"
    assert any("refund_log" in g and "可疑" in g for g in issuance["gaps"]), issuance["gaps"]
    assert issuance["entities"]["counted"] is True and issuance["entities"]["unknown"][0]["name"] == "refund_log"
    event = await issuance_event(row.id)
    assert event["tier"] == "degraded" and event["entities"] == {"unknown": 1, "unverified": 0, "counted": True}


async def test_published_formal_runs_only_annotate(client, warehouse, monkeypatch):
    script(monkeypatch, SUSPICIOUS)
    issuance = (await formal(client, weekly(), status="published")).output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == []
    assert issuance["entities"]["unknown"][0]["name"] == "refund_log" and issuance["entities"]["counted"] is False


async def test_names_that_cannot_be_checked_are_only_annotated_even_when_governed(client, partial_warehouse,
                                                                                  monkeypatch):
    script(monkeypatch, SUSPICIOUS)
    issuance = (await formal(client, weekly(), status="governed")).output["_issuance"]
    assert issuance["tier"] == "formal" and issuance["gaps"] == []
    assert issuance["entities"]["unknown"] == []
    assert [u["name"] for u in issuance["entities"]["unverified"]] == ["refund_log"]


async def test_the_exit_says_the_report_turned_entities_off(warehouse, monkeypatch):
    """entities: off 的报告写了 [[t:]]：出口复核和报告节点一样照实说是这个节点关掉的，不说成「后续版本支持」。"""
    from app.engine.evidence import ENTITIES_OFF_REASON, LATER_REASON

    script(monkeypatch, "东区销售额 [[m:gmv]]，见 [[t:orders]]。[[see:m:gmv]]")
    issuance = await explore(weekly(report={"entities": "off"}))
    [unresolved] = issuance["unresolved"]
    assert unresolved["ref"] == "t:orders" and ENTITIES_OFF_REASON in unresolved["message"]
    assert LATER_REASON not in unresolved["message"] and "entities" not in issuance


async def test_clean_reports_carry_no_entity_annotation(warehouse, monkeypatch):
    script(monkeypatch, CLEAN)
    issuance = await explore(weekly())
    assert issuance["tier"] == "formal" and "entities" not in issuance
