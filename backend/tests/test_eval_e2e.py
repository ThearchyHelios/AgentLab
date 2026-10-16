"""评测从头到尾（细案 01 测试 test_eval_e2e）：dual_check_shop 的虚构订单库 + 剧本模型。

题目是「9/1–9/7 按支付日、不含退款的销售额」，期望 SQL 算出 330。助手剧本只在查看结果里看到数据目录写着「按支付日期归属」
时才按支付日写（330），没看到就按下单日写（310）。所以：

- 臂「当前目录」按支付日答 330：通过、可追溯、答案的 SQL 和期望 SQL 等价已证明（逐题 E1）；
- 臂「关目录」按下单日答 310：不通过，差异方面是 time；
- 「目录贡献」对照（当前 vs 关）的配对表是 1:0，两臂共用期望（同一份期望运行）；
- 再评一次（把目录里的业务日期去掉）和上一次比：这题从通过变成不通过，归因「做法变」，指纹里目录快照变了。

评测运行的来源都是 eval，不进使用统计；运行名「评测：题集名·第 n 题」。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Run
from app.db.models_eval import EvalResult, EvalRun
from app.engine.origin import counted_runs
from app.engine.runner import run_manager
from app.main import app
from tests import eval_samples as es
from tests.fixtures import dual_check_shop as shop
from tests.fixtures.sources import drop_source


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await es.drop_suites()
    await run_manager.shutdown()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def source(tmp_path):
    name, sid = await es.make_source(tmp_path)
    yield {"name": name, "id": sid}
    await drop_source(sid)


async def _results(run_id: str) -> list[EvalResult]:
    async with SessionLocal() as session:
        return list((await session.execute(select(EvalResult).where(EvalResult.eval_run_id == run_id)
                                           .order_by(EvalResult.seq))).scalars())


async def test_catalog_contribution_pair_end_to_end(client, monkeypatch, source):
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    suite_id = await es.make_suite(source["id"])
    started = await client.post(f"/api/evals/suites/{suite_id}/evaluations",
                                json={"preset": "catalog", "variant": {"repeats": 1}})
    assert started.status_code == 202, started.text
    body = started.json()
    live, off = body["evaluations"]
    assert (live["arm"], off["arm"]) == ("当前目录", "关目录") and body["pair_id"] == live["id"]
    await es.wait_eval(live["id"])

    got = (await client.get(f"/api/evals/evaluations/{live['id']}")).json()
    assert got["status"] == "succeeded", got
    [right] = await _results(live["id"])
    [wrong] = await _results(off["id"])
    assert right.path == "ran" and right.verdict == "pass", (right.error, right.scores)
    assert right.scores[0]["outcome"] == "traced" and right.scores[0]["got"] == 330.0
    assert right.sql_match["status"] == "proven", right.sql_match
    assert wrong.verdict == "fail" and wrong.scores[0]["outcome"] == "wrong" and wrong.scores[0]["got"] == 310.0
    assert wrong.sql_match["status"] == "related" and "time" in wrong.sql_match["aspects"], wrong.sql_match
    # 两臂共用期望：同一份期望运行
    assert right.gold_run_ids == wrong.gold_run_ids and len(right.gold_run_ids) == 2
    assert right.gold["status"] == "ok"

    pair = (await client.get(f"/api/evals/pairs/{body['pair_id']}")).json()
    [table] = pair["table"]["pairs"]
    assert (table["only_a"], table["only_b"], table["both"], table["neither"]) == (1, 0, 0, 0)
    assert table["p_value"] == 1.0 and table["note"] == "这批题上看不出差别"
    summary = got["summary"]
    assert summary["accuracy"]["passed"] == 1 and summary["traced_accuracy"]["passed"] == 1
    assert summary["accuracy"]["ci"] is not None and summary["e1"]["passed"] == 1

    # 开评时拍了一份当前目录（不占数据源的队），上传表格版本和表结构哈希前后各记一次
    async with SessionLocal() as session:
        ev = await session.get(EvalRun, live["id"])
    assert ev.catalog_snapshot and ev.fingerprint["catalog"].startswith("live:")
    assert ev.fingerprint["sources_before"] == ev.fingerprint["sources_after"]
    assert ev.fingerprint["sources_changed"] == [] and ev.fingerprint["prompt"]

    # 评测运行：来源 eval、不进使用统计、名字写题集和题号
    async with SessionLocal() as session:
        run = await session.get(Run, right.run_id)
        assert run.origin_kind == "eval" and run.origin["role"] == "answer"
        assert run.workflow_name == "评测：虚构零售题集·第 1 题"
        assert run.options["catalog_view"]["mode"] == "snapshot"
        offrun = await session.get(Run, wrong.run_id)
        assert offrun.options["catalog_view"] == {"mode": "off", "statuses": "all"}
        counted = (await session.execute(select(Run.id).where(
            Run.id.in_([right.run_id, wrong.run_id, *right.gold_run_ids]), counted_runs()))).all()
        assert counted == []


async def test_comparing_with_the_previous_run_finds_the_regression(client, monkeypatch, source):
    es.use_copilot(monkeypatch, es.Copilot(source["name"]))
    es.use_report(monkeypatch)
    suite_id = await es.make_suite(source["id"])
    first = (await client.post(f"/api/evals/suites/{suite_id}/evaluations", json={})).json()["evaluations"][0]
    await es.wait_eval(first["id"])
    # 目录里的业务日期去掉：助手改按下单日写
    await es.set_notes(source["id"], {"label": es.NOTES["label"]})
    second = (await client.post(f"/api/evals/suites/{suite_id}/evaluations", json={})).json()["evaluations"][0]
    await es.wait_eval(second["id"])
    async with SessionLocal() as session:
        row = await session.get(EvalRun, second["id"])
    assert row.baseline_id == first["id"]
    assert row.summary["baseline"]["counts"]["regressed"] == 1
    compared = (await client.get(f"/api/evals/evaluations/{second['id']}/compare")).json()
    assert compared["base"]["id"] == first["id"]
    [item] = compared["transitions"]["items"]
    assert item["transition"] == "regressed" and item["cause"] == "method", item
    assert "catalog" in {d["key"] for d in compared["fingerprint_diff"]}, compared["fingerprint_diff"]
    assert shop.RIGHT != shop.BY_ORDER_DATE


async def test_judge_on_charges_the_eval_ledger_and_text_points_stay_apart(client, monkeypatch, source):
    from langchain_core.messages import AIMessage

    from app.engine import judge
    from app.engine.spend import eval_spend
    from app.eval.judging import POINTS_SYSTEM_PROMPT
    from app.providers import mock_model

    es.use_copilot(monkeypatch, es.Copilot(source["name"]))

    def decide(self, messages):
        text = "\n".join(str(m.content) for m in messages)
        if POINTS_SYSTEM_PROMPT[:20] in text:
            return AIMessage(content='[{"point": 1, "verdict": "covered", "reason": "说了按支付日"}]')
        return AIMessage(content=es.REPORT)

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", decide)
    case = es.paid_case(source["id"])
    case["expectations"].append({"kind": "text", "points": ["说明按支付日统计"]})
    suite_id = await es.make_suite(source["id"], cases=[case])
    judge_before = await judge.judge_spend.daily()
    ledger_before = (await eval_spend.daily())["calls"]
    [ev] = (await client.post(f"/api/evals/suites/{suite_id}/evaluations",
                              json={"variant": {"judge": True}})).json()["evaluations"]
    await es.wait_eval(ev["id"])
    [item] = await _results(ev["id"])
    assert item.verdict == "pass", item.error
    text = next(s for s in item.scores if s["kind"] == "text")
    assert text["outcome"] == "covered" and text["required"] is False, "文字要点单列"
    assert item.judge["claims"]["calls"] >= 1, "走了按需裁判那条路"
    assert set(item.judge["claims"]["counts"]) == {"supported", "partial", "contradicted", "insufficient", "unjudged"}
    assert await judge.judge_spend.daily() == judge_before, "裁判的花费记在评测那本账上"
    assert (await eval_spend.daily())["calls"] > ledger_before
    got = (await client.get(f"/api/evals/evaluations/{ev['id']}")).json()
    assert got["summary"]["judge"]["on"] is True and got["summary"]["text"] == {"covered": 1}
