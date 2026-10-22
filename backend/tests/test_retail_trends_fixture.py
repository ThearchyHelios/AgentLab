"""分析题用的虚构零售库（tests/fixtures/acceptance/retail_trends.py）：同一个「今天」生成的库一样；埋的八个效应在题集
（tests/fixtures/eval/analysis_suite.json）的期望 SQL 上用平台的算子（app.analysis.run，缺省阈值）算出来真的成立，换几个
「今天」也成立（星期几不同、跨年）。表的日期范围按库里的实际最小、最大值现查（同探查器交回的形状）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import app.analysis as analysis
from app.analysis.contract import DEFAULTS
from app.eval import cases, judgements
from tests.fixtures.acceptance import retail_trends

SUITE = json.loads((Path(__file__).parent / "fixtures" / "eval" / "analysis_suite.json").read_text(encoding="utf-8"))
#: 几个不同的「今天」：周三、周日、周六，和跨年的那一周
TODAYS = [date(2026, 10, 21), date(2026, 10, 25), date(2026, 7, 4), date(2027, 1, 6)]


def _rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM orders ORDER BY id").fetchall()


async def _judge(path: Path, today: date) -> dict[int, dict]:
    """题集每道题的分析期望在这个库上算出来的期望值：{题号: judgements.expected}。"""
    db = sqlite3.connect(path)
    as_of = datetime(today.year, today.month, today.day, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    variables = cases.variable_values(as_of, "Asia/Shanghai")

    async def probe(source: str, table: str, column: str) -> dict:
        lo, hi = db.execute(f"SELECT MIN({column}), MAX({column}) FROM {table}").fetchone()
        return {"source": source, "table": table, "column": column, "min": lo, "max": hi, "basis": "live",
                "at": as_of.isoformat()}

    out: dict[int, dict] = {}
    for raw in SUITE["cases"]:
        fields = cases.normalize_case({"question": raw["question"], "expectations": raw["expectations"],
                                       "gold": {"kind": "sql", "datasource_id": "x", "sql": raw["gold"]["sql"]}})
        sql = cases.render(raw["gold"]["sql"], variables)
        cursor = db.execute(sql)
        snapshot = {"columns": [d[0] for d in cursor.description], "rows": [list(r) for r in cursor.fetchall()],
                    "sql": sql, "source": retail_trends.SOURCE_NAME, "dialect": "sqlite", "masked": []}
        [exp] = [e for e in fields["expectations"] if e["kind"] in judgements.ANALYSIS_KINDS]
        request = judgements.request_of(exp, variables, snapshot)
        result = await analysis.run(request, lambda name: snapshot if name == judgements.GOLD_REF else None,
                                    settings=DEFAULTS, context={"today": today, "probe": probe}, refuse_data_errors=True)
        out[raw["seq"]] = judgements.expected(exp, result)
    db.close()
    return out


def _caveats(value: dict) -> set[str]:
    return {c["code"] for c in value["result"].get("caveats") or []}


def test_the_same_day_builds_the_same_database(tmp_path):
    a = retail_trends.build(tmp_path / "a.db", TODAYS[0])
    b = retail_trends.build(tmp_path / "b.db", TODAYS[0])
    assert a.read_bytes() == b.read_bytes() and len(_rows(a)) > 20_000
    with sqlite3.connect(a) as db:
        first, last = db.execute("SELECT MIN(order_date), MAX(order_date) FROM orders").fetchone()
        stores = db.execute("SELECT COUNT(*) FROM stores WHERE region = '西南'").fetchone()[0]
    assert (date.fromisoformat(last) - TODAYS[0]).days == -1, "截止昨天"
    assert (TODAYS[0] - date.fromisoformat(first)).days == retail_trends.DAYS and stores == 1


@pytest.mark.parametrize("today", TODAYS, ids=[d.isoformat() for d in TODAYS])
async def test_the_effects_hold_on_the_suite_sql(tmp_path, today):
    found = await _judge(retail_trends.build(tmp_path / "trends.db", today), today)
    assert all(v["status"] == "ok" for v in found.values()), {k: v.get("reason") for k, v in found.items()}
    # 1 上周销售额下降，主要来自华东
    assert (found[1]["judgement"], found[1]["subjects"], found[1]["direction"]) == ("source", ["华东"], "down")
    # 2 退款率上升来自结构变化，各渠道自身的退款率都在降（辛普森）
    assert (found[2]["judgement"], found[2]["direction"]) == ("mix", "up") and "simpson" in _caveats(found[2])
    rows = found[2]["result"]["table"]["rows"]
    columns = found[2]["result"]["table"]["columns"]
    r_base, r_target = columns.index("r_base"), columns.index("r_target")
    assert all(float(r[r_target]) < float(r[r_base]) for r in rows), rows
    # 3 订单数在常规波动范围内（不该叫异常）
    assert found[3]["judgement"] == "normal" and found[3]["result"]["verdict"]["code"] == "within_band"
    # 4 取消单明显超出常规波动，往上
    assert (found[4]["judgement"], found[4]["direction"], found[4]["verdict"]) == ("anomaly", "up", "far_outside_band")
    # 5 线上销售额呈上升趋势，末尾连涨十周以上
    values = found[5]["result"]["values"]
    assert found[5]["judgement"] == "rising" and int(values["run_length"]) >= 10 and values["run_direction"] == "up"
    # 6 线上销售额的变化主要来自订单数，不是客单价
    assert (found[6]["judgement"], found[6]["subjects"], found[6]["direction"]) == ("source", ["订单数"], "up")
    # 7 西南（一家小店）看着比华东高，但样本太少，差异不足以下结论
    assert found[7]["judgement"] == "no_evidence" and found[7]["subjects"] == ["西南", "华东"]
    a_value, b_value = (float(found[7]["result"]["values"][k]) for k in ("a", "b"))
    assert a_value > b_value and "small_sample" in _caveats(found[7])
    # 8 本周还没过完：和上周不可比
    assert found[8]["judgement"] == "incomparable" and "还差" in found[8]["phrase"]


async def test_on_a_monday_this_week_has_no_data_yet(tmp_path):
    monday = date(2026, 10, 26)
    found = await _judge(retail_trends.build(tmp_path / "trends.db", monday), monday)
    assert found[8]["status"] == "refused" and found[8]["reason_code"] == "input_unavailable", found[8]
    assert all(found[seq]["status"] == "ok" for seq in range(1, 8)), "别的效应照样成立"
