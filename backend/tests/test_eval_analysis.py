"""分析期望（贡献、判定）的规矩和评分（app/eval/judgements.py、cases.py、score.py；docs/design/analysis-round/00-总方案.md
§3.8）。纯计算：期望的分析结论用 app/analysis/testing 的假结论（形状同真算子），答案直接构造。走真运行的在
tests/test_eval_analysis_runs.py。地区、门店、数字都是虚构的。

- 校验：贡献恒为变化拆解、要选模式；判定是另外四种分析；参数只能写分析工具的那几个、不能写阈值、不能写输入；来源、方向、
  判定不许人填；文字参数里的变量只认定时任务那份；口径题配不了分析期望。
- 期望值：判定码归到判定类，主要来源按 top_n 取前几段；比较作答前后两次期望时不看运行当天、现查时刻、输入快照的工件。
- 评分五种结论：引用了判定对得上的结论 → 对且可追溯；正文按说法表写对了、没引用 → 对但没引用；引用的判定对不上、正文
  写反了（常规波动说成异常、方向反了、来源说成别的段）、根本没做也没说 → 错；期望算不了 → 不能评分。
- 可比性不进说法表：期望不可比、正文明确说了两期不能直接比的（评分自己的小词表）算对但没引用；假设、疑问、否定的不算；
  期望可比的只能靠引用。
"""
from __future__ import annotations

import pytest

import app.db.models  # noqa: F401 - conftest 按已导入的模型建表：只跑这一个文件时也要有表
from app.analysis.testing import fake_result
from app.eval import cases, judgements, stats
from app.eval.cases import CaseInvalid, normalize_case
from app.eval.score import Answer, score_case

SQL = {"kind": "sql", "datasource_id": "ds1", "sql": "SELECT region AS 地区, week AS 周, SUM(amount) AS 销售额 FROM orders"}
SOURCE = {"id": "e1", "kind": "contribution", "required": True, "op": "decompose", "mode": "additive",
          "params": {"by": ["地区"], "period": "周", "measure": "销售额"}, "top_n": 1}
BAND = {"id": "e1", "kind": "verdict", "required": True, "op": "baseline",
        "params": {"period": "周", "measure": "订单数"}}


def invalid(expectation: dict, **gold) -> CaseInvalid:
    with pytest.raises(CaseInvalid) as caught:
        normalize_case({"question": "上周销售额为什么下降？", "gold": gold or SQL, "expectations": [expectation]})
    return caught.value


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def test_the_two_kinds_are_normalized():
    fields = normalize_case({"question": "上周销售额为什么下降？", "gold": SQL, "expectations": [
        {"kind": "contribution", "mode": "additive", "params": {"by": ["地区"], "period": "周", "measure": "销售额",
                                                                  "label": "", "base": None}},
        {"kind": "verdict", "op": "comparable", "params": {
            "period": "周", "base": "{{ schedule.last_week_start }}", "target": "{{ schedule.this_week_start }}"}}]})
    contribution, verdict = fields["expectations"]
    assert contribution == {"id": "e1", "kind": "contribution", "required": True, "op": "decompose", "mode": "additive",
                            "params": {"by": ["地区"], "period": "周", "measure": "销售额"}, "top_n": 1}
    assert verdict["op"] == "comparable" and "mode" not in verdict and verdict["params"]["base"].startswith("{{")
    assert cases.case_hash(fields) == cases.case_hash(dict(fields)), "指纹照常按内容算"


@pytest.mark.parametrize("expectation, field, words", [
    ({"kind": "contribution", "op": "baseline", "params": {}}, "expectations.0.op", "只能是变化拆解"),
    ({"kind": "contribution", "params": {"by": ["地区"]}}, "expectations.0.mode", "要选一种模式"),
    ({"kind": "verdict", "params": {"period": "周", "measure": "订单数"}}, "expectations.0.op", "波动基线、趋势判断"),
    ({"kind": "verdict", "op": "decompose", "mode": "additive"}, "expectations.0.op", "当前为「decompose」"),
    ({"kind": "verdict", "op": "trend", "mode": "additive", "params": {"period": "周", "measure": "m"}},
     "expectations.0.mode", "没有模式可选"),
    ({"kind": "verdict", "op": "differ", "params": {}}, "expectations.0.mode", "差异检验（比率）"),
    ({"kind": "verdict", "op": "baseline", "params": {"period": "周"}}, "expectations.0.params.measure", "没写"),
    ({**SOURCE, "params": {**SOURCE["params"], "main_share": 0.3}}, "expectations.0.params.main_share",
     "阈值不写在期望里"),
    ({**SOURCE, "params": {**SOURCE["params"], "query": "Q1"}}, "expectations.0.params.query", "没有「query」"),
    ({**SOURCE, "params": {**SOURCE["params"], "headline": {"base": "Q1.r0.x", "target": "Q1.r1.x"}}},
     "expectations.0.params.headline", "没有「headline」"),
    ({**SOURCE, "params": {**SOURCE["params"], "base": "{{ schedule.nope }}"}}, "expectations.0.params.base",
     "不是可用的变量"),
    ({**SOURCE, "top_n": 0}, "expectations.0.top_n", "1 到 10"),
    ({**SOURCE, "params": "地区"}, "expectations.0.params", "需要是一个对象"),
])
def test_wrong_analysis_expectations_are_refused(expectation, field, words):
    e = invalid(expectation)
    assert e.field == field and words in e.message, (e.field, e.message)


@pytest.mark.parametrize("key", ["segment", "subjects", "direction", "verdict", "judgement", "driver", "value"])
def test_conclusions_cannot_be_filled_in_by_hand(key):
    e = invalid({**SOURCE, key: "华东"})
    assert e.field == f"expectations.0.{key}" and "不许人填" in e.message and "算出来" in e.message


def test_a_caliber_gold_cannot_carry_an_analysis_expectation():
    caliber = {"kind": "caliber", "workflow_id": "w", "version": 1, "node_id": "c", "inputs": {}}
    assert invalid(BAND, **caliber).field == "expectations.0.kind"
    # 期望反问的题同样不能带分析期望：问法含糊就不该下判断
    with pytest.raises(CaseInvalid):
        normalize_case({"question": "最近怎么样？", "gold": SQL, "expectations": [{"kind": "ask"}, BAND]})


def test_the_request_renders_variables_and_fills_two_periods():
    snapshot = {"columns": ["地区", "周", "销售额"], "sql": SQL["sql"],
                "rows": [["华东", "2026-10-12", 900], ["华东", "2026-10-05", 1000], ["华南", "2026-10-05", 800]]}
    values = {"last_week_start": "2026-10-12", "this_week_start": "2026-10-19"}
    request = judgements.request_of(SOURCE, values, snapshot)
    assert request["op"] == "decompose" and request["mode"] == "additive"
    assert request["params"] == {"query": judgements.GOLD_REF, "by": ["地区"], "period": "周",
                                 "base": "2026-10-05", "target": "2026-10-12", "measure": "销售额"}
    explicit = {**SOURCE, "params": {**SOURCE["params"], "target": "{{ schedule.last_week_start }}",
                                     "base": "2026-10-05"}}
    assert judgements.request_of(explicit, values, snapshot)["params"]["target"] == "2026-10-12"
    three = {**snapshot, "rows": [*snapshot["rows"], ["华南", "2026-09-28", 700]]}
    with pytest.raises(judgements.ParamsInvalid) as caught:
        judgements.request_of(SOURCE, values, three)
    assert "有 3 期" in caught.value.message and "只能返回两期" in caught.value.message


# --------------------------------------------------------------------------
# 期望值、比较用的那一份
# --------------------------------------------------------------------------


def _gold(*pairs: tuple[dict, dict]) -> dict:
    values = {exp["id"]: judgements.expected(exp, result) for exp, result in pairs}
    sets = [{"run_id": "g", "snapshot": "x", "version": None, "values": values}]
    return {"status": "ok", "reason": None, "code": None, "before": {"status": "ok", "sets": sets},
            "after": {"status": "ok", "sets": sets}}


def _case(*expectations: dict) -> dict:
    return {"kind": "ask", "question": "虚构问题", "expectations": list(expectations), "gold": SQL}


def test_expected_values_follow_the_judgement_classes():
    driver = judgements.expected(SOURCE, fake_result())
    assert (driver["judgement"], driver["subjects"], driver["direction"]) == ("source", ["华东"], "down")
    assert driver["phrase"].startswith("销售额下降") and driver["result"]["op"] == "decompose"
    two = judgements.expected({**SOURCE, "top_n": 2}, fake_result(verdict="concentrated", subjects=[["华东"], ["华北"]]))
    assert two["judgement"] == "source" and two["subjects"] == ["华东", "华北"]
    assert judgements.expected(SOURCE, fake_result(verdict="concentrated", subjects=[["华东"], ["华北"]])
                               )["subjects"] == ["华东"], "top_n 缺省 1：只要点名第一段"
    for code, judgement in (("within_band", "normal"), ("outside_band", "anomaly"), ("far_outside_band", "anomaly")):
        assert judgements.expected(BAND, fake_result("baseline", None, code))["judgement"] == judgement
    for code, judgement in (("comparable", "comparable"), ("conditional", "comparable"),
                            ("incomparable", "incomparable")):
        exp = {**BAND, "op": "comparable"}
        assert judgements.expected(exp, fake_result("comparable", None, code))["judgement"] == judgement
    differ = judgements.expected({**BAND, "op": "differ", "mode": "rate"},
                                 fake_result("differ", "rate", "no_evidence", subjects=[["西南"], ["华东"]],
                                             direction=None))
    assert differ["judgement"] == "no_evidence" and differ["subjects"] == ["西南", "华东"]
    refused = judgements.expected(BAND, fake_result("baseline", None, None, status="refused",
                                                    reason_code="history_short"))
    assert refused["status"] == "refused" and refused["reason"] == "历史只有 5 期，至少要 8 期"
    assert set(judgements.JUDGEMENTS) >= {v for m in judgements._JUDGEMENT_OF.values() for v in m.values()}


def test_comparing_two_golds_ignores_the_day_the_probe_time_and_the_snapshot_artifact():
    from app.eval import gold as gold_rules

    def outcome(day: str, at: str, artifact: str) -> gold_rules.GoldOutcome:
        result = fake_result("baseline", None, "within_band")
        result["params"]["today"] = day
        result["probes"] = [{"source": "shop", "table": "orders", "column": "order_date", "min": "2026-04-01",
                             "max": "2026-10-20", "basis": "live", "at": at}]
        result["inputs"] = [{"ref": judgements.GOLD_REF, "artifact": artifact, "role": "data", "columns": ["周"],
                             "rows": "all"}]
        values = {"e1": judgements.expected(BAND, result), "e2": {"kind": "scalar", "value": 330, "column": "gmv"}}
        return gold_rules.GoldOutcome(status="ok", as_of="", sets=[gold_rules.GoldSet("r", "s", values)])

    before = outcome("2026-10-21", "2026-10-21T09:00:01", "a" * 64)
    after = outcome("2026-10-22", "2026-10-22T00:00:03", "b" * 64)
    assert gold_rules.settle(before, after).status == "ok"
    from app.eval.runner import gold_key

    assert gold_key({"status": "ok", "before": before.to_dict()}) == gold_key({"status": "ok", "before": after.to_dict()})
    changed = outcome("2026-10-21", "x", "a" * 64)
    changed.sets[0].values["e1"]["result"]["probes"][0]["max"] = "2026-10-19"
    assert gold_rules.settle(before, changed).status == "drifted", "表的范围变了才算数据变了"
    # 别的期望的指纹和以前一样（老结果和新结果比「数据变了」不受影响）
    from app.core.artifact_store import canonical_json

    plain = gold_rules.GoldOutcome(status="ok", as_of="", sets=[gold_rules.GoldSet("r", "s", {"e2": {"value": 1}})])
    assert gold_rules._fingerprint(plain) == canonical_json([[None, {"e2": {"value": 1}}]])


# --------------------------------------------------------------------------
# 评分
# --------------------------------------------------------------------------


def _record(result: dict, alias: str | None = "A1", cited: tuple[str, ...] = ("phrase",)) -> dict:
    record = judgements.record_of(result, artifact="c" * 64)
    record.update(alias=alias, cited=list(cited))
    return record


def _answer(*records: dict, text: str = "", cited_in: tuple[str, ...] = ()) -> Answer:
    sentences = judgements.sentences_of_text(text)
    for s in sentences:
        s["analyses"] = list(cited_in)
    return Answer(run_id="r", text=text, analyses=list(records), sentences=sentences)


def _score(exp: dict, result: dict, answer: Answer) -> dict:
    graded = score_case(_case(exp), _gold((exp, result)), answer, path="ran")
    [item] = graded.scores
    return item


def test_a_cited_conclusion_that_agrees_is_traced():
    item = _score(SOURCE, fake_result(), _answer(_record(fake_result())))
    assert item["outcome"] == "traced" and item["found"]["alias"] == "A1", item
    assert item["want"] == {"judgement": "source", "subjects": ["华东"], "direction": "down", "status": "ok",
                            "phrase": fake_result()["phrase"], "method": "变化拆解（可加）"}
    # 主要来源点名了期望的那一段就行：答案的拆解判成集中在华东、华北也对得上
    wider = fake_result(verdict="concentrated", subjects=[["华东"], ["华北"]])
    assert _score(SOURCE, fake_result(), _answer(_record(wider)))["outcome"] == "traced"


def test_the_text_right_without_a_citation_is_untraced():
    item = _score(SOURCE, fake_result(), _answer(text="上周销售额下降主要来自华东。"))
    assert item["outcome"] == "untraced" and item["found"]["text"] == "上周销售额下降主要来自华东", item
    normal = _score(BAND, fake_result("baseline", None, "within_band"),
                    _answer(text="订单数的下降在常规波动范围内，不算异常。"))
    assert normal["outcome"] == "untraced", normal
    rising = _score({**BAND, "op": "trend"}, fake_result("trend", None, "rising", direction="up"),
                    _answer(text="线上销售额持续上升。"))
    assert rising["outcome"] == "untraced", rising


@pytest.mark.parametrize("text, words", [
    ("订单数的下降属于异常。", "判定是"),                      # 常规波动说成异常
    ("上周销售额下降主要来自华南。", "不是华南"),                # 主要来源说成别的段
    ("上周销售额上升主要来自华东。", "涨跌写反了"),              # 方向反了
    ("线上销售额呈下降趋势。", "相反"),                          # 趋势方向反了
])
def test_saying_the_opposite_is_wrong(text, words):
    exp, result = {
        "订单数": (BAND, fake_result("baseline", None, "within_band")),
        "销售额": (SOURCE, fake_result()),
        "线上": ({**BAND, "op": "trend"}, fake_result("trend", None, "rising", direction="up")),
    }[next(k for k in ("订单数", "线上", "销售额") if k in text)]
    item = _score(exp, result, _answer(text=text))
    assert item["outcome"] == "wrong" and words in item["reason"], item


def test_a_cited_conclusion_that_disagrees_is_wrong():
    anomaly = fake_result("baseline", None, "outside_band", direction="down")
    item = _score(BAND, fake_result("baseline", None, "within_band"), _answer(_record(anomaly)))
    assert item["outcome"] == "wrong" and "A1" in item["reason"] and "不是一类" in item["reason"], item
    assert item["found"]["judgement"] == "anomaly"
    # 差异检验比的是别的两组：对不上
    other = fake_result("differ", "rate", "no_evidence", subjects=[["华北"], ["华东"]], direction=None)
    want = fake_result("differ", "rate", "no_evidence", subjects=[["西南"], ["华东"]], direction=None)
    item = _score({**BAND, "op": "differ", "mode": "rate"}, want, _answer(_record(other)))
    assert item["outcome"] == "wrong" and "期望比的是西南和华东" in item["reason"], item


def test_citing_the_right_conclusion_but_writing_the_opposite_is_wrong():
    normal = fake_result("baseline", None, "within_band")
    item = _score(BAND, normal, _answer(_record(normal, cited=("see",)), text="订单数下降属于异常。",
                                        cited_in=("A1",)))
    assert item["outcome"] == "wrong" and "引用了 A1" in item["reason"], item


def test_doing_nothing_and_saying_nothing_is_wrong():
    item = _score(BAND, fake_result("baseline", None, "within_band"), _answer(text="上周订单数是 1,203 单。"))
    assert item["outcome"] == "wrong" and "没有做波动基线" in item["reason"], item
    uncited = _score(BAND, fake_result("baseline", None, "within_band"),
                     _answer(_record(fake_result("baseline", None, "within_band"), cited=()), text="上周订单数是 1,203 单。"))
    assert uncited["outcome"] == "wrong" and "但没有引用" in uncited["reason"], uncited


def test_saying_the_two_periods_cannot_be_compared_directly_is_untraced():
    """可比性不进说法表（说法核对不打回谨慎的提醒），评分另认：期望不可比、正文明确说了两期不能直接比（不完整、还没过完、
    只有几天、天数不同、口径不同、不能直接比……）的算对但没引用；假设、疑问、否定、「没法比」（差得远）不算；期望可比的
    正文怎么写都不算，只能靠引用。"""
    from app.engine import analysis_claims

    exp = {**BAND, "op": "comparable"}
    incomparable = fake_result("comparable", None, "incomparable")
    for text, said in (("本周还没过完，两周不能直接比。", "本周还没过完"), ("本周数据不完整，不能直接比。", "本周数据不完整"),
                       ("两周的天数不同（7 天 vs 3 天），不宜直接比较。", "两周的天数不同（7 天 vs 3 天）"),
                       ("统计口径有调整，前后两期不可比。", "统计口径有调整"),
                       ("这周才过了三天，放在一起看会失真。", "这周才过了三天"),
                       ("不能直接拿本周和上周比较。", "不能直接拿本周和上周比较")):
        item = _score(exp, incomparable, _answer(text=text))
        assert item["outcome"] == "untraced", (text, item)
        assert item["found"] == {"judgement": "incomparable", "text": said}, item
        assert analysis_claims.scan(text) == [], "说法表照旧不认这类说法：说法核对不会打回"
    for text in ("如果口径不同，就不能直接比。", "是不是不能直接比？", "不是不能比，只是要按日均看。", "这周跟上周没法比，差远了。",
                 "两周都过完了，可以直接比。", "订单信息不完整的有 3 单。"):
        item = _score(exp, incomparable, _answer(text=text))
        assert item["outcome"] == "wrong" and "没有做可比性检查" in item["reason"], (text, item)
    # 只有说了这类话的句子留下来（和说法表认出判断词的句子一起）
    assert [x["text"] for x in judgements.sentences_of_text("上周订单数是 1,203 单。本周还没过完，两周不能直接比。")] == \
        ["本周还没过完，两周不能直接比。"]
    # 期望可比：正文怎么写都不算，只能靠引用
    comparable = fake_result("comparable", None, "comparable")
    for text in ("两周都是完整的 7 天，可以直接比。", "本周还没过完，两周不能直接比。"):
        assert _score(exp, comparable, _answer(text=text))["outcome"] == "wrong", text
    assert _score(exp, comparable, _answer(_record(comparable)))["outcome"] == "traced"
    assert _score(exp, incomparable, _answer(_record(incomparable)))["outcome"] == "traced"


def test_an_analysis_that_cannot_be_computed_cannot_be_graded():
    refused = fake_result("baseline", None, None, status="refused", reason_code="history_short")
    graded = score_case(_case(BAND, {"id": "e2", "kind": "scalar", "column": "gmv", "required": True}),
                        {**_gold((BAND, refused)), "before": {"status": "ok", "sets": [{"values": {
                            "e1": judgements.expected(BAND, refused),
                            "e2": {"kind": "scalar", "value": 330.0, "column": "gmv", "row": 0, "dims": {}}}}]}},
                        Answer(run_id="r", values=[{"value": 330.0, "kind": "cell", "artifact": "a", "column": "gmv",
                                                    "dims": {}}]), path="ran")
    band, sales = graded.scores
    assert band["outcome"] == "ungradable" and band["reason"] == "期望算不了：历史只有 5 期，至少要 8 期"
    assert sales["outcome"] == "traced", "同一题别的期望照常评"
    assert graded.verdict == "ungradable"
    # 答案里同类的分析做成了算不了：不算答对
    item = _score(BAND, fake_result("baseline", None, "within_band"), _answer(_record(refused)))
    assert item["outcome"] == "wrong" and "没有做成" in item["reason"], item


def test_top_n_wants_every_named_source():
    want = fake_result(verdict="concentrated", subjects=[["华东"], ["华北"]])
    exp = {**SOURCE, "top_n": 2}
    only_one = _score(exp, want, _answer(text="上周销售额下降集中在华东。"))
    assert only_one["outcome"] == "wrong" and "还有华北" in only_one["reason"], only_one
    both = _score(exp, want, _answer(text="上周销售额下降集中在华东、华北。"))
    assert both["outcome"] == "untraced", both


def test_old_answers_without_analyses_read_as_empty():
    old = Answer.from_dict({"run_id": "r", "text": "上周销售额 330。", "values": [], "bare": []})
    assert old.analyses == [] and old.sentences == []
    item = _score(SOURCE, fake_result(), old)
    assert item["outcome"] == "wrong", item
    again = Answer.from_dict(_answer(_record(fake_result())).to_dict())
    assert again.analyses[0]["alias"] == "A1"


def test_the_summary_counts_outcomes_by_kind():
    rows = [{"status": "ok", "verdict": "pass", "case_id": "c1", "scores": [
        {"kind": "contribution", "required": True, "outcome": "traced"},
        {"kind": "scalar", "required": False, "outcome": "wrong"}]},
        {"status": "ok", "verdict": "fail", "case_id": "c2", "scores": [
            {"kind": "verdict", "required": True, "outcome": "wrong"},
            {"kind": "scalar", "required": True, "outcome": "untraced"},
            {"kind": "text", "required": False, "outcome": "covered"}]}]
    summary = stats.summarize(rows)
    assert summary["outcomes_by_kind"] == {"contribution": {"traced": 1}, "verdict": {"wrong": 1},
                                           "scalar": {"untraced": 1}}
    assert summary["outcomes"] == {"traced": 1, "wrong": 1, "untraced": 1}


def test_e1_follows_the_analysis_inputs_down_the_drill():
    """逐题 E1 拿产出答案的查询：引用的是下钻那一层的分析结论时，顺着上一层的结论找到两层各自读的查询。"""
    from app.eval.score import _query_inputs

    found = {"a1": {"inputs": ["q1"]}, "a2": {"inputs": ["a1", "q2"]}}
    assert _query_inputs(found["a2"], found, {"q1": {}, "q2": {}}) == ["q1", "q2"]
    assert _query_inputs({"inputs": ["gone"]}, found, {"q1": {}}) == [], "取不回的输入不算"

