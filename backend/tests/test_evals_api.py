"""评测接口（app/api/evals.py）：题集和题目的增删改查（乐观锁）、保存时试算、从运行认定、口径升版处置、导出导入、
目录快照；零售样例题集（tests/fixtures/eval/retail_suite.json）在虚构零售库上试算通过。

库、表、数字、人名都是虚构的。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.data import introspect
from app.db.base import SessionLocal
from app.db.models import DataSource, Run, Workflow, WorkflowVersion
from app.db.models_eval import EvalRun, EvalSuite
from app.engine.runner import run_manager
from app.eval import snapshots
from app.main import app
from tests.fixtures import dual_check_shop as shop
from tests.fixtures.acceptance import retail
from tests.fixtures.catalog.fake_model import FakeModel
from tests.fixtures.names import letters
from tests.fixtures.sources import drop_source

SUITE_FILE = Path(__file__).parent / "fixtures" / "eval" / "retail_suite.json"
LAST_WEEK = "BETWEEN '{{ schedule.last_week_start }}' AND '{{ schedule.last_week_end }}'"
#: dual_check_shop 的订单都在 2026-09：「上周」按截至时间算，这里用写死日期的 SQL
SEPT = "BETWEEN '2026-09-01' AND '2026-09-07'"
ACTOR = {"X-Actor": quote("评测员甲")}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def source(tmp_path):
    name, sid = await shop.add_source(shop.build(tmp_path / "shop.db"))
    yield {"name": name, "id": sid}
    await drop_source(sid)


async def new_suite(client, **body) -> dict:
    res = await client.post("/api/evals/suites", json={"name": f"虚构题集_{letters(6)}", **body}, headers=ACTOR)
    assert res.status_code == 201, res.text
    return res.json()


def sql_case(source_id: str, sql: str = f"SELECT SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' "
                                       f"AND paid_date {SEPT}", **kw) -> dict:
    return {"question": "9 月第一周按支付日、不含退款的销售额？", "datasource_ids": [source_id],
            "gold": {"kind": "sql", "datasource_id": source_id, "sql": sql},
            "expectations": [{"kind": "scalar", "column": "gmv"}], **kw}


# --------------------------------------------------------------------------
# 题集
# --------------------------------------------------------------------------


async def test_suites_crud_with_optimistic_lock(client):
    made = await new_suite(client, description="说明", budget={"max_parallel": 3})
    assert made["created_by"] == "评测员甲" and made["version"] == 1 and made["timezone"] == "Asia/Shanghai"
    assert made["budget"] == {"max_parallel": 3}
    assert made["effective_budget"] == {"max_parallel": 3, "per_case_timeout_s": 900, "per_run_max_usd": 2.0,
                                        "daily_max_usd": 5.0}
    listed = (await client.get("/api/evals/suites")).json()["suites"]
    assert any(s["id"] == made["id"] and s["counts"] == {"cases": 0, "enabled": 0} for s in listed)

    res = await client.patch(f"/api/evals/suites/{made['id']}", json={"name": "改了名"})
    assert res.status_code == 422 and res.json()["field"] == "version"
    res = await client.patch(f"/api/evals/suites/{made['id']}", json={"name": "改了名", "version": 1})
    assert res.status_code == 200 and res.json()["name"] == "改了名" and res.json()["version"] == 2
    assert res.json()["budget"] == {"max_parallel": 3}, "没带的项照旧"
    res = await client.patch(f"/api/evals/suites/{made['id']}", json={"name": "再改", "version": 1})
    assert res.status_code == 409 and res.json()["code"] == "eval_version_conflict"

    for body, field in (({"name": ""}, "name"), ({"name": "x", "timezone": "火星"}, "timezone"),
                        ({"name": "x", "budget": {"per_case_timeout_s": 5}}, "budget.per_case_timeout_s"),
                        ({"name": "x", "budget": {"per_run_max_usd": 50, "daily_max_usd": 10}}, "budget.per_run_max_usd"),
                        ({"name": "x", "budget": {"nope": 1}}, "budget.nope")):
        res = await client.post("/api/evals/suites", json=body)
        assert res.status_code == 422 and res.json()["field"] == field, (body, res.text)

    # 有评测记录的不能删，只能归档
    async with SessionLocal() as session:
        session.add(EvalRun(suite_id=made["id"], status="succeeded"))
        await session.commit()
    res = await client.delete(f"/api/evals/suites/{made['id']}")
    assert res.status_code == 409 and res.json()["code"] == "eval_suite_has_runs"
    async with SessionLocal() as session:
        await session.execute(delete(EvalRun).where(EvalRun.suite_id == made["id"]))
        await session.commit()
    assert (await client.delete(f"/api/evals/suites/{made['id']}")).status_code == 204
    assert (await client.get(f"/api/evals/suites/{made['id']}")).status_code == 404


# --------------------------------------------------------------------------
# 题目：保存时试算
# --------------------------------------------------------------------------


async def test_a_case_is_saved_only_after_a_trial_passes(client, source):
    suite = await new_suite(client)
    url = f"/api/evals/suites/{suite['id']}/cases"
    res = await client.post(url, json=sql_case(source["id"]), headers=ACTOR)
    assert res.status_code == 201, res.text
    body = res.json()
    case, trial = body["case"], body["trial"]
    assert case["seq"] == 1 and case["version"] == 1 and case["created_by"] == "评测员甲" and len(case["case_hash"]) == 64
    assert trial["status"] == "ok" and trial["sets"][0]["values"]["e1"]["value"] == 330.0
    # 题目上只记试算的结论和运行，不存期望值（期望每次重算）
    assert case["trial"]["status"] == "ok" and case["trial"]["by"] == "评测员甲" and "sets" not in case["trial"]
    assert case["gold_source"] == {"id": source["id"], "name": source["name"]}
    async with SessionLocal() as session:
        run = await session.get(Run, trial["run_ids"][0])
    assert run.origin_kind == "eval" and run.origin["trial"] is True and run.origin["suite_id"] == suite["id"]

    # 只试算、不保存
    res = await client.post(f"{url}/trial", json=sql_case(source["id"], sql="SELECT nope FROM orders"))
    assert res.status_code == 200 and res.json()["trial"]["status"] == "failed"

    for bad, code, field in (
        (sql_case(source["id"], expectations=[{"kind": "scalar", "column": "gmv", "value": 330}]),
         "eval_case_invalid", "expectations.0.value"),                              # 评测-D4：数值期望不许人填
        (sql_case(source["id"], history=[{"question": "上一轮"}]), "eval_case_invalid", "history"),   # 评测-D12
        (sql_case(source["id"], sql="DELETE FROM orders"), "eval_gold_guard", "gold.sql"),
        (sql_case(source["id"], sql="SELECT SUM(amount) AS gmv FROM orders WHERE {{ schedule.nope }}"),
         "eval_case_invalid", "gold.sql"),
        (sql_case(source["id"], sql="SELECT region FROM orders WHERE region = '华中'"), "eval_gold_no_value", "gold"),
        ({"question": "q", "expectations": [{"kind": "scalar", "column": "gmv"}]}, "eval_case_invalid", "gold"),
        ({"question": "最近卖得怎么样", "expectations": [{"kind": "ask"}, {"kind": "none"}],
          "gold": {"kind": "sql", "datasource_id": source["id"], "sql": "SELECT 1"}}, "eval_case_invalid", "expectations"),
        (sql_case("nope"), "eval_case_invalid", "gold.datasource_id"),
    ):
        res = await client.post(url, json=bad)
        assert res.status_code == 422, (bad, res.text)
        assert (res.json()["code"], res.json().get("field")) == (code, field), res.json()
    detail = (await client.get(f"/api/evals/suites/{suite['id']}")).json()
    assert [c["seq"] for c in detail["cases"]] == [1], "没过的一道都没存"


async def test_masked_columns_are_refused_at_save(client, tmp_path):
    name, sid = await shop.add_source(shop.build(tmp_path / "masked.db"), options={"mask_columns": ["region"]})
    try:
        suite = await new_suite(client)
        body = sql_case(sid, sql="SELECT region AS 地区, SUM(amount) AS gmv FROM orders GROUP BY region",
                        expectations=[{"kind": "table", "keys": ["地区"]}])
        res = await client.post(f"/api/evals/suites/{suite['id']}/cases", json=body)
        assert res.status_code == 422 and res.json()["code"] == "eval_gold_masked" and "遮罩" in res.json()["detail"]
    finally:
        await drop_source(sid)


async def test_editing_a_case_retries_only_when_its_content_changes(client, source):
    suite = await new_suite(client)
    made = (await client.post(f"/api/evals/suites/{suite['id']}/cases", json=sql_case(source["id"]))).json()["case"]
    url = f"/api/evals/suites/{suite['id']}/cases/{made['id']}"
    res = await client.patch(url, json={"tags": ["口径陷阱"], "version": 1})
    assert res.status_code == 200 and res.json()["trial"] is None, "只改标签不试算"
    case = res.json()["case"]
    assert case["tags"] == ["口径陷阱"] and case["version"] == 2 and case["case_hash"] == made["case_hash"]
    res = await client.patch(url, json={"gold": {"kind": "sql", "datasource_id": source["id"],
                                                 "sql": f"SELECT SUM(amount) AS gmv FROM orders WHERE paid_date {SEPT}"},
                                        "version": 2})
    assert res.status_code == 200 and res.json()["trial"]["sets"][0]["values"]["e1"]["value"] == 380.0
    assert res.json()["case"]["case_hash"] != made["case_hash"], "题目改过，指纹跟着变"
    res = await client.patch(url, json={"gold": {"kind": "sql", "datasource_id": source["id"],
                                                 "sql": "DELETE FROM orders"}, "version": 3})
    assert res.status_code == 422 and res.json()["code"] == "eval_gold_guard"
    res = await client.patch(url, json={"tags": [], "version": 2})
    assert res.status_code == 409
    # 已有的题按此刻再试算一次：记进 trial、不改 version
    res = await client.post(f"{url}/trial")
    assert res.status_code == 200 and res.json()["trial"]["status"] == "ok"
    assert (await client.get(f"/api/evals/suites/{suite['id']}")).json()["cases"][0]["version"] == 3
    assert (await client.delete(url)).status_code == 204


# --------------------------------------------------------------------------
# 从运行认定
# --------------------------------------------------------------------------


async def test_a_query_from_a_run_becomes_a_case(client, source):
    from app.engine.launch import launch_run

    sql = f"SELECT SUM(amount) AS gmv FROM orders WHERE status <> 'refunded' AND paid_date {SEPT}"
    graph = {"nodes": [{"id": "start", "type": "input", "data": {"config": {}}},
                       {"id": "q", "type": "tool", "data": {"config": {"tool": f"db_query__{source['name']}",
                                                                       "args": {"sql": sql}}}},
                       {"id": "out", "type": "output", "data": {"config": {}}}],
             "edges": [{"source": "start", "target": "q"}, {"source": "q", "target": "out"}]}
    async with SessionLocal() as session:
        run = await launch_run(session, graph=graph, input_payload={"question": "9 月第一周卖了多少？"}, actor="提问人",
                               origin_kind="chat")
    await run_manager.wait_idle(run.id, timeout=30)
    queries = (await client.get(f"/api/evals/runs/{run.id}/queries")).json()["queries"]
    assert len(queries) == 1 and queries[0]["sql"] == sql and queries[0]["source_id"] == source["id"]
    assert "rows" not in queries[0], "只给 SQL、列名、行数，不给结果的格"
    suite = await new_suite(client)
    res = await client.post(f"/api/evals/suites/{suite['id']}/cases/from-run", headers=ACTOR,
                            json={"run_id": run.id, "query": queries[0]["artifact"],
                                  "expectations": [{"kind": "scalar", "column": "gmv"}]})
    assert res.status_code == 201, res.text
    case = res.json()["case"]
    assert case["question"] == "9 月第一周卖了多少？", "问法没给就取那次运行的问题"
    gold = case["gold"]
    assert gold["kind"] == "from_run" and gold["sql"] == sql and gold["run_id"] == run.id
    assert gold["confirmed_by"] == "评测员甲" and gold["confirmed_at"] and gold["query"] == queries[0]["artifact"]
    assert res.json()["trial"]["sets"][0]["values"]["e1"]["value"] == 330.0
    # 之后按这条 SQL 重算，不冻结数值；要改 SQL 就改成 SQL 题，不能在「从运行认定」上改
    res = await client.patch(f"/api/evals/suites/{suite['id']}/cases/{case['id']}",
                             json={"gold": {**gold, "sql": "SELECT 1 AS gmv"}, "version": 1})
    assert res.status_code == 422 and res.json()["field"] == "gold.kind"
    res = await client.patch(f"/api/evals/suites/{suite['id']}/cases/{case['id']}",
                             json={"gold": gold, "tags": ["从运行"], "version": 1})
    assert res.status_code == 200 and res.json()["case"]["gold"]["confirmed_by"] == "评测员甲"
    res = await client.post(f"/api/evals/suites/{suite['id']}/cases/from-run",
                            json={"run_id": run.id, "query": "nope", "expectations": [{"kind": "none"}]})
    assert res.status_code == 404
    # 客户端不能自己写「从运行认定」
    res = await client.post(f"/api/evals/suites/{suite['id']}/cases", json={**sql_case(source["id"]), "gold": gold})
    assert res.status_code == 422 and res.json()["field"] == "gold.kind"


# --------------------------------------------------------------------------
# 口径题：清单与升版处置
# --------------------------------------------------------------------------


def _weekly(source_name: str, *, refunds: bool = False) -> dict:
    status = "" if refunds else "status <> 'refunded' AND "

    def node(nid, ntype, **config):
        return {"id": nid, "type": ntype, "data": {"label": nid, "config": config}}

    nodes = [node("start", "input", fields=[{"name": "start", "default": "2026-09-01"},
                                            {"name": "end", "default": "2026-09-07"}]),
             node("fetch", "tool", tool=f"db_query__{source_name}", args={"sql": (
                 f"SELECT SUM(amount) AS gmv FROM orders WHERE {status}"
                 "paid_date BETWEEN '{{ input.start }}' AND '{{ input.end }}'")}),
             node("card", "metrics", caliber="周销售", metrics=[{"id": "gmv", "name": "销售额",
                                                                 "expression": "cell(nodes.fetch, 0, 'gmv')"}]),
             node("out", "output")]
    return {"nodes": nodes, "edges": [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]}


async def test_caliber_cases_list_and_dispose_upgrades(client, source):
    async with SessionLocal() as session:
        wf = Workflow(name=f"周销售_{letters(6)}", graph=_weekly(source["name"]), status="published",
                      published_version=1, version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=_weekly(source["name"]), level="published"))
        await session.commit()
        wf_id = wf.id
    calibers = (await client.get("/api/evals/calibers")).json()["calibers"]
    mine = [c for c in calibers if c["workflow_id"] == wf_id]
    assert mine == [{"workflow_id": wf_id, "workflow_name": mine[0]["workflow_name"], "version": 1,
                     "level": "published", "node_id": "card", "card": "card", "caliber": "周销售 @ v1",
                     "metric_id": "gmv", "name": "销售额", "unit": "", "reusable": True, "reason": None}]
    suite = await new_suite(client)
    body = {"question": "上周的销售额？", "gold": {"kind": "caliber", "workflow_id": wf_id, "version": 1,
                                                 "node_id": "card", "inputs": {}},
            "expectations": [{"kind": "scalar", "metric": "gmv"}]}
    res = await client.post(f"/api/evals/suites/{suite['id']}/cases", json=body)
    assert res.status_code == 201, res.text
    case = res.json()["case"]
    assert case["upgrade"]["pending"] is False and res.json()["trial"]["sets"][0]["values"]["e1"]["value"] == 330.0
    bad = {**body, "expectations": [{"kind": "scalar", "metric": "nope"}]}
    assert (await client.post(f"/api/evals/suites/{suite['id']}/cases", json=bad)).json()["field"] == \
        "expectations.0.metric"

    async with SessionLocal() as session:
        wf = await session.get(Workflow, wf_id)
        wf.published_version = wf.version = 2
        session.add(WorkflowVersion(workflow_id=wf_id, version=2, graph=_weekly(source["name"], refunds=True),
                                    level="published"))
        await session.commit()
    detail = (await client.get(f"/api/evals/suites/{suite['id']}")).json()
    assert detail["pending_upgrades"] == 1 and detail["cases"][0]["upgrade"]["message"] == "口径已升版到 v2，待处置"
    url = f"/api/evals/suites/{suite['id']}/cases/{case['id']}/upgrade"
    info = (await client.get(url)).json()
    assert set(info["choices"]) == {"recompute", "dual", "incomparable"}
    res = await client.post(url, json={"version": 1})
    assert res.status_code == 422 and res.json()["code"] == "eval_dispose_invalid", "没有缺省"
    # 不可比：旧题归档，按新版本另建新题
    res = await client.post(url, json={"policy": "incomparable", "version": 1}, headers=ACTOR)
    assert res.status_code == 200, res.text
    old, new = res.json()["case"], res.json()["new_case"]
    assert old["archived"] and not old["enabled"] and old["superseded_by"] == new["id"]
    assert new["gold"]["version"] == 2 and new["seq"] == 2 and new["upgrade"]["pending"] is False
    assert res.json()["trial"]["sets"][0]["values"]["e1"]["value"] == 380.0
    detail = (await client.get(f"/api/evals/suites/{suite['id']}")).json()
    assert detail["pending_upgrades"] == 0 and detail["counts"]["cases"] == 1
    # 归档的题不再待处置；在新题上再来一版就改钉
    async with SessionLocal() as session:
        wf = await session.get(Workflow, wf_id)
        wf.published_version = wf.version = 3
        session.add(WorkflowVersion(workflow_id=wf_id, version=3, graph=_weekly(source["name"]), level="published"))
        await session.commit()
    res = await client.post(f"/api/evals/suites/{suite['id']}/cases/{new['id']}/upgrade",
                            json={"policy": "recompute", "version": 1}, headers=ACTOR)
    assert res.status_code == 200, res.text
    moved = res.json()["case"]
    assert moved["gold"]["version"] == 3 and moved["gold"]["upgrade"]["by"] == "评测员甲"
    assert moved["case_hash"] != new["case_hash"] and moved["version"] == 2
    res = await client.post(f"/api/evals/suites/{suite['id']}/cases/{new['id']}/upgrade",
                            json={"policy": "dual", "version": 2})
    assert res.status_code == 409 and res.json()["code"] == "eval_dispose_nothing_pending"


async def test_caliber_cases_pin_only_published_versions(client, source):
    async with SessionLocal() as session:
        wf = Workflow(name=f"草稿口径_{letters(6)}", graph=_weekly(source["name"]), status="draft", version=1)
        session.add(wf)
        await session.flush()
        session.add(WorkflowVersion(workflow_id=wf.id, version=1, graph=_weekly(source["name"])))
        await session.commit()
        wf_id = wf.id
    suite = await new_suite(client)
    res = await client.post(f"/api/evals/suites/{suite['id']}/cases", json={
        "question": "q", "gold": {"kind": "caliber", "workflow_id": wf_id, "version": 1, "node_id": "card"},
        "expectations": [{"kind": "scalar", "metric": "gmv"}]})
    assert res.status_code == 422 and res.json()["field"] == "gold.version" and "没有发布过" in res.json()["detail"]


# --------------------------------------------------------------------------
# 导出导入
# --------------------------------------------------------------------------


async def test_export_then_import_matches_sources_by_name(client, source):
    suite = await new_suite(client, description="导出用")
    await client.post(f"/api/evals/suites/{suite['id']}/cases", json=sql_case(source["id"], tags=["口径陷阱"]))
    await client.post(f"/api/evals/suites/{suite['id']}/cases",
                      json={"question": "最近怎么样？", "expectations": [{"kind": "ask"}]})
    res = await client.get(f"/api/evals/suites/{suite['id']}/export")
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    content = res.json()
    text = json.dumps(content, ensure_ascii=False)
    assert content["format"] == "agentlab-eval-suite" and len(content["cases"]) == 2
    # 按名字、不含期望值、答案、运行
    first = content["cases"][0]
    assert first["datasources"] == [source["name"]] and first["gold"]["datasource"] == source["name"]
    assert "datasource_id" not in first["gold"] and source["id"] not in text
    assert "330" not in text and "run_id" not in text and "trial" not in text

    preview = (await client.post("/api/evals/suites/import/preview", json={"content": content})).json()
    assert preview["importable"] == 2 and preview["sources"] == {source["name"]: source["id"]}
    res = await client.post("/api/evals/suites/import", json={"content": content, "name": "导进来的"}, headers=ACTOR)
    assert res.status_code == 201, res.text
    imported = res.json()["suite"]
    assert imported["name"] == "导进来的" and res.json()["imported"] == 2 and res.json()["skipped"] == []
    assert [c["question"] for c in imported["cases"]] == [c["question"] for c in content["cases"]]
    assert imported["cases"][0]["gold"]["datasource_id"] == source["id"]
    assert imported["cases"][0]["trial"]["status"] == "ok"

    # 对不上的数据源：跳过、写原因；一道都导不进来就不建题集
    other = json.loads(json.dumps(content).replace(source["name"], "ghost_source"))
    preview = (await client.post("/api/evals/suites/import/preview", json={"content": other})).json()
    assert preview["skipped"] == 1 and "ghost_source" in preview["cases"][0]["reason"]
    only_bad = {**other, "cases": other["cases"][:1]}
    res = await client.post("/api/evals/suites/import", json={"content": only_bad})
    assert res.status_code == 422 and "ghost_source" in res.json()["detail"]
    res = await client.post("/api/evals/suites/import/preview", json={"content": {"format": "x"}})
    assert res.status_code == 422 and res.json()["code"] == "eval_import_invalid"


async def test_the_retail_sample_suite_trials_on_the_retail_database(client, tmp_path):
    """仓库里附的零售样例题集：在虚构零售库上导入，每道题都试算通过，期望和直接查库一样，陷阱按口径算。"""
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    path = retail.build(tmp_path / "retail.db", today)
    await drop_source(name=retail.SOURCE_NAME)
    async with SessionLocal() as session:
        row = DataSource(name=retail.SOURCE_NAME, kind="sqlite", database=str(path), readonly=True,
                         options={"mask_columns": ["phone"]})
        session.add(row)
        await session.flush()
        row.schema_cache = await introspect.introspect(row)
        await session.commit()
        sid = row.id
    try:
        content = json.loads(SUITE_FILE.read_text(encoding="utf-8"))
        res = await client.post("/api/evals/suites/import", json={"content": content}, headers=ACTOR)
        assert res.status_code == 201, res.text
        assert res.json()["imported"] == 6 and res.json()["skipped"] == []
        suite = res.json()["suite"]
        cases = {c["seq"]: c for c in suite["cases"]}
        assert all(c["trial"]["status"] == "ok" for c in cases.values())

        monday = today - timedelta(days=today.weekday())
        start, end = (monday - timedelta(days=7)).isoformat(), (monday - timedelta(days=1)).isoformat()

        def one(sql: str):
            with sqlite3.connect(path) as db:
                return db.execute(sql).fetchone()

        async def trial(seq: int) -> dict:
            out = (await client.post(f"/api/evals/suites/{suite['id']}/cases/{cases[seq]['id']}/trial")).json()["trial"]
            assert out["status"] == "ok", out
            assert f"'{start}' AND '{end}'" in (out["rendered"].get("sql") or f"'{start}' AND '{end}'")
            return out["sets"][0]["values"] if out["sets"] else {}

        paid = one(f"SELECT ROUND(SUM(amount), 2) FROM orders WHERE status = 'paid' AND paid_date BETWEEN '{start}' "
                   f"AND '{end}'")[0]
        by_order_date = one(f"SELECT ROUND(SUM(amount), 2) FROM orders WHERE status = 'paid' AND order_date BETWEEN "
                            f"'{start}' AND '{end}'")[0]
        with_refunds = one(f"SELECT ROUND(SUM(amount), 2) FROM orders WHERE status <> 'cancelled' AND paid_date "
                           f"BETWEEN '{start}' AND '{end}'")[0]
        values = await trial(1)
        assert values["sales"]["value"] == paid
        # 陷阱确实埋着：按下单日、含退款算出来的都不一样
        assert paid not in (by_order_date, with_refunds)
        refunds = await trial(2)
        assert (refunds["refund_orders"]["value"], refunds["refund_amount"]["value"]) == one(
            f"SELECT COUNT(*), ROUND(SUM(amount), 2) FROM orders WHERE status = 'refunded' AND paid_date BETWEEN "
            f"'{start}' AND '{end}'")
        assert refunds["refund_orders"]["value"] > 0
        cancelled = await trial(3)
        assert cancelled["cancelled"]["value"] == one(
            f"SELECT COUNT(*) FROM orders WHERE status = 'cancelled' AND order_date BETWEEN '{start}' AND '{end}'")[0]
        regions = (await trial(4))["by_region"]
        assert [r["地区"] for r in regions["rows"]] == ["华南", "华东"] and regions["ordered"] is True
        assert regions["rows"][0]["销售额"] > regions["rows"][1]["销售额"]
        assert (await trial(5)) == {"no_data": {"kind": "none", "rows": 0}}
        ask = (await client.post(f"/api/evals/suites/{suite['id']}/cases/{cases[6]['id']}/trial")).json()["trial"]
        assert ask["status"] == "ok" and ask["run_ids"] == [] and cases[6]["expectations"][0]["kind"] == "ask"
        assert cases[1]["tags"] == ["口径陷阱", "支付日归属", "相对日期"]
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(EvalSuite).where(EvalSuite.name == "零售样例题集"))
            await session.commit()
        await drop_source(sid)


# --------------------------------------------------------------------------
# 目录快照
# --------------------------------------------------------------------------


async def test_catalog_snapshots_live_and_shadow(client, source, monkeypatch):
    res = await client.post("/api/evals/catalog-snapshots", json={"source_ids": [source["id"]]}, headers=ACTOR)
    assert res.status_code == 201, res.text
    live = res.json()
    assert live["status"] == "succeeded" and live["view"] == {"mode": "snapshot", "statuses": "all",
                                                             "snapshot": live["artifact_id"]}
    assert (await client.post("/api/evals/catalog-snapshots", json={"source_ids": []})).status_code == 422

    # 测试环境没有真实接入：起草模型用不了，照实回 409，不建任务
    res = await client.post("/api/evals/catalog-snapshots/shadow/estimate",
                            json={"sources": [{"id": source["id"], "tables": ["orders"]}]})
    assert res.status_code == 409 and res.json()["code"] == snapshots.SNAPSHOT_MODEL_UNAVAILABLE

    async def fake_model(session, provider, model):
        return FakeModel({"orders": {"label": "影子订单"}}), "fake-draft"

    monkeypatch.setattr(snapshots, "resolve_model", fake_model)
    monkeypatch.setattr(snapshots.ShadowDraft, "model_override", FakeModel({"orders": {"label": "影子订单"}}))
    est = (await client.post("/api/evals/catalog-snapshots/shadow/estimate",
                             json={"sources": [{"id": source["id"], "tables": ["orders"]}]})).json()
    assert est["sources"][0]["tables"] == ["orders"] and est["estimate"]["calls"] == 1
    assert est["estimate"]["unpriced"] is True and est["estimate"]["model"] == "fake-draft"
    res = await client.post("/api/evals/catalog-snapshots/shadow", headers=ACTOR,
                            json={"sources": [{"id": source["id"], "tables": ["orders"]}], "guidance": "按支付日"})
    assert res.status_code == 202 and res.json()["status"] in ("queued", "running")
    snap_id = res.json()["id"]
    await snapshots.manager.wait(snap_id, timeout=30)
    done = (await client.get(f"/api/evals/catalog-snapshots/{snap_id}")).json()
    assert done["status"] == "succeeded" and done["artifact_id"] and done["created_by"] == "评测员甲"
    assert done["draft"]["guidance"] == "按支付日"
    listed = (await client.get("/api/evals/catalog-snapshots")).json()["snapshots"]
    assert {live["id"], snap_id} <= {s["id"] for s in listed}
    res = await client.post(f"/api/evals/catalog-snapshots/{snap_id}/stop")
    assert res.status_code == 409 and res.json()["code"] == snapshots.SNAPSHOT_FINISHED
    assert (await client.get("/api/evals/catalog-snapshots/nope")).status_code == 404


async def test_evals_are_for_builders_only():
    """期望值和查询原文只给搭建者及以上：使用者 403，搭建者能用。"""
    from tests.fakes import auth_env

    try:
        admin = await auth_env.turn_on_local()
        await admin.aclose()
        await auth_env.make_user("eval_user", "user")
        await auth_env.make_user("eval_builder", "builder")
        user = await auth_env.login("eval_user")
        res = await user.get("/api/evals/suites")
        assert res.status_code == 403 and res.json()["code"] == "role_required"
        assert (await user.post("/api/evals/suites", json={"name": "x"})).status_code == 403
        await user.aclose()
        builder = await auth_env.login("eval_builder")
        res = await builder.post("/api/evals/suites", json={"name": "搭建者的题集"})
        assert res.status_code == 201 and res.json()["created_by"] == "eval_builder"
        await builder.aclose()
    finally:
        await auth_env.reset()
        async with SessionLocal() as session:
            await session.execute(delete(EvalSuite).where(EvalSuite.name == "搭建者的题集"))
            await session.commit()
