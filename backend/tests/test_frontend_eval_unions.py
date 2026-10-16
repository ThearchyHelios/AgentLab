"""评测页的前端约定（frontend/src/typesEval.ts）和文案（lib/termsEval.ts 的 EVAL_TEXT）里的取值，和服务端的常量逐个对上。

评测页上的结论、作答路径、转移、归因、目录视图、状态都是服务端给的取值，前端照着写叫法（界面层不放逻辑，叫法在 termsEval）。
服务端加了一种（比如作答路径「被驳回」），前端的联合类型和叫法表没跟上，界面就写出英文原值；前端多写了一种，就在等一个
永远不会来的值。这里直接读两份 .ts 文件比（同 test_frontend_type_unions.py 的写法），改了一边没改另一边就过不去。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TYPES_EVAL = ROOT / "frontend" / "src" / "typesEval.ts"
TERMS_EVAL = ROOT / "frontend" / "src" / "lib" / "termsEval.ts"

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _source(path: Path) -> str:
    return _COMMENT.sub("", path.read_text(encoding="utf-8"))


def _union(name: str) -> list[str]:
    text = _source(TYPES_EVAL)
    found = re.search(rf"export type {name} =(.*?)(?=\nexport |\n\n|\Z)", text, re.S)
    assert found is not None, f"typesEval.ts 里没有 export type {name}"
    return re.findall(r"'([^']+)'", found.group(1))


def _labels(path: str) -> list[str]:
    """EVAL_TEXT 里某一张叫法表（a.b 这样的路径）的键，按写的顺序。只认一行写完、或几行写完的对象字面量。"""
    text = _source(TERMS_EVAL)
    *parents, leaf = path.split(".")
    start = 0
    for part in parents:
        hit = re.search(rf"\n\s*{part}: \{{", text[start:])
        assert hit is not None, f"EVAL_TEXT 里没有 {part}"
        start += hit.end()
    hit = re.search(rf"\n\s*{leaf}: \{{(.*?)\}}( as Record<[^>]+>)?,", text[start:], re.S)
    assert hit is not None, f"EVAL_TEXT 里没有 {path}"
    return re.findall(r"(\w+): '", hit.group(1))


def test_statuses_triggers_and_kinds():
    from app.core.job_runtime import ACTIVE, FINISHED
    from app.db.models_eval import CASE_KINDS, RESULT_PATHS, RUN_TRIGGERS, VERDICTS

    assert _union("EvalStatus") == [*ACTIVE, *FINISHED]
    assert _union("EvalTrigger") == list(RUN_TRIGGERS)
    assert _union("EvalCaseKind") == list(CASE_KINDS)
    assert _union("EvalVerdict") == list(VERDICTS)
    assert _union("EvalPath") == list(RESULT_PATHS)
    assert _labels("status") == [*ACTIVE, *FINISHED]
    assert _labels("trigger") == list(RUN_TRIGGERS)
    assert _labels("verdict") == list(VERDICTS)
    assert _labels("path") == list(RESULT_PATHS)


def test_expectations_gold_and_trial():
    from app.eval.cases import EXPECTATION_KINDS, GOLD_KINDS
    from app.eval.gold import STATUSES, UPGRADE_LABEL

    assert _union("EvalExpectationKind") == list(EXPECTATION_KINDS)
    assert _labels("expectation.kind") == list(EXPECTATION_KINDS)
    assert _union("EvalGoldKind") == list(GOLD_KINDS)
    assert _labels("gold.kind") == list(GOLD_KINDS)
    assert _union("EvalUpgradePolicy") == list(UPGRADE_LABEL)
    # 试算的结论：服务端的几种，加上作答前后算出来不一样时的 drifted（gold.settle）
    assert set(STATUSES) | {"drifted"} == set(_union("EvalTrialStatus"))
    assert set(_union("EvalTrialStatus")) <= set(_labels("trialStatus"))


def test_scores_compare_and_catalog_modes():
    from app.eval.compare import CAUSES, TRANSITIONS
    from app.eval.score import OUTCOMES, TEXT_OUTCOMES
    from app.eval.variants import CATALOG_MODES

    assert _union("EvalOutcome") == list(OUTCOMES)
    assert _labels("outcome") == list(OUTCOMES)
    assert _union("EvalTextOutcome") == list(TEXT_OUTCOMES)
    assert _labels("textOutcome") == list(TEXT_OUTCOMES)
    assert _union("EvalTransition") == list(TRANSITIONS)
    assert _labels("compare.transition") == list(TRANSITIONS)
    assert _union("EvalCause") == list(CAUSES)
    assert _labels("compare.cause") == list(CAUSES)
    assert _union("EvalCatalogMode") == list(CATALOG_MODES)
    assert _labels("start.catalogMode") == list(CATALOG_MODES)


def test_signal_bands_follow_the_server():
    from app.api.evals import SIGNAL_CRITERIA

    assert _union("EvalE1Band") == [c["band"] for c in SIGNAL_CRITERIA["e1"]]
    assert _union("EvalE3Band") == [c["band"] for c in SIGNAL_CRITERIA["e3"]]
    assert set(_labels("signals.band")) == {c["band"] for c in [*SIGNAL_CRITERIA["e1"], *SIGNAL_CRITERIA["e3"]]}
