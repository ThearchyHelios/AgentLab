"""仓库里附的分析题集（tests/fixtures/eval/analysis_suite.json）：在虚构库 retail_trends（按今天生成）上导入，每道题都试算
通过，分析期望都算得出来、判定和库里埋的效应一致（导入走接口：按数据源名对上、逐题过校验、试算一次）。今天是周一时本周还
没有数据，「这周和上周比」那一题的期望算不了（照实记，这一项不能评分），导入照样成功。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.runner import run_manager
from app.eval import judgements
from app.main import app
from tests import eval_samples as es
from tests.fixtures.acceptance import retail_trends

SUITE_FILE = Path(__file__).parent / "fixtures" / "eval" / "analysis_suite.json"
ACTOR = {"X-Actor": quote("评测员甲")}
#: 每道题的分析期望该算出的判定类（库里埋的效应，见 retail_trends 的说明）
WANTED = {1: "source", 2: "mix", 3: "normal", 4: "anomaly", 5: "rising", 6: "source", 7: "no_evidence",
          8: "incomparable"}


@pytest.fixture(autouse=True)
async def engine_up():
    await run_manager.setup()
    yield
    await es.drop_suites()
    await run_manager.shutdown()


async def test_the_analysis_suite_imports_and_every_expectation_computes(tmp_path, scratch_source):
    content = json.loads(SUITE_FILE.read_text(encoding="utf-8"))
    # 「今天」按题集的时区取（题目里的期次按它算），不看本机时区：本机在别的时区时，跨日那几个钟头两边的今天不是同一天
    today = datetime.now(ZoneInfo(content["suite"]["timezone"])).date()
    path = retail_trends.build(tmp_path / "trends.db", today)
    sid = await scratch_source(retail_trends.SOURCE_NAME, path, schema=True)
    assert {c["seq"] for c in content["cases"]} == set(WANTED)
    assert all(c["gold"]["datasource"] == retail_trends.SOURCE_NAME for c in content["cases"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/evals/suites/import", json={"content": content}, headers=ACTOR)
        assert res.status_code == 201, res.text
        assert res.json()["imported"] == len(WANTED) and res.json()["skipped"] == []
        suite = res.json()["suite"]
        assert all(c["gold"]["datasource_id"] == sid for c in suite["cases"])
        monday = today.weekday() == 0
        for case in suite["cases"]:
            assert case["trial"]["status"] == "ok", (case["seq"], case["trial"])
            trial = (await client.post(f"/api/evals/suites/{suite['id']}/cases/{case['id']}/trial")).json()["trial"]
            assert trial["status"] == "ok", (case["seq"], trial["reason"])
            [values] = [s["values"] for s in trial["sets"]]
            found = [v for v in values.values() if v["kind"] in judgements.ANALYSIS_KINDS]
            assert len(found) == 1, (case["seq"], values)
            if monday and case["seq"] == 8:
                assert found[0]["status"] == "refused", found[0]
                continue
            assert found[0]["status"] == "ok" and found[0]["judgement"] == WANTED[case["seq"]], (case["seq"], found[0])
            # 期望值里没有人填的数：来源、判定都是算子在期望 SQL 上算的，判定语由系统渲染
            assert found[0]["phrase"] and found[0]["result"]["inputs"][0]["ref"] == judgements.GOLD_REF
            for exp in case["expectations"]:
                assert not set(exp) & set(judgements.JUDGEMENTS) and "subjects" not in exp
