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
#: 分析方法、模式的叫法只有这一份（termsEval 引用它，不另抄）
TERMS_ANALYSIS = ROOT / "frontend" / "src" / "lib" / "termsAnalysis.ts"
#: 分析卡的一项摆哪几个参数格（评测页的分析期望和分析卡用同一份）
ANALYSIS_ITEMS = ROOT / "frontend" / "src" / "canvas" / "analysisItems.ts"
#: 评测页题目编辑框（分析期望那一块）
CASE_EDITOR = ROOT / "frontend" / "src" / "pages" / "evals" / "CaseEditor.tsx"

_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _source(path: Path) -> str:
    return _COMMENT.sub("", path.read_text(encoding="utf-8"))


def _union(name: str) -> list[str]:
    text = _source(TYPES_EVAL)
    found = re.search(rf"export type {name} =(.*?)(?=\nexport |\n\n|\Z)", text, re.S)
    assert found is not None, f"typesEval.ts 里没有 export type {name}"
    return re.findall(r"'([^']+)'", found.group(1))


def _pairs(path: str) -> list[tuple[str, str]]:
    """EVAL_TEXT 里某一张叫法表（a.b 这样的路径）的 (键, 叫法)，按写的顺序。只认一行写完、或几行写完的对象字面量。"""
    text = _source(TERMS_EVAL)
    *parents, leaf = path.split(".")
    start = 0
    for part in parents:
        hit = re.search(rf"\n\s*{part}: \{{", text[start:])
        assert hit is not None, f"EVAL_TEXT 里没有 {part}"
        start += hit.end()
    hit = re.search(rf"\n\s*{leaf}: \{{(.*?)\}}( as Record<[^>]+>)?,", text[start:], re.S)
    assert hit is not None, f"EVAL_TEXT 里没有 {path}"
    return re.findall(r"(\w+): '([^']*)'", hit.group(1))


def _labels(path: str) -> list[str]:
    """EVAL_TEXT 里某一张叫法表的键，按写的顺序。"""
    return [key for key, _ in _pairs(path)]


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


def test_analysis_tools_and_presets_follow_the_server():
    """变体的分析工具开、关和预设对照（api/evals.PRESETS）：前端的取值和服务端一一对上。叫法写「开」「关」，和服务端
    describe、对比里写的同一种说法。"""
    from app.api.evals import PRESETS
    from app.eval.variants import ANALYSIS_TOOLS, ANALYSIS_TOOLS_LABEL

    assert _union("EvalAnalysisTools") == list(ANALYSIS_TOOLS)
    assert _pairs("start.analysisToolsMode") == list(ANALYSIS_TOOLS_LABEL.items())
    assert _union("EvalPreset") == list(PRESETS)


def test_signal_bands_follow_the_server():
    from app.api.evals import SIGNAL_CRITERIA

    assert _union("EvalE1Band") == [c["band"] for c in SIGNAL_CRITERIA["e1"]]
    assert _union("EvalE3Band") == [c["band"] for c in SIGNAL_CRITERIA["e3"]]
    assert set(_labels("signals.band")) == {c["band"] for c in [*SIGNAL_CRITERIA["e1"], *SIGNAL_CRITERIA["e3"]]}


def _analysis_table(name: str) -> list[tuple[str, str]]:
    """lib/termsAnalysis.ts 里一张叫法表（export const NAME … = { … }）的 (键, 叫法)，按写的顺序。"""
    text = _source(TERMS_ANALYSIS)
    found = re.search(rf"export const {name}\b[^=]*= \{{(.*?)\n\}}", text, re.S)
    assert found is not None, f"termsAnalysis.ts 里没有 export const {name}"
    return re.findall(r"(\w+): '([^']*)'", found.group(1))


def test_analysis_expectations_follow_the_server():
    """分析期望：判定类和服务端 app/eval/judgements 一致；分析方法、模式的叫法和契约（app/analysis/contract 的 OP_LABELS、
    MODE_LABELS）一字不差——界面上的「变化拆解（可加）」和服务端拼的、证据面板写的是同一种说法。叫法只有 lib/termsAnalysis
    一份：评测页（EVAL_TEXT.analysis.op / mode）引用它，不另抄一份。"""
    from app.analysis.contract import MODE_LABELS, MODES, OP_LABELS, OPS
    from app.eval.judgements import JUDGEMENTS

    assert _union("EvalJudgement") == list(JUDGEMENTS)
    assert _labels("analysis.judgement") == list(JUDGEMENTS)
    assert _union("EvalAnalysisOp") == list(OPS)
    assert _analysis_table("ANALYSIS_OP_LABEL") == list(OP_LABELS.items())
    assert _union("EvalAnalysisMode") == list(MODE_LABELS)
    assert _analysis_table("ANALYSIS_MODE_LABEL") == list(MODE_LABELS.items())
    assert {m for modes in MODES.values() for m in modes} == set(MODE_LABELS)
    terms = _source(TERMS_EVAL)
    assert re.search(r"\n\s*op: ANALYSIS_OP_LABEL\b", terms) and re.search(r"\n\s*mode: ANALYSIS_MODE_LABEL\b", terms), \
        "评测页的分析方法、模式叫法要引用 lib/termsAnalysis 的那一份"


def test_analysis_param_grids_follow_the_card_and_the_server():
    """分析期望的参数格和分析卡的一项同一份（canvas/analysisItems.paramSpecs）：评测页（CaseEditor）引用它，不另抄。
    参数名对得上服务端各参数模型（tools/builtin/analysis.param_names）；叫法只有 lib/termsAnalysis 一份
    （ANALYSIS_TEXT.node.param / basis / grain），评测页（EVAL_TEXT.analysis.param / basis / grain）引用它。"""
    from app.eval.judgements import VERDICT_OPS, param_names

    items = _source(ANALYSIS_ITEMS)
    grid = re.search(r"export function paramSpecs\(op: AnalysisOp.*?\n\}\n", items, re.S)
    assert grid is not None, "analysisItems.ts 里没有 export function paramSpecs"
    # 期次三格（period、base、target）写在上面的 PERIODS 常量里，一起算
    periods = re.search(r"const PERIODS: ParamSpec\[\] = \[(.*?)\]\n", items, re.S)
    assert periods is not None, "analysisItems.ts 里没有 const PERIODS"
    grid_keys = {a or b for a, b in re.findall(r"(?:col|cols|val)\('(\w+)'\)|key: '(\w+)'",
                                                grid.group(0) + periods.group(1))}
    # 服务端各参数模型里期望能写的那些（去掉输入、总数核对、下钻、两份查询），格子里一个不少、一个不多
    server = {k for op in ("decompose", *VERDICT_OPS) for k in param_names(op)}
    assert grid_keys == server, f"格子和服务端的参数模型对不上：少 {server - grid_keys}，多 {grid_keys - server}"
    editor = _source(CASE_EDITOR)
    assert "paramSpecs(" in editor and "PARAM_FIELDS" not in editor, "评测页的参数格要引用分析卡的那一份（paramSpecs）"
    terms = _source(TERMS_EVAL)
    for table in ("param", "basis", "grain"):
        assert re.search(rf"\n\s*{table}: ANALYSIS_TEXT\.node\.{table}\b", terms), \
            f"评测页的 {table} 叫法要引用 lib/termsAnalysis 的那一份"
    analysis = _source(TERMS_ANALYSIS)
    labels = re.search(r"\n\s*param: \{(.*?)\} as Record<string, string>", analysis, re.S)
    assert labels is not None, "termsAnalysis.ts 里没有 ANALYSIS_TEXT.node.param"
    assert set(grid_keys) <= set(re.findall(r"(\w+): '", labels.group(1))), "参数格里有叫不出名字的参数"
