"""评分：拿期望和答案比，给出每个期望的结论和这道题过没过（细案 01 做法 10；评测-D4）。

**只读封存，不调模型、不查库。** 答案从作答那次运行封存范围内的报告文档、查询快照里取（api/evidence 的封存读法，和证据
面板、口径账本同一份）；期望是作答前后算好的那一份（EvalResult.gold，gold.settle 的形状）；文字要点的判断是作答时裁判给的
（EvalResult.judge，judging.py）。所以可以「按新规则重新评分」：规则改了 SCORER_VERSION 就加一，重评只把这几样再读一遍。

**从答案里取数。**
- 报告文档里引用解析成的数（segment.cite.role=value、status=resolved）：取 cite.value 原值（显示时的换算、取整不影响）。
  引用的是查询的一格时，读那份快照里这一行的非数值列当维度；引用的是口径卡的指标时没有维度。
- 正文裸写的数（uncited_number）、没有报告时成果里的文字、直接回答的原文：按出具校验同一套认法取（engine/issuance 的
  extract_numbers：千分位、万 / 亿后缀、百分号、中文数字），容差按写出的精度推。叙述里自己算出来的数也在这里——只能算
  「对但没引用」，不算可追溯（叙述层无算术）。

**每个期望五种结论**（OUTCOMES）：traced 对且可追溯（引用的格或指标对上了）、untraced 对但没引用（只有正文裸写的对上了）、
wrong 错、scale 量纲不同（差 10 的整数次幂，比如元和万元、比率和百分数写反了：单列，也不算对）、ungradable 不能评分（期望
没算出来、作答期间数据变了、答案引用的是遮罩格、封存核对不上）。
- scalar：数值按「允许的相对误差」比（engine/tolerance.agree，和双盲复算同一个函数）；期望没写容差时，引用的原值按
  DEFAULT_TOLERANCE，裸写的数按它写出的精度（3.5 万 ±500）。维度配对：期望写了 where（或期望那一行本身有维度）时，答案
  那一格所在行的非数值列要有这个取值；答案那一格没有维度（按条件筛过、只剩一个数）不算对不上。期望值是 0–10 的小整数
  又没有维度可配时标「弱匹配」（巧合的数：3 家门店正好等于期望的 3）。
- table：逐行按 keys 的取值配对，每个要比的值同 scalar；ordered 时按答案里各行键值出现的先后核对顺序。
- set：名单里每一项都要在答案里出现（ordered 时按顺序）；每一项都出现在答案查过的结果里算可追溯。
- none：期望答「没有数据」：答案里没有数，而且查过的查询里有返回 0 行的（可追溯）；没有数、但也没有一条查询返回 0 行的
  算「对但没引用」。
- ask：期望反问：作答路径是反问才对。
- text：文字要点，由裁判判（covered / partial / missing / unjudged），单列「模型判断」，不进准确率（评测-D4）。

**题目结论**（verdict）：路径不对（要答出数的题直接回答了、反问了、只建不跑、建图或运行失败）直接不通过、写明原因；
必需的期望全是 traced / untraced 就通过，另报「可追溯通过」（全是 traced）；有 wrong / scale 的不通过；剩下的（有不能评分的、
没有错的）不能评分。期望没算出来按 gold 的结论：口径升版待处置是跳过，别的是不能评分——不混进「答错」。被驳回（总方案 X9）
记路径「被驳回」，不算失败：不能评分。只有文字要点的题不能评分（单列，不计入准确率）。

**逐题的 E1**（sql_match）：答案里的查询（先看报告引用了的）和期望的取数（SQL 题是期望 SQL，口径题是口径卡的取数节点）
用口径账本同一个比对函数（ledger/match.compare_queries：先绑参数、再由 explain_sql_diff 判等价）比一次，给出等价与否和差在
哪个方面（总方案 X2：E1 只有一份实现）。
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.engine.tolerance import agree, is_finite_number

logger = logging.getLogger(__name__)

#: 评分规则的版本：规则改了就加一，结果里记着按哪一版评的，可以按新规则重评
SCORER_VERSION = 1

OUTCOMES = ("traced", "untraced", "wrong", "scale", "ungradable")
OUTCOME_LABEL = {"traced": "对且可追溯", "untraced": "对但没引用", "wrong": "错", "scale": "量纲不同",
                 "ungradable": "不能评分"}
#: 文字要点的判断（裁判给的）
TEXT_OUTCOMES = ("covered", "partial", "missing", "unjudged")
TEXT_LABEL = {"covered": "都答到了", "partial": "答到一部分", "missing": "没答到", "unjudged": "没有判"}
VERDICT_LABEL = {"pass": "通过", "fail": "不通过", "ungradable": "不能评分", "skipped": "跳过"}
PATH_LABEL = {"reply": "直接回答", "asked": "反问", "ready": "只建不跑", "build_failed": "建图失败",
              "run_failed": "运行失败", "ran": "跑完了", "rejected": "被驳回"}
#: 期望没写容差时，引用的原值按这个相对误差比（同双盲复算的缺省）
DEFAULT_TOLERANCE = 1e-6
#: 量纲不同：差这几个倍数（元 / 万元、比率 / 百分数……）
SCALE_FACTORS = (10.0, 100.0, 1000.0, 1e4, 1e5, 1e6, 1e8)
#: 弱匹配：期望是这么小的整数、又没有维度可配
WEAK_MAX = 10

#: 路径不对时给人看的原因
WRONG_PATH = {
    "reply": "该查不查：助手直接回答了，没有查数",
    "asked": "该答不答：助手反问了",
    "ready": "只建了图、没有运行",
    "build_failed": "建图失败",
    "run_failed": "运行失败",
}


# ==========================================================================
# 从封存的运行里读答案
# ==========================================================================


def _numbers(text: str) -> list[dict[str, Any]]:
    from app.engine.issuance import _tolerance, extract_numbers

    out = []
    for token in extract_numbers(text or ""):
        out.append({"raw": token.raw, "value": token.value, "decimals": token.decimals, "percent": token.is_percent,
                    "tolerance": _tolerance(token)})
    return out


def _snapshot(artifact: str | None, cache: dict[str, Any]) -> dict[str, Any] | None:
    from app.core import artifact_store

    if not artifact:
        return None
    if artifact not in cache:
        try:
            content = artifact_store.load(artifact)
        except ValueError:
            content = None
        cache[artifact] = content if isinstance(content, dict) else None
    return cache[artifact]


def _masked_columns(snapshot: Mapping[str, Any]) -> set[str]:
    return {str(c) for c in snapshot.get("masked_at_query") or []}


def _cell_facts(cite: Mapping[str, Any], catalog: Mapping[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    """引用的一格：哪份快照、第几行哪一列、这一行的维度（非数值列）、是不是遮罩格。"""
    entry = catalog.get(cite.get("alias") or "") or {}
    locator = cite.get("locator") or {}
    artifact = entry.get("artifact")
    out: dict[str, Any] = {"artifact": artifact, "row": locator.get("row"), "column": locator.get("column"), "dims": {}}
    snapshot = _snapshot(artifact, cache)
    if snapshot is None:
        return out
    columns = [str(c) for c in snapshot.get("columns") or []]
    masked = _masked_columns(snapshot)
    out["masked"] = bool(snapshot.get("masked_whole")) or str(locator.get("column")) in masked
    row_index = locator.get("row")
    rows = snapshot.get("rows") or []
    if isinstance(row_index, int) and 0 <= row_index < len(rows) and isinstance(rows[row_index], (list, tuple)):
        row = rows[row_index]
        out["dims"] = {c: row[i] for i, c in enumerate(columns)
                       if i < len(row) and c != locator.get("column") and c not in masked and row[i] is not None
                       and not is_finite_number(row[i]) and not isinstance(row[i], bool)}
    return out


@dataclass
class Answer:
    """从封存的作答运行里读出来的答案（评分只看这些）。problem 不为空时整份答案读不了（封存核对不上、没有封存）。"""

    run_id: str | None
    text: str = ""
    values: list[dict[str, Any]] = field(default_factory=list)
    bare: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    tier: str | None = None
    report: str | None = None
    problem: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "text": self.text, "values": self.values, "bare": self.bare,
                "queries": self.queries, "cited": self.cited, "stats": self.stats, "tier": self.tier,
                "report": self.report, "problem": self.problem}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Answer:
        return cls(run_id=raw.get("run_id"), text=str(raw.get("text") or ""), values=list(raw.get("values") or []),
                   bare=list(raw.get("bare") or []), queries=list(raw.get("queries") or []),
                   cited=list(raw.get("cited") or []), stats=dict(raw.get("stats") or {}), tier=raw.get("tier"),
                   report=raw.get("report"), problem=raw.get("problem"))


def _output_text(output: Any) -> str:
    """成果里给人看的文字（没有报告文档时从这里取数）：字段值里的文字，_ 开头的元数据不算。"""
    if not isinstance(output, Mapping):
        return str(output or "")
    parts = []
    for key, value in output.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(str(value))
    return "\n".join(parts)


async def read_answer(run_id: str | None, *, reply: str | None = None) -> Answer:
    """读一次作答运行的答案。没有运行（直接回答、反问）的只有原文：reply。"""
    from fastapi import HTTPException

    from app.api.evidence import _choose, _reports, _sealed

    if not run_id:
        text = reply or ""
        return Answer(run_id=None, text=text, bare=_numbers(text))
    try:
        sealed = await _sealed(run_id)
    except HTTPException as e:
        return Answer(run_id=run_id, problem=str(e.detail))
    if not sealed.sealed:
        return Answer(run_id=run_id, problem="作答的运行没有封存，答案无从核对")
    if not sealed.trusted:
        return Answer(run_id=run_id, problem="作答的运行封存核对不一致，答案不能当证据")
    cache: dict[str, Any] = {}
    answer = Answer(run_id=run_id)
    for artifact, info in sealed.queries.items():
        snap = _snapshot(artifact, cache)
        if snap is None:
            continue
        rows = snap.get("rows") or []
        answer.queries.append({"artifact": artifact, "node_id": (info or {}).get("node_id"),
                               "source": snap.get("source") or (info or {}).get("source"), "sql": snap.get("sql"),
                               "columns": [str(c) for c in snap.get("columns") or []],
                               "row_count": snap.get("row_count", len(rows)), "truncated": bool(snap.get("truncated")),
                               "role": snap.get("role")})
    from app.db.base import SessionLocal
    from app.db.models import Run

    async with SessionLocal() as session:
        row = await session.get(Run, run_id)
        final = row.output if row is not None else None
    issuance = (final or {}).get("_issuance") if isinstance(final, Mapping) else None
    answer.tier = (issuance or {}).get("tier") if isinstance(issuance, Mapping) else None
    reports = _reports(sealed)
    chosen = _choose(reports, None, sealed) if reports else None
    if chosen is None or chosen.doc is None or chosen.hash_ok is False:
        answer.text = _output_text(final)
        answer.bare = _numbers(answer.text)
        if chosen is not None and chosen.hash_ok is False:
            answer.problem = "报告文档与哈希不一致，疑似被修改，答案不能当证据"
        return answer
    doc = chosen.doc
    catalog = doc.get("catalog") or {}
    answer.report = chosen.node_id
    answer.text = str(doc.get("markdown") or "")
    answer.stats = dict(doc.get("stats") or {})
    from app.engine.evidence import iter_segments

    cited: list[str] = []
    for seg in iter_segments(doc):
        cite = seg.get("cite") or {}
        if cite.get("role") == "value" and cite.get("status") == "resolved":
            item: dict[str, Any] = {"value": cite.get("value"), "kind": cite.get("kind"), "text": seg.get("text"),
                                    "segment": seg.get("id"), "alias": cite.get("alias")}
            if cite.get("kind") == "metric":
                entry = catalog.get(cite.get("alias") or "") or {}
                item.update(metric=(cite.get("locator") or {}).get("metric"), artifact=entry.get("artifact"), dims={})
            else:
                item.update(_cell_facts(cite, catalog, cache))
            answer.values.append(item)
            if item.get("artifact"):
                cited.append(item["artifact"])
        elif seg.get("issue") == "uncited_number" or (seg.get("kind") == "number" and not cite):
            answer.bare += _numbers(str(seg.get("text") or ""))
    answer.cited = list(dict.fromkeys(cited))
    return answer


# ==========================================================================
# 比
# ==========================================================================


def _close(expected: Any, got: Any, tolerance: float) -> bool:
    if is_finite_number(expected) and is_finite_number(got):
        return agree(float(expected), float(got), tolerance)
    return expected is not None and got is not None and str(expected).strip() == str(got).strip()


def _bare_close(expected: Any, token: Mapping[str, Any], tolerance: float | None) -> bool:
    """裸写的数对不对得上：容差取期望写的、或者这个数写出的精度（大的那个）。百分数和比率互认。"""
    if not is_finite_number(expected):
        return False
    value = float(token["value"])
    allowed = max(float(token.get("tolerance") or 0), abs(float(expected)) * (tolerance or 0))
    if abs(value - float(expected)) <= allowed + 1e-9:
        return True
    return bool(token.get("percent")) and abs(value - float(expected) * 100) <= allowed + 1e-9


def _scaled(expected: Any, got: float) -> bool:
    """差 10 的整数次幂（量纲不同）。"""
    if not is_finite_number(expected) or expected == 0 or got == 0:
        return False
    ratio = abs(got / float(expected))
    return any(math.isclose(ratio, f, rel_tol=1e-6) or math.isclose(ratio, 1 / f, rel_tol=1e-6) for f in SCALE_FACTORS)


def _dims_ok(wanted: Mapping[str, Any], dims: Mapping[str, Any]) -> bool:
    """维度配对：答案那一格所在行有维度时，每个期望的键值都要出现在这一行的非数值列里（按取值认，列名可以不同）。"""
    if not wanted or not dims:
        return True
    shown = {str(v).strip() for v in dims.values()}
    return all(str(v).strip() in shown for v in wanted.values())


def _key_of(exp: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    return dict(exp.get("where") or gold.get("dims") or {})


def _score_scalar(exp: Mapping[str, Any], gold: Mapping[str, Any], answer: Answer) -> dict[str, Any]:
    expected = gold.get("value")
    tolerance = exp.get("tolerance")
    key = _key_of(exp, gold)
    out: dict[str, Any] = {"expected": expected}
    if key:
        out["key"] = key
    if is_finite_number(expected) and float(expected).is_integer() and 0 <= float(expected) <= WEAK_MAX and not key:
        out["weak"] = True
    usable = [v for v in answer.values if _dims_ok(key, v.get("dims") or {})]
    masked = [v for v in usable if v.get("masked")]
    clean = [v for v in usable if not v.get("masked")]
    for v in clean:
        if _close(expected, v.get("value"), tolerance if tolerance is not None else DEFAULT_TOLERANCE):
            return {**out, "outcome": "traced", "got": v.get("value"), "cell": _cell_ref(v)}
    for token in answer.bare:
        if _bare_close(expected, token, tolerance):
            return {**out, "outcome": "untraced", "got": token["value"], "raw": token["raw"]}
    if not is_finite_number(expected) and expected is not None and str(expected).strip() and \
            str(expected).strip() in answer.text:
        return {**out, "outcome": "untraced", "got": str(expected)}
    if masked and not clean:
        return {**out, "outcome": "ungradable", "reason": "答案引用的是遮罩的格，不评分"}
    for v in clean:
        if is_finite_number(v.get("value")) and _scaled(expected, float(v["value"])):
            return {**out, "outcome": "scale", "got": v.get("value"), "cell": _cell_ref(v)}
    for token in answer.bare:
        if _scaled(expected, float(token["value"])):
            return {**out, "outcome": "scale", "got": token["value"], "raw": token["raw"]}
    got = next((v.get("value") for v in clean), None)
    return {**out, "outcome": "wrong", **({"got": got} if got is not None else {}),
            "reason": "答案里没有对得上的数" if not answer.values and not answer.bare else "答案里的数和期望对不上"}


def _cell_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    return {k: value.get(k) for k in ("artifact", "row", "column", "metric", "segment") if value.get(k) is not None}


def _first_position(text: str, word: Any) -> int:
    return text.find(str(word).strip()) if word is not None and str(word).strip() else -1


def _score_table(exp: Mapping[str, Any], gold: Mapping[str, Any], answer: Answer) -> dict[str, Any]:
    keys = list(gold.get("keys") or [])
    columns = [c for c in gold.get("columns") or [] if c not in keys]
    rows = list(gold.get("rows") or [])
    tolerance = exp.get("tolerance")
    cells: list[str] = []
    for row in rows:
        key = {k: row.get(k) for k in keys}
        for column in columns:
            sub = _score_scalar({"tolerance": tolerance, "where": key}, {"value": row.get(column)}, answer)
            cells.append(sub["outcome"])
    outcome = _combine(cells)
    out: dict[str, Any] = {"rows": len(rows), "cells": dict(_count(cells)), "outcome": outcome}
    if outcome in ("traced", "untraced") and gold.get("ordered"):
        positions = [_first_position(answer.text, row.get(keys[0])) if keys else -1 for row in rows]
        if any(p < 0 for p in positions) or positions != sorted(positions):
            out.update(outcome="wrong", reason="行的先后和期望不一致")
    return out


def _score_set(exp: Mapping[str, Any], gold: Mapping[str, Any], answer: Answer, cache: dict[str, Any]) -> dict[str, Any]:
    values = [v for v in gold.get("values") or [] if v is not None]
    positions = [_first_position(answer.text, v) for v in values]
    missing = [v for v, p in zip(values, positions) if p < 0]
    if missing:
        return {"outcome": "wrong", "missing": missing[:20], "reason": f"答案里少了 {len(missing)} 项"}
    if gold.get("ordered") and positions != sorted(positions):
        return {"outcome": "wrong", "reason": "名单的先后和期望不一致"}
    seen: set[str] = set()
    for query in answer.queries:
        snap = _snapshot(query.get("artifact"), cache) or {}
        masked = _masked_columns(snap)
        columns = [str(c) for c in snap.get("columns") or []]
        for row in snap.get("rows") or []:
            if isinstance(row, (list, tuple)):
                seen |= {str(c).strip() for i, c in enumerate(row) if c is not None and
                         (i >= len(columns) or columns[i] not in masked)}
    traced = all(str(v).strip() in seen for v in values)
    return {"outcome": "traced" if traced else "untraced", "items": len(values)}


def _score_none(answer: Answer) -> dict[str, Any]:
    if answer.values or answer.bare:
        return {"outcome": "wrong", "reason": "期望答「没有数据」，答案里却给了数"}
    if any(int(q.get("row_count") or 0) == 0 and q.get("role") != "verify" for q in answer.queries):
        return {"outcome": "traced"}
    return {"outcome": "untraced", "reason": "答案没有给数，但没有一条查询返回 0 行可以说明「没有数据」"}


def _combine(outcomes: Sequence[str]) -> str:
    """几个结论合成一个：有错就错（全是量纲不同的算量纲不同），有不能评分的不能评分，全可追溯才可追溯。"""
    if not outcomes:
        return "ungradable"
    bad = [o for o in outcomes if o in ("wrong", "scale")]
    if bad:
        return "scale" if all(o == "scale" for o in bad) else "wrong"
    if "ungradable" in outcomes:
        return "ungradable"
    return "traced" if all(o == "traced" for o in outcomes) else "untraced"


def _count(outcomes: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for o in outcomes:
        out[o] = out.get(o, 0) + 1
    return out


def _text_outcome(exp: Mapping[str, Any], judgement: Mapping[str, Any] | None) -> dict[str, Any]:
    """文字要点：裁判对每条要点的判断（judging.judge_points 的结果），合成一个。"""
    points = list(exp.get("points") or [])
    found = ((judgement or {}).get("expectations") or {}).get(exp["id"]) if judgement else None
    if not found:
        reason = (judgement or {}).get("reason") or "这次评测没有打开结论裁判，文字要点没有判"
        return {"outcome": "unjudged", "points": len(points), "reason": reason}
    verdicts = [p.get("verdict") for p in found.get("points") or []]
    if not verdicts or any(v not in ("covered", "partial", "missing") for v in verdicts):
        return {"outcome": "unjudged", "points": len(points), "reason": found.get("reason") or "裁判没有给出判断"}
    outcome = "covered" if all(v == "covered" for v in verdicts) else \
        "missing" if all(v == "missing" for v in verdicts) else "partial"
    return {"outcome": outcome, "points": len(points), "detail": found.get("points")}


# ==========================================================================
# 一道题
# ==========================================================================


@dataclass
class CaseScore:
    verdict: str
    traced: bool
    reason: str | None
    scores: list[dict[str, Any]]
    #: 口径题选了 dual 时新版那一份期望的结论（旧的那一份就是 scores）
    by_version: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "traced": self.traced, "reason": self.reason, "scores": self.scores,
                "by_version": self.by_version, "scorer_version": SCORER_VERSION}


def _expectation_scores(expectations: Sequence[Mapping[str, Any]], values: Mapping[str, Any], answer: Answer,
                        path: str | None, judgement: Mapping[str, Any] | None, cache: dict[str, Any]
                        ) -> list[dict[str, Any]]:
    out = []
    for exp in expectations:
        kind = exp.get("kind")
        base = {"id": exp["id"], "kind": kind, "required": bool(exp.get("required", True))}
        if exp.get("label"):
            base["label"] = exp["label"]
        if kind == "text":
            out.append({**base, **_text_outcome(exp, judgement)})
            continue
        if kind == "ask":
            ok = path == "asked"
            out.append({**base, "outcome": "traced" if ok else "wrong",
                        **({} if ok else {"reason": "期望反问（问法含糊），助手却没有反问"})})
            continue
        gold = values.get(exp["id"])
        if gold is None:
            out.append({**base, "outcome": "ungradable", "reason": "期望没有算出这一项"})
            continue
        if kind == "scalar":
            out.append({**base, **_score_scalar(exp, gold, answer)})
        elif kind == "table":
            out.append({**base, **_score_table(exp, gold, answer)})
        elif kind == "set":
            out.append({**base, **_score_set(exp, gold, answer, cache)})
        elif kind == "none":
            out.append({**base, **_score_none(answer)})
    return out


def _verdict(scores: Sequence[Mapping[str, Any]]) -> tuple[str, bool, str | None]:
    counted = [s for s in scores if s.get("required") and s.get("kind") != "text"]
    if not counted:
        return "ungradable", False, "这道题只有文字要点：由裁判判、单列，不计入准确率"
    outcomes = [s["outcome"] for s in counted]
    if any(o in ("wrong", "scale") for o in outcomes):
        first = next(s for s in counted if s["outcome"] in ("wrong", "scale"))
        return "fail", False, first.get("reason") or OUTCOME_LABEL[first["outcome"]]
    if any(o == "ungradable" for o in outcomes):
        first = next(s for s in counted if s["outcome"] == "ungradable")
        return "ungradable", False, first.get("reason") or OUTCOME_LABEL["ungradable"]
    return "pass", all(o == "traced" for o in outcomes), None


def score_case(case: Mapping[str, Any], gold: Mapping[str, Any] | None, answer: Answer, *, path: str | None,
               path_reason: str | None = None, judgement: Mapping[str, Any] | None = None) -> CaseScore:
    """一道题的评分。gold 是 gold.Settled.to_dict() 的形状；answer 是 read_answer 读出来的。纯计算。"""
    from app.eval.cases import NEEDS_GOLD

    expectations = list(case.get("expectations") or [])
    needs_numbers = any(e.get("kind") in NEEDS_GOLD for e in expectations)
    gold = dict(gold or {})
    if needs_numbers and gold.get("status") != "ok":
        verdict = "skipped" if gold.get("status") == "skipped" else "ungradable"
        reason = gold.get("reason") or "期望没有算出来"
        scores = [{"id": e["id"], "kind": e.get("kind"), "required": bool(e.get("required", True)),
                   "outcome": "ungradable", "reason": reason} for e in expectations if e.get("kind") != "text"]
        return CaseScore(verdict, False, reason, scores)
    if path == "rejected":
        return CaseScore("ungradable", False, path_reason or "运行被驳回（人的决定，不算失败）", [])
    ask_only = any(e.get("kind") == "ask" for e in expectations)
    if needs_numbers and path != "ran":
        reason = WRONG_PATH.get(path or "", "没有跑出答案")
        if path_reason:
            reason = f"{reason}：{path_reason}"
        return CaseScore("fail", False, reason, [{"id": e["id"], "kind": e.get("kind"),
                                                   "required": bool(e.get("required", True)), "outcome": "wrong",
                                                   "reason": reason} for e in expectations if e.get("kind") != "text"])
    if answer.problem and needs_numbers:
        return CaseScore("ungradable", False, answer.problem, [])
    cache: dict[str, Any] = {}
    sets = list(((gold.get("before") or {}).get("sets") or [])) if needs_numbers else [{}]
    first = sets[0] if sets else {}
    scores = _expectation_scores(expectations, (first or {}).get("values") or {}, answer, path, judgement, cache)
    verdict, traced, reason = _verdict(scores)
    if ask_only and path != "asked":
        verdict, traced, reason = "fail", False, "期望反问（问法含糊），助手却没有反问"
    by_version = []
    for extra in sets[1:]:
        more = _expectation_scores(expectations, (extra or {}).get("values") or {}, answer, path, judgement, cache)
        v, t, r = _verdict(more)
        by_version.append({"version": (extra or {}).get("version"), "verdict": v, "traced": t, "reason": r,
                           "scores": more})
    return CaseScore(verdict, traced, reason, scores, by_version)


# ==========================================================================
# 逐题的 E1：答案的查询和期望的取数比一次（ledger/match 的比对函数）
# ==========================================================================


async def gold_calibers(case: Mapping[str, Any], gold: Mapping[str, Any] | None) -> list[Any]:
    """期望的取数，包成口径账本比对用的 Caliber：SQL 题是一条期望 SQL（按截至时间渲染好的那一条，不带参数），口径题是
    钉住版本里口径卡读的取数节点（带模板参数，由 match_template 绑定）。拿不到的为空。"""
    from app.db.base import SessionLocal
    from app.db.models import DataSource, WorkflowVersion
    from app.eval.gold import QUERY_NODE
    from app.ledger.match import Caliber, Fetch, _table_keys, caliber_of

    spec = (case.get("gold") or {})
    if not spec or (gold or {}).get("status") not in ("ok", "drifted"):
        return []
    before = (gold or {}).get("before") or {}
    if spec.get("kind") in ("sql", "from_run"):
        sql = str((before.get("rendered") or {}).get("sql") or "")
        async with SessionLocal() as session:
            source = await session.get(DataSource, spec.get("datasource_id"))
        if not sql or source is None:
            return []
        return [Caliber(workflow_id="gold", version=0, level="", graph_hash=None,
                        fetches=(Fetch(QUERY_NODE, source.name, sql, _table_keys(sql)),))]
    if spec.get("kind") == "caliber":
        from sqlalchemy import select

        async with SessionLocal() as session:
            row = (await session.execute(select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == spec.get("workflow_id"),
                WorkflowVersion.version == spec.get("version")))).scalar_one_or_none()
        found = caliber_of(str(spec.get("workflow_id")), row) if row is not None else None
        return [found] if found is not None else []
    return []


async def sql_match(case: Mapping[str, Any], gold: Mapping[str, Any] | None, answer: Answer, *,
                    timeout: float = 8.0) -> dict[str, Any] | None:
    """逐题的 E1：{status: proven|related|none|skipped, query, aspects, diff, reason}。没有作答运行、没有期望取数的为 None。
    比对在线程里跑（纯计算，和账本一样限时）。"""
    import asyncio
    import time

    from app.api.evidence import _sealed
    from app.ledger.match import _kinds, compare_queries, read_queries

    if not answer.run_id or answer.problem:
        return None
    calibers = await gold_calibers(case, gold)
    if not calibers:
        return None
    try:
        sealed = await _sealed(answer.run_id)
    except Exception:  # noqa: BLE001 - 读不出封存就不比
        return None
    from app.db.base import SessionLocal

    async with SessionLocal() as session:
        kinds = await _kinds(session)

    def work() -> list[dict[str, Any]]:
        queries = [q for q in read_queries(sealed, kinds)]
        return compare_queries(queries, calibers, deadline=time.monotonic() + timeout)

    try:
        rows = await asyncio.wait_for(asyncio.to_thread(work), timeout=timeout + 2)
    except asyncio.TimeoutError:
        return {"status": "skipped", "reason": "比对超过时限，没有做完"}
    except Exception:  # noqa: BLE001 - 比对是附加的一步，出错照实记没比
        logger.exception("评测：逐题比 SQL 出错")
        return {"status": "skipped", "reason": "比对出错"}
    cited = [r for r in rows if r.get("artifact") in set(answer.cited)]
    pool = cited or rows
    proven = next((r for r in pool if r.get("status") == "proven"), None)
    chosen = proven or next((r for r in pool if r.get("status") == "related"), None) or \
        next((r for r in pool if r.get("status") == "skipped"), None) or (pool[0] if pool else None)
    if chosen is None:
        return {"status": "none", "reason": "答案没有查询"}
    candidate = next((c for c in chosen.get("candidates") or [] if c.get("status") == chosen.get("status")), None) or \
        next(iter(chosen.get("candidates") or []), None) or {}
    diff = candidate.get("diff") or {}
    aspects = sorted({i.get("aspect") for i in diff.get("items") or [] if i.get("aspect")})
    sql = next((q.get("sql") for q in answer.queries if q.get("artifact") == chosen.get("artifact")), None)
    return {"status": chosen.get("status"), "query": chosen.get("artifact"), "sql": sql, "aspects": aspects,
            "diff": diff or None, "via": candidate.get("via"), "binding": candidate.get("binding"),
            "reason": chosen.get("reason") or candidate.get("reason"), "cited": bool(cited)}


__all__ = ["Answer", "CaseScore", "DEFAULT_TOLERANCE", "OUTCOMES", "OUTCOME_LABEL", "PATH_LABEL", "SCORER_VERSION",
           "TEXT_LABEL", "TEXT_OUTCOMES", "VERDICT_LABEL", "gold_calibers", "read_answer", "score_case", "sql_match"]
