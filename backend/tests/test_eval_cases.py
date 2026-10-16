"""题目的形状与校验（app/eval/cases.py、suites.py）：数值期望不许人填（评测-D4）、文字要点不计入准确率、只做单轮
（评测-D12）、变量只认定时任务那份、期望和期望来源配得上、题目指纹只看内容。纯函数，不碰库。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.eval import cases, suites
from app.eval.cases import CaseInvalid, normalize_case

SQL = {"kind": "sql", "datasource_id": "ds1", "sql": "SELECT SUM(amount) AS gmv FROM orders"}


def invalid(body: dict, **kw) -> CaseInvalid:
    with pytest.raises(CaseInvalid) as caught:
        normalize_case(body, **kw)
    return caught.value


@pytest.mark.parametrize("key", cases.HUMAN_VALUE_KEYS)
def test_numbers_cannot_be_filled_in_by_hand(key):
    e = invalid({"question": "q", "gold": SQL, "expectations": [{"kind": "scalar", "column": "gmv", key: 330}]})
    assert "数值期望不许人填" in e.message and e.field == f"expectations.0.{key}"


def test_text_points_never_count_toward_accuracy():
    fields = normalize_case({"question": "为什么涨了？", "expectations": [
        {"kind": "text", "points": [" 华南门店多 ", "客单价高"], "required": True}]})
    [exp] = fields["expectations"]
    assert exp == {"id": "e1", "kind": "text", "required": False, "points": ["华南门店多", "客单价高"]}
    assert fields["gold"] is None


def test_only_single_turn_cases_this_round():
    assert invalid({"question": "q", "history": [{"q": "上一轮"}], "expectations": [{"kind": "ask"}]}).field == "history"
    assert normalize_case({"question": "q", "history": [], "expectations": [{"kind": "ask"}]})["history"] is None


@pytest.mark.parametrize("body, field", [
    ({"question": "q", "expectations": [{"kind": "scalar", "column": "gmv"}]}, "gold"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "ask"}]}, "gold"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "ask"}, {"kind": "none"}]}, "expectations"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "scalar", "metric": "gmv"}]}, "expectations.0.metric"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "scalar"}]}, "expectations.0.column"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "scalar", "column": "g", "row": 1, "where": {"a": 1}}]},
     "expectations.0.row"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "table"}]}, "expectations.0.keys"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "scalar", "column": "g", "colum": "x"}]},
     "expectations.0.colum"),
    ({"question": "q", "gold": SQL, "expectations": [{"kind": "scalar", "column": "g", "tolerance": 2}]},
     "expectations.0.tolerance"),
    ({"question": "q", "gold": SQL, "expectations": [{"id": "a", "kind": "none"}, {"id": "a", "kind": "none"}]},
     "expectations.1.id"),
    ({"question": "", "expectations": [{"kind": "ask"}]}, "question"),
    ({"question": "q", "expectations": []}, "expectations"),
    ({"question": "q", "workflow_id": "w1", "expectations": [{"kind": "ask"}]}, "workflow_id"),
    ({"kind": "workflow", "workflow_id": "w1", "workflow_version": 1, "expectations": [{"kind": "ask"}]},
     "expectations"),
    ({"question": "上周 {{ schedule.lastweek }}", "expectations": [{"kind": "ask"}]}, "question"),
    ({"question": "q", "gold": {**SQL, "sql": "SELECT {{ schedule.today | shout }}"},
      "expectations": [{"kind": "none"}]}, "gold.sql"),
    ({"question": "q", "gold": {**SQL, "sql": "SELECT {{ schedule.today "}, "expectations": [{"kind": "none"}]},
     "gold.sql"),
    ({"question": "q", "gold": {"kind": "caliber", "workflow_id": "w", "version": 1, "node_id": "c",
                                "inputs": {"start": "{{ vars.x }}"}}, "expectations": [{"kind": "scalar",
                                                                                         "metric": "m"}]},
     "gold.inputs"),
    ({"question": "q", "gold": {"kind": "caliber", "workflow_id": "w", "version": 1, "node_id": "c"},
      "expectations": [{"kind": "table", "keys": ["a"]}]}, "expectations.0.kind"),
    ({"question": "q", "gold": {"kind": "magic"}, "expectations": [{"kind": "none"}]}, "gold.kind"),
])
def test_expectations_and_gold_must_fit_together(body, field):
    assert invalid(body).field == field


def test_from_run_gold_is_only_built_by_the_server():
    gold = {"kind": "from_run", "datasource_id": "ds1", "sql": "SELECT 1 AS n", "run_id": "r1", "query": "a1",
            "confirmed_by": "甲", "confirmed_at": "2026-10-01T00:00:00+00:00"}
    assert invalid({"question": "q", "gold": gold, "expectations": [{"kind": "scalar", "column": "n"}]}).field \
        == "gold.kind"
    fields = normalize_case({"question": "q", "gold": gold, "expectations": [{"kind": "scalar", "column": "n"}]},
                            trusted_gold=True)
    # 原样带回来的可以过（改标签时整份 gold 跟着回来）
    again = normalize_case({"gold": gold, "tags": ["x"]}, current=fields)
    assert again["gold"] == fields["gold"] and again["tags"] == ["x"]


def test_the_case_hash_follows_content_not_records():
    base = normalize_case({"question": "q", "gold": SQL, "expectations": [{"kind": "scalar", "column": "gmv"}],
                           "tags": ["a"]})
    assert cases.case_hash({**base, "tags": ["b"], "enabled": False}) == cases.case_hash(base)
    assert cases.case_hash({**base, "question": "q2"}) != cases.case_hash(base)
    caliber = {"kind": "caliber", "workflow_id": "w", "version": 1, "node_id": "c", "inputs": {}}
    pinned = {**base, "gold": caliber}
    # 升版处置的记录不进指纹（dual 时旧的那一份照旧可比），改钉版本才进
    assert cases.case_hash({**pinned, "gold": {**caliber, "upgrade": {"policy": "dual", "to_version": 2}}}) == \
        cases.case_hash(pinned)
    assert cases.case_hash({**pinned, "gold": {**caliber, "version": 2}}) != cases.case_hash(pinned)


def test_the_disposal_record_is_kept_only_by_the_server():
    caliber = {"kind": "caliber", "workflow_id": "w", "version": 1, "node_id": "c", "inputs": {}}
    stored = {**caliber, "upgrade": {"policy": "dual", "from_version": 1, "to_version": 2}}
    body = {"question": "q", "expectations": [{"kind": "scalar", "metric": "m"}]}
    kept = normalize_case({**body, "gold": {**caliber, "upgrade": {"policy": "recompute", "to_version": 9}}},
                          current={"gold": stored})
    assert kept["gold"]["upgrade"] == stored["upgrade"], "客户端带来的不算，照库里的留着"
    moved = normalize_case({**body, "gold": {**caliber, "node_id": "other"}}, current={"gold": stored})
    assert "upgrade" not in moved["gold"], "换了卡就没有处置"
    fresh = normalize_case({**body, "gold": {**caliber, "upgrade": {"policy": "dual"}}})
    assert "upgrade" not in fresh["gold"]


def test_variables_render_at_the_as_of_moment():
    as_of = datetime(2027, 1, 1, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    values = cases.variable_values(as_of, "Asia/Shanghai")
    assert values["last_week"] == "2026-W52" and values["yesterday"] == "2026-12-31"
    assert cases.render("'{{ schedule.last_week_start }}'..'{{ schedule.last_week_end }}'", values) == \
        "'2026-12-21'..'2026-12-27'"


def test_suite_budget_defaults_and_ranges():
    assert suites.effective_budget({}) == {"max_parallel": 2, "per_case_timeout_s": 900, "per_run_max_usd": 2.0,
                                           "daily_max_usd": 5.0}
    assert suites.normalize_budget({"max_parallel": 2, "per_run_max_usd": 1}) == {"per_run_max_usd": 1.0}
    with pytest.raises(CaseInvalid) as caught:
        suites.normalize_budget({"max_parallel": 1.5})
    assert caught.value.field == "budget.max_parallel" and caught.value.code == suites.SUITE_INVALID
