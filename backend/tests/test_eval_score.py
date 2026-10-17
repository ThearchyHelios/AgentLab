"""评分（app/eval/score.py；细案 01 做法 10，评测-D4）。纯计算的部分直接喂构造好的答案；读封存、逐题 E1、重评走一次真的
运行（tests/ledger_samples 的店铺库）。数字、问法、地区都是虚构的。

- scalar：对且可追溯（引用的格对上）、对但没引用（只有正文裸写的数对上，含叙述里自己算的比率）、错、量纲不同（差 10 的
  整数次幂）、答案引用遮罩格的不能评分；维度配对（华东的那一格才算）、小整数又没维度的标弱匹配；
- table：按键配对、要求顺序时核对顺序；set：名单齐不齐、顺序；none：没给数 + 有查询返回 0 行；ask：反问才对；text：单列不计；
- 题目结论：路径不对直接不通过；期望没算出来不能评分 / 升版待处置跳过；只有文字要点的不能评分；
- dual：新旧两版各给一份结论；
- 允许的相对误差和双盲复算是同一个函数（engine/tolerance）；
- 重评只读封存、不调模型。
"""
from __future__ import annotations

import pytest

import app.db.models  # noqa: F401 - conftest 按已导入的模型建表：只跑这一个文件时也要有表
from app.engine import tolerance
from app.eval.score import Answer, score_case


def _case(*expectations: dict, gold_kind: str | None = "sql") -> dict:
    return {"kind": "ask", "question": "虚构问题", "expectations": list(expectations),
            "gold": {"kind": gold_kind, "datasource_id": "s", "sql": "SELECT 1"} if gold_kind else None}


def _gold(values: dict, *more: dict) -> dict:
    sets = [{"run_id": "g", "snapshot": "x", "version": None, "values": values}]
    sets += [{"run_id": "g2", "snapshot": "y", "version": m.pop("_version", 2), "values": m} for m in more]
    return {"status": "ok", "reason": None, "code": None, "before": {"status": "ok", "sets": sets},
            "after": {"status": "ok", "sets": sets}}


def _scalar(value, **extra) -> dict:
    return {"kind": "scalar", "value": value, "column": "gmv", "row": 0, "dims": {}, **extra}


def _cell(value, **dims) -> dict:
    return {"value": value, "kind": "cell", "artifact": "a1", "row": 0, "column": "gmv", "dims": dims}


def _bare(text: str) -> list[dict]:
    from app.eval.score import _numbers

    return _numbers(text)


EXP = {"id": "e1", "kind": "scalar", "column": "gmv", "required": True}


def test_scalar_outcomes():
    case = _case(EXP)
    gold = _gold({"e1": _scalar(330.0)})
    traced = score_case(case, gold, Answer(run_id="r", values=[_cell(330.0)]), path="ran")
    assert traced.verdict == "pass" and traced.traced and traced.scores[0]["outcome"] == "traced"
    untraced = score_case(case, gold, Answer(run_id="r", text="销售额 330 元", bare=_bare("销售额 330 元")), path="ran")
    assert untraced.verdict == "pass" and not untraced.traced and untraced.scores[0]["outcome"] == "untraced"
    wrong = score_case(case, gold, Answer(run_id="r", values=[_cell(310.0)]), path="ran")
    assert wrong.verdict == "fail" and wrong.scores[0]["outcome"] == "wrong" and wrong.scores[0]["got"] == 310.0
    # 差 100 倍：量纲不同，单列，也不算对
    scale = score_case(case, gold, Answer(run_id="r", values=[_cell(3.3)]), path="ran")
    assert scale.verdict == "fail" and scale.scores[0]["outcome"] == "scale"
    masked = score_case(case, gold, Answer(run_id="r", values=[{**_cell(330.0), "masked": True}]), path="ran")
    assert masked.verdict == "ungradable" and "遮罩" in masked.scores[0]["reason"]


def test_bare_numbers_follow_their_written_precision_and_percent():
    case = _case(EXP)
    # 3.3 万写到千位：33,000 ± 500 都算对
    gold = _gold({"e1": _scalar(33280.0)})
    assert score_case(case, gold, Answer(run_id="r", bare=_bare("约 3.3 万")), path="ran").verdict == "pass"
    # 比率写成百分数
    ratio = _gold({"e1": _scalar(0.125)})
    got = score_case(case, ratio, Answer(run_id="r", bare=_bare("占比 12.5%")), path="ran")
    assert got.verdict == "pass" and got.scores[0]["outcome"] == "untraced", "叙述里写的比率只算对但没引用"


def test_dimensions_pair_and_small_integers_are_weak():
    case = _case({**EXP, "where": {"region": "华东"}})
    gold = _gold({"e1": _scalar(300.0, where={"region": "华东"}, dims={"region": "华东"})})
    other = score_case(case, gold, Answer(run_id="r", values=[_cell(300.0, region="华南")]), path="ran")
    assert other.verdict == "fail", "华南那一格碰巧也是 300，不算"
    same = score_case(case, gold, Answer(run_id="r", values=[_cell(300.0, 地区="华东")]), path="ran")
    assert same.verdict == "pass" and same.scores[0]["key"] == {"region": "华东"}
    # 按条件筛过、只剩一个数（没有维度列）不算对不上
    assert score_case(case, gold, Answer(run_id="r", values=[_cell(300.0)]), path="ran").verdict == "pass"
    weak = score_case(_case(EXP), _gold({"e1": _scalar(3)}), Answer(run_id="r", values=[_cell(3)]), path="ran")
    assert weak.verdict == "pass" and weak.scores[0]["weak"] is True


def test_tables_sets_none_and_ask():
    table = {"id": "t", "kind": "table", "keys": ["region"], "ordered": True, "required": True}
    gold = _gold({"t": {"kind": "table", "keys": ["region"], "columns": ["region", "gmv"], "ordered": True,
                        "rows": [{"region": "华东", "gmv": 300.0}, {"region": "华北", "gmv": 30.0}]}})
    cells = [_cell(300.0, region="华东"), _cell(30.0, region="华北")]
    ok = score_case(_case(table), gold, Answer(run_id="r", text="华东 300，华北 30", values=cells), path="ran")
    assert ok.verdict == "pass" and ok.traced
    flipped = score_case(_case(table), gold, Answer(run_id="r", text="华北 30，华东 300", values=cells), path="ran")
    assert flipped.verdict == "fail" and "先后" in flipped.scores[0]["reason"]

    names = {"id": "s", "kind": "set", "column": "region", "required": True}
    gold = _gold({"s": {"kind": "set", "column": "region", "values": ["华东", "华北"], "ordered": False}})
    assert score_case(_case(names), gold, Answer(run_id="r", text="有华东、华北两个"), path="ran").verdict == "pass"
    assert score_case(_case(names), gold, Answer(run_id="r", text="只有华东"), path="ran").verdict == "fail"

    none = {"id": "n", "kind": "none", "required": True}
    gold = _gold({"n": {"kind": "none", "rows": 0}})
    empty = [{"artifact": "q", "row_count": 0, "sql": "SELECT 1"}]
    traced = score_case(_case(none), gold, Answer(run_id="r", text="华中区没有数据", queries=empty), path="ran")
    assert traced.verdict == "pass" and traced.traced
    with_number = score_case(_case(none), gold, Answer(run_id="r", text="华中区 12 元", bare=_bare("华中区 12 元"),
                                                       queries=empty), path="ran")
    assert with_number.verdict == "fail"

    ask = _case({"id": "a", "kind": "ask", "required": True}, gold_kind=None)
    assert score_case(ask, {"status": "ok"}, Answer(run_id=None), path="asked").verdict == "pass"
    assert score_case(ask, {"status": "ok"}, Answer(run_id="r"), path="ran").verdict == "fail"


def test_a_wrong_answer_shows_the_paired_cell():
    """判错时「答案里的数」给评分时配对上的那一格（按列名、指标、维度配），不是答案引用的第一个值；配不上照实写配不上。"""
    case = _case({**EXP, "column": "refund", "label": "退款金额"})
    gold = _gold({"e1": _scalar(1234.5, column="refund")})

    def cell(value, column, **dims):
        return {"value": value, "kind": "cell", "artifact": "a1", "row": 0, "column": column, "dims": dims}

    question = cell("上周退款金额是多少", "问题")
    both = score_case(case, gold, Answer(run_id="r", values=[question, cell(36, "单数"), cell(1200.0, "refund")]),
                      path="ran")
    assert both.verdict == "fail" and both.scores[0]["got"] == 1200.0, both.scores
    assert both.scores[0]["cell"]["column"] == "refund"
    labelled = score_case(case, gold, Answer(run_id="r", values=[cell(36, "单数"), cell(1200.0, "退款金额")]), path="ran")
    assert labelled.scores[0]["got"] == 1200.0, "期望的说明（label）和答案的列名一样也算配上"
    lone = score_case(case, gold, Answer(run_id="r", values=[question, cell(36, "单数")]), path="ran")
    [s] = lone.scores
    assert s["outcome"] == "wrong" and "got" not in s, "单数不是退款金额：配不上就不给「答案里的数」"
    assert "配不上" in s["reason"] and "单数 36" in s["reason"] and "上周退款" not in s["reason"], s["reason"]
    # 按维度配：华东那一行只有一个数时就是它；同一行有几个数、列名又都配不上时不猜
    by_dims = _case({**EXP, "column": "refund", "where": {"region": "华东"}})
    gold = _gold({"e1": _scalar(1234.5, column="refund", where={"region": "华东"}, dims={"region": "华东"})})
    one = score_case(by_dims, gold, Answer(run_id="r", values=[cell(1200.0, "total", region="华东")]), path="ran")
    assert one.scores[0]["got"] == 1200.0, one.scores
    two = score_case(by_dims, gold, Answer(run_id="r", values=[cell(36, "单数", region="华东"),
                                                               cell(1200.0, "total", region="华东")]), path="ran")
    assert "got" not in two.scores[0] and "配不上" in two.scores[0]["reason"], two.scores
    text_only = score_case(case, _gold({"e1": _scalar(1234.5, column="refund")}), Answer(run_id="r", values=[question]),
                           path="ran")
    assert "got" not in text_only.scores[0] and text_only.scores[0]["reason"] == "答案里没有对得上的数"


def test_none_counts_only_numbers_as_giving_data():
    """期望「没有数据」：只有引用了数值格、或叙述里写了数，才算答案给了数。引用的是文字格（地区名）、空值格（NULL）的，
    答案明说没有数据就按没有数据判对。"""
    none = _case({"id": "n", "kind": "none", "required": True})
    gold = _gold({"n": {"kind": "none", "rows": 0}})
    empty = [{"artifact": "q", "row_count": 0, "sql": "SELECT 1"}]
    text_cell = {"value": "华中", "kind": "cell", "artifact": "q", "row": 0, "column": "region", "dims": {}}
    named = score_case(none, gold, Answer(run_id="r", text="华中区没有数据", values=[text_cell], queries=empty), path="ran")
    assert named.verdict == "pass" and named.scores[0]["outcome"] == "traced", named.scores
    null_cell = {"value": None, "kind": "cell", "artifact": "q", "row": 0, "column": "gmv", "dims": {}}
    null = score_case(none, gold, Answer(run_id="r", text="华中区的销售额为空，没有数据", values=[null_cell],
                                         queries=empty), path="ran")
    assert null.verdict == "pass", null.scores
    number = score_case(none, gold, Answer(run_id="r", text="华中区 12 元", values=[_cell(12.0)], queries=empty),
                        path="ran")
    assert number.verdict == "fail" and number.scores[0]["got"] == 12.0, number.scores
    said = score_case(none, gold, Answer(run_id="r", text="华中区 12 元", values=[text_cell], bare=_bare("12 元"),
                                         queries=empty), path="ran")
    assert said.verdict == "fail" and said.scores[0]["raw"] == "12", said.scores


def test_the_path_and_the_gold_decide_before_numbers():
    case = _case(EXP)
    gold = _gold({"e1": _scalar(330.0)})
    replied = score_case(case, gold, Answer(run_id=None, text="330", bare=_bare("330")), path="reply")
    assert replied.verdict == "fail" and "该查不查" in replied.reason, "直接回答的数是没查过的"
    skipped = score_case(case, {"status": "skipped", "reason": "口径已升版到 v2，待处置"}, Answer(run_id=None), path=None)
    assert skipped.verdict == "skipped" and "待处置" in skipped.reason
    drifted = score_case(case, {"status": "drifted", "reason": "作答期间数据变了"}, Answer(run_id="r"), path="ran")
    assert drifted.verdict == "ungradable", "数据变了不判，不算答错"
    rejected = score_case(case, gold, Answer(run_id="r"), path="rejected")
    assert rejected.verdict == "ungradable", "驳回是人的决定，不算失败"
    text_only = _case({"id": "p", "kind": "text", "points": ["说明口径"], "required": False}, gold_kind=None)
    judged = {"expectations": {"p": {"points": [{"point": 1, "verdict": "covered", "reason": ""}]}}}
    scored = score_case(text_only, {"status": "ok"}, Answer(run_id="r", text="按支付日"), path="ran", judgement=judged)
    assert scored.verdict == "ungradable" and scored.scores[0]["outcome"] == "covered", "文字要点单列、不计准确率"
    unjudged = score_case(text_only, {"status": "ok"}, Answer(run_id="r"), path="ran")
    assert unjudged.scores[0]["outcome"] == "unjudged"


def test_dual_reports_both_versions():
    case = _case(EXP, gold_kind="caliber")
    case["gold"] = {"kind": "caliber", "workflow_id": "w", "version": 1, "node_id": "card", "inputs": {}}
    case["expectations"] = [{"id": "e1", "kind": "scalar", "metric": "gmv", "required": True}]
    gold = _gold({"e1": _scalar(330.0)}, {"e1": _scalar(380.0), "_version": 2})
    got = score_case(case, gold, Answer(run_id="r", values=[_cell(330.0)]), path="ran")
    assert got.verdict == "pass" and got.by_version[0]["version"] == 2 and got.by_version[0]["verdict"] == "fail"


def test_the_tolerance_is_the_dual_check_function():
    from app.engine.nodes import dual_check

    assert dual_check._agree is tolerance.agree and dual_check._delta_pct is tolerance.delta_pct
    assert tolerance.agree(100.0, 100.001, 1e-6) is False and tolerance.agree(100.0, 100.001, 1e-3)
    assert tolerance.agree(0, 0.0000001, 1e-6) and tolerance.delta_pct(200, 180) == -10.0
    assert tolerance.delta_pct(0, 1) is None and tolerance.agree("华东", "华东", 0)


# --------------------------------------------------------------------------
# 读封存的答案、逐题 E1、重评（走一次真的运行）
# --------------------------------------------------------------------------


@pytest.fixture
async def engine_on():
    from app.engine.runner import run_manager

    await run_manager.setup()
    yield
    await run_manager.shutdown()


async def test_reading_the_sealed_answer_and_the_per_case_sql_match(engine_on, monkeypatch, tmp_path):
    from app.eval import score
    from tests import ledger_samples as ls

    await ls.fresh_source(tmp_path / "shop.db")
    run = await ls.explore(monkeypatch, ls.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]，同比增长 12%。")
    answer = await score.read_answer(run.id)
    assert answer.problem is None and answer.report == "write"
    [value] = answer.values
    assert value["value"] == 170.5 and value["kind"] == "cell" and value["column"] == "销售额"
    assert [b["raw"] for b in answer.bare] == ["12%"] and answer.stats["numbers_cited"] == 1
    assert len(answer.queries) == 1 and answer.cited == [answer.queries[0]["artifact"]]

    from app.db.base import SessionLocal
    from app.db.models import DataSource
    from sqlalchemy import select

    async with SessionLocal() as session:
        sid = (await session.execute(select(DataSource.id).where(DataSource.name == ls.CURRENT["source"]))).scalar()
    case = {"gold": {"kind": "sql", "datasource_id": sid, "sql": ls.WITH_REFUNDS}, "expectations": []}
    gold = {"status": "ok", "before": {"rendered": {"sql": ls.WITH_REFUNDS}, "sets": []}}
    match = await score.sql_match(case, gold, answer)
    assert match["status"] == "related" and match["cited"] and "filter" in match["aspects"], match
    gold["before"]["rendered"]["sql"] = ("SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' "
                                         "AND paid_at >= '2026-09-01' AND paid_at < '2026-09-08'")
    assert (await score.sql_match(case, gold, answer))["status"] == "proven"


async def test_the_per_case_sql_match_compares_the_query_behind_the_answer(engine_on, monkeypatch, tmp_path):
    """逐题 E1 比的是产出答案的那条查询：答案引用了就比被引用的；没引用、这次运行又查了几条时不硬比第一条（往往是探索
    查询），照实写没法对上是哪条；没引用、只查了一条时它就是唯一可能的那条，按它比、写明依据。"""
    from app.db.base import SessionLocal
    from app.db.models import DataSource
    from app.engine.runner import run_manager
    from app.eval import score
    from app.ledger import match as ledger_match
    from sqlalchemy import select
    from tests import ledger_samples as ls

    await ls.fresh_source(tmp_path / "shop.db")
    async with SessionLocal() as session:
        sid = (await session.execute(select(DataSource.id).where(DataSource.name == ls.CURRENT["source"]))).scalar()
    case = {"gold": {"kind": "sql", "datasource_id": sid, "sql": "x"}, "expectations": []}
    gold = {"status": "ok", "before": {"rendered": {"sql": (
        "SELECT SUM(amount) AS gmv FROM orders WHERE status = 'paid' AND paid_at >= '2026-09-01' "
        "AND paid_at < '2026-09-08'")}, "sets": []}}

    # 先探了一下有哪些地区，再查销售额；答案没引用查询（数写在叙述里）
    graph = ls.chain(ls.node("start", "input"),
                     ls.node("peek", "tool", tool=ls.tool(), args={"sql": "SELECT DISTINCT region FROM orders"}),
                     ls.node("q", "tool", tool=ls.tool(), args={"sql": ls.WITH_REFUNDS}),
                     ls.node("write", "report", instructions="写答案"),
                     ls.node("out", "output", fields=[{"name": "答案", "value": "{{ nodes.write.text }}"}]))
    ls.script(monkeypatch, "本周销售额约 200 元。")
    started = await run_manager.start(graph=graph, input_payload={}, run_class="exploratory", origin_kind="canvas")
    await ls.wait(started.id)
    await ledger_match.drain()
    answer = await score.read_answer(started.id)
    assert answer.cited == [] and len(answer.queries) == 2, answer.queries
    match = await score.sql_match(case, gold, answer)
    assert match["status"] == "skipped" and match["cited"] is False and "没有引用查询" in match["reason"], match
    assert match.get("query") is None and match.get("aspects") in (None, []), "没法对上是哪条，就不给差在哪"

    # 只查了一条、答案没引用：唯一的那条就是产出答案的，按它比，依据写明
    run = await ls.explore(monkeypatch, ls.EQUIVALENT, text="本周销售额 170.5 元。")
    only = await score.read_answer(run.id)
    assert only.cited == [] and len(only.queries) == 1
    match = await score.sql_match(case, gold, only)
    assert match["status"] == "proven" and match["basis"] == "only_query" and not match["cited"], match


async def test_rescoring_reads_the_seal_and_never_calls_a_model(engine_on, monkeypatch, tmp_path):
    from app.db.base import SessionLocal
    from app.db.models_eval import EvalResult, EvalRun
    from app.eval import rescore, score
    from app.providers import mock_model
    from tests import eval_samples as es
    from tests import ledger_samples as ls

    await ls.fresh_source(tmp_path / "shop.db")
    run = await ls.explore(monkeypatch, ls.EQUIVALENT, text="本周销售额 [[v:Q1.r0.销售额]]。")
    suite_id = await es.make_suite("none", cases=[{"question": "本周销售额？", "expectations": [
        {"kind": "scalar", "column": "gmv"}], "gold": {"kind": "sql", "datasource_id": "none", "sql": "SELECT 1"}}])

    def boom(*_a, **_k):
        raise AssertionError("重评不该调模型")

    monkeypatch.setattr(mock_model.MockChatModel, "_decide", boom)
    async with SessionLocal() as session:
        from app.db.models_eval import EvalCase
        from sqlalchemy import select

        case = (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite_id))).scalar_one()
        er = EvalRun(suite_id=suite_id, status="succeeded")
        session.add(er)
        await session.flush()
        session.add(EvalResult(eval_run_id=er.id, case_id=case.id, case_hash=case.case_hash, status="ok", path="ran",
                               run_id=run.id, verdict="fail", scores=[],
                               gold=_gold({"e1": _scalar(170.5)})))
        await session.commit()
        er_id = er.id
    assert await rescore.rescore(er_id) == 1
    async with SessionLocal() as session:
        row = (await session.execute(select(EvalResult).where(EvalResult.eval_run_id == er_id))).scalar_one()
        assert row.verdict == "pass" and row.scores[0]["outcome"] == "traced"
        rescored = await session.get(EvalRun, er_id)
        assert rescored.summary["accuracy"]["passed"] == 1 and rescored.summary["rescored_at"]
        assert rescored.fingerprint["scorer_version"] == score.SCORER_VERSION
    await es.drop_suites()
